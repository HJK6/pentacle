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

### Web voice composer

The [web composer contract](chat_protocol.md#web-composer-recording) is exercised
by `web-chat-voice` in [the web gate](../test/e2e/web_gate.js), implemented in
[web_voice_scenario.js](../test/e2e/lib/web_voice_scenario.js). It runs the
shipped composer, browser recorder, real blob upload and ordinary optimistic
send store in headless Chrome against disposable loopback services and seeded
synthetic sessions. Slot PTY creation and ASR/provider replies are stubbed; an
oscillator supplies a synthetic browser MediaStream. Transcription is held until
the pending row is observed, and the provider send is captured. No physical
microphone or live provider is used, and no live transcription backend is
contacted.

The scenario retains recording duration/accessible Cancel, visible
`backend_unavailable` with Retry, stable transcription identity, one text row,
media-track release and the assertion **chat mic leaves room mic untouched**.
It additionally checks:

- At least one visible metering bar within 1 s of recording, with no more than
  46 live bars, and a timer that advances from `0:00` in `m:ss` format
- The mounted composer capsule's computed green tint and border
- The computed green, circular 40 px **Stop and send** control
- A visible pending row in the transcript area containing uppercase
  `TRANSCRIBING` and 30 waveform bars before any text send
- Exactly one ordinary text dispatch, store row and DOM user row, with positive
  `meta.voice.duration_s` on the send and `voice.duration_s` on the projected row,
  plus a visible DOM voice caption in `m:ss` format
- No partial/interim transcript text or transcription/send call before stop.
  A MutationObserver plus 25 ms samples examines the recording panel, including
  text briefly inserted, removed or changed between samples. Recording labels,
  controls and `m:ss` timer text are allowed; other text fails the oracle

This browser scenario covers the optimistic row; it does not establish live
receipt persistence or a voice-caption reload against a deployed daemon.
The caption's optimistic/echo/fresh-history rendering, above-bubble pending
caption, Retry/Discard and interruption journeys have separate synthetic
coverage below.

#### Commands and collection

Run from the repository root after [installing the prerequisites](../README.md#local-setup)
and dependencies with `npm ci`. The complete candidate gates are:

```sh
npm run prestart
npm test
npm run build:web
node test/e2e/web_gate.js
```

`npm test` invokes the existing collector exactly as follows:

```sh
node scripts/run-tests.js "test/*.test.js" "test/*.test.ts" "test/e2e/terminal_interaction_runtime/*.test.js"
```

The web gate needs Chrome/Chromium, `tmux`, the daemon's provisioned Python
packages and permission to create local sockets and attach Chrome over CDP.
Use the no-profile invocation above for voice acceptance. The external
`--profile <config.js>` mode skips this hermetic-only voice fixture and the
history child gates, so it is not an equivalent pass. Never point this
acceptance run at a production daemon.

For focused repair and collection checks:

```sh
node scripts/run-tests.js test/web_voice.test.js test/web_voice_fidelity.test.js test/web_voice_journey.test.ts test/web_voice_scenario.test.js test/web_voice_integration.test.js test/web_voice_store.test.ts test/cc_handlers_parity.test.js test/web_cc.test.js
```

Keep the collector output naming the three added test files and their executed
cases; a filename listing or a focused pass does not replace the aggregate:

- [web_voice_fidelity.test.js](../test/web_voice_fidelity.test.js): 90 ms sampling,
  46-bar retention and stable nodes, geometry/opacity/animation rules, capsule
  tint, hidden controls with preserved draft, Discard, timer, green stop circle,
  MP4/WAV shared-stream metering and capture resource cleanup
- [web_voice_journey.test.ts](../test/web_voice_journey.test.ts): the right-aligned
  30-bin bubble with its caption above it; upload/transcription/empty failures;
  stable Blob/request identity on Retry; late-completion suppression on Discard;
  tab-hidden and ended-track interruptions, including pending-start/stop races;
  and the actual shared renderer for optimistic, acknowledged, exact-echo and
  fresh-history captions. Equal text with distinct identities and ordinary text
  Retry are covered without duplicating or transferring captions
- [web_voice_scenario.test.js](../test/web_voice_scenario.test.js): positive and
  isolated negative controls for the browser oracle, transient text insertion
  and replacement through MutationObserver, and collection of all independent
  failed predicates

Existing voice, bridge and store tests also cover permission denial,
unsupported contexts, MP4/WAV selection, PCM size bounds, empty transcription,
cancellation, captured destination, room-mic isolation and Retry metadata.

#### Full aggregate and evidence boundaries

The current [scenario registry](../test/e2e/lib/web_scenarios.js) contains
**15 named scenarios**:

1. `transport-and-config`
2. `settings-version-line`
3. `sidebar-from-inventory`
4. `coloured-host-glyphs`
5. `slot-attach-type-resize-kill`
6. `chat-transcript-paint`
7. `chat-file-delivery`
8. `web-chat-voice`
9. `public-chat-renderer-contracts`
10. `mic-panel-answer-window`
11. `slot-survives-cc-reconnect`
12. `slot-column-split`
13. `question-free-text`
14. `closed-chat-slot`
15. `host-restart-restores-input`

After a successful no-profile main run, the command also runs both
[history retention](../test/e2e/web_chat_history_retention_gate.cjs) and
[history paging](../test/e2e/web_chat_history_paging_gate.cjs) child gates, each
with its own browser/services and evidence directory. Either child failing
makes the command fail. A printed main `web_gate: PASS` alone is not full
aggregate success; inspect the child results, final exit status and cleanup.
Those general history gates do not replace the voice-specific caption tests.

Keep these evidence levels explicit in a candidate report:

- **Local unit/JSDOM and source checks:** deterministic synthetic devices,
  clocks, daemon frames, DOM rendering and CSS/source assertions. They establish
  the tested logic and renderer contract, not physical capture, real Chrome
  layout/animation or deployed backend behavior
- **Hermetic Chrome, local or hosted:** the built web bundle, real browser
  recorder and computed UI, real fixture blob upload, 15 scenarios and both
  history child gates. The existing [Public checks workflow](../.github/workflows/predeploy-tests.yml)
  runs the unit/build commands above and installs Chrome before the web gate.
  Bind a hosted result to the
  exact candidate SHA, harness and complete job output. An installation step
  still running or a Chrome/CDP/local-socket startup failure is not a product
  RED or a browser pass; record it as pending or setup-blocked
- **Physical/live release proof:** microphone hardware and permissions on the
  supported browser/device, live backend availability/transcription, durable
  receipt/USER echo/history through the deployed daemon, and the served bundle's
  record → waveform → stop → TRANSCRIBING → captioned-text journey. These remain
  separate fleet-owned release checks; synthetic tests do not establish them

For baseline RED evidence, use the unchanged pre-feature product with the new
oracle and retain each observed predicate. Missing live bars, the mobile stop
circle and the uppercase/30-bin pending row are the intended new regressions.
Timer progress, one text send with voice metadata and no interim transcript
already worked on that baseline; their deliberately broken observations are
**oracle negative controls**, not baseline regressions. Expected RED is not
observed RED until browser setup succeeds and the predicates actually run.

Retain command output, candidate/harness identity, verdicts and cleanup results
without real transcripts, credentials, private endpoints or machine paths.
The browser scenario can save `voice-recording.png` and `voice-transcribing.png`
in its run directory. Treat them as evidence only when they were produced by a
verified synthetic run; this document does not assert that screenshots or any
particular candidate's browser gates have passed.
