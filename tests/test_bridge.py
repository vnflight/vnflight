"""Tests for vnflight.bridge — GameState and SlotManager."""

import sys
import os
import json
import threading
import time
from collections import OrderedDict

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from vnflight.bridge import GameState, SlotManager, TransactionJournalError


@pytest.fixture(autouse=True)
def isolated_bridge_storage(tmp_path, monkeypatch):
    """Default managers must never read or rewrite real playthrough journals."""
    monkeypatch.chdir(tmp_path)


class TestGameState:
    """Tests for GameState — the core game state container."""

    def test_runtime_playback_snapshot_corrects_bridge_metadata(self):
        gs = GameState()
        gs.update_config({"auto_advance": True, "auto_advance_delay": 0.05})
        gs.push_event({"type": "game_state", "playback_config": {
            "auto_advance": False, "auto_advance_delay": 0.3}})
        assert gs.auto_advance is False
        assert gs.auto_advance_delay == 0.3
        gs.push_event({"type": "game_state", "playback_config": {
            "auto_advance": True, "auto_advance_delay": 0.05}})
        assert gs.auto_advance is True
        assert gs.auto_advance_delay == 0.05

    def test_creation(self):
        gs = GameState()
        assert gs.status == "idle"
        assert gs.event_counter == 0
        assert gs.pending_request is None
        assert gs.anomaly_flag is None

    def test_push_event_increments_counter(self):
        gs = GameState()
        gs.push_event({"type": "dialogue", "text": "Hello"})
        assert gs.event_counter == 1
        gs.push_event({"type": "narration", "text": "..."})
        assert gs.event_counter == 2

    def test_push_event_adds_seq(self):
        gs = GameState()
        gs.push_event({"type": "test"})
        assert gs.transcript[-1]["_seq"] == 1

    def test_push_event_preserves_source_order_metadata(self):
        gs = GameState()
        gs.push_event({
            "type": "narration",
            "text": "Before the choice.",
            "_source_id": "process-a",
            "_source_seq": 7,
            "_source_ts": 123.5,
        })

        assert gs.transcript[-1]["_source_id"] == "process-a"
        assert gs.transcript[-1]["_source_seq"] == 7
        assert gs.transcript[-1]["_source_ts"] == 123.5

    def test_request_identity_ignores_transport_order_metadata(self):
        first = {
            "type": "choice_request",
            "id": "request-1",
            "choices": ["Continue"],
            "_source_id": "process-a",
            "_source_seq": 7,
            "_source_ts": 123.5,
        }
        replay = dict(
            first,
            _source_id="process-b",
            _source_seq=9,
            _source_ts=125.0,
        )

        assert GameState._action_request_signature(first) == (
            GameState._action_request_signature(replay)
        )

    def test_transcript_stores_events(self):
        gs = GameState()
        gs.push_event({"type": "dialogue", "character": "Alice", "text": "Hi"})
        assert len(gs.transcript) == 1
        assert gs.transcript[0]["type"] == "dialogue"

    def test_screenshot_not_in_transcript(self):
        gs = GameState()
        gs.push_event({"type": "screenshot", "image": "base64data"})
        assert len(gs.transcript) == 0
        assert gs.latest_screenshot == "base64data"

    def test_screenshot_snapshot_keeps_capture_identity_with_image(self):
        gs = GameState()
        gs.push_event({"type": "screenshot", "image": "fresh", "capture_id": "capture"})
        assert gs.get_screenshot_snapshot() == {"screenshot": "fresh", "capture_id": "capture"}
        gs.push_event({"type": "screenshot", "image": "legacy"})
        assert gs.get_screenshot_snapshot() == {"screenshot": "legacy", "capture_id": None}

    def test_screen_content_not_in_transcript(self):
        gs = GameState()
        gs.push_event({"type": "screen_content", "texts": ["Hello"], "buttons": []})
        assert len(gs.transcript) == 0
        assert gs.current_screen is not None

    def test_changed_passive_overlay_snapshots_are_drainable(self):
        gs = GameState()
        first = {
            "type": "screen_content",
            "overlay_screens": ["echo_terminal_live"],
            "overlay_texts": ["ECHO-7>", "FIRST"],
        }
        second = dict(first, overlay_texts=["ECHO-7>", "FIRST", "SECOND"])

        gs.push_event(dict(first))
        gs.push_event(dict(first))
        gs.push_event(second)

        drained = gs.get_state(since=0)["transcript"]
        assert [event["overlay_texts"] for event in drained] == [
            ["ECHO-7>", "FIRST"],
            ["ECHO-7>", "FIRST", "SECOND"],
        ]
        assert all(event["passive_overlay_snapshot"] for event in drained)
        assert [event["passive_overlay_delta"] for event in drained] == [
            ["ECHO-7>", "FIRST"],
            ["SECOND"],
        ]
        assert gs.current_screen["passive_overlay_delta"] == ["SECOND"]
        assert gs.current_screen["passive_overlay_row_seqs"] == [1, 1, 3]

    def test_unchanged_passive_latest_state_declares_empty_delta(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_texts": ["READY"],
            "overlay_texts_by_screen": {"terminal": ["READY"]},
            "overlay_generations": {"terminal": "1"},
        }

        gs.push_event(dict(shown))
        gs.push_event(dict(shown))

        assert gs.current_screen["passive_overlay_delta"] == []
        assert gs.current_screen["passive_overlay_row_seqs_by_screen"] == {
            "terminal": [1]
        }
        assert len(gs.transcript) == 1

    def test_game_resume_baselines_restored_passive_overlay_once(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_retained_screens": ["terminal"],
            "overlay_texts": ["OLD", "SCROLLBACK"],
            "overlay_texts_by_screen": {
                "terminal": ["OLD", "SCROLLBACK"],
            },
            "overlay_generations": {"terminal": "7"},
        }
        gs.push_event(dict(shown))
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event(dict(shown))

        restored = gs.transcript[-1]
        assert restored["passive_overlay_snapshot"] is True
        assert restored["passive_overlay_resumed_baseline"] is True
        assert restored["passive_overlay_delta"] == []

        grown = dict(
            shown,
            overlay_texts=["OLD", "SCROLLBACK", "NEW"],
            overlay_texts_by_screen={
                "terminal": ["OLD", "SCROLLBACK", "NEW"],
            },
        )
        gs.push_event(grown)

        assert gs.transcript[-1]["passive_overlay_delta"] == ["NEW"]
        assert "passive_overlay_resumed_baseline" not in gs.transcript[-1]

    def test_resume_baseline_preserves_new_first_snapshot_suffix(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_retained_screens": ["terminal"],
            "overlay_texts": ["OLD"],
            "overlay_texts_by_screen": {"terminal": ["OLD"]},
            "overlay_generations": {"terminal": "7"},
        }
        gs.push_event(dict(shown))
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({
            **shown,
            "overlay_texts": ["OLD", "NEW AFTER RESUME"],
            "overlay_texts_by_screen": {
                "terminal": ["OLD", "NEW AFTER RESUME"],
            },
        })

        assert gs.transcript[-1]["passive_overlay_delta"] == [
            "NEW AFTER RESUME"
        ]
        assert gs.transcript[-1]["passive_overlay_resumed_baseline"] is True

    def test_resume_baselines_divergent_older_overlay_generation(self):
        gs = GameState()
        future = {
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_retained_screens": ["terminal"],
            "overlay_texts": ["FUTURE"],
            "overlay_texts_by_screen": {"terminal": ["FUTURE"]},
            "overlay_generations": {"terminal": "8"},
        }
        past = {
            **future,
            "overlay_texts": ["PAST RESTORED"],
            "overlay_texts_by_screen": {"terminal": ["PAST RESTORED"]},
            "overlay_generations": {"terminal": "7"},
        }
        gs.push_event(future)
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event(past)

        assert gs.transcript[-1]["passive_overlay_delta"] == []

        gs.push_event({
            **past,
            "overlay_texts": ["PAST RESTORED", "NEW"],
            "overlay_texts_by_screen": {
                "terminal": ["PAST RESTORED", "NEW"],
            },
        })
        assert gs.transcript[-1]["passive_overlay_delta"] == ["NEW"]

    def test_resume_changed_generation_baselines_shared_prefix_history(self):
        gs = GameState()
        future = {
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_retained_screens": ["terminal"],
            "overlay_texts": ["STABLE"],
            "overlay_texts_by_screen": {"terminal": ["STABLE"]},
            "overlay_generations": {"terminal": "8"},
        }
        restored = {
            **future,
            "overlay_texts": ["STABLE", "OLD RESTORED ROW"],
            "overlay_texts_by_screen": {
                "terminal": ["STABLE", "OLD RESTORED ROW"],
            },
            "overlay_generations": {"terminal": "7"},
        }
        gs.push_event(future)
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event(restored)

        assert gs.transcript[-1]["passive_overlay_delta"] == []

        gs.push_event({
            **restored,
            "overlay_texts": ["STABLE", "OLD RESTORED ROW", "NEW"],
            "overlay_texts_by_screen": {
                "terminal": ["STABLE", "OLD RESTORED ROW", "NEW"],
            },
        })
        assert gs.transcript[-1]["passive_overlay_delta"] == ["NEW"]

    def test_game_resume_baselines_restored_overlay_without_prior_panel(self):
        gs = GameState()
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_texts": ["NEW SESSION"],
            "overlay_texts_by_screen": {"terminal": ["NEW SESSION"]},
            "overlay_generations": {"terminal": "9"},
        })

        assert gs.transcript[-1]["passive_overlay_delta"] == []
        assert gs.transcript[-1]["passive_overlay_resumed_baseline"] is True

    def test_resume_empty_frame_retires_prior_overlay_baseline(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_texts": ["OLD SESSION"],
            "overlay_texts_by_screen": {"terminal": ["OLD SESSION"]},
        }
        gs.push_event(dict(shown))
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({"type": "screen_content", "texts": ["Room"]})
        gs.push_event({
            **shown,
            "overlay_texts": ["NEW SESSION"],
            "overlay_texts_by_screen": {"terminal": ["NEW SESSION"]},
        })

        assert gs.transcript[-1]["passive_overlay_delta"] == ["NEW SESSION"]
        assert "passive_overlay_resumed_baseline" not in gs.transcript[-1]

    def test_resume_blocking_frame_retires_prior_overlay_baseline(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_texts": ["OLD SESSION"],
            "overlay_texts_by_screen": {"terminal": ["OLD SESSION"]},
        }
        gs.push_event(dict(shown))
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({
            "type": "screen_content", "overlay_active": True,
            "modal_screens": ["choice"],
        })
        gs.push_event({
            **shown,
            "overlay_texts": ["NEW SESSION"],
            "overlay_texts_by_screen": {"terminal": ["NEW SESSION"]},
        })

        assert gs.transcript[-1]["passive_overlay_delta"] == ["NEW SESSION"]
        assert "passive_overlay_resumed_baseline" not in gs.transcript[-1]

    def test_resume_retained_empty_frame_preserves_generation_ownership(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_retained_screens": ["terminal"],
            "overlay_texts": ["OLD"],
            "overlay_texts_by_screen": {"terminal": ["OLD"]},
            "overlay_generations": {"terminal": "7"},
        }
        gs.push_event(dict(shown))
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({
            **shown,
            "overlay_texts": [],
            "overlay_texts_by_screen": {"terminal": []},
        })
        gs.push_event(dict(shown))

        assert gs.transcript[-1]["passive_overlay_delta"] == []

    def test_resume_preserves_hidden_retained_contributor_ownership(self):
        gs = GameState()
        both = {
            "type": "screen_content",
            "overlay_screens": ["a", "b"],
            "overlay_retained_screens": ["a", "b"],
            "overlay_texts": ["A OLD", "B OLD"],
            "overlay_texts_by_screen": {
                "a": ["A OLD"], "b": ["B OLD"],
            },
            "overlay_generations": {"a": "1", "b": "1"},
        }
        gs.push_event(dict(both))
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({
            **both,
            "overlay_screens": ["a"],
            "overlay_texts": ["A OLD"],
            "overlay_texts_by_screen": {"a": ["A OLD"]},
        })
        gs.push_event({
            **both,
            "overlay_screens": ["b"],
            "overlay_texts": ["B OLD"],
            "overlay_texts_by_screen": {"b": ["B OLD"]},
        })

        assert gs.transcript[-1]["passive_overlay_delta"] == []

        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({
            **both,
            "overlay_screens": ["a"],
            "overlay_texts": ["A OLD"],
            "overlay_texts_by_screen": {"a": ["A OLD"]},
        })
        gs.push_event({
            **both,
            "overlay_screens": ["b"],
            "overlay_texts": ["B OLD"],
            "overlay_texts_by_screen": {"b": ["B OLD"]},
            "overlay_generations": {"a": "1", "b": "2"},
        })

        assert gs.transcript[-1]["passive_overlay_delta"] == ["B OLD"]

    def test_resume_no_overlay_frame_preserves_registered_retained_tag(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_retained_screens": ["terminal"],
            "overlay_texts": ["OLD"],
            "overlay_texts_by_screen": {"terminal": ["OLD"]},
            "overlay_generations": {"terminal": "7"},
        }
        gs.push_event(dict(shown))
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({
            "type": "screen_content",
            "overlay_retained_screens": ["terminal"],
        })
        gs.push_event(dict(shown))

        assert gs.transcript[-1]["passive_overlay_delta"] == []

    def test_hidden_retained_contributor_survives_intervening_overlay(self):
        gs = GameState()
        retained_a = {
            "type": "screen_content",
            "overlay_screens": ["a"],
            "overlay_retained_screens": ["a"],
            "overlay_texts": ["A"],
            "overlay_texts_by_screen": {"a": ["A"]},
            "overlay_generations": {"a": "1"},
        }
        gs.push_event(dict(retained_a))
        gs.push_event({
            "type": "screen_content",
            "overlay_retained_screens": ["a"],
        })
        gs.push_event({
            "type": "screen_content",
            "overlay_screens": ["c"],
            "overlay_retained_screens": ["a"],
            "overlay_texts": ["C"],
            "overlay_texts_by_screen": {"c": ["C"]},
            "overlay_generations": {"c": "1"},
        })
        assert gs.transcript[-1]["passive_overlay_delta"] == ["C"]

        gs.push_event(dict(retained_a))
        assert gs.transcript[-1]["passive_overlay_delta"] == []

    def test_resume_retained_contributors_survive_intervening_frames(self):
        gs = GameState()
        both = {
            "type": "screen_content",
            "overlay_screens": ["a", "b"],
            "overlay_retained_screens": ["a", "b"],
            "overlay_texts": ["A", "B"],
            "overlay_texts_by_screen": {"a": ["A"], "b": ["B"]},
            "overlay_generations": {"a": "1", "b": "1"},
        }
        gs.push_event(dict(both))
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({
            **both,
            "overlay_screens": ["a"],
            "overlay_texts": ["A"],
            "overlay_texts_by_screen": {"a": ["A"]},
        })
        gs.push_event({
            "type": "screen_content",
            "overlay_retained_screens": ["a", "b"],
        })
        gs.push_event({
            **both,
            "overlay_screens": ["b"],
            "overlay_texts": ["B"],
            "overlay_texts_by_screen": {"b": ["B"]},
        })
        assert gs.transcript[-1]["passive_overlay_delta"] == []

        gs.push_event({
            **both,
            "overlay_screens": ["a"],
            "overlay_texts": ["A"],
            "overlay_texts_by_screen": {"a": ["A"]},
        })
        assert gs.transcript[-1]["passive_overlay_delta"] == []

    def test_hidden_contributor_prunes_when_retention_is_removed(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["a"],
            "overlay_retained_screens": ["a"],
            "overlay_texts": ["A"],
            "overlay_texts_by_screen": {"a": ["A"]},
            "overlay_generations": {"a": "1"},
        }
        gs.push_event(dict(shown))
        gs.push_event({
            "type": "screen_content",
            "overlay_retained_screens": ["a"],
        })
        gs.push_event({"type": "screen_content"})
        gs.push_event({
            **shown,
            "overlay_retained_screens": [],
        })

        assert gs.transcript[-1]["passive_overlay_delta"] == ["A"]

    def test_blocking_frame_prunes_removed_retained_contributor(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["a"],
            "overlay_retained_screens": ["a"],
            "overlay_texts": ["A"],
            "overlay_texts_by_screen": {"a": ["A"]},
            "overlay_generations": {"a": "1"},
        }
        gs.push_event(dict(shown))
        gs.push_event({
            "type": "screen_content",
            "overlay_active": True,
            "overlay_screens": ["choice"],
            "active_overlays": ["choice"],
            "overlay_texts": ["Choose"],
            "overlay_texts_by_screen": {"choice": ["Choose"]},
            "modal_screens": ["choice"],
        })
        gs.push_event({
            **shown,
            "overlay_retained_screens": [],
        })

        assert gs.transcript[-1]["passive_overlay_delta"] == ["A"]

    def test_blocking_overlay_does_not_alias_new_passive_contributor(self):
        gs = GameState()
        gs.push_event({
            "type": "screen_content",
            "overlay_screens": ["a"],
            "overlay_texts": ["SAME"],
            "overlay_texts_by_screen": {"a": ["SAME"]},
            "overlay_generations": {"a": "1"},
        })
        gs.push_event({
            "type": "screen_content",
            "overlay_active": True,
            "overlay_screens": ["modal"],
            "active_overlays": ["modal"],
            "overlay_texts": ["Modal"],
            "overlay_texts_by_screen": {"modal": ["Modal"]},
        })
        gs.push_event({
            "type": "screen_content",
            "overlay_screens": ["c"],
            "overlay_texts": ["SAME"],
            "overlay_texts_by_screen": {"c": ["SAME"]},
            "overlay_generations": {"c": "1"},
        })

        assert gs.transcript[-1]["passive_overlay_delta"] == ["SAME"]

    def test_blocking_visible_passive_preserves_resume_first_snapshot(self):
        gs = GameState()
        retained = {
            "type": "screen_content",
            "overlay_screens": ["a"],
            "overlay_retained_screens": ["a"],
            "overlay_texts": ["OLD"],
            "overlay_texts_by_screen": {"a": ["OLD"]},
            "overlay_generations": {"a": "8"},
        }
        restored = {
            **retained,
            "overlay_generations": {"a": "7"},
        }
        gs.push_event(dict(retained))
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({
            **restored,
            "overlay_active": True,
            "overlay_screens": ["a", "modal"],
            "active_overlays": ["modal"],
            "overlay_texts": ["OLD", "Modal"],
            "overlay_texts_by_screen": {
                "a": ["OLD"], "modal": ["Modal"],
            },
        })
        gs.push_event(restored)

        assert gs.transcript[-1]["passive_overlay_delta"] == []

    def test_blocking_visible_nonretained_preserves_resume_carry(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["a"],
            "overlay_texts": ["OLD"],
            "overlay_texts_by_screen": {"a": ["OLD"]},
            "overlay_generations": {"a": "1"},
        }
        gs.push_event(dict(shown))
        gs.push_event({"type": "game_resumed", "reason": "rollback"})
        gs.push_event({
            **shown,
            "overlay_active": True,
            "overlay_screens": ["a", "modal"],
            "active_overlays": ["modal"],
            "overlay_texts": ["OLD", "Modal"],
            "overlay_texts_by_screen": {
                "a": ["OLD"], "modal": ["Modal"],
            },
        })
        gs.push_event(dict(shown))

        assert gs.transcript[-1]["passive_overlay_delta"] == []

    def test_refreshed_passive_row_does_not_replay_stable_tail(self):
        """Bridge-authored deltas share the handler's refresh policy."""
        gs = GameState()
        base = {
            "type": "screen_content",
            "overlay_screens": ["echo_terminal_live"],
            "overlay_texts_by_screen": {
                "echo_terminal_live": [
                    "Core integrity now 73%.",
                    "FINDINGS // CONVERGENCE.DAT",
                    "Trust-chain downgrade confirmed.",
                    "Archive target ready.",
                ],
            },
            "overlay_generations": {"echo_terminal_live": "15"},
        }
        refreshed = dict(
            base,
            overlay_texts_by_screen={
                "echo_terminal_live": [
                    "Core integrity now 72%.",
                    "FINDINGS // CONVERGENCE.DAT",
                    "Trust-chain downgrade confirmed.",
                    "Archive target ready.",
                    "Archive copy complete.",
                ],
            },
        )
        base["overlay_texts"] = base["overlay_texts_by_screen"][
            "echo_terminal_live"
        ]
        refreshed["overlay_texts"] = refreshed["overlay_texts_by_screen"][
            "echo_terminal_live"
        ]

        gs.push_event(base)
        gs.push_event(refreshed)

        drained = gs.get_state(since=0)["transcript"]
        assert drained[-1]["passive_overlay_delta"] == [
            "Archive copy complete."
        ]

    def test_passive_overlay_delta_is_per_contributor_and_generation(self):
        gs = GameState()
        first = {
            "type": "screen_content",
            "overlay_screens": ["terminal", "status"],
            "overlay_texts": ["HEADER", "ONE", "READY"],
            "overlay_texts_by_screen": {
                "terminal": ["HEADER", "ONE"],
                "status": ["READY"],
            },
            "overlay_generations": {"terminal": "7", "status": "2"},
        }
        appended = dict(
            first,
            overlay_texts=["HEADER", "ONE", "TWO", "READY"],
            overlay_texts_by_screen={
                "terminal": ["HEADER", "ONE", "TWO"],
                "status": ["READY"],
            },
        )
        reset = dict(
            appended,
            overlay_generations={"terminal": "8", "status": "2"},
        )

        gs.push_event(first)
        gs.push_event(appended)
        gs.push_event(reset)

        drained = gs.get_state(since=0)["transcript"]
        assert [event["passive_overlay_delta"] for event in drained] == [
            ["HEADER", "ONE", "READY"],
            ["TWO"],
            ["HEADER", "ONE", "TWO"],
        ]

    def test_blocking_modal_does_not_close_passive_overlay_delta(self):
        gs = GameState()
        terminal = {
            "type": "screen_content",
            "overlay_screens": ["terminal"],
            "overlay_texts": ["ONE"],
            "overlay_texts_by_screen": {"terminal": ["ONE"]},
            "overlay_generations": {"terminal": "7"},
        }
        gs.push_event(terminal)
        gs.push_event({
            "type": "screen_content",
            "overlay_active": True,
            "overlay_screens": ["terminal", "choice"],
            "active_overlays": ["choice"],
            "overlay_texts": ["ONE", "Choose"],
            "overlay_texts_by_screen": {
                "terminal": ["ONE"], "choice": ["Choose"],
            },
            "modal_screens": ["choice"],
        })
        gs.push_event(dict(
            terminal,
            overlay_texts=["ONE", "TWO"],
            overlay_texts_by_screen={"terminal": ["ONE", "TWO"]},
        ))

        drained = gs.get_state(since=0)["transcript"]
        assert [event["passive_overlay_delta"] for event in drained] == [
            ["ONE"], ["TWO"],
        ]

    def test_passive_overlay_reopen_with_identical_text_is_drainable(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["journal"],
            "overlay_texts": ["SAME ROW"],
        }
        gs.push_event(dict(shown))
        gs.push_event({"type": "screen_content", "texts": []})
        gs.push_event(dict(shown))

        drained = gs.get_state(since=0)["transcript"]
        assert len(drained) == 3
        assert drained[1].get("overlay_texts", []) == []

    def test_explicit_empty_contributor_snapshot_is_drainable(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["journal"],
            "overlay_texts": ["SAME ROW"],
            "overlay_texts_by_screen": {"journal": ["SAME ROW"]},
        }
        empty = dict(
            shown,
            overlay_texts=[],
            overlay_texts_by_screen={"journal": []},
        )

        gs.push_event(dict(shown))
        gs.push_event(empty)
        gs.push_event(dict(shown))

        drained = gs.get_state(since=0)["transcript"]
        assert [event["overlay_texts"] for event in drained] == [
            ["SAME ROW"], [], ["SAME ROW"],
        ]

    def test_passive_overlay_generation_change_with_identical_text_is_drainable(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["journal", "status"],
            "overlay_retained_screens": ["journal"],
            "overlay_texts": ["SAME ROW"],
            "overlay_generations": {"journal": "7", "status": "2"},
        }
        reordered = dict(
            shown,
            overlay_generations={"status": "2", "journal": "7"},
        )
        next_generation = dict(
            shown,
            overlay_generations={"journal": "8", "status": "2"},
        )

        gs.push_event(dict(shown))
        gs.push_event(reordered)
        gs.push_event(next_generation)

        drained = gs.get_state(since=0)["transcript"]
        assert [event["overlay_generations"]["journal"] for event in drained] == [
            "7",
            "8",
        ]
        assert all(event["passive_overlay_snapshot"] for event in drained)

    def test_passive_overlay_retention_change_with_identical_text_is_drainable(self):
        gs = GameState()
        shown = {
            "type": "screen_content",
            "overlay_screens": ["journal", "status"],
            "overlay_retained_screens": ["status", "journal"],
            "overlay_texts": ["SAME ROW"],
        }

        gs.push_event(dict(shown))
        gs.push_event(dict(
            shown,
            overlay_retained_screens=["journal", "status"],
        ))
        gs.push_event(dict(shown, overlay_retained_screens=["journal"]))

        drained = gs.get_state(since=0)["transcript"]
        assert [event["overlay_retained_screens"] for event in drained] == [
            ["status", "journal"],
            ["journal"],
        ]

    def test_blocking_overlay_remains_state_only(self):
        gs = GameState()
        gs.push_event({
            "type": "screen_content",
            "overlay_active": True,
            "overlay_screens": ["blocking_panel"],
            "overlay_texts": ["VISIBLE THROUGH SCREEN TEXT"],
        })

        assert gs.transcript == []

    def test_game_started_sets_running(self):
        gs = GameState()
        gs.push_event({"type": "game_started"})
        assert gs.status == "running"

    def test_game_ended_sets_ended(self):
        gs = GameState()
        gs.push_event({"type": "game_ended", "reason": "main_menu"})
        assert gs.status == "ended"
        assert gs.end_reason == "main_menu"

    def test_context_in_game_resets_ended(self):
        gs = GameState()
        gs.status = "ended"
        gs.push_event({"type": "context", "context": "in_game"})
        assert gs.status == "running"
        assert gs.end_reason is None

    def test_context_main_menu_after_gameplay_marks_ended(self):
        gs = GameState()
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.set_pending_request({"type": "choice_request", "id": "c1", "choices": ["End"]})

        gs.push_event({"type": "context", "context": "main_menu"})

        assert gs.status == "ended"
        assert gs.end_reason == "return_to_menu"
        assert gs.pending_request is None
        assert gs.pending_action is None
        assert gs.transcript[-1]["type"] == "game_ended"
        assert gs.transcript[-1]["reason"] == "return_to_menu"
        # Return-to-menu after gameplay is playthrough-terminal: latch
        # game_terminal so act/wait results surface it (no progress mod needed).
        assert gs.current_game_terminal is True
        assert gs.get_state().get("game_terminal") is True

    def test_return_to_menu_terminal_clears_on_new_playthrough(self):
        gs = GameState()
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "narration", "text": "Once upon a time..."})
        gs.push_event({"type": "context", "context": "main_menu"})
        assert gs.current_game_terminal is True  # latched at the ending
        # A fresh playthrough begins from the menu -> the stale terminal clears.
        gs.push_event({"type": "context", "context": "in_game"})
        assert gs.status == "running"
        assert gs.current_game_terminal is False

    def test_launch_splashscreen_does_not_mark_ended(self):
        # A launch splashscreen reports the in_game context but shows no story
        # content, so the main menu that follows must NOT latch a false ending
        # (regression: Slay the Princess auto-ended ~96s in on its splash->menu).
        gs = GameState()
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})  # splashscreen
        gs.push_event({"type": "context", "context": "main_menu"})
        assert gs.status != "ended"
        assert gs.current_game_terminal is False
        assert gs.end_reason != "return_to_menu"

    def test_story_content_then_menu_marks_ended(self):
        # Real story content (dialogue) marks gameplay seen, so a later return
        # to the main menu still latches the terminal state.
        gs = GameState()
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "dialogue", "character": "Narrator", "text": "..."})
        gs.push_event({"type": "context", "context": "main_menu"})
        assert gs.status == "ended"
        assert gs.end_reason == "return_to_menu"
        assert gs.current_game_terminal is True

    def test_context_main_menu_after_pending_request_marks_ended(self):
        gs = GameState()
        gs.push_event({"type": "game_started"})
        gs.set_pending_request({"type": "choice_request", "id": "c1", "choices": ["End"]})

        gs.push_event({"type": "context", "context": "main_menu"})

        assert gs.status == "ended"
        assert gs.end_reason == "return_to_menu"
        assert gs.pending_request is None
        assert gs.transcript[-1]["type"] == "game_ended"

    def test_initial_main_menu_context_does_not_mark_ended(self):
        gs = GameState()

        gs.push_event({"type": "context", "context": "main_menu"})

        assert gs.status == "idle"
        assert gs.end_reason is None
        assert all(ev.get("type") != "game_ended" for ev in gs.transcript)

    def test_anomaly_sets_flag(self):
        gs = GameState()
        anomaly = {"type": "anomaly", "kind": "renpy_exception", "details": {"message": "crash"}}
        gs.push_event(anomaly)
        assert gs.anomaly_flag is not None
        assert gs.anomaly_flag["kind"] == "renpy_exception"

    def test_anomaly_flag_persists(self):
        """Anomaly flag should NOT auto-clear on state read."""
        gs = GameState()
        gs.push_event({"type": "anomaly", "kind": "test"})
        state = gs.get_state()
        assert state.get("anomaly") is not None
        # Read again — should still be there (no auto-clear).
        state2 = gs.get_state()
        assert state2.get("anomaly") is not None

    def test_anomaly_flag_clear(self):
        gs = GameState()
        gs.push_event({"type": "anomaly", "kind": "test"})
        assert gs.anomaly_flag is not None
        gs.anomaly_flag = None
        assert gs.anomaly_flag is None

    def test_get_state_with_since(self):
        gs = GameState()
        gs.push_event({"type": "a"})
        gs.push_event({"type": "b"})
        gs.push_event({"type": "c"})
        state = gs.get_state(since=1)
        events = state["transcript"]
        assert len(events) == 2  # b and c (seq 2 and 3).

    def test_get_state_all_events(self):
        gs = GameState()
        gs.push_event({"type": "a"})
        gs.push_event({"type": "b"})
        state = gs.get_state(since=0)
        assert len(state["transcript"]) == 2

    def test_get_state_exposes_whether_gameplay_has_started(self):
        gs = GameState()
        assert gs.get_state()["gameplay_seen"] is False

        gs.push_event({"type": "dialogue", "who": "ARIA", "text": "Ready."})

        assert gs.get_state()["gameplay_seen"] is True

    def test_respond_json_swallows_client_disconnects(self, capsys):
        # A poll loop that times out its own request aborts the socket while
        # the bridge is mid-write; a live session logged 156 of these as full
        # socketserver stack dumps. One quiet line, never a raised exception.
        from vnflight.bridge import BridgeHandler

        handler = BridgeHandler.__new__(BridgeHandler)
        handler.path = "/transaction?action_nonce=x"
        handler.send_response = lambda status: None
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None

        class _AbortingWfile:
            def write(self, _body):
                raise ConnectionAbortedError

        handler.wfile = _AbortingWfile()
        handler._respond_json({"ok": True})  # must not raise
        assert "client closed connection" in capsys.readouterr().out

    def test_get_transcript_zero_explicitly_returns_retained_history(self):
        gs = GameState()
        gs.push_event({"type": "a"})
        gs.push_event({"type": "b"})

        assert gs.get_transcript(last_n=0) == gs.get_transcript()

    def test_transcript_capping(self):
        gs = GameState()
        gs._max_transcript = 10
        for i in range(40):
            gs.push_event({"type": "test", "n": i})
        # The in-memory transcript is bounded to _max_transcript so long
        # runs don't grow unbounded (durable history lives in the JSONL log).
        assert len(gs.transcript) == 10
        # The most recent events are kept; event_counter stays monotonic
        # so get_state(since=N) still works after trimming.
        assert gs.transcript[-1]["n"] == 39
        assert gs.transcript[0]["n"] == 30
        assert gs.event_counter == 40
        # A client polling from before the trimmed floor gets the window
        # from the floor onward (no crash, no duplication).
        recent = gs.get_state(since=0)
        assert len(recent["transcript"]) == 10
        # since past the floor returns only the newer slice (_seq 36-40).
        assert len(gs.get_state(since=35)["transcript"]) == 5

    def test_requests_by_id_bounded(self):
        gs = GameState()
        gs._max_transcript = 10  # cap floor for requests is max(this, 1000)
        for i in range(1100):
            gs.set_pending_request({"id": "req-%d" % i, "type": "choice_request"})
        # Bounded to the recent window; the newest request is retained.
        assert len(gs.requests_by_id) <= 1000
        assert "req-1099" in gs.requests_by_id

    def test_auto_advance_config(self):
        gs = GameState()
        assert gs.auto_advance is True  # Default.
        gs.auto_advance = False
        assert gs.auto_advance is False

    def test_story_reset_preserves_slot_runtime_configuration(self):
        gs = GameState()
        gs.update_config({
            "auto_advance": False,
            "auto_advance_delay": 0.01,
            "end_on_menu_return": False,
        })
        gs.push_event({"type": "narration", "text": "Old run."})

        gs.reset()

        assert gs.auto_advance is False
        assert gs.auto_advance_delay == 0.01
        assert gs.end_on_menu_return is False
        assert gs.transcript == []

    def test_mod_loaded_sets_pid(self):
        gs = GameState()
        gs.push_event({"type": "mod_loaded", "pid": 12345})
        assert gs.game_pid == 12345

    def test_get_state_lifts_custom_commands_from_game_state(self):
        gs = GameState()
        gs.push_event({
            "type": "game_state",
            "custom_commands": ["after_input_text", "progress"],
        })

        state = gs.get_state()

        assert state["game_state"]["custom_commands"] == [
            "after_input_text",
            "progress",
        ]
        assert state["custom_commands"] == ["after_input_text", "progress"]

    def test_pending_hidden_after_action_submitted(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "input_request",
            "id": "name-1",
            "prompt": "What is your name?",
        })
        ok, msg = gs.submit_action({
            "type": "input",
            "request_id": "name-1",
            "text": "Alex",
        })

        assert ok is True
        assert msg == "Action submitted."
        assert gs.get_pending_request() is None
        state = gs.get_state()
        assert state["pending_request"] is None
        assert state["has_pending_action"] is True

    def test_input_retry_cannot_replace_accepted_text(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "input_request",
            "id": "name-1",
            "prompt": "What is your name?",
        })
        assert gs.submit_action({
            "type": "input", "request_id": "name-1", "text": "Alex",
        })[0] is True

        same_ok, _ = gs.submit_action({
            "type": "input", "request_id": "name-1", "text": "Alex",
        })
        changed_ok, changed_message = gs.submit_action({
            "type": "input", "request_id": "name-1", "text": "Alice",
        })

        assert same_ok is True
        assert changed_ok is False
        assert "identical payload" in changed_message
        assert gs.consume_action(request_id="name-1")["text"] == "Alex"

    def test_consuming_action_clears_pending_request(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "input_request",
            "id": "name-1",
            "prompt": "What is your name?",
        })
        ok, _ = gs.submit_action({
            "type": "input",
            "request_id": "name-1",
            "text": "Alex",
        })
        assert ok is True

        action = gs.consume_action(request_id="name-1")

        assert action is not None
        assert action["text"] == "Alex"
        assert gs.pending_request is None
        assert gs.get_pending_request() is None
        assert gs.status == "running"
        assert gs.requests_by_id["name-1"]["resolution"] == action

    def test_choice_resolved_event_clears_matching_pending_request(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request",
            "id": "choice-1",
            "choices": ["Done"],
        })

        gs.push_event({
            "type": "choice_resolved",
            "request_id": "choice-1",
            "label": "Done",
            "resolved_by": "shim",
        })

        assert gs.pending_request is None
        assert gs.get_pending_request() is None
        assert gs.status == "running"
        assert gs.requests_by_id["choice-1"]["resolution"]["label"] == "Done"

    def test_choice_resolved_event_ignores_mismatched_pending_request(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request",
            "id": "choice-1",
            "choices": ["Done"],
        })

        gs.push_event({
            "type": "choice_resolved",
            "request_id": "other-choice",
            "label": "Done",
        })

        assert gs.pending_request is not None
        assert gs.pending_request["id"] == "choice-1"
        assert gs.status == "waiting_for_input"

    def test_input_resolved_event_clears_matching_pending_request(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "input_request",
            "id": "input-1",
            "prompt": "Name",
        })

        gs.push_event({
            "type": "input_resolved",
            "request_id": "input-1",
        })

        assert gs.pending_request is None
        assert gs.requests_by_id["input-1"]["resolution"]["type"] == (
            "input_resolved"
        )

    def test_retried_matching_resolution_is_idempotent(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "input_request",
            "id": "input-1",
            "prompt": "Name",
        })
        resolution = {
            "type": "input_resolved",
            "request_id": "input-1",
        }

        first_seq = gs.push_event(dict(resolution))
        second_seq = gs.push_event(dict(resolution))

        assert second_seq == first_seq
        assert [
            event["type"] for event in gs.transcript
            if event.get("request_id") == "input-1"
        ] == ["input_resolved"]

    def test_retried_command_result_is_idempotent_per_reset_generation(self):
        gs = GameState()
        result = {
            "type": "command_result",
            "command": "set",
            "nonce": "set-1",
            "success": True,
        }

        first_seq = gs.push_event(dict(result))
        duplicate_seq = gs.push_event(dict(result))

        assert duplicate_seq == first_seq
        assert [
            event for event in gs.transcript
            if event.get("nonce") == "set-1"
        ] == [dict(result, _seq=first_seq)]

        gs.reset()
        reset_counter = gs.event_counter
        replay_seq = gs.push_event(dict(result))

        assert replay_seq == reset_counter + 1
        assert [
            event for event in gs.transcript
            if event.get("nonce") == "set-1"
        ] == [dict(result, _seq=replay_seq)]

        duplicate_replay_seq = gs.push_event(dict(result))
        assert duplicate_replay_seq == replay_seq
        assert len(gs.transcript) == 1

    def test_retried_source_occurrence_is_idempotent_for_act_result(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request",
            "id": "choice-1",
            "choices": ["Continue"],
        })
        command = {
            "name": "act",
            "args": {"index": 1},
            "nonce": "act-1",
            "reset_generation": 0,
        }
        ok, message, _ack = gs.submit_command_with_ack(command)
        assert ok is True, message
        assert gs.consume_command() == command
        result = {
            "type": "command_result",
            "command": "act",
            "nonce": "act-1",
            "success": True,
            "_source_id": "shim-session",
            "_source_seq": 7,
        }

        first_seq = gs.push_event(dict(result))
        retry_seq = gs.push_event(dict(result))

        assert retry_seq == first_seq
        assert [event["type"] for event in gs.transcript] == [
            "choice_request", "command_result",
        ]
        transaction = gs.get_action_transaction("act-1")
        assert transaction["transaction_state"] == "applied"
        assert [
            event["type"] for event in transaction["events"]
            if event.get("type") == "command_result"
        ] == ["command_result"]

    # -- command queue (mailbox-loss fix) --

    def test_commands_queue_fifo(self):
        gs = GameState()
        ok1, _ = gs.submit_command({"name": "save", "args": {"slot": "a"}})
        ok2, _ = gs.submit_command({"name": "load", "args": {"slot": "b"}})
        assert ok1 is True
        assert ok2 is True
        # Both commands survive until the shim consumes them, in order.
        assert gs.consume_command()["name"] == "save"
        assert gs.consume_command()["name"] == "load"
        assert gs.consume_command() is None

    def test_command_queue_full_rejects_submission(self):
        gs = GameState()
        for i in range(gs._MAX_PENDING_COMMANDS):
            ok, _ = gs.submit_command({"name": f"cmd{i}"})
            assert ok is True
        ok, msg = gs.submit_command({"name": "overflow"})
        # The caller must SEE the failure — the old single-slot mailbox
        # overwrote the pending command and reported success.
        assert ok is False
        assert "queue is full" in msg
        # The queued commands are untouched.
        assert gs.consume_command()["name"] == "cmd0"

    def test_command_missing_name_rejected(self):
        gs = GameState()
        ok, msg = gs.submit_command({"args": {}})
        assert ok is False
        assert "name" in msg

    def test_pending_command_compat_property(self):
        gs = GameState()
        assert gs.pending_command is None
        gs.submit_command({"name": "save"})
        assert gs.pending_command["name"] == "save"
        state = gs.get_state()
        assert state["has_pending_command"] is True
        assert state["pending_command_count"] == 1

    def test_reset_clears_command_queue(self):
        gs = GameState()
        gs.submit_command({"name": "save"})
        gs.reset()
        assert gs.consume_command() is None

    # -- command nonce idempotency (transport-timeout retry double-act fix) --

    def test_command_nonce_dedups_duplicate_enqueue(self):
        """A transport-timeout retry re-sends the SAME logical command (same
        nonce) while the first is still queued.  The bridge must enqueue it
        ONCE and hand the retry the original success ack — a duplicate act
        would apply the choice twice once the shim polls."""
        gs = GameState()
        ok1, msg1 = gs.submit_command(
            {"name": "act", "args": {"index": 1}, "nonce": "N1"})
        # Simulate the lost-response retry: identical body, identical nonce,
        # first command still pending (not yet consumed by the shim).
        ok2, msg2 = gs.submit_command(
            {"name": "act", "args": {"index": 1}, "nonce": "N1"})
        assert ok1 is True and ok2 is True
        # The replay returns the SAME ack shape (success, not error)...
        assert (ok2, msg2) == (ok1, msg1)
        # ...and does NOT enqueue a second copy.
        assert len(gs.pending_commands) == 1

    def test_command_nonce_requeues_result_replay_after_consumption(self):
        gs = GameState()
        command = {
            "name": "load", "args": {"slot": "quick"}, "nonce": "N1",
        }
        ok1, msg1 = gs.submit_command(dict(command))
        assert gs.consume_command()["nonce"] == "N1"

        ok2, msg2 = gs.submit_command(dict(command))

        assert (ok2, msg2) == (ok1, msg1)
        assert list(gs.pending_commands) == [command]

    def test_command_distinct_nonce_enqueues_normally(self):
        gs = GameState()
        gs.submit_command({"name": "act", "args": {"index": 1}, "nonce": "N1"})
        ok, _ = gs.submit_command(
            {"name": "act", "args": {"index": 2}, "nonce": "N2"})
        assert ok is True
        assert len(gs.pending_commands) == 2

    def test_command_without_nonce_is_never_deduped(self):
        """Old clients / raw curl send no nonce — behave exactly as before
        (every submission enqueues)."""
        gs = GameState()
        ok1, _ = gs.submit_command({"name": "save"})
        ok2, _ = gs.submit_command({"name": "save"})
        assert ok1 is True and ok2 is True
        assert len(gs.pending_commands) == 2

    def test_reset_preserves_command_nonce_memory_for_load_retry(self):
        gs = GameState()
        command = {"name": "load", "args": {"slot": "quick"}, "nonce": "N1"}
        gs.submit_command(dict(command))
        assert gs.consume_command()["nonce"] == "N1"
        gs.reset()

        ok, _ = gs.submit_command(dict(command))

        assert ok is True
        # The replay reaches the shim's nonce-result cache instead of being
        # accepted as a new logical load.
        assert len(gs.pending_commands) == 1

    def test_command_nonce_rejects_different_arguments(self):
        gs = GameState()
        gs.submit_command({
            "name": "save", "args": {"slot": "a"}, "nonce": "N1",
        })

        ok, message = gs.submit_command({
            "name": "save", "args": {"slot": "b"}, "nonce": "N1",
        })

        assert ok is False
        assert "different command or argument" in message
        assert len(gs.pending_commands) == 1

    def test_reset_generation_is_exposed_and_monotonic(self):
        gs = GameState()
        assert gs.get_state()["reset_generation"] == 0

        gs.reset()
        assert gs.get_state()["reset_generation"] == 1

        gs.reset()
        assert gs.get_state()["reset_generation"] == 2

    def test_transactional_act_ack_apply_settle_and_drain(self):
        gs = GameState()
        gs.slot_id = 3
        gs.set_pending_request({
            "type": "choice_request", "id": "before", "choices": ["Go"],
        })
        command = {
            "name": "act", "args": {"index": 1}, "nonce": "act-1",
            "reset_generation": 0,
            "_invocation": {
                "server_instance_id": "server-a",
                "call_id": "call-a",
                "original_target": "Go",
                "attempt_kind": "initial",
            },
        }

        ok, _, ack = gs.submit_command_with_ack(command)

        assert ok is True
        assert ack["transaction_state"] == "accepted"
        assert ack["action_id"] == 1
        assert ack["submitted_target"] == 1
        assert "invocation" not in ack
        assert "resolved_as" not in ack
        shim_command = gs.consume_command()
        assert "_invocation" not in shim_command
        assert shim_command == {
            key: value for key, value in command.items()
            if key != "_invocation"
        }
        assert gs._act_transactions["act-1"]["invocation"] == {
            "server_instance_id": "server-a",
            "call_id": "call-a",
            "original_target": "Go",
            "attempt_kind": "initial",
        }

        gs.push_event({"type": "dialogue", "text": "Onward."})
        gs.push_event({
            "type": "command_result", "command": "act", "nonce": "act-1",
            "success": True, "resolved_as": "choice", "resolved_label": "Go",
        })
        applied = gs.get_action_transaction("act-1")
        assert applied["transaction_state"] == "applied"
        assert applied["resolved_as"] == "choice"

        gs.set_pending_request({
            "type": "choice_request", "id": "after", "choices": ["Stay"],
        })
        gs._ACTION_SETTLE_GRACE = 0
        settled = gs.get_action_transaction("act-1")
        assert settled["transaction_state"] == "settled"
        assert settled["pending"] is False
        assert [event["type"] for event in settled["events"]] == [
            "dialogue", "command_result", "choice_request",
        ]
        assert all(event["action_id"] == 1 for event in settled["events"])
        # Reads are replay-safe; only an explicit ack consumes.
        replayed = gs.get_action_transaction("act-1")
        assert replayed["events"] == settled["events"]
        assert gs.acknowledge_action_events("act-1", replayed["delivery_end"])
        assert gs.get_action_transaction("act-1")["events"] == []

    def test_transaction_acceptance_persists_real_source_boundary(self):
        gs = GameState()
        gs.slot_id = 3
        gs.push_event({
            "type": "narration", "text": "Before.",
            "_source_id": "shim-session", "_source_seq": 91,
        })
        gs.set_pending_request({
            "type": "choice_request", "id": "before", "choices": ["Go"],
            "_source_id": "shim-session", "_source_seq": 92,
        })

        ok, _, ack = gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "source-bound",
            "reset_generation": 0,
        })

        assert ok is True
        assert ack["transaction_state"] == "accepted"
        record = gs.get_action_transaction("source-bound")
        assert record["_source_id"] == "shim-session"
        assert record["_source_seq"] == 92

    @pytest.mark.parametrize("event_type", ["game_started", "game_resumed"])
    def test_active_action_owns_lifecycle_but_native_event_does_not(
        self, event_type,
    ):
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request", "id": "before", "choices": ["Go"],
        })
        command = {
            "name": "act", "args": {"index": 1}, "nonce": "boundary",
            "reset_generation": 0,
        }
        ok, message, _ack = gs.submit_command_with_ack(command)
        assert ok is True, message
        assert gs.consume_command() == command
        gs.push_event({
            "type": "command_result", "command": "act",
            "nonce": "boundary", "success": True,
        })

        gs.push_event({"type": event_type})

        owned = gs.get_action_transaction("boundary")["events"][-1]
        assert owned["type"] == event_type
        assert owned["action_id"] == 1
        assert gs.transcript[-1]["action_id"] == 1

        native = GameState()
        native.push_event({"type": event_type})
        assert "action_id" not in native.transcript[-1]

    @pytest.mark.parametrize("invocation", [
        "not-a-dict",
        {"server_instance_id": "server-only"},
        {"server_instance_id": 7, "call_id": "call-a"},
    ])
    def test_transactional_act_drops_malformed_invocation(self, invocation):
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request", "id": "before", "choices": ["Go"],
        })
        command = {
            "name": "act",
            "args": {"index": 1},
            "nonce": "bad-invocation",
            "reset_generation": 0,
            "_invocation": invocation,
        }

        ok, _, ack = gs.submit_command_with_ack(command)

        assert ok is True
        assert "invocation" not in ack
        assert "invocation" not in gs._act_transactions["bad-invocation"]
        assert "_invocation" not in gs.consume_command()

    def test_transactional_act_deduplicates_beyond_transport_lru(self):
        gs = GameState()
        command = {
            "name": "act", "args": {"label": "Open"}, "nonce": "durable",
            "reset_generation": 0,
        }
        _, _, first = gs.submit_command_with_ack(command)
        gs.consume_command()
        gs.push_event({
            "type": "command_result", "command": "act", "nonce": "durable",
            "success": True,
        })
        gs.set_pending_request({
            "type": "choice_request", "id": "next", "choices": ["Next"],
        })
        for index in range(40):
            gs.submit_command({"name": "save", "nonce": f"later-{index}"})
            gs.consume_command()

        ok, _, replay = gs.submit_command_with_ack(command)

        assert ok is True
        assert replay["deduplicated"] is True
        assert replay["action_id"] == first["action_id"]
        assert gs.consume_command() is None

    def test_transactional_act_rejects_stale_generation_after_reset(self):
        gs = GameState()
        gs.reset()

        ok, _, rejected = gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "old",
            "reset_generation": 0,
        })

        assert ok is False
        assert rejected["transaction_state"] == "rejected"
        assert rejected["reason"] == "stale_generation"
        assert gs.consume_command() is None

    def test_transaction_journal_survives_bridge_restart(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        first = GameState()
        first.configure_identity(1, "test_game", 4321)
        command = {
            "name": "act", "args": {"index": 1}, "nonce": "resume-me",
            "reset_generation": 0,
        }
        first.submit_command_with_ack(command)
        first.consume_command()
        first.push_event({"type": "dialogue", "text": "Remember me."})

        restarted = GameState()
        restarted.configure_identity(1, "test_game", 4321)
        recovered = restarted.get_action_transaction("resume-me")

        assert recovered["action_id"] == 1
        assert recovered["transaction_state"] == "accepted"
        assert recovered["events"][0]["text"] == "Remember me."
        # The bridge re-leases the command after restart. The shim's durable-
        # for-process nonce cache replays its prior result instead of applying
        # the Ren'Py action twice; if the first GET response was lost, this
        # lease is what ensures the action still executes once.
        assert restarted.consume_command() == command
        assert restarted._active_action_nonce == "resume-me"

    def test_reset_preserves_old_transaction_output_and_generation(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "reset_game", 9876)
        gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "old-action",
            "reset_generation": 0,
        })
        gs.consume_command()
        gs.push_event({"type": "dialogue", "text": "Before reset."})

        gs.reset()

        recovered = gs.get_action_transaction("old-action")
        assert recovered["transaction_state"] == "failed"
        assert recovered["events"][0]["text"] == "Before reset."
        restarted = GameState()
        restarted.configure_identity(1, "reset_game", 9876)
        assert restarted.reset_generation == 1

    def test_transactional_act_records_shim_failure(self):
        gs = GameState()
        gs.submit_command_with_ack({
            "name": "act", "args": {"label": "Missing"}, "nonce": "bad-act",
            "reset_generation": 0,
        })
        gs.consume_command()
        gs.push_event({
            "type": "command_result", "command": "act", "nonce": "bad-act",
            "success": False, "error": "No interaction matching target.",
        })

        failed = gs.get_action_transaction("bad-act")
        assert failed["transaction_state"] == "failed"
        assert failed["pending"] is False
        assert "No interaction" in failed["error"]

    def test_transactional_act_rejects_when_acceptance_cannot_be_persisted(
        self, monkeypatch,
    ):
        gs = GameState()
        monkeypatch.setattr(gs, "_persist_transaction", lambda record: False)

        ok, _, rejected = gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "not-durable",
            "reset_generation": 0,
        })

        assert ok is False
        assert rejected["reason"] == "persistence_error"
        assert gs.get_action_transaction("not-durable") is None
        assert gs.consume_command() is None

    def test_transaction_read_is_replay_safe_until_acknowledged(self):
        gs = GameState()
        command = {
            "name": "act", "args": {"index": 1}, "nonce": "replay-safe",
            "reset_generation": 0,
        }
        gs.submit_command_with_ack(command)
        gs.consume_command()
        gs.push_event({"type": "dialogue", "text": "Do not lose this."})

        first = gs.get_action_transaction("replay-safe")
        replay = gs.get_action_transaction("replay-safe")

        assert first["events"] == replay["events"]
        assert first["delivery_end"] == 1
        assert gs.acknowledge_action_events("replay-safe", 1) == "ok"
        assert gs.get_action_transaction("replay-safe")["events"] == []

    def test_legacy_nonce_act_without_generation_still_queues(self):
        gs = GameState()
        command = {"name": "act", "args": {"index": 1}, "nonce": "legacy"}

        ok, _, ack = gs.submit_command_with_ack(command)

        assert ok is True
        assert ack == {}
        assert gs.consume_command() == command

    def test_acceptance_is_not_visible_before_journal_commit(self, monkeypatch):
        gs = GameState()
        observed = {}

        def persist(record):
            observed["registered"] = record["action_nonce"] in gs._act_transactions
            observed["queued"] = bool(gs.pending_commands)
            return True

        monkeypatch.setattr(gs, "_persist_transaction", persist)
        ok, _, _ = gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "ordered",
            "reset_generation": 0,
        })

        assert ok is True
        assert observed == {"registered": False, "queued": False}

    def test_late_story_during_settle_grace_stays_action_scoped(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request", "id": "before", "choices": ["Go"],
        })
        gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "late-story",
            "reset_generation": 0,
        })
        gs.consume_command()
        gs.push_event({
            "type": "command_result", "command": "act", "nonce": "late-story",
            "success": True, "resolved_as": "choice",
        })
        gs.set_pending_request({
            "type": "choice_request", "id": "after", "choices": ["Next"],
        })
        gs.push_event({"type": "dialogue", "text": "Arrived after request."})
        gs._ACTION_SETTLE_GRACE = 0

        recovered = gs.get_action_transaction("late-story")

        assert recovered["transaction_state"] == "settled"
        assert recovered["events"][-1]["text"] == "Arrived after request."
        assert recovered["events"][-1]["action_id"] == recovered["action_id"]

    def test_late_result_cannot_resurrect_reset_transaction(self):
        gs = GameState()
        gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "old-result",
            "reset_generation": 0,
        })
        gs.consume_command()
        gs.reset()

        gs.push_event({
            "type": "command_result", "command": "act", "nonce": "old-result",
            "success": True, "resolved_as": "choice",
        })

        recovered = gs.get_action_transaction("old-result")
        assert recovered["transaction_state"] == "failed"
        assert recovered["events"] == []

    def test_freed_slot_retains_transaction_recovery_archive(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        manager = SlotManager(require_token=True)
        slot_id = manager.assign("archive_game", game_pid=7654)
        manager.reserve(str(slot_id), token="archive-secret")
        gs = manager.get(slot_id)
        gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "archived",
            "reset_generation": 0,
        })

        assert manager.free(slot_id) is True
        assert manager.get(slot_id) is None
        assert manager.get_transaction_archive(slot_id) is gs
        assert manager.is_empty() is True
        assert manager.check_token(slot_id, "archive-secret") is True
        assert manager.check_token(slot_id, "wrong") is False

        restarted = SlotManager(require_token=True)
        recovered = restarted.get_transaction_archive(slot_id)
        assert recovered is not None
        transaction = recovered.get_action_transaction("archived")
        assert transaction["transaction_state"] == "failed"
        assert "freed" in transaction["error"]
        assert restarted.check_token(slot_id, "archive-secret") is True
        assert restarted.check_token(slot_id, None) is False
        assert restarted.assign("new_game") > slot_id

    def test_archive_index_failure_keeps_slot_active(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        manager = SlotManager()
        slot_id = manager.assign("archive_failure")
        gs = manager.get(slot_id)
        gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "keep-active",
            "reset_generation": 0,
        })
        monkeypatch.setattr(manager, "_persist_transaction_archive", lambda *args: False)

        assert manager.free(slot_id) is False
        assert manager.get(slot_id) is gs
        assert manager.get_transaction_archive(slot_id) is None
        assert gs._closed is False
        assert gs._act_transactions["keep-active"]["transaction_state"] == "accepted"
        assert gs.pending_commands[0]["nonce"] == "keep-active"

    def test_rollback_write_failure_is_repaired_by_the_snapshot_backlog(
        self, tmp_path, monkeypatch,
    ):
        """Disk must never LEAD memory (bridge.py invariant at the drain site).

        The seal writes TERMINAL states, then the archive index fails and
        memory rolls back to live.  If the re-persist is dropped on the floor,
        the journal keeps saying "failed" while memory says "accepted" — a
        restart would then resurrect the wrong verdict.  The failed write goes
        to the retryable snapshot backlog instead, and a later flush repairs it.
        """
        monkeypatch.chdir(tmp_path)
        manager = SlotManager()
        slot_id = manager.assign("rollback_repair")
        gs = manager.get(slot_id)
        gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "rolled-back",
            "reset_generation": 0,
        })
        monkeypatch.setattr(
            manager, "_persist_transaction_archive", lambda *args: False)

        # Fail exactly the rollback write (the restored, non-terminal record);
        # the seal's terminal writes must land so disk really does lead.
        real_persist = gs._persist_transaction
        failed_once = []

        def flaky(record):
            if record.get("transaction_state") == "accepted" and not failed_once:
                failed_once.append(record.get("action_nonce"))
                return False
            return real_persist(record)

        monkeypatch.setattr(gs, "_persist_transaction", flaky)

        assert manager.free_with_status(slot_id) == (False, "persistence_failed")
        assert failed_once == ["rolled-back"]
        assert gs._act_transactions["rolled-back"]["transaction_state"] == (
            "accepted")
        # Disk is still ahead of memory at this point ...
        stale = GameState()
        stale._transaction_log_path = gs._transaction_log_path
        stale._load_transaction_journal()
        assert stale._act_transactions["rolled-back"]["transaction_state"] == (
            "failed")

        # ... but the correction was retained, not lost.
        assert "rolled-back" in gs._transaction_snapshot_backlog
        assert gs._flush_transaction_snapshots() is True

        restarted = GameState()
        restarted._transaction_log_path = gs._transaction_log_path
        restarted._load_transaction_journal()
        assert restarted._act_transactions["rolled-back"][
            "transaction_state"] == "accepted"

    def test_free_does_not_hold_the_manager_lock_across_slot_disk_io(
        self, tmp_path, monkeypatch,
    ):
        """One hung write on one slot must not stall every other slot.

        free_with_status takes the per-slot locks and does its journal/archive
        writes with the SlotManager lock RELEASED; ``_freeing`` keeps the
        teardown exclusive instead.
        """
        monkeypatch.chdir(tmp_path)
        manager = SlotManager()
        slot_id = manager.assign("slow_free")
        other_id = manager.assign("other_game")
        gs = manager.get(slot_id)
        gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "slow",
            "reset_generation": 0,
        })

        entered = threading.Event()
        release = threading.Event()

        def hung_write(*args):
            entered.set()
            release.wait(10)
            return False

        monkeypatch.setattr(manager, "_persist_transaction_archive", hung_write)
        worker = threading.Thread(target=manager.free, args=(slot_id,))
        worker.start()
        try:
            assert entered.wait(5) is True
            observed: list[object] = []
            probe = threading.Thread(
                target=lambda: observed.append(manager.get(other_id)))
            probe.start()
            probe.join(2.0)
            assert not probe.is_alive(), (
                "SlotManager._lock is held across per-slot disk I/O")
            assert observed and observed[0] is not None
            # A second free of the same slot is refused while one is running.
            assert manager.free_with_status(slot_id) == (False, "not_found")
        finally:
            release.set()
            worker.join(10)
        assert worker.is_alive() is False
        assert manager.get(slot_id) is gs

    def test_freed_state_rejects_stale_reference_submissions(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        manager = SlotManager()
        slot_id = manager.assign("closed_slot")
        gs = manager.get(slot_id)

        assert manager.free(slot_id) is True
        accepted, _, transaction = gs.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "after-free",
            "reset_generation": 0,
        })

        assert accepted is False
        assert transaction["reason"] == "slot_closed"
        assert gs.submit_action({"type": "act"}) == (False, "Slot is closed.")
        assert gs.submit_inventory_change({}) == (False, "Slot is closed.")
        assert gs.update_config({"auto_advance": False}) is None
        assert gs.reset() is False
        assert gs.push_event({"type": "dialogue", "text": "late"}) is None
        assert gs.set_pending_request({"type": "choice_request"}) is False
        assert manager.get_transaction_archive(slot_id) is None

    def test_archive_retention_compacts_index_and_journals(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        manager = SlotManager()
        manager._MAX_TRANSACTION_ARCHIVES = 2
        journal_paths = []
        slot_ids = []
        for index in range(3):
            slot_id = manager.assign(f"archive_{index}")
            slot_ids.append(slot_id)
            gs = manager.get(slot_id)
            gs.submit_command_with_ack({
                "name": "act", "args": {"index": 1},
                "nonce": f"archive-{index}", "reset_generation": 0,
            })
            journal_paths.append(os.path.abspath(gs._transaction_log_path))
            assert manager.free(slot_id) is True

        assert manager.get_transaction_archive(slot_ids[0]) is None
        assert not os.path.exists(journal_paths[0])
        assert all(os.path.exists(path) for path in journal_paths[1:])
        with open(manager._transaction_archive_path, encoding="utf-8") as stream:
            assert len(stream.readlines()) == 2

    def test_archive_pruning_never_deletes_active_journal(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        manager = SlotManager()
        manager._MAX_TRANSACTION_ARCHIVES = 1

        old_slot = manager.assign("shared_game")
        old_state = manager.get(old_slot)
        old_state.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "old",
            "reset_generation": 0,
        })
        shared_path = os.path.abspath(old_state._transaction_log_path)
        assert manager.free(old_slot) is True

        active_slot = manager.assign("shared_game")
        assert os.path.abspath(
            manager.get(active_slot)._transaction_log_path,
        ) == shared_path
        other_slot = manager.assign("other_game")
        other_state = manager.get(other_slot)
        other_state.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "other",
            "reset_generation": 0,
        })
        assert manager.free(other_slot) is True

        assert os.path.exists(shared_path)

    def test_later_compaction_retries_prior_failed_cleanup(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        manager = SlotManager()
        manager._MAX_TRANSACTION_ARCHIVES = 1
        paths = []

        first_slot = manager.assign("cleanup_0")
        first_state = manager.get(first_slot)
        first_state.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "cleanup-0",
            "reset_generation": 0,
        })
        paths.append(os.path.abspath(first_state._transaction_log_path))
        assert manager.free(first_slot) is True

        real_compact = manager._compact_transaction_archive_index
        monkeypatch.setattr(
            manager, "_compact_transaction_archive_index", lambda: False,
        )
        second_slot = manager.assign("cleanup_1")
        second_state = manager.get(second_slot)
        second_state.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "cleanup-1",
            "reset_generation": 0,
        })
        assert manager.free(second_slot) is True
        assert os.path.exists(paths[0])

        monkeypatch.setattr(
            manager, "_compact_transaction_archive_index", real_compact,
        )
        third_slot = manager.assign("cleanup_2")
        third_state = manager.get(third_slot)
        third_state.submit_command_with_ack({
            "name": "act", "args": {"index": 1}, "nonce": "cleanup-2",
            "reset_generation": 0,
        })
        assert manager.free(third_slot) is True
        assert not os.path.exists(paths[0])

    def test_story_after_terminal_recovers_loaded_game_context(self):
        gs = GameState()
        gs.submit_command({"name": "load"})
        gs.consume_command()
        gs.push_event({"type": "dialogue", "text": "Before the menu."})
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        assert gs.get_state()["game_terminal"] is True

        gs.push_event({
            "type": "dialogue",
            "character": "Alex",
            "text": "What do you do?",
        })
        state = gs.get_state()

        assert state["status"] == "running"
        assert state["end_reason"] is None
        assert state.get("game_terminal") is not True
        assert state["context"]["context"] == "in_game"
        assert state["context"]["inferred"] == "story_after_terminal"

    def test_request_after_terminal_recovers_loaded_game_context(self):
        gs = GameState()
        gs.submit_command({"name": "load"})
        gs.consume_command()
        gs.push_event({"type": "dialogue", "text": "Loaded dialogue."})
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})

        gs.set_pending_request({
            "type": "choice_request",
            "id": "loaded-choice",
            "choices": ["Enter", "Leave"],
        })
        state = gs.get_state()

        assert state["status"] == "waiting_for_input"
        assert state["end_reason"] is None
        assert state.get("game_terminal") is not True
        assert state["context"]["context"] == "in_game"
        assert state["context"]["inferred"] == "request_after_terminal"

    def test_request_after_terminal_without_load_stays_terminal(self):
        gs = GameState()
        gs.push_event({"type": "dialogue", "text": "The ending."})
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})

        gs.set_pending_request({
            "type": "choice_request",
            "id": "menu-choice",
            "choices": ["New Game", "Quit"],
        })
        state = gs.get_state()

        assert state["game_terminal"] is True
        assert state["end_reason"] == "return_to_menu"

    def test_failed_load_does_not_recover_later_terminal_request(self):
        gs = GameState()
        gs.submit_command({"name": "load"})
        gs.consume_command()
        gs.push_event({
            "type": "command_result",
            "command": "load",
            "success": False,
            "error": "Save slot not found",
        })
        gs.push_event({"type": "dialogue", "text": "The ending."})
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})

        gs.set_pending_request({
            "type": "choice_request",
            "id": "menu-choice",
            "choices": ["New Game", "Quit"],
        })

        assert gs.get_state()["game_terminal"] is True

    def test_command_nonce_not_recorded_when_queue_full(self):
        """A full queue is a retryable failure — the nonce must NOT be
        remembered, so a later retry can still enqueue once the shim drains."""
        gs = GameState()
        for i in range(gs._MAX_PENDING_COMMANDS):
            gs.submit_command({"name": f"cmd{i}"})
        ok, msg = gs.submit_command({"name": "act", "nonce": "Nfull"})
        assert ok is False and "queue is full" in msg
        # Drain one slot, then retry with the same nonce — it must enqueue now.
        gs.consume_command()
        ok2, _ = gs.submit_command({"name": "act", "nonce": "Nfull"})
        assert ok2 is True

    # -- flush re-queue on transient write failure --

    def _install_failing_log_file(self, gs, fail_times):
        """Point gs at a fake log file whose write() raises fail_times, then
        succeeds.  Returns the fake so the test can read what landed."""
        class FailingFile:
            def __init__(self):
                self.remaining_failures = fail_times
                self.written = []

            def write(self, line):
                if self.remaining_failures > 0:
                    self.remaining_failures -= 1
                    raise IOError("simulated transient disk failure")
                self.written.append(line)

            def flush(self):
                pass

            def tell(self):
                return 0

            def close(self):
                pass

        fake = FailingFile()
        gs._open_log = lambda: setattr(gs, "_log_file", fake)  # type: ignore
        return fake

    def test_flush_requeues_unwritten_lines_in_order(self):
        gs = GameState()
        fake = self._install_failing_log_file(gs, fail_times=1)
        gs._log_buffer = ["line1\n", "line2\n"]
        # First write raises -> nothing written; both lines survive in order.
        gs._flush_log()
        assert fake.written == []
        assert gs._log_buffer == ["line1\n", "line2\n"]
        # Next flush succeeds -> lines land in original order, buffer drains.
        gs._flush_log()
        assert fake.written == ["line1\n", "line2\n"]
        assert gs._log_buffer == []

    def test_flush_caps_buffer_when_file_keeps_failing(self):
        gs = GameState()
        gs._MAX_LOG_BUFFER = 3  # shrink the cap for the test
        self._install_failing_log_file(gs, fail_times=10 ** 9)  # always fails
        gs._log_buffer = ["l1\n", "l2\n", "l3\n", "l4\n", "l5\n"]
        gs._flush_log()
        # Bounded: oldest dropped down to the cap, newest kept, noted once.
        assert gs._log_buffer == ["l3\n", "l4\n", "l5\n"]
        assert gs._log_buffer_overflow_noted is True
        # Reset clears both the buffer and the one-shot overflow latch.
        gs.reset()
        assert gs._log_buffer == []
        assert gs._log_buffer_overflow_noted is False

    def test_new_request_supersedes_unconsumed_action_visibly(self):
        """An accepted-but-unconsumed action dropped by a NEW pending
        request must surface as an action_superseded event, not vanish."""
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request",
            "id": "req-1",
            "choices": ["Go"],
        })
        ok, _ = gs.submit_action({"type": "act", "request_id": "req-1", "index": 1})
        assert ok is True
        assert gs.pending_action is not None

        # Shim moves on to a new interaction without consuming the action.
        gs.set_pending_request({
            "type": "choice_request",
            "id": "req-2",
            "choices": ["Stay"],
        })

        assert gs.pending_action is None
        superseded = [e for e in gs.transcript if e.get("type") == "action_superseded"]
        assert len(superseded) == 1
        assert superseded[0]["request_id"] == "req-1"
        assert superseded[0]["new_request_id"] == "req-2"

    def test_enrichment_update_does_not_drop_pending_action(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request",
            "id": "req-1",
            "choices": ["Go"],
        })
        ok, _ = gs.submit_action({"type": "act", "request_id": "req-1", "index": 1})
        assert ok is True

        # Same-id re-push is an enrichment, not a new interaction.
        gs.set_pending_request({
            "type": "choice_request",
            "id": "req-1",
            "choices": [{"id": "go", "label": "Go"}],
        })

        assert gs.pending_action is not None
        assert not [e for e in gs.transcript if e.get("type") == "action_superseded"]

    def test_resubmitting_consumed_request_is_idempotent(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "input_request",
            "id": "name-1",
            "prompt": "What is your name?",
        })
        ok, _ = gs.submit_action({
            "type": "input",
            "request_id": "name-1",
            "text": "Alex",
        })
        assert ok is True
        assert gs.consume_action(request_id="name-1") is not None

        ok, msg = gs.submit_action({
            "type": "input",
            "request_id": "name-1",
            "text": "Alex",
        })

        assert ok is True
        assert msg == "Request name-1 already resolved."

        ok, msg = gs.submit_action({
            "type": "input",
            "request_id": "name-1",
            "text": "Morgan",
        })
        assert ok is False
        assert "different action" in msg

    def test_enriched_choice_id_retry_matches_canonical_pending_action(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request",
            "id": "route-1",
            "choices": [
                {"id": "stay", "label": "Stay"},
                {"id": "leave", "label": "Leave"},
            ],
        })
        action = {
            "type": "act", "request_id": "route-1", "index": "leave",
        }
        assert gs.submit_action(dict(action))[0] is True

        ok, msg = gs.submit_action(dict(action))

        assert ok is True
        assert msg == "Action already submitted."

        assert gs.consume_action(request_id="route-1") is not None
        ok, msg = gs.submit_action(dict(action))
        assert ok is True
        assert msg == "Request route-1 already resolved."


def _play_to_menu(gs):
    """Drive a GameState through gameplay and back to the main menu."""
    gs.push_event({"type": "game_started"})
    gs.push_event({"type": "context", "context": "in_game"})
    gs.push_event({"type": "narration", "text": "Once upon a time..."})
    gs.push_event({"type": "context", "context": "main_menu"})


class TestMenuReturnGate:
    """Per-game end_on_menu_return opt-out: when False, neither the
    bridge's context latch nor a shim game_ended(return_to_menu) may end
    the run (Slay the Princess classifies live gameplay screens as
    main_menu).  quit / process_exit stay always-terminal."""

    def test_gate_off_context_menu_return_not_terminal(self):
        gs = GameState()
        gs.end_on_menu_return = False
        _play_to_menu(gs)
        assert gs.status != "ended"
        assert gs.end_reason is None
        assert gs.current_game_terminal is False
        assert all(ev.get("type") != "game_ended" for ev in gs.transcript)
        assert gs.get_state().get("game_terminal") is not True

    def test_gate_off_keeps_pending_request(self):
        gs = GameState()
        gs.end_on_menu_return = False
        gs.push_event({"type": "game_started"})
        gs.set_pending_request(
            {"type": "choice_request", "id": "c1", "choices": ["Go"]})
        gs.push_event({"type": "context", "context": "main_menu"})
        assert gs.status != "ended"
        assert gs.pending_request is not None

    def test_gate_off_shim_game_ended_menu_return_not_terminal(self):
        gs = GameState()
        gs.end_on_menu_return = False
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "dialogue", "character": "N", "text": "..."})
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        assert gs.status != "ended"
        assert gs.end_reason is None
        assert gs.current_game_terminal is False
        # Gameplay bookkeeping stays coherent for a later opt-in flip.
        assert gs._gameplay_seen is True

    def test_gate_off_quit_still_terminal(self):
        gs = GameState()
        gs.end_on_menu_return = False
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "game_ended", "reason": "quit"})
        assert gs.status == "ended"
        assert gs.end_reason == "quit"

    def test_gate_off_process_exit_still_terminal(self):
        gs = GameState()
        gs.end_on_menu_return = False
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "game_ended", "reason": "process_exit"})
        assert gs.status == "ended"
        assert gs.end_reason == "process_exit"

    def test_gate_on_default_still_latches(self):
        gs = GameState()
        assert gs.end_on_menu_return is True
        _play_to_menu(gs)
        assert gs.status == "ended"
        assert gs.end_reason == "return_to_menu"
        assert gs.current_game_terminal is True

    def test_gate_on_shim_game_ended_menu_return_latches_game_terminal(self):
        # An UNSUPPRESSED shim game_ended(return_to_menu) must set the gated
        # game_terminal flag (not just status), so a reason-aware consumer can
        # trust a menu-return ending EXCLUSIVELY via game_terminal.
        gs = GameState()
        assert gs.end_on_menu_return is True
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "narration", "text": "story"})
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        assert gs.status == "ended"
        assert gs.end_reason == "return_to_menu"
        assert gs.current_game_terminal is True
        state = gs.get_state()
        assert state.get("game_terminal") is True
        assert state.get("end_reason") == "return_to_menu"

    def test_get_state_exposes_end_reason_on_quit(self):
        # The reason-aware poll reads end_reason from GET /state; confirm it is
        # surfaced there (not just on /status) for hard-terminal ends.
        gs = GameState()
        assert gs.get_state()["end_reason"] is None
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "game_ended", "reason": "quit"})
        assert gs.get_state()["end_reason"] == "quit"

    def test_gate_off_then_opt_in_flip_latches_next_menu_return(self):
        gs = GameState()
        gs.end_on_menu_return = False
        _play_to_menu(gs)
        assert gs.status != "ended"
        # Operator flips the gate back on mid-run.
        gs.end_on_menu_return = True
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "narration", "text": "More story."})
        gs.push_event({"type": "context", "context": "main_menu"})
        assert gs.status == "ended"
        assert gs.end_reason == "return_to_menu"

    def test_get_state_config_exposes_gate(self):
        gs = GameState()
        assert gs.get_state()["config"]["end_on_menu_return"] is True
        gs.end_on_menu_return = False
        assert gs.get_state()["config"]["end_on_menu_return"] is False

    def test_reset_preserves_explicit_gate_configuration(self):
        gs = GameState()
        gs.end_on_menu_return = False
        gs.reset()
        assert gs.end_on_menu_return is False


def _play_with_stats(gs, stats=None, inventory=None):
    """Drive a live playthrough that scrapes real stats/inventory."""
    gs.push_event({"type": "game_started"})
    gs.push_event({"type": "context", "context": "in_game"})
    gs.push_event({"type": "narration", "text": "Once upon a time..."})
    gs.push_event({
        "type": "game_state",
        "stats": stats if stats is not None else {"aria_integrity": 42,
                                                  "evidence": 7},
        "inventory": (inventory if inventory is not None
                      else [{"name": "CONVERGENCE.DAT"}]),
        "screen_buttons": [{"label": "Continue"}],
    })


_MENU_SCRAPE = {
    "type": "game_state",
    # Post-menu scrape: Ren'Py reset the store, so these are GAME DEFAULTS.
    "stats": {"aria_integrity": 100, "evidence": 0},
    "inventory": [],
    "screen_buttons": [{"label": "Start"}, {"label": "Load"}, {"label": "Quit"}],
    "interactions": [{"label": "Start"}],
}


class TestPostTerminalProgressFreeze:
    """Once the playthrough-terminal state latches, the game has returned to
    the main menu and Ren'Py has reset its store — later scrapes report game
    DEFAULTS.  Serving those as "final stats" made two playthrough agents file
    false story-continuity bugs ("unearned reset"), so stats/inventory freeze
    at the last pre-terminal snapshot until a new run un-latches the flag."""

    def test_stats_frozen_after_menu_return_latch(self):
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        assert gs.current_game_terminal is True

        gs.push_event(dict(_MENU_SCRAPE))

        served = gs.get_state()["game_state"]
        assert served["stats"] == {"aria_integrity": 42, "evidence": 7}
        assert served["inventory"] == [{"name": "CONVERGENCE.DAT"}]
        assert served["progress_frozen"] is True
        assert served["game_terminal"] is True

    def test_stats_sampling_time_is_frozen_with_progress(self):
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({
            "type": "game_state",
            "stats": {"aria_integrity": 42},
            "inventory": [],
            "_stats_ts": 100.0,
        })
        gs.push_event({"type": "context", "context": "main_menu"})
        menu = dict(_MENU_SCRAPE)
        menu["_stats_ts"] = 200.0
        gs.push_event(menu)

        served = gs.get_game_state()
        assert served["stats"] == {"aria_integrity": 42}
        assert served["_stats_ts"] == 100.0

    def test_frozen_state_does_not_mutate_stored_event(self):
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event(dict(_MENU_SCRAPE))

        gs.get_state()
        # The raw event keeps the honest live scrape (JSONL log parity).
        assert gs.current_game_state["stats"] == {"aria_integrity": 100,
                                                  "evidence": 0}
        assert "progress_frozen" not in gs.current_game_state

    def test_ui_surface_stays_live_after_terminal(self):
        # Only the VALUES freeze: the menu must stay navigable after an ending
        # (return_to_menu deliberately keeps the slot alive).
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event(dict(_MENU_SCRAPE))

        served = gs.get_state()["game_state"]
        assert [b["label"] for b in served["screen_buttons"]] == [
            "Start", "Load", "Quit"]
        assert served["interactions"] == [{"label": "Start"}]

    def test_menu_scrape_before_context_latch_does_not_poison_snapshot(self):
        # The shim pushes screen_content immediately before game_state, while
        # the context event that latches the terminal is rate-limited (<=0.5s
        # later).  A defaults scrape that beats the latch must not be captured.
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "screen_content", "texts": [],
                       "screens": ["main_menu"], "main_menu": True})
        gs.push_event(dict(_MENU_SCRAPE))
        gs.push_event({"type": "context", "context": "main_menu"})

        served = gs.get_state()["game_state"]
        assert served["stats"] == {"aria_integrity": 42, "evidence": 7}
        assert served["progress_frozen"] is True

    def test_get_game_state_accessor_serves_frozen(self):
        # GET /game_state is a separate read path used by act/wait.
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event(dict(_MENU_SCRAPE))

        served = gs.get_game_state()
        assert served["stats"] == {"aria_integrity": 42, "evidence": 7}
        assert served["game_terminal"] is True

    def test_unfreeze_on_new_playthrough_from_menu(self):
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event(dict(_MENU_SCRAPE))
        assert gs.get_state()["game_state"]["progress_frozen"] is True

        # A fresh run begins from the menu -> live state resumes.
        gs.push_event({"type": "context", "context": "in_game"})
        assert gs.current_game_terminal is False
        gs.push_event({
            "type": "game_state",
            "stats": {"aria_integrity": 100, "evidence": 0},
            "inventory": [],
        })
        served = gs.get_state()["game_state"]
        assert served["stats"] == {"aria_integrity": 100, "evidence": 0}
        assert "progress_frozen" not in served
        assert "game_terminal" not in served

    def test_unfreeze_on_game_started(self):
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event(dict(_MENU_SCRAPE))

        gs.push_event({"type": "game_started"})
        assert gs.current_game_terminal is False
        assert gs.get_state().get("game_state") is None
        # Un-latching is what restores live values; the snapshot itself is a
        # cache that outlives the latch on purpose (see
        # test_menu_return_restart_game_started_keeps_snapshot).
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({
            "type": "game_state",
            "stats": {"aria_integrity": 100, "evidence": 0},
            "inventory": [],
        })
        served = gs.get_state()["game_state"]
        assert served["stats"] == {"aria_integrity": 100, "evidence": 0}
        assert "progress_frozen" not in served

    def test_unfreeze_on_load_recovery(self):
        gs = GameState()
        _play_with_stats(gs)
        gs.submit_command({"name": "load"})
        gs.consume_command()
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        assert gs.current_game_terminal is True

        # Restored story proves the episode is live again.
        gs.push_event({"type": "dialogue", "character": "A", "text": "Back."})
        assert gs.current_game_terminal is False
        gs.push_event({
            "type": "game_state",
            "stats": {"aria_integrity": 88},
            "inventory": [],
        })
        served = gs.get_state()["game_state"]
        assert served["stats"] == {"aria_integrity": 88}
        assert "progress_frozen" not in served

    def test_reset_clears_frozen_progress(self):
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.reset()
        assert gs._live_progress is None
        assert gs.current_game_terminal is False

    def test_terminal_without_snapshot_serves_live_state(self):
        # No pre-terminal scrape was ever captured: behave exactly as before
        # (live game_state + the latched game_terminal flag).
        gs = GameState()
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "narration", "text": "The end."})
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event(dict(_MENU_SCRAPE))

        served = gs.get_state()["game_state"]
        assert served["stats"] == {"aria_integrity": 100, "evidence": 0}
        assert served["game_terminal"] is True
        assert "progress_frozen" not in served

    def test_gate_off_game_keeps_live_stats_at_menu_screens(self):
        # end_on_menu_return=False games (Slay the Princess) classify live
        # gameplay screens as main_menu — nothing freezes for them.
        gs = GameState()
        gs.end_on_menu_return = False
        _play_with_stats(gs)
        gs.push_event({"type": "screen_content", "texts": [],
                       "screens": ["main_menu"]})
        gs.push_event({
            "type": "game_state",
            "stats": {"aria_integrity": 12},
            "inventory": [],
        })
        gs.push_event({"type": "context", "context": "main_menu"})

        served = gs.get_state()["game_state"]
        assert served["stats"] == {"aria_integrity": 12}
        assert "progress_frozen" not in served
        assert gs.current_game_terminal is False

    def test_progress_change_terminal_also_freezes(self):
        # Games with a progress mod latch game_terminal from an ending node,
        # before any menu return.
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "progress_change", "node": "ending_aurora",
                       "game_terminal": True})
        gs.push_event(dict(_MENU_SCRAPE))

        served = gs.get_state()["game_state"]
        assert served["stats"] == {"aria_integrity": 42, "evidence": 7}
        assert served["progress_frozen"] is True

    @pytest.mark.parametrize("reason", ["load", "rollback"])
    def test_native_resume_clears_progress_terminal_latch(self, reason):
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "progress_change", "node": "ending_aurora",
                       "game_terminal": True})
        gs.push_event(dict(_MENU_SCRAPE))
        assert gs.status == "running"
        assert gs.get_game_state()["progress_frozen"] is True

        gs.pending_request = {"id": "abandoned-choice"}
        gs.pending_action = {"choice": 1}
        gs.pending_commands.append({"name": "abandoned-command"})
        gs.push_event({"type": "game_resumed", "reason": reason})

        assert gs.status == "running"
        assert gs.current_game_terminal is False
        assert gs.current_screen is None
        assert gs.current_game_state is None
        assert gs.pending_request is None
        assert gs.pending_action is None
        assert not gs.pending_commands
        assert gs.current_context["context"] == "in_game"
        assert gs.current_context["inferred"] == f"{reason}_resume"

        gs.push_event({
            "type": "game_state",
            "stats": {"aria_integrity": 73, "evidence": 4},
            "inventory": [{"name": "restored"}],
        })
        served = gs.get_game_state()
        assert served["stats"] == {"aria_integrity": 73, "evidence": 4}
        assert served["inventory"] == [{"name": "restored"}]
        assert "progress_frozen" not in served
        assert "game_terminal" not in served


class TestMenuReturnRestartKeepsFrozenProgress:
    """Live repro (Echoes of Tomorrow, 2026-08-14): the ENDING text and the
    menu NAVIGATION block rendered correctly, but the stats footer on the same
    render read the store-RESET defaults.

    Ren'Py fires config.start_callbacks — which the shim reports as
    game_started — as part of the return-to-menu RESTART, before the menu
    context re-latches the terminal.  game_started un-latches, and it used to
    drop the frozen snapshot with it, so the re-latched terminal had nothing
    left to serve and fell back to the live (defaults) scrape."""

    def _ended_run(self):
        gs = GameState()
        _play_with_stats(gs)
        # The progress mod latches the ending node while still in-game.
        gs.push_event({"type": "progress_change", "node": "ending_on_faith",
                       "game_terminal": True})
        return gs

    def test_game_started_restart_then_menu_latch_keeps_frozen_stats(self):
        gs = self._ended_run()
        # Ren'Py restarts into the menu: start_callbacks first, menu second.
        gs.push_event({"type": "game_started"})
        assert gs.current_game_terminal is False  # transiently un-latched
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event({"type": "screen_content", "texts": [],
                       "screens": ["menu"]})
        gs.push_event(dict(_MENU_SCRAPE))

        served = gs.get_state()["game_state"]
        assert gs.current_game_terminal is True
        assert served["stats"] == {"aria_integrity": 42, "evidence": 7}
        assert served["inventory"] == [{"name": "CONVERGENCE.DAT"}]
        assert served["progress_frozen"] is True
        # ...while the menu stays navigable.
        assert served["interactions"] == [{"label": "Start"}]

    def test_defaults_scrape_between_restart_and_latch_is_not_captured(self):
        # Same restart, but the reset-store scrape beats the menu context
        # event (which the shim rate-limits to 0.5s).  Capture is suspended
        # from game_started until gameplay is re-confirmed.
        gs = self._ended_run()
        gs.push_event({"type": "game_started"})
        gs.push_event(dict(_MENU_SCRAPE))
        gs.push_event({"type": "context", "context": "main_menu"})

        served = gs.get_game_state()
        assert served["stats"] == {"aria_integrity": 42, "evidence": 7}
        assert served["progress_frozen"] is True

    def test_menu_guard_recognizes_custom_menu_before_context_event(self):
        # Echoes' main menu screen is tagged `menu`. Use the store-derived bit
        # before the shim's rate-limited context event catches up, so a reset
        # scrape is already post-terminal. The lifecycle policy remains gated
        # on end_on_menu_return.
        gs = GameState()
        gs.current_screen = {"type": "screen_content", "screens": ["menu"],
                             "main_menu": True}
        assert gs._at_menu_screen_locked() is True
        gs.end_on_menu_return = False
        assert gs._at_menu_screen_locked() is False

    def test_in_game_generic_menu_does_not_freeze_live_progress(self):
        gs = GameState()
        _play_with_stats(gs, stats={"hp": 4})
        gs.push_event({"type": "screen_content", "screens": ["menu"],
                       "main_menu": False})
        gs.push_event({"type": "game_state", "stats": {"hp": 3},
                       "inventory": []})

        assert gs._live_progress["stats"] == {"hp": 3}

    def test_in_game_generic_menu_does_not_tag_terminal_sequence_stats(self):
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "progress_change", "node": "ending",
                       "game_terminal": True})
        gs.push_event({"type": "screen_content", "screens": ["menu"],
                       "main_menu": False})
        gs.push_event({"type": "stats_update", "changed": {"evidence": 8},
                       "previous": {"evidence": 7}})

        assert "post_terminal" not in gs.get_transcript(last_n=1)[0]

    def test_game_resumed_after_restart_serves_live_values_again(self):
        # Recovery contract (native load / rollback out of an ending) is
        # unchanged: un-latching is what restores LIVE values, and the next
        # scrape is authoritative.
        gs = self._ended_run()
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event(dict(_MENU_SCRAPE))
        assert gs.get_game_state()["progress_frozen"] is True

        gs.push_event({"type": "game_resumed", "reason": "load"})
        assert gs.current_game_terminal is False
        gs.push_event({
            "type": "game_state",
            "stats": {"aria_integrity": 73, "evidence": 4},
            "inventory": [{"name": "restored"}],
        })
        served = gs.get_game_state()
        assert served["stats"] == {"aria_integrity": 73, "evidence": 4}
        assert "progress_frozen" not in served
        assert "game_terminal" not in served

    def test_new_run_replaces_the_surviving_snapshot(self):
        # The snapshot outlives the latch, so pin that a genuinely new run
        # cannot serve the PREVIOUS run's values at its own ending.
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        assert gs.get_game_state()["progress_frozen"] is True

        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "narration", "text": "Again, then."})
        gs.push_event({
            "type": "game_state",
            "stats": {"aria_integrity": 9, "evidence": 2},
            "inventory": [{"name": "second run"}],
        })
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event(dict(_MENU_SCRAPE))

        served = gs.get_game_state()
        assert served["stats"] == {"aria_integrity": 9, "evidence": 2}
        assert served["inventory"] == [{"name": "second run"}]
        assert served["progress_frozen"] is True

    def test_main_menu_hides_the_frozen_pre_terminal_summary(self):
        # The frozen snapshot remains available to run-history consumers, but
        # it is historical rather than live once the main menu is visible.
        from vnflight.format import build_state_data

        gs = GameState()
        _play_with_stats(gs, stats={
            "_summary": "Location: Habitat Module | Evidence: 14 | ARIA: 59%",
            "evidence": 14,
        })
        gs.push_event({"type": "progress_change", "node": "ending_on_faith",
                       "game_terminal": True})
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event({
            "type": "game_state",
            "stats": {"_summary": "Location: Lab | Evidence: 0 | ARIA: 100%",
                      "evidence": 0},
            "inventory": [],
            "screen_buttons": [{"label": "Start"}],
        })

        data = build_state_data(gs.get_state())
        assert "_stats_summary" not in data
        assert "stats" not in data

    def test_reset_drops_the_snapshot(self):
        gs = self._ended_run()
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.reset()
        assert gs._live_progress is None
        assert gs._progress_capture_suspended is False
        assert gs.current_game_terminal is False

    def test_post_terminal_stats_scrape_is_tagged_not_dropped(self):
        # The reset store re-emits every stat as a "change" back to default.
        # The transcript keeps the honest event; the tag lets formatters skip
        # the phantom deltas.
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        gs.push_event({"type": "stats_update",
                       "changed": {"evidence": 0},
                       "previous": {"evidence": 7}})
        gs.push_event({"type": "inventory_update", "items": []})

        tail = gs.get_transcript(last_n=2)
        assert [e["type"] for e in tail] == ["stats_update", "inventory_update"]
        assert all(e["post_terminal"] is True for e in tail)

    def test_menu_scrape_tags_reset_before_context_reports_ended(self):
        # The screen scrape can beat the rate-limited context push back to the
        # bridge. Once a terminal node and the actual title menu are both
        # visible, the reset store deltas are post-terminal even while status
        # still says running.
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "progress_change", "node": "ending_on_faith",
                       "game_terminal": True})
        # Echoes uses a custom screen literally named ``menu``; the store
        # context that confirms main_menu can arrive after this scrape.
        gs.push_event({"type": "screen_content", "screens": ["menu"],
                       "main_menu": True,
                       "buttons": [{"label": "Start"}]})
        gs.push_event({"type": "stats_update",
                       "changed": {"evidence": 0},
                       "previous": {"evidence": 7}})

        event = gs.get_transcript(last_n=1)[0]
        assert gs.status != "ended"
        assert event["post_terminal"] is True

    def test_stats_change_during_the_ending_sequence_is_not_tagged(self):
        # Gated on status=="ended" (the menu return), not the bare terminal
        # latch, so an ending SEQUENCE that still moves stats renders them.
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "progress_change", "node": "ending_on_faith",
                       "game_terminal": True})
        gs.push_event({"type": "stats_update",
                       "changed": {"evidence": 15},
                       "previous": {"evidence": 14}})

        assert "post_terminal" not in gs.get_transcript(last_n=1)[0]


class TestSuppressedGameEndedAnnotation:
    """A suppressed shim game_ended(return_to_menu) stays in the transcript
    (the shim did report it) but is ANNOTATED terminal=False so downstream
    terminal derivations that key off event presence don't false-fire.
    Convention: absence of the annotation means terminal."""

    @staticmethod
    def _game_ended(gs):
        return [e for e in gs.transcript if e.get("type") == "game_ended"]

    def test_suppressed_shim_game_ended_is_annotated_non_terminal(self):
        gs = GameState()
        gs.end_on_menu_return = False
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "narration", "text": "story"})
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        events = self._game_ended(gs)
        assert len(events) == 1
        assert events[0]["terminal"] is False
        assert events[0]["suppressed"] == "end_on_menu_return"
        # Not an ending: status/latch untouched.
        assert gs.status != "ended"
        assert gs.get_state().get("game_terminal") is not True

    def test_suppressed_annotation_persisted_to_state_and_log(self, tmp_path,
                                                              monkeypatch):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.end_on_menu_return = False
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "narration", "text": "story"})
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        # Visible via get_state (a consumer scanning the transcript batch).
        state = gs.get_state(since=0)
        ended = [e for e in state["transcript"] if e.get("type") == "game_ended"]
        assert ended and ended[0]["terminal"] is False
        # And durably in the JSONL.
        logs = list((tmp_path / "bridge" / "logs").glob("playthrough_*.jsonl"))
        assert logs
        lines = [json.loads(l) for l in
                 logs[0].read_text(encoding="utf-8").splitlines()]
        logged = [e for e in lines if e.get("type") == "game_ended"]
        assert logged and logged[0]["suppressed"] == "end_on_menu_return"

    def test_quit_game_ended_is_not_annotated(self):
        gs = GameState()
        gs.end_on_menu_return = False
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "game_ended", "reason": "quit"})
        events = self._game_ended(gs)
        assert len(events) == 1
        # Absence of the annotation == terminal (backward compatible).
        assert "terminal" not in events[0]
        assert "suppressed" not in events[0]
        assert gs.status == "ended"

    def test_unsuppressed_menu_return_game_ended_is_not_annotated(self):
        gs = GameState()  # gate on (default)
        _play_to_menu(gs)
        events = self._game_ended(gs)
        assert len(events) == 1
        assert "terminal" not in events[0]
        assert "suppressed" not in events[0]
        assert gs.status == "ended"
        assert gs.current_game_terminal is True


class TestTerminalEvidence:
    """terminal_evidence: a compact transcript snapshot emitted when the
    menu-return terminal fires — or is suppressed by the opt-out — so
    the next false positive self-documents what the menu-classified
    screen actually contained."""

    @staticmethod
    def _evidence(gs):
        return [e for e in gs.transcript if e.get("type") == "terminal_evidence"]

    def test_evidence_on_fire_context_source(self):
        gs = GameState()
        _play_to_menu(gs)
        evidence = self._evidence(gs)
        assert len(evidence) == 1
        ev = evidence[0]
        assert ev["reason"] == "return_to_menu"
        assert ev["suppressed"] is False
        assert ev["source"] == "context"
        assert "snapshot" in ev
        # Evidence precedes the synthesized game_ended in the transcript.
        types = [e.get("type") for e in gs.transcript]
        assert types.index("terminal_evidence") < types.index("game_ended")

    def test_evidence_on_suppress_context_source(self):
        gs = GameState()
        gs.end_on_menu_return = False
        _play_to_menu(gs)
        evidence = self._evidence(gs)
        assert len(evidence) == 1
        assert evidence[0]["suppressed"] is True
        assert evidence[0]["source"] == "context"

    def test_evidence_on_suppress_game_ended_source(self):
        gs = GameState()
        gs.end_on_menu_return = False
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "narration", "text": "story"})
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        evidence = self._evidence(gs)
        assert len(evidence) == 1
        assert evidence[0]["suppressed"] is True
        assert evidence[0]["source"] == "game_ended"

    def test_evidence_on_fire_game_ended_source(self):
        # Shim-detected menu return without the bridge's own context
        # latch (bridge saw no story content, but a choice request did).
        gs = GameState()
        gs.push_event({"type": "game_started"})
        gs.set_pending_request(
            {"type": "choice_request", "id": "c1", "choices": ["x"]})
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        evidence = self._evidence(gs)
        assert len(evidence) == 1
        assert evidence[0]["suppressed"] is False
        assert evidence[0]["source"] == "game_ended"
        assert gs.status == "ended"

    def test_no_evidence_for_quit(self):
        gs = GameState()
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "game_ended", "reason": "quit"})
        assert self._evidence(gs) == []

    def test_evidence_snapshot_contents(self):
        gs = GameState()
        gs.push_event({"type": "game_started"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "narration", "text": "First line."})
        gs.push_event({"type": "dialogue", "character": "Hero",
                       "text": "Second line."})
        gs.push_event({"type": "narration", "text": "Third line."})
        gs.push_event({"type": "narration", "text": "Fourth line."})
        gs.push_event({
            "type": "screen_content",
            "texts": ["Chapter III"],
            "screens": ["main_menu", "ctc"],
            "buttons": [{"label": f"Button {i}"} for i in range(25)],
        })
        gs.push_event({
            "type": "game_state",
            "interactions": [{"label": "Continue"}, {"label": "Investigate"}],
            "screen_buttons": [],
        })
        gs.push_event({"type": "context", "context": "main_menu"})
        ev = self._evidence(gs)[0]
        snap = ev["snapshot"]
        assert snap["screens"] == ["main_menu", "ctc"]
        # Button labels present, capped at 20.
        assert snap["buttons"][0] == "Button 0"
        assert len(snap["buttons"]) == 20
        assert snap["interactions"] == ["Continue", "Investigate"]
        # Last 2-3 story texts, in order, speaker-prefixed for dialogue.
        assert snap["story_tail"] == [
            "Hero: Second line.", "Third line.", "Fourth line."]
        # Compact: no screenshots/base64.
        assert "image" not in snap
        assert "screenshot" not in snap

    def test_evidence_deduped_context_then_shim_game_ended(self):
        # The shim pushes context(main_menu) and THEN its own
        # game_ended(return_to_menu) — one episode, one evidence event.
        gs = GameState()
        _play_to_menu(gs)
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        assert len(self._evidence(gs)) == 1

    def test_evidence_new_episode_after_story_resumes(self):
        gs = GameState()
        gs.end_on_menu_return = False
        _play_to_menu(gs)
        assert len(self._evidence(gs)) == 1
        # Gameplay continues (the false menu was mid-game), then the
        # misclassification fires again — the new episode re-documents.
        gs.push_event({"type": "context", "context": "in_game"})
        gs.push_event({"type": "narration", "text": "still playing"})
        gs.push_event({"type": "context", "context": "main_menu"})
        assert len(self._evidence(gs)) == 2

    def test_evidence_seq_monotonic_and_counter_coherent(self):
        gs = GameState()
        _play_to_menu(gs)
        gs.push_event({"type": "narration", "text": "epilogue?"})
        seqs = [e["_seq"] for e in gs.transcript]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))
        assert gs.event_counter == max(seqs)
        # since= paging still sees the synthesized events.
        state = gs.get_state(since=0)
        assert any(e["type"] == "terminal_evidence"
                   for e in state["transcript"])

    def test_evidence_written_to_playthrough_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        _play_to_menu(gs)
        logs = list((tmp_path / "bridge" / "logs").glob("playthrough_*.jsonl"))
        assert logs, "playthrough JSONL should exist"
        lines = [json.loads(l) for l in
                 logs[0].read_text(encoding="utf-8").splitlines()]
        evidence = [e for e in lines if e.get("type") == "terminal_evidence"]
        assert len(evidence) == 1
        assert evidence[0]["snapshot"] is not None


class TestOffLockLogWriter:
    """The JSONL flush must run OUTSIDE the state lock so a stalled disk /
    AV scan can't block command traffic (the act-stall saturation root
    cause).  Ordering + seq numbering must still be preserved."""

    def test_push_event_seq_ordering_under_concurrency(self, tmp_path, monkeypatch):
        import threading

        monkeypatch.chdir(tmp_path)
        gs = GameState()

        n_threads = 8
        per_thread = 50
        barrier = threading.Barrier(n_threads)

        def worker(tid):
            barrier.wait()
            for i in range(per_thread):
                gs.push_event({"type": "dialogue",
                               "text": "t{}-{}".format(tid, i)})

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = n_threads * per_thread
        # Counter + in-memory seqs: unique and contiguous 1..N.
        assert gs.event_counter == total
        seqs = [e["_seq"] for e in gs.transcript]
        assert seqs == list(range(1, total + 1))

        # On-disk order matches seq order (FIFO drain under the write lock).
        logs = list((tmp_path / "bridge" / "logs").glob("playthrough_*.jsonl"))
        assert logs, "playthrough JSONL should exist"
        lines = [json.loads(l) for l in
                 logs[0].read_text(encoding="utf-8").splitlines()]
        file_seqs = [e["_seq"] for e in lines if "_seq" in e]
        assert file_seqs == sorted(file_seqs)
        assert file_seqs == list(range(1, total + 1))

    def test_slow_log_write_does_not_hold_state_lock(self):
        import threading
        import time

        gs = GameState()

        write_started = threading.Event()

        class _SlowFile:
            def __init__(self):
                self.lines = []

            def write(self, s):
                write_started.set()
                time.sleep(1.0)  # simulate a stalled disk / AV scan
                self.lines.append(s)

            def flush(self):
                pass

            def tell(self):
                return 0

            def close(self):
                pass

        # Pre-open a slow log file so _open_log() no-ops and the slow write
        # runs inside _flush_log (with the state lock released).
        gs._log_file = _SlowFile()

        def pusher():
            gs.push_event({"type": "dialogue", "text": "slow"})

        t = threading.Thread(target=pusher)
        t.start()

        # Wait until the (slow) write is in progress, then time a state read.
        assert write_started.wait(timeout=2.0)
        start = time.perf_counter()
        state = gs.get_state(since=0)
        elapsed = time.perf_counter() - start

        # The read must NOT be blocked by the 1.0s write.
        assert elapsed < 0.3, (
            "get_state blocked for {:.3f}s — state lock held during file write"
            .format(elapsed))
        assert state is not None
        t.join()

    def test_reset_during_flush_does_not_write_after_close(
            self, tmp_path, monkeypatch):
        """reset() must take _log_write_lock before _lock so it can't close
        the log out from under an in-flight off-lock flush (write-after-close
        would swallow the diagnostic line).  Lock order: _log_write_lock ->
        _lock everywhere, matching _flush_log."""
        import threading
        import time

        monkeypatch.chdir(tmp_path)
        gs = GameState()

        write_started = threading.Event()
        write_after_close = []

        class _SlowFile:
            def __init__(self):
                self.lines = []
                self.closed = False

            def write(self, s):
                write_started.set()
                time.sleep(0.5)  # simulate a stalled disk / AV scan
                if self.closed:
                    write_after_close.append(s)
                self.lines.append(s)

            def flush(self):
                pass

            def tell(self):
                return 0

            def close(self):
                self.closed = True

        stub = _SlowFile()
        gs._log_file = stub
        # Queue a line so the flush actually performs a (slow) write.
        gs._log_buffer.append('{"queued": 1}\n')

        errors = []

        def flusher():
            try:
                gs._flush_log()
            except Exception as e:  # pragma: no cover - failure path
                errors.append(("flush", e))

        def resetter():
            try:
                gs.reset()
            except Exception as e:  # pragma: no cover - failure path
                errors.append(("reset", e))

        ft = threading.Thread(target=flusher)
        ft.start()

        # Once the slow write is in progress, reset concurrently.  With the
        # fix, reset blocks on _log_write_lock until the flush completes.
        assert write_started.wait(timeout=2.0)
        rt = threading.Thread(target=resetter)
        rt.start()

        ft.join(timeout=5.0)
        rt.join(timeout=5.0)
        assert not ft.is_alive() and not rt.is_alive(), "threads deadlocked"

        assert not errors, "flush/reset raised: {}".format(errors)
        assert not write_after_close, (
            "reset closed the log during an active flush — wrote after close")
        assert stub.closed, "reset should have closed the old log handle"
        assert stub.lines == ['{"queued": 1}\n']

        # Writer remains usable after reset: push_event -> flush lands in a
        # fresh real log file.
        gs.push_event({"type": "dialogue", "text": "after-reset"})
        logs = list((tmp_path / "bridge" / "logs").glob("playthrough_*.jsonl"))
        assert logs, "a fresh playthrough JSONL should exist after reset"
        content = "\n".join(p.read_text(encoding="utf-8") for p in logs)
        assert "after-reset" in content


class TestSlotManager:
    """Tests for SlotManager — multi-game slot management."""

    def test_creation(self):
        sm = SlotManager(max_slots=4)
        assert sm.max_slots == 4
        assert len(sm.slots) == 0

    def test_registration_rejections_are_bounded_and_expire(self):
        sm = SlotManager()
        sm._MAX_REGISTRATION_REJECTIONS = 2
        sm.record_registration_rejection(
            "token-a", reason="mismatch", message="a",
        )
        sm.record_registration_rejection(
            "token-b", reason="mismatch", message="b",
        )
        sm.record_registration_rejection(
            "token-c", reason="mismatch", message="c",
        )
        assert sm.registration_rejection("token-a") is None
        assert sm.registration_rejection("token-b")["message"] == "b"

        token_hash = sm._token_hash("token-b")
        token_b_record = next(
            record for record in sm._registration_rejections.values()
            if record.get("_token_hash") == token_hash
        )
        token_b_record["rejected_at"] = (
            time.time() - sm._REGISTRATION_REJECTION_TTL - 1
        )
        assert sm.registration_rejection("token-b") is None

    def test_registration_rejections_are_bounded_per_token(self, monkeypatch):
        from vnflight import bridge

        # A coarse clock (Windows on Python 3.10 ticks every ~16 ms) stamps
        # all three rejections with the same time; order must still be the
        # order they were recorded in, newest first.
        monkeypatch.setattr(bridge.time, "time", lambda: 100.0)
        sm = SlotManager()
        sm._MAX_REGISTRATION_REJECTIONS_PER_TOKEN = 2
        for pid in (1, 2, 3):
            sm.record_registration_rejection(
                "shared-token",
                reason="reservation_conflict",
                message=str(pid),
                game_id="roadwarden",
                game_pid=pid,
            )

        assert [
            record["message"]
            for record in sm.registration_rejections("shared-token")
        ] == ["3", "2"]

    def test_finite_rejection_promotion_preserves_launch_provenance(
            self, monkeypatch):
        from vnflight import bridge

        clock = {"now": 100.0}
        monkeypatch.setattr(bridge.time, "time", lambda: clock["now"])
        sm = SlotManager()
        sm.record_registration_rejection(
            "shared-token",
            reason="reservation_conflict",
            message="reserved elsewhere",
            game_id="roadwarden",
            transient=True,
            retry_mode="finite",
            retry_until=101.0,
        )

        clock["now"] = 200.0
        promoted = sm.registration_rejection("shared-token")
        assert promoted["transient"] is False
        assert promoted["rejected_at"] == 100.0
        assert promoted["promoted_at"] == 200.0
        assert next(iter(sm._registration_rejections.values()))[
            "rejected_at"
        ] == 100.0
        assert sm.registration_rejection(
            "shared-token",
            game_id="roadwarden",
            launch_id="new-launch",
            launch_started_at=150.0,
        ) is None

    def test_assign_slot(self):
        sm = SlotManager()
        slot_id = sm.assign("mystic_cafe")
        assert slot_id is not None
        assert slot_id in sm.slots

    def test_assign_returns_incremental_ids(self):
        sm = SlotManager()
        id1 = sm.assign("game_a")
        id2 = sm.assign("game_b")
        assert id1 != id2
        assert isinstance(id1, int)
        assert isinstance(id2, int)

    def test_max_slots_enforced(self):
        sm = SlotManager(max_slots=2)
        sm.assign("game_a")
        sm.assign("game_b")
        result = sm.assign("game_c")
        assert result is None  # Should fail — max reached.

    def test_free_slot(self):
        sm = SlotManager()
        slot_id = sm.assign("test")
        assert slot_id in sm.slots
        sm.free(slot_id)
        assert slot_id not in sm.slots

    def test_free_opens_space(self):
        sm = SlotManager(max_slots=1)
        id1 = sm.assign("game_a")
        sm.free(id1)
        id2 = sm.assign("game_b")
        assert id2 is not None

    def test_can_free_without_admin_allows_ended_slots(self):
        sm = SlotManager()
        slot_id = sm.assign("game_a", game_pid=os.getpid())
        sm.slots[slot_id].status = "ended"

        assert sm.can_free_without_admin(slot_id) is True

    def test_can_free_without_admin_allows_dead_pid_slots(self):
        sm = SlotManager()
        slot_id = sm.assign("game_a", game_pid=2**31 - 1)

        assert sm.can_free_without_admin(slot_id) is True

    def test_can_free_without_admin_rejects_live_active_slots(self):
        sm = SlotManager()
        slot_id = sm.assign("game_a", game_pid=os.getpid())

        assert sm.can_free_without_admin(slot_id) is False

    def test_can_free_without_admin_rejects_ambiguous_game_id(self):
        sm = SlotManager()
        sm.assign("mystic_cafe", game_pid=111)
        sm.assign("mystic_cafe", game_pid=222)

        assert sm.can_free_without_admin("mystic_cafe") is False

    def test_multiple_assigns(self):
        sm = SlotManager()
        id1 = sm.assign("mystic_cafe")
        id2 = sm.assign("roadwarden")
        assert id1 is not None
        assert id2 is not None
        assert id1 != id2

    def test_duplicate_game_id_slots_require_numeric_hint(self):
        sm = SlotManager()
        id1 = sm.assign("mystic_cafe", game_pid=111)
        id2 = sm.assign("mystic_cafe", game_pid=222)

        assert id1 is not None
        assert id2 is not None
        assert id1 != id2
        assert sm.get("mystic_cafe") is None
        assert sm.get_slot_id("mystic_cafe") is None

        reserve = sm.reserve("mystic_cafe", token="stable-token")
        assert reserve["status"] == "ambiguous"
        assert reserve["matches"] == [id1, id2]
        assert "Use a numeric slot id" in reserve["error"]

        numeric = sm.reserve(str(id1), token="stable-token")
        assert numeric["slot_id"] == id1
        assert numeric["token"] == "stable-token"

    def test_case_variant_duplicate_game_id_slots_require_numeric_hint(self):
        sm = SlotManager()
        id1 = sm.assign("Mystic_Cafe", game_pid=111)
        id2 = sm.assign("mystic_cafe", game_pid=222)

        assert id1 is not None
        assert id2 is not None
        assert sm.get("MYSTIC_CAFE") is None
        assert sm.get_slot_id("mystic_cafe") is None

        reserve = sm.reserve("Mystic_Cafe", token="stable-token")
        assert reserve["status"] == "ambiguous"
        assert reserve["matches"] == [id1, id2]

    def test_free_duplicate_game_slot_preserves_survivor_mapping(self):
        sm = SlotManager()
        id1 = sm.assign("mystic_cafe", game_pid=111)
        id2 = sm.assign("mystic_cafe", game_pid=222)

        assert id1 is not None
        assert id2 is not None
        assert sm.free(id1) is True

        assert sm.get_slot_id("mystic_cafe") == id2
        assert sm.get("mystic_cafe") is sm.slots[id2]

    def test_list_slots(self):
        sm = SlotManager()
        sm.assign("game_a")
        sm.assign("game_b")
        slots = sm.list_slots()
        assert len(slots) == 2

    def test_list_slots_exposes_successful_registration_provenance(self):
        sm = SlotManager()
        assigned = sm.assign_reserved(
            "game_a", game_pid=1234, token="stable-token",
            launch_id="launch-current",
        )

        slot = next(
            item for item in sm.list_slots()
            if item["slot_id"] == assigned["slot_id"]
        )
        assert slot["launch_id"] == "launch-current"
        assert slot["registered_at"] > 0

    def test_reserve_is_idempotent_with_same_token(self):
        sm = SlotManager()
        slot_id = sm.assign("game_a")
        first = sm.reserve(str(slot_id), token="stable-token")
        second = sm.reserve(str(slot_id), token="stable-token")

        assert first["token"] == "stable-token"
        assert second["token"] == "stable-token"
        assert second["slot_id"] == slot_id

    def test_reserve_rejects_different_token_when_reserved(self):
        sm = SlotManager()
        slot_id = sm.assign("game_a")
        sm.reserve(str(slot_id), token="stable-token")
        second = sm.reserve(str(slot_id), token="other-token")

        assert second["status"] == "reserved"
        assert "already reserved" in second["error"]

    def test_assign_reserved_does_not_adopt_reused_pid_from_other_game(self):
        sm = SlotManager()
        first = sm.assign_reserved("game_a", 1234, "token-a")
        second = sm.assign_reserved("game_b", 1234, "token-b")

        assert first["slot_id"] != second["slot_id"]
        assert sm.slot_to_game[first["slot_id"]] == "game_a"
        assert sm.slot_to_game[second["slot_id"]] == "game_b"
        assert sm.check_token(first["slot_id"], "token-a") is True
        assert sm.check_token(second["slot_id"], "token-b") is True

    def test_default_max_slots(self):
        sm = SlotManager()
        assert sm.max_slots == 16  # Updated default.

    # -- token semantics --

    def test_check_token_admin_and_slot_tokens(self):
        sm = SlotManager(admin_token="admin-secret")
        slot_id = sm.assign("game_a")
        sm.reserve(str(slot_id), token="slot-secret")

        assert sm.check_token(slot_id, "admin-secret") is True
        assert sm.check_token(slot_id, "slot-secret") is True
        assert sm.check_token(slot_id, "wrong") is False
        assert sm.check_token(slot_id, None) is False

    def test_check_token_unreserved_slot_open_unless_required(self):
        sm = SlotManager(admin_token="admin-secret")
        slot_id = sm.assign("game_a")
        assert sm.check_token(slot_id, None) is True

        strict = SlotManager(admin_token="admin-secret", require_token=True)
        strict_id = strict.assign("game_a")
        assert strict.check_token(strict_id, None) is False
        assert strict.check_token(strict_id, "admin-secret") is True

    def test_reserve_force_replaces_existing_reservation(self):
        sm = SlotManager(admin_token="admin-secret")
        slot_id = sm.assign("game_a")
        sm.reserve(str(slot_id), token="old-token")

        result = sm.reserve(str(slot_id), token="new-token", force=True)

        assert result["token"] == "new-token"
        assert sm.check_token(slot_id, "new-token") is True
        assert sm.check_token(slot_id, "old-token") is False

    def test_reserve_without_force_keeps_existing_reservation(self):
        sm = SlotManager(admin_token="admin-secret")
        slot_id = sm.assign("game_a")
        sm.reserve(str(slot_id), token="old-token")

        result = sm.reserve(str(slot_id), token="new-token")

        assert result.get("status") == "reserved"
        assert sm.check_token(slot_id, "old-token") is True


class TestPidWatchdog:
    """Tests for PID-based dead slot reaping."""

    def test_pid_alive_self(self):
        sm = SlotManager()
        # Our own PID is always alive.
        assert sm._pid_alive(os.getpid()) is True

    def test_pid_alive_nonexistent(self):
        sm = SlotManager()
        # PID 0 / max int range — should be reported dead.
        assert sm._pid_alive(2**31 - 1) is False

    def test_reap_dead_slots_keeps_live(self):
        sm = SlotManager()
        slot_id = sm.assign("test_game", game_pid=os.getpid())
        dead = sm.reap_dead_slots()
        assert dead == []
        assert slot_id in sm.slots

    def test_reap_dead_slots_removes_dead(self):
        sm = SlotManager()
        slot_id = sm.assign("test_game", game_pid=2**31 - 1)
        dead = sm.reap_dead_slots()
        assert slot_id in dead
        assert slot_id not in sm.slots

    def test_reap_skips_slots_without_pid(self):
        sm = SlotManager()
        # Slot without game_pid — watchdog shouldn't touch it.
        slot_id = sm.assign("test_game")
        dead = sm.reap_dead_slots()
        assert dead == []
        assert slot_id in sm.slots


class TestLaunchSplashMenuDoesNotLatchTerminal:
    """Live repro (Echoes of Tomorrow, three parallel runs, 2026-08-14):
    EVERY bridge latched game_terminal a few seconds after launch, before the
    agent had played a single beat.

    Echoes' splashscreen runs inside a normal game context — Ren'Py reports
    ``context: in_game`` while the "Aethon Systems presents" card plays — and
    then drops to the title menu.  To the shim that is indistinguishable from
    an end-of-run menu return, so it emitted ``game_ended reason=return_to_menu``
    at launch.

    The bridge's CONTEXT-driven menu latch already guarded this with
    _gameplay_seen ("the launch-time menu never trips it"), but the shim's own
    game_ended path did not, so the guard was bypassed.  The latch self-cleared
    on the next in_game context, which is exactly what made it dangerous: a
    transient True edge that anything mirroring the flag could catch and keep.

    Fixture is the real event sequence, bridge log
    ``bridge/logs/playthrough_20260814_204853.jsonl`` _seq 1..46 (trimmed to
    the events that drive state)."""

    # _seq 1..46 of the live log, verbatim in shape.
    _LAUNCH_SEQUENCE = [
        {"type": "mod_loaded", "pid": 55360},                          # _seq 1
        {"type": "game_started", "game_name": "Echoes of Tomorrow"},   # _seq 2
        {"type": "scene", "layer": None},                              # _seq 3
        {"type": "show", "name": "black"},                             # _seq 4
        # Splashscreen plays INSIDE a game context...
        {"type": "context", "context": "in_game",                      # _seq 9
         "available_commands": ["save", "load", "quit"]},
        {"type": "game_state", "screen_buttons": [],                   # _seq 10
         "stats": {"signal_strength": 80, "aria_integrity": 100}},
        {"type": "progress_change", "from": None, "to": "start",       # _seq 13
         "label": "Game Start", "terminal": False, "game_terminal": False},
        {"type": "show",                                               # _seq 19
         "name": 'text "{size=+10}Aethon Systems presents{/size}"'},
        {"type": "pause", "delay": 2.0},                               # _seq 20
        {"type": "hide", "name": "text"},                              # _seq 24
        # ...and then falls through to the title menu.
        {"type": "screen_content",                                     # _seq 28
         "texts": ["Echoes of Tomorrow", "A signal from the future"],
         "screens": ["menu"],
         "buttons": [{"label": "Start"}, {"label": "Load"},
                     {"label": "Quit"}]},
        {"type": "context", "context": "main_menu",                    # _seq 31
         "available_commands": ["start", "load", "quit"]},
        # The shim's OWN menu-return detection fires here.
        {"type": "game_ended", "reason": "return_to_menu"},            # _seq 32
    ]

    def _launch(self):
        gs = GameState()
        for event in self._LAUNCH_SEQUENCE:
            gs.push_event(dict(event))
        return gs

    def test_launch_splash_then_menu_does_not_latch_game_terminal(self):
        gs = self._launch()
        assert gs.current_game_terminal is False
        assert gs.status != "ended"
        assert gs.get_state()["game_terminal"] is False

    def test_the_suppressed_menu_return_is_labelled_in_the_transcript(self):
        # The shim genuinely reported a menu return, so the transcript stays
        # honest — but it is annotated non-terminal, and distinguishably so
        # from the end_on_menu_return per-game opt-out.
        gs = self._launch()
        ended = [e for e in gs.get_transcript() if e["type"] == "game_ended"]
        assert len(ended) == 1
        assert ended[0]["terminal"] is False
        assert ended[0]["suppressed"] == "no_gameplay_seen"
        evidence = [e for e in gs.get_transcript()
                    if e["type"] == "terminal_evidence"]
        assert evidence and evidence[0]["suppressed"] is True

    def test_the_run_that_follows_the_splash_menu_plays_normally(self):
        # Pressing Start after the splash-menu must behave like a fresh run:
        # nothing latched, live progress captured.
        gs = self._launch()
        gs.push_event({"type": "context", "context": "in_game"})      # _seq 46
        _play_with_stats(gs)
        assert gs.current_game_terminal is False
        assert gs.get_game_state()["stats"] == {"aria_integrity": 42,
                                                "evidence": 7}
        assert "progress_frozen" not in gs.get_game_state()

    def test_a_real_menu_return_after_gameplay_still_latches(self):
        # The guard is _gameplay_seen, not "ignore game_ended": once the run
        # is genuinely underway, the shim's menu-return is still an ending.
        gs = self._launch()
        gs.push_event({"type": "context", "context": "in_game"})
        _play_with_stats(gs)
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        assert gs.current_game_terminal is True
        assert gs.status == "ended"
        ended = [e for e in gs.get_transcript() if e["type"] == "game_ended"]
        assert "suppressed" not in ended[-1]

    def test_hard_end_reasons_are_never_gated_on_gameplay_seen(self):
        # Only return_to_menu is ambiguous. A quit/process_exit is terminal
        # whether or not the bridge ever saw story content.
        gs = self._launch()
        gs.push_event({"type": "game_ended", "reason": "quit"})
        assert gs.status == "ended"
        assert gs.end_reason == "quit"

    def test_opt_out_games_keep_their_own_suppression_label(self):
        gs = GameState()
        gs.end_on_menu_return = False
        _play_with_stats(gs)
        gs.push_event({"type": "game_ended", "reason": "return_to_menu"})
        ended = [e for e in gs.get_transcript() if e["type"] == "game_ended"]
        assert ended[-1]["suppressed"] == "end_on_menu_return"
        assert gs.current_game_terminal is False


class TestStateAlwaysCarriesTheTerminalVerdict:
    """The harness mirrors game_terminal into its own terminal_reached flag.
    A mirror that only ever sees the True edge cannot recover when the bridge
    UN-latches, so /state reports both polarities explicitly."""

    def test_live_run_reports_game_terminal_false(self):
        gs = GameState()
        _play_with_stats(gs)
        assert gs.get_state()["game_terminal"] is False

    def test_ended_run_reports_game_terminal_true(self):
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        assert gs.get_state()["game_terminal"] is True

    def test_resume_flips_the_verdict_back_to_false(self):
        gs = GameState()
        _play_with_stats(gs)
        gs.push_event({"type": "context", "context": "main_menu"})
        assert gs.get_state()["game_terminal"] is True
        gs.push_event({"type": "game_resumed", "reason": "load"})
        assert gs.get_state()["game_terminal"] is False


class TestActTransactionLifecycle:
    """Lifecycle gaps found by the transactional-act review (Aug 2026).

    Each test is one of the review's own execution probes.
    """

    @staticmethod
    def _act(gs, nonce, index=1):
        command = {
            "name": "act", "args": {"index": index}, "nonce": nonce,
            "reset_generation": gs.reset_generation,
        }
        return command, gs.submit_command_with_ack(command)

    def _apply(self, gs, nonce, index=1):
        command, (ok, _, ack) = self._act(gs, nonce, index)
        assert ok is True, ack
        assert gs.consume_command() == command
        gs.push_event({
            "type": "command_result", "command": "act", "nonce": nonce,
            "success": True, "resolved_as": "choice", "label": "Go",
        })
        return ack

    def test_passive_overlay_snapshot_is_scoped_to_the_active_act(self):
        gs = GameState()
        self._apply(gs, "overlay-act")
        snapshot = {
            "type": "screen_content",
            "overlay_screens": ["echo_terminal_live"],
            "overlay_texts": ["ECHO-7>", "THE WINDOW IS NARROWING."],
        }

        gs.push_event(dict(snapshot))
        gs.push_event(dict(snapshot))

        events = gs.get_action_transaction("overlay-act")["events"]
        overlay_events = [
            event for event in events
            if event.get("type") == "screen_content"
        ]
        assert len(overlay_events) == 1
        assert overlay_events[0]["action_id"] == 1
        assert overlay_events[0]["passive_overlay_snapshot"] is True

    # -- HIGH-3: finalization must not depend on a client polling ----------

    def test_unpolled_applied_transaction_does_not_block_the_next_act(self):
        """act(wait=False) never reads /transaction; the slot must not wedge."""
        gs = GameState()
        self._apply(gs, "first")
        # Story settles, but nobody ever calls get_action_transaction().
        gs.set_pending_request({
            "type": "choice_request", "id": "next", "choices": ["On"],
        })
        gs._act_transactions["first"]["settle_observed_at"] -= 5.0

        _, (ok, message, ack) = self._act(gs, "second")

        assert ok is True, message
        assert ack["transaction_state"] == "accepted"
        assert gs._act_transactions["first"]["transaction_state"] == "settled"

    def test_push_event_finalizes_a_quiet_transaction(self):
        gs = GameState()
        self._apply(gs, "quiet")
        gs.set_pending_request({
            "type": "choice_request", "id": "next", "choices": ["On"],
        })
        assert gs._act_transactions["quiet"]["transaction_state"] == "applied"
        gs._act_transactions["quiet"]["settle_observed_at"] -= 5.0

        gs.push_event({"type": "context", "context": "in_game"})

        assert gs._act_transactions["quiet"]["transaction_state"] == "settled"
        assert gs._active_action_nonce is None

    # -- The applied-record bound is a TWO-TIER IDLE TTL; all four directions --
    #
    # History (design/DESIGN_act_transactional_ack.md, revision history):
    # a fixed TTL measured from applied_at force-settled a 63-narration ending
    # sequence and lost 35 lines; removing the TTL entirely wedged the slot
    # forever when the settle boundary was never observable — both when the
    # game process died AND when an act re-rendered an identical surface.
    # Round 7 made a live shim heartbeat a hard CONJUNCT of expiry, which
    # reopened the second wedge (the heartbeat comes from the shim's
    # background poll thread and outlives a hung main loop).  It is now a
    # MULTIPLIER: the clock runs from the last event attributed to the
    # transaction, and a live heartbeat buys _ACTION_LIVE_SHIM_IDLE_MULTIPLIER
    # times the budget for a legitimately quiet cutscene — not forever.

    def test_a_live_story_is_never_force_settled_however_long_it_runs(
        self, monkeypatch,
    ):
        """Direction 1: events keep arriving, so neither tier ever fires.

        Runs for 3x the LIVE-shim budget (the longer of the two tiers) with
        the heartbeat pinned to its most-abandoned value, so the only thing
        keeping the transaction open is the story itself.
        """
        monkeypatch.setattr(GameState, "_ACTION_APPLIED_IDLE_TTL", 0.2)
        gs = GameState()
        self._apply(gs, "long-story")
        record = gs._act_transactions["long-story"]
        assert record["transaction_state"] == "applied"
        assert not record.get("settle_observed")
        # Shim state is irrelevant while the story is speaking: even a shim
        # that never polled at all cannot cut a live playback short.
        gs._last_shim_command_poll_at = 0.0

        spoken = []
        end = time.time() + 3 * (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
        )
        index = 0
        while time.time() < end:
            text = "line {}".format(index)
            gs.push_event({"type": "narration", "text": text})
            spoken.append(text)
            index += 1
            time.sleep(0.05)

        assert len(spoken) > 5
        assert record["transaction_state"] == "applied"
        assert record.get("settled_by") is None
        # Nothing lost: every line the story spoke is still scoped to the act.
        view = gs.get_action_transaction("long-story")
        assert [
            event["text"] for event in view["events"]
            if event.get("type") == "narration"
        ] == spoken

    def test_an_applied_transaction_expires_after_true_idleness(self):
        """Direction 2: silence + a stale shim expires at the base TTL.

        The attributed event here is a bare ``command_result``, which is the
        one attributed shape that is NOT an outcome boundary — the shim
        acknowledging a command it ran says nothing about an effect (the
        phantom ``ok: True`` of the act-stall saga).  Live evidence for the
        shape: Roadwarden actions 984 and 1340-1342 each produced exactly one
        attributed command_result and nothing else, ever.  A story event here
        would settle at the outcome boundary instead and stop exercising the
        idle tier at all.
        """
        gs = GameState()
        self._apply(gs, "silent")
        record = gs._act_transactions["silent"]
        assert record["transaction_state"] == "applied"
        assert not record.get("settle_observed")
        # The game process died here: nothing observed a settle boundary and
        # nothing has been attributed to the transaction since.
        idle = GameState._ACTION_APPLIED_IDLE_TTL + 1
        record["last_event_at"] -= idle
        record["applied_at"] -= idle
        gs._last_shim_command_poll_at -= idle

        _, (ok, message, ack) = self._act(gs, "after-silence")

        assert ok is True, message
        assert ack["transaction_state"] == "accepted"
        assert record["transaction_state"] == "applied"
        assert record["gate_released"] is True
        assert record["gate_released_by"] == "abandoned_shim"
        # Releasing admission is not discarding: output stays drainable.
        assert gs.get_action_transaction("silent")["pending"] is True

    def test_later_command_result_does_not_refresh_active_act(self):
        """A rejected retry/back command cannot prolong its own blocker."""
        gs = GameState()
        self._apply(gs, "active")
        record = gs._act_transactions["active"]
        before = record["last_event_at"]

        event = {
            "type": "command_result", "command": "back",
            "nonce": "later-command", "success": False,
            "error": "A choice is active.",
        }
        gs.push_event(event)

        assert record["last_event_at"] == before
        assert event.get("action_id") is None
        assert all(
            item.get("command") != "back" for item in record["events"]
        )

    def test_later_successful_command_bounds_prior_act_attribution(self):
        """Queued command effects must not be stolen by the previous act."""
        gs = GameState()
        self._apply(gs, "active")
        record = gs._act_transactions["active"]
        record["gate_released"] = True
        record["gate_released_by"] = "idle_ttl_live_shim"
        record["gate_released_at"] = time.time()

        result = {
            "type": "command_result", "command": "back",
            "nonce": "later-command", "success": True,
        }
        gs.push_event(result)
        settled_revision = record["revision"]

        assert record["transaction_state"] == "settled"
        assert record["settled_by"] == "superseded_by_command"
        assert record["superseding_command"] == "back"
        assert record.get("gate_released") is None
        assert gs._active_action_nonce is None
        assert gs._last_settled_action_nonce is None
        assert result.get("action_id") is None

        effect = {"type": "hide", "image": "preferences"}
        gs.push_event(effect)

        assert effect.get("action_id") is None
        assert record["revision"] == settled_revision
        assert not any(
            item.get("type") == "hide" for item in record["events"]
        )

    @pytest.mark.parametrize(
        "command", [
            "dump_tree", "flight_recorder", "get_defaults", "get_stats",
            "inspect", "inventory_scan", "progress", "resync", "save",
            "screenshot", "set", "set_save_slot", "watchdog_status",
        ],
    )
    def test_observational_command_does_not_end_active_act(self, command):
        gs = GameState()
        self._apply(gs, "active")
        record = gs._act_transactions["active"]

        gs.push_event({
            "type": "command_result", "command": command, "success": True,
        })

        assert record["transaction_state"] == "applied"
        assert gs._active_action_nonce == "active"

    def test_extension_command_can_declare_a_causal_boundary(self):
        gs = GameState()
        self._apply(gs, "active")
        record = gs._act_transactions["active"]

        gs.push_event({
            "type": "command_result", "command": "mod_advance",
            "success": True, "causal_boundary": True,
        })

        assert record["transaction_state"] == "settled"
        assert record["superseding_command"] == "mod_advance"

    def test_command_boundary_closes_existing_trailing_attribution(self):
        gs = GameState()
        self._apply(gs, "active")
        record = gs._act_transactions["active"]
        gs._settle_transaction_locked(record)
        assert gs._last_settled_action_nonce == "active"

        gs.push_event({
            "type": "command_result", "command": "back", "success": True,
        })
        effect = {"type": "hide", "image": "preferences"}
        gs.push_event(effect)

        assert gs._last_settled_action_nonce is None
        assert effect.get("action_id") is None
        assert not any(
            item.get("type") == "hide" for item in record["events"]
        )

    def test_command_boundary_survives_quiet_finalize_in_same_push(self):
        gs = GameState()
        self._apply(gs, "active")
        record = gs._act_transactions["active"]
        record["settle_observed"] = True
        record["settle_observed_at"] = (
            time.time() - GameState._ACTION_SETTLE_GRACE - 1
        )

        gs.push_event({
            "type": "command_result", "command": "back", "success": True,
        })
        effect = {"type": "show", "image": "preferences"}
        gs.push_event(effect)

        assert record["transaction_state"] == "settled"
        assert gs._last_settled_action_nonce is None
        assert effect.get("action_id") is None

    def test_idle_ttl_falls_back_to_applied_at_before_any_event(self):
        gs = GameState()
        self._apply(gs, "no-events")
        record = gs._act_transactions["no-events"]
        record.pop("last_event_at", None)
        record["applied_at"] -= GameState._ACTION_APPLIED_IDLE_TTL + 1
        gs._last_shim_command_poll_at -= GameState._ACTION_APPLIED_IDLE_TTL + 1

        _, (ok, message, _ack) = self._act(gs, "after-no-events")

        assert ok is True, message
        assert record["gate_released_by"] == "abandoned_shim"

    def test_event_silence_does_not_settle_at_the_base_ttl_while_shim_is_alive(
        self,
    ):
        """A live heartbeat BUYS TIME: past tier 1, short of tier 2."""
        gs = GameState()
        self._apply(gs, "quiet-live-screen")
        record = gs._act_transactions["quiet-live-screen"]
        idle = GameState._ACTION_APPLIED_IDLE_TTL + 1
        record["last_event_at"] -= idle
        record["applied_at"] -= idle
        gs._last_shim_command_poll_at = time.time()

        _, (ok, _message, ack) = self._act(gs, "must-wait")

        assert ok is False
        assert ack["reason"] == "action_in_flight"
        assert record["transaction_state"] == "applied"

    def test_silence_under_a_live_shim_expires_at_the_multiplied_budget(self):
        """Direction 3, the wedge-freedom regression.

        The heartbeat as a hard conjunct meant an act that re-rendered an
        identical surface (no new pending request, no differing screen) never
        settled while the game process lived — the review measured the next
        act still rejected after a simulated HOUR.  The heartbeat multiplies
        the idle budget; it does not veto expiry.

        The identical re-render now settles at its OUTCOME boundary whenever
        it produced any attributed state at all (see
        ``test_a_stats_delta_under_an_identical_menu_settles_at_the_grace``);
        what remains for this tier is the harder case the same live session
        also produced — an act that emitted nothing but the shim's own
        command ack.
        """
        gs = GameState()
        self._apply(gs, "identical-rerender")
        record = gs._act_transactions["identical-rerender"]
        assert not record.get("settle_observed")
        live_budget = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
        )
        an_hour = 3600.0
        assert an_hour > live_budget
        record["last_event_at"] -= an_hour
        record["applied_at"] -= an_hour
        # ...and the shim is still polling throughout, as its background
        # thread does even through a hung Ren'Py main loop.
        gs._last_shim_command_poll_at = time.time()

        _, (ok, message, ack) = self._act(gs, "next-act")

        assert ok is True, message
        assert ack["transaction_state"] == "accepted"
        assert record["transaction_state"] == "applied"
        assert record["gate_released"] is True
        assert record["gate_released_by"] == "idle_ttl_live_shim"
        # Freed, not discarded: the originating act result remains drainable.
        view = gs.get_action_transaction("identical-rerender")
        assert any(
            event.get("command") == "act" for event in view["events"]
        )

    def test_story_resuming_after_live_idle_expiry_stays_scoped_to_the_act(self):
        """Timeout releases the gate without inventing an output boundary."""
        gs = GameState()
        self._apply(gs, "long-pause")
        record = gs._act_transactions["long-pause"]
        live_budget = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
        )
        record["last_event_at"] -= live_budget + 1
        record["applied_at"] -= live_budget + 1
        gs._last_shim_command_poll_at = time.time()

        assert gs.consume_command() is None
        assert record["gate_released_by"] == "idle_ttl_live_shim"
        gs._last_settled_at -= GameState._ACTION_POST_SETTLE_GRACE + 1
        gs.push_event({"type": "narration", "text": "The scene resumes."})

        view = gs.get_action_transaction("long-pause")
        assert any(
            event.get("text") == "The scene resumes."
            for event in view["events"]
        )
        assert gs.transcript[-1]["action_id"] == record["action_id"]
        assert record.get("gate_released") is None
        assert record["transaction_state"] == "applied"

    # -- Round 16: an attributed post-apply state change IS a boundary -----
    #
    # Reconstructed from the overnight Roadwarden session (2026-08-16/17):
    # 34 `action_in_flight` 409s in 2,073 acts, every one of them blocked by a
    # menu act that re-rendered an IDENTICAL surface.  Action 929 ("I'd like to
    # eat.") is the canonical timeline and the tests below replay it:
    #
    #   01:41:59.767  applied
    #   01:42:00.092  show plus2food          <- attributed
    #   01:42:00.221  narration               <- attributed
    #   01:42:01.219  stats_update Food: full <- attributed, the act's effect
    #   01:43:14      next act -> 409 action_in_flight (blocking_action_id 929)
    #   01:43:36      next act -> 409
    #   01:44:00      next act -> 409
    #   01:44:01.304  gate_released_by idle_ttl_live_shim  (120 s tier)
    #   01:44:25.772  settled_by superseded_after_idle
    #
    # No new request id and no differing screen signature were ever produced,
    # so nothing marked `settle_observed` and the whole 2.5-minute live-shim
    # tier had to run out.  The act's effect had been observed at 01:42:01.

    def test_a_stats_delta_under_an_identical_menu_settles_at_the_grace(self):
        """The live wedge, converted: 409 at 01:43:14 becomes an acceptance."""
        gs = GameState()
        self._apply(gs, "eat")
        record = gs._act_transactions["eat"]
        # Exactly what the shim attributed to action 929, in order.
        gs.push_event({"type": "show", "name": "plus2food"})
        gs.push_event({"type": "narration", "text": "She stretches out her arm"})
        gs.push_event({
            "type": "stats_update",
            "stats": {"_summary": "Food: full"},
        })
        # Nothing else ever arrives: the menu re-rendered identically, so no
        # new pending request and no differing screen signature follow.
        assert record["settle_observed"] is True
        assert record["settle_observed_by"] == "attributed_state_change"
        assert record["transaction_state"] == "applied"
        # The shim keeps polling throughout, as it did live — under the old
        # rules that only bought the record the longer of the two idle tiers.
        gs._last_shim_command_poll_at = time.time()

        # 73 seconds later (01:43:14), the agent's next act.
        record["settle_observed_at"] -= 73.0
        record["last_event_at"] -= 73.0
        _, (ok, message, ack) = self._act(gs, "rest")

        assert ok is True, message
        assert ack["transaction_state"] == "accepted"
        assert record["transaction_state"] == "settled"
        # Settled at a boundary, NOT stepped over by an idle release.
        assert record.get("gate_released_by") is None
        assert record.get("settled_by") is None
        # Settling is not discarding.
        view = gs.get_action_transaction("eat")
        assert [event["type"] for event in view["events"]] == [
            "command_result", "show", "narration", "stats_update",
        ]

    def test_a_stats_update_alone_is_an_outcome_boundary(self):
        """A toggle whose only visible effect is a stat still settles.

        This is the narrower shape of the live wedge (Roadwarden 942/1304 and
        the power-priority toggles): the actionable surface is byte-identical
        before and after, so the cancellation projection sees nothing — but
        something demonstrably happened.
        """
        gs = GameState()
        self._apply(gs, "toggle")
        record = gs._act_transactions["toggle"]
        record["interaction_type"] = "button"
        gs.push_event({
            "type": "stats_update", "stats": {"power_priority": "comms"},
        })

        assert record["settle_observed"] is True
        record["settle_observed_at"] -= (
            GameState._ACTION_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
        )
        gs._last_shim_command_poll_at = time.time()
        _, (ok, message, _ack) = self._act(gs, "confirm")

        assert ok is True, message
        assert record["transaction_state"] == "settled"

    def test_delayed_story_cannot_be_stolen_by_a_successor(self):
        """An inferred output boundary keeps the old provenance window shut.

        The live Roadwarden trace had a 0.998 s gap between narration and the
        final stats event, longer than the structural 0.75 s grace. Admitting a
        successor inside that gap assigns the late event to the wrong action.
        """
        gs = GameState()
        self._apply(gs, "toggle")
        record = gs._act_transactions["toggle"]
        record["interaction_type"] = "button"
        gs.push_event({
            "type": "stats_update", "stats": {"power_priority": "comms"},
        })
        # Past the structural grace, but still inside the attribution-safe
        # outcome grace.
        record["settle_observed_at"] -= GameState._ACTION_SETTLE_GRACE + 0.1

        _, (ok, _message, ack) = self._act(gs, "too-early")

        assert ok is False
        assert ack["reason"] == "action_in_flight"
        gs.push_event({"type": "narration", "text": "The relay answers."})
        assert gs.transcript[-1]["action_id"] == record["action_id"]
        assert record["transaction_state"] == "applied"

        record["settle_observed_at"] -= (
            GameState._ACTION_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
        )
        _, (ok, message, _ack) = self._act(gs, "after-quiet")
        assert ok is True, message

    # -- Fleet R64 defect 1: the hold is a WINDOW, not a treadmill ---------
    #
    # The trailing-attribution grace exists to stop a successor stealing
    # DELAYED output (the test above).  It was measured from
    # `settle_observed_at`, which every attributed row deliberately pushes
    # forward so a burst can never be settled mid-playback — so an act whose
    # consequence keeps narrating held the one-act gate for the whole scene.
    # R64: echo64-s04 and echo64-s07 each had an act rejected
    # `action_in_flight` at 10.2 s (the client's absorb cap) by a blocker that
    # had applied ~45 s earlier and was merely still talking.  Anchoring the
    # hold to the FIRST observed outcome keeps the delayed-output protection
    # and drops the treadmill.

    def test_a_running_burst_stops_holding_admission_after_its_first_outcome(
        self,
    ):
        gs = GameState()
        self._apply(gs, "48-hours")
        record = gs._act_transactions["48-hours"]
        gs.push_event({
            "type": "choice_resolved", "text": "Not yet. Give me 48 hours.",
        })
        assert record["settle_observed_by"] == "attributed_state_change"
        # Forty-five seconds of unbroken narration: the FIRST outcome is long
        # past, the LAST one is a moment ago, and the record is nowhere near
        # settling because that is exactly what the refresh is for.
        record["settle_observed_first_at"] -= (
            GameState._ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 30
        )
        gs.push_event({"type": "narration", "text": "Seven years from now."})
        assert time.time() - record["settle_observed_at"] < 1.0

        _command, (ok, message, _ack) = self._act(gs, "echo-7")

        assert ok is True, message
        # Admitted, NOT settled: silence is still the only thing that settles
        # a choice receipt, and the burst has not gone silent.
        assert record["transaction_state"] == "applied"
        assert record.get("gate_released") is None

    def test_an_accepted_act_blocks_however_long_it_has_been_waiting(self):
        """Tier 1: the click has not RUN, so a second act is a double-act."""
        gs = GameState()
        _command, (ok, _message, _ack) = self._act(gs, "queued")
        assert ok is True
        record = gs._act_transactions["queued"]
        assert record["transaction_state"] == "accepted"
        # Age it past every grace there is, and hand it an outcome marker it
        # could never legitimately own.  Neither may open the gate.
        record["accepted_at"] -= 3600
        record["settle_observed"] = True
        record["settle_observed_first_at"] = time.time() - 3600

        _command, (ok, _message, ack) = self._act(gs, "second")

        assert ok is False
        assert ack["reason"] == "action_in_flight"
        assert ack["blocking_transaction_state"] == "accepted"
        assert "admission_retry_after" not in ack

    def test_an_applied_blocker_with_no_outcome_yet_is_not_on_a_clock(self):
        """Tier 2: nothing has happened, so anything next may BE the outcome."""
        gs = GameState()
        self._apply(gs, "silent")

        _command, (ok, _message, ack) = self._act(gs, "next")

        assert ok is False
        assert ack["reason"] == "action_in_flight"
        assert ack["blocking_transaction_state"] == "applied"
        assert "admission_retry_after" not in ack

    def test_a_blocker_inside_its_grace_says_when_to_come_back(self):
        """Tier 3: the bridge knows the instant, so it reports it."""
        gs = GameState()
        self._apply(gs, "toggle")
        record = gs._act_transactions["toggle"]
        record["interaction_type"] = "button"
        gs.push_event({
            "type": "stats_update", "stats": {"power_priority": "comms"},
        })
        record["settle_observed_first_at"] -= (
            GameState._ACTION_ATTRIBUTED_OUTCOME_SETTLE_GRACE - 2.0
        )

        _command, (ok, _message, ack) = self._act(gs, "too-early")

        assert ok is False
        assert ack["reason"] == "action_in_flight"
        assert ack["blocking_transaction_state"] == "applied"
        assert 1.0 < ack["admission_retry_after"] <= 2.0

    def test_admitting_over_a_running_burst_loses_no_story(self):
        """Dispatch stays the handoff, and settling is still not discarding."""
        gs = GameState()
        self._apply(gs, "prior")
        prior = gs._act_transactions["prior"]
        gs.push_event({"type": "narration", "text": "The first line."})
        prior["settle_observed_first_at"] -= (
            GameState._ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
        )

        command, (ok, message, _ack) = self._act(gs, "successor")
        assert ok is True, message

        # Admission is NOT the handoff: until the shim takes the new command
        # the burst still belongs to the act that caused it.
        gs.push_event({"type": "narration", "text": "Still the prior act."})
        assert gs.transcript[-1]["action_id"] == prior["action_id"]
        assert prior["transaction_state"] == "applied"

        assert gs.consume_command() == command
        assert prior["transaction_state"] == "settled"
        assert prior["settled_by"] == "superseded_by_next_act"

        view = gs.get_action_transaction("prior")
        assert [event["type"] for event in view["events"]] == [
            "command_result", "narration", "narration",
        ]
        assert [event.get("text") for event in view["events"][1:]] == [
            "The first line.", "Still the prior act.",
        ]
        assert sum(
            1 for event in gs.transcript
            if event.get("text") == "Still the prior act."
        ) == 1

        # ...and from the handoff on, output belongs to the successor.
        gs.push_event({"type": "narration", "text": "After the handoff."})
        assert gs.transcript[-1]["action_id"] == (
            gs._act_transactions["successor"]["action_id"]
        )

    def test_choice_menu_disappearance_is_not_a_structural_boundary(self):
        """The old menu clears before the successor request is registered."""
        gs = GameState()
        self._apply(gs, "tell-him")
        record = gs._act_transactions["tell-him"]
        gs.push_event({"type": "narration", "text": "How much do I say?"})
        assert record["settle_observed_by"] == "attributed_state_change"

        gs._observe_transaction_settle_locked(screen={
            "screens": ["say"],
            "interactions": [],
        })

        assert record["settle_observed_by"] == "attributed_state_change"
        assert record.get("settled_screen") is None

        successor = {
            "type": "choice_request",
            "id": "successor-menu",
            "choices": ["Continue", "Wait"],
        }
        gs.set_pending_request(successor)

        assert "settle_observed_by" not in record
        assert record["settled_pending"]["id"] == "successor-menu"

    def test_custom_screen_choice_uses_distinct_actionable_successor(self):
        """A call-screen Return has no pending request to mark its boundary."""
        gs = GameState()
        original = {
            "screens": ["echo_topics"],
            "interactions": [{"type": "choice", "label": "Ask Marcus."}],
        }
        gs.current_game_state = {"screen": original}
        self._apply(gs, "custom-screen-choice")
        record = gs._act_transactions["custom-screen-choice"]
        record["interaction_type"] = "choice"
        record["resolved_as"] = "button"
        record["initial_request_id"] = None
        record["initial_screen_signature"] = gs._action_screen_signature(
            original
        )
        record["initial_screen_choice_content_signature"] = (
            gs._choice_screen_content_signature(original)
        )

        blank = {"screens": ["say"], "interactions": []}
        gs._observe_transaction_settle_locked(screen=blank)
        assert record.get("settled_screen") is None

        successor = {
            "screens": ["echo_topics"],
            "interactions": [{"type": "choice", "label": "Ask ARIA."}],
        }
        gs._observe_transaction_settle_locked(screen=successor)

        assert record["settled_screen"] == successor
        assert record.get("settle_observed_by") is None

    def test_custom_screen_choice_ignores_nonchoice_screen_controls(self):
        """Quick-menu chrome is not the successor decision boundary."""
        gs = GameState()
        original = {
            "screens": ["echo_topics"],
            "interactions": [{"type": "choice", "label": "Ask Marcus."}],
        }
        gs.current_game_state = {"screen": original}
        self._apply(gs, "custom-screen-chrome")
        record = gs._act_transactions["custom-screen-chrome"]
        record["interaction_type"] = "choice"
        record["resolved_as"] = "button"
        record["initial_screen_signature"] = gs._action_screen_signature(
            original
        )
        record["initial_screen_choice_content_signature"] = (
            gs._choice_screen_content_signature(original)
        )

        reissued = {
            "screens": ["echo_topics", "quick_menu"],
            "interactions": [
                {"type": "choice", "label": "Ask Marcus."},
                {"type": "navigation", "source": "info", "label": "Q.Load"},
            ],
            "screen_buttons": [{"label": "Q.Load"}],
        }
        gs._observe_transaction_settle_locked(screen=reissued)
        assert record.get("settled_screen") is None

        chrome = {
            "screens": ["say", "quick_menu"],
            "interactions": [{
                "type": "navigation", "source": "info", "label": "Q.Load",
            }],
            "screen_buttons": [{"label": "Q.Load"}],
        }
        gs._observe_transaction_settle_locked(screen=chrome)
        assert record.get("settled_screen") is None

        successor = {
            "screens": ["echo_topics"],
            "interactions": [{
                "type": "choice", "source": "choice", "label": "Ask ARIA.",
            }],
        }
        gs._observe_transaction_settle_locked(screen=successor)
        assert record["settled_screen"] == successor

    def test_custom_screen_choice_ignores_reconstructed_row_identity(self):
        """A focus-list rebuild of the same decision is not a successor."""
        gs = GameState()
        rows = [
            ("01  ARIA TRUST PROTOCOL  — READ", "trust"),
            ("02  TEMPORAL MECHANICS MODEL  — READ", "temporal"),
            ("03  DR. CHEN — PERSONNEL FILE  — READ", "chen"),
            ("04  ARIA_CORE — SOURCE AUDIT  — REPORT READY", "aria_code"),
            ("05  RECOVERED THREAD — AETHON LIAISON ’45  — READ", "liaison"),
            ("Step away from the console.", "done"),
        ]
        original = {
            "screens": ["echo_terminal_choice"],
            "interactions": [
                {
                    "id": str(index),
                    "screen": "echo_terminal_choice",
                    "index": index,
                    "type": "choice",
                    "source": "button",
                    "display_label": label,
                    "action_strs": [f"Return value={value}"],
                    "action_names": ["Return"],
                    "category": "choices",
                    "wait_after_action": True,
                }
                for index, (label, value) in enumerate(rows, 1)
            ],
        }
        gs.current_game_state = {"screen": original}
        self._apply(gs, "custom-screen-rebuild")
        record = gs._act_transactions["custom-screen-rebuild"]
        record["interaction_type"] = "choice"
        record["resolved_as"] = "button"
        record["initial_screen_signature"] = gs._action_screen_signature(
            original
        )
        record["initial_screen_choice_content_signature"] = (
            gs._choice_screen_content_signature(original)
        )

        reconstructed = {
            "screens": ["_focus_list"],
            "interactions": [
                {
                    "id": f"_focus_list:{' '.join(label.split())}",
                    "screen": "_focus_list",
                    "index": index + 3,
                    "type": "choice",
                    "source": "button",
                    "display_label": " ".join(label.split()),
                    "action_strs": [f"Return value={value}"],
                    "action_names": ["Return"],
                    "category": "choices",
                }
                for index, (label, value) in enumerate(rows, 1)
            ],
        }
        gs._observe_transaction_settle_locked(screen=reconstructed)
        assert record.get("settled_screen") is None

        successor = {
            "screens": ["menu"],
            "interactions": [{
                "id": "room:1",
                "screen": "menu",
                "index": 1,
                "type": "choice",
                "source": "choice",
                "display_label": "Talk to Marcus.",
                "action_strs": ["Return value=marcus"],
            }],
            "choices": ["Talk to Marcus."],
        }
        gs._observe_transaction_settle_locked(screen=successor)
        assert record["settled_screen"] == successor

    def test_custom_screen_choice_can_return_to_a_pending_underlay(self):
        """A modal Return may reveal the same live Ren'Py choice request."""
        gs = GameState()
        underlay = {
            "type": "choice_request",
            "id": "room-menu",
            "choices": ["Use the console.", "Leave."],
        }
        gs.set_pending_request(underlay)
        modal = {
            "screens": ["echo_topics"],
            "interactions": [{"type": "choice", "label": "Ask Marcus."}],
        }
        gs.current_game_state = {"screen": modal}
        self._apply(gs, "custom-screen-underlay")
        record = gs._act_transactions["custom-screen-underlay"]
        record["interaction_type"] = "choice"
        record["resolved_as"] = "button"
        record["initial_screen_signature"] = gs._action_screen_signature(modal)
        record["initial_screen_choice_content_signature"] = (
            gs._choice_screen_content_signature(modal)
        )

        revealed = {
            "screens": ["menu"],
            "interactions": [{
                "type": "choice", "source": "choice",
                "label": "Use the console.",
            }],
        }
        gs._observe_transaction_settle_locked(screen=revealed)

        assert record["initial_request_id"] == "room-menu"
        assert record["settled_screen"] == revealed

    def test_recovered_custom_choice_without_content_signature_defers(self):
        """A pre-field journal record must fail toward delayed settlement."""
        gs = GameState()
        original = {
            "screens": ["echo_topics"],
            "interactions": [{"type": "choice", "label": "Ask Marcus."}],
        }
        gs.current_game_state = original
        self._apply(gs, "recovered-custom-choice")
        record = gs._act_transactions["recovered-custom-choice"]
        record["interaction_type"] = "choice"
        record["resolved_as"] = "button"
        record.pop("initial_screen_choice_content_signature", None)

        successor = {
            "screens": ["echo_topics"],
            "interactions": [{"type": "choice", "label": "Ask ARIA."}],
        }
        gs._observe_transaction_settle_locked(screen=successor)

        assert record.get("settled_screen") is None

    def test_choice_menu_reregistration_is_not_the_successor_boundary(self):
        """A consumed Ren'Py menu may be registered again inside its branch."""
        gs = GameState()
        original = {
            "type": "choice_request",
            "id": "marcus-menu-1",
            "choices": [
                "Tell Marcus about the scan.",
                "Leave him to it.",
            ],
        }
        gs.set_pending_request(original)
        self._apply(gs, "tell-marcus")
        record = gs._act_transactions["tell-marcus"]
        gs.push_event({
            "type": "narration",
            "text": "There is a console within reach, and she says it anyway.",
        })

        reregistered = dict(original, id="marcus-menu-2")
        gs.set_pending_request(reregistered)

        assert record["settle_observed_by"] == "attributed_state_change"
        assert record.get("settled_pending") is None

        successor = {
            "type": "choice_request",
            "id": "lab-menu",
            "choices": ["Go to the audit console.", "Leave the lab."],
        }
        gs.set_pending_request(successor)

        assert "settle_observed_by" not in record
        assert record["settled_pending"]["id"] == "lab-menu"

    def test_identical_successor_menu_still_settles_after_outcome_grace(self):
        """Equal consecutive menus cost latency, never correctness."""
        gs = GameState()
        original = {
            "type": "choice_request",
            "id": "continue-1",
            "choices": ["Continue"],
        }
        gs.set_pending_request(original)
        self._apply(gs, "continue")
        record = gs._act_transactions["continue"]
        gs.push_event({"type": "narration", "text": "The next beat."})

        gs.set_pending_request(dict(original, id="continue-2"))
        record["settle_observed_at"] -= (
            GameState._ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
        )
        gs._last_shim_command_poll_at = time.time()
        _, (ok, message, _ack) = self._act(gs, "after-continue")

        assert ok is True, message
        assert record["transaction_state"] == "settled"

    def test_choice_dialogue_uses_the_longer_inferred_quiet_window(self):
        """Fleet receipt: a Marcus reply landed 6.3 s after the prior line."""
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request",
            "id": "marcus-menu",
            "choices": ["Tell Marcus.", "Leave him to it."],
        })
        self._apply(gs, "tell-marcus")
        record = gs._act_transactions["tell-marcus"]
        gs.push_event({
            "type": "narration",
            "text": "Telling him has a price.",
        })
        record["settle_observed_at"] -= (
            GameState._ACTION_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
        )

        _, (ok, _message, ack) = self._act(gs, "too-early")

        assert ok is False
        assert ack["reason"] == "action_in_flight"
        gs.push_event({
            "type": "dialogue",
            "character": "Marcus",
            "text": "Thank you for telling me.",
        })
        assert gs.transcript[-1]["action_id"] == record["action_id"]

        record["settle_observed_at"] -= (
            GameState._ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
        )
        _, (ok, message, _ack) = self._act(gs, "after-dialogue")
        assert ok is True, message

    def test_screen_button_change_remains_a_structural_boundary(self):
        gs = GameState()
        self._apply(gs, "screen-button")
        record = gs._act_transactions["screen-button"]
        record["interaction_type"] = "button"
        gs.push_event({"type": "narration", "text": "Panel opened."})

        changed = {"screens": ["preferences"], "interactions": []}
        gs._observe_transaction_settle_locked(screen=changed)

        assert "settle_observed_by" not in record
        assert record["settled_screen"] == changed

    def test_preferences_selection_change_settles_without_reissuing_menu(self):
        gs = GameState()
        initial = {"screens": ["menu"], "interactions": [{
            "id": "Mute All", "label": "Mute All", "type": "nav", "is_selected": False,
        }]}
        gs.push_event(dict(initial, type="screen_content"))
        self._apply(gs, "mute-toggle")
        record = gs._act_transactions["mute-toggle"]
        record["interaction_type"] = "nav"
        record["initial_screen_signature"] = gs._action_screen_signature(initial)
        unchanged = dict(initial)
        gs._observe_transaction_settle_locked(screen=unchanged)
        assert not record.get("settled_screen")
        changed = dict(initial, interactions=[dict(initial["interactions"][0], is_selected=True)])
        gs._observe_transaction_settle_locked(screen=changed)
        assert record["settled_screen"] == changed

    def test_the_settle_grace_still_has_to_elapse_after_an_outcome(self):
        """The boundary is supplied, never the quiet.

        Reversing this would settle an act inside the same interaction that
        produced its first line — the direction round 6's reverts were about.
        """
        gs = GameState()
        self._apply(gs, "mid-story")
        record = gs._act_transactions["mid-story"]
        gs.push_event({"type": "narration", "text": "The first line."})
        gs._last_shim_command_poll_at = time.time()

        _, (ok, _message, ack) = self._act(gs, "too-soon")

        assert ok is False
        assert ack["reason"] == "action_in_flight"
        assert record["transaction_state"] == "applied"

    def test_a_state_burst_keeps_pushing_the_settle_boundary_out(self):
        """Direction 1 for the new types: stats/inventory are proof of life."""
        gs = GameState()
        self._apply(gs, "burst")
        record = gs._act_transactions["burst"]
        gs.push_event({"type": "narration", "text": "It begins."})
        first_boundary = record["settle_observed_at"]
        for index in range(5):
            time.sleep(0.02)
            gs.push_event({
                "type": "inventory_update", "inventory": [f"item {index}"],
            })
        assert record["settle_observed_at"] > first_boundary
        assert record["transaction_state"] == "applied"

    def test_a_command_result_alone_is_not_an_outcome_boundary(self):
        """The phantom-ok direction: an ack is not an effect.

        Roadwarden 1896 lived this: two attributed command_results 100 s
        apart, no state event between them, four rejected acts in the gap.
        The shim acking a command it ran must never stand in for the game
        producing something, or the act-stall saga's silent no-ops become
        silent settles.
        """
        gs = GameState()
        self._apply(gs, "ack-only")
        record = gs._act_transactions["ack-only"]
        gs.push_event({
            "type": "command_result", "command": "resync", "success": True,
        })

        assert not record.get("settle_observed")
        gs._last_shim_command_poll_at = time.time()
        record["last_event_at"] -= GameState._ACTION_APPLIED_IDLE_TTL + 1
        record["applied_at"] -= GameState._ACTION_APPLIED_IDLE_TTL + 1

        _, (ok, _message, ack) = self._act(gs, "next")

        # Still inside the live-shim tier: the idle policy is untouched.
        assert ok is False
        assert ack["reason"] == "action_in_flight"
        assert ack["blocking_action_nonce"] == "ack-only"

    def test_observation_progress_renews_admission_without_settling(self):
        """Visual pacing is live work, not a game-side outcome boundary."""
        gs = GameState()
        self._apply(gs, "scrolling")
        record = gs._act_transactions["scrolling"]
        idle = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
            + 1
        )
        record["last_event_at"] -= idle
        record["applied_at"] -= idle
        gs._last_shim_command_poll_at = time.time()

        gs.push_event({
            "type": "observation_progress",
            "command": "act",
            "elapsed": 110.0,
            "phase": "nvl_scroll",
        })
        _, (ok, _message, ack) = self._act(gs, "too-soon")

        assert ok is False
        assert ack["reason"] == "action_in_flight"
        assert record["transaction_state"] == "applied"
        assert not record.get("settle_observed")
        assert record["events"][-1]["type"] == "observation_progress"

    def test_observation_lease_still_expires_after_progress_stops(self):
        gs = GameState()
        self._apply(gs, "stalled-scroll")
        record = gs._act_transactions["stalled-scroll"]
        gs.push_event({
            "type": "observation_progress",
            "command": "act",
            "elapsed": 10.0,
            "phase": "nvl_scroll",
        })
        idle = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
            + 1
        )
        record["last_event_at"] -= idle
        gs._last_shim_command_poll_at = time.time()

        _, (ok, message, ack) = self._act(gs, "after-stall")

        assert ok is True, message
        assert ack["transaction_state"] == "accepted"
        assert record["gate_released_by"] == "idle_ttl_live_shim"

    def test_an_unattributed_event_never_supplies_a_boundary(self):
        """Round 7/8's false-settle direction stays closed.

        Neither a bookkeeping event type nor story arriving with no active
        action may settle an applied record.
        """
        gs = GameState()
        self._apply(gs, "waiting")
        record = gs._act_transactions["waiting"]

        # Not an action event type at all (the shim's context/heartbeat traffic).
        gs.push_event({"type": "context", "context": "in_game"})
        assert not record.get("settle_observed")

        # A story event with the active pointer cleared and the post-settle
        # attribution window shut: attributed to nothing, so it settles nothing.
        gs._active_action_nonce = None
        gs._last_settled_action_nonce = None
        gs.push_event({"type": "narration", "text": "Somebody else's line."})

        assert not record.get("settle_observed")
        assert record["transaction_state"] == "applied"

    def test_output_before_the_apply_is_not_an_outcome(self):
        """``applied`` is the floor: pre-ack output predates the effect."""
        gs = GameState()
        command, (ok, _message, _ack) = self._act(gs, "dispatched")
        assert ok is True
        assert gs.consume_command() == command
        record = gs._act_transactions["dispatched"]
        assert record["transaction_state"] == "accepted"

        gs.push_event({"type": "narration", "text": "Still the old scene."})

        assert not record.get("settle_observed")

    def test_the_outcome_marker_never_reaches_the_transaction_view(self):
        gs = GameState()
        self._apply(gs, "internal")
        gs.push_event({"type": "narration", "text": "Something happened."})

        view = gs.get_action_transaction("internal")

        assert "settle_observed_by" not in view
        assert "settle_observed" not in view

    # -- Round 11: silence never fabricates a story boundary ---------------

    def test_a_dead_shims_released_record_stays_recoverable(self):
        gs = GameState()
        self._apply(gs, "dead-game")
        record = gs._act_transactions["dead-game"]
        ttl = GameState._ACTION_APPLIED_IDLE_TTL
        # The shim applied the act and the process then died.
        record["last_event_at"] -= ttl + 1
        record["applied_at"] -= ttl + 1
        gs._last_shim_command_poll_at -= ttl + 1

        released = gs.get_action_transaction("dead-game")
        assert released["transaction_state"] == "applied"
        assert released["admission_open"] is True

        # Even an arbitrarily long pause is not evidence that story output is
        # complete. Process death is handled by slot reaping/free instead.
        record["gate_released_at"] -= 24 * 60 * 60
        view = gs.get_action_transaction("dead-game")

        assert view["transaction_state"] == "applied"
        assert view["pending"] is True
        assert view["admission_open"] is True
        assert [event["type"] for event in view["events"]] == ["command_result"]
        assert gs.acknowledge_action_events(
            "dead-game", view["delivery_end"]) == "ok"
        assert gs._act_transactions["dead-game"].get("compacted") is not True

    def test_late_story_after_a_long_release_remains_action_scoped(self):
        gs = GameState()
        self._apply(gs, "quiet-live")
        record = gs._act_transactions["quiet-live"]
        ttl = GameState._ACTION_APPLIED_IDLE_TTL
        live_budget = ttl * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
        record["last_event_at"] -= live_budget + 1
        record["applied_at"] -= live_budget + 1
        gs._last_shim_command_poll_at = time.time()

        released = gs.get_action_transaction("quiet-live")
        assert released["admission_open"] is True
        assert released["released_by"] == "idle_ttl_live_shim"

        record["gate_released_at"] -= 24 * 60 * 60
        gs.push_event({"type": "narration", "text": "Still this action."})
        view = gs.get_action_transaction("quiet-live")

        assert view["transaction_state"] == "applied"
        assert "admission_open" not in view
        assert any(
            event.get("text") == "Still this action."
            for event in view["events"]
        )

    def test_story_resuming_after_release_revokes_the_gate(self):
        """Resumed output closes provisional admission without settling."""
        gs = GameState()
        self._apply(gs, "slow-scene")
        record = gs._act_transactions["slow-scene"]
        ttl = GameState._ACTION_APPLIED_IDLE_TTL
        record["last_event_at"] -= ttl + 1
        record["applied_at"] -= ttl + 1
        gs._last_shim_command_poll_at -= ttl + 1
        assert gs.get_action_transaction("slow-scene")["admission_open"] is True

        gs.push_event({"type": "narration", "text": "It was only a pause."})

        # The record starts a fresh idle budget from this line.
        assert record.get("gate_released") is None
        assert record.get("gate_released_at") is None
        view = gs.get_action_transaction("slow-scene")
        assert view["transaction_state"] == "applied"
        assert view["pending"] is True
        assert "admission_open" not in view
        assert any(
            event.get("text") == "It was only a pause."
            for event in view["events"]
        )

    def test_undispatched_result_cannot_apply_or_drop_a_queued_act(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "undispatched-result", 917)
        command, (ok, message, _ack) = self._act(gs, "not-served")
        assert ok is True, message

        gs.push_event({
            "type": "command_result", "command": "act",
            "nonce": "not-served", "success": True,
            "resolved_as": "choice", "label": "Go",
        })

        record = gs._act_transactions["not-served"]
        assert record["transaction_state"] == "accepted"
        assert record["events"] == []
        assert gs.pending_commands == [command]
        assert gs.transcript[-1]["ignored_reason"] == "act_not_dispatched"
        assert gs.consume_command() == command

    # -- Round 10: the acceptance/revocation race --------------------------

    def test_an_event_during_acceptance_rejects_the_stale_action(
        self, tmp_path, monkeypatch,
    ):
        """The review's H4 probe.

        Acceptance is decided under the state lock, persisted OFF it, then
        committed under it again.  A story event landing in that window
        revoked the idle release the acceptance had been granted on, leaving
        two live records that the gate-conditional supersede never cleaned up.
        """
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "accept-race", 918)
        self._apply(gs, "prior")
        prior = gs._act_transactions["prior"]
        live_budget = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
        )
        prior["last_event_at"] -= live_budget + 1
        prior["applied_at"] -= live_budget + 1
        gs._last_shim_command_poll_at = time.time()
        assert gs.consume_command() is None
        assert prior["gate_released"] is True
        released_at = prior["gate_released_at"]

        original_persist = gs._persist_transaction
        raced: list[bool] = []

        def racing_persist(record):
            result = original_persist(record)
            if record.get("action_nonce") == "next" and not raced:
                raced.append(True)
                gs.push_event({
                    "type": "narration", "text": "Resumed mid-acceptance.",
                })
            return result

        monkeypatch.setattr(gs, "_persist_transaction", racing_persist)
        _command, (ok, message, ack) = self._act(gs, "next")

        assert ok is False
        assert "interaction changed" in message
        assert ack["reason"] == "stale_surface"
        assert raced == [True]
        live = [
            record["action_nonce"]
            for record in gs._act_transactions.values()
            if record["transaction_state"] in {"accepted", "applied"}
            and not record.get("gate_released")
        ]
        assert live == ["prior"]
        assert prior.get("gate_released") is None
        assert released_at is not None
        assert gs._act_transactions["next"]["transaction_state"] == "failed"
        assert gs.pending_commands == []
        # The resumed line still belongs to the older transaction.
        assert any(
            event.get("text") == "Resumed mid-acceptance."
            for event in gs.get_action_transaction("prior")["events"]
        )

    def test_replacement_request_during_acceptance_rejects_stale_choice(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "choice-race", 919)
        gs.set_pending_request({
            "type": "choice_request", "id": "old-choice",
            "choices": [{"index": 1, "text": "Old"}],
        })
        original_persist = gs._persist_transaction

        def racing_persist(record):
            result = original_persist(record)
            if record.get("action_nonce") == "stale-choice":
                gs.set_pending_request({
                    "type": "choice_request", "id": "replacement",
                    "choices": [{"index": 1, "text": "New"}],
                })
            return result

        monkeypatch.setattr(gs, "_persist_transaction", racing_persist)
        _command, (ok, _message, ack) = self._act(gs, "stale-choice")

        assert ok is False
        assert ack["reason"] == "stale_surface"
        assert gs.pending_commands == []
        assert gs.pending_request["id"] == "replacement"

    def test_choice_enrichment_during_acceptance_rejects_stale_target(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "choice-enrichment-race", 921)
        gs.set_pending_request({
            "type": "choice_request", "id": "same-choice",
            "choices": [{"index": 1, "text": "Old"}],
        })
        original_persist = gs._persist_transaction

        def racing_persist(record):
            result = original_persist(record)
            if record.get("action_nonce") == "stale-enrichment":
                gs.set_pending_request({
                    "type": "choice_request", "id": "same-choice",
                    "choices": [{"index": 1, "text": "Changed"}],
                })
            return result

        monkeypatch.setattr(gs, "_persist_transaction", racing_persist)
        _command, (ok, _message, ack) = self._act(gs, "stale-enrichment")

        assert ok is False
        assert ack["reason"] == "stale_surface"
        assert gs.pending_commands == []

    def test_stale_rejection_must_reach_disk_before_deduplication(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "stale-write-retry", 920)
        self._apply(gs, "prior")
        prior = gs._act_transactions["prior"]
        idle = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
            + 1
        )
        prior["last_event_at"] -= idle
        prior["applied_at"] -= idle
        gs._last_shim_command_poll_at -= idle
        assert gs.consume_command() is None
        original_persist = gs._persist_transaction
        injected_event = False
        failed_correction = False

        def racing_persist(record):
            nonlocal injected_event, failed_correction
            if (
                record.get("action_nonce") == "next"
                and record.get("transaction_state") == "accepted"
                and not injected_event
            ):
                result = original_persist(record)
                injected_event = True
                gs.push_event({"type": "narration", "text": "Resumed."})
                return result
            if (
                record.get("action_nonce") == "next"
                and record.get("transaction_state") == "failed"
                and not failed_correction
            ):
                failed_correction = True
                return False
            return original_persist(record)

        monkeypatch.setattr(gs, "_persist_transaction", racing_persist)
        _command, (ok, _message, ack) = self._act(gs, "next")
        assert ok is False
        assert ack["transaction_state"] == "acceptance_unknown"
        assert ack["pending"] is True

        _command, (ok, _message, retry) = self._act(gs, "next")
        assert ok is False
        assert retry["reason"] == "stale_surface"
        assert retry["deduplicated"] is True

        restarted = GameState()
        restarted.configure_identity(1, "stale-write-retry", 920)
        assert restarted._act_transactions["next"]["transaction_state"] == (
            "failed"
        )
        assert all(
            command.get("nonce") != "next"
            for command in restarted.pending_commands
        )

    def test_dispatched_abandoned_acts_never_outgrow_the_registry_bound(
        self, tmp_path, monkeypatch,
    ):
        """The review's registry probe, in the form the bridge accepts.

        Round 10's version injected act RESULTS for commands the simulated
        shim had never polled; round 11 rightly made the bridge ignore
        undispatched results, which made the probe vacuous, so it was deleted.
        The finding it measured is real, though (296 records against a bound
        of 256), so this is the same probe driven through a genuine dispatch:
        300 acts, each leased by `consume_command()` and applied, and each
        then abandoned — the game stops attributing anything to it.

        Bounding depends on dispatch settling the prior as
        `superseded_after_idle`, and on a journal to spill settled-but-
        undrained records into.  Only the newest records may still be pending.
        """
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "registry-bound", 917)
        ttl = GameState._ACTION_APPLIED_IDLE_TTL
        for index in range(300):
            nonce = "act-{}".format(index)
            command, (ok, message, _ack) = self._act(gs, nonce)
            assert ok is True, message
            # A real lease: the shim polled for this command and reported it.
            assert gs.consume_command() == command
            gs.push_event({
                "type": "command_result", "command": "act", "nonce": nonce,
                "success": True, "resolved_as": "choice", "label": "Go",
            })
            # ...and is then never heard from again.  Age every clock,
            # including the poll heartbeat consume_command just stamped, so
            # the next act is admitted over a genuinely idle predecessor.
            for record in gs._act_transactions.values():
                for key in ("last_event_at", "applied_at", "gate_released_at"):
                    if record.get(key):
                        record[key] -= ttl + 1
            gs._last_shim_command_poll_at -= ttl + 1

        assert len(gs._act_transactions) <= GameState._MAX_ACT_TRANSACTIONS
        still_pending = [
            record for record in gs._act_transactions.values()
            if GameState._transaction_pending(
                str(record.get("transaction_state", "accepted")))
        ]
        assert len(still_pending) <= 2, [
            record.get("action_nonce") for record in still_pending
        ]

    # -- Round 12: the cancellation matrix ---------------------------------
    #
    # One test per direction of the review's matrix.  The rule under test is
    # A queued act is cancelled only when the canonical actionable surface or
    # request identity changed under it. Narration, context-only state and an
    # explicitly marked resync leave it resolvable.

    MENU_A = {
        "type": "game_state",
        "interactions": [{
            "id": "map", "display_label": "Map", "type": "nav",
            "disabled": False, "source": "button", "screen": "hud",
            "index": 1, "aliases": ["Travel", "Map"],
            "original_label": "World map",
            "action_names": ["ShowMenu"],
            "action_strs": ["ShowMenu screen=map"],
        }],
        "screen_buttons": [{
            "label": "Map", "screen": "hud", "actions": ["ShowMenu"],
            "is_disabled": False, "index": 1,
            "action_strs": ["ShowMenu screen=map"],
        }],
        "choices": ["Go north"],
        "stats": {"hp": 10},
    }
    REQUEST_A = {
        "type": "choice_request", "id": "r1",
        "choices": [{"id": "north", "label": "Go north"}],
        "full_items": [{"label": "Go north", "is_disabled": False}],
    }

    def _queued_successor(self, gs, *, request=None, menu=None):
        """A released prior plus an accepted, undispatched successor."""
        gs.push_event(dict(menu or self.MENU_A))
        if request is not None:
            gs.set_pending_request(dict(request))
        self._apply(gs, "prior")
        prior = gs._act_transactions["prior"]
        idle = GameState._ACTION_APPLIED_IDLE_TTL + 1
        prior["last_event_at"] -= idle
        prior["applied_at"] -= idle
        gs._last_shim_command_poll_at -= idle
        command, (ok, message, _ack) = self._act(gs, "successor")
        assert ok is True, message
        assert gs.pending_commands == [command]
        assert prior.get("gate_released") is True
        return prior, command

    def _assert_kept(self, gs, command):
        successor = gs._act_transactions["successor"]
        assert successor["transaction_state"] == "accepted", successor
        assert successor.get("reason") is None
        assert gs.pending_commands == [command]
        assert gs.consume_command() == command

    def _assert_cancelled(self, gs):
        successor = gs._act_transactions["successor"]
        assert successor["transaction_state"] == "failed", successor
        assert successor["reason"] == "admission_revoked_before_dispatch"
        assert gs.pending_commands == []
        return successor

    def test_a_replaced_menu_cancels_the_queued_successor(self):
        """(a) menu A -> menu B: the act's target genuinely went away."""
        gs = GameState()
        _prior, _command = self._queued_successor(gs)

        gs.push_event(dict(
            self.MENU_A, choices=["Go SOUTH"],
        ))

        successor = self._assert_cancelled(gs)
        # The diagnostic has to match the contract: the same revocation also
        # CLOSES admission, so "act again" on its own sent the caller into a
        # 409 (the round-11 review probed exactly that).
        error = successor["error"]
        assert "surface it targeted changed" in error
        assert "same action_nonce returns this cancelled transaction" in error
        assert "admission reopens" in error
        _command, (ok, _message, retry) = self._act(gs, "too-soon")
        assert ok is False
        assert retry["reason"] == "action_in_flight"

    def test_an_identical_enrichment_rerender_keeps_the_successor(self):
        """(b1) same request id, byte-identical content."""
        gs = GameState()
        _prior, command = self._queued_successor(gs, request=self.REQUEST_A)

        gs.set_pending_request(dict(self.REQUEST_A))

        self._assert_kept(gs, command)

    def test_identical_menu_content_under_a_new_request_id_cancels_the_act(
        self,
    ):
        """A new id owns a new value_map, even if its labels happen to match."""
        gs = GameState()
        _prior, _command = self._queued_successor(gs, request=self.REQUEST_A)

        gs.set_pending_request(dict(self.REQUEST_A, id="r2"))

        self._assert_cancelled(gs)

    def test_a_replaced_menu_cancels_an_ordinary_queued_act(self):
        """The accept-to-dispatch race does not require an idle predecessor."""
        gs = GameState()
        gs.push_event(dict(self.MENU_A))
        command, (ok, message, _ack) = self._act(gs, "ordinary")
        assert ok is True, message
        assert gs.pending_commands == [command]

        gs.push_event(dict(
            self.MENU_A, choices=["Go SOUTH"],
        ))

        record = gs._act_transactions["ordinary"]
        assert record["transaction_state"] == "failed"
        assert record["reason"] == "admission_revoked_before_dispatch"
        assert gs.pending_commands == []
        assert gs.consume_command() is None

    def test_an_explicit_resync_of_the_same_menu_keeps_the_successor(self):
        gs = GameState()
        _prior, command = self._queued_successor(gs, request=self.REQUEST_A)

        gs.set_pending_request(dict(
            self.REQUEST_A,
            id="r2",
            reissued_from_request_id="r1",
            reissue_root_request_id="r1",
        ))

        gs.set_pending_request(dict(
            self.REQUEST_A,
            id="r3",
            reissued_from_request_id="r2",
            reissue_root_request_id="r1",
        ))

        self._assert_kept(gs, command)

    def test_a_new_request_id_with_different_content_still_cancels(self):
        """(b2) must not weaken (a): a genuinely replaced menu still cancels."""
        gs = GameState()
        _prior, _command = self._queued_successor(gs, request=self.REQUEST_A)

        gs.set_pending_request({
            "type": "choice_request", "id": "r2",
            "choices": [{"id": "south", "label": "Go SOUTH"}],
        })

        self._assert_cancelled(gs)

    def test_an_identical_game_state_repush_keeps_the_successor(self):
        """(b3) the scraper re-pushing the same screen is not a change."""
        gs = GameState()
        _prior, command = self._queued_successor(gs)

        gs.push_event(dict(self.MENU_A))

        self._assert_kept(gs, command)

    def test_a_stats_only_game_state_change_keeps_the_successor(self):
        """(b4) a stat ticked; the menu did not."""
        gs = GameState()
        _prior, command = self._queued_successor(gs)

        gs.push_event(dict(self.MENU_A, stats={"hp": 9}))

        self._assert_kept(gs, command)

    def test_request_context_only_change_keeps_the_successor(self):
        gs = GameState()
        request = dict(self.REQUEST_A, stats={"hp": 10}, inventory=["key"])
        _prior, command = self._queued_successor(gs, request=request)

        gs.set_pending_request(dict(
            request, stats={"hp": 9}, inventory=["key", "coin"],
        ))

        self._assert_kept(gs, command)

    def test_unknown_request_scalar_change_keeps_the_successor(self):
        """New decision-context fields do not silently widen cancellation."""
        gs = GameState()
        request = dict(self.REQUEST_A, future_context="before")
        _prior, command = self._queued_successor(gs, request=request)

        gs.set_pending_request(dict(request, future_context="after"))

        self._assert_kept(gs, command)

    @pytest.mark.parametrize("key", [
        "interactions", "screen_buttons", "promoted_buttons", "choices",
    ])
    def test_absent_and_empty_request_target_lists_match(self, key):
        """Optional fallback-list representation is not an actionable change."""
        request = {"type": "input_request", "id": "r1"}
        with_empty = dict(request, **{key: []})

        assert GameState._actionable_request_signature(request) == (
            GameState._actionable_request_signature(with_empty)
        )

    def test_request_type_change_cancels_the_successor(self):
        """The request family remains part of the actionable contract."""
        gs = GameState()
        _prior, _command = self._queued_successor(gs, request=self.REQUEST_A)

        gs.set_pending_request(dict(self.REQUEST_A, type="input_request"))

        self._assert_cancelled(gs)

    def test_narration_under_a_stable_choice_set_keeps_the_successor(self):
        """(c) resumed story revokes the GATE but must not cancel the act.

        Round 11 cancelled on any attributed event.  Resumed output is proof
        the prior transaction is alive — so closing admission is right — but
        it is no evidence at all that the successor's target went away.
        """
        gs = GameState()
        prior, command = self._queued_successor(gs)

        gs.push_event({"type": "narration", "text": "The prior act resumes."})

        # The revoke half of round 9/10 stays: admission is closed again...
        assert prior.get("gate_released") is None
        assert prior["transaction_state"] == "applied"
        _command, (ok, _message, blocked) = self._act(gs, "third")
        assert ok is False
        assert blocked["reason"] == "action_in_flight"
        # ...and the resumed line still belongs to the prior transaction.
        assert any(
            event.get("text") == "The prior act resumes."
            for event in gs.get_action_transaction("prior")["events"]
        )
        # ...but the already-admitted successor is still dispatchable.
        self._assert_kept(gs, command)

    def test_a_canonical_interaction_delta_cancels_the_successor(self):
        """The shim resolves against interactions, including aliases/state."""
        gs = GameState()
        _prior, _command = self._queued_successor(gs)

        gs.push_event(dict(self.MENU_A, interactions=[
            {
                "id": "map", "display_label": "Map", "type": "nav",
                "disabled": True, "source": "button", "screen": "hud",
                "index": None, "aliases": ["Travel", "Map"],
                "original_label": "World map",
                "action_names": ["ShowMenu"],
                "action_strs": ["ShowMenu screen=map"],
            },
        ]))

        self._assert_cancelled(gs)

    def test_an_interaction_annotation_delta_keeps_the_successor(self):
        """Presentation metadata cannot change how the shim resolves an act."""
        gs = GameState()
        _prior, command = self._queued_successor(gs)

        annotated = dict(self.MENU_A)
        annotated["interactions"] = [dict(
            self.MENU_A["interactions"][0],
            annotation="[roll: 8 + 3 = 11]",
        )]
        gs.push_event(annotated)

        self._assert_kept(gs, command)

    def test_button_and_choice_annotations_keep_the_successor(self):
        """Shim presentation metadata can appear on either fallback list."""
        gs = GameState()
        initial = dict(
            self.MENU_A,
            choices=[{"id": "north", "label": "Go north"}],
        )
        _prior, command = self._queued_successor(gs, menu=initial)

        annotated = dict(initial)
        annotated["screen_buttons"] = [dict(
            self.MENU_A["screen_buttons"][0],
            annotation="[roll: 8 + 3 = 11]",
            category="navigation",
        )]
        annotated["choices"] = [{
            "id": "north", "label": "Go north",
            "annotation": "[roll: 8 + 3 = 11]",
        }]
        gs.push_event(annotated)

        self._assert_kept(gs, command)

    def test_request_annotations_keep_the_successor(self):
        """Choice annotations in request choices/full_items are contextual."""
        gs = GameState()
        _prior, command = self._queued_successor(gs, request=self.REQUEST_A)

        gs.set_pending_request(dict(
            self.REQUEST_A,
            choices=[{
                "id": "north", "label": "Go north",
                "annotation": "[roll: 8 + 3 = 11]",
            }],
            full_items=[{
                "label": "Go north", "is_disabled": False,
                "annotation": "[roll: 8 + 3 = 11]",
            }],
        ))

        self._assert_kept(gs, command)

    def test_alias_reordering_keeps_the_successor(self):
        """Aliases are matched by membership, not provider ordering."""
        gs = GameState()
        _prior, command = self._queued_successor(gs)

        rerender = dict(self.MENU_A)
        rerender["interactions"] = [dict(
            self.MENU_A["interactions"][0], aliases=["Map", "Travel"],
        )]
        gs.push_event(rerender)

        self._assert_kept(gs, command)

    def test_original_label_delta_cancels_the_successor(self):
        """The shim uses original_label during transformed-label fallback."""
        gs = GameState()
        _prior, _command = self._queued_successor(gs)

        rerender = dict(self.MENU_A)
        rerender["interactions"] = [dict(
            self.MENU_A["interactions"][0], original_label="Local map",
        )]
        gs.push_event(rerender)

        self._assert_cancelled(gs)

    def test_action_string_delta_cancels_the_successor(self):
        """A changed action value can execute a different target."""
        gs = GameState()
        _prior, _command = self._queued_successor(gs)

        rerender = dict(self.MENU_A)
        rerender["interactions"] = [dict(
            self.MENU_A["interactions"][0],
            action_strs=["ShowMenu screen=local_map"],
        )]
        gs.push_event(rerender)

        self._assert_cancelled(gs)

    def test_a_dispatched_successor_is_never_cancelled(self):
        """(d) after dispatch the shim owns the act; it may already have landed."""
        gs = GameState()
        _prior, command = self._queued_successor(gs)
        assert gs.consume_command() == command

        gs.push_event(dict(
            self.MENU_A, choices=["Go SOUTH"],
        ))
        gs.push_event({"type": "narration", "text": "Anything at all."})

        successor = gs._act_transactions["successor"]
        assert successor["transaction_state"] == "accepted"
        assert successor["dispatched"] is True
        assert successor.get("reason") is None

    def test_a_surface_change_cancels_with_the_active_pointer_elsewhere(self):
        """The cancellers sweep the registry, as the idle reaper already does.

        A released prior can sit in the registry while `_active_action_nonce`
        is None (a failed sibling cleared it, a reconstruction rebuilt more
        than one live record).  Keying the canceller on that pointer left the
        successor accepted against a replaced menu.
        """
        gs = GameState()
        _prior, _command = self._queued_successor(gs)
        gs._active_action_nonce = None

        gs.push_event(dict(
            self.MENU_A, choices=["Go SOUTH"],
        ))

        self._assert_cancelled(gs)

    def test_post_acceptance_request_enrichment_cancels_stale_successor(self):
        gs = GameState()
        gs.set_pending_request({
            "type": "choice_request", "id": "same-choice",
            "choices": [{"id": "route", "label": "Old"}],
        })
        self._apply(gs, "prior")
        prior = gs._act_transactions["prior"]
        idle = GameState._ACTION_APPLIED_IDLE_TTL + 1
        prior["last_event_at"] -= idle
        prior["applied_at"] -= idle
        gs._last_shim_command_poll_at -= idle
        command, (ok, message, _ack) = self._act(gs, "successor")
        assert ok is True, message
        assert gs.pending_commands == [command]

        gs.set_pending_request({
            "type": "choice_request", "id": "same-choice",
            "choices": [{"id": "route", "label": "Changed"}],
        })

        assert gs.pending_commands == []
        successor = gs._act_transactions["successor"]
        assert successor["transaction_state"] == "failed"
        assert successor["reason"] == "admission_revoked_before_dispatch"
        _command, (ok, _message, retry) = self._act(gs, "enrichment-too-soon")
        assert ok is False
        assert retry["reason"] == "action_in_flight"

    def test_post_acceptance_button_surface_change_cancels_stale_successor(self):
        gs = GameState()
        gs.push_event({
            "type": "game_state",
            "screen_buttons": [{
                "label": "Old", "screen": "hud", "actions": ["Jump"],
                "is_disabled": False, "index": 1,
                "action_strs": ["Jump target=old"],
            }],
        })
        self._apply(gs, "prior")
        prior = gs._act_transactions["prior"]
        idle = GameState._ACTION_APPLIED_IDLE_TTL + 1
        prior["last_event_at"] -= idle
        prior["applied_at"] -= idle
        gs._last_shim_command_poll_at -= idle
        command, (ok, message, _ack) = self._act(gs, "successor")
        assert ok is True, message
        assert gs.pending_commands == [command]

        gs.push_event({
            "type": "game_state",
            "screen_buttons": [{
                "label": "Changed", "screen": "hud", "actions": ["Jump"],
                "is_disabled": False, "index": 1,
                "action_strs": ["Jump target=changed"],
            }],
        })

        assert gs.pending_commands == []
        successor = gs._act_transactions["successor"]
        assert successor["transaction_state"] == "failed"
        assert successor["reason"] == "admission_revoked_before_dispatch"

    def test_duplicate_prior_result_does_not_cancel_a_queued_successor(self):
        gs = GameState()
        self._apply(gs, "prior")
        prior = gs._act_transactions["prior"]
        idle = GameState._ACTION_APPLIED_IDLE_TTL + 1
        prior["last_event_at"] -= idle
        prior["applied_at"] -= idle
        gs._last_shim_command_poll_at -= idle
        command, (ok, message, _ack) = self._act(gs, "successor")
        assert ok is True, message

        gs.push_event({
            "type": "command_result", "command": "act", "nonce": "prior",
            "success": True, "resolved_as": "choice",
        })

        assert gs.transcript[-1]["ignored_reason"] == (
            "act_result_already_recorded"
        )
        assert gs.pending_commands == [command]
        assert gs._act_transactions["successor"]["transaction_state"] == (
            "accepted"
        )

    def test_dispatch_supersedes_a_live_prior_without_the_gate_flag(self):
        """The supersede is a defense, so it cannot depend on flag timing."""
        gs = GameState()
        self._apply(gs, "prior")
        prior = gs._act_transactions["prior"]
        ttl = GameState._ACTION_APPLIED_IDLE_TTL
        prior["last_event_at"] -= ttl + 1
        prior["applied_at"] -= ttl + 1
        gs._last_shim_command_poll_at -= ttl + 1
        command, (ok, message, _ack) = self._act(gs, "next")
        assert ok is True, message
        # Whatever cleared the flag (a revocation the commit could not see, a
        # reconstruction that rebuilt two live records), the invariant holds.
        for key in ("gate_released", "gate_released_by", "gate_released_at"):
            prior.pop(key, None)

        assert gs.consume_command() == command

        assert prior["transaction_state"] == "settled"
        assert prior["settled_by"] == "superseded_by_next_act"
        assert gs._active_action_nonce == "next"

    def test_ack_cannot_compact_a_gate_released_transaction(self):
        gs = GameState()
        self._apply(gs, "ack-before-resume")
        record = gs._act_transactions["ack-before-resume"]
        live_budget = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
        )
        record["last_event_at"] -= live_budget + 1
        gs._last_shim_command_poll_at = time.time()
        view = gs.get_action_transaction("ack-before-resume")
        # The documented admission contract, not the internal flag.
        assert view["admission_open"] is True
        assert view["released_by"] == "idle_ttl_live_shim"
        assert "gate_released" not in view

        assert gs.acknowledge_action_events(
            "ack-before-resume", view["delivery_end"]
        ) == "ok"
        assert record.get("compacted") is not True
        gs.push_event({"type": "narration", "text": "Late after ack."})

        replay = gs.get_action_transaction("ack-before-resume")
        assert any(
            event.get("text") == "Late after ack."
            for event in replay["events"]
        )

    def test_resumed_output_durably_revokes_idle_gate_release(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "gate-revocation", 811)
        self._apply(gs, "pause")
        record = gs._act_transactions["pause"]
        live_budget = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
        )
        record["last_event_at"] -= live_budget + 1
        gs._last_shim_command_poll_at = time.time()
        assert gs.consume_command() is None
        gs.push_event({"type": "narration", "text": "Back."})
        assert record.get("gate_released") is None

        restarted = GameState()
        restarted.configure_identity(1, "gate-revocation", 811)

        recovered = restarted._act_transactions["pause"]
        assert recovered["transaction_state"] == "applied"
        assert recovered.get("gate_released") is None

    def test_next_dispatched_act_closes_a_timeout_attribution_tail(self):
        gs = GameState()
        self._apply(gs, "timed-out")
        old = gs._act_transactions["timed-out"]
        live_budget = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
        )
        old["last_event_at"] -= live_budget + 1
        gs._last_shim_command_poll_at = time.time()
        assert gs.consume_command() is None

        command, (ok, message, _ack) = self._act(gs, "next")
        assert ok is True, message
        assert gs.consume_command() == command
        assert old["transaction_state"] == "settled"
        assert old["settled_by"] == "superseded_after_idle"
        gs.push_event({"type": "narration", "text": "New action output."})

        assert gs.transcript[-1]["action_id"] == (
            gs._act_transactions["next"]["action_id"]
        )
        assert not any(
            event.get("text") == "New action output."
            for event in gs.get_action_transaction("timed-out")["events"]
        )

    def test_restart_preserves_act_accepted_through_an_idle_gate(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "gate-accept-restart", 812)
        self._apply(gs, "old")
        old = gs._act_transactions["old"]
        live_budget = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
        )
        old["last_event_at"] -= live_budget + 1
        gs._last_shim_command_poll_at = time.time()
        command, (ok, message, _ack) = self._act(gs, "new")
        assert ok is True, message
        assert old["gate_released"] is True

        restarted = GameState()
        restarted.configure_identity(1, "gate-accept-restart", 812)

        assert restarted._active_action_nonce == "old"
        assert restarted.pending_commands == [command]
        assert restarted.consume_command() == command
        assert restarted._act_transactions["old"]["transaction_state"] == "settled"
        assert restarted._act_transactions["old"]["settled_by"] == (
            "superseded_after_idle"
        )

    def test_resumed_events_reset_the_idle_clock_under_a_live_shim(self):
        """Direction 4: silence just short of tier 2, then the story speaks."""
        gs = GameState()
        self._apply(gs, "long-pause")
        record = gs._act_transactions["long-pause"]
        live_budget = (
            GameState._ACTION_APPLIED_IDLE_TTL
            * GameState._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
        )
        almost = time.time() - live_budget * 0.975
        record["last_event_at"] = almost
        record["applied_at"] = almost
        gs._last_shim_command_poll_at = time.time()

        gs.push_event({"type": "narration", "text": "...and then it spoke."})

        assert record["transaction_state"] == "applied"
        assert record.get("settled_by") is None
        assert record["last_event_at"] > almost
        _, (ok, _message, ack) = self._act(gs, "still-blocked")
        assert ok is False
        assert ack["reason"] == "action_in_flight"

    def test_a_shim_that_never_polled_counts_as_abandoned(self):
        """Never-polled is the MOST abandoned state, not an exemption."""
        gs = GameState()
        assert GameState()._last_shim_command_poll_at == 0.0
        self._apply(gs, "never-polled")
        record = gs._act_transactions["never-polled"]
        idle = GameState._ACTION_APPLIED_IDLE_TTL + 1
        record["last_event_at"] -= idle
        record["applied_at"] -= idle
        # consume_command() in _apply is what normally stamps the heartbeat;
        # clear it back to the constructor's default.
        gs._last_shim_command_poll_at = 0.0

        _, (ok, message, _ack) = self._act(gs, "after-never-polled")

        assert ok is True, message
        assert record["gate_released_by"] == "abandoned_shim"

    def test_slot_assignment_stamps_the_shim_heartbeat(
        self, tmp_path, monkeypatch,
    ):
        """configure_identity and consume_command are its only two stampers."""
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "heartbeat", 4321)
        assert gs._last_shim_command_poll_at > 0

    def test_idle_applied_records_are_reaped_by_the_transaction_read(self):
        """The sweep runs from the same opportunistic sites as the lease TTL."""
        gs = GameState()
        self._apply(gs, "orphan")
        record = gs._act_transactions["orphan"]
        # An applied record the active pointer no longer refers to (a restart
        # reconstruction, or an active nonce cleared by a failed sibling).
        gs._active_action_nonce = None
        idle = GameState._ACTION_APPLIED_IDLE_TTL + 1
        record["last_event_at"] -= idle
        record["applied_at"] -= idle
        gs._last_shim_command_poll_at -= idle

        view = gs.get_action_transaction("orphan")

        assert view["transaction_state"] == "applied"
        assert view["admission_open"] is True
        assert view["released_by"] == "abandoned_shim"
        assert "gate_released" not in view
        assert "gate_released_by" not in view

    # -- HIGH-4: leases are bounded ---------------------------------------

    def test_dispatched_act_is_not_reserved_again_immediately(self):
        gs = GameState()
        command, (ok, _, _) = self._act(gs, "leased")
        assert ok is True
        assert gs.consume_command() == command
        # The shim polls several times a second; re-serving every time made
        # its result cache the only thing preventing double execution.
        assert gs.consume_command() is None
        assert gs.consume_command() is None

    def test_lease_is_reserved_again_after_the_reserve_delay(self):
        gs = GameState()
        command, _ = self._act(gs, "leased")
        assert gs.consume_command() == command
        gs._act_transactions["leased"]["reserved_at"] -= (
            GameState._ACTION_LEASE_RESERVE_DELAY + 1)

        assert gs.consume_command() == command

    def test_expired_lease_fails_the_transaction_and_unblocks_the_channel(self):
        gs = GameState()
        command, _ = self._act(gs, "lost-result")
        assert gs.consume_command() == command
        gs._act_transactions["lost-result"]["dispatched_at"] -= (
            GameState._ACTION_LEASE_TTL + 1)

        assert gs.consume_command() is None
        record = gs._act_transactions["lost-result"]
        assert record["transaction_state"] == "failed"
        assert record["reason"] == "shim_no_result"
        assert gs.pending_commands == []

    def test_transaction_read_expires_a_lost_shim_lease(self):
        gs = GameState()
        command, _ = self._act(gs, "lost-reader")
        assert gs.consume_command() == command
        gs._act_transactions["lost-reader"]["dispatched_at"] -= (
            GameState._ACTION_LEASE_TTL + 1)

        view = gs.get_action_transaction("lost-reader")

        assert view["transaction_state"] == "failed"
        assert view["reason"] == "shim_no_result"
        assert gs.pending_commands == []

        _, (ok, message, ack) = self._act(gs, "after-expiry")
        assert ok is True, message
        assert ack["transaction_state"] == "accepted"

    def test_unknown_transaction_read_persists_other_expired_lease(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "unknown-expiry", 4245)
        command, _ = self._act(gs, "real")
        assert gs.consume_command() == command
        gs._act_transactions["real"]["dispatched_at"] -= (
            GameState._ACTION_LEASE_TTL + 1)

        assert gs.get_action_transaction("missing") is None
        restarted = GameState()
        restarted.configure_identity(1, "unknown-expiry", 4245)

        assert restarted._act_transactions["real"]["transaction_state"] == "failed"
        assert restarted.consume_command() is None

    def test_acceptance_is_serialized_with_reset(self, monkeypatch):
        gs = GameState()
        entered_persist = threading.Event()
        release_persist = threading.Event()
        original_persist = gs._persist_transaction

        def persist(record):
            if record.get("action_nonce") == "racing":
                entered_persist.set()
                assert release_persist.wait(2)
            return original_persist(record)

        monkeypatch.setattr(gs, "_persist_transaction", persist)
        submitted = {}
        reset_done = threading.Event()
        submit_thread = threading.Thread(target=lambda: submitted.setdefault(
            "result", self._act(gs, "racing")[1]))
        submit_thread.start()
        assert entered_persist.wait(1)
        reset_thread = threading.Thread(target=lambda: (
            gs.reset(), reset_done.set()))
        reset_thread.start()
        time.sleep(0.05)
        assert not reset_done.is_set()

        release_persist.set()
        submit_thread.join(2)
        reset_thread.join(2)

        assert submitted["result"][0] is True
        assert reset_done.is_set()
        assert gs.reset_generation == 1
        assert gs._act_transactions["racing"]["transaction_state"] == "failed"
        assert gs.pending_commands == []

    def test_reset_meta_failure_leaves_generation_and_state_unchanged(
        self, monkeypatch,
    ):
        gs = GameState()
        monkeypatch.setattr(gs, "_persist_transaction_meta", lambda **kwargs: False)

        assert gs.reset() is False
        assert gs.reset_generation == 0
        assert gs._closed is False

    def test_reset_generation_barrier_survives_snapshot_failure(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "reset-barrier", 4244)
        command, (ok, _, _) = self._act(gs, "old-generation")
        assert ok is True
        assert gs.consume_command() == command
        original_snapshot = gs._persist_transaction_snapshot
        monkeypatch.setattr(
            gs, "_persist_transaction_snapshot", lambda snapshot: False,
        )

        assert gs.reset() is True
        monkeypatch.setattr(gs, "_persist_transaction_snapshot", original_snapshot)
        restarted = GameState()
        restarted.configure_identity(1, "reset-barrier", 4244)

        assert restarted.reset_generation == 1
        assert restarted.consume_command() is None

    def test_next_acceptance_persists_prior_settlement(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "lifecycle", 4242)
        self._apply(gs, "old")
        gs.set_pending_request({
            "type": "choice_request", "id": "next", "choices": ["On"],
        })
        gs._act_transactions["old"]["settle_observed_at"] -= 5

        _, (ok, message, _) = self._act(gs, "new")
        assert ok is True, message

        restarted = GameState()
        restarted.configure_identity(1, "lifecycle", 4242)
        assert restarted._act_transactions["old"]["transaction_state"] == "settled"
        assert restarted._act_transactions["new"]["transaction_state"] == "accepted"

    def test_journal_rewrite_aborts_if_a_new_write_lands(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "rewrite-race", 4243)
        self._apply(gs, "old")
        monkeypatch.setattr(GameState, "_MAX_TRANSACTION_JOURNAL_BYTES", 0)
        original_dumps = json.dumps
        injected = False

        def dumps(value, *args, **kwargs):
            nonlocal injected
            if isinstance(value, dict) and value.get(
                "journal_type") == "act_transaction_meta" and not injected:
                injected = True
                gs._persist_transaction({
                    "action_nonce": "newer", "action_id": 99,
                    "reset_generation": 0, "transaction_state": "settled",
                    "revision": 1,
                })
            return original_dumps(value, *args, **kwargs)

        monkeypatch.setattr(json, "dumps", dumps)

        assert gs._maybe_rewrite_transaction_journal() is False
        with open(gs._transaction_log_path, encoding="utf-8") as stream:
            assert any(
                (json.loads(line).get("record") or {}).get("action_nonce") == "newer"
                for line in stream
            )

    def test_lease_keeps_strict_fifo_until_it_expires(self):
        """Ordering matters: a save must not be reordered ahead of an act."""
        gs = GameState()
        command, _ = self._act(gs, "leased")
        gs.submit_command({"name": "save", "args": {"slot": "1"}})
        assert gs.consume_command() == command
        assert gs.consume_command() is None  # save stays behind the lease

        gs._act_transactions["leased"]["dispatched_at"] -= (
            GameState._ACTION_LEASE_TTL + 1)
        assert gs.consume_command()["name"] == "save"

    # -- MEDIUM-1: post-settle attribution is bounded ----------------------

    def test_post_settle_attribution_stops_after_the_event_cap(self):
        """50 unrelated narrations must not all carry the old action_id."""
        gs = GameState()
        self._apply(gs, "done")
        gs.set_pending_request({
            "type": "choice_request", "id": "next", "choices": ["On"],
        })
        gs._act_transactions["done"]["settle_observed_at"] -= 5.0
        gs.push_event({"type": "context", "context": "in_game"})
        assert gs._act_transactions["done"]["transaction_state"] == "settled"

        for i in range(50):
            gs.push_event({"type": "narration", "text": "Unrelated {}.".format(i)})

        tagged = [
            event for event in gs.transcript
            if event.get("type") == "narration" and event.get("action_id")
        ]
        assert len(tagged) <= GameState._ACTION_POST_SETTLE_MAX_EVENTS
        assert len(tagged) < 50
        assert gs._last_settled_action_nonce is None

    def test_post_settle_attribution_stops_after_the_time_window(self):
        gs = GameState()
        self._apply(gs, "done")
        gs.set_pending_request({
            "type": "choice_request", "id": "next", "choices": ["On"],
        })
        gs._act_transactions["done"]["settle_observed_at"] -= 5.0
        gs.push_event({"type": "context", "context": "in_game"})
        gs._last_settled_at -= GameState._ACTION_POST_SETTLE_GRACE + 1

        gs.push_event({"type": "narration", "text": "Much later."})

        assert gs.transcript[-1].get("action_id") is None
        assert gs._last_settled_action_nonce is None

    # -- MEDIUM-2: retention ----------------------------------------------

    def test_drained_transaction_compacts_to_a_tombstone(self):
        gs = GameState()
        self._apply(gs, "old")
        gs.push_event({"type": "narration", "text": "A long scene."})
        record = gs._act_transactions["old"]
        record["transaction_state"] = "settled"
        gs._active_action_nonce = None

        view = gs.get_action_transaction("old")
        assert view["events"]
        assert gs.acknowledge_action_events("old", view["delivery_end"]) == "ok"

        assert record["compacted"] is True
        assert record["events"] == []
        assert record["event_count"] >= 1
        # Identity survives so a late retry still deduplicates.
        assert record["action_id"] == 1
        assert record["transaction_state"] == "settled"
        _, (ok, message, ack) = self._act(gs, "old")
        assert ok is True, message
        assert ack["deduplicated"] is True
        assert ack["action_id"] == 1

    def test_undrained_output_is_never_compacted_away(self):
        gs = GameState()
        self._apply(gs, "undrained")
        gs.push_event({"type": "narration", "text": "Nobody read this yet."})
        record = gs._act_transactions["undrained"]
        record["transaction_state"] = "settled"

        assert gs._compact_transaction_locked(record) is False
        assert record["events"]

    def test_registry_is_bounded(self, monkeypatch):
        gs = GameState()
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTIONS", 8)
        for i in range(30):
            nonce = "act-{}".format(i)
            self._apply(gs, nonce)
            record = gs._act_transactions[nonce]
            record["transaction_state"] = "settled"
            record["events"] = []
            record["drain_index"] = 0
            gs._active_action_nonce = None
            gs._mark_settled_locked(None)
            assert gs._compact_transaction_locked(record) is True
            gs._evict_transactions_locked()
        assert len(gs._act_transactions) <= 8

    def test_registry_target_never_evicts_undrained_output(self, monkeypatch):
        gs = GameState()
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTIONS", 4)
        for i in range(8):
            nonce = "undrained-{}".format(i)
            self._apply(gs, nonce)
            record = gs._act_transactions[nonce]
            record["transaction_state"] = "settled"
            record["events"] = [{
                "type": "narration", "text": nonce, "action_id": i + 1,
                "_seq": i + 1,
            }]
            record["drain_index"] = 0
            gs._active_action_nonce = None
            gs._mark_settled_locked(None)

        assert len(gs._act_transactions) == 8
        assert all(
            gs._act_transactions["undrained-{}".format(i)]["events"]
            for i in range(8)
        )

    def test_single_transaction_keeps_all_undrained_events(self):
        gs = GameState()
        self._apply(gs, "runaway")
        for i in range(20):
            gs.push_event({"type": "narration", "text": "line {}".format(i)})
        record = gs._act_transactions["runaway"]
        assert [event["text"] for event in record["events"] if event.get(
            "type") == "narration"] == ["line {}".format(i) for i in range(20)]

    def test_without_a_journal_nothing_is_ever_offloaded(self, monkeypatch):
        """No spill target means no spill: RAM bounds never cost story text."""
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTION_EVENTS", 5)
        gs = GameState()
        assert gs._transaction_log_path is None
        self._apply(gs, "no-journal")
        for i in range(40):
            gs.push_event({"type": "narration", "text": "line {}".format(i)})
        record = gs._act_transactions["no-journal"]
        assert record.get("events_offloaded", 0) == 0
        assert len(record["events"]) == 41

    # -- MEDIUM-2 (the reviewer's two unbounded probes): SPILL, never delete --

    def test_per_transaction_event_cap_offloads_to_the_journal(
        self, tmp_path, monkeypatch,
    ):
        """Reviewer's probe: one act produced 50,001 retained events.

        Memory is bounded now, and a full drain still returns every line —
        the overflow lives in the transaction journal, not nowhere.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTION_EVENTS", 200)
        gs = GameState()
        gs.configure_identity(1, "spill", 4242)
        self._apply(gs, "runaway")
        spoken = []
        for i in range(2000):
            text = "line {}".format(i)
            gs.push_event({"type": "narration", "text": text})
            spoken.append(text)
        record = gs._act_transactions["runaway"]

        # Bounded in memory ...
        assert len(record["events"]) == GameState._MAX_ACT_TRANSACTION_EVENTS
        assert record["events_offloaded"] == 2001 - 200
        # ... and complete on the drain, reloaded from the journal.
        view = gs.get_action_transaction("runaway")
        assert view["delivery_cursor"] == 0
        assert view["delivery_end"] == 2001
        assert [
            event["text"] for event in view["events"]
            if event.get("type") == "narration"
        ] == spoken
        assert view["events_reloaded"] == 2001 - 200

    def test_failed_event_write_never_advances_the_spill_watermark(
        self, tmp_path, monkeypatch,
    ):
        import builtins

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTION_EVENTS", 2)
        gs = GameState()
        gs.configure_identity(1, "spill-write-failure", 4246)
        self._apply(gs, "write-failure")
        real_open = builtins.open

        def fail_transaction_append(path, mode="r", *args, **kwargs):
            if path == gs._transaction_log_path and "a" in mode:
                raise OSError("disk unavailable")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fail_transaction_append)
        for i in range(5):
            gs.push_event({"type": "narration", "text": "line {}".format(i)})

        record = gs._act_transactions["write-failure"]
        assert record.get("events_offloaded", 0) == 0
        assert len(record["events"]) == 6
        assert len(gs._transaction_event_backlog) == 5

        monkeypatch.setattr(builtins, "open", real_open)
        assert gs._flush_transaction_events() is True
        assert record["events_offloaded"] == 4
        assert [
            event.get("text") for event in gs.get_action_transaction(
                "write-failure")["events"] if event.get("type") == "narration"
        ] == ["line {}".format(i) for i in range(5)]

    def test_offloaded_events_survive_a_restart(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTION_EVENTS", 10)
        gs = GameState()
        gs.configure_identity(1, "spill-restart", 5150)
        self._apply(gs, "restarted")
        spoken = ["line {}".format(i) for i in range(50)]
        for text in spoken:
            gs.push_event({"type": "narration", "text": text})
        assert gs._act_transactions["restarted"]["events_offloaded"] > 0

        restarted = GameState()
        restarted.configure_identity(1, "spill-restart", 5150)

        view = restarted.get_action_transaction("restarted")
        assert [
            event["text"] for event in view["events"]
            if event.get("type") == "narration"
        ] == spoken
        # Still bounded after the reload — the loader re-drops the prefix.
        assert len(restarted._act_transactions["restarted"]["events"]) <= 10

    def test_journal_rewrite_carries_spilled_events_forward(
        self, tmp_path, monkeypatch,
    ):
        """The rewrite must not garbage-collect what the spill relies on."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTION_EVENTS", 10)
        monkeypatch.setattr(GameState, "_MAX_TRANSACTION_JOURNAL_BYTES", 1)
        gs = GameState()
        gs.configure_identity(1, "rewrite-spill", 909)
        self._apply(gs, "spilled")
        spoken = ["line {}".format(i) for i in range(60)]
        for text in spoken:
            gs.push_event({"type": "narration", "text": text})
        assert gs._act_transactions["spilled"]["events_offloaded"] > 0

        assert gs._maybe_rewrite_transaction_journal() is True

        view = gs.get_action_transaction("spilled")
        assert [
            event["text"] for event in view["events"]
            if event.get("type") == "narration"
        ] == spoken

    def test_journal_rewrite_keeps_evicted_offloaded_tombstones(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTIONS", 4)
        gs = GameState()
        gs.configure_identity(1, "rewrite-evicted", 910)
        for i in range(20):
            nonce = "act-{}".format(i)
            self._apply(gs, nonce)
            gs.push_event({"type": "narration", "text": "story {}".format(i)})
            gs.set_pending_request({
                "type": "choice_request", "id": "req-{}".format(i),
                "choices": ["On"],
            })
            gs._act_transactions[nonce]["settle_observed_at"] -= (
                GameState._ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
            )
        assert "act-0" not in gs._act_transactions

        monkeypatch.setattr(GameState, "_MAX_TRANSACTION_JOURNAL_BYTES", 1)
        assert gs._maybe_rewrite_transaction_journal() is True

        view = gs.get_action_transaction("act-0")
        assert view is not None
        assert any(
            event.get("text") == "story 0" for event in view["events"])

    def test_registry_bound_spills_undrained_records_instead_of_deleting(
        self, tmp_path, monkeypatch,
    ):
        """Reviewer's probe: 600 records at a documented cap of 256.

        The registry is bounded again, and every one of those transactions is
        still drainable by nonce — evicting an undrained record spills it to
        the journal and rehydrates on demand.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTIONS", 32)
        gs = GameState()
        gs.configure_identity(1, "spill-registry", 606)
        for i in range(600):
            nonce = "act-{}".format(i)
            self._apply(gs, nonce)
            gs.push_event({"type": "narration", "text": "story {}".format(i)})
            gs.set_pending_request({
                "type": "choice_request", "id": "req-{}".format(i),
                "choices": ["On"],
            })
            gs._act_transactions[nonce]["settle_observed_at"] -= (
                GameState._ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
            )

        assert len(gs._act_transactions) <= GameState._MAX_ACT_TRANSACTIONS
        # Nothing was destroyed: sample across the whole run, including the
        # very first records, which were evicted hundreds of acts ago.
        for i in (0, 1, 299, 598):
            view = gs.get_action_transaction("act-{}".format(i))
            assert view is not None, i
            assert any(
                event.get("text") == "story {}".format(i)
                for event in view["events"]
            ), i

    def test_a_spilled_transaction_can_still_be_acknowledged(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTIONS", 4)
        gs = GameState()
        gs.configure_identity(1, "spill-ack", 707)
        for i in range(20):
            nonce = "act-{}".format(i)
            self._apply(gs, nonce)
            gs.push_event({"type": "narration", "text": "story {}".format(i)})
            gs.set_pending_request({
                "type": "choice_request", "id": "req-{}".format(i),
                "choices": ["On"],
            })
            gs._act_transactions[nonce]["settle_observed_at"] -= (
                GameState._ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
            )

        assert "act-0" not in gs._act_transactions
        view = gs.get_action_transaction("act-0")
        assert view["events"]
        assert gs.acknowledge_action_events(
            "act-0", view["delivery_end"]) == "ok"
        # Drained: the record compacted to a tombstone (and, being over the
        # registry target, was then evicted — identity survives in the
        # journal, which is what a late retry deduplicates against).
        drained = gs.get_action_transaction("act-0")
        assert drained["compacted"] is True
        assert drained["events"] == []

    def test_a_spilled_nonce_still_deduplicates_a_retry(
        self, tmp_path, monkeypatch,
    ):
        """The registry bound must never make a nonce reusable (criterion 6)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTIONS", 4)
        gs = GameState()
        gs.configure_identity(1, "spill-dedup", 808)
        for i in range(20):
            nonce = "act-{}".format(i)
            self._apply(gs, nonce)
            gs.push_event({"type": "narration", "text": "story {}".format(i)})
            gs.set_pending_request({
                "type": "choice_request", "id": "req-{}".format(i),
                "choices": ["On"],
            })
            gs._act_transactions[nonce]["settle_observed_at"] -= (
                GameState._ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
            )
        assert "act-0" not in gs._act_transactions

        _, (ok, message, ack) = self._act(gs, "act-0")

        assert ok is True, message
        assert ack["deduplicated"] is True
        assert ack["action_id"] == 1

    def test_spilled_nonce_read_failure_is_retryable_not_reaccepted(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "spill-read-failure", 809)
        self._apply(gs, "same")
        record = gs._act_transactions.pop("same")
        gs._active_action_nonce = None
        gs._mark_settled_locked(None)
        gs._remember_offloaded_nonce_locked("same")
        next_action_id = gs._next_action_id
        monkeypatch.setattr(
            gs, "_scan_transaction_journal",
            lambda nonce=None: (_ for _ in ()).throw(
                TransactionJournalError("temporarily unreadable")
            ),
        )

        _, (ok, _message, result) = self._act(gs, "same")

        assert ok is False
        assert result["transaction_state"] == "acceptance_unknown"
        assert result["reason"] == "storage_error"
        assert gs._next_action_id == next_action_id
        assert gs.pending_commands == []
        # Keep the local variable live so the test documents what was spilled.
        assert record["action_id"] == 1

    def test_a_missing_offloaded_record_is_reported_once_per_process(
        self, capsys, monkeypatch,
    ):
        """The 503 is permanent for that nonce and invisible in the protocol."""
        monkeypatch.setattr(
            GameState, "_missing_journal_record_logged", OrderedDict(),
        )
        gs = GameState()
        gs._remember_offloaded_nonce_locked("gone")
        gs._scan_transaction_journal = lambda nonce=None: ({}, {})

        for _ in range(3):
            with pytest.raises(TransactionJournalError):
                gs._rehydrate_transaction_from_journal("gone")

        errors = capsys.readouterr().err
        assert errors.count("'gone'") == 1
        assert "503" in errors

    def test_missing_record_diagnostic_is_thread_safe_and_slot_scoped(
        self, capsys, monkeypatch,
    ):
        monkeypatch.setattr(
            GameState, "_missing_journal_record_logged", OrderedDict(),
        )
        first = GameState()
        first.slot_id = 1
        second = GameState()
        second.slot_id = 2

        threads = [
            threading.Thread(
                target=first._log_missing_journal_record,
                args=("same", "record"),
            )
            for _ in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        second._log_missing_journal_record("same", "record")

        errors = capsys.readouterr().err
        assert errors.count("'same'") == 2

    def test_missing_spilled_record_is_retryable_not_reaccepted(self):
        gs = GameState()
        gs._remember_offloaded_nonce_locked("missing")
        next_action_id = gs._next_action_id
        gs._scan_transaction_journal = lambda nonce=None: ({}, {})

        _, (ok, _message, result) = self._act(gs, "missing")

        assert ok is False
        assert result["transaction_state"] == "acceptance_unknown"
        assert result["reason"] == "storage_error"
        assert gs._next_action_id == next_action_id
        assert gs.pending_commands == []

    def test_missing_spilled_prefix_fails_instead_of_shifting_the_cursor(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(GameState, "_MAX_ACT_TRANSACTION_EVENTS", 2)
        gs = GameState()
        gs.configure_identity(1, "spill-prefix-failure", 810)
        self._apply(gs, "prefix")
        for i in range(5):
            gs.push_event({"type": "narration", "text": "line {}".format(i)})
        assert gs._act_transactions["prefix"]["events_offloaded"] > 0
        monkeypatch.setattr(
            gs, "_read_journal_transaction_events", lambda nonce: [],
        )

        with pytest.raises(TransactionJournalError):
            gs.get_action_transaction("prefix")

    def test_offloaded_nonce_index_does_not_forget_old_transactions(self):
        gs = GameState()
        for i in range(9000):
            gs._remember_offloaded_nonce_locked("nonce-{}".format(i))

        assert len(gs._offloaded_transaction_nonces) == 9000
        assert "nonce-0" in gs._offloaded_transaction_nonces

    def test_an_unknown_nonce_never_costs_a_journal_scan(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gs = GameState()
        gs.configure_identity(1, "no-scan", 808)
        scans = []
        real_scan = gs._scan_transaction_journal
        monkeypatch.setattr(
            gs, "_scan_transaction_journal",
            lambda nonce=None: (scans.append(nonce), real_scan(nonce))[1],
        )

        assert gs.get_action_transaction("never-existed") is None
        assert scans == []

    # -- MEDIUM-5: scoped drains must carry screen_text --------------------

    def test_scoped_drain_includes_screen_text(self):
        gs = GameState()
        self._apply(gs, "overlay")
        gs.push_event({
            "type": "screen_text", "text": "Roadwarden journal page.",
        })

        view = gs.get_action_transaction("overlay")

        assert any(
            event.get("type") == "screen_text" for event in view["events"]
        )

    # -- LOW: ack storage failure is not an unknown nonce ------------------

    def test_ack_persistence_failure_is_distinct_from_unknown_nonce(self, monkeypatch):
        gs = GameState()
        self._apply(gs, "storage")
        gs.push_event({"type": "narration", "text": "Text."})
        view = gs.get_action_transaction("storage")

        monkeypatch.setattr(gs, "_persist_transaction", lambda record: False)
        assert gs.acknowledge_action_events(
            "storage", view["delivery_end"]) == "persist_failed"
        assert gs.acknowledge_action_events("nope", 1) == "unknown_nonce"


class TestModalOverlaySurfaceSignatures:
    """A modal panel opening or closing is a surface change in itself.

    The panel owns the whole actionable surface, so a queued act must be
    cancelled and a transaction must not settle against a screen whose
    controls only *look* the same because the panel's own buttons happen to
    project identically to the scene's.
    """

    _CONTROLS = {
        "interactions": [
            {"id": "b:1", "index": 1, "type": "button",
             "display_label": "CLOSE"},
        ],
        "screen_buttons": [{"label": "CLOSE", "index": 1}],
        "choices": [],
    }

    def test_declared_modal_changes_the_action_screen_signature(self):
        gs = GameState()
        scene = dict(self._CONTROLS)
        panel = dict(self._CONTROLS, modal_overlay_screens=["evidence_screen"])

        assert gs._action_screen_signature(scene) != (
            gs._action_screen_signature(panel)
        )
        assert gs._actionable_screen_signature(scene) != (
            gs._actionable_screen_signature(panel)
        )
        assert "evidence_screen" in gs._action_screen_signature(panel)

    def test_absent_declaration_leaves_the_signature_untouched(self):
        """Every unmodified game keeps byte-identical signatures."""
        gs = GameState()
        scene = dict(self._CONTROLS)

        assert "modal_overlay_screens" not in gs._action_screen_signature(
            scene)
        assert "modal_overlay_screens" not in gs._actionable_screen_signature(
            scene)
        # An empty list is a declaration of nothing, not of a modal panel.
        assert gs._action_screen_signature(
            dict(scene, modal_overlay_screens=[])
        ) == gs._action_screen_signature(scene)

    def test_same_panel_reopened_keeps_one_signature(self):
        gs = GameState()
        first = dict(self._CONTROLS, modal_overlay_screens=["evidence_screen"])
        again = dict(
            self._CONTROLS,
            modal_overlay_screens=["evidence_screen", "evidence_screen"],
        )

        assert gs._action_screen_signature(first) == (
            gs._action_screen_signature(again)
        )


def test_anomaly_latch_records_when_it_was_set():
    """Consumers need to tell a fresh crash from an old one.

    The latch outlives the condition it describes until a later story
    event resolves it; the stamp is what identifies one occurrence.
    """
    import time

    from vnflight.bridge import GameState

    gs = GameState()
    before = time.time()
    event = {
        "type": "anomaly",
        "kind": "renpy_exception",
        "details": {"message": "boom"},
    }
    gs.push_event(event)

    assert gs.anomaly_flag["kind"] == "renpy_exception"
    assert before <= gs.anomaly_flag["_latched_at"] <= time.time()
    # The stored event is a copy: the caller's dict is not stamped.
    assert "_latched_at" not in event


def test_terminal_progress_report_is_ignored_before_gameplay_resumes():
    """rw70-sonnet: after New Game the mod kept reporting the previous run's
    ending node, the bridge re-latched, and the footer served the old run's
    frozen stats for the entire second playthrough."""
    from vnflight.bridge import GameState

    gs = GameState()
    gs.push_event({"type": "game_started", "game_name": "rw"})
    gs.push_event({"type": "context", "context": "in_game"})
    gs.push_event({"type": "dialogue", "who": "A", "what": "play"})
    gs.push_event({"type": "progress_change", "game_terminal": True,
                   "node": "ending_fail1"})
    assert gs.current_game_terminal is True
    gs.push_event({"type": "context", "context": "main_menu"})

    # New Game: game_started clears the latch and suspends progress capture
    # until an in_game context confirms play. A stale terminal report in
    # that window must not re-arm it.
    gs.push_event({"type": "game_started", "game_name": "rw"})
    assert gs.current_game_terminal is False
    gs.push_event({"type": "progress_change", "game_terminal": True,
                   "node": "ending_fail1"})
    assert gs.current_game_terminal is False

    # Once play is confirmed, a real ending latches as before.
    gs.push_event({"type": "context", "context": "in_game"})
    gs.push_event({"type": "progress_change", "game_terminal": True,
                   "node": "ending_success1"})
    assert gs.current_game_terminal is True


def test_anomaly_latch_resolves_on_story_progress_only():
    """The latch is current until the script demonstrably runs again.

    Scrapes do not count: the exception screen itself is scraped, so
    screen_content/screen_text after the anomaly say nothing about
    recovery. A say statement or a menu does.
    """
    from vnflight.bridge import GameState

    gs = GameState()
    gs.push_event({"type": "anomaly", "kind": "renpy_exception",
                   "details": {"message": "boom"}})
    latched_seq = gs.anomaly_flag["_seq"]
    assert gs.anomaly_flag["_latched_seq"] == latched_seq

    gs.push_event({"type": "screen_content", "screens": []})
    gs.push_event({"type": "screen_text", "texts": ["traceback"]})
    gs.push_event({"type": "stats_update", "stats": {"hp": 1}})
    assert "_resolved_at" not in gs.anomaly_flag

    gs.push_event({"type": "dialogue", "who": "A", "what": "hello"})
    assert gs.anomaly_flag["_resolved_by"] == "dialogue"
    assert gs.anomaly_flag["_resolved_seq"] > latched_seq
    # The resolved record is still exposed, so diagnostics keep it.
    assert gs.anomaly_flag["kind"] == "renpy_exception"

    # A fresh anomaly replaces the resolved one and is current again.
    gs.push_event({"type": "anomaly", "kind": "duplicate_buttons",
                   "details": {"message": "menu"}})
    assert "_resolved_at" not in gs.anomaly_flag
    gs.push_event({"type": "choice_request", "request_id": 1,
                   "choices": ["a"]})
    assert gs.anomaly_flag["_resolved_by"] == "choice_request"

    # A real menu/input request arrives through set_pending_request (the
    # /request route), never through push_event: it must resolve too.
    gs.push_event({"type": "anomaly", "kind": "renpy_exception",
                   "details": {"message": "menu-recovery"}})
    assert "_resolved_at" not in gs.anomaly_flag
    assert gs.set_pending_request({"id": "m-1", "type": "choice_request",
                                   "choices": ["go"]}) is not False
    assert gs.anomaly_flag["_resolved_by"] == "choice_request"
    # An enrichment of the SAME request is not new progress.
    gs.push_event({"type": "anomaly", "kind": "renpy_exception",
                   "details": {"message": "during-menu"}})
    gs.set_pending_request({"id": "m-1", "type": "choice_request",
                            "choices": ["go", "stay"]})
    assert "_resolved_at" not in gs.anomaly_flag
    gs.set_pending_request({"id": "m-2", "type": "input_request"})
    assert gs.anomaly_flag["_resolved_by"] == "input_request"

    # Lifecycle boundaries resolve too, even though the bridge handles
    # them in its own arms of the event-type chain.
    for lifecycle in ("game_resumed", "game_started", "game_ended"):
        gs.push_event({"type": "anomaly", "kind": "renpy_exception",
                       "details": {"message": "again"}})
        assert "_resolved_at" not in gs.anomaly_flag
        gs.push_event({"type": lifecycle})
        assert gs.anomaly_flag["_resolved_by"] == lifecycle, lifecycle


# -- story-entry button retires the menu it interrupted (rw70 stale render) --

_GALE_ROCKS_MENU = {
    "type": "choice_request",
    "id": "d279a5b2",
    "choices": [
        "Iuno, the digger.", "Navica, the boatmaker.", "Petronius, the gossip.",
        "Photios, the fisher.", "Porcia, the cook.",
        "Severina, the village headwoman.", "Tatius, the armorer.",
        "I\u2019m looking for someone.",
    ],
}
_TRAVEL_LABEL = "\u2192 [map: Creeks \u2014 2h15m, food, work, water]"


def _accept_and_dispatch_travel(gs, nonce="act-1971", **overrides):
    """Accept a map-travel act against the current pending menu and hand it
    to the shim, the way handle_act does (submit_command_with_ack + poll)."""
    command = {
        "name": "act",
        "args": {"label": _TRAVEL_LABEL},
        "nonce": nonce,
        "reset_generation": gs.reset_generation,
    }
    ok, message, _ack = gs.submit_command_with_ack(command)
    assert ok is True, message
    assert gs.consume_command() == command
    result = {
        "type": "command_result",
        "command": "act",
        "nonce": nonce,
        "success": True,
        "resolved_as": "button",
        "interaction_type": "other",
        "screen": "map_display",
        "label": _TRAVEL_LABEL,
        "wait_after_action": True,
        "story_entry": True,
    }
    result.update(overrides)
    return result


def test_story_entry_button_retires_the_menu_it_interrupted():
    """Repro (a) from bridge/logs/transactions_roadwarden_98636.jsonl:
    01:55:13 Gale Rocks villager menu pending; 01:57:44 act 1971 travels to
    Creeks (map_display button, story_entry, no choice_resolved); 01:57:46
    stats_update location=Creeks; 01:57:51 arrival narration.  A state() in
    that window must not offer the Gale Rocks menu."""
    from vnflight.bridge import GameState

    gs = GameState()
    gs.push_event({"type": "game_started", "game_name": "rw"})
    gs.push_event({"type": "context", "context": "in_game"})
    gs.set_pending_request(dict(_GALE_ROCKS_MENU))
    assert gs.status == "waiting_for_input"

    result = _accept_and_dispatch_travel(gs)
    gs.push_event(result)

    # Retired on the applied result, before any stats/narration lands.
    assert gs.get_pending_request() is None
    assert gs.get_state()["pending_request"] is None
    assert gs.status == "running"
    assert gs.get_action_transaction("act-1971")["transaction_state"] == "applied"

    gs.push_event({"type": "stats_update",
                   "changed": {"location": "Creeks", "pc_area": "creeks"}})
    gs.push_event({"type": "narration",
                   "text": "A few souls, carrying wooden and stone weapons "
                           "and tools, are just leaving the village."})
    assert gs.get_state()["pending_request"] is None

    # The successor (Creeks) menu registers normally.
    gs.set_pending_request({"type": "choice_request", "id": "creeks-01",
                            "choices": ["A young villager."]})
    assert gs.get_pending_request()["id"] == "creeks-01"
    assert gs.status == "waiting_for_input"

    # A late act against the dead menu is refused, not phantom-accepted.
    ok, message = gs.submit_action({"type": "act", "index": 1,
                                    "request_id": "d279a5b2"})
    assert ok is False
    assert "d279a5b2" in message


def test_story_entry_retire_keeps_a_request_newer_than_the_act():
    """The request the shim registered AFTER the act was accepted is the
    live one; retiring is keyed on the id captured at acceptance."""
    from vnflight.bridge import GameState

    gs = GameState()
    gs.push_event({"type": "game_started", "game_name": "rw"})
    gs.push_event({"type": "context", "context": "in_game"})
    gs.set_pending_request(dict(_GALE_ROCKS_MENU))

    result = _accept_and_dispatch_travel(gs)
    gs.set_pending_request({"type": "choice_request", "id": "newer-01",
                            "choices": ["Stay", "Go"]})
    gs.push_event(result)

    assert gs.get_pending_request()["id"] == "newer-01"
    assert gs.status == "waiting_for_input"


def test_only_a_story_entry_button_retires_the_pending_menu():
    """A screen-only button (a panel toggle that merely rebuilds the UI) and
    a choice resolution leave the pending request to the existing paths."""
    from vnflight.bridge import GameState

    def fresh():
        gs = GameState()
        gs.push_event({"type": "game_started", "game_name": "rw"})
        gs.push_event({"type": "context", "context": "in_game"})
        gs.set_pending_request(dict(_GALE_ROCKS_MENU))
        return gs

    # One act per GameState: the bridge refuses a second act while the first
    # transaction is still in flight.
    gs = fresh()
    panel = _accept_and_dispatch_travel(
        gs, nonce="act-panel", story_entry=False, screen="quick_menu",
        label="Character")
    gs.push_event(panel)
    assert gs.get_pending_request()["id"] == "d279a5b2"

    gs = fresh()
    choice = _accept_and_dispatch_travel(
        gs, nonce="act-choice", resolved_as="choice", story_entry=True,
        screen=None, label="Iuno, the digger.")
    gs.push_event(choice)
    assert gs.get_pending_request()["id"] == "d279a5b2"
