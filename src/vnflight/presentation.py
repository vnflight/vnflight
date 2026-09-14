"""Pure story-presentation planning for composed handler results.

Bridge sequence numbers define chronology. Occurrence and overlay-delivery
identities define ownership. Keeping those rules here prevents act settling,
ordinary waits, and passive-overlay look-ahead from growing separate merge
policies again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


Section = dict[str, Any]
Identity = tuple[str, object]


def normalize_sections(value: object) -> list[Section]:
    """Validate private render sections without inventing provenance."""
    if not isinstance(value, list):
        return []
    normalized: list[Section] = []
    for raw in value:
        if (
            not isinstance(raw, dict)
            or raw.get("channel") not in ("text", "screen_text")
            or not isinstance(raw.get("text"), str)
            or not raw["text"].strip()
        ):
            continue
        section = dict(raw)
        ids = section.get("delivery_ids")
        if not (
            isinstance(ids, list)
            and len(ids) == 1
            and type(ids[0]) is int
        ):
            section.pop("delivery_ids", None)
        occurrences = section.get("occurrence_ids")
        if not (
            isinstance(occurrences, list)
            and len(occurrences) == 1
            and isinstance(occurrences[0], str)
        ):
            section.pop("occurrence_ids", None)
        if type(section.get("_bridge_seq")) is not int:
            section.pop("_bridge_seq", None)
        normalized.append(section)
    return normalized


def section_identities(section: Section) -> set[Identity]:
    return {
        (kind, value)
        for kind, values in (
            ("delivery", section.get("delivery_ids", [])),
            ("occurrence", section.get("occurrence_ids", [])),
        )
        for value in values
    }


def merge_sections(
    groups: Iterable[Iterable[Section]],
    *,
    join_story_text: Callable[[str, str], str],
) -> list[Section]:
    """Merge sections once, deduplicating only explicit occurrences."""
    merged: list[Section] = []
    seen: set[Identity] = set()
    for group in groups:
        for raw in group:
            section = dict(raw)
            identities = section_identities(section)
            if identities and identities.issubset(seen):
                continue
            seen.update(identities)
            text = str(section.get("text") or "").strip()
            channel = section.get("channel")
            if not text or channel not in ("text", "screen_text"):
                continue
            section["text"] = text
            if (
                merged
                and merged[-1]["channel"] == channel
                and not section_identities(merged[-1])
                and type(merged[-1].get("_bridge_seq")) is not int
                and not identities
                and type(section.get("_bridge_seq")) is not int
            ):
                if channel == "screen_text":
                    merged[-1]["text"] += "\n" + text
                else:
                    merged[-1]["text"] = join_story_text(
                        merged[-1]["text"], text)
            else:
                merged.append(section)
    if merged and all(type(item.get("_bridge_seq")) is int for item in merged):
        merged.sort(key=lambda item: item["_bridge_seq"])
    return merged


def merge_sections_by_bridge_sequence(
    sections: Iterable[Section],
    additions: Iterable[Section],
    *,
    join_story_text: Callable[[str, str], str],
) -> list[Section]:
    """Insert newly observed authoritative rows at their bridge position."""
    base = merge_sections([sections], join_story_text=join_story_text)
    incoming = merge_sections([additions], join_story_text=join_story_text)
    result = list(base)
    seen = {identity for section in base for identity in section_identities(section)}
    for section in incoming:
        identities = section_identities(section)
        if identities and identities.issubset(seen):
            continue
        bridge_seq = section.get("_bridge_seq")
        if type(bridge_seq) is not int:
            result.append(section)
        else:
            insert_at = len(result)
            for index, existing in enumerate(result):
                existing_seq = existing.get("_bridge_seq")
                if type(existing_seq) is int and existing_seq > bridge_seq:
                    insert_at = index
                    break
            result.insert(insert_at, section)
        seen.update(identities)
    return result


def render_sections(sections: Iterable[Section]) -> str:
    rendered = ""
    previous_channel = None
    for section in sections:
        if rendered:
            rendered += "\n" if section["channel"] == previous_channel else "\n\n"
        rendered += section["text"]
        previous_channel = section["channel"]
    return rendered


# ---------------------------------------------------------------------------
# Story text joining and shim source provenance
# ---------------------------------------------------------------------------

def _source_event_position(value: dict | None) -> tuple[str, int] | None:
    if not isinstance(value, dict):
        return None
    source_id = value.get("_source_id")
    source_seq = value.get("_source_seq")
    if isinstance(source_id, str) and source_id and type(source_seq) is int:
        return source_id, source_seq
    return None


def _join_story_text(previous_text: str, new_text: str) -> str:
    """Concatenate two rendered story blocks without repeating their overlap.

    Successive settle waits re-render an overlapping tail of the same burst
    (wait #1 sees lines 1-4, wait #2 sees lines 2-4).  A plain concatenation
    prints lines 2-4 twice, so drop the longest suffix of *previous_text* that
    the new block repeats as its prefix.
    """
    if not new_text:
        return previous_text
    if not previous_text:
        return new_text
    prev_lines = previous_text.splitlines()
    new_lines = new_text.splitlines()
    overlap = 0
    for size in range(min(len(prev_lines), len(new_lines)), 0, -1):
        if prev_lines[-size:] == new_lines[:size]:
            overlap = size
            break
    remainder = new_lines[overlap:]
    if not remainder:
        return previous_text
    return "\n".join(prev_lines + remainder)
