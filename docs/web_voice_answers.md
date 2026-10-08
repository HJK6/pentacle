# Voice answers on question cards

Record one take while paging through the assistant chat's durable question
cards. Use the card-header microphone, speak on each relevant page, then choose
**Done**. Capture, upload and transcription reuse `renderer/web_voice.js` on
both Electron and the served web client. The deck always uses browser capture
mode; it never toggles the room microphone or needs the mic server.

The recording bar sits between the pager and card. It shows elapsed time, the
existing audio meter, **k of n answered by voice**, and Done. Below 480 px it
wraps into two lines. The composer remains available for typed answers.

## Implementation map

- `renderer/question_voice_bar.js` owns the deck capture lifecycle: mic, recording
  bar, Done, discard confirmation and the busy-recorder message
- `renderer/voice_answers_binding.js` owns coverage segments, the frozen selected
  set and the per-recording binding registry
- `renderer/app.js` owns the question portal, paging and the rule that only a
  real assistant composite chat can record
- `main/voice_answers_meta.js` and `main/chat_stream_client.js` own host
  validation, normalization and the `voice_answers_invalid` refusal
- `renderer/src/chat_store_controller.ts` and
  `renderer/src/shared_transcript_view.ts` carry and render the refusal, the
  daemon status and the plain-note conversion
- `pentacle-chat-core/src` carries the shared status and bound-item count types

Focused tests: `test/voice_answers_binding.test.js`,
`test/voice_answers_card.test.js`, `test/voice_answers_meta.test.js`,
`test/chat_stream_client_voice_answers.test.js` and
`test/voice_answers_scenario.test.js`. The browser scenario is
`test/e2e/lib/voice_answers_scenario.js`.

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
Unit and build success do not certify the browser scenarios: the complete
gate must run where Chrome can start. Its loopback daemon needs a Python with
the `services/chat-stream-v2/requirements.txt` packages (`--python` or
`PENTACLE_PYTHON`) and runs with `--disable-reconciler`, because fixture
sessions have no pane and the session reconciler would otherwise close them,
and their open questions, once a run outlasts one tick. The hermetic gate does
not exercise live transcription, deployed HTTPS or a physical microphone.

## Live check with saved audio

A deployed HTTPS client can be exercised end to end without a person or a
physical microphone. The bound assistant seat raises one clearly marked test
question, because only its questions surface in the assistant chat and the
daemon drops a binding for any other producer. Headless Chrome then records on
that card with a saved WAV as the capture device:

```
--use-fake-ui-for-media-stream --use-fake-device-for-media-stream
--use-file-for-fake-audio-capture=<absolute path>.wav%noloop
--disable-features=AudioServiceSandbox
```

Without `--disable-features=AudioServiceSandbox` the sandboxed audio service
cannot open the file and the fake device feeds silence. Capture, upload and
binding still succeed, and the speech model returns a filler phrase instead of
the spoken text. Prove the capture on a local page first: record a few seconds
with the same flags and check the level or transcribe the result.

The driver must confirm that the test card is the only visible card before it
presses the mic, so no real question is covered. A passing run shows the spoken
text as the transcript, `voice_answers_status.state === 'bound'` on the USER
echo, and the question answered by the assistant from that recording. This was
run once against the deployed client after rollout. It does not certify a
physical microphone or a person's browser permission prompt.
