"""Console Manager for GUMMI task terminal emulation and backend tmux agent session."""

import base64
import json
import os
import re
import shlex
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple


def build_tmux_keys(sess: str, keys: List[str]) -> List[str]:
    """Builds tmux send-keys command arguments from hex byte strings."""
    if keys == ["7f"] or keys == ["08"]:
        return ["tmux", "send-keys", "-t", sess, "BSpace"]
    elif keys == ["1b", "5b", "33", "7e"]:
        return ["tmux", "send-keys", "-t", sess, "DC"]
    return ["tmux", "send-keys", "-t", sess, "-H"] + keys


class GummiConsoleManager:
    """Manages backend tmux session 'gummi~agent' and terminal I/O for Jetski."""

    def __init__(
        self,
        session_name: str = "gummi~agent",
        repo_root: Optional[str] = None,
        runtime_dir: Optional[str] = None,
        mock_mode: bool = False,
    ):
        self.session_name = session_name
        self.mock_mode = mock_mode
        self.repo_root = repo_root or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        self.runtime_dir = runtime_dir or os.path.join(self.repo_root, "var")
        os.makedirs(self.runtime_dir, exist_ok=True)
        self.log_file = os.path.join(self.runtime_dir, "gummi_agent.log")
        self.exit_file = os.path.join(self.runtime_dir, "gummi_agent.exit")
        self.conv_file = os.path.join(self.runtime_dir, ".jetski_conv_id")
        self.last_cols = 120
        self.last_rows = 30
        self._mock_log = "GUMMI Task Console [gummi~agent]\r\nWelcome to Jetski interactive session.\r\n> "
        self._mock_running: bool = False
        self._mock_active: bool = False
        self._mock_error: bool = False
        self._mock_exit_code: int = 0

    def is_running(self) -> bool:
        """Returns True if the tmux session is active."""
        if self.mock_mode:
            return self._mock_running
        res = subprocess.run(["tmux", "has-session", "-t", self.session_name], capture_output=True)
        return res.returncode == 0

    def get_conv_id(self) -> Optional[str]:
        """Reads cached conversation ID if available."""
        if os.path.exists(self.conv_file):
            try:
                with open(self.conv_file, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
        return None

    def start_jetski(
        self,
        prompt: Optional[str] = None,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Starts or attaches to the jetski tmux session."""
        c = int(cols) if cols else self.last_cols
        r = int(rows) if rows else self.last_rows
        self.last_cols = c
        self.last_rows = r

        if self.mock_mode:
            self._mock_running = True
            self._mock_error = False
            self._mock_exit_code = 0
            return {"status": "started", "session": self.session_name, "conv_id": "mock-conv-123"}

        conv_id = ""
        if os.path.exists(self.conv_file):
            try:
                with open(self.conv_file, "r", encoding="utf-8") as f:
                    conv_id = f.read().strip()
            except Exception:
                pass

        if not conv_id:
            my_env = os.environ.copy()
            my_env["PATH"] = (
                my_env.get("PATH", "")
                + ":"
                + os.path.expanduser("~/bin")
                + ":"
                + os.path.expanduser("~/.gemini/jetski/bin")
            )
            try:
                res = subprocess.run(
                    [
                        "agentapi",
                        "new-conversation",
                        f"Hi! Let's work on the GUMMI interface in {self.repo_root}.",
                    ],
                    capture_output=True,
                    text=True,
                    env=my_env,
                    cwd=self.repo_root,
                    timeout=10,
                )
                match = re.search(r'"conversationId":\s*"([^"]+)"', res.stdout)
                if match:
                    conv_id = match.group(1)
            except Exception:
                pass

            if conv_id:
                try:
                    with open(self.conv_file, "w", encoding="utf-8") as f:
                        f.write(conv_id)
                except Exception:
                    pass

        if self.is_running():
            return {"status": "already_running", "session": self.session_name, "conv_id": conv_id}

        if os.path.exists(self.exit_file):
            try:
                os.remove(self.exit_file)
            except Exception:
                pass

        if prompt:
            if conv_id:
                cmd_str = f"jetski --repl_mode --conversation {conv_id} -i {shlex.quote(prompt)}"
            else:
                cmd_str = f"jetski --repl_mode -i {shlex.quote(prompt)}"
        else:
            if conv_id:
                cmd_str = f"jetski --repl_mode --conversation {conv_id}"
            else:
                cmd_str = "jetski --repl_mode"

        diag_cmd = (
            f"export PATH=\"{os.path.expanduser('~/bin')}:{os.path.expanduser('~/.gemini/jetski/bin')}:$PATH\"; "
            f"export COLUMNS={c}; export LINES={r}; "
        )
        wrapped_command = f"( {diag_cmd} {cmd_str} ) ; echo $? > {self.exit_file}"

        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.session_name,
                "-x",
                str(c),
                "-y",
                str(r),
                "-c",
                self.repo_root,
                wrapped_command,
            ],
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "pipe-pane", "-t", self.session_name, "-o", f"cat > {self.log_file}"],
            capture_output=True,
        )

        return {"status": "started", "session": self.session_name, "conv_id": conv_id}

    def get_pane_child_pids(self) -> List[str]:
        """Returns child process PIDs running in the tmux session pane."""
        if self.mock_mode:
            return []
        res = subprocess.run(
            ["tmux", "list-panes", "-t", self.session_name, "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return []
        parent_pids = [p.strip() for p in res.stdout.strip().splitlines() if p.strip()]
        child_pids: List[str] = []
        for p in parent_pids:
            c_res = subprocess.run(["pgrep", "-P", p], capture_output=True, text=True)
            if c_res.returncode == 0:
                child_pids.extend([c.strip() for c in c_res.stdout.strip().splitlines() if c.strip()])
        return child_pids

    def get_session_cli_log(self) -> Optional[str]:
        """Discovers the active Jetski CLI log file from the session's open file descriptors."""
        pids = self.get_pane_child_pids()
        for pid in pids:
            fd_dir = f"/proc/{pid}/fd"
            if os.path.exists(fd_dir):
                try:
                    for fd in os.listdir(fd_dir):
                        target = os.path.realpath(os.path.join(fd_dir, fd))
                        if "cli-" in target and target.endswith(".log"):
                            return target
                except Exception:
                    pass
        log_dir = os.path.expanduser("~/.gemini/jetski/cli/log")
        if os.path.isdir(log_dir):
            try:
                files = [
                    os.path.join(log_dir, f)
                    for f in os.listdir(log_dir)
                    if f.startswith("cli-") and f.endswith(".log")
                ]
                if files:
                    return max(files, key=os.path.getmtime)
            except Exception:
                pass
        return None

    def is_active(self) -> bool:
        """Determines if the running session is actively performing work vs idle."""
        if self.mock_mode:
            return getattr(self, "_mock_active", False)
        if not self.is_running():
            return False

        # 1. Check if child tool processes exist under the pane PID (e.g. running bash, python, git)
        try:
            child_pids = self.get_pane_child_pids()
            if len(child_pids) > 1:
                return True
        except Exception:
            pass

        # 2. Check conversation transcript for authoritative turn status
        conv_id = self.get_conv_id()
        if conv_id:
            transcript_path = os.path.expanduser(f"~/.gemini/jetski/brain/{conv_id}/.system_generated/logs/transcript.jsonl")
            if os.path.exists(transcript_path):
                try:
                    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                        lines = [line.strip() for line in f if line.strip()]
                        if lines:
                            last_step = json.loads(lines[-1])
                            step_type = last_step.get("type")
                            status = last_step.get("status")
                            has_tools = bool(last_step.get("tool_calls"))

                            # If user input was sent, step not finished, or tools are running: active!
                            if step_type == "USER_INPUT" or status != "DONE" or has_tools:
                                return True

                            # If final planner response is DONE without pending tool calls: idle!
                            if step_type == "PLANNER_RESPONSE" and status == "DONE" and not has_tools:
                                return False
                except Exception:
                    pass

        # 3. Check screen capture: inspect only active status line at bottom of pane
        try:
            cap = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", self.session_name],
                capture_output=True,
                text=True,
                timeout=1,
            )
            if cap.returncode == 0 and cap.stdout:
                bottom_lines = cap.stdout.splitlines()[-6:]
                bottom_text = "\n".join(bottom_lines)
                spinners = [
                    "Thinking...",
                    "Loading...",
                    "Generating...",
                    "Calling tool",
                    "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
                    "⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷",
                ]
                if any(s in bottom_text for s in spinners):
                    return True
        except Exception:
            pass

        return False

    def get_diagnostics(self) -> Dict[str, Any]:
        """Analyzes session state and active logs to detect auth failures or startup errors."""
        running = self.is_running()
        if self.mock_mode:
            has_error = getattr(self, "_mock_error", False)
            exit_code = getattr(self, "_mock_exit_code", 0)
            is_active = getattr(self, "_mock_active", False)
            if not running:
                if has_error or exit_code != 0:
                    code = exit_code or 1
                    return {
                        "state": "error",
                        "status_text": f"Exited (code {code})",
                        "severity": "error",
                        "button_state": "red",
                        "running": False,
                        "active": False,
                        "exit_code": code,
                        "alert": f"Agent process exited with code {code}.",
                    }
                return {
                    "state": "not_running",
                    "status_text": "Not Running",
                    "severity": "neutral",
                    "button_state": "blue",
                    "running": False,
                    "active": False,
                    "alert": None,
                }
            else:
                if is_active:
                    return {
                        "state": "active",
                        "status_text": "Actively Working",
                        "severity": "warning",
                        "button_state": "yellow",
                        "running": True,
                        "active": True,
                        "alert": None,
                    }
                return {
                    "state": "idle",
                    "status_text": "Idle",
                    "severity": "success",
                    "button_state": "green",
                    "running": True,
                    "active": False,
                    "alert": None,
                }

        if not running:
            if os.path.exists(self.exit_file):
                code = 1
                try:
                    with open(self.exit_file, "r", encoding="utf-8") as f:
                        code_str = f.read().strip()
                    if code_str.isdigit() or (code_str.startswith("-") and code_str[1:].isdigit()):
                        code = int(code_str)
                except Exception:
                    pass
                if code != 0:
                    err_detail = ""
                    if os.path.exists(self.log_file):
                        try:
                            with open(self.log_file, "r", errors="replace") as lf:
                                lines = [l.strip() for l in lf.readlines() if l.strip()]
                                if lines:
                                    err_detail = lines[-1]
                        except Exception:
                            pass
                    msg = f"Agent process exited with code {code}."
                    if err_detail:
                        msg += f" Details: {err_detail}"
                    return {
                        "state": "error",
                        "status_text": f"Exited (code {code})",
                        "severity": "error",
                        "button_state": "red",
                        "running": False,
                        "active": False,
                        "alert": msg,
                        "exit_code": code,
                    }
            return {
                "state": "not_running",
                "status_text": "Not Running",
                "severity": "neutral",
                "button_state": "blue",
                "running": False,
                "active": False,
                "alert": None,
            }

        cli_log = self.get_session_cli_log()
        alert_msg = None
        state = "idle"
        severity = "success"
        if cli_log and os.path.exists(cli_log):
            try:
                with open(cli_log, "r", errors="replace") as cf:
                    cf.seek(max(0, os.path.getsize(cli_log) - 20000))
                    content = cf.read()
                if (
                    "ThinMint is expired" in content
                    or "AUTH_FAIL" in content
                    or "loas2 handshake failed" in content
                    or "Couldn't get ID from REKE cert" in content
                ):
                    state = "auth_required"
                    severity = "warning"
                    alert_msg = (
                        "Authentication Required: Google ThinMint / LOAS certificate has expired. "
                        "Run 'glogin' in your workstation terminal to authenticate, then click Restart Agent."
                    )
                elif "Required key not available" in content:
                    state = "key_error"
                    severity = "error"
                    alert_msg = "Permission/Key Error: Required key not available. Check security credentials."
            except Exception:
                pass

        active = self.is_active()
        if active:
            button_state = "yellow"
            status_text = "Actively Working"
        else:
            button_state = "green"
            status_text = (
                "Idle"
                if state == "idle"
                else (
                    "Auth Required (run 'glogin')"
                    if state == "auth_required"
                    else "Key Error"
                )
            )

        return {
            "state": state if state != "idle" else ("active" if active else "idle"),
            "status_text": status_text,
            "severity": severity if state != "idle" else ("warning" if active else "success"),
            "button_state": button_state,
            "running": True,
            "active": active,
            "alert": alert_msg,
            "cli_log": cli_log,
        }

    def get_log(self, offset: int = 0) -> Dict[str, Any]:
        """Reads console output from tmux capture or log file."""
        diag = self.get_diagnostics()
        running = self.is_running()
        button_state = diag.get("button_state", "blue" if not running else "green")
        if self.mock_mode:
            data_bytes = self._mock_log.encode("utf-8")
            if offset >= len(data_bytes):
                return {
                    "data": "",
                    "offset": len(data_bytes),
                    "cleared": False,
                    "running": running,
                    "diagnostics": diag,
                    "button_state": button_state,
                }
            chunk = data_bytes[offset:]
            return {
                "data": base64.b64encode(chunk).decode("ascii"),
                "offset": len(data_bytes),
                "cleared": False,
                "running": running,
                "diagnostics": diag,
                "button_state": button_state,
            }

        data_b64 = ""
        new_offset = offset
        cleared = False

        if os.path.exists(self.log_file):
            file_len = os.path.getsize(self.log_file)
            if offset > file_len:
                offset = 0
                cleared = True

            if offset == 0 and running:
                cap = subprocess.run(
                    ["tmux", "capture-pane", "-S", "-", "-e", "-p", "-t", self.session_name],
                    capture_output=True,
                )
                if cap.returncode == 0 and cap.stdout:
                    cursor_code = ""
                    pos_res = subprocess.run(
                        ["tmux", "display-message", "-p", "-t", self.session_name, "#{cursor_x},#{cursor_y}"],
                        capture_output=True,
                        text=True,
                    )
                    if pos_res.returncode == 0 and "," in pos_res.stdout:
                        try:
                            cx, cy = map(int, pos_res.stdout.strip().split(","))
                            cursor_code = f"\033[{cy + 1};{cx + 1}H"
                        except Exception:
                            pass
                    raw_lines = cap.stdout.split(b"\n")
                    while raw_lines and not raw_lines[-1].strip():
                        raw_lines.pop()
                    screen_data = b"\r\n".join(raw_lines)
                    payload = b"\033[H" + screen_data + cursor_code.encode("utf-8")
                    data_b64 = base64.b64encode(payload).decode("ascii")
                    new_offset = file_len
                    return {
                        "data": data_b64,
                        "offset": new_offset,
                        "cleared": cleared,
                        "running": running,
                        "diagnostics": diag,
                        "button_state": button_state,
                    }

            with open(self.log_file, "rb") as f:
                if offset == 0 and file_len > 500000:
                    f.seek(max(0, file_len - 200000))
                else:
                    f.seek(offset)
                data = f.read()
                new_offset = file_len if (offset == 0 and file_len > 500000) else f.tell()
                data_b64 = base64.b64encode(data).decode("ascii")
        elif running and offset == 0:
            cap = subprocess.run(
                ["tmux", "capture-pane", "-S", "-", "-e", "-p", "-t", self.session_name],
                capture_output=True,
            )
            if cap.returncode == 0 and cap.stdout:
                data_b64 = base64.b64encode(cap.stdout).decode("ascii")

        return {
            "data": data_b64,
            "offset": new_offset,
            "cleared": cleared,
            "running": running,
            "diagnostics": diag,
            "button_state": button_state,
        }

    def send_keys(self, hex_keys: List[str]) -> Tuple[bool, str]:
        """Sends hex keys to tmux session."""
        if not hex_keys:
            return False, "Missing keys"
        if self.mock_mode:
            return True, "ok"
        if not self.is_running():
            return False, f"Session {self.session_name} does not exist"
        args = build_tmux_keys(self.session_name, hex_keys)
        res = subprocess.run(args, capture_output=True, text=True)
        if res.returncode != 0:
            return False, res.stderr
        return True, "ok"

    def resize(self, cols: int, rows: int) -> bool:
        """Resizes the tmux session window."""
        self.last_cols = cols
        self.last_rows = rows
        if self.mock_mode or not self.is_running():
            return True
        res = subprocess.run(
            ["tmux", "resize-window", "-t", self.session_name, "-x", str(cols), "-y", str(rows)],
            capture_output=True,
        )
        return res.returncode == 0

    def kill(self) -> bool:
        """Terminates the tmux session."""
        if self.mock_mode:
            self._mock_running = False
            return True
        res = subprocess.run(["tmux", "kill-session", "-t", self.session_name], capture_output=True)
        return res.returncode == 0
