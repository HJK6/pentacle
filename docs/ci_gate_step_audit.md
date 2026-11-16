# Supported CI entrypoint audit

The public workflow keeps `frontend`, `web` and `contracts` checks independent.
`daemon-unit` runs six serial jobs partitioned by the SHA256 of each
root-relative test filename modulo six. `daemon` runs smoke once after the
unit matrix passes. `checks` requires every named job to succeed. Feature
branches use pull requests; pushes to `main` still validate main.

The legacy single-job workflow digest and promotion-step consumer were retired
with `tools/merge_gate.py promote`. The [protected PR route](repo_workflow.md#landing-on-main)
binds the reviewed head, hosted checkout/source, complete required coverage,
expected-head merge and separate merged-SHA/main readbacks.

| Previous step | Supported replacement and preserved coverage |
| --- | --- |
| npm ci / npm run prestart / npm test | `Supported npm tests`: `gate_env.py run --target npm-test`; same lockfile, prestart build and three default runner globs. |
| Python service requirements + editable CLI install | Per-target owned Python 3.13 venv from the same requirements and editable declaration; no ambient pip install. |
| Web mode E2E gate | `--target web-gate`: existing prestart/build:web, real `web_gate.js` (including ancillary browser journeys), then `node --test test/e2e/web_gate_interrupt.test.js`. |
| Daemon unit | Six `v2-unit-ci-shard` partitions keep whole files together. |
| Daemon smoke | `v2-smoke` runs once after all unit partitions pass. |
| CLI tests | `--target cli-pytest --select tests`: complete existing `services/agent-orch/tests`, prepared interpreter, existing PYTHONPATH. |
| Shared service tests | `--target shared-pytest --select tests`: complete existing `services/_shared/tests`, prepared interpreter, existing PYTHONPATH. |
| Failed evidence | Each job owns `${runner.temp}/gate-evidence/${github.job}/**`: started/setup-failure events, manifests, preflight identities, runner logs/JUnit and browser reports. Scratch, credential HOME and prepared environments remain outside uploads. |

Each job installs only missing system tools it needs. The runner image supplies
Chrome. Source-integrity checks follow setup and run at job end. Artifact names
include job, run and attempt; unit partitions also include shard index.

Each root invocation has a run-stable logical series (GitHub run ID plus target),
separate evidence directory and purpose `acceptance`. GitHub reruns keep that
series and run-attempt artifact identity. Missing previous manifests remain
unknown history; they are never advertised as clean first attempts. Short /tmp
scratch and separate owned HOME prevent pytest cleanup from deleting evidence.
The supported launcher preflights the actual child interpreter, dependencies,
process/socket capabilities and installed UTF-8 locale. Preparation/build failure
stops before the subject and retains its receipt. No ambient credentials or
pytest flags enter the child environment.

Chrome flags have one JS home, `test/e2e/lib/chrome_launch.cjs`; caller debugging,
profile, sandbox and window flags stay at each launcher. Playwright supplies its
headless switch while receiving the shared keychain flags. The Python Selenium
launcher is an explicit separate-runtime exception, tested through Python AST:
its executable option loop retains `--headless=new` and `--use-mock-keychain`.
Comment-only flags cannot satisfy either audit. The JS audit inventories launches,
rejects raw legacy/current headless flags outside the helper and verifies actual
helper use. Mutation fixtures prove missing helper use and comment-only claims
fail. Rollback uses the scoped source revert through the release relay; unchanged
profiles retain their existing preimages. Retained attempt evidence is never deleted.

Daemon gate targets declare Node 22 alongside Python and tmux in `scripts/gate-env.json`. The launcher resolves and preflights Node before preparation or subject launch, then includes its resolved directory in the closed child PATH. This covers installations where tmux and Node occupy separate directories, including hosted CI toolcaches. Missing or unusable Node produces the `missing_node` setup diagnostic; ambient PATH is never copied into the subject.
