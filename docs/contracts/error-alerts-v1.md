# Error Alerts v1

The executable wire schema is `error-alerts-v1.schema.json`: the optional `voice_operation` intent on `upload_blob_init`/`transcribe_blob` and the one `error.report` verb. There are no list, detail, mark or settings verbs.

## Facts and delivery

One typed fact is one `notifications.db` row with a versioned `error_context` and a unique `error_key`. The existing outbox (`v2_outbound_notices` in `sessions.db`) is the only delivery owner: `error_alert` notices are enqueued by `ErrorAlerts.reconcile` inside the existing five-second outbox pass, fenced to the current front-desk binding and generation, folded into the typed digest by the fixed rate limits, and proved by the existing tell delivery record. Notice bodies are server-generated, content-free and carry `alert=<notification_id>` and `episode=<id>`; the one exception is `operator_notification.v1` below. `PENTACLE_ERROR_ALERTS_MODE=off|record-only|on` (default off) is the kill switch.

Activation gate: while the mode is not `on`, a reconciled fact is stored with `disposition: muted` and `muted_revision` = its `operation_revision`. With the mode `on`, a muted fact stays silent until a newer emit advances `operation_revision` past `muted_revision`; a muted fact without `muted_revision` (recorded before this gate) is adopted at its current revision and stays silent. Turning the mode on never replays history.

## Producers

- Tagged voice: the `voice_operation` intent above, plus `error.report` for client-only evidence. Reporting requires an actual operator auth_v2 credential.
- Installed voice clients without the tag: an operator `transcribe_blob` with an `audio/*` MIME, and an operator `send` carrying `meta.voice`, register implicit operations. Only failures the daemon actually observes alert. Anything before the daemon sees audio is out of scope.
- Other families: one line in `FAMILY_CODES` and one mapper in `ADAPTERS` (`services/chat-stream-v2/error_adapters.py`). Producers call `await alerts.record(kind, **fields)` (log line plus fixed mapping) or `await alerts.error(fact)`. Both return the notification_id only after commit, or None when the core is not configured. `alerts.emit` stays log-only and never makes a typed fact.
- Operator notifications (`operator_notification.v1`): every `notification.create` (including `agent-orch notify`) of severity `warning` or `critical` that is not one of the fixed authenticated producers above records one fact under the fixed principal `system:operator-notify`, with `code` = severity, `stage` = the producer slug (lower-cased, characters outside `[a-z_]` become `_`, at most 32), and `error_context.source_notification_id` = the card's notification_id. The card itself, its broadcast and the reply bytes are unchanged; `info` stays a card only. One episode is (producer, severity, dedup_key or title): repeating a dedup_key updates the same fact and does not re-alert, so a recurring failure needs a changing dedup_key (for example a date). The immediate fingerprint is principal, family, stage and code, so one producer gets at most one immediate notice per severity per 15 minutes within the shared three-per-15-minutes budget; the rest fold into the FD digest. The notice body is read from the stored card, title only (control characters stripped, at most 200 characters): `BLOCKER <severity> from <producer>: <title> alert=<fact id> card=<card id> first=… count=…; Bart decides, no auto-repair.` If the card is gone, the generic body is used.

## Reading retained facts

Use read-only access to the existing stores, for example:

    sqlite3 -readonly notifications.db "SELECT notification_id,producer,first_fired_at,last_fired_at,firing_count,state,error_context FROM notifications WHERE error_context IS NOT NULL ORDER BY last_fired_at DESC LIMIT 50"

To check delivery, match the `sessions.db` `v2_outbound_notices` row on `json_extract(metadata,'$.notification_id')`. Then follow `folded_into_notice_id` to the digest and join `v2_tell_deliveries` on `tell_id` for proof. A folded member is not delivered: only the linked digest's proof counts.

## Historical ingest identity incidents

`integrity / ingest_identity_blocked` records a refused local observer identity
at normal ingest cadence. Its `episode_id` is `ingest_identity_blocked:` plus a
SHA256 of the ordered stream, generation, complete expected binding digest and
canonical observed start (or `unavailable`). Its stage is one finite reason:
`start_mismatch`, `proof_unavailable`, `foreign_identity` or `cas_changed`.
The notice contains the hashed episode and reason; local structured ingest logs
map it to the compared metadata without transcript text, arguments, nonce or paths.

This code is a historical incident. The first durable fact wins: identical
retries, reason changes and recurrence after a healthy pass retain the same
notification, revision and acknowledgement. Different binding, generation or
observed-start tuples can create another incident. A healthy idle session,
whitespace-only presentation difference or successful recovery creates none.
Sink absence/failure warns and retries on subsequent normal passes. Closing a
session before a successful durable handoff guarantees only the warning log;
committed facts retain normal outbox delivery after close.

Look up an incident ID using the indexed error key and its daemon principal:

    sqlite3 -readonly notifications.db "SELECT notification_id,state,error_context FROM notifications WHERE error_key='integrity:daemon:<local host>:<episode id>'"

A notice's `alert=<id>` also supports a direct retained-fact read:

    sqlite3 -readonly notifications.db "SELECT notification_id,state,error_context FROM notifications WHERE notification_id='<alert id>'"

Verify the returned `error_context.episode_id`, then use the private ingest log
mapping to identify the generation and stream. Read the current session with
`agent-orch inspect <stream> --event-tail 1` and check its open status, generation,
observer binding and actual current ingestion. A retained warning is not a claim
that the session is currently blocked; retirement or silence proves no recovery.
Use the existing notification acknowledgement/disposition path after review.
Strict-root success/recovery logs the prior in-memory incident when known; after
restart it does not fabricate a durable recovery state. The earlier alert core
can read/deliver retained facts for this code on rollback, but must not be asked
to produce a code absent from its fixed registry.
