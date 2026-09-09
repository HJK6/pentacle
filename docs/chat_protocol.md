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
| `subscribe`, `unsubscribe` | event visibility controls |
| `watch`, `wake` | optional local notifications |

Each implementation must document the exact fields and error codes it registers. Unsupported legacy verbs return a typed error rather than silently changing behavior.

## Event handling

Clients should treat snapshots as authoritative for the keys they contain and apply later events in sequence order. Unknown event types are safely ignored after logging a bounded diagnostic. A reconnect should create a new request correlation scope and reconcile optimistic UI rows from the snapshot before replaying only requests that the implementation marks retry-safe.

This contract intentionally uses synthetic client, host, and stream examples. It does not describe a private fleet, managed endpoint, deployment channel, or credential location.
