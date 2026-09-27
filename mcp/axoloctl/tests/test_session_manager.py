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


def test_webmcp_protocol_handshake(tmp_path):
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
  tool_names = {t["name"] for t in tools_res["result"]["tools"]}
  assert tool_names == {
      "start_server",
      "stop_server",
      "get_status",
      "list_servers",
      "read_logs",
  }
