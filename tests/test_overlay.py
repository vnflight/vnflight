from vnflight.overlay import (
    align_row_provenance,
    passive_row_delta_indices,
    passive_rows_delta,
)
import random


def test_row_provenance_survives_append_and_refresh():
    assert align_row_provenance(
        ["HEADER", "status: old"],
        ["HEADER", "status: new", "APPENDED"],
        [10, 20],
        30,
    ) == [10, 30, 30]


def test_delta_indices_identify_a_repeated_appended_occurrence():
    assert passive_row_delta_indices(["SAME"], ["SAME", "SAME"]) == [1]


def test_in_place_refresh_plus_append_only_delivers_appended_rows():
    previous = [
        "ARIA SOURCE CODE AUDIT - REPORT",
        "Commissioned locally.",
        "Core integrity now 57%.",
        "FINDINGS:",
        "1. downgrade",
        "2. old handshake",
        "3. no mutual auth",
        "Annotated.",
        "ARCHIVE TARGET - ARMED",
        "1 drive in the bag.",
    ]
    panel = [
        "ARIA SOURCE CODE AUDIT - REPORT",
        "Commissioned locally.",
        "Core integrity now 54%.",
        "FINDINGS:",
        "1. downgrade",
        "2. old handshake",
        "3. no mutual auth",
        "Annotated.",
        "ARCHIVE TARGET - ARMED",
        "0 drives in the bag.",
        "DRIVE LOADED - HASH RUNNING.",
    ]

    assert passive_rows_delta(previous, panel) == [
        "DRIVE LOADED - HASH RUNNING.",
    ]


def test_divergent_panel_without_stable_anchor_is_delivered():
    assert passive_rows_delta(
        ["OLD TITLE", "old body"],
        ["NEW TITLE", "new body"],
    ) == ["NEW TITLE", "new body"]


def test_refresh_and_append_is_stable_under_many_counter_mutations():
    rng = random.Random(0xA37A)
    for case in range(500):
        previous = [f"row {index}" for index in range(12)]
        panel = list(previous)
        panel[rng.randrange(0, 3)] += f" old={case}"
        panel[rng.randrange(9, 12)] += f" now={case}"
        appended = [f"fresh {case}:{index}" for index in range(1 + case % 3)]
        panel.extend(appended)

        assert passive_rows_delta(previous, panel) == appended


def test_unrelated_expansion_after_stable_anchor_replays_from_divergence():
    previous = ["HEADER", "stable one", "stable two", "old footer"]
    panel = [
        "HEADER", "stable one", "stable two", "new notice", "new footer",
    ]

    assert passive_rows_delta(previous, panel) == [
        "new notice", "new footer",
    ]
