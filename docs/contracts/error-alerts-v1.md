# Error Alerts v1

The executable wire schema is `error-alerts-v1.schema.json`: the optional `voice_operation` intent on `upload_blob_init`/`transcribe_blob` and the one `error.report` verb. There are no list, detail, mark or settings verbs.

## Facts and delivery

One typed fact is one `notifications.db` row with a versioned `error_context` and a unique `error_key`. The existing outbox (`v2_outbound_notices` in `sessions.db`) is the only delivery owner: `error_alert` notices are enqueued by `ErrorAlerts.reconcile` inside the existing five-second outbox pass, fenced to the current front-desk binding and generation, folded into the typed digest by the fixed rate limits, and proved by the existing tell delivery record. Notice bodies are server-generated, content-free and carry `alert=<notification_id>` and `episode=<id>`. `PENTACLE_ERROR_ALERTS_MODE=off|record-only|on` (default off) is the kill switch.

## Producers

- Tagged voice: the `voice_operation` intent above, plus `error.report` for client-only evidence. Reporting requires an actual operator auth_v2 credential.
- Installed voice clients without the tag: an operator `transcribe_blob` with an `audio/*` MIME, and an operator `send` carrying `meta.voice`, register implicit operations. Only failures the daemon actually observes alert. Anything before the daemon sees audio is out of scope.
- Other families: one line in `FAMILY_CODES` and one mapper in `ADAPTERS` (`services/chat-stream-v2/error_adapters.py`). Producers call `await alerts.record(kind, **fields)` (log line plus fixed mapping) or `await alerts.error(fact)`. Both return the notification_id only after commit, or None when the core is not configured. `alerts.emit` stays log-only and never makes a typed fact.

## Reading retained facts

Use read-only access to the existing stores, for example:

    sqlite3 -readonly notifications.db "SELECT notification_id,producer,first_fired_at,last_fired_at,firing_count,state,error_context FROM notifications WHERE error_context IS NOT NULL ORDER BY last_fired_at DESC LIMIT 50"

To check delivery, match the `sessions.db` `v2_outbound_notices` row on `json_extract(metadata,'$.notification_id')`. Then follow `folded_into_notice_id` to the digest and join `v2_tell_deliveries` on `tell_id` for proof. A folded member is not delivered: only the linked digest's proof counts.
