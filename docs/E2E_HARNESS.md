# Public end-to-end validation harness

The public harness is provider-free and uses a disposable local daemon. It validates the shipped websocket and renderer contracts with synthetic sessions; it does not connect to a production service or collect live user content.

## Unit and protocol checks

Run the repository's normal unit gate first:

```bash
python3 services/chat-stream-v2/tools/run_gate.py unit
```

For a websocket smoke check, start a daemon on an ephemeral loopback port with temporary session and notification stores, then exercise `welcome`, `hello`, `list_sessions`, `spawn`, `send`, and `close`. The test should use a fake provider executable that writes deterministic output to a disposable tmux session.

## Renderer walk

The renderer walk may use a browser automation client or a DOM test. Seed one synthetic session, wait for the sidebar row, send a fixed prompt, and assert both the state update and the rendered transcript row. A telemetry counter alone is not render evidence.

Every walk should:

- use a fresh temporary directory;
- use loopback URLs and generated request ids;
- record compact verdict JSON beside the test run;
- clear the directory after the run; and
- fail if a required DOM element is absent or contains unexpected fixture text.

## Web mode gate

`test/e2e/web_gate.js` is the deterministic web-mode gate run by `Public checks`.
It seeds a scratch `chat-stream-v2` sessions DB before boot (`test/e2e/lib/seed_web_gate.py`
— one visible session plus a short transcript), starts a loopback daemon on an
ephemeral port against that DB, serves the web bundle, drives the served page in
real headless Chrome over CDP, runs the named scenario functions in
`test/e2e/lib/web_scenarios.js`, and tears everything down (by default; `--keep`
intentionally leaves the browser and the scratch directory for debugging).
Ephemeral ports and a
seeded fixture make it deterministic; it exits non-zero on any scenario failure.

Named scenarios: **transport-and-config** (window.cc/HOST install, config matches
`/api/config`), **sidebar-from-inventory** (the seeded session renders), **slot
attach+type+resize+kill** (a real browser→host→tmux→browser round trip over a
local `ptest-web-*` session), **chat-transcript-paint** (the seeded transcript
renders). Only local tmux is touched; nothing is spawned, sent, or closed on any
shared daemon. A chat *send-turn* round trip is intentionally not a CDP scenario
(the public repo lacks the spawn/ingest fixture `tests/smoke/stub_cli.py`); the
browser send path is covered by `test/web_cc.test.js` and daemon send/ingest by
the `chat-stream-v2` python tests.

The **slot-column-split** scenario also drives the actual divider, checks both
tracks, reload persistence, mouse/touch reset and measured width limits.

Run locally: `npm run build:web && node test/e2e/web_gate.js` (needs a system
Chrome, `tmux`, and a Python with the daemon's `websockets`). `--profile <config.js>`
runs the scenarios against an external daemon for a by-hand check.

For focused full-renderer slot-layout acceptance, build the web bundle and run:

```bash
npm run build:web
node test/e2e/grid_split_gate.js web /tmp/pentacle-split-web
node test/e2e/grid_split_gate.js desktop /tmp/pentacle-split-desktop
```

This uses the same CDP client and scratch-daemon seeder. It launches the actual
Electron entry point or served browser bundle with an isolated profile, creates
four uniquely named local tmux fixtures, and checks their native PTY dimensions
after drag, maximize/restore and view changes. It also checks narrow widths,
header-control access and desktop process-restart persistence. It never restarts
an existing desktop. The `finally` path removes its owned sessions and processes;
cleanup failures fail the result. JSON, screenshots and logs go to the output
directory. Set `PENTACLE_PYTHON` or `PENTACLE_CHROME` for a local interpreter or
browser path; macOS uses the standard Google Chrome application path by default.

## Evidence contract

Evidence is a small object containing the test name, candidate identifier, timestamps, and pass/fail assertions. Do not paste transcripts, environment dumps, absolute home paths, or tokens into evidence. Synthetic prompts and responses should be short and recognizable, for example `fixture-question` and `fixture-answer`.

Retired provider-specific launchers and live operational evidence are outside this public harness. If a scenario needs a private service, keep it in a local-only test package rather than weakening this contract.
