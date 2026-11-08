# Message-envelope registry

`message_envelopes.py` is the daemon owner of the operator-chat wire formats.
Each immutable `MESSAGE_ENVELOPES` entry has a builder, a full-string matcher,
and a render policy. Producers use `build_message_envelope` (or the historical
`notice_needle`/`ensure_notice_marker` primitives, which delegate to the
registry) so marker construction and matching stay coupled.

Ingest annotates authenticated `USER` events before persistence/projection with
the additive tag:

```json
{"message_envelope":{"kind":"child_session_closed","id":"…","schema_version":1}}
```

The exact pre-projection text is retained as `raw.envelope_source`. Existing
`provider_wrapper`, identity, and `raw.daemon_notice` fields remain unchanged.
An untagged event beginning with a notice marker is counted and logged with
`subsystem=message_envelopes`; it is never trusted by clients.

For authenticated provider wrappers, annotation matches unwrapped display text
against the existing notice kinds first. A known inner notice wins over the
`claude_pasted_content` fallback; plain wrapped prose keeps the wrapper tag.
`provider_wrapper` and `raw.provider_content` remain intact in both cases.

Authenticated satellite Claude events use the same annotator in
`EventPush.handle_push`, after durable receipt stamping and before the
lifecycle-CAS append. The sink computes durable identity before annotation,
preserves source/session admission and raw provider bytes, and broadcasts the
tag only for a new insert. Satellite replay adds no rows or broadcasts.
Non-Claude events retain their existing remote-ingress behavior. Local/remote
parity, wrapped notice precedence and replay are covered in
`tests/test_event_push.py` through the real sink and Store.

Version-one kinds and policies are:

| Kind | Policy | Purpose |
| --- | --- | --- |
| `notice_marker` | `internal` | Bare delivery marker; never prose |
| `notification_answer` | `structured_card` | Compact operator-answer row |
| `child_session_closed` | `structured_card` | Daemon lifecycle row |
| `child_inactivity_threshold` | `structured_card` | Daemon inactivity row |
| `tree_idle` | `structured_card` | Whole-lane idle notice for the direct-primary root |
| `lane_digest` | `structured_card` | Change-only periodic lane digest for the direct-primary root |
| `child_report_ready` | `structured_card` | Compact child-report row |
| `claude_pasted_content` | `chat_prose` | Lane-A authenticated wrapper, re-exported from `provider_wrappers.py` |

The shared fixture at `pentacle-chat-core/tests/fixtures/message-envelopes.json`
binds wire bytes to SHA-256 digests and is consumed by both Python and
TypeScript tests. `pentacleEventInterpreter.ts` renders registered tags and
fails closed for an unregistered marker-prefixed `USER` event; its legacy
inactivity/report parsers remain compatibility fallbacks for historical,
untagged rows.

## Multiline notification answers

The notification-answer producer keeps historical one-line fields unchanged.
For `answer` (joined selections), `text`, or `custom_text` containing LF or CR,
it emits `<field>_json=<JSON string>` on one physical wire line. For example,
`custom_text_json="line one\nline two"` represents an actual LF in the answer.
Ordinary `custom_text=literal\ntext` remains literal backslash-n text.

The anchored notification-answer matcher accepts JSON strings only for these
three fields. It rejects malformed JSON, empty/non-string values, duplicate
logical fields (including plain/encoded aliases), raw multiline continuations,
a missing terminal nonempty `by=`, and mismatched notification/marker ids.
Other envelope grammars are unchanged. The existing answer-back length limit
still applies. Encoded truncation ends with a separate `[answer truncated]`
line, so even a cut inside the actor field fails closed after provider whitespace
trimming. Historical one-line truncation bytes and behavior are unchanged.

When encoded fields occur, the matcher returns all validated answer fields and
the annotator carries those fields plus the notification id into the tag.
Historical one-line tags keep their minimal shape. The shared interpreter
prefers the validated tag value over the raw fallback for each field, retaining
`answer`, `text`, `custom_text`, `choice` priority and exact decoded whitespace.
Its `notificationAnswerText` presentation field passes through the transcript
model so the web/desktop answer element preserves LF and escapes markup without
treating a continuation such as `Note:` as a separate note. Structured question
notes keep their existing rendering.

`tests/test_notification_answer_multiline.py` and the shared multiline fixture
cover producer, matcher, annotation and ingest. Core and transcript-view tests
assert exact LF text; the hermetic web scenario `multiline-notification-answer`
checks the real compact answer row after scratch-daemon ingest and transport.

## Mixed native input and peer provenance

The `peer_delivery` registry entry owns the unchanged wire
`[from <stream>] [tell:<id>]\n<payload>` (and the existing `send` anchor).
Comms and both Claude/Codex adapters use that builder/decoder. A submitted
pre-enveloped peer body is literal payload inside the actual sender's outer
stamp. Its whitespace and inner claims are preserved.

Native text blocks are decoded before joining; only adjacent ordinary blocks
merge. Tool-result, notice and peer boundaries retain their order. A single
coalesced block splits only at unique exact spans supported by retained
pre-input delivery or dispatch evidence. Unresolved boundaries keep their exact
bytes with an unknown-origin diagnostic. Raw metadata retains the native record,
wrapper, block ordinals and segment spans. Existing event identities and a
retained split recipe make interrupted/restarted and overlapping ingest stable;
records already persisted before this change are neither split again nor rewritten.

A textual header or authenticated satellite connection supplies a candidate,
not sender authority. Store projects `raw.input_provenance` from exact
session-generation, watermark and once-consumed occurrence proof; client-supplied
values are replaced. Proven operator dispatches take precedence over text that
resembles a delivered wire. A later byte-identical quotation remains literal.
The shared interpreter renders operator and unknown envelope-like input literally;
unknown input is labeled without claiming a peer or operator identity.

Ordinary send captures its final wire, exact SHA-256, verified sender,
destination generation and watermark before input in daemon-owned
`_input_proof` JSON on the existing append-only receipt log. Occurrence binding
also appends a receipt. This private field is excluded from public/client metadata;
retry/coalescing does not create a fresh physical occurrence. Legacy, stale or
ambiguous evidence remains unknown. Tell uses the existing prepared/promoted
ledger proof, retaining captured provenance through retries. An operator's
correlated final answer survives a later proven peer/notice in the same turn;
a peer-only turn publishes no coordination prose.

When an unproven native TELL projects as a literal USER, daemon-owned `raw.input_source_kind` retains its native kind. Submission proof uses that original kind; a display projection cannot promote a non-USER event into USER proof. A finite Claude wrapper spanning adjacent native text blocks is decoded as one wrapper, with all original block spans retained.
