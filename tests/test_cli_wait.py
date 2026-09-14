import pytest
import os
import sys
import json
from argparse import Namespace
from unittest.mock import ANY

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from vnflight import cli
from vnflight.client import drain_stale_pending_request
from vnflight.cli import _freshen_choice_request_events


class FakeScreenSession:
    def __init__(self, screen):
        self.screen = screen

    def _get(self, path, timeout=2.0):
        if path == "/screen":
            return 200, {"screen": self.screen}
        return 404, {}

    def get_transcript(self, last_n=30):
        return []


class FakeClientState:
    def set_cursor(self, key, cursor):
        pass

    def set_last_request_id(self, key, request_id):
        pass

    def save(self):
        pass


def test_cmd_launch_binds_followup_display_to_created_slot(monkeypatch, capsys):
    class FakeSession:
        bridge_url = "http://bridge"
        slot_prefix = "/42"

        def state(self):
            return {"context": {"context": "main_menu", "buttons": []}}

    seen_slots = []

    def fake_make_session(args, state):
        seen_slots.append(getattr(args, "target_slot", None))
        return FakeSession()

    monkeypatch.setattr(
        cli,
        "launch_game",
        lambda *args, **kwargs: (True, "Launched 'game' (slot 42)", 42),
    )
    monkeypatch.setattr(cli, "_make_session", fake_make_session)
    monkeypatch.setattr(cli, "_auto_apply_default_profile", lambda *args: None)
    monkeypatch.setattr(cli, "_save_session", lambda *args: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    args = Namespace(
        game="game",
        bridge="http://bridge",
        games_dir=None,
        fast_forward=False,
        auto=False,
        timeout=1,
        save_slot=None,
        quiet=False,
        json=True,
        wait=False,
        target_slot=None,
    )

    assert cli.cmd_launch(args, FakeClientState()) == 0
    assert seen_slots == ["42"]
    assert args.target_slot == "42"


def test_cmd_launch_quiet_applies_profile_without_cosmetic_state_poll(
    monkeypatch, capsys,
):
    monkeypatch.setattr(
        cli,
        "launch_game",
        lambda *args, **kwargs: (True, "Launched 'game' (slot 42)", 42),
    )
    session = object()
    profile_calls = []
    monkeypatch.setattr(cli, "_make_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(cli, "_has_default_profile", lambda args: True)
    monkeypatch.setattr(
        cli,
        "_auto_apply_default_profile",
        lambda args, seen_session, state, emit=True: (
            profile_calls.append((seen_session, emit))
            or {"profile_applied": "turbo"}
        ),
    )
    args = Namespace(
        game="game",
        bridge="http://bridge",
        games_dir=None,
        fast_forward=False,
        auto=False,
        timeout=1,
        save_slot=None,
        quiet=True,
        json=True,
        wait=False,
        target_slot=None,
        token=None,
    )

    assert cli.cmd_launch(args, FakeClientState()) == 0
    assert args.target_slot == "42"
    assert profile_calls == [(session, False)]
    assert json.loads(capsys.readouterr().out) == {
        "success": True,
        "message": "Launched 'game' (slot 42)",
        "slot_id": 42,
        "profile_applied": "turbo",
    }


def test_cmd_launch_quiet_wait_applies_default_profile_once(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "launch_game",
        lambda *args, **kwargs: (True, "Launched 'game' (slot 42)", 42),
    )
    session = object()
    profile_calls = []
    monkeypatch.setattr(cli, "_make_session", lambda *args: session)
    monkeypatch.setattr(
        cli, "_auto_apply_default_profile",
        lambda *args, **kwargs: (
            profile_calls.append(kwargs.get("emit", True))
            or {"profile_applied": "turbo"}
        ),
    )
    monkeypatch.setattr(cli, "_perform_wait", lambda *args: 0)
    monkeypatch.setattr(cli, "_has_default_profile", lambda args: True)
    args = Namespace(
        game="game", bridge="http://bridge", games_dir=None,
        fast_forward=False, auto=False, timeout=1, save_slot=None,
        quiet=True, json=True, wait=True, target_slot=None, token=None,
    )

    assert cli.cmd_launch(args, FakeClientState()) == 0
    assert profile_calls == [False]
    # A --wait launch now prints a second, trailing JSON object carrying
    # first_choice_after_s (the --wait phase's own elapsed time) after the
    # launch receipt -- decode just the receipt here.
    receipt = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    assert receipt["profile_applied"] == "turbo"


def test_cmd_launch_deferred_profile_does_not_create_session(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "launch_game",
        lambda *args, **kwargs: (True, "Launched 'game' (slot 42)", 42),
    )
    monkeypatch.setattr(
        cli, "_make_session",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("deferred profile must not create a session")
        ),
    )
    args = Namespace(
        game="game", bridge="http://bridge", games_dir=None,
        fast_forward=False, auto=True, timeout=1, save_slot=None,
        quiet=True, json=True, wait=False, target_slot=None, token=None,
        defer_default_profile=True,
    )

    assert cli.cmd_launch(args, FakeClientState()) == 0
    assert json.loads(capsys.readouterr().out)["slot_id"] == 42


def test_cmd_launch_preserves_explicit_zero_timeout(monkeypatch):
    seen = []

    def fake_launch_game(*args, **kwargs):
        seen.append(kwargs.get("connect_timeout"))
        return False, "Launch timeout must be greater than zero.", None

    monkeypatch.setattr(cli, "launch_game", fake_launch_game)
    args = Namespace(
        game="game",
        bridge="http://bridge",
        games_dir=None,
        fast_forward=False,
        auto=False,
        timeout=0,
        save_slot=None,
        quiet=True,
        json=True,
        wait=False,
        target_slot=None,
        token=None,
    )

    assert cli.cmd_launch(args, FakeClientState()) == 1
    assert seen == [0]


def test_cmd_wait_marks_current_events_seen_for_explicit_slot(monkeypatch):
    class FakeSession:
        def __init__(self):
            self.mark_calls = 0

        def mark_current_events_seen(self):
            self.mark_calls += 1
            return True

        def attach_to_running_slot(self, *, warn=None):
            return self.mark_current_events_seen()

    session = FakeSession()
    calls = []
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(
        cli,
        "_perform_wait",
        lambda session, args, state, timeout: calls.append((session, timeout)) or 0,
    )

    args = Namespace(target_slot="23", timeout=5)

    assert cli.cmd_wait(args, FakeClientState()) == 0
    assert session.mark_calls == 1
    assert calls == [(session, 5)]


def test_cmd_wait_does_not_mark_current_events_seen_without_explicit_slot(
    monkeypatch,
):
    class FakeSession:
        def __init__(self):
            self.mark_calls = 0

        def mark_current_events_seen(self):
            self.mark_calls += 1
            return True

        def attach_to_running_slot(self, *, warn=None):
            return self.mark_current_events_seen()

    session = FakeSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(cli, "_perform_wait", lambda *args: 0)

    args = Namespace(target_slot=None, timeout=5)

    assert cli.cmd_wait(args, FakeClientState()) == 0
    assert session.mark_calls == 0


def test_cmd_wait_traces_failed_mark_current_events_seen(
    monkeypatch,
    capsys,
):
    class FakeSession:
        def mark_current_events_seen(self):
            return False

        def attach_to_running_slot(self, *, warn=None):
            if warn:
                warn("returned_false")
            return False

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeSession())
    monkeypatch.setattr(cli, "_perform_wait", lambda *args: 0)

    args = Namespace(target_slot="23", timeout=5, trace_wait=True)

    assert cli.cmd_wait(args, FakeClientState()) == 0
    err = capsys.readouterr().err
    assert "mark_current_events_seen_failed" in err
    assert "returned_false" in err


def test_cmd_wait_traces_mark_current_events_seen_exception(
    monkeypatch,
    capsys,
):
    class FakeSession:
        def mark_current_events_seen(self):
            raise RuntimeError("bridge unavailable")

        def attach_to_running_slot(self, *, warn=None):
            if warn:
                warn("bridge unavailable")
            return False

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeSession())
    monkeypatch.setattr(cli, "_perform_wait", lambda *args: 0)

    args = Namespace(target_slot="23", timeout=5, trace_wait=True)

    assert cli.cmd_wait(args, FakeClientState()) == 0
    err = capsys.readouterr().err
    assert "mark_current_events_seen_failed" in err
    assert "bridge unavailable" in err


def test_screen_event_prompt_ignores_default_focus_list_chrome():
    event = {
        "type": "screen_content",
        "buttons": [
            {
                "label": "Back",
                "screen": "_focus_list",
                "actions": ["Rollback"],
            },
            {
                "label": "Q.Save",
                "screen": "_focus_list",
                "actions": ["FileTakeScreenshot", "FileSave"],
            },
            {
                # QuickLoad() is a FileLoad on the quick page; this one
                # button used to make every wait return at once.
                "label": "Q.Load",
                "screen": "_focus_list",
                "actions": ["FileLoad"],
            },
        ],
        "interactions": [
            {
                "display_label": "History",
                "screen": "_focus_list",
                "action_names": ["ShowMenu"],
                "type": "nav",
                "category": "navigation",
            },
            {
                "display_label": "Skip",
                "screen": "_focus_list",
                "action_names": ["Skip"],
                "type": "other",
                "category": "other",
            },
        ],
    }

    assert cli._screen_event_has_actionable_prompt(event) is False


def test_public_screen_payload_filters_focus_chrome_but_keeps_real_actions():
    buttons = [
        {"label": "History", "screen": "_focus_list", "actions": ["ShowMenu"]},
        {"label": "Talk", "screen": "_focus_list", "actions": ["ShowMenu"]},
        {"label": "Back", "screen": "_focus_list", "actions": ["Return"]},
        {"label": "Raw", "screen": "modal", "_displayable": object()},
    ]
    interactions = [
        {
            "display_label": "Skip",
            "screen": "_focus_list",
            "action_names": ["Skip"],
        },
        {
            "display_label": "Talk",
            "screen": "_focus_list",
            "action_names": ["ShowMenu"],
            "_raw": "hidden",
        },
        {
            "display_label": "Back",
            "screen": "_focus_list",
            "action_names": ["Return"],
        },
    ]

    assert [b["label"] for b in cli._public_screen_buttons(buttons)] == [
        "Talk",
        "Back",
        "Raw",
    ]
    assert "_displayable" not in cli._public_screen_buttons(buttons)[2]
    public_interactions = cli._public_interactions(interactions)
    assert [i["display_label"] for i in public_interactions] == ["Talk", "Back"]
    assert "_raw" not in public_interactions[0]


def test_cmd_act_uses_shared_handler(monkeypatch, capsys):
    calls = []

    class FakeSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 7
        last_request_id = "request-1"

    def fake_handle_tool(ctx, name, params):
        calls.append((ctx, name, params))
        return {
            "ok": True,
            "resolved_as": "button",
            "label": "Travel",
            "wait": {"text": "ignored nested copy"},
            "text": "You travel east.",
            "pending": "CHOICE REQUIRED\n1. Continue",
            "_footer": "Day 1 | HP 4/4",
        }

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeSession())
    monkeypatch.setattr(cli, "handle_tool", fake_handle_tool)

    args = Namespace(
        target="Travel",
        wait=True,
        timeout=12,
        json=False,
        quiet=False,
    )

    assert cli.cmd_act(args, FakeClientState()) == 0

    out = capsys.readouterr().out
    assert "You travel east." in out
    assert "CHOICE REQUIRED" in out
    assert "Day 1 | HP 4/4" in out
    assert "Acted" not in out
    assert len(calls) == 1
    assert isinstance(calls[0][0].client, FakeSession)
    assert calls[0][0].allow_live_overlay_lookahead is False
    assert calls[0][1] == "act"
    assert calls[0][2] == {
        "target": "Travel",
        "wait": True,
        "timeout": 12,
        "format": "text",
    }


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("event_type", ["narration", "screen_content"])
def test_cli_act_withholds_menu_until_real_poll_drains_queued_story(
        monkeypatch, capsys, json_mode, event_type):
    from vnflight.client import BridgeClient, preserve_prefetched_events

    session = BridgeClient("http://test")
    earlier = {"type": event_type, "_seq": 20, "action_id": 6,
               "text": "Earlier story", "texts": ["Earlier story"],
               "passive_overlay_delta": ["Earlier story"]}
    monkeypatch.setattr(session, "_get", lambda *a, **kw: (
        200, {"event_counter": 30,
              "transcript": [earlier] if session.cursor < 20 else []}))
    # A command-observation poll records ownership and advances the cursor,
    # but parks the event rather than delivering it to the CLI user.
    preserve_prefetched_events(session, session.poll())
    assert session.cursor == 20
    original = {"ok": True, "text": "Already delivered prefix",
                "pending": "HABITAT ROOM CHOICES", "buttons": "STATION MAP",
                "wait": {"pending": "HABITAT ROOM CHOICES",
                         "buttons": "STATION MAP"},
                "transaction": {"pending": False, "transaction_state": "settled"}}
    monkeypatch.setattr(cli, "_make_session", lambda *a: session)
    monkeypatch.setattr(cli, "handle_tool", lambda *a: original)
    args = Namespace(target="HABITAT", wait=True, timeout=5,
                     json=json_mode, quiet=False)
    saved = {}
    state = FakeClientState()
    state.set_deferred_events = lambda key, events: saved.update(events=events)
    assert cli.cmd_act(args, state) == 0
    output = capsys.readouterr().out
    assert "HABITAT ROOM CHOICES" not in output
    assert "STATION MAP" not in output
    assert "Earlier story is queued" in output
    assert "Already delivered prefix" in output
    assert session._prefetched_events == [earlier]
    assert saved["events"] == [earlier]
    assert original["wait"]["buttons"] == "STATION MAP"
    if json_mode:
        rendered = json.loads(output)
        assert rendered["transaction"]["pending"] is False
        assert rendered["story_continues"] is True
    # The normal poll, not the guard, consumes the parked row exactly once.
    assert session.poll() == [earlier]
    assert session.poll() == []
    assert cli._cli_withhold_queued_story_decision(session, original) is original


def test_cli_act_bookkeeping_does_not_withhold_menu():
    from types import SimpleNamespace
    session = SimpleNamespace(_prefetched_events=[
        {"type": "command_result", "_seq": 20},
        {"type": "stats_update", "_seq": 21},
    ])
    result = {"ok": True, "pending": "Choose"}
    assert cli._cli_withhold_queued_story_decision(session, result) is result


def test_cmd_act_json_strips_nested_internal_wait_fields(monkeypatch, capsys):
    class FakeSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 7
        last_request_id = "request-1"

    def fake_handle_tool(ctx, name, params):
        assert name == "act"
        assert ctx.allow_live_overlay_lookahead is False
        return {
            "ok": True,
            "resolved_as": "choice",
            "label": "Continue",
            "_data": {"internal": True},
            "wait": {
                "pending": {"type": "choice", "choices": []},
                "_data": {"internal": True},
                "_pending_raw": {"type": "choice_request"},
                "_footer": "Day 1",
            },
        }

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeSession())
    monkeypatch.setattr(cli, "handle_tool", fake_handle_tool)

    args = Namespace(
        target="Continue",
        wait=True,
        timeout=12,
        json=True,
        quiet=False,
    )

    assert cli.cmd_act(args, FakeClientState()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "_data" not in payload
    assert "_data" not in payload["wait"]
    assert "_pending_raw" not in payload["wait"]
    assert payload["wait"]["_footer"] == "Day 1"


def test_cmd_act_no_wait_prints_confirmation(monkeypatch, capsys):
    class FakeSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeSession())
    monkeypatch.setattr(
        cli,
        "handle_tool",
        lambda ctx, name, params: {
            "ok": True,
            "resolved_as": "choice",
            "label": "Friendly",
        },
    )

    args = Namespace(
        target="friendly",
        wait=False,
        timeout=None,
        json=False,
        quiet=False,
    )

    assert cli.cmd_act(args, FakeClientState()) == 0
    assert "Acted (choice): Friendly" in capsys.readouterr().out


def test_cmd_act_reports_handler_error(monkeypatch, capsys):
    class FakeSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeSession())
    monkeypatch.setattr(
        cli,
        "handle_tool",
        lambda ctx, name, params: {
            "error": "Interaction is disabled: 'Sleep'"
        },
    )

    args = Namespace(
        target="Sleep",
        wait=True,
        timeout=None,
        json=False,
        quiet=False,
    )

    assert cli.cmd_act(args, FakeClientState()) == 1
    assert "Interaction is disabled" in capsys.readouterr().out


def test_act_parser_defaults_to_wait_and_supports_no_wait():
    parser = cli.build_parser()

    args = parser.parse_args(["act", "friendly"])
    assert args.command == "act"
    assert args.target == "friendly"
    assert args.wait is True

    args = parser.parse_args(["act", "friendly", "--no-wait"])
    assert args.wait is False


def test_cmd_slots_reap_stale_frees_ended_and_dead_pid(monkeypatch, capsys):
    freed = []

    class FakeSlotsClient:
        def __init__(self, bridge):
            self.bridge = bridge

        def list_slots(self):
            return [
                {
                    "slot_id": 1,
                    "game_id": "ended_game",
                    "status": "ended",
                    "event_counter": 10,
                    "game_pid": 111,
                },
                {
                    "slot_id": 2,
                    "game_id": "dead_game",
                    "status": "idle",
                    "event_counter": 20,
                    "game_pid": 222,
                },
                {
                    "slot_id": 3,
                    "game_id": "live_game",
                    "status": "idle",
                    "event_counter": 30,
                    "game_pid": 333,
                },
            ]

        def free_slot(self, slot_id):
            freed.append(slot_id)
            return True, {"message": f"Slot {slot_id} freed."}

    monkeypatch.setattr(cli, "BridgeClient", FakeSlotsClient)
    monkeypatch.setattr(cli, "_is_process_alive", lambda pid: pid == 333)

    args = Namespace(
        bridge="http://bridge",
        reap_stale=True,
        json=False,
    )

    assert cli.cmd_slots(args, FakeClientState()) == 0
    assert freed == [1, 2]
    out = capsys.readouterr().out
    assert "slot 1 ended_game (ended)" in out
    assert "slot 2 dead_game (dead_pid)" in out
    assert "live_game" not in out


def test_cmd_slots_reap_stale_reports_free_failures(monkeypatch, capsys):
    class FakeSlotsClient:
        def __init__(self, bridge):
            self.bridge = bridge

        def list_slots(self):
            return [
                {
                    "slot_id": 1,
                    "game_id": "stale_game",
                    "status": "ended",
                    "event_counter": 10,
                    "game_pid": 111,
                },
            ]

        def free_slot(self, slot_id):
            return False, {"error": "forbidden"}

    monkeypatch.setattr(cli, "BridgeClient", FakeSlotsClient)

    args = Namespace(
        bridge="http://bridge",
        reap_stale=True,
        json=False,
    )

    assert cli.cmd_slots(args, FakeClientState()) == 1
    out = capsys.readouterr().out
    assert "kept: slot 1 stale_game (ended): forbidden" in out


def test_cmd_slots_reap_treats_already_gone_as_success(monkeypatch, capsys):
    class FakeSlotsClient:
        def __init__(self, bridge):
            self.bridge = bridge

        def list_slots(self):
            return [
                {
                    "slot_id": 1,
                    "game_id": "stale_game",
                    "status": "ended",
                    "event_counter": 10,
                    "game_pid": 111,
                },
            ]

        def free_slot(self, slot_id):
            return False, {"error": "No slot '1' to free."}

    monkeypatch.setattr(cli, "BridgeClient", FakeSlotsClient)

    args = Namespace(
        bridge="http://bridge",
        reap_stale=True,
        json=False,
    )

    assert cli.cmd_slots(args, FakeClientState()) == 0
    out = capsys.readouterr().out
    assert "freed: slot 1 stale_game (ended)" in out


def test_cmd_stop_reports_slot_free_failure(monkeypatch, capsys):
    sent_commands = []

    class FakeStopClient:
        def __init__(self, bridge, slot=None):
            self.bridge = bridge
            self.slot = slot

        def resolve_slot_info(self, target_slot):
            assert target_slot == "27"
            return {
                "slot_id": 27,
                "game_id": "roadwarden",
                "game_pid": 58228,
            }

        def _send_command(self, command):
            sent_commands.append((self.slot, command))

        def free_slot(self, slot_id):
            assert slot_id == 27
            return False, {"error": "Admin token required."}

    monkeypatch.setattr(cli, "BridgeClient", FakeStopClient)
    monkeypatch.setattr(cli, "_is_process_alive", lambda pid: False)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    args = Namespace(
        bridge="http://bridge",
        game="27",
        target_slot=None,
        json=False,
    )

    assert cli.cmd_stop(args, FakeClientState()) == 1
    assert sent_commands == [(27, "quit")]
    out = capsys.readouterr().out
    assert "could not free slot 27" in out
    assert "Admin token required." in out


def test_cmd_stop_fails_when_process_survives_kill(monkeypatch, capsys):
    class FakeStopClient:
        def __init__(self, bridge, slot=None, token=None):
            self.slot = slot

        def resolve_slot_info(self, target_slot):
            return {
                "slot_id": 28,
                "game_id": "roadwarden",
                "game_pid": 58228,
            }

        def _send_command(self, command):
            pass

        def free_slot(self, slot_id):  # pragma: no cover - should not be called
            raise AssertionError("unexpected free")

    monkeypatch.setattr(cli, "BridgeClient", FakeStopClient)
    monkeypatch.setattr(cli, "_is_process_alive", lambda pid: True)
    monkeypatch.setattr(cli, "kill_process", lambda pid, *a, **k: False)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    args = Namespace(
        bridge="http://bridge",
        game="28",
        target_slot=None,
        json=False,
    )

    assert cli.cmd_stop(args, FakeClientState()) == 1
    out = capsys.readouterr().out
    assert "Could not stop 'roadwarden' process" in out
    assert "slot 28 left intact" in out


def test_cmd_stop_frees_slot_before_reporting_success(monkeypatch, capsys):
    freed = []

    class FakeStopClient:
        def __init__(self, bridge, slot=None):
            self.bridge = bridge
            self.slot = slot

        def resolve_slot_info(self, target_slot):
            return {
                "slot_id": 28,
                "game_id": "roadwarden",
                "game_pid": None,
            }

        def _send_command(self, command):
            pass

        def free_slot(self, slot_id):
            freed.append(slot_id)
            return True, {"message": "Slot freed."}

    monkeypatch.setattr(cli, "BridgeClient", FakeStopClient)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    args = Namespace(
        bridge="http://bridge",
        game="28",
        target_slot=None,
        json=False,
    )

    assert cli.cmd_stop(args, FakeClientState()) == 0
    assert freed == [28]
    assert "Stopped 'roadwarden' (slot 28)" in capsys.readouterr().out


def test_cmd_stop_uses_stored_admin_token(monkeypatch, capsys):
    clients = []

    class TokenState(FakeClientState):
        def get_admin_token(self, bridge_url):
            assert bridge_url == "http://bridge"
            return "admin-secret"

    class FakeStopClient:
        def __init__(self, bridge, slot=None, token=None):
            self.bridge = bridge
            self.slot = slot
            self.token = token
            clients.append(self)

        def resolve_slot_info(self, target_slot):
            return {
                "slot_id": 28,
                "game_id": "roadwarden",
                "game_pid": None,
            }

        def _send_command(self, command):
            pass

        def free_slot(self, slot_id):
            return True, {"message": "Slot freed."}

    monkeypatch.setattr(cli, "BridgeClient", FakeStopClient)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    args = Namespace(
        bridge="http://bridge",
        game="28",
        target_slot=None,
        json=False,
    )

    assert cli.cmd_stop(args, TokenState()) == 0
    assert [client.token for client in clients] == ["admin-secret", "admin-secret"]


def test_cmd_reset_bridge_uses_token_before_clearing_state(monkeypatch):
    calls = []

    class TokenState(FakeClientState):
        def __init__(self):
            self.cleared = False

        def get_admin_token(self, bridge_url):
            assert self.cleared is False
            return "admin-secret"

        def clear(self, bridge_url):
            self.cleared = True

    class FakeResetClient:
        def __init__(self, bridge, token=None):
            calls.append((bridge, token))

        def reset_bridge(self):
            return True

    monkeypatch.setattr(cli, "BridgeClient", FakeResetClient)

    args = Namespace(bridge="http://bridge", bridge_reset=True, json=True)
    state = TokenState()

    assert cli.cmd_reset(args, state) == 0
    assert calls == [("http://bridge", "admin-secret")]
    assert state.cleared is True


def test_cmd_reset_bridge_failure_keeps_client_state(monkeypatch):
    calls = []

    class TokenState(FakeClientState):
        def __init__(self):
            self.cleared = False

        def get_admin_token(self, bridge_url):
            return "admin-secret"

        def clear(self, bridge_url):
            self.cleared = True

    class FakeResetClient:
        def __init__(self, bridge, token=None):
            calls.append((bridge, token))

        def reset_bridge(self):
            return False

    monkeypatch.setattr(cli, "BridgeClient", FakeResetClient)

    args = Namespace(bridge="http://bridge", bridge_reset=True, json=True)
    state = TokenState()

    assert cli.cmd_reset(args, state) == 1
    assert calls == [("http://bridge", "admin-secret")]
    assert state.cleared is False


def test_cmd_stop_treats_already_freed_slot_as_success(monkeypatch, capsys):
    class FakeStopClient:
        def __init__(self, bridge, slot=None):
            self.bridge = bridge
            self.slot = slot

        def resolve_slot_info(self, target_slot):
            return {
                "slot_id": 1,
                "game_id": "roadwarden",
                "game_pid": None,
            }

        def _send_command(self, command):
            pass

        def free_slot(self, slot_id):
            return False, {"error": "No slot '1' to free."}

    monkeypatch.setattr(cli, "BridgeClient", FakeStopClient)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    args = Namespace(
        bridge="http://bridge",
        game="1",
        target_slot=None,
        json=False,
    )

    assert cli.cmd_stop(args, FakeClientState()) == 0
    assert "Stopped 'roadwarden' (slot 1)" in capsys.readouterr().out


def test_freshen_choice_request_events_uses_live_choice_screen():
    stale_event = {
        "type": "choice_request",
        "choices": ["I approach Foggy.", "I go outside."],
        "full_items": [
            {"label": "I approach Foggy.", "is_disabled": False},
            {
                "label": "I’m too exhausted to brew potions.",
                "is_disabled": True,
            },
            {"label": "I go outside.", "is_disabled": False},
        ],
        "interactions": [
            {
                "type": "choice",
                "display_label": "I approach Foggy.",
                "disabled": False,
            },
            {
                "type": "choice",
                "display_label": "I’m too exhausted to brew potions.",
                "disabled": True,
            },
            {
                "type": "choice",
                "display_label": "I go outside.",
                "disabled": False,
            },
        ],
    }
    live_screen = {
        "interactions": [
            {
                "type": "choice",
                "display_label": "I approach Foggy.",
                "disabled": False,
            },
            {
                "type": "choice",
                "display_label": "[cost] I go downstairs, to the alchemy set.",
                "disabled": False,
            },
            {
                "type": "choice",
                "display_label": "I go outside.",
                "disabled": False,
            },
        ],
    }

    result = _freshen_choice_request_events(
        [stale_event],
        FakeScreenSession(live_screen),
    )

    updated = result[0]
    assert updated["choices"] == [
        "I approach Foggy.",
        "[cost] I go downstairs, to the alchemy set.",
        "I go outside.",
    ]
    assert not any(item.get("is_disabled") for item in updated["full_items"])


def test_freshen_choice_request_events_ignores_only_disabled_normalization():
    event = {
        "type": "choice_request",
        "choices": ["Go"],
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "Go",
                "disabled": False,
            },
            {
                "source": "button",
                "type": "info",
                "display_label": "Help",
                "action_names": ["NullAction"],
                "disabled": False,
            },
        ],
    }

    result = _freshen_choice_request_events(
        [event],
        FakeScreenSession({}),
    )

    assert result[0] is event


def test_cmd_choices_keeps_pending_request_after_disabled_normalization(monkeypatch, capsys):
    pending = {
        "type": "choice_request",
        "choices": ["Go"],
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "Go",
                "disabled": False,
            },
            {
                "source": "button",
                "type": "info",
                "display_label": "Help",
                "action_names": ["NullAction"],
                "disabled": False,
            },
        ],
    }

    class FakeChoicesSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = None
        last_request_id = None

        def pending(self):
            return pending

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeChoicesSession({}))
    monkeypatch.setattr(cli, "_save_session", lambda session, args, state: None)

    args = Namespace(bridge="http://bridge", json=False, quiet=False)

    assert cli.cmd_choices(args, object()) == 0
    out = capsys.readouterr().out
    assert "--- CHOICE REQUIRED ---" in out
    assert "--- OTHER BUTTONS ---" not in out


def test_cmd_choices_ignores_stale_main_menu_screen(monkeypatch, capsys):
    pending = {
        "type": "choice_request",
        "choices": ["Go inside"],
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "Go inside",
                "disabled": False,
            },
        ],
    }
    stale_menu = {
        "type": "screen_content",
        "screens": ["menu"],
        "buttons": [
            {"label": "Start", "actions": ["Start"], "screen": "menu"},
        ],
        "interactions": [
            {
                "source": "button",
                "type": "nav",
                "display_label": "Start",
                "disabled": False,
            },
        ],
    }

    class FakeChoicesSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = None
        last_request_id = None

        def pending(self):
            return pending

        def state(self):
            return {"context": {"context": "in_game"}}

    monkeypatch.setattr(
        cli,
        "_make_session",
        lambda args, state: FakeChoicesSession(stale_menu),
    )
    monkeypatch.setattr(cli, "_save_session", lambda session, args, state: None)

    args = Namespace(bridge="http://bridge", json=False, quiet=False)

    assert cli.cmd_choices(args, object()) == 0
    out = capsys.readouterr().out
    assert "--- CHOICE REQUIRED ---" in out
    assert "Go inside" in out
    assert "--- NAVIGATION ---" not in out


def test_cmd_choices_respects_empty_live_interactions(monkeypatch, capsys):
    pending = {
        "type": "choice_request",
        "choices": ["Stale choice."],
        "interactions": [
            {
                "source": "button",
                "type": "other",
                "display_label": "[spell]",
                "promoted": True,
            },
        ],
    }
    live_screen = {
        "type": "screen_content",
        "interactions": [],
    }

    class FakeChoicesSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = None
        last_request_id = None

        def pending(self):
            return pending

    monkeypatch.setattr(
        cli,
        "_make_session",
        lambda args, state: FakeChoicesSession(live_screen),
    )
    monkeypatch.setattr(cli, "_save_session", lambda session, args, state: None)

    args = Namespace(bridge="http://bridge", json=False, quiet=False)

    assert cli.cmd_choices(args, object()) == 0
    out = capsys.readouterr().out
    assert "No choices or items available." in out
    assert "Stale choice." not in out
    assert "[spell]" not in out


def test_pending_not_hidden_by_non_modal_screen_without_choice_buttons():
    pending = {"type": "choice_request", "id": "spell", "choices": ["Cast light"]}
    screen = {
        "screens": ["nvl", "quick_menu"],
        "interactions": [
            {
                "type": "other",
                "display_label": "[spell]",
            },
        ],
        "buttons": [
            {
                "label": "[spell]",
                "screen": "tutorialtooltips",
                "action_strs": ["Function"],
            },
        ],
    }

    class FakeSession:
        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": pending,
            }

    assert cli._pending_hidden_by_screen(FakeSession(), screen, pending) is False


def test_pending_hidden_by_real_menu_replacement():
    pending = {"type": "choice_request", "id": "old", "choices": ["Continue"]}
    screen = {
        "screens": ["menu"],
        "buttons": [{"label": "Start", "screen": "menu"}],
    }

    class FakeSession:
        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "main_menu"},
                "pending_request": pending,
            }

    assert cli._pending_hidden_by_screen(FakeSession(), screen, pending) is True


def test_get_settled_screen_buttons_waits_past_transient_shell():
    class SequencedScreenSession:
        def __init__(self):
            self.last_screen = None
            self.screens = [
                {
                    "screens": ["inventory"],
                    "buttons": [{"label": "Return", "screen": "inventory"}],
                },
                {
                    "screens": ["inventory"],
                    "buttons": [
                        {"label": "Small Healing Potion", "screen": "inventory"},
                        {"label": "Return", "screen": "inventory"},
                    ],
                    "interactions": [
                        {
                            "type": "item",
                            "category": "supplies",
                            "display_label": "Small Healing Potion",
                        },
                    ],
                },
                {
                    "screens": ["inventory"],
                    "buttons": [
                        {"label": "Small Healing Potion", "screen": "inventory"},
                        {"label": "Return", "screen": "inventory"},
                    ],
                    "interactions": [
                        {
                            "type": "item",
                            "category": "supplies",
                            "display_label": "Small Healing Potion",
                        },
                    ],
                },
            ]

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            if self.screens:
                self.last_screen = self.screens.pop(0)
            screen = self.last_screen
            return 200, {"screen": screen}

        def get_transcript(self, last_n=30):
            return [
                {
                    "type": "screen_content",
                    "texts": ["Loaded scene context."],
                },
            ]

    screen = cli._get_settled_screen_buttons(
        SequencedScreenSession(),
        timeout=1.0,
        settle_delay=0.2,
    )

    labels = [button["label"] for button in screen["buttons"]]
    assert "Small Healing Potion" in labels


def test_get_settled_screen_buttons_can_require_a_change():
    class DelayedScreenSession:
        def __init__(self):
            self.last_screen = None
            self.screens = [
                {
                    "screens": ["inventory"],
                    "buttons": [{"label": "Return", "screen": "inventory"}],
                },
                {
                    "screens": ["inventory"],
                    "buttons": [{"label": "Return", "screen": "inventory"}],
                },
                {
                    "screens": ["inventory"],
                    "buttons": [
                        {"label": "Food Rations", "screen": "inventory"},
                    ],
                },
                {
                    "screens": ["inventory"],
                    "buttons": [
                        {"label": "Food Rations", "screen": "inventory"},
                    ],
                },
            ]

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            if self.screens:
                self.last_screen = self.screens.pop(0)
            return 200, {"screen": self.last_screen}

        def get_transcript(self, last_n=30):
            return []

    initial = {
        "screens": ["inventory"],
        "buttons": [{"label": "Return", "screen": "inventory"}],
    }

    screen = cli._get_settled_screen_buttons(
        DelayedScreenSession(),
        initial,
        timeout=1.0,
        settle_delay=0.0,
        return_initial_when_stable=False,
    )

    labels = [button["label"] for button in screen["buttons"]]
    assert labels == ["Food Rations"]


def test_screen_content_display_can_defer_actionable_prompt():
    event = {
        "type": "screen_content",
        "texts": ["Unfortunately, this only drives the arrow deeper."],
        "screens": ["classes"],
        "buttons": [{"label": "Done", "screen": "classes"}],
        "interactions": [{"type": "other", "display_label": "Done"}],
    }

    display = cli._screen_content_event_for_display(
        event,
        has_choice_req=False,
        defer_actionable_prompt=True,
    )

    assert display["texts"] == event["texts"]
    assert "buttons" not in display
    assert "interactions" not in display


def test_screen_buttons_after_story_prints_settled_prompt(monkeypatch, capsys):
    stale = {
        "type": "screen_content",
        "screens": ["classes"],
        "buttons": [{"label": "Done", "screen": "classes"}],
    }
    settled = {
        "type": "screen_content",
        "screens": ["end_menu_screen"],
        "buttons": [
            {"label": "Title Screen", "screen": "end_menu_screen"},
            {"label": "Show Log", "screen": "end_menu_screen"},
            {"label": "Load Game", "screen": "end_menu_screen"},
            {"label": "Quit", "screen": "end_menu_screen"},
        ],
    }

    class FakeSession:
        def pending(self):
            return None

    seen = {}

    def fake_settle(session, initial, *, timeout, return_initial_when_stable):
        seen["timeout"] = timeout
        seen["return_initial_when_stable"] = return_initial_when_stable
        assert initial is stale
        return settled

    monkeypatch.setattr(cli, "_get_settled_screen_buttons", fake_settle)

    result = cli._return_screen_buttons_after_events_phase(
        FakeSession(),
        Namespace(json=False),
        [
            {
                "type": "narration",
                "text": "Unfortunately, this only drives the arrow deeper.",
            },
            stale,
        ],
        has_screen_buttons=True,
        deferred_choice_request_event=None,
        transition_since_story=False,
        initial_grace=0.0,
        fresh_story_seen=True,
        story_wait_until=0.0,
        latest_sc_event=stale,
        colour=False,
        quiet=False,
        verbose=False,
        printed_something=True,
    )

    out = capsys.readouterr().out
    assert result.should_return is True
    assert seen["timeout"] == 3.0
    assert seen["return_initial_when_stable"] is False
    assert "Load Game" in out
    assert "Quit" in out
    assert "Done" not in out


@pytest.mark.parametrize("json_mode", [False, True])
def test_settled_screen_waits_for_real_poll_to_deliver_earlier_story(monkeypatch, capsys, json_mode):
    from vnflight.client import BridgeClient

    session = BridgeClient("http://test")
    initial = {"type": "screen_content", "_seq": 10,
               "screens": ["terminal"], "buttons": [{"label": "Continue"}]}
    current = {"type": "screen_content", "_seq": 30,
               "screens": ["terminal"], "buttons": [{"label": "Ask ECHO"}]}
    introduction = {"type": "narration", "_seq": 20, "text": "I am ECHO-7."}
    replies = iter([
        {"event_counter": 10, "transcript": [initial]},
        {"event_counter": 30, "transcript": [introduction]},
    ])
    monkeypatch.setattr(session, "_get", lambda *a, **kw: (200, next(replies)))
    monkeypatch.setattr(session, "pending", lambda: None)
    monkeypatch.setattr(cli, "_get_settled_screen_buttons", lambda *a, **kw: current)
    first_batch = session.poll()
    kwargs = dict(has_screen_buttons=True, deferred_choice_request_event=None,
                  transition_since_story=False, initial_grace=0.0,
                  fresh_story_seen=True, story_wait_until=0.0,
                  latest_sc_event=initial, colour=False, quiet=False,
                  verbose=False, printed_something=True)
    result = cli._return_screen_buttons_after_events_phase(
        session, Namespace(json=json_mode), first_batch, **kwargs)
    assert result.should_continue and not result.should_return
    assert capsys.readouterr().out == ""
    assert session.poll() == [introduction]
    # A state-only screen need not appear in the transcript. The state counter,
    # not just cursor=20, proves the ordinary read covered screen sequence 30.
    assert session.cursor == 20
    result = cli._return_screen_buttons_after_events_phase(
        session, Namespace(json=json_mode), [], **kwargs)
    assert result.should_return and not result.should_continue
    assert "Ask ECHO" in capsys.readouterr().out


def test_buffered_story_fences_a_screen_even_after_state_counter_catches_up():
    from types import SimpleNamespace
    session = SimpleNamespace(_last_event_counter=40, cursor=40,
                              _prefetched_events=[{"_seq": 20, "text": "Earlier"}])
    assert cli._screen_prompt_ahead_of_story(session, {"_seq": 30})
    session._prefetched_events.clear()
    assert not cli._screen_prompt_ahead_of_story(session, {"_seq": 30})


def test_checked_screen_prompt_does_not_fetch_a_newer_menu():
    class NoReads:
        def __getattr__(self, name):
            raise AssertionError("Unexpected live read: " + name)

    screen = {"_seq": 30, "screens": ["terminal"],
              "buttons": [{"label": "Checked question"}]}
    text = cli._format_current_screen_prompt(
        NoReads(), Namespace(json=False), screen=screen)
    assert "Checked question" in text


def test_cmd_load_drops_the_stash_restored_from_the_previous_timeline(monkeypatch, capsys):
    """Rows a previous invocation polled past and _make_session restored
    belong to the timeline the load replaces; poll() would have returned
    them as the load's first events."""
    seen = {}

    class FakeLoadSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 99
        last_request_id = None
        last_request_type = None
        last_choices = None
        last_actionable_snapshot = None
        _prefetched_events = [
            {"type": "narration", "text": "stale, pre-load", "_seq": 41},
        ]
        state_calls = 0
        load_nonce = None

        def state(self):
            self.state_calls += 1
            if self.state_calls == 1:
                return {"event_counter": 99, "pending_request": {"id": "old"}}
            return {"event_counter": 101, "pending_request": {"id": "new"}}

        def _send_command(self, name, args=None, nonce=None):
            self.load_nonce = nonce
            return True, "accepted"

        def poll(self, timeout=0):
            seen["stash_at_poll"] = list(self._prefetched_events)
            return [{"type": "command_result", "command": "load",
                     "success": True, "nonce": self.load_nonce}]

    session = FakeLoadSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(cli, "_save_session", lambda s, a, st: None)

    args = Namespace(bridge="http://bridge", json=True, quiet=False,
                    slot="any", wait=False)
    assert cli.cmd_load(args, object()) == 0
    capsys.readouterr()
    assert seen["stash_at_poll"] == []
    assert session._prefetched_events == []


@pytest.mark.parametrize("outcome", ["refused", "unconfirmed", "unsubmitted"])
def test_cmd_load_keeps_the_stash_when_the_load_does_not_replace_the_timeline(
    monkeypatch, capsys, outcome
):
    """Astra: the stash was cleared at submission; a load of a missing
    save left the timeline in place but lost its unread rows."""
    stale = [{"type": "narration", "text": "unread, still valid", "_seq": 41}]
    saved = []

    class FakeLoadSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 99
        last_request_id = None
        last_request_type = None
        last_choices = None
        last_actionable_snapshot = None
        _prefetched_events = list(stale)
        load_nonce = None

        def state(self):
            return {"event_counter": 99, "pending_request": {"id": "old"}}

        def _send_command(self, name, args=None, nonce=None):
            self.load_nonce = nonce
            if outcome == "unsubmitted":
                return False, "bridge refused"
            return True, "accepted"

        def poll(self, timeout=0):
            if outcome == "refused":
                return [{"type": "command_result", "command": "load",
                         "success": False, "error": "No such save",
                         "nonce": self.load_nonce}]
            return []

    session = FakeLoadSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(cli, "_save_session",
                        lambda s, a, st: saved.append(list(s._prefetched_events)))
    monkeypatch.setattr(cli, "_wait_for_load_ready", lambda *a, **k: False)

    args = Namespace(bridge="http://bridge", json=True, quiet=False,
                    slot="missing", wait=False)
    assert cli.cmd_load(args, object()) == 1
    capsys.readouterr()
    assert session._prefetched_events == stale
    # The last persisted state carries the rows again.
    assert saved and saved[-1] == stale


def test_cmd_load_keeps_preload_cursor_until_reset(monkeypatch, capsys):
    saved_cursors = []

    class FakeLoadSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 12
        last_request_id = "old"
        last_request_type = "choice_request"
        last_choices = ["old"]
        state_calls = 0
        load_nonce = None

        def state(self):
            self.state_calls += 1
            if self.state_calls == 1:
                return {
                    "event_counter": 99,
                    "pending_request": {"id": "old"},
                }
            return {
                "event_counter": 101,
                "pending_request": {"id": "new"},
            }

        def _send_command(self, name, args=None, nonce=None):
            assert name == "load"
            assert args == {"slot": "rw-regression-potion-unlock"}
            assert self.cursor == 99
            self.load_nonce = nonce
            return True, "accepted"

        def poll(self, timeout=0):
            return [
                {
                    "type": "command_result",
                    "command": "load",
                    "success": True,
                    "nonce": self.load_nonce,
                },
            ]

    def fake_save(session, args, state):
        saved_cursors.append(session.cursor)

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeLoadSession())
    monkeypatch.setattr(cli, "_save_session", fake_save)

    args = Namespace(
        bridge="http://bridge",
        json=True,
        quiet=False,
        slot="rw-regression-potion-unlock",
        wait=False,
    )

    assert cli.cmd_load(args, object()) == 0
    capsys.readouterr()
    assert saved_cursors == [0]


def test_cmd_load_wait_does_not_duplicate_loaded_choice_request(monkeypatch, capsys):
    class FakeLoadWaitSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 12
        last_request_id = "old"
        last_request_type = "choice_request"
        last_choices = ["old"]
        state_calls = 0
        load_nonce = None

        def state(self):
            self.state_calls += 1
            if self.state_calls == 1:
                return {
                    "event_counter": 99,
                    "pending_request": {"id": "old"},
                }
            return {
                "event_counter": 1,
                "pending_request": {"id": "new"},
            }

        def _send_command(self, name, args=None, nonce=None):
            assert name == "load"
            self.load_nonce = nonce
            return True, "accepted"

        def poll(self, timeout=0):
            return [
                {
                    "type": "command_result",
                    "command": "load",
                    "success": True,
                    "nonce": self.load_nonce,
                },
                {
                    "type": "choice_request",
                    "id": "new",
                    "choices": ["Go"],
                },
            ]

        def get_transcript(self, last_n=30):
            return []

        def _get(self, path, timeout=2.0):
            return 404, {}

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeLoadWaitSession())
    monkeypatch.setattr(cli, "_save_session", lambda session, args, state: None)

    def fail_prompt(session, args):
        raise AssertionError("prompt should not be queried after choice_request event")

    monkeypatch.setattr(cli, "_format_current_screen_prompt", fail_prompt)

    args = Namespace(
        bridge="http://bridge",
        json=False,
        quiet=False,
        verbose=False,
        slot="echoes-smoke",
        wait=True,
        timeout=30,
    )

    assert cli.cmd_load(args, object()) == 0
    out = capsys.readouterr().out
    assert out.count("--- CHOICE REQUIRED ---") == 1
    assert "Go" in out


def test_cmd_load_wait_includes_current_screen_text_before_prompt(monkeypatch, capsys):
    class FakeLoadWaitSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 12
        last_request_id = "old"
        last_request_type = "choice_request"
        last_choices = ["old"]
        state_calls = 0
        load_nonce = None

        def state(self):
            self.state_calls += 1
            if self.state_calls == 1:
                return {
                    "event_counter": 99,
                    "pending_request": {"id": "old"},
                }
            return {
                "event_counter": 1,
                "pending_request": {"id": "new"},
            }

        def _send_command(self, name, args=None, nonce=None):
            assert name == "load"
            self.load_nonce = nonce
            return True, "accepted"

        def poll(self, timeout=0):
            return [
                {
                    "type": "command_result",
                    "command": "load",
                    "success": True,
                    "nonce": self.load_nonce,
                },
                {
                    "type": "choice_request",
                    "id": "new",
                    "choices": ["Go"],
                },
            ]

        def get_transcript(self, last_n=30):
            return [
                {
                    "type": "screen_content",
                    "texts": ["Loaded scene context."],
                },
            ]

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {
                    "screen": {
                        "type": "screen_content",
                        "screens": ["nvl"],
                        "texts": [],
                    },
                }
            return 404, {}

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeLoadWaitSession())
    monkeypatch.setattr(cli, "_save_session", lambda session, args, state: None)

    args = Namespace(
        bridge="http://bridge",
        json=False,
        quiet=False,
        verbose=False,
        slot="roadwarden-context",
        wait=True,
        timeout=30,
    )

    assert cli.cmd_load(args, object()) == 0
    out = capsys.readouterr().out
    assert "Loaded scene context." in out
    assert out.index("Loaded scene context.") < out.index("--- CHOICE REQUIRED ---")


def test_cmd_load_wait_fast_forwards_loaded_prompt_cursor(monkeypatch, capsys):
    saved_cursors = []

    class FakeLoadWaitSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 12
        last_request_id = "old"
        last_request_type = "choice_request"
        last_choices = ["old"]
        state_calls = 0
        load_nonce = None

        def state(self):
            self.state_calls += 1
            if self.state_calls == 1:
                return {
                    "event_counter": 99,
                    "pending_request": {"id": "old"},
                }
            if self.state_calls == 2:
                return {
                    "event_counter": 3,
                    "pending_request": None,
                }
            return {
                "event_counter": 19,
                "pending_request": None,
            }

        def _send_command(self, name, args=None, nonce=None):
            assert name == "load"
            self.load_nonce = nonce
            return True, "accepted"

        def poll(self, timeout=0):
            self.cursor = 3
            return [
                {
                    "type": "command_result",
                    "command": "load",
                    "success": True,
                    "nonce": self.load_nonce,
                    "_seq": 1,
                },
                {"type": "inventory_update", "_seq": 2},
                {"type": "stats_update", "_seq": 3},
            ]

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeLoadWaitSession())
    monkeypatch.setattr(
        cli,
        "_save_session",
        lambda session, args, state: saved_cursors.append(session.cursor),
    )
    monkeypatch.setattr(
        cli,
        "_format_current_screen_prompt",
        lambda session, args, **kwargs: "--- TOPICS ---\n  1: Ask about herbs.",
    )

    args = Namespace(
        bridge="http://bridge",
        json=False,
        quiet=False,
        verbose=False,
        slot="roadwarden-topic",
        wait=True,
        timeout=30,
    )

    assert cli.cmd_load(args, object()) == 0
    out = capsys.readouterr().out
    assert "--- TOPICS ---" in out
    assert saved_cursors[-1] == 19


def test_load_events_move_screen_text_before_prompt():
    events = [
        {"type": "choice_request", "id": "new", "choices": ["Go"]},
        {"type": "screen_content", "texts": ["Loaded scene context."]},
    ]

    result = cli._load_events_with_screen_text(events, session=object())

    assert result[0]["type"] == "screen_content"
    assert result[1]["type"] == "choice_request"


def test_load_events_uses_state_screen_text_before_bare_prompt():
    class FakeSession:
        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": {"buttons": [], "texts": []}}
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

        def state(self):
            return {
                "screen": {
                    "texts": ["Loaded scene context from state."],
                },
            }

    events = [
        {"type": "choice_request", "id": "new", "choices": ["Go"]},
    ]

    result = cli._load_events_with_screen_text(events, session=FakeSession())

    assert result[0]["type"] == "screen_content"
    assert result[0]["texts"] == ["Loaded scene context from state."]
    assert result[1]["type"] == "choice_request"


def test_load_events_uses_transcript_narration_before_bare_prompt():
    class FakeSession:
        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": {"buttons": [], "texts": []}}
            return 404, {}

        def get_transcript(self, last_n=30):
            return [
                {"type": "narration", "text": "Loaded narration context."},
            ]

        def state(self):
            return {}

    events = [
        {"type": "choice_request", "id": "new", "choices": ["Go"]},
    ]

    result = cli._load_events_with_screen_text(events, session=FakeSession())

    assert result[0]["type"] == "screen_content"
    assert result[0]["texts"] == ["Loaded narration context."]
    assert result[1]["type"] == "choice_request"


def test_load_events_drains_late_text_after_bare_prompt():
    class FakeSession:
        def __init__(self):
            self.polls = 0

        def poll(self, timeout=0):
            self.polls += 1
            if self.polls == 1:
                return [{"type": "command_result", "command": "resync"}]
            return [{"type": "narration", "text": "Late loaded narration."}]

    events = [
        {"type": "choice_request", "id": "new", "choices": ["Go"]},
    ]

    drained = cli._load_events_with_late_text(
        events,
        session=FakeSession(),
        timeout=1.0,
    )
    result = cli._load_events_with_screen_text(drained, session=object())

    assert result[0]["type"] == "narration"
    assert result[0]["text"] == "Late loaded narration."
    assert result[1]["type"] == "choice_request"


def test_load_events_drains_late_prompt_then_text_after_load_metadata():
    class FakeSession:
        transcript = lambda self, last=20: []

        def __init__(self):
            self.polls = 0

        def poll(self, timeout=0):
            self.polls += 1
            if self.polls == 1:
                return [{"type": "choice_request", "id": "new", "choices": ["Go"]}]
            return [{"type": "narration", "text": "Late loaded narration."}]

    events = [
        {"type": "command_result", "command": "load", "success": True},
        {"type": "stats_update"},
    ]

    result = cli._load_events_with_late_text(
        events,
        session=FakeSession(),
        timeout=1.0,
    )

    assert [event["type"] for event in result] == [
        "command_result",
        "stats_update",
        "choice_request",
        "narration",
    ]


def test_cmd_load_reports_game_load_failure(monkeypatch, capsys):
    class FakeFailedLoadSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 12
        last_request_id = "old"
        last_request_type = "choice_request"
        last_choices = ["old"]
        state_calls = 0
        load_nonce = None

        def state(self):
            self.state_calls += 1
            if self.state_calls == 1:
                return {
                    "event_counter": 99,
                    "pending_request": {"id": "old"},
                }
            return {
                "event_counter": 1,
                "pending_request": None,
            }

        def _send_command(self, name, args=None, nonce=None):
            assert name == "load"
            self.load_nonce = nonce
            return True, "accepted"

        def poll(self, timeout=0):
            return [
                {
                    "type": "command_result",
                    "command": "load",
                    "success": False,
                    "error": "missing save",
                    "nonce": self.load_nonce,
                },
            ]

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeFailedLoadSession())
    monkeypatch.setattr(cli, "_save_session", lambda session, args, state: None)

    args = Namespace(
        bridge="http://bridge",
        json=False,
        quiet=False,
        verbose=False,
        slot="missing",
        wait=True,
        timeout=30,
    )

    assert cli.cmd_load(args, object()) == 1
    out = capsys.readouterr().out
    assert "Load failed for slot 'missing': missing save" in out
    assert "accepted" not in out


def test_cmd_load_rejects_stale_command_result(monkeypatch, capsys):
    class FakeStaleLoadSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 12
        last_request_id = "old"
        last_request_type = "choice_request"
        last_choices = ["old"]
        state_calls = 0
        load_nonce = None

        def state(self):
            self.state_calls += 1
            if self.state_calls == 1:
                return {
                    "event_counter": 99,
                    "pending_request": {"id": "old"},
                }
            return {
                "event_counter": 1,
                "pending_request": {"id": "new"},
            }

        def _send_command(self, name, args=None, nonce=None):
            assert name == "load"
            self.load_nonce = nonce
            return True, "accepted"

        def poll(self, timeout=0):
            return [
                {
                    "type": "command_result",
                    "command": "load",
                    "success": True,
                    "nonce": "previous-client",
                },
                {
                    "type": "choice_request",
                    "id": "new",
                    "choices": ["Go"],
                },
            ]

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeStaleLoadSession())
    monkeypatch.setattr(cli, "_save_session", lambda session, args, state: None)

    args = Namespace(
        bridge="http://bridge",
        json=False,
        quiet=False,
        verbose=False,
        slot="stale",
        wait=True,
        timeout=30,
    )

    assert cli.cmd_load(args, object()) == 1
    out = capsys.readouterr().out
    assert "Load command was submitted but not confirmed" in out
    assert "--- CHOICE REQUIRED ---" not in out


def test_cmd_load_wait_still_shows_pending_after_screen_actions(monkeypatch, capsys):
    class FakeLoadScreenSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 12
        last_request_id = "old"
        last_request_type = "choice_request"
        last_choices = ["old"]
        state_calls = 0
        load_nonce = None

        def state(self):
            self.state_calls += 1
            if self.state_calls == 1:
                return {
                    "event_counter": 99,
                    "pending_request": {"id": "old"},
                }
            return {
                "event_counter": 1,
                "pending_request": {"id": "new"},
            }

        def _send_command(self, name, args=None, nonce=None):
            assert name == "load"
            self.load_nonce = nonce
            return True, "accepted"

        def poll(self, timeout=0):
            return [
                {
                    "type": "command_result",
                    "command": "load",
                    "success": True,
                    "nonce": self.load_nonce,
                },
                {
                    "type": "screen_content",
                    "interactions": [
                        {
                            "type": "choice",
                            "index": 1,
                            "display_label": "Go",
                        },
                    ],
                },
            ]

        def pending(self):
            return {"type": "choice_request", "id": "new", "choices": ["Go"]}

        def get_transcript(self, last_n=30):
            return []

        def _get(self, path, timeout=2.0):
            return 404, {}

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeLoadScreenSession())
    monkeypatch.setattr(cli, "_save_session", lambda session, args, state: None)
    monkeypatch.setattr(
        cli,
        "_format_current_screen_prompt",
        lambda session, args, **kwargs: "--- CHOICE REQUIRED ---\n  1: Go",
    )

    args = Namespace(
        bridge="http://bridge",
        json=False,
        quiet=False,
        verbose=False,
        slot="roadwarden-smoke",
        wait=True,
        timeout=30,
    )

    assert cli.cmd_load(args, object()) == 0
    out = capsys.readouterr().out
    assert "--- CHOICE REQUIRED ---" in out
    assert "Go" in out


def test_perform_wait_injects_current_screen_text_before_pending(capsys):
    class FakeLaggingScreenSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            return []

        def pending(self):
            return {
                "type": "choice_request",
                "id": "next",
                "choices": ["Continue"],
            }

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": self.pending(),
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {
                    "screen": {
                        "type": "screen_content",
                        "screens": ["nvl"],
                        "texts": ["Fresh narration from the current screen."],
                        "buttons": [
                            {
                                "label": "Continue",
                                "screen": "nvl",
                                "actions": ["ChoiceReturn"],
                            },
                        ],
                    },
                }
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeLaggingScreenSession(),
        args,
        FakeClientState(),
        timeout=0.2,
    ) == 0
    out = capsys.readouterr().out
    assert "Fresh narration from the current screen." in out
    assert out.index("Fresh narration") < out.index("--- CHOICE REQUIRED ---")


def test_perform_wait_waits_for_screen_text_before_bare_choice_event(capsys):
    class FakeBareChoiceSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0
            self.screen_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {
                        "type": "choice_request",
                        "id": "next",
                        "choices": ["Continue"],
                    },
                ]
            return []

        def pending(self):
            return {
                "type": "choice_request",
                "id": "next",
                "choices": ["Continue"],
            }

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": self.pending(),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            self.screen_calls += 1
            text = [] if self.screen_calls == 1 else ["Narration caught up."]
            return 200, {
                "screen": {
                    "type": "screen_content",
                    "screens": ["nvl"],
                    "texts": text,
                    "buttons": [
                        {
                            "label": "Continue",
                            "screen": "nvl",
                            "actions": ["ChoiceReturn"],
                        },
                    ],
                },
            }

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeBareChoiceSession(),
        args,
        FakeClientState(),
        timeout=0.2,
        initial_grace=1.2,
    ) == 0
    out = capsys.readouterr().out
    assert "Narration caught up." in out
    assert out.index("Narration caught up.") < out.index("--- CHOICE REQUIRED ---")


def test_perform_wait_defers_bare_continue_pending_without_initial_grace(
    monkeypatch,
    capsys,
):
    class FakeContinueRaceSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls < 2:
                return []
            return [
                {
                    "type": "narration",
                    "text": "The report continues.",
                },
                {
                    "type": "choice_request",
                    "id": "next",
                    "choices": ["(continue)"],
                },
            ]

        def pending(self):
            if self.poll_calls < 2:
                return {
                    "type": "choice_request",
                    "id": "continue",
                    "choices": ["(continue)"],
                }
            return {
                "type": "choice_request",
                "id": "next",
                "choices": ["(continue)"],
            }

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": self.pending(),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": {"buttons": []}}
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    now = {"value": 100.0}
    monkeypatch.setattr(cli.time, "time", lambda: now["value"])
    monkeypatch.setattr(
        cli.time,
        "sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    args = Namespace(json=False, quiet=False, verbose=False)
    session = FakeContinueRaceSession()

    assert cli._perform_wait(
        session,
        args,
        FakeClientState(),
        timeout=5.0,
    ) == 0
    out = capsys.readouterr().out
    assert session.poll_calls == 2
    assert "The report continues." in out
    assert "--- CHOICE REQUIRED ---" in out


def test_perform_wait_returns_bare_continue_screen_with_short_timeout(capsys):
    class FakeContinueScreenSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            return []

        def pending(self):
            return None

        def state(self):
            return {
                "status": "idle",
                "context": {"context": "in_game"},
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {
                    "screen": {
                        "type": "screen_content",
                        "screens": ["nvl"],
                        "texts": [],
                        "buttons": [
                            {
                                "label": "(continue)",
                                "screen": "nvl",
                                "actions": ["ChoiceReturn"],
                            },
                        ],
                        "interactions": [
                            {
                                "display_label": "(continue)",
                                "type": "choice",
                                "source": "button",
                                "index": 1,
                            },
                        ],
                    },
                }
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeContinueScreenSession(),
        args,
        FakeClientState(),
        timeout=0.1,
    ) == 0
    out = capsys.readouterr().out
    assert "(continue)" in out
    assert "--- OTHER BUTTONS ---" in out


def test_perform_wait_suppresses_stale_return_to_menu_before_fresh_choice(capsys):
    class FakeFreshChoiceAfterMenuEndSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {"type": "game_ended", "reason": "return_to_menu"},
                ]
            if self.poll_calls == 2:
                return [
                    {"type": "narration", "text": "The new scene begins."},
                    {
                        "type": "choice_request",
                        "id": "first",
                        "choices": ["Inspect the signal"],
                    },
                ]
            return []

        def pending(self):
            return {
                "type": "choice_request",
                "id": "first",
                "choices": ["Inspect the signal"],
            }

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": self.pending(),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            return 200, {
                "screen": {
                    "type": "screen_content",
                    "screens": ["choice"],
                    "buttons": [
                        {
                            "label": "Inspect the signal",
                            "screen": "choice",
                            "actions": ["ChoiceReturn"],
                        },
                    ],
                },
            }

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeFreshChoiceAfterMenuEndSession(),
        args,
        FakeClientState(),
        timeout=3.0,
        initial_grace=2.0,
    ) == 0
    out = capsys.readouterr().out
    assert "Game ended" not in out
    assert "The new scene begins." in out


def test_perform_wait_suppresses_return_to_menu_when_screen_actionable(capsys):
    class FakeMenuAfterReturnSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def get_transcript(self, last_n=30):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [{"type": "game_ended", "reason": "return_to_menu"}]
            return []

        def pending(self):
            return None

        def state(self):
            return {
                "status": "ended",
                "context": {"context": "main_menu"},
                "transcript": [],
            }

        def status(self):
            return {"status": "ended", "end_reason": "return_to_menu"}

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            return 200, {
                "screen": {
                    "type": "screen_content",
                    "screens": ["menu", "vnf_command_poller"],
                    "buttons": [
                        {
                            "label": "Start",
                            "screen": "main_menu",
                            "actions": ["Start"],
                        },
                        {
                            "label": "Load",
                            "screen": "main_menu",
                            "actions": ["ShowMenu"],
                        },
                    ],
                },
            }

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeMenuAfterReturnSession(),
        args,
        FakeClientState(),
        timeout=0.2,
        initial_grace=0,
    ) == 0
    out = capsys.readouterr().out
    assert "Game ended" not in out
    assert "Start" in out
    assert "Load" in out


def test_perform_wait_returns_post_action_main_menu_after_grace(capsys):
    class FakePostActionMenuReturnSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = "answered"

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def get_transcript(self, last_n=30):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [{"type": "game_ended", "reason": "return_to_menu"}]
            return []

        def pending(self):
            return None

        def state(self):
            return {
                "status": "ended",
                "context": {"context": "main_menu"},
                "transcript": [],
            }

        def status(self):
            return {"status": "ended", "end_reason": "return_to_menu"}

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            return 200, {
                "screen": {
                    "type": "screen_content",
                    "screens": ["menu", "vnf_command_poller"],
                    "buttons": [
                        {
                            "label": "Start",
                            "screen": "main_menu",
                            "actions": ["Start"],
                        },
                        {
                            "label": "Load",
                            "screen": "main_menu",
                            "actions": ["ShowMenu"],
                        },
                    ],
                },
            }

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakePostActionMenuReturnSession(),
        args,
        FakeClientState(),
        timeout=0.5,
        initial_grace=0.05,
        skip_request_id="answered",
    ) == 0
    out = capsys.readouterr().out
    assert "timeout" not in out.lower()
    assert "Game ended" not in out
    assert "Start" in out
    assert "Load" in out


def test_perform_wait_json_main_menu_strips_raw_button_refs(monkeypatch, capsys):
    class FakePostActionMenuReturnSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = "answered"

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def get_transcript(self, last_n=30):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [{"type": "game_ended", "reason": "return_to_menu"}]
            return []

        def pending(self):
            return None

        def state(self):
            return {
                "status": "ended",
                "context": {"context": "main_menu"},
                "transcript": [],
            }

        def status(self):
            return {"status": "ended", "end_reason": "return_to_menu"}

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            return 200, {
                "screen": {
                    "type": "screen_content",
                    "screens": ["menu", "vnf_command_poller"],
                    "buttons": [
                        {
                            "label": "Decorative",
                            "screen": "main_menu",
                            "actions": ["NullAction"],
                            "_displayable": "<Button>",
                            "_action_obj": "<NullAction>",
                        },
                    ],
                },
            }

    tick = iter(range(1000, 1100))
    monkeypatch.setattr(cli.time, "time", lambda: float(next(tick)))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    args = Namespace(json=True, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakePostActionMenuReturnSession(),
        args,
        FakeClientState(),
        timeout=30,
        initial_grace=0.05,
        skip_request_id="answered",
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "main_menu"
    assert payload["buttons"] == [
        {
            "label": "Decorative",
            "screen": "main_menu",
            "actions": ["NullAction"],
        },
    ]


def test_perform_wait_keeps_return_to_menu_for_nullaction_screen(capsys):
    class FakeDisabledMenuAfterReturnSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def get_transcript(self, last_n=30):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [{"type": "game_ended", "reason": "return_to_menu"}]
            return []

        def pending(self):
            return None

        def state(self):
            return {
                "status": "ended",
                "context": {"context": "main_menu"},
                "transcript": [],
            }

        def status(self):
            return {"status": "ended", "end_reason": "return_to_menu"}

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            return 200, {
                "screen": {
                    "type": "screen_content",
                    "screens": ["menu", "vnf_command_poller"],
                    "buttons": [
                        {
                            "label": "Decorative",
                            "screen": "main_menu",
                            "actions": ["NullAction"],
                        },
                    ],
                },
            }

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeDisabledMenuAfterReturnSession(),
        args,
        FakeClientState(),
        timeout=0.2,
        initial_grace=0,
    ) == 0
    out = capsys.readouterr().out
    assert "Game ended" in out
    assert "Decorative" not in out


def test_perform_wait_suppresses_stale_menu_screen_during_gameplay(capsys):
    pending = {
        "type": "choice_request",
        "id": "fresh",
        "choices": ["Enter the cabin"],
    }

    class FakeInGameMenuScreenSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def get_transcript(self, last_n=30):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {
                        "type": "narration",
                        "text": "The cabin waits at the end of the path.",
                    }
                ]
            if self.poll_calls == 2:
                return [pending]
            return []

        def pending(self):
            if self.poll_calls >= 2:
                return pending
            return None

        def state(self):
            return {
                "status": "running",
                "context": {"context": "in_game"},
                "pending_request": self.pending(),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            return 200, {
                "screen": {
                    "type": "screen_content",
                    "screens": ["menu", "vnf_command_poller"],
                    "buttons": [
                        {
                            "label": "New Game",
                            "screen": "main_menu",
                            "actions": ["Start"],
                        },
                    ],
                },
            }

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeInGameMenuScreenSession(),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0,
    ) == 0
    out = capsys.readouterr().out
    assert "The cabin waits" in out
    assert "Enter the cabin" in out
    assert "New Game" not in out


def test_perform_wait_inserts_catchup_narration_before_pending(capsys):
    pending = {
        "type": "choice_request",
        "id": "next",
        "choices": ["What is the plan?"],
    }

    class FakePendingBeforeNarrationSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [dict(pending)]
            if self.poll_calls == 2:
                return [
                    {
                        "type": "narration",
                        "text": "The valley narration arrives after the prompt.",
                    }
                ]
            return []

        def pending(self):
            return dict(pending)

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": dict(pending),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakePendingBeforeNarrationSession(),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.5,
    ) == 0
    out = capsys.readouterr().out
    assert "The valley narration arrives after the prompt." in out
    assert "What is the plan?" in out
    assert out.index("The valley narration") < out.index("--- CHOICE REQUIRED ---")


def test_perform_wait_defers_bare_pending_until_story_grace(capsys):
    pending = {
        "type": "choice_request",
        "id": "next",
        "choices": ["What is the plan?"],
    }

    class FakeDelayedNarrationSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [dict(pending)]
            if self.poll_calls == 2:
                return []
            if self.poll_calls == 3:
                return [
                    {
                        "type": "narration",
                        "text": "The delayed story arrives before the prompt.",
                    }
                ]
            return []

        def pending(self):
            return dict(pending)

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": dict(pending),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeDelayedNarrationSession(),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.5,
    ) == 0
    out = capsys.readouterr().out
    assert "The delayed story arrives before the prompt." in out
    assert "What is the plan?" in out
    assert out.index("The delayed story") < out.index("--- CHOICE REQUIRED ---")


def test_perform_wait_returns_prompt_when_no_catchup_narration(capsys):
    pending = {
        "type": "choice_request",
        "id": "next",
        "choices": ["Continue without narration."],
    }

    class FakePromptOnlySession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            super().__init__(None)
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [dict(pending)]
            return []

        def pending(self):
            return dict(pending)

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": dict(pending),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakePromptOnlySession(),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.5,
    ) == 0
    out = capsys.readouterr().out
    assert "Continue without narration." in out
    assert "--- CHOICE REQUIRED ---" in out


def test_perform_wait_skips_stale_choice_event_by_label_signature(capsys):
    class FakeStaleChoiceSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {"type": "narration", "text": "The answer changes everything."},
                    {
                        "type": "choice_request",
                        "id": "renpy-reused-id",
                        "choices": ["Ask the question", "Shut it down"],
                    },
                ]
            if self.poll_calls == 2:
                return [
                    {
                        "type": "screen_content",
                        "screens": ["observatory_map"],
                        "buttons": [
                            {
                                "label": "Generator Room",
                                "screen": "observatory_map",
                                "actions": ["Return"],
                            },
                        ],
                    },
                ]
            return []

        def pending(self):
            return None

        def state(self):
            return {
                "status": "running",
                "context": {"context": "in_game"},
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeStaleChoiceSession(),
        args,
        FakeClientState(),
        timeout=2,
        initial_grace=1,
        skip_request_id="pre-action-id",
        skip_request_choices=["Ask the question", "Shut it down"],
    ) == 0
    out = capsys.readouterr().out
    assert "The answer changes everything." in out
    assert "Generator Room" in out
    assert "Ask the question" not in out
    assert "--- CHOICE REQUIRED ---" not in out


def test_perform_wait_prefers_screen_when_stale_pending_matches_skip_labels(capsys):
    stale_pending = {
        "type": "choice_request",
        "id": "renpy-reused-id",
        "choices": ["Ask the question", "Shut it down"],
    }
    screen = {
        "type": "screen_content",
        "screens": ["observatory_map"],
        "buttons": [
            {
                "label": "Generator Room",
                "screen": "observatory_map",
                "actions": ["Return"],
            },
        ],
    }

    class FakeStalePendingSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            super().__init__(screen)
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {"type": "narration", "text": "The answer changes everything."},
                    dict(stale_pending),
                ]
            return []

        def pending(self):
            return dict(stale_pending)

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": dict(stale_pending),
                "screen": screen,
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeStalePendingSession(),
        args,
        FakeClientState(),
        timeout=2,
        initial_grace=1,
        skip_request_id="pre-action-id",
        skip_request_choices=["Ask the question", "Shut it down"],
    ) == 0
    out = capsys.readouterr().out
    assert "The answer changes everything." in out
    assert "Generator Room" in out
    assert "Ask the question" not in out
    assert "--- CHOICE REQUIRED ---" not in out


def test_perform_wait_shows_same_label_menu_after_story(capsys):
    class FakeSameMenuSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {"type": "narration", "text": "You ask about the signal."},
                    {
                        "type": "choice_request",
                        "id": "hub",
                        "choices": ["Ask the question", "Shut it down"],
                    },
                ]
            return []

        def pending(self):
            return {
                "type": "choice_request",
                "id": "hub",
                "choices": ["Ask the question", "Shut it down"],
            }

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": self.pending(),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeSameMenuSession(),
        args,
        FakeClientState(),
        timeout=2,
        initial_grace=1,
        skip_request_id="hub",
        skip_request_choices=["Ask the question", "Shut it down"],
    ) == 0
    out = capsys.readouterr().out
    assert "You ask about the signal." in out
    assert "--- CHOICE REQUIRED ---" in out
    assert "Ask the question" in out


def test_perform_wait_does_not_hide_reused_pending_id_until_timeout(capsys):
    pending = {
        "type": "choice_request",
        "id": "reused-id",
        "choices": ["Fresh follow-up choice"],
    }

    class FakeReusedIdSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            return []

        def pending(self):
            return dict(pending)

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": dict(pending),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeReusedIdSession(),
        args,
        FakeClientState(),
        timeout=0.3,
        initial_grace=0.1,
        skip_request_id="reused-id",
    ) == 0
    out = capsys.readouterr().out
    assert "Fresh follow-up choice" in out
    assert "timeout" not in out


def test_perform_wait_does_not_reprint_drained_duplicate_narration(capsys):
    text = "You steady your breath as the bird charges."
    pending = {
        "type": "choice_request",
        "id": "next",
        "choices": ["I take a good look at the monster."],
    }

    class FakeDuplicateDrainSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {"type": "narration", "text": text},
                    dict(pending),
                ]
            if self.poll_calls == 2:
                return [{"type": "narration", "text": text}]
            return []

        def pending(self):
            return dict(pending)

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": dict(pending),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeDuplicateDrainSession(),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.5,
    ) == 0
    out = capsys.readouterr().out
    assert out.count(text) == 1
    assert "I take a good look at the monster." in out


def test_perform_wait_trace_reports_duplicate_drain_suppression(capsys):
    text = "You steady your breath as the bird charges."
    pending = {
        "type": "choice_request",
        "id": "next",
        "choices": ["I take a good look at the monster."],
    }

    class FakeDuplicateDrainSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {"type": "narration", "text": text},
                    dict(pending),
                ]
            if self.poll_calls == 2:
                return [{"type": "narration", "text": text}]
            return []

        def pending(self):
            return dict(pending)

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": dict(pending),
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False, trace_wait=True)

    assert cli._perform_wait(
        FakeDuplicateDrainSession(),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.5,
    ) == 0
    captured = capsys.readouterr()
    assert captured.out.count(text) == 1
    assert "[wait-trace]" in captured.err
    assert "skip_duplicate_pre_drain_narration" in captured.err
    assert "return_pending" in captured.err


def test_perform_wait_does_not_report_main_menu_when_live_actions_exist(capsys):
    screen = {
        "type": "screen_content",
        "screens": ["menu", "startbox"],
        "buttons": [
            {
                "label": "Begin the journey",
                "screen": "startbox",
                "actions": ["ToggleScreen"],
            }
        ],
    }

    class FakeStaleMainMenuSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            return []

        def pending(self):
            return None

        def state(self):
            return {
                "status": "ended",
                "end_reason": "return_to_menu",
                "context": {"context": "main_menu"},
                "pending_request": None,
                "transcript": [],
            }

        def status(self):
            return {"status": "ended", "end_reason": "return_to_menu"}

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": screen}
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeStaleMainMenuSession(),
        args,
        FakeClientState(),
        timeout=0.5,
    ) == 0
    out = capsys.readouterr().out
    assert "Begin the journey" in out
    assert "Returned to main menu" not in out
    assert "Game ended" not in out


def test_perform_wait_does_not_return_quick_menu_during_post_action_grace(capsys):
    quick_menu = {
        "type": "screen_content",
        "screens": ["quick_menu"],
        "buttons": [
            {"label": "Q. Save", "screen": "quick_menu", "actions": ["FileSave"]},
            {"label": "Q. Load", "screen": "quick_menu", "actions": ["FileLoad"]},
        ],
        "interactions": [
            {
                "category": "navigation",
                "type": "nav",
                "display_label": "Q. Save",
                "screen": "quick_menu",
                "disabled": False,
            },
            {
                "category": "navigation",
                "type": "nav",
                "display_label": "Q. Load",
                "screen": "quick_menu",
                "disabled": False,
            },
        ],
    }
    pending = {
        "type": "choice_request",
        "id": "camp",
        "choices": ["Look around."],
    }

    class FakeQuickMenuTransitionSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [{"type": "hide", "name": "difficultypick"}]
            if self.poll_calls == 2:
                return [
                    {"type": "narration", "text": "The camp is quiet."},
                    dict(pending),
                ]
            return []

        def pending(self):
            return dict(pending) if self.poll_calls >= 2 else None

        def state(self):
            return {
                "status": "running",
                "context": {"context": "in_game"},
                "pending_request": self.pending(),
                "game_state": {"screen_buttons": quick_menu["buttons"]},
                "screen": quick_menu,
                "transcript": [],
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": quick_menu}
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeQuickMenuTransitionSession(),
        args,
        FakeClientState(),
        timeout=2.0,
        initial_grace=0.5,
    ) == 0
    out = capsys.readouterr().out
    assert "The camp is quiet." in out
    assert "Look around." in out
    assert "Q. Save" not in out


def test_perform_wait_suppresses_stale_main_menu_until_story(capsys):
    main_menu = {
        "type": "screen_content",
        "screens": ["menu", "main_menu"],
        "buttons": [
            {"label": "Start", "screen": "main_menu", "actions": ["Start"]},
            {"label": "Load", "screen": "main_menu", "actions": ["ShowMenu"]},
        ],
    }
    pending = {
        "type": "choice_request",
        "id": "first-choice",
        "choices": ["Go inside."],
    }

    class FakeInputTransitionSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0
            self.screen_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls < 3:
                return []
            if self.poll_calls == 3:
                return [
                    {"type": "narration", "text": "The first story line."},
                ]
            return [
                {"type": "narration", "text": "The story starts after input."},
                dict(pending),
            ]

        def pending(self):
            return dict(pending) if self.poll_calls >= 3 else None

        def state(self):
            if self.poll_calls >= 3:
                return {
                    "status": "waiting_for_input",
                    "context": {"context": "in_game"},
                    "pending_request": dict(pending),
                    "transcript": [],
                }
            return {
                "status": "idle",
                "context": {"context": "main_menu"},
                "pending_request": None,
                "screen": main_menu,
                "transcript": [],
            }

        def status(self):
            return {"status": "running"} if self.poll_calls >= 3 else {
                "status": "ended",
                "end_reason": "return_to_menu",
            }

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                self.screen_calls += 1
                if self.screen_calls == 1:
                    return 200, {"screen": {"screens": ["input"], "buttons": []}}
                if self.screen_calls == 2:
                    return 200, {"screen": dict(main_menu, screens=["menu"])}
                return 200, {"screen": main_menu}
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeInputTransitionSession(),
        args,
        FakeClientState(),
        timeout=2.0,
        initial_grace=0.5,
    ) == 0
    out = capsys.readouterr().out
    assert "The story starts after input." in out
    assert "Go inside." in out
    assert "Start  |  Load" not in out


def test_cmd_input_uses_longer_story_grace(monkeypatch):
    captured = {}

    class FakeInputSession:
        last_request_id = "name-input"

        def pending(self):
            return {
                "type": "input_request",
                "id": "name-input",
                "prompt": "What is your name?",
            }

        def input_text(self, text):
            return {"ok": True, "message": "submitted"}

    def fake_wait(session, args, client_state, timeout, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeInputSession())
    monkeypatch.setattr(cli, "_save_session", lambda session, args, state: None)
    monkeypatch.setattr(cli, "_perform_wait", fake_wait)

    args = Namespace(
        text=["Alex"],
        wait=True,
        timeout=120,
        json=False,
        quiet=False,
    )

    assert cli.cmd_input(args, FakeClientState()) == 0
    assert captured["initial_grace"] == 5.0
    assert captured["skip_request_id"] == "name-input"


def test_cmd_input_saves_the_story_the_post_input_hook_held(monkeypatch):
    """Mystic Cafe's opening narration arrives while the post-input hook
    polls; the hook holds it in _prefetched_events.  The session used to be
    saved BEFORE the hook ran, so the held rows died with the process and
    the next `wait` printed only navigation."""
    saves = []

    class FakeInputSession:
        last_request_id = "name-input"
        _prefetched_events = []

        def pending(self):
            return {"type": "input_request", "id": "name-input",
                    "prompt": "What is your name?"}

        def input_text(self, text):
            return {"ok": True, "message": "submitted"}

    def fake_hook(ctx, result, text):
        ctx.client._prefetched_events.append(
            {"type": "dialogue", "character": "Narrator",
             "text": "The city felt different tonight."})

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeInputSession())
    monkeypatch.setattr(cli, "_run_after_input_text_hook", fake_hook)
    monkeypatch.setattr(
        cli, "_save_session",
        lambda session, args, state: saves.append(list(session._prefetched_events)))

    args = Namespace(text=["Mira"], wait=False, timeout=None, json=False, quiet=False)
    assert cli.cmd_input(args, FakeClientState()) == 0

    assert saves, "the session was never saved"
    assert saves[-1] and saves[-1][0]["text"] == "The city felt different tonight."


def test_cmd_input_auto_confirms_roadwarden_confirm_screen(monkeypatch):
    captured = {}

    class FakeRoadwardenInputSession:
        last_request_id = "search-input"

        def __init__(self):
            self.calls = []

        def pending(self):
            self.calls.append(("pending", None))
            return {
                "type": "input_request",
                "id": "search-input",
                "prompt": "Which person or service are you looking for?",
            }

        def input_text(self, text):
            self.calls.append(("input_text", text))
            return {"ok": True, "message": "submitted"}

        def state(self):
            self.calls.append(("state", None))
            return {
                "custom_commands": ["after_input_text"],
            }

        def command(self, name, **kwargs):
            self.calls.append(("command", (name, kwargs)))
            return {
                "success": True,
                "handled": True,
                "auto_confirmed": True,
                "resolved_as": "button",
                "label": "Confirm",
            }

        def _get(self, path, timeout=5.0):
            return 404, None

    session = FakeRoadwardenInputSession()

    def fake_wait(session_arg, args, client_state, timeout, **kwargs):
        captured["wait_session"] = session_arg
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(cli, "_save_session", lambda session, args, state: None)
    monkeypatch.setattr(cli, "_perform_wait", fake_wait)

    args = Namespace(
        text=["Tatius"],
        wait=True,
        timeout=120,
        json=False,
        quiet=True,
    )

    assert cli.cmd_input(args, FakeClientState()) == 0
    assert ("input_text", "Tatius") in session.calls
    assert ("command", ("after_input_text", {
        "text": "Tatius", "_deadline": ANY,
    })) in session.calls
    assert captured["kwargs"]["skip_request_id"] == "search-input"
    assert captured["kwargs"]["stale_screen_label"] == "Confirm"


def test_cmd_state_json_uses_settled_state_not_raw_bridge_state(monkeypatch, capsys):
    screen = {
        "type": "screen_content",
        "screens": ["menu", "startbox"],
        "buttons": [
            {
                "label": "Begin the journey",
                "screen": "startbox",
                "actions": ["ToggleScreen"],
            }
        ],
    }

    class FakeStateSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def state(self):
            return {
                "status": "ended",
                "end_reason": "return_to_menu",
                "context": {"context": "main_menu"},
                "pending_request": None,
                "game_state": {"screen_buttons": screen["buttons"]},
                "screen": screen,
                "transcript": [{"type": "narration", "text": "old raw history"}],
            }

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": screen}
            return 404, {}

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeStateSession())

    args = Namespace(json=True, quiet=False, verbose=False)

    assert cli.cmd_state(args, FakeClientState()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "screen_actions"
    assert data["buttons"]["other"] == ["Begin the journey"]
    assert "transcript" not in data
    assert "_raw_status" not in data
    assert "_lifecycle" not in data
    assert "_interactions" not in data


def test_cmd_state_suppresses_input_prompt_on_screen_preamble(monkeypatch, capsys):
    prompt = "Which place are you asking about?"
    screen = {
        "type": "screen_content",
        "screens": ["input"],
        "texts": [prompt],
        "buttons": [{"label": "Confirm", "screen": "confirm"}],
    }

    class FakeStateSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def state(self):
            return {
                "status": "waiting_for_input",
                "pending_request": {
                    "type": "input_request",
                    "prompt": prompt,
                },
                "screen": screen,
                "transcript": [],
            }

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": screen}
            return 404, {}

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeStateSession())

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli.cmd_state(args, FakeClientState()) == 0
    out = capsys.readouterr().out
    assert "On Screen:" not in out
    assert out.count(prompt) == 1
    assert "--- INPUT REQUIRED ---" in out


def test_perform_wait_defers_screen_prompt_until_post_click_story(capsys):
    topic_screen = {
        "type": "screen_content",
        "screens": ["nvl"],
        "interactions": [
            {
                "category": "topics",
                "type": "topic",
                "index": 1,
                "display_label": "Ask another question.",
            }
        ],
    }

    class FakeScreenFirstTopicSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            super().__init__(topic_screen)
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [dict(topic_screen)]
            if self.poll_calls == 2:
                return [
                    {
                        "type": "narration",
                        "text": "The answer arrives after the topic menu refresh.",
                    },
                ]
            return []

        def pending(self):
            return None

        def state(self):
            return {"status": "running", "context": {"context": "in_game"}}

        def status(self):
            return {"status": "running"}

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeScreenFirstTopicSession(),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.5,
    ) == 0
    out = capsys.readouterr().out
    assert "The answer arrives after the topic menu refresh." in out
    assert "Ask another question." in out
    assert out.index("The answer arrives") < out.index("Ask another question.")


def test_current_screen_prompt_renders_idle_interactions():
    class FakeIdleSession(FakeScreenSession):
        def pending(self):
            return None

    screen = {
        "type": "screen_content",
        "interactions": [
            {
                "category": "topics",
                "type": "topic",
                "index": 2,
                "display_label": "What can you sell me?",
            },
        ],
    }
    args = Namespace(json=False, quiet=False, verbose=False)

    text = cli._format_current_screen_prompt(FakeIdleSession(screen), args)

    assert "--- TOPICS ---" in text
    assert "What can you sell me?" in text


def test_current_screen_prompt_does_not_revive_old_choices(monkeypatch):
    class Session:
        def pending(self):
            return None

    monkeypatch.setattr(cli, "_get_live_screen", lambda session: {
        "screens": ["menu"], "buttons": [{"label": "Start", "actions": ["Start"]}],
    })
    monkeypatch.setattr(cli, "_get_latest_interactions", lambda *a, **k: [
        {"type": "choice", "index": 1, "label": "Old cafe choice"},
    ])
    text = cli._format_current_screen_prompt(
        Session(), Namespace(json=False, quiet=False, verbose=False))
    assert "Start" in text
    assert "Old cafe choice" not in text


def test_current_screen_prompt_preserves_newer_interactions_without_live_screen():
    class Session(FakeScreenSession):
        def pending(self):
            return None

        def get_transcript(self, last_n=30):
            return [
                {"type": "screen_content", "screens": ["menu"],
                 "buttons": [{"label": "Old Start", "actions": ["Start"]}]},
                {"type": "screen_content", "buttons": [], "interactions": [
                    {"type": "topic", "category": "topics", "index": 1,
                     "display_label": "Current topic"}]},
            ]

    text = cli._format_current_screen_prompt(
        Session(None), Namespace(json=False, quiet=False, verbose=False))
    assert "Current topic" in text
    assert "Old Start" not in text


def test_wait_for_load_ready_accepts_new_idle_interactions():
    class FakeReadySession(FakeScreenSession):
        cursor = 12
        state_calls = 0

        def state(self):
            self.state_calls += 1
            return {"event_counter": 13, "pending_request": None}

    screen = {
        "type": "screen_content",
        "interactions": [
            {
                "category": "topics",
                "type": "topic",
                "index": 2,
                "display_label": "What can you sell me?",
            },
        ],
    }
    session = FakeReadySession(screen)

    assert cli._wait_for_load_ready(
        session,
        preload_cursor=12,
        preload_pending_id=None,
        timeout=0.2,
    ) is True
    assert session.cursor == 12


def test_perform_wait_returns_idle_screen_interactions(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    class FakeIdleWaitSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            return []

        def pending(self):
            return None

        def state(self):
            return {"status": "idle", "context": None}

        def status(self):
            return {"status": "idle"}

    screen = {
        "type": "screen_content",
        "interactions": [
            {
                "category": "topics",
                "type": "topic",
                "index": 2,
                "display_label": "What can you sell me?",
            },
        ],
    }
    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeIdleWaitSession(screen),
        args,
        FakeClientState(),
        timeout=0.2,
    ) == 0
    out = capsys.readouterr().out
    assert "--- TOPICS ---" in out
    assert "What can you sell me?" in out


def test_perform_wait_json_returns_idle_screen_interactions(capsys):
    class FakeIdleWaitSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            return []

        def pending(self):
            return None

        def state(self):
            return {"status": "idle", "context": None}

        def status(self):
            return {"status": "idle"}

    screen = {
        "type": "screen_content",
        "screens": ["menu"],
        "buttons": [
            {
                "label": "Load",
                "screen": "menu",
                "enabled": True,
                "_displayable": "<raw renpy object>",
            },
        ],
        "interactions": [
            {
                "category": "navigation",
                "type": "nav",
                "index": 2,
                "display_label": "Load",
            },
        ],
    }
    args = Namespace(json=True, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeIdleWaitSession(screen),
        args,
        FakeClientState(),
        timeout=0.2,
    ) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "screen_interactions"
    assert data["screens"] == ["menu"]
    assert data["buttons"][0]["label"] == "Load"
    assert "_displayable" not in data["buttons"][0]
    assert data["interactions"][0]["display_label"] == "Load"


def test_perform_wait_returns_running_screen_topics(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    class FakeRunningTopicSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            return []

        def pending(self):
            return None

        def state(self):
            return {"status": "running", "context": None}

        def status(self):
            return {"status": "running"}

    screen = {
        "type": "screen_content",
        "interactions": [
            {
                "category": "topics",
                "type": "topic",
                "index": 1,
                "display_label": "Any rumors worth sharing?",
            },
            {
                "category": "navigation",
                "type": "nav",
                "display_label": "Settings",
            },
        ],
    }
    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeRunningTopicSession(screen),
        args,
        FakeClientState(),
        timeout=0.2,
    ) == 0
    out = capsys.readouterr().out
    assert "--- TOPICS ---" in out
    assert "Any rumors worth sharing?" in out


def test_perform_wait_does_not_return_stale_screen_prompt_during_grace(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    fresh_pending = {
        "type": "choice_request",
        "id": "fresh",
        "choices": ["I am on it."],
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "I am on it.",
            }
        ],
    }

    class FakePostClickSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self, screen):
            super().__init__(screen)
            self.poll_calls = 0
            self.fresh = False

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {
                        "type": "command_result",
                        "command": "act",
                        "success": True,
                    }
                ]
            if self.poll_calls == 2:
                self.fresh = True
                return [fresh_pending]
            return []

        def pending(self):
            return fresh_pending if self.fresh else None

        def state(self):
            return {"status": "running", "context": None}

        def status(self):
            return {"status": "running"}

    stale_screen = {
        "type": "screen_content",
        "interactions": [
            {
                "category": "topics",
                "type": "topic",
                "index": 1,
                "display_label": "Old topic menu",
            },
        ],
    }
    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakePostClickSession(stale_screen),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.5,
    ) == 0
    out = capsys.readouterr().out
    assert "I am on it." in out
    assert "Old topic menu" not in out


def test_perform_wait_skips_unchanged_post_action_screen_prompt(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    fresh_pending = {
        "type": "choice_request",
        "id": "courtyard",
        "choices": ["I enter the inn."],
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "I enter the inn.",
            }
        ],
    }

    class FakeTopicClickSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self, screen):
            super().__init__(screen)
            self.poll_calls = 0
            self.fresh = False

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return []
            self.fresh = True
            return [
                {"type": "narration", "text": "You step outside."},
                fresh_pending,
            ]

        def pending(self):
            return fresh_pending if self.fresh else None

        def state(self):
            return {"status": "running", "context": None}

        def status(self):
            return {"status": "running"}

    stale_screen = {
        "type": "screen_content",
        "interactions": [
            {
                "category": "topics",
                "type": "topic",
                "index": 7,
                "display_label": "That's all I need.",
            },
        ],
    }
    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeTopicClickSession(stale_screen),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.1,
    ) == 0
    out = capsys.readouterr().out
    assert "You step outside." in out
    assert "I enter the inn." in out
    assert "That's all I need." not in out


def test_perform_wait_skips_screen_prompt_with_clicked_label(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    fresh_pending = {
        "type": "choice_request",
        "id": "courtyard",
        "choices": ["I enter the inn."],
    }

    class FakeTopicClickSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self, screen):
            super().__init__(screen)
            self.poll_calls = 0
            self.fresh = False

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return []
            self.fresh = True
            return [
                {"type": "narration", "text": "You step outside."},
                fresh_pending,
            ]

        def pending(self):
            return fresh_pending if self.fresh else None

        def state(self):
            return {"status": "idle", "context": None}

        def status(self):
            return {"status": "running"}

    stale_screen = {
        "type": "screen_content",
        "interactions": [
            {
                "category": "topics",
                "type": "topic",
                "index": 7,
                "display_label": "That is all I need.",
            },
        ],
        "texts": ["A newly scraped but stale copy of the topic screen."],
    }
    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeTopicClickSession(stale_screen),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.1,
        stale_screen_label="That is all I need.",
    ) == 0
    out = capsys.readouterr().out
    assert "You step outside." in out
    assert "I enter the inn." in out
    assert "That is all I need." not in out


def test_perform_wait_drops_stale_screen_event_with_clicked_label(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    fresh_pending = {
        "type": "choice_request",
        "id": "courtyard",
        "choices": ["I enter the inn."],
    }

    class FakeTopicClickSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self, screen):
            super().__init__(screen)
            self.poll_calls = 0
            self.fresh = False

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {
                        "type": "screen_content",
                        "texts": ["Old inn text."],
                        "buttons": [
                            {
                                "label": "That is all I need.",
                                "screen": "nvl",
                            }
                        ],
                        "interactions": [
                            {
                                "category": "topics",
                                "type": "topic",
                                "display_label": "That is all I need.",
                            }
                        ],
                    }
                ]
            self.fresh = True
            return [
                {"type": "narration", "text": "You step outside."},
                fresh_pending,
            ]

        def pending(self):
            return fresh_pending if self.fresh else None

        def state(self):
            return {"status": "running", "context": None}

        def status(self):
            return {"status": "running"}

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeTopicClickSession(None),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.1,
        stale_screen_label="That is all I need.",
    ) == 0
    out = capsys.readouterr().out
    assert "You step outside." in out
    assert "I enter the inn." in out
    assert "Old inn text." not in out
    assert "That is all I need." not in out


def test_perform_wait_ignores_nav_only_screen_before_fresh_choice(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    fresh_pending = {
        "type": "choice_request",
        "id": "shop-confirm",
        "choices": ["Let me take a look first."],
    }

    class FakeTopicClickSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            super().__init__(None)
            self.poll_calls = 0
            self.fresh = False

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {"type": "narration", "text": "She brings out the sack."},
                    {
                        "type": "screen_content",
                        "buttons": [
                            {"label": "Character", "screen": "quick_menu"},
                            {"label": "Inventory", "screen": "quick_menu"},
                        ],
                        "interactions": [
                            {
                                "category": "navigation",
                                "type": "nav",
                                "display_label": "Character",
                            },
                            {
                                "category": "navigation",
                                "type": "nav",
                                "display_label": "Inventory",
                            },
                        ],
                    },
                ]
            self.fresh = True
            return [fresh_pending]

        def pending(self):
            return fresh_pending if self.fresh else None

        def state(self):
            return {"status": "running", "context": None}

        def status(self):
            return {"status": "running"}

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeTopicClickSession(),
        args,
        FakeClientState(),
        timeout=1.0,
        initial_grace=0.1,
    ) == 0
    out = capsys.readouterr().out
    assert "She brings out the sack." in out
    assert "Let me take a look first." in out
    assert "--- NAVIGATION ---" not in out


def test_perform_wait_eventually_shows_same_topic_menu_after_return(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    class FakeTopicReturnSession(FakeScreenSession):
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            super().__init__({
                "type": "screen_content",
                "texts": ["You collect your belongings."],
                "interactions": [
                    {
                        "category": "topics",
                        "type": "topic",
                        "index": 1,
                        "display_label": "I have some things for sale.",
                    },
                    {
                        "category": "topics",
                        "type": "topic",
                        "index": 2,
                        "display_label": "Time for me to go.",
                    },
                ],
            })
            self.poll_calls = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {
                        "type": "narration",
                        "text": "You collect your belongings.",
                    }
                ]
            return []

        def pending(self):
            return None

        def state(self):
            return {"status": "idle", "context": None}

        def status(self):
            return {"status": "running"}

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeTopicReturnSession(),
        args,
        FakeClientState(),
        timeout=3.0,
        initial_grace=0.1,
        stale_screen_label="I have some things for sale.",
    ) == 0
    out = capsys.readouterr().out
    assert "You collect your belongings." in out
    assert "--- TOPICS ---" in out
    assert "Time for me to go." in out


def test_screen_event_contains_label_handles_quote_variants():
    event = {
        "interactions": [
            {"display_label": '"That\'s all I need." I go outside.'},
        ],
    }

    assert cli._screen_event_contains_label(
        event,
        "“That’s all I need.” I go outside.",
    )


def test_perform_wait_drains_stale_map_pending(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    stale_pending = {
        "type": "choice_request",
        "id": "western-crossroads",
        "choices": [
            "I approach the western signpost.",
            "I search the area.",
        ],
    }
    fresh_pending = {
        "type": "choice_request",
        "id": "howlers-dell",
        "choices": [
            "I go to the main square.",
            "I go to Elpis, the druidess.",
        ],
    }

    class FakeMapTravelSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.pending_calls = 0
            self.drained = False
            self._prefetched_events = []

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            if self._prefetched_events:
                events = self._prefetched_events
                self._prefetched_events = []
                return events
            if timeout >= 1.0 and not self.drained:
                self.drained = True
                return [
                    {"type": "narration", "text": "You ride through the gate."},
                    fresh_pending,
                ]
            return []

        def pending(self):
            self.pending_calls += 1
            return stale_pending if not self.drained else fresh_pending

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": self.pending(),
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": None}
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeMapTravelSession(),
        args,
        FakeClientState(),
        timeout=2.0,
        drain_stale_map_pending=True,
    ) == 0
    out = capsys.readouterr().out
    assert "You ride through the gate" in out
    assert "I go to the main square" in out
    assert "western signpost" not in out


def test_drain_stale_pending_request_uses_bounded_timeout():
    stale_pending = {
        "type": "choice_request",
        "id": "western-crossroads",
        "choices": ["I approach the western signpost."],
    }

    class FakeNoFreshSession:
        def __init__(self):
            self.poll_timeouts = []
            self._prefetched_events = []

        def poll(self, timeout=0):
            self.poll_timeouts.append(timeout)
            return [{"type": "narration", "text": "Still travelling."}]

        def pending(self):
            return stale_pending

    session = FakeNoFreshSession()

    result = drain_stale_pending_request(session, stale_pending, timeout=0.1)

    assert result is stale_pending
    assert session.poll_timeouts
    assert session.poll_timeouts[0] <= 0.1
    assert session._prefetched_events
    assert session._prefetched_events[0] == {
        "type": "narration",
        "text": "Still travelling.",
    }


def test_perform_wait_prints_settled_screen_before_returning(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    stale_screen = {
        "type": "screen_content",
        "screens": ["inventory"],
        "buttons": [{"label": "Small Healing Potion", "screen": "menu"}],
        "interactions": [
            {
                "category": "Supplies",
                "type": "item",
                "index": 1,
                "display_label": "Small Healing Potion",
            },
        ],
    }
    settled_screen = {
        "type": "screen_content",
        "screens": ["inventory"],
        "buttons": [{"label": "Drink the potion", "screen": "menu"}],
        "interactions": [
            {
                "category": "item_action",
                "type": "nav",
                "index": 1,
                "display_label": "Drink the potion",
            },
        ],
    }

    class FakeSettlingSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.screen_polls = [stale_screen, settled_screen, settled_screen]

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            return [stale_screen]

        def pending(self):
            return None

        def state(self):
            return {"status": "running", "context": {"context": "in_game"}}

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            if self.screen_polls:
                return 200, {"screen": self.screen_polls.pop(0)}
            return 200, {"screen": settled_screen}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeSettlingSession(),
        args,
        FakeClientState(),
        timeout=0.2,
    ) == 0
    out = capsys.readouterr().out
    assert "Small Healing Potion" not in out
    assert "Drink the potion" in out


def test_perform_wait_json_returns_event_batch_interactions(capsys):
    screen = {
        "type": "screen_content",
        "screens": ["terminal_topics"],
        "buttons": [{"label": "Ask ECHO-7", "screen": "terminal_topics"}],
        "interactions": [
            {
                "category": "topics",
                "type": "topic",
                "index": 1,
                "display_label": "Ask ECHO-7",
            },
        ],
    }

    class FakeJsonScreenEventSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_count = 0

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            self.poll_count += 1
            if self.poll_count == 1:
                return [screen]
            return []

        def pending(self):
            return None

        def state(self):
            return {"status": "running", "context": {"context": "in_game"}}

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": screen}
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=True, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeJsonScreenEventSession(),
        args,
        FakeClientState(),
        timeout=0.2,
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "screen_interactions"
    assert payload["buttons"][0]["label"] == "Ask ECHO-7"
    assert payload["screens"] == ["terminal_topics"]
    assert payload["interactions"][0]["display_label"] == "Ask ECHO-7"


def test_perform_wait_settles_overlay_before_showing_pending(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    stale_screen = {
        "type": "screen_content",
        "screens": ["nvl"],
        "buttons": [{"label": "Inventory", "screen": "quick_menu"}],
    }
    settled_screen = {
        "type": "screen_content",
        "screens": ["inventory"],
        "overlay_active": True,
        "buttons": [{"label": "Small Healing Potion", "screen": "menu"}],
        "interactions": [
            {
                "category": "Supplies",
                "type": "item",
                "index": 1,
                "display_label": "Small Healing Potion",
            },
        ],
    }

    class FakeOverlaySettlingSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.screen_polls = [stale_screen, settled_screen, settled_screen]

        def transcript(self, last=20):
            return []

        def poll(self, timeout=0):
            return []

        def pending(self):
            return {
                "type": "choice_request",
                "id": "old-scene",
                "choices": ["I approach Foggy.", "I go outside."],
            }

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": self.pending(),
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            if self.screen_polls:
                return 200, {"screen": self.screen_polls.pop(0)}
            return 200, {"screen": settled_screen}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=False, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeOverlaySettlingSession(),
        args,
        FakeClientState(),
        timeout=0.2,
    ) == 0
    out = capsys.readouterr().out
    assert "Small Healing Potion" in out
    assert "I approach Foggy" not in out


def test_perform_wait_json_settles_overlay_before_pending(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    stale_screen = {
        "type": "screen_content",
        "screens": ["nvl"],
        "buttons": [{"label": "Inventory", "screen": "quick_menu"}],
    }
    settled_screen = {
        "type": "screen_content",
        "screens": ["inventory"],
        "overlay_active": True,
        "buttons": [{"label": "Small Healing Potion", "screen": "menu"}],
        "interactions": [
            {
                "category": "Supplies",
                "type": "item",
                "index": 1,
                "display_label": "Small Healing Potion",
            },
        ],
    }

    class FakeOverlaySettlingSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.screen_polls = [stale_screen, settled_screen, settled_screen]

        def transcript(self, last=20):
            return [{"type": "narration", "text": "Old transcript line."}]

        def poll(self, timeout=0):
            return []

        def pending(self):
            return {
                "type": "choice_request",
                "id": "old-scene",
                "choices": ["I approach Foggy.", "I go outside."],
            }

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": self.pending(),
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path != "/screen":
                return 404, {}
            if self.screen_polls:
                return 200, {"screen": self.screen_polls.pop(0)}
            return 200, {"screen": settled_screen}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=True, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeOverlaySettlingSession(),
        args,
        FakeClientState(),
        timeout=0.2,
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "screen_interactions"
    assert payload["events"] == []
    assert payload["event_count"] == 0
    assert payload["pending_action"] is None
    assert payload["buttons"][0]["label"] == "Small Healing Potion"
    assert payload["interactions"][0]["display_label"] == "Small Healing Potion"


def test_perform_wait_json_keeps_fresh_events_before_pending(capsys):
    class FakeClientState:
        def set_cursor(self, key, cursor):
            pass

        def set_last_request_id(self, key, request_id):
            pass

        def save(self):
            pass

    pending = {
        "type": "choice_request",
        "id": "first",
        "choices": ["Inspect the signal"],
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "Inspect the signal",
            },
            {
                "source": "button",
                "type": "topic",
                "category": "topics",
                "display_label": "Ask about the signal",
            },
        ],
    }

    class FakeFreshEventSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 0
        last_request_id = None

        def __init__(self):
            self.poll_calls = 0

        def transcript(self, last=20):
            return [{"type": "narration", "text": "Old transcript line."}]

        def poll(self, timeout=0):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return [
                    {
                        "type": "narration",
                        "text": "Fresh line.",
                        "_seq": 1,
                        "timestamp": 123.0,
                    },
                    dict(pending, _seq=2, _set_at=123.0, timestamp=124.0),
                ]
            return []

        def pending(self):
            return pending

        def state(self):
            return {
                "status": "waiting_for_input",
                "context": {"context": "in_game"},
                "pending_request": pending,
            }

        def status(self):
            return {"status": "running"}

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": None}
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=True, quiet=False, verbose=False)

    assert cli._perform_wait(
        FakeFreshEventSession(),
        args,
        FakeClientState(),
        timeout=0.2,
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event_count"] == 2
    assert [event["type"] for event in payload["events"]] == [
        "narration",
        "choice_request",
    ]
    assert payload["events"][0]["text"] == "Fresh line."
    assert "_seq" not in payload["events"][0]
    assert "timestamp" not in payload["events"][0]
    assert payload["events"][1]["choices"] == ["Inspect the signal"]
    assert "_seq" not in payload["events"][1]
    assert "_set_at" not in payload["events"][1]
    assert "timestamp" not in payload["events"][1]
    assert "interactions" not in payload["events"][1]
    assert "Old transcript line." not in json.dumps(payload)
    assert payload["pending_action"] == {
        "type": "choice",
        "id": "first",
        "choices": [{"label": "Inspect the signal", "index": 1}],
        "actions": [
            {
                "label": "Ask about the signal",
                "type": "topic",
                "category": "topics",
                "index": 2,
            }
        ],
    }


def test_pending_json_final_drain_dedupes_events_by_seq(capsys):
    class FakeSession:
        def poll(self, timeout=0):
            assert timeout == 0
            return [
                {"type": "narration", "text": "Fresh line again.", "_seq": 1},
                {"type": "narration", "text": "Second fresh line.", "_seq": 2},
            ]

    args = Namespace(json=True, quiet=False, verbose=False)
    pending = {
        "type": "choice_request",
        "id": "first",
        "choices": ["Inspect the signal"],
    }

    cli._emit_pending_or_modal_json_phase(
        FakeSession(),
        args,
        pending,
        sc_latest={},
        modal_active=True,
        current_events=[
            {"type": "narration", "text": "Fresh line.", "_seq": 1},
        ],
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["event_count"] == 2
    assert [event["text"] for event in payload["events"]] == [
        "Fresh line.",
        "Second fresh line.",
    ]


def test_pending_json_modal_interactions_filter_focus_chrome(capsys):
    class FakeSession:
        def poll(self, timeout=0):
            return []

    args = Namespace(json=True, quiet=False, verbose=False)
    pending = {
        "type": "choice_request",
        "id": "first",
        "choices": ["Inspect the signal"],
    }

    cli._emit_pending_or_modal_json_phase(
        FakeSession(),
        args,
        pending,
        sc_latest={
            "screens": ["crt_overlay"],
            "buttons": [
                {
                    "label": "History",
                    "screen": "_focus_list",
                    "actions": ["ShowMenu"],
                },
                {
                    "label": "Calibrate",
                    "screen": "_focus_list",
                    "actions": ["ShowMenu"],
                    "_displayable": "raw",
                },
            ],
            "interactions": [
                {
                    "display_label": "Skip",
                    "screen": "_focus_list",
                    "action_names": ["Skip"],
                },
                {
                    "display_label": "Calibrate",
                    "screen": "_focus_list",
                    "action_names": ["ShowMenu"],
                    "_raw": "hidden",
                },
            ],
        },
        modal_active=True,
        current_events=[],
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "screen_interactions"
    assert [button["label"] for button in payload["buttons"]] == ["Calibrate"]
    assert "_displayable" not in payload["buttons"][0]
    assert [i["display_label"] for i in payload["interactions"]] == ["Calibrate"]
    assert "_raw" not in payload["interactions"][0]


def test_wait_json_events_sanitize_screen_content_payloads(capsys):
    class FakeSession:
        def poll(self, timeout=0):
            return []

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": None}
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=True, quiet=False, verbose=False)
    pending = {
        "type": "choice_request",
        "id": "first",
        "choices": ["Inspect the signal"],
    }

    cli._emit_pending_or_modal_json_phase(
        FakeSession(),
        args,
        pending,
        sc_latest={},
        modal_active=False,
        current_events=[
            {
                "type": "screen_content",
                "_seq": 7,
                "timestamp": 123.0,
                "texts": ["A terminal hums."],
                "screens": ["crt_overlay"],
                "buttons": [
                    {
                        "label": "History",
                        "screen": "_focus_list",
                        "actions": ["ShowMenu"],
                    },
                    {
                        "label": "Calibrate",
                        "screen": "_focus_list",
                        "actions": ["ShowMenu"],
                        "_displayable": "raw",
                    },
                ],
                "interactions": [
                    {
                        "display_label": "Skip",
                        "screen": "_focus_list",
                        "action_names": ["Skip"],
                    },
                    {
                        "display_label": "Calibrate",
                        "screen": "_focus_list",
                        "action_names": ["ShowMenu"],
                        "_raw": "hidden",
                    },
                ],
            }
        ],
    )

    payload = json.loads(capsys.readouterr().out)
    event = payload["events"][0]
    assert event["type"] == "screen_content"
    assert event["texts"] == ["A terminal hums."]
    assert event["screens"] == ["crt_overlay"]
    assert "_seq" not in event
    assert "timestamp" not in event
    assert [button["label"] for button in event["buttons"]] == ["Calibrate"]
    assert "_displayable" not in event["buttons"][0]
    assert [i["display_label"] for i in event["interactions"]] == ["Calibrate"]
    assert "_raw" not in event["interactions"][0]


def test_wait_json_diag_includes_raw_current_events(capsys):
    class FakeSession:
        def poll(self, timeout=0):
            return []

        def _get(self, path, timeout=2.0):
            if path == "/screen":
                return 200, {"screen": None}
            return 404, {}

        def get_transcript(self, last_n=30):
            return []

    args = Namespace(json=True, quiet=False, verbose=False, diag=True)
    pending = {
        "type": "choice_request",
        "id": "first",
        "choices": ["Inspect the signal"],
    }
    raw_event = {
        "type": "screen_content",
        "_seq": 7,
        "timestamp": 123.0,
        "texts": ["A terminal hums."],
        "buttons": [
            {
                "label": "History",
                "screen": "_focus_list",
                "actions": ["ShowMenu"],
            },
        ],
    }

    cli._emit_pending_or_modal_json_phase(
        FakeSession(),
        args,
        pending,
        sc_latest={},
        modal_active=False,
        current_events=[raw_event],
    )

    payload = json.loads(capsys.readouterr().out)
    assert "diag_events" in payload
    assert payload["diag_event_count"] == 1
    assert payload["diag_events"][0]["_seq"] == 7
    assert payload["diag_events"][0]["timestamp"] == 123.0
    assert payload["diag_events"][0]["buttons"][0]["label"] == "History"
    assert "_seq" not in payload["events"][0]
    assert payload["events"][0]["buttons"] == []


def test_history_json_sanitizes_transcript_events(monkeypatch, capsys):
    class FakeSession:
        def transcript(self, last=50):
            return [
                {
                    "type": "screen_content",
                    "_seq": 7,
                    "timestamp": 123.0,
                    "texts": ["A terminal hums."],
                    "screens": ["crt_overlay"],
                    "buttons": [
                        {
                            "label": "History",
                            "screen": "_focus_list",
                            "actions": ["ShowMenu"],
                        },
                        {
                            "label": "Calibrate",
                            "screen": "_focus_list",
                            "actions": ["ShowMenu"],
                            "_displayable": "raw",
                        },
                    ],
                    "interactions": [
                        {
                            "display_label": "Skip",
                            "screen": "_focus_list",
                            "action_names": ["Skip"],
                        },
                        {
                            "display_label": "Calibrate",
                            "screen": "_focus_list",
                            "action_names": ["ShowMenu"],
                            "_raw": "hidden",
                        },
                    ],
                }
            ]

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeSession())

    args = Namespace(
        json=True,
        all=False,
        first=None,
        last=5,
        verbose=False,
        quiet=False,
    )

    assert cli.cmd_history(args, FakeClientState()) == 0
    payload = json.loads(capsys.readouterr().out)
    event = payload["events"][0]
    assert event["type"] == "screen_content"
    assert event["texts"] == ["A terminal hums."]
    assert "_seq" not in event
    assert "timestamp" not in event
    assert [button["label"] for button in event["buttons"]] == ["Calibrate"]
    assert "_displayable" not in event["buttons"][0]
    assert [i["display_label"] for i in event["interactions"]] == ["Calibrate"]
    assert "_raw" not in event["interactions"][0]


def test_history_json_diag_includes_raw_transcript_events(monkeypatch, capsys):
    raw_event = {
        "type": "screen_content",
        "_seq": 7,
        "timestamp": 123.0,
        "texts": ["A terminal hums."],
        "buttons": [
            {
                "label": "History",
                "screen": "_focus_list",
                "actions": ["ShowMenu"],
            },
        ],
    }

    class FakeSession:
        def transcript(self, last=50):
            return [raw_event]

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeSession())

    args = Namespace(
        json=True,
        all=False,
        first=None,
        last=5,
        verbose=False,
        quiet=False,
        diag=True,
    )

    assert cli.cmd_history(args, FakeClientState()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["diag_event_count"] == 1
    assert payload["diag_events"][0]["_seq"] == 7
    assert payload["diag_events"][0]["timestamp"] == 123.0
    assert payload["diag_events"][0]["buttons"][0]["label"] == "History"
    assert "_seq" not in payload["events"][0]
    assert payload["events"][0]["buttons"] == []


def test_freshen_choice_request_accepts_empty_live_interactions():
    event = {
        "type": "choice_request",
        "choices": ["Stale choice."],
        "interactions": [
            {
                "source": "button",
                "type": "other",
                "display_label": "[spell]",
                "promoted": True,
            },
        ],
    }
    live = {
        "type": "screen_content",
        "interactions": [],
    }

    updated = cli._freshen_choice_request_events(
        [event],
        FakeScreenSession(live),
    )

    assert updated == [{
        "type": "choice_request",
        "choices": [],
        "interactions": [],
        "full_items": [],
    }]


def test_cmd_load_without_a_slot_says_it_loaded_the_newest_save(monkeypatch, capsys):
    """`load` with no slot used to print "Load command for slot 'None'
    confirmed." (seen on the Linux from-zero run)."""

    class FakeLoadSession:
        bridge_url = "http://bridge"
        slot_prefix = ""
        cursor = 5
        last_request_id = None
        last_request_type = None
        last_choices = None
        last_actionable_snapshot = None
        _prefetched_events = []
        load_nonce = None

        def state(self):
            return {"event_counter": 5, "pending_request": None}

        def _send_command(self, name, args=None, nonce=None):
            assert args == {}, "no slot means the shim loads its newest save"
            self.load_nonce = nonce
            return True, "accepted"

        def poll(self, timeout=0):
            return [{"type": "command_result", "command": "load",
                     "success": True, "nonce": self.load_nonce}]

    monkeypatch.setattr(cli, "_make_session", lambda args, state: FakeLoadSession())
    monkeypatch.setattr(cli, "_save_session", lambda s, a, st: None)
    args = Namespace(bridge="http://bridge", json=False, quiet=False,
                    slot=None, wait=False)

    assert cli.cmd_load(args, object()) == 0

    out = capsys.readouterr().out
    assert "newest save" in out
    assert "None" not in out
