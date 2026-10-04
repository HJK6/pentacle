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

## M2: receipt handoff CLI (source stage)

`agent-orch send-file sample.pdf` validates a supported file and uploads with
`purpose=chat_attachment`. It prints only the server-issued receipt. It does
not post to a composite. Optional `--upload-request-id` reuses the original
upload request for a retry with identical bytes, metadata, and identity.

An already-authorized publisher uses its actual dispatch context:

```
agent-orch send-file --upload-id <server-upload-id> --to <composite> \
  --dispatch-id <actual-dispatch> --reply-to-message-id <actual-input> \
  --publish-kind result --request-id <existing-publication-key>
```

`--from-receipt '<receipt-json>'` or `--from-receipt receipt.json` is an
alternative carrier of **only upload_id**. Other fields are neither forwarded
nor echoed. The daemon owns provenance resolution and authorization. `--to`
grants no authority. A publisher may instead supply a local path; its upload
receipt is printed before attempting publication and remains available if
publication is refused or the transport fails. No automatic publication retry
is performed; reuse the receipt and stable publication key.

`--evidence-refs-json` forwards existing operation/report receipt IDs required by
non-prose publications; it creates no authority. Successful publication output
retains the daemon event_id and duplicate flag. `--caption` supplies optional text. Prose defaults to the existing
`publish:<dispatch-id>` key and `response_state=final`; other kinds require an
explicit stable request key. `--response-state` and `--reply-to-question-id`
remain available for the existing protocol. No dispatch or input ID is invented.

The path guard checks lexical and canonical credential directories, configured
token-file paths and .env names, before reading; descriptor-relative no-follow
opens refuse symlink replacement, and nonregular files are refused. Reads are
bounded to 25 MiB, with mutation checks. This is location-based refusal only:
it cannot certify arbitrary ZIP/PDF/CAD contents are secret-free. Files are not
executed or unpacked. Unknown response fields and transport exception details
are not printed. Generic blob upload wire frames and send-image are unchanged.

M2 source tests cover all ten extensions, prefix mismatches, real oversize,
symlink escape/replacement, token/receipt locations, FIFO refusal, invalid and
forged advisory receipts, non-seat null-generation receipts, upload replay keys,
nonpublisher receipt preservation, correlation, and both managed/generic upload
wire frames. Server publication and full roundtrip enforcement are **M3 gates**;
M2 unit transport tests are not a deployed end-to-end claim.

Evidence: 56 new CLI cases; 116 selected CLI/M1 regression tests passed, with
compileall passing. Initial missing-command RED and direct-primary final-state
RED were reproduced before their corresponding fixes. Private evidence hashes:

- Initial RED: `7da2fd911bb252d2e8fd4bbce381b54b6425767ed36446ff5d255a577414fd16`
- Direct-prose RED: `829d1dfad7331fe47be0ccc837b7400d9b7b8cdf683484e1af4aa61612db9400`
- Collection: `11d8a66fd11fd1786149694399a6309330c056e64637ffdc7b2ae05adefa0672`
- Selected test log: `1174d7ce395d71e8261f66885342d3d41a257e4a6044f599727721f369635d3c`

M3 preflight exposed two CLI omissions: non-prose operation evidence forwarding
and publication event/duplicate output. Both reproduced RED before repair.

## M3: managed publication and durable references (source stage)

assistant.publish now resolves upload IDs from daemon-owned ready provenance,
uses read_verified, and rechecks canonical type, size and filename. Raw blob
hashes and invented IDs cannot substitute for receipts. Scoped uploads remain
within their original assistant scope. Attachment-only replies use the existing
bounded 16-item envelope; empty posts, caller provenance fields, stale/missing
publisher generation and uncorrelated publications remain refused.

Each published attachment retains distinct uploader auth kind/principal/seat
(or explicit non-seat nulls), server upload time, and verified publisher
stream/generation. Event insertion, publication idempotency and an additive
publication reference row commit in one transaction. A sorted per-digest guard
rechecks ready rows, expected metadata and actual bytes immediately before the
transaction and stays held through commit, preparing the shared boundary for
M7 GC. No GC runs in this milestone. Changing the direct binding does not rewrite
historical dispatch authority or create another card on retry.

The executable test_file_delivery_roundtrip.py runs the exact CLI Step A,
nonpublisher refusal, Step B upload-ID handoff, advisory receipt carrier retry,
and fetch_blob journey for all ten supported extensions. It exercises real JSON
RPC framing/daemon dispatch/blob and SQLite handlers. Networking/hello is replaced
by a synthetic verified connection adapter; this is an in-process gate, not a
claim of deployed transport or device acceptance. Existing auth/scoped regression
tests remain in the selected suite; M6 extends published-file read authorization.

M3 evidence: 31 new publication cases plus 10 CLI/daemon roundtrip cases;
204 selected regressions passed; compileall passed. Tests cover corruption,
missing/pending bytes, mismatched metadata, scope, reference-insert rollback,
concurrent retry and hot rebind. REDs reproduced attachment-only refusal,
raw-hash acceptance, the required stable nonpublisher error, and missing verified
publisher generation before fixes. Fixture setup corrections are not product REDs.

M2 review repair included here: KUBECONFIG is split with os.pathsep, empty entries
ignored, and every listed path is canonicalized. A failing synthetic test proved
that an external first entry previously uploaded; first/second entries and a
symlink alias now refuse before upload. Existing server scope_denied remains
unchanged on the wire; send-file reports that publication refusal as
publish_not_authorized. CLI suite now has 59 cases.

Private evidence hashes:
- m3-publish-red.log: `3459e5b67e2a1be5f3708eb005d37d732077d3eb4b1afba6a0650e5d359c3299`
- m3-generation-red.log: `8254826c596106390f380bc617a80e5a8e11100c46753b76dda73757e85d64e4`
- m3-collected.log: `6c9d458dfeef2b27cd8c8b52110d336aa733e566d5f80d9c306f3882fd42cf12`
- m3-regression.log: `5e27b45e933b6e3417e2804615e334ff70c51466337690779e6f414fcbca9618`
- m2-kubeconfig-red.log: `0bb3f20eda277a2dc009393cacd1fc8a7037dfed52ab1f30ca384dd2aaba7244`
- m2-kubeconfig-green.log: `f6eec9b16af98e64764fadfa47259289059a0f06ab6ede25054452161091e7e8`

## M4: web download and unavailable-state source checkpoint

Supported non-image attachments render an escaped filename, canonical type and
bounded size with an inert download placeholder. The existing authenticated
chatFetchBlob bridge supplies bytes; size and SHA-256 must match before an
object URL/download anchor is enabled. Files are application/octet-stream
 downloads, never PDF/archive/CAD/HTML/SVG previews. URLs are node-scoped and
revoked when nodes are removed; late responses cannot resurrect detached links.
Transient errors offer a retry; the exact blob_unknown refusal displays
"File expired or unavailable" with no href. The shared Electron/web bridge now
preserves that stable error code without exposing arbitrary exception text.
Images keep their existing viewer path.

Actual store/view tests exposed a related shared-core bug: empty ASSIST_TEXT
attachments were filtered as noise. The attachment exemption now includes
ASSIST_TEXT alongside ASSIST. The same change will be carried to mobile in M5.

The existing web-mode CDP gate gains a seeded-file scenario: real authenticated
bridge/daemon fetch, PDF and ZIP object-URL byte-hash equality, and an absent
file's unavailable state. The fixture is created only inside the gate's scratch
DB/blob root. No CI workflow was changed. This browser scenario is pending
exact-head CI execution; DOM tests are not substituted for browser evidence.

Local evidence: 17 new web/DOM cases plus 25 existing display-parity tests pass;
renderer core bundle builds. Seeder readback verifies two exact-hash files and
one intentionally absent file. The inherited M3 recovery test was updated to
use an actual server upload_id, with separate raw-SHA refusal preserved. The
combined ten-format CLI journey now interrupts a partial upload, retries the
same upload request ID on a new connection, publishes, retries and hot-rebinds,
and proves one card plus exact fetched bytes. 252 selected Python tests pass.
The former M3 head's CI failure remains historical until this candidate passes.

Private evidence hashes:
- m4-render-red.log: `721b1d18879be7e20a50a29ddc957d23cb0a60001f7ba3cf9b02d66fec096e88`
- m4-focused.log: `12357f948d0bdc478a7cd25cdff5de745675913926f92fa8e51c143c43a342af`
- m4-build.log: `65ba9956d59c72ca9323cb4a6e511ef7ee6cfda1c5114ff448a053eca3e1d6f6`
- m3-legacy-fixture-red.log: `600e206b05d49096e27c88558e6e347066483c3731d74a522b32bead3f1b3b4e`
- m3-review-repair.log: `6839f1893d54d52e5aa48488de6de846c1052775c62b971b5f535f7476fa9498`
- m4-python-regression.log: `ef84bcf84db36cde21ac063c9de8034df82f0ab4e93e7af04244d125cbd832da`
