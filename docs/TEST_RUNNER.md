# Bounded root test runner (TH-H1)

The root npm test command retains its three shallow file globs. The runner
reports the original JS/TS source name for every discovered entry and executes
one file at a time. TypeScript uses the existing esbuild options; a build failure
is non-success. No existing test is removed, weakened, skipped or marked xfail.

## Options

```sh
npm test -- --file-timeout-ms 60000 --run-timeout-ms 900000 \
  --summary-path /tmp/pentacle-test-summary.json
```

Flags override environment values:

| Flag | Environment | Default |
| --- | --- | --- |
| --file-timeout-ms | PENTACLE_TEST_FILE_TIMEOUT_MS | 60000 |
| --run-timeout-ms | PENTACLE_TEST_RUN_TIMEOUT_MS | 900000 |
| --summary-path | PENTACLE_TEST_SUMMARY_PATH | no JSON file |

Time limits are positive integer milliseconds, at most2147483647. Invalid
values fail; zero never disables a gate. A file's budget includes its TS build.
The absolute scheduling deadline includes setup, builds, tests and previous
cleanup. A file timeout preserves timeout status even if TERM causes exit0.
After the whole-run deadline, every remaining discovered file is `not_run`.

The scheduling deadline is followed only by cleanup of already-owned work.
TERM has250ms grace and KILL has1000ms verification grace. These grace values
exclude the initial/final process inspection, event-loop scheduling and summary
I/O overhead; this is a bounded scheduling/cleanup policy, not a hard-real-time
wall-clock promise. Bundling is asynchronous and obeys the same deadline.

## Isolation and cleanup

One temporary HOME and USERPROFILE are shared by the suite, as before, and are
removed after success/failure. The original HOME-isolation test and probe are
unchanged. Each launched build/test owns a separate POSIX process group. Only
those exact groups receive signals, including after a group leader exits.
SIGINT/SIGTERM stop scheduling and clean the active group.

Live surviving members are reported by their actual PIDs and file, and make the
run fail. Known limit: a descendant that leaves the owned process group
(`setsid` or a detached spawn) is neither signalled nor reported, because the
runner never acts outside groups it started; such a file can show clean cleanup
while the escapee lives on. Inspection/signal errors are non-success. Dead unreaped zombie PIDs
are reported separately: they cannot execute or retain descriptors, cannot be
killed again, and are not mislabeled as live survivors. No process-name matching
or fleet-host operation is used. Linux inspects /proc; macOS uses a bounded,
injectable ps snapshot filtered to the exact owned group. Other platforms,
are not validated: Windows explicitly returns `not_run`/nonzero; other
unsupported platforms fail process inspection and therefore cannot report a
clean passing run. This implementation cannot prove group cleanup there. The repository's current required root
CI is Ubuntu/Node22; dot's local receipt uses Linux/Node24.19.0. The Windows limit
is not a test skip or a claim of Windows validation.

HOME/cache removal failures preserve completed rows and force nonzero. Output
forwarding is capped at1MiB per operation and the final output tail at64KiB and
20 lines. Timeout tails are captured before termination diagnostics can replace
the relevant last test output.

## JSON contract

The caller supplies an existing parent directory for the summary path. The
summary is atomically replaced outside cleanup-owned directories. A summary
write failure is nonzero even if tests passed.

Top-level fields include version, discovered source list, budgets, measured
`duration_ms`, `exit_code`, `runner_errors`, `directory_cleanup_errors`, and files.
Each file has `file`, status (`pass`, `fail`, `timeout`, `not_run`, `build_failed`),
`duration_ms`, reason when applicable, cleanup status and survivor PIDs. Build
and execution records include exit/signal, timeout/abort, output-tail/truncation
and cleanup details. A stderr table lists every row. A run succeeds only if all
files pass and all runner/directory cleanup obligations succeed.

## Synthetic regression corpus

`test/fixtures/root_runner/MANIFEST.json` identifies each fixture as synthetic
and pins its bytes. Controls cover pass, assertion failure, hang, TERM→exit0,
left-behind child, invalid TS, missing global WebSocket, oversized output and
the pinned144-file discovery set. The discovery proof also compares the legacy
algorithm with the new one on the current tree, without forbidding future tests.
Injected seams cover denied inspection, live survivors, late exit callbacks,
bundling ceilings, cancellation between build/execution and directory failures.

```sh
node --test test/run_tests_bounds.test.js
node scripts/run-tests.js test/run_tests_home_isolation.test.js
node --check scripts/run-tests.js
node --check scripts/lib/test-runner.js
```

## Reproduced failure and honest local limits

The unchanged runner never terminated the synthetic hanging fixture; an outer
owned-process timeout stopped it after3.011s. In a controlled missing-global-
WebSocket environment, the original microphone transport test opened two owned
listeners, threw during construction before entering its finally block, and
remained open. The new runner attributed `test/web_mic_adapter.test.js` as a
1551ms timeout with the ReferenceError. The test-only repair supplies its
already-declared ws implementation and enters cleanup before resource acquisition;
all six existing tests/assertions then pass (278ms in the focused measurement).
No product transport behavior changed.

The pristine Node24 baseline safe subset did NOT hang:141 files/1118 tests passed
in17.971s. An initial archive-only run failed the Git-metadata assertion; using
an authentic pinned checkout fixed that environment without changing the test.
Do not interpret either fact as proof that the fleet's reported full-suite hang
is resolved. Three unchanged files were not run locally under the packet's
no-live boundary:

- test/web_server.test.js: real default-server tmux operations and an unowned
  loopback daemon connection
- test/web_auth.test.js: unowned loopback daemon connection
- test/pre_push_hook.test.js: real ps fallback in the test and executed hook

They remain in default discovery and unchanged. Local gates explicitly select
safe files and disclose those three as `not_run` in the handoff receipt, not as
pytest/Node skips. The fleet owns the complete npm test run at the exact head.
The candidate safe subset contains the141 original files plus the new runner
proof. Its final counts, duration and slowest-ten table are in the PR receipt.
Serial per-file isolation adds wall time compared with the old parallel call;
it makes attribution, deadlines and cleanup deterministic.

Install the unchanged lockfile dependencies. On a fleet host use plain `npm ci`:
the root postinstall must run (on macOS it makes the node-pty spawn helper
executable; without it `test/provider_relogin.test.js` and
`test/web_server.test.js` fail). The sandbox-only variant exercised in the
authoring workspace is `npm ci --ignore-scripts --no-audit --no-fund`, followed by
`npm rebuild node-pty`; writable npm/node-gyp cache directories are environment
settings only. No dependency, pin or workflow file changes are included.
