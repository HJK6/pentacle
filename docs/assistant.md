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

## Validation

`services/chat-stream-v2/tests/test_assistant_role.py` covers default-off behavior, ordinary close protection, role authority, concurrent activation, idempotent replay, pending intents, managed dead-session handoff, retained recovery entry, scheduled admission and forged managed-close fields. Existing role, handoff and scheduler suites cover their surrounding contracts. Run the daemon merge gate and each client's documented gate before activation. A live provider/installed-client readback remains distinct from deterministic fixture evidence.

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
