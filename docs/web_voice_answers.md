# Voice answers on question cards

Record one take while paging through the assistant chat's durable question
cards. Use the card-header microphone, speak on each relevant page, then choose
**Done**. Capture, upload and transcription reuse `renderer/web_voice.js` on
both Electron and the served web client. The deck always uses browser capture
mode; it never toggles the room microphone or needs the mic server.

The recording bar sits between the pager and card. It shows elapsed time, the
existing audio meter, **k of n answered by voice**, and Done. Below 480 px it
wraps into two lines. The composer remains available for typed answers.

## Coverage and completion

A single visit of at least 1,500 ms makes an eligible page covered. Separate
short visits are not added together. When a page has several qualifying visits,
its longest visit supplies its segment. The page reads **RECORDING YOUR ANSWER…**
until it is covered, then **ANSWER RECORDED**.

- The total `n` is frozen at recording start: every durable page counts,
  including a durable page missing binding identifiers
- Legacy pane pages read **ANSWER BY TAP**, contribute to neither `k` nor `n`,
  and must be answered by their ordinary controls
- Questions arriving after recording starts cannot join that take
- Questions removed or closed during recording are omitted when Done freezes
  the selected set
- Selected items follow their segment-start order, rather than pager order
- The tracker caps the earliest 20 covered pages before removing pages the
  daemon no longer lists. It never backfills: with 21 covered and the earliest
  closed, 19 are bound. `n` stays the full original durable count

Coverage is eligibility, never a question acknowledgement. Done, upload,
transcription and a successful binding leave cards, pager dots and the sidebar
question count unchanged. Only the daemon's ordinary question-close updates
retire questions. The assistant can answer some bound questions while leaving
others open. Stale items remain marked in the daemon's binding status.

The pending transcript bubble says **ANSWERS n QUESTIONS**. The completed
transcript replaces the pending bubble. There is no live or interim
transcription while recording.

## Cancel, leave and busy recording

The recording discard control, Escape, leaving the slot, and changing view
ask **Discard this recording?** Choose **Keep** or **Discard**. Discard releases
the tracks and sends/uploads nothing. Done with no covered eligible page
discards directly. Existing unavailable, denied and interrupted capture
handling remains owned by the shared voice unit.

The shared recorder permits one active capture at a time. If another deck or
composer is recording, the new attempt starts nothing and displays
**Another chat is recording. Finish or cancel that take first.** Neither
controller cancels or discards the other's recording.

## Wire contract and refusal

This is the P6 `voice_answers.v1` contract, matching the mobile voice-answer
selection semantics pinned at `HJK6/pentacle-mobile`
`b9f4564ac1affcb244e89145aa850bec71ab3acf`. The daemon's authoritative shape
and validation live in [`voice_answers.py`](../services/chat-stream-v2/voice_answers.py);
its integration vectors live in
[`test_voice_answers.py`](../services/chat-stream-v2/tests/test_voice_answers.py).
Coverage segment timestamps preserve the mobile port's one-decimal rounding.
The host applies the daemon's three-decimal normalization to the wire shape.

The existing composite `send` carries only the newly allowed binding beside
ordinary voice metadata:

- `meta.voice`: positive captured duration in `duration_s`
- `meta.voice_answers`: `version: 1`, `recording_id`, `blob_sha`, `duration_s`,
  and 1–20 items
- Each item: unique `key`, `question_id`, `notification_id`,
  `producer_stream_id`, `surface_stream_id`, `prompt`, and
  `segment: { start_s, end_s }`

The five item identifiers, recording identifier and blob hash are nonempty
strings of at most 200 characters. The prompt is a string of at most 2,000
characters. Durations and segment endpoints are finite non-negative numbers,
not booleans; the end cannot precede the start. The host normalizes the allowed
shape and excludes unrelated metadata keys.

An invalid supplied binding is never stripped silently. The host does not
send it and returns `error_code: voice_answers_invalid`. The row displays
**Couldn't attach questions** with **Send as plain voice note**. Nothing is
sent until the operator chooses that control. Conversion sends the transcript
with `meta.voice` only, retaining the optimistic row identity. It does not mark
any question answered or start an automatic binding retry.

The real daemon validates question identities under the authenticated operator,
deduplicates on `recording_id`, and places `voice_answers_status` on the USER
echo. `bound` means the take was attached, not that a question was answered.
`dropped` produces **Couldn't attach questions**; stale keys remain available
on the projected row. The ported shared-core slice carries this status and the
bound-item count without syncing unrelated mobile changes.

## Capture policy and evidence boundaries

Capture uses the existing secure-context and `mediaDevices.getUserMedia`
checks, including their existing disabled-control explanation. This feature
adds no permission handler, blanket Electron media grant, CSP,
Permissions-Policy, origin change or different-origin frame. Browser permission,
the served origin and the actual response headers must be checked, not inferred
from a successful build or from loopback access.

The hermetic gate records its own `http://127.0.0.1:<port>` origin, actual
served-page response headers, secure-context flag and microphone permission
state. Those observations do not establish the deployed HTTPS host's policy.
The fleet-owned `web-voice-answers-live-check` records the deployed HTTPS origin,
headers, permission state and seeded card-mic rendering after rollout, without
making a recording.

Physical-microphone acceptance remains **UNTESTED until the operator's first
natural take**. Synthetic capture does not certify a real microphone, device
permissions, live ASR, a provider response, or rollout.

## Hermetic gate and focused checks

`node test/e2e/web_gate.js` launches Chrome with
`--use-fake-device-for-media-stream --use-fake-ui-for-media-stream`. Never use
`--profile` for the voice-answer acceptance run. The `web-voice-answers`
scenario does not replace `getUserMedia`, `MediaRecorder`, the send bridge or
PTY creation. Only ASR responses are synthetic. Track-stop and upload observers
forward to the original functions and restore them in cleanup.

The isolated producer creates three durable questions on a real assistant
composite. The fixture disables only downstream routing; authentication,
binding validation, question storage, USER admission and readback are the real
daemon. No external daemon or live provider is contacted.

The scenario covers pages 1 and 3 while briefly visiting page 2, checks the
pending count, reads the daemon USER events through `requestStreamEvents`, and
requires `voice_answers_status.state === 'bound'`. It checks unchanged question
state and counts, then tampers a prompt beyond 2,000 characters to exercise
host refusal and the explicit one-send plain conversion. Cancel and leaving the
slot must stop actual tracks without uploads or sends. Screenshots contain only
synthetic questions and transcripts.

`test/voice_answers_scenario.test.js` exercises the same DOM reader and oracle
functions as the browser scenario, including negative controls for each new
contract predicate and scenario ordering. Existing voice error tests and the
`web-chat-voice`, `question-free-text`, `mic-panel-answer-window` and renderer
contract scenarios remain required.

Current scenario order: transport-and-config; settings-version-line;
sidebar-from-inventory; coloured-host-glyphs; slot-attach-type-resize-kill;
chat-transcript-paint; chat-file-delivery; web-chat-voice;
public-chat-renderer-contracts; mic-panel-answer-window;
slot-survives-cc-reconnect; slot-column-split; question-free-text;
**web-voice-answers**; web-dashboards-revamp; dashboard_catalog; web-work-lanes;
closed-chat-slot; host-restart-restores-input.

Required candidate checks are `npm test`, `npm run build:web` (and
`npm run prestart` when needed), and the complete `node test/e2e/web_gate.js`.
The focused oracle tests and fixture setup checks pass. The complete local
gate was attempted with a prepared Python environment: the isolated daemon
and web host started, but Chrome exited before any scenario with
`process_singleton_posix.cc: socket() failed: Operation not permitted`.
CDP then reported `ECONNREFUSED`. Browser scenarios are therefore **NOT RUN**;
unit/build success does not certify them. The fixture daemon and generated
operator credential were cleaned up. The fleet must run the full gate on the
candidate in its supported browser environment. Live transcription, deployed
HTTPS and physical-microphone acceptance remain untested.
