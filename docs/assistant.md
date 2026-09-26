# Optional persistent assistant

The assistant is an ordinary top-level session identified by a configured role. It uses the existing chat view, transcripts, reports, questions, spawn reservations and handoff. This option adds no service, database or model router. It is disabled by default.

## Configuration

Set `PENTACLE_ASSISTANT_ROLE=assistant` in the daemon's private environment to protect that role on its local host. Set `features.assistantRole` to the same nonempty slug in each participating desktop/mobile private configuration. Leave these absent or empty for ordinary behavior. Public examples ship without an active assistant role. Restart the daemon after changing its environment and rebuild/reload clients according to their existing configuration workflow. Do not commit personal instructions, machine identities, credentials or context into these public repositories.

Clients pin an exact role match ahead of the normal attention order while retaining its attention indicators. They hide existing delete and rename controls and refuse local deletion and rename attempts. The mobile assistant row does not respond to sideways swipe gestures. Other sessions retain their existing behavior. A mismatched or disabled client may still show a delete action; the enabled daemon remains authoritative and returns `close_protected`. That error is terminal and must not enter a retry loop.

## Activation and context

Use the existing authenticated operator or trusted service spawn interface to create a top-level session on the daemon host with the configured role, chosen provider/model/effort, and private initial instructions. An ordinary agent token cannot grant or remove the protected role. Operator/service role changes use the existing role-set interface. The role does not confer general orchestration authority.

Private deployments may place an `agents/<role>_baseline.md` in their configured memory root so the existing orchestration CLI prepends it on spawn. The daemon does not invent or manage personal memory. The bootstrap should load a compact private context file and link existing work/receipt records. Record new commitments before acknowledging them, and retain dispatch IDs so a successor checks delivery evidence before retrying. A conversation checkpoint is not evidence that a project completed.

## Continuation and failure

Exactly one assistant holder is admitted, except for the bounded overlap during an existing managed handoff. Concurrent activation and role grants are serialized; unresolved persisted spawn intents also prevent a duplicate activation. Repeating the same spawn request keeps existing idempotent replay semantics.

The current assistant may use its verified token for `spawn --handoff`, preserving the role and staying on the daemon host. Authenticated operator recovery uses the existing spawn handoff fields with the prior stream as `handoff_from_stream_id`. Successful launch uses the existing managed predecessor close. A successor should wait until it is the sole open holder before writing shared context or dispatching work. If cleanup is uncertain, inspect the existing receipt and predecessor instead of starting another replacement.

Ordinary operator, self, idle-reap and sweep closes are protected. Only internal `handed_off` and `spawn_rollback` close kinds bypass protection; a client-supplied `close_kind` cannot authorize deletion. Confirmed pane death leaves the protected row open with death evidence so its existing client entry remains available for recovery. Recovery is explicit; there is no new automatic restart loop. A daemon restart reconstructs the same session and pending-intent state.

Existing owner-authorized scheduled handoffs can provide a future continuation. Assistant authority is checked at schedule admission; internal dispatch uses the scheduler's trusted identity. Track the accepted schedule receipt and keep obsolete wakeups cancelled. A locally hosted model alone does not provide offline phone access.

## Fleet lifecycle authority

By default no session may close or reparent a session it does not own: close stays limited to self, direct parent and the authenticated operator; reparent to the worker's current parent or a live handoff successor of that parent. Naming yourself the new parent confers nothing, so a top-level stream, including a protected assistant, has no ordinary reparent owner. The operator may name one existing seat as the fleet lifecycle manager. The daemon keeps a single durable grant (`stream_id`, exact `session_generation`, monotonically increasing revision) with append-only audit and per-actor receipts. No role, title, lineage, configuration entry or environment variable confers it.

- **Designate / replace / revoke:** `assistant.lifecycle` creates a phone approval challenge and returns `consent_pending`; it never directly changes the grant. The web menu requests approval on the phone. `agent-orch consent request designate --target <stream> --reason <why>` or `consent request revoke --reason <why>` requests the same journey. A verified seat may request, but only a current operator-authenticated mobile credential owning an active audience key may approve. Each challenge binds the action, exact target generation, expected revision, requester, audience, nonce and expiry. The app signs the daemon bytes with its enrolled approval key; one Store transaction verifies the signature, mutates, consumes and audits. A chat answer or ordinary notification answer is never approval.
- **Transfer:** disabled (`authority_transfer_disabled`) in this initial mode. To change holders, request a new phone-approved designation. `agent-orch lifecycle inspect [--target <stream>]` shows the grant, audit and eligibility. `agent-orch consent status|cancel <challenge-id>` reads or cancels a challenge; deny is admitted only from a current audience mobile device. Approval/deny do not participate in reconnect replay. Exact signed-tuple approval retry returns the stored receipt; a different tuple conflicts.
- **Eligible recipient:** an open session with a positively observed live pane and `ready` bootstrap (unknown state fails closed), not offline or presumed dead, whose daemon-recorded role is `lead`, or the protected assistant role. Eligibility, target generation and expected revision are re-checked inside the mutation transaction. Designation never changes the recipient's role, credentials, parentage or composite routing.
- **Manager close:** the holder may close a non-child only with an explicit `--reason` (CLI defaults are refused) when the target has a terminal report for its current generation, no live children or pending child spawns, is not protected, is online and is observed idle. These fences, and the grant itself, are re-checked under the target's lifecycle lock immediately before the kill. An `admitted` audit row is written before any kill; an audit failure refuses the close.
- **Manager reparent:** allowed for the holder with an explicit reason; protected assistants and cycles are refused. All reparents validate ancestry and write under one daemon graph lock, which a manager close also holds from admission through the kill. Consent approval, emergency revoke, the handoff carry and every manager close/reparent (admission, audit and effect) hold one daemon authority lock, so a revocation or replacement takes effect before or after an admitted manager action, never during it. Lock order is authority, then the target's lifecycle lock, then the graph lock; nothing holding an inner lock acquires the authority lock. Once a manager action holds its locks, admission, effect and outcome audit (`applied` or `refused`) run to completion even if the request is cancelled; the caller sees the cancellation only after the outcome row commits. Cancellation while still waiting for the locks leaves no effect.
- **Handoff:** a successful protected-assistant handoff moves the grant to the successor only when the source is the current holder and the successor is eligible. Live and scheduled own-token handoffs preserve the same protected role and emit an informational continuity card; a revoked or replaced source grant is never carried. A retired source may afterwards replay only its own stored handoff receipt; the CLI caches the exact request (owner-only file, no credential) and resends it without reading the fleet.

Audit rows record the verified actor (`operator:<credential id>` with no generation, or the seat's stream and generation, as `manager` when it holds the grant) and the grant revision in force; refused attempts by verified seats are audited too. Caller-supplied actor fields are ignored. Reason and request id are bounded, control characters stripped and credential-shaped runs redacted; an unauthenticated caller's free text is not stored. Approved revocation and replacement take effect immediately and survive restart; a reopened stream name gets a new generation and does not inherit the grant. A code rollback leaves these tables unread; revoke through the operator path first if the prior runtime must not see a live grant later.

## Validation

`services/chat-stream-v2/tests/test_lifecycle_authority.py` covers lifecycle mutation fences, disabled transfer, revocation, manager close/reparent fences and retired handoff replay. `services/chat-stream-v2/tests/test_assistant_role.py` covers default-off behavior, ordinary close protection, role authority, concurrent activation, idempotent replay, pending intents, managed dead-session handoff, retained recovery entry, scheduled admission and forged managed-close fields. `services/chat-stream-v2/tests/test_consent.py` covers enrolment, signed challenges, refusal, rollback and revocation linearization. Existing role, handoff and scheduler suites cover their surrounding contracts. See [phone approval enrolment and recovery](REMOTE_AUTH.md#phone-approval-keys). Run the daemon merge gate and each client's documented gate before activation. A live provider/installed-client readback remains distinct from deterministic fixture evidence.

## Composite authority and routing

For the optional `assistant_composite_v1` chat, the local classifier routes work
admission, prioritization and cross-project coordination as `new_topic` to the
configured authority backend. Delegated uncertainty about priorities does not
require an intermediate conversation turn. Unresolved consent still requires
clarification; routing itself never grants execution permission.

The current configured authority may admit and publish against a resolved,
authenticated operator input even when that input was initially routed to the
conversation backend. This standing authority does not extend lead-only
question or terminal-report permissions. Original input/dispatch correlation,
current session generation, lane scope, expected versions and publication
evidence remain required. Decision/terminal wakes use the authority admission
receipt to recover context, independently of the original router destination.


The configured conversation backend can request authority coordination with
`agent-orch assistant operation --operation authority.request --request-id <stable-key> --composite-stream-id <composite> --dispatch-id <original-dispatch> --payload '{"reason":"Coordination needed"}'`.
This operation accepts only a bounded reason on a resolved authenticated operator
dispatch owned by the current conversation backend. It atomically records one
existing-outbox notice per original dispatch, preserving literal input, IDs and
bounded routing context. It does not change the route owner or grant authority.
A different request key for the same dispatch conflicts; an identical replay
returns the current original notice receipt. Queued is not delivered. A changed
or unavailable configured authority generation leaves an inspectable failed
notice instead of silently redirecting to a replacement.

Routine hidden-backend ingress reports `persisted` and
`submission_confirmed=false`; it is not provider delivery. A suppressed queued
notice ends as persisted-only rather than retrying a wake. Composite lane
inventory is not the global work inventory: an empty list does not prove no
work, owner or progress. Status summaries need correlated current evidence;
when it is insufficient, the conversation backend requests authority review
and the authority publishes its own response.

## Progress and decisions

A currently bound lead can send a concise user-directed update through
`assistant publish --publish-kind prose` using its original dispatch and input
IDs. This commits a normal chat event; it does not wake either conversation or
authority backend. The lead should identify the subject in its text. Raw tools
and internal logs remain in the lead's own transcript. Use the conversation
backend for requested synthesis, not as a mandatory relay for each update.

For a blocker or question requiring judgment, the lead first sends the existing
`lane.decision` operation with `transition=wait`, the current lane version and
existing operator-basis IDs. An optional `reason` carries the decision needed,
relevant evidence and recommended next action (nonempty, at most 1,024
characters). It is stored in the immutable receipt and JSON-encoded into the
single existing authority wake. It adds context, never consent. Identical
replays do not wake again; changing the reason under the same key conflicts.

The authority resolves what it can within the existing grant. When the operator
must decide or act, it instructs the current lead to open the existing durable
question in the composite chat. This ordering is a commission/baseline rule;
it does not add a second approval store or change question authorization.
The answer remains bound to the current lead, which continues within the grant.

A lane already in `waiting` cannot transition to `waiting` again. Reconcile the
same receipt for the same blocker; for a distinct decision while waiting, send
one explicit correlated decision notice through the existing peer channel. Do
not manufacture a start/wait cycle. Completion retains the existing one-time
terminal report and authority acceptance path.

Existing owners outside the composite keep their parentage and grants. A report
from a parentless or differently parented owner does not automatically notify
the authority that commissioned it by ordinary send. Such a commission must
request one explicit completion notice with its durable report ID. Routine
progress should remain in the owner's visible chat unless a supported
composite publication binding exists. No duplicate owner or reparenting is
needed solely for presentation.
