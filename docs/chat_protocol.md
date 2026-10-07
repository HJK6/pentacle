# Pentacle chat-stream protocol

This document is a public compatibility summary for the websocket service. Frames are UTF-8 JSON objects. Daemon-specific invariants and CLI command semantics belong to their respective READMEs.

## Transport and correlation

Every request has a string `type`; clients should include a unique `request_id`. The first non-streaming reply copies that id when the handler did not set it. Streaming replies carry the id on every frame. Replies may arrive out of order.

Malformed input produces a transport error:

| Input | Response |
|---|---|
| invalid JSON | `protocol.error` with `error_code: "bad_json"` |
| JSON that is not an object | `protocol.error` with `error_code: "bad_envelope"` |
| unknown `type` | `<type>.error` with `error_code: "unsupported"` |

Handler failures retain the requested verb. Expected failures use stable error codes; unexpected failures use `internal_error`.

## Handshake

The server sends `welcome` first:

```json
{
  "type": "welcome",
  "runtime_sha": "<checkout-sha-or-empty>",
  "auth_required": false,
  "auth": {"protocol_version": 2}
}
```

The client then sends `hello`:

```json
{
  "type": "hello",
  "client": "example-client",
  "build_sha": "<optional>",
  "subscribe": {"include_subagents": true, "events_mode": "summary"}
}
```

The optional subscription controls visibility and event detail. Invalid event modes fall back to `full`; `snapshot: false` requests RPC-only mode. A successful handshake returns a typed `hello` result and, when requested, a snapshot of the caller's visible sessions.

## Authentication boundaries

Authentication is optional for a loopback fixture daemon. A deployment that enables operator proof supplies a challenge nonce and expects an HMAC proof bound to that nonce, the credential id, and the client kind. Use placeholders in tests; never commit a proof key or an enrollment value.

Session ownership tokens are separate from operator credentials. A caller stream may include a short-lived token supplied through its process environment or a protected local file. The server derives authorization context from the verified token and ignores client-supplied private fields.

## Public request families

The public service may expose the following generic families:

| Family | Purpose |
|---|---|
| `ping`, `hello` | connection and capability checks |
| `list_sessions`, `inspect_stream` | read visible session state |
| `spawn`, `send`, `close`, `rename` | session lifecycle and input |
| `send_image` | attach an already-uploaded image to the caller's OWN conversation as an agent-authored transcript event |
| `subscribe`, `unsubscribe` | event visibility controls |
| `watch`, `wake` | optional local notifications |
| `household.*` | operator-only adapter to the Cosmo household store (see below) |

Each implementation must document the exact fields and error codes it registers. Unsupported legacy verbs return a typed error rather than silently changing behavior.

Image attachments (both operator→agent and agent→operator) reuse one content-addressed blob path: the client uploads bytes with the chunked `upload_blob_init` / `upload_blob_chunk` verbs, then references the blob by its sha256 in an attachment descriptor `{key, mime, bytes, width?, height?}` (supported mime: `image/jpeg`, `image/png`; per-attachment and per-message size/count limits apply). `send_image` is the agent-authored form: the daemon authorizes the destination from the caller's verified stream token — a seat may attach only to its OWN conversation — confirms the referenced blob is present, and emits exactly one agent-authored transcript event carrying the attachment (no pane injection). It is idempotent by `request_id`, so a retry adds no second transcript row. Clients fetch the bytes for display through the existing blob-read path and render the same image bubble/viewer regardless of author. Agent-side usage: `agent-orch send-image` (see the agent-orch README "Send an image").

## Household (Cosmo) adapter

`services/chat-stream-v2/household.py` lets an operator-authenticated client (Pentacle mobile's
Personal tab) read and change the operator's lists and calendar in Cosmo, the one authoritative
household store. The daemon keeps no household state, cache or broadcast.

- **Who may call.** Only the human operator: a connection whose server-injected `_auth_context`
  is operator-authenticated with an `operator:<credential>` principal from operator-trusted
  transport. A seat token elevated by the opt-in `PENTACLE_SEAT_OPERATOR_AUTHORITY` mode
  (`operator_authority_source: stream_token`) is still a seat and is refused. No Cosmo call is made
  for a refused caller. Denial codes: the dispatcher answers remote unauthenticated callers
  `authentication_required`, Dot principals `dot_scope_denied` and scoped (Cosmo) credentials
  `scope_denied` before the handler runs; every other non-operator caller (loopback without operator
  trust, seats, Nexus seats, service producers, seat-authority-elevated seats) gets `unauthorized`.
- **Credential.** Cosmo's optional `pentacle` role (acts for the operator; `created_by=app`; cannot set
  priority or due dates). The bearer token is read at call time from `COSMO_PENTACLE_TOKEN_FILE`
  (default `~/.cosmo/pentacle.token`, mode 0600) and used only in the `Authorization` header.
  Cosmo URL: `PENTACLE_COSMO_URL` (required, no default); it must be `https://`, otherwise
  every verb answers `unavailable` without sending anything. Cosmo enforces
  visibility (the operator's private rows + shared rows); the adapter never sends `scope`, so new rows
  are private to the operator.
- **Neutral vocabulary.** No household member's name or assistant identity crosses the wire. Rows are
  translated viewer-relatively: `who` `self` / `partner` / `both`; `scope` `private` / `shared`;
  `created_by` `assistant` (the operator's assistant), `app`, or `partner_assistant`. The operator's
  Cosmo person value comes from `PENTACLE_COSMO_SELF` (required) and the snapshot's
  `people.partner` display name from `PENTACLE_HOUSEHOLD_PARTNER_NAME` (default `Partner`, max 40).
- **Verbs** (each accepts exactly the listed fields; anything else is `invalid_request`):

| Verb | Fields | Result |
|---|---|---|
| `household.snapshot` | `month?` (`YYYY-MM`, 2000-01…2100-12, default today's America/Chicago month) | `{today, month, people:{partner}, lists:{tasks,grocery,meals,chores,study}, events, server_now}`; open items only; events from that month plus today…today+7, de-duplicated |
| `household.item.add` | `list`, `label` (1–1000 chars) | `{item}` |
| `household.item.done` | `item_id` | `{item}` (Cosmo keeps it 5 s, then removes it) |
| `household.item.remove` | `item_id` | `{item_id}` (Cosmo's 204 carries no `server_now`) |
| `household.event.add` | `date` (`YYYY-MM-DD`), `time` (`HH:MM` or null), `title` (1–500), `who` (`self`, `partner`, `both`; display only) | `{event}` |
| `household.event.remove` | `event_id` | `{event_id}` (no `server_now`, as above) |

- **Errors** (`error_code`): `unauthorized` (or the dispatcher codes above); `invalid_request`; `invalid_range` (bad `month`);
  `not_found` (Cosmo 404/410, including rows outside the operator's audience); `forbidden` (403);
  `unavailable` (token file missing, URL or `PENTACLE_COSMO_SELF` unset, URL not HTTPS, connection refused, Cosmo 401, any snapshot sub-call failing,
  timing out or exceeding 1 MiB); `unknown_outcome` (a change was sent but not confirmed: timeout or
  connection lost while waiting, or an unexpected status). Calls are never retried; after
  `unknown_outcome` the client reads back with `household.snapshot` instead of resending.
- **Bounds.** Seven concurrent GETs per snapshot, 5 s per call, 8 s overall, all-or-nothing.

## Session interrupt

`send.interrupt` names a session with `host` and `session_name`. For a configured
remote host, updated web and desktop clients also send the selected open row's
`expected_session_generation`. They capture it when the user requests Stop and
keep that same value across asynchronous dispatch and retry. The daemon never
fills in a missing value from the current row: a remote request without it
returns `send.interrupt.error` with `error_code: "generation_required"` and
sends no key. Existing local requests may omit the field.

The daemon checks that the remote row is still open at that generation, the SSH
tmux pane matches its session name and PID, and the row and exact pane identity
still match immediately before sending one Escape to the checked pane ID.
Unknown or unreachable hosts, stale rows, and changed pane identity return
typed errors without sending a key. An absent pane returns
`confirm: "pane_unavailable"`; a successful key send returns
`confirm: "interrupt_unconfirmed"`, since key delivery alone does not prove the
provider stopped. Installed mobile clients without generation propagation
receive the typed remote refusal until their separate client release. The
[daemon README](../services/chat-stream-v2/README.md#remote-session-interrupt)
describes the implementation and live validation boundary.

## Close and open inventories

Once a close is durably recorded it also leaves every open inventory, even if
the requesting connection drops mid-request (a self-close kills its own
caller): the daemon finishes the inventory update before honouring the
cancellation. A close that finds the generation it targets already closed, or
archived out of the live table, removes that exact generation from open
inventories before replying `close.already_closed`; a reopened generation is
never removed by a close aimed at its predecessor. A deferred or refused close
leaves the row open and says so in its reply.

## Close on an offline host

`close` accepts the target as `stream_id` or `host` plus `session_name`.
Normal close authorization still applies: a verified self, direct parent, or
authenticated operator may close the row. `operator_confirm: true` records an
explicitly authorized offline close when the initial host probe fails; the flag
does not grant authority or bypass generation, visibility, or idle guards.
`force` alone does not authorize an offline close.

The successful reply is `close.ok` with `reap_status: "deferred_host_offline"`.
The row leaves open inventories immediately, with `closed_at`,
`dead_open_closed_at`, and `close_kind: "operator_offline_close"`. This is a record
of operator intent; pane death remains unknown. The daemon atomically writes
the close, caller audit, and durable deferred-reap record, and surfaces an Updates
notification naming the caller, host, and request time.

Without confirmation, the failed probe still produces `close.failed` with
`reason: "ssh_unreachable"` and leaves the row open. A transport failure after
the initial successful probe also retains the existing close-failure behavior.
Repeated close of a closed row returns `close.already_closed` without resetting
the deferred intent or its retry count.

When the peer becomes reachable, reconciliation attempts pane cleanup before
spawn adoption. Only a `gone` readback completes the deferred record. Up to five
reachable attempts are made; offline passes do not consume the budget. Pending
and exhausted intents prevent reopening or adopting that stream. Pane identity
changes are refused and retained for inspection. `inspect_stream.ok` includes
the session's close kind, `close_audit`, and `deferred_reap` (or null).

`agent-orch close --operator-confirm remote-peer:v2-example` sends the confirmation
and prints the reply including `reap_status`. `agent-orch inspect` shows the
close kind and deferred state; `--json` retains the complete record.

`send` replies with `send.result` (`delivery`, `submission_confirmed`, receipt identifiers). When the target provider holds the prompt in its native queue, the result also carries `provider_queued: true`; see [send receipt surfacing](send_receipt_surfacing_governance.md#provider-native-queue-provider_queued) for caption rules.

## Event handling

Claude transcript normalization maps a non-empty `thinking` content block to `ASSIST_TEXT` (with `raw.claude_block_type: "thinking"`): Claude Code stores model reasoning signature-only and prints a thinking block that carries text as an ordinary assistant paragraph. An empty block remains a `THINKING` placeholder.

Clients should treat snapshots as authoritative for the keys they contain and apply later events in sequence order. Unknown event types are safely ignored after logging a bounded diagnostic. A reconnect should create a new request correlation scope and reconcile optimistic UI rows from the snapshot before replaying only requests that the implementation marks retry-safe.

## Per-session context tracking fields

A session snapshot may carry four context-usage fields, projected by the server and rendered only when present: `context_tokens` (current context use), `model_context_window` (the provider-reported window), `context_updated_at` (the observation timestamp), and `context_level` (`none`, `advisory`, or `compact`). Clients display the token count and window percentage whenever `context_tokens` is present, independent of `context_level`; the numeric badge remains visible at compact level.

`context_level` is provider-aware. For Claude, capped advisory/compact thresholds default to 70% / 85% of the model window, capped at 400,000 / 500,000 tokens. `PENTACLE_CONTEXT_ADVISORY_ABS`/`_PCT` and `PENTACLE_CONTEXT_COMPACT_ABS`/`_PCT` override them. The advisory is one notice per crossing episode. A fresh compact reading can trigger a daemon-owned `/compact` only for an idle seat with an empty composer and durable submission proof; ambiguous input is fenced without automatic repaste. `PENTACLE_CONTEXT_COMPACT_ENABLED=0` disables the input action while leaving advisory active. For Codex, including the configured assistant backend, tokens/window remain visible but `context_level` is always `none`, and a Codex parent receives no forwarded Claude-child context notice. Deliberate `spawn --handoff`, scheduled handoff and recovery handoff remain available independently of `context_level`.

This contract intentionally uses synthetic client, host, and stream examples. It does not describe a private fleet, managed endpoint, deployment channel, or credential location.

## Voice transcription

The authenticated v2 command `transcribe_blob` takes `request_id`, `blob_sha`
from the existing upload protocol and `mime` (`audio/mp4` or `audio/wav`). It
reads the stored blob and calls the managed loopback mic backend's
`POST /transcribe?prompt_profile=fleet`. The daemon never loads an ASR model.
A disabled mic feature or unreachable backend returns `backend_unavailable`.

Success: `transcribe_blob.ok {request_id, text, duration_s, model,
vocabulary_version}`. Error: `transcribe_blob.error {request_id, error_code}`,
where error_code is `bad_request` (missing identity, malformed SHA, or request identity rebound to another take), `blob_unknown`, `mime_unsupported`, `backend_unavailable`,
`transcribe_failed` or `too_long`. Request identities bind the blob SHA and MIME for the ten-minute in-process
cache window; rebinding an identity is rejected. Successful results are cached
by blob SHA and concurrent calls for identical content are coalesced. Each
request also retains its successful result for ten minutes from completion. Cache and
identity bindings reset on daemon restart. An interrupted
socket can replay the same transcription identity; explicit client retries
must keep that identity. An empty transcript is not a text send.

The subsequent `send`, including sends to assistant composites, accepts optional
`meta: {voice: {duration_s: number}}`. Metadata is stored in the send receipt's
`meta_json` on each append-only receipt state row, attached to the USER
echo/history event and preserved by receipt replay. It is additive: clients without voice UI can ignore it. The send
contains the transcript as text, with no audio attachment; optimistic_id and
request_id retain the ordinary send deduplication contract.
Ordinary send outcomes retain the validated metadata from their send plan. A
replay and receipt/USER projection use the first receipt row for that exact
request and stream, sanitized through the ordinary-send whitelist. This also
recovers metadata from older accepted→landed histories whose landed row is
empty. An originally empty metadata value stays empty; a rotated request uses
its own validated metadata when delivery coalesces.
Receipt history remains immutable, and identical transcript text in another
request or stream cannot replace the metadata of a request-stamped USER event.

Composite sends whitelist only finite numeric `voice.duration_s` with
`0 < duration_s <= 600`, normalized to milliseconds (a value that rounds to 0 is dropped). Other metadata is dropped;
the receipt retains its internal `assistant_composite` marker. Ordinary-stream
metadata validation is unchanged. Voice metadata does not alter send identity.

### Web composer recording

In web mode, open the experimental Chat view and tap **Record voice message**
in the chat composer to start a take in the viewer's browser. See the
[web setup](../README.md#local-setup) for enabling Chat UI. The implementation
is [the browser recorder and take lifecycle](../renderer/web_voice.js), mounted
by [the chat composer](../renderer/app.js), with voice styles in
[chat_v3.css](../renderer/chat_v3.css).

While recording:

- The composer capsule gains a green tint. Attachment, draft and text-send
  controls are hidden, preserving their current contents for when recording ends
- A Discard X, blinking dot and tabular `m:ss` timer sit beside the live waveform
- Web Audio samples the same captured stream every 90 ms. The strip retains the
  latest 46 bars; the complete level series is retained for the pending waveform
- Live bars are 2.5 px wide with 2 px gaps in a right-aligned 26 px area. A clamped
  level `l` gives height `max(1, round(l * 26))` and opacity `0.45 + 0.55 * l`.
  New bars grow from scaleY 0.2 over 180 ms; existing bars keep their nodes
- Tap the 40 px green **Stop and send** circle, with an outline mic glyph and
  two 1.4 s pulse rings offset by 0.7 s, to finish. The dot uses a 1 s stepped
  blink. As a web-only deviation from mobile, `prefers-reduced-motion`
  disables rings and bar, dot and spinner animation

The meter normalizes RMS audio levels from −60..0 dBFS to 0..1; it does not
perform speech recognition. There is no live, partial or streaming
transcription while recording. A take stops automatically at five minutes.
Only one capture can be active across the page's chat slots at a time.

Stopping immediately restores the composer controls and shows a right-aligned
pending row in the transcript area. A muted ring spinner, mono 9 px
`TRANSCRIBING` label (1 px letter spacing) and Discard X form the caption
**above** the voice bubble. The bubble contains a decorative play glyph, 30
peak bins from the full recording's levels, and its `m:ss` duration. The glyph
is not a playback control. Pending bars are 2.5 px wide with 2 px gaps, height
`max(2, round(l * 22))` and opacity `0.4 + 0.6 * l`; a take without samples uses
30 fallback levels of 0.2. The pending spinner respects reduced motion too.

The path remains **upload_blob → transcribe_blob → ordinary send**. The browser
uses `chat-stream:upload-blob`, then the authenticated
`chat-stream:transcribe-blob` bridge to the daemon's existing `transcribe_blob`
verb. It does not introduce a second backend. A nonempty trimmed transcript
replaces the pending row with ordinary optimistic text carrying
`meta.voice.duration_s`; no audio attachment is sent to the agent.

The [shared transcript renderer](../renderer/src/shared_transcript_view.ts)
shows a green mic glyph and green mono 9 px duration beneath the sent text.
It reads validated `item.voice` metadata for optimistic, echoed and historical
user rows, so the caption is reconstructed after reload when the daemon returns
top-level `meta.voice` on history events. Post-reload validation against the
real daemon remains a fleet-owned check after rollout. Message identity, rather than matching transcript text, governs
reconciliation; equal text from a different message does not inherit a caption.

Failures and interruptions keep the existing bounded take lifecycle:

- Upload/transcription failure retains the pending bubble and an amber error
  with **RETRY**. Retry keeps the audio Blob and transcription request identity;
  an already-uploaded blob is reused. Duplicate Retry clicks cannot dispatch
  the same take twice. An empty transcript shows “Nothing was recognized.”
  and sends no message
- Discard removes the pending take and suppresses late upload/transcription
  completion. Cancelling while permission or recorder stop is pending also
  releases the late capture before another slot can acquire it
- Hiding the tab or ending a media track stops through the same lifecycle and
  labels the pending row `interrupted at m:ss`. The captured portion can still
  transcribe and send unless discarded. Cleanup-generated track endings do not
  trigger another stop/send
- The destination is captured when recording starts; another chat cannot
  redirect the take. Closing the originating slot cancels its capture or
  pending transcription
- Once text is dispatched, the ordinary optimistic send store owns delivery,
  reconnect reconciliation and text Retry. Audio is released; text Retry keeps
  `meta.voice.duration_s` without another upload or transcription

Capture uses `audio/mp4` when MediaRecorder supports it, otherwise mono PCM WAV
through Web Audio, reduced to at most 16 kHz to fit the daemon's 16 MiB audio
cap. Both paths use the same Web Audio analyser and release media tracks and
the audio context on stop, discard or capture failure. Microphone capture needs
a secure context and `getUserMedia`; unsupported contexts show a disabled button
with an explanation. Missing audio/metering capability or permission denial
shows a retryable error; change the browser permission before retrying a denial.

The chat button never toggles the host room microphone. The
[separate room-mic path](desktop_config.md#per-chat-voice-messages) and Electron's
existing room-mic binding are unchanged. See
[web voice validation](E2E_HARNESS.md#web-voice-composer) for automated coverage
and the distinction between fixture, browser and live release evidence.
