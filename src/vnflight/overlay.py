"""Shared passive-overlay differencing policy.

The bridge annotates durable events with overlay deltas while handlers also
reconstruct deltas from snapshots supplied by older bridges and transcripts.
Both paths must agree about which rows are newly visible.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Sequence, TypeVar


_Row = TypeVar("_Row", bound=str)


def align_row_provenance(
    previous: Sequence[str],
    panel: Sequence[str],
    previous_provenance: Sequence[int],
    current_provenance: int,
) -> list[int]:
    """Carry occurrence provenance through a cumulative panel refresh."""
    if len(previous_provenance) != len(previous):
        previous_provenance = [current_provenance] * len(previous)
    aligned: list[int] = []
    matcher = SequenceMatcher(None, previous, panel, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            aligned.extend(previous_provenance[old_start:old_end])
        elif tag in ("replace", "insert"):
            aligned.extend([current_provenance] * (new_end - new_start))
    return aligned


def passive_rows_delta(
    previous: Sequence[_Row], panel: Sequence[_Row],
) -> list[_Row]:
    """Return newly appended rows from an ordered, possibly rolling panel."""
    return [panel[index] for index in passive_row_delta_indices(previous, panel)]


def passive_row_delta_indices(
    previous: Sequence[_Row], panel: Sequence[_Row],
) -> list[int]:
    """Return panel indexes selected by the shared differencing policy."""
    if panel == previous:
        return []
    if len(panel) >= len(previous) and panel[:len(previous)] == previous:
        return list(range(len(previous), len(panel)))

    overlap = 0
    for size in range(min(len(previous), len(panel)), 0, -1):
        if previous[-size:] == panel[:size]:
            overlap = size
            break

    common = 0
    for old_row, new_row in zip(previous, panel):
        if old_row != new_row:
            break
        common += 1

    # Some bounded logs pin a title at row zero while rotating only their
    # body. Search for overlap below that stable prefix as well.
    body_overlap = 0
    if common:
        old_body = previous[common:]
        new_body = panel[common:]
        for size in range(min(len(old_body), len(new_body)), 0, -1):
            if old_body[-size:] == new_body[:size]:
                body_overlap = size
                break

    consumed = max(overlap, common + body_overlap)
    if consumed == common:
        refreshed = _delta_indices_after_in_place_refresh(
            previous, panel, common)
        if refreshed is not None:
            return refreshed
    return list(range(consumed, len(panel)))


def _delta_indices_after_in_place_refresh(
    previous: Sequence[_Row], panel: Sequence[_Row], common: int,
) -> list[int] | None:
    """Return inserts when an existing panel was refreshed in place.

    A retained terminal can update counters embedded in old rows and append a
    new result in the same render. ``SequenceMatcher`` represents a changed
    final row plus an appended row as one unequal ``replace`` opcode, not a
    replace followed by an insert. Treat the aligned part of such a block as
    presentation refresh and only its surplus new rows as fresh output.

    We require a stable block of at least two rows after the common prefix.
    Without that continuity proof, a divergent panel is a clear/rewrite and
    must be delivered from the divergence rather than silently discarded.
    """
    matcher = SequenceMatcher(None, previous, panel, autojunk=False)
    opcodes = matcher.get_opcodes()
    has_stable_anchor = any(
        tag == "equal"
        and old_start >= common
        and new_start >= common
        and old_end - old_start >= 2
        for tag, old_start, old_end, new_start, _new_end in opcodes
        if old_start > common or new_start > common
    )
    if not has_stable_anchor:
        return None

    fresh: list[int] = []
    stable_anchor_seen = False
    for tag, old_start, old_end, new_start, new_end in opcodes:
        if tag == "equal":
            if (
                old_start >= common
                and new_start >= common
                and old_end - old_start >= 2
                and (old_start > common or new_start > common)
            ):
                stable_anchor_seen = True
        elif tag == "insert":
            fresh.extend(range(new_start, new_end))
        elif tag == "delete":
            continue
        elif tag == "replace":
            old_size = old_end - old_start
            new_size = new_end - new_start
            if old_size != new_size and not stable_anchor_seen:
                return None
            aligned = min(old_size, new_size)
            if old_size != new_size and any(
                SequenceMatcher(
                    None,
                    previous[old_start + offset],
                    panel[new_start + offset],
                    autojunk=False,
                ).ratio() < 0.5
                for offset in range(aligned)
            ):
                # A stable prefix elsewhere does not prove that unrelated
                # replacement rows correspond positionally. Replaying is
                # safer than dropping one of two genuinely new rows.
                return None
            fresh.extend(range(new_start + aligned, new_end))
    return fresh
