#!/usr/bin/env python3
"""Axoloctl Web MCP Daemon & Session Lifecycle Server."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

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
            "Deploys the specified 40-character Git commit hash for this "
            "agent's paired session and starts the configured web server entrypoint."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "commit_hash": {
                    "type": "string",
                    "description": "40-character hexadecimal Git commit SHA to deploy.",
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable description of the web server deployment.",
                },
            },
            "required": ["commit_hash", "description"],
        },
    },
    {
        "name": "stop_server",
        "description": "Stops this agent's paired web server session.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_status",
        "description": (
            "Returns the current lifecycle state and deployed commit_hash of "
            "this agent's paired web server session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "read_logs",
        "description": (
            "Streams captured unified [server] and [browser] log lines for "
            "this agent's paired web server session starting from cursor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
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
        },
    },
]


class WebMCPServer:
  """Core MCP JSON-RPC 2.0 dispatcher for Axoloctl (1:1 Agent-to-Server bound via AXOLOCTL_TAG)."""

  def __init__(self, session_mgr: SessionManager):
    self.session_mgr = session_mgr

  def dispatch_tool(
      self, tool_name: str, args: Dict[str, Any], bound_tag: str = ""
  ) -> Any:
    tag = (bound_tag or os.environ.get("AXOLOCTL_TAG") or "").strip()
    if not tag:
      raise ValueError(
          "Missing required session binding: set AXOLOCTL_TAG or pass X-Axoloctl-Tag header."
      )
    if "tag" in args:
      raise ValueError(
          "Unexpected 'tag' argument: each agent is strictly bound 1:1 to its "
          "paired server via AXOLOCTL_TAG."
      )

    if tool_name == "start_server":
      for req_key in ("commit_hash", "description"):
        if req_key not in args or args[req_key] is None:
          raise ValueError(
              f"Missing required argument '{req_key}' for start_server."
          )
      return self.session_mgr.start_server(
          tag,
          str(args["commit_hash"]),
          str(args["description"]),
      )
    if tool_name == "stop_server":
      return self.session_mgr.stop_server(tag)
    if tool_name == "get_status":
      return self.session_mgr.get_status(tag)
    if tool_name == "read_logs":
      return self.session_mgr.read_logs(
          tag,
          int(args.get("cursor", 0)),
          int(args.get("max_lines", 200)),
      )
    raise KeyError(f"Tool '{tool_name}' not found")

  def handle_jsonrpc(
      self, req: Dict[str, Any], bound_tag: str = ""
  ) -> Optional[Dict[str, Any]]:
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
        result = self.dispatch_tool(tool_name, args, bound_tag=bound_tag)
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
    self.send_header(
        "Access-Control-Allow-Headers", "Content-Type, X-Axoloctl-Tag"
    )
    self.end_headers()

  def do_GET(self) -> None:
    session_mgr = self.mcp_server.session_mgr
    parsed = urlparse(self.path)
    if parsed.path == "/status":
      defined = session_mgr.list_defined_sessions()
      sessions = {}
      for tag, info in defined.items():
        if info.get("running"):
          port = info.get("port")
          ready = bool(session_mgr._probe_http(port)) if port else False
          sessions[tag] = {
              "port": port,
              "running": True,
              "ready": ready,
              "commit": info.get("commit", ""),
              "description": info.get("description", ""),
              "url": info.get("url", ""),
              "agent_running": info.get("agent_running", False),
              "workspace": info.get("workspace", ""),
              "nonces": info.get("nonces", {}),
          }
          info["ready"] = ready
        else:
          info["ready"] = False
      self._send_response({
          "sessions": sessions,
          "defined_sessions": defined,
      })
    elif parsed.path == "/resolve":
      query = parse_qs(parsed.query)
      nonce = (query.get("nonce") or [""])[0].strip()
      if not nonce:
        self._send_response(
            {"error": "Missing required 'nonce' query parameter."},
            status_code=400,
        )
        return
      resolved = session_mgr.resolve_nonce(nonce)
      if resolved is None:
        self._send_response(
            {"resolved": False, "nonce": nonce}, status_code=404
        )
        return
      self._send_response({"resolved": True, **resolved})
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

    parsed = urlparse(self.path)
    if parsed.path == "/sessions":
      tag_arg = str(req.get("tag") or "").strip()
      commit_arg = str(req.get("commit_hash") or req.get("commit") or "").strip()
      desc_arg = str(req.get("description") or "").strip()
      try:
        res = session_mgr.create_or_start_session(
            tag=tag_arg, commit_hash=commit_arg, description=desc_arg
        )
        self._send_response(res, status_code=200)
      except ValueError as e:
        self._send_response({"error": str(e)}, status_code=400)
      except Exception as e:
        self._send_response({"error": str(e)}, status_code=500)
      return

    if parsed.path == "/telemetry":
      nonce = str(req.get("nonce") or "").strip()
      tag_arg = str(req.get("tag") or "").strip()
      port = req.get("port")
      msg = req.get("message", "")
      target_tag = None

      if nonce:
        resolved = session_mgr.resolve_nonce(nonce)
        if resolved:
          target_tag = resolved["tag"]
      if not target_tag and tag_arg and session_mgr.is_running(tag_arg):
        target_tag = tag_arg
      if not target_tag and port is not None:
        ports = session_mgr._load_ports()
        for tag, p in ports.items():
          if p == port and session_mgr.is_running(tag):
            target_tag = tag
            break

      if target_tag and msg:
        session_mgr.append_browser_log(target_tag, msg)

      self._send_response({"ok": bool(target_tag), "tag": target_tag})
      return

    bound_tag = (self.headers.get("X-Axoloctl-Tag") or "").strip()
    resp = self.mcp_server.handle_jsonrpc(req, bound_tag=bound_tag)
    if resp is None:
      self.send_response(204)
      self.send_header("Access-Control-Allow-Origin", "*")
      self.end_headers()
      return
    self._send_response(resp)

  def _send_response(
      self, resp_dict: Dict[str, Any], status_code: int = 200
  ) -> None:
    payload = json.dumps(resp_dict).encode("utf-8")
    self.send_response(status_code)
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

  server = ThreadingHTTPServer(("127.0.0.1", config.webmcp_port), WebMCPHandler)
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
