# Pentacle chat-stream protocol

This document is a public compatibility summary for the websocket service. Frames are UTF-8 JSON objects. Daemon-specific invariants and CLI command semantics belong to their respective READMEs.

## Transport and correlation

Every request has a string `type`; clients should include a unique `request_id`. The first non-streaming reply copies that id when the handler did not set it. Streaming replies carry the id on every frame. Replies may arrive out of order.

Malformed input produces a transport error:

| Input | Response |
|---|---|
| invalid JSON | `protocol.error` with `error_code: "bad_json"` |
| JSON that is not an object | `protocol.error` with `error_code: "bad_envelope"` |
| unknown `type` | `<type>.error` with `error_code: "unsupported_in_v2"` |

Handler failures retain the requested verb. Expected failures use stable error codes; unexpected failures use `internal_error`.

## Protocol versions

| Version | Introduced | What a client must handle | Status |
|---|---|---|---|
| 2 | PR 663, commit `8fa5cf28`, 2026-11-24 | The wire contract described in this guide. | supported |

Earlier daemons spoke version 2 without advertising a protocol version. To add a
version, raise the maximum, add a table row and its contract test, and add a
CHANGELOG line in one PR. To drop one, raise the minimum, mark its row `dropped`,
remove its contract test, and add a CHANGELOG line in one PR.

## Frame sizes and client allowance

Inventory, lane and snapshot frames are complete replacements. Measurements as
of 2026-11-28 show session inventory at about 85–110 KB before the mobile slim
and about 10 KB after it, lane inventory at about 140 KB, and authenticated
hello at about 480 KB before the slim and about 169 KB after it. The post-slim
figures are design samples or projections; read steady-state bytes by frame type
from the nightly soak receipt.

The daemon intends steady-state frames to a phone to stay under 256 KB. This is
not yet enforced; it will be bound once inventory sends changes only. Assuming
a 0.5 Mbps design floor (link capacity has not been measured), clients should
allow at least about 4.2 seconds for a complete 256 KB frame, and longer for the
first snapshot after connecting.

## Handshake

The server sends `welcome` first:

```json
{
  "type": "welcome",
  "runtime_sha": "<checkout-sha-or-empty>",
  "schema_version": 2,
  "protocol_version": 2,
  "min_protocol_version": 2,
  "max_protocol_version": 2,
  "auth_required": false,
  "auth": {"operator": {
    "protocol_version": 2, "scheme": "hmac-sha256-v2",
    "nonce": "<challenge>", "expires_at": 0
  }}
}
```

The nonce is fresh per connection and `expires_at` is a Unix timestamp (shown
as a placeholder above). Do not use the legacy `auth_required` field to decide
whether the endpoint requires proof.

A native loopback client may use the legacy `hello` without operator proof:

```json
{
  "type": "hello",
  "client": "example-client",
  "build_sha": "<optional>",
  "subscribe": {"include_subagents": true, "events_mode": "summary"}
}
```

The optional subscription controls visibility and event detail. Invalid event modes fall back to `full`; `snapshot: false` or `mode: "rpc"`
opts out of the snapshot and the server replies `ready`. With a snapshot
subscription, a successful handshake sends the correlated `hello` and then a
separate `snapshot` of the caller's visible sessions.

## Event backfill

`request_stream_events` returns existing `request_stream_events.chunk` frames
followed by a terminal `request_stream_events.ok`, or one terminal reply for a
small page. The effective `limit` caps the total events across all pages;
`chunk_limit` can reduce the per-page count. The newest eligible events are
returned in newer pages before older pages, ascending by durable `daemon_seq`
within each page. Resume older history with `before_daemon_seq` set to the
smallest sequence received; the cursor is exclusive. Each UTF-8 JSON frame stays
within 900 KiB, including its envelope.

An event too large to fit alone becomes a wire-only text preview ending with
`[Event truncated for backfill; original retained by daemon.]`. Optional payload
members are omitted only when needed to fit; replay identities, routing and
visibility/provenance metadata remain. A peer `TELL` preview uses its authoritative
`raw.peer_payload` and marks both that field and `text`, so existing clients show
the signal. Hidden-event rules still apply. The stored event remains complete;
ordinary events and cursor ordering are unchanged. This bound covers backfill,
not live pushes or `inspect_stream` replies, and assumes valid bounded identity
and request-envelope fields.

## Session message previews

`list_sessions`, summary-mode hello snapshots and `session.inventory` carry
`last_text` and `last_kind` for the latest nonempty conversation message.
Normalized `USER`, `ASSIST_TEXT`, `ASSIST` and `TELL` events qualify; tool,
status, system and sidechain events do not replace it. Registered USER message
envelopes reuse their render policy: only `chat_prose` qualifies. Notice cards
remain in the transcript. `last_event_at` still advances for other durable
activity, so its timestamp can be newer than the preview message.

Visible messages are ordered by event timestamp, then durable `daemon_seq`.
The assistant composite stream's rows reach the store without a provider ingest
event, so `AssistantComposite` advances its registry preview itself after each
committed row: reply publication, work-lane publication, operator input
admission, the mirror/turn-final projection when strict publication is off, the unanswered marker and the authority-unavailable
ruling status line. The composite's `last_text`
therefore tracks its latest visible message without a daemon restart. The live inventory preserves that result across metadata refreshes and clears
it when a session generation changes. Startup reconstructs each open session's
preview from at most its newest 500 lifecycle-matched durable events, using the
same projection as live ingest. Existing chats therefore gain previews without
waiting for another provider event. Empty chats, and chats whose latest visible
message lies beyond that bounded scan, omit the preview fields. Summary clients
receive those fields without a transcript fetch; list polling stays in memory.

The shared chat model uses the summary as a temporary bubble only when matching
transcript content is unavailable locally. It preserves the original Markdown,
line breaks and fenced code for display. Matching uses kind and normalized text,
without comparing the message timestamp to `last_event_at`: later tool or status
activity does not make the preview a new message. A held matching event suppresses
the fallback even outside the visible page. A genuinely repeated message gains
its own row when its transcript event arrives. Returned-to-prompt, optimistic,
image-wrapper, working-state and tool suppression rules still apply.

The web sidebar's one-line row preview comes from `last_text` only; the tmux pane
line in a session's presence `preview` never appears in a row. It is plain text: `sanitizeSidebarDetail`
in `renderer/chat_ui_state.js` drops emphasis, code-span ticks, link syntax and
heading, list, quote and fence markers, keeping the words, and joins lines. The
`sidebar-preview-plain-text` web gate scenario checks it against a seeded
Markdown message. The chat view keeps full Markdown.

The captured synthetic provider-ingestion fixture is
`pentacle-chat-core/tests/fixtures/summary-fallback-frames.json`. Core tests replay
it before and after backfill; `composite-summary-fallback-frames.json` holds real
composite rows (operator input, reply publication, mirror) for the same check
against the composite's now-live `last_text`; the `summary-fallback-formatting` web gate scenario
also records DOM mutations across idle/working inventory updates, checking both
Markdown structure and the absence of a transient duplicate bubble.

## Authentication boundaries

Authentication is optional for a loopback fixture daemon. A deployment that enables operator proof supplies a challenge nonce and expects an HMAC proof bound to that nonce, the credential id, and the client kind. Use placeholders in tests; never commit a proof key or an enrollment value.

Session ownership tokens are separate from operator credentials. A caller stream may include a short-lived token supplied through its process environment or a protected local file. The server derives authorization context from the verified token and ignores client-supplied private fields.

## Independent client walkthrough

Use a disposable daemon and its private credential registry. From the checkout
root, point `FIXTURE_REGISTRY` at the **same disposable registry** the daemon
uses. Set `FIXTURE_KEY_FILE` and `FIXTURE_CREDENTIAL_ID_FILE` to two different,
unused paths in your private fixture directory. The
[issuer](../services/chat-stream-v2/tools/operator_auth_cli.py) returns JSON;
the example's key file must contain only the raw envelope from `code`.

<!-- fixture-credential-issuance -->
```bash
(
  set -euo pipefail
  umask 077
  test "$FIXTURE_KEY_FILE" != "$FIXTURE_CREDENTIAL_ID_FILE"
  test ! -e "$FIXTURE_KEY_FILE" && test ! -L "$FIXTURE_KEY_FILE" || exit 1
  test ! -e "$FIXTURE_CREDENTIAL_ID_FILE" && test ! -L "$FIXTURE_CREDENTIAL_ID_FILE" || exit 1
  python services/chat-stream-v2/tools/operator_auth_cli.py \
    --registry "$FIXTURE_REGISTRY" issue --client-kind pentacle |
    python -c '
import json, sys
issued = json.load(sys.stdin)
with open(sys.argv[2], "x", encoding="utf-8") as receipt:
    receipt.write(issued["credential_id"] + "\n")
with open(sys.argv[1], "x", encoding="utf-8") as key:
    key.write(issued["code"] + "\n")
' "$FIXTURE_KEY_FILE" "$FIXTURE_CREDENTIAL_ID_FILE"
)
```

This creates mode-0600 files without printing the envelope. The separate ID
file is for revocation, not authentication. This is enrollment by an authorized
operator, not public self-registration. When finished, revoke using the saved
ID (only the ID, never the envelope, is passed as an argument):

```sh
python services/chat-stream-v2/tools/operator_auth_cli.py \
  --registry "$FIXTURE_REGISTRY" revoke "$(cat "$FIXTURE_CREDENTIAL_ID_FILE")"
```

See [network access](REMOTE_AUTH.md) for trust boundaries.

1. On every connection, receive `welcome` and answer its `auth.operator`
   challenge before it expires. Send `hello` with a unique `request_id`, the
   envelope's `client_kind` in **`client`**, and `auth_v2` containing exactly
   `scheme`, `credential_id`, and `proof`. Use the
   [existing proof helper](../services/_shared/operator_auth.py), which binds
   the HMAC to the nonce, credential ID and registered kind. The two accepted
   wire labels are `pentacle` and `pentacle-mobile`; “Example Presentation”
   is a local display name, not a wire label or a grant.
2. Use `hello.subscribe` to select the event projection (for example,
   `{"events_mode":"summary","include_subagents":false}`). Inspect
   `snapshot.sessions` or send `list_sessions` and read
   `list_sessions.ok.active`; use the visible row's `stream_id`, not its
   title. Do not dispatch unsolicited pushes as replies. Match `request_id`
   on replies and history chunks; handle a matching `*.error` separately
   and do not display its raw contents in logs.
3. If the daemon advertises `snapshot.capabilities.work_lanes_v1: true`,
   offer `capabilities: {"work_lanes_v1":true}` in hello to receive
   `snapshot.work_lanes` and subsequent `work_lanes.inventory` pushes.
   Read-only RPC example: send
   `{"type":"work_lanes.list","request_id":"lanes-1","limit":20}`;
   read `work_lanes.list.ok.lanes`, keyed by `lane_id`. Lanes require the
   server's lane projection plus an operator or eligible verified seat (or
   trusted native loopback); scoped credentials do not qualify. Hello's
   capability opt-in controls projection delivery, not authorization.
4. Read the newest `request_stream_events` page for the fixture stream, then
   a second one with the exclusive `before_daemon_seq` cursor described
   below. To exercise input, send one `send` with `to_stream_id`, `text`,
   and unique `request_id` and `optimistic_id`. Check its `send.result`,
   including the resolved target and `submission_confirmed`; an accepted or
   pending result is not proof of a durable echo. Observe the durable
   `chat.event.event` for the fixture turn (with `stream_id`, `daemon_seq`,
   `kind` and `timestamp`). Before disconnecting persist
   the greatest observed sequence with its stream and session generation.
5. Reconnect and subscribe first, then fetch history from newest backward
   until it includes the saved anchor. Combine history and buffered live rows
   by `(stream_id, daemon_seq)`, sort by sequence, and keep each identity
   once. Sequences need not be contiguous. If the anchor is no longer
   retained, fail gap-free recovery rather than silently advancing. A new
   subscription alone does not replay what arrived while it was offline.

| Client or change | Compatibility |
|---|---|
| Independent trusted native client or server-side host | Keep the long-lived operator secret here. A valid unscoped credential grants broad operator rights (including operator RPCs and visible inventory), subject to daemon policy; do not give it to arbitrary code or browser JavaScript. On a proof-required endpoint, missing or invalid proof receives `hello.error`, not a protected inventory. A revoked credential cannot authenticate a new socket. |
| Stream-scoped credential | Confined to its configured assistant stream and a small verb allowlist; hello has no fleet inventory, and lane reads and cross-stream history/send are not granted. A seat token is a separate principal, not this operator credential. |
| First-party browser, optional native features | The shipping web host's RPC bridge and server-side credential serve its own UI; the bridge is not this public API. Provider launch, local pane control, OS notifications, device approval keys, and OS audio capture are outside this subset. Keep proof and keys on the trusted host or native side. |
| Old clients and future evolution | Existing projections and defaults are unchanged. Ignore additive fields and unknown unsolicited event kinds, but not mismatched request IDs, errors, or missing required identities. Incompatible changes to this subset require a major release and a migration note in the [release manifest](../releases/manifest.json) and [changelog](../CHANGELOG.md). |

**Run it.** With the service requirements installed, use the standalone
[example](../services/chat-stream-v2/tools/independent_client_example.py)
against an owned loopback fixture daemon. Provision a fresh, non-forwarding
`fixture:v2-client-contract-<32 lowercase hex digits>` stream, its generation,
at least four history events, a provider fixture that echoes the synthetic turn
and a capable lane projection. No other process should replace or forward this
session: the generation comparison is an admission check, not an atomic wire
fence for `send`. Use a private temporary directory and a new checkpoint file.
Set `FIXTURE_URL`, `FIXTURE_KEY_FILE`, `FIXTURE_STREAM`, `FIXTURE_GENERATION`
and `FIXTURE_CHECKPOINT` to the resources your fixture owns:

```sh
probe=services/chat-stream-v2/tools/independent_client_example.py
python "$probe" start --url "$FIXTURE_URL" --credential-file "$FIXTURE_KEY_FILE" \
  --stream "$FIXTURE_STREAM" --generation "$FIXTURE_GENERATION" --checkpoint "$FIXTURE_CHECKPOINT"
# While disconnected, have the fixture append and confirm an event on this stream.
python "$probe" resume --url "$FIXTURE_URL" --credential-file "$FIXTURE_KEY_FILE" \
  --stream "$FIXTURE_STREAM" --generation "$FIXTURE_GENERATION" --checkpoint "$FIXTURE_CHECKPOINT"
```

The example prints counts only and persists identities, not transcript content
or credentials. Do not rerun `start` after an ambiguous send. Revoke and remove
only your fixture's credential, session and files after the run. Public checks
run this same example against the real socket/server and a durable Store with a
synthetic provider. From `services/chat-stream-v2`, run
`python -m pytest -q tests/test_client_contract_probe.py -k independent_client_contract`.
The test checks proof denial and revocation, exact cursor recovery and the
live-only failure control; it does not prove native provider behavior or
deployment access. A trusted-host repeat runs the same fixture and example on
that host, not against its live chats or credentials.

## Public request families

The public service may expose the following generic families:

| Family | Purpose |
|---|---|
| `ping`, `hello` | connection and capability checks |
| `list_sessions`, `inspect_stream` | read visible session state |
| `spawn`, `send`, `close`, `rename` | session lifecycle and input |
| `compact` | authenticated self-only context compaction through ordinary input; see the [CLI contract](../services/agent-orch/README.md#request-context-compaction) |
| `send_image` | attach an already-uploaded image to the caller's OWN conversation as an agent-authored transcript event |
| `hello.subscribe` | event visibility controls on the connection |
| `watch`, `wake` | optional local notifications |
| `household.*` | operator-only adapter to the Cosmo household store (see below) |

Each implementation must document the exact fields and error codes it registers. Unsupported legacy verbs return a typed error rather than silently changing behavior.

Image attachments (both operator→agent and agent→operator) reuse one content-addressed blob path: the client uploads bytes with the chunked `upload_blob_init` / `upload_blob_chunk` verbs, then references the blob by its sha256 in an attachment descriptor `{key, mime, bytes, width?, height?}` (supported mime: `image/jpeg`, `image/png`; per-attachment and per-message size/count limits apply). `send_image` is the agent-authored form: the daemon authorizes the destination from the caller's verified stream token — a seat may attach only to its OWN conversation — confirms the referenced blob is present, and emits exactly one agent-authored transcript event carrying the attachment (no pane injection). It is idempotent by `request_id`, so a retry adds no second transcript row. Clients fetch the bytes for display through the existing blob-read path and render the same image bubble/viewer regardless of author. Agent-side usage: `agent-orch send-image` (see the agent-orch README "Send an image").

Attachment captions in the provider-facing image instruction use NFC Unicode composition so native echoes match their durable send receipts. The original caption and validated image metadata remain in the receipt and projected history. This applies to new image sends; it does not re-stamp old stored events. Attachment paths, appended voice context and ordinary text sends keep their existing bytes and matching behavior.

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
  `created_by` `assistant` (the operator's assistant), `app`, or `partner_assistant`. Cosmo rows whose
  `created_by` equals `PENTACLE_COSMO_ASSISTANT_AUTHOR` (default `assistant`) map to `assistant`. The operator's
  Cosmo person value comes from `PENTACLE_COSMO_SELF` (required) and the snapshot's
  `people.partner` display name from `PENTACLE_HOUSEHOLD_PARTNER_NAME` (default `Partner`, max 40).
- **Verbs** (each accepts exactly the listed fields; anything else is `invalid_request`):

| Verb | Fields | Result |
|---|---|---|
| `household.snapshot` | `month?` (`YYYY-MM`, 2000-01…2100-12, default today's America/Chicago month) | `{today, month, people:{partner}, lists:{tasks,grocery,meals,chores,study}, events, routines, server_now}`; open items only; events from that month plus today…today+7, de-duplicated |
| `household.item.add` | `list`, `label` (1–1000 chars) | `{item, server_now}` |
| `household.item.done` | `item_id` | `{item, server_now}` (Cosmo keeps it 5 s, then removes it) |
| `household.item.remove` | `item_id` | `{item_id}` (Cosmo's 204 carries no `server_now`) |
| `household.event.add` | `date` (`YYYY-MM-DD`), `time` (`HH:MM` or null), `title` (1–500), `who` (`self`, `partner`, `both`; display only) | `{event, server_now}` |
| `household.event.remove` | `event_id` | `{event_id}` (no `server_now`, as above) |

- **Errors** (`error_code`): `unauthorized` (or the dispatcher codes above); `invalid_request`; `invalid_range` (bad `month`);
  `not_found` (Cosmo 404/410, including rows outside the operator's audience); `forbidden` (403);
  `unavailable` (token file missing, unreadable or empty, URL or `PENTACLE_COSMO_SELF` unset, URL not HTTPS, a connect-time failure before the request was sent such as connection refused or name resolution, Cosmo 401, any snapshot sub-call failing,
  timing out or exceeding 1 MiB); `unknown_outcome` (a change was sent but not confirmed: timeout or
  connection lost while waiting, or an unexpected status). Calls are never retried; after
  `unknown_outcome` the client reads back with `household.snapshot` instead of resending.
- **Routines.** Additive `routines` rows come from `GET /routines` with the same neutral scope/author mapping; the read is required for a successful refresh. They carry the known `next_due`, `paused` and `item_id`, not a forecast series. Older clients ignore this field.
- **Bounds.** Eight concurrent GETs per snapshot, 5 s per call, 8 s overall, all-or-nothing.

## Session interrupt

The operator web composer uses ordinary `send` for Send, Enter and Alt+Enter; Shift+Enter inserts a newline. The operator-only `chat-stream:send` IPC handler drops urgency, including stale-tab `reply.urgent=true`. Intentional lower-level agent sends/tells and scheduled urgent wakes retain their existing urgency. Explicit Stop remains generation-fenced and cancels pending queued continuations.

Qualified provider-queued rows offer **Send now**, without a permanent warning. The separate `chat-stream:queued-send-now` IPC sends `send.interrupt` with `queued_send_now: { original_request_id, optimistic_id }` and `expected_session_generation`; it never repastes the original body. The daemon validates the authenticated operator, durable receipt, queue ownership, generation and installed provider/executable identity. Native intake already proved means no keypress. A durable control record in the existing `kv` store begins when ordinary intake observes provider queue ownership under the lifecycle/input locks; legacy rows without this generation binding fail closed.

For qualified Claude, the daemon sends one native Ctrl+X Ctrl+S. Native queue-and-draft semantics apply: backgroundable work can continue, other work may interrupt. A nonempty or unproven native draft refuses the action; web drafts remain untouched. For qualified Codex, it sends one Escape bound to the active native turn. Only a matching authenticated native `turn_aborted` with reason `interrupted`, after the operation watermark, plus the selected original's durable native intake, authorizes one ordinary daemon `continue`. Explicit Stop, external Escape, agent urgency, typed interruption text and historical events never arm this policy. Continuation requests use permanent same-ID send dedupe; key-dispatch uncertainty never replays a key. Receipt/native intake does not assert model completion.

Supported identities are held by the daemon's qualified provider/version/executable pins and advertised as `capabilities.queued_send_now`. Remote/unqualified versions keep provider queue behavior with no new key fallback. Repeated clicks, lost ACK and reconnect reconcile the same control operation. The queued-action RPC has a bounded 15-second acknowledgement wait; retry retains the original identities.

Old cached queued-row controls still call generic interrupt, indistinguishable from Stop. Host urgency stripping protects stale new-message urgency only. The existing build-change icon prompts a click to reload; it neither auto-reloads nor blocks an old tab. A deployment requires its authorized operator-tab refresh/closure disposition before a live claim.

`send.interrupt` names a session with `host` and `session_name`. For a configured
remote host, updated web clients also send the selected open row's
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
`0 < duration_s <= 600`, normalized to milliseconds (a value that rounds to 0 is dropped),
plus the optional bounded context below. Other metadata is dropped;
the receipt retains its internal `assistant_composite` marker. Ordinary-stream
voice durations must also be finite. Voice metadata does not alter send identity.

### Captured page context

An existing voice send may add `meta.voice.context_v1` beside `duration_s`:

```json
{"voice":{"duration_s":15,"context_v1":{"visits":[{"offset_ms":0,"page_id":"personal","title":"Personal"},{"offset_ms":7200,"page_id":"questions","title":"Questions","question_id":"q-example-1"}]}}}
```

One recording carries one trail, without audio segmentation or automatic
question answers. Only IDs and short titles are carried, never page contents,
question bodies or credentials. Older daemons may ignore this optional field;
voice still sends, but context visibility is not implied.

The daemon accepts 1–24 visits and at most 2048 bytes of compact UTF-8
normalized context JSON. Titles have at most 48 Unicode code points;
controls/format characters are removed and whitespace is normalized. Empty
titles are valid. The optional boolean `truncated` is emitted only when true.
Unknown fields are dropped. Integer `offset_ms` starts at 0, never decreases,
and cannot exceed the recording's duration or 300000 ms. Clients measure
captured sound, excluding paused/background dwell. Consecutive equal
page/entity/question IDs coalesce; revisits after another identity remain.

`page_id` is one of `assistant`, `chat`, `personal`, `calendar`, `lists`, `list`,
`dashboards`, `settings`, `agents`, `sessions`, `status`, `updates`, `lanes`,
`lane`, `lane_history`, `questions`, `report`, `image`, `other`. Optional
`entity_id` and `question_id` require a full-string ASCII match against
`[A-Za-z0-9][A-Za-z0-9._:-]{0,95}`. Invalid optional IDs are omitted;
`question_id` is retained only on `questions`. Slashes, Unicode, whitespace,
controls and trailing newlines are forbidden in IDs. Other malformed context
is dropped without rejecting valid voice text/duration. Clients cap overflow
by retaining early visits plus the latest and setting `truncated`; the server
drops over-cap context rather than inventing visits.

Normalized metadata appears on the canonical USER event and receipt.
Composite admission freezes it in existing route JSON, so routing, rebind,
restart and retry cannot replace it with later screen visits. Direct and
routed assistant dispatch append it beside existing voice-answer data;
ordinary chat includes the same block in its materialized send. The visible
event text remains the transcript. The receiving pane gets one
`<voice-context-v1>` JSON block labeled untrusted observed UI data. Its JSON
escapes delimiter characters in titles. Context cannot change recipient,
generation, send identity or instruction authority; recipients look up IDs
only when needed.

The four-visit fixture is 306 UTF-8 bytes (roughly 77 tokens at four bytes per
token; tokenization and dispatch framing vary). Context is sent once per
logical input, without a new verb, table, recorder, capture-time lookup or
extra model turn. Logs tagged
`subsystem=voice bug_ref=voice_everywhere_context` contain only visit counts
and truncation, never titles or IDs.

From the repository root, run
`python -m pytest services/chat-stream-v2/tests/test_voice_context.py -q`.
Shared conformance fixtures are `tests/fixtures/voice_context_v1.json` and
`voice_context_id_alphabet.json` under the service. Coverage includes an
authenticated isolated loopback send, real Store/receipt readback, the
source-derived dispatcher that `main.run` installs, existing Comms
materialization, and a synthetic receiving pane with a correlated answer.
No external model, microphone or deployed daemon is exercised. Run the
existing serial merge gate on the final clean committed candidate.

### Web composer recording

In web mode, open the experimental Chat view and tap **Record voice message**
in the chat composer to start a take in the viewer's browser. See the
[web setup](../README.md#quick-start) for enabling Chat UI. The implementation
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

When the daemon advertises `outbound_commit_v1`, the chat composer commits the
take before uploading it (`chat-stream:outbound-commit`, see
[committed-outbound-send.md](../services/chat-stream-v2/docs/committed-outbound-send.md)):
the daemon transcribes and sends once the audio arrives, even if the page is
reloaded or closed. The take polls `chat-stream:outbound-get` until the send
commits. A server failure shows its reason; Retry then takes the client path
below with the committed request and optimistic ids, so receipts coalesce.
Discard calls `chat-stream:outbound-cancel`, which never implies the message
landed. A definite commit refusal (no capability, older daemon, invalid
mandate) uses the client path directly; voice answers always do.

The client path is **upload_blob → transcribe_blob → ordinary send**. The browser
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
[separate room-mic path](desktop_config.md#per-chat-voice-messages) remains separate. See
[web voice validation](E2E_HARNESS.md#web-voice-composer) for automated coverage
and the distinction between fixture, browser and live release evidence.

## Strict publication

The primary composite defaults to strict publication: front-desk turn text is
not projected into chat. Explicit publications deliver dispatch replies or
proactive notes; the latter carry a note type and a reference to an existing
record and are checked for duplicate normalized text over 60 minutes. Note
types are `result`, `problem`, `reminder`, `relay`, `started`, `change` and
`other`; `other` carries a stored, unrendered `note_reason`, and `started` or
`change` is accepted once per ref per 24 hours. A landed
operator dispatch without a publish receives an immediate `dispatch_no_reply`
notice and, after 300 seconds, an `unanswered` chat event tied to the input.
Desk-originated dispatches receive neither. `inspect_stream` reports the setting
and source. Set `assistant.strict_publication.enabled` to `off` in kv to restore
legacy mirroring and untyped notes without a restart.

### Typed Design links

The agent image send and proactive `assistant.publish` paths accept
`dashboard_link` for a Design target with one PNG attachment. The daemon
writes it as `raw.dashboard_link` on the same transcript event as the
ordinary caption and attachment. Authorization and request-ID deduplication
use the same rules as the underlying image publication. A new Design post
requires this shape:

```json
{
  "dashboard": "design",
  "target": { "project_id": "sample", "design_id": "orders", "view_id": "phone" },
  "title": "Phone",
  "subtitle": "Orders",
  "revision_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Omit `view_id` to target a board. IDs have 1–80 lowercase ASCII letters or
digits, with hyphens allowed after the first character. The shared reader
also accepts an omitted `target` for the Design Home chip. A null or malformed
target, or an unknown dashboard, leaves the caption and attachment in the
ordinary transcript.

`revision_id` pins the preview image only. Web and mobile open the current
target at `/next/design` and `/next/embed/design` respectively, followed by
`/<project>/<design>[/<view>]` for a target. A missing or invalid optional
preview field leaves the link actionable. New web tries the recorded
revision's authenticated media first and then the PNG attachment. Older
clients show the caption and attachment.
