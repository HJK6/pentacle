# Post-boot fleet smoke verdicts (TH-H5)

A post-activation verdict describes a deployment that already restarted the daemon.
It is never an invitation to repeat or force the deployment. The smoke is run once;
there is no automatic deployment retry.

## Caller contract

- Exit **0**, `passed`: every configured host/provider/prompt-mode cell passed,
  the smoke command succeeded, and there is no contradictory evidence. Only this
  smoke outcome installs the recurring smoke schedule. Other post-activation
  checks, including schedule installation, must still succeed for deploy exit 0.
- Exit **6**, `failed`: any cell failed, including a remote cell or a failure
  reported only in the smoke payload's `failures` array. Failure wins over a
  duplicate pass, an unavailable row, or a contradictory command exit 0. A smoke
  command failure other than its documented UNTESTED exit 2 also fails.
- Exit **9**, `untested`: local evidence is incomplete/unavailable, any cell is
  quota-limited or otherwise untested, or configuration/evidence is missing or
  invalid. This retains the existing quota/UNTESTED non-success contract.
- Exit **10**, `partial`: every local cell passed and one or more remote cells
  are explicitly unreachable; all other cells passed. The operator line names
  the unreachable satellites. This is **non-success** and cannot satisfy a
  full-fleet release gate. Callers must require exit 0, not accept exit 10.

The existing precedence of boot/runtime/post-activation/slow-consumer checks is
unchanged. Their failure can take precedence over the smoke exit above, while the
printed stamp still retains the smoke evidence.

## Evidence and identity

The JSON stamp and stdout include every normalized `fleet_smoke.cells` outcome
and reason, all merged `reasons`, `local_hosts`, `unreachable_hosts`, and any
configuration/evidence `issues`. `fleet_smoke.evidence` preserves the complete
parsed original smoke payload, including full failure details, metrics, quota
reset times, and duplicate observations. Unparseable/empty payloads retain the raw
stdout/stderr. The abbreviated terminal `detail` is not the evidence archive.

Evidence from `cells`, `untested`, legacy `quota_exhausted`, and `failures` is
merged by `(host, provider, prompt_mode)`. Failure dominates; quota/general
untested dominates unreachable; incomplete evidence never becomes passed.
Expected cells missing from all channels appear explicitly as
`untested` / `missing_evidence`.

Local identity and the expected host set come from the installed launchd
`PENTACLE_MACHINES_FILE`, using the existing `ssh_target: null` local marker.
Ambient host identity and inline machine overrides cannot establish local health.
Both a machines list and an object containing `machines` are supported. A relative
installed path is resolved once against the release checkout (the smoke child
working directory), and that exact absolute path is used for both execution and
verdict. An empty setting remains invalid rather than resolving to a directory.

## Assumptions and conservative defaults

- The full matrix is the producer's current `claude`/`codex` ×
  `prompted`/`promptless` matrix for every installed host. Update this contract
  with the producer if that matrix changes. An excluded configured cell is still
  missing full-fleet acceptance evidence and does not prove a pass.
- If several hosts have the local marker, all of them must pass. If none does,
  the local identity is unproven and neither full nor partial acceptance is given.
- Only explicit `host_unavailable`/`unreachable` evidence qualifies for partial.
  Unknown causes, omitted cells, and quota exhaustion retain exit 9.
- Smoke exit 2 is the producer's UNTESTED code; other nonzero smoke exits fail.
  Malformed/unconfigured rows remain preserved and cannot establish acceptance.

## Synthetic validation and fixture manifest

All new host identities, `.example.com` targets, SHA values, times, paths, payloads,
and errors below are synthetic. No real SSH, daemon, socket, tmux, GitHub, or fleet
operation is exercised.

- `tests/test_deploy_fleet_verdict.py`: 31 synthetic cases drive the existing fake
  runner through post-activation classification and the real deploy caller. They
  check single kickstart/smoke invocation, stamp/stdout retention, failure
  precedence, remote partial, local incompleteness, quotas, missing/invalid cells,
  installed identity, command status, and full-pass-only schedule installation.
- `tests/test_deploy_script.py`: all original assertions remain unchanged,
  including every exit-9 assertion. Three stamp/schedule tests previously supplied
  empty successful stdout; their shared synthetic fixture now supplies an installed
  machine file and four explicit passed cells, so they still reach the intended
  stamp/schedule boundary without treating absent evidence as a smoke pass.
- The existing installed-machine-file test still executes only the real
  configuration planner. Its fake runner now translates the verified plan to
  explicit synthetic passed execution cells; the original planner-output and
  exit-code assertions remain unchanged. A bare execution plan is not smoke
  execution evidence. All four configured parameter cases keep passing.

Initial RED run before the deploy change: **14 failed, 2 passed in 0.23s**.
Examples: the partial assertion observed `untested` instead of `partial`; both
local and remote explicit failure rows with command exit 0 returned **0 instead
of 6**; missing/invalid receipts installed the schedule; stamps omitted `cells`.
Additional boundary coverage was added after that initial reproduction.

Focused command (use the prepared gate interpreter as `python`):

```sh
python -m pytest services/chat-stream-v2/tests/test_deploy_fleet_verdict.py services/chat-stream-v2/tests/test_deploy_script.py -q
python -m compileall -q services/chat-stream-v2/deploy/deploy.py services/chat-stream-v2/tests/test_deploy_fleet_verdict.py services/chat-stream-v2/tests/test_deploy_script.py
```

The focused run passed **61 tests** (31 new, 30 existing), with no skips.
The relative-path regression first reproduced the wrong local identity from a
conflicting caller-directory file (**1 failed, 30 deselected in 0.11s**); it now
verifies the release-relative identity and exact path passed to the child.
Full fleet/full-capability activation remains the fleet's responsibility; these
checks verify only synthetic orchestration and verdict behavior.
