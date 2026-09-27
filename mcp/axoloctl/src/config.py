"""Strict configuration loader and validator for Axoloctl."""

from dataclasses import dataclass
import json
import os
from typing import Any, Dict, List


REQUIRED_CONFIG_KEYS = (
    "repo_path",
    "app_subpath",
    "entrypoint",
    "host_port",
    "webmcp_port",
    "session_port_base",
    "mcp_config",
    "uis",
)


@dataclass(frozen=True)
class UIItemConfig:
  id: str
  label: str
  path: str


@dataclass(frozen=True)
class AxoloctlConfig:
  config_path: str
  repo_path: str
  app_subpath: str
  entrypoint: str
  host_port: int
  webmcp_port: int
  session_port_base: int
  mcp_config_path: str
  mcp_servers: Dict[str, Dict[str, Any]]
  default_ui: str
  uis: List[UIItemConfig]
  port_env_var: str = ""

  @property
  def app_work_dir(self) -> str:
    return os.path.normpath(os.path.join(self.repo_path, self.app_subpath))


def _resolve_path(base_dir: str, raw_path: str) -> str:
  if os.path.isabs(raw_path):
    return os.path.normpath(raw_path)
  return os.path.normpath(os.path.abspath(os.path.join(base_dir, raw_path)))


def _is_git_repo(path: str) -> bool:
  if not os.path.isdir(path):
    return False
  if os.path.exists(os.path.join(path, ".git")):
    return True
  return (
      os.path.isfile(os.path.join(path, "HEAD"))
      and os.path.isdir(os.path.join(path, "objects"))
  )


def load_config(config_path: str, udmi_root: str) -> AxoloctlConfig:
  """Loads and strictly validates an Axoloctl JSON configuration file.

  Fails immediately if the file or any required attribute is missing or invalid.
  """
  if not config_path or not str(config_path).strip():
    raise ValueError(
        "Axoloctl configuration file is required. "
        "Pass an explicit config file path or set AXOLOCTL_CONFIG."
    )

  udmi_root_abs = os.path.abspath(udmi_root)
  resolved_config_path = _resolve_path(udmi_root_abs, config_path)
  if not os.path.isfile(resolved_config_path):
    raise ValueError(
        f"Axoloctl configuration file not found: {resolved_config_path}"
    )

  try:
    with open(resolved_config_path, "r", encoding="utf-8") as f:
      raw = json.load(f)
  except Exception as e:
    raise ValueError(
        f"Failed to parse Axoloctl config JSON at {resolved_config_path}: {e}"
    ) from e

  if not isinstance(raw, dict):
    raise ValueError("Axoloctl config root must be a JSON object.")

  missing = [k for k in REQUIRED_CONFIG_KEYS if k not in raw or raw[k] is None]
  if missing:
    raise ValueError(
        f"Axoloctl config at {resolved_config_path} is missing required keys: "
        f"{', '.join(missing)}"
    )

  repo_raw = raw["repo_path"]
  if not isinstance(repo_raw, str) or not repo_raw.strip():
    raise ValueError("'repo_path' must be a non-empty string.")
  repo_path = _resolve_path(udmi_root_abs, repo_raw)
  if not _is_git_repo(repo_path):
    raise ValueError(
        f"Configured 'repo_path' does not exist or is not a Git repository: {repo_path}"
    )

  app_subpath = raw["app_subpath"]
  if not isinstance(app_subpath, str) or not app_subpath.strip():
    raise ValueError("'app_subpath' must be a non-empty string.")
  if os.path.isabs(app_subpath):
    raise ValueError("'app_subpath' must be a relative path inside 'repo_path'.")

  app_dir = os.path.normpath(os.path.join(repo_path, app_subpath))
  if not os.path.isdir(app_dir):
    raise ValueError(
        f"Configured 'app_subpath' directory does not exist in repo: {app_dir}"
    )

  entrypoint = raw["entrypoint"]
  if not isinstance(entrypoint, str) or not entrypoint.strip():
    raise ValueError("'entrypoint' must be a non-empty string.")
  if os.path.isabs(entrypoint):
    raise ValueError("'entrypoint' must be a relative path inside 'app_subpath'.")

  for port_key in ("host_port", "webmcp_port", "session_port_base"):
    val = raw[port_key]
    if not isinstance(val, int) or isinstance(val, bool) or val <= 0 or val > 65535:
      raise ValueError(f"'{port_key}' must be a valid TCP port integer (1-65535).")

  mcp_config_raw = raw["mcp_config"]
  if not isinstance(mcp_config_raw, str) or not mcp_config_raw.strip():
    raise ValueError("'mcp_config' must be a non-empty string path.")
  mcp_config_path = _resolve_path(udmi_root_abs, mcp_config_raw)
  if not os.path.isfile(mcp_config_path):
    raise ValueError(f"Configured 'mcp_config' file not found: {mcp_config_path}")

  try:
    with open(mcp_config_path, "r", encoding="utf-8") as f:
      mcp_raw = json.load(f)
  except Exception as e:
    raise ValueError(
        f"Failed to parse MCP config JSON at {mcp_config_path}: {e}"
    ) from e

  mcp_servers = mcp_raw.get("mcpServers") if isinstance(mcp_raw, dict) else None
  if not isinstance(mcp_servers, dict) or not mcp_servers:
    raise ValueError(
        f"MCP config at {mcp_config_path} must define a non-empty 'mcpServers' object."
    )

  uis_raw = raw["uis"]
  if not isinstance(uis_raw, dict):
    raise ValueError("'uis' must be a JSON object containing 'default_ui' and 'items'.")

  default_ui = uis_raw.get("default_ui")
  items_raw = uis_raw.get("items")
  if not isinstance(default_ui, str) or not default_ui.strip():
    raise ValueError("'uis.default_ui' must be a non-empty string.")
  if not isinstance(items_raw, list) or not items_raw:
    raise ValueError("'uis.items' must be a non-empty list of UI definitions.")

  ui_items: List[UIItemConfig] = []
  ui_ids = set()
  for item in items_raw:
    if not isinstance(item, dict):
      raise ValueError("Each entry in 'uis.items' must be a JSON object.")
    ui_id = item.get("id")
    ui_label = item.get("label")
    ui_path = item.get("path")
    if not all(isinstance(v, str) and v.strip() for v in (ui_id, ui_label, ui_path)):
      raise ValueError(
          "Each entry in 'uis.items' must define non-empty string 'id', 'label', and 'path'."
      )
    if not ui_path.startswith("/"):
      raise ValueError(f"UI path '{ui_path}' for '{ui_id}' must start with '/'.")
    ui_items.append(UIItemConfig(id=ui_id, label=ui_label, path=ui_path))
    ui_ids.add(ui_id)

  if default_ui not in ui_ids:
    raise ValueError(
        f"'uis.default_ui' ('{default_ui}') does not match any configured UI id in {sorted(ui_ids)}."
    )

  port_env_var = raw.get("port_env_var", "")
  if port_env_var and not isinstance(port_env_var, str):
    raise ValueError("'port_env_var' must be a string if provided.")

  return AxoloctlConfig(
      config_path=resolved_config_path,
      repo_path=repo_path,
      app_subpath=app_subpath,
      entrypoint=entrypoint,
      host_port=raw["host_port"],
      webmcp_port=raw["webmcp_port"],
      session_port_base=raw["session_port_base"],
      mcp_config_path=mcp_config_path,
      mcp_servers=mcp_servers,
      default_ui=default_ui,
      uis=ui_items,
      port_env_var=port_env_var.strip(),
  )
