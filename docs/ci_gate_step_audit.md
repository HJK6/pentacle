# Supported CI entrypoint audit

The public workflow and `tools/merge_gate.py` workflow digest/required-step set
change atomically. Promotion still requires the exact workflow bytes and one
completed successful job with exactly one successful result for every required
step. Missing, skipped, failed or duplicate steps remain refusals.

| Previous step | Supported replacement and preserved coverage |
| --- | --- |
| npm ci / npm run prestart / npm test | `Supported npm tests`: `gate_env.py run --target npm-test`; same lockfile, prestart build and three default runner globs. |
| Python service requirements + editable CLI install | Per-target owned Python 3.13 venv from the same requirements and editable declaration; no ambient pip install. |
| Web mode E2E gate | `--target web-gate`: existing prestart/build:web, real `web_gate.js` (including ancillary browser journeys), then `node --test test/e2e/web_gate_interrupt.test.js`. |
| Daemon merge | `--target v2-merge-ci`: same unit then smoke; CI unit 1200s and smoke 900s preserved as explicit profile constants. Ordinary local targets keep their existing bounds. No tier/host repair is bundled. |
| CLI tests | `--target cli-pytest --select tests`: complete existing `services/agent-orch/tests`, prepared interpreter, existing PYTHONPATH. |
| Shared service tests | `--target shared-pytest --select tests`: complete existing `services/_shared/tests`, prepared interpreter, existing PYTHONPATH. |
| Failed daemon evidence | Whole `${runner.temp}/gate-evidence/**`, including started/setup-failure events, manifests, preflight identities, runner logs/JUnit and browser reports. |

System packages, Chrome installation, both source-integrity checks, public
residue checks, portable microphone contracts and vendor dashboard checks keep
their commands and order. The mic/vendor suites retain their distinct dependency
domains. The required `Run npm test` step becomes `Supported npm tests`; other
required coverage names are preserved.

Each root invocation has a run-stable logical series (GitHub run ID plus target),
separate evidence directory and purpose `acceptance`. GitHub reruns keep that
series and run-attempt artifact identity. Missing previous manifests remain
unknown history; they are never advertised as clean first attempts. Short /tmp
scratch and separate owned HOME prevent pytest cleanup from deleting evidence.
The supported launcher preflights the actual child interpreter, dependencies,
process/socket capabilities and installed UTF-8 locale. Preparation/build failure
stops before the subject and retains its receipt. No ambient credentials or
pytest flags enter the child environment.

For v2 CI the finite envelope preserves the two original tier budgets, plus the
existing preflight/cleanup/startup reserves. The workflow's existing 40 minute
job bound remains unchanged. Browser and npm subjects retain their existing
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
fail. Rollback restores the workflow, promotion digest/step audit and profile
from the same preimage together; retained attempt evidence is never deleted.
