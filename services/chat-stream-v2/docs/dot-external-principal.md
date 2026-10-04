# The external scoped ("Dot") principal

Dot is an outside agent (the operator's cloud agent on another host). In **v1**
(operator b3e1c657, parent ruling f4e6c3cf) it may do exactly one useful thing:
message **only** the current assistant ("Bart") binding as an attributed handoff.
Every fleet **read** path — the `list_sessions` RPC, the hello snapshot, and
broadcast/subscription frames — is **denied by default**; the server, not a
cooperating client, enforces this. The read view is retained behind a config
toggle (`PENTACLE_DOT_READ_ENABLED`, default **off**) so it can be re-enabled
later without a rebuild, but turning it on requires a new operator grant.

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

The TLS listener, like the plain listener, also enforces the WebSocket Origin
containment guard (H1) at the opening handshake — any `Origin`/`Sec-Fetch-*`
upgrade is refused before `hello`. The supported Dot client is agent-orch over
`wss://` (below), which sends neither header, so the guard does not affect it.
See [ws-origin-containment.md](ws-origin-containment.md).

## Read scope (v1: denied by default; toggle-gated)

Effective reachable verbs for a Dot principal are `_dot_allowed_verbs`:
`ping`, `hello`, and `send` **always** (`DOT_BASE_ALLOWED_VERBS`), plus the single
projected read verb `list_sessions` **only when `PENTACLE_DOT_READ_ENABLED` is
on** (`DOT_READ_VERBS`). Every other registered verb — including `inspect_stream`,
`request_stream_events`, `send.receipt.get` and the upload verbs — returns
`dot_scope_denied`, proven by a registry-sweep test under both toggle states.

Three read channels are all closed by default:

- **`list_sessions` (RPC):** denied with `dot_scope_denied` unless the toggle is
  on. When on, it is projected to `DOT_LIST_FIELDS` — metadata
  (`DOT_METADATA_FIELDS`: ids, host, role/phase, spec ids, working/turn state,
  lineage, online, provider/model, last event, generation, host status) plus
  title-bearing free text (`DOT_FREETEXT_FIELDS`: `display_name`, `objective`,
  `status_card`, `working_label`) — never content/transcript (`last_text`,
  `draft`, `question`, bodies, `usage`, `agents`, `assistant_activity`).
- **hello response:** a Dot connection's `hello` returns exactly
  `[hello, <empty snapshot>]` — empty `sessions`/`notifications`/`working_states`,
  empty `hosts`, and **no directly-appended `hosts.stats` frame** (that frame
  carries live host telemetry and bypasses the broadcast filter). This is
  **unconditional** (independent of the toggle): Dot never reads the fleet via the
  hello push; when read is enabled it reads via the explicit `list_sessions` RPC.
  The empty-but-well-formed snapshot also keeps the stock `agent-orch` client,
  which blocks waiting for a `snapshot` frame, from hanging.
- **broadcasts/subscriptions:** with read off, `_frame_for_client` returns `None`
  for **every** frame type to a Dot connection (including `session.inventory`), so
  no push read path exists and Bart's reply frames never reach Dot over wss. When
  read is on, only the projected `session.inventory` is delivered; never
  `chat.event`/`working.state`/`report`/`notification`/`schedule`, and nothing
  over plain ws.

## Write scope (Bart-only, attributed)

A Dot `send` may target only the assistant composite stream. The daemon resolves
the composite's **current** backing seat at send time (never a hardcoded seat) and
delivers the message as an ordinary attributed peer `send` carrying the Dot's own
`from_stream_id`, so the existing `[from <dot>]` envelope and token-verified
provenance apply. It is **not** operator intake: Bart receives an attributed
external handoff and applies its normal grant rules (and gates production actions
as usual). Any other send target is `dot_scope_denied`.

The **ack** returned to Dot discloses no fleet data: it echoes only the stable
composite id (e.g. `bart:assistant`) plus safe delivery-status fields — never the
resolved backend seat id, host, or session name (that is live fleet binding
state). The message is still delivered internally to the resolved backend.

## Error frames (code-only for Dot)

Because Dot may call `send`, a failure must not leak backend/internal detail. A
final egress scrub (`_dot_scrub_outbound`) reduces **any** `*.error` frame bound
to a Dot connection to `{type, error_code, request_id}`, stripping the free-form
`error` text and `VerbError` extras. Non-Dot callers are unaffected.

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
| `PENTACLE_DOT_READ_ENABLED` | v1: default **off** → `list_sessions` + inventory broadcast denied; `1`/`true`/`yes`/`on` re-enables the projected read. A restart action; needs a new grant. |
