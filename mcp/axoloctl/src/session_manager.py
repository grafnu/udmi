import json
import os
import re
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional

class SessionManager:
    def __init__(self, udmi_root: str):
        self.udmi_root = os.path.abspath(udmi_root)
        self.axoloctl_root = os.path.join(self.udmi_root, "var", "axoloctl")
        self.sessions_dir = os.path.join(self.axoloctl_root, "sessions")
        self.shared_dir = os.path.join(self.axoloctl_root, "shared")
        self.repo_dir = os.path.join(self.axoloctl_root, "repo.git")
        self.ports_file = os.path.join(self.sessions_dir, "ports.json")
        self.session_web = "udmi_axoloctl_web"
        
        os.makedirs(self.sessions_dir, exist_ok=True)
        os.makedirs(self.shared_dir, exist_ok=True)

    def _load_ports(self) -> Dict[str, int]:
        if os.path.exists(self.ports_file):
            try:
                with open(self.ports_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_ports(self, ports: Dict[str, int]):
        with open(self.ports_file, "w") as f:
            json.dump(ports, f, indent=2)

    def _allocate_port(self, tag: str) -> int:
        ports = self._load_ports()
        if tag in ports:
            return ports[tag]
        
        used = set(ports.values())
        port = 9300
        while port in used:
            port += 1
        ports[tag] = port
        self._save_ports(ports)
        return port

    def _is_valid_hash(self, commit_hash: str) -> bool:
        return bool(re.match(r"^[0-9a-f]{40}$", commit_hash))

    def _probe_http(self, port: int) -> bool:
        try:
            import urllib.request
            # Succeeds if < 500
            req = urllib.request.Request(f"http://127.0.0.1:{port}/", method="GET")
            with urllib.request.urlopen(req, timeout=1.0) as response:
                return response.status < 500
        except urllib.error.HTTPError as e:
            return e.code < 500
        except Exception:
            return False

    def is_running(self, tag: str) -> bool:
        res = subprocess.run(
            ["tmux", "list-windows", "-t", self.session_web, "-F", "#{window_name}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        if res.returncode != 0:
            return False
        return tag in [line.strip() for line in res.stdout.splitlines()]

    def list_servers(self) -> List[Dict[str, str]]:
        ports = self._load_ports()
        servers = []
        res = subprocess.run(
            ["tmux", "list-windows", "-t", self.session_web, "-F", "#{window_name}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        active_tags = set(line.strip() for line in res.stdout.splitlines()) if res.returncode == 0 else set()
        
        for tag, port in ports.items():
            if tag in active_tags:
                desc_path = os.path.join(self.sessions_dir, tag, "description.txt")
                desc = ""
                if os.path.exists(desc_path):
                    with open(desc_path, "r") as f:
                        desc = f.read().strip()
                servers.append({"tag": tag, "description": desc})
        return servers

    def stop_server(self, tag: str) -> Dict[str, Any]:
        if not self.is_running(tag):
            raise ValueError(f"Session tag '{tag}' is not currently running.")

        subprocess.run(["tmux", "kill-window", "-t", f"{self.session_web}:{tag}"], check=False, stderr=subprocess.DEVNULL)
        time.sleep(0.5)

        return {
            "running": False,
            "exit_code": 0,
            "logs": self.read_logs(tag, cursor=0, max_lines=100)["lines"][-5:]
        }

    def start_server(self, tag: str, commit_hash: str, description: str) -> Dict[str, Any]:
        if not self._is_valid_hash(commit_hash):
            raise ValueError(f"Invalid commit hash: {commit_hash}")

        if self.is_running(tag):
            self.stop_server(tag)

        session_root = os.path.join(self.sessions_dir, tag)
        code_dir = os.path.join(session_root, "code")
        log_file = os.path.join(session_root, "unified.log")
        shared_dir = os.path.join(self.shared_dir, tag)

        os.makedirs(session_root, exist_ok=True)
        os.makedirs(shared_dir, exist_ok=True)
        with open(os.path.join(session_root, "description.txt"), "w") as f:
            f.write(description)
        with open(os.path.join(session_root, "commit.txt"), "w") as f:
            f.write(commit_hash)

        # Clear existing read-only code dir
        if os.path.exists(code_dir):
            subprocess.run(["chmod", "-R", "+w", code_dir], check=False)
            shutil.rmtree(code_dir, ignore_errors=True)

        os.makedirs(code_dir, exist_ok=True)

        # Archive the specific commit from bare repo into code_dir
        res = subprocess.run(
            f"git -c safe.bareRepository=all -C '{self.repo_dir}' archive '{commit_hash}' | tar -x -C '{code_dir}'",
            shell=True, stderr=subprocess.PIPE
        )
        if res.returncode != 0:
            raise ValueError(f"Failed to checkout commit {commit_hash}: {res.stderr.decode('utf-8')}")

        if not os.path.isfile(os.path.join(code_dir, "bin", "serve")):
            raise ValueError("Canonical entrypoint bin/serve not found in commit.")

        # Make read-only
        subprocess.run(["chmod", "-R", "a-w", code_dir])

        port = self._allocate_port(tag)
        url = f"http://127.0.0.1:{port}"

        # Setup logging via a wrapper script
        # The wrapper script will prefix lines with [server] and append to unified.log
        wrapper_script = os.path.join(session_root, "runner.sh")
        with open(wrapper_script, "w") as f:
            f.write(f"""#!/bin/bash
export AXOLOCTL_PORT={port}
export GUMMI_PORT={port}
export AXOLOCTL_DATA_DIR="{shared_dir}"
export AXOLOCTL_TAG="{tag}"
export AXOLOCTL_COMMIT="{commit_hash}"
export AXOLOCTL_MCP_PROXY_URL="http://127.0.0.1:${{AXOLOCTL_HOST_PORT:-9290}}"
export PYTHONUNBUFFERED=1
cd "{code_dir}"
exec ./bin/serve 2>&1 | while read -r line; do
  echo "[server] $line" | tee -a "{log_file}"
done
""")
        os.chmod(wrapper_script, 0o755)

        # Clear log
        open(log_file, 'w').close()

        # Start in tmux
        subprocess.run([
            "tmux", "new-window", "-d", "-t", self.session_web, "-n", tag, wrapper_script
        ])

        # Wait for readiness
        timeout = 10
        start_time = time.time()
        running = False
        while time.time() - start_time < timeout:
            if not self.is_running(tag):
                break
            if self._probe_http(port):
                running = True
                break
            time.sleep(0.5)

        logs = self.read_logs(tag)["lines"]
        if not running:
            if self.is_running(tag):
                self.stop_server(tag)
            return {
                "running": False,
                "url": None,
                "cursor": len(logs),
                "logs": logs
            }

        return {
            "running": True,
            "url": url,
            "cursor": len(logs),
            "logs": logs
        }

    def get_status(self, tag: str) -> Dict[str, Any]:
        commit_path = os.path.join(self.sessions_dir, tag, "commit.txt")
        commit_hash = ""
        if os.path.exists(commit_path):
            with open(commit_path, "r") as f:
                commit_hash = f.read().strip()
        
        running = self.is_running(tag)
        return {
            "running": running,
            "commit_hash": commit_hash,
            "exit_code": None if running else 0
        }

    def read_logs(self, tag: str, cursor: int = 0, max_lines: int = 200) -> Dict[str, Any]:
        log_file = os.path.join(self.sessions_dir, tag, "unified.log")
        lines = []
        if os.path.exists(log_file):
            with open(log_file, "r") as f:
                all_lines = [line.strip() for line in f.readlines()]
                lines = all_lines[cursor : cursor + max_lines]
        
        return {
            "running": self.is_running(tag),
            "lines": lines,
            "next_cursor": cursor + len(lines)
        }

    def append_browser_log(self, tag: str, message: str):
        log_file = os.path.join(self.sessions_dir, tag, "unified.log")
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, "a") as f:
            f.write(f"[browser] {message}\n")
