# Answer window and silent mode

Two additions to the room-microphone voice stack: an **answer window** so Bart can
ask a question and listen for the reply without a fresh wake word, and a **silent
mode** that stops Bart's audio while the microphone keeps listening. Both are proven
without an audio device (null sink); live activation on the running service is a
separate owner-controlled step. See [room microphone conversation](room_mic_conversation.md)
and [local spoken conversations](voice_speaker.md) for the surrounding contract.

## Answer window after a spoken question

A spoken line may declare that it is a question Bart needs answered to proceed:

```sh
printf '%s' 'Should I deploy the release now?' |
  bart-say --conversation-id <id> --kind reply --expects-answer
```

- `/speak` and the helper accept `expects_answer` (boolean, default false). A line
  cannot be both `final` and `expects_answer`; that is refused (`final_and_expects_answer`)
  and renders nothing.
- Every accepted line's response includes a `line_id` unique within the conversation.
- When a question finishes playing, the service plays the **listening tone** (when
  `replies.listening_tone` is on) and opens the microphone for an answer. The window
  opens only after the recognition fence for that playback has passed, so Bart's own
  question is never captured.
- The operator speaks the answer and ends with **"over"**, with no wake word. The answer
  is delivered as a room-microphone turn whose machine header carries exactly `origin`
  `room_mic`, the same `conversation_id`, and `answer_to` set to the question's `line_id`.
  A turn that is not an answer carries no `answer_to`.
- An acknowledgement plays for the answer as for any routed request, on the same conversation.
- No speech inside the window closes it silently; the conversation stays open and the
  operator can still say "Hey Bart". A fresh wake word during the window starts a new
  request and closes the window. A suppressed question (silent mode) opens no window.
- One window at a time: a Bart answer window never overlaps a local-action answer window.

### Line allowance

This extends the speaker-service closure rules in one respect. The line allowance counts
accepted lines **since the last delivered answer**, bounded by `replies.lines_per_conversation`:

- A line is refused (`exhausted_conversation`) once that count reaches the limit.
- Reaching the limit closes the conversation, except when the limit-reaching line carries
  `expects_answer`; closure is then deferred until its window ends.
- A delivered answer sets the count to zero and the conversation stays open.
- A window that ends with no answer closes the conversation only if the limit had been reached.
- `final` closes at once and the conversation ceiling closes at its time, both unchanged.
- An answer captured for an already-closed conversation is delivered as a fresh room-mic
  request with a new id and no `answer_to`.

The question ceiling is `replies.questions_per_conversation`; a question beyond it is refused
(`questions_per_conversation`). On the evaluation set, a line flagged `expects_answer` must be a
single direct question ending in "?"; a statement or a multi-question line fails the run
(`tools/voice_line.check_expects_answer`, surfaced by `tools/voice_eval.py` as `expects_answer_ok`).

`/status` exposes `speaker.answer_window` (`waiting`, `ready`, `conversation_id`, `line_id`,
`expires_in`); the renderer helper `mic-state.computeAnswerWindowState` renders the waiting-for-answer
state wherever the mic status is shown.

## Silent mode

Silent mode is a speaker-service flag, **independent of the mic mode and of meeting mode**
(neither sets the other). While silent, no clip and no spoken line plays: a submitted line
returns `suppressed` with reason `silent_mode`, and suppressed lines are never queued or
replayed when silent mode ends. The microphone keeps listening, so a spoken request is still
delivered and answered in chat. The flag survives a service restart (persisted to
`MIC_VOICE_SILENT_STATE`, default `<speaker output dir>/silent_state.json`).

- **Endpoint.** `POST /mode/silent {"on": <bool>, "source": "web"|"mobile"|"voice"}` sets the
  flag and returns `{silent, source, changed_at}`. Turning it **off** plays the `silent_off`
  confirmation clip; turning it **on** plays nothing.
- **Voice.** "Hey Bart, silent mode on, over" / "... silent mode off, over" are matched inside
  the mic service from the rules `modes.silent` phrases when the wake line completes, before any
  routing to Bart, and are never delivered to the assistant. Near misses ("silent movie",
  "meeting notes") are ordinary room-mic lines.
- **Meeting by voice.** "start meeting" / "end meeting" are matched the same way from
  `modes.meeting` and start/stop the meeting recorder. The listener stays in `LISTENING` during a
  meeting, so "Hey Bart ... over" still reaches Bart and can end the meeting. (Before this change,
  with the wake-capture path those phrases were delivered to Bart as text rather than executed.)
- `/status` reports `speaker.silent`, `speaker.silent_source` and `speaker.silent_changed_at`;
  the renderer helper `mic-state.computeSilentState` renders the state. The web and mobile toggles
  post to `/mode/silent`.

## Rules additions

`replies` gains `answer_window_seconds` (20), `listening_tone` (true) and
`questions_per_conversation` (3); `clips` gains a `listening_tone` group. The shipped
`listening_tone` clip is a placeholder phrase to be replaced with a short tone asset at
activation. The `modes.silent`/`modes.meeting` phrase sets already exist in the schema.
Existing policy files must gain the three new `replies` keys and the `listening_tone` clip
before activation; an invalid or old schema leaves the last valid policy loaded with an error.

## Validation and rollout

`PYTHONPATH=mic-server python -m pytest mic-server/test` (null sink enforced) covers the window
open/expire/deliver path, the allowance, the `final`+`expects_answer` and question-ceiling refusals,
silent suppression/persistence/source, the in-service silent/meeting phrase matching (including the
meeting step-2 classification), the no-wake-word answer capture and tagged delivery, and the
`expects_answer` evaluation rule. The renderer's `room_mic_turns`, `wake_delivery` and `mic-state`
suites cover the `answer_to` header, the mic-status strings and the panel-state helpers.

Keep `MIC_LOCAL_ACTIONS=false` on this build (see [local spoken conversations](voice_speaker.md)).
Live-microphone proof and activation run on the host service in one owner-controlled window with a
rollback preimage and `/status` readback; the running service bytes are reconciled with public main
during that window. No operator test step or audible playback is required for the automated suite.
