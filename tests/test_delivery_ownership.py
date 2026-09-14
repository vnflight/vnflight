"""Ownership transitions independent of network and renderer timing."""

from vnflight.delivery_ownership import ActionDeliveryOwnership
import pytest


class Ledger(ActionDeliveryOwnership):
    def __init__(self):
        self._delivered_action_events = set()
        self._delivered_action_event_ownership = set()
        self._action_delivery_reset_generation = None
        self._prefetched_events = []


def test_generation_moves_forward_without_replaying_current_rows():
    ledger = Ledger()
    ledger._record_delivered_action_events([(1, 10)])
    ledger._observe_action_delivery_generation(2)
    assert ledger._action_event_was_delivered((1, 10), 2)
    ledger._observe_action_delivery_generation(3)
    assert not ledger._action_event_was_delivered((1, 10), 3)
    ledger._record_delivered_action_events([(1, 10)])
    ledger._observe_action_delivery_generation(2)
    assert ledger._action_delivery_generation() == 3
    assert ledger._action_event_was_delivered((1, 10), 3)
    assert not ledger._action_event_was_delivered((1, 10), 2)


def test_transcript_and_receipt_share_occurrences_not_text_identity():
    ledger = Ledger()
    first = {"action_id": 1, "_seq": 10, "text": "Again"}
    repeat = {"action_id": 1, "_seq": 11, "text": "Again"}
    unowned = {"text": "Unattributed"}
    ledger._record_delivered_action_events([(1, 10)])
    assert ledger.claim_undelivered_action_events([first, repeat, unowned]) == [repeat, unowned]
    assert ledger.claim_undelivered_action_events([repeat]) == []
    ledger.reset_action_event_delivery()
    assert ledger.claim_undelivered_action_events([first]) == [first]


def test_bounded_ownership_retains_newest_generations_and_coordinates():
    ledger = Ledger()
    ledger._MAX_DELIVERED_ACTION_EVENTS = 2
    ledger._record_delivered_action_events([(1, 10)], reset_generation=1)
    ledger._record_delivered_action_events([(1, 10), (1, 11)], reset_generation=2)
    assert not ledger._action_event_was_delivered((1, 10), 1)
    assert ledger._action_event_was_delivered((1, 10), 2)
    assert ledger._action_event_was_delivered((1, 11), 2)


def test_claim_preserves_policy_override_dispatch():
    class RefusingLedger(Ledger):
        def _action_event_was_delivered(self, key, reset_generation=None):
            return True

    assert RefusingLedger().claim_undelivered_action_events([
        {"action_id": 1, "_seq": 10},
    ]) == []


def test_held_booked_rows_survive_transfer_then_receipt_replay_is_suppressed():
    ledger = Ledger()
    ledger._observe_action_delivery_generation(1)
    opening = {"type": "narration", "_seq": 10, "text": "Opening"}
    reply = {"type": "dialogue", "_seq": 11, "action_id": 1, "text": "Reply"}
    foreign = {"type": "dialogue", "_seq": 12, "action_id": 2, "text": "Later"}
    ledger._prefetched_events = [opening, reply, foreign]
    # Command observation has booked the row, but has not shown it to the user.
    ledger._record_delivered_action_events([(1, 11)])
    assert ledger._claim_prefetched_action_events(1, include_unowned=True) == [opening, reply]
    assert ledger._prefetched_events == [foreign]
    assert ledger.claim_undelivered_action_events([reply]) == []
    assert ledger._claim_prefetched_action_events(1, include_unowned=True) == []
    assert ledger._claim_prefetched_action_events(2) == [foreign]


def test_transcript_prefix_fence_never_partially_consumes_held_rows():
    ledger = Ledger()
    first = {"type": "narration", "_seq": 10}
    decision = {"type": "choice_request", "_seq": 11}
    ledger._prefetched_events = [first, decision]
    assert ledger.claim_prefetched_visible_prefix(20) == ([], True)
    assert ledger._prefetched_events == [first, decision]
    ledger._prefetched_events = [first]
    assert ledger.claim_prefetched_visible_prefix(20) == ([first], False)
    assert ledger._prefetched_events == []


def test_detach_restore_preserves_prefix_before_concurrent_observation():
    ledger = Ledger()
    first = {"type": "dialogue", "action_id": 1, "_seq": 10}
    later = {"type": "dialogue", "action_id": 2, "_seq": 20}
    ledger._hold_events([first])
    ledger._record_delivered_action_events([(1, 10)])
    detached = ledger._take_held_events()
    assert ledger._prefetched_events == []
    ledger._hold_events([later])
    ledger._restore_held_events(detached)
    assert ledger._take_held_events() == [first, later]
    assert ledger._action_event_was_delivered((1, 10))
    assert not ledger._action_event_was_delivered((2, 20))


def test_chronological_hold_is_stable_and_does_not_deduplicate_text():
    ledger = Ledger()
    later = {"_seq": 20, "text": "Again"}
    first = {"_seq": 10, "text": "Again"}
    same_position = {"_seq": 20, "text": "Another channel"}
    ledger._hold_events([later])
    ledger._hold_events(iter([first, same_position]), chronological=True)
    assert ledger._take_held_events() == [first, later, same_position]


def test_lifecycle_clear_does_not_rewrite_occurrence_ownership():
    ledger = Ledger()
    ledger._record_delivered_action_events([(1, 10)])
    ledger._hold_events([{"action_id": 1, "_seq": 10}])
    ledger._clear_held_events()
    ledger._hold_events([{"action_id": 2, "_seq": 20}])
    assert ledger._take_held_events() == [{"action_id": 2, "_seq": 20}]
    assert ledger._action_event_was_delivered((1, 10))


@pytest.mark.parametrize("module_name", ["client", "cli", "settle", "overlay_presentation"])
def test_no_scattered_held_queue_mutations(module_name):
    import ast
    import importlib
    import inspect

    tree = ast.parse(inspect.getsource(importlib.import_module("vnflight." + module_name)))
    writes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "_prefetched_events"
        and isinstance(node.ctx, ast.Store)
    ]
    # Only the typed constructor initialization remains in the transport layer.
    assert len(writes) == (1 if module_name == "client" else 0)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            assert not (
                isinstance(target, ast.Attribute)
                and target.attr == "_prefetched_events"
                and node.func.attr in {"append", "extend", "clear", "pop", "remove", "sort"}
            )


def test_settle_restore_uses_same_transition_for_duck_typed_client():
    from types import SimpleNamespace
    from vnflight.settle import restore_prefetched_events
    client = SimpleNamespace(_prefetched_events=[{"_seq": 20}])
    restore_prefetched_events(client, [{"_seq": 10}])
    assert client._prefetched_events == [{"_seq": 10}, {"_seq": 20}]


def test_explicit_wait_keeps_predecessor_fence_but_auto_wait_drains_it():
    ledger = Ledger()
    older = {"action_id": 1, "_seq": 10, "type": "progress_change"}
    current = {"action_id": 2, "_seq": 20, "type": "dialogue"}
    newer = {"action_id": 3, "_seq": 30, "type": "dialogue"}
    tail = {"_seq": 31, "type": "narration"}
    ledger._hold_events([older, current, newer, tail])
    assert ledger._claim_prefetched_action_events(2) == []
    assert ledger._claim_prefetched_action_events(2, include_unowned=True) == [older, current]
    assert ledger._prefetched_events == [newer, tail]
