"""UDMI Unified UDMI Functional Interface (UUFI) MCP Server Package.

Encapsulates external-facing messaging transport (MQTT/UUFI), Layer 1 service handshakes,
device configuration mutations, system model operations, live state queries, and
event stream ingress according to the authoritative UUFI specification (docs/specs/uufi.md).
"""

from mcp.uufi.client import UUFIClient
from mcp.uufi.provider import UUFIProvider

__all__ = ["UUFIProvider", "UUFIClient"]
