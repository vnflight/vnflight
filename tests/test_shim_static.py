"""Static contract checks for the Ren'Py shim.

These tests parse Python blocks embedded in ``vnflight.rpy``.  They are not a
replacement for live save regressions, but they catch fragile wiring mistakes in
the shim without launching Ren'Py.
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
import types
from pathlib import Path

import pytest

from vnflight.bridge import GameState
from vnflight.shim_schema import (
    ACTIONABLE_ITEM_FIELDS,
    ACTIONABLE_REQUEST_FIELDS,
    ACTIONABLE_REQUEST_TARGET_FIELDS,
    SHIM_CHOICE_REQUEST_PAYLOAD_FIELDS,
    SHIM_GAME_STATE_BUTTON_FIELDS,
    SHIM_INTERACTION_FIELDS,
    SHIM_REQUEST_BUTTON_FIELDS,
    SHIM_REQUEST_CHOICE_FIELDS,
    SHIM_PROTOCOL_VERSION,
)


ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "vnflight.rpy"


@pytest.mark.parametrize("key_name", ["content_key", "_empty_hash"])
def test_playback_changes_invalidate_unchanged_screen_snapshot(key_name):
    function = next(node for node in ast.walk(parse_shim_python())
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_vnf_scrape_visible_screens")
    assignments = {node.targets[0].id: node.value for node in ast.walk(function)
                   if isinstance(node, ast.Assign) and len(node.targets) == 1
                   and isinstance(node.targets[0], ast.Name)}
    player = types.SimpleNamespace(auto_advance=True, auto_advance_delay=0.3)
    ns = dict(vnf_player=player, all_data={"texts": [], "choices": []},
              btn_sigs=(), _inventory_sig=(), _stats_sig=(), _at_mm=False,
              _overlay_active=False, modal_screens=[],
              _vnf_autoskip=types.SimpleNamespace(resolve_value=None))
    def key():
        ns["_playback_sig"] = eval(compile(ast.Expression(assignments["_playback_sig"]),
                                         "<shim>", "eval"), ns)
        return eval(compile(ast.Expression(assignments[key_name]), "<shim>", "eval"), ns)
    original = key()
    assert key() == original
    player.auto_advance = False
    disabled = key()
    assert disabled != original
    player.auto_advance_delay = 0.7
    assert key() != disabled

# The Ren'Py SDKs are read-only reference trees kept out of git (and so out
# of git worktrees); they only live at this fixed absolute location. Prefer
# a same-name sibling of ROOT (a normal checkout) so the tests still work if
# the SDKs are ever vendored in-repo, but fall back to the absolute path so
# these tests pass from an isolated worktree too.
@pytest.mark.parametrize("changed_surface", [False, True])
@pytest.mark.parametrize("missing_poller", [False, True])
def test_ui_act_defers_then_refreshes_targets_without_replaying_old_action(changed_surface, missing_poller):
    events, executed = [], []
    focus = types.SimpleNamespace(focus_list=[object()])
    old = {"id": "nvl:answer", "index": 1, "display_label": "Answer"}
    command = {"name": "act", "args": {"index": 1}, "nonce": "action-once"}
    module = parse_shim_python()
    sys_alias = next(alias.asname or alias.name for node in module.body
                     if isinstance(node, ast.Import) for alias in node.names if alias.name == "sys")
    client = types.SimpleNamespace(
        has_pending_critical_events=lambda: False,
        push_event=lambda event: events.append(dict(event)),
        push_event_sync=lambda event: events.append(dict(event)),
    )
    ns = load_shim_functions(
        "_vnf_mark_focus_snapshot_stale", "_vnf_focus_snapshot_is_current",
        "_vnf_signature_value", "_vnf_prepare_ui_command", "_vnf_execute_pending_command",
        "_vnf_missing_poller_observation_pump", "_vnf_error_screen_action_pump",
        "_vnf_execute_native_action_once", "_vnf_poller_timers_live",
        namespace={
            sys_alias: types.SimpleNamespace(), "renpy": types.SimpleNamespace(
                display=types.SimpleNamespace(focus=focus)),
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_poller_timer_tick": [100.0], "_VNF_POLLER_TIMER_STALE_S": 0.6,
            "_vnf_pending_command_box": [command], "_vnf_current_interactions": [dict(old)],
            "_vnf_client": client, "_vnf_command_result_cache": {},
            "_vnf_command_causal_boundaries": {}, "_CONTROL_EXCEPTIONS": (),
            "_vnf_log": lambda _: None, "_vnf_remember_command_result": lambda *args: None,
            "_is_renpy6": False, "_vnf_error_screen_visible": [False],
            "_vnf_native_action_queue": None, "_vnf_pending_click": None,
            "_time": types.SimpleNamespace(time=lambda: 100.0),
            "_vnf_shim_resolved_flag": [False],
        })
    ns["renpy"].exports = types.SimpleNamespace(
        get_screen=lambda name: None if missing_poller else object())
    ns["renpy"].run = lambda action: action()
    ns["raw_action"] = lambda: executed.append("old")
    def scrape(*, force=False):
        assert ns["_vnf_focus_snapshot_is_current"]()
        ns["_vnf_current_interactions"] = [dict(old, display_label="New answer")] if changed_surface else [dict(old)]
        ns["raw_action"] = lambda: executed.append("new")
        ns["_vnf_error_screen_visible"][0] = missing_poller
    ns["_vnf_scrape_visible_screens"] = scrape
    def handle(*args):
        if missing_poller:
            ns["_vnf_native_action_queue"] = ns["raw_action"]
        else:
            ns["raw_action"]()
    ns["_vnf_command_handlers"] = {"act": handle}
    ns["_vnf_mark_focus_snapshot_stale"]()
    ns["_vnf_execute_pending_command"]()
    ns["_vnf_missing_poller_observation_pump"]()
    assert ns["_vnf_pending_command_box"][0] is command
    assert events == [] and executed == []
    focus.focus_list = list(focus.focus_list)
    for _ in range(2):
        if missing_poller:
            ns["_vnf_missing_poller_observation_pump"]()
            ns["_vnf_error_screen_action_pump"]()
        else:
            ns["_vnf_execute_pending_command"]()
    assert ns["_vnf_pending_command_box"][0] is None
    assert executed == ([] if changed_surface else ["new"])
    if changed_surface:
        assert events[0]["nonce"] == "action-once"
        assert events[0]["success"] is False and events[0]["error_code"] == "stale_surface"
    # Forced dispatch capture must refresh refs even when content is identical.
    source = SHIM.read_text(encoding="utf-8")
    assert "_vnf_consecutive_unchanged_scrapes == 1 or force" in source


@pytest.mark.parametrize("poller_ticking", [False, True])
def test_pump_takes_over_when_a_present_poller_has_silenced_timers(poller_ticking):
    """Ren'Py 7 drops modal TIMEEVENTs for timers under a modal screen; its
    `_exception` screen (modal, zorder 1090) sits above the poller (999).
    The poller screen is present but inert, so the pump must scrape and
    dispatch the pending act; it still yields to a poller that is ticking."""
    now = [100.0]
    scrapes, executed = [], []
    ns = load_shim_functions(
        "_vnf_missing_poller_observation_pump", "_vnf_poller_timers_live",
        "_vnf_poller_scrape_tick", "_vnf_execute_native_action_once",
        namespace={
            "vnf_player": types.SimpleNamespace(enabled=True), "_is_renpy6": False,
            "_vnf_focus_snapshot_is_current": lambda: True,
            "_vnf_poller_timer_tick": [0.0], "_VNF_POLLER_TIMER_STALE_S": 0.6,
            "_vnf_pending_command_box": [None], "_vnf_pending_click": None,
            "_vnf_native_action_queue": None, "_vnf_shim_resolved_flag": [False],
            "_vnf_scrape_visible_screens": lambda: scrapes.append(now[0]),
            "_vnf_execute_pending_command": lambda: executed.append("act"),
            "_time": types.SimpleNamespace(time=lambda: now[0]),
            "renpy": types.SimpleNamespace(
                exports=types.SimpleNamespace(get_screen=lambda name: object()),
                run=lambda action: action()),
        })
    if poller_ticking:
        ns["_vnf_poller_scrape_tick"]()  # the poller's own timer fired
        assert scrapes == [100.0] and ns["_vnf_poller_timer_tick"] == [100.0]
        now[0] += 0.2
    ns["_vnf_missing_poller_observation_pump"]()
    assert len(scrapes) == 1
    ns["_vnf_pending_command_box"][0] = {"name": "act", "args": {"index": 1}}
    ns["_vnf_missing_poller_observation_pump"]()
    assert executed == ([] if poller_ticking else ["act"])
    # A poller that stops ticking (modal screen above it) hands over.
    now[0] += 1.0
    ns["_vnf_missing_poller_observation_pump"]()
    assert executed[-1] == "act"
    # No poller screen at all keeps the original behaviour.
    ns["renpy"].exports.get_screen = lambda name: None
    assert ns["_vnf_poller_timers_live"]() is False


def test_missing_poller_native_return_propagates_once_from_event_loop():
    class EndInteraction(Exception):
        pass
    values = []
    def end(value):
        values.append(value)
        raise EndInteraction()
    ns = load_shim_functions(
        "_vnf_missing_poller_observation_pump", "_vnf_execute_native_action_once",
        "_vnf_poller_timers_live",
        namespace={
            "vnf_player": types.SimpleNamespace(enabled=True), "_is_renpy6": False,
            "_vnf_focus_snapshot_is_current": lambda: True,
            "_vnf_poller_timer_tick": [0.0], "_VNF_POLLER_TIMER_STALE_S": 0.6,
            "_vnf_pending_command_box": [None], "_vnf_pending_click": None,
            "_vnf_native_action_queue": lambda: "ignore",
            "_vnf_scrape_visible_screens": lambda: None,
            "_vnf_shim_resolved_flag": [False],
            "_time": types.SimpleNamespace(time=lambda: 1.0),
            "renpy": types.SimpleNamespace(
                exports=types.SimpleNamespace(get_screen=lambda name: None),
                run=lambda action: action(), end_interaction=end),
        })
    with pytest.raises(EndInteraction):
        ns["_vnf_missing_poller_observation_pump"]()
    assert ns["_vnf_native_action_queue"] is None
    ns["_vnf_missing_poller_observation_pump"]()
    assert values == ["ignore"]


def test_shim_protocol_version_matches_python_contract():
    source = SHIM.read_text(encoding="utf-8")
    match = re.search(
        r"^\s*_VNFLIGHT_SHIM_PROTOCOL_VERSION\s*=\s*(\d+)\s*$",
        source,
        re.MULTILINE,
    )
    assert match, "vnflight.rpy must declare its wire protocol version"
    assert int(match.group(1)) == SHIM_PROTOCOL_VERSION
    assert '"shim_protocol_version": _VNFLIGHT_SHIM_PROTOCOL_VERSION' in source
    assert 'headers["X-VNFlight-Shim-Protocol"]' in source
    assert "retry_window=(8.0 if vnf_player._launch_id else 0.0)" in source
    assert 'os.environ.get("VNFLIGHT_LAUNCH_ID")' in source
    assert "if _vnf_slot_assigned:" in source
    assert "self._cfg.enabled = False" in source


@pytest.mark.parametrize("failure", ["transport", "missing_slot"])
def test_initial_slot_assignment_failure_disables_shim(failure):
    import threading
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )

    class Response:
        def read(self):
            return json.dumps({
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            }).encode("utf-8")

    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url
            self.data = data
            self.headers = headers
            self.method = None

    def urlopen(request, timeout=0):
        if failure == "transport":
            raise OSError("bridge unavailable")
        return Response()

    namespace = {
        "_HAS_URLLIB": True,
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "_is_legacy": False,
        "_tb_module": types.SimpleNamespace(print_exc=lambda: None),
        "_urllib_parse": types.SimpleNamespace(urlencode=lambda value: ""),
        "_urllib_request": types.SimpleNamespace(Request=Request, urlopen=urlopen),
        "_vnf_log": lambda value: None,
        "json": json,
        "os": __import__("os"),
        "threading": threading,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    config = types.SimpleNamespace(
        enabled=True,
        bridge_url="http://127.0.0.1:8385",
        slot_token="token",
        debug=False,
    )
    client = namespace["VNFBridgeClient"](config)

    assert client.assign_slot(disable_on_failure=True) is False
    assert config.enabled is False
    assert client.slot_id is None


def test_recovery_slot_assignment_failure_remains_retryable():
    import threading
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )

    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url
            self.data = data
            self.headers = headers
            self.method = None

    receipts = []
    namespace = {
        "_HAS_URLLIB": True,
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "_is_legacy": False,
        "_tb_module": types.SimpleNamespace(print_exc=lambda: None),
        "_urllib_parse": types.SimpleNamespace(urlencode=lambda value: ""),
        "_urllib_request": types.SimpleNamespace(
            Request=Request,
            urlopen=lambda request, timeout=0: (_ for _ in ()).throw(
                OSError("replacement bridge still starting")
            ),
        ),
        "_vnf_record_launch_registration": lambda *a, **k: receipts.append((a, k)),
        "_vnf_log": lambda value: None,
        "json": json,
        "os": __import__("os"),
        "threading": threading,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    config = types.SimpleNamespace(
        enabled=True,
        bridge_url="http://127.0.0.1:8385",
        slot_token="token",
        debug=False,
        _launch_file={"launch_id": "retryable-launch"},
    )
    client = namespace["VNFBridgeClient"](config)

    assert client.assign_slot() is False
    assert config.enabled is True
    assert receipts == []


@pytest.mark.parametrize("failure", ["malformed", "protocol", "http_409"])
@pytest.mark.parametrize("disable_on_failure", [False, True])
def test_terminal_slot_assignment_respects_disable_mode(
    failure, disable_on_failure,
):
    import threading
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )

    class Response:
        def read(self):
            if failure == "malformed":
                return b"not-json"
            return json.dumps({
                "slot_id": 7, "shim_protocol_version": 999,
            }).encode("utf-8")

    class HTTPError(Exception):
        code = 409

        def read(self):
            return b'{"error":"reservation conflict"}'

    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url
            self.data = data
            self.headers = headers
            self.method = None

    def urlopen(request, timeout=0):
        if failure == "http_409":
            raise HTTPError()
        return Response()

    receipts = []
    namespace = {
        "_HAS_URLLIB": True,
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "_is_legacy": False,
        "_tb_module": types.SimpleNamespace(print_exc=lambda: None),
        "_urllib_parse": types.SimpleNamespace(urlencode=lambda value: ""),
        "_urllib_request": types.SimpleNamespace(Request=Request, urlopen=urlopen),
        "_vnf_record_launch_registration": lambda *a, **k: receipts.append((a, k)),
        "_vnf_log": lambda value: None,
        "json": json,
        "os": __import__("os"),
        "threading": threading,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    config = types.SimpleNamespace(
        enabled=True,
        bridge_url="http://127.0.0.1:8385",
        slot_token="token",
        debug=False,
        _launch_file={"launch_id": "candidate-launch"},
    )
    client = namespace["VNFBridgeClient"](config)

    assert client.assign_slot(disable_on_failure=disable_on_failure) is False
    assert config.enabled is (not disable_on_failure)
    assert receipts[-1][0][0]["launch_id"] == "candidate-launch"
    assert receipts[-1][0][1] == "failed"


@pytest.mark.parametrize("transient_failure", ["transport", "reserved"])
def test_initial_slot_assignment_retries_transient_failure_with_same_identity(
    transient_failure,
):
    import threading
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )

    class Response:
        def read(self):
            return json.dumps({
                "slot_id": 7,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            }).encode("utf-8")

    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url
            self.data = data
            self.headers = headers
            self.method = None

    class ReservationPending(Exception):
        code = 409

        def read(self):
            return b'{"error":"handoff pending","status":"reserved"}'

    calls = []

    def urlopen(request, timeout=0):
        calls.append((request.data, request.headers, timeout))
        if len(calls) == 1:
            if transient_failure == "reserved":
                raise ReservationPending()
            raise OSError("bridge still starting")
        return Response()

    receipts = []
    namespace = {
        "_HAS_URLLIB": True,
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "_is_legacy": False,
        "_tb_module": types.SimpleNamespace(print_exc=lambda: None),
        "_urllib_parse": types.SimpleNamespace(urlencode=lambda value: ""),
        "_urllib_request": types.SimpleNamespace(Request=Request, urlopen=urlopen),
        "_vnf_record_launch_registration": lambda *a, **k: receipts.append((a, k)),
        "_vnf_log": lambda value: None,
        "json": json,
        "os": __import__("os"),
        "threading": threading,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    config = types.SimpleNamespace(
        enabled=True, bridge_url="http://127.0.0.1:8385",
        slot_token="stable-token", debug=False,
        _launch_file={"launch_id": "launch-1"},
    )

    client = namespace["VNFBridgeClient"](config)
    assert client.assign_slot(disable_on_failure=True, retry_window=1.0) is True
    assert client.slot_id == 7
    assert config.enabled is True
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    assert calls[0][1]["X-Slot-Token"] == "stable-token"
    assignment = json.loads(calls[0][0].decode("utf-8"))
    assert assignment["registration_retry_mode"] == "finite"
    assert assignment["registration_retry_until"] > 0
    assert receipts[-1][0][1] == "assigned"


def test_direct_launch_assignment_sends_environment_launch_id():
    import threading
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )

    class Response:
        def read(self):
            return json.dumps({
                "slot_id": 7,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            }).encode("utf-8")

    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url
            self.data = data
            self.headers = headers
            self.method = None

    class RevertableDict(dict):
        """Mirror Ren'Py's store-level replacement for the dict name."""

    requests = []

    def urlopen(request, timeout=0):
        requests.append(request)
        return Response()

    namespace = {
        "_HAS_URLLIB": True,
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "_is_legacy": False,
        "_tb_module": types.SimpleNamespace(print_exc=lambda: None),
        "_urllib_parse": types.SimpleNamespace(urlencode=lambda value: ""),
        "_urllib_request": types.SimpleNamespace(Request=Request, urlopen=urlopen),
        "_vnf_log": lambda value: None,
        "json": json,
        "dict": RevertableDict,
        "os": __import__("os"),
        "threading": threading,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    config = types.SimpleNamespace(
        enabled=True,
        bridge_url="http://127.0.0.1:8385",
        slot_token="token",
        debug=False,
        _launch_file=None,
        _launch_id="direct-launch-id",
    )

    assert namespace["VNFBridgeClient"](config).assign_slot() is True
    assignment = json.loads(requests[0].data.decode("utf-8"))
    assert assignment["launch_id"] == "direct-launch-id"
    assert assignment["registration_retry_mode"] == "single"
    assert "registration_retry_until" not in assignment


def test_transactional_command_nonce_cache_prevents_double_execution():
    source = SHIM.read_text(encoding="utf-8")

    assert "_vnf_command_result_cache = {}" in source
    assert "_vnf_remember_command_result(_cmd_nonce" in source
    assert "Replaying cached command result for nonce" in source
    assert "_vnf_client.push_event_sync(dict(_cached_result))" in source


_PYTHON_BLOCK_RE = re.compile(
    r"^\s*(?:(?:init(?:\s+[-\d]+)?\s+python)|(?:python(?:\s+\w+)?)):\s*$"
)


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def iter_python_blocks(source: str) -> list[tuple[int, str]]:
    """Return ``(start_line, code)`` for Ren'Py Python blocks."""
    lines = source.splitlines()
    blocks: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not _PYTHON_BLOCK_RE.match(line):
            i += 1
            continue

        header_indent = _indent_width(line)
        start_line = i + 1
        i += 1
        body: list[str] = []
        while i < len(lines):
            current = lines[i]
            if current.strip() and _indent_width(current) <= header_indent:
                break
            body.append(current)
            i += 1
        blocks.append((start_line, textwrap.dedent("\n".join(body)).strip()))
    return blocks


def parse_rpy_python(path: Path) -> ast.Module:
    source = path.read_text(encoding="utf-8")
    merged = "\n\n".join(
        f"# from {path.name}:{line_no}\n{block}"
        for line_no, block in iter_python_blocks(source)
        if block
    )
    return ast.parse(merged, filename=str(path))


def parse_shim_python() -> ast.Module:
    return parse_rpy_python(SHIM)


def function_node(module: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"Function not found in shim Python blocks: {name}")


def test_shim_routes_text_conversion_through_unicode_safe_helpers():
    """Bare str(value) is unsafe for non-ASCII unicode on Ren'Py 6/7."""
    module = parse_shim_python()
    bare_calls: list[tuple[str, str]] = []
    unsafe_callbacks: list[tuple[str, str]] = []
    unsafe_exception_formats: list[tuple[str, str]] = []
    allowed_protocol_calls = [
        ("_vnf_stringify", "str(value)"),
        ("VNFPlayerConfig/__init__", "str(self._launch_file['launch_id'])"),
        ("VNFPlayerConfig/__init__", "str(self._launch_file['bridge_url'])"),
        ("VNFPlayerConfig/__init__", "str(_lf_token)"),
        ("VNFPlayerConfig/__init__", "str(_lf_slot)"),
        ("VNFBridgeClient/__init__", "str(uuid.uuid4())"),
        ("VNFBridgeClient/__init__", "str(uuid.uuid4())"),
        ("VNFBridgeClient/_url", "str(self.slot_id)"),
        ("VNFBridgeClient/_auth_headers",
         "str(_VNFLIGHT_SHIM_PROTOCOL_VERSION)"),
        ("VNFBridgeClient/assign_slot", "str(_assignment_launch_id)"),
        ("VNFBridgeClient/_recover_stale_slot", "str(save_slot)"),
        ("VNFBridgeClient/_recover_stale_slot", "str(bridge_url)"),
        ("VNFBridgeClient/_recover_stale_slot", "str(token)"),
        ("VNFBridgeClient/_recover_stale_slot", "str(bridge_url)"),
        ("VNFBridgeClient/_recover_stale_slot", "str(token)"),
        ("VNFBridgeClient/_recover_stale_slot",
         "str(launch.get('launch_id'))"),
        ("VNFBridgeClient/push_request", "str(uuid.uuid4())"),
    ]

    class BareStrVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.exception_names: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            name = node.name if isinstance(node.name, str) else None
            if name is not None:
                self.exception_names.append(name)
            for statement in node.body:
                self.visit(statement)
            if name is not None:
                self.exception_names.pop()

        def _contains_raw_exception(self, node: ast.AST) -> bool:
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_vnf_text"
            ):
                return False
            if (
                isinstance(node, ast.Name)
                and node.id in self.exception_names
            ):
                return True
            return any(
                self._contains_raw_exception(child)
                for child in ast.iter_child_nodes(node)
            )

        def visit_Call(self, node: ast.Call) -> None:
            path = "/".join(self.scope)
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "str"
            ):
                bare_calls.append((path, ast.unparse(node)))
            for keyword in node.keywords:
                if (
                    keyword.arg == "default"
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id == "str"
                ):
                    unsafe_callbacks.append((path, ast.unparse(node)))
            if (
                self.exception_names
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
                and any(
                    self._contains_raw_exception(value)
                    for value in (
                        list(node.args)
                        + [keyword.value for keyword in node.keywords]
                    )
                )
            ):
                unsafe_exception_formats.append((path, ast.unparse(node)))
            self.generic_visit(node)

        def visit_BinOp(self, node: ast.BinOp) -> None:
            if (
                self.exception_names
                and isinstance(node.op, ast.Mod)
                and self._contains_raw_exception(node.right)
            ):
                unsafe_exception_formats.append(
                    ("/".join(self.scope), ast.unparse(node))
                )
            self.generic_visit(node)

    BareStrVisitor().visit(module)
    assert bare_calls == allowed_protocol_calls, (
        "Use _vnf_text for store, event, UI, and diagnostic conversions. "
        "Only fixed-shape ASCII transport identifiers may use bare str(); "
        f"actual calls: {bare_calls}"
    )
    assert unsafe_callbacks == [], (
        "JSON/event fallback callbacks must use _vnf_text, not bare str(): "
        f"{unsafe_callbacks}"
    )
    assert unsafe_exception_formats == [], (
        "Exception diagnostics must route exception values through _vnf_text "
        "before interpolation: "
        f"{unsafe_exception_formats}"
    )


def test_shim_container_predicates_accept_native_and_revertable_values():
    module = parse_shim_python()
    wanted_assignments = {"_VNF_NATIVE_DICT_TYPE", "_VNF_NATIVE_LIST_TYPE"}
    wanted_functions = {"_vnf_is_mapping", "_vnf_is_list", "_vnf_is_sequence"}
    nodes = []
    for node in module.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in wanted_assignments
                for target in node.targets
            )
        ) or (
            isinstance(node, ast.FunctionDef) and node.name in wanted_functions
        ):
            nodes.append(node)

    class RevertableDict(dict):
        pass

    class RevertableList(list):
        pass

    namespace = {
        "json": json,
        "dict": RevertableDict,
        "list": RevertableList,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), SHIM, "exec"), namespace)

    assert namespace["_vnf_is_mapping"](json.loads('{"item": 1}'))
    assert namespace["_vnf_is_mapping"](RevertableDict(item=1))
    assert namespace["_vnf_is_list"](json.loads('[1]'))
    assert namespace["_vnf_is_list"](RevertableList([1]))
    assert namespace["_vnf_is_sequence"]((1, 2))


def test_progress_nodes_can_defer_label_visit_until_passive_check():
    module = parse_shim_python()
    wanted = {
        "_vnf_resolve_graph_state",
        "_vnf_on_label",
        "_vnf_periodic_progress_check",
    }
    nodes = [
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    reached = [False]
    events = []
    namespace = {
        "_vnf_progress_graph": {
            "ending_aurora": {
                "check": lambda: reached[0],
                "next": [],
                "terminal": True,
                "game_terminal": True,
                "thread": "main",
                "label": "Ending: Aurora",
                "label_trigger": False,
            },
            "ordinary_label": {
                "next": [],
                "terminal": False,
                "game_terminal": False,
                "thread": "main",
                "label": "Ordinary",
            },
        },
        "_vnf_label_history": [("ending_aurora", 1.0)],
        "_vnf_current_progress_node": None,
        "_vnf_progress_emitted": set(),
        "_VNF_MAX_LABEL_HISTORY": 500,
        "_time": types.SimpleNamespace(time=lambda: 2.0),
        "_vnf_client": types.SimpleNamespace(push_event=events.append),
        "_vnf_log": lambda _message: None,
        "vnf_player": types.SimpleNamespace(enabled=True),
    }
    exec_shim_nodes(nodes, namespace)

    before_title = namespace["_vnf_resolve_graph_state"]()
    assert "ending_aurora" not in before_title["nodes"]["visited"]
    assert before_title["game_terminal"] is False

    namespace["_vnf_on_label"]("ending_aurora")
    assert events == []
    assert namespace["_vnf_current_progress_node"] is None

    reached[0] = True
    at_title = namespace["_vnf_resolve_graph_state"]()
    assert at_title["nodes"]["terminal"] == ["ending_aurora"]
    assert at_title["game_terminal"] is True
    namespace["_vnf_periodic_progress_check"]()
    assert [event["to"] for event in events] == ["ending_aurora"]
    assert events[0]["game_terminal"] is True
    namespace["_vnf_periodic_progress_check"]()
    assert len(events) == 1
    reached[0] = False
    namespace["_vnf_periodic_progress_check"]()
    reached[0] = True
    namespace["_vnf_periodic_progress_check"]()
    assert [event["to"] for event in events] == [
        "ending_aurora", "ending_aurora",
    ]

    namespace["_vnf_on_label"]("ordinary_label")
    assert events[-1]["to"] == "ordinary_label"


def test_generic_inventory_changes_tolerate_non_mapping_rows_and_items():
    inventory = {}
    renpy = types.SimpleNamespace(
        store=types.SimpleNamespace(inventory=inventory),
    )
    namespace = load_shim_functions(
        "_vnf_apply_inventory_changes",
        namespace={"renpy": renpy, "basestring": str},
    )

    result = namespace["_vnf_apply_inventory_changes"]([
        None,
        {"action": "add", "item": 7},
    ])

    assert result["success"] is True
    assert inventory == {"7": 1}


def test_inventory_capture_keeps_non_ascii_and_unprintable_items_isolated():
    class Unprintable:
        def __str__(self):
            raise UnicodeEncodeError("ascii", "x", 0, 1, "test")

    store = types.SimpleNamespace(inventory=[
        types.SimpleNamespace(
            name="Zażółć",
            quantity=2,
            description="Pamiątka",
        ),
        Unprintable(),
    ])
    ns = load_shim_functions(
        "_vnf_get_inventory_stats",
        namespace={
            "renpy": types.SimpleNamespace(store=store),
            "vnf_player": types.SimpleNamespace(debug=False),
            "_vnf_log": lambda _message: None,
            "_tb_module": types.SimpleNamespace(format_exc=lambda: ""),
        },
    )

    inventory, stats = ns["_vnf_get_inventory_stats"]()

    assert inventory == [
        {"name": "Zażółć", "quantity": 2,
         "description": "Pamiątka"},
        {"name": ""},
    ]
    assert stats == {}


def assigned_dict_fields(fn: ast.FunctionDef, name: str) -> set[str]:
    """Collect literal fields assigned to one dict variable in a function."""
    fields: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                if isinstance(node.value, ast.Dict):
                    fields.update(
                        key.value for key in node.value.keys
                        if isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                    )
                elif (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "dict"
                ):
                    fields.update(
                        keyword.arg for keyword in node.value.keywords
                        if keyword.arg is not None
                    )
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == name
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                fields.add(target.slice.value)
    return fields


def appended_dict_field_sets(
    fn: ast.FunctionDef,
    name: str,
) -> list[frozenset[str]]:
    """Return literal dict schemas appended to one list variable."""
    out: list[frozenset[str]] = []
    for node in ast.walk(fn):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
        ):
            continue
        assert len(node.args) == 1
        assert isinstance(node.args[0], ast.Dict)
        item = node.args[0]
        keys = {
            key.value for key in item.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        assert len(keys) == len(item.keys)
        out.append(frozenset(keys))
    return out


def test_actionable_schema_matches_shim_emitters():
    """Keep bridge projections tied to the dicts built by vnflight.rpy."""
    module = parse_shim_python()

    interaction_fields = assigned_dict_fields(
        function_node(module, "_vnf_build_interactions"), "interaction",
    )
    assert interaction_fields - {"_raw_ref"} == SHIM_INTERACTION_FIELDS

    game_state_button_fields = assigned_dict_fields(
        function_node(module, "_vnf_push_game_state"), "_eb",
    )
    assert game_state_button_fields == SHIM_GAME_STATE_BUTTON_FIELDS

    menu_wrapper = function_node(module, "_wrapper_inner")
    assert assigned_dict_fields(menu_wrapper, "_btn_dict") == (
        SHIM_REQUEST_BUTTON_FIELDS
    )
    request_choice_schemas = appended_dict_field_sets(
        menu_wrapper, "_enriched_choices",
    )
    assert len(request_choice_schemas) == 2
    assert all(
        fields == SHIM_REQUEST_CHOICE_FIELDS
        for fields in request_choice_schemas
    )
    assert assigned_dict_fields(menu_wrapper, "_req_kwargs") == (
        SHIM_CHOICE_REQUEST_PAYLOAD_FIELDS
    )

    presentation_fields = {"annotation", "category", "_category", "is_selected"}
    emitted_actionable = (
        SHIM_INTERACTION_FIELDS
        | SHIM_GAME_STATE_BUTTON_FIELDS
        | SHIM_REQUEST_BUTTON_FIELDS
        | SHIM_REQUEST_CHOICE_FIELDS
    ) - presentation_fields
    assert emitted_actionable <= ACTIONABLE_ITEM_FIELDS
    assert GameState._ACTIONABLE_ITEM_FIELDS == ACTIONABLE_ITEM_FIELDS
    assert GameState._ACTIONABLE_REQUEST_FIELDS == ACTIONABLE_REQUEST_FIELDS
    assert GameState._ACTIONABLE_REQUEST_TARGET_FIELDS == (
        ACTIONABLE_REQUEST_TARGET_FIELDS
    )


def exec_shim_nodes(nodes: list[ast.AST], namespace: dict | None = None) -> dict:
    """Compile shim nodes with the shared predicates they use at runtime."""
    module = parse_shim_python()
    support_assignments = {"_VNF_NATIVE_DICT_TYPE", "_VNF_NATIVE_LIST_TYPE"}
    selected = [
        node for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id in support_assignments
            for target in node.targets
        )
    ]
    support_functions = (
        "_vnf_is_mapping", "_vnf_is_list", "_vnf_is_sequence",
        "_vnf_stringify", "_vnf_text",
    )
    selected.extend(function_node(module, name) for name in support_functions)
    selected.extend(
        node for node in nodes
        if not (
            isinstance(node, ast.FunctionDef)
            and node.name in support_functions
        )
    )
    tree = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(tree)
    loaded = namespace if namespace is not None else {}
    loaded.setdefault("json", json)
    loaded.setdefault("basestring", str)
    loaded.setdefault("bytes", bytes)
    loaded.setdefault("_vnf_observe_rollback_resume", lambda: None)
    exec(compile(tree, str(SHIM), "exec"), loaded)
    return loaded


def load_shim_functions(*names: str, namespace: dict | None = None) -> dict:
    """Compile selected top-level shim functions for behavior tests."""
    module = parse_shim_python()
    nodes = [function_node(module, name) for name in names]
    return exec_shim_nodes(nodes, dict(namespace or {}))


def test_text_fallback_decodes_python2_non_ascii_bytes():
    class FormatFailure:
        def __format__(self, _spec):
            raise UnicodeEncodeError("ascii", "\u0141", 0, 1, "non-ascii")

    ns = load_shim_functions(
        "_vnf_stringify", "_vnf_text",
        namespace={
            "basestring": (str, bytes),
            "str": lambda _value: b"\xff",
        },
    )

    assert ns["_vnf_text"](FormatFailure()) == "\ufffd"


def global_names(fn: ast.FunctionDef) -> set[str]:
    out: set[str] = set()
    for node in fn.body:
        if isinstance(node, ast.Global):
            out.update(node.names)
    return out


def test_shim_renpy_version_parser_handles_build_suffixes():
    source = SHIM.read_text(encoding="utf-8")
    assert "def _vnf_parse_renpy_version" in source
    assert "tuple(int(x) for x in version_part.split('.'))" not in source


def test_renpy6_filter_text_tags_preserves_non_ascii_unicode():
    class Py2UnicodeLike(str):
        def __str__(self):
            raise UnicodeEncodeError("ascii", "Ł", 0, 1, "non-ascii")

    ns = load_shim_functions(
        "_vnf_filter_text_tags",
        namespace={
            "_compat_re": re,
            "basestring": str,
        },
    )

    value = Py2UnicodeLike("Jestem {i}tutaj{/i} — Łucja")
    assert ns["_vnf_filter_text_tags"](value) == "Jestem tutaj — Łucja"


def test_menu_and_input_text_paths_use_unicode_safe_conversion():
    source = SHIM.read_text(encoding="utf-8")

    assert "filter_text_tags(str(lbl)" not in source
    assert "_image_tag_re.findall(str(label))" not in source
    assert "filter_text_tags(str(prompt)" not in source
    assert "_vnf_stringify(lbl) or \"\"" in source
    assert "_vnf_stringify(label) or \"\"" in source
    assert "_vnf_stringify(prompt) or \"\"" in source


def test_shim_has_action_label_fallback_for_image_buttons():
    source = SHIM.read_text(encoding="utf-8")
    module = parse_shim_python()

    function_node(module, "_vnf_action_label_hint")
    assert '"ShowMenu"' in source
    assert '"Jump"' in source
    assert '"Start Game"' in source
    assert "label = _vnf_action_label_hint(action)" in source
    assert 'getattr(func, "__name__", "") == "_returns"' in source
    assert '"_page"' in source


def test_focus_choice_merge_dedups_markup_variants_with_disabled_middle_choice():
    def filter_text_tags(value, allow=None):
        assert allow == set()
        return re.sub(r"\{[^}]*\}", "", value)

    renpy = types.SimpleNamespace(
        text=types.SimpleNamespace(
            extras=types.SimpleNamespace(
                filter_text_tags=filter_text_tags
            )
        )
    )
    ns = load_shim_functions(
        "_vnf_normalize_focus_label",
        "_vnf_merge_scraped_choice_labels",
        namespace={"renpy": renpy},
    )
    merge = ns["_vnf_merge_scraped_choice_labels"]

    choices = [
        {"label": "{i}\u2022 [[Give up.]{/i}", "index": 1, "disabled": False},
        {
            "label": "{i}\u2022 [[Finish the job.]{/i}",
            "index": None,
            "disabled": True,
        },
        {
            "label": "{i}\u2022 [[Flee and lock her in the basement.]{/i}",
            "index": 2,
            "disabled": False,
        },
    ]

    merged = merge(
        choices,
        ["[Give up.]", "[Flee and lock her in the basement.]"],
        {"[Give up.]": 1, "[Flee and lock her in the basement.]": 2},
    )

    assert merged is choices
    assert len(merged) == 3
    assert [choice["index"] for choice in merged] == [1, None, 2]


def test_focus_choice_merge_still_appends_a_genuinely_new_live_choice():
    renpy = types.SimpleNamespace(
        text=types.SimpleNamespace(
            extras=types.SimpleNamespace(
                filter_text_tags=lambda value, allow=None: value
            )
        )
    )
    ns = load_shim_functions(
        "_vnf_normalize_focus_label",
        "_vnf_merge_scraped_choice_labels",
        namespace={"renpy": renpy},
    )
    choices = [{"label": "\u2022 [Stay.]", "index": 1, "disabled": False}]

    ns["_vnf_merge_scraped_choice_labels"](
        choices,
        ["[Stay.]", "[Run.]"],
        {"[Stay.]": 1, "[Run.]": 2},
    )

    assert [choice["label"] for choice in choices] == ["\u2022 [Stay.]", "[Run.]"]
    assert choices[-1]["index"] == 2


def test_focus_choice_action_dedup_uses_normalized_label_key():
    renpy = types.SimpleNamespace(
        text=types.SimpleNamespace(
            extras=types.SimpleNamespace(
                filter_text_tags=lambda value, allow=None: re.sub(
                    r"\{[^}]*\}", "", value
                )
            )
        )
    )
    ns = load_shim_functions(
        "_vnf_normalize_focus_label",
        "_vnf_action_transform_dedup_choice_button",
        namespace={"renpy": renpy},
    )
    transform = ns["_vnf_action_transform_dedup_choice_button"]
    actions = [
        {
            "source": "choice",
            "label": "{i}\u2022 [[Give up.]{/i}",
            "screen": "",
            "actions": [],
        },
        {
            "source": "button",
            "label": "[Give up.]",
            "screen": "_focus_list",
            "actions": ["ChoiceReturn"],
        },
        {
            "source": "button",
            "label": "[Give up.]",
            "screen": "journal",
            "actions": ["Jump"],
        },
    ]

    result = transform(
        actions,
        {"has_choices": True, "choice_labels": ["{i}\u2022 [[Give up.]{/i}"]},
    )

    assert result == [actions[0], actions[2]]


def assigned_names(fn: ast.FunctionDef) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(fn):
        for target in getattr(node, "targets", []):
            if isinstance(target, ast.Name):
                out.add(target.id)
    return out


def test_shim_python_blocks_parse():
    blocks = iter_python_blocks(SHIM.read_text(encoding="utf-8"))
    assert len(blocks) >= 4
    parse_shim_python()


def test_bridge_mutations_share_one_source_ordered_outbox():
    module = parse_shim_python()
    post_async = function_node(module, "_post_async")
    queue_post = function_node(module, "_queue_post")
    outbox_worker = function_node(module, "_outbox_worker")
    push_request = function_node(module, "push_request")

    assert "threading.Thread" not in ast.unparse(post_async)
    assert "_queue_post" in ast.unparse(post_async)
    assert "_source_id" in ast.unparse(queue_post)
    assert "_source_seq" in ast.unparse(queue_post)
    assert "_source_ts" in ast.unparse(queue_post)
    assert "done.wait(remaining)" in ast.unparse(queue_post)
    assert "self._post" in ast.unparse(outbox_worker)
    assert "_queue_post" in ast.unparse(push_request)


def test_choice_request_caption_is_registered_for_screen_text_dedup():
    import threading
    import time
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": lambda *args: None,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    client._scene_texts = set()
    sent = []
    client._queue_post = lambda path, event, **kwargs: sent.append(
        (path, event.copy())
    ) or {"ok": True}

    client.push_request(
        "choice_request",
        choices=["Tell him", "Yes"],
        full_items=[
            {"label": "Should I tell him?", "is_caption": True,
             "is_disabled": False},
            {"label": "Tell him", "is_caption": False,
             "is_disabled": False},
            {"label": "Yes", "is_caption": False,
             "is_disabled": False},
        ],
    )
    scraped = client._dedup_event({
        "type": "screen_content",
        "texts": [
            "Should I tell him?", "Tell him", "No",
            "No further warnings are available.", "unrelated",
        ],
    })

    assert sent[0][0] == "/request"
    assert scraped["texts"] == [
        "Tell him", "No", "No further warnings are available.", "unrelated",
    ]

    client._dedup_event({
        "type": "choice_resolved", "request_id": sent[0][1]["id"],
    })
    after_resolution = client._dedup_event({
        "type": "screen_content", "texts": ["Should I tell him?"],
    })
    assert after_resolution["texts"] == ["Should I tell him?"]


@pytest.mark.parametrize("boundary", ["game_started", "game_resumed", "mod_loaded"])
def test_timeline_boundary_retires_request_caption_dedup(boundary):
    import threading
    import time
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"), "threading": threading,
        "time": time, "uuid": uuid, "_vnf_log": lambda *args: None,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    client._request_caption_texts = {"Old prompt"}

    client._dedup_event({"type": boundary})
    scraped = client._dedup_event({
        "type": "screen_content", "texts": ["Old prompt"],
    })

    assert scraped["texts"] == ["Old prompt"]


def test_choice_request_dedup_uses_python2_safe_string_predicate():
    source = ast.unparse(
        function_node(parse_shim_python(), "_dedup_event")
    )

    assert "isinstance(c, basestring)" in source
    assert "isinstance(c, str)" not in source


def test_bridge_outbox_sends_prior_events_before_sync_boundary():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    sent = []
    def record_post(path, data, timeout=2.0):
        sent.append((path, data["value"], data["_source_seq"]))
        return {"ok": True}

    client._post = record_post

    client._queue_post("/event", {"value": "story"})
    client._queue_post("/request", {"value": "choice"}, wait=True)

    assert sent == [
        ("/event", "story", 1),
        ("/request", "choice", 2),
    ]


def test_bridge_outbox_preserves_a_concurrent_burst_before_request_boundary():
    """A loaded producer burst remains one contiguous source chronology."""
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    sent = []
    sent_lock = threading.Lock()

    def slow_post(path, data, timeout=2.0):
        # Make queue pressure visible while keeping completion deterministic.
        time.sleep(0.001)
        with sent_lock:
            sent.append((path, data["_source_seq"]))
        return {"ok": True}

    client._post = slow_post
    producers = []
    for producer in range(8):
        thread = threading.Thread(target=lambda owner=producer: [
            client._queue_post("/event", {"value": (owner, index)})
            for index in range(25)
        ])
        producers.append(thread)
        thread.start()
    for thread in producers:
        thread.join()

    client._queue_post("/request", {"value": "choice"}, wait=True)

    assert [source_seq for _path, source_seq in sent] == list(range(1, 202))
    assert sent[-1] == ("/request", 201)


def test_screenshot_lane_cannot_block_story_request_barrier():
    import threading
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": __import__("time"),
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    screenshot_started = threading.Event()
    release_screenshot = threading.Event()
    sent = []

    def post(path, data, timeout=2.0):
        if data.get("type") == "screenshot":
            screenshot_started.set()
            release_screenshot.wait(2.0)
        sent.append((path, data.get("type"), data["_source_id"]))
        return {"ok": True}

    client._post = post
    client._queue_post("/event", {"type": "screenshot", "image": "large"})
    assert screenshot_started.wait(1.0)

    result = client._queue_post(
        "/request", {"type": "choice_request"}, wait=True)

    assert result == {"ok": True}
    assert sent == [("/request", "choice_request", client._source_id)]
    release_screenshot.set()


def test_screenshot_lane_coalesces_waiting_frames_to_latest():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    first_started = threading.Event()
    release_first = threading.Event()
    sent = []

    def post(path, data, timeout=2.0):
        if data.get("image") == "A":
            first_started.set()
            release_first.wait(2.0)
        sent.append(data.get("image"))
        return {"ok": True}

    client._post = post
    client._queue_post("/event", {"type": "screenshot", "image": "A"})
    assert first_started.wait(1.0)
    client._queue_post("/event", {"type": "screenshot", "image": "B"})
    client._queue_post("/event", {"type": "screenshot", "image": "C"})
    release_first.set()
    deadline = time.time() + 1.0
    while len(sent) < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert sent == ["A", "C"]


def test_repeated_bridge_failures_drop_async_backlog_but_keep_barrier():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    logs = []
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": logs.append,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    client._post = lambda path, data, timeout=2.0: None

    for index in range(20):
        client._queue_post("/event", {"type": "narration", "value": index})
    barrier_result = []
    barrier = threading.Thread(target=lambda: barrier_result.append(
        client._queue_post(
            "/request", {"type": "choice_request"}, wait=True)))
    barrier.start()
    barrier.join(2.0)

    assert not barrier.is_alive()
    assert barrier_result == [None]
    assert any("discarded" in message for message in logs)
    with client._outbox_condition:
        assert not client._outbox
        assert type(client._outbox) is namespace["_VNF_NATIVE_LIST_TYPE"]


def test_bridge_failure_fast_drain_does_not_shorten_barrier_timeout():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    observed_timeouts = []

    def record_failure(_path, _data, timeout=2.0):
        observed_timeouts.append(timeout)
        return None

    client._post = record_failure
    client._outbox_failure_streak = 1

    client._queue_post("/event", {"type": "narration"}, timeout=2.0)
    client._queue_post(
        "/request", {"type": "choice_request"}, timeout=2.0, wait=True,
    )

    assert observed_timeouts[0] == 0.25
    assert 1.9 <= observed_timeouts[1] <= 2.0


def test_bridge_barrier_expires_in_queue_without_late_submission():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": lambda _message: None,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    first_started = threading.Event()
    release_first = threading.Event()
    sent = []

    def blocked_post(path, data, timeout=2.0):
        sent.append((path, data.get("type")))
        if data.get("type") == "narration":
            first_started.set()
            release_first.wait(1.0)
        return {"ok": True}

    client._post = blocked_post
    client._queue_post("/event", {"type": "narration"})
    assert first_started.wait(1.0)

    started = time.time()
    result = client._queue_post(
        "/request", {"type": "choice_request"}, timeout=0.05, wait=True,
    )
    elapsed = time.time() - started
    with client._outbox_condition:
        assert not any(
            item["path"] == "/request" for item in client._outbox
        )
    release_first.set()
    assert client._queue_post(
        "/reset", {"type": "probe"}, timeout=1.0, wait=True,
    ) == {"ok": True}

    assert result is None
    assert elapsed < 0.5
    assert sent == [("/event", "narration"), ("/reset", "probe")]


def test_failed_request_publication_retries_the_same_stable_id():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    logs = []
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": logs.append,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    attempts = []

    def fail_then_succeed(path, data, **_kwargs):
        attempts.append((path, data["id"]))
        if len(attempts) == 1:
            return None
        return {"ok": True}

    client._queue_post = fail_then_succeed

    request_id = client.push_request(
        "choice_request", choices=["Continue"])
    deadline = time.time() + 1.0
    while len(attempts) < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert request_id is not None
    assert attempts == [
        ("/request", request_id),
        ("/request", request_id),
    ]
    assert any("retrying stable request ID" in message for message in logs)


def test_started_request_timeout_is_reconciled_as_acceptance_unknown():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    logs = []
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": logs.append,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    sent = []

    def late_success(path, data, timeout=2.0):
        time.sleep(0.5)
        sent.append((path, data["id"]))
        return {"ok": True}

    client._post = late_success
    original_queue_post = client._queue_post

    def short_deadline(path, data, timeout=2.0, wait=False):
        return original_queue_post(path, data, timeout=0.05, wait=wait)

    client._queue_post = short_deadline
    started = time.time()
    request_id = client.push_request(
        "choice_request", choices=["Continue"])
    elapsed = time.time() - started
    deadline = time.time() + 1.0
    while not sent and time.time() < deadline:
        time.sleep(0.01)

    assert request_id is not None
    assert elapsed < 0.4
    assert sent == [("/request", request_id)]
    assert any("acknowledgement timed out" in message for message in logs)
    deadline = time.time() + 1.0
    while client._request_retry_threads and time.time() < deadline:
        time.sleep(0.01)
    assert client._request_retry_threads == {}


def test_closing_interaction_cancels_request_publication_retry():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": lambda _message: None,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    attempts = []
    client._queue_post = lambda path, data, **_kwargs: (
        attempts.append((path, data["id"])) or None
    )

    request_id = client.push_request(
        "input_request", prompt="Name", default="")
    client.cancel_request_retry(request_id)
    time.sleep(0.35)

    assert attempts == [("/request", request_id)]
    assert client._request_retry_threads == {}


def test_successful_request_cancellation_does_not_retain_id():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())

    client.cancel_request_retry("already-published")

    assert client._request_retry_cancelled == set()


def test_retry_cancellation_is_atomic_with_full_queue_enqueue():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": lambda _message: None,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    client._outbox_limit = 1
    first_started = threading.Event()
    release_first = threading.Event()
    sent = []

    def blocked_post(path, data, timeout=2.0):
        sent.append((path, data.get("type")))
        if data.get("type") == "blocker":
            first_started.set()
            release_first.wait(2.0)
        return {"ok": True}

    client._post = blocked_post
    client._queue_post("/event", {"type": "blocker"})
    assert first_started.wait(1.0)
    client._queue_post("/event", {"type": "queued"})
    client._schedule_request_retry(
        "request-1", {"type": "choice_request", "id": "request-1"})
    time.sleep(0.35)

    client.cancel_request_retry("request-1")
    release_first.set()
    assert client._queue_post(
        "/reset", {"type": "probe"}, timeout=1.0, wait=True,
    ) == {"ok": True}
    deadline = time.time() + 1.0
    while client._request_retry_threads and time.time() < deadline:
        time.sleep(0.01)

    assert ("/request", "choice_request") not in sent
    assert client._request_retry_threads == {}
    assert client._request_retry_cancelled == set()


def test_request_retry_does_not_depend_on_event_wait_return_value():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())

    class Py2StyleEvent(object):
        def __init__(self):
            self.event = threading.Event()

        def wait(self, timeout=None):
            self.event.wait(timeout)
            return None

        def is_set(self):
            return self.event.is_set()

        def set(self):
            self.event.set()

    done = Py2StyleEvent()
    item = {"done": done, "result": [None]}
    client._schedule_request_retry(
        "request-py2",
        {"type": "choice_request", "id": "request-py2"},
        item,
    )
    item["result"][0] = {"ok": True}
    done.set()
    deadline = time.time() + 1.0
    while client._request_retry_threads and time.time() < deadline:
        time.sleep(0.01)

    assert client._request_retry_threads == {}


def test_superseding_request_cancels_old_retry_before_new_publish():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": lambda _message: None,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    attempts = []

    def publish(path, data, **_kwargs):
        attempts.append(data["id"])
        if data["id"] == "old":
            return None
        return {"ok": True}

    client._queue_post = publish
    assert client.push_request(
        "choice_request", req_id="old", choices=["Old"]
    ) == "old"
    client.cancel_request_retry("old")
    assert client.push_request(
        "choice_request", req_id="new", choices=["New"]
    ) == "new"
    time.sleep(0.35)

    assert attempts == ["old", "new"]
    assert client._request_retry_threads == {}


def test_menu_teardown_cancels_current_replacement_request_id():
    source = SHIM.read_text(encoding="utf-8")

    wrapper_start = source.index("    def _make_vnf_menu_wrapper(")
    wrapper_end = source.index("    def _vnf_input_wrapper(", wrapper_start)
    wrapper = source[wrapper_start:wrapper_end]

    assert 'pending["wrapper_req"][0] = _new_rid' in source
    assert "cancel_request_retry(_wrapper_req[0])" in wrapper
    assert 'request_id=_wrapper_req[0]' in wrapper


def test_resolution_expired_behind_request_is_retried_in_order():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": lambda _message: None,
        "_vnf_event_hooks": [],
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace(
        enabled=True, debug=False))
    sent = []

    def slow_request(path, data, timeout=2.0):
        if data.get("type") == "input_request":
            time.sleep(0.6)
        sent.append((path, data.get("type"), data.get("request_id")))
        return {"ok": True}

    client._post = slow_request
    original_queue_post = client._queue_post

    def short_deadline(path, data, timeout=2.0, wait=False, **kwargs):
        return original_queue_post(
            path, data, timeout=0.05, wait=wait, **kwargs)

    client._queue_post = short_deadline
    request_id = client.push_request(
        "input_request", prompt="Name", default="")
    client.push_event_sync({
        "type": "input_resolved",
        "request_id": request_id,
    })
    deadline = time.time() + 2.0
    while client._critical_event_items and time.time() < deadline:
        time.sleep(0.01)

    assert sent == [
        ("/request", "input_request", None),
        ("/event", "input_resolved", request_id),
    ]
    assert client._critical_event_items == {}


@pytest.mark.parametrize(
    "delivery_mode", ["not_accepted", "accepted"])
def test_critical_event_retries_in_place_before_later_story_output(
    delivery_mode,
):
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    restarts = []
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_pending_command_box": [{"name": "set"}],
        "_vnf_log": lambda _message: None,
        "_vnf_event_hooks": [],
        "renpy": types.SimpleNamespace(
            exports=types.SimpleNamespace(
                restart_interaction=lambda: restarts.append(True))),
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace(
        enabled=True, debug=False))
    state = GameState()
    attempts = []

    def publish(path, event, _timeout=2.0):
        attempts.append(dict(event))
        if len(attempts) == 1:
            if delivery_mode != "not_accepted":
                state.push_event(dict(event))
            return None
        sequence = state.push_event(dict(event))
        return {"ok": True, "event_counter": sequence}

    client._post = publish
    client.push_event_sync({
        "type": "command_result",
        "command": "set",
        "nonce": "set-1",
        "success": True,
    })
    client.push_event({"type": "narration", "text": "After set."})

    deadline = time.time() + 3.0
    while ((client._critical_event_items or len(attempts) < 3)
           and time.time() < deadline):
        time.sleep(0.01)

    assert [event["type"] for event in attempts] == [
        "command_result", "command_result", "narration",
    ]
    assert attempts[0]["_source_id"] == attempts[1]["_source_id"]
    assert attempts[0]["_source_seq"] == attempts[1]["_source_seq"]
    assert [event["type"] for event in state.transcript] == [
        "command_result", "narration",
    ]
    assert client._critical_event_items == {}
    assert restarts == [True]


def test_successful_critical_event_does_not_restart_without_pending_command():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    restarts = []
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_pending_command_box": [None],
        "_vnf_log": lambda _message: None,
        "_vnf_event_hooks": [],
        "renpy": types.SimpleNamespace(
            exports=types.SimpleNamespace(
                restart_interaction=lambda: restarts.append(True))),
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace(
        enabled=True, debug=False))
    client._post = lambda *_args, **_kwargs: {"ok": True}

    client.push_event_sync({
        "type": "command_result",
        "command": "set",
        "nonce": "set-1",
        "success": True,
    })

    assert client._critical_event_items == {}
    assert restarts == []


@pytest.mark.parametrize("status", [403, 409])
def test_terminal_critical_event_rejection_retires_and_unblocks(status):
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    restarts = []
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_pending_command_box": [{"name": "set"}],
        "_vnf_log": lambda _message: None,
        "_vnf_event_hooks": [],
        "renpy": types.SimpleNamespace(
            exports=types.SimpleNamespace(
                restart_interaction=lambda: restarts.append(True))),
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace(
        enabled=True, debug=False))
    client._post = lambda *_args, **_kwargs: {
        "_vnf_delivery_rejected": True, "status": status,
    }

    client.push_event_sync({
        "type": "command_result", "command": "set",
        "nonce": "set-rejected", "success": True,
    })

    assert client._critical_event_items == {}
    assert restarts == [True]


def test_post_recovers_unknown_slot_and_retries_once():
    import threading
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )

    class UnknownSlot(Exception):
        code = 403

        def read(self):
            return b'{"error":"Unknown slot identity."}'

    class Response:
        def read(self):
            return b'{"ok":true}'

    calls = []

    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url

    def urlopen(request, timeout=0):
        calls.append(request.url)
        if len(calls) == 1:
            raise UnknownSlot()
        return Response()

    namespace = {
        "_HAS_URLLIB": True,
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "_VNF_NATIVE_DICT_TYPE": dict,
        "_is_legacy": False,
        "_tb_module": types.SimpleNamespace(print_exc=lambda: None),
        "_urllib_request": types.SimpleNamespace(
            Request=Request, urlopen=urlopen),
        "_vnf_log": lambda _message: None,
        "_vnf_text": str,
        "json": json,
        "os": __import__("os"),
        "threading": threading,
        "time": __import__("time"),
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    config = types.SimpleNamespace(
        enabled=True, debug=False, bridge_url="http://old",
        slot_token="old-token",
    )
    client = namespace["VNFBridgeClient"](config)
    client.slot_id = 7
    recoveries = []

    def recover(error, stale_slot):
        recoveries.append((error.code, stale_slot))
        client.slot_id = 9
        return True

    client._recover_stale_slot = recover

    assert client._post("/event", {"type": "narration"}) == {"ok": True}
    assert recoveries == [(403, 7)]
    assert calls == ["http://old/7/event", "http://old/9/event"]

    class Conflict(Exception):
        code = 409

    def reject_conflict(_request, timeout=0):
        raise Conflict()

    namespace["_urllib_request"].urlopen = reject_conflict
    assert client._post("/event", {"type": "narration"}) == {
        "_vnf_delivery_rejected": True,
        "status": 409,
    }


def test_post_recovers_closed_slot_but_retries_transient_http_failures():
    import threading
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )

    class HttpFailure(Exception):
        def __init__(self, code, body=b""):
            self.code = code
            self.body = body

        def read(self):
            return self.body

    failures = [HttpFailure(409, b'{"error":"Slot is closed."}')]

    class Response:
        def read(self):
            return b'{"ok":true}'

    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url

    def urlopen(_request, timeout=0):
        if failures:
            raise failures.pop(0)
        return Response()

    namespace = {
        "_HAS_URLLIB": True,
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "_VNF_NATIVE_DICT_TYPE": dict,
        "_is_legacy": False,
        "_tb_module": types.SimpleNamespace(print_exc=lambda: None),
        "_urllib_request": types.SimpleNamespace(
            Request=Request, urlopen=urlopen),
        "_vnf_log": lambda _message: None,
        "_vnf_text": str,
        "json": json,
        "os": __import__("os"),
        "threading": threading,
        "time": __import__("time"),
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    config = types.SimpleNamespace(
        enabled=True, debug=False, bridge_url="http://bridge",
        slot_token="token", _launch_file={}, save_slot="",
    )
    client = namespace["VNFBridgeClient"](config)
    client.slot_id = 3
    client._recover_stale_slot = lambda error, stale_slot: (
        error.code == 409 and stale_slot == 3)

    assert client._post("/event", {"type": "command_result"}) == {"ok": True}

    namespace["_urllib_request"].urlopen = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(HttpFailure(429)))
    assert client._post("/event", {"type": "command_result"}) is None


def test_critical_event_survives_until_stale_slot_recovery_is_ready():
    import threading
    import time
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )

    class UnknownSlot(Exception):
        code = 403

        def read(self):
            return b'{"error":"Unknown slot identity."}'

    class Response:
        def read(self):
            return b'{"ok":true}'

    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url

    attempts = []

    def urlopen(request, timeout=0):
        attempts.append(request.url)
        if len(attempts) <= 2:
            raise UnknownSlot()
        return Response()

    namespace = {
        "_HAS_URLLIB": True,
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "_VNF_NATIVE_DICT_TYPE": dict,
        "_VNF_NATIVE_LIST_TYPE": list,
        "_is_legacy": False,
        "_tb_module": types.SimpleNamespace(print_exc=lambda: None),
        "_urllib_request": types.SimpleNamespace(
            Request=Request, urlopen=urlopen),
        "_vnf_log": lambda _message: None,
        "_vnf_text": str,
        "_vnf_event_hooks": [],
        "_vnf_pending_command_box": [{"name": "set"}],
        "json": json,
        "os": __import__("os"),
        "renpy": types.SimpleNamespace(
            exports=types.SimpleNamespace(restart_interaction=lambda: None)),
        "threading": threading,
        "time": time,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    config = types.SimpleNamespace(
        enabled=True, debug=False, bridge_url="http://bridge",
        slot_token="token",
    )
    client = namespace["VNFBridgeClient"](config)
    client.slot_id = 3
    recoveries = []

    def recover(error, stale_slot):
        recoveries.append((error.code, stale_slot))
        if len(recoveries) == 1:
            return False
        client.slot_id = 4
        return True

    client._recover_stale_slot = recover
    client.push_event_sync({
        "type": "command_result",
        "command": "set",
        "nonce": "set-delayed-recovery",
        "success": True,
    })

    deadline = time.time() + 2.0
    while client._critical_event_items and time.time() < deadline:
        time.sleep(0.01)

    assert recoveries == [(403, 3), (403, 3)]
    assert attempts == [
        "http://bridge/3/event",
        "http://bridge/3/event",
        "http://bridge/4/event",
    ]
    assert client._critical_event_items == {}


def test_critical_event_acceptance_unknown_is_retained_deterministically():
    import threading
    import time
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": lambda _message: None,
        "_vnf_event_hooks": [],
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace(
        enabled=True, debug=False))
    state = GameState()
    first_started = threading.Event()
    release_first = threading.Event()
    attempts = []

    def publish(_path, event, _timeout=2.0):
        attempts.append(dict(event))
        if len(attempts) == 1:
            state.push_event(dict(event))
            first_started.set()
            release_first.wait(2.0)
            return None
        sequence = state.push_event(dict(event))
        return {"ok": True, "event_counter": sequence}

    client._post = publish
    event = {
        "type": "command_result",
        "command": "advance",
        "nonce": "advance-1",
        "success": True,
    }
    result = []

    def submit():
        result.append(client._queue_post(
            "/event", event, timeout=0.01, wait=True))

    submitter = threading.Thread(target=submit)
    submitter.start()
    assert first_started.wait(1.0)
    submitter.join(1.0)
    assert not submitter.is_alive()
    assert result[0][0] is client.POST_ACCEPTANCE_UNKNOWN

    release_first.set()
    deadline = time.time() + 2.0
    while client._critical_event_items and time.time() < deadline:
        time.sleep(0.01)

    assert len(attempts) == 2
    assert attempts[0]["_source_seq"] == attempts[1]["_source_seq"]
    assert len(state.transcript) == 1
    assert client._critical_event_items == {}


def test_critical_retry_uses_fresh_timeout_after_caller_budget_expires():
    import threading
    import time
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": lambda _message: None,
        "_vnf_event_hooks": [],
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace(
        enabled=True, debug=False))
    critical_timeouts = []

    def publish(_path, event, timeout=2.0):
        if event.get("type") == "narration":
            time.sleep(1.85)
            return {"ok": True}
        critical_timeouts.append(timeout)
        if len(critical_timeouts) == 1:
            return None
        return {"ok": True}

    client._post = publish
    client.push_event({"type": "narration", "text": "Before."})
    client.push_event_sync({
        "type": "command_result",
        "command": "advance",
        "nonce": "advance-1",
        "success": True,
    })

    deadline = time.time() + 2.0
    while client._critical_event_items and time.time() < deadline:
        time.sleep(0.01)

    assert critical_timeouts[0] < 0.5
    assert critical_timeouts[1] == pytest.approx(2.0)
    assert client._critical_event_items == {}


def test_shim_forwards_nonce_to_all_command_results():
    source = SHIM.read_text(encoding="utf-8")

    assert 'and cmd_name in ("set", "get_defaults")' not in source
    assert "def _push_event_with_command_nonce" in source
    assert "def _push_event_sync_with_command_nonce" in source
    assert '_event.get("type") == "command_result"' in source
    assert '_event.get("command") == cmd_name' in source
    assert '_event.setdefault("nonce", _cmd_nonce)' in source
    assert "if _is_nonce_result and callable(_old_push_event_sync):" in source
    assert "return _old_push_event_sync(_event)" in source
    assert "_vnf_client.push_event_sync(_handler_result)" in source
    assert source.count("_vnf_client.push_event_sync(_event)") >= 2
    assert "finally:" in source
    assert "_vnf_client.push_event = _old_push_event" in source
    assert "_vnf_client.push_event_sync = _old_push_event_sync" in source


def test_critical_outbox_state_is_native_and_pauses_command_consumption():
    source = SHIM.read_text(encoding="utf-8")
    module = parse_shim_python()

    assert "self._outbox = _VNF_NATIVE_LIST_TYPE()" in source
    assert "self._critical_event_items = _VNF_NATIVE_DICT_TYPE()" in source
    assert "item = _VNF_NATIVE_DICT_TYPE({" in source
    assert "result = _VNF_NATIVE_LIST_TYPE((None,))" in source
    assert "keep = _VNF_NATIVE_LIST_TYPE()" in source
    assert "dropped = _VNF_NATIVE_LIST_TYPE()" in source
    assert "resume_pending_command = not self._critical_event_items" in source
    poller_screen = source[
        source.index("screen vnf_command_poller():"):
        source.index("screen vnf_player_debug():")
    ]
    assert "not _vnf_client.has_pending_critical_events()" in poller_screen
    for function_name in (
        "_vnf_command_poll_worker",
        "_vnf_periodic_check_pending_command",
        "_vnf_interact_execute_command",
        "_vnf_6x_poll_and_execute",
    ):
        function_source = ast.unparse(function_node(module, function_name))
        assert "has_pending_critical_events" in function_source


def test_pending_command_is_not_executed_across_critical_barrier():
    pending = {"name": "set", "args": {}, "nonce": "set-2"}
    calls = []
    client = types.SimpleNamespace(
        has_pending_critical_events=lambda: True,
    )
    namespace = {
        "vnf_player": types.SimpleNamespace(enabled=True),
        "_vnf_pending_command_box": [pending],
        "_vnf_client": client,
        "_vnf_command_handlers": {
            "set": lambda _name, _args: calls.append(True),
        },
        "_vnf_command_result_cache": {},
        "_vnf_command_causal_boundaries": {},
        "_vnf_log": lambda _message: None,
        "_vnf_remember_command_result": lambda _nonce, _event: None,
        "_CONTROL_EXCEPTIONS": (),
    }
    ns = load_shim_functions(
        "_vnf_execute_pending_command", namespace=namespace)

    ns["_vnf_execute_pending_command"]()

    assert ns["_vnf_pending_command_box"][0] is pending
    assert calls == []


def test_shim_save_load_emit_matching_command_results_without_fake_quick_aliases():
    source = SHIM.read_text(encoding="utf-8")

    assert '_vnf_add_command_handler("quicksave", _vnf_cmd_save)' not in source
    assert '_vnf_add_command_handler("quickload", _vnf_cmd_load)' not in source
    assert 'dict(type="command_result", command=cmd_name, success=True, slot=slot)' in source
    assert 'command=cmd_name,' in source


def test_shim_load_resets_only_after_load_success_signal():
    source = SHIM.read_text(encoding="utf-8")
    start = source.index("    def _vnf_cmd_load(")
    end = source.index("\n    def _vnf_cmd_rollback(", start)
    load_source = source[start:end]

    call_pos = load_source.index("            _load_fn(slot)")
    control_pos = load_source.index("        except BaseException as _load_e:")
    success_pos = load_source.index(
        "            if _vnf_is_load_success_exception(_load_e):")
    failure_pos = load_source.index(
        "            if not isinstance(_load_e, Exception):")
    reset_calls = [
        pos for pos in range(len(load_source))
        if load_source.startswith(
            "            _vnf_reset_after_successful_load()", pos)
    ]

    assert "def _vnf_reset_after_successful_load():" in load_source
    assert len(reset_calls) == 2
    assert (call_pos < control_pos < success_pos < reset_calls[0]
            < failure_pos < reset_calls[1])


def test_native_load_and_rollback_emit_resume_events():
    module = parse_shim_python()
    after_load = function_node(module, "_vnf_after_load_callback")
    observe_rollback = function_node(module, "_vnf_observe_rollback_resume")
    rollback = function_node(module, "_vnf_rollback_resume_interact_callback")
    finish_rollback = function_node(module, "_vnf_finish_rollback_resume")

    after_load_source = ast.unparse(after_load)
    observe_rollback_source = ast.unparse(observe_rollback)
    rollback_source = ast.unparse(rollback)
    finish_source = ast.unparse(finish_rollback)
    assert "game_resumed" in after_load_source and "load" in after_load_source
    assert "_vnf_observe_rollback_resume()" in rollback_source
    assert "_vnf_finish_rollback_resume()" in observe_rollback_source
    assert "game_resumed" in finish_source and "rollback" in finish_source


class _FakeRewindExports:
    def __init__(self, can_rollback_value):
        self._can_rollback_value = can_rollback_value

    def can_rollback(self):
        return self._can_rollback_value


class _FakeRewindGame:
    def __init__(self, context_rollback):
        self._context = types.SimpleNamespace(rollback=context_rollback)

    def context(self):
        return self._context


class _FakeRewindRenpy:
    """Stubs only the can_rollback() signals _vnf_cmd_rewind consults.

    ``rollback_enabled``/``context_rollback`` mirror the two SDK eras
    exercised by real games: 7.5.2's ``can_rollback()`` only checks
    ``config.rollback_enabled`` + the rollback log, while 8.x additionally
    checks ``store._rollback`` and ``context().rollback`` first.
    """

    def __init__(
        self,
        can_rollback_value,
        rollback_enabled=True,
        store_rollback=True,
        context_rollback=True,
    ):
        self.exports = _FakeRewindExports(can_rollback_value)
        self.config = types.SimpleNamespace(rollback_enabled=rollback_enabled)
        self.store = types.SimpleNamespace(_rollback=store_rollback)
        self.game = _FakeRewindGame(context_rollback)


class _FakeRollbackAction:
    """Stand-in for the common00 ``Rollback`` screen action."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _RewindRecordingClient:
    def __init__(self):
        self.events = []

    def push_event(self, event):
        self.events.append(event)


def _load_rewind_namespace(fake_renpy, is_legacy):
    client = _RewindRecordingClient()
    namespace = {
        "_is_legacy": is_legacy,
        "renpy": fake_renpy,
        "_vnf_client": client,
        "_vnf_text": lambda value: str(value),
        "Rollback": _FakeRollbackAction,
        "_vnf_native_action_queue": None,
    }
    ns = load_shim_functions(
        "_vnf_cmd_rewind", "_vnf_rewind_refusal_reason",
        namespace=namespace,
    )
    return ns, client


def test_rewind_queues_rollback_when_checkpoint_available_on_renpy75():
    fake_renpy = _FakeRewindRenpy(can_rollback_value=True, rollback_enabled=True)
    ns, client = _load_rewind_namespace(fake_renpy, is_legacy=True)

    ns["_vnf_cmd_rewind"]("rewind", {})

    assert client.events == [{
        "type": "command_result",
        "command": "rewind",
        "success": True,
        "note": "Rollback action queued.",
    }]
    assert isinstance(ns["_vnf_native_action_queue"], _FakeRollbackAction)


def test_rewind_refusal_names_game_disabled_reason():
    # e.g. Roadwarden's options.rpy sets config.rollback_enabled = False
    # game-wide; can_rollback() is False from the very first interaction,
    # not just after a specific choice.
    fake_renpy = _FakeRewindRenpy(can_rollback_value=False, rollback_enabled=False)
    ns, client = _load_rewind_namespace(fake_renpy, is_legacy=True)

    ns["_vnf_cmd_rewind"]("rewind", {})

    assert len(client.events) == 1
    event = client.events[0]
    assert event["success"] is False
    assert event["reason"] == "rollback_disabled_by_game"
    assert "disabled by this game" in event["error"]
    assert ns["_vnf_native_action_queue"] is None


def test_rewind_refusal_names_context_blocked_reason_on_renpy8():
    # A called screen / nested interaction (renpy.game.context().rollback
    # False) only gates can_rollback() on the 8.x export -- not on 7.x/6.x.
    fake_renpy = _FakeRewindRenpy(
        can_rollback_value=False,
        rollback_enabled=True,
        store_rollback=True,
        context_rollback=False,
    )
    ns, client = _load_rewind_namespace(fake_renpy, is_legacy=False)

    ns["_vnf_cmd_rewind"]("rewind", {})

    assert len(client.events) == 1
    event = client.events[0]
    assert event["success"] is False
    assert event["reason"] == "context_blocks_rollback"
    assert "called screen" in event["error"]


def test_rewind_refusal_names_no_checkpoint_reason_on_legacy():
    # rollback is enabled and no game-side gate applies -- can_rollback()
    # is False only because the rollback log has no checkpoint yet.
    fake_renpy = _FakeRewindRenpy(can_rollback_value=False, rollback_enabled=True)
    ns, client = _load_rewind_namespace(fake_renpy, is_legacy=True)

    ns["_vnf_cmd_rewind"]("rewind", {})

    assert len(client.events) == 1
    event = client.events[0]
    assert event["success"] is False
    assert event["reason"] == "no_checkpoint_yet"


def test_shim_load_preserves_state_on_failure_and_resets_on_success():
    class LoadSucceeded(Exception):
        pass

    class FakeClient:
        def __init__(self):
            self.events = []
            self.reset_count = 0

        def push_event_sync(self, event):
            self.events.append(event)

        def reset_bridge(self):
            self.reset_count += 1

    class FakeAutoskip:
        def __init__(self):
            self.reset_count = 0

        def reset(self):
            self.reset_count += 1

    client = FakeClient()
    autoskip = FakeAutoskip()
    active_clear_count = [0]
    deferred_choice = ["pending-choice"]
    pending_vis_check = ["pending-visibility"]
    load_behavior = [RuntimeError("corrupt save")]

    def fake_load(_slot):
        raise load_behavior[0]

    namespace = load_shim_functions(
        "_vnf_cmd_load",
        namespace={
            "_CONTROL_EXCEPTIONS": (LoadSucceeded,),
            "_vnf_is_load_success_exception": (
                lambda exc: isinstance(exc, LoadSucceeded)),
            "_vnf_autoskip": autoskip,
            "_vnf_clear_active_request": lambda: active_clear_count.__setitem__(
                0, active_clear_count[0] + 1),
            "_vnf_client": client,
            "_vnf_deferred_choice": deferred_choice,
            "_vnf_log": lambda _message: None,
            "_vnf_pending_vis_check": pending_vis_check,
            "renpy": types.SimpleNamespace(
                exports=types.SimpleNamespace(load=fake_load),
                loadsave=types.SimpleNamespace(list_slots=lambda: ["1-1"]),
            ),
        },
    )
    cmd_load = namespace["_vnf_cmd_load"]

    cmd_load("load", {"slot": "1"})

    assert active_clear_count == [0]
    assert autoskip.reset_count == 0
    assert client.reset_count == 0
    assert deferred_choice == ["pending-choice"]
    assert pending_vis_check == ["pending-visibility"]
    assert client.events[-1]["success"] is False
    assert client.events[-1]["error"] == "corrupt save"

    load_behavior[0] = LoadSucceeded()
    with pytest.raises(LoadSucceeded):
        cmd_load("load", {"slot": "1"})

    assert active_clear_count == [1]
    assert autoskip.reset_count == 1
    assert client.reset_count == 1
    assert deferred_choice == [None]
    assert pending_vis_check == [None]
    assert client.events[-1] == {
        "type": "command_result",
        "command": "load",
        "success": True,
        "slot": "1-1",
    }


def test_shim_transform_pipelines_log_failures_and_preserve_control_flow():
    source = SHIM.read_text(encoding="utf-8")

    for fn_name, log_text in (
        ("_vnf_apply_screen_transforms", "Screen transform error in"),
        ("_vnf_apply_action_transforms", "Action transform error in"),
    ):
        start = source.index("    def {}(".format(fn_name))
        end = source.index("\n    def ", start + 8)
        fn_source = source[start:end]
        assert "except _CONTROL_EXCEPTIONS:\n                raise" in fn_source
        assert "if vnf_player.debug:" in fn_source
        assert log_text in fn_source
        assert "_tb_module.format_exc()" in fn_source


def test_shim_profiles_support_text_dialogue_advance_mode():
    source = SHIM.read_text(encoding="utf-8")

    assert 'self.dialogue_advance_mode = "auto"' in source
    assert "self.text_cps = 0" in source
    assert "self.post_reveal_hold = 0.0" in source
    assert "def _vnf_effective_dialogue_advance_mode" in source
    assert "def _vnf_apply_text_cps_preference" in source
    assert 'renpy.game.preferences.text_cps = int(vnf_player.text_cps)' in source
    assert 'if key == "fast_forward":' in source
    assert "_reveal_time + vnf_player.post_reveal_hold" in source
    assert "_read_time + vnf_player.auto_advance_delay" in source


def test_save_slot_redirect_has_logger_before_init_call():
    source = SHIM.read_text(encoding="utf-8")
    log_pos = source.index("def _vnf_log(msg):")
    apply_pos = source.index("def _vnf_apply_save_slot(slot_name):")
    init_call_pos = source.index("_vnf_apply_save_slot(vnf_player.save_slot)")

    assert log_pos < apply_pos < init_call_pos


def test_act_command_handler_declares_button_observation_globals():
    module = parse_shim_python()
    fn = function_node(module, "_vnf_cmd_act")

    assert {
        "_vnf_button_observation_start",
        "_vnf_button_observation_target",
        "_vnf_button_observation_focus_applied",
        "_vnf_native_action_queue",
        "_vnf_pending_click",
    } <= global_names(fn)


def test_button_observation_queues_native_action_and_pending_click():
    module = parse_shim_python()
    fn = function_node(module, "_vnf_execute_button_observation")

    assert {"_vnf_native_action_queue", "_vnf_pending_click"} <= global_names(fn)
    assert {"_vnf_native_action_queue", "_vnf_pending_click"} <= assigned_names(fn)


def test_nvl_scroll_tracks_rebuilt_adjustment_with_same_geometry():
    """A Ren'Py interaction restart must not strand choice observation."""
    class Adjustment:
        def __init__(self, value):
            self.range = 600
            self.page = 300
            self.value = value

        def change(self, value):
            self.value = value

    old_adjustment = Adjustment(120)
    live_adjustment = Adjustment(600)
    scroll = types.SimpleNamespace(
        start_time=10.0,
        adj=old_adjustment,
        content_hash=hash((600, 300)),
        scroll_range=600,
        done_time=0.0,
    )
    now = [12.0]
    ns = load_shim_functions(
        "_vnf_nvl_auto_scroll_tick",
        "_vnf_nvl_scroll_in_progress",
        namespace={
            "vnf_player": types.SimpleNamespace(
                nvl_auto_scroll=True,
                nvl_auto_scroll_delay=0.0,
                nvl_auto_scroll_speed=100.0,
                reading_cps=0.0,
            ),
            "_vnf_scroll": scroll,
            "_vnf_find_nvl_viewport_adj": lambda: live_adjustment,
            "_time": types.SimpleNamespace(time=lambda: now[0]),
            "renpy": types.SimpleNamespace(
                exports=types.SimpleNamespace(
                    restart_interaction=lambda: None,
                ),
            ),
        },
    )

    ns["_vnf_nvl_auto_scroll_tick"]()

    assert scroll.adj is live_adjustment
    assert scroll.done_time == 12.0
    now[0] = 14.1
    assert ns["_vnf_nvl_scroll_in_progress"]() is False


def test_nvl_scroll_rebuilt_before_bottom_clears_prior_settle_time():
    class Adjustment:
        def __init__(self, value):
            self.range = 600
            self.page = 300
            self.value = value

        def change(self, value):
            self.value = value

    old_adjustment = Adjustment(600)
    live_adjustment = Adjustment(120)
    scroll = types.SimpleNamespace(
        start_time=10.0,
        adj=old_adjustment,
        content_hash=hash((600, 300)),
        scroll_range=600,
        done_time=11.0,
    )
    ns = load_shim_functions(
        "_vnf_nvl_auto_scroll_tick",
        namespace={
            "vnf_player": types.SimpleNamespace(
                nvl_auto_scroll=True,
                nvl_auto_scroll_delay=10.0,
                nvl_auto_scroll_speed=100.0,
                reading_cps=0.0,
            ),
            "_vnf_scroll": scroll,
            "_vnf_find_nvl_viewport_adj": lambda: live_adjustment,
            "_time": types.SimpleNamespace(time=lambda: 12.0),
            "renpy": types.SimpleNamespace(
                exports=types.SimpleNamespace(
                    restart_interaction=lambda: None,
                ),
            ),
        },
    )

    ns["_vnf_nvl_auto_scroll_tick"]()

    assert scroll.adj is live_adjustment
    assert scroll.done_time == 0.0


def test_nvl_scroll_bottom_observed_outside_tick_starts_grace_period():
    adjustment = types.SimpleNamespace(range=600, page=300, value=600)
    scroll = types.SimpleNamespace(
        start_time=10.0,
        adj=adjustment,
        content_hash=hash((600, 300)),
        scroll_range=600,
        done_time=0.0,
    )
    now = [12.0]
    ns = load_shim_functions(
        "_vnf_nvl_scroll_in_progress",
        namespace={
            "vnf_player": types.SimpleNamespace(nvl_auto_scroll=True),
            "_vnf_scroll": scroll,
            "_vnf_find_nvl_viewport_adj": lambda: adjustment,
            "_time": types.SimpleNamespace(time=lambda: now[0]),
        },
    )

    assert ns["_vnf_nvl_scroll_in_progress"]() is True
    assert scroll.done_time == 12.0
    now[0] = 14.1
    assert ns["_vnf_nvl_scroll_in_progress"]() is False


def test_nvl_scroll_gate_rebuild_before_bottom_restarts_settle_time():
    old_adjustment = types.SimpleNamespace(range=600, page=300, value=600)
    live_adjustment = types.SimpleNamespace(range=600, page=300, value=120)
    scroll = types.SimpleNamespace(
        start_time=10.0,
        adj=old_adjustment,
        content_hash=hash((600, 300)),
        scroll_range=600,
        done_time=9.0,
    )
    now = [12.0]
    ns = load_shim_functions(
        "_vnf_nvl_scroll_in_progress",
        namespace={
            "vnf_player": types.SimpleNamespace(nvl_auto_scroll=True),
            "_vnf_scroll": scroll,
            "_vnf_find_nvl_viewport_adj": lambda: live_adjustment,
            "_time": types.SimpleNamespace(time=lambda: now[0]),
        },
    )

    assert ns["_vnf_nvl_scroll_in_progress"]() is True
    assert scroll.done_time == 0.0
    live_adjustment.value = 600
    assert ns["_vnf_nvl_scroll_in_progress"]() is True
    assert scroll.done_time == 12.0


def test_choice_observation_has_progress_lease_and_hard_deadline():
    now = [15.0]
    finished = []
    events = []
    request = types.SimpleNamespace(
        obs_start=1.0,
        obs_target=1,
        obs_deadline=20.0,
        obs_last_progress=1.0,
        external_mode=False,
        is_input=False,
        focus_applied=True,
        scroll_correction=True,
        choice_scrolled=False,
        choice_len=1,
    )
    ns = load_shim_functions(
        "_vnf_execute_observation",
        namespace={
            "vnf_player": types.SimpleNamespace(
                enabled=True,
                choice_delay_speed_scroll=0.0,
                choice_delay_offset_scroll=0.0,
                choice_delay_speed_no_scroll=0.0,
                choice_delay_offset_no_scroll=0.0,
            ),
            "_vnf_request": request,
            "_vnf_nvl_scroll_in_progress": lambda: True,
            "_vnf_finish_observation": lambda: finished.append(True),
            "_vnf_highlight_choice": lambda _target: True,
            "_vnf_client": types.SimpleNamespace(push_event=events.append),
            "_vnf_log": lambda _message: None,
            "_time": types.SimpleNamespace(time=lambda: now[0]),
            "time": types.SimpleNamespace(time=lambda: now[0]),
            "renpy": types.SimpleNamespace(),
            "_EndInteraction": type("EndInteraction", (Exception,), {}),
            "_CONTROL_EXCEPTIONS": (),
        },
    )

    ns["_vnf_execute_observation"]()
    assert finished == []
    assert events[-1]["type"] == "observation_progress"
    assert events[-1]["phase"] == "nvl_scroll"

    now[0] = 20.0
    ns["_vnf_execute_observation"]()
    assert finished == [True]


def test_choice_observation_deadline_stays_inside_bridge_idle_budget():
    request = types.SimpleNamespace()
    ns = load_shim_functions(
        "_vnf_begin_observation",
        namespace={
            "vnf_player": types.SimpleNamespace(action_timeout=900.0),
            "_vnf_request": request,
            "_VNFRequestState": types.SimpleNamespace(OBSERVING="observing"),
            "_time": types.SimpleNamespace(time=lambda: 100.0),
        },
    )

    duration = ns["_vnf_begin_observation"](3)

    assert duration == 90.0
    assert request.obs_deadline == 190.0
    assert request.obs_target == 3
    assert request.state == "observing"

    vnf_player = ns["vnf_player"]
    vnf_player.action_timeout = object()
    assert ns["_vnf_begin_observation"](4) == 90.0


def test_act_button_observation_skips_main_menu_context():
    source = SHIM.read_text(encoding="utf-8")
    assert 'not getattr(renpy.store, "main_menu", False)' in source
    assert 'matched_screen not in ("menu", "main_menu")' in source


def test_direct_choice_observation_delay_is_numeric_for_cli_consumers():
    source = SHIM.read_text(encoding="utf-8")
    start = source.index("def _vnf_cmd_act")
    end = source.index("def _vnf_cmd_screenshot", start)
    body = source[start:end]

    assert 'type="observation_started"' in body
    assert "delay=0.0" in body
    assert "delay=None" not in body


def _r66_shim_interactions():
    """The reconstructed R66 surface: a KIT button alongside a story
    choice whose label happens to contain the substring "kit"."""
    return [
        {"id": "1", "source": "choice", "index": 1,
         "display_label": (
             "The signal analysis toolkit - pull the waveform apart "
             "layer by layer.")},
        {"id": "2", "source": "choice", "index": 2,
         "display_label": (
             "The spacetime equations - if this is temporal, there "
             "must be a mechanism.")},
        {"id": "3", "source": "choice", "index": 3,
         "display_label": "ARIA's source logs - whatever this is, it "
                           "came through our hardware."},
        {"id": "hud:KIT", "source": "button", "index": 4,
         "display_label": "KIT"},
    ]


def _load_resolve_act_interaction(interactions, aliases=None):
    return load_shim_functions(
        "_vnf_resolve_act_interaction",
        namespace={
            "_vnf_current_interactions": interactions,
            "_vnf_interaction_aliases": aliases or {},
            "_vnf_normalize_quotes": lambda s: s,
            "_vnf_text": lambda v, default=u"": v,
        },
    )


@pytest.mark.parametrize("target", ["KIT", "LOG", "The"])
def test_act_resolver_short_absent_control_does_not_select_story(target):
    ns = _load_resolve_act_interaction(_r66_shim_interactions()[:3])
    assert ns["_vnf_resolve_act_interaction"]({"label": target}) == (None, None)


def test_act_resolver_exact_button_wins_over_fuzzy_toolkit_choice():
    """R66: act(target="KIT") must resolve to the exact KIT button, never
    the story choice that merely contains "kit" as a substring."""
    ns = _load_resolve_act_interaction(_r66_shim_interactions())

    matched, ambiguous = ns["_vnf_resolve_act_interaction"]({"label": "KIT"})

    assert ambiguous is None
    assert matched is not None
    assert matched["id"] == "hud:KIT"


def test_act_resolver_exact_story_choice_label_still_resolves():
    ns = _load_resolve_act_interaction(_r66_shim_interactions())

    matched, ambiguous = ns["_vnf_resolve_act_interaction"]({
        "label": "ARIA's source logs - whatever this is, it came "
                 "through our hardware.",
    })

    assert ambiguous is None
    assert matched is not None
    assert matched["id"] == "3"


def test_act_resolver_fuzzy_match_refuses_when_ambiguous():
    """Without an exact hit, a fuzzy target must land on exactly one
    interaction or refuse -- never guess among several."""
    interactions = [
        {"id": "map:HABITAT", "source": "button", "index": 1,
         "display_label": "HABITAT"},
        {"id": "map:HABITAT_LOG", "source": "button", "index": 2,
         "display_label": "HABITAT LOG"},
    ]
    ns = _load_resolve_act_interaction(interactions)

    matched, ambiguous = ns["_vnf_resolve_act_interaction"]({"label": "habi"})

    assert matched is None
    assert ambiguous is not None
    assert {itr["id"] for itr in ambiguous} == {
        "map:HABITAT", "map:HABITAT_LOG",
    }


def test_act_resolver_exact_match_ties_across_categories_refuse():
    """A target that exactly matches BOTH a story choice and a button must
    refuse instead of silently preferring one category."""
    interactions = [
        {"id": "1", "source": "choice", "index": 1,
         "display_label": "Close the hatch."},
        {"id": "hud:close", "source": "button", "index": 2,
         "display_label": "Close the hatch."},
    ]
    ns = _load_resolve_act_interaction(interactions)

    matched, ambiguous = ns["_vnf_resolve_act_interaction"]({
        "label": "Close the hatch.",
    })

    assert matched is None
    assert ambiguous is not None
    assert {itr["source"] for itr in ambiguous} == {"choice", "button"}


def test_act_resolver_numeric_index_is_unambiguous():
    ns = _load_resolve_act_interaction(_r66_shim_interactions())

    matched, ambiguous = ns["_vnf_resolve_act_interaction"]({"index": 4})

    assert ambiguous is None
    assert matched is not None
    assert matched["id"] == "hud:KIT"


def test_act_command_pushes_candidate_list_on_ambiguous_target():
    """The command handler surfaces every candidate and never queues a
    click when the resolver comes back ambiguous."""
    events = []
    interactions = [
        {"id": "map:HABITAT", "source": "button", "index": 1,
         "display_label": "HABITAT"},
        {"id": "map:HABITAT_LOG", "source": "button", "index": 2,
         "display_label": "HABITAT LOG"},
    ]
    ns = load_shim_functions(
        "_vnf_cmd_act", "_vnf_resolve_act_interaction", "_vnf_act_candidate_desc",
        namespace={
            "_vnf_current_interactions": interactions,
            "_vnf_interaction_aliases": {},
            "_vnf_normalize_quotes": lambda s: s,
            "_vnf_text": lambda v, default=u"": v,
            "_vnf_client": types.SimpleNamespace(push_event=events.append),
            "_vnf_native_action_queue": None,
            "_vnf_pending_click": None,
            "_vnf_button_observation_start": None,
            "_vnf_button_observation_target": None,
            "_vnf_button_observation_focus_applied": None,
        },
    )

    ns["_vnf_cmd_act"]("act", {"label": "habi"})

    assert len(events) == 1
    result = events[0]
    assert result["success"] is False
    assert "matches more than one thing" in result["error"]
    assert "HABITAT" in result["error"]
    assert "HABITAT LOG" in result["error"]
    # Fail closed: nothing was queued to click.
    assert ns["_vnf_native_action_queue"] is None
    assert ns["_vnf_pending_click"] is None


def test_interaction_builder_uniquifies_duplicate_ids():
    source = SHIM.read_text(encoding="utf-8")
    fn = function_node(parse_shim_python(), "_vnf_build_interactions")

    assert "seen_ids" in assigned_names(fn)
    assert 'iid = "{}#{}".format(iid, seen_ids[iid])' in source


def test_selected_state_survives_real_action_and_interaction_builders():
    ns = load_shim_functions(
        "_vnf_build_action_list", "_vnf_build_interactions", "_vnf_categorize_action",
        namespace={"_vnf_alias_providers": [], "_vnf_pipeline_shared": {}, "_VNF_SCREEN_CATEGORIES": {},
                   "_VNF_NAV_ACTIONS": frozenset(), "_VNF_QUICK_FILE_ACTIONS": frozenset(), "_VNF_FILE_SLOT_SCREENS": frozenset(), "_vnf_normalize_quotes": lambda x: x},
    )
    for selected in (False, True):
        actions, _ = ns["_vnf_build_action_list"]([], [{
            "label": "Mute All", "screen": "menu", "actions": ["Preference"],
            "is_selected": selected,
        }], [])
        interactions = ns["_vnf_build_interactions"](actions, set_global=False)
        assert actions[0]["is_selected"] is selected
        assert interactions[0]["is_selected"] is selected
        assert not interactions[0]["disabled"]
        menus, _ = ns["_vnf_build_action_list"]([], [{
            "label": "About", "screen": "menu", "actions": ["ShowMenu"],
            "is_selected": selected,
        }], [])
        assert menus[0]["disabled"] is selected


def test_stock_menu_return_is_navigation_but_valued_return_stays_choice():
    ns = load_shim_functions("_vnf_categorize_action", namespace={
        "_VNF_SCREEN_CATEGORIES": {}, "_VNF_NAV_ACTIONS": frozenset(), "_VNF_QUICK_FILE_ACTIONS": frozenset(), "_VNF_FILE_SLOT_SCREENS": frozenset(),
    })
    action = {"source": "button", "screen": "menu", "actions": ["Return"],
              "action_strs": ["Return"]}
    assert ns["_vnf_categorize_action"](action) == "nav"
    assert ns["_vnf_categorize_action"](dict(action, action_strs=["Return value=yes"])) == "choice"
    assert ns["_vnf_categorize_action"](dict(action, screen="star_map_screen")) == "choice"


def test_focus_toggle_selection_is_sampled_and_changes_scrape_signature():
    scrape = function_node(parse_shim_python(), "_vnf_scrape_visible_screens")
    sample = next(node for node in ast.walk(scrape) if isinstance(node, ast.Try)
                  and isinstance(node.body[0], ast.Assign)
                  and "_focus_selected" in assigned_names(node.body[0]))
    signature = next(node for node in ast.walk(scrape) if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "btn_sigs" for t in node.targets))
    code = compile(ast.fix_missing_locations(ast.Module(body=[sample, signature], type_ignores=[])),
                   "<focus-selection>", "exec")
    results = []
    for selected in (False, True):
        action = object()
        ns = {"_act": action, "renpy": types.SimpleNamespace(display=types.SimpleNamespace(
            behavior=types.SimpleNamespace(is_selected=lambda a: selected if a is action else None))),
              "all_data": {"buttons": [{"label": "Mute All", "actions": ["Preference"],
                                         "is_selected": selected}]}}
        exec(code, ns)
        assert ns["_focus_selected"] is selected
        results.append(ns["btn_sigs"])
    assert results[0] != results[1]
    assert '"is_selected": _focus_selected' in SHIM.read_text(encoding="utf-8")


def test_interaction_aliases_are_first_write_wins():
    source = SHIM.read_text(encoding="utf-8")

    assert "alias_map.setdefault(dl, iid)" in source


def test_wait_after_action_hint_flows_to_button_act_result():
    source = SHIM.read_text(encoding="utf-8")
    function_node(parse_shim_python(), "_vnf_build_action_list")
    function_node(parse_shim_python(), "_vnf_build_interactions")
    function_node(parse_shim_python(), "_vnf_execute_pending_command")

    assert '"_wait_after_action"' in source
    assert 'interaction["wait_after_action"] = bool(a["_wait_after_action"])' in source
    assert 'wait_after_action=bool(_itr_matched.get("wait_after_action"))' in source


def test_story_navigation_commands_are_distinct_from_overlay_back():
    source = SHIM.read_text(encoding="utf-8")
    module = parse_shim_python()
    function_node(module, "_vnf_execute_pending_command")
    function_node(module, "_vnf_story_advance_block_reason")
    function_node(module, "_vnf_cmd_advance")
    function_node(module, "_vnf_cmd_rewind")
    function_node(module, "_vnf_cmd_replay")
    function_node(module, "_vnf_cmd_back")

    for alias, handler in (
        ("advance", "advance"), ("step", "advance"), ("next", "advance"),
        ("rewind", "rewind"), ("backward", "rewind"), ("story_back", "rewind"),
        ("replay", "replay"), ("forward", "replay"), ("story_forward", "replay"),
        ("back", "back"),
    ):
        assert (
            '_vnf_add_command_handler("{}", _vnf_cmd_{}, causal_boundary=True)'.format(
                alias, handler)
            in source
        )
    assert "_block_reason = _vnf_story_advance_block_reason()" in source
    assert '"A choice request is active; use act()."' in source
    assert '"An input request is active; use input_text()."' in source
    assert "_vnf_native_action_queue = Return(True)" in source
    assert "_vnf_native_action_queue = Rollback()" in source
    assert "_vnf_native_action_queue = RollForward()" in source


def test_manual_advance_refuses_during_pre_request_menu_window():
    ns = load_shim_functions(
        "_vnf_story_advance_block_reason",
        namespace={
            "vnf_player": types.SimpleNamespace(enabled=True),
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(main_menu=False),
                exports=types.SimpleNamespace(get_screen=lambda _name: None),
            ),
            "_vnf_has_modal_overlay": lambda: False,
            "_vnf_request": types.SimpleNamespace(
                request_id=None, is_input=False),
            "_vnf_current_menu_context": [object()],
        },
    )

    assert ns["_vnf_story_advance_block_reason"]() == (
        "A choice/menu interaction is active; use act().")


def test_unavailable_roll_forward_is_not_exposed_as_an_action():
    renpy = types.SimpleNamespace(exports=types.SimpleNamespace(
        roll_forward_info=lambda: None))
    ns = load_shim_functions(
        "_vnf_action_transform_unavailable_roll_forward",
        namespace={"renpy": renpy},
    )
    actions = [
        {"label": "Forward", "actions": ["RollForward"]},
        {"label": "Preferences", "actions": ["ShowMenu"]},
    ]

    assert ns["_vnf_action_transform_unavailable_roll_forward"](
        actions, {}) == [actions[1]]

    renpy.exports.roll_forward_info = lambda: ("checkpoint",)
    assert ns["_vnf_action_transform_unavailable_roll_forward"](
        actions, {}) == actions


def test_auto_advance_on_uses_enable_helper_once():
    source = SHIM.read_text(encoding="utf-8")
    function_node(parse_shim_python(), "_vnf_cmd_auto_advance_on")

    assert (
        source.count(
            '_vnf_add_command_handler("auto_advance_on", '
            '_vnf_cmd_auto_advance_on, causal_boundary=True)')
        == 1
    )
    assert "_vnf_enable_auto_advance()" in source


def test_command_results_declare_gameplay_causal_boundaries():
    source = SHIM.read_text(encoding="utf-8")
    module = parse_shim_python()
    function_node(module, "_vnf_add_command_handler")
    function_node(module, "_vnf_execute_pending_command")

    assert "_vnf_command_causal_boundaries = {}" in source
    assert '"causal_boundary"' in source
    assert "_vnf_command_causal_boundaries.get(" in source
    assert "cmd_name, False)" in source
    for command in (
        "auto_advance_on", "fast_forward_on", "inventory_modify",
        "stats_modify",
    ):
        assert (
            '_vnf_add_command_handler("{}",'.format(command) in source
            and '_vnf_cmd_{}, causal_boundary=True)'.format(command) in source
        )
    for command in (
        "save", "screenshot", "inspect", "get_stats",
        "auto_advance_off", "fast_forward_off", "skip_toggle",
    ):
        assert (
            '_vnf_add_command_handler("{}", _vnf_cmd_{})'.format(
                command, command)
            in source
        )
    function_node(module, "_vnf_cmd_skip_toggle")
    skip_source = source[
        source.index("    def _vnf_cmd_skip_toggle"):
        source.index("    def _vnf_cmd_auto_advance_on")
    ]
    assert "causal_boundary=renpy.config.skipping is not None" in skip_source


def test_command_failure_with_unprintable_exception_still_emits_result():
    class UnprintableError(Exception):
        def __str__(self):
            raise UnicodeEncodeError("ascii", "\u0141", 0, 1, "non-ascii")

    class RecordingClient:
        def __init__(self):
            self.events = []
            self.methods = []

        def push_event(self, event):
            self.methods.append("async")
            self.events.append(dict(event))

        def push_event_sync(self, event):
            self.methods.append("sync")
            self.events.append(dict(event))

        def has_pending_critical_events(self):
            return False

    def fail(_name, _args):
        raise UnprintableError()

    client = RecordingClient()
    namespace = {
        "vnf_player": types.SimpleNamespace(enabled=True),
        "_vnf_pending_command_box": [{
            "name": "explode", "args": {}, "nonce": "nonce-1",
        }],
        "_vnf_command_handlers": {"explode": fail},
        "_vnf_command_result_cache": {},
        "_vnf_command_causal_boundaries": {},
        "_vnf_client": client,
        "_vnf_log": lambda _message: None,
        "_vnf_remember_command_result": lambda _nonce, _event: None,
        "_CONTROL_EXCEPTIONS": (),
    }
    ns = load_shim_functions(
        "_vnf_execute_pending_command", namespace=namespace)

    ns["_vnf_execute_pending_command"]()

    assert client.events == [{
        "type": "command_result",
        "command": "explode",
        "success": False,
        "error": "unknown command failure",
        "nonce": "nonce-1",
    }]
    assert client.methods == ["sync"]


def test_nonce_command_handler_async_result_is_promoted_to_sync_delivery():
    class RecordingClient:
        def __init__(self):
            self.events = []
            self.methods = []

        def push_event(self, event):
            self.methods.append("async")
            self.events.append(dict(event))

        def push_event_sync(self, event):
            self.methods.append("sync")
            self.events.append(dict(event))

        def has_pending_critical_events(self):
            return False

    client = RecordingClient()

    def succeed(name, _args):
        client.push_event({
            "type": "command_result",
            "command": name,
            "success": True,
        })

    namespace = {
        "vnf_player": types.SimpleNamespace(enabled=True),
        "_vnf_pending_command_box": [{
            "name": "set", "args": {}, "nonce": "nonce-1",
        }],
        "_vnf_command_handlers": {"set": succeed},
        "_vnf_command_result_cache": {},
        "_vnf_command_causal_boundaries": {},
        "_vnf_client": client,
        "_vnf_log": lambda _message: None,
        "_vnf_remember_command_result": lambda _nonce, _event: None,
        "_CONTROL_EXCEPTIONS": (),
    }
    ns = load_shim_functions(
        "_vnf_execute_pending_command", namespace=namespace)

    ns["_vnf_execute_pending_command"]()

    assert client.methods == ["sync"]
    assert client.events == [{
        "type": "command_result",
        "command": "set",
        "success": True,
        "causal_boundary": False,
        "nonce": "nonce-1",
    }]


def test_cached_command_replay_does_not_duplicate_bridge_receipt():
    import threading
    import time
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    client_namespace = {
        "os": __import__("os"),
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "_vnf_log": lambda _message: None,
        "_vnf_event_hooks": [],
    }
    exec_shim_nodes([client_node], client_namespace)
    client = client_namespace["VNFBridgeClient"](types.SimpleNamespace(
        enabled=True, debug=False))
    state = GameState()

    def publish(_path, event, _timeout=2.0):
        sequence = state.push_event(dict(event))
        return {"ok": True, "event_counter": sequence}

    client._post = publish
    handler_calls = []

    def succeed(name, _args):
        handler_calls.append(name)
        client.push_event({
            "type": "command_result",
            "command": name,
            "success": True,
        })

    cache = {}

    def remember(nonce, event):
        cache[nonce] = dict(event)

    command = {"name": "set", "args": {}, "nonce": "nonce-1"}
    namespace = {
        "vnf_player": types.SimpleNamespace(enabled=True),
        "_vnf_pending_command_box": [dict(command)],
        "_vnf_command_handlers": {"set": succeed},
        "_vnf_command_result_cache": cache,
        "_vnf_command_causal_boundaries": {},
        "_vnf_client": client,
        "_vnf_log": lambda _message: None,
        "_vnf_remember_command_result": remember,
        "_CONTROL_EXCEPTIONS": (),
    }
    ns = load_shim_functions(
        "_vnf_execute_pending_command", namespace=namespace)

    ns["_vnf_execute_pending_command"]()
    ns["_vnf_pending_command_box"][0] = dict(command)
    ns["_vnf_execute_pending_command"]()

    assert handler_calls == ["set"]
    assert len(state.transcript) == 1
    assert state.transcript[0]["type"] == "command_result"
    assert state.transcript[0]["nonce"] == "nonce-1"


def test_full_restart_diag_uses_inspect_compat_probe():
    source = SHIM.read_text(encoding="utf-8")
    function_node(parse_shim_python(), "_vnf_diag_full_restart")

    assert 'getattr(_insp, "getfullargspec", None)' in source
    assert 'getattr(_insp, "getargspec", None)' in source
    assert "_insp.getargspec(" not in source


def test_game_state_choices_follow_visible_transformed_choice_actions():
    source = SHIM.read_text(encoding="utf-8")
    fn = function_node(parse_shim_python(), "_vnf_push_game_state")

    assert "_visible_choice_actions" in assigned_names(fn)
    assert "_had_choice_context" in assigned_names(fn)
    assert (
        'if _ea.get("source") == "choice" and not _ea.get("hidden")'
        in source
    )
    assert '"choice_value_index"' in source
    assert "_choice_dicts = [" in source
    assert "if _choice_dicts or _had_choice_context:" in source


def test_modal_game_menu_masks_underlying_story_choices():
    source = SHIM.read_text(encoding="utf-8")
    function_node(parse_shim_python(), "_vnf_push_game_state")

    assert "_story_choices_masked = bool(overlay_active or modal_screens)" in source
    assert "not _story_choices_masked" in source
    assert '_tag == "menu"' in source
    assert 'scr_data.get("modal") or _implicit_game_menu' in source
    assert "_vnf_is_generic_game_menu_showing()" in source
    assert "_generic_game_menu_active" in source
    assert '"menu" if _generic_game_menu_active' in source

    scrape_start = source.index("def _vnf_scrape_visible_screens")
    scrape_body = source[scrape_start:]
    assert (
        "_generic_game_menu_active = bool(\n"
        "            _vnf_is_generic_game_menu_showing())"
        in scrape_body
    )
    assert '"menu" in _showing_tags and not _at_mm' not in scrape_body


def test_generic_game_menu_blocks_advance_and_reports_menu_context():
    source = SHIM.read_text(encoding="utf-8")

    periodic = source[
        source.index("    def _vnf_periodic_auto_advance"):
        source.index("    # Do NOT enable auto-advance", source.index(
            "    def _vnf_periodic_auto_advance"))
    ]
    block_reason = source[
        source.index("    def _vnf_story_advance_block_reason"):
        source.index("    def _vnf_detect_context")
    ]
    context = source[
        source.index("    def _vnf_detect_context"):
        source.index("    # -------------------------------------------------------------------------",
                     source.index("    def _vnf_detect_context"))
    ]

    assert "if _vnf_has_modal_overlay():" in periodic
    assert "if _vnf_has_modal_overlay():" in block_reason
    assert "generic_menu = _vnf_is_generic_game_menu_showing()" in context
    assert 'current_screen = "menu"' in context


def test_generic_game_menu_focus_fallback_ignores_underlying_buttons():
    helper_names = (
        "_vnf_needs_focus_button_fallback",
        "_vnf_focus_fallback_existing_labels",
        "_vnf_is_generic_menu_focus_screen",
        "_vnf_action_transform_modal_filter",
    )
    namespace = load_shim_functions(*helper_names)

    underlying = {"label": "HUD", "screen": "quick_menu"}
    assert namespace["_vnf_needs_focus_button_fallback"](
        [underlying], True
    )
    assert namespace["_vnf_focus_fallback_existing_labels"](
        [underlying], True
    ) == set()

    focused_menu = {"label": "Preferences", "screen": "menu"}
    assert not namespace["_vnf_needs_focus_button_fallback"](
        [underlying, focused_menu], False
    )
    assert namespace["_vnf_needs_focus_button_fallback"](
        [underlying, focused_menu], True
    )
    focus_owned = namespace["_vnf_is_generic_menu_focus_screen"]
    assert focus_owned("menu")
    assert focus_owned("preferences")
    assert not focus_owned("quick_menu")
    assert not focus_owned(None)
    filtered = namespace["_vnf_action_transform_modal_filter"](
        [
            {"source": "button", "screen": "quick_menu", "label": "HUD"},
            {
                "source": "button",
                "screen": "menu",
                "label": "Preferences",
            },
        ],
        {"has_modal": True, "modal_screens": ["menu"]},
    )
    assert [item["label"] for item in filtered] == ["Preferences"]


def test_game_state_carries_source_stat_sample_time():
    source = SHIM.read_text(encoding="utf-8")
    function_node(parse_shim_python(), "_vnf_push_game_state")

    assert '_gs["_stats_ts"] = _stats_ts' in source
    assert "_scrape_inv, _scrape_stats, _time.time()" in source


def test_screen_content_carries_overlay_rows_by_contributor():
    source = SHIM.read_text(encoding="utf-8")

    assert '_ov_texts_by_screen[_ov_tag] = list(_osd_texts)' in source
    assert 'ev["overlay_texts_by_screen"] = _ov_texts_by_screen' in source
    assert source.index("_ov_screens.append(_ov_tag)") < source.index(
        "if _osd_texts:")
    assert "if _ov_screens:" in source
    assert '"_screen_instance": _screen_instance' in source
    assert "_vnf_overlay_instance_generation(tag, scr)" in source
    assert "_vnf_overlay_instance_serial += 1" in source
    assert "def _vnf_reset_overlay_instance_generations():" in source
    assert source.count("_vnf_reset_overlay_instance_generations()") >= 4
    assert '"instance:" + _vnf_text(' in source


def test_screen_scrape_publishes_registry_wide_retained_overlay_tags():
    source = SHIM.read_text(encoding="utf-8")

    assignment = "_ov_retained = sorted(_vnf_retained_overlay_screens)"
    publication = 'ev["overlay_retained_screens"] = _ov_retained'
    assert assignment in source
    assert publication in source
    assert source.index(assignment) < source.index("for _osd in per_screen:")
    assert source.index(publication) > source.index("if _ov_screens:")


def test_menu_action_transforms_rebuild_visible_choice_value_map():
    source = SHIM.read_text(encoding="utf-8")
    function_node(parse_shim_python(), "_make_vnf_menu_wrapper")

    assert "_visible_choices = []" in source
    assert "_visible_value_map = {}" in source
    assert "_visible_choice_labels = []" in source
    assert "_vm_idx = len(_visible_value_map) + 1" in source
    assert 'a["choice_value_index"] = _vm_idx' in source
    assert "_visible_value_map[_vm_idx] = value_map.get(_old_vm_idx)" in source
    assert "choices = _visible_choices" in source
    assert "value_map = _visible_value_map" in source
    assert "choice_labels = _visible_choice_labels" in source
    assert "_menu_interactions = _vnf_build_interactions(_at_actions)" in source
    assert "_remapped_aug_pre_sets[_vm_idx]" in source
    assert "_remapped_aug_pre_resolve[_vm_idx]" in source


def test_resync_restores_local_choice_state_when_bridge_pending_matches():
    source = SHIM.read_text(encoding="utf-8")

    assert 'message="Already in sync"' in source
    assert 'message="Restored local choice state"' in source
    assert '_event["nonce"] = _nonce' in source
    assert '_vnf_request.request_id == ctx["req_id"]' in source
    assert "_vnf_request.value_map" in source
    assert 'ctx["req_id"], ctx["value_map"], choices=ctx["choices"]' in source
    assert '_resync_kwargs["reissued_from_request_id"] = ctx["req_id"]' in source
    assert '_resync_kwargs["reissue_root_request_id"] = (' in source
    assert 'ctx["reissue_root_request_id"] = _resync_kwargs[' in source
    assert "alias_map.setdefault(" in source
    assert "raw_button_refs" not in source


def test_resync_cancels_deferred_activation_before_restoring_choice_state():
    class FakeClient:
        def __init__(self):
            self.events = []

        def _get(self, _path):
            return {"pending": {"id": "request-1"}}

        def push_event(self, event):
            self.events.append(event)

    client = FakeClient()
    deferred = [(123.0, lambda: None)]
    activated = []
    context = [{
        "req_id": "request-1",
        "value_map": {1: "choice-value"},
        "choices": [{"index": 1, "label": "Continue"}],
        "req_kwargs": {},
    }]
    namespace = load_shim_functions(
        "_vnf_cmd_resync",
        namespace={
            "_vnf_current_menu_context": context,
            "_vnf_deferred_choice": deferred,
            "_vnf_pending_vis_check": [None],
            "_vnf_periodic_vis_check": lambda: None,
            "_vnf_client": client,
            "_vnf_request": types.SimpleNamespace(
                request_id=None, value_map=None),
            "_vnf_set_active_choice_request": lambda *args, **kwargs: (
                activated.append((args, kwargs))),
            "vnf_player": types.SimpleNamespace(allow_user_override=True),
            "_vnf_log": lambda _message: None,
        },
    )

    namespace["_vnf_cmd_resync"]("resync", {"nonce": "nonce-1"})

    assert deferred == [None]
    assert activated == [((
        "request-1",
        {1: "choice-value"},
    ), {
        "choices": [{"index": 1, "label": "Continue"}],
        "external_mode": False,
    })]
    assert client.events == [{
        "type": "command_result",
        "command": "resync",
        "success": True,
        "message": "Restored local choice state",
        "nonce": "nonce-1",
    }]


def test_resync_settles_visibility_filter_before_activating_choice():
    class FakeClient:
        def __init__(self):
            self.events = []

        def _get(self, _path):
            return {"pending": {"id": "request-2"}}

        def push_event(self, event):
            self.events.append(event)

    client = FakeClient()
    deferred = [(123.0, lambda: None)]
    pending_vis = [{"req_id": "request-1"}]
    context = [{
        "req_id": "request-1",
        "value_map": {1: "old-value"},
        "choices": [{"index": 1, "label": "Old"}],
        "req_kwargs": {},
    }]

    def settle_visibility():
        pending_vis[0] = None
        context[0] = {
            "req_id": "request-2",
            "value_map": {1: "visible-value"},
            "choices": [{"index": 1, "label": "Visible"}],
            "req_kwargs": {},
        }

    activated = []
    namespace = load_shim_functions(
        "_vnf_cmd_resync",
        namespace={
            "_vnf_current_menu_context": context,
            "_vnf_deferred_choice": deferred,
            "_vnf_pending_vis_check": pending_vis,
            "_vnf_periodic_vis_check": settle_visibility,
            "_vnf_client": client,
            "_vnf_request": types.SimpleNamespace(
                request_id=None, value_map=None),
            "_vnf_set_active_choice_request": lambda *args, **kwargs: (
                activated.append((args, kwargs))),
            "vnf_player": types.SimpleNamespace(allow_user_override=True),
            "_vnf_log": lambda _message: None,
        },
    )

    namespace["_vnf_cmd_resync"]("resync", {"nonce": "nonce-2"})

    assert pending_vis == [None]
    assert deferred == [None]
    assert activated[0][0] == ("request-2", {1: "visible-value"})
    assert activated[0][1]["choices"] == [
        {"index": 1, "label": "Visible"},
    ]


def test_visibility_filter_rebases_saved_menu_context_with_replacement():
    source = SHIM.read_text(encoding="utf-8")
    start = source.index("    def _vnf_periodic_vis_check():")
    end = source.index(
        "    renpy.config.periodic_callbacks.append(", start)
    vis_check = source[start:end]

    assert (
        'and _menu_ctx.get("req_id") == _old_rid' in vis_check
    )
    for assignment in (
        '_menu_ctx["req_id"] = _new_rid',
        '_menu_ctx["req_kwargs"] = dict(_new_kw)',
        '_menu_ctx["value_map"] = dict(new_value_map)',
        '_menu_ctx["choices"] = list(new_choices)',
    ):
        assert assignment in vis_check


def test_menu_context_clears_after_menu_returns():
    source = SHIM.read_text(encoding="utf-8")
    wrapper_factory = function_node(parse_shim_python(), "_make_vnf_menu_wrapper")
    wrapper_source = ast.get_source_segment(source, wrapper_factory) or ""

    cleanup = (
        "_vnf_deferred_choice[0] = None\n"
        "                _vnf_pending_vis_check[0] = None\n"
        "                _vnf_current_menu_context[0] = None\n"
        "                _vnf_clear_active_request()"
    )
    assert cleanup in wrapper_source


def test_menu_wrapper_notifies_bridge_when_choice_resolves():
    source = SHIM.read_text(encoding="utf-8")

    assert 'type="choice_resolved"' in source
    assert "request_id=_wrapper_req[0]" in source
    assert "resolved_by=resolved_by" in source
    assert "_vnf_client.push_event_sync" in source


def test_character_callback_resolves_dynamic_display_name_expressions():
    source = SHIM.read_text(encoding="utf-8")
    module = parse_shim_python()

    function_node(module, "_vnf_resolve_display_name_expr")
    display_name_state = function_node(
        module, "_vnf_character_display_name_state")
    display_name_source = ast.unparse(display_name_state)

    assert "renpy.python.py_eval(raw)" in source
    assert "raw.endswith(\")\")" in source
    assert "_vnf_resolve_display_name_expr(char)" in display_name_source
    assert "_vnf_resolve_display_name_expr(name)" in display_name_source
    assert "_vnf_resolve_display_name_expr(fallback)" in display_name_source


def test_focus_list_fallback_uses_clicked_actions():
    source = SHIM.read_text(encoding="utf-8")

    assert "if _vnf_needs_focus_button_fallback(" in source
    assert 'b.get("screen") == "menu"' in source
    assert 'getattr(_w, "clicked", None)' in source
    assert 'getattr(f.widget, "clicked", None)' in source
    assert '"menu" if _generic_game_menu_active' in source
    assert 'else "_focus_list"' in source


def test_input_prompt_transform_runs_before_pending_request():
    source = SHIM.read_text(encoding="utf-8")
    module = parse_shim_python()

    assert "self.input_prompt_transform = None" in source
    assert "def _vnf_transform_input_prompt" in source

    input_wrapper = function_node(module, "_vnf_input_wrapper")
    wrapper_source = ast.get_source_segment(source, input_wrapper)
    assert wrapper_source is None or "_vnf_transform_input_prompt" in wrapper_source

    assert "clean_prompt = _vnf_transform_input_prompt(clean_prompt, default, screen)" in source
    assert "_si_prompt = _vnf_transform_input_prompt(_si_prompt)" in source


def test_input_prompt_transform_logs_unprintable_exception_safely():
    class UnprintableError(Exception):
        def __str__(self):
            raise UnicodeEncodeError("ascii", "\u0141", 0, 1, "non-ascii")

    def fail(_prompt):
        raise UnprintableError()

    logs = []
    ns = load_shim_functions(
        "_vnf_transform_input_prompt",
        namespace={
            "vnf_player": types.SimpleNamespace(
                input_prompt_transform=fail),
            "_vnf_log": logs.append,
        },
    )

    assert ns["_vnf_transform_input_prompt"]("Prompt") == "Prompt"
    assert logs == [
        "input_prompt_transform error: unknown prompt transform failure"
    ]


def test_auto_advancing_state_uses_actual_autoskip_gates():
    source = SHIM.read_text(encoding="utf-8")
    module = parse_shim_python()

    function_node(module, "_vnf_is_autoskip_loop")
    function_node(module, "_vnf_auto_skip_predicate_allows")
    assert "self.last_text = None" in source
    assert "_vnf_autoskip.last_text = _vnf_current_autoskip_text()" in source
    assert "_vnf_autoskip.resolve_value is not None," in source
    assert "and _vnf_autoskip.resolve_value is not None" in source
    assert "and not _vnf_is_autoskip_loop(_enabled[0].get(\"label\", \"\"))" in source
    assert "and _vnf_auto_skip_predicate_allows(_enabled[0].get(\"label\", \"\"))" in source


def test_context_end_detection_tracks_gameplay_seen():
    source = SHIM.read_text(encoding="utf-8")
    fn = function_node(parse_shim_python(), "_vnf_detect_context")

    assert "_vnf_gameplay_seen" in source
    assert "_vnf_gameplay_seen" in global_names(fn)
    assert 'if context == "in_game":' in source
    assert 'context == "main_menu" and prev_context != "main_menu" and _vnf_gameplay_seen' in source


# ---------------------------------------------------------------------------
# Launch-file handshake (executable slice)
#
# steam:// launches don't inherit the vnflight launcher's env, so the shim
# reads game/vnflight_launch.json as an env-overriding source.  These tests
# exec the reader + VNFPlayerConfig slice out of the shim and drive it with
# real files.
# ---------------------------------------------------------------------------


def _launch_file_namespace(gamedir):
    """Exec the launch-file reader + VNFPlayerConfig slice from the shim."""
    import json as json_mod
    import os as os_mod
    import time as time_mod
    import traceback
    import types

    source = SHIM.read_text(encoding="utf-8")
    # Intentionally brittle anchors: if the config block moves, fail loudly
    # instead of silently testing nothing.
    start = source.index("    _VNF_LAUNCH_FILE_NAME")
    end = source.index("    # Inventory/Stats Capture", start)
    sliced = textwrap.dedent(source[start:end])

    # Mirror Ren'Py store semantics: the names `dict` and `list` are
    # rebound to Revertable subclasses in shim code, while json.loads
    # returns PLAIN containers — so isinstance(json.loads(...), dict)
    # is always False there.  Caught live on Slay the Princess
    # (Ren'Py 8.0.3): the launch file was ignored as "not a JSON object".
    class _RevertableDict(dict):
        pass

    class _RevertableList(list):
        pass

    namespace = {
        "os": os_mod,
        "json": json_mod,
        "time": time_mod,
        "_tb_module": traceback,
        "dict": _RevertableDict,
        "list": _RevertableList,
        "renpy": types.SimpleNamespace(
            config=types.SimpleNamespace(gamedir=str(gamedir))
        ),
    }
    helpers = load_shim_functions()
    namespace["_vnf_stringify"] = helpers["_vnf_stringify"]
    namespace["_vnf_text"] = helpers["_vnf_text"]
    exec(compile(sliced, str(SHIM), "exec"), namespace)
    return namespace, sliced


def _write_launch_json(gamedir, payload):
    import json as json_mod

    path = gamedir / "vnflight_launch.json"
    path.write_text(json_mod.dumps(payload), encoding="utf-8")
    return path


def _set_launch_env(monkeypatch, bridge, token, save):
    monkeypatch.setenv("VNFLIGHT_ENABLED", "1")
    monkeypatch.setenv("VNFLIGHT_BRIDGE_URL", bridge)
    monkeypatch.setenv("VNFLIGHT_SLOT_TOKEN", token)
    monkeypatch.setenv("VNFLIGHT_SAVE_SLOT", save)


def test_shim_launch_file_fresh_wins_over_env(tmp_path, monkeypatch):
    """A fresh launch file is this-launch intent: warm Steam can carry
    stale env, so the file overrides all three values."""
    import time as time_mod

    _set_launch_env(monkeypatch, "http://127.0.0.1:7777", "env-token", "env-slot")
    _write_launch_json(tmp_path, {
        "bridge_url": "http://127.0.0.1:9611",
        "slot_token": "file-token",
        "save_slot": "file-slot",
        "written_at": time_mod.time(),
    })

    namespace, _ = _launch_file_namespace(tmp_path)
    cfg = namespace["VNFPlayerConfig"]()

    assert cfg.bridge_url == "http://127.0.0.1:9611"
    assert cfg.slot_token == "file-token"
    assert cfg.save_slot == "file-slot"


def test_shim_pointer_setting_uses_env_and_fresh_launch_file(
    tmp_path, monkeypatch
):
    import time as time_mod

    monkeypatch.setenv("VNFLIGHT_MOVE_HOST_POINTER", "0")
    namespace, _ = _launch_file_namespace(tmp_path)
    assert namespace["VNFPlayerConfig"]().move_host_pointer is False

    _write_launch_json(tmp_path, {
        "bridge_url": "http://127.0.0.1:9611",
        "move_host_pointer": True,
        "written_at": time_mod.time(),
    })
    namespace, _ = _launch_file_namespace(tmp_path)
    assert namespace["VNFPlayerConfig"]().move_host_pointer is True


def test_shim_launch_file_fresh_without_token_clears_stale_env_token(
    tmp_path, monkeypatch
):
    """A fresh file WITHOUT slot_token/save_slot means THIS launch is
    tokenless — leftover env credentials from an earlier launch must not
    leak in."""
    import time as time_mod

    _set_launch_env(monkeypatch, "http://127.0.0.1:7777", "env-token", "env-slot")
    _write_launch_json(tmp_path, {
        "bridge_url": "http://127.0.0.1:8385",
        "written_at": time_mod.time(),
    })

    namespace, _ = _launch_file_namespace(tmp_path)
    cfg = namespace["VNFPlayerConfig"]()

    assert cfg.bridge_url == "http://127.0.0.1:8385"
    assert cfg.slot_token is None
    assert cfg.save_slot == ""


def test_shim_launch_file_stale_falls_back_to_env(tmp_path, monkeypatch):
    import time as time_mod

    _set_launch_env(monkeypatch, "http://127.0.0.1:7777", "env-token", "env-slot")
    _write_launch_json(tmp_path, {
        "bridge_url": "http://127.0.0.1:9611",
        "slot_token": "file-token",
        "save_slot": "file-slot",
        "written_at": time_mod.time() - 3600,  # > default 900s window
    })

    namespace, _ = _launch_file_namespace(tmp_path)
    cfg = namespace["VNFPlayerConfig"]()

    assert cfg.bridge_url == "http://127.0.0.1:7777"
    assert cfg.slot_token == "env-token"
    assert cfg.save_slot == "env-slot"
    assert namespace["_vnf_launch_file_note"]  # one deferred log line


def test_shim_launch_file_honors_embedded_ttl(tmp_path, monkeypatch):
    import time as time_mod

    _set_launch_env(monkeypatch, "http://127.0.0.1:7777", "env-token", "env-slot")
    _write_launch_json(tmp_path, {
        "bridge_url": "http://127.0.0.1:9611",
        "written_at": time_mod.time() - 3600,
        "ttl": 7200,  # file's own window overrides the default
    })

    namespace, _ = _launch_file_namespace(tmp_path)
    cfg = namespace["VNFPlayerConfig"]()

    assert cfg.bridge_url == "http://127.0.0.1:9611"


def test_shim_launch_file_malformed_or_absent_is_safe(tmp_path, monkeypatch):
    """Malformed/unreadable/missing files must never break config init —
    fall through to env."""
    _set_launch_env(monkeypatch, "http://127.0.0.1:7777", "env-token", "env-slot")

    # 1. No file at all.
    namespace, _ = _launch_file_namespace(tmp_path)
    cfg = namespace["VNFPlayerConfig"]()
    assert cfg.bridge_url == "http://127.0.0.1:7777"
    assert cfg.slot_token == "env-token"
    assert cfg.save_slot == "env-slot"

    # 2. Garbage bytes.
    (tmp_path / "vnflight_launch.json").write_bytes(b"{not json \xff\xfe")
    namespace, _ = _launch_file_namespace(tmp_path)
    cfg = namespace["VNFPlayerConfig"]()
    assert cfg.bridge_url == "http://127.0.0.1:7777"
    assert namespace["_vnf_launch_file_note"]

    # 3. Valid JSON, wrong shape.
    (tmp_path / "vnflight_launch.json").write_text("[1, 2]", encoding="utf-8")
    namespace, _ = _launch_file_namespace(tmp_path)
    cfg = namespace["VNFPlayerConfig"]()
    assert cfg.bridge_url == "http://127.0.0.1:7777"

    # 4. Bogus written_at.
    _write_launch_json(tmp_path, {
        "bridge_url": "http://127.0.0.1:9611",
        "written_at": "not-a-number",
    })
    namespace, _ = _launch_file_namespace(tmp_path)
    cfg = namespace["VNFPlayerConfig"]()
    assert cfg.bridge_url == "http://127.0.0.1:7777"

    # 5. Missing gamedir (renpy.config without gamedir) — reader no-ops.
    import types

    namespace, _ = _launch_file_namespace(tmp_path)
    namespace["renpy"] = types.SimpleNamespace(config=types.SimpleNamespace())
    assert namespace["_vnf_read_launch_file"]() is None


def test_shim_launch_file_claimed_by_other_pid_is_rejected(tmp_path, monkeypatch):
    """Claim protocol: a file another live process already adopted belongs
    to that launch.  Two same-game launches must not share one bridge."""
    import os as os_mod
    import time as time_mod

    _set_launch_env(monkeypatch, "http://127.0.0.1:7777", "env-token", "env-slot")
    _write_launch_json(tmp_path, {
        "bridge_url": "http://127.0.0.1:9611",
        "slot_token": "file-token",
        "written_at": time_mod.time(),
        "claimed_by": os_mod.getpid() + 12345,
        "claimed_at": time_mod.time(),
    })

    namespace, _ = _launch_file_namespace(tmp_path)
    assert namespace["_vnf_read_launch_file"]() is None
    assert "claimed by pid" in namespace["_vnf_launch_file_note"]

    cfg = namespace["VNFPlayerConfig"]()
    assert cfg.bridge_url == "http://127.0.0.1:7777"
    assert cfg.slot_token == "env-token"


def test_unknown_slot_reconnect_adopts_fresh_launch_intent_once():
    source = SHIM.read_text(encoding="utf-8")

    assert "def _recover_stale_slot(self, error, stale_slot):" in source
    assert '"Unknown slot identity" in raw' in source
    assert 'code == 409 and "Slot is closed" in raw' in source
    assert "launch = _vnf_read_launch_file()" in source
    assert "self._cfg.slot_token = str(token) if token else None" in source
    assert "self.slot_id = None" in source
    assert "self.assign_slot(registration_context=launch)" in source
    assert "_retry_unknown_slot=False" in source
    assert "if not newer and not different:" in source


@pytest.mark.parametrize("failure", ["unknown", "transport"])
@pytest.mark.parametrize("new_save_slot", ["new-save", ""])
def test_stale_slot_recovery_reassigns_and_retries(
    failure, new_save_slot,
):
    import threading
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )

    class FakeHTTPError(Exception):
        code = 403

        def read(self):
            return b'{"error":"Unknown slot identity."}'

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    calls = []

    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url
            self.data = data
            self.headers = headers
            self.method = None

    def urlopen(request, timeout=0):
        calls.append(request.url)
        if len(calls) == 1:
            if failure == "unknown":
                raise FakeHTTPError()
            raise OSError("old bridge is gone")
        if request.url.endswith("/slots/assign"):
            return Response({
                "slot_id": 52,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            })
        return Response({"command": None})

    launch = {
        "bridge_url": "http://127.0.0.1:9601",
        "slot_token": "new-token",
        "save_slot": new_save_slot,
        "written_at": 200.0,
    }
    claimed = []
    applied_save_slots = []
    namespace = {
        "_HAS_URLLIB": True,
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "_is_legacy": False,
        "_tb_module": types.SimpleNamespace(print_exc=lambda: None),
        "_urllib_parse": types.SimpleNamespace(urlencode=lambda value: ""),
        "_urllib_request": types.SimpleNamespace(
            Request=Request, urlopen=urlopen,
        ),
        "_vnf_claim_launch_file": lambda value: claimed.append(value),
        "_vnf_apply_save_slot": applied_save_slots.append,
        "_vnf_log": lambda value: None,
        "_vnf_read_launch_file": lambda: dict(launch),
        "json": json,
        "os": __import__("os"),
        "threading": threading,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    config = types.SimpleNamespace(
        enabled=True,
        bridge_url="http://127.0.0.1:9600",
        slot_token="old-token",
        debug=False,
        save_slot="old-save",
        _launch_file={"written_at": 100.0},
    )
    client = namespace["VNFBridgeClient"](config)
    client.slot_id = 51

    assert client._get("/command") == {"command": None}
    assert client.slot_id == 52
    assert config.bridge_url == "http://127.0.0.1:9601"
    assert config.slot_token == "new-token"
    assert config.save_slot == new_save_slot
    assert applied_save_slots == [new_save_slot]
    assert claimed == [launch]
    assert calls == [
        "http://127.0.0.1:9600/51/command",
        "http://127.0.0.1:9601/slots/assign",
        "http://127.0.0.1:9601/52/command",
    ]


@pytest.mark.parametrize(
    "failure_stage", ["assign_transport", "assign_terminal", "save"],
)
def test_stale_slot_recovery_retries_after_transient_handoff_failure(
    failure_stage,
):
    import threading
    import types
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    class AssignmentError(Exception):
        code = 409

        def read(self):
            return (
                b'{"error":"replacement reservation not ready",'
                b'"status":"reserved"}'
            )

    calls = []
    assignment_payloads = []

    class Request:
        def __init__(self, url, data=None, headers=None):
            self.url = url
            self.data = data
            self.headers = headers
            self.method = None

    assign_attempts = [0]

    def urlopen(request, timeout=0):
        calls.append(request.url)
        if request.url.endswith("/slots/assign"):
            assignment_payloads.append(json.loads(request.data.decode("utf-8")))
            assign_attempts[0] += 1
            if failure_stage == "assign_transport" and assign_attempts[0] == 1:
                raise OSError("replacement bridge still starting")
            if failure_stage == "assign_terminal" and assign_attempts[0] == 1:
                raise AssignmentError()
            return Response({
                "slot_id": 52,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            })
        if "/51/command" in request.url:
            raise OSError("old bridge is gone")
        return Response({"command": None})

    launch = {
        "bridge_url": "http://127.0.0.1:9601",
        "slot_token": "new-token",
        "save_slot": "new-save",
        "launch_id": "new-launch",
        "written_at": 200.0,
    }
    save_attempts = [0]

    def apply_save_slot(value):
        save_attempts[0] += 1
        if failure_stage == "save" and save_attempts[0] == 1:
            raise OSError("save directory temporarily unavailable")

    claimed = []
    receipts = []
    namespace = {
        "_HAS_URLLIB": True,
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "_is_legacy": False,
        "_tb_module": types.SimpleNamespace(print_exc=lambda: None),
        "_urllib_parse": types.SimpleNamespace(urlencode=lambda value: ""),
        "_urllib_request": types.SimpleNamespace(
            Request=Request, urlopen=urlopen,
        ),
        "_vnf_apply_save_slot": apply_save_slot,
        "_vnf_claim_launch_file": claimed.append,
        "_vnf_record_launch_registration": lambda *a, **k: receipts.append((a, k)),
        "_vnf_log": lambda value: None,
        "_vnf_read_launch_file": lambda: dict(launch),
        "json": json,
        "os": __import__("os"),
        "threading": threading,
        "uuid": uuid,
    }
    exec_shim_nodes([client_node], namespace)
    config = types.SimpleNamespace(
        enabled=True,
        bridge_url="http://127.0.0.1:9600",
        slot_token="old-token",
        save_slot="old-save",
        debug=False,
        _launch_file={"launch_id": "old-launch", "written_at": 100.0},
    )
    client = namespace["VNFBridgeClient"](config)
    client.slot_id = 51

    assert client._get("/command") is None
    assert client.slot_id == 51
    assert config.bridge_url == "http://127.0.0.1:9600"
    assert config.slot_token == "old-token"
    assert config.enabled is True
    if failure_stage == "assign_terminal":
        assert receipts == [], (
            "a retryable reservation handoff must not publish a failed launch"
        )
    assert client._get("/command") == {"command": None}
    assert client.slot_id == 52
    assert config.save_slot == "new-save"
    assert claimed == [launch]
    assert receipts
    assert all(item[0][0]["launch_id"] == "new-launch" for item in receipts)
    assert assignment_payloads
    assert all(
        payload["registration_retry_mode"] == "recovery"
        and "registration_retry_until" not in payload
        for payload in assignment_payloads
    )
    if failure_stage == "assign_terminal":
        assert [item[0][1] for item in receipts] == ["assigned"]
    assert calls == [
        "http://127.0.0.1:9600/51/command",
        "http://127.0.0.1:9601/slots/assign",
        "http://127.0.0.1:9600/51/command",
        "http://127.0.0.1:9601/slots/assign",
        "http://127.0.0.1:9601/52/command",
    ]


def test_shim_launch_file_own_claim_is_accepted(tmp_path, monkeypatch):
    """Our OWN claim must still read: utter_restart re-runs init and
    re-reads the same file."""
    import os as os_mod
    import time as time_mod

    _set_launch_env(monkeypatch, "http://127.0.0.1:7777", "env-token", "env-slot")
    path = _write_launch_json(tmp_path, {
        "bridge_url": "http://127.0.0.1:9611",
        "slot_token": "file-token",
        "written_at": time_mod.time(),
        "claimed_by": os_mod.getpid(),
        "claimed_at": time_mod.time(),
    })

    namespace, _ = _launch_file_namespace(tmp_path)
    assert namespace["_vnf_read_launch_file"]() is not None

    cfg = namespace["VNFPlayerConfig"]()
    assert cfg.bridge_url == "http://127.0.0.1:9611"
    assert cfg.slot_token == "file-token"
    # Already ours: no pointless rewrite, claimed_at is left alone.
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["claimed_by"] == os_mod.getpid()


def test_registration_receipt_preserves_claim_and_token(tmp_path):
    import types

    module = parse_shim_python()
    receipt_node = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_vnf_record_launch_registration"
    )
    launch = {
        "launch_id": "launch-1",
        "bridge_url": "http://127.0.0.1:8385",
        "slot_token": "secret-token",
        "written_at": 100.0,
        "claimed_by": 1234,
        "claimed_at": 101.0,
    }
    target = _write_launch_json(tmp_path, launch)
    namespace = {
        "_VNF_LAUNCH_FILE_NAME": target.name,
        "_VNF_LAUNCH_RECEIPT_PREFIX": "vnflight_registration_",
        "_VNFLIGHT_SHIM_PROTOCOL_VERSION": SHIM_PROTOCOL_VERSION,
        "json": json,
        "os": __import__("os"),
        "renpy": types.SimpleNamespace(
            config=types.SimpleNamespace(gamedir=str(tmp_path)),
        ),
        "time": __import__("time"),
    }
    exec_shim_nodes([receipt_node], namespace)

    assert namespace["_vnf_record_launch_registration"](
        launch, "assigned", slot_id=7,
    ) is True
    assert json.loads(target.read_text(encoding="utf-8")) == launch
    receipt_path = tmp_path / "vnflight_registration_launch-1.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["launch_id"] == "launch-1"
    assert receipt["status"] == "assigned"
    assert receipt["slot_id"] == 7
    assert "slot_token" not in receipt


def test_shim_claims_launch_file_on_adoption(tmp_path, monkeypatch):
    """Adopting a file stamps our pid into it (that is what the launcher
    waits for), preserving the launcher's payload and written_at."""
    import os as os_mod
    import time as time_mod

    _set_launch_env(monkeypatch, "http://127.0.0.1:7777", "env-token", "env-slot")
    written_at = time_mod.time()
    path = _write_launch_json(tmp_path, {
        "bridge_url": "http://127.0.0.1:9611",
        "slot_token": "file-token",
        "save_slot": "file-slot",
        "written_at": written_at,
    })

    namespace, _ = _launch_file_namespace(tmp_path)
    cfg = namespace["VNFPlayerConfig"]()
    assert cfg.bridge_url == "http://127.0.0.1:9611"

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["claimed_by"] == os_mod.getpid()
    assert data["claimed_at"] >= written_at
    # Payload survives untouched -- written_at especially: it is the
    # launcher's freshness stamp, not ours to refresh.
    assert data["bridge_url"] == "http://127.0.0.1:9611"
    assert data["slot_token"] == "file-token"
    assert data["save_slot"] == "file-slot"
    assert data["written_at"] == written_at
    # Atomic-ish: tmp file renamed into place, nothing left behind.
    assert [p.name for p in tmp_path.iterdir()] == ["vnflight_launch.json"]


def test_shim_claim_launch_file_never_raises(tmp_path, monkeypatch):
    """A lost claim costs serialization, not the launch -- every failure
    path returns False quietly."""
    import time as time_mod

    _set_launch_env(monkeypatch, "http://127.0.0.1:7777", "env-token", "env-slot")
    namespace, _ = _launch_file_namespace(tmp_path)
    claim = namespace["_vnf_claim_launch_file"]
    payload = {"bridge_url": "http://127.0.0.1:9611", "written_at": time_mod.time()}

    # Unwritable/nonexistent directory.
    assert claim(payload, str(tmp_path / "no-such-dir")) is False
    assert namespace["_vnf_launch_file_note"]
    # No gamedir at all.
    assert claim(payload, "") is False
    # Payload that isn't a mapping.
    assert claim(["not", "a", "dict"], str(tmp_path)) is False
    # Nothing was created by the failures.
    assert list(tmp_path.iterdir()) == []


def test_shim_launch_file_slice_is_py2_compatible(tmp_path):
    """The shim runs on Ren'Py 6/7 (Python 2): the launch-file code must
    not use f-strings or other Py3-only constructs."""
    _, sliced = _launch_file_namespace(tmp_path)
    tree = ast.parse(sliced)
    fstrings = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
    assert fstrings == [], "launch-file slice must not use f-strings (Py2)"
    # Guarded byte handling: file read in binary + explicit decode, no
    # Py3-only open(encoding=...) in the reader.
    assert 'open(path, "rb")' in sliced
    assert 'raw.decode("utf-8")' in sliced


# ---------------------------------------------------------------------------
# Screenshot push throttle (executable slice)
#
# `_vnf_capture_screenshot` is the single choke point for every screenshot
# path (interaction callback, scene change, 6.x tick, manual command).  Under
# auto-advance + restart_interaction churn the callbacks fire ~20x/sec; the
# min-interval + content-dedup gates keep that from saturating the bridge.
# These tests exec the throttle slice out of the shim and drive it with a
# controllable clock + capture bytes.
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now


def test_explicit_screenshot_identity_survives_newer_automatic_frame():
    clock, client = _FakeClock(), _RecordingClient()
    frames = iter([b"requested", b"newer"])
    ns, _ = _screenshot_namespace(clock, client, _screenshot_player(), lambda: next(frames))
    assert ns["_vnf_capture_screenshot"](force=True, capture_id="request") is True
    clock.now += 10
    ns["_vnf_capture_screenshot"]()
    assert [e["capture_id"] for e in client.pushes] == ["request", "request"]


def test_selected_menu_refusal_excludes_toggles_and_composite_actions():
    ns = load_shim_functions("_vnf_already_selected_menu")
    selected = type("ShowMenu", (), {"get_selected": lambda self: True})()
    other = type("ShowMenu", (), {"get_selected": lambda self: False})()
    toggle = type("ToggleField", (), {"get_selected": lambda self: True})()
    check = ns["_vnf_already_selected_menu"]
    assert check(selected)
    assert check([selected])
    assert not check(other)
    assert not check(toggle)
    assert not check([selected, toggle])


@pytest.mark.parametrize("main_menu, enabled, full", [(True, False, True), (False, False, False), (False, True, True)])
def test_menu_text_publication_does_not_enable_gameplay_scraping(main_menu, enabled, full):
    source = SHIM.read_text(encoding="utf-8")
    start = source.index("        if vnf_player.scrape_screens or ev.get(")
    end = source.index("        # Modal screen text:", start)
    client = _RecordingClient()
    ns = {"vnf_player": types.SimpleNamespace(scrape_screens=enabled), "_vnf_client": client,
          "ev": {"type": "screen_content", "main_menu": main_menu,
                 "texts": ["Credits"], "buttons": [{"label": "Return"}]}}
    exec(textwrap.dedent(source[start:end]), ns)
    assert client.pushes[0]["texts"] == (["Credits"] if full else [])


class _RecordingClient:
    def __init__(self):
        self.pushes = []

    def push_event(self, event):
        self.pushes.append(event)


def _screenshot_namespace(clock, client, player, capture):
    """Exec the throttle state + `_vnf_capture_screenshot` slice from the shim.

    *capture* is a zero-arg callable returning the raw bytes the fake
    ``renpy.exports.screenshot_to_bytes`` should hand back.
    """
    import base64 as base64_mod
    import hashlib as hashlib_mod
    import traceback
    import types

    source = SHIM.read_text(encoding="utf-8")
    # Brittle anchors: fail loudly if the throttle block moves.
    start = source.index("    _vnf_screenshot_last_push = [0.0]")
    end = source.index("    def _vnf_screenshot_interact_callback():", start)
    sliced = textwrap.dedent(source[start:end])

    exports = types.SimpleNamespace(
        screenshot_to_bytes=lambda size: capture(),
    )
    namespace = {
        "vnf_player": player,
        "_vnf_client": client,
        "_time": clock,
        "_hashlib": hashlib_mod,
        "base64": base64_mod,
        "_tb_module": traceback,
        "renpy": types.SimpleNamespace(exports=exports),
    }
    exec(compile(sliced, str(SHIM), "exec"), namespace)
    return namespace, sliced


def _screenshot_player():
    import types

    return types.SimpleNamespace(
        enabled=True,
        screenshot_enabled=True,
        screenshot_size=None,
        screenshot_min_interval=1.0,
        debug=False,
    )


def test_screenshot_min_interval_gate_collapses_rapid_pushes():
    """Two captures inside the min-interval window yield a single push."""
    clock = _FakeClock()
    client = _RecordingClient()
    player = _screenshot_player()
    ns, _ = _screenshot_namespace(clock, client, player, lambda: b"frame-A")
    capture = ns["_vnf_capture_screenshot"]

    capture()
    clock.now += 0.1  # well inside the 1.0s window
    capture()

    assert len(client.pushes) == 1


def test_screenshot_content_dedup_skips_identical_frame():
    """A static screen re-captured after the interval is not re-pushed."""
    clock = _FakeClock()
    client = _RecordingClient()
    player = _screenshot_player()
    ns, _ = _screenshot_namespace(clock, client, player, lambda: b"frame-A")
    capture = ns["_vnf_capture_screenshot"]

    capture()  # pushes
    clock.now += 5.0  # past the interval, but same bytes
    capture()  # deduped -> no push

    assert len(client.pushes) == 1
    # Timestamp still refreshed so it stays quiet instead of re-capturing.
    assert ns["_vnf_screenshot_last_push"][0] == clock.now


def test_screenshot_changed_frame_pushes_again():
    """A changed frame after the interval pushes normally."""
    clock = _FakeClock()
    client = _RecordingClient()
    player = _screenshot_player()
    frames = [b"frame-A", b"frame-B"]
    ns, _ = _screenshot_namespace(
        clock, client, player, lambda: frames.pop(0))
    capture = ns["_vnf_capture_screenshot"]

    capture()  # frame-A -> push
    clock.now += 5.0
    capture()  # frame-B -> push

    assert len(client.pushes) == 2


def test_screenshot_force_bypasses_both_gates():
    """force=True (the manual command) ignores interval AND dedup."""
    clock = _FakeClock()
    client = _RecordingClient()
    player = _screenshot_player()
    ns, _ = _screenshot_namespace(clock, client, player, lambda: b"frame-A")
    capture = ns["_vnf_capture_screenshot"]

    capture(force=True)  # push
    # Same bytes, same instant: interval gate + dedup gate would both skip,
    # but force bypasses them.
    capture(force=True)  # push
    capture(force=True)  # push

    assert len(client.pushes) == 3


def test_screenshot_min_interval_is_configurable_via_player():
    """Lowering screenshot_min_interval lets a vision-heavy profile push
    more often (the field is a plain vnf_player attr, set-able like the
    other screenshot config)."""
    clock = _FakeClock()
    client = _RecordingClient()
    player = _screenshot_player()
    player.screenshot_min_interval = 0.0  # no min-interval gate
    frames = [b"a", b"b", b"c"]
    ns, _ = _screenshot_namespace(
        clock, client, player, lambda: frames.pop(0))
    capture = ns["_vnf_capture_screenshot"]

    capture()
    capture()  # same instant, but interval=0 and different bytes -> push
    capture()

    assert len(client.pushes) == 3


def test_screenshot_throttle_slice_is_py2_compatible():
    """The throttle slice runs on Ren'Py 6/7 (Python 2) — no f-strings."""
    clock = _FakeClock()
    client = _RecordingClient()
    player = _screenshot_player()
    _, sliced = _screenshot_namespace(clock, client, player, lambda: b"x")
    tree = ast.parse(sliced)
    fstrings = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
    assert fstrings == [], "throttle slice must not use f-strings (Py2)"


# ---------------------------------------------------------------------------
# Turbo mode (executable slice)
#
# Turbo is the "render as fast as the engine allows" switch for agent-driven
# validation runs: instant text, no transitions, clamped scripted pauses.
# Everything it touches is saved on the way in, so these tests care mostly
# about the restore path and about the pause patch NOT stacking.
# ---------------------------------------------------------------------------


def _turbo_namespace(player=None, preferences=None, pause=None):
    """Exec the turbo block out of the shim against a fake Ren'Py."""
    import types

    source = SHIM.read_text(encoding="utf-8")
    # Brittle anchors: fail loudly if the turbo block moves.
    start = source.index("    # --- Turbo mode ---")
    end = source.index("    def _vnf_periodic_auto_advance():", start)
    sliced = textwrap.dedent(source[start:end])

    if player is None:
        player = types.SimpleNamespace(
            enabled=True, turbo=False, fast_forward=False, debug=False,
            auto_advance_delay=1, post_action_delay=2)
    if preferences is None:
        preferences = types.SimpleNamespace(text_cps=30, transitions=2)
    if pause is None:
        calls = []

        def pause(delay=None, **kwargs):
            calls.append(delay)
            return delay

        pause.calls = calls

    renpy = types.SimpleNamespace(
        exports=types.SimpleNamespace(pause=pause),
        pause=pause,
        config=types.SimpleNamespace(allow_skipping=True),
        game=types.SimpleNamespace(preferences=preferences),
    )
    stash_calls = []
    namespace = {
        "vnf_player": player,
        "renpy": renpy,
        "_vnf_log": lambda _message: None,
        # Persistent-stash helpers live outside the turbo slice; the
        # capture list lets tests assert turbo stashes what it mutates.
        "_vnf_stash_pref": lambda name, value: stash_calls.append(
            ("stash", name, value)),
        "_vnf_unstash_pref": lambda name: stash_calls.append(
            ("unstash", name)),
        "_vnf_auto_advance_active": True,
        "_vnf_enable_auto_advance": lambda: None,
    }
    helpers = load_shim_functions()
    namespace["_vnf_stringify"] = helpers["_vnf_stringify"]
    namespace["_vnf_text"] = helpers["_vnf_text"]
    ff_start = source.index("    _vnf_fast_forward_saved_box = [None]")
    ff_end = source.index("    # --- Turbo mode ---", ff_start)
    exec(compile(textwrap.dedent(source[ff_start:ff_end]),
                 str(SHIM), "exec"), namespace)
    exec(compile(sliced, str(SHIM), "exec"), namespace)
    namespace["_fake_renpy"] = renpy
    namespace["_fake_prefs"] = preferences
    namespace["_original_pause"] = pause
    namespace["_stash_calls"] = stash_calls
    return namespace, sliced


def test_turbo_apply_zeroes_text_speed_and_transitions():
    ns, _ = _turbo_namespace()
    ns["vnf_player"].turbo = True
    ns["_vnf_apply_turbo"]()

    assert ns["_fake_prefs"].text_cps == 0
    assert ns["_fake_prefs"].transitions == 0


def test_turbo_pause_patch_clamps_only_positive_delays():
    ns, _ = _turbo_namespace()
    ns["vnf_player"].turbo = True
    ns["_vnf_apply_turbo"]()

    patched = ns["_fake_renpy"].pause
    assert patched is ns["_fake_renpy"].exports.pause
    assert patched is not ns["_original_pause"]

    patched(5.0)      # long scripted pause -> clamped
    patched(0.1)      # already faster than the cap -> untouched
    patched()         # bare/infinite pause -> left to the shim's own wrapper

    assert ns["_original_pause"].calls == [0.2, 0.1, None]


def test_turbo_pause_patch_is_inert_once_the_flag_is_off():
    """Belt and braces: the wrapper re-checks vnf_player.turbo, so even a
    wrapper left in the chain by a foreign patch stops clamping."""
    ns, _ = _turbo_namespace()
    ns["vnf_player"].turbo = True
    ns["_vnf_apply_turbo"]()
    patched = ns["_fake_renpy"].pause

    ns["vnf_player"].turbo = False
    patched(5.0)

    assert ns["_original_pause"].calls == [5.0]


def test_turbo_pause_patch_does_not_stack_on_repeated_enable():
    """Turning turbo on twice must not wrap the wrapper — otherwise the
    original function becomes unrecoverable."""
    ns, _ = _turbo_namespace()
    ns["vnf_player"].turbo = True
    ns["_vnf_apply_turbo"]()
    first = ns["_fake_renpy"].pause
    ns["_vnf_apply_turbo"]()
    ns["_vnf_apply_turbo"]()

    assert ns["_fake_renpy"].pause is first

    ns["vnf_player"].turbo = False
    ns["_vnf_restore_turbo"]()

    assert ns["_fake_renpy"].pause is ns["_original_pause"]
    assert ns["_fake_renpy"].exports.pause is ns["_original_pause"]


def test_turbo_restore_puts_back_the_exact_previous_values():
    ns, _ = _turbo_namespace()
    ns["vnf_player"].turbo = True
    ns["_vnf_apply_turbo"]()
    # Re-applying while already on must not memorize turbo's own values
    # (0/0) as the restore target.
    ns["_vnf_apply_turbo"]()
    ns["vnf_player"].turbo = False
    ns["_vnf_restore_turbo"]()

    assert ns["_fake_prefs"].text_cps == 30
    assert ns["_fake_prefs"].transitions == 2
    assert ns["_vnf_turbo_saved_box"] == [None]
    assert ns["_vnf_turbo_pause_patch"] == [None]


def test_turbo_sync_applies_a_flag_that_was_set_outside_the_set_command():
    """Profiles / mods / the console can set vnf_player.turbo directly; the
    interact callback reconciles it."""
    ns, _ = _turbo_namespace()
    ns["vnf_player"].turbo = True

    ns["_vnf_sync_turbo"]()
    assert ns["_fake_prefs"].text_cps == 0
    assert ns["_fake_renpy"].pause is not ns["_original_pause"]

    # Idempotent: repeated interactions must not re-save or re-wrap.
    saved = ns["_vnf_turbo_saved_box"][0]
    patched = ns["_fake_renpy"].pause
    ns["_vnf_sync_turbo"]()
    assert ns["_vnf_turbo_saved_box"][0] is saved
    assert ns["_fake_renpy"].pause is patched

    ns["vnf_player"].turbo = False
    ns["_vnf_sync_turbo"]()
    assert ns["_fake_prefs"].text_cps == 30
    assert ns["_fake_renpy"].pause is ns["_original_pause"]


def test_turbo_sync_reasserts_preferences_a_save_load_put_back():
    ns, _ = _turbo_namespace()
    ns["vnf_player"].turbo = True
    ns["_vnf_sync_turbo"]()

    # Something (a loaded save, the game's own options screen) restores them.
    ns["_fake_prefs"].text_cps = 30
    ns["_fake_prefs"].transitions = 2
    ns["_vnf_sync_turbo"]()

    assert ns["_fake_prefs"].text_cps == 0
    assert ns["_fake_prefs"].transitions == 0
    # ...but the restore target is still the pre-turbo values.
    ns["vnf_player"].turbo = False
    ns["_vnf_sync_turbo"]()
    assert ns["_fake_prefs"].text_cps == 30
    assert ns["_fake_prefs"].transitions == 2


def test_turbo_and_fast_forward_compose_when_nested():
    """Both features zero preferences.text_cps.  Each saves what it found,
    so on/on/off/off (either order) ends at the original value."""
    ns, _ = _turbo_namespace()
    player = ns["vnf_player"]

    # turbo first, then fast_forward, unwound in reverse.
    player.turbo = True
    ns["_vnf_apply_turbo"]()
    player.fast_forward = True          # _vnf_enable_fast_forward saves 0
    player.fast_forward = False         # ...and restores 0 on disable
    player.turbo = False
    ns["_vnf_restore_turbo"]()

    assert ns["_fake_prefs"].text_cps == 30
    assert ns["_fake_prefs"].transitions == 2


def test_turbo_off_does_not_undo_an_active_fast_forward():
    """Non-nested order: disabling turbo while fast_forward is still on must
    leave instant text alone — fast_forward holds its own saved value."""
    ns, _ = _turbo_namespace()
    player = ns["vnf_player"]

    player.turbo = True
    ns["_vnf_apply_turbo"]()
    player.fast_forward = True
    player.turbo = False
    ns["_vnf_restore_turbo"]()

    assert ns["_fake_prefs"].text_cps == 0    # left to fast_forward
    assert ns["_fake_prefs"].transitions == 2  # turbo owns this outright


def test_turbo_then_fast_forward_restore_in_non_lifo_order():
    """The two runtime modes are independent toggles, not a stack. Turning
    turbo off first hands its pre-turbo text speed to fast-forward; the last
    owner restores it and only then clears the durable stash."""
    ns, _ = _turbo_namespace()
    player = ns["vnf_player"]

    player.turbo = True
    ns["_vnf_apply_turbo"]()
    ns["_vnf_enable_fast_forward"]()
    player.turbo = False
    ns["_vnf_restore_turbo"]()

    text_unstashes = [call for call in ns["_stash_calls"]
                      if call == ("unstash", "text_cps")]
    assert text_unstashes == []
    assert ns["_fake_prefs"].text_cps == 0

    ns["_vnf_disable_fast_forward"]()
    assert ns["_fake_prefs"].text_cps == 30
    assert ns["_stash_calls"].count(("unstash", "text_cps")) == 1


def test_fast_forward_then_turbo_restore_in_non_lifo_order():
    """The symmetric order also leaves the original speed with turbo when
    fast-forward exits first."""
    ns, _ = _turbo_namespace()
    player = ns["vnf_player"]

    ns["_vnf_enable_fast_forward"]()
    player.turbo = True
    ns["_vnf_apply_turbo"]()
    ns["_vnf_disable_fast_forward"]()

    assert ns["_fake_prefs"].text_cps == 0
    assert ("unstash", "text_cps") not in ns["_stash_calls"]

    player.turbo = False
    ns["_vnf_restore_turbo"]()
    assert ns["_fake_prefs"].text_cps == 30
    assert ns["_stash_calls"].count(("unstash", "text_cps")) == 1


def test_fast_forward_does_not_touch_transitions_so_turbo_owns_them():
    """Guards the no-duplication claim: the fast-forward helpers save and
    set text_cps/afm/delays but never preferences.transitions."""
    source = SHIM.read_text(encoding="utf-8")
    start = source.index("    def _vnf_enable_fast_forward():")
    end = source.index("    # --- Turbo mode ---", start)
    ff_source = source[start:end]

    assert "renpy.game.preferences.text_cps = 0" in ff_source
    assert "preferences.transitions" not in ff_source


def test_turbo_survives_a_renpy_without_the_preferences_it_wants():
    """Defensive style: a missing/read-only preference must not stop turbo
    from installing the pause clamp, and must not break restore."""
    import types

    class _LockedPrefs(object):
        def __setattr__(self, name, value):
            raise RuntimeError("read-only preferences")

    ns, _ = _turbo_namespace(preferences=_LockedPrefs())
    ns["vnf_player"].turbo = True
    ns["_vnf_apply_turbo"]()

    assert ns["_fake_renpy"].pause is not ns["_original_pause"]
    ns["_fake_renpy"].pause(5.0)
    assert ns["_original_pause"].calls == [0.2]

    ns["vnf_player"].turbo = False
    ns["_vnf_restore_turbo"]()
    assert ns["_fake_renpy"].pause is ns["_original_pause"]
    assert ns["_vnf_turbo_saved_box"] == [None]


def test_turbo_slice_is_py2_compatible():
    """The shim runs on Ren'Py 6/7 (Python 2) — no f-strings."""
    _, sliced = _turbo_namespace()
    tree = ast.parse(sliced)
    fstrings = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
    assert fstrings == [], "turbo slice must not use f-strings (Py2)"


def test_turbo_is_a_settable_config_key_with_a_false_default():
    source = SHIM.read_text(encoding="utf-8")

    # Declared on the config object -> accepted by `set` (which rejects
    # anything not already an attribute of vnf_player) and captured in the
    # init-time defaults snapshot that get_defaults reports.
    assert "self.turbo = False" in source
    assert "_vnf_config_defaults = {" in source
    assert 'elif key == "turbo":' in source


def test_set_turbo_key_applies_only_on_transition():
    """`set turbo` drives apply/restore, coerces strings to bool, and does
    not re-apply when the value is unchanged."""
    calls = []
    player = types.SimpleNamespace(turbo=False)
    ns = load_shim_functions(
        "_vnf_coerce_config_value",
        "_vnf_set_config_key",
        namespace={
            "vnf_player": player,
            "_vnf_log": lambda _message: None,
            "_vnf_apply_turbo": lambda: calls.append("apply"),
            "_vnf_restore_turbo": lambda: calls.append("restore"),
        },
    )
    set_key = ns["_vnf_set_config_key"]

    assert set_key("turbo", "true") == (False, True)
    assert player.turbo is True
    set_key("turbo", True)          # unchanged -> no second apply
    assert set_key("turbo", "false") == (True, False)
    assert player.turbo is False
    set_key("turbo", False)         # unchanged -> no second restore

    assert calls == ["apply", "restore"]

    with pytest.raises(KeyError):
        set_key("not_a_real_key", 1)


def test_screenshot_config_field_is_settable_like_other_screenshot_keys():
    """`screenshot_min_interval` is a simple vnf_player attr captured in the
    init-time defaults snapshot, so `set`/profiles can adjust it."""
    source = SHIM.read_text(encoding="utf-8")
    assert "self.screenshot_min_interval = 1.0" in source
    # Snapshot rule: non-underscore, non-callable attrs become settable
    # defaults; a float field qualifies.
    assert "_vnf_config_defaults = {" in source


def test_stats_delta_distinguishes_null_values_from_removed_keys():
    ns = load_shim_functions("_vnf_stats_delta")
    changed, removed = ns["_vnf_stats_delta"](
        {"nullable": 1, "deleted": 2, "steady": 3},
        {"nullable": None, "steady": 3},
    )

    assert changed == {"nullable": None, "deleted": None}
    assert removed == ["deleted"]

    added, added_removed = ns["_vnf_stats_delta"](
        {"steady": 3},
        {"steady": 3, "new_nullable": None},
    )
    assert added == {"new_nullable": None}
    assert added_removed == []


def test_inventory_delta_preserves_occurrences_and_reports_only_changes():
    ns = load_shim_functions("_vnf_inventory_delta")
    old = [
        {"name": "same"},
        {"name": "same"},
        {"name": "removed"},
        {"name": "quantity", "quantity": 1},
    ]
    new = [
        {"name": "same"},
        {"name": "quantity", "quantity": 2},
        {"name": "added"},
    ]

    changed, removed = ns["_vnf_inventory_delta"](old, new)

    assert changed == [
        {"name": "quantity", "quantity": 2},
        {"name": "added"},
    ]
    assert removed == [
        {"name": "same"},
        {"name": "removed"},
        {"name": "quantity", "quantity": 1},
    ]


def test_inventory_delta_initial_snapshot_reports_every_item():
    ns = load_shim_functions("_vnf_inventory_delta")
    current = [{"name": "first"}, {"name": "second"}]

    changed, removed = ns["_vnf_inventory_delta"](None, current)

    assert changed == current
    assert removed == []
    assert changed is not current


def test_inventory_publisher_keeps_snapshot_but_emits_incremental_status_data():
    events = []
    client = types.SimpleNamespace(push_event=lambda event: events.append(event))
    ns = load_shim_functions(
        "_vnf_stats_delta",
        "_vnf_inventory_delta",
        "_vnf_publish_inventory_stats",
        namespace={
            "_vnf_client": client,
            "vnf_player": types.SimpleNamespace(debug=False),
            "_vnf_last_inventory": None,
            "_vnf_last_stats": None,
            "_vnf_consecutive_unchanged_scrapes": 0,
            "_vnf_last_visible_scrape_hash": None,
            "_time": types.SimpleNamespace(time=lambda: 1.0),
        },
    )

    ns["_vnf_publish_inventory_stats"]([{"name": "old"}], {})
    ns["_vnf_publish_inventory_stats"](
        [{"name": "old"}, {"name": "new"}], {})

    updates = [event for event in events
               if event.get("type") == "inventory_update"]
    assert updates[-1]["inventory"] == [
        {"name": "old"}, {"name": "new"},
    ]
    assert updates[-1]["changed"] == [{"name": "new"}]
    assert updates[-1]["removed"] == []


# ---------------------------------------------------------------------------
# Panel (overlay / modal) text freshness
#
# A registered blocking overlay — Echoes' KIT/LOG/MAP, Roadwarden's
# inventory/journal — draws a snapshot of live state: item counts, rows that
# only exist while something is held.  The scrape's delta filter exists for
# the opposite kind of screen (NVL history, terminal scrollback), and it used
# to swallow a re-shown panel whose lines had been emitted once before, so the
# panel body read as whatever happened to be new on the screens underneath.
# ---------------------------------------------------------------------------


def _panel_text_helpers():
    return load_shim_functions(
        "_vnf_panel_text_tags",
        "_vnf_scrape_emit_texts",
        "_vnf_scrape_text_pairs",
        "_vnf_modal_delta_texts",
    )


def _kit_scrape(counts, conditional=None):
    """Texts + per-text screen tags for one KIT-open scrape.

    Mirrors the live shape: the map and HUD stay visible underneath the
    modal panel, so their text is in every scrape too.
    """
    texts = ["Select station section", "STATION STATUS", "EQUIPMENT"]
    sources = ["observatory_map", "observatory_hud", "equipment_screen"]
    for label, count in counts:
        texts.append("{} x{}".format(label, count))
        sources.append("equipment_screen")
    for label in (conditional or []):
        texts.append(label)
        sources.append("equipment_screen")
    return texts, sources


def test_panel_text_survives_delta_filter_when_only_counts_change():
    """Re-opening a static-shape panel reports the CURRENT counts.

    The tile labels repeat verbatim between opens; only the interpolated
    numbers move.  Emitting the delta alone would drop every tile whose
    text happened to match an earlier scrape.
    """
    ns = _panel_text_helpers()
    tags = ns["_vnf_panel_text_tags"](
        [{"_tag": "observatory_map"}, {"_tag": "equipment_screen"}],
        ["equipment_screen"],
        set(["equipment_screen"]),
        set(),
    )

    first_texts, first_sources = _kit_scrape(
        [("POWER CELL", 0), ("DATA DRIVE", 0), ("SPARE COUPLING", 0)])
    emitted_first = ns["_vnf_scrape_emit_texts"](
        first_texts, first_sources, set(), tags)
    assert "POWER CELL x0" in emitted_first

    # Second open: same tiles, real counts, and the map text underneath is
    # unchanged so it is (correctly) delta filtered away.
    second_texts, second_sources = _kit_scrape(
        [("POWER CELL", 1), ("DATA DRIVE", 2), ("SPARE COUPLING", 2)])
    emitted_second = ns["_vnf_scrape_emit_texts"](
        second_texts, second_sources, set(first_texts), tags)

    assert "POWER CELL x1" in emitted_second
    assert "DATA DRIVE x2" in emitted_second
    assert "SPARE COUPLING x2" in emitted_second
    assert "POWER CELL x0" not in emitted_second
    assert "Select station section" not in emitted_second


def test_panel_repeated_body_is_still_reported_when_nothing_changed():
    """An unchanged tile must not vanish just because it repeats.

    The count that did NOT move between opens is the one the old delta
    filter dropped, leaving a panel that listed some tiles and not others.
    """
    ns = _panel_text_helpers()
    tags = set(["equipment_screen"])

    first_texts, first_sources = _kit_scrape(
        [("POWER CELL", 1), ("DATA DRIVE", 2)])
    second_texts, second_sources = _kit_scrape(
        [("POWER CELL", 1), ("DATA DRIVE", 3)])

    emitted = ns["_vnf_scrape_emit_texts"](
        second_texts, second_sources, set(first_texts), tags)

    assert "POWER CELL x1" in emitted   # unchanged tile, still reported
    assert "DATA DRIVE x3" in emitted
    assert "EQUIPMENT" in emitted       # panel header repeats every open


def test_panel_conditional_row_appears_on_later_open():
    """A row gated on `if coolant_cartridges > 0` shows up once held."""
    ns = _panel_text_helpers()
    tags = set(["equipment_screen"])

    first_texts, first_sources = _kit_scrape([("POWER CELL", 0)])
    second_texts, second_sources = _kit_scrape(
        [("POWER CELL", 1)], conditional=["COOLANT x1", "RF PREAMP x1"])

    emitted = ns["_vnf_scrape_emit_texts"](
        second_texts, second_sources, set(first_texts), tags)

    assert "COOLANT x1" in emitted
    assert "RF PREAMP x1" in emitted


def test_delta_filter_still_strips_repeated_non_panel_text():
    """The dedup the exemption is carved out of must keep working.

    NVL/terminal scrollback re-scrapes its whole backlog every tick; only
    the new line belongs in the event.
    """
    ns = _panel_text_helpers()
    backlog = ["line one", "line two", "line three"]
    texts = backlog + ["line four"]
    sources = ["nvl"] * len(texts)

    emitted = ns["_vnf_scrape_emit_texts"](
        texts, sources, set(backlog), set())

    assert emitted == ["line four"]


def test_panel_tags_exclude_passive_overlays():
    """Passive overlays are the cumulative renderers — keep them delta'd.

    Echoes' live terminal is registered as a (non-blocking) overlay and
    redraws its entire scrollback; exempting it would re-ship the whole
    log on every scrape.
    """
    ns = _panel_text_helpers()
    per_screen = [
        {"_tag": "echo_terminal_live"},
        {"_tag": "equipment_screen"},
        {"_tag": "observatory_map"},
    ]
    tags = ns["_vnf_panel_text_tags"](
        per_screen,
        [],
        set(["echo_terminal_live", "equipment_screen"]),
        set(["echo_terminal_live"]),
    )

    assert tags == set(["equipment_screen"])


def test_panel_tags_cover_unregistered_modal_screens():
    """A `call screen` popup is a panel even without an overlay registration."""
    ns = _panel_text_helpers()
    tags = ns["_vnf_panel_text_tags"](
        [{"_tag": "confirm"}], ["confirm"], set(), set())
    assert tags == set(["confirm"])


def test_modal_screen_text_event_only_carries_modal_lines():
    """screen_text is a transcript event: new lines, drawn by the modal.

    The map text under the panel used to ride along and be attributed to
    the panel, so the popup read as if it had said something it never
    showed.
    """
    ns = _panel_text_helpers()
    texts, sources = _kit_scrape([("POWER CELL", 1)])
    texts.append("Storm 2/3")
    sources.append("observatory_hud")

    # Nothing seen before: the panel body is new, the HUD line is too, but
    # only the panel's own lines belong to the modal event.
    out = ns["_vnf_modal_delta_texts"](
        texts, sources, set(), set(["equipment_screen"]))
    assert out == ["EQUIPMENT", "POWER CELL x1"]

    # Repeats stay out — screen_text lands in the transcript.
    prev = set(ns["_vnf_scrape_text_pairs"](texts, sources))
    out_again = ns["_vnf_modal_delta_texts"](
        texts, sources, prev, set(["equipment_screen"]))
    assert out_again == []


def test_panel_text_helpers_are_py2_compatible():
    """These run on Ren'Py 6/7 (Python 2): no f-strings, no walrus."""
    module = parse_shim_python()
    for name in ("_vnf_panel_text_tags", "_vnf_scrape_emit_texts",
                 "_vnf_scrape_text_pairs", "_vnf_modal_delta_texts"):
        node = function_node(module, name)
        assert not [n for n in ast.walk(node) if isinstance(n, ast.JoinedStr)]
        assert not [
            n for n in ast.walk(node)
            if isinstance(n, getattr(ast, "NamedExpr", ()))
        ]


def test_scrape_uses_panel_aware_text_helpers():
    """Wiring check: the scrape path must go through the helpers."""
    source = SHIM.read_text(encoding="utf-8")
    assert "_emit_texts = _vnf_scrape_emit_texts(" in source
    assert "_panel_tags = _vnf_panel_text_tags(" in source
    assert "_modal_delta = _vnf_modal_delta_texts(" in source
    # The modal event must be handed the previous scrape's (tag, text)
    # PAIRS, not the flat text set the ordinary delta filter uses.
    assert "_prev_pairs = set(_vnf_last_scrape_text_pairs)" in source
    assert ("_vnf_last_scrape_text_pairs[:] = _vnf_scrape_text_pairs("
            in source)
    assert "all_data[\"texts\"], _text_sources, _prev_pairs," in source


# ---------------------------------------------------------------------------
# Modal transcript dedup is PER SOURCE SCREEN
#
# b4ef64c made the screen_text event carry only lines the modal itself drew,
# but it still asked "was this string anywhere in the previous scrape?".  A
# popup that repeats a string the HUD was already showing ("STATION STATUS")
# therefore lost that line the moment it opened -- the modal's transcript
# entry silently omitted text the player could plainly see on it.
# ---------------------------------------------------------------------------


def test_modal_line_kept_when_another_screen_showed_the_same_text_before():
    """New ON THE MODAL beats "seen somewhere else last scrape"."""
    ns = _panel_text_helpers()

    # Previous scrape: no modal, the HUD is showing "STATION STATUS".
    prev_texts = ["Select station section", "STATION STATUS"]
    prev_sources = ["observatory_map", "observatory_hud"]
    prev = set(ns["_vnf_scrape_text_pairs"](prev_texts, prev_sources))

    # The popup opens and its header happens to read "STATION STATUS" too.
    texts = ["Select station section", "STATION STATUS", "STATION STATUS",
             "All systems nominal."]
    sources = ["observatory_map", "observatory_hud", "status_popup",
               "status_popup"]

    out = ns["_vnf_modal_delta_texts"](
        texts, sources, prev, set(["status_popup"]))

    assert out == ["STATION STATUS", "All systems nominal."]


def test_unchanged_modal_rescrape_still_dedups():
    """The modal's own repeat is still suppressed -- transcript purity."""
    ns = _panel_text_helpers()

    texts = ["Select station section", "STATION STATUS", "STATION STATUS",
             "All systems nominal."]
    sources = ["observatory_map", "observatory_hud", "status_popup",
               "status_popup"]
    prev = set(ns["_vnf_scrape_text_pairs"](texts, sources))

    out = ns["_vnf_modal_delta_texts"](
        texts, sources, prev, set(["status_popup"]))

    assert out == []

    # ...and a line the modal adds on the next tick still comes through.
    texts2 = texts + ["Coolant loop: NOMINAL"]
    sources2 = sources + ["status_popup"]
    out2 = ns["_vnf_modal_delta_texts"](
        texts2, sources2, prev, set(["status_popup"]))
    assert out2 == ["Coolant loop: NOMINAL"]


def test_modal_delta_without_attribution_falls_back_to_text_compare():
    """Older callers (sources shorter than texts) keep the old semantics."""
    ns = _panel_text_helpers()

    prev = set(ns["_vnf_scrape_text_pairs"](
        ["Are you sure?"], ["confirm"]))

    # No sources at all -> every line is treated as the modal's, compared
    # against the flat set of texts seen last scrape.
    out = ns["_vnf_modal_delta_texts"](
        ["Are you sure?", "Yes / No"], [], prev, set(["confirm"]))
    assert out == ["Yes / No"]


def test_scrape_text_pairs_pads_missing_sources_with_none():
    ns = _panel_text_helpers()
    pairs = ns["_vnf_scrape_text_pairs"](["a", "b", "c"], ["hud"])
    assert pairs == [("hud", "a"), (None, "b"), (None, "c")]


# ---------------------------------------------------------------------------
# Menu-caption attribution (executable slice)
#
# Ren'Py speaks a `menu:` caption through the NARRATOR from
# ast.Menu.execute(), a path that never updates store._last_say_who.  The
# stale value still names whoever spoke last, so without a guard the
# caption is published as that character's dialogue -- live Echoes runs
# rendered Elara's interior prompts as "[Dr. Chen] Should I tell him?".
# ---------------------------------------------------------------------------


class _FakeMenuNode:
    """Stand-in for renpy.ast.Menu."""


class _FakeSayNode:
    """Stand-in for renpy.ast.Say."""


class _FakeCharacter:
    def __init__(self, name, kind=None):
        self.name = name
        # Ren'Py's ADVCharacter copies properties from ``kind`` and does not
        # retain it.  Character(kind=nvl) exposes both mode and display type.
        self.mode = kind or "adv"
        self.display_args = {"type": "nvl" if kind == "nvl" else "say"}


def _character_callback_env(node, last_say_who, last_say_what,
                            characters=None, mode="adv", string_types=str,
                            return_state=False, prepublished=None,
                            engine_version=(7, 5, 2),
                            note_nvl_callback=None):
    """Load the character callback with a stubbed Ren'Py around it.

    `node` is the AST node Ren'Py is currently executing -- a Menu node
    while a caption is being narrated, a Say node for ordinary dialogue.

    `return_state` additionally hands back the mutable engine state
    (`.node`, `.last_say_who`, `.last_say_what`) so a test can drive a
    SEQUENCE of statements through one callback -- required to exercise
    the dedup, which only compares against the immediately preceding
    push.
    """
    import time as time_mod

    pushed: list[dict] = []
    characters = characters or {}
    pending_menu_caption = [None]

    state = types.SimpleNamespace(
        node=node,
        last_say_who=last_say_who,
        last_say_what=last_say_what,
        nvl_occurrences=[],
    )

    store = types.SimpleNamespace(
        _last_say_who=last_say_who,
        _last_say_what=last_say_what,
        _mode=mode,
    )

    renpy = types.SimpleNamespace(
        ast=types.SimpleNamespace(Menu=_FakeMenuNode, Say=_FakeSayNode),
        game=types.SimpleNamespace(
            context=lambda: types.SimpleNamespace(current=("script", 1)),
            script=types.SimpleNamespace(lookup=lambda name: state.node),
            preferences=types.SimpleNamespace(afm_enable=False),
        ),
        store=store,
        python=types.SimpleNamespace(
            py_eval=lambda expr: characters[expr]),
        substitutions=types.SimpleNamespace(
            substitute=lambda raw: (raw, True)),
        text=types.SimpleNamespace(
            extras=types.SimpleNamespace(
                filter_text_tags=lambda value, allow=None: value)),
    )

    namespace = {
        "renpy": renpy,
        "renpy_version": engine_version,
        "basestring": string_types,
        "vnf_player": types.SimpleNamespace(
            enabled=True, dialogue_dedup=True),
        "_get_last_say": lambda: (state.last_say_who, state.last_say_what),
        "_vnf_resolve_display_name_expr": lambda value: value,
        "_vnf_substitute": lambda value: value,
        "_vnf_log": lambda *a, **kw: None,
        "_vnf_client": types.SimpleNamespace(
            push_event=pushed.append),
        "_time": time_mod,
        "_vnf_mouse": types.SimpleNamespace(
            is_user_active=lambda: False),
        "_vnf_last_shim_action_time": time_mod.time(),
        "_vnf_auto_advanced_flag": [False],
        "_vnf_last_dialogue_push": [None, None, 0.0],
        "_vnf_pending_menu_caption": pending_menu_caption,
        "_vnf_last_scrape_text_pairs": [],
        "_vnf_last_visible_scrape_hash": None,
        "_vnf_last_what": None,
        "_vnf_note_nvl_callback_event": (
            note_nvl_callback or (
                lambda event, speaker_source=None:
                    state.nvl_occurrences.append(
                        (event.copy(), speaker_source)))),
        "_vnf_nvl_prepublished_callbacks": (
            prepublished if prepublished is not None else []),
        "_vnf_nvl_event_key": lambda event: (
            event.get("character"), event["text"]),
    }
    ns = load_shim_functions(
        "_vnf_executing_menu_statement",
        "_vnf_clean_event_text",
        "_vnf_stringify",
        "_vnf_character_display_name_state",
        "_vnf_character_display_name",
        "_vnf_claim_nvl_prepublished_callback_event",
        "_vnf_stage_menu_caption",
        "_vnf_attach_pending_menu_caption",
        "_vnf_commit_pending_menu_caption",
        "_vnf_flush_pending_menu_caption",
        "_vnf_character_callback",
        namespace=namespace,
    )
    state.pending_menu_caption = pending_menu_caption
    state.attach_pending_menu_caption = ns[
        "_vnf_attach_pending_menu_caption"
    ]
    state.commit_pending_menu_caption = ns[
        "_vnf_commit_pending_menu_caption"
    ]
    state.flush_pending_menu_caption = ns[
        "_vnf_flush_pending_menu_caption"
    ]
    if return_state:
        return ns["_vnf_character_callback"], pushed, state
    return ns["_vnf_character_callback"], pushed


def test_nvl_character_callback_claims_emitted_boundary_occurrence():
    """Execute the real inverse-ledger branch, not only its AST ordering."""
    markers = [
        (("active", 17), 2, "ARIA>", "Already drained.", None),
    ]
    callback, pushed, state = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who="aria_nvl",
        last_say_what="Already drained.",
        characters={"aria_nvl": _FakeCharacter("ARIA>", kind="nvl")},
        mode="nvl",
        return_state=True,
        prepublished=markers,
    )

    callback("begin", True, type="nvl", what="Already drained.")

    assert pushed == []
    assert markers == []
    assert len(state.nvl_occurrences) == 1
    assert state.nvl_occurrences[0][0]["text"] == "Already drained."


@pytest.mark.parametrize(
    ("engine_version", "persists_on_done"),
    [
        ((6, 99, 12, 4), False),
        ((6, 99, 13), True),
        ((7, 5, 2), True),
        ((0,), True),
    ],
)
def test_nvl_hide_boundary_callback_ownership_matches_engine_era(
        engine_version, persists_on_done):
    """Pre-hide ownership moves forward only when do_done persists the row."""
    lifecycle = []
    markers = []
    occurrence_ns, fallback_pushed, forward_occurrences = (
        _nvl_occurrence_env())

    def forced_flush(source, force_boundary=False):
        lifecycle.append(("flush", source, force_boundary))
        lifecycle.append(("dialogue", "Already drained."))
        markers.append((
            ("active", 23), 4, "ARIA>", "Already drained.", "aria_nvl",
        ))

    hide_ns = load_shim_functions(
        "_vnf_nvl_hide_wrapper",
        namespace={
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_observe_rollback_resume": (
                lambda: lifecycle.append(("observe",))),
            "_vnf_flush_nvl_entries": forced_flush,
            "_vnf_nvl_callback_occurrences": forward_occurrences,
            "_vnf_client": types.SimpleNamespace(
                push_event=lambda event:
                    lifecycle.append(("event", event["type"]))),
            "_vnf_log": lambda message: lifecycle.append(("log", message)),
            "_vnf_original_nvl_hide": (
                lambda *args, **kwargs: lifecycle.append(("original",))),
        },
    )

    hide_ns["_vnf_nvl_hide_wrapper"]()

    callback, callback_pushed = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who="aria_nvl",
        last_say_what="Already drained.",
        characters={"aria_nvl": _FakeCharacter("ARIA>", kind="nvl")},
        mode="nvl",
        prepublished=markers,
        engine_version=engine_version,
        note_nvl_callback=occurrence_ns["_vnf_note_nvl_callback_event"],
    )
    callback("begin", True, type="nvl", what="Already drained.")

    assert lifecycle[:4] == [
        ("observe",),
        ("flush", "pre-hide", True),
        ("dialogue", "Already drained."),
        ("event", "nvl_hide"),
    ]
    assert lifecycle[-1] == ("original",)
    assert callback_pushed == []
    assert markers == []
    assert len(forward_occurrences) == (1 if persists_on_done else 0)

    # On modern engines this is the same occurrence persisted by do_done and
    # must be claimed. On 6.99.12 it represents a later identical fallback and
    # must remain visible because do_add already persisted the drained row.
    occurrence_ns["_vnf_publish_nvl_entry"](
        ("aria_nvl", "Already drained."), "post-done")
    assert len(fallback_pushed) == (0 if persists_on_done else 1)
    assert forward_occurrences == []


def test_nvl_character_detection_accepts_renpy_string_subclasses():
    """Use the copied display type on a real-shaped Character, including py2 names."""
    class RenpyString:
        pass

    who = RenpyString()
    callback, pushed, state = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who=who,
        last_say_what="The cold is in her hands now.",
        characters={who: _FakeCharacter("ARIA>", kind="nvl")},
        string_types=(str, RenpyString),
        return_state=True,
    )

    callback("begin", True, what="The cold is in her hands now.")

    assert pushed == [{
        "type": "dialogue",
        "character": "ARIA>",
        "text": "The cold is in her hands now.",
        "mode": "nvl",
    }]
    assert state.nvl_occurrences == [(pushed[0], who)]


def test_adv_character_with_nvl_mode_does_not_claim_nvl_fallback_row():
    character = _FakeCharacter("ARIA>")
    character.mode = "nvl"
    callback, pushed, state = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who="aria",
        last_say_what="This is still an ADV-owned line.",
        characters={"aria": character},
        mode="nvl",
        return_state=True,
    )

    callback(
        "begin", True, type="say", what="This is still an ADV-owned line.")

    assert pushed == [{
        "type": "dialogue",
        "character": "ARIA>",
        "text": "This is still an ADV-owned line.",
        "mode": "adv",
    }]
    assert state.nvl_occurrences == []


def test_nvl_non_ascii_speaker_avoids_python2_str_conversion():
    class Py2UnicodeLike(str):
        def __str__(self):
            raise UnicodeEncodeError("ascii", "Ł", 0, 1, "non-ascii")

        def __format__(self, _spec):
            return "Łucja"

    speaker = Py2UnicodeLike("Łucja")
    callback, pushed = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who="speaker_nvl",
        last_say_what="Jestem tutaj.",
        characters={
            "speaker_nvl": _FakeCharacter(speaker, kind="nvl"),
        },
        mode="nvl",
    )

    callback("begin", True, what="Jestem tutaj.")

    assert pushed[0]["character"] == "Łucja"

    ns = load_shim_functions(
        "_vnf_stringify",
        "_vnf_nvl_speaker_key",
        "_vnf_nvl_event_key",
        namespace={
            "basestring": str,
            "renpy": types.SimpleNamespace(
                text=types.SimpleNamespace(
                    extras=types.SimpleNamespace(
                        filter_text_tags=lambda value, allow=None: value))),
        },
    )
    assert ns["_vnf_nvl_event_key"]({
        "character": speaker,
        "text": "Jestem tutaj.",
    }) == ("Łucja", "Jestem tutaj.")

    resolver = load_shim_functions(
        "_vnf_stringify",
        "_vnf_resolve_display_name_expr",
        namespace={
            "basestring": str,
            "renpy": types.SimpleNamespace(
                python=types.SimpleNamespace(py_eval=lambda raw: raw)),
        },
    )
    assert resolver["_vnf_resolve_display_name_expr"](speaker) == "Łucja"


def test_nvl_real_name_resolver_marks_only_failed_symbol_lookups_unknown():
    characters = {"aria_nvl": _FakeCharacter("ARIA>", kind="nvl")}

    def py_eval(expression):
        if expression not in characters:
            raise NameError(expression)
        return characters[expression]

    renpy = types.SimpleNamespace(
        python=types.SimpleNamespace(py_eval=py_eval),
        store=types.SimpleNamespace(),
        substitutions=types.SimpleNamespace(
            substitute=lambda raw: (raw, True)),
        text=types.SimpleNamespace(
            extras=types.SimpleNamespace(
                filter_text_tags=lambda value, allow=None: value)),
    )
    ns = load_shim_functions(
        "_vnf_clean_event_text",
        "_vnf_stringify",
        "_vnf_character_display_name_state",
        "_vnf_character_display_name",
        "_vnf_nvl_entry_event",
        namespace={
            "renpy": renpy,
            "basestring": str,
            "_vnf_substitute": lambda value: value,
            "_vnf_resolve_display_name_expr": lambda value: value,
        },
    )

    assert ns["_vnf_nvl_entry_event"](
        ("aria_nvl", "  Known.  ")) == {
            "type": "dialogue",
            "character": "ARIA>",
            "text": "Known.",
            "mode": "nvl",
        }
    assert ns["_vnf_nvl_entry_event"](
        ("legacy_nvl", "Unknown.")) == {
            "type": "dialogue",
            "character": "legacy_nvl",
            "text": "Unknown.",
            "mode": "nvl",
            "_vnf_speaker_unresolved": True,
            "_vnf_speaker_source": "legacy_nvl",
        }
    assert ns["_vnf_nvl_entry_event"]((None, "Narration.")) == {
        "type": "narration",
        "text": "Narration.",
        "mode": "nvl",
    }


def _nvl_occurrence_env():
    """Load the occurrence reconciler with Ren'Py-like name resolution."""
    pushed: list[dict] = []
    occurrences: list[tuple[str | None, str, str | None]] = []
    renpy = types.SimpleNamespace(
        text=types.SimpleNamespace(
            extras=types.SimpleNamespace(
                filter_text_tags=lambda value, allow=None: value)),
    )
    ns = load_shim_functions(
        "_vnf_nvl_speaker_key",
        "_vnf_nvl_event_key",
        "_vnf_note_nvl_callback_event",
        "_vnf_claim_nvl_callback_event",
        "_vnf_nvl_entry_event",
        "_vnf_publish_nvl_entry",
        namespace={
            "renpy": renpy,
            "basestring": str,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_clean_event_text": lambda value: str(value).strip(),
            "_vnf_stringify": lambda value: (
                None if value is None else str(value)),
            "_vnf_character_display_name_state": lambda who: (
                ("ARIA>", True) if who == "aria_nvl"
                else (getattr(who, "name", None), True)
                if who is not None else (None, True)),
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_client": types.SimpleNamespace(push_event=pushed.append),
        },
    )
    return ns, pushed, occurrences


def test_nvl_decorated_speaker_reconciles_through_real_resolver():
    occurrences = []
    pushed = []

    def py_eval(expression):
        if expression == "aria_nvl":
            return _FakeCharacter("ARIA>", kind="nvl")
        raise NameError(expression)

    renpy = types.SimpleNamespace(
        python=types.SimpleNamespace(py_eval=py_eval),
        store=types.SimpleNamespace(),
        substitutions=types.SimpleNamespace(
            substitute=lambda raw: (raw, True)),
        text=types.SimpleNamespace(
            extras=types.SimpleNamespace(
                filter_text_tags=lambda value, allow=None: re.sub(
                    r"\{[^}]*\}", "", value))),
    )
    ns = load_shim_functions(
        "_vnf_clean_event_text",
        "_vnf_stringify",
        "_vnf_character_display_name_state",
        "_vnf_nvl_speaker_key",
        "_vnf_nvl_event_key",
        "_vnf_note_nvl_callback_event",
        "_vnf_claim_nvl_callback_event",
        "_vnf_nvl_entry_event",
        "_vnf_publish_nvl_entry",
        namespace={
            "renpy": renpy,
            "basestring": str,
            "_vnf_substitute": lambda value: value,
            "_vnf_resolve_display_name_expr": lambda value: value,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_client": types.SimpleNamespace(push_event=pushed.append),
        },
    )
    callback_name, resolved = ns["_vnf_character_display_name_state"](
        "aria_nvl")
    assert resolved is True
    callback_event = {
        "type": "dialogue",
        "character": callback_name,
        "text": "Same occurrence.",
        "mode": "nvl",
    }
    ns["_vnf_note_nvl_callback_event"](callback_event, "aria_nvl")

    ns["_vnf_publish_nvl_entry"](
        ("{color=#0ff}[ARIA>]{/color}", "Same occurrence."), "watch")

    assert pushed == []
    assert occurrences == []


def test_nvl_parenthesized_speaker_remains_distinct():
    ns, pushed, occurrences = _nvl_occurrence_env()
    occurrences[:] = [("ARIA", "Same words.", None)]
    ns["_vnf_character_display_name_state"] = lambda who: (str(who), True)

    ns["_vnf_publish_nvl_entry"](("(ARIA)", "Same words."), "watch")

    assert pushed == [{
        "type": "dialogue",
        "character": "(ARIA)",
        "text": "Same words.",
        "mode": "nvl",
    }]
    assert occurrences == [("ARIA", "Same words.", None)]


def test_nvl_fallback_claims_callback_owned_occurrence_once():
    """The fleet-r17 immediate NVL clear must not publish the line twice."""
    ns, pushed, occurrences = _nvl_occurrence_env()
    callback_event = {
        "type": "dialogue",
        "character": "ARIA>",
        "text": "Core integrity 46%.",
        "mode": "nvl",
    }
    pushed.append(callback_event)
    ns["_vnf_note_nvl_callback_event"](callback_event)

    ns["_vnf_publish_nvl_entry"](
        ("aria_nvl", "  Core integrity 46%.  "), "pre-clear")

    assert pushed == [callback_event]
    assert occurrences == []


def test_nvl_callback_and_fallback_share_public_whitespace_contract():
    callback, callback_rows = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who="aria_nvl",
        last_say_what="  Same row.  ",
        characters={"aria_nvl": _FakeCharacter("ARIA>", kind="nvl")},
        mode="nvl",
    )
    callback("begin", True, what="  Same row.  ")

    ns, fallback_rows, _occurrences = _nvl_occurrence_env()
    ns["_vnf_publish_nvl_entry"](
        ("aria_nvl", "  Same row.  "), "watch")

    assert callback_rows[0]["text"] == "Same row."
    assert fallback_rows[0]["text"] == "Same row."


def test_nvl_menu_redisplay_is_owned_after_prior_fallback_flush():
    """A menu-context do_add must not replay a callback-deduped NVL line."""
    callback, callback_rows = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who="aria_nvl",
        last_say_what="The same question remains.",
        characters={"aria_nvl": _FakeCharacter("ARIA>", kind="nvl")},
        mode="nvl",
    )
    occurrence_ns, fallback_rows, occurrences = _nvl_occurrence_env()
    callback.__globals__["_vnf_note_nvl_callback_event"] = (
        occurrence_ns["_vnf_note_nvl_callback_event"])

    store = types.SimpleNamespace(
        nvl_list=[("aria_nvl", "The same question remains.")])
    last_len = [0]
    page_fp = [None]
    added = [1]
    tracked_entries = [["aria_nvl", "The same question remains."]]
    callback.__globals__["_vnf_nvl_added_since_watch"] = added
    occurrence_ns["_vnf_nvl_added_since_watch"] = added
    watcher_ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=None)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": added,
            "_vnf_nvl_entries_since_watch": tracked_entries,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns["_vnf_claim_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns["_vnf_publish_nvl_entry"],
        },
    )

    callback("begin", True, type="nvl", what="The same question remains.")
    watcher_ns["_vnf_flush_nvl_entries"]("first")
    assert len(callback_rows) == 1
    assert fallback_rows == []
    assert occurrences == []

    # Ren'Py calls NVLCharacter.do_add before the identical noninteractive
    # callback used to reconstruct menu context.
    store.nvl_list.append(("aria_nvl", "The same question remains."))
    added[0] += 1
    tracked_entries.append(["aria_nvl", "The same question remains."])
    callback("begin", False, type="nvl", what="The same question remains.")
    watcher_ns["_vnf_flush_nvl_entries"]("menu-context")

    assert len(callback_rows) == 1
    assert fallback_rows == []
    assert occurrences == []


def test_nvl_capped_batch_expires_evicted_redisplay_ownership():
    """An evicted redisplay marker must not swallow a later missed callback."""
    callback, callback_rows = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who="aria_nvl",
        last_say_what="Again.",
        characters={"aria_nvl": _FakeCharacter("ARIA>", kind="nvl")},
        mode="nvl",
    )
    occurrence_ns, fallback_rows, occurrences = _nvl_occurrence_env()
    callback.__globals__["_vnf_note_nvl_callback_event"] = (
        occurrence_ns["_vnf_note_nvl_callback_event"])

    store = types.SimpleNamespace(nvl_list=[("aria_nvl", "Again.")])
    last_len = [0]
    page_fp = [None]
    added = [1]
    tracked_entries = [["aria_nvl", "Again."]]
    callback.__globals__["_vnf_nvl_added_since_watch"] = added
    occurrence_ns["_vnf_nvl_added_since_watch"] = added
    watcher_ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=1)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": added,
            "_vnf_nvl_entries_since_watch": tracked_entries,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns["_vnf_claim_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns["_vnf_publish_nvl_entry"],
        },
    )

    callback("begin", True, type="nvl", what="Again.")
    watcher_ns["_vnf_flush_nvl_entries"]("first")

    # The menu redisplay is followed by a genuine identical occurrence before
    # the cap-one page is observed. Only the latter remains visible.
    added[0] += 1
    tracked_entries.append(["aria_nvl", "Again."])
    callback("begin", False, type="nvl", what="Again.")
    added[0] += 1
    tracked_entries.append(["aria_nvl", "Again."])
    callback("begin", True, type="nvl", what="Again.")
    watcher_ns["_vnf_flush_nvl_entries"]("batched")

    assert len(callback_rows) == 2
    assert fallback_rows == []
    assert occurrences == []

    # A later occurrence whose callback is genuinely missed must remain
    # visible through the fallback instead of being claimed by stale state.
    added[0] += 1
    tracked_entries.append(["aria_nvl", "Again."])
    watcher_ns["_vnf_flush_nvl_entries"]("missed-callback")

    assert [event["text"] for event in fallback_rows] == ["Again."]
    assert occurrences == []


def test_nvl_tracked_batch_preserves_repeated_extend_occurrences():
    """Collapsed extends use the exact do_add stream, not page arithmetic."""
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    original_rows = [(None, "A"), (None, "B"), (None, "C")]
    store = types.SimpleNamespace(nvl_list=original_rows)
    last_len = [3]
    page_fp = [None]
    added = [2]
    tracked_entries = [[None, "D"], [None, "E"]]
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=3)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": added,
            "_vnf_nvl_entries_since_watch": tracked_entries,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns["_vnf_claim_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns["_vnf_publish_nvl_entry"],
        },
    )
    page_fp[0] = ns["_vnf_nvl_fingerprint"](original_rows)

    callback_d = {"type": "narration", "text": "D", "mode": "nvl"}
    pushed.append(callback_d)
    occurrence_ns["_vnf_note_nvl_callback_event"](callback_d)
    store.nvl_list = [(None, "A"), (None, "B"), (None, "E")]
    ns["_vnf_flush_nvl_entries"]("repeated-extend")

    assert [event["text"] for event in pushed] == ["D", "E"]
    assert occurrences == []

    tracked_entries.append([None, "D"])
    added[0] += 1
    store.nvl_list = [(None, "A"), (None, "B"), (None, "D")]
    ns["_vnf_flush_nvl_entries"]("missed-repeat")

    assert [event["text"] for event in pushed] == ["D", "E", "D"]
    assert occurrences == []


def test_nvl_tracked_batch_merges_direct_list_appends_in_order():
    """Custom nvl_list writes around do_add must not be baselined away."""
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            # Real Ren'Py only prepares/evicts here. The row is not yet in
            # nvl_list when the shim's do_add wrapper returns. This override
            # also writes one auxiliary row inside the method.
            store.nvl_list.append((None, "Inside add override"))

        def do_done(self, who, what, multiple=None):
            store.nvl_list.append((who, what))
            store.nvl_list.append((None, "Inside done override"))

    store = types.SimpleNamespace(
        nvl_list=[(None, "Existing")], NVLCharacter=FakeNVLCharacter)
    last_len = [1]
    page_fp = [None]
    tracker_fp = [None]
    done_depth = [0]
    added = [0]
    tracked_entries = []
    pending_done = []
    done_in_progress = []
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=added,
        _vnf_nvl_entries_since_watch=tracked_entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_done_in_progress_entries=done_in_progress,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        "_vnf_install_nvl_done_tracker",
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_filter_pending_nvl_display_entries",
        "_vnf_filter_in_progress_nvl_done_entries",
        "_vnf_strip_active_nvl_display_entries",
        "_vnf_capture_untracked_nvl_delta",
        "_vnf_publish_and_queue_nvl_entry",
        "_vnf_merge_nvl_method_delta",
        "_vnf_claim_recorded_nvl_add",
        "_vnf_deactivate_recorded_nvl_display",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=None)),
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": list,
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_tracker_page_fp": tracker_fp,
            "_vnf_nvl_done_tracker_depth": done_depth,
            "_vnf_nvl_added_since_watch": added,
            "_vnf_nvl_entries_since_watch": tracked_entries,
            "_vnf_nvl_recorded_adds_pending_done": pending_done,
            "_vnf_nvl_done_in_progress_entries": done_in_progress,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns[
                "_vnf_claim_nvl_callback_event"],
            "_vnf_note_nvl_callback_event": occurrence_ns[
                "_vnf_note_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns[
                "_vnf_publish_nvl_entry"],
        },
    )
    initial_fp = ns["_vnf_nvl_fingerprint"](store.nvl_list)
    page_fp[0] = initial_fp
    tracker_fp[0] = initial_fp
    ns["_vnf_install_nvl_add_tracker"]()
    ns["_vnf_install_nvl_done_tracker"]()
    character = FakeNVLCharacter()

    # A custom screen writes directly before the next ordinary do_add. The
    # wrapper's pre-add capture publishes it before the callback-owned row.
    store.nvl_list.append((None, "Direct before"))
    character.do_add(None, "Tracked")

    callback_event = {
        "type": "narration", "text": "Tracked", "mode": "nvl",
    }
    pushed.append(callback_event)
    occurrence_ns["_vnf_note_nvl_callback_event"](callback_event)
    character.do_done(None, "Tracked")

    # A second direct write after do_add must join the same watcher batch.
    store.nvl_list.append((None, "Direct after"))
    ns["_vnf_flush_nvl_entries"]("watch")

    assert [event["text"] for event in pushed] == [
        "Direct before", "Inside add override", "Tracked",
        "Inside done override", "Direct after",
    ]
    assert occurrences == []
    assert tracked_entries == []


def test_nvl_interaction_flush_keeps_add_receipt_until_done():
    """A watcher inside do_display must not replay its temporary NVL row."""
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    clear_on_done = [False]
    append_after_clear = [False]
    clear_events = []

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return None

        def do_done(self, who, what, multiple=None):
            store.nvl_list.append((who, what))
            if clear_on_done[0]:
                ns["_vnf_flush_nvl_entries"]("pre-clear")
                ns["_vnf_reset_nvl_capture_state"]()
                store.nvl_list[:] = []
                clear_events.append("nvl_clear")
                if append_after_clear[0]:
                    store.nvl_list.append((who, what))

    store = types.SimpleNamespace(
        nvl_list=[], NVLCharacter=FakeNVLCharacter)
    added = [0]
    tracked_entries = []
    pending_done = []
    done_in_progress = []
    page_fp = [()]
    tracker_fp = [()]
    done_depth = [0]
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=added,
        _vnf_nvl_entries_since_watch=tracked_entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_done_in_progress_entries=done_in_progress,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        "_vnf_install_nvl_done_tracker",
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_filter_pending_nvl_display_entries",
        "_vnf_filter_in_progress_nvl_done_entries",
        "_vnf_strip_active_nvl_display_entries",
        "_vnf_capture_untracked_nvl_delta",
        "_vnf_publish_and_queue_nvl_entry",
        "_vnf_merge_nvl_method_delta",
        "_vnf_claim_recorded_nvl_add",
        "_vnf_deactivate_recorded_nvl_display",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        "_vnf_reset_nvl_capture_state",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=None)),
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": list,
            "_vnf_nvl_watch_last_len": [0],
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_tracker_page_fp": tracker_fp,
            "_vnf_nvl_done_tracker_depth": done_depth,
            "_vnf_nvl_added_since_watch": added,
            "_vnf_nvl_entries_since_watch": tracked_entries,
            "_vnf_nvl_recorded_adds_pending_done": pending_done,
            "_vnf_nvl_done_in_progress_entries": done_in_progress,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns[
                "_vnf_claim_nvl_callback_event"],
            "_vnf_note_nvl_callback_event": occurrence_ns[
                "_vnf_note_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns[
                "_vnf_publish_nvl_entry"],
        },
    )
    ns["_vnf_install_nvl_add_tracker"]()
    ns["_vnf_install_nvl_done_tracker"]()
    character = FakeNVLCharacter()

    character.do_add(None, "Tracked")
    callback_event = {
        "type": "narration", "text": "Tracked", "mode": "nvl",
    }
    pushed.append(callback_event)
    occurrence_ns["_vnf_note_nvl_callback_event"](callback_event)

    # Ren'Py's do_display temporarily appends the row and runs an interaction;
    # periodic callbacks can therefore flush before do_done persists it.
    store.nvl_list.append((None, "Tracked"))
    store.nvl_list.append((None, "Direct during display"))
    ns["_vnf_flush_nvl_entries"]("during-display")
    assert pending_done == [(id(character), None, "Tracked", True)]
    ns["_vnf_flush_nvl_entries"]("during-display-again")
    ns["_vnf_flush_nvl_entries"]("during-display-third")
    assert [event["text"] for event in pushed] == [
        "Tracked", "Direct during display",
    ]
    del store.nvl_list[0]

    character.do_done(None, "Tracked")
    ns["_vnf_flush_nvl_entries"]("after-done")

    assert [event["text"] for event in pushed] == [
        "Tracked", "Direct during display",
    ]
    assert store.nvl_list == [
        (None, "Direct during display"), (None, "Tracked"),
    ]
    assert pending_done == []
    assert tracked_entries == []
    assert occurrences == []

    # Repeat the same line while an identical persistent row remains. Entering
    # do_done proves which copy was temporary even though their keys match.
    character.do_add(None, "Tracked")
    repeated_callback = {
        "type": "narration", "text": "Tracked", "mode": "nvl",
    }
    pushed.append(repeated_callback)
    occurrence_ns["_vnf_note_nvl_callback_event"](repeated_callback)
    store.nvl_list.append((None, "Tracked"))
    ns["_vnf_flush_nvl_entries"]("repeat-during-display")
    ns["_vnf_flush_nvl_entries"]("repeat-during-display-again")
    store.nvl_list.pop()
    character.do_done(None, "Tracked")
    ns["_vnf_flush_nvl_entries"]("repeat-after-done")

    assert [event["text"] for event in pushed] == [
        "Tracked", "Direct during display", "Tracked",
    ]
    assert store.nvl_list == [
        (None, "Direct during display"),
        (None, "Tracked"),
        (None, "Tracked"),
    ]
    assert pending_done == []
    assert tracked_entries == []
    assert occurrences == []

    # A clear-on-say character nests the clear hook's pre-flush inside do_done.
    # A custom outer method then writes identical direct text on the new page;
    # ownership of the cleared occurrence must not consume that second row.
    clear_on_done[0] = True
    append_after_clear[0] = True
    character.do_add(None, "Clear line")
    clear_callback = {
        "type": "narration", "text": "Clear line", "mode": "nvl",
    }
    pushed.append(clear_callback)
    occurrence_ns["_vnf_note_nvl_callback_event"](clear_callback)
    store.nvl_list.append((None, "Clear line"))
    ns["_vnf_flush_nvl_entries"]("clear-during-display")
    ns["_vnf_flush_nvl_entries"]("clear-during-display-again")
    store.nvl_list.pop()
    character.do_done(None, "Clear line")
    ns["_vnf_flush_nvl_entries"]("after-clear")

    assert [event["text"] for event in pushed] == [
        "Tracked", "Direct during display", "Tracked",
        "Clear line", "Clear line",
    ]
    assert clear_events == ["nvl_clear"]
    assert store.nvl_list == [(None, "Clear line")]
    assert pending_done == []
    assert done_in_progress == []
    assert tracked_entries == []
    assert occurrences == []


def test_nvl_direct_append_at_window_limit_keeps_retained_prefix():
    ns = load_shim_functions("_vnf_nvl_delta_start")

    assert ns["_vnf_nvl_delta_start"](
        ((None, "A"), (None, "B"), (None, "C")),
        ((None, "A"), (None, "B"), (None, "C"), (None, "D")),
        delivered=3,
        window_limit=3,
        added=0,
    ) == 3


@pytest.mark.parametrize(
    ("engine_version", "events_after_add", "persistent_rows"),
    [
        ((6, 99, 12, 4), 0, 1),
        ((6, 99, 13), 1, 2),
        ((7, 5, 2), 1, 2),
    ],
)
def test_nvl_do_add_persistence_is_engine_era_aware(
        engine_version, events_after_add, persistent_rows):
    """Pre-6.99.13 owns its add row; later matching adds are custom."""
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            store.nvl_list.append((who, what))

        def do_done(self, who, what, multiple=None):
            if engine_version >= (6, 99, 13):
                store.nvl_list.append((who, what))

    store = types.SimpleNamespace(
        nvl_list=[], NVLCharacter=FakeNVLCharacter)
    added = [0]
    tracked_entries = []
    pending_done = []
    done_in_progress = []
    page_fp = [()]
    tracker_fp = [()]
    done_depth = [0]
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=added,
        _vnf_nvl_entries_since_watch=tracked_entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_done_in_progress_entries=done_in_progress,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        "_vnf_install_nvl_done_tracker",
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_filter_pending_nvl_display_entries",
        "_vnf_filter_in_progress_nvl_done_entries",
        "_vnf_strip_active_nvl_display_entries",
        "_vnf_capture_untracked_nvl_delta",
        "_vnf_publish_and_queue_nvl_entry",
        "_vnf_merge_nvl_method_delta",
        "_vnf_claim_recorded_nvl_add",
        "_vnf_deactivate_recorded_nvl_display",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=None)),
            "vnf_player": types.SimpleNamespace(enabled=True),
            "renpy_version": engine_version,
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": list,
            "_vnf_nvl_watch_last_len": [0],
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_tracker_page_fp": tracker_fp,
            "_vnf_nvl_done_tracker_depth": done_depth,
            "_vnf_nvl_added_since_watch": added,
            "_vnf_nvl_entries_since_watch": tracked_entries,
            "_vnf_nvl_recorded_adds_pending_done": pending_done,
            "_vnf_nvl_done_in_progress_entries": done_in_progress,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns[
                "_vnf_claim_nvl_callback_event"],
            "_vnf_note_nvl_callback_event": occurrence_ns[
                "_vnf_note_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns[
                "_vnf_publish_nvl_entry"],
        },
    )
    ns["_vnf_install_nvl_add_tracker"]()
    ns["_vnf_install_nvl_done_tracker"]()
    character = FakeNVLCharacter()

    character.do_add(None, "Six-era row")
    # A pre-flip persisted say waits for callback ownership. The same append
    # on modern engines is a custom extra and publishes immediately.
    assert len(pushed) == events_after_add
    callback_event = {
        "type": "narration", "text": "Six-era row", "mode": "nvl",
    }
    pushed.append(callback_event)
    occurrence_ns["_vnf_note_nvl_callback_event"](callback_event)
    ns["_vnf_flush_nvl_entries"]("6x-during-display")
    character.do_done(None, "Six-era row")
    ns["_vnf_flush_nvl_entries"]("6x-after-done")

    assert [event["text"] for event in pushed] == [
        "Six-era row",
    ] * (events_after_add + 1)
    assert store.nvl_list == [
        (None, "Six-era row"),
    ] * persistent_rows
    assert pending_done == []
    assert done_in_progress == []
    assert tracked_entries == []
    assert occurrences == []


def test_nvl_capped_do_add_eviction_does_not_replay_retained_tail():
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            while len(store.nvl_list) + 1 > 3:
                store.nvl_list.pop(0)

        def do_done(self, who, what, multiple=None):
            store.nvl_list.append((who, what))

    store = types.SimpleNamespace(
        nvl_list=[(None, "A"), (None, "B"), (None, "C")],
        NVLCharacter=FakeNVLCharacter,
    )
    added = [0]
    tracked_entries = []
    pending_done = []
    done_in_progress = []
    page_fp = [None]
    tracker_fp = [None]
    done_depth = [0]
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=added,
        _vnf_nvl_entries_since_watch=tracked_entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_done_in_progress_entries=done_in_progress,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        "_vnf_install_nvl_done_tracker",
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_filter_pending_nvl_display_entries",
        "_vnf_filter_in_progress_nvl_done_entries",
        "_vnf_strip_active_nvl_display_entries",
        "_vnf_capture_untracked_nvl_delta",
        "_vnf_publish_and_queue_nvl_entry",
        "_vnf_merge_nvl_method_delta",
        "_vnf_claim_recorded_nvl_add",
        "_vnf_deactivate_recorded_nvl_display",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=3)),
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": list,
            "_vnf_nvl_watch_last_len": [3],
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_tracker_page_fp": tracker_fp,
            "_vnf_nvl_done_tracker_depth": done_depth,
            "_vnf_nvl_added_since_watch": added,
            "_vnf_nvl_entries_since_watch": tracked_entries,
            "_vnf_nvl_recorded_adds_pending_done": pending_done,
            "_vnf_nvl_done_in_progress_entries": done_in_progress,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns[
                "_vnf_claim_nvl_callback_event"],
            "_vnf_note_nvl_callback_event": occurrence_ns[
                "_vnf_note_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns[
                "_vnf_publish_nvl_entry"],
        },
    )
    initial_fp = ns["_vnf_nvl_fingerprint"](store.nvl_list)
    page_fp[0] = initial_fp
    tracker_fp[0] = initial_fp
    ns["_vnf_install_nvl_add_tracker"]()
    ns["_vnf_install_nvl_done_tracker"]()

    character = FakeNVLCharacter()
    character.do_add(None, "D")
    callback_event = {"type": "narration", "text": "D", "mode": "nvl"}
    pushed.append(callback_event)
    occurrence_ns["_vnf_note_nvl_callback_event"](callback_event)
    character.do_done(None, "D")
    ns["_vnf_flush_nvl_entries"]("watch")

    assert [event["text"] for event in pushed] == ["D"]
    assert store.nvl_list == [(None, "B"), (None, "C"), (None, "D")]
    assert occurrences == []


def test_nvl_done_falls_back_when_add_override_bypasses_tracker():
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return None

        def do_done(self, who, what, multiple=None):
            store.nvl_list.append((who, what))

    store = types.SimpleNamespace(
        nvl_list=[], NVLCharacter=FakeNVLCharacter)
    added = [0]
    tracked_entries = []
    pending_done = []
    done_in_progress = []
    page_fp = [()]
    tracker_fp = [()]
    done_depth = [0]
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=added,
        _vnf_nvl_entries_since_watch=tracked_entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_done_in_progress_entries=done_in_progress,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        "_vnf_install_nvl_done_tracker",
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_filter_pending_nvl_display_entries",
        "_vnf_filter_in_progress_nvl_done_entries",
        "_vnf_capture_untracked_nvl_delta",
        "_vnf_publish_and_queue_nvl_entry",
        "_vnf_merge_nvl_method_delta",
        "_vnf_claim_recorded_nvl_add",
        "_vnf_deactivate_recorded_nvl_display",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=None)),
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": list,
            "_vnf_nvl_watch_last_len": [0],
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_tracker_page_fp": tracker_fp,
            "_vnf_nvl_done_tracker_depth": done_depth,
            "_vnf_nvl_added_since_watch": added,
            "_vnf_nvl_entries_since_watch": tracked_entries,
            "_vnf_nvl_recorded_adds_pending_done": pending_done,
            "_vnf_nvl_done_in_progress_entries": done_in_progress,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns[
                "_vnf_claim_nvl_callback_event"],
            "_vnf_note_nvl_callback_event": occurrence_ns[
                "_vnf_note_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns[
                "_vnf_publish_nvl_entry"],
        },
    )
    ns["_vnf_install_nvl_add_tracker"]()
    ns["_vnf_install_nvl_done_tracker"]()

    # A mod replaces do_add without delegating after installation, while the
    # inherited do_done tracker remains active. With no callback, the done
    # method must preserve the row through the fallback path.
    FakeNVLCharacter.do_add = lambda self, *args, **kwargs: None
    character = FakeNVLCharacter()
    character.do_add(None, "Fallback row")
    character.do_done(None, "Fallback row")
    ns["_vnf_flush_nvl_entries"]("watch")

    assert [event["text"] for event in pushed] == ["Fallback row"]
    assert tracked_entries == []
    assert pending_done == []
    assert occurrences == []


def test_nvl_exact_batch_keeps_callback_ownership_beyond_128_rows():
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    tracked_entries = []
    for index in range(129):
        text = "Row %d" % index
        event = {"type": "narration", "text": text, "mode": "nvl"}
        pushed.append(event)
        occurrence_ns["_vnf_note_nvl_callback_event"](event)
        tracked_entries.append([None, text])

    store = types.SimpleNamespace(nvl_list=[(None, "Row 128")])
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=1)),
            "_vnf_nvl_watch_last_len": [0],
            "_vnf_nvl_watch_first_fp": [None],
            "_vnf_nvl_added_since_watch": [129],
            "_vnf_nvl_entries_since_watch": tracked_entries,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns["_vnf_claim_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns["_vnf_publish_nvl_entry"],
        },
    )

    ns["_vnf_flush_nvl_entries"]("large-batch")

    assert len(pushed) == 129
    assert occurrences == []
    assert tracked_entries == []


def test_nvl_no_tracker_replay_retires_evicted_ownership():
    """Conservative page replay cannot leave legacy markers for later rows."""
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    original_rows = [(None, "A")]
    store = types.SimpleNamespace(nvl_list=original_rows)
    last_len = [1]
    page_fp = [None]
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=1)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": [0],
            "_vnf_nvl_entries_since_watch": [],
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns["_vnf_claim_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns["_vnf_publish_nvl_entry"],
        },
    )
    page_fp[0] = ns["_vnf_nvl_fingerprint"](original_rows)

    redisplay_a = {"type": "narration", "text": "A", "mode": "nvl"}
    occurrence_ns["_vnf_note_nvl_callback_event"](redisplay_a)
    store.nvl_list = [(None, "B")]
    ns["_vnf_flush_nvl_entries"]("no-tracker-b")
    store.nvl_list = [(None, "A")]
    ns["_vnf_flush_nvl_entries"]("no-tracker-a")

    assert [event["text"] for event in pushed] == ["B", "A"]
    assert occurrences == []


def test_nvl_fallback_preserves_missed_and_repeated_occurrences():
    ns, pushed, _occurrences = _nvl_occurrence_env()
    callback_event = {
        "type": "narration", "text": "Again.", "mode": "nvl",
    }
    ns["_vnf_note_nvl_callback_event"](callback_event)

    ns["_vnf_publish_nvl_entry"]((None, "Again."), "pre-clear")
    ns["_vnf_publish_nvl_entry"]((None, "Again."), "pre-clear")
    ns["_vnf_publish_nvl_entry"]((None, "Missed."), "pre-clear")

    assert [event["text"] for event in pushed] == ["Again.", "Missed."]


def test_nvl_stale_occurrence_does_not_block_a_later_match():
    ns, pushed, occurrences = _nvl_occurrence_env()
    occurrences[:] = [
        ("STALE>", "An abandoned timeline.", None),
        ("ARIA>", "The current line.", None),
    ]

    ns["_vnf_publish_nvl_entry"](
        ("aria_nvl", "The current line."), "pre-clear")
    ns["_vnf_publish_nvl_entry"](
        ("STALE>", "An abandoned timeline."), "pre-clear")

    assert [event["text"] for event in pushed] == ["An abandoned timeline."]
    assert occurrences == []


def test_nvl_narration_does_not_claim_same_text_from_named_speaker():
    ns, pushed, occurrences = _nvl_occurrence_env()
    occurrences[:] = [("ARIA>", "Again.", None)]

    ns["_vnf_publish_nvl_entry"]((None, "Again."), "watch")
    ns["_vnf_publish_nvl_entry"](("aria_nvl", "Again."), "watch")

    assert pushed == [{
        "type": "narration",
        "text": "Again.",
        "mode": "nvl",
    }]
    assert occurrences == []


def test_nvl_unresolved_speaker_can_claim_named_callback_occurrence():
    ns, pushed, occurrences = _nvl_occurrence_env()
    occurrences[:] = [("ARIA>", "Legacy row.", "aria_nvl")]
    ns["_vnf_character_display_name_state"] = lambda who: (str(who), False)

    ns["_vnf_publish_nvl_entry"](("aria_nvl", "Legacy row."), "watch")

    assert pushed == []
    assert occurrences == []


def test_nvl_unresolved_speaker_without_matching_source_stays_visible():
    ns, pushed, occurrences = _nvl_occurrence_env()
    occurrences[:] = [("OTHER>", "Legacy row.", "other_nvl")]
    ns["_vnf_character_display_name_state"] = lambda who: (str(who), False)

    ns["_vnf_publish_nvl_entry"](("aria_nvl", "Legacy row."), "watch")

    assert pushed == [{
        "type": "dialogue",
        "character": "aria_nvl",
        "text": "Legacy row.",
        "mode": "nvl",
    }]
    assert occurrences == [("OTHER>", "Legacy row.", "other_nvl")]


def test_nvl_occurrence_ledger_preserves_large_unflushed_batch():
    ns, pushed, occurrences = _nvl_occurrence_env()
    for index in range(130):
        ns["_vnf_note_nvl_callback_event"]({
            "type": "narration",
            "text": "Row %d" % index,
            "mode": "nvl",
        })

    assert len(occurrences) == 130

    for index in range(130):
        ns["_vnf_publish_nvl_entry"](
            (None, "Row %d" % index), "pre-clear")

    assert pushed == []
    assert occurrences == []


def test_nvl_watcher_reconciles_each_row_instead_of_using_global_say_flag():
    watcher = function_node(parse_shim_python(), "_vnf_periodic_nvl_watch")
    watcher_source = ast.unparse(watcher)

    assert "_vnf_flush_nvl_entries('watch')" in watcher_source
    assert "if _vnf_last_what is not None" not in watcher_source


def test_disabled_nvl_watcher_baselines_pending_state():
    baselines = []
    ns = load_shim_functions(
        "_vnf_periodic_nvl_watch",
        namespace={
            "vnf_player": types.SimpleNamespace(enabled=False),
            "_vnf_baseline_nvl_capture_state": lambda: baselines.append(True),
        },
    )

    ns["_vnf_periodic_nvl_watch"]()

    assert baselines == [True]


def test_nvl_hide_flushes_rows_before_publishing_the_lifecycle_event():
    clear_wrapper = function_node(
        parse_shim_python(), "_vnf_nvl_clear_wrapper")
    hide_wrapper = function_node(parse_shim_python(), "_vnf_nvl_hide_wrapper")
    clear_source = ast.unparse(clear_wrapper)
    source = ast.unparse(hide_wrapper)

    assert clear_source.index("_vnf_observe_rollback_resume()") < (
        clear_source.index("_vnf_flush_nvl_entries('pre-clear', True)"))
    assert source.index("_vnf_observe_rollback_resume()") < source.index(
        "_vnf_flush_nvl_entries('pre-hide', True)")
    assert source.index(
        "_vnf_flush_nvl_entries('pre-hide', True)") < source.index(
        "_vnf_client.push_event")
    assert source.index("_vnf_nvl_callback_occurrences[:] = []") < source.index(
        "_vnf_client.push_event")


def test_nvl_clear_observes_rollback_before_forced_page_flush():
    lifecycle = []
    ns = load_shim_functions(
        "_vnf_nvl_clear_wrapper",
        namespace={
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_observe_rollback_resume": (
                lambda: lifecycle.append("rollback-baseline")),
            "_vnf_flush_nvl_entries": (
                lambda source, force: lifecycle.append((source, force))),
            "_vnf_reset_nvl_capture_state": (
                lambda: lifecycle.append("reset")),
            "_vnf_client": types.SimpleNamespace(
                push_event=lambda event: lifecycle.append(event["type"])),
            "_vnf_log": lambda _message: None,
            "_vnf_original_nvl_clear": (
                lambda: lifecycle.append("original")),
        },
    )

    ns["_vnf_nvl_clear_wrapper"]()

    assert lifecycle == [
        "rollback-baseline",
        ("pre-clear", True),
        "reset",
        "nvl_clear",
        "original",
    ]


def test_nvl_capture_resets_on_discontinuous_timelines():
    tree = parse_shim_python()
    after_load = ast.unparse(function_node(tree, "_vnf_after_load_callback"))
    finish_rollback = ast.unparse(function_node(
        tree, "_vnf_finish_rollback_resume"))
    rollback_edge_observer = ast.unparse(function_node(
        tree, "_vnf_observe_rollback_resume"))
    rollback_interact_callback = ast.unparse(function_node(
        tree, "_vnf_rollback_resume_interact_callback"))

    assert "_vnf_baseline_nvl_capture_state()" in after_load
    assert "_vnf_baseline_nvl_capture_state()" in finish_rollback
    assert "_vnf_finish_rollback_resume()" in rollback_edge_observer
    assert "_vnf_observe_rollback_resume()" in rollback_interact_callback

    start_source = ast.unparse(function_node(tree, "_vnf_start_callback"))
    assert "_vnf_reset_nvl_capture_state()" in start_source


@pytest.mark.parametrize(
    ("configured_name", "expected_name"),
    [
        ("Café Étoilé", "Café Étoilé"),
        (type("Unprintable", (), {
            "__str__": lambda self: (_ for _ in ()).throw(
                UnicodeEncodeError("ascii", "x", 0, 1, "test")),
        })(), "Unknown"),
    ],
)
def test_start_callback_survives_unicode_and_unprintable_titles(
    configured_name,
    expected_name,
):
    pushed = []
    logs = []
    prepublished = [("stale",)]
    renpy = types.SimpleNamespace(
        config=types.SimpleNamespace(name=configured_name),
        substitutions=types.SimpleNamespace(
            substitute=lambda value: (value, False)),
    )
    label_history = [("ending_fail1", 1.0)]
    ns = load_shim_functions(
        "_vnf_start_callback",
        namespace={
            "renpy": renpy,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_commit_pending_menu_caption": lambda: None,
            "_vnf_reset_nvl_capture_state": lambda: None,
            "_vnf_nvl_prepublished_callbacks": prepublished,
            "_vnf_label_history": label_history,
            "_vnf_reset_overlay_instance_generations": lambda: None,
            "_vnf_client": types.SimpleNamespace(
                push_event_sync=lambda event: pushed.append(event)),
            "_vnf_log": logs.append,
        },
    )

    ns["_vnf_start_callback"]()

    assert pushed == [{
        "type": "game_started",
        "game_name": expected_name,
        "version": "",
    }]
    assert logs == ["Game started: " + expected_name]
    assert prepublished == []
    # A new run has visited no labels (frozen footer, rw70-sonnet).
    assert label_history == []


def test_renpy6_rollback_wrapper_marks_successful_restart():
    class RestartContext(BaseException):
        pass

    pending = [False]
    game = types.SimpleNamespace(after_rollback=False)

    def successful_rollback():
        game.after_rollback = True
        raise RestartContext()

    ns = load_shim_functions(
        "_vnf_rollback_wrapper",
        namespace={
            "_vnf_original_rollback": successful_rollback,
            "_vnf_rollback_pending": pending,
            "_sys_mod": types.SimpleNamespace(
                _vnf_rollback_pending=pending),
            "_is_renpy6": True,
            "renpy": types.SimpleNamespace(game=game),
        },
    )

    with pytest.raises(RestartContext):
        ns["_vnf_rollback_wrapper"]()

    assert pending == [True]

    source = SHIM.read_text(encoding="utf-8")
    assert "_vnf_rollback_pending = [False]" not in source
    assert "_VNF_NATIVE_LIST_TYPE((False,))" in source
    assert "_sys_mod._vnf_rollback_pending[0] = True" in source
    assert "_sys_mod._vnf_rollback_pending[0] = False" in source


def test_renpy6_rollback_wrapper_ignores_refused_rollback():
    pending = [False]
    ns = load_shim_functions(
        "_vnf_rollback_wrapper",
        namespace={
            "_vnf_original_rollback": lambda: None,
            "_vnf_rollback_pending": pending,
            "_is_renpy6": True,
            "renpy": types.SimpleNamespace(
                game=types.SimpleNamespace(after_rollback=False)),
        },
    )

    assert ns["_vnf_rollback_wrapper"]() is None
    assert pending == [False]


def test_renpy6_rollback_keymap_patch_replaces_previous_shim_wrapper():
    previous_wrapper = lambda: None
    previous_wrapper._vnf_owned_rollback_wrapper = True
    keymap = types.SimpleNamespace(keymap={"rollback": previous_wrapper})
    renpy = types.SimpleNamespace(
        config=types.SimpleNamespace(underlay=[keymap]))
    ns = load_shim_functions(
        "_vnf_rollback_wrapper",
        "_vnf_patch_renpy6_rollback_keymaps",
        namespace={
            "_vnf_original_rollback": lambda: None,
            "_vnf_previous_rollback_wrapper": previous_wrapper,
            "_vnf_rollback_pending": [False],
            "_is_renpy6": True,
            "renpy": renpy,
        },
    )

    ns["_vnf_patch_renpy6_rollback_keymaps"]()

    assert keymap.keymap["rollback"] is ns["_vnf_rollback_wrapper"]


def test_renpy6_periodic_and_story_observers_share_resume_edge():
    lifecycle = []
    flushes = []
    pending = [True]
    after_rollback = types.SimpleNamespace(after_rollback=True)
    ns = load_shim_functions(
        "_vnf_periodic_nvl_watch",
        "_vnf_observe_rollback_resume",
        namespace={
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_is_renpy6": True,
            "_sys_mod": types.SimpleNamespace(
                _vnf_rollback_pending=pending),
            "_vnf_finish_rollback_resume": (
                lambda: lifecycle.append("game_resumed")),
            "_vnf_rollback_resume_seen": [False],
            "_vnf_baseline_nvl_capture_state": lambda: None,
            "_vnf_flush_nvl_entries": flushes.append,
            "renpy": types.SimpleNamespace(game=after_rollback),
        },
    )

    ns["_vnf_periodic_nvl_watch"]()
    ns["_vnf_observe_rollback_resume"]()

    assert lifecycle == ["game_resumed"]
    assert flushes == ["watch"]
    assert pending == [False]
    assert ns["_vnf_rollback_resume_seen"] == [True]


def test_renpy6_story_entry_observer_consumes_rollback_latch():
    lifecycle = []
    pending = [True]
    after_rollback = types.SimpleNamespace(after_rollback=True)
    ns = load_shim_functions(
        "_vnf_observe_rollback_resume",
        namespace={
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_is_renpy6": True,
            "_sys_mod": types.SimpleNamespace(
                _vnf_rollback_pending=pending),
            "_vnf_finish_rollback_resume": (
                lambda: lifecycle.append("game_resumed")),
            "_vnf_rollback_resume_seen": [False],
            "renpy": types.SimpleNamespace(game=after_rollback),
        },
    )

    ns["_vnf_observe_rollback_resume"]()
    ns["_vnf_observe_rollback_resume"]()

    assert lifecycle == ["game_resumed"]
    assert pending == [False]
    assert ns["_vnf_rollback_resume_seen"] == [True]

    after_rollback.after_rollback = False
    ns["_vnf_observe_rollback_resume"]()
    assert ns["_vnf_rollback_resume_seen"] == [False]

    pending[0] = True
    after_rollback.after_rollback = True
    ns["_vnf_observe_rollback_resume"]()
    ns["_vnf_observe_rollback_resume"]()
    assert lifecycle == ["game_resumed", "game_resumed"]


def _after_load_observer_env(lifecycle, *, renpy6, latch_pending,
                             after_rollback):
    return load_shim_functions(
        "_vnf_after_load_callback",
        "_vnf_observe_rollback_resume",
        namespace={
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_is_renpy6": renpy6,
            "_vnf_mouse": types.SimpleNamespace(sync=lambda: None),
            "_vnf_commit_pending_menu_caption": lambda: None,
            "_vnf_baseline_nvl_capture_state": lambda: None,
            "_vnf_reset_overlay_instance_generations": lambda: None,
            "_vnf_log": lambda *args, **kwargs: None,
            "_vnf_client": types.SimpleNamespace(
                push_event_sync=lambda event: lifecycle.append(
                    event["type"] + ":" + event["reason"]),
                push_event=lambda event: None),
            "_time": types.SimpleNamespace(time=lambda: 0.0),
            "_vnf_last_visible_scrape_hash": None,
            "_vnf_last_scrape_text_pairs": [],
            "_vnf_get_inventory_stats": lambda: ({}, {}),
            "_vnf_finish_rollback_resume": (
                lambda: lifecycle.append("game_resumed:rollback")),
            "_vnf_rollback_resume_seen": [False],
            "_sys_mod": types.SimpleNamespace(
                _vnf_rollback_pending=[latch_pending]),
            "renpy": types.SimpleNamespace(game=after_rollback),
        },
    )


def test_after_load_resume_suppresses_spurious_rollback_observation():
    """A load's own after_rollback flag must not add a second game_resumed."""
    lifecycle = []
    after_rollback = types.SimpleNamespace(after_rollback=True)
    ns = _after_load_observer_env(
        lifecycle, renpy6=False, latch_pending=False,
        after_rollback=after_rollback)

    ns["_vnf_after_load_callback"]()
    # The restored statement re-executes with the rollback flag still set.
    ns["_vnf_observe_rollback_resume"]()
    ns["_vnf_observe_rollback_resume"]()

    assert lifecycle == ["game_resumed:load"]
    assert ns["_vnf_rollback_resume_seen"] == [True]


    # The first post-load interaction clears the flag and re-arms the edge,
    # so a genuine rollback afterwards is still observed exactly once.
    after_rollback.after_rollback = False
    ns["_vnf_observe_rollback_resume"]()
    assert ns["_vnf_rollback_resume_seen"] == [False]
    after_rollback.after_rollback = True
    ns["_vnf_observe_rollback_resume"]()
    ns["_vnf_observe_rollback_resume"]()
    assert lifecycle == ["game_resumed:load", "game_resumed:rollback"]


@pytest.mark.parametrize("enabled", [False, True])
def test_after_load_reengages_only_configured_auto_advance(enabled):
    lifecycle = []
    ns = _after_load_observer_env(
        lifecycle, renpy6=False, latch_pending=False,
        after_rollback=types.SimpleNamespace(after_rollback=True))
    ns["vnf_player"].auto_advance = enabled
    calls = []
    ns["_vnf_enable_auto_advance"] = lambda: calls.append("enabled")
    ns["_vnf_disable_auto_advance"] = lambda: calls.append("disabled")
    ns["_vnf_after_load_callback"]()
    assert calls == (["enabled"] if enabled else ["disabled"])
    assert lifecycle == ["game_resumed:load"]


def test_after_load_resume_consumes_renpy6_rollback_latch():
    """On Ren'Py 6 the load's internal rollback latch is retired by the load."""
    lifecycle = []
    after_rollback = types.SimpleNamespace(after_rollback=True)
    ns = _after_load_observer_env(
        lifecycle, renpy6=True, latch_pending=True,
        after_rollback=after_rollback)

    ns["_vnf_after_load_callback"]()
    ns["_vnf_observe_rollback_resume"]()

    assert lifecycle == ["game_resumed:load"]
    assert ns["_sys_mod"]._vnf_rollback_pending == [False]
    assert ns["_vnf_rollback_resume_seen"] == [True]


def test_modern_watcher_baselines_rollback_before_flushing_restored_rows():
    restored_rows = [
        ("SYSTEM", "Completion header."),
        ("ARIA>", "Completion detail."),
        ("ARIA>", "Final completion line."),
    ]
    published = []
    lifecycle = []
    renpy = types.SimpleNamespace(
        game=types.SimpleNamespace(after_rollback=True),
        store=types.SimpleNamespace(nvl_list=restored_rows),
        text=types.SimpleNamespace(extras=types.SimpleNamespace(
            filter_text_tags=lambda value, allow: value)),
    )
    ns = load_shim_functions(
        "_vnf_nvl_speaker_key",
        "_vnf_nvl_event_key",
        "_vnf_note_nvl_callback_event",
        "_vnf_claim_nvl_callback_event",
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        "_vnf_baseline_nvl_capture_state",
        "_vnf_finish_rollback_resume",
        "_vnf_observe_rollback_resume",
        "_vnf_periodic_nvl_watch",
        namespace={
            "renpy": renpy,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_is_renpy6": False,
            "_vnf_rollback_resume_seen": [False],
            "_vnf_nvl_watch_last_len": [0],
            "_vnf_nvl_watch_first_fp": [None],
            "_vnf_nvl_tracker_page_fp": [None],
            "_vnf_nvl_added_since_watch": [0],
            "_vnf_nvl_callback_occurrences": [],
            "_vnf_nvl_prepublished_callbacks": [],
            "_vnf_nvl_entries_since_watch": [],
            "_vnf_nvl_recorded_adds_pending_done": [],
            "_vnf_stringify": lambda value: str(value),
            "basestring": str,
            "_vnf_nvl_event_key": lambda event: (
                event.get("character"), event.get("text", "")),
            "_vnf_nvl_entry_event": lambda entry: {
                "type": "dialogue" if entry[0] else "narration",
                "character": entry[0],
                "text": entry[1],
                "mode": "nvl",
            },
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": lambda entry, source: None,
            "_vnf_mouse": types.SimpleNamespace(sync=lambda: None),
            "_vnf_commit_pending_menu_caption": lambda: None,
            "_vnf_reset_overlay_instance_generations": lambda: None,
            "_vnf_client": types.SimpleNamespace(
                push_event_sync=lambda event: lifecycle.append(event)),
            "_time": types.SimpleNamespace(time=lambda: 123.0),
        },
    )

    ns["_vnf_periodic_nvl_watch"]()

    assert published == []
    assert [event["type"] for event in lifecycle] == ["game_resumed"]

    ns["_vnf_periodic_nvl_watch"]()
    assert published == []
    assert len(lifecycle) == 1

    current_row = ("ARIA>", "Current restored interaction.")
    restored_rows.append(current_row)
    ns["_vnf_nvl_entries_since_watch"].append(current_row)
    ns["_vnf_periodic_nvl_watch"]()
    assert ns["_vnf_nvl_entries_since_watch"] == [current_row]

    current_event = {
        "type": "dialogue", "character": "ARIA>",
        "text": "Current restored interaction.", "mode": "nvl",
    }
    published.append((current_row, "callback"))
    ns["_vnf_note_nvl_callback_event"](current_event, "ARIA>")

    def publish_unowned(entry, source):
        event = {
            "type": "dialogue", "character": entry[0],
            "text": entry[1], "mode": "nvl",
        }
        if not ns["_vnf_claim_nvl_callback_event"](event):
            published.append((entry, source))

    ns["_vnf_publish_nvl_entry"] = publish_unowned

    renpy.game.after_rollback = False
    fresh_row = ("ARIA>", "Fresh after rollback.")
    restored_rows.append(fresh_row)
    ns["_vnf_nvl_entries_since_watch"].append(fresh_row)
    ns["_vnf_periodic_nvl_watch"]()

    assert published == [
        (("ARIA>", "Current restored interaction."), "callback"),
        (("ARIA>", "Fresh after rollback."), "watch"),
    ]


def test_nvl_resume_baselines_restored_rows_before_watching():
    restored_rows = [(None, "Already shown.")]
    published = []
    last_len = [0]
    first_fp = [None]
    occurrences = [(None, "Abandoned callback marker.", None)]
    prepublished = [("abandoned inverse marker",)]
    renpy = types.SimpleNamespace(
        store=types.SimpleNamespace(nvl_list=restored_rows))
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        "_vnf_baseline_nvl_capture_state",
        namespace={
            "renpy": renpy,
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": first_fp,
            "_vnf_nvl_added_since_watch": [4],
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_prepublished_callbacks": prepublished,
            "_vnf_nvl_event_key": lambda event: (None, event.get("text", "")),
            "_vnf_nvl_entry_event": lambda entry: None,
            "_vnf_claim_nvl_callback_event": lambda event: False,
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": (
                lambda entry, source: published.append((entry, source))),
        },
    )

    ns["_vnf_baseline_nvl_capture_state"]()
    ns["_vnf_flush_nvl_entries"]("watch")

    assert published == []
    assert occurrences == []
    assert prepublished == []
    assert ns["_vnf_nvl_added_since_watch"] == [0]

    restored_rows.append((None, "New after resume."))
    ns["_vnf_flush_nvl_entries"]("watch")

    assert published == [((None, "New after resume."), "watch")]


def test_modern_visible_scrape_observes_rollback_before_screen_push():
    calls = []
    ns = load_shim_functions(
        "_vnf_visible_scrape_interact_callback",
        namespace={
            "_vnf_observe_rollback_resume": (
                lambda: calls.append("resume")),
            "_vnf_scrape_visible_screens": (
                lambda: calls.append("screen")),
            "vnf_player": types.SimpleNamespace(debug=False),
        },
    )

    ns["_vnf_visible_scrape_interact_callback"]()

    assert calls == ["resume", "screen"]


def test_nvl_watcher_does_not_replay_reconstructed_equivalent_page():
    original_rows = [
        ("SYSTEM", "Retained header."),
        ("ARIA>", "Already shown."),
    ]
    published = []
    last_len = [2]
    first_fp = [None]
    occurrences = []
    store = types.SimpleNamespace(nvl_list=original_rows)
    renpy = types.SimpleNamespace(store=store)
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        "_vnf_reset_nvl_capture_state",
        namespace={
            "renpy": renpy,
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": first_fp,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": lambda entry: None,
            "_vnf_claim_nvl_callback_event": lambda event: False,
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": (
                lambda entry, source: published.append((entry, source))),
        },
    )
    first_fp[0] = ns["_vnf_nvl_fingerprint"](original_rows)

    # A fresh tuple and list represent the same retained NVL page.
    store.nvl_list = [
        ("SYSTEM", "Retained header."),
        ("ARIA>", "Already shown."),
    ]
    ns["_vnf_flush_nvl_entries"]("watch")

    assert published == []
    assert last_len == [2]

    # Ren'Py's NVL extend replaces a row in place. The unchanged prefix must
    # not replay; only the replacement row is offered to the fallback path.
    store.nvl_list = [
        ("SYSTEM", "Retained header."),
        ("ARIA>", "A new page."),
    ]
    ns["_vnf_flush_nvl_entries"]("watch")

    assert published == [(("ARIA>", "A new page."), "watch")]

    # An explicitly observed clear is the generation signal that makes an
    # exactly identical fresh page distinguishable from reconstruction.
    published[:] = []
    ns["_vnf_reset_nvl_capture_state"]()
    ns["_vnf_flush_nvl_entries"]("watch")

    assert published == [
        (("SYSTEM", "Retained header."), "watch"),
        (("ARIA>", "A new page."), "watch"),
    ]


def test_nvl_watcher_extend_preserves_prefix_and_claims_replacement():
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    original_rows = [
        (None, "Retained narration."),
        ("aria_nvl", "The signal is weak."),
    ]
    last_len = [2]
    page_fp = [None]
    store = types.SimpleNamespace(nvl_list=original_rows)
    watcher_ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=None)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": [1],
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns["_vnf_claim_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns["_vnf_publish_nvl_entry"],
        },
    )
    page_fp[0] = watcher_ns["_vnf_nvl_fingerprint"](original_rows)
    callback_event = {
        "type": "dialogue",
        "character": "ARIA>",
        "text": "The signal is weakening.",
        "mode": "nvl",
    }
    occurrence_ns["_vnf_note_nvl_callback_event"](
        callback_event, "aria_nvl")

    # Ren'Py's NVL extend replaces only the last row in nvl_list.
    store.nvl_list = [
        (None, "Retained narration."),
        ("aria_nvl", "The signal is weakening."),
    ]
    watcher_ns["_vnf_flush_nvl_entries"]("watch")

    assert pushed == []
    assert occurrences == []
    assert last_len == [2]


def test_nvl_watcher_extend_and_append_between_ticks_loses_neither_row():
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    original_rows = [
        (None, "Retained narration."),
        ("aria_nvl", "The signal is weak."),
    ]
    last_len = [2]
    page_fp = [None]
    store = types.SimpleNamespace(nvl_list=original_rows)
    watcher_ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=None)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": [2],
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns["_vnf_claim_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns["_vnf_publish_nvl_entry"],
        },
    )
    page_fp[0] = watcher_ns["_vnf_nvl_fingerprint"](original_rows)
    replacement = {
        "type": "dialogue", "character": "ARIA>",
        "text": "The signal is weakening.", "mode": "nvl",
    }
    appended = {
        "type": "narration", "text": "Static fills the room.", "mode": "nvl",
    }
    occurrence_ns["_vnf_note_nvl_callback_event"](replacement, "aria_nvl")
    occurrence_ns["_vnf_note_nvl_callback_event"](appended, None)

    store.nvl_list = [
        (None, "Retained narration."),
        ("aria_nvl", "The signal is weakening."),
        (None, "Static fills the room."),
    ]
    watcher_ns["_vnf_flush_nvl_entries"]("watch")

    assert pushed == []
    assert occurrences == []
    assert last_len == [3]


def test_nvl_watcher_bounded_window_publishes_only_new_tail():
    published = []
    original_rows = [(None, "A"), (None, "B"), (None, "C")]
    last_len = [3]
    page_fp = [None]
    store = types.SimpleNamespace(nvl_list=original_rows)
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=3)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": [1],
            "_vnf_nvl_callback_occurrences": [],
            "_vnf_nvl_entry_event": lambda entry: None,
            "_vnf_nvl_event_key": lambda event: (None, event.get("text", "")),
            "_vnf_claim_nvl_callback_event": lambda event: False,
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": (
                lambda entry, source: published.append((entry, source))),
        },
    )
    page_fp[0] = ns["_vnf_nvl_fingerprint"](original_rows)

    store.nvl_list = [(None, "B"), (None, "C"), (None, "D")]
    ns["_vnf_flush_nvl_entries"]("watch")

    assert published == [((None, "D"), "watch")]
    assert last_len == [3]


def test_nvl_watcher_bounded_window_prefers_longer_overlap_with_repeats():
    published = []
    original_rows = [(None, "A"), (None, "A"), (None, "B")]
    last_len = [3]
    page_fp = [None]
    store = types.SimpleNamespace(nvl_list=original_rows)
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=3)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": [1],
            "_vnf_nvl_callback_occurrences": [],
            "_vnf_nvl_entry_event": lambda entry: None,
            "_vnf_nvl_event_key": lambda event: (None, event.get("text", "")),
            "_vnf_claim_nvl_callback_event": lambda event: False,
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": (
                lambda entry, source: published.append((entry, source))),
        },
    )
    page_fp[0] = ns["_vnf_nvl_fingerprint"](original_rows)

    store.nvl_list = [(None, "A"), (None, "B"), (None, "C")]
    ns["_vnf_flush_nvl_entries"]("watch")

    assert published == [((None, "C"), "watch")]
    assert last_len == [3]


def test_nvl_watcher_repeated_multiappend_does_not_skip_new_row():
    published = []
    original_rows = [(None, "A"), (None, "B"), (None, "A")]
    last_len = [3]
    page_fp = [None]
    store = types.SimpleNamespace(nvl_list=original_rows)
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=3)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": [2],
            # Model a callback-missing engine: both new rows must come from
            # the fallback even though their values appeared on the old page.
            "_vnf_nvl_callback_occurrences": [],
            "_vnf_nvl_entry_event": lambda entry: None,
            "_vnf_nvl_event_key": lambda event: (None, event.get("text", "")),
            "_vnf_claim_nvl_callback_event": lambda event: False,
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": (
                lambda entry, source: published.append((entry, source))),
        },
    )
    page_fp[0] = ns["_vnf_nvl_fingerprint"](original_rows)

    # Two appends evict A then B. The surviving old tail A is the one-row
    # exact overlap; the following B is a new occurrence despite its value.
    store.nvl_list = [(None, "A"), (None, "B"), (None, "C")]
    ns["_vnf_flush_nvl_entries"]("watch")

    assert published == [((None, "B"), "watch"), ((None, "C"), "watch")]
    assert last_len == [3]


def test_nvl_watcher_bounded_roll_with_extend_uses_add_provenance():
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    original_rows = [(None, "A"), (None, "B"), (None, "C")]
    last_len = [3]
    page_fp = [None]
    store = types.SimpleNamespace(nvl_list=original_rows)
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=3)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": [2],
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns["_vnf_claim_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns["_vnf_publish_nvl_entry"],
        },
    )
    page_fp[0] = ns["_vnf_nvl_fingerprint"](original_rows)
    extended = {"type": "narration", "text": "D", "mode": "nvl"}
    appended = {"type": "narration", "text": "E", "mode": "nvl"}
    pushed.extend([extended, appended])
    occurrence_ns["_vnf_note_nvl_callback_event"](extended)
    occurrence_ns["_vnf_note_nvl_callback_event"](appended)

    # At the cap, A is evicted, C is extended to D, then E is appended.
    store.nvl_list = [(None, "B"), (None, "D"), (None, "E")]
    ns["_vnf_flush_nvl_entries"]("watch")

    assert pushed == [extended, appended]
    assert occurrences == []
    assert last_len == [3]


def test_nvl_watcher_full_capped_turnover_never_drops_repeated_prefix():
    published = []
    original_rows = [(None, "A"), (None, "B"), (None, "A")]
    last_len = [3]
    page_fp = [None]
    additions = [3]
    store = types.SimpleNamespace(nvl_list=original_rows)
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=3)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": additions,
            "_vnf_nvl_callback_occurrences": [],
            "_vnf_nvl_entry_event": lambda entry: None,
            "_vnf_claim_nvl_callback_event": lambda event: False,
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": (
                lambda entry, source: published.append((entry, source))),
        },
    )
    page_fp[0] = ns["_vnf_nvl_fingerprint"](original_rows)

    store.nvl_list = [(None, "A"), (None, "C"), (None, "D")]
    ns["_vnf_flush_nvl_entries"]("watch")

    assert published == [
        ((None, "A"), "watch"),
        ((None, "C"), "watch"),
        ((None, "D"), "watch"),
    ]
    assert additions == [0]


def test_nvl_add_tracker_is_idempotent_and_uses_native_counter():
    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return (who, what, multiple)

    sys_state = types.SimpleNamespace()
    native_list_type = type(json.loads("[]"))
    sys_state._vnf_nvl_added_since_watch = native_list_type((0,))
    sys_state._vnf_nvl_entries_since_watch = native_list_type()
    renpy = types.SimpleNamespace(
        store=types.SimpleNamespace(NVLCharacter=FakeNVLCharacter))
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": renpy,
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
        },
    )

    ns["_vnf_install_nvl_add_tracker"]()
    first_wrapper = FakeNVLCharacter.do_add
    ns["_vnf_install_nvl_add_tracker"]()
    assert FakeNVLCharacter.do_add is first_wrapper

    result = FakeNVLCharacter().do_add("ARIA>", "Line", multiple=2)

    assert result == ("ARIA>", "Line", 2)
    assert sys_state._vnf_nvl_added_since_watch == [1]
    assert sys_state._vnf_nvl_entries_since_watch == [("ARIA>", "Line")]


def test_nvl_add_observes_rollback_before_capturing_restored_page():
    """The first rebuilt do_add must baseline rollback before fallback scan."""
    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return (who, what, multiple)

    native_list_type = type(json.loads("[]"))
    store = types.SimpleNamespace(
        NVLCharacter=FakeNVLCharacter,
        nvl_list=[(None, "Restored page row")],
    )
    tracker_page = [None]
    captured = []
    calls = []
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=native_list_type((0,)),
        _vnf_nvl_entries_since_watch=native_list_type(),
    )

    def observe_rollback():
        calls.append("rollback")
        tracker_page[0] = tuple(store.nvl_list)

    def capture_restored_page(source, publish):
        calls.append((source, publish))
        if tracker_page[0] != tuple(store.nvl_list):
            captured.extend(store.nvl_list)

    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(store=store),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_tracker_page_fp": tracker_page,
            "_vnf_nvl_prepublished_callbacks": native_list_type(),
            "_vnf_observe_rollback_resume": observe_rollback,
            "_vnf_capture_untracked_nvl_delta": capture_restored_page,
        },
    )

    ns["_vnf_install_nvl_add_tracker"]()
    FakeNVLCharacter().do_add(None, "Rebuilt current row")

    assert calls[:2] == ["rollback", ("pre-add", True)]
    assert captured == []
    assert sys_state._vnf_nvl_entries_since_watch == [
        (None, "Rebuilt current row"),
    ]


def test_nvl_add_tracker_does_not_accumulate_while_disabled():
    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return (who, what, multiple)

    native_list_type = type(json.loads("[]"))
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=native_list_type((0,)),
        _vnf_nvl_entries_since_watch=native_list_type())
    player = types.SimpleNamespace(enabled=False)
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(NVLCharacter=FakeNVLCharacter)),
            "_sys_mod": sys_state,
            "vnf_player": player,
        },
    )
    ns["_vnf_install_nvl_add_tracker"]()

    for index in range(256):
        FakeNVLCharacter().do_add(None, "Line %d" % index)

    assert sys_state._vnf_nvl_added_since_watch == [0]
    assert sys_state._vnf_nvl_entries_since_watch == []

    player.enabled = True
    FakeNVLCharacter().do_add(None, "Connected.")
    assert sys_state._vnf_nvl_added_since_watch == [1]
    assert sys_state._vnf_nvl_entries_since_watch == [(None, "Connected.")]


def test_nvl_done_tracker_rewraps_replacements_without_double_merging():
    calls = []
    merges = []
    captures = []

    class FakeNVLCharacter:
        def do_done(self, who, what, multiple=None):
            calls.append("base")

    store = types.SimpleNamespace(
        NVLCharacter=FakeNVLCharacter, nvl_list=[])
    sys_state = types.SimpleNamespace()
    depth = [0]
    ns = load_shim_functions(
        "_vnf_install_nvl_done_tracker",
        namespace={
            "renpy": types.SimpleNamespace(store=store),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_done_tracker_depth": depth,
            "_vnf_nvl_done_in_progress_entries": [],
            "_vnf_nvl_tracker_page_fp": [()],
            "_vnf_capture_untracked_nvl_delta": (
                lambda source, publish: captures.append(source)),
            "_vnf_nvl_fingerprint": lambda page: tuple(page or ()),
            "_vnf_claim_recorded_nvl_add": lambda character, args: True,
            "_vnf_deactivate_recorded_nvl_display": (
                lambda character, args: None),
            "_vnf_merge_nvl_method_delta": (
                lambda *args: merges.append(args)),
        },
    )
    ns["_vnf_install_nvl_done_tracker"]()
    first_tracker = FakeNVLCharacter.do_done

    def foreign_delegate(self, *args, **kwargs):
        calls.append("delegate")
        return first_tracker(self, *args, **kwargs)

    FakeNVLCharacter.do_done = foreign_delegate
    ns["_vnf_install_nvl_done_tracker"]()
    FakeNVLCharacter().do_done(None, "Delegated")

    assert calls == ["delegate", "base"]
    assert captures == ["pre-done"]
    assert len(merges) == 1
    assert depth == [0]

    calls[:] = []
    captures[:] = []
    merges[:] = []

    def foreign_replacement(self, *args, **kwargs):
        calls.append("replacement")

    FakeNVLCharacter.do_done = foreign_replacement
    ns["_vnf_install_nvl_done_tracker"]()
    assert FakeNVLCharacter.do_done is not foreign_replacement
    FakeNVLCharacter().do_done(None, "Replaced")

    assert calls == ["replacement"]
    assert captures == ["pre-done"]
    assert len(merges) == 1
    assert depth == [0]


def test_nvl_reinit_drops_unpaired_native_entry_queue_and_baselines_page():
    native_list_type = type(json.loads("[]"))
    pending = native_list_type(((None, "Already callback-owned."),))
    sys_state = types.SimpleNamespace(
        _vnf_nvl_entries_since_watch=pending)
    ns = load_shim_functions(
        "_vnf_prepare_nvl_entry_queue",
        namespace={
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": native_list_type,
        },
    )

    queue, reloaded = ns["_vnf_prepare_nvl_entry_queue"]()

    assert reloaded is True
    assert queue is pending
    assert queue == []
    source = SHIM.read_text(encoding="utf-8")
    assert "if _vnf_nvl_tracker_reloaded:" in source
    assert "_vnf_baseline_nvl_capture_state()" in source


def test_nvl_pre_queue_tracker_upgrade_is_treated_as_reinit():
    native_list_type = type(json.loads("[]"))
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=native_list_type((3,)),
        _vnf_nvl_tracker_class=object,
        _vnf_nvl_tracker_wrapper=lambda *args: None)
    ns = load_shim_functions(
        "_vnf_prepare_nvl_entry_queue",
        namespace={
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": native_list_type,
        },
    )

    queue, reloaded = ns["_vnf_prepare_nvl_entry_queue"]()

    assert reloaded is True
    assert queue == []
    assert sys_state._vnf_nvl_entries_since_watch is queue


def test_nvl_tracker_upgrade_normalizes_delegating_v1_counter():
    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return (who, what, multiple)

    native_list_type = type(json.loads("[]"))
    counter = native_list_type((0,))
    entries = native_list_type()
    original = FakeNVLCharacter.do_add

    def v1_tracker(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        counter[0] += 1
        return result

    v1_tracker._vnf_owned_nvl_add_tracker = True
    v1_tracker._vnf_original_nvl_add = original

    def foreign_outer(self, *args, **kwargs):
        return v1_tracker(self, *args, **kwargs)

    FakeNVLCharacter.do_add = foreign_outer
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=v1_tracker)
    player = types.SimpleNamespace(enabled=True)
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(NVLCharacter=FakeNVLCharacter)),
            "_sys_mod": sys_state,
            "vnf_player": player,
        },
    )

    ns["_vnf_install_nvl_add_tracker"]()
    FakeNVLCharacter().do_add("ARIA>", "One exact occurrence.")

    assert counter == [1]
    assert entries == [("ARIA>", "One exact occurrence.")]

    player.enabled = False
    FakeNVLCharacter().do_add("ARIA>", "Not tracked.")
    assert counter == [1]
    assert entries == [("ARIA>", "One exact occurrence.")]


def test_nvl_tracker_upgrade_normalizes_delegating_v2_exact_queue():
    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return (who, what, multiple)

    native_list_type = type(json.loads("[]"))
    counter = native_list_type((0,))
    entries = native_list_type()
    pending_done = native_list_type()
    original = FakeNVLCharacter.do_add

    def v2_tracker(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        counter[0] += 1
        entries.append((args[0], args[1]))
        return result

    v2_tracker._vnf_owned_nvl_add_tracker = True
    v2_tracker._vnf_nvl_tracker_version = 2
    v2_tracker._vnf_original_nvl_add = original

    def foreign_outer(self, *args, **kwargs):
        return v2_tracker(self, *args, **kwargs)

    FakeNVLCharacter.do_add = foreign_outer
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=v2_tracker,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(NVLCharacter=FakeNVLCharacter)),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
        },
    )

    ns["_vnf_install_nvl_add_tracker"]()
    character = FakeNVLCharacter()
    character.do_add("ARIA>", "One v3 occurrence.")

    assert counter == [1]
    assert entries == [("ARIA>", "One v3 occurrence.")]
    assert pending_done == [
        (id(character), "ARIA>", "One v3 occurrence.", False),
    ]


@pytest.mark.parametrize("installed_version", [3, 4, 6])
def test_nvl_add_tracker_upgrades_committed_wrappers_on_reload(
        installed_version):
    """Shift+R must replace wrappers carrying older persistence boundaries."""
    calls = []

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            calls.append("base")

    native_list_type = type(json.loads("[]"))
    counter = native_list_type((0,))
    entries = native_list_type()
    pending_done = native_list_type()
    original = FakeNVLCharacter.do_add

    def old_tracker(self, *args, **kwargs):
        calls.append("old")
        result = original(self, *args, **kwargs)
        counter[0] += 1
        entries.append((args[0], args[1]))
        pending_done.append((id(self), args[0], args[1], False))
        return result

    old_tracker._vnf_owned_nvl_add_tracker = True
    old_tracker._vnf_nvl_tracker_version = installed_version
    old_tracker._vnf_original_nvl_add = original
    FakeNVLCharacter.do_add = old_tracker
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=old_tracker,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(
                    NVLCharacter=FakeNVLCharacter, nvl_list=[])),
            "renpy_version": (6, 99, 12, 4),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_tracker_page_fp": [()],
            "_vnf_capture_untracked_nvl_delta": lambda *args: None,
            "_vnf_nvl_fingerprint": lambda rows: tuple(rows or ()),
            "_vnf_merge_nvl_method_delta": lambda *args: None,
        },
    )

    ns["_vnf_install_nvl_add_tracker"]()
    character = FakeNVLCharacter()
    character.do_add("ARIA>", "Upgraded occurrence.")

    assert calls == ["base"]
    assert FakeNVLCharacter.do_add is not old_tracker
    assert FakeNVLCharacter.do_add._vnf_nvl_tracker_version == 8
    assert counter == [1]
    assert entries == [("ARIA>", "Upgraded occurrence.")]
    assert pending_done == [
        (id(character), "ARIA>", "Upgraded occurrence.", False),
    ]


@pytest.mark.parametrize(
    ("installed_version", "engine_version", "events_after_add"),
    [
        (3, (6, 99, 12, 4), 0),
        (4, (6, 99, 13), 1),
    ],
)
def test_nvl_add_tracker_upgrade_neutralizes_nested_old_merge(
        installed_version, engine_version, events_after_add):
    """A foreign outer wrapper may delegate through an installed old tracker."""
    occurrence_ns, pushed, _occurrences = _nvl_occurrence_env()
    calls = []
    merge_holder = [None]

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            calls.append("base")
            store.nvl_list.append((who, what))

    store = types.SimpleNamespace(
        NVLCharacter=FakeNVLCharacter, nvl_list=[])
    native_list_type = type(json.loads("[]"))
    counter = native_list_type((0,))
    entries = native_list_type()
    pending_done = native_list_type()
    tracker_fp = [()]
    original = FakeNVLCharacter.do_add

    def old_tracker(self, *args, **kwargs):
        calls.append("old")
        before = tracker_fp[0]
        result = original(self, *args, **kwargs)
        tracked = ((args[0], args[1]) if installed_version == 4 else None)
        merge_holder[0](
            before, tuple(store.nvl_list), tracked, "post-add", True)
        counter[0] += 1
        entries.append((args[0], args[1]))
        pending_done.append((id(self), args[0], args[1], False))
        return result

    old_tracker._vnf_owned_nvl_add_tracker = True
    old_tracker._vnf_nvl_tracker_version = installed_version
    old_tracker._vnf_original_nvl_add = original

    def foreign_outer(self, *args, **kwargs):
        calls.append("foreign")
        return old_tracker(self, *args, **kwargs)

    FakeNVLCharacter.do_add = foreign_outer
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_add_merge_suppressed=[0],
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=old_tracker,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        "_vnf_nvl_fingerprint",
        "_vnf_publish_and_queue_nvl_entry",
        "_vnf_merge_nvl_method_delta",
        namespace={
            "renpy": types.SimpleNamespace(store=store),
            "renpy_version": engine_version,
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": native_list_type,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_tracker_page_fp": tracker_fp,
            "_vnf_capture_untracked_nvl_delta": lambda *args: None,
            "_vnf_nvl_entries_since_watch": entries,
            "_vnf_nvl_recorded_adds_pending_done": pending_done,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_note_nvl_callback_event": occurrence_ns[
                "_vnf_note_nvl_callback_event"],
            "_vnf_publish_nvl_entry": occurrence_ns[
                "_vnf_publish_nvl_entry"],
        },
    )
    merge_holder[0] = ns["_vnf_merge_nvl_method_delta"]

    ns["_vnf_install_nvl_add_tracker"]()
    character = FakeNVLCharacter()
    character.do_add("ARIA>", "Nested upgraded occurrence.")

    assert calls == ["foreign", "old", "base"]
    assert FakeNVLCharacter.do_add._vnf_nvl_tracker_version == 8
    assert len(pushed) == events_after_add
    assert counter == [1]
    assert entries == [
        ("ARIA>", "Nested upgraded occurrence."),
    ] * (events_after_add + 1)
    assert pending_done == [
        (id(character), "ARIA>", "Nested upgraded occurrence.", False),
    ]
    assert sys_state._vnf_nvl_add_merge_suppressed == [0]


def test_nvl_add_tracker_upgrade_preserves_reentrant_current_calls():
    """Legacy cleanup must not erase a nested call through the new tracker."""
    calls = []
    nested_character = [None]

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            calls.append(("base", who))
            if who == "outer":
                nested_character[0].do_add("inner", "Nested occurrence.")

    native_list_type = type(json.loads("[]"))
    counter = native_list_type((0,))
    entries = native_list_type()
    pending_done = native_list_type()
    original = FakeNVLCharacter.do_add

    def old_tracker(self, *args, **kwargs):
        calls.append(("old", args[0]))
        result = original(self, *args, **kwargs)
        counter[0] += 1
        entries.append((args[0], args[1]))
        pending_done.append((id(self), args[0], args[1], False))
        return result

    old_tracker._vnf_owned_nvl_add_tracker = True
    old_tracker._vnf_nvl_tracker_version = 4
    old_tracker._vnf_original_nvl_add = original

    def foreign_outer(self, *args, **kwargs):
        calls.append(("foreign", args[0]))
        return old_tracker(self, *args, **kwargs)

    FakeNVLCharacter.do_add = foreign_outer
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_add_merge_suppressed=[0],
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=old_tracker,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(
                    NVLCharacter=FakeNVLCharacter, nvl_list=[])),
            "renpy_version": (6, 99, 12, 4),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_tracker_page_fp": [()],
            "_vnf_capture_untracked_nvl_delta": lambda *args: None,
            "_vnf_nvl_fingerprint": lambda rows: tuple(rows or ()),
            "_vnf_merge_nvl_method_delta": lambda *args: None,
        },
    )

    ns["_vnf_install_nvl_add_tracker"]()
    outer_character = FakeNVLCharacter()
    nested_character[0] = FakeNVLCharacter()
    outer_character.do_add("outer", "Outer occurrence.")

    assert calls == [
        ("foreign", "outer"),
        ("old", "outer"),
        ("base", "outer"),
        ("foreign", "inner"),
        ("old", "inner"),
        ("base", "inner"),
    ]
    assert counter == [2]
    assert entries == [
        ("inner", "Nested occurrence."),
        ("outer", "Outer occurrence."),
    ]
    assert pending_done == [
        (id(nested_character[0]), "inner", "Nested occurrence.", False),
        (id(outer_character), "outer", "Outer occurrence.", False),
    ]
    assert sys_state._vnf_nvl_add_merge_suppressed == [0]


def test_nvl_add_tracker_upgrade_cleans_legacy_effects_after_raise():
    """A foreign wrapper raising after delegation must leave no old receipt."""

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return (who, what, multiple)

    native_list_type = type(json.loads("[]"))
    counter = native_list_type((0,))
    entries = native_list_type()
    pending_done = native_list_type()
    original = FakeNVLCharacter.do_add

    def old_tracker(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        counter[0] += 1
        entries.append((args[0], args[1]))
        pending_done.append((id(self), args[0], args[1], False))
        return result

    old_tracker._vnf_owned_nvl_add_tracker = True
    old_tracker._vnf_nvl_tracker_version = 4
    old_tracker._vnf_original_nvl_add = original

    def foreign_outer(self, *args, **kwargs):
        old_tracker(self, *args, **kwargs)
        raise ValueError("foreign wrapper failed after delegation")

    FakeNVLCharacter.do_add = foreign_outer
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_add_merge_suppressed=[0],
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=old_tracker,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(
                    NVLCharacter=FakeNVLCharacter, nvl_list=[])),
            "renpy_version": (6, 99, 12, 4),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_tracker_page_fp": [()],
            "_vnf_capture_untracked_nvl_delta": lambda *args: None,
            "_vnf_nvl_fingerprint": lambda rows: tuple(rows or ()),
            "_vnf_merge_nvl_method_delta": lambda *args: None,
        },
    )

    ns["_vnf_install_nvl_add_tracker"]()
    with pytest.raises(ValueError, match="failed after delegation"):
        FakeNVLCharacter().do_add("ARIA>", "Failed occurrence.")

    assert counter == [0]
    assert entries == []
    assert pending_done == []
    assert sys_state._vnf_nvl_add_merge_suppressed == [0]


def test_nvl_add_tracker_upgrade_survives_queue_drain_during_delegate():
    """Queue positions may change before the old wrapper records its call."""
    character_holder = [None]

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            entries[:] = []
            pending_done[:] = []
            counter[0] = 0

    native_list_type = type(json.loads("[]"))
    counter = native_list_type((7,))
    entries = native_list_type((("old", "Already delivered."),))
    pending_done = native_list_type(((123, "old", "Pending."),))
    original = FakeNVLCharacter.do_add

    def old_tracker(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        counter[0] += 1
        entries.append((args[0], args[1]))
        pending_done.append((id(self), args[0], args[1], False))
        return result

    old_tracker._vnf_owned_nvl_add_tracker = True
    old_tracker._vnf_nvl_tracker_version = 4
    old_tracker._vnf_original_nvl_add = original

    def foreign_outer(self, *args, **kwargs):
        return old_tracker(self, *args, **kwargs)

    FakeNVLCharacter.do_add = foreign_outer
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_add_merge_suppressed=[0],
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=old_tracker,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(
                    NVLCharacter=FakeNVLCharacter, nvl_list=[])),
            "renpy_version": (6, 99, 12, 4),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_tracker_page_fp": [()],
            "_vnf_capture_untracked_nvl_delta": lambda *args: None,
            "_vnf_nvl_fingerprint": lambda rows: tuple(rows or ()),
            "_vnf_merge_nvl_method_delta": lambda *args: None,
        },
    )

    ns["_vnf_install_nvl_add_tracker"]()
    character_holder[0] = FakeNVLCharacter()
    character_holder[0].do_add("ARIA>", "After the drain.")

    assert counter == [1]
    assert entries == [("ARIA>", "After the drain.")]
    assert pending_done == [
        (id(character_holder[0]), "ARIA>", "After the drain.", False),
    ]
    assert sys_state._vnf_nvl_add_merge_suppressed == [0]


@pytest.mark.parametrize("installed_version", [1, 4])
@pytest.mark.parametrize(
    ("reset_timing", "expected_occurrences"),
    [("before", 1), ("after", 0)],
)
def test_nvl_add_tracker_upgrade_tracks_reset_generation(
        installed_version, reset_timing, expected_occurrences):
    """A nested timeline reset must not hide the old wrapper's new record."""
    reset_holder = [None]

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            if reset_timing == "before":
                reset_holder[0]()

    native_list_type = type(json.loads("[]"))
    counter = native_list_type((1,))
    entries = native_list_type((("ARIA>", "Repeated line."),))
    pending_done = native_list_type()
    callback_occurrences = native_list_type()
    tracker_fp = [()]
    watch_last_len = [1]
    watch_first_fp = [("prior",)]
    original = FakeNVLCharacter.do_add

    def old_tracker(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        counter[0] += 1
        if installed_version >= 2:
            entries.append((args[0], args[1]))
            pending_done.append((id(self), args[0], args[1], False))
        return result

    old_tracker._vnf_owned_nvl_add_tracker = True
    old_tracker._vnf_nvl_tracker_version = installed_version
    old_tracker._vnf_original_nvl_add = original

    def foreign_outer(self, *args, **kwargs):
        result = old_tracker(self, *args, **kwargs)
        if reset_timing == "after":
            reset_holder[0]()
        return result

    FakeNVLCharacter.do_add = foreign_outer
    character = FakeNVLCharacter()
    pending_done.append(
        (id(character), "ARIA>", "Repeated line.", False))
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_add_merge_suppressed=[0],
        _vnf_nvl_capture_reset_epoch=[0],
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=old_tracker,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        "_vnf_reset_nvl_capture_state",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(
                    NVLCharacter=FakeNVLCharacter, nvl_list=[])),
            "renpy_version": (6, 99, 12, 4),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_tracker_page_fp": tracker_fp,
            "_vnf_capture_untracked_nvl_delta": lambda *args: None,
            "_vnf_nvl_fingerprint": lambda rows: tuple(rows or ()),
            "_vnf_merge_nvl_method_delta": lambda *args: None,
            "_vnf_nvl_callback_occurrences": callback_occurrences,
            "_vnf_nvl_watch_last_len": watch_last_len,
            "_vnf_nvl_watch_first_fp": watch_first_fp,
            "_vnf_nvl_added_since_watch": counter,
            "_vnf_nvl_entries_since_watch": entries,
            "_vnf_nvl_recorded_adds_pending_done": pending_done,
        },
    )
    reset_holder[0] = ns["_vnf_reset_nvl_capture_state"]

    ns["_vnf_install_nvl_add_tracker"]()
    character.do_add("ARIA>", "Repeated line.")

    assert counter == [expected_occurrences]
    assert entries == [
        ("ARIA>", "Repeated line."),
    ] * expected_occurrences
    assert pending_done == ([
        (id(character), "ARIA>", "Repeated line.", False),
    ] if expected_occurrences else [])
    assert sys_state._vnf_nvl_capture_reset_epoch == [1]
    assert sys_state._vnf_nvl_add_merge_suppressed == [0]


def test_nvl_add_tracker_upgrade_does_not_assume_foreign_delegation():
    """A conditional foreign wrapper may return without calling the old one."""
    calls = []

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            calls.append("base")

    native_list_type = type(json.loads("[]"))
    counter = native_list_type((1,))
    entries = native_list_type((("ARIA>", "Repeated line."),))
    pending_done = native_list_type()
    original = FakeNVLCharacter.do_add

    def old_tracker(self, *args, **kwargs):
        calls.append("old")
        result = original(self, *args, **kwargs)
        counter[0] += 1
        entries.append((args[0], args[1]))
        pending_done.append((id(self), args[0], args[1], False))
        return result

    old_tracker._vnf_owned_nvl_add_tracker = True
    old_tracker._vnf_nvl_tracker_version = 4
    old_tracker._vnf_original_nvl_add = original

    def foreign_outer(self, *args, **kwargs):
        calls.append("foreign")
        return (args, kwargs)

    FakeNVLCharacter.do_add = foreign_outer
    character = FakeNVLCharacter()
    pending_done.append(
        (id(character), "ARIA>", "Repeated line.", False))
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_add_merge_suppressed=[0],
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=old_tracker,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(
                    NVLCharacter=FakeNVLCharacter, nvl_list=[])),
            "renpy_version": (7, 5, 2),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_tracker_page_fp": [()],
            "_vnf_capture_untracked_nvl_delta": lambda *args: None,
            "_vnf_nvl_fingerprint": lambda rows: tuple(rows or ()),
            "_vnf_merge_nvl_method_delta": lambda *args: None,
        },
    )

    ns["_vnf_install_nvl_add_tracker"]()
    character.do_add("ARIA>", "Repeated line.")

    assert calls == ["foreign"]
    assert counter == [2]
    assert entries == [
        ("ARIA>", "Repeated line."),
        ("ARIA>", "Repeated line."),
    ]
    assert pending_done == [
        (id(character), "ARIA>", "Repeated line.", False),
    ]
    assert sys_state._vnf_nvl_add_merge_suppressed == [0]


@pytest.mark.parametrize("reset_after_done", [False, True])
def test_nvl_add_tracker_upgrade_carries_nested_done_completion(
        reset_after_done):
    """A foreign wrapper completing do_done must not leave a stale receipt."""
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    merge_holder = [None]
    reset_holder = [None]

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return None

        def do_done(self, who, what, multiple=None):
            store.nvl_list.append((None, "Direct completion row."))
            store.nvl_list.append((who, what))

    store = types.SimpleNamespace(
        NVLCharacter=FakeNVLCharacter, nvl_list=[])
    native_list_type = type(json.loads("[]"))
    counter = native_list_type((0,))
    entries = native_list_type()
    pending_done = native_list_type()
    done_in_progress = native_list_type()
    tracker_fp = [()]
    original_add = FakeNVLCharacter.do_add

    def old_tracker(self, *args, **kwargs):
        before = tracker_fp[0]
        result = original_add(self, *args, **kwargs)
        merge_holder[0](
            before, tuple(store.nvl_list), (args[0], args[1]),
            "post-add", True)
        counter[0] += 1
        entries.append((args[0], args[1]))
        pending_done.append((id(self), args[0], args[1], False))
        return result

    old_tracker._vnf_owned_nvl_add_tracker = True
    old_tracker._vnf_nvl_tracker_version = 4
    old_tracker._vnf_original_nvl_add = original_add

    def foreign_outer(self, *args, **kwargs):
        result = old_tracker(self, *args, **kwargs)
        self.do_done(*args, **kwargs)
        if reset_after_done:
            store.nvl_list[:] = []
            reset_holder[0]()
        return result

    FakeNVLCharacter.do_add = foreign_outer
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_done_in_progress_entries=done_in_progress,
        _vnf_nvl_add_merge_suppressed=[0],
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=old_tracker,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        "_vnf_install_nvl_done_tracker",
        "_vnf_nvl_fingerprint",
        "_vnf_publish_and_queue_nvl_entry",
        "_vnf_merge_nvl_method_delta",
        "_vnf_claim_recorded_nvl_add",
        "_vnf_deactivate_recorded_nvl_display",
        "_vnf_reset_nvl_capture_state",
        namespace={
            "renpy": types.SimpleNamespace(store=store),
            "renpy_version": (7, 5, 2),
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": native_list_type,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_tracker_page_fp": tracker_fp,
            "_vnf_nvl_done_tracker_depth": [0],
            "_vnf_nvl_watch_first_fp": [None],
            "_vnf_nvl_watch_last_len": [0],
            "_vnf_nvl_added_since_watch": counter,
            "_vnf_nvl_done_in_progress_entries": done_in_progress,
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_capture_untracked_nvl_delta": lambda *args: None,
            "_vnf_nvl_entries_since_watch": entries,
            "_vnf_nvl_recorded_adds_pending_done": pending_done,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_note_nvl_callback_event": occurrence_ns[
                "_vnf_note_nvl_callback_event"],
            "_vnf_publish_nvl_entry": occurrence_ns[
                "_vnf_publish_nvl_entry"],
        },
    )
    merge_holder[0] = ns["_vnf_merge_nvl_method_delta"]
    reset_holder[0] = ns["_vnf_reset_nvl_capture_state"]

    ns["_vnf_install_nvl_add_tracker"]()
    ns["_vnf_install_nvl_done_tracker"]()
    character = FakeNVLCharacter()
    character.do_add("ARIA>", "Completed inside the wrapper.")

    assert [event["text"] for event in pushed] == [
        "Direct completion row.",
    ]
    assert len(occurrences) == (0 if reset_after_done else 1)
    assert store.nvl_list == ([] if reset_after_done else [
        (None, "Direct completion row."),
        ("ARIA>", "Completed inside the wrapper."),
    ])
    assert counter == [0 if reset_after_done else 1]
    assert entries == ([] if reset_after_done else [
        ("", "Direct completion row."),
        ("ARIA>", "Completed inside the wrapper."),
    ])
    assert pending_done == []
    assert sys_state._vnf_nvl_active_legacy_adds == []
    assert sys_state._vnf_nvl_add_merge_suppressed == [0]


def test_nvl_flush_defers_during_legacy_add_upgrade():
    """A reentrant watcher cannot publish legacy ownership mid-upgrade."""
    ns = load_shim_functions(
        "_vnf_flush_nvl_entries",
        namespace={
            "_sys_mod": types.SimpleNamespace(
                _vnf_nvl_add_merge_suppressed=[1]),
        },
    )

    ns["_vnf_flush_nvl_entries"]("watch")


def test_nvl_direct_delta_defers_during_legacy_add_upgrade():
    """Nested add pre-capture cannot publish old ownership mid-upgrade."""
    ns = load_shim_functions(
        "_vnf_capture_untracked_nvl_delta",
        namespace={
            "_sys_mod": types.SimpleNamespace(
                _vnf_nvl_add_merge_suppressed=[1]),
        },
    )

    ns["_vnf_capture_untracked_nvl_delta"]("pre-add", True)


@pytest.mark.parametrize(
    ("legacy_recorded", "nested_recorded", "expected_current_rows"),
    [(False, False, 1), (True, False, 2), (True, True, 3)],
)
def test_nvl_boundary_flush_rescues_backlog_during_legacy_upgrade(
        legacy_recorded, nested_recorded, expected_current_rows):
    """A clear/hide boundary drains prior rows but not the old tracker's row."""
    published = []
    target = ("ARIA>", "Current line.")
    entries = [(None, "Backlog one."), (None, "Backlog two."), target]
    current_records = []
    if nested_recorded:
        entries.append(target)
        current_records.append((321, target[0], target[1], 0))
    if legacy_recorded:
        entries.append(target)
    counter = [len(entries)]
    active_record = [
        123, target[0], target[1], False, True, 1, 0, 0, False, 3,
    ]
    sys_state = types.SimpleNamespace(
        _vnf_nvl_add_merge_suppressed=[1],
        _vnf_nvl_capture_reset_epoch=[0],
        _vnf_nvl_current_adds_during_legacy=current_records,
        _vnf_nvl_active_legacy_adds=[active_record],
    )
    store = types.SimpleNamespace(nvl_list=list(entries))
    ns = load_shim_functions(
        "_vnf_nvl_delta_start",
        "_vnf_prepare_legacy_boundary_entries",
        "_vnf_merge_forced_boundary_page_entries",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store,
                config=types.SimpleNamespace(nvl_list_length=None),
            ),
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": list,
            "_vnf_capture_untracked_nvl_delta": lambda *args: None,
            "_vnf_nvl_entries_since_watch": entries,
            "_vnf_nvl_done_in_progress_entries": [],
            "_vnf_nvl_callback_occurrences": [],
            "_vnf_nvl_watch_last_len": [0],
            "_vnf_nvl_watch_first_fp": [None],
            "_vnf_nvl_tracker_page_fp": [tuple(store.nvl_list)],
            "_vnf_nvl_added_since_watch": counter,
            "_vnf_nvl_fingerprint": lambda rows: tuple(rows or ()),
            "_vnf_strip_active_nvl_display_entries": (
                lambda rows: tuple(rows or ())),
            "_vnf_note_nvl_prepublished_callback": lambda *args: None,
            "_vnf_publish_forced_boundary_entry": (
                lambda entry, source, candidates:
                    published.append((entry, source)) or True),
            "_vnf_reconcile_equal_nvl_tail": lambda rows: None,
            "_vnf_publish_nvl_entry": (
                lambda entry, source:
                    published.append((entry, source)) or True),
        },
    )

    ns["_vnf_flush_nvl_entries"]("pre-clear", True)

    assert [entry for entry, _source in published] == [
        (None, "Backlog one."),
        (None, "Backlog two."),
    ] + [target] * expected_current_rows
    assert all(source == "pre-clear" for _entry, source in published)
    assert entries == []
    assert counter == [0]
    assert active_record[8] is legacy_recorded


@pytest.mark.parametrize(
    ("previous", "current", "window_limit", "expected_direct"),
    [
        (
            [(None, "A"), (None, "B")],
            [(None, "A"), (None, "C")],
            None,
            [(None, "C")],
        ),
        (
            [(None, "A"), (None, "A")],
            [(None, "A"), (None, "C")],
            2,
            [(None, "A"), (None, "C")],
        ),
    ],
)
def test_nvl_forced_boundary_uses_shared_page_delta_policy(
        previous, current, window_limit, expected_direct):
    """Off-cap replacements keep prefixes; capped ambiguity replays safely."""
    target = ("ARIA>", "Deferred exact row.")
    active_record = [
        123, target[0], target[1], False, True, 0, 0, 0, True, 0,
    ]
    sys_state = types.SimpleNamespace(
        _vnf_nvl_capture_reset_epoch=[0],
        _vnf_nvl_current_adds_during_legacy=[],
        _vnf_nvl_active_legacy_adds=[active_record],
    )
    ns = load_shim_functions(
        "_vnf_nvl_delta_start",
        "_vnf_merge_forced_boundary_page_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(nvl_list=list(current)),
                config=types.SimpleNamespace(nvl_list_length=window_limit),
            ),
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": list,
            "_vnf_nvl_tracker_page_fp": [tuple(previous)],
            "_vnf_nvl_fingerprint": lambda rows: tuple(rows or ()),
            "_vnf_strip_active_nvl_display_entries": (
                lambda rows: tuple(rows or ())),
        },
    )

    result = ns["_vnf_merge_forced_boundary_page_entries"]([target])

    assert result == [target] + expected_direct


def test_nvl_forced_boundary_strips_temporary_display_occurrence():
    """An active do_display row is already owned by the exact add stream."""
    target = ("ARIA>", "Temporary current line.")
    pending_done = [(123, target[0], target[1], True)]
    sys_state = types.SimpleNamespace(
        _vnf_nvl_capture_reset_epoch=[0],
        _vnf_nvl_current_adds_during_legacy=[],
        _vnf_nvl_active_legacy_adds=[
            [123, target[0], target[1], False, True, 0, 0, 0, True, 0],
        ],
    )
    ns = load_shim_functions(
        "_vnf_strip_active_nvl_display_entries",
        "_vnf_nvl_delta_start",
        "_vnf_merge_forced_boundary_page_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(nvl_list=[target]),
                config=types.SimpleNamespace(nvl_list_length=None),
            ),
            "renpy_version": (7, 5, 2),
            "_sys_mod": sys_state,
            "_VNF_NATIVE_LIST_TYPE": list,
            "_vnf_nvl_recorded_adds_pending_done": pending_done,
            "_vnf_nvl_tracker_page_fp": [()],
            "_vnf_nvl_entry_event": lambda entry: {
                "type": "dialogue", "character": entry[0],
                "text": entry[1], "mode": "nvl",
            },
            "_vnf_nvl_event_key": lambda event: (
                event.get("character"), event["text"]),
            "_vnf_nvl_fingerprint": lambda rows: tuple(rows or ()),
        },
    )

    result = ns["_vnf_merge_forced_boundary_page_entries"]([target])

    assert result == [target]
    assert pending_done == [(123, target[0], target[1], True)]


@pytest.mark.parametrize("published", [False, True])
def test_nvl_forced_boundary_marks_only_an_emitted_exact_occurrence(published):
    """Callback-first suppression must not leave reverse ownership behind."""
    entry = ("ARIA>", "One occurrence.")
    candidate = (entry, ("nested", 42), 3)
    notes = []
    candidates = [candidate]
    ns = load_shim_functions(
        "_vnf_publish_forced_boundary_entry",
        namespace={
            "_vnf_publish_nvl_entry": lambda value, source: published,
            "_vnf_note_nvl_prepublished_callback": (
                lambda *args: notes.append(args)),
        },
    )

    assert ns["_vnf_publish_forced_boundary_entry"](
        entry, "pre-clear", candidates) is published
    assert notes == ([(entry, ("nested", 42), 3)] if published else [])
    assert candidates == ([] if published else [candidate])


@pytest.mark.parametrize(
    ("engine_version", "stock_persists", "direct_row",
     "fresh_after_boundary"),
    [
        ((6, 99, 12, 4), True, (None, "Direct boundary row."), False),
        ((6, 99, 12, 4), True, ("ARIA>", "Current line."), False),
        ((6, 99, 13), False, ("ARIA>", "Current line."), False),
        ((7, 5, 2), False, ("ARIA>", "Current line."), False),
        ((7, 5, 2), False, (None, "Direct boundary row."), True),
    ],
)
def test_nvl_clear_boundary_does_not_resurrect_flushed_legacy_add(
        engine_version, stock_persists, direct_row, fresh_after_boundary):
    """A mid-wrapper clear publishes backlog/current once, before abandoning."""
    published = []
    flush_holder = [None]
    reset_holder = [None]
    claim_holder = [None]

    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            if stock_persists:
                store.nvl_list.append((who, what))

    store = types.SimpleNamespace(
        NVLCharacter=FakeNVLCharacter,
        nvl_list=[(None, "Backlog one."), (None, "Backlog two.")],
    )
    native_list_type = type(json.loads("[]"))
    entries = native_list_type(store.nvl_list)
    counter = native_list_type((2,))
    pending_done = native_list_type()
    callback_occurrences = native_list_type()
    prepublished_callbacks = native_list_type()
    tracker_fp = [tuple(store.nvl_list)]
    original = FakeNVLCharacter.do_add

    def old_tracker(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        counter[0] += 1
        entries.append((args[0], args[1]))
        pending_done.append((id(self), args[0], args[1], False))
        return result

    old_tracker._vnf_owned_nvl_add_tracker = True
    old_tracker._vnf_nvl_tracker_version = 4
    old_tracker._vnf_original_nvl_add = original

    def foreign_outer(self, *args, **kwargs):
        result = old_tracker(self, *args, **kwargs)
        store.nvl_list.append(direct_row)
        flush_holder[0]("pre-clear", True)
        if fresh_after_boundary:
            callback_event = {
                "type": "dialogue", "character": "ARIA>",
                "text": "Current line.", "mode": "nvl",
            }
            assert claim_holder[0](callback_event, "ARIA>") is True
            # A second boundary in the same cooperative wrapper must not
            # recreate ownership for the occurrence drained above.
            flush_holder[0]("pre-hide", True)
        reset_holder[0]()
        store.nvl_list[:] = []
        if fresh_after_boundary:
            old_tracker(self, *args, **kwargs)
        return result

    FakeNVLCharacter.do_add = foreign_outer
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=counter,
        _vnf_nvl_entries_since_watch=entries,
        _vnf_nvl_recorded_adds_pending_done=pending_done,
        _vnf_nvl_done_in_progress_entries=native_list_type(),
        _vnf_nvl_add_merge_suppressed=[0],
        _vnf_nvl_capture_reset_epoch=[0],
        _vnf_nvl_tracker_class=FakeNVLCharacter,
        _vnf_nvl_tracker_wrapper=old_tracker,
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        "_vnf_nvl_delta_start",
        "_vnf_note_nvl_prepublished_callback",
        "_vnf_claim_nvl_prepublished_callback_event",
        "_vnf_prepare_legacy_boundary_entries",
        "_vnf_merge_forced_boundary_page_entries",
        "_vnf_publish_forced_boundary_entry",
        "_vnf_flush_nvl_entries",
        "_vnf_reset_nvl_capture_state",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store,
                config=types.SimpleNamespace(nvl_list_length=None),
            ),
            "renpy_version": engine_version,
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_VNF_NATIVE_LIST_TYPE": native_list_type,
            "_vnf_nvl_tracker_page_fp": tracker_fp,
            "_vnf_capture_untracked_nvl_delta": lambda *args: None,
            "_vnf_nvl_fingerprint": lambda rows: tuple(rows or ()),
            "_vnf_strip_active_nvl_display_entries": (
                lambda rows: tuple(rows or ())),
            "_vnf_merge_nvl_method_delta": lambda *args: None,
            "_vnf_nvl_entries_since_watch": entries,
            "_vnf_nvl_recorded_adds_pending_done": pending_done,
            "_vnf_nvl_done_in_progress_entries": (
                sys_state._vnf_nvl_done_in_progress_entries),
            "_vnf_nvl_callback_occurrences": callback_occurrences,
            "_vnf_nvl_prepublished_callbacks": prepublished_callbacks,
            "_vnf_nvl_entry_event": lambda entry: {
                "type": "dialogue" if entry[0] else "narration",
                "character": entry[0],
                "text": entry[1],
                "mode": "nvl",
            },
            "_vnf_nvl_event_key": lambda event: (
                event.get("character"), event["text"]),
            "_vnf_stringify": lambda value: (
                None if value is None else str(value)),
            "basestring": str,
            "_vnf_nvl_watch_last_len": [2],
            "_vnf_nvl_watch_first_fp": [tuple(store.nvl_list)],
            "_vnf_nvl_added_since_watch": counter,
            "_vnf_reconcile_equal_nvl_tail": lambda rows: None,
            "_vnf_publish_nvl_entry": (
                lambda entry, source:
                    published.append((entry, source)) or True),
        },
    )
    flush_holder[0] = ns["_vnf_flush_nvl_entries"]
    reset_holder[0] = ns["_vnf_reset_nvl_capture_state"]
    claim_holder[0] = ns["_vnf_claim_nvl_prepublished_callback_event"]

    ns["_vnf_install_nvl_add_tracker"]()
    character = FakeNVLCharacter()
    character.do_add("ARIA>", "Current line.")

    assert [entry for entry, _source in published] == [
        (None, "Backlog one."),
        (None, "Backlog two."),
        ("ARIA>", "Current line."),
        direct_row,
    ]
    callback_event = {
        "type": "dialogue", "character": "ARIA>",
        "text": "Current line.", "mode": "nvl",
    }
    assert ns["_vnf_claim_nvl_prepublished_callback_event"](
        callback_event, "ARIA>") is (not fresh_after_boundary)
    assert ns["_vnf_claim_nvl_prepublished_callback_event"](
        callback_event, "ARIA>") is False
    expected_fresh = [("ARIA>", "Current line.")] if fresh_after_boundary else []
    assert entries == expected_fresh
    assert pending_done == ([
        (id(character), "ARIA>", "Current line.", False),
    ] if fresh_after_boundary else [])
    assert counter == [1 if fresh_after_boundary else 0]
    assert sys_state._vnf_nvl_capture_reset_epoch == [1]
    assert sys_state._vnf_nvl_add_merge_suppressed == [0]


def test_nvl_character_callback_claims_boundary_published_row_before_push():
    callback = function_node(parse_shim_python(), "_vnf_character_callback")
    source = ast.unparse(callback)

    claim = "_vnf_claim_nvl_prepublished_callback_event(ev, who)"
    assert source.index(claim) < source.index("_vnf_client.push_event(ev)")
    assert source.index(claim) < source.index(
        "_vnf_note_nvl_callback_event(ev, who)")


def test_character_callback_observes_rollback_before_reading_story():
    callback = function_node(parse_shim_python(), "_vnf_character_callback")
    source = ast.unparse(callback)

    assert source.index("_vnf_observe_rollback_resume()") < source.index(
        "_get_last_say()")


def test_nvl_add_tracker_does_not_stack_across_foreign_outer_wrapper():
    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return (who, what, multiple)

    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=type(json.loads("[]"))((0,)),
        _vnf_nvl_entries_since_watch=type(json.loads("[]"))())
    renpy = types.SimpleNamespace(
        store=types.SimpleNamespace(NVLCharacter=FakeNVLCharacter))
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": renpy,
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
        },
    )
    ns["_vnf_install_nvl_add_tracker"]()
    tracker = FakeNVLCharacter.do_add

    def foreign_wrapper(self, *args, **kwargs):
        return tracker(self, *args, **kwargs)

    FakeNVLCharacter.do_add = foreign_wrapper
    ns["_vnf_install_nvl_add_tracker"]()
    FakeNVLCharacter().do_add("ARIA>", "Line")

    assert FakeNVLCharacter.do_add is foreign_wrapper
    assert sys_state._vnf_nvl_added_since_watch == [1]
    assert sys_state._vnf_nvl_entries_since_watch == [("ARIA>", "Line")]


def test_nvl_add_tracker_retires_boundary_marker_inside_suppressed_chain():
    """A nested add is a fresh occurrence even while an outer add is active."""
    class FakeNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return (who, what, multiple)

    native_list_type = type(json.loads("[]"))
    prepublished = native_list_type((
        (("nested", 1), 0, "ARIA>", "Same line.", None),
    ))
    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=native_list_type((0,)),
        _vnf_nvl_entries_since_watch=native_list_type(),
        _vnf_nvl_add_merge_suppressed=native_list_type((1,)),
    )
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(NVLCharacter=FakeNVLCharacter)),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
            "_vnf_nvl_prepublished_callbacks": prepublished,
        },
    )
    ns["_vnf_install_nvl_add_tracker"]()

    FakeNVLCharacter().do_add("ARIA>", "Same line.")

    assert prepublished == []
    assert sys_state._vnf_nvl_entries_since_watch == [
        ("ARIA>", "Same line."),
    ]
    assert sys_state._vnf_nvl_add_merge_suppressed == [1]


def test_nvl_add_tracker_does_not_stack_on_delegating_subclass():
    class BaseNVLCharacter:
        def do_add(self, who, what, multiple=None):
            return (who, what, multiple)

    sys_state = types.SimpleNamespace(
        _vnf_nvl_added_since_watch=type(json.loads("[]"))((0,)),
        _vnf_nvl_entries_since_watch=type(json.loads("[]"))())
    store = types.SimpleNamespace(NVLCharacter=BaseNVLCharacter)
    ns = load_shim_functions(
        "_vnf_install_nvl_add_tracker",
        namespace={
            "renpy": types.SimpleNamespace(store=store),
            "_sys_mod": sys_state,
            "vnf_player": types.SimpleNamespace(enabled=True),
        },
    )
    ns["_vnf_install_nvl_add_tracker"]()
    tracked_base = BaseNVLCharacter.do_add

    class DerivedNVLCharacter(BaseNVLCharacter):
        def do_add(self, *args, **kwargs):
            return tracked_base(self, *args, **kwargs)

    store.NVLCharacter = DerivedNVLCharacter
    ns["_vnf_install_nvl_add_tracker"]()
    DerivedNVLCharacter().do_add("ARIA>", "Line")

    assert DerivedNVLCharacter.do_add is not tracked_base
    assert sys_state._vnf_nvl_added_since_watch == [1]
    assert sys_state._vnf_nvl_entries_since_watch == [("ARIA>", "Line")]


def test_nvl_watcher_partial_callback_tail_never_drops_changed_row():
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    original_rows = [(None, "A"), (None, "B"), (None, "C")]
    last_len = [3]
    page_fp = [None]
    store = types.SimpleNamespace(nvl_list=original_rows)
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=3)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": [2],
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_claim_nvl_callback_event": occurrence_ns["_vnf_claim_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns["_vnf_publish_nvl_entry"],
        },
    )
    page_fp[0] = ns["_vnf_nvl_fingerprint"](original_rows)
    appended = {"type": "narration", "text": "E", "mode": "nvl"}
    pushed.append(appended)
    occurrence_ns["_vnf_note_nvl_callback_event"](appended)

    store.nvl_list = [(None, "B"), (None, "D"), (None, "E")]
    ns["_vnf_flush_nvl_entries"]("watch")

    # D's callback was missed, so it must be visible through the fallback.
    assert {event["text"] for event in pushed} == {"D", "E"}
    assert occurrences == []
    assert last_len == [3]


def test_nvl_watcher_equal_bounded_roll_consumes_all_callback_markers():
    occurrence_ns, pushed, occurrences = _nvl_occurrence_env()
    rows = [("aria_nvl", "Again."), ("aria_nvl", "Again.")]
    last_len = [2]
    page_fp = [None]
    store = types.SimpleNamespace(nvl_list=rows)
    ns = load_shim_functions(
        "_vnf_nvl_fingerprint",
        "_vnf_nvl_delta_start",
        "_vnf_reconcile_equal_nvl_tail",
        "_vnf_flush_nvl_entries",
        namespace={
            "renpy": types.SimpleNamespace(
                store=store, config=types.SimpleNamespace(nvl_list_length=2)),
            "_vnf_nvl_watch_last_len": last_len,
            "_vnf_nvl_watch_first_fp": page_fp,
            "_vnf_nvl_added_since_watch": [2],
            "_vnf_nvl_callback_occurrences": occurrences,
            "_vnf_nvl_entry_event": occurrence_ns["_vnf_nvl_entry_event"],
            "_vnf_nvl_event_key": occurrence_ns["_vnf_nvl_event_key"],
            "_vnf_claim_nvl_callback_event": occurrence_ns["_vnf_claim_nvl_callback_event"],
            "_vnf_log": lambda *a, **kw: None,
            "_vnf_publish_nvl_entry": occurrence_ns["_vnf_publish_nvl_entry"],
        },
    )
    page_fp[0] = ns["_vnf_nvl_fingerprint"](rows)
    callback_event = {
        "type": "dialogue", "character": "ARIA>",
        "text": "Again.", "mode": "nvl",
    }
    pushed.append(callback_event)
    occurrence_ns["_vnf_note_nvl_callback_event"](
        callback_event, "aria_nvl")
    pushed.append(callback_event.copy())
    occurrence_ns["_vnf_note_nvl_callback_event"](
        callback_event, "aria_nvl")

    # At a bounded window, another identical occurrence rolls the page into
    # the same semantic snapshot. The callback is the occurrence signal.
    store.nvl_list = [("aria_nvl", "Again."), ("aria_nvl", "Again.")]
    ns["_vnf_flush_nvl_entries"]("watch")

    assert pushed == [callback_event, callback_event]
    assert occurrences == []
    assert last_len == [2]


def test_menu_caption_is_not_attributed_to_the_previous_speaker():
    """The live Echoes canteen repro: Marcus spoke last, the caption is
    Elara's interior prompt, and Ren'Py narrates it with a stale
    _last_say_who still pointing at him."""
    callback, pushed, state = _character_callback_env(
        node=_FakeMenuNode(),
        last_say_who="marcus",
        last_say_what="You have that look. The one from the review board.",
        characters={"marcus": _FakeCharacter("Dr. Chen")},
        return_state=True,
    )

    callback("begin", False, what="Should I tell him?")

    assert pushed == []
    ev = state.pending_menu_caption[0]
    assert ev["type"] == "narration"
    assert "character" not in ev
    assert ev["text"] == "Should I tell him?"
    assert ev["menu_caption"] is True


def test_menu_caption_moves_atomically_into_the_next_choice_request_items():
    callback, pushed, state = _character_callback_env(
        node=_FakeMenuNode(),
        last_say_who="marcus",
        last_say_what="You have that look.",
        characters={"marcus": _FakeCharacter("Dr. Chen")},
        return_state=True,
    )
    callback("begin", False, what="Should I tell him?")
    choices = [
        {"index": 1, "label": "Tell him", "caption": False,
         "disabled": False},
        {"index": 2, "label": "Yes", "caption": False,
         "disabled": False},
    ]

    # Even if publication is delayed until after another poll/command, there
    # is no standalone caption event to overtake or be consumed separately.
    assert pushed == []
    state.attach_pending_menu_caption(choices)

    assert choices[0] == {
        "index": None,
        "label": "Should I tell him?",
        "caption": True,
        "disabled": False,
    }
    assert state.pending_menu_caption[0]["text"] == "Should I tell him?"
    state.commit_pending_menu_caption()
    assert state.pending_menu_caption == [None]
    state.attach_pending_menu_caption(choices)
    assert [item["label"] for item in choices] == [
        "Should I tell him?", "Tell him", "Yes",
    ]


def test_menu_caption_handoff_dedups_an_inline_renpy_caption():
    callback, pushed, state = _character_callback_env(
        node=_FakeMenuNode(),
        last_say_who=None,
        last_say_what="",
        return_state=True,
    )
    callback("begin", False, what="Which route?")
    choices = [
        {"index": None, "label": "Which route?", "caption": True,
         "disabled": False},
        {"index": 1, "label": "North", "caption": False,
         "disabled": False},
    ]

    state.attach_pending_menu_caption(choices)

    assert pushed == []
    assert [item["label"] for item in choices] == ["Which route?", "North"]
    assert state.pending_menu_caption[0]["text"] == "Which route?"
    state.commit_pending_menu_caption()
    assert state.pending_menu_caption == [None]


def test_attached_menu_caption_survives_until_exception_fallback_flush():
    callback, pushed, state = _character_callback_env(
        node=_FakeMenuNode(),
        last_say_who=None,
        last_say_what="",
        return_state=True,
    )
    callback("begin", False, what="Continue?")
    choices = [
        {"index": 1, "label": "Yes", "caption": False,
         "disabled": False},
    ]

    state.attach_pending_menu_caption(choices)
    # Simulate a transform/control-flow exception before push_request.
    state.flush_pending_menu_caption()

    assert [event["text"] for event in pushed] == ["Continue?"]
    assert choices[0]["label"] == "Continue?"


def test_staged_menu_caption_flushes_once_without_a_choice_request():
    callback, pushed, state = _character_callback_env(
        node=_FakeMenuNode(),
        last_say_who=None,
        last_say_what="",
        return_state=True,
    )
    callback("begin", False, what="Continue?")

    state.flush_pending_menu_caption()
    state.flush_pending_menu_caption()

    assert [event["text"] for event in pushed] == ["Continue?"]
    assert pushed[0]["menu_caption"] is True


def test_menu_wrapper_attaches_caption_before_request_publication():
    wrapper = ast.unparse(
        function_node(parse_shim_python(), "_wrapper_inner")
    )

    assert wrapper.index("_vnf_attach_pending_menu_caption(choices)") < (
        wrapper.index("_full_items = []")
    ) < wrapper.index("_vnf_client.push_request(")
    assert wrapper.index("_vnf_client.push_request(") < wrapper.index(
        "_vnf_commit_pending_menu_caption()")


def test_menu_caption_handoff_uses_nonrollback_native_state():
    source = SHIM.read_text(encoding="utf-8")

    assert "_sys_mod._vnf_pending_menu_caption = _VNF_NATIVE_LIST_TYPE" in source
    assert "_vnf_commit_pending_menu_caption()" in ast.unparse(
        function_node(parse_shim_python(), "_vnf_after_load_callback"))
    assert "_vnf_commit_pending_menu_caption()" in ast.unparse(
        function_node(parse_shim_python(), "_vnf_finish_rollback_resume"))


def test_renpy7_narrator_menu_restores_engine_joined_captions_from_ast():
    """Old callbacks omit what and narrator_menu removes captions from items."""
    evaluated = []
    renpy = types.SimpleNamespace(
        config=types.SimpleNamespace(narrator_menu=True),
        python=types.SimpleNamespace(
            py_eval=lambda expression: evaluated.append(expression)),
    )
    ns = load_shim_functions(
        "_vnf_restore_raw_menu_captions",
        namespace={
            "renpy": renpy,
            "basestring": str,
            "_vnf_is_mapping": lambda value: hasattr(value, "get"),
        },
    )
    choices = [{
        "index": 1, "label": "Tell him", "caption": False,
        "disabled": False,
    }]
    raw = [
        {"label": "Should I tell him?", "condition": "visible",
         "is_caption": True},
        {"label": "Hidden prompt", "condition": "hidden",
         "is_caption": True},
        {"label": "Tell him", "condition": "visible",
         "is_caption": False},
    ]

    result = ns["_vnf_restore_raw_menu_captions"](
        choices, raw, lambda value: value)

    assert [item["label"] for item in result] == [
        "Should I tell him?\nHidden prompt", "Tell him",
    ]
    assert result[0]["caption"] is True
    assert evaluated == []
    assert '"is_caption": is_caption' in SHIM.read_text(encoding="utf-8")


def test_joined_raw_menu_caption_dedups_the_rendered_screen_payload():
    import threading
    import time
    import uuid

    module = parse_shim_python()
    client_node = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VNFBridgeClient"
    )
    namespace = {
        "os": __import__("os"), "threading": threading,
        "time": time, "uuid": uuid, "_vnf_log": lambda *args: None,
    }
    exec_shim_nodes([client_node], namespace)
    client = namespace["VNFBridgeClient"](types.SimpleNamespace())
    client._queue_post = lambda *_args, **_kwargs: {"ok": True}
    client.push_request(
        "choice_request",
        choices=["Tell him"],
        full_items=[
            {"label": "Caption A\nCaption B", "is_caption": True},
            {"label": "Tell him", "is_caption": False},
        ],
    )

    scraped = client._dedup_event({
        "type": "screen_content",
        "texts": ["Caption A\nCaption B", "Tell him"],
    })

    assert scraped["texts"] == ["Tell him"]


def test_character_say_before_a_menu_keeps_its_speaker():
    """A genuine say is a Say node at execution time — untouched."""
    callback, pushed = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who="marcus",
        last_say_what="Protocol says he has a point.",
        characters={"marcus": _FakeCharacter("Dr. Chen")},
    )

    callback("begin", True, what="Protocol says he has a point.")

    assert len(pushed) == 1
    ev = pushed[0]
    assert ev["type"] == "dialogue"
    assert ev["character"] == "Dr. Chen"
    assert ev["text"] == "Protocol says he has a point."
    assert "menu_caption" not in ev


def test_adv_callback_preserves_existing_outer_whitespace_contract():
    callback, pushed = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who=None,
        last_say_what="  Deliberate spacing.  ",
    )

    callback("begin", True, what="  Deliberate spacing.  ")

    assert pushed[0]["text"] == "  Deliberate spacing.  "


def test_adv_callback_preserves_whitespace_only_event_contract():
    callback, pushed = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who=None,
        last_say_what="   ",
    )

    callback("begin", True, what="   ")

    assert pushed[0]["text"] == "   "


def test_nvl_menu_caption_is_narration_too():
    """`nvl menu:` is the same ast.Menu path; only the mode differs."""
    callback, pushed, state = _character_callback_env(
        node=_FakeMenuNode(),
        last_say_who="marcus",
        last_say_what="You have that look.",
        characters={"marcus": _FakeCharacter("Dr. Chen")},
        mode="nvl",
        return_state=True,
    )

    callback("begin", False, what="How much do I tell him?")

    assert pushed == []
    ev = state.pending_menu_caption[0]
    assert ev["type"] == "narration"
    assert "character" not in ev
    assert ev["mode"] == "nvl"
    assert ev["text"] == "How much do I tell him?"
    assert ev["menu_caption"] is True


def test_interacting_say_under_a_menu_node_keeps_its_speaker():
    """Renamed from test_menu_redisplay_of_the_preceding_say_keeps_its_
    speaker (2026-08-15): its premise was wrong.  Neither engine re-says
    the preceding line under the Menu node -- ast.Menu.execute() narrates
    only its caption items -- so the case it named cannot occur.  What
    the assertion really pins is the interact discriminator: Menu.execute
    says ONLY with interact=False, so a say that arrives with
    interact=True while a Menu node is current did not come from the
    menu and must keep its speaker."""
    callback, pushed = _character_callback_env(
        node=_FakeMenuNode(),
        last_say_who="marcus",
        last_say_what="Protocol says he has a point.",
        characters={"marcus": _FakeCharacter("Dr. Chen")},
    )

    callback("begin", True, what="Protocol says he has a point.")

    assert len(pushed) == 1
    assert pushed[0]["type"] == "dialogue"
    assert pushed[0]["character"] == "Dr. Chen"


def test_menu_caption_repeating_the_previous_line_is_still_narration():
    """The 2026-08-15 review case.  The first cut of this guard escaped
    when the caption text EQUALLED the previous say's, on the theory that
    Ren'Py was re-displaying that say.  A caption is free to repeat the
    line before it -- "Should I tell him?" said aloud, then posed again
    as the choice prompt -- and the equality escape published the prompt
    as the previous speaker's dialogue, where the dedup could even
    swallow it whole.  Menu context plus interact=False plus the callback
    carrying its own `what` is authoritative; text equality is not."""
    callback, pushed, state = _character_callback_env(
        node=_FakeMenuNode(),
        last_say_who="marcus",
        last_say_what="Should I tell him?",
        characters={"marcus": _FakeCharacter("Dr. Chen")},
        return_state=True,
    )

    callback("begin", False, what="Should I tell him?")

    assert pushed == []
    ev = state.pending_menu_caption[0]
    assert ev["type"] == "narration"
    assert "character" not in ev
    assert ev["text"] == "Should I tell him?"
    assert ev["menu_caption"] is True


def test_say_captioned_menu_emits_exactly_one_attributed_line():
    """`menu:` with a `chen "..."` say menuitem, end to end.

    parser.parse_menu() compiles that line into its OWN ast.Say node
    (finish_say(..., interact=False)) placed before the Menu node, so it
    executes under a Say node and keeps its speaker.  The Menu node that
    follows adds no narration of its own; the only say it can raise is
    config.choice_empty_window's EMPTY one, whose blank `what` makes the
    shim fall back to _last_say_what -- the line just published.  Exactly
    one correctly attributed line: no duplicate, no loss."""
    callback, pushed, state = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who="marcus",
        last_say_what="Protocol says he has a point.",
        characters={"marcus": _FakeCharacter("Dr. Chen")},
        return_state=True,
    )

    # 1. The say menuitem, executing as its own non-interacting Say node.
    callback("begin", False, what="Protocol says he has a point.")

    assert len(pushed) == 1
    assert pushed[0]["type"] == "dialogue"
    assert pushed[0]["character"] == "Dr. Chen"
    assert "menu_caption" not in pushed[0]

    # 2. The Menu node, raising config.choice_empty_window's empty say.
    state.node = _FakeMenuNode()
    callback("begin", False, what="")

    assert len(pushed) == 1


def test_textless_menu_say_on_renpy_7_keeps_the_previous_speaker():
    """Ren'Py 7.x/6.x never pass `what` to character callbacks (7.5.2
    character.py: c("begin", interact=interact, type=type, **cb_args)),
    so a menu caption there arrives with no text of its own and `what`
    falls back to _last_say_what -- the PREVIOUS line.  Narrating that
    would change the dedup key and emit a phantom narration duplicate;
    keeping the speaker lets the dedup drop it."""
    callback, pushed, state = _character_callback_env(
        node=_FakeSayNode(),
        last_say_who="marcus",
        last_say_what="You have that look.",
        characters={"marcus": _FakeCharacter("Dr. Chen")},
        return_state=True,
    )

    callback("begin", True, what="You have that look.")
    assert len(pushed) == 1

    state.node = _FakeMenuNode()
    callback("begin", False)          # no `what` kwarg at all

    assert len(pushed) == 1


def test_auto_advance_never_fires_inside_a_menu_interaction():
    """2026-08-18 Echoes wait-door 5/5 repro.

    The periodic auto-advance dismisser guards menus by request id and by
    renpy.get_screen("choice") -- but NVL menus (and any custom menu
    screen) never show "choice", and during the wrapper's pacing window
    the request id is still None.  A menu opened directly from another
    menu's arm, with no say line in between to reset the stale-say timer,
    was therefore dismissed instantly with end_interaction(True): True ==
    1 picked the FIRST item, and the resolution carried no shim flag, so
    the wrapper logged it as a user choice.  The wrapper's context
    marker (_vnf_current_menu_context) is set before the interaction
    starts and cleared in its finally, so it is the one guard that covers
    every menu shape for the whole window -- pin that it sits between the
    choice-screen check and the stale-say dismissal.
    """
    source = SHIM.read_text(encoding="utf-8")

    start = source.index("    def _vnf_periodic_auto_advance():")
    end = source.index("\n    # Do NOT enable auto-advance during init", start)
    fn = source[start:end]

    choice_screen_pos = fn.index('renpy.get_screen("choice")')
    menu_ctx_pos = fn.index("if _vnf_current_menu_context[0] is not None:")
    dismiss_pos = fn.index("renpy.exports.end_interaction(True)")
    assert choice_screen_pos < menu_ctx_pos < dismiss_pos
    # The guard returns without dismissing.
    guard_block = fn[menu_ctx_pos:menu_ctx_pos + 80]
    assert "return" in guard_block


def test_auto_advance_refreshes_custom_overlay_guard_before_dismissal():
    """A just-opened call-screen must beat a stale say-line timer."""
    source = SHIM.read_text(encoding="utf-8")
    start = source.index("    def _vnf_periodic_auto_advance():")
    end = source.index("\n    # Do NOT enable auto-advance during init", start)
    fn = source[start:end]

    delay_pos = fn.index("if now - _vnf_auto_advance_say_time < _adv_delay:")
    refresh_pos = fn.index("if _vnf_refresh_transform_pause_reasons():")
    dismiss_pos = fn.index("renpy.exports.end_interaction(True)")
    assert delay_pos < refresh_pos < dismiss_pos


def _load_choice_merge_fns():
    source = SHIM.read_text(encoding="utf-8")
    start = source.index("    def _vnf_normalize_focus_label(label):")
    end = source.index("\n    def _vnf_push_game_state(", start)
    sliced = textwrap.dedent(source[start:end])
    # No renpy in the namespace: the normalizer's filter_text_tags call
    # raises NameError inside its try and the manual tag-strip fallback
    # runs — deterministic for the test.  _vnf_log is the merge's
    # identity-suppression trace; capture instead of printing.
    logged = []
    namespace = {
        "_vnf_log": logged.append,
        "_vnf_is_mapping": lambda value: isinstance(value, dict),
        "_vnf_is_sequence": lambda value: isinstance(value, (list, tuple)),
    }
    exec(compile(sliced, str(SHIM), "exec"), namespace)
    namespace["_test_logged"] = logged
    return namespace


def test_scraped_merge_dedups_by_action_identity_not_label_text():
    """2026-08-18 shim round: the scrape pipeline stops trusting label text.

    A drawn ChoiceReturn button whose value is already a value_map entry
    IS one of the pipeline choices, however a screen transform renders
    it.  Label equality alone appended it as a new item the moment the
    rendered text diverged from the menu caption — live-reproduced with
    the gutter-cost split, which stripped the cost parenthetical from
    the drawn label and produced phantom cost-less duplicates.
    """
    ns = _load_choice_merge_fns()
    merge = ns["_vnf_merge_scraped_choice_labels"]

    caption = "Give the dome the long stretch. (half an hour)"
    drawn = "Give the dome the long stretch."   # transform stripped the cost
    base = [{"label": caption, "index": 3, "caption": False, "disabled": False}]

    # Identity known -> the diverged render is NOT a new choice, and the
    # suppression leaves a trace in the log (silent absences are what
    # made the label-heuristic phantoms expensive to bisect).
    merged = merge(list(base), [drawn], {drawn: 3}, {drawn})
    assert [c["label"] for c in merged] == [caption]
    assert any("identity-dedup" in m for m in ns["_test_logged"])

    # Same diverged label with NO identity claim (a genuinely new widget
    # from a live menu re-filter) must still append — the identity skip
    # must not eat real additions.
    merged = merge(list(base), [drawn], {drawn: 7}, set())
    assert [c["label"] for c in merged] == [caption, drawn]
    assert merged[1]["index"] == 7


def test_scraped_merge_collapses_whitespace_so_newline_layouts_hold():
    """The normalizer's whitespace collapse is what makes a newline cost
    layout dedup-safe even before identity data is available (scraped
    labels can come from the plain choices list, which carries no action
    objects)."""
    ns = _load_choice_merge_fns()
    merge = ns["_vnf_merge_scraped_choice_labels"]

    caption = "Give the dome the long stretch. (half an hour)"
    drawn = "Give the dome the long stretch.\n(half an hour)"
    base = [{"label": caption, "index": 3, "caption": False, "disabled": False}]

    merged = merge(list(base), [drawn], {}, set())
    assert [c["label"] for c in merged] == [caption]


def test_disabled_return_buttons_stay_scraped_and_stale_drop_sees_them():
    """DISABLED-INTERACTION CONTRACT (2026-08-18): never actable, still
    listed.  Two shim-side halves, pinned together:

    1. The scraper used to bail (`return`) on an insensitive Return-class
       button, dropping the widget from the scrape entirely; it must fall
       through (`break`) so the button record keeps it visible with
       is_disabled True, while skipping the actable choice/value_map
       registration.
    2. The stale-drop must treat a disabled augmenter entry as rendered
       when ANY scraped button flagged is_disabled matches its label —
       actable ChoiceReturn labels alone can never contain a disabled
       widget (value-disabled menu items render with action None), which
       is exactly how Echoes' greyed one-shots were stale-dropped out of
       every game_state.
    """
    source = SHIM.read_text(encoding="utf-8")

    # Half 1: inside the Return-action branch, the disabled arm breaks
    # (falls through to the button record) instead of returning.
    ret_branch = source.index('if ("Return" in a_name or "returns" in a_str')
    branch = source[ret_branch:ret_branch + 1200]
    disabled_arm = branch.index("if _btn_disabled:")
    is_return_set = branch.index("is_return = True")
    arm = branch[disabled_arm:is_return_set]
    assert "break" in arm
    # No bare `return` STATEMENT in the arm (the prose mentions the old
    # one) — a return here is the regression that hid the widget.
    assert not re.search(r"^\s*return\s*$", arm, re.M)

    # Half 2: disabled ChoiceReturns never enter the enabled scraped-label
    # merge, but their widget identity still retains the canonical disabled
    # row when a screen transform changes its rendered text.
    ns = _load_choice_merge_fns()
    labels = ns["_vnf_actable_scraped_choice_labels"]
    rendered = ns["_vnf_disabled_choice_is_rendered"]
    merge = ns["_vnf_merge_scraped_choice_labels"]

    class ChoiceReturn:
        def __init__(self, value):
            self.value = value

    canonical = {
        "label": "Pay the toll. (too expensive)",
        "index": None,
        "caption": False,
        "disabled": True,
        "_choice_value": 7,
    }
    button = {
        "label": "Pay the toll.",
        "actions": ["ChoiceReturn"],
        "is_disabled": True,
        "_action_obj": ChoiceReturn(7),
    }
    scraped = labels([button], [])
    assert scraped == []
    assert rendered(canonical, [button]) is True
    assert merge([dict(canonical)], scraped, {}, set()) == [canonical]

    # A different disabled return value is not allowed to keep a stale row.
    button["_action_obj"] = ChoiceReturn(8)
    assert rendered(canonical, [button]) is False

    # Match the enabled identity hardening: None is not an identity and must
    # never retain an unrelated disabled row.
    canonical["_choice_value"] = None
    button["_action_obj"] = ChoiceReturn(None)
    assert rendered(canonical, [button]) is False


def test_pref_stash_survives_a_killed_session():
    """2026-08-18 live-reported fade loss: turbo/fast-forward mutate
    renpy.game.preferences, which PERSIST — a killed session left
    transitions=0/text_cps=0 for every later user session. The stash
    keeps the pre-mutation values in persistent data; init reconciles."""
    source = SHIM.read_text(encoding="utf-8")
    start = source.index("    def _vnf_stash_pref(")
    end = source.index("\n    # Reconcile at define time", start)
    sliced = textwrap.dedent(source[start:end])
    logged = []
    persistent = types.SimpleNamespace(_vnf_stashed_prefs=None)
    prefs = types.SimpleNamespace(text_cps=45, transitions=2, afm_enable=False)
    ns = {
        "renpy": types.SimpleNamespace(
            game=types.SimpleNamespace(persistent=persistent,
                                       preferences=prefs)),
        "_vnf_log": logged.append,
    }
    exec(compile(sliced, str(SHIM), "exec"), ns)

    # Turbo applies: stash, then mutate. First writer wins per key.
    ns["_vnf_stash_pref"]("transitions", 2)
    ns["_vnf_stash_pref"]("text_cps", 45)
    ns["_vnf_stash_pref"]("text_cps", 0)      # later writer must NOT win
    prefs.transitions = 0
    prefs.text_cps = 0
    # Session killed here -- in-memory restore never runs. Next init:
    ns["_vnf_reconcile_stashed_prefs"]()
    assert prefs.transitions == 2
    assert prefs.text_cps == 45
    assert persistent._vnf_stashed_prefs is None

    # Clean cycle: stash + restore + unstash leaves nothing behind.
    ns["_vnf_stash_pref"]("transitions", 2)
    prefs.transitions = 0
    prefs.transitions = 2
    ns["_vnf_unstash_pref"]("transitions")
    assert persistent._vnf_stashed_prefs is None

    # Values alone carry no provenance. Both are valid player preferences,
    # so an empty stash must leave them exactly as the player selected them.
    prefs.transitions = 0
    prefs.text_cps = 0
    ns["_vnf_reconcile_stashed_prefs"]()
    assert prefs.transitions == 0
    assert prefs.text_cps == 0


def test_back_refuses_to_resolve_a_live_choice_menu():
    """`back` must never send a naked Return() into a live menu.

    Ren'Py resolves the MENU with a queued Return — it PICKS an option
    (resolved_by=shim) and runs that arm's side effects. Run f2-moralist
    (2026-08-19) tripled its storage inventory this way: three back calls,
    three re-executions of the collection arm, 10 minutes each, duplicated
    items persisting to the finale (bridge/logs 102243, seqs 1868-1949).
    Same family as the wait-door auto-advance menu guard.
    """
    src = SHIM.read_text(encoding="utf-8")
    start = src.index("def _vnf_cmd_back")
    end = src.index("def _vnf_cmd_inspect", start)
    body = src[start:end]
    # Only Ren'Py's game-menu wrapper has a generic Return contract. Unknown
    # custom call screens must fail closed before the live-choice and generic
    # fallback paths.
    assert "_vnf_has_modal_overlay()" in body
    assert "_vnf_has_transient_custom_screen()" in body
    assert "_vnf_is_generic_game_menu_showing(allow_main_menu=True)" in body
    assert "if _generic_game_menu:" in body
    assert "elif _called_screen and not _covered_overlay:" in body
    assert 'not in (None, "choice", "nvl")' in body
    assert "A custom modal is active" in body
    assert "elif _vnf_current_menu_context[0]:" in body
    assert "'back' would resolve" in body
    # Naked Return survives for the game-menu case and the no-modal fallback.
    assert "Return()" in body
    custom_guard_pos = body.index("A custom modal is active")
    called_guard_pos = body.index("elif _called_screen and not _covered_overlay:")
    registered_overlay_pos = body.index("elif _back_target:")
    choice_guard_pos = body.index("elif _vnf_current_menu_context[0]:")
    return_positions = [
        index for index in range(len(body))
        if body.startswith("_vnf_native_action_queue = Return()", index)
    ]
    assert len(return_positions) == 2
    assert called_guard_pos < registered_overlay_pos
    assert (return_positions[0] < custom_guard_pos < choice_guard_pos
            < return_positions[1])


def _run_cmd_back(*, called_screen=None, modal=None, generic_game_menu=False,
                  screens=(), registered=(), passive=(), panel_lookup=None):
    """Execute the real ``_vnf_cmd_back`` with the branch-selecting probes
    stubbed, so the pushed ``command_result`` reflects the actual message
    text agents see -- not just a static substring check."""
    events = []
    ns = load_shim_functions(
        "_vnf_cmd_back",
        namespace={
            "_vnf_native_action_queue": None,
            "_vnf_client": types.SimpleNamespace(push_event=events.append),
            "_vnf_get_showing_screens": lambda: screens,
            "_vnf_overlay_screens": set(registered),
            "_vnf_passive_overlay_screens": set(passive),
            "renpy": types.SimpleNamespace(exports=types.SimpleNamespace(
                get_screen=panel_lookup or dict(screens).get)),
            "_vnf_has_modal_overlay": lambda: modal,
            "_vnf_has_transient_custom_screen": lambda: called_screen,
            "_vnf_is_generic_game_menu_showing": lambda **kwargs: generic_game_menu,
            "_vnf_current_menu_context": [None],
            "_vnf_text": lambda v, default=u"": str(v),
            "Return": lambda *a, **k: ("Return", a, k),
            "Hide": lambda *a, **k: ("Hide", a, k),
        },
    )
    ns["_vnf_cmd_back"]("back", {})
    return events


def test_back_on_a_called_screen_names_it_and_points_at_rewind():
    """Fleet R66: back() refused 4/4 times on this game's `call screen`
    consoles (byte-identical error, no screen name, no next step).  The
    refusal itself is correct -- see the class comment above -- but the
    message must now name the screen and point at rewind() as the tool
    that DOES roll back through it."""
    events = _run_cmd_back(called_screen="observatory_hud")

    assert len(events) == 1
    result = events[0]
    assert result["success"] is False
    assert "observatory_hud" in result["error"]
    assert "act() on its visible Close or Release control" in result["error"]
    assert "rewind()" in result["error"]
    assert "moves the story backward" in result["error"]


@pytest.mark.parametrize("tag", ["equipment_screen", "evidence_screen"])
def test_back_hides_registered_panel_without_resolving_underlying_call(tag):
    caller = types.SimpleNamespace(transient=True)
    panel = types.SimpleNamespace(transient=False)
    # Enumeration order must not pick the called map as the Hide target.
    events = _run_cmd_back(
        called_screen="observatory_map", modal=tag,
        screens=[(tag, panel), ("observatory_map", caller)],
        registered=[tag, "observatory_map"])
    assert events[0]["success"] is True
    assert events[0]["note"] == "Hide '{}' queued.".format(tag)
    assert _run_cmd_back(called_screen="observatory_map")[0]["success"] is False


@pytest.mark.parametrize("transient,registered,passive", [
    (True, True, False), (False, False, False), (False, True, True),
])
def test_back_does_not_hide_unproven_panel_over_called_screen(
        transient, registered, passive):
    events = _run_cmd_back(
        called_screen="observatory_map", modal="panel",
        screens=[("panel", types.SimpleNamespace(transient=transient))],
        registered=["panel"] if registered else [],
        passive=["panel"] if passive else [])
    assert events[0]["success"] is False


def test_back_panel_then_called_map_with_real_screen_probes():
    events = []
    screens = {
        "observatory_map": types.SimpleNamespace(transient=True, modal=False),
        "equipment_screen": types.SimpleNamespace(transient=False, modal=True),
    }
    ns = load_shim_functions(
        "_vnf_cmd_back", "_vnf_has_modal_overlay",
        "_vnf_has_transient_custom_screen", "_vnf_is_generic_game_menu_showing",
        namespace={
            "_vnf_native_action_queue": None,
            "_vnf_client": types.SimpleNamespace(push_event=events.append),
            "_vnf_get_showing_screens": lambda: list(screens.items()),
            "_vnf_overlay_screens": set(screens),
            "_vnf_passive_overlay_screens": set(),
            "_vnf_current_menu_context": [None],
            "renpy": types.SimpleNamespace(
                store=types.SimpleNamespace(main_menu=False),
                context=lambda: types.SimpleNamespace(_menu=False),
                exports=types.SimpleNamespace(
                    get_screen=screens.get,
                    current_interact_type=lambda: "screen")),
            "Return": lambda: ("Return",),
            "Hide": lambda tag: ("Hide", tag),
        })
    ns["_vnf_cmd_back"]("back", {"overlays_only": True})
    assert ns["_vnf_native_action_queue"] == ("Hide", "equipment_screen")
    assert events[-1]["success"] is True
    del screens["equipment_screen"]
    ns["_vnf_native_action_queue"] = None
    ns["_vnf_cmd_back"]("back", {"overlays_only": True})
    assert ns["_vnf_native_action_queue"] is None
    assert events[-1]["success"] is False
    assert "observatory_map" in events[-1]["error"]


def test_back_on_a_custom_modal_names_it_and_points_at_rewind():
    events = _run_cmd_back(modal="custom_confirm_screen")

    assert len(events) == 1
    result = events[0]
    assert result["success"] is False
    # The literal phrase existing tests pin must survive verbatim.
    assert "A custom modal is active" in result["error"]
    assert "custom_confirm_screen" in result["error"]
    assert "rewind()" in result["error"]
    assert "moves the story backward" in result["error"]


def test_nonmodal_called_screen_is_detected_by_transient_ownership():
    src = SHIM.read_text(encoding="utf-8")
    start = src.index("def _vnf_has_transient_custom_screen")
    end = src.index("def _vnf_finish_observation", start)
    body = src[start:end]

    assert 'getattr(_scr, "transient", False)' in body
    assert 'getattr(_sd, "transient", False)' in body
    assert 'renpy.exports.current_interact_type() == "screen"' in body
    assert '_tag in ("choice", "nvl")' in body
    assert "and not _called_interaction" in body
    assert '_tag == "menu" and _generic_game_menu' in body
    assert 'if _tag in ("menu", "choice", "nvl")' not in body


def test_generic_game_menu_requires_renpy_menu_context():
    src = SHIM.read_text(encoding="utf-8")
    start = src.index("def _vnf_is_generic_game_menu_showing")
    end = src.index("def _vnf_has_modal_overlay", start)
    body = src[start:end]

    # A custom ``call screen`` may legally declare ``tag menu``. Only the
    # dedicated game-menu context gives that tag Ren'Py's generic Return
    # contract; outside it, transient ownership must fail closed.
    assert 'getattr(renpy.context(), "_menu", False)' in body
    assert 'renpy.exports.current_interact_type() == "screen"' in body
    assert '_tag == "menu"' in body

    context = types.SimpleNamespace(_menu=False)
    interact_type = [None]
    screen = types.SimpleNamespace(transient=True)
    screens = [("menu", screen)]
    renpy = types.SimpleNamespace(
        store=types.SimpleNamespace(main_menu=False),
        context=lambda: context,
        exports=types.SimpleNamespace(
            get_screen=lambda _tag: None,
            current_interact_type=lambda: interact_type[0],
        ),
    )
    ns = load_shim_functions(
        "_vnf_is_generic_game_menu_showing",
        "_vnf_has_transient_custom_screen",
        namespace={
            "renpy": renpy,
            "_vnf_get_showing_screens": lambda: screens,
        },
    )

    assert not ns["_vnf_is_generic_game_menu_showing"]()
    assert ns["_vnf_has_transient_custom_screen"]() == "menu"

    context._menu = True
    assert ns["_vnf_is_generic_game_menu_showing"]()
    assert ns["_vnf_has_transient_custom_screen"]() is None

    renpy.store.main_menu = True
    assert not ns["_vnf_is_generic_game_menu_showing"]()
    assert ns["_vnf_is_generic_game_menu_showing"](allow_main_menu=True)
    renpy.exports.get_screen = lambda tag: screen if tag == "main_menu" else None
    assert not ns["_vnf_is_generic_game_menu_showing"](allow_main_menu=True)
    renpy.exports.get_screen = lambda tag: None
    interact_type[0] = "screen"
    assert not ns["_vnf_is_generic_game_menu_showing"](allow_main_menu=True)
    renpy.store.main_menu = False

    interact_type[0] = "screen"
    assert not ns["_vnf_is_generic_game_menu_showing"]()
    assert ns["_vnf_has_transient_custom_screen"]() == "menu"

    for reserved_tag in ("choice", "nvl"):
        screens[:] = [(reserved_tag, screen)]
        assert ns["_vnf_has_transient_custom_screen"]() == reserved_tag

    interact_type[0] = "menu"
    for builtin_tag in ("choice", "nvl"):
        screens[:] = [(builtin_tag, screen)]
        assert ns["_vnf_has_transient_custom_screen"]() is None


def test_screen_scrape_carries_explicit_main_menu_provenance():
    src = SHIM.read_text(encoding="utf-8")
    start = src.index("def _vnf_scrape_visible_screens")
    body = src[start:]

    assert "main_menu=bool(_at_mm)" in body
    assert "bool(_at_mm)," in body
    assert 'main_menu=ev.get("main_menu", False)' in body


def test_orphan_choice_resync_does_not_bypass_deferred_pacing():
    source = SHIM.read_text(encoding="utf-8")
    start = source.index("        # --- Orphan choice detection ---")
    end = source.index("        if vnf_player.debug:", start)
    orphan_block = source[start:end]

    assert (
        "and _vnf_request.request_id is None\n"
        "                    and _vnf_deferred_choice[0] is None\n"
        "                    and _vnf_current_menu_context[0] is not None"
        in orphan_block
    )


def test_modern_overlay_self_heals_missing_command_poller_screen():
    module = parse_shim_python()
    overlay = function_node(module, "_vnf_overlay")
    shown = set()
    calls = []

    def show_screen(name):
        calls.append(name)
        shown.add(name)

    namespace = {
        "renpy": types.SimpleNamespace(
            get_screen=lambda name: name if name in shown else None,
            exports=types.SimpleNamespace(show_screen=show_screen),
        ),
        "vnf_player": types.SimpleNamespace(enabled=False, debug=False),
    }
    exec(
        compile(ast.Module(body=[overlay], type_ignores=[]), SHIM, "exec"),
        namespace,
    )
    run_overlay = namespace["_vnf_overlay"]

    run_overlay()
    assert calls == []

    namespace["vnf_player"].enabled = True
    shown.add("vnf_command_poller")
    run_overlay()
    assert calls == []

    shown.remove("vnf_command_poller")
    run_overlay()
    run_overlay()
    assert calls == ["vnf_command_poller"]

    shown.remove("vnf_command_poller")
    run_overlay()
    assert calls == ["vnf_command_poller", "vnf_command_poller"]

    registrations = []
    for node in ast.walk(module):
        if not isinstance(node, ast.If):
            continue
        if not (
            isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name)
            and node.test.operand.id == "_is_renpy6"
        ):
            continue
        for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if not isinstance(child, ast.Call) or not child.args:
                continue
            if not (
                isinstance(child.func, ast.Attribute)
                and child.func.attr == "append"
                and isinstance(child.args[0], ast.Name)
                and child.args[0].id == "_vnf_overlay"
            ):
                continue
            registrations.append(child)
    assert len(registrations) == 1


def test_host_pointer_motion_can_be_disabled_for_unattended_runs():
    src = SHIM.read_text(encoding="utf-8")

    assert "self.move_host_pointer = True" in src
    tracker_start = src.index("def is_user_active(self):")
    tracker_end = src.index("_vnf_mouse = _VNFMouseTracker()", tracker_start)
    assert "if not vnf_player.move_host_pointer:" in src[
        tracker_start:tracker_end]

    wrapper_start = src.index("def _vnf_move_mouse")
    wrapper_end = src.index("_vnf_native_action_queue", wrapper_start)
    wrapper = src[wrapper_start:wrapper_end]
    assert "if not vnf_player.move_host_pointer:" in wrapper
    assert "if _is_renpy6:" in wrapper
    assert "return _vnf_orig_set_mouse_pos" in wrapper
    assert "return _vnf_set_mouse_pos" in wrapper

    # Every shim-owned park calls the guarded helper directly. This matters on
    # Ren'Py 6, where replacing renpy.exports.set_mouse_pos is intentionally
    # skipped for compatibility.
    assert src.count("_vnf_move_mouse(") == 6


def test_mouse_drift_baseline_resets_after_timeline_recovery():
    src = SHIM.read_text(encoding="utf-8")

    assert "def sync(self):" in src
    assert src.count("_vnf_mouse.sync()") >= 4

    tracker = src[src.index("def is_user_active(self):"):]
    tracker = tracker.split("_vnf_mouse = _VNFMouseTracker()", 1)[0]
    assert "_vnf_exception.exception_flag" in tracker
    assert 'getattr(renpy.game, "after_rollback", False)' in tracker
    assert "self.sync()" in tracker

    exception = src[src.index("def _vnf_exception_handler"):]
    assert exception.index("_vnf_mouse.sync()") < exception.index(
        "_vnf_original_exception_handler")

    after_load = src[src.index("def _vnf_after_load_callback"):]
    assert "_vnf_mouse.sync()" in after_load.split(
        "if hasattr(renpy.config, \"after_load_callbacks\")", 1)[0]

    rollback = src[src.index("def _vnf_finish_rollback_resume"):]
    rollback = rollback.split(
        "def _vnf_rollback_resume_interact_callback", 1)[0]
    assert "_vnf_mouse.sync()" in rollback


def test_exception_detection_edge_is_consumed_after_recovery():
    tree = parse_shim_python()
    state_class = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name == "_VNFExceptionState"
    )
    namespace = {}
    exec(compile(ast.Module(body=[state_class], type_ignores=[]),
                 str(SHIM), "exec"), namespace)
    state = namespace["_VNFExceptionState"]()

    state.exception_flag = True
    assert state.consume_detected(False)
    assert not state.exception_flag
    state.error_notified = True
    assert not state.consume_detected(False)
    assert not state.error_notified

    assert state.consume_detected(True)
    assert not state.exception_flag


# ---------------------------------------------------------------------------
# Modal overlay PRESENTATION
#
# Blocking says "this screen owns input".  Modal says "the player sees this
# INSTEAD of the scene", which is a different question and needs its own
# declaration: Roadwarden's journal is blocking but sits beside its dialogue,
# while Echoes' LOG/KIT/MAP paint a full-screen scrim over the observatory.
# The consumer needs the second fact to know whether to layer the panel's rows
# over the scene or present the panel as the whole surface.
# ---------------------------------------------------------------------------


def _overlay_registry_functions():
    return load_shim_functions(
        "_vnf_register_overlay_screen",
        "_vnf_set_overlay_presentation",
        "_vnf_visible_modal_overlay_tags",
        namespace={
            "_vnf_overlay_screens": set(),
            "_vnf_passive_overlay_screens": set(),
            "_vnf_retained_overlay_screens": set(),
            "_vnf_modal_overlay_screens": set(),
        },
    )


def test_overlay_registration_defaults_to_layered_presentation():
    """Every existing mod registration must keep its current behavior."""
    ns = _overlay_registry_functions()

    ns["_vnf_register_overlay_screen"]("journal")
    ns["_vnf_register_overlay_screen"](
        "echo_terminal_live", blocking=False, retain_generation=True)

    assert ns["_vnf_overlay_screens"] == {"journal", "echo_terminal_live"}
    assert ns["_vnf_passive_overlay_screens"] == {"echo_terminal_live"}
    assert ns["_vnf_retained_overlay_screens"] == {"echo_terminal_live"}
    # The new registry stays empty: no declaration, no modal presentation.
    assert ns["_vnf_modal_overlay_screens"] == set()


def test_modal_registration_declares_presentation_and_implies_blocking():
    ns = _overlay_registry_functions()

    ns["_vnf_register_overlay_screen"]("evidence_screen", modal=True)

    assert ns["_vnf_modal_overlay_screens"] == {"evidence_screen"}
    assert ns["_vnf_overlay_screens"] == {"evidence_screen"}
    # A modal panel owns input by definition; it can never be passive.
    assert ns["_vnf_passive_overlay_screens"] == set()


def test_modal_and_non_blocking_together_resolve_to_blocking():
    """A contradictory registration must not leave a passive modal."""
    ns = _overlay_registry_functions()

    ns["_vnf_register_overlay_screen"](
        "evidence_screen", blocking=False, modal=True)

    assert ns["_vnf_passive_overlay_screens"] == set()
    assert ns["_vnf_modal_overlay_screens"] == {"evidence_screen"}


def test_re_registering_without_modal_restores_layered_presentation():
    ns = _overlay_registry_functions()

    ns["_vnf_register_overlay_screen"]("evidence_screen", modal=True)
    ns["_vnf_register_overlay_screen"]("evidence_screen")

    assert ns["_vnf_modal_overlay_screens"] == set()


def test_set_overlay_presentation_toggles_modal_without_re_registering():
    ns = _overlay_registry_functions()

    ns["_vnf_register_overlay_screen"](
        "echo_terminal_live", blocking=False, retain_generation=True)
    ns["_vnf_set_overlay_presentation"]("evidence_screen", "modal")

    assert ns["_vnf_modal_overlay_screens"] == {"evidence_screen"}
    assert "evidence_screen" in ns["_vnf_overlay_screens"]
    # Retention is a separate contract and must survive a mode change.
    ns["_vnf_set_overlay_presentation"]("echo_terminal_live", "layered")
    assert ns["_vnf_retained_overlay_screens"] == {"echo_terminal_live"}
    assert ns["_vnf_passive_overlay_screens"] == {"echo_terminal_live"}

    ns["_vnf_set_overlay_presentation"]("evidence_screen", "layered")
    assert ns["_vnf_modal_overlay_screens"] == set()


def test_set_overlay_presentation_rejects_an_unknown_mode():
    """A typo must not silently leave the old presentation contract."""
    ns = _overlay_registry_functions()

    with pytest.raises(ValueError):
        ns["_vnf_set_overlay_presentation"]("evidence_screen", "blocking")


def test_visible_modal_tags_report_screen_order_and_only_shown_panels():
    ns = _overlay_registry_functions()
    per_screen = [
        {"_tag": "observatory_map"},
        {"_tag": "echo_terminal_live"},
        {"_tag": "evidence_screen"},
        {"_tag": "evidence_screen"},
    ]

    tags = ns["_vnf_visible_modal_overlay_tags"](
        per_screen, set(["evidence_screen", "star_map_screen"]))

    # star_map_screen is registered modal but is NOT on screen: an agent must
    # never be told a panel it cannot see owns the surface.
    assert tags == ["evidence_screen"]


def test_visible_modal_tags_are_empty_without_declarations():
    ns = _overlay_registry_functions()

    assert ns["_vnf_visible_modal_overlay_tags"](
        [{"_tag": "journal"}, {"_tag": "nvl"}], set()) == []
    assert ns["_vnf_visible_modal_overlay_tags"](None, set()) == []


def test_modal_overlay_presentation_helpers_are_py2_compatible():
    """These run on Ren'Py 6/7 (Python 2): no f-strings, no walrus."""
    module = parse_shim_python()
    for name in ("_vnf_register_overlay_screen",
                 "_vnf_set_overlay_presentation",
                 "_vnf_visible_modal_overlay_tags"):
        node = function_node(module, name)
        assert not [n for n in ast.walk(node) if isinstance(n, ast.JoinedStr)]
        assert not [
            n for n in ast.walk(node)
            if isinstance(n, getattr(ast, "NamedExpr", ()))
        ]


def test_modal_overlay_screens_reach_both_actionable_payloads():
    """Wiring check: the additive field rides screen_content AND game_state.

    game_state is the canonical actionable surface, so the act/settle
    projections need the modal tags there to see the panel open and close as a
    real surface change; screen_content is what the format layer reads to
    decide the panel is the primary surface.
    """
    source = SHIM.read_text(encoding="utf-8")

    assert "_vnf_modal_overlay_screens = set()" in source
    assert 'ev["modal_overlay_screens"] = _ov_modal' in source
    assert '_gs["modal_overlay_screens"] = _gs_modal_overlays' in source
    assert source.count("_vnf_visible_modal_overlay_tags(") == 3


def test_modal_overlay_field_needs_no_protocol_bump():
    """The field is additive at the event level, which nothing validates.

    The shim schema pins ITEM shapes (interactions, buttons, choices) and the
    choice-request payload.  Screen-level event keys such as overlay_screens
    and overlay_retained_screens appear in no frozenset and are read with
    .get(), so a new sibling key cannot break an older bridge.
    """
    screen_level_keys = {
        "overlay_screens", "overlay_texts", "overlay_texts_by_screen",
        "overlay_retained_screens", "overlay_generations",
        "modal_overlay_screens",
    }
    validated = (
        SHIM_INTERACTION_FIELDS
        | SHIM_GAME_STATE_BUTTON_FIELDS
        | SHIM_REQUEST_BUTTON_FIELDS
        | SHIM_REQUEST_CHOICE_FIELDS
        | SHIM_CHOICE_REQUEST_PAYLOAD_FIELDS
        | ACTIONABLE_ITEM_FIELDS
    )
    assert not screen_level_keys & validated
    assert SHIM_PROTOCOL_VERSION == 1


def test_load_handler_treats_renpy8_unfreeze_as_a_successful_load():
    """Ren'Py 8's renpy.load() raises rollback.UnfreezeException, a
    BaseException outside CONTROL_EXCEPTIONS; the handler caught only
    _CONTROL_EXCEPTIONS, so every 8.x load went unconfirmed and skipped
    the after-load reset."""
    src = (Path(__file__).resolve().parents[1] / "vnflight.rpy").read_text(encoding="utf-8")
    assert "_VNF_LOAD_SUCCESS_EXCEPTIONS = tuple(" in src
    assert 'getattr(getattr(renpy, "rollback", None), "UnfreezeException", None)' in src
    assert 'getattr(renpy.game, "RestartTopContext", None)' in src
    start = src.index("def _vnf_cmd_load(cmd_name, cmd_args):")
    body = src[start:src.index("_vnf_add_command_handler(\"save\"", start)]
    assert "except BaseException as _load_e:" in body
    assert "_vnf_is_load_success_exception(_load_e)" in body
    assert "except _CONTROL_EXCEPTIONS:" not in body
    # The name check backs the isinstance tuple when the module path is
    # not resolvable at shim init.
    assert 'type(exc).__name__ in ("UnfreezeException", "RestartTopContext")' in src


def test_shim_treats_quick_menu_file_actions_as_navigation():
    src = (Path(__file__).resolve().parents[1] / "vnflight.rpy").read_text(encoding="utf-8")
    assert '"FileLoad", "FileSave", "FileTakeScreenshot",' in src
    start = src.index("def _vnf_categorize_action(a):")
    body = src[start:src.index("def _vnf_build_interactions(", start)]
    assert "_VNF_QUICK_FILE_ACTIONS" in body
    assert "screen not in _VNF_FILE_SLOT_SCREENS" in body
