# Per-stream token accounting

`agent-orch list` and `agent-orch inspect` expose a `usage` snapshot. The daemon
collects native usage from each bound **local** provider transcript. These are
token counts, not prices, quota debits, or current-context estimates. Read each
stream once; its additional spec associations are not additional usage.

| Provider | Snapshot token field | Meaning |
|---|---|---|
| Codex | `input_total` | Cumulative input, including cached input |
| Codex | `cached_input` | Subset of `input_total` |
| Codex | `output` | Cumulative output, including reasoning |
| Codex | `reasoning` | Subset of `output` |
| Claude | `uncached_input` | Input excluding cache reads and writes |
| Claude | `cache_read` | Input read from cache |
| Claude | `cache_write` | Input written to cache |
| Claude | `output` | Response output |

Do not add Codex cached input to input, or reasoning to output. Codex
`last_token_usage` is used for context telemetry elsewhere and is never summed
by this ledger. Claude's nested cache-creation breakdown is already represented
by `cache_write`.

## Reading a snapshot

Every snapshot has `schema_version: 1`, `scope: "stream"`, `stream_id`,
`session_generation`, `host`, `provider`, and `tokens`. Before a valid observation,
all defined provider token fields are `null`; an unknown provider has `{}`.
A measured zero is `0`.

`collection_host` is null until the local daemon collects a span, then equals
`host`. `collected_since` and `updated_at` are server UTC timestamps;
`revision` increases when the recorded counters or coverage diagnostics change.
Replay of an unchanged span leaves the revision and timestamps unchanged.

Historical coverage is not certified in this release. `incomplete` is always
true, with `history_not_verified` in `incomplete_reasons`. Known counts are lower
bounds over accepted observations. Other sticky reasons are `invalid_usage`,
`missing_identity`, `malformed_record`, `counter_regression`,
`message_usage_regression`, and `ownership_conflict`. Good later observations
can increase counts, but never erase a coverage warning.

`spec_ids` is the session's normalized, ordered association list. `attribution`
is `{ "mode": "exclusive_primary", "spec_id": "<first-id>" }`, or
`{ "mode": "unattributed", "spec_id": null }` when that list is empty.
Authorization qualification is separate from accounting attribution. Retagging
changes live attribution; earlier report snapshots retain their earlier tags.
There is no split-allocation editor or fleet aggregation endpoint.

A successor's `handoff_from_stream_id` links to its predecessor. Each generation
keeps its own totals; the successor never silently includes predecessor usage.
Reusing the same native provider session under another owner is conservatively
marked `ownership_conflict` and skipped, including same-name reopen and handoff
resume. Start a distinct native provider session for separately counted work.

## Replay and persistence

The existing Store worker owns two additive tables in `sessions.db`:
`v2_usage_state`, keyed by stream and generation, and `v2_usage_records`, keyed
by host, provider, native session identity and record identity. Codex has one
cumulative record per native session. Claude assistant records use
`message.id`; sidechains are excluded. The ledger keeps these identities when
logs are truncated, replaced, compacted or removed. It introduces no TTL.

All four native counters must be nonnegative integers; booleans, floating-point
values, missing fields, strings and invalid Codex subset relationships reject
the whole observation. Counters remain unchanged and coverage is marked
incomplete. Duplicate or updated identities merge each field by its maximum;
only positive deltas enter the stream total. A decreased counter adds a warning,
never starts an inferred new epoch. Distinct native session identities contribute
separate components.

The bounded local ingestion path reads complete lines off the event loop.
Partial trailing lines await their terminating newline. Malformed complete lines
are consumed with a diagnostic; valid later lines still count. Native source
identity, session generation, provider, host and observed source binding fence
writes. A stale fence or failed transaction leaves the span pending for replay.
Unknown legacy provider rows keep their existing chat ingestion while their
accounting remains unknown. Codex bookkeeping remains absent from chat events.

Remote inventory rows can be displayed, but a daemon never reads a remote row's
provider path or mutates its accounting. Each host needs its own activated
collector; this feature adds no cross-host accounting transport or fleet totals.

## Reports and rollout

New completion reports store a server-authored `usage_snapshot` in the same
Store transaction as report insertion. The report acknowledgement and stored
report readbacks expose it. A replay returns that saved snapshot, even if usage
or spec associations have since changed. Existing reports have a null snapshot
and are never backfilled. Caller-supplied top-level `usage_snapshot` is rejected.

Migration is additive and runs on Store open. Older daemon code can ignore the
new tables and report column. Before coordinated activation, the release owner
records a verified database backup and the previous running artifact/PID on
each host. This lane does not restart or deploy daemons. Keep the database
preimage and accepted source identity with the release's rollback evidence.

Acceptance is automated in `services/chat-stream-v2/tests/test_usage_accounting.py`
and `services/agent-orch/tests/test_usage_cli.py`: native-file replay, restart,
truncation, invalid input, generation/host fences, transaction rollback, report
interleaving, lineage, attribution, migration and public readbacks. The v2 merge
gate and agent-orch tests are the final local source gates; passing them does not
establish runtime activation.
