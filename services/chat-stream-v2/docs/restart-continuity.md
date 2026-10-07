# Daemon restart continuity

A chat_streamd v2 restart (`launchctl kickstart -k`: SIGTERM to the daemon, SIGKILL
after 20 s) must not lose admitted work. Clients see a temporary disconnect; each
admitted spawn then either finishes once (one pane, one session row, one initial
prompt delivery) or lands in a durable failure that `spawn-status` explains. "Once"
means relative to the daemon's acknowledgement records: idempotency keys, request
ids, report ids and delivery receipts.

## Durable state by stage

A spawn writes, in order: the reservation claim (`v2_stream_reservations`, with
`request_id`, `nonce`, `idempotency_key`), the staged prompt file, the spawn intent
(`payload` on the reservation), the tmux pane (born with `PENTACLE_SPAWN_NONCE`),
the `sessions` row, the pre-paste proof watermark (rewritten into the intent), the
paste, and finally the `v2_spawn_outcomes` row.

| Interrupted after | Restart outcome |
|---|---|
| claim, before intent | Startup reconcile records `failed` and releases the claim. A same-key retry replays that failure; a fresh key spawns normally. The staged file is content-addressed and unreferenced. |
| intent, before the pane | `failed spawn_interrupted`, released; same replay rules. |
| pane and row, before the paste | The pane is adopted by nonce. The intent has no pre-paste watermark, which proves no paste happened, so adoption waits for provider readiness and pastes the brief once. It then proves that paste like any post-paste adoption. A provider that is still booting leaves the outcome `indeterminate` and the reservation retained; a later reconcile pass delivers it. |
| paste, before the outcome | Adoption proves delivery from the post-watermark USER event or the provider transcript. It never pastes again. |

The whole graceful stop shares one absolute deadline (`shutdown_budget.py`):

- **Budget:** 15 s by default. `PENTACLE_V2_SHUTDOWN_BUDGET_S` may lower it but never raise it.
- **Every step:** gets min(its own cap, what remains). The steps are the spawn drain, background tasks, composites, lane rulings, `Server.close` (accepted sends, consent expiry, and the TLS and plain listeners), notify, assets and lifecycle.
- **Overruns:** a step that overruns is cancelled and abandoned, never awaited again, so a step that ignores cancellation cannot hold the stop open.
- **Store reserve:** 1 s is held back for `store.stop()`.
- **Loop teardown:** after the stop, teardown waits at most 0.5 s for leftover tasks (`run_bounded`).
- **Total:** stays under launchd's 20 s exit window, so the store stops before SIGKILL.

A graceful stop first closes spawn admission (new spawns wait and reconnect after the
restart). It then cancels in-flight spawn tasks while the store is still running,
within at most 5 s (`SHUTDOWN_SPAWN_DRAIN_S`). Each spawn records its interruption
handoff (retained intent, an `indeterminate` outcome for an admitted row, intent
owner released) before the store stops. One step is never cut: a spawn that has
persisted its pre-paste watermark gets up to half the drain window to land its
paste before it is cancelled, because from that point adoption may only prove a
paste, never make one. A SIGKILL inside that sub-second step remains the one
window that ends `failed` with a live pane.

`indeterminate` is a recovery handle, not a terminal state. While its reservation
exists, reconcile keeps working on it until `delivered` or `failed`. A same-key spawn
replays it as the in-flight `starting` handle, never as a failure and never as a
second pane.

## What clients do

- Retry-eligible verbs (`await`, `await-spawn`, `report`, `tell`, keyed `spawn`, the
  read verbs; see `RPC_RETRY_ELIGIBLE_TYPES`) treat a refused or dropped socket as a
  restart. They reconnect with capped backoff (0.25 s doubling to 2 s) until the
  verb's own retry deadline (call timeout × attempts), then re-send the same
  request. `await` resolves from the durable ledger. An explicit
  `AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS` restores a fixed attempt bound, and
  `AGENT_ORCH_RPC_RETRY_DEADLINE_S` sets the total window. An unanswered RPC
  timeout keeps its attempt bound.
- A spawn that was already admitted (`spawn.ok state=starting`) and then loses its
  socket resolves through `await_spawn` by request id. The result is `ready`, the
  recorded failure, or a typed `spawn.indeterminate` naming the request and stream
  id. The CLI prints `spawn key: <key>` before the RPC.
- Single-shot verbs (`close`, `reparent`, `notification.await`, `send.receipt.get`)
  still fail fast with a typed transport error.

Recovery reads after any doubt: `agent-orch spawn-status <key|request_id>`,
`agent-orch await-spawn --request-id <id>`, `agent-orch await --from <stream>`
(ledger-resolved), `agent-orch inspect`.

## Verification

The restart matrix runs real daemon processes: SIGTERM and SIGKILL at each stage,
plus client journeys for `await`, keyed `spawn`, `report` and the daily retro `run`
path. It is explicitly run and is not part of the unit gate:

```bash
cd services/chat-stream-v2
PENTACLE_FORCE_LIVE_DAEMON=1 python3 -m pytest tests/soak/test_restart_continuity.py -q
```

`RESTART_MATRIX_EVIDENCE=<dir>` keeps each cell's records, timelines, sqlite
snapshots, pane captures and daemon log. In-process pins for each change are in
`tests/test_restart_continuity_daemon.py` and
`services/agent-orch/tests/test_restart_continuity_client.py`.
