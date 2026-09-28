#!/usr/bin/env python3
"""Transparent front-door HTTP proxy and supervisor for Axoloctl web sessions.

Listens on the external session port allocated by SessionManager, intercepts
/.axoloctl/beacon and /.axoloctl/telemetry for two-path browser-to-backend
correlation, and proxies all application traffic (including streaming SSE) to
the target web server entrypoint running on an internal loopback port.
"""

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse


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


def allocate_ephemeral_port() -> int:
  """Allocates a free loopback TCP port for the internal application server."""
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    return int(s.getsockname()[1])


def wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 9.0) -> bool:
  """Waits for the internal application server to accept TCP connections."""
  deadline = time.time() + timeout
  while time.time() < deadline:
    if proc.poll() is not None:
      return False
    try:
      with socket.create_connection(("127.0.0.1", port), timeout=0.5):
        return True
    except OSError:
      time.sleep(0.2)
  return False


class BeaconStore:
  """Thread-safe persistence for viewer nonces correlated with this session."""

  def __init__(self, beacons_file: str, log_file: str, max_nonces: int = 128):
    self.beacons_file = beacons_file
    self.log_file = log_file
    self.max_nonces = max_nonces
    self._lock = threading.Lock()

  def record_nonce(
      self, nonce: str, url: str = "", title: str = ""
  ) -> Dict[str, Any]:
    now = time.time()
    with self._lock:
      data = self._load_unlocked()
      # Prune expired (>24h) or excess nonces
      cutoff = now - 86400.0
      data = {
          k: v
          for k, v in data.items()
          if isinstance(v, dict) and v.get("timestamp", 0) >= cutoff
      }
      data[nonce] = {
          "timestamp": now,
          "url": url,
          "title": title,
      }
      if len(data) > self.max_nonces:
        sorted_keys = sorted(
            data.keys(), key=lambda k: data[k].get("timestamp", 0)
        )
        for old_key in sorted_keys[: len(data) - self.max_nonces]:
          del data[old_key]
      self._save_unlocked(data)
      return data[nonce]

  def has_nonce(self, nonce: str) -> bool:
    with self._lock:
      data = self._load_unlocked()
      return nonce in data

  def append_browser_log(self, message: str) -> None:
    with self._lock:
      os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
      with open(self.log_file, "a", encoding="utf-8") as f:
        f.write(f"[browser] {message}\n")

  def append_server_log(self, line: str) -> None:
    with self._lock:
      os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
      with open(self.log_file, "a", encoding="utf-8") as f:
        f.write(f"[server] {line}\n")

  def _load_unlocked(self) -> Dict[str, Any]:
    if os.path.exists(self.beacons_file):
      try:
        with open(self.beacons_file, "r", encoding="utf-8") as f:
          content = json.load(f)
          if isinstance(content, dict):
            return content
      except Exception:
        pass
    return {}

  def _save_unlocked(self, data: Dict[str, Any]) -> None:
    tmp_path = f"{self.beacons_file}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
      json.dump(data, f, indent=2)
    os.replace(tmp_path, self.beacons_file)


class SessionProxyHandler(BaseHTTPRequestHandler):
  """Intercepts /.axoloctl/* and reverse-proxies all other traffic to app_port."""

  protocol_version = "HTTP/1.1"
  app_port: int = 0
  tag: str = ""
  commit_hash: str = ""
  description: str = ""
  beacon_store: Optional[BeaconStore] = None

  def do_OPTIONS(self) -> None:
    parsed = urlparse(self.path)
    if parsed.path.startswith("/.axoloctl/"):
      self.send_response(204)
      self._send_cors_headers()
      self.send_header("Content-Length", "0")
      self.end_headers()
      return
    self._proxy_request("OPTIONS")

  def do_GET(self) -> None:
    parsed = urlparse(self.path)
    if parsed.path == "/.axoloctl/beacon":
      query = parse_qs(parsed.query)
      nonce = (query.get("nonce") or [""])[0].strip()
      url = (query.get("url") or [""])[0].strip()
      title = (query.get("title") or [""])[0].strip()
      self._handle_beacon(nonce, url, title)
      return
    self._proxy_request("GET")

  def do_POST(self) -> None:
    parsed = urlparse(self.path)
    if parsed.path == "/.axoloctl/beacon":
      body = self._read_json_body()
      if body is None:
        return
      nonce = str(body.get("nonce") or "").strip()
      url = str(body.get("url") or "").strip()
      title = str(body.get("title") or "").strip()
      self._handle_beacon(nonce, url, title)
      return

    if parsed.path == "/.axoloctl/telemetry":
      body = self._read_json_body()
      if body is None:
        return
      message = str(body.get("message") or "").strip()
      nonce = str(body.get("nonce") or "").strip()
      if not message:
        self._send_json(400, {"error": "Missing required 'message' field."})
        return
      if nonce:
        self.beacon_store.record_nonce(nonce)
      self.beacon_store.append_browser_log(message)
      self._send_json(
          200,
          {
              "ok": True,
              "tag": self.tag,
              "commit_hash": self.commit_hash,
              "nonce": nonce,
          },
      )
      return

    self._proxy_request("POST")

  def do_PUT(self) -> None:
    self._proxy_request("PUT")

  def do_DELETE(self) -> None:
    self._proxy_request("DELETE")

  def do_PATCH(self) -> None:
    self._proxy_request("PATCH")

  def do_HEAD(self) -> None:
    self._proxy_request("HEAD", is_head=True)

  def _handle_beacon(self, nonce: str, url: str, title: str) -> None:
    if not nonce:
      self._send_json(400, {"error": "Missing required 'nonce' parameter."})
      return
    self.beacon_store.record_nonce(nonce, url=url, title=title)
    self._send_json(
        200,
        {
            "axoloctl": True,
            "tag": self.tag,
            "commit_hash": self.commit_hash,
            "description": self.description,
            "nonce": nonce,
        },
    )

  def _read_json_body(self) -> Optional[Dict[str, Any]]:
    content_len = int(self.headers.get("Content-Length", 0))
    if content_len <= 0:
      return {}
    raw = self.rfile.read(content_len)
    try:
      data = json.loads(raw.decode("utf-8"))
      if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
      return data
    except Exception as e:
      self._send_json(400, {"error": f"Invalid JSON payload: {e}"})
      return None

  def _send_cors_headers(self) -> None:
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header(
        "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS, HEAD"
    )
    self.send_header(
        "Access-Control-Allow-Headers", "Content-Type, Authorization, X-Axoloctl-Nonce"
    )

  def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    self.send_response(status_code)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self._send_cors_headers()
    self.end_headers()
    self.wfile.write(body)

  def _proxy_request(self, method: str, is_head: bool = False) -> None:
    content_len = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_len) if content_len > 0 else None

    # Record header-based nonce if present on normal requests
    hdr_nonce = self.headers.get("X-Axoloctl-Nonce", "").strip()
    if hdr_nonce:
      self.beacon_store.record_nonce(hdr_nonce, url=self.path)

    forward_headers = {}
    for k, v in self.headers.items():
      if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "host":
        forward_headers[k] = v
    forward_headers["Host"] = f"127.0.0.1:{self.app_port}"
    forward_headers["Connection"] = "close"

    conn = http.client.HTTPConnection("127.0.0.1", self.app_port, timeout=60)
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

      self.send_header("X-Axoloctl-Tag", self.tag)
      self.send_header("X-Axoloctl-Commit", self.commit_hash)

      if is_sse:
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        if not is_head:
          while True:
            chunk = upstream.fp.read1(4096) if hasattr(upstream.fp, "read1") else upstream.read(128)
            if not chunk:
              break
            self.wfile.write(chunk)
            self.wfile.flush()
        return

      resp_body = b"" if is_head else upstream.read()
      if not has_content_length and not is_head:
        self.send_header("Content-Length", str(len(resp_body)))
      self.send_header("Connection", "close")
      self.end_headers()
      if not is_head and resp_body:
        self.wfile.write(resp_body)
        self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
      pass
    except Exception as e:
      try:
        self._send_json(
            502,
            {
                "error": "Bad Gateway",
                "message": f"Upstream session '{self.tag}' on port {self.app_port} failed: {e}",
            },
        )
      except Exception:
        pass
    finally:
      conn.close()

  def log_message(self, fmt: str, *args: Any) -> None:
    pass


def main() -> None:
  parser = argparse.ArgumentParser(description="Axoloctl Session Front-Door Proxy")
  parser.add_argument("--port", type=int, required=True, help="External session port")
  parser.add_argument("--tag", required=True, help="Session tag")
  parser.add_argument("--commit", required=True, help="40-char commit hash")
  parser.add_argument("--description", default="", help="Session description")
  parser.add_argument("--app-dir", required=True, help="Application working directory")
  parser.add_argument("--entrypoint", required=True, help="Relative entrypoint path")
  parser.add_argument("--port-env-var", default="", help="Optional port env var name")
  parser.add_argument("--log-file", required=True, help="Path to unified.log")
  parser.add_argument("--beacons-file", required=True, help="Path to beacons.json")
  args = parser.parse_args()

  app_port = allocate_ephemeral_port()
  beacon_store = BeaconStore(args.beacons_file, args.log_file)

  env = os.environ.copy()
  env["AXOLOCTL_PORT"] = str(app_port)
  env["AXOLOCTL_EXTERNAL_PORT"] = str(args.port)
  if args.port_env_var:
    env[args.port_env_var] = str(app_port)

  child_proc = subprocess.Popen(
      [f"./{args.entrypoint}"],
      cwd=args.app_dir,
      env=env,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      bufsize=1,
  )

  def forward_output() -> None:
    assert child_proc.stdout is not None
    for raw_line in child_proc.stdout:
      line = raw_line.rstrip("\r\n")
      beacon_store.append_server_log(line)
      print(f"[server] {line}", flush=True)

  log_thread = threading.Thread(target=forward_output, daemon=True)
  log_thread.start()

  if not wait_for_port(app_port, child_proc, timeout=9.0):
    rc = child_proc.poll()
    if rc is None:
      child_proc.terminate()
      try:
        child_proc.wait(timeout=2.0)
      except subprocess.TimeoutExpired:
        child_proc.kill()
      rc = 1
    log_thread.join(timeout=1.0)
    sys.exit(rc if rc != 0 else 1)

  SessionProxyHandler.app_port = app_port
  SessionProxyHandler.tag = args.tag
  SessionProxyHandler.commit_hash = args.commit
  SessionProxyHandler.description = args.description
  SessionProxyHandler.beacon_store = beacon_store

  httpd = ThreadingHTTPServer(("0.0.0.0", args.port), SessionProxyHandler)
  httpd.allow_reuse_address = True
  httpd.daemon_threads = True

  beacon_store.append_server_log(
      f"Axoloctl session proxy listening on :{args.port} -> 127.0.0.1:{app_port} (tag={args.tag})"
  )

  def shutdown_handler(signum: int, _frame: Any) -> None:
    if child_proc.poll() is None:
      child_proc.terminate()
      try:
        child_proc.wait(timeout=2.0)
      except subprocess.TimeoutExpired:
        child_proc.kill()
    threading.Thread(target=httpd.shutdown, daemon=True).start()

  signal.signal(signal.SIGTERM, shutdown_handler)
  signal.signal(signal.SIGINT, shutdown_handler)
  signal.signal(signal.SIGHUP, shutdown_handler)

  def watch_child() -> None:
    child_proc.wait()
    httpd.shutdown()

  watcher = threading.Thread(target=watch_child, daemon=True)
  watcher.start()

  try:
    httpd.serve_forever()
  finally:
    httpd.server_close()
    if child_proc.poll() is None:
      child_proc.terminate()
    log_thread.join(timeout=1.0)
    sys.exit(child_proc.returncode or 0)


if __name__ == "__main__":
  main()
