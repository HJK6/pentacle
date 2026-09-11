# Chat-stream v2 daemon observability

The v2 daemon exposes one read-only process snapshot through `daemon.stats`. It does not implement the former periodic stats JSONL sink, stats rotation controls, RPC latency histograms, queue gauges, database-lock timing, client counts, or host-probe timing.

## Read a snapshot

From the repository root:

```bash
python3 services/chat-stream-v2/tools/read_daemon_stats.py \
  --ws-url ws://127.0.0.1:7791
```

The tool opens a websocket, sends a correlated `daemon.stats` request, waits for `daemon.stats.ok`, and prints the frame as JSON. `--timeout` controls both connection and receive timeout.

The response's `stats` object has only these keys:

| Key | Source | Meaning |
|---|---|---|
| `unsupported_in_v2` | `services/chat-stream-v2/server.py` | bounded counts, callers, and recent records for unknown or capability-rejected verbs |
| `seat_tokens` | `services/chat-stream-v2/seat_token_telemetry.py` | process-local issuance and verification counts, failure reasons, and bounded token-free recent records |
| `boot_queue_depth` | `services/chat-stream-v2/spawnctl.py` | waiting Codex boots by host |
| `lifecycle` | `services/chat-stream-v2/daemon_lifecycle.py` | current process instance id and its in-memory start/stop events |

These records are process-local. A daemon restart resets unsupported-verb, seat-token, and lifecycle event history; `daemon.stats` is not a durable incident archive.

## Reading the fields

- A rising `unsupported_in_v2.total` means a caller is sending a verb that is not in the active dispatch table or a registered handler is returning the compatibility error. Use `verbs.<name>.callers` and `last_seen` to locate it. The daemon also rate-limits a warning log per unsupported verb.
- `seat_tokens.counts.verification_failure` and `failure_reasons` distinguish absent, malformed, expired, wrong-seat, and internal failures without exposing credential material. `recent` contains bounded labels, stream ids, reason codes, and timestamps only.
- `boot_queue_depth.<host>` counts coroutines waiting on that host's Codex boot semaphore. An absent host or zero means no queued waiter was captured at snapshot time.
- `lifecycle.instance_id` identifies this process. Its events are held only in memory and returned newest first.

Use `agent-orch reconcile status --json` for durable-session-versus-tmux reconciliation, and `agent-orch inspect <stream-id>` for one seat. Those contracts belong to [`services/agent-orch/README.md`](../services/agent-orch/README.md), not this process snapshot.

## Operator-confirmed offline closes

`agent-orch inspect <stream-id>` exposes `session.close_kind`, `close_audit`, and
`deferred_reap`. The latter is durable across restarts and contains `stream_id`,
`host`, `session_name`, `generation`, `requested_at`, `attempts`, `last_error`,
`done_at`, `exhausted_at`, and the saved `pane_identity` lease. A null `done_at`
means pane death has not been verified. `close_kind: operator_offline_close`
remains unchanged after cleanup, preserving how the row was closed.

The daemon logs `operator_offline_close` with caller, generation, and timestamp,
and creates an Updates notification for the close. Each reachable cleanup
attempt logs `deferred_reap`; five failed attempts set `exhausted_at` and emit
`deferred_reap_exhausted`. Exhaustion stops automatic retries and retains the
record and adoption fence for inspection. Offline passes consume no attempts.
Identity mismatch or unavailable identity never authorizes a kill. An exhausted
intent requires investigation of the named peer and pane; there is no automatic
reset or bulk cleanup verb.

The close row, audit, and `v2_deferred_reap` entry commit in one session-database
transaction. A pending close is separate from the verified-death `session_reap`
ledger. See the [close protocol](chat_protocol.md#close-on-an-offline-host) for
request and reply semantics.

## Nonexistent controls

Do not configure `--daemon-stats-*` options or `PENTACLE_DAEMON_STATS_*` variables: `services/chat-stream-v2/main.py --help` exposes neither. There is no v2 `daemon_stats.jsonl` output path to tail or rotate.

Release verification uses the deploy tool's launchd and `welcome.runtime_sha` readbacks described in README § Daemon releases. `daemon.stats` does not contain deploy stamps or a running SHA.
