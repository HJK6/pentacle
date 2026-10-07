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

## Provenance

Provenance joins each ledger record to an account, a model and an observation
time without touching admission: nothing below reads or writes
`v2_usage_records`/`v2_usage_state` totals, and stream open/closed state and
session generation are ignored, so closed and archived native sessions accept
it. Store open adds three tables and stamps `PRAGMA user_version = 2`; an older
daemon ignores them, so rollback is reinstalling the previous release.

| Table | Key | Holds |
|---|---|---|
| `v2_usage_provenance` | ledger key (host, provider, native session, record key) | Claude `observed_at`, `model` per `message.id` |
| `v2_usage_identity` | host, provider, native session | `account_id`, `account_source` (`transcript`/`unknown`), `conflict`, `first_observed_at`, `cli_version` |
| `v2_usage_codex_responses` | host, native session, `response_id` | Codex per-response `observed_at`, `model`, `input`, `cached_input`, `cache_write_input`, `output`, `reasoning_output` |

The Codex response table is detail beside the one cumulative ledger row; it is
not a ledger table and never changes totals.

**Identity.** Claude: every `credential_org` attachment's `organizationUuid`
(Claude Code ≥ 2.1.283). Codex: `session_meta.payload.creator_account_id`
(CLI ≥ 0.160.0). One id is `transcript`; a whole transcript with none is
`unknown`; two different ids set `conflict=1` with a null account. Conflict is
sticky: once set, no later live, replay or backfill item repopulates the
account. A null account is filled only while `conflict=0`. Identity is never
derived from the host or the current login, and a live span without an identity
record carries `identity: null` rather than `unknown`.

**Records.** Claude: one `claude_record` per non-sidechain assistant
`message.id`, keeping the copy with the largest `output_tokens`; upsert fills
nulls only. Codex: one `codex_response` per `token_usage_record.response_id`,
model from the enclosing `turn_context`; insert-or-ignore, and a replay whose
values differ is counted `response_conflict` and ignored.

**Wire.** `event.push` carries an optional `usage_provenance` block
`{version: 1, items: [...], dry_run?}` (≤ 2000 items) and requires the source
host proof. Every item has exactly `kind`, `provider`, `native_session_id`,
`source_file_identity_digest`, `identity`, `data`; `data` keys are fixed per
kind (`claude_record`, `codex_response`, `rate_limit`). A `claude_record` or
`codex_response` needs a ledger row for that native session on the
authenticated host (`unknown_native_session`; a missing Claude record key is
`unknown_record`). A Claude `rate_limit` is `unsupported_kind_for_provider`.
The ack's `usage_provenance` block reports `counts` per outcome, the known and
unknown native sessions, and per-item rejections; a block-level `error` leaves
the satellite's batch queued. `dry_run` validates and classifies in a rolled
back transaction and writes no history. Timestamps are stored as UTC ISO-8601
with `Z` (millisecond precision, omitted when zero).

**Exporters.** The satellite tail and Thoth-local ingest emit provenance for
live spans through one `ProvenanceSink` (`usage_provenance.py`). Backfill walks
every local transcript once: Claude `~/.claude/projects/**/*.jsonl` (including
`subagents/`) and Codex `~/.codex/sessions/**/rollout-*.jsonl`, in batches of
500, resumable by a per-file (size, mtime) cursor; a re-run is a no-op.

```
# satellite host (its satellite.env supplies the WS URL and secrets)
python3 services/chat-stream-v2/satellite.py --backfill --dry-run
python3 services/chat-stream-v2/satellite.py --backfill
# ledger host (Thoth), beside the running daemon
python3 services/chat-stream-v2/tools/backfill_usage_provenance.py \
  --db ~/.local/share/pentacle-stream/sessions.db --host thoth [--dry-run]
```

Records whose transcript is gone stay without provenance; nothing is filled
from the current login. `agent-orch inspect <stream> --json` returns
`usage_provenance`: `account_id`, `account_source`, `conflict`,
`provenance_coverage` (`records`, `with_provenance`: a Claude record with a
provenance row, a Codex cumulative record whose session has a response row) and
the per-native-session identities.

## Percent history

`usage_history.jsonl` beside `sessions.db` on Thoth is append-only. Each line
has exactly `observed_at`, `probed_at`, `host`, `provider`, `account_id`,
`window_kind`, `window_minutes`, `pct`, `resets_at`, `source`, and every value
comes from one observation:

| `source` | Writer | Account | `observed_at` |
|---|---|---|---|
| `cache` | usage collector (Thoth `~/.claude.json`), `agent-orch usage --host <satellite>` | OAuth `organizationUuid` only when `cachedUsageUtilization.accountUuid == oauthAccount.accountUuid` in the same read, else null | `fetchedAtMs` (exact ms); a missing or invalid value writes no line |
| `probe` | usage collector's existing Claude scrape and Codex app-server probe | null | `probed_at`; display only, excluded from fitting |
| `rollout` | daemon, from Codex `rate_limit` items | `creator_account_id` or null | rollout record time; `probed_at` is daemon receipt; `host` is the authenticated pusher |

Claude windows are `seven_day` and `seven_day_fable` (`window_minutes`
10080; Fable from `seven_day_fable` or the scoped weekly `limits` row). Codex
windows use the rollout `limit_id` (`codex`) and the window's own duration, so
a 300-minute and a 10080-minute window are separate lines. The writer drops a
repeated observation: rollout lines by `(provider, account_id, window_kind,
window_minutes, resets_at, pct)` with the first `observed_at` kept, cache lines
by the same snapshot; consumers apply the same tuple. The readback writes only
where `sessions.db` sits beside the file (Thoth). The Codex probe contract is
unchanged. Volume is about 300 lines a day; no rotation.

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

Acceptance is automated in `services/chat-stream-v2/tests/test_usage_accounting.py`,
`services/chat-stream-v2/tests/test_usage_provenance.py` (provenance and history),
`services/agent-orch/tests/test_usage_readback_cli.py` and
`services/agent-orch/tests/test_usage_cli.py`: native-file replay, restart,
truncation, invalid input, generation/host fences, transaction rollback, report
interleaving, lineage, attribution, migration and public readbacks. The v2 merge
gate and agent-orch tests are the final local source gates; passing them does not
establish runtime activation.
