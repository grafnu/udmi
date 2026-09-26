#!/usr/bin/env python3
import json
import os
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

HOST_PORT = int(os.environ.get("AXOLOCTL_HOST_PORT", "9290"))
UDMI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

class UIHostHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/uis":
            uis = {
                "default_ui": "cliView",
                "uis": [
                    { "id": "cliView", "label": "CLI Console", "url": f"http://localhost:{HOST_PORT}/ui/cli" },
                    { "id": "hubView", "label": "Web Hub", "url": f"http://localhost:{HOST_PORT}/ui/hub" },
                    { "id": "apiView", "label": "Custom Chat (Agent API)", "url": f"http://localhost:{HOST_PORT}/ui/api" }
                ]
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(uis).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/mcp/"):
            # Format: /mcp/<server_name>/<tool_name>
            parts = self.path.split("/")
            if len(parts) >= 4:
                server_name = parts[2]
                tool_name = parts[3]
                
                content_length = int(self.headers.get('Content-Length', 0))
                post_data = self.rfile.read(content_length)
                try:
                    args = json.loads(post_data.decode('utf-8'))
                except json.JSONDecodeError:
                    args = {}

                # Create MCP JSON-RPC payload
                mcp_req = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": args
                    }
                }
                
                # Exec bin/mcp_<server_name> (e.g. butler, barbican)
                bin_path = os.path.join(UDMI_ROOT, "bin", f"mcp_{server_name}")
                if not os.path.exists(bin_path):
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error": "MCP server not found"}')
                    return
                
                # For simplicity, we spawn the process per request right now.
                # In a high-throughput scenario, this should use persistent child processes.
                try:
                    proc = subprocess.run(
                        [bin_path],
                        input=json.dumps(mcp_req).encode('utf-8') + b'\n',
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=15
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    
                    # Return the exact JSON-RPC response from the child
                    # Assuming the child emits exactly one JSON line on stdout
                    out_lines = proc.stdout.decode('utf-8').strip().splitlines()
                    if out_lines:
                        self.wfile.write(out_lines[-1].encode('utf-8'))
                    else:
                        self.wfile.write(b'{"error": "Empty response from MCP child"}')
                        
                except Exception as e:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
            else:
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    server = HTTPServer(('127.0.0.1', HOST_PORT), UIHostHandler)
    print(f"ui_host listening on port {HOST_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
