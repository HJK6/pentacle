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

Each implementation must document the exact fields and error codes it registers. Unsupported legacy verbs return a typed error rather than silently changing behavior.

Image attachments (both operator→agent and agent→operator) reuse one content-addressed blob path: the client uploads bytes with the chunked `upload_blob_init` / `upload_blob_chunk` verbs, then references the blob by its sha256 in an attachment descriptor `{key, mime, bytes, width?, height?}` (supported mime: `image/jpeg`, `image/png`; per-attachment and per-message size/count limits apply). `send_image` is the agent-authored form: the daemon authorizes the destination from the caller's verified stream token — a seat may attach only to its OWN conversation — confirms the referenced blob is present, and emits exactly one agent-authored transcript event carrying the attachment (no pane injection). It is idempotent by `request_id`, so a retry adds no second transcript row. Clients fetch the bytes for display through the existing blob-read path and render the same image bubble/viewer regardless of author. Agent-side usage: `agent-orch send-image` (see the agent-orch README "Send an image").

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

## Event handling

Clients should treat snapshots as authoritative for the keys they contain and apply later events in sequence order. Unknown event types are safely ignored after logging a bounded diagnostic. A reconnect should create a new request correlation scope and reconcile optimistic UI rows from the snapshot before replaying only requests that the implementation marks retry-safe.

## Per-session context tracking fields

A session snapshot may carry four context-usage fields, projected by the server and rendered only when present: `context_tokens` (current context use), `model_context_window` (the provider-reported window), `context_updated_at` (the observation timestamp), and `context_level` (`none`, `advisory`, or `handoff`). Clients display the token count and window percentage whenever `context_tokens` is present, independent of `context_level`; the level only adds advisory/handoff styling.

`context_level` is provider-aware. For a Claude session the server classifies the level from the model window against capped advisory/handoff thresholds (defaults 70% / 85%, capped at 400,000 / 600,000 tokens; overridable via `PENTACLE_CONTEXT_ADVISORY_ABS`/`PENTACLE_CONTEXT_ADVISORY_PCT` and `PENTACLE_CONTEXT_HANDOFF_ABS`/`PENTACLE_CONTEXT_HANDOFF_PCT`) and emits a one-and-done context notification at each crossing. For a Codex session the server reports `context_tokens` and `model_context_window` for display but `context_level` is always `none`: Codex compacts its context automatically, so it receives no routine context-threshold handoff or advisory notification and no handoff-level status-card pressure. This is a notification-and-display policy only; it does not affect deliberate `spawn --handoff`, scheduled handoff, or recovery handoff, which remain available to both providers and independent of `context_level`.

This contract intentionally uses synthetic client, host, and stream examples. It does not describe a private fleet, managed endpoint, deployment channel, or credential location.

## Mobile voice transcription

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

The subsequent ordinary `send` accepts optional
`meta: {voice: {duration_s: number}}`. Metadata is stored in the send receipt's
`meta_json`, attached to the USER echo/history event and preserved by receipt
replay. It is additive: clients without voice UI can ignore it. The send
contains the transcript as text, with no audio attachment; optimistic_id and
request_id retain the ordinary send deduplication contract.
