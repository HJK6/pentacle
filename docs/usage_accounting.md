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

## Unfenced spans

A satellite learns a stream's `session_generation` only from the usage fences
on a `host.stats` ack (every 30 s). Before wire version 2, a seat that answered
and closed between two stats acks had no fence, and its usage was lost. A
record read after a same-name reopen was also credited to the new generation.
Wire v2 resolves the generation on the daemon from durable history and the
record's own transcript time. It never uses receipt time or a grace window.

**History.** `v2_session_generation_history` has one row per
(host, session name, generation). The row holds `pane_pid`, `provider`,
`created_at`, `precision`, `closed_at` and the transcript identity
(`native_session_id`, `source_file_identity_digest`).

- `open_session` writes the row with a millisecond `created_at` (`precision='ms'`).
- Opening a new generation over a row that is still open closes the prior row
  at the new `created_at`, so a name has at most one open row.
- Every close path writes `closed_at` in milliseconds at the time of the write.
- Daemon start backfills a row for each open session that has none, keeping the
  second-resolution `sessions.created_at` and flagging it `precision='s'`.
- The identity columns are written once: by fenced admission, or by the first
  credited unfenced record. A reopen never overwrites them.
- A same-name reopen always mints a new generation, so it gets its own row.
- The scheduled retention pass deletes a row only when it closed more than 30
  days ago. Open rows are never pruned. Rows are never archived with
  `sessions`.

**Classifier.** `usage_admission.classify` is the single ordered refusal-only
rule. Both v2 paths call it; only the candidate set differs.

- A fenced item's only candidate is its fence generation's history row.
- A held record's candidates are the rows that match host, session name, pane
  pid and provider.
- The order of checks is: `timestamp_missing`, `no_candidate_generation`,
  `clock_unavailable` (transient), `outside_all_generations`,
  `generation_overlap`, `boundary_uncertain`, credited.

Clock evidence comes from the stats round trip. The ack carries `server_now`,
and the satellite keeps `{offset_s, rtt_s, server_now}`. Each record is
corrected by the hull of its capture-time and send-time samples. Every
uncertainty term only widens the refusal zone. An open row has no end
boundary.

**Clock limit.** The hull bounds the offset only at the two sampled instants.
A clock step or drift between samples that the samples do not show is not
detected. It could place a record on the wrong side of a boundary.

The measured offsets on 2026-10-07 were Amaterasu +0.109 s and Merlin −0.115 s
against Thoth.

**Held-span file.** The satellite keeps
`~/.local/state/pentacle-satellite/unfenced_usage.json`
(`PENTACLE_SATELLITE_HELD_SPAN_PATH`). It holds a span, with each record's
`transcript_ts` and capture clock, in these cases:

- the span has no matching fence;
- the daemon refused the item only because its fence was stale
  (`session_not_open`, `generation_mismatch`, `pane_pid_mismatch` or
  `provider_mismatch`);
- the daemon deferred the item as `clock_unavailable`;
- the daemon is v2 but the satellite has no clock sample from the last 540 s.

The file is fsynced before that pass's events push. Event offsets advance
exactly as before.

The bounds are 256 records and 64 KiB per entry, and 64 entries in all. An
entry expires after 24 h of time with the daemon enabled, or 7 days after it
was written while the daemon is disabled. Every drop is first recorded as a
loss in the same atomic write. A loss has a reason (`held_span_expired_ttl`,
`held_span_capacity_entries`, `held_span_capacity_bytes` or
`held_span_overflow_records`), an id `instance:seq` that is never reused, and
counts that merge per (entry key, reason). The list holds at most 64 records:
60 keyed records and one overflow record per reason.

**Wire and ack.** The satellite sends `usage_unfenced` (entries) and
`usage_unfenced_losses` only while the most recent `event.push` ack had
`wire_version >= 2` and a `usage_unfenced` block. Any other ack, or any push
error, clears that flag. A v1 daemon accepts such a frame and ignores the
fields, so after a rollback at most one frame carries them, and the held data
stays on disk.

Fenced items are classified only when the frame carries `clock`. A frame
without `clock` (every v1 satellite) keeps the legacy admission unchanged.

Inside one store transaction committed before the ack, the daemon does the
following:

- credits records through `record_historical_usage_conn`. This is the
  history-row writer and shares the merge core with the live-row writer;
- persists every terminal refusal in `v2_usage_unplaced`;
- upserts losses with the revision guard.

The ack carries `usage_unfenced: {recorded: [{key, seq}], rejected: [{key, seq,
reason, transient}]}`, `losses_recorded` and `losses_conflict`. Record-level
refusals of a fenced item are added to `usage_rejected` with `index`, so they
never block the stream's offset.

Codex `cumulative` ownership stays with the first generation that credits it.
A later generation's record is reported as
`cumulative_owned_by_prior_generation`.

**Loss conflicts.** A loss payload with a lower or equal `rev` and different
counts is `loss_conflict`. Its symptom is a hold file restored from an older
copy, which is unsupported. When it happens:

- the stored `loss:` row is unchanged;
- the pending payload is kept in a `loss_conflict:` row; from then on every
  payload for that id is routed to that row (a latch) and replaces its pending
  payload when the incoming `rev` is at least the retained pending `rev`;
- the id is never acked as recorded, and the satellite keeps the record;
- the satellite mints ids from a fresh instance after the first conflict.

Conflict rows are never counted. If a restored record's first send already
exceeds the stored `rev`, it silently replaces the stored counts. That case
stays an undetectable limit.

Loss upserts for one host run under one lock. A loss is applied only when the
frame's `request_id` is above the highest applied for that satellite process.
The server serves each request in its own task, so after an ack timeout a later
frame can arrive first; the earlier frame's losses are then neither stored nor
acked, and the satellite sends them again.

**Readback.** On the daemon host, `agent-orch usage --unplaced [--json]` lists:

- refused records per stream, with their reasons;
- loss `records_lost` summed per reason;
- unresolved conflicts, showing both payloads and no recovered count.

Unplaced usage is never measured, placed or priced. The rollup partition does
not read it yet; that is a tracked residual.

A resumed transcript's history replay (its records predate the pane) is
refused. It is listed there, normally as `outside_all_generations` or
`no_candidate_generation`.

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
values differ is counted `response_conflict` and ignored. A live span that
starts mid-turn (no known model yet) leaves that response to backfill.

**Wire.** `event.push` carries an optional `usage_provenance` block
`{version: 1|2, items: [...], dry_run?}` (≤ 2000 items) and requires the source
host proof. The daemon admits versions 1 and 2 and answers with its own
version (2) in the ack; any other version is `unsupported_version`. Version 2
adds the thread proof (§ Thread proof); version-1 items are stored exactly as
before. Every item has exactly `kind`, `provider`, `native_session_id`,
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
live spans through one `ProvenanceSink` (`usage_provenance.py`); cross-span
context resets whenever a tail binds another file or native session. The
satellite attaches pending items within a byte budget (events take
precedence), keeps at most 8000 queued, and stops attaching until reconnect
when an ack lacks the `usage_provenance` block (an older daemon). Backfill walks
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

### Thread proof

Version 2 lets a `codex_response` carry both `thread_token_usage` (the record's
own five-field thread counter after that response: `input`, `cached_input`,
`cache_write_input`, `output`, `reasoning_output`; non-negative, cached ≤ input,
reasoning ≤ output) and `transcript_seq` (its 1-based ordinal among the
rollout's accepted own-thread `token_usage_record`s). Only the whole-file
backfill knows the ordinal; the live tail sends `transcript_seq: null`. A
record without a valid thread counter carries neither field. A malformed field
makes the item `bad_provenance`.

| Table | Key | Holds |
|---|---|---|
| `v2_usage_codex_thread_proof` | host, native session, `response_id`; partial unique index on (host, native session, `transcript_seq`) where not null | `transcript_seq`, the five `thread_*` counters (`INTEGER NOT NULL CHECK >= 0`), `source` (`backfill` with an ordinal, `live` without), `observed_at` |
| `v2_usage_codex_thread_flags` | host, native session, `flag`, `response_id` | adverse evidence: `detail`, `first_seen_at`; insert-or-ignore, never deleted |

The proof row is written in the response row's `BEGIN IMMEDIATE` transaction.
Merge never erases: an identical replay is `replayed`; a `live` row becomes
`backfill` only by gaining its ordinal with an identical vector; a differing
vector or a differing ordinal is not written and is flagged `proof_conflict`;
an ordinal already held by another response of the session is not written and
is flagged `duplicate_ordinal`. The producer reports `counter_reset` (a thread
counter field lower than at the previous ordinal, whole-file pass only) and
`foreign_thread_response` (a copied record whose `thread_id` is another
thread) as `codex_thread_flag` items `{flag, response_id, detail}`; a
version-2 `response_conflict` is kept as `duplicate_response_conflict`. The
ack reports `proof_counts` beside `counts`.

**Negotiation.** A satellite with no version-2 grant in its process sends the
empty probe `{version: 2, items: []}` before its first data batch; ack
version 2 → it sends version 2; `unsupported_version` → it strips the proof,
drops flags, sends version 1 and probes again after an hour. A version-2 data
batch answered `unsupported_version` (a rolled-back daemon) downgrades the same
way. The backfill cursor records the version each file was sent under (an
entry without one is version 1): a pass under version 2 re-sends every
version-1 file once, so responses replay and proof rows insert. The ledger host's own
ingest and backfill call the sink in process and always send version 2.

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

## Per-host collector and the observation tuple

Every execution host (Thoth, Amaterasu, Merlin) runs the same usage-state
collector, so each host's reading is refreshed on its own cadence instead of only
when someone uses the Claude CLI there. Thoth keeps its launchd job at 300 s;
Amaterasu (systemd user timer, `OnUnitActiveSec=600`) and Merlin (launchd,
`StartInterval` 600) are rendered by `services/chat-stream-v2/deploy/install_usage_collector.py`
from `deploy/satellite/`. The install, the schedule enable and the release are
separate steps: `--install` writes the units and a rollback preimage and activates
nothing, `--verify` runs the rendered command once, `--enable` starts the schedule,
`--rollback` restores the preimage.

The job runs from the pinned release checkout with absolute `PENTACLE_CLAUDE_BIN`,
`PENTACLE_CODEX_BIN` and `PENTACLE_USAGE_TMUX_BIN`, a trusted `PENTACLE_USAGE_CWD`,
`PENTACLE_HOST_ID` and `PENTACLE_USAGE_CLAUDE_OAUTH=0`; it carries no secret. Both
probes resolve their CLI from those variables only: an unset or non-executable pin
fails closed as `pinned_executable_missing`, never a `PATH` search. One run at a time
(`usage_state.json.lock`; a tick that finds the previous run still going exits), each
probe is bounded at 75 s and its whole process group is killed on timeout. The
Claude CLI `/usage` scrape is the observation; the CLI's cache refresh is a side
effect that is verified per host, not assumed. The Codex read is quota-only
(`initialize` and `account/rateLimits/read`); no model inference runs.

`usage_state.json` (schema v2) gains one optional top-level key, `observations`,
that never enters the limits rows or frame:

```json
"observations": {
  "claude": {"observed_at": "<UTC>|null", "account_id": "<org id>|null",
             "collection": {"status": "ok|no_update|failed", "attempted_at": "<UTC>", "error": "<code>|null"}},
  "codex":  {"observed_at": "<UTC>|null", "account_id": null, "collection": {...}}
}
```

`observed_at` is the provider's last successful observation: Claude's cache
`fetchedAtMs` when this run refreshed it (never an older stamp), else the scrape's
completion time; Codex's `upstream_reported_at`. `account_id` is the OAuth
`organizationUuid` only under the C1 same-read match, else null (Codex carries no
identity). A `failed` or `no_update` collection keeps the old `observed_at` and
`account_id`, so the value keeps aging and is never re-stamped or re-attributed to a
newer account. The history writers and their dedupe are unchanged (one row per
populated window; `cache` rows dedupe per observation, `probe` rows are one per tick).

`agent-orch usage --host <h>` plucks the host's cadence file and cache in one ssh
call (allowlisted fields only) and selects per provider: (a) a collector-`ok`,
account-matched cadence observation within `--max-age-seconds` (default 900) →
`ok`, `source=cadence-file`; (b) else a newer account-matched cache observation →
`source=claude-cache` with its own age, still showing the collector status; (c) else
the newest matched value as `stale`. A stale or failed reading is never `ok`. Columns:
`STATUS`, `SOURCE`, `OBSERVED (UTC)`, `AGE`, `ACCOUNT`, `COLLECTOR`; `--json` carries
`observed_at`, `age_seconds`, `account_id`, `status` (same as `outcome`) and `collector`.
A host reading its own cadence file applies the age and status rules but cannot
re-check the login account.

## Rollup and calibration

`agent-orch usage rollup` runs `services/chat-stream-v2/tools/usage_rollup.py`
on Thoth (stdlib, Python 3.9+). The CLI finds the tool under the services root
that `_shared` was imported from: the checkout's `services/`, or
`<release>/app/services` in a fleet release. `PENTACLE_USAGE_ROLLUP_TOOL`
overrides the path. It reads `sessions.db`, `sessions_archive.db`,
`notifications.db` and `usage_history.jsonl` with `mode=ro`, plus work-item
frontmatter under `~/agent-workspace/triforce-memory/work`. Its only write is
`--calibrate` output. It never changes admission, attribution storage or the
daemon. It covers Claude and Codex; Codex is reconciled per native session
first (see **Codex** below).

```
agent-orch usage rollup --spec <spec_id>... [--since ISO] [--until ISO] --json
agent-orch usage rollup --project <epic_id|repo> --json
agent-orch usage rollup --comparables --repo <repo> [--kind feature|defect|infra|convention|analysis] [--n 20]
agent-orch usage rollup --calibrate [--redact] --json
```

**Attribution.** A stream counts toward its first spec id, which is the same
rule as the snapshot. An unattributed predecessor in a `handoff_from_stream_id`
chain folds into its successor's spec (`folded_streams`). A predecessor that
already has its own spec keeps it. A project is an epic (`epic:` frontmatter) or
a repo prefix (`spec_<repo>__…`, `-` equals `_`), and it counts each stream once.
`fleet_totals` always reports attributed and unattributed Claude tokens and
dollars per account. A stream that has no seat row counts as unattributed.

**Per spec / project (`claude`).** Each record falls into exactly one partition:

| partition | rule |
|---|---|
| `untimed` | no provenance row, or a row with null or invalid `observed_at`; it is **unplaceable** in time, since seat lifetime and collection times are not usage bounds |
| `unpriced` | timed, but the model is missing from `tools/usage_rollup_config/pricing.json` |
| `unknown_account` | timed and priced, but the identity is unknown or in conflict |
| `measured` | timed, priced, with a known account |

Each scope reports `by_model_account` (tokens by bucket and dollars; an unpriced
model shows `dollars: null, bucket: unpriced`, never zero), `by_account` (tokens,
dollars, and `weekly_pct` computed from `calibration.json`, or null with a reason),
`provenance_row_coverage` (records with a row ÷ records), and `measured_coverage`.
For a scope, `measured_coverage` is measured ÷ all tokens of the scope, so
untimed records stay in the denominator. Dollars are API-equivalent weights,
versioned by `pricing.json` `version`. They are a proxy, not billing.

**Time (`time`).** `elapsed_delivery_h` is `{value_h, endpoint: close}` from the
first seat open to the last seat close. It is reported only for a `completed`
work item that has `completed_at`, when every chain seat has a `closed_at`.
Otherwise it is `{censored: true, elapsed_so_far_h, reason}`, with reason
`not_completed`, `open_seat`, `no_close` or `no_seats`. QA reports are never an
endpoint. Censored items are excluded from comparables. `known_wait_h` is the
union of question-card and hold intervals, clipped to the union of seat
intervals. `activity_proxy_h` is the union of Claude inter-response gaps of 10
minutes or less. It is labelled a proxy, and it is null when any record lacks
provenance. `qa_rounds` counts reports that carry a `qa_verdict`.
`time_to_first_qa_h` is measured from the first seat open.

**Comparables.** These are completed specs by repo prefix, optionally filtered
by a kind tag from `{feature, defect, infra, convention, analysis}`; the legacy
tag `bug` reads as `defect`. Results run newest first, capped at `--n`, and give
`p25`/`median`/`p75` (inclusive linear quantiles) of `elapsed_delivery_h`,
Claude dollars and weekly percent per account. A row with no Claude records has
`dollars: null` (`codex_only` or `no_usage_records`) and is left out of
the dollar quantiles. Each row also carries `codex_dollars` (placed Codex
responses only) and `codex_completeness`; `codex_dollars` has its own quantiles. With fewer than 3 matches the result is
`status: insufficient_comparables`, and `rows` lists the matches found.

**Calibration (`--calibrate`).** This fits one quota: Claude `seven_day`
(`window_minutes` 10080). `seven_day_fable` and other kinds are listed under
`reported_only`. The output is one entry per Claude account, per (account,
`claude`, `seven_day`), and goes to `~/.local/share/pentacle-stream/calibration.json`
(mode 0600; `--calibration-out` overrides). Every entry carries:

- `measured_rollup`: the account's measured, unpriced and ambiguous tokens, plus dollars.
- `provenance_row_coverage` and `measured_coverage`.
- `unplaceable`: the ratio and threshold.
- `identity_mass_by_host` (see below).
- `methods`: points and samples, each with its exclusion reason.
- `exclusions`.
- A `coefficient` in `usd_per_pct`, or `null` with `status`/`reason`.

Coverage is partitioned per (provider, account, quota). A record that belongs to
another account never enters this account's numerator or denominator. Unknown,
conflict and unpriced-unknown mass counts in the denominator of every account
whose `hosts` could own it, which is every host unless the config narrows it.
`measured_coverage` = measured ÷ (measured + unpriced + unknown_account), token-weighted.

- **Unplaceable gate.** If untimed Claude tokens exceed `unplaceable_threshold`
  (default 1 %) of all Claude ledger tokens, every window and interval is
  `eligibility: unknown`. Its coverage is then withheld (`measured_coverage: null`,
  `measured_coverage_if_bounded` printed) and no coefficient is published. The
  ratio is printed with every entry, and `unplaceable.by_host_account` gives the mass.
  Tokens of an honoured retired host (below) are removed from both numerator and
  denominator and reported as `unplaceable.retired_mass` per (provider, host).
- **Retired hosts.** The private config may list `retired_hosts`. A listed host
  is honoured only if, at run time, it has no open seat in `sessions` and no
  `v2_usage_state` row updated in the last 7 days. Open composite assistant rows
  (`provider: composite`, routing aliases such as `bart:assistant`) carry no usage
  and are not counted; the honoured entry reports them as
  `open_composite_rows_not_counted`. Otherwise the entry is
  ignored with a printed warning. `retired_hosts` in the output gives each
  entry's `status` (`honoured` or `ignored`, with `reason`). Retired hosts only
  change calibration; per-spec and project rollups are unchanged.
- **Identity mass by host.** Each Claude record is classified once, in this
  order: `retired` (an honoured retired host, whatever its time or identity),
  then `untimed`, then a window bucket for timed records (`measured`,
  `unpriced`, `unknown_account`, with `conflict` as a labelled sub-count of
  `unknown_account`). A retired host's records therefore enter no window. Every
  entry, every Method A point, the Method B `interval_union`, and the top level
  carry `identity_mass_by_host`: one row per (provider, host) with the three
  window buckets plus `conflict` under `window`. The `untimed` and `retired`
  totals for that host, fleet-wide since they cannot be placed in a window, sit
  under `outside_window_sum`. Summed over hosts, the `window` buckets equal the
  parent's `measured + unpriced + unknown_account` exactly. The top level is
  fleet-wide, not per account.
- **Method A `full_week_100`** (fleet-only account). Windows run from the
  config anchor (Wed 16:00Z) for `days` 7. For each window, `dollars` = Σ measured
  dollars of the account in the window, and `usd_per_pct` = dollars ÷ 100,
  `bias: floor`. A point is used only if the window is completed, not in
  `windows.excluded`, eligible, and has coverage ≥ 0.90. The coefficient is the
  median of the used points.
- **Method B `history_regression`** (fleet-only account). Observations are the
  deduped non-probe `seven_day` history lines of the account, in time order. A
  sample is formed at every observation whose `pct` is above the `pct` of the
  previous pct-change observation (the base). Δ = 0 observations extend the open
  interval instead of closing it, so their tokens stay in the next sample. A
  `pct` decrease forms no sample, starts a new base, and is listed as
  `pct_decrease_new_base`. Each sample pairs Δpct = pct_k − pct_base with the
  measured dollars in (t_base, t_k] and reports `tokens` and `observations`. A
  sample is excluded for any of the following, judged over the whole interval:
  `probe_source` (the line is dropped before pairing), `reset_unknown`,
  `reset_crossing` (any `resets_at` in the interval differs, compared to the
  minute), `stale` (same `observed_at`), `interval_over_24h`,
  `eligibility_unknown`, or coverage below 0.95. `interval_union` gives the
  tokens and identity mass over all formed sample intervals. The fit
  is least squares through the origin, and `residual_mape_pct` is the median
  absolute percent error of Δpct. Malformed history lines are listed as
  `invalid_line`. The method activates only with ≥ 10 valid samples spanning
  ≥ 30 points. An active Method B supersedes Method A.
- **Shared account.** It gets `conversion: null, reason: "shared account; not
  fitted"` unless its config entry sets `transfer_from` together with a
  `justification`. In that case `basis: transferred`, and each window carries
  `external_residual` = observed pct − predicted pct, labelled as an estimate
  of non-fleet use.

**Codex.** The ledger keeps one cumulative row `C_n` per native session
(`uncached_input = input_total − cached_input`, `cache_read = cached_input`,
`cache_write = 0`, `output`); `v2_usage_codex_responses` holds the
per-response rows `R_n` in the same four buckets (`cache_write =
cache_write_input`). `reasoning_output` is a labelled subset of `output`,
reported but never a fifth bucket and never priced. Each session gets exactly
one class before any window logic, computed over every response, timed or not:

| class | rule | mass |
|---|---|---|
| `reconciled` | `Σ R_n == C_n` in every bucket | timed responses are placed at their `observed_at`; null-time responses are `untimed` |
| `partial` | `Σ R_n ≤ C_n` in every bucket, some bucket below (includes a row with no responses) | as reconciled, plus the residual `C_n − Σ R_n` as `unreconciled` |
| `reconciled_by_rows` | would be `unverifiable`, has thread-proof rows, and predicate P holds (below) | the response rows only (the proven prefix): timed rows placed, null-time rows `untimed`; the ledger row is never added and `unreconciled` is zero |
| `unverifiable` | any bucket `Σ R_n > C_n`, or responses with no cumulative row, and not `reconciled_by_rows` | `max(Σ R_n, C_n)` per bucket (`Σ R_n` without a row), only in `unverifiable_mass`; nothing of it is placed, untimed or unreconciled |

**Predicate P** (thread proof, § Thread proof) is evaluated in the same single
deferred read-only snapshot as every other table the rollup reads. With `n` the
largest backfill ordinal: (a) `identity` — a cumulative row exists and the
session's `v2_usage_identity.conflict` is 0; (b) `completeness` — exactly one
backfill proof row per ordinal `1..n`, mapped one-to-one to existing response
rows, and no response row without one (a live-only tail is not proven);
(c) `equality` — the five-field sum of the mapped rows equals the vector at
`n`; (d) `counter_consistency` — vectors non-decreasing along `1..n`;
(e) `flags` — no stored flag of any name, and `malformed_proof` — every stored proof row
re-validated on read (types, non-negative, cached ≤ input, reasoning ≤ output,
`backfill` ⇔ ordinal), since an out-of-band write can pass the column CHECKs;
(f) `ledger_le_thread` — `C_n` ≤ the vector at `n` in every bucket. A timed row
is placement, not completeness evidence. A session with no proof rows keeps
its class and the rollup JSON is byte-for-byte as before: the
`reconciled_by_rows` class key, its block counts and `proof_sessions` appear
only when the ledger holds at least one proof row. Then `proof_sessions` lists
each session P was evaluated on (hashed `session`, `host`, `class`,
`proof_rows`, stored `flags` by name, `malformed_proof_rows` counted on read): proven ones add `source: rows`, `proof_boundary_seq`,
`thread_vector` and per-bucket `ledger_below_thread_by`; the rest name their
failing clauses in `reconciled_by_rows_blocked_by` and stay `unverifiable`
(never `partial` by subtraction). The reconciliation block's
`reconciled_by_rows` entry adds `blocked_sessions` and `blocked_by` per clause.

For reconciled and partial sessions, placed + untimed + unreconciled equals
`C_n` per bucket; for `reconciled_by_rows`, placed + untimed equals `Σ R_n`. A session's account is its `v2_usage_identity` creator
account (or unknown / conflict); creator ids are never pooled. The private
config's `account_aliases` (`[{provider: codex, account_id, alias_of,
justification}]`) folds one id into another only with a non-empty
`justification`; otherwise the entry is rejected with a printed warning. An id
may be an alias source once and never both a source and a target (chains are
rejected whatever the entry order).
Applied aliases make the entry `basis: aliased` (else `creator_account`).

Pricing: Codex models in `pricing.json` are priced by named API-equivalent
assumption classes (`codex_frontier`, `codex_standard`, `codex_small`); an
unlisted model is `unpriced`. Only placed responses are priced; untimed,
unreconciled and unverifiable mass never is. Per spec and project, `codex`
reports placed `tokens` by bucket, `dollars`, `partition_tokens`
(`measured`/`unpriced`/`unknown_account` for placed rows plus `untimed`,
`unreconciled`, `unverifiable`), `outside_window_tokens` by bucket,
`reconciliation` (sessions and tokens per class), `unreconciled_mass` and
`unverifiable_mass` per (host, account), `by_model_account`, `by_account`
(with `weekly_pct` from the Codex calibration) and `by_stream` (spec only).
`completeness` = placed ÷ (placed + untimed + unreconciled + unverifiable),
token-weighted; with `--since`/`--until` it is withheld because the
unplaceable classes have no time. A session with no cumulative row has no
stream and appears only in calibration. `time.codex_activity_proxy_h` is the
union of placed-response gaps of 10 minutes or less, null when any Codex mass
of the scope is not placed.

Codex calibration (`calibration.codex`) fits exactly one quota: history lines
with `source = rollout`, `window_kind = codex`, `window_minutes = 10080` and
an account. Every other line (`codex_bengalfox`, `base_model_inference`, any
300-minute window, probe lines, null-account lines) is listed in
`reported_only` with its count and never enters a sample. The partition is
the Claude one per (codex, account, quota) with the order `retired` →
`unverifiable` → `unreconciled` → `untimed` → window buckets;
`identity_mass_by_host` puts those four under `outside_window_sum`; in an
account entry they count only that account's sessions plus unknown/conflict
sessions on hosts it may own (the top level is fleet-wide). The Codex
unplaceable gate is (unverifiable + unreconciled + untimed) ÷ all Codex
session mass, honoured retired hosts removed from both sides, threshold 1 %.
Method C uses the Method B pairing and exclusions on that quota and, with
≥ 10 valid samples spanning ≥ 30 points, fits both `coefficient`
(`usd_per_pct`) and `tokens_per_pct` through the origin, with residuals.
Otherwise the entry is `status: insufficient, coefficient: null` with a
reason. There is no Method A for Codex, and no probe or Codex workload is run
to make samples. One entry per creator account (from sessions, the quota's
history lines, or a `provider: codex` config account) carries
`reconciliation`, `reconciliation_masses`, `completeness`, `measured_coverage`
and its own `reported_only` lines.

**Private config.** Real account ids live only on Thoth in
`~/.local/share/pentacle-stream/calibration_config.json`, which must be mode
0600 and outside the repository; anything else is refused. Labels must be
non-identifying role names (for example `fleet_only`, `shared`), because
`--redact` prints them. Each account needs an explicit `role`: `fleet_only` is
fitted, `shared` follows the transfer rule, and any other or missing role is
`not_fitted`. `retired_hosts` lists retired execution hosts by name only. The schema is
`tools/usage_rollup_config/calibration_config.example.json` (synthetic ids). A
missing config leaves every account `not_configured`. The public example is
never read. `--redact` replaces account ids with config labels (or
`unconfigured_account_<n>`) in printed output. Use it for receipts. The
private `calibration.json` keeps the real ids for weekly-percent lookups.

Codex acceptance: `services/chat-stream-v2/tests/test_usage_rollup_codex.py`
(AC1–AC7 of `spec_pentacle__usage_codex_rollup_and_calibration_2026_10`).

Acceptance: `services/chat-stream-v2/tests/test_usage_rollup.py` (AC1–AC9
fixtures on the real Store schema, plus the `test_usage_calibration_amend_*`
fixtures of `spec_pentacle__usage_calibration_amendments_2026_10`) and
`services/agent-orch/tests/test_usage_rollup_cli.py`.

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
