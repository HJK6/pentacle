# Front-desk turn-budget observation

## Observer containment

Runtime `PENTACLE_FD_TURN_BUDGET_OBSERVER_ENABLED` and
`PENTACLE_FD_TURN_BUDGET_ENABLED` both default to false. With the observer off,
ingest and pending-receipt callbacks perform no observer reads or writes,
crossings or new notices. Ordinary source ingestion/replay continues and
retained cursors/events are preserved. When the observer is explicitly on and
delivery is off, observation remains durable and record-only. Direct Python
config construction preserves its prior observer-on behavior; runtime
environment construction ships off. Invalid switch values fail startup.

The repair removes archive access from ingestion. It uses bounded hot-store
proof and conservative gaps, with at most one off-path recovery for each
stream, generation and lifecycle. Coverage is deliberately narrower: evidence
that previously required an archive search now leaves unknown coverage. No
archive index, schema change, migration or new background service is included.
Turning the observer on remains a separate requester decision after fleet
scale acceptance. This change does not re-enable it.

The daemon observes only the currently bound direct-primary assistant. Defaults
are five own tool calls, 60 seconds of own work, and **notice delivery off**.
This is advisory: it does not interrupt, stop, pause, kill, delegate, change a
status card, or enforce the existing delegation rule.

## Configuration

Configure the existing direct-primary assistant normally, then use:

- `PENTACLE_FD_TURN_BUDGET_CALLS=5`
- `PENTACLE_FD_TURN_BUDGET_SECONDS=60`
- `PENTACLE_FD_TURN_BUDGET_OBSERVER_ENABLED=false`
- `PENTACLE_FD_TURN_BUDGET_ENABLED=false`

These are daemon startup settings. Invalid values fail startup. Enabling is a
separate operator/integration decision. Disabling delivery alone does not stop
observation. Disabling the observer stops observer work. Retain the database and
archive so existing crossing identities and recovery claims remain durable.

## What counts

A proved distinct inbound USER or inbound TELL begins a turn. The immutable key
is `[stream_id, actual_session_generation, ordinal]`. Same-generation restart
and replay preserve the existing cursor. A proved new authoritative generation
can drop unfinished work and start from a proved new origin at ordinal one.
Changing generation alone does not bypass the hot store's duplicate decision;
same-lifecycle replay cannot establish coverage for a new generation. Restart,
rebind and compaction do not establish a new generation or origin. Late binding
with earlier unobserved events reports a history gap. Different streams never
share a key even when their generation strings match.

Count distinct normalized `raw.tool_use_id` values, not shell subcommands,
results, or thinking. Own time runs from the first ASSIST_TEXT or TOOL_USE
timestamp to the latest ASSIST_TEXT, TOOL_USE, or TOOL_RESULT timestamp. Equal
timestamps are stable. Strictly more than the configured budget crosses it;
exactly five calls or exactly 60 seconds does not. Calls is the dimension when
both thresholds first cross on the same event.

Hot-store source admission and deduplication happen before observer preparation
on both single append and lifecycle-checked batch append. This includes
duplicates within one batch and replayed receipts that revisit a source row.
An ignored source insert performs no observer preparation, reads, cursor write
or crossing work. A newly admitted source, the small existing `kv` cursor, one
immutable `fd_turn_budget` crossing event in `session_event_tail`, and any
enabled outbound notice commit in the same transaction. Rollback removes all
their effects. Binding, target, generation and lifecycle are revalidated so a
change cannot write stale state or effects. Disabled crossings remain
record-only forever; enabling later does not notify them. The next proved turn
can independently cross and notify.

The notice text is:

> Turn budget crossed (N calls / S s). Delegate the remainder and end the turn.

Existing marker, tell ID, receipt, retry and generation-fenced input handling are
reused. The exact internal `fd_turn_budget` kind is eligible for ordinary
nonurgent delivery immediately on crossing, even with the default front-desk
digest enabled. It does not wait for the periodic digest; existing scheduling,
authentication, retained-tell conflicts and generation checks still apply.
Unrelated ordinary notices retain their existing digest policy. Delivery repeats the exact direct-binding generation/revision check
under the existing binding and lifecycle locks. A stale or disabled queued
notice is terminally suppressed, never redirected. A delivered notice itself
opens one zero-work turn, clearing the preceding count/span; it cannot trigger
itself.

## Evidence gaps and recovery

A TELL label or sender-looking text is not proof. Provider-native directed
Codex agent messages retain their existing direction interpretation. Peer and
notice envelopes require matching existing durable provenance, target and body.
Historical Claude queued peers remain USER; the normalizers are unchanged.
SYSTEM/developer setup, sidechains, outbound agent messages and assistant
quotations never become inbound boundaries.

### Hot-store proof on ingestion

Ingestion performs zero archive opens and zero archive queries on single append,
batch append and receipt replay. Newly admitted events use the existing cursor,
source identity/content proofs and indexed hot-store receipt evidence. A healthy
cursor must prove the required identity and prior-dispatch interval within the
existing 128-row retained delta. Incomplete or oversized evidence records a gap;
the observer does not search older history to fill it. Excluded SYSTEM/THINKING,
sidechain events and work in an already-crossed turn can advance a validated
cursor without imposing a generation-length cutoff. Their proof window remains
bounded; a later event requiring older identity or dispatch evidence becomes
unknown if that evidence no longer fits the retained window.

A hot-store miss does not prove that an identity or dispatch never existed.
An archived-only replay, a missing cursor or origin, or an unproved prior
dispatch leaves unknown coverage: an explicit gap, no guessed ordinal and no
notice. A legacy generation with no trusted cursor cannot infer ordinal one
from the oldest surviving USER; it reports `ordinal_history_unproven`.
Corrupt cursors, changed transcript identity and unobserved continuity likewise
leave gaps. The observer does not claim protection for those spans.

A valid saved permanent gap for the same K returns without history or receipt
proof work, while source ingestion continues normally. Existing permanent gaps
do not heal. Here K is the exact tuple
`(stream_id, session_generation, session_created_at)`; binding revision is not
part of K. Same-generation restart or rebind retains proved state and gaps.
Only a proved new authoritative generation with a proved origin can start
fresh, subject to the existing hot-store deduplication rules.

### Receipt eligibility and the one recovery claim

Ordinary tell provenance can arrive after its transcript event. The observer
retains its last proved bounded checkpoint, exact source proofs and each
event's delivery-enabled state, then records `peer_provenance_pending`. While
pending it reports unknown coverage and emits no notice. Further admitted
source events may extend the bounded checkpoint; they do not schedule recovery.

Only the arrival of the exact durable receipt can make that pending anchor
eligible. The checkpoint and exact source IDs, identities, hashes and enabled-state
facts must already be durable, along with all required receipt inputs. The
charged reader checks that the referenced source bodies still exist and match. Indexed send
receipt metadata must match the anchor and trusted sender/digest facts. An
exact `tell_id` delivery row must be present with a bounded stored body size;
its envelope, sender, target and body hashes are validated by the charged
reader after the claim. The presence of that row is readiness to attempt proof,
not proof that its contents are valid. Invalid contents consume the one attempt
and leave a sticky gap.

Duplicate physical receipt callbacks may perform only a fixed number of
indexed, fixed-row hot cursor/eligibility reads. They do not prepare evidence,
read history or archives, schedule a reader, renew a claim or write observer
state. An ignored source insert itself still does no observer work.

For each K, the Store durably claims the whole allowance before any recovery
work: 100,000 charged units, once. The claim occupies the existing `kv` table at
a deterministic key derived from K. Its K and spent allowance are immutable;
bounded status and actual-work accounting may be updated at completion. The
record is retained forever, including across cursor replacement. Storage grows
by one bounded-value key per charged lifecycle, with no cleanup or expiry. There
is no new table, queue, registry, scheduler, daemon or writer.

The claim and cursor transition use the Store's transaction and cursor
compare-and-swap. Eligibility schedules one reader only after the claim is
durable. Duplicate deliveries, repeated gaps, later receipts, daemon restart,
binding revision changes and cancellation never refill the allowance. A crash
or cancellation after claiming leaves it spent, even when final actual-work
accounting could not be recorded. If a later separate recovery would be needed
in the same K, coverage remains unknown.

### Bounded reader and fenced commit

The reader runs off both the main thread and the Store worker. It may look up
source rows by `event_id` primary key and use indexed hot-store receipts. It
must not scan an archive stream/lifecycle cohort or use SQL JSON functions.
Stored body size is checked before fetching or decoding evidence; oversized
evidence remains unknown.

One unit is charged for every SQLite VM instruction and every raw evidence byte
decoded or hashed. Decoding and hashing the same bytes are separate charges.
The shared progress handler reserves its next increment against the remaining
balance, reducing the increment to fit; byte charges are reserved before the
body is fetched, decoded or hashed. The same nonrenewable balance covers all
recovery queries and proof revalidation. Work stops before exceeding it. The
existing 100 ms deadline is an additional limit for the whole recovery job,
including commit proof; it is not restarted per query, database or receipt.
Pre-claim readiness reads are bounded indexed metadata checks and do not fetch,
decode or hash receipt bodies. Terminal fixed-row accounting writes remain
possible after evidence work stops; they cannot replenish the allowance.

The Store alone commits recovery state and effects. Its existing transaction,
binding/generation/lifecycle checks, cursor compare-and-swap and source-hash
revalidation must prove that the original facts are unchanged at commit time.
A complete retained hot-source proof can recover once. Recovery cannot repair
an independent permanent gap or invent a missing origin or ordinal.

If any required source proof is available only in the archive, the existing
Store-owned mechanisms cannot fence that separate database through commit.
The allowance stays spent and coverage remains unknown with
`recovery_archive_fence_unavailable`; no new lock or second writer is added.
Missing evidence, a failed or exhausted reader, stale commit evidence, or a
spent claim with no result also becomes sticky unknown after restart. A
`:memory:` Store cannot provide the off-path reader and records unknown
recovery coverage.

Historical recovered crossings remain record-only. No late notice is sent for
a completed or pre-activation turn. A still-current turn that crossed while
delivery was enabled may emit its one notice after successful proof and commit.
Only current-turn tool IDs, at most the call budget plus one, are retained;
after crossing further calls cannot generate another trigger.

### Gap reasons

Existing reasons remain where applicable, including `ordinal_history_unproven`,
`cursor_corrupt_or_lifecycle_unproved`, `cursor_evidence_unavailable`,
`unobserved_history_continuity_unproved`, `dispatch_proof_delta_exceeds_bound`,
`prior_dispatch_provenance_unproved`, `peer_provenance_pending` and
`pending_provenance_history_unproved`. New reasons are:

- `retained_history_oversized`: retained hot evidence exceeds its body bound
- `recovery_incomplete`: a durable claim has no committed recovery result
- `recovery_spent`: K would require another recovery after its allowance was spent
- `recovery_allowance_exhausted`: the shared work allowance or job deadline stopped proof
- `recovery_failed`: recovery failed without a usable result
- `recovery_stale`: commit-time binding, cursor or evidence no longer matches
- `recovery_unavailable`: the off-path reader is unavailable, including `:memory:`
- `recovery_archive_fence_unavailable`: required archive-only evidence cannot be fenced at commit
- `recovery_claim_corrupt`: a retained spent-claim record cannot be trusted; it never grants another allowance

## Retro and validation

Run the existing collector on demand in the same session:

```
python services/chat-stream-v2/tools/daily_retro.py collect \
  --config /absolute/path/to/retro.json --now 2026-10-10T05:00:00-05:00
```

The collector's primary observations combine readable hot/archive budget events
by immutable identity before time filtering and limits. `primary.turns_over_budget`
contains count, bounded turn IDs, Chicago-local day counts and completeness.
Unavailable hot storage does not hide readable archive evidence. The canonical
aware timestamp parser supplies exact window bounds. Overflow, conflicting/
malformed rows and unavailable stores remain explicit;
partial counts are observed lower bounds, not complete totals. No transcript or
notice body is copied into budget observations. Existing row/byte budgets apply.
The collector caches each local day; a controlled synthetic acceptance journey
must use a fresh isolated state root to avoid reading an earlier same-day packet.

### Direct synthetic checks

Run from the repository root using one isolated Python 3.13 interpreter and the
declared dependencies. Missing declared dependencies are environment errors,
not acceptable skips. These direct tests do not run tmux, a live daemon, Unix
sockets or a gate wrapper:

```sh
python3.13 -m venv /tmp/pentacle-observer-venv
/tmp/pentacle-observer-venv/bin/python -m pip install -r services/chat-stream-v2/requirements.txt -e services/agent-orch
env -u TMUX /tmp/pentacle-observer-venv/bin/python -m pytest services/chat-stream-v2/tests/test_fd_turn_budget.py services/chat-stream-v2/tests/test_fd_turn_budget_no_archive.py services/chat-stream-v2/tests/test_fd_turn_budget_recovery.py services/chat-stream-v2/tests/test_fd_turn_budget_scale.py services/chat-stream-v2/tests/test_daily_retro.py -q -rs
/tmp/pentacle-observer-venv/bin/python -m compileall -q services/chat-stream-v2/store.py services/chat-stream-v2/fd_turn_budget.py services/chat-stream-v2/tests
```

The no-archive and recovery tests instrument archive connections, queries and
reader handoffs rather than using timing as proof. They cover admission before
preparation, repeated gaps, exact receipt eligibility, one allowance across
restart, exhaustion/cancellation, stale evidence and unchanged observer controls.

### Generated archive fixture

`tests/test_fd_turn_budget_scale.py` generates invented data using the production
archive table definition and index set. It includes one large same-stream and
lifecycle cohort, other streams, distinct identities and mixed body sizes. It
records the seed, actual file size, row/cohort/body-size distributions and
`EXPLAIN QUERY PLAN` for recovery queries. It never uses real archive data.

The six phases run in order: healthy new generation, archived identity, a 90%
duplicate mix, permanent gap, pending receipt, then restart and replay with the
same K. Each phase counts all delivery attempts, including zero-insert batches.
The default run uses about 32 MiB and asserts deterministic correctness only:
zero ingest archive access, at most 100,000 charged recovery units per K across
restart, and a terminal result or sticky failure for each admitted recovery.
This generated sample does not establish how often conservative refusals occur
in deployed traffic. Any reported refusal rate must state its eligible-case
denominator and the synthetic workload it describes.

The environment parameters are:

- `PENTACLE_FD_SCALE_BYTES=33554432`: generated archive target bytes
- `PENTACLE_FD_SCALE_ATTEMPTS=40`: attempts per phase; a positive multiple of ten
- `PENTACLE_FD_SCALE_RATE=0`: attempts per second; zero means unthrottled
- `PENTACLE_FD_SCALE_SEED=20261011`: deterministic generator seed
- `PENTACLE_FD_SCALE_REPORT`: optional path for the JSON facts report

Full-size archives of 5,118,746,624 bytes, three times that size and ten times
that size are fleet runs at intake. The ten-minute comparison at 100 attempts
per second with the observer off versus on, and every CPU, latency and wall-time
threshold, also belong to fleet intake. The small correctness run makes no
performance or fleet-acceptance claim.

### Intake and deployed acceptance

At intake, the requester runs
`python services/chat-stream-v2/tools/run_gate.py merge`, the socket smoke test
and full-size scale acceptance against the exact candidate. These are not part
of the direct synthetic run above. Preserve any denied check's error and label
it "fleet runs at intake" rather than treating it as passed.

`tests/smoke/test_fd_turn_budget_socket.py` drives an authenticated private
`event.push` socket, synthetic provider input through real Comms/proof, replay,
and the immediate collector CLI. It checks default-digest-on delivery, disabled
record-only mode, stale revision/generation suppression, the unchanged ordinary
notice policy, and a replay-safe zero-work next turn. The existing merge gate selects these tests.

Source/harness proof is not deployed acceptance. Installation, activation and a
controlled six-command journey on the intended daemon remain separately gated.
Read back the event, one notice and its tell/turn IDs, then the on-demand primary
retro packet. Preserve the configuration/release preimage and rollback pointer.
