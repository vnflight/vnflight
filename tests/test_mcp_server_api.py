"""The MCP server relies on decorator hooks that exist only in mcp 1.x.

The unit tests build the tool table without a real ``mcp.server.Server``,
so a CI run with mcp 2.x stays green while ``vnflight.py mcp`` dies at start
with ``AttributeError: 'Server' object has no attribute 'list_tools'`` (seen
with mcp 2.2.0 on a Linux from-zero run).  This test asks the installed
package for the hooks ``run_server`` actually uses, so loosening the pin in
requirements.txt fails here instead of in a user's terminal.
"""
from pathlib import Path
import re

import pytest

mcp_server = pytest.importorskip("mcp.server")

_ROOT = Path(__file__).resolve().parents[1]


def _hooks_used_by_run_server() -> set[str]:
    source = (_ROOT / "src" / "vnflight" / "mcp.py").read_text(encoding="utf-8")
    hooks = set(re.findall(r"@server\.([a-z_]+)\(\)", source))
    assert hooks, "run_server no longer registers hooks with @server.<hook>()"
    return hooks


def test_installed_mcp_has_the_decorator_hooks_run_server_uses():
    server = mcp_server.Server("vnflight-test")
    missing = sorted(h for h in _hooks_used_by_run_server()
                     if not callable(getattr(server, h, None)))
    assert not missing, (
        f"mcp.server.Server lacks {missing}; the installed mcp version is not "
        "one the server supports (requirements.txt pins mcp>=1.26,<2)"
    )


def test_requirements_pin_mcp_to_1x():
    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^mcp>=1\.\d+,<2\s*$", text, re.M), text
