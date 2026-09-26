#!/usr/bin/env python3
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from session_manager import SessionManager

WEBMCP_PORT = int(os.environ.get("AXOLOCTL_WEBMCP_PORT", "9291"))
UDMI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

session_mgr = SessionManager(UDMI_ROOT)

class WebMCPHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            ports = session_mgr._load_ports()
            sessions = {}
            for tag, port in ports.items():
                if session_mgr.is_running(tag):
                    commit_path = os.path.join(session_mgr.sessions_dir, tag, "commit.txt")
                    commit_hash = ""
                    if os.path.exists(commit_path):
                        with open(commit_path, "r") as f:
                            commit_hash = f.read().strip()
                    sessions[tag] = {
                        "port": port,
                        "commit": commit_hash
                    }
            
            self._send_response({"sessions": sessions})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            req = json.loads(post_data.decode('utf-8'))
        except json.JSONDecodeError:
            self._send_response({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}})
            return

        if self.path == "/telemetry":
            port = req.get("port")
            msg = req.get("message", "")
            
            # Find which tag corresponds to this port
            target_tag = None
            ports = session_mgr._load_ports()
            for tag, p in ports.items():
                if p == port and session_mgr.is_running(tag):
                    target_tag = tag
                    break
            
            if target_tag:
                session_mgr.append_browser_log(target_tag, msg)
            
            self.send_response(200)
            self.end_headers()
            return

        method = req.get("method")
        params = req.get("params", {})
        req_id = req.get("id")

        if method != "tools/call":
            self._send_response({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})
            return

        tool_name = params.get("name")
        args = params.get("arguments", {})

        try:
            if tool_name == "start_server":
                result = session_mgr.start_server(
                    args.get("tag"), 
                    args.get("commit_hash"), 
                    args.get("description", "")
                )
            elif tool_name == "stop_server":
                result = session_mgr.stop_server(args.get("tag"))
            elif tool_name == "get_status":
                result = session_mgr.get_status(args.get("tag"))
            elif tool_name == "list_servers":
                result = {"servers": session_mgr.list_servers()}
            elif tool_name == "read_logs":
                result = session_mgr.read_logs(
                    args.get("tag"),
                    args.get("cursor", 0),
                    args.get("max_lines", 200)
                )
            else:
                self._send_response({
                    "jsonrpc": "2.0", 
                    "id": req_id, 
                    "error": {"code": -32601, "message": f"Tool {tool_name} not found"}
                })
                return
            
            # Format as MCP response
            self._send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False
                }
            })
            
        except Exception as e:
            self._send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)}
            })

    def _send_response(self, resp_dict):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(resp_dict).encode('utf-8'))

    def log_message(self, format, *args):
        # Disable default logging to keep tmux console clean
        pass

if __name__ == "__main__":
    server = HTTPServer(('127.0.0.1', WEBMCP_PORT), WebMCPHandler)
    print(f"web_mcp daemon listening on port {WEBMCP_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
