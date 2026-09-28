#!/usr/bin/env python3
"""Axoloctl UI Host Gateway, Virtual-Host Session Router & HTTP-to-MCP Proxy."""

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, Optional
import urllib.error
from urllib.parse import parse_qs, urlparse
import urllib.request

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


def _render_ui_page(
    config: AxoloctlConfig, ui_id: str, ui_label: str, active_tag: str = ""
) -> bytes:
  """Renders a self-contained HTML page for the requested host UI endpoint, routed to active_tag's dedicated Agent."""
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
  else:
    escaped_worktree = html.escape(
        f"{UDMI_ROOT}/var/axoloctl/sessions/<tag>/workspace/{config.app_subpath}"
    )
    escaped_tag = "<tag>"
    agent_window_ref = "udmi_axoloctl_agent:<tag>"
    branch_ref = "axoloctl-<tag>"
    attach_cmd = "bin/tmux_axoloctl attach <tag>"

  body_section = ""
  if ui_id == "cliView":
    body_section = f"""
      <div class="card">
        <h2>Dedicated Agent CLI Console (<code>{agent_window_ref}</code>)</h2>
        <p>Session Tag: <code>{escaped_tag}</code> (1:1 Paired Agent &amp; Server)</p>
        <p>Isolated Worktree: <code>{escaped_worktree}</code> (branch <code>{branch_ref}</code>)</p>
        <p>Entrypoint: <code>{escaped_entry}</code></p>
        <pre class="terminal" id="cli-output">$ cd {escaped_worktree}
$ # Attach in terminal: {attach_cmd}
[axoloctl:{escaped_tag}] Ready. Bound AXOLOCTL_TAG={escaped_tag}. Connected MCPs: {html.escape(mcp_names)}</pre>
      </div>
    """
  elif ui_id == "hubView":
    body_section = f"""
      <div class="card">
        <h2>Axoloctl Agent Web Hub (<code>{agent_window_ref}</code>)</h2>
        <p>Bound Session Tag: <code>{escaped_tag}</code> (branch <code>{branch_ref}</code>)</p>
        <p>Isolated Worktree: <code>{escaped_worktree}</code></p>
        <p>Configured MCP Servers: <code>{html.escape(mcp_names)}</code></p>
        <p>Target Application: <code>{escaped_subpath}</code> (<code>{escaped_entry}</code>)</p>
      </div>
    """
  else:
    body_section = f"""
      <div class="card">
        <h2>Custom Chat — Dedicated Agent (<code>{agent_window_ref}</code>)</h2>
        <div id="chat-log" class="chat-log">
          <div class="msg agent">Axoloctl Agent [<code>{escaped_tag}</code>] ready in worktree <code>{branch_ref}</code>. Describe the fleet workflow or filter view you need.</div>
        </div>
        <form id="chat-form" class="chat-form">
          <input type="text" id="chat-input" placeholder="Ask agent [{escaped_tag}] to filter devices or update the view..." required />
          <button type="submit">Send</button>
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
      padding: 16px;
      background: #0f172a;
      color: #e2e8f0;
    }}
    h1 {{ font-size: 16px; margin: 0 0 12px 0; color: #38bdf8; }}
    h2 {{ font-size: 14px; margin: 0 0 8px 0; color: #f8fafc; }}
    .card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 6px;
      padding: 12px;
      margin-bottom: 12px;
    }}
    code {{
      background: #0f172a;
      padding: 2px 5px;
      border-radius: 4px;
      color: #7dd3fc;
      font-size: 12px;
    }}
    .terminal {{
      background: #020617;
      color: #4ade80;
      padding: 10px;
      border-radius: 4px;
      font-size: 12px;
      overflow-x: auto;
    }}
    .session-item {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 0;
      border-bottom: 1px solid #334155;
      font-size: 13px;
    }}
    .session-item:last-child {{ border-bottom: none; }}
    a {{ color: #38bdf8; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .chat-log {{
      background: #020617;
      border: 1px solid #334155;
      border-radius: 4px;
      height: 180px;
      overflow-y: auto;
      padding: 8px;
      margin-bottom: 8px;
      font-size: 13px;
    }}
    .msg {{ margin-bottom: 6px; padding: 6px 8px; border-radius: 4px; }}
    .msg.agent {{ background: #1e293b; color: #e2e8f0; }}
    .msg.user {{ background: #0369a1; color: #ffffff; text-align: right; }}
    .chat-form {{ display: flex; gap: 6px; }}
    .chat-form input {{
      flex: 1;
      background: #020617;
      border: 1px solid #475569;
      color: #f8fafc;
      padding: 6px 8px;
      border-radius: 4px;
    }}
    .chat-form button {{
      background: #0284c7;
      color: white;
      border: none;
      padding: 6px 12px;
      border-radius: 4px;
      cursor: pointer;
    }}
  </style>
</head>
<body>
  <h1>{escaped_label}</h1>
  {body_section}
  <div class="card">
    <h2>Active 1:1 Agent &amp; Web Server Sessions (Gateway :{config.host_port})</h2>
    <div id="sessions-list">Loading active sessions...</div>
  </div>
  <script>
    const WEBMCP_URL = "/api/status";
    const activeTagParam = new URLSearchParams(window.location.search).get("tag") || "";
    async function refreshSessions() {{
      const container = document.getElementById("sessions-list");
      try {{
        const resp = await fetch(WEBMCP_URL);
        const data = await resp.json();
        const entries = Object.entries(data.sessions || {{}});
        if (entries.length === 0) {{
          container.innerHTML = "<em>No active web server sessions.</em>";
          return;
        }}
        const portPart = window.location.port ? `:${{window.location.port}}` : "";
        container.innerHTML = entries.map(([tag, info]) => {{
          const isCorrelated = activeTagParam && activeTagParam === tag;
          const badge = isCorrelated
            ? `<span style="background:#065f46;color:#6ee7b7;border:1px solid #059669;padding:1px 6px;border-radius:4px;font-size:11px;margin-left:6px;">ACTIVE VIEWER</span>`
            : "";
          const agentBadge = info.agent_running
            ? `<span style="background:#1e3a8a;color:#93c5fd;border:1px solid #2563eb;padding:1px 6px;border-radius:4px;font-size:11px;margin-left:6px;">agent:${{tag}}</span>`
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
    refreshSessions();
    setInterval(refreshSessions, 3000);

    const chatForm = document.getElementById("chat-form");
    if (chatForm) {{
      chatForm.addEventListener("submit", (ev) => {{
        ev.preventDefault();
        const input = document.getElementById("chat-input");
        const log = document.getElementById("chat-log");
        const text = input.value.trim();
        if (!text) return;
        const userDiv = document.createElement("div");
        userDiv.className = "msg user";
        userDiv.textContent = text;
        log.appendChild(userDiv);
        input.value = "";
        log.scrollTop = log.scrollHeight;
      }});
    }}
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

    if parsed_path in ("/api/status", "/status"):
      self._proxy_webmcp_get("/status")
      return

    if parsed_path in ("/api/resolve", "/resolve"):
      qs = f"?{parsed.query}" if parsed.query else ""
      self._proxy_webmcp_get(f"/resolve{qs}")
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
      body = json.dumps(uis_payload).encode("utf-8")
      self.send_response(200)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()
      self.wfile.write(body)
      return

    for item in cfg.uis:
      if parsed_path == item.path:
        query_params = parse_qs(parsed.query)
        active_tag = (query_params.get("tag") or [""])[0].strip()
        body = _render_ui_page(cfg, item.id, item.label, active_tag=active_tag)
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
