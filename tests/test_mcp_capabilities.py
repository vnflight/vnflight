import asyncio
import json
import os
import subprocess
import sys
import types

import pytest

from vnflight import mcp


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def tool_names(capabilities: str | None = None, *, debug: bool = False) -> set[str]:
    caps = mcp.parse_capabilities(capabilities, debug=debug)
    return {tool["name"] for tool in mcp.select_tools(caps)}


def test_act_invocation_stamp_overwrites_reserved_values_and_is_unique():
    supplied = {
        "target": "Ask something else.",
        "_mcp_server_instance_id": "caller-server",
        "_mcp_call_id": "caller-call",
        "_mcp_original_target": "caller-target",
    }

    first = mcp.stamp_act_invocation(supplied, "server-a")
    second = mcp.stamp_act_invocation(supplied, "server-a")

    assert supplied["_mcp_call_id"] == "caller-call"
    assert first["_mcp_server_instance_id"] == "server-a"
    assert second["_mcp_server_instance_id"] == "server-a"
    assert first["_mcp_call_id"] != second["_mcp_call_id"]
    assert first["_mcp_original_target"] == "Ask something else."


def test_private_launch_policy_does_not_depend_on_pre_dispatch_attachment():
    arguments = {"game_id": "echoes_of_tomorrow"}

    prepared = mcp.apply_private_launch_policy(
        "launch", arguments, shared_bridge=False)

    assert arguments == {"game_id": "echoes_of_tomorrow"}
    assert prepared["_stop_existing"] is True
    assert "_stop_existing" not in mcp.apply_private_launch_policy(
        "launch", arguments, shared_bridge=True)


def test_mcp_default_capabilities_expose_play_tools_only():
    names = tool_names()

    assert "wait" in names
    assert "act" in names
    assert "advance" in names
    assert "rewind" in names
    assert "replay" in names
    assert "state" in names
    assert "launch" not in names
    assert "stop" not in names
    assert "games" not in names
    assert "inspect" not in names
    assert "save_scan" not in names
    assert "command" not in names


def test_mcp_lifecycle_capability_adds_launch_stop_games():
    names = tool_names("play,lifecycle")

    assert "wait" in names
    assert "launch" in names
    assert "stop" in names
    assert "games" in names
    assert "save_scan" not in names


def test_mcp_diagnostic_capability_adds_read_only_diagnostics():
    names = tool_names("play,diagnostic")

    assert "inspect" in names
    assert "save_scan" in names
    assert "progress" in names
    assert "command" not in names


def test_mcp_admin_capability_adds_raw_command():
    names = tool_names("play,admin")

    assert "command" in names
    assert "save_scan" not in names


def test_mcp_debug_legacy_alias_adds_diagnostic_and_admin():
    names = tool_names(debug=True)

    assert "launch" not in names
    assert "inspect" in names
    assert "save_scan" in names
    assert "command" in names


def test_mcp_unknown_capability_is_rejected():
    with pytest.raises(ValueError, match="Unknown MCP capabilities"):
        mcp.parse_capabilities("play,chaos")


def test_mcp_tools_allowlist_filters_enabled_tools():
    caps = mcp.parse_capabilities("play")
    names = {tool["name"] for tool in mcp.select_tools(caps, "state,transcript")}

    assert names == {"state", "transcript"}


def test_mcp_tools_allowlist_can_select_diagnostic_tools_when_enabled():
    caps = mcp.parse_capabilities("diagnostic")
    names = {tool["name"] for tool in mcp.select_tools(caps, "save_scan")}

    assert names == {"save_scan"}


def test_mcp_tools_allowlist_rejects_unknown_tools():
    caps = mcp.parse_capabilities("play")

    with pytest.raises(ValueError, match="Unknown MCP tools"):
        mcp.select_tools(caps, "state,nope")


def test_mcp_tools_allowlist_rejects_capability_violation():
    caps = mcp.parse_capabilities("play")

    with pytest.raises(ValueError, match="not enabled by selected capabilities"):
        mcp.select_tools(caps, "state,save_scan")


def test_mcp_state_schema_advertises_format_option():
    caps = mcp.parse_capabilities("play")
    tools = {tool["name"]: tool for tool in mcp.select_tools(caps)}

    state_schema = tools["state"]["parameters"]["properties"]

    assert state_schema["format"]["enum"] == ["text", "json", "quiet"]


def test_mcp_auto_skip_schema_advertises_command_retry_nonce():
    caps = mcp.parse_capabilities("play")
    tools = {tool["name"]: tool for tool in mcp.select_tools(caps)}

    properties = tools["auto_skip"]["parameters"]["properties"]

    assert "command_nonce" in properties


def test_mcp_profile_schema_lists_only_configured_profiles():
    caps = mcp.parse_capabilities("play,lifecycle")
    base = mcp.select_tools(caps)

    tools = {
        tool["name"]: tool
        for tool in mcp.personalize_profile_tools(
            base,
            {"turbo": {"turbo": True}, "hybrid_text": {"text_cps": 32}},
        )
    }

    expected = ["default", "hybrid_text", "turbo"]
    assert tools["set_profile"]["parameters"]["properties"]["profile"]["enum"] == expected
    assert "command_nonce" in tools["set_profile"]["parameters"]["properties"]
    assert tools["launch"]["parameters"]["properties"]["profile"]["enum"] == expected
    assert "Available: default, hybrid_text, turbo" in tools["set_profile"]["description"]


def test_mcp_hides_profile_controls_without_profile_definitions():
    caps = mcp.parse_capabilities("play,lifecycle")
    base = mcp.select_tools(caps)

    tools = {
        tool["name"]: tool
        for tool in mcp.personalize_profile_tools(base, {})
    }

    assert "set_profile" not in tools
    assert "profile" not in tools["launch"]["parameters"]["properties"]
    original = {tool["name"]: tool for tool in base}
    assert "profile" in original["launch"]["parameters"]["properties"]


def test_mcp_keeps_generic_profile_controls_when_config_is_unreadable():
    caps = mcp.parse_capabilities("play,lifecycle")
    base = mcp.select_tools(caps)

    tools = {
        tool["name"]: tool
        for tool in mcp.personalize_profile_tools_for_config(
            base, None, "vnflight.json: permission denied",
        )
    }

    assert "set_profile" in tools
    assert "enum" not in (
        tools["set_profile"]["parameters"]["properties"]["profile"]
    )
    assert "profile" in tools["launch"]["parameters"]["properties"]


def test_mcp_text_renders_private_story_chronology_before_scrubbing():
    result = {
        "text": "Narration A.\nNarration C.",
        "screen_text": "Terminal B.",
        "_story_render_sections": [
            {"channel": "text", "text": "Narration A."},
            {"channel": "screen_text", "text": "Terminal B."},
            {"channel": "text", "text": "Narration C."},
        ],
    }

    rendered = mcp._mcp_result_text("wait", {"format": "text"}, result, "text")

    assert rendered.index("Narration A.") < rendered.index("Terminal B.")
    assert rendered.index("Terminal B.") < rendered.index("Narration C.")
    public = json.loads(mcp._mcp_result_text(
        "wait", {"format": "json"}, result, "text"))
    assert "_story_render_sections" not in public


def test_input_text_renders_wait_story_once():
    result = {
        "ok": True,
        "text": "The city felt different tonight.",
        "wait": {"text": "The city felt different tonight."},
    }
    rendered = mcp._mcp_result_text("input_text", {}, result, "text")
    assert rendered.count("The city felt different tonight.") == 1
    assert '"wait"' not in rendered
    public = json.loads(mcp._mcp_result_text(
        "input_text", {"format": "json"}, result, "text"))
    assert public["wait"]["text"] == result["text"]


def test_mcp_story_navigation_tools_have_distinct_descriptions():
    caps = mcp.parse_capabilities("play")
    tools = {tool["name"]: tool for tool in mcp.select_tools(caps)}

    assert "dialogue interaction" in tools["advance"]["description"]
    assert "dialogue/checkpoint backward" in tools["rewind"]["description"]
    assert "Roll forward" in tools["replay"]["description"]
    assert "Does not move through dialogue history" in tools["back"]["description"]


def test_mcp_call_tool_rejects_hidden_tool(monkeypatch):
    captured = {}

    class FakeTextContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeImageContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMCPTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeServer:
        def __init__(self, name, version=None):
            self.name = name

        def list_tools(self):
            def decorate(fn):
                captured["list_tools"] = fn
                return fn

            return decorate

        def call_tool(self):
            def decorate(fn):
                captured["call_tool"] = fn
                return fn

            return decorate

        def create_initialization_options(self):
            return {}

        async def run(self, read_stream, write_stream, options):
            captured["result"] = await captured["call_tool"]("command", {})

    class FakeStdio:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_run_sync(fn):
        return fn()

    fake_anyio = types.ModuleType("anyio")
    fake_anyio.run = lambda fn: asyncio.run(fn())
    fake_anyio.to_thread = types.SimpleNamespace(run_sync=fake_run_sync)

    fake_server_mod = types.ModuleType("mcp.server")
    fake_server_mod.Server = FakeServer
    fake_stdio_mod = types.ModuleType("mcp.server.stdio")
    fake_stdio_mod.stdio_server = lambda: FakeStdio()
    fake_types_mod = types.ModuleType("mcp.types")
    fake_types_mod.ImageContent = FakeImageContent
    fake_types_mod.TextContent = FakeTextContent
    fake_types_mod.Tool = FakeMCPTool

    monkeypatch.setitem(sys.modules, "anyio", fake_anyio)
    monkeypatch.setitem(sys.modules, "mcp.server", fake_server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", fake_stdio_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types_mod)

    mcp.run_server(
        bridge_url="http://127.0.0.1:9",
        capabilities="play",
    )

    payload = json.loads(captured["result"][0].text)
    assert payload == {
        "error": "tool_not_enabled",
        "tool": "command",
        "capabilities": ["play"],
    }


@pytest.mark.parametrize(
    (
        "load_success",
        "marker_results",
        "expected_cursor",
        "expected_marked",
        "expected_mark_calls",
    ),
    [
        (True, [True], 123, True, 1),
        (True, [False, True], 123, True, 2),
        (True, [False], 0, True, 3),
        (False, [True], 99, False, 0),
    ],
)
def test_mcp_load_clears_prefetched_client_state(
    monkeypatch,
    load_success,
    marker_results,
    expected_cursor,
    expected_marked,
    expected_mark_calls,
):
    captured = {}
    real_bridge_client = mcp.BridgeClient

    class FakeTextContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeImageContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMCPTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeServer:
        def __init__(self, name, version=None):
            self.name = name

        def list_tools(self):
            def decorate(fn):
                return fn

            return decorate

        def call_tool(self):
            def decorate(fn):
                captured["call_tool"] = fn
                return fn

            return decorate

        def create_initialization_options(self):
            return {}

        async def run(self, read_stream, write_stream, options):
            captured["result"] = await captured["call_tool"](
                "load",
                {"slot": "checkpoint"},
            )

    class FakeStdio:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeBridgeClient(real_bridge_client):
        def __init__(self, bridge_url, slot=None, token=None):
            captured["client"] = self
            self.bridge_url = bridge_url
            self.slot_prefix = f"/{slot}" if slot is not None else "/1"
            self.cursor = 99
            self.last_request_id = "old-request"
            self.last_request_type = "choice_request"
            self.last_choices = ["old choice"]
            self._acted_request_id = "acted-request"
            self._prefetched_events = [{"type": "narration", "text": "stale"}]
            self._last_poll_pending = {"id": "old-pending"}
            self.marked_current_events_seen = False
            self.mark_current_events_seen_calls = 0

        def mark_current_events_seen(
            self,
            *,
            allow_rewind=False,
            require_decision=False,
            preserve_story_after_pending=True,
        ):
            assert allow_rewind is True
            assert preserve_story_after_pending is False
            self.marked_current_events_seen = True
            self.mark_current_events_seen_calls += 1
            marker_result = (
                marker_results.pop(0)
                if marker_results
                else False
            )
            if marker_result:
                self.cursor = 123
            return marker_result

        def attach_to_running_slot(self, *, warn=None):
            self.marked_current_events_seen = True
            self.mark_current_events_seen_calls += 1
            return True

    def fake_handle_tool(ctx, name, args):
        assert name == "load"
        result = {
            "type": "command_result",
            "command": "load",
            "success": load_success,
        }
        return ctx.hooks.after_command("load", result)

    async def fake_run_sync(fn):
        return fn()

    fake_anyio = types.ModuleType("anyio")
    fake_anyio.run = lambda fn: asyncio.run(fn())
    fake_anyio.to_thread = types.SimpleNamespace(run_sync=fake_run_sync)

    fake_server_mod = types.ModuleType("mcp.server")
    fake_server_mod.Server = FakeServer
    fake_stdio_mod = types.ModuleType("mcp.server.stdio")
    fake_stdio_mod.stdio_server = lambda: FakeStdio()
    fake_types_mod = types.ModuleType("mcp.types")
    fake_types_mod.ImageContent = FakeImageContent
    fake_types_mod.TextContent = FakeTextContent
    fake_types_mod.Tool = FakeMCPTool

    monkeypatch.setitem(sys.modules, "anyio", fake_anyio)
    monkeypatch.setitem(sys.modules, "mcp.server", fake_server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", fake_stdio_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types_mod)
    monkeypatch.setattr(mcp, "BridgeClient", FakeBridgeClient)
    monkeypatch.setattr(mcp, "handle_tool", fake_handle_tool)
    monotonic = {"value": 0.0}
    monkeypatch.setattr(
        mcp.time,
        "monotonic",
        lambda: monotonic["value"],
    )
    monkeypatch.setattr(
        mcp.time,
        "sleep",
        lambda seconds: monotonic.__setitem__(
            "value", monotonic["value"] + max(seconds, 3.0)
        ),
    )

    mcp.run_server(bridge_url="http://127.0.0.1:8385")

    client = captured["client"]
    assert client.cursor == expected_cursor
    assert client.marked_current_events_seen is expected_marked
    assert client.mark_current_events_seen_calls == expected_mark_calls
    if load_success:
        assert client.last_request_id is None
        assert client.last_request_type is None
        assert client.last_choices is None
        assert client._acted_request_id is None
        assert client._prefetched_events == []
        assert client._last_poll_pending is None
    else:
        assert client.last_request_id == "old-request"
        assert client.last_request_type == "choice_request"
        assert client.last_choices == ["old choice"]
        assert client._acted_request_id == "acted-request"
        assert client._prefetched_events == [{"type": "narration", "text": "stale"}]
        assert client._last_poll_pending == {"id": "old-pending"}


def test_mcp_run_server_binds_explicit_slot(monkeypatch):
    captured = {}

    class FakeTextContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeImageContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMCPTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeServer:
        def __init__(self, name, version=None):
            self.name = name

        def list_tools(self):
            def decorate(fn):
                return fn

            return decorate

        def call_tool(self):
            def decorate(fn):
                return fn

            return decorate

        def create_initialization_options(self):
            return {}

        async def run(self, read_stream, write_stream, options):
            return None

    class FakeStdio:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeBridgeClient:
        def __init__(self, bridge_url, slot=None, token=None):
            captured["bridge_url"] = bridge_url
            captured["slot"] = slot
            captured["token"] = token
            self.bridge_url = bridge_url
            self.slot_prefix = f"/{slot}" if slot is not None else ""

        def mark_current_events_seen(self):
            captured["marked_current_events_seen"] = True
            return True

        def attach_to_running_slot(self, *, warn=None):
            return self.mark_current_events_seen()

    fake_anyio = types.ModuleType("anyio")
    fake_anyio.run = lambda fn: asyncio.run(fn())
    fake_anyio.to_thread = types.SimpleNamespace(run_sync=lambda fn: fn())

    fake_server_mod = types.ModuleType("mcp.server")
    fake_server_mod.Server = FakeServer
    fake_stdio_mod = types.ModuleType("mcp.server.stdio")
    fake_stdio_mod.stdio_server = lambda: FakeStdio()
    fake_types_mod = types.ModuleType("mcp.types")
    fake_types_mod.ImageContent = FakeImageContent
    fake_types_mod.TextContent = FakeTextContent
    fake_types_mod.Tool = FakeMCPTool

    monkeypatch.setitem(sys.modules, "anyio", fake_anyio)
    monkeypatch.setitem(sys.modules, "mcp.server", fake_server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", fake_stdio_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types_mod)
    monkeypatch.setattr(mcp, "BridgeClient", FakeBridgeClient)

    mcp.run_server(
        bridge_url="http://127.0.0.1:8385",
        slot="33",
        token="slot-token",
    )

    assert captured == {
        "bridge_url": "http://127.0.0.1:8385",
        "slot": "33",
        "token": "slot-token",
        "marked_current_events_seen": True,
    }


def test_mcp_bridge_connect_drops_unresolved_configured_slot(monkeypatch):
    captured = {}
    real_transition = mcp.run_presentation_transition

    class FakeTextContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeImageContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMCPTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeServer:
        def __init__(self, name, version=None):
            self.name = name

        def list_tools(self):
            def decorate(fn):
                return fn

            return decorate

        def call_tool(self):
            def decorate(fn):
                captured["call_tool"] = fn
                return fn

            return decorate

        def create_initialization_options(self):
            return {}

        async def run(self, read_stream, write_stream, options):
            captured["result"] = await captured["call_tool"](
                "bridge_connect",
                {"url": "http://127.0.0.1:8385"},
            )

    class FakeStdio:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeBridgeClient:
        def __init__(self, bridge_url, slot=None, token=None):
            self.bridge_url = bridge_url
            self.slot_prefix = f"/{slot}" if slot is not None else ""
            self.attached = 0

        def resolve_slot_info(self, selector):
            # The configured startup selector is stale on the connected bridge.
            if selector == "latest:roadwarden":
                return None
            return {"slot_id": 19, "game_id": "echoes_of_tomorrow"}

        def _resolve_slot(self, selector):
            self.slot_prefix = f"/{selector}"

        def auto_select_slot(self, game_hint=None):
            captured.setdefault("auto_select_hints", []).append(game_hint)
            if game_hint is None:
                self.slot_prefix = "/19"
                return True
            return False

        def attach_to_running_slot(self, *, warn=None):
            self.attached += 1
            captured["attached_prefix"] = self.slot_prefix
            return True

        def is_up(self):
            return True

        def list_slots(self):
            return [{"slot_id": 19, "game_id": "echoes_of_tomorrow"}]

    async def fake_run_sync(fn):
        return fn()

    fake_anyio = types.ModuleType("anyio")
    fake_anyio.run = lambda fn: asyncio.run(fn())
    fake_anyio.to_thread = types.SimpleNamespace(run_sync=fake_run_sync)

    fake_server_mod = types.ModuleType("mcp.server")
    fake_server_mod.Server = FakeServer
    fake_stdio_mod = types.ModuleType("mcp.server.stdio")
    fake_stdio_mod.stdio_server = lambda: FakeStdio()
    fake_types_mod = types.ModuleType("mcp.types")
    fake_types_mod.ImageContent = FakeImageContent
    fake_types_mod.TextContent = FakeTextContent
    fake_types_mod.Tool = FakeMCPTool

    monkeypatch.setitem(sys.modules, "anyio", fake_anyio)
    monkeypatch.setitem(sys.modules, "mcp.server", fake_server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", fake_stdio_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types_mod)
    monkeypatch.setattr(mcp, "BridgeClient", FakeBridgeClient)

    def capture_transition(ctx, callback, **kwargs):
        ctx.overlay.pending_deliveries = [{"id": 1, "text": "old bridge"}]
        ctx.overlay.timeline_source_sequences = {"old-source": 7}
        result = real_transition(ctx, callback, **kwargs)
        captured["pending_after_connect"] = ctx.overlay.pending_deliveries
        captured["sources_after_connect"] = ctx.overlay.timeline_source_sequences
        return result

    monkeypatch.setattr(
        mcp, "run_presentation_transition", capture_transition)

    mcp.run_server(
        bridge_url="http://127.0.0.1:8385",
        slot="latest:roadwarden",
        capabilities="play,lifecycle",
    )

    payload = json.loads(captured["result"][0].text)
    assert payload["ok"] is True
    assert payload["slots"] == 1
    assert captured["auto_select_hints"] == [None]
    assert captured["attached_prefix"] == "/19"
    assert captured["pending_after_connect"] == []
    assert captured["sources_after_connect"] == {}


def test_mcp_bridge_connect_keeps_unresolved_explicit_slot_unbound(monkeypatch):
    captured = {}

    class FakeTextContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeImageContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMCPTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeServer:
        def __init__(self, name, version=None):
            self.name = name

        def list_tools(self):
            def decorate(fn):
                return fn

            return decorate

        def call_tool(self):
            def decorate(fn):
                captured["call_tool"] = fn
                return fn

            return decorate

        def create_initialization_options(self):
            return {}

        async def run(self, read_stream, write_stream, options):
            captured["result"] = await captured["call_tool"](
                "bridge_connect",
                {"url": "http://127.0.0.1:8385"},
            )

    class FakeStdio:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeBridgeClient:
        def __init__(self, bridge_url, slot=None, token=None):
            self.bridge_url = bridge_url
            self.slot_prefix = f"/{slot}" if slot is not None else ""

        def resolve_slot_info(self, selector):
            if str(selector) == "33":
                return None
            return {"slot_id": 19, "game_id": "echoes_of_tomorrow"}

        def _resolve_slot(self, selector):
            self.slot_prefix = f"/{selector}"

        def auto_select_slot(self, game_hint=None):
            captured.setdefault("auto_select_hints", []).append(game_hint)
            self.slot_prefix = "/19"
            return True

        def attach_to_running_slot(self, *, warn=None):
            captured.setdefault("attached_prefixes", []).append(self.slot_prefix)
            return True

        def is_up(self):
            return True

        def list_slots(self):
            captured["slot_prefix_during_bridge_connect_report"] = self.slot_prefix
            return [{"slot_id": 19, "game_id": "echoes_of_tomorrow"}]

    async def fake_run_sync(fn):
        return fn()

    fake_anyio = types.ModuleType("anyio")
    fake_anyio.run = lambda fn: asyncio.run(fn())
    fake_anyio.to_thread = types.SimpleNamespace(run_sync=fake_run_sync)

    fake_server_mod = types.ModuleType("mcp.server")
    fake_server_mod.Server = FakeServer
    fake_stdio_mod = types.ModuleType("mcp.server.stdio")
    fake_stdio_mod.stdio_server = lambda: FakeStdio()
    fake_types_mod = types.ModuleType("mcp.types")
    fake_types_mod.ImageContent = FakeImageContent
    fake_types_mod.TextContent = FakeTextContent
    fake_types_mod.Tool = FakeMCPTool

    monkeypatch.setitem(sys.modules, "anyio", fake_anyio)
    monkeypatch.setitem(sys.modules, "mcp.server", fake_server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", fake_stdio_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types_mod)
    monkeypatch.setattr(mcp, "BridgeClient", FakeBridgeClient)

    mcp.run_server(
        bridge_url="http://127.0.0.1:8385",
        slot="33",
        capabilities="play,lifecycle",
    )

    payload = json.loads(captured["result"][0].text)
    assert payload["ok"] is True
    assert payload["slots"] == 1
    assert "auto_select_hints" not in captured
    assert captured["attached_prefixes"] == ["/33"]
    assert captured["slot_prefix_during_bridge_connect_report"] == ""


@pytest.mark.parametrize(
    "marker_result, expected_error",
    [
        (False, "returned_false"),
        (RuntimeError("bridge unavailable"), "bridge unavailable"),
    ],
)
def test_mcp_run_server_traces_explicit_slot_mark_failures(
    monkeypatch,
    capsys,
    marker_result,
    expected_error,
):
    class FakeTextContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeImageContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMCPTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeServer:
        def __init__(self, name, version=None):
            self.name = name

        def list_tools(self):
            def decorate(fn):
                return fn

            return decorate

        def call_tool(self):
            def decorate(fn):
                return fn

            return decorate

        def create_initialization_options(self):
            return {}

        async def run(self, read_stream, write_stream, options):
            return None

    class FakeStdio:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeBridgeClient:
        def __init__(self, bridge_url, slot=None, token=None):
            self.bridge_url = bridge_url
            self.slot_prefix = f"/{slot}" if slot is not None else ""

        def mark_current_events_seen(self):
            if isinstance(marker_result, Exception):
                raise marker_result
            return marker_result

        def attach_to_running_slot(self, *, warn=None):
            try:
                marked = self.mark_current_events_seen()
            except Exception as exc:
                if warn:
                    warn(str(exc))
                return False
            if marked is False:
                if warn:
                    warn("returned_false")
                return False
            return True

    fake_anyio = types.ModuleType("anyio")
    fake_anyio.run = lambda fn: asyncio.run(fn())
    fake_anyio.to_thread = types.SimpleNamespace(run_sync=lambda fn: fn())

    fake_server_mod = types.ModuleType("mcp.server")
    fake_server_mod.Server = FakeServer
    fake_stdio_mod = types.ModuleType("mcp.server.stdio")
    fake_stdio_mod.stdio_server = lambda: FakeStdio()
    fake_types_mod = types.ModuleType("mcp.types")
    fake_types_mod.ImageContent = FakeImageContent
    fake_types_mod.TextContent = FakeTextContent
    fake_types_mod.Tool = FakeMCPTool

    monkeypatch.setitem(sys.modules, "anyio", fake_anyio)
    monkeypatch.setitem(sys.modules, "mcp.server", fake_server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", fake_stdio_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types_mod)
    monkeypatch.setattr(mcp, "BridgeClient", FakeBridgeClient)

    mcp.run_server(bridge_url="http://127.0.0.1:8385", slot="33")

    err = capsys.readouterr().err
    assert "mark_current_events_seen failed" in err
    assert expected_error in err


def test_mcp_main_accepts_slot_argument(monkeypatch):
    captured = {}

    def fake_run_server(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(mcp, "run_server", fake_run_server)

    rc = mcp.mcp_main([
        "--bridge", "http://127.0.0.1:8385",
        "--slot", "33",
    ])

    assert rc == 0
    assert captured["slot"] == "33"


def test_mcp_main_accepts_latest_slot_selector(monkeypatch):
    captured = {}

    def fake_run_server(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(mcp, "run_server", fake_run_server)

    rc = mcp.mcp_main([
        "--bridge", "http://127.0.0.1:8385",
        "--slot", "latest:roadwarden",
    ])

    assert rc == 0
    assert captured["slot"] == "latest:roadwarden"


def test_mcp_package_import_works_from_repo_root_when_src_is_first():
    code = f"""
import sys
sys.path.insert(0, {SRC!r})
from vnflight import mcp, save_scan
from vnflight.handlers import HandlerContext, handle_save_scan
assert 'save_scan' in {{tool['name'] for tool in mcp.select_tools({{'diagnostic'}})}}
assert save_scan.iter_save_files.__name__ == 'iter_save_files'
assert handle_save_scan(HandlerContext(client=object()), {{}})['error'].startswith('Missing')
"""

    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr


class _FakeBridgeProc:
    pid = 4242

    def poll(self):
        return None

    def kill(self):  # pragma: no cover - only called when startup fails
        pass


class _FakeBridgeClient:
    def __init__(self, url, **kwargs):
        self.url = url

    def is_up(self):
        return True


def _capture_owned_bridge_cmd(monkeypatch, artifact, admin_token="tok123"):
    """Run _start_owned_bridge with subprocess + readiness stubbed out."""
    from vnflight import lib

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeBridgeProc()

    monkeypatch.setattr(lib, "_single_file_artifact", lambda: artifact)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mcp, "BridgeClient", _FakeBridgeClient)

    proc = mcp._start_owned_bridge(9999, admin_token=admin_token)
    assert proc is not None
    return captured["cmd"]


def test_start_owned_bridge_package_mode_uses_bridge_module(monkeypatch):
    cmd = _capture_owned_bridge_cmd(
        monkeypatch,
        artifact=None,
        admin_token="-leading-dash",
    )

    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "vnflight.bridge"]
    assert "--token=-leading-dash" in cmd
    assert "--token" not in cmd


def test_start_owned_bridge_single_file_mode_reinvokes_artifact(
    monkeypatch, tmp_path
):
    """Flat deployment: the owned bridge must be spawned from the artifact.

    ``python -m vnflight.bridge`` has no package to import next to a
    standalone vnflight.py, so the MCP server's private bridge silently
    failed to start and the server fell back to the default port.  This
    is the same argv-self fix lib._launch_game already received.
    """
    artifact = tmp_path / "vnflight.py"
    artifact.write_text("# standalone build", encoding="utf-8")

    cmd = _capture_owned_bridge_cmd(
        monkeypatch,
        artifact=artifact,
        admin_token="-leading-dash",
    )

    assert cmd[:3] == [sys.executable, str(artifact), "bridge"]
    assert "--port" in cmd and "9999" in cmd
    assert "--token=-leading-dash" in cmd
    assert "--token" not in cmd

def _capture_tool_arguments(monkeypatch, module, tool_name, arguments):
    """Run one call_tool dispatch and return what handle_tool received."""
    captured = {}

    class FakeTextContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeImageContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMCPTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeServer:
        def __init__(self, name, version=None):
            self.name = name

        def list_tools(self):
            def decorate(fn):
                captured["list_tools"] = fn
                return fn

            return decorate

        def call_tool(self):
            def decorate(fn):
                captured["call_tool"] = fn
                return fn

            return decorate

        def create_initialization_options(self):
            return {}

        async def run(self, read_stream, write_stream, options):
            captured["result"] = await captured["call_tool"](
                tool_name, dict(arguments))

    class FakeStdio:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_run_sync(fn):
        return fn()

    fake_anyio = types.ModuleType("anyio")
    fake_anyio.run = lambda fn: asyncio.run(fn())
    fake_anyio.to_thread = types.SimpleNamespace(run_sync=fake_run_sync)

    fake_server_mod = types.ModuleType("mcp.server")
    fake_server_mod.Server = FakeServer
    fake_stdio_mod = types.ModuleType("mcp.server.stdio")
    fake_stdio_mod.stdio_server = lambda: FakeStdio()
    fake_types_mod = types.ModuleType("mcp.types")
    fake_types_mod.ImageContent = FakeImageContent
    fake_types_mod.TextContent = FakeTextContent
    fake_types_mod.Tool = FakeMCPTool

    monkeypatch.setitem(sys.modules, "anyio", fake_anyio)
    monkeypatch.setitem(sys.modules, "mcp.server", fake_server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", fake_stdio_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types_mod)

    def fake_handle_tool(_ctx, name, args):
        captured["name"] = name
        captured["arguments"] = dict(args)
        return {"ok": True}

    monkeypatch.setattr(module, "handle_tool", fake_handle_tool)
    module.run_server(bridge_url="http://127.0.0.1:9", capabilities="play")
    return captured["arguments"]


def test_mcp_act_carries_the_result_deadline_like_wait(monkeypatch):
    """act settles on a deadline-bounded policy, so it must be told the deadline.

    Before this, only wait received ``_result_deadline``; act's settle loop
    fell back to a bare 60 s, which is why fleet R61's stalled overlay acts
    all returned at exactly 60.0 s.
    """
    act_args = _capture_tool_arguments(
        monkeypatch, mcp, "act", {"target": "1"})

    assert act_args["_result_deadline"] == act_args["_transport_deadline"]

    wait_args = _capture_tool_arguments(
        monkeypatch, mcp, "wait", {"timeout": 5})

    assert wait_args["_result_deadline"] == wait_args["_transport_deadline"]


def test_game_bound_tool_without_a_game_is_refused_with_the_way_out():
    err = mcp.unbound_game_error("wait", "mystic_cafe", {"wait", "act"})
    assert err["error"] == "no_game_bound"
    assert err["tool"] == "wait"
    assert "does not launch" in err["message"]
    from vnflight.lib import cli_command_hint
    assert f"{cli_command_hint()} launch mystic_cafe" in err["message"]
    assert "lifecycle" in err["message"]

    with_launch = mcp.unbound_game_error("state", None, {"state", "launch"})
    assert 'launch(game_id="<game>")' in with_launch["message"]
    assert "lifecycle" not in with_launch["message"]

    for tool in ("wait", "act", "state", "screenshot", "inspect", "command"):
        assert tool in mcp._TOOLS_NEEDING_A_GAME
    for tool in ("launch", "stop", "games", "bridge_connect", "set_format", "save_scan"):
        assert tool not in mcp._TOOLS_NEEDING_A_GAME


def test_owned_bridge_shutdown_stops_its_games_first():
    """Terminating only the bridge left the game running with nothing to
    reach it and nothing for `stop` to find."""
    from vnflight.handlers import HandlerContext, Hooks

    calls = []

    def fake_run_cli(*args, timeout=90):
        calls.append(list(args))
        return {"ok": True, "output": "stopped"}

    client = types.SimpleNamespace(
        bridge_url="http://127.0.0.1:1", token=None, slot_prefix="",
        admin_token=None,
    )
    ctx = HandlerContext(client=client, hooks=Hooks(run_cli=fake_run_cli))

    result = mcp.stop_owned_games(ctx)

    assert result == {"ok": True, "output": "stopped"}
    assert calls and "stop" in calls[0]
    assert mcp.stop_owned_games(None) is None


def test_mcp_server_info_reports_the_package_version():
    """serverInfo.version used to be the mcp library's version (1.26.0),
    not vnflight's."""
    from vnflight import __version__

    info = mcp.mcp_server_info()
    assert info == {"name": "vnflight", "version": __version__}

    lowlevel = pytest.importorskip("mcp.server.lowlevel")
    options = lowlevel.Server(**info).create_initialization_options()
    assert options.server_name == "vnflight"
    assert options.server_version == __version__
