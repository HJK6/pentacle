# Daily brief service tells

The daily brief uses a dedicated file credential to tell the assistant composite
and one configured ops address. It does not need an open producing agent seat.
This credential permits only RPC `hello` and the bounded `tell` below. It cannot
publish assets, create notifications, read inventories, spawn, or invoke any other
verb. Existing operator and agent permissions are unchanged.

## Private configuration

Set `PENTACLE_WMI_BRIEF_PRODUCER_CONFIG` to the path of an owner-only JSON file
(at most 16 KiB). Configure exactly these keys, using deployment-specific values:

```json
{
  "stream_id": "examplehost:wmi-daily-brief",
  "token_file": "/private/config/brief-token",
  "composite_stream_id": "desk:assistant",
  "ops_stream_id": "examplehost:ops-lead"
}
```

The principal's session name is fixed to `wmi-daily-brief`; only the full identity
in this file authenticates. The config and token must be regular files owned by
the daemon user, with no group/world permissions (normally mode 0600). Keep both
outside the repository. The token must differ from every other service token;
the report producer cannot also claim this identity. This is one dedicated
producer binding, with no configurable verbs, limits or capabilities.

All three stream IDs must be distinct. The composite must be an enabled assistant
composite, and the ops address must be an ordinary seat address. Tells use the
existing logical ops route, including its normal handoff handling. Update both
sender and daemon configuration when replacing the ops address. A closed prior
lead is not a suitable new deployment destination.

The daemon rechecks the config and credential on every RPC, including bound
connections that omit a token. Removing the file, changing the token, or changing
the binding refuses old sockets. Reconnect with the current credential after a
valid configuration change. Missing, unsafe or invalid configuration disables the
principal; the reserved session name cannot fall through to loopback/seat trust.

## Wire contract

First send an RPC-only hello:

```json
{
  "type": "hello", "request_id": "brief-hello",
  "from_stream_id": "examplehost:wmi-daily-brief", "stream_token": "<token>",
  "subscribe": {"snapshot": false, "mode": "rpc"}
}
```

After `ready`, send exactly these fields:

```json
{
  "type": "tell", "request_id": "brief-request", "tell_id": "brief-fd-cutoff",
  "from_stream_id": "examplehost:wmi-daily-brief", "stream_token": "<token>",
  "to_stream_id": "desk:assistant", "text": "GATE Daily brief ...",
  "ttl_seconds": 3600, "sanitize": true
}
```

- `to_stream_id` is exactly the configured composite or ops address. Raw pane
  targets and other composites are refused.
- `text` starts literal `GATE ` with nonblank content after it, and is at most
  131072 UTF-8 bytes before sanitization. Complete briefs are retained within that
  limit; nothing is truncated. A retry may start
  `GATE RESEND (first tell to FD not delivered) — ` before the original brief.
- `request_id` and `tell_id` contain 1–120 printable ASCII characters without
  whitespace. Use a stable tell ID per logical message and destination.
- `sanitize` must be `true`; control sequences are stripped before persistence or
  delivery. Extra fields, internal auth/routing fields and urgent overrides are
  refused. Each tell includes its file token even on an authenticated connection.
- `ttl_seconds` must be the integer 3600. It is compatibility metadata: the
  existing tell and composite queue do **not** enforce expiry.
- At most six validated attempts are admitted in a rolling 60-second window per
  daemon, including retries. Connections share the limit. The limiter resets on
  daemon restart; it is not a fleet-wide or durable quota.

Authentication failures return `system_producer_auth_required`; disallowed verbs
return `system_producer_forbidden`; invalid envelopes or unavailable composite
routes return `system_producer_payload_invalid`; excess attempts return
`system_producer_rate_limited`. Other routing/delivery failures retain their
existing typed errors. Refused admission does not queue or deliver a tell.

The daemon derives `from_stream_id` from the authenticated principal and stamps
`[from <principal>] [tell:<id>]` ahead of the sanitized body. The delivery ledger
and durable unbound queue retain this sender. The stamp survives restart and
queue flush; service credentials are never treated as seat credentials.

## Acknowledgement and release

`tell.ok` describes the actual delivery result. `queued_unbound` with `queued:true`
means the assistant queue persisted the message for a later binding; it does not
claim submission. A committed result with `action_committed:true` and
`do_not_resubmit:true` must not be blindly resent while submission proof is
pending. The composite route preserves these acknowledgement fields. Use the
existing delivery receipt/reconciliation path for uncertainty. Stable-ID retries
reuse existing tell/queue behavior; this feature adds no exactly-once or expiry
protocol. Ops acknowledgement is independent of the front desk acknowledgement.

Source validation covers synthetic private configuration, the real dispatcher,
Comms, SQLite queue/ledger and fake pane transport in
`services/chat-stream-v2/tests/test_wmi_brief_tell_principal.py`. Run the v2 unit
gate with the checked-in runner; its smoke preflight is a separate release check.
Before deployment, the release owner must pass the applicable merge gate, review
the private bindings and credentials, retain a rollback artifact, and verify the
configured sender's bound FD/ops delivery, unbound recovery and revocation in an
authorized runtime window. Source acceptance does not activate a schedule.
