# M6 scoped blob-read repair and bounded audit

## Security boundary

A stream-scoped fetch validates a canonical lowercase 64-character SHA256 before
both ownership and reference lookup. Exact ownership is sufficient for a scoped
credential's own completed upload. For another uploader's bytes, only an exact
managed publication reference in that scoped stream is sufficient: the durable
reference must join its publication and ready managed upload provenance.

Transcript text, nested metadata, attachment-shaped event fields, caller role,
provider, event type and stream labels do not grant access. Generic history,
imports and event pushes cannot mint the publication reference ledger. Unknown
historical envelopes fail closed. In particular, arbitrary legacy transcript
images and another credential's USER-input attachment do not become readable
merely because they appear in history; only own uploads or validated managed
assistant publications qualify. This is an intentional compatibility restriction,
not a migration/backfill of trust from untrusted historical content.

No schema change is added by this repair. It depends on the earlier managed
upload/publication tables in this PR. It is a separately identifiable commit,
not a standalone patch for a deployment missing those prerequisite tables.

## Writer trace

- `server._dispatch` constructs verified authentication context. Scoped clients
  cannot call `assistant.publish` or generic event import verbs; external
  principals retain their independent allowlist and TLS/revocation gates.
- `server._on_send` retains scoped-input attachment ownership admission through
  `_validate_assistant_input_attachments`; a forged top-level attachment with
  someone else's digest is rejected before composite input admission.
- Operator input and server-only `accept_assistant_operator_input` validate
  attachments before `AssistantComposite.accept_input`. Input routes and their
  USER events do not write `v2_attachment_refs` and do not independently grant
  scoped reads of another uploader's bytes.
- `server._on_assistant_publish` requires a verified current publisher generation.
  `AssistantComposite.publish` validates stream, dispatch, reply correlation,
  publisher and generation, resolves managed upload IDs and constructs metadata.
  `store_routing.record_assistant_composite_publication` repeats actor/route
  checks in its transaction. `publication_attachment_guard` locks digests and
  revalidates ready upload rows, scope, canonical type/name, size and actual bytes.
  That transaction is the sole `v2_attachment_refs` writer; it commits event,
  publication and typed reference together. Rollback grants nothing.
- Generic transcript writers (`store.append_session_event`, lifecycle-CAS batch
  append) are reached from `event_push.py`, `ingest.py`, `comms.py`, routine
  composite ingress and `assistant_lane_rulings.py`. They write transcript rows,
  not the managed publication ledger. They cannot grant scoped file access.
- Snapshot/history/mirror presentation is not a grant source. Copying an event or
  forging its labels cannot copy its publication ledger provenance.

## Bounded reader audit

Searched `services/chat-stream-v2/server.py`, `blobs.py`, `store.py`, `store_*.py`,
`assistant_composite.py`, `event_push.py`, `ingest.py`, `comms.py`, `notify.py`, and
`assistant_lane_rulings.py` for SQL LIKE/instr, JSON extraction/membership,
text/JSON substring checks and callers of ownership/reference readers.

Findings and disposition:

1. `blob_referenced_in_stream`: event JSON LIKE granted access on hash text.
   Replaced with exact durable managed-publication provenance joins.
2. `scoped_owner`: exact `(kind,key)` equality and first-writer ownership.
   Fetch now rejects malformed keys before this branch. Scoped send validates
   canonical attachment keys and ownership. Transcribe is owner-only and does
   not use transcript references; its byte reader performs digest validation.
3. Request/receipt reader: `_on_send_receipt_get` checks exact scope and exact
   credential ownership, then `get_send_receipt` uses exact target/request.
   No content-substring authorization there. A separate admission-order flaw
   was confirmed through synthetic RPC: request ownership is recorded before
   composite admission completes. A rejected send can claim a known operator
   request ID and subsequently read that same-stream receipt. The RED evidence
   is retained privately and reported separately for its own repair commit.
   It is not changed by this blob patch; scoped activation must remain held.
4. `store.list_delivered_tell_ids` uses LIKE for a fixed internal namespace
   (`notification-answer-`) requested only by notification startup recovery.
   It additionally checks structured delivered ledger identities/statuses. The
   current caller's prefix has no SQL wildcard. Arbitrary-prefix generalization
   would need literal-prefix handling; no current scoped blob grant uses it.
5. Routing JSON comparisons for activity/final-response state use exact fields
   for presentation and lifecycle accounting, not blob read authority. Encoded
   frame marker `.find` is wire sizing, not authorization.

This is a bounded source and synthetic-test audit, not proof that all authorization
bugs are absent. No live conversations, credentials or blob probes were used.

## Reproduction and regression

`tests/test_managed_attachment_fetch_scope.py` exercises actual JSON dispatch and
scoped fetch handlers with synthetic registry/connection fixtures and temporary
SQLite/blob stores. Against the original implementation: 13 failures, 6 passes.
The failures include plaintext/nested/unrelated/forged transcript references and
malformed keys seeded as owned. The repaired candidate passes those 19 cases plus five additional cases for
failed/interrupted publication and the supported owned photo/voice journeys.

Additional coverage retains wrong-stream denial, forged scoped-send rejection,
actual scoped upload ownership and byte fetch, revocation on the next fetch,
external-principal denial, authorized publication positive and honest missing
bytes. Combined scoped/external/publication/roundtrip/transcribe selection: 117 passing tests.
The original RED log is retained as private synthetic test evidence; no live data
or internal machine paths are included in this document.
