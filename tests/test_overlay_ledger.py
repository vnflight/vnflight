from vnflight.overlay_ledger import (
    hold_pending_deliveries,
    register_receipt,
    trim_oldest,
    trim_provisional_deliveries,
)


def test_eviction_releases_only_the_evicted_rows_receipt():
    first = ("source", "10", 0)
    second = ("source", "11", 0)
    receipts = {first: None, second: None}
    pending = []

    hold_pending_deliveries(pending, receipts, [], {}, [
        {"text": "first", "_durable_receipt": first},
        {"text": "second", "_durable_receipt": second},
    ], 1)

    assert pending == [{"text": "second", "_durable_receipt": second}]
    assert receipts == {second: None}


def test_eviction_releases_provisional_and_recovered_ownership():
    recovered = (12, "same", 1)
    provisional = [
        {"id": 7, "text": "same"},
        {"id": 8, "text": "keep"},
    ]
    recovered_occurrences = {recovered: None}
    pending = []

    hold_pending_deliveries(
        pending,
        {},
        provisional,
        recovered_occurrences,
        [{
            "id": 7,
            "text": "same",
            "_recovered_occurrence": recovered,
        }],
        0,
    )

    assert pending == []
    assert provisional == [{"id": 8, "text": "keep"}]
    assert recovered_occurrences == {}


def test_receipt_registration_is_idempotent():
    receipts = {}
    receipt = ("source", "10", 0)

    assert register_receipt(receipts, receipt) is True
    assert register_receipt(receipts, receipt) is False
    assert receipts == {receipt: None}


def test_trim_oldest_keeps_the_newest_half_when_over_limit():
    ledger = {index: None for index in range(6)}

    trim_oldest(ledger, limit=4)

    assert list(ledger) == [2, 3, 4, 5]


def test_trim_oldest_never_removes_pending_ownership():
    ledger = {index: None for index in range(6)}

    trim_oldest(ledger, limit=4, protected={0})

    assert list(ledger) == [0, 3, 4, 5]


def test_trim_oldest_enforces_limit_after_bulk_growth():
    ledger = {index: None for index in range(10_000)}

    trim_oldest(ledger, limit=4096)

    assert len(ledger) == 4096
    assert next(iter(ledger)) == 5904


def test_provisional_trim_preserves_pending_owner_id():
    deliveries = [{"id": 1}] + [{"id": i} for i in range(2, 8)]

    trim_provisional_deliveries(
        deliveries, limit=3, protected_ids={1})

    assert deliveries == [
        {"id": 1}, {"id": 5}, {"id": 6}, {"id": 7},
    ]


def test_provisional_trim_bounds_unowned_markers_beside_pending_owners():
    deliveries = [
        {"id": index} for index in range(1, 501)
    ] + [
        {"id": index} for index in range(501, 1003)
    ]

    trim_provisional_deliveries(
        deliveries,
        limit=500,
        protected_ids=set(range(1, 501)),
    )

    assert len(deliveries) == 1000
    assert deliveries[:500] == [
        {"id": index} for index in range(1, 501)
    ]
    assert deliveries[500]["id"] == 503
    assert deliveries[-1]["id"] == 1002
