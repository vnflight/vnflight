"""First attachment and delayed screen catch-up have different ownership."""
from argparse import Namespace
import json
import sys

import pytest

from vnflight import cli
from vnflight.lib import ClientState


@pytest.mark.parametrize("json_mode", [False, True])
def test_repeated_wait_after_modal_close_uses_current_pending(tmp_path, capsys, json_mode):
    pending = {"type": "choice_request", "id": "lab", "choices": ["Search the partition", "Leave lab"]}
    old = {"type": "screen_content", "_seq": 10, "screens": ["audit"],
           "modal_screens": ["audit"], "buttons": [{"label": "Old audit choice"}]}
    # Matches the CLI trace: the newer screen declares its visible screens,
    # but neither buttons nor interactions (the pending request owns choices).
    current = {"type": "screen_content", "_seq": 20,
               "screens": ["nvl", "hud"], "texts": [], "choices": pending["choices"]}

    class Session:
        bridge_url = "http://test"
        slot_prefix = "/1"
        cursor = 20
        last_request_id = None

        def _get(self, path, **kwargs):
            return (200, {"screen": current}) if path == "/screen" else (404, {})

        def transcript(self, **kwargs):
            return [old, current]

        def get_transcript(self, **kwargs):
            return [old, current]

        def poll(self, **kwargs):
            return []

        def pending(self):
            return pending

        def state(self):
            return {"status": "waiting_for_input", "pending_request": pending,
                    "context": {"context": "in_game"}, "transcript": [old, current]}

    args = Namespace(json=json_mode, quiet=False, verbose=False)
    for _ in range(2):
        assert cli._perform_wait(Session(), args, ClientState(str(tmp_path)), timeout=2) == 0
        output = capsys.readouterr().out
        assert "Old audit choice" not in output
        assert "Search the partition" in output
        if json_mode:
            assert json.loads(output)["pending_action"] is not None


def test_final_drain_screen_text_does_not_own_later_dialogue(capsys):
    class Session:
        def poll(self, **kwargs):
            return [{"type": "screen_content", "texts": ["Stay here."]},
                    {"type": "dialogue", "character": "Marcus", "text": "Stay here."}]

    seen_narration = set()
    cli._final_drain_before_pending_phase(
        Session(), Namespace(json=False, verbose=False),
        {"type": "choice_request", "id": "next", "choices": ["Continue"]},
        initial_grace=0, post_action_story_seen=False, fresh_story_seen=False,
        transition_since_story=False, story_wait_until=0, initial_screen_texts=set(),
        narr_texts_cumulative=seen_narration, seen_sc_keys=set(), printed_ids=set(),
        colour=False, quiet=False, skip_stale_game_ended_event=lambda *args: False,
    )
    output = capsys.readouterr().out
    assert output.count("Stay here.") == 2
    assert "[Marcus] Stay here." in output
    assert "Stay here." in seen_narration


def test_catchup_preserves_dialogue_after_same_text_screen():
    class Session:
        def poll(self, **kwargs):
            return [{"type": "screen_content", "texts": ["Stay here."]},
                    {"type": "dialogue", "character": "Marcus", "text": "Stay here."}]

    pending = {"type": "choice_request", "id": "next", "choices": ["Continue"]}
    result = cli._choice_request_catchup_phase(
        Session(), Namespace(json=False, verbose=False), [pending],
        initial_grace=0, post_action_story_seen=False, fresh_story_seen=False,
        initial_screen_texts=set(), narr_texts_cumulative=set(), seen_sc_keys=set(),
    )
    assert [event["type"] for event in result.display_events] == [
        "screen_content", "dialogue", "choice_request"]
    assert result.display_events[1]["character"] == "Marcus"


@pytest.mark.parametrize("phase", ["latest", "drain", "catchup"])
def test_reopened_terminal_empty_delta_is_respected_by_every_wait_phase(
    monkeypatch, capsys, phase,
):
    # Same shape as slot 3 event 3572: retained generation, 33 old rows,
    # and an authoritative empty delta despite a nonempty screen snapshot.
    rows = ["[ECHO-7>] Old terminal row " + str(i) for i in range(33)]
    screen = {"type": "screen_content", "_seq": 3572, "texts": rows,
              "overlay_texts": rows, "passive_overlay_delta": [],
              "overlay_generations": {"echo_terminal_live": "11"},
              "screens": ["echo_terminal_live"]}
    pending = {"type": "choice_request", "_seq": 3571, "id": "identity",
               "choices": ["It gives me context, not instructions."]}
    args = Namespace(json=False, verbose=False)
    common = dict(initial_grace=0, post_action_story_seen=False,
                  fresh_story_seen=False, initial_screen_texts=set(),
                  narr_texts_cumulative=set(), seen_sc_keys=set())

    class Session:
        def poll(self, **kwargs):
            return [screen]

        def state(self):
            return {"transcript": [screen]}

    if phase == "latest":
        cli._print_latest_screen_text_before_pending_phase(
            args, pending, screen, modal_active=False,
            colour=False, quiet=False, **common)
    elif phase == "drain":
        cli._final_drain_before_pending_phase(
            Session(), args, pending, transition_since_story=False,
            story_wait_until=0, printed_ids=set(), colour=False, quiet=False,
            skip_stale_game_ended_event=lambda *args: False, **common)
    else:
        monkeypatch.setattr(cli, "_get_settled_screen_buttons", lambda *a, **kw: screen)
        result = cli._choice_request_catchup_phase(Session(), args, [pending], **common)
        assert not any(e.get("texts") for e in result.display_events)
    assert "Old terminal row" not in capsys.readouterr().out


@pytest.mark.parametrize("phase", ["latest", "drain", "catchup"])
@pytest.mark.parametrize("empty_texts", [False, True])
def test_wait_fallbacks_deliver_fresh_passive_rows_only_from_events(
    monkeypatch, capsys, phase, empty_texts,
):
    texts = [] if empty_texts else ["Repeat", "Repeat", "Ordinary screen note"]
    screen = {"type": "screen_content", "_seq": 52,
              "texts": texts,
              "overlay_texts": ["Repeat", "Repeat"],
              "passive_overlay_delta": ["Repeat"]}
    pending = {"type": "choice_request", "_seq": 51, "id": "next",
               "choices": ["Continue"]}
    args = Namespace(json=False, verbose=False)
    common = dict(initial_grace=0, post_action_story_seen=False,
                  fresh_story_seen=False, initial_screen_texts=set(),
                  narr_texts_cumulative={"Repeat"}, seen_sc_keys=set())

    class Session:
        def poll(self, **kwargs):
            return [screen]

        def state(self):
            return {"transcript": [screen]}

    if phase == "latest":
        cli._print_latest_screen_text_before_pending_phase(
            args, pending, screen, modal_active=False,
            colour=False, quiet=False, **common)
        out = capsys.readouterr().out
        assert "Repeat" not in out
    elif phase == "drain":
        cli._final_drain_before_pending_phase(
            Session(), args, pending, transition_since_story=False,
            story_wait_until=0, printed_ids=set(), colour=False, quiet=False,
            skip_stale_game_ended_event=lambda *args: False, **common)
        out = capsys.readouterr().out
        assert out.count("Repeat") == 1
    else:
        monkeypatch.setattr(cli, "_get_settled_screen_buttons", lambda *a, **kw: screen)
        result = cli._choice_request_catchup_phase(Session(), args, [pending], **common)
        out = cli.format_events(result.display_events, colour=False)
        assert out.count("Repeat") == 1
        assert out.index("Repeat") < out.index("Continue")
    assert ("Ordinary screen note" in out) is not empty_texts
    assert screen["texts"] == texts


@pytest.mark.parametrize("with_prompt", [False, True])
def test_empty_screen_text_does_not_hide_fresh_passive_delta(with_prompt):
    event = {"type": "screen_content", "texts": [],
             "overlay_texts": ["Old row", "Receiving specifications"],
             "passive_overlay_delta": ["Receiving specifications"]}
    if with_prompt:
        event["buttons"] = [{"label": "Continue"}]
    result = cli._filter_screen_content_event(
        Namespace(), event, stale_screen_label=None, initial_grace=5,
        post_action_story_seen=False, fresh_story_seen=False,
        story_wait_until=float("inf"), initial_screen_texts=set(),
        narr_texts_cumulative={"Receiving specifications"}, seen_sc_keys=set(),
    )
    assert result.event["texts"] == ["Receiving specifications"]
    assert result.deferred_screen_prompt is None
    assert result.fresh_story_seen
    assert event["texts"] == []


@pytest.mark.parametrize("hint,ambiguous", [("missing", None), (None, [{"slot_id": 1}, {"slot_id": 2}])])
def test_unresolved_slot_stops_before_restoring_session(monkeypatch, tmp_path, hint, ambiguous):
    from vnflight.client import BridgeClient
    monkeypatch.setattr(BridgeClient, "auto_select_slot", lambda *args: False)
    state = ClientState(str(tmp_path))
    monkeypatch.setattr(state, "get_cursor", lambda *args: pytest.fail("Unscoped session must not be used"))
    original = cli.BridgeClient

    def make_client(*args, **kwargs):
        client = original(*args, **kwargs)
        client._ambiguous_slots = ambiguous
        return client

    monkeypatch.setattr(cli, "BridgeClient", make_client)
    with pytest.raises(ValueError, match="--slot"):
        cli._make_session(Namespace(bridge="http://test", target_slot=hint, token=None), state)


@pytest.mark.parametrize("event_type", ["narration", "auto_skipped", "observation_started"])
@pytest.mark.parametrize("bounded", [False, True])
def test_wait_activity_respects_explicit_timeout(monkeypatch, tmp_path, capsys, event_type, bounded):
    now = [100.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    class Session:
        bridge_url = "http://test"
        slot_prefix = "/1"
        cursor = 0
        last_request_id = None

        def transcript(self, **kwargs):
            return []

        def get_transcript(self, **kwargs):
            return []

        def _get(self, *args, **kwargs):
            return 404, {}

        def poll(self, timeout=0):
            assert self.cursor < 4, "Activity kept extending the explicit wait"
            now[0] += min(1.0, timeout)
            self.cursor += 1
            if self.cursor == 4:
                return [{"type": "game_ended", "reason": "completed"}]
            return [{"type": event_type, "text": "Fresh row " + str(self.cursor), "delay": 60}]

        def pending(self):
            return None

        def status(self):
            return {"status": "ended" if self.cursor == 4 else "running"}

        def state(self):
            return {"status": self.status()["status"], "context": {"context": "in_game"}}

    session = Session()
    state = ClientState(str(tmp_path))
    args = Namespace(json=False, quiet=False, verbose=False)
    assert cli._perform_wait(session, args, state, timeout=2 if bounded else None) == 0
    assert now[0] == (102.0 if bounded else 104.0)
    assert session.cursor == (2 if bounded else 4)
    assert state.get_cursor("http://test/1") == session.cursor
    if event_type == "narration":
        output = capsys.readouterr().out
        assert output.count("Fresh row 1") == 1
        assert output.count("Fresh row 2") == 1


@pytest.mark.parametrize("command", [["state"], ["act", "Start"]])
@pytest.mark.parametrize("json_mode", [False, True])
def test_missing_explicit_slot_is_a_cli_error(monkeypatch, tmp_path, capsys, command, json_mode):
    from vnflight.client import BridgeClient
    monkeypatch.setattr(BridgeClient, "auto_select_slot", lambda *args: False)
    monkeypatch.setattr(BridgeClient, "_get", lambda *args, **kwargs: pytest.fail("Unscoped read"))
    monkeypatch.setattr(BridgeClient, "_post", lambda *args, **kwargs: pytest.fail("Unscoped action"))
    monkeypatch.setattr(cli, "default_state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["vnflight", "--slot", "missing"]
                        + (["--json"] if json_mode else []) + command)
    assert cli.main() == 1
    output = capsys.readouterr().out
    if json_mode:
        assert "--slot 'missing'" in json.loads(output)["error"]
    else:
        assert "--slot 'missing'" in output


@pytest.mark.parametrize("delta", [[], ["old"], ["old", "old"]])
def test_passive_delta_preserves_occurrences_and_ordinary_screen_text(delta):
    event = {"type": "screen_content", "texts": ["old", "ordinary"],
             "overlay_texts": ["old"], "passive_overlay_delta": delta,
             "buttons": [{"label": "Continue"}]}
    result = cli._filter_screen_content_event(
        Namespace(), event, stale_screen_label=None, initial_grace=0,
        post_action_story_seen=False, fresh_story_seen=False, story_wait_until=0,
        initial_screen_texts=set(), narr_texts_cumulative={"old"}, seen_sc_keys=set(),
    )
    assert result.event["texts"] == delta + ["ordinary"]
    assert result.event["buttons"] == event["buttons"]
    assert event["texts"] == ["old", "ordinary"]


def test_passive_empty_delta_keeps_actionable_prompt():
    result = cli._filter_screen_content_event(
        Namespace(), {"type": "screen_content", "texts": ["old"],
                      "overlay_texts": ["old"], "passive_overlay_delta": [],
                      "buttons": [{"label": "Continue"}]},
        stale_screen_label=None, initial_grace=0, post_action_story_seen=False,
        fresh_story_seen=False, story_wait_until=0, initial_screen_texts=set(),
        narr_texts_cumulative=set(), seen_sc_keys=set(),
    )
    assert result.event["texts"] == []
    assert result.event["buttons"] == [{"label": "Continue"}]


@pytest.mark.parametrize("cursor", [0, 40])
def test_saved_cursor_is_not_fast_forwarded(monkeypatch, tmp_path, cursor):
    from vnflight.client import BridgeClient
    monkeypatch.setattr(BridgeClient, "auto_select_slot", lambda *args: True)
    args = Namespace(bridge="http://test", target_slot="1", token=None)
    state = ClientState(str(tmp_path))
    state.set_cursor(args.bridge, cursor)
    state.save()
    restored = cli._make_session(args, ClientState(str(tmp_path)))
    monkeypatch.setattr(restored, "attach_to_running_slot",
                        lambda **kwargs: pytest.fail("Continuation cannot skip unread rows"))
    cli._mark_current_events_seen_for_explicit_wait(restored, args)
    assert restored.cursor == cursor


@pytest.mark.parametrize("snapshot_seq,expected", [(20, False), (None, False), (51, True)])
def test_choice_catchup_does_not_replay_old_hud(monkeypatch, snapshot_seq, expected):
    screen = {"type": "screen_content", "texts": ["STORM PEAK IN 33m"]}
    if snapshot_seq is not None:
        screen["_seq"] = snapshot_seq

    class Session:
        def poll(self, **kwargs):
            return []

        def state(self):
            return {"transcript": [screen]}

    monkeypatch.setattr(cli, "_get_settled_screen_buttons", lambda *args, **kwargs: screen)
    request = {"type": "choice_request", "_seq": 50, "id": "new", "choices": ["Talk"]}
    result = cli._choice_request_catchup_phase(
        Session(), Namespace(trace_wait=False), [request], initial_grace=0,
        post_action_story_seen=False, fresh_story_seen=False,
        initial_screen_texts=set(), narr_texts_cumulative=set(), seen_sc_keys=set())
    texts = [text for event in result.display_events for text in event.get("texts", [])]
    assert ("STORM PEAK IN 33m" in texts) is expected
    assert result.display_events[-1] == request


def test_main_menu_state_does_not_rescue_ending_or_query_gameplay_stats(monkeypatch, capsys):
    class Session:
        def state(self):
            return {
                "status": "ended", "context": {"context": "main_menu"},
                "transcript": [
                    {"type": "screen_content", "texts": ["Old room"]},
                    {"type": "narration", "text": "Old ending passage"}],
            }

        def _get(self, path, **kwargs):
            assert path == "/screen"
            return 200, {"screen": {"main_menu": True, "texts": ["Title"], "buttons": []}}

        def poll(self, **kwargs):
            pytest.fail("Main menu must not poll for gameplay stats")

    monkeypatch.setattr(cli, "_make_session", lambda *args: Session())
    monkeypatch.setattr(cli, "_save_session", lambda *args: None)
    assert cli.cmd_state(Namespace(json=False, verbose=False), object()) == 0
    output = capsys.readouterr().out
    assert "Old ending passage" not in output
    assert "Recent:" not in output


def test_cli_guidance_does_not_rewrite_story_or_mutate_result():
    original = {
        "warning": "Story is still arriving; call wait() to continue.",
        "pending": 'Choices\nUse act <N> or act "<label>" to respond.',
        "text": "She wrote wait() in her notebook.",
    }
    result = cli._cli_result_guidance(original)
    assert result["warning"] == "Story is still arriving; call the CLI `wait` command to continue."
    # One hint form everywhere now; the CLI passes it through untouched.
    assert result["pending"] == original["pending"]
    assert result["text"] == original["text"]
    assert "wait()" in original["warning"]
