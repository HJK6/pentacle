# Chat stream daemon

The daemon launches configured Claude/Codex CLI sessions in tmux, ingests their native JSONL transcripts, persists session and coordination state, and serves WebSocket clients. It includes the `agent-orch` coordination RPCs, assets, schedules, notifications and authenticated desktop/mobile transport.

Use the complete [local startup recipe](../../README.md#local-setup) first. It aligns the desktop and daemon host identity (`local`), native transcript directory, loopback endpoint and desktop credential. Python 3.11+ and tmux are required; provider CLIs require their own accounts and authentication.

## Private configuration

`PENTACLE_MACHINES_FILE` selects a private machine JSON file; `PENTACLE_MACHINES_JSON` supplies the same shape inline. [machines.example.json](machines.example.json) demonstrates a single local machine. Set executable locations and working directories for your machine. `projects_root` must point to the actual Claude `.claude/projects` directory. A daemon's `--local-host` must match its machine name and the host used by clients.

Without peer configuration, the daemon runs locally. To keep specs and QA records outside the checkout, configure the documented memory root for the orchestration CLI and daemon. Do not put transcripts, credentials, runtime databases or real work receipts in the public source tree.

`python services/chat-stream-v2/main.py --help` lists runtime options. `--port 0 --db :memory:` is useful for a temporary probe; ephemeral sessions alone do not isolate every optional service database. The desktop smoke explicitly sets separate session, notification, asset and blob paths and uses its own tmux socket.

## Connecting during startup

The listener binds before startup adoption finishes. A connecting client waits
for persisted sessions to be adopted and the runtime SHA to be resolved before
receiving its first welcome or any broadcast. Its first hello snapshot therefore
includes the adopted inventory. Authentication challenges are created after this
wait; disconnecting during startup releases the pending handler.

SHA resolution keeps its existing checkout, deploy-stamp and VERSION fallbacks.
If none provides an identity, the daemon logs that limitation. The desktop's
five-second welcome timeout can still require a reconnect during an unusually
slow startup; the existing git lookup timeout is ten seconds.

## Interrupted input and spawn recovery

Accepted direct-assistant input retains its original binding identity. When fresh
local pane and process checks prove the holder died unintentionally, the daemon
keeps input queued before dispatch. It releases that input only to a recorded
restore, generation handoff, or committed host-transfer successor. An unrelated
rebind does not inherit the old input; new input uses the current binding.

An interrupted spawn with a saved pre-paste watermark and a live owned pane stays
`indeterminate` with `spawn_delivery_unknown` until positive delivery evidence
arrives. Status, await and same-key retry retain the original handle and report
prompt submission as unknown. They never repaste. Inspect or await that seat; an
authorized close does not undo provider work. A NULL intent with no pane reports
`spawn_interrupted`, rather than an objective validation error.

Before downgrading to an older daemon, retain or explicitly disposition queued
recovery inputs and unresolved spawn handles without replay: older binaries do
not enforce the new route-origin fence.

## Desktop and mobile credentials

Unauthenticated local bootstrap is only for actual loopback peers whose
WebSocket handshake omits `Origin` (native CLI tools and the Node web bridge).
Any supplied `Origin`, including a local, empty or `null` value, requires the
same authentication as a remote peer for reads, snapshots and broadcasts.
Authenticated browser/mobile clients continue to use their existing credentials.
This isolates websites; it is not authentication against other local processes.

`tools/operator_auth_cli.py issue --client-kind pentacle` issues a desktop credential; `--client-kind pentacle-mobile` issues a separate mobile credential. The JSON output's `code` field is secret. Store desktop credentials in a mode-0600 file under a mode-0700 directory and point `chatStream.tokenPath` at its canonical path. Do not place a v2 credential inline in the desktop config. The CLI's `list`, `revoke` and `rotate` commands manage credentials in the daemon's registry.

For the mobile app, create a **single-use enrollment link** on the daemon host,
as the same OS user running the daemon. This is separate from the raw credential
envelope returned by `operator_auth_cli.py issue`; that envelope cannot be used
as an eight-character enrollment code.

```sh
python services/chat-stream-v2/tools/mobile_enrollment_cli.py \
  --ws-url ws://127.0.0.1:7791 --label simulator
```

The JSON contains a secret `url` that expires after ten minutes and can be used
once. Open it in the installed mobile app. The app exchanges the code with this
daemon and stores the resulting credential in SecureStore. Create the link only
after the app is built and the daemon is reachable. The issuer writes the
existing private `~/.config/pentacle-mobile/enrollment-codes.json` registry with
owner-only permissions; no daemon restart is needed. `--ttl-seconds` accepts
1–3600 seconds. Do not commit or publish the link.

For a physical phone, replace loopback with the daemon host's reachable LAN/VPN
endpoint and bind the listener to the appropriate interface. Keep access on a
private network or authenticated tunnel; use WSS if traffic crosses an untrusted
network. See [network access](../../docs/REMOTE_AUTH.md) and the complete
[mobile setup guide](https://github.com/HJK6/pentacle-mobile/blob/main/docs/FRIEND_SETUP.md).
An SSH tunnel can retain a loopback daemon listener; forwarded connections
inherit the tunnel endpoint's trust boundary. Restrict tunnel access to the
operator.

## Remote session interrupt

`send.interrupt` targets a session by `host` and `session_name`. For a configured
remote host, updated web and desktop clients also send the selected open row's
`expected_session_generation`. They capture that value before handing the
request to an asynchronous bridge and retain it for a retry. A remote request
without the selected generation returns `generation_required`; the daemon does
not substitute the current row's generation. The existing local request may
omit it.

For a remote request, the daemon requires an open durable row of the expected
generation and an SSH tmux pane whose session name and PID match that row. It
rechecks the row and exact pane identity before sending one Escape to the
checked pane ID. Unknown hosts, unreachable peers, changed rows or panes, and
missing identity return typed errors without sending a key. A missing pane
returns `pane_unavailable`. A live pane returns `interrupt_unconfirmed` after
the key because a successful tmux send does not prove that the provider stopped
its turn. Installed mobile clients that lack selected-generation propagation
receive `generation_required` for remote targets until their separate client
release; this daemon release does not claim mobile remote interrupt support.

## Approved close deferral

An approved lane close that returns `close.deferred` stays actionable in the
existing durable ruling loop. Each retry uses the ordinary close path and its
generation, report, requester and liveness fences. Once capture proves idle,
the original request closes exactly once, including after a daemon restart.
Definitive refusals remain terminal. The existing 600-second unruled deadline
policy is unchanged.

## Codex terminal provider errors

When a Codex rollout records `event_msg/task_complete` with a nonempty `error`,
the shared normalizer emits an `ERROR` event. Local ingestion and satellite
ingestion retain it in the ordinary event tail, so chat and inspection show why
a turn stopped even when Codex emitted no final assistant message.

Capacity failures use fixed text identifying `server_overloaded`; other errors
use a fixed generic message. Arbitrary provider messages and details are not
copied. The event includes the turn ID and an allowlisted error code. Turn IDs
provide replay identity, so re-reading a completion does not duplicate it.
Successful completions and token bookkeeping remain excluded. This visibility
does not resume a task, switch models, or produce a completion report.

## Tests and supported integrations

```sh
python -m pip install -r services/chat-stream-v2/requirements.txt -e services/agent-orch
python -m pytest services/chat-stream-v2/tests
python -m pytest services/agent-orch/tests
python services/chat-stream-v2/tools/run_gate.py merge
```

Run tests in a harness-owned tmux session on an explicit `tmux -L <test-socket>` namespace, with `env -u TMUX` before Python. The gate runner and preflight refuse an inherited `TMUX`; daemon fixtures clear it and use their own explicit socket. Never run bare `tmux kill-server` against ambient operator state.

Codex guardian review rollouts are internal bookkeeping. Local and satellite
ingest exclude them by their `session_meta` provenance before selecting a seat's
conversation transcript; genuine messages and operator approval prompts remain
unchanged. Previously stored events are retained without rewriting history.

Run the final merge gate from a clean checkout; it produces a source-bound evidence file outside the repository. The macOS preflight requires the [127.0.0.2 loopback alias](deploy/loopback-alias/README.md). See [public runtime boundaries](../../docs/public_release.md) for unsupported private integrations and the real desktop smoke. Fixtures must use invented content and valid public wire identifiers.

Public CI sets the existing unit-only budget `V2_UNIT_TIMEOUT_S=1200`; smoke and
soak limits keep their defaults. Pytest scratch is separate from
`V2_GATE_EVIDENCE_DIR`. On failure CI retains the gate logs/JUnit/receipt and the
synthetic stall helper's complete output, result and fixed sidecar allowlist for
seven days. Runtime databases, arbitrary scratch and linked files are excluded.
Helper failures and timeouts remain failures; use the retained first cause rather
than rerunning the whole gate. Hard job cancellation can prevent artifact upload.

Broadcast queues replace pending `session.inventory` and `hosts.stats` snapshots,
and `working.state` snapshots for the same stream, at every enqueue. The newest
state stays at its arrival position after intervening events; an in-flight send
is never replaced. Other existing state families retain warning/overflow
compaction. Chat events and all non-coalescible frames (including
`work_lanes.inventory`) preserve their exact bytes and order. An incompressible
full queue still disconnects with 1011/`slow_consumer`, signaling the client to
reconnect and refetch.

### Scheduled spawn fleet smoke

Synthetic warning and failure cards are published only in a harness-owned
temporary loopback daemon with a unique `smoke-*:assistant` composite, then
removed with its state. The harness refuses the configured operator composite
as a target. Fleet RPCs never submit these cards through `notification.create`;
failures and disposable-publication receipts remain in the runner's JSON/log
output. PASS, FAIL and UNTESTED exit behavior is preserved.

`tools/spawn_fleet_smoke.py --dry-run` resolves the full matrix without opening a WebSocket or spawning a session. The no-argument full run requires `PENTACLE_MACHINES_FILE` to point to the same file configured for the daemon. It rejects a missing file or `PENTACLE_MACHINES_JSON`, reads host names from that file, and includes every configured host by default. Set `PENTACLE_SMOKE_EXCLUDED_HOSTS` to a comma-separated list of unique configured host IDs only when a deployment explicitly excludes them; unknown names and an empty final matrix are refused. An optional `PENTACLE_SMOKE_HOSTS` subset must contain only remaining configured hosts. The dry-run output names the source, selected hosts, explicit excluded names and their configuration source, and every host/provider/prompt cell. A machine entry may set `"smoke_optional": true` for an intermittently available host. Only its proven `host_unavailable` cells become `SKIPPED_OFFLINE`; quota, missing evidence and failed cells retain their existing verdicts. Both the scheduled runner and deploy verdict retain skipped evidence and allow PASS when the required hosts pass. The flag defaults to false and must be a JSON boolean.

The launchd template under `deploy/` runs every 12 hours and sets the machine-file path. The deploy helper also passes the installed daemon’s machine-file setting explicitly to its immediate full smoke, clearing inherited JSON configuration and host-subset overrides. A missing or invalid setting remains a post-activation smoke failure (exit 6); the activated SHA/PID stamp and do-not-retry instruction are preserved. A full run closes each spawned session, including after cell failure; a failed teardown is reported as a failure even if the provider is over quota. The explicit `<url> <host>` post-deploy canary remains a single Codex promptless cell.

### Transcript ingest fairness

Cold ingest replays open streams from the beginning. Each pass retains the shared
500-new-event cap and starts after the last visited stream, using stream identity
and inventory order. If that stream disappears, its first surviving successor
from the previous inventory goes first. Backoff skips, zero inserts and handled
failures advance the scheduling frontier; a backoff skip does not attempt ingest.
Each snapshot row is visited at most once per pass. Per-stream offsets, durable
dedupe, lifecycle fences and byte limits remain intact, so finite old transcripts
continue replaying without skipping history while fresh streams receive service.

For a fixed finite inventory of N continuously eligible streams, each receives
an ingest attempt within N completed passes while the actor runs. Churn, actor
restart and whole-pass backoff do not inherit this bound. Wall time is conditional
on bounded pass duration; a prompt buried within its own stream's history may
still need more service before its durable USER proof appears. This scheduling
bound alone does not guarantee readiness within the prompt-proof deadline.

### Busy-seat send probe

`tools/busy_seat_send_probe.py --provider claude|codex [--host H --ssh USER@HOST]` reproduces a send to a working seat without a UI: it spawns one low-cost hidden seat, holds it in a ~75 s foreground command, sends once through `send`, and compares `send.result` with the pane's native-queue chrome. Verdicts: `queued_reported` (0), `queued_unreported` (1), `not_queued` (2), `harness_error` (3, including a failed cleanup close). The seat is always closed unless `--keep`. Codex 0.159 needs a working sandbox on the seat host (`bwrap` on Linux).

### Router replay harness

`tools/router_replay.py` measures the same `_router_input` builder and SSH
`AssistantRouterAdapter` used by the daemon. It selects a deterministic,
redacted set of Claude transcript sessions, interleaves real turns with the
short-reply and burst cases, and freezes each case's canonical router-input
JSON bytes plus `router_input_sha256`. A run writes only a manifest, report and
run-scoped `misses.jsonl`; it never edits the fixture automatically.

Configure `PENTACLE_ASSISTANT_ROUTER_ENDPOINT` and
`PENTACLE_ASSISTANT_ROUTER_ACTION_PATH`, then run from an isolated harness
session:

```sh
python3 services/chat-stream-v2/tools/router_replay.py \
  --manifest /tmp/router-replay/transcript-manifest.json \
  --seed 20260921 --threads 3 \
  --report /tmp/router-replay/report.json \
  --misses /tmp/router-replay/misses.jsonl
```

Candidate runs must reuse the same manifest and harness configuration and may
add `--baseline-report /tmp/router-replay/baseline.json`; a changed
`harness_config_sha256` or per-case router-input digest is a `HARNESS_ERROR`.
Compare the report before an explicit, reviewed merge:

```sh
python3 services/chat-stream-v2/tools/router_replay.py \
  --misses /tmp/router-replay/candidate-misses.jsonl \
  --merge-misses --fixture services/chat-stream-v2/tests/fixtures/assistant_router_v1_cases.json
```

The router input carries bounded `last_outbound_excerpt` and
`last_outbound_truncated` values for each open lane. They come only from the
newest durable `ASSIST_TEXT` publication associated with its persisted route;
absent output is `null`/`false`. Transcript, manifest, report, fixture and
diagnostic sinks are redacted fail-closed. Keep raw run artifacts outside the
repository and do not put credentials or unredacted transcripts in fixtures.

Lane publication excerpts never replace the current operator input's
`body_excerpt`, `body_truncated` or `original_length`. On a nonzero router
process exit, the durable `router_failure` retains `returncode`, `stdout` and
`stderr` alongside the exception and elapsed time. Each stream captures only
its first 2048 bytes, decoding UTF-8 with invalid/incomplete sequences dropped.
Exit 2 with a bounded stderr JSON object whose `error` is
`assistant_router_failed` records `assistant_router_script_failed`: the remote
script reported an exception, including inference failures. Other exits record
`assistant_router_transport_failed`, an unclassified process/SSH failure rather
than proof of a network outage. Fallback resolution preserves this evidence.

Committed history batches yield between broadcasts so existing socket writers can drain. Each client still has one writer and a 256-frame queue; a blocked writer remains subject to `1011 slow_consumer` isolation. Smoke failure records retain the full exception chain, including the original cell failure when teardown also fails. Preserve the smoke command's complete output when investigating a failed deployment; the deployment stamp contains only a shortened command detail.

## Context crossing and compaction

`NudgeJob` sends a `context_advisory` notice to an open, reachable Claude
seat and its current live non-Codex parent, including hidden and working seats.
Parentless seats also get a daemon-owned operator notification through `Notify`.
Codex seats, including the configured assistant backend and Codex parents of
Claude children, receive no context-pressure notices. Title/card reminders
keep their existing visible, idle, operator-engaged filter. Context notices
do not refuse spawns or implement a `handoff_planned` exemption.

Claude defaults are advisory at `min(400000, 70% of model window)` and compact
at `min(500000, 85%)`. `PENTACLE_CONTEXT_ADVISORY_ABS`,
`PENTACLE_CONTEXT_COMPACT_ABS` and their `_PCT` overrides are supported.
The Claude seat bound as the assistant front desk uses 150000/200000
(`PENTACLE_CONTEXT_ASSISTANT_ADVISORY_ABS`, `PENTACLE_CONTEXT_ASSISTANT_COMPACT_ABS`).
Codex still reports tokens and model window but always has level `none`.
Routine context-threshold handoff is retired; deliberate, scheduled and
recovery handoff remain independent of the level.

Telemetry ingestion atomically stores the reading and crossing episodes in
`v2_nudge_state.basis`. The compact episode persists through compact →
advisory → compact and rearms only after a reading below advisory or a new
source generation. Restart preserves the episode and its input outcome; close
prunes its state. Advisory keeps its one-delivery-per-recipient episode.

With `PENTACLE_CONTEXT_COMPACT_ENABLED=1` (the default), a fresh compact
reading can submit one daemon-owned `/compact` command with a proven empty
Claude composer, including while working: Claude queues the initial command.
The source lifecycle lock and Comms pane input lock serialize this with
observations and cooperating daemon input. They do not exclude independent
terminal typing. A durable attempt ID and USER-event watermark are recorded
before possible pane input. Only a matching current-generation USER event
proves submission; native queue visibility alone is not a receipt.

Ambiguous input stays `pending_input` for at least
`PENTACLE_CONTEXT_COMPACT_COOLDOWN_S=600`. After that, a seat still at compact
level can retry only while idle with an empty composer; busy or unknown turn
state retains the possible-input fence, including across restart and crossing
changes. A proven pre-input failure can retry after the same cooldown while
working only when no earlier command remains uncertain. A confirmed submission
consumes the episode, so continued high context does not repeat the command. Disabling the action leaves advisory and
pending-input state intact and suppresses compaction defer alerts.

The empty-composer predicate recognizes the provider's exact update banner
variants and main-selected agent roster below the composer rule and permission
footer. Drafts, unknown chrome and the ambiguous queued-message placeholder
remain refusals. A persistent pre-input hold after the compact cooldown from
crossing observation raises one content-free notification per episode; it never
forces input through a draft. Ordinary messages and slash commands have no
promised cross-type FIFO order.

Only valid readings no older than 1800 seconds and no earlier than session
creation qualify. Future/malformed readings, offline/dead sources and routing
mismatches are suppressed. An unavailable parent is deferred; it is not treated
as a parentless seat. The retired park API is not restored.

Each recipient uses a stable tell ID and frozen body. A delivered receipt prevents
resending; a committed but unconfirmed paste stays pending and is reconciled
read-only. A crash after a persisted attempt without a receipt is indeterminate:
the owner must investigate it, and the job does not blindly paste again. Proven
pre-input route refusals retry the same ID after the existing cooldown. Operator
cards use the same stable ID as their notification dedup key.

Context delivery attempts share the existing cadence, per-pass cap, cooldown,
and whole-pass error backoff with title/card work. Context gets the available
slots first. `--disable-nudges` disables the combined job. Tagged logs use
`subsystem=context_nudge bug_ref=context_notifications_handoff_proof` and retain
source, kind, epoch, target, outcome and tell ID.

The installed handoff verifier runs without daemon mutation:
`python3 services/chat-stream-v2/tools/context_handoff_proof.py <evidence-directory>`.
It checks delivered successor evidence, source/witness generations, inherited
Fable 5.1/high tuple, predecessor closure and witness parentage. The evidence
producer owns the three disposable seats and must verify all three are closed
and their panes absent after capture. A successful spawn reply alone is
insufficient: existing handoff post-steps are best-effort on reparent failure.

### QA dispatch containment

The optional [QA dispatch reject counter](../../docs/qa-dispatch-counter.md) adds
`spec-issue adjudicate/diagnose/show` and explicit QA surface/cycle fields for
immediate and scheduled reviews. Enforcement defaults off; the coordinated
release owner activates it after upgrading callers.

## Attachment send correlation

A send claims its logical message before materializing attachments. Retries match
the target, actor, optimistic ID, display text and canonical attachment metadata
within the existing recency window; replaying a request ID remains request-scoped.
Materialized file paths belong only to provider wire correlation. Before any paste,
the daemon appends the final wire correlation to the same receipt history so an
early provider USER event already carries the caption, attachments and optimistic
ID. The newest receipt outcome remains authoritative, including a materialized
`not_landed` result that permits retry. No prior receipt row is rewritten.

When receipt projection replaces provider text, ingress stores a
`provider_text_digest` on that same event before persistence. It hashes the
original normalized provider text and contains no staged path. Both durable USER
proof and the Comms event-proof reader use this shared digest; a caption match
alone cannot confirm an image prompt. Ingress discards producer-supplied digests,
and malformed proof metadata cannot fall back to caption equality. Existing
stream, generation, event-kind and post-watermark proof fences still apply.

### Observer start presentation and rollout recovery

The public `prockill.canonical_start` helper collapses only whitespace in printed
`ps lstart` strings. Missing, non-string and empty values are invalid; dates,
locale/timezone tokens and case are not transformed. Printed starts are not
portable process-birth tokens and a mismatch alone does not establish clock
movement or PID reuse.

Local ingestion keeps its strict generation, observer/live pane PID, UID,
provider executable and nonempty canonical-start check at entry, discovery and
pre-append. After root failure, an anchored rollout may recover either a changed
pane PID or a same-PID changed start. Recovery requires one distinct current
writable transcript in the legitimate provider tree, matching the prior
anchor's device/inode and native provider session ID parsed from an opened,
fstat-checked descriptor. A pane/process re-read fences that proof before the
exact expected-binding Store CAS; only observer PID/start can change. Ingestion
then reruns the strict check and keeps the ordinary lifecycle/native-ID checks
and durable event dedup. A recorded path or first bind cannot seed recovery.
An unchanged strict identity still tolerates an ordinary writer gap/restart.

Claude seats carry a launch-recorded `jsonl_path` and need not keep their
transcript open, so they have no writer to census. A healthy strict pass anchors
such a row once, best effort: only for the recorded path, through a descriptor
whose fstat identity is still that path's file, carrying the recorded
`claude_session_id`; a refused anchor leaves the legacy path unchanged. An
anchored Claude row recovers only a same-PID changed start: the anchor's path
and session ID must equal the row's launch record, and the path's device/inode
and native session ID must equal the anchor. A changed Claude pane PID stays
refused. Bindings refused before this anchor existed are not repaired.

Refused identities produce [historical integrity incidents](../../docs/contracts/error-alerts-v1.md#historical-ingest-identity-incidents),
not idle-timeout claims. This detector belongs to local `Ingest`; satellite
`event.push` ownership must be inspected separately when investigating a remote
session.

The start-field consumer audit covers process-record/table capture and reap
proof capture (`prockill`), remote spawn capture and cancelled-spawn cleanup
(`spawnctl`), strict/recovery ingest checks, reconciler remote table capture and
reap proof comparison, Store rebind no-op detection, and assistant-restore ps
classification. Exact JSON binding CAS snapshots and unrelated timestamps are
unchanged. Gate ownership uses Linux start ticks or its existing platform
fallback; orphan-reaper date parsing is an elapsed-age input, not observer
presentation equality. Private cache/delivery validators are maintained by
their owner outside this public source tree.

### Native Codex cwd trust and late delivery

Before a configured Codex launch, the target host seeds the realpath cwd in
`$CODEX_HOME/config.toml` (default `~/.codex/config.toml`). A file lock protects
idempotent writes; unrelated configuration bytes are preserved. The existing
command-line trust override remains. Invalid or unsupported project-table
syntax fails launch with `codex_trust_failed` rather than rewriting the config.

During native initial-prompt proof, the daemon recognizes current and legacy
folder-trust dialogs and sends at most one Enter, logs a warning, and continues
waiting for the initial USER event. It never pastes into that dialog. Reset
interstitials remain operator-only; pane capture cannot extend the proof wait.

Failed native proof logs its key and pane classification and persists a parent
notice that bypasses the front-desk digest. Classification is `trust_dialog`,
`reset_interstitial`, or `silent`. The notice survives requester disconnect and
is fenced to the parent generation. Late proof retains `failure_code` and
`delivery_failed_at`, adds `delivered_late_at`, and records
`late_first_user_event`; `adopted_after_restart` requires evidence of a different
daemon instance. Reuse the existing spawn handle for recovery.
