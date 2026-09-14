"""vnflight bridge client library.

Core HTTP client for communicating with the vnflight bridge server.
Used by both the vnflight CLI and vnharness agents.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .action_surface import project_bridge_actionable_items
from .delivery_ownership import (
    ActionDeliveryOwnership,
    _event_seq,
    _VISIBLE_STORY_EVENT_TYPES,
    _DECISION_EVENT_TYPES,
    _PREFETCH_BOOKKEEPING_EVENT_TYPES,
)
from .lifecycle import has_actionable_screen_buttons
from .settle import screen_signature
from .shim_schema import (
    ACTIONABLE_ITEM_FIELDS,
    ACTIONABLE_REQUEST_TARGET_FIELDS,
    SHIM_PROTOCOL_VERSION,
)


_ENDED_STATUS_GRACE = 1.0
_ENDED_STATUS_POLL_DELAY = 0.2
_SINGLE_CONTINUE_PENDING_GRACE = 2.4
_SINGLE_CONTINUE_DEADLINE_MARGIN = 0.25
_LIVE_SLOT_STATUSES = {"running", "idle", "waiting_for_input"}
_DYNAMIC_SLOT_PREFIX = "latest:"




# Fields whose VALUE changes with scrape provenance, not with resolution.
# f3 payload check (bridge/logs 141841): the same menu re-registers with
# interaction ids flipping form entirely — '1','2','3' from the menu
# pipeline versus '_focus_list:<label>' from the focus-list scrape — while
# action_strs (the actual Return values) stay identical. Keeping `id` in
# the projection made equal menus compare unequal, so the equivalence gate
# never opened for exactly the re-registrations it was built to admit
# (haiku ×2, ariafirst ×2, terminal ×1 residual rejections). Resolution
# identity rests on action_strs, which the gate separately REQUIRES on
# every enabled row — dropping the volatile id loses nothing.
_PROVENANCE_VOLATILE_ITEM_FIELDS = frozenset({"id"})


def _project_actionable_items(items: object) -> list:
    """Project shim rows to fields that can change target resolution."""
    return project_bridge_actionable_items(
        items,
        item_fields=ACTIONABLE_ITEM_FIELDS - _PROVENANCE_VOLATILE_ITEM_FIELDS,
    )


def actionable_state_snapshot(state: dict | None) -> dict | None:
    """Canonical actionable state, excluding request identity and cosmetics."""
    if not isinstance(state, dict):
        return None
    request = state.get("pending_request") or {}
    game_state = state.get("game_state") or {}
    if not request and not game_state:
        return None
    return {
        "request_type": request.get("type"),
        "request": {
            key: _project_actionable_items(request.get(key))
            for key in ACTIONABLE_REQUEST_TARGET_FIELDS
        },
        "screen": {
            key: _project_actionable_items(game_state.get(key))
            for key in ("interactions", "screen_buttons", "choices")
        },
    }


def actionable_snapshots_equivalent(
    previous: dict | None,
    current: dict | None,
) -> bool:
    """Return whether equal snapshots carry concrete resolution identity.

    Choice labels alone are not capabilities: consecutive menus can display
    the same text while returning different values. Older or partial shims
    that omit interactions therefore fail closed instead of turning snapshot
    equality back into a label-only comparison.
    """
    if previous is None or previous != current:
        return False
    interactions = previous.get("screen", {}).get("interactions") or []
    actionable = [
        item for item in interactions
        if isinstance(item, dict)
        and item.get("type") != "info"
        and not item.get("disabled")
        and not item.get("is_disabled")
        and not item.get("caption")
        and not item.get("is_caption")
    ]
    # Default interaction ids are often derived from screen/label or numeric
    # position, so they cannot prove equal Return() values. Every enabled row
    # must carry the shim's serialized action target (including Return values);
    # an unrelated button must not bless a choice whose own target is unknown.
    return bool(actionable) and all(item.get("action_strs") for item in actionable)


def _has_stale_return_to_menu_batch(events: list[dict]) -> bool:
    """Return True when a return_to_menu event is followed by fresh gameplay."""
    saw_return_to_menu = False
    gameplay_types = {
        "game_started",
        "game_resumed",
        "scene",
        "show",
        "narration",
        "dialogue",
        "choice_request",
        "input_request",
    }
    for event in events:
        if event.get("type") == "game_ended" and event.get("reason") == "return_to_menu":
            saw_return_to_menu = True
            continue
        if saw_return_to_menu and event.get("type") in gameplay_types:
            return True
    return False


def _is_single_continue_pending(pending: dict | None) -> bool:
    if not pending:
        return False
    choices = pending.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return False
    choice = choices[0]
    label = choice.get("label") if isinstance(choice, dict) else choice
    normalized = str(label or "").strip().lower()
    return normalized in {"(continue)", "continue"}




def _bridge_url_is_local(bridge_url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(bridge_url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _local_pid_alive(pid: Any) -> bool | None:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return None
    try:
        if os.name == "nt":
            # errors="replace" so tasklist's OEM-codepage bytes don't
            # crash the reader thread (-> stdout None); CSV output lets
            # us match the PID column exactly, not as a substring.
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid_int}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
            )
            out = result.stdout or ""
            for row in csv.reader(out.splitlines()):
                if len(row) >= 2 and row[1].strip() == str(pid_int):
                    return True
            return False
        os.kill(pid_int, 0)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _is_dynamic_slot_selector(selector: Any) -> bool:
    if selector is None or isinstance(selector, int):
        return False
    return str(selector).strip().lower().startswith(_DYNAMIC_SLOT_PREFIX)


def _normalize_game_id_for_match(game_id: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(game_id or "").lower())


def _game_ids_match(left: Any, right: Any) -> bool:
    return _normalize_game_id_for_match(left) == _normalize_game_id_for_match(right)


def _normalized_game_id_collision(slots: list[dict], game_id: Any) -> set[str]:
    """Distinct raw slot game_ids that all normalize to ``game_id``'s form.

    Returns the colliding raw ids when more than one distinct raw id
    matches, or an empty set when the id is unambiguous.  Fails closed:
    an exact raw-id match does NOT disambiguate, because callers bind
    the first normalized match rather than the exact one — proceeding
    could silently pick the wrong slot.  This is the single shared
    definition for both slot resolution (truthiness) and launch guards
    (error messages listing the colliding ids).
    """
    target = _normalize_game_id_for_match(game_id)
    raw_ids = {
        str(slot.get("game_id") or "")
        for slot in slots
        if _normalize_game_id_for_match(slot.get("game_id")) == target
    }
    return raw_ids if len(raw_ids) > 1 else set()


def _nvl_continue_button(screen: dict | None) -> dict | None:
    buttons = [
        button for button in (screen or {}).get("buttons") or []
        if (
            str(button.get("screen") or "") == "nvl"
            or "Jump" in {str(action) for action in (button.get("actions") or [])}
        )
    ]
    if len(buttons) != 1:
        return None
    button = buttons[0]
    if str(button.get("screen") or "") != "nvl":
        return None
    label = str(button.get("label") or "").strip().lower()
    if label not in {"(continue)", "continue", "(end the game)", "end the game"}:
        return None
    action_names = {str(action) for action in (button.get("actions") or [])}
    if not action_names & {"ChoiceReturn", "Jump", "SetField"}:
        return None
    return button


def _nvl_continue_signature(screen: dict | None) -> tuple | None:
    button = _nvl_continue_button(screen)
    if not button:
        return None
    return (
        str(button.get("label") or "").strip(),
        str(button.get("screen") or "").strip(),
        tuple(str(action) for action in (button.get("actions") or [])),
    )


def _screen_is_nvl_continue_only(screen: dict | None) -> bool:
    return _nvl_continue_button(screen) is not None


# A never-settling nonce must not hijack plain wait() forever.  After this
# long it is dropped from AUTO selection (wait(action_nonce=...) still works).
_AUTO_ACTION_NONCE_MAX_AGE_SECONDS = 600.0
# ... and this many CONSECUTIVE unknown-nonce answers on the auto path drop it
# too: a bridge that does not know the transaction will never resolve it.
_AUTO_ACTION_NONCE_UNKNOWN_STREAK = 3
# New displayable events extend a scoped drain's min_wait window by this much,
# mirroring the ordinary wait loop's observation-window behaviour.
_SCOPED_DRAIN_EXTEND_SECONDS = 0.5


@dataclass
class WaitResult:
    """Result of blocking until a decision point."""
    events: list[dict] = field(default_factory=list)
    pending: dict | None = None
    ended: bool = False
    screen: dict | None = None
    # Set when the ``on_events`` callback returned a truthy value,
    # asking wait() to exit early (used to interrupt a long wait when
    # an operator message arrives, etc). The events / pending / screen
    # fields still reflect what was observed up to that point.
    interrupted: bool = False
    transaction: dict | None = None
    # An ordinary presentation-tail drain encountered rows owned by another
    # action. The caller must not attach the globally live successor menu,
    # which belongs after those retained rows.
    foreign_action_boundary: bool = False


@dataclass(frozen=True)
class _CommandSubmission:
    """Two-value compatible command ack with per-call transport provenance."""

    ok: bool
    message: str
    acceptance_unknown: bool = False

    def __iter__(self):
        yield self.ok
        yield self.message


def preserve_prefetched_events(client: Any, events: list[dict]) -> None:
    """Return observed non-command events to the next poll() consumer."""
    if not events:
        return
    ActionDeliveryOwnership._restore_held_events(client, events)


def drain_stale_pending_request(
    client: Any,
    pending: dict,
    *,
    timeout: float = 8.0,
    poll_interval: float = 1.0,
    sleep_interval: float = 0.05,
) -> dict:
    """Wait briefly for a newer pending request without swallowing events.

    Screen-button actions can return before the shim has replaced the old
    pending request.  Poll for a request with a different id and preserve
    narration/dialogue events so the next wait() still formats them.
    """
    old_id = pending.get("id") if isinstance(pending, dict) else None
    deadline = time.time() + max(0.0, timeout)
    latest = pending
    buffered_events: list[dict] = []
    restored_events = False

    def restore_buffered_events() -> None:
        nonlocal restored_events
        if restored_events:
            return
        preserve_prefetched_events(client, buffered_events)
        restored_events = True

    try:
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            events = client.poll(timeout=min(poll_interval, remaining))
            for event in events or []:
                if event.get("type") != "choice_request":
                    buffered_events.append(event)
                    continue
                event_id = event.get("id")
                if event_id and event_id != old_id:
                    restore_buffered_events()
                    return event
                latest = event

            candidate = client.pending()
            if candidate:
                candidate_id = candidate.get("id")
                if candidate_id and candidate_id != old_id:
                    restore_buffered_events()
                    return candidate
                latest = candidate

            remaining = max(0.0, deadline - time.time())
            if remaining:
                time.sleep(min(sleep_interval, remaining))

        restore_buffered_events()
        return latest
    except Exception:
        restore_buffered_events()
        raise


class BridgeClient(ActionDeliveryOwnership):
    """Stateful HTTP client for the vnflight bridge.

    Handles slot management, event polling with cursor tracking,
    blocking waits for decision points, and action submission.
    """

    def __init__(
        self,
        bridge_url: str = "http://127.0.0.1:8385",
        slot: str | int | None = None,
        slot_prefix: str = "",
        token: str | None = None,
    ) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        # Env fallback lets legacy/tokenless entry points (GUI, hub,
        # diagnostics) talk to token-gated bridges without code changes:
        # export VNFLIGHT_TOKEN with the bridge admin token or the
        # slot's reservation token.
        self.token: str | None = token or os.environ.get("VNFLIGHT_TOKEN") or None
        self.cursor: int = 0
        self.last_request_id: str | None = None
        self.last_request_type: str | None = None
        self.last_choices: list | None = None
        # Action-resolution projection from the last full state returned to
        # the caller. Unlike request ids, this survives a byte-equivalent menu
        # re-registration without making equal labels proof of equal actions.
        self.last_actionable_snapshot: dict | None = None
        # The last actionable screen actually rendered to the caller. A later
        # plain wait may confirm this snapshot after one ordinary event poll
        # instead of spending three idle long-poll rounds rediscovering an
        # unchanged custom-screen decision.
        self._last_delivered_actionable_screen: dict | None = None
        self._last_delivered_actionable_screen_signature: tuple = ()
        self._acted_request_id: str | None = None
        self._ambiguous_slots: list | None = None
        # Nonce of the most recent logical command sent via command().  A
        # transport-timeout retry REUSES this so the bridge dedups the replay
        # (see BridgeClient.command / GameState.submit_command) instead of
        # enqueuing a duplicate act.
        self._last_command_nonce: str | None = None
        # Events observed while waiting for a command_result still belong to
        # the next user-visible wait() call.  Keep them so fast screen-button
        # actions do not swallow dialogue before act(wait=True) formats it.
        self._prefetched_events: list[dict] = []
        self._state_poll_serial = 0
        # The bridge's own "has this run reached gameplay yet" flag, cached
        # from whatever /state read happened last.  The act settle policy
        # reads it to tell a boot lull from a quiet successor; caching it off
        # the reads that already happen keeps that free.
        self._last_gameplay_seen: bool | None = None
        self.supports_transactional_act: bool = True
        self._active_action_nonces: list[str] = []
        # Action ids are bridge-assigned after submission. Keep the identity
        # beside each auto-selectable nonce so a full /state lifecycle marker
        # can preserve only the transaction that caused that boundary.
        self._action_ids_by_nonce: dict[str, int] = {}
        self._action_boundary_floor_by_nonce: dict[str, int] = {}
        # Escape hatches for a nonce that never settles (see
        # _next_auto_action_nonce): when it was first submitted, and how many
        # consecutive 404s the auto path has seen for it.
        self._action_nonce_started: dict[str, float] = {}
        self._action_nonce_unknown_streak: dict[str, int] = {}
        self._auto_action_nonce_retired: dict[str, str] = {}
        self._delivered_action_events: set[tuple[int, int]] = set()
        # The public pair set above remains a compact compatibility/debug
        # view. Ownership itself is generation-qualified so the same bridge
        # coordinates can recur after a load without colliding, and a receipt
        # from the next generation can arrive before /state catches up.
        self._delivered_action_event_ownership: set[
            tuple[int | None, int, int]
        ] = set()

        # Bridge connectivity tracking.
        self._bridge_up: bool = True
        self._consecutive_failures: int = 0
        self._DOWN_THRESHOLD: int = 3
        self._reconnected: bool = False
        self._down_notified: bool = False
        # Last HTTP exchange, for callers that need to tell an auth
        # failure (403 on a reserved slot / require-token bridge) apart
        # from "bridge dead" and "bridge up but nothing there".  The
        # convenience getters (state/pending/...) collapse non-200
        # responses to {}/None, which used to make a 403 render as
        # "no game is connected".
        self.last_http_status: int | None = None
        self.last_http_error: str | None = None

        # Slot prefix (e.g. "/1", "/roadwarden").
        self.slot_prefix = slot_prefix or ""
        self._dynamic_slot_selector: str | None = (
            str(slot).strip() if _is_dynamic_slot_selector(slot) else None
        )
        self._dynamic_slot_resolved_at: float = 0.0
        self._dynamic_slot_ttl: float = 1.0
        if slot is not None and not self.slot_prefix:
            self._resolve_slot(slot)

    def _invalidate_delivered_screen_hint(self) -> None:
        """Retire the fast-wait hint before any command may change the UI."""
        self._last_delivered_actionable_screen = None
        self._last_delivered_actionable_screen_signature = ()

    # -- slot resolution -----------------------------------------------------

    def _resolve_slot(self, slot: str | int) -> None:
        if isinstance(slot, int) or str(slot).isdigit():
            self.slot_prefix = f"/{slot}"
            return
        if _is_dynamic_slot_selector(slot):
            self._dynamic_slot_selector = str(slot).strip()
        selected = self._select_slot(slot)
        if selected is not None:
            self.slot_prefix = f"/{selected['slot_id']}"
            if _is_dynamic_slot_selector(slot):
                self._dynamic_slot_resolved_at = time.time()
            return
        if _is_dynamic_slot_selector(slot):
            self.slot_prefix = ""
            return
        slots = self.list_slots() or []
        if _normalized_game_id_collision(slots, slot):
            self._ambiguous_slots = slots
            self.slot_prefix = ""
            return
        for s in slots:
            if _game_ids_match(s.get("game_id"), slot):
                self.slot_prefix = f"/{s['slot_id']}"
                return
        self.slot_prefix = f"/{slot}"

    def resolve_slot_info(self, selector: str | int) -> dict | None:
        """Return bridge slot metadata for a slot id, game id, or selector."""
        if isinstance(selector, int) or str(selector).isdigit():
            target = str(selector)
            for slot in self.list_slots() or []:
                if str(slot.get("slot_id")) == target:
                    return slot
            return None
        selected = self._select_slot(selector)
        if selected is not None:
            return selected
        slots = self.list_slots() or []
        if _normalized_game_id_collision(slots, selector):
            self._ambiguous_slots = slots
            return None
        for slot in slots:
            if _game_ids_match(slot.get("game_id"), selector):
                return slot
        return None

    def _select_slot(self, selector: str | int | None) -> dict | None:
        """Resolve selector forms that need bridge slot metadata.

        `latest:<game_id>` is intentionally explicit: plain game ids keep their
        historical first-match behavior, while MCP configs can opt into a
        restart-friendly "newest live slot for this game" policy.
        """
        if selector is None or isinstance(selector, int):
            return None
        raw = str(selector).strip()
        if not raw.lower().startswith(_DYNAMIC_SLOT_PREFIX):
            return None
        game_id = raw[len(_DYNAMIC_SLOT_PREFIX):].strip()
        if not game_id:
            return None
        candidates = [
            s for s in (self.list_slots() or [])
            if _game_ids_match(s.get("game_id"), game_id)
        ]
        if _normalized_game_id_collision(candidates, game_id):
            self._ambiguous_slots = candidates
            return None
        if not candidates:
            return None
        local_bridge = _bridge_url_is_local(self.bridge_url)

        def score(slot: dict) -> tuple[int, int, int]:
            pid_alive = (
                _local_pid_alive(slot.get("game_pid")) if local_bridge else None
            )
            live_pid_score = 1 if pid_alive is True else 0
            status = str(slot.get("status") or "").lower()
            live_status_score = 1 if status in _LIVE_SLOT_STATUSES else 0
            try:
                slot_id = int(slot.get("slot_id") or 0)
            except (TypeError, ValueError):
                slot_id = 0
            return live_pid_score, live_status_score, slot_id

        return max(candidates, key=score)

    def _refresh_dynamic_slot(self, *, force: bool = False) -> None:
        if not getattr(self, "_dynamic_slot_selector", None):
            return
        now = time.time()
        if (
            not force
            and self.slot_prefix
            and now - getattr(self, "_dynamic_slot_resolved_at", 0.0)
            < getattr(self, "_dynamic_slot_ttl", 1.0)
        ):
            return
        selected = self._select_slot(self._dynamic_slot_selector)
        if selected is None:
            return
        selected_prefix = f"/{selected['slot_id']}"
        if selected_prefix != self.slot_prefix:
            # Screen occurrence sequences are slot-local.  Carrying this
            # hint across a latest:<game> rebind could make an unrelated
            # screen with a colliding _seq qualify for the fast-wait path.
            self._invalidate_delivered_screen_hint()
            self.slot_prefix = selected_prefix
        self._dynamic_slot_resolved_at = now

    def auto_select_slot(self, game_hint: str | None = None) -> bool:
        """Auto-select a slot. Returns True if successful."""
        if self.slot_prefix:
            return True
        slots = self.list_slots()
        if slots is None:
            return False
        if game_hint:
            if _is_dynamic_slot_selector(game_hint):
                self._dynamic_slot_selector = str(game_hint).strip()
            selected = self._select_slot(game_hint)
            if selected is not None:
                self.slot_prefix = f"/{selected['slot_id']}"
                if _is_dynamic_slot_selector(game_hint):
                    self._dynamic_slot_resolved_at = time.time()
                return True
            if _normalized_game_id_collision(slots, game_hint):
                self._ambiguous_slots = slots
                return False
            for s in slots:
                if (str(s.get("slot_id")) == game_hint
                        or _game_ids_match(s.get("game_id"), game_hint)):
                    self.slot_prefix = f"/{s['slot_id']}"
                    return True
            return False
        if len(slots) == 1:
            self.slot_prefix = f"/{slots[0]['slot_id']}"
            return True
        if len(slots) == 0:
            return True
        # Multiple slots, no hint — ambiguous.
        self._ambiguous_slots = slots
        return False

    def reset_bridge(self) -> bool:
        """Reset the bridge state (clear events, pending requests)."""
        # Reset starts a new event-sequence namespace even when the selected
        # slot itself does not change.
        self._invalidate_delivered_screen_hint()
        code, _ = self._post("/reset", {})
        return code == 200

    def mark_current_events_seen(
        self,
        *,
        allow_rewind: bool = False,
        require_decision: bool = False,
        preserve_story_after_pending: bool = True,
    ) -> bool:
        """Advance the local cursor to the bridge's current event counter.

        Useful for clients attaching to an already-running slot: their first
        wait() should report the current decision, not replay the entire
        historical transcript.
        Set allow_rewind after loading a save, where the bridge event counter
        may legitimately reset below the previous cursor.
        Set require_decision after loading a save to avoid marking only the
        transient reset/command-result boundary before the loaded interaction
        has pushed its active decision.
        Set preserve_story_after_pending false after a load: restored screen
        and NVL history are already visible state, not newly played story.
        """
        code, data = self._get("/state", timeout=3.0)
        if code != 200 or not isinstance(data, dict):
            return False
        self._observe_action_delivery_generation(
            data.get("reset_generation"))
        pending = self._enrich_pending_from_state(
            data.get("pending_request"),
            data,
        )
        self.last_actionable_snapshot = actionable_state_snapshot(data)
        self._remember_pending(pending)
        def to_int(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        counter = to_int(data.get("event_counter", 0))
        pending_seq = to_int((pending or {}).get("_seq"))
        if require_decision and not pending:
            return False
        transcript = data.get("transcript") or []
        transcript_max = 0
        if isinstance(transcript, list):
            for event in transcript:
                if not isinstance(event, dict):
                    continue
                seq = to_int(event.get("_seq", 0))
                transcript_max = max(transcript_max, seq)
        # When a decision is active, keep any story emitted after that
        # request. Roadwarden often pushes a fresh choice_request first and
        # the explanatory narration immediately after it. For no active
        # pending request, mark the full history seen.
        boundary = (
            pending_seq
            if preserve_story_after_pending and pending_seq > 0
            else max(counter, transcript_max)
        )
        self.cursor = boundary if allow_rewind else max(self.cursor, boundary)
        return True

    def reconcile_after_load(
        self,
        result: dict,
        *,
        timeout: float = 2.0,
    ) -> bool:
        """Reconcile local request/cursor state after a load command.

        Failed loads leave the current scene active, so their client state
        must be preserved.  Successful loads invalidate request caches and
        rewind the event stream; wait briefly for the loaded decision before
        falling back to cursor zero.
        """
        if result.get("success") is False or result.get("ok") is False:
            return False

        self.last_request_id = None
        self.last_request_type = None
        self.last_choices = None
        self.last_actionable_snapshot = None
        self._acted_request_id = None
        self._clear_held_events()
        self._last_poll_pending = None

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if self.mark_current_events_seen(
                    allow_rewind=True,
                    require_decision=True,
                    preserve_story_after_pending=False,
                ):
                    return True
            except TypeError:
                # Compatibility for clients overriding the older marker API.
                try:
                    if self.mark_current_events_seen(allow_rewind=True):
                        return True
                except Exception:
                    break
            except Exception:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        # A save can resume at a say pause with no pending request. Establish
        # a best-effort high-water baseline instead of rewinding to zero and
        # replaying the restored timeline as if it had just happened.
        try:
            if self.mark_current_events_seen(
                allow_rewind=True,
                preserve_story_after_pending=False,
            ):
                return True
        except TypeError:
            try:
                if self.mark_current_events_seen(allow_rewind=True):
                    return True
            except Exception:
                pass
        except Exception:
            pass
        self.cursor = 0
        return True

    def attach_to_running_slot(
        self,
        *,
        warn: Callable[[str], None] | None = None,
    ) -> bool:
        """Attach to the current bridge state without replaying old history.

        This is the standard cursor-fast-forward operation for clients that
        bind to an already-running slot.  Load handling is stricter and should
        call mark_current_events_seen(allow_rewind=True, require_decision=True)
        after clearing request caches.
        """
        try:
            marked = self.mark_current_events_seen()
        except Exception as exc:
            if warn:
                warn(str(exc))
            return False
        if marked is False:
            if warn:
                warn("returned_false")
            return False
        return True

    # -- HTTP helpers --------------------------------------------------------

    def _url(self, path: str) -> str:
        self._refresh_dynamic_slot()
        return self.bridge_url + self.slot_prefix + path

    def _admin_url(self, path: str) -> str:
        return self.bridge_url + path

    def _token_candidates(self) -> list:
        """Fresh stored credentials worth retrying after a 403.

        Stored slot tokens first (most recent launch first) — a slot token
        authenticates its slot even when the stored ADMIN token is stale,
        which is exactly the harness-shared-bridge / bridge-restarted case
        where this long-lived client 403s on a game it launched itself.
        """
        try:
            from .lib import (
                resolve_stored_admin_token,
                resolve_stored_slot_tokens,
            )
        except Exception:
            return []
        candidates = list(resolve_stored_slot_tokens(self.bridge_url))
        admin = resolve_stored_admin_token(self.bridge_url)
        if admin:
            candidates.append(admin)
        seen = {self.token} if self.token else set()
        fresh = []
        for tok in candidates:
            if tok and tok not in seen:
                seen.add(tok)
                fresh.append(tok)
        return fresh

    def _request(
        self, method: str, url: str,
        data: dict | None = None, timeout: float = 5.0,
        deadline: float | None = None,
    ) -> tuple[int, Any]:
        def remaining_timeout() -> float | None:
            if deadline is None:
                return timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            return min(timeout, remaining)

        request_timeout = remaining_timeout()
        if request_timeout is None:
            return 0, {"error": "Request deadline exceeded."}
        code, payload = self._request_once(
            method, url, data, request_timeout,
        )
        if code != 403:
            return code, payload
        # 403 self-heal: the bridge rejected our credential outright (no
        # side effects server-side, so re-sending is safe — POSTs included).
        # Stored tokens may have rotated since this client was constructed:
        # a new launch minted a slot token, or the bridge restarted with a
        # new admin token. Retry once per fresh stored credential and adopt
        # the first that works. A fully failed refresh backs off for 30s so
        # a legitimately-denied caller doesn't pay the retry tax per call.
        now = time.time()
        if now - getattr(self, "_token_refresh_failed_at", 0.0) < 30.0:
            return code, payload
        original = self.token
        for tok in self._token_candidates():
            request_timeout = remaining_timeout()
            if request_timeout is None:
                self.token = original
                return code, payload
            self.token = tok
            retry_code, retry_payload = self._request_once(
                method, url, data, request_timeout,
            )
            if retry_code != 403:
                return retry_code, retry_payload
        self.token = original
        self._token_refresh_failed_at = now
        return code, payload

    def _request_once(
        self, method: str, url: str,
        data: dict | None = None, timeout: float = 5.0,
    ) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if urllib.parse.urlparse(url).path.rstrip("/").endswith("/reset"):
            headers["X-VNFlight-Shim-Protocol"] = str(SHIM_PROTOCOL_VERSION)
        if self.token:
            headers["X-Slot-Token"] = self.token
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                self._mark_up()
                self.last_http_status = resp.status
                self.last_http_error = None
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, {"_raw": raw}
        except urllib.error.HTTPError as exc:
            self._mark_up()
            self.last_http_status = exc.code
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {"error": str(exc)}
            error = payload.get("error") if isinstance(payload, dict) else None
            self.last_http_error = str(error or exc)
            return exc.code, payload
        except urllib.error.URLError as exc:
            self._mark_down()
            self.last_http_status = None
            self.last_http_error = f"Connection failed: {exc.reason}"
            return 0, {"error": f"Connection failed: {exc.reason}"}
        except Exception as exc:
            self._mark_down()
            self.last_http_status = None
            self.last_http_error = str(exc)
            return 0, {"error": str(exc)}

    def _mark_up(self) -> None:
        was_down = not self._bridge_up
        self._consecutive_failures = 0
        self._bridge_up = True
        if was_down:
            self._reconnected = True

    def _mark_down(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._DOWN_THRESHOLD:
            self._bridge_up = False

    def _get(self, path: str, params: dict | None = None, timeout: float = 5.0):
        url = self._url(path)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        deadline = time.monotonic() + max(0.0, float(timeout))
        return self._request(
            "GET", url, timeout=timeout, deadline=deadline,
        )

    def _post(self, path: str, data: dict, timeout: float = 5.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        return self._request(
            "POST", self._url(path), data=data, timeout=timeout,
            deadline=deadline,
        )

    # -- admin ---------------------------------------------------------------

    def list_slots(self, *, timeout: float = 3.0) -> list[dict] | None:
        timeout = max(0.0, float(timeout))
        if timeout <= 0:
            return None
        code, data = self._request(
            "GET", self._admin_url("/slots"), timeout=timeout,
            deadline=time.monotonic() + timeout,
        )
        if code == 200 and data:
            return data.get("slots", [])
        return None

    def registration_rejection(
        self,
        *,
        timeout: float = 3.0,
        game_id: str | None = None,
        launch_id: str | None = None,
        launch_started_at: float | None = None,
    ) -> dict | None:
        """Return the assignment refusal owned by this client's exact token.

        This intentionally bypasses credential self-healing: substituting a
        stored admin or another slot token would destroy the launch correlation.
        """
        timeout = max(0.0, float(timeout))
        if timeout <= 0 or not self.token:
            return None
        params = {}
        if game_id is not None:
            params["game_id"] = str(game_id)
        if launch_id is not None:
            params["launch_id"] = str(launch_id)
        if launch_started_at is not None:
            params["launch_started_at"] = str(float(launch_started_at))
        path = "/registration-rejection"
        if params:
            path += "?" + urllib.parse.urlencode(params)
        code, data = self._request_once(
            "GET", self._admin_url(path), timeout=timeout,
        )
        if code == 200 and isinstance(data, dict):
            rejection = data.get("registration_rejection")
            return rejection if isinstance(rejection, dict) else None
        if code == 404:
            # Distinguish an authoritative empty ledger from transport failure
            # so callers can clear an earlier transient diagnostic.
            return {}
        return None

    def free_slot(self, slot_id: int | str) -> tuple[bool, dict]:
        code, data = self._request(
            "POST",
            self._admin_url(f"/slots/free/{slot_id}"),
            timeout=3.0,
        )
        return code == 200, data or {}

    def is_up(self, timeout: float = 2.0) -> bool:
        code, _ = self._request(
            "GET", self._admin_url("/status"), timeout=timeout,
        )
        return code == 200

    def reserve(self, slot_hint: str | None = None) -> dict | None:
        """Reserve the current slot (or one matching slot_hint).

        On success, stores the slot token for future requests and
        returns the reserve response dict. Returns None on failure.
        """
        hint = slot_hint or self.slot_prefix.lstrip("/") or ""
        if not hint:
            return None
        code, data = self._request(
            "POST", self._admin_url("/reserve"),
            data={"slot_hint": hint}, timeout=5.0,
        )
        if code == 200 and data and "token" in data:
            self.token = data["token"]
            # Ensure slot_prefix matches the reserved slot.
            sid = data.get("slot_id")
            if sid is not None:
                self.slot_prefix = f"/{sid}"
            return data
        return data if data else None

    # -- event polling -------------------------------------------------------

    def fast_forward(self) -> None:
        """Advance cursor to the latest event, skipping old history."""
        code, data = self._get("/state", timeout=3.0)
        if code == 200 and data:
            self._observe_action_delivery_generation(
                data.get("reset_generation"))
            counter = data.get("event_counter", 0)
            if counter > 0:
                self.cursor = counter

    # Cached pending_request from last poll() — avoids a separate
    # /pending round-trip that can race with auto-clear.
    _last_poll_pending: dict | None = None
    # monotonic() stamp of the last cache write. The cache is only
    # trustworthy for a moment: it exists to close a millisecond race
    # between /state and /pending, not to stand in for the bridge. A
    # stale survivor (drain_stale_pending_request re-caching the old
    # request via pending(), or poll()'s prefetched-events shortcut
    # skipping the /state refresh that would have cleared it) once
    # re-rendered a choice block 52 SECONDS after the choice was
    # resolved (run driftwood, 2026-08-18).
    _last_poll_pending_at: float = 0.0
    # Fast-path trust window (seconds) for the cached pending.
    _PENDING_CACHE_FRESH_S = 2.0

    def _fresh_cached_pending(self) -> dict | None:
        """The cached pending, but only while it is recent enough to
        still speak for the bridge."""
        if self._last_poll_pending is None:
            return None
        if time.monotonic() - self._last_poll_pending_at \
                > self._PENDING_CACHE_FRESH_S:
            return None
        return self._last_poll_pending

    def _fresh_unacted_cached_pending(self) -> dict | None:
        """Return a fresh pending only when it is not the request just acted.

        Budget-exhausted and callback-interrupted waits cannot afford a live
        /pending reconciliation. They must still preserve the acted-echo
        invariant enforced by _poll_pending, or a slow bridge can re-present
        the exact menu whose answer was already accepted.
        """
        pending = self._fresh_cached_pending()
        if not pending:
            return None
        request_id = pending.get("id")
        if request_id and request_id == self._acted_request_id:
            self._last_poll_pending = None
            return None
        return pending

    def _enrich_pending_from_state(
        self,
        pending: dict | None,
        state: dict | None,
    ) -> dict | None:
        """Copy live game-state request flags onto cached pending metadata."""
        if not pending or not isinstance(state, dict):
            return pending
        game_state = state.get("game_state")
        if not isinstance(game_state, dict):
            return pending
        if game_state.get("_auto_advancing") and not pending.get("_auto_advancing"):
            pending = dict(pending)
            pending["_auto_advancing"] = True
        return pending

    def _remember_pending(self, pending: dict | None) -> None:
        """Keep request metadata aligned with endpoints returning pending."""
        self._last_poll_pending = pending
        self._last_poll_pending_at = time.monotonic()
        if not pending:
            return
        rid = pending.get("id")
        if rid:
            self.last_request_id = rid
        self.last_request_type = pending.get("type")
        self.last_choices = pending.get("choices")

    def poll(
        self,
        timeout: float = 0,
        include_prefetched: bool = True,
        *,
        ordinary_action_id: int | None = None,
        one_shot: bool = False,
    ) -> list[dict]:
        """Fetch events newer than cursor. one_shot bounds this to one HTTP read."""
        self._last_poll_foreign_action_boundary = False

        def partition_action_events(events: list[dict]) -> tuple[list[dict], list[dict]]:
            if ordinary_action_id is None:
                return events, []
            try:
                allowed_action_id = int(ordinary_action_id or 0)
            except (TypeError, ValueError):
                allowed_action_id = 0
            delivered = []
            retained = []
            crossed_foreign_boundary = False
            for event in events:
                try:
                    event_action_id = int(event.get("action_id", 0) or 0)
                except (AttributeError, TypeError, ValueError):
                    event_action_id = 0
                if crossed_foreign_boundary or (
                    event_action_id and event_action_id != allowed_action_id
                ):
                    crossed_foreign_boundary = True
                    retained.append(event)
                else:
                    delivered.append(event)
            return delivered, retained

        if include_prefetched and getattr(self, "_prefetched_events", None):
            events = self._take_held_events()
            events, retained = partition_action_events(events)
            if retained:
                self._last_poll_foreign_action_boundary = True
                self._hold_events(retained)
            if events:
                delivered_keys = []
                for event in events:
                    try:
                        key = (
                            int(event.get("action_id", 0) or 0),
                            int(event.get("_seq", 0) or 0),
                        )
                    except (AttributeError, TypeError, ValueError):
                        continue
                    if key[0] > 0 and key[1] > 0:
                        delivered_keys.append(key)
                self._record_delivered_action_events(delivered_keys)
                return events
            if retained:
                # The retained row is an ordering fence, not merely a filter.
                # A fresh /state read here could expose later durable rows and
                # let them leapfrog the foreign action still in prefetch.
                return []

        deadline = time.time() + timeout if timeout > 0 else 0

        while True:
            params = {"since": str(self.cursor)} if self.cursor > 0 else {}
            request_timeout = 3.0
            if timeout > 0:
                request_timeout = min(
                    request_timeout, max(0.0, deadline - time.time()))
                if request_timeout <= 0:
                    return []
            code, data = self._get(
                "/state", params=params, timeout=request_timeout)
            events: list[dict] = []

            if code == 200 and data:
                events = data.get("transcript", [])
                self._observe_action_delivery_generation(
                    data.get("reset_generation"))
                try:
                    self._last_event_counter = int(data.get("event_counter", 0) or 0)
                except (TypeError, ValueError):
                    self._last_event_counter = 0
                try:
                    generation = data.get("reset_generation")
                    self._last_reset_generation = (
                        int(generation) if generation is not None else None
                    )
                except (TypeError, ValueError):
                    self._last_reset_generation = None
                self.last_actionable_snapshot = actionable_state_snapshot(data)
                pending_for_signature = data.get("pending_request") or {}
                context_for_signature = data.get("context")
                if isinstance(context_for_signature, dict):
                    context_for_signature = context_for_signature.get("context")
                self._last_state_signature = (
                    data.get("status"),
                    context_for_signature,
                    pending_for_signature.get("type"),
                    pending_for_signature.get("id"),
                    tuple(pending_for_signature.get("choices") or ()),
                )
                self._last_has_pending_command = (
                    bool(data.get("has_pending_command"))
                    if "has_pending_command" in data else None
                )
                if "gameplay_seen" in data:
                    self._last_gameplay_seen = bool(data.get("gameplay_seen"))
                pending = self._enrich_pending_from_state(
                    data.get("pending_request"),
                    data,
                )
                self._remember_pending(pending)

                # Stale cursor detection (game restarted).
                if not events and self.cursor > 0:
                    counter = data.get("event_counter", 0)
                    if 0 < counter < self.cursor:
                        self.cursor = 0
                        if one_shot:
                            return []
                        continue
                self._state_poll_serial = (
                    getattr(self, "_state_poll_serial", 0) + 1
                )

            if events:
                max_seq = max(e.get("_seq", 0) for e in events)
                if max_seq > self.cursor:
                    self.cursor = max_seq
                # Establish the chronology fence on the raw sequence. A row
                # already present in the cross-lane delivery ledger still
                # separates the prefix from everything that followed it.
                events, retained = partition_action_events(events)
                if retained:
                    self._last_poll_foreign_action_boundary = True
                    # This suffix came from a fresh durable-state read, not
                    # the prefetch lane. Do not re-prefetch action rows already
                    # delivered by a scoped receipt. (Ledger-marked rows that
                    # are already in _prefetched_events remain pending: command
                    # observation is not user delivery.) The raw row still
                    # establishes the chronology fence above.
                    retained_for_prefetch = []
                    for event in retained:
                        try:
                            key = (
                                int(event.get("action_id", 0) or 0),
                                int(event.get("_seq", 0) or 0),
                            )
                        except (AttributeError, TypeError, ValueError):
                            key = (0, 0)
                        if key[0] > 0 and self._action_event_was_delivered(key):
                            continue
                        retained_for_prefetch.append(event)
                    self._hold_events(retained_for_prefetch)
                events = [
                    event for event in events
                    if not self._action_event_was_delivered((
                        int(event.get("action_id", 0) or 0),
                        int(event.get("_seq", 0) or 0),
                    ))
                ]
                # Symmetric ledger: record what the ORDINARY path just served
                # so an explicit scoped drain (recovery by nonce) does not
                # redeliver it.  Pruning by cursor would defeat that — a
                # transaction's events stay drainable by nonce long after the
                # ordinary cursor has passed them.
                self._record_delivered_action_events(
                    (
                        int(event.get("action_id", 0) or 0),
                        int(event.get("_seq", 0) or 0),
                    )
                    for event in events
                    if event.get("action_id")
                )
                if events:
                    return events
                if retained:
                    # Fence-emptied batch. The retained rows are an ordering
                    # fence, not merely a filter: a retry read here could
                    # expose later durable rows and let them leapfrog the
                    # foreign action still parked in prefetch. Mirror the
                    # prefetch branch and let the caller's next wait drain
                    # the fence first. (A ledger-emptied batch, with nothing
                    # retained, may keep reading.)
                    return []
                if one_shot:
                    return []
                continue

            # Bridge-down early exit: once the connection-failure threshold
            # trips, don't burn the rest of the poll timeout hammering a
            # dead socket.  Callers (the wait loops) own the reconnect
            # backoff policy and can distinguish "no new events" from
            # "bridge unreachable" via the connectivity tracker.
            if not self._bridge_up:
                return []

            # Access-denied early exit: a 403 (reserved slot / require-token
            # bridge, wrong or missing token) is deterministic for this
            # session — burning the rest of the poll window cannot succeed.
            # Callers see the denial via last_http_status.
            if code == 403:
                return []

            if one_shot or timeout <= 0 or time.time() >= deadline:
                return []

            elapsed = time.time() - (deadline - timeout)
            if elapsed < 2:
                time.sleep(0.1)
            elif elapsed < 10:
                time.sleep(0.25)
            else:
                time.sleep(0.5)

    def _poll_pending(
        self,
        retries: int = 5,
        delay: float = 0.2,
        *,
        deadline: float | None = None,
    ) -> dict | None:
        """Poll /pending with retries for race-condition tolerance.

        Checks the cached pending from the last poll() first to avoid
        a round-trip race where /pending is cleared between /state and
        the separate /pending request.
        """
        # Fast path: use pending cached from the last poll() /state response.
        # Freshness-gated: an old cache entry no longer speaks for the
        # bridge — the request may have been resolved (auto-advance,
        # native click, another client) since it was written. Stale
        # entries fall through to the authoritative /pending below.
        cached = self._fresh_cached_pending()
        if cached is None and self._last_poll_pending is not None:
            self._last_poll_pending = None  # discard stale cache
        if cached is not None:
            rid = cached.get("id")
            if rid and rid == self._acted_request_id:
                self._last_poll_pending = None  # consume so we don't re-check
                # stale — fall through to /pending endpoint
            else:
                if rid:
                    self.last_request_id = rid
                self._last_poll_pending = None  # consume
                return cached

        for _i in range(retries):
            request_timeout = 2.0
            if deadline is not None:
                request_timeout = min(
                    request_timeout, max(0.0, deadline - time.time()))
                if request_timeout <= 0:
                    return None
            code, data = self._get("/pending", timeout=request_timeout)
            if code == 200 and data:
                pending = data.get("pending")
                if pending is not None:
                    rid = pending.get("id")
                    if rid and rid == self._acted_request_id:
                        sleep_s = delay
                        if deadline is not None:
                            sleep_s = min(
                                sleep_s, max(0.0, deadline - time.time()))
                        if sleep_s > 0:
                            time.sleep(sleep_s)
                        continue
                    if rid:
                        self.last_request_id = rid
                        self._acted_request_id = None
                    return pending
            sleep_s = delay
            if deadline is not None:
                sleep_s = min(
                    sleep_s, max(0.0, deadline - time.time()))
            if sleep_s > 0:
                time.sleep(sleep_s)
        # Exhausted retries — clear acted flag so the next call
        # accepts the pending.  Covers pre_resolve_steps where
        # the same pending ID stays active after a choice step.
        self._acted_request_id = None
        return None



    def _track_action_nonce(
        self, nonce: str, action_id: object = None,
    ) -> None:
        """Register a nonce as auto-selectable by a plain wait()."""
        try:
            normalized_action_id = int(action_id or 0)
        except (TypeError, ValueError):
            normalized_action_id = 0
        boundary_floor = getattr(
            self, "_action_boundary_floor_by_nonce", {},
        ).get(nonce, 0)
        if (
            boundary_floor > 0
            and normalized_action_id > 0
            and normalized_action_id < boundary_floor
        ):
            self._retire_auto_action_nonce(nonce, "timeline_reset")
            return
        # Re-posting a nonce is idempotent recovery of the same logical act.
        # It cannot revive an act already proven to belong to an old timeline.
        if self._auto_action_nonce_retired.get(nonce) == "timeline_reset":
            return
        self._auto_action_nonce_retired.pop(nonce, None)
        if nonce not in self._active_action_nonces:
            self._active_action_nonces.append(nonce)
        self._remember_action_id(nonce, normalized_action_id)
        self._action_nonce_started.setdefault(nonce, time.time())
        self._action_nonce_unknown_streak.setdefault(nonce, 0)

    def _remember_action_id(self, nonce: str, action_id: object) -> None:
        """Attach bridge identity without changing nonce lifecycle state."""
        try:
            normalized_action_id = int(action_id or 0)
        except (TypeError, ValueError):
            normalized_action_id = 0
        if normalized_action_id > 0:
            identities = getattr(self, "_action_ids_by_nonce", None)
            if identities is None:
                identities = {}
                self._action_ids_by_nonce = identities
            identities[nonce] = normalized_action_id

    def action_nonce_for_id(self, action_id: object) -> str | None:
        """Return the active nonce that owns one bridge action id."""
        try:
            normalized_action_id = int(action_id or 0)
        except (TypeError, ValueError):
            return None
        matches = [
            nonce for nonce in self._active_action_nonces
            if getattr(self, "_action_ids_by_nonce", {}).get(nonce)
            == normalized_action_id
        ]
        return matches[0] if len(matches) == 1 else None

    def action_nonces_for_boundary(self, action_id: object) -> list[str]:
        """Return active actions not proven to predate a lifecycle boundary."""
        try:
            normalized_action_id = int(action_id or 0)
        except (TypeError, ValueError):
            return []
        if normalized_action_id <= 0:
            return []
        identities = getattr(self, "_action_ids_by_nonce", {})
        # Bridge action ids are monotonic. A delayed boundary for N cannot
        # invalidate N, a newer accepted action, or an acceptance_unknown
        # action whose bridge id was lost with its response.
        return [
            nonce for nonce in self._active_action_nonces
            if nonce not in identities
            or identities.get(nonce, 0) >= normalized_action_id
        ]

    def mark_action_boundary_floor(
        self, nonces, action_id: object,
    ) -> None:
        """Fence unresolved nonces against receipts older than a lifecycle."""
        try:
            normalized_action_id = int(action_id or 0)
        except (TypeError, ValueError):
            return
        if normalized_action_id <= 0:
            return
        identities = getattr(self, "_action_ids_by_nonce", {})
        floors = getattr(self, "_action_boundary_floor_by_nonce", None)
        if floors is None:
            floors = {}
            self._action_boundary_floor_by_nonce = floors
        active = set(self._active_action_nonces)
        for nonce in nonces or ():
            if nonce not in active or nonce in identities:
                continue
            floors[nonce] = max(floors.get(nonce, 0), normalized_action_id)

    def _retire_auto_action_nonce(self, nonce: str, reason: str) -> None:
        """Drop a nonce from AUTO selection.

        Explicit waits still reach ordinary retired transactions. A
        ``timeline_reset`` is stronger: it tombstones an action proven to
        predate the active story timeline, whose replay would resurrect
        abandoned story or decisions.
        """
        if nonce in self._active_action_nonces:
            self._active_action_nonces.remove(nonce)
        getattr(self, "_action_ids_by_nonce", {}).pop(nonce, None)
        getattr(self, "_action_boundary_floor_by_nonce", {}).pop(nonce, None)
        self._action_nonce_started.pop(nonce, None)
        self._action_nonce_unknown_streak.pop(nonce, None)
        if self._auto_action_nonce_retired.get(nonce) != "timeline_reset":
            self._auto_action_nonce_retired[nonce] = reason

    def retire_auto_action_nonces(
        self,
        reason: str,
        *,
        preserve_nonce: str | None = None,
        preserve_nonces=None,
    ) -> None:
        """Stop automatic drains, tombstoning proven old-timeline actions."""
        preserved = set(preserve_nonces or ())
        if preserve_nonce is not None:
            preserved.add(preserve_nonce)
        for nonce in list(self._active_action_nonces):
            if nonce in preserved:
                continue
            self._retire_auto_action_nonce(nonce, reason)

    def _next_auto_action_nonce(self) -> str | None:
        """Oldest auto-selectable in-flight action, skipping stuck ones.

        A nonce that never settles used to hijack every plain ``wait()``
        forever: removal happened only on settled/failed/rejected, and 404s
        were swallowed on the auto path.  Two escape hatches bound that — an
        age cap and a consecutive-unknown streak. Both drop the nonce from
        AUTO selection only; ``wait(action_nonce=...)`` still reaches them.
        Timeline-reset tombstones are handled separately and suppress their
        abandoned transaction even when addressed explicitly.
        """
        now = time.time()
        for nonce in list(self._active_action_nonces):
            started = self._action_nonce_started.get(nonce, now)
            if now - started > _AUTO_ACTION_NONCE_MAX_AGE_SECONDS:
                self._retire_auto_action_nonce(nonce, "age_cap")
                continue
            if (
                self._action_nonce_unknown_streak.get(nonce, 0)
                >= _AUTO_ACTION_NONCE_UNKNOWN_STREAK
            ):
                self._retire_auto_action_nonce(nonce, "unknown_streak")
                continue
            return nonce
        return None

    def _wait_action_transaction(
        self,
        action_nonce: str,
        *,
        timeout: float,
        on_events: Optional[Callable[[list[dict]], Any]] = None,
        min_wait: float = 0,
        unknown_is_pending: bool = True,
        return_on_admission: bool = False,
        include_unowned_prefetch: bool = False,
    ) -> WaitResult:
        """Drain one accepted act without consuming unrelated events.

        *return_on_admission* is for the ACT path only: once the bridge
        reports ``admission_open`` it has decided this transaction may be
        stepped over, so the caller can stop burning its result timeout and
        decide whether to retry.  A plain ``wait()`` must NOT set it — late
        story text belongs to this transaction until it settles, and returning
        early there would hand the caller a drain that is not finished.
        """
        deadline = time.time() + timeout

        # The post-action observation window matters for a scoped drain exactly
        # as much as for an ordinary wait: trailing story output keeps arriving
        # for a moment after the transaction reports settled.  New displayable
        # events extend the window, mirroring wait()'s own min_wait.
        min_wait_until = time.time() + min_wait if min_wait > 0 else 0.0
        all_events: list[dict] = []
        latest: dict | None = None
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            code, data = self._get(
                "/transaction",
                params={"action_nonce": action_nonce},
                timeout=min(3.0, remaining),
            )
            if code == 200 and data:
                self._action_nonce_unknown_streak[action_nonce] = 0
                latest = data.get("transaction") or {}
                self._remember_action_id(
                    action_nonce, latest.get("action_id"),
                )
                try:
                    revealed_action_id = int(latest.get("action_id", 0) or 0)
                except (TypeError, ValueError):
                    revealed_action_id = 0
                boundary_floor = getattr(
                    self, "_action_boundary_floor_by_nonce", {},
                ).get(action_nonce, 0)
                if (
                    boundary_floor > 0
                    and revealed_action_id > 0
                    and revealed_action_id < boundary_floor
                ):
                    self._retire_auto_action_nonce(
                        action_nonce, "timeline_reset",
                    )
                raw_events = latest.pop("events", []) or []
                # Scoped receipts omit unattributed ADV rows. Automatic waits
                # fetch the ordinary prefix before publishing newer receipt
                # rows. Do this before checking generation: /state can reveal
                # a timeline reset that makes this receipt stale.
                receipt_seq = max((_event_seq(e) for e in raw_events), default=0)
                if include_unowned_prefetch and receipt_seq > self.cursor:
                    remaining = max(0.0, deadline - time.time())
                    poll_serial = self._state_poll_serial
                    if remaining > 0:
                        prefix = self.poll(
                            timeout=min(1.0, remaining), include_prefetched=False,
                            one_shot=True,
                        )
                        self._hold_events(prefix, chronological=True)
                    if self._state_poll_serial == poll_serial:
                        # Keep the accepted receipt unacknowledged and retryable.
                        latest = dict(latest, transaction_state="applied",
                                      pending=True, reason="story_prefix_unavailable")
                        break
                transaction_generation = self._action_delivery_generation(
                    latest.get("reset_generation"))
                authoritative_generation = getattr(
                    self, "_action_delivery_reset_generation", None)
                stale_transaction = (
                    (
                        transaction_generation is not None
                        and authoritative_generation is not None
                        and transaction_generation < authoritative_generation
                    )
                    or (
                        self._auto_action_nonce_retired.get(action_nonce)
                        == "timeline_reset"
                    )
                )
                settled_pending = (
                    None if stale_transaction
                    else latest.get("settled_pending")
                )
                settled_screen = (
                    None if stale_transaction
                    else latest.get("settled_screen")
                )
                settled_ended = (
                    False if stale_transaction
                    else bool(latest.get("ended"))
                )
                source_id = latest.get("_source_id")
                source_after = latest.get("_source_seq")
                source_through = source_after
                for source_event in raw_events:
                    if not isinstance(source_event, dict):
                        continue
                    if source_event.get("_source_id") != source_id:
                        continue
                    event_source_seq = source_event.get("_source_seq")
                    if type(event_source_seq) is int and (
                        type(source_through) is not int
                        or event_source_seq > source_through
                    ):
                        source_through = event_source_seq
                prefetched_events = []
                if not stale_transaction:
                    prefetched_events = self._claim_prefetched_action_events(
                        latest.get("action_id"),
                        include_unowned=include_unowned_prefetch,
                        source_id=source_id,
                        source_after=source_after,
                        source_through=source_through,
                    )
                prefetched_keys = set()
                events = []
                for event in prefetched_events:
                    try:
                        key = (
                            int(event.get("action_id", 0) or 0),
                            int(event.get("_seq", 0) or 0),
                        )
                        if key in prefetched_keys:
                            continue
                        if key[0] > 0 and key[1] > 0:
                            prefetched_keys.add(key)
                            self._record_delivered_action_events(
                                [key],
                                reset_generation=transaction_generation,
                            )
                    except (AttributeError, TypeError, ValueError):
                        pass
                    events.append(event)
                for event in raw_events:
                    if stale_transaction:
                        continue
                    try:
                        key = (
                            int(event.get("action_id", 0) or 0),
                            int(event.get("_seq", 0) or 0),
                        )
                        if key in prefetched_keys:
                            continue
                        if self._action_event_was_delivered(
                            key,
                            reset_generation=transaction_generation,
                        ):
                            continue
                        self._record_delivered_action_events(
                            [key],
                            reset_generation=transaction_generation,
                        )
                    except (TypeError, ValueError):
                        pass
                    events.append(event)
                events.sort(key=_event_seq)
                all_events.extend(events)
                if events and min_wait_until:
                    min_wait_until = max(
                        min_wait_until, time.time() + _SCOPED_DRAIN_EXTEND_SECONDS,
                    )
                if on_events and events:
                    try:
                        if on_events(events):
                            return WaitResult(
                                events=all_events,
                                pending=settled_pending,
                                screen=settled_screen,
                                ended=settled_ended,
                                interrupted=True,
                                transaction=latest,
                            )
                    except Exception:
                        pass
                state = latest.get("transaction_state")
                terminal_observation_open = (
                    state in {"settled", "failed", "rejected"}
                    and time.time() < min_wait_until
                )
                # A terminal transaction can still receive bounded trailing
                # story output. Do not acknowledge/compact it until the
                # observation window closes; local event dedup makes replayed
                # reads harmless in the meantime.
                ack_ok = not raw_events and not terminal_observation_open
                if raw_events and not terminal_observation_open:
                    ack_end = latest.get("delivery_end")
                    if ack_end is not None:
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            break
                        ack_code, _ = self._get(
                            "/transaction",
                            params={
                                "action_nonce": action_nonce,
                                "ack": str(ack_end),
                            },
                            timeout=min(3.0, remaining),
                        )
                        ack_ok = ack_code == 200
                if (
                    state in {"settled", "failed", "rejected"}
                    and ack_ok
                    and time.time() >= min_wait_until
                ):
                    self._retire_auto_action_nonce(action_nonce, state)
                    return WaitResult(
                        events=all_events,
                        pending=settled_pending,
                        screen=settled_screen,
                        ended=settled_ended,
                        transaction=latest,
                    )
                if (
                    return_on_admission
                    and latest.get("admission_open")
                    and ack_ok
                    and time.time() >= min_wait_until
                ):
                    # Still pending, so the nonce is deliberately NOT retired:
                    # whatever this transaction says later is still recoverable
                    # by nonce, and by a plain wait() in the meantime.
                    return WaitResult(
                        events=all_events,
                        pending=settled_pending,
                        screen=settled_screen,
                        ended=settled_ended,
                        transaction=latest,
                    )
            elif code == 404:
                # An acceptance-timeout client may arrive before the original
                # POST finishes. Keep polling; a retry with the same nonce is
                # the explicit way to resolve prolonged uncertainty.
                if not unknown_is_pending:
                    return WaitResult(events=all_events, transaction={
                        "action_nonce": action_nonce,
                        "transaction_state": "rejected",
                        "pending": False,
                        "reason": "unknown_nonce",
                    })
                # Auto-selected nonce: a 404 STREAK means the bridge does not
                # know this transaction (fresh bridge, pruned run).  Count it
                # so _next_auto_action_nonce can stop letting it hijack every
                # plain wait(); a single 404 is still just a race.
                streak = self._action_nonce_unknown_streak.get(action_nonce, 0) + 1
                self._action_nonce_unknown_streak[action_nonce] = streak
                if streak >= _AUTO_ACTION_NONCE_UNKNOWN_STREAK:
                    self._retire_auto_action_nonce(action_nonce, "unknown_streak")
                    return WaitResult(events=all_events, transaction={
                        "action_nonce": action_nonce,
                        "transaction_state": "rejected",
                        "pending": False,
                        "reason": "unknown_nonce",
                        "auto_selection_retired": True,
                    })
            if on_events:
                try:
                    if on_events([]):
                        return WaitResult(
                            events=all_events,
                            interrupted=True,
                            transaction=latest,
                        )
                except Exception:
                    pass
            time.sleep(min(0.2, max(0, deadline - time.time())))
        return WaitResult(events=all_events, transaction=latest or {
            "action_nonce": action_nonce,
            "transaction_state": "acceptance_unknown",
            "pending": True,
        })

    def _observe_new_anomaly(self, anomaly: Any) -> bool:
        """True the first time this latched anomaly is seen.

        The bridge never clears its anomaly latch, and this poll runs on
        every wait iteration, so without an identity check one exception
        would be re-emitted into the story stream for the rest of the
        slot's life. Identity is the latch stamp when present, else the
        anomaly's own content.
        """
        if not isinstance(anomaly, dict):
            return False
        if "_resolved_at" in anomaly:
            # The bridge saw story progress after the latch: not current,
            # and a client connecting later must not replay it either.
            return False
        details = anomaly.get("details")
        details = details if isinstance(details, dict) else {}
        key = (
            anomaly.get("_latched_at"),
            anomaly.get("_seq"),
            str(anomaly.get("kind") or details.get("type") or ""),
            str(anomaly.get("summary") or details.get("message") or ""),
        )
        if key == getattr(self, "_last_observed_anomaly_key", None):
            return False
        self._last_observed_anomaly_key = key
        return True

    def wait(
        self,
        timeout: float = 60,
        on_events: Optional[Callable[[list[dict]], Any]] = None,
        min_wait: float = 0,
        action_nonce: str | None = None,
        return_on_admission: bool = False,
        ordinary_only: bool = False,
        ordinary_action_id: int | None = None,
        include_unowned_prefetch: bool = False,
    ) -> WaitResult:
        """Block until a decision point: pending choice/input, screen
        buttons with no pending request (e.g. main menu), or game end.

        If *on_events* is provided, it is called with each batch of new
        events as they arrive from the bridge AND once per idle main-
        loop iteration with an empty list (so peek-only callbacks like
        the harness pending-instructions check can interrupt even in
        quiet bridge states). The callback may return a truthy value
        to ask wait() to exit early; pure streaming callbacks that
        only push non-empty batches can cheaply return None on the
        empty-tick path. The returned WaitResult has
        ``interrupted=True`` when the callback signalled exit, with
        events/pending/screen reflecting what was observed before the
        interrupt.

        *min_wait* sets a minimum time (seconds) before early-exit
        checks (screen buttons, idle) are allowed.  Useful after
        act() to let the observation phase complete before checking.
        New displayable events extend the min_wait window.

        *return_on_admission* applies to a scoped (action_nonce) drain only
        and is for the act path: return as soon as the bridge signals
        ``admission_open`` instead of waiting out a transaction the bridge has
        already agreed to let another act step over.

        *ordinary_only* bypasses automatic transaction selection. It is used
        after an explicitly scoped receipt has finished but its story is still
        crossing onto the ordinary event lane; selecting a different active
        nonce there would attribute another action's output to this wait.
        When *ordinary_action_id* is supplied, action-attributed rows owned by
        any other transaction stay prefetched for their scoped receipt.

        *include_unowned_prefetch* gives an internally selected pre-action
        receipt the same chronological prefix as an automatic wait. Explicit
        recovery waits leave it False to retain their receipt-only scope.
        """
        wait_started_at = time.time()
        selected_nonce = action_nonce
        selected_from_active = False
        selected_automatically = False
        if selected_nonce is None and not ordinary_only:
            selected_nonce = self._next_auto_action_nonce()
            selected_from_active = selected_nonce is not None
            selected_automatically = selected_from_active
        elif selected_nonce in self._active_action_nonces:
            selected_from_active = True
        while selected_nonce is not None:
            remaining = max(0.0, timeout - (time.time() - wait_started_at))
            scoped = self._wait_action_transaction(
                selected_nonce,
                timeout=remaining,
                on_events=on_events,
                min_wait=min_wait,
                unknown_is_pending=selected_from_active,
                return_on_admission=return_on_admission,
                include_unowned_prefetch=(selected_automatically or include_unowned_prefetch),
            )
            # An explicit recovery wait is a request for this exact receipt,
            # including an empty terminal one.  A plain wait, however, may
            # auto-select several transactions left behind by fast admission
            # returns.  Do not let an empty settled receipt end the user's
            # wait while a newer action or ordinary story output is pending.
            if action_nonce is not None:
                return scoped
            transaction = scoped.transaction or {}
            # Only a successfully settled, empty receipt is bookkeeping that
            # a plain wait may step past.  Failures and rejections are
            # actionable diagnostics and must remain visible to the caller.
            skippable = transaction.get("transaction_state") == "settled"
            meaningful = bool(
                scoped.events
                or scoped.pending
                or scoped.screen
                or scoped.ended
                or scoped.interrupted
            )
            if meaningful or not skippable or remaining <= 0:
                return scoped
            selected_nonce = self._next_auto_action_nonce()
            selected_from_active = selected_nonce is not None
            selected_automatically = selected_from_active

        # Auto-draining empty receipts spent part of this caller's budget.
        # Preserve the original deadline for the ordinary event loop below.
        timeout = max(0.0, timeout - (time.time() - wait_started_at))

        all_events: list[dict] = []
        deadline = time.time() + timeout

        def request_timeout(cap: float) -> float:
            return min(cap, max(0.0, deadline - time.time()))

        idle_rounds = 0
        ended_seen_at: float | None = None
        screen_snapshot: dict | None = None
        deferred_nvl_continue_sigs: set[tuple] = set()
        single_continue_seen_at: dict[str, float] = {}
        single_continue_screen_seen_at: dict[tuple, float] = {}
        _min_wait_until = time.time() + min_wait if min_wait > 0 else 0
        _initial_screen_sig = None
        if min_wait > 0 and time.time() < deadline:
            try:
                code, screen_data = self._get(
                    "/screen", timeout=request_timeout(2.0))
                if code == 200 and screen_data:
                    _initial_screen_sig = screen_signature(screen_data.get("screen"))
            except Exception:
                _initial_screen_sig = None
        _unchanged_story_screen_defer_until = 0.0
        # Passive overlay snapshots are event-driven, but still dynamic. A
        # decision is ready only after the overlay has been quiet for this
        # window; every changed snapshot pushes the boundary out again.
        _passive_overlay_quiet_until = 0.0
        _latest_story_seq = 0
        _latest_story_cursor = 0
        lifecycle_is_live = False
        acted_request_id_at_start = (
            self._acted_request_id if min_wait > 0 else None
        )
        # Latched once on_events returns truthy; the main loop checks
        # it each iteration and bails with interrupted=True.
        _interrupted = [False]

        def _is_acted_pending(pending: dict | None) -> bool:
            if not acted_request_id_at_start or not pending:
                return False
            return pending.get("id") == acted_request_id_at_start

        def _emit(batch: list[dict]) -> None:
            all_events.extend(batch)
            if on_events and batch:
                try:
                    if on_events(batch):
                        _interrupted[0] = True
                except Exception:
                    pass

        def _tick() -> None:
            """Give on_events a chance to signal interrupt even on
            idle iterations where no bridge batch arrived. Important
            for the harness pending-instructions check — a quiet VN
            scene must not block an operator message until timeout.
            Callback receives an empty list so streaming callbacks can
            cheaply early-return."""
            if on_events and not _interrupted[0]:
                try:
                    if on_events([]):
                        _interrupted[0] = True
                except Exception:
                    pass

        import sys as _sys
        _trace_wait = bool(os.environ.get("VNFLIGHT_TRACE_WAIT"))
        def _dbg(msg: str) -> None:
            if _trace_wait:
                print(f"[wait] {msg}", file=_sys.stderr, flush=True)

        _dbg(f"start cursor={self.cursor} last_req={self.last_request_id} acted={self._acted_request_id} slot={self.slot_prefix}")
        _loop = 0

        while time.time() < deadline:
            _loop += 1
            # Idle-tick the callback before any work this iteration —
            # if a peek-only callback (e.g. operator-message check)
            # wants to bail, we honor it even when no bridge events
            # are arriving.
            _tick()
            # on_events asked us to bail (e.g. operator message landed).
            # Return what we have so far — events stream is still valid;
            # the caller can choose whether to re-enter wait().
            if _interrupted[0]:
                _dbg(f"loop {_loop}: on_events requested early-exit")
                return WaitResult(events=all_events,
                                  pending=self._fresh_unacted_cached_pending(),
                                  screen=screen_snapshot,
                                  interrupted=True)
            # Bridge down — backoff and probe.
            if not self._bridge_up:
                _dbg(f"loop {_loop}: bridge down")
                if not self._down_notified:
                    _emit([{
                        "type": "bridge_down",
                        "text": "Game bridge is unreachable. Waiting for reconnection...",
                        "ts": time.strftime("%H:%M:%S"),
                    }])
                    self._down_notified = True
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(2.0, remaining))
                if time.time() >= deadline:
                    break
                self._request(
                    "GET", self._url("/status"),
                    timeout=request_timeout(2.0),
                )
                if not self._bridge_up:
                    continue

            # Reconnection event.
            if self._reconnected:
                self._reconnected = False
                self._down_notified = False
                _emit([{
                    "type": "bridge_reconnected",
                    "text": "Game bridge reconnected.",
                    "ts": time.strftime("%H:%M:%S"),
                }])

            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if ordinary_only:
                events = self.poll(
                    timeout=min(remaining, 5.0),
                    ordinary_action_id=(
                        ordinary_action_id
                        if ordinary_action_id is not None else 0
                    ),
                )
            else:
                events = self.poll(timeout=min(remaining, 5.0))
            _emit(events)

            if (
                ordinary_only
                and getattr(self, "_last_poll_foreign_action_boundary", False)
            ):
                # The global decision surface is already beyond retained
                # output owned by another transaction. Stop at this ownership
                # boundary so its menu cannot precede its story.
                return WaitResult(
                    events=all_events,
                    foreign_action_boundary=True,
                )

            if events:
                for event in events:
                    event_type = event.get("type")
                    if event_type in {"game_started", "game_resumed"}:
                        lifecycle_is_live = True
                    elif (
                        event_type == "game_ended"
                        and event.get("terminal") is not False
                    ):
                        lifecycle_is_live = False
                idle_rounds = 0
                _ev_types = [e.get("type","?") for e in events]
                _dbg(f"loop {_loop}: {len(events)} events: {_ev_types[:5]} cursor={self.cursor}")
                # Extend min_wait window when displayable events arrive
                # (narration, dialogue, choice_request, input_request).
                if _min_wait_until > 0:
                    _displayable = {"narration", "dialogue", "choice_request",
                                    "input_request", "auto_skipped",
                                    "screen_content"}
                    if any(e.get("type") in _displayable for e in events):
                        _min_wait_until = max(_min_wait_until,
                                              time.time() + 3.0)
                    if any(e.get("type") in ("narration", "dialogue") for e in events):
                        _latest_story_seq = max(
                            _latest_story_seq,
                            *(
                                _event_seq(e)
                                for e in events
                                if e.get("type") in ("narration", "dialogue")
                            ),
                        )
                        _latest_story_cursor = max(
                            _latest_story_cursor,
                            int(self.cursor or 0),
                        )
                        _unchanged_story_screen_defer_until = max(
                            _unchanged_story_screen_defer_until,
                            time.time() + max(12.0, min_wait * 4.0),
                        )
                if any(
                    e.get("type") == "screen_content"
                    and e.get("passive_overlay_snapshot")
                    for e in events
                ):
                    _passive_overlay_quiet_until = max(
                        _passive_overlay_quiet_until,
                        time.time() + 3.0,
                    )
            else:
                idle_rounds += 1
                _dbg(f"loop {_loop}: 0 events, idle={idle_rounds} cursor={self.cursor}")

            if time.time() < _passive_overlay_quiet_until:
                _dbg("passive overlay still inside settle window")
                time.sleep(min(
                    0.2,
                    max(0.0, deadline - time.time()),
                    max(0.0, _passive_overlay_quiet_until - time.time()),
                ))
                continue

            # The long poll above owns the remaining wait budget. Do not turn
            # an on-time empty/story response into post-deadline status,
            # pending, or screen traffic.
            if time.time() >= deadline:
                break

            # Check for sticky anomaly flag (set by exception handler).
            code_s, data_s = self._get(
                "/status", timeout=request_timeout(2.0))
            if code_s == 200 and data_s:
                anomaly = data_s.get("anomaly")
                if anomaly and self._observe_new_anomaly(anomaly):
                    _emit([anomaly])
                if (
                    data_s.get("status") == "ended"
                    and lifecycle_is_live
                ):
                    # A lifecycle restart/resume in this same batch is newer
                    # than the sticky ended status. Continue to the pending
                    # probe instead of spending the whole deadline debouncing
                    # an end state we already know is obsolete.
                    ended_seen_at = None
                elif data_s.get("status") == "ended":
                    if any(e.get("type") == "game_ended"
                           and e.get("terminal") is not False
                           for e in events):
                        if not _has_stale_return_to_menu_batch(events):
                            _dbg(f"loop {_loop}: game_ended event, returning")
                            return WaitResult(events=all_events, ended=True,
                                              screen=screen_snapshot)
                        _dbg(f"loop {_loop}: stale return_to_menu batch ignored")

                    code, screen_data = self._get(
                        "/screen", timeout=request_timeout(2.0))
                    if code == 200 and screen_data:
                        screen = screen_data.get("screen")
                        if screen:
                            screen_snapshot = screen
                            if has_actionable_screen_buttons(screen):
                                _dbg(f"loop {_loop}: ended status but screen has buttons")
                                return WaitResult(events=all_events,
                                                  screen=screen_snapshot)

                    now = time.time()
                    if ended_seen_at is None:
                        ended_seen_at = now
                        _dbg(f"loop {_loop}: ended status first seen, debouncing")
                        time.sleep(min(_ENDED_STATUS_POLL_DELAY,
                                       max(0.0, deadline - now)))
                        continue
                    if now - ended_seen_at < _ENDED_STATUS_GRACE:
                        _dbg(f"loop {_loop}: ended status still in grace window")
                        time.sleep(min(_ENDED_STATUS_POLL_DELAY,
                                       max(0.0, deadline - now)))
                        continue
                    _dbg(f"loop {_loop}: game ended after grace, returning")
                    return WaitResult(events=all_events, ended=True,
                                      screen=screen_snapshot)
                ended_seen_at = None

            if time.time() >= deadline:
                break

            # Check pending choice/input.
            has_request_event = any(
                e.get("type") in ("choice_request", "input_request")
                for e in events
            )
            retries = 5 if has_request_event else 3
            _dbg(f"loop {_loop}: poll_pending retries={retries} has_req_ev={has_request_event} acted={self._acted_request_id}")
            pending = self._poll_pending(
                retries=retries, delay=0.2, deadline=deadline)
            if pending is not None:
                _dbg(f"loop {_loop}: got pending id={pending.get('id')} type={pending.get('type')}")
                if _is_acted_pending(pending) and time.time() < _min_wait_until:
                    self._acted_request_id = acted_request_id_at_start
                    self._last_poll_pending = None
                    _dbg(f"loop {_loop}: skip acted pending id={pending.get('id')}")
                    time.sleep(min(0.2, max(0.0, deadline - time.time())))
                    continue
                # During min_wait window, don't return on a new pending
                # unless we have displayable story events.  This prevents
                # returning "(no new events)" when the game transitions
                # quickly from one choice to another (e.g. attitude menus).
                _displayable_types = {"narration", "dialogue", "auto_skipped"}
                _has_story = any(e.get("type") in _displayable_types
                                 for e in all_events)
                if _min_wait_until > 0 and time.time() < _min_wait_until and not _has_story:
                    _dbg(f"loop {_loop}: pending found but min_wait active, continuing")
                    # Stash pending for later — don't lose it.
                    self._last_poll_pending = pending
                elif pending.get("_auto_advancing") and time.time() < deadline:
                    _dbg(f"loop {_loop}: pending is auto-advancing, continuing")
                    # The shim will resolve this single choice once its
                    # human-readable delay elapses. Keep waiting rather than
                    # presenting it as a decision point. Story already emitted
                    # in this wait call is preserved in all_events.
                    self._last_poll_pending = pending
                    time.sleep(min(0.2, max(0.0, deadline - time.time())))
                    continue
                elif (
                    _is_single_continue_pending(pending)
                    and not _has_story
                    and time.time() < deadline
                ):
                    # Just after load, a pending can arrive one poll before
                    # the shim publishes or resolves its auto-skip state. Give
                    # that resolver a short chance; stable manual continues
                    # still return after this grace.
                    rid = str(pending.get("id") or "")
                    now = time.time()
                    first_seen = single_continue_seen_at.setdefault(rid, now)
                    if (
                        now - first_seen < _SINGLE_CONTINUE_PENDING_GRACE
                        and deadline - now > _SINGLE_CONTINUE_DEADLINE_MARGIN
                    ):
                        _dbg(f"loop {_loop}: deferring bare continue pending")
                        self._last_poll_pending = pending
                        time.sleep(min(0.2, max(0.0, deadline - now)))
                        continue
                    return WaitResult(events=all_events, pending=pending,
                                      screen=screen_snapshot)
                else:
                    return WaitResult(events=all_events, pending=pending,
                                      screen=screen_snapshot)
            _dbg(f"loop {_loop}: no pending found")

            # Screen buttons (e.g. main menu) — query stateful /screen endpoint.
            # Use lower threshold (1) when we already have story events —
            # buttons appearing after dialogue are a decision point.
            # Skip early-exit checks during the min_wait window.
            delivered_screen = getattr(
                self, "_last_delivered_actionable_screen", None,
            )
            delivered_screen_sig = getattr(
                self, "_last_delivered_actionable_screen_signature", (),
            )
            delivered_screen_seq = _event_seq(delivered_screen)
            hint_eligible = bool(
                delivered_screen_sig
                and delivered_screen_seq > 0
                and action_nonce is None
                and not ordinary_only
                and min_wait <= 0
                and not self._prefetched_events
                and not self._active_action_nonces
            )
            if not hint_eligible:
                delivered_screen_sig = ()
            # A previously delivered custom-screen decision is a useful hint,
            # not authority. Always perform one ordinary poll first so queued
            # story wins; after that, one matching live /screen read proves the
            # unchanged decision without two more five-second idle rounds.
            _idle_threshold = 1 if all_events or delivered_screen_sig else 3
            if idle_rounds >= _idle_threshold and time.time() >= _min_wait_until:
                _dbg(f"loop {_loop}: idle>=3, extra pending check")
                pending = self._poll_pending(
                    retries=3, delay=0.2, deadline=deadline)
                if pending is not None:
                    if _is_acted_pending(pending) and time.time() < _min_wait_until:
                        self._acted_request_id = acted_request_id_at_start
                        self._last_poll_pending = None
                        _dbg(f"loop {_loop}: skip idle acted pending id={pending.get('id')}")
                        time.sleep(min(0.2, max(0.0, deadline - time.time())))
                        continue
                    _dbg(f"loop {_loop}: idle pending found id={pending.get('id')}")
                    return WaitResult(events=all_events, pending=pending,
                                      screen=screen_snapshot)
                code, screen_data = self._get(
                    "/screen", timeout=request_timeout(2.0))
                if code == 200 and screen_data:
                    screen = screen_data.get("screen")
                    if screen:
                        screen_snapshot = screen
                        _btn_count = len(screen.get("buttons", []))
                        _has_actionable_buttons = has_actionable_screen_buttons(
                            screen,
                        )
                        _dbg(
                            f"loop {_loop}: screen has {_btn_count} buttons, "
                            f"screen_seq={_event_seq(screen)} "
                            f"latest_story_seq={_latest_story_seq} "
                            f"latest_story_cursor={_latest_story_cursor} "
                            f"all_events={len(all_events)}"
                        )
                        if _has_actionable_buttons and not all_events:
                            same_delivered_occurrence = bool(
                                delivered_screen_sig
                                and screen_signature(screen)
                                == delivered_screen_sig
                                and _event_seq(screen) == delivered_screen_seq
                            )
                            if (
                                delivered_screen_sig
                                and idle_rounds < 3
                                and not same_delivered_occurrence
                            ):
                                # The cached decision was only permission to
                                # confirm that same screen early. A changed
                                # surface may have outrun its story events, so
                                # retain the normal three-poll boundary.
                                _dbg(
                                    "defer changed screen after cached "
                                    "decision"
                                )
                                continue
                            screen_sig = _nvl_continue_signature(screen)
                            now = time.time()
                            if (
                                screen_sig is not None
                                and deadline - now > _SINGLE_CONTINUE_DEADLINE_MARGIN
                            ):
                                first_seen = single_continue_screen_seen_at.setdefault(
                                    screen_sig,
                                    now,
                                )
                                if (
                                    now - first_seen
                                    < _SINGLE_CONTINUE_PENDING_GRACE
                                ):
                                    _dbg(f"loop {_loop}: defer nvl continue screen-buttons")
                                    time.sleep(min(0.2, max(0.0, deadline - now)))
                                    continue
                            _dbg(f"loop {_loop}: EXIT screen-buttons (no story events)")
                            return WaitResult(events=all_events,
                                              screen=screen_snapshot)
                        # Buttons visible after story events but no
                        # pending choice_request — the buttons ARE the
                        # decision point (e.g. NVL topic buttons).
                        if (
                            _has_actionable_buttons
                            and all_events
                            and pending is None
                        ):
                            screen_sig = _nvl_continue_signature(screen)
                            if (
                                screen_sig is not None
                                and screen_sig not in deferred_nvl_continue_sigs
                            ):
                                deferred_nvl_continue_sigs.add(screen_sig)
                                _dbg(f"loop {_loop}: defer nvl continue screen-buttons")
                                continue
                            if (
                                (
                                    _latest_story_seq > 0
                                    and _event_seq(screen) <= _latest_story_seq
                                )
                                or (
                                    _latest_story_cursor > 0
                                    and _event_seq(screen) <= _latest_story_cursor
                                )
                            ):
                                _dbg(f"loop {_loop}: defer stale pre-story screen-buttons")
                                time.sleep(min(0.2, max(0.0, deadline - time.time())))
                                continue
                            if (
                                _initial_screen_sig is not None
                                and screen_signature(screen) == _initial_screen_sig
                                and time.time() < _unchanged_story_screen_defer_until
                            ):
                                _dbg(f"loop {_loop}: defer unchanged story screen-buttons")
                                time.sleep(min(0.2, max(0.0, deadline - time.time())))
                                continue
                            _dbg(f"loop {_loop}: EXIT screen-buttons (story + no pending)")
                            return WaitResult(events=all_events,
                                              screen=screen_snapshot)
                        # Overlay early exit: if an overlay is active and
                        # the screen content has stabilized (idle), return
                        # without waiting for a pending choice (overlays
                        # don't produce choice_requests).
                        if (
                            screen.get("overlay_active")
                            and _has_actionable_buttons
                        ):
                            _dbg(f"loop {_loop}: EXIT overlay-active ({_btn_count} buttons)")
                            return WaitResult(events=all_events,
                                              screen=screen_snapshot)

            if not events:
                time.sleep(min(0.5, max(0.0, deadline - time.time())))

        _dbg(f"timeout reached, final pending check. all_events={len(all_events)}")
        pending = self._fresh_unacted_cached_pending()
        _dbg(f"final pending: {pending.get('id') if pending else None}")
        return WaitResult(events=all_events, pending=pending,
                          screen=screen_snapshot)

    # -- actions -------------------------------------------------------------

    def act_transaction(
        self,
        target: int | str,
        *,
        action_nonce: str | None = None,
        accept_timeout: float = 15.0,
        deadline: float | None = None,
        invocation: dict | None = None,
    ) -> dict:
        """Submit an act and return once the bridge acknowledges it."""
        nonce = action_nonce or uuid.uuid4().hex
        self._last_command_nonce = nonce
        accept_timeout = max(0.0, float(accept_timeout))
        accept_deadline = time.time() + accept_timeout
        if isinstance(deadline, (int, float)):
            accept_deadline = min(accept_deadline, float(deadline))
        remaining = accept_deadline - time.time()
        if remaining <= 0:
            return {
                "action_nonce": nonce,
                "transaction_state": "rejected",
                "pending": False,
                "reason": "result_timeout_before_submission",
                "ok": False,
                "success": False,
            }
        code, state = self._get("/state", timeout=min(3.0, remaining))
        if code != 200 or not isinstance(state, dict):
            return {
                "action_nonce": nonce,
                "transaction_state": "rejected",
                "pending": False,
                "reason": "state_unavailable",
                "ok": False,
                "success": False,
            }
        self._observe_action_delivery_generation(
            state.get("reset_generation"))
        try:
            generation = int(state.get("reset_generation", 0) or 0)
        except (TypeError, ValueError):
            generation = 0
        args = {"index": target} if isinstance(target, int) else {
            "label": str(target)
        }
        command = {
            "name": "act",
            "args": args,
            "nonce": nonce,
            "reset_generation": generation,
        }
        if isinstance(invocation, dict):
            command["_invocation"] = dict(invocation)
        remaining = accept_deadline - time.time()
        if remaining <= 0:
            return {
                "action_nonce": nonce,
                "transaction_state": "rejected",
                "pending": False,
                "reason": "result_timeout_before_submission",
                "ok": False,
                "success": False,
            }
        self._invalidate_delivered_screen_hint()
        code, response = self._post(
            "/command", command, timeout=max(0.001, remaining / 2),
        )
        retried = False
        if code not in {200, 400, 409}:
            # The response may have been lost after acceptance. Reusing the
            # exact nonce and generation makes this retry idempotent.
            retried = True
            remaining = accept_deadline - time.time()
            if remaining <= 0:
                code, response = 0, response
            else:
                code, response = self._post(
                    "/command", command, timeout=max(0.001, remaining),
                )
        if (
            code == 409
            and isinstance(response, dict)
            and response.get("reason") == "action_in_flight"
        ):
            # Absorb exactly one.  The bridge rejects a submission while a
            # prior action is applied and unreleased; the prior usually settles
            # within a fraction of a second of the act() that produced it, so
            # the overwhelming majority of these are a race with our own
            # previous call rather than a genuinely busy slot.
            #
            # The nonce is REUSED deliberately.  The rejection is synthesized
            # in _accept_act_transaction and is neither stored in the registry
            # nor journalled, so nothing is recorded under it and this exact
            # command stays replayable — which also makes the resubmit
            # idempotent if ITS response is lost.  Minting a fresh nonce would
            # give up that property for nothing.
            if self._await_blocking_admission(
                response, deadline=accept_deadline,
            ):
                remaining = accept_deadline - time.time()
                if remaining > 0:
                    code, response = self._post(
                        "/command", command,
                        timeout=max(0.001, remaining),
                    )
            # A second 409 falls through untouched: one retry, then the
            # caller sees the bridge's own answer.
        if code == 200 and response:
            # Every bridge ack carries transaction_state (submit_command_with_ack).
            self._track_action_nonce(nonce, response.get("action_id"))
            # Prefetched events PREDATE this transaction and are already past
            # the ordinary cursor — clearing them here dropped them on the
            # floor (the spec's problem #2, recreated).  The transaction's own
            # output arrives through the action-scoped drain, never through
            # poll(), so keeping the buffer cannot mix the two.
            if retried:
                response["_retried_after_transport_timeout"] = True
            return response
        if code in {400, 409} and response:
            return response
        self._track_action_nonce(nonce)
        return {
            "action_nonce": nonce,
            "reset_generation": generation,
            "submitted_target": target,
            "transaction_state": "acceptance_unknown",
            "pending": True,
        }

    # One bounded absorption of an `action_in_flight` 409, never a poll loop:
    # a prior action that produced no observable outcome keeps the slot busy
    # for the bridge's full idle budget (tens of seconds), and waiting that out
    # here would turn a clear error into a hang.
    # How long the absorb-one retry will poll the BLOCKER before giving up
    # and surfacing the 409. Sized from evidence, not hope: the f3 wave's
    # transaction journal (playthrough 40060) shows structural settles at
    # p50 1.8s / p90 3.0s / max 3.4s, and attributed-evidence boundaries
    # hold admission for the full 5.0s post-settle grace. The old 3.0s cap
    # sat exactly at p90, so every fleet lane leaked ~8-12 agent-visible
    # "action_in_flight" errors per run on ordinary quick successive acts
    # (Echoes' menu loops re-offer faster than the settle grace closes).
    # 8.0s covers the attributed grace with margin; the caller's
    # accept_timeout still bounds the total.
    _ADMISSION_RETRY_CAP = 8.0
    _ADMISSION_POLL_INTERVAL = 0.25

    def _await_blocking_admission(
        self, rejection: dict, *, deadline: float,
    ) -> bool:
        """Whether the transaction that rejected us stopped blocking in time.

        Polls the BLOCKER (identified by ``blocking_action_nonce`` on the 409
        body) rather than re-POSTing the act: ``GET /transaction`` is a peek,
        so it drains nothing and cannot double-submit while we look.
        """
        nonce = rejection.get("blocking_action_nonce")
        if not nonce:
            return False
        end = min(deadline, time.time() + self._ADMISSION_RETRY_CAP)
        # The bridge holds the gate for an APPLIED blocker only until its
        # trailing-attribution grace has run from the first outcome it was
        # seen to produce, and it reports how long that is.  ``/transaction``
        # deliberately does NOT publish that as ``admission_open`` (which the
        # act path's scoped drain treats as an early exit), so the retry-after
        # is what the poll below has to work with.  Fleet R64 measured the
        # alternative: two acts spent the whole 8 s cap polling a blocker
        # whose gate reopened on a clock the client could not see, and
        # surfaced a hard `action_in_flight` error at 10.2 s.
        admission_due = rejection.get("admission_retry_after")
        admission_due = (
            time.time() + max(0.0, float(admission_due))
            if isinstance(admission_due, (int, float))
            and not isinstance(admission_due, bool)
            else None
        )
        while True:
            now = time.time()
            if admission_due is not None and now >= admission_due:
                return True
            # No poll and no sleep may run past the reported hold: the point
            # is to resubmit AT it, not one poll interval later.  The cap
            # still bounds the whole absorb, so a hold beyond it simply
            # surfaces the 409 as before.
            budget_end = end if admission_due is None else min(
                end, admission_due)
            remaining = budget_end - now
            if remaining <= 0:
                return False
            blocking = self.action_transaction(
                str(nonce), timeout=min(1.0, remaining),
            )
            if blocking is None:
                # Unknown to the bridge (404) — it cannot be blocking us.
                return True
            if (
                blocking.get("transaction_state")
                in {"settled", "failed", "rejected"}
                or blocking.get("admission_open")
            ):
                return True
            remaining = budget_end - time.time()
            if remaining <= 0:
                # The top of the loop decides which bound was reached.
                continue
            time.sleep(min(self._ADMISSION_POLL_INTERVAL, remaining))

    def action_transaction(
        self, action_nonce: str, *, timeout: float = 3.0,
    ) -> dict | None:
        """Peek at a transaction record without draining or acknowledging it.

        ``GET /transaction`` is replayable until ``acknowledge_action_events``
        succeeds, so a peek moves no cursor and marks nothing delivered — the
        events are deliberately dropped here rather than recorded in
        ``_delivered_action_events``, leaving them for the scoped drain.
        """
        timeout = max(0.0, float(timeout))
        if timeout <= 0:
            return None
        code, data = self._get(
            "/transaction",
            params={"action_nonce": action_nonce},
            timeout=timeout,
        )
        if code != 200 or not isinstance(data, dict):
            return None
        transaction = data.get("transaction")
        if not isinstance(transaction, dict):
            return None
        transaction = dict(transaction)
        transaction.pop("events", None)
        return transaction

    def act(self, target: int | str, *, _nonce: str | None = None) -> dict:
        """Submit a visible choice or screen button transaction.

        *_nonce* is internal: the transport-timeout retry path passes the
        original attempt's nonce so the bridge dedups the replay rather than
        applying the choice twice.  Normal callers omit it (a fresh nonce is
        minted per act).
        """
        return self.act_transaction(target, action_nonce=_nonce)

    def input_text(
        self,
        text: str,
        *,
        deadline: float | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Submit text input.

        Input still uses the bridge /act endpoint (the shim's input
        wrapper polls for it during renpy.input interactions).
        """
        data: dict = {"type": "input", "text": text}
        submitted_request_id = request_id or self.last_request_id
        if submitted_request_id:
            data["request_id"] = submitted_request_id
        timeout = 5.0
        if deadline is not None:
            timeout = min(timeout, max(0.0, deadline - time.time()))
        if timeout <= 0:
            return {
                "ok": False,
                "success": False,
                "reason": "transport_timeout_before_submission",
            }
        self._invalidate_delivered_screen_hint()
        code, resp = self._post("/act", data, timeout=timeout)
        if code == 200 and submitted_request_id:
            self._acted_request_id = submitted_request_id
        if code == 0:
            return {
                "ok": False,
                "success": False,
                "confirmed": False,
                "acceptance_unknown": True,
                "mutation_may_have_applied": True,
                "retry_safe": bool(submitted_request_id),
                "request_id": submitted_request_id,
                "reason": "input_acceptance_unknown",
                "error": (
                    "Input submission may have reached the bridge. Inspect "
                    "the current request, or retry the identical text with "
                    "this request_id."
                ),
            }
        return {"ok": code == 200, **(resp or {})}

    # -- config --------------------------------------------------------------

    def set_config(
        self, config: dict, *, deadline: float | None = None,
    ) -> dict:
        """Push bridge-side runtime config (POST /config): auto_advance,
        auto_advance_delay, end_on_menu_return, ..."""
        timeout = 5.0
        if deadline is not None:
            timeout = min(timeout, max(0.0, deadline - time.time()))
        if timeout <= 0:
            return {
                "ok": False,
                "success": False,
                "reason": "transport_timeout_before_submission",
            }
        code, resp = self._post(
            "/config", {"config": config}, timeout=timeout)
        return {"ok": code == 200, **(resp or {})}

    def set_auto_advance(
        self, enabled: bool, delay: float | None = None,
        *, deadline: float | None = None,
    ) -> dict:
        """Apply auto-forward in the game, then mirror confirmed bridge config."""
        config: dict = {"auto_advance": enabled}
        if delay is not None:
            config["auto_advance_delay"] = delay
        if deadline is None:
            deadline = time.time() + 15.0
        result = self.command("set", changes=config, _deadline=deadline)
        if result.get("success") is not True:
            return {**result, "ok": False}
        mirrored = self.set_config(config, deadline=deadline)
        return {**result, **mirrored}

    # -- commands ------------------------------------------------------------

    def _send_command(
        self, name: str, args: dict | None = None, nonce: str | None = None,
        *, timeout: float = 15.0,
    ) -> _CommandSubmission:
        cmd: dict = {"name": name}
        if args:
            cmd["args"] = args
        # Idempotency nonce: lets the bridge dedup a transport-timeout retry
        # (the POST enqueued but its response was lost) so the same logical
        # command can't double-apply.  Omitted when None for backward compat
        # with tokenless/raw callers the bridge treats exactly as before.
        if nonce:
            cmd["nonce"] = nonce
        # A live bridge answers POST /command near-instantly (it just appends
        # to a queue under a lock).  A slow response means the bridge is busy
        # under lock contention — e.g. the shim's ~20/s screenshot POST flood
        # on heavy streaming runs — NOT that it is dead.  The default 5s read
        # timeout fired during those spikes and surfaced "Connection failed:
        # timed out" while the command never landed (a silent act no-op).  Ride
        # the contention out instead of false-failing the submission.
        if timeout <= 0:
            return _CommandSubmission(False, "Command deadline expired")
        code, resp = self._post("/command", cmd, timeout=timeout)
        if code == 200 and resp:
            return _CommandSubmission(
                True, resp.get("message", f"Command '{name}' sent."))
        return _CommandSubmission(
            False,
            (resp or {}).get("error", "Command failed"),
            acceptance_unknown=(code == 0),
        )

    def _wait_command_result(
        self,
        command: str,
        timeout: float = 3.0,
        *,
        after_seq: int | None = None,
        match: Callable[[dict], bool] | None = None,
        reset_after_generation: int | None = None,
        load_after_signature: tuple | None = None,
    ) -> dict | None:
        deadline = time.time() + timeout
        # Preserve a final authoritative read for nonce-matched commands. An
        # incremental cursor can advance past a result observed by another
        # concurrent consumer; the full transcript is the durable authority.
        final_lookup_reserve = (
            min(3.0, timeout * 0.5) if match is not None and timeout >= 5.0
            else 0.0
        )
        poll_deadline = deadline - final_lookup_reserve
        def event_sequence(event: dict) -> int:
            try:
                return int(event.get("_seq", 0) or 0)
            except (TypeError, ValueError):
                return 0

        def event_after_boundary(event: dict) -> bool:
            if after_seq is None:
                return True
            seq = event_sequence(event)
            return not seq or seq > after_seq

        def should_prefetch(
            event: dict,
            *,
            matched_result_seq: int | None = None,
        ) -> bool:
            if event.get("type") == "command_result":
                return False
            if event.get("type") in {"choice_request", "input_request"}:
                # A request emitted before the command ack may be the active
                # menu being re-registered, even when its sequence is newer
                # than the preflight boundary. Only a request demonstrably
                # emitted after this result can be its successor. Pending
                # state remains the authority when that ordering is unknown.
                seq = event_sequence(event)
                return bool(
                    command != "act"
                    and matched_result_seq
                    and seq
                    and seq > matched_result_seq
                )
            if command in {"act", "start", "load"}:
                if not event_after_boundary(event):
                    # An act deliberately starts a new visible result window;
                    # start/load replace the timeline outright. Ordinary
                    # commands do neither. In particular, a refused advance
                    # must not consume older story rows that happened to
                    # arrive after the caller's preceding wait.
                    return False
            return True

        while time.time() < poll_deadline:
            remaining = poll_deadline - time.time()
            if remaining <= 0:
                break
            # A fleet can put several slot requests ahead of this read in the
            # bridge's bounded worker pool.  Sub-second reads then all time out
            # even when the durable command_result already exists.  Give each
            # attempt enough time to reach the server while preserving the
            # final full-transcript lookup below.
            events = self.poll(
                timeout=min(2.0, remaining),
                include_prefetched=False,
            )
            for ev in events:
                if (
                    ev.get("type") == "command_result"
                    and ev.get("command") == command
                    and event_after_boundary(ev)
                    and (match is None or match(ev))
                ):
                    result_seq = event_sequence(ev)
                    self._hold_events(
                        e for e in events
                        if (
                            e is not ev
                            and should_prefetch(
                                e, matched_result_seq=result_seq,
                            )
                        )
                    )
                    return ev
            if command == "load" and any(
                ev.get("type") == "game_resumed"
                and ev.get("reason") == "load"
                and event_after_boundary(ev)
                for ev in events
            ):
                # Ren'Py applies a load by raising a control-flow exception.
                # On some versions the shim's success command_result, pushed
                # from that exception arm, is lost while the after-load
                # callback's lifecycle event survives.  This event is emitted
                # only after the restored state has been installed, so it is
                # a stronger confirmation than a visual state heuristic.
                return {
                    "type": "command_result",
                    "command": command,
                    "success": True,
                    "confirmed": True,
                    "confirmation": "game_resumed",
                }
            # Inference is strictly a fallback: a nonce-matched result in the
            # same batch, especially a failure, always wins. A generation
            # increment proves this same bridge reset; a raw counter rewind
            # does not, because replacing the bridge process also rewinds it.
            generation = getattr(self, "_last_reset_generation", None)
            if (reset_after_generation is not None
                    and generation is not None
                    and generation > reset_after_generation):
                return {
                    "type": "command_result",
                    "command": command,
                    "success": True,
                    "confirmed": True,
                    "confirmation": "bridge_reset",
                }
            current_signature = getattr(self, "_last_state_signature", None)
            current_pending = current_signature[2:] if current_signature else ()
            prior_pending = load_after_signature[2:] if load_after_signature else ()
            if (load_after_signature is not None
                    and getattr(self, "_last_has_pending_command", None) is False
                    and current_signature != load_after_signature
                    and current_pending != prior_pending
                    and any(current_pending)):
                return {
                    "type": "command_result",
                    "command": command,
                    "success": True,
                    "confirmed": True,
                    "confirmation": "state_transition",
                }
            self._hold_events(e for e in events if should_prefetch(e))
            if not events:
                time.sleep(0.1)
        # The authoritative endpoint is an ordinary GET, not a long poll.  A
        # quick empty response therefore must not spend the whole reserved
        # observation window: keep checking through the caller's real
        # deadline while giving each read enough time to clear fleet queueing.
        while match is not None:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            code, state = self._get(
                "/state", timeout=min(2.0, remaining))
            if code == 200 and isinstance(state, dict):
                self._observe_action_delivery_generation(
                    state.get("reset_generation"))
                # This authoritative read also observes the decision that may
                # have raced a queued command.  Preserve it before returning
                # the matching result so a deadline-exhausted handler can
                # expose the menu without another bridge round trip.
                pending = self._enrich_pending_from_state(
                    state.get("pending_request"), state,
                )
                self.last_actionable_snapshot = actionable_state_snapshot(
                    state,
                )
                self._remember_pending(pending)
                for event in reversed(state.get("transcript") or []):
                    if not isinstance(event, dict):
                        continue
                    if (
                        event.get("type") == "command_result"
                        and event.get("command") == command
                        and match(event)
                    ):
                        return event
            remaining = deadline - time.time()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        return None

    def discard_rendered_action_transaction(
        self,
        action_nonce: str,
        *,
        action_id: object = None,
        timeout: float = 1.5,
    ) -> WaitResult:
        """Drain a screen action already represented by an authoritative state.

        Screen-only/info buttons settle through ``state()`` rather than their
        story receipt. Their durable presentation snapshots must still be
        acknowledged, otherwise the old nonce remains auto-selectable and a
        later plain wait can replay that panel ahead of newer story output.
        Semantic events such as inventory/stat changes are not represented by
        that screen snapshot, so the returned receipt preserves them.

        Neither are the game's OWN say/narration lines.  "Represented by the
        rendered state" is true of a screen scrape and of nothing else: a
        script line is an occurrence that the successor screen does not
        contain, so discarding it here is a silent drop, not a de-duplication.
        Fleet R62 lost ARIA's antenna-repair instruction that way.  Script
        story is therefore preserved exactly like the semantic rows -- kept in
        the prefetch stash for the next wait when it is parked there, and
        returned on the receipt when the transaction still holds it.
        """
        try:
            rendered_action_id = int(action_id or 0)
        except (TypeError, ValueError):
            rendered_action_id = 0
        semantic_types = {
            "inventory_update", "stats_update", "error", "anomaly",
            "command_error",
            # The script's own output.  screen_text is deliberately absent:
            # that IS the panel the rendered state already shows.
            "narration", "dialogue", "auto_skipped",
        }
        if rendered_action_id > 0:
            remaining_prefetch = []
            delivered_keys = []
            for event in list(self._prefetched_events or []):
                try:
                    event_action_id = int(event.get("action_id", 0) or 0)
                except (AttributeError, TypeError, ValueError):
                    event_action_id = 0
                if event_action_id != rendered_action_id:
                    remaining_prefetch.append(event)
                    continue
                if event.get("type") in semantic_types:
                    remaining_prefetch.append(event)
                    continue
                try:
                    key = (
                        event_action_id,
                        int(event.get("_seq", 0) or 0),
                    )
                    if key[1] > 0:
                        delivered_keys.append(key)
                except (AttributeError, TypeError, ValueError):
                    pass
            self._retain_held_events(remaining_prefetch)
            self._record_delivered_action_events(delivered_keys)
        try:
            receipt = self._wait_action_transaction(
                action_nonce,
                timeout=max(0.0, float(timeout)),
                unknown_is_pending=False,
            )
        except Exception:
            receipt = WaitResult(events=[], transaction={
                "action_nonce": action_nonce,
                "transaction_state": "acceptance_unknown",
                "pending": True,
                "reason": "rendered_action_receipt_unavailable",
            })
        finally:
            if action_nonce in self._active_action_nonces:
                self._retire_auto_action_nonce(
                    action_nonce, "rendered_screen_state",
                )
        receipt.events = [
            event for event in (receipt.events or [])
            if isinstance(event, dict) and event.get("type") in semantic_types
        ]
        return receipt

    def command(
        self,
        cmd_name: str,
        *,
        _nonce: str | None = None,
        _deadline: float | None = None,
        **args: Any,
    ) -> dict:
        """Send a generic command and wait for result.

        Each logical command carries a fresh idempotency nonce unless
        *_nonce* is supplied (the transport-timeout retry reuses the original
        attempt's nonce so the bridge dedups the replay).
        """
        nonce = _nonce or uuid.uuid4().hex
        self._last_command_nonce = nonce
        old_prefetched_events: list[dict] = []
        command_start_seq: int | None = None
        command_start_generation: int | None = None
        command_start_signature: tuple | None = None
        reset_boundary_command = cmd_name in {"start", "load"}
        remaining = (
            _deadline - time.time() if _deadline is not None else 2.0)
        if remaining <= 0:
            return {
                "ok": False,
                "success": False,
                "reason": "transport_timeout_before_submission",
            }
        try:
            code, state = self._get("/state", timeout=min(2.0, remaining))
            if code == 200 and isinstance(state, dict):
                self._observe_action_delivery_generation(
                    state.get("reset_generation"))
                command_start_seq = int(state.get("event_counter", 0) or 0)
                generation = state.get("reset_generation")
                if generation is not None:
                    command_start_generation = int(generation)
                pending = state.get("pending_request") or {}
                context = state.get("context")
                if isinstance(context, dict):
                    context = context.get("context")
                command_start_signature = (
                    state.get("status"),
                    context,
                    pending.get("type"),
                    pending.get("id"),
                    tuple(pending.get("choices") or ()),
                )
        except Exception:
            command_start_seq = None
        remaining = (
            _deadline - time.time() if _deadline is not None else 15.0)
        if remaining <= 0:
            return {
                "ok": False,
                "success": False,
                "reason": "transport_timeout_before_submission",
            }
        if cmd_name == "act":
            # Clear only immediately before submission. Preflight deadline
            # exits must not discard observations that no mutation consumed.
            old_prefetched_events = self._take_held_events()
        self._invalidate_delivered_screen_hint()
        submission = self._send_command(
            cmd_name,
            args if args else None,
            nonce=nonce,
            timeout=min(15.0, remaining),
        )
        ok, msg = submission
        post_acceptance_unknown = bool(
            getattr(submission, "acceptance_unknown", False))
        if ok:
            result_match = lambda event: event.get("nonce") == nonce
            # `act` can trigger long game sequences (e.g. Slay the Princess
            # chapter-transition cutscenes) whose command_result only arrives
            # after the scene settles — well past 5s.  _wait_command_result
            # POLLS until the result or the deadline, so a generous act timeout
            # doesn't slow normal acts (they return the instant the result
            # lands); it only lets a slow-but-valid result ride out instead of
            # false-timing-out.  Loads are also reset-boundary operations that
            # may spend substantial time entering the restored scene before
            # their confirmation becomes observable. Profile application can
            # likewise span several settings. Other commands keep the snappy
            # default.
            bulk_profile_set = cmd_name == "set" and "changes" in args
            _result_timeout = (
                45.0 if cmd_name in {"act", "load"} or bulk_profile_set
                else 5.0
            )
            if _deadline is not None:
                _result_timeout = min(
                    _result_timeout, max(0.0, _deadline - time.time()))
            if _result_timeout <= 0:
                return {
                    "ok": False,
                    "success": False,
                    "confirmed": False,
                    "acceptance_unknown": True,
                    "mutation_may_have_applied": True,
                    "retry_safe": False,
                    "command_nonce": nonce,
                    "transaction_state": "acceptance_unknown",
                    "reason": "transport_timeout_after_submission",
                    "message": msg,
                }
            result = self._wait_command_result(
                cmd_name,
                timeout=_result_timeout,
                after_seq=None if reset_boundary_command else command_start_seq,
                match=result_match,
                reset_after_generation=(
                    command_start_generation if cmd_name == "load" else None
                ),
                load_after_signature=(
                    command_start_signature if cmd_name == "load" else None
                ),
            )
            if result:
                if (cmd_name == "load"
                        and result.get("confirmation") in {
                            "bridge_reset", "state_transition",
                        }):
                    result["nonce"] = nonce
                    if args.get("slot"):
                        result["slot"] = args["slot"]
                return result
            if old_prefetched_events:
                self._restore_held_events(old_prefetched_events)
            # The command was accepted by the bridge, but the game never
            # pushed a command_result.  Reporting ok:True here made
            # save/load/etc. look successful when the shim never executed
            # them (e.g. the command was lost or the game is wedged).
            return {
                "ok": False,
                "success": False,
                "confirmed": False,
                "acceptance_unknown": True,
                "mutation_may_have_applied": True,
                "retry_safe": False,
                "command_nonce": nonce,
                "transaction_state": "acceptance_unknown",
                "reason": "command_result_timeout_after_submission",
                "error": (
                    f"Command '{cmd_name}' was submitted but not confirmed "
                    "by the game (no result within timeout). Do not submit "
                    "a fresh retry; inspect state or retry with command_nonce."
                ),
                "message": msg,
            }
        if old_prefetched_events:
            self._restore_held_events(old_prefetched_events)
        if post_acceptance_unknown:
            return {
                "ok": False,
                "success": False,
                "confirmed": False,
                "acceptance_unknown": True,
                "mutation_may_have_applied": True,
                "retry_safe": False,
                "command_nonce": nonce,
                "transaction_state": "acceptance_unknown",
                "reason": "transport_timeout_after_submission",
                "error": (
                    f"Command '{cmd_name}' may have reached the bridge. "
                    "Do not submit a fresh retry; inspect state or retry "
                    "with command_nonce."
                ),
                "message": msg,
            }
        return {"ok": False, "error": msg}

    # -- state queries -------------------------------------------------------

    def state(self, *, timeout: float = 3.0) -> dict:
        """Full game state including transcript, pending, inventory."""
        code, data = self._get("/state", timeout=max(0.0, timeout))
        if code == 200 and data:
            self._observe_action_delivery_generation(
                data.get("reset_generation"))
            pending = self._enrich_pending_from_state(
                data.get("pending_request"),
                data,
            )
            if pending is not data.get("pending_request"):
                data = dict(data)
                data["pending_request"] = pending
            self.last_actionable_snapshot = actionable_state_snapshot(data)
            self._remember_pending(pending)
            if "gameplay_seen" in data:
                self._last_gameplay_seen = bool(data.get("gameplay_seen"))
        return data if code == 200 and data else {}

    def pending(self, *, timeout: float = 3.0) -> dict | None:
        """Current pending choice/input request, or None."""
        code, data = self._get("/pending", timeout=max(0.0, timeout))
        if code == 200 and data:
            p = data.get("pending")
            self._remember_pending(p)
            return p
        return None

    def transcript(self, last: int = 20, *, timeout: float = 3.0) -> list[dict]:
        """Recent events from the transcript."""
        code, data = self._get(
            "/transcript", params={"last": str(last)},
            timeout=max(0.0, timeout),
        )
        if code == 200 and data:
            return data.get("transcript", [])
        return []

    def get_transcript(self, last_n: int = 20) -> list[dict]:
        """Alias for transcript() — used by format.py helpers."""
        return self.transcript(last=last_n)

    def screenshot(self, *, timeout: float = 10.0) -> str | None:
        """Request a fresh frame; never substitute an unrelated cached image."""
        deadline = time.time() + max(0.0, timeout)
        if time.time() >= deadline:
            return None
        capture_id = uuid.uuid4().hex
        result = self.command("screenshot", capture_id=capture_id,
                              hide_gui=False, _deadline=deadline)
        if not result.get("success"):
            return None
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            if not remaining:
                break
            code, data = self._get("/screenshot", timeout=min(2.0, remaining))
            if code == 200 and data and data.get("capture_id") == capture_id:
                return data.get("screenshot")
            time.sleep(min(0.05, max(0.0, deadline - time.time())))
        return None

    def screen(self) -> dict | None:
        """Current screen state (buttons, texts) from stateful endpoint."""
        code, data = self._get("/screen", timeout=2.0)
        if code == 200 and data:
            return data.get("screen")
        return None

    def game_state(self, *, timeout: float = 2.0) -> dict | None:
        """Current game state (post-transform interactions, stats, inventory)."""
        code, data = self._get("/game_state", timeout=max(0.0, timeout))
        if code == 200 and data:
            return data.get("game_state")
        return None

    def status(self, *, timeout: float = 3.0) -> dict:
        """Bridge status for this slot."""
        code, data = self._get("/status", timeout=max(0.0, timeout))
        return data if code == 200 and data else {}

    def inventory(self) -> dict:
        """Current inventory."""
        code, data = self._get("/inventory", timeout=3.0)
        return data if code == 200 and data else {}
