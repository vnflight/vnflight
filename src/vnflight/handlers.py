"""Shared tool handlers for vnflight MCP servers.

This module contains the core logic for each game tool (wait, act,
screenshot, state, etc.).  Both the simple vnflight MCP server and the
vnharness agent MCP server use these handlers, extending behaviour
through hooks.

Usage::

    ctx = HandlerContext(client)
    ctx.hooks.format_screenshot = my_downscaler
    result = handle_tool(ctx, "wait", {"timeout": 60})
"""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .act_settle import (
    _ACT_POST_ACTION_MIN_WAIT_FLOOR,
    _ACT_SETTLE_CONTINUE,
    _ACT_PRE_GAMEPLAY_WAIT_SECONDS,
    _ACT_SETTLE_HANDBACK_POLL_CHUNK_SECONDS,
    _ACT_SETTLE_POLL_CHUNK_SECONDS,
    _ACT_SETTLE_QUIET_SECONDS,
    _ACT_SETTLE_STORY_HANDBACK,
    _ACT_STORY_HANDBACK_SECONDS,
    _STORY_ENTRY_LABELS,
    _ActSettleEvidence,
    _act_settle_deadline,
    _act_settle_positive_verdict,
    _act_settle_verdict,
    _mark_act_story_continues,
    _post_action_sample_is_after,
)
from .client import (
    actionable_state_snapshot,
    actionable_snapshots_equivalent,
    drain_stale_pending_request,
)
from .lib import (
    BRIDGE_READINESS_TIMEOUT_S,
    _load_config,
    _single_file_artifact,
    _validated_setting_application_receipt,
)
from .lifecycle import has_actionable_screen_buttons
from .presentation import (
    _join_story_text,
    render_sections,
    section_identities,
)
from .format import (
    build_wait_data,
    format_wait_text,
    format_event,
    build_state_data,
    format_state_text,
    format_pending_text,
    categorize_buttons,
    button_label_for_display_index,
    _button_is_disabled,
    _match_interaction,
    _resolve_label_interaction,
    _normalize_interaction_disabled,
    choice_target_to_action_label,
    pending_numbered_choice_count,
    numbered_button_count,
    _declared_modal_overlay_tags,
    modal_overlay_panel_name,
    _is_suppressed_pending_choice,
    _normalize_label,
    _raw_pending_labels,
)
from .overlay_presentation import (
    OverlayPresentationState,
    _ACTIVE_PRESENTATION_INVOCATION,
    _OVERLAY_DELIVERIES_KEY,
    _SCREEN_TEXT_BEFORE_STORY_KEY,
    _STORY_RENDER_SECTIONS_KEY,
    _book_drained_overlay_events,
    _carry_overlay_deliveries_into,
    _claim_prefetched_screen_text_snapshot,
    _drop_rendered_screen_occurrence_events,
    _game_state_proves_overlay_closed,
    _get_screen,
    _merge_overlay_delivery_records,
    _merge_passive_overlay_text,
    _merge_story_render_sections_by_bridge_sequence,
    _order_overlay_deliveries_by_sections,
    _overlay_delivery_records,
    _passive_overlay_delta,
    _passive_overlay_rows,
    _passive_overlay_rows_by_screen,
    _render_story_section_plan,
    _render_value_text,
    _reset_timeline_context,
    _screen_has_choice_buttons,
    _story_render_sections,
)
from .presentation_lane import (
    _preemptible_on_events,
    run_presentation_transition,
    run_public_tool,
)
from .settle import (
    button_labels,
    is_single_enter_screen,
    poll_preserving_events,
    rendered_state_signature,
    screen_signature,
    wait_for_stable_change,
)


_TRANSIENT_ACT_RECOVERY_WAIT_CAP = 10
_STORY_TRANSITION_DRAIN_HARD_CAP_SECONDS = 90.0
_STORY_TRANSITION_DRAIN_IDLE_SECONDS = 30.0
_STORY_TRANSITION_DRAIN_CHUNK_SECONDS = 8.0

# Transaction metadata is ADDITIVE (spec criterion 4): the wait formatter owns
# the agent-facing envelope.  Only these scalar identity fields are promoted to
# the top level; everything else stays namespaced under result["transaction"].
# `pending` is deliberately NOT here — the formatter's `pending` is the rendered
# "--- CHOICE REQUIRED ---" block and ~10 internal branches read it as the
# decision-point signal; the transaction's `pending` is a derived boolean and
# reaches callers as `transaction_pending` / `transaction["pending"]`.
_TRANSACTION_PROMOTED_KEYS = ("action_nonce", "action_id", "transaction_state")

# How long act(wait=True) peeks for the shim's resolution of an accepted
# transaction before falling back to a client-side prediction.  Short on
# purpose: the shim answers on its next command poll in the normal case, and
# the whole point of the transaction is that a slow apply must not block.
_ACT_APPLY_PROBE_SECONDS = 2.0
_ACT_APPLY_PROBE_POLL_SECONDS = 0.1

# Fields the bridge owns on a transaction record.  Everything else on a
# settled/applied record is the shim's resolution metadata, which the act
# result carries at the top level exactly as the legacy command_result did.
_TRANSACTION_BOOKKEEPING_KEYS = frozenset({
    "action_nonce", "action_id", "slot_id", "reset_generation",
    "current_reset_generation", "submitted_target", "transaction_state",
    "initial_request_id", "accepted_at", "applied_at", "settled_by",
    "pending", "ok", "success", "deduplicated", "ended", "reason",
    "blocking_action_id", "events", "settled_pending", "settled_screen",
    "delivery_cursor", "delivery_end", "drain_index", "revision",
    "compacted", "event_count", "events_truncated", "events_offloaded",
    "events_journalled", "events_reloaded", "offloaded", "last_event_at",
    "dispatched", "dispatched_at", "reserved_at",
    "_source_id", "_source_seq",
    # Admission signalling: "admission_open"/"released_by" are the documented
    # contract fields and belong on the transaction, not promoted into the act
    # result as if the shim had reported them.  The raw gate_* names are
    # bridge internals that the view already strips; they are listed too so a
    # mixed-version bridge can never leak them into an act result either.
    "admission_open", "released_by",
    "gate_released", "gate_released_by", "gate_released_at",
})

# Raw settle snapshots are diagnostics, not agent-facing text: they duplicate
# the whole pending request and the whole screen scrape into every act result.
_TRANSACTION_VIEW_DROPPED_KEYS = ("settled_pending", "settled_screen", "events")

# A formatted act result is not itself a transaction view. When an older or
# partial path omitted result["transaction"], recover only the lifecycle and
# resolution fields that are legitimately promoted to the result envelope.
# In particular, top-level ``pending`` is rendered choice text, not the
# transaction's boolean pending flag.
_TRANSACTION_RESULT_RECOVERY_KEYS = (
    "action_nonce", "action_id", "transaction_state", "reset_generation",
    "submitted_target", "resolved_as", "resolved_label", "label",
    "request_id", "error", "reason",
)


def _transaction_view(transaction: Any) -> dict | None:
    """Strip the noisy raw snapshots off a bridge transaction view."""
    if not isinstance(transaction, dict) or not transaction:
        return None
    return {
        key: value for key, value in transaction.items()
        if key not in _TRANSACTION_VIEW_DROPPED_KEYS
    }


def _apply_transaction_to_output(out: dict, transaction: dict | None) -> None:
    """Attach transaction metadata without overwriting formatted output."""
    if not transaction:
        return
    out["transaction"] = transaction
    for key in _TRANSACTION_PROMOTED_KEYS:
        if key in transaction:
            out[key] = transaction[key]
    if "pending" in transaction:
        out["transaction_pending"] = transaction["pending"]
    if transaction.get("transaction_state") in {"failed", "rejected"}:
        # The initial submission acknowledgement is optimistic.  A later
        # authoritative transaction view must not leave those booleans true.
        _normalize_terminal_act_result(out)


def _surface_scoped_wait_rejection(out: dict, transaction: dict | None) -> None:
    """Make an invalid explicit transaction wait impossible to miss."""
    if not transaction or transaction.get("reason") != "unknown_nonce":
        return
    nonce = transaction.get("action_nonce")
    suffix = ' "{}"'.format(nonce) if nonce else ""
    out["error"] = (
        "The provided nonce{} is not known as a current act transaction on "
        "this bridge slot. wait(action_nonce=...) accepts only a currently "
        "live act nonce. If this came from advance, back, rewind, or replay, "
        "it is a command nonce and cannot be passed here; call plain wait() "
        "to continue observing the game. If it came from act before a "
        "restart, expiry, or slot reset, that transaction state was lost."
    ).format(suffix)


def _normalize_terminal_act_result(result: dict) -> None:
    """Replace admission optimism once the transaction is terminal-failed."""
    state = result.get("transaction_state")
    if state not in {"failed", "rejected"}:
        return
    result["ok"] = False
    result["success"] = False
    # ``status`` can also contain formatted stats/inventory occurrences. Only
    # replace the admission-layer word, never a real delivered status line.
    admission_status = result.get("status")
    if (isinstance(admission_status, str)
            and admission_status in {"accepted", "submitted"}):
        result["status"] = state
    if result.get("message") == "Command 'act' submitted.":
        result["message"] = "Command 'act' {}.".format(state)
    if isinstance(result.get("pending"), bool):
        result["pending"] = False
    result["transaction_pending"] = False
    transaction = result.get("transaction")
    if isinstance(transaction, dict):
        transaction["pending"] = False


def _transaction_from_result(result: dict) -> dict:
    """Recover transaction metadata from a formatted act result."""
    transaction = {
        key: result[key]
        for key in _TRANSACTION_RESULT_RECOVERY_KEYS
        if key in result
    }
    pending = result.get("transaction_pending")
    if isinstance(pending, bool):
        transaction["pending"] = pending
    return transaction


# ---------------------------------------------------------------------------
# Client protocol — anything with BridgeClient-like methods
# ---------------------------------------------------------------------------

class GameClient(Protocol):
    """Minimal interface that both BridgeClient and Session satisfy."""

    def wait(self, timeout: int = 60, **kw) -> Any: ...
    def pending(self) -> dict | None: ...
    def act(self, target: int | str) -> dict: ...
    def input_text(
        self,
        text: str,
        *,
        deadline: float | None = None,
        request_id: str | None = None,
    ) -> dict: ...
    def screenshot(self) -> str | None: ...
    def state(self) -> dict: ...
    def transcript(self, last: int = 20) -> list[dict]: ...
    def command(self, name: str, **kwargs: Any) -> dict: ...
    def set_auto_advance(self, enabled: bool, delay: float | None = None) -> dict: ...
    def _get(self, path: str, timeout: float = 5.0) -> tuple[int, dict | None]: ...


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

@dataclass
class Hooks:
    """Extension points for tool handlers.

    Each hook is optional (None = no-op / use default).
    Hooks can be replaced or chained by the harness.
    """

    # act: transform target before matching (e.g. fuzzy match).
    # fn(target_str, choices) -> matched_index (1-based) or None
    match_choice: Callable[[str, list], int | None] | None = None

    # screenshot: transform image bytes (e.g. downscale).
    # fn(base64_png) -> base64_png
    format_screenshot: Callable[[str], str] | None = None

    # wait: called for each *batch* of events during wait (streaming)
    # AND once per idle main-loop iteration with an empty list (so
    # peek-only callbacks can interrupt even in quiet bridge states).
    # fn(batch: list[dict]) -> Any
    # Return a truthy value to ask wait() to return early with
    # ``WaitResult.interrupted=True``; return None / falsy to continue.
    # The harness uses the truthy-return to interrupt a long wait when
    # an operator message lands; pure stream-to-hub callbacks just
    # forward non-empty batches and return None on the empty tick.
    on_event: Callable[[list[dict]], Any] | None = None

    # wait: post-process the structured wait data before formatting.
    # fn(data, result) -> data
    after_wait: Callable[[dict, Any], dict] | None = None

    # act: post-process after action is taken.
    # fn(action_type, target, result) -> result
    after_act: Callable[[str, str, dict], dict] | None = None

    # mutating gameplay action: called immediately before the handler
    # forwards an action to the bridge/shim. Harnesses can use this as a
    # gateway to pace visible stream UI before the game state changes.
    # fn(tool_name, payload) -> None
    before_action: Callable[[str, dict], None] | None = None

    # state: post-process structured state data.
    # fn(data) -> data
    after_state: Callable[[dict], dict] | None = None

    # save/load: post-process after save or load command.
    # fn(command_name, result) -> result
    after_command: Callable[[str, dict], dict] | None = None

    # lifecycle: run a vnflight CLI command.
    # fn(*args, timeout) -> {"ok": True, "output": str} or {"error": str}
    run_cli: Callable[..., dict] | None = None

    # MCP: lazily attach the shared client before a live-game call. This runs
    # inside the presentation-call lane so it cannot rebind an active call.
    ensure_attached: Callable[[], None] | None = None


# ---------------------------------------------------------------------------
# Handler context
# ---------------------------------------------------------------------------

@dataclass
class HandlerContext:
    """Holds the client and hooks for a tool handler session."""

    client: Any  # GameClient
    hooks: Hooks = field(default_factory=Hooks)
    # Default for act(wait=...): when True, act() automatically
    # follows up with wait() and merges the results.
    act_wait: bool = True
    # Anomaly visibility for agent-facing renders: "off", "errors"
    # (game failures only, the default) or "all". Set by set_format.
    anomaly_visibility: str = "errors"
    # Every occurrence ledger this session owns: booked overlay panel,
    # pending/durable/provisional delivery records, callback mirrors and the
    # presentation receipts that suppress a replay. They are cleared together
    # through the state object's reset()/baseline(), never field by field.
    overlay: OverlayPresentationState = field(
        default_factory=OverlayPresentationState, repr=False)
    # Public calls that can consume or expose story state are serialized per
    # game before their handlers run. A later call therefore cannot consume
    # bridge rows while an earlier response is still being composed.
    _presentation_call_lock: Any = field(
        default_factory=threading.Lock, repr=False)
    # Diagnostic lease for the serialized presentation lane. The lock remains
    # fail-closed; if a handler wedges, overlapping callers receive the owner
    # and age instead of an opaque refusal that looks identical forever.
    _presentation_call_name: str | None = field(default=None, repr=False)
    _presentation_call_started_at: float = field(default=0.0, repr=False)
    _presentation_call_watchdog_seconds: float = field(
        default=305.0, repr=False)
    _presentation_call_watchdog_at: float = field(default=0.0, repr=False)
    # Lifecycle tools (stop/launch/bridge_connect) must stay reachable while
    # the lane is wedged, otherwise an agent whose `wait timeout=300` is stuck
    # on a dead game has no escape short of killing the MCP process. Setting
    # this asks the live observation to abandon itself at its next poll; the
    # incoming lifecycle call clears it once it owns the lane.
    _presentation_lane_preempt: Any = field(
        default_factory=threading.Event, repr=False)
    # How long a lifecycle tool waits for the interrupted owner to hand the
    # lane back before it proceeds anyway. A wait poll is capped at 5s, so the
    # default leaves one full poll plus scheduling margin.
    _presentation_preempt_wait_seconds: float = field(
        default=8.0, repr=False)
    # Bumped whenever a lifecycle call proceeds without the lane. A handler
    # that was composing across that bump is describing a game that is gone:
    # its rows must never be released and its ledger writes are discarded.
    _presentation_lane_epoch: int = field(default=0, repr=False)
    # A process-local CLI context cannot carry provisional latest-screen rows
    # into the next command. It disables look-ahead and relies on durable
    # events; persistent MCP contexts keep the default.
    allow_live_overlay_lookahead: bool = field(default=True, repr=False)
    # Non-zero while handle_act composes. Its internal settle waits render
    # candidate output that may never reach the caller, so they must not
    # rewrite the numeric binding; the composed act response is bound once,
    # by _bind_returned_pending. See _remember_rendered_decision.
    _composing_act_response: int = field(default=0, repr=False)
# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce a tool param to bool, tolerating string forms.

    Boolean MCP/JSON params sometimes arrive as strings ("false", "0").
    A bare truthiness test treats any non-empty string as True, which
    silently flips ``wait:false`` back into waiting — so normalize the
    common falsy spellings explicitly.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _reconcile_applied_timeline_jump(
    ctx: HandlerContext, *, detach_slot: bool = False,
) -> None:
    """Perform no-I/O bookkeeping after an applied load or launch."""
    _reset_timeline_context(
        ctx,
        clear_client_prefetch=True,
        # Same-slot loads may complete before /state exposes the increment.
        # Retain generation-qualified ownership until that authoritative read
        # prunes it; detached/rebound slots share no occurrence identity.
        clear_client_action_delivery=detach_slot,
    )
    ctx.overlay.timeline_source_sequences = {}
    client = ctx.client
    client.cursor = 0
    client.last_request_type = None
    client._last_poll_pending = None
    if detach_slot:
        client.slot_prefix = ""
        client._token_refresh_failed_at = 0.0


def _latest_fresh_timeline_boundary(
    ctx: HandlerContext, events: list[dict] | None,
) -> dict | None:
    """Return the newest lifecycle marker not yet seen by the handler."""
    latest = None
    for event in events or []:
        if not isinstance(event, dict) or event.get("type") not in {
            "game_started", "game_resumed",
        }:
            continue
        source_id = str(event.get("_source_id") or "legacy")
        source_seq = event.get("_source_seq")
        if type(source_seq) is not int:
            source_seq = event.get("_seq")
        if (
            type(source_seq) is int
            and source_seq > ctx.overlay.timeline_source_sequences.get(source_id, 0)
        ):
            latest = event
    return latest


def _timeline_boundary_action_owners(
    ctx: HandlerContext,
    events: list[dict] | None,
    *,
    fallback_action_nonce: str | None = None,
    fallback_action_id: object = None,
) -> tuple[str | None, set[str], object]:
    """Return every active action not proven older than a fresh boundary."""
    latest_boundary = _latest_fresh_timeline_boundary(ctx, events)
    if latest_boundary is None:
        return None, set(), None
    boundary_action_id = latest_boundary.get("action_id")
    try:
        normalized_boundary_id = int(boundary_action_id or 0)
    except (TypeError, ValueError):
        normalized_boundary_id = 0

    client = ctx.client
    action_nonces_for_boundary = getattr(
        client, "action_nonces_for_boundary", None)
    if callable(action_nonces_for_boundary) and normalized_boundary_id > 0:
        preserve_action_nonces = set(
            action_nonces_for_boundary(normalized_boundary_id))
    elif normalized_boundary_id <= 0:
        # Without a monotonic action id there is no proof that any unresolved
        # action belongs to the abandoned timeline. Keep all of them and let a
        # later authoritative receipt establish ownership.
        preserve_action_nonces = set(
            getattr(client, "_active_action_nonces", ()) or ())
    else:
        preserve_action_nonces = set()
        try:
            normalized_fallback_id = int(fallback_action_id or 0)
        except (TypeError, ValueError):
            normalized_fallback_id = 0
        if (
            fallback_action_nonce is not None
            and normalized_fallback_id == normalized_boundary_id
        ):
            preserve_action_nonces.add(fallback_action_nonce)

    mark_boundary_floor = getattr(client, "mark_action_boundary_floor", None)
    if callable(mark_boundary_floor) and normalized_boundary_id > 0:
        mark_boundary_floor(preserve_action_nonces, normalized_boundary_id)
    preserve_action_nonce = (
        next(iter(preserve_action_nonces))
        if len(preserve_action_nonces) == 1 else None
    )
    return (
        preserve_action_nonce,
        preserve_action_nonces,
        normalized_boundary_id if normalized_boundary_id > 0 else None,
    )


def _observe_timeline_boundaries(
    ctx: HandlerContext,
    events: list[dict] | None,
    *,
    preserve_action_nonce: str | None = None,
    preserve_action_nonces: set[str] | None = None,
    preserve_action_id: object = None,
) -> None:
    """Reset timeline-local ledgers once for each fresh lifecycle marker."""
    latest_boundary = _latest_fresh_timeline_boundary(ctx, events)
    for event in events or []:
        if not isinstance(event, dict):
            continue
        source_id = str(event.get("_source_id") or "legacy")
        source_seq = event.get("_source_seq")
        if type(source_seq) is not int:
            source_seq = event.get("_seq")
        if type(source_seq) is not int:
            continue
        previous = ctx.overlay.timeline_source_sequences.get(source_id, 0)
        is_fresh = source_seq > previous
        if is_fresh:
            ctx.overlay.timeline_source_sequences[source_id] = source_seq
    if latest_boundary is None:
        return
    try:
        event_action_id = int(latest_boundary.get("action_id", 0) or 0)
        owner_action_id = int(preserve_action_id or 0)
    except (TypeError, ValueError):
        event_action_id = 0
        owner_action_id = 0
    owner_matches = (
        event_action_id == owner_action_id
        if event_action_id > 0 else preserve_action_id is None
    )
    boundary_owner_nonce = preserve_action_nonce if owner_matches else None
    boundary_owner_nonces = (
        set(preserve_action_nonces or ()) if owner_matches else set()
    )
    # Production client paths have already observed this lifecycle boundary
    # before filtering and recording the returned rows. Preserve that fresh
    # occurrence ownership while resetting abandoned presentation state.
    _reset_timeline_context(
        ctx,
        clear_client_prefetch=True,
        clear_client_action_delivery=False,
        preserve_client_action_nonce=boundary_owner_nonce,
        preserve_client_action_nonces=boundary_owner_nonces,
    )


def _call_with_timeout(method: Callable, timeout: float):
    """Pass a bounded timeout when the client method supports that contract."""
    try:
        parameters = inspect.signature(method).parameters.values()
        supports_timeout = any(
            parameter.name == "timeout"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_timeout = True
    if supports_timeout:
        return method(timeout=max(0.0, timeout))
    return method()


def _default_match_choice(target: str, choices: list) -> int | None:
    """Case-insensitive match against label or ID. Returns 1-based index or None."""
    norm = _normalize_label(target)
    # First pass: exact label match.
    for i, c in enumerate(choices, 1):
        label = c if isinstance(c, str) else c.get("label", c.get("caption", ""))
        if _normalize_label(label) == norm:
            return i
    # Second pass: match by choice ID (e.g. attitude IDs like "friendly").
    for i, c in enumerate(choices, 1):
        if isinstance(c, dict):
            cid = c.get("id", "")
            if cid and _normalize_label(cid) == norm:
                return i
    return None


def _choice_candidate_key(item: Any) -> tuple[str, str, str]:
    if isinstance(item, dict):
        label = str(item.get("label") or item.get("caption") or "")
        annotation = str(item.get("annotation") or "")
        choice_id = str(item.get("id") or "")
        return label, annotation, choice_id
    return str(item), "", ""


def _add_choice_candidate(items: list, seen: dict, item: Any) -> None:
    key = _choice_candidate_key(item)
    if not key[0]:
        return
    if key in seen:
        return
    has_alias_data = bool(key[1] or key[2])
    plain_key = (key[0], "", "")
    if has_alias_data and plain_key in seen:
        idx = seen.pop(plain_key)
        items[idx] = item
        seen[key] = idx
        return
    if not has_alias_data:
        for existing in seen:
            if existing[0] == key[0] and (existing[1] or existing[2]):
                return
    items.append(item)
    seen[key] = len(items) - 1


def _pending_choice_items(state: dict) -> list:
    pending = (state or {}).get("pending_request") or {}
    items = []
    seen = {}
    for item in pending.get("full_items") or []:
        if not isinstance(item, dict):
            continue
        if _is_suppressed_pending_choice(item):
            continue
        if item.get("is_caption") or item.get("is_disabled"):
            continue
        label = item.get("label")
        if label:
            _add_choice_candidate(items, seen, item)
    for choice in pending.get("choices") or []:
        if _is_suppressed_pending_choice(choice):
            continue
        _add_choice_candidate(items, seen, choice)
    try:
        rendered_pending = build_state_data(state).get("pending") or {}
        for choice in rendered_pending.get("choices") or []:
            if isinstance(choice, dict) and (
                    choice.get("is_caption")
                    or choice.get("is_disabled")
                    or _is_suppressed_pending_choice(choice)):
                continue
            _add_choice_candidate(items, seen, choice)
    except Exception:
        pass
    for itr in ((state or {}).get("game_state") or {}).get("interactions", []):
        if not isinstance(itr, dict) or itr.get("type") != "choice":
            continue
        label = itr.get("display_label") or itr.get("label")
        if not label:
            continue
        candidate = {"label": label}
        for key in ("annotation", "id", "index"):
            if itr.get(key) is not None:
                candidate[key] = itr[key]
        _add_choice_candidate(items, seen, candidate)
    return items


def _button_labels(screen: dict | None) -> list[str]:
    return button_labels(screen)


def _is_single_enter_screen(screen: dict | None) -> bool:
    return is_single_enter_screen(screen)


def _refresh_single_enter_screen(
    ctx: HandlerContext,
    screen: dict | None,
    gs: dict | None,
    *,
    deadline: float | None = None,
) -> tuple[dict | None, dict | None]:
    """Replace transient one-button Enter screens with fresher menu state."""
    if not _is_single_enter_screen(screen):
        return screen, gs

    import time as _t
    stop_at = _t.time() + 2.0
    if isinstance(deadline, (int, float)):
        stop_at = min(stop_at, float(deadline))
    while True:
        remaining = max(0.0, stop_at - _t.time())
        if remaining <= 0:
            return screen, gs
        fresh_gs = gs
        fresh_screen = screen
        try:
            fresh_gs = _call_with_timeout(
                ctx.client.game_state, min(2.0, remaining))
        except Exception:
            pass
        try:
            remaining = max(0.0, stop_at - _t.time())
            fetched = _get_screen(ctx, timeout=min(2.0, remaining)) \
                if remaining > 0 else None
            if fetched:
                fresh_screen = fetched
        except Exception:
            pass

        gs_buttons = (fresh_gs or {}).get("screen_buttons") or []
        gs_labels = [str(b.get("label", "")).strip() for b in gs_buttons if str(b.get("label", "")).strip()]
        screen_labels = _button_labels(fresh_screen)
        if len(gs_labels) > 1 or (screen_labels and screen_labels != ["Enter"]):
            return fresh_screen, fresh_gs

        if _t.time() >= stop_at:
            return fresh_screen, fresh_gs
        _t.sleep(min(0.2, max(0.0, stop_at - _t.time())))


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _latest_pending_request_event(events: list[dict] | None) -> dict | None:
    """Return the newest actionable request carried by a scoped drain.

    A transaction may settle before its successor request is captured in
    ``settled_pending``. The request still arrives in the transaction's
    ordered event stream, including its durable id. Retaining the older
    ``WaitResult.pending`` while enriching it with the successor's live labels
    creates a split-brain menu: visually new, but actionable as the old id.
    """
    for event in reversed(events or []):
        if not isinstance(event, dict):
            continue
        if event.get("type") not in {
            "choice_request", "choices", "input_request",
        }:
            continue
        if event.get("id"):
            return event
    return None


def _remember_delivered_actionable_screen(
    ctx: HandlerContext,
    rendered: dict,
    screen: dict | None,
    *,
    record_presentation: bool = True,
) -> None:
    """Cache only a raw screen decision that this response actually exposed."""
    if not isinstance(screen, dict):
        if record_presentation:
            ctx.overlay.ordinary_screen_presentation_receipt = None
        return
    if record_presentation:
        _record_ordinary_screen_presentation(ctx, rendered, screen)
    if (
        not has_actionable_screen_buttons(screen)
        or not _has_actionable_rendered_state(rendered)
    ):
        invalidate = getattr(
            ctx.client, "_invalidate_delivered_screen_hint", None,
        )
        if callable(invalidate):
            invalidate()
        else:
            ctx.client._last_delivered_actionable_screen = None
            ctx.client._last_delivered_actionable_screen_signature = ()
        return
    ctx.client._last_delivered_actionable_screen = dict(screen)
    ctx.client._last_delivered_actionable_screen_signature = screen_signature(
        screen,
    )


def _ordinary_screen_presentation_signature(screen: dict | None) -> tuple | None:
    """Stable contributor identity plus the full visible ordinary-screen body."""
    if not isinstance(screen, dict):
        return None
    rows = tuple(
        str(text).strip()
        for text in (screen.get("texts") or [])
        if str(text).strip()
    )
    return (
        tuple(sorted(str(tag) for tag in (screen.get("screens") or []))),
        tuple(sorted(
            str(tag)
            for tag in (
                list(screen.get("modal_screens") or [])
                # A declared-modal overlay owns the surface even when the
                # game never marked its screen Ren'Py-modal, so opening or
                # closing one must retire the previous presentation receipt.
                + list(screen.get("modal_overlay_screens") or [])
            )
        )),
        "\n".join(rows),
    )


def _rendered_screen_text_blob(rendered: dict) -> str:
    """Normalize a rendered screen_text channel for full-body comparison."""
    value = rendered.get("screen_text")
    if isinstance(value, list):
        return "\n".join(str(row).strip() for row in value if str(row).strip())
    if isinstance(value, str):
        return value.strip()
    return ""


def _record_ordinary_screen_presentation(
    ctx: HandlerContext,
    rendered: dict,
    screen: dict,
) -> None:
    """Receipt a screen body only when this response actually rendered it."""
    signature = _ordinary_screen_presentation_signature(screen)
    previous = ctx.overlay.ordinary_screen_presentation_receipt
    if (
        signature is not None
        and previous is not None
        and signature[:2] != previous[:2]
    ):
        # Crossing to a different screen owner retires the old presentation,
        # even when this response exposed only the new screen's choices.
        ctx.overlay.ordinary_screen_presentation_receipt = None
    if (
        signature is not None
        and signature[2]
        and _rendered_screen_text_blob(rendered) == signature[2]
    ):
        ctx.overlay.ordinary_screen_presentation_receipt = signature


def _own_terminal_presentation_once(
    ctx: HandlerContext,
    rendered: dict,
    params: dict,
) -> None:
    """Suppress a replayed title boundary on repeated waits for one action."""
    if (
        params.get("action_nonce") is None
        or not rendered.get("ended")
        or not rendered.get("_title_boundary")
    ):
        return
    transaction = rendered.get("transaction") or {}
    nonce = str(
        transaction.get("action_nonce") or params.get("action_nonce") or ""
    )
    try:
        generation = int(transaction.get("reset_generation") or 0)
    except (TypeError, ValueError):
        generation = 0
    receipt = (generation, nonce)
    if receipt not in ctx.overlay.terminal_presentation_receipts:
        ctx.overlay.terminal_presentation_receipts.add(receipt)
        return

    # The title screen is persistent state, not a second ending occurrence.
    # Keep terminal lifecycle metadata private while removing its repeated
    # player-facing navigation and banner from this scoped recovery response.
    rendered["_defer_ended_banner"] = True
    rendered["_terminal_presentation_replayed"] = True
    for key in ("pending", "buttons"):
        rendered.pop(key, None)
    data = rendered.get("_data")
    if isinstance(data, dict):
        data.pop("pending", None)
        data.pop("buttons", None)


def _ensure_empty_scoped_wait_receipt(rendered: dict, params: dict) -> None:
    """Make every empty explicit-nonce receipt honest and actionable."""
    transaction = rendered.get("transaction")
    if not isinstance(transaction, dict):
        transaction = _transaction_from_result(rendered)
    if (
        params.get("action_nonce") is None
        or rendered.get("error")
    ):
        return
    state = transaction.get("transaction_state")
    if state in {"failed", "rejected"}:
        reason = transaction.get("error") or transaction.get("reason")
        suffix = ": {}".format(reason) if reason else "."
        rendered["error"] = "Transaction {}{}".format(state, suffix)
        rendered.pop("warning", None)
        return
    if rendered.get("warning"):
        return
    visible = bool(
        _rendered_has_story_text(rendered)
        or _has_actionable_rendered_state(rendered)
        or rendered.get("status")
        or rendered.get("brief")
    )
    if visible or (rendered.get("ended") and not rendered.get("_defer_ended_banner")):
        return
    if str(rendered.get("text") or "").strip() == "(no new events)":
        rendered.pop("text", None)
    if state == "settled":
        rendered["brief"] = (
            "Transaction settled; no additional transaction output. "
            "Call wait() for the current scene or decision."
        )
        return
    if transaction.get("admission_open"):
        rendered["brief"] = (
            "The prior transaction no longer blocks new actions and has no "
            "additional output. Call wait() for the current scene or decision."
        )
        return
    nonce = transaction.get("action_nonce") or params.get("action_nonce")
    rendered["warning"] = (
        "The transaction is still settling; no additional output is "
        "available yet. Call wait(action_nonce=\"{}\") again to continue "
        "it."
    ).format(nonce)


# Shown when a chronology/ownership fence withheld a decision this wait would
# otherwise have presented.  Without it the agent sees story, no menu, and no
# reason -- indistinguishable from the game simply not being at a choice.
_WITHHELD_DECISION_HINT = (
    "Earlier story is still arriving; the current menu is withheld until it "
    "is shown. Call wait() again."
)


def _ensure_withheld_decision_hint(rendered: dict) -> None:
    """Explain a decision this wait deliberately did not present.

    Reuses the single agent-facing ``warning`` field (rendered ahead of story
    by render_tool_result_text). A real error or a more specific warning
    already set by the scoped-receipt/recovery passes always wins.
    """
    if rendered.get("error") or rendered.get("warning"):
        return
    rendered["warning"] = _WITHHELD_DECISION_HINT


def _handback_story_event_is_displayable(event: dict) -> bool:
    """Does this row carry story a reader would see?

    ``screen_content`` is emitted for every re-render, including button-only
    frames with no text at all -- those are UI churn, not a story still
    arriving, and counting them would let an idle panel hold the hand-back
    clock alive.  Rows that DO carry text are exactly the NVL/terminal lines
    that make up most of Echoes' bursts, which is why the plain
    narration/dialogue pair was too narrow.
    """
    event_type = event.get("type")
    if event_type not in ("screen_text", "screen_content"):
        return True
    texts = event.get("texts")
    if isinstance(texts, (list, tuple)):
        return any(str(text or "").strip() for text in texts)
    return bool(str(event.get("text") or "").strip())


class _WaitStoryHandback:
    """Bound a plain wait() the way the settle policy bounds an act.

    ``act()`` hands a still-running story back after
    ``_ACT_STORY_HANDBACK_SECONDS`` rather than holding the tool call open for
    a scene with no end in sight.  ``wait()`` had no such rule, so once the
    act settle stopped absorbing long openings the block simply moved: fleet
    R62 recorded 41 of 150 waits in a 55-61 s cluster (R61: 0 of 114), most
    returning no menu and forcing the agent to wait again.

    It rides ``BridgeClient.wait``'s existing caller-driven early exit -- a
    truthy ``on_events`` return, checked on the idle tick too -- so it invents
    no second abort path and leaves min_wait, the settle re-fetch and the
    foreign-action boundary exactly as they were.  Nothing is consumed early:
    what the wait rendered is returned, and the caller's next wait() continues
    from the same cursor.
    """

    _STORY_EVENT_TYPES = (
        "narration", "dialogue", "auto_skipped", "screen_text",
        "screen_content",
    )
    _DECISION_EVENT_TYPES = (
        "choice_request", "input_request", "game_ended",
    )
    # The act that OWNS this drain resolved the request itself, so its own
    # choice_resolved is a receipt, not a new decision.  On a plain wait the
    # same row IS a boundary and still disarms.
    _RESOLUTION_EVENT_TYPES = ("choice_resolved", "input_resolved")

    def __init__(
        self,
        inner,
        *,
        seconds: float,
        armed: bool,
        since: float | None = None,
        resolution_ends_wait: bool = True,
    ) -> None:
        self._inner = inner
        self._seconds = seconds
        self._armed = armed
        self._resolution_ends_wait = bool(resolution_ends_wait)
        # The bound is wall time from the call's issue, not "N seconds of
        # story after the first line": anchoring it to the first line made
        # the observed hand-back the sum of the receipt's own settle time and
        # the constant (fleet R63: median 32 s against 20).  A caller that
        # cannot name its issue time falls back to the old anchor.
        self._first_story_at = since
        self._last_story_at = None
        self.fired = False

    def __call__(self, batch):
        if self._inner is not None:
            inner_verdict = self._inner(batch)
            if inner_verdict:
                return inner_verdict
        if not self._armed:
            return None
        now = time.time()
        for event in batch or ():
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type in self._DECISION_EVENT_TYPES or (
                self._resolution_ends_wait
                and event_type in self._RESOLUTION_EVENT_TYPES
            ):
                # A decision or an ending ends the wait on its own terms.
                self._armed = False
                return None
            if event_type in self._STORY_EVENT_TYPES:
                if not _handback_story_event_is_displayable(event):
                    continue
                if self._first_story_at is None:
                    self._first_story_at = now
                self._last_story_at = now
        if self._first_story_at is None:
            return None
        if self._last_story_at is None:
            # Anchored to the issue time, so the clock can be past due before
            # a single line has arrived.  Silence is not a story to hand back.
            return None
        if now - self._first_story_at < self._seconds:
            return None
        if now - (self._last_story_at or now) > _ACT_SETTLE_QUIET_SECONDS:
            # The burst ended; let the wait finish on its own boundary.
            return None
        self.fired = True
        return True


def handle_wait(ctx: HandlerContext, params: dict) -> dict:
    """Wait for story events and/or a decision point."""
    wait_started_at = time.time()
    timeout = params.get("timeout", 60)
    if timeout is None:
        timeout = 60
    result_deadline = params.get("_result_deadline")
    if isinstance(result_deadline, (int, float)):
        timeout = min(
            max(0.0, float(timeout)),
            max(0.0, float(result_deadline) - time.time()),
        )
    caller_deadline = (
        float(result_deadline)
        if isinstance(result_deadline, (int, float))
        else wait_started_at + max(0.0, float(timeout))
    )
    on_events = _preemptible_on_events(
        ctx, params.get("_on_events") or ctx.hooks.on_event)
    # A plain, caller-owned wait long enough to outlast the handback window
    # gets the same rule act() has.  The act's internal settle waits run in
    # chunks far shorter than the window and are governed by the settle
    # policy's own handback, so this never arms for them.
    #
    # The ACT's own scoped receipt drain is the exception, and it is the
    # reason fleet R63 still had 56 acts at the 60 s deadline: a choice
    # resolution's receipt only settles on a successor request, a game end,
    # or 15 s of attributed silence (bridge
    # _ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE), so an unbroken burst
    # kept _wait_action_transaction polling for the WHOLE act budget and the
    # settle observer -- built after it returns -- never ran at all.  The act
    # asks for the hand-back explicitly; nothing else scoped gets it.
    _act_handback = bool(params.get("_act_story_handback"))
    _handback_since = params.get("_handback_since")
    if not isinstance(_handback_since, (int, float)):
        _handback_since = wait_started_at
    _handback_since = float(_handback_since)
    try:
        # Does the due time fall inside THIS call?  With the clock anchored to
        # the issue time that is the honest test; for a plain wait (anchor ==
        # its own start) it is the old "timeout > window".
        _handback_fits = (
            _handback_since + _ACT_STORY_HANDBACK_SECONDS
            < wait_started_at + float(timeout)
        )
    except (TypeError, ValueError):
        _handback_fits = False
    story_handback = _WaitStoryHandback(
        on_events,
        seconds=_ACT_STORY_HANDBACK_SECONDS,
        armed=(
            (params.get("action_nonce") is None or _act_handback)
            and not params.get("_ordinary_only")
            and _handback_fits
        ),
        since=_handback_since,
        resolution_ends_wait=not _act_handback,
    )
    min_wait = params.get("_min_wait", 0)
    wait_kwargs = {
        "timeout": timeout,
        "on_events": story_handback,
        "min_wait": min_wait,
    }
    if params.get("action_nonce") is not None:
        wait_kwargs["action_nonce"] = params["action_nonce"]
        if params.get("_pre_action_drain") or params.get("_act_story_handback"):
            wait_kwargs["include_unowned_prefetch"] = True
        if params.get("_return_on_admission"):
            # Act path only (see _settle_wait_after_action): stop waiting on a
            # transaction the bridge has already agreed to admit past.  Never
            # set for a plain wait — late story must stay drainable there.
            wait_kwargs["return_on_admission"] = True
    if params.get("_ordinary_only"):
        wait_kwargs["ordinary_only"] = True
        if params.get("_ordinary_action_id") is not None:
            wait_kwargs["ordinary_action_id"] = params["_ordinary_action_id"]
    result = ctx.client.wait(**wait_kwargs)
    raw_wait_ended = bool(getattr(result, "ended", False))
    foreign_action_boundary = bool(
        getattr(result, "foreign_action_boundary", False))

    # The wait timeout governs bridge observation. Keep all subsequent state
    # composition reads under one additional, bounded budget instead of
    # allowing several independent two-second reads to stack under load.
    composition_deadline = result_deadline
    if not isinstance(composition_deadline, (int, float)):
        composition_deadline = time.time() + 2.0

    boundary_transaction = getattr(result, "transaction", None)
    boundary_action_nonce = (
        boundary_transaction.get("action_nonce")
        if isinstance(boundary_transaction, dict) else None
    )
    boundary_action_id = (
        boundary_transaction.get("action_id")
        if isinstance(boundary_transaction, dict) else None
    )
    prefix_action_id = params.get("_ordinary_action_id")
    if (
        prefix_action_id is None
        and params.get("action_nonce") is not None
    ):
        prefix_action_id = boundary_action_id
    (
        preserve_action_nonce,
        preserve_action_nonces,
        preserve_action_id,
    ) = _timeline_boundary_action_owners(
        ctx,
        getattr(result, "events", None),
        fallback_action_nonce=boundary_action_nonce,
        fallback_action_id=boundary_action_id,
    )
    _observe_timeline_boundaries(
        ctx,
        getattr(result, "events", None),
        preserve_action_nonce=preserve_action_nonce,
        preserve_action_nonces=preserve_action_nonces,
        preserve_action_id=preserve_action_id,
    )
    _drop_rendered_screen_occurrence_events(ctx, result)
    _drop_already_rescued_transcript_events(ctx, result)
    if not foreign_action_boundary:
        foreign_action_boundary = bool(
            _merge_late_transcript_events(
                ctx,
                result,
                deadline=composition_deadline,
                ordinary_action_id=prefix_action_id,
                action_scoped=bool(
                    params.get("action_nonce") is not None
                    or params.get("_ordinary_only")
                ),
            )
        )
    delivered_overlays = _book_drained_overlay_events(ctx, result)
    event_pending = _latest_pending_request_event(result.events)
    effective_pending = (
        None if foreign_action_boundary else event_pending or result.pending
    )
    # Only a fence that actually removed a decision owes the agent an
    # explanation; a boundary with nothing pending changed nothing visible.
    withheld_decision = bool(
        foreign_action_boundary and (event_pending or result.pending))
    data = build_wait_data(result.events, effective_pending, result.ended)
    # Record occurrence output before current controls add a persistent panel
    # snapshot. Re-reading that snapshot must not prevent its Close action.
    delivered_output = bool(data.get("story") or data.get("status")
                            or data.get("_screen_texts"))
    # A plain wait may receive the bridge's most recently settled transaction
    # as diagnostic context.  That receipt belongs to an earlier act and must
    # not leak into an unrelated Back/state cycle.  Scoped waits are the only
    # callers asking for transaction metadata.
    raw_transaction = getattr(result, "transaction", None)
    transaction = _transaction_view(raw_transaction)
    if (
        params.get("action_nonce") is None
        and transaction
        and transaction.get("transaction_state") == "settled"
    ):
        transaction = None
    if transaction:
        data["transaction"] = transaction

    # Fetch the current rendered state.  build_state_data() is the canonical
    # wait/state decision pipeline: game_state > pending snapshot > screen.
    def remaining(cap: float) -> float:
        if not isinstance(composition_deadline, (int, float)):
            return cap
        return max(
            0.0,
            min(cap, float(composition_deadline) - time.time()),
        )

    gs = None
    gs_timeout = remaining(2.0) if not foreign_action_boundary else 0.0
    if gs_timeout > 0:
        try:
            gs = _call_with_timeout(ctx.client.game_state, gs_timeout)
        except Exception:
            pass
    screen = (
        getattr(result, "screen", None)
        if not foreign_action_boundary else None
    )
    if isinstance(screen, dict) and screen.get("type") == "game_state":
        # A transaction can settle on a game-state observation. That proves
        # the control surface changed, but carries no rendered panel body.
        # Keep game_state authoritative for controls and fetch real screen
        # content instead of treating this witness as an empty screenshot.
        if not isinstance(gs, dict):
            gs = screen
        screen = None
    if screen is None and not foreign_action_boundary:
        screen_timeout = remaining(2.0)
        screen = _get_screen(ctx, timeout=screen_timeout) \
            if screen_timeout > 0 else None
    if not result.pending:
        screen, gs = _refresh_single_enter_screen(
            ctx, screen, gs, deadline=composition_deadline,
        )

    # Settle delay: a choice request is published before Ren'Py finishes the
    # frame that adds persistent HUD controls. Likewise, closing an overlay may
    # leave one pre-close game_state sample behind. Briefly re-fetch either
    # boundary so the returned decision contains the stable screen controls.
    _had_overlay = (
        (screen and screen.get("overlay_active"))
        or (gs and not gs.get("interactions") and effective_pending))
    _overlay_screen_sample = screen
    _overlay_game_state_sample = gs
    _decision_settle_delay = 0.5 if _had_overlay else 0.15
    if ((_had_overlay or effective_pending)
            and remaining(_decision_settle_delay) > 0):
        import time as _t
        _t.sleep(remaining(_decision_settle_delay))
        gs_timeout = remaining(2.0)
        refreshed_gs = None
        if gs_timeout > 0:
            try:
                refreshed_gs = _call_with_timeout(
                    ctx.client.game_state, gs_timeout)
                if refreshed_gs is not None:
                    gs = refreshed_gs
            except Exception:
                pass
        screen_timeout = remaining(2.0)
        if screen_timeout > 0:
            refreshed_screen = _get_screen(ctx, timeout=screen_timeout)
            if refreshed_screen is not None:
                screen = refreshed_screen
            if (
                _had_overlay
                and screen
                and (
                    screen.get("overlay_active")
                    or screen.get("modal_screens")
                )
                and _game_state_proves_overlay_closed(
                    effective_pending,
                    refreshed_gs,
                    overlay_screen=screen,
                    previous_game_state=_overlay_game_state_sample,
                )
            ):
                # /screen is a latest-state cache and can remain on an old
                # overlay if its noncritical update was lost. A newer ordered
                # game_state with the exact pending action surface proves the
                # cached overlay no longer owns the interaction.
                # A fresh game-state frame must not let the pre-close cached
                # overlay suppress that live menu.
                screen = None

    # The mirror image of the close case just above: a stamped screen snapshot
    # that predates a panel the live game_state already declares.  Presenting
    # from it drops the panel's whole body, so replace it with a provably
    # later read before anything is composed.
    if not foreign_action_boundary:
        screen = _refresh_modal_panel_screen(
            ctx, screen, gs, deadline=composition_deadline,
        )
    modal_panel_open = bool(_declared_modal_panels(gs))

    lifecycle_context = None
    lifecycle_state = None
    title_boundary = bool(
        screen
        and (
            screen.get("main_menu")
            or "main_menu" in (screen.get("screens") or [])
        )
    )
    if data.get("ended") or title_boundary or (
        gs and (gs.get("game_terminal") or gs.get("progress_frozen"))
    ):
        # /game_state intentionally serves the frozen final run snapshot at
        # the title menu. Pair it with the bridge lifecycle before formatting:
        # the snapshot remains useful to history consumers, but is no longer
        # live state and must not become wait()'s footer.
        state_timeout = remaining(2.0)
        if state_timeout > 0:
            try:
                lifecycle_state = ctx.client.state(timeout=state_timeout)
                lifecycle_context = lifecycle_state.get("context")
            except Exception:
                pass
    terminal_verdict = None
    if (
        isinstance(lifecycle_state, dict)
        and isinstance(lifecycle_state.get("game_terminal"), bool)
    ):
        # /state was read after /game_state, so both True and False edges are
        # authoritative. A fresh Start/load may clear a frozen snapshot while
        # the inverse ordering can latch an ending after the snapshot read.
        terminal_verdict = lifecycle_state["game_terminal"]
    elif gs and gs.get("game_terminal") is True:
        # /game_state was sampled before the lifecycle read. It is a useful
        # positive fallback, but its False cannot erase a newer game_ended
        # event when the confirming /state request failed.
        terminal_verdict = True
    if terminal_verdict:
        # A scoped transaction can settle just before the bridge's synthetic
        # return-to-menu event is attached to its receipt. The live frozen
        # game_state is authoritative in that boundary window. Preserve the
        # terminal verdict even though the title menu exposes navigation.
        data["ended"] = True
        data["_game_terminal"] = True
    elif terminal_verdict is False:
        data.pop("_game_terminal", None)
        lifecycle_status = str(
            (lifecycle_state or {}).get("status") or "").lower()
        if lifecycle_status == "ended":
            # game_terminal is the playthrough-ending verdict, not process
            # liveness. Quit/crash exits intentionally carry False while the
            # lifecycle still authoritatively reports that the game ended.
            data["ended"] = True
        else:
            data["ended"] = False
    if title_boundary and (terminal_verdict or data.get("ended")):
        _suppress_title_return_store_resets(
            data, gs, events=getattr(result, "events", None))
    current = {} if foreign_action_boundary else _build_current_decision_data(
        gs=gs,
        screen=screen,
        pending=effective_pending,
        status="waiting_for_input" if effective_pending else "running",
        context=lifecycle_context,
    )
    _reconcile_status_stats_with_current(
        data, current, events=getattr(result, "events", None), game_state=gs)
    _clear_stale_screen_text_for_current_decision(
        data, current, modal_panel_open=modal_panel_open)
    _merge_decision_data(
        data, current, replace_pending=bool(effective_pending))
    if not foreign_action_boundary:
        _replace_stale_pending_from_state(
            ctx, data, deadline=composition_deadline)

    if ctx.hooks.after_wait:
        data = ctx.hooks.after_wait(data, result)

    if not foreign_action_boundary:
        _merge_current_decision(ctx, data, deadline=composition_deadline)
    _suppress_resolved_synthetic_choice(data, raw_transaction)

    fmt = params.get("format", "text")
    out = format_wait_text(
        data, fmt=fmt, anomalies=ctx.anomaly_visibility)
    out["_raw_wait_ended"] = raw_wait_ended
    out["_delivered_output"] = delivered_output
    out["_title_boundary"] = title_boundary
    out["_foreign_action_boundary"] = foreign_action_boundary
    if delivered_overlays:
        out[_OVERLAY_DELIVERIES_KEY] = delivered_overlays
    _merge_passive_overlay_text(
        ctx,
        out,
        screen,
        fmt,
        deadline=composition_deadline,
        sample_live=not foreign_action_boundary,
    )
    _apply_transaction_to_output(out, transaction)
    # Boundary detection uses the structured controls, including modal
    # navigation. Make them available before deciding to drain more story.
    out["_data"] = data
    if (
        params.get("action_nonce") is not None
        and not params.get("_return_on_admission")
        and transaction
        and transaction.get("transaction_state") == "settled"
        and not _story_tail_has_boundary(out, allow_derived_terminal=True)
        and time.time() < caller_deadline
    ):
        # Transaction settlement says the action took effect; it does not say
        # its presentation has reached the next decision. Start and other
        # kinetic transitions can settle while their remaining story and
        # successor choice are still crossing onto the ordinary event lane.
        tail_params = dict(params)
        tail_params["_result_deadline"] = caller_deadline
        tail_params["_ordinary_only"] = True
        tail_params["_ordinary_action_id"] = transaction.get("action_id")
        tail_params["_allow_empty_story_tail"] = True
        tail_params["_allow_derived_terminal_tail"] = True
        _drain_story_gap_after_choice_action(ctx, out, tail_params, out)
        _apply_transaction_to_output(out, transaction)
    if (
        out.get("ended")
        and not out.get("_raw_wait_ended")
        and not out.get("_title_boundary")
    ):
        # A game-specific terminal verdict may precede its ending-card and
        # credits presentation. Keep the verdict available to lifecycle
        # consumers, but do not tell the player the game has ended while the
        # presentation itself reaches a real bridge end or title boundary.
        out["_defer_ended_banner"] = True
    else:
        out.pop("_defer_ended_banner", None)
    out["_data"] = data
    if params.get("action_nonce") is not None:
        _surface_scoped_wait_rejection(out, transaction)
        _ensure_transaction_recovery_guidance(out)
    _own_terminal_presentation_once(ctx, out, params)
    _ensure_empty_scoped_wait_receipt(out, params)
    if withheld_decision:
        _ensure_withheld_decision_hint(out)
    _remember_delivered_actionable_screen(
        ctx,
        out,
        screen,
        record_presentation=not params.get("_suppress_details", False),
    )
    _remember_rendered_decision(ctx, out)
    if (
        story_handback.fired
        and not out.get("pending")
        and not out.get("ended")
        and not out.get("_title_boundary")
    ):
        # Same handoff act() gives: the story so far, plus the hint that the
        # next wait continues it.  Suppressed if the composition found a
        # decision after all -- the caller has something to act on and needs
        # no invitation to wait again.
        _mark_act_story_continues(out)
        out.pop("_interrupted", None)
    # Surface the on_events early-exit signal so the harness can tell
    # whether the wait ended naturally (decision point / timeout) or
    # was interrupted by an operator message landing mid-poll.
    elif getattr(result, "interrupted", False):
        out["_interrupted"] = True
    elif (
        params.get("action_nonce") is None
        and not params.get("_ordinary_only")
        and gs is not None
        and (lifecycle_state or {}).get("status", "running") == "running"
        and not _story_tail_has_boundary(out)
        and not _rendered_has_story_text(out)
        and not out.get("warning")
        and not out.get("error")
    ):
        # A receipt can return before the long-story handback timer fires.
        # Stats alone are not a decision, nor proof that new story arrived.
        out["warning"] = "No new dialogue or decision yet; call wait() to continue."
    return out


def _suppress_title_return_store_resets(
    data: dict,
    game_state: dict | None,
    *,
    events: list[dict] | None = None,
) -> None:
    """Drop title-menu reset deltas that disagree with frozen final state."""
    status = data.get("status")
    if not isinstance(status, dict) or not isinstance(game_state, dict):
        return

    final_stats = game_state.get("stats")
    if isinstance(final_stats, dict) and isinstance(status.get("stats"), list):
        reset_names: set[str] = set()
        for event in events or []:
            if not isinstance(event, dict) or event.get("type") != "stats_update":
                continue
            changed = {
                name: value
                for name, value in (event.get("changed") or {}).items()
                if not str(name).startswith("_")
            }
            if changed and any(
                name not in final_stats or final_stats.get(name) != value
                for name, value in changed.items()
            ):
                # Store reset observers publish one aggregate event. Once one
                # member proves that snapshot disagrees with frozen final
                # state, every member belongs to the same reset occurrence.
                reset_names.update(changed)
        status["stats"] = [
            entry for entry in status["stats"]
            if not isinstance(entry, dict)
            or entry.get("stat") not in reset_names
        ]

    if "inventory" in status and isinstance(game_state.get("inventory"), list):
        def _inventory_signature(items: Any) -> list[tuple[str, str]]:
            signature = []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    name = item.get("name", str(item))
                    quantity = item.get("quantity")
                else:
                    name = str(item)
                    quantity = None
                signature.append((str(name), repr(quantity)))
            return sorted(signature)

        final_signature = _inventory_signature(game_state["inventory"])
        safe_inventory: list[str] = []
        inventory_events = [
            event for event in events or []
            if isinstance(event, dict) and event.get("type") == "inventory_update"
        ]
        matching_indices = [
            index for index, event in enumerate(inventory_events)
            if isinstance(event.get("inventory"), list)
            and _inventory_signature(event["inventory"]) == final_signature
        ]
        last_final_index = max(matching_indices) if matching_indices else None
        for index, event in enumerate(inventory_events):
            if event.get("post_terminal"):
                continue
            snapshot = event.get("inventory")
            if isinstance(snapshot, list):
                # Independent POST ordering can let a reset arrive before the
                # bridge sees the title boundary. Preserve the ordered chain
                # that culminates in frozen final state, but reject divergent
                # snapshots after it (and unmatched reset-only batches).
                if (
                    last_final_index is None
                    or index > last_final_index
                ) and _inventory_signature(snapshot) != final_signature:
                    continue
            event_status = build_wait_data([event]).get("status") or {}
            event_inventory = event_status.get("inventory") or []
            if event.get("changed") is not None:
                safe_inventory.extend(event_inventory)
            else:
                safe_inventory = list(event_inventory)
        if inventory_events:
            if safe_inventory:
                status["inventory"] = safe_inventory
            else:
                status.pop("inventory", None)

    if not any(value for value in status.values()):
        data.pop("status", None)


def _reconcile_status_stats_with_current(
    data: dict,
    current: dict,
    *,
    events: list[dict] | None = None,
    game_state: dict | None = None,
) -> None:
    """Replace sampled stat values only when live state is provably newer.

    Ren'Py can expose a returned menu before the store observer emits the
    mutation performed immediately before that return. A fast next action can
    therefore attribute the observer's old delta to the successor. Conversely,
    the observer emits ``stats_update`` before the scraper publishes its next
    game-state sample. Source timestamps distinguish those cases; bridge event
    sequence cannot, because the shim POSTs events on independent threads.
    """
    live = current.get("stats") or {}
    try:
        live_ts = float((game_state or {}).get("_stats_ts"))
    except (TypeError, ValueError):
        return
    latest_update_ts: dict[str, float] = {}
    for event in events or []:
        if not isinstance(event, dict) or event.get("type") != "stats_update":
            continue
        try:
            event_ts = float(event.get("_ts"))
        except (TypeError, ValueError):
            continue
        for name in (event.get("changed") or {}):
            if not str(name).startswith("_"):
                latest_update_ts[name] = max(
                    event_ts, latest_update_ts.get(name, event_ts))
    status = data.get("status") or {}
    updates = status.get("stats") or []
    for update in updates:
        if not isinstance(update, dict):
            continue
        name = update.get("stat")
        update_ts = latest_update_ts.get(name)
        if (
            update_ts is not None
            and live_ts > update_ts
            and name in live
            and update.get("value") != live[name]
        ):
            update["value"] = live[name]
            update.pop("delta", None)


def _merge_late_transcript_events(
    ctx: HandlerContext,
    result: object,
    *,
    deadline: float | None = None,
    ordinary_action_id: object = None,
    action_scoped: bool = False,
) -> bool:
    """Append story events recorded after wait returned a pending request.

    Some Roadwarden input screens emit the fresh choice_request before the
    answer narration. BridgeClient.wait() correctly returns at the decision
    point, but formatting should still include immediately-following story
    events that are already in the transcript. Return true when an earlier
    prefetched ownership boundary also requires withholding that decision.
    """
    pending = getattr(result, "pending", None)
    if not pending:
        return
    events = getattr(result, "events", None)
    if not isinstance(events, list):
        return
    base_types = {
        "narration",
        "dialogue",
        "auto_skipped",
        "screen_text",
        "choice_request",
        "input_request",
    }
    last_seq = max(
        (
            int(event.get("_seq", 0) or 0)
            for event in events
            if event.get("type") in base_types
        ),
        default=0,
    )
    if last_seq <= 0:
        return
    remaining = deadline - time.time() if deadline is not None else 3.0
    if remaining <= 0:
        return
    try:
        transcript_method = ctx.client.transcript
        try:
            supports_timeout = "timeout" in inspect.signature(
                transcript_method).parameters
        except (TypeError, ValueError):
            supports_timeout = True
        if supports_timeout:
            transcript = transcript_method(
                last=12, timeout=min(3.0, remaining)) or []
        else:
            transcript = transcript_method(last=12) or []
    except Exception:
        return
    if deadline is not None and time.time() >= deadline:
        return
    seen = {
        int(event.get("_seq", 0) or 0)
        for event in events
        if int(event.get("_seq", 0) or 0) > 0
    }
    story_types = {"narration", "dialogue", "auto_skipped"}
    late = []
    for event in transcript:
        seq = int(event.get("_seq", 0) or 0)
        if seq <= last_seq or seq in seen:
            continue
        if event.get("type") in story_types:
            late.append(event)
    prefix = []
    if late:
        claim_prefix = getattr(
            ctx.client, "claim_prefetched_visible_prefix", None)
        if callable(claim_prefix):
            rescue_through_seq = max(
                int(event.get("_seq", 0) or 0) for event in late)
            prefix, blocked = claim_prefix(
                rescue_through_seq + 1,
                ordinary_action_id=ordinary_action_id,
                action_scoped=action_scoped,
            )
            if blocked:
                # Leave both lanes untouched. The durable stream will deliver
                # the transcript candidate after the prefetched fence clears.
                return True
            events.extend(prefix)
            prefix_seqs = {
                int(event.get("_seq", 0) or 0)
                for event in prefix
                if isinstance(event, dict)
                and int(event.get("_seq", 0) or 0) > 0
            }
            if prefix_seqs:
                # Transcript rescue was computed before the prefix claim. A
                # durable row may therefore be present in both lanes; its
                # bridge sequence is the occurrence identity in either copy.
                late = [
                    event for event in late
                    if int(event.get("_seq", 0) or 0) not in prefix_seqs
                ]
    claim_action_events = getattr(
        ctx.client, "claim_undelivered_action_events", None)
    if late and callable(claim_action_events):
        transaction = getattr(result, "transaction", None)
        transaction_generation = (
            transaction.get("reset_generation")
            if isinstance(transaction, dict) else None
        )
        late = claim_action_events(
            late, reset_generation=transaction_generation,
        )
    if late:
        events.extend(late)
    if prefix or late:
        events.sort(key=lambda event: int(event.get("_seq", 0) or 0))
    if late:
        ctx.overlay.transcript_rescued_seqs.update(
            int(event.get("_seq", 0) or 0) for event in late
        )
        if len(ctx.overlay.transcript_rescued_seqs) > 512:
            ctx.overlay.transcript_rescued_seqs = set(
                sorted(ctx.overlay.transcript_rescued_seqs)[-256:]
            )
    return False


def _drop_already_rescued_transcript_events(
    ctx: HandlerContext,
    result: object,
) -> None:
    """Discard queue copies of story already surfaced by transcript rescue."""
    events = getattr(result, "events", None)
    if not isinstance(events, list) or not ctx.overlay.transcript_rescued_seqs:
        return
    if any(
        isinstance(event, dict) and event.get("type") == "game_started"
        for event in events
    ):
        # Bridge reset starts transcript sequence numbers over. Old receipts
        # must not suppress unrelated rows in the new run when ids collide.
        ctx.overlay.transcript_rescued_seqs.clear()
        return
    caught_up = {
        int(event.get("_seq", 0) or 0)
        for event in events
        if isinstance(event, dict)
        and int(event.get("_seq", 0) or 0) in ctx.overlay.transcript_rescued_seqs
    }
    if not caught_up:
        return
    events[:] = [
        event for event in events
        if not (
            isinstance(event, dict)
            and int(event.get("_seq", 0) or 0) in caught_up
        )
    ]
    ctx.overlay.transcript_rescued_seqs.difference_update(caught_up)


def _pending_id_from_data(data: dict | None) -> str | None:
    data = data or {}
    raw = data.get("_pending_raw") or {}
    pending = data.get("pending") or {}
    return raw.get("id") or pending.get("id")


def _replace_stale_pending_from_state(
    ctx: HandlerContext,
    data: dict,
    *,
    deadline: float | None = None,
) -> None:
    """Key wait's pending snapshot on the bridge's ACTIVE request.

    Replace it when /state already has a newer one, and DROP it when
    /state has none at all — a snapshot the bridge no longer carries
    was resolved while we were composing (auto-advance, native click,
    another client), and rendering it re-offers a decided choice (run
    driftwood re-rendered one 52s after resolution, 2026-08-18). The
    no-pending read is re-checked once after a short delay so a
    menu→menu transition (old cleared, new not yet registered) isn't
    mistaken for a resolution; a genuinely new pending that registers
    just after the drop is picked up by _merge_current_decision.

    A decision built from live ``game_state`` choice rows carries no request
    id at all (``format._pending_from_choice_interactions`` stamps ``id: ""``).
    It is a real menu — the freshest surface there is — but an unnamed one, so
    ``act N`` against it cannot be told apart from a reply to whatever the
    caller saw before.  Name it here from the bridge's own request whenever
    that request is the same numbered menu, so the response the caller
    receives can be recorded as the numeric binding.
    """
    current_id = _pending_id_from_data(data)
    unnamed_labels = (
        _numbered_choice_labels((data.get("_pending_raw") or {}).get("choices"))
        if not current_id else []
    )
    if not current_id and not unnamed_labels:
        return
    remaining = (
        max(0.0, float(deadline) - time.time())
        if isinstance(deadline, (int, float)) else 3.0
    )
    if remaining <= 0:
        return
    try:
        raw = _call_with_timeout(ctx.client.state, min(3.0, remaining)) or {}
    except Exception:
        return
    if not raw:
        # Empty response = state unavailable, not "no pending" — an
        # unreachable bridge must not eat a real decision point.
        return
    state_data = _build_decision_state_data(raw)
    state_id = _pending_id_from_data(state_data)
    if not current_id:
        # The rendered menu has no request id of its own.  Adopt the bridge's
        # request only when it is the SAME numbered menu; anything else leaves
        # the live surface exactly as composed (it is newer than /state, and
        # dropping it would hide a decision the game is really showing).
        acted_id = getattr(ctx.client, "_acted_request_id", None)
        if (
            state_id
            and state_id != acted_id
            and _numbered_choice_labels(
                (state_data.get("_pending_raw") or {}).get("choices")
            ) == unnamed_labels
        ):
            _merge_decision_data(data, state_data, replace_pending=True)
        return
    if state_id == current_id:
        if "_actionable_snapshot" in state_data:
            data["_actionable_snapshot"] = state_data["_actionable_snapshot"]
        return
    if not state_id:
        import time as _t
        remaining = (
            max(0.0, float(deadline) - _t.time())
            if isinstance(deadline, (int, float)) else 0.4
        )
        if remaining <= 0:
            return
        _t.sleep(min(0.4, remaining))
        remaining = (
            max(0.0, float(deadline) - _t.time())
            if isinstance(deadline, (int, float)) else 3.0
        )
        if remaining <= 0:
            return
        try:
            raw = _call_with_timeout(
                ctx.client.state, min(3.0, remaining)) or {}
        except Exception:
            return
        state_data = _build_decision_state_data(raw)
        state_id = _pending_id_from_data(state_data)
        if state_id == current_id:
            if "_actionable_snapshot" in state_data:
                data["_actionable_snapshot"] = state_data[
                    "_actionable_snapshot"
                ]
            return
        if not raw:
            return
        if not state_id:
            data.pop("pending", None)
            data.pop("_pending_raw", None)
            return
    acted_id = getattr(ctx.client, "_acted_request_id", None)
    if acted_id and state_id == acted_id:
        return
    _merge_decision_data(data, state_data, replace_pending=True)


def _merge_current_decision(
    ctx: HandlerContext,
    data: dict,
    *,
    deadline: float | None = None,
) -> None:
    """Patch in a just-arrived decision that wait() missed at timeout."""
    if data.get("pending") or data.get("buttons") or data.get("ended"):
        return
    if not data.get("story") and not data.get("status"):
        return
    raw = None
    import time as _t
    stop_at = _t.time() + 1.0
    if isinstance(deadline, (int, float)):
        stop_at = min(stop_at, float(deadline))
    while True:
        remaining = max(0.0, stop_at - _t.time())
        if remaining <= 0:
            return
        try:
            candidate = _call_with_timeout(
                ctx.client.state, min(3.0, remaining)) or {}
        except Exception:
            candidate = {}
        state_data = _build_decision_state_data(candidate)
        if state_data.get("pending") or state_data.get("buttons"):
            raw = candidate
            break
        if _t.time() >= stop_at:
            raw = candidate
            break
        _t.sleep(min(0.2, max(0.0, stop_at - _t.time())))

    pending = raw.get("pending_request") or {}
    pending_id = pending.get("id")
    acted_id = getattr(ctx.client, "_acted_request_id", None)
    if pending_id and acted_id and pending_id == acted_id:
        return

    state_data = _build_decision_state_data(raw)
    _clear_stale_screen_text_for_current_decision(data, state_data)
    _merge_decision_data(data, state_data)


def _suppress_resolved_synthetic_choice(
    data: dict,
    transaction: dict | None,
) -> None:
    """Do not re-offer a just-resolved menu synthesized from stale state.

    Custom ``call screen`` choices sometimes exist only in game_state and are
    therefore represented with an empty request id. That fallback is valid,
    but one frame of stale interactions after a choice resolves can recreate
    the menu the player just answered. Suppress only an exact content match to
    the transaction's initial choice list. A different successor menu, or any
    authoritative request id, remains actionable.
    """
    if not isinstance(transaction, dict):
        return
    if transaction.get("transaction_state") != "settled":
        return
    if transaction.get("resolved_as") != "choice":
        return
    if not transaction.get("initial_request_id"):
        return
    if transaction.get("settled_pending"):
        return
    raw = data.get("_pending_raw")
    if not isinstance(raw, dict) or raw.get("type") != "choice_request":
        return
    if raw.get("id"):
        return

    signature = (
        transaction.get("initial_request_content_signature")
        or transaction.get("initial_request_signature")
    )
    if not isinstance(signature, str):
        return
    try:
        import json
        initial = json.loads(signature)
    except (TypeError, ValueError):
        return
    if not isinstance(initial, dict):
        return

    def labels(items: object) -> list[str]:
        result = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                value = item.get("label") or item.get("caption") or ""
            else:
                value = item
            normalized = _normalize_label(str(value))
            if normalized:
                result.append(normalized)
        return result

    if labels(raw.get("choices")) != labels(initial.get("choices")):
        return
    data.pop("pending", None)
    data.pop("_pending_raw", None)
    data.pop("_actionable_snapshot", None)


# How long a composition may poll /screen for a modal panel the live
# game_state already declares.  One extra read normally settles it -- the
# panel's screen_content and its game_state come from the same shim frame --
# so this is a bound on a race, not a budget anyone is expected to spend.
_MODAL_PANEL_SCREEN_POLL_SECONDS = 1.5
_MODAL_PANEL_SCREEN_POLL_INTERVAL = 0.15


def _declared_modal_panels(source: dict | None) -> list:
    """Modal-overlay panel tags a raw shim payload declares."""
    return _declared_modal_overlay_tags(
        source if isinstance(source, dict) else None)


def _modal_panel_screen_is_stale(
    screen: dict | None,
    game_state: dict | None,
) -> bool:
    """Does this screen snapshot predate a panel the game_state already shows?

    ``handle_wait`` prefers the screen a transaction stamped on itself over a
    live ``/screen`` read.  The bridge stamps ``settled_screen`` when it first
    observes the action's boundary, and for a panel toggle that boundary is
    the mid-click frame -- one frame BEFORE the panel renders.  Composing from
    that snapshot means ``build_state_data`` never enters its overlay branch,
    so the panel contributes no ``_screen_texts`` and no ``_hidden_menu``,
    while the buttons still come from the live game_state.  Fleet R62: 9/9
    ``act('LOG')`` returned ``1: CLOSE`` and nothing else, and 6/6
    ``act('KIT')`` dropped the ``x0`` quantity rows, while a ``state()`` on the
    very same open panel rendered the whole thing.

    The live game_state is the newer sample, so its declaration wins.
    """
    live = _declared_modal_panels(game_state)
    if not live:
        return False
    shown = set(_declared_modal_panels(screen))
    return not set(live).issubset(shown)


def _refresh_modal_panel_screen(
    ctx: HandlerContext,
    screen: dict | None,
    game_state: dict | None,
    *,
    deadline: float | None,
) -> dict | None:
    """Re-read /screen until it shows the panel the game_state declares.

    Bounded and evidence-gated: it runs only once the live game_state has
    already declared the panel, and it accepts a replacement only when that
    replacement is provably a LATER sample than the snapshot being replaced
    (the same ``_source_seq``-beyond-the-anchor proof E2 uses).  If the panel
    never reaches ``/screen`` inside the bound, the stamped snapshot is kept
    and the caller's next wait renders the panel from its own read.
    """
    if not _modal_panel_screen_is_stale(screen, game_state):
        return screen
    anchor = screen if isinstance(screen, dict) else {}
    anchor_source_id = str(anchor.get("_source_id") or "") or None
    anchor_source_seq = anchor.get("_source_seq")
    anchor_seq = anchor.get("_seq")
    has_anchor = (
        (anchor_source_id and type(anchor_source_seq) is int)
        or type(anchor_seq) is int
    )
    poll_end = time.time() + _MODAL_PANEL_SCREEN_POLL_SECONDS
    if isinstance(deadline, (int, float)):
        poll_end = min(poll_end, float(deadline))
    while True:
        remaining = poll_end - time.time()
        if remaining <= 0:
            return screen
        try:
            candidate = _get_screen(ctx, timeout=min(1.0, remaining))
        except Exception:
            candidate = None
        if (
            isinstance(candidate, dict)
            and not _modal_panel_screen_is_stale(candidate, game_state)
            and (
                not has_anchor
                or _post_action_sample_is_after(
                    candidate,
                    source_id=anchor_source_id,
                    source_seq=(
                        anchor_source_seq
                        if type(anchor_source_seq) is int else None
                    ),
                    admission_seq=(
                        anchor_seq if type(anchor_seq) is int else None
                    ),
                )
            )
        ):
            return candidate
        remaining = poll_end - time.time()
        if remaining <= 0:
            return screen
        time.sleep(min(_MODAL_PANEL_SCREEN_POLL_INTERVAL, remaining))


def _clear_stale_screen_text_for_current_decision(
    data: dict,
    state_data: dict,
    *,
    modal_panel_open: bool = False,
) -> None:
    """Drop stale overlay scrape text when a fresh underlay decision appears.

    ``modal_panel_open`` is the live game_state's own answer to "is a declared
    modal panel on screen right now".  While one is, the drained screen_text
    rows ARE the current surface, not a leftover scrape: clearing them there
    consumes the panel's body from the bridge cursor and no later call can
    re-deliver it.  Fleet R62 lost every LOG panel that way when the screen
    snapshot the composition used predated the panel.
    """
    if modal_panel_open:
        return
    if state_data.get("_overlay_active") or state_data.get("_screen_texts"):
        return
    if not (state_data.get("pending") or state_data.get("buttons")):
        return
    story = data.get("story")
    if not isinstance(story, list):
        return
    filtered = [
        item for item in story
        if not (
            isinstance(item, dict)
            and item.get("source") == "screen_text"
            and not item.get("passive_overlay_snapshot")
        )
    ]
    if filtered:
        data["story"] = filtered
    else:
        data.pop("story", None)
    data.pop("_screen_texts", None)
    data.pop("_overlay_active", None)
    data.pop("_overlay_snapshot_identity", None)
    data.pop("_modal_overlay_screens", None)
    data.pop("_modal_overlay_panel", None)
    data.pop("_hidden_menu", None)


def _build_current_decision_data(
    *,
    gs: dict | None,
    screen: dict | None,
    pending: dict | None,
    status: str = "running",
    context: dict | None = None,
) -> dict:
    """Build the canonical current decision payload used by state/wait."""
    raw = {
        "status": status,
        "game_state": gs or {},
        "pending_request": pending,
        "screen": screen or {},
        "context": context,
    }
    data = _build_decision_state_data(raw)
    return data


def _merge_decision_data(
    data: dict,
    state_data: dict,
    *,
    replace_pending: bool = False,
    replace_footer: bool = False,
) -> None:
    """Merge build_state_data() decision fields into wait data.

    Wait data may have a structured status block; screen-act data may instead
    carry a status string. Preserve either shape rather than copying
    state_data["status"]. Everything actionable comes from the shared builder.
    """
    for key in (
        "pending",
        "_pending_raw",
        "buttons",
        # The bridge's current (unresolved) anomaly latch, so a wait that
        # began after the crash was first streamed still renders it while
        # the exception screen is up. Streamed and latched copies of the
        # same anomaly render as one line.
        "anomalies",
        "_button_categories",
        "_overlay_active",
        "_overlay_snapshot_identity",
        "_modal_overlay_screens",
        "_modal_overlay_panel",
        "_hidden_menu",
        "_screen_texts",
        "_actionable_snapshot",
        "_raw_wait_ended",
        "_title_boundary",
        "_defer_ended_banner",
        "_foreign_action_boundary",
    ):
        if key in state_data:
            data[key] = state_data[key]
        elif replace_pending and key in ("pending", "_pending_raw"):
            data.pop(key, None)
    summary = state_data.get("_stats_summary")
    # The observer and screen sampler publish independently. A formatted
    # summary cannot be patched safely when this response's deltas disagree.
    sampled_stats = state_data.get("stats") or {}
    status = data.get("status")
    updates = (status.get("stats") or []) if isinstance(status, dict) else []
    if any(isinstance(update, dict)
           and update.get("stat") in sampled_stats
           and update.get("value") != sampled_stats[update["stat"]]
           for update in updates):
        data.pop("_footer", None)
        return
    if summary and (replace_footer or "_footer" not in data):
        data["_footer"] = f"  {summary}"


def _promote_wait_output(result: dict, wait_result: dict) -> None:
    """Expose rendered wait sections at top level for MCP text joining.

    Overlay rows are the one exception to "the newer render wins".  The
    passive-overlay ledger hands each row out exactly once, so rows already
    spent into ``result`` are carried across the promotion (deduplicated
    against whatever the newer render shows) instead of being dropped with
    the output that happened to collect them.  Run nightecho2 lost both of
    ECHO-7's answers that way: the settle wait rendered them, and the
    state-change re-render promoted over the top.
    """
    keys = (
        "text",
        "story",
        "screen_text",
        "status",
        "pending",
        "anomaly_note",
        "overlay_note",
        "buttons",
        "brief",
        "ended",
        "_footer",
        "_data",
        "_pending_raw",
        "_actionable_snapshot",
        "_raw_wait_ended",
        "_title_boundary",
        "_defer_ended_banner",
        "_foreign_action_boundary",
        _OVERLAY_DELIVERIES_KEY,
        _SCREEN_TEXT_BEFORE_STORY_KEY,
        _STORY_RENDER_SECTIONS_KEY,
    )
    carried = _overlay_delivery_records(result)
    carried_sections = [
        {
            "channel": "screen_text",
            "text": record["text"],
            "delivery_ids": [record["id"]],
            **(
                {"_bridge_seq": record["_bridge_seq"]}
                if type(record.get("_bridge_seq")) is int else {}
            ),
        }
        for record in carried
    ]
    wait_sections = _story_render_sections(wait_result)
    carried_as_list = isinstance(result.get("screen_text"), list)
    for key in keys:
        result.pop(key, None)
    for key in keys:
        if key in wait_result:
            result[key] = wait_result[key]
    if carried:
        _carry_overlay_deliveries_into(
            result, carried, as_list=carried_as_list)
    sections = _merge_story_render_sections_by_bridge_sequence(
        carried_sections, wait_sections)
    if sections:
        result[_STORY_RENDER_SECTIONS_KEY] = sections
        _order_overlay_deliveries_by_sections(result, sections)


def _merge_status_output(previous: Any, current: Any) -> Any:
    """Merge delivered status occurrences without changing wire format.

    Not the same rule as ``format.build_wait_data``'s stat coalescing, which
    runs over the raw events of ONE wait and therefore sums composable numeric
    deltas.  This merges two already-coalesced windows, whose deltas can
    overlap (A+B followed by B+C) with no occurrence ids left to tell the
    repeat from a new transition, so the newer aggregate wins instead.  Keep
    them separate.
    """
    if not previous:
        return current
    if not current:
        return previous
    if previous == current:
        return current
    if isinstance(previous, str) and isinstance(current, str):
        if previous.strip() == current.strip():
            return current
        return _join_story_text(previous.strip(), current.strip())
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return current

    merged = dict(previous)
    for key, value in current.items():
        if key == "inventory":
            # Inventory snapshots are complete, so the newest wins.
            merged[key] = list(value or [])
            continue
        if key == "stats":
            prior_entries = list(merged.get(key) or [])
            # Settle/state promotion can replay part of an already delivered
            # batch alongside a new stat. Remove exact overlap before delta
            # coalescing so the replay cannot double-count a transition.
            fresh_entries = [
                entry for entry in list(value or [])
                if entry not in prior_entries
            ]
            combined = prior_entries + fresh_entries
            coalesced = {}
            order = []
            for entry in combined:
                if not isinstance(entry, dict) or not entry.get("stat"):
                    continue
                name = entry["stat"]
                if name not in coalesced:
                    coalesced[name] = dict(entry)
                    order.append(name)
                    continue
                prior = coalesced[name]
                prior["value"] = entry.get("value")
                # Each promoted wait has already coalesced its own event
                # window. Those windows can overlap (A+B followed by B+C),
                # and no occurrence IDs survive in JSON status. Summing here
                # would fabricate A+B+B+C, so the newer aggregate is the
                # conservative authoritative description for this stat.
                if "delta" in entry:
                    prior["delta"] = entry.get("delta")
                else:
                    prior.pop("delta", None)
            merged[key] = [coalesced[name] for name in order]
            continue
        if isinstance(value, list):
            combined = list(merged.get(key) or [])
            for item in value:
                if item not in combined:
                    combined.append(item)
            merged[key] = combined
            continue
        merged[key] = value
    return merged


def _promote_wait_output_preserving_story(
    result: dict,
    wait_result: dict,
) -> None:
    """Promote a follow-up wait result without losing prior story text."""
    previous_sections = _story_render_sections(result)
    if _active_overlay_snapshot_replaces_previous(result, wait_result):
        # Blocking overlays are current-state snapshots, not occurrences. A
        # settle chain can observe the same LOG/KIT panel several times; only
        # explicitly identified historical rows are additive across those
        # reads. Keeping every unowned snapshot produced the fleet-r30 sixfold
        # and sevenfold panel replays even though the bridge journal contained
        # one clean screen_text event.
        previous_sections = [
            section for section in previous_sections
            if section.get("channel") != "screen_text"
            or section_identities(section)
        ]
    previous_text = _render_value_text(result.get("text"))
    previous_status = result.get("status")
    if _is_co_terminal_older_story_snapshot(
        previous_text,
        previous_sections,
        _render_value_text(wait_result.get("text")),
        _story_render_sections(wait_result),
    ):
        # A state read can reconstruct the whole visible dialogue history
        # without source occurrence ids.  When it ends at the same substantial
        # story tail as the transaction output, it is an older snapshot of the
        # same scene, not fresh narration.  Keep its state/pending fields while
        # withholding only the unowned story channel.
        wait_result = dict(wait_result)
        wait_result.pop("text", None)
        wait_result.pop("story", None)
        wait_result[_STORY_RENDER_SECTIONS_KEY] = [
            section for section in _story_render_sections(wait_result)
            if section.get("channel") != "text"
        ]
        data = wait_result.get("_data")
        if isinstance(data, dict) and data.get("story"):
            wait_result["_data"] = {**data, "story": []}
    previous_story = list(result.get("story") or [])
    previous_deliveries = _overlay_delivery_records(result)
    story_deliveries = [
        record for record in previous_deliveries
        if record.get("channel") == "story"
    ]
    result[_OVERLAY_DELIVERIES_KEY] = [
        record for record in previous_deliveries
        if record.get("channel") != "story"
    ]
    result["wait"] = wait_result
    _promote_wait_output(result, wait_result)
    # Status events are occurrences just like story text. A follow-up state
    # render often carries the successor menu/footer but no event batch; it
    # must not erase a stats/inventory update already delivered by the
    # transaction wait.
    current_status = result.get("status")
    if previous_status:
        result["status"] = _merge_status_output(
            previous_status, current_status)
    promoted_sections = _story_render_sections(result)
    new_text = _render_value_text(result.get("text"))
    if previous_text and previous_text not in new_text:
        result["text"] = _join_story_text(
            previous_text, new_text)
    if previous_story:
        current_story = list(result.get("story") or [])
        previous_delivery_ids = {
            item.get("overlay_delivery_id")
            for item in previous_story
            if isinstance(item, dict)
            and isinstance(item.get("overlay_delivery_id"), int)
        }
        result["story"] = previous_story + [
            item for item in current_story
            if not (
                isinstance(item, dict)
                and item.get("overlay_delivery_id") in previous_delivery_ids
            )
        ]
    # These occurrences never left the preserved text channel. Reattach only
    # their ownership records; carrying their strings would print them twice.
    _merge_overlay_delivery_records(result, story_deliveries)
    sections = _merge_story_render_sections_by_bridge_sequence(
        previous_sections, promoted_sections)
    if sections:
        result[_STORY_RENDER_SECTIONS_KEY] = sections
        _order_overlay_deliveries_by_sections(result, sections)


def _absorb_unpromoted_wait_story(result: dict, wait_result: Any) -> None:
    """Keep script story from a follow-up wait the settle chain rejected.

    Every follow-up ``handle_wait`` inside the act settle CONSUMES the bridge
    event stream: ``poll()`` advances the cursor and action-attributed rows are
    booked in the cross-lane delivery ledger.  A branch that then rejects the
    render for its pending/signature shape therefore does not defer those
    lines, it destroys them -- there is no lane left that can serve them
    again.  Fleet R62 lost 31 NVL lines exactly this way (the antenna
    storm-damage beat, including ARIA's repair instruction, on five agents),
    silently: no gap marker, and no agent noticed.

    Screen scrapes stay droppable -- a panel snapshot is current state and the
    successor render carries it.  The game's own say/narration lines are
    occurrences and are never droppable, so they are carried onto the act
    result even when the render that delivered them is not.  Worst case this
    shows the agent a line twice (``_join_story_text`` removes an overlapping
    tail, and identical lines are skipped); dup-not-drop is the standing bias.
    """
    if not isinstance(wait_result, dict) or not isinstance(result, dict):
        return
    if not _rendered_has_script_story(wait_result):
        return
    new_text = _render_value_text(wait_result.get("text"))
    if not new_text:
        return
    previous_text = _render_value_text(result.get("text"))
    previous_lines = {
        line.strip() for line in previous_text.splitlines() if line.strip()
    }
    kept = [
        line for line in new_text.splitlines()
        if line.strip() and line.strip() not in previous_lines
    ]
    if not kept:
        return
    addition = "\n".join(kept)
    result["text"] = _join_story_text(previous_text, addition)
    if result.get(_STORY_RENDER_SECTIONS_KEY):
        # The explicit section plan is what render_tool_result_text prints
        # when it is present; appending only to ``text`` would keep the lines
        # invisible.
        result[_STORY_RENDER_SECTIONS_KEY] = _story_render_sections(result) + [
            {"channel": "text", "text": addition},
        ]
    result[_SCRIPT_STORY_MARKER] = True


def _active_overlay_snapshot_replaces_previous(
    previous: dict,
    current: dict,
) -> bool:
    """True when the current blocking-overlay snapshot supersedes the prior one."""
    previous_data = previous.get("_data") or {}
    current_data = current.get("_data") or {}
    if not (
        isinstance(previous_data, dict)
        and isinstance(current_data, dict)
        and previous_data.get("_overlay_active")
        and current_data.get("_overlay_active")
    ):
        return False
    previous_identity = previous_data.get("_overlay_snapshot_identity")
    current_identity = current_data.get("_overlay_snapshot_identity")
    current_text = _render_value_text(current.get("screen_text"))
    return bool(
        current_text
        and previous_identity
        and previous_identity == current_identity
    )


def _float_param(
    params: dict,
    *keys: str,
    default: float,
) -> float:
    for key in keys:
        if key not in params:
            continue
        try:
            return float(params[key])
        except (TypeError, ValueError):
            return default
    return default


def _story_transition_drain_deadline(params: dict, default: float = 30.0) -> float:
    followup_timeout = params.get("timeout", default)
    try:
        followup_timeout = float(followup_timeout)
    except (TypeError, ValueError):
        followup_timeout = default
    hard_cap = _float_param(
        params,
        "_story_transition_hard_timeout",
        "story_transition_hard_timeout",
        default=_STORY_TRANSITION_DRAIN_HARD_CAP_SECONDS,
    )
    deadline = time.time() + min(
        max(0.1, followup_timeout),
        max(0.1, hard_cap),
    )
    result_deadline = params.get("_result_deadline")
    if isinstance(result_deadline, (int, float)):
        deadline = min(deadline, float(result_deadline))
    return deadline


def _story_transition_idle_deadline(params: dict, hard_deadline: float) -> float:
    idle_seconds = _float_param(
        params,
        "_story_transition_idle_timeout",
        "story_transition_idle_timeout",
        default=_STORY_TRANSITION_DRAIN_IDLE_SECONDS,
    )
    if idle_seconds <= 0:
        return hard_deadline
    return min(
        hard_deadline,
        time.time() + idle_seconds,
    )


def _story_transition_wait_params(
    params: dict,
    hard_deadline: float,
    idle_deadline: float,
) -> dict:
    remaining = min(hard_deadline, idle_deadline) - time.time()
    followup_params = {
        "timeout": min(
            _STORY_TRANSITION_DRAIN_CHUNK_SECONDS,
            max(0.1, remaining),
        ),
        "_min_wait": 1,
    }
    if isinstance(params.get("_result_deadline"), (int, float)):
        followup_params["_result_deadline"] = params["_result_deadline"]
    if "format" in params:
        followup_params["format"] = params["format"]
    if params.get("_ordinary_only"):
        followup_params["_ordinary_only"] = True
        if params.get("_ordinary_action_id") is not None:
            followup_params["_ordinary_action_id"] = params[
                "_ordinary_action_id"]
    return followup_params


def _clamp_chunk_to_handback(followup_params: dict, settle: Any) -> None:
    """Never let a drain's poll chunk overshoot the story hand-back.

    The drains consult the verdict once per chunk, and their chunk is eight
    seconds.  Without this the hand-back could only be NOTICED up to a chunk
    late, which is the second half of fleet R63's "20 s constant, ~32 s
    observed": the loop that owns the bound polls in short chunks, but the
    drains that run before it do not.
    """
    handback_at = getattr(settle, "story_handback_at", None) if settle else None
    if handback_at is None:
        return
    followup_params["timeout"] = min(
        followup_params.get("timeout", _STORY_TRANSITION_DRAIN_CHUNK_SECONDS),
        max(0.1, handback_at - time.time()),
    )

# ---------------------------------------------------------------------------
# One act-settle policy
# ---------------------------------------------------------------------------
# handle_act accreted five independent notions of "the act is done": the
# bridge's structural settle on the scoped receipt, a 3/5/8-second post-action
# timer, the story-entry drain, the decision/story-gap drains, and the overlay
# closure fence.  Each was added for a real fleet defect and none of them could
# see the others, so an overlay toggle could satisfy four of them and still
# wait out the caller's entire budget (fleet R61: 130/854 acts at exactly
# 60.0 s).  Everything below answers that one question from NAMED EVIDENCE:
#
#   E1 bridge_settled      the act's scoped receipt is settled -- the bridge
#                          observed a structural outcome boundary (new request
#                          id, changed screen signature, or game end) plus its
#                          settle grace of quiet.
#   E2 post_action_sample  a game_state sample provably taken AFTER the act
#                          (shim source coordinates, or the bridge's event
#                          counter).  This is what the 3/5/8-second timers were
#                          standing in for before events carried provenance.
#   E3 decision            a new pending request (id != the acted request id).
#   E4 surface_changed     rendered visible signature != the pre-act snapshot.
#   E5 story_flowing       displayable output within the last QUIET seconds.
#   E6 terminal            game ended / title screen.
#
#   DONE            E1 and (E3 or E4 or E6)
#   DONE            E1 and E2 and not E5      (quiet after a post-action
#                                              sample: nothing more is coming)
#   STORY_HANDBACK  E1 and E5 continuously for 20 s with no E3
#   CONTINUE        otherwise, while the act still expects an outcome
#
# The verdict is a pure function of an evidence record; _ActSettleObserver is
# the only thing that talks to the bridge, and the drains consult the verdict
# rather than running their own independent exits.

class _ActSettleObserver:
    """Collects the evidence the act-settle policy runs on.

    One instance per act.  It owns the only bridge read the policy needs (the
    post-action game_state sample) and the only cross-round state (when story
    was last seen, how long E1 and E5 have held together).
    """

    # Never sample the bridge faster than this while waiting for E2.
    _SAMPLE_INTERVAL_SECONDS = 0.5

    def __init__(
        self,
        ctx: "HandlerContext",
        *,
        pre_visible_sig: tuple | None,
        story_entry: bool,
        rescrape_expected: bool,
        acted_request_id: str | None = None,
        pre_pending_id: str | None = None,
        pre_act_seq: int | None = None,
        allow_empty_probe: bool = False,
        allow_derived_terminal: bool = False,
        issued_at: float | None = None,
    ) -> None:
        self._ctx = ctx
        # The story hand-back's anchor: WHEN THE ACT WAS ISSUED, so the bound
        # is wall time the caller can size against.  Defaults to now for
        # callers with no act behind them (tests, direct use).
        self._issued_at = (
            float(issued_at) if isinstance(issued_at, (int, float))
            else time.time()
        )
        self._pre_visible_sig = pre_visible_sig
        self._story_entry = bool(story_entry)
        self._rescrape_expected = bool(rescrape_expected)
        self._stale_request_ids = {
            request_id
            for request_id in (acted_request_id, pre_pending_id)
            if request_id
        }
        # The last game_state seq observed BEFORE submission.  Anything beyond
        # it is a sample the bridge published after we read, which combined
        # with E1 is the weaker of the two proofs and the fallback when the
        # shim stamped no source coordinates.
        self._admission_seq = (
            pre_act_seq if type(pre_act_seq) is int and pre_act_seq > 0
            else None
        )
        self._source_id: str | None = None
        self._source_seq: int | None = None
        self._allow_derived_terminal = bool(allow_derived_terminal)
        self._empty_probe_rounds = 2 if allow_empty_probe else 0
        self._settled = False
        self._applied = False
        self._pre_gameplay_since: float | None = None
        self._post_action_sample = False
        self._last_sample_at = 0.0
        self._story_seen_at: float | None = None
        self._story_signature: tuple | None = None
        self._settled_story_since: float | None = None
        self.verdict = _ACT_SETTLE_CONTINUE
        self.positive = False
        self.story_handback = False

    # -- inputs ---------------------------------------------------------

    def note_transaction(self, transaction: Any) -> None:
        """Record the scoped receipt's settle state and acceptance stamp."""
        if not isinstance(transaction, dict):
            return
        state = transaction.get("transaction_state")
        if state == "settled":
            self._settled = True
        if state in {"applied", "settled"} or transaction.get("admission_open"):
            # E1': the shim ran the click.  The bridge re-stamps the record at
            # apply time and only calls it settled after its own post-action
            # quiet, so this is strictly earlier than E1 -- and it is the only
            # receipt evidence available while a story burst is running.
            self._applied = True
        source_id = transaction.get("_source_id")
        source_seq = transaction.get("_source_seq")
        if (
            isinstance(source_id, str)
            and source_id
            and type(source_seq) is int
        ):
            # The bridge re-stamps the record when the act APPLIES, so the
            # latest coordinates it reports are the strongest anchor there is.
            self._source_id = source_id
            self._source_seq = source_seq

    def note_empty_probe(self, produced_output: bool) -> None:
        """Bound the 'the action has not published anything yet' window."""
        if produced_output:
            self._empty_probe_rounds = 0
        elif self._empty_probe_rounds:
            self._empty_probe_rounds -= 1

    @property
    def anchored(self) -> bool:
        """True when E2 can be decided at all."""
        return bool(
            (self._source_id is not None and type(self._source_seq) is int)
            or type(self._admission_seq) is int
        )

    # -- evidence -------------------------------------------------------

    def _sample_is_post_action(self, now: float) -> bool:
        if self._post_action_sample or not self.anchored:
            return self._post_action_sample
        if now - self._last_sample_at < self._SAMPLE_INTERVAL_SECONDS:
            return False
        self._last_sample_at = now
        try:
            sample = _call_with_timeout(self._ctx.client.game_state, 1.0)
        except Exception:
            sample = None
        if _post_action_sample_is_after(
            sample,
            source_id=self._source_id,
            source_seq=self._source_seq,
            admission_seq=self._admission_seq,
        ):
            self._post_action_sample = True
        return self._post_action_sample

    def _pre_gameplay(self, now: float) -> bool:
        """Is the run still in the bridge's own pre-gameplay lull?

        Read from the ``gameplay_seen`` flag every ``/state`` read already
        carries, so this costs no extra bridge traffic.  Bounded: a run that
        never publishes gameplay must not hold an act open past
        ``_ACT_PRE_GAMEPLAY_WAIT_SECONDS``.
        """
        if getattr(self._ctx.client, "_last_gameplay_seen", None) is not False:
            self._pre_gameplay_since = None
            return False
        if self._pre_gameplay_since is None:
            self._pre_gameplay_since = now
        return now - self._pre_gameplay_since < _ACT_PRE_GAMEPLAY_WAIT_SECONDS

    def evidence(
        self, rendered: Any, *, now: float | None = None,
    ) -> _ActSettleEvidence:
        now = time.time() if now is None else now
        rendered = rendered if isinstance(rendered, dict) else {}
        # E5 is "displayable output ARRIVED recently", never "the render I am
        # holding contains story".  Judging the same render twice must not
        # refresh the clock, or a single narration line would keep the act
        # open forever.
        if _rendered_has_story_text(rendered):
            story_signature = (
                rendered.get("text") or "",
                tuple(repr(item) for item in (rendered.get("story") or [])),
                rendered.get("screen_text") or "",
            )
            if story_signature != self._story_signature:
                self._story_signature = story_signature
                self._story_seen_at = now
        story_flowing = bool(
            self._story_seen_at is not None
            and now - self._story_seen_at <= _ACT_SETTLE_QUIET_SECONDS
        )
        pending_id = _pending_request_id(rendered)
        evidence = _ActSettleEvidence(
            bridge_settled=self._settled,
            post_action_sample=self._sample_is_post_action(now),
            decision=bool(rendered.get("pending")) and (
                not pending_id or pending_id not in self._stale_request_ids
            ),
            surface_changed=_rendered_surface_left_pre_act_screen(
                rendered, self._pre_visible_sig),
            story_flowing=story_flowing,
            terminal=bool(
                rendered.get("ended")
                or rendered.get("_raw_wait_ended")
                or rendered.get("_title_boundary")
            ),
            story_boundary=_story_tail_has_boundary(
                rendered,
                allow_derived_terminal=self._allow_derived_terminal,
            ),
            action_applied=self._applied or self._settled,
            pending_surface=bool(rendered.get("pending")),
            pre_gameplay=self._pre_gameplay(now),
            story_entry=self._story_entry,
            rescrape_expected=self._rescrape_expected,
            empty_probe_pending=self._empty_probe_rounds > 0,
        )
        if evidence.action_applied and evidence.story_flowing:
            if self._settled_story_since is None:
                # Only "is the clock running" -- E1' (the applied receipt, not
                # E1: the bridge cannot settle while the burst this rule
                # exists for is still emitting) together with E5.  How much
                # time has passed is measured from the ACT's issue below, so
                # the receipt's own scoped drain cannot be added on top of the
                # constant (fleet R63: hand-backs at ~32 s against 20).
                self._settled_story_since = self._story_seen_at or now
        else:
            self._settled_story_since = None
        if self._settled_story_since is not None:
            evidence.settled_story_seconds = now - self._issued_at
        return evidence

    def judge(self, rendered: Any, *, now: float | None = None) -> str:
        """Evaluate the policy against the currently rendered output."""
        evidence = self.evidence(rendered, now=now)
        positive = _act_settle_positive_verdict(evidence)
        self.positive = positive is not None
        self.verdict = _act_settle_verdict(evidence)
        if self.verdict == _ACT_SETTLE_STORY_HANDBACK:
            self.story_handback = True
        return self.verdict

    @property
    def story_handback_at(self) -> float | None:
        """When the story handback becomes due, if E1' and E5 hold now."""
        if self._settled_story_since is None:
            return None
        return self._issued_at + _ACT_STORY_HANDBACK_SECONDS

    @property
    def handback_clock_running(self) -> bool:
        """Is the act accumulating handback time right now?

        The settle loop polls in short chunks while this holds, so the
        handback lands near its own constant instead of a chunk boundary.
        """
        return self._settled_story_since is not None

    @property
    def preempts(self) -> bool:
        """May the last verdict cut a story drain short?

        Only positive evidence may: a drain already knows when it expects
        nothing more, so letting "not awaiting" stop it would just move its
        own stop condition somewhere less tested.
        """
        return self.positive and self.verdict != _ACT_SETTLE_CONTINUE


def _settle_act_until_verdict(
    ctx: HandlerContext,
    result: dict,
    params: dict,
    wait_result: dict,
    settle: _ActSettleObserver,
) -> dict:
    """Poll until the one settle policy says the act is done.

    This replaces the post-action min-wait timer and the story-entry drain's
    ad-hoc exits: nothing here decides on its own that an act has landed, it
    only feeds fresh renders to the verdict until one of the DONE rules holds,
    the story is handed back, or the caller's deadline expires.
    """
    current = wait_result
    if settle.judge(current) != _ACT_SETTLE_CONTINUE:
        return current
    deadline = _act_settle_deadline(params)
    while time.time() < deadline:
        chunk = min(
            (
                _ACT_SETTLE_HANDBACK_POLL_CHUNK_SECONDS
                if settle.handback_clock_running
                else _ACT_SETTLE_POLL_CHUNK_SECONDS
            ),
            max(0.1, deadline - time.time()),
        )
        handback_at = settle.story_handback_at
        if handback_at is not None:
            # Do not let a five-second poll overshoot the handback: the point
            # of the handback is a predictable return, not a rounded one.
            chunk = min(chunk, max(0.1, handback_at - time.time()))
        followup_params = {
            "timeout": chunk,
            "_min_wait": 1,
        }
        if isinstance(params.get("_result_deadline"), (int, float)):
            followup_params["_result_deadline"] = params["_result_deadline"]
        if "format" in params:
            followup_params["format"] = params["format"]
        if params.get("_ordinary_only"):
            followup_params["_ordinary_only"] = True
            if params.get("_ordinary_action_id") is not None:
                followup_params["_ordinary_action_id"] = params[
                    "_ordinary_action_id"]
        followup_result = handle_wait(ctx, followup_params)
        settle.note_transaction(
            _transaction_view(
                ((followup_result.get("_data") or {}).get("transaction"))
                or followup_result.get("transaction")
            )
        )
        meaningful = _meaningful_rendered_output(followup_result)
        settle.note_empty_probe(meaningful)
        if meaningful and (
            _visible_output_signature(followup_result)
            != _visible_output_signature(current)
        ):
            result["wait"] = followup_result
            _promote_wait_output_preserving_story(result, followup_result)
            current = followup_result
        else:
            _absorb_unpromoted_wait_story(result, followup_result)
        if settle.judge(current) != _ACT_SETTLE_CONTINUE:
            break
        # handle_wait can answer instantly on a quiet bridge; keep the loop
        # off a hot spin without extending the caller's budget.
        time.sleep(min(0.25, max(0.0, deadline - time.time())))
    return current

def _drain_story_entry_wait_after_action(
    ctx: HandlerContext,
    result: dict,
    params: dict,
    wait_result: dict,
    settle: "_ActSettleObserver | None" = None,
) -> dict:
    """Keep story-entry button acts waiting across transition-only batches.

    Subordinate to the settle policy: when *settle* is supplied this loop stops
    the moment the verdict leaves CONTINUE, so its own idle/hard caps are a
    floor on how long it may run, never a reason to keep running.
    """
    if (
        not _button_action_is_story_entry(result)
        or not _rendered_has_story_text(wait_result)
        or wait_result.get("pending")
        or wait_result.get("ended")
    ):
        return wait_result

    deadline = _story_transition_drain_deadline(params)
    idle_deadline = _story_transition_idle_deadline(params, deadline)
    current = wait_result
    current_sig = _visible_output_signature(current)
    while time.time() < deadline and time.time() < idle_deadline:
        followup_params = _story_transition_wait_params(
            params,
            deadline,
            idle_deadline,
        )
        _clamp_chunk_to_handback(followup_params, settle)
        followup_result = handle_wait(ctx, followup_params)
        followup_meaningful = _meaningful_rendered_output(followup_result)
        followup_actionable = bool(
            followup_result.get("pending")
            or followup_result.get("buttons")
            or followup_result.get("ended")
        )
        if followup_meaningful:
            followup_sig = _visible_output_signature(followup_result)
            if followup_sig != current_sig:
                _promote_wait_output_preserving_story(result, followup_result)
                current = followup_result
                current_sig = followup_sig
                idle_deadline = _story_transition_idle_deadline(params, deadline)
            else:
                _absorb_unpromoted_wait_story(result, followup_result)
            if followup_actionable and not _rendered_has_story_text(followup_result):
                break
        # Non-meaningful transition batches can sit between story lines and
        # the final actionable end screen; keep draining until the bounded
        # idle window expires or the hard story-entry cap is reached.
        if followup_actionable and not _rendered_has_story_text(followup_result):
            break
        if settle is not None:
            settle.judge(current)
            if settle.preempts:
                break
    return current


def _has_actionable_rendered_surface(rendered: dict) -> bool:
    return bool(rendered.get("ended") or _has_actionable_rendered_state(rendered))


def _story_tail_has_boundary(
    rendered: dict,
    *,
    allow_derived_terminal: bool = False,
) -> bool:
    """True when a story-tail drain reached a real presentation boundary."""
    if (
        rendered.get("_raw_wait_ended")
        or rendered.get("_title_boundary")
        or rendered.get("_foreign_action_boundary")
    ):
        return True
    if _has_actionable_rendered_state(rendered):
        return True
    return bool(rendered.get("ended") and not allow_derived_terminal)


def _drain_story_gap_after_choice_action(
    ctx: HandlerContext,
    result: dict,
    params: dict,
    wait_result: dict,
    settle: "_ActSettleObserver | None" = None,
) -> dict:
    """Continue after story-only action transitions until a decision appears.

    Subordinate to the settle policy in the same way as the story-entry drain:
    with *settle* supplied, the verdict may end this loop before its own idle
    or hard cap does.
    """
    allow_empty_start = bool(params.get("_allow_empty_story_tail"))
    allow_derived_terminal = bool(
        params.get("_allow_derived_terminal_tail"))
    if (
        (not allow_empty_start and not _rendered_has_story_text(wait_result))
        or _story_tail_has_boundary(
            wait_result,
            allow_derived_terminal=allow_derived_terminal,
        )
    ):
        return wait_result

    deadline = _story_transition_drain_deadline(params)
    idle_deadline = _story_transition_idle_deadline(params, deadline)
    current = wait_result
    current_sig = _visible_output_signature(current)
    empty_probe_rounds = 2 if (
        allow_empty_start and not _rendered_has_story_text(wait_result)
    ) else 0
    while time.time() < deadline and time.time() < idle_deadline:
        followup_params = _story_transition_wait_params(
            params,
            deadline,
            idle_deadline,
        )
        _clamp_chunk_to_handback(followup_params, settle)
        followup_result = handle_wait(ctx, followup_params)
        followup_sig = _visible_output_signature(followup_result)
        followup_boundary = _story_tail_has_boundary(
            followup_result,
            allow_derived_terminal=allow_derived_terminal,
        )
        if (
            (_meaningful_rendered_output(followup_result) or followup_boundary)
            and (followup_sig != current_sig or followup_boundary)
        ):
            _promote_wait_output_preserving_story(result, followup_result)
            current = followup_result
            current_sig = followup_sig
            idle_deadline = _story_transition_idle_deadline(params, deadline)
        else:
            _absorb_unpromoted_wait_story(result, followup_result)
        if empty_probe_rounds:
            if _meaningful_rendered_output(followup_result) or followup_boundary:
                empty_probe_rounds = 0
            else:
                empty_probe_rounds -= 1
                if empty_probe_rounds == 0:
                    # Two ordinary wait chunks give an action up to sixteen
                    # seconds to publish its first presentation. Repeated
                    # immediate empty responses must not spin through the
                    # entire 30-second story-transition idle window.
                    break
        if settle is not None:
            settle.note_empty_probe(
                _meaningful_rendered_output(followup_result)
                or followup_boundary
            )
        if followup_boundary:
            break
        if settle is not None:
            settle.judge(current)
            if settle.preempts:
                break
    return current


def render_tool_result_text(result: dict) -> str:
    """Join rendered handler sections into the text agents expect.

    error and warning lead the output: a swallowed error renders as
    "(no new events)" and a dropped recovery warning tells the agent
    its act succeeded cleanly when it may not have executed at all —
    both are silent-failure modes.
    """
    sections = []
    err = result.get("error")
    if err:
        # Coerce non-string errors (dict/exception) rather than dropping
        # them into "(no new events)" — never swallow a failure.
        sections.append("✗ ERROR: " + (err if isinstance(err, str) else str(err)))
    warning = result.get("warning")
    if warning:
        # A warning is guidance for a normal condition ("story is still
        # arriving"); the alarm glyph stays reserved for game errors.
        sections.append("Note: " + (warning if isinstance(warning, str) else str(warning)))
    # A crashed game is not story: it leads, like error/warning, because
    # every later section (buttons, stats) looks normal on the exception
    # screen and an agent cannot tell the difference from them alone.
    anomaly_note = result.get("anomaly_note")
    if anomaly_note:
        sections.append(
            "⚠ " + (anomaly_note if isinstance(anomaly_note, str)
                    else str(anomaly_note)))
    render_sections = _story_render_sections(result)
    if result.get(_STORY_RENDER_SECTIONS_KEY) and render_sections:
        sections.append(_render_story_section_plan(render_sections))
        story_keys = ()
    else:
        story_keys = ("screen_text", "text") if result.get(
            _SCREEN_TEXT_BEFORE_STORY_KEY
        ) else ("text", "screen_text")
    pending_text = result.get("pending")
    pending_text = pending_text if isinstance(pending_text, str) else ""
    for key in (
        *story_keys, "status", "pending", "overlay_note", "buttons", "brief",
    ):
        val = result.get(key)
        if val and isinstance(val, str):
            # ``(no new events)`` is a fallback sentinel, not an event. If
            # another surface contributes output, including a story render
            # plan or footer, never present the sentinel beside it. When it
            # is the only value, the empty section list below still returns
            # the same sentinel.
            if val.strip() == "(no new events)":
                continue
            # Choice rendering includes categorized trailing actions inside
            # the pending block. A late state merge can also retain the same
            # formatted button block as a top-level section; render it once,
            # based on the complete presentation block rather than raw labels.
            if (
                key == "buttons"
                and val.strip()
                and val.strip() in pending_text
            ):
                continue
            sections.append(val)
    if result.get("ended") and not result.get("_defer_ended_banner"):
        sections.append("--- GAME ENDED ---")
    footer = result.get("_footer")
    if footer and isinstance(footer, str):
        sections.append(footer)
    if sections:
        return "\n\n".join(sections)
    return "(no new events)"


def strip_internal_result_fields(result: dict) -> dict:
    """Remove diagnostic/private keys from agent-facing tool output."""
    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: scrub(v)
                for k, v in value.items()
                if not k.startswith("_") or k == "_footer"
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return scrub(result)


def _result_succeeded(result: dict) -> bool:
    transaction_state = result.get("transaction_state")
    if transaction_state:
        return transaction_state not in {"failed", "rejected"}
    return bool(result.get("success", result.get("ok")))


def _renpy_exception_anomaly(data: Any) -> dict | None:
    """Return a bridge anomaly when it looks like a Ren'Py exception."""
    if not isinstance(data, dict):
        return None
    anomaly = data.get("anomaly")
    if not isinstance(anomaly, dict):
        return None
    kind = str(anomaly.get("type") or anomaly.get("kind") or "").lower()
    message = str(anomaly.get("message") or anomaly.get("summary") or "").lower()
    if (
        kind == "renpy_exception"
        or "ren'py exception" in message
        or "renpy exception" in message
    ):
        return anomaly
    return None


def _append_renpy_exception_hint_on_failed_act(
    ctx: HandlerContext,
    result: dict,
    *,
    deadline: float | None = None,
) -> dict:
    """Annotate a failed act when the bridge reports a Ren'Py exception."""
    if _result_succeeded(result):
        return result

    anomaly = None
    for method_name in ("state", "status"):
        remaining = deadline - time.time() if deadline is not None else 3.0
        if remaining <= 0:
            break
        method = getattr(ctx.client, method_name, None)
        if not callable(method):
            continue
        try:
            anomaly = _renpy_exception_anomaly(
                _call_with_timeout(method, min(3.0, remaining)) or {})
        except Exception:
            anomaly = None
        if anomaly:
            break
    if not anomaly:
        return result

    current_error = str(result.get("error") or "act failed")
    lowered = current_error.lower()
    if "ren'py exception" not in lowered and "renpy exception" not in lowered:
        message = str(
            anomaly.get("message")
            or anomaly.get("summary")
            or "Ren'Py exception reported by the bridge"
        ).strip()
        result["error"] = (
            f"{current_error} (Ren'Py exception detected: {message}. "
            "The game may be on the exception screen; inspect state/anomaly "
            "or reload/restart before retrying.)"
        )
    result["_renpy_exception_anomaly"] = anomaly
    return result


def _button_action_uses_wait_path(result: dict) -> bool:
    """Return True when a button is expected to enter story/script flow."""
    resolved_as = result.get("resolved_as", "choice")
    if resolved_as != "button":
        return True
    if result.get("wait_after_action"):
        return True
    if _button_action_is_screen_only(result):
        return False
    interaction_type = result.get("interaction_type", "")
    if interaction_type == "shop":
        return False
    if interaction_type not in ("nav", "info"):
        return True
    label = str(result.get("label") or result.get("matched") or "").strip().lower()
    # Main-menu entry points look like navigation, but they execute script and
    # should return the first story/choice instead of a transient screen state.
    return _button_action_is_story_entry(result)


def _button_action_is_screen_only(result: dict) -> bool:
    """Return True for buttons that only hide a screen/modal."""
    label = str(result.get("label") or result.get("matched") or "").strip().lower()
    screen = str(result.get("screen") or "").strip().lower()
    if screen == "map_display" and "close map" in label:
        return True
    action_names = result.get("action_names") or []
    action_strs = result.get("action_strs") or []
    if "Hide" in action_names:
        return True
    return any(str(action).startswith("Hide screen=") for action in action_strs)


def _button_action_is_story_entry(result: dict) -> bool:
    """True for Start-like buttons whose click runs script.

    ``wait_after_action`` is deliberately NOT read here.  That flag means "the
    UI rebuilds after this click, re-scrape the frame" (the Echoes mod sets it
    on the KIT/LOG/MAP panel toggles); reading it as "this is a Start-like
    story entry" is what made a panel toggle sit in the story-entry drain for
    the caller's whole budget -- fleet R61, 130/854 acts at exactly 60.0 s.  It
    now feeds E2's expectation in the settle policy instead, and a mod that
    really means "this enters story flow" says so with ``_story_entry``.
    """
    if result.get("story_entry") or result.get("_story_entry"):
        return True
    label = str(result.get("label") or result.get("matched") or "").strip().lower()
    return label in _STORY_ENTRY_LABELS


def _transient_act_error(error: str) -> bool:
    return (
        "No interaction matching" in error
        or "No active choice request" in error
    )


def _act_transport_failed(result: dict) -> bool:
    """True when an act's POST /command never reached the bridge.

    The client's HTTP layer reports a connection/socket timeout as
    "Connection failed: <reason>" (see BridgeClient._request).  Unlike a
    game-level rejection ("No active choice request", "Invalid choice
    index", ...), a transport failure means the command was never queued —
    the bridge did not answer the submission at all — so the click provably
    did NOT land and the pre-act choice is still pending.  This is the
    silent act no-op the streaming runs hit when the bridge was briefly
    saturated by the screenshot flood: "Connection failed: timed out",
    state still shows the choice pending, and a manual retry succeeds.
    """
    if _result_succeeded(result):
        return False
    err = str(result.get("error", "")).lower()
    return "connection failed" in err


def _numbered_choice_labels(choices: Any) -> list[str]:
    """Normalized labels of the numbered (non-caption/disabled) choices."""
    labels: list[str] = []
    for choice in choices or []:
        if isinstance(choice, dict):
            if (
                choice.get("is_caption")
                or choice.get("caption")
                or choice.get("is_disabled")
                or choice.get("disabled")
            ):
                continue
            label = choice.get("label") or choice.get("caption")
        else:
            label = choice
        label = str(label or "").strip()
        if label:
            labels.append(_normalize_label(label))
    return labels


def _numbered_choices_unchanged(
    read_choices: Any,
    pre_rendered: dict | None,
) -> bool:
    """True when the current numbered choices match what the caller last saw.

    Used only after explicit reissue provenance confirms that a resync rotated
    the request id. It then verifies the numbered options still match what the
    caller rendered (client.last_choices, captured before this act refreshed
    state).
    """
    prev = _numbered_choice_labels(read_choices)
    if not prev:
        # No record of what the caller saw (e.g. the CLI does not persist
        # last_choices across invocations) — fail closed, keep strict drift
        # refusal.
        return False
    curr = _rendered_choice_labels(pre_rendered)
    return bool(curr) and prev == curr


def _retry_act_after_transport_timeout(
    ctx: HandlerContext,
    result: dict,
    *,
    act_target: int | str | None,
    pre_pending_id: str | None,
    invocation: dict | None = None,
    deadline: float | None = None,
) -> dict:
    """Resubmit an act whose POST /command timed out without reaching the bridge.

    Only fires on a transport-level failure (no HTTP response), and only after
    re-confirming the pre-act pending choice is STILL the active decision — so
    a response-lost-in-flight race (the command actually landed) cannot
    double-apply the choice.  This automates the manual retry the agent had to
    perform: "Connection failed: timed out" no-op, choice still pending, retry
    succeeds.
    """
    if not _act_transport_failed(result):
        return result
    if not pre_pending_id or act_target is None:
        return result
    if deadline is not None and time.time() >= deadline:
        return result
    # Reuse the original attempt's nonce so the bridge treats the resubmit as
    # the SAME logical command: if the first POST actually enqueued (only its
    # response was lost), the bridge returns the original ack WITHOUT
    # enqueuing again, so the choice can't double-apply.  Captured before the
    # state() call below (a GET, which never mints a nonce).
    original_nonce = getattr(ctx.client, "_last_command_nonce", None)
    remaining = deadline - time.time() if deadline is not None else 3.0
    if remaining <= 0:
        return result
    try:
        state = _call_with_timeout(ctx.client.state, min(3.0, remaining)) or {}
    except Exception:
        return result
    current_id = (state.get("pending_request") or {}).get("id")
    if not current_id or current_id != pre_pending_id:
        # The pending changed or cleared: the act may have landed after all,
        # or the scene advanced.  Leave it to the advance-recovery path rather
        # than risk a double-act.
        return result
    try:
        remaining = (
            max(0.0, deadline - time.time())
            if deadline is not None else 15.0
        )
        if remaining <= 0:
            return result
        retry = _call_transactional_act(
            ctx.client.act_transaction,
            act_target,
            action_nonce=original_nonce,
            accept_timeout=remaining,
            deadline=deadline,
            invocation={
                **(invocation or {}),
                "attempt_kind": "transport_retry",
            } if invocation else None,
        )
    except Exception:
        return result
    if not isinstance(retry, dict):
        return result
    retry["_retried_after_transport_timeout"] = True
    retry.setdefault("_original_error", str(result.get("error", "")))
    if not _result_succeeded(retry):
        retry["_transport_retry_failed"] = True
    return retry


def _call_transactional_act(
    method: Callable,
    target: object,
    *,
    action_nonce: str | None = None,
    accept_timeout: float,
    deadline: float | None,
    invocation: dict | None = None,
) -> dict:
    """Call ``BridgeClient.act_transaction`` with an absolute deadline."""
    kwargs: dict[str, Any] = {"accept_timeout": accept_timeout}
    if action_nonce is not None:
        kwargs["action_nonce"] = action_nonce
    if deadline is not None:
        kwargs["deadline"] = deadline
    if invocation:
        kwargs["invocation"] = invocation
    return method(target, **kwargs)


def _act_invocation_from_params(
    params: dict, attempt_kind: str = "initial",
) -> dict | None:
    """Return bounded diagnostic provenance supplied by an MCP server."""
    server_id = params.get("_mcp_server_instance_id")
    call_id = params.get("_mcp_call_id")
    if not isinstance(server_id, str) or not isinstance(call_id, str):
        return None
    server_id = server_id[:128]
    call_id = call_id[:128]
    if not server_id or not call_id:
        return None
    original_target = params.get(
        "_mcp_original_target", params.get("target"))
    if not isinstance(original_target, (str, int, float, bool, type(None))):
        original_target = repr(original_target)
    if isinstance(original_target, str):
        original_target = original_target[:512]
    return {
        "server_instance_id": server_id,
        "call_id": call_id,
        "original_target": original_target,
        "attempt_kind": attempt_kind,
    }


def _meaningful_rendered_output(rendered: dict) -> bool:
    if rendered.get("story"):
        return True
    text = str(rendered.get("text") or "").strip()
    if text and text != "(no new events)":
        return True
    if rendered.get("screen_text"):
        return True
    if _has_actionable_rendered_state(rendered):
        return True
    return False


def _rendered_has_story_text(rendered: dict) -> bool:
    text = str(rendered.get("text") or "").strip()
    if rendered.get("story"):
        return True
    return bool(text and text != "(no new events)") or bool(
        rendered.get("screen_text"))


# Private marker on an act result: its rendered text still carries the game's
# own say/narration lines even though a settle pass has already replaced
# result["_data"] with a bare state snapshot.  _promote_wait_output() only
# rewrites its fixed key list, so this survives the promotion chain and lets a
# LATER settle pass know the text it is about to discard is script output.
_SCRIPT_STORY_MARKER = "_carries_script_story"


def _result_carries_script_story(result: dict) -> bool:
    return bool(result.get(_SCRIPT_STORY_MARKER)) or _rendered_has_script_story(
        result)


def _is_co_terminal_older_story_snapshot(
    previous_text: str,
    previous_sections: list[dict],
    new_text: str,
    new_sections: list[dict],
) -> bool:
    """Recognize an older state-history snapshot converging on current story.

    Under a slow settle, reconstructed dialogue history can arrive after the
    transaction output and contain an older opening followed by the same
    current scene tail.  Some bridge paths stamp that snapshot with transport
    sequence metadata, so provenance cannot distinguish it reliably.  The
    chronology can: fresh story cannot precede a substantial tail that was
    already delivered.  Withhold the later story channel while still merging
    its pending and state fields.
    """
    if not previous_text or not new_text:
        return False

    previous_lines = previous_text.splitlines()
    new_lines = new_text.splitlines()
    overlap = 0
    for size in range(min(len(previous_lines), len(new_lines)), 0, -1):
        if previous_lines[-size:] == new_lines[-size:]:
            overlap = size
            break
    if overlap < 3 or overlap == len(new_lines):
        return False
    return sum(len(line.strip()) for line in new_lines[-overlap:]) >= 120


def _rendered_has_script_story(rendered: dict | None) -> bool:
    """True when rendered output carries the game's own say/narration lines.

    ``_rendered_has_story_text`` also answers True for screen scrapes: modal
    and overlay text arrives as ``screen_text`` events that build_wait_data()
    folds into the story list, and that scrape CAN be a stale echo of the
    pre-action screen.  Dialogue/narration events come from the script itself
    (the same lines the transcript keeps), so they are always new output the
    action produced and must never be dropped by a settle/re-fetch pass.
    """
    story = ((rendered or {}).get("_data") or {}).get("story")
    if not isinstance(story, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") in ("dialogue", "narration")
        and item.get("source") != "screen_text"
        and str(item.get("text") or "").strip()
        for item in story
    )


def _visible_output_signature(rendered: dict | None) -> tuple:
    rendered = rendered or {}
    return (
        rendered.get("text") or "",
        tuple(repr(item) for item in (rendered.get("story") or [])),
        rendered.get("screen_text") or "",
        rendered.get("pending") or "",
        rendered.get("buttons") or "",
        rendered.get("brief") or "",
        rendered.get("_footer") or "",
        bool(rendered.get("ended")),
    )


def _rendered_surface_left_pre_act_screen(
    rendered: dict | None,
    pre_visible_sig: tuple | None,
) -> bool:
    """True when *rendered* is a NEW actionable surface, not the pre-act one.

    Screen/overlay transitions (a panel toggle, a modal close, a map switch)
    produce no story text and no new choice request: their whole observable
    outcome is that a different set of controls is now on screen.  The
    story-entry drain below has no other stopping condition, so without this
    an overlay act waits out the caller's entire budget even though the
    successor surface arrived in the first hundred milliseconds (fleet R61:
    130/854 acts at exactly 60.0 s).

    The test is deliberately a DELTA against the pre-act rendered snapshot,
    never "something is on screen": returning the surface the act was aimed at
    would re-present a consumed menu as though it were the successor, which is
    the numeric-act staleness this drain exists to prevent.
    """
    if not isinstance(rendered, dict) or pre_visible_sig is None:
        return False
    if not _meaningful_rendered_output(rendered):
        return False
    if not _has_actionable_rendered_state(rendered):
        return False
    return _visible_output_signature(rendered) != pre_visible_sig


def _rendered_choice_labels(rendered: dict | None) -> list[str]:
    data = (rendered or {}).get("_data") or {}
    pending = data.get("pending") or {}
    labels: list[str] = []
    if isinstance(pending, dict):
        for choice in pending.get("choices") or []:
            if isinstance(choice, dict):
                label = choice.get("label")
            else:
                label = choice
            label = str(label or "").strip()
            if label:
                labels.append(_normalize_label(label))
    if labels:
        return labels
    raw = data.get("_pending_raw") or {}
    for choice in raw.get("choices") or []:
        if isinstance(choice, dict):
            label = choice.get("label") or choice.get("caption")
        else:
            label = choice
        label = str(label or "").strip()
        if label:
            labels.append(_normalize_label(label))
    return labels


def _rendered_button_labels(rendered: dict | None) -> list[str]:
    data = (rendered or {}).get("_data") or {}
    labels: list[str] = []
    for button in data.get("buttons") or []:
        if isinstance(button, dict):
            label = button.get("label")
        else:
            label = button
        label = str(label or "").strip()
        if label:
            labels.append(_normalize_label(label))
    return labels


def _hidden_behind_panel_refusal_text(target: str, panel: str) -> str:
    """Shared wording for both arms of the modal-panel refusal.

    Fleet R66 found the two arms disagreed with each other AND with the
    ``overlay_note``/system-prompt phrase agents were told to expect
    ("Underlying menu hidden behind <panel>"): the known-choice arm said
    "belongs to the menu hidden behind X. Close the panel first, then act
    on the menu."; the unresolved-button arm said "is not a control on the
    open X panel, and the surface behind it is hidden..." — neither
    contained the literal substring the prompt promised. One vocabulary,
    one helper, used by both arms so they can never drift apart again.
    """
    return (
        "Did not act — {!r} is hidden behind {}. Close the panel (act on "
        "CLOSE) or use the panel's own buttons.".format(str(target), panel)
    )


def _modal_overlay_hidden_label_refusal(
    target: str,
    rendered: dict | None,
    *,
    unresolved: bool = False,
) -> dict | None:
    """Refuse a label act aimed at a surface an open modal panel is covering.

    A modal panel is presented as the whole surface: the covered choices were
    reported as hidden, not numbered, so acting on one by name would reach
    through a panel the player cannot see past. Fail closed and name the
    panel instead of silently clicking through it.

    Two shapes of covered target, and only the first can be recognised before
    the ordinary resolution runs:

    * a covered numbered CHOICE, which ``_hidden_menu`` names outright;
    * a covered BUTTON on the underlying surface — KIT, LOG, a location — of
      which the panel frame keeps no record at all, because the shim reports
      only the panel's own controls while it is up.  Nothing here can prove
      such a target exists, so this arm runs under ``unresolved=True`` ONLY,
      at the point where the act is already being refused; it renames that
      refusal, it never creates one.  Fleet R62 saw two of these: an agent
      firing ``act('KIT')`` and ``act('LOG')`` in one batch was told LOG "is
      not in the rendered choices or controls" when LOG was one CLOSE away.

    Only a proven covered choice is described as hidden. An unresolved
    target may instead be a panel control that changed after a selection,
    so that refusal names the current panel without inventing visibility.

    Absent a declared modal overlay — every game that never registers one —
    this is inert.
    """
    data = (rendered or {}).get("_data")
    if not isinstance(data, dict):
        return None
    normalized = _normalize_label(str(target or ""))
    if not normalized:
        return None
    # A panel control that happens to share the covered choice's name is
    # still visible and still actionable; the panel wins.
    if normalized in _rendered_button_labels(rendered):
        return None
    hidden = data.get("_hidden_menu")
    if isinstance(hidden, dict):
        covered = {
            _normalize_label(str(label))
            for label in (hidden.get("labels") or [])
            if str(label).strip()
        }
        if normalized in covered:
            panel = str(hidden.get("panel") or "the panel")
            return {
                "error": _hidden_behind_panel_refusal_text(target, panel),
                "_modal_overlay_hides_target": True,
            }
    if not unresolved or not data.get("_modal_overlay_screens"):
        return None
    panel = str(
        data.get("_modal_overlay_panel")
        or modal_overlay_panel_name(
            list(data.get("_modal_overlay_screens") or []), None)
        or "the panel"
    )
    return {
        "error": (
            "Did not act - {!r} is not among the visible controls in {}. "
            "Use a currently shown panel control; close the panel only if "
            "you intended to act on the underlying scene."
        ).format(target, panel),
        "_target_not_visible_at_act": True,
    }


def _hidden_choice_labels(rendered: dict | None) -> set:
    """Normalized labels of story choices a modal panel currently covers.

    They remain in the shim's raw interaction list — mods keep choices
    "always visible" there for the panel's own hidden-menu bookkeeping —
    even though the player cannot see or act on them while the panel owns
    the surface.  Excluding them from ``_resolve_label_interaction``'s
    candidate pool lets a real, visible panel control win instead of a
    spurious cross-category tie; a target that reaches ONLY one of them
    still resolves to nothing, and ``_modal_overlay_hidden_label_refusal``
    names the panel instead of this function inventing a second refusal.
    """
    data = (rendered or {}).get("_data")
    if not isinstance(data, dict):
        return set()
    hidden = data.get("_hidden_menu")
    if not isinstance(hidden, dict):
        return set()
    return {
        _normalize_label(str(label))
        for label in (hidden.get("labels") or [])
        if str(label).strip()
    }


def _ambiguous_act_candidate_desc(itr: dict) -> str:
    """One human-readable candidate for an ambiguous-match refusal."""
    label = str(itr.get("display_label") or "").strip()
    if len(label) > 60:
        label = label[:57].rstrip() + "..."
    if itr.get("source") == "choice":
        return "choice {!r}".format(label)
    return "button {}".format(label)


def _ambiguous_act_target_refusal(target: object, candidates: list) -> dict:
    """Refuse a label act that reached more than one interaction.

    Fired when an exact normalized match ties across categories (a story
    choice AND a button/control share the same rendered text) or a fuzzy
    match lands on more than one candidate at all.  Fail closed: this
    never clicks anything, and names every candidate so the caller can
    retry unambiguously by number or exact label.
    """
    described = ", ".join(
        _ambiguous_act_candidate_desc(itr)
        for itr in candidates
        if isinstance(itr, dict)
    )
    return {
        "error": (
            "Did not act — {!r} matches more than one thing: {} — "
            "act by number or exact label.".format(str(target), described)
        ),
        "_ambiguous_act_target": True,
    }


def _nothing_actionable_before_act(pre_rendered: dict | None) -> bool:
    """True when the pre-act state showed no pending request and no buttons.

    An act that fails in this situation is not a race with a scene advance
    (there was nothing to act on in the first place), so the transient-error
    rescue paths must not rewrite it into a quiet success — that is the
    silent-act-failure family: the CLI printed the just-arrived prompt with
    exit 0 and runner retry logic never re-submitted the action.

    ``None``/malformed pre-state means "unknown"; callers keep the existing
    rescue behavior then rather than failing acts on missing telemetry.
    """
    if not isinstance(pre_rendered, dict):
        return False
    data = pre_rendered.get("_data")
    if not isinstance(data, dict):
        return False
    if data.get("pending"):
        return False
    if _rendered_button_labels(pre_rendered):
        return False
    return True


def _target_looks_like_auto_continue(target: object) -> bool:
    """Return True for bracketed single-continue labels auto-skip can resolve."""
    label = str(target or "").strip()
    return len(label) >= 3 and label.startswith("[") and label.endswith("]")


def _fail_act_with_no_pending(result: dict, target: object) -> dict:
    """Rewrite a failed act into an explicit did-not-act error."""
    failed = dict(result)
    failed["success"] = False
    failed["ok"] = False
    failed["_no_pending_at_act"] = True
    failed["_original_error"] = str(result.get("error", ""))
    failed["error"] = (
        "Did not act (target {!r}) — but this is an EXPECTED auto-advance "
        "timing quirk, not a real failure: no choice or button was pending "
        "because the game was still advancing (typically through narration) "
        "when the command ran. Just call wait() to let the scene settle, then "
        "act on the choice it surfaces.".format(target)
    )
    return failed


def _target_was_visible_before_act(target: object, rendered: dict | None) -> bool:
    """Return whether a string target matched pre-action visible options."""
    target_str = str(target or "").strip()
    if not target_str or target_str.isdigit():
        return True
    norm = _normalize_label(target_str)
    labels = _rendered_choice_labels(rendered) + _rendered_button_labels(rendered)
    if len(norm) < 3:
        return any(norm == label for label in labels)
    return any(norm == label or norm in label or label.startswith(norm)
               for label in labels)


def _target_was_visible_in_snapshot(
    target: object,
    snapshot: dict | None,
) -> bool:
    """Return whether the caller-bound actionable snapshot exposed a target."""
    target_str = str(target or "").strip()
    if not target_str or target_str.isdigit() or not isinstance(snapshot, dict):
        return False
    norm = _normalize_label(target_str)
    labels: list[str] = []
    for surface in (snapshot.get("request"), snapshot.get("screen")):
        if not isinstance(surface, dict):
            continue
        for items in surface.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                label = (
                    item.get("display_label")
                    or item.get("label")
                    or item.get("text")
                )
                label = str(label or "").strip()
                if label:
                    labels.append(_normalize_label(label))
    if len(norm) < 3:
        return any(norm == label for label in labels)
    return any(norm == label or norm in label or label.startswith(norm)
               for label in labels)


def _target_matches_rendered_choices(target: object, choices: Any) -> bool:
    """Return whether a string target names a caller-bound choice list."""
    target_str = str(target or "").strip()
    if not target_str or target_str.isdigit():
        return False
    norm = _normalize_label(target_str)
    labels = _numbered_choice_labels(choices)
    if len(norm) < 3:
        return any(norm == label for label in labels)
    return any(norm == label or norm in label or label.startswith(norm)
               for label in labels)


def _rendered_has_map_buttons(rendered: dict | None) -> bool:
    data = (rendered or {}).get("_data") or {}
    for button in data.get("buttons") or []:
        if not isinstance(button, dict):
            continue
        marker = " ".join(
            str(button.get(key) or "")
            for key in ("screen", "label", "_category")
        ).lower()
        if "map" in marker:
            return True
    return False


def _rendered_buttons_repeat_pre_choices(
    rendered: dict | None,
    pre_rendered: dict | None,
) -> bool:
    pre_labels = set(_rendered_choice_labels(pre_rendered))
    button_labels = set(_rendered_button_labels(rendered))
    return bool(pre_labels and button_labels and pre_labels <= button_labels)


def _recover_failed_act_after_advance(
    ctx: HandlerContext,
    result: dict,
    pre_rendered: dict | None,
    pre_state_sig: tuple | None,
    params: dict,
) -> dict:
    """Return current output when a transient act error raced a scene advance."""
    if _result_succeeded(result):
        return result
    if pre_state_sig is None:
        return result

    error = str(result.get("error", ""))
    if not _transient_act_error(error):
        return result
    if result.get("_resync_retry_failed"):
        return result
    if (
        "No interaction matching" in error
        and not _target_was_visible_before_act(params.get("target"), pre_rendered)
    ):
        return result

    pre_visible_sig = _visible_output_signature(pre_rendered)
    state_result = None
    try:
        state_result = _wait_for_rendered_state_change(
            ctx,
            pre_state_sig,
            params,
            timeout=_remaining_act_result_timeout(params, 1.5),
            prefer_actionable=True,
            settle_delay=0.3,
        )
    except Exception:
        state_result = None

    # The state render can re-offer the ACTED request while it sits in the
    # bridge's auto-clear grace: run f2-physicist got its resolved 3-option
    # menu back with a pre-spend footer, retried "1", and the retry landed
    # on a different screen by index (2026-08-19). A recovered output whose
    # pending is the request this very act resolved is a stale echo, not a
    # decision — fall through to the wait branch, which verifies against
    # the bridge's active request and drains the real aftermath.
    acted_id = _pending_request_id(pre_rendered)
    if (
        state_result is not None
        and acted_id
        and _pending_id_from_data(state_result.get("_data")) == acted_id
    ):
        state_result = None

    state_changed = (
        state_result is not None
        and _visible_output_signature(state_result) != pre_visible_sig
    )
    state_recovery_available = (
        state_result is not None
        and state_changed
        and _meaningful_rendered_output(state_result)
    )

    try:
        timeout = int(params.get("timeout", 60))
    except Exception:
        timeout = 60
    wait_params = {
        # A changed state already proves the scene advanced. Spend only a
        # short pass draining its queued output; the state snapshot remains a
        # valid fallback. Returning it without this pass strands those events
        # in the ordinary stream, where the next act can inherit and replay
        # them (Echoes' terminal document duplication, 2026-08-22).
        "timeout": (
            1 if state_recovery_available
            else max(1, min(timeout, _TRANSIENT_ACT_RECOVERY_WAIT_CAP))
        ),
        "_min_wait": 0,
    }
    wait_params["timeout"] = _remaining_act_result_timeout(
        params, wait_params["timeout"])
    if wait_params["timeout"] <= 0:
        return result
    if "format" in params:
        wait_params["format"] = params["format"]
    # A failed act owns no story. Its scoped transaction can only replay the
    # failure envelope, while the scene output that raced it lives in the
    # ordinary stream (or under the preceding successful action).
    try:
        wait_result = handle_wait(ctx, wait_params)
    except Exception:
        wait_result = None

    wait_changed = (
        wait_result is not None
        and _visible_output_signature(wait_result) != pre_visible_sig
    )
    if (
        wait_result is not None
        and _meaningful_rendered_output(wait_result)
        and (wait_changed or _rendered_has_story_text(wait_result))
    ):
        recovered = dict(result)
        recovered.pop("error", None)
        recovered["success"] = True
        recovered["ok"] = True
        recovered["_original_error"] = error
        recovered["_recovered_after_advance"] = True
        recovered["_recovery_source"] = "next_wait"
        recovered["wait"] = wait_result
        _promote_wait_output(recovered, wait_result)
        return recovered

    if state_recovery_available:
        recovered = dict(result)
        recovered.pop("error", None)
        recovered["success"] = True
        recovered["ok"] = True
        recovered["_original_error"] = error
        recovered["_recovered_after_advance"] = True
        recovered["_recovery_source"] = "current_state"
        recovered["wait"] = state_result
        _promote_wait_output(recovered, state_result)
        return recovered

    return result


def _retry_failed_act_after_resync(
    ctx: HandlerContext,
    result: dict,
    *,
    target: object,
    act_target: object,
    pre_rendered: dict | None,
    invocation: dict | None = None,
    deadline: float | None = None,
) -> dict:
    """Retry a visible target once after resyncing a lost active choice menu."""
    if _result_succeeded(result):
        return result
    error = str(result.get("error", ""))
    original_nonce = result.get("action_nonce")
    if "No active choice request" not in error:
        return result
    if not _target_was_visible_before_act(target, pre_rendered):
        return result
    if deadline is not None and time.time() >= deadline:
        return result

    resync_result = _run_resync_for_act_retry(ctx, deadline=deadline)
    if not _result_succeeded(resync_result):
        recovered = dict(result)
        recovered["_resync_failed"] = (
            resync_result.get("error")
            or resync_result.get("message")
            or "resync failed"
        )
        return recovered

    pending = getattr(ctx.client, "pending", None)
    if callable(pending):
        remaining = deadline - time.time() if deadline is not None else 3.0
        if remaining <= 0:
            return result
        try:
            _call_with_timeout(pending, min(3.0, remaining))
        except Exception:
            pass

    try:
        remaining = (
            deadline - time.time() if deadline is not None else 15.0
        )
        if remaining <= 0:
            recovered = dict(result)
            recovered["_resync_failed"] = "result timeout expired"
            return recovered
        retry = _call_transactional_act(
            ctx.client.act_transaction,
            act_target,
            accept_timeout=remaining,
            deadline=deadline,
            invocation={
                **(invocation or {}),
                "attempt_kind": "resync_retry",
            } if invocation else None,
        )
    except Exception as exc:
        recovered = dict(result)
        recovered["_resync_failed"] = str(exc)
        return recovered
    if _result_succeeded(retry):
        retry["_resynced_before_act"] = True
        retry.setdefault("_original_error", error)
        retry_nonce = retry.get("action_nonce")
        if original_nonce and retry_nonce and original_nonce != retry_nonce:
            retire_nonce = getattr(
                ctx.client, "_retire_auto_action_nonce", None)
            if callable(retire_nonce):
                retire_nonce(original_nonce, "resync_retry_replaced")
            retry["_recovered_from_action_nonce"] = original_nonce
    else:
        retry["_resync_retry_failed"] = True
    return retry


def _run_resync_for_act_retry(
    ctx: HandlerContext,
    *,
    deadline: float | None = None,
) -> dict:
    """Queue resync and require its post-boundary command_result before retrying."""
    after_seq = _client_event_counter(ctx.client, deadline=deadline)
    if deadline is not None and time.time() >= deadline:
        return {"ok": False, "error": "result timeout expired"}
    nonce = f"act-resync-{time.time():.6f}"
    remaining = deadline - time.time() if deadline is not None else 15.0
    if remaining <= 0:
        return {"ok": False, "error": "result timeout expired"}
    try:
        success, msg = ctx.client._send_command(
            "resync", {"nonce": nonce}, timeout=min(15.0, remaining),
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if not success:
        return {"ok": False, "error": msg}
    try:
        timeout = (
            max(0.0, min(3.0, deadline - time.time()))
            if deadline is not None else 3.0
        )
        if timeout <= 0:
            return {"ok": False, "error": "result timeout expired"}
        resync_result = ctx.client._wait_command_result(
            "resync",
            timeout=timeout,
            after_seq=after_seq,
            match=lambda ev: ev.get("nonce") == nonce,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if not isinstance(resync_result, dict):
        return {"ok": False, "error": "Timed out waiting for resync result"}
    return resync_result


def _client_event_counter(
    client: Any, *, deadline: float | None = None,
) -> int | None:
    """Return the bridge event counter used to reject stale command results."""
    get = getattr(client, "_get", None)
    if callable(get):
        remaining = deadline - time.time() if deadline is not None else 2.0
        if remaining <= 0:
            return None
        try:
            code, state = get("/state", timeout=min(2.0, remaining))
            if code == 200 and isinstance(state, dict):
                return int(state.get("event_counter", 0) or 0)
        except Exception:
            pass
    state_fn = getattr(client, "state", None)
    if callable(state_fn):
        remaining = deadline - time.time() if deadline is not None else 3.0
        if remaining <= 0:
            return None
        try:
            state = _call_with_timeout(state_fn, min(3.0, remaining)) or {}
            return int(state.get("event_counter", 0) or 0)
        except Exception:
            pass
    return None


def _state_signature(rendered: dict) -> tuple:
    """Compact signature for visible/actionable state changes."""
    return rendered_state_signature(rendered)


def _pending_request_id(rendered: dict | None) -> str | None:
    """Return the raw/current pending id from rendered handler output."""
    rendered = rendered or {}
    data = rendered.get("_data") or {}
    raw = data.get("_pending_raw") or rendered.get("_pending_raw") or {}
    pending = data.get("pending") or {}
    if not isinstance(raw, dict):
        raw = {}
    if not isinstance(pending, dict):
        pending = {}
    return raw.get("id") or pending.get("id")


def _formatted_pending_labels(rendered: dict | None) -> list[str]:
    """Extract the numbered labels the text response actually exposed."""
    pending = (rendered or {}).get("pending")
    if not isinstance(pending, str):
        return []
    labels = []
    for line in pending.splitlines():
        number, separator, label = line.strip().partition(":")
        if separator and number.isdigit() and label.strip():
            labels.append(_normalize_label(label.strip()))
    return labels


def _build_decision_state_data(raw: dict) -> dict:
    """Build rendered decision data with its exact actionable fingerprint."""
    data = build_state_data(raw)
    snapshot = actionable_state_snapshot(raw)
    if snapshot is not None:
        data["_actionable_snapshot"] = snapshot
    return data


def _bind_returned_pending(
    ctx: HandlerContext,
    rendered: dict | None,
    *,
    deadline: float | None = None,
    acted_request_id: str | None = None,
) -> None:
    """Bind numeric acts to the pending request actually returned to caller."""
    original_binding = (
        getattr(ctx.client, "last_request_id", None),
        getattr(ctx.client, "last_choices", None),
        getattr(ctx.client, "last_actionable_snapshot", None),
    )
    if (
        isinstance(rendered, dict)
        and rendered.get("_stale_pending_suppressed")
        and not _formatted_pending_labels(rendered)
    ):
        return
    visible_labels = _formatted_pending_labels(rendered)
    candidate = rendered
    seen: set[int] = set()
    bindings: list[tuple[str, dict, dict | None]] = []
    while isinstance(candidate, dict) and id(candidate) not in seen:
        seen.add(id(candidate))
        request_id = _pending_request_id(candidate)
        if request_id:
            data = candidate.get("_data") or {}
            raw = (
                data.get("_pending_raw")
                or candidate.get("_pending_raw")
                or data.get("pending")
                or {}
            )
            snapshot = (
                data.get("_actionable_snapshot")
                or candidate.get("_actionable_snapshot")
            )
            # Composed act results retain their earlier output at the outer
            # level and place a later settle/read in ``wait``. A slower path
            # can therefore expose the consumed request outside while the
            # successor menu the caller actually sees lives inside. Keep
            # walking and let the deepest rendered decision win.
            if (
                not isinstance(snapshot, dict)
                and bindings
                and bindings[-1][0] == request_id
            ):
                # Promotion layers may repeat the same request while omitting
                # private metadata. Preserve provenance only for that exact
                # request; a different deeper request must still fail closed.
                snapshot = bindings[-1][2]
            bindings.append((
                request_id,
                raw if isinstance(raw, dict) else {},
                snapshot if isinstance(snapshot, dict) else None,
            ))
        candidate = candidate.get("wait")
    # Long settle chains are not guaranteed to be chronologically nested:
    # promotion can put the newest decision at the response root while an
    # older diagnostic wait remains below it. Prefer provenance whose raw
    # labels exactly match the numbered menu the caller saw. Falling back to
    # the deepest binding preserves the conservative behavior for responses
    # that do not expose a comparable menu.
    matching_bindings = [
        item for item in bindings
        if visible_labels and _raw_pending_labels(item[1]) == visible_labels
    ]
    binding = (
        matching_bindings[-1]
        if matching_bindings
        else (bindings[-1] if bindings else None)
    )
    consumed_request_id = (
        acted_request_id
        or getattr(ctx.client, "_acted_request_id", None)
    )
    if (
        binding is not None
        and consumed_request_id
        and binding[0] == consumed_request_id
        and (
            not visible_labels
            or _raw_pending_labels(binding[1]) == visible_labels
        )
    ):
        # A successful choice consumes its request. Under load the composed
        # settle response can contain new story beside the bridge's cached
        # copy of that old menu. Never let the stale menu become the numeric
        # binding for the caller's next act: keep the story and require one
        # fresh observation to discover the real successor.
        rendered.pop("pending", None)
        rendered.pop("buttons", None)
        rendered.pop("_pending_raw", None)
        rendered.pop("_actionable_snapshot", None)
        data = rendered.get("_data")
        if isinstance(data, dict):
            data.pop("pending", None)
            data.pop("buttons", None)
            data.pop("_pending_raw", None)
            data.pop("_actionable_snapshot", None)
        transaction = rendered.get("transaction") or {}
        nonce = (
            transaction.get("action_nonce")
            if isinstance(transaction, dict) else None
        ) or rendered.get("action_nonce")
        continuation = (
            f'wait(action_nonce="{nonce}")' if nonce else "wait()"
        )
        rendered["warning"] = (
            "The choice resolved, but its successor menu is still settling. "
            f"Call {continuation} before acting again."
        )
        rendered["_stale_pending_suppressed"] = True
        ctx.client.last_request_id = (
            "__resolved_choice_waiting_for_successor__"
        )
        ctx.client.last_choices = None
        ctx.client.last_actionable_snapshot = None
        return
    returned_snapshot = None
    if binding is None and not visible_labels:
        return
    if binding is not None:
        request_id, raw, snapshot = binding
        ctx.client.last_request_id = request_id
        if isinstance(raw.get("choices"), list):
            choices = raw["choices"]
            ctx.client.last_choices = [
                item.get("label", "") if isinstance(item, dict) else item
                for item in choices
            ]
        returned_snapshot = snapshot if isinstance(snapshot, dict) else None
        # Provenance belongs to this returned request. A prior request's
        # cached action identity must never fill a missing snapshot here.
        ctx.client.last_actionable_snapshot = returned_snapshot

    # Some composed act responses promote only the formatted pending block:
    # their private raw metadata can still describe the consumed menu even
    # though the caller visibly received its successor. Reconcile when the
    # bridge's live menu exactly matches the numbered labels returned to the
    # caller and either the actionable snapshots agree OR the response itself
    # proves its private metadata is stale (its raw labels differ from the
    # labels it rendered). The latter is safe without comparing snapshots:
    # one atomic /state read supplies both the exact live request and its
    # actionable projection. Same-label menus with different actions remain
    # fail-closed because their visible/private labels do not prove drift.
    if visible_labels and (deadline is None or time.time() < deadline):
        previous_binding = (
            ctx.client.last_request_id,
            ctx.client.last_choices,
            ctx.client.last_actionable_snapshot,
        )
        try:
            remaining = (
                3.0 if deadline is None else max(0.0, deadline - time.time())
            )
            live_state = _call_with_timeout(
                ctx.client.state, min(3.0, remaining)
            ) or {}
        except Exception:
            live_state = {}
        if deadline is not None and time.time() >= deadline:
            (
                ctx.client.last_request_id,
                ctx.client.last_choices,
                ctx.client.last_actionable_snapshot,
            ) = original_binding
            return
        live_pending = (
            live_state.get("pending_request")
            or live_state.get("pending")
            or {}
        )
        if not isinstance(live_pending, dict):
            live_pending = {}
        live_choices = []
        for choice in live_pending.get("choices") or []:
            label = (
                choice.get("label") or choice.get("caption")
                if isinstance(choice, dict) else choice
            )
            if str(label or "").strip():
                live_choices.append(str(label).strip())
        live_snapshot = actionable_state_snapshot({
            "pending_request": live_pending,
            "game_state": live_state.get("game_state") or {},
        })
        live_labels = [_normalize_label(label) for label in live_choices]
        returned_labels = _raw_pending_labels(raw) if binding is not None else []
        response_proves_stale_metadata = bool(
            returned_snapshot is not None
            and returned_labels
            and returned_labels != visible_labels
        )
        if (
            visible_labels == live_labels
            and (
                actionable_snapshots_equivalent(
                    returned_snapshot, live_snapshot,
                )
                or response_proves_stale_metadata
            )
        ):
            live_request_id = live_pending.get("id")
            if live_request_id:
                ctx.client.last_request_id = live_request_id
                ctx.client.last_choices = live_choices
                if live_snapshot is not None:
                    ctx.client.last_actionable_snapshot = live_snapshot
                # Repair the response boundary too, not just the client's
                # cache. Downstream composition and diagnostics should see the
                # same request that supplied the visible numbered menu.
                live_data = _build_decision_state_data({
                    "status": "waiting_for_input",
                    "pending_request": live_pending,
                    "game_state": live_state.get("game_state") or {},
                    "screen": live_state.get("screen") or {},
                })
                rendered["_pending_raw"] = live_pending
                if live_snapshot is not None:
                    rendered["_actionable_snapshot"] = live_snapshot
                data = rendered.get("_data")
                if isinstance(data, dict):
                    # This atomic state read supplied the exact request whose
                    # labels were rendered. Its stats are authoritative too;
                    # retaining an earlier transaction footer can otherwise
                    # pair a fresh menu with pre-action values.
                    _merge_decision_data(
                        data,
                        live_data,
                        replace_pending=True,
                        replace_footer=True,
                    )
                return
        # BridgeClient.pending() remembers what it fetched. A mismatch means
        # the visible response and live decision cannot safely be joined, so
        # restore the conservative binding instead of moving onto an unseen
        # menu.
        (
            ctx.client.last_request_id,
            ctx.client.last_choices,
            ctx.client.last_actionable_snapshot,
        ) = previous_binding


def _rendered_decision_binding(rendered: dict | None) -> tuple | None:
    """The decision a response actually put in front of its caller.

    Returns ``(request_id, choices, snapshot)`` for a response that rendered a
    pending decision block whose request can be named, or None.  A decision
    synthesized from live choice interactions carries ``id: ""`` (see
    ``format._pending_from_choice_interactions``) and is deliberately NOT a
    binding: an unnamed request cannot be compared with the one the next act
    reads back, so it must be reconciled with the bridge's own request first
    (``_replace_stale_pending_from_state``).
    """
    if not isinstance(rendered, dict):
        return None
    if not rendered.get("pending"):
        return None
    request_id = _pending_request_id(rendered)
    if not request_id:
        return None
    data = rendered.get("_data")
    if not isinstance(data, dict):
        data = {}
    raw = data.get("_pending_raw") or rendered.get("_pending_raw") or {}
    if not isinstance(raw, dict):
        raw = {}
    choices = raw.get("choices")
    snapshot = (
        data.get("_actionable_snapshot")
        or rendered.get("_actionable_snapshot")
    )
    return (
        request_id,
        list(choices) if isinstance(choices, list) else None,
        snapshot if isinstance(snapshot, dict) else None,
    )


def _remember_rendered_decision(
    ctx: HandlerContext,
    rendered: dict | None,
) -> None:
    """Bind numeric acts to the decision THIS response rendered.

    ``act N`` is a reply to the last numbered list the caller SAW, so every
    response that shows one has to record it.  Before this, only ``handle_act``
    reconciled its own response (``_bind_returned_pending``); ``wait`` and
    ``state`` relied on whatever ``BridgeClient._remember_pending`` happened to
    cache during their internal reads.  That cache is not a render receipt: it
    only moves when a read observes a pending, so a menu that reached the agent
    on a path where no read did — an unseen successor still absent from
    ``/state``, a failed composition read, a menu composed from live
    ``game_state`` rows — left the binding on the PREVIOUS menu.  Fleet R64
    ``echo64-s08`` call113: two waits after the CONVERGENCE.DAT menu resolved,
    the successor was rendered from live choice interactions and the binding
    still named the consumed request, so the numeric guard refused ``act('3')``
    with the OLD menu in ``rendered_choices`` and the new one in
    ``current_choices``.  The identical retry was accepted.

    All three fields move together: a partial update would compare a fresh id
    against a stale snapshot.  Absent choices/snapshot record as None, which
    keeps the guard fail-closed rather than blessing an unrelated surface.
    """
    if getattr(ctx, "_composing_act_response", 0):
        # handle_act's internal settle waits render candidate output that may
        # never reach the caller.  Its composed response is bound once, at the
        # end, by _bind_returned_pending.
        return
    binding = _rendered_decision_binding(rendered)
    if binding is None:
        return
    request_id, choices, snapshot = binding
    if request_id == getattr(ctx.client, "_acted_request_id", None):
        # A request this client already answered is not a decision the caller
        # can reply to, whatever a lagging render still shows.
        return
    ctx.client.last_request_id = request_id
    ctx.client.last_choices = choices
    ctx.client.last_actionable_snapshot = snapshot


def _clear_settling_warning_for_actionable_decision(result: dict) -> None:
    """Reconcile recovery guidance with a proven successor decision.

    A visible, provenance-checked menu proves what the next decision will be;
    it does not by itself prove that the bridge has reopened act admission.
    Keep explicit nonce guidance while the prior transaction still blocks, and
    clear it only once the receipt is terminal or admission is open.
    """
    warning = result.get("warning")
    recognized_warning = isinstance(warning, str) and warning.startswith((
        "The action was accepted, but its result is still settling.",
        "Available output is shown; transaction confirmation is still settling.",
        "The choice resolved, but its successor menu is still settling.",
        "The successor menu is visible, but the prior action is still settling.",
    ))
    transaction = result.get("transaction")
    if isinstance(transaction, dict):
        transaction["successor_actionable"] = True
        if (
            transaction.get("transaction_state")
            not in {"settled", "failed", "rejected"}
            and not transaction.get("admission_open")
        ):
            nonce = transaction.get("action_nonce") or result.get("action_nonce")
            if nonce:
                result["warning"] = (
                    "The successor menu is visible, but the prior action is "
                    "still settling. "
                    f'Call wait(action_nonce="{nonce}") before acting again.'
                )
            elif not recognized_warning:
                result["warning"] = (
                    "The successor menu is visible, but the prior action is "
                    "still settling. Call wait() before acting again."
                )
            result.pop("_stale_pending_suppressed", None)
            return
    if recognized_warning:
        result.pop("warning", None)
    result.pop("_stale_pending_suppressed", None)


def _mark_story_only_act_result_continues(result: dict) -> None:
    """An act that returns story and NOTHING to answer is a hand-back.

    ``story_continues`` used to be stamped only by the two settle-layer sites
    that KNOW the hand-back bound fired.  Every other way an act can return
    mid-scene — the numeric-target recovery drain that never dispatched a
    click, a quiet verdict taken during a lull in a burst — produced the same
    agent-visible surface (story text, no numbered menu, no ending) with no
    marker at all.  Fleet R64: 7 of 857 acts, 6 agents, indistinguishable from
    a settled scene; the marker is precisely what the guidance tells agents to
    key on.

    The test is what the AGENT can see, not how the act got here: if the
    response carries story and offers nothing to act on, the only next move is
    ``wait()``, so say so.  Deliberately silent for the shapes that already
    have their own wording:

    * an error or a terminal-failed receipt — the error leads the render;
    * ``result_timeout_reached`` — ``_mark_act_result_timeout`` writes the
      opposite promise there ("that wait may contain no additional story");
    * a scene-unchanged notice — "nothing happened" and "story is still
      arriving" cannot both be true;
    * an ending, or any rendered decision (pending or buttons), which IS
      something to answer.
    """
    if result.get("story_continues"):
        return
    if result.get("error") or not _result_succeeded(result):
        return
    if result.get("result_timeout_reached") or result.get("_scene_unchanged"):
        return
    if result.get("ended") or result.get("_defer_ended_banner"):
        return
    data = result.get("_data")
    data = data if isinstance(data, dict) else {}
    if (
        result.get("pending")
        or result.get("buttons")
        or data.get("pending")
        or data.get("buttons")
    ):
        return
    if not _rendered_has_story_text(result):
        return
    _mark_act_story_continues(result)


def finalize_act_presentation(
    result: dict, *, actionable_decision: bool,
) -> dict:
    """Apply every receipt-wording rule an act result is subject to, once.

    The three passes are order-dependent and were previously spelled inline at
    the end of ``handle_act``:

    1. A proven successor decision reconciles (or re-words) the settling
       warning, and stamps ``successor_actionable`` on the transaction.
    2. Whatever warning survives, a still-blocking transaction must name the
       repeatable wait — but only if step 1 did not already name one, which is
       why recovery guidance runs second and checks for ``wait(`` in the text.
    3. Terminal failure overrides the optimistic admission wording last, so a
       failed receipt cannot keep claiming ``ok``/``submitted``/``pending``
       under a settling warning written by the earlier passes.

    The wait path's own receipt passes (``_surface_scoped_wait_rejection`` and
    ``_ensure_empty_scoped_wait_receipt``) are deliberately NOT folded in:
    both are gated on an explicit ``action_nonce`` wait parameter that an act
    result does not carry, so running them here would add wording act results
    have never had.  ``_mark_act_result_timeout`` stays where it is for the
    same reason: it runs before the deferred-resolution hook, which may
    replace the result dict this pass would then be shaping.
    """
    if actionable_decision:
        _clear_settling_warning_for_actionable_decision(result)
    # Before the recovery guidance, exactly like the settle layer's own
    # hand-back: naming wait() here is what suppresses the redundant second
    # "call wait(...)" sentence, and both paths must read identically.
    _mark_story_only_act_result_continues(result)
    _ensure_transaction_recovery_guidance(result)
    _normalize_terminal_act_result(result)
    return result


def _ensure_transaction_recovery_guidance(result: dict) -> None:
    """Name the repeatable wait whenever a transaction remains blocking."""
    transaction = result.get("transaction")
    if not isinstance(transaction, dict):
        return
    if transaction.get("transaction_state") in {"settled", "failed", "rejected"}:
        return
    if transaction.get("admission_open"):
        return
    state = transaction.get("transaction_state")
    if not (
        state in {"accepted", "applied", "acceptance_unknown"}
        or transaction.get("pending")
        or transaction.get("admission_pending")
        or result.get("result_timeout_reached")
    ):
        return
    if isinstance(result.get("warning"), str) and "wait(" in result["warning"]:
        return
    nonce = transaction.get("action_nonce") or result.get("action_nonce")
    continuation = f'wait(action_nonce="{nonce}")' if nonce else "wait()"
    result["warning"] = (
        "The transaction is still settling. "
        f"Call {continuation} again to continue it."
    )


def _drain_stale_pending_followup(
    ctx: HandlerContext,
    rendered: dict | None,
    params: dict,
    *,
    timeout: float,
) -> dict | None:
    """Use the shared client drain for post-button stale pending recovery."""
    data = (rendered or {}).get("_data") or {}
    raw = data.get("_pending_raw") or {}
    if not raw:
        return None
    if not hasattr(ctx.client, "poll") or not hasattr(ctx.client, "pending"):
        return None

    old_id = raw.get("id")
    try:
        fresh = drain_stale_pending_request(
            ctx.client,
            raw,
            timeout=max(0.0, timeout),
        )
    except Exception:
        return None
    fresh_id = fresh.get("id") if isinstance(fresh, dict) else None
    if not fresh_id or fresh_id == old_id:
        return None
    try:
        ctx.client._last_poll_pending = fresh
    except Exception:
        pass

    wait_params = {
        "timeout": max(0.1, min(timeout, 2.0)),
        "_min_wait": 0,
    }
    if "format" in params:
        wait_params["format"] = params["format"]
    try:
        return handle_wait(ctx, wait_params)
    except Exception:
        return None


def _drain_same_pending_after_choice(
    ctx: HandlerContext,
    rendered: dict | None,
    params: dict,
    *,
    timeout: float,
) -> dict | None:
    """Wait for a successful choice act to consume its pre-action pending.

    Some games queue ChoiceReturn handling one interaction tick later than the
    command_result.  The normal wait path can then observe the same pending id
    twice and treat it as an intentional same-id menu.  This stricter drain is
    only used for a no-story, same-pending choice shell after the normal
    follow-up already had a chance to replace it.
    """
    data = (rendered or {}).get("_data") or {}
    raw = data.get("_pending_raw") or {}
    if not raw:
        return None
    if not hasattr(ctx.client, "poll") or not hasattr(ctx.client, "pending"):
        return None

    old_id = raw.get("id")
    try:
        fresh = drain_stale_pending_request(
            ctx.client,
            raw,
            timeout=max(0.0, timeout),
            poll_interval=0.5,
        )
    except Exception:
        return None
    fresh_id = fresh.get("id") if isinstance(fresh, dict) else None
    if not fresh_id or fresh_id == old_id:
        return None
    try:
        ctx.client._last_poll_pending = fresh
    except Exception:
        pass

    wait_params = {
        "timeout": max(0.1, min(timeout, 2.0)),
        "_min_wait": 0,
    }
    if "format" in params:
        wait_params["format"] = params["format"]
    try:
        return handle_wait(ctx, wait_params)
    except Exception:
        return None


def _has_actionable_rendered_state(rendered: dict) -> bool:
    """True when rendered state has a real decision, not just quick nav."""
    data = rendered.get("_data") or {}
    pending = data.get("pending")
    if pending:
        if pending.get("type") != "choice":
            return True
        return any(
            not _is_suppressed_pending_choice(c)
            and not c.get("disabled")
            and not c.get("caption")
            for c in pending.get("choices", []))
    for button in data.get("buttons") or []:
        if button.get("disabled"):
            continue
        screen = button.get("screen", "")
        if screen not in ("quick_menu", "menu"):
            return True
    return False


def _button_label_for_rendered_index(rendered: dict | None, idx: int | None) -> str | None:
    """Map an agent-visible button index back to its current label.

    Rendered modal/shop/item screens often sit on top of a stale pending
    choice request.  In that state, numeric act(N) must follow the visible
    rendered button list, not pending.full_items from the story behind it.
    """
    if idx is None:
        return None
    data = (rendered or {}).get("_data") or {}
    buttons = data.get("buttons") or []
    label = button_label_for_display_index(
        buttons,
        idx,
        data.get("_button_categories"),
    )
    if label:
        return label
    for flat_idx, button in enumerate(buttons, 1):
        if button.get("disabled"):
            continue
        button_idx = button.get("index", flat_idx)
        if button_idx == idx:
            label = str(button.get("label", "")).strip()
            return label or None
    return None


def _choice_label_for_rendered_index(rendered: dict | None, idx: int | None) -> str | None:
    """Map an agent-visible choice index back to its current label."""
    if idx is None:
        return None
    data = (rendered or {}).get("_data") or {}
    pending = data.get("pending") or {}
    if pending.get("type") != "choice":
        return None
    choices = pending.get("choices") or []
    # Match by the agent-visible number (entry.index), not list position:
    # captions/disabled rows sit in this list with index=None and would
    # otherwise shift positional lookups off by one.
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if _is_suppressed_pending_choice(choice):
            continue
        if choice.get("disabled") or choice.get("caption"):
            continue
        if choice.get("index") == idx:
            label = str(choice.get("label", "")).strip()
            return label or None
    return None


def _probe_act_resolution(
    ctx: HandlerContext,
    result: dict,
    *,
    timeout: float = _ACT_APPLY_PROBE_SECONDS,
) -> dict:
    """Briefly wait for the shim's resolution of an accepted transaction.

    The acknowledgment intentionally carries no ``resolved_as`` (spec criterion
    8) because the bridge has only queued the command.  Post-act routing needs
    it, and the shim normally consumes the command on its very next poll, so a
    short bounded peek keeps fast interactions behaviorally identical to the
    pre-transactional flow (criterion 4).  A genuinely slow apply falls through
    to _predicted_act_resolution() instead of blocking — that slow case is the
    whole point of the transaction.  Peeking never acknowledges, so no story
    output is consumed here.
    """
    nonce = result.get("action_nonce")
    probe = getattr(ctx.client, "action_transaction", None)
    if not nonce or not callable(probe):
        return result
    timeout = max(0.0, timeout)
    if timeout <= 0:
        return result
    deadline = time.time() + timeout
    while True:
        transaction = None
        remaining = deadline - time.time()
        if remaining <= 0:
            return result
        try:
            # The GET's read timeout is the REMAINING BUDGET, not the poll
            # interval.  Capping it at _ACT_APPLY_PROBE_POLL_SECONDS made the
            # deadline term dead code and every probe request a 100 ms one:
            # under any real bridge latency the peek always timed out, so
            # settle routing silently fell through to the prediction path on
            # every act.  The poll constant governs the SLEEP between
            # attempts (below), nothing else.
            transaction = probe(
                nonce,
                timeout=remaining,
            )
        except Exception:
            return result
        if isinstance(transaction, dict):
            state = transaction.get("transaction_state")
            if state in {"applied", "settled", "failed", "rejected"}:
                result["transaction_state"] = state
                # Everything that is not transaction bookkeeping is the shim's
                # resolution metadata (resolved_as, interaction_type, screen,
                # action_names, ...) that post-act routing reads.
                for key, value in transaction.items():
                    if key in _TRANSACTION_BOOKKEEPING_KEYS:
                        continue
                    result[key] = value
                return result
        if time.time() >= deadline:
            return result
        time.sleep(_ACT_APPLY_PROBE_POLL_SECONDS)


def _predicted_act_resolution(
    rendered: dict | None,
    act_target: int | str | None,
) -> str:
    """Predict ``resolved_as`` for an accepted-but-unresolved transaction.

    A transactional acknowledgment carries ``submitted_target`` but never
    ``resolved_as`` (spec criterion 8) — the shim has not consumed the command
    yet.  Post-act routing (choice settle vs. screen-state settle) used to read
    the shim's answer, so fall back to what the caller had rendered when it
    picked the target.  Ambiguity resolves to "choice", matching the legacy
    ``result.get("resolved_as", "choice")`` default.
    """
    data = (rendered or {}).get("_data") or {}
    if isinstance(act_target, int):
        if _choice_label_for_rendered_index(rendered, act_target):
            return "choice"
        if _button_label_for_rendered_index(rendered, act_target):
            return "button"
        return "choice"
    label = str(act_target or "").strip().lower()
    if not label:
        return "choice"
    pending = data.get("pending") or {}
    for choice in pending.get("choices") or []:
        if (
            isinstance(choice, dict)
            and str(choice.get("label", "")).strip().lower() == label
        ):
            return "choice"
    for button in data.get("buttons") or []:
        if (
            isinstance(button, dict)
            and str(button.get("label", "")).strip().lower() == label
        ):
            return "button"
    return "choice"


def _choice_display_count(pending: dict | None) -> int:
    # Canonical choice/button display boundary — shared with the renderer
    # so act(N) resolves to the same row the agent saw numbered.
    return pending_numbered_choice_count(pending)


def _effective_choice_count(pr: dict | None, pre_rendered: dict | None) -> int:
    """Choice offset for numeric act resolution.

    Prefer the raw bridge pending_request, but fall back to the rendered
    state's (possibly synthesized) pending — a `call screen` choice screen
    reaches the bridge with no pending_request, so the raw count is 0 and
    the offset would otherwise be wrong, leaving numeric button resolution
    to rely on shim-index/rendered-number alignment.
    """
    n = _choice_display_count(pr) if pr else 0
    if n:
        return n
    rd = (pre_rendered or {}).get("_data") or {}
    rdp = rd.get("pending") or {}
    if rdp.get("type") == "choice":
        return pending_numbered_choice_count(rdp)
    return 0


def _wait_for_rendered_state_change(
    ctx: HandlerContext,
    pre_state_sig: tuple | None,
    params: dict,
    timeout: float = 3.0,
    prefer_actionable: bool = False,
    settle_delay: float = 0.6,
) -> dict:
    """Poll state until rendered output changes and briefly stops changing."""
    # Settlement needs the full rendered decision surface and complete
    # structured ``_data``, but not the diagnostic stats/inventory expansion.
    # Promoting that expansion into act() made screen-only controls (map
    # selections, routing previews, shops) dump it again on every click.
    state_params = {"brief": False, "_suppress_details": True}
    result_deadline = params.get("_result_deadline")
    if isinstance(result_deadline, (int, float)):
        state_params["_result_deadline"] = result_deadline
    if "format" in params:
        state_params["format"] = params["format"]

    last_error = None

    def fetch_state() -> dict | None:
        nonlocal last_error
        if (
            isinstance(result_deadline, (int, float))
            and time.time() >= float(result_deadline)
        ):
            return None
        try:
            fresh = handle_state(ctx, state_params)
            if (
                isinstance(result_deadline, (int, float))
                and time.time() >= float(result_deadline)
                and (fresh.get("_data") or {}).get("status") == "unknown"
            ):
                return None
            return fresh
        except Exception as exc:
            last_error = exc
            return None

    state_result = wait_for_stable_change(
        fetch=fetch_state,
        signature=_state_signature,
        initial_signature=pre_state_sig,
        timeout=timeout,
        settle_delay=settle_delay,
        poll_interval=0.3,
        accept=_has_actionable_rendered_state if prefer_actionable else None,
    )
    if state_result is not None:
        return state_result
    if last_error is not None:
        raise last_error
    if (
        isinstance(result_deadline, (int, float))
        and time.time() >= float(result_deadline)
    ):
        return {}
    return handle_state(ctx, state_params)


def _settle_state_after_screen_action(
    ctx: HandlerContext,
    result: dict,
    params: dict,
    *,
    pre_state_sig: tuple | None,
    pre_was_button_only: bool,
) -> None:
    """Poll rendered state after a screen-only button action."""
    state_result = _wait_for_rendered_state_change(
        ctx,
        pre_state_sig,
        params,
        timeout=_remaining_act_result_timeout(params),
    )
    if pre_was_button_only and (state_result.get("_data") or {}).get("pending"):
        # Modal/screen transitions can briefly expose stale underlay choices
        # after the overlay closes; one extra settle pass catches replacement.
        settled_result = _wait_for_rendered_state_change(
            ctx,
            _state_signature(state_result),
            params,
            timeout=_remaining_act_result_timeout(params, 2.5),
            prefer_actionable=True,
        )
        if _state_signature(settled_result) != _state_signature(state_result):
            state_result = settled_result
    result["wait"] = state_result
    _promote_wait_output(result, state_result)
    nonce = result.get("action_nonce")
    discard = getattr(ctx.client, "discard_rendered_action_transaction", None)
    if nonce and callable(discard):
        timeout = _remaining_act_result_timeout(params, 1.5)
        try:
            receipt = discard(
                nonce,
                action_id=result.get("action_id"),
                timeout=timeout,
            )
        except Exception:
            receipt = None
        transaction = getattr(receipt, "transaction", None)
        semantic_events = list(getattr(receipt, "events", None) or [])
        if semantic_events:
            semantic_output = format_wait_text(
                build_wait_data(semantic_events, None, False),
                fmt=params.get("format", "text"),
            )
            if semantic_output.get("status"):
                result["status"] = _merge_status_output(
                    result.get("status"), semantic_output["status"])
            for key in ("text", "story"):
                if semantic_output.get(key):
                    result[key] = _join_story_text(
                        _render_value_text(result.get(key)),
                        _render_value_text(semantic_output[key]),
                    )
        if isinstance(transaction, dict):
            cleanup_lost = (
                transaction.get("transaction_state") == "rejected"
                and transaction.get("reason") == "unknown_nonce"
            )
            if cleanup_lost:
                # The authoritative rendered-state read already proved that
                # the screen action landed. A restart/prune can lose only its
                # cleanup receipt; it must not reverse the visible success.
                local_transaction = result.get("transaction")
                if isinstance(local_transaction, dict):
                    local_transaction = dict(local_transaction)
                else:
                    local_transaction = _transaction_from_result(result)
                local_transaction.update({
                    "action_nonce": nonce,
                    "transaction_state": "settled",
                    "pending": False,
                    "settled_by": "rendered_state_local_retirement",
                })
                for key in ("reason", "error", "admission_pending"):
                    local_transaction.pop(key, None)
                _apply_transaction_to_output(result, local_transaction)
                if isinstance(result.get("pending"), bool):
                    result["pending"] = False
                result["_screen_action_retirement_warning"] = (
                    "The rendered action succeeded, but its transaction "
                    "receipt was no longer available during cleanup."
                )
            elif transaction.get("transaction_state") in {
                "settled", "failed", "rejected",
            }:
                _merge_probed_transaction(result, transaction)
        result["_screen_action_locally_retired"] = True


def _settle_wait_after_action(
    ctx: HandlerContext,
    result: dict,
    params: dict,
    *,
    button_context: bool,
    pre_state_sig: tuple | None,
    pre_rendered: dict | None,
    pre_visible_sig: tuple | None,
    pre_pending_id: str | None,
    pre_was_button_only: bool,
    acted_request_id: str | None = None,
    pre_act_seq: int | None = None,
) -> None:
    """Wait after an action and replace stale rendered output when needed."""
    # The observation window is no longer a timer.  "The post-action scrape has
    # landed" is E2 in the settle policy below -- a game_state sample provably
    # stamped after this act -- so the old 3/5/8-second min_waits are gone and
    # only a one-second floor remains, as insurance against mistaking a single
    # mid-transition frame for the outcome.
    min_wait = _ACT_POST_ACTION_MIN_WAIT_FLOOR
    issued_at = params.get("_act_issued_at")
    if not isinstance(issued_at, (int, float)):
        issued_at = time.time()
    issued_at = float(issued_at)
    wait_params = {
        "timeout": _remaining_act_result_timeout(params),
        "_min_wait": min_wait,
        # This drain owns the act's whole observation budget whenever the
        # receipt cannot settle (a choice into an unbroken burst), so the
        # story hand-back has to live HERE as well as in the settle loop
        # below -- otherwise it can only fire on acts whose receipt settled
        # early, which is exactly the button-only population fleet R63 saw
        # hand back while every numeric story choice ran to 60 s.
        "_act_story_handback": True,
        "_handback_since": issued_at,
    }
    if isinstance(params.get("_result_deadline"), (int, float)):
        wait_params["_result_deadline"] = params["_result_deadline"]
    if "format" in params:
        wait_params["format"] = params["format"]
    if params.get("action_nonce"):
        wait_params["action_nonce"] = params["action_nonce"]
        # This wait feeds the stuck-choice recovery below, which needs to know
        # whether a retry is allowed.  An open admission gate is that answer,
        # so return on it rather than burning the whole result timeout on a
        # transaction the bridge is already willing to step over.
        wait_params["_return_on_admission"] = True
    wait_result = handle_wait(ctx, wait_params)
    result["wait"] = wait_result
    _promote_wait_output(result, wait_result)
    scoped_transaction = _transaction_view(
        ((wait_result.get("_data") or {}).get("transaction"))
        or wait_result.get("transaction")
    )
    if scoped_transaction:
        # Later story-entry waits are ordinary and intentionally omit settled
        # transaction context. Preserve the scoped receipt now so a later
        # presentation-only result cannot downgrade the act to "settling".
        _merge_probed_transaction(result, scoped_transaction)
    if wait_result.get("story_continues"):
        # The scoped drain hit the hand-back bound with the story still
        # arriving.  Everything below this point exists to FIND a successor
        # surface, which is precisely what is being deferred to the caller's
        # next wait(); running it would spend the rest of the act budget
        # undoing the hand-back.
        #
        # Safe under the numeric-act contract: handle_wait marks
        # story_continues only when the composed output carries NO pending
        # and no ending, so nothing numbered is returned here -- the pre-act
        # menu is provably consumed (the receipt applied) and no successor is
        # claimed.  The following wait() binds the next numeric act against a
        # fresh render.
        _mark_act_story_continues(result)
        result.pop("pending", None)
        result.pop("_pending_raw", None)
        result.pop("_actionable_snapshot", None)
        data = result.get("_data")
        if isinstance(data, dict):
            data.pop("pending", None)
            data.pop("_pending_raw", None)
            data.pop("_actionable_snapshot", None)
        return
    drain_params = params
    if (
        scoped_transaction
        and scoped_transaction.get("transaction_state") == "settled"
    ):
        # Once the scoped receipt settles, presentation may continue on the
        # ordinary lane. Keep that tail bound to this action and stop at any
        # newer action's ownership boundary rather than auto-selecting it.
        drain_params = dict(params)
        drain_params["_ordinary_only"] = True
        drain_params["_ordinary_action_id"] = scoped_transaction.get(
            "action_id")
        drain_params["_allow_empty_story_tail"] = True
        drain_params["_allow_derived_terminal_tail"] = True
    # One settle policy, one observer.  The two story drains below keep their
    # own entry conditions and caps, but they are now SUBORDINATE: they run
    # only while the verdict is CONTINUE and stop as soon as it is not.
    settle = _ActSettleObserver(
        ctx,
        pre_visible_sig=pre_visible_sig,
        story_entry=_button_action_is_story_entry(result),
        rescrape_expected=bool(result.get("wait_after_action")),
        acted_request_id=acted_request_id,
        pre_pending_id=pre_pending_id,
        pre_act_seq=pre_act_seq,
        allow_empty_probe=bool(drain_params.get("_allow_empty_story_tail")),
        allow_derived_terminal=bool(
            drain_params.get("_allow_derived_terminal_tail")),
        # Anchored to the ACT, not to the moment this observer was built: the
        # scoped receipt drain above has already spent part of the bound.
        issued_at=issued_at,
    )
    settle.note_transaction(scoped_transaction)
    settle.judge(wait_result)
    if not settle.preempts:
        wait_result = _drain_story_entry_wait_after_action(
            ctx,
            result,
            drain_params,
            wait_result,
            settle,
        )
        settle.judge(wait_result)
    if not settle.preempts:
        wait_result = _drain_story_gap_after_choice_action(
            ctx,
            result,
            drain_params,
            wait_result,
            settle,
        )
    wait_result = _settle_act_until_verdict(
        ctx, result, drain_params, wait_result, settle)
    if settle.story_handback:
        _mark_act_story_continues(result)
    wait_repeats_pre_action_screen = (
        not button_context
        and pre_visible_sig is not None
        and bool(wait_result.get("buttons"))
        and not wait_result.get("pending")
        and not _rendered_has_story_text(wait_result)
        and (
            _visible_output_signature(wait_result) == pre_visible_sig
            or _rendered_buttons_repeat_pre_choices(wait_result, pre_rendered)
        )
    )
    if wait_repeats_pre_action_screen:
        followup_timeout = params.get("timeout", 8)
        try:
            followup_timeout = min(float(followup_timeout), 8.0)
        except (TypeError, ValueError):
            followup_timeout = 8
        followup_timeout = _remaining_act_result_timeout(
            params, followup_timeout)
        followup_params = {
            "timeout": followup_timeout,
            "_min_wait": 1,
        }
        if isinstance(params.get("_result_deadline"), (int, float)):
            followup_params["_result_deadline"] = params["_result_deadline"]
        if "format" in params:
            followup_params["format"] = params["format"]
        followup_result = handle_wait(ctx, followup_params)
        if (
            _meaningful_rendered_output(followup_result)
            and _visible_output_signature(followup_result) != pre_visible_sig
        ):
            result["wait"] = followup_result
            _promote_wait_output(result, followup_result)
            wait_result = followup_result
        else:
            _absorb_unpromoted_wait_story(result, followup_result)

    # (The story-entry drain that used to live here -- a second, independent
    # "is it done yet" loop whose only exits were story or a new choice
    # request -- is now _settle_act_until_verdict above, run for every act
    # against the one settle policy.)

    wait_pending_id = _pending_request_id(wait_result)
    wait_has_same_empty_pending = (
        wait_pending_id
        and wait_pending_id == pre_pending_id
        and not (wait_result.get("text") or wait_result.get("screen_text"))
    )
    wait_has_empty_pending_after_choice = (
        not button_context
        and bool(wait_result.get("pending"))
        and not _rendered_has_story_text(wait_result)
    )
    if wait_has_empty_pending_after_choice:
        followup_timeout = params.get("timeout", 8)
        try:
            followup_timeout = min(float(followup_timeout), 30.0)
        except (TypeError, ValueError):
            followup_timeout = 8
        followup_timeout = _remaining_act_result_timeout(
            params, followup_timeout)
        followup_params = {
            "timeout": followup_timeout,
            "_min_wait": 1,
        }
        if isinstance(params.get("_result_deadline"), (int, float)):
            followup_params["_result_deadline"] = params["_result_deadline"]
        if "format" in params:
            followup_params["format"] = params["format"]
        followup_result = handle_wait(ctx, followup_params)
        followup_pending_id = _pending_request_id(followup_result)
        if (
            _meaningful_rendered_output(followup_result)
            and (
                _rendered_has_story_text(followup_result)
                or followup_pending_id != wait_pending_id
                or _visible_output_signature(followup_result)
                != _visible_output_signature(wait_result)
            )
        ):
            result["wait"] = followup_result
            _promote_wait_output(result, followup_result)
            wait_result = followup_result
            wait_pending_id = followup_pending_id
        else:
            _absorb_unpromoted_wait_story(result, followup_result)
    if (
        not button_context
        and pre_pending_id
        and wait_pending_id == pre_pending_id
        and not _rendered_has_story_text(wait_result)
    ):
        drain_timeout = params.get("timeout", 8)
        try:
            drain_timeout = min(float(drain_timeout), 12.0)
        except (TypeError, ValueError):
            drain_timeout = 8.0
        drain_timeout = _remaining_act_result_timeout(params, drain_timeout)
        drained_result = _drain_same_pending_after_choice(
            ctx,
            wait_result,
            params,
            timeout=drain_timeout,
        )
        drained_pending_id = _pending_request_id(drained_result)
        if (
            drained_result is not None
            and _meaningful_rendered_output(drained_result)
            and (
                _rendered_has_story_text(drained_result)
                or (
                    drained_pending_id
                    and drained_pending_id != wait_pending_id
                )
                or _visible_output_signature(drained_result)
                != _visible_output_signature(wait_result)
            )
        ):
            result["wait"] = drained_result
            _promote_wait_output(result, drained_result)
            wait_result = drained_result
            wait_pending_id = drained_pending_id
        else:
            _absorb_unpromoted_wait_story(result, drained_result)
    wait_has_same_pending_after_button = (
        button_context
        and wait_pending_id
        and wait_pending_id == pre_pending_id
    )
    button_map_transition = (
        "map" in str(result.get("screen") or "").lower()
        or "[map:" in str(result.get("label") or "").lower()
    )
    wait_text = str(wait_result.get("text") or "").strip()
    wait_has_changed_buttons_after_button = (
        button_context
        and bool(wait_result.get("buttons"))
        and _visible_output_signature(wait_result) != pre_visible_sig
    )
    wait_has_story_output = (
        bool(wait_text and wait_text != "(no new events)")
        or bool(wait_result.get("screen_text"))
        or bool(wait_result.get("pending"))
        or wait_has_changed_buttons_after_button
    )
    # A declared modal panel IS the settled surface: its rows are the panel's
    # body and its buttons are the panel's own controls, both read from the
    # same post-act frame.  "Story text beside buttons and no pending" is the
    # stale-shell signature everywhere else, but here it is simply what an
    # open panel looks like, and re-polling for a successor spends up to six
    # seconds of the act's budget confirming a surface already proven.
    wait_shows_modal_panel = bool(
        (wait_result.get("_data") or {}).get("_modal_overlay_screens"))
    wait_has_stale_button_shell = (
        button_context
        and pre_state_sig is not None
        and bool(wait_result.get("buttons"))
        and not wait_result.get("pending")
        and _rendered_has_story_text(wait_result)
        and not wait_shows_modal_panel
    )
    wait_has_unverified_story_pending = (
        button_context
        and pre_state_sig is not None
        and bool(wait_result.get("pending"))
        and _rendered_has_story_text(wait_result)
        and (not pre_pending_id or wait_pending_id != pre_pending_id)
    )
    wait_has_pending_after_button_overlay = (
        button_context
        and pre_state_sig is not None
        and bool(wait_result.get("pending"))
        and not pre_pending_id
    )
    wait_has_same_pending_after_choice_story = (
        not button_context
        and wait_pending_id
        and wait_pending_id == pre_pending_id
        and _rendered_has_story_text(wait_result)
    )
    if (
        wait_has_stale_button_shell
        or wait_has_unverified_story_pending
        or wait_has_pending_after_button_overlay
        or (
            button_map_transition
            and wait_has_same_pending_after_button
        )
    ):
        state_result = _wait_for_rendered_state_change(
            ctx, pre_state_sig, params,
            timeout=_remaining_act_result_timeout(params, 6.0),
            prefer_actionable=True)
        state_data = state_result.get("_data") or {}
        if (
            state_data.get("pending")
            and _visible_output_signature(state_result)
            != _visible_output_signature(wait_result)
        ):
            has_previous_story = bool(
                str(result.get("text") or "").strip()
                or result.get("story")
            )
            # Script lines are never optional: the map guards below exist to
            # stop a STALE SCREEN SCRAPE of the old location being glued onto
            # the destination, not to discard say/narration the action just
            # produced.  A location button that plays a one-shot scene before
            # its menu (Echoes' map travel) hits every map guard, and dropping
            # here handed the agent a bare choice block while the transcript
            # kept the whole scene.
            previous_is_script_story = _result_carries_script_story(result)
            keep_previous_story = has_previous_story and (
                previous_is_script_story
                or (
                    not button_map_transition
                    and not _rendered_has_map_buttons(pre_rendered)
                    and not _rendered_has_story_text(state_result)
                )
            )
            result["wait"] = state_result
            if keep_previous_story:
                _promote_wait_output_preserving_story(result, state_result)
            else:
                _promote_wait_output(result, state_result)
            result[_SCRIPT_STORY_MARKER] = bool(
                _rendered_has_script_story(state_result)
                or (keep_previous_story and previous_is_script_story)
            )
        followup_candidate = result.get("wait") or wait_result
        followup_pending_id = _pending_request_id(followup_candidate)
        stale_shell_resolved_by_wait = (
            wait_has_stale_button_shell
            and wait_has_changed_buttons_after_button
            and not wait_pending_id
            and not button_map_transition
        )
        stale_pending_ids = {
            pending_id for pending_id in (
                pre_pending_id,
                wait_pending_id,
            ) if pending_id
        }
        if (
            (pre_was_button_only or button_map_transition)
            and (
                wait_has_stale_button_shell
                or wait_has_pending_after_button_overlay
                or wait_has_same_pending_after_button
            )
            and not stale_shell_resolved_by_wait
            and (
                not stale_pending_ids
                or followup_pending_id in stale_pending_ids
            )
        ):
            followup_timeout = params.get("timeout", 8)
            try:
                followup_timeout = min(float(followup_timeout), 8.0)
            except (TypeError, ValueError):
                followup_timeout = 8
            followup_timeout = _remaining_act_result_timeout(
                params, followup_timeout)
            followup_result = None
            if button_map_transition and wait_has_same_pending_after_button:
                followup_result = _drain_stale_pending_followup(
                    ctx,
                    followup_candidate,
                    params,
                    timeout=followup_timeout,
                )
            if followup_result is None:
                followup_params = {
                    "timeout": followup_timeout,
                    "_min_wait": 1,
                }
                if isinstance(params.get("_result_deadline"), (int, float)):
                    followup_params["_result_deadline"] = params[
                        "_result_deadline"]
                if "format" in params:
                    followup_params["format"] = params["format"]
                followup_result = handle_wait(ctx, followup_params)
            followup_data = followup_result.get("_data") or {}
            if (
                followup_data.get("pending")
                and _visible_output_signature(followup_result)
                != _visible_output_signature(wait_result)
            ):
                has_previous_story = bool(
                    str(result.get("text") or "").strip()
                    or result.get("story")
                )
                previous_is_script_story = _result_carries_script_story(result)
                keep_previous_story = has_previous_story and (
                    previous_is_script_story
                    or (
                        not button_map_transition
                        and not _rendered_has_map_buttons(pre_rendered)
                        and not _rendered_has_story_text(followup_result)
                    )
                )
                result["wait"] = followup_result
                if keep_previous_story:
                    _promote_wait_output_preserving_story(
                        result, followup_result)
                else:
                    _promote_wait_output(result, followup_result)
                result[_SCRIPT_STORY_MARKER] = bool(
                    _rendered_has_script_story(followup_result)
                    or (keep_previous_story and previous_is_script_story)
                )
            else:
                # This follow-up wait already CONSUMED the bridge's event
                # stream.  Rejecting its render because no successor menu has
                # appeared yet must not take the script lines it drained with
                # it: an NVL interlude between a console screen and its next
                # menu is exactly that shape, and it is where fleet R62 lost
                # the antenna storm-damage beat.
                _absorb_unpromoted_wait_story(result, followup_result)
    if wait_has_same_pending_after_choice_story:
        followup_timeout = params.get("timeout", 8)
        try:
            followup_timeout = min(float(followup_timeout), 8.0)
        except (TypeError, ValueError):
            followup_timeout = 8
        followup_timeout = _remaining_act_result_timeout(
            params, followup_timeout)
        followup_params = {
            "timeout": followup_timeout,
            "_min_wait": 0,
        }
        if isinstance(params.get("_result_deadline"), (int, float)):
            followup_params["_result_deadline"] = params["_result_deadline"]
        if "format" in params:
            followup_params["format"] = params["format"]
        followup_result = handle_wait(ctx, followup_params)
        followup_pending_id = _pending_request_id(followup_result)
        followup_has_story = _rendered_has_story_text(followup_result)
        followup_replaces_pending = bool(
            followup_pending_id
            and wait_pending_id
            and followup_pending_id != wait_pending_id
        )
        followup_promoted = bool(
            _meaningful_rendered_output(followup_result)
            and _visible_output_signature(followup_result)
            != _visible_output_signature(wait_result)
            and (followup_has_story or followup_replaces_pending)
        )
        if followup_promoted:
            result["wait"] = followup_result
            _promote_wait_output_preserving_story(result, followup_result)
        else:
            _absorb_unpromoted_wait_story(result, followup_result)
        if not followup_promoted and (
            followup_pending_id == wait_pending_id
            and acted_request_id == wait_pending_id
        ):
            # A resolved menu can leave one say pause visible while the
            # bridge still carries that menu's final snapshot. Waiting alone
            # cannot move the script, and retrying the choice can target the
            # successor menu once it appears. Ask Ren'Py to advance the say
            # interaction instead. A genuinely live same-menu hub refuses
            # advance, so its intentional request remains untouched. Use the
            # stable pre-action id: BridgeClient may clear its transient
            # _acted_request_id after exhausting same-request polling before
            # this recovery branch runs.
            _advanced_post_choice_story = False
            for _advance_attempt in range(6):
                try:
                    advance_result = ctx.client.command("advance")
                except Exception:
                    advance_result = None
                if not (
                    isinstance(advance_result, dict)
                    and _result_succeeded(advance_result)
                ):
                    break
                _advanced_post_choice_story = True
                advanced_result = handle_wait(ctx, followup_params)
                _promote_wait_output_preserving_story(
                    result, advanced_result)
                advanced_pending_id = _pending_request_id(advanced_result)
                if advanced_pending_id != wait_pending_id:
                    break
            if (
                _advanced_post_choice_story
                and _pending_request_id(result) == wait_pending_id
            ):
                # Never advertise a request that this successful choice has
                # already answered. Keep its story, but make the caller take
                # a fresh wait before another number can target a new menu.
                result.pop("pending", None)
                result.pop("_pending_raw", None)
                result.pop("_actionable_snapshot", None)
                data = result.get("_data")
                if isinstance(data, dict):
                    data.pop("pending", None)
                    data.pop("_pending_raw", None)
                result["warning"] = (
                    "The choice resolved, but its successor menu is still "
                    "settling. Call wait() before acting again."
                )
                result["_stale_pending_suppressed"] = True
                ctx.client.last_request_id = (
                    "__resolved_choice_waiting_for_successor__"
                )
                ctx.client.last_choices = None
                ctx.client.last_actionable_snapshot = None
    result_pending_id = _pending_request_id(result)
    result_still_pre_action_choice = (
        not button_context
        and pre_state_sig is not None
        and bool(pre_pending_id)
        and result_pending_id == pre_pending_id
    )
    if result_still_pre_action_choice:
        try:
            followup_timeout = params.get("timeout", 8)
            try:
                followup_timeout = min(float(followup_timeout), 30.0)
            except (TypeError, ValueError):
                followup_timeout = 8.0
            followup_timeout = _remaining_act_result_timeout(
                params, followup_timeout)
            state_result = _wait_for_rendered_state_change(
                ctx,
                pre_state_sig,
                params,
                timeout=followup_timeout,
                prefer_actionable=True,
                settle_delay=0.3,
            )
        except Exception:
            state_result = None
        if state_result is not None:
            state_pending_id = _pending_request_id(state_result)
            state_is_fresh_pending = bool(
                state_pending_id and state_pending_id != pre_pending_id
            )
            state_visible_changed = (
                _visible_output_signature(state_result) != pre_visible_sig
            )
            if (
                _meaningful_rendered_output(state_result)
                and (state_is_fresh_pending or state_visible_changed)
            ):
                has_previous_story = bool(
                    str(result.get("text") or "").strip()
                    or result.get("story")
                )
                previous_is_script_story = _result_carries_script_story(result)
                keep_previous_story = has_previous_story and (
                    previous_is_script_story
                    or not _rendered_has_story_text(state_result)
                )
                result["wait"] = state_result
                if keep_previous_story:
                    _promote_wait_output_preserving_story(result, state_result)
                else:
                    _promote_wait_output(result, state_result)
                result[_SCRIPT_STORY_MARKER] = bool(
                    _rendered_has_script_story(state_result)
                    or (keep_previous_story and previous_is_script_story)
                )
        if _pending_request_id(result) == pre_pending_id:
            # The acknowledged choice already consumed this request. If every
            # drain still sees its stale shell, do not hand the answered menu
            # back to the caller. Story that arrived beside the stale shell is
            # preserved, but exact request identity is authoritative: a real
            # loop re-registers a fresh request id.
            result.pop("pending", None)
            result.pop("_pending_raw", None)
            result.pop("_actionable_snapshot", None)
            data = result.get("_data")
            if isinstance(data, dict):
                data.pop("pending", None)
                data.pop("_pending_raw", None)
            result["warning"] = (
                "The choice resolved, but its successor menu is still "
                "settling. Call wait() before acting again."
            )
            result["_stale_pending_suppressed"] = True
            ctx.client.last_request_id = (
                "__resolved_choice_waiting_for_successor__"
            )
            ctx.client.last_choices = None
            ctx.client.last_actionable_snapshot = None
    stale_wait_ids = {
        pending_id for pending_id in (
            pre_pending_id,
            wait_pending_id,
        ) if pending_id
    }
    result_pending_id = _pending_request_id(result)
    result_has_fresh_pending = (
        bool(result.get("pending"))
        and bool(result_pending_id)
        and bool(stale_wait_ids)
        and result_pending_id not in stale_wait_ids
    )
    if (
        button_context
        and pre_state_sig is not None
        and not result_has_fresh_pending
        and (
            not wait_has_story_output
            or wait_has_same_empty_pending
            or wait_has_same_pending_after_button
        )
    ):
        state_result = _wait_for_rendered_state_change(
            ctx, pre_state_sig, params,
            timeout=_remaining_act_result_timeout(params, 6.0),
            prefer_actionable=True)
        if _state_signature(state_result) != pre_state_sig:
            has_previous_story = bool(
                str(result.get("text") or "").strip()
                or result.get("story")
            )
            previous_is_script_story = _result_carries_script_story(result)
            result["wait"] = state_result
            if has_previous_story and previous_is_script_story:
                _promote_wait_output_preserving_story(result, state_result)
            else:
                _promote_wait_output(result, state_result)
# _post_action_min_wait (3 s plain button / 5 s choice / 8 s wait-after-action
# or story entry) lived here.  It was a May-2026 stand-in for "the post-action
# scrape has landed", from before events carried provenance, and it is now E2
# in the settle policy: a game_state sample the shim stamped after this act.
# All that survives is _ACT_POST_ACTION_MIN_WAIT_FLOOR.


@dataclass
class _PostActSettleContext:
    button_context: bool
    pre_state_sig: tuple | None
    pre_rendered: dict | None
    pre_pending_id: str | None
    acted_request_id: str | None
    pre_was_button_only: bool
    pre_act_seq: int | None
    pre_screen_presentation: tuple | None


def _mark_scene_unchanged_after_settle(
    ctx: HandlerContext,
    result: dict,
    *,
    pre_act_seq: int | None,
    deadline: float | None = None,
) -> None:
    """Annotate successful actions that settle back to the same game seq."""
    if pre_act_seq is None:
        return
    remaining = deadline - time.time() if deadline is not None else 3.0
    if remaining <= 0:
        return
    try:
        post_gs = _call_with_timeout(
            ctx.client.game_state, min(3.0, remaining))
        post_seq = (post_gs or {}).get("_seq", 0)
        if result.get("pending") or result.get("buttons") or result.get("screen_text"):
            return
        text = (result.get("text") or "").strip()
        looks_empty = text in ("(no new events)", "")
        if not looks_empty or post_seq != pre_act_seq:
            return
        overlays = (post_gs or {}).get("_active_overlays") or []
        if overlays:
            msg = (
                "(action succeeded but scene unchanged \u2014 overlay {!r} is "
                "active and may be absorbing actions; dismiss it first)"
            ).format(", ".join(overlays))
        else:
            msg = (
                "(action succeeded but scene unchanged \u2014 the target may "
                "have been a no-op, or the resulting state matched the "
                "prior one)"
            )
        result["text"] = msg
        result["_scene_unchanged"] = True
    except Exception:
        pass


# act(wait=True) is supposed to mean "applied AND settled".  The only
# operational meaning of "settled" a caller can plan on is: my next act will
# be ADMITTED.  _settle_wait_after_action legitimately returns before that —
# min_wait expiry and a screen full of buttons are both sufficient for it —
# and the gap between those two definitions is what surfaced live as
# `action_in_flight` 409s on the agent's very next call.  So the act path
# spends a small, bounded slice of the caller's OWN budget confirming
# admission, and says so plainly when it could not.
_ADMISSION_WAIT_CAP_SECONDS = 3.0
_ADMISSION_POLL_INTERVAL_SECONDS = 0.25


def _act_result_timeout(params: dict) -> float:
    """The caller's own act budget, matching _settle_wait_after_action."""
    try:
        return max(
            0.0, float(params.get("result_timeout", params.get("timeout", 60)))
        )
    except (TypeError, ValueError):
        return 60.0


def _remaining_act_result_timeout(
    params: dict,
    requested: float | int | None = None,
) -> float:
    """Return time still available inside the act's single wall-clock budget."""
    if requested is None:
        requested = params.get(
            "result_timeout", params.get("timeout", 60))
    try:
        limit = max(0.0, float(requested))
    except (TypeError, ValueError):
        limit = 60.0
    deadline = params.get("_result_deadline")
    if not isinstance(deadline, (int, float)):
        return limit
    return max(0.0, min(limit, float(deadline) - time.time()))


def _returned_decision_is_actionable(
    ctx: HandlerContext,
    result: dict,
    acted_request_id: str | None,
) -> bool:
    """Whether the decision rendered at timeout is safely bound for act()."""
    labels = _formatted_pending_labels(result)
    data = result.get("_data") or {}
    has_buttons = bool(data.get("buttons") or result.get("buttons"))
    if not labels and not has_buttons:
        return False
    snapshot = (
        data.get("_actionable_snapshot")
        or result.get("_actionable_snapshot")
    )
    cached = getattr(ctx.client, "last_actionable_snapshot", None)
    if not isinstance(snapshot, dict) or not isinstance(cached, dict):
        return False
    if not actionable_snapshots_equivalent(snapshot, cached):
        return False
    request_id = _pending_request_id(result)
    if request_id and acted_request_id and request_id == acted_request_id:
        return False
    if request_id and getattr(ctx.client, "last_request_id", None) != request_id:
        return False
    return True


def _mark_act_result_timeout(
    result: dict,
    *,
    actionable_decision: bool = False,
) -> None:
    """Describe an accepted action whose observation budget expired.

    Expiry is not an action failure. Keep the acknowledgement successful and
    expose the unfinished lifecycle consistently instead of synthesizing an
    error or mixing ``transaction_state=failed`` with optimistic success
    booleans. Recovery guidance deliberately uses a plain wait: it follows the
    transaction while it remains live and still works if the bridge retires
    the receipt before the caller's next round trip.
    """
    transaction = result.get("transaction")
    if not isinstance(transaction, dict):
        transaction = _transaction_from_result(result)
        if transaction:
            result["transaction"] = transaction
    state = (transaction or result).get("transaction_state")
    data = result.get("_data") or {}
    has_rendered_decision = bool(
        result.get("pending")
        or result.get("buttons")
        or data.get("pending")
        or data.get("buttons")
    )
    if state in {"failed", "rejected"} or (
        state == "settled"
        and (actionable_decision or has_rendered_decision)
    ):
        result.pop("result_timeout_reached", None)
        return
    presentation_only_timeout = state == "settled"
    result["result_timeout_reached"] = True
    if transaction and not presentation_only_timeout:
        transaction["admission_pending"] = True
        if actionable_decision:
            transaction["successor_actionable"] = True
    if actionable_decision:
        result.setdefault(
            "warning",
            "The successor menu is visible, but the prior action is still "
            "settling. "
            "Call wait() before acting again.",
        )
    else:
        # A settling warning and a numbered menu make contradictory promises:
        # the warning says observe again, while the menu invites an immediate
        # numeric act. If provenance could not prove the decision actionable,
        # preserve story but remove the unverified surface. A fresh wait will
        # return the real successor.
        result.pop("pending", None)
        result.pop("buttons", None)
        result.pop("_pending_raw", None)
        result.pop("_actionable_snapshot", None)
        data = result.get("_data")
        if isinstance(data, dict):
            data.pop("pending", None)
            data.pop("buttons", None)
            data.pop("_pending_raw", None)
            data.pop("_actionable_snapshot", None)
        result["_stale_pending_suppressed"] = True
        if presentation_only_timeout and _rendered_has_story_text(result):
            result.setdefault(
                "warning",
                "Available output is shown, but observation reached the "
                "result timeout before any successor decision was confirmed. "
                "Call wait() to continue; that wait may contain no additional "
                "story.",
            )
        elif presentation_only_timeout:
            result.setdefault(
                "warning",
                "The action settled, but observation reached the result "
                "timeout before any successor decision was confirmed. Call "
                "wait() to continue.",
            )
        elif _rendered_has_story_text(result):
            result.setdefault(
                "warning",
                "Available output is shown; transaction confirmation is "
                "still settling. "
                "Call wait() to confirm it; that wait may contain "
                "no additional story.",
            )
        else:
            result.setdefault(
                "warning",
                "The action was accepted, but its result is still settling. "
                "Call wait() to continue this transaction.",
            )


def _act_preflight_timeout_result(params: dict) -> dict | None:
    """Fail before submission once the act's shared observation budget ends."""
    if _remaining_act_result_timeout(params) > 0:
        return None
    transport_deadline = params.get("_transport_deadline")
    if (
        isinstance(transport_deadline, (int, float))
        and time.time() >= float(transport_deadline)
    ):
        return {
            "ok": False,
            "success": False,
            "transaction_state": "rejected",
            "reason": "transport_timeout_before_submission",
            "error": (
                "Did not act - the MCP response budget expired before "
                "submission."
            ),
        }
    return {
        "ok": False,
        "success": False,
        "transaction_state": "rejected",
        "reason": "result_timeout_before_submission",
        "error": "Did not act - the result timeout expired during preflight.",
    }


def _merge_probed_transaction(result: dict, probed: dict) -> None:
    """Fold a fresher /transaction peek into the act result.

    Merged, not replaced: the settle drain's view carries the shim's
    resolution fields, and a later peek must refresh the lifecycle without
    dropping them.
    """
    view = _transaction_view(probed) or {}
    existing = result.get("transaction")
    if isinstance(existing, dict):
        existing.update(view)
        view = existing
    _apply_transaction_to_output(result, view)


def _await_act_admission(
    ctx: HandlerContext, result: dict, *, deadline: float,
) -> None:
    """Wait, briefly, until a follow-up act would be accepted.

    Bounded three ways and never any other way: the ``_ADMISSION_WAIT_CAP``,
    whatever is left of the caller's ``result_timeout``, and the first
    observation that the transaction is no longer admission-blocking.  It
    peeks (``GET /transaction``), so it acknowledges nothing and no story
    output is consumed here.

    This does NOT try to outlast the bridge's idle tiers — an act that
    produced no observable outcome at all still blocks for its full idle
    budget, by design, and burning the caller's timeout on that would trade
    one visible failure for a much slower invisible one.
    """
    nonce = result.get("action_nonce")
    if not nonce:
        return
    transaction = result.get("transaction")
    transaction = transaction if isinstance(transaction, dict) else None
    state = (transaction or result).get("transaction_state")
    if state in {"settled", "failed", "rejected"}:
        return
    if transaction is not None and transaction.get("admission_open"):
        return
    if result.get("story_continues"):
        # The act deliberately handed a still-running story back.  Its
        # transaction is applied and, by construction, still emitting, so the
        # admission gate provably will not open inside the cap -- spending it
        # would put the hand-back back over its own bound for a question this
        # branch can already answer: it would NOT be admitted.
        if transaction is None:
            transaction = _transaction_from_result(result)
            if transaction:
                result["transaction"] = transaction
        if transaction:
            transaction["admission_pending"] = True
        return
    probe = getattr(ctx.client, "action_transaction", None)
    if not callable(probe):
        return
    end = time.time() + min(
        _ADMISSION_WAIT_CAP_SECONDS, max(0.0, deadline - time.time())
    )
    latest = None
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            break
        try:
            probed = probe(nonce, timeout=remaining)
        except Exception:
            probed = None
        if isinstance(probed, dict) and probed.get("action_nonce") in (
            None, nonce,
        ):
            latest = probed
            if (
                probed.get("transaction_state")
                in {"settled", "failed", "rejected"}
                or probed.get("admission_open")
            ):
                _merge_probed_transaction(result, probed)
                return
        remaining = end - time.time()
        if remaining <= 0:
            break
        time.sleep(min(_ADMISSION_POLL_INTERVAL_SECONDS, remaining))
    if latest is not None:
        _merge_probed_transaction(result, latest)
    # Returning anyway is the right call (the old behaviour), but the caller
    # deserves to know that its next act may be rejected rather than
    # discovering it from a 409.
    transaction = result.get("transaction")
    if not isinstance(transaction, dict):
        transaction = _transaction_from_result(result)
        if transaction:
            result["transaction"] = transaction
    if transaction:
        transaction["admission_pending"] = True


def _settle_after_successful_act(
    ctx: HandlerContext,
    result: dict,
    params: dict,
    settle_ctx: _PostActSettleContext,
) -> None:
    """Route successful act() follow-up through the rendered-state settle layer."""
    # Default to wait path (captures narration after Jump/Call buttons like
    # topics, shop items, dialogue exits).  Only use state path for buttons
    # that are purely screen-level (ShowMenu, Hide, FileSave; no script
    # narration follows).
    if _button_action_uses_wait_path(result):
        _settle_wait_after_action(
            ctx,
            result,
            params,
            button_context=settle_ctx.button_context,
            pre_state_sig=settle_ctx.pre_state_sig,
            pre_rendered=settle_ctx.pre_rendered,
            pre_visible_sig=(
                _visible_output_signature(settle_ctx.pre_rendered)
                if settle_ctx.pre_rendered is not None
                else None
            ),
            pre_pending_id=settle_ctx.pre_pending_id,
            acted_request_id=settle_ctx.acted_request_id,
            pre_was_button_only=settle_ctx.pre_was_button_only,
            pre_act_seq=settle_ctx.pre_act_seq,
        )
    else:
        _settle_state_after_screen_action(
            ctx,
            result,
            params,
            pre_state_sig=settle_ctx.pre_state_sig,
            pre_was_button_only=settle_ctx.pre_was_button_only,
        )
    passive_screen = _refresh_missing_screen_text_from_state(
        ctx,
        result,
        params,
        pre_screen_presentation=settle_ctx.pre_screen_presentation,
    )
    _merge_passive_overlay_text(
        ctx,
        result,
        screen=passive_screen,
        fmt=params.get("format", "text"),
        deadline=params.get("_result_deadline"),
        sample_live=passive_screen is None,
    )
    _mark_scene_unchanged_after_settle(
        ctx,
        result,
        pre_act_seq=settle_ctx.pre_act_seq,
        deadline=params.get("_result_deadline"),
    )


def _refresh_missing_screen_text_from_state(
    ctx: HandlerContext,
    result: dict,
    params: dict,
    *,
    pre_screen_presentation: tuple | None = None,
) -> dict | None:
    """Patch a bare choice shell, or return its passive overlay snapshot."""
    if params.get("format", "text") not in ("text", "quiet"):
        return
    if not result.get("pending"):
        return
    if result.get("text") or result.get("screen_text"):
        return
    deadline = params.get("_result_deadline")
    remaining = deadline - time.time() if isinstance(
        deadline, (int, float)) else 2.0
    if remaining <= 0:
        return
    result_labels = _rendered_choice_labels(result)
    try:
        screen = _get_screen(ctx, timeout=min(2.0, remaining))
    except Exception:
        screen = None
    if screen:
        screen_labels = [
            _normalize_label(str(button.get("label") or "").strip())
            for button in screen.get("buttons") or []
            if str(button.get("label") or "").strip()
        ]
        if (
            result_labels
            and screen_labels
            and not (set(result_labels) & set(screen_labels))
            and _screen_has_choice_buttons(screen)
        ):
            return
        # Registered passive overlays have their own occurrence ledger. Their
        # cumulative rows are also flattened into screen["texts"], so neither
        # field is a safe bare-choice fallback: copying either here replays the
        # whole panel before _merge_passive_overlay_text can deliver only the
        # new occurrences. Blocking overlays remain owned by this fallback.
        passive_overlay = bool(
            not screen.get("overlay_active")
            and (
                _passive_overlay_rows(screen)
                or _passive_overlay_rows_by_screen(screen)
            )
        )
        if passive_overlay:
            return screen
        current_screen_presentation = _ordinary_screen_presentation_signature(
            screen)
        if (
            pre_screen_presentation is not None
            and current_screen_presentation == pre_screen_presentation
            and not screen.get("overlay_active")
        ):
            # An ordinary say/menu screen may keep persistent HUD chrome while
            # replacing only its actionable choices.  The bare-choice rescue
            # exists to recover newly visible presentation, not to replay an
            # unchanged screen snapshot after every submenu and Back action.
            # Registered blocking overlays remain snapshot-owned and are
            # deliberately exempt above.
            return
        pending_data = (result.get("_data") or {}).get("pending")
        # Pending is only needed here to filter exact input-prompt echoes.
        # Passing choice shells into format_wait_text() can render incomplete
        # fallback pending data that was never meant for agent output.
        if not (
            isinstance(pending_data, dict)
            and pending_data.get("type") == "input"
        ):
            pending_data = None
        screen_text = format_wait_text({
            "pending": pending_data,
            "_screen_texts": (
                screen.get("overlay_texts") or screen.get("texts") or []
            )
        }).get("screen_text")
        if screen_text:
            result["screen_text"] = screen_text
            _record_ordinary_screen_presentation(ctx, result, screen)
            _claim_prefetched_screen_text_snapshot(ctx, screen, screen_text)
            return
    if isinstance(deadline, (int, float)) and time.time() >= deadline:
        return
    state_params = {"brief": False}
    if isinstance(deadline, (int, float)):
        state_params["_result_deadline"] = deadline
    try:
        state_result = handle_state(ctx, state_params)
    except Exception:
        return
    screen_text = state_result.get("screen_text") or state_result.get("text")
    if not screen_text or not state_result.get("pending"):
        return
    result_id = _pending_request_id(result)
    state_id = _pending_request_id(state_result)
    if result_id and state_id and result_id != state_id:
        return
    state_labels = _rendered_choice_labels(state_result)
    if result_labels and state_labels and result_labels != state_labels:
        return
    result["screen_text"] = screen_text



@dataclass
class _ActCall:
    """Everything one act() call carries from its pre-act read to its receipt.

    handle_act is a pipeline: read the surface, resolve the target, refuse or
    submit, settle, finalize. Each stage needs most of what the previous one
    learned, so the shared bindings live here instead of as three dozen
    locals threaded through an 800-line body.
    """

    params: dict
    target: Any
    act_deadline: float
    act_invocation: Any = None
    transport_deadline: Any = None
    # Resolved target, rewritten in place by the resolution phases.
    target_str: str = ""
    is_numeric: bool = False
    idx: int | None = None
    target_is_interaction_id: bool = False
    do_wait: bool = True
    act_target: Any = None
    # What the CALLER last saw, captured before our own reads refresh it.
    read_request_id: Any = None
    read_choices: Any = None
    read_actionable_snapshot: Any = None
    # What the game shows now, read once before acting.
    pre_state_result: dict | None = None
    pre_state_sig: tuple | None = None
    pre_actionable_snapshot: Any = None
    pre_pending_id: Any = None
    pre_data: dict = field(default_factory=dict)
    pre_was_button_only: bool = False
    pre_act_seq: Any = None
    pre_act_req_id: Any = None
    pre_screen_presentation: Any = None
    requested_accept_timeout: float = 15.0
    acceptance_reserve: float = 0.0
    # Filled in from the submission onward.
    resolution_deferred: bool = False
    deferred_resolution: Any = None
    optimistic_acted_id: Any = None
    consumed_request_id: Any = None
    result: dict | None = None
    finished: bool = False


def _read_pre_act_surface(ctx: HandlerContext, call: _ActCall) -> dict | None:
    """Read the surface the act is about to be resolved against.

    Captures both bindings that matter: what the caller last saw, and what the
    game shows now. Returns a refusal when a declared-modal panel owns the
    surface, or None to continue.
    """
    params = call.params
    call.target_str = str(call.target).strip()
    call.is_numeric = isinstance(call.target, int) or call.target_str.isdigit()
    call.idx = int(call.target_str) if call.is_numeric else None
    call.target_is_interaction_id = False
    # Coerce once up front: wait may arrive as a string ("false"), which a
    # bare truthiness test would wrongly read as True. See _coerce_bool.
    call.do_wait = _coerce_bool(params.get("wait"), ctx.act_wait)

    # The id of the request this client last RENDERED to its caller —
    # the previous wait/state/choices output (persisted across CLI
    # invocations, in-memory for MCP).  Captured BEFORE our own state
    # fetch below refreshes it: this is the binding for "act N is a
    # reply to the numbered list you last saw".
    call.read_request_id = getattr(ctx.client, "last_request_id", None)
    # The choices the caller last RENDERED, captured before our state fetch
    # below overwrites them.  Lets the staleness guard tell a bare request-id
    # rotation (same menu re-emitted) from a genuine decision-point change.
    call.read_choices = getattr(ctx.client, "last_choices", None)
    call.read_actionable_snapshot = getattr(
        ctx.client, "last_actionable_snapshot", None,
    )

    call.pre_state_result = None
    call.pre_state_sig = None
    try:
        call.pre_state_result = handle_state(ctx, {
            "brief": False,
            "_suppress_details": True,
            "_result_deadline": call.act_deadline,
        })
        call.pre_state_sig = _state_signature(call.pre_state_result)
    except Exception:
        pass
    _timed_out = _act_preflight_timeout_result(params)
    if _timed_out is not None:
        return _timed_out
    call.pre_actionable_snapshot = getattr(
        ctx.client, "last_actionable_snapshot", None,
    )
    call.pre_pending_id = _pending_request_id(call.pre_state_result)
    call.pre_data = (call.pre_state_result or {}).get("_data") or {}
    call.pre_was_button_only = bool(call.pre_data.get("buttons")) and not call.pre_data.get("pending")

    # Fail closed before any recovery path: a declared-modal panel owns the
    # whole surface, so a label belonging to the menu it covers is not
    # actionable no matter what an older snapshot still remembers seeing.
    if not call.is_numeric:
        _modal_refusal = _modal_overlay_hidden_label_refusal(
            call.target_str, call.pre_state_result)
        if _modal_refusal is not None:
            return _modal_refusal

    # A screen scrape can briefly omit persistent HUD controls while the
    # previous state response has already shown one to the caller. Keep the
    # fail-closed visibility rule, but give the live surface a short bounded
    # chance to confirm that same named control again. We never act from the
    # old snapshot alone: if the current scraper does not restore the label,
    # the ordinary rejection below still wins.
    if (
        not call.is_numeric
        and not _target_was_visible_before_act(call.target_str, call.pre_state_result)
        and _target_was_visible_in_snapshot(
            call.target_str, call.read_actionable_snapshot,
        )
    ):
        _visibility_deadline = min(call.act_deadline, time.time() + 1.5)
        while time.time() < _visibility_deadline:
            time.sleep(min(0.05, max(0.0, _visibility_deadline - time.time())))
            try:
                _candidate = handle_state(ctx, {
                    "brief": False,
                    "_suppress_details": True,
                    "_result_deadline": _visibility_deadline,
                })
            except Exception:
                break
            if _target_was_visible_before_act(call.target_str, _candidate):
                call.pre_state_result = _candidate
                call.pre_state_sig = _state_signature(_candidate)
                call.pre_pending_id = _pending_request_id(_candidate)
                call.pre_data = (_candidate.get("_data") or {})
                call.pre_was_button_only = (
                    bool(call.pre_data.get("buttons"))
                    and not call.pre_data.get("pending")
                )
                break

    # Starting from the main menu abandons every delivery/binding from the
    # completed run before the Start action can compose its first response.
    # Transactional acts normally preserve prefetched pre-action events, but
    # at this boundary those events belong to the old timeline and otherwise
    # replay into (or reorder) the new opening.
    _timeline_action_label = call.target_str
    if call.is_numeric:
        _timeline_action_label = (
            _button_label_for_rendered_index(call.pre_state_result, call.idx)
            or _choice_label_for_rendered_index(call.pre_state_result, call.idx)
            or call.target_str
        )
    _pre_lifecycle = call.pre_data.get("_lifecycle") or {}
    if (
        _pre_lifecycle.get("context") in {"main_menu", "menu"}
        and _normalize_label(_timeline_action_label) in {
            "start", "new game", "newgame",
        }
    ):
        # Drop old handler/prefetch presentation state, but retain action
        # occurrence ownership until an authoritative /state read advances
        # the reset generation. A still-drainable ending transaction may
        # straddle this click; clearing its receipts here replays that ending
        # into the fresh opening on the next scoped wait.
        _reset_timeline_context(
            ctx,
            clear_client_prefetch=True,
            clear_client_action_delivery=False,
        )


def _resolve_act_target(ctx: HandlerContext, call: _ActCall) -> dict | None:
    """Map the target onto a concrete control, or refuse.

    A rendered number may name a choice, a numbered button, or a gated
    class-ability row the shim only resolves by label/id; a label may name a
    disabled interaction or nothing visible at all. Returns a refusal, or
    None with the target rewritten in place.
    """
    params = call.params
    # Pre-check: if numeric target points to an info/disabled item,
    # return a helpful error instead of silently firing a NullAction.
    # Also: if numeric target matches the display-filtered choice row
    # whose shim interaction is disabled/gated (e.g. class-ability
    # choices whose index=None in interactions but visible in pending),
    # substitute the label so the shim's pre_resolve path runs.
    if call.is_numeric:
        try:
            _timed_out = _act_preflight_timeout_result(params)
            if _timed_out is not None:
                return _timed_out
            state = _call_with_timeout(
                ctx.client.state,
                _remaining_act_result_timeout(params, 3.0),
            ) or {}
            pr = state.get("pending_request") or {}
            gs = state.get("game_state") or {}
            rendered_button_label = _button_label_for_rendered_index(
                call.pre_state_result, call.idx)
            rendered_choice_label = _choice_label_for_rendered_index(
                call.pre_state_result, call.idx)
            choice_rows = [
                it for it in (pr.get("full_items") or [])
                if isinstance(it, dict)
                and (it.get("label") or "").strip()
                and (it.get("label") or "").strip() != "(disabled)"
            ]
            choice_count = _effective_choice_count(pr, call.pre_state_result)
            rendered_data = (call.pre_state_result or {}).get("_data") or {}
            prefer_rendered_button = (
                rendered_button_label
                and (
                    not rendered_data.get("pending")
                    or rendered_data.get("_overlay_active")
                    or (choice_count and call.idx is not None and call.idx > choice_count)
                )
            )
            if (
                rendered_choice_label
                and call.idx is not None
                and (not choice_count or call.idx > choice_count)
            ):
                call.target_str = rendered_choice_label
                call.is_numeric = False
                call.idx = None
            elif prefer_rendered_button:
                call.target_str = rendered_button_label
                call.is_numeric = False
                call.idx = None
            # Display-filtered choices: prefer the post-format rendered rows.
            # Raw full_items can include captions/info rows that do not consume
            # the agent-visible numeric index.
            # Numbered rows only: captions and disabled entries don't
            # consume an agent-visible number, so positional indexing must
            # skip them (raw full_items keeps captions/disabled-with-label).
            full = pr.get("full_items") or []
            display_rows = []
            for it in full:
                if not isinstance(it, dict):
                    continue
                lbl = (it.get("label") or "").strip()
                if not lbl or lbl == "(disabled)":
                    continue
                if it.get("is_caption") or it.get("is_disabled"):
                    continue
                display_rows.append(it)
            # Map numeric position -> display row (1-based).
            rendered_pending = rendered_data.get("pending") or (
                (call.pre_state_result or {}).get("pending")
                if isinstance(call.pre_state_result, dict) else None
            )
            rendered_choice_rows = (
                rendered_pending.get("choices")
                if isinstance(rendered_pending, dict) else None
            )
            row = None
            if call.is_numeric and isinstance(rendered_choice_rows, list):
                # Match by agent-visible number (entry.index), not position.
                for _rc in rendered_choice_rows:
                    if (isinstance(_rc, dict)
                            and not _rc.get("disabled")
                            and not _rc.get("caption")
                            and _rc.get("index") == call.idx):
                        row = _rc
                        break
            if row is None and call.is_numeric and 1 <= call.idx <= len(display_rows):
                row = display_rows[call.idx - 1]
            if call.is_numeric and isinstance(row, dict):
                row_label = row.get("label", "")
                # Look up the shim's interaction for this label.
                _shim_itr = None
                for itr in gs.get("interactions", []):
                    if itr.get("display_label") == row_label:
                        _shim_itr = itr
                        break
                # If the shim has this as a gated/disabled choice
                # (idx=None OR disabled=True) AND pending shows it
                # as enabled, redirect to label path so pre_resolve
                # / pre_set can toggle the class action first.
                # Also: if the shim interaction is MISSING entirely
                # (scraper missed it) but pending shows it enabled,
                # label-based is the only way to resolve correctly —
                # numeric would land on a different shim interaction.
                _pr_enabled = not (
                    row.get("is_disabled", False) or row.get("disabled", False)
                )
                _shim_index = (
                    _shim_itr.get("index") if isinstance(_shim_itr, dict)
                    else None
                )
                if _pr_enabled and (
                        _shim_itr is None
                        or _shim_index != call.idx
                        or _shim_itr.get("index") is None
                        or _shim_itr.get("disabled")):
                    _interaction_id = (
                        _shim_itr.get("id") if isinstance(_shim_itr, dict)
                        else None
                    )
                    call.target_str = str(_interaction_id or row_label)
                    call.target_is_interaction_id = bool(_interaction_id)
                    call.is_numeric = False
                    call.idx = None
            # Legacy info/disabled check on interactions.
            if call.is_numeric:
                _interaction_offset = choice_count if pr else 0
                for itr in _normalize_interaction_disabled(
                        gs.get("interactions", [])):
                    if _interaction_offset and itr.get("type") == "choice":
                        continue
                    if itr.get("index") == call.idx:
                        if itr.get("type") == "info":
                            return {"error": f"Index {call.idx} is an info item "
                                              f"(not clickable): '{itr.get('display_label','')[:80]}'"}
                        if itr.get("disabled") or _button_is_disabled(itr):
                            return {"error": f"Index {call.idx} is disabled: "
                                              f"'{itr.get('display_label','')[:80]}'"}
                        break
        except Exception:
            pass  # Fall through to normal act path on any lookup error.

    if not call.is_numeric:
        try:
            _timed_out = _act_preflight_timeout_result(params)
            if _timed_out is not None:
                return _timed_out
            state = _call_with_timeout(
                ctx.client.state,
                _remaining_act_result_timeout(params, 3.0),
            ) or {}
            if not call.target_is_interaction_id:
                call.target_str = choice_target_to_action_label(
                    call.target_str,
                    _pending_choice_items(state),
                )
            gs = state.get("game_state") or {}
            interactions = _normalize_interaction_disabled(
                gs.get("interactions", []))
            hidden_choice_labels = _hidden_choice_labels(call.pre_state_result)
            match = _resolve_label_interaction(
                call.target_str, interactions, hidden_choice_labels)
            if match.ambiguous:
                # Fail closed before ever reaching the shim: an ambiguous
                # target must never be forwarded for the shim to guess at
                # independently (Fleet R66's KIT/toolkit shape resolved
                # differently on each side of that seam).
                return _ambiguous_act_target_refusal(
                    call.target_str, match.ambiguous)
            matched = match.matched
            if matched and (matched.get("disabled") or _button_is_disabled(matched)):
                return {"error": "Interaction is disabled: "
                                 f"'{matched.get('display_label', call.target_str)[:80]}'"}
            if matched and not call.target_is_interaction_id:
                # Bind the exact resolved label so the shim's own resolver
                # lands on its exact-match tier instead of re-running fuzzy
                # matching independently — the two layers must never
                # disagree about what a short or ambiguous-looking target
                # meant.  Skipped when the target is already a deliberately
                # chosen interaction id (the gated/disabled-row redirect
                # above): that id was picked specifically to disambiguate
                # rows a shared display_label cannot.
                call.target_str = matched.get("display_label", call.target_str)
            if (
                not matched
                and not _target_was_visible_before_act(
                    call.target_str, call.pre_state_result)
                and not (
                    call.read_request_id
                    and _target_matches_rendered_choices(
                        call.target_str, call.read_choices)
                )
                and not _target_looks_like_auto_continue(call.target_str)
                and isinstance((call.pre_state_result or {}).get("_data"), dict)
                and bool(
                    "_interactions" in call.pre_data
                    or _rendered_button_labels(call.pre_state_result)
                )
            ):
                # The act is refused either way; a declared modal panel just
                # explains WHY the target is not on the surface, and that it
                # is one CLOSE away rather than gone.
                panel_refusal = _modal_overlay_hidden_label_refusal(
                    call.target_str, call.pre_state_result, unresolved=True,
                )
                if panel_refusal is not None:
                    return panel_refusal
                return {
                    "error": (
                        "Did not act - target {!r} is not in the rendered "
                        "choices or controls. Call state() and act on a "
                        "visible option.".format(call.target_str)
                    ),
                    "_target_not_visible_at_act": True,
                }
        except Exception:
            pass


def _snapshot_pre_act_sequence(ctx: HandlerContext, call: _ActCall) -> dict | None:
    """Record the pre-act game_state sequence for post-failure diagnosis."""
    params = call.params
    # Snapshot game_state._seq and the pending_request id BEFORE
    # acting so we can diagnose post-failure whether the target became
    # stale (scene advanced between the agent reading state and
    # calling act).
    call.pre_act_seq = None
    call.pre_act_req_id = getattr(ctx.client, "last_request_id", None)
    try:
        _timed_out = _act_preflight_timeout_result(params)
        if _timed_out is not None:
            return _timed_out
        _pre_gs = _call_with_timeout(
            ctx.client.game_state,
            _remaining_act_result_timeout(params, 2.0),
        )
        if _pre_gs:
            call.pre_act_seq = _pre_gs.get("_seq", 0)
    except Exception:
        pass


def _bind_numeric_act_target(ctx: HandlerContext, call: _ActCall) -> dict | None:
    """Hold a still-numeric target to the list the caller actually saw.

    Past this point a raw number would be resolved against the shim's LIVE
    value map, so anything the rendered snapshot cannot account for refuses
    loudly instead. Returns a refusal, or None.
    """
    params = call.params
    # All targets go through the shim's unified interaction list
    # (choices + buttons merged, post-transform).  The shim act command
    # resolves by index, label, or ID.
    if call.is_numeric:
        try:
            _timed_out = _act_preflight_timeout_result(params)
            if _timed_out is not None:
                return _timed_out
            state = _call_with_timeout(
                ctx.client.state,
                _remaining_act_result_timeout(params, 3.0),
            ) or {}
            pr = state.get("pending_request") or {}
            gs = state.get("game_state") or {}
            offset = _effective_choice_count(pr, call.pre_state_result)
            if offset and call.idx is not None and call.idx > offset:
                interactions = [
                    itr for itr in _normalize_interaction_disabled(
                        gs.get("interactions", [])
                    )
                    if itr.get("type") != "choice"
                ]
                matched = _match_interaction(
                    call.target_str,
                    interactions,
                    display_index_offset=offset,
                )
                if matched:
                    if matched.get("disabled") or _button_is_disabled(matched):
                        return {"error": "Interaction is disabled: "
                                         f"'{matched.get('display_label', call.target_str)[:80]}'"}
                    call.target_str = matched.get("display_label", call.target_str)
                    call.is_numeric = False
                    call.idx = None
        except Exception:
            pass

    # Numeric contract: `act N` is a reply to the last rendered numbered
    # list — an index into what the format layer numbered in the pre-act
    # snapshot, nothing else.  By this point every rendered number that
    # maps to a BUTTON has been resolved to a concrete label/id above;
    # a still-numeric target is only ever forwarded to the shim as a
    # reply within the rendered CHOICE range.  Everything else refuses
    # loudly here instead of letting the shim resolve the raw number
    # against its live value_map / focus list (the dynamic value_map
    # scan is how a numeric act during pure narration clicked a
    # newly-sensitive nav button).  A missing snapshot (state fetch
    # failed) keeps the legacy fallthrough — never refuse on missing
    # telemetry.
    if call.is_numeric and call.idx is not None and isinstance(
            (call.pre_state_result or {}).get("_data"), dict):
        try:
            _rd = call.pre_state_result["_data"]
            _rd_pending = _rd.get("pending") or {}
            _choice_n = (
                pending_numbered_choice_count(_rd_pending)
                if _rd_pending.get("type") == "choice" else 0
            )
            _btn_n = numbered_button_count(
                _rd.get("buttons"), _rd.get("_button_categories"))
            _max_visible = _choice_n + _btn_n
        except Exception:
            _choice_n = _btn_n = _max_visible = None
        if _max_visible == 0:
            if _rd.get("_state_unavailable"):
                # Nobody answered the state read: there is no game to act
                # on, which is not "narration in progress".
                return {
                    "error": (
                        "Did not act — the bridge did not answer, so there "
                        "is no game state to act on. Launch a game first "
                        "(launch <game>), or check slots and the bridge URL."
                    ),
                    "reason": "bridge_unreachable",
                }
            if call.do_wait and call.read_request_id:
                recovered = _recover_numeric_act_during_autoadvance(
                    ctx,
                    call.pre_state_result,
                    call.pre_state_sig,
                    params,
                )
                if recovered is not None:
                    return recovered
            # Pure narration (or an unnumbered UI): there is no list the
            # number could be replying to.
            return {
                "error": (
                    "Did not act — nothing is numbered right now "
                    "(narration in progress). Use wait or advance, or "
                    "act by label for UI buttons. (target {})".format(call.idx)
                ),
                "_nothing_numbered_at_act": True,
            }
        if _max_visible is not None and call.idx > _max_visible:
            # Out-of-range guard (kept from June): past the last visible
            # number the shim act silently no-ops with
            # menu_indexerror_guard off while the client reports
            # ok-on-timeout.
            return {"error": (
                "Index {} is out of range — {} option(s) available "
                "(1-{}). Call state() to see the current options.".format(
                    call.idx, _max_visible, _max_visible))}
        if _choice_n is not None and call.idx > _choice_n:
            # The number belongs to a rendered numbered button that the
            # resolution passes above could not map to a shim
            # interaction.  Last chance: resolve it against the rendered
            # snapshot's own numbering; if even that fails, refuse — a
            # raw number would be resolved against the shim's LIVE list,
            # which may have shifted since the render.
            _snapshot_label = _button_label_for_rendered_index(
                call.pre_state_result, call.idx)
            if _snapshot_label:
                call.target_str = _snapshot_label
                call.is_numeric = False
                call.idx = None
            else:
                return {
                    "error": (
                        "Did not act — {} is numbered on screen but no "
                        "longer maps to a concrete control. Call state() "
                        "and act again by number or label.".format(call.idx)
                    ),
                    "_unmapped_numbered_button": True,
                }
        if (
            call.is_numeric
            and call.read_request_id
            and call.pre_pending_id
            and call.read_request_id != call.pre_pending_id
        ):
            # Staleness binding: a numeric reply targets the numbered
            # choice list the caller last saw.  The pending request id
            # has changed since that render — the world moved on, and
            # index N of the NEW menu may be a completely different
            # choice.  The codebase treats a new request id as a new
            # decision point (drain_stale_pending_request), so this is
            # genuine drift, not a re-render.
            #
            # A changed id is NOT on its own a changed decision, and the
            # message below claims one.  Fleet R62 echo62-o01 08:38:17 was
            # refused on a menu whose rendered_choices and current_choices its
            # own stale_details showed to be character-for-character identical,
            # five labels in the same order; only the id had rotated.  The
            # refusal now needs a real surface difference as well, and stays
            # fail-closed: it refuses when the id changed AND the surface
            # changed, or when the surface cannot be read at all.
            #
            # The strongest available comparison wins.  When the caller's read
            # carried a full actionable snapshot, an identical one now means
            # nothing the numbering depends on moved -- interactions, the
            # other-button row, disabled state and action values included --
            # and a differing one means something did, even if the numbered
            # labels alone still match (a vanished KIT button renumbers
            # everything below it).  Explicit shim reissue provenance is a
            # strict subset of this and no longer needs its own arm.
            _same_actionable_surface = (
                call.read_actionable_snapshot is not None
                and call.read_actionable_snapshot
                == call.pre_actionable_snapshot
            )
            # With no snapshot to compare -- an older shim, or the CLI, which
            # persists neither snapshot nor choices across invocations -- the
            # numbered list is the whole readable surface.  An identical list
            # (same labels, same count, same order) is a re-registration: the
            # act binds against the CURRENT snapshot, so index N resolves
            # through the live menu's own value map and still reads as the
            # option the caller chose.  An empty list is unreadable, not
            # equal, and keeps the strict refusal.
            _same_visible_menu = (
                call.read_actionable_snapshot is None
                and _numbered_choices_unchanged(
                    call.read_choices, call.pre_state_result,
                )
            )
            if not (_same_actionable_surface or _same_visible_menu):
                return {
                    "error": (
                        "Did not act — the state changed since you last "
                        "looked (the numbered choices were replaced). Call "
                        "wait() to see the current decision, then act again."
                    ),
                    "stale_details": {
                        "rendered_request_id": call.read_request_id,
                        "current_request_id": call.pre_pending_id,
                        "rendered_choices": list(call.read_choices or []),
                        "current_choices": list(
                            ((call.pre_data.get("_pending_raw") or {}).get(
                                "choices") or [])
                        ),
                        "rendered_snapshot_present": isinstance(
                            call.read_actionable_snapshot, dict),
                        "current_snapshot_present": isinstance(
                            call.pre_actionable_snapshot, dict),
                    },
                    "_stale_numeric_act": True,
                }


def _submit_act(ctx: HandlerContext, call: _ActCall) -> None:
    """Present, submit and rescue the action; leave the result on *call*.

    Sets ``call.result``, and ``call.finished`` when the budget ran out before
    submission could be attempted.
    """
    params = call.params
    call.act_target = call.idx if call.is_numeric else call.target_str
    call.pre_screen_presentation = ctx.overlay.ordinary_screen_presentation_receipt
    try:
        call.requested_accept_timeout = max(
            0.0, float(params.get("accept_timeout", 15)))
    except (TypeError, ValueError):
        call.requested_accept_timeout = 15.0
    call.acceptance_reserve = min(
        call.requested_accept_timeout,
        _remaining_act_result_timeout(params),
    )
    if ctx.hooks.before_action:
        # Broadcast presentation is deliberately outside the caller's
        # transaction-result budget: it waits for the agent's action bubble to
        # drain before mutating the game and can take tens of seconds under a
        # fleet. It still counts against the MCP transport deadline, so a slow
        # presentation can never make the tool outlive its outer tools/call.
        _presentation_started = time.time()
        try:
            _hook_payload = {
                "target": call.act_target,
                "original_target": call.target,
                "wait": call.do_wait,
            }
            if isinstance(call.transport_deadline, (int, float)):
                # Presentation and submission share the MCP transport budget.
                # Keep the requested acceptance window out of the bubble
                # drain's allowance so a saturated streamer cannot consume the
                # whole call before POST /command is attempted.
                _hook_payload["_transport_deadline"] = max(
                    time.time(),
                    float(call.transport_deadline) - call.acceptance_reserve,
                )
            ctx.hooks.before_action("act", _hook_payload)
        finally:
            call.act_deadline += max(0.0, time.time() - _presentation_started)
            if isinstance(call.transport_deadline, (int, float)):
                call.act_deadline = min(call.act_deadline, float(call.transport_deadline))
            params["_result_deadline"] = call.act_deadline
        _timed_out = _act_preflight_timeout_result(params)
        if _timed_out is not None:
            call.result = _timed_out
            call.finished = True
            return
    remaining = _remaining_act_result_timeout(params)
    if remaining <= 0:
        call.result = _act_preflight_timeout_result(params)
        call.finished = True
        return
    accept_timeout = min(call.requested_accept_timeout, remaining)
    result = _call_transactional_act(
        ctx.client.act_transaction,
        call.act_target,
        action_nonce=params.get("action_nonce"),
        accept_timeout=accept_timeout,
        deadline=call.act_deadline,
        invocation=call.act_invocation,
    )
    # Transport-timeout no-op recovery: when the act's POST /command never
    # reached a briefly-saturated bridge ("Connection failed: timed out"), the
    # click did not land and the choice is still pending.  Re-confirm the same
    # pending is active and resubmit once, so the agent no longer has to spot
    # the no-op and retry by hand (the ~2-minute streaming stall).
    result = _retry_act_after_transport_timeout(
        ctx,
        result,
        act_target=call.act_target,
        pre_pending_id=call.pre_pending_id,
        invocation=call.act_invocation,
        deadline=call.act_deadline,
    )
    if call.do_wait:
        # Fast interactions get the shim's real resolution here, so the
        # recovery machinery below (and the settle routing) sees exactly what
        # the pre-transactional flow saw.  Slow ones stay deferred.
        result = _probe_act_resolution(
            ctx,
            result,
            timeout=_remaining_act_result_timeout(
                params, _ACT_APPLY_PROBE_SECONDS),
        )
    # No-pending gate: when nothing was actionable before the act, a
    # transient shim error ("No active choice request" / "No interaction
    # matching") means the caller acted on nothing — not that a visible
    # target raced a scene advance.  Both rescue paths below would
    # otherwise resync-retry onto a menu the caller never saw, or rewrite
    # the failure into a success that shows the just-arrived prompt.
    _acted_on_nothing = (
        not _result_succeeded(result)
        and _transient_act_error(str(result.get("error", "")))
        and _nothing_actionable_before_act(call.pre_state_result)
        and not _target_looks_like_auto_continue(call.target)
    )
    if _acted_on_nothing:
        # Not an early return: the failure tail below still appends the
        # Ren'Py-exception hint when the game is actually wedged.  The
        # rewritten error no longer matches _transient_act_error, so the
        # resync/recovery paths leave it alone.
        result = _fail_act_with_no_pending(result, call.target)
    result = _retry_failed_act_after_resync(
        ctx,
        result,
        target=call.target,
        act_target=call.act_target,
        pre_rendered=call.pre_state_result,
        invocation=call.act_invocation,
        deadline=call.act_deadline,
    )
    call.resolution_deferred = result.get("transaction_state") in {
        "acceptance_unknown", "accepted", "applied",
    } and not result.get("resolved_as")
    if ctx.hooks.after_act and not call.resolution_deferred:
        source = result.get("resolved_as", "choice")
        result = ctx.hooks.after_act(source, call.target_str, result)

    if call.do_wait:  # coerced above
        result = _recover_failed_act_after_advance(
            ctx, result, call.pre_state_result, call.pre_state_sig, params)

    # "Scene changed" hint on match-failure: if the act failed with a
    # "No interaction matching" error AND the pending request id has
    # advanced since we started, the agent's target was correct for
    # the previous screen but the game moved on (auto-advance, async
    # narration, etc.).  Append a hint so the agent knows to wait()
    # instead of retrying with a different label.
    if not _result_succeeded(result):
        err = str(result.get("error", ""))
        if "No interaction matching" in err and call.pre_act_req_id is not None:
            remaining = _remaining_act_result_timeout(params, 3.0)
            try:
                _post_state = (
                    _call_with_timeout(ctx.client.state, remaining) or {}
                    if remaining > 0 else {}
                )
                _post_req_id = ((_post_state.get("pending_request") or {}).get("id"))
                if _post_req_id and _post_req_id != call.pre_act_req_id:
                    result["error"] = (err
                        + " (scene changed mid-act — the visible "
                          "choices have moved on; call wait() before "
                          "retrying)")
                    result["_scene_advanced"] = True
            except Exception:
                pass
        result = _append_renpy_exception_hint_on_failed_act(
            ctx, result, deadline=call.act_deadline)
    call.result = result


def _settle_act_after_submission(ctx: HandlerContext, call: _ActCall, result: dict) -> dict:
    """Mark the consumed request, settle the action, and promote its
    transaction onto the act result.
    """
    params = call.params
    # Mark the acted request so wait() skips the stale pending.
    # Only for choices — button actions are side actions that don't
    # resolve the pending (e.g. topic Jump returns to the same menu).
    #
    # A deferred transaction has no resolved_as yet, but its ACCEPTANCE is
    # durable: from the caller's side the pre-act pending is already answered.
    # Waiting for the settle drain to set this let the stale pending leak back
    # into the act's own wait output, so mark it optimistically from the
    # pre-act context and undo it if the shim later reports a button.
    call.deferred_resolution = (
        _predicted_act_resolution(call.pre_state_result, call.act_target)
        if call.resolution_deferred else None
    )
    call.optimistic_acted_id = None
    call.consumed_request_id = None
    if _result_succeeded(result):
        if call.resolution_deferred:
            resolved = (
                "button"
                if (call.deferred_resolution == "button" or call.pre_was_button_only)
                else "choice"
            )
        else:
            resolved = result.get("resolved_as", "choice")
        if resolved != "button":
            # The wait below may already have observed the successor menu.
            # Only the request visible before this act was consumed; marking
            # a freshly observed successor as acted makes the next numeric
            # choice look stale despite being exactly what we rendered.
            acted_id = call.read_request_id or call.pre_pending_id
            if acted_id:
                call.consumed_request_id = acted_id
                previous_acted_id = getattr(ctx.client, "_acted_request_id", None)
                ctx.client._acted_request_id = acted_id
                if call.resolution_deferred:
                    call.optimistic_acted_id = (acted_id, previous_acted_id)

    # Follow up with wait() or state() if requested.
    if call.do_wait and _result_succeeded(result) and "wait" not in result:
        resolved_as = (
            call.deferred_resolution
            if call.resolution_deferred and call.deferred_resolution
            else result.get("resolved_as", "choice")
        )
        button_context = resolved_as == "button" or call.pre_was_button_only
        settle_params = params
        if result.get("action_nonce") and not params.get("action_nonce"):
            settle_params = dict(params)
            settle_params["action_nonce"] = result["action_nonce"]
        _settle_after_successful_act(
            ctx,
            result,
            settle_params,
            _PostActSettleContext(
                button_context=button_context,
                pre_state_sig=call.pre_state_sig,
                pre_rendered=call.pre_state_result,
                pre_pending_id=call.pre_pending_id,
                acted_request_id=call.read_request_id or call.pre_pending_id,
                pre_was_button_only=call.pre_was_button_only,
                pre_act_seq=call.pre_act_seq,
                pre_screen_presentation=call.pre_screen_presentation,
            ),
        )
        # The settle drain carries the transaction's FINAL state.  Promote it
        # unconditionally: an apply-probe that caught "applied" otherwise left
        # the act result claiming a pending transaction that had since settled.
        wait_result = result.get("wait") or {}
        transaction = _transaction_view(
            ((wait_result.get("_data") or {}).get("transaction"))
            or (wait_result.get("transaction") if isinstance(wait_result, dict) else None)
        )
        if not isinstance(transaction, dict):
            transaction = {
                key: wait_result[key]
                for key in (
                    "action_nonce", "action_id", "transaction_state",
                    "resolved_as", "resolved_label", "label", "request_id",
                )
                if isinstance(wait_result, dict) and key in wait_result
            }
        if transaction and transaction.get("action_nonce") in (
            None, result.get("action_nonce"),
        ):
            result["transaction"] = transaction
            for key in (
                "action_nonce", "action_id", "transaction_state",
                "resolved_as", "resolved_label", "label", "request_id",
                "error", "reason",
            ):
                if key in transaction:
                    result[key] = transaction[key]
            if "pending" in transaction:
                result["transaction_pending"] = transaction["pending"]
        # act(wait=True) returns only once a follow-up act would be admitted —
        # or says that it would not.  See _await_act_admission.
        # Settle composition can already expose the successor menu while its
        # private metadata still names the consumed request. Repair that
        # response boundary while the act budget can still fund the one
        # authoritative /state read. Admission polling may consume the
        # remainder of the budget afterwards.
        if result.get("resolved_as") == "button":
            call.consumed_request_id = None
        _bind_returned_pending(
            ctx,
            result,
            deadline=call.act_deadline,
            acted_request_id=call.consumed_request_id,
        )
        _await_act_admission(ctx, result, deadline=call.act_deadline)
        if time.time() >= call.act_deadline:
            _mark_act_result_timeout(
                result,
                actionable_decision=_returned_decision_is_actionable(
                    ctx,
                    result,
                    call.read_request_id or call.pre_pending_id,
                ),
            )
        if call.resolution_deferred:
            if result.get("resolved_as"):
                if ctx.hooks.after_act:
                    result = ctx.hooks.after_act(
                        result["resolved_as"], call.target_str, result,
                    )
                if result.get("resolved_as") != "button":
                    acted_id = call.read_request_id or call.pre_pending_id
                    if acted_id:
                        ctx.client._acted_request_id = acted_id
                elif call.optimistic_acted_id is not None:
                    # The optimistic "this answered a choice" mark was wrong:
                    # the shim resolved a screen button, which leaves the
                    # pending request live.  Restore what we overwrote.
                    marked, previous = call.optimistic_acted_id
                    if getattr(ctx.client, "_acted_request_id", None) == marked:
                        ctx.client._acted_request_id = previous
    return result


def _drain_previous_action_before_act(ctx: HandlerContext, call: _ActCall) -> dict | None:
    """Hand back unread predecessor output before accepting another choice."""
    next_nonce = getattr(ctx.client, "_next_auto_action_nonce", None)
    nonce = next_nonce() if callable(next_nonce) else None
    if not nonce or nonce == call.params.get("action_nonce"):
        return None
    drain_deadline = min(call.act_deadline, time.time() + 3.0)
    checked = set()
    while nonce and nonce != call.params.get("action_nonce"):
        checked.add(nonce)
        remaining = drain_deadline - time.time()
        if remaining <= 0:
            return {"ok": False, "_act_not_submitted": True, "error":
                    "Did not submit the requested action: earlier receipts "
                    "are still being checked. Call wait() before choosing again."}
        previous = handle_wait(ctx, {
            "action_nonce": nonce,
            "timeout": remaining,
            "_result_deadline": drain_deadline,
            "_return_on_admission": True,
            "_pre_action_drain": True,
            "format": call.params.get("format", "text"),
        })
        if (previous.get("_delivered_output")
                or previous.get(_OVERLAY_DELIVERIES_KEY) or previous.get("status")
                or previous.get("error")):
            break
        # A still-open but empty screen receipt must not prevent Close,
        # nor hide unread output owned by a later outstanding action.
        nonce = next((item for item in getattr(ctx.client, "_active_action_nonces", ())
                      if item not in checked), None)
    else:
        return None
    previous["ok"] = False
    previous["_act_not_submitted"] = True
    previous["error"] = (
        "Did not submit the requested action: the previous action has output "
        "to read first. Read this response, then choose from the current controls."
    ) if not previous.get("error") else (
        "Did not submit the requested action: " + str(previous["error"]))
    return previous


def handle_act(ctx: HandlerContext, params: dict) -> dict:
    """Pick a visible choice or screen button.

    When ``wait`` is True (the default, controlled by ``ctx.act_wait``),
    the handler automatically follows up with a wait() call and merges
    the story events and next decision point into the result.
    """
    target = params.get("target")
    if target is None:
        return {"error": "Missing 'target' parameter"}

    # One deadline covers preflight reads, acceptance, settlement, recovery,
    # and the final admission probe. Previously each stage minted a fresh
    # timeout, so a 60-second act routinely returned after ~75 seconds under
    # fleet load and recovery branches could extend it further.
    params = dict(params)
    _act_invocation = _act_invocation_from_params(params)
    # The story hand-back's anchor.  Stamped once, here, so every stage that
    # can hold the act open (the scoped receipt drain, the two story drains,
    # the settle loop) measures the same bound from the same instant.
    params.setdefault("_act_issued_at", time.time())
    _act_deadline = time.time() + _act_result_timeout(params)
    _transport_deadline = params.get("_transport_deadline")
    if isinstance(_transport_deadline, (int, float)):
        _act_deadline = min(_act_deadline, float(_transport_deadline))
    # act now receives a _result_deadline from the MCP servers the same way
    # wait does.  It BOUNDS the act budget and never extends it, so a caller
    # that asks for a long timeout still cannot outlive its own tools/call.
    _caller_result_deadline = params.get("_result_deadline")
    if isinstance(_caller_result_deadline, (int, float)):
        _act_deadline = min(_act_deadline, float(_caller_result_deadline))
    params["_result_deadline"] = _act_deadline
    _timed_out = _act_preflight_timeout_result(params)
    if _timed_out is not None:
        return _timed_out

    call = _ActCall(
        params=params,
        target=target,
        act_deadline=_act_deadline,
        act_invocation=_act_invocation,
        transport_deadline=_transport_deadline,
    )
    # Every read and internal wait from here on is act composition: only the
    # composed response below binds the caller's next numeric act.
    ctx._composing_act_response = getattr(
        ctx, "_composing_act_response", 0) + 1
    try:
        for phase in (
            _drain_previous_action_before_act,
            _read_pre_act_surface,
            _resolve_act_target,
            _snapshot_pre_act_sequence,
            _bind_numeric_act_target,
        ):
            refusal = phase(ctx, call)
            if refusal is not None:
                if refusal.get("_act_not_submitted") and (
                        _rendered_has_story_text(refusal) or refusal.get("status")):
                    _bind_returned_pending(ctx, refusal, deadline=call.act_deadline)
                # Not every pre-submission return is a refusal: the numeric
                # auto-advance recovery returns a SUCCESSFUL result carrying
                # the story that raced the click, and it used to skip the
                # finalize pass entirely.  Fleet R64's seven marker-less acts
                # are that return (~13.2 s: a 1.5 s state-change probe plus
                # the recovery drain's 10 s cap).  Refusals are unaffected --
                # every pass in finalize is a no-op on a result with no
                # transaction.
                return finalize_act_presentation(
                    refusal, actionable_decision=False)

        _submit_act(ctx, call)
        if call.finished:
            return call.result
        result = _settle_act_after_submission(ctx, call, call.result)

        # Polling and settle helpers may finish by restoring the consumed
        # request's bookkeeping even though the composed response visibly
        # carries a successor menu.  Numeric input belongs to what the caller
        # received, so make that response boundary authoritative.
        _bind_returned_pending(
            ctx,
            result,
            deadline=call.act_deadline,
            acted_request_id=call.consumed_request_id,
        )
        return finalize_act_presentation(
            result,
            actionable_decision=_returned_decision_is_actionable(
                ctx,
                result,
                call.read_request_id or call.pre_pending_id,
            ),
        )
    finally:
        ctx._composing_act_response = max(
            0, getattr(ctx, "_composing_act_response", 1) - 1)


def _recover_numeric_act_during_autoadvance(
    ctx: HandlerContext,
    pre_rendered: dict | None,
    pre_state_sig: tuple | None,
    params: dict,
) -> dict | None:
    """Recover act(N) when the numbered single-choice already auto-advanced.

    We intentionally do not forward the raw number to the shim here: during
    narration the live focus list may contain unrelated nav buttons. Instead,
    use the same post-advance recovery path a transient shim error would use.
    """
    result = {
        "success": False,
        "ok": False,
        "error": "No active choice request for choice resolution",
        "_numeric_autoadvance_race": True,
    }
    recovered = _recover_failed_act_after_advance(
        ctx,
        result,
        pre_rendered,
        pre_state_sig,
        params,
    )
    return recovered if _result_succeeded(recovered) else None


def handle_input_text(ctx: HandlerContext, params: dict) -> dict:
    """Type text for an input prompt."""
    text = params.get("text")
    if text is None:
        return {"error": "Missing 'text' parameter"}
    do_wait = _coerce_bool(params.get("wait"), True)
    transport_deadline = params.get("_transport_deadline")
    hook_payload = {"text": text, "wait": do_wait}
    if isinstance(transport_deadline, (int, float)):
        hook_payload["_transport_deadline"] = transport_deadline
    if ctx.hooks.before_action:
        ctx.hooks.before_action("input_text", hook_payload)
    if (
        isinstance(transport_deadline, (int, float))
        and time.time() >= transport_deadline
    ):
        return _transport_timeout_before_submission()
    input_method = ctx.client.input_text
    try:
        supports_deadline = "deadline" in inspect.signature(
            input_method).parameters
    except (TypeError, ValueError):
        supports_deadline = True
    input_kwargs: dict[str, Any] = {}
    if supports_deadline and isinstance(transport_deadline, (int, float)):
        input_kwargs["deadline"] = transport_deadline
    request_id = str(params.get("request_id") or "").strip() or None
    if request_id:
        input_kwargs["request_id"] = request_id
    result = input_method(text, **input_kwargs)
    if _result_succeeded(result):
        _run_after_input_text_hook(
            ctx, result, text, deadline=transport_deadline)
    if do_wait and _result_succeeded(result) and "wait" not in result:
        wait_params: dict[str, Any] = {
            "format": params.get("format", "text"),
        }
        if "timeout" in params:
            wait_params["timeout"] = params["timeout"]
        if isinstance(transport_deadline, (int, float)):
            wait_params["_result_deadline"] = transport_deadline
        wait_result = handle_wait(ctx, wait_params)
        result["wait"] = wait_result
        _promote_wait_output(result, wait_result)
    return result


def _run_after_input_text_hook(
    ctx: HandlerContext,
    result: dict,
    text: object,
    *,
    deadline: float | None = None,
) -> None:
    """Run a game-mod post-input hook when the shim advertises one."""
    command_name = "after_input_text"
    remaining = deadline - time.time() if deadline is not None else 3.0
    if remaining <= 0:
        return
    try:
        raw = _call_with_timeout(ctx.client.state, min(3.0, remaining)) or {}
    except Exception:
        raw = {}
    custom_commands = raw.get("custom_commands")
    if custom_commands is None:
        game_state = raw.get("game_state")
        if isinstance(game_state, dict):
            custom_commands = game_state.get("custom_commands")
    if (
        isinstance(custom_commands, list)
        and command_name not in custom_commands
    ):
        return
    stop_at = time.time() + 0.8
    if deadline is not None:
        stop_at = min(stop_at, deadline)
    hook_result: dict | None = None
    while time.time() < stop_at:
        try:
            hook_result = _run_transport_bounded_command(
                ctx,
                command_name,
                {"_transport_deadline": stop_at},
                text=str(text),
            )
        except Exception as exc:
            result["_after_input_text_error"] = str(exc)
            return
        if not isinstance(hook_result, dict):
            hook_result = {}
        if hook_result.get("auto_confirm_skipped") != "no_confirm_screen":
            break
        if not hasattr(ctx.client, "poll"):
            break
        remaining = stop_at - time.time()
        if remaining <= 0:
            break
        try:
            poll_preserving_events(
                ctx.client,
                timeout=min(0.2, remaining),
            )
        except Exception:
            break
    result["_after_input_text"] = hook_result
    if hook_result is None:
        return
    if hook_result.get("auto_confirmed"):
        _settle_after_input_text_hook_action(ctx, deadline=deadline)
        result["_auto_confirmed"] = True
        result["_auto_confirm_result"] = hook_result
        return
    skipped = hook_result.get("auto_confirm_skipped")
    if skipped:
        result["_auto_confirm_skipped"] = skipped
        return
    if not _result_succeeded(hook_result):
        error = str(hook_result.get("error") or "")
        if "unknown command" in error.lower():
            result.pop("_after_input_text", None)
            return
        result["_after_input_text_error"] = (
            error or "after_input_text hook failed"
        )


def _settle_after_input_text_hook_action(
    ctx: HandlerContext, *, deadline: float | None = None,
) -> None:
    """Give queued mod actions one interaction cycle before follow-up wait."""
    if not hasattr(ctx.client, "poll"):
        return
    stop_at = time.time() + 1.2
    if deadline is not None:
        stop_at = min(stop_at, deadline)
    while time.time() < stop_at:
        remaining = stop_at - time.time()
        if remaining <= 0:
            return
        try:
            raw = _call_with_timeout(
                ctx.client.state, min(3.0, remaining)) or {}
        except Exception:
            raw = {}
        pending = raw.get("pending_request") or {}
        if pending.get("type") in ("choice_request", "choices", "input_request"):
            _preserve_late_post_input_events(ctx, deadline=deadline)
            return
        remaining = stop_at - time.time()
        if remaining <= 0:
            return
        try:
            poll_preserving_events(
                ctx.client,
                timeout=min(0.2, remaining),
            )
        except Exception:
            return


def _preserve_late_post_input_events(
    ctx: HandlerContext, *, deadline: float | None = None,
) -> None:
    """Roadwarden can push input-answer narration after the next prompt.

    The input_text tool itself returns an acknowledgement, so the user's next
    wait() must still see any answer narration that arrives just after the
    confirm action exposes a fresh choice_request.
    """
    story_types = {"narration", "dialogue", "auto_skipped"}
    prefetched = getattr(ctx.client, "_prefetched_events", []) or []
    if any(event.get("type") in story_types for event in prefetched):
        return
    stop_at = time.time() + 1.0
    if deadline is not None:
        stop_at = min(stop_at, deadline)
    while time.time() < stop_at:
        remaining = stop_at - time.time()
        if remaining <= 0:
            return
        try:
            events = poll_preserving_events(
                ctx.client,
                timeout=min(0.2, remaining),
            )
        except Exception:
            return
        if any(event.get("type") in story_types for event in events or []):
            return


def handle_screenshot(ctx: HandlerContext, params: dict) -> dict:
    """Take a screenshot of the current game screen."""
    timeout = 10.0
    for key in ("_result_deadline", "_transport_deadline"):
        deadline = params.get(key)
        if isinstance(deadline, (int, float)):
            timeout = min(timeout, max(0.0, deadline - time.time()))
    if timeout <= 0:
        return {"error": "Screenshot deadline expired"}
    img = _call_with_timeout(ctx.client.screenshot, timeout)
    if not img:
        return {"error": "No screenshot available"}
    if ctx.hooks.format_screenshot:
        img = ctx.hooks.format_screenshot(img)
    return {"screenshot_base64": img}


def handle_state(ctx: HandlerContext, params: dict) -> dict:
    """Check the current game state."""
    brief = params.get("brief", True)
    deadline = params.get("_result_deadline")
    state_timeout = 3.0
    if isinstance(deadline, (int, float)):
        state_timeout = max(0.0, min(3.0, deadline - time.time()))
    raw = _call_with_timeout(ctx.client.state, state_timeout)
    # A falsy read means nobody answered (bridge down, or no game bound).
    # Remember that: an empty state and "no state" render the same, but
    # an act must not blame "narration in progress" for a dead bridge.
    state_unavailable = not raw
    transcript = raw.get("transcript") if isinstance(raw, dict) else None
    (
        preserve_action_nonce,
        preserve_action_nonces,
        preserve_action_id,
    ) = _timeline_boundary_action_owners(ctx, transcript)
    _observe_timeline_boundaries(
        ctx,
        transcript,
        preserve_action_nonce=preserve_action_nonce,
        preserve_action_nonces=preserve_action_nonces,
        preserve_action_id=preserve_action_id,
    )

    # Merge screen data.
    screen_timeout = 2.0
    if isinstance(deadline, (int, float)):
        screen_timeout = max(0.0, min(2.0, deadline - time.time()))
    screen = _get_screen(ctx, timeout=screen_timeout) if screen_timeout > 0 else None
    if screen:
        raw["screen"] = screen

    # Use the same canonical decision builder as wait(). A state render can be
    # promoted into an act result; if it exposes a numbered successor menu it
    # must carry that menu's exact actionable fingerprint, not merely labels.
    data = _build_decision_state_data(raw)
    if state_unavailable:
        data["_state_unavailable"] = True

    if ctx.hooks.after_state:
        data = ctx.hooks.after_state(data)

    fmt = params.get("format", "text")
    out = format_state_text(
        data,
        verbose=not brief,
        fmt=fmt,
        include_details=not params.get("_suppress_details", False),
        anomalies=ctx.anomaly_visibility,
    )
    out["_data"] = data
    _remember_delivered_actionable_screen(ctx, out, screen)
    _remember_rendered_decision(ctx, out)
    return out


_TRANSCRIPT_RETENTION_LIMIT = 2000


def _transcript_event_cursor(event: dict) -> str:
    """Stable opaque cursor for one retained transcript event."""
    payload = json.dumps(
        event, sort_keys=True, ensure_ascii=False, default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _rendered_transcript_entries(
    events: list[dict],
) -> list[tuple[int, str, str]]:
    entries = []
    contributor_snapshots: dict[str, list[str]] = {}
    contributor_keys: dict[str, str] = {}
    retained_contributors: set[str] = set()
    aggregate_key: tuple[str, ...] = ()
    aggregate_snapshot: list[str] = []
    overlay_schema_mode = ""
    overlay_baseline: list[str] = []
    for index, event in enumerate(events):
        rendered_event = event
        if event.get("type") == "choice_request":
            request_choices = [
                str(choice.get("label", choice.get("text", "")))
                if isinstance(choice, dict) else str(choice)
                for choice in (event.get("choices") or [])
            ]
            # Augmentation runs before Ren'Py renders the choice screen. A
            # following game_state with the same enabled choices is the
            # authoritative visibility pass for disabled/caption rows.
            for later in events[index + 1:]:
                later_type = later.get("type")
                if later_type in {
                    "choice_request", "input_request", "choice_resolved",
                    "input_resolved",
                    "request_resolved", "game_started", "game_resumed",
                }:
                    break
                if later_type != "game_state":
                    continue
                state_choices = [
                    str(choice) for choice in later.get("choices") or []
                ]
                if state_choices != request_choices:
                    continue
                if isinstance(later.get("full_items"), list):
                    rendered_event = dict(event)
                    rendered_event["full_items"] = later["full_items"]
                break
        if event.get("type") in {"game_started", "game_resumed"}:
            contributor_snapshots.clear()
            contributor_keys.clear()
            retained_contributors.clear()
            aggregate_key = ()
            aggregate_snapshot = []
            overlay_schema_mode = ""
            overlay_baseline = []
        if (
            event.get("type") == "screen_content"
            and event.get("passive_overlay_snapshot")
        ):
            generations = event.get("overlay_generations") or {}
            if not isinstance(generations, dict):
                generations = {}
            raw_by_screen = event.get("overlay_texts_by_screen")
            contributors = event.get("overlay_screens") or []
            rows = _passive_overlay_rows(event)
            if not rows and not contributors and not isinstance(raw_by_screen, dict):
                aggregate_key = ()
                aggregate_snapshot = []
                for tag in list(contributor_snapshots):
                    if tag not in retained_contributors:
                        contributor_snapshots.pop(tag, None)
                        contributor_keys.pop(tag, None)
                if contributor_snapshots:
                    overlay_schema_mode = "contributors"
                    overlay_baseline = [
                        row
                        for panel in contributor_snapshots.values()
                        for row in panel
                    ]
                else:
                    overlay_schema_mode = ""
                    overlay_baseline = []
                fresh = []
            elif isinstance(raw_by_screen, dict):
                groups = _passive_overlay_rows_by_screen(event)
                active_tags = {tag for tag, _rows in groups}
                if overlay_schema_mode == "aggregate":
                    fresh = _passive_overlay_delta(overlay_baseline, rows)
                    contributor_snapshots = {
                        tag: list(panel) for tag, panel in groups}
                    contributor_keys = {
                        tag: (
                            "{}@{}".format(tag, generations[tag])
                            if tag in generations else tag
                        )
                        for tag, _panel in groups
                    }
                    aggregate_key = ()
                    aggregate_snapshot = []
                else:
                    fresh = []
                    for tag, panel in groups:
                        key = (
                            "{}@{}".format(tag, generations[tag])
                            if tag in generations else tag
                        )
                        prior_key = contributor_keys.get(tag)
                        previous = (
                            contributor_snapshots.get(tag, [])
                            if prior_key == key else []
                        )
                        if (
                            not panel
                            and prior_key == key
                            and tag in (
                                event.get("overlay_retained_screens") or [])
                        ):
                            continue
                        if (
                            panel
                            and len(panel) < len(previous)
                            and previous[:len(panel)] == panel
                        ):
                            continue
                        fresh.extend(_passive_overlay_delta(previous, panel))
                        contributor_snapshots[tag] = list(panel)
                        contributor_keys[tag] = key
                overlay_schema_mode = "contributors"
                overlay_baseline = list(rows)
                retained_contributors.difference_update(active_tags)
                retained_contributors.update(
                    str(tag)
                    for tag in (event.get("overlay_retained_screens") or [])
                    if str(tag) in active_tags
                )
            else:
                key = tuple(
                    "{}@{}".format(tag, generations[tag])
                    if tag in generations else str(tag)
                    for tag in contributors
                )
                if overlay_schema_mode == "contributors":
                    fresh = _passive_overlay_delta(overlay_baseline, rows)
                    contributor_snapshots.clear()
                    contributor_keys.clear()
                    retained_contributors.clear()
                else:
                    previous = aggregate_snapshot if aggregate_key == key else []
                    if (
                        rows
                        and len(rows) < len(previous)
                        and previous[:len(rows)] == rows
                    ):
                        fresh = []
                    else:
                        fresh = _passive_overlay_delta(previous, rows)
                aggregate_key = key
                aggregate_snapshot = list(rows)
                overlay_baseline = list(rows)
                overlay_schema_mode = "aggregate"
            durable_delta = event.get("passive_overlay_delta")
            if isinstance(durable_delta, list):
                # The bridge owns cross-lifecycle occurrence provenance. Keep
                # the full snapshot above as the local baseline, but render
                # exactly its durable rows (including an empty resume delta)
                # instead of recomputing restored scrollback as fresh text.
                fresh = [str(row) for row in durable_delta]
            rendered_event = dict(event)
            rendered_event["texts"] = fresh
            # These are historical text snapshots, not decision points.
            # Re-rendering their copied interaction surface produced a growing
            # INFO/button block for every appended terminal line.
            rendered_event.pop("interactions", None)
            rendered_event.pop("buttons", None)
        formatted = format_event(
            rendered_event, colour=False, quiet=False, verbose=False,
        )
        if formatted:
            entries.append((index, _transcript_event_cursor(event), formatted))
    return entries


def _recent_transcript_entries(
    ctx: HandlerContext,
    last: int,
) -> tuple[list[dict], list[tuple[int, str, str]], bool]:
    """Widen until ``last`` rendered events or retained-history start."""
    fetch = min(max(last * 10, 200), _TRANSCRIPT_RETENTION_LIMIT)
    while True:
        events = ctx.client.transcript(last=fetch) or []
        entries = _rendered_transcript_entries(events)
        reached_start = (
            len(events) < fetch
            or fetch >= _TRANSCRIPT_RETENTION_LIMIT
        )
        if (
            len(entries) >= last
            or reached_start
            or fetch >= _TRANSCRIPT_RETENTION_LIMIT
        ):
            return events, entries, reached_start
        fetch = min(fetch * 2, _TRANSCRIPT_RETENTION_LIMIT)


def handle_transcript(ctx: HandlerContext, params: dict) -> dict:
    """Review rendered history, optionally paging around an opaque cursor."""
    try:
        last = int(params.get("last", 20))
    except (TypeError, ValueError):
        last = 20
    if last <= 0:
        last = 20
    before = str(params.get("before") or "").strip() or None
    after = str(params.get("after") or "").strip() or None
    if before and after:
        return {"error": "Use either before or after, not both."}

    if before or after:
        # The bridge retains at most 2,000 events, so a cursor lookup is
        # bounded even though last=0 requests the whole retained transcript.
        events = ctx.client.transcript(last=0) or []
        raw_cursors = [_transcript_event_cursor(event) for event in events]
        cursor = before or after
        try:
            pivot = raw_cursors.index(cursor)
        except ValueError:
            return {
                "error": (
                    "Transcript cursor is no longer in retained history. "
                    "Request the recent transcript again to obtain a fresh cursor."
                )
            }
        entries = _rendered_transcript_entries(events)
        if before:
            candidates = [entry for entry in entries if entry[0] < pivot]
            selected = candidates[-last:]
        else:
            candidates = [entry for entry in entries if entry[0] > pivot]
            selected = candidates[:last]
        reached_start = True
    else:
        events, entries, reached_start = _recent_transcript_entries(ctx, last)
        selected = entries[-last:]

    if not selected:
        return {"text": "(empty transcript)"}

    first_index, first_cursor, _first_text = selected[0]
    last_index, last_cursor, _last_text = selected[-1]
    return {
        "text": "\n".join(entry[2] for entry in selected),
        "first_cursor": first_cursor,
        "last_cursor": last_cursor,
        "has_more_before": bool(
            any(entry[0] < first_index for entry in entries)
            or not reached_start
        ),
        "has_more_after": any(entry[0] > last_index for entry in entries),
    }


def handle_back(ctx: HandlerContext, params: dict) -> dict:
    """Close the current overlay/modal screen; does not move dialogue history."""
    return _handle_story_navigation_command(
        ctx,
        params,
        tool_name="back",
        command_name="back",
    )


# Child-process time reserved for the game/slot setup phase after the
# bridge answers, and for bridge readiness plus that phase when the bridge
# still has to be started (see handle_launch and _launch_startup_reserve).
_LAUNCH_SETUP_RESERVE_S = 15.0
_LAUNCH_STARTUP_RESERVE_S = BRIDGE_READINESS_TIMEOUT_S + _LAUNCH_SETUP_RESERVE_S


def _launch_startup_reserve(ctx: HandlerContext) -> float:
    """Child startup time to reserve ahead of the connect window.

    A bridge that already answers skips readiness entirely, so only the
    setup phase is reserved; otherwise the full readiness wait is. The
    probe is bounded and any failure counts as "not up".
    """
    is_up = getattr(ctx.client, "is_up", None)
    if not callable(is_up):
        return _LAUNCH_STARTUP_RESERVE_S
    try:
        up = bool(is_up(timeout=1.0))
    except Exception:
        up = False
    return _LAUNCH_SETUP_RESERVE_S if up else _LAUNCH_STARTUP_RESERVE_S
# The CLI launcher's own connect window when no --timeout is passed.
_LAUNCHER_DEFAULT_CONNECT_S = 90.0

_BACK_ALL_MAX_STEPS = 8
# Let a queued Hide/Return run (screen timer, ~10 ms) before the next
# back re-checks what is showing; without it the second step sees the
# same overlay and queues a duplicate.
_BACK_ALL_SETTLE_S = 0.25


def handle_back_all(ctx: HandlerContext, params: dict) -> dict:
    """Close overlay/menu screens until the world screen is back.

    A bounded loop over the shim's ``back`` with ``overlays_only`` set, so
    the shim refuses (``nothing_to_close``) instead of sending a bare
    Return() into a say interaction once nothing dismissable is showing.
    Any other refusal (a custom called screen, a modal the shim cannot
    safely close, a live choice) stops the loop and is reported as is.
    Reverses navigation only: it never runs a forward action.
    """
    if ctx.hooks.before_action:
        ctx.hooks.before_action("back_all", dict(params or {}))
    step_params = dict(params or {})
    step_params.pop("command_nonce", None)
    closed = 0
    stopped_by = "max_steps"
    refusal: dict | None = None
    deadline = params.get("_result_deadline")
    if not isinstance(deadline, (int, float)):
        deadline = params.get("_transport_deadline")
    if not isinstance(deadline, (int, float)):
        deadline = None
    if deadline is not None:
        # _run_transport_bounded_command reads _transport_deadline only.
        step_params.setdefault("_transport_deadline", deadline)

    def _remaining() -> float | None:
        return None if deadline is None else deadline - time.time()

    for _ in range(_BACK_ALL_MAX_STEPS):
        remaining = _remaining()
        if remaining is not None and remaining <= 0:
            stopped_by = "deadline"
            break
        result = _run_transport_bounded_command(
            ctx, "back", step_params, overlays_only=True)
        ok = bool(result.get("success", result.get("ok")))
        if not ok:
            if result.get("nothing_to_close"):
                stopped_by = "nothing_to_close"
            else:
                stopped_by = "refusal"
                refusal = result
            break
        closed += 1
        if _BACK_ALL_SETTLE_S:
            remaining = _remaining()
            time.sleep(_BACK_ALL_SETTLE_S if remaining is None
                       else max(0.0, min(_BACK_ALL_SETTLE_S, remaining)))
    remaining = _remaining()
    if remaining is not None and remaining <= 0:
        # No fresh state read past the deadline: report what was done.
        out = {"ok": True, "success": True}
        deadline_note = (
            "back_all ran out of its time budget; the current screen was "
            "not re-read. Call state() to see where you are.")
    else:
        state_params = {"brief": True}
        if deadline is not None:
            state_params["_result_deadline"] = deadline
        out = handle_state(ctx, state_params)
        deadline_note = None
    out["closed"] = closed
    out["stopped_by"] = stopped_by
    summary = "back_all: closed {} screen{}.".format(
        closed, "" if closed == 1 else "s")
    if refusal is not None:
        out["success"] = False
        out["ok"] = False
        out["error"] = str(refusal.get("error") or "back refused")
        summary += " Stopped by a refusal."
    elif stopped_by == "max_steps":
        out["warning"] = (
            "back_all stopped after {} steps with screens still showing; "
            "something reopens on close. Use state() and act() on the "
            "screen's own Close control.".format(_BACK_ALL_MAX_STEPS))
    if deadline_note:
        out["warning"] = deadline_note
    previous_text = out.get("text")
    out["text"] = summary if not previous_text else (
        summary + "\n\n" + str(previous_text))
    if ctx.hooks.after_command:
        out = ctx.hooks.after_command("back", out)
    return out


def _handle_story_navigation_command(
    ctx: HandlerContext,
    params: dict,
    *,
    tool_name: str,
    command_name: str,
) -> dict:
    """Run one normal story navigation command through the shim."""
    if ctx.hooks.before_action:
        ctx.hooks.before_action(tool_name, dict(params or {}))
    result = _run_transport_bounded_command(ctx, command_name, params)
    result = _recover_advance_menu_race(
        ctx, result, params, command_name=command_name)
    if (
        isinstance(result, dict)
        and result.get("nonce")
        and not result.get("action_nonce")
    ):
        result.setdefault("command_nonce", result["nonce"])
        result.setdefault("nonce_kind", "command")
        result.setdefault(
            "nonce_guidance",
            "This command nonce can only retry the same {} call; it is not an "
            "act transaction nonce and must not be passed to "
            "wait(action_nonce=...).".format(tool_name),
        )
    if ctx.hooks.after_command:
        result = ctx.hooks.after_command(command_name, result)
    return result


def _recover_advance_menu_race(
    ctx: HandlerContext,
    result: dict,
    params: dict,
    *,
    command_name: str,
) -> dict:
    """Attach the choice that became active while advance was queued."""
    if command_name != "advance" or not isinstance(result, dict):
        return result
    error = str(result.get("error") or "")
    if not any(message in error for message in (
        "Cannot advance while a game menu is active.",
        "Cannot advance while a choice is active.",
    )):
        return result

    # The command-result poll and its final authoritative /state lookup both
    # cache the pending request they observed.  Prefer that exact snapshot:
    # the command may have consumed the entire caller deadline, but rendering
    # already-observed state costs no I/O and closes the race that caused the
    # refusal in the first place.
    cached_pending = None
    fresh_pending = getattr(ctx.client, "_fresh_cached_pending", None)
    if callable(fresh_pending):
        try:
            cached_pending = fresh_pending()
        except Exception:
            cached_pending = None
    current = None
    if isinstance(cached_pending, dict):
        raw = {
            "status": "waiting_for_input",
            "pending_request": cached_pending,
        }
        data = _build_decision_state_data(raw)
        cached_snapshot = getattr(
            ctx.client, "last_actionable_snapshot", None,
        )
        if isinstance(cached_snapshot, dict):
            data["_actionable_snapshot"] = cached_snapshot
        if ctx.hooks.after_state:
            data = ctx.hooks.after_state(data)
        current = format_state_text(
            data,
            verbose=True,
            fmt=params.get("format", "text"),
            anomalies=ctx.anomaly_visibility,
        )
        current["_data"] = data

    deadline = params.get("_transport_deadline")
    if not isinstance(deadline, (int, float)):
        deadline = params.get("_result_deadline")
    if not isinstance(deadline, (int, float)):
        deadline = time.time() + 2.0
    if current is None and time.time() >= float(deadline):
        return result

    if current is None:
        state_params = {
            "brief": False,
            "_result_deadline": float(deadline),
        }
        if "format" in params:
            state_params["format"] = params["format"]
        try:
            current = handle_state(ctx, state_params)
        except Exception:
            return result
    if not current.get("pending"):
        return result

    # The refusal remains a refusal: advance did not mutate the game. Expose
    # only the live decision that caused it so the next numeric act is bound to
    # the authoritative request rather than forcing a separate state() call.
    _promote_wait_output(result, current)
    _remember_rendered_decision(ctx, result)
    result["error"] = error
    result["ok"] = False
    result["success"] = False
    result["_advance_menu_race_exposed"] = True
    return result


def _transport_timeout_before_submission() -> dict:
    return {
        "ok": False,
        "success": False,
        "reason": "transport_timeout_before_submission",
        "error": "Did not mutate the game - the MCP response budget expired.",
    }


def _run_transport_bounded_command(
    ctx: HandlerContext, command_name: str, params: dict, **args: Any,
) -> dict:
    deadline = params.get("_transport_deadline")
    if isinstance(deadline, (int, float)) and time.time() >= deadline:
        return _transport_timeout_before_submission()
    command = ctx.client.command
    try:
        supports_deadline = any(
            parameter.name == "_deadline"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in inspect.signature(command).parameters.values()
        )
    except (TypeError, ValueError):
        supports_deadline = True
    command_nonce = params.get("command_nonce")
    command_args = dict(args)
    if command_nonce:
        command_args["_nonce"] = command_nonce
    if supports_deadline and isinstance(deadline, (int, float)):
        return command(
            command_name,
            _deadline=deadline,
            **command_args,
        )
    if isinstance(deadline, (int, float)):
        return {
            **_transport_timeout_before_submission(),
            "error": "Did not mutate the game - bounded command unsupported.",
        }
    return command(command_name, **command_args)


def handle_advance(ctx: HandlerContext, params: dict) -> dict:
    """Advance one dialogue interaction without enabling auto-forward."""
    return _handle_story_navigation_command(
        ctx,
        params,
        tool_name="advance",
        command_name="advance",
    )


def handle_rewind(ctx: HandlerContext, params: dict) -> dict:
    """Move one normal Ren'Py rollback/checkpoint backward."""
    return _handle_story_navigation_command(
        ctx,
        params,
        tool_name="rewind",
        command_name="rewind",
    )


def handle_replay(ctx: HandlerContext, params: dict) -> dict:
    """Roll forward after a prior story rollback."""
    return _handle_story_navigation_command(
        ctx,
        params,
        tool_name="replay",
        command_name="replay",
    )


def _save_slot_from_display_name(name: str) -> str:
    """Build a stable Ren'Py slot alias for name-only saves."""
    text = str(name or "").strip().lower()
    parts = []
    last_dash = False
    for ch in text:
        if ch.isalnum():
            parts.append(ch)
            last_dash = False
        elif not last_dash:
            parts.append("-")
            last_dash = True
    slug = "".join(parts).strip("-") or "checkpoint"
    return "named-" + slug[:72].strip("-")


def handle_save(ctx: HandlerContext, params: dict) -> dict:
    """Save the game."""
    slot = params.get("slot")
    display_name = params.get("name")
    if slot or display_name:
        # A name without a slot is a named save — honor it instead of
        # silently dropping the name and falling through to the default slot.
        kwargs = {}
        if not slot and display_name:
            slot = _save_slot_from_display_name(str(display_name))
        if slot:
            kwargs["slot"] = str(slot)
        if display_name:
            kwargs["name"] = display_name
        result = _run_transport_bounded_command(
            ctx, "save", params, **kwargs)
    else:
        result = _run_transport_bounded_command(ctx, "save", params)
    if result.get("reason") == "transport_timeout_before_submission":
        return result
    deadline = params.get("_transport_deadline")
    if isinstance(deadline, (int, float)) and time.time() >= deadline:
        return result
    if ctx.hooks.after_command:
        result = ctx.hooks.after_command("save", result)
    return result


def handle_load(ctx: HandlerContext, params: dict) -> dict:
    """Load a saved game."""
    slot = params.get("slot")
    if slot:
        result = _run_transport_bounded_command(
            ctx, "load", params, slot=slot)
    else:
        result = _run_transport_bounded_command(ctx, "load", params)
    if result.get("reason") == "transport_timeout_before_submission":
        return result
    if _result_succeeded(result) or result.get("acceptance_unknown"):
        _reconcile_applied_timeline_jump(ctx)
    deadline = params.get("_transport_deadline")
    if isinstance(deadline, (int, float)) and time.time() >= deadline:
        return result
    if ctx.hooks.after_command:
        result = ctx.hooks.after_command("load", result)
    return result


def handle_auto_skip(ctx: HandlerContext, params: dict) -> dict:
    """Toggle auto-skip for single-choice menus."""
    enabled = params.get("enabled")
    if enabled is None:
        # Query mode is implemented by the shim's "set" command when value
        # is omitted; there is no generic "get" command.
        result = _run_transport_bounded_command(
            ctx, "set", params, key="auto_skip_single_choice")
        mode = "query"
    else:
        result = _run_transport_bounded_command(
            ctx, "set", params, key="auto_skip_single_choice", value=enabled)
        mode = "update"

    # ``set`` is the shim transport primitive, not the public operation. Keep
    # it for diagnostics without making a read-only auto_skip() call look like
    # a mutation to agents and API consumers.
    if isinstance(result, dict):
        result = dict(result)
        result["transport_command"] = result.get("command", "set")
        result["command"] = "auto_skip"
        result["mode"] = mode
    return result


# ---------------------------------------------------------------------------
# Lifecycle handlers (launch, stop, games, set_profile)
# ---------------------------------------------------------------------------

def _cli_invocation() -> "tuple[list[str], str | None]":
    """Base command + cwd for re-invoking the vnflight CLI as a subprocess.

    Deployment-aware: the built single-file artifact IS the CLI, while the
    package checkout has vnflight.py at the repo root (falling back to
    ``python -m vnflight.cli`` when only src/ is importable).  The old
    path math assumed the package layout — in a flat single-file
    deployment it pointed two directories above the artifact, so MCP
    launch/stop/games/set_profile silently broke when running from
    a downloaded vnflight.py.
    """
    import os
    import sys
    artifact = _single_file_artifact()
    if artifact is not None:
        return [sys.executable, str(artifact)], str(artifact.parent)
    package_dir = os.path.dirname(os.path.abspath(__file__))   # src/vnflight
    repo_root = os.path.dirname(os.path.dirname(package_dir))
    # The committed artifact lives in dist/; a root-level copy is the
    # older layout, still honoured.  Either way the subprocess runs with
    # the repo root as cwd so vnflight.json and games resolve from there.
    for vnflight_py in (
        os.path.join(repo_root, "dist", "vnflight.py"),
        os.path.join(repo_root, "vnflight.py"),
    ):
        if os.path.exists(vnflight_py):
            return [sys.executable, vnflight_py], repo_root
    # Installed package / bare src checkout: run the CLI module directly
    # (there is no vnflight/__main__.py, so `-m vnflight` would fail).
    return [sys.executable, "-m", "vnflight.cli"], None


def _run_cli(ctx: HandlerContext, *args: str, timeout: float = 90) -> dict:
    """Run a vnflight CLI command via hook or default subprocess."""
    if ctx.hooks.run_cli:
        return ctx.hooks.run_cli(*args, timeout=timeout)
    # Default: run vnflight as a subprocess.
    import os
    import subprocess
    base_cmd, cwd = _cli_invocation()
    cmd = base_cmd + list(args)
    try:
        result = subprocess.run(
            # The child prints UTF-8 (PYTHONIOENCODING below); decoding
            # with the console codec turned "✓" into mojibake on Windows.
            cmd, capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        output = result.stdout.strip()
        if result.returncode == 0:
            return {"ok": True, "output": output}
        error = result.stderr.strip() if result.stderr else ""
        return {
            "error": error or output or "Command failed",
            "output": output,
            "stderr": error,
        }
    except subprocess.TimeoutExpired:
        return {
            "error": f"Command timed out after {timeout}s",
            "reason": "cli_timeout",
        }
    except Exception as e:
        return {"error": str(e)}


def handle_launch(ctx: HandlerContext, params: dict) -> dict:
    """Launch a visual novel game."""
    game_id = params.get("game_id")
    if not game_id:
        return {"error": "Missing 'game_id' parameter"}
    raw_timeout = params.get("timeout")
    try:
        timeout = (
            max(0.0, float(raw_timeout))
            if raw_timeout is not None else None
        )
    except (TypeError, ValueError):
        timeout = None
    if timeout is not None and timeout <= 0:
        return {"error": "Launch timeout must be greater than zero."}
    deadline = params.get("_transport_deadline")
    # Keep the receipt-producing child and the parent-owned profile command
    # inside one transport budget. The child needs the startup reserve
    # (bridge readiness + setup) beyond its connect window; the parent needs
    # one bounded profile POST. Without a transport deadline (CLI, tests)
    # the budget is simply what the requested connect window needs.
    parent_profile_headroom = 10.0
    startup_reserve = _launch_startup_reserve(ctx)
    remaining = (
        float(deadline) - time.time()
        if isinstance(deadline, (int, float))
        else (
            (timeout if timeout is not None else _LAUNCHER_DEFAULT_CONNECT_S)
            + startup_reserve + 5.0 + parent_profile_headroom
        )
    )
    if remaining <= _LAUNCH_SETUP_RESERVE_S + parent_profile_headroom:
        return _transport_timeout_before_submission()
    # A child killed during bridge readiness never prints its receipt, and
    # a connect bound cannot protect that phase: refuse up front when the
    # startup reserve the launch actually needs cannot fit the budget.
    if remaining - parent_profile_headroom < startup_reserve + 1.0:
        return {
            "error": (
                f"Only {remaining:g}s of response budget remain and this "
                f"launch needs about {startup_reserve:g}s of startup before "
                "it can even connect. Retry with a fresh call, or use the "
                "hub launcher for asynchronous game boots."
            ),
            "reason": "launch_startup_reserve_exceeds_budget",
        }
    if params.get("_stop_existing") and ctx.client.slot_prefix:
        stop_result = handle_stop(ctx, params)
        if not _result_succeeded(stop_result):
            return {
                **stop_result,
                "ok": False,
                "success": False,
                "reason": stop_result.get("reason") or "launch_pre_stop_failed",
                "error": (
                    "Did not launch the new game because the prior game "
                    "could not be stopped: {}"
                ).format(
                    stop_result.get("error")
                    or stop_result.get("output")
                    or "unknown stop failure"
                ),
            }
        _reconcile_applied_timeline_jump(ctx, detach_slot=True)
        # Stopping the old game can take its bridge down too, and it spent
        # budget: measure the startup reserve again and re-check it.
        startup_reserve = _launch_startup_reserve(ctx)
        if isinstance(deadline, (int, float)):
            remaining = float(deadline) - time.time()
            if remaining <= _LAUNCH_SETUP_RESERVE_S + parent_profile_headroom:
                return {
                    "ok": False,
                    "success": False,
                    "reason": "transport_timeout_after_pre_stop",
                    "error": (
                        "The prior game was stopped, but the response budget "
                        "expired before the new game could be launched."
                    ),
                }
    # The child must finish slot registration before its subprocess timeout,
    # and the subprocess must return before the MCP transport deadline. This
    # keeps a timeout unambiguous: it cannot kill the CLI after the game was
    # launched but before the successful launch receipt was printed.
    if remaining - parent_profile_headroom < startup_reserve + 1.0:
        # Reached only after a pre-stop (the first check refused earlier
        # otherwise): the stop consumed budget or took the bridge down.
        return {
            "ok": False,
            "success": False,
            "reason": "launch_startup_reserve_exceeds_budget_after_pre_stop",
            "error": (
                f"The prior game was stopped, but only {remaining:g}s of "
                f"response budget remain and the new launch needs about "
                f"{startup_reserve:g}s of startup before it can connect. "
                "Retry with a fresh call."
            ),
        }
    launch_timeout = min(
        (timeout if timeout is not None else _LAUNCHER_DEFAULT_CONNECT_S)
        + startup_reserve + 5.0,
        remaining - parent_profile_headroom,
    )
    # launch_game's connect timer starts after bridge/game startup (up to
    # BRIDGE_READINESS_TIMEOUT_S for the bridge alone when it must be
    # started) and is followed by a bounded setup phase. Reserve enough
    # child process time for all of it instead of making the connect and
    # subprocess deadlines coincide under a slow bridge bootstrap.
    max_connect_timeout = max(0.0, launch_timeout - startup_reserve)
    if timeout is not None and timeout > max_connect_timeout:
        return {
            "error": (
                f"Requested launch timeout {timeout:g}s exceeds the "
                f"{max_connect_timeout:g}s synchronous transport window. "
                "Use the hub launcher for longer asynchronous game boots."
            ),
            "reason": "launch_timeout_exceeds_transport_window",
        }
    # An omitted timeout keeps the launcher's own default connect window
    # unless a transport deadline makes that window longer than the child
    # may live; then the child gets the bounded window explicitly (floored
    # at one second, so a short budget still starts the game and returns
    # its slot receipt instead of being killed mid-connect).
    game_timeout = timeout
    if (
        timeout is None
        and isinstance(deadline, (int, float))
        and max_connect_timeout < _LAUNCHER_DEFAULT_CONNECT_S
    ):
        game_timeout = max(1.0, max_connect_timeout)
    # Pass the client's bridge URL so the game connects to the right bridge.
    ba = _bridge_args(ctx)
    import secrets
    reservation_token = secrets.token_urlsafe(24)
    cli_args = [
        "--yes", "--quiet", "--json", *ba,
        "launch", game_id, "--auto",
        "--reservation-token", reservation_token,
        # The parent owns all MCP profile work. The child must publish its
        # slot receipt before a potentially slow profile command.
        "--defer-default-profile",
    ]
    if game_timeout is not None:
        cli_args.extend(("--timeout", str(game_timeout)))
    debug_flag = params.get("debug")
    if debug_flag is True:
        cli_args.append("--debug")
    elif debug_flag is False:
        cli_args.append("--no-debug")
    result = _run_cli(ctx, *cli_args, timeout=launch_timeout)
    import json
    raw_receipt = result.get("output") or result.get("error")
    try:
        receipt = json.loads(raw_receipt) if raw_receipt else None
    except (TypeError, ValueError):
        receipt = None
    if isinstance(receipt, dict):
        if receipt.get("success"):
            result = {"ok": True, **receipt}
        else:
            partial_slot = receipt.get("slot_id")
            if receipt.get("partial_launch") and partial_slot is not None:
                _reconcile_applied_timeline_jump(ctx, detach_slot=True)
                ctx.client.slot_prefix = f"/{partial_slot}"
                ctx.client.token = reservation_token
                try:
                    ctx.client._dynamic_slot_selector = None
                    ctx.client._dynamic_slot_resolved_at = 0.0
                except Exception:
                    pass
                receipt = {**receipt, "recovery_attached": True}
            return receipt
    if not result.get("ok"):
        if result.get("reason") == "cli_timeout":
            import hashlib
            return {
                "ok": False,
                "success": False,
                "partial_launch": True,
                "slot_id": None,
                "game_id": game_id,
                "mutation_may_have_applied": True,
                "retry_safe": False,
                "reason": "launch_acceptance_unknown",
                "reservation_id": hashlib.sha256(
                    reservation_token.encode("utf-8")
                ).hexdigest()[:16],
                "error": (
                    "The launch process exceeded its response budget after "
                    "it may have started the game. Do not retry launch; "
                    "inspect the hub for a slot matching the returned "
                    "reservation ID."
                ),
            }
        return result

    slot_id = result.get("slot_id")
    if slot_id is None:
        return {
            "error": "Launch succeeded without a slot receipt.",
            "reason": "launch_slot_receipt_missing",
        }

    def partial_launch(error: str, reason: str) -> dict:
        return {
            "ok": False,
            "success": False,
            "partial_launch": True,
            "slot_id": slot_id,
            "error": error,
            "reason": reason,
        }

    _reconcile_applied_timeline_jump(ctx, detach_slot=True)
    # Bind locally to the exact slot named by the child receipt. This performs
    # no bridge read and cannot race another game's registration.
    ctx.client.slot_prefix = f"/{slot_id}"
    try:
        ctx.client._dynamic_slot_selector = None
        ctx.client._dynamic_slot_resolved_at = 0.0
    except Exception:
        pass

    if isinstance(deadline, (int, float)) and time.time() >= deadline:
        return partial_launch(
            "Game launched, but the client attachment budget expired. "
            f"Slot {slot_id} remains active.",
            "launch_attachment_timeout",
        )

    # Adopt the exact launch credential before any profile command. Token
    # resolution is local persistent-state I/O; failures are explicit partial
    # launches rather than a silent fall-through to a possibly wrong token.
    try:
        # Adopt the SLOT token the launch just minted and stored — it is the
        # credential guaranteed to open this game's slot, even when the
        # client's current/stored ADMIN token is stale (an open-mode bridge
        # accepts admin ops from anyone, so a stale admin token survives
        # launches unnoticed while locking this client out of the reserved
        # slot; likewise a harness-started bridge whose admin token this
        # machine never held). Adopt it even over an existing client token:
        # admin-only ops that then 403 fall back through the client's
        # _request token refresh. Admin-token adoption remains the fallback
        # for tokenless clients. Must run BEFORE auto_select_slot, which
        # needs a working token to list slots on a require-token bridge.
        ctx.client.token = reservation_token
    except Exception as exc:
        return partial_launch(
            f"Game launched in slot {slot_id}, but its token could not be "
            f"adopted: {exc}",
            "launch_token_adoption_failed",
        )

    if result.get("profile_error"):
        profile_name = result.get("profile_name") or "default"
        return partial_launch(
            f"Game launched in slot {slot_id}, but profile {profile_name!r} "
            f"failed: {result['profile_error']}",
            "launch_profile_failed",
        )

    profile = params.get("profile")
    if not profile:
        config = _load_config() or {}
        game_config = (config.get("games") or {}).get(game_id) or {}
        profile = game_config.get("default_profile")
    if profile and result.get("profile_applied") == profile:
        # The quiet JSON child applied the default before emitting its single
        # launch receipt. Do not send the same profile a second time.
        result["profile"] = profile
    elif profile:
        profile_params = {"profile": profile}
        if isinstance(deadline, (int, float)):
            if time.time() >= deadline:
                return partial_launch(
                    f"Game launched in slot {slot_id}, but profile "
                    f"{profile!r} was not applied before timeout.",
                    "launch_profile_timeout",
                )
            profile_params["_transport_deadline"] = deadline
        pr = handle_set_profile(ctx, profile_params)
        if pr.get("ok"):
            result["profile"] = profile
        else:
            return partial_launch(
                f"Game launched in slot {slot_id}, but profile {profile!r} "
                f"failed: {pr.get('error', 'unknown error')}",
                "launch_profile_failed",
            )
    return result


def _bridge_args(ctx: HandlerContext) -> list[str]:
    """Return --bridge URL args so CLI subprocess uses the same bridge."""
    bridge_url = getattr(ctx.client, "bridge_url", None)
    if bridge_url:
        return ["--bridge", bridge_url]
    return []


def handle_stop(ctx: HandlerContext, params: dict) -> dict:
    """Stop a running game."""
    game = params.get("game")
    cmd_args = ["--yes", *_bridge_args(ctx), "stop"]
    if game:
        cmd_args.append(game)
    deadline = params.get("_transport_deadline")
    remaining = (
        float(deadline) - time.time()
        if isinstance(deadline, (int, float)) else 15.0
    )
    if remaining <= 0:
        return _transport_timeout_before_submission()
    return _run_cli(ctx, *cmd_args, timeout=min(15.0, remaining))


def handle_games(ctx: HandlerContext, params: dict) -> dict:
    """List available games."""
    result = _run_cli(ctx, "--json", "games", timeout=10)
    if result.get("ok"):
        import json
        try:
            return {"games": json.loads(result["output"])}
        except Exception:
            return {"games": result["output"]}
    return result


def handle_set_act_wait(ctx: HandlerContext, params: dict) -> dict:
    """Toggle whether act() automatically follows up with wait()."""
    if "enabled" in params:
        ctx.act_wait = bool(params["enabled"])
        # Persist on client so it survives across handler calls.
        ctx.client._act_wait = ctx.act_wait
    return {"act_wait": ctx.act_wait}


def handle_set_profile(ctx: HandlerContext, params: dict) -> dict:
    """Apply a timing profile to the current game."""
    profile = params.get("profile")
    if not profile:
        return {"error": "Missing 'profile' parameter"}
    config = _load_config() or {}
    profiles = config.get("profiles") or {}
    if profile == "default":
        # A caller-supplied nonce identifies the mutating profile attempt.
        # Do not let the prerequisite query claim it: bridge idempotency binds
        # each nonce to one exact command signature, so reusing it here would
        # make the subsequent set look like a conflicting command.
        defaults_params = {
            key: value for key, value in params.items()
            if key != "command_nonce"
        }
        defaults_result = _run_transport_bounded_command(
            ctx, "get_defaults", defaults_params)
        if defaults_result.get("reason") == (
            "transport_timeout_before_submission"
        ):
            return defaults_result
        if not defaults_result.get("success") and not defaults_result.get("ok"):
            error = (
                defaults_result.get("error")
                or defaults_result.get("message")
                or "get_defaults_unsupported"
            )
            return {"error": f"Cannot query defaults: {error}",
                    "profile": profile}
        defaults = dict(defaults_result.get("defaults") or {})
        profile_keys = set()
        for values in profiles.values():
            if isinstance(values, dict):
                profile_keys.update(values.keys())
        if profile_keys:
            defaults = {k: v for k, v in defaults.items() if k in profile_keys}
        if not defaults:
            return {
                "ok": True,
                "profile": profile,
                "applied": [],
                "message": "No profile-relevant defaults to restore.",
            }
        changes = defaults
    elif profile not in profiles:
        available = ", ".join(sorted(profiles)) or "(none)"
        return {"error": f"Unknown profile: {profile!r}. Available: {available}"}
    else:
        changes = dict(profiles.get(profile) or {})
        if not changes:
            return {
                "ok": True,
                "profile": profile,
                "applied": [],
                "message": f"Profile {profile!r} has nothing to apply.",
            }

    # Use the live client instead of shelling out to the CLI. Harness-spawned
    # MCP servers already hold the per-slot token used by act()/wait(); a CLI
    # subprocess may not, which made manual set_profile fail on locked slots.
    result = _run_transport_bounded_command(
        ctx, "set", params, changes=changes)
    if result.get("reason") == "transport_timeout_before_submission":
        return result
    success = bool(result.get("success") or result.get("ok"))
    if not success:
        errors = result.get("errors")
        if isinstance(errors, list) and errors:
            error = "; ".join(str(e) for e in errors)
        else:
            error = result.get("error") or result.get("message") or "Set failed"
        return {
            **result,
            "error": f"Set failed: {error}",
            "profile": profile,
        }

    profile_warning = None
    try:
        delay = changes.get("auto_advance_delay")
        if delay is not None and profile != "default":
            enabled = _coerce_bool(changes.get("auto_advance", True))
            deadline = params.get("_transport_deadline")
            if isinstance(deadline, (int, float)) and time.time() >= deadline:
                profile_warning = (
                    "Profile settings were applied; auto-advance activation "
                    "was skipped at the deadline."
                )
            elif isinstance(deadline, (int, float)):
                followup = ctx.client.set_auto_advance(
                    enabled, delay=float(delay), deadline=deadline)
                if followup.get("ok") is False or followup.get("success") is False:
                    profile_warning = (
                        "Profile settings were applied; auto-advance activation "
                        "was not confirmed."
                    )
            else:
                followup = ctx.client.set_auto_advance(enabled, delay=float(delay))
                if followup.get("ok") is False or followup.get("success") is False:
                    profile_warning = (
                        "Profile settings were applied; auto-advance activation "
                        "was not confirmed."
                    )
    except Exception:
        profile_warning = (
            "Profile settings were applied; auto-advance activation "
            "was not confirmed."
        )

    applied, receipt_error = _validated_setting_application_receipt(
        result.get("applied"), changes)
    if receipt_error:
        return {
            "ok": False,
            "success": False,
            "profile": profile,
            "applied": result.get("applied"),
            "reason": "invalid_application_receipt",
            "mutation_may_have_applied": True,
            "error": (
                "Set was confirmed successful but returned an invalid "
                "application receipt: {}. Inspect current settings before "
                "retrying.".format(receipt_error)
            ),
        }
    if profile == "default":
        changed = [
            entry for entry in applied
            if entry.get("old_value") != entry.get("value")
        ]
        message = (
            f"Restored {len(changed)} settings to defaults"
            if changed else "All settings already at defaults"
        )
    else:
        changed = [
            entry for entry in applied
            if entry.get("old_value") != entry.get("value")
        ]
        change_label = "setting" if len(changed) == 1 else "settings"
        message = (
            f"Applied profile {profile!r} ({len(changed)} {change_label} changed)"
            if changed
            else f"Profile {profile!r} already applied"
        )
    out = {"ok": True, "profile": profile, "applied": applied, "message": message}
    if profile_warning:
        out["warning"] = profile_warning
    if "data" in result:
        out["data"] = result["data"]
    return out


# ---------------------------------------------------------------------------
# Debug handlers (inspect, command)
# ---------------------------------------------------------------------------

def handle_inspect(ctx: HandlerContext, params: dict) -> dict:
    """Return raw game state for debugging."""
    st = ctx.client.state()
    out: dict[str, Any] = {}
    out["status"] = st.get("status", "unknown")
    out["config"] = st.get("config", {})
    pending = st.get("pending_request")
    if pending:
        out["pending_request"] = pending
    inv = st.get("inventory")
    if inv:
        out["inventory"] = inv
    stats = st.get("stats")
    if stats:
        out["stats"] = stats
    screens = st.get("screens")
    if screens:
        out["screens"] = screens
    interactions = st.get("interactions")
    if interactions:
        out["interactions"] = interactions
    # Client diagnostics.
    out["slot_prefix"] = getattr(ctx.client, "slot_prefix", None)
    out["bridge_url"] = getattr(ctx.client, "bridge_url", None)
    out["last_request_id"] = getattr(ctx.client, "last_request_id", None)
    return out


def handle_command(ctx: HandlerContext, params: dict) -> dict:
    """Send a generic game command."""
    name = params.get("name")
    if not name:
        return {"error": "Missing 'name' parameter"}
    args = params.get("args", {})
    args = dict(args) if isinstance(args, dict) else {}
    args.pop("_deadline", None)
    return _run_transport_bounded_command(ctx, name, params, **args)


def handle_progress(ctx: HandlerContext, params: dict) -> dict:
    """Query game progress — story beats, key choices, current phase."""
    result = ctx.client.command("progress")
    if result.get("ok") is False:
        return {"error": result.get("error", "Progress tracking not available")}
    return result


def handle_save_scan(ctx: HandlerContext, params: dict) -> dict:
    """Read-only scan for saves that may contain vnflight shim references."""
    path = params.get("path")
    if not path:
        return {"error": "Missing 'path' parameter"}
    try:
        from .save_scan import (
            iter_save_files,
            scan_save_file,
            summarize_save_scan_results,
        )
        from pathlib import Path
    except Exception as exc:
        return {"error": f"save scan unavailable: {exc}"}
    root = Path(path).expanduser()
    if not root.exists():
        return {"error": "path_not_found", "path": str(root)}
    recursive = bool(params.get("recursive", False))
    results = [scan_save_file(p) for p in iter_save_files(root, recursive)]
    summary = summarize_save_scan_results(results)
    return {
        "path": str(root),
        "recursive": recursive,
        **summary,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Callable[[HandlerContext, dict], dict]] = {
    # Gameplay.
    "wait": handle_wait,
    "act": handle_act,
    "input_text": handle_input_text,
    "screenshot": handle_screenshot,
    "state": handle_state,
    "transcript": handle_transcript,
    "back": handle_back,
    "back_all": handle_back_all,
    "advance": handle_advance,
    "rewind": handle_rewind,
    "replay": handle_replay,
    "save": handle_save,
    "load": handle_load,
    "auto_skip": handle_auto_skip,
    "set_act_wait": handle_set_act_wait,
    # Lifecycle.
    "launch": handle_launch,
    "stop": handle_stop,
    "games": handle_games,
    "set_profile": handle_set_profile,
    # Debug.
    "inspect": handle_inspect,
    "command": handle_command,
    "progress": handle_progress,
    "save_scan": handle_save_scan,
}


def handle_tool(ctx: HandlerContext, name: str, params: dict) -> dict:
    """Dispatch one public tool call under a presentation ownership scope."""
    handler = _HANDLERS.get(name)
    if not handler:
        return {"error": f"Unknown tool: {name}"}
    return run_public_tool(ctx, name, params, handler)
