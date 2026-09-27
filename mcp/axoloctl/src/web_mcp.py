#!/usr/bin/env python3
"""Axoloctl Web MCP Daemon & Session Lifecycle Server."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import sys
from typing import Any, Dict, Optional

# Ensure local src directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import AxoloctlConfig, load_config
from session_manager import SessionManager

UDMI_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)

MCP_TOOLS = [
    {
        "name": "start_server",
        "description": (
            "Deploys the specified 40-character Git commit hash for the given "
            "session tag and starts the configured web server entrypoint."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "Unique identifier for the web server session (e.g. 'gummi', 'ui-dev').",
                },
                "commit_hash": {
                    "type": "string",
                    "description": "40-character hexadecimal Git commit SHA to deploy.",
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable description of the web server session.",
                },
            },
            "required": ["tag", "commit_hash", "description"],
        },
    },
    {
        "name": "stop_server",
        "description": "Stops the running web server session identified by tag.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "Session identifier of the web server to stop.",
                },
            },
            "required": ["tag"],
        },
    },
    {
        "name": "get_status",
        "description": (
            "Returns the current lifecycle state and deployed commit_hash of "
            "the web server session identified by tag."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "Session identifier of the web server to query.",
                },
            },
            "required": ["tag"],
        },
    },
    {
        "name": "list_servers",
        "description": "Lists all currently active web server sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "read_logs",
        "description": (
            "Streams captured unified [server] and [browser] log lines for "
            "the session identified by tag starting from cursor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "Session identifier of the web server whose logs are being read.",
                },
                "cursor": {
                    "type": "integer",
                    "description": "Zero-based line offset cursor (default: 0).",
                    "default": 0,
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of log lines to return (default: 200).",
                    "default": 200,
                },
            },
            "required": ["tag"],
        },
    },
]


class WebMCPServer:
  """Core MCP JSON-RPC 2.0 dispatcher for Axoloctl."""

  def __init__(self, session_mgr: SessionManager):
    self.session_mgr = session_mgr

  def dispatch_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
    if tool_name == "start_server":
      for req_key in ("tag", "commit_hash", "description"):
        if req_key not in args or args[req_key] is None:
          raise ValueError(f"Missing required argument '{req_key}' for start_server.")
      return self.session_mgr.start_server(
          str(args["tag"]),
          str(args["commit_hash"]),
          str(args["description"]),
      )
    if tool_name == "stop_server":
      if "tag" not in args or not args["tag"]:
        raise ValueError("Missing required argument 'tag' for stop_server.")
      return self.session_mgr.stop_server(str(args["tag"]))
    if tool_name == "get_status":
      if "tag" not in args or not args["tag"]:
        raise ValueError("Missing required argument 'tag' for get_status.")
      return self.session_mgr.get_status(str(args["tag"]))
    if tool_name == "list_servers":
      return {"servers": self.session_mgr.list_servers()}
    if tool_name == "read_logs":
      if "tag" not in args or not args["tag"]:
        raise ValueError("Missing required argument 'tag' for read_logs.")
      return self.session_mgr.read_logs(
          str(args["tag"]),
          int(args.get("cursor", 0)),
          int(args.get("max_lines", 200)),
      )
    raise KeyError(f"Tool '{tool_name}' not found")

  def handle_jsonrpc(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if not method:
      return {
          "jsonrpc": "2.0",
          "id": req_id,
          "error": {"code": -32600, "message": "Invalid Request: missing method"},
      }

    if method == "initialize":
      return {
          "jsonrpc": "2.0",
          "id": req_id,
          "result": {
              "protocolVersion": "2024-11-05",
              "capabilities": {"tools": {"listChanged": False}},
              "serverInfo": {
                  "name": "udmi-axoloctl",
                  "version": "1.0.0",
              },
          },
      }

    if method in ("notifications/initialized", "initialized"):
      return None

    if method == "ping":
      return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
      return {
          "jsonrpc": "2.0",
          "id": req_id,
          "result": {"tools": MCP_TOOLS},
      }

    if method == "tools/call":
      tool_name = params.get("name")
      args = params.get("arguments") or {}
      try:
        result = self.dispatch_tool(tool_name, args)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": False,
            },
        }
      except KeyError as e:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": str(e)},
        }
      except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32603, "message": str(e)},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


class WebMCPHandler(BaseHTTPRequestHandler):
  """HTTP handler for Web MCP JSON-RPC, /status, and /telemetry."""

  mcp_server: Optional[WebMCPServer] = None

  def do_OPTIONS(self) -> None:
    self.send_response(204)
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    self.send_header("Access-Control-Allow-Headers", "Content-Type")
    self.end_headers()

  def do_GET(self) -> None:
    session_mgr = self.mcp_server.session_mgr
    if self.path == "/status":
      ports = session_mgr._load_ports()
      sessions = {}
      for tag, port in ports.items():
        if session_mgr.is_running(tag):
          commit_path = os.path.join(session_mgr.sessions_dir, tag, "commit.txt")
          commit_hash = ""
          if os.path.exists(commit_path):
            with open(commit_path, "r", encoding="utf-8") as f:
              commit_hash = f.read().strip()
          sessions[tag] = {
              "port": port,
              "commit": commit_hash,
              "url": f"http://127.0.0.1:{port}",
          }
      self._send_response({"sessions": sessions})
    else:
      self.send_response(404)
      self.end_headers()

  def do_POST(self) -> None:
    session_mgr = self.mcp_server.session_mgr
    content_length = int(self.headers.get("Content-Length", 0))
    post_data = self.rfile.read(content_length)

    try:
      req = json.loads(post_data.decode("utf-8"))
    except json.JSONDecodeError:
      self._send_response(
          {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}
      )
      return

    if self.path == "/telemetry":
      port = req.get("port")
      msg = req.get("message", "")
      target_tag = None
      ports = session_mgr._load_ports()
      for tag, p in ports.items():
        if p == port and session_mgr.is_running(tag):
          target_tag = tag
          break

      if target_tag:
        session_mgr.append_browser_log(target_tag, msg)

      self.send_response(200)
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()
      return

    resp = self.mcp_server.handle_jsonrpc(req)
    if resp is None:
      self.send_response(204)
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()
      return
    self._send_response(resp)

  def _send_response(self, resp_dict: Dict[str, Any]) -> None:
    payload = json.dumps(resp_dict).encode("utf-8")
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(payload)))
    self.send_header("Access-Control-Allow-Origin", "*")
    self.end_headers()
    self.wfile.write(payload)

  def log_message(self, fmt: str, *args: Any) -> None:
    pass


def main() -> None:
  parser = argparse.ArgumentParser(description="Axoloctl Web MCP Daemon")
  parser.add_argument(
      "--config",
      default=os.environ.get("AXOLOCTL_CONFIG", ""),
      help="Path to the explicit Axoloctl JSON configuration file",
  )
  args = parser.parse_args()

  config: AxoloctlConfig = load_config(args.config, UDMI_ROOT)
  session_mgr = SessionManager(UDMI_ROOT, config)
  WebMCPHandler.mcp_server = WebMCPServer(session_mgr)

  server = HTTPServer(("127.0.0.1", config.webmcp_port), WebMCPHandler)
  server.allow_reuse_address = True
  print(
      f"web_mcp daemon listening on http://127.0.0.1:{config.webmcp_port} "
      f"(repo={config.repo_path}, subpath={config.app_subpath}, "
      f"entrypoint={config.entrypoint})"
  )
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    server.server_close()


if __name__ == "__main__":
  main()
