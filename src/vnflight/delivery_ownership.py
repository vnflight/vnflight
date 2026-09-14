"""Cross-lane action occurrence ownership.

This component owns generation-qualified delivery decisions, not transport or
rendering. BridgeClient inherits it so existing policy overrides remain live.
Held/prefetched rows are not retired here: their lane owns returning them even
when polling has already booked an occurrence in this ledger.
"""

from __future__ import annotations


# --- Prefetch chronology classification -------------------------------------
# Single source of truth for what a parked prefetch row means to the agent's
# reading order.  Used by claim_prefetched_events() and by the fence in
# claim_prefetched_visible_prefix().
#
# VISIBLE: rows build_wait_data() turns into story/status the agent reads as
# narrative.  A later menu must never be presented ahead of one of these, so
# rescue claims them as a chronological prefix (or defers, when another
# action owns them).
_VISIBLE_STORY_EVENT_TYPES = frozenset({
    "narration", "dialogue", "auto_skipped", "screen_text",
})
# DECISION: an earlier decision point.  Presenting a newer menu before it is a
# chronology violation the agent cannot recover from, so these always fence,
# whoever owns them.
_DECISION_EVENT_TYPES = frozenset({
    "choice_request", "input_request",
})
# BOOKKEEPING: rows that carry neither story nor a decision.  stats/inventory
# deltas are reconciled against live game_state before rendering; nvl_* and
# context/pause are verbose-only CLI diagnostics; command_result is the shim
# acking its own click; game_state/screenshot/observation_* are sampling
# traffic.  None of them establish an order the agent can perceive, so a
# rescue steps OVER them: they stay parked for the next ordinary drain rather
# than withholding a settled menu.
#
# This is deliberately an allowlist.  Anything else -- scene/show/hide,
# screen_content (which carries scraped `texts`), progress_change (the
# game_terminal latch), game_started/game_ended, anomaly, the *_resolved
# receipts -- still fences, because it either carries text or moves the
# lifecycle.
_PREFETCH_BOOKKEEPING_EVENT_TYPES = frozenset({
    "stats_update", "inventory_update",
    "nvl_clear", "nvl_show", "nvl_hide",
    "command_result", "game_state", "screenshot", "mod_loaded",
    "context", "pause",
    "observation_started", "observation_progress",
})


def _event_seq(event: dict | None) -> int:
    if not isinstance(event, dict):
        return 0
    try:
        return int(event.get("_seq", 0) or 0)
    except (TypeError, ValueError):
        return 0



class ActionDeliveryOwnership:
    def _hold_events(self, events, *, chronological: bool = False) -> None:
        """Park observed rows without booking or acknowledging their delivery."""
        held = list(getattr(self, "_prefetched_events", []) or [])
        held.extend(events)
        if chronological:
            held.sort(key=_event_seq)
        self._prefetched_events = held

    def _restore_held_events(self, events) -> None:
        """Return a detached prefix ahead of rows observed while it was detached."""
        self._prefetched_events = list(events) + list(
            getattr(self, "_prefetched_events", []) or [])

    def _take_held_events(self) -> list[dict]:
        """Transfer the pending batch; the caller must return or restore it."""
        held = list(getattr(self, "_prefetched_events", []) or [])
        self._prefetched_events = []
        return held

    def _clear_held_events(self) -> None:
        """Abandon held rows only at an established lifecycle boundary."""
        self._prefetched_events = []

    def _retain_held_events(self, events) -> None:
        """Keep the unclaimed remainder after a selective ownership transfer."""
        self._prefetched_events = list(events)

    def _claim_prefetched_action_events(
        self,
        action_id: object,
        *,
        include_unowned: bool = False,
        source_id: str | None = None,
        source_after: int | None = None,
        source_through: int | None = None,
    ) -> list[dict]:
        """Take internally observed events for a scoped transaction wait.

        ``_wait_command_result`` polls the ordinary bridge stream while a
        command is in flight. It preserves non-command events for the next
        user-visible wait, but ``poll`` has already entered action-attributed
        occurrences in the cross-path delivery ledger. If that next wait
        auto-selects the still-active transaction, the scoped drain would
        otherwise suppress the durable copies while leaving the prefetched
        copies stranded. A following act is then allowed to discard them as
        pre-action output.

        Prefetch is pending delivery, not proof of delivery. An explicit
        nonce wait transfers only rows owned by that transaction. A plain
        wait that auto-selected the transaction also transfers unattributed
        rows and earlier actions' held output. Earlier actions cannot fence
        the automatic consumer forever: ordinary polling has already booked
        those rows, so leaving them held also suppresses the receipt copies
        behind them. Newer actions still fence the prefix, and explicit nonce
        waits remain scoped to their own action. An explicit wait also claims unattributed
        story inside the transaction's proven shim-source interval. This
        covers a narration row emitted immediately before its successor menu
        without absorbing operator traffic or another game source.
        """
        try:
            owned_action_id = int(action_id or 0)
        except (TypeError, ValueError):
            owned_action_id = 0
        if owned_action_id <= 0 and not include_unowned:
            return []
        claimed = []
        remaining = []
        crossed_foreign_boundary = False
        for event in list(getattr(self, "_prefetched_events", []) or []):
            try:
                event_action_id = int(event.get("action_id", 0) or 0)
            except (AttributeError, TypeError, ValueError):
                event_action_id = 0
            event_in_source_interval = False
            if (
                event_action_id == 0
                and source_id
                and type(source_after) is int
                and type(source_through) is int
                and isinstance(event, dict)
                and event.get("type") in _VISIBLE_STORY_EVENT_TYPES
                and event.get("_source_id") == source_id
                and type(event.get("_source_seq")) is int
                and source_after < event["_source_seq"] <= source_through
            ):
                event_in_source_interval = True
            is_predecessor = bool(
                include_unowned
                and 0 < event_action_id < owned_action_id
            )
            is_foreign = bool(
                event_action_id
                and event_action_id != owned_action_id
                and not is_predecessor
            )
            if crossed_foreign_boundary or is_foreign:
                crossed_foreign_boundary = True
                remaining.append(event)
            elif (
                (include_unowned and event_action_id == 0)
                or is_predecessor
                or event_action_id == owned_action_id
                or event_in_source_interval
            ):
                claimed.append(event)
            else:
                remaining.append(event)
        self._retain_held_events(remaining)
        return claimed

    def claim_prefetched_visible_prefix(
        self,
        before_seq: object,
        *,
        ordinary_action_id: object = None,
        action_scoped: bool = False,
    ) -> tuple[list[dict], bool]:
        """Claim visible prefetch that precedes a later transcript row.

        Transcript rescue can observe a later event while ``wait`` still has
        an earlier occurrence parked for user delivery.  Rescue must expose a
        chronological prefix.  Another action's row is an ownership fence;
        callers must defer the later event rather than crossing it.

        The fence is ownership- and chronology-shaped, not merely
        "unrecognised type".  A parked bookkeeping row (see
        ``_PREFETCH_BOOKKEEPING_EVENT_TYPES``) establishes no order the agent
        can perceive, so it is stepped over -- left parked, unclaimed, and
        non-blocking -- instead of withholding a legitimately settled menu.
        A decision row always fences; foreign-owned story fences only for an
        action-scoped caller; malformed rows fence conservatively.
        """
        try:
            upper = int(before_seq or 0)
        except (TypeError, ValueError):
            return [], False
        if upper <= 0:
            return [], False
        try:
            allowed_action_id = int(ordinary_action_id or 0)
        except (TypeError, ValueError):
            allowed_action_id = 0

        prefetched = list(getattr(self, "_prefetched_events", []) or [])
        claim_indexes = []
        for index, event in enumerate(prefetched):
            if not isinstance(event, dict):
                return [], True
            try:
                seq = int(event.get("_seq", 0) or 0)
                event_action_id = int(event.get("action_id", 0) or 0)
            except (TypeError, ValueError):
                return [], True
            if seq <= 0:
                return [], True
            if seq >= upper:
                continue
            event_type = event.get("type")
            if event_type in _PREFETCH_BOOKKEEPING_EVENT_TYPES:
                # Carries no story and no decision: stepping over it costs the
                # agent nothing and keeps the settled menu deliverable now.
                continue
            if event_type in _DECISION_EVENT_TYPES:
                # An earlier decision the agent has not seen: a newer menu
                # must not be presented ahead of it, whoever owns it.
                return [], True
            if event_type not in _VISIBLE_STORY_EVENT_TYPES:
                # Unclassified: conservatively fence rather than guess.
                return [], True
            if (
                event_action_id
                and action_scoped
                and event_action_id != allowed_action_id
            ):
                return [], True
            claim_indexes.append(index)

        claimed = [prefetched[index] for index in claim_indexes]
        claimed_index_set = set(claim_indexes)
        self._retain_held_events([
            event for index, event in enumerate(prefetched)
            if index not in claimed_index_set
        ])
        delivered_keys = [
            (int(event.get("action_id", 0) or 0), int(event["_seq"]))
            for event in claimed
            if int(event.get("action_id", 0) or 0) > 0
        ]
        self._record_delivered_action_events(delivered_keys)
        claimed.sort(key=_event_seq)
        return claimed, False

    # Bound on the delivered-action-event ledger. It is generation-qualified
    # because action/sequence coordinates can recur after a load, and it is no
    # longer pruned by the ordinary cursor (a scoped drain can arrive later).
    _MAX_DELIVERED_ACTION_EVENTS = 4096

    def _action_delivery_generation(
        self, reset_generation: object = None,
    ) -> int | None:
        if reset_generation is None:
            reset_generation = getattr(
                self, "_action_delivery_reset_generation", None)
        try:
            return (
                int(reset_generation)
                if reset_generation is not None else None
            )
        except (TypeError, ValueError):
            return None

    def _action_event_was_delivered(
        self, key: tuple[int, int], reset_generation: object = None,
    ) -> bool:
        generation = self._action_delivery_generation(reset_generation)
        ownership = getattr(
            self, "_delivered_action_event_ownership", set())
        if (generation, key[0], key[1]) in ownership:
            return True
        # Preserve compatibility with callers/tests that seed the historical
        # pair set directly. Attribute such entries to the current timeline.
        if key in self._delivered_action_events and not any(
            owned_action == key[0] and owned_seq == key[1]
            for _owned_generation, owned_action, owned_seq in ownership
        ):
            owned_pairs = {
                (owned_action, owned_seq)
                for _owned_generation, owned_action, owned_seq in ownership
            }
            ownership.update(
                (generation, action_id, seq)
                for action_id, seq in self._delivered_action_events
                if (action_id, seq) not in owned_pairs
            )
            self._delivered_action_event_ownership = ownership
            self._trim_action_event_delivery_ownership()
            return True
        return False

    def _trim_action_event_delivery_ownership(self) -> None:
        ownership = self._delivered_action_event_ownership
        overflow = len(ownership) - self._MAX_DELIVERED_ACTION_EVENTS
        if overflow > 0:
            ordered = sorted(
                ownership,
                key=lambda item: (
                    -1 if item[0] is None else item[0], item[1], item[2],
                ),
            )
            for owned in ordered[:overflow]:
                ownership.discard(owned)
        self._delivered_action_events = {
            (action_id, seq)
            for _generation, action_id, seq in ownership
        }

    def _record_delivered_action_events(
        self, keys, *, reset_generation: object = None,
    ) -> None:
        generation = self._action_delivery_generation(reset_generation)
        ownership = getattr(
            self, "_delivered_action_event_ownership", set())
        for key in keys:
            self._delivered_action_events.add(key)
            ownership.add((generation, key[0], key[1]))
        self._delivered_action_event_ownership = ownership
        self._trim_action_event_delivery_ownership()

    def claim_undelivered_action_events(
        self, events: list[dict], *, reset_generation: object = None,
    ) -> list[dict]:
        """Claim action-attributed occurrences for one presentation path.

        Transcript look-ahead is outside both the ordinary and scoped event
        lanes. It must share their occurrence ledger or it can replay rows a
        transaction receipt already presented. Unattributed rows pass through
        unchanged; positive ``(action_id, _seq)`` pairs are claimed once.
        """
        claimed = []
        claimed_keys = []
        for event in events:
            try:
                key = (
                    int(event.get("action_id", 0) or 0),
                    int(event.get("_seq", 0) or 0),
                )
            except (AttributeError, TypeError, ValueError):
                key = (0, 0)
            if key[0] > 0 and key[1] > 0:
                if self._action_event_was_delivered(
                    key, reset_generation=reset_generation,
                ):
                    continue
                claimed_keys.append(key)
            claimed.append(event)
        self._record_delivered_action_events(
            claimed_keys, reset_generation=reset_generation,
        )
        return claimed

    def reset_action_event_delivery(self) -> None:
        """Discard occurrence ownership from the abandoned timeline."""
        self._delivered_action_events.clear()
        self._delivered_action_event_ownership.clear()
        self._action_delivery_reset_generation = None

    def _observe_action_delivery_generation(
        self, reset_generation: object,
    ) -> None:
        """Retire occurrence ownership only on a newer durable timeline.

        ``/state`` is the authoritative source for the bridge's current reset
        generation. Replayable ``/transaction`` receipts retain the generation
        in which their action began; adopting that value can move this ledger
        backward and make every alternating read clear ownership again.
        """
        try:
            generation = int(reset_generation)
        except (TypeError, ValueError):
            return
        previous_generation = getattr(
            self, "_action_delivery_reset_generation", None)
        ownership = getattr(
            self, "_delivered_action_event_ownership", set())
        if previous_generation is None:
            ownership = {
                (
                    generation if owned_generation is None else owned_generation,
                    action_id,
                    seq,
                )
                for owned_generation, action_id, seq in ownership
            }
            self._delivered_action_event_ownership = ownership
            self._action_delivery_reset_generation = generation
            return
        if generation <= previous_generation:
            return
        self._action_delivery_reset_generation = generation
        ownership = {
            owned
            for owned in ownership
            if owned[0] is not None and owned[0] >= generation
        }
        self._delivered_action_event_ownership = ownership
        self._delivered_action_events = {
            (action_id, seq)
            for _owned_generation, action_id, seq in ownership
        }
