"""Presentation-lane ownership for public tool calls.

Every public call that can consume or expose story state runs inside one lane
per handler context, so overlapping requests cannot rebind, consume, or mutate
a single game's timeline out of order. This module owns that scope end to end:
who may hold the lane, how a lifecycle call preempts a wedged owner, the epoch
fence that stops an abandoned handler from publishing, and the single boundary
at which deferred overlay rows are released.

``handlers.handle_tool`` is the thin dispatcher over ``run_public_tool``;
nothing here imports handlers, and the handler to run is passed in.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .overlay_presentation import (
    _ACTIVE_PRESENTATION_INVOCATION,
    _merge_passive_overlay_text,
    _reset_timeline_context,
)


def _preemptible_on_events(
    ctx: Any,
    on_events: Callable[[list[dict]], Any] | None,
) -> Callable[[list[dict]], Any]:
    """Wrap a wait callback so a lifecycle preemption can end the wait.

    ``BridgeClient.wait`` already supports a caller-driven early exit: any
    truthy ``on_events`` return (including on the idle tick, which runs once
    per main-loop iteration) makes it return ``interrupted=True`` with the
    events observed so far. Reusing that contract is what lets ``stop``
    reclaim the presentation lane from a long ``wait`` without inventing a
    second, less-tested abort path inside the client.

    The preempt flag is checked before delegating: the client swallows
    exceptions raised by ``on_events``, so a broken caller callback must not
    be able to mask the interrupt.
    """
    def _hook(batch: list[dict]) -> Any:
        if ctx._presentation_lane_preempt.is_set():
            return True
        if on_events is None:
            return None
        return on_events(batch)

    return _hook


# These tools touch the live client. They share one lane per HandlerContext so
# overlapping requests cannot rebind, consume, or mutate a single game's
# timeline out of order. Serialization alone does not grant overlay ownership.
_PRESENTATION_RESULT_TOOLS = {
    "wait",
    "act",
    "input_text",
    "screenshot",
    "state",
    "transcript",
    "back",
    "back_all",
    "advance",
    "rewind",
    "replay",
    "load",
    "save",
    "auto_skip",
    "command",
    "inspect",
    "launch",
    "stop",
    "set_profile",
    "progress",
}


# Lifecycle tools present no story of their own, and they are the only escape
# from a wedged lane: refusing them is what forced "restart the owning MCP
# process" on an agent whose `wait timeout=300` was stuck on a dead game.
# They preempt instead of refusing — see _acquire_presentation_lane_or_preempt.
_PRESENTATION_LIFECYCLE_TOOLS = {
    "launch",
    "stop",
}


# Fallback bound for how long a lifecycle tool waits for the interrupted owner
# to hand the lane back. Per-context override: _presentation_preempt_wait_seconds.
_PRESENTATION_LANE_PREEMPT_WAIT_S = 8.0


# Response budget kept back for the lifecycle call itself. handle_stop spends
# up to 15s in its CLI child; leave that plus margin rather than burning the
# caller's whole transport budget waiting for a lane that may never come back.
_PRESENTATION_LANE_PREEMPT_RESERVE_S = 20.0


# Only results composed from live wait output may claim deferred overlay rows.
# State/transcript/debug/lifecycle responses are serialized above, but their
# flat result shapes cannot place a bridge-sequenced row safely.
_OVERLAY_PRESENTATION_RESULT_TOOLS = {
    "wait",
    "act",
    "input_text",
    "state",
}


def _flush_public_presentation_rows(
    ctx: Any,
    result: dict,
    params: dict,
) -> None:
    """Claim safe ledger rows at the serialized public result boundary."""
    # The presentation-call lock excludes every production caller of the
    # overlay merge here. This RLock acquisition is consequently uncontended;
    # unlike the earlier handler merge, the operation performs no bridge I/O.
    _merge_passive_overlay_text(
        ctx,
        result,
        screen=None,
        fmt=params.get("format", "text"),
        deadline=params.get("_result_deadline"),
        sample_live=False,
        release_owner_fences=True,
    )


def _presentation_call_busy_result(ctx: Any | None = None) -> dict:
    active_tool = None
    active_for = 0.0
    watchdog_seconds = 305.0
    if ctx is not None:
        active_tool = ctx._presentation_call_name
        started_at = float(ctx._presentation_call_started_at or 0.0)
        if started_at > 0:
            active_for = max(0.0, time.monotonic() - started_at)
        watchdog_seconds = float(
            ctx._presentation_call_watchdog_seconds or watchdog_seconds)
    watchdog_at = float(
        getattr(ctx, "_presentation_call_watchdog_at", 0.0) or 0.0
    ) if ctx is not None else 0.0
    watchdog_exceeded = (
        time.monotonic() >= watchdog_at
        if watchdog_at > 0 else active_for >= watchdog_seconds
    )
    owner = active_tool or "unknown presentation tool"
    error = (
        "Did not consume or mutate game state - an earlier presentation "
        "call ({}) has been active for {:.1f}s. Retry after it returns."
    ).format(owner, active_for)
    if watchdog_exceeded:
        error += (
            " The presentation-lane watchdog threshold was exceeded; "
            "call stop (or launch) to preempt it - lifecycle tools take the "
            "lane over instead of refusing."
        )
    return {
        "ok": False,
        "success": False,
        "reason": "presentation_call_in_flight",
        "error": error,
        "active_tool": active_tool,
        "active_for_seconds": round(active_for, 3),
        "watchdog_exceeded": watchdog_exceeded,
    }


def _presentation_watchdog_at(
    ctx: Any,
    name: str,
    params: dict,
    started_at: float,
) -> float:
    """Return the monotonic point after which a call is unexpectedly live."""
    grace = 5.0
    result_deadline = params.get("_result_deadline")
    if isinstance(result_deadline, (int, float)):
        remaining = max(0.0, float(result_deadline) - time.time())
        return started_at + remaining + grace
    if name == "wait":
        try:
            wait_budget = max(0.0, float(params.get("timeout", 60) or 0))
        except (TypeError, ValueError):
            wait_budget = 60.0
        # Direct callers do not carry the MCP result deadline. Include the
        # wait's bounded post-observation composition plus scheduling margin.
        return started_at + wait_budget + grace
    return started_at + float(
        ctx._presentation_call_watchdog_seconds or 305.0)


def _presentation_preempt_budget(
    ctx: Any, params: dict,
) -> float:
    """Return how long a lifecycle call may wait for the wedged lane owner."""
    cap = float(
        getattr(ctx, "_presentation_preempt_wait_seconds", 0.0)
        or _PRESENTATION_LANE_PREEMPT_WAIT_S
    )
    # The MCP servers stamp the transport/response budget for this call onto
    # params. Holding the response back past it would turn a recoverable stop
    # into a transport timeout, so the caller's own deadline bounds the wait.
    deadline = params.get("_response_deadline")
    if not isinstance(deadline, (int, float)):
        deadline = params.get("_transport_deadline")
    if isinstance(deadline, (int, float)):
        cap = min(
            cap,
            float(deadline) - time.time()
            - _PRESENTATION_LANE_PREEMPT_RESERVE_S,
        )
    return max(0.0, cap)


def _acquire_presentation_lane_or_preempt(
    ctx: Any, params: dict,
) -> bool:
    """Take the presentation lane for a lifecycle call, or preempt it.

    Returns True when the lane is held (release it as usual) and False when
    the caller must proceed without it.

    Why lifecycle calls do not simply refuse, and why they do not simply
    bypass either:

    * Refusing is what the watchdog message admitted was a dead end. A wait
      does not observe the game going away on its own: ``BridgeClient.wait``
      loops until its own deadline, and a stopped game merely makes its polls
      return nothing, so `wait timeout=300` can hold the lane for five minutes
      after the game it was watching is unreachable.
    * Bypassing outright would let ``stop`` free the slot underneath a live
      observation, and the abandoned wait would then compose and *release*
      overlay rows belonging to a game that no longer exists - into the ctx
      that the next launch inherits.

    So: ask first, take second. The preempt flag makes the live wait return
    ``interrupted=True`` at its next poll, which is a clean, already-supported
    exit; only if the owner is genuinely wedged past that bounded window do we
    proceed anyway, and then the epoch bump fences the abandoned handler out
    of publication (see handle_tool).
    """
    if ctx._presentation_call_lock.acquire(blocking=False):
        ctx._presentation_lane_preempt.clear()
        return True
    ctx._presentation_lane_preempt.set()
    budget = _presentation_preempt_budget(ctx, params)
    if budget > 0 and ctx._presentation_call_lock.acquire(timeout=budget):
        ctx._presentation_lane_preempt.clear()
        return True
    # Still wedged. Take over: everything composed across this bump belongs to
    # an abandoned timeline. Leave the preempt flag set so the stuck owner
    # keeps being asked to unwind; the next lane acquisition clears it.
    # A monotonic fence read by the late owner in its finally block; the
    # single increment needs no lock (the serial machinery that once guarded
    # it is gone with the serialized lane).
    ctx._presentation_lane_epoch += 1
    return False


def _discard_abandoned_timeline(ctx: Any) -> None:
    """Drop ledger state written for a timeline a preemption abandoned."""
    _reset_timeline_context(ctx, clear_client_prefetch=True)
    ctx.overlay.timeline_source_sequences = {}


def _run_preempted_lifecycle_call(
    ctx: Any,
    handler: Callable[[Any, dict], dict],
    params: dict,
) -> dict:
    """Run a lifecycle handler that had to proceed without the lane.

    ``ctx.hooks.ensure_attached`` is deliberately skipped here: lazy
    attachment mutates the client binding, and by construction some other
    call already owns the lane, so it has already attached. Running it from a
    second thread would be the one client mutation this path can avoid.
    """
    try:
        result = handler(ctx, params)
    finally:
        # The wedged owner may still be composing. Reset now so its slot is
        # not carried forward, and note that it resets again on its own way
        # out (handle_tool's epoch check) if it writes after this point.
        _discard_abandoned_timeline(ctx)
    if isinstance(result, dict):
        return {**result, "presentation_lane_preempted": True}
    return result


def run_presentation_transition(
    ctx: Any,
    callback: Callable[[], dict],
    *,
    reset_context: bool = False,
    transition_name: str = "binding_transition",
    preemptible: bool = False,
    params: dict | None = None,
) -> dict:
    """Run a client-binding transition without racing a live tool call.

    *preemptible* opts a caller-initiated transition (bridge_connect) into the
    same lifecycle escape as stop/launch: rebinding the client is how an agent
    recovers from a bridge that stopped answering, so it must not be gated
    behind the very wait that the dead bridge wedged.
    """
    preempted = False
    if preemptible:
        if not _acquire_presentation_lane_or_preempt(ctx, params or {}):
            preempted = True
    elif not ctx._presentation_call_lock.acquire(blocking=False):
        return _presentation_call_busy_result(ctx)
    else:
        ctx._presentation_lane_preempt.clear()
    if preempted:
        try:
            result = callback()
        finally:
            _discard_abandoned_timeline(ctx)
        if isinstance(result, dict):
            return {**result, "presentation_lane_preempted": True}
        return result
    ctx._presentation_call_name = transition_name
    ctx._presentation_call_started_at = time.monotonic()
    ctx._presentation_call_watchdog_at = (
        ctx._presentation_call_started_at
        + float(ctx._presentation_call_watchdog_seconds or 305.0)
    )
    try:
        binding_before = (
            getattr(ctx.client, "bridge_url", None),
            getattr(ctx.client, "slot_prefix", None),
        )
        result = None
        try:
            result = callback()
            return result
        finally:
            binding_after = (
                getattr(ctx.client, "bridge_url", None),
                getattr(ctx.client, "slot_prefix", None),
            )
            applied_forced_reset = bool(
                reset_context
                and isinstance(result, dict)
                and result.get("ok") is True
            )
            if applied_forced_reset or binding_after != binding_before:
                _reset_timeline_context(ctx, clear_client_prefetch=True)
                ctx.overlay.timeline_source_sequences = {}
    finally:
        ctx._presentation_call_name = None
        ctx._presentation_call_started_at = 0.0
        ctx._presentation_call_watchdog_at = 0.0
        ctx._presentation_call_lock.release()


def run_public_tool(
    ctx: Any,
    name: str,
    params: dict,
    handler: Callable[[Any, dict], dict],
) -> dict:
    """Run one public tool call inside the serialized presentation lane.

    ``handlers.handle_tool`` resolves the handler and delegates here; the
    ownership rules — serialization, lifecycle preemption, the epoch fence and
    the single row-release boundary — all live in this module.
    """
    if _ACTIVE_PRESENTATION_INVOCATION.get() == id(ctx):
        # Re-entrant dispatch on the context that already owns the lane. The
        # presentation-call lock is not reentrant, so run inside the caller's
        # scope rather than deadlocking against it.
        return handler(ctx, params)
    if name not in _PRESENTATION_RESULT_TOOLS:
        return handler(ctx, params)

    if name in _PRESENTATION_LIFECYCLE_TOOLS:
        # stop/launch present no story and are the only way out of a wedged
        # lane, so they interrupt the owner and, if that fails, take over.
        if not _acquire_presentation_lane_or_preempt(ctx, params):
            return _run_preempted_lifecycle_call(ctx, handler, params)
    # Never queue overlapping calls: threading.Lock waiter selection is not
    # FIFO, and a retry could otherwise overtake the request it replaces.
    # Refusal occurs before the handler consumes or mutates any game state.
    elif not ctx._presentation_call_lock.acquire(blocking=False):
        return _presentation_call_busy_result(ctx)
    else:
        ctx._presentation_lane_preempt.clear()

    try:
        lane_epoch = ctx._presentation_lane_epoch
        ctx._presentation_call_name = name
        ctx._presentation_call_started_at = time.monotonic()
        ctx._presentation_call_watchdog_at = _presentation_watchdog_at(
            ctx, name, params, ctx._presentation_call_started_at)
        token = _ACTIVE_PRESENTATION_INVOCATION.set(id(ctx))
        try:
            binding_before = (
                getattr(ctx.client, "bridge_url", None),
                getattr(ctx.client, "slot_prefix", None),
            )
            if ctx.hooks.ensure_attached is not None:
                ctx.hooks.ensure_attached()
            binding_after = (
                getattr(ctx.client, "bridge_url", None),
                getattr(ctx.client, "slot_prefix", None),
            )
            if binding_after != binding_before:
                _reset_timeline_context(ctx, clear_client_prefetch=True)
                ctx.overlay.timeline_source_sequences = {}
            result = handler(ctx, params)
            if ctx._presentation_lane_epoch != lane_epoch:
                # A lifecycle call gave up on this handler and stopped or
                # rebound the game underneath it. Whatever it composed
                # describes a timeline that is gone: never release its
                # deferred rows, and discard the ledger state it wrote after
                # the preemption already reset the context. The caller still
                # gets its own result, marked so it is not mistaken for a
                # live view.
                _discard_abandoned_timeline(ctx)
                if isinstance(result, dict):
                    return {**result, "presentation_lane_superseded": True}
                return result
            if (
                isinstance(result, dict)
                and name in _OVERLAY_PRESENTATION_RESULT_TOOLS
            ):
                # Nested waits may book rows, but the serialized composed
                # response is the only boundary allowed to release them.
                _flush_public_presentation_rows(ctx, result, params)
            return result
        finally:
            _ACTIVE_PRESENTATION_INVOCATION.reset(token)
    finally:
        ctx._presentation_call_name = None
        ctx._presentation_call_started_at = 0.0
        ctx._presentation_call_watchdog_at = 0.0
        ctx._presentation_call_lock.release()
