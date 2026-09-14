"""Selection-only utility changes are visible to screen settlement."""

from types import SimpleNamespace

import pytest

from test_handlers import MockClient
from vnflight import handlers, settle
from vnflight.format import build_state_data, format_state_text


def rendered_toggle(selected):
    interaction = {
        "id": "preferences:Mute All", "display_label": "Mute All",
        "screen": "preferences", "type": "nav", "source": "button",
        "action_names": ["ToggleMute"], "is_selected": selected,
    }
    data = build_state_data({
        "status": "running", "context": {"context": "game_menu"},
        "game_state": {
            "interactions": [interaction],
            "screen_buttons": [{"label": "Mute All", "screen": "preferences",
                                "actions": ["ToggleMute"], "index": 1}],
        },
        "screen": {"screens": ["preferences"], "interactions": [interaction]},
    })
    result = format_state_text(data)
    result["_data"] = data
    return result


def test_formatter_preserves_selection_for_settle_signature():
    before, after = rendered_toggle(True), rendered_toggle(False)
    assert before["buttons"] == after["buttons"]
    assert settle.rendered_state_signature(before) != settle.rendered_state_signature(after)
    assert settle.screen_signature({
        "interactions": before["_data"]["_interactions"],
    }) != settle.screen_signature({
        "interactions": after["_data"]["_interactions"],
    })


@pytest.mark.parametrize("action_type, actions", [
    ("choice", []), ("other", ["ChoiceReturn"]), ("nav", ["Return"]),
])
def test_story_answer_highlight_is_not_a_screen_change(action_type, actions):
    def screen(selected):
        return {"interactions": [{
            "display_label": "Answer", "type": action_type,
            "action_names": actions, "is_selected": selected,
        }]}
    assert settle.screen_signature(screen(True)) == settle.screen_signature(screen(False))
    assert settle.rendered_state_signature({
        "_data": {"_interactions": screen(True)["interactions"]},
    }) == settle.rendered_state_signature({
        "_data": {"_interactions": screen(False)["interactions"]},
    })


def install_clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    clock.time = lambda: clock.now
    clock.sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
    monkeypatch.setattr(handlers, "time", clock)
    monkeypatch.setattr(settle, "time", clock)
    return clock


def test_screen_action_settles_on_mute_change_without_using_deadline(monkeypatch):
    before, after = rendered_toggle(True), rendered_toggle(False)
    clock = install_clock(monkeypatch)
    calls = []
    monkeypatch.setattr(handlers, "handle_state", lambda *args: calls.append(clock.now) or after)
    result = {"ok": True}
    handlers._settle_state_after_screen_action(
        handlers.HandlerContext(client=MockClient()), result,
        {"_result_deadline": 115.0},
        pre_state_sig=settle.rendered_state_signature(before),
        pre_was_button_only=True,
    )
    assert clock.now < 102.0
    assert 2 <= len(calls) <= 5
    assert result["_data"]["_interactions"][0]["is_selected"] is False


@pytest.mark.parametrize("expire_during_fetch", [False, True])
def test_expiry_preserves_last_valid_preferences_snapshot(monkeypatch, expire_during_fetch):
    before = rendered_toggle(True)
    clock = install_clock(monkeypatch)
    calls = []

    def fetch(*args):
        calls.append(clock.now)
        if expire_during_fetch and len(calls) == 2:
            clock.now = 101.0
            return {"status": "unknown", "_data": {"status": "unknown"}}
        return before

    monkeypatch.setattr(handlers, "handle_state", fetch)
    result = handlers._wait_for_rendered_state_change(
        handlers.HandlerContext(client=MockClient()),
        settle.rendered_state_signature(before),
        {"_result_deadline": 101.0}, timeout=1.0,
    )
    assert result is before
    assert all(at < 101.0 for at in calls)
