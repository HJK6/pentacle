# Supported CI entrypoint audit

The public workflow uses four independent jobs and the required `checks`
aggregate. `frontend` runs npm/residue/dashboard checks, `web` runs browser
checks, `daemon` runs the v2 merge target, and `contracts` runs microphone,
CLI and shared-service checks. All retain their original selections.

`checks` runs with `always()` and invokes the stdlib
`scripts/ci_required_aggregate.py` predicate. It accepts only the exact expected
job set with every result `success`; failure, error, skipped, cancelled, missing
or invalid results fail. The focused controls bind the expected IDs to workflow
`needs`, exercise every partition failure and check trigger/command preservation.
Required `checks`/app `15368` stays unchanged. Feature-branch updates run through
`pull_request`; pushes to `main` still validate main.

The legacy single-job workflow digest and promotion-step consumer were retired
with `tools/merge_gate.py promote`. The [protected PR route](repo_workflow.md#landing-on-main)
binds the reviewed head, hosted checkout/source, complete required coverage,
expected-head merge and separate merged-SHA/main readbacks.

| Previous step | Supported replacement and preserved coverage |
| --- | --- |
| npm ci / npm run prestart / npm test | `Supported npm tests`: `gate_env.py run --target npm-test`; same lockfile, prestart build and three default runner globs. |
| Python service requirements + editable CLI install | Per-target owned Python 3.13 venv from the same requirements and editable declaration; no ambient pip install. |
| Web mode E2E gate | `--target web-gate`: existing prestart/build:web, real `web_gate.js` (including ancillary browser journeys), then `node --test test/e2e/web_gate_interrupt.test.js`. |
| Daemon merge | `--target v2-merge-ci`: same unit then smoke; CI unit 1500s and smoke 900s are explicit profile constants. Ordinary local targets keep their existing bounds. Hosted-runner unit headroom was increased after the same 9521-case suite took 1056.5s and 1196.4s; the latter completed its journal but reached the old 1200s process deadline before exit. |
| CLI tests | `--target cli-pytest --select tests`: complete existing `services/agent-orch/tests`, prepared interpreter, existing PYTHONPATH. |
| Shared service tests | `--target shared-pytest --select tests`: complete existing `services/_shared/tests`, prepared interpreter, existing PYTHONPATH. |
| Failed evidence | Each job owns `${runner.temp}/gate-evidence/${github.job}/**`: started/setup-failure events, manifests, preflight identities, runner logs/JUnit and browser reports. Scratch, credential HOME and prepared environments remain outside uploads. |

System packages, Chrome installation, public residue, portable microphone and
vendor dashboard checks keep their existing commands. Each job has its own
checkout, workspace and manifest. Source-integrity checks follow mutating setup
and run at job end; web also retains the post-Chrome check. Job-specific artifact
names include job ID, run ID and attempt. Small existing manifest/index/event
summaries upload with `always()`; failures also retain detailed evidence. The
microphone and vendor suites keep their distinct dependency domains.

Each root invocation has a run-stable logical series (GitHub run ID plus target),
separate evidence directory and purpose `acceptance`. GitHub reruns keep that
series and run-attempt artifact identity. Missing previous manifests remain
unknown history; they are never advertised as clean first attempts. Short /tmp
scratch and separate owned HOME prevent pytest cleanup from deleting evidence.
The supported launcher preflights the actual child interpreter, dependencies,
process/socket capabilities and installed UTF-8 locale. Preparation/build failure
stops before the subject and retains its receipt. No ambient credentials or
pytest flags enter the child environment.

For v2 CI the finite envelope is 2400s for unit plus smoke, with the existing
120s preflight/cleanup/startup reserves (2520s total). The reviewed workflow job
bound is 50 minutes. This is hosted CI headroom; local tier bounds stay unchanged. Browser and npm subjects retain their existing
runner behavior and own teardown; direct pytest subjects use the existing owner
manifest. CLI/shared service pytest has no daemon-specific capability plugin.

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

All five daemon targets declare Node 22 alongside Python and tmux in `scripts/gate-env.json`. The launcher resolves and preflights Node before preparation or subject launch, then includes its resolved directory in the closed child PATH. This covers installations where tmux and Node occupy separate directories, including hosted CI toolcaches. Missing or unusable Node produces the `missing_node` setup diagnostic; ambient PATH is never copied into the subject.
