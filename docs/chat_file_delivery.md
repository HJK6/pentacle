# Chat file delivery: M1 upload foundation

This is the first source milestone. CLI publication, web/mobile downloads,
authenticated publication integration and managed-attachment GC follow in M2–M7.
Do not activate a partial packet as if the full operator journey were qualified.

## Wire and limits

The existing `upload_blob_init` / `upload_blob_chunk` transport accepts the new
`purpose: "chat_attachment"` and a `filename`. The daemon derives a canonical
media type from the sanitized basename and validates the format prefix on the
actual bytes. A supplied content-type is ignored. PNG/JPEG use the existing
image-envelope validator. PDF, ZIP, 3MF, STL, STEP/STP and SCAD are inert downloads;
there is no archive extraction, CAD execution, active preview or content secret
scanner. Canonical path refusal is a separate CLI responsibility in M2.

The original `ATTACHMENT_MAX_BYTES` value is shared without changing its 25 MiB
value or its `comms` import surface. It now gates the running byte count for the
new purpose as well as the image metadata validator. A false size hint cannot
bypass it. Generic/report uploads retain the original 64 MiB total / 1 MiB chunk
limits and their original reply shape. `send-image` and `asset publish` are unchanged.

```json
{"type":"upload_blob_init","request_id":"fixture-upload-1","purpose":"chat_attachment","filename":"example.pdf","size_hint_bytes":1024}
```

Authentication comes from the existing connection. Never put a caller-asserted
uploader, generation, principal or origin into this envelope. Success keeps
`blob_sha` and `size_bytes`, and additionally returns a server-issued `upload_id`,
`bytes`, `media_type`, `filename`, `uploader`, `generation`, `uploaded_at`,
`auth_kind`, `seat_stream_id`, `seat_generation`, `credential_id`, `assistant_scope`.
The upload ID is a database lookup identifier, NOT a bearer publication grant.
The upload alone does not post a composite event.

## Verified identity contract

- Seat tokens retain the verified stream owner and actual session generation,
  including seats with operator-equivalent rights. Incomplete/malformed seat
  identity is refused; it is never downgraded to a non-seat principal
- Scoped credentials retain their verified credential ID and assistant scope,
  with a distinct credential principal ID and explicit null seat fields
- Operator auth retains the verified operator principal/credential identity,
  with explicit null seat fields. Operators are never collapsed to a generic role
- The target scope is never substituted for the uploader. Wire-injected internal
  auth fields are removed by the existing server dispatcher. No bearer secret
  enters the provenance record or receipt
- Active upload chunks/reset attempts remain bound to the initiating connection
  and verified identity/generation. A stranger's rejected chunk does not destroy
  the legitimate owner's upload

M3 will prove publication by a distinct authorized publisher against these records;
a scoped uploader still cannot invoke `assistant.publish` in M1.

## Durable provenance and failure states

`v2_attachment_uploads` is an additive Store-owned table. The existing SQLite
worker remains the only DB execution path. A shared POSIX per-digest lock covers
both the DB and file transition; its acquisition is bounded. A pending row is
committed first, then verified bytes are materialized, then the row becomes ready.
Only ready upload IDs are referenceable. Pending-only managed bytes return
`blob_unknown`, including alternate-case digest requests. Existing legacy bytes
are explicitly protected; M1 does not implement or activate any GC.

Replay of the same authenticated identity/request and identical metadata/bytes
returns the same server upload ID, including after restart. Conflicting reuse is
refused. Cancellation joins the in-flight materialization before releasing upload
ownership; cleanup cannot close a recycled file descriptor. A failure before or
after materialization leaves an explicit pending row for M7 reconciliation,
never a misleading ready receipt. Lock files are persistent synchronization
identities, not attachment blobs.

Schema source is delivered, not applied to a live database. Tests use disposable
fixture databases only. Rollback of code leaves additive records intact; do not
drop provenance rows or delete blobs as an informal rollback step. Fleet owns
migration review, activation and the final deploy gate.

## M1 source verification

Current base: `ba8c9119448507b5a3b9c2db78c44b6790ea467a` (only documentation changes
beyond the reviewed `1182bd2f` pin). Existing CI is unchanged.

```sh
python -m pip install -r services/chat-stream-v2/requirements.txt
python -m pytest services/chat-stream-v2/tests/test_managed_attachment_upload.py --collect-only -q
python -m pytest services/chat-stream-v2/tests/test_managed_attachment_upload.py services/chat-stream-v2/tests/test_blob_upload_leg_ownership.py services/chat-stream-v2/tests/test_store_schema_floor.py services/chat-stream-v2/tests/test_scoped_credential.py services/chat-stream-v2/tests/test_send_attachment_ordering.py -v
python -m compileall -q services/chat-stream-v2 services/_shared
```

Local selected gate: 85 passed; 45 newly collected managed-upload cases; compileall
passed. Actual 26 MiB chat upload first returned success (RED) before the running
25 MiB gate; generic/report 26 MiB uploads still succeed. A second RED caught
uppercase digest access bypassing pending-state gating and was repaired. The
selected gate also covers receipt persistence/idempotency, seat/scoped/operator
identity, forged/incomplete identity, wire-field injection, cancellation ordering,
pending-before-file durability, exact media types and existing scope/attachment
controls. This is not a full merge-gate or deployed-runtime claim. Exact-head CI
results and independent review are tracked on the draft PR. Raw local receipts
remain outside the public source tree; their content hashes are below.

- m1-cap-red.log: `7dbf2c088d355090e1f890064f4c82ec0ad0e373439426c43e4ab9ab31e08994`
- m1-pending-case-red.log: `5321865f67c68bb7fde4b53ca692e86549f1323c70a07935ebbae81f89c27520`
- m1-focused.log: `6f447b6565a3526d861dd75d13e8badeabece9b0ce64c49d96fc6fdf9d25ba75`
- m1-collected.log: `3c38e5460bcf7c61ab6c43ae7f9364a78e0b02d913408ba1833314a9738bf384`
- m1-compile.log: `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- dependencies.txt: `34ce759edef7a4c4697e24d6be2e704c2fb300f2041497deccd963cdf5f529a4`
