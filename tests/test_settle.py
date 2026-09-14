from vnflight.settle import (
    button_labels,
    is_single_enter_screen,
    poll_preserving_events,
    rendered_state_signature,
    screen_signature,
    wait_for_stable_change,
)


def test_button_labels_and_single_enter_screen():
    screen = {
        "buttons": [
            {"label": ""},
            {"label": "  Enter  "},
        ],
    }

    assert button_labels(screen) == ["Enter"]
    assert is_single_enter_screen(screen) is True


def test_screen_signature_tracks_buttons_and_interactions():
    first = {
        "screens": ["inventory"],
        "buttons": [{"label": "Return", "screen": "inventory"}],
    }
    second = {
        "screens": ["inventory"],
        "buttons": [{"label": "Potion", "screen": "inventory"}],
        "interactions": [{"display_label": "Potion", "type": "item"}],
    }

    assert screen_signature(first) != screen_signature(second)


def test_screen_signature_tracks_text_changes():
    first = {"screens": ["nvl"], "texts": ["Old text"]}
    second = {"screens": ["nvl"], "texts": ["Fresh text"]}

    assert screen_signature(first) != screen_signature(second)


def test_rendered_state_signature_tracks_visible_decisions():
    first = {"_data": {"buttons": [{"label": "Return"}]}, "buttons": "1. Return"}
    second = {"_data": {"buttons": [{"label": "Potion"}]}, "buttons": "1. Potion"}

    assert rendered_state_signature(first) != rendered_state_signature(second)


def test_poll_preserving_events_restores_events_for_next_wait():
    class Client:
        def __init__(self):
            self._prefetched_events = [{"type": "narration", "text": "old"}]
            self.calls = []

        def poll(self, timeout=0, include_prefetched=True):
            self.calls.append((timeout, include_prefetched))
            return [{"type": "narration", "text": "new"}]

    client = Client()

    events = poll_preserving_events(client, timeout=0.2)

    assert events == [{"type": "narration", "text": "new"}]
    assert client._prefetched_events == [
        {"type": "narration", "text": "new"},
        {"type": "narration", "text": "old"},
    ]
    assert client.calls == [(0.2, True)]


def test_wait_for_stable_change_returns_changed_value_after_repeat():
    values = iter(["old", "new", "new"])

    result = wait_for_stable_change(
        fetch=lambda: next(values, "new"),
        signature=lambda value: (value,),
        initial="old",
        timeout=0.2,
        settle_delay=0.0,
        poll_interval=0.0,
    )

    assert result == "new"


def test_wait_for_stable_change_can_return_stable_initial_value():
    values = iter(["old", "old"])

    result = wait_for_stable_change(
        fetch=lambda: next(values, "old"),
        signature=lambda value: (value,),
        initial="old",
        timeout=0.2,
        settle_delay=0.0,
        poll_interval=0.0,
        return_initial_when_stable=True,
    )

    assert result == "old"
