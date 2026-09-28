# Public end-to-end validation harness

The public harness is provider-free and uses a disposable local daemon. It validates the shipped websocket and renderer contracts with synthetic sessions; it does not connect to a production service or collect live user content.

## Unit and protocol checks

Run the repository's normal unit gate first:

```bash
python3 services/chat-stream-v2/tools/run_gate.py unit
```

For a websocket smoke check, start a daemon on an ephemeral loopback port with temporary session and notification stores, then exercise `welcome`, `hello`, `list_sessions`, `spawn`, `send`, and `close`. The test should use a fake provider executable that writes deterministic output to a disposable tmux session.

## Recorded ingress probe rehearsal

`services/chat-stream-v2/tools/provider_wrapper_probe.py` retains the existing
fleet smoke ownership and nonce checks. Its `assert_provider_source` oracle
accepts a native one-line USER only with the exact provider record UUID and
source/display text. XML-wrapped content still requires the strict grammar,
retained raw content and exact provider source. An absent satellite optional
raw field is not permission to omit the actual source record.

The reused L4 native AX oracle is
`services/chat-stream-v2/tools/mobile_probe_oracle.py`. A shown Read card must
have the exact daemon tool-use card identity and the renderer's
`Tool result. Read <owned-path>` label. Hidden mode must omit tool cards.
Navigation identity, compact answer, metadata exclusion, and loaded image
controls remain required. Positive operator controls additionally require
native non-meta provider USER records and a PNG image block for the attachment;
daemon attachment metadata or caption text alone cannot pass.

Run the focused harness controls with:

```bash
python3 -m pytest services/chat-stream-v2/tests/test_provider_wrapper_probe.py services/chat-stream-v2/tests/test_provider_wrappers.py
```

Replay recorded source/events and saved AX captures with malformed UUID,
display, XML, route, authentication, navigation and card controls before any
live window. Keep fixture remapping and mocked UI results explicitly separate
from recorded inputs. A dry-run must intercept subprocess calls in imported
L4 helpers too; deny native/browser transports and allow only the exact offline
shared-model invocation. Reuse the existing L2/L4 client helpers rather than
creating another measurement harness.

Live provider probes require a coordinator's resource window and any required
test-target exception. The initial prompt declares every step, including the
useful finite foreground scratch pytest job; queued journeys require
`--foreground-job 'HOST=python3 /absolute/scratch/job.py probe'`. No wait-only
busy task is used. The oracle checks foreground tool parameters, pending
pytest descendants in the owned pane ancestry, and absence of a tool result
immediately before resolving the notification.

Bind the harness source commit separately from the observed running daemon
artifact/PID. Replay cannot pass live typed-coordinate, actual PNG,
idle hidden/shown mobile, web, queued-mobile or closed-session visual cells.
The recorded work-item matrix owns their individual status and prerequisites.
Finally cleanup must report exact expected/closed seats and browsers and
expected/revoked credentials, including when capture or CDP close fails.
Keep raw receipts and host configuration outside the public source tree.

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
The fixture daemon uses its own operator credential registry and token, with the
production challenge/proof verifier. Scratch paths are canonicalized so secure
token reads also work on systems whose temporary directory has a symlink parent.
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

The **question-free-text** scenario creates disposable durable questions in the
isolated daemon and checks the complete adapter/card/submit path for free text
and custom single/multi-choice answers, whitespace rejection, duplicate clicks,
reconnect and answered-state reload. Free-text questions send `text` with the
`resolved` action; choice custom answers send `custom_text`. A zero-choice card
shows its labeled textarea immediately. Actual asking-seat delivery still needs
a live fixture journey; this provider-free gate asserts durable resolution.

The **slot-column-split** scenario drives both row dividers and checks independent
30/70 and 65/35 widths, true reload persistence, legacy preference migration,
row-specific mouse/touch/keyboard reset, cancellation and measured width limits.

Run locally: `npm run build:web && node test/e2e/web_gate.js` (needs a system
Chrome, `tmux`, and a Python with the daemon's `websockets`). `--profile <config.js>`
runs the scenarios against an external daemon for a by-hand check.

For the external loopback profile, choose a free port for each run and use that
same value for the daemon and gate. For example, start the daemon in one shell:

```bash
export PENTACLE_SMOKE_PORT=49001
SCRATCH=$(mktemp -d)
python3 services/chat-stream-v2/main.py --host 127.0.0.1 --port "$PENTACLE_SMOKE_PORT" \
  --local-host local --db "$SCRATCH/sessions.db" \
  --notifications-db "$SCRATCH/notifications.db" \
  --assets-db "$SCRATCH/assets.db" --blob-root "$SCRATCH/blobs" \
  --disable-hosts --disable-mirror --disable-nudges \
  --disable-outbound-notices --disable-remote-presence
```

In a second shell, export the same `PENTACLE_SMOKE_PORT` and run
`node test/e2e/web_gate.js --profile test/e2e/configs/web_mode_local_smoke.js`.
Stop the daemon and remove its scratch directory afterward. The profile rejects
missing ports and values outside decimal `1..65535`. Each gate run stores its
verdict in a unique directory under `test/e2e/runs/`, including runs started in
the same millisecond. The no-profile gate still starts its own daemon on an
ephemeral port. Its verdict records the owned daemon PID and confirmed exit;
failure to stop that process fails the gate after a forced termination attempt.

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
