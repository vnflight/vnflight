#!/usr/bin/env python3
"""vnflight bridge server.

HTTP server that brokers game state between a Ren'Py game (running the
vnflight.rpy shim) and external clients (CLI, MCP, agents).

The bridge is intentionally "dumb" — it holds state, forwards events,
and accepts actions.  It has no opinion about who or what is playing.

All dependencies are stdlib-only.  Can be run standalone::

    python -m vnflight.bridge [--host HOST] [--port PORT]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import math
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .shim_schema import (
    ACTIONABLE_ITEM_FIELDS,
    ACTIONABLE_REQUEST_FIELDS,
    ACTIONABLE_REQUEST_TARGET_FIELDS,
    SHIM_PROTOCOL_VERSION,
)
from .action_surface import (
    CHOICE_CONTENT_LABEL_FIELDS,
    CHOICE_CONTENT_VOLATILE_FIELDS,
    action_request_signature,
    action_screen_signature,
    actionable_request_content_signature,
    actionable_request_signature,
    actionable_request_surface,
    actionable_screen_signature,
    choice_screen_content_signature,
    normalize_choice_content_label,
    project_bridge_actionable_items,
    project_choice_content_items,
)
from .overlay import align_row_provenance, passive_rows_delta

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class TransactionJournalError(OSError):
    """Transaction recovery storage is temporarily unreadable or unwritable."""


# Event types that prove the script is running again after an anomaly: a
# say statement, a menu, an input prompt, or a lifecycle boundary. Scrapes
# (screen_content, screen_text) are deliberately excluded: the exception
# screen itself is scraped, and must not count as progress.
_ANOMALY_RESOLVING_EVENTS = frozenset({
    "dialogue", "narration", "choice_request", "input_request",
    "game_started", "game_resumed", "game_ended",
})


class GameState:
    """Thread-safe container for the entire game state."""

    def __init__(self, storage_dir: str | None = None) -> None:
        self._lock = threading.Lock()
        self._storage_dir = storage_dir

        # The actual PID of the game process as reported by the mod.
        self.game_pid: int | None = None
        self.shim_protocol_version: int = SHIM_PROTOCOL_VERSION
        # Last successful /slots/assign provenance. Reservation ownership by
        # itself is not registration proof: a dead slot can retain its token.
        self.registration_launch_id: str | None = None
        self.registered_at: float | None = None

        # Full ordered list of game events (capped to prevent unbounded growth).
        self.transcript: list[dict[str, Any]] = []
        self._max_transcript: int = 2000  # Keep last N events.

        # The currently pending request (choice_request / input_request).
        # None when no interaction is pending.
        self.pending_request: dict[str, Any] | None = None

        # An action submitted by an external client, waiting to be consumed
        # by the Ren'Py mod via GET /action.
        self.pending_action: dict[str, Any] | None = None

        # Commands submitted by external clients, waiting to be consumed
        # by the Ren'Py mod via GET /command.  A small bounded FIFO —
        # the old single-slot mailbox silently dropped the first command
        # when a second arrived before the shim's poll, even though the
        # first caller had already been told "submitted".
        self.pending_commands: list[dict[str, Any]] = []
        # Idempotency memory for command submissions carrying a "nonce".
        # A transport-timeout retry (client re-sends the SAME logical
        # command after a lost HTTP response) must NOT enqueue twice — a
        # duplicate act would apply the choice again once the shim polls.
        # Maps nonce -> canonical command signature plus its first ack.
        # Identical retries replay a pending ack or re-enqueue a consumed
        # command for the shim's result cache; a nonce cannot name new args.
        # Bounded LRU; nonce-less commands are unaffected.
        self._recent_command_nonces: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        # A nonce-bearing non-act result may be replayed after the shim loses
        # the original HTTP acknowledgement. Keep the first delivered receipt
        # authoritative so retrying delivery cannot duplicate transcript rows.
        # Act results retain their richer transaction-level late-event audit.
        # This is scoped to one retained transcript generation. A story reset
        # clears both the transcript and this ledger so a lost pre-reset
        # receipt can be replayed into the new authoritative window once.
        self._delivered_command_result_nonces: "OrderedDict[str, None]" = OrderedDict()
        # Retries from the ordered shim outbox reuse their source occurrence.
        # Dedup that transport identity for every event type, including act
        # results whose later semantic replays retain transaction diagnostics.
        self._delivered_source_events: "OrderedDict[tuple[str, int], None]" = OrderedDict()

        # Durable act transactions. Unlike _recent_command_nonces this is not
        # a transport cache: records survive reset and retain scoped output.
        self.slot_id: int | None = None
        self.game_id: str | None = None
        self._next_action_id: int = 1
        self._act_transactions: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._active_action_nonce: str | None = None
        # Post-settle attribution: trailing story output still belongs to the
        # action that caused it, but only for a bounded window (see
        # _post_settle_attribution_nonce_locked).
        self._last_settled_action_nonce: str | None = None
        self._last_settled_at: float = 0.0
        self._post_settle_events: int = 0
        # Nonces whose events (or whole record) were spilled to the journal to
        # bound memory.  A registry miss for one of these is worth a journal
        # rehydration scan; any other unknown nonce is answered from memory
        # alone, so a bogus nonce cannot trigger a full-file read.
        self._offloaded_transaction_nonces: "OrderedDict[str, None]" = OrderedDict()
        self._transaction_log_path: str | None = None
        self._transaction_log_lock = threading.Lock()
        self._transaction_event_backlog: list[tuple[str, str]] = []
        self._transaction_journal_epoch: int = 0
        self._transaction_ack_lock = threading.Lock()
        self._transaction_snapshot_lock = threading.Lock()
        self._transaction_snapshot_backlog: "OrderedDict[str, dict[str, Any]]" = (
            OrderedDict()
        )
        # Serializes act ACCEPTANCE so the durable write can happen with the
        # state lock released while still being a critical section (July's
        # act-stall saga removed flush-under-lock; do not reintroduce it).
        self._act_submit_lock = threading.Lock()
        # Set atomically by SlotManager before removing this state. Requests
        # that resolved the slot just before free() must not mutate an orphan.
        self._closed: bool = False
        # A quiet story is not necessarily finished.  Command polling is the
        # shim heartbeat that separates a live-but-silent game from an
        # abandoned applied transaction — it MULTIPLIES the applied-idle
        # budget (see _ACTION_LIVE_SHIM_IDLE_MULTIPLIER), it does not veto
        # expiry: the shim stamps this from a background thread that keeps
        # polling through a hung main loop.  Zero means "never polled", the
        # most abandoned state of all.
        self._last_shim_command_poll_at: float = 0.0

        # The current game context (main_menu, in_game, game_menu, etc.)
        self.current_context: dict[str, Any] | None = None

        # The latest screenshot bytes (PNG), base64-encoded.
        self.latest_screenshot: str | None = None
        self.latest_screenshot_capture_id: str | None = None

        # Current screen state (stateful, overwritten each scrape). Ordinary
        # screen content is state, not events. Changed snapshots from registered
        # passive overlays are also retained: their story text may disappear
        # before the next decision point, so latest-state-only storage is lossy.
        self.current_screen: dict[str, Any] | None = None
        self._passive_overlay_signature: tuple[Any, ...] | None = None
        # Full passive-overlay snapshots are cumulative UI state. Keep the
        # bridge-side baseline as well as the signature so retained events can
        # carry a durable delta for stateless clients (notably one-shot CLI
        # invocations). Handler-local ledgers remain useful for old bridges and
        # the latest, non-drained screen scrape.
        self._passive_overlay_rows: tuple[str, ...] = ()
        self._passive_overlay_rows_by_screen: dict[str, tuple[str, ...]] = {}
        self._passive_overlay_generations: dict[str, str] = {}
        self._passive_overlay_row_seqs: tuple[int, ...] = ()
        self._passive_overlay_row_seqs_by_screen: dict[str, tuple[int, ...]] = {}
        self._passive_overlay_resume_pending = False
        self._passive_overlay_resume_first_snapshot = False
        self._passive_overlay_resume_rows: tuple[str, ...] = ()
        self._passive_overlay_resume_rows_by_screen: dict[
            str, tuple[str, ...]
        ] = {}
        self._passive_overlay_resume_generations: dict[str, str] = {}
        self._passive_overlay_resume_row_seqs: tuple[int, ...] = ()
        self._passive_overlay_resume_row_seqs_by_screen: dict[
            str, tuple[int, ...]
        ] = {}

        # Current game state (stateful, overwritten each scrape).
        # Carries post-transform interactions, live stats, inventory,
        # and screen buttons.  Clients read this for enriched data
        # instead of the pending_request.
        self.current_game_state: dict[str, Any] | None = None
        # Sticky playthrough-terminal flag, latched from progress_change
        # events whose graph node is a game ending (game_terminal=True).
        # Surfaced in act/wait results so the harness can detect "the game
        # reached an ending" even though progress events aren't relayed to it.
        self.current_game_terminal: bool = False
        # Last game_state progress values (stats / inventory) scraped while a
        # playthrough was genuinely live.  Once the terminal latches, the game
        # has usually returned to the main menu and Ren'Py has reset its store,
        # so every later scrape reports GAME DEFAULTS (integrity back to 100,
        # evidence 0, ...).  Serving those as "final stats" misled playthrough
        # agents into filing story-continuity bugs, so post-terminal reads are
        # frozen at this snapshot until a genuinely live scrape replaces it.
        self._live_progress: dict[str, Any] | None = None
        # Capture hold: Ren'Py fires config.start_callbacks (-> game_started)
        # as part of the return-to-menu RESTART itself, before any context /
        # screen scrape has been reclassified as the menu.  Scrapes arriving in
        # that window already read the reset store, so capture is suspended
        # from game_started until gameplay is re-confirmed (in_game context,
        # story content, or an actionable request).
        self._progress_capture_suspended: bool = False

        # Running / waiting_for_input / ended
        self.status: str = "idle"

        # Reason the game ended (if status == "ended").
        self.end_reason: str | None = None

        # Whether this slot has observed gameplay since the last ended state.
        # Used as a bridge-side fallback for games that return to main menu
        # through a custom/intermediate menu without emitting game_ended.
        self._gameplay_seen: bool = False

        # Dedup latch for terminal_evidence events: one per menu-return
        # episode.  Reset when gameplay resumes (story content / a new
        # pending request / an in_game context) so the NEXT false fire
        # documents itself again.
        self._menu_evidence_emitted: bool = False

        # Sticky anomaly flag — set when an anomaly event is pushed,
        # cleared when a client reads it via /state.
        self.anomaly_flag: dict | None = None

        # Monotonic counter for events so clients can ask "what's new".
        self.event_counter: int = 0
        self.reset_generation: int = 0
        self._load_consumed_at: float | None = None

        # Runtime configuration (can be updated after game start)
        self.auto_advance: bool = True
        self.auto_advance_delay: float = 0.3
        # Whether returning to the main menu after gameplay counts as a
        # playthrough-terminal ending.  Default True (generic Ren'Py VNs
        # end at the menu); games whose gameplay screens are classified
        # as main_menu (Slay the Princess) opt out via vnflight.json
        # ("end_on_menu_return": false) so the heuristic never falsely
        # ends a live run.  quit / process_exit endings are unaffected.
        self.end_on_menu_return: bool = True

        # Log file handle.
        self._log_file: Any | None = None
        self._log_path: str | None = None
        # Off-lock JSONL writer: events are serialized + queued under
        # ``_lock`` (cheap), then written+flushed by ``_flush_log`` after the
        # lock is released, so a slow disk / AV scan can't block command
        # traffic.  ``_log_write_lock`` serializes the drain so lines land in
        # _seq order even under concurrent POST handlers.
        self._log_buffer: list[str] = []
        self._log_write_lock = threading.Lock()
        # One-shot latch so a permanently-broken log file (buffer capped and
        # dropping oldest lines) is only noted once, not every flush.
        self._log_buffer_overflow_noted: bool = False

        # Track all requests by ID (both pending and resolved).
        # Key: request_id, Value: dict with 'request', 'resolution', 'submitted_action'
        self.requests_by_id: dict[str, dict[str, Any]] = {}

        # Inventory versioning for conflict detection (optimistic locking)
        self.inventory_version: int = 0
        self.current_inventory: list[dict[str, Any]] = []

    # -- helpers --

    # Transport envelope of an act command_result — everything else in the
    # event is the shim's resolution metadata and belongs on the record.
    _ACT_RESULT_ENVELOPE_KEYS = frozenset({
        "type", "command", "success", "nonce", "_seq", "timestamp",
        "action_id", "error",
    })

    # -- act transaction lifecycle tuning ------------------------------------
    # Quiet period after the last observed story event before an applied
    # transaction counts as settled.
    _ACTION_SETTLE_GRACE: float = 0.75
    # Post-settle attribution window: trailing story output is still tagged
    # with the settled action for this long / this many events, then becomes
    # ordinary-stream only. Unbounded fallback tagged unrelated narration
    # with a stale action_id and served it from that transaction's drain.
    _ACTION_POST_SETTLE_GRACE: float = 5.0
    _ACTION_POST_SETTLE_MAX_EVENTS: int = 25
    # A boundary inferred from attributed output is weaker than a structural
    # boundary (new request, changed screen, game end): the shim cannot attach
    # durable provenance to story/state events after the click handler returns.
    # Keep admission closed for the full trailing-attribution window so a
    # successor cannot steal delayed output from the action that caused it.
    _ACTION_ATTRIBUTED_OUTCOME_SETTLE_GRACE: float = (
        _ACTION_POST_SETTLE_GRACE
    )
    # A choice can enter a dialogue branch whose individual lines are emitted
    # several seconds apart under fleet contention. An inferred state-change
    # boundary is not strong enough to prove that branch finished. Keep the
    # gate closed longer for choices; an actual successor request, changed
    # screen, or game end still upgrades to the short structural grace.
    _ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE: float = 15.0
    # Idle bound for an applied transaction whose settle boundary was never
    # observed.  The clock runs from the LAST EVENT ATTRIBUTED TO THE
    # TRANSACTION (falling back to applied_at), never from apply time: a story
    # that keeps emitting narration keeps resetting it, so a long playback can
    # never be force-settled — only genuine silence expires here.  The shim's
    # command-poll heartbeat is a MULTIPLIER on this budget, not a veto:
    # silence plus a stale/absent heartbeat expires at the TTL, silence under
    # a live heartbeat expires at TTL * _ACTION_LIVE_SHIM_IDLE_MULTIPLIER.
    # Both failure directions are recorded in
    # design/DESIGN_act_transactional_ack.md: a fixed-from-applied_at TTL cut
    # long stories off mid-playback, and no expiry at all (including a
    # heartbeat-gated one, since the shim's poll thread outlives a hung main
    # loop) wedged the slot forever.
    _ACTION_APPLIED_IDLE_TTL: float = 30.0
    # A live shim heartbeat only BUYS TIME for a quiet screen (a cutscene,
    # animation or user-paced surface can legitimately be silent).  It cannot
    # buy forever: the heartbeat is stamped by the shim's background poll
    # thread, which keeps ticking through a hung Ren'Py main loop, so treating
    # it as a hard conjunct reopens the permanent wedge.  4 -> 120 s.
    _ACTION_LIVE_SHIM_IDLE_MULTIPLIER: float = 4.0
    # The idle release above frees ADMISSION only: the record stays applied and
    # pending because elapsed silence cannot prove a story boundary. A dead
    # slot reaches a terminal state through PID reaping/free, while a live slot
    # remains recoverable until a real boundary or a superseding act.
    # A dispatched act command is leased, not popped, so a lost command_result
    # does not lose the action.  It is not re-served to the shim within this
    # delay (the shim is presumably still executing it) ...
    _ACTION_LEASE_RESERVE_DELAY: float = 5.0
    # ... and the lease expires after this, failing the transaction with
    # reason "shim_no_result" so the channel unblocks instead of wedging.
    _ACTION_LEASE_TTL: float = 60.0
    # Live registry bound.  SPILL, never delete: settled + fully drained
    # transactions compact to tombstones and are evicted LRU; settled records
    # that still hold UNDRAINED output have their events offloaded to the
    # transaction journal first and become offloaded tombstones, which a
    # scoped drain rehydrates on demand.  A pending transaction is never
    # evicted and never offloaded.
    _MAX_ACT_TRANSACTIONS: int = 256
    # Hard cap on ONE transaction's retained events.  Above this the oldest
    # events are offloaded out of memory AFTER they are journalled; the
    # scoped drain reloads that range from the journal when the cursor reaches
    # into it.  Nothing is destroyed — this bounds RAM, not recoverability.
    _MAX_ACT_TRANSACTION_EVENTS: int = 2000
    # Rewrite the transaction journal from live state once it grows past this.
    _MAX_TRANSACTION_JOURNAL_BYTES: int = 8 * 1024 * 1024
    # A known-offloaded nonce whose journal record has gone missing is a
    # PERMANENT 503 for that nonce (never a re-apply), so nothing in the
    # protocol ever surfaces it.  Log it once per process instead of silently
    # failing forever.  Process-wide and FIFO-bounded: a nonce evicted after
    # this many distinct failures may log a second time, which is preferable
    # to an unbounded set on a diagnostic path.
    _MISSING_JOURNAL_RECORD_LOG_LIMIT: int = 256
    _missing_journal_record_logged: "OrderedDict[tuple[str, str, str], None]" = (
        OrderedDict()
    )
    _missing_journal_record_log_lock = threading.Lock()

    # A tombstone keeps identities, terminal state and the drain boundary.
    _TOMBSTONE_KEYS = frozenset({
        "action_nonce", "action_id", "slot_id", "reset_generation",
        "submitted_target", "transaction_state", "resolved_as",
        "resolved_label", "label", "screen", "interaction_type",
        "request_id", "interaction_id", "accepted_at", "applied_at",
        "settled_by", "error", "ended", "revision", "compacted",
        "event_count", "events_truncated", "events_offloaded",
        "events_journalled", "offloaded", "invocation",
    })

    @staticmethod
    def _sanitize_act_invocation(value: Any) -> dict[str, Any] | None:
        """Bound diagnostic MCP provenance without affecting act admission."""
        if not isinstance(value, dict):
            return None
        server_id = value.get("server_instance_id")
        call_id = value.get("call_id")
        if not isinstance(server_id, str) or not isinstance(call_id, str):
            return None
        server_id = server_id[:128]
        call_id = call_id[:128]
        if not server_id or not call_id:
            return None
        attempt_kind = value.get("attempt_kind")
        if attempt_kind not in {
            "initial", "transport_retry", "resync_retry",
        }:
            attempt_kind = "unknown"
        original_target = value.get("original_target")
        if not isinstance(original_target, (str, int, float, bool, type(None))):
            original_target = None
        if isinstance(original_target, str):
            original_target = original_target[:512]
        return {
            "server_instance_id": server_id,
            "call_id": call_id,
            "original_target": original_target,
            "attempt_kind": attempt_kind,
        }

    @staticmethod
    def _transaction_pending(state: str) -> bool:
        return state in {"acceptance_unknown", "accepted", "applied"}

    @staticmethod
    def _action_screen_signature(screen: dict | None) -> str:
        return action_screen_signature(screen)

    @classmethod
    def _choice_screen_content_signature(cls, screen: dict | None) -> str:
        """Choice-only content, excluding utility chrome and screen identity."""
        return choice_screen_content_signature(
            screen,
            item_fields=cls._ACTIONABLE_ITEM_FIELDS,
            volatile_fields=cls._CHOICE_CONTENT_VOLATILE_FIELDS,
            label_fields=cls._CHOICE_CONTENT_LABEL_FIELDS,
            project_items=cls._project_choice_content_items,
        )

    @staticmethod
    def _action_request_signature(request: dict | None) -> str:
        return action_request_signature(request)

    # -- the ACTIONABLE surface -------------------------------------------
    #
    # The two signatures above answer "is this the same moment?" and are what
    # the acceptance window revalidates.  The pair below answers the narrower
    # question a QUEUED act cares about: "can my target still be resolved?"
    # The shim resolves acts against its canonical ``interactions`` list, which
    # includes transformed labels, aliases, disabled state and indices. Choices
    # and screen_buttons remain in the signature because they can arrive before
    # the post-render canonical list. Context-only stats/inventory are omitted.

    _ACTIONABLE_ITEM_FIELDS = ACTIONABLE_ITEM_FIELDS
    _ACTIONABLE_REQUEST_FIELDS = ACTIONABLE_REQUEST_FIELDS
    _ACTIONABLE_REQUEST_TARGET_FIELDS = ACTIONABLE_REQUEST_TARGET_FIELDS
    _CHOICE_CONTENT_VOLATILE_FIELDS = CHOICE_CONTENT_VOLATILE_FIELDS
    _CHOICE_CONTENT_LABEL_FIELDS = CHOICE_CONTENT_LABEL_FIELDS

    @staticmethod
    def _normalize_choice_label(value: object) -> object:
        return normalize_choice_content_label(value)

    @classmethod
    def _project_choice_content_items(cls, items: object) -> list:
        """Project a rendered choice to stable decision semantics.

        Ren'Py can reconstruct the same custom-screen choice through a
        different screen/focus-list pass. Row ids, screen names, indices and
        wait hints then change even though the labels and Return actions do
        not. Those fields remain authoritative for act admission, but they
        cannot prove that the game reached a successor decision after an act.
        """
        return project_choice_content_items(
            items,
            item_fields=cls._ACTIONABLE_ITEM_FIELDS,
            volatile_fields=cls._CHOICE_CONTENT_VOLATILE_FIELDS,
            label_fields=cls._CHOICE_CONTENT_LABEL_FIELDS,
            normalize_label=cls._normalize_choice_label,
        )

    @classmethod
    def _project_actionable_items(cls, items: object) -> list:
        """Keep only fields that can affect act target lookup or execution."""
        return project_bridge_actionable_items(
            items, item_fields=cls._ACTIONABLE_ITEM_FIELDS)

    @classmethod
    def _actionable_screen_signature(cls, screen: dict | None) -> str:
        """The part of a game_state a queued act can still target."""
        return actionable_screen_signature(
            screen,
            item_fields=cls._ACTIONABLE_ITEM_FIELDS,
            project_items=cls._project_actionable_items,
        )

    @classmethod
    def _actionable_request_surface(cls, request: dict | None) -> dict:
        """Project a request to the controls that can resolve an act."""
        return actionable_request_surface(
            request,
            item_fields=cls._ACTIONABLE_ITEM_FIELDS,
            target_fields=cls._ACTIONABLE_REQUEST_TARGET_FIELDS,
            project_items=cls._project_actionable_items,
        )

    @classmethod
    def _actionable_request_content_signature(
        cls, request: dict | None,
    ) -> str:
        """A request's actionable content without its transient identity."""
        return actionable_request_content_signature(
            request,
            item_fields=cls._ACTIONABLE_ITEM_FIELDS,
            target_fields=cls._ACTIONABLE_REQUEST_TARGET_FIELDS,
            project_request=cls._actionable_request_surface,
        )

    @classmethod
    def _actionable_request_signature(cls, request: dict | None) -> str:
        """A request's identity and actionable content, without context data.

        A fresh request id is a new interaction even when its labels happen to
        match: it owns a different shim value_map. A resync can explicitly name
        the request it reissues to preserve the logical identity.
        """
        return actionable_request_signature(
            request,
            item_fields=cls._ACTIONABLE_ITEM_FIELDS,
            target_fields=cls._ACTIONABLE_REQUEST_TARGET_FIELDS,
            project_request=cls._actionable_request_surface,
        )

    def _actionable_surface_signature_locked(self) -> str:
        """Everything a queued act could be targeting, in one comparable value."""
        return "{}\x1f{}".format(
            self._actionable_screen_signature(self.current_game_state),
            self._actionable_request_signature(self.pending_request),
        )

    def _queued_act_surface_signature_locked(self) -> str | None:
        """The same signature, but only while an act is queued to target it.

        ``None`` means "nothing could be cancelled by a change here", and it
        short-circuits the exit comparison.  ``push_event`` is the hot path: a
        screenshot flood must not pay for two ``json.dumps`` of the menu per
        frame to answer a question with no consequence.
        """
        if not any(
            command.get("name") == "act" for command in self.pending_commands
        ):
            return None
        return self._actionable_surface_signature_locked()

    def configure_identity(
        self, slot_id: int, game_id: str, game_pid: int | None = None,
    ) -> None:
        """Bind persistent transaction storage to a game process."""
        self.slot_id = slot_id
        self.game_id = game_id
        self._closed = False
        self._last_shim_command_poll_at = time.time()
        if game_pid is not None:
            self.game_pid = game_pid
        safe_game = "".join(
            ch if ch.isalnum() or ch in "-_" else "_" for ch in game_id
        ) or "game"
        identity = str(game_pid) if game_pid is not None else "legacy"
        logs_dir = self._storage_dir or os.path.join(os.getcwd(), "bridge", "logs")
        os.makedirs(logs_dir, exist_ok=True)
        self._transaction_log_path = os.path.join(
            logs_dir, f"transactions_{safe_game}_{identity}.jsonl")
        self._load_transaction_journal()

    def _load_transaction_journal(self) -> None:
        try:
            self._restore_transaction_journal()
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            # Includes decoding/schema failures. Do not expose partial nonce
            # history as a successful recovery, even when JSON itself parses.
            raise TransactionJournalError(
                f"Invalid transaction journal: {self._transaction_log_path}") from exc

    def _restore_transaction_journal(self) -> None:
        path = self._transaction_log_path
        if not path or not os.path.exists(path):
            return
        records: dict[str, dict[str, Any]] = {}
        compacted_nonces: set[str] = set()
        try:
            with open(path, "r", encoding="utf-8") as stream:
                for line in stream:
                    item = json.loads(line)
                    if item.get("journal_type") == "act_transaction_meta":
                        self.reset_generation = max(
                            self.reset_generation,
                            int(item.get("reset_generation", 0) or 0),
                        )
                        self._next_action_id = max(
                            self._next_action_id,
                            int(item.get("next_action_id", 1) or 1),
                        )
                        continue
                    if item.get("journal_type") == "act_transaction_event":
                        nonce = item.get("action_nonce")
                        event = item.get("event")
                        if nonce in compacted_nonces:
                            # Retired history: never load a tombstoned
                            # transaction's events back into memory.
                            continue
                        if nonce and isinstance(event, dict):
                            records.setdefault(nonce, {
                                "action_nonce": nonce, "events": [],
                            }).setdefault("events", []).append(event)
                        continue
                    record = item.get("record")
                    nonce = (record or {}).get("action_nonce")
                    if item.get("journal_type") == "act_transaction" and nonce:
                        prior_events = records.get(nonce, {}).get("events", [])
                        record["events"] = prior_events
                        prior = records.get(nonce, {})
                        if int(record.get("revision", 0) or 0) >= int(
                            prior.get("revision", 0) or 0
                        ):
                            records[nonce] = record
                        # Compaction is journalled AFTER the events it retires,
                        # so a single forward pass can drop them here.
                        if records[nonce].get("compacted"):
                            compacted_nonces.add(nonce)
                            records[nonce]["events"] = []
        except OSError as exc:
            raise TransactionJournalError(f"Cannot read transaction journal: {path}") from exc
        with self._lock:
            max_seq = 0
            for record in records.values():
                if record.get("compacted"):
                    # Tombstone: its events were retired on purpose.  The
                    # journal is append-only, so older act_transaction_event
                    # lines for this nonce are still on disk — skip them
                    # rather than resurrecting a compacted playthrough.
                    record["events"] = []
                    record["drain_index"] = 0
                    continue
                events = self._deduplicate_journal_events(
                    record.setdefault("events", []))
                # The sequence high-water mark must come from EVERY journalled
                # event, including the ones dropped from memory below.
                max_seq = max(
                    max_seq,
                    max((int(event.get("_seq", 0) or 0) for event in events),
                        default=0),
                )
                # A record may have offloaded its oldest events to bound RAM.
                # They are still on disk (that is the whole point), so the
                # loader sees them again — re-drop exactly that prefix so the
                # logical numbering the drain cursor uses is preserved.
                offloaded = min(
                    int(record.get("events_offloaded", 0) or 0), len(events))
                record["events"] = events[offloaded:]
                record["events_offloaded"] = offloaded
                record["events_journalled"] = len(events)
                if self._offload_transaction_events_locked(record) or offloaded:
                    self._remember_offloaded_nonce_locked(
                        str(record.get("action_nonce") or ""))
            self._act_transactions.update(records)
            self.event_counter = max(self.event_counter, max_seq)
            max_record_id = max(
                (int(record.get("action_id", 0) or 0)
                 for record in records.values()),
                default=0,
            )
            self._next_action_id = max(
                self._next_action_id, max_record_id + 1,
            )
            self.reset_generation = max(
                self.reset_generation,
                max((int(record.get("reset_generation", 0) or 0)
                     for record in records.values()), default=0),
            )
            live = sorted(
                (record for record in records.values()
                 if record.get("transaction_state") in {"accepted", "applied"}
                 and int(record.get("reset_generation", -1)) == self.reset_generation),
                key=lambda record: int(record.get("action_id", 0) or 0),
            )
            if live:
                # Admission release can leave one applied/pending transaction
                # plus a newer accepted-but-not-yet-dispatched act. Rebuild
                # every accepted command, not merely the oldest live record,
                # or a crash in that window silently loses the newer action.
                for current in live:
                    if current.get("transaction_state") == "applied" or current.get(
                        "dispatched"
                    ):
                        self._active_action_nonce = current.get("action_nonce")
                    if (
                        current.get("transaction_state") == "accepted"
                        and current.get("command")
                    ):
                        self.pending_commands.append(dict(current["command"]))
                        # A reconstructed lease belongs to THIS bridge: clear
                        # the re-serve suppression so the shim gets the command
                        # on its next poll, and restart the TTL clock from now
                        # rather than from the dead process's dispatch time.
                        current.pop("reserved_at", None)
                        if current.get("dispatched"):
                            current["dispatched_at"] = time.time()
            self._evict_transactions_locked()
        # Recovery is read-only. Maintenance belongs to the owning live bridge,
        # not to every process discovering a historical archive at startup.

    @staticmethod
    def _deduplicate_journal_events(
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Order and de-duplicate one transaction's journalled events.

        LOAD-BEARING, do not remove: the journal is append-only *and* is
        rewritten in place once it passes ``_MAX_TRANSACTION_JOURNAL_BYTES``.
        A rewrite re-emits every retained event, and any event written between
        the rewrite's in-memory snapshot and the atomic replace is appended
        again afterwards, so the same event can legitimately appear twice
        across a rewrite+append boundary.  The rewrite-epoch check
        (``_transaction_journal_epoch``) narrows that window but cannot close
        it — this ``(action_id, _seq, type)`` dedup is what keeps a reloaded
        transaction from replaying duplicated story text and from mis-numbering
        the drain cursor.
        """
        ordered = sorted(events, key=lambda event: int(event.get("_seq", 0) or 0))
        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[int, int, Any]] = set()
        for event in ordered:
            key = (
                int(event.get("action_id", 0) or 0),
                int(event.get("_seq", 0) or 0),
                event.get("type"),
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(event)
        return deduplicated

    # -- spill-to-journal: bound RAM without destroying recoverable output ---

    @staticmethod
    def _transaction_event_total(record: dict[str, Any]) -> int:
        """Logical event count: offloaded prefix + what is still in memory.

        ``drain_index`` indexes this LOGICAL sequence, not the in-memory list.
        """
        return (
            int(record.get("events_offloaded", 0) or 0)
            + len(record.get("events") or [])
        )

    def _remember_offloaded_nonce_locked(self, nonce: str) -> None:
        """Record that this nonce's recovery data lives (partly) on disk.

        Deliberately UNBOUNDED, and deliberately never pruned on success.
        Forgetting a nonce would make it look unknown: the next submission of
        it would mint a second action_id and apply the act twice, and the next
        journal rewrite would drop the only copy of its output.  A successful
        rehydration does not remove the nonce either — the journal record it
        came from persists, so the nonce must stay claimed.  The cost is
        monotonic but tiny (~100 B per nonce, one per spilled act for the life
        of the slot), which the retention policy accepts.
        """
        if not nonce:
            return
        self._offloaded_transaction_nonces.pop(nonce, None)
        self._offloaded_transaction_nonces[nonce] = None

    def _offload_transaction_events_locked(self, record: dict[str, Any]) -> bool:
        """Drop the oldest JOURNALLED events of an over-cap record from memory.

        Returns True when anything was offloaded.  Only events already handed
        to the journal are eligible (``events_journalled`` counts those), so a
        drop can never precede the write that makes the event recoverable.
        With no journal configured nothing is offloaded: bounding RAM must not
        cost story text that has nowhere else to live.
        """
        if not self._transaction_log_path:
            return False
        events = record.get("events") or []
        overflow = len(events) - self._MAX_ACT_TRANSACTION_EVENTS
        if overflow <= 0:
            return False
        offloaded = int(record.get("events_offloaded", 0) or 0)
        journalled = int(record.get("events_journalled", 0) or 0)
        # Never offload past the journalled watermark.
        overflow = min(overflow, max(0, journalled - offloaded))
        if overflow <= 0:
            return False
        del events[:overflow]
        record["events"] = events
        record["events_offloaded"] = offloaded + overflow
        return True

    def _scan_transaction_journal(
        self, nonce: str | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        """One forward pass over the journal: latest records + ordered events.

        Optionally filtered to a single *nonce* so a recovery read costs only
        that transaction's memory.  Events come back de-duplicated and _seq
        ordered (see ``_deduplicate_journal_events``).
        """
        path = self._transaction_log_path
        records: dict[str, dict[str, Any]] = {}
        events: dict[str, list[dict[str, Any]]] = {}
        if not path:
            raise TransactionJournalError("Transaction journal is not configured.")
        if not self._flush_transaction_events():
            raise TransactionJournalError(
                "Could not flush pending transaction events."
            )
        try:
            with open(path, "r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    kind = item.get("journal_type")
                    if kind == "act_transaction_event":
                        key = item.get("action_nonce")
                        if not key or (nonce is not None and key != nonce):
                            continue
                        event = item.get("event")
                        if isinstance(event, dict):
                            events.setdefault(str(key), []).append(event)
                    elif kind == "act_transaction":
                        record = item.get("record")
                        if not isinstance(record, dict):
                            continue
                        key = record.get("action_nonce")
                        if not key or (nonce is not None and key != nonce):
                            continue
                        prior = records.get(str(key))
                        if prior is None or int(record.get("revision", 0) or 0) >= int(
                            prior.get("revision", 0) or 0
                        ):
                            records[str(key)] = record
        except OSError as exc:
            raise TransactionJournalError(
                "Could not read transaction journal."
            ) from exc
        for key, items in events.items():
            events[key] = self._deduplicate_journal_events(items)
        return records, events

    def _read_journal_transaction_events(
        self, nonce: str,
    ) -> list[dict[str, Any]]:
        """Read one transaction's full journalled event list back from disk."""
        if not nonce:
            return []
        _, events = self._scan_transaction_journal(nonce)
        return events.get(nonce, [])

    def _log_missing_journal_record(self, nonce: str, missing: str) -> None:
        """Report recovery damage once per journal, nonce and failure kind.

        Behaviour is unchanged by this: the caller still raises, the nonce is
        still never re-applied, and the client still sees 503.  Without the
        line the failure is invisible — every later retry of that nonce 503s
        forever with nothing in any log to say why.
        """
        cls = type(self)
        # Journal path first (that is the real identity of a configured slot's
        # storage).  An UNCONFIGURED GameState has neither path nor slot_id, so
        # fall back to a per-instance token: keying two of them as "None" made
        # the first one's failure silence the second's.  id() is only unique
        # among live objects, which is exactly the scope of this diagnostic.
        key = (
            str(
                self._transaction_log_path
                or "slot:{}:{}".format(self.slot_id, id(self))
            ),
            nonce,
            missing,
        )
        with cls._missing_journal_record_log_lock:
            logged = cls._missing_journal_record_logged
            if key in logged:
                return
            logged[key] = None
            while len(logged) > cls._MISSING_JOURNAL_RECORD_LOG_LIMIT:
                logged.popitem(last=False)
        print(
            "[transactions] Offloaded action nonce {!r} is missing its {} in "
            "the transaction journal; recovery for it will keep failing with "
            "503 and the nonce will never be re-applied.".format(
                nonce, missing),
            file=sys.stderr,
        )

    def _rehydrate_transaction_from_journal(
        self, nonce: str,
    ) -> dict[str, Any] | None:
        """Reload a spilled transaction (record + events) from the journal.

        This is what makes the registry bound a SPILL rather than a deletion:
        an offloaded tombstone still answers ``wait(action_nonce=...)``.  Only
        nonces recorded in ``_offloaded_transaction_nonces`` reach this path,
        so an unknown nonce never costs a file scan.
        """
        with self._lock:
            if nonce in self._act_transactions:
                return self._act_transactions[nonce]
            if nonce not in self._offloaded_transaction_nonces:
                return None
        journal_records, journal_events = self._scan_transaction_journal(nonce)
        record = journal_records.get(nonce)
        if record is None:
            # Reaching this scan means the nonce was previously offloaded.
            # Treat a missing record as damaged/incomplete recovery storage,
            # never as permission to reuse the nonce and apply the act twice.
            self._log_missing_journal_record(nonce, "record")
            raise TransactionJournalError(
                "Transaction journal is missing a known offloaded record."
            )
        record = dict(record)
        events = [] if record.get("compacted") else list(
            journal_events.get(nonce, []))
        expected_offloaded = int(record.get("events_offloaded", 0) or 0)
        if not record.get("compacted") and len(events) < expected_offloaded:
            self._log_missing_journal_record(nonce, "event prefix")
            raise TransactionJournalError(
                "Transaction journal is missing an offloaded event prefix."
            )
        with self._lock:
            existing = self._act_transactions.get(nonce)
            if existing is not None:
                return existing
            offloaded = min(
                int(record.get("events_offloaded", 0) or 0), len(events))
            record["events"] = events[offloaded:]
            record["events_offloaded"] = offloaded
            record["events_journalled"] = len(events)
            # Deliberately NOT evicting here: this record was just reloaded on
            # a caller's behalf and is the oldest by action_id, so an eviction
            # pass would spill it straight back out before the caller could
            # acknowledge the drain.  The next acceptance/ack prunes instead.
            self._act_transactions[nonce] = record
            return record

    def _persist_transaction(self, record: dict[str, Any]) -> bool:
        """Synchronously persist a snapshot before acknowledging an act."""
        path = self._transaction_log_path
        if not path:
            return True
        stored_record = dict(record)
        stored_record.pop("events", None)
        line = json.dumps({
            "journal_type": "act_transaction",
            "record": stored_record,
            "timestamp": time.time(),
        }, ensure_ascii=False) + "\n"
        persisted_events: list[tuple[str, str]] = []
        try:
            with self._transaction_log_lock:
                with open(path, "a", encoding="utf-8") as stream:
                    persisted_events = list(self._transaction_event_backlog)
                    for _, pending_line in persisted_events:
                        stream.write(pending_line)
                    stream.write(line)
                    stream.flush()
                    self._transaction_event_backlog = []
                    self._transaction_journal_epoch += 1
            self._mark_transaction_events_journalled(persisted_events)
            return True
        except OSError:
            # In-memory idempotency remains available if diagnostics storage
            # is temporarily unavailable, matching the playthrough log model.
            return False

    @staticmethod
    def _touch_transaction_locked(record: dict[str, Any]) -> None:
        record["revision"] = int(record.get("revision", 0) or 0) + 1

    def _mark_transaction_events_journalled(
        self, persisted: list[tuple[str, str]],
    ) -> None:
        """Advance spill watermarks only for writes whose flush succeeded."""
        if not persisted:
            return
        counts: dict[str, int] = {}
        for nonce, _ in persisted:
            counts[nonce] = counts.get(nonce, 0) + 1
        with self._lock:
            for nonce, count in counts.items():
                record = self._act_transactions.get(nonce)
                if record is None or record.get("compacted"):
                    continue
                record["events_journalled"] = int(
                    record.get("events_journalled", 0) or 0
                ) + count
                if self._offload_transaction_events_locked(record):
                    self._remember_offloaded_nonce_locked(nonce)

    def _persist_transaction_snapshot(self, record: dict[str, Any]) -> bool:
        """Persist state in order, retaining the latest failed snapshot."""
        nonce = str(record.get("action_nonce") or "")
        if not nonce:
            return self._persist_transaction(record)
        with self._transaction_snapshot_lock:
            self._transaction_snapshot_backlog[nonce] = dict(record)
            for pending_nonce, snapshot in list(
                self._transaction_snapshot_backlog.items()
            ):
                if not self._persist_transaction(snapshot):
                    return False
                self._transaction_snapshot_backlog.pop(pending_nonce, None)
        return True

    def _flush_transaction_snapshots(self) -> bool:
        """Retry state snapshots that previously lost a journal write."""
        with self._transaction_snapshot_lock:
            for nonce, snapshot in list(self._transaction_snapshot_backlog.items()):
                if not self._persist_transaction(snapshot):
                    return False
                self._transaction_snapshot_backlog.pop(nonce, None)
        return True

    def _seal_and_flush_transactions(self, *, terminalize: bool = False) -> bool:
        """Stop mutation and durably flush complete state before teardown."""
        with self._lock:
            if self._closed:
                return False
            self._closed = True
        # A push that mutated state before the seal may still be doing its
        # off-lock journal work. Waiting for the snapshot lock joins that
        # pipeline; persisting every current record then covers both a writer
        # between its event and snapshot writes and any prior backlog.
        with self._transaction_snapshot_lock:
            with self._lock:
                snapshots = [
                    dict(record) for record in self._act_transactions.values()
                ]
            snapshots.sort(key=lambda snapshot: self._transaction_pending(
                str(snapshot.get("transaction_state", "accepted"))
            ))
            if terminalize:
                for snapshot in snapshots:
                    state = snapshot.get("transaction_state")
                    if state == "applied":
                        snapshot["transaction_state"] = "settled"
                        snapshot["settled_by"] = "slot_free"
                    elif state == "accepted":
                        snapshot["transaction_state"] = "failed"
                        snapshot["error"] = (
                            "Slot was freed before the shim applied the action."
                        )
                    else:
                        continue
                    self._touch_transaction_locked(snapshot)
            # Complete snapshots supersede the retry backlog. Persist the one
            # live transaction last, so a failure cannot durably terminalize
            # it and then reopen the still-live in-memory command channel.
            self._transaction_snapshot_backlog.clear()
            for snapshot in snapshots:
                nonce = str(snapshot.get("action_nonce") or "")
                if nonce:
                    self._transaction_snapshot_backlog[nonce] = snapshot
            for nonce, snapshot in list(self._transaction_snapshot_backlog.items()):
                if not self._persist_transaction(snapshot):
                    with self._lock:
                        self._closed = False
                    return False
                self._transaction_snapshot_backlog.pop(nonce, None)
        if not self._flush_transaction_events():
            with self._lock:
                self._closed = False
            return False
        if terminalize:
            with self._lock:
                for snapshot in snapshots:
                    nonce = str(snapshot.get("action_nonce") or "")
                    if nonce in self._act_transactions:
                        self._act_transactions[nonce] = snapshot
                self._active_action_nonce = None
                self._mark_settled_locked(None)
                self.pending_commands[:] = [
                    command for command in self.pending_commands
                    if command.get("name") != "act"
                ]
        return True

    def _persist_transaction_event(self, nonce: str, event: dict[str, Any]) -> bool:
        path = self._transaction_log_path
        if not path:
            return True
        line = json.dumps({
            "journal_type": "act_transaction_event",
            "action_nonce": nonce,
            "event": event,
            "timestamp": time.time(),
        }, ensure_ascii=False) + "\n"
        # Take the batch out of the backlog BEFORE writing and put exactly it
        # back on failure.  Appending this line to a backlog the failed write
        # may or may not have cleared is what let one batch be written (and
        # later counted) twice; the loader's (action_id, _seq, type) dedup
        # bounds the damage on disk either way, but the in-memory watermark it
        # feeds must count each event once.
        with self._transaction_log_lock:
            persisted_events = self._transaction_event_backlog + [(nonce, line)]
            self._transaction_event_backlog = []
            try:
                with open(path, "a", encoding="utf-8") as stream:
                    for _, pending_line in persisted_events:
                        stream.write(pending_line)
                    stream.flush()
                self._transaction_journal_epoch += 1
            except OSError:
                # The lock was held throughout, so nothing was appended to the
                # emptied backlog meanwhile: restoring the batch is exact.
                self._transaction_event_backlog = persisted_events
                return False
        self._mark_transaction_events_journalled(persisted_events)
        return True

    def _persist_transaction_meta(
        self, *, reset_generation: int | None = None,
        next_action_id: int | None = None,
    ) -> bool:
        path = self._transaction_log_path
        if not path:
            return True
        line = json.dumps({
            "journal_type": "act_transaction_meta",
            "reset_generation": (
                self.reset_generation if reset_generation is None
                else reset_generation
            ),
            "next_action_id": (
                self._next_action_id if next_action_id is None else next_action_id
            ),
            "timestamp": time.time(),
        }) + "\n"
        persisted_events: list[tuple[str, str]] = []
        try:
            with self._transaction_log_lock:
                with open(path, "a", encoding="utf-8") as stream:
                    persisted_events = list(self._transaction_event_backlog)
                    for _, pending_line in persisted_events:
                        stream.write(pending_line)
                    stream.write(line)
                    stream.flush()
                    self._transaction_event_backlog = []
                    self._transaction_journal_epoch += 1
            self._mark_transaction_events_journalled(persisted_events)
            return True
        except OSError:
            return False

    def _maybe_rewrite_transaction_journal(self) -> bool:
        """Rewrite the append-only journal from live state when it gets large.

        Retention policy: the journal is an INDEX over the playthrough JSONL,
        not a second archive.  Once it grows past
        ``_MAX_TRANSACTION_JOURNAL_BYTES`` it is rewritten to the current
        registry — one record line per transaction, event lines only for
        transactions whose scoped output has not been drained yet.  Tombstones
        and already-drained history do not survive the rewrite as events.

        SPILL-AWARE: the registry is a bounded cache over this file, so the
        rewrite must also carry forward (a) the offloaded event PREFIX of a
        record whose oldest events live only here, and (b) whole records that
        were evicted as offloaded tombstones.  Dropping either would turn the
        memory bound into data loss the moment the journal happened to grow.
        """
        path = self._transaction_log_path
        if not path:
            return False
        try:
            if os.path.getsize(path) <= self._MAX_TRANSACTION_JOURNAL_BYTES:
                return False
        except OSError:
            return False
        # Scan BEFORE sampling the epoch: the scan flushes any backlogged
        # event lines, which would otherwise bump the epoch under us and abort
        # every rewrite.
        try:
            journal_records, journal_events = self._scan_transaction_journal()
        except TransactionJournalError:
            return False
        with self._transaction_log_lock:
            snapshot_epoch = self._transaction_journal_epoch
        with self._lock:
            snapshot = [dict(record) for record in self._act_transactions.values()]
            spilled = [
                nonce for nonce in self._offloaded_transaction_nonces
                if nonce not in self._act_transactions
                and nonce in journal_records
            ]
            meta = {
                "journal_type": "act_transaction_meta",
                "reset_generation": self.reset_generation,
                "next_action_id": self._next_action_id,
                "timestamp": time.time(),
            }
        for nonce in spilled:
            retained = dict(journal_records[nonce])
            retained.pop("events", None)
            snapshot.append(retained)
        lines = [json.dumps(meta) + "\n"]
        for record in snapshot:
            nonce = str(record.get("action_nonce") or "")
            if record.get("compacted"):
                events: list[dict[str, Any]] = []
            else:
                # Logical event list = the prefix that lives only in the
                # journal + whatever is still resident.  A record that was
                # evicted entirely has no resident tail at all.
                tail = record.pop("events", None)
                offloaded = int(record.get("events_offloaded", 0) or 0)
                stored = journal_events.get(nonce, [])
                if tail is None:
                    events = stored
                else:
                    events = stored[:offloaded] + list(tail)
            record.pop("events", None)
            lines.append(json.dumps({
                "journal_type": "act_transaction",
                "record": record,
                "timestamp": time.time(),
            }, ensure_ascii=False) + "\n")
            # Events are rewritten whole (not from drain_index): the record's
            # cursor indexes into this list, so trimming the prefix here would
            # make a reload skip that many real undrained events.  The size
            # win comes from dropping tombstoned history and the per-revision
            # record snapshots the append-only log accumulated.
            for event in events:
                lines.append(json.dumps({
                    "journal_type": "act_transaction_event",
                    "action_nonce": record.get("action_nonce"),
                    "event": event,
                    "timestamp": time.time(),
                }, ensure_ascii=False) + "\n")
        temp_path = None
        try:
            with self._transaction_log_lock:
                if self._transaction_journal_epoch != snapshot_epoch:
                    return False
                fd, temp_path = tempfile.mkstemp(prefix=".journal-", dir=os.path.dirname(path))
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    for line in lines:
                        stream.write(line)
                    persisted_events = list(self._transaction_event_backlog)
                    for _, pending_line in persisted_events:
                        stream.write(pending_line)
                    stream.flush()
                os.replace(temp_path, path)
                self._transaction_event_backlog = []
                self._transaction_journal_epoch += 1
            self._mark_transaction_events_journalled(persisted_events)
            return True
        except OSError:
            try:
                if temp_path is not None:
                    os.remove(temp_path)
            except OSError:
                pass
            return False

    def _flush_transaction_events(self) -> bool:
        """Retry buffered transaction event writes without changing state."""
        path = self._transaction_log_path
        if not path:
            return True
        persisted_events: list[tuple[str, str]] = []
        try:
            with self._transaction_log_lock:
                if not self._transaction_event_backlog:
                    return True
                with open(path, "a", encoding="utf-8") as stream:
                    persisted_events = list(self._transaction_event_backlog)
                    for _, pending_line in persisted_events:
                        stream.write(pending_line)
                    stream.flush()
                    self._transaction_event_backlog = []
                    self._transaction_journal_epoch += 1
            self._mark_transaction_events_journalled(persisted_events)
            return True
        except OSError:
            return False

    def _transaction_view_locked(
        self, record: dict[str, Any], *, deduplicated: bool = False,
    ) -> dict[str, Any]:
        state = str(record.get("transaction_state", "accepted"))
        view = {
            key: value for key, value in record.items()
            if key not in {
                "events", "drain_index", "settle_observed", "command",
                "invocation",
                "initial_request_signature",
                "initial_request_content_signature", "initial_screen_signature",
                "initial_screen_choice_content_signature",
                "settle_observed_at", "settle_observed_by",
                "settle_observed_first_at", "revision",
                "dispatched", "dispatched_at", "reserved_at",
                "last_event_at", "events_journalled",
                # Internal admission bookkeeping.  These names are NOT the
                # contract: clients (and handlers.py's resolution-copy loop,
                # which promotes every non-bookkeeping key to the act result)
                # read the documented pair below instead.
                "gate_released", "gate_released_by", "gate_released_at",
            }
        }
        view["pending"] = self._transaction_pending(state)
        view["ok"] = state not in {"failed", "rejected"}
        view["success"] = view["ok"]
        if record.get("gate_released"):
            # Documented contract: "this transaction is still applied and
            # pending, AND the bridge will admit another act for this slot."
            # Present only while admission is open — absent means closed, so a
            # caller never has to infer admission from timestamps or from the
            # transaction state alone.
            view["admission_open"] = True
            view["released_by"] = record.get("gate_released_by")
        if deduplicated:
            view["deduplicated"] = True
        return view

    # Story-ish events that a scoped drain must deliver.  Mirrors handle_wait's
    # own base_types: screen_text is Roadwarden/overlay story text and used to
    # vanish from action-scoped waits entirely.
    _ACTION_EVENT_TYPES = frozenset({
        "narration", "dialogue", "say", "scene", "show", "hide",
        "screen_text", "screen_content", "choice_resolved", "input_resolved",
        "command_result", "observation_started", "observation_progress",
        "choice_request",
        "input_request", "auto_skipped", "progress_change",
        "stats_update", "inventory_update", "game_ended",
        # A Start/load screen action can cross a lifecycle boundary before its
        # trailing story settles. These inherit only an ACTIVE action; they
        # are intentionally absent from the post-settle trailing set below,
        # so native load/rollback/start callbacks remain unowned.
        "game_started", "game_resumed",
    })
    # The subset whose arrival with no active action may still be attributed to
    # the JUST-settled action (trailing output), and which extends the
    # settle-quiet window.
    _ACTION_TRAILING_EVENT_TYPES = frozenset({
        "narration", "dialogue", "say", "scene", "show", "hide",
        "screen_text", "screen_content", "auto_skipped", "progress_change", "stats_update",
        "inventory_update",
    })
    # Attributed events that constitute an OBSERVED OUTCOME of an applied act:
    # state the game produced BECAUSE of it.  Two properties matter.
    #
    # * ``command_result`` is deliberately excluded.  It is the shim saying "I
    #   ran your click", not the game saying "and here is what happened" — the
    #   July act-stall saga's phantom ``ok: True`` is exactly the failure an
    #   ack-shaped boundary resurrects.  An act whose only attributed event is
    #   a command_result has produced no evidence of an effect and keeps its
    #   two-tier idle budget, unchanged.
    # * The test is BROAD on purpose, and is NOT the actionable-surface
    #   projection used for cancellation.  Those answer different questions:
    #   cancellation asks "can my queued act still resolve against this
    #   surface?" (so stats and context are rightly excluded), while settle
    #   asks "did anything at all happen because of my act?" — for which a
    #   stats delta under an identically re-rendered menu is a complete answer.
    _ACTION_OUTCOME_EVENT_TYPES = _ACTION_EVENT_TYPES - {
        "command_result", "observation_started", "observation_progress",
    }
    # Core commands that explicitly start a new game-side causal chain.
    # Extension commands are observational unless their result opts in with
    # ``causal_boundary: true``; command names alone cannot tell whether a mod
    # handler merely reports diagnostics or advances story state.
    _SUPERSEDING_COMMANDS = frozenset({
        "advance", "back", "backward", "forward", "load", "next", "quit",
        "replay", "rewind", "rollback", "start", "step", "story_back",
        "story_forward",
    })

    def _mark_settled_locked(self, nonce: str | None) -> None:
        """Open (or close) the bounded post-settle attribution window."""
        self._last_settled_action_nonce = nonce
        self._last_settled_at = time.time() if nonce else 0.0
        self._post_settle_events = 0

    def _post_settle_attribution_nonce_locked(self) -> str | None:
        """The just-settled action, while its trailing output still belongs to it.

        Bounded in BOTH time and event count.  Without this, story events with
        no active action fell back to the last settled nonce forever: unrelated
        narration was tagged with a stale action_id and served by that
        transaction's scoped drain.
        """
        nonce = self._last_settled_action_nonce
        if nonce is None:
            return None
        if (
            self._post_settle_events >= self._ACTION_POST_SETTLE_MAX_EVENTS
            or time.time() - self._last_settled_at > self._ACTION_POST_SETTLE_GRACE
        ):
            self._mark_settled_locked(None)
            return None
        self._post_settle_events += 1
        return nonce

    def _fail_stale_queued_acts_locked(self) -> list[dict[str, Any]]:
        """Cancel every accepted act whose target changed before dispatch.

        The accepted-to-dispatched race exists for an ordinary act as well as
        one admitted over an idle predecessor. Once dispatched, the shim owns
        the act and its own target validation is authoritative.
        """
        stale_nonces = {
            str(command.get("nonce"))
            for command in self.pending_commands
            if command.get("name") == "act"
            and command.get("nonce")
            and (
                successor := self._act_transactions.get(
                    str(command.get("nonce"))
                )
            ) is not None
            and successor.get("transaction_state") == "accepted"
            and not successor.get("dispatched")
        }
        if not stale_nonces:
            return []
        self.pending_commands[:] = [
            command for command in self.pending_commands
            if not (
                command.get("name") == "act"
                and str(command.get("nonce")) in stale_nonces
            )
        ]
        canceled_snapshots: list[dict[str, Any]] = []
        for action_nonce in stale_nonces:
            action = self._act_transactions[action_nonce]
            action["transaction_state"] = "failed"
            action["reason"] = "admission_revoked_before_dispatch"
            action["error"] = (
                "This action was cancelled before dispatch because the "
                "interaction surface it targeted changed. Retrying with the "
                "same action_nonce returns this cancelled transaction; a NEW "
                "act may be submitted after re-reading the current state. If "
                "another action remains in flight, wait for its observed story "
                "boundary or until admission reopens after its idle budget."
            )
            self._touch_transaction_locked(action)
            canceled_snapshots.append(dict(action))
        return canceled_snapshots

    def _revoke_released_admission_locked(
        self, record: dict[str, Any],
    ) -> bool:
        """Close a provisional idle gate.  Deliberately cancels nothing.

        Round 11 cancelled queued successors from here, so any attributed
        event did it.  Resumed output IS proof this transaction is alive —
        closing admission again is right — but it is no proof at all that a
        successor's target went away, and narration under a stable menu killed
        still-valid acts.  ``_cancel_stale_queued_acts_locked`` owns that
        decision now, on the actionable surface alone.
        """
        if not record.get("gate_released"):
            return False
        record.pop("gate_released", None)
        record.pop("gate_released_by", None)
        record.pop("gate_released_at", None)
        record["last_event_at"] = time.time()
        self._touch_transaction_locked(record)
        return True

    def _cancel_stale_queued_acts_locked(self) -> list[dict[str, Any]]:
        """The actionable surface changed: cancel what can no longer resolve.

        Called from ``push_event`` / ``set_pending_request`` once the whole
        state mutation is done and the actionable-surface signature is known
        to differ. Two disciplines retained through the review rounds:

        * **Content, not events.** The trigger is a canonical-interaction,
          choices, screen-buttons, or request-identity/content delta, never
          merely "an event happened". Context-only state is excluded.
        * **Queue plus registry, not ``_active_action_nonce``.** Every accepted
          undispatched act in the queue is protected, including an ordinary
          act with no idle predecessor. Released priors are swept separately
          so their provisional admission is revoked with the same mutation.

        Returns every snapshot the caller must persist — cancelled acts and
        the priors whose admission was revoked.
        """
        snapshots = self._fail_stale_queued_acts_locked()
        for record in list(self._act_transactions.values()):
            if (
                record.get("transaction_state") == "applied"
                and self._revoke_released_admission_locked(record)
            ):
                snapshots.append(dict(record))
        return snapshots

    def _record_transaction_event_locked(
        self, event: dict[str, Any],
    ) -> tuple[str | None, bool]:
        event_type = event.get("type")
        if event_type not in self._ACTION_EVENT_TYPES:
            return None, False
        if event_type == "screen_content" and not event.get(
            "passive_overlay_snapshot"
        ):
            return None, False
        if event_type == "command_result":
            # Only the shim result for the transactional act belongs to this
            # transaction. Results from later commands carry their own nonce
            # and must not refresh the active act's attribution window.
            if event.get("command") != "act":
                return None, False
            nonce = event.get("nonce")
        else:
            nonce = self._active_action_nonce
            if nonce is None and event_type in self._ACTION_TRAILING_EVENT_TYPES:
                nonce = self._post_settle_attribution_nonce_locked()
        record = self._act_transactions.get(nonce or "")
        if not record or record.get("transaction_state") in {"failed", "rejected"}:
            return None, False
        if (
            event_type == "command_result"
            and event.get("command") == "act"
            and not record.get("dispatched")
        ):
            event["ignored_reason"] = "act_not_dispatched"
            return None, False
        if (
            event_type == "command_result"
            and event.get("command") == "act"
            and record.get("transaction_state") != "accepted"
        ):
            event["ignored_reason"] = "act_result_already_recorded"
            return None, False
        if record.get("compacted"):
            return None, False
        if (
            record.get("transaction_state") == "settled"
            and nonce != self._last_settled_action_nonce
        ):
            return None, False
        event["action_id"] = record["action_id"]
        events = record.setdefault("events", [])
        events.append(dict(event))
        # The idle release was provisional and this event disproves it, so
        # admission closes again.  It does NOT cancel a queued successor:
        # story resuming says nothing about whether the successor's TARGET
        # survived.  If the actionable surface really moved, the caller's own
        # surface-delta check cancels it a few lines later.
        gate_revoked = self._revoke_released_admission_locked(record)
        # The applied-idle TTL's clock, shared by BOTH expiry tiers.  Any event
        # attributed to this transaction is proof of life and resets it, so a
        # long story cannot be force-settled while it is still playing,
        # whatever the shim heartbeat says (see _ACTION_APPLIED_IDLE_TTL).
        record["last_event_at"] = time.time()
        if event_type in self._ACTION_OUTCOME_EVENT_TYPES:
            if (
                not record.get("settle_observed")
                and record.get("transaction_state") == "applied"
            ):
                # THE OUTCOME BOUNDARY.  Until this existed, the only boundaries
                # were a NEW pending request id and a DIFFERING screen signature
                # — neither of which a menu toggle produces, because it
                # re-renders the identical surface under the same request.  The
                # live overnight Roadwarden session wedged on exactly that: an
                # act that reported "Food: hungry -> full" via an attributed
                # stats_update sat applied-and-unreleased for the full 120 s
                # live-shim tier, rejecting every follow-up act with
                # ``action_in_flight`` (34 of them in 2,073 acts).  Requiring
                # ``applied`` keeps this strictly POST-outcome: output that
                # arrives before the shim has acknowledged running the command
                # cannot be that command's effect.
                record["settle_observed"] = True
                record["settle_observed_by"] = "attributed_state_change"
                record.setdefault("settle_observed_first_at", time.time())
            if record.get("settle_observed"):
                # The settle GRACE clock, not the settle itself: every further
                # attributed state event pushes the boundary out again, so a
                # story still landing can never be cut short by the act that
                # started it.  This is the round-6 discipline, now applied to
                # every state-bearing type rather than a narration subset —
                # a stats/inventory burst is proof of life just as much as a
                # spoken line.
                record["settle_observed_at"] = time.time()
        return str(nonce), gate_revoked

    def _supersede_active_transaction_for_command_locked(
        self, event: dict[str, Any],
    ) -> dict[str, Any] | None:
        """End prior-act attribution before another command's effects land.

        The shim reports a queued Back/rewind/load/custom command before the
        resulting show/hide/stats events. Merely leaving that command_result
        unscoped is insufficient: while ``_active_action_nonce`` remains set,
        those later effects are still assigned to the older act and can close
        its admission gate indefinitely. A successful state-changing command
        establishes a new causal boundary. Failed and observational commands
        do not, because no new game-side effects are expected from them.
        """
        if (
            event.get("type") != "command_result"
            or event.get("command") in (None, "act")
            or event.get("success") is not True
            or (
                event.get("command") not in self._SUPERSEDING_COMMANDS
                and event.get("causal_boundary") is not True
            )
        ):
            return None
        record = self._act_transactions.get(self._active_action_nonce or "")
        if record is None or record.get("transaction_state") != "applied":
            # The opportunistic quiet-finalizer runs before this method and
            # may already have moved the act into its trailing attribution
            # window. The new command is still an explicit causal break.
            self._mark_settled_locked(None)
            return None
        record["settled_by"] = "superseded_by_command"
        record["superseding_command"] = str(event.get("command"))
        self._settle_transaction_locked(record)
        # This is an explicit causal break, not a normal story boundary. The
        # bounded trailing window opened by _settle_transaction_locked would
        # otherwise attribute the new command's first show/hide event back to
        # the old act and recreate the bug one branch later.
        self._mark_settled_locked(None)
        return dict(record)

    def _observe_transaction_settle_locked(
        self, *, pending: dict | None = None, ended: bool = False,
        screen: dict | None = None,
    ) -> dict[str, Any] | None:
        nonce = self._active_action_nonce
        record = self._act_transactions.get(nonce or "")
        if not record:
            return None
        pending_id = (pending or {}).get("id")
        pending_boundary = bool(
            pending_id
            and pending_id != record.get("initial_request_id")
        )
        screen_boundary = bool(
            screen is not None
            and self._action_screen_signature(screen)
            != record.get("initial_screen_signature")
        )
        # Resolving a Ren'Py choice necessarily removes its old menu before
        # the following dialogue and successor menu render. That transient
        # screen difference is not an end-of-act boundary: treating it as one
        # applies the short structural grace and can settle/compact the
        # transaction before the successor choice_request arrives. Screen
        # controls still use screen changes as their authoritative boundary.
        interaction_type = record.get("interaction_type")
        choice_resolution = (
            interaction_type == "choice"
            if interaction_type
            else record.get("resolved_as") == "choice"
        )
        custom_screen_choice = bool(
            choice_resolution
            and record.get("resolved_as") == "button"
        )
        if choice_resolution and pending_boundary:
            # Ren'Py may register the consumed menu again while executing the
            # selected branch. Its request id is new, but its controls still
            # describe the decision this act already answered. Treating that
            # transient registration as the successor boundary returns the
            # old menu beside new story text and makes the caller's next
            # numeric act stale. Identity still matters for admission; here,
            # at settlement, actionable content tells us whether the game has
            # actually reached a new decision.
            pending_content = self._actionable_request_content_signature(
                pending
            )
            if (
                pending_content
                and pending_content
                == record.get("initial_request_content_signature")
            ):
                pending_boundary = False
        if choice_resolution and not (pending_boundary or ended):
            # A custom call-screen Return is classified as a choice by the
            # interaction, but resolves as a button and has no Ren'Py pending
            # request. Its distinct actionable successor screen is therefore
            # the structural boundary. Keep ignoring the transient blank
            # frame that ordinary and custom choices both pass through.
            initial_choice_content = record.get(
                "initial_screen_choice_content_signature")
            choice_content = self._choice_screen_content_signature(screen)
            if not (
                custom_screen_choice
                and screen_boundary
                and initial_choice_content
                and choice_content
                and choice_content
                != initial_choice_content
            ):
                screen_boundary = False
        if not (pending_boundary or screen_boundary or ended):
            return None
        if (
            not record.get("settle_observed")
            or record.get("settle_observed_by") == "attributed_state_change"
        ):
            # Structural evidence is authoritative. A choice/input request is
            # first recorded as an attributed event, so it may have installed
            # the weaker inferred marker a few lines earlier; replace that
            # marker here so new requests/screens/endings retain the short
            # structural grace.
            record["settle_observed"] = True
            record["settle_observed_at"] = time.time()
            # The admission hold's anchor is the FIRST outcome this act was
            # seen to produce, whichever kind of evidence supplied it.  An
            # upgrade from the inferred marker to a structural one shortens
            # the grace; it does not restart the clock.
            record.setdefault(
                "settle_observed_first_at", record["settle_observed_at"])
            record.pop("settle_observed_by", None)
        if pending is not None:
            record["settled_pending"] = dict(pending)
        if screen is not None:
            record["settled_screen"] = dict(screen)
        if ended:
            record["ended"] = True
        self._touch_transaction_locked(record)
        return record

    def _transaction_settle_grace(self, record: dict[str, Any]) -> float:
        """How much quiet this record's observed boundary needs to settle."""
        if record.get("settle_observed_by") != "attributed_state_change":
            return self._ACTION_SETTLE_GRACE
        interaction_type = record.get("interaction_type")
        choice_resolution = (
            interaction_type == "choice"
            if interaction_type
            else record.get("resolved_as") == "choice"
        )
        if choice_resolution:
            return self._ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE
        return self._ACTION_ATTRIBUTED_OUTCOME_SETTLE_GRACE

    def _admission_hold_expiry_locked(
        self, record: dict[str, Any],
    ) -> float | None:
        """When an applied record stops holding the one-act-in-flight gate.

        ``None`` means "indefinitely, for now": the act's own outcome has
        never been observed, so anything that arrives next may BE that
        outcome, and a successor dispatched here would steal it.  Only the
        idle tiers may step over that record.

        Once an outcome HAS been observed, the hold is the trailing-attribution
        window for that kind of evidence (the same grace the settle path uses)
        measured from the FIRST observation — never refreshed by the rows that
        follow it.  That distinction is the whole fix: ``settle_observed_at``
        is deliberately pushed forward by every attributed row so a burst can
        never be settled mid-playback, and reusing it for admission turned
        "protect five seconds of delayed output" into "hold admission for the
        whole story".  Fleet R64 measured that as an `action_in_flight`
        rejection issued to an act whose blocker had applied ~45 s earlier and
        was merely still narrating; it is the long-story form of the Round 16
        Roadwarden wedge (34 rejections in 2,073 acts under an identically
        re-rendered menu).

        After the grace has run from the first outcome, output that is STILL
        arriving is not delayed output — it is continuous output, and the
        bridge already has a rule for that: it belongs to this transaction
        until the successor is DISPATCHED (``consume_command`` settles the
        prior as ``superseded_by_next_act`` at that instant), which is strictly
        later than admission.  Nothing is dropped either way: the record keeps
        its events, its scoped drain stays replayable by nonce, and the same
        rows remain on the ordinary lane.
        """
        if not record.get("settle_observed"):
            return None
        observed_first_at = record.get("settle_observed_first_at")
        if not isinstance(observed_first_at, (int, float)):
            observed_first_at = record.get("settle_observed_at")
        if not isinstance(observed_first_at, (int, float)):
            return None
        return float(observed_first_at) + self._transaction_settle_grace(record)

    def _transaction_holds_admission_locked(
        self, record: dict[str, Any],
    ) -> bool:
        """Does *record* still occupy the one-act-in-flight gate?

        Three tiers, narrowest first:

        * ``accepted`` — the click has NOT run.  A second act here is a
          genuine double-act: two clicks queued against one rendered menu,
          the first of which consumes it.  Always blocks.
        * ``applied`` with no observed outcome — the click ran but produced
          nothing yet.  Blocks until an idle tier releases it.
        * ``applied`` with an observed outcome — blocks only for the
          trailing-attribution grace after the FIRST observation.  See
          ``_admission_hold_expiry_locked``.
        """
        state = record.get("transaction_state")
        if state == "accepted":
            return True
        if state != "applied":
            return False
        if record.get("gate_released"):
            return False
        hold_until = self._admission_hold_expiry_locked(record)
        if hold_until is None:
            return True
        return time.time() < hold_until

    def _finalize_transaction_if_quiet_locked(
        self, record: dict[str, Any],
    ) -> bool:
        """Settle at a boundary, or release admission after idle uncertainty.

        Two independent releases, and both are needed:

        * **Observed boundary** — a new request, a differing screen, a game
          end, or (since the toggle fix) any attributed state-bearing event
          after apply marked ``settle_observed``. Structural boundaries settle
          after ``_ACTION_SETTLE_GRACE`` of quiet. An inferred attributed-
          output boundary waits for the full post-settle attribution window,
          preventing a successor from stealing delayed output. The boundary
          only SUPPLIES evidence where an identical re-render produced none.
        * **Idle gate release, two tiers** — no boundary was ever observed and nothing
          has been attributed to the transaction for a while.  How long "a
          while" is depends on the shim's command-poll heartbeat, which
          MULTIPLIES the budget rather than vetoing expiry:

          - stale or absent heartbeat (a dead or never-started game): release
            after ``_ACTION_APPLIED_IDLE_TTL``, ``gate_released_by:
            "abandoned_shim"``;
          - live heartbeat (something is still polling, so a quiet cutscene or
            user-paced screen is plausible): expire after
            ``_ACTION_APPLIED_IDLE_TTL * _ACTION_LIVE_SHIM_IDLE_MULTIPLIER``,
            ``gate_released_by: "idle_ttl_live_shim"``.

          A live heartbeat must not be a hard conjunct: the shim stamps it from
          its BACKGROUND POLL thread, which survives a hung Ren'Py main loop,
          and an act that re-renders an identical surface produces neither a
          new pending request nor a differing screen — so "wait for the
          heartbeat to die" is a wedge that never resolves while the process
          lives.  Elapsed time since *apply* remains no evidence at all: a
          live story resets the clock on every line and is never truncated.
          Either tier releases admission for another act but leaves the
          transaction applied/pending. Resumed output revokes the release;
          dispatching a newly accepted act settles the old record as
          ``superseded_after_idle`` before activating the new nonce.

        """
        if record.get("transaction_state") != "applied":
            return False
        now = time.time()
        if record.get("settle_observed"):
            observed_at = float(record.get("settle_observed_at", 0) or 0)
            if now - observed_at < self._transaction_settle_grace(record):
                return False
        elif record.get("gate_released"):
            # Admission and completion are deliberately separate. Continued
            # silence cannot establish a story boundary.
            return False
        else:
            return self._release_idle_admission_locked(record, now)
        self._settle_transaction_locked(record)
        return True

    def _shim_heartbeat_alive_locked(self, now: float) -> bool:
        """Whether the shim's command poll was seen within the base idle TTL.

        Never polled (``0``) is the MOST abandoned state, not an exemption.
        """
        shim_seen_at = float(self._last_shim_command_poll_at or 0)
        return (
            shim_seen_at > 0
            and now - shim_seen_at < self._ACTION_APPLIED_IDLE_TTL
        )

    def _release_idle_admission_locked(
        self, record: dict[str, Any], now: float,
    ) -> bool:
        """Open the admission gate for a record whose settle was never seen."""
        idle_since = float(
            record.get("last_event_at")
            or record.get("applied_at", 0)
            or 0
        )
        idle_for = now - idle_since
        if idle_for < self._ACTION_APPLIED_IDLE_TTL:
            return False
        if self._shim_heartbeat_alive_locked(now):
            if idle_for < (
                self._ACTION_APPLIED_IDLE_TTL
                * self._ACTION_LIVE_SHIM_IDLE_MULTIPLIER
            ):
                return False
            record["gate_released_by"] = "idle_ttl_live_shim"
        else:
            record["gate_released_by"] = "abandoned_shim"
        record["gate_released"] = True
        record["gate_released_at"] = now
        self._touch_transaction_locked(record)
        return True

    def _settle_transaction_locked(self, record: dict[str, Any]) -> None:
        """Move a record to ``settled`` and close its admission bookkeeping.

        Events are untouched: settling is not discarding, and the scoped drain
        (plus the registry's spill rules) works on the record exactly as it
        does for a boundary settle.
        """
        record["transaction_state"] = "settled"
        record.pop("gate_released", None)
        record.pop("gate_released_by", None)
        record.pop("gate_released_at", None)
        nonce = record.get("action_nonce")
        if self._active_action_nonce == nonce:
            self._active_action_nonce = None
        self._mark_settled_locked(nonce)
        self._touch_transaction_locked(record)

    def _finalize_active_transaction_if_quiet_locked(self) -> dict[str, Any] | None:
        """Advance the active transaction at a boundary or idle gate.

        Called from event push, a new pending request, and the next act
        submission, so lifecycle progress never depends on a client polling
        ``/transaction``. An un-polled applied record used to block the next
        act forever (act(wait=False) wedged the slot).
        """
        record = self._act_transactions.get(self._active_action_nonce or "")
        if record is None:
            return None
        if self._finalize_transaction_if_quiet_locked(record):
            return dict(record)
        return None

    def _reap_idle_applied_transactions_locked(self) -> list[dict[str, Any]]:
        """Advance every applied record past its idle budget.

        This sweep opens admission after the appropriate idle budget. It does
        not settle a released transaction: silence is not a story boundary,
        and dead processes are handled by slot reaping/free.

        ``_finalize_active_transaction_if_quiet_locked`` only ever looks at
        ``_active_action_nonce``.  A record can be applied while that pointer
        is elsewhere (a bridge restart reconstructing more than one live
        record, or an active nonce cleared by a failed sibling), so the
        in-flight guard is swept the same way the lease TTL is: from every
        opportunistic site, over the whole registry.
        """
        reaped: list[dict[str, Any]] = []
        for record in list(self._act_transactions.values()):
            if record.get("transaction_state") != "applied":
                continue
            if record.get("settle_observed"):
                # The observed-boundary path belongs to the active pointer;
                # this sweep exists only for the never-observed wedge.
                continue
            if self._finalize_transaction_if_quiet_locked(record):
                reaped.append(dict(record))
        return reaped

    def _compact_transaction_locked(self, record: dict[str, Any]) -> bool:
        """Collapse a settled, fully drained transaction to a tombstone.

        The tombstone keeps identities, terminal state and the drain boundary
        so a late retry still deduplicates against the original transaction.
        Its events are dropped: the raw stream remains in the playthrough
        JSONL — the transaction journal is an index, not the archive.
        """
        if record.get("compacted"):
            return False
        if record.get("transaction_state") not in {"settled", "failed", "rejected"}:
            return False
        total = self._transaction_event_total(record)
        if int(record.get("drain_index", 0) or 0) < total:
            return False  # undrained output must stay recoverable by nonce
        record["event_count"] = int(
            record.get("events_truncated", 0) or 0) + total
        for key in list(record.keys()):
            if key not in self._TOMBSTONE_KEYS:
                del record[key]
        record["events"] = []
        record["drain_index"] = 0
        record["compacted"] = True
        self._touch_transaction_locked(record)
        return True

    def _evict_transactions_locked(self) -> None:
        """Bound the registry by SPILLING to the journal, never by deleting.

        Two tiers, oldest first:

        1. **Drained tombstones** are already event-free; dropping the dict
           costs nothing recoverable.
        2. **Settled records that still hold UNDRAINED output** are not
           destroyed.  Their events were journalled as they arrived, so the
           record becomes an *offloaded tombstone* — identities, terminal
           state and the drain boundary survive in the journal, the nonce is
           remembered in ``_offloaded_transaction_nonces``, and a later
           ``wait(action_nonce=...)`` rehydrates the whole thing from disk
           (``_rehydrate_transaction_from_journal``).

        Pending transactions are never touched.  With no journal configured
        tier 2 is skipped entirely and the registry is allowed to exceed the
        target: bounding RAM must not cost story text that has nowhere to live.
        """
        if len(self._act_transactions) <= self._MAX_ACT_TRANSACTIONS:
            return
        # Oldest first; pending records always sort last and are never evicted.
        def rank(item: tuple[str, dict[str, Any]]) -> tuple[int, int]:
            _, record = item
            if self._transaction_pending(
                str(record.get("transaction_state", "accepted"))
            ):
                return (2, int(record.get("action_id", 0) or 0))
            if record.get("compacted"):
                return (0, int(record.get("action_id", 0) or 0))
            return (1, int(record.get("action_id", 0) or 0))

        ordered = sorted(self._act_transactions.items(), key=rank)
        excess = len(self._act_transactions) - self._MAX_ACT_TRANSACTIONS
        for nonce, record in ordered:
            if excess <= 0:
                break
            if self._transaction_pending(
                str(record.get("transaction_state", "accepted"))
            ):
                break  # pending sorts last: nothing evictable remains
            if nonce in (self._active_action_nonce, self._last_settled_action_nonce):
                continue
            if not record.get("compacted"):
                if not self._transaction_log_path:
                    continue
                # Tier 2: this record's events are already IN the journal —
                # every attributed event is written there as it arrives, and
                # its own snapshot (identities, terminal state, drain
                # boundary) was persisted at acceptance and on every ack.
                # Dropping the in-memory copy is therefore a spill, not a
                # deletion, as long as the journal is caught up.
                if int(record.get("events_journalled", 0) or 0) < (
                    self._transaction_event_total(record)
                ):
                    continue  # not fully journalled yet — keep it in memory
            self._remember_offloaded_nonce_locked(nonce)
            del self._act_transactions[nonce]
            excess -= 1

    def get_action_transaction(self, nonce: str) -> dict[str, Any] | None:
        # Event delivery is replayable until acknowledge_action_events()
        # succeeds; reading a transaction never consumes it.
        self._flush_transaction_events()
        # A spilled (offloaded-tombstone) transaction is reloaded from the
        # journal before anything else — the registry bound is a cache bound,
        # not a recovery bound.
        self._rehydrate_transaction_from_journal(nonce)
        snapshots: list[dict[str, Any]] = []
        with self._lock:
            if not self._closed:
                snapshots.extend(self._expire_stale_transactions_locked())
            record = self._act_transactions.get(nonce)
            if record is not None and (
                not self._closed
                and self._finalize_transaction_if_quiet_locked(record)
            ):
                snapshots.append(dict(record))
        for snapshot in snapshots:
            # Durability first: the view below claims transaction_state
            # "settled", so that transition must be on disk before any caller
            # can act on it.  A failed write leaves the record settled in
            # memory and re-persists on the next revision — divergence is
            # bounded and one-directional (disk lags memory, never leads).
            self._persist_transaction_snapshot(snapshot)
        if record is None:
            return None
        with self._lock:
            record = self._act_transactions.get(nonce)
            if record is None:
                return None
            view = self._transaction_view_locked(record)
            start = int(record.get("drain_index", 0) or 0)
            offloaded = int(record.get("events_offloaded", 0) or 0)
            tail = list(record.get("events") or [])
        # The cursor is a LOGICAL index. When it points into events this
        # transaction spilled to bound RAM, reload exactly that range from the
        # journal (with the state lock released) instead of serving a hole.
        if start < offloaded:
            stored_events = self._read_journal_transaction_events(nonce)
            if len(stored_events) < offloaded:
                raise TransactionJournalError(
                    "Transaction journal is missing an offloaded event prefix."
                )
            head = stored_events[start:offloaded]
            events = head + tail
        else:
            head = []
            events = tail[start - offloaded:]
        view["events"] = events
        view["delivery_cursor"] = start
        view["delivery_end"] = start + len(events)
        if head:
            view["events_reloaded"] = len(head)
        return view

    def acknowledge_action_events(self, nonce: str, delivery_end: int) -> str:
        """Advance a transaction's delivery cursor.

        Returns "ok", "unknown_nonce", or "persist_failed" — a storage problem
        is NOT the same answer as an unknown nonce, and reporting both as 404
        told clients to give up on a transaction that still exists.
        """
        with self._transaction_ack_lock:
            return self._acknowledge_action_events_serialized(nonce, delivery_end)

    def _acknowledge_action_events_serialized(
        self, nonce: str, delivery_end: int,
    ) -> str:
        # A spilled transaction can be acknowledged too — rehydrate first so a
        # bounded registry never turns a real drain into "unknown_nonce".
        try:
            self._rehydrate_transaction_from_journal(nonce)
        except TransactionJournalError:
            return "storage_error"
        with self._lock:
            record = self._act_transactions.get(nonce)
            if record is None:
                return "unknown_nonce"
            current = int(record.get("drain_index", 0) or 0)
            bounded = min(
                max(current, int(delivery_end)),
                self._transaction_event_total(record))
            if bounded == current:
                return "ok"
            snapshot = dict(record)
            snapshot["drain_index"] = bounded
            self._touch_transaction_locked(snapshot)
        # Durability BEFORE the ack response, with the state lock released.
        if not self._persist_transaction(snapshot):
            return "persist_failed"
        with self._lock:
            record = self._act_transactions.get(nonce)
            if record is None:
                return "unknown_nonce"
            record["drain_index"] = max(
                int(record.get("drain_index", 0) or 0),
                int(snapshot["drain_index"]),
            )
            record["revision"] = max(
                int(record.get("revision", 0) or 0),
                int(snapshot["revision"]),
            )
            compacted = self._compact_transaction_locked(record)
            if compacted:
                tombstone = dict(record)
            self._evict_transactions_locked()
        if compacted:
            self._persist_transaction(tombstone)
            self._maybe_rewrite_transaction_journal()
        return "ok"

    def _open_log(self) -> None:
        if self._log_file is not None:
            return
        logs_dir = self._storage_dir or os.path.join(os.getcwd(), "bridge", "logs")
        os.makedirs(logs_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Slots can start (or rotate) in the same second. Claim a fresh file
        # atomically rather than appending to another slot's transcript.
        fd, path = tempfile.mkstemp(
            prefix=f"playthrough_{ts}_", suffix=".jsonl", dir=logs_dir)
        self._log_file = os.fdopen(fd, "w", encoding="utf-8")
        self._log_path = path

    def _close_log(self) -> None:
        """Close the log file under the write lock (never the state lock).

        Uses the same ``_log_write_lock`` as ``_flush_log`` so a concurrent
        off-lock flush can't write into a just-closed handle.  MUST NOT be
        called while already holding ``_log_write_lock`` (the lock is not
        reentrant) — ``reset`` inlines the close for that reason.
        """
        with self._log_write_lock:
            if self._log_file:
                try:
                    self._log_file.close()
                except Exception:
                    pass
                self._log_file = None
                self._log_path = None

    # Max log file size before rotation (50 MB).
    _MAX_LOG_BYTES: int = 50 * 1024 * 1024

    # Hard cap on the in-memory log buffer.  A transient write failure
    # re-queues unwritten lines (see _flush_log); this bound keeps a
    # permanently-broken file from growing memory without limit.
    _MAX_LOG_BUFFER: int = 2000

    def _log_event(self, event: dict) -> None:
        """Serialize *event* and queue it for the off-lock writer.

        Called while holding ``self._lock`` — this MUST stay cheap (no file
        I/O).  The actual write+flush happens in ``_flush_log`` after the lock
        is released, so a stalled disk / AV scan can't block command traffic.
        Ordering is preserved: lines are appended in _seq order under the
        state lock, and ``_flush_log`` drains them FIFO under a dedicated
        write lock.
        """
        try:
            # Strip large binary blobs (e.g. screenshot image data) from log.
            if event.get("type") == "screenshot":
                event = {k: v for k, v in event.items() if k != "image"}
                event["image"] = "<stripped>"
            self._log_buffer.append(
                json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _flush_log(self) -> None:
        """Write queued log lines to disk OUTSIDE the state lock.

        Safe to call redundantly.  Must be called with ``self._lock`` NOT
        held (lock order is always ``_log_write_lock`` -> ``_lock``, never the
        reverse, so no deadlock with the enqueue path).
        """
        with self._log_write_lock:
            # Brief, I/O-free hold to snapshot & clear the pending buffer;
            # the slow write below runs with the state lock released.
            with self._lock:
                if not self._log_buffer:
                    return
                pending = self._log_buffer
                self._log_buffer = []
            written = 0
            try:
                self._open_log()
                for line in pending:
                    self._log_file.write(line)  # type: ignore[union-attr]
                    written += 1
                self._log_file.flush()  # type: ignore[union-attr]

                # Rotate if file exceeds size limit.
                if self._log_file.tell() > self._MAX_LOG_BYTES:  # type: ignore[union-attr]
                    self._log_file.close()  # type: ignore[union-attr]
                    self._log_file = None
                    # _open_log creates a new timestamped file on next call.
            except Exception:
                # A transient disk failure (open/write/flush) must not lose
                # diagnostic lines.  Re-queue the lines that were NOT written
                # at the FRONT of the buffer, preserving order, so the next
                # flush retries them ahead of newer lines.
                unwritten = pending[written:]
                if unwritten:
                    with self._lock:
                        self._log_buffer[:0] = unwritten
                        # Bounded: if the file stays broken the buffer grows
                        # each flush; drop the OLDEST over the cap so memory
                        # can't grow without limit, and note it once.
                        overflow = len(self._log_buffer) - self._MAX_LOG_BUFFER
                        if overflow > 0:
                            del self._log_buffer[:overflow]
                            if not self._log_buffer_overflow_noted:
                                self._log_buffer_overflow_noted = True
                                print(
                                    "[vnflight.bridge] log write failing; "
                                    "buffer capped at {} lines, dropping "
                                    "oldest diagnostics".format(
                                        self._MAX_LOG_BUFFER
                                    ),
                                    file=sys.stderr,
                                    flush=True,
                                )

    # game_state keys that carry playthrough VALUES (as opposed to the UI
    # surface: interactions / choices / screen_buttons, which must stay live
    # so the main menu is still navigable after an ending).
    _PROGRESS_KEYS: tuple[str, ...] = ("stats", "inventory", "_stats_ts")

    def _at_menu_screen_locked(self) -> bool:
        """Whether the freshest scrape showed the main menu.

        The shim pushes screen_content immediately before game_state in the
        same scrape, so its explicit ``main_menu`` bit is the tightest signal
        that a scrape happened after Ren'Py reset its store — it lands even
        when the (rate limited, <=0.5s) context event that latches the terminal
        has not arrived yet. It keys off ``store.main_menu`` rather than a
        screen name, so it also works for games whose title screen is not
        literally called "main_menu" (Echoes of Tomorrow's is ``menu``).
        Gated on end_on_menu_return so games whose live gameplay screens are
        classified as the main menu (Slay the Princess) are unaffected: their
        terminal never latches from a menu return either.
        """
        if not self.end_on_menu_return:
            return False
        if (self.current_context or {}).get("context") == "main_menu":
            return True
        # Screen tags are not lifecycle provenance. Ren'Py uses ``menu`` for
        # both a custom title menu and the in-game game-menu wrapper, and some
        # games leave ``main_menu`` mounted during play. The shim samples
        # store.main_menu in the same scrape, before the rate-limited context
        # event, so only that explicit bit can close the ordering window.
        return (self.current_screen or {}).get("main_menu") is True

    def _capture_live_progress_locked(self, event: dict) -> None:
        """Remember stats/inventory from a game_state scraped during play.

        Skipped once the playthrough is terminal/ended (the store has reset)
        so ``_live_progress`` keeps the last PRE-terminal values.  Caller
        holds _lock.
        """
        if self.current_game_terminal or self.status == "ended":
            return
        if self._progress_capture_suspended:
            return
        if self._at_menu_screen_locked():
            return
        progress = {k: event[k] for k in self._PROGRESS_KEYS if k in event}
        if progress:
            self._live_progress = progress

    def _clear_terminal_latch_locked(self) -> None:
        """Un-latch the playthrough-terminal flag.

        Called wherever a new / resumed run supersedes an ending (game_started,
        a fresh in_game context, load recovery, game_resumed, reset).  Clearing
        the latch is what restores LIVE values: ``_effective_game_state_locked``
        only consults the snapshot while the latch is set.

        The snapshot itself deliberately SURVIVES.  Ren'Py fires
        config.start_callbacks — which the shim reports as game_started — as
        part of the return-to-menu restart, a fraction of a second BEFORE the
        menu-return latch re-arms; dropping the snapshot here threw away the
        very end-of-run values the freeze exists to preserve, so the re-latched
        terminal then served the reset store's defaults.  ``_live_progress`` is
        a plain cache of the last genuinely-live scrape: only a newer live
        capture replaces it, and only reset() (a brand-new session) drops it.
        Caller holds _lock.
        """
        self.current_game_terminal = False

    def _effective_game_state_locked(self) -> dict | None:
        """The game_state to serve clients: live, or frozen when terminal.

        While the terminal is latched the stored event keeps updating from
        post-menu scrapes (buttons/interactions must stay live), but the
        progress values are replaced with the last pre-terminal snapshot and
        tagged ``progress_frozen`` so callers can tell.  Caller holds _lock.
        """
        gs = self.current_game_state
        if gs is None or not self.current_game_terminal:
            return gs
        # Surface the latched terminal flag without mutating the stored event.
        gs = dict(gs)
        gs["game_terminal"] = True
        if self._live_progress:
            gs.update(self._live_progress)
            gs["progress_frozen"] = True
        return gs

    def _terminal_evidence_snapshot_locked(self) -> dict:
        """Compact snapshot of what the menu-classified screen actually
        contained.  Caller holds _lock.  No screenshots/base64 — this is
        sized to live in the playthrough JSONL."""
        screen = self.current_screen or {}
        gstate = self.current_game_state or {}
        buttons: list[str] = []
        for b in (screen.get("buttons") or []):
            label = b.get("label") if isinstance(b, dict) else None
            if label:
                buttons.append(str(label))
        if not buttons:
            for b in (gstate.get("screen_buttons") or []):
                label = b.get("label") if isinstance(b, dict) else None
                if label:
                    buttons.append(str(label))
        interactions: list[str] = []
        for itr in (gstate.get("interactions") or []):
            if isinstance(itr, dict):
                label = itr.get("label") or itr.get("text")
                if label:
                    interactions.append(str(label))
        story_tail: list[str] = []
        for ev in reversed(self.transcript):
            if ev.get("type") in ("narration", "dialogue", "say"):
                text = str(ev.get("text", ""))
                who = ev.get("character")
                story_tail.append(f"{who}: {text}" if who else text)
                if len(story_tail) == 3:
                    break
        story_tail.reverse()
        return {
            "screens": list(screen.get("screens") or []),
            "buttons": buttons[:20],
            "interactions": interactions[:20],
            "story_tail": story_tail,
        }

    def _emit_terminal_evidence_locked(self, source: str,
                                       suppressed: bool) -> None:
        """Synthesize a terminal_evidence transcript event at the moment
        the menu-return terminal fires (or is suppressed by the per-game
        end_on_menu_return opt-out).  Caller holds _lock.

        The next false positive self-documents what the menu-classified
        screen contained, so a button-shape gate or a game-mod override
        can be designed from data.  One event per menu-return episode
        (deduped via _menu_evidence_emitted)."""
        if self._menu_evidence_emitted:
            return
        self._menu_evidence_emitted = True
        self.event_counter += 1
        evidence = {
            "type": "terminal_evidence",
            "reason": "return_to_menu",
            "suppressed": suppressed,
            "source": source,
            "snapshot": self._terminal_evidence_snapshot_locked(),
            "timestamp": time.time(),
            "_seq": self.event_counter,
        }
        self.transcript.append(evidence)
        self._log_event(evidence)

    # -- public API --

    def _trim_memory_locked(self) -> None:
        """Bound in-memory growth on long (multi-hour) runs. Caller holds _lock.

        The JSONL event log keeps full durable history; the in-memory
        transcript only needs a recent window. event_counter is never
        reset, so get_state(since=N) keeps working — a client whose cursor
        predates the trimmed floor simply receives events from the floor
        onward (its stale-cursor reset handles the gap). Without this the
        transcript and requests_by_id grew unbounded (an 8h playthrough is
        exactly the scenario that exhausts memory).
        """
        if len(self.transcript) > self._max_transcript:
            del self.transcript[:-self._max_transcript]
        # requests_by_id accrues one entry per request forever; keep a
        # generous recent window. Pending/in-flight requests are the most
        # recent insertions, so they are never pruned. dict preserves
        # insertion order (py3.7+).
        _max_req = max(self._max_transcript, 1000)
        if len(self.requests_by_id) > _max_req:
            _excess = len(self.requests_by_id) - _max_req
            for _old_key in list(self.requests_by_id)[:_excess]:
                del self.requests_by_id[_old_key]

    @staticmethod
    def _passive_rows_delta(
        previous: tuple[str, ...], panel: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Return appended rows from an ordered, possibly rolling panel."""
        return tuple(passive_rows_delta(previous, panel))

    def _annotate_passive_overlay_delta_locked(
        self,
        event: dict[str, Any],
        contributors: tuple[str, ...],
        by_screen: dict[str, tuple[str, ...]],
        generations: dict[str, str],
        retained: set[str],
        passive_texts: tuple[str, ...],
    ) -> None:
        """Attach the new rows while updating the bridge-owned baseline."""
        event_seq = int(event["_seq"])
        delta: list[str] = []
        if by_screen:
            if (
                not self._passive_overlay_rows_by_screen
                and self._passive_overlay_rows
            ):
                # A shim can gain contributor provenance while the bridge is
                # running. Seed the richer ledger from the full snapshot, but
                # expose only the aggregate overlap delta at that boundary.
                delta.extend(self._passive_rows_delta(
                    self._passive_overlay_rows, passive_texts,
                ))
                self._passive_overlay_rows_by_screen = {
                    tag: by_screen.get(tag, ()) for tag in contributors
                }
                self._passive_overlay_row_seqs_by_screen = {
                    tag: (event_seq,) * len(by_screen.get(tag, ()))
                    for tag in contributors
                }
                self._passive_overlay_generations = {
                    tag: generations[tag]
                    for tag in contributors if tag in generations
                }
                self._passive_overlay_rows = passive_texts
                self._passive_overlay_row_seqs = tuple(
                    seq
                    for tag in contributors
                    for seq in self._passive_overlay_row_seqs_by_screen.get(
                        tag, ())
                )
                event["passive_overlay_delta"] = delta
                self._attach_passive_overlay_row_seqs_locked(
                    event, contributors, by_screen)
                return
            for tag in contributors:
                rows = by_screen.get(tag, ())
                prior_generation = self._passive_overlay_generations.get(tag)
                generation = generations.get(tag)
                previous = (
                    self._passive_overlay_rows_by_screen.get(tag, ())
                    if generation is not None and generation == prior_generation
                    else ()
                )
                previous_seqs = (
                    self._passive_overlay_row_seqs_by_screen.get(tag, ())
                    if previous else ()
                )
                # A short prefix is commonly an asynchronous stale scrape of
                # a cumulative panel. Do not shrink the durable baseline.
                if len(rows) < len(previous) and previous[:len(rows)] == rows:
                    continue
                # Retained overlays own their generation across an explicit
                # empty frame; reopening the same generation is not new.
                if not rows and tag in retained and generation == prior_generation:
                    continue
                delta.extend(self._passive_rows_delta(previous, rows))
                self._passive_overlay_row_seqs_by_screen[tag] = tuple(
                    align_row_provenance(
                        previous, rows, previous_seqs, event_seq)
                )
                self._passive_overlay_rows_by_screen[tag] = rows
                if generation is not None:
                    self._passive_overlay_generations[tag] = generation
            self._prune_passive_overlay_contributors_locked(
                retained, set(contributors))
            self._passive_overlay_rows = passive_texts
            self._passive_overlay_row_seqs = tuple(
                seq
                for tag in contributors
                for seq in self._passive_overlay_row_seqs_by_screen.get(tag, ())
            )
            self._attach_passive_overlay_row_seqs_locked(
                event, contributors, by_screen)
        else:
            contributor_state = bool(
                self._passive_overlay_rows_by_screen
                or self._passive_overlay_generations
                or self._passive_overlay_row_seqs_by_screen
            )
            if contributor_state and not passive_texts:
                # A registry-only frame means the retained contributors are
                # hidden, not that their delivered occurrences disappeared.
                # Preserve those ledgers so an intervening passive screen
                # cannot force a schema-upgrade reset and replay them.
                self._prune_passive_overlay_contributors_locked(retained)
                self._passive_overlay_rows = ()
                self._passive_overlay_row_seqs = ()
                event["passive_overlay_row_seqs_by_screen"] = {}
                event["passive_overlay_row_seqs"] = []
                event["passive_overlay_delta"] = delta
                return
            previous = self._passive_overlay_rows
            if not (
                len(passive_texts) < len(previous)
                and previous[:len(passive_texts)] == passive_texts
            ):
                delta.extend(self._passive_rows_delta(previous, passive_texts))
                self._passive_overlay_row_seqs = tuple(
                    align_row_provenance(
                        previous,
                        passive_texts,
                        self._passive_overlay_row_seqs,
                        event_seq,
                    )
                )
                self._passive_overlay_rows = passive_texts
            self._passive_overlay_rows_by_screen = {}
            self._passive_overlay_generations = {}
            self._passive_overlay_row_seqs_by_screen = {}
            event["passive_overlay_row_seqs"] = list(
                self._passive_overlay_row_seqs)
        event["passive_overlay_delta"] = delta

    def _prune_passive_overlay_contributors_locked(
        self,
        retained: set[str],
        active: set[str] | None = None,
    ) -> None:
        """Retire hidden contributor ownership absent from the registry."""
        active = active or set()
        tracked = (
            set(self._passive_overlay_rows_by_screen)
            | set(self._passive_overlay_generations)
            | set(self._passive_overlay_row_seqs_by_screen)
        )
        for tag in tracked:
            if tag in active or tag in retained:
                continue
            self._passive_overlay_rows_by_screen.pop(tag, None)
            self._passive_overlay_generations.pop(tag, None)
            self._passive_overlay_row_seqs_by_screen.pop(tag, None)

    def _attach_passive_overlay_row_seqs_locked(
        self,
        event: dict[str, Any],
        contributors: tuple[str, ...],
        by_screen: dict[str, tuple[str, ...]],
    ) -> None:
        """Expose first-observed sequence numbers beside cumulative rows."""
        aligned = {
            tag: list(
                self._passive_overlay_row_seqs_by_screen.get(tag, ())[
                    :len(by_screen.get(tag, ()))
                ]
            )
            for tag in contributors
        }
        event["passive_overlay_row_seqs_by_screen"] = aligned
        event["passive_overlay_row_seqs"] = [
            seq for tag in contributors for seq in aligned.get(tag, ())
        ]

    def _retire_passive_overlay_resume_locked(self) -> None:
        self._passive_overlay_resume_pending = False
        self._passive_overlay_resume_first_snapshot = False
        self._passive_overlay_resume_rows = ()
        self._passive_overlay_resume_rows_by_screen = {}
        self._passive_overlay_resume_generations = {}
        self._passive_overlay_resume_row_seqs = ()
        self._passive_overlay_resume_row_seqs_by_screen = {}

    def _retain_passive_overlay_resume_locked(
        self,
        retained: set[str],
        preserve_first_snapshot: bool = False,
    ) -> bool:
        """Keep only hidden contributors explicitly retained by the shim."""
        first_snapshot = self._passive_overlay_resume_first_snapshot
        remaining = {
            tag for tag in self._passive_overlay_resume_rows_by_screen
            if tag in retained
        }
        if not remaining:
            self._retire_passive_overlay_resume_locked()
            return False
        self._passive_overlay_resume_pending = True
        self._passive_overlay_resume_first_snapshot = bool(
            preserve_first_snapshot and first_snapshot)
        self._passive_overlay_resume_rows = ()
        self._passive_overlay_resume_rows_by_screen = {
            tag: self._passive_overlay_resume_rows_by_screen[tag]
            for tag in remaining
        }
        self._passive_overlay_resume_generations = {
            tag: self._passive_overlay_resume_generations[tag]
            for tag in remaining
            if tag in self._passive_overlay_resume_generations
        }
        self._passive_overlay_resume_row_seqs = ()
        self._passive_overlay_resume_row_seqs_by_screen = {
            tag: self._passive_overlay_resume_row_seqs_by_screen.get(tag, ())
            for tag in remaining
        }
        return True

    def _reset_passive_overlay_state_locked(self) -> None:
        self._passive_overlay_signature = None
        self._passive_overlay_rows = ()
        self._passive_overlay_rows_by_screen = {}
        self._passive_overlay_generations = {}
        self._passive_overlay_row_seqs = ()
        self._passive_overlay_row_seqs_by_screen = {}
        self._retire_passive_overlay_resume_locked()

    def _prime_resumed_passive_overlay_locked(
        self,
        event: dict[str, Any],
        contributors: tuple[str, ...],
        by_screen: dict[str, tuple[str, ...]],
        generations: dict[str, str],
        retained: set[str],
        passive_texts: tuple[str, ...],
    ) -> None:
        """Seed first-resume differencing from already delivered rows."""
        if not self._passive_overlay_resume_pending:
            return
        first_snapshot = self._passive_overlay_resume_first_snapshot
        carried_rows = dict(self._passive_overlay_resume_rows_by_screen)
        carried_generations = dict(
            self._passive_overlay_resume_generations)
        carried_seqs = dict(
            self._passive_overlay_resume_row_seqs_by_screen)
        seeded = False
        if by_screen:
            for tag in contributors:
                if tag not in carried_rows and not first_snapshot:
                    continue
                if (
                    not first_snapshot
                    and tag in carried_generations
                    and generations.get(tag) != carried_generations[tag]
                ):
                    continue
                seeded = True
                generation_changed = bool(
                    first_snapshot
                    and tag in carried_generations
                    and generations.get(tag) != carried_generations[tag]
                )
                current_rows = by_screen.get(tag, ())
                baseline_rows = carried_rows.get(tag, current_rows)
                baseline_seqs = carried_seqs.get(tag, ())
                if generation_changed:
                    # A shared prompt does not prove occurrence continuity
                    # across generations. The first synchronous scrape is
                    # restored history; later growth is fresh output.
                    baseline_rows = current_rows
                    baseline_seqs = ()
                self._passive_overlay_rows_by_screen[tag] = baseline_rows
                self._passive_overlay_row_seqs_by_screen[tag] = baseline_seqs
                if tag in generations:
                    # A rollback can restore an older generation value. The
                    # carried rows are occurrence ownership, so compare them
                    # to this restored generation rather than replaying them
                    # merely because its value changed.
                    self._passive_overlay_generations[tag] = generations[tag]
        elif passive_texts and (
            self._passive_overlay_resume_rows or first_snapshot
        ):
            seeded = True
            self._passive_overlay_rows = (
                self._passive_overlay_resume_rows or passive_texts
            )
            self._passive_overlay_row_seqs = (
                self._passive_overlay_resume_row_seqs
                if self._passive_overlay_resume_rows
                else (int(event["_seq"]),) * len(passive_texts)
            )
        if seeded:
            event["passive_overlay_resumed_baseline"] = True
        remaining_tags = {
            tag for tag in carried_rows
            if tag not in contributors and tag in retained
        }
        if remaining_tags:
            self._passive_overlay_resume_pending = True
            self._passive_overlay_resume_first_snapshot = False
            self._passive_overlay_resume_rows = ()
            self._passive_overlay_resume_rows_by_screen = {
                tag: carried_rows[tag] for tag in remaining_tags
            }
            self._passive_overlay_resume_generations = {
                tag: carried_generations[tag]
                for tag in remaining_tags if tag in carried_generations
            }
            self._passive_overlay_resume_row_seqs = ()
            self._passive_overlay_resume_row_seqs_by_screen = {
                tag: carried_seqs.get(tag, ()) for tag in remaining_tags
            }
        else:
            self._retire_passive_overlay_resume_locked()

    def push_event(self, event: dict) -> int | None:
        """Append an event to the transcript. Returns the new event counter."""
        transaction_snapshot = None
        canceled_transaction_snapshots: list[dict[str, Any]] = []
        transaction_event: tuple[str, dict[str, Any]] | None = None
        finalized_snapshot = None
        with self._lock:
            if self._closed:
                return None
            etype = event.get("type", "")
            source_id = event.get("_source_id")
            source_seq = event.get("_source_seq")
            source_key = None
            if (isinstance(source_id, str)
                    and isinstance(source_seq, int)):
                source_key = (source_id, source_seq)
                if source_key in self._delivered_source_events:
                    self._delivered_source_events.move_to_end(source_key)
                    return self.event_counter
            if etype in ("choice_resolved", "input_resolved"):
                req_id = event.get("request_id")
                record = self.requests_by_id.get(req_id)
                prior_resolution = (
                    record.get("resolution") if record is not None else None
                )
                if (
                    isinstance(prior_resolution, dict)
                    and prior_resolution.get("type") == etype
                ):
                    return self.event_counter
            if (etype == "command_result"
                    and event.get("command") != "act"):
                result_nonce = event.get("nonce")
                if isinstance(result_nonce, str) and result_nonce:
                    if result_nonce in self._delivered_command_result_nonces:
                        self._delivered_command_result_nonces.move_to_end(
                            result_nonce)
                        return self.event_counter
            # What a queued act could still target, BEFORE this event mutates
            # anything.  Compared again at the end of the critical section: a
            # difference is the one thing that cancels a queued successor.
            surface_before = self._queued_act_surface_signature_locked()
            # Opportunistic lifecycle progress: an event may finalize an
            # observed boundary or release an otherwise wedged idle gate.
            finalized_snapshot = self._finalize_active_transaction_if_quiet_locked()
            self.event_counter += 1
            event["_seq"] = self.event_counter

            # Track status from certain event types.
            superseded_snapshot = (
                self._supersede_active_transaction_for_command_locked(event)
            )
            if superseded_snapshot is not None:
                transaction_snapshot = superseded_snapshot
            if etype == "screen_content":
                # The shim already pushes every rendered screen snapshot. Keep
                # ordinary UI state latest-only, but retain changed passive
                # overlay text so a short-lived terminal/log cannot appear and
                # vanish between client polls. Exact repeats stay state-only.
                passive_texts = tuple(
                    str(text).strip()
                    for text in (event.get("overlay_texts") or [])
                    if str(text).strip()
                )
                if not event.get("overlay_active"):
                    passive_signature = None
                    contributors = tuple(
                        str(tag) for tag in (
                            event.get("overlay_screens") or []
                        )
                    )
                    raw_by_screen = event.get("overlay_texts_by_screen")
                    by_screen = {}
                    if isinstance(raw_by_screen, dict):
                        by_screen = {
                            str(tag): tuple(
                                str(text).strip()
                                for text in (texts or [])
                                if str(text).strip()
                            )
                            for tag, texts in raw_by_screen.items()
                            if isinstance(texts, list)
                        }
                    raw_generations = event.get("overlay_generations") or {}
                    if not isinstance(raw_generations, dict):
                        raw_generations = {}
                    generations = {
                        str(tag): str(generation)
                        for tag, generation in raw_generations.items()
                    }
                    retained = {
                        str(tag) for tag in (
                            event.get("overlay_retained_screens") or []
                        )
                    }
                    if passive_texts or contributors or by_screen:
                        passive_signature = (
                            contributors,
                            tuple(
                                (tag, generations[tag])
                                for tag in contributors
                                if tag in generations
                            ),
                            tuple(
                                tag for tag in contributors if tag in retained
                            ),
                            tuple(
                                (tag, by_screen.get(tag, ()))
                                for tag in contributors
                            ),
                            passive_texts,
                        )
                    if (
                        (passive_signature is not None
                         or self._passive_overlay_signature is not None)
                        and passive_signature != self._passive_overlay_signature
                    ):
                        event["passive_overlay_snapshot"] = True
                        self._prime_resumed_passive_overlay_locked(
                            event, contributors, by_screen, generations,
                            retained, passive_texts,
                        )
                        self._annotate_passive_overlay_delta_locked(
                            event,
                            contributors,
                            by_screen,
                            generations,
                            retained,
                            passive_texts,
                        )
                    elif passive_signature is not None:
                        # Protocol marker for latest-state reads: this bridge
                        # evaluated the cumulative panel and found no new
                        # occurrences. Absence of the field remains the legacy
                        # fallback signal for older bridges.
                        event["passive_overlay_delta"] = []
                        if by_screen:
                            self._attach_passive_overlay_row_seqs_locked(
                                event, contributors, by_screen)
                        else:
                            event["passive_overlay_row_seqs"] = list(
                                self._passive_overlay_row_seqs)
                    self._passive_overlay_signature = passive_signature
                    self._prune_passive_overlay_contributors_locked(
                        retained, set(contributors))
                    if (
                        self._passive_overlay_resume_pending
                        and passive_signature is None
                    ):
                        # The restored interaction has no visible passive
                        # panel. Preserve only contributors the shim still
                        # identifies as retained; otherwise a later panel is
                        # a new story surface.
                        self._retain_passive_overlay_resume_locked(retained)
                else:
                    # A blocking/modal frame is authoritative too. It cannot
                    # carry passive text, but may explicitly retain hidden
                    # contributors whose occurrence ownership still applies.
                    retained = {
                        str(tag) for tag in (
                            event.get("overlay_retained_screens") or []
                        )
                    }
                    visible_overlays = {
                        str(tag) for tag in (
                            event.get("overlay_screens") or []
                        )
                    }
                    blocking_overlays = {
                        str(tag) for tag in (
                            event.get("active_overlays") or []
                        )
                    }
                    visible_passive = visible_overlays - blocking_overlays
                    self._prune_passive_overlay_contributors_locked(
                        retained, visible_passive)
                    tracked = (
                        set(self._passive_overlay_rows_by_screen)
                        | set(self._passive_overlay_generations)
                        | set(self._passive_overlay_row_seqs_by_screen)
                    )
                    if not tracked.intersection(visible_passive):
                        self._passive_overlay_signature = None
                        self._passive_overlay_rows = ()
                        self._passive_overlay_row_seqs = ()
                    if self._passive_overlay_resume_pending:
                        self._retain_passive_overlay_resume_locked(
                            retained | visible_passive,
                            preserve_first_snapshot=bool(visible_passive),
                        )
            event_nonce, gate_revoked = self._record_transaction_event_locked(
                event
            )
            if event_nonce:
                transaction_event = (event_nonce, dict(event))
                if gate_revoked:
                    transaction_snapshot = dict(
                        self._act_transactions[event_nonce]
                    )

            if etype == "command_result" and event.get("command") == "act":
                nonce = event.get("nonce") or self._active_action_nonce
                record = self._act_transactions.get(str(nonce or ""))
                if (
                    record is not None
                    and record.get("transaction_state") == "accepted"
                    and record.get("dispatched")
                    and int(record.get("reset_generation", -1)) == self.reset_generation
                ):
                    self.pending_commands[:] = [
                        queued for queued in self.pending_commands
                        if queued.get("nonce") != nonce
                    ]
                    if event.get("success") is False:
                        record["transaction_state"] = "failed"
                        record["error"] = event.get("error") or event.get("message")
                        if self._active_action_nonce == nonce:
                            self._active_action_nonce = None
                    else:
                        record["transaction_state"] = "applied"
                        # Spec criterion 8: the applied event adds the ACTUAL
                        # resolution.  Copy every payload key the shim reported
                        # (resolved_as, interaction_type, wait_after_action,
                        # label, screen, index, plus anything a mod adds)
                        # rather than a whitelist that silently drops the
                        # fields post-act routing needs.
                        for key, value in event.items():
                            if key in self._ACT_RESULT_ENVELOPE_KEYS:
                                continue
                            record[key] = value
                        self._retire_request_ended_by_story_entry_locked(
                            record, event)
                    record["applied_at"] = time.time()
                    self._touch_transaction_locked(record)
                    transaction_snapshot = dict(record)

            # Screenshots and game_state are stateful. screen_content remains
            # latest-state-first, with changed passive-overlay snapshots also
            # appended as the narrow story-bearing exception.
            if etype == "screenshot":
                self.latest_screenshot = event.get("image")
                self.latest_screenshot_capture_id = event.get("capture_id")
            elif etype == "screen_content":
                self.current_screen = event
                if event.get("passive_overlay_snapshot"):
                    self.transcript.append(event)
            elif etype == "game_state":
                self.current_game_state = event
                # Runtime observations supersede bridge-only desired settings.
                playback = event.get("playback_config")
                if isinstance(playback, dict):
                    enabled = playback.get("auto_advance")
                    delay = playback.get("auto_advance_delay")
                    if isinstance(enabled, bool):
                        self.auto_advance = enabled
                    if isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay >= 0:
                        self.auto_advance_delay = delay
                self._capture_live_progress_locked(event)
            else:
                if (
                    etype == "progress_change"
                    and event.get("game_terminal")
                    and not self._progress_capture_suspended
                ):
                    # Latch: an ending node was reached. Stays set even if the
                    # game then returns to the main menu (a later non-terminal
                    # progress sample must not clear it).  Not during the
                    # pre-gameplay window after game_started: a fresh run
                    # cannot have ended before its first in_game context, and
                    # a stale terminal report there (a mod still seeing the
                    # previous run's ending) would re-arm the end-of-run
                    # freeze for the whole new playthrough.
                    self.current_game_terminal = True
                elif (etype in ("stats_update", "inventory_update")
                        and self.current_game_terminal
                        and (
                            self.status == "ended"
                            or self._at_menu_screen_locked()
                        )):
                    # Same store-reset artefact the game_state freeze guards
                    # against: once the run ENDED at the menu, Ren'Py's reset
                    # store re-emits every stat as a "change" back to its
                    # default.  Mark it so formatters skip the phantom deltas;
                    # the transcript/JSONL keeps the honest event.  Gated on
                    # status=="ended" OR direct menu-screen evidence. The
                    # latter closes the narrow ordering window where the
                    # terminal has rendered the title menu and reset its
                    # store, but the rate-limited context event has not yet
                    # changed status. A bare terminal latch is still
                    # insufficient, so real stat changes during an ending
                    # SEQUENCE continue to render.
                    event["post_terminal"] = True
                self.transcript.append(event)

            if etype == "game_started":
                self.status = "running"
                # Clear stale screen/game_state from previous context
                # (e.g. main menu buttons lingering after game start).
                self.current_screen = None
                self._reset_passive_overlay_state_locked()
                self.current_game_state = None
                self._clear_terminal_latch_locked()
                # Ren'Py runs start_callbacks for the return-to-menu restart as
                # well as for a real new run, and the store is already reset by
                # then — hold progress capture until gameplay is re-confirmed.
                self._progress_capture_suspended = True
                self._menu_evidence_emitted = False
            elif etype == "game_resumed":
                # A native Ren'Py load or rollback can move back out of an
                # ending without passing through game_started or an ended
                # main-menu context. Keep the transcript, but discard every
                # stateful value tied to the abandoned timeline so the next
                # scrape becomes authoritative.
                self.status = "running"
                self.end_reason = None
                self.pending_request = None
                self.pending_action = None
                del self.pending_commands[:]
                self.current_screen = None
                resume_rows = self._passive_overlay_rows
                resume_rows_by_screen = dict(
                    self._passive_overlay_rows_by_screen)
                resume_generations = dict(self._passive_overlay_generations)
                resume_row_seqs = self._passive_overlay_row_seqs
                resume_row_seqs_by_screen = dict(
                    self._passive_overlay_row_seqs_by_screen)
                self._reset_passive_overlay_state_locked()
                self._passive_overlay_resume_pending = True
                self._passive_overlay_resume_first_snapshot = True
                self._passive_overlay_resume_rows = resume_rows
                self._passive_overlay_resume_rows_by_screen = (
                    resume_rows_by_screen)
                self._passive_overlay_resume_generations = resume_generations
                self._passive_overlay_resume_row_seqs = resume_row_seqs
                self._passive_overlay_resume_row_seqs_by_screen = (
                    resume_row_seqs_by_screen)
                self.current_game_state = None
                self._clear_terminal_latch_locked()
                self._progress_capture_suspended = False
                self._gameplay_seen = True
                self._menu_evidence_emitted = False
                self._load_consumed_at = None
                self.current_context = {
                    "type": "context",
                    "context": "in_game",
                    "available_commands": ["save", "load", "quit"],
                    "inferred": f"{event.get('reason', 'unknown')}_resume",
                }
            elif etype == "game_ended":
                reason = event.get("reason", "unknown")
                # Why a shim-reported menu return may be ruled NON-terminal:
                #   end_on_menu_return -- per-game opt-out (Slay the Princess
                #     classifies live gameplay screens as main_menu).
                #   no_gameplay_seen -- the run never started.  A game whose
                #     splashscreen runs inside a normal game context (Ren'Py
                #     reports context=in_game while the studio card plays) and
                #     THEN drops to its title menu looks exactly like an
                #     end-of-run menu return to the shim.  The context-driven
                #     latch below already guards this with _gameplay_seen; the
                #     shim's own game_ended path did not, so every launch
                #     transiently latched game_terminal until the first
                #     in_game context cleared it.  Gate both paths identically.
                suppressed_reason: str | None = None
                if reason == "return_to_menu":
                    if not self.end_on_menu_return:
                        suppressed_reason = "end_on_menu_return"
                    elif not self._gameplay_seen:
                        suppressed_reason = "no_gameplay_seen"
                if suppressed_reason is not None:
                    # Per-game opt-out: a shim-detected menu return is NOT
                    # an ending for this game (Slay the Princess classifies
                    # live gameplay screens as main_menu).  Keep status /
                    # pending / _gameplay_seen untouched — the run is still
                    # going — but record what the screen contained.
                    #
                    # ANNOTATE the game_ended event in place (it was already
                    # appended to the transcript above and is logged below):
                    # the shim genuinely reported a menu return so the
                    # transcript stays honest, but downstream terminal
                    # derivations that key off event PRESENCE (not the
                    # bridge's gated status/game_terminal) must be able to
                    # tell it was ruled non-terminal — otherwise a
                    # harness --auto-end run false-fires when a Slay route
                    # ending returns to the (real) main menu.  Convention:
                    # ABSENCE of these keys means terminal, so old logs and
                    # replays keep reading as endings.
                    event["terminal"] = False
                    event["suppressed"] = suppressed_reason
                    self._emit_terminal_evidence_locked(
                        "game_ended", suppressed=True)
                else:
                    if reason == "return_to_menu":
                        # The menu-return terminal is firing via the shim's
                        # own detection — capture the same evidence the
                        # context latch would (deduped if it already fired).
                        self._emit_terminal_evidence_locked(
                            "game_ended", suppressed=False)
                        # Latch the gated game_terminal verdict too, so an
                        # UNSUPPRESSED menu-return ALWAYS carries game_terminal
                        # (matching the context-driven main_menu latch below).
                        # Downstream consumers can then treat a menu-return as
                        # terminal EXCLUSIVELY via this flag — a status="ended"
                        # whose end_reason is "return_to_menu" is not trusted on
                        # its own (a partially-updated / stale bridge could
                        # report it without a real ending).
                        self.current_game_terminal = True
                    self.status = "ended"
                    self.end_reason = reason
                    self.pending_request = None
                    self.pending_action = None
                    self._gameplay_seen = False
            elif etype in ("choice_resolved", "input_resolved"):
                req_id = event.get("request_id")
                if (
                    self.pending_request is not None
                    and req_id is not None
                    and self.pending_request.get("id") == req_id
                ):
                    request = self.pending_request
                    self.pending_request = None
                    self.pending_action = None
                    if self.status == "waiting_for_input":
                        self.status = "running"
                    entry = self.requests_by_id.setdefault(req_id, {
                        "request": request,
                        "resolution": None,
                        "submitted_action": None,
                    })
                    entry["request"] = request
                    entry["resolution"] = event
            elif (etype == "command_result"
                  and event.get("command") == "load"
                  and event.get("success") is False):
                self._load_consumed_at = None
            elif etype == "context":
                self.current_context = event
                context = event.get("context")
                # Reset ended status when game enters gameplay again
                # (e.g. player starts a new playthrough from main menu).
                if context == "in_game":
                    # NB: do NOT mark _gameplay_seen on the bare in_game
                    # context — a launch splashscreen reports it too (before
                    # any play). Gameplay is "seen" only once real story
                    # content (narration/dialogue) or a choice appears (see
                    # below / set_pending_request), so a splash->menu launch
                    # flow doesn't falsely latch the return-to-menu terminal.
                    # Gameplay context is confirmation enough to resume
                    # capturing live progress after a game_started restart.
                    self._progress_capture_suspended = False
                    self._menu_evidence_emitted = False
                    if self.status == "ended":
                        self.status = "running"
                        self.end_reason = None
                        # A fresh playthrough began from the menu — clear the
                        # latched terminal flag so the new run isn't reported as
                        # already ended (return_to_menu latches it below).
                        self._clear_terminal_latch_locked()
                elif context == "main_menu" and self._gameplay_seen and self.status != "ended":
                    if not self.end_on_menu_return:
                        # Per-game opt-out: this game's menu-classified
                        # screens appear during live gameplay, so a menu
                        # return is not an ending.  Leave status/pending/
                        # _gameplay_seen untouched (a later opt-in flip
                        # can still latch from the same state) and record
                        # the evidence for designing the real fix.
                        self._emit_terminal_evidence_locked(
                            "context", suppressed=True)
                    else:
                        self._emit_terminal_evidence_locked(
                            "context", suppressed=False)
                        self.status = "ended"
                        self.end_reason = "return_to_menu"
                        # Returning to the main menu AFTER gameplay is a
                        # playthrough-terminal state. Latch game_terminal so games
                        # WITHOUT a progress mod still surface it in act/wait results
                        # (the harness latches terminal_reached from there) — else the
                        # run idles at the menu until its duration cap. Generic across
                        # Ren'Py VNs; gated on _gameplay_seen so the launch-time menu
                        # (before any play) never trips it.
                        self.current_game_terminal = True
                        self.pending_request = None
                        self.pending_action = None
                        self._gameplay_seen = False
                        self.event_counter += 1
                        ended_event = {
                            "type": "game_ended",
                            "reason": "return_to_menu",
                            "timestamp": time.time(),
                            "_seq": self.event_counter,
                        }
                        self.transcript.append(ended_event)
                        self._log_event(ended_event)
            elif etype == "mod_loaded":
                self.game_pid = event.get("pid")
            elif etype == "anomaly":
                # Sticky anomaly flag. It stays current until the story
                # moves on (see the resolving branch below), so an agent
                # renderer can tell "the game is on its exception screen
                # now" from "something odd happened earlier and play
                # continued" without guessing from age.
                stamped = dict(event)
                stamped["_latched_at"] = time.time()
                stamped["_latched_seq"] = event.get("_seq")
                self.anomaly_flag = stamped

            # Story progress after an anomaly latch: whatever it described
            # is no longer the current situation. Kept out of the type
            # chain above because the lifecycle arms (game_started,
            # game_resumed, game_ended) live there too. The record stays
            # (state/status still expose it for diagnostics) but is marked
            # resolved so renderers and the client stop presenting it as
            # happening now.
            self._resolve_anomaly_latch_locked(etype, event.get("_seq"))

            if etype == "game_ended" and event.get("terminal") is not False:
                settled = self._observe_transaction_settle_locked(ended=True)
                if settled is not None:
                    transaction_snapshot = dict(settled)
            elif etype == "game_state" and self._active_action_nonce:
                interactions = event.get("interactions") or event.get("screen_buttons")
                if interactions:
                    settled = self._observe_transaction_settle_locked(screen=event)
                    if settled is not None:
                        transaction_snapshot = dict(settled)

            # Real story content means the playthrough is genuinely underway.
            # Gate the return-to-menu terminal latch on this (plus choices via
            # set_pending_request) rather than the bare in_game context, so a
            # launch splashscreen -> main-menu flow never trips a false ending.
            if etype in ("narration", "dialogue", "say"):
                if self.status == "ended" and self._recent_load_consumed_locked():
                    # A programmatic Ren'Py load from the main menu can restore
                    # story content without clearing store.main_menu or
                    # returning through the command handler.  The new story is
                    # stronger evidence than that stale menu context: resume
                    # the run so auto-end does not immediately kill the next
                    # episode.
                    self.status = "running"
                    self.end_reason = None
                    self._clear_terminal_latch_locked()
                    self.current_context = {
                        "type": "context",
                        "context": "in_game",
                        "available_commands": ["save", "load", "quit"],
                        "inferred": "story_after_terminal",
                    }
                    self._load_consumed_at = None
                self._gameplay_seen = True
                self._progress_capture_suspended = False
                # Story content means the menu-return episode (if any) is
                # over — let the next one emit fresh terminal evidence.
                self._menu_evidence_emitted = False
            # Auto-clear stale pending: if the game is pushing story
            # events (narration, dialogue) but the pending_request is
            # still set with no pending_action, the choice was already
            # resolved by the shim — the resolve notification just
            # didn't make it through (Python 2.7 urllib2 timeouts).
            # Grace period: async events (auto_skipped) can arrive out
            # of order with synchronous push_request calls.  Don't
            # clear a request that was just set (< 2s ago).
            if (etype in ("narration", "dialogue", "auto_skipped")
                    and self.pending_request is not None
                    and self.pending_action is None
                    and time.time() - self.pending_request.get("_set_at", 0) > 2.0):
                self.pending_request = None
                if self.status == "waiting_for_input":
                    self.status = "running"

            if (
                surface_before is not None
                and self._actionable_surface_signature_locked() != surface_before
            ):
                canceled_transaction_snapshots.extend(
                    self._cancel_stale_queued_acts_locked()
                )

            if (etype == "command_result"
                    and event.get("command") != "act"):
                result_nonce = event.get("nonce")
                if isinstance(result_nonce, str) and result_nonce:
                    self._delivered_command_result_nonces[result_nonce] = None
                    while (len(self._delivered_command_result_nonces)
                           > self._MAX_COMMAND_RESULT_NONCES):
                        self._delivered_command_result_nonces.popitem(last=False)

            if source_key is not None:
                self._delivered_source_events[source_key] = None
                while (len(self._delivered_source_events)
                       > self._MAX_SOURCE_EVENTS):
                    self._delivered_source_events.popitem(last=False)

            self._log_event(event)
            self._trim_memory_locked()
            result = self.event_counter
        # Write queued lines to disk with the state lock released.
        self._flush_log()
        if finalized_snapshot is not None:
            self._persist_transaction_snapshot(finalized_snapshot)
        if transaction_event is not None:
            self._persist_transaction_event(*transaction_event)
        if transaction_snapshot is not None:
            self._persist_transaction_snapshot(transaction_snapshot)
        for canceled_snapshot in canceled_transaction_snapshots:
            self._persist_transaction_snapshot(canceled_snapshot)
        return result

    def _resolve_anomaly_latch_locked(self, etype: str, seq: Any) -> None:
        """Mark the anomaly latch resolved on story progress.

        Called from push_event for streamed story events and from
        set_pending_request for the menu/input requests the shim delivers
        through /request (they never pass through push_event). Caller
        holds the lock.
        """
        if (
            etype in _ANOMALY_RESOLVING_EVENTS
            and self.anomaly_flag is not None
            and "_resolved_at" not in self.anomaly_flag
        ):
            resolved = dict(self.anomaly_flag)
            resolved["_resolved_at"] = time.time()
            resolved["_resolved_by"] = etype
            resolved["_resolved_seq"] = seq
            self.anomaly_flag = resolved

    def set_pending_request(self, request: dict) -> bool:
        """Set a new pending request (choice / input).
        If a request with the same ID already exists, this is an enrichment
        update — replace in place without a new transcript entry."""
        transaction_snapshot = None
        canceled_transaction_snapshots: list[dict[str, Any]] = []
        transaction_event: tuple[str, dict[str, Any]] | None = None
        finalized_snapshot = None
        with self._lock:
            if self._closed:
                return False
            # See push_event: the actionable surface before this request lands.
            surface_before = self._queued_act_surface_signature_locked()
            # Same opportunistic lifecycle progress as push_event: a fresh
            # decision point is a settle observation, while idle fallback must
            # not depend on somebody polling /transaction.
            finalized_snapshot = self._finalize_active_transaction_if_quiet_locked()
            req_id = request.get("id")
            is_enrichment = (req_id and self.pending_request
                             and self.pending_request.get("id") == req_id)

            request["_set_at"] = time.time()
            self.pending_request = request
            if not is_enrichment:
                # A new menu or input prompt is the script running again:
                # a latched exception/anomaly no longer describes now.
                self._resolve_anomaly_latch_locked(
                    str(request.get("type") or "choice_request"),
                    request.get("_seq", self.event_counter),
                )
            if ((self.status == "ended" or self.current_game_terminal)
                    and self._recent_load_consumed_locked()):
                # Programmatic loads can deliver the restored dialogue either
                # side of a stale main-menu ending event. An actionable choice
                # or input request proves the episode is active regardless of
                # that ordering.
                self.status = "running"
                self.end_reason = None
                self._clear_terminal_latch_locked()
                self.current_context = {
                    "type": "context",
                    "context": "in_game",
                    "available_commands": ["save", "load", "quit"],
                    "inferred": "request_after_terminal",
                }
                self._load_consumed_at = None
            self._gameplay_seen = True
            self._progress_capture_suspended = False
            self._menu_evidence_emitted = False
            if not is_enrichment:
                if self.pending_action is not None:
                    # An accepted-but-unconsumed action is being discarded
                    # because the shim moved on to a NEW request.  The
                    # submitter already received "Action submitted." — make
                    # the loss visible instead of silently dropping it.
                    dropped = self.pending_action
                    self.event_counter += 1
                    superseded = {
                        "type": "action_superseded",
                        "request_id": dropped.get("request_id"),
                        "new_request_id": req_id,
                        "action_type": dropped.get("type"),
                        "text": (
                            "A submitted action was discarded before the game "
                            "consumed it (a new interaction replaced request "
                            "{!r}). Re-check the current state and act again."
                            .format(dropped.get("request_id"))
                        ),
                        "timestamp": time.time(),
                        "_seq": self.event_counter,
                    }
                    self.transcript.append(superseded)
                    self._log_event(superseded)
                self.pending_action = None
                self.status = "waiting_for_input"
                self.event_counter += 1
                request["_seq"] = self.event_counter
                (
                    event_nonce,
                    gate_revoked,
                ) = self._record_transaction_event_locked(request)
                if event_nonce:
                    transaction_event = (event_nonce, dict(request))
                    if gate_revoked:
                        transaction_snapshot = dict(
                            self._act_transactions[event_nonce]
                        )
                self.transcript.append(request)
                self._log_event(request)
                if req_id:
                    self.requests_by_id[req_id] = {
                        "request": request,
                        "resolution": None,
                        "submitted_action": None,
                    }
            elif req_id and req_id in self.requests_by_id:
                self.requests_by_id[req_id]["request"] = request
            if not is_enrichment:
                settled = self._observe_transaction_settle_locked(pending=request)
                if settled is not None:
                    transaction_snapshot = dict(settled)
            if (
                surface_before is not None
                and self._actionable_surface_signature_locked() != surface_before
            ):
                canceled_transaction_snapshots.extend(
                    self._cancel_stale_queued_acts_locked()
                )
            self._trim_memory_locked()
        self._flush_log()
        if finalized_snapshot is not None:
            self._persist_transaction_snapshot(finalized_snapshot)
        if transaction_event is not None:
            self._persist_transaction_event(*transaction_event)
        if transaction_snapshot is not None:
            self._persist_transaction_snapshot(transaction_snapshot)
        for canceled_snapshot in canceled_transaction_snapshots:
            self._persist_transaction_snapshot(canceled_snapshot)
        return True

    def _retire_request_ended_by_story_entry_locked(
        self, record: dict, event: dict,
    ) -> None:
        """Drop the menu a story-entering screen button just killed.

        A screen button whose click runs script (a map destination, Start)
        ends the Ren'Py interaction that was showing the menu, so the
        choice_request that was pending when the act was ACCEPTED is dead
        the moment the shim reports the click applied.  Nothing else clears
        it in time: no choice_resolved arrives (it was not a choice), and the
        narration auto-clear only fires on the arrival prose -- which is the
        same event the act settles on -- so a state() between the click and
        the successor menu rendered the old village list under the new
        location (rw70-opus-r: Gale Rocks menu over a Creeks footer, acts
        1971/1995/2136).

        Keyed on identity, not time: only the request the record captured at
        acceptance (``initial_request_id``) is retired.  A request that
        registered after acceptance carries a different id and survives, as
        does anything with an accepted-but-unconsumed action.
        """
        if event.get("resolved_as") != "button":
            return
        if not (event.get("story_entry") or event.get("_story_entry")):
            return
        pending = self.pending_request
        if pending is None or self.pending_action is not None:
            return
        initial_id = record.get("initial_request_id")
        if not initial_id or pending.get("id") != initial_id:
            return
        self.pending_request = None
        if self.status == "waiting_for_input":
            self.status = "running"
        self._log_event({
            "type": "pending_request_retired",
            "request_id": initial_id,
            "action_nonce": record.get("action_nonce"),
            "reason": "story_entry_button",
            "timestamp": time.time(),
            "_seq": self.event_counter,
        })

    def get_pending_request(self) -> dict | None:
        with self._lock:
            if self.pending_action is not None:
                return None
            return self.pending_request

    def submit_action(self, action: dict) -> tuple[bool, str]:
        """
        External client submits an action.
        Returns (success, message).
        """
        with self._lock:
            if self._closed:
                return False, "Slot is closed."
            submitted_id = action.get("request_id")

            # First, check if we have a pending request
            if self.pending_request is not None:
                # Validate: if it's a choice, check index bounds.
                req_type = self.pending_request.get("type", "")
                action_type = action.get("type", "")

                if req_type == "choice_request":
                    if action_type != "act":
                        return (
                            False,
                            f"Invalid action type '{action_type}' for a choice request (expected 'act').",
                        )

                    choices = self.pending_request.get("choices", [])
                    index = action.get("index")
                    if index is None:
                        # Try to parse from "value" field
                        index = action.get("value")

                    # Resolve string/int IDs when choices are enriched dicts.
                    if choices and isinstance(choices[0], dict):
                        # Enriched format: [{id, label}, ...]
                        resolved_idx = None
                        for ci, ch in enumerate(choices, 1):
                            ch_id = ch.get("id")
                            if ch_id == index:
                                resolved_idx = ci
                                break
                            # Case-insensitive string match.
                            if isinstance(index, str) and isinstance(ch_id, str):
                                if ch_id.lower() == index.lower():
                                    resolved_idx = ci
                                    break
                        # Fallback: accept 1-based positional index.
                        if resolved_idx is None and isinstance(index, int):
                            if 1 <= index <= len(choices):
                                resolved_idx = index
                        if resolved_idx is None:
                            available_ids = [ch.get("id") for ch in choices]
                            return (
                                False,
                                f"Unknown choice ID: {index!r}. Available: {available_ids}",
                            )
                        index = resolved_idx
                    else:
                        # Legacy flat list: index must be integer.
                        if not isinstance(index, int) or index < 1 or index > len(choices):
                            return (
                                False,
                                f"Invalid choice index: {index}. Must be 1..{len(choices)}.",
                            )
                    action["index"] = index

                elif req_type == "input_request":
                    if action_type != "input":
                        return (
                            False,
                            f"Invalid action type '{action_type}' for an input request (expected 'input').",
                        )

                    text = action.get("text", action.get("value", ""))
                    action["text"] = str(text)

                # If request_id is provided, validate it matches
                if (
                    submitted_id is not None
                    and submitted_id != self.pending_request.get("id")
                ):
                    return (
                        False,
                        f"Stale request ID: {submitted_id}. Current request ID: {self.pending_request.get('id')}",
                    )

                # Compare only after enriched choice IDs and input values are
                # canonicalized. The stored pending action uses that canonical
                # shape, so a lost-response retry with the same wire ID must
                # not look different merely because its index was a string.
                if (
                    self.pending_action is not None
                    and submitted_id is not None
                    and submitted_id == self.pending_action.get("request_id")
                ):
                    prior = self.pending_action
                    if action_type == "input":
                        same = (
                            prior.get("type") == "input"
                            and str(prior.get("text", ""))
                            == action.get("text", "")
                        )
                    else:
                        same = (
                            prior.get("type") == action_type
                            and prior.get("index", prior.get("value"))
                            == action.get("index", action.get("value"))
                        )
                    if same:
                        return True, "Action already submitted."
                    return False, (
                        "Request already has a different accepted action; "
                        "retry with the identical payload."
                    )

                action["request_id"] = self.pending_request.get("id")
                action["submitted_at"] = time.time()
                self.pending_action = action

                # Track in requests_by_id if we have an ID
                if submitted_id:
                    self.requests_by_id[submitted_id]["submitted_action"] = action

                return True, "Action submitted."

            # If no pending request, check if we already resolved this request
            elif submitted_id and submitted_id in self.requests_by_id:
                existing = self.requests_by_id[submitted_id]
                # Check if already resolved
                if existing.get("resolution"):
                    prior = existing.get("submitted_action")
                    if prior is not None:
                        action_type = action.get("type", "")
                        if action_type == "input":
                            same = (
                                prior.get("type") == "input"
                                and str(prior.get("text", "")) == str(
                                    action.get("text", action.get("value", ""))
                                )
                            )
                        else:
                            incoming_index = action.get(
                                "index", action.get("value"),
                            )
                            stored_request = existing.get("request")
                            choices = (
                                stored_request.get("choices", [])
                                if isinstance(stored_request, dict) else []
                            )
                            if choices and isinstance(choices[0], dict):
                                for ci, choice in enumerate(choices, 1):
                                    choice_id = choice.get("id")
                                    if choice_id == incoming_index or (
                                        isinstance(choice_id, str)
                                        and isinstance(incoming_index, str)
                                        and choice_id.lower()
                                        == incoming_index.lower()
                                    ):
                                        incoming_index = ci
                                        break
                            same = (
                                prior.get("type") == action_type
                                and prior.get("index", prior.get("value"))
                                == incoming_index
                            )
                        if not same:
                            return False, (
                                "Request already resolved with a different "
                                "action; retry with the identical payload."
                            )
                    # A user-resolved request has no submitted_action to bind;
                    # retain the hybrid-mode idempotent acknowledgement.
                    return True, f"Request {submitted_id} already resolved."
                # Otherwise, this is a stale request_id with no resolution yet
                return (
                    False,
                    f"Request {submitted_id} was not found (no pending or resolved request).",
                )

            # No pending request and not in requests_by_id
            return False, "No pending request."

    def consume_action(self, request_id: str | None = None) -> dict | None:
        """
        Ren'Py mod consumes the pending action.
        Returns the action if available (and clears it), else None.
        """
        with self._lock:
            if self._closed:
                return None
            if self.pending_action is None:
                return None

            # If request_id is specified, only return if it matches.
            if request_id and self.pending_action.get("request_id") != request_id:
                return None

            action = self.pending_action
            self.pending_action = None
            req_id = action.get("request_id")
            if (
                self.pending_request is not None
                and (req_id is None or self.pending_request.get("id") == req_id)
            ):
                request = self.pending_request
                self.pending_request = None
                if self.status == "waiting_for_input":
                    self.status = "running"
                if req_id:
                    entry = self.requests_by_id.setdefault(req_id, {
                        "request": request,
                        "resolution": None,
                        "submitted_action": None,
                    })
                    entry["request"] = request
                    entry["resolution"] = action
                    entry["submitted_action"] = action
            return action

    def submit_inventory_change(self, changes: dict) -> tuple[bool, str]:
        """
        External client submits inventory modifications.
        Returns (success, message).

        Expected format:
        {
            "changes": [
                {"type": "add", "item": "health_potion", "quantity": 1},
                {"type": "remove", "item": "wood", "quantity": 5},
                {"type": "modify", "item": "gold", "new_quantity": 150}
            ],
            "request_id": "abc123",  // optional, for hybrid mode
            "inventory_version": 5   // optional, for optimistic locking
        }
        """
        with self._lock:
            if self._closed:
                return False, "Slot is closed."
            requested_version = changes.get("inventory_version")
            # Optimistic locking: reject if version doesn't match
            if (
                requested_version is not None
                and requested_version != self.inventory_version
            ):
                return (
                    False,
                    f"Stale inventory state. Expected version {self.inventory_version}, got {requested_version}.",
                )

            # Validate changes
            change_list = changes.get("changes", [])
            if not isinstance(change_list, list):
                return False, "'changes' must be a list."

            for change in change_list:
                change_type = change.get("type")
                if change_type not in ("add", "remove", "modify"):
                    return (
                        False,
                        f"Invalid change type: {change_type}. Must be 'add', 'remove', or 'modify'.",
                    )
                if "item" not in change:
                    return False, "Each change must have 'item' field."

            # Apply changes to current_inventory (for tracking)
            try:
                for change in change_list:
                    item_name = change["item"]
                    if change["type"] == "add":
                        # Find existing item or create new
                        existing = next(
                            (
                                i
                                for i in self.current_inventory
                                if i.get("name") == item_name
                            ),
                            None,
                        )
                        if existing:
                            existing["quantity"] = existing.get(
                                "quantity", 1
                            ) + change.get("quantity", 1)
                        else:
                            self.current_inventory.append(
                                {
                                    "name": item_name,
                                    "quantity": change.get("quantity", 1),
                                }
                            )
                    elif change["type"] == "remove":
                        existing = next(
                            (
                                i
                                for i in self.current_inventory
                                if i.get("name") == item_name
                            ),
                            None,
                        )
                        if existing:
                            existing["quantity"] = max(
                                0,
                                existing.get("quantity", 1) - change.get("quantity", 1),
                            )
                            if existing["quantity"] == 0:
                                self.current_inventory.remove(existing)
                        else:
                            return False, f"Item '{item_name}' not found in inventory."
                    elif change["type"] == "modify":
                        existing = next(
                            (
                                i
                                for i in self.current_inventory
                                if i.get("name") == item_name
                            ),
                            None,
                        )
                        if existing:
                            existing["quantity"] = change.get("new_quantity", 1)
                        else:
                            return False, f"Item '{item_name}' not found in inventory."

                # Increment version
                self.inventory_version += 1
                changes["applied_version"] = self.inventory_version

                # Push inventory update event
                self.event_counter += 1
                self.transcript.append(
                    {
                        "_seq": self.event_counter,
                        "type": "inventory_update",
                        "inventory": self.current_inventory,
                        "version": self.inventory_version,
                        "submitted_by": "external",
                        "changes": change_list,
                    }
                )
                self._log_event(self.transcript[-1])

                # Store for request_id lookup if provided
                req_id = changes.get("request_id")
                if req_id:
                    if req_id not in self.requests_by_id:
                        self.requests_by_id[req_id] = {
                            "request": None,
                            "resolution": None,
                            "submitted_action": None,
                        }
                    self.requests_by_id[req_id]["inventory_changes"] = changes

                _result = (True, f"Inventory updated (version {self.inventory_version}).")

            except Exception as e:
                _result = (False, f"Error applying inventory changes: {str(e)}")
        # Write the queued inventory_update line with the state lock released.
        self._flush_log()
        return _result

    # Maximum queued commands awaiting shim consumption.  The shim polls
    # GET /command several times a second, so a small bound is plenty;
    # the bound exists so a wedged game surfaces "queue full" errors to
    # callers instead of accumulating stale save/load/act commands.
    _MAX_PENDING_COMMANDS: int = 8

    # How many recently-accepted command nonces to remember for idempotent
    # replay dedup.  Bounded so a long run can't grow the map without limit;
    # a retry always fires within seconds of the original, so a small window
    # is plenty.
    _MAX_COMMAND_NONCES: int = 32
    _MAX_COMMAND_RESULT_NONCES: int = 64
    _MAX_SOURCE_EVENTS: int = 256

    @property
    def pending_command(self) -> dict | None:
        """Backward-compat view of the head of the command queue."""
        return self.pending_commands[0] if self.pending_commands else None

    def submit_command(self, command: dict) -> tuple[bool, str]:
        """Submit a legacy command, serialized with transactional acceptance."""
        with self._act_submit_lock:
            return self._submit_command_serialized(command)

    def _submit_command_serialized(self, command: dict) -> tuple[bool, str]:
        """
        External client submits a game command (start, save, load, rollback, quit).
        Returns (success, message).

        Commands are queued FIFO (bounded).  When the queue is full the
        submission is REJECTED so the caller knows the command was not
        accepted — the old behaviour overwrote the previous command,
        whose caller had already received a success response.
        """
        with self._lock:
            if self._closed:
                return False, "Slot is closed."
            cmd_name = command.get("name", "")
            if not cmd_name:
                return False, "Missing 'name' field in command."
            # Idempotency: a command carrying a nonce we already accepted is a
            # transport-timeout replay (the first POST enqueued fine but its
            # HTTP response was lost, so the client re-sent the SAME logical
            # command).  Return the ORIGINAL success ack without enqueuing
            # again — enqueuing a duplicate would double-apply the act once the
            # shim polls.  Nonce-less commands skip this entirely.
            nonce = command.get("nonce")
            if nonce:
                prior_record = self._recent_command_nonces.get(nonce)
                if prior_record is not None:
                    self._recent_command_nonces.move_to_end(nonce)
                    signature = json.dumps(
                        {
                            key: value for key, value in command.items()
                            if key != "nonce"
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    )
                    if signature != prior_record.get("signature"):
                        return False, (
                            "Command nonce was already used for a different "
                            "command or argument set."
                        )
                    prior = tuple(prior_record["result"])
                    if any(
                        queued.get("nonce") == nonce
                        for queued in self.pending_commands
                    ):
                        return prior
                    # The original left the bridge queue, but its result may
                    # have been lost in transit. Requeue the same nonce so the
                    # shim can replay its bounded command-result cache. The
                    # shim checks the nonce before executing the command, so
                    # this recovers a receipt without double-applying it.
                    if len(self.pending_commands) >= self._MAX_PENDING_COMMANDS:
                        return False, (
                            "Command queue is full; the accepted command "
                            "result cannot be replayed yet. Retry with the "
                            "same nonce once the game catches up."
                        )
                    self.pending_commands.append(command)
                    return prior
            if len(self.pending_commands) >= self._MAX_PENDING_COMMANDS:
                queued = [c.get("name", "?") for c in self.pending_commands]
                # A full queue is retryable — do NOT record the nonce, so a
                # later retry with the same nonce can still enqueue once the
                # shim drains the backlog.
                return (
                    False,
                    "Command queue is full ({} pending: {}). The game is not "
                    "consuming commands; retry once it catches up.".format(
                        len(queued), ", ".join(queued)
                    ),
                )
            self.pending_commands.append(command)
            result = (True, f"Command '{cmd_name}' submitted.")
            if nonce:
                signature = json.dumps(
                    {
                        key: value for key, value in command.items()
                        if key != "nonce"
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )
                self._recent_command_nonces[nonce] = {
                    "signature": signature,
                    "result": result,
                }
                while len(self._recent_command_nonces) > self._MAX_COMMAND_NONCES:
                    self._recent_command_nonces.popitem(last=False)
            return result

    def submit_command_with_ack(
        self, command: dict,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Submit a command, returning transaction metadata for modern acts."""
        if (
            command.get("name") != "act"
            or not command.get("nonce")
            or "reset_generation" not in command
        ):
            success, message = self.submit_command(command)
            return success, message, {}

        nonce = str(command["nonce"])
        # Acceptance is serialized on its OWN lock so the durable write can
        # happen with the state lock released (July's act-stall saga: never
        # flush under the state lock) while staying a single critical section.
        with self._act_submit_lock:
            return self._accept_act_transaction(command, nonce)

    def _accept_act_transaction(
        self, command: dict, nonce: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        lifecycle_snapshots: list[dict[str, Any]] = []
        invocation = self._sanitize_act_invocation(
            command.get("_invocation"))
        shim_command = dict(command)
        shim_command.pop("_invocation", None)
        # Deduplication must survive the registry bound: a retry of a nonce
        # whose record was spilled to the journal has to return the ORIGINAL
        # transaction, not mint a second action_id and apply the choice twice.
        # This is a set-membership check for every ordinary act; only a real
        # spilled-nonce retry pays for the journal read.
        try:
            self._rehydrate_transaction_from_journal(nonce)
        except TransactionJournalError:
            return False, "Could not read prior action transaction.", {
                "action_nonce": nonce,
                "transaction_state": "acceptance_unknown",
                "pending": True,
                "reason": "storage_error",
            }
        # A prior attempt may have made a terminal correction in memory after
        # its durable acceptance (for example, a stale-surface rejection) but
        # failed to append that correction. Do not deduplicate against memory
        # until disk has caught up, or a restart could still apply an action
        # whose retry was told it had failed.
        if not self._flush_transaction_snapshots():
            return False, "Could not durably record prior action state.", {
                "action_nonce": nonce,
                "transaction_state": "acceptance_unknown",
                "pending": True,
                "reason": "persistence_error",
            }
        try:
            supplied_generation = int(command.get("reset_generation"))
        except (TypeError, ValueError):
            return False, "Transactional act requires reset_generation.", {
                "action_nonce": nonce,
                "transaction_state": "rejected",
                "pending": False,
                "reason": "missing_generation",
            }
        with self._lock:
            if self._closed:
                return False, "Slot is closed.", {
                    "action_nonce": nonce,
                    "transaction_state": "rejected",
                    "pending": False,
                    "reason": "slot_closed",
                }
            if supplied_generation != self.reset_generation:
                return False, "Stale reset generation.", {
                    "action_nonce": nonce,
                    "transaction_state": "rejected",
                    "pending": False,
                    "reason": "stale_generation",
                    "reset_generation": supplied_generation,
                    "current_reset_generation": self.reset_generation,
                }
            prior = self._act_transactions.get(nonce)
            if prior is not None:
                prior_view = self._transaction_view_locked(
                    prior, deduplicated=True)
                if prior.get("transaction_state") in {"failed", "rejected"}:
                    return False, str(
                        prior.get("error") or "Action was not accepted."
                    ), prior_view
                return True, "Action already accepted.", prior_view
            # Never block on a transaction that only LOOKS in flight: settle
            # what has gone quiet, and expire leases the shim never answered.
            finalized = self._finalize_active_transaction_if_quiet_locked()
            if finalized is not None:
                lifecycle_snapshots.append(finalized)
            lifecycle_snapshots.extend(self._expire_stale_transactions_locked())

        # Lifecycle transitions performed while deciding whether the channel
        # is free must be durable even when this submission is later rejected
        # (for example because the legacy command queue is full). Keep failed
        # snapshots for the next submission attempt.
        for lifecycle_snapshot in lifecycle_snapshots:
            if not self._persist_transaction_snapshot(lifecycle_snapshot):
                return False, "Could not durably record prior action state.", {
                    "action_nonce": nonce,
                    "transaction_state": "rejected",
                    "pending": False,
                    "reason": "persistence_error",
                }
        if not self._flush_transaction_snapshots():
            return False, "Could not durably record prior action state.", {
                "action_nonce": nonce,
                "transaction_state": "rejected",
                "pending": False,
                "reason": "persistence_error",
            }

        with self._lock:
            # Reset/free and every command submission share _act_submit_lock,
            # but event ingestion can still settle a transaction while the
            # journal write runs. Re-read authoritative state before building
            # and persisting the new acceptance.
            if self._closed:
                return False, "Slot is closed.", {
                    "action_nonce": nonce,
                    "transaction_state": "rejected",
                    "pending": False,
                    "reason": "slot_closed",
                }
            if supplied_generation != self.reset_generation:
                return False, "Stale reset generation.", {
                    "action_nonce": nonce,
                    "transaction_state": "rejected",
                    "pending": False,
                    "reason": "stale_generation",
                    "reset_generation": supplied_generation,
                    "current_reset_generation": self.reset_generation,
                }
            prior = self._act_transactions.get(nonce)
            if prior is not None:
                prior_view = self._transaction_view_locked(
                    prior, deduplicated=True)
                if prior.get("transaction_state") in {"failed", "rejected"}:
                    return False, str(
                        prior.get("error") or "Action was not accepted."
                    ), prior_view
                return True, "Action already accepted.", prior_view
            in_flight = next((
                record for record in self._act_transactions.values()
                if int(record.get("reset_generation", -1)) == self.reset_generation
                and self._transaction_holds_admission_locked(record)
            ), None)
            if in_flight is not None:
                retry_after = None
                if in_flight.get("transaction_state") == "applied":
                    hold_until = self._admission_hold_expiry_locked(in_flight)
                    if hold_until is not None:
                        retry_after = max(0.0, hold_until - time.time())
                return False, "Another action is still in flight.", {
                    "action_nonce": nonce,
                    "transaction_state": "rejected",
                    "pending": False,
                    "reason": "action_in_flight",
                    "blocking_action_id": in_flight.get("action_id"),
                    "blocking_transaction_state": in_flight.get(
                        "transaction_state"),
                    # When the blocker is applied and its outcome has been
                    # observed, the bridge knows EXACTLY when it stops holding
                    # the gate.  Saying so lets the client come back at that
                    # instant instead of polling blind (and lets it stop
                    # polling a blocker that cannot open inside its budget).
                    # Absent when the answer is "not on a clock" — an accepted
                    # act, or an applied one that has produced nothing yet.
                    **({"admission_retry_after": round(retry_after, 3)}
                       if retry_after is not None else {}),
                    # The blocker's nonce, so a client can POLL it (GET
                    # /transaction is keyed by nonce and nothing else) instead
                    # of re-POSTing the whole act to discover whether the
                    # channel freed up.  Nothing is recorded under the REJECTED
                    # nonce here: the rejection is synthesized, never stored
                    # and never journalled, so this exact submission stays
                    # replayable and idempotent.
                    "blocking_action_nonce": in_flight.get("action_nonce"),
                }
            # Capture every live transaction this admission steps over, and
            # the interaction surface the decision relies on.  Event ingestion
            # continues while acceptance is written off-lock, so all of it must
            # still be authoritative at commit.
            admitted_over = [
                str(other.get("action_nonce") or "")
                for other in self._act_transactions.values()
                if other.get("transaction_state") == "applied"
                and int(other.get("reset_generation", -1)) == self.reset_generation
            ]
            if len(self.pending_commands) >= self._MAX_PENDING_COMMANDS:
                return False, "Command queue is full; retry once the game catches up.", {
                    "action_nonce": nonce,
                    "transaction_state": "rejected",
                    "pending": False,
                    "reason": "queue_full",
                }
            args = command.get("args") or {}
            source_position = next((
                (event.get("_source_id"), event.get("_source_seq"))
                for event in reversed(self.transcript)
                if isinstance(event.get("_source_id"), str)
                and isinstance(event.get("_source_seq"), int)
            ), None)
            record = {
                "action_nonce": nonce,
                "action_id": self._next_action_id,
                "slot_id": self.slot_id,
                "reset_generation": self.reset_generation,
                "submitted_target": args.get("index", args.get("label")),
                "transaction_state": "accepted",
                "initial_request_id": (self.pending_request or {}).get("id"),
                "initial_request_signature": self._action_request_signature(
                    self.pending_request
                ),
                "initial_request_content_signature": (
                    self._actionable_request_content_signature(
                        self.pending_request
                    )
                ),
                "events": [],
                "drain_index": 0,
                "accepted_at": time.time(),
                "command": dict(shim_command),
                **({"invocation": invocation} if invocation else {}),
                "initial_screen_signature": self._action_screen_signature(
                    self.current_game_state
                ),
                "initial_screen_choice_content_signature": (
                    self._choice_screen_content_signature(
                        self.current_game_state)
                ),
                "revision": 1,
            }
            if source_position is not None:
                record["_source_id"], record["_source_seq"] = source_position
            self._next_action_id += 1
            snapshot = dict(record)
        # Acceptance must be on disk before either the shim or a retry can
        # observe this transaction. A persisted-but-not-yet-queued record is
        # safe after a crash: reconstruction queues it exactly once.  The write
        # happens with the state lock RELEASED — _act_submit_lock still makes
        # this a critical section, so no second act can interleave.
        if not self._persist_transaction(snapshot):
            return False, "Could not durably record action acceptance.", {
                "action_nonce": nonce,
                "transaction_state": "rejected",
                "pending": False,
                "reason": "persistence_error",
            }
        with self._lock:
            if self._closed:
                return False, "Slot is closed.", {
                    "action_nonce": nonce,
                    "transaction_state": "rejected",
                    "pending": False,
                    "reason": "slot_closed",
                }
            current_request_id = (self.pending_request or {}).get("id")
            current_request_signature = self._action_request_signature(
                self.pending_request
            )
            current_screen_signature = self._action_screen_signature(
                self.current_game_state
            )
            # Re-run the SAME admission predicate on every prior we stepped
            # over.  An idle release is provisional — resumed output revokes
            # it, and (if that output is the act's first observed outcome) it
            # opens a fresh trailing-attribution hold — so a prior that is
            # blocking again must fail this acceptance rather than interleave
            # with it.  An outcome-grace release, by contrast, is anchored to
            # a fixed instant and cannot flap.
            release_still_valid = not any(
                (
                    (prior_record := self._act_transactions.get(
                        prior_nonce
                    )) is not None
                    and self._transaction_holds_admission_locked(prior_record)
                )
                for prior_nonce in admitted_over
            )
            surface_still_valid = (
                current_request_id == record.get("initial_request_id")
                and current_request_signature
                == record.get("initial_request_signature")
                and current_screen_signature
                == record.get("initial_screen_signature")
            )
            if not release_still_valid or not surface_still_valid:
                record["transaction_state"] = "failed"
                record["reason"] = "stale_surface"
                record["error"] = (
                    "The interaction changed while the action was being "
                    "accepted; inspect the current state and act again."
                )
                self._touch_transaction_locked(record)
                self._act_transactions[nonce] = record
                self._evict_transactions_locked()
                rejected_snapshot = dict(record)
                ack = self._transaction_view_locked(record)
            else:
                rejected_snapshot = None
                self._act_transactions[nonce] = record
                self._evict_transactions_locked()
                self.pending_commands.append(shim_command)
                ack = self._transaction_view_locked(record)
        # The accepted snapshot is already durable. If the surface changed in
        # that window, its terminal rejection must be durable before the
        # caller can safely retry with a new nonce.
        if rejected_snapshot is not None:
            if not self._persist_transaction_snapshot(rejected_snapshot):
                return False, "Could not durably reject stale action.", {
                    "action_nonce": nonce,
                    "transaction_state": "acceptance_unknown",
                    "pending": True,
                    "reason": "persistence_error",
                }
            return False, str(record["error"]), ack
        return True, "Command 'act' submitted.", ack

    def _expire_stale_transactions_locked(self) -> list[dict[str, Any]]:
        """Release every transaction that can no longer make progress.

        Two bounds, applied together from the same opportunistic sites
        (``consume_command``, ``get_action_transaction``, the next acceptance):

        * **Lease TTL.** ``consume_command`` leases an act instead of popping
          it, so a lost command_result cannot lose the action.  Without a TTL
          that lease is permanent: the shim re-receives the same act on every
          poll, anything queued behind it is unreachable, and the slot never
          accepts another act.  Expiry fails the transaction with a distinct
          reason and unblocks the channel.
        * **Applied idle gate.** An applied record whose settle was never
          observed is swept by ``_reap_idle_applied_transactions_locked`` so it
          cannot hold the one-in-flight guard forever.  What releases it is
          SILENCE — no event attributed to the transaction — never elapsed
          time since apply, which a long story would trip while still
          playing.  The silence budget is two-tier: ``_ACTION_APPLIED_IDLE_TTL``
          when the shim's command-poll heartbeat is stale or absent, and that
          TTL times ``_ACTION_LIVE_SHIM_IDLE_MULTIPLIER`` when the heartbeat is
          live (a quiet cutscene deserves more rope, but not infinite rope —
          the poll thread outlives a hung main loop).
        """
        expired: list[dict[str, Any]] = self._reap_idle_applied_transactions_locked()
        now = time.time()
        for record in self._act_transactions.values():
            if record.get("transaction_state") != "accepted":
                continue
            if not record.get("dispatched"):
                # Still queued — it WILL be applied; the queue bound covers it.
                continue
            dispatched_at = float(record.get("dispatched_at", 0) or 0)
            if now - dispatched_at < self._ACTION_LEASE_TTL:
                continue
            record["transaction_state"] = "failed"
            record["reason"] = "shim_no_result"
            record["error"] = (
                "The game never reported a result for this action within "
                "{:.0f}s of dispatch; the command lease expired.".format(
                    self._ACTION_LEASE_TTL)
            )
            nonce = record.get("action_nonce")
            if self._active_action_nonce == nonce:
                self._active_action_nonce = None
            self.pending_commands[:] = [
                queued for queued in self.pending_commands
                if queued.get("nonce") != nonce
            ]
            self._touch_transaction_locked(record)
            expired.append(dict(record))
        return expired

    def consume_command(self) -> dict | None:
        """
        Ren'Py mod consumes the oldest pending command.

        Act commands are LEASED rather than popped: the shim dedups by nonce
        and replays its cached command_result, so a lost response on either
        side loses neither the action nor its result.  The lease is bounded on
        both ends — it is not re-served within ``_ACTION_LEASE_RESERVE_DELAY``
        (the shim is presumably still executing it), and it expires entirely
        after ``_ACTION_LEASE_TTL``.

        Ordering stays strict FIFO: a lease blocks the commands behind it.
        ``act`` mutates game state, and a ``save``/``load`` reordered ahead of
        a pending act would snapshot or restore the wrong moment.  The TTL is
        what bounds that head-of-line blocking.

        This is the hot poll path: no disk I/O happens under the state lock.
        """
        pending_snapshots: list[dict[str, Any]] = []
        command = None
        with self._lock:
            if self._closed:
                return None
            self._last_shim_command_poll_at = time.time()
            pending_snapshots.extend(self._expire_stale_transactions_locked())
            if not self.pending_commands:
                command = None
            else:
                command = self.pending_commands[0]
                nonce = command.get("nonce")
                if command.get("name") == "act":
                    prior = self._act_transactions.get(
                        self._active_action_nonce or ""
                    )
                    if (
                        prior is not None
                        and prior.get("action_nonce") != nonce
                        and prior.get("transaction_state") in {
                            "accepted", "applied",
                        }
                    ):
                        # Dispatch is the unambiguous handoff: output from this
                        # point belongs to the new nonce, so settle whatever
                        # the active pointer still refers to before activating
                        # it.  DELIBERATELY unconditional on the gate flag —
                        # this is the last line of defense, and an acceptance
                        # that raced a revocation (or a reconstruction that
                        # rebuilt two live records) must not leave a second
                        # live transaction behind because a flag happened to be
                        # off at this instant.
                        prior["settled_by"] = (
                            "superseded_after_idle"
                            if prior.get("gate_released")
                            or prior.get("gate_released_at")
                            else "superseded_by_next_act"
                        )
                        self._settle_transaction_locked(prior)
                        pending_snapshots.append(dict(prior))
                    self._mark_settled_locked(None)
                if command.get("name") == "act" and nonce in self._act_transactions:
                    record = self._act_transactions[nonce]
                    now = time.time()
                    if record.get("dispatched"):
                        # reserved_at tracks the last actual SERVE (the
                        # re-serve delay); dispatched_at tracks the lease TTL.
                        # They differ after a bridge restart, where the
                        # reconstructed lease must be servable immediately.
                        reserved_at = float(record.get("reserved_at", 0) or 0)
                        if now - reserved_at < self._ACTION_LEASE_RESERVE_DELAY:
                            # Already handed to the shim moments ago; re-serving
                            # it on every poll made the shim's result cache the
                            # only thing preventing double execution.
                            command = None
                        else:
                            record["reserved_at"] = now
                    else:
                        record["dispatched"] = True
                        record["dispatched_at"] = now
                        record["reserved_at"] = now
                        self._touch_transaction_locked(record)
                        pending_snapshots.append(dict(record))
                    if command is not None:
                        self._active_action_nonce = nonce
                else:
                    command = self.pending_commands.pop(0)
                if command is not None and command.get("name") == "load":
                    self._load_consumed_at = time.monotonic()
        # Durability with the state lock released — this path runs several
        # times a second and must never block on disk (act-stall saga).
        for snapshot in pending_snapshots:
            self._persist_transaction_snapshot(snapshot)
        return command

    def _recent_load_consumed_locked(self) -> bool:
        """Whether a load command was consumed recently enough to explain recovery."""
        consumed = self._load_consumed_at
        return consumed is not None and time.monotonic() - consumed <= 30.0

    def get_context(self) -> dict | None:
        """Return the current game context."""
        with self._lock:
            return self.current_context

    def get_state(self, since: int = 0) -> dict:
        """
        Return the full game state, or events since a given sequence number.
        """
        with self._lock:
            if since > 0:
                events = [e for e in self.transcript if e.get("_seq", 0) > since]
            else:
                events = list(self.transcript)

            # Read and clear sticky anomaly flag.
            anomaly = self.anomaly_flag

            visible_pending = None if self.pending_action is not None else self.pending_request
            result = {
                "status": self.status,
                "end_reason": self.end_reason,
                "event_counter": self.event_counter,
                "reset_generation": self.reset_generation,
                "pending_request": visible_pending,
                "has_pending_action": self.pending_action is not None,
                "has_pending_command": bool(self.pending_commands),
                "pending_command_count": len(self.pending_commands),
                "context": self.current_context,
                "gameplay_seen": self._gameplay_seen,
                "transcript": events,
                "config": {
                    "auto_advance": self.auto_advance,
                    "auto_advance_delay": self.auto_advance_delay,
                    "end_on_menu_return": self.end_on_menu_return,
                },
                "inventory": {
                    "current": list(self.current_inventory),
                    "version": self.inventory_version,
                },
            }
            if anomaly:
                result["anomaly"] = anomaly
            if self.current_game_state:
                # Live during play; stats/inventory frozen at the last
                # pre-terminal snapshot once an ending latched.
                result["game_state"] = self._effective_game_state_locked()
                custom_commands = self.current_game_state.get("custom_commands")
                if isinstance(custom_commands, list):
                    result["custom_commands"] = list(custom_commands)
            # ALWAYS surface the verdict, both polarities.  Consumers that
            # mirror it (harness terminal_reached) need to see the bridge
            # UN-latch — game_resumed / a fresh in_game context / a load all
            # clear the latch, and a mirror that only ever sees the True
            # edge stays stuck on a flag the bridge no longer holds.
            result["game_terminal"] = bool(self.current_game_terminal)
            return result

    def get_game_state(self) -> dict | None:
        """Current game_state as served to clients (frozen when terminal)."""
        with self._lock:
            return self._effective_game_state_locked()

    def get_transcript(self, last_n: int | None = None) -> list[dict]:
        with self._lock:
            # ``last=0`` is the explicit wire contract for fetching the
            # complete retained window (used by transcript cursor lookup).
            # Do not leave that behavior dependent on Python's ``-0 == 0``
            # slicing coincidence.
            if last_n is None or last_n == 0:
                return list(self.transcript)
            return list(self.transcript[-last_n:])

    def get_screenshot(self) -> str | None:
        with self._lock:
            return self.latest_screenshot

    def get_screenshot_snapshot(self) -> dict:
        with self._lock:
            return {"screenshot": self.latest_screenshot,
                    "capture_id": self.latest_screenshot_capture_id}

    def reset(self) -> bool:
        """Reset all state for a new game, serialized with act acceptance."""
        with self._act_submit_lock, self._transaction_ack_lock:
            if not self._seal_and_flush_transactions():
                return False
            with self._lock:
                next_generation = self.reset_generation + 1
            # The generation barrier is the reset commit record, and it is
            # written FIRST: once durable, a restart cannot requeue any
            # old-generation action even if a following per-action terminal
            # snapshot hits a transient error.  Because it does not depend on
            # those snapshots, the terminal transitions happen exactly once —
            # in _reset_serialized, which owns both the mutation and its
            # persistence (this method used to do a full duplicate pass on
            # copies and persist every record twice).
            if not self._persist_transaction_meta(
                reset_generation=next_generation,
                next_action_id=self._next_action_id,
            ):
                with self._lock:
                    self._closed = False
                return False
            return self._reset_serialized()

    def _reset_serialized(self) -> bool:
        """Reset all state for a new game (single pass; caller holds the locks)."""
        transaction_snapshots: list[dict[str, Any]] = []
        # Acquire the log write lock BEFORE the state lock — matching
        # _flush_log's established order (_log_write_lock -> _lock, never the
        # reverse) — so a reset can't close/clear the log out from under an
        # in-flight off-lock flush (which would write-after-close and swallow
        # the error).  This blocks until any active flush finishes.
        with self._log_write_lock, self._lock:
            # reset() deliberately sealed the slot before durably recording
            # the next generation and terminal transaction states.
            for record in self._act_transactions.values():
                state = record.get("transaction_state")
                if state == "applied":
                    record["transaction_state"] = "settled"
                    record["settled_by"] = "slot_reset"
                    self._touch_transaction_locked(record)
                    transaction_snapshots.append(dict(record))
                elif state == "accepted":
                    record["transaction_state"] = "failed"
                    record["error"] = "Slot reset before the shim applied the action."
                    self._touch_transaction_locked(record)
                    transaction_snapshots.append(dict(record))
            self._active_action_nonce = None
            self._mark_settled_locked(None)
            self.transcript.clear()
            self._delivered_command_result_nonces.clear()
            self._delivered_source_events.clear()
            self.pending_request = None
            self.pending_action = None
            del self.pending_commands[:]
            self.current_context = None
            self.latest_screenshot = None
            self.latest_screenshot_capture_id = None
            self.current_screen = None
            self._reset_passive_overlay_state_locked()
            self.current_game_state = None
            self._clear_terminal_latch_locked()
            # A brand-new session: the previous run's frozen values are gone
            # for good (the latch clear alone deliberately keeps them).
            self._live_progress = None
            self._progress_capture_suspended = False
            self.status = "idle"
            self.end_reason = None
            self._gameplay_seen = False
            self._menu_evidence_emitted = False
            self.event_counter = 0
            self.reset_generation += 1
            self._load_consumed_at = None
            self.requests_by_id.clear()
            # Runtime configuration belongs to the slot, not to one story
            # generation.  Starting or loading a run resets transcript and
            # request ownership, but must not silently discard a profile the
            # caller applied while the game was at its main menu.
            # Reset inventory versioning
            self.inventory_version = 0
            self.current_inventory = []
            self._closed = False
            # Discard any unflushed lines from the previous session — the
            # transcript/counter are being reset, so stale buffered lines
            # must not leak into the next log file.  We already hold
            # _log_write_lock, so inline the close (don't call _close_log,
            # which would re-acquire the non-reentrant write lock).
            del self._log_buffer[:]
            self._log_buffer_overflow_noted = False
            if self._log_file:
                try:
                    self._log_file.close()
                except Exception:
                    pass
                self._log_file = None
                self._log_path = None
        for snapshot in transaction_snapshots:
            self._persist_transaction_snapshot(snapshot)
        self._persist_transaction_meta()
        return True

    def update_config(self, config: dict[str, Any]) -> dict[str, Any] | None:
        """Apply bridge configuration unless this slot has been closed."""
        with self._lock:
            if self._closed:
                return None
            if "auto_advance" in config:
                self.auto_advance = bool(config["auto_advance"])
            if "auto_advance_delay" in config:
                self.auto_advance_delay = float(config["auto_advance_delay"])
            if "end_on_menu_return" in config:
                self.end_on_menu_return = bool(config["end_on_menu_return"])
            return {
                "auto_advance": self.auto_advance,
                "auto_advance_delay": self.auto_advance_delay,
                "end_on_menu_return": self.end_on_menu_return,
            }


# ---------------------------------------------------------------------------
# Slot Manager
# ---------------------------------------------------------------------------


class SlotManager:
    """Manages multiple concurrent game slots, each with its own GameState."""

    _MAX_TRANSACTION_ARCHIVES = 128
    _MAX_REGISTRATION_REJECTIONS = 256
    _MAX_REGISTRATION_REJECTIONS_PER_TOKEN = 16
    _REGISTRATION_REJECTION_TTL = 300.0

    def __init__(self, max_slots: int = 16, admin_token: str | None = None,
                 require_token: bool = False, storage_dir: str | None = None,
                 load_archives: bool = True) -> None:
        self._storage_dir = os.path.abspath(storage_dir or os.path.join("bridge", "logs"))
        self.max_slots = max_slots
        self.slots: dict[int, GameState] = {}
        self.game_to_slot: dict[str, int] = {}
        self.slot_to_game: dict[int, str] = {}
        self.transaction_archives: dict[int, GameState] = {}
        self.archived_slot_to_game: dict[int, str] = {}
        self._next_slot = 1
        self._lock = threading.Lock()
        # Slots whose teardown is in progress. free_with_status releases the
        # manager lock across per-slot disk I/O; this keeps that release
        # exclusive without holding the lock for the writes.
        self._freeing: set[int] = set()
        # Token management.
        self.admin_token: str = admin_token or secrets.token_urlsafe(16)
        self.require_token: bool = require_token
        self._slot_tokens: dict[int, str] = {}  # slot_id -> token
        self._token_to_slot: dict[str, int] = {}  # token -> slot_id
        self._archived_token_hashes: dict[int, str] = {}
        self._archive_cleanup_backlog: set[str] = set()
        self._unavailable_archive_paths: set[str] = set()
        # Failed slot assignments are not slots and their tokens must never
        # become bridge credentials. Keep only a bounded, short-lived record
        # keyed by the token hash so the owning launcher can diagnose a stale
        # shim that predates filesystem registration receipts.
        self._registration_rejections: "OrderedDict[str, dict[str, Any]]" = (
            OrderedDict()
        )
        self._transaction_archive_path = os.path.join(
            self._storage_dir, "transaction_archives.jsonl",
        )
        if load_archives:
            self._load_transaction_archives()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _registration_value(value: Any, limit: int = 200) -> Any:
        """Normalize untrusted diagnostic fields without retaining large trees."""
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:limit]
        return f"<{type(value).__name__}>"

    def _prune_registration_rejections_locked(self, now: float) -> None:
        cutoff = now - self._REGISTRATION_REJECTION_TTL
        while self._registration_rejections:
            _key, record = next(iter(self._registration_rejections.items()))
            if float(record.get("rejected_at", 0.0) or 0.0) >= cutoff:
                break
            self._registration_rejections.popitem(last=False)
        while len(self._registration_rejections) > self._MAX_REGISTRATION_REJECTIONS:
            self._registration_rejections.popitem(last=False)

    @staticmethod
    def _registration_attempt_key(
        token_hash: str,
        game_id: Any,
        game_pid: Any,
        launch_id: Any,
        retry_mode: Any,
    ) -> str:
        """Return an internal key for one process-level registration attempt."""
        provenance = json.dumps(
            [
                str(game_id or "").lower(),
                game_pid if isinstance(game_pid, int) else None,
                str(launch_id) if launch_id is not None else None,
                str(retry_mode) if retry_mode is not None else None,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        suffix = hashlib.sha256(provenance.encode("utf-8")).hexdigest()[:16]
        return f"{token_hash}:{suffix}"

    def record_registration_rejection(
        self,
        token: str | None,
        *,
        reason: str,
        message: str,
        game_id: Any = None,
        game_pid: Any = None,
        expected_protocol: Any = None,
        received_protocol: Any = None,
        launch_id: Any = None,
        transient: bool = False,
        first_rejected_at: Any = None,
        retry_mode: Any = None,
        retry_until: Any = None,
    ) -> None:
        """Remember one token-owned assignment refusal without storing secrets."""
        if not token:
            return
        now = time.time()
        record = {
            "reason": str(reason)[:80],
            "message": str(message)[:500],
            "game_id": self._registration_value(game_id),
            "game_pid": game_pid if isinstance(game_pid, int) else None,
            "expected_protocol": self._registration_value(
                expected_protocol, limit=80,
            ),
            "received_protocol": self._registration_value(
                received_protocol, limit=80,
            ),
            "launch_id": self._registration_value(launch_id, limit=128),
            "transient": bool(transient),
            "first_rejected_at": (
                float(first_rejected_at)
                if isinstance(first_rejected_at, (int, float)) else now
            ),
            "retry_mode": self._registration_value(retry_mode, limit=32),
            "retry_until": (
                float(retry_until)
                if isinstance(retry_until, (int, float)) else None
            ),
            "rejected_at": now,
        }
        token_hash = self._token_hash(str(token))
        record["reservation_id"] = token_hash[:16]
        record["_token_hash"] = token_hash
        attempt_key = self._registration_attempt_key(
            token_hash, game_id, game_pid, launch_id, retry_mode,
        )
        with self._lock:
            self._registration_rejections[attempt_key] = record
            self._registration_rejections.move_to_end(attempt_key)
            token_attempts = [
                key for key, stored in self._registration_rejections.items()
                if stored.get("_token_hash") == token_hash
            ]
            for stale_key in token_attempts[
                :-self._MAX_REGISTRATION_REJECTIONS_PER_TOKEN
            ]:
                self._registration_rejections.pop(stale_key, None)
            self._prune_registration_rejections_locked(now)

    def registration_rejections(
        self,
        token: str | None,
        *,
        game_id: Any = None,
        launch_id: Any = None,
        launch_started_at: Any = None,
    ) -> list[dict[str, Any]]:
        """Return this token's current attempt-matching assignment refusals."""
        if not token:
            return []
        now = time.time()
        token_hash = self._token_hash(str(token))
        try:
            started_at = float(launch_started_at)
        except (TypeError, ValueError):
            started_at = None
        with self._lock:
            self._prune_registration_rejections_locked(now)
            matches: list[dict[str, Any]] = []
            for attempt_key, stored in list(self._registration_rejections.items()):
                if stored.get("_token_hash") != token_hash:
                    continue
                if (
                    game_id is not None
                    and str(stored.get("game_id") or "").lower()
                    != str(game_id or "").lower()
                ):
                    continue
                stored_launch_id = stored.get("launch_id")
                if (
                    launch_id is not None
                    and stored_launch_id is not None
                    and str(stored_launch_id) != str(launch_id)
                ):
                    continue
                if (
                    launch_id is not None
                    and stored_launch_id is None
                    and started_at is not None
                    and float(stored.get("rejected_at", 0.0) or 0.0)
                    < started_at
                ):
                    continue
                record = stored
                if (
                    record.get("reason") == "reservation_conflict"
                    and record.get("transient") is True
                    and record.get("retry_mode") != "recovery"
                    and (
                        not isinstance(record.get("retry_until"), (int, float))
                        or now >= float(record["retry_until"])
                    )
                ):
                    record = dict(record)
                    record["transient"] = False
                    # Promotion changes terminality, not attempt provenance.
                    # Keep rejected_at as the last POST time used to correlate
                    # launch-id-less shims; a diagnostic GET must not make an
                    # old attempt look newer than the launch reading it.
                    record["promoted_at"] = now
                    self._registration_rejections[attempt_key] = record
                public = dict(record)
                public.pop("_token_hash", None)
                matches.append(public)
            # A terminal failure must not be hidden by a newer retryable record
            # from another process sharing the same platform launch handoff.
            matches.sort(key=lambda item: (
                item.get("transient") is not True,
                float(item.get("rejected_at", 0.0) or 0.0),
            ), reverse=True)
            return matches

    def registration_rejection(
        self,
        token: str | None,
        *,
        game_id: Any = None,
        launch_id: Any = None,
        launch_started_at: Any = None,
    ) -> dict[str, Any] | None:
        """Return the highest-priority refusal owned by this launch token."""
        matches = self.registration_rejections(
            token,
            game_id=game_id,
            launch_id=launch_id,
            launch_started_at=launch_started_at,
        )
        return matches[0] if matches else None

    def clear_registration_attempt(
        self,
        token: str | None,
        *,
        game_id: Any,
        game_pid: Any,
        launch_id: Any,
    ) -> None:
        """Clear stale diagnostics for the process that just assigned."""
        if not token:
            return
        token_hash = self._token_hash(str(token))
        normalized_game = str(game_id or "").lower()
        normalized_launch = str(launch_id) if launch_id is not None else None
        with self._lock:
            self._clear_registration_attempt_locked(
                token_hash, normalized_game, game_pid, normalized_launch,
            )

    def _clear_registration_attempt_locked(
        self,
        token_hash: str,
        normalized_game: str,
        game_pid: Any,
        normalized_launch: str | None,
    ) -> None:
        """Clear one attempt while the caller holds ``self._lock``."""
        for attempt_key, record in list(self._registration_rejections.items()):
            if (
                record.get("_token_hash") == token_hash
                and str(record.get("game_id") or "").lower() == normalized_game
                and record.get("game_pid") == game_pid
                and (
                    str(record.get("launch_id"))
                    if record.get("launch_id") is not None else None
                ) == normalized_launch
            ):
                self._registration_rejections.pop(attempt_key, None)

    def _load_transaction_archives(self) -> None:
        """Rebuild freed-slot nonce recovery from its durable archive index."""
        path = self._transaction_archive_path
        if not os.path.isfile(path):
            return
        latest: dict[int, dict[str, Any]] = {}
        try:
            with open(path, "rb") as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                        slot_id = int(entry["slot_id"])
                        timestamp = float(entry.get("timestamp", 0) or 0)
                        if not math.isfinite(timestamp):
                            raise ValueError("non-finite archive timestamp")
                        latest[slot_id] = entry
                    except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
                        print(f"[bridge] Invalid archive index row in {path}; preserving file", file=sys.stderr)
                        continue
        except OSError:
            return
        self._next_slot = max(latest, default=0) + 1
        retained = sorted(
            latest.items(),
            key=lambda item: float(item[1].get("timestamp", 0) or 0),
        )[-self._MAX_TRANSACTION_ARCHIVES:]
        for slot_id, entry in retained:
            journal_path = str(entry.get("journal_path") or "")
            if not journal_path or not os.path.isfile(journal_path):
                continue
            if os.path.dirname(os.path.realpath(journal_path)) != os.path.realpath(self._storage_dir):
                print(f"[bridge] Ignoring archive outside owned storage: {journal_path}", file=sys.stderr)
                continue
            gs = GameState(storage_dir=self._storage_dir)
            gs.slot_id = slot_id
            gs.game_id = str(entry.get("game_id") or "")
            gs.game_pid = entry.get("game_pid")
            gs._transaction_log_path = journal_path
            try:
                gs._load_transaction_journal()
            except TransactionJournalError as exc:
                self._unavailable_archive_paths.add(os.path.realpath(journal_path))
                print(f"[bridge] Archive unavailable; preserving {journal_path}: {exc}", file=sys.stderr)
                continue
            if not gs._act_transactions:
                continue
            gs._closed = True
            self.transaction_archives[slot_id] = gs
            self.archived_slot_to_game[slot_id] = gs.game_id
            token_hash = entry.get("token_hash")
            if isinstance(token_hash, str) and token_hash:
                self._archived_token_hashes[slot_id] = token_hash
        # Recovery never rewrites journals or the archive index.

    def _persist_transaction_archive(
        self, slot_id: int, game_id: str, gs: GameState, token: str | None,
    ) -> bool:
        path = self._transaction_archive_path
        entry = {
            "slot_id": slot_id,
            "game_id": game_id,
            "game_pid": getattr(gs, "game_pid", None),
            "journal_path": (
                os.path.abspath(gs._transaction_log_path)
                if gs._transaction_log_path else None
            ),
            "token_hash": self._token_hash(token) if token else None,
            "timestamp": time.time(),
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                stream.flush()
            return True
        except OSError:
            return False

    def _compact_transaction_archive_index(self) -> bool:
        """Atomically rewrite the archive index from retained tombstones."""
        path = self._transaction_archive_path
        temp_path = None
        retained_paths = {
            os.path.realpath(gs._transaction_log_path)
            for gs in self.transaction_archives.values()
            if gs._transaction_log_path
        }
        # Re-read the old index before replacement. If an earlier compaction
        # failed, its pruned entries are still here and become cleanup work for
        # this attempt instead of leaking their journals permanently.
        try:
            with open(path, "rb") as old_stream:
                for line in old_stream:
                    try:
                        old_entry = json.loads(line)
                        old_path = str(old_entry.get("journal_path") or "")
                    except (
                        AttributeError, TypeError, ValueError,
                        json.JSONDecodeError,
                    ):
                        print(f"[bridge] Archive index compaction refused; preserving invalid row in {path}", file=sys.stderr)
                        return False
                    if old_path and os.path.realpath(old_path) not in retained_paths:
                        self._archive_cleanup_backlog.add(old_path)
        except OSError:
            pass
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix=".archives-", dir=os.path.dirname(path))
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for slot_id, gs in sorted(self.transaction_archives.items()):
                    entry = {
                        "slot_id": slot_id,
                        "game_id": self.archived_slot_to_game.get(slot_id, ""),
                        "game_pid": getattr(gs, "game_pid", None),
                        "journal_path": (
                            os.path.abspath(gs._transaction_log_path)
                            if gs._transaction_log_path else None
                        ),
                        "token_hash": self._archived_token_hashes.get(slot_id),
                        "timestamp": time.time(),
                    }
                    stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                stream.flush()
            os.replace(temp_path, path)
            return True
        except OSError:
            try:
                if temp_path is not None:
                    os.remove(temp_path)
            except OSError:
                pass
            return False

    def _delete_archived_journals(self, paths: list[str]) -> None:
        """Delete pruned journals, constrained to the bridge log directory."""
        log_dir = os.path.realpath(os.path.dirname(self._transaction_archive_path))
        retained = {
            os.path.realpath(gs._transaction_log_path)
            for gs in (
                list(self.transaction_archives.values())
                + list(self.slots.values())
            )
            if gs._transaction_log_path
        }
        for path in paths:
            resolved = os.path.realpath(path)
            basename = os.path.basename(resolved)
            if (
                os.path.dirname(resolved) != log_dir
                or not basename.startswith("transactions_")
                or not basename.endswith(".jsonl")
            ):
                self._archive_cleanup_backlog.discard(path)
                continue
            if resolved in retained or resolved in self._unavailable_archive_paths:
                continue
            try:
                os.remove(resolved)
                self._archive_cleanup_backlog.discard(path)
            except FileNotFoundError:
                self._archive_cleanup_backlog.discard(path)
            except OSError:
                pass

    def _prune_transaction_archives_locked(self) -> None:
        """Bound recovery tombstones without treating them as live slots."""
        excess = len(self.transaction_archives) - self._MAX_TRANSACTION_ARCHIVES
        if excess <= 0:
            return
        oldest = sorted(self.transaction_archives)[:excess]
        pruned_paths: list[str] = []
        for slot_id in oldest:
            gs = self.transaction_archives.pop(slot_id, None)
            if gs and gs._transaction_log_path:
                pruned_paths.append(gs._transaction_log_path)
            self.archived_slot_to_game.pop(slot_id, None)
            self._archived_token_hashes.pop(slot_id, None)
        self._archive_cleanup_backlog.update(pruned_paths)
        if self._compact_transaction_archive_index():
            self._delete_archived_journals(
                list(self._archive_cleanup_backlog),
            )

    def _slot_ids_for_game_locked(self, game_id: str) -> list[int]:
        """Return slot ids whose game_id matches case-insensitively.

        Caller must hold ``_lock``. Case-only duplicates are still ambiguous:
        a game-id hint must never silently pick one active slot over another.
        """
        gid_lower = game_id.lower()
        return sorted(
            sid for sid, gid in self.slot_to_game.items()
            if gid.lower() == gid_lower
        )

    def _unique_slot_id_for_game_locked(self, game_id: str) -> int | None:
        """Return the unique slot for a game id, or None when absent/ambiguous."""
        matches = self._slot_ids_for_game_locked(game_id)
        return matches[0] if len(matches) == 1 else None

    def _refresh_game_mapping_locked(self, game_id: str | None) -> None:
        """Keep legacy game_to_slot populated only for unique game ids."""
        if not game_id:
            return
        for gid in list(self.game_to_slot):
            if gid == game_id or gid.lower() == game_id.lower():
                self.game_to_slot.pop(gid, None)
        slot_id = self._unique_slot_id_for_game_locked(game_id)
        if slot_id is not None:
            self.game_to_slot[self.slot_to_game[slot_id]] = slot_id

    def assign(
        self,
        game_id: str,
        game_pid: int | None = None,
        shim_protocol_version: int = SHIM_PROTOCOL_VERSION,
    ) -> int | None:
        """Assign a slot to a game. Returns slot_id or None if full.

        When *game_pid* is provided, a new slot is allocated even if another
        slot already exists for the same game_id (multi-instance mode).
        Without game_pid, reuses existing slot (case-insensitive).
        """
        result = self.assign_reserved(
            game_id,
            game_pid=game_pid,
            token=None,
            shim_protocol_version=shim_protocol_version,
        )
        return result.get("slot_id") if "error" not in result else None

    def assign_reserved(
        self,
        game_id: str,
        game_pid: int | None,
        token: str | None,
        shim_protocol_version: int = SHIM_PROTOCOL_VERSION,
        launch_id: Any = None,
    ) -> dict:
        """Assign and optionally reserve a shim slot as one operation."""
        with self._lock:
            slot_id = None
            if game_pid is not None:
                for sid, gs in self.slots.items():
                    if (
                        getattr(gs, "game_pid", None) == game_pid
                        and self.slot_to_game.get(sid, "").lower()
                        == game_id.lower()
                    ):
                        slot_id = sid
                        break
            else:
                slot_id = self._unique_slot_id_for_game_locked(game_id)

            created = False
            if slot_id is None:
                if len(self.slots) >= self.max_slots:
                    return {"error": "All slots are full.", "status": "full"}
                slot_id = self._next_slot
                self._next_slot += 1
                gs = GameState(storage_dir=self._storage_dir)
                gs.shim_protocol_version = shim_protocol_version
                if game_pid is not None:
                    gs.game_pid = game_pid
                gs.configure_identity(slot_id, game_id, game_pid)
                self.slots[slot_id] = gs
                self.slot_to_game[slot_id] = game_id
                self._refresh_game_mapping_locked(game_id)
                created = True

            if token:
                token_slot = self._token_to_slot.get(token)
                if token_slot is not None and token_slot != slot_id:
                    if created:
                        self.slots.pop(slot_id, None)
                        self.slot_to_game.pop(slot_id, None)
                        self._refresh_game_mapping_locked(game_id)
                    return {
                        "error": f"Token already reserves slot {token_slot}",
                        "slot_id": token_slot,
                        "status": "reserved",
                    }
                existing = self._slot_tokens.get(slot_id)
                if existing and not secrets.compare_digest(existing, token):
                    if created:
                        self.slots.pop(slot_id, None)
                        self.slot_to_game.pop(slot_id, None)
                        self._refresh_game_mapping_locked(game_id)
                    return {
                        "error": f"Slot {slot_id} already reserved",
                        "slot_id": slot_id,
                        "status": "reserved",
                    }
                if not existing:
                    self._slot_tokens[slot_id] = token
                    self._token_to_slot[token] = slot_id
                self._clear_registration_attempt_locked(
                    self._token_hash(str(token)),
                    str(game_id or "").lower(),
                    game_pid,
                    str(launch_id) if launch_id is not None else None,
                )

            gs = self.slots[slot_id]
            gs.shim_protocol_version = shim_protocol_version
            if game_pid is not None:
                gs.game_pid = game_pid
            gs.registration_launch_id = (
                str(launch_id) if launch_id is not None else None
            )
            gs.registered_at = time.time()

            result = {
                "status": "assigned",
                "slot_id": slot_id,
                "game_id": game_id,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            }
            if token:
                result["token"] = token
            return result

    def get(self, key: int | str) -> GameState | None:
        """Look up a slot by slot_id (int) or game_id (str, case-insensitive)."""
        with self._lock:
            if isinstance(key, int):
                return self.slots.get(key)
            slot_id = self._unique_slot_id_for_game_locked(key)
            return self.slots.get(slot_id) if slot_id is not None else None

    def get_slot_id(self, key: str) -> int | None:
        """Look up slot_id by game_id."""
        with self._lock:
            return self._unique_slot_id_for_game_locked(key)

    def get_transaction_archive(self, key: int | str) -> GameState | None:
        """Return an ended/freed slot retained for nonce recovery only."""
        with self._lock:
            if isinstance(key, int):
                return self.transaction_archives.get(key)
            matches = [
                sid for sid, game_id in self.archived_slot_to_game.items()
                if game_id.lower() == key.lower()
            ]
            if len(matches) != 1:
                return None
            return self.transaction_archives.get(matches[0])

    def free_with_status(self, key: int | str) -> tuple[bool, str | None]:
        """Release a slot and distinguish lookup from persistence failures.

        LOCK ORDERING CONTRACT (do not collapse these back together):
        the per-slot teardown — ``gs._act_submit_lock`` ->
        ``gs._transaction_ack_lock`` -> journal/archive disk writes — runs with
        the SlotManager lock RELEASED.  Holding the manager lock across those
        writes meant one hung write on one slot stalled ``assign``/``get``/
        ``free`` for every other slot.  ``_freeing`` is what the manager lock
        protects instead: it makes the release exclusive per slot while the
        slot is still mapped, so two concurrent frees cannot both tear down.

        The seal-before-unmap property is preserved: ``gs._closed`` is set by
        ``_seal_and_flush_transactions`` before the manager lock is retaken to
        remove the mappings, so any request that resolved this GameState
        earlier still rejects once it reaches ``gs._lock``.
        """
        with self._lock:
            if isinstance(key, str):
                slot_id = self._unique_slot_id_for_game_locked(key)
                if slot_id is None:
                    return False, "not_found"
            else:
                slot_id = key
            if slot_id not in self.slots or slot_id in self._freeing:
                return False, "not_found"
            gs = self.slots[slot_id]
            game_id = self.slot_to_game.get(slot_id, "")
            token = self._slot_tokens.get(slot_id)
            self._freeing.add(slot_id)
        try:
            with gs._act_submit_lock, gs._transaction_ack_lock:
                with gs._lock:
                    rollback_records = OrderedDict(
                        (nonce, dict(record))
                        for nonce, record in gs._act_transactions.items()
                    )
                    rollback_commands = list(gs.pending_commands)
                    rollback_active = gs._active_action_nonce
                    rollback_settled = gs._last_settled_action_nonce
                    rollback_settled_at = gs._last_settled_at
                    rollback_post_settle = gs._post_settle_events
                if not gs._seal_and_flush_transactions(terminalize=True):
                    return False, "persistence_failed"
                if gs._act_transactions and not self._persist_transaction_archive(
                    slot_id, game_id, gs, token,
                ):
                    with gs._lock:
                        for nonce, original in rollback_records.items():
                            current = gs._act_transactions.get(nonce, {})
                            original["revision"] = max(
                                int(original.get("revision", 0) or 0),
                                int(current.get("revision", 0) or 0),
                            ) + 1
                        gs._act_transactions = rollback_records
                        gs.pending_commands[:] = rollback_commands
                        gs._active_action_nonce = rollback_active
                        gs._last_settled_action_nonce = rollback_settled
                        gs._last_settled_at = rollback_settled_at
                        gs._post_settle_events = rollback_post_settle
                    # The seal already wrote TERMINAL states for these records.
                    # Memory is going back live, so disk must follow — ignoring
                    # this result let the journal LEAD memory, inverting the
                    # "disk lags memory, never leads" invariant.  A failed write
                    # stays in the retryable snapshot backlog and is repaired by
                    # the next _flush_transaction_snapshots() (acceptance path).
                    deferred = [
                        nonce for nonce, snapshot in rollback_records.items()
                        if not gs._persist_transaction_snapshot(snapshot)
                    ]
                    if deferred:
                        print(
                            "[vnflight.bridge] slot {} rollback could not "
                            "re-persist {} transaction snapshot(s); the journal "
                            "still shows terminal states until the snapshot "
                            "backlog flushes".format(slot_id, len(deferred)),
                            file=sys.stderr,
                            flush=True,
                        )
                    with gs._lock:
                        gs._closed = False
                    return False, "persistence_failed"
                # Sealed: safe to unmap. Retake the manager lock only for the
                # in-memory bookkeeping, never for the disk work above.
                with self._lock:
                    game_id = self.slot_to_game.pop(slot_id, None)
                    if game_id:
                        self._refresh_game_mapping_locked(game_id)
                    gs = self.slots.pop(slot_id)
                    if gs._act_transactions:
                        self.transaction_archives[slot_id] = gs
                        self.archived_slot_to_game[slot_id] = game_id or ""
                        if token:
                            self._archived_token_hashes[slot_id] = self._token_hash(
                                token)
                            self._slot_tokens.pop(slot_id, None)
                            self._token_to_slot.pop(token, None)
                        self._prune_transaction_archives_locked()
                    else:
                        token = self._slot_tokens.pop(slot_id, None)
                        if token:
                            self._token_to_slot.pop(token, None)
        finally:
            with self._lock:
                self._freeing.discard(slot_id)
        # Close the log OUTSIDE the SlotManager lock, under the GameState
        # write lock, so a concurrent off-lock flush on this slot can't
        # write-after-close (same race as reset()).
        gs._persist_transaction_meta()
        gs._close_log()
        return True, None

    def free(self, key: int | str) -> bool:
        """Release a slot by slot_id or game_id. Returns True if freed."""
        freed, _ = self.free_with_status(key)
        return freed

    def can_free_without_admin(self, key: int | str) -> bool:
        """Return True for slots safe to free without an admin token.

        Ended slots are disconnected.  Slots with a recorded dead PID are also
        disconnected enough for cleanup; this supports manual bridge recovery
        after game crashes or stale process bookkeeping.
        """
        with self._lock:
            if isinstance(key, int):
                gs = self.slots.get(key)
            else:
                matches = self._slot_ids_for_game_locked(key)
                if len(matches) > 1:
                    return False
                slot_id = matches[0] if matches else None
                gs = self.slots.get(slot_id) if slot_id is not None else None
            if gs is None:
                return True
            if gs.status == "ended":
                return True
            pid = getattr(gs, "game_pid", None)
        return bool(pid and not self._pid_alive(pid))

    def list_slots(self) -> list[dict[str, Any]]:
        """List all active slots."""
        with self._lock:
            result = []
            for slot_id, gs in sorted(self.slots.items()):
                game_id = self.slot_to_game.get(slot_id, "")
                result.append({
                    "slot_id": slot_id,
                    "game_id": game_id,
                    "status": gs.status,
                    "event_counter": gs.event_counter,
                    "has_pending_request": gs.pending_request is not None,
                    "game_pid": gs.game_pid,
                    "launch_id": gs.registration_launch_id,
                    "registered_at": gs.registered_at,
                    "reserved": slot_id in self._slot_tokens,
                    "shim_protocol_version": gs.shim_protocol_version,
                    "reservation_id": (
                        hashlib.sha256(
                            self._slot_tokens[slot_id].encode("utf-8")
                        ).hexdigest()[:16]
                        if slot_id in self._slot_tokens else None
                    ),
                })
            return result

    def sole_slot(self) -> GameState | None:
        """Return the sole GameState if exactly one slot exists, else None."""
        with self._lock:
            if len(self.slots) == 1:
                return next(iter(self.slots.values()))
            return None

    def find_slot_id(self, gs: GameState) -> int | None:
        """Find the slot_id for a given GameState instance."""
        with self._lock:
            for sid, s in self.slots.items():
                if s is gs:
                    return sid
            return None

    def find_any_slot_id(self, gs: GameState) -> int | None:
        """Find an active or archived slot for access-control checks."""
        with self._lock:
            for collection in (self.slots, self.transaction_archives):
                for sid, state in collection.items():
                    if state is gs:
                        return sid
            return None

    def is_empty(self) -> bool:
        """True if no games are active; archives do not keep the hub alive."""
        with self._lock:
            return not self.slots

    def _pid_alive(self, pid: int) -> bool:
        """Check if a process is still running."""
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def reap_dead_slots(self) -> list[int]:
        """Free slots whose game_pid is no longer running."""
        dead: list[int] = []
        with self._lock:
            for slot_id, gs in list(self.slots.items()):
                pid = getattr(gs, "game_pid", None)
                if pid and not self._pid_alive(pid):
                    dead.append(slot_id)
        for slot_id in dead:
            game_id = self.slot_to_game.get(slot_id, "?")
            print(f"[bridge] Reaping dead slot {slot_id} (game={game_id}, pid={self.slots[slot_id].game_pid})")
            self.free(slot_id)
        return dead

    def start_pid_watchdog(self, interval: float = 10.0) -> None:
        """Start a background thread that reaps dead slots periodically."""
        def _watchdog():
            while True:
                time.sleep(interval)
                try:
                    self.reap_dead_slots()
                except Exception:
                    pass
        t = threading.Thread(target=_watchdog, daemon=True)
        t.start()

    # -- Token management --

    def reserve(self, slot_hint: str, token: str | None = None,
                force: bool = False) -> dict:
        """Reserve a slot. Returns {"slot_id", "token"} or {"error"}.

        slot_hint: game_id, slot number, or partial match.
        token: optional pre-generated token; bridge generates if omitted.
        force: replace an existing reservation (admin override).  Handled
            here, under the manager lock, so the check-then-unreserve is
            atomic (the old handler-side dance raced concurrent reserves).
        """
        with self._lock:
            try:
                int(slot_hint)
            except ValueError:
                matches = self._slot_ids_for_game_locked(slot_hint)
                if len(matches) > 1:
                    return {
                        "error": (
                            f"Ambiguous slot hint '{slot_hint}': "
                            + ", ".join(str(sid) for sid in matches)
                            + ". Use a numeric slot id."
                        ),
                        "status": "ambiguous",
                        "matches": matches,
                    }
            # Resolve hint to slot_id.
            slot_id = self._resolve_hint(slot_hint)
            if slot_id is None:
                return {"error": f"No slot matching '{slot_hint}'"}
            if slot_id not in self.slots:
                return {"error": f"Slot {slot_id} does not exist"}
            # Check if already reserved.
            if slot_id in self._slot_tokens:
                existing = self._slot_tokens[slot_id]
                if token and secrets.compare_digest(existing, token):
                    game_id = self.slot_to_game.get(slot_id, "")
                    return {"slot_id": slot_id, "game_id": game_id,
                            "token": existing}
                if force:
                    # Admin override: atomically drop the old reservation
                    # and fall through to create the new one.
                    self._slot_tokens.pop(slot_id, None)
                    self._token_to_slot.pop(existing, None)
                else:
                    return {"error": f"Slot {slot_id} already reserved",
                            "slot_id": slot_id, "status": "reserved"}
            # Generate or use provided token.
            slot_token = token or secrets.token_urlsafe(16)
            self._slot_tokens[slot_id] = slot_token
            self._token_to_slot[slot_token] = slot_id
            game_id = self.slot_to_game.get(slot_id, "")
            return {"slot_id": slot_id, "game_id": game_id,
                    "token": slot_token}

    def unreserve(self, slot_id: int) -> bool:
        """Release a slot's reservation. Returns True if unreserved."""
        with self._lock:
            token = self._slot_tokens.pop(slot_id, None)
            if token:
                self._token_to_slot.pop(token, None)
                return True
            return False

    def is_admin_token(self, token: str | None) -> bool:
        """Constant-time comparison against the admin token."""
        if not token:
            return False
        return secrets.compare_digest(str(token), self.admin_token)

    def is_known_token(self, token: str | None) -> bool:
        """True for the admin token or any slot reservation token.

        Used to gate slot METADATA (the /slots listing) in require-token
        mode: any client that holds a valid token for this bridge may
        discover what is running; a tokenless caller may not.
        """
        if not token:
            return False
        if self.is_admin_token(token):
            return True
        with self._lock:
            reserved = list(self._slot_tokens.values())
            archived_hashes = list(self._archived_token_hashes.values())
        if any(
            secrets.compare_digest(str(token), reserved_token)
            for reserved_token in reserved
        ):
            return True
        candidate_hash = self._token_hash(str(token))
        return any(
            secrets.compare_digest(candidate_hash, archived_hash)
            for archived_hash in archived_hashes
        )

    def check_token(self, slot_id: int, token: str | None) -> bool:
        """Check if a token is valid for a slot.

        Returns True if:
        - token matches the admin token
        - token matches the slot's reservation token
        - slot is unreserved and require_token is False

        Comparisons use secrets.compare_digest — a plain == leaks the
        token via response-timing differences.
        """
        if self.is_admin_token(token):
            return True
        reserved_token = self._slot_tokens.get(slot_id)
        if reserved_token is not None:
            if not token:
                return False
            return secrets.compare_digest(str(token), reserved_token)
        archived_hash = self._archived_token_hashes.get(slot_id)
        if archived_hash is not None:
            if not token:
                return False
            return secrets.compare_digest(
                self._token_hash(str(token)), archived_hash,
            )
        # Slot is unreserved — allow if tokens not required.
        return not self.require_token

    def _resolve_hint(self, hint: str) -> int | None:
        """Resolve a slot hint to a slot_id. Must hold _lock."""
        # Numeric slot ID?
        try:
            sid = int(hint)
            if sid in self.slots:
                return sid
            return None
        except ValueError:
            pass
        # Game ID, case-insensitive. Duplicate game ids are ambiguous;
        # callers must use a numeric slot id then.
        return self._unique_slot_id_for_game_locked(hint)


# Singleton slot manager.
# Importing the module (including the bundled CLI) must not touch recovery files.
slots = SlotManager(load_archives=False)


def _schedule_shutdown(server: "ThreadedHTTPServer") -> None:
    """Shut down the server after a grace period, unless a new game connects.

    Setting VNFLIGHT_KEEP_ALIVE=1 in the bridge's environment disables
    the auto-shutdown so the bridge stays up even when all games have
    disconnected.  Useful for interactive human-in-the-loop workflows
    where the bridge needs to survive game restarts.
    """
    if os.environ.get("VNFLIGHT_KEEP_ALIVE") == "1":
        print("[slots] All games disconnected — staying alive "
              "(VNFLIGHT_KEEP_ALIVE=1).")
        return
    def _do():
        time.sleep(10)  # Grace period for new games to connect
        if slots.is_empty():
            print("[slots] Grace period elapsed, still empty — shutting down.")
            server.shutdown()
        else:
            print("[slots] New game connected during grace period — staying alive.")
    t = threading.Thread(target=_do, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------


class BridgeHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests for the bridge."""

    # Suppress default logging to stderr.
    def log_message(self, format: str, *args: Any) -> None:
        if self.server.verbose:  # type: ignore[attr-defined]
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] {self.address_string()} {format % args}")

    # -- helpers --

    def _read_json(self) -> dict | None:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return None
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _respond_json(self, data: Any, status: int = 200) -> None:
        # NOTE: deliberately NO Access-Control-Allow-Origin header.  The
        # bridge has no in-browser consumers (the harness/GUI web apps
        # proxy through their own servers), and a CORS wildcard let any
        # webpage read full game/agent state from localhost.
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # The client hung up mid-response — a poll loop timing out its
            # own request and retrying.  Routine under load (a live session
            # logged 156 of these as full socketserver stack dumps); one
            # quiet line keeps the log honest without burying real errors.
            print("[bridge] client closed connection mid-response "
                  f"({self.path.split('?')[0]})", flush=True)

    def _respond_error(self, status: int, message: str) -> None:
        self._respond_json({"error": message}, status=status)

    def _respond_ok(self, message: str = "ok") -> None:
        self._respond_json({"status": message})

    def _get_token(self) -> str | None:
        """Extract token from X-Slot-Token header."""
        return self.headers.get("X-Slot-Token")

    def _check_shim_protocol(self) -> bool:
        """Reject shim traffic that did not complete the current handshake."""
        received = self.headers.get("X-VNFlight-Shim-Protocol")
        try:
            received_version = int(received) if received is not None else None
        except (TypeError, ValueError):
            received_version = None
        if received_version == SHIM_PROTOCOL_VERSION:
            return True
        self._respond_json(
            {
                "error": "Installed vnflight shim protocol mismatch.",
                "reason": "shim_protocol_mismatch",
                "expected": SHIM_PROTOCOL_VERSION,
                "received": received,
                "remediation": (
                    "Run install-shim for this game, then restart the game "
                    "and bridge."
                ),
            },
            status=409,
        )
        return False

    def _check_slot_access(self, gs: GameState) -> bool:
        """Check if the request may read/write this slot.

        Returns True if allowed. Sends 403 and returns False if denied.
        Reserved slots require the slot token (or admin token); unreserved
        slots are open unless the bridge runs with --require-token.
        """
        token = self._get_token()
        slot_id = slots.find_any_slot_id(gs)
        if slot_id is None:
            self._respond_error(403, "Unknown slot identity.")
            return False
        if slots.check_token(slot_id, token):
            return True
        self._respond_error(403, "Invalid or missing token for this slot.")
        return False

    def _check_admin(self) -> bool:
        """Check if the request has admin privileges.

        Returns True if allowed. Sends 403 and returns False if denied.
        """
        if slots.is_admin_token(self._get_token()):
            return True
        self._respond_error(403, "Admin token required.")
        return False

    def _slots_metadata_allowed(self) -> bool:
        """May this request read slot metadata (game ids, PIDs, flags)?

        Open bridges keep /slots (and the slots mirrored on /status and /)
        tokenless for discovery.  --require-token bridges expose the same
        metadata only to holders of the admin token or any slot
        reservation token.
        """
        if not slots.require_token:
            return True
        return slots.is_known_token(self._get_token())

    # -- request-level hardening ------------------------------------------

    _LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}

    @classmethod
    def _hostname_is_local(cls, hostname: str) -> bool:
        """True for localhost names and any raw IP literal.

        IP literals are safe from DNS rebinding (the attack needs a DNS
        name the attacker controls); non-local DNS names are not.
        """
        hostname = hostname.strip("[]").lower()
        if not hostname:
            return False
        if hostname in cls._LOCAL_HOSTNAMES:
            return True
        try:
            ipaddress.ip_address(hostname)
            return True
        except ValueError:
            return False

    def _request_allowed(self) -> bool:
        """Cheap browser-attack guards applied to every request.

        1. Host allowlist: a DNS-rebinding attack reaches the bridge with
           the attacker's hostname in the Host header.  Only localhost
           names, IP literals, and explicitly allowed hosts pass.
        2. Origin/Referer check: browser-initiated cross-origin requests
           carry an Origin (fetch/XHR/forms) or Referer (subresource
           loads) header.  Non-local web origins are rejected — this cuts
           off "simple" GET/POSTs that skip the CORS preflight entirely.

        Native clients (shim, CLI, harness) send neither header, or send
        a local Host, so they are unaffected.  Sends 403 when denied.
        """
        host = self.headers.get("Host")
        if host:
            try:
                hostname = urlparse("//" + host).hostname or ""
            except ValueError:
                self._respond_error(403, "Malformed Host header.")
                return False
            extra = getattr(self.server, "allowed_hosts", None) or set()
            if (not self._hostname_is_local(hostname)
                    and hostname.lower() not in extra):
                self._respond_error(
                    403, "Host not allowed (possible DNS rebinding).")
                return False
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if not value:
                continue
            if value == "null":
                self._respond_error(403, f"{header} not allowed.")
                return False
            try:
                origin_host = (urlparse(value).hostname or "").lower()
            except ValueError:
                self._respond_error(403, f"Malformed {header} header.")
                return False
            if origin_host and not self._hostname_is_local(origin_host):
                self._respond_error(
                    403, f"Cross-origin requests are not allowed ({header}).")
                return False
        return True

    def _check_post_content_type(self) -> bool:
        """Require application/json on POST bodies.

        Browsers cannot attach this Content-Type to a cross-origin
        request without triggering a CORS preflight (which the bridge
        never approves), so this single check turns every body-bearing
        POST route into a non-"simple" request.  All in-repo clients
        (shim included) already send application/json.
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            content_length = 0
        if content_length == 0:
            return True
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            return True
        self._respond_error(
            415, "Content-Type must be application/json.")
        return False

    def _respond_png(self, data_b64: str) -> None:
        raw = base64.b64decode(data_b64)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _parse_query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        # Flatten: take first value for each key.
        return {k: v[0] for k, v in qs.items()}

    @property
    def _route(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def _resolve_slot(self) -> tuple[str, GameState | None, str | None]:
        """Parse the URL path into (route, game_state, error_message).

        Supports three addressing modes:
          /1/pending          — by slot_id
          /echoes_of_tomorrow/pending — by game_id
          /pending            — auto-resolve to sole slot

        Returns (route, state, None) on success, or
                (route, None, error_msg) on failure.
        """
        raw = self._route
        parts = raw.strip("/").split("/", 1)

        # Try slot prefix (first path segment).
        if len(parts) == 2:
            prefix, rest = parts
            route = "/" + rest

            # Numeric slot ID?
            try:
                slot_id = int(prefix)
                gs = slots.get(slot_id)
                if gs is not None:
                    return route, gs, None
                if route == "/transaction":
                    archived = slots.get_transaction_archive(slot_id)
                    if archived is not None:
                        return route, archived, None
                return route, None, f"No game in slot {slot_id}"
            except ValueError:
                pass

            # Game ID string?
            gs = slots.get(prefix)
            if gs is not None:
                return route, gs, None
            if route == "/transaction":
                archived = slots.get_transaction_archive(prefix)
                if archived is not None:
                    return route, archived, None

        # No prefix (or prefix didn't match a slot) — try sole-slot fallback.
        route = raw
        if self.headers.get("X-VNFlight-Shim-Protocol") is not None:
            return route, None, (
                "Shim requests require an assigned slot prefix. Reconnect the "
                "game before sending gameplay traffic."
            )
        sole = slots.sole_slot()
        if sole is not None:
            return route, sole, None

        # Multiple slots and no prefix: ambiguous.
        active = slots.list_slots()
        if not active:
            return route, None, "No games connected. Use /slots/assign first."
        return route, None, (
            "Multiple games connected. Use a slot prefix: "
            + ", ".join(f"/{s['slot_id']}/" for s in active)
        )

    # -- CORS preflight --

    def do_OPTIONS(self) -> None:
        # No CORS approval headers: browser preflights fail, so pages
        # cannot make non-simple cross-origin requests to the bridge.
        if not self._request_allowed():
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- GET routes --

    def do_GET(self) -> None:
        if not self._request_allowed():
            return
        raw_route = self._route
        query = self._parse_query()

        # --- Admin endpoints (no slot prefix) ---

        if raw_route == "/slots":
            if not self._slots_metadata_allowed():
                # Wording keeps the "missing token" marker the harness
                # uses to detect token mismatches on /slots reads.
                self._respond_error(
                    403, "Invalid or missing token for this bridge.")
                return
            self._respond_json({"slots": slots.list_slots()})
            return

        if raw_route == "/status":
            # Global status — works without a slot prefix.
            # If a sole slot exists, include its details.
            if not self._slots_metadata_allowed():
                # Keep the liveness probe contract (is_up() only checks
                # for a 200) without leaking slot metadata or the sole
                # slot's details on a require-token bridge.
                self._respond_json({
                    "status": "restricted",
                    "version": __version__,
                    "slots": [],
                    "shim_protocol_version": SHIM_PROTOCOL_VERSION,
                })
                return
            sole = slots.sole_slot()
            if sole:
                self._respond_json(
                    {
                        "status": sole.status,
                        "version": __version__,
                        "shim_protocol_version": SHIM_PROTOCOL_VERSION,
                        "end_reason": sole.end_reason,
                        "event_counter": sole.event_counter,
                        "game_pid": sole.game_pid,
                        "has_pending_request": sole.pending_request is not None,
                        "has_pending_command": sole.pending_command is not None,
                        "context": sole.current_context,
                        "config": {
                            "auto_advance": sole.auto_advance,
                            "auto_advance_delay": sole.auto_advance_delay,
                            "end_on_menu_return": sole.end_on_menu_return,
                        },
                        "slots": slots.list_slots(),
                    }
                )
            else:
                self._respond_json(
                    {
                        "status": "idle",
                        "version": __version__,
                        "shim_protocol_version": SHIM_PROTOCOL_VERSION,
                        "slots": slots.list_slots(),
                    }
                )
            return

        if raw_route == "/":
            metadata_allowed = self._slots_metadata_allowed()
            sole = slots.sole_slot() if metadata_allowed else None
            if metadata_allowed:
                status = sole.status if sole else "no_game"
            else:
                status = "restricted"
            self._respond_json(
                {
                    "name": "vnflight bridge",
                    "version": __version__,
                    "shim_protocol_version": SHIM_PROTOCOL_VERSION,
                    "status": status,
                    "slots": slots.list_slots() if metadata_allowed else [],
                    "endpoints": {
                        "GET": [
                            "/status", "/state", "/pending", "/transcript",
                            "/action", "/command", "/transaction", "/context", "/screenshot",
                            "/slots", "/registration-rejection",
                        ],
                        "POST": [
                            "/event", "/request", "/act",
                            "/command", "/config", "/reset", "/inventory",
                            "/slots/assign", "/slots/free/<id>",
                        ],
                    },
                }
            )
            return

        if raw_route == "/registration-rejection":
            # A rejected reservation token is deliberately not a bridge
            # credential, but possession of it proves ownership of this one
            # diagnostic. Never permit tokenless/game-id/PID lookups.
            token = self._get_token()
            if not token:
                self._respond_error(403, "Registration token required.")
                return
            rejection = slots.registration_rejection(
                token,
                game_id=query.get("game_id"),
                launch_id=query.get("launch_id"),
                launch_started_at=query.get("launch_started_at"),
            )
            if rejection is None:
                self._respond_error(404, "No registration rejection recorded.")
                return
            self._respond_json({"registration_rejection": rejection})
            return

        # --- Slot-scoped endpoints ---

        route, gs, err = self._resolve_slot()
        if gs is None:
            self._respond_error(400, err or "No game state available.")
            return

        # Slot-scoped GETs carry gameplay data (/state, /transcript,
        # /screenshot) or CONSUME state (/action pops the pending action,
        # /command pops queued commands).  All of them require the slot
        # token once the slot is reserved (and always in require-token
        # mode); unreserved slots on an open bridge stay tokenless.
        if not self._check_slot_access(gs):
            return

        if route in {"/action", "/command"}:
            if not self._check_shim_protocol():
                return

        if route == "/status":
            resp = {
                    "status": gs.status,
                    "version": __version__,
                    "shim_protocol_version": SHIM_PROTOCOL_VERSION,
                    "end_reason": gs.end_reason,
                    "event_counter": gs.event_counter,
                    "game_pid": gs.game_pid,
                    "has_pending_request": gs.pending_request is not None,
                    "has_pending_command": gs.pending_command is not None,
                    "context": gs.current_context,
                    "config": {
                        "auto_advance": gs.auto_advance,
                        "auto_advance_delay": gs.auto_advance_delay,
                        "end_on_menu_return": gs.end_on_menu_return,
                    },
                }
            # Include sticky anomaly flag (not cleared on read).
            if gs.anomaly_flag:
                resp["anomaly"] = gs.anomaly_flag
            self._respond_json(resp
            )

        elif route == "/state":
            since = int(query.get("since", 0))
            self._respond_json(gs.get_state(since=since))

        elif route == "/screen":
            screen = gs.current_screen
            self._respond_json({"screen": screen})

        elif route == "/game_state":
            self._respond_json({"game_state": gs.get_game_state()})

        elif route == "/pending":
            req = gs.get_pending_request()
            if req is None:
                self._respond_json({"pending": None})
            else:
                self._respond_json({"pending": req})

        elif route == "/transcript":
            last_n = query.get("last")
            last_n = int(last_n) if last_n else None
            self._respond_json({"transcript": gs.get_transcript(last_n=last_n)})

        elif route == "/action":
            request_id = query.get("request_id")
            action = gs.consume_action(request_id=request_id)
            if action is not None:
                self._respond_json({"action": action})
            else:
                self._respond_json({"action": None})

        elif route == "/command":
            cmd = gs.consume_command()
            if cmd is not None:
                self._respond_json({"command": cmd})
            else:
                self._respond_json({"command": None})

        elif route == "/transaction":
            nonce = query.get("action_nonce") or query.get("nonce")
            if not nonce:
                self._respond_error(400, "Missing action_nonce.")
                return
            if "ack" in query:
                try:
                    acknowledged = gs.acknowledge_action_events(
                        nonce, int(query["ack"])
                    )
                except (TypeError, ValueError):
                    self._respond_error(400, "Invalid transaction ack cursor.")
                    return
                if acknowledged == "unknown_nonce":
                    self._respond_error(404, "Unknown action nonce.")
                    return
                if acknowledged != "ok":
                    # Storage failure, not a missing transaction: the drain is
                    # still replayable, so the client must retry, not give up.
                    self._respond_error(
                        503,
                        "Could not durably record the transaction ack "
                        "(reason: {}).".format(acknowledged),
                    )
                    return
            try:
                transaction = gs.get_action_transaction(nonce)
            except TransactionJournalError:
                self._respond_error(
                    503, "Transaction recovery storage is temporarily unavailable."
                )
                return
            if transaction is None:
                self._respond_error(404, "Unknown action nonce.")
            else:
                self._respond_json({"transaction": transaction})

        elif route == "/context":
            ctx = gs.get_context()
            self._respond_json({"context": ctx})

        elif route == "/inventory":
            with gs._lock:
                self._respond_json(
                    {
                        "inventory": list(gs.current_inventory),
                        "version": gs.inventory_version,
                    }
                )

        elif route == "/screenshot":
            snapshot = gs.get_screenshot_snapshot()
            screenshot = snapshot["screenshot"]
            if screenshot is None:
                self._respond_error(404, "No screenshot available.")
            else:
                accept = self.headers.get("Accept", "")
                if "image/png" in accept:
                    self._respond_png(screenshot)
                else:
                    self._respond_json(snapshot)

        else:
            self._respond_error(404, f"Unknown route: {raw_route}")

    # -- POST routes --

    def do_POST(self) -> None:
        if not self._request_allowed():
            return
        if not self._check_post_content_type():
            return
        raw_route = self._route
        body = self._read_json()

        # --- Admin endpoints (no slot prefix) ---

        if raw_route == "/slots/assign":
            if slots.require_token and not (
                (body or {}).get("token") or self._get_token()
            ):
                # In require-token mode slot registration must present a
                # token (it becomes the slot's reservation).  Keeps a
                # tokenless local process from creating/claiming slots.
                self._respond_error(403, "Bridge requires a token.")
                return
            if not body or "game_id" not in body:
                self._respond_error(400, "Missing 'game_id' in body.")
                return
            game_id = body["game_id"]
            game_pid = body.get("game_pid")
            if isinstance(game_pid, str) and game_pid.isdigit():
                game_pid = int(game_pid)
            assign_token = body.get("token") or self._get_token()
            received_protocol = body.get("shim_protocol_version")
            if received_protocol != SHIM_PROTOCOL_VERSION:
                safe_received_protocol = slots._registration_value(
                    received_protocol, limit=80,
                )
                slots.record_registration_rejection(
                    assign_token,
                    reason="shim_protocol_mismatch",
                    message=(
                        "Installed vnflight shim protocol mismatch. Run "
                        "install-shim for this game, then restart the game "
                        "and bridge."
                    ),
                    game_id=game_id,
                    game_pid=game_pid,
                    expected_protocol=SHIM_PROTOCOL_VERSION,
                    received_protocol=safe_received_protocol,
                    launch_id=body.get("launch_id"),
                )
                self._respond_json(
                    {
                        "error": "Installed vnflight shim protocol mismatch.",
                        "reason": "shim_protocol_mismatch",
                        "expected": SHIM_PROTOCOL_VERSION,
                        "received": safe_received_protocol,
                        "remediation": (
                            "Run install-shim for this game, then restart "
                            "the game and bridge."
                        ),
                    },
                    status=409,
                )
                return
            resp = slots.assign_reserved(
                game_id,
                game_pid=game_pid,
                token=assign_token,
                shim_protocol_version=received_protocol,
                launch_id=body.get("launch_id"),
            )
            if "error" in resp:
                # Capacity is transient. Reservation handoff conflicts follow
                # the retry intent declared by the shim: startup supplies a
                # finite deadline, while warm recovery remains retryable until
                # a successful assignment clears the record.
                if resp.get("status") == "reserved":
                    retry_mode = body.get("registration_retry_mode")
                    if retry_mode not in ("finite", "recovery", "single"):
                        retry_mode = "single"
                    requested_retry_until = body.get("registration_retry_until")
                    if (
                        isinstance(requested_retry_until, (int, float))
                        and not isinstance(requested_retry_until, bool)
                        and math.isfinite(float(requested_retry_until))
                    ):
                        retry_until = float(requested_retry_until)
                    else:
                        retry_until = None
                    priors = slots.registration_rejections(
                        assign_token,
                        game_id=game_id,
                        launch_id=body.get("launch_id"),
                    )
                    prior = next((candidate for candidate in priors if (
                        candidate.get("reason") == "reservation_conflict"
                        and candidate.get("game_pid") == game_pid
                        and candidate.get("retry_mode") == retry_mode
                    )), {})
                    same_conflict = (
                        prior.get("reason") == "reservation_conflict"
                        and str(prior.get("game_id") or "").lower()
                        == str(game_id or "").lower()
                        and prior.get("game_pid") == game_pid
                        and prior.get("launch_id")
                        == slots._registration_value(
                            body.get("launch_id"), limit=128,
                        )
                        and prior.get("retry_mode") == retry_mode
                    )
                    if same_conflict:
                        first_rejected_at = float(
                            prior.get("first_rejected_at") or time.time()
                        )
                    else:
                        first_rejected_at = time.time()
                    conflict_is_transient = (
                        retry_mode == "recovery"
                        or (
                            retry_mode == "finite"
                            and retry_until is not None
                            and time.time() < retry_until
                        )
                    )
                    slots.record_registration_rejection(
                        assign_token,
                        reason="reservation_conflict",
                        message=str(
                            resp.get("error") or "Slot assignment refused."
                        ),
                        game_id=game_id,
                        game_pid=game_pid,
                        expected_protocol=SHIM_PROTOCOL_VERSION,
                        received_protocol=received_protocol,
                        launch_id=body.get("launch_id"),
                        transient=conflict_is_transient,
                        first_rejected_at=first_rejected_at,
                        retry_mode=retry_mode,
                        retry_until=retry_until,
                    )
                elif resp.get("status") != "full":
                    slots.record_registration_rejection(
                        assign_token,
                        reason="reservation_conflict",
                        message=str(
                            resp.get("error") or "Slot assignment refused."
                        ),
                        game_id=game_id,
                        game_pid=game_pid,
                        expected_protocol=SHIM_PROTOCOL_VERSION,
                        received_protocol=received_protocol,
                        launch_id=body.get("launch_id"),
                    )
                status = 503 if resp.get("status") == "full" else 409
                self._respond_json(resp, status=status)
            else:
                self._respond_json(resp)
            return

        # POST /reserve — reserve a slot and get a token.
        if raw_route == "/reserve":
            if not body or "slot_hint" not in body:
                self._respond_error(400, "Missing 'slot_hint' in body.")
                return
            slot_hint = body["slot_hint"]
            provided_token = body.get("token")
            # Admin can force-reserve an already-reserved slot.  The
            # override runs inside SlotManager.reserve under its lock —
            # resolving/unreserving here raced concurrent reserves.
            is_admin = slots.is_admin_token(self._get_token())
            result = slots.reserve(slot_hint, token=provided_token,
                                   force=is_admin)
            if "error" in result:
                status = 409 if "already reserved" in result["error"] else 404
                self._respond_json(result, status=status)
            else:
                self._respond_json(result)
            return

        # POST /unreserve — release a slot's reservation (admin only).
        if raw_route == "/unreserve":
            if not self._check_admin():
                return
            if not body or "slot" not in body:
                self._respond_error(400, "Missing 'slot' in body.")
                return
            slot_key = body["slot"]
            try:
                slot_id = int(slot_key)
            except (ValueError, TypeError):
                slot_id = slots._resolve_hint(str(slot_key))
            if slot_id is not None and slots.unreserve(slot_id):
                self._respond_ok(f"Slot {slot_id} unreserved.")
            else:
                self._respond_error(404, f"No reservation for slot '{slot_key}'.")
            return

        # /slots/free/<id> — admin token required for active slots;
        # ended slots can be freed without auth (game already disconnected).
        if raw_route.startswith("/slots/free/"):
            key_str = raw_route[len("/slots/free/"):]
            try:
                key: int | str = int(key_str)
            except ValueError:
                key = key_str
            # Allow unauthenticated cleanup for slots that are already
            # disconnected: ended slots or slots whose recorded game PID died.
            if not slots.can_free_without_admin(key):
                if not self._check_admin():
                    return
            freed, free_error = slots.free_with_status(key)
            if freed:
                self._respond_ok(f"Slot '{key_str}' freed.")
            elif free_error == "persistence_failed":
                self._respond_error(
                    503,
                    f"Slot '{key_str}' could not be archived; retry the request.",
                )
            else:
                self._respond_error(404, f"No slot '{key_str}' to free.")
            return

        # Global reset (no slot prefix) — admin only.
        if raw_route == "/reset":
            if not self._check_shim_protocol():
                return
            if not self._check_admin():
                return
            for gs in list(slots.slots.values()):
                gs.reset()
            self._respond_ok("All slots reset.")
            return

        # --- Slot-scoped endpoints ---

        if body is None and raw_route not in ("/reset",):
            # Check if it's a slot-prefixed reset.
            _, route_check, _ = self._resolve_slot()
            if route_check != "/reset":
                self._respond_error(400, "Invalid or missing JSON body.")
                return

        if raw_route == "/inventory":
            self._respond_error(
                400,
                "Inventory updates require an explicit slot prefix.",
            )
            return

        route, gs, err = self._resolve_slot()
        if gs is None:
            self._respond_error(400, err or "No game state available.")
            return

        # Token check for slot writes.  /event and /request (shim pushes)
        # are gated too: a forged POST /event with type=game_ended frees
        # the slot, and forged /request events poison the transcript.
        # The shim authenticates with the token handed to the game
        # process via the VNFLIGHT_SLOT_TOKEN environment variable.
        _GATED_ROUTES = {"/act", "/command", "/config", "/inventory",
                         "/reset", "/event", "/request"}
        if route in _GATED_ROUTES:
            if not self._check_slot_access(gs):
                return
        if route in {"/event", "/request", "/reset"}:
            if not self._check_shim_protocol():
                return

        if route == "/event":
            seq = gs.push_event(body)  # type: ignore[arg-type]
            if seq is None:
                self._respond_error(409, "Slot is closed.")
                return
            self._respond_json({"seq": seq})

            # Auto-free slot when game ends.
            if body and body.get("type") == "game_ended":  # type: ignore[union-attr]
                reason = body.get("reason", "unknown")
                slot_id = slots.find_slot_id(gs)
                if slot_id is not None:
                    game_id = slots.slot_to_game.get(slot_id, "?")
                    if reason == "return_to_menu":
                        # Game process still alive — keep slot so client can
                        # poll /status and see "ended" before freeing.
                        if gs.end_on_menu_return:
                            print(f"[slots] Game '{game_id}' returned to menu — slot {slot_id} marked ended (kept)")
                        else:
                            print(f"[slots] Game '{game_id}' returned to menu — ignored (end_on_menu_return=false), slot {slot_id} kept")
                    else:
                        print(f"[slots] Game '{game_id}' ended ({reason}) — freeing slot {slot_id}")
                        freed, free_error = slots.free_with_status(slot_id)
                        if not freed and free_error == "persistence_failed":
                            print(
                                f"[slots] Slot {slot_id} archive failed; "
                                "keeping it active for retry.",
                                file=sys.stderr,
                            )
                        if freed and slots.is_empty():
                            print("[slots] All games disconnected — scheduling shutdown.")
                            _schedule_shutdown(self.server)

        elif route == "/request":
            if gs.set_pending_request(body):  # type: ignore[arg-type]
                self._respond_ok("Request registered.")
            else:
                self._respond_error(409, "Slot is closed.")

        elif route == "/act":
            success, message = gs.submit_action(body)  # type: ignore[arg-type]
            if success:
                self._respond_json({"status": "accepted", "message": message})
            else:
                self._respond_error(409, message)

        elif route == "/command":
            success, message, ack = gs.submit_command_with_ack(body)  # type: ignore[arg-type]
            if success:
                self._respond_json({
                    "status": "accepted", "message": message, **ack,
                })
            else:
                # 503 when recovery storage is the problem (retry the same
                # nonce), 409 for a state conflict the caller can wait out or
                # must resolve — a full queue, a stale generation, a closed
                # slot, another act still in flight — and 400 only for a
                # malformed command.  action_in_flight is a conflict, not a
                # client error: the request is well-formed and becomes valid
                # once the prior transaction settles.  client.py treats 400 and
                # 409 identically (no retry, return the body), so the change is
                # transparent to it.
                if ack.get("reason") in {"persistence_error", "storage_error"}:
                    status = 503
                elif (
                    "queue is full" in message.lower()
                    or ack.get("reason") in {
                        "stale_generation", "slot_closed", "action_in_flight",
                        "stale_surface", "admission_revoked_before_dispatch",
                    }
                ):
                    status = 409
                else:
                    status = 400
                self._respond_json({"error": message, **ack}, status=status)

        elif route == "/config":
            if body:
                config = body.get("config", {})
                updated_config = gs.update_config(config)
                if updated_config is None:
                    self._respond_error(409, "Slot is closed.")
                    return
                self._respond_json(
                    {
                        "status": "accepted",
                        "config": updated_config,
                    }
                )
            else:
                self._respond_json(
                    {
                        "status": "ok",
                        "config": {
                            "auto_advance": gs.auto_advance,
                            "auto_advance_delay": gs.auto_advance_delay,
                            "end_on_menu_return": gs.end_on_menu_return,
                        },
                    }
                )

        elif route == "/reset":
            if gs.reset():
                self._respond_ok("State reset.")
            else:
                self._respond_error(409, "Slot is closed.")

        elif route == "/inventory":
            success, message = gs.submit_inventory_change(body)  # type: ignore[arg-type]
            if success:
                self._respond_json({"status": "accepted", "message": message})
            else:
                self._respond_error(409, message)

        else:
            self._respond_error(404, f"Unknown route: {raw_route}")


# ---------------------------------------------------------------------------
# Threaded HTTP Server
# ---------------------------------------------------------------------------


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """An HTTP server that handles each request in a new thread."""

    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        # Windows SO_REUSEADDR permits a second listener on the same endpoint;
        # endpoint ownership also protects the bridge's durable storage.
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
    daemon_threads = True
    verbose: bool = False
    # Extra hostnames accepted in the Host header (beyond localhost
    # names and IP literals).  Populated from the bind host and the
    # VNFLIGHT_ALLOWED_HOSTS environment variable (comma-separated).
    allowed_hosts: set[str] = set()


def _extra_allowed_hosts(bind_host: str) -> set[str]:
    hosts = set()
    if bind_host and bind_host not in ("0.0.0.0", "::"):
        hosts.add(bind_host.lower())
    env_hosts = os.environ.get("VNFLIGHT_ALLOWED_HOSTS", "")
    for h in env_hosts.split(","):
        h = h.strip().lower()
        if h:
            hosts.add(h)
    return hosts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_bridge_server(host: str = "127.0.0.1", port: int = 8385, verbose: bool = True,
                      max_slots: int = 16, admin_token: str | None = None,
                      require_token: bool = False) -> str:
    """Start the bridge server. Returns the admin token."""
    global slots
    server = ThreadedHTTPServer((host, port), BridgeHandler)
    # Bind before opening recovery storage: a second process on this endpoint
    # must fail without reading or modifying the first process's journals.
    endpoint = f"{server.server_address[0].replace(':', '_')}-{server.server_address[1]}"
    storage_dir = os.path.join(os.getcwd(), "bridge", "logs", "endpoints", endpoint)
    try:
        slots = SlotManager(max_slots=max_slots, admin_token=admin_token,
                            require_token=require_token, storage_dir=storage_dir)
    except BaseException:
        server.server_close()
        raise
    server.verbose = verbose
    server.allowed_hosts = _extra_allowed_hosts(host)

    token_display = slots.admin_token[:8] + "..."
    mode = "require-token" if require_token else "open"
    print("+----------------------------------------------+")
    print(f"|{'   vnflight bridge server v' + __version__:<46}|")
    print("+----------------------------------------------+")
    print(f"|  Listening on http://{host}:{port:<5}           |")
    print(f"|  Admin token: {token_display:<31}|")
    print(f"|  Mode: {mode:<37}|")
    print("|                                              |")
    print("|  Endpoints (slot-scoped):                    |")
    print("|    GET  /state    -- full game state          |")
    print("|    GET  /pending  -- current pending request  |")
    print("|    POST /act      -- submit a choice/input    |")
    print("|    POST /command  -- send game command        |")
    print("|                                              |")
    print("|  Admin:                                      |")
    print("|    GET  /slots    -- list active game slots   |")
    print("|    POST /reserve  -- reserve a slot (token)   |")
    print("|    POST /unreserve -- release reservation     |")
    print("|                                              |")
    print("|  Waiting for Ren'Py game to connect...       |")
    print("+----------------------------------------------+")
    print()

    slots.start_pid_watchdog(interval=10.0)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down bridge server...")
        server.shutdown()
        # Flush logs for all active slots.
        for gs in slots.slots.values():
            gs._close_log()
        print("Goodbye!")
    return slots.admin_token


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ren'Py LLM Player — Bridge Server",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8385,
        help="Port to bind to (default: 8385)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=True,
        help="Enable verbose request logging (default: True)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=False,
        help="Suppress request logging",
    )
    parser.add_argument(
        "--max-slots",
        type=int,
        default=16,
        help="Maximum concurrent game slots (default: 16)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Admin token (generated if omitted)",
    )
    parser.add_argument(
        "--require-token",
        action="store_true",
        default=False,
        help="Require token for all write operations (default: open)",
    )

    args = parser.parse_args()
    verbose = args.verbose and not args.quiet

    run_bridge_server(host=args.host, port=args.port, verbose=verbose,
               max_slots=args.max_slots, admin_token=args.token,
               require_token=args.require_token)


if __name__ == "__main__":
    main()
