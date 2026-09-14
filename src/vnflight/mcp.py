"""Simple MCP server for vnflight.

Exposes game tools (wait, act, screenshot, state, etc.) via the
Model Context Protocol.  No hub, no agents, no tavern — just a
user or LLM playing a visual novel through any MCP client.

Usage:
    vnflight mcp [--bridge URL] [--game GAME_ID] [--slot SLOT]
    python -m vnflight.mcp [--bridge URL] [--game GAME_ID] [--slot SLOT]
"""

from __future__ import annotations

import argparse
import copy
import inspect
import json
import secrets
import sys
import time
from typing import Any

from . import __version__
from .client import BridgeClient
from .format import ANOMALY_VISIBILITY_MODES
from .client import _is_dynamic_slot_selector
from .handlers import (
    HandlerContext,
    Hooks,
    handle_stop,
    handle_tool,
    render_tool_result_text,
    run_presentation_transition,
    strip_internal_result_fields,
)


_MCP_TOOL_RESPONSE_BUDGET_S = 115.0
_MCP_TOOL_MUTATION_BUDGET_S = 110.0
_TRANSPORT_BOUNDED_TOOLS = {
    "wait", "act", "input_text", "back", "back_all", "auto_skip", "save",
    "load", "advance", "rewind", "replay", "reset", "resync", "command",
    "launch", "stop", "set_profile",
}


# Tools that act on a running game.  Without a bound slot they used to run
# anyway and answer state_unavailable / (no new events) for the whole
# budget; a user who followed "mcp --game X" (which only connects) saw 100 s
# of nothing.  They are refused up front with the way out instead.
_TOOLS_NEEDING_A_GAME = {
    "wait", "act", "input_text", "state", "transcript", "screenshot",
    "back", "back_all", "advance", "rewind", "replay", "save", "load",
    "auto_skip", "set_profile", "inspect", "progress", "command", "reset",
    "resync",
}


def unbound_game_error(
    tool: str, game_hint: str | None, active_tool_names,
) -> dict:
    """The refusal a game-bound tool returns when no game is running."""
    from .lib import cli_command_hint
    game = game_hint or "<game>"
    cli = cli_command_hint()
    if "launch" in set(active_tool_names or ()):
        way_out = (
            f"Call launch(game_id=\"{game}\") first, or run "
            f"`{cli} launch {game}` and retry."
        )
    else:
        way_out = (
            f"Run `{cli} launch {game}` first and retry, or "
            "start the server with --capabilities lifecycle (or all) and "
            f"call launch(game_id=\"{game}\")."
        )
    return {
        "error": "no_game_bound",
        "tool": tool,
        "game": game_hint,
        "message": (
            f"No running game is bound to this MCP server"
            f"{' for ' + repr(game_hint) if game_hint else ''}. "
            "`mcp --game` connects to a running game; it does not launch "
            "one. " + way_out
        ),
    }


def stop_owned_games(ctx, timeout: float = 12.0):
    """Stop the games launched on a private (owned) bridge before that
    bridge goes away with its MCP server.  Terminating only the bridge
    left the game running with nothing to reach it (and nothing for
    `stop` to find)."""
    if ctx is None:
        return None
    try:
        return handle_stop(ctx, {"_transport_deadline": time.time() + timeout})
    except Exception as exc:  # noqa: BLE001 - best effort at exit
        return {"error": str(exc)}


def stamp_act_invocation(
    arguments: dict, server_instance_id: str,
) -> dict:
    """Attach diagnostic-only provenance to one MCP act invocation."""
    stamped = dict(arguments)
    stamped["_mcp_server_instance_id"] = server_instance_id
    stamped["_mcp_call_id"] = secrets.token_hex(16)
    stamped["_mcp_original_target"] = arguments.get("target")
    return stamped


def apply_private_launch_policy(
    name: str, arguments: dict, *, shared_bridge: bool,
) -> dict:
    """Require owned launch replacement independent of current attachment."""
    if name != "launch" or shared_bridge:
        return arguments
    prepared = dict(arguments)
    prepared["_stop_existing"] = True
    return prepared


def _mcp_result_text(
    name: str,
    arguments: dict,
    result: object,
    default_format: str,
) -> str:
    """Render text output before private chronology metadata is stripped."""
    fmt = arguments.get("format") or default_format
    act_has_wait = name in ("act", "input_text") and isinstance(result, dict) and "wait" in result
    if (
        (name in ("wait", "state") or act_has_wait)
        and fmt in ("text", "quiet")
        and isinstance(result, dict)
    ):
        return render_tool_result_text(result)
    public = strip_internal_result_fields(result) if isinstance(
        result, dict) else result
    return json.dumps(public, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------

CAPABILITY_DEFAULT = "play"
CAPABILITIES = {"play", "lifecycle", "diagnostic", "admin"}
DEBUG_CAPABILITIES = {"diagnostic", "admin"}

_TOOLS = [
    {
        "name": "wait",
        "capability": "play",
        "description": (
            "Watch the story unfold until a decision point (choice or text input). "
            "Returns dialogue, narration, and any pending choice. "
            "While a panel (e.g. KIT/LOG) is open, the story menu beneath is "
            "hidden — use the panel's buttons or CLOSE it; a refusal will "
            "say 'hidden behind <PANEL>'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to wait (default 60)",
                    "default": 60,
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "json", "quiet"],
                    "description": "Output format: text (default), json (structured), quiet (minimal)",
                },
                "action_nonce": {
                    "type": "string",
                    "description": (
                        "Recover and drain one interrupted act transaction. "
                        "Normally omitted; plain wait selects this client's "
                        "oldest undrained action automatically."
                    ),
                },
            },
        },
    },
    {
        "name": "act",
        "capability": "play",
        "description": (
            # Fresh agents guessed label= / action= on their first call
            # (R64: 3 of 12), so the very first thing this says is the call
            # shape with the argument named.
            "Call as act(target=\"3\") - the one required argument is "
            "`target`: a number from the last rendered choice list (1-based, "
            "as a string), or the text of a choice or screen button "
            "(e.g. act(target=\"Leave the comms room\")). "
            "By default, the tool waits for "
            "the next story events. If an accepted action is still settling, "
            "continue it with wait(action_nonce=...) using the returned nonce. "
            "While a panel (e.g. KIT/LOG) is open, the story menu beneath is "
            "hidden — use the panel's buttons or CLOSE it; a refusal will "
            "say 'hidden behind <PANEL>'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Choice number (1-based), choice label, or button name",
                },
                "wait": {
                    "type": "boolean",
                    "description": "Wait for story events after acting (default true)",
                },
                "action_nonce": {
                    "type": "string",
                    "description": (
                        "Stable client identity for retrying the same logical "
                        "action. Normally generated automatically."
                    ),
                },
                "accept_timeout": {
                    "type": "number",
                    "description": "Seconds to wait for bridge acceptance (default 15)",
                    "default": 15,
                },
                "result_timeout": {
                    "type": "number",
                    "description": (
                        "Action-lifecycle seconds covering preflight, bridge "
                        "acceptance, and settlement. Required stream-"
                        "presentation drain time is excluded. Total tool wall "
                        "time may be longer. The lifecycle budget is capped "
                        "by the remaining MCP transport/mutation "
                        "budget; timeout is also accepted as a compatibility "
                        "alias."
                    ),
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "input_text",
        "capability": "play",
        "description": (
            "Type text when the game asks for input (e.g. character name). "
            "By default, automatically waits for the next story events after "
            "submitting -- no separate wait call is needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to enter",
                },
                "wait": {
                    "type": "boolean",
                    "description": "Wait for story events after submitting (default true)",
                },
                "request_id": {
                    "type": "string",
                    "description": (
                        "Retry only: reuse the request_id from an "
                        "acceptance-unknown input result with identical text."
                    ),
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "screenshot",
        "capability": "play",
        "description": "Take a screenshot of the current game screen.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "state",
        "capability": "play",
        "description": (
            "Check the current game state. Returns a brief stats summary "
            "by default. Use brief=false for full inventory and config. "
            "While a panel (e.g. KIT/LOG) is open, the story menu beneath is "
            "hidden — use the panel's buttons or CLOSE it; a refusal will "
            "say 'hidden behind <PANEL>'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "brief": {
                    "type": "boolean",
                    "description": "Brief one-liner stats (default true)",
                    "default": True,
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "json", "quiet"],
                    "description": "Output format: text (default), json (structured), quiet (minimal)",
                },
            },
        },
    },
    {
        "name": "transcript",
        "capability": "play",
        "description": (
            "Review rendered dialogue and events. Results include opaque "
            "first_cursor/last_cursor values for stable history paging."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "last": {
                    "type": "integer",
                    "description": "Number of rendered events (default 20)",
                    "default": 20,
                },
                "before": {
                    "type": "string",
                    "description": (
                        "Return events before an earlier first_cursor. "
                        "Mutually exclusive with after."
                    ),
                },
                "after": {
                    "type": "string",
                    "description": (
                        "Return events after an earlier last_cursor. "
                        "Mutually exclusive with before."
                    ),
                },
            },
        },
    },
    {
        "name": "back",
        "capability": "play",
        "description": (
            "Close the current overlay/menu/modal screen (sends Escape/Return). "
            "Does not move through dialogue history. Refuses on a custom "
            "`call screen` console it cannot safely dismiss for you (the "
            "error names the screen) -- act() on its visible Close/Release "
            "control, or use rewind() to roll back through it instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command_nonce": {
                    "type": "string",
                    "description": (
                        "Reuse only after an acceptance-unknown result for "
                        "this same tool and identical arguments."
                    ),
                },
            },
        },
    },
    {
        "name": "back_all",
        "capability": "play",
        "description": (
            "Close overlay/menu screens one after another until the world "
            "screen is back (bounded), then return the current state. Use "
            "it after you have read what you opened, e.g. an inventory item "
            "detail two menus deep. Reverses navigation only: it never picks "
            "a choice or runs a forward action, and it stops (with the "
            "shim's reason) on a custom called screen, a modal it cannot "
            "safely close, or a live choice."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "advance",
        "capability": "play",
        "description": (
            "Advance one dialogue interaction, like pressing Space/click once. "
            "Does not enable auto-forward and should not be used to pick choices."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command_nonce": {
                    "type": "string",
                    "description": (
                        "Reuse only after an acceptance-unknown result for "
                        "this same tool and identical arguments."
                    ),
                },
            },
        },
    },
    {
        "name": "rewind",
        "capability": "play",
        "description": (
            "Move one normal dialogue/checkpoint backward using Ren'Py rollback "
            "-- this is the tool that UNDOES a story choice. This also rolls "
            "back through a custom `call screen` console back() cannot safely "
            "close for you; use back() instead only for plain registered "
            "overlays/menus, which it dismisses without touching story history."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command_nonce": {
                    "type": "string",
                    "description": (
                        "Reuse only after an acceptance-unknown result for "
                        "this same tool and identical arguments."
                    ),
                },
            },
        },
    },
    {
        "name": "replay",
        "capability": "play",
        "description": (
            "Roll forward after rewind/backward dialogue navigation. "
            "Only works when Ren'Py has roll-forward history available."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command_nonce": {
                    "type": "string",
                    "description": (
                        "Reuse only after an acceptance-unknown result for "
                        "this same tool and identical arguments."
                    ),
                },
            },
        },
    },
    {
        "name": "save",
        "capability": "play",
        "description": "Save the game. Optionally specify a slot name.",
        "parameters": {
            "type": "object",
            "properties": {
                "slot": {
                    "type": "string",
                    "description": (
                        "Save slot/alias. Omit with no name for the default save slot; "
                        "omit with name to derive a distinct named slot."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Display name for the save. If slot is omitted, "
                        "also used to derive the save slot alias."
                    ),
                },
                "command_nonce": {
                    "type": "string",
                    "description": (
                        "Reuse only after an acceptance-unknown result for "
                        "this same tool and identical arguments."
                    ),
                },
            },
        },
    },
    {
        "name": "load",
        "capability": "play",
        "description": "Load a saved game. Optionally specify a slot name.",
        "parameters": {
            "type": "object",
            "properties": {
                "slot": {
                    "type": "string",
                    "description": "Save slot name (omit to load the newest save)",
                },
                "command_nonce": {
                    "type": "string",
                    "description": (
                        "Reuse only after an acceptance-unknown result for "
                        "this same tool and identical arguments."
                    ),
                },
            },
        },
    },
    # Lifecycle tools.
    {
        "name": "launch",
        "capability": "lifecycle",
        "description": (
            "Launch a visual novel game with the vnflight bridge. "
            "Starts the bridge server (if needed) and the game process."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {
                    "type": "string",
                    "description": "Game ID from vnflight config (e.g. 'mystic_cafe')",
                },
                "profile": {
                    "type": "string",
                    "description": "Timing profile to apply after launch (e.g. 'turbo')",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Connection timeout in seconds (default 30)",
                    "default": 30,
                },
                "debug": {
                    "type": "boolean",
                    "description": (
                        "Override the game's vnflight.json 'debug' setting for "
                        "this launch: shim command/action logging to the game's "
                        "debug_logs directory (per session)."
                    ),
                },
            },
            "required": ["game_id"],
        },
    },
    {
        "name": "stop",
        "capability": "lifecycle",
        "description": "Stop a running game (or all games if no game specified).",
        "parameters": {
            "type": "object",
            "properties": {
                "game": {
                    "type": "string",
                    "description": "Game ID or slot to stop (omit to stop all)",
                },
            },
        },
    },
    {
        "name": "games",
        "capability": "lifecycle",
        "description": "List all available visual novel games configured in vnflight.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "set_profile",
        "capability": "play",
        "description": "Apply a timing profile to the current game (e.g. 'turbo', 'default').",
        "parameters": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "string",
                    "description": "Profile name",
                },
                "command_nonce": {
                    "type": "string",
                    "description": (
                        "Reuse only after an acceptance-unknown result for "
                        "this same profile."
                    ),
                },
            },
            "required": ["profile"],
        },
    },
    {
        "name": "auto_skip",
        "capability": "play",
        "description": (
            "Toggle auto-skip for single-choice menus. When enabled (default), "
            "single-choice 'continue' prompts are resolved automatically. "
            "Disable when you need to interact with screen buttons (Travel, Map). "
            "Omit 'enabled' to query current state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "True to enable, False to disable. Omit to query.",
                },
                "command_nonce": {
                    "type": "string",
                    "description": (
                        "Reuse only after an acceptance-unknown result for "
                        "this same mode and identical arguments."
                    ),
                },
            },
        },
    },
    # Output format control.
    {
        "name": "set_format",
        "capability": "play",
        "description": (
            "Set the default output format for wait/state results, and how "
            "much engine diagnostics you see. format: 'text' (readable), "
            "'json' (structured dicts), or 'quiet' (text without nav/info). "
            "anomalies: 'errors' (default; report engine failures such as a "
            "Ren'Py exception screen), 'all' (also scrape diagnostics like "
            "duplicate buttons), or 'off'. Pass either argument alone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["text", "json", "quiet"],
                    "description": "Output format",
                },
                "anomalies": {
                    "type": "string",
                    "enum": ["off", "errors", "all"],
                    "description": "Engine anomaly reporting level",
                },
            },
        },
    },
    # Bridge management (standalone MCP only).
    {
        "name": "bridge_connect",
        "capability": "lifecycle",
        "description": (
            "Switch to a different bridge or start a new private one. "
            "Use 'url' to connect to an existing bridge, or 'start' to "
            "launch a new bridge on a free port. Games launched after "
            "this will connect to the new bridge."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Bridge URL to connect to (e.g. http://127.0.0.1:8385)",
                },
                "start": {
                    "type": "boolean",
                    "description": "Start a new private bridge on a free port",
                },
            },
        },
    },
]

# Diagnostic/admin tools — opt-in via --capabilities or legacy --debug.
_DEBUG_TOOLS = [
    {
        "name": "inspect",
        "capability": "diagnostic",
        "description": (
            "Debug: show raw game state including screens, widgets, button coordinates, "
            "and pending request details."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "progress",
        "capability": "diagnostic",
        "description": (
            "Check game progress — story beats visited, key choices made, "
            "current phase, and endings reached. Requires a game-specific "
            "progress mod. WARNING: may contain spoilers."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "command",
        "capability": "admin",
        "description": "Send a raw game command (e.g. 'save', 'load', 'eval'). Debug use only.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Command name",
                },
                "args": {
                    "type": "object",
                    "description": "Command arguments",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "save_scan",
        "capability": "diagnostic",
        "description": (
            "Read-only diagnostic: scan Ren'Py .save files for likely vnflight/"
            "vnf_/llm shim references. Results are warnings, not proof of safety."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ren'Py .save file or directory to scan",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Recurse into subdirectories when scanning a directory",
                    "default": False,
                },
            },
            "required": ["path"],
        },
    },
]


def parse_capabilities(value: str | None, *, debug: bool = False) -> set[str]:
    """Parse MCP tool capability names."""
    caps = {CAPABILITY_DEFAULT}
    if value:
        caps = {
            item.strip().lower()
            for item in value.split(",")
            if item.strip()
        }
    if "all" in caps:
        caps = set(CAPABILITIES)
    unknown = caps - CAPABILITIES
    if unknown:
        raise ValueError(f"Unknown MCP capabilities: {', '.join(sorted(unknown))}")
    if debug:
        caps |= DEBUG_CAPABILITIES
    return caps


def tools_for_capabilities(capabilities: set[str]) -> list[dict[str, Any]]:
    """Return MCP tool definitions exposed for the selected capabilities."""
    all_tools = _TOOLS + _DEBUG_TOOLS
    return [
        tool for tool in all_tools
        if tool.get("capability", CAPABILITY_DEFAULT) in capabilities
    ]


def parse_tool_allowlist(value: str | None) -> set[str] | None:
    """Parse an optional comma-separated MCP tool allowlist."""
    if not value:
        return None
    return {
        item.strip()
        for item in value.split(",")
        if item.strip()
    }


def select_tools(
    capabilities: set[str],
    tool_allowlist: str | None = None,
) -> list[dict[str, Any]]:
    """Return enabled tools after capability filtering and optional allowlist."""
    capability_tools = tools_for_capabilities(capabilities)
    requested = parse_tool_allowlist(tool_allowlist)
    if requested is None:
        return capability_tools

    all_tool_names = {tool["name"] for tool in _TOOLS + _DEBUG_TOOLS}
    unknown = requested - all_tool_names
    if unknown:
        raise ValueError(f"Unknown MCP tools: {', '.join(sorted(unknown))}")

    enabled_names = {tool["name"] for tool in capability_tools}
    disabled = requested - enabled_names
    if disabled:
        raise ValueError(
            "MCP tools not enabled by selected capabilities: "
            + ", ".join(sorted(disabled))
        )
    return [tool for tool in capability_tools if tool["name"] in requested]


def personalize_profile_tools(
    tools: list[dict[str, Any]],
    profiles: object,
) -> list[dict[str, Any]]:
    """Make profile-related MCP schemas reflect the loaded configuration.

    Capability and explicit allowlist filtering happen first.  This pass only
    describes functionality that is actually usable in the current process:
    without configured profiles ``set_profile`` is omitted and launch does not
    advertise a profile override.  A copied schema is returned so the module's
    static definitions remain reusable across MCP sessions and tests.
    """
    if isinstance(profiles, dict):
        names = sorted(
            str(name) for name, values in profiles.items()
            if isinstance(values, dict) and str(name) != "default"
        )
    else:
        names = []

    personalized = copy.deepcopy(tools)
    if not names:
        result = []
        for tool in personalized:
            if tool.get("name") == "set_profile":
                continue
            if tool.get("name") == "launch":
                properties = tool.get("parameters", {}).get("properties", {})
                properties.pop("profile", None)
            result.append(tool)
        return result

    choices = ["default", *names]
    display = ", ".join(choices)
    for tool in personalized:
        if tool.get("name") == "set_profile":
            tool["description"] = (
                "Apply a configured timing profile to the current game. "
                f"Available: {display}."
            )
            profile_schema = tool["parameters"]["properties"]["profile"]
            profile_schema["enum"] = choices
            profile_schema["description"] = (
                f"Configured profile name ({display})"
            )
        elif tool.get("name") == "launch":
            profile_schema = tool["parameters"]["properties"].get("profile")
            if profile_schema is not None:
                profile_schema["enum"] = choices
                profile_schema["description"] = (
                    f"Timing profile to apply after launch ({display})"
                )
    return personalized


def personalize_profile_tools_for_config(
    tools: list[dict[str, Any]],
    config: object,
    config_error: object = None,
) -> list[dict[str, Any]]:
    """Personalize profile schemas, preserving generic tools on read errors.

    A valid config with no profiles means profile operations are unavailable.
    An unreadable config is different: startup could not establish that fact,
    so retain the generic schema rather than silently removing tools for the
    lifetime of the MCP process.
    """
    if config_error:
        return copy.deepcopy(tools)
    profiles = config.get("profiles") if isinstance(config, dict) else {}
    return personalize_profile_tools(tools, profiles or {})


# ---------------------------------------------------------------------------
# MCP server (requires `mcp` package)
# ---------------------------------------------------------------------------

def mcp_server_info() -> dict:
    """Name and version the MCP server announces in serverInfo.

    The version is the package's __version__, not the mcp library's (which
    is what an unversioned Server() reports).
    """
    return {"name": "vnflight", "version": __version__}


def _find_free_port() -> int:
    """Find a free TCP port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_owned_bridge(port: int, admin_token: str | None = None) -> "subprocess.Popen | None":
    """Start a bridge subprocess owned by this MCP server. Returns the process or None."""
    import os
    import subprocess
    try:
        from .lib import (
            _find_bridge_script,
            _find_project_root,
            _single_file_artifact,
            _BRIDGE_MODULE_SENTINEL,
        )
    except (ImportError, ModuleNotFoundError):
        return None
    script = _find_bridge_script()
    if not script:
        return None
    root = _find_project_root()
    env = os.environ.copy()
    token_args = ["--token=" + admin_token] if admin_token else []
    artifact = _single_file_artifact()
    if script == _BRIDGE_MODULE_SENTINEL and artifact is not None:
        # Built single-file deployment: the artifact itself hosts the
        # bridge subcommand — there is no vnflight package to -m into.
        # (Same fix _launch_game in lib.py already has.)
        cmd = [sys.executable, str(artifact), "bridge", "--host", "127.0.0.1",
               "--port", str(port), *token_args]
    elif script == _BRIDGE_MODULE_SENTINEL:
        cmd = [sys.executable, "-m", "vnflight.bridge", "--host", "127.0.0.1",
               "--port", str(port), *token_args]
        # Ensure the package is importable when vnflight.py shadows it.
        if root:
            _src = os.path.join(str(root), "src")
            if os.path.isdir(os.path.join(_src, "vnflight")):
                env["PYTHONPATH"] = _src + os.pathsep + env.get("PYTHONPATH", "")
    else:
        cmd = [sys.executable, script, "--host", "127.0.0.1", "--port", str(port),
               *token_args]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(root) if root else None,
            env=env,
        )
        # Wait for bridge to become ready.
        import time
        client = BridgeClient(f"http://127.0.0.1:{port}")
        for _ in range(30):
            time.sleep(0.3)
            if client.is_up():
                return proc
        # Didn't come up — kill it.
        proc.kill()
        return None
    except Exception:
        return None


def run_server(
    bridge_url: str | None = None,
    game: str | None = None,
    slot: str | int | None = None,
    debug: bool = False,
    token: str | None = None,
    capabilities: str | None = None,
    tools: str | None = None,
    **kwargs,
) -> None:
    """Start a stdio MCP server.

    If bridge_url is None, starts a private bridge on a free port
    (cleaned up on exit). If bridge_url is provided, connects to
    an existing shared bridge.
    """
    try:
        import anyio
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import ImageContent, TextContent, Tool as MCPTool
    except ImportError:
        print("Error: 'mcp' package required. Install with: pip install mcp",
              file=sys.stderr)
        sys.exit(1)

    print(f"vnflight {__version__} MCP server starting "
          f"(bridge: {bridge_url or 'owned, starting one'})",
          file=sys.stderr, flush=True)

    # Start owned bridge if no URL provided.
    owned_bridge = None
    if not bridge_url:
        import os as _os
        import secrets as _secrets
        port = _find_free_port()
        # Mint an admin token for the owned bridge so this MCP server
        # (and the CLI subprocesses it spawns for launch/stop, which
        # read VNFLIGHT_TOKEN from the environment) can access slots
        # that get reserved at game launch.
        owned_token = token or _secrets.token_urlsafe(16)
        owned_bridge = _start_owned_bridge(port, admin_token=owned_token)
        if owned_bridge:
            bridge_url = f"http://127.0.0.1:{port}"
            token = owned_token
            _os.environ["VNFLIGHT_TOKEN"] = owned_token
            print(f"Started bridge on port {port} (PID {owned_bridge.pid})",
                  file=sys.stderr, flush=True)
        else:
            # Fall back to default port.
            bridge_url = "http://127.0.0.1:8385"
            print("Could not start bridge, using default port 8385",
                  file=sys.stderr, flush=True)

    import atexit
    _owned_bridge_ref = [owned_bridge]  # Mutable ref for atexit + _state sync.
    _ctx_ref = [None]  # The HandlerContext, once built below.
    def _cleanup():
        proc = _owned_bridge_ref[0]
        if proc and proc.poll() is None:
            # The bridge is ours and about to die: take its games with it.
            stop_owned_games(_ctx_ref[0])
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    atexit.register(_cleanup)

    # Token parity with the CLI: an explicit --token (or the owned bridge's
    # admin token, set above) wins; otherwise adopt the admin token this
    # machine stored for this bridge (a require-token harness/shared bridge
    # persists it in ClientState at game launch).  Without this, the launch
    # subprocess authenticates via the stored token and reserves the slot,
    # but this long-lived client stays tokenless and 403s on its own
    # act/state/screenshot calls.  BridgeClient still applies the
    # VNFLIGHT_TOKEN env fallback on top of a None here.
    if token is None:
        try:
            from .lib import resolve_stored_admin_token
            token = resolve_stored_admin_token(bridge_url)
        except Exception:
            token = None

    client = BridgeClient(bridge_url, slot=slot, token=token)
    server_instance_id = secrets.token_hex(16)

    def _warn_mark_current_events_seen(error: str) -> None:
        print(
            f"Warning: mark_current_events_seen failed: {error}",
            file=sys.stderr,
            flush=True,
        )

    def _attach_to_running_slot() -> None:
        client.attach_to_running_slot(warn=_warn_mark_current_events_seen)

    def _rebind_after_bridge_connect() -> None:
        """Rebind the client after switching bridges.

        A dynamic selector such as ``latest:roadwarden`` is useful at MCP
        startup, but it can become stale after ``bridge_connect`` points the
        same MCP process at a different bridge or a different live game.  Do
        not let an unresolved dynamic selector become a literal slot path.
        Explicit slot ids are stricter: if they are absent on the new bridge,
        keep the client unbound rather than silently acting on another slot.
        """
        client.slot_prefix = ""
        selected_slot = _state.get("slot")
        if selected_slot is not None:
            try:
                if client.resolve_slot_info(selected_slot) is not None:
                    client._resolve_slot(selected_slot)
                    _attach_to_running_slot()
                    return
            except Exception:
                pass
            if not _is_dynamic_slot_selector(selected_slot):
                return
            _state["slot"] = None

        hint = _state.get("game")
        if hint is not None:
            try:
                if client.auto_select_slot(game_hint=hint):
                    _attach_to_running_slot()
                    return
            except Exception:
                pass
            _state["game"] = None

        try:
            if client.auto_select_slot():
                _attach_to_running_slot()
        except Exception:
            pass

    if slot is not None:
        _attach_to_running_slot()

    def _after_command(cmd: str, result: dict) -> dict:
        if cmd == "load":
            client.reconcile_after_load(result)
        return result

    ctx_hooks = Hooks(after_command=_after_command)

    if game:
        try:
            # Only attach when a slot for the game exists; with none the
            # attach used to print "mark_current_events_seen failed" at
            # every start of a connect-only server.
            if client.auto_select_slot(game_hint=game):
                _attach_to_running_slot()
                client.set_auto_advance(True)
        except Exception:
            pass  # Bridge may not be up yet — lazy connect on first tool call.

    # ctx is assembled below; on_event is installed once we know we have
    # a hub to peek at. Without a hub, wait() runs to its natural decision
    # point with no interrupt path (same as today).
    ctx = HandlerContext(client=client, hooks=ctx_hooks)
    _ctx_ref[0] = ctx

    def _ensure_attached() -> None:
        if client.slot_prefix:
            return
        try:
            hint = _state.get("game")
            if client.auto_select_slot(game_hint=hint):
                _attach_to_running_slot()
            client.set_auto_advance(True)
        except Exception:
            pass  # Bridge may not be up yet.

    ctx.hooks.ensure_attached = _ensure_attached

    def _bridge_answers() -> bool:
        try:
            return bool(client.is_up())
        except Exception:
            return False
    try:
        active_capabilities = parse_capabilities(capabilities, debug=debug)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        active_tools = select_tools(active_capabilities, tools)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        from .lib import _load_config_with_error
        profile_config, profile_config_error = _load_config_with_error()
    except Exception as exc:
        profile_config, profile_config_error = None, str(exc)
    active_tools = personalize_profile_tools_for_config(
        active_tools, profile_config, profile_config_error,
    )
    active_tool_names = {tool["name"] for tool in active_tools}

    # Optional hub connection for dashboard event logging.
    _hub = None
    hub_url = kwargs.get("hub_url")
    if hub_url:
        try:
            from harness.hub import HubClient as _HubClient
            _hub_agent = kwargs.get("hub_agent") or "claude-code-local"
            _hub = _HubClient(hub_url, agent_id=_hub_agent)
            _hub.register(model="local-mcp")
            print(f"Hub connected: {hub_url}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"Hub connection failed (non-fatal): {e}", file=sys.stderr, flush=True)
            _hub = None

    # Install the pending-instructions check on the wait loop. The
    # callback peeks at the hub's read-only pending mailbox each time
    # the bridge yields a batch; if anything is queued, wait() returns
    # with WaitResult.interrupted=True and the runner drains the queue
    # on the next turn. Only attached when the hub has get_pending —
    # the legacy vnharness client may not, in which case we leave
    # on_event unset (no interrupt path, same as today).
    if _hub is not None and hasattr(_hub, "get_pending"):
        def _on_event_check_pending(_batch):
            try:
                pending = _hub.get_pending()
            except Exception:
                return None
            return True if pending else None
        ctx.hooks.on_event = _on_event_check_pending

    server = Server(**mcp_server_info())

    @server.list_tools()
    async def handle_list_tools() -> list[MCPTool]:
        return [
            MCPTool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["parameters"],
            )
            for t in active_tools
        ]

    # Mutable state for connection management.
    # Shared bridge = user provided --bridge URL (not our private bridge).
    _shared_bridge = owned_bridge is None
    _state = {"game": game, "slot": slot, "owned_bridge": owned_bridge,
              "shared_bridge": _shared_bridge, "format": "text"}

    def _handle_bridge_connect(args: dict) -> dict:
        """Switch bridge or start a new one."""
        new_url = args.get("url")
        start = args.get("start", False)

        if start:
            port = _find_free_port()
            proc = _start_owned_bridge(port)
            if not proc:
                return {"error": "Failed to start bridge"}
            # Clean up old owned bridge.
            old = _state.get("owned_bridge")
            if old and old.poll() is None:
                old.terminate()
            _state["owned_bridge"] = proc
            _owned_bridge_ref[0] = proc
            new_url = f"http://127.0.0.1:{port}"

        if new_url:
            client.bridge_url = new_url
            _rebind_after_bridge_connect()

        # Report current state.
        up = client.is_up()
        slots = client.list_slots() or [] if up else []
        return {
            "ok": True,
            "bridge_url": client.bridge_url,
            "bridge_up": up,
            "slots": len(slots),
        }

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None) -> list[TextContent | ImageContent]:
        arguments = arguments or {}
        if name == "act":
            arguments = stamp_act_invocation(
                arguments, server_instance_id)
        if name not in active_tool_names:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "error": "tool_not_enabled",
                        "tool": name,
                        "capabilities": sorted(active_capabilities),
                    }),
                )
            ]

        # Handle local-only tools.
        if name == "bridge_connect":
            result = await anyio.to_thread.run_sync(
                lambda: run_presentation_transition(
                    ctx,
                    lambda: _handle_bridge_connect(arguments),
                    reset_context=bool(
                        arguments.get("url") or arguments.get("start")),
                    transition_name="bridge_connect",
                    # Rebinding is a recovery move: a bridge that stopped
                    # answering is exactly what wedges the wait holding the
                    # lane, so bridge_connect preempts rather than refusing.
                    preemptible=True,
                    params=arguments,
                )
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == "set_format":
            fmt = arguments.get("format")
            anomalies = arguments.get("anomalies")
            if fmt is not None and fmt not in ("text", "json", "quiet"):
                return [TextContent(type="text", text=json.dumps({"error": "Invalid format"}))]
            if anomalies is not None and anomalies not in ANOMALY_VISIBILITY_MODES:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Invalid anomalies level; use off, errors or all"}))]
            if fmt is None and anomalies is None:
                fmt = "text"
            if fmt is not None:
                _state["format"] = fmt
            if anomalies is not None:
                ctx.anomaly_visibility = anomalies
            return [TextContent(type="text", text=json.dumps({
                "ok": True,
                "format": _state.get("format", "text"),
                "anomalies": ctx.anomaly_visibility,
            }))]

        # A game-bound tool with no game: refuse with the way out rather
        # than letting the handler time out against an unbound client.
        if name in _TOOLS_NEEDING_A_GAME and not client.slot_prefix:
            _ensure_attached()
            if not client.slot_prefix and (
                _state.get("game") or _bridge_answers()
            ):
                return [TextContent(type="text", text=json.dumps(
                    unbound_game_error(
                        name, _state.get("game"), active_tool_names),
                    indent=2))]

        # Apply default format to wait/state if not explicitly set.
        if name in ("wait", "state") and "format" not in arguments:
            arguments = dict(arguments)
            arguments["format"] = _state.get("format", "text")
        if name in _TRANSPORT_BOUNDED_TOOLS:
            arguments = dict(arguments)
            now = time.time()
            arguments["_transport_deadline"] = (
                now + _MCP_TOOL_MUTATION_BUDGET_S)
            # act settles on the same result budget wait does: its settle
            # policy polls until the deadline, so it must be told what the
            # deadline is instead of falling back to a bare 60 s.
            if name in ("wait", "act"):
                arguments["_result_deadline"] = arguments[
                    "_transport_deadline"]
            arguments["_response_deadline"] = (
                now + _MCP_TOOL_RESPONSE_BUDGET_S)

        # Standalone policy: stop existing game before launching a new one.
        # Only in private bridge mode — shared bridge may have other players.
        if name == "launch" and not _state.get("shared_bridge"):
            # The launch handler performs pre-stop, detach, and launch while
            # holding one presentation-call ownership lock. Lazy attachment
            # also runs inside that lock, so do not predicate this policy on
            # the pre-dispatch slot binding.
            arguments = apply_private_launch_policy(
                name,
                arguments,
                shared_bridge=bool(_state.get("shared_bridge")),
            )

        try:
            result = await anyio.to_thread.run_sync(
                lambda: handle_tool(ctx, name, arguments)
            )
        except Exception as e:
            result = {"error": str(e)}

        # Push tool call + result to hub for dashboard visibility.
        if _hub:
            try:
                import time as _time
                ts = _time.strftime("%H:%M:%S")
                display_arguments = {
                    key: value for key, value in arguments.items()
                    if not str(key).startswith("_")
                }
                hub_events = [{"type": "tool_call", "ts": ts,
                               "text": "%s(%s)" % (
                                   name,
                                   json.dumps(display_arguments, ensure_ascii=False),
                               )}]
                # Extract key info from result.
                if isinstance(result, dict):
                    if name == "act":
                        hub_events.append({"type": "action", "ts": ts,
                                           "text": "act(%s)" % arguments.get("target", "")})
                        hub_events.append({"type": "action_resolved", "ts": ts,
                                           "text": json.dumps({
                                               "resolved_as": result.get("resolved_as"),
                                               "label": result.get("label", ""),
                                               "screen": result.get("screen", ""),
                                               "success": result.get("success", result.get("ok")),
                                           }, ensure_ascii=False)})
                    elif result.get("error"):
                        hub_events.append({"type": "error", "ts": ts,
                                           "text": result["error"]})
                    # Forward game output from wait/state results.
                    # act+wait: the wait output is promoted to the act result.
                    if name in ("wait", "state") or (name == "act" and "wait" in result):
                        for _key in ("text", "status", "pending"):
                            _val = result.get(_key)
                            if _val and isinstance(_val, str) and _val.strip():
                                hub_events.append({"type": "game", "ts": ts,
                                                   "text": _val[:500]})
                deadline = arguments.get("_response_deadline")
                try:
                    supports_timeout = "timeout" in inspect.signature(
                        _hub.push_events).parameters
                except (TypeError, ValueError):
                    supports_timeout = True
                remaining = deadline - time.time() if isinstance(
                    deadline, (int, float)) else 5.0
                if remaining > 0 and supports_timeout:
                    _hub.push_events(
                        hub_events, timeout=min(5.0, remaining))
                elif remaining > 0 and deadline is None:
                    _hub.push_events(hub_events)
            except Exception:
                pass  # Best-effort.

        # Track launched game for future auto-connect.
        if name == "launch" and isinstance(result, dict) and result.get("ok"):
            game_id = (arguments or {}).get("game_id")
            if game_id:
                _state["game"] = game_id

        # Return screenshots as MCP image content.
        if isinstance(result, dict) and "screenshot_base64" in result:
            return [ImageContent(type="image", data=result["screenshot_base64"],
                                 mimeType="image/png")]
        # Text rendering consumes private chronology metadata. Structured
        # output is scrubbed inside the helper before JSON serialization.
        text = _mcp_result_text(
            name, arguments, result, _state.get("format", "text"))
        return [TextContent(type="text", text=text)]

    async def _run() -> None:
        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )
        finally:
            _cleanup()

    anyio.run(_run)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def mcp_main(argv: list[str] | None = None) -> int:
    """Entry point for standalone MCP server mode."""
    parser = argparse.ArgumentParser(
        prog="vnflight-mcp",
        description="Simple MCP server for playing visual novels via vnflight.",
    )
    parser.add_argument("--bridge", default=None,
                        help="Bridge URL (omit to start a private bridge)")
    parser.add_argument("--game", default=None,
                        help="Game ID or slot to connect to")
    parser.add_argument("--slot", default=None,
                        help=(
                            "Specific bridge slot ID or game_id to bind this MCP "
                            "session to. Use latest:<game_id> to pick the newest "
                            "live slot for a game."
                        ))
    parser.add_argument("--debug", action="store_true",
                        help="Enable diagnostic/admin tools (legacy alias)")
    parser.add_argument(
        "--capabilities",
        default=None,
        help=(
            "Comma-separated tool capability set: play, lifecycle, "
            "diagnostic, admin, or all. Default: play"
        ),
    )
    parser.add_argument(
        "--tools",
        default=None,
        help=(
            "Comma-separated exact tool allowlist. Tools must also be enabled "
            "by --capabilities."
        ),
    )
    parser.add_argument("--token", default=None,
                        help="Bridge slot reservation token")
    parser.add_argument("--hub", default=None,
                        help="Hub URL for dashboard event logging (optional)")
    parser.add_argument("--hub-agent", default=None,
                        help="Agent name in hub (default: claude-code-local)")
    args = parser.parse_args(argv)

    run_server(
        bridge_url=args.bridge,
        game=args.game,
        slot=args.slot,
        debug=args.debug,
        token=args.token,
        capabilities=args.capabilities,
        tools=args.tools,
        hub_url=args.hub,
        hub_agent=args.hub_agent,
    )
    return 0


if __name__ == "__main__":
    sys.exit(mcp_main())
