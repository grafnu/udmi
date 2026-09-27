#!/usr/bin/env python3
"""Axoloctl UI Host Gateway & HTTP-to-MCP Proxy."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import html
import json
import os
import subprocess
import sys
from typing import Any, Dict, Optional
from urllib.parse import urlparse

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import AxoloctlConfig, load_config

UDMI_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)


def _render_ui_page(
    config: AxoloctlConfig, ui_id: str, ui_label: str
) -> bytes:
  """Renders a self-contained HTML page for the requested host UI endpoint."""
  escaped_label = html.escape(ui_label)
  escaped_repo = html.escape(config.repo_path)
  escaped_subpath = html.escape(config.app_subpath)
  escaped_entry = html.escape(config.entrypoint)
  mcp_names = ", ".join(sorted(config.mcp_servers.keys()))

  body_section = ""
  if ui_id == "cliView":
    body_section = f"""
      <div class="card">
        <h2>Agent CLI Console (<code>udmi_axoloctl_agent:agent</code>)</h2>
        <p>Working Directory: <code>{escaped_repo}/{escaped_subpath}</code></p>
        <p>Entrypoint: <code>{escaped_entry}</code></p>
        <pre class="terminal" id="cli-output">$ cd {escaped_repo}/{escaped_subpath}
$ # Attach in terminal: bin/tmux_axoloctl attach
[axoloctl-agent] Ready. Connected MCPs: {html.escape(mcp_names)}</pre>
      </div>
    """
  elif ui_id == "hubView":
    body_section = f"""
      <div class="card">
        <h2>Axoloctl Agent Web Hub</h2>
        <p>Configured MCP Servers: <code>{html.escape(mcp_names)}</code></p>
        <p>Target Application: <code>{escaped_subpath}</code> (<code>{escaped_entry}</code>)</p>
      </div>
    """
  else:
    body_section = """
      <div class="card">
        <h2>Custom Chat (Agent API)</h2>
        <div id="chat-log" class="chat-log">
          <div class="msg agent">Axoloctl Agent ready. Describe the fleet workflow or filter view you need.</div>
        </div>
        <form id="chat-form" class="chat-form">
          <input type="text" id="chat-input" placeholder="Ask the agent to filter devices or update the tabular view..." required />
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
    <h2>Active Managed Web Sessions (Web MCP :{config.webmcp_port})</h2>
    <div id="sessions-list">Loading active sessions...</div>
  </div>
  <script>
    const WEBMCP_URL = "http://127.0.0.1:{config.webmcp_port}/status";
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
        container.innerHTML = entries.map(([tag, info]) =>
          `<div class="session-item">
            <span><strong>${{tag}}</strong> (<code>${{info.commit.slice(0, 8)}}</code>)</span>
            <a href="http://127.0.0.1:${{info.port}}" target="_blank">http://127.0.0.1:${{info.port}}</a>
          </div>`
        ).join("");
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
  """HTTP request handler for UI Discovery, Host UIs, and MCP Proxy."""

  config: Optional[AxoloctlConfig] = None

  def do_OPTIONS(self) -> None:
    self.send_response(204)
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    self.send_header("Access-Control-Allow-Headers", "Content-Type")
    self.end_headers()

  def do_GET(self) -> None:
    cfg = self.config
    parsed_path = urlparse(self.path).path

    if parsed_path == "/api/uis":
      uis_payload = {
          "default_ui": cfg.default_ui,
          "uis": [
              {
                  "id": item.id,
                  "label": item.label,
                  "url": f"http://localhost:{cfg.host_port}{item.path}",
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
        body = _render_ui_page(cfg, item.id, item.label)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        return

    self.send_response(404)
    self.send_header("Access-Control-Allow-Origin", "*")
    self.end_headers()

  def do_POST(self) -> None:
    cfg = self.config
    parsed_path = urlparse(self.path).path

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

  server = HTTPServer(("127.0.0.1", config.host_port), UIHostHandler)
  server.allow_reuse_address = True
  print(
      f"ui_host listening on http://127.0.0.1:{config.host_port} "
      f"(mcp_config={config.mcp_config_path})"
  )
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    server.server_close()


if __name__ == "__main__":
  main()
