#!/usr/bin/env python3
"""Axoloctl UI Host Gateway, Virtual-Host Session Router & HTTP-to-MCP Proxy."""

import argparse
import base64
from datetime import datetime
import glob
import hashlib
import html
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
from urllib.parse import parse_qs, urlparse
import urllib.request
import uuid
import zipfile

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import AxoloctlConfig, load_config

UDMI_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

ALLOWED_TMUX_KEYS = {
    "Enter",
    "C-c",
    "Escape",
    "Up",
    "Down",
    "Tab",
    "BSpace",
}

WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSConnection:
  """RFC 6455 WebSocket framing wrapper for full-duplex CLI Console streaming."""

  def __init__(self, sock: socket.socket):
    self.sock = sock
    self.lock = threading.Lock()
    self.closed = False
    try:
      self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
      pass

  def _recv_exact(self, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
      try:
        chunk = self.sock.recv(n - len(buf))
        if not chunk:
          return None
        buf.extend(chunk)
      except Exception:
        return None
    return bytes(buf)

  def send_json(self, data: Dict[str, Any]) -> bool:
    if self.closed:
      return False
    try:
      payload = json.dumps(data).encode("utf-8")
      length = len(payload)
      header = bytearray([0x81])
      if length < 126:
        header.append(length)
      elif length < 65536:
        header.append(126)
        header.extend(struct.pack(">H", length))
      else:
        header.append(127)
        header.extend(struct.pack(">Q", length))
      with self.lock:
        self.sock.sendall(header + payload)
      return True
    except Exception:
      self.closed = True
      return False

  def read_message(self) -> Optional[Dict[str, Any]]:
    fragments: List[bytes] = []
    while not self.closed:
      hdr = self._recv_exact(2)
      if not hdr:
        self.closed = True
        return None
      b1, b2 = hdr[0], hdr[1]
      fin = bool(b1 & 0x80)
      opcode = b1 & 0x0F
      masked = bool(b2 & 0x80)
      length = b2 & 0x7F

      if length == 126:
        ext = self._recv_exact(2)
        if not ext:
          self.closed = True
          return None
        length = struct.unpack(">H", ext)[0]
      elif length == 127:
        ext = self._recv_exact(8)
        if not ext:
          self.closed = True
          return None
        length = struct.unpack(">Q", ext)[0]

      mask_key = b""
      if masked:
        mask_key = self._recv_exact(4) or b""
        if len(mask_key) < 4:
          self.closed = True
          return None

      payload = self._recv_exact(length) if length > 0 else b""
      if payload is None:
        self.closed = True
        return None
      if masked and payload:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

      if opcode == 0x8:  # Close
        self.closed = True
        return None
      if opcode == 0x9:  # Ping -> Pong
        try:
          pong = bytearray([0x8A, len(payload)]) + payload if len(payload) < 126 else bytearray([0x8A, 0x00])
          with self.lock:
            self.sock.sendall(pong)
        except Exception:
          self.closed = True
          return None
        continue
      if opcode == 0xA:  # Pong
        continue

      if opcode in (0x0, 0x1):
        fragments.append(payload)
        if fin:
          raw_bytes = b"".join(fragments)
          fragments.clear()
          try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
            if isinstance(parsed, dict):
              return parsed
          except Exception:
            continue
    return None


class JetskiAgentBridge:
  """Manages per-tag communication with the session's dedicated Jetski Agent."""

  _web_bundle_lock = threading.Lock()
  _web_bundle_cache: Optional[Dict[str, bytes]] = None

  def __init__(self, udmi_root: str, config: AxoloctlConfig, sessions_dir: str):
    self.udmi_root = udmi_root
    self.config = config
    self.sessions_dir = sessions_dir
    self.session_agent = "udmi_axoloctl_agent"
    self.brain_root = os.path.expanduser("~/.gemini/jetski/brain")
    self.hub_port = int(os.environ.get("AXOLOCTL_HUB_PORT", "5387"))
    self.last_hub_error: str = ""
    self.active_hub_tag: str = ""
    self._hub_server: Optional[ThreadingHTTPServer] = None
    self._hub_server_port: Optional[int] = None
    self._hub_thread: Optional[threading.Thread] = None
    self._lock = threading.Lock()
    self._ls_cache: Dict[str, Tuple[str, str, float]] = {}
    self._pending_turns: Dict[str, Dict[str, Any]] = {}
    self._piped_tags: set = set()
    self._running_cache: Dict[str, Tuple[bool, float]] = {}
    self._tag_locks: Dict[str, threading.Lock] = {}
    self._session_dims: Dict[str, Tuple[int, int]] = {}

  def _get_tag_lock(self, tag: str) -> threading.Lock:
    with self._lock:
      lock = self._tag_locks.get(tag)
      if lock is None:
        lock = threading.Lock()
        self._tag_locks[tag] = lock
      return lock

  def _sanitize_tag(self, tag: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", (tag or "").strip())

  def resolve_default_tag(self, requested_tag: str = "") -> str:
    return self._sanitize_tag(requested_tag)

  def worktree_path(self, tag: str) -> str:
    return os.path.normpath(
        os.path.join(
            self.sessions_dir,
            tag,
            "workspace",
            self.config.app_subpath,
        )
    )

  def conv_file_path(self, tag: str) -> str:
    return os.path.join(self.sessions_dir, tag, "conversation_id.txt")

  def term_log_path(self, tag: str) -> str:
    return os.path.join(self.sessions_dir, tag, "agent_term.log")

  def _get_pane_pids(self, tag: str) -> List[str]:
    if not tag or not self.is_agent_window_running(tag):
      return []
    window_target = f"{self.session_agent}:{tag}"
    res = subprocess.run(
        ["tmux", "list-panes", "-t", window_target, "-F", "#{pane_pid}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
      return []
    root_pids = [p.strip() for p in res.stdout.splitlines() if p.strip().isdigit()]
    all_pids = set(root_pids)
    frontier = list(root_pids)
    while frontier:
      curr = frontier.pop(0)
      ch = subprocess.run(
          ["pgrep", "-P", curr],
          stdout=subprocess.PIPE,
          stderr=subprocess.DEVNULL,
          text=True,
          check=False,
      )
      if ch.returncode == 0:
        for cpid in ch.stdout.splitlines():
          cpid = cpid.strip()
          if cpid.isdigit() and cpid not in all_pids:
            all_pids.add(cpid)
            frontier.append(cpid)
    return list(all_pids)

  def _discover_pane_conversation_id(self, tag: str) -> Optional[str]:
    for pid in self._get_pane_pids(tag):
      fd_dir = f"/proc/{pid}/fd"
      try:
        for entry in os.listdir(fd_dir):
          try:
            target = os.readlink(os.path.join(fd_dir, entry))
          except OSError:
            continue
          m = re.search(
              r"/presence/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.lock$",
              target,
          )
          if m:
            candidate = m.group(1)
            if os.path.exists(os.path.join(self.brain_root, candidate)):
              return candidate
      except OSError:
        continue
    return None

  def get_conversation_id(self, tag: str) -> str:
    cfile = self.conv_file_path(tag)
    cid = ""
    if os.path.exists(cfile):
      try:
        with open(cfile, "r", encoding="utf-8") as f:
          cid = f.read().strip()
      except Exception:
        pass
    if cid and os.path.exists(os.path.join(self.brain_root, cid)):
      return cid
    pane_cid = self._discover_pane_conversation_id(tag)
    if pane_cid:
      self.set_conversation_id(tag, pane_cid)
      return pane_cid
    if cid:
      return cid
    cid = str(uuid.uuid4())
    os.makedirs(os.path.dirname(cfile), exist_ok=True)
    with open(cfile, "w", encoding="utf-8") as f:
      f.write(cid)
    return cid

  def set_conversation_id(self, tag: str, cid: str) -> None:
    cfile = self.conv_file_path(tag)
    os.makedirs(os.path.dirname(cfile), exist_ok=True)
    with open(cfile, "w", encoding="utf-8") as f:
      f.write(cid.strip())

  def is_agent_window_running(self, tag: str, use_cache: bool = False) -> bool:
    if not tag:
      return False
    now = time.time()
    if use_cache:
      cached = self._running_cache.get(tag)
      if cached and (now - cached[1]) < 1.5:
        return cached[0]
    res = subprocess.run(
        [
            "tmux",
            "list-windows",
            "-t",
            self.session_agent,
            "-F",
            "#{window_name}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    running = (
        res.returncode == 0
        and tag in [line.strip() for line in res.stdout.splitlines()]
    )
    self._running_cache[tag] = (running, now)
    return running

  def _ensure_pipe_pane(self, tag: str) -> str:
    log_file = self.term_log_path(tag)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if tag not in self._piped_tags or not os.path.exists(log_file):
      open(log_file, "a", encoding="utf-8").close()
      subprocess.run(
          [
              "tmux",
              "pipe-pane",
              "-t",
              f"{self.session_agent}:{tag}",
              f"cat >> '{log_file}'",
          ],
          check=False,
      )
      self._piped_tags.add(tag)
    return log_file

  # ---------------------------------------------------------------------------
  # Technique 1: Direct Terminal / Tmux Pane Control (cliView / xterm.js)
  # ---------------------------------------------------------------------------

  def capture_cli(
      self, tag: str, offset: Optional[int] = None
  ) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    if not resolved_tag:
      return {
          "tag": "",
          "running": False,
          "window": "",
          "output": "No active Axoloctl session. Start a session to attach its dedicated agent.",
          "data": "",
          "offset": 0,
          "cleared": False,
      }
    use_cache = offset is not None and offset > 0
    running = self.is_agent_window_running(resolved_tag, use_cache=use_cache)
    window_target = f"{self.session_agent}:{resolved_tag}"
    if not running:
      return {
          "tag": resolved_tag,
          "running": False,
          "window": window_target,
          "output": f"Agent window {window_target} is not currently running.",
          "data": "",
          "offset": offset or 0,
          "cleared": False,
      }

    log_file = self._ensure_pipe_pane(resolved_tag)
    file_len = os.path.getsize(log_file) if os.path.exists(log_file) else 0
    cleared = False
    data_bytes = b""
    new_offset = file_len

    if offset is None or offset == 0:
      cap_res = subprocess.run(
          ["tmux", "capture-pane", "-e", "-p", "-t", window_target],
          stdout=subprocess.PIPE,
          stderr=subprocess.DEVNULL,
          check=False,
      )
      if cap_res.returncode == 0 and cap_res.stdout:
        raw_screen = cap_res.stdout
        if raw_screen.endswith(b"\n"):
          raw_screen = raw_screen[:-1]
        data_bytes = raw_screen.replace(b"\n", b"\r\n")
        cur_res = subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                window_target,
                "#{cursor_x},#{cursor_y}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if cur_res.returncode == 0 and "," in (cur_res.stdout or ""):
          try:
            cx, cy = [int(x) for x in cur_res.stdout.strip().split(",", 1)]
            data_bytes += f"\033[{cy + 1};{cx + 1}H".encode("ascii")
          except ValueError:
            pass
      new_offset = os.path.getsize(log_file) if os.path.exists(log_file) else 0
    else:
      req_offset = max(0, int(offset))
      if file_len < req_offset:
        cleared = True
        req_offset = 0
      if file_len > req_offset:
        try:
          with open(log_file, "rb") as f:
            f.seek(req_offset)
            data_bytes = f.read(file_len - req_offset)
        except OSError:
          data_bytes = b""
      new_offset = file_len

    plain_output = ""
    if offset is None:
      res = subprocess.run(
          ["tmux", "capture-pane", "-p", "-J", "-S", "-200", "-t", window_target],
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          text=True,
          check=False,
      )
      raw_text = res.stdout if res.returncode == 0 else res.stderr
      plain_output = raw_text.rstrip() + "\n"

    return {
        "tag": resolved_tag,
        "running": True,
        "window": window_target,
        "workspace": self.worktree_path(resolved_tag),
        "output": plain_output,
        "data": base64.b64encode(data_bytes).decode("ascii"),
        "offset": new_offset,
        "cleared": cleared,
    }

  def _build_tmux_key_commands(
      self, window_target: str, hex_keys: List[str]
  ) -> List[List[str]]:
    cmds: List[List[str]] = []
    current_hex: List[str] = []

    def flush_hex() -> None:
      nonlocal current_hex
      if current_hex:
        cmds.append(["tmux", "send-keys", "-t", window_target, "-H"] + current_hex)
        current_hex = []

    i = 0
    n = len(hex_keys)
    while i < n:
      k = str(hex_keys[i]).lower()
      if k in ("7f", "08"):
        flush_hex()
        cmds.append(["tmux", "send-keys", "-t", window_target, "BSpace"])
        i += 1
      elif (
          i + 3 < n
          and [str(x).lower() for x in hex_keys[i : i + 4]]
          == ["1b", "5b", "33", "7e"]
      ):
        flush_hex()
        cmds.append(["tmux", "send-keys", "-t", window_target, "DC"])
        i += 4
      elif re.match(r"^[0-9a-f]{2}$", k):
        current_hex.append(k)
        i += 1
      else:
        i += 1
    flush_hex()
    return cmds

  def maybe_resize_cli_window(self, tag: str, cols: int, rows: int) -> bool:
    """Resizes the session's tmux window only when dimensions actually change."""
    resolved_tag = self.resolve_default_tag(tag)
    if not resolved_tag:
      return False
    c_val = max(20, min(500, int(cols)))
    r_val = max(5, min(200, int(rows)))
    window_target = f"{self.session_agent}:{resolved_tag}"
    with self._get_tag_lock(resolved_tag):
      if self._session_dims.get(resolved_tag) == (c_val, r_val):
        return False
      cur_res = subprocess.run(
          [
              "tmux",
              "display-message",
              "-p",
              "-t",
              window_target,
              "#{window_width}x#{window_height}",
          ],
          stdout=subprocess.PIPE,
          stderr=subprocess.DEVNULL,
          text=True,
          check=False,
      )
      if cur_res.returncode == 0 and cur_res.stdout.strip() == f"{c_val}x{r_val}":
        self._session_dims[resolved_tag] = (c_val, r_val)
        return False
      subprocess.run(
          [
              "tmux",
              "resize-window",
              "-t",
              window_target,
              "-x",
              str(c_val),
              "-y",
              str(r_val),
          ],
          check=False,
      )
      self._session_dims[resolved_tag] = (c_val, r_val)
      return True

  def send_cli_input(
      self,
      tag: str,
      text: str = "",
      key: str = "",
      submit: bool = True,
      hex_keys: Optional[List[str]] = None,
      cols: Optional[int] = None,
      rows: Optional[int] = None,
      action: str = "",
  ) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    if not resolved_tag:
      raise ValueError("No active session tag specified for CLI input.")
    use_cached_running = bool(hex_keys) and not action and not text and not key
    if not self.is_agent_window_running(resolved_tag, use_cache=use_cached_running):
      raise ValueError(
          f"Agent window '{self.session_agent}:{resolved_tag}' is not running."
      )
    window_target = f"{self.session_agent}:{resolved_tag}"

    if action == "restart":
      with self._get_tag_lock(resolved_tag):
        log_file = self.term_log_path(resolved_tag)
        if os.path.exists(log_file):
          open(log_file, "w", encoding="utf-8").close()
        runner_script = os.path.join(
            self.sessions_dir, resolved_tag, "runner_agent.sh"
        )
        if os.path.exists(runner_script):
          subprocess.run(
              ["tmux", "respawn-pane", "-k", "-t", window_target, runner_script],
              check=False,
          )
        else:
          subprocess.run(["tmux", "send-keys", "-t", window_target, "C-c"], check=False)
        self._piped_tags.discard(resolved_tag)
        self._ls_cache.pop(resolved_tag, None)
        self._session_dims.pop(resolved_tag, None)
        self._ensure_pipe_pane(resolved_tag)
      return {
          "ok": True,
          "tag": resolved_tag,
          "running": True,
          "window": window_target,
          "restarted": True,
      }

    if cols is not None and rows is not None:
      c_val = max(20, min(500, int(cols)))
      r_val = max(5, min(200, int(rows)))
      self.maybe_resize_cli_window(resolved_tag, c_val, r_val)
      if not text and not key and not hex_keys:
        return {
            "ok": True,
            "tag": resolved_tag,
            "running": True,
            "window": window_target,
            "cols": c_val,
            "rows": r_val,
        }

    if hex_keys:
      with self._get_tag_lock(resolved_tag):
        for cmd in self._build_tmux_key_commands(window_target, hex_keys):
          res = subprocess.run(cmd, check=False)
          if res.returncode != 0:
            self._running_cache.pop(resolved_tag, None)
      return {
          "ok": True,
          "tag": resolved_tag,
          "running": True,
          "window": window_target,
      }

    with self._get_tag_lock(resolved_tag):
      if text:
        subprocess.run(
            ["tmux", "send-keys", "-t", window_target, "-l", "--", text],
            check=True,
        )
        if submit:
          subprocess.run(
              ["tmux", "send-keys", "-t", window_target, "Enter"],
              check=True,
          )
      if key:
        if key not in ALLOWED_TMUX_KEYS:
          raise ValueError(
              f"Unsupported terminal key '{key}'. Allowed: {sorted(ALLOWED_TMUX_KEYS)}"
          )
        subprocess.run(
            ["tmux", "send-keys", "-t", window_target, key],
            check=True,
        )
    time.sleep(0.15)
    state = self.capture_cli(resolved_tag)
    state["ok"] = True
    return state

  def handle_cli_websocket(
      self, sock: socket.socket, initial_tag: str = ""
  ) -> None:
    """Full-duplex WebSocket loop for ungarbled keystroke dispatch and 25ms PTY streaming."""
    ws = WSConnection(sock)
    active_tag = self.resolve_default_tag(initial_tag)
    stream_stop: Optional[threading.Event] = None
    stream_thread: Optional[threading.Thread] = None

    def stop_current_stream() -> None:
      nonlocal stream_stop, stream_thread
      if stream_stop is not None:
        stream_stop.set()
      if stream_thread is not None and stream_thread.is_alive():
        stream_thread.join(timeout=0.5)
      stream_stop = None
      stream_thread = None

    def start_stream_for_tag(
        tag_name: str,
        req_offset: int = 0,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
    ) -> None:
      nonlocal active_tag, stream_stop, stream_thread
      resolved = self.resolve_default_tag(tag_name)
      if not resolved:
        return
      stop_current_stream()
      active_tag = resolved
      stop_ev = threading.Event()
      stream_stop = stop_ev

      if (
          cols is not None
          and rows is not None
          and self.is_agent_window_running(resolved, use_cache=True)
      ):
        resized = self.maybe_resize_cli_window(resolved, cols, rows)
        if resized and req_offset == 0:
          time.sleep(0.05)

      def _stream_worker() -> None:
        window_target = f"{self.session_agent}:{resolved}"
        cur_offset = max(0, int(req_offset))
        last_running: Optional[bool] = None
        last_status_ts = 0.0

        is_running = self.is_agent_window_running(resolved, use_cache=False)
        last_running = is_running
        last_status_ts = time.time()
        if not ws.send_json({
            "type": "status",
            "tag": resolved,
            "running": is_running,
            "window": window_target,
        }):
          return

        if is_running and cur_offset == 0:
          snap = self.capture_cli(resolved, offset=0)
          cur_offset = int(snap.get("offset") or 0)
          if snap.get("data"):
            if not ws.send_json({
                "type": "snapshot",
                "tag": resolved,
                "data": snap["data"],
                "offset": cur_offset,
            }):
              return

        while not ws.closed and not stop_ev.is_set():
          try:
            now = time.time()
            if now - last_status_ts >= 1.0:
              is_running = self.is_agent_window_running(resolved, use_cache=False)
              last_status_ts = now
              if is_running != last_running:
                last_running = is_running
                if not ws.send_json({
                    "type": "status",
                    "tag": resolved,
                    "running": is_running,
                    "window": window_target,
                }):
                  break

            if is_running:
              log_file = self._ensure_pipe_pane(resolved)
              file_size = (
                  os.path.getsize(log_file) if os.path.exists(log_file) else 0
              )
              if file_size < cur_offset:
                cur_offset = 0
                if not ws.send_json({
                    "type": "cleared",
                    "tag": resolved,
                    "offset": 0,
                }):
                  break
              if file_size > cur_offset:
                with open(log_file, "rb") as f:
                  f.seek(cur_offset)
                  chunk = f.read(min(65536, file_size - cur_offset))
                if chunk:
                  cur_offset += len(chunk)
                  if not ws.send_json({
                      "type": "output",
                      "tag": resolved,
                      "data": base64.b64encode(chunk).decode("ascii"),
                      "offset": cur_offset,
                  }):
                    break
          except Exception:
            pass
          stop_ev.wait(0.025)

      thr = threading.Thread(target=_stream_worker, daemon=True)
      stream_thread = thr
      thr.start()

    try:
      while not ws.closed:
        msg = ws.read_message()
        if msg is None:
          break
        msg_type = str(msg.get("type") or "")
        target_tag = (
            self.resolve_default_tag(str(msg.get("tag") or "")) or active_tag
        )

        if msg_type in ("attach", "subscribe"):
          cols_arg = (
              int(msg["cols"])
              if "cols" in msg and msg["cols"] is not None
              else None
          )
          rows_arg = (
              int(msg["rows"])
              if "rows" in msg and msg["rows"] is not None
              else None
          )
          offset_arg = int(msg.get("offset") or 0)
          if target_tag:
            start_stream_for_tag(
                target_tag,
                req_offset=offset_arg,
                cols=cols_arg,
                rows=rows_arg,
            )

        elif msg_type == "resize":
          if (
              target_tag
              and msg.get("cols") is not None
              and msg.get("rows") is not None
          ):
            self.maybe_resize_cli_window(
                target_tag, int(msg["cols"]), int(msg["rows"])
            )

        elif msg_type == "input":
          if not target_tag:
            continue
          raw_hex = msg.get("hexKeys")
          hex_keys: Optional[List[str]] = (
              [str(k) for k in raw_hex] if isinstance(raw_hex, list) else None
          )
          if hex_keys is None and isinstance(msg.get("data"), str) and msg["data"]:
            hex_keys = [f"{b:02x}" for b in msg["data"].encode("utf-8")]
          try:
            self.send_cli_input(
                tag=target_tag,
                text=str(msg.get("text") or ""),
                key=str(msg.get("key") or ""),
                submit=bool(msg.get("submit", True)),
                hex_keys=hex_keys,
                action=str(msg.get("action") or ""),
            )
          except Exception as e:
            ws.send_json({
                "type": "error",
                "tag": target_tag,
                "error": str(e),
            })

        elif msg_type == "ping":
          ws.send_json({"type": "pong"})
    finally:
      ws.closed = True
      stop_current_stream()
      try:
        sock.close()
      except Exception:
        pass

  # ---------------------------------------------------------------------------
  # Technique 2: Jetski Web Hub Management (hubView)
  # ---------------------------------------------------------------------------

  def _is_port_listening(self, port: int) -> bool:
    try:
      with socket.create_connection(("127.0.0.1", port), timeout=0.5):
        return True
    except OSError:
      return False

  def _load_web_bundle_files(self) -> Dict[str, bytes]:
    with JetskiAgentBridge._web_bundle_lock:
      if JetskiAgentBridge._web_bundle_cache is not None:
        return JetskiAgentBridge._web_bundle_cache

      candidates: List[str] = []
      try:
        agentapi_bin = self._get_agentapi_bin()
        if agentapi_bin:
          candidates.append(agentapi_bin)
      except Exception:
        pass
      candidates.extend(
          sorted(
              glob.glob(
                  "/tmp/sar.cli_internal.*/cli_internal_impl.runfiles/google3/third_party/jetski/cmd/cli/cli"
              ),
              reverse=True,
          )
      )
      candidates.extend(
          sorted(
              glob.glob(
                  "/tmp/sar.server.*/server_bin.runfiles/google3/third_party/jetski/cmd/hub/server/jetski-hub-server"
              ),
              reverse=True,
          )
      )

      seen: set = set()
      marker = b"antigravityActionRequired.mp3"
      for bin_path in candidates:
        if not bin_path or bin_path in seen or not os.path.isfile(bin_path):
          continue
        seen.add(bin_path)
        try:
          if os.path.getsize(bin_path) < 1024 * 1024:
            continue
          with open(bin_path, "rb") as f:
            data = f.read()
          idx = data.find(marker)
          if idx <= 30:
            continue
          start = data.rfind(b"PK\x03\x04", max(0, idx - 64), idx)
          if start < 0:
            continue
          search_pos = start
          limit = min(len(data), start + 30 * 1024 * 1024)
          while search_pos < limit:
            eocd = data.find(b"PK\x05\x06", search_pos, limit)
            if eocd < 0:
              break
            try:
              with zipfile.ZipFile(io.BytesIO(data[start : eocd + 22])) as zf:
                files = {
                    info.filename: zf.read(info.filename)
                    for info in zf.infolist()
                    if not info.is_dir()
                }
                if "index.html" in files:
                  JetskiAgentBridge._web_bundle_cache = files
                  return files
            except Exception:
              pass
            search_pos = eocd + 4
        except Exception:
          continue

      return {
          "index.html": (
              b'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
              b"<title>Jetski Web Hub</title></head>"
              b'<body><div id="root">Jetski Web Hub</div></body></html>'
          )
      }

  def _resolve_hub_tag(self, requested_tag: str = "") -> str:
    clean = self._sanitize_tag(requested_tag)
    if clean:
      self.active_hub_tag = clean
      return clean
    if self.active_hub_tag:
      return self.active_hub_tag
    ports_file = os.path.join(self.sessions_dir, "ports.json")
    if os.path.exists(ports_file):
      try:
        with open(ports_file, "r", encoding="utf-8") as f:
          ports_map = json.load(f)
        if isinstance(ports_map, dict) and ports_map:
          tags = [self._sanitize_tag(str(k)) for k in ports_map.keys()]
          tags = [t for t in tags if t]
          for t in tags:
            if self.is_agent_window_running(t, use_cache=True):
              self.active_hub_tag = t
              return t
          if tags:
            self.active_hub_tag = tags[0]
            return tags[0]
      except Exception:
        pass
    return ""

  def _render_hub_index(self, tag: str) -> bytes:
    resolved_tag = self._resolve_hub_tag(tag)
    files = self._load_web_bundle_files()
    raw_html = files.get(
        "index.html",
        b'<!DOCTYPE html><html lang="en"><head></head><body></body></html>',
    ).decode("utf-8", errors="replace")

    csrf_token = ""
    if resolved_tag:
      _, csrf_token = self._resolve_ls_credentials(resolved_tag)
    if not csrf_token:
      csrf_token = f"axoloctl-{resolved_tag}" if resolved_tag else "axoloctl-hub"

    app_config = {
        "productName": "jetski",
        "appVersion": "2026.09.24.04",
        "csrfToken": csrf_token,
        "devMode": False,
        "startupWarning": "",
    }
    inject_head = (
        "<head>\n"
        f"<script>window.__APP_CONFIG__ = {json.dumps(app_config)};</script>\n"
        "<script>\n"
        "(function() {\n"
        "  'use strict';\n"
        "  try {\n"
        f"    var hubTag = {json.dumps(resolved_tag)};\n"
        "    if (hubTag) {\n"
        "      document.cookie = 'axoloctl_hub_tag=' + encodeURIComponent(hubTag) + '; path=/; SameSite=Lax';\n"
        "    }\n"
        "  } catch (e) {}\n"
        "  if (window.nativeStorage) return;\n"
        "  window.nativeStorage = {\n"
        "    getItems: async function() {\n"
        "      const items = {};\n"
        "      for (let i = 0; i < localStorage.length; i++) {\n"
        "        const key = localStorage.key(i);\n"
        "        if (key && key.startsWith('ag:')) {\n"
        "          items[key.slice(3)] = localStorage.getItem(key);\n"
        "        }\n"
        "      }\n"
        "      return items;\n"
        "    },\n"
        "    updateItems: async function(changes) {\n"
        "      for (const [key, value] of Object.entries(changes)) {\n"
        "        const storageKey = 'ag:' + key;\n"
        "        if (value == null) {\n"
        "          localStorage.removeItem(storageKey);\n"
        "        } else {\n"
        "          localStorage.setItem(storageKey, value);\n"
        "        }\n"
        "      }\n"
        "    },\n"
        "  };\n"
        "})();\n"
        "</script>"
    )
    if "<head>" in raw_html:
      raw_html = raw_html.replace("<head>", inject_head, 1)
    return raw_html.encode("utf-8")

  def _ensure_hub_server(self) -> bool:
    with self._lock:
      if (
          self._hub_server is not None
          and self._hub_server_port == self.hub_port
      ):
        return True
      if self._hub_server is not None:
        try:
          self._hub_server.shutdown()
          self._hub_server.server_close()
        except Exception:
          pass
        self._hub_server = None
        self._hub_server_port = None

      if self._is_port_listening(self.hub_port):
        self.last_hub_error = ""
        return True

      bridge_ref = self

      class BoundHubHandler(HubRequestHandler):
        bridge = bridge_ref

      try:
        ThreadingHTTPServer.allow_reuse_address = True
        srv = ThreadingHTTPServer(("127.0.0.1", self.hub_port), BoundHubHandler)
        thr = threading.Thread(target=srv.serve_forever, daemon=True)
        thr.start()
        self._hub_server = srv
        self._hub_server_port = self.hub_port
        self._hub_thread = thr
        self.last_hub_error = ""
        return True
      except OSError as e:
        if self._is_port_listening(self.hub_port):
          self.last_hub_error = ""
          return True
        self.last_hub_error = str(e)
        return False

  def stop_hub(self) -> None:
    with self._lock:
      srv = self._hub_server
      self._hub_server = None
      self._hub_server_port = None
      self._hub_thread = None
    if srv is not None:
      try:
        srv.shutdown()
        srv.server_close()
      except Exception:
        pass

  def get_hub_status(self, tag: str) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    if resolved_tag:
      self.active_hub_tag = resolved_tag
    wt_path = self.worktree_path(resolved_tag) if resolved_tag else self.udmi_root
    running = self._ensure_hub_server()
    hub_url = (
        f"http://localhost:{self.hub_port}/?tag={resolved_tag}&hostTheme=dark"
        if resolved_tag
        else f"http://localhost:{self.hub_port}/"
    )
    return {
        "tag": resolved_tag,
        "hub_port": self.hub_port,
        "hub_url": hub_url,
        "running": running,
        "workspace": wt_path,
        "last_error": "" if running else self.last_hub_error,
    }

  def start_hub(self, tag: str) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    if resolved_tag:
      self.active_hub_tag = resolved_tag
      self._ls_cache.pop(resolved_tag, None)
      if not self.is_agent_window_running(resolved_tag):
        runner_script = os.path.join(
            self.sessions_dir, resolved_tag, "runner_agent.sh"
        )
        if os.path.exists(runner_script):
          subprocess.run(
              [
                  "tmux",
                  "new-window",
                  "-d",
                  "-t",
                  self.session_agent,
                  "-n",
                  resolved_tag,
                  runner_script,
              ],
              check=False,
          )
          self._running_cache.pop(resolved_tag, None)
    self._ensure_hub_server()
    return self.get_hub_status(resolved_tag)

  # ---------------------------------------------------------------------------
  # Technique 3: Structured Chat via Jetski agentapi & transcript.jsonl (apiView)
  # ---------------------------------------------------------------------------

  def _get_jetski_bin(self) -> str:
    for candidate in [
        os.path.expanduser("~/bin/jetski"),
        os.environ.get("ANTIGRAVITY_AGENTAPI_EXE", ""),
        "/google/bin/releases/jetski-devs/tools/cli",
    ]:
      if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
        return candidate
    sar_bins = sorted(
        glob.glob(
            "/tmp/sar.cli_internal.*/cli_internal_impl.runfiles/google3/third_party/jetski/cmd/cli/cli"
        )
    )
    for b in reversed(sar_bins):
      if os.access(b, os.X_OK):
        return b
    return "jetski"

  def _get_agentapi_bin(self) -> str:
    """Resolves the unwrapped internal Jetski CLI binary so argv[1] == 'agentapi'.

    The outer /google/bin/releases/jetski-devs/tools/cli wrapper prepends
    '--app_data_dir=jetski' before '$@', which prevents the subcommand router
    from recognizing 'agentapi' at argv[1]. Invoking the extracted SAR binary
    directly passes 'agentapi' as argv[1].
    """
    env_exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE", "")
    if env_exe and os.path.exists(env_exe) and os.access(env_exe, os.X_OK):
      return env_exe

    sar_pattern = "/tmp/sar.cli_internal.*/cli_internal_impl.runfiles/google3/third_party/jetski/cmd/cli/cli"
    sar_bins = sorted(
        glob.glob(sar_pattern),
        key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
        reverse=True,
    )
    for b in sar_bins:
      if os.access(b, os.X_OK):
        return b

    # Trigger SAR extraction via --help if not yet extracted in /tmp
    wrapper = self._get_jetski_bin()
    try:
      subprocess.run(
          [wrapper, "--help"],
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
          timeout=10,
          check=False,
      )
    except Exception:
      pass

    sar_bins = sorted(
        glob.glob(sar_pattern),
        key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
        reverse=True,
    )
    for b in sar_bins:
      if os.access(b, os.X_OK):
        return b
    return wrapper

  def _read_tmux_global_env(self) -> Dict[str, str]:
    env_map: Dict[str, str] = {}
    res = subprocess.run(
        ["tmux", "show-environment", "-g"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if res.returncode == 0:
      for line in res.stdout.splitlines():
        if "=" in line and not line.startswith("-"):
          k, v = line.split("=", 1)
          env_map[k.strip()] = v.strip()
    return env_map

  def _discover_pane_ports(self, tag: str) -> List[str]:
    all_pids = self._get_pane_pids(tag)
    if not all_pids:
      return []
    lsof_res = subprocess.run(
        [
            "lsof",
            "-w",
            "-a",
            "-p",
            ",".join(all_pids),
            "-i",
            "tcp",
            "-s",
            "TCP:LISTEN",
            "-n",
            "-P",
            "-F",
            "n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    ports: List[str] = []
    for line in lsof_res.stdout.splitlines():
      if line.startswith("n") and ":" in line:
        port = line.rsplit(":", 1)[-1].strip()
        if port.isdigit() and port not in ports:
          ports.append(port)
    return ports

  def _resolve_ls_credentials(self, tag: str) -> Tuple[str, str]:
    now = time.time()
    cached = self._ls_cache.get(tag)
    if cached and (now - cached[2]) < 10.0:
      return cached[0], cached[1]

    agentapi_bin = self._get_agentapi_bin()
    tmux_env = self._read_tmux_global_env()
    host_ls = os.environ.get("ANTIGRAVITY_LS_ADDRESS") or tmux_env.get(
        "ANTIGRAVITY_LS_ADDRESS", ""
    )
    host_csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN") or tmux_env.get(
        "ANTIGRAVITY_CSRF_TOKEN", ""
    )

    candidates: List[Tuple[str, str]] = []
    for port in reversed(self._discover_pane_ports(tag)):
      candidates.append((f"localhost:{port}", f"axoloctl-{tag}"))
      if host_csrf:
        candidates.append((f"localhost:{port}", host_csrf))
    if host_ls:
      candidates.append((host_ls, host_csrf))

    probe_cid = self.get_conversation_id(tag)
    for addr, csrf in candidates:
      env = self._build_agentapi_env(addr, csrf)
      try:
        res = subprocess.run(
            [agentapi_bin, "agentapi", "get-conversation-metadata", probe_cid],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=1.5,
            check=False,
        )
        combined = (res.stdout or "") + (res.stderr or "")
        if (
            '"response"' in (res.stdout or "")
            and "code = Unavailable" not in combined
            and "connection refused" not in combined
            and "code = Unauthenticated" not in combined
        ):
          self._ls_cache[tag] = (addr, csrf, now)
          return addr, csrf
      except Exception:
        continue

    if candidates:
      return candidates[0][0], candidates[0][1]
    return "", ""

  def _build_agentapi_env(self, ls_address: str, csrf_token: str) -> Dict[str, str]:
    env = os.environ.copy()
    for k in [
        "ANTIGRAVITY_AGENT",
        "ANTIGRAVITY_CONVERSATION_ID",
        "ANTIGRAVITY_SOURCE_METADATA",
        "ANTIGRAVITY_TRAJECTORY_ID",
    ]:
      env.pop(k, None)
    if ls_address:
      env["ANTIGRAVITY_LS_ADDRESS"] = ls_address
    if csrf_token:
      env["ANTIGRAVITY_CSRF_TOKEN"] = csrf_token
    return env

  def _conversation_exists_on_ls(
      self, agentapi_bin: str, cid: str, env: Dict[str, str], cwd: str
  ) -> bool:
    if not cid:
      return False
    tfile = os.path.join(
        self.brain_root, cid, ".system_generated", "logs", "transcript.jsonl"
    )
    try:
      res = subprocess.run(
          [agentapi_bin, "agentapi", "get-conversation-metadata", cid],
          cwd=cwd,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          text=True,
          env=env,
          timeout=4,
          check=False,
      )
      if res.returncode == 0 and '"conversationMetadata"' in res.stdout:
        return True
    except Exception:
      pass
    return os.path.exists(tfile)

  def _get_last_transcript_step(self, cid: str) -> int:
    if not cid:
      return -1
    tfile = os.path.join(
        self.brain_root, cid, ".system_generated", "logs", "transcript.jsonl"
    )
    if not os.path.exists(tfile):
      return -1
    max_step = -1
    try:
      with open(tfile, "r", encoding="utf-8") as f:
        for line in f:
          line = line.strip()
          if not line:
            continue
          try:
            entry = json.loads(line)
            max_step = max(max_step, int(entry.get("step_index", -1)))
          except Exception:
            continue
    except Exception:
      pass
    return max_step

  def _extract_tool_action(self, tc: Dict[str, Any]) -> str:
    name = str(tc.get("name") or "tool").strip()
    args = tc.get("args") or tc.get("arguments") or {}
    if isinstance(args, dict):
      raw_action = args.get("toolAction") or args.get("toolSummary") or ""
      if isinstance(raw_action, str):
        clean_action = raw_action.strip().strip('"').strip()
        if clean_action:
          return clean_action
    return name

  def _parse_iso_epoch(self, ts: Optional[str]) -> Optional[float]:
    if not ts or not isinstance(ts, str):
      return None
    try:
      return datetime.fromisoformat(ts.strip().replace("Z", "+00:00")).timestamp()
    except Exception:
      return None

  def send_chat_prompt(self, tag: str, prompt: str) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    if not resolved_tag:
      raise ValueError("No active session tag specified for chat.")
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
      raise ValueError("Chat prompt cannot be empty.")

    wt_path = self.worktree_path(resolved_tag)
    cwd = wt_path if os.path.isdir(wt_path) else self.udmi_root
    agentapi_bin = self._get_agentapi_bin()
    ls_address, csrf_token = self._resolve_ls_credentials(resolved_tag)
    if not ls_address:
      raise RuntimeError(
          f"No reachable Jetski Language Server found for session '{resolved_tag}'."
      )

    env = self._build_agentapi_env(ls_address, csrf_token)
    cid = self.get_conversation_id(resolved_tag)

    with self._lock:
      if self._conversation_exists_on_ls(agentapi_bin, cid, env, cwd):
        baseline_step = self._get_last_transcript_step(cid)
        res = subprocess.run(
            [agentapi_bin, "agentapi", "send-message", cid, clean_prompt],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )
        if res.returncode == 0:
          self._pending_turns[resolved_tag] = {
              "cid": cid,
              "prompt": clean_prompt,
              "baseline_step": baseline_step,
              "sent_at": time.time(),
          }
          return {
              "ok": True,
              "tag": resolved_tag,
              "conversation_id": cid,
              "ls_address": ls_address,
          }

      # Start a new conversation scoped to this session's isolated worktree
      contextual_prompt = (
          f"[Axoloctl Session: {resolved_tag} | Worktree: {wt_path} | Branch: axoloctl-{resolved_tag}]\n"
          f"{clean_prompt}"
      )
      res_new = subprocess.run(
          [
              agentapi_bin,
              "agentapi",
              "new-conversation",
              f"--title=Axoloctl [{resolved_tag}]",
              contextual_prompt,
          ],
          cwd=cwd,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          text=True,
          env=env,
          timeout=20,
          check=False,
      )
      if res_new.returncode != 0:
        err_detail = (res_new.stdout or "") + "\n" + (res_new.stderr or "")
        raise RuntimeError(
            f"jetski agentapi failed ({ls_address}): {err_detail.strip()}"
        )

      new_cid = ""
      for line in res_new.stdout.splitlines():
        candidate = line.strip()
        if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", candidate):
          new_cid = candidate
          break
      if not new_cid:
        m = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            res_new.stdout,
        )
        if m:
          new_cid = m.group(1)

      if not new_cid:
        raise RuntimeError(
            f"Could not parse conversation UUID from agentapi output: {res_new.stdout.strip()}"
        )

      self.set_conversation_id(resolved_tag, new_cid)
      self._pending_turns[resolved_tag] = {
          "cid": new_cid,
          "prompt": clean_prompt,
          "baseline_step": -1,
          "sent_at": time.time(),
      }
      return {
          "ok": True,
          "tag": resolved_tag,
          "conversation_id": new_cid,
          "ls_address": ls_address,
      }

  def reset_conversation(self, tag: str) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    if not resolved_tag:
      raise ValueError("No active session tag specified.")
    new_cid = str(uuid.uuid4())
    self.set_conversation_id(resolved_tag, new_cid)
    self._pending_turns.pop(resolved_tag, None)
    return {
        "ok": True,
        "tag": resolved_tag,
        "conversation_id": new_cid,
    }

  def _strip_context_prefix(self, text: str) -> str:
    cleaned = re.sub(
        r"^\[Axoloctl Session:[^\]]*\]\s*", "", text.strip()
    )
    return cleaned

  def get_chat_messages(self, tag: str, since_step: int = -1) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    if not resolved_tag:
      return {
          "tag": "",
          "conversation_id": None,
          "messages": [],
          "last_step": -1,
          "agent_state": {
              "is_busy": False,
              "status": "idle",
              "active_tool": None,
              "thinking": "",
              "recent_actions": [],
              "step_count": 0,
              "elapsed_s": 0,
          },
      }

    cid = self.get_conversation_id(resolved_tag)
    tfile = os.path.join(
        self.brain_root, cid, ".system_generated", "logs", "transcript.jsonl"
    )
    messages: List[Dict[str, Any]] = []
    last_step = since_step
    max_seen_step = -1
    last_entry: Optional[Dict[str, Any]] = None
    turn_actions: List[str] = []
    turn_thinking: str = ""
    turn_started_epoch: Optional[float] = None
    running_tasks: Dict[str, str] = {}

    if os.path.exists(tfile):
      try:
        with open(tfile, "r", encoding="utf-8") as f:
          for line in f:
            line = line.strip()
            if not line:
              continue
            try:
              entry = json.loads(line)
            except json.JSONDecodeError:
              continue
            last_entry = entry
            step_idx = int(entry.get("step_index", 0))
            max_seen_step = max(max_seen_step, step_idx)

            etype = entry.get("type")
            content = entry.get("content", "")

            if etype == "USER_INPUT":
              turn_actions = []
              turn_thinking = ""
              running_tasks = {}
              turn_started_epoch = self._parse_iso_epoch(entry.get("created_at"))
              if step_idx > since_step:
                m = re.search(
                    r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>",
                    content,
                    re.DOTALL,
                )
                clean_text = self._strip_context_prefix(
                    m.group(1).strip() if m else content.strip()
                )
                if clean_text:
                  messages.append({
                      "step_index": step_idx,
                      "role": "user",
                      "content": clean_text,
                      "timestamp": entry.get("created_at"),
                  })
                last_step = max(last_step, step_idx)

            elif etype == "SYSTEM_MESSAGE":
              if "/task-" in content:
                m_sender = re.search(r"sender=([^\s]+)", content)
                if m_sender:
                  running_tasks.pop(m_sender.group(1).strip(), None)
                if step_idx > since_step:
                  last_step = max(last_step, step_idx)
              else:
                turn_actions = []
                turn_thinking = ""
                running_tasks = {}
                m_ts = re.search(r"timestamp=([^\s]+)", content)
                turn_started_epoch = (
                    self._parse_iso_epoch(m_ts.group(1) if m_ts else None)
                    or self._parse_iso_epoch(entry.get("created_at"))
                )
                if step_idx > since_step:
                  m = re.search(
                      r"\[Message\].*?content=(.*?)\s*</SYSTEM_MESSAGE>",
                      content,
                      re.DOTALL,
                  )
                  if m:
                    clean_text = self._strip_context_prefix(m.group(1).strip())
                    if clean_text:
                      messages.append({
                          "step_index": step_idx,
                          "role": "user",
                          "content": clean_text,
                          "timestamp": entry.get("created_at"),
                      })
                  last_step = max(last_step, step_idx)

            elif etype == "PLANNER_RESPONSE":
              raw_thinking = (entry.get("thinking") or "").strip()
              if raw_thinking:
                first_line = raw_thinking.splitlines()[0].strip()
                if len(first_line) > 140:
                  first_line = first_line[:137] + "..."
                turn_thinking = first_line

              for tc in entry.get("tool_calls", []) or []:
                if tc.get("name") != "send_message":
                  label = self._extract_tool_action(tc)
                  if not turn_actions or turn_actions[-1] != label:
                    turn_actions.append(label)

              if step_idx > since_step:
                msg_text = content.strip()
                if not msg_text:
                  for tc in entry.get("tool_calls", []) or []:
                    if tc.get("name") == "send_message":
                      args_dict = tc.get("arguments") or tc.get("args") or {}
                      msg_text = str(args_dict.get("Message", "")).strip().strip('"')
                      if msg_text:
                        break
                if msg_text:
                  messages.append({
                      "step_index": step_idx,
                      "role": "assistant",
                      "content": msg_text,
                      "thinking": entry.get("thinking", ""),
                      "timestamp": entry.get("created_at"),
                  })
                last_step = max(last_step, step_idx)

            elif etype in ("GENERIC", "TOOL_RESPONSE"):
              if entry.get("status") == "RUNNING":
                m_task = re.search(r"task id:\s*([^\s\n]+)", content)
                if m_task:
                  task_id = m_task.group(1).strip()
                  running_tasks[task_id] = (
                      turn_actions[-1] if turn_actions else "Running background task"
                  )
              if step_idx > since_step:
                last_step = max(last_step, step_idx)
      except Exception:
        pass

    now = time.time()
    pending = self._pending_turns.get(resolved_tag)
    in_preflush_window = (
        pending is not None
        and pending.get("cid") == cid
        and max_seen_step <= int(pending.get("baseline_step", -1))
        and (now - float(pending.get("sent_at", 0.0))) < 180.0
    )

    if in_preflush_window:
      elapsed_s = max(0, int(now - float(pending["sent_at"])))
      agent_state: Dict[str, Any] = {
          "is_busy": True,
          "status": "thinking",
          "active_tool": "Analyzing prompt & planning...",
          "thinking": "",
          "recent_actions": [],
          "step_count": 0,
          "elapsed_s": elapsed_s,
          "pending_prompt": pending.get("prompt", ""),
      }
    else:
      is_busy = False
      status = "ready"
      active_tool: Optional[str] = None

      if last_entry is not None:
        ltype = last_entry.get("type")
        lstatus = last_entry.get("status")
        ltools = [
            tc
            for tc in (last_entry.get("tool_calls") or [])
            if tc.get("name") != "send_message"
        ]
        if lstatus in ("RUNNING", "PENDING", "IN_PROGRESS"):
          is_busy = True
          status = "executing_tool" if turn_actions else "thinking"
          active_tool = turn_actions[-1] if turn_actions else "Processing..."
        elif ltype in ("USER_INPUT", "SYSTEM_MESSAGE", "TOOL_RESPONSE", "GENERIC"):
          is_busy = True
          status = "thinking"
          active_tool = (
              f"Evaluating: {turn_actions[-1]}" if turn_actions else "Thinking..."
          )
        elif ltype == "PLANNER_RESPONSE" and ltools:
          action_labels = [self._extract_tool_action(tc) for tc in ltools]
          is_busy = True
          status = "executing_tool"
          active_tool = ", ".join(action_labels)
        elif running_tasks:
          active_task_desc = next(iter(running_tasks.values()))
          is_busy = True
          status = "executing_tool"
          active_tool = f"{active_task_desc} (background task)"

      if is_busy:
        start_ref = (
            float(pending["sent_at"])
            if (pending and pending.get("cid") == cid)
            else (turn_started_epoch or now)
        )
        elapsed_s = max(0, int(now - start_ref))
        agent_state = {
            "is_busy": True,
            "status": status,
            "active_tool": active_tool,
            "thinking": turn_thinking,
            "recent_actions": turn_actions[-5:],
            "step_count": len(turn_actions),
            "elapsed_s": elapsed_s,
        }
      else:
        if pending and pending.get("cid") == cid:
          self._pending_turns.pop(resolved_tag, None)
        agent_state = {
            "is_busy": False,
            "status": "ready",
            "active_tool": None,
            "thinking": "",
            "recent_actions": [],
            "step_count": len(turn_actions),
            "elapsed_s": 0,
        }

    return {
        "tag": resolved_tag,
        "conversation_id": cid,
        "workspace": self.worktree_path(resolved_tag),
        "messages": messages,
        "last_step": last_step,
        "agent_state": agent_state,
    }


class HubRequestHandler(BaseHTTPRequestHandler):
  """HTTP handler for the embedded Jetski Web Hub SPA and Language Server proxy."""

  protocol_version = "HTTP/1.1"
  bridge: Optional[JetskiAgentBridge] = None

  _MIME_OVERRIDES = {
      ".js": "application/javascript; charset=utf-8",
      ".mjs": "application/javascript; charset=utf-8",
      ".css": "text/css; charset=utf-8",
      ".html": "text/html; charset=utf-8",
      ".json": "application/json; charset=utf-8",
      ".svg": "image/svg+xml",
      ".wasm": "application/wasm",
      ".mp3": "audio/mpeg",
      ".ttf": "font/ttf",
      ".woff2": "font/woff2",
      ".ico": "image/x-icon",
      ".png": "image/png",
  }

  def _extract_request_tag(self) -> str:
    if self.bridge is None:
      return ""
    parsed = urlparse(self.path)
    q_tag = (parse_qs(parsed.query).get("tag") or [""])[0].strip()
    if q_tag:
      return self.bridge._resolve_hub_tag(q_tag)

    csrf_hdr = (self.headers.get("x-codeium-csrf-token") or "").strip()
    if csrf_hdr.startswith("axoloctl-"):
      suffix = csrf_hdr[len("axoloctl-") :].strip()
      if suffix and suffix != "hub":
        return self.bridge._resolve_hub_tag(suffix)

    referer = (self.headers.get("Referer") or "").strip()
    if referer:
      ref_tag = (parse_qs(urlparse(referer).query).get("tag") or [""])[0].strip()
      if ref_tag:
        return self.bridge._resolve_hub_tag(ref_tag)

    cookie_hdr = self.headers.get("Cookie") or ""
    m = re.search(r"(?:^|;\s*)axoloctl_hub_tag=([^;\s]+)", cookie_hdr)
    if m:
      return self.bridge._resolve_hub_tag(m.group(1))

    return self.bridge._resolve_hub_tag("")

  def _is_rpc_or_api_path(self, path: str) -> bool:
    return path.startswith((
        "/exa.",
        "/learning.",
        "/proxy/",
        "/clearcut_proxy",
        "/healthz",
        "/metrics",
        "/debug/",
        "/static/artifacts/",
        "/connect-websocket",
    ))

  def _guess_mime_type(self, rel_path: str) -> str:
    _, ext = os.path.splitext(rel_path.lower())
    if ext in self._MIME_OVERRIDES:
      return self._MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(rel_path)
    return guessed or "application/octet-stream"

  def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
    raw = json.dumps(payload).encode("utf-8")
    origin = self.headers.get("Origin") or "*"
    self.send_response(status_code)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(raw)))
    self.send_header("Access-Control-Allow-Origin", origin)
    self.send_header("Access-Control-Allow-Credentials", "true")
    self.end_headers()
    self.wfile.write(raw)

  def _proxy_to_ls(self, method: str) -> None:
    if self.bridge is None:
      self._send_json(503, {"code": "unavailable", "message": "Hub bridge not initialized."})
      return

    tag = self._extract_request_tag()
    ls_addr, csrf_token = self.bridge._resolve_ls_credentials(tag) if tag else ("", "")
    if not ls_addr:
      self._send_json(
          503,
          {
              "code": "unavailable",
              "message": f"No active Jetski Language Server found for session '{tag}'.",
          },
      )
      return

    content_len = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_len) if content_len > 0 else b""

    upstream_sock: Optional[socket.socket] = None
    ls_host = "127.0.0.1"
    ls_port = 0
    for attempt in range(2):
      if not ls_addr or ":" not in ls_addr:
        break
      host_part, port_part = ls_addr.rsplit(":", 1)
      ls_host = (
          "127.0.0.1"
          if host_part in ("localhost", "127.0.0.1", "")
          else host_part
      )
      try:
        ls_port = int(port_part)
        upstream_sock = socket.create_connection((ls_host, ls_port), timeout=5.0)
        break
      except (OSError, ValueError):
        upstream_sock = None
        if attempt == 0 and tag:
          self.bridge._ls_cache.pop(tag, None)
          ls_addr, csrf_token = self.bridge._resolve_ls_credentials(tag)

    if upstream_sock is None:
      self._send_json(
          502,
          {
              "code": "unavailable",
              "message": f"Failed to connect to Language Server ({ls_addr}) for session '{tag}'.",
          },
      )
      return

    is_upgrade = (
        "upgrade" in (self.headers.get("Connection") or "").lower()
        and bool(self.headers.get("Upgrade"))
    )
    hdr_lines = [f"{method} {self.path} HTTP/1.1"]
    for k, v in self.headers.items():
      kl = k.lower()
      if kl in ("host", "connection", "x-codeium-csrf-token"):
        continue
      hdr_lines.append(f"{k}: {v}")
    hdr_lines.append(f"Host: {ls_host}:{ls_port}")
    if csrf_token:
      hdr_lines.append(f"x-codeium-csrf-token: {csrf_token}")
    hdr_lines.append("Connection: Upgrade" if is_upgrade else "Connection: close")
    raw_req = ("\r\n".join(hdr_lines) + "\r\n\r\n").encode("utf-8") + body

    try:
      upstream_sock.sendall(raw_req)
      self.close_connection = True
      if is_upgrade:
        sockets = [self.connection, upstream_sock]
        while True:
          rlist, _, _ = select.select(sockets, [], [], 300.0)
          if not rlist:
            break
          closed = False
          for s in rlist:
            chunk = s.recv(65536)
            if not chunk:
              closed = True
              break
            target = upstream_sock if s is self.connection else self.connection
            target.sendall(chunk)
          if closed:
            break
      else:
        while True:
          rlist, _, _ = select.select([upstream_sock], [], [], 300.0)
          if not rlist:
            break
          chunk = upstream_sock.recv(65536)
          if not chunk:
            break
          self.wfile.write(chunk)
          self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
      pass
    finally:
      try:
        upstream_sock.close()
      except OSError:
        pass

  def do_OPTIONS(self) -> None:
    origin = self.headers.get("Origin") or "*"
    req_headers = (
        self.headers.get("Access-Control-Request-Headers")
        or "Content-Type, x-codeium-csrf-token, Connect-Protocol-Version, Connect-Timeout-Ms"
    )
    self.send_response(204)
    self.send_header("Access-Control-Allow-Origin", origin)
    self.send_header("Access-Control-Allow-Credentials", "true")
    self.send_header(
        "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS, HEAD"
    )
    self.send_header("Access-Control-Allow-Headers", req_headers)
    self.send_header("Content-Length", "0")
    self.end_headers()

  def _handle_get_or_head(self, is_head: bool) -> None:
    parsed = urlparse(self.path)
    parsed_path = parsed.path
    tag = self._extract_request_tag()

    if parsed_path.startswith("/proxy/unleash"):
      self._send_json(200, {"toggles": []})
      return

    if (
        self._is_rpc_or_api_path(parsed_path)
        or "upgrade" in (self.headers.get("Connection") or "").lower()
    ):
      self._proxy_to_ls("HEAD" if is_head else "GET")
      return

    bundle_files = self.bridge._load_web_bundle_files() if self.bridge else {}
    rel_path = parsed_path.lstrip("/")

    if rel_path in ("", "index.html"):
      body = self.bridge._render_hub_index(tag) if self.bridge else b""
      ctype = "text/html; charset=utf-8"
    elif rel_path in bundle_files:
      body = bundle_files[rel_path]
      ctype = self._guess_mime_type(rel_path)
    elif "." not in os.path.basename(rel_path):
      body = self.bridge._render_hub_index(tag) if self.bridge else b""
      ctype = "text/html; charset=utf-8"
    else:
      self.send_response(404)
      self.send_header("Content-Length", "0")
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()
      return

    self.send_response(200)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Access-Control-Allow-Origin", "*")
    self.end_headers()
    if not is_head and body:
      self.wfile.write(body)

  def do_GET(self) -> None:
    self._handle_get_or_head(is_head=False)

  def do_HEAD(self) -> None:
    self._handle_get_or_head(is_head=True)

  def do_POST(self) -> None:
    parsed_path = urlparse(self.path).path
    if parsed_path.startswith("/proxy/unleash") or parsed_path == "/clearcut_proxy":
      content_len = int(self.headers.get("Content-Length", 0))
      if content_len > 0:
        self.rfile.read(content_len)
      self._send_json(200, {})
      return
    self._proxy_to_ls("POST")

  def log_message(self, fmt: str, *args: Any) -> None:
    pass


def _render_ui_page(
    config: AxoloctlConfig,
    ui_id: str,
    ui_label: str,
    active_tag: str = "",
    hub_port: int = 5387,
) -> bytes:
  """Renders the interactive HTML page for cliView, hubView, or apiView."""
  escaped_label = html.escape(ui_label)
  escaped_subpath = html.escape(config.app_subpath)
  escaped_entry = html.escape(config.entrypoint)
  mcp_names = ", ".join(sorted(config.mcp_servers.keys()))
  tag_clean = re.sub(r"[^a-zA-Z0-9_-]", "", active_tag or "")

  if tag_clean:
    worktree_path = os.path.normpath(
        os.path.join(
            UDMI_ROOT,
            "var",
            "axoloctl",
            "sessions",
            tag_clean,
            "workspace",
            config.app_subpath,
        )
    )
    escaped_worktree = html.escape(worktree_path)
    escaped_tag = html.escape(tag_clean)
    agent_window_ref = f"udmi_axoloctl_agent:{escaped_tag}"
    branch_ref = f"axoloctl-{escaped_tag}"
    attach_cmd = f"bin/tmux_axoloctl attach {escaped_tag}"

    if ui_id == "cliView":
      body_section = f"""
        <div class="view-shell">
          <div class="toolbar">
            <div class="toolbar-title" title="Worktree: {escaped_worktree} ({branch_ref}) | Attach: {attach_cmd}">
              <code>{agent_window_ref}</code>
              <span class="sr-meta">Session: <code id="bound-tag-label">{escaped_tag}</code> | <code id="bound-worktree-label">{escaped_worktree}</code></span>
            </div>
            <div class="toolbar-actions">
              <span id="cli-status-pill" class="pill">Connecting...</span>
              <button type="button" id="cli-restart-btn" class="key-btn" title="Restart Agent CLI in {agent_window_ref}">Restart</button>
            </div>
          </div>
          <div id="cli-xterm-container" class="xterm-wrapper"></div>
          <pre class="terminal" id="cli-output" style="display:none;">[axoloctl:{escaped_tag}] Attached to {agent_window_ref}</pre>
          <form id="cli-form" class="chat-form" style="display:none;">
            <input type="text" id="cli-input" placeholder="Send prompt or command to {agent_window_ref}..." autocomplete="off" />
            <button type="submit">Send</button>
          </form>
          <div class="key-bar">
            <button type="button" class="key-btn" data-key="Enter">Enter</button>
            <button type="button" class="key-btn" data-key="C-c">Ctrl+C</button>
            <button type="button" class="key-btn" data-key="Escape">Esc</button>
            <button type="button" class="key-btn" data-key="Up">↑</button>
            <button type="button" class="key-btn" data-key="Down">↓</button>
            <button type="button" class="key-btn" data-key="Tab">Tab</button>
          </div>
        </div>
      """
    elif ui_id == "hubView":
      hub_default_url = f"http://localhost:{hub_port}/?tag={escaped_tag}&hostTheme=dark"
      body_section = f"""
        <div class="view-shell">
          <div class="toolbar">
            <div class="toolbar-title" title="Worktree: {escaped_worktree} ({branch_ref}) | MCP: {html.escape(mcp_names)}">
              <code>{agent_window_ref}</code>
              <span class="sr-meta"><code id="bound-tag-label">{escaped_tag}</code> <code id="bound-worktree-label">{escaped_worktree}</code></span>
            </div>
            <div class="toolbar-actions">
              <span id="hub-status-pill" class="pill ok">● Hub Listening (:{hub_port})</span>
              <button type="button" id="hub-start-btn" class="action-btn">Reload Hub</button>
              <a id="hub-open-link" href="{hub_default_url}" target="_blank" class="key-btn" title="Open Hub in New Tab">↗</a>
            </div>
          </div>
          <div id="hub-error-box" class="error-banner" style="display:none;"></div>
          <iframe id="hub-iframe" class="hub-frame" src="{hub_default_url}" style="display:block;"></iframe>
        </div>
      """
    else:
      body_section = f"""
        <div class="view-shell">
          <div class="toolbar">
            <div class="toolbar-title" title="Branch: {branch_ref} | Worktree: {escaped_worktree}">
              <code>{agent_window_ref}</code>
              <code id="chat-conv-id" style="color:#94a3b8;">...</code>
              <span class="sr-meta"><code id="bound-tag-label">{escaped_tag}</code></span>
            </div>
            <div class="toolbar-actions">
              <span id="chat-status-pill" class="pill">● Ready</span>
              <button type="button" id="chat-reset-btn" class="key-btn" title="Start a fresh conversation">New Chat</button>
            </div>
          </div>
          <div id="chat-error-box" class="error-banner" style="display:none;"></div>
          <div id="chat-log" class="chat-log">
            <div class="msg agent">Agent [<code>{escaped_tag}</code>] bound to worktree <code>{branch_ref}</code>.</div>
          </div>
          <form id="chat-form" class="chat-form">
            <input type="text" id="chat-input" placeholder="Ask agent [{escaped_tag}] to inspect or modify {escaped_subpath}..." autocomplete="off" required />
            <button type="submit" id="chat-send-btn">Send</button>
          </form>
        </div>
      """
  else:
    if ui_id == "cliView":
      body_section = """
        <div class="view-shell">
          <div class="toolbar">
            <div class="toolbar-title">CLI Console — <code id="bound-tag-label">None</code></div>
            <span id="cli-status-pill" class="pill">○ Uncorrelated</span>
          </div>
          <pre class="terminal" id="cli-output" style="color:#475569;">No correlated viewer in active tab.

Switch to an active session tab (e.g. http://&lt;tag&gt;.localhost:9290) to attach its dedicated agent CLI console.</pre>
          <form id="cli-form" class="chat-form" style="display:none;">
            <input type="text" id="cli-input" placeholder="No correlated viewer in active tab" disabled />
            <button type="submit" disabled>Send</button>
          </form>
        </div>
      """
    elif ui_id == "hubView":
      body_section = """
        <div class="view-shell">
          <div class="toolbar">
            <div class="toolbar-title">Web Hub — <code id="bound-tag-label">None</code></div>
            <span id="hub-status-pill" class="pill">○ Uncorrelated</span>
          </div>
          <div style="padding:8px;flex:1;">
            <div class="msg agent" style="border-style:dashed;color:#94a3b8;"><strong>No correlated viewer in active tab.</strong><br>Switch to an active session tab (e.g. <code>http://&lt;tag&gt;.localhost:9290</code>) to view its Web Hub.</div>
          </div>
        </div>
      """
    else:
      body_section = """
        <div class="view-shell">
          <div class="toolbar">
            <div class="toolbar-title">Agent Chat — <code id="bound-tag-label">None</code></div>
            <div class="toolbar-actions">
              <span id="chat-status-pill" class="pill">○ Uncorrelated</span>
              <button type="button" id="chat-reset-btn" class="key-btn" disabled>New Chat</button>
            </div>
          </div>
          <div id="chat-log" class="chat-log">
            <div class="msg agent" style="border-style:dashed;color:#94a3b8;"><strong>No correlated viewer in active tab.</strong><br>Switch to an active session tab (e.g. <code>http://&lt;tag&gt;.localhost:9290</code>) to interact with its Agent Chat.</div>
          </div>
          <form id="chat-form" class="chat-form">
            <input type="text" id="chat-input" placeholder="No correlated viewer in active tab" autocomplete="off" disabled />
            <button type="submit" id="chat-send-btn" disabled>Send</button>
          </form>
        </div>
      """

  xterm_head = (
      """
  <link rel="stylesheet" href="/vendor/xterm/xterm.css" />
  <script src="/vendor/xterm/xterm.js"></script>
  <script src="/vendor/xterm/xterm-addon-fit.js"></script>
"""
      if ui_id == "cliView"
      else ""
  )
  body_class_attr = ' class="theme-light"' if ui_id == "cliView" else ""

  page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Axoloctl — {escaped_label}</title>
  <link rel="icon" type="image/svg+xml" href="/ui/axoloctl.svg" />{xterm_head}
  <style>
    *, *::before, *::after {{
      box-sizing: border-box;
    }}
    html, body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      background: #0f172a;
      color: #e2e8f0;
    }}
    .sr-meta {{
      display: none;
    }}
    .view-shell {{
      flex: 1;
      min-height: 0;
      width: 100%;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}
    .toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      background: #0f172a;
      border-bottom: 1px solid #1e293b;
      flex-shrink: 0;
      min-width: 0;
    }}
    .toolbar-title {{
      display: flex;
      align-items: center;
      gap: 4px;
      font-size: 11px;
      font-weight: 600;
      color: #cbd5e1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .toolbar-actions {{
      display: flex;
      align-items: center;
      gap: 4px;
      flex-shrink: 0;
      min-width: 0;
    }}
    .pill {{
      font-size: 10px;
      padding: 2px 6px;
      border-radius: 999px;
      background: #020617;
      border: 1px solid #334155;
      color: #38bdf8;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 165px;
    }}
    .pill.busy {{
      color: #fbbf24;
      border-color: #d97706;
    }}
    .pill.ok {{
      color: #4ade80;
      border-color: #16a34a;
    }}
    code {{
      background: #1e293b;
      padding: 1px 4px;
      border-radius: 3px;
      color: #7dd3fc;
      font-size: 11px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .terminal {{
      flex: 1;
      min-height: 0;
      width: 100%;
      background: #f8fafc;
      color: #0f172a;
      padding: 8px;
      font-size: 11px;
      line-height: 1.35;
      overflow-y: auto;
      overflow-x: hidden;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 0;
    }}
    .xterm-wrapper {{
      flex: 1;
      min-height: 0;
      width: 100%;
      background: #f8fafc;
      padding: 2px 4px;
      margin: 0;
      overflow: hidden;
    }}
    .xterm-wrapper .xterm-viewport {{
      overflow: hidden !important;
      scrollbar-width: none !important;
      background-color: #f8fafc !important;
    }}
    .xterm-wrapper .xterm-viewport::-webkit-scrollbar {{
      display: none !important;
      width: 0 !important;
      height: 0 !important;
    }}
    body.theme-light {{
      background: #f8fafc;
      color: #0f172a;
    }}
    body.theme-light .toolbar {{
      background: #f1f5f9;
      border-bottom: 1px solid #cbd5e1;
    }}
    body.theme-light .toolbar-title {{
      color: #1e293b;
    }}
    body.theme-light code {{
      background: #e2e8f0;
      color: #0369a1;
    }}
    body.theme-light .pill {{
      background: #ffffff;
      border-color: #cbd5e1;
      color: #0284c7;
    }}
    body.theme-light .pill.ok {{
      background: #dcfce7;
      border-color: #86efac;
      color: #15803d;
    }}
    body.theme-light .key-bar {{
      background: #f1f5f9;
      border-top: 1px solid #cbd5e1;
    }}
    body.theme-light .key-btn {{
      background: #ffffff;
      color: #334155;
      border-color: #cbd5e1;
    }}
    body.theme-light .key-btn:hover {{
      background: #e2e8f0;
      filter: none;
    }}
    body.theme-light .sessions-footer {{
      background: #f1f5f9;
      border-top: 1px solid #cbd5e1;
      color: #475569;
    }}
    body.theme-light a {{
      color: #0284c7;
    }}
    .key-bar {{
      display: flex;
      gap: 4px;
      padding: 4px 8px;
      background: #0f172a;
      border-top: 1px solid #1e293b;
      flex-wrap: wrap;
      flex-shrink: 0;
    }}
    .key-btn, .action-btn {{
      background: #1e293b;
      color: #cbd5e1;
      border: 1px solid #475569;
      padding: 2px 7px;
      border-radius: 4px;
      font-size: 11px;
      cursor: pointer;
      white-space: nowrap;
    }}
    .action-btn {{
      background: #0284c7;
      color: #ffffff;
      border-color: #0369a1;
    }}
    .key-btn:hover, .action-btn:hover {{
      filter: brightness(1.15);
    }}
    .hub-frame {{
      flex: 1;
      min-height: 0;
      width: 100%;
      border: none;
      background: #020617;
    }}
    .error-banner {{
      background: #450a0a;
      border-bottom: 1px solid #dc2626;
      color: #fca5a5;
      padding: 5px 8px;
      font-size: 11px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      flex-shrink: 0;
    }}
    .sessions-footer {{
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 3px 8px;
      background: #090d16;
      border-top: 1px solid #1e293b;
      font-size: 10px;
      color: #64748b;
      flex-shrink: 0;
      min-width: 0;
      overflow: hidden;
    }}
    #sessions-list {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex: 1;
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
    }}
    .session-item {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 10px;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    a {{ color: #38bdf8; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .chat-log {{
      flex: 1;
      min-height: 0;
      width: 100%;
      background: #020617;
      overflow-y: auto;
      overflow-x: hidden;
      padding: 8px;
      margin: 0;
      font-size: 12px;
    }}
    .msg {{
      margin-bottom: 6px;
      padding: 6px 8px;
      border-radius: 6px;
      line-height: 1.4;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      word-break: break-word;
      max-width: 100%;
    }}
    .msg.agent {{
      background: #1e293b;
      color: #e2e8f0;
      border: 1px solid #334155;
    }}
    .msg.user {{
      background: #0369a1;
      color: #ffffff;
      margin-left: 12%;
    }}
    .chat-form {{
      display: flex;
      gap: 6px;
      padding: 6px 8px;
      background: #0f172a;
      border-top: 1px solid #1e293b;
      flex-shrink: 0;
      min-width: 0;
    }}
    .chat-form input {{
      flex: 1;
      min-width: 0;
      background: #020617;
      border: 1px solid #475569;
      color: #f8fafc;
      padding: 5px 8px;
      border-radius: 4px;
      font-size: 12px;
    }}
    .chat-form button {{
      background: #0284c7;
      color: white;
      border: none;
      padding: 5px 10px;
      border-radius: 4px;
      cursor: pointer;
      font-size: 12px;
      flex-shrink: 0;
    }}
    .chat-form button:disabled {{
      opacity: 0.5;
      cursor: not-allowed;
    }}
    @keyframes axo-spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .spinner {{
      display: inline-block;
      width: 11px;
      height: 11px;
      border: 2px solid rgba(251, 191, 36, 0.3);
      border-top-color: #fbbf24;
      border-radius: 50%;
      animation: axo-spin 0.8s linear infinite;
      flex-shrink: 0;
    }}
    .agent-activity {{
      background: #0f172a;
      border: 1px dashed #b45309;
      border-left: 3px solid #fbbf24;
      border-radius: 6px;
      padding: 6px 8px;
      margin-bottom: 6px;
      font-size: 11px;
      color: #e2e8f0;
      max-width: 100%;
      overflow: hidden;
    }}
    .activity-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 6px;
      color: #fbbf24;
      font-weight: 600;
      min-width: 0;
    }}
    .activity-title {{
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .activity-elapsed {{
      color: #94a3b8;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 400;
      font-size: 10px;
      white-space: nowrap;
      flex-shrink: 0;
    }}
    .activity-thinking {{
      color: #94a3b8;
      font-style: italic;
      margin-top: 4px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .activity-steps {{
      margin-top: 4px;
      padding-top: 4px;
      border-top: 1px solid #1e293b;
      color: #cbd5e1;
      font-size: 10px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }}
  </style>
</head>
<body{body_class_attr}>
  {body_section}
  <div class="sessions-footer">
    <span>Sessions:</span>
    <div id="sessions-list">Loading...</div>
  </div>
  <script>
    const UI_ID = {json.dumps(ui_id)};
    const currentTag = new URLSearchParams(window.location.search).get("tag") || "";

    async function refreshSessions() {{
      const container = document.getElementById("sessions-list");
      try {{
        const resp = await fetch("/api/status");
        const data = await resp.json();
        const entries = Object.entries(data.sessions || {{}});
        if (entries.length === 0) {{
          container.innerHTML = "<em>None</em>";
          return;
        }}
        const portPart = window.location.port ? `:${{window.location.port}}` : "";
        container.innerHTML = entries.map(([tag, info]) => {{
          const isCorrelated = currentTag && currentTag === tag;
          const badge = isCorrelated
            ? `<span style="background:#065f46;color:#6ee7b7;border:1px solid #059669;padding:0 4px;border-radius:3px;font-size:9px;">ACTIVE</span>`
            : "";
          const vhostUrl = `${{window.location.protocol}}//${{tag}}.localhost${{portPart}}`;
          const uiSwitchUrl = `${{window.location.pathname}}?tag=${{encodeURIComponent(tag)}}`;
          return `<span class="session-item">
            <a href="${{uiSwitchUrl}}"><strong>${{tag}}</strong></a>
            <code>${{info.commit.slice(0, 7)}}</code>${{badge}}
            <a href="${{vhostUrl}}" target="_blank" title="Open ${{vhostUrl}}">↗</a>
          </span>`;
        }}).join(" · ");
      }} catch (e) {{
        container.innerHTML = "<em>Offline</em>";
      }}
    }}

    // -------------------------------------------------------------------------
    // Technique 1: cliView interactive xterm.js terminal & WebSocket tmux bridge
    // -------------------------------------------------------------------------
    let cliTerm = null;
    let cliFitAddon = null;
    let cliOffset = 0;
    let cliWs = null;
    let cliReconnectTimer = null;
    let cliPingTimer = null;
    let cliPendingFrames = [];
    let lastCols = 0;
    let lastRows = 0;
    const cliTextEncoder = new TextEncoder();

    function stripEraseScrollbackBytes(bytes) {{
      if (!(bytes instanceof Uint8Array)) return bytes;
      const out = [];
      for (let i = 0; i < bytes.length; i++) {{
        if (
          i + 3 < bytes.length &&
          bytes[i] === 0x1b &&
          bytes[i + 1] === 0x5b &&
          bytes[i + 2] === 0x33 &&
          bytes[i + 3] === 0x4a
        ) {{
          i += 3;
          continue;
        }}
        out.push(bytes[i]);
      }}
      return new Uint8Array(out);
    }}

    function sendCliWsFrame(payloadObj) {{
      if (!currentTag) return;
      const raw = JSON.stringify(payloadObj);
      if (cliWs && cliWs.readyState === WebSocket.OPEN) {{
        cliWs.send(raw);
      }} else {{
        cliPendingFrames.push(raw);
        if (!cliWs || cliWs.readyState === WebSocket.CLOSED || cliWs.readyState === WebSocket.CLOSING) {{
          connectCliWebSocket();
        }}
      }}
    }}

    function syncCliSize() {{
      if (!currentTag || !cliTerm || !cliFitAddon) return;
      try {{
        cliFitAddon.fit();
        const cols = cliTerm.cols;
        const rows = cliTerm.rows;
        if (cols && rows && (cols !== lastCols || rows !== lastRows)) {{
          lastCols = cols;
          lastRows = rows;
          if (cliWs && cliWs.readyState === WebSocket.OPEN) {{
            cliWs.send(JSON.stringify({{ type: "resize", tag: currentTag, cols, rows }}));
          }}
        }}
      }} catch (e) {{}}
    }}

    function handleCliWsMessage(msg) {{
      if (!msg) return;
      const pill = document.getElementById("cli-status-pill");
      if (msg.type === "status") {{
        if (pill) {{
          pill.textContent = msg.running ? "● Live" : "○ Stopped";
          pill.title = msg.window || "";
          pill.className = msg.running ? "pill ok" : "pill";
        }}
      }} else if (msg.type === "cleared") {{
        if (cliTerm) {{
          cliTerm.reset();
        }}
        cliOffset = msg.offset || 0;
      }} else if (msg.type === "snapshot" || msg.type === "output") {{
        if (cliTerm && msg.data) {{
          if (msg.type === "snapshot") {{
            cliTerm.clear();
          }}
          const binStr = window.atob(msg.data);
          const bytes = new Uint8Array(binStr.length);
          for (let i = 0; i < binStr.length; i++) {{
            bytes[i] = binStr.charCodeAt(i);
          }}
          cliTerm.write(stripEraseScrollbackBytes(bytes));
        }}
        if (typeof msg.offset === "number") {{
          cliOffset = msg.offset;
        }}
      }}
    }}

    function connectCliWebSocket() {{
      if (!currentTag) return;
      if (cliWs && (cliWs.readyState === WebSocket.OPEN || cliWs.readyState === WebSocket.CONNECTING)) {{
        return;
      }}
      if (cliReconnectTimer) {{
        clearTimeout(cliReconnectTimer);
        cliReconnectTimer = null;
      }}
      const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = `${{wsProto}}//${{window.location.host}}/ws?tag=${{encodeURIComponent(currentTag)}}`;
      const ws = new WebSocket(wsUrl);
      cliWs = ws;

      ws.onopen = () => {{
        if (cliTerm && cliFitAddon) {{
          try {{
            cliFitAddon.fit();
            lastCols = cliTerm.cols;
            lastRows = cliTerm.rows;
          }} catch (e) {{}}
        }}
        ws.send(JSON.stringify({{
          type: "attach",
          tag: currentTag,
          cols: lastCols || 80,
          rows: lastRows || 24,
          offset: cliOffset,
        }}));
        while (cliPendingFrames.length > 0 && ws.readyState === WebSocket.OPEN) {{
          ws.send(cliPendingFrames.shift());
        }}
        if (cliPingTimer) clearInterval(cliPingTimer);
        cliPingTimer = setInterval(() => {{
          if (cliWs && cliWs.readyState === WebSocket.OPEN) {{
            cliWs.send(JSON.stringify({{ type: "ping" }}));
          }}
        }}, 15000);
      }};

      ws.onmessage = (event) => {{
        try {{
          const msg = JSON.parse(event.data);
          handleCliWsMessage(msg);
        }} catch (e) {{}}
      }};

      ws.onclose = () => {{
        if (cliPingTimer) {{
          clearInterval(cliPingTimer);
          cliPingTimer = null;
        }}
        const pill = document.getElementById("cli-status-pill");
        if (pill) {{
          pill.textContent = "Reconnecting...";
          pill.className = "pill";
        }}
        cliReconnectTimer = setTimeout(connectCliWebSocket, 1000);
      }};

      ws.onerror = () => {{}};
    }}

    function sendCliPayload(payload) {{
      if (!currentTag) return;
      sendCliWsFrame({{ type: "input", tag: currentTag, ...payload }});
      if (cliTerm) cliTerm.focus();
    }}

    if (UI_ID === "cliView" && currentTag) {{
      const xtermContainer = document.getElementById("cli-xterm-container");
      if (xtermContainer && window.Terminal && window.FitAddon) {{
        const extendedAnsi = [];
        const cubeLevels = [0, 95, 135, 175, 215, 255];
        for (let r = 0; r < 6; r++) {{
          for (let g = 0; g < 6; g++) {{
            for (let b = 0; b < 6; b++) {{
              extendedAnsi.push(
                `#${{cubeLevels[r].toString(16).padStart(2, "0")}}${{cubeLevels[g].toString(16).padStart(2, "0")}}${{cubeLevels[b].toString(16).padStart(2, "0")}}`
              );
            }}
          }}
        }}
        for (let i = 0; i < 24; i++) {{
          const inv = 23 - i;
          const v = Math.round(15 + (inv / 23) * 230);
          const hex = v.toString(16).padStart(2, "0");
          extendedAnsi.push(`#${{hex}}${{hex}}${{hex}}`);
        }}
        const lightOverrides = {{
          39: "#0284c7",
          109: "#0f766e",
          111: "#0369a1",
          114: "#15803d",
          146: "#475569",
          189: "#1e293b",
          221: "#b45309",
          236: "#e2e8f0",
          238: "#cbd5e1",
          240: "#94a3b8",
        }};
        for (const [idx, hex] of Object.entries(lightOverrides)) {{
          extendedAnsi[Number(idx) - 16] = hex;
        }}

        cliTerm = new window.Terminal({{
          cursorBlink: true,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Courier New", monospace',
          fontSize: 12,
          minimumContrastRatio: 4.5,
          theme: {{
            background: "#f8fafc",
            foreground: "#0f172a",
            cursor: "#0284c7",
            cursorAccent: "#f8fafc",
            selectionBackground: "rgba(2, 132, 199, 0.22)",
            selectionForeground: "#0f172a",
            black: "#0f172a",
            red: "#dc2626",
            green: "#16a34a",
            yellow: "#b45309",
            blue: "#0284c7",
            magenta: "#9333ea",
            cyan: "#0891b2",
            white: "#475569",
            brightBlack: "#64748b",
            brightRed: "#ef4444",
            brightGreen: "#22c55e",
            brightYellow: "#d97706",
            brightBlue: "#2563eb",
            brightMagenta: "#a855f7",
            brightCyan: "#06b6d4",
            brightWhite: "#0f172a",
            extendedAnsi,
          }},
          convertEol: false,
          scrollback: 0,
        }});
        cliFitAddon = new window.FitAddon.FitAddon();
        cliTerm.loadAddon(cliFitAddon);
        cliTerm.open(xtermContainer);

        cliTerm.onData((data) => {{
          const bytes = cliTextEncoder.encode(data);
          const hexKeys = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));
          sendCliWsFrame({{ type: "input", tag: currentTag, hexKeys }});
        }});

        let resizeTimer = null;
        const ro = new ResizeObserver(() => {{
          if (resizeTimer) clearTimeout(resizeTimer);
          resizeTimer = setTimeout(syncCliSize, 100);
        }});
        ro.observe(xtermContainer);

        connectCliWebSocket();
        cliTerm.focus();
      }}

      const cliForm = document.getElementById("cli-form");
      if (cliForm) {{
        cliForm.addEventListener("submit", (ev) => {{
          ev.preventDefault();
          const input = document.getElementById("cli-input");
          const text = input.value;
          input.value = "";
          sendCliPayload({{ text, submit: true }});
        }});
      }}
      const restartBtn = document.getElementById("cli-restart-btn");
      if (restartBtn) {{
        restartBtn.addEventListener("click", () => {{
          if (cliTerm) cliTerm.reset();
          cliOffset = 0;
          sendCliPayload({{ action: "restart" }});
        }});
      }}
      document.querySelectorAll(".key-btn[data-key]").forEach((btn) => {{
        btn.addEventListener("click", () => {{
          sendCliPayload({{ key: btn.getAttribute("data-key") }});
        }});
      }});
    }}

    // -------------------------------------------------------------------------
    // Technique 2: hubView status & launch
    // -------------------------------------------------------------------------
    async function pollHubStatus() {{
      if (!currentTag) return;
      const pill = document.getElementById("hub-status-pill");
      const errBox = document.getElementById("hub-error-box");
      const iframe = document.getElementById("hub-iframe");
      const openLink = document.getElementById("hub-open-link");
      if (!pill) return;
      try {{
        const qs = `?tag=${{encodeURIComponent(currentTag)}}`;
        const resp = await fetch(`/api/hub/status${{qs}}`);
        const data = await resp.json();
        if (openLink && data.hub_url) openLink.href = data.hub_url;
        if (data.running) {{
          pill.textContent = `● Hub Listening (:${{data.hub_port}})`;
          pill.className = "pill ok";
          if (errBox) errBox.style.display = "none";
          if (iframe && iframe.getAttribute("src") !== data.hub_url) {{
            iframe.setAttribute("src", data.hub_url);
            iframe.style.display = "block";
          }}
        }} else {{
          pill.textContent = `○ Hub Offline (:${{data.hub_port}})`;
          pill.className = "pill";
          if (errBox && data.last_error) {{
            errBox.textContent = data.last_error;
            errBox.style.display = "block";
          }}
        }}
      }} catch (e) {{}}
    }}

    if (UI_ID === "hubView" && currentTag) {{
      const startBtn = document.getElementById("hub-start-btn");
      if (startBtn) {{
        startBtn.addEventListener("click", async () => {{
          startBtn.disabled = true;
          startBtn.textContent = "Reloading...";
          try {{
            await fetch("/api/hub/start", {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{ tag: currentTag }}),
            }});
            const iframe = document.getElementById("hub-iframe");
            if (iframe) iframe.setAttribute("src", "about:blank");
            await pollHubStatus();
          }} finally {{
            startBtn.disabled = false;
            startBtn.textContent = "Reload Hub";
          }}
        }});
      }}
      pollHubStatus();
      setInterval(pollHubStatus, 3000);
    }}

    // -------------------------------------------------------------------------
    // Technique 3: apiView structured chat via jetski agentapi & transcript
    // -------------------------------------------------------------------------
    let renderedSteps = new Set();
    let isSubmittingChat = false;
    let localSendStartedAt = 0;

    function renderAgentActivity(log, st) {{
      let card = document.getElementById("agent-activity-card");
      const active = isSubmittingChat || (st && st.is_busy);
      if (!active) {{
        if (card) card.remove();
        return;
      }}
      const nearBottom = (log.scrollHeight - log.scrollTop - log.clientHeight) < 60;
      if (!card) {{
        card = document.createElement("div");
        card.id = "agent-activity-card";
        card.className = "agent-activity";
      }}
      const elapsed = (st && typeof st.elapsed_s === "number" && st.elapsed_s > 0)
        ? st.elapsed_s
        : (localSendStartedAt ? Math.max(0, Math.floor((Date.now() - localSendStartedAt) / 1000)) : 0);
      const stepBadge = (st && st.step_count > 0) ? ` · step ${{st.step_count}}` : "";
      const label = isSubmittingChat
        ? "Sending prompt to remote agent..."
        : ((st && st.active_tool) ? st.active_tool : "Agent is processing message...");

      let htmlParts = `
        <div class="activity-header">
          <span class="activity-title"><span class="spinner"></span> <span class="activity-label"></span></span>
          <span class="activity-elapsed">${{elapsed}}s${{stepBadge}}</span>
        </div>
      `;
      if (st && st.thinking) {{
        htmlParts += `<div class="activity-thinking"></div>`;
      }}
      const recent = (st && Array.isArray(st.recent_actions)) ? st.recent_actions : [];
      if (recent.length > 0) {{
        htmlParts += `<div class="activity-steps"></div>`;
      }}
      card.innerHTML = htmlParts;
      card.querySelector(".activity-label").textContent = label;
      if (st && st.thinking) {{
        card.querySelector(".activity-thinking").textContent = `💭 ${{st.thinking}}`;
      }}
      if (recent.length > 0) {{
        const stepsEl = card.querySelector(".activity-steps");
        stepsEl.textContent = recent.map((act, idx) =>
          idx === recent.length - 1 ? `⟳ ${{act}}` : `✓ ${{act}}`
        ).join("  →  ");
      }}
      if (card.parentElement !== log || log.lastElementChild !== card) {{
        log.appendChild(card);
      }}
      if (nearBottom) {{
        log.scrollTop = log.scrollHeight;
      }}
    }}

    async function pollChatMessages() {{
      if (!currentTag) return;
      const log = document.getElementById("chat-log");
      const pill = document.getElementById("chat-status-pill");
      const convEl = document.getElementById("chat-conv-id");
      const input = document.getElementById("chat-input");
      if (!log) return;
      try {{
        const qs = `?tag=${{encodeURIComponent(currentTag)}}`;
        const resp = await fetch(`/api/chat/messages${{qs}}`);
        const data = await resp.json();
        if (convEl && data.conversation_id) {{
          convEl.textContent = data.conversation_id.slice(0, 8);
          convEl.title = data.conversation_id;
        }}
        const st = data.agent_state || {{}};
        const busy = isSubmittingChat || Boolean(st.is_busy);
        if (pill) {{
          const elapsedStr = (typeof st.elapsed_s === "number" && st.elapsed_s > 0) ? ` (${{st.elapsed_s}}s)` : "";
          if (isSubmittingChat) {{
            pill.textContent = "⏳ Sending to Agent...";
            pill.className = "pill busy";
          }} else if (st.status === "executing_tool") {{
            const shortTool = (st.active_tool || "tool").slice(0, 32);
            pill.textContent = `⚡ ${{shortTool}}${{elapsedStr}}`;
            pill.className = "pill busy";
          }} else if (st.is_busy) {{
            pill.textContent = `⏳ Thinking${{elapsedStr}}`;
            pill.className = "pill busy";
          }} else {{
            pill.textContent = "● Ready";
            pill.className = "pill ok";
            localSendStartedAt = 0;
          }}
        }}
        if (input) {{
          input.placeholder = busy
            ? `Agent [${{currentTag}}] is working... (type follow-up)`
            : `Ask agent [${{currentTag}}] to inspect or modify...`;
        }}
        const msgs = data.messages || [];
        let added = false;
        for (const m of msgs) {{
          const key = `${{m.step_index}}:${{m.role}}`;
          if (renderedSteps.has(key)) continue;
          renderedSteps.add(key);
          if (m.role === "user") {{
            const pending = log.querySelector(".msg.user.optimistic");
            if (pending) pending.remove();
          }}
          const div = document.createElement("div");
          div.className = `msg ${{m.role === "user" ? "user" : "agent"}}`;
          div.textContent = m.content;
          log.appendChild(div);
          added = true;
        }}
        if (st.pending_prompt && !log.querySelector(".msg.user.optimistic")) {{
          const userDiv = document.createElement("div");
          userDiv.className = "msg user optimistic";
          userDiv.textContent = st.pending_prompt;
          log.appendChild(userDiv);
          added = true;
        }}
        renderAgentActivity(log, st);
        if (added) {{
          log.scrollTop = log.scrollHeight;
        }}
      }} catch (e) {{}}
    }}

    if (UI_ID === "apiView" && currentTag) {{
      const chatForm = document.getElementById("chat-form");
      const resetBtn = document.getElementById("chat-reset-btn");
      const errBox = document.getElementById("chat-error-box");

      if (chatForm) {{
        chatForm.addEventListener("submit", async (ev) => {{
          ev.preventDefault();
          const input = document.getElementById("chat-input");
          const sendBtn = document.getElementById("chat-send-btn");
          const log = document.getElementById("chat-log");
          const pill = document.getElementById("chat-status-pill");
          const text = input.value.trim();
          if (!text) return;

          if (errBox) errBox.style.display = "none";
          const userDiv = document.createElement("div");
          userDiv.className = "msg user optimistic";
          userDiv.textContent = text;
          log.appendChild(userDiv);
          input.value = "";

          isSubmittingChat = true;
          localSendStartedAt = Date.now();
          if (sendBtn) sendBtn.disabled = true;
          if (pill) {{
            pill.textContent = "⏳ Sending to Agent...";
            pill.className = "pill busy";
          }}
          renderAgentActivity(log, {{ is_busy: true, status: "thinking", active_tool: "Sending prompt to remote agent..." }});
          log.scrollTop = log.scrollHeight;

          try {{
            const resp = await fetch("/api/chat", {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{ tag: currentTag, prompt: text }}),
            }});
            const resData = await resp.json();
            isSubmittingChat = false;
            if (!resp.ok || resData.error) {{
              if (errBox) {{
                errBox.textContent = resData.error || `HTTP ${{resp.status}}`;
                errBox.style.display = "block";
              }}
              renderAgentActivity(log, {{ is_busy: false }});
            }} else {{
              await pollChatMessages();
            }}
          }} catch (err) {{
            isSubmittingChat = false;
            if (errBox) {{
              errBox.textContent = String(err);
              errBox.style.display = "block";
            }}
            renderAgentActivity(log, {{ is_busy: false }});
          }} finally {{
            isSubmittingChat = false;
            if (sendBtn) sendBtn.disabled = false;
          }}
        }});
      }}

      if (resetBtn) {{
        resetBtn.addEventListener("click", async () => {{
          await fetch("/api/chat/new", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ tag: currentTag }}),
          }});
          renderedSteps.clear();
          localSendStartedAt = 0;
          const log = document.getElementById("chat-log");
          if (log) {{
            log.innerHTML = `<div class="msg agent">Started new conversation for session <code>${{currentTag}}</code>.</div>`;
          }}
          await pollChatMessages();
        }});
      }}

      pollChatMessages();
      setInterval(pollChatMessages, 1000);
    }}

    refreshSessions();
    setInterval(refreshSessions, 3000);
  </script>
</body>
</html>
"""
  return page_html.encode("utf-8")


class UIHostHandler(BaseHTTPRequestHandler):
  """HTTP request handler for Virtual-Host Session Routing, UI Discovery, Host UIs, and MCP Proxy."""

  protocol_version = "HTTP/1.1"
  config: Optional[AxoloctlConfig] = None
  sessions_dir: str = os.path.join(UDMI_ROOT, "var", "axoloctl", "sessions")
  agent_bridge: Optional[JetskiAgentBridge] = None

  @classmethod
  def get_bridge(cls) -> JetskiAgentBridge:
    if (
        cls.agent_bridge is None
        or cls.agent_bridge.sessions_dir != cls.sessions_dir
        or cls.agent_bridge.config != cls.config
    ):
      cls.agent_bridge = JetskiAgentBridge(
          UDMI_ROOT, cls.config, cls.sessions_dir
      )
    return cls.agent_bridge

  def _load_session_ports(self) -> Dict[str, int]:
    ports_file = os.path.join(self.sessions_dir, "ports.json")
    if os.path.exists(ports_file):
      try:
        with open(ports_file, "r", encoding="utf-8") as f:
          data = json.load(f)
          if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items()}
      except Exception:
        pass
    return {}

  def _extract_vhost_tag(self) -> Optional[str]:
    """Extracts the session tag from a virtual-host Host header (e.g. <tag>.localhost:9290)."""
    host_hdr = (self.headers.get("Host") or "").strip()
    if not host_hdr or host_hdr.startswith("["):
      return None
    hostname = host_hdr.split(":", 1)[0].strip().lower()
    if not hostname or hostname in ("localhost", "127.0.0.1"):
      return None
    if hostname.endswith(".localhost"):
      prefix = hostname[: -len(".localhost")]
      tag = prefix.split(".", 1)[0].strip()
      return tag if tag else None
    if "." in hostname and not re.match(r"^\d+\.\d+\.\d+\.\d+$", hostname):
      first_label = hostname.split(".", 1)[0].strip()
      if first_label and first_label in self._load_session_ports():
        return first_label
    return None

  def _proxy_to_session(
      self, tag: str, method: str, is_head: bool = False
  ) -> None:
    """Reverse-proxies a virtual-host request (<tag>.localhost) to the session's internal port."""
    ports = self._load_session_ports()
    if tag not in ports:
      self._send_json(
          404, {"error": f"Unknown session tag '{tag}' for virtual host."}
      )
      return

    session_port = ports[tag]
    content_len = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_len) if content_len > 0 else None

    forward_headers = {}
    for k, v in self.headers.items():
      if k.lower() not in HOP_BY_HOP_HEADERS:
        forward_headers[k] = v
    forward_headers["Connection"] = "close"

    conn: Optional[http.client.HTTPConnection] = None
    try:
      upstream = None
      last_err: Optional[Exception] = None
      for attempt in range(6):
        conn = http.client.HTTPConnection("127.0.0.1", session_port, timeout=60)
        try:
          conn.request(method, self.path, body=body, headers=forward_headers)
          upstream = conn.getresponse()
          break
        except ConnectionRefusedError as e:
          last_err = e
          conn.close()
          conn = None
          if attempt < 5:
            time.sleep(0.25)
      if upstream is None:
        raise last_err or ConnectionRefusedError(
            f"Connection refused on port {session_port}"
        )

      content_type = (upstream.getheader("Content-Type") or "").lower()
      is_sse = "text/event-stream" in content_type

      self.send_response(upstream.status, upstream.reason)
      has_content_length = False
      for k, v in upstream.getheaders():
        kl = k.lower()
        if kl in HOP_BY_HOP_HEADERS or kl in ("server", "date"):
          continue
        if kl == "content-length":
          has_content_length = True
        self.send_header(k, v)

      if is_sse:
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        if not is_head:
          while True:
            chunk = upstream.read(1024)
            if not chunk:
              break
            self.wfile.write(chunk)
            self.wfile.flush()
      else:
        data = b"" if is_head else upstream.read()
        if not has_content_length and not is_head:
          self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if not is_head and data:
          self.wfile.write(data)
        self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
      pass
    except Exception as e:
      self._send_json(
          502,
          {
              "error": (
                  f"Session '{tag}' is not currently running or unreachable on "
                  f"internal port {session_port}: {e}"
              )
          },
      )
    finally:
      if conn is not None:
        conn.close()

  def do_OPTIONS(self) -> None:
    vhost_tag = self._extract_vhost_tag()
    if vhost_tag is not None:
      self._proxy_to_session(vhost_tag, "OPTIONS")
      return
    self.send_response(204)
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    self.send_header("Access-Control-Allow-Headers", "Content-Type")
    self.send_header("Content-Length", "0")
    self.end_headers()

  def do_PUT(self) -> None:
    vhost_tag = self._extract_vhost_tag()
    if vhost_tag is not None:
      self._proxy_to_session(vhost_tag, "PUT")
      return
    self._send_json(405, {"error": "Method Not Allowed on control host."})

  def do_DELETE(self) -> None:
    vhost_tag = self._extract_vhost_tag()
    if vhost_tag is not None:
      self._proxy_to_session(vhost_tag, "DELETE")
      return
    self._send_json(405, {"error": "Method Not Allowed on control host."})

  def do_PATCH(self) -> None:
    vhost_tag = self._extract_vhost_tag()
    if vhost_tag is not None:
      self._proxy_to_session(vhost_tag, "PATCH")
      return
    self._send_json(405, {"error": "Method Not Allowed on control host."})

  def do_HEAD(self) -> None:
    vhost_tag = self._extract_vhost_tag()
    if vhost_tag is not None:
      self._proxy_to_session(vhost_tag, "HEAD", is_head=True)
      return
    self.send_response(404)
    self.send_header("Content-Length", "0")
    self.end_headers()

  def _proxy_webmcp_get(self, target_path_and_query: str) -> None:
    cfg = self.config
    target_url = f"http://127.0.0.1:{cfg.webmcp_port}{target_path_and_query}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(target_url, method="GET")
    try:
      with opener.open(req, timeout=5.0) as resp:
        raw = resp.read()
        self.send_response(resp.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)
    except urllib.error.HTTPError as e:
      raw = e.read()
      self.send_response(e.code)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(raw)))
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()
      self.wfile.write(raw)
    except Exception as e:
      self._send_json(502, {"error": f"Failed to reach web_mcp daemon: {e}"})

  def _proxy_webmcp_post(
      self, target_path: str, body: bytes, timeout: float = 5.0
  ) -> None:
    cfg = self.config
    target_url = f"http://127.0.0.1:{cfg.webmcp_port}{target_path}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        target_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
      with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
        self.send_response(resp.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)
    except urllib.error.HTTPError as e:
      raw = e.read()
      self.send_response(e.code)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(raw)))
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()
      self.wfile.write(raw)
    except Exception as e:
      self._send_json(502, {"error": f"Failed to reach web_mcp daemon: {e}"})

  def do_GET(self) -> None:
    vhost_tag = self._extract_vhost_tag()
    if vhost_tag is not None:
      self._proxy_to_session(vhost_tag, "GET")
      return

    cfg = self.config
    parsed = urlparse(self.path)
    parsed_path = parsed.path
    query_params = parse_qs(parsed.query)
    tag_param = (query_params.get("tag") or [""])[0].strip()

    if (
        self.headers.get("Upgrade", "").lower() == "websocket"
        or parsed_path in ("/ws", "/api/cli/ws")
    ):
      ws_key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
      if not ws_key:
        self._send_json(400, {"error": "Missing Sec-WebSocket-Key header."})
        return
      accept = base64.b64encode(
          hashlib.sha1((ws_key + WS_MAGIC_GUID).encode("utf-8")).digest()
      ).decode("ascii")
      handshake = (
          b"HTTP/1.1 101 Switching Protocols\r\n"
          b"Upgrade: websocket\r\n"
          b"Connection: Upgrade\r\n"
          b"Sec-WebSocket-Accept: "
          + accept.encode("ascii")
          + b"\r\n\r\n"
      )
      self.connection.sendall(handshake)
      self.close_connection = True
      self.get_bridge().handle_cli_websocket(
          self.connection, initial_tag=tag_param
      )
      return

    if parsed_path in (
        "/vendor/xterm/xterm.js",
        "/vendor/xterm/xterm-addon-fit.js",
        "/vendor/xterm/xterm.css",
    ):
      fname = os.path.basename(parsed_path)
      fpath = os.path.join(
          UDMI_ROOT, "gummi", "src", "static", "vendor", "xterm", fname
      )
      if os.path.exists(fpath):
        with open(fpath, "rb") as f:
          raw = f.read()
        ctype = (
            "text/css; charset=utf-8"
            if fname.endswith(".css")
            else "application/javascript; charset=utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)
        return

    if parsed_path in ("/ui/axoloctl.svg", "/favicon.ico"):
      icon_dir = os.path.join(UDMI_ROOT, "mcp", "axoloctl", "extension", "icons")
      if parsed_path == "/ui/axoloctl.svg":
        icon_path = os.path.join(icon_dir, "axoloctl.svg")
        ctype = "image/svg+xml; charset=utf-8"
      else:
        icon_path = os.path.join(icon_dir, "icon32.png")
        ctype = "image/png"
      if os.path.exists(icon_path):
        with open(icon_path, "rb") as f:
          raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)
        return

    if parsed_path in ("/api/status", "/status"):
      self._proxy_webmcp_get("/status")
      return

    if parsed_path in ("/api/resolve", "/resolve"):
      qs = f"?{parsed.query}" if parsed.query else ""
      self._proxy_webmcp_get(f"/resolve{qs}")
      return

    if parsed_path == "/api/cli":
      offset_val: Optional[int] = None
      if "offset" in query_params:
        try:
          offset_val = int(query_params["offset"][0])
        except ValueError:
          offset_val = 0
      self._send_json(
          200, self.get_bridge().capture_cli(tag_param, offset=offset_val)
      )
      return

    if parsed_path == "/api/hub/status":
      self._send_json(200, self.get_bridge().get_hub_status(tag_param))
      return

    if parsed_path == "/api/chat/messages":
      since_raw = (query_params.get("since") or ["-1"])[0]
      try:
        since_step = int(since_raw)
      except ValueError:
        since_step = -1
      self._send_json(
          200, self.get_bridge().get_chat_messages(tag_param, since_step=since_step)
      )
      return

    if parsed_path == "/api/uis":
      host_hdr = self.headers.get("Host") or f"localhost:{cfg.host_port}"
      if host_hdr.startswith("127.0.0.1:"):
        host_hdr = f"localhost:{host_hdr.split(':', 1)[1]}"
      uis_payload = {
          "default_ui": cfg.default_ui,
          "uis": [
              {
                  "id": item.id,
                  "label": item.label,
                  "url": f"http://{host_hdr}{item.path}",
              }
              for item in cfg.uis
          ],
      }
      self._send_json(200, uis_payload)
      return

    for item in cfg.uis:
      if parsed_path == item.path:
        bridge = self.get_bridge()
        resolved_tag = bridge.resolve_default_tag(tag_param)
        if item.id == "hubView":
          bridge.get_hub_status(resolved_tag)
        body = _render_ui_page(
            cfg,
            item.id,
            item.label,
            active_tag=resolved_tag,
            hub_port=bridge.hub_port,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        return

    self.send_response(404)
    self.send_header("Content-Length", "0")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.end_headers()

  def _read_json_body(self) -> Dict[str, Any]:
    content_length = int(self.headers.get("Content-Length", 0))
    if content_length <= 0:
      return {}
    raw = self.rfile.read(content_length)
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else {}

  def do_POST(self) -> None:
    vhost_tag = self._extract_vhost_tag()
    if vhost_tag is not None:
      self._proxy_to_session(vhost_tag, "POST")
      return

    cfg = self.config
    parsed = urlparse(self.path)
    parsed_path = parsed.path

    if parsed_path in ("/api/sessions", "/sessions"):
      content_length = int(self.headers.get("Content-Length", 0))
      post_data = self.rfile.read(content_length)
      self._proxy_webmcp_post("/sessions", post_data, timeout=30.0)
      return

    if parsed_path in ("/api/telemetry", "/telemetry"):
      content_length = int(self.headers.get("Content-Length", 0))
      post_data = self.rfile.read(content_length)
      self._proxy_webmcp_post("/telemetry", post_data)
      return

    if parsed_path == "/api/cli":
      try:
        payload = self._read_json_body()
        raw_hex = payload.get("hexKeys")
        hex_keys = (
            [str(k) for k in raw_hex] if isinstance(raw_hex, list) else None
        )
        cols = (
            int(payload["cols"])
            if "cols" in payload and payload["cols"] is not None
            else None
        )
        rows = (
            int(payload["rows"])
            if "rows" in payload and payload["rows"] is not None
            else None
        )
        res = self.get_bridge().send_cli_input(
            tag=str(payload.get("tag", "")),
            text=str(payload.get("text", "")),
            key=str(payload.get("key", "")),
            submit=bool(payload.get("submit", True)),
            hex_keys=hex_keys,
            cols=cols,
            rows=rows,
            action=str(payload.get("action", "")),
        )
        self._send_json(200, res)
      except ValueError as e:
        self._send_json(400, {"error": str(e)})
      except Exception as e:
        self._send_json(500, {"error": str(e)})
      return

    if parsed_path == "/api/hub/start":
      try:
        payload = self._read_json_body()
        res = self.get_bridge().start_hub(tag=str(payload.get("tag", "")))
        self._send_json(200, res)
      except Exception as e:
        self._send_json(500, {"error": str(e)})
      return

    if parsed_path == "/api/chat":
      try:
        payload = self._read_json_body()
        res = self.get_bridge().send_chat_prompt(
            tag=str(payload.get("tag", "")),
            prompt=str(payload.get("prompt", "")),
        )
        self._send_json(200, res)
      except ValueError as e:
        self._send_json(400, {"error": str(e)})
      except Exception as e:
        self._send_json(502, {"error": str(e)})
      return

    if parsed_path == "/api/chat/new":
      try:
        payload = self._read_json_body()
        res = self.get_bridge().reset_conversation(tag=str(payload.get("tag", "")))
        self._send_json(200, res)
      except ValueError as e:
        self._send_json(400, {"error": str(e)})
      except Exception as e:
        self._send_json(500, {"error": str(e)})
      return

    if parsed_path.startswith("/mcp/"):
      parts = parsed_path.split("/")
      if len(parts) >= 4 and parts[2] and parts[3]:
        server_name = parts[2]
        tool_name = parts[3]

        if server_name not in cfg.mcp_servers:
          self._send_json(
              404,
              {
                  "error": (
                      f"MCP server '{server_name}' is not registered in "
                      f"{cfg.mcp_config_path}"
                  )
              },
          )
          return

        server_spec = cfg.mcp_servers[server_name]
        cmd_raw = server_spec.get("command")
        cmd_args = server_spec.get("args", [])
        if not cmd_raw:
          self._send_json(
              500, {"error": f"Invalid command spec for MCP '{server_name}'"}
          )
          return

        bin_path = (
            cmd_raw
            if os.path.isabs(cmd_raw)
            else os.path.join(UDMI_ROOT, cmd_raw)
        )
        if not os.path.exists(bin_path):
          self._send_json(
              404, {"error": f"MCP binary not found at {bin_path}"}
          )
          return

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)
        try:
          args = json.loads(post_data.decode("utf-8")) if post_data else {}
        except json.JSONDecodeError:
          self._send_json(400, {"error": "Invalid JSON body"})
          return

        mcp_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": args,
            },
        }

        env = os.environ.copy()
        env["AXOLOCTL_CONFIG"] = cfg.config_path
        env["AXOLOCTL_WEBMCP_PORT"] = str(cfg.webmcp_port)
        hdr_tag = (self.headers.get("X-Axoloctl-Tag") or "").strip()
        if hdr_tag:
          env["AXOLOCTL_TAG"] = hdr_tag

        try:
          proc = subprocess.run(
              [bin_path] + [str(a) for a in cmd_args],
              input=json.dumps(mcp_req).encode("utf-8") + b"\n",
              stdout=subprocess.PIPE,
              stderr=subprocess.PIPE,
              env=env,
              timeout=15,
              check=False,
          )
          out_lines = proc.stdout.decode("utf-8").strip().splitlines()
          if out_lines:
            raw_out = out_lines[-1].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw_out)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(raw_out)
          else:
            stderr_msg = proc.stderr.decode("utf-8").strip()
            self._send_json(
                502,
                {
                    "error": (
                        f"Empty response from MCP server '{server_name}'"
                        + (f": {stderr_msg}" if stderr_msg else "")
                    )
                },
            )
        except Exception as e:
          self._send_json(500, {"error": str(e)})
      else:
        self._send_json(
            400, {"error": "Expected path /mcp/<server_name>/<tool_name>"}
        )
    else:
      self.send_response(404)
      self.send_header("Content-Length", "0")
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()

  def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
    raw = json.dumps(payload).encode("utf-8")
    self.send_response(status_code)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(raw)))
    self.send_header("Access-Control-Allow-Origin", "*")
    self.end_headers()
    self.wfile.write(raw)

  def log_message(self, fmt: str, *args: Any) -> None:
    pass


def main() -> None:
  parser = argparse.ArgumentParser(description="Axoloctl UI Host Gateway")
  parser.add_argument(
      "--config",
      default=os.environ.get("AXOLOCTL_CONFIG", ""),
      help="Path to the explicit Axoloctl JSON configuration file",
  )
  args = parser.parse_args()

  config = load_config(args.config, UDMI_ROOT)
  UIHostHandler.config = config
  UIHostHandler.sessions_dir = os.path.join(
      UDMI_ROOT, "var", "axoloctl", "sessions"
  )
  UIHostHandler.get_bridge()._ensure_hub_server()

  server = ThreadingHTTPServer(("127.0.0.1", config.host_port), UIHostHandler)
  server.allow_reuse_address = True
  print(
      f"ui_host listening on http://127.0.0.1:{config.host_port} "
      f"(vhost=*.localhost:{config.host_port}, mcp_config={config.mcp_config_path})"
  )
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    server.server_close()


if __name__ == "__main__":
  main()
