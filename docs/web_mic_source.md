# Web microphone Source

The served web client's MIC panel has a **Source** control. It says which
microphone this browser's chat voice and question voice answers capture from:

- **Room Source** (the default on every browser). Its label is the web host's
  configured machine name, `hostNames[chatStream.localHost]`, or **Room mic**
  when that name is unset. Capture uses the browser's default input. Behaviour
  is the same as before the Source control existed.
- **This device**. Only rendered in a secure context when
  `navigator.mediaDevices.enumerateDevices()` lists at least one `audioinput`.
  A device picker appears under the control, with **System default** first and
  then the browser's inputs, plus an `Active: <label>` line. A chosen input is
  passed to `getUserMedia` as `{ audio: { deviceId: { exact: <id> } } }`.
  System default passes `{ audio: true }`.

The Source applies only to browser capture: the composer microphone and the
question-card voice bar. The room controls (On/Off, Copy, Meeting, Silent),
their `/status` polling and the transcript preview are unchanged and keep
controlling the room mic service under either Source. Spoken replies stay on
the room speaker.

The control renders only in the web client (`HOST.isWeb`) when `features.mic`
is on. With `features.mic` off, the MIC panel is hidden and capture always uses
the browser default.

## Persistence and device loss

The choice is stored per browser in `localStorage` under `pentacle.micSource`
as `{ "source": "device", "deviceId": "<id>" }`. Choosing the room Source
removes the key, because room is the default. A missing, unreadable or invalid
value reads as room. Storage errors are caught; the panel then behaves as a
fresh browser.

Browsers hide device ids and labels until microphone permission is granted.
Before that, inputs are listed as disabled `Microphone n` placeholders, and
**System default** still records. After the first capture the list is
re-enumerated, so real labels appear.

The chosen input is treated as lost, and the Source falls back to room, when:

- a `devicechange` re-enumeration with real ids no longer lists it,
- no input remains, or
- `getUserMedia` rejects it with `OverconstrainedError` or `NotFoundError`.

On fallback the stored key is cleared and an inline note says
`The selected microphone is no longer available. Switched to <room label>.`
Selecting **This device** or an input clears the note. A permission or busy error does not
trigger fallback. An enumeration without ids (before permission) never counts
as loss, so a stored choice survives a reload.

## Implementation map

- `renderer/mic-state.js`: `computeMicSourceState`, `micSourceStorage`
  and `micRoomSourceLabel` (pure)
- `renderer/web_voice.js`: `audioConstraint` and the
  `createBrowserRecorder(env, { getDeviceId, onDeviceFailure, onCaptureStart })`
  hooks
- `renderer/app.js` (`Mic Source (web)`): rendering, enumeration, the
  `devicechange` listener, fallback, and one recorder per composer and per
  question voice bar using those hooks
- `renderer/index.html` (`#mic-source`, `#mic-source-note`,
  `#mic-local-section`) and `renderer/styles.css`

Tests: `test/mic_source.test.js`. The browser scenario is
`test/e2e/lib/mic_source_scenario.js`, registered as `mic-source-selector` in
`test/e2e/lib/web_scenarios.js`. `web_gate.js` gives that one scenario a
service-less mic profile through `configureMic`: `features.mic` on, an unused
loopback `micServerUrl`, a host name for the label, and no `mic.startCommand`.
The scenario restores the default profile afterwards. Only `enumerateDevices`
and `getUserMedia` are faked.

## Out of scope

Hands-free wake-word listening or voice conversation on the browser's machine
is not part of this control. It would need a mic service on that machine, or
audio streaming to the room service, plus a local speaker path. It is a
separate follow-up idea, not a planned change.
