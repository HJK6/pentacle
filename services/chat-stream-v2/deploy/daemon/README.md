# Start the daemon at login

These units start the `chat_streamd` v2 daemon when you log in and restart it if it
exits, on macOS (launchd) and Linux (systemd user unit, including WSL with systemd
enabled). They are opt-in: nothing here is installed or started unless you run the
installer.

Not covered yet:

- **Windows without systemd in WSL.** There is no Task Scheduler unit.
- **Restoring the assistant.** After a reboot the daemon is back, but a protected
  assistant whose pane died still needs the explicit recovery in
  [`docs/assistant.md`](../../../../docs/assistant.md). The daemon does not restart it.

## Before you install

- Create the daemon's virtual environment in the checkout the service will run from
  (`services/chat-stream-v2/.venv`) and install `services/chat-stream-v2/requirements.txt`
  and `services/agent-orch` into it.
- Install `tmux` and log in to your provider CLIs as the same user.
- Use a dedicated checkout, not a development worktree: the service runs whatever is
  checked out there.

## Install

Run the installer with any Python 3.11+ from the checkout. All three steps are
separate; nothing starts until `--enable`.

```sh
cd services/chat-stream-v2/deploy
python3 install_daemon_service.py --release-checkout ~/repos/pentacle --print    # look first
python3 install_daemon_service.py --release-checkout ~/repos/pentacle            # write the unit
python3 install_daemon_service.py --enable                                       # start now and at login
```

The install step writes one file and starts nothing:

| Platform | Unit file |
|---|---|
| macOS | `~/Library/LaunchAgents/com.pentacle.chat-streamd-v2.plist` |
| Linux | `~/.config/systemd/user/pentacle-chat-streamd-v2.service` |

If a unit file is already there with different content, the installer stops and
changes nothing. Pass `--replace` to overwrite it; the old file is kept for
`--rollback`.

Options:

| Option | Default | Meaning |
|---|---|---|
| `--release-checkout` | required | Checkout the daemon runs from. |
| `--python` | `<checkout>/services/chat-stream-v2/.venv/bin/python` | Daemon interpreter. |
| `--tmux-bin` | `tmux` on your `PATH` | Its directory leads the unit's `PATH`. |
| `--state-dir` | `~/.local/share/pentacle-stream` | Session, notification and asset stores and blobs. |
| `--spawn-cwd` | your home directory | Directory new agent sessions start in. |
| `--port` | `7791` | Loopback port the daemon listens on. |
| `--local-host` | the machine's hostname | This machine's name in your machines file. |

Paths containing spaces, quotes, backslashes, `%`, `$` or `;` are refused, because the
two unit formats quote them differently.

Both units run the same command, on `127.0.0.1` only. To listen on another interface
or pass other daemon options, edit the installed unit; `--print` shows the starting
point and `main.py --help` lists the options.

### Linux: start at boot, and settings

```sh
loginctl enable-linger "$USER"                                    # start without an interactive login
systemd-analyze --user verify ~/.config/systemd/user/pentacle-chat-streamd-v2.service
```

Put `PENTACLE_*` settings, one `KEY=VALUE` per line, in `~/.config/pentacle/daemon.env`.
The unit reads the file if it exists. On macOS, add them to the plist's
`EnvironmentVariables` instead.

## Check that it is running

```sh
agent-orch list          # exits 0 and prints a JSON list (empty on a new installation)
```

If your CLI is not already configured for this daemon, set
`AGENT_ORCH_WS_URL=ws://127.0.0.1:7791` first.

Logs:

| Platform | Where |
|---|---|
| macOS | `~/Library/Logs/pentacle/chat-streamd-v2/daemon.out.log` and `daemon.err.log` |
| Linux | `journalctl --user -u pentacle-chat-streamd-v2 -f` |

Service state: `launchctl print gui/$(id -u)/com.pentacle.chat-streamd-v2` on macOS,
`systemctl --user status pentacle-chat-streamd-v2` on Linux.

## Remove it

Stop the service first, then roll back:

```sh
launchctl bootout gui/$(id -u)/com.pentacle.chat-streamd-v2      # macOS
systemctl --user disable --now pentacle-chat-streamd-v2.service  # Linux
python3 install_daemon_service.py --rollback
```

If a watchdog outside Pentacle restarts the daemon when it finds it down, mark any
intentional stop outside `deploy.py` as maintenance first, using that watchdog's own
runbook. This covers the `bootout` before `--rollback` and any manual
`launchctl bootout`. Otherwise the watchdog starts the daemon again, and
`--rollback` refuses because the service is no longer stopped. Remove the marker, or let
it expire, when the stop is over. `deploy.py` needs no marker: the watchdog takes the
same deploy lock.

Rollback restores what was at the unit path before the first install: the earlier
file, or nothing. It does not stop the service. It changes a file only when the
service manager explicitly reports the service stopped (launchd no longer knows the
label; systemd reports the unit `inactive` or `failed`). If the service is running,
or its state cannot be read, rollback exits with an error, prints the stop command,
and changes nothing.

## Using it with `deploy.py` (macOS)

[`deploy.py`](../deploy.py) restarts a LaunchAgent with this label and refuses to run
when none is loaded, so this installer is the first-time setup that `deploy.py` builds
on. The installed plist meets its checks when you keep the default `--python`: the
interpreter is inside the release checkout, and the log directory exists. A later
`deploy.py` run adds its own environment keys to the plist; after that, re-running
this installer needs `--replace`.


Imported `deploy.py` command helpers require an explicit `runner`, including
`deploy`, environment edits, usage-collector install/rollback and restart helpers.
Omitting it raises before helper-body file or native effects. The CLI entrypoint
passes its native runner explicitly; direct library callers must do the same only
inside their authorized live path. Collector bootstrap failures propagate instead
of being reported as a successful install.

### Read-timing receipt (optional manual check)

`--read-timing-receipt` is an optional manual check, not a forward-deploy
requirement (operator decision 2026-11-11). A forward daemon deploy, plain or
coordinated, proceeds without one; when a receipt **is** passed it is still
validated for the exact target SHA, so the tool stays usable for a spot check
before a risky read change. The read-regression class it watched for is now
covered live by the per-call store-call lines, which name every slow call within
the first hour after a release, for all calls rather than three, with rollback
ungated.

When you choose to run it, it times the candidate's main reads on copies of the
live database:

```sh
python services/chat-stream-v2/tools/read_timing.py \
  --candidate /absolute/clean-candidate-checkout --out /private/read-timing.json
```

Run it with the daemon's provisioned interpreter on a clean checkout of the
candidate. It backs up `sessions.db` and the archive it is bound to through
SQLite's backup API from read-only sources into one temporary directory, rebinds
the copy to the copied archive, then starts the candidate's own `Store` on the
copies. Before the candidate is imported, the `sqlite3` module's `Connection`,
`Cursor` and `connect` are replaced with guarded versions, so every candidate
connection, factory subclass and cursor checks its path and `ATTACH` target before
SQLite opens it; replacing the authorizer or enabling extensions is refused, and
any refusal makes the run NOT_EVALUATED. This stops accidents in ordinary
repository code; deliberate bypass (`_sqlite3`, `ctypes`, raw file I/O, another
process) is out of scope. Budget version 2 calls each read once untimed to warm the caches of the
fresh copy, then three times timed, and compares the median store execution with
the budget. It certifies warmed median execution only: not a cold first read, a
read after cache invalidation, or tail latency. A failed or short invocation,
warm-up included, makes that read NOT_EVALUATED. The reads are
`list_open_sessions_with_event_summary` (3.0 s), the newest 200-event
`fetch_session_event_page` of the open session with the most events, called as the
daemon calls it for an open chat, without a generation (1.0 s, and
the page must hold at least 10 user or tell events), and
`list_open_usage_fence_rows` for every host with an open session (0.1 s each).
It exits 0 for PASS, 1 for FAIL (a breach, or the 15-minute deadline) and 2 for
NOT_EVALUATED (anything that kept a read from running). Both copies are always
removed; the copies need free disk of both files plus 20%, and the backup holds a
read transaction on the live database while it runs.

`deploy.py` no longer refuses a forward deploy for a missing receipt. When a
receipt **is** supplied it is still validated and refuses, before any service is
stopped, if it is for another SHA or budget version (version 1 single-sample
receipts included), from a dirty source, not passing, or has an absent, failing,
unevaluated, repeated or contradictory read; it then requires a warm-up and
exactly three finite non-negative samples with full output per read and per host,
and recomputes the median itself. To re-check after a refusal, rerun the command,
or change a budget by editing the spec and the command together. Rollback and
coordinated recovery never need a receipt.

### Coordinated helper and daemon deployment

A forward release that changes `provider_cli_artifact.code_identity()` requires
an admitted coordinated transaction. The ordinary deployer refuses it before
changing the active checkout. Identity equality remains enforced by the account
executor. Run the tooling from a separate clean, pinned controller checkout;
the daemon target can be an older exact origin-backed commit.

Create an owner-only (0600), nonsymlinked intent file in a private (0700)
directory, using absolute canonical paths. The schema is:

```json
{
  "schema": "pentacle.coordinated-deploy.intent.v1",
  "transaction_id": "release-example-01",
  "controller_sha": "<40 lowercase hex characters>",
  "target_sha": "<exact daemon target SHA>",
  "prior_sha": "<current daemon checkout SHA>",
  "machines_file": "/private/config/machines.json",
  "policy_file": "/private/config/provider-account-policy.json",
  "gate_evidence": "/private/evidence/target-merge.json",
  "prior_gate_evidence": "/private/evidence/prior-merge.json",
  "excluded_hosts": []
}
```

Only a configured optional remote host can be excluded. Each declaration is
`{"host":"optional-peer","reason":"tracked readiness defect","authority":"deployment-owner grant reference"}`.
Local and required hosts, unknown hosts, duplicates and empty reasons/authority
are refused. Installed catalog/transport data defines the host population.

From the pinned controller root, use its provisioned interpreter:

```sh
python services/chat-stream-v2/deploy/deploy.py chat-streamd-v2 \
  --release-checkout /absolute/runtime --coordinated-plan /private/intent.json \
  --coordinated-receipt /private/receipt.json --prepare-only \
  --read-timing-receipt /private/read-timing.json
# Inside the separately granted deployment window:
python services/chat-stream-v2/deploy/deploy.py chat-streamd-v2 \
  --release-checkout /absolute/runtime --coordinated-plan /private/intent.json \
  --coordinated-receipt /private/receipt.json --deployer-stream-id host:deployer \
  --read-timing-receipt /private/read-timing.json
# Whole-transaction recovery, including a committed transaction or down daemon:
python services/chat-stream-v2/deploy/deploy.py chat-streamd-v2 \
  --release-checkout /absolute/runtime --rollback \
  --coordinated-receipt /private/receipt.json --deployer-stream-id host:deployer
```

Preparation stages full immutable helper source, tests the existing configured
interpreters and read-only native artifacts, validates target/prior gate evidence
and the sealed forward smoke plan, binds the prior's production forward
qualification, and saves private preimages. No prior runner/profile API is required. It changes no active
policy, source, launchd state, CLI launcher, pin or account generation. It needs
the owner's explicit host-staging/probe authority. Keep its directories through
handoff; there is no automatic garbage collection.

Account admission follows the existing recovery predicates, including sealed
resolution of parked repins, and checks live maintenance input slots and
unexpired spawn holds. Historical maintenance receipts do not represent live
slots. Unresolved recovery or generation intents, including attempts awaiting
enrollment, still refuse preparation and revalidation; deployment never rewrites
the account journal to clear them.

Helper child failures report argv0, exit status or timeout, and redacted stdout/stderr tails (at most 1000 characters each). Remaining argv and environment are omitted. Pre-stop apply refusals retain this diagnostic in `helper_proof_failure` in the prepared journal; post-stop failures retain it in the existing recovery failure record. A journal write error preserves the original refusal. Only an artifact-proof child exiting 1 with the exact JSON outcome `account_busy` waits. One shared 15-second deadline starts at the first busy outcome in a controller invocation, with 250 ms backoff; later proofs cannot reset it. The deadline exceeds three times the measured observation hold maximum and is below the 60-second cap. Retry child execution is bounded by the remaining deadline. Each wait records attempt count, elapsed seconds and last outcome in the journal (preparation records them when its journal is created), and exhausted waits retain that detail in the refusal. Every other exit/outcome refuses immediately. Locks, observation cadence and admission guards stay unchanged.

Apply rechecks sealed inputs, sole-deployer/schedule/account quiescence and
staged proofs. It unloads the old job and proves daemon/local helper exit before
switching the checkout and included executor script fields. Other policy fields
and excluded executors stay intact. It bootstraps and verifies the target,
the boot line and exact served SHA plus dispatched helper identity. Only those
serving checks decide restoration. Smoke, slow-consumer logs and the authenticated
Front Desk reply run after commit, as reports; a failure leaves the serving target
active and emits one aggregate BLOCKER.
No coordinated invocation accepts `--skip-kickstart`.

The immutable receipt links the intent digest to preimages, staged source,
interpreter/dependency evidence, catalog/policy/pin digests and exact scope.
A separate owner-only journal lives beside the runtime checkout in
`.pentacle-coordinated-<checkout-path-hash>/<transaction-id>/journal.json`.
`active.json` binds the current receipt. Each phase is durable before its side
effects: prepared, stopping, stopped, switching, verifying, committed,
rolling_back, rolled_back, discarded or recovery_required. An unfinished journal blocks a
new forward deploy. Recover explicitly with the same receipt after interruption.
Plain `--rollback` is allowed with coordinated ancestry when current and prior
helper identity are equal. Identity-changing recovery still requires the receipt;
unfinished transactions remain protected. Unchanged-identity releases retain ancestry.

If apply has never begun, the same receipt-based `--rollback` command discards
the preparation without stopping a daemon, restoring files, probing helpers or
running canaries. It works after input drift and preserves all current runtime
state, staged directories and receipt bytes. The journal becomes terminal
`discarded`; operation exit 0 has `outcome=discarded`, `scope=preparation_only`
and `full_fleet_acceptance=false`, and is not deployment acceptance. Prepare
again with fresh inputs, a new transaction ID and a new receipt path. Discard
is idempotent until a new preparation supersedes the active receipt; it does
not bypass any older coordinated ancestry in the live deployment stamp.

The public disposition, rather than individual report classifiers, determines exit:

| Disposition | Exit |
|---|---|
| active and checks passed | 0 |
| refused before change | 2 |
| active with failed checks | 3 |
| target not active, prior restored | 4 |
| target not active, restore unproven | 5 |

Ordinary deployment fetches the exact target and verifies exact gate evidence
before source mutation. It retains prior source and stamp/plist/policy bytes,
modes and absence. When target boot or runtime SHA is not proved, automatic
restoration first unloads the job and proves the process and owned children exited.
It restores the entire prior source and saved files before bootstrap. An incomplete
restore or unknown exit never starts the prior. A complete restore whose subsequent
boot/SHA readback is unproven remains loaded and reports restore unproven.
The restored prior stamp is not rewritten with the failed attempt report.

Coordinated apply retains sealed scope, preimages, authority, account/quiescence
checks and helper identity proof. After serving success, canary/log/FD failures do
not trigger recovery or stop that target. Restoration proofs, cleanup and dispatch
reports after prior restoration do not veto a complete prior boot attempt or stop
a fully restored prior. Restoration uses no provider prompts. Excluded scope remains
explicit and does not imply fleet acceptance. Receipt preparation stays inert.

The recurring deploy-time smoke installer is retired. Nightly checks report against
the already deployed stamp and never deploy or select a target. The detailed smoke
and log classifier reports remain available alongside the public disposition.

Ordinary releases and nightly checks share the optional `release_exclusions`
array in the installed machines JSON (`PENTACLE_MACHINES_FILE`). Each entry names
an optional remote `host`, a nonempty `reason`, and an open `readiness_item`.
For example: `{"host":"optional-host","reason":"CLI pin mismatch",
"readiness_item":"provider-readiness-repair"}`. The front desk removes the entry
when its readiness item closes. Declared hosts are not spawned and appear as
`SKIPPED_DECLARED`; all included hosts must pass. Releases snapshot declarations
on their stamp. Nightly reads the same home afresh and records its scope without
rewriting the deployed stamp. Invocation environment overrides do not alter scope.

A sealed receipt pins its controller SHA. To recover it from a repaired
controller, the FD places an owner-only authorization file in the transaction
directory and sends a GO tell quoting the file's sha256 and the replacement SHA:

```json
{"transaction_id": "...", "receipt_sha256": "...",
 "original_controller_sha": "<receipt intent.controller_sha>",
 "replacement_controller_sha": "<clean controller HEAD>", "operation": "recovery",
 "expected_prior_sha": "...", "expected_target_sha": "...",
 "expected_live": {"sha": "...", "pid": 123, "birth": "<ps lstart>"},
 "authorized_by": "host:fd-stream", "fd_gate_reference": "..."}
```

```bash
.venv/bin/python deploy/deploy.py chat-streamd-v2 \
  --release-checkout /absolute/runtime --rollback \
  --coordinated-receipt /private/receipt.json \
  --recovery-controller-authorization /private/<transaction>/recovery-authorization.json \
  --recovery-go-tell-id <GO tell id> --deployer-stream-id host:deployer
```

It is accepted only with `--rollback --coordinated-receipt`. While it is in
force the original controller no longer qualifies. Before admission, recovery
requires the controller to be clean at exactly the replacement SHA with the
receipt's origin, the live launchd PID, process start and checkout SHA to equal
`expected_live`, and the replacement installer to reproduce every sealed prior
`original_helpers` proof (`original helper proof drift before stop`). All
existing receipt, preimage, catalog, account, gate and drift checks still apply,
and forward apply stays pinned to the receipt controller. The journal records
the authorization digest, replacement SHA, `authorized_by`, `fd_gate_reference`
and the GO tell id.

Remote-creation witness rollout requires a qualified compatible reader before
any writer-enabled release. Seal that exact live reader as the writer release's
prior and record the actual deploy-controller checkout/SHA separately from the
smoke-runner hash. After witness production, the release owner must keep every
selected target/prior compatible, including a new coordinated forward
preparation; source does not enforce a minimum reader version. Follow the
[cleanup compatibility contract](../../docs/remote-creation-cleanup.md) for
receipt staleness, indefinite unknown obligations and further downgrade limits.

The rollback result reports `restoration` acceptance with `model_canary` not run
by design. Even a full-scope restoration sets `full_fleet_acceptance=false`:
inference authentication/quota/availability, replies and response-driven pressure
were not retested. Forward verification retains its cheap sealed full matrix.
The daemon transaction restores source, private policy/original helper bindings,
plist/environment, collector/stamp/settings and applicable schedule preimages.
Installed CLI launcher pointers, native binaries and provider pins remain outside
this transaction and must stay unchanged; persistent state must remain readable
by the prior. CLI operations remain separately owned.

New preparations write receipt schema v2. Older sealed receipts are recovered
using their pinned original controller and policy, without rewriting seals or
silently interpreting an old receipt under the new verification contract.

The reusable offline rehearsal is `PYTHONPATH=services/chat-stream-v2:services
python -m deploy.coordinated_harness --root /absolute/empty/owned/scratch`.
Its typed bindings reject production labels, endpoints and external resource
paths before starting. It exercises real disposable Server/Store/helper
processes, controlled post-switch failure, automatic recovery and explicit
committed rollback with invented account data and mocked native responses.
It proves transaction ordering and cleanup; actual fleet readiness still needs
the separately granted deployment window.

Proof harnesses must pass a no-native runner to every helper and redirect every
written descriptor to owned scratch paths. Block subprocess entrypoints before
importing the module as a second containment boundary. Scratch plist paths alone
do not isolate `launchctl` service labels. Never invoke the CLI entrypoint as an
offline proof, and never use `--skip-kickstart` as a substitute for containment: it
still permits checkout and stamp changes.

### Deploy smoke process attribution

The daemon binds plain and optional TLS listeners in a paused state, starts
every successfully bound listener, then synchronously writes the
`chat_streamd_v2 listening on` record to both managed streams with identical
PID, canonical process start and boot UUID. The structured
identity precedes the original listening text, keeping its terminal `host:port`
for existing port readers. The announced port accepts immediate connections;
both records flush without an intervening await before client handlers run.
An optional TLS start failure leaves the plain listener available.

The deploy scanner requires both matching markers, a stable activated PID/start,
and a distinct outgoing process whose recorded PID/start has exited. The one
exception is a down-daemon rollback whose guard and activation launchd samples
were both answered with no PID (`down_basis: launchd_answered_no_pid`): there is
no outgoing writer, so attribution records `no_outgoing_writer` and the exit
follows the slow-consumer scan and fleet-smoke verdicts instead of being forced
to 8; `full-window.py` still rejects that outcome. The scanner reads only bytes
after each matching marker. `old_generation.inputs` retains the exact
pre-marker ranges: the outgoing tail plus incoming startup logs. Its classifier
is separate evidence, not a verdict on the activated process. Existing 1011,
4000, overflow and peer-liveness failure rules are unchanged.

Missing, conflicting, duplicated, truncated, expired or unproven identity emits
`attribution.outcome=attribution_ambiguous`, keeps the gate failed (exit 8), and
retains input range pointers. A timestamp, connection age or cancellation reason
cannot recover a pass. A process-start probe failure is bounded and non-fatal to
startup; its incomplete marker still fails attribution.

`deploy/packet/full-window.py` uses the same paired-marker byte selector. Supply
its final optional `GENERATION_PROOF` argument with the deploy stamp (or its
`slow_consumer` object); use `-` for an absent watchdog proof. It requires the
attributed writer-lifetime receipt and the same exact marker boundaries. Offset
rows may include `backups` with `device` to traverse retained managed generations.
A missing or mismatched receipt fails as `attribution_ambiguous`. Retained
pre-marker failures stay in the packet receipt and do not clear or condemn the
new generation. Runtime rollback remains an explicit deployment-owner action.
