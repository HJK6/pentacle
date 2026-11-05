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
  --coordinated-receipt /private/receipt.json --prepare-only
# Inside the separately granted deployment window:
python services/chat-stream-v2/deploy/deploy.py chat-streamd-v2 \
  --release-checkout /absolute/runtime --coordinated-plan /private/intent.json \
  --coordinated-receipt /private/receipt.json --deployer-stream-id host:deployer
# Whole-transaction recovery, including a committed transaction or down daemon:
python services/chat-stream-v2/deploy/deploy.py chat-streamd-v2 \
  --release-checkout /absolute/runtime --rollback \
  --coordinated-receipt /private/receipt.json --deployer-stream-id host:deployer
```

Preparation stages full immutable helper source, tests the existing configured
interpreters and read-only native artifacts, validates target/prior gate evidence
and both target smoke plans, and saves private preimages. It changes no active
policy, source, launchd state, CLI launcher, pin or account generation. It needs
the owner's explicit host-staging/probe authority. Keep its directories through
handoff; there is no automatic garbage collection.

Account admission follows the existing recovery predicates, including sealed
resolution of parked repins, and checks live maintenance input slots and
unexpired spawn holds. Historical maintenance receipts do not represent live
slots. Unresolved recovery or generation intents, including attempts awaiting
enrollment, still refuse preparation and revalidation; deployment never rewrites
the account journal to clear them.

Apply rechecks sealed inputs, sole-deployer/schedule/account quiescence and
staged proofs. It unloads the old job and proves daemon/local helper exit before
switching the checkout and included executor script fields. Other policy fields
and excluded executors stay intact. It bootstraps and verifies the target,
dispatched identities, included canaries, cleanup and slow-consumer evidence.
No coordinated invocation accepts `--release-inputs` or `--skip-kickstart`.

The immutable receipt links the intent digest to preimages, staged source,
interpreter/dependency evidence, catalog/policy/pin digests and exact scope.
A separate owner-only journal lives beside the runtime checkout in
`.pentacle-coordinated-<checkout-path-hash>/<transaction-id>/journal.json`.
`active.json` binds the current receipt. Each phase is durable before its side
effects: prepared, stopping, stopped, switching, verifying, committed,
rolling_back, rolled_back, discarded or recovery_required. An unfinished journal blocks a
new forward deploy. Recover explicitly with the same receipt after interruption.
Plain `--rollback` refuses coordinated ancestry and prints that receipt path;
unchanged-identity releases retain the ancestry in their stamps.

If apply has never begun, the same receipt-based `--rollback` command discards
the preparation without stopping a daemon, restoring files, probing helpers or
running canaries. It works after input drift and preserves all current runtime
state, staged directories and receipt bytes. The journal becomes terminal
`discarded`; operation exit 0 has `outcome=discarded`, `scope=preparation_only`
and `full_fleet_acceptance=false`, and is not deployment acceptance. Prepare
again with fresh inputs, a new transaction ID and a new receipt path. Discard
is idempotent until a new preparation supersedes the active receipt; it does
not bypass any older coordinated ancestry in the live deployment stamp.

Exit **11** means committed scoped success (or successful explicit restoration)
with declared optional exclusions and `full_fleet_acceptance=false`. It requires
every included cell to pass and preserves four `SKIPPED_DECLARED` rows per
excluded host. Only a coordinated consumer that validates receipt digest,
committed phase, admitted exclusions and complete included evidence may accept
11; generic release gates continue to require 0. Scoped deployment never changes
the recurring smoke schedule or satellite admission/pins.

Exit **12** is an applied/recovered or recovery-required coordinated failure.
Automatic rollback retains the original forward failure and never converts it
to success. Inspect the journal and returned receipt, and GATE the owner when
recovery is required. A stale receipt or post-commit source/policy/plist drift
refuses before stop; prepare a new transaction rather than overwriting later
work. Explicit rollback restores original source/policy/plist/stamp bytes and
verifies prior health under the same declared canary scope.

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
