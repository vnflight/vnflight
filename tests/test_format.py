"""Tests for vnflight.format — state data builder."""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from vnflight.format import (
    _build_pending,
    _pending_from_choice_interactions,
    _get_latest_interactions,
    _get_latest_screen_buttons,
    _has_choice_overlay,
    _match_interaction,
    _match_choice_target,
    _resolve_label_interaction,
    build_wait_data,
    build_state_data,
    format_event,
    format_categorized_buttons,
    format_interactions,
    format_main_menu,
    format_pending_text,
    format_state_text,
    format_wait_text,
)


@pytest.mark.parametrize("main_menu", [True, False])
def test_main_menu_page_text_is_visible_without_gameplay_hud_replay(main_menu):
    raw = {"status": "running", "context": {"context": "main_menu" if main_menu else "game"},
           "screen": {"main_menu": main_menu, "screens": ["menu"] if main_menu else ["hud"],
                      "texts": ["Music", "Composer"], "buttons": [{"label": "About", "actions": ["ShowMenu"]}]}}
    data = build_state_data(raw)
    rendered = format_state_text(data, verbose=True)
    assert ("Composer" in rendered.get("text", "")) is main_menu


def test_caption_strips_renpy_text_tags():
    """rw70 epilogue: the caption rendered '{color=#f6d6bd}Old Págos{/color}'."""
    from vnflight.format import format_pending_text

    text = format_pending_text({
        "type": "choice",
        "choices": [
            {"label": "The tribe of {color=#f6d6bd}Old Págos{/color} "
                      "survived {b}{{brace{/b}.",
             "index": None, "caption": True, "disabled": False},
            {"label": "(continue)", "index": 1, "caption": False,
             "disabled": False},
        ],
    })

    # "{{" is Ren'Py's escaped opening brace; a closing brace has no escape.
    assert "  | The tribe of Old Págos survived {brace." in text
    assert "{color" not in text and "{/b}" not in text
    assert "  1: (continue)" in text


def test_wait_omits_a_caption_that_repeats_the_narration():
    """The same paragraph arrived as narration AND as the menu's caption;
    the agent read every epilogue paragraph twice."""
    from vnflight.format import format_wait_text

    paragraph = ("The tribe of Old Págos lost many lives to the plague, but "
                 "the supplies brought to them by their friends from Gale "
                 "Rocks helped them overcome the harsh winter.")
    tagged = ("The tribe of {color=#f6d6bd}Old Págos{/color} lost many lives "
              "to the plague, but the supplies brought to them by their "
              "friends from {color=#f6d6bd}Gale Rocks{/color} helped them "
              "overcome the harsh winter.")

    def pending():
        return {
            "type": "choice_request",
            "id": "epilogue",
            "choices": ["(continue)"],
            "full_items": [
                {"label": tagged, "is_caption": True, "is_disabled": False},
                {"label": "(continue)", "is_caption": False,
                 "is_disabled": False},
            ],
        }

    data = build_wait_data(
        [{"type": "narration", "text": paragraph}], pending=pending())
    out = format_wait_text(data)

    assert paragraph in out["text"]
    assert "  | The tribe" not in out["pending"]
    assert "  1: (continue)" in out["pending"]

    # A caption that is NOT in the story stays: it is the only place the
    # prompt appears (tags still stripped).
    other = build_wait_data(
        [{"type": "narration", "text": "Something else entirely."}],
        pending=pending())
    rendered = format_wait_text(other)["pending"]
    assert "  | The tribe of Old Págos lost many lives" in rendered
    assert "{color" not in rendered


def test_caption_with_escaped_brace_is_not_an_echo_of_the_narration():
    """Astra P3: the echo key was taken from the stripped caption, so
    "{{b}Hello" -> "{b}Hello" was stripped AGAIN to "Hello" and the caption
    vanished after narration "Hello"."""
    from vnflight.format import format_wait_text

    pending = {
        "type": "choice_request",
        "id": "m",
        "choices": ["Go"],
        "full_items": [
            {"label": "{{b}Hello", "is_caption": True, "is_disabled": False},
            {"label": "Go", "is_caption": False, "is_disabled": False},
        ],
    }
    data = build_wait_data([{"type": "narration", "text": "Hello"}],
                           pending=pending)
    rendered = format_wait_text(data)["pending"]
    assert "  | {b}Hello" in rendered

    # The genuine echo (same text, real tags) is still dropped.
    pending["full_items"][0]["label"] = "{b}Hello{/b}"
    data = build_wait_data([{"type": "narration", "text": "Hello"}],
                           pending=pending)
    assert "  | " not in format_wait_text(data)["pending"]


def test_game_state_choice_fallback_preserves_menu_caption():
    pending = _pending_from_choice_interactions([
        {
            "id": "The board keeps cycling.",
            "display_label": "The board keeps cycling.",
            "type": "choice",
            "source": "choice",
            "disabled": True,
            "caption": True,
            "index": None,
        },
        {
            "id": "wait",
            "display_label": "Wait.",
            "type": "choice",
            "source": "choice",
            "disabled": False,
            "caption": False,
            "index": 1,
        },
    ])

    built = _build_pending(pending)

    assert built["choices"][0] == {
        "label": "The board keeps cycling.",
        "disabled": False,
        "index": None,
        "caption": True,
    }
    assert built["choices"][1]["index"] == 1


class FakeScreenSession:
    def __init__(self, screen=None, transcript=None):
        self.screen = screen
        self.transcript = transcript or []

    def _get(self, path, timeout=2.0):
        if path == "/screen":
            return 200, {"screen": self.screen}
        return 404, {}

    def get_transcript(self, last_n=30):
        return self.transcript[-last_n:]


def test_format_main_menu_points_to_act():
    text = format_main_menu({"available_commands": ["start", "load"]}, colour=False)

    assert "--- MAIN MENU ---" in text
    assert "start" in text
    assert 'Use: act "<command>"' in text
    assert "Use: cmd" not in text


def test_wait_format_unescapes_renpy_percent_literals():
    data = build_wait_data([
        {"type": "narration", "text": "Signal clarity 99.2%%."},
    ])
    data["_screen_texts"] = ["ARIA: 100%%"]

    rendered = format_wait_text(data)

    assert "99.2%." in rendered["text"]
    assert "99.2%%" not in rendered["text"]
    assert rendered["screen_text"] == "ARIA: 100%"


def test_player_origin_uses_user_role_name():
    event = {
        "type": "dialogue",
        "character": "Dr. Chen",
        "text": "You clicked through.",
        "user_initiated": True,
    }

    rendered = format_wait_text(build_wait_data([event]))

    assert rendered["text"] == "[user] [Dr. Chen] You clicked through."
    assert "[human]" not in rendered["text"]


def test_wait_text_keeps_pending_binding_metadata_for_numeric_followup():
    pending = {
        "type": "choice_request",
        "id": "successor-menu",
        "choices": ["Continue", "Wait"],
    }
    snapshot = {"request_id": "successor-menu", "choices": ["Continue", "Wait"]}
    data = build_wait_data([], pending=pending)
    data["_actionable_snapshot"] = snapshot

    rendered = format_wait_text(data)

    assert rendered["_pending_raw"] == pending
    assert rendered["_actionable_snapshot"] == snapshot


def test_state_text_keeps_pending_binding_metadata_for_numeric_followup():
    pending = {
        "type": "choice_request",
        "id": "successor-menu",
        "choices": ["Continue", "Wait"],
    }
    snapshot = {"request_id": "successor-menu", "choices": ["Continue", "Wait"]}
    data = build_state_data({"pending_request": pending})
    data["_actionable_snapshot"] = snapshot

    for verbose in (False, True):
        rendered = format_state_text(data, verbose=verbose)
        assert rendered["_pending_raw"] == pending
        assert rendered["_actionable_snapshot"] == snapshot


def test_wait_data_preserves_overlay_occurrence_ids_on_story_rows():
    data = build_wait_data([{
        "type": "screen_text",
        "texts": ["SAME ROW", "SAME ROW"],
        "overlay_delivery_ids": [41, 42],
        "passive_overlay_snapshot": True,
    }])

    assert [item["overlay_delivery_id"] for item in data["story"]] == [41, 42]


def test_wait_text_preserves_equal_overlay_rows_with_distinct_ids():
    data = build_wait_data([{
        "type": "screen_text",
        "texts": ["SAME ROW", "SAME ROW"],
        "overlay_delivery_ids": [41, 42],
        "passive_overlay_snapshot": True,
    }])

    rendered = format_wait_text(data)

    assert rendered["text"].splitlines() == ["SAME ROW", "SAME ROW"]
    assert [
        section["delivery_ids"]
        for section in rendered["_story_render_sections"]
    ] == [[41], [42]]


def test_wait_text_keeps_equal_overlay_and_narration_in_either_order():
    overlay = {
        "type": "screen_text",
        "texts": ["SAME"],
        "overlay_delivery_ids": [41],
        "passive_overlay_snapshot": True,
    }
    narration = {"type": "narration", "text": "SAME"}

    for events in ([overlay, narration], [narration, overlay]):
        rendered = format_wait_text(build_wait_data(events))
        assert rendered["text"].splitlines() == ["SAME", "SAME"]


def test_wait_text_preserves_equal_source_occurrences_in_order():
    events = [
        {"type": "narration", "text": "SAME", "_source_seq": 11},
        {"type": "narration", "text": "SAME", "_source_seq": 12},
    ]

    rendered = format_wait_text(build_wait_data(events))

    assert rendered["text"].splitlines() == ["SAME", "SAME"]
    assert rendered["_story_render_sections"] == [
        {
            "channel": "text",
            "text": "SAME",
            "occurrence_ids": ["source:11"],
        },
        {
            "channel": "text",
            "text": "SAME",
            "occurrence_ids": ["source:12"],
        },
    ]


def test_wait_text_keeps_bridge_sequence_in_private_render_plan():
    rendered = format_wait_text(build_wait_data([{
        "type": "narration",
        "text": "Sequenced story.",
        "_source_seq": 11,
        "_seq": 47,
    }]))

    assert rendered["_story_render_sections"] == [{
        "channel": "text",
        "text": "Sequenced story.",
        "occurrence_ids": ["source:11"],
        "_bridge_seq": 47,
    }]


def test_wait_text_deduplicates_replayed_source_occurrence():
    event = {"type": "narration", "text": "ONCE", "_source_seq": 11}

    rendered = format_wait_text(build_wait_data([event, dict(event)]))

    assert rendered["text"] == "ONCE"
    assert len(rendered["_story_render_sections"]) == 1


def test_wait_text_distinguishes_source_processes_with_reused_sequence():
    events = [
        {
            "type": "narration", "text": "BEFORE",
            "_source_id": "old", "_source_seq": 1,
        },
        {
            "type": "narration", "text": "AFTER",
            "_source_id": "new", "_source_seq": 1,
        },
    ]

    rendered = format_wait_text(build_wait_data(events))

    assert rendered["text"].splitlines() == ["BEFORE", "AFTER"]
    assert [
        section["occurrence_ids"][0]
        for section in rendered["_story_render_sections"]
    ] == ["source:old:1", "source:new:1"]


def test_wait_json_does_not_expose_source_occurrence_metadata():
    data = build_wait_data([{
        "type": "narration", "text": "PUBLIC", "_source_seq": 11,
    }])

    rendered = format_wait_text(data, fmt="json")

    assert rendered["story"] == [{"type": "narration", "text": "PUBLIC"}]


def test_wait_text_keeps_stats_in_status_without_raw_event_prefix():
    data = build_wait_data([
        {
            "type": "stats_update",
            "changed": {"pc_food": 2, "appearance": 3},
            "previous": {"pc_food": 0, "appearance": 2},
        },
    ])

    rendered = format_wait_text(data)

    assert data["status"]["stats"]
    assert "pc_food" in rendered["status"]
    assert "appearance" in rendered["status"]
    assert "[stats]" not in "\n".join(str(v) for v in rendered.values())


def test_wait_text_coalesces_repeated_stats_to_final_value_and_net_delta():
    data = build_wait_data([
        {
            "type": "stats_update",
            "changed": {"aria_integrity": 100},
            "previous": {"aria_integrity": 65},
        },
        {
            "type": "stats_update",
            "changed": {"aria_integrity": 90},
            "previous": {"aria_integrity": 100},
        },
    ])

    rendered = format_wait_text(data)

    assert rendered["status"] == (
        "(stats updated: aria_integrity: 90 (+25))"
    )
    assert len(data["status"]["stats"]) == 1


def test_wait_text_renders_stat_removal_without_previous_snapshot():
    data = build_wait_data([{
        "type": "stats_update",
        "stats": {},
        "changed": {
            "aux_power_minutes": None,
            "marcus_location": None,
        },
        "removed": ["aux_power_minutes", "marcus_location"],
    }])

    rendered = format_wait_text(data)

    assert rendered["status"] == (
        "(stats updated: aux_power_minutes: removed, "
        "marcus_location: removed)"
    )
    assert "None" not in rendered["status"]


def test_wait_text_suppresses_progress_surface_teardown_diagnostics():
    data = build_wait_data([
        {
            "type": "stats_update",
            "stats": {
                "_suppress_brief": True,
                "aria_integrity": 72,
            },
            "changed": {
                "signal_strength": None,
                "marcus_location": None,
            },
            "removed": ["signal_strength", "marcus_location"],
        },
    ])

    assert "status" not in data
    assert "status" not in format_wait_text(data)

    semantic = build_wait_data([{
        "type": "stats_update",
        "stats": {
            "_suppress_brief": True,
            "evidence_count": 17,
        },
        "changed": {"evidence_count": 17},
        "removed": [],
    }])
    assert format_wait_text(semantic)["status"] == (
        "(stats updated: evidence_count: 17)")


def test_wait_text_distinguishes_null_stat_from_removed_stat():
    data = build_wait_data([{
        "type": "stats_update",
        "stats": {"optional_reading": None},
        "changed": {"optional_reading": None},
        "removed": [],
    }])

    assert format_wait_text(data)["status"] == (
        "(stats updated: optional_reading: None)"
    )


def test_wait_text_preserves_legacy_previous_only_removal_shape():
    data = build_wait_data([{
        "type": "stats_update",
        "changed": {"aux_power_minutes": None},
        "previous": {"aux_power_minutes": 5},
    }])

    assert format_wait_text(data)["status"] == (
        "(stats updated: aux_power_minutes: removed)"
    )


def test_wait_text_readded_stat_supersedes_removal_in_same_wait():
    data = build_wait_data([
        {
            "type": "stats_update",
            "stats": {},
            "changed": {"aux_power_minutes": None},
            "removed": ["aux_power_minutes"],
        },
        {
            "type": "stats_update",
            "stats": {"aux_power_minutes": 5},
            "changed": {"aux_power_minutes": 5},
            "removed": [],
        },
    ])

    assert format_wait_text(data)["status"] == (
        "(stats updated: aux_power_minutes: 5)"
    )


def test_wait_text_skips_post_terminal_store_reset_updates():
    # The bridge tags stats/inventory scrapes taken after the run ended at the
    # main menu: Ren'Py reset its store, so every stat "changed" back to its
    # default.  Rendering those deltas reads as an unearned end-of-run reset.
    data = build_wait_data([
        {
            "type": "stats_update",
            "changed": {"evidence": 0},
            "previous": {"evidence": 14},
            "post_terminal": True,
        },
        {"type": "inventory_update", "items": [], "post_terminal": True},
    ])

    assert "status" not in data

    live = build_wait_data([
        {
            "type": "stats_update",
            "changed": {"evidence": 0},
            "previous": {"evidence": 14},
        },
    ])
    assert live["status"]["stats"]


def test_format_event_hides_stats_update_unless_verbose():
    event = {
        "type": "stats_update",
        "stats": {"pc_food": 2},
        "changed": {"pc_food": 2},
        "previous": {"pc_food": 0},
    }

    assert format_event(event, colour=False, verbose=False) is None
    assert "[stats]" in format_event(event, colour=False, verbose=True)


def test_wait_text_reads_live_shim_inventory_update_shape():
    data = build_wait_data([{
        "type": "inventory_update",
        "inventory": [
            {"name": "prediction verified", "type": "evidence"},
            "sealed drive",
        ],
    }])

    assert format_wait_text(data)["status"] == (
        "[inventory updated] prediction verified, sealed drive"
    )


def test_wait_text_prefers_incremental_inventory_changes_to_full_snapshot():
    data = build_wait_data([{
        "type": "inventory_update",
        "inventory": [
            {"name": "old evidence"},
            {"name": "new evidence"},
        ],
        "changed": [{"name": "new evidence"}],
        "removed": [{"name": "spent drive"}],
    }])

    assert format_wait_text(data)["status"] == (
        "[inventory updated] new evidence, spent drive (removed)"
    )


def test_wait_text_renders_same_name_quantity_change_as_one_update():
    data = build_wait_data([{
        "type": "inventory_update",
        "inventory": [{"name": "coolant", "quantity": 2}],
        "changed": [{"name": "coolant", "quantity": 2}],
        "removed": [{"name": "coolant", "quantity": 1}],
    }])

    assert format_wait_text(data)["status"] == (
        "[inventory updated] coolant"
    )


def test_wait_text_omits_inventory_status_for_reordered_snapshot():
    data = build_wait_data([{
        "type": "inventory_update",
        "inventory": [{"name": "second"}, {"name": "first"}],
        "changed": [],
        "removed": [],
    }])

    assert "status" not in data


def test_wait_text_keeps_multiple_inventory_deltas_in_one_batch():
    data = build_wait_data([
        {
            "type": "inventory_update",
            "inventory": [{"name": "first"}],
            "changed": [{"name": "first"}],
            "removed": [],
        },
        {
            "type": "inventory_update",
            "inventory": [{"name": "first"}, {"name": "second"}],
            "changed": [{"name": "second"}],
            "removed": [],
        },
    ])

    assert format_wait_text(data)["status"] == (
        "[inventory updated] first, second"
    )


def test_wait_text_nets_item_replaced_within_one_batch():
    data = build_wait_data([
        {
            "type": "inventory_update",
            "inventory": [{"name": "prediction pending"}],
            "changed": [{"name": "prediction pending"}],
            "removed": [],
        },
        {
            "type": "inventory_update",
            "inventory": [{"name": "prediction verified"}],
            "changed": [{"name": "prediction verified"}],
            "removed": [{"name": "prediction pending"}],
        },
    ])

    assert format_wait_text(data)["status"] == (
        "[inventory updated] prediction verified"
    )


def test_wait_text_keeps_unmatched_inventory_removal():
    data = build_wait_data([{
        "type": "inventory_update",
        "inventory": [],
        "changed": [],
        "removed": [{"name": "spent drive"}],
    }])

    assert format_wait_text(data)["status"] == (
        "[inventory updated] spent drive (removed)"
    )


def test_wait_text_remove_then_readd_renders_present_item_once():
    data = build_wait_data([
        {
            "type": "inventory_update",
            "inventory": [],
            "changed": [],
            "removed": [{"name": "drive"}],
        },
        {
            "type": "inventory_update",
            "inventory": [{"name": "drive"}],
            "changed": [{"name": "drive"}],
            "removed": [],
        },
    ])

    assert format_wait_text(data)["status"] == (
        "[inventory updated] drive"
    )


def test_wait_text_legacy_snapshot_then_removal_keeps_removal_visible():
    data = build_wait_data([
        {
            "type": "inventory_update",
            "inventory": [{"name": "existing"}],
        },
        {
            "type": "inventory_update",
            "inventory": [],
            "changed": [],
            "removed": [{"name": "existing"}],
        },
    ])

    assert format_wait_text(data)["status"] == (
        "[inventory updated] existing (removed)"
    )


def test_wait_text_uses_latest_legacy_inventory_snapshot_in_one_batch():
    data = build_wait_data([
        {
            "type": "inventory_update",
            "inventory": [{"name": "stale"}],
        },
        {
            "type": "inventory_update",
            "inventory": [{"name": "current"}],
        },
    ])

    assert format_wait_text(data)["status"] == (
        "[inventory updated] current"
    )


def test_wait_text_empty_legacy_snapshot_clears_earlier_snapshot():
    data = build_wait_data([
        {
            "type": "inventory_update",
            "inventory": [{"name": "last item"}],
        },
        {
            "type": "inventory_update",
            "inventory": [],
        },
    ])

    assert "status" not in data


def test_wait_text_composes_legacy_snapshot_then_incremental_delta():
    data = build_wait_data([
        {
            "type": "inventory_update",
            "inventory": [{"name": "existing"}],
        },
        {
            "type": "inventory_update",
            "inventory": [{"name": "existing"}, {"name": "added"}],
            "changed": [{"name": "added"}],
            "removed": [],
        },
    ])

    assert format_wait_text(data)["status"] == (
        "[inventory updated] existing, added"
    )


def test_state_drops_stale_main_menu_pending():
    raw = {
        "status": "running",
        "context": {"context": "main_menu"},
        "pending_request": {
            "type": "choice_request",
            "id": "old-ending-continue",
            "choices": ["(continue)"],
        },
        "game_state": {
            "stats": {"_summary": "Day 0/40 | dusk"},
            "inventory": ["sealed evidence drive"],
            "screen_buttons": [
                {
                    "label": "Continue",
                    "screen": "menu",
                    "actions": ["LoadMostRecent"],
                },
                {
                    "label": "New Game",
                    "screen": "menu",
                    "actions": ["Start"],
                },
            ],
            "interactions": [
                {
                    "id": "menu:Continue",
                    "display_label": "Continue",
                    "screen": "menu",
                    "action_names": ["LoadMostRecent"],
                },
                {
                    "id": "menu:New Game",
                    "display_label": "New Game",
                    "screen": "menu",
                    "action_names": ["Start"],
                },
            ],
        },
        "screen": {
            "screens": ["menu"],
            "buttons": [
                {
                    "label": "Continue",
                    "screen": "menu",
                    "actions": ["LoadMostRecent"],
                },
                {
                    "label": "New Game",
                    "screen": "menu",
                    "actions": ["Start"],
                },
            ],
        },
    }

    data = build_state_data(raw)
    rendered = format_state_text(data, verbose=True)

    assert "pending" not in data
    assert "pending" not in rendered
    assert "Continue" in rendered["buttons"]
    assert "New Game" in rendered["buttons"]
    assert "stats" not in data
    assert "inventory" not in data
    assert "_stats_summary" not in data
    assert "_footer" not in rendered


def test_state_hides_run_data_before_gameplay_on_auxiliary_menu():
    raw = {
        "status": "running",
        "context": {"context": "game_menu"},
        "gameplay_seen": False,
        "game_state": {
            "stats": {"_summary": "Location: Lab", "location": "Lab"},
            "inventory": ["sealed evidence drive"],
        },
    }

    data = build_state_data(raw)
    rendered = format_state_text(data, verbose=True)

    assert "stats" not in data
    assert "inventory" not in data
    assert "_stats_summary" not in data
    assert "_footer" not in rendered


def test_state_keeps_run_data_on_auxiliary_menu_after_gameplay():
    raw = {
        "status": "running",
        "context": {"context": "game_menu"},
        "gameplay_seen": True,
        "game_state": {
            "stats": {"_summary": "Location: Lab", "location": "Lab"},
            "inventory": ["sealed evidence drive"],
        },
    }

    data = build_state_data(raw)

    assert data["stats"] == {"location": "Lab"}
    assert data["inventory"] == ["sealed evidence drive"]


def test_state_footer_omits_null_stats():
    raw = {
        "status": "running",
        "context": {"context": "in_game"},
        "game_state": {
            "stats": {
                "_summary": "Location: Lab",
                "location": "Lab",
                "marcus_location": None,
            },
        },
    }

    data = build_state_data(raw)
    rendered = format_state_text(data, verbose=True)

    assert data["stats"] == {"location": "Lab"}
    assert "None" not in rendered["_footer"]


def test_brief_state_can_suppress_raw_stats_without_hiding_inspection_data():
    raw = {
        "status": "running",
        "context": {"context": "in_game"},
        "game_state": {
            "stats": {
                "_suppress_brief": True,
                "marcus_location": "lab",
                "aux_power_minutes": 40,
            },
            "inventory": ["sealed evidence drive"],
        },
    }

    data = build_state_data(raw)

    assert data["stats"] == {
        "marcus_location": "lab",
        "aux_power_minutes": 40,
    }
    assert data["inventory"] == ["sealed evidence drive"]
    assert format_state_text(data, verbose=False) == {}
    assert "marcus_location" in format_state_text(data, verbose=True)[
        "_footer"]


def test_state_uses_fresh_main_menu_screen_over_stale_game_state():
    raw = {
        "status": "running",
        "context": {"context": "in_game"},
        "gameplay_seen": False,
        "config": {
            "auto_advance": True,
            "auto_advance_delay": 0.3,
            "end_on_menu_return": True,
        },
        "game_state": {
            "stats": {"_summary": "Evidence: 0", "evidence_count": 0},
            "screen_buttons": [],
        },
        "screen": {
            "main_menu": True,
            "screens": ["menu"],
            "buttons": [
                {"label": "Start", "screen": "menu", "actions": ["Start"]},
                {"label": "Load", "screen": "menu", "actions": ["ShowMenu"]},
                {"label": "Quit", "screen": "menu", "actions": ["Quit"]},
            ],
        },
    }

    data = build_state_data(raw)
    verbose = format_state_text(data, verbose=True)
    brief = format_state_text(data, verbose=False)

    assert data["_effective_status"] == "screen_actions"
    assert "stats" not in data
    assert "Start" in verbose["buttons"]
    assert "Load" in verbose["buttons"]
    assert "playing" not in str(verbose)
    assert "Config:" not in verbose.get("_footer", "")
    assert "Start" in brief["buttons"]
    assert "brief" not in brief


def test_verbose_state_omits_config_footer_at_defaults():
    """Default bridge config is boilerplate; it must not print a line."""
    data = build_state_data({
        "status": "running",
        "context": {"context": "in_game"},
        "gameplay_seen": True,
        "config": {
            "auto_advance": True,
            "auto_advance_delay": 0.3,
            "end_on_menu_return": True,
        },
        "game_state": {"stats": {"_summary": "Evidence: 0"}},
    })

    verbose = format_state_text(data, verbose=True)

    assert "Config:" not in verbose.get("_footer", "")


def test_verbose_state_shows_only_changed_config_keys():
    data = build_state_data({
        "status": "running",
        "context": {"context": "in_game"},
        "gameplay_seen": True,
        "config": {
            "auto_advance": False,
            "auto_advance_delay": 0.3,
            "end_on_menu_return": True,
        },
        "game_state": {"stats": {"_summary": "Evidence: 0"}},
    })

    footer = format_state_text(data, verbose=True)["_footer"]

    assert "Config: auto_advance: False" in footer
    assert "auto_advance_delay" not in footer
    assert "end_on_menu_return" not in footer


def test_verbose_state_config_footer_lists_every_deviation():
    data = build_state_data({
        "status": "running",
        "context": {"context": "in_game"},
        "gameplay_seen": True,
        "config": {
            "auto_advance": True,
            "auto_advance_delay": 1.5,
            "end_on_menu_return": False,
            "game_specific_knob": "on",
        },
        "game_state": {"stats": {"_summary": "Evidence: 0"}},
    })

    footer = format_state_text(data, verbose=True)["_footer"]

    assert "auto_advance_delay: 1.5" in footer
    assert "end_on_menu_return: False" in footer
    # Unknown keys have no known default, so they always show.
    assert "game_specific_knob: on" in footer
    # The one key still at its default stays out.
    assert "auto_advance: True" not in footer


def test_config_footer_suppressed_with_details():
    data = build_state_data({
        "status": "running",
        "context": {"context": "in_game"},
        "gameplay_seen": True,
        "config": {"auto_advance": False},
        "game_state": {"stats": {"_summary": "Evidence: 0"}},
    })

    rendered = format_state_text(data, verbose=True, include_details=False)

    assert "Config:" not in rendered.get("_footer", "")


def test_brief_state_fallback_uses_effective_status():
    rendered = format_state_text(
        {"status": "running", "_effective_status": "menu"},
        verbose=False,
    )

    assert rendered["brief"] == "status: menu"


def test_pre_game_state_reports_starting_until_first_screen_arrives():
    raw = {
        "status": "running",
        "context": {"context": "in_game"},
        "gameplay_seen": False,
        "config": {
            "auto_advance": True,
            "auto_advance_delay": 0.3,
            "end_on_menu_return": True,
        },
        "game_state": {"screen_buttons": []},
        "transcript": [
            {"type": "context", "context": "in_game", "_seq": 1},
        ],
    }

    data = build_state_data(raw)
    verbose = format_state_text(data, verbose=True)
    brief = format_state_text(data, verbose=False)

    assert data["_effective_status"] == "starting"
    assert verbose["status"] == "starting"
    assert brief["brief"] == "status: starting"


def test_game_ended_terminal_renders_ended_line():
    event = {"type": "game_ended", "reason": "quit"}
    rendered = format_event(event, colour=False)
    assert rendered == "X Game ended (quit)"


def test_game_ended_unannotated_menu_return_still_reads_as_ended():
    # No annotation == terminal (backward compatible with old logs).
    event = {"type": "game_ended", "reason": "return_to_menu"}
    rendered = format_event(event, colour=False)
    assert "Game ended" in rendered


def test_game_resumed_is_visible_only_in_verbose_output():
    event = {"type": "game_resumed", "reason": "rollback"}
    assert format_event(event, verbose=False, colour=False) is None
    assert format_event(event, verbose=True, colour=False) == (
        "Game resumed (rollback)")


def test_suppressed_game_ended_renders_hint_not_ended_phrase():
    # A menu return the bridge ruled non-terminal (end_on_menu_return opt-out)
    # must NOT render "Game ended": that phrase trips downstream terminal
    # heuristics into a false auto-end. Render an agent-facing hint instead.
    event = {"type": "game_ended", "reason": "return_to_menu",
             "terminal": False, "suppressed": "end_on_menu_return"}
    rendered = format_event(event, colour=False)
    assert rendered is not None
    low = rendered.lower()
    # None of the harness terminal phrases may appear.
    for phrase in ("game ended", "end the game", "the end", "credits"):
        assert phrase not in low, f"leaked terminal phrase {phrase!r}: {rendered!r}"
    # The agent still learns what happened and how to continue.
    assert "main menu" in low
    assert "continue" in low or "load" in low


def test_launch_menu_transition_does_not_read_as_a_return_from_gameplay():
    event = {"type": "game_ended", "reason": "return_to_menu",
             "terminal": False, "suppressed": "no_gameplay_seen"}

    rendered = format_event(event, colour=False)

    assert rendered == "Reached the main menu before gameplay began."
    assert "Game ended" not in rendered
    assert "Returned" not in rendered


def test_input_request_preserves_prompt_text_from_shim_or_mod():
    event = {"type": "input_request", "prompt": "Which place are you asking about?"}

    assert format_event(event, colour=False) == (
        "--- INPUT REQUIRED: Which place are you asking about? ---"
    )


def test_pending_numbered_choice_count_excludes_disabled_either_naming():
    # The count must exclude disabled/caption under BOTH key families so a
    # synthesized raw pending (is_disabled) can't be miscounted as enabled.
    from vnflight.format import pending_numbered_choice_count
    # Structured shape.
    assert pending_numbered_choice_count({
        "choices": [
            {"label": "A", "index": 1},
            {"label": "B (locked)", "index": None, "disabled": True},
            {"label": "prompt", "index": None, "caption": True},
            {"label": "C", "index": 2},
        ],
    }) == 2
    # Raw/synthesized shape with is_disabled / is_caption keys.
    assert pending_numbered_choice_count({
        "choices": [
            {"label": "A", "is_disabled": False},
            {"label": "B", "is_disabled": True},
            {"label": "prompt", "is_caption": True},
            {"label": "C"},
        ],
    }) == 2


def test_suppressed_pending_choice_is_not_rendered_or_numbered():
    from vnflight.format import pending_numbered_choice_count

    raw = {
        "type": "choice_request",
        "choices": [
            {
                "label": "(quintus1 preset)",
                "_suppress_pending_choice": True,
            },
        ],
        "full_items": [
            {
                "label": "(quintus1 preset)",
                "_suppress_pending_choice": True,
            },
        ],
        "interactions": [
            {
                "type": "button",
                "label": "Ask about the town.",
                "display_label": "Ask about the town.",
                "category": "topics",
            },
        ],
    }

    pending = _build_pending(raw)
    rendered = format_pending_text(pending)

    assert pending["choices"] == []
    assert pending_numbered_choice_count(pending) == 0
    assert "(quintus1 preset)" not in rendered
    assert "1: Ask about the town." in rendered


def test_build_pending_choice_id_aligns_past_disabled_row():
    # A disabled row before an enabled choice must not shift augmenter ids
    # onto the wrong option (synthesized pending: choices == full_items).
    from vnflight.format import _build_pending
    raw = {
        "type": "choice_request",
        "choices": [
            {"label": "Pay the toll", "is_disabled": True, "id": "toll"},
            {"label": "Sneak past", "id": "sneak"},
        ],
        "full_items": [
            {"label": "Pay the toll", "is_disabled": True},
            {"label": "Sneak past", "is_disabled": False},
        ],
    }
    pending = _build_pending(raw)
    enabled = [c for c in pending["choices"] if c.get("index") is not None]
    assert len(enabled) == 1
    # The id must follow the enabled choice "Sneak past", not the disabled row.
    assert enabled[0]["label"] == "Sneak past"
    assert enabled[0].get("id") == "sneak"


def test_caption_only_pending_is_not_actionable():
    # A caption carries disabled=False but is prompt text, not a choice.
    # The actionability predicates must treat it as non-actionable,
    # consistent with format_pending_text's has_enabled.
    from vnflight.format import _has_actionable_wait_pending
    caption_only = {
        "type": "choice",
        "choices": [
            {"label": "What do you do?", "index": None, "caption": True},
            {"label": "Locked (need key)", "index": None, "disabled": True},
        ],
    }
    assert _has_actionable_wait_pending(caption_only) is False
    with_enabled = {
        "type": "choice",
        "choices": [
            {"label": "What do you do?", "index": None, "caption": True},
            {"label": "Fight", "index": 1},
        ],
    }
    assert _has_actionable_wait_pending(with_enabled) is True


def test_button_matching_disabled_choice_label_is_kept():
    # The choice/button dedup must only shadow buttons that match a
    # *numbered* (enabled) choice — a button sharing a disabled choice's
    # label is still a real action and must survive.
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "choices": ["Go north"],
            "full_items": [
                {"label": "Go north", "is_disabled": False},
                {"label": "Force the door", "is_disabled": True},
            ],
        },
        "game_state": {
            "type": "game_state",
            "choices": ["Go north"],
            "screen_buttons": [
                {"label": "Force the door", "screen": "nvl",
                 "actions": ["Jump"], "_category": "topics"},
            ],
        },
        "screen": {"button_categories": {"topics": {"header": "TOPICS"}}},
    }
    data = build_state_data(raw)
    btn_labels = [b.get("label") for b in data.get("buttons", [])]
    assert "Force the door" in btn_labels  # not dropped by the dedup


def test_build_wait_data_preserves_pending_input_prompt():
    data = build_wait_data([], {
        "type": "input_request",
        "prompt": "Which place are you asking about?",
    })

    assert data["pending"]["prompt"] == "Which place are you asking about?"


def test_wait_text_drops_screen_text_duplicate_of_input_prompt():
    prompt = "Which place are you asking about?"
    data = build_wait_data([
        {"type": "screen_text", "texts": [prompt]},
    ], {
        "type": "input_request",
        "prompt": prompt,
    })
    rendered = format_wait_text(data)

    assert "text" not in rendered
    assert "screen_text" not in rendered
    assert rendered["pending"].count(prompt) == 1


def test_state_text_drops_screen_text_duplicate_of_input_prompt():
    prompt = "Which place are you asking about?"
    rendered = format_state_text({
        "pending": {
            "type": "input",
            "prompt": prompt,
        },
        "_screen_texts": [prompt],
    })

    assert "text" not in rendered
    assert rendered["pending"].count(prompt) == 1


def test_build_wait_data_drops_menu_prompt_echo_before_choice():
    pending = {
        "type": "choice_request",
        "id": "menu",
        "choices": ["Go inside"],
    }
    data = build_wait_data([
        {"type": "dialogue", "character": "Alex", "text": "What do you do?"},
        pending,
    ], pending=pending)

    assert data["story"] == []
    assert data["pending"]["choices"][0]["label"] == "Go inside"


def test_build_wait_data_keeps_real_dialogue_before_prompt_echo():
    pending = {
        "type": "choice_request",
        "id": "drink",
        "choices": ["Surprise me"],
    }
    data = build_wait_data([
        {
            "type": "dialogue",
            "character": "Rosemary",
            "text": "Charming? I suppose so.",
        },
        {
            "type": "dialogue",
            "character": "Rosemary",
            "text": "Rosemary waits for your order.",
        },
        pending,
    ], pending=pending)

    assert data["story"] == [{
        "type": "dialogue",
        "character": "Rosemary",
        "text": "Charming? I suppose so.",
    }]
    assert data["pending"]["choices"][0]["label"] == "Surprise me"


def test_build_wait_data_drops_embedded_menu_prompt_echo():
    pending = {
        "type": "choice_request",
        "id": "marcus",
        "choices": ["Here. All of it."],
    }
    data = build_wait_data([
        {
            "type": "dialogue",
            "character": "Dr. Chen",
            "text": "Marcus is waiting. What do I do?",
        },
        pending,
    ], pending=pending)

    assert data["story"] == []
    assert data["pending"]["choices"][0]["label"] == "Here. All of it."


def test_event_format_unescapes_screen_content_percent_literals():
    rendered = format_event({
        "type": "screen_content",
        "texts": ["Signal: 80%%"],
        "screens": ["observatory_hud"],
    }, colour=False)

    assert "Signal: 80%" in rendered
    assert "80%%" not in rendered


def test_format_event_labels_disabled_only_choices_as_blocked():
    rendered = format_event({
        "type": "choice_request",
        "choices": [],
        "full_items": [
            {"label": "We ride forward. (disabled)", "is_disabled": True},
        ],
    }, colour=False)

    assert "--- NO AVAILABLE CHOICES ---" in rendered
    assert "Use act(number)" not in rendered
    assert "No enabled choices" in rendered
    assert "- We ride forward." in rendered


def test_format_event_empty_interactions_suppress_stale_promoted_buttons():
    rendered = format_event({
        "type": "choice_request",
        "choices": ["Live choice."],
        "interactions": [],
        "promoted_buttons": [
            {"label": "[spell]", "annotation": "stale class toggle"},
        ],
    }, colour=False)

    assert 'act "[spell]"' not in rendered
    assert "Live choice." in rendered


def test_format_pending_text_labels_disabled_only_choices_as_blocked():
    rendered = format_pending_text({
        "type": "choice",
        "id": "blocked",
        "choices": [
            {"label": "We ride forward.", "disabled": True, "index": None},
        ],
    })

    assert "--- NO AVAILABLE CHOICES ---" in rendered
    assert "Use act(number)" not in rendered
    assert "No enabled choices" in rendered


def test_build_wait_data_treats_disabled_suffix_choice_as_disabled():
    data = build_wait_data([], {
        "type": "choice_request",
        "id": "blocked",
        "choices": [
            "[knowledge] Pick the mushroom at noon. (disabled)",
        ],
        "full_items": [
            {
                "label": "[knowledge] Pick the mushroom at noon. (disabled)",
                "is_disabled": False,
            },
        ],
    })

    choice = data["pending"]["choices"][0]
    assert choice["disabled"] is True
    assert choice["index"] is None
    assert "--- NO AVAILABLE CHOICES ---" in format_wait_text(data)["pending"]


def test_latest_screen_buttons_prefers_live_screen_over_stale_transcript():
    live = {
        "type": "screen_content",
        "buttons": [{"label": "Drink the potion", "screen": "menu"}],
    }
    stale = {
        "type": "screen_content",
        "buttons": [{"label": "Inventory", "screen": "quick_menu"}],
    }

    result = _get_latest_screen_buttons(FakeScreenSession(live, [stale]))

    assert result["buttons"][0]["label"] == "Drink the potion"


@pytest.mark.parametrize("live_available", [False, True])
@pytest.mark.parametrize("screens", [[], ["nvl", "hud"]])
def test_latest_screen_without_buttons_retires_old_modal(live_available, screens):
    old = {"type": "screen_content", "screens": ["audit"],
           "modal_screens": ["audit"], "buttons": [{"label": "Read file"}]}
    current = {"type": "screen_content", "screens": screens, "texts": []}
    session = FakeScreenSession(current if live_available else None, [old, current])
    assert _get_latest_screen_buttons(session) == current


def test_latest_screen_ignores_lightweight_say_progress():
    modal = {"type": "screen_content", "screens": ["audit"],
             "buttons": [{"label": "Read file"}]}
    partial = {"type": "screen_content", "_lightweight": True,
               "texts": ["Still typing"], "screens": ["say"]}
    assert _get_latest_screen_buttons(FakeScreenSession(None, [modal, partial])) == modal


@pytest.mark.parametrize("live_available", [False, True])
def test_latest_interactions_do_not_revive_closed_modal(live_available):
    old = {"type": "screen_content", "screens": ["audit"],
           "interactions": [{"kind": "choice", "label": "Old audit choice", "index": 0}]}
    current = {"type": "screen_content", "screens": ["nvl", "hud"], "texts": []}
    pending = {"interactions": [{"kind": "choice", "label": "Leave lab", "index": 0}]}
    session = FakeScreenSession(current if live_available else None, [old, current])
    assert _get_latest_interactions(session, pending=pending) == pending["interactions"]
    assert _get_latest_interactions(session) == []


def test_latest_interactions_prefers_live_overlay_interactions():
    live = {
        "type": "screen_content",
        "buttons": [{"label": "Drink the potion", "screen": "menu"}],
        "interactions": [
            {
                "source": "button",
                "type": "nav",
                "display_label": "Drink the potion",
                "action_names": ["SetField"],
            },
        ],
    }
    pending = {
        "type": "choice_request",
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "I approach Foggy.",
            },
        ],
    }

    result = _get_latest_interactions(
        FakeScreenSession(live),
        pending,
        overlay_active=True,
    )

    assert result[0]["display_label"] == "Drink the potion"
    assert result[0]["type"] == "items"


def test_latest_interactions_prefers_live_mixed_overlay_interactions():
    live = {
        "type": "screen_content",
        "overlay_active": True,
        "interactions": [
            {
                "source": "button",
                "type": "choice",
                "display_label": "“Forget it.”",
                "index": 1,
            },
            {
                "source": "button",
                "category": "shop",
                "type": "other",
                "display_label": "[Sell: Bronze Rod]",
                "index": 10,
            },
        ],
    }
    pending = {
        "type": "choice_request",
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "“Forget it.”",
                "index": 1,
            },
            {
                "source": "button",
                "category": "shop",
                "type": "other",
                "display_label": "[Sell: Bronze Rod]",
                "index": 11,
            },
            {
                "source": "button",
                "type": "other",
                "display_label": "You sold the iron scraps.\n+7",
                "index": 2,
            },
        ],
    }

    result = _get_latest_interactions(
        FakeScreenSession(live),
        pending,
        overlay_active=False,
    )

    assert [i["display_label"] for i in result] == [
        "“Forget it.”",
        "[Sell: Bronze Rod]",
    ]
    assert result[1]["index"] == 10


def test_latest_interactions_prefers_live_choices_when_pending_is_stale():
    live = {
        "type": "screen_content",
        "interactions": [
            {
                "source": "button",
                "type": "choice",
                "display_label": "I approach Foggy.",
                "disabled": False,
            },
            {
                "source": "button",
                "type": "choice",
                "display_label": "[cost] I go downstairs, to the alchemy set.",
                "disabled": False,
            },
        ],
    }
    pending = {
        "type": "choice_request",
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "I approach Foggy.",
                "disabled": False,
            },
            {
                "source": "choice",
                "type": "choice",
                "display_label": "I’m too exhausted to brew potions.",
                "disabled": True,
            },
        ],
    }

    result = _get_latest_interactions(
        FakeScreenSession(live),
        pending,
        overlay_active=False,
    )

    labels = [i["display_label"] for i in result]
    assert "[cost] I go downstairs, to the alchemy set." in labels


def test_latest_interactions_keeps_pending_when_live_choices_are_subset():
    live = {
        "type": "screen_content",
        "interactions": [
            {
                "source": "button",
                "type": "choice",
                "display_label": "[chance] I take my axe.",
                "disabled": False,
            },
            {
                "source": "button",
                "type": "choice",
                "display_label": "I grab my crossbow.",
                "disabled": False,
            },
        ],
    }
    pending = {
        "type": "choice_request",
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "[chance] I take my axe.",
                "disabled": False,
            },
            {
                "source": "choice",
                "type": "choice",
                "display_label": "Getting through them should be easy enough.",
                "disabled": False,
            },
            {
                "source": "choice",
                "type": "choice",
                "display_label": "I grab my crossbow.",
                "disabled": False,
            },
        ],
    }

    result = _get_latest_interactions(
        FakeScreenSession(live),
        pending,
        overlay_active=False,
    )

    labels = [i["display_label"] for i in result]
    assert "Getting through them should be easy enough." in labels


def test_latest_interactions_refreshes_live_nav_with_pending_choices():
    live = {
        "type": "screen_content",
        "interactions": [
            {
                "source": "button",
                "type": "nav",
                "display_label": "Inventory",
                "disabled": False,
            },
            {
                "source": "button",
                "type": "nav",
                "display_label": "Sleep",
                "disabled": False,
            },
        ],
    }
    pending = {
        "type": "choice_request",
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "I keep watch.",
                "disabled": False,
            },
            {
                "source": "button",
                "type": "nav",
                "display_label": "Settings",
                "disabled": False,
            },
        ],
    }

    result = _get_latest_interactions(
        FakeScreenSession(live),
        pending,
        overlay_active=False,
    )

    labels = [i["display_label"] for i in result]
    assert "I keep watch." in labels
    assert "Sleep" in labels
    assert "Settings" not in labels


def test_latest_interactions_marks_nullaction_buttons_disabled():
    live = {
        "type": "screen_content",
        "interactions": [
            {
                "source": "button",
                "type": "shop",
                "display_label": "Buy Marshbules: ? (too expensive)",
                "action_names": ["NullAction"],
                "disabled": False,
            },
        ],
    }

    result = _get_latest_interactions(FakeScreenSession(live))

    assert result[0]["disabled"] is True


def test_latest_interactions_treats_empty_live_interactions_as_authoritative():
    live = {
        "type": "screen_content",
        "interactions": [],
    }
    stale = {
        "type": "screen_content",
        "interactions": [
            {
                "source": "button",
                "type": "other",
                "display_label": "[spell]",
                "promoted": True,
            },
        ],
    }
    pending = {
        "type": "choice_request",
        "interactions": [
            {
                "source": "button",
                "type": "other",
                "display_label": "[spell]",
                "promoted": True,
            },
        ],
    }

    result = _get_latest_interactions(
        FakeScreenSession(live, [stale]),
        pending,
    )

    assert result == []


def test_categorized_buttons_mark_disabled_noncompact_items():
    text = format_categorized_buttons({
        "shop": [
            {
                "label": "Buy Marshbules: ? (too expensive)",
                "index": 3,
                "disabled": True,
            },
        ],
    })

    assert "-: Buy Marshbules: ? (too expensive) (disabled)" in text


def test_disabled_annotation_follows_single_disabled_marker():
    text = format_categorized_buttons({
        "shop": [{
            "label": "Locked",
            "annotation": "need key",
            "disabled": True,
        }],
    })

    assert "Locked (disabled) — need key" in text
    assert text.count("(disabled)") == 1


def test_interactions_use_explicit_category_over_generic_type():
    text = format_interactions(
        [
            {
                "category": "shop",
                "type": "other",
                "index": 12,
                "display_label": "[Sell: Bronze Rod]",
            },
        ],
        colour=False,
    )

    assert "--- SHOP ---" in text
    assert "--- OTHER BUTTONS ---" not in text


def test_format_interactions_uses_registered_category_headers():
    text = format_interactions(
        [
            {
                "category": "castle",
                "type": "other",
                "index": 12,
                "display_label": "Garden",
            },
            {
                "category": "options",
                "type": "other",
                "index": 13,
                "display_label": "Mood",
            },
        ],
        colour=False,
        extra_categories={
            "castle": {"header": "CASTLE", "compact": False},
            "options": {"header": "OPTIONS", "compact": False},
        },
    )

    assert "--- CASTLE ---" in text
    assert "--- OPTIONS ---" in text
    assert text.index("--- CASTLE ---") < text.index("--- OPTIONS ---")
    assert "--- OTHER BUTTONS ---" not in text


def test_format_interactions_classifies_choice_type_without_category():
    text = format_interactions(
        [
            {
                "index": 1,
                "type": "choice",
                "display_label": "Go",
                "disabled": False,
            },
        ],
        colour=False,
    )

    assert "--- CHOICES ---" in text
    assert "--- OTHER BUTTONS ---" not in text


def test_format_interactions_classifies_return_button_as_clickable_navigation():
    text = format_interactions(
        [
            {
                "source": "button",
                "type": "choice",
                "index": 11,
                "screen": "menu",
                "action_names": ["Return"],
                "display_label": "Return",
                "disabled": False,
            },
        ],
        colour=False,
    )

    assert "--- NAVIGATION ---" in text
    assert 'Use: act "<label>"' in text
    assert "--- CHOICES ---" not in text
    assert "Use: choose" not in text
    assert "Use: click" not in text


def test_format_interactions_keeps_choice_return_button_as_choice():
    text = format_interactions(
        [
            {
                "source": "button",
                "type": "choice",
                "index": 1,
                "screen": "nvl",
                "action_names": ["ChoiceReturn"],
                "display_label": "Take it.",
                "disabled": False,
            },
        ],
        colour=False,
    )

    assert "--- CHOICES ---" in text
    assert "Use: act <N>" in text


def test_format_interactions_marks_disabled_compact_navigation():
    text = format_interactions(
        [
            {
                "category": "navigation",
                "type": "nav",
                "index": None,
                "screen": "quick_menu",
                "action_names": ["NullAction"],
                "display_label": "Sleep",
                "disabled": True,
            },
            {
                "category": "navigation",
                "type": "nav",
                "index": 1,
                "screen": "quick_menu",
                "action_names": ["Show"],
                "display_label": "Map",
                "disabled": False,
            },
        ],
        colour=False,
    )

    assert "Sleep (disabled)" in text
    assert "Map" in text


def test_mundanejob_interactions_render_as_options():
    text = format_interactions(
        [
            {
                "screen": "mundanejob",
                "type": "other",
                "index": 1,
                "display_label": "Close",
                "action_names": ["Hide"],
            },
            {
                "screen": "mundanejob",
                "type": "other",
                "index": 2,
                "display_label": "Work for 2",
                "action_names": ["Hide", "Jump"],
            },
        ],
        colour=False,
    )

    assert "--- OPTIONS ---" in text
    assert "--- OTHER BUTTONS ---" not in text


def test_info_interaction_does_not_consume_visible_topic_number():
    interactions = [
        {
            "source": "button",
            "type": "info",
            "category": "info",
            "index": 1,
            "display_label": "A contextual hint.",
            "action_names": ["NullAction"],
        },
        {
            "source": "button",
            "type": "topic",
            "category": "topics",
            "index": 2,
            "display_label": "Ask about herbs.",
            "action_names": ["SetField", "Jump"],
        },
    ]

    text = format_interactions(interactions, colour=False)

    assert "  1: Ask about herbs." in text
    assert "  2: Ask about herbs." not in text
    assert text.index("--- TOPICS ---") < text.index("--- INFO ---")
    assert _match_interaction("1", interactions)["display_label"] == "Ask about herbs."


def test_choice_request_interaction_numbers_continue_after_choices():
    event = {
        "type": "choice_request",
        "choices": ["Go north", "Go south"],
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "index": 1,
                "display_label": "Go north",
            },
            {
                "source": "choice",
                "type": "choice",
                "index": 2,
                "display_label": "Go south",
            },
            {
                "source": "button",
                "type": "topic",
                "category": "topics",
                "index": 3,
                "display_label": "Ask about herbs.",
                "action_names": ["SetField", "Jump"],
            },
        ],
    }

    text = format_event(event, colour=False)

    assert "  1: Go north" in text
    assert "  2: Go south" in text
    assert "  3: Ask about herbs." in text
    assert "  1: Ask about herbs." not in text
    non_choice = [i for i in event["interactions"] if i.get("type") != "choice"]
    assert _match_interaction(
        "3",
        non_choice,
        display_index_offset=2,
    )["display_label"] == "Ask about herbs."


# ---------------------------------------------------------------------------
# _resolve_label_interaction — Fleet R66's label-precedence defect
#
# act(target="KIT") silently fuzzy-matched an unrelated story choice
# ("The signal analysis toolkit...") because the substring tier accepted any
# lone hit with no regard for what else was on the surface.  The fix:
# category-aware precedence (exact beats fuzzy, one category beats a tie
# across categories) that refuses with candidates instead of guessing.
# ---------------------------------------------------------------------------

def _r66_interactions():
    """A KIT button and an unrelated story choice mentioning 'toolkit',
    reconstructed from the fleet R66 debrief (echo66-04)."""
    return [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": (
             "The signal analysis toolkit — pull the waveform apart "
             "layer by layer.")},
        {"source": "choice", "type": "choice", "index": 2,
         "display_label": (
             "The spacetime equations — if this is temporal, there "
             "must be a mechanism.")},
        {"source": "choice", "type": "choice", "index": 3,
         "display_label": "ARIA's source logs — whatever this is, "
                           "it came through our hardware."},
        {"id": "hud:KIT", "source": "button", "type": "other", "index": 4,
         "display_label": "KIT"},
    ]


def test_exact_button_wins_over_fuzzy_substring_choice_match():
    """R66: an exact KIT button must win outright; fuzzy never even runs."""
    match = _resolve_label_interaction("KIT", _r66_interactions())

    assert match.matched is not None
    assert match.matched["id"] == "hud:KIT"
    assert match.ambiguous is None


@pytest.mark.parametrize("target", ["KIT", "LOG", "The"])
def test_short_target_requires_exact_hit_when_control_is_absent(target):
    interactions = _r66_interactions()[:3]
    match = _resolve_label_interaction(target, interactions)
    assert match.matched is None
    assert match.ambiguous is None
    choices = [{"label": row["display_label"]} for row in interactions]
    assert _match_choice_target(target, choices) is None


def test_long_dialogue_fragment_still_resolves_without_control():
    match = _resolve_label_interaction("signal analysis", _r66_interactions()[:3])
    assert match.matched["index"] == 1


@pytest.mark.parametrize("target", ["Yes", "No", "KIT"])
def test_short_exact_choice_and_id_remain_actionable(target):
    choices = [{"id": "x", "label": target}]
    assert _match_choice_target(target, choices) == ("x", target)
    assert _match_choice_target("x", choices) == ("x", target)
    assert _match_choice_target("1", choices) == ("x", target)


def test_fuzzy_match_refuses_when_it_reaches_more_than_one_interaction():
    """Without an exact hit, a fuzzy target must resolve to exactly one
    candidate or refuse -- never guess among several."""
    interactions = [
        {"source": "button", "type": "other", "index": 1,
         "display_label": "HABITAT"},
        {"source": "button", "type": "other", "index": 2,
         "display_label": "HABITAT LOG"},
    ]

    match = _resolve_label_interaction("habi", interactions)

    assert match.matched is None
    assert match.ambiguous is not None
    assert {itr["display_label"] for itr in match.ambiguous} == {
        "HABITAT", "HABITAT LOG",
    }


def test_exact_story_choice_label_still_resolves():
    """A real, unambiguous exact choice label must still act normally."""
    match = _resolve_label_interaction(
        "ARIA's source logs — whatever this is, it came through our "
        "hardware.",
        _r66_interactions(),
    )

    assert match.matched is not None
    assert match.matched["index"] == 3
    assert match.ambiguous is None


def test_exact_match_tolerates_missing_trailing_punctuation():
    """Case/whitespace/trailing-punctuation folding: a target typed
    without the choice's own trailing period is still an exact hit, not a
    fuzzy one -- so it does not need to compete for uniqueness."""
    interactions = [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Grab the toolkit."},
    ]

    match = _resolve_label_interaction("Grab the toolkit", interactions)

    assert match.matched is not None
    assert match.matched["index"] == 1
    assert match.ambiguous is None


def test_exact_match_ties_across_categories_refuse_instead_of_guessing():
    """A target that exactly matches BOTH a button and a story choice must
    refuse with both candidates rather than picking one by category."""
    interactions = [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Close the hatch."},
        {"id": "hud:close", "source": "button", "type": "other", "index": 2,
         "display_label": "Close the hatch."},
    ]

    match = _resolve_label_interaction("Close the hatch", interactions)

    assert match.matched is None
    assert match.ambiguous is not None
    assert len(match.ambiguous) == 2
    assert {itr["source"] for itr in match.ambiguous} == {"choice", "button"}


def test_hidden_choice_behind_modal_lets_a_same_named_panel_button_win():
    """The modal exception: a story choice a panel covers stays in the raw
    interaction list for hidden-menu bookkeeping, but it is not really on
    the surface, so an exact tie with a visible panel control resolves to
    the control instead of refusing."""
    interactions = [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Call Marcus"},
        {"id": "evidence_screen:Call Marcus", "source": "button",
         "type": "other", "index": 1, "display_label": "Call Marcus"},
        {"id": "evidence_screen:CLOSE", "source": "button",
         "type": "other", "index": 2, "display_label": "CLOSE"},
    ]

    match = _resolve_label_interaction(
        "Call Marcus", interactions, {"call marcus"})

    assert match.matched is not None
    assert match.matched["id"] == "evidence_screen:Call Marcus"
    assert match.ambiguous is None

    # A target that reaches ONLY the hidden choice still resolves to
    # nothing -- the caller's own hidden-menu refusal names the panel.
    only_hidden = _resolve_label_interaction(
        "Call Marcus",
        interactions[:1],
        {"call marcus"},
    )
    assert only_hidden.matched is None
    assert only_hidden.ambiguous is None


def test_class_toggle_button_does_not_suppress_pending_choices():
    pending = {
        "type": "choice_request",
        "choices": ["I grab my pearl pendant. [Cost: 1]"],
    }
    screen = {
        "type": "screen_content",
        "screens": ["nvl", "quick_menu"],
        "buttons": [
            {
                "label": "[spell]",
                "screen": "nvl",
                "actions": ["SetField"],
                "action_strs": ["SetField field=at value=spell"],
            },
        ],
    }

    assert _has_choice_overlay(screen, pending) is False


def test_state_pending_includes_promoted_reveal_actions():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "story",
            "choices": ["Tell a plain story."],
            "interactions": [
                {
                    "source": "choice",
                    "type": "choice",
                    "display_label": "Tell a plain story.",
                    "id": "1",
                },
                {
                    "source": "button",
                    "type": "other",
                    "display_label": "[knowledge]",
                    "id": "nvl:[knowledge]",
                    "annotation": (
                        "class ability toggle — click to reveal/hide "
                        "class-specific choices, then re-read"
                    ),
                    "promoted": True,
                },
            ],
        },
    }

    data = build_state_data(raw)

    assert data["pending"]["actions"] == [
        {
            "label": "[knowledge]",
            "annotation": (
                "class ability toggle — click to reveal/hide "
                "class-specific choices, then re-read"
            ),
            "id": "nvl:[knowledge]",
        }
    ]
    rendered = format_state_text(data, verbose=True)
    assert 'act "[knowledge]" — class ability toggle' in rendered["pending"]


def test_state_pending_uses_game_state_promoted_actions():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "story",
            "choices": ["Stale choice."],
        },
        "game_state": {
            "choices": ["Live choice."],
            "interactions": [
                {
                    "source": "choice",
                    "type": "choice",
                    "display_label": "Live choice.",
                    "id": "1",
                },
                {
                    "source": "button",
                    "type": "other",
                    "display_label": "[spell]",
                    "annotation": "class ability toggle",
                    "promoted": True,
                },
            ],
        },
    }

    data = build_state_data(raw)

    assert data["pending"]["choices"][0]["label"] == "Live choice."
    assert data["pending"]["actions"] == [
        {"label": "[spell]", "annotation": "class ability toggle"}
    ]
    assert 'act "[spell]" — class ability toggle' in format_state_text(
        data,
        verbose=True,
    )["pending"]


def test_wait_pending_includes_non_choice_interaction_actions():
    pending = {
        "type": "choice_request",
        "id": "story",
        "choices": ["Stay here", "Leave"],
        "interactions": [
            {
                "source": "choice",
                "type": "choice",
                "display_label": "Stay here",
            },
            {
                "source": "choice",
                "type": "choice",
                "display_label": "Leave",
            },
            {
                "source": "button",
                "type": "topic",
                "category": "topics",
                "display_label": "Ask about the town",
                "id": "topic:town",
            },
        ],
    }

    data = build_wait_data([], pending)

    assert data["pending"]["actions"] == [
        {
            "label": "Ask about the town",
            "type": "topic",
            "category": "topics",
            "index": 3,
            "id": "topic:town",
        }
    ]
    # Non-choice actions render grouped under their category header (matching
    # state()), not as bare `act "label"` lines.
    _rendered = format_state_text(
        {"pending": data["pending"]},
        verbose=True,
    )["pending"]
    assert "--- TOPICS ---" in _rendered
    assert "Ask about the town" in _rendered
    assert 'act "Ask about the town"' not in _rendered


def test_state_pending_ignores_stale_promoted_buttons_with_live_interactions():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "story",
            "choices": ["Stale choice."],
            "interactions": [
                {
                    "source": "button",
                    "type": "other",
                    "display_label": "[spell]",
                    "annotation": "stale class toggle",
                    "promoted": True,
                },
            ],
            "promoted_buttons": [
                {"label": "[spell]", "annotation": "stale class toggle"},
            ],
        },
        "game_state": {
            "choices": ["Live choice."],
            "interactions": [
                {
                    "source": "choice",
                    "type": "choice",
                    "display_label": "Live choice.",
                    "id": "1",
                },
                {
                    "source": "button",
                    "type": "other",
                    "display_label": "[0]",
                    "hidden": True,
                },
            ],
            "promoted_buttons": [
                {"label": "[spell]", "annotation": "stale class toggle"},
            ],
        },
    }

    data = build_state_data(raw)

    assert data["pending"]["choices"][0]["label"] == "Live choice."
    assert "actions" not in data["pending"]
    assert 'act "[spell]"' not in format_state_text(
        data,
        verbose=True,
    )["pending"]


def test_state_pending_treats_empty_live_interactions_as_authoritative():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "story",
            "choices": ["Stale choice."],
            "promoted_buttons": [
                {"label": "[spell]", "annotation": "stale class toggle"},
            ],
        },
        "game_state": {
            "choices": ["Live choice."],
            "interactions": [],
            "promoted_buttons": [
                {"label": "[spell]", "annotation": "stale class toggle"},
            ],
        },
    }

    data = build_state_data(raw)

    assert data["pending"]["choices"][0]["label"] == "Live choice."
    assert "actions" not in data["pending"]
    assert 'act "[spell]"' not in format_state_text(
        data,
        verbose=True,
    )["pending"]


def test_state_pending_empty_live_interactions_suppress_stale_choices():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "story",
            "choices": ["Stale choice."],
        },
        "game_state": {
            "interactions": [],
        },
    }

    data = build_state_data(raw)

    assert data["pending"]["choices"] == []
    assert "Stale choice." not in format_state_text(
        data,
        verbose=True,
    )["pending"]


def test_state_pending_non_choice_live_interactions_suppress_stale_choices():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "tutorial",
            "choices": ["Tell me more", "I've played this before"],
            "full_items": [
                {"label": "Tell me more"},
                {"label": "I've played this before"},
            ],
        },
        "game_state": {
            "interactions": [
                {
                    "id": "sidebar:Classes",
                    "display_label": "Classes",
                    "type": "other",
                    "source": "button",
                    "screen": "sidebar",
                    "index": 1,
                },
                {
                    "id": "sidebar:Menu",
                    "display_label": "Menu",
                    "type": "other",
                    "source": "button",
                    "screen": "sidebar",
                    "index": 2,
                },
            ],
            "screen_buttons": [
                {"label": "Classes", "screen": "sidebar", "index": 1},
                {"label": "Menu", "screen": "sidebar", "index": 2},
            ],
        },
    }

    data = build_state_data(raw)
    rendered = format_state_text(data, verbose=True)

    assert data["pending"]["choices"] == []
    assert [a["label"] for a in data["pending"]["actions"]] == [
        "Classes",
        "Menu",
    ]
    assert "Tell me more" not in rendered["pending"]
    assert "I've played this before" not in rendered["pending"]
    assert "--- OTHER BUTTONS ---" in rendered["pending"]
    assert "Classes" in rendered["pending"]
    assert 'act "Classes"' not in rendered["pending"]
    assert [b["label"] for b in data["buttons"]] == ["Classes", "Menu"]


def test_format_interactions_uses_custom_category_headers():
    rendered = format_interactions(
        [
            {
                "display_label": "Morning category: Royal Demeanor",
                "type": "other",
                "category": "lltq_morning_groups",
            },
            {
                "display_label": "Done",
                "type": "other",
                "category": "lltq_ui",
                "disabled": True,
            },
        ],
        colour=False,
        extra_categories={
            "lltq_morning_groups": {"header": "MORNING CATEGORIES"},
            "lltq_ui": {"header": "UI", "compact": True},
        },
    )

    assert "--- MORNING CATEGORIES ---" in rendered
    assert "--- UI ---" in rendered
    assert "LLTQ_MORNING_GROUPS" not in rendered


def test_state_uses_game_state_button_category_headers():
    data = build_state_data({
        "status": "running",
        "game_state": {
            "button_categories": {
                "lltq_week_menu": {"header": "WEEK MENU", "compact": True},
            },
            "screen_buttons": [
                {
                    "label": "Classes",
                    "screen": "sidebar",
                    "_category": "lltq_week_menu",
                },
            ],
        },
    })
    rendered = format_state_text(data, verbose=True)

    assert "--- WEEK MENU ---" in rendered["buttons"]
    assert "LLTQ_WEEK_MENU" not in rendered["buttons"]


def test_categorized_buttons_preserve_annotations():
    data = build_state_data({
        "status": "running",
        "game_state": {
            "button_categories": {
                "lltq_morning_groups": {
                    "header": "MORNING CATEGORIES",
                },
            },
            "screen_buttons": [
                {
                    "label": "Morning category: Royal Demeanor",
                    "annotation": "active",
                    "_category": "lltq_morning_groups",
                },
            ],
        },
    })
    rendered = format_state_text(data, verbose=True)

    assert "Morning category: Royal Demeanor — active" in rendered["buttons"]


def test_state_pending_non_choice_live_interactions_keep_pending_choice_interactions():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "outfit",
            "choices": [
                "Boarding School Uniform",
                "Uniform - Boosts Military",
                "Done",
            ],
            "interactions": [
                {
                    "id": "1",
                    "display_label": "Boarding School Uniform",
                    "type": "choice",
                    "source": "choice",
                    "index": 1,
                },
                {
                    "id": "2",
                    "display_label": "Uniform - Boosts Military",
                    "type": "choice",
                    "source": "choice",
                    "index": 2,
                },
                {
                    "id": "3",
                    "display_label": "Done",
                    "type": "choice",
                    "source": "choice",
                    "index": 3,
                },
            ],
        },
        "game_state": {
            "interactions": [
                {
                    "id": "sidebar:Classes",
                    "display_label": "Classes",
                    "type": "other",
                    "source": "button",
                    "screen": "sidebar",
                    "index": 1,
                },
            ],
            "screen_buttons": [
                {"label": "Classes", "screen": "sidebar", "index": 1},
            ],
        },
    }

    data = build_state_data(raw)
    rendered = format_state_text(data, verbose=True)["pending"]

    assert [c["label"] for c in data["pending"]["choices"]] == [
        "Boarding School Uniform",
        "Uniform - Boosts Military",
        "Done",
    ]
    assert "Boarding School Uniform" in rendered
    assert "Uniform - Boosts Military" in rendered
    assert "--- OTHER BUTTONS ---" in rendered
    assert "Classes" in rendered
    assert 'act "Classes"' not in rendered


def test_state_pending_empty_screen_interactions_do_not_suppress_choices():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "story",
            "choices": ["Stale choice."],
            "promoted_buttons": [
                {"label": "[spell]", "annotation": "stale class toggle"},
            ],
        },
        "screen": {
            "type": "screen_content",
            "interactions": [],
        },
    }

    data = build_state_data(raw)

    assert data["pending"]["choices"][0]["label"] == "Stale choice."
    rendered = format_state_text(data, verbose=True)["pending"]
    assert "Stale choice." in rendered
    assert 'act "[spell]"' not in rendered


def test_state_pending_non_choice_screen_interactions_do_not_suppress_choices():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "story",
            "choices": ["Legitimate pending choice."],
        },
        "screen": {
            "type": "screen_content",
            "interactions": [
                {
                    "id": "sidebar:Classes",
                    "display_label": "Classes",
                    "type": "other",
                    "source": "button",
                    "screen": "sidebar",
                    "index": 1,
                },
            ],
        },
    }

    data = build_state_data(raw)
    rendered = format_state_text(data, verbose=True)["pending"]

    assert data["pending"]["choices"][0]["label"] == "Legitimate pending choice."
    assert "Legitimate pending choice." in rendered
    assert "--- OTHER BUTTONS ---" in rendered
    assert "Classes" in rendered
    assert 'act "Classes"' not in rendered


def test_brief_state_suppresses_empty_inventory_bookkeeping():
    rendered = format_state_text(
        {
            "status": "waiting_for_input",
            "pending": {
                "type": "choice_request",
                "choices": ["Take the blade", "Enter the basement"],
            },
            "inventory": {"current": [], "version": 0},
        },
        verbose=False,
    )

    assert "pending" in rendered
    assert "brief" not in rendered


def test_state_uses_pending_when_live_choices_duplicate_pending_rows():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "dialogue",
            "choices": [
                "\u2022 ''Hi!''",
                "\u2022 ''Just checking in on you.''",
                "\u2022 [Continue down the stairs.]",
            ],
            "full_items": [
                {"label": "\u2022 ''Hi!''", "is_disabled": False},
                {
                    "label": "\u2022 ''Just checking in on you.''",
                    "is_disabled": False,
                },
                {
                    "label": "\u2022 [Continue down the stairs.]",
                    "is_disabled": False,
                },
            ],
        },
        "game_state": {
            "choices": [
                "\u2022 ''Hi!''",
                "\u2022 ''Just checking in on you.''",
                "\u2022 [Continue down the stairs.]",
                "''Hi!''",
                "''Just checking in on you.''",
                "[Continue down the stairs.]",
            ],
            "full_items": [
                {"label": "\u2022 ''Hi!''", "is_disabled": False},
                {
                    "label": "\u2022 ''Just checking in on you.''",
                    "is_disabled": False,
                },
                {
                    "label": "\u2022 [Continue down the stairs.]",
                    "is_disabled": False,
                },
                {"label": "''Hi!''", "is_disabled": False},
                {"label": "''Just checking in on you.''", "is_disabled": False},
                {"label": "[Continue down the stairs.]", "is_disabled": False},
            ],
        },
    }

    data = build_state_data(raw)
    rendered = format_state_text(data, verbose=True)["pending"]

    assert [c["label"] for c in data["pending"]["choices"]] == [
        "\u2022 ''Hi!''",
        "\u2022 ''Just checking in on you.''",
        "\u2022 [Continue down the stairs.]",
    ]
    assert "4:" not in rendered
    assert rendered.count("''Hi!''") == 1


def test_brief_state_keeps_status_when_buttons_are_all_disabled():
    rendered = format_state_text(
        {
            "status": "blocked_on_choice",
            "buttons": [{"label": "Locked", "disabled": True}],
            "inventory": {"current": [], "version": 0},
        },
        verbose=False,
    )

    assert rendered["brief"] == "status: blocked_on_choice"
    assert rendered["buttons"] == []


def test_brief_state_renders_main_menu_navigation_as_text():
    rendered = format_state_text(
        {
            "status": "screen_actions",
            "buttons": [
                {"label": "Start", "_category": "navigation"},
                {"label": "Load", "_category": "navigation"},
                {"label": "Quit", "_category": "navigation"},
            ],
        },
        verbose=False,
    )

    assert rendered["buttons"] == (
        "--- NAVIGATION ---\n  Start  |  Load  |  Quit"
    )


def test_brief_state_formats_non_empty_inventory_dict_without_version():
    rendered = format_state_text(
        {
            "status": "waiting_for_input",
            "inventory": {"current": ["Key"], "version": 1},
        },
        verbose=False,
    )

    assert rendered["brief"] == "items: Key"
    assert "version" not in rendered["brief"]


def test_stale_menu_empty_screen_interactions_do_not_suppress_pending():
    raw = {
        "status": "waiting_for_input",
        "context": {"context": "in_game"},
        "pending_request": {
            "type": "choice_request",
            "id": "story",
            "choices": ["Stay here"],
        },
        "screen": {
            "type": "screen_content",
            "screens": ["menu"],
            "interactions": [],
        },
    }

    data = build_state_data(raw)

    assert data["pending"]["choices"][0]["label"] == "Stay here"
    assert "Stay here" in format_state_text(
        data,
        verbose=True,
    )["pending"]


def test_state_pending_empty_live_choices_are_authoritative():
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "story",
            "choices": ["Stale choice."],
        },
        "game_state": {
            "choices": [],
            "full_items": [],
            "interactions": [],
        },
    }

    data = build_state_data(raw)

    assert data["pending"]["choices"] == []
    assert "Stale choice." not in format_state_text(
        data,
        verbose=True,
    )["pending"]


def test_no_enabled_pending_still_shows_promoted_actions():
    rendered = format_pending_text({
        "type": "choice",
        "id": "blocked",
        "choices": [
            {"label": "Requires knowledge.", "disabled": True, "index": None}
        ],
        "actions": [
            {
                "label": "[knowledge]",
                "annotation": "class ability toggle",
            }
        ],
    })

    assert "--- NO AVAILABLE CHOICES ---" in rendered
    assert 'act "[knowledge]" — class ability toggle' in rendered


def test_format_pending_text_groups_navigation_under_header():
    # Faithful minimal reproduction of a live Roadwarden choice screen
    # (playthrough_20260705_100258.jsonl): four story choices plus the
    # quick_menu navigation buttons (_category="navigation").  The wait()
    # / state()-with-pending path (build_pending -> format_pending_text)
    # used to flatten these nav buttons into bare `act "label"` lines with
    # no category header, so they read like loose extra choices.
    raw = {
        "type": "choice_request",
        "id": "req1",
        "choices": [
            "I could just look for another shelter.",
            "I need to look around. Cautiously.",
            "I dismount and sneak to the gate.",
            "I get off the horse and enter the camp briskly.",
        ],
        "full_items": [
            {"is_disabled": False, "is_caption": False,
             "label": "I could just look for another shelter."},
            {"is_disabled": False, "is_caption": False,
             "label": "I need to look around. Cautiously."},
            {"is_disabled": False, "is_caption": False,
             "label": "I dismount and sneak to the gate."},
            {"is_disabled": False, "is_caption": False,
             "label": "I get off the horse and enter the camp briskly."},
        ],
        "interactions": [
            {"index": 1, "choice_index": 1, "screen": "", "disabled": False,
             "source": "choice", "type": "choice", "id": "1",
             "display_label": "I could just look for another shelter."},
            {"index": 2, "choice_index": 2, "screen": "", "disabled": False,
             "source": "choice", "type": "choice", "id": "2",
             "display_label": "I need to look around. Cautiously."},
            {"index": 3, "choice_index": 3, "screen": "", "disabled": False,
             "source": "choice", "type": "choice", "id": "3",
             "display_label": "I dismount and sneak to the gate."},
            {"index": 4, "choice_index": 4, "screen": "", "disabled": False,
             "source": "choice", "type": "choice", "id": "4",
             "display_label": "I get off the horse and enter the camp briskly."},
            {"category": "navigation", "index": None, "screen": "quick_menu",
             "disabled": True, "source": "button", "action_names": ["none"],
             "type": "nav", "id": "quick_menu:Wait", "display_label": "Wait"},
            {"category": "navigation", "index": 5, "screen": "quick_menu",
             "disabled": False, "source": "button",
             "action_names": ["SelectedIf", "ShowMenu"],
             "type": "nav", "id": "quick_menu:Settings",
             "display_label": "Settings"},
            {"category": "navigation", "index": 6, "screen": "quick_menu",
             "disabled": False, "source": "button",
             "action_names": ["FileTakeScreenshot", "FileSave", "Notify"],
             "type": "nav", "id": "quick_menu:Q. Save",
             "display_label": "Q. Save"},
            {"category": "navigation", "index": 7, "screen": "quick_menu",
             "disabled": False, "source": "button",
             "action_names": ["FileLoad"],
             "type": "nav", "id": "quick_menu:Q. Load",
             "display_label": "Q. Load"},
            {"category": "navigation", "index": 8, "screen": "quick_menu",
             "disabled": False, "source": "button",
             "action_names": ["SelectedIf", "ShowMenu"],
             "type": "nav", "id": "quick_menu:Archive",
             "display_label": "Archive"},
        ],
    }
    rendered = format_pending_text(_build_pending(raw))

    # Numbered story choices are untouched.
    assert "--- CHOICE REQUIRED ---" in rendered
    assert "  1: I could just look for another shelter." in rendered
    assert "  4: I get off the horse and enter the camp briskly." in rendered

    # Navigation buttons render under their category header as one compact
    # pipe-separated line, matching state()'s screen rendering — disabled
    # buttons marked "(disabled)".
    assert "--- NAVIGATION ---" in rendered
    nav_line = next(
        line for line in rendered.splitlines()
        if "Settings" in line and "|" in line
    )
    assert "Wait (disabled)" in nav_line
    assert "Settings" in nav_line
    assert "Q. Save" in nav_line
    assert "Archive" in nav_line

    # No bare, ungrouped `act "label"` lines for the nav buttons.
    assert 'act "Settings"' not in rendered
    assert 'act "Q. Save"' not in rendered
    assert 'act "Archive"' not in rendered


def test_format_pending_text_filters_stale_skip_ahead_action():
    rendered = format_pending_text({
        "type": "choice",
        "id": "duel",
        "choices": [
            {"label": "Magic sword", "index": 1},
            {"label": "Dazzle him", "index": 2},
        ],
        "actions": [
            {"label": "Skip Ahead", "_suppress_pending_action": True},
            {"label": "Show Log"},
        ],
    })

    assert "Magic sword" in rendered
    assert 'act "Skip Ahead"' not in rendered
    assert 'act "Show Log"' in rendered


def test_format_event_filters_suppressed_promoted_actions():
    rendered = format_event({
        "type": "choice_request",
        "choices": ["Magic sword", "Dazzle him"],
        "promoted_buttons": [
            {"label": "Skip Ahead", "_suppress_pending_action": True},
            {"label": "Show Log"},
        ],
    })

    assert "Magic sword" in rendered
    assert 'act "Skip Ahead"' not in rendered
    assert 'act "Show Log"' in rendered


def test_modal_screen_without_choice_buttons_suppresses_pending_choices():
    pending = {
        "type": "choice_request",
        "choices": ["Underlying choice"],
    }
    screen = {
        "type": "screen_content",
        "overlay_active": True,
        "modal_screens": ["inventory"],
        "buttons": [
            {
                "label": "Food Rations",
                "screen": "inventory",
                "actions": ["Return"],
                "action_strs": ["Return"],
            },
        ],
    }

    assert _has_choice_overlay(screen, pending) is True


def test_generic_modal_screen_suppresses_underlying_pending_choices():
    """A Ren'Py generic ``menu`` wrapper masks the story menu beneath it."""
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": "story-menu",
            "choices": ["Enter the inn", "Leave"],
        },
        "game_state": {
            "screen_buttons": [
                {
                    "label": "Return",
                    "screen": "menu",
                    "actions": ["Return"],
                },
                {
                    "label": "Preferences",
                    "screen": "menu",
                    "actions": ["ShowMenu"],
                },
            ],
        },
        "screen": {
            "type": "screen_content",
            "overlay_active": False,
            "modal_screens": ["menu"],
            "buttons": [],
            "texts": ["Settings"],
        },
    }

    data = build_state_data(raw)

    assert "pending" not in data
    assert data["_overlay_active"] is True
    assert [button["label"] for button in data["buttons"]] == [
        "Return", "Preferences",
    ]


def test_modal_choice_does_not_repeat_passive_terminal_scrollback():
    """A modal choice must not claim its passive underlay's cumulative rows."""
    terminal_rows = [
        "INCOMING TRANSMISSION — ANOMALOUS SOURCE",
        "ELARA. YOU STAYED. GOOD.",
        "MY POWER BUDGET FOR THIS WINDOW IS SMALL. TWO QUESTIONS.",
    ]
    raw = {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "choices": ["What is coming?", "Stop."],
        },
        "screen": {
            "type": "screen_content",
            "modal_screens": ["echo_terminal_choice"],
            "texts": terminal_rows,
            "overlay_texts": terminal_rows,
            "overlay_texts_by_screen": {
                "echo_terminal_live": terminal_rows,
            },
            "overlay_screens": ["echo_terminal_live"],
        },
    }

    data = build_state_data(raw)

    assert data["_overlay_active"] is True
    assert "_screen_texts" not in data


def test_modal_keeps_own_text_above_passive_terminal_scrollback():
    raw = {
        "status": "waiting_for_input",
        "screen": {
            "type": "screen_content",
            "overlay_active": True,
            "modal_screens": ["confirm"],
            "texts": ["OLD TERMINAL ROW", "Delete this save?"],
            "overlay_texts": ["OLD TERMINAL ROW"],
            "overlay_texts_by_screen": {
                "echo_terminal_live": ["OLD TERMINAL ROW"],
            },
            "overlay_screens": ["echo_terminal_live"],
        },
    }

    data = build_state_data(raw)

    assert data["_screen_texts"] == ["Delete this save?"]
    assert data["_overlay_snapshot_identity"] == (
        ("confirm", "echo_terminal_live"), ()
    )


def test_blocking_overlay_snapshot_identity_includes_generation():
    data = build_state_data({
        "status": "waiting_for_input",
        "screen": {
            "type": "screen_content",
            "overlay_active": True,
            "overlay_screens": ["station_log"],
            "overlay_generations": {"station_log": "run-3"},
            "overlay_texts": ["STATION LOG", "ENTRY"],
        },
    })

    assert data["_overlay_snapshot_identity"] == (
        ("station_log",), (("station_log", "run-3"),)
    )


class TestScreenButtonsKeyPresence:
    """Regression tests for the empty-list-vs-missing-key fix.

    Bug: format used `if game_state.get("screen_buttons")` which
    treats empty list `[]` as falsy and falls through to stale
    `screen.buttons` cache.  Fix: check `"screen_buttons" in game_state`.
    """

    def test_empty_screen_buttons_in_game_state_clears_stale(self):
        """game_state with screen_buttons=[] should override stale screen cache."""
        raw = {
            "status": "running",
            "game_state": {"type": "game_state", "screen_buttons": []},
            "screen": {
                "type": "screen_content",
                "buttons": [
                    {"label": "Continue", "actions": ["NullAction"]},
                    {"label": "Quit", "actions": ["Quit"]},
                ],
            },
        }
        data = build_state_data(raw)
        # The empty list in game_state should win — no buttons surfaced.
        assert "buttons" not in data

    def test_missing_screen_buttons_falls_back_to_screen(self):
        """game_state without screen_buttons key should fall back to screen cache."""
        raw = {
            "status": "running",
            "game_state": {"type": "game_state"},
            "screen": {
                "type": "screen_content",
                "buttons": [
                    {"label": "Continue", "actions": ["NullAction"]},
                ],
            },
        }
        data = build_state_data(raw)
        assert "buttons" in data
        labels = [b["label"] for b in data["buttons"]]
        assert "Continue" in labels

    def test_populated_screen_buttons_used(self):
        """Non-empty game_state.screen_buttons should be used directly."""
        raw = {
            "status": "running",
            "game_state": {
                "type": "game_state",
                "screen_buttons": [
                    {"label": "Travel", "screen": "navigation"},
                ],
            },
        }
        data = build_state_data(raw)
        assert "buttons" in data
        assert data["buttons"][0]["label"] == "Travel"

    def test_choicereturn_filtered_from_raw_fallback(self):
        """When falling back to screen.buttons, ChoiceReturn entries are filtered."""
        raw = {
            "status": "running",
            "game_state": {},
            "screen": {
                "buttons": [
                    {"label": "Choice 1", "actions": ["ChoiceReturn"]},
                    {"label": "Map", "actions": ["NullAction"]},
                ],
            },
        }
        data = build_state_data(raw)
        labels = [b["label"] for b in data.get("buttons", [])]
        assert "Choice 1" not in labels
        assert "Map" in labels
        assert data["buttons"][0]["disabled"] is True

    def test_nullaction_game_state_button_is_disabled(self):
        raw = {
            "status": "idle",
            "game_state": {
                "type": "game_state",
                "screen_buttons": [
                    {
                        "label": "Buy Marshbules: ? (too expensive)",
                        "screen": "shopscreen",
                        "actions": ["NullAction"],
                        "_category": "shop",
                    },
                ],
                "interactions": [
                    {
                        "display_label": "Buy Marshbules: ? (too expensive)",
                        "type": "shop",
                        "action_names": ["NullAction"],
                        "disabled": False,
                    },
                ],
            },
        }

        data = build_state_data(raw)

        assert data["buttons"][0]["disabled"] is True
        assert data["_interactions"][0]["disabled"] is True

    def test_compact_game_state_button_renders_disabled_suffix(self):
        raw = {
            "status": "idle",
            "game_state": {
                "type": "game_state",
                "screen_buttons": [
                    {
                        "_category": "navigation",
                        "label": "Sleep",
                        "screen": "quick_menu",
                        "actions": ["NullAction"],
                        "is_disabled": True,
                    },
                    {
                        "_category": "navigation",
                        "label": "Map",
                        "screen": "quick_menu",
                        "actions": ["Show"],
                    },
                ],
            },
        }

        rendered = format_state_text(build_state_data(raw), verbose=True)

        assert "Sleep (disabled)" in rendered.get("buttons", "")
        assert "Map" in rendered.get("buttons", "")

    def test_game_state_buttons_preserve_interaction_indices(self):
        raw = {
            "status": "idle",
            "game_state": {
                "type": "game_state",
                "screen_buttons": [
                    {
                        "label": "Wait",
                        "screen": "quick_menu",
                        "actions": ["none"],
                        "_category": "navigation",
                    },
                    {
                        "label": "[Sell: Bronze Rod]",
                        "screen": "selling",
                        "actions": ["Jump"],
                        "_category": "shop",
                    },
                ],
                "interactions": [
                    {
                        "index": None,
                        "display_label": "Wait",
                        "screen": "quick_menu",
                        "category": "navigation",
                        "action_names": ["none"],
                    },
                    {
                        "index": 12,
                        "display_label": "[Sell: Bronze Rod]",
                        "screen": "selling",
                        "category": "shop",
                        "action_names": ["Jump"],
                    },
                ],
            },
        }

        data = build_state_data(raw)

        sell_button = next(
            b for b in data["buttons"]
            if b["label"] == "[Sell: Bronze Rod]"
        )
        assert sell_button["index"] == 12

    def test_mundanejob_buttons_render_as_options(self):
        raw = {
            "status": "idle",
            "game_state": {
                "type": "game_state",
                "screen_buttons": [
                    {
                        "label": "Work for 2",
                        "screen": "mundanejob",
                        "actions": ["Hide", "Jump"],
                    },
                ],
            },
        }

        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert "--- OPTIONS ---" in rendered["buttons"]
        assert "--- OTHER BUTTONS ---" not in rendered["buttons"]

    def test_live_choice_interactions_render_when_pending_missing(self):
        raw = {
            "status": "running",
            "game_state": {
                "type": "game_state",
                "screen_buttons": [
                    {"label": "Inventory", "screen": "quick_menu"},
                    {"label": "Settings", "screen": "quick_menu"},
                ],
                "interactions": [
                    {
                        "type": "choice",
                        "display_label": "I nod and change the topic.",
                        "disabled": False,
                    },
                    {
                        "type": "choice",
                        "display_label": "This option is blocked.",
                        "disabled": True,
                    },
                ],
            },
        }

        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert "I nod and change the topic" in rendered.get("pending", "")
        assert "This option is blocked" in rendered.get("pending", "")
        assert "Inventory" in rendered.get("buttons", "")

    def test_default_focus_list_chrome_not_added_to_pending_actions(self):
        raw = {
            "status": "running",
            "pending_request": {
                "type": "choice_request",
                "id": "next",
                "choices": ["Keep working."],
            },
            "game_state": {
                "interactions": [
                    {
                        "type": "choice",
                        "source": "choice",
                        "display_label": "Keep working.",
                        "disabled": False,
                    },
                    {
                        "type": "nav",
                        "category": "navigation",
                        "screen": "_focus_list",
                        "display_label": "Save Game",
                        "_category": "other",
                        "action_names": ["ShowMenu"],
                    },
                    {
                        "type": "nav",
                        "category": "navigation",
                        "screen": "_focus_list",
                        "display_label": "History",
                        "_category": "other",
                        "action_names": ["ShowMenu"],
                    },
                    {
                        "type": "other",
                        "category": "other",
                        "screen": "_focus_list",
                        "display_label": "Skip",
                        "_category": "other",
                        "action_names": ["Skip"],
                    },
                ],
                "screen_buttons": [
                    {
                        "label": "History",
                        "screen": "_focus_list",
                        "_category": "other",
                        "actions": ["ShowMenu"],
                    },
                    {
                        "label": "Save Game",
                        "screen": "_focus_list",
                        "_category": "other",
                        "actions": ["ShowMenu"],
                    },
                    {
                        "label": "Skip",
                        "screen": "_focus_list",
                        "_category": "other",
                        "actions": ["Skip"],
                    },
                ],
            },
        }

        pending = build_state_data(raw)["pending"]
        rendered = format_state_text(build_state_data(raw), verbose=True)

        assert pending["choices"][0]["label"] == "Keep working."
        assert "actions" not in pending
        assert "History" not in rendered.get("buttons", "")
        assert "Save Game" not in rendered.get("buttons", "")
        assert "Skip" not in rendered.get("buttons", "")

    def test_focus_skip_ahead_not_added_to_pending_actions(self):
        raw = {
            "status": "running",
            "pending_request": {
                "type": "choice_request",
                "id": "duel",
                "choices": ["Magic sword", "Dazzle him"],
                "promoted_buttons": [
                    {
                        "label": "Skip Ahead",
                        "_suppress_pending_action": True,
                    },
                    {
                        "label": "Show Log",
                        "screen": "end_menu_screen",
                    },
                ],
            },
        }

        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert "Magic sword" in rendered.get("pending", "")
        assert 'act "Skip Ahead"' not in rendered.get("pending", "")
        assert 'act "Show Log"' in rendered.get("pending", "")

    def test_unflagged_skip_ahead_can_remain_pending_action(self):
        raw = {
            "status": "running",
            "pending_request": {
                "type": "choice_request",
                "id": "skip",
                "choices": ["Wait"],
                "promoted_buttons": [{"label": "Skip Ahead"}],
            },
        }

        rendered = format_state_text(build_state_data(raw), verbose=True)

        assert 'act "Skip Ahead"' in rendered.get("pending", "")

    def test_overlay_does_not_synthesize_hidden_choice_interactions(self):
        raw = {
            "status": "running",
            "game_state": {
                "type": "game_state",
                "interactions": [
                    {
                        "type": "choice",
                        "display_label": "Stale story choice.",
                        "disabled": False,
                    },
                ],
            },
            "screen": {
                "overlay_active": True,
                "buttons": [
                    {"label": "Close", "screen": "inventory", "actions": ["Return"]},
                ],
            },
        }

        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert "Stale story choice" not in rendered.get("pending", "")
        assert "Close" in rendered.get("buttons", "")

    def test_screen_return_button_does_not_synthesize_pending_choice(self):
        raw = {
            "status": "running",
            "game_state": {
                "type": "game_state",
                "screen_buttons": [
                    {
                        "label": "TELESCOPE - boosts observation range",
                        "screen": "power_allocation_screen",
                        "actions": ["SetVariable"],
                        "_category": "items",
                    },
                    {
                        "label": "HEATING - keeps the station liveable",
                        "screen": "power_allocation_screen",
                        "actions": ["SetVariable"],
                        "_category": "items",
                    },
                    {
                        "label": "Confirm",
                        "screen": "power_allocation_screen",
                        "actions": ["Return"],
                        "_category": "items",
                    },
                ],
                "interactions": [
                    {
                        "source": "button",
                        "type": "items",
                        "category": "items",
                        "display_label": "TELESCOPE - boosts observation range",
                        "screen": "power_allocation_screen",
                        "action_names": ["SetVariable"],
                    },
                    {
                        "source": "button",
                        "type": "items",
                        "category": "items",
                        "display_label": "HEATING - keeps the station liveable",
                        "screen": "power_allocation_screen",
                        "action_names": ["SetVariable"],
                    },
                    {
                        "source": "button",
                        "type": "choice",
                        "category": "items",
                        "display_label": "Confirm",
                        "screen": "power_allocation_screen",
                        "action_names": ["Return"],
                    },
                ],
            },
        }

        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert "pending" not in data
        assert rendered.get("buttons", "").count("Confirm") == 1

    def test_ended_status_hidden_when_buttons_are_actionable(self):
        raw = {
            "status": "ended",
            "screen": {
                "buttons": [
                    {"label": "New Game", "screen": "main_menu", "actions": ["Jump"]},
                ],
            },
        }
        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert rendered.get("status") != "ended"
        assert "New Game" in rendered.get("buttons", "")

    def test_json_wait_hides_ended_when_buttons_are_actionable(self):
        rendered = format_wait_text(
            {
                "ended": True,
                "buttons": [
                    {"label": "Start", "screen": "menu", "actions": ["Start"]},
                ],
            },
            fmt="json",
        )

        assert "ended" not in rendered
        assert rendered["buttons"]["navigation"] == ["Start"]

    def test_json_wait_keeps_authoritative_terminal_beside_title_buttons(self):
        rendered = format_wait_text(
            {
                "ended": True,
                "_game_terminal": True,
                "buttons": [
                    {"label": "Start", "screen": "menu", "actions": ["Start"]},
                ],
            },
            fmt="json",
        )

        assert rendered["ended"] is True
        assert rendered["buttons"]["navigation"] == ["Start"]

    def test_progress_freeze_alone_does_not_override_title_buttons(self):
        rendered = format_wait_text(
            {
                "ended": True,
                "progress_frozen": True,
                "_game_terminal": False,
                "buttons": [
                    {"label": "Start", "screen": "menu", "actions": ["Start"]},
                ],
            },
            fmt="json",
        )

        assert "ended" not in rendered

    def test_json_wait_keeps_ended_when_buttons_are_disabled(self):
        rendered = format_wait_text(
            {
                "ended": True,
                "buttons": [
                    {
                        "label": "Continue",
                        "actions": ["NullAction"],
                        "disabled": False,
                    },
                ],
            },
            fmt="json",
        )

        assert rendered["ended"] is True
        assert rendered["_buttons_raw"][0]["label"] == "Continue"

    def test_text_wait_keeps_ended_when_buttons_are_disabled(self):
        rendered = format_wait_text({
            "ended": True,
            "buttons": [
                {
                    "label": "Continue",
                    "actions": ["NullAction"],
                    "disabled": False,
                },
            ],
        })

        assert rendered["ended"] is True

    def test_text_wait_keeps_ended_when_buttons_have_no_label(self):
        rendered = format_wait_text({
            "ended": True,
            "buttons": [
                {
                    "label": "",
                    "actions": ["Jump"],
                    "disabled": False,
                },
            ],
        })

        assert rendered["ended"] is True
        assert "buttons" not in rendered

    def test_text_wait_keeps_ended_when_pending_choices_are_disabled(self):
        rendered = format_wait_text({
            "ended": True,
            "pending": {
                "type": "choice",
                "choices": [
                    {
                        "label": "Continue",
                        "index": 1,
                        "disabled": True,
                    },
                ],
            },
        })

        assert rendered["ended"] is True
        assert "--- NO AVAILABLE CHOICES ---" in rendered["pending"]

    def test_json_wait_keeps_ended_when_pending_choices_are_disabled(self):
        rendered = format_wait_text(
            {
                "ended": True,
                "pending": {
                    "type": "choice",
                    "choices": [
                        {
                            "label": "Continue",
                            "index": 1,
                            "disabled": True,
                        },
                    ],
                },
            },
            fmt="json",
        )

        assert rendered["ended"] is True
        assert rendered["pending"]["choices"][0]["disabled"] is True

    def test_json_wait_hides_ended_when_pending_choice_is_actionable(self):
        rendered = format_wait_text(
            {
                "ended": True,
                "pending": {
                    "type": "choice",
                    "choices": [
                        {
                            "label": "Continue",
                            "index": 1,
                            "disabled": False,
                        },
                    ],
                },
            },
            fmt="json",
        )

        assert "ended" not in rendered
        assert rendered["pending"]["choices"][0]["label"] == "Continue"

    def test_text_wait_hides_ended_when_pending_action_is_actionable(self):
        rendered = format_wait_text({
            "ended": True,
            "pending": {
                "type": "choice",
                "choices": [
                    {
                        "label": "Requires knowledge",
                        "index": 1,
                        "disabled": True,
                    },
                ],
                "actions": [
                    {
                        "label": "[knowledge]",
                        "annotation": "Reveal scholar option",
                    },
                ],
            },
        })

        assert "ended" not in rendered
        assert 'act "[knowledge]"' in rendered["pending"]

    def test_json_wait_hides_ended_when_pending_action_is_actionable(self):
        rendered = format_wait_text(
            {
                "ended": True,
                "pending": {
                    "type": "choice",
                    "choices": [
                        {
                            "label": "Requires knowledge",
                            "index": 1,
                            "disabled": True,
                        },
                    ],
                    "actions": [
                        {
                            "label": "[knowledge]",
                            "annotation": "Reveal scholar option",
                        },
                    ],
                },
            },
            fmt="json",
        )

        assert "ended" not in rendered
        assert rendered["pending"]["actions"][0]["label"] == "[knowledge]"

    def test_text_wait_skips_disabled_pending_actions(self):
        rendered = format_wait_text({
            "ended": True,
            "pending": {
                "type": "choice",
                "choices": [
                    {
                        "label": "Requires knowledge",
                        "index": 1,
                        "disabled": True,
                    },
                ],
                "actions": [
                    {
                        "label": "[knowledge]",
                        "annotation": "Reveal scholar option",
                        "disabled": True,
                    },
                    {
                        "label": "",
                        "annotation": "No visible target",
                    },
                ],
            },
        })

        assert rendered["ended"] is True
        assert 'act "[knowledge]"' not in rendered["pending"]

    def test_json_state_uses_effective_status_but_preserves_raw_status(self):
        raw = {
            "status": "ended",
            "screen": {
                "buttons": [
                    {"label": "New Game", "screen": "main_menu", "actions": ["Jump"]},
                ],
            },
        }
        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True, fmt="json")

        assert rendered["status"] == "screen_actions"
        assert rendered["_raw_status"] == "ended"
        assert rendered["_lifecycle"]["raw_status"] == "ended"
        assert rendered["buttons"]["other"] == ["New Game"]

    def test_running_status_hidden_when_buttons_are_actionable(self):
        raw = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Return", "screen": "shop", "actions": ["Return"]},
                ],
            },
        }
        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert "status" not in rendered
        assert "Return" in rendered.get("buttons", "")

    def test_idle_status_hidden_when_buttons_are_actionable(self):
        raw = {
            "status": "idle",
            "game_state": {
                "screen_buttons": [
                    {
                        "label": "Ask about work",
                        "screen": "nvl",
                        "actions": ["Jump"],
                        "_category": "topics",
                    },
                ],
            },
            "screen": {
                "button_categories": {
                    "topics": {"header": "TOPICS"},
                },
            },
        }
        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert "status" not in rendered
        assert "Ask about work" in rendered.get("buttons", "")

    def test_info_button_does_not_consume_visible_topic_number(self):
        raw = {
            "status": "idle",
            "game_state": {
                "screen_buttons": [
                    {
                        "label": "A contextual hint.",
                        "screen": "nvl",
                        "actions": ["NullAction"],
                        "_category": "info",
                    },
                    {
                        "label": "Ask about herbs.",
                        "screen": "nvl",
                        "actions": ["SetField", "Jump"],
                        "_category": "topics",
                    },
                ],
            },
            "screen": {
                "button_categories": {
                    "topics": {"header": "TOPICS"},
                },
            },
        }

        rendered = format_state_text(build_state_data(raw), verbose=True)

        assert "  1: Ask about herbs." in rendered.get("buttons", "")
        assert "  2: Ask about herbs." not in rendered.get("buttons", "")

    def test_state_button_numbers_continue_after_pending_choices(self):
        raw = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "choices": ["Go north", "Go south"],
            },
            "game_state": {
                "type": "game_state",
                "choices": ["Go north", "Go south"],
                "interactions": [
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 1,
                        "display_label": "Go north",
                    },
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 2,
                        "display_label": "Go south",
                    },
                    {
                        "source": "button",
                        "type": "topic",
                        "category": "topics",
                        "index": 3,
                        "display_label": "Ask about herbs.",
                        "screen": "nvl",
                        "action_names": ["SetField", "Jump"],
                    },
                ],
                "screen_buttons": [
                    {
                        "label": "Ask about herbs.",
                        "screen": "nvl",
                        "actions": ["SetField", "Jump"],
                        "_category": "topics",
                    },
                ],
            },
            "screen": {
                "button_categories": {
                    "topics": {"header": "TOPICS"},
                },
            },
        }

        rendered = format_state_text(build_state_data(raw), verbose=True)

        assert "  1: Go north" in rendered.get("pending", "")
        assert "  2: Go south" in rendered.get("pending", "")
        # Rendered once: in the pending block, numbered after the choices;
        # the buttons block must not repeat it.
        assert "  3: Ask about herbs." in rendered.get("pending", "")
        assert "Ask about herbs." not in rendered.get("buttons", "")

    def test_button_numbers_skip_disabled_pending_choices(self):
        # Regression: a visible disabled choice must NOT consume a button
        # number. Two enabled + one disabled choice + one topic button →
        # the topic is button 3, not 4 (the renderer offset must match the
        # resolver, which counts enabled choices only).
        raw = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "choices": ["Ask about herbs", "Ask about roads"],
                "full_items": [
                    {"label": "Ask about herbs", "is_disabled": False},
                    {"label": "Ask about roads", "is_disabled": False},
                    {"label": "Pay the toll (need 5 coin)", "is_disabled": True},
                ],
            },
            "game_state": {
                "type": "game_state",
                "choices": ["Ask about herbs", "Ask about roads"],
                "interactions": [
                    {"source": "choice", "type": "choice", "index": 1,
                     "display_label": "Ask about herbs"},
                    {"source": "choice", "type": "choice", "index": 2,
                     "display_label": "Ask about roads"},
                    {"source": "button", "type": "topic", "category": "topics",
                     "index": 3, "display_label": "Open the map", "screen": "nvl",
                     "action_names": ["Jump"]},
                ],
                "screen_buttons": [
                    {"label": "Open the map", "screen": "nvl",
                     "actions": ["Jump"], "_category": "topics"},
                ],
            },
            "screen": {"button_categories": {"topics": {"header": "TOPICS"}}},
        }

        rendered = format_state_text(build_state_data(raw), verbose=True)
        pending = rendered.get("pending", "")
        buttons = rendered.get("buttons", "")

        assert "  1: Ask about herbs" in pending
        assert "  2: Ask about roads" in pending
        # The disabled choice is shown but unnumbered.
        assert "Pay the toll" in pending
        assert "3: Pay the toll" not in pending
        # The topic button continues at 3 (after the 2 enabled choices),
        # not 4 (which would skip past the disabled choice's phantom slot).
        assert "  3: Open the map" in rendered.get("pending", "")
        assert "  4: Open the map" not in rendered.get("pending", "")
        assert "Open the map" not in buttons

    def test_caption_does_not_consume_choice_number(self):
        # Regression: a menu caption (prompt line) must render unnumbered
        # and must not shift the choices after it. Shim emits captions in
        # full_items with is_caption=True, excluded from the enabled-only
        # choices list.
        raw = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "choices": ["Fight", "Flee"],
                "full_items": [
                    {"label": "What do you do now, hero?", "is_caption": True},
                    {"label": "Fight", "is_disabled": False},
                    {"label": "Flee", "is_disabled": False},
                ],
            },
        }
        rendered = format_state_text(build_state_data(raw), verbose=True)
        pending = rendered.get("pending", "")

        assert "  | What do you do now, hero?" in pending
        # Caption is not numbered, and choices keep the shim's numbering.
        assert "1: What do you do now" not in pending
        assert "  1: Fight" in pending
        assert "  2: Flee" in pending

    @pytest.mark.parametrize("verbose", [False, True])
    def test_call_screen_choice_buttons_not_duplicated(self, verbose):
        # Regression (real Echoes echo_terminal_choice shape): the bridge
        # has NO pending_request and NO game_state.choices — only
        # screen_buttons in the "choices" category (plain Return() actions)
        # plus matching choice-type interactions. The format layer
        # synthesizes a pending choice block from those interactions, so
        # the same options must not ALSO render as numbered buttons.
        raw = {
            "status": "screen_actions",
            "game_state": {
                "type": "game_state",
                "screen_buttons": [
                    {"label": "The signal analysis toolkit.", "screen": "",
                     "actions": ["Return"], "_category": "choices"},
                    {"label": "The spacetime equations.", "screen": "",
                     "actions": ["Return"], "_category": "choices"},
                    {"label": "ARIA's source logs.", "screen": "",
                     "actions": ["Return"], "_category": "choices"},
                ],
                "interactions": [
                    {"source": "button", "type": "choice", "category": "choices",
                     "index": 1, "display_label": "The signal analysis toolkit.",
                     "action_names": ["Return"]},
                    {"source": "button", "type": "choice", "category": "choices",
                     "index": 2, "display_label": "The spacetime equations.",
                     "action_names": ["Return"]},
                    {"source": "button", "type": "choice", "category": "choices",
                     "index": 3, "display_label": "ARIA's source logs.",
                     "action_names": ["Return"]},
                ],
            },
        }
        data = build_state_data(raw)
        # Pending is synthesized from the choice interactions.
        assert data.get("pending", {}).get("type") == "choice"
        rendered = format_state_text(data, verbose=verbose)
        pending = rendered.get("pending", "")
        buttons = rendered.get("buttons", "")

        # Brief mode must preserve the same decision as full state.
        assert "  1: The signal analysis toolkit." in pending
        assert "  3: ARIA's source logs." in pending
        # Not duplicated as buttons 4-6.
        assert "4:" not in buttons
        assert "The signal analysis toolkit." not in buttons

    def test_focus_list_continue_renders_once_as_numbered_choice(self):
        raw = {
            "status": "screen_actions",
            "game_state": {
                "type": "game_state",
                "screen_buttons": [
                    {
                        "label": "\u2022 [End this.]",
                        "screen": "_focus_list",
                        "actions": ["Return"],
                        "category": "choices",
                        "index": 1,
                    },
                ],
                "interactions": [
                    {
                        "source": "button",
                        "type": "choice",
                        "category": "choices",
                        "index": 1,
                        "id": "_focus_list:\u2022 [End this.]",
                        "display_label": "\u2022 [End this.]",
                        "screen": "_focus_list",
                        "action_names": ["Return"],
                    },
                ],
            },
        }

        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert data.get("pending", {}).get("type") == "choice"
        assert "  1: [End this.]" in rendered.get("pending", "")
        assert "\u2022 [End this.]" not in rendered.get("pending", "")
        assert "_focus_list" not in rendered.get("pending", "")
        assert "End this" not in rendered.get("buttons", "")

    def test_screen_content_focus_list_continue_uses_public_category(self):
        raw = {
            "status": "screen_actions",
            "screen": {
                "type": "screen_content",
                "buttons": [
                    {
                        "label": "\u2022 [Get up.]",
                        "screen": "_focus_list",
                        "actions": ["Return"],
                        "category": "choices",
                        "index": 1,
                    },
                ],
                "interactions": [
                    {
                        "source": "button",
                        "type": "choice",
                        "category": "choices",
                        "index": 1,
                        "id": "_focus_list:\u2022 [Get up.]",
                        "display_label": "\u2022 [Get up.]",
                        "screen": "_focus_list",
                        "action_names": ["Return"],
                    },
                ],
            },
        }

        rendered = format_state_text(build_state_data(raw), verbose=True)

        assert "  1: [Get up.]" in rendered.get("pending", "")
        assert "\u2022 [Get up.]" not in rendered.get("pending", "")
        assert "Get up" not in rendered.get("buttons", "")

    def test_dedup_keeps_same_label_button_in_other_category(self):
        # Codex review: the choice/button dedup must not drop a legitimate
        # button that merely shares a label with an enabled choice. Only
        # "choices"-category duplicates (call-screen Return buttons) get
        # dropped; a topics/nav button keeps its number and act(N) range.
        raw = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "choices": ["Ask about the bridge"],
            },
            "game_state": {
                "type": "game_state",
                "choices": ["Ask about the bridge"],
                "screen_buttons": [
                    # Real choice duplicate (choices category) -> dropped.
                    {"label": "Ask about the bridge", "screen": "",
                     "actions": ["Return"], "_category": "choices"},
                    # Coincidental same label in a different category -> kept.
                    {"label": "Ask about the bridge", "screen": "nvl",
                     "actions": ["Jump"], "_category": "topics"},
                ],
            },
            "screen": {"button_categories": {"topics": {"header": "TOPICS"}}},
        }
        data = build_state_data(raw)
        btns = data.get("buttons", [])
        cats = [b.get("_category") for b in btns
                if (b.get("label") or "") == "Ask about the bridge"]
        # The choices-category copy is gone; the topics copy survives.
        assert "choices" not in cats
        assert "topics" in cats

    def test_notifyimage_buttons_are_informational(self):
        raw = {
            "status": "waiting_for_input",
            "game_state": {
                "screen_buttons": [
                    {
                        "label": "You sold the iron scraps.\n+7",
                        "screen": "notifyimage",
                        "actions": ["Hide"],
                    },
                ],
            },
        }
        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert "OTHER BUTTONS" not in rendered.get("buttons", "")
        assert "--- INFO ---" in rendered.get("buttons", "")
        assert "You sold the iron scraps" in rendered.get("buttons", "")

    def test_ended_status_hidden_when_pending_is_actionable(self):
        raw = {
            "status": "ended",
            "pending_request": {
                "type": "choice_request",
                "id": "camp",
                "choices": ["Look around"],
            },
        }
        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert rendered.get("status") != "ended"
        assert "Look around" in rendered.get("pending", "")

    def test_stale_menu_screen_hidden_when_pending_choice_is_in_game(self):
        raw = {
            "status": "running",
            "context": {"context": "in_game"},
            "pending_request": {
                "type": "choice_request",
                "id": "next",
                "choices": ["Stay here"],
            },
            "screen": {
                "screens": ["menu"],
                "buttons": [
                    {"label": "Start", "screen": "menu", "actions": ["Start"]},
                ],
            },
        }
        data = build_state_data(raw)
        rendered = format_state_text(data, verbose=True)

        assert data["_lifecycle"]["stale_menu_overlay"] is True
        assert "Stay here" in rendered.get("pending", "")
        assert "Start" not in rendered.get("buttons", "")


# ---------------------------------------------------------------------------
# Anomaly formatting (shared helper used by cli.py + harness VN plugin)
# ---------------------------------------------------------------------------

def test_format_anomaly_text_renders_kind_and_first_detail():
    from vnflight.format import format_anomaly_text

    s = format_anomaly_text({
        "type": "anomaly",
        "kind": "orphaned_choice_screen",
        "details": {"screen": "rest_screen", "request_id": "r-42"},
    })

    assert "orphaned_choice_screen" in s
    assert "screen=rest_screen" in s
    assert "request_id=r-42" in s


def test_format_anomaly_text_prefers_explicit_text_when_present():
    from vnflight.format import format_anomaly_text

    assert format_anomaly_text({"text": "raw warning"}) == "raw warning"


def test_format_anomaly_text_handles_empty_event():
    from vnflight.format import format_anomaly_text

    assert format_anomaly_text({}) == "unknown"
    assert format_anomaly_text(None) == ""


def test_anomaly_summary_returns_canonical_shape():
    from vnflight.format import anomaly_summary

    out = anomaly_summary({
        "type": "anomaly",
        "kind": "duplicate_buttons",
        "details": {"label": "Continue", "duplicates": ["a", "b", "c"]},
    })
    assert out["kind"] == "duplicate_buttons"
    assert "duplicate_buttons" in out["summary"]
    assert "label=Continue" in out["summary"]
    assert out["details"] == {"label": "Continue", "duplicates": ["a", "b", "c"]}


def test_anomaly_summary_handles_non_dict_input():
    from vnflight.format import anomaly_summary

    assert anomaly_summary(None) == {}
    assert anomaly_summary("not a dict") == {}


def test_menu_caption_narration_renders_without_a_speaker_prefix():
    """The shim publishes a `menu:` caption as narration (see
    _vnf_executing_menu_statement) precisely so it cannot inherit the
    previous speaker.  The extra menu_caption marker must not disturb
    rendering: the caption reads as the prompt it is, while the say that
    preceded it keeps its speaker."""
    data = build_wait_data([
        {"type": "dialogue", "character": "Dr. Chen",
         "text": "You have that look. The one from the review board."},
        {"type": "narration", "text": "Should I tell him?",
         "menu_caption": True},
    ])

    rendered = format_wait_text(data)["text"]

    assert "[Dr. Chen] You have that look." in rendered
    assert "Should I tell him?" in rendered
    assert "[Dr. Chen] Should I tell him?" not in rendered


# ---------------------------------------------------------------------------
# (disabled) marker dedupe — Roadwarden labels carry their own suffix
# ---------------------------------------------------------------------------

def test_mark_disabled_does_not_double_game_side_suffix():
    from vnflight.format import _mark_disabled

    assert _mark_disabled("Wait") == "Wait (disabled)"
    label = "I'm too exhausted to brew potions. (Required vitality: 1) (disabled)"
    assert _mark_disabled(label) == label
    # Trailing whitespace does not defeat the check.
    assert _mark_disabled("Rest (disabled)  ") == "Rest (disabled)  "


def test_pending_actions_do_not_double_disabled_suffix():
    from vnflight.format import format_pending_text

    pending = {
        "type": "choice",
        "choices": [{"index": 1, "label": "Go on."}],
        "actions": [
            {
                "label": "Sleep (disabled)",
                "category": "navigation",
                "is_disabled": True,
            },
        ],
    }
    text = format_pending_text(pending)
    assert "Sleep (disabled)" in text
    assert "(disabled) (disabled)" not in text


# ---------------------------------------------------------------------------
# Modal overlay presentation
#
# A full-screen panel (Echoes' LOG/KIT/MAP) is what the player is looking at.
# Layering its rows over the observatory scene described a screen nobody could
# see, and silently dropping the hub menu left the agent with no idea a
# decision was waiting underneath.  A declared modal panel is the primary
# surface: its rows are the story text, its own buttons are the numbered list,
# and the covered menu is reported, not erased.
# ---------------------------------------------------------------------------


_MODAL_PANEL_ROWS = [
    "EVIDENCE LOG",
    "1. Cracked antenna mount",
    "2. Marcus's last transmission",
]
_MODAL_HUB_CHOICES = ["Check the generator", "Call Marcus", "Head outside"]


def _modal_panel_raw(*, declare_modal=True, panel_name=True):
    """An Echoes hub menu with the LOG panel open on top of it."""
    screen = {
        "type": "screen_content",
        "overlay_active": True,
        "modal_screens": ["evidence_screen"],
        "overlay_screens": ["echo_terminal_live", "evidence_screen"],
        "overlay_texts_by_screen": {
            "echo_terminal_live": ["> QUERY ARIA"],
            "evidence_screen": list(_MODAL_PANEL_ROWS),
        },
        "texts": [
            "Select station section",
            "STATION STATUS",
            "> QUERY ARIA",
        ] + _MODAL_PANEL_ROWS,
        "buttons": [{"label": "CLOSE", "screen": "evidence_screen"}],
    }
    if declare_modal:
        screen["modal_overlay_screens"] = ["evidence_screen"]
    if panel_name:
        screen["screen_names"] = {"evidence_screen": "LOG"}
    return {
        "status": "waiting_for_input",
        "context": {"context": "in_game"},
        "pending_request": {
            "type": "choice_request",
            "id": "hub-7",
            "choices": list(_MODAL_HUB_CHOICES),
        },
        "game_state": {
            "screen_buttons": [
                {"label": "CLOSE", "screen": "evidence_screen",
                 "actions": ["Hide"], "index": 1},
            ],
        },
        "screen": screen,
    }


def _modal_panel_closed_raw():
    """The same hub menu once the panel is gone."""
    return {
        "status": "waiting_for_input",
        "context": {"context": "in_game"},
        "pending_request": {
            "type": "choice_request",
            "id": "hub-7",
            "choices": list(_MODAL_HUB_CHOICES),
        },
        "game_state": {
            "interactions": [
                {"source": "choice", "type": "choice", "index": i,
                 "display_label": label, "disabled": False}
                for i, label in enumerate(_MODAL_HUB_CHOICES, 1)
            ],
            "screen_buttons": [],
        },
        "screen": {
            "type": "screen_content",
            "screens": ["observatory_map", "nvl"],
            "texts": ["Select station section"],
        },
    }


def test_modal_overlay_makes_the_panel_the_primary_surface():
    data = build_state_data(_modal_panel_raw())

    assert data["_modal_overlay_screens"] == ["evidence_screen"]
    # The panel body, in panel order — not the map and HUD rows underneath
    # it, and not the passive terminal that is still technically visible.
    assert data["_screen_texts"] == _MODAL_PANEL_ROWS
    # The covered menu is not numbered.
    assert "pending" not in data
    assert "_pending_raw" not in data
    # But it is not erased either.
    assert data["_hidden_menu"]["panel"] == "LOG"
    assert data["_hidden_menu"]["count"] == 3
    assert data["_hidden_menu"]["labels"] == _MODAL_HUB_CHOICES
    assert data["_hidden_menu"]["screens"] == ["evidence_screen"]
    assert [b["label"] for b in data["buttons"]] == ["CLOSE"]


def test_modal_overlay_renders_panel_rows_note_and_panel_buttons():
    rendered = format_state_text(
        build_state_data(_modal_panel_raw()), verbose=True)

    assert rendered["text"] == "\n".join(_MODAL_PANEL_ROWS)
    assert rendered["overlay_note"] == (
        "Underlying menu hidden behind LOG: 3 choices "
        "(close the panel to act)"
    )
    # The panel's own control owns number 1; nothing renumbers the hub menu.
    assert "1: CLOSE" in rendered["buttons"]
    assert "pending" not in rendered
    for label in _MODAL_HUB_CHOICES:
        assert label not in rendered["buttons"]


def test_modal_overlay_note_falls_back_to_the_screen_tag():
    """A mod that registers no display name still gets an honest name."""
    data = build_state_data(_modal_panel_raw(panel_name=False))

    assert data["_hidden_menu"]["panel"] == "evidence_screen"
    assert "hidden behind evidence_screen" in format_state_text(
        data, verbose=True)["overlay_note"]


def test_modal_overlay_note_reaches_brief_state_and_wait():
    raw = _modal_panel_raw()
    data = build_state_data(raw)

    assert "hidden behind LOG" in format_state_text(data)["overlay_note"]

    wait_rendered = format_wait_text({
        "_overlay_active": True,
        "_modal_overlay_screens": data["_modal_overlay_screens"],
        "_hidden_menu": data["_hidden_menu"],
        "_screen_texts": data["_screen_texts"],
        "buttons": data["buttons"],
    })

    assert wait_rendered["overlay_note"] == (
        "Underlying menu hidden behind LOG: 3 choices "
        "(close the panel to act)"
    )
    assert wait_rendered["screen_text"] == "\n".join(_MODAL_PANEL_ROWS)
    assert "pending" not in wait_rendered


def test_modal_note_counts_only_choices_that_would_be_numbered():
    raw = _modal_panel_raw()
    raw["pending_request"]["choices"] = [
        {"label": "Marcus is unreachable", "is_caption": True},
        {"label": "Check the generator"},
        {"label": "Force the airlock", "is_disabled": True},
        {"label": "Call Marcus"},
    ]
    data = build_state_data(raw)

    assert data["_hidden_menu"]["count"] == 2
    assert data["_hidden_menu"]["labels"] == [
        "Check the generator", "Call Marcus",
    ]
    assert "2 choices" in format_state_text(data, verbose=True)["overlay_note"]


def test_closing_the_modal_restores_the_menu_and_its_numbering():
    """Open then close: the covered menu comes back numbered from 1."""
    open_data = build_state_data(_modal_panel_raw())
    closed_data = build_state_data(_modal_panel_closed_raw())

    assert "pending" not in open_data
    assert [
        (c["label"], c["index"]) for c in closed_data["pending"]["choices"]
    ] == [(label, i) for i, label in enumerate(_MODAL_HUB_CHOICES, 1)]
    # The panel is gone from every presentation channel.
    assert "_modal_overlay_screens" not in closed_data
    assert "_hidden_menu" not in closed_data
    assert "_overlay_active" not in closed_data

    rendered = format_state_text(closed_data, verbose=True)
    assert "overlay_note" not in rendered
    for i, label in enumerate(_MODAL_HUB_CHOICES, 1):
        assert "{}: {}".format(i, label) in rendered["pending"]


def test_undeclared_overlay_keeps_the_layered_presentation():
    """Roadwarden's journal is blocking, not modal: nothing changes for it.

    Without ``modal_overlay_screens`` the scene rows stay in the overlay text
    channel and no hidden-menu note appears — byte-for-byte the old contract.
    """
    data = build_state_data(_modal_panel_raw(declare_modal=False))

    assert "_modal_overlay_screens" not in data
    assert "_hidden_menu" not in data
    assert data["_overlay_active"] is True
    # Legacy behavior: the scene's own rows are still merged in, and only the
    # passive terminal's contribution is subtracted.
    assert data["_screen_texts"] == [
        "Select station section", "STATION STATUS",
    ] + _MODAL_PANEL_ROWS
    assert "overlay_note" not in format_state_text(data, verbose=True)


def test_modal_panel_name_is_recorded_without_a_covered_menu():
    """Fleet R62 #5: the panel name must be available to a refusal aimed at a
    BUTTON on the surface underneath, where there is no ``_hidden_menu`` to
    read it from.  Every panel open the fleet performed was over the
    navigation row, never over a numbered menu.
    """
    raw = _modal_panel_raw()
    raw.pop("pending_request")
    data = build_state_data(raw)

    assert data["_modal_overlay_screens"] == ["evidence_screen"]
    assert data["_modal_overlay_panel"] == "LOG"
    # No covered menu, so the note stays silent — the R62 gating, confirmed
    # correct against all 53 modal frames in the fleet's bridge logs.
    assert "_hidden_menu" not in data
    assert "overlay_note" not in format_state_text(data, verbose=True)


def test_modal_panel_name_falls_back_to_the_tag_without_a_covered_menu():
    raw = _modal_panel_raw(panel_name=False)
    raw.pop("pending_request")

    assert build_state_data(raw)["_modal_overlay_panel"] == "evidence_screen"


def test_undeclared_overlay_records_no_panel_name():
    raw = _modal_panel_raw(declare_modal=False)
    raw.pop("pending_request")

    assert "_modal_overlay_panel" not in build_state_data(raw)


def test_modal_panel_rows_survive_a_missing_contributor_entry():
    """A declared modal with no scraped rows falls back, never blanks out."""
    raw = _modal_panel_raw()
    raw["screen"]["overlay_texts_by_screen"].pop("evidence_screen")
    data = build_state_data(raw)

    assert data["_modal_overlay_screens"] == ["evidence_screen"]
    # No provenance for the panel: keep the legacy body rather than claiming
    # the panel is empty.
    assert _MODAL_PANEL_ROWS[0] in data["_screen_texts"]


# --- Anomaly visibility -------------------------------------------------
#
# 578c4247 moved every anomaly into status.anomalies, which no renderer
# printed: orphaned_choice_screen lines were interleaving with dialogue.
# The side effect was that a crashed game (renpy_exception) also went
# unreported to agents — R68's fleet clicked an exception screen for 60+
# tool calls without ever being told the game had crashed. Errors are
# visible by default now; the noisy kinds stay opt-in.

def _anomaly_event(kind, message):
    return {
        "type": "anomaly",
        "kind": kind,
        "details": {"type": kind, "message": message},
    }


_EXC_EVENT = _anomaly_event(
    "renpy_exception",
    "Ren'Py exception: While running game code:\n"
    "Exception: The say screen must return a Text object.",
)
_NOISE_EVENT = _anomaly_event("duplicate_buttons", "2 duplicate buttons")


def test_wait_reports_a_game_error_by_default():
    from vnflight.format import format_wait_text

    data = build_wait_data([
        _EXC_EVENT,
        {"type": "dialogue", "character": "Voss", "text": "hello"},
    ])
    note = format_wait_text(data)["anomaly_note"]

    assert "GAME ERROR" in note
    assert "renpy_exception" in note
    # The message is flattened to one line and carries the "clicking its
    # buttons will not resume" advice the fleet agents never got.
    assert "\n" not in note
    assert "exception screen" in note


def test_wait_hides_scrape_diagnostics_by_default_and_shows_them_on_all():
    from vnflight.format import format_wait_text

    data = build_wait_data([_NOISE_EVENT])

    assert "anomaly_note" not in format_wait_text(data)
    note_all = format_wait_text(data, anomalies="all")["anomaly_note"]
    assert "duplicate_buttons" in note_all
    assert "GAME ERROR" not in note_all


def test_anomalies_off_suppresses_even_a_game_error():
    from vnflight.format import format_wait_text

    data = build_wait_data([_EXC_EVENT])

    assert "anomaly_note" not in format_wait_text(data, anomalies="off")
    # The structured channel is untouched by visibility: JSON consumers and
    # the hub still read status.anomalies as before.
    assert data["status"]["anomalies"]


def test_state_surfaces_the_bridge_sticky_anomaly():
    import time

    from vnflight.format import format_state_text

    latched = dict(_EXC_EVENT, _latched_at=time.time())
    data = build_state_data({"status": "in_game", "anomaly": latched})

    assert data["anomalies"] == [latched]
    assert "GAME ERROR" in format_state_text(data)["anomaly_note"]
    assert "anomaly_note" not in format_state_text(data, anomalies="off")


def test_state_without_an_anomaly_adds_no_key():
    data = build_state_data({"status": "in_game"})

    assert "anomalies" not in data


def test_repeated_identical_anomalies_render_once():
    from vnflight.format import format_wait_text

    data = build_wait_data([_EXC_EVENT, _EXC_EVENT, _EXC_EVENT])

    assert format_wait_text(data)["anomaly_note"].count("GAME ERROR") == 1


def test_anomaly_error_classification():
    from vnflight.format import anomaly_is_error

    assert anomaly_is_error("renpy_exception", "")
    # Kind may be missing when a mod reports free-form text.
    assert anomaly_is_error("unknown", "Ren'Py exception: boom")
    assert not anomaly_is_error("duplicate_buttons", "2 duplicate buttons")
    assert not anomaly_is_error("orphaned_choice_screen", "menu without say")


def test_state_ignores_a_resolved_anomaly_latch():
    """Story progress since the latch decides, not age.

    The live Roadwarden slot in R68 carried a duplicate_buttons latch for
    thousands of turns of healthy play; reporting it forever would train
    agents to ignore the one message that matters. But an exception screen
    that has been up for an hour is still the current situation: the bridge
    marks the latch resolved only when a later story event arrives, and
    the renderer trusts that mark alone.
    """
    import time

    from vnflight.format import format_state_text

    old_but_current = dict(_EXC_EVENT, _latched_at=time.time() - 3600)
    resolved = dict(
        _EXC_EVENT, _latched_at=time.time() - 5,
        _resolved_at=time.time(), _resolved_by="dialogue")

    assert format_state_text(
        build_state_data({"status": "in_game", "anomaly": old_but_current})
    )["anomaly_note"]
    stale_data = build_state_data({"status": "in_game", "anomaly": resolved})
    assert "anomaly_note" not in format_state_text(stale_data)
    # Still available to diagnostics, just not presented as current.
    assert stale_data["_stale_anomaly"] == resolved
    assert "anomalies" not in stale_data


def test_latched_and_streamed_copies_of_one_anomaly_render_once():
    """A wait carries the streamed event AND the bridge latch (merged from
    the decision snapshot); the agent must read one line, not two."""
    import time

    from vnflight.format import format_wait_text

    data = build_wait_data([_EXC_EVENT])
    data["anomalies"] = [dict(_EXC_EVENT, _latched_at=time.time())]

    note = format_wait_text(data)["anomaly_note"]
    assert note.count("GAME ERROR") == 1
    assert "message=" not in note
    assert "Ignore continues" in note


def test_unstamped_anomaly_latch_is_not_treated_as_current():
    """Bridge and client ship as one artifact: every latch is stamped.

    An unstamped latch is malformed, so it is not presented as something
    happening now.
    """
    from vnflight.format import format_state_text

    data = build_state_data({"status": "in_game", "anomaly": _EXC_EVENT})

    assert "anomaly_note" not in format_state_text(data)
    assert data["_stale_anomaly"] == _EXC_EVENT


def test_input_hint_names_the_documented_cli_invocation():
    from vnflight.format import format_pending_text

    text = format_pending_text({"type": "input", "prompt": "What is your name?"})
    assert 'python vnflight.py input "your text"' in text
    assert 'vnflight input "your text" in CLI' not in text


def test_state_does_not_render_pending_actions_a_second_time_as_buttons():
    """With a menu pending, the quick-menu button rode along in the pending
    block ("4: Q.Load") and was rendered again in the buttons block."""
    from vnflight.format import _buttons_not_in_pending_actions

    pending = {"actions": [
        {"label": "Q.Load", "category": "other", "index": 4, "id": "quick:load"},
        {"label": "Journal", "type": "other"},
    ]}
    buttons = [
        {"label": "Q.Load", "id": "quick:load", "index": 0},
        {"label": "journal", "index": 1},
        {"label": "Map", "index": 2},
    ]
    kept = _buttons_not_in_pending_actions(buttons, pending)
    assert [b["label"] for b in kept] == ["Map"]
    # Nothing listed as an action: buttons are untouched.
    assert _buttons_not_in_pending_actions(buttons, {"actions": []}) == buttons


def test_quick_menu_file_actions_are_navigation_not_numbered_buttons():
    """Mystic Cafe's "Q.Load" (FileLoad on the quick menu) arrives from the
    focus-list scrape with screen "_focus_list", so the quick_menu screen
    rule never saw it and it was numbered under OTHER BUTTONS on every
    read.  Slot buttons on the load screen keep their own category."""
    from vnflight.format import _categorize_button

    quick = {"label": "Q.Load", "screen": "_focus_list", "actions": ["FileLoad"]}
    assert _categorize_button(quick) == "navigation"
    quick_save = {"label": "Q.Save", "screen": "quick_menu", "actions": ["FileSave"]}
    assert _categorize_button(quick_save) == "navigation"
    slot = {"label": "1. Empty Slot", "screen": "load", "actions": ["FileLoad"]}
    assert _categorize_button(slot) != "navigation"


def test_state_button_takes_navigation_from_the_shim_typed_interaction():
    from vnflight.format import build_state_data

    raw = {
        "status": "running",
        "context": {"context": "in_game"},
        "game_state": {
            "type": "game_state",
            "interactions": [
                {"id": "_focus_list:Q.Load", "display_label": "Q.Load",
                 "type": "nav", "source": "button", "screen": "_focus_list",
                 "index": 6, "action_names": ["FileLoad"]},
            ],
            "screen_buttons": [
                {"label": "Q.Load", "screen": "_focus_list",
                 "actions": ["FileLoad"], "index": 6},
            ],
        },
    }
    data = build_state_data(raw)
    assert data["buttons"][0]["_category"] == "navigation"
