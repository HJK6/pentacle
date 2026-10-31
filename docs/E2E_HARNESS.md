# Public end-to-end validation harness

The public harness is provider-free and uses a disposable local daemon. It validates the shipped websocket and renderer contracts with synthetic sessions; it does not connect to a production service or collect live user content.

## Supported entry: `scripts/gate_env.py`

Use the [behavior coverage map](TEST_COVERAGE.md) to find existing protection,
real/fake boundaries and unresolved selection; send retry to durable receipt
is the first bounded reference flow.

[Advisory test selection](TEST_SELECTION.md) reports affected boundaries and
coverage gaps for an exact baseline and proposed head. It runs no tests and
does not change required gates.

QA briefs and fleet gates run the daemon and npm checks through one entrypoint instead of an ambient shell. It builds an owned venv (Python 3.13) and `npm ci` cache keyed by toolchain and lockfile hashes, gives the child its own `HOME`, a short `TMPDIR` and a UTF-8 locale, drops every other environment variable (including `TMUX`, `MAKEFLAGS` and credentials), and preflights the exact child context before the subject starts. Missing or wrong executables, denied `ps`, absent tmux/lsof/socket capabilities, dependency drift and nested HOME/evidence/basetemp paths are all reported together as a setup failure and never reach the tests.

```bash
python3 scripts/gate_env.py run --profile pentacle --workspace OWNED_ROOT --evidence-dir EVIDENCE \
  --target v2-unit --spec SPEC --series SERIES --purpose acceptance --attempt-manifest MANIFEST
```

Targets are the closed list in `scripts/gate-env.json` (`v2-unit`, `v2-smoke`, `v2-merge`, `v2-merge-capacity`, `v2-pytest --select tests/test_x.py`, `npm-test --select test/x.test.js`); `prepare` (optionally with `--target`, to provision and preflight just that target) runs the same setup and never launches a subject or accepts `--select`; the focused `v2-pytest` target refuses to run without at least one `--select` (it never defaults to the full suite — use `v2-unit`/`v2-merge` for that). Every invocation writes append-only receipts under `EVIDENCE/attempts/<attempt_id>/` before any setup, so failures, cancellation and crashes stay visible; `EVIDENCE/test-attempts.v1.json` is a derived index. The daemon targets run `run_gate.py` under a finite `--deadline-seconds` envelope (tier limits, smoke preflight and reserve) and fold its `primary_outcome` and secondary outcomes into the receipt. A changed requirement, lockfile or profile selects a new prepared cache; a drifted cache is refused until `--rebuild` (which removes only that owned directory). Exit codes: 0 pass, 1 subject failure, 3 setup failure, 130 cancelled. Running `run_gate.py` or `pytest` directly remains possible but is not the supported route. Self-test: `python3 scripts/test_gate_env.py`.

Focused `v2-pytest` always includes the four existing repository invariants in
`tests/test_capabilities.py`: changed-test skip confinement, original assertion
pins, fixture manifest hashes and portability scope completeness. Selecting the
whole module already covers that floor; nodes are not added twice. The requested
selection and effective selection remain separate in the attempt receipts.
Fixture-only edits therefore receive the same cheap metadata checks without a
changed-file detector or regenerating metadata automatically. Select affected
behavior modules separately, including `test_h2_portable_fixtures.py` when its
behavior changes; the invariant floor does not replace those tests.

Explicitly select the Chrome interrupt contract; it is outside npm's defaults:

```bash
python3 scripts/gate_env.py run --profile pentacle --workspace OWNED_ROOT --evidence-dir EVIDENCE \
  --target npm-test --select test/e2e/web_gate_interrupt.test.js \
  --select test/e2e/web_wrap_paste_gate.test.js --select test/e2e/web_wrap_paste_served.test.js \
  --select test/e2e/lib/render_proofs.test.js \
  --spec SPEC --series SERIES --purpose acceptance --attempt-manifest MANIFEST
```

These selected web integration files (including supported wildcard selections)
reuse web-gate's builds and Chrome preflight; the interrupt contract additionally
reuses its Python dependencies, tmux and capabilities. Ordinary npm selections
retain their existing preparation. The integration file budget uses the existing
finite web-gate run budget rather than npm's ordinary 60-second file bound.
Missing Chrome or build is setup failure, not a skipped acceptance PASS. Retain
actual case counts, browser PID audits, file cleanup and the terminal receipt.
`--keep` now retains debug files only: the existing owner always stops its Chrome
group, on success, failure and supported signals. Unknown process inspection is
failed cleanup. It does not handle an uncatchable kill by claiming a finally ran.

The gate owner manifest compares process start identities before recording or
reaping children. On macOS, its `ps` probe uses `TZ=UTC0` and `LC_ALL=C`, so an
empty parent `TZ` and an unset child `TZ` identify the same process. These values
apply only to the probe; caller environments and ownership checks are preserved.

Sandboxed seats use the same supported entry. Do not hand-build its venv with
`--system-site-packages`: ambient user packages can disappear under the nested
isolated interpreter. The prepared venv fully installs the declared requirements.
The owned `HOME` also owns `.ssh/cm`; ambient SSH control-directory overrides are
not forwarded. Keep the real SSH directory read-only. Put Python 3.13 and Node 22
on the launching PATH, then use `--target v2-merge` for the serial closing gate;
its child PATH and HOME come from the bootstrap rather than the launching shell.

For a workstation with a measured unit overrun, the opt-in `v2-merge-capacity`
target runs the same serial unit and smoke union with a 1,200-second unit limit
and the unchanged 900-second smoke limit. Its runner deadline is 2,220 seconds,
inside a 2,250-second outer subject bound. Qualify it with a complete supported
gate on the intended host and retain the exact source, environment, node counts
and timings. A pass establishes that observed window, not a timing guarantee.
The default `v2-merge` and CI budgets are unchanged. The full 60-second hermetic
daemon observation, loss-operation trials and durability assertions still run;
the fixture speedup batches SQLite seed setup into one durable transaction per
database without changing product synchronization or crash tests.

## Unit and protocol checks

### Installed client seam

`installed-client-seam` extends the existing web gate's isolated seeder and
candidate daemon. It receives real summary snapshot/list, durable history,
live ingested update/inventory and reconnect/replay frames through the web
transport and renderer. Its MutationObserver checks intermediate DOM for
duplicate preview flicker; missing/malformed/legacy optional-summary controls
are explicitly synthetic model checks. It does not certify native rendering.

Run the named scope through the supported entry:

```bash
PENTACLE_WEB_GATE_SCENARIOS=installed-client-seam \
python3 scripts/gate_env.py run --profile pentacle --workspace OWNED_ROOT --evidence-dir EVIDENCE \
  --target web-gate --pass-env PENTACLE_WEB_GATE_SCENARIOS \
  --spec SPEC --series SERIES --purpose acceptance --attempt-manifest MANIFEST
```

The comma-separated filter accepts registered names only. Unknown names fail;
omitting it retains the full default web, interrupt and subsidiary gates.
`node test/e2e/web_gate.js --scenario installed-client-seam` is the underlying
focused browser entry. The supported wrapper also retains interrupt cleanup
checks when filtering scenarios.

To qualify an installed web version, additionally set
`PENTACLE_WEB_GATE_CONSUMER_ROOT` to an isolated checkout at its evidence-backed
source revision and pass `--pass-env PENTACLE_WEB_GATE_CONSUMER_ROOT`. Provision
that checkout's locked dependencies and copy its exact deployed
`renderer/dist/web` artifacts there; never rebuild a newer source and call it
installed coverage. The candidate daemon stays in the invoking checkout.
No configured root means candidate-only web coverage. Record actual deployment
and mobile build/source receipts before selecting the finite support set;
missing identities remain UNTESTED, not a main-branch substitution.

Each scenario retains `client-seam-wire.json` with raw synthetic application
frames, producer source hashes, producer/consumer Git SHAs and web asset hashes,
plus browser DOM observations and PNGs. Authentication handshakes are excluded.
The capture can feed the mobile repository's explicit Jest seam entry using
its capture path, SHA256 and producer pin; bind both receipts in the same
acceptance run. Mobile transport/model proof does not establish native UI
behavior. Pin installed and candidate consumers separately, deduplicating only
identical sources. A verified producer whitespace-flattening mutation must fail
the before-history rendering assertion; retain mutation/preimage hashes, raw
exit, failed predicate, restored-source identity and subsequent GREEN.

Run the repository's normal unit gate first, through the supported entry above (`--target v2-unit`). The underlying tier runner is:

```bash
python3 services/chat-stream-v2/tools/run_gate.py unit
```

Each tier's evidence keeps the original subject outcome (`reason`, also `primary_outcome`) apart from reporting and cleanup facts (`secondary_outcomes`), plus the raw `subprocess_returncode` and a `termination` record that tells an internal tier timeout from an external signal. A progress journal (`<tier>.progress.jsonl`, written by `tools/gate_progress.py` outside basetemp) names the last started and completed nodes even after a per-test timeout hard exit; it holds node identities, phases, outcomes and monotonic times only. Missing JUnit, an incomplete journal, a truncated log or an unwritable log always fail the tier. The evidence file is written as `finished: false` before launch, so a runner killed without cleanup stays visibly incomplete. `--deadline-seconds N` refuses with `deadline_envelope_exceeded`, before launch or between tiers, when the remaining time cannot cover the remaining tier bounds, smoke/soak preflight (30s), usage validation (30s) and `--deadline-reserve-seconds` (default 60s); every check's components are recorded under `deadline`.

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

The **optimistic-reload** scenario drives the real composer before and after a
page reload, holding sends in a deterministic bridge while replaying a delayed
pre-reload USER echo. With both different and identical message text, it checks
that the new send stays visible and pending until its own echo arrives. This
tests renderer/chat-core correlation against the isolated fixture daemon;
provider delivery is outside this scenario. New ordinary and composite input
IDs use 128 random bits via `crypto.getRandomValues`, including on HTTP hosts
without `randomUUID`; retries and reconnect replays retain the stored ID. The
daemon still receives an opaque string, with no wire-contract change.

The **question-partial-send** scenario answers one of two owned durable cards and
checks that its sibling stays open. **question-skip** clicks Skip, then verifies
the ordinary answered state, reserved answer text and producer await readback.

The **question-free-text** scenario creates disposable durable questions in the
isolated daemon and checks the complete adapter/card/submit path for free text
and custom single/multi-choice answers, whitespace rejection, duplicate clicks,
reconnect and answered-state reload. Free-text questions send `text` with the
`resolved` action; choice custom answers send `custom_text`. A zero-choice card
shows its labeled textarea immediately. Actual asking-seat delivery still needs
a live fixture journey; this provider-free gate asserts durable resolution.

The **composer-draft-per-chat** scenario uses the disposable chats and the real
composer to check navigation, image ownership, send clearing, delayed uploads,
newer edits, a composite chat and separate browser-context isolation. It also
checks that unsent fixture text is absent from outgoing websocket frames. The
**composer-draft-pending-send** and **composer-draft-retire** scenarios verify
that a held upload cannot be sent twice after navigation and that an
authoritatively closed, unmounted chat releases its image preview.

The **scheduled-section** scenario reads the seeded recurring rule (weekly, days from
firing, in America/Chicago) and one pending one-shot from the sidebar Scheduled section,
then clicks Pause, Resume and Edit (a new `at_time`) and reads each result back from the
daemon with `schedule.recurring.get`, including the re-armed `next_fire_at_utc`.

The **slot-column-split** scenario drives both row dividers and checks independent
30/70 and 65/35 widths, true reload persistence, legacy preference migration,
row-specific mouse/touch/keyboard reset, cancellation and measured width limits.

Run locally: `npm run build:web && node test/e2e/web_gate.js` (needs a system
Chrome, `tmux`, and a Python with the daemon's `websockets`). `--profile <config.js>`
runs the scenarios against an external daemon for a by-hand check.

SIGINT, SIGTERM and SIGHUP tear the gate down the same way a finished run does:
Chrome runs in its own process group and the whole group is stopped, then the daemon,
scratch, user-data and runtime directories are removed (unless `--keep`). The gate
records a FAIL verdict with `interrupted` set and exits 130, 143 or 129; a second
signal during teardown is ignored. SIGKILL cannot be handled. Check it with
`node --test test/e2e/web_gate_interrupt.test.js` (needs Chrome and the web build;
it boots the gate three times, so it is not part of `npm test`).

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

The [scenario registry](../test/e2e/lib/web_scenarios.js) is authoritative for
the default gate, including the installed-client seam above. Core journeys include:

1. `default-pane-assistant`
2. `transport-and-config`
3. `branding`
4. `cross-origin-boundary`
5. `settings-version-line`
6. `sidebar-from-inventory`
7. `coloured-host-glyphs`
8. `slot-attach-type-resize-kill`
9. `chat-transcript-paint`
10. `chat-file-delivery`
11. `web-chat-voice`
12. `public-chat-renderer-contracts`
13. `mic-panel-answer-window`
14. `mic-source-selector`
15. `slot-survives-cc-reconnect`
16. `slot-column-split`
17. `question-free-text`
18. `question-partial-send`
19. `question-skip`
20. `web-voice-answers`
21. `web-dashboards-revamp`
22. `dashboard_catalog`
23. `web-work-lanes`
24. `scheduled-section`
25. `pane-count-selector`
26. `chat-first-style`
27. `agent-chat-steer-controls`
28. `closed-chat-slot`
29. `host-restart-restores-input`

`cross-origin-boundary` is hermetic-only. A page on a second loopback origin
must get the host's own 403 for a `fetch` (read from Chrome's network log, not
a generic CORS failure), must fail to open the unauthenticated `/cc` bridge
(the host's connection count is read while the page holds the attempt), and
must receive `authentication_required`, never a snapshot, from the daemon. The
same page, holding the gate's isolated operator credential, is admitted (hello,
snapshot, `list_sessions`); only the single-use proof enters the page. A
DNS-rebinding hostname, mapped to loopback by a Chrome `--host-resolver-rules`
entry merged into the fixture's rules, must get `origin_refused`. The served
same-origin page and an Origin-less native daemon client are the positive
controls.

`default-pane-assistant` runs first on the gate's fresh browser profile: one
pane holding the seeded assistant composite chat, no spawn, then a saved
four-pane choice wins on reload and remains for later scenarios.
`pane-count-selector` checks the 1-4 pane geometry, hidden panes keeping their
sessions and live tmux clients (no detach), placement into an empty visible
pane and replacement of the focused pane when all visible panes are full,
maximize/restore, persistence and the phone-width drawer.
`chat-first-style` checks the flag-off classic palette, copy and chrome and the
flag-on chat-first look for both saved themes and densities, with screenshots.
At a fixed 1440x900 viewport it records a flag-off probe (every stylesheet
custom property, theme, title, default view, sidebar labels, empty-pane copy,
chrome computed styles) and screenshot per theme/density. With
`PENTACLE_FLAG_OFF_BASELINE_DIR` set to a run directory recorded on main, it
requires the probe to equal main's and every changed pixel to lie in a named
region: the titlebar (sidebar toggle and pane selector) or live data (working
spinners, machine stats). Gate page reloads (`reloadDashboardPage`) start the next document
with an empty four-pane workspace so scenarios see a fresh renderer.

`agent-chat-send-default-queue-state` drives the real composer/shared transcript with deterministic ordinary-send, queued-action and Stop bridges. Enter/Alt+Enter retain ordinary intake; qualified provider queues render minimal Send now with no warning. Repeated pending clicks preserve original IDs/inputs and the web draft, without invoking Stop. Explicit Stop remains generation-fenced; the composite has no queued acceleration or Stop. These are DOM/bridge checks, not native shortcut, abort or model-completion proofs; native provider and durable-control/restart gates are separate.

`test/e2e/web_host_recovery_gate.cjs` retains counted ordinary/composite lost-ACK, real reconnect, manual Retry overlap and >60-second durable restart acceptance. Its legacy `PHASE2_KIND=urgent` cell now verifies the retired operator action's ordinary replacement, with zero Escape and no urgent wire flag; intentional lower-level urgency has separate preservation controls. Counters live outside restarted processes; provider acceptance/queued receipts never stand in for model completion.

Disposable installed-provider acceptance must separately bind the actual CLI/version, owned session generation, browser assets and harness hash. Establish the real provider queue creation route first (Codex versions that steer without queuing require explicit queue-cell N/A, not seeded queue proof). Delay only the owned USER echo to reproduce native intake before the stale queued control; count original inputs and native turn interruption/current-tool completion separately, since Codex tools can outlive an aborted turn. Keep the original failure and any corrected oracle replay, release owned profiles/processes, and never use operator chats. Deployment additionally inventories each operator tab's own injected build; the existing refresh icon only prompts a click.

After a successful no-profile main run, the command also runs the
[history retention](../test/e2e/web_chat_history_retention_gate.cjs),
[history paging](../test/e2e/web_chat_history_paging_gate.cjs) and
[first-visit default pane](../test/e2e/web_default_pane_gate.cjs) child gates,
each with its own browser/services and evidence directory. The last proves one
pane bound to the direct-assistant target exactly as a sidebar click binds it
when the daemon has an assistant composite, keeps that pane and its draft
across a reload, and retires it (never opening a stale target, never
reopening later) when a reload finds the target on another generation; without
a composite it shows one empty pane. Any child failing makes the command fail. A printed main `web_gate: PASS` alone is not full
aggregate success; inspect the child results, final exit status and cleanup.
Those general history gates do not replace the voice-specific caption tests.

Keep these evidence levels explicit in a candidate report:

- **Local unit/JSDOM and source checks:** deterministic synthetic devices,
  clocks, daemon frames, DOM rendering and CSS/source assertions. They establish
  the tested logic and renderer contract, not physical capture, real Chrome
  layout/animation or deployed backend behavior
- **Hermetic Chrome, local or hosted:** the built web bundle, real browser
  recorder and computed UI, real fixture blob upload, every registered scenario and the
  child gates. The existing [Public checks workflow](../.github/workflows/predeploy-tests.yml)
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

The supported clean-HOME web entry is `python3 scripts/gate_env.py run --profile
pentacle --target web-gate` with the workspace/evidence/spec/series/purpose/manifest
identity options in the gate environment guide. It prepares the existing builds,
then runs the real browser journey and interrupt contract, retaining reports in
the evidence root. Shared JS Chrome flags live in `test/e2e/lib/chrome_launch.cjs`.
See [the CI step audit](ci_gate_step_audit.md) for the exact preserved CI coverage.
