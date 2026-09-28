# Daily retrospective review

The launchd producer runs at 05:00 America/Chicago. It freezes eligible terminal
retros, commissions one Codex GPT-6 Sol/medium draft and one GPT-6 Astra/high
finalization, then sends a retained REPORT notice to the current assistant binding.
assistant reviews immediately, records dispositions, and uses normal work records for
decisions and observed outcomes. Quiet days produce no operator chat message.
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
  "host": "configured-host"
}
```

`state_root` must be outside memory. Preserve it across upgrades and rollback.
Collection manifests commit before their rebuildable enrollment index. Older
first-run history is explicitly **not reviewed**; unreadable sources and whole
originals exceeding 40 sources/64 KiB stay explicit pending coverage. Daily retry
reuses immutable inputs, worker/report identities and frozen delivery keys.

From the installed release, using its pinned interpreter:

```sh
python services/chat-stream-v2/tools/daily_retro.py run --config /absolute/config.json
python services/chat-stream-v2/tools/daily_retro.py run --config /absolute/config.json --on-demand
```

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
