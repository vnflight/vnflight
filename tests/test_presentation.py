from vnflight.presentation import (
    merge_sections,
    merge_sections_by_bridge_sequence,
    normalize_sections,
    render_sections,
)


def _join(left: str, right: str) -> str:
    return left + "\n" + right


def test_authoritative_late_observation_returns_to_bridge_position():
    base = [
        {"channel": "text", "text": "A", "_bridge_seq": 10,
         "occurrence_ids": ["bridge:10"]},
        {"channel": "text", "text": "C", "_bridge_seq": 30,
         "occurrence_ids": ["bridge:30"]},
    ]
    addition = [
        {"channel": "screen_text", "text": "B", "_bridge_seq": 20,
         "delivery_ids": [7]},
    ]

    merged = merge_sections_by_bridge_sequence(
        base, addition, join_story_text=_join)

    assert render_sections(merged) == "A\n\nB\n\nC"


def test_replayed_occurrence_is_not_rendered_twice():
    occurrence = {
        "channel": "text",
        "text": "only once",
        "_bridge_seq": 4,
        "occurrence_ids": ["bridge:4"],
    }
    merged = merge_sections(
        [[occurrence], [dict(occurrence)]], join_story_text=_join)

    assert render_sections(merged) == "only once"


def test_malformed_provenance_degrades_to_unowned_text():
    assert normalize_sections([{
        "channel": "text",
        "text": "valid text",
        "delivery_ids": [1, 2],
        "occurrence_ids": 3,
        "_bridge_seq": True,
    }]) == [{"channel": "text", "text": "valid text"}]
