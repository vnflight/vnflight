"""Shared helpers for waiting until scraped/rendered state stabilizes."""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar
from .delivery_ownership import ActionDeliveryOwnership

T = TypeVar("T")


def button_labels(screen: dict | None) -> list[str]:
    """Return non-empty button labels from a screen snapshot."""
    if not screen:
        return []
    return [
        str(btn.get("label", "")).strip()
        for btn in screen.get("buttons", [])
        if str(btn.get("label", "")).strip()
    ]


def is_single_enter_screen(screen: dict | None) -> bool:
    """True for Roadwarden's transient one-button Enter shell."""
    return button_labels(screen) == ["Enter"]


def restore_prefetched_events(client: Any, events: list[dict]) -> None:
    """Put internally-polled events back so the next wait can format them."""
    if not events:
        return
    try:
        ActionDeliveryOwnership._restore_held_events(client, events)
    except Exception:
        pass


def poll_preserving_events(
    client: Any,
    *,
    timeout: float = 0.0,
    include_prefetched: bool = True,
) -> list[dict]:
    """Poll a client without consuming events from the caller's next wait."""
    events = client.poll(timeout=timeout, include_prefetched=include_prefetched)
    restore_prefetched_events(client, events)
    return events


def _utility_selection_signature(items: list[dict]) -> tuple:
    """Selected controls change state; highlighted story answers do not."""
    return tuple(
        (item.get("screen", ""),
         item.get("display_label", item.get("label", "")),
         bool(item["is_selected"]))
        for item in items
        if isinstance(item, dict)
        and "is_selected" in item
        and item.get("type") not in {"choice", "input"}
        and not set(item.get("action_names") or item.get("actions") or []).intersection(
            {"Return", "ChoiceReturn", "Jump", "Call", "Start"})
    )


def screen_signature(screen: dict | None) -> tuple:
    """Compact signature for screen_content changes."""
    if not screen:
        return ()
    return (
        tuple(screen.get("screens") or []),
        bool(screen.get("overlay_active")),
        tuple(screen.get("modal_screens") or []),
        tuple(
            (
                b.get("label"),
                b.get("screen"),
                tuple(b.get("actions") or []),
                tuple(b.get("action_strs") or []),
            )
            for b in screen.get("buttons") or []
        ),
        tuple(
            (
                i.get("display_label"),
                i.get("type"),
                i.get("category"),
                bool(i.get("disabled")),
            )
            for i in screen.get("interactions") or []
        ),
        tuple(str(t).strip() for t in screen.get("texts") or []),
        _utility_selection_signature(screen.get("interactions") or []),
        _utility_selection_signature(screen.get("buttons") or []),
    )


def rendered_state_signature(rendered: dict | None) -> tuple:
    """Compact signature for formatted state/wait changes."""
    rendered = rendered or {}
    data = rendered.get("_data") or {}
    pending = data.get("pending") or {}
    buttons = data.get("buttons") or []
    return (
        data.get("status"),
        pending.get("id"),
        tuple(
            (
                b.get("label"),
                b.get("screen", ""),
                bool(b.get("disabled")),
            )
            for b in buttons
        ),
        rendered.get("text"),
        rendered.get("pending"),
        rendered.get("buttons"),
        rendered.get("brief"),
        rendered.get("_footer"),
        _utility_selection_signature(data.get("_interactions") or []),
        _utility_selection_signature(buttons),
    )


def wait_for_stable_change(
    *,
    fetch: Callable[[], T | None],
    signature: Callable[[T | None], tuple],
    initial: T | None = None,
    initial_signature: tuple | None = None,
    timeout: float = 3.0,
    settle_delay: float = 0.6,
    poll_interval: float = 0.3,
    accept: Callable[[T], bool] | None = None,
    return_initial_when_stable: bool = False,
) -> T | None:
    """Return the latest value after it changes and remains stable briefly.

    If no stable change appears before timeout, return the last changed value,
    then the last observed value, then the initial value.
    """
    current = initial
    current_sig = initial_signature
    if current is not None and current_sig is None:
        current_sig = signature(current)

    last_observed = current
    changed_value = None
    changed_sig = None
    changed_at = None
    initial_stable_since = time.time()
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(poll_interval)
        fresh = fetch()
        if fresh is None:
            continue
        last_observed = fresh
        fresh_sig = signature(fresh)
        if current_sig is not None and fresh_sig == current_sig:
            if (
                return_initial_when_stable
                and accept is None
                and time.time() - initial_stable_since >= settle_delay
            ):
                return fresh
            continue
        initial_stable_since = time.time()
        if accept is not None and not accept(fresh):
            continue
        now = time.time()
        if fresh_sig != changed_sig:
            changed_value = fresh
            changed_sig = fresh_sig
            changed_at = now
            continue
        if changed_at is None or now - changed_at >= settle_delay:
            return fresh

    return changed_value if changed_value is not None else last_observed
