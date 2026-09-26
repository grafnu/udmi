import os
import sys
import pytest

UDMI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(UDMI_ROOT, "mcp", "axoloctl", "src"))

from session_manager import SessionManager

def test_session_manager_init():
    mgr = SessionManager(UDMI_ROOT)
    assert mgr.udmi_root == UDMI_ROOT
    assert os.path.exists(mgr.sessions_dir)
    assert os.path.exists(mgr.shared_dir)

def test_hash_validation():
    mgr = SessionManager(UDMI_ROOT)
    valid_hash = "a" * 40
    invalid_hash = "a" * 39
    
    assert mgr._is_valid_hash(valid_hash) is True
    assert mgr._is_valid_hash(invalid_hash) is False
    assert mgr._is_valid_hash("HEAD") is False
