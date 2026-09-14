"""vnflight — LLM bridge for Ren'Py visual novels."""

# The one version constant.  cli.py (--version), mcp.py (serverInfo),
# bridge.py (/status) read it; build_vnflight.py copies this line into the
# single-file artifact so the two never disagree.
__version__ = "0.9.1"

from .client import BridgeClient, WaitResult
from .format import (
    build_wait_data,
    build_state_data,
    format_wait_text,
    format_state_text,
    format_pending_text,
)

# Lazy imports for functions used by hub.py and other consumers.
# lib.py attrs are resolved from lib; CLI/format attrs from their modules.
def __getattr__(name):
    _LIB_ATTRS = {
        "ClientState", "launch_game", "stop_game", "discover_games",
        "generate_prompt", "kill_process", "_is_process_alive",
        "_http_request", "_find_project_root", "_load_config",
    }
    if name in _LIB_ATTRS:
        from . import lib as _lib
        return getattr(_lib, name)
    _CLI_ATTRS = {
        "_apply_profile",
    }
    if name in _CLI_ATTRS:
        from . import cli as _cli
        return getattr(_cli, name)
    _FMT_ATTRS = {
        "format_event", "format_events",
        "format_pending_request", "format_screen_buttons",
        "format_interactions", "format_main_menu",
    }
    if name in _FMT_ATTRS:
        from . import format as _fmt
        return getattr(_fmt, name)
    raise AttributeError(f"module 'vnflight' has no attribute {name!r}")

__all__ = [
    "BridgeClient",
    "WaitResult",
    "build_wait_data",
    "build_state_data",
    "format_wait_text",
    "format_state_text",
    "format_pending_text",
]
