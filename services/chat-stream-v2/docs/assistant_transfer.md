# Assistant host transfer: route custody and continuity

Scope: what happens to the composite's operator routes (`v2_assistant_composite_routes`) when a
committed host transfer moves the direct-primary assistant seat. Transfer authorization,
preparation and the successor `ready` handshake are unchanged and documented with the
transfer protocol itself.

## Custody

A direct route's `route_target`/`route_target_generation` name the seat that currently owes the
answer. They are mutable custody, rewritten by transfer commit. `route_id`, `dispatch_id`,
`input_identity`, `payload_digest`, `body`, `created_at` and the frozen envelope's
`original_input` never change. Each commit appends one entry to `route_json.carry[]`
(`command_id`, `from_stream_id`, `from_generation`, `prior_delivery_state`, `carried_at`), so a
chained transfer A→B→C repoints the same rows twice with two entries and no history walk.

An **unanswered route of a seat** is one with `routing_state='resolved'`,
`admission_mode='direct_primary'`, `delivery_state` in `intent`/`committed_pending`/
`uncertain`/`landed`, no `result`/`final` publication for its `input_identity`, and that seat's
exact current generation as target. A route already `failed` is terminal to the operator and is
not carried.

## At commit (`store_host_transfer._carry_routes`, inside `_move_custody`)

For every seat mapping of the command, each unanswered route of the source is updated in the
commit transaction: `route_target`/`route_target_generation` → the successor,
`delivery_state='intent'`, `error_code=NULL`, `carry[]` appended, and the frozen
`direct_envelope` re-addressed with `assistant_composite.carry_direct_envelope`. That transform
rewrites only the two target header lines, inserts the predecessor block immediately before the
existing `<assistant-original-input-json>` block and keeps the original input JSON, dispatch id,
reply ids and publication contract byte-for-byte; a missing or repeated anchor is refused. The
block names the most recent predecessor only:

```
[inherited dispatch] First delivered to <seat> (generation <generation>) which may have started this. Check children, lanes, receipts and existing publications before acting. Answer once under this same dispatch id.
```

The commit audit gains a `routes_carried` event listing the carried route ids and prior states.
Zero unanswered routes means no change. The retired seat's post-commit close therefore finds no
routes to fail-close; `fail_closed_assistant_routes` itself is unchanged, and an ordinary close of
the successor later fail-closes the routes it now holds, carried ones included.

## After commit (`HostTransfer.redeliver_carried`, from `project_and_cleanup`)

On every drive of a committed command, including after a daemon restart, the driver delivers each
route carried by that command whose state is `intent`, or `uncertain` with
`recovery_delivery_evidence_required` (the state startup recovery assigns to in-flight dispatches),
through the ordinary `AssistantComposite._dispatch_one` path. Guarantee, stated exactly: each
carried route is dispatched to the successor **at least once per commit**; a restart or re-drive
in the window may dispatch it again, and the predecessor block covers repeats. Nothing retries
beyond the existing receipt paths and nothing is silently dropped: a redelivery whose receipt is
`failed` or `uncertain` is visible in activity like any dispatch. The daemon never replays side
effects and makes no exactly-once claim; the successor checks children, lanes and receipts before
repeating a commission.

## Receipt fencing

`_dispatch_one` captures the route's custody when it starts and passes
`expected_route_target`/`expected_route_target_generation` to
`update_assistant_composite_route` on every receipt write. A receipt from a delivery that started
under the predecessor's custody and resumes after commit (landed, failed or exception) does not
match the successor's row and is logged as `assistant dispatch receipt stale custody` without
changing state, so the carried row stays `intent` for redelivery.

## Answering and the operator-visible state

The successor publishes under the original `dispatch_id`/`input_identity`; publication
authorization already follows current custody. A second final under the same publication key
returns `duplicate:true`. `assistant_activity.project_input` ranks a `result`/`final` publication
above a recorded `delivery_state='failed'`: an input that was fail-closed and is later answered
under its original dispatch id projects `answered` with no `error_code`, which is what clears the
failure in the clients. Uncertainty and cancellation keep their safety rank.

## Compatibility

Hot `rebind` is unchanged (routes pinned to a predecessor still fail on its ordinary close; the
dead-window queue replays unresolved input). After carry an inherited route is an open dispatch of
the successor for dispatch batching, so a new operator input batches into the oldest open carried
dispatch. Tests: `tests/test_assistant_transfer_route_continuity.py`.
