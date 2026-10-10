# Daemon progress and connection diagnostics

The daemon publishes constant-cost MAIN and Store progress for an independently
installed checker. This source does not provision or claim an independent
notification transport. The checker, its startup service, approval, private
caller, recipient and installed readbacks belong to the deployment owner.

## Configuration and ownership

Set both `PENTACLE_DAEMON_PROGRESS_PATH` (an absolute file in the configured
runtime directory) and `PENTACLE_DAEMON_INSTANCE_ID` (an opaque stable installation
identifier, 1–128 ASCII letters, digits, `.`, `_`, `:`, or `-`). Provision the
parent directory as owner-only mode 0700. All path components must be real
directories, with no symlink traversal; files must be owned regular single-link
files in mode 0600. No directory is created implicitly. Malformed configuration
fails startup; missing/insecure files leave monitoring unavailable while existing
daemon work continues. With neither setting, only local diagnostics run.

`loop_watchdog.py` owns a single publisher thread and a one-second MAIN pulse.
The Store's enqueue/start/finish hooks update counters and a queue-capped deque
in O(1), without SQL, file I/O, logging, payload inspection or history scans. The
publisher takes coherent snapshots and atomically replaces the progress file
outside MAIN and Store. It owns and joins its thread at shutdown. The last file
is retained as evidence; its stale timestamp is not a healthy shutdown signal.
Stopping the daemon never stops the external checker.

The maximum JSON file size is 4096 bytes. `daemon-progress.v1` contains:

- `instance_id`, per-boot UUID `boot_id`, and diagnostic `pid`
- Host-monotonic `sample_mono_s`, `loop_seq`, `loop_mono_s`
- `store.enqueued_seq`, `started_seq`, `finished_seq`, `pending_count`,
  `oldest_pending_mono_s`, and `current_started_mono_s`

Empty queued/current work has null ages. Sequence/timestamp tuples contain no
payload, identity-bearing path, command, credential or contact information.
`read_json`, `validate_snapshot`, and `cause_at` expose the strict portable wire
contract. Invalid/future/negative/impossible progress is unavailable, not healthy.
Monotonic timestamps are host-local and must not become persisted wall-clock ages.

## Independent checker contract

The checker samples once per second, grants five seconds after first valid boot
binding, and detects loop age, running Store callback age or oldest pending work
age reaching five seconds. A stale sample identifies publisher/process
unavailability. It verifies the expected installation identity and never treats
PID existence alone as progress. A missing snapshot after live binding raises
monitor-unavailable; failure to bind initially is not protected service. Recovery
requires two consecutive fresh healthy samples, including after daemon restart.
Whole-host and network outages are outside this proof.

Only the external checker allocates a persisted monotonic episode ordinal. The
shared episode ID is SHA-256 of UTF-8 `instance_id + NUL + boot_id + NUL + ordinal`.
Cause changes retain that identity. The checker must retain one active incident,
one pending recovery, and last acknowledged terminal identity. It must not
replace unacknowledged state.

Write the sibling `<progress filename>.episodes.json` with exactly:

- `version: "daemon-episodes.v1"`, `instance_id`, `active`, `recovery`
- Each non-null event has `boot_id`, positive signed-64-bit `ordinal`,
  `episode_id`, `condition`, `cause`
- Active condition is `active`, recovery condition is `recovered`; cause is
  `loop`, `store`, `loop_store`, or `unavailable`
- Retain the matching opening `active` event alongside its `recovery` until ACK

The daemon's serialized consumer records active then recovered using
`Alerts.record("daemon_loop_stalled", ...)`, producing
`daemon_runtime.v1 / loop_stalled`. The safe cause is the typed fact's `stage`.
Existing ErrorAlerts policy is unchanged: off/record-only/on and recovery before
first in-app attempt retain their normal suppression behavior. Independent
transport submission is separate from that policy and must already be authorized.
No private sender configuration or mode grant is accepted from the handoff.

After durable sink acceptance, the publisher atomically writes sibling
`<progress filename>.ack.json` with `version: "daemon-episode-ack.v1"`,
`instance_id`, `active` and `terminal` (episode IDs or null). A missing sink or
failed commit is not acknowledged. Durable facts use stable IDs, so a crash
between commit and ACK can replay without creating a second notification.
Malformed handoffs, conflicting identity and premature episode replacement are
refused. The most recent accepted handoff remains bounded and retryable.

The checker must persist `inflight` before invoking its caller. Provider timeout,
crash, malformed result or uncertain acceptance is `submission_unknown`, without
automatic resubmission; restarted inflight is also unknown. Only definitely
unsubmitted local preflight failures may use the owner's bounded retry policy.
An accepted SID/status proves provider submission, not handset delivery. The
portable synthetic HTTP sink's idempotency is not private-provider idempotency.

## Request-owned diagnostics and validation

`request_store_diag` logs only connection ID, completed Store call count, total
queue wait and callback execution milliseconds for that request's context.
Concurrent requests have separate aggregates; completed contexts reject updates
from tasks that outlive their originating request. These diagnostics do not
change RPC authorization, admission, time budgets or response payloads.

Portable focused checks are `tests/test_loop_stall_guard.py`, the existing
logging/adapter/core suites and held-component process tests. Required installed
acceptance additionally holds actual MAIN and Store faults until an independent
submission receipt is observed, proves healthy MAIN during the Store fault,
checks process/publisher freeze, restart/uncertainty, owner binding, healthy idle
and bounded resources, and reads back the deployed observer and delivery setup.
Source tests and isolated sink receipts do not establish live SMS protection.

Rollback disables only this integration and reverts its instrumentation while
preserving episode evidence and the existing alert policy. No auto-kill,
auto-restart, stack collection or new messaging authority is introduced.
