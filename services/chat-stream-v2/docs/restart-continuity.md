# Daemon restart continuity

A chat_streamd v2 restart (`launchctl kickstart -k`: SIGTERM to the daemon, SIGKILL
after 20 s) must not lose admitted work. Clients see a temporary disconnect; each
admitted spawn then finishes once (one pane, one session row, one initial prompt
delivery), lands in a durable failure that `spawn-status` explains, or, when the
daemon cannot prove whether its prompt was submitted, stays `indeterminate`: the
seat is kept and the prompt is never repeated. "Once"
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
| claim, before intent | After a crash the empty claim survives; once its pane is confirmed gone, reconcile records `failed` (`spawn_interrupted: … before spawn intent was persisted`) and releases it. A completed graceful drain releases the empty claim itself and records nothing, so `spawn-status` for the request id finds nothing. Either way a same-key retry makes one fresh claim: a failed outcome is not replayed. The staged file is content-addressed and unreferenced. |
| intent, before the pane | The reservation and intent are retained, by a crash or by a graceful drain, which only clears its owner. While retained, a same-key retry returns the original starting handle and creates nothing. Once the pane is confirmed gone, reconcile records `failed` (`spawn_interrupted: … before the session row was persisted`), which `spawn-status` returns for the request id, and releases it; a same-key retry then makes one fresh claim. |
| pane and row, before the paste | The pane is adopted by nonce. The intent has no pre-paste watermark, which proves no paste happened, so adoption waits for provider readiness and pastes the brief once. It then proves that paste like any post-paste adoption. A provider that is still booting leaves the outcome `indeterminate` and the reservation retained; a later reconcile pass delivers it. |
| paste, before the outcome | Adoption proves delivery from the post-watermark USER event or the provider transcript. It never pastes again. Without proof by the adoption deadline, a saved pre-paste watermark keeps the outcome `indeterminate` (`spawn_delivery_unknown`, `do_not_respawn`) rather than `failed`: the paste may have happened. |

A seat whose prompt went in as a native launch argument (`native_argv`) is
proof-only on adoption: its first USER event proves delivery, otherwise the
outcome stays `indeterminate`.

Post-paste adoption polls until the 20 s adoption deadline (`ADOPTION_BOOT_DEADLINE_S`). When the receipt supports Store proof:

Each pass observes in a fixed order and then decides once, from a single table. A pass decides from what it saw, with one carry-over: an expiry pass also counts a found transcript or a probe error from the window pass that crossed the deadline.

- **Early lookup:** for a receipt with Store proof only, inside the window, a Store lookup is offered while at least `ADOPTION_MIN_EVENT_LOOKUP_S` (0.25 s) remains. It can only prove delivery; any other answer is ignored.
- **Transcript:** the probe runs up to the deadline. At the deadline, one more observation always runs, with its own `ADOPTION_EXPIRY_TRANSCRIPT_OBSERVATION_S` (3 s) allowance measured from when it starts. Any exception from the probe, a `TimeoutError` included, is held until the decision. At that expiry observation only, no transcript needs a `ps` and `lsof` enumeration that ran to completion: exit 0, or lsof's no-match exit 1 with nothing on stderr. Anything else is held as such an exception, not an absent transcript: an observation the allowance cut off, a transport error, ssh exit 255, a missing binary, another exit status, a diagnostic or a partial listing. A window observation that its deadline cuts off counts as no transcript.
- **Pane liveness:** read on every pass, as alive, gone or unknown. Any error reading it (an SSH or tmux failure) counts as unknown. With Store proof, unknown never produces a failure.
- **Store sample:** one sample with its own 0.25 s grant follows the observations. A window pass that reaches the deadline before or during that sample decides nothing, unless the sample came back proven: then the pass delivers at once. Otherwise the expiry pass observes again, carrying over only a found transcript or a held error.
- **Decision:** a proven sample means delivered, ahead of a found transcript. A found transcript otherwise delivers, even when the Store is unreachable or timed out. A held probe error is re-raised unless delivery is proven, so the reconcile pass defers. A failure (`transcript_absent`, `pane_died_during_boot`, `agent_never_started`) needs a `pending` sample and a pane read as alive or gone. Any other timeout or unreachable Store keeps the outcome `indeterminate` and the reservation retained.
- **Added wait:** one full 0.25 s gate per window pass admitted before expiry, plus at most 3.25 s of transcript/Store grants on an unresolved expiry pass. This is not an end-to-end deadline. The early lookup, the liveness read (10 s transport timeout), scheduling delay and cancellation cleanup fall outside it. Startup settles intents one at a time, so these costs accrue for each unresolved intent with Store proof.
- **Liveness on every pass:** a found transcript, a probe error and (without a saved watermark) an absent transcript used to end the pass before liveness was read. They now read it first, for receipts with and without Store proof. That adds a cancellation point and up to a 10 s transport wait per affected intent.

Receipts without Store proof use the transcript alone, as before, and never enter the Store lookups. They read pane liveness the same three-state way on every pass, and an unknown pane keeps polling and defers at the deadline, with one exception: without a saved watermark, an absent transcript fails as `transcript_absent` whatever the pane reads, unknown included.

The whole graceful stop shares one absolute deadline (`shutdown_budget.py`):

- **Budget:** 15 s by default. `PENTACLE_V2_SHUTDOWN_BUDGET_S` may lower it but never raise it.
- **Every step:** gets min(its own cap, what remains). The steps are the spawn drain, the work index, background tasks, composites, lane rulings, `Server.close` (accepted sends, consent expiry, and the TLS and plain listeners), notify, assets, lifecycle and the loop-progress watchdog.
- **Overruns:** a step that overruns is cancelled and abandoned, never awaited again, so a step that ignores cancellation cannot hold the stop open.
- **Store reserve:** 1 s is held back for `store.stop()`.
- **Loop teardown:** after the stop, teardown waits at most 0.5 s for leftover tasks (`run_bounded`).
- **Total:** the asynchronous shutdown and loop teardown stay under launchd's 20 s exit window, so the store stops before SIGKILL.
- **Limit:** CPython's exit-time join of the notify and assets executor threads runs after this and is not budgeted. A worker stuck there is ended by launchd's SIGKILL at 20 s. Writes from an abandoned step may be lost by design.

A graceful stop first closes spawn admission (new spawns wait and reconnect after the
restart). It then cancels in-flight spawn tasks while the store is still running,
within at most 5 s (`SHUTDOWN_SPAWN_DRAIN_S`). When the drain completes
(`unfinished=0` in its log line), each spawn has recorded its interruption handoff
(retained intent, an `indeterminate` outcome for an admitted row, intent owner
released) before the store stops. A cancelled spawn does not trust its own
in-memory record of what it wrote: the cancel can land after the intent commits
and before the write returns. Every exit without a confirmed outcome settles
through one Store operation instead, queued behind any intent write the spawn
submitted and matched on host, name, request id, creation nonce and current
owner. A committed intent keeps its payload and only its owner is cleared; an
empty claim with no pane is deleted; any other row (missing, foreign, tombstoned
or pane-bound) is left alone. If that settle itself fails or is cancelled,
nothing falls back to a delete, a pane kill or an invented outcome; the next
daemon instance reconciles the row it finds. A nonzero `unfinished` count or an abandoned
drain step leaves that handoff unproven; check `spawn-status` before relying on
restart recovery. One step is never cut: a spawn that has persisted its pre-paste
watermark gets up to half the drain window to land its paste before it is
cancelled, because from that point adoption may only prove a paste, never make
one. A SIGKILL inside that sub-second step leaves the outcome `indeterminate`
(`spawn_delivery_unknown`), never a second paste.

`indeterminate` is a recovery handle, not a failure. While its reservation exists,
reconcile keeps working on it until `delivered` or `failed`; a
`spawn_delivery_unknown` or unproven native-argv outcome can stay `indeterminate`,
and its seat must not be respawned automatically. A same-key spawn replays it as
the in-flight `starting` handle, never as a failure and never as a second pane.

### Send cancellation during attachment materialization

After a send's acceptance receipt commits, cancellation while fetching or staging
attachments is known to precede provider input. `Comms.send` attempts to append
`not_landed` state and delivery, `submission_confirmed=false`, reason
`cancelled_before_submission`, and no attempt count under the original request
and receipt IDs. It then propagates cancellation; it does not return a successful
send response. If that outcome commits, the highest-rowid receipt survives a
Store reopen and an explicit retry of the unchanged payload with the same
request ID can submit once. A further identical retry coalesces after landing.

The outcome write uses the existing shutdown time and Store ownership rules.
There is no shield, detached cleanup task, write retry or extra shutdown budget.
An ordinary persistence error logs only its type and request ID with
`subsystem=send bug_ref=restart_materialization_cancel`; cancellation still
propagates. A second cancellation can interrupt cleanup. An already-admitted
Store operation may still commit, so only the actual durable receipt establishes
retryability. A failed or interrupted write is not permission to resend.

This narrow boundary excludes initial claim persistence, held ingress, the later
materialized wire-update append, provider paste and proof waits. Post-input
cancellation remains uncertain and same-key retries must not paste again. Abrupt
process death is unchanged, and startup does not rewrite historical receipts.
`tests/test_send_restart_materialization.py` covers the real `Server.close`
five-second drain and file Store reopen with synthetic remote staging and a fake
provider. It is not subprocess-restart or native-provider exactly-once proof;
the test's 8.5-second elapsed bound is fixture tolerance, not a production SLA.

After every start, the daemon also sends the front desk one notice that lists the
work in progress; see [restart notice](restart-notice.md).

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
  socket resolves through `await_spawn` by request id. After a restart, `await_spawn`
  (like `spawn`) waits until the startup reconcile has settled interrupted spawns,
  so it never returns a pre-adoption `indeterminate`. The result is `ready`, the
  recorded failure, or a typed `spawn.indeterminate` naming the request and stream
  id. The CLI prints `spawn key: <key>` before the RPC.
- The whole post-admission wait runs under ONE absolute deadline set at admission:
  connect, hello and snapshot, inventory reads, the outcome readback, reconnect and
  backoff, and cleanup. Every socket it opens is owned by the wait. At the deadline
  those transports are aborted and the wait is cancelled, so no close handshake,
  retry or send runs past it, including during the CLI's own loop shutdown.
- Terminal proof (ready, or a recorded failure for this request and stream) is handed
  to the owner as soon as it is established, before any socket cleanup. This covers
  the initial snapshot, a later inventory frame, and the recovery readback, whose
  response is handed over before its own socket closes. A proven result is returned
  even if cleanup then stalls past the deadline or fails. Proof that first arrives
  after the deadline, or names another stream, never counts.
- Without terminal proof by the deadline, the result is a typed
  `spawn.indeterminate`, never a success or a failure. The admitted seat is never
  cancelled or re-spawned.
- This is a bound on the client's own awaits. It is not a real-time guarantee
  under host starvation, and interpreter exit after the loop is not counted.
- Single-shot verbs (`close`, `reparent`, `notification.await`, `send.receipt.get`)
  still fail fast with a typed transport error.

Recovery reads after any doubt: `agent-orch spawn-status <key|request_id>`,
`agent-orch await-spawn --request-id <id>`, `agent-orch await --from <stream>`
(ledger-resolved), `agent-orch inspect`.

## Verification

The restart matrix runs real daemon processes: SIGTERM and SIGKILL at each stage,
plus client journeys for `await`, keyed `spawn` and `report`. It is explicitly run and is not part of the unit gate:

```bash
cd services/chat-stream-v2
PENTACLE_FORCE_LIVE_DAEMON=1 python3 -m pytest tests/soak/test_restart_continuity.py -q
```

`RESTART_MATRIX_EVIDENCE=<dir>` keeps each cell's records, timelines, sqlite
snapshots, pane captures and daemon log. In-process pins for each change are in
`tests/test_restart_continuity_daemon.py` and
`services/agent-orch/tests/test_restart_continuity_client.py`.
