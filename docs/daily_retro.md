# Daily retrospective review

The launchd producer runs at 05:00 America/Chicago. It freezes eligible terminal
and explicitly tagged active retros, commissions one Codex GPT-6 Sol/medium draft and one GPT-6 Astra/high
finalization, then sends a retained REPORT notice to the current assistant binding.
assistant reviews immediately, records dispositions, and uses normal work records for
decisions and observed outcomes. Quiet ordinary days produce no operator chat message;
material results and the due weekly account use supported visible delivery.
Historical backfill is a separate bounded, explicitly approved scope.

The producer reuses `tools/live_window` operator authentication, as the scheduled
fleet-smoke tool does. Its allowlist permits only owned spawn/await/close and
current-binding reads plus exact retained REPORT send/receipt operations. Both
workers self-close on terminal reports; cleanup passes their recorded generations.
Each report wait uses calls of at most 900 seconds within one 3,600-second
deadline. A bounded RPC timeout waits again for the same owned report; it never
admits another worker. The total deadline also bounds a stalled transport.
Questions and work review run only as the open current assistant generation with
seat credentials. The bound assistant may be hidden behind its operator composite.
The daemon admits its ordinary durable `prompt.ask` only when the authenticated
stream, session generation and current binding all agree. Other hidden seats
still ask their parent; this path grants no consent or execution authority.

Local JSON configuration (absolute paths; keep credentials outside source):

```json
{
  "timezone": "America/Chicago",
  "memory_root": "/absolute/memory",
  "state_root": "/absolute/local-state/daily-retro",
  "ws_url": "ws://127.0.0.1:7791",
  "token_path": "/absolute/private/operator-token",
  "host": "configured-host",
  "primary_store": "/absolute/private/sessions.db",
  "primary_archive": "/absolute/private/sessions_archive.db",
  "primary_composite": "bart:assistant"
}
```

`state_root` must be outside memory. Preserve it across upgrades and rollback.
Collection manifests commit before their rebuildable enrollment index. Older
first-run history is explicitly **not reviewed**; unreadable sources and whole
originals exceeding 40 sources/64 KiB stay explicit pending coverage. Daily retry
reuses immutable inputs, worker/report identities and frozen delivery keys.

## Continuing Bart and DOT work

The capture owner adds the exact `continuous-retro` tag to the existing Bart
portfolio and each DOT-owning spec. Other active work remains excluded. Keep the
lesson in the existing `## Retro`, with one dated capture-window marker:

```markdown
## Retro
<!-- continuous-retro-window {"start":"2026-10-04T00:00:00-05:00","end":"2026-10-05T04:00:00-05:00"} -->
Outcome and quality; what worked; avoidable stops and observed blocked time
(unknown when unmeasured); correction cause/rounds; an evidence-backed change
and evaluation measure when one is warranted.
```

Both timestamps need offsets, an ordered range ending by collection cutoff,
and overlap with the collection window starting at previous local midnight.
Missing, invalid, future or stale windows and missing/empty or duplicate sections remain
coverage gaps. A marker alone is not a lesson. These are section contents, so
no shared frontmatter schema change is required. Capture at material milestones,
incidents and corrections; continue the next unblocked milestone during review.
No extra message or improvement quota is introduced.

The fingerprint covers the exact Retro section and survives path/status moves.
Changed eligible sections enroll once; unchanged sections and unrelated edits
skip. Whole-source overflow remains pending, never enrolled. Terminal and
explicit historical intake retain their existing rules. A frozen same-day
manifest is reused on retry; adoption after that cutoff enters the next pass.

## Primary evidence and coverage

`primary_store` and `primary_archive` are explicit local paths, opened with
SQLite `mode=ro`, `query_only` and a read transaction. Missing configuration or
unavailable projections are gaps, never all-clear. Primary coverage runs from
previous local midnight through exact collection cutoff; the terminal date
window remains separately labelled.

The packet reads metadata from the current binding, successful rebind audit,
retained session/archived-session states, terminal reports addressed to Bart,
canonical publication references, send receipts, outbound notices and structured
lane digests. It projects identifiers, timestamps, typed states, plan statuses
and checkpoint fields. Report/publication/status step/send text, provider/tool
logs and credential fields are excluded. A report is correlated with operator
delivery only by an exact report-ID publication reference; otherwise publication
is **unknown**. Transport delivery to Bart and operator publication remain
different observations. Current badges do not reconstruct historical state.
The provider `working` flag distinguishes active execution; the specific tool
remains unknown. Only the exact routine suppression codes `persisted_suppressed`
and `folded_into_digest` are excluded from delivery failures, with a coverage
count. Other recorded errors still require inspection.

Each projection counts its observed denominator and reads at most 1,000 rows;
failures are prioritized. One primary original enters the existing worker packet,
with a 24 KiB bound, at most 100 routine observations and explicit deferred counts
by kind and urgent count. It shares the existing 40-source/64 KiB intake bound.
Observed failures and unexplained stalls that fit precede routine samples.
Overflow and unavailable binding/recipient/hold provenance remain visible.
Large binding/rebind reference lists are sampled with observed, retained and
deferred counts; this does not restrict recursive SQL membership. Notices cover
both scoped senders and recipients. Archived plans use the same safe projection
as live plans, with live rows authoritative. A waiting exemption retains the
exact configured composite's matched lane, generation, version and timestamp.
This is a bounded sample, not complete fleet health or historical coverage.

At the first digest with an expired checkpoint or more than two hours idle
without an evidenced wait, inspect the exact dependency/report and act or record
a reason plus next checkpoint. A live tool, retained planner, terminal report
and an exact generation-bound waiting lane are distinct classes. An expired
checkpoint still needs a reason/next checkpoint during a live tool or hold.
Later unchanged age increments do not restart inspection. A closed reviewer or
delivery refusal requires handling on receipt. Hidden prose never establishes
delivery. Existing digests and the daily pass supply these checks; they add no
timer, service, model stage or termination grant.

From the installed release, using its pinned interpreter:

```sh
python services/chat-stream-v2/tools/daily_retro.py run --config /absolute/config.json
python services/chat-stream-v2/tools/daily_retro.py run --config /absolute/config.json --on-demand
```

## Interruption and recovery

A daemon restart can drop the producer's connection while a worker's terminal
report is already retained, or while the daemon is still down. The producer's
retry-eligible RPCs (`await_report`, `await_spawn`, keyed `spawn`) reconnect under
the agent-orch client contract. A refused or dropped socket is retried with capped
backoff until the verb's own retry deadline; `AGENT_ORCH_RPC_RETRY_DEADLINE_S`
bounds that window. The producer then re-awaits the same owned report from the
ledger. It never spawns another worker and never reruns a completed stage. Only
connection loss counts: an `OSError` such as a missing token file fails at once.

A pass that still fails appends to `runs/<date>/failure.json`. The top level holds
the latest failure (`seq`, `stage`, `error`, `notice`, `latest: true`), and
earlier failures move to a bounded `history`, each with its own notice state.

Error records (`error`, `cleanup_error` and `notice_error`) never contain
exception text, because free text can carry a credential in a form that no
pattern anticipates. Each record is `{class, reason, bytes, sha256}`:
- `class`: the exception class name.
- `reason`: a fixed code (`transport_loss`, `timeout`, `os_error[:ERRNO]`,
  `error` or `interrupted`).
- `bytes` and `sha256`: the raw message's byte length and SHA-256.

The raw message is kept only at `runs/<date>/errors/<sha256>.txt` (mode 0600) for
local diagnosis. It is never persisted elsewhere or delivered. The failure notice
quotes the structured record. When the CLI exits on an uncaught error, it
prints only `{"error": <record>}` to stderr (the scheduled job's log file) and
exits 1. The traceback is kept beside the raw message as
`errors/<sha256>.traceback.txt` (0600).

Cleanup errors are recorded as `cleanup_error` and never replace the primary
error; cleanup is retried on the next pass.

The failure notice is queued in `failure-delivery.json` (`pending`) before any
RPC, including on an interrupt, because the daemon may be unreachable. Its
delivery key carries the failure `seq`, so each failure gets exactly one notice. A
newer failure supersedes an older notice that never landed, and a recorded review
supersedes a pending notice. The next scheduled or `--on-demand` pass flushes the
pending notice once (receipt-reconciled, never resent), then resumes the retained
stage. There is no timer or retry loop: while the daemon stays down past the
reconnect window, the next attempt is the next pass.

RunAtLoad catches missed executions after 05:00. Before 05:00 a timer invocation
does nothing; authorized activation uses `--on-demand`. Install only when
`/etc/localtime` resolves America/Chicago. Render the plist's interpreter, release,
config and log paths as absolute values; lint and read back its 05:00 calendar.
Use a durable service interpreter or the release's own virtual environment;
record its resolved path, executable hash and dependency versions. Scratch output
or temporary virtual environments must not appear in the production plist.

assistant reads the retained `astra.json`, verifies evidence and chooses one disposition
per candidate. A review result file contains `packet_hash` and `dispositions`, each
with `id`, `disposition` and `reason`. Allowed dispositions: resolved, duplicate,
no_change, investigate, authorized, propose, defer. Action/investigation/defer also
requires `work_id`, `proposal_id` and `version`, matching a normal work proposal.
Create or reuse that item with the existing triage process before recording review.

New daily packets use `schema_version: 2`. Each candidate has
`recommendation_kind`: `no_change`, `resolved`, `duplicate`, `future_work`,
`immediate_work` or `investigate`. A future recommendation cannot become
`no_change`; an unassigned backlog does not suppress it. `resolved` requires
`outcome_evidence` with `receipt`, aware `observed_at` and observed `measure`.
`duplicate` requires `existing_work_evidence` with `work_id`, `owner` and
`acceptance_receipt`. Legacy receipts and historical packets remain immutable.

```sh
python services/chat-stream-v2/tools/daily_retro.py record-review --config C --run-id YYYY-MM-DD --result REVIEW_JSON_FILE
python services/chat-stream-v2/tools/daily_retro.py decision --config C --work-id spec_existing_item --proposal PROPOSAL_JSON_FILE
```

The proposal file contains stable `id`, exact `scope`, `citations`, concise `title`
and `body`, options (`label`/`value`/optional `description`), accepting `owner`,
`checkpoint` and `success_measure`. A non-question disposition uses `disposition`.
Include the authority reference when work is already authorized. The helper stores
the version hash, question attempts and answer provenance inside that work spec.
The helper locks `state_root/locks/<work_id>.lock`, outside shared memory, and
checks the `spec.md` byte preimage before each update so concurrent edits survive.
Approval remains authorization until an observed success or honest blocker is
recorded there through the normal delivery process.

New defers and proposals linked by schema-2 review also carry `schema_version: 2`
and explicit acceptance/checkpoint fields:

```json
{
  "schema_version": 2,
  "id": "bounded-followup",
  "disposition": "defer",
  "scope": "Recheck the defect at the accepted release checkpoint.",
  "citations": ["spec_existing_item"],
  "owner": "accepting-owner",
  "owner_acceptance": {
    "owner": "accepting-owner",
    "receipt": "accepted-commission-or-report-reference",
    "accepted_at": "2026-10-05T16:00:00Z"
  },
  "checkpoint": {"owner": "accepting-owner", "at": "2026-10-09T10:00:00Z"},
  "success_measure": "The original journey and negative controls pass."
}
```

A named event checkpoint uses `owner`, a stable `event` identifier and a
checkable `trigger_ref`, optionally dated `review_at`. A prose “next touch” does
not qualify. Acceptance cites an actual accepted owner commission or receipt;
the independent reviewer checks it. Schema-2 version hashes include ownership,
acceptance, checkpoint, disposition, measure, authority and citations. Changing
those fields cannot reuse an old approval. Review binds the same disposition
and exact current proposal version. Existing schema-1 decisions can still be
reconciled; they cannot establish schema-2 accountable ownership.
An existing schema-2 proposal cannot downgrade on replay. A changed proposal
without new outcome evidence clears the previous scope's outcome.

The helper retains an immutable local decision receipt pointer; the normal work
proposal remains the authority. Record an observed result through the same
helper with `outcome_evidence`. A grant or source commit alone is not observed
success. `shipped_at` counts only with an observed receipt. Routine work uses
existing authority; only a missing decision uses the versioned question path.

## Weekly accounting

`record-review` saves a rolling seven-day summary and, on Sunday or the next
review catching up that week, one immutable ISO-week receipt. It returns
`weekly_summary.due` for Bart to publish the compact account once through its
supported completion path. Use its stable `publication_key` and frozen summary
bytes for identical retries. Replay creates no second weekly receipt. No extra
model call, timer or question is used.

The summary distinguishes collected/reviewed runs and source/candidate
denominators, dispositions, accepted current work, approval-needed work,
observed outcomes, oldest unresolved work and checkpoint state. It reports
missing days, historical semantic limits, scan/packet overflow and unknown
publication or event-trigger coverage. Overlapping windows count observed
samples, not unique fleet incidents. Unmeasured DOT blocked time/rework remains
unknown. Primary coverage has one set of per-table observed/scanned/overflow
totals, observation counts and completion-correlation counts.

When an outcome receipt measures a DOT milestone, use
`measurement_scope: "dot_milestone"` and measured `blocked_seconds`,
`correction_rounds` and/or `avoidable_stops`. Each measure retains its observed
sample count and total; absent values remain unknown, including unmeasured days.
Only observed outcome receipts within the named week count as that week's
verified/shipped results. Older retained unresolved work remains visible.
Outcome accounting deduplicates exact receipt references from retained resolved
review rows and current normal-work proposals. Prior proposal versions without
retained outcome receipts remain unknown. Inspect retained receipts without
changing them:

```sh
python services/chat-stream-v2/tools/daily_retro.py summary --config C --end-day YYYY-MM-DD
```

Before asking, assistant reconciles all prior questions. Existing live questions remain
live across a hot rebind. After expiry, one current-generation question replaces
the old question. Answered old-generation rows are recovered; stale-version answers
never authorize changed scope. Unknown status or inability to retire an old live
question blocks a new ask. Intent commits before ask, making response-loss retries
idempotent. Silence never grants authority. Re-run `decision` for each still-open
normal work proposal after assistant replacement, using the stored proposal fields.
Before that daemon admission is deployed, a refused hidden ask retains the entire
proposal/version as `ask_blocked` and sends one durable REPORT to the bound assistant.
Repeated calls reconcile that REPORT without retrying the question. Once the daemon
is live, explicitly resume with `decision --retry-blocked`; silence never authorizes work.

Focused isolated validation:

```sh
python -m pytest services/chat-stream-v2/tests/test_daily_retro.py services/chat-stream-v2/tests/smoke/test_daily_retro_socket.py -q -rs
python -m pytest services/agent-orch/tests/test_close_generation.py services/agent-orch/tests/test_close_cli.py -q -rs
```

`daily_retro_surface` runs a real socket server and stores on a temporary port,
with separate credentials/tree/state and `fixture-chat:assistant`. Only provider/
tmux is a counterpart. Synthetic questions never touch the production daemon or
the production assistant surface. The fixture proves answer/version recovery, quiet delivery,
authorization allowlist, replacement preservation and observed fixture outcomes.

The separately gated real-worker rehearsal uses six source fixtures in
`tests/fixtures/daily_retro_sources.json`, with fixture files in its `files` object
and a normal active `spec_fixture_existing_owner` work record. Date each terminal
spec in the previous local day. Run a `daily_retro_surface` counterpart configured
to record the received packet review, supply TEST_C with `isolated:true` and its
explicit non-7791 endpoint/credentials, and WORKER_C with existing worker transport:

```sh
python services/chat-stream-v2/tools/daily_retro.py rehearse --config TEST_C --workers-config WORKER_C --evidence-dir E
```

This reads originals through real Sol/Astra reports. A declared fixture mutation
omits the serious shortlist entry and inserts a small evidence gap in the repeated
case; the original report and every disposition remain retained. Astra must recover
the serious insight, group the repeated issue, correct the gap and retain substantial
uncertainty. Delivery and assistant helper review stay on the isolated Codex/provider
counterpart. It is distinct from real production first-run delivery and review.

Release staging requires a fresh immutable SHA directory:

```bash
set -euo pipefail
: "${RETRO_CHECKOUT:?reviewed public checkout required}"
: "${RETRO_CANDIDATE:?approved full SHA required}"
: "${RETRO_RELEASE:?fresh release path required}"
test ! -e "$RETRO_RELEASE"
test ! -L "$RETRO_RELEASE"
install -d -m 0755 "$RETRO_RELEASE"
git -C "$RETRO_CHECKOUT" archive "$RETRO_CANDIDATE" \
  services/chat-stream-v2/tools/daily_retro.py \
  services/chat-stream-v2/message_envelopes.py \
  services/chat-stream-v2/provider_wrappers.py \
  services/chat-stream-v2/tools/live_window services/_shared/operator_auth.py \
  services/agent-orch/agent_orch \
  services/chat-stream-v2/deploy/com.pentacle.daily-retro.plist \
  docs/daily_retro.md | tar -x -C "$RETRO_RELEASE"
```

Hash extracted bytes, pin interpreter/dependencies, save previous label/plist/config
identities, and obtain the runtime-window owner's GATE before landing or activation.
Use one batched window: owned test label bootstrap/print/kickstart/bootout, isolated
installed rehearsal, initial real on-demand run and assistant review, then production
label bootstrap/calendar readback. Retain separate delivery/decision-ready latency
and cleanup receipts.
Do not restart the daemon. Reject moved preimages.

Rollback bootouts only `com.pentacle.daily-retro`, restores the exact previous
plist/config/release pointer, and bootstraps the old label only if it existed.
Otherwise verify it absent. Keep manifests/decisions intact. Public rollback is
a reviewed revert through ordinary gates, never a force push.

## Explicit historical pilot

History uses a separately gated private config and state root. Keep the daily
config, timer and state unchanged. `history-collect --config HISTORY_CONFIG
--baseline DAILY_COLLECTION_JSON --batch 1` freezes only the initial daily
baseline originals, verifies every fingerprint and selects one deterministic
batch of at most 40 sources and 65,536 original UTF-8 bytes. Missing, changed,
malformed or oversized originals refuse the whole intake. The private
baseline must be a dated daily collection with its matching enrollment projection;
history state containing daily enrollment records, or nested within them, refuses
even when the supplied baseline was copied to another directory. The private
`history.json` retains complete originals, the baseline file SHA, inventory
digest and all batch boundaries. Replay uses this snapshot, refuses conflicting
baseline bytes, and checks the full collection manifest before worker admission.

`history-run` with the same arguments processes only that selected batch through
the existing Sol/medium, Astra/high, durable current-Bart report and fenced
cleanup path. Its `history-<digest16>-<batch4>` identity and separate state root
keep worker/report/delivery keys distinct from daily runs. The current assistant
records its actual packet review with the same `record-review` helper and run ID.
Reviewed batches are no-ops; no next batch is automatically admitted and no
per-batch Bart review barrier is added.

Source landing and ONE installed pilot need separate parent gates. Pin the
baseline, config, interpreter, source and release hashes before execution; use
the reviewed checkout explicitly for both remote checks and archives. Capture
actual tokens (including incompleteness), latency, new versus already tracked
recommendations, delivery and owned-generation cleanup. After the pilot, the
owner decides whether to continue the remaining batches. Authorized serial
continuation and selective checkpoint/final consolidation are described below.
Rollback only owned history config/generations; retain its evidence and protect
the active daily timer/state. No daemon restart or calendar bootstrap is needed.

## Serial historical continuation

After an explicit continuation grant, prepare each remaining batch sequentially:

```sh
python services/chat-stream-v2/tools/daily_retro.py history-run --config HISTORY_CONFIG --baseline DAILY_COLLECTION_JSON --batch N --no-deliver
```

This pins a cumulative snapshot of prior validated findings, reviews and work
references. Both workers read every original, but avoid renewed investigation of
an equivalent unchanged finding. Recurrence, changed evidence, ownership gaps,
revised actions and new grant needs remain eligible. Finding versions are
computed from substantive content; a matching issue label or work link alone
cannot suppress a changed recommendation. Full original dispositions and all
candidates/citations stay in the private per-batch packets.

Quiet preparation performs no delivery or binding RPC, including on failure.
It retains local failure evidence, closes only its recorded worker generations
and stops. Report the blocker to the runtime owner; failed worker recovery needs
an explicit owner ruling. Repeating completed preparation admits no new workers.
A run pinned to this mode cannot later send an individual packet.

At an authorized checkpoint (up to five newly prepared batches):

Checkpoint and final compilation accept retained `evidence_sources` mappings or
lists. List entries keep their original IDs and contents; conflicting batch-local
labels are also retained under qualified keys. Raw worker packets and candidate
citations remain unchanged during compilation and replay.

```sh
python services/chat-stream-v2/tools/daily_retro.py history-consolidate --config HISTORY_CONFIG --baseline DAILY_COLLECTION_JSON --batches 2,3,4,5,6
```

The compiler sends one REPORT only when new relevant finding versions exist.
Already surfaced unchanged versions are counts-only; changed occurrences survive.
An empty checkpoint is retained privately and sends nothing. Deprecated, retired,
shipped/resolved and accepted owned unchanged subjects are excluded only on
current evidence. An unassigned triage backlog is not executing ownership.
Ambiguity and recurrence/evidence/ownership/action/grant exceptions remain visible.
Checkpoint selection uses the preceding Astra annotations; it adds no model pass.
Partial checkpoint overlap or an unresolved preceding delivery refuses.

When the entire frozen inventory is complete, including previously reviewed runs:

```sh
python services/chat-stream-v2/tools/daily_retro.py history-consolidate --config HISTORY_CONFIG --baseline DAILY_COLLECTION_JSON --final
```

One additional self-closing Astra/high reviews the full findings catalogue,
original/disposition references, prior reviews and current work. Its complete
keep/drop alias audit remains retained. Compilation preserves every original
source disposition and citation. One comprehensive REPORT reaches the current
assistant, even when it contains only counts. Prior checkpoint reviews remain
linked; the final report never grants another commission for the same action.

Synthetic `history-consolidated-<digest64>` runs use the same `record-review`
helper and exact packet-hash/current-generation/proposal checks. They do not
fabricate per-batch assistant reviews. Replay uses retained reports and immutable
delivery keys; pending sends reconcile their receipts. An unreviewed report can
be delivered once to a replacement current generation; reviewed replay is a
no-op. Final replay does not commission another Astra pass.

Keep the daily label, plist, calendar, configuration, state and daemon untouched.
Historical execution uses a separately pinned immutable release/interpreter.
Respect the granted serial window and any daily exclusion band, leaving enough
time to close owned generations before either ends. Stop on an unresolved worker,
cleanup, input or delivery failure. Retain the exact packets and raw usage,
including incomplete counters, coverage, checkpoint yield and cleanup receipts.
Do not run an older delivery-only tool on new quiet-mode state during rollback;
that could send the private per-batch packets individually.

Weekly source-gap totals now describe the latest valid retained inventory rather
than adding the same gaps across daily samples. The projection reports distinct
current gaps, additions observed during the week, and snapshot coverage. A
missing or invalid inventory remains unknown; an older inventory is marked stale.
Primary-store gaps retain their separate sampled denominators.

The initial retained inventory baselines only `missing Retro` gaps under
`work/completed/` or `work/deprecated/`. This is a terminal-format baseline; its
age before the convention is unproven. Active, operational and provenance gaps
remain actionable. Additions use set differences against the latest prior
snapshot, including transient gaps. With no prior snapshot, the first observed
inventory supplies the comparison and the left boundary remains unknown.
Retained daily, review and weekly receipts are immutable; projecting a past week
does not rewrite its published receipt.

Publish the weekly gap headline with new-this-week and actionable current first.
Keep the retained initial terminal-format baseline on one labelled line; never
combine it into a summed daily-gap headline. Unknown and stale inventory labels
remain visible.
