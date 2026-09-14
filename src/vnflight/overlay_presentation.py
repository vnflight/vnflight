"""Stateful passive-overlay and screen-text presentation ledger.

``overlay.py`` and ``overlay_ledger.py`` hold the pure differencing and
bounded-queue policy.  This module owns the half that needs a live handler
session: booking overlay occurrences from screens and drained bridge events,
fencing look-ahead rows until their durable events arrive, and claiming the
rows a composed response is allowed to present.

Every function takes the handler context only to reach ``ctx.client`` and the
overlay presentation state; nothing here imports ``handlers``.
"""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from .delivery_ownership import ActionDeliveryOwnership

from .client import (
    actionable_snapshots_equivalent,
    actionable_state_snapshot,
)
from .format import (
    _choice_interactions,
    _display_text,
    _normalize_label,
    _raw_pending_labels,
)
from .overlay import passive_row_delta_indices, passive_rows_delta
from .overlay_ledger import (
    hold_pending_deliveries,
    register_receipt,
    trim_oldest,
    trim_provisional_deliveries,
)
from .presentation import (
    _join_story_text,
    _source_event_position,
    merge_sections_by_bridge_sequence,
    normalize_sections,
    render_sections,
)


@dataclass
class OverlayPresentationState:
    """Every occurrence ledger a handler session owns, and how it is cleared.

    These fields advance together. Pending queues, durable receipts,
    provisional look-ahead markers and recovered-occurrence keys are three
    views of the same rows: clearing one without the others either replays a
    row that was already presented or strands a row that never will be. The
    two lifecycle wipes are therefore defined once, here, instead of being
    re-spelled at every site that abandons a timeline.
    """

    # Later public calls may overlap while booking or claiming the same screen
    # snapshot. The full occurrence ledger is one-owner-at-a-time; helpers
    # re-enter this lock while merge/drain operations hold it.
    lock: Any = field(default_factory=threading.RLock, repr=False)
    # Last passive-overlay snapshot booked from the current screen or drained
    # screen_content events. Position matters: repeated rows are
    # legitimate terminal output and must not collapse by string value.
    text_snapshot: list[str] = field(default_factory=list, repr=False)
    screen_key: tuple[str, ...] = field(default_factory=tuple, repr=False)
    retain_generation: bool = field(default=False, repr=False)
    contributor_snapshots: dict[str, list[str]] = field(
        default_factory=dict, repr=False)
    contributor_keys: dict[str, str] = field(default_factory=dict, repr=False)
    retained_contributors: set[str] = field(default_factory=set, repr=False)
    schema_mode: str = field(default="", repr=False)
    # Occurrence identities are local to this handler session. They let an
    # output promotion transfer a rendered row without guessing from text.
    delivery_serial: int = field(default=0, repr=False)
    # Overlay occurrences drained during a wait but not yet handed to output.
    pending_deliveries: list[dict] = field(default_factory=list, repr=False)
    # Transaction reads are replayable until acknowledged. Bridge-authored
    # passive deltas therefore need an event occurrence receipt in addition
    # to their content delta, or two settle reads assign the same row two
    # local delivery IDs.
    durable_receipts: dict[tuple[str, str, int], None] = field(
        default_factory=dict, repr=False)
    # A latest-screen scrape can run ahead of the ordered event cursor. Rows
    # delivered from that look-ahead are provisional until their durable
    # passive_overlay_delta events arrive; reconcile them instead of assigning
    # a second occurrence identity to the same source row.
    provisional_deliveries: list[dict] = field(default_factory=list, repr=False)
    # Callback lines this context already printed that a passive overlay row
    # may mirror. The in-batch matcher can only pair a callback with an
    # overlay row drained in the SAME response; a look-ahead row of the same
    # occurrence is fenced and flushes a call later, where it would read as a
    # new terminal line. Entries are consumed once and retired one
    # presentation boundary after they were printed.
    presented_callback_lines: list[dict] = field(
        default_factory=list, repr=False)
    # Rows recovered from a later cumulative snapshot still have an original
    # durable event that may arrive through another read lane. Claim that
    # eventual event by sequence + text occurrence instead of rendering it
    # again under its durable receipt identity.
    recovered_occurrences: dict[tuple[int, str, int], None] = field(
        default_factory=dict, repr=False)
    last_bridge_seq: int = field(default=0, repr=False)
    # Story rows opportunistically read from transcript after a pending
    # boundary. They remain in the bridge's ordinary drain, so remember their
    # sequence ids until that drain catches up and suppress the second copy.
    transcript_rescued_seqs: set[int] = field(default_factory=set, repr=False)
    # A latest-screen fallback can render a screen_text occurrence before its
    # durable transaction drain catches up. Remember that occurrence by bridge
    # and shim-source provenance so replayable transaction reads cannot show it
    # again in a later scene.
    rendered_screen_event_receipts: dict[
        tuple[str, str, int], tuple[str, ...]
    ] = field(default_factory=dict, repr=False)
    # Exact ordinary-screen body last rendered to the caller. This is separate
    # from the client's actionable-screen hint: exposing choices proves a
    # decision was shown, not that persistent screen chrome was included in
    # the response that carried those choices.
    ordinary_screen_presentation_receipt: tuple | None = field(
        default=None, repr=False)
    # Highest shim source sequence observed per source identity. Lifecycle
    # markers can be replayed by scoped transaction reads, so source order is
    # what distinguishes a real new timeline from an old marker seen late.
    timeline_source_sequences: dict[str, int] = field(
        default_factory=dict, repr=False)
    # A scoped ending can remain applied while Ren'Py already exposes the
    # title menu. Own that title occurrence once per action/run so a required
    # nonce recovery does not repeat navigation and GAME ENDED.
    terminal_presentation_receipts: set[tuple[int, str]] = field(
        default_factory=set, repr=False)

    def baseline(self) -> None:
        """Drop the delivery ledger for a game generation that just began.

        A ``game_started``/``game_resumed`` marker invalidates every unclaimed
        occurrence and every receipt that would have suppressed its replay.
        The booked panel itself is retired separately, by ending the passive
        overlay generation, because a retained overlay may legitimately
        survive the restart.
        """
        with self.lock:
            self.pending_deliveries = []
            self.durable_receipts = {}
            self.provisional_deliveries = []
            self.recovered_occurrences = {}
            self.presented_callback_lines = []

    def reset(self) -> None:
        """Discard everything owned by an abandoned story timeline.

        Timeline source sequences deliberately survive: they are what proves a
        replayed lifecycle marker is older than the timeline that replaced it.
        """
        with self.lock:
            self.text_snapshot = []
            self.screen_key = ()
            self.retain_generation = False
            self.contributor_snapshots = {}
            self.contributor_keys = {}
            self.retained_contributors = set()
            self.schema_mode = ""
            self.baseline()
            self.last_bridge_seq = 0
            self.delivery_serial = 0
            self.transcript_rescued_seqs.clear()
            self.rendered_screen_event_receipts.clear()
            self.ordinary_screen_presentation_receipt = None
            self.terminal_presentation_receipts.clear()


def _reset_timeline_context(
    ctx: Any,
    *,
    clear_client_prefetch: bool = True,
    clear_client_action_delivery: bool = True,
    preserve_client_action_nonce: str | None = None,
    preserve_client_action_nonces: set[str] | None = None,
) -> None:
    """Discard handler/client state owned by an abandoned story timeline."""
    ctx.overlay.reset()
    client = ctx.client
    client.last_request_id = None
    client.last_choices = None
    client.last_actionable_snapshot = None
    client._last_delivered_actionable_screen = None
    client._last_delivered_actionable_screen_signature = ()
    client._acted_request_id = None
    retire_action_nonces = getattr(
        client, "retire_auto_action_nonces", None)
    if callable(retire_action_nonces):
        retire_action_nonces(
            "timeline_reset",
            preserve_nonce=preserve_client_action_nonce,
            preserve_nonces=preserve_client_action_nonces,
        )
    if clear_client_action_delivery:
        reset_action_delivery = getattr(
            client, "reset_action_event_delivery", None)
        if callable(reset_action_delivery):
            reset_action_delivery()
    if clear_client_prefetch:
        ActionDeliveryOwnership._clear_held_events(client)


# One public tool call can perform several internal waits and ordinary state
# polls. Those nested reads belong to the same presentation consumer: they
# must not make a latest-screen look-ahead row appear safe merely because a
# poll completed. This holds the id() of the HandlerContext whose public call
# owns the current thread, so an internal merge can tell itself apart from the
# serialized public boundary, and a re-entrant handle_tool() on the same
# context does not deadlock on the (non-reentrant) presentation-call lock.
# It is a marker, not an identity: the lane admits one public call per context
# at a time, so there is nothing left to distinguish between calls.
_ACTIVE_PRESENTATION_INVOCATION: ContextVar[int | None] = (
    ContextVar("vnflight_presentation_invocation", default=None)
)


def _get_screen(
    ctx: Any, *, timeout: float = 2.0,
) -> dict | None:
    """Get the bridge's timeout-aware screen snapshot."""
    try:
        code, data = ctx.client._get("/screen", timeout=max(0.0, timeout))
        if code == 200 and data:
            return data.get("screen")
    except Exception:
        pass
    return None


def _game_state_matches_pending_choice_surface(
    pending: dict | None,
    game_state: dict | None,
) -> bool:
    """Prove a fresh game-state menu resolves the pending request."""
    if not isinstance(pending, dict) or not isinstance(game_state, dict):
        return False
    pending_interactions = _choice_interactions(pending)
    live_interactions = _choice_interactions(game_state)
    if not pending_interactions or not live_interactions:
        return False

    pending_labels = _raw_pending_labels(pending)
    live_labels = _raw_pending_labels({
        "choices": game_state.get("choices") or [],
    })
    if not live_labels:
        live_labels = [
            _normalize_label(
                interaction.get("display_label")
                or interaction.get("label")
                or ""
            )
            for interaction in live_interactions
            if not interaction.get("disabled")
            and not interaction.get("is_disabled")
            and not interaction.get("caption")
            and not interaction.get("is_caption")
        ]
        live_labels = [label for label in live_labels if label]
    if not pending_labels or pending_labels != live_labels:
        return False

    pending_request = {
        "type": pending.get("type"),
        "choices": list(pending_labels),
        "interactions": pending_interactions,
    }
    live_request = {
        "type": pending.get("type"),
        "choices": list(live_labels),
        "interactions": live_interactions,
    }
    previous = actionable_state_snapshot({
        "pending_request": pending_request,
        "game_state": {"interactions": pending_interactions},
    })
    current = actionable_state_snapshot({
        "pending_request": live_request,
        "game_state": {"interactions": live_interactions},
    })
    return actionable_snapshots_equivalent(previous, current)


def _game_state_proves_overlay_closed(
    pending: dict | None,
    game_state: dict | None,
    *,
    overlay_screen: dict | None,
    previous_game_state: dict | None,
) -> bool:
    """Require a matching menu sample newer than the covered overlay."""
    fresh_position = _source_event_position(game_state)
    overlay_position = _source_event_position(overlay_screen)
    previous_position = _source_event_position(previous_game_state)
    if (
        isinstance(overlay_screen, dict)
        and (
            overlay_screen.get("overlay_active")
            or overlay_screen.get("modal_screens")
            or overlay_screen.get("modal_overlay_screens")
        )
        and overlay_position is None
    ):
        # A present legacy/custom screen is still authoritative. Without its
        # source position, no state sample can prove it predates that overlay.
        return False
    # A visible overlay sample is the authoritative boundary. The first state
    # read may already be a stable post-close menu and need not advance again.
    # Without such a screen sample, the prior masked game_state is the bound.
    boundary = (
        overlay_position if overlay_position is not None
        else previous_position
    )
    if (
        fresh_position is None
        or boundary is None
        or fresh_position[0] != boundary[0]
        or fresh_position[1] <= boundary[1]
    ):
        return False
    return _game_state_matches_pending_choice_surface(pending, game_state)


# Overlay occurrences owned by a rendered output. Each record carries a stable
# session-local id, its text, and the channel currently rendering it. This is
# deliberately identity-based: equal text can be two legitimate terminal rows.
_OVERLAY_DELIVERIES_KEY = "_overlay_deliveries"


# Ledger bookkeeping stamped on overlay delivery records while they are held
# or fenced. Public copies (result payloads, hub pushes) never carry them.
_OVERLAY_LEDGER_RECORD_KEYS = (
    "_durable_receipt",
    "_recovered_occurrence",
    "_cursor_fenced",
    "_cursor_fenced_poll_serial",
    "_cursor_fenced_source_id",
    "_cursor_fenced_source_seq",
)


_SCREEN_TEXT_BEFORE_STORY_KEY = "_screen_text_before_story"


_STORY_RENDER_SECTIONS_KEY = "_story_render_sections"


def _overlay_delivery_records(out: dict) -> list[dict]:
    return [
        dict(record)
        for record in (out.get(_OVERLAY_DELIVERIES_KEY) or [])
        if isinstance(record, dict)
        and type(record.get("id")) is int
        and isinstance(record.get("text"), str)
        and record.get("channel") in ("story", "screen_text")
    ]


def _merge_overlay_delivery_records(out: dict, records: list[dict]) -> None:
    """Merge ownership by identity in canonical occurrence order."""
    merged = _overlay_delivery_records(out)
    positions = {
        record["id"]: index for index, record in enumerate(merged)
    }
    for record in records:
        if (
            type(record.get("id")) is int
            and isinstance(record.get("text"), str)
            and record.get("channel") in ("story", "screen_text")
        ):
            clean = dict(record)
            position = positions.get(record["id"])
            if position is None:
                positions[record["id"]] = len(merged)
                merged.append(clean)
            else:
                merged[position] = clean
    if merged:
        out[_OVERLAY_DELIVERIES_KEY] = merged
    else:
        out.pop(_OVERLAY_DELIVERIES_KEY, None)


def _render_value_text(value: object) -> str:
    if isinstance(value, str):
        text = value.strip()
        return "" if text == "(no new events)" else text
    if isinstance(value, list):
        texts = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(
            text for text in texts if text != "(no new events)").strip()
    return ""


def _find_rendered_block(value: str, block: str) -> int:
    """Find a complete line/block occurrence, never a substring of a row."""
    start = 0
    while block:
        index = value.find(block, start)
        if index < 0:
            return -1
        end = index + len(block)
        before_boundary = index == 0 or value[index - 1] in "\r\n"
        after_boundary = end == len(value) or value[end] in "\r\n"
        if before_boundary and after_boundary:
            return index
        start = index + 1
    return -1


def _story_render_sections(out: dict) -> list[dict]:
    """Return the private chronological story presentation for an output."""
    existing = out.get(_STORY_RENDER_SECTIONS_KEY)
    if isinstance(existing, list):
        return [
            section for section in normalize_sections(existing)
            if _render_value_text(section.get("text"))
        ]

    deliveries = _overlay_delivery_records(out)
    order = ("screen_text", "text") if out.get(
        _SCREEN_TEXT_BEFORE_STORY_KEY
    ) else ("text", "screen_text")
    sections: list[dict] = []
    for channel in order:
        value = _render_value_text(out.get(channel))
        if not value:
            continue
        delivery_channel = "story" if channel == "text" else "screen_text"
        channel_deliveries = [
            record for record in deliveries
            if record.get("channel") == delivery_channel
        ]
        if channel_deliveries:
            # Delivery occurrences stay atomic. A section that combines an
            # old occurrence with fresh text cannot be safely deduplicated:
            # dropping the old id would drop the fresh suffix too. Match the
            # aggregate string directly so multiline terminal rows retain
            # their identity as well.
            remaining = value
            for record in channel_deliveries:
                index = _find_rendered_block(remaining, record["text"])
                if index < 0:
                    continue
                prefix = remaining[:index].strip()
                if prefix:
                    sections.append({"channel": channel, "text": prefix})
                sections.append({
                    "channel": channel,
                    "text": record["text"],
                    "delivery_ids": [record["id"]],
                    **(
                        {"_bridge_seq": record["_bridge_seq"]}
                        if type(record.get("_bridge_seq")) is int else {}
                    ),
                })
                remaining = remaining[index + len(record["text"]):]
            suffix = remaining.strip()
            if suffix:
                sections.append({"channel": channel, "text": suffix})
        else:
            section = {"channel": channel, "text": value}
            ids = [record["id"] for record in channel_deliveries]
            if ids:
                section["delivery_ids"] = ids
            sections.append(section)
    return sections


def _merge_story_render_sections_by_bridge_sequence(
    sections: list[dict], additions: list[dict],
) -> list[dict]:
    """Insert late-observed sections at their original bridge position.

    A passive overlay snapshot can reach the handler after newer narration has
    already been formatted. Its bridge sequence is still authoritative, so do
    not append that row merely because the latest-screen merge observed it
    last. Sections without sequence provenance keep their existing order.
    """
    return merge_sections_by_bridge_sequence(
        sections, additions, join_story_text=_join_story_text)


def _render_story_section_plan(section_plan: list[dict]) -> str:
    """Render atomic chronology sections without spacing terminal rows out."""
    return render_sections(section_plan)


def _order_overlay_deliveries_by_sections(
    out: dict, sections: list[dict],
) -> None:
    """Keep private ownership records in their rendered occurrence order."""
    records = _overlay_delivery_records(out)
    if not records:
        return
    by_id = {record["id"]: record for record in records}
    ordered: list[dict] = []
    seen: set[int] = set()
    for section in sections:
        for delivery_id in section.get("delivery_ids", []):
            record = by_id.get(delivery_id)
            if record is not None and delivery_id not in seen:
                ordered.append(record)
                seen.add(delivery_id)
    ordered.extend(record for record in records if record["id"] not in seen)
    out[_OVERLAY_DELIVERIES_KEY] = ordered


def _carry_overlay_deliveries_into(
    out: dict,
    deliveries: list[dict],
    *,
    as_list: bool,
) -> None:
    """Transfer specific overlay occurrences into a replacement output."""
    current = _overlay_delivery_records(out)
    current_ids = {record["id"] for record in current}
    carry = [record for record in deliveries if record["id"] not in current_ids]
    rows = [record["text"] for record in carry]
    existing = out.get("screen_text")
    existing_story = out.get("text")
    if rows:
        # Promotion replaces an older render with a later wait. If that wait
        # has story, these older rows happened before it. Preserve their API
        # channel and record the presentation order instead of folding screen
        # rows into narration.
        if isinstance(existing_story, str) and existing_story.strip():
            out[_SCREEN_TEXT_BEFORE_STORY_KEY] = True
        if isinstance(existing, list):
            out["screen_text"] = rows + existing
        elif isinstance(existing, str) and existing.strip():
            out["screen_text"] = "\n".join(rows) + "\n" + existing.lstrip("\n")
        elif as_list:
            out["screen_text"] = rows
        else:
            out["screen_text"] = "\n".join(rows)
    transferred = [
        {**record, "channel": "screen_text"}
        for record in carry
    ]
    _merge_overlay_delivery_records(out, transferred + current)


# Bound on occurrences held between decision points (a runaway log must not
# grow the session's memory without limit); the newest occurrences are kept.
_OVERLAY_PENDING_DELIVERY_LIMIT = 500


# Bound on printed callback lines a held overlay row may still be recognised
# as mirroring. Entries expire by presentation boundary; this only caps a
# single response that prints an implausible number of callbacks.
_PRESENTED_CALLBACK_LINE_LIMIT = 128


# One settle before believing a panel closed, so a scrape taken between two
# frames is not mistaken for the window closing.
_OVERLAY_CLOSE_CONFIRM_DELAY = 0.2


def _passive_overlay_rows(screen: dict | None) -> list[str]:
    """Non-empty registered-overlay rows carried by a screen snapshot."""
    if not isinstance(screen, dict):
        return []
    texts = screen.get("overlay_texts") or []
    return [line for line in (str(t).strip() for t in texts) if line]


def _passive_overlay_rows_by_screen(
    screen: dict | None,
) -> list[tuple[str, list[str]]]:
    """Return contributor-owned rows, including explicit empty panels."""
    if not isinstance(screen, dict):
        return []
    raw = screen.get("overlay_texts_by_screen")
    if not isinstance(raw, dict) or not raw:
        return []
    order = [str(tag) for tag in (screen.get("overlay_screens") or [])]
    order.extend(str(tag) for tag in raw if str(tag) not in order)
    contributors = []
    for tag in order:
        texts = raw.get(tag)
        if not isinstance(texts, list):
            continue
        rows = [line for line in (str(text).strip() for text in texts) if line]
        contributors.append((tag, rows))
    return contributors


def _passive_overlay_key(screen: dict | None) -> tuple[str, ...]:
    """Stable identity of the overlays that contributed a snapshot's rows.

    ``screens`` contains every scraped screen and changes when unrelated
    notifications, choices, or HUD panels appear.  New shims provide the
    registered overlays that actually contributed these rows; old shims omit
    the field and naturally share the empty fallback key, leaving the ordered
    content overlap to detect replacement.
    """
    if not isinstance(screen, dict):
        return ()
    generations = screen.get("overlay_generations") or {}
    if not isinstance(generations, dict):
        generations = {}
    return tuple(
        f"{tag}@{generations[tag]}" if tag in generations else str(tag)
        for tag in (screen.get("overlay_screens") or [])
    )


def _passive_overlay_retains_generation(screen: dict | None) -> bool:
    """Whether any contributor owns its close/reopen generation explicitly."""
    if not isinstance(screen, dict):
        return False
    contributors = set(screen.get("overlay_screens") or [])
    retained = set(screen.get("overlay_retained_screens") or [])
    return bool(contributors & retained)


def _passive_overlay_delta(previous: list[str], panel: list[str]) -> list[str]:
    """Return rows appended to an ordered, possibly rolling panel."""
    return passive_rows_delta(previous, panel)


def _book_passive_overlay_snapshot(
    ctx: Any,
    screen: dict | None,
    *,
    confirm_absent: bool = False,
) -> list[dict]:
    """Book a snapshot and return identified occurrences for its new rows.

    Delivery is based on ordered snapshot overlap rather than text membership.
    Repeated rows therefore remain distinct, while an append-only log costs
    each occurrence once.  A divergent snapshot starts a new panel generation,
    as a terminal clear/reset does.

    A snapshot that is a strict PREFIX of what the ledger already holds is a
    stale re-scrape of the same generation, not a rewrite: successive events
    may race with the bridge's latest screen, which serves whichever
    ``screen_content`` arrived last, so treating a short read as replacement
    would re-print the whole panel.  Such a snapshot books nothing and does
    not shrink the ledger; a genuine restart diverges from the old rows and
    is delivered from the divergence on.
    """
    bridge_seq = screen.get("_seq") if isinstance(screen, dict) else None

    def _delivery(
        row: str, row_seq: object = None, contributor: str | None = None,
    ) -> dict:
        ctx.overlay.delivery_serial += 1
        provenance = row_seq if type(row_seq) is int else bridge_seq
        return {
            "id": ctx.overlay.delivery_serial,
            "text": row,
            **({"_overlay_contributor": contributor} if contributor else {}),
            **({"_bridge_seq": provenance}
               if type(provenance) is int else {}),
        }

    raw_sequences = screen.get("passive_overlay_row_seqs_by_screen")
    sequences_by_screen = (
        raw_sequences if isinstance(raw_sequences, dict) else {}
    )
    raw_aggregate_sequences = screen.get("passive_overlay_row_seqs")
    aggregate_sequences = (
        raw_aggregate_sequences
        if isinstance(raw_aggregate_sequences, list) else []
    )

    def _deliver_delta(
        previous: list[str], rows: list[str], row_sequences: object,
        contributor: str | None = None,
    ) -> list[dict]:
        sequences = row_sequences if isinstance(row_sequences, list) else []
        return [
            _delivery(
                rows[index],
                sequences[index] if index < len(sequences) else None,
                contributor,
            )
            for index in passive_row_delta_indices(previous, rows)
        ]

    contributors = _passive_overlay_rows_by_screen(screen)
    panel = _passive_overlay_rows(screen)
    if not panel and not contributors:
        return []
    if contributors:
        generations = screen.get("overlay_generations") or {}
        if not isinstance(generations, dict):
            generations = {}
        active_tags = {tag for tag, _rows in contributors}
        current_retained = set(screen.get("overlay_retained_screens") or [])
        if ctx.overlay.schema_mode == "aggregate":
            # A running handler can outlive a shim replacement. The first
            # contributor-aware snapshot is a synchronization boundary: the
            # aggregate schema cannot tell us which contributor owned prior
            # rows, so replaying them would be less honest than seeding them.
            aggregate_previous = list(ctx.overlay.text_snapshot)
            ctx.overlay.contributor_snapshots = {
                tag: list(rows) for tag, rows in contributors}
            ctx.overlay.contributor_keys = {
                tag: (f"{tag}@{generations[tag]}"
                      if tag in generations else tag)
                for tag, _rows in contributors
            }
            ctx.overlay.retained_contributors = {
                str(tag) for tag in current_retained if str(tag) in active_tags}
            ctx.overlay.text_snapshot = list(panel)
            ctx.overlay.screen_key = _passive_overlay_key(screen)
            ctx.overlay.retain_generation = bool(
                ctx.overlay.retained_contributors)
            ctx.overlay.schema_mode = "contributors"
            return _deliver_delta(
                aggregate_previous, panel, aggregate_sequences)
        ctx.overlay.schema_mode = "contributors"
        missing_tags = {
            tag for tag in ctx.overlay.contributor_snapshots
            if tag not in active_tags
            and tag not in ctx.overlay.retained_contributors
        }
        deliveries = []
        for tag, rows in contributors:
            key = f"{tag}@{generations[tag]}" if tag in generations else tag
            prior_key = ctx.overlay.contributor_keys.get(tag)
            previous = (
                ctx.overlay.contributor_snapshots.get(tag, [])
                if prior_key == key
                else []
            )
            if (
                not rows
                and prior_key == key
                and tag in current_retained
            ):
                continue
            if rows and len(rows) < len(previous) and previous[:len(rows)] == rows:
                continue
            deliveries.extend(_deliver_delta(
                previous,
                rows,
                sequences_by_screen.get(tag),
                tag,
            ))
            ctx.overlay.contributor_snapshots[tag] = list(rows)
            ctx.overlay.contributor_keys[tag] = key
        ctx.overlay.retained_contributors.difference_update(active_tags)
        ctx.overlay.retained_contributors.update(
            str(tag) for tag in current_retained if str(tag) in active_tags)
        ctx.overlay.text_snapshot = list(panel)
        ctx.overlay.screen_key = _passive_overlay_key(screen)
        ctx.overlay.retain_generation = bool(
            ctx.overlay.retained_contributors)
        if confirm_absent and missing_tags:
            confirmed = _confirm_passive_overlay_closed(ctx)
            if confirmed is None:
                confirmed_active: set[str] = set()
            elif confirmed is _OVERLAY_CLOSE_UNKNOWN:
                confirmed_active = set(ctx.overlay.contributor_snapshots)
            else:
                confirmed_active = {
                    tag for tag, _rows in
                    _passive_overlay_rows_by_screen(confirmed)
                }
                deliveries.extend(_book_passive_overlay_snapshot(
                    ctx, confirmed))
            for tag in missing_tags - confirmed_active:
                ctx.overlay.contributor_snapshots.pop(tag, None)
                ctx.overlay.contributor_keys.pop(tag, None)
        return deliveries
    if ctx.overlay.schema_mode == "contributors":
        # The first legacy aggregate snapshot after a schema downgrade cannot
        # be apportioned safely. Seed it and let later aggregate deltas carry
        # news, avoiding replay of every contributor-owned row.
        aggregate_previous = list(ctx.overlay.text_snapshot)
        ctx.overlay.contributor_snapshots = {}
        ctx.overlay.contributor_keys = {}
        ctx.overlay.retained_contributors = set()
        ctx.overlay.text_snapshot = list(panel)
        ctx.overlay.screen_key = _passive_overlay_key(screen)
        ctx.overlay.retain_generation = _passive_overlay_retains_generation(
            screen)
        ctx.overlay.schema_mode = "aggregate"
        return _deliver_delta(
            aggregate_previous, panel, aggregate_sequences)
    ctx.overlay.schema_mode = "aggregate"
    screen_key = _passive_overlay_key(screen)
    previous = (
        ctx.overlay.text_snapshot
        if screen_key == ctx.overlay.screen_key
        else []
    )
    if len(panel) < len(previous) and previous[:len(panel)] == panel:
        return []
    deliveries = _deliver_delta(previous, panel, aggregate_sequences)
    ctx.overlay.text_snapshot = list(panel)
    ctx.overlay.screen_key = screen_key
    ctx.overlay.retain_generation = _passive_overlay_retains_generation(screen)
    return deliveries


def _end_passive_overlay_generation(ctx: Any) -> None:
    """Close the current panel generation; reopening redelivers its rows."""
    ctx.overlay.text_snapshot = []
    ctx.overlay.screen_key = ()
    ctx.overlay.retain_generation = False
    ctx.overlay.contributor_snapshots = {}
    ctx.overlay.contributor_keys = {}
    ctx.overlay.retained_contributors = set()
    ctx.overlay.schema_mode = ""


def _close_absent_overlay_contributors(ctx: Any) -> None:
    """Close non-retained contributors while preserving retained ledgers."""
    for tag in list(ctx.overlay.contributor_snapshots):
        if tag not in ctx.overlay.retained_contributors:
            ctx.overlay.contributor_snapshots.pop(tag, None)
            ctx.overlay.contributor_keys.pop(tag, None)
    if not ctx.overlay.contributor_snapshots:
        _end_passive_overlay_generation(ctx)
        return
    ctx.overlay.text_snapshot = [
        row
        for rows in ctx.overlay.contributor_snapshots.values()
        for row in rows
    ]
    ctx.overlay.screen_key = tuple(
        ctx.overlay.contributor_keys.values())
    ctx.overlay.retain_generation = True


def _hold_passive_overlay_deliveries(
    ctx: Any, deliveries: list[dict],
) -> None:
    if not deliveries:
        return
    with ctx.overlay.lock:
        hold_pending_deliveries(
            ctx.overlay.pending_deliveries,
            ctx.overlay.durable_receipts,
            ctx.overlay.provisional_deliveries,
            ctx.overlay.recovered_occurrences,
            deliveries,
            _OVERLAY_PENDING_DELIVERY_LIMIT,
        )


def _claim_passive_overlay_deliveries(ctx: Any) -> list[dict]:
    """Atomically transfer ownership of deferred rows to one public call."""
    with ctx.overlay.lock:
        claimed = ctx.overlay.pending_deliveries
        ctx.overlay.pending_deliveries = []
    return claimed


def _remember_presented_callback_lines(
    ctx: Any, callback_occurrences: dict[str, list[dict]],
) -> None:
    """Remember callback lines printed without an overlay row to mirror them.

    ``_book_drained_overlay_events_locked`` pairs a drained overlay row with
    the character callback of the same occurrence and transfers the overlay
    identity onto the callback, so the line prints once.  That matching is
    per-response: an overlay row observed by the look-ahead instead of the
    drain is fenced behind the cursor and flushes on a later call, where it
    reads as a brand new terminal line.  The callbacks left unpaired here are
    exactly the ones such a held row can still mirror.
    """
    for line, candidates in callback_occurrences.items():
        for candidate in candidates:
            if candidate.get("overlay_delivery_id") is not None:
                continue
            if candidate.get("passive_overlay_snapshot"):
                continue
            candidate_seq = candidate.get("_seq")
            ctx.overlay.presented_callback_lines.append({
                "line": line,
                "seq": candidate_seq if type(candidate_seq) is int else None,
                "aged": False,
            })
    overflow = (
        len(ctx.overlay.presented_callback_lines) - _PRESENTED_CALLBACK_LINE_LIMIT
    )
    if overflow > 0:
        del ctx.overlay.presented_callback_lines[:overflow]


def _claim_presented_callback_mirror(
    ctx: Any, record: dict,
) -> bool:
    """Consume the printed callback that a held overlay row merely mirrors."""
    text = record.get("text")
    if not text:
        return False
    record_seq = record.get("_bridge_seq")
    for index, entry in enumerate(ctx.overlay.presented_callback_lines):
        if entry["line"] != text:
            continue
        entry_seq = entry["seq"]
        if (
            type(entry_seq) is int
            and type(record_seq) is int
            and entry_seq > record_seq
        ):
            # A callback printed after this row is a different occurrence,
            # exactly as in the in-batch matcher.
            continue
        del ctx.overlay.presented_callback_lines[index]
        return True
    return False


def _age_presented_callback_lines(ctx: Any) -> None:
    """Retire mirror memory one presentation boundary after it was printed.

    A held row mirrors the callback of the response that printed it or of the
    one immediately before; beyond that the terminal is legitimately repeating
    itself, and repeating a line is the failure this layer accepts.  Entries
    are recorded before their own response's flush, survive it, and are
    dropped at the next one.
    """
    remaining = []
    for entry in ctx.overlay.presented_callback_lines:
        if entry["aged"]:
            continue
        entry["aged"] = True
        remaining.append(entry)
    ctx.overlay.presented_callback_lines = remaining


def _book_durable_passive_overlay_delta(
    ctx: Any, screen: dict, *, authoritative_drain: bool = False,
) -> list[dict] | None:
    """Book a bridge-authored delta, or return None for legacy snapshots."""
    durable_delta = screen.get("passive_overlay_delta")
    if not isinstance(durable_delta, list):
        return None
    screen_key = _passive_overlay_key(screen)
    previous_schema_mode = ctx.overlay.schema_mode
    previous_screen_key = ctx.overlay.screen_key
    previous_contributor_keys = dict(ctx.overlay.contributor_keys)
    had_baseline = bool(
        previous_schema_mode
        and (ctx.overlay.text_snapshot or ctx.overlay.contributor_snapshots)
    )
    # Always synchronize from the full panel. The returned local delta is
    # normally redundant with the bridge delta. A persistent handler can,
    # however, miss one durable event while another command is confirming.
    # Per-row bridge provenance lets the next cumulative snapshot recover that
    # older occurrence without making a fresh/one-shot handler replay history.
    local_delta = _book_passive_overlay_snapshot(ctx, screen)
    source_id = str(screen.get("_source_id") or "")
    source_seq = str(
        screen.get("_source_seq")
        if screen.get("_source_seq") is not None
        else screen.get("_seq", "")
    )
    event_seq = screen.get("_seq")
    sequenced = type(event_seq) is int
    fresh = []
    durable_occurrences = {}

    def _claim_held(receipt: tuple) -> dict | None:
        if not authoritative_drain:
            return None
        for index, record in enumerate(ctx.overlay.pending_deliveries):
            if record.get("_durable_receipt") == receipt:
                record = ctx.overlay.pending_deliveries.pop(index)
                if _claim_presented_callback_mirror(ctx, record):
                    return None
                for key in ("_cursor_fenced", "_cursor_fenced_poll_serial",
                            "_cursor_fenced_source_id", "_cursor_fenced_source_seq"):
                    record.pop(key, None)
                return record
        return None

    def _claim_provisional(row: str, row_seq: object) -> dict | None:
        for index, record in enumerate(ctx.overlay.provisional_deliveries):
            if record.get("text") != row:
                continue
            provisional_seq = record.get("_bridge_seq")
            if type(row_seq) is int and type(provisional_seq) is int:
                if provisional_seq != row_seq:
                    continue
            elif record.get("screen_key") != screen_key:
                continue
            return ctx.overlay.provisional_deliveries.pop(index)
        return None

    for row_index, value in enumerate(durable_delta):
        row = str(value).strip()
        if not row:
            continue
        occurrence_key = (event_seq, row)
        occurrence = durable_occurrences.get(occurrence_key, 0)
        durable_occurrences[occurrence_key] = occurrence + 1
        receipt = (source_id, source_seq, row_index)
        if not register_receipt(ctx.overlay.durable_receipts, receipt):
            # A live-screen read may have booked this row without rendering
            # it. Its ordered drain now owns delivery, not the older fence.
            held = _claim_held(receipt)
            if held is not None:
                fresh.append(held)
            continue
        recovered_key = (event_seq, row, occurrence)
        if sequenced and recovered_key in ctx.overlay.recovered_occurrences:
            ctx.overlay.recovered_occurrences.pop(recovered_key, None)
            for pending in ctx.overlay.pending_deliveries:
                if pending.get("_recovered_occurrence") == recovered_key:
                    pending.pop("_recovered_occurrence", None)
                    pending["_durable_receipt"] = receipt
                    break
            held = _claim_held(receipt)
            if held is not None:
                fresh.append(held)
            continue
        provisional = _claim_provisional(row, event_seq)
        if provisional is not None:
            # The latest-screen occurrence may still be fenced in the pending
            # queue. Transfer durable ownership onto that exact occurrence so
            # a later queue eviction can release the receipt for replay.
            provisional_id = provisional.get("id")
            for pending in ctx.overlay.pending_deliveries:
                if pending.get("id") == provisional_id:
                    pending["_durable_receipt"] = receipt
                    break
            held = _claim_held(receipt)
            if held is not None:
                fresh.append(held)
            continue
        if sequenced:
            # Negative IDs occupy a separate namespace from local positive
            # delivery serials and remain stable across replayed reads.
            delivery_id = -(event_seq * 10000 + row_index + 1)
        else:
            ctx.overlay.delivery_serial += 1
            delivery_id = ctx.overlay.delivery_serial
        fresh.append({
            "id": delivery_id,
            "text": row,
            "_durable_receipt": receipt,
            **({"_bridge_seq": event_seq} if sequenced else {}),
        })
    if had_baseline and sequenced and local_delta:
        local_occurrences = {}
        for record in local_delta:
            row_seq = record.get("_bridge_seq")
            if type(row_seq) is not int:
                continue
            contributor = record.get("_overlay_contributor")
            if contributor:
                generations = screen.get("overlay_generations") or {}
                if not isinstance(generations, dict):
                    generations = {}
                current_key = (
                    f"{contributor}@{generations[contributor]}"
                    if contributor in generations else contributor
                )
                if previous_contributor_keys.get(contributor) != current_key:
                    continue
            elif (
                previous_schema_mode != "aggregate"
                or previous_screen_key != screen_key
            ):
                continue
            key = (row_seq, record["text"])
            occurrence = local_occurrences.get(key, 0)
            local_occurrences[key] = occurrence + 1
            if occurrence < durable_occurrences.get(key, 0):
                continue
            if _claim_provisional(record["text"], row_seq) is not None:
                continue
            recovered_key = (row_seq, record["text"], occurrence)
            ctx.overlay.recovered_occurrences[recovered_key] = None
            record["_recovered_occurrence"] = recovered_key
            fresh.append(record)
        if fresh and all(type(record.get("_bridge_seq")) is int for record in fresh):
            fresh.sort(key=lambda record: record["_bridge_seq"])
    trim_oldest(
        ctx.overlay.durable_receipts,
        limit=4096,
        protected={
            record.get("_durable_receipt")
            for record in ctx.overlay.pending_deliveries
            if isinstance(record.get("_durable_receipt"), tuple)
        },
    )
    trim_oldest(
        ctx.overlay.recovered_occurrences,
        limit=4096,
        protected={
            record.get("_recovered_occurrence")
            for record in ctx.overlay.pending_deliveries
            if isinstance(record.get("_recovered_occurrence"), tuple)
        },
    )
    return fresh


def _remember_provisional_overlay_deliveries(
    ctx: Any,
    records: list[dict],
    screen: dict,
) -> None:
    """Remember rows observed by latest-screen look-ahead before the cursor."""
    screen_key = _passive_overlay_key(screen)
    ctx.overlay.provisional_deliveries.extend({
        "id": record["id"],
        "text": record["text"],
        "screen_key": screen_key,
        **(
            {"_bridge_seq": record["_bridge_seq"]}
            if type(record.get("_bridge_seq")) is int else {}
        ),
    } for record in records)
    _trim_provisional_overlay_ownership(
        ctx, incoming_ids={record.get("id") for record in records})


def _trim_provisional_overlay_ownership(
    ctx: Any,
    *,
    incoming_ids: set[object] | None = None,
) -> None:
    """Bound provisional claims while retaining pending/in-flight owners."""
    trim_provisional_deliveries(
        ctx.overlay.provisional_deliveries,
        limit=500,
        protected_ids={
            record.get("id")
            for record in ctx.overlay.pending_deliveries
        } | (incoming_ids or set()),
    )


def _passive_overlay_ahead_of_presentation(
    ctx: Any, bridge_seq: object,
) -> bool:
    """Return whether a latest-state row is ahead of visible chronology.

    ``GameClient.cursor`` is a fetch high-water mark: command polling may
    advance it while preserving older rows in ``_prefetched_events`` for the
    next visible wait.  A live ``/screen`` sample must not leapfrog that
    backlog merely because the network cursor has crossed its sequence.
    Cursor zero is likewise an unknown/fresh timeline, not proof that a
    positive-sequence row is ready to present.
    """
    if type(bridge_seq) is not int:
        return False
    cursor = getattr(ctx.client, "cursor", None)
    if type(cursor) is not int or cursor <= 0 or bridge_seq > cursor:
        return True
    for event in list(getattr(ctx.client, "_prefetched_events", []) or []):
        if not isinstance(event, dict):
            continue
        event_seq = event.get("_seq")
        if type(event_seq) is int and event_seq <= bridge_seq:
            return True
        if type(event_seq) is not int and event.get("type") in {
            "dialogue", "narration", "screen_text", "auto_skipped",
        }:
            # An unsequenced visible row cannot be proven newer. Preserve the
            # duplicate-side bias until the ordinary lane consumes it.
            return True
    return False


def _passive_overlay_fence_still_active(
    ctx: Any,
    record: dict,
    *,
    release_owner_fences: bool = True,
) -> bool:
    """Keep a look-ahead row behind known or not-yet-polled chronology."""
    bridge_seq = record.get("_bridge_seq")
    if type(bridge_seq) is not int:
        return False
    if not release_owner_fences:
        # Internal settle results may be promoted, replaced, or discarded.
        # A cursor-fenced occurrence can leave the ledger only at the
        # serialized public boundary where its destination is final.
        return True
    for event in list(getattr(ctx.client, "_prefetched_events", []) or []):
        if not isinstance(event, dict):
            continue
        event_seq = event.get("_seq")
        if type(event_seq) is int and event_seq <= bridge_seq:
            return True
        if type(event_seq) is not int and event.get("type") in {
            "dialogue", "narration", "screen_text", "auto_skipped",
        }:
            return True
    source_id = record.get("_cursor_fenced_source_id")
    source_seq = record.get("_cursor_fenced_source_seq")
    source_position_unseen = bool(
        isinstance(source_id, str)
        and source_id
        and type(source_seq) is int
        and source_seq > ctx.overlay.timeline_source_sequences.get(source_id, 0)
    )
    cursor = getattr(ctx.client, "cursor", None)
    if (
        not source_position_unseen
        and type(cursor) is int
        and cursor > 0
        and bridge_seq <= cursor
    ):
        # The ordinary lane has reached this row's chronology.
        return False
    fenced_poll = record.get("_cursor_fenced_poll_serial")
    current_poll = getattr(ctx.client, "_state_poll_serial", None)
    if (
        type(fenced_poll) is int
        and type(current_poll) is int
        and current_poll > fenced_poll
    ):
        # A later successful ordinary poll found no predecessor to retain.
        # This is the bounded escape for a durable screen event that was
        # pruned or otherwise absent from the transcript.  The presentation
        # lane admits one public call per context, so "later poll" and "later
        # call" cannot disagree here: every poll this fence can observe was
        # either taken by the call that fenced the row -- which reaches this
        # branch only at its own serialized flush, where its destination is
        # final -- or by a call that started after that one returned.
        return False
    return True


def _fence_latest_passive_overlay_deliveries(
    ctx: Any, records: list[dict], screen: dict,
) -> None:
    """Mark latest-state rows that are not yet safe to present."""
    screen_source_id = str(screen.get("_source_id") or "")
    screen_source_seq = screen.get("_source_seq")
    source_position_unseen = bool(
        screen_source_id
        and type(screen_source_seq) is int
        and screen_source_seq > ctx.overlay.timeline_source_sequences.get(
            screen_source_id, 0)
    )
    for record in records:
        record_seq = record.get("_bridge_seq")
        if not (
            source_position_unseen
            or _passive_overlay_ahead_of_presentation(ctx, record_seq)
        ):
            continue
        record["_cursor_fenced"] = True
        record["_cursor_fenced_poll_serial"] = getattr(
            ctx.client, "_state_poll_serial", 0)
        if source_position_unseen:
            record["_cursor_fenced_source_id"] = screen_source_id
            record["_cursor_fenced_source_seq"] = screen_source_seq


def _book_drained_overlay_events(
    ctx: Any, result: object,
) -> list[dict]:
    """Book a drained batch under the context's occurrence-ledger lock."""
    with ctx.overlay.lock:
        return _book_drained_overlay_events_locked(ctx, result)


def _book_drained_overlay_events_locked(
    ctx: Any, result: object,
) -> list[dict]:
    """Turn drained overlay deltas into chronologically placed story events.

    The bridge retains changed passive ``screen_content`` snapshots in its
    sequenced stream while keeping ordinary screen state latest-only. This
    books those snapshots through the same ledger.  Fresh rows replace their
    source snapshot with a synthetic ``screen_text`` event in the same list
    position.  That keeps terminal output between the narration/dialogue that
    actually surrounded it instead of appending the whole panel at the end of
    the formatted response.

    Return identified occurrences owned by this response's text channel, so a
    later wait-output promotion can transfer them without matching strings.
    """
    events = getattr(result, "events", None)
    if not isinstance(events, list):
        return []
    inline_with_story = any(
        isinstance(event, dict)
        and event.get("type") in {"dialogue", "narration", "screen_text"}
        and not event.get("passive_overlay_snapshot")
        for event in events
    )
    transaction = getattr(result, "transaction", None)
    transaction = transaction if isinstance(transaction, dict) else {}
    transaction_action_id = transaction.get("action_id")
    transaction_source_id = transaction.get("_source_id")
    transaction_source_after = transaction.get("_source_seq")

    def callback_line(event: dict) -> str | None:
        if event.get("type") == "dialogue":
            who = event.get("character") or event.get("who") or "Narrator"
            text = _display_text(event.get("text") or event.get("what", ""))
            prefix = "[user] " if event.get("user_initiated") else ""
            return "{}[{}] {}".format(prefix, who, text)
        if event.get("type") == "narration":
            text = _display_text(event.get("text", ""))
            return text if text.strip() else None
        return None

    def belongs_to_transaction(event: dict) -> bool:
        if event.get("action_id") == transaction_action_id:
            return transaction_action_id is not None
        source_seq = event.get("_source_seq")
        return bool(
            not event.get("action_id")
            and transaction_source_id
            and event.get("_source_id") == transaction_source_id
            and type(transaction_source_after) is int
            and type(source_seq) is int
            and source_seq > transaction_source_after
        )

    callback_occurrences: dict[str, list[dict]] = {}
    for candidate in events:
        if not isinstance(candidate, dict) or not belongs_to_transaction(candidate):
            continue
        line = callback_line(candidate)
        if line:
            callback_occurrences.setdefault(line, []).append(candidate)

    rewritten = []
    delivered = []
    relocated_overlay_ids: set[int] = set()
    for event in events:
        if not isinstance(event, dict):
            rewritten.append(event)
            continue
        event_seq = event.get("_seq")
        sequenced = type(event_seq) is int
        if event.get("type") in {"game_started", "game_resumed"}:
            # Scoped transaction reads and the ordinary cursor are merged by
            # the client. A newly read ordinary batch can therefore contain a
            # lifecycle marker older than overlay rows already processed in a
            # preceding settle read. Do not let that stale marker erase the
            # occurrence receipts and replay cumulative history.
            if not sequenced or event_seq >= ctx.overlay.last_bridge_seq:
                _end_passive_overlay_generation(ctx)
                ctx.overlay.baseline()
                if sequenced:
                    ctx.overlay.last_bridge_seq = event_seq
            rewritten.append(event)
            continue
        if event.get("type") != "screen_content" or event.get("overlay_active"):
            rewritten.append(event)
            continue
        if sequenced:
            ctx.overlay.last_bridge_seq = max(
                ctx.overlay.last_bridge_seq, event_seq)
        has_rows = bool(_passive_overlay_rows(event))
        has_contributors = bool(_passive_overlay_rows_by_screen(event))
        has_durable_delta = isinstance(
            event.get("passive_overlay_delta"), list)
        if not event.get("passive_overlay_snapshot") and not has_rows:
            rewritten.append(event)
            continue
        if not has_rows and not has_contributors:
            if ctx.overlay.contributor_snapshots:
                _close_absent_overlay_contributors(ctx)
            elif not ctx.overlay.retain_generation:
                _end_passive_overlay_generation(ctx)
            continue
        if has_durable_delta:
            # The bridge computed this delta before retaining the cumulative
            # snapshot. Synchronize the local ledger from the full panel, but
            # render only its durable rows. This keeps one-shot CLI/MCP
            # processes from replaying history merely because their
            # HandlerContext started empty.
            fresh = _book_durable_passive_overlay_delta(
                ctx, event, authoritative_drain=True) or []
        else:
            fresh = _book_passive_overlay_snapshot(ctx, event)
        if not fresh:
            continue
        if not inline_with_story:
            _hold_passive_overlay_deliveries(ctx, fresh)
            continue
        for record in fresh:
            candidates = callback_occurrences.get(record["text"], [])
            callback = None
            while candidates:
                candidate = candidates[0]
                candidate_seq = candidate.get("_seq")
                record_seq = record.get("_bridge_seq")
                if (
                    type(candidate_seq) is int
                    and type(record_seq) is int
                    and candidate_seq > record_seq
                ):
                    # A later identical callback is a different occurrence;
                    # leave it available for the later screen row.
                    break
                candidates.pop(0)
                if "overlay_delivery_id" not in candidate:
                    callback = candidate
                    break
            if callback is not None:
                # The same visible occurrence can arrive through both the say
                # callback and a passive screen scrape. Keep the callback at
                # its original chronological position, but transfer the
                # overlay occurrence identity to it so the cumulative screen
                # channel does not print the row a second time.
                callback["overlay_delivery_id"] = record["id"]
                callback["passive_overlay_snapshot"] = True
                continue
            synthetic = {
                "type": "screen_text",
                "texts": [record["text"]],
                "overlay_delivery_ids": [record["id"]],
                "passive_overlay_snapshot": True,
            }
            if type(record.get("_bridge_seq")) is int:
                synthetic["_seq"] = record["_bridge_seq"]
            elif "_seq" in event:
                synthetic["_seq"] = event["_seq"]
            rewritten.append(synthetic)
            if (
                type(record.get("_bridge_seq")) is int
                and record.get("_bridge_seq") != event.get("_seq")
            ):
                relocated_overlay_ids.add(id(synthetic))
        for record in fresh:
            public_record = {
                key: value for key, value in record.items()
                if key not in _OVERLAY_LEDGER_RECORD_KEYS
            }
            public_record["channel"] = "story"
            delivered.append(public_record)
    # Whatever is still queued here was printed as a callback with no overlay
    # row of its own. A look-ahead row of the same occurrence is held behind
    # the cursor and would otherwise reappear as a fresh terminal line on the
    # next call.
    _remember_presented_callback_lines(ctx, callback_occurrences)
    sequenced_overlays = []
    greatest_prior_seq: int | None = None
    for event in rewritten:
        event_seq = event.get("_seq") if isinstance(event, dict) else None
        if (
            isinstance(event, dict)
            and event.get("passive_overlay_snapshot")
            and type(event_seq) is int
            and (
                id(event) in relocated_overlay_ids
                or (
                    greatest_prior_seq is not None
                    and greatest_prior_seq > event_seq
                )
            )
        ):
            sequenced_overlays.append(event)
        if type(event_seq) is int:
            greatest_prior_seq = max(
                event_seq,
                greatest_prior_seq if greatest_prior_seq is not None else event_seq,
            )
    if sequenced_overlays:
        sequenced_overlay_ids = {id(event) for event in sequenced_overlays}
        rewritten = [
            event for event in rewritten
            if id(event) not in sequenced_overlay_ids]
        for overlay in sorted(
            sequenced_overlays, key=lambda item: item["_seq"],
        ):
            insert_at = len(rewritten)
            for index, existing in enumerate(rewritten):
                existing_seq = (
                    existing.get("_seq") if isinstance(existing, dict) else None
                )
                if type(existing_seq) is int and existing_seq > overlay["_seq"]:
                    insert_at = index
                    break
            rewritten.insert(insert_at, overlay)
    events[:] = rewritten
    return delivered


_OVERLAY_CLOSE_UNKNOWN = object()


def _confirm_passive_overlay_closed(
    ctx: Any, *, deadline: float | None = None,
) -> dict | None | object:
    """Re-scrape once at a boundary that shows no overlay.

    Returns the live snapshot when the panel is still up, None when a usable
    empty snapshot confirms closure, or a sentinel when the check failed and
    the existing generation must be retained.
    """
    remaining = deadline - time.time() if deadline is not None else 2.2
    if remaining <= 0:
        return _OVERLAY_CLOSE_UNKNOWN
    time.sleep(min(_OVERLAY_CLOSE_CONFIRM_DELAY, remaining))
    remaining = deadline - time.time() if deadline is not None else 2.0
    if remaining <= 0:
        return _OVERLAY_CLOSE_UNKNOWN
    try:
        screen = _get_screen(ctx, timeout=min(2.0, remaining))
    except Exception:
        return _OVERLAY_CLOSE_UNKNOWN
    if not isinstance(screen, dict) or screen.get("overlay_active"):
        return _OVERLAY_CLOSE_UNKNOWN
    if _passive_overlay_rows(screen) or _passive_overlay_rows_by_screen(screen):
        return screen
    return None


def _merge_passive_overlay_text(
    ctx: Any,
    out: dict,
    screen: dict | None = None,
    fmt: str = "text",
    deadline: float | None = None,
    sample_live: bool = True,
    release_owner_fences: bool | None = None,
) -> None:
    """Merge passive overlay output under one occurrence-ledger owner."""
    with ctx.overlay.lock:
        _merge_passive_overlay_text_locked(
            ctx, out, screen=screen, fmt=fmt, deadline=deadline,
            sample_live=sample_live,
            release_owner_fences=release_owner_fences,
        )


def _merge_passive_overlay_text_locked(
    ctx: Any,
    out: dict,
    screen: dict | None = None,
    fmt: str = "text",
    deadline: float | None = None,
    sample_live: bool = True,
    release_owner_fences: bool | None = None,
) -> None:
    """Deliver registered-overlay scrollback text no other channel carries.

    The shim reports the text of every mod-registered overlay screen in
    ``overlay_texts``, passive (non-blocking) registrations included --
    "the text channel and the input-blocking flag are deliberately
    independent" (vnflight.rpy).  build_state_data() reads that field only
    inside its ``if overlay:`` branch, and the shim raises ``overlay_active``
    for BLOCKING overlays only.  So a passive scrollback panel reached the
    agent through no channel at all before passive snapshots became drainable:
    its rows are not say/NVL dialogue, and the post-act rescue in
    _refresh_missing_screen_text_from_state() only fires after act(), and only
    when the act produced no text of its own.

    Echoes of Tomorrow's live terminal is exactly that shape.  Agents were
    choosing which of ECHO-7's scarce questions to spend without ever being
    shown what ECHO-7 said -- a bare wait() at the terminal returned the stats
    line and the option list and nothing else, and a line written after the
    last act() settled only surfaced an interaction later, if at all.

    Delivery is based on ordered snapshot overlap rather than text membership
    (see _book_passive_overlay_snapshot).  This snapshot is the DECISION-POINT
    view; rows printed and hidden since the previous decision point were
    already booked from drainable screen_content events and are flushed in the
    order they were seen, so each row is delivered exactly once whether the
    panel is still up or closed before the agent got a turn.

    An absent ordinary overlay ends a generation only when a fresh re-scrape
    confirms it: a Journal reopened on identical rows must render. A retained
    overlay instead supplies an explicit generation key; its ledger survives
    a live/choice layer handoff, while a clear/reset changes the key.
    """
    if release_owner_fences is None:
        release_owner_fences = _ACTIVE_PRESENTATION_INVOCATION.get() is None
    if not ctx.allow_live_overlay_lookahead:
        # One-shot callers cannot preserve provisional ownership into their
        # next process. Durable screen_content events remain booked through
        # _book_drained_overlay_events and flush from the pending ledger below.
        screen = None
        sample_live = False
    if sample_live and deadline is not None and time.time() >= deadline:
        sample_live = False
    if screen is None and sample_live:
        remaining = deadline - time.time() if deadline is not None else 2.0
        if remaining <= 0:
            sample_live = False
        else:
            try:
                screen = _get_screen(ctx, timeout=min(2.0, remaining))
            except Exception:
                screen = None
    fresh: list[dict] = []
    if not isinstance(screen, dict):
        # No usable view of the screen: never guess at a close.  Rows already
        # booked from samples are still news and are flushed below.
        pass
    elif screen.get("overlay_active"):
        # Blocking overlays render through _screen_texts already; leave both
        # the panel and the ledger to that path.
        pass
    elif _passive_overlay_rows(screen) or _passive_overlay_rows_by_screen(screen):
        screen_seq = screen.get("_seq")
        client_cursor = getattr(ctx.client, "cursor", None)
        screen_source_id = str(screen.get("_source_id") or "")
        screen_source_seq = screen.get("_source_seq")
        source_position_unseen = bool(
            screen_source_id
            and type(screen_source_seq) is int
            and screen_source_seq > ctx.overlay.timeline_source_sequences.get(
                screen_source_id, 0)
        )
        presentation_fenced = (
            source_position_unseen
            or _passive_overlay_ahead_of_presentation(ctx, screen_seq)
        )
        already_drained = bool(
            type(screen_seq) is int
            and type(client_cursor) is int
            and screen_seq <= client_cursor
            and not source_position_unseen
        )
        durable = _book_durable_passive_overlay_delta(ctx, screen)
        if already_drained and not presentation_fenced:
            # /screen is a latest-state cache, not a second event channel. If
            # the ordinary cursor has already passed this exact snapshot, its
            # durable event was the only authority allowed to deliver rows.
            # Synchronize the local ledger/receipts above, but do not turn a
            # stale cached panel into provisional output after a later scene.
            if durable is None:
                _book_passive_overlay_snapshot(ctx, screen)
        elif durable is None:
            fresh = _book_passive_overlay_snapshot(
                ctx, screen, confirm_absent=True)
            _remember_provisional_overlay_deliveries(ctx, fresh, screen)
        else:
            fresh = durable
        # These rows came from latest-screen look-ahead, not the current
        # scoped/ordinary drain. Preserve that ownership distinction for both
        # modern durable deltas and legacy cumulative snapshots.
        _fence_latest_passive_overlay_deliveries(ctx, fresh, screen)
    elif ctx.overlay.text_snapshot and (
        ctx.overlay.contributor_snapshots
        or not ctx.overlay.retain_generation
    ):
        confirmed = _confirm_passive_overlay_closed(ctx, deadline=deadline)
        if confirmed is _OVERLAY_CLOSE_UNKNOWN:
            pass
        elif confirmed is None:
            if ctx.overlay.contributor_snapshots:
                _close_absent_overlay_contributors(ctx)
            else:
                _end_passive_overlay_generation(ctx)
        else:
            # The panel outlived the boundary snapshot — and it may have
            # printed its last rows in the gap.
            durable = _book_durable_passive_overlay_delta(ctx, confirmed)
            if durable is None:
                fresh = _book_passive_overlay_snapshot(ctx, confirmed)
                _remember_provisional_overlay_deliveries(
                    ctx, fresh, confirmed)
            else:
                fresh = durable
            _fence_latest_passive_overlay_deliveries(ctx, fresh, confirmed)

    pending = _claim_passive_overlay_deliveries(ctx)
    # Object identity, not the delivery id: durable re-bookings can reuse a
    # sequence-derived id, and only these exact records were held.
    held_records = {id(record) for record in pending}
    if pending:
        fresh = pending + fresh
    # A latest-screen read can observe a passive row while older narration is
    # still waiting on the ordinary transcript lane.  Book that occurrence
    # now, but do not present it ahead of the bridge cursor: its durable
    # screen_content event will either reconcile the provisional record or
    # the cursor will cross its sequence on a later wait.  Rows drained by the
    # current transaction are not provisional and remain immediately
    # renderable even though scoped reads do not advance the ordinary cursor.
    deferred = [
        record for record in fresh
        if (
            record.get("_cursor_fenced")
            and _passive_overlay_fence_still_active(
                ctx,
                record,
                release_owner_fences=release_owner_fences,
            )
        )
    ]
    if deferred:
        deferred_ids = {record["id"] for record in deferred}
        fresh = [
            record for record in fresh
            if record.get("id") not in deferred_ids
        ]
        _hold_passive_overlay_deliveries(ctx, deferred)
    if held_records and ctx.overlay.presented_callback_lines:
        # A row that was held while its own occurrence printed as a character
        # callback is now becoming visible a call later. Apply the same
        # mirrored-overlay matching the drain applies in-batch, so the line
        # does not read as a new terminal row. Only rows released from the
        # hold queue are eligible, and each printed callback is claimed once:
        # anything this cannot pair is presented, duplicate over drop.
        mirrored = set()
        for record in fresh:
            if id(record) not in held_records:
                continue
            if _claim_presented_callback_mirror(ctx, record):
                mirrored.add(id(record))
        if mirrored:
            fresh = [
                record for record in fresh if id(record) not in mirrored
            ]
    if release_owner_fences:
        # This merge is a presentation boundary (the serialized public flush,
        # or a direct one-shot caller). Age the mirror memory here, after the
        # matching above has had its chance to use it.
        _age_presented_callback_lines(ctx)
    # Incoming markers were protected until the fence decision. Normalize a
    # second time against final pending ownership so a large immediately
    # rendered batch cannot bypass the unowned-marker bound.
    _trim_provisional_overlay_ownership(ctx)
    for record in fresh:
        for key in _OVERLAY_LEDGER_RECORD_KEYS:
            record.pop(key, None)
    if fresh and all(type(record.get("_bridge_seq")) is int for record in fresh):
        # Scoped transaction reads and ordinary cursor reads can observe the
        # same durable stream in different batches. The bridge sequence, not
        # HTTP completion order, is the presentation chronology.
        fresh.sort(key=lambda record: record["_bridge_seq"])
    if not fresh:
        return

    if str(out.get("text") or "").strip() == "(no new events)":
        # The event formatter ran before this late overlay flush. Once booked
        # rows become visible, its empty-batch placeholder is no longer true.
        out.pop("text", None)

    existing_screen = out.get("screen_text")
    deliver = fresh
    rendered_rows = [record["text"] for record in deliver]
    if fmt == "json":
        prior = existing_screen if isinstance(existing_screen, list) else []
        if rendered_rows:
            out["screen_text"] = list(prior) + rendered_rows
    elif fmt in ("text", "quiet"):
        if rendered_rows:
            rendered = "\n".join(rendered_rows)
            if isinstance(existing_screen, str) and existing_screen.strip():
                out["screen_text"] = existing_screen.rstrip() + "\n" + rendered
            else:
                out["screen_text"] = rendered
    records = [{**record, "channel": "screen_text"} for record in deliver]
    _merge_overlay_delivery_records(out, records)
    if out.get(_STORY_RENDER_SECTIONS_KEY):
        # Promotion may already have established A -> B -> C chronology.
        # Rows discovered at this final decision-point merge must extend that
        # plan; otherwise the renderer trusts the stale plan and hides them.
        sections = _story_render_sections(out)
        additions = [{
            "channel": "screen_text",
            "text": record["text"],
            "delivery_ids": [record["id"]],
            **(
                {"_bridge_seq": record["_bridge_seq"]}
                if type(record.get("_bridge_seq")) is int
                else (
                    {"_bridge_seq": screen["_seq"]}
                    if (
                        isinstance(screen, dict)
                        and type(screen.get("_seq")) is int
                    ) else {}
                )
            ),
        } for record in deliver]
        out[_STORY_RENDER_SECTIONS_KEY] = (
            _merge_story_render_sections_by_bridge_sequence(
                sections, additions)
        )


def _claim_prefetched_screen_text_snapshot(
    ctx: Any,
    screen: dict,
    rendered_text: str,
) -> None:
    """Retire the durable event already represented by a screen fallback.

    A screen-level action can expose its updated modal through ``/screen``
    before the normal event drain is composed. The command-result poll may
    already have parked the matching ``screen_text`` event in prefetch. Once
    the latest-state fallback renders that same occurrence, leaving the event
    queued makes a later plain wait replay the modal in an unrelated scene.

    Claim only the event immediately emitted from this exact source snapshot;
    text equality alone is not ownership because identical terminal rows can
    be legitimate later occurrences.
    """
    source_id = screen.get("_source_id")
    source_seq = screen.get("_source_seq")
    bridge_seq = screen.get("_seq")
    rendered_rows = [
        line.strip() for line in str(rendered_text or "").splitlines()
        if line.strip()
    ]
    if not rendered_rows:
        return

    receipts = ctx.overlay.rendered_screen_event_receipts
    expected_rows = tuple(rendered_rows)
    if source_id and type(source_seq) is int:
        receipts[("source", str(source_id), source_seq + 1)] = expected_rows
    if type(bridge_seq) is int:
        receipts[("bridge", "", bridge_seq + 1)] = expected_rows
    while len(receipts) > 4096:
        receipts.pop(next(iter(receipts)))

    prefetched = list(getattr(ctx.client, "_prefetched_events", []) or [])
    if not prefetched:
        return

    def _is_ordered_subset(rows: list[str], whole: list[str]) -> bool:
        position = 0
        for row in rows:
            try:
                position = whole.index(row, position) + 1
            except ValueError:
                return False
        return True

    remaining = []
    claimed = False
    for event in prefetched:
        if not isinstance(event, dict):
            remaining.append(event)
            continue
        same_source_snapshot = (
            event.get("type") == "screen_text"
            and (
                (
                    source_id
                    and event.get("_source_id") == source_id
                    and type(source_seq) is int
                    and event.get("_source_seq") == source_seq + 1
                )
                or (
                    type(bridge_seq) is int
                    and event.get("_seq") == bridge_seq + 1
                )
            )
        )
        event_texts = event.get("texts")
        if not isinstance(event_texts, (list, tuple)):
            remaining.append(event)
            continue
        event_rows = [
            str(value).strip() for value in event_texts
            if str(value).strip()
        ]
        if (
            not claimed
            and same_source_snapshot
            and event_rows
            and _is_ordered_subset(event_rows, rendered_rows)
        ):
            claimed = True
            continue
        remaining.append(event)
    if claimed:
        ActionDeliveryOwnership._retain_held_events(ctx.client, remaining)


def _drop_rendered_screen_occurrence_events(
    ctx: Any,
    result: object,
) -> None:
    """Drop durable screen events already rendered from the latest cache."""
    events = getattr(result, "events", None)
    receipts = ctx.overlay.rendered_screen_event_receipts
    if not isinstance(events, list) or not receipts:
        return

    def was_rendered(event: object) -> bool:
        if not isinstance(event, dict) or event.get("type") != "screen_text":
            return False
        event_texts = event.get("texts")
        if not isinstance(event_texts, (list, tuple)):
            return False
        event_rows = [
            str(value).strip() for value in event_texts if str(value).strip()
        ]
        if not event_rows:
            return False

        def matches(receipt: tuple[str, str, int]) -> bool:
            expected = receipts.get(receipt)
            if not expected:
                return False
            position = 0
            for row in event_rows:
                try:
                    position = expected.index(row, position) + 1
                except ValueError:
                    return False
            return True

        source_id = event.get("_source_id")
        source_seq = event.get("_source_seq")
        if (
            source_id
            and type(source_seq) is int
            and matches(("source", str(source_id), source_seq))
        ):
            return True
        bridge_seq = event.get("_seq")
        return (
            type(bridge_seq) is int
            and matches(("bridge", "", bridge_seq))
        )

    events[:] = [event for event in events if not was_rendered(event)]


def _screen_has_choice_buttons(screen: dict | None) -> bool:
    """Return True when raw /screen buttons include story choice actions."""
    for button in (screen or {}).get("buttons") or []:
        actions = button.get("actions") or []
        action_strs = button.get("action_strs") or ""
        if "ChoiceReturn" in actions or "ChoiceReturn" in str(action_strs):
            return True
    return False
