"""Session lifecycle manager for Axoloctl git-backed web servers."""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from config import AxoloctlConfig


class SessionManager:
  """Manages isolated git-archived web server sessions inside tmux."""

  def __init__(self, udmi_root: str, config: AxoloctlConfig):
    if config is None:
      raise ValueError("AxoloctlConfig instance is required by SessionManager.")
    self.udmi_root = os.path.abspath(udmi_root)
    self.config = config
    self.axoloctl_root = os.path.join(self.udmi_root, "var", "axoloctl")
    self.sessions_dir = os.path.join(self.axoloctl_root, "sessions")
    self.shared_dir = os.path.join(self.axoloctl_root, "shared")
    self.ports_file = os.path.join(self.sessions_dir, "ports.json")
    self.session_web = "udmi_axoloctl_web"

    os.makedirs(self.sessions_dir, exist_ok=True)
    os.makedirs(self.shared_dir, exist_ok=True)

  def _load_ports(self) -> Dict[str, int]:
    if os.path.exists(self.ports_file):
      try:
        with open(self.ports_file, "r", encoding="utf-8") as f:
          return json.load(f)
      except Exception:
        pass
    return {}

  def _save_ports(self, ports: Dict[str, int]) -> None:
    with open(self.ports_file, "w", encoding="utf-8") as f:
      json.dump(ports, f, indent=2)

  def _allocate_port(self, tag: str) -> int:
    ports = self._load_ports()
    if tag in ports:
      return ports[tag]

    used = set(ports.values())
    port = self.config.session_port_base
    while port in used:
      port += 1
    ports[tag] = port
    self._save_ports(ports)
    return port

  def _is_valid_hash(self, commit_hash: str) -> bool:
    return bool(commit_hash and re.match(r"^[0-9a-f]{40}$", commit_hash))

  def _is_valid_tag(self, tag: str) -> bool:
    return bool(tag and re.match(r"^[a-zA-Z0-9_-]+$", tag))

  def _ensure_known_tag(self, tag: str) -> str:
    if not self._is_valid_tag(tag):
      raise ValueError(f"Invalid session tag: '{tag}'")
    session_root = os.path.join(self.sessions_dir, tag)
    commit_path = os.path.join(session_root, "commit.txt")
    if not os.path.isdir(session_root) or not os.path.isfile(commit_path):
      raise ValueError(f"Unknown session tag: '{tag}'")
    return session_root

  def _probe_http(self, port: int) -> bool:
    try:
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
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if res.returncode != 0:
      return False
    return tag in [line.strip() for line in res.stdout.splitlines()]

  def list_servers(self) -> List[Dict[str, str]]:
    ports = self._load_ports()
    servers = []
    res = subprocess.run(
        ["tmux", "list-windows", "-t", self.session_web, "-F", "#{window_name}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    active_tags = (
        set(line.strip() for line in res.stdout.splitlines())
        if res.returncode == 0
        else set()
    )

    for tag in ports:
      if tag in active_tags:
        desc_path = os.path.join(self.sessions_dir, tag, "description.txt")
        desc = ""
        if os.path.exists(desc_path):
          with open(desc_path, "r", encoding="utf-8") as f:
            desc = f.read().strip()
        servers.append({"tag": tag, "description": desc})
    return servers

  def stop_server(self, tag: str) -> Dict[str, Any]:
    self._ensure_known_tag(tag)
    if not self.is_running(tag):
      raise ValueError(f"Session tag '{tag}' is not currently running.")

    subprocess.run(
        ["tmux", "kill-window", "-t", f"{self.session_web}:{tag}"],
        check=False,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    return {
        "running": False,
        "exit_code": 0,
        "logs": self.read_logs(tag, cursor=0, max_lines=100)["lines"][-5:],
    }

  def start_server(
      self, tag: str, commit_hash: str, description: str
  ) -> Dict[str, Any]:
    if not self._is_valid_tag(tag):
      raise ValueError(
          f"Invalid session tag '{tag}'. Use alphanumeric characters, '-' or '_'."
      )
    if not self._is_valid_hash(commit_hash):
      raise ValueError(
          f"Invalid commit hash '{commit_hash}'. Expected 40-character hex SHA."
      )

    # Verify commit exists in configured repository before touching session state
    verify_res = subprocess.run(
        [
            "git",
            "-c",
            "safe.bareRepository=all",
            "-C",
            self.config.repo_path,
            "cat-file",
            "-t",
            commit_hash,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if verify_res.returncode != 0 or verify_res.stdout.strip() != "commit":
      raise ValueError(
          f"Commit {commit_hash} not found in repository {self.config.repo_path}."
      )

    if self.is_running(tag):
      self.stop_server(tag)

    session_root = os.path.join(self.sessions_dir, tag)
    code_dir = os.path.join(session_root, "code")
    log_file = os.path.join(session_root, "unified.log")
    shared_dir = os.path.join(self.shared_dir, tag)

    os.makedirs(session_root, exist_ok=True)
    os.makedirs(shared_dir, exist_ok=True)
    with open(
        os.path.join(session_root, "description.txt"), "w", encoding="utf-8"
    ) as f:
      f.write(description or "")
    with open(
        os.path.join(session_root, "commit.txt"), "w", encoding="utf-8"
    ) as f:
      f.write(commit_hash)

    # Clear existing read-only code dir
    if os.path.exists(code_dir):
      subprocess.run(["chmod", "-R", "+w", code_dir], check=False)
      shutil.rmtree(code_dir, ignore_errors=True)

    os.makedirs(code_dir, exist_ok=True)

    # Archive the specific commit from configured repo into code_dir
    res = subprocess.run(
        f"git -c safe.bareRepository=all -C '{self.config.repo_path}' "
        f"archive '{commit_hash}' | tar -x -C '{code_dir}'",
        shell=True,
        stderr=subprocess.PIPE,
        check=False,
    )
    if res.returncode != 0:
      raise ValueError(
          f"Failed to checkout commit {commit_hash}: "
          f"{res.stderr.decode('utf-8')}"
      )

    app_code_dir = os.path.normpath(
        os.path.join(code_dir, self.config.app_subpath)
    )
    if not os.path.isdir(app_code_dir):
      raise ValueError(
          f"Configured app_subpath '{self.config.app_subpath}' not found in "
          f"checked-out commit {commit_hash}."
      )

    entrypoint_path = os.path.normpath(
        os.path.join(app_code_dir, self.config.entrypoint)
    )
    if not os.path.isfile(entrypoint_path) or not os.access(
        entrypoint_path, os.X_OK
    ):
      raise ValueError(
          f"Configured entrypoint '{self.config.entrypoint}' not found or not "
          f"executable inside '{self.config.app_subpath}' at commit {commit_hash}."
      )

    # Make code tree read-only, with code/var linked to the mutable shared_dir
    subprocess.run(["chmod", "-R", "a-w", code_dir], check=True)
    var_link = os.path.join(code_dir, "var")
    if not os.path.exists(var_link):
      os.chmod(code_dir, 0o755)
      os.symlink(shared_dir, var_link)
      os.chmod(code_dir, 0o555)

    port = self._allocate_port(tag)
    url = f"http://127.0.0.1:{port}"
    venv_activate = os.path.join(self.udmi_root, "venv", "bin", "activate")
    extra_port_export = (
        f"export {self.config.port_env_var}={port}\n"
        if self.config.port_env_var
        else ""
    )

    wrapper_script = os.path.join(session_root, "runner.sh")
    with open(wrapper_script, "w", encoding="utf-8") as f:
      f.write(
          f"""#!/bin/bash
if [ -f "{venv_activate}" ]; then
  source "{venv_activate}"
fi
export AXOLOCTL_PORT={port}
{extra_port_export}export AXOLOCTL_DATA_DIR="{shared_dir}"
export AXOLOCTL_TAG="{tag}"
export AXOLOCTL_COMMIT="{commit_hash}"
export AXOLOCTL_MCP_PROXY_URL="http://127.0.0.1:{self.config.host_port}"
export PYTHONUNBUFFERED=1
cd "{app_code_dir}"
exec "./{self.config.entrypoint}" 2>&1 | while IFS= read -r line; do
  echo "[server] $line" | tee -a "{log_file}"
done
"""
      )
    os.chmod(wrapper_script, 0o755)

    open(log_file, "w", encoding="utf-8").close()

    subprocess.run(
        [
            "tmux",
            "new-window",
            "-d",
            "-t",
            self.session_web,
            "-n",
            tag,
            wrapper_script,
        ],
        check=True,
    )

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
          "logs": logs,
      }

    return {
        "running": True,
        "url": url,
        "cursor": len(logs),
        "logs": logs,
    }

  def get_status(self, tag: str) -> Dict[str, Any]:
    session_root = self._ensure_known_tag(tag)
    commit_path = os.path.join(session_root, "commit.txt")
    with open(commit_path, "r", encoding="utf-8") as f:
      commit_hash = f.read().strip()

    running = self.is_running(tag)
    return {
        "running": running,
        "commit_hash": commit_hash,
        "exit_code": None if running else 0,
    }

  def read_logs(
      self, tag: str, cursor: int = 0, max_lines: int = 200
  ) -> Dict[str, Any]:
    session_root = self._ensure_known_tag(tag)
    log_file = os.path.join(session_root, "unified.log")
    lines = []
    if os.path.exists(log_file):
      with open(log_file, "r", encoding="utf-8") as f:
        all_lines = [line.strip() for line in f.readlines()]
        lines = all_lines[cursor : cursor + max_lines]

    return {
        "running": self.is_running(tag),
        "lines": lines,
        "next_cursor": cursor + len(lines),
    }

  def append_browser_log(self, tag: str, message: str) -> None:
    session_root = os.path.join(self.sessions_dir, tag)
    log_file = os.path.join(session_root, "unified.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
      f.write(f"[browser] {message}\n")
