# Portable communication contract v1

This is a pure library with synthetic conformance fixtures. It does not import a
provider SDK, load credentials, persist data, change deployment configuration, or
wire a deployed reader. The receiving owner owns private identity mapping,
evidence authentication, transactions, protected storage, TTL, and real adapter
validation. Passing these tests is not deployed or transactional proof.

## Parser (CG1)

`parse_sections(contacts, ParserConfig)` returns one contact record with
`contact_id`, `valid: bool`, `reason` (`parsed` or `malformed_grant`), and `grants`.
Contacts use `id` plus `heading`/`lines`, or a single-element `sections` list.
Both headings, repeated headings, duplicate contact IDs, or any malformed line
invalidate that whole contact. The only headings are `SMS grant` and
`Communication grant`.

`ParserConfig(email_default_contacts: tuple[str], default_voice_token='as_agent',
legacy_outreach_scope='legacy_outreach')` must come from trusted private intake.
Only the public default voice token is accepted. The owner maps these public
aliases losslessly to private vocabulary; this library does not change grants.

Each grant contains `contact_id`, normalized `line_id` (`contact:mode:scope`),
`mode`, `original_mode`, `scope`, exact `line`, exact `handling`, `channels`,
`voice`, `autonomy`, and nullable `authority`. The line grammar is
`- mode scope [optional qualifiers]: handling`; colon and one syntax space are
required. Everything after that one space is preserved, including additional
spaces and newlines. Modes are `answer`, `outreach`, `change`; scope is
`[a-z][a-z0-9_]{0,63}`. A configured legacy answer scope normalizes to outreach,
without changing original bytes. Qualifiers have unique keys among channels,
voice, autonomy, authority. Channels are sms/email/botcomm, explicit channels
replace defaults, and duplicates/empty values/trailing commas are invalid.
Autonomy defaults to send; change remains action metadata, never egress.
Operator voice requires a valid calendar date plus a single identifier.
Syntactic authority is not authenticated authority.

## Local checks

From the repository root, use the base requirements and editable agent-orch
package in an isolated Python 3.13 environment. Run:

    python -m pytest services/chat-stream-v2/tests/test_comm_contract.py -q
    python -m pytest services/chat-stream-v2/tests/test_comm_contract.py --collect-only -q
    python -m pytest services/chat-stream-v2/tests/test_send_receipt_late_proof.py -q
    python scripts/test_check_public_residue.py

Only the four new contract, test, fixture and documentation files are required.
All fixture records are made up; addresses, when present, use `.invalid`.

## Authenticated evidence boundary

`TrustedContext(resolver, now, proofs)` has an aware RFC3339 clock string and a
unique tuple of `(purpose, opaque_reference)` pairs. The receiving owner supplies
the trusted resolver object, which implements `resolve(reference, purpose)`.
It authenticates the reference against its original issuer, checks current
revocation/versions, and returns an `Evidence` or `None`. A message body, path
label, `authenticated=True`, arbitrary dataclass, or caller-supplied dictionary
cannot replace that lookup. Choosing the trusted resolver is an application
security boundary, not a sandbox against a hostile Python caller replacing code.

`Evidence(kind, subject, version, issued_at, expires_at, data_json)` freezes a
canonical JSON object. The library requires correct purpose and request subject,
well-formed version, and `issued_at <= now < expires_at`. A missing, expired,
conflicting, malformed or unregistered proof holds. The owner is responsible for
the trusted clock. `receipt_proof.epoch` validates timestamps; comparisons use
exact decimal seconds and keep original strings, never float ordering.

CG2 purposes, all scoped to the trusted request ID:

- request: request_id, send_id, original_utc
- identity: account_ref, sender_ref, recipients keyed by normalized destination;
  each entry has contact_ids (exactly one), channels, recorded_email, bot_type,
  status. An email address must be recorded even with explicit email permission
- source: current commit (40 lowercase hex), status=current, contacts and parser
  config. Supplied parser output must equal a fresh parse of this verified source
- capability: enabled channels, content_allowed, bot_allowed, operator_voice,
  contacts, setup_ref. Enabled flags are authenticated data, never naked inputs
- workflow: classification=ordinary, mode, scope, inbound_id/inbound_contact_ids
  for answer or dispatch_ref for outreach
- actor: actor_ref and account_ref
- config: default_voice_token=as_agent; version matches the frozen envelope

## Logical envelope and shared admission (CG2)

`bind_envelope(dict, wire_bytes=None)` returns immutable
`BoundEnvelope(logical_json, digest, wire_digest)`. `envelope_digest` revalidates
its canonical form and SHA-256 rather than trusting its supplied digest.
Malformed envelopes raise `ContractError`; admission converts invalid input to
an unadmitted result. All envelope fields are required:

- schema_version=1, channel (sms/email/botcomm), account_ref, sender_ref
- recipients: nonempty list of contact_id, role (to/cc/bcc), normalized destination
- exact subject, body, text, html, introduction strings
- mime_parts: ordered objects with content_type and exactly one of exact text or
  canonical standard base64 bytes
- protected_refs and references: ordered reference lists; nullable thread_id/reply_id
- attachments: ordered sha256, size (nonnegative integer), media_type, name_ref,
  content_ref descriptors; their hashes bind actual bytes through owner validation
- voice, voice_config_version, nullable voice_authority_ref

Canonical JSON is UTF-8, sorted keys, compact separators, no NaN, unescaped
Unicode. Only within-role recipient order and identical recipient triples are
normalized. Different roles remain different. No payload, Unicode, newline or
whitespace normalization occurs. Compose final payload and SMS introduction
before binding. Adapters must not append text afterward. Wire bytes, if available,
have a separate optional SHA-256; generated MIME boundaries are not asserted by
the logical digest and cannot replace authorization. Addresses and low-entropy
hashes remain protected metadata.

`admit(bound, context, parsed_grants, capabilities=None, approval=None)` returns
`admitted`, `reason`, observed `versions`, and exact admitted `snapshots`.
`capabilities`, when supplied, is the same opaque capability proof reference.
All recipients must independently pass. Names/body text supply no identity or
scope. Answer requires authenticated inbound linkage; outreach requires trusted
dispatch. Change is action metadata only. Source/config/identity mismatches,
missing versions, disabled channels, email address restrictions, bot restrictions,
draft autonomy or unmatched content fail closed. Each snapshot retains original
line/mode and normalized line ID plus source commit/version.

## Durable draft and exact approval commands (CG3)

`Command(kind, request_id, body_json, conditions_json)` is immutable and content
addressed by `command_id`. The owner must atomically enforce every condition and
return an authenticated acknowledgment naming that exact ID before dependent
work. A returned command means proposed work, never committed success.

`draft_command(bound, context, decision)` returns status, reason, commands, draft.
The trusted request derives stable draft/handoff IDs and supplies original time
and optional positive draft_version. `put_draft_and_handoff` atomically persists
one draft and pending marker, with create-or-identical and prior-version checks.
Only a verified `draft` proof carrying the exact committed command ID unlocks a
`request_handoff`. Handoff retries keep the same ID until an authenticated proof
acknowledges that handoff command. No branch returns a transport command.

The authenticated persistence proof must declare status available/unavailable,
restricted=true, atomic=true and policy_version=1. Unavailable storage proposes
`stage_draft` with restricted atomic semantics. A verified stored stage remains
unadmitted; failed staging holds explicitly. Recovery repeats draft/intent
persistence only. A missing persistence proof is a hold, not assumed staging.

Draft records contain draft_id, request_id, version, exact envelope JSON/digest,
state=draft, safe reason token, handoff_id, observed versions, and created_at.
These records are protected data. They are not log or preview output. The owner
must reject conflicting versions/content and resolve edits through a new trusted
draft version rather than silently changing an approved record.

`approval_binding(bound, context, draft, event_ref=None)` requires the committed
draft and an authenticated `operator_event`: event_id, origin=operator,
original=true, occurred_at, action, actor_ref and the exact draft_id,
draft_version, envelope_digest, channel, account_ref, recipients and observed
versions. It returns a `bind_approval` command; the command itself is not consent
proof. Only the owner's committed available `approval` proof can admit an exact
one-shot. Repeated original events are idempotent. Canceled, revoked or consumed
approvals reject. `cancel`/`revoke` produces `cancel_approval` with unclaimed-only
conditions; it cannot recall an already claimed send.

One-shot is narrowly limited to default-voice unmatched or draft-autonomy
content after all identity/channel/content/bot/workflow restrictions pass. It
cannot reinterpret a malformed contact, add transport permission, authorize
change metadata, enable operator voice, or edit standing grants. Each envelope
component and all authority versions remain bound. Claim will atomically consume
the approval even when provider acceptance later becomes unknown.

## Canonical lifecycle and enforcement (CG4)

`create_intent`, `claim_attempt`, `record_result`, `reconcile_receipt` return
status/reason/commands/record plans. All storage and provider operations remain
injected. `intent_pending`, `claim_pending`, `result_pending` and
`reconciliation_pending` are proposals, never acknowledgments. The owner must
commit each command atomically and return its exact authenticated readback.
An accepted provider result is not proof of delivery.

One logical table contains canonical rows, associations, attempts, request
pointers, published enforcement versions and expired-request barriers. Canonical
keys are `CONTACT#<first-lexicographic-contact-id>` and
`SEND#<exact-original-UTC>#<send-id>`. Other contacts get pointer-only associations.
An unresolved draft uses `CONTACT#UNRESOLVED#<draft-id>` and cannot be claimed;
resolution requires an explicitly trusted new draft version, newly bound envelope
and new approval. Draft updates condition on previous version and digest.

Canonical v1 rows carry:

- send_id, request_id, optional draft_id, version, original_utc, pk/sk, revision
- channel, account_ref, sender_ref, recipients with contact_id/role/destination_ref,
  direction, mode/scope, voice/config/authority references, actor_ref
- grant_line_ids, authority_ref, operator_dispatch_ref, observed_versions
- envelope_digest, optional wire_digest, preview_policy_version, protected
  content/subject/snapshot/attachment refs, attachment_count, source_expires_at
- provider_message_id/provider_thread_id, thread_id/reply_id/references
- state, attempt_id, acceptance_at, delivery_state, reconciled_at, terminal_at,
  receipt_conflict, receipts (protected receipt metadata)

Exact admitted lines/original modes/source commit/versions and one-shot binding
live in the immutable protected snapshot named by snapshot_ref. Canonical
recipients contain protected address references, never address strings. The
owner's protected proof binds those refs and attachment refs to the exact
envelope. Copying an envelope or snapshot into a preview/log is prohibited.

Creation is one conditional transaction for canonical row, associations, snapshot
and `REQUEST#<trusted-request-id>` pointer. Same-request changed digest conflicts.
`replay` proof must establish newness via the trusted issuer reference or identify
the exact existing canonical key and digest. Missing canonical data never proves
newness. Missing/unknown replay status holds. Expired/consumed request status
returns `expired_request_rejected` before any intent/claim/send.

Additional owner-resolved purposes:

- persistence: available, restricted, atomic policy version 1
- preview: policy_version=1 and exact fields channel/direction/state/attachment_count
- protected: access=allowed, exact envelope digest, content/subject/snapshot refs,
  ordered attachment and recipient refs, immutable_snapshot=true,
  retention=owner_enforced, optional source_expires_at
- publication: committed transaction_ref, versions, pending=[], tombstones=[]
- attempt_control: open/canceled disposition bound to envelope digest and committed
  transaction_ref; its version is checked atomically at claim
- clock: exact now matching context, source=trusted_utc
- intent: current canonical record, original creation Command, committed command
  ID and transaction_ref; immutable creation fields cannot change
- claim: exact committed claim Command, command ID and transaction_ref
- provider_result: attempt/digest, accepted/rejected/timeout/ambiguous outcome,
  occurrence time and accepted provider ID/delivery state
- receipt: receipt/attempt/provider IDs, digest, acceptance/delivery and occurrence

`publish_version(context, kind, publication_ref=None)` emits a conditional
publication for a verified refresh with previous/new version, target proof,
validated status and tombstone flag. Private refresh cannot acknowledge success
before that publication commits. Pending/failed/tombstoned publications hold.
Claim re-reads admission and protected-access evidence, verifies unchanged exact
historical snapshots, and condition-checks all observed enforcement versions,
request pointer and canonical revision. One-shot consumption is atomic with the
claim. Current approval state and historical approval acknowledgment are distinct:
replay preserves the original command while current revoked/canceled/consumed or
conflicting state denies. Boolean/null/duplicate evidence slots do not prove absence.

Lifecycle is ready → attempting → accepted/rejected/acceptance_unknown. A crash
before claim has no transport effect. A committed claim remains consumed after
crash, timeout, ambiguous provider outcome or accepted-but-write-failed result.
No automatic resubmission exists. A deterministic rejection cannot be reclaimed
without a new explicit authorization; the receiving owner owns that decision.
Receipt reconciliation only changes evidence, never emits send/prompt/cancel.
Accepted state never regresses. Older weak delivery cannot override verified
final delivery. Contradictory same-provider acceptance or delivery produces
unknown delivery and conflict, preventing terminal expiry or a retry.

`retention_policy(record, context)` authenticates the exact persisted record.
Rejected/canceled or accepted with resolved final delivery may expire; draft,
held, unresolved, ready, attempting, acceptance_unknown, pending/unknown delivery
and receipt conflicts do not. Terminal metadata and immutable snapshot expire at
exactly 365×86400 seconds from recorded terminal transition, preserving fractional
precision. Provider occurrence time is retained separately. Source content
retention is independent and never extended. The returned metadata-only expired
request barrier must be persisted before paired TTL deletion; actual TTL/access
and barrier atomicity remain receiving-owner obligations.

`staging_cleanup(context)` emits deletion only after authenticated canonical
acknowledgment matching the staged envelope. Failure retains protected staging;
retries repeat only deletion, never prompt/send/cancel. A cleanup acknowledgment
must name the exact deletion command.

## Final voice composition (CG5)

Call `compose_voice(unbound_dict, context, parsed_grants)` before binding. It
returns composed/held, reason, final envelope and configuration evidence. Already
bound payloads are refused. The public synthetic default composition signs body,
text and HTML as `Synthetic assistant`, with the same SMS introduction; repeated
composition is stable. Admission requires the final default signature and
channel-appropriate introduction, so skipping composition cannot silently send
an unsigned fixture message. Private signature/wire mapping belongs to intake.

Operator voice keeps the authored first-person fixture text exact and removes
the assistant introduction. All three conditions are required, in this order:

1. `voice_authority` resolves through the owner ledger and matches dated grant,
   account, contact and frozen voice_authority_ref; otherwise authority_missing
2. Capability explicitly enables operator_voice and references an authenticated
   provisioned `voice_setup` for this account/channel/capability version;
   otherwise operator_voice_disabled (the default)
3. Contact is in the trusted capability contacts; otherwise contact_not_enabled

Malformed grant syntax remains malformed_grant from CG1. Default-voice one-shot
approval never bypasses these checks. The successful snapshot retains exact dated
authority and row voice/config evidence; claim checks authority/setup versions.
No function enables a deployment channel or changes real configuration.

## Adapter path plans (CG6)

`gate_path(path, bound, context, grants, approval=None)` returns the next
conditional command, status/reason, protected record, and ordered labeled traces.
`prior_trace` is authenticated, already-observed history; `next_trace` contains only
new proposals; `trace` concatenates the two. The receiving adapter retains the
prior history once and records only effects it actually commits or observations
it makes. It must not execute either trace as commands. Conformance compares the
retained history plus executed effects with the fixture base, including exclusion,
cleanup, cancellation and receipt-only recovery branches.
It never invokes a provider/browser/AppleScript, even for enabled paths. Prior
inbound prompt labels are read from authenticated path evidence and retained as
history; they are never replayed as new effects.

Covered synthetic paths are SMS desk, three host-neutral legacy SMS routes (primary/analytics/chat),
email helper/CLI, analytics email, botcomm and bot direct handoff. Their snake-case
IDs are listed in the fixture. Disabled IDs cover personal Google Voice direct
and queue consumer, WhatsApp, iMessage, Apple Mail direct send and voice calls.
Disabled paths propose a durable held draft/handoff or restricted staging and
cannot propose provider/browser/AppleScript or success-receipt effects.

The authenticated path proof binds path/channel and enabled state, source
availability/access, base trace and already-committed prompt/cancel observations.
New intent proposes storage only. Verified intent precedes current-version claim;
verified claim precedes a possible single provider invocation. Cleanup follows
canonical acknowledgment and retries only deletion. Receipt/result recovery is
chosen before current grant/voice/source availability checks and returns only
receipt commands. A cancellation before claim conditionally cancels the ready
row; after claim it cannot recall or resubmit.

`invoke_provider_once` requires a receiving-owner `dispatch_activation` bound to
the exact committed claim, attempt and live execution invocation. The trusted
`execution` proof must still be live and match the invocation recorded in the
claim. Fresh authenticated clock evidence is checked again at activation before
source expiry is evaluated. The effect adapter must atomically consume both activation and attempt,
check invocation liveness, then perform at most one fake/real effect. It must
never mint/reuse an activation for a recovery invocation after a post-claim crash.
A crash after consumption but before transport remains consumed. Replaying a
stale proposal or reissuing a different activation for the same attempt must fail
at the adapter. These are proven by the in-memory double, not by this pure module
or a distributed storage implementation.

Delegated assignment communication and provenance-verified business product
texting are separate owner workflows. A body, path name or boolean cannot select
them. An authenticated workflow classification must reference matching exclusion
proof with owner/workflow identity. The result is
`excluded-with-owner-evidence`, with no generic transport command. No coverage of
those private workflow implementations is claimed.

Trace categories include prompt/handoff, storage/claim, provider, cancellation,
receipt, cleanup and explicit hold. Tests map the earlier units' actual malformed
parser/contact, identity/envelope/wire, approval/cancel, external-input/version,
request-pointer, retention, preview, staging, voice and receipt mutations into
path traces. A denial preserves earlier recorded effects and prevents later
success effects. Combined fixtures own/close all memory stores and record zero
real sends.

## Read-only thread projection

`project_thread(rows, context, cursor=None, limit=50)` authenticates an exact
history snapshot and its source versions/access/expiry. It returns status,
metadata rows, named missing_sources, an opaque snapshot-bound cursor, and safe
plain-text fixture CLI output. Policy version 1 is mandatory. Invalid metadata
schema or unknown policy holds; unavailable/denied sources yield explicit
partial_history, including sources that supplied no rows.

History rows identify source_type/source_record_id/stable_id and source version,
channel/account/provider ID, original UTC, direction/state/delivery,
attachment_count, historical normalized grant_line_ids, thread/reply/references,
content/snapshot disposition, optional source/metadata expiry and To/CC/BCC role.
Only documented metadata is projected; raw body, subject, attachment names,
addresses, hashes, grant lines and hostile markup are never copied to output.
BCC suppresses line IDs and link counts. Protected, expired and unavailable
content produces fixed safe placeholders. A paired terminal-expired row exposes
only its expired placeholder, not historical audit fields. Unresolved/unknown,
nonterminal and conflict rows ignore invalid terminal TTL claims and stay visible.

Dedup keys are (channel, account, provider_message_id), else
(source_type, source_record_id). Deterministic mirrors use the exact instant,
source_type and stable_id order. Exact provider/thread/reply references link
messages within their channel/account; unresolved identities are never guessed.
Fraction arithmetic preserves every timestamp digit. Chicago display includes
UTC offset, including repeated DST wall times. Cursors bind the authenticated
snapshot reference/version and full exact ordering tuple; they are protected
control metadata, not anonymous public identifiers or log output. CLI output
combines SMS/email and separately normalizes botcomm.

## Decisions for review and boundaries

- A normalized recipient identity includes its normalized channel destination;
  distinct addresses are preserved, identical role/contact/destination triples
  collapse, and moving between roles changes the digest
- One-shot does not override malformed whole-contact records or channel-specific
  permission; it only covers unmatched/default-voice draft-autonomy content
- Rejected attempts return explicit_attempt_authorization_required. This library
  deliberately does not implement a retry/resumption writer. The packet's named
  lifecycle has no rejected-to-ready transition; private intake must obtain a
  new explicit authorization before any separately designed retry flow
- Receipt conflicts stay sticky and nonexpiring under ordinary callbacks. No
  conflict-clearing or special resolution-proof protocol is invented here
- The four new files provide portable contracts and synthetic conformance only.
  Private parser integration, real grant compatibility, transactional/cloud proof,
  transport restoration, actual operator setup and deployed readbacks remain
  receiving-owner obligations. Existing runtime readers/imports are unchanged

### Retention and projection repair clarifications

Draft/staging records carry content_disposition, content_ref and the exact nullable
source_expires_at. Only current authenticated protected evidence permits a payload
copy. Expired, denied or missing evidence produces a metadata-only held draft with
envelope_json=null. It cannot be approved. Persisted payload copies must obey the
source deadline independently of the nonexpiring draft metadata. The provider
command binds the shortest historical/current source deadline, current protected
version and live invocation; the effect adapter rechecks them immediately before
consumption/effect. Holding an expired claimed attempt never reopens it.

Operator composition rejects assistant-signed payloads rather than silently
rewriting authored text. Operator admission also rejects an assistant introduction
or signature. All three invocation IDs (execution, committed claim and activation)
must be valid nonempty identifiers, not merely equal null values. Canonical
cancellation requires committed control evidence bound to the exact envelope.

History source versions are mandatory. An unavailable, denied, expired or
unversioned source is reported even when it returns no rows. BCC and unresolved
identity suppression is independent of content placeholders, and BCC rows cannot
contribute hidden identities to other rows' link counts. Mirrors propagate the
strictest privacy/expiry disposition before deduplication; conflicting metadata
returns a fixed conflict placeholder. Duplicate ordering identities hold rather
than produce unusable cursors. Snapshot-bound cursors are opaque one-way control
tokens and never encode expired timestamps or source/stable IDs as readable text.

The stateful synthetic adapter implements durable draft/handoff, staging failure
and recovery, canonical creation, claims, one-use provider activation, cancellation,
receipt writes and deletion-only cleanup. Per-path tests execute these transitions,
verify same-ID retry behavior and compare successful effect traces to each fixture's
base trace. Earlier mock effects remain recorded on failure; no later success
receipt or send is substituted for a failed storage/handoff/cleanup operation.
