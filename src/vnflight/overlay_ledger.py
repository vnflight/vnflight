"""Pure ownership policy for deferred passive-overlay occurrences.

Handlers decide when rows are safe to present. This module owns the smaller,
state-only contract: bounded pending queues and replay receipts must advance
together so an evicted row can be recovered instead of silently dropped.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any


def hold_pending_deliveries(
    pending: list[dict],
    durable_receipts: MutableMapping[tuple[str, str, int], None],
    provisional_deliveries: list[dict],
    recovered_occurrences: MutableMapping[tuple[int, str, int], None],
    deliveries: list[dict],
    limit: int,
) -> None:
    """Append deliveries and release receipts for bounded-queue evictions."""
    if not deliveries:
        return
    pending.extend(deliveries)
    overflow = len(pending) - limit
    if overflow <= 0:
        return
    evicted = pending[:overflow]
    del pending[:overflow]
    for record in evicted:
        receipt = record.get("_durable_receipt")
        if isinstance(receipt, tuple):
            durable_receipts.pop(receipt, None)
        delivery_id = record.get("id")
        provisional_deliveries[:] = [
            provisional
            for provisional in provisional_deliveries
            if provisional.get("id") != delivery_id
        ]
        recovered = record.get("_recovered_occurrence")
        if isinstance(recovered, tuple):
            recovered_occurrences.pop(recovered, None)


def register_receipt(
    receipts: MutableMapping[tuple[str, str, int], None],
    receipt: tuple[str, str, int],
) -> bool:
    """Book a receipt once and report whether the occurrence is fresh."""
    if receipt in receipts:
        return False
    receipts[receipt] = None
    return True


def trim_oldest(
    mapping: MutableMapping[Any, None],
    *,
    limit: int,
    protected: set[Any] | None = None,
) -> None:
    """Bound insertion-ordered occurrence ledgers while retaining the newest."""
    if len(mapping) <= limit:
        return
    protected = protected or set()
    remove = max(0, len(mapping) - limit)
    removed = 0
    for key in list(mapping):
        if key in protected:
            continue
        mapping.pop(key, None)
        removed += 1
        if removed >= remove:
            break


def trim_provisional_deliveries(
    deliveries: list[dict],
    *,
    limit: int,
    protected_ids: set[Any] | None = None,
) -> None:
    """Bound unowned markers without counting protected pending owners."""
    protected_ids = protected_ids or set()
    unprotected_count = sum(
        record.get("id") not in protected_ids for record in deliveries)
    overflow = unprotected_count - limit
    if overflow <= 0:
        return
    kept = []
    removed = 0
    for record in deliveries:
        if removed < overflow and record.get("id") not in protected_ids:
            removed += 1
            continue
        kept.append(record)
    deliveries[:] = kept
