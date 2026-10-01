# The external scoped ("Dot") principal

Dot is an outside agent (the operator's cloud agent on another host) that needs
to (1) see fleet state read-only and (2) message **only** the current assistant
("Bart") binding. It is a least-privilege, revocable principal whose scope the
**daemon** enforces — a cooperating client is never trusted to self-limit.

## Identity and revocation

A Dot principal is an **ordinary per-RPC-revalidated seat**, not a new credential
type. Its stream id is listed in `PENTACLE_DOT_PRINCIPAL_STREAM_IDS` (comma
separated). On every RPC the daemon revalidates the seat token against the durable
store (`stream_token_state`), so closing/revoking the seat drops a **live**
connection on its next request. Rotation is a new token grant. There is no bespoke
registry: `_auth_context` sets `dot_principal=True` once a verified seat id is in
the configured set.

## Transport: daemon-terminated TLS (`transport_tls`)

A Dot token is honoured **only over a TLS connection the daemon itself
terminates**. When `PENTACLE_DOT_TLS_CERT`/`PENTACLE_DOT_TLS_KEY` are configured,
the daemon binds a second `websockets.serve` listener with its own `SSLContext` on
`PENTACLE_DOT_TLS_PORT` (binds default to the main interfaces; override with
`PENTACLE_DOT_TLS_BINDS`). Connections accepted there — and only there — are
flagged `transport_tls`. The dispatch gate refuses a Dot principal that is not on
such a connection with `external_requires_tls`, so the token cannot be used over
plain ws on any port, and a plain-ws client cannot even complete a handshake
against the TLS port. The existing plain-ws binds (the fleet transport) and any
Tailscale Serve routes are untouched; a TLS bind failure is logged and does not
take the plain listener down.

This is deliberately **not** a Serve→plain-loopback design: Serve terminates TLS
and speaks plain HTTP to the daemon, so the daemon could not distinguish a
TLS-fronted client from a local plain-ws client, and the "refused over plain ws"
property would not hold. Terminating TLS in the daemon makes it a real,
non-spoofable boundary.

## Read scope (default-deny, metadata only)

A Dot principal reaches only `ping`, `hello`, `list_sessions`, and `send`
(`DOT_ALLOWED_VERBS`); every other registered verb returns `dot_scope_denied`,
including `inspect_stream` and `request_stream_events` (which expose
events/report/audit). `list_sessions` is projected to `DOT_LIST_FIELDS`:

- **metadata** (`DOT_METADATA_FIELDS`): ids, host, role/phase, spec ids,
  working/idle + turn state, parent/handoff lineage, online, provider/model, last
  event time and kind, generation, bootstrap/host status.
- **free text** (`DOT_FREETEXT_FIELDS`): `display_name`/title, `objective`,
  `status_card`. These are human-authored and can carry operator content; they are
  the **egress surface** and their disclosure is an operator activation decision.

Content/transcript fields — `last_text`, `draft`, `question`, peer/message bodies,
`usage` internals, `agents`, `assistant_activity` — are always dropped. Broadcasts
to a Dot connection are likewise limited to the projected `session.inventory`, and
nothing is pushed to a Dot connection over plain ws.

## Write scope (Bart-only, attributed)

A Dot `send` may target only the assistant composite stream. The daemon resolves
the composite's **current** backing seat at send time (never a hardcoded seat) and
delivers the message as an ordinary attributed peer `send` carrying the Dot's own
`from_stream_id`, so the existing `[from <dot>]` envelope and token-verified
provenance apply. It is **not** operator intake: Bart receives an attributed
external handoff and applies its normal grant rules (and gates production actions
as usual). Any other send target is `dot_scope_denied`.

## Wiring a Dot client

The `agent-orch` client already supports a `wss://` endpoint (it passes
`AGENT_ORCH_WS_URL` straight to `websockets.connect`, which negotiates TLS for a
`wss://` URL). Point the Dot host's `AGENT_ORCH_WS_URL` at the daemon's TLS
endpoint and provide the Dot seat token; no client code change is required.

## Configuration summary

| Env | Meaning |
|---|---|
| `PENTACLE_DOT_PRINCIPAL_STREAM_IDS` | comma-separated seat ids that receive the Dot scope |
| `PENTACLE_DOT_TLS_CERT` / `PENTACLE_DOT_TLS_KEY` | cert/key enabling the daemon TLS listener |
| `PENTACLE_DOT_TLS_PORT` | TLS listener port (0 picks a free port; production pins one) |
| `PENTACLE_DOT_TLS_BINDS` | comma-separated TLS bind interfaces (default: main binds) |
