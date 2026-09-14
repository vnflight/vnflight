# Delivery ownership

The client has three readers of overlapping bridge history: ordinary `/state`
polling, scoped transaction receipts, and transcript recovery. They must agree
on occurrence ownership without treating identical text as the same occurrence.

`delivery_ownership.ActionDeliveryOwnership` is the shared policy component.
`BridgeClient` inherits it deliberately: existing method overrides and callers
keep using the same hooks. It owns no HTTP requests, rendering, or protocol
acknowledgements. The single-file builder places it before the client.

## Current transitions

- Observed: transport has read a row. Advancing a transport cursor does not prove
  the row was displayed to a user.
- Held: a command observation can park that row in `_prefetched_events`. Its
  attributed occurrence may already be booked in the cross-lane dedup ledger.
  A held copy must still be returned; the ledger must not suppress it.
- Claimed: a scoped wait takes its owned prefix, stopping at a foreign action.
  Transcript recovery may claim an earlier visible prefix, but a decision or
  unknown row fences the entire claim without partially consuming it.
- Recorded: attributed occurrences are keyed by generation, action id and
  bridge sequence. Unattributed rows are not globally deduplicated by text.
- Acknowledged: transaction transport acknowledges only after preparing its
  result. This remains in the client, outside the ownership component.

## Invariants

1. Only authoritative state advances the delivery generation, never an older
   replayable transaction receipt. Generation cannot move backwards.
2. Legitimate equal-text occurrences remain distinct.
3. Booking an occurrence cannot consume its held copy.
4. A rejected prefix claim leaves every held row intact.
5. A caller's explicit action wait cannot consume another action's held output.
   Internal act-handback waits deliberately enable the broader prefix claim
   used by automatic waits: they may return held predecessor-action rows and
   unattributed rows before the current action, preserving chronology. Newer
   actions still fence that prefix. Delivery does not change row attribution.
6. History retention stays bounded by the existing generation-qualified limit.
7. Moving the component does not bypass subclass policy overrides.

## Refactor boundary

All held-queue writes now go through five operations on the ownership component:
`_hold_events` appends observations (optionally ordering by bridge sequence),
`_take_held_events` detaches a batch, `_restore_held_events` prepends that batch
after a failed/interrupted attempt, `_retain_held_events` keeps a selective
claim's remainder, and `_clear_held_events` abandons it at a lifecycle boundary.
CLI, settle, and overlay helpers use the same implementation, including with
duck-typed clients. None of these operations changes acknowledgement or the
dedup ledger. Existing code still decides when a lifecycle boundary is valid.

The initial extraction preserves method bodies and state fields. It is not a
new delivery protocol or a claim that all presentation state is centralized.
Ordinary polling, transaction acquisition/acknowledgement, nonce retirement,
and handler overlay ownership still have their existing responsibilities.

Any subsequent consolidation must first pin those full sequences through real
client/handler calls, including failed prefix reads, stale receipts after load,
interruption, and repeated waits. Do not collapse recorded and user-visible
states into one boolean. Do not change NVL capture or engine compatibility as
part of this work.

Verification includes component transition tests, existing client/handler
sequence regressions, and execution of the built single-file artifact. Live
CLI and persistent-MCP checks must use the rebuilt code before release.
