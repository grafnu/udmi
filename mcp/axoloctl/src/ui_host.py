#!/usr/bin/env python3
"""Axoloctl UI Host Gateway, Virtual-Host Session Router & HTTP-to-MCP Proxy."""

import argparse
from datetime import datetime
import glob
import html
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
from urllib.parse import parse_qs, urlparse
import urllib.request
import uuid

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


class JetskiAgentBridge:
  """Manages per-tag communication with the session's dedicated Jetski Agent."""

  def __init__(self, udmi_root: str, config: AxoloctlConfig, sessions_dir: str):
    self.udmi_root = udmi_root
    self.config = config
    self.sessions_dir = sessions_dir
    self.session_agent = "udmi_axoloctl_agent"
    self.brain_root = os.path.expanduser("~/.gemini/jetski/brain")
    self.hub_port = int(os.environ.get("AXOLOCTL_HUB_PORT", "5387"))
    self.last_hub_error: str = ""
    self._lock = threading.Lock()
    self._ls_cache: Dict[str, Tuple[str, str, float]] = {}
    self._pending_turns: Dict[str, Dict[str, Any]] = {}

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

  def get_conversation_id(self, tag: str) -> str:
    cfile = self.conv_file_path(tag)
    if os.path.exists(cfile):
      try:
        with open(cfile, "r", encoding="utf-8") as f:
          cid = f.read().strip()
        if cid:
          return cid
      except Exception:
        pass
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

  def is_agent_window_running(self, tag: str) -> bool:
    if not tag:
      return False
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
    if res.returncode != 0:
      return False
    return tag in [line.strip() for line in res.stdout.splitlines()]

  # ---------------------------------------------------------------------------
  # Technique 1: Direct Terminal / Tmux Pane Control (cliView)
  # ---------------------------------------------------------------------------

  def capture_cli(self, tag: str) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    if not resolved_tag:
      return {
          "tag": "",
          "running": False,
          "window": "",
          "output": "No active Axoloctl session. Start a session to attach its dedicated agent.",
      }
    running = self.is_agent_window_running(resolved_tag)
    window_target = f"{self.session_agent}:{resolved_tag}"
    if not running:
      return {
          "tag": resolved_tag,
          "running": False,
          "window": window_target,
          "output": f"Agent window {window_target} is not currently running.",
      }
    res = subprocess.run(
        ["tmux", "capture-pane", "-p", "-J", "-S", "-200", "-t", window_target],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    output = res.stdout if res.returncode == 0 else res.stderr
    return {
        "tag": resolved_tag,
        "running": True,
        "window": window_target,
        "workspace": self.worktree_path(resolved_tag),
        "output": output.rstrip() + "\n",
    }

  def send_cli_input(
      self,
      tag: str,
      text: str = "",
      key: str = "",
      submit: bool = True,
  ) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    if not resolved_tag:
      raise ValueError("No active session tag specified for CLI input.")
    if not self.is_agent_window_running(resolved_tag):
      raise ValueError(
          f"Agent window '{self.session_agent}:{resolved_tag}' is not running."
      )
    window_target = f"{self.session_agent}:{resolved_tag}"
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

  # ---------------------------------------------------------------------------
  # Technique 2: Jetski Web Hub Management (hubView)
  # ---------------------------------------------------------------------------

  def _is_port_listening(self, port: int) -> bool:
    try:
      with socket.create_connection(("127.0.0.1", port), timeout=0.5):
        return True
    except OSError:
      return False

  def get_hub_status(self, tag: str) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    wt_path = self.worktree_path(resolved_tag) if resolved_tag else self.udmi_root
    running = self._is_port_listening(self.hub_port)
    return {
        "tag": resolved_tag,
        "hub_port": self.hub_port,
        "hub_url": f"http://localhost:{self.hub_port}/",
        "running": running,
        "workspace": wt_path,
        "last_error": "" if running else self.last_hub_error,
    }

  def start_hub(self, tag: str) -> Dict[str, Any]:
    resolved_tag = self.resolve_default_tag(tag)
    wt_path = self.worktree_path(resolved_tag) if resolved_tag else self.udmi_root
    if not os.path.isdir(wt_path):
      wt_path = self.udmi_root

    jetski_bin = self._get_jetski_bin()
    try:
      proc = subprocess.Popen(
          [jetski_bin, "hub", "launch", wt_path, "--port", str(self.hub_port)],
          cwd=wt_path,
          stdout=subprocess.PIPE,
          stderr=subprocess.STDOUT,
          text=True,
      )
      for _ in range(10):
        if self._is_port_listening(self.hub_port):
          self.last_hub_error = ""
          break
        if proc.poll() is not None:
          out = (proc.stdout.read() if proc.stdout else "").strip()
          self.last_hub_error = out or f"jetski hub exited with code {proc.returncode}"
          break
        time.sleep(0.25)
    except Exception as e:
      self.last_hub_error = str(e)

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

    jetski_bin = self._get_jetski_bin()
    tmux_env = self._read_tmux_global_env()
    host_ls = os.environ.get("ANTIGRAVITY_LS_ADDRESS") or tmux_env.get(
        "ANTIGRAVITY_LS_ADDRESS", ""
    )
    host_csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN") or tmux_env.get(
        "ANTIGRAVITY_CSRF_TOKEN", ""
    )

    candidates: List[Tuple[str, str]] = []
    if host_ls:
      candidates.append((host_ls, host_csrf))
    for port in reversed(self._discover_pane_ports(tag)):
      candidates.append((f"localhost:{port}", f"axoloctl-{tag}"))
      if host_csrf:
        candidates.append((f"localhost:{port}", host_csrf))

    probe_cid = self.get_conversation_id(tag)
    for addr, csrf in candidates:
      env = self._build_agentapi_env(addr, csrf)
      try:
        res = subprocess.run(
            [jetski_bin, "agentapi", "get-conversation-metadata", probe_cid],
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
      self, jetski_bin: str, cid: str, env: Dict[str, str], cwd: str
  ) -> bool:
    if not cid:
      return False
    tfile = os.path.join(
        self.brain_root, cid, ".system_generated", "logs", "transcript.jsonl"
    )
    try:
      res = subprocess.run(
          [jetski_bin, "agentapi", "get-conversation-metadata", cid],
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
    jetski_bin = self._get_jetski_bin()
    ls_address, csrf_token = self._resolve_ls_credentials(resolved_tag)
    if not ls_address:
      raise RuntimeError(
          f"No reachable Jetski Language Server found for session '{resolved_tag}'."
      )

    env = self._build_agentapi_env(ls_address, csrf_token)
    cid = self.get_conversation_id(resolved_tag)

    with self._lock:
      if self._conversation_exists_on_ls(jetski_bin, cid, env, cwd):
        baseline_step = self._get_last_transcript_step(cid)
        res = subprocess.run(
            [jetski_bin, "agentapi", "send-message", cid, clean_prompt],
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
              jetski_bin,
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


def _render_ui_page(
    config: AxoloctlConfig, ui_id: str, ui_label: str, active_tag: str = ""
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
        <div class="card">
          <div class="card-header">
            <h2>Dedicated Agent CLI Console (<code>{agent_window_ref}</code>)</h2>
            <span id="cli-status-pill" class="pill">Connecting...</span>
          </div>
          <p class="meta">Session: <code id="bound-tag-label">{escaped_tag}</code> | Worktree: <code id="bound-worktree-label">{escaped_worktree}</code> (<code>{branch_ref}</code>)</p>
          <p class="meta">Terminal attach: <code>{attach_cmd}</code> | Entrypoint: <code>{escaped_entry}</code></p>
          <pre class="terminal" id="cli-output">$ cd {escaped_worktree}
[axoloctl:{escaped_tag}] Connecting to tmux pane {agent_window_ref}...</pre>
          <form id="cli-form" class="chat-form" style="margin-top:8px;">
            <input type="text" id="cli-input" placeholder="Send command or prompt to {agent_window_ref}..." autocomplete="off" />
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
      body_section = f"""
        <div class="card">
          <div class="card-header">
            <h2>Axoloctl Agent Web Hub (<code>{agent_window_ref}</code>)</h2>
            <span id="hub-status-pill" class="pill">Checking Hub...</span>
          </div>
          <p class="meta">Session: <code id="bound-tag-label">{escaped_tag}</code> (branch <code>{branch_ref}</code>) | Worktree: <code id="bound-worktree-label">{escaped_worktree}</code></p>
          <p class="meta">MCP Servers: <code>{html.escape(mcp_names)}</code> | App: <code>{escaped_subpath}</code> (<code>{escaped_entry}</code>)</p>
          <div class="key-bar" style="margin-bottom:8px;">
            <button type="button" id="hub-start-btn" class="action-btn">Launch / Rebind Jetski Hub</button>
            <a id="hub-open-link" href="http://localhost:5387/" target="_blank" class="key-btn" style="display:inline-block;line-height:18px;">Open Hub in New Tab ↗</a>
          </div>
          <div id="hub-error-box" class="error-banner" style="display:none;"></div>
          <iframe id="hub-iframe" class="hub-frame" src="about:blank" style="display:none;"></iframe>
        </div>
      """
    else:
      body_section = f"""
        <div class="card">
          <div class="card-header">
            <h2>Agent Chat — Dedicated Agent (<code>{agent_window_ref}</code>)</h2>
            <div style="display:flex;gap:6px;align-items:center;">
              <span id="chat-status-pill" class="pill">● Ready</span>
              <button type="button" id="chat-reset-btn" class="key-btn" title="Start a fresh conversation">New Chat</button>
            </div>
          </div>
          <p class="meta">Session: <code id="bound-tag-label">{escaped_tag}</code> | Branch: <code>{branch_ref}</code> | Conv: <code id="chat-conv-id">...</code></p>
          <div id="chat-error-box" class="error-banner" style="display:none;"></div>
          <div id="chat-log" class="chat-log">
            <div class="msg agent">Axoloctl Agent [<code>{escaped_tag}</code>] bound to isolated worktree <code>{branch_ref}</code>. Send a prompt below to communicate via <code>jetski agentapi</code>.</div>
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
        <div class="card">
          <div class="card-header">
            <h2>Dedicated Agent CLI Console — No Correlated Viewer</h2>
            <span id="cli-status-pill" class="pill">○ Uncorrelated</span>
          </div>
          <p class="meta">Session: <code id="bound-tag-label">None</code> | No correlated viewer in active tab</p>
          <pre class="terminal" id="cli-output" style="color:#94a3b8;">No correlated Axoloctl viewer in the active browser tab.

Switch to an active session tab (e.g. http://&lt;tag&gt;.localhost:9290) to attach its dedicated agent CLI console.</pre>
          <form id="cli-form" class="chat-form" style="margin-top:8px;">
            <input type="text" id="cli-input" placeholder="No correlated viewer in active tab" disabled />
            <button type="submit" disabled>Send</button>
          </form>
        </div>
      """
    elif ui_id == "hubView":
      body_section = """
        <div class="card">
          <div class="card-header">
            <h2>Axoloctl Agent Web Hub — No Correlated Viewer</h2>
            <span id="hub-status-pill" class="pill">○ Uncorrelated</span>
          </div>
          <p class="meta">Session: <code id="bound-tag-label">None</code> | No correlated viewer in active tab</p>
          <div class="msg agent" style="border-style:dashed;color:#94a3b8;margin-top:8px;"><strong>No correlated viewer in active tab.</strong><br>There is no Axoloctl session associated with the current browser tab. Switch to an active session tab (e.g. <code>http://&lt;tag&gt;.localhost:9290</code>) to view its Web Hub.</div>
        </div>
      """
    else:
      body_section = """
        <div class="card">
          <div class="card-header">
            <h2>Agent Chat — No Correlated Viewer</h2>
            <div style="display:flex;gap:6px;align-items:center;">
              <span id="chat-status-pill" class="pill">○ Uncorrelated</span>
              <button type="button" id="chat-reset-btn" class="key-btn" disabled>New Chat</button>
            </div>
          </div>
          <p class="meta">Session: <code id="bound-tag-label">None</code> | No correlated viewer in active tab</p>
          <div id="chat-log" class="chat-log">
            <div class="msg agent" style="border-style:dashed;color:#94a3b8;"><strong>No correlated viewer in active tab.</strong><br>There is no Axoloctl session associated with the current browser tab. Switch to an active session tab (e.g. <code>http://&lt;tag&gt;.localhost:9290</code>) to view and interact with its Agent Chat.</div>
          </div>
          <form id="chat-form" class="chat-form">
            <input type="text" id="chat-input" placeholder="No correlated viewer in active tab" autocomplete="off" disabled />
            <button type="submit" id="chat-send-btn" disabled>Send</button>
          </form>
        </div>
      """

  page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Axoloctl — {escaped_label}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      margin: 0;
      padding: 12px;
      background: #0f172a;
      color: #e2e8f0;
    }}
    h1 {{ font-size: 15px; margin: 0 0 10px 0; color: #38bdf8; }}
    h2 {{ font-size: 13px; margin: 0; color: #f8fafc; }}
    .card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 6px;
      padding: 10px;
      margin-bottom: 10px;
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
    }}
    .meta {{
      margin: 3px 0;
      font-size: 11px;
      color: #94a3b8;
      word-break: break-all;
    }}
    .pill {{
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 999px;
      background: #0f172a;
      border: 1px solid #334155;
      color: #38bdf8;
      white-space: nowrap;
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
      background: #0f172a;
      padding: 1px 4px;
      border-radius: 4px;
      color: #7dd3fc;
      font-size: 11px;
    }}
    .terminal {{
      background: #020617;
      color: #4ade80;
      padding: 8px;
      border-radius: 4px;
      font-size: 11px;
      line-height: 1.35;
      height: 260px;
      overflow-y: auto;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      margin: 6px 0 0 0;
      border: 1px solid #1e293b;
    }}
    .key-bar {{
      display: flex;
      gap: 6px;
      margin-top: 6px;
      flex-wrap: wrap;
    }}
    .key-btn, .action-btn {{
      background: #0f172a;
      color: #cbd5e1;
      border: 1px solid #475569;
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 11px;
      cursor: pointer;
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
      width: 100%;
      height: 360px;
      border: 1px solid #334155;
      border-radius: 4px;
      background: #020617;
    }}
    .error-banner {{
      background: #450a0a;
      border: 1px solid #dc2626;
      color: #fca5a5;
      padding: 6px 8px;
      border-radius: 4px;
      font-size: 11px;
      margin-bottom: 8px;
      white-space: pre-wrap;
    }}
    .session-item {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 0;
      border-bottom: 1px solid #334155;
      font-size: 12px;
    }}
    .session-item:last-child {{ border-bottom: none; }}
    a {{ color: #38bdf8; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .chat-log {{
      background: #020617;
      border: 1px solid #334155;
      border-radius: 4px;
      height: 250px;
      overflow-y: auto;
      padding: 8px;
      margin: 6px 0 8px 0;
      font-size: 12px;
    }}
    .msg {{
      margin-bottom: 8px;
      padding: 6px 10px;
      border-radius: 6px;
      line-height: 1.4;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .msg.agent {{
      background: #1e293b;
      color: #e2e8f0;
      border: 1px solid #334155;
    }}
    .msg.user {{
      background: #0369a1;
      color: #ffffff;
      margin-left: 20%;
    }}
    .chat-form {{ display: flex; gap: 6px; }}
    .chat-form input {{
      flex: 1;
      background: #020617;
      border: 1px solid #475569;
      color: #f8fafc;
      padding: 6px 8px;
      border-radius: 4px;
      font-size: 12px;
    }}
    .chat-form button {{
      background: #0284c7;
      color: white;
      border: none;
      padding: 6px 12px;
      border-radius: 4px;
      cursor: pointer;
      font-size: 12px;
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
      padding: 8px 10px;
      margin-bottom: 8px;
      font-size: 11px;
      color: #e2e8f0;
    }}
    .activity-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      color: #fbbf24;
      font-weight: 600;
    }}
    .activity-title {{
      display: flex;
      align-items: center;
      gap: 6px;
      word-break: break-word;
    }}
    .activity-elapsed {{
      color: #94a3b8;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 400;
      font-size: 10px;
      white-space: nowrap;
    }}
    .activity-thinking {{
      color: #94a3b8;
      font-style: italic;
      margin-top: 4px;
      line-height: 1.35;
    }}
    .activity-steps {{
      margin-top: 5px;
      padding-top: 4px;
      border-top: 1px solid #1e293b;
      color: #cbd5e1;
      font-size: 10px;
      line-height: 1.45;
    }}
  </style>
</head>
<body>
  <h1>{escaped_label}</h1>
  {body_section}
  <div class="card">
    <h2>Active 1:1 Agent &amp; Web Server Sessions (Gateway :{config.host_port})</h2>
    <div id="sessions-list" style="margin-top:6px;">Loading active sessions...</div>
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
          container.innerHTML = "<em>No active web server sessions.</em>";
          return;
        }}
        const portPart = window.location.port ? `:${{window.location.port}}` : "";
        container.innerHTML = entries.map(([tag, info]) => {{
          const isCorrelated = currentTag && currentTag === tag;
          const badge = isCorrelated
            ? `<span style="background:#065f46;color:#6ee7b7;border:1px solid #059669;padding:1px 6px;border-radius:4px;font-size:10px;margin-left:6px;">ACTIVE</span>`
            : "";
          const agentBadge = info.agent_running
            ? `<span style="background:#1e3a8a;color:#93c5fd;border:1px solid #2563eb;padding:1px 6px;border-radius:4px;font-size:10px;margin-left:6px;">agent:${{tag}}</span>`
            : "";
          const vhostUrl = `${{window.location.protocol}}//${{tag}}.localhost${{portPart}}`;
          const uiSwitchUrl = `${{window.location.pathname}}?tag=${{encodeURIComponent(tag)}}`;
          return `<div class="session-item">
            <span><a href="${{uiSwitchUrl}}"><strong>${{tag}}</strong></a> (<code>${{info.commit.slice(0, 8)}}</code>)${{agentBadge}}${{badge}}</span>
            <a href="${{vhostUrl}}" target="_blank">${{vhostUrl}}</a>
          </div>`;
        }}).join("");
      }} catch (e) {{
        container.innerHTML = "<em>Unable to reach web_mcp daemon.</em>";
      }}
    }}

    // -------------------------------------------------------------------------
    // Technique 1: cliView polling & terminal input
    // -------------------------------------------------------------------------
    let lastCliOutput = "";
    async function pollCliView() {{
      if (!currentTag) return;
      const pre = document.getElementById("cli-output");
      const pill = document.getElementById("cli-status-pill");
      if (!pre) return;
      try {{
        const qs = `?tag=${{encodeURIComponent(currentTag)}}`;
        const resp = await fetch(`/api/cli${{qs}}`);
        const data = await resp.json();
        if (pill) {{
          pill.textContent = data.running ? `● Live (${{data.window}})` : "○ Stopped";
          pill.className = data.running ? "pill ok" : "pill";
        }}
        if (typeof data.output === "string" && data.output !== lastCliOutput) {{
          const nearBottom = (pre.scrollHeight - pre.scrollTop - pre.clientHeight) < 40;
          lastCliOutput = data.output;
          pre.textContent = data.output;
          if (nearBottom) pre.scrollTop = pre.scrollHeight;
        }}
      }} catch (e) {{
        if (pill) pill.textContent = "Disconnected";
      }}
    }}

    async function sendCliPayload(payload) {{
      if (!currentTag) return;
      try {{
        const resp = await fetch("/api/cli", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ tag: currentTag, ...payload }}),
        }});
        const data = await resp.json();
        const pre = document.getElementById("cli-output");
        if (pre && typeof data.output === "string") {{
          lastCliOutput = data.output;
          pre.textContent = data.output;
          pre.scrollTop = pre.scrollHeight;
        }}
      }} catch (e) {{}}
    }}

    if (UI_ID === "cliView" && currentTag) {{
      const cliForm = document.getElementById("cli-form");
      if (cliForm) {{
        cliForm.addEventListener("submit", async (ev) => {{
          ev.preventDefault();
          const input = document.getElementById("cli-input");
          const text = input.value;
          input.value = "";
          await sendCliPayload({{ text, submit: true }});
        }});
      }}
      document.querySelectorAll(".key-btn[data-key]").forEach((btn) => {{
        btn.addEventListener("click", () => {{
          sendCliPayload({{ key: btn.getAttribute("data-key") }});
        }});
      }});
      pollCliView();
      setInterval(pollCliView, 1000);
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
          startBtn.textContent = "Launching Hub...";
          try {{
            await fetch("/api/hub/start", {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{ tag: currentTag }}),
            }});
            await pollHubStatus();
          }} finally {{
            startBtn.disabled = false;
            startBtn.textContent = "Launch / Rebind Jetski Hub";
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

    conn = http.client.HTTPConnection("127.0.0.1", session_port, timeout=60)
    try:
      conn.request(method, self.path, body=body, headers=forward_headers)
      upstream = conn.getresponse()

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

  def _proxy_webmcp_post(self, target_path: str, body: bytes) -> None:
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

    if parsed_path in ("/api/status", "/status"):
      self._proxy_webmcp_get("/status")
      return

    if parsed_path in ("/api/resolve", "/resolve"):
      qs = f"?{parsed.query}" if parsed.query else ""
      self._proxy_webmcp_get(f"/resolve{qs}")
      return

    if parsed_path == "/api/cli":
      self._send_json(200, self.get_bridge().capture_cli(tag_param))
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
        resolved_tag = self.get_bridge().resolve_default_tag(tag_param)
        body = _render_ui_page(cfg, item.id, item.label, active_tag=resolved_tag)
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

    if parsed_path in ("/api/telemetry", "/telemetry"):
      content_length = int(self.headers.get("Content-Length", 0))
      post_data = self.rfile.read(content_length)
      self._proxy_webmcp_post("/telemetry", post_data)
      return

    if parsed_path == "/api/cli":
      try:
        payload = self._read_json_body()
        res = self.get_bridge().send_cli_input(
            tag=str(payload.get("tag", "")),
            text=str(payload.get("text", "")),
            key=str(payload.get("key", "")),
            submit=bool(payload.get("submit", True)),
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
