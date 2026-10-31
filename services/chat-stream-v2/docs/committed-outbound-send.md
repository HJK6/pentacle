# Committed outbound send (server-owned finish)

Once the bytes are on the daemon, it finishes the send: it transcribes a voice take and sends the message once, with the identity the client committed before uploading. The client no longer has to stay awake between the upload and the send. The wire schema is `docs/contracts/error-alerts-v1.schema.json` (`$defs.send_mandate`, `outbound.*` requests). The code is `outbound_finisher.py`.

## Capability and carriers

The snapshot advertises `capabilities.outbound_commit_v1: true` to an authenticated v2 operator connection (the same gate as `error_alerts_v1`). Clients send mandates only when it is advertised. Without a mandate, nothing changes: no finisher runs and installed builds keep their client-owned transcribe and send.

- Mobile voice: an optional `send_mandate` on the protocol-2 `upload_blob_init` (see [resumable-voice-upload.md](resumable-voice-upload.md)). Clients send it on every init of the take. The init ACK carries `server_send: true` when the mandate is stored. The mandate's audio descriptor is the init's own `sha256`/`mime`/`total_bytes`.
- Photo messages and web voice: `outbound.commit {request_id, voice_operation, send_mandate}` returns `outbound.commit.ok {operation_id, server_send: true}`. `voice_operation.operation_kind` is `message` for a photo message. The client then uploads with ordinary protocol-1 `upload_blob_*`. Web voice includes `send_mandate.audio`.

## Mandate

`send_mandate` (version 1) contains the client's `request_id`, `optimistic_id`, `msg_id` (present exactly when the origin is an assistant composite, and equal to `optimistic_id`), `sequence`, and `meta` and `client_build` (equal to the operation's). It also has optional `attachments` (≤5 sha256 keys), `reply_to_*` and the following:

- voice: `original_request` (voice-v1 proof; `request_id` must equal its derivation for the operation's origin and generation), `text_prefix`, an optional `audio` descriptor, and `meta.voice_answers` whose `recording_id` is the operation and whose `blob_sha` is the audio.
- message: `text` (it may be empty when attachments are present).

A mandate is at most 16 KiB. An invalid mandate gets `invalid_request` and nothing is stored. An identical repeat is idempotent; a different second mandate is a conflict (`upload_conflict` on init, `event_conflict` on `outbound.commit`). Only a current, unscoped operator credential may use these verbs; scoped, Dot, service and stream-token callers are refused. Blob access keeps full-operator semantics: blobs are checked for existence, not ownership.

The mandate is stored on the existing `voice_operations` row as `data.server_mandate`, with `data.state`. There is no other queue or table. Milestones stay content-free, and logs never print mandate text or attachments.

## State machine

`pending → running → submitting → committed | proved`, or one of the terminals `failed` / `cancelled`.

- Kicks: a protocol-2 `upload_committed`, a protocol-1 blob commit whose sha the uploader's active mandates name, `outbound.commit`, the startup sweep (`running` becomes `pending`) and every `ErrorAlerts.reconcile` pass. A kick while a worker owns the row does nothing.
- Readiness: the audio blob (voice) or every attachment blob (message) must exist. If one is missing, the row stays `running` with `pending_blobs`.
- Steps: check the credential (`credential_revoked`) and the origin, which must be open at the operation's generation (`origin_unavailable`). For voice, call ASR with `request_id = transcribe-<operation_id>`. `backend_unavailable` and transport errors retry after 5, 15 and 30 s, then every 60 s, for 10 min, then fail `transcribe_unavailable`. An empty transcript is `no_speech`.
- Pre-submission, under a per-operation lock: honour a cancel, reconcile `server_send_request_ids` (a landed receipt means committed, and client-reported ids are never consulted), recheck the origin, then durably mark `submitting` with an `attempt` and `send_attempted`. The lock is released before the send.
- Send: the same frame the client would send, through the error-alert-wrapped `send` handler. That handler binds the request id before submission and fences the origin generation. Plain sessions use the existing `voice_origin_generation_conflict` check. For a server-owned send to a composite, `admit_assistant_composite_input` compares `expected_origin_generation` inside its admission transaction (client sends to composites are unchanged).
- Outcomes: landed, persisted, accepted, or a conflict proving the message exists, are `committed` (`proved` when `submission_confirmed`). A definite refusal is `failed <code>`; a generation conflict is reported as `origin_unavailable`. A transport error or an `enter_failed` phase leaves the row `submitting`. With no live worker, the reconcile pass resolves it from receipts: `not_landed` fails `not_landed`, and no receipt after 10 min fails `send_unconfirmed`. A restart never resubmits `submitting`.
- Terminal rows are never re-kicked. A `failed` mandate never sends again. After that the client owns the unit with the same identity, and the daemon's exact-request coalescing applies.

## Verbs

- `outbound.cancel {operation_id}` → `{state}`, where state is one of:
  - `cancelled`: the row was pending or running and is now fenced; a failed row also returns `cancelled`.
  - `in_flight`: the row was submitting, and nothing changes.
  - `committed`: the send is already committed.
  - `no_mandate`: the operation has no mandate.

  The reply never means the message landed.
- `outbound.get {operation_id}` → `{state, milestones}`. The milestones are content-free: `upload_committed, pending_blobs, transcribe_started, transcribe_result, transcribe_failed(_code), send_attempted, send_committed, send_proved, server_finish_failed(_code), cancelled_at, send_request_id`.

## Alerts

Mandate rows project into the existing `voice_operation.v1` family:

- `failed` projects `active server_finish_failed`; `no_speech` and the other expected codes project as cancelled.
- A row that has been ready for ≥60 s without ASR or a send attempt, or not ready after 10 min, projects `unknown server_finish_stalled`.
- A row `submitting` for ≥60 s projects `unknown send_unconfirmed`.

Rows without a mandate keep the existing evidence rules, including offline-gap suppression. Old rows are never backfilled.

## Rollback

There is no schema migration. An older daemon ignores `data.server_mandate`, answers `outbound.*` as an unknown verb and refuses `send_mandate` on init (`invalid_request`). A capability-gated client then falls back to its own path with the same identity, and receipts coalesce.
