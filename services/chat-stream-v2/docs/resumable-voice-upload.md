# Resumable voice upload

Protocol 2 is an opt-in extension of `upload_blob_init` and
`upload_blob_chunk` on the existing authenticated daemon socket. It is for
finalized voice audio only. Generic, photo, managed-attachment and prompt
protocol-1 uploads retain their existing wire format, limits and
connection-owned cleanup. No new provider, socket or transcription cache is
introduced.

## Authentication and immutable identity

Every protocol-2 init, resume and chunk (including an empty final chunk)
requires a currently active, unscoped v2 operator credential. Loopback is not
an exception. The daemon derives the principal using the existing error-alert
adapter; wire-supplied identity cannot authorize an upload. Anonymous, revoked,
scoped, service and seat credentials receive `upload_forbidden`.

The client owns a stable UUID `upload_id` for the immutable audio. Each RPC
uses its own `request_id`; the RPC ID is never the resume identity. Sessions
are keyed by authenticated principal plus upload UUID. A second valid
credential cannot read another principal's offsets or receipt. Its init can
create an independent zero-offset session with its own authorized operation;
its chunk before that init receives `upload_expired`. Credential rotation does
not migrate bytes or ownership.

An init supplies:

- `protocol: 2`, `request_id`, `upload_id`
- `total_bytes`: integer 1 through 16,777,216 (16 MiB)
- `sha256`: exact lowercase SHA256 of the entire immutable audio
- `mime`: `audio/mp4` or `audio/wav`
- `voice_operation`: the existing six-field object below
- Optional `purpose: "generic"`
- Optional `send_mandate` when the daemon advertises `outbound_commit_v1`; the
  daemon then transcribes and sends after `upload_committed` and the ACK
  carries `server_send: true` ([committed outbound send](committed-outbound-send.md))

The frozen `voice_operation` fields are `version: 1`, `operation_id` (the
recording UUID), `origin_stream_id`, `origin_generation`, `operation_kind`
(`voice_chat` or `voice_answer`), and `client_build`. Existing schema and
origin checks apply. Resume must repeat exactly the same audio metadata and
operation; a conflict never resets or deletes accepted bytes. Unknown ordinary
fields, including claimed principal, credential, and `upload_protocol`, are
rejected. The client does not write server protocol flags.

The canonical origin generation comes from the actual Store session row.
Composite projection preserves that generation; a missing generation is
omitted, never replaced with a constant. Closed/replaced origins still fail
registration. Queued mobile takes carrying an old fabricated generation are
held rather than silently rebound.

## Chunk and ACK contract

Each chunk supplies `protocol`, `upload_id`, `request_id`, raw-byte `offset`,
strict canonical `data_b64`, lowercase `chunk_sha256`, and Boolean `final`.
The decoded chunk is at most 65,536 bytes. Empty data is allowed only with
`final: true` at the exact total offset. A final chunk must end at the exact
advertised total, and the whole digest must match.

Init replies use `upload_blob.init.ok`; chunk replies use `upload_blob.ok`.
Both echo the RPC `request_id` and carry `protocol: 2`, `upload_id`,
`next_offset`, and `complete`. An incomplete ACK has `complete: false` and
no `blob_sha`. Only a durable completed receipt carries `blob_sha`.

The client keeps one chunk in flight and uses only acknowledged offsets. Each
RPC retains the existing 30-second timeout. After timeout or disconnect the
client reconnects through its ordinary connection owner and re-inits the same
upload and metadata to obtain server truth. It does not guess progress or
silently fall back to protocol 1. A server without explicit protocol-2 replies
requires an update while the client retains its local recording.

A replay entirely within the committed prefix compares exact bytes and hash,
then acknowledges the current offset without appending. Different bytes,
overlap with the uncommitted suffix, holes and incompatible metadata are
rejected. Repeated final/init after completion returns the same digest and
length. A nonfinal chunk that reaches the total is still incomplete until an
explicit final request; an empty final is valid then.

Protocol errors use `upload_blob.error` with `protocol: 2`, the RPC ID and one
closed `error_code`: `invalid_request`, `upload_forbidden`, `upload_conflict`,
`offset_mismatch`, `digest_mismatch`, `upload_expired`, `upload_quota`, or
`upload_blob_disk_full`. No disk paths or exception text are returned. An
unknown/expired upload chunk requires re-init; re-init after expiry starts
at zero. Origin or immutable-operation conflicts require client attention.

## Disk durability and recovery

Sessions live under `<blob-root>/.voice-uploads-v2/<principal-hash>/<upload-id>/`,
outside the protocol-1 `.tmp` directory. The directory contains a bounded
`checkpoint.json`, `audio.part` while staging, and an optional quarantine
marker. It contains no credential key or bearer token.

The checkpoint holds immutable principal/upload/audio/operation metadata,
acknowledged raw offset and prefix SHA256, activity time, `final_requested`,
and the final complete/digest receipt. The internal prefix digest detects
corruption of previously acknowledged bytes. `final_requested` distinguishes
an authorized finalization from a nonfinal full-length upload.

Append and file fsync precede writing a same-directory temporary checkpoint,
fsyncing it, replacing the checkpoint, and fsyncing its directory. ACK follows
all of them. File operations and streaming hash verification run off the
asyncio loop. Principal/upload locks serialize writers; cancellation, including
repeated cancellation, joins the actual disk operation before releasing its
lock. Disconnect retains these checkpoints and bytes.

Startup/resume ignores incomplete temporary JSON and truncates bytes beyond
the committed offset. Shorter data, invalid checkpoint metadata and damaged
acknowledged prefixes are quarantined and never reported as a usable receipt.
A missing empty zero-offset file can be recreated safely. Quarantined bytes
are retained until ordinary seven-day expiry and consume a conservative
reservation when their metadata is unreadable.

Finalization first commits the full verified prefix and final request, then
uses the existing Store `complete_unmanaged_upload` and shared digest lock to
materialize the canonical content-addressed blob. Canonical bytes are
rehash/size-verified, including dedup and recovery, and file/directory durability
is established before the complete receipt. A crash after canonical move but
before receipt is repaired by verifying those bytes and completing the
milestone/receipt on resume. A filename alone is never proof. Replays do not
create another transcription cache or another canonical blob.

Durability assumes a functioning filesystem honoring fsync/atomic rename.
Disk failures produce an error without a false ACK; the next init reconciles
what actually committed. Local deletion, corrupt storage and credential loss
are not permanent-resume guarantees.

## Quotas and expiry

A principal may reserve at most 10 active sessions and 160 MiB of advertised
audio; the global reserved staging cap is 1 GiB. Admission and reservation are
serialized before accepting an upload, so concurrent init cannot overbook.
Completed sessions release their staging reservation. Their receipts and
inactive staging expire after seven days. Reaping runs on startup and init,
skips active writers, and never removes canonical blobs through this mechanism.
There is no new timer or alert-retention policy. Retained client audio can
restart an expired upload from zero.

## Milestones and queued gaps

Authenticated protocol-2 registration binds the existing operation's
`upload_request_id` to stable `upload_id` and persists server-owned
`data.upload_protocol: 2`. Protocol 1 remains request-bound. Finalization,
transcription start/result/failure and send commitment/proof use the existing
voice milestones.

Only two absence projections are suppressed for protocol 2, and only while
no `server_send_request_id`/`server_send_request_ids` is bound:

1. Upload committed with no later transcription start
2. Transcription result with no later send

Those gaps can be a legitimate offline queue. No operation is invented for
capture that has not reached the server. Explicit client events and failures,
the existing 150-second active-transcription timeout, and committed/unproved
send alerts retain their precedence. Staging expiry is not evidence that the
client recording was lost. Mobile emits local lifecycle telemetry; it does
not acquire a new `error.report` producer or queued-state wire field.

## Voice send receipt identity

Upload completion is not send delivery proof. The mobile first persists its
exact dispatch intent and uses the existing receipt and canonical USER proof
paths. The added original-request shape is exactly:

`{protocol:"voice-v1",logical_id,target,generation,sequence}`

`logical_id` is the recording UUID, target is the original stream (1–160
characters), generation is its canonical generation (1–128), and sequence is
an integer 1–1000, not a Boolean. The request ID is `voice-send-` plus the first
40 lowercase hex characters of SHA256 over UTF-8 JSON of the array
`[logical_id,target,generation,sequence]`, without spaces or ASCII escaping.
JavaScript uses `JSON.stringify`; Python uses
`json.dumps(..., ensure_ascii=False, separators=(",", ":"))`.

Unknown protocols, extra/missing keys, invalid types and tampered digest
bindings fail verification. The existing four-field dot-email identity is
unchanged. This identity supports generation evidence lookup; it does not
broaden pending-proof eligibility or dispatch authority. Image-bearing or
otherwise unproved sends may remain unknown. Only explicit proved
`not_landed` enables a new attempt sequence; unknown lookup/reconnect reuses
the saved attempt and never blindly resends. The optimistic and composite
logical message identities stay stable across attempts.

## Rollout, rollback and validation limits

Deploy and verify the daemon first, including legacy upload, v2 auth/resume
and receipt behavior, before activating the mobile client. An old daemon
cannot drain the protocol-2 client queue. Preserve both daemon staging and
mobile take directories for rollback; do not delete retained audio to make
an older binary appear compatible. Rolling back this daemon does not convert
v2 checkpoints into protocol-1 partials. Canonical blobs keep the existing
content-addressed layout, and the new operation flag uses existing JSON data
without SQL/schema migration.

`tests/test_voice_upload_resume.py` and `tests/test_voice_upload_crash.py`
exercise real local authenticated ingress, registered synthetic credentials,
Store and blob files. They cover ACK/replay/resume, restart, corruption,
append/checkpoint and final-move faults, lost final replies, revocation,
credential classes, quotas, expiry and v1 compatibility. Providers are local
fixtures. These tests do not establish native microphone behavior, mobile OS
scheduling or physical-device durability. Native/device acceptance remains a
separate release requirement.
