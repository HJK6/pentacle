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

Version-one kinds and policies are:

| Kind | Policy | Purpose |
| --- | --- | --- |
| `notice_marker` | `internal` | Bare delivery marker; never prose |
| `notification_answer` | `structured_card` | Compact operator-answer row |
| `child_session_closed` | `structured_card` | Daemon lifecycle row |
| `child_inactivity_threshold` | `structured_card` | Daemon inactivity row |
| `child_report_ready` | `structured_card` | Compact child-report row |
| `claude_pasted_content` | `chat_prose` | Lane-A authenticated wrapper, re-exported from `provider_wrappers.py` |

The shared fixture at `pentacle-chat-core/tests/fixtures/message-envelopes.json`
binds wire bytes to SHA-256 digests and is consumed by both Python and
TypeScript tests. `pentacleEventInterpreter.ts` renders registered tags and
fails closed for an unregistered marker-prefixed `USER` event; its legacy
inactivity/report parsers remain compatibility fallbacks for historical,
untagged rows.
