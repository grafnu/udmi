"""Session lifecycle manager for Axoloctl git-backed web servers."""

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

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
    self.session_agent = "udmi_axoloctl_agent"

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

  def agent_workspace_dir(self, tag: str) -> str:
    workspace_root = os.path.join(self.sessions_dir, tag, "workspace")
    return os.path.normpath(os.path.join(workspace_root, self.config.app_subpath))

  def _ensure_agent_worktree(self, tag: str, commit_hash: str) -> str:
    session_root = os.path.join(self.sessions_dir, tag)
    workspace_root = os.path.join(session_root, "workspace")
    branch_name = f"axoloctl-{tag}"
    git_marker = os.path.join(workspace_root, ".git")

    if not os.path.exists(git_marker):
      if os.path.exists(workspace_root):
        shutil.rmtree(workspace_root, ignore_errors=True)
      subprocess.run(
          [
              "git",
              "-c",
              "safe.bareRepository=all",
              "-C",
              self.config.repo_path,
              "worktree",
              "prune",
          ],
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
          check=False,
      )
      res = subprocess.run(
          [
              "git",
              "-c",
              "safe.bareRepository=all",
              "-C",
              self.config.repo_path,
              "worktree",
              "add",
              "-f",
              "-B",
              branch_name,
              workspace_root,
              commit_hash,
          ],
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          text=True,
          check=False,
      )
      if res.returncode != 0:
        raise ValueError(
            f"Failed to provision git worktree for session '{tag}' at "
            f"{commit_hash}: {res.stderr.strip()}"
        )

    return self.agent_workspace_dir(tag)

  def _ensure_tmux_window(
      self, session_name: str, window_name: str, script_path: str
  ) -> None:
    has_sess = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if has_sess.returncode != 0:
      subprocess.run(
          [
              "tmux",
              "new-session",
              "-d",
              "-s",
              session_name,
              "-n",
              window_name,
              script_path,
          ],
          check=True,
      )
    else:
      subprocess.run(
          [
              "tmux",
              "new-window",
              "-d",
              "-t",
              session_name,
              "-n",
              window_name,
              script_path,
          ],
          check=True,
      )

  def get_conversation_id(self, tag: str) -> str:
    session_root = os.path.join(self.sessions_dir, tag)
    os.makedirs(session_root, exist_ok=True)
    conv_file = os.path.join(session_root, "conversation_id.txt")
    if os.path.exists(conv_file):
      try:
        with open(conv_file, "r", encoding="utf-8") as f:
          cid = f.read().strip()
        if cid:
          return cid
      except Exception:
        pass
    cid = str(uuid.uuid4())
    with open(conv_file, "w", encoding="utf-8") as f:
      f.write(cid)
    return cid

  def _ensure_agent_window(
      self, tag: str, app_worktree_dir: str, shared_dir: str
  ) -> None:
    session_root = os.path.join(self.sessions_dir, tag)
    os.makedirs(session_root, exist_ok=True)
    conv_id = self.get_conversation_id(tag)
    venv_activate = os.path.join(self.udmi_root, "venv", "bin", "activate")
    agent_script = os.path.join(session_root, "runner_agent.sh")
    agent_term_log = os.path.join(session_root, "agent_term.log")
    with open(agent_script, "w", encoding="utf-8") as f:
      f.write(
          f"""#!/bin/bash
trap ':' INT
source "{venv_activate}"
export PATH="$HOME/bin:$HOME/.gemini/jetski/bin:$PATH"
export AXOLOCTL_CONFIG="{self.config.config_path}"
export AXOLOCTL_TAG="{tag}"
export AXOLOCTL_CONV_ID="{conv_id}"
export AXOLOCTL_DATA_DIR="{shared_dir}"
export AXOLOCTL_HOST_PORT="{self.config.host_port}"
export AXOLOCTL_WEBMCP_PORT="{self.config.webmcp_port}"
export AXOLOCTL_MCP_PROXY_URL="http://127.0.0.1:{self.config.host_port}"
export MCP_CONFIG="{self.config.mcp_config_path}"
cd "{app_worktree_dir}"
echo "Axoloctl Dedicated Agent [{tag}]"
echo "  Worktree : {app_worktree_dir} (branch: axoloctl-{tag})"
echo "  MCP Cfg  : {self.config.mcp_config_path}"
export PS1="[axoloctl:{tag}] \\W $ "
CONV_FILE="{session_root}/conversation_id.txt"
if [[ "${{AXOLOCTL_AUTO_JETSKI:-1}}" != "0" ]]; then
  unset ANTIGRAVITY_AGENT ANTIGRAVITY_CONVERSATION_ID ANTIGRAVITY_LS_ADDRESS ANTIGRAVITY_SOURCE_METADATA ANTIGRAVITY_TRAJECTORY_ID
  export ANTIGRAVITY_CSRF_TOKEN="axoloctl-{tag}"
  while true; do
    if [[ -f "$CONV_FILE" ]]; then
      export AXOLOCTL_CONV_ID="$(tr -d '[:space:]' < "$CONV_FILE")"
    fi
    if command -v jetski >/dev/null 2>&1; then
      if [[ -n "${{AXOLOCTL_CONV_ID:-}}" && -d "$HOME/.gemini/jetski/brain/$AXOLOCTL_CONV_ID" ]]; then
        jetski --repl_mode --conversation "$AXOLOCTL_CONV_ID" --csrf_token="axoloctl-{tag}" || true
      else
        jetski --repl_mode --csrf_token="axoloctl-{tag}" || true
      fi
    elif command -v gemini >/dev/null 2>&1; then
      gemini || true
    else
      break
    fi
    echo "Agent CLI exited. Restarting in 2 seconds..."
    sleep 2
  done
fi
exec bash --norc -i
"""
      )
    os.chmod(agent_script, 0o755)
    if not self.is_agent_running(tag):
      self._ensure_tmux_window(self.session_agent, tag, agent_script)
    open(agent_term_log, "a", encoding="utf-8").close()
    subprocess.run(
        [
            "tmux",
            "pipe-pane",
            "-t",
            f"{self.session_agent}:{tag}",
            f"cat >> '{agent_term_log}'",
        ],
        check=False,
    )

  def _probe_http(self, port: int) -> bool:
    try:
      opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
      req = urllib.request.Request(f"http://127.0.0.1:{port}/", method="GET")
      with opener.open(req, timeout=1.0) as response:
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

  def is_agent_running(self, tag: str) -> bool:
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

  def list_servers(self) -> List[Dict[str, Any]]:
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
        servers.append({
            "tag": tag,
            "description": desc,
            "url": self.session_url(tag),
            "agent_running": self.is_agent_running(tag),
            "workspace": self.agent_workspace_dir(tag),
        })
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
    desc_path = os.path.join(session_root, "description.txt")
    commit_path = os.path.join(session_root, "commit.txt")

    os.makedirs(session_root, exist_ok=True)
    os.makedirs(shared_dir, exist_ok=True)
    if not os.path.exists(commit_path):
      with open(desc_path, "w", encoding="utf-8") as f:
        f.write(description or "")
      with open(commit_path, "w", encoding="utf-8") as f:
        f.write(commit_hash)

    # Provision isolated Git worktree and paired Agent window for this tag
    app_worktree_dir = self._ensure_agent_worktree(tag, commit_hash)
    self._ensure_agent_window(tag, app_worktree_dir, shared_dir)

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
    url = self.session_url(tag)
    venv_activate = os.path.join(self.udmi_root, "venv", "bin", "activate")
    proxy_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "session_proxy.py"
    )
    beacons_file = os.path.join(session_root, "beacons.json")
    if not os.path.exists(beacons_file):
      with open(beacons_file, "w", encoding="utf-8") as f:
        json.dump({}, f)

    wrapper_script = os.path.join(session_root, "runner.sh")
    with open(wrapper_script, "w", encoding="utf-8") as f:
      f.write(
          f"""#!/bin/bash -e
source "{venv_activate}"
export AXOLOCTL_DATA_DIR="{shared_dir}"
export AXOLOCTL_TAG="{tag}"
export AXOLOCTL_COMMIT="{commit_hash}"
export AXOLOCTL_MCP_PROXY_URL="http://127.0.0.1:{self.config.host_port}"
export PYTHONUNBUFFERED=1
exec python3 "{proxy_script}" \\
  --port {port} \\
  --tag "{tag}" \\
  --commit "{commit_hash}" \\
  --description {shlex.quote(description or "")} \\
  --app-dir "{app_code_dir}" \\
  --entrypoint "{self.config.entrypoint}" \\
  --port-env-var "{self.config.port_env_var or ''}" \\
  --log-file "{log_file}" \\
  --beacons-file "{beacons_file}"
"""
      )
    os.chmod(wrapper_script, 0o755)

    open(log_file, "w", encoding="utf-8").close()

    self._ensure_tmux_window(self.session_web, tag, wrapper_script)

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

    with open(desc_path, "w", encoding="utf-8") as f:
      f.write(description or "")
    with open(commit_path, "w", encoding="utf-8") as f:
      f.write(commit_hash)

    logs = self.read_logs(tag)["lines"]
    if not running:
      if self.is_running(tag):
        self.stop_server(tag)
      return {
          "running": False,
          "agent_running": self.is_agent_running(tag),
          "workspace": app_worktree_dir,
          "url": None,
          "cursor": len(logs),
          "logs": logs,
      }

    return {
        "running": True,
        "agent_running": self.is_agent_running(tag),
        "workspace": app_worktree_dir,
        "url": url,
        "cursor": len(logs),
        "logs": logs,
    }

  def list_defined_sessions(self) -> Dict[str, Dict[str, Any]]:
    """Returns all defined sessions (both running and stopped) keyed by tag."""
    ports = self._load_ports()
    defined_tags = list(ports.keys())
    if os.path.isdir(self.sessions_dir):
      for entry in sorted(os.listdir(self.sessions_dir)):
        if entry not in ports and self._is_valid_tag(entry):
          cpath = os.path.join(self.sessions_dir, entry, "commit.txt")
          if os.path.isfile(cpath):
            defined_tags.append(entry)

    result: Dict[str, Dict[str, Any]] = {}
    for tag in defined_tags:
      session_root = os.path.join(self.sessions_dir, tag)
      commit_path = os.path.join(session_root, "commit.txt")
      commit_hash = ""
      if os.path.isfile(commit_path):
        with open(commit_path, "r", encoding="utf-8") as f:
          commit_hash = f.read().strip()
      desc_path = os.path.join(session_root, "description.txt")
      desc = ""
      if os.path.isfile(desc_path):
        with open(desc_path, "r", encoding="utf-8") as f:
          desc = f.read().strip()
      running = self.is_running(tag)
      result[tag] = {
          "tag": tag,
          "port": ports.get(tag),
          "running": running,
          "commit": commit_hash,
          "description": desc,
          "url": self.session_url(tag),
          "agent_running": self.is_agent_running(tag),
          "workspace": self.agent_workspace_dir(tag),
          "nonces": self.get_beacons(tag) if running else {},
      }
    return result

  def create_or_start_session(
      self, tag: str, commit_hash: str = "", description: str = ""
  ) -> Dict[str, Any]:
    """Creates a new tagged session at HEAD or starts an existing defined session."""
    tag = (tag or "").strip()
    if not self._is_valid_tag(tag) or tag.lower() in ("localhost", "__new__"):
      raise ValueError(
          f"Invalid session tag '{tag}'. Use alphanumeric characters, '-' or '_'."
      )

    session_root = os.path.join(self.sessions_dir, tag)
    commit_path = os.path.join(session_root, "commit.txt")
    desc_path = os.path.join(session_root, "description.txt")

    resolved_commit = (commit_hash or "").strip()
    if not resolved_commit and os.path.isfile(commit_path):
      with open(commit_path, "r", encoding="utf-8") as f:
        resolved_commit = f.read().strip()

    if not resolved_commit:
      head_res = subprocess.run(
          [
              "git",
              "-c",
              "safe.bareRepository=all",
              "-C",
              self.config.repo_path,
              "rev-parse",
              "HEAD",
          ],
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          text=True,
          check=False,
      )
      if head_res.returncode != 0:
        raise ValueError(
            f"Failed to resolve HEAD commit in {self.config.repo_path}: "
            f"{head_res.stderr.strip()}"
        )
      resolved_commit = head_res.stdout.strip()

    resolved_desc = (description or "").strip()
    if not resolved_desc and os.path.isfile(desc_path):
      with open(desc_path, "r", encoding="utf-8") as f:
        resolved_desc = f.read().strip()
    if not resolved_desc:
      resolved_desc = f"Session {tag}"

    ports = self._load_ports()
    if (
        not commit_hash
        and self.is_running(tag)
        and tag in ports
        and self._probe_http(ports[tag])
    ):
      shared_dir = os.path.join(self.shared_dir, tag)
      os.makedirs(shared_dir, exist_ok=True)
      app_wt = self._ensure_agent_worktree(tag, resolved_commit)
      self._ensure_agent_window(tag, app_wt, shared_dir)
      return {
          "running": True,
          "tag": tag,
          "commit": resolved_commit,
          "description": resolved_desc,
          "agent_running": self.is_agent_running(tag),
          "workspace": app_wt,
          "url": self.session_url(tag),
      }

    res = self.start_server(tag, resolved_commit, resolved_desc)
    return {
        **res,
        "tag": tag,
        "commit": resolved_commit,
        "description": resolved_desc,
    }

  def get_beacons(self, tag: str) -> Dict[str, Any]:
    beacons_file = os.path.join(self.sessions_dir, tag, "beacons.json")
    if os.path.exists(beacons_file):
      try:
        with open(beacons_file, "r", encoding="utf-8") as f:
          data = json.load(f)
          if isinstance(data, dict):
            return data
      except Exception:
        pass
    return {}

  def record_beacon(
      self, tag: str, nonce: str, url: str = "", title: str = ""
  ) -> Dict[str, Any]:
    session_root = self._ensure_known_tag(tag)
    beacons_file = os.path.join(session_root, "beacons.json")
    data = self.get_beacons(tag)
    entry = {
        "timestamp": time.time(),
        "url": url,
        "title": title,
    }
    data[nonce] = entry
    tmp_path = f"{beacons_file}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
      json.dump(data, f, indent=2)
    os.replace(tmp_path, beacons_file)
    return entry

  def session_url(self, tag: str) -> str:
    return f"http://{tag}.localhost:{self.config.host_port}"

  def resolve_nonce(self, nonce: str) -> Optional[Dict[str, Any]]:
    if not nonce:
      return None
    ports = self._load_ports()
    for tag, port in ports.items():
      beacons = self.get_beacons(tag)
      if nonce in beacons:
        if not self.is_running(tag):
          continue
        commit_path = os.path.join(self.sessions_dir, tag, "commit.txt")
        commit_hash = ""
        if os.path.exists(commit_path):
          with open(commit_path, "r", encoding="utf-8") as f:
            commit_hash = f.read().strip()
        desc_path = os.path.join(self.sessions_dir, tag, "description.txt")
        desc = ""
        if os.path.exists(desc_path):
          with open(desc_path, "r", encoding="utf-8") as f:
            desc = f.read().strip()
        return {
            "tag": tag,
            "port": port,
            "commit": commit_hash,
            "description": desc,
            "url": self.session_url(tag),
            "agent_running": self.is_agent_running(tag),
            "workspace": self.agent_workspace_dir(tag),
            "nonce": nonce,
            "beacon": beacons[nonce],
        }
    return None

  def get_status(self, tag: str) -> Dict[str, Any]:
    session_root = self._ensure_known_tag(tag)
    commit_path = os.path.join(session_root, "commit.txt")
    with open(commit_path, "r", encoding="utf-8") as f:
      commit_hash = f.read().strip()

    running = self.is_running(tag)
    return {
        "running": running,
        "agent_running": self.is_agent_running(tag),
        "commit_hash": commit_hash,
        "workspace": self.agent_workspace_dir(tag),
        "url": self.session_url(tag),
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
