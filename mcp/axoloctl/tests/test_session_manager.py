"""Unit tests for Axoloctl config validation, SessionManager, and WebMCPServer."""

import json
import os
import sys
import pytest

UDMI_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
sys.path.insert(0, os.path.join(UDMI_ROOT, "mcp", "axoloctl", "src"))

from config import load_config
from session_manager import SessionManager
from web_mcp import WebMCPServer

GUMMI_CONFIG_PATH = os.path.join(
    UDMI_ROOT, "mcp", "axoloctl", "etc", "gummi_config.json"
)


def test_load_valid_config():
  cfg = load_config(GUMMI_CONFIG_PATH, UDMI_ROOT)
  assert cfg.repo_path == UDMI_ROOT
  assert cfg.app_subpath == "gummi"
  assert cfg.entrypoint == "bin/gummi"
  assert cfg.host_port == 9290
  assert cfg.webmcp_port == 9291
  assert cfg.session_port_base == 9300
  assert "axoloctl" in cfg.mcp_servers
  assert "butler" in cfg.mcp_servers
  assert cfg.default_ui == "cliView"
  assert len(cfg.uis) == 3


def test_config_fail_fast_missing_path():
  with pytest.raises(ValueError, match="configuration file is required"):
    load_config("", UDMI_ROOT)

  with pytest.raises(ValueError, match="not found"):
    load_config("/nonexistent/axoloctl_cfg.json", UDMI_ROOT)


def test_config_fail_fast_missing_keys(tmp_path):
  incomplete_cfg = tmp_path / "incomplete.json"
  incomplete_cfg.write_text(json.dumps({"repo_path": "."}), encoding="utf-8")
  with pytest.raises(ValueError, match="missing required keys"):
    load_config(str(incomplete_cfg), UDMI_ROOT)


def test_session_manager_init_and_validation(tmp_path):
  cfg = load_config(GUMMI_CONFIG_PATH, UDMI_ROOT)
  mgr = SessionManager(str(tmp_path), cfg)
  assert os.path.exists(mgr.sessions_dir)
  assert os.path.exists(mgr.shared_dir)

  assert mgr._is_valid_hash("a" * 40) is True
  assert mgr._is_valid_hash("a" * 39) is False
  assert mgr._is_valid_hash("HEAD") is False
  assert mgr._is_valid_hash("main") is False


def test_unknown_tag_fail_fast(tmp_path):
  cfg = load_config(GUMMI_CONFIG_PATH, UDMI_ROOT)
  mgr = SessionManager(str(tmp_path), cfg)

  with pytest.raises(ValueError, match="Unknown session tag"):
    mgr.get_status("nonexistent_tag")

  with pytest.raises(ValueError, match="Unknown session tag"):
    mgr.read_logs("nonexistent_tag")

  with pytest.raises(ValueError, match="Unknown session tag"):
    mgr.stop_server("nonexistent_tag")


def test_sticky_port_allocation(tmp_path):
  cfg = load_config(GUMMI_CONFIG_PATH, UDMI_ROOT)
  mgr = SessionManager(str(tmp_path), cfg)

  p1 = mgr._allocate_port("tag_a")
  p2 = mgr._allocate_port("tag_b")
  p1_again = mgr._allocate_port("tag_a")

  assert p1 == 9300
  assert p2 == 9301
  assert p1_again == 9300


def test_webmcp_protocol_handshake(tmp_path, monkeypatch):
  monkeypatch.delenv("AXOLOCTL_TAG", raising=False)
  cfg = load_config(GUMMI_CONFIG_PATH, UDMI_ROOT)
  mgr = SessionManager(str(tmp_path), cfg)
  mcp = WebMCPServer(mgr)

  init_res = mcp.handle_jsonrpc(
      {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
  )
  assert init_res["result"]["serverInfo"]["name"] == "udmi-axoloctl"

  notif_res = mcp.handle_jsonrpc(
      {"jsonrpc": "2.0", "method": "notifications/initialized"}
  )
  assert notif_res is None

  tools_res = mcp.handle_jsonrpc(
      {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
  )
  tools_list = tools_res["result"]["tools"]
  tool_names = {t["name"] for t in tools_list}
  assert tool_names == {
      "start_server",
      "stop_server",
      "get_status",
      "read_logs",
  }
  for tool_def in tools_list:
    assert "tag" not in tool_def["inputSchema"].get("properties", {})

  with pytest.raises(ValueError, match="Missing required session binding"):
    mcp.dispatch_tool("get_status", {}, bound_tag="")

  with pytest.raises(ValueError, match="Unexpected 'tag' argument"):
    mcp.dispatch_tool("get_status", {"tag": "gummi"}, bound_tag="gummi")



def test_multi_session_nonce_resolution(tmp_path, monkeypatch):
  cfg = load_config(GUMMI_CONFIG_PATH, UDMI_ROOT)
  mgr = SessionManager(str(tmp_path), cfg)

  for tag, sha in [("gummi", "a" * 40), ("exp_ui", "b" * 40)]:
    mgr._allocate_port(tag)
    sdir = os.path.join(mgr.sessions_dir, tag)
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "commit.txt"), "w", encoding="utf-8") as f:
      f.write(sha)
    with open(os.path.join(sdir, "description.txt"), "w", encoding="utf-8") as f:
      f.write(f"Session {tag}")

  monkeypatch.setattr(mgr, "is_running", lambda t: t in ("gummi", "exp_ui"))

  mgr.record_beacon("gummi", "nonce-tab-1", url="http://localhost:55001/", title="Tab 1")
  mgr.record_beacon("exp_ui", "nonce-tab-2", url="http://localhost:55002/", title="Tab 2")

  res1 = mgr.resolve_nonce("nonce-tab-1")
  assert res1 is not None
  assert res1["tag"] == "gummi"
  assert res1["commit"] == "a" * 40
  assert res1["port"] == 9300
  assert res1["url"] == "http://gummi.localhost:9290"

  res2 = mgr.resolve_nonce("nonce-tab-2")
  assert res2 is not None
  assert res2["tag"] == "exp_ui"
  assert res2["commit"] == "b" * 40
  assert res2["port"] == 9301
  assert res2["url"] == "http://exp_ui.localhost:9290"

  assert mgr.resolve_nonce("unknown-nonce") is None


def test_session_proxy_beacon_telemetry_and_forwarding(tmp_path):
  from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
  import threading
  import urllib.request
  from session_proxy import BeaconStore, SessionProxyHandler, allocate_ephemeral_port

  class DummyAppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
      body = b"hello from app"
      self.send_response(200)
      self.send_header("Content-Type", "text/plain")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def log_message(self, fmt, *args):
      pass

  app_port = allocate_ephemeral_port()
  app_srv = ThreadingHTTPServer(("127.0.0.1", app_port), DummyAppHandler)
  app_thread = threading.Thread(target=app_srv.serve_forever, daemon=True)
  app_thread.start()

  beacons_file = str(tmp_path / "beacons.json")
  log_file = str(tmp_path / "unified.log")
  store = BeaconStore(beacons_file, log_file)

  class CustomProxyHandler(SessionProxyHandler):
    pass

  CustomProxyHandler.app_port = app_port
  CustomProxyHandler.tag = "gummi"
  CustomProxyHandler.commit_hash = "c" * 40
  CustomProxyHandler.description = "Proxy test"
  CustomProxyHandler.beacon_store = store

  proxy_port = allocate_ephemeral_port()
  proxy_srv = ThreadingHTTPServer(("127.0.0.1", proxy_port), CustomProxyHandler)
  proxy_thread = threading.Thread(target=proxy_srv.serve_forever, daemon=True)
  proxy_thread.start()

  opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
  try:
    # 1. Test POST /.axoloctl/beacon
    beacon_req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}/.axoloctl/beacon",
        data=json.dumps({"nonce": "test-nonce-99", "url": "http://forwarded:1234/"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(beacon_req, timeout=5) as resp:
      payload = json.loads(resp.read().decode("utf-8"))
      assert payload["axoloctl"] is True
      assert payload["tag"] == "gummi"
      assert payload["commit_hash"] == "c" * 40
      assert payload["nonce"] == "test-nonce-99"

    assert store.has_nonce("test-nonce-99") is True

    # 2. Test POST /.axoloctl/telemetry
    tel_req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}/.axoloctl/telemetry",
        data=json.dumps({"nonce": "test-nonce-99", "message": "ReferenceError: foo is not defined"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(tel_req, timeout=5) as resp:
      payload = json.loads(resp.read().decode("utf-8"))
      assert payload["ok"] is True

    with open(log_file, "r", encoding="utf-8") as f:
      logs = f.read()
    assert "[browser] ReferenceError: foo is not defined" in logs

    # 3. Test transparent forwarding of application GET request
    with opener.open(f"http://127.0.0.1:{proxy_port}/index.html", timeout=5) as resp:
      body = resp.read().decode("utf-8")
      assert body == "hello from app"
      assert resp.headers.get("X-Axoloctl-Tag") == "gummi"
      assert resp.headers.get("X-Axoloctl-Commit") == "c" * 40
  finally:
    proxy_srv.shutdown()
    app_srv.shutdown()


def test_ui_host_virtual_host_multiplexing(tmp_path):
  from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
  import threading
  import urllib.error
  import urllib.request
  from session_proxy import BeaconStore, SessionProxyHandler, allocate_ephemeral_port
  from ui_host import UIHostHandler

  cfg = load_config(GUMMI_CONFIG_PATH, UDMI_ROOT)
  servers = []

  def start_mock_session(tag_name: str, commit_char: str, body_text: str) -> int:
    class AppHandler(BaseHTTPRequestHandler):
      def do_GET(self):
        b = body_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

      def log_message(self, fmt, *args):
        pass

    app_port = allocate_ephemeral_port()
    app_srv = ThreadingHTTPServer(("127.0.0.1", app_port), AppHandler)
    threading.Thread(target=app_srv.serve_forever, daemon=True).start()
    servers.append(app_srv)

    sdir = tmp_path / "sessions" / tag_name
    sdir.mkdir(parents=True, exist_ok=True)
    store = BeaconStore(str(sdir / "beacons.json"), str(sdir / "unified.log"))

    class ProxyHandler(SessionProxyHandler):
      pass

    ProxyHandler.app_port = app_port
    ProxyHandler.tag = tag_name
    ProxyHandler.commit_hash = commit_char * 40
    ProxyHandler.description = f"Session {tag_name}"
    ProxyHandler.beacon_store = store

    proxy_port = allocate_ephemeral_port()
    proxy_srv = ThreadingHTTPServer(("127.0.0.1", proxy_port), ProxyHandler)
    threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()
    servers.append(proxy_srv)
    return proxy_port

  try:
    port_alpha = start_mock_session("alpha", "a", "alpha-backend-response")
    port_beta = start_mock_session("beta", "b", "beta-backend-response")

    sessions_dir = tmp_path / "sessions"
    (sessions_dir / "ports.json").write_text(
        json.dumps({"alpha": port_alpha, "beta": port_beta}), encoding="utf-8"
    )

    class CustomUIHostHandler(UIHostHandler):
      pass

    CustomUIHostHandler.config = cfg
    CustomUIHostHandler.sessions_dir = str(sessions_dir)

    gateway_port = allocate_ephemeral_port()
    gateway_srv = ThreadingHTTPServer(("127.0.0.1", gateway_port), CustomUIHostHandler)
    threading.Thread(target=gateway_srv.serve_forever, daemon=True).start()
    servers.append(gateway_srv)

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # 1. Request to Control Plane (Host: localhost:<port>) serves /api/uis
    req_ctrl = urllib.request.Request(
        f"http://127.0.0.1:{gateway_port}/api/uis",
        headers={"Host": f"localhost:{gateway_port}"},
        method="GET",
    )
    with opener.open(req_ctrl, timeout=5) as resp:
      uis_data = json.loads(resp.read().decode("utf-8"))
      assert uis_data["default_ui"] == "cliView"

    # 2. Request to alpha.localhost:<port> routes to alpha session over same gateway port
    req_alpha = urllib.request.Request(
        f"http://127.0.0.1:{gateway_port}/",
        headers={"Host": f"alpha.localhost:{gateway_port}"},
        method="GET",
    )
    with opener.open(req_alpha, timeout=5) as resp:
      assert resp.read().decode("utf-8") == "alpha-backend-response"
      assert resp.headers.get("X-Axoloctl-Tag") == "alpha"
      assert resp.headers.get("X-Axoloctl-Commit") == "a" * 40

    # 3. Request to beta.localhost:<port> routes to beta session over same gateway port
    req_beta = urllib.request.Request(
        f"http://127.0.0.1:{gateway_port}/",
        headers={"Host": f"beta.localhost:{gateway_port}"},
        method="GET",
    )
    with opener.open(req_beta, timeout=5) as resp:
      assert resp.read().decode("utf-8") == "beta-backend-response"
      assert resp.headers.get("X-Axoloctl-Tag") == "beta"
      assert resp.headers.get("X-Axoloctl-Commit") == "b" * 40

    # 4. Beacon registration via virtual host alpha.localhost:<port>
    req_beacon = urllib.request.Request(
        f"http://127.0.0.1:{gateway_port}/.axoloctl/beacon",
        data=json.dumps({"nonce": "vhost-nonce-1"}).encode("utf-8"),
        headers={
            "Host": f"alpha.localhost:{gateway_port}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with opener.open(req_beacon, timeout=5) as resp:
      b_data = json.loads(resp.read().decode("utf-8"))
      assert b_data["axoloctl"] is True
      assert b_data["tag"] == "alpha"
      assert b_data["nonce"] == "vhost-nonce-1"

    # 5. Unknown virtual host fails fast with 404
    req_unknown = urllib.request.Request(
        f"http://127.0.0.1:{gateway_port}/",
        headers={"Host": f"nonexistent.localhost:{gateway_port}"},
        method="GET",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
      opener.open(req_unknown, timeout=5)
    assert exc_info.value.code == 404
  finally:
    for srv in reversed(servers):
      srv.shutdown()


def test_per_tag_agent_worktree_isolation(tmp_path):
  import dataclasses
  import subprocess
  repo_dir = tmp_path / "repo"
  app_dir = repo_dir / "gummi"
  app_dir.mkdir(parents=True)
  (app_dir / "index.txt").write_text("v1", encoding="utf-8")

  subprocess.run(["git", "init", str(repo_dir)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
  subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
  subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
  subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial"], check=True, stdout=subprocess.DEVNULL)
  commit_sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()

  cfg = dataclasses.replace(
      load_config(GUMMI_CONFIG_PATH, UDMI_ROOT), repo_path=str(repo_dir)
  )
  mgr = SessionManager(str(tmp_path / "udmi"), cfg)

  wt_alpha = mgr._ensure_agent_worktree("alpha", commit_sha)
  wt_beta = mgr._ensure_agent_worktree("beta", commit_sha)

  assert wt_alpha != wt_beta
  assert os.path.isdir(wt_alpha)
  assert os.path.isdir(wt_beta)

  branch_alpha = subprocess.check_output(["git", "-C", wt_alpha, "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
  branch_beta = subprocess.check_output(["git", "-C", wt_beta, "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
  assert branch_alpha == "axoloctl-alpha"
  assert branch_beta == "axoloctl-beta"

  # Modifying alpha worktree does not affect beta worktree
  with open(os.path.join(wt_alpha, "index.txt"), "w", encoding="utf-8") as f:
    f.write("alpha-edit")
  with open(os.path.join(wt_beta, "index.txt"), "r", encoding="utf-8") as f:
    assert f.read() == "v1"


def test_agent_bridge_three_techniques(tmp_path):
  from http.server import ThreadingHTTPServer
  import subprocess
  import threading
  import urllib.request
  from session_proxy import allocate_ephemeral_port
  from ui_host import JetskiAgentBridge, UIHostHandler

  cfg = load_config(GUMMI_CONFIG_PATH, UDMI_ROOT)
  sessions_dir = tmp_path / "sessions"
  alpha_dir = sessions_dir / "alpha"
  (alpha_dir / "workspace" / "gummi").mkdir(parents=True)
  (sessions_dir / "ports.json").write_text(
      json.dumps({"alpha": 9300}), encoding="utf-8"
  )

  bridge = JetskiAgentBridge(str(tmp_path), cfg, str(sessions_dir))
  bridge.brain_root = str(tmp_path / "brain")
  test_tmux_session = f"axoloctl_test_agent_{os.getpid()}"
  bridge.session_agent = test_tmux_session

  subprocess.run(
      [
          "tmux",
          "new-session",
          "-d",
          "-s",
          test_tmux_session,
          "-n",
          "alpha",
          "bash --norc -i",
      ],
      check=True,
  )

  # Populate a realistic transcript.jsonl for Technique 3 (apiView)
  cid = bridge.get_conversation_id("alpha")
  log_dir = tmp_path / "brain" / cid / ".system_generated" / "logs"
  log_dir.mkdir(parents=True)
  transcript_lines = [
      {
          "step_index": 1,
          "type": "USER_INPUT",
          "status": "DONE",
          "created_at": "2026-09-28T12:00:00Z",
          "content": "<USER_REQUEST>[Axoloctl Session: alpha | Worktree: /tmp] Filter devices by site</USER_REQUEST>",
      },
      {
          "step_index": 2,
          "type": "PLANNER_RESPONSE",
          "status": "DONE",
          "created_at": "2026-09-28T12:00:02Z",
          "content": "Filtered devices by site in the alpha worktree.",
      },
  ]
  (log_dir / "transcript.jsonl").write_text(
      "\n".join(json.dumps(x) for x in transcript_lines) + "\n",
      encoding="utf-8",
  )

  class CustomUIHostHandler(UIHostHandler):
    pass

  CustomUIHostHandler.config = cfg
  CustomUIHostHandler.sessions_dir = str(sessions_dir)
  CustomUIHostHandler.agent_bridge = bridge

  gateway_port = allocate_ephemeral_port()
  gateway_srv = ThreadingHTTPServer(("127.0.0.1", gateway_port), CustomUIHostHandler)
  threading.Thread(target=gateway_srv.serve_forever, daemon=True).start()
  opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

  try:
    # 1. Technique 1 (cliView): POST /api/cli sends command to tmux pane, GET /api/cli captures output
    cli_post = urllib.request.Request(
        f"http://127.0.0.1:{gateway_port}/api/cli",
        data=json.dumps({
            "tag": "alpha",
            "text": "echo AXOLOCTL_CLI_MARKER_42",
            "submit": True,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(cli_post, timeout=5) as resp:
      cli_res = json.loads(resp.read().decode("utf-8"))
      assert cli_res["ok"] is True
      assert cli_res["running"] is True
      assert "AXOLOCTL_CLI_MARKER_42" in cli_res["output"]

    # 2. Technique 2 (hubView): GET /api/hub/status returns Hub status and bound worktree
    with opener.open(
        f"http://127.0.0.1:{gateway_port}/api/hub/status?tag=alpha", timeout=5
    ) as resp:
      hub_res = json.loads(resp.read().decode("utf-8"))
      assert hub_res["tag"] == "alpha"
      assert hub_res["hub_port"] == 5387
      assert hub_res["workspace"].endswith("alpha/workspace/gummi")

    # 3. Technique 3 (apiView): GET /api/chat/messages parses transcript.jsonl & POST /api/chat/new rotates conversation
    with opener.open(
        f"http://127.0.0.1:{gateway_port}/api/chat/messages?tag=alpha", timeout=5
    ) as resp:
      chat_res = json.loads(resp.read().decode("utf-8"))
      assert chat_res["tag"] == "alpha"
      assert chat_res["conversation_id"] == cid
      assert len(chat_res["messages"]) == 2
      assert chat_res["messages"][0]["role"] == "user"
      assert chat_res["messages"][0]["content"] == "Filter devices by site"
      assert chat_res["messages"][1]["role"] == "assistant"
      assert (
          chat_res["messages"][1]["content"]
          == "Filtered devices by site in the alpha worktree."
      )
      assert chat_res["agent_state"]["status"] == "ready"
      assert chat_res["agent_state"]["is_busy"] is False

    new_req = urllib.request.Request(
        f"http://127.0.0.1:{gateway_port}/api/chat/new",
        data=json.dumps({"tag": "alpha"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(new_req, timeout=5) as resp:
      new_res = json.loads(resp.read().decode("utf-8"))
      assert new_res["ok"] is True
      assert new_res["conversation_id"] != cid
  finally:
    gateway_srv.shutdown()
    subprocess.run(
        ["tmux", "kill-session", "-t", test_tmux_session],
        check=False,
        stderr=subprocess.DEVNULL,
    )


def test_chat_activity_preflush_and_background_tasks(tmp_path):
  """Verifies continuous busy indicator across pre-flush gap, toolAction calls, and background tasks."""
  import time
  from ui_host import JetskiAgentBridge

  cfg = load_config(GUMMI_CONFIG_PATH, UDMI_ROOT)
  sessions_dir = tmp_path / "sessions"
  (sessions_dir / "alpha" / "workspace" / "gummi").mkdir(parents=True)
  (sessions_dir / "ports.json").write_text(
      json.dumps({"alpha": 9300}), encoding="utf-8"
  )

  bridge = JetskiAgentBridge(str(tmp_path), cfg, str(sessions_dir))
  bridge.brain_root = str(tmp_path / "brain")
  cid = bridge.get_conversation_id("alpha")
  log_dir = tmp_path / "brain" / cid / ".system_generated" / "logs"
  log_dir.mkdir(parents=True)
  tfile = log_dir / "transcript.jsonl"

  # Initial completed turn (steps 1-2)
  entries = [
      {
          "step_index": 1,
          "type": "USER_INPUT",
          "status": "DONE",
          "created_at": "2026-09-28T12:00:00Z",
          "content": "<USER_REQUEST>Initial prompt</USER_REQUEST>",
      },
      {
          "step_index": 2,
          "type": "PLANNER_RESPONSE",
          "status": "DONE",
          "created_at": "2026-09-28T12:00:02Z",
          "content": "Initial response.",
      },
  ]
  tfile.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

  # 1. Simulate pre-flush gap immediately after send_chat_prompt
  bridge._pending_turns["alpha"] = {
      "cid": cid,
      "prompt": "Update theme to forest green",
      "baseline_step": 2,
      "sent_at": time.time() - 3.0,
  }
  state_preflush = bridge.get_chat_messages("alpha")["agent_state"]
  assert state_preflush["is_busy"] is True
  assert state_preflush["status"] == "thinking"
  assert state_preflush["pending_prompt"] == "Update theme to forest green"
  assert state_preflush["elapsed_s"] >= 2

  # 2. Transcript flushes user SYSTEM_MESSAGE (step 3) + PLANNER_RESPONSE with toolAction (step 4)
  entries.extend([
      {
          "step_index": 3,
          "type": "SYSTEM_MESSAGE",
          "status": "DONE",
          "created_at": "2026-09-28T12:00:10Z",
          "content": "[Message] timestamp=2026-09-28T12:00:05Z sender=system content=Update theme to forest green\n</SYSTEM_MESSAGE>",
      },
      {
          "step_index": 4,
          "type": "PLANNER_RESPONSE",
          "status": "DONE",
          "created_at": "2026-09-28T12:00:10Z",
          "thinking": "Inspecting CSS variables in app.css first.",
          "tool_calls": [
              {
                  "name": "run_command",
                  "args": {
                      "toolAction": '"Running Playwright E2E browser tests"',
                      "toolSummary": '"Playwright E2E test run"',
                  },
              }
          ],
      },
  ])
  tfile.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

  state_tool = bridge.get_chat_messages("alpha")["agent_state"]
  assert state_tool["is_busy"] is True
  assert state_tool["status"] == "executing_tool"
  assert state_tool["active_tool"] == "Running Playwright E2E browser tests"
  assert state_tool["step_count"] == 1
  assert "Inspecting CSS variables" in state_tool["thinking"]

  # 3. Background task detaches (step 5 RUNNING) + intermediate PLANNER_RESPONSE without tool_calls (step 6)
  entries.extend([
      {
          "step_index": 5,
          "type": "GENERIC",
          "status": "RUNNING",
          "created_at": "2026-09-28T12:00:15Z",
          "content": f"Tool is running as a background task with task id: {cid}/task-5\nTask Description: pytest",
      },
      {
          "step_index": 6,
          "type": "PLANNER_RESPONSE",
          "status": "DONE",
          "created_at": "2026-09-28T12:00:17Z",
          "content": "Running Playwright browser test suite in the background...",
      },
  ])
  tfile.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

  state_bg = bridge.get_chat_messages("alpha")["agent_state"]
  assert state_bg["is_busy"] is True
  assert state_bg["status"] == "executing_tool"
  assert "Running Playwright E2E browser tests (background task)" == state_bg["active_tool"]

  # 4. Background task finishes (step 7 SYSTEM_MESSAGE) -> still busy while model evaluates result
  entries.append({
      "step_index": 7,
      "type": "SYSTEM_MESSAGE",
      "status": "DONE",
      "created_at": "2026-09-28T12:00:25Z",
      "content": f"<SYSTEM_MESSAGE>\n[Message] timestamp=2026-09-28T12:00:24Z sender={cid}/task-5 content=Task finished\n</SYSTEM_MESSAGE>",
  })
  tfile.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

  state_eval = bridge.get_chat_messages("alpha")["agent_state"]
  assert state_eval["is_busy"] is True
  assert state_eval["status"] == "thinking"

  # 5. Final PLANNER_RESPONSE (step 8) completes the turn -> transitions to ready
  entries.append({
      "step_index": 8,
      "type": "PLANNER_RESPONSE",
      "status": "DONE",
      "created_at": "2026-09-28T12:00:28Z",
      "content": "Theme updated and all Playwright tests passed.",
  })
  tfile.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

  state_done = bridge.get_chat_messages("alpha")["agent_state"]
  assert state_done["is_busy"] is False
  assert state_done["status"] == "ready"
  assert "alpha" not in bridge._pending_turns

  # 6. Uncorrelated tab (empty tag) never falls back to ports.json ('alpha')
  from ui_host import _render_ui_page
  uncorrelated_chat = bridge.get_chat_messages("")
  assert uncorrelated_chat["tag"] == ""
  assert uncorrelated_chat["conversation_id"] is None
  assert uncorrelated_chat["messages"] == []

  uncorrelated_html = _render_ui_page(cfg, "apiView", "Agent Chat", active_tag="").decode("utf-8")
  assert "No correlated viewer in active tab" in uncorrelated_html
  assert "udmi_axoloctl_agent:alpha" not in uncorrelated_html



