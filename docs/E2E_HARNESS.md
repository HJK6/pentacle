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

Run locally: `npm run build:web && node test/e2e/web_gate.js` (needs a system
Chrome, `tmux`, and a Python with the daemon's `websockets`). `--profile <config.js>`
runs the scenarios against an external daemon for a by-hand check.

## Evidence contract

Evidence is a small object containing the test name, candidate identifier, timestamps, and pass/fail assertions. Do not paste transcripts, environment dumps, absolute home paths, or tokens into evidence. Synthetic prompts and responses should be short and recognizable, for example `fixture-question` and `fixture-answer`.

Retired provider-specific launchers and live operational evidence are outside this public harness. If a scenario needs a private service, keep it in a local-only test package rather than weakening this contract.
