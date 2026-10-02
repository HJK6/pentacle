# Optional persistent assistant

The assistant is an ordinary top-level session identified by a configured role. It uses the existing chat view, transcripts, reports, questions, spawn reservations and handoff. This option adds no service, database or model router. It is disabled by default.

## Configuration

Set `PENTACLE_ASSISTANT_ROLE=assistant` in the daemon's private environment to protect that role on its local host. Set `features.assistantRole` to the same nonempty slug in each participating desktop/mobile private configuration. Leave these absent or empty for ordinary behavior. Public examples ship without an active assistant role. Restart the daemon after changing its environment and rebuild/reload clients according to their existing configuration workflow. Do not commit personal instructions, machine identities, credentials or context into these public repositories.

Clients pin an exact role match ahead of the normal attention order while retaining its attention indicators. They hide existing delete and rename controls and refuse local deletion and rename attempts. The mobile assistant row does not respond to sideways swipe gestures. Other sessions retain their existing behavior. A mismatched or disabled client may still show a delete action; the enabled daemon remains authoritative and returns `close_protected`. That error is terminal and must not enter a retry loop.

## Bootstrap your own assistant

Run this from a public checkout with its Python dependencies and candidate `agent-orch` installed, tmux and a working provider CLI. In every shell used below, activate the installed environment (`source .venv-dev/bin/activate` after [developer installation](developer_onboarding.md#1-install-prerequisites)), or use that environment's Python executable explicitly. The provider account must already be logged in and support your explicit model/effort; check the installed `agent-orch models` catalog as well as your account. The assistant's name (for example Nova), physical host label (`local` here), provider/model and protected task role (`assistant`) are distinct. Keep instructions, process memory, credentials, config and stores outside the checkout. [Agent setup](../AGENT_SETUP.md) covers ordinary installation; this wrapper adds the named protected backend and exact composite configuration.

1. Prepare a private working directory and a compact instructions file. The example paths below are operator-chosen variables, not host defaults. Do not overwrite existing files. Include references to your private `soul.md`, process `MEMORY.md`, active work and commitments; the soul owns local paths/capabilities, while memory owns facts/preferences/decisions. Copy [the process kit](../process/README.md) separately when desired, and configure the daemon's `PENTACLE_MEMORY_ROOT` and CLI's `AGENT_ORCH_MEMORY_REPO` to that copy. Role discovery uses `agents/assistant_baseline.md` in the CLI memory root when you author one privately; the bootstrap passes the explicit instructions file to initial spawn without modifying it.

```sh
PRIVATE_WORKSPACE="$HOME/pentacle-private/assistant"
PRIVATE_STATE="$HOME/pentacle-private/state"
INSTRUCTIONS_FILE="$HOME/pentacle-private/instructions.md"
OPERATOR_CREDENTIAL="$HOME/pentacle-private/operator.token"
mkdir -p "$PRIVATE_WORKSPACE" "$PRIVATE_STATE"
chmod 700 "$PRIVATE_WORKSPACE" "$PRIVATE_STATE"
# Create your instructions privately, then set these two roots if using the kit:
# export PENTACLE_MEMORY_ROOT="/absolute/path/to/private-process"
# export AGENT_ORCH_MEMORY_REPO="$PENTACLE_MEMORY_ROOT"
```

2. Issue a `pentacle` operator credential using the daemon owner's registry. This registry must be the same one read by the daemon (the default registry is home-local). The command prints secret material, so redirect it into a private file and extract only `code`; never paste/log its output. Run once for a fresh credential destination, preserving any existing enrollment. Use [operator authentication](REMOTE_AUTH.md) for rotation and mobile enrollment.

```sh
umask 077
test ! -e "$OPERATOR_CREDENTIAL" && test ! -L "$OPERATOR_CREDENTIAL" || exit 1
python3 services/chat-stream-v2/tools/operator_auth_cli.py issue \
  --client-kind pentacle --label 'Own assistant Web' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])' \
  > "$OPERATOR_CREDENTIAL"
chmod 600 "$OPERATOR_CREDENTIAL"
```

3. Start one owner-controlled daemon with role protection on and composite off. Use the correct provider executable, transcript root and private machines file from [orchestration setup](agent_orchestration_setup.md). Keep the machines-file host name equal to `--local-host`; `local` is only a synthetic single-host label. Record the PID, start/stop command and all store/registry paths. A foreground terminal is sufficient; no service installation is needed. This initial phase must not point at a production or existing assistant installation.

```sh
export PENTACLE_ASSISTANT_ROLE=assistant
export PENTACLE_ASSISTANT_COMPOSITE_ENABLED=0
# Leave PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS unset (default []).
python3 services/chat-stream-v2/main.py \
  --host 127.0.0.1 --port 7791 --local-host local \
  --db "$PRIVATE_STATE/sessions.db" \
  --notifications-db "$PRIVATE_STATE/notifications.db" \
  --assets-db "$PRIVATE_STATE/assets.db" --blob-root "$PRIVATE_STATE/blobs" \
  --spawn-cwd "$PRIVATE_WORKSPACE"
```

4. In another shell, invoke the actual [bootstrap tool](../tools/bootstrap_assistant.py). All authority-bearing inputs are explicit; it never selects an inherited endpoint or invents an operator CLI flag. It uses the daemon's operator nonce challenge/proof, requests a top-level protected role, and inspects the ready backend's actual stream, generation and effective tuple. It refuses existing outputs/bindings rather than replace an installation. `--dry-run` validates inputs and prints a plan with no network, spawn or file mutation; remove that flag to provision. The tool preserves the instructions and writes `assistant.env`, `assistant-config.json`, `assistant-receipt.json`, `assistant-client.cjs` and a durable `assistant-bootstrap-intent.json` with mode 0600. The intent records the exact request/backend before spawn so an ambiguous failure cannot create a second owner. Partial failure is an inspect/recover action, not permission to blindly spawn again.

```sh
python3 tools/bootstrap_assistant.py \
  --url ws://127.0.0.1:7791 --credential-file "$OPERATOR_CREDENTIAL" \
  --physical-host local --name Nova --provider codex \
  --model gpt-6-sol --effort medium \
  --private-workspace "$PRIVATE_WORKSPACE" \
  --instructions-file "$INSTRUCTIONS_FILE" --dry-run
```

The machine-readable receipt separates `composite_stream_id` (`local:assistant`) from the returned `backend_stream_id` (`local:assistant-backend-…`), `backend_generation`, `effective_tuple` and `bootstrap_state`. A provider-native ID is not a session generation. The display name is quoted as data; do not construct shell commands from it or use it as authorization.

5. Stop only the recorded daemon PID, retaining the live backend tmux pane. Load the emitted `assistant.env` in the daemon owner's launch environment and restart the exact same daemon command with the **same stores, registry, machines file and backend**. For a managed installation, use its existing owner-authorized restart procedure and feed those variables into that service explicitly; sourcing an interactive shell does not update a service. The wrapper never restarts anything. It leaves exception recovery off; no private work-item name or title grants rebind power.

```sh
# After stopping only your recorded daemon:
. "$PRIVATE_WORKSPACE/assistant.env"
# Repeat the exact daemon command above with unchanged stores/registry.
# In a separate shell, repeat the bootstrap invocation above using
# --readback in place of --dry-run. It is read-only and checks exact binding,
# backend readiness and configured composite title against the receipt.
```

Readback must show `activation: restart_binding_verified`, the exact returned stream/generation, binding `source` (`env` initially) and `revision` (0 initially). A stale pair fails closed; do not invent a new generation or edit SQLite. The immutable startup setting `PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS` is a JSON array, default `[]`; malformed values fail startup. An explicit opt-in requires a matching qualified spec with verified grant provenance on a live visible parentless seat. Naming a title/role/spec without that grant gives no privilege. The happy-path recipe leaves this setting unset.

6. Build/serve Pentacle Web using the generated `assistant-client.cjs` as the private config (`PENTACLE_CONFIG` or `--profile`), following [the web host guide](../server/README.md). It carries the same physical host, credential-file path, `features.assistantRole: assistant`, mic off and experimental Chat on so the composite can be selected. Select Nova's `local:assistant` row, send one typed input and see the correlated provider answer in the browser. Queue persistence or `submission_confirmed=false` alone is not a reply. Retain the input/dispatch IDs, backend generation, canonical publication receipt/event and actual effective tuple privately. Have the backend follow the daemon-authored `assistant publish` contract exactly, preserving answer text and IDs on retry.

For Web/mobile away from the daemon machine, loopback is not reachable. Use the physical host's permitted VPN/LAN address in the client endpoint and the documented authenticated listener/access setup; a phone's `localhost` is the phone. The generated client URL is the bootstrap URL; change your private client endpoint deliberately for remote access. Mobile needs its own enrolled credential and matching `assistantRole`, following [mobile setup](https://github.com/HJK6/pentacle-mobile/blob/main/AGENT_SETUP.md). Composite custom title comes from the daemon; no legacy assistant alias or client rebuild is needed to choose the typed name. Provider login, supported account tuple, physical phone/signing and microphone provisioning remain separate installation prerequisites.

The microphone source is available at [mic-server](../mic-server/README.md), but capture and `features.mic` stay off by default. The listener currently uses a fixed built-in assistant wake; changing the typed display name does not change it. Custom natural wake and mobile legacy alias migration are deferred. Do not enable voice merely to validate typed bootstrap.

## Binding readback and recovery

From the current backend's actual issued seat-token environment, `agent-orch assistant binding` reads the effective `stream_id`, `generation`, `source` and `revision`. The owner may hot-rebind with the supported candidate CLI:

```sh
agent-orch assistant rebind --target "$SUCCESSOR_STREAM" \
  --generation "$SUCCESSOR_GENERATION" --expected-revision "$BINDING_REVISION" \
  --request-id "$STABLE_REQUEST_ID"
agent-orch assistant binding
```

Only a current binding owner, an authenticated handoff successor, or the narrowly configured exception can mutate it. Revision CAS, exact live generation/tuple and replay fences remain authoritative. `--clear` removes the durable override only when the startup env binding is still usable; it is not a way to repair a stale env pair. Use owner-authorized `agent-orch spawn --handoff` for continuation and inspect its returned successor/retirement evidence, then read back binding. A dead/stale owner requires the existing authenticated operator recovery/handoff journey; do not run the fresh bootstrap again over retained outputs. Inspect request receipts before retries, and keep automatic recovery off.

For reproducible installation evidence, run [the portable first-turn gate](developer_onboarding.md#7-run-validation). It exercises the actual bootstrap, daemon, tmux, native-format counterpart's own candidate publish CLI and real browser, including duplicate publish, stale-generation refusal and restart readback. It does not establish paid-provider login, mobile hardware, microphone or private runtime activation.

## Activation and context

Use the existing authenticated operator or trusted service spawn interface to create a top-level session on the daemon host with the configured role, chosen provider/model/effort, and private initial instructions. An ordinary agent token cannot grant or remove the protected role. Operator/service role changes use the existing role-set interface. The role does not confer general orchestration authority.

Private deployments may place an `agents/<role>_baseline.md` in their configured memory root so the existing orchestration CLI prepends it on spawn. The daemon does not invent or manage personal memory. The bootstrap should load a compact private context file and link existing work/receipt records. Record new commitments before acknowledging them, and retain dispatch IDs so a successor checks delivery evidence before retrying. A conversation checkpoint is not evidence that a project completed.

## Continuation and failure

Exactly one assistant holder is admitted, except for the bounded overlap during an existing managed handoff. Concurrent activation and role grants are serialized; unresolved persisted spawn intents also prevent a duplicate activation. Repeating the same spawn request keeps existing idempotent replay semantics.

The current assistant may use its verified token for `spawn --handoff`, preserving the role and staying on the daemon host. Authenticated operator recovery uses the existing spawn handoff fields with the prior stream as `handoff_from_stream_id`. Successful launch uses the existing managed predecessor close. A successor should wait until it is the sole open holder before writing shared context or dispatching work. If cleanup is uncertain, inspect the existing receipt and predecessor instead of starting another replacement.

Ordinary operator, self, idle-reap and sweep closes are protected. Only internal `handed_off` and `spawn_rollback` close kinds bypass protection; a client-supplied `close_kind` cannot authorize deletion. Confirmed pane death leaves the protected row open with death evidence so its existing client entry remains available for recovery. Recovery is explicit; there is no new automatic restart loop. A daemon restart reconstructs the same session and pending-intent state.

Existing owner-authorized scheduled handoffs can provide a future continuation. Assistant authority is checked at schedule admission; internal dispatch uses the scheduler's trusted identity. At fire, a protected handoff requires the owner generation recorded at admission; an obsolete or unbound schedule fails as `stale_owner_generation` before launching a successor. The daemon holds that generation's lifecycle fence through boot and retirement. Track the accepted schedule receipt and keep obsolete wakeups canceled. A locally hosted model alone does not provide offline phone access.

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

`services/chat-stream-v2/tests/test_lifecycle_authority.py` covers lifecycle mutation fences, disabled transfer, revocation, manager close/reparent fences and retired handoff replay. `services/chat-stream-v2/tests/test_assistant_role.py` covers default-off behavior, ordinary close protection, role authority, concurrent activation, idempotent replay, pending intents, managed dead-session handoff, retained recovery entry, scheduled admission and forged managed-close fields. `services/chat-stream-v2/tests/test_consent.py` covers enrollment, signed challenges, refusal, rollback and revocation linearization. Existing role, handoff and scheduler suites cover their surrounding contracts. See [phone approval enrollment and recovery](REMOTE_AUTH.md#phone-approval-keys). `services/chat-stream-v2/tests/test_consent_host_ceremony.py` rehearses the actual candidate CLI and WebSocket handlers with synthetic signatures and an isolated home, registry and local-admin token. Run the daemon merge gate and each client's documented gate before activation. A live provider/installed-client readback remains distinct from deterministic fixture evidence.

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

## Direct reply mirroring

A direct-primary dispatch publishes its final answer through `assistant publish`
with the dispatch's fixed request key and `response_state=final`. Publish the
exact answer, preserving Markdown and whitespace. The canonical prose event is
the authoritative reply; the shared transcript renderer handles its Markdown.

## Direct-primary questions

The current direct-primary seat asks with the ordinary durable `prompt ask`,
even while hidden. The daemon surfaces those cards in the composite chat: a
`prompt.list` for the composite stream also covers the bound seat, and each of
its question notifications carries `surfaced_to_stream_id` naming the composite.
The producer and the answer route stay the bound seat. After a rebind the
daemon stops listing and stamping the previous seat's cards; a web client that
was already open keeps its cached cards until it reloads, and answers still go
to that seat. A closed seat's questions expire as usual. A failed binding read
omits the stamp and the wider scope; it never fails hello or a list. Lane
`question.open` is not used in this mode.

The source transcript remains intact. In the same ingest transaction, the store
omits a mirrored final when that source turn contains the route's exact frozen
USER envelope and the same dispatch already has a final prose publication. It
checks the source stream, current generation, session lifecycle and transcript
identity. Claude `end_turn` and Codex `final_answer` delimit turns; separate text
blocks from one provider record share that boundary. Classified commentary and
tool-use text and sidechain finals do not end the primary turn. Legacy assistant rows without classification
metadata conservatively fence the search during an upgrade.

This correlation does not depend on identical answer text, publication age, or
whether publication preceded source transcript ingestion. A following ordinary
final still mirrors, even when it repeats the published answer. Active direct
routes retain prepublication suppression through ambiguous delivery; a proven
failed delivery releases it. Unclassified events retain the existing short-lived
exact-text fallback. Existing historical duplicate events are not rewritten.

Focused coverage is in `tests/test_assistant_prose_mirror.py` and
`tests/test_codex_rollout_norm.py` under `services/chat-stream-v2`, including an
isolated authenticated WebSocket journey. Renderer coverage is in
`test/shared_transcript_view.test.ts`. Tests use isolated streams/databases and
never send dispatch probes to a configured live assistant.
