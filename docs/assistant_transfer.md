# Full assistant host transfer

The current front desk can request a daemon-owned migration after an authenticated operator instruction in its ordinary assistant chat. The operation moves the front desk, fixed advisor and explicitly listed helpers together. It preserves provider/model/effort, transfers child custody and grants immediate lifecycle-manager powers at one authority commit. It does not move the daemon.

The daemon verifies the FD's own token and current generation against both its assistant binding and manager grant. The referenced operator dispatch must be the original authenticated, direct, landed input to that generation, less than 600 seconds old at claim and commit, with an intact input digest. Tell/report attribution and arbitrary user-role text are not operator provenance. The original input is usable for only one transfer, and the destination must be a configured trusted host. No additional biometric confirmation or later adoption occurs on this path.

## Request and readback

From the currently authorized FD, use a stable command identity and list the complete roster:

```sh
agent-orch assistant transfer request --command-id move-unique-id \
  --composite-stream-id example:assistant --dispatch-id ORIGINAL_DISPATCH \
  --destination trusted-peer --member source:fd --member source:advisor \
  --member source:helper --pointers-file /private/path/transfer-pointers.json
agent-orch assistant transfer status --command-id move-unique-id \
  --composite-stream-id example:assistant
```

The pointer file maps each source stream to a list of `{ "host": "source", "path": "/absolute/path", "sha256": "64-hex-digest" }` records. Every listed member supplies at least one checkpoint or no-pending-work disposition pointer. Empty FD/advisor lists cannot establish resumability. Checkpoints name current commissions, exact spec/work paths, stage, unresolved obligations and owners; their authors and successors verify completeness. Include only the context and artifact pointers needed to resume. Paths are interpreted on the recorded host and hash-checked there. Secrets remain in provisioned stores. Successor seat tokens are issued and staged by the existing daemon spawn path, never copied from conversation history.

Each inert prepared successor reads its pointers and sends `assistant transfer ready --command-id … --pointer-digest …` using its own token. Its preparation prompt supplies the exact digest. Ordinary mutations remain fenced until commit. A successful request reply acknowledges the durable command; it does not establish completion. Read the command's state and receipt. Repeating the same command and identical manifest returns its existing outcome; changing the manifest conflicts. Reusing the original input with another command also conflicts.

## Commit, failure and restart

Preparation retains a usable source. Unknown helper authority, changed source bindings, ambiguous in-flight schedules, unresolved attempted notices or unsettled child spawns refuse commit. The detached spawn obligation retains its generation effect lock after an RPC disconnect, through launch and settlement. Transfer drains the FD first; it refuses a busy additional actor or ruling release without waiting while holding another actor. It then revalidates under its existing authority/lifecycle/graph locks and operator-registry lock. A single Store transaction changes the binding, manager, advisor/helper pointers, roles and child custody, records succession and fences the old generations. Original dispatch, ruling, schedule and QA provenance remain historical evidence.

After commit the successor can manage and close inherited children immediately. Old tokens and already-bound old sockets cannot perform authority-bearing work. Sources remain physically open until generation-checked cleanup confirms termination. Cleanup failure is retried; it never restores source authority. Ordinary questions resolve ownership through committed succession until the separate notifications database projection catches up; this is not a claim of atomicity across two databases.

Startup resumes preparation only within its original authority and expiry, and resumes committed projection/cleanup. A cancelled or failed command retains its one-use input claim. Before commit, an authenticated operator/local administrator or the exact requesting FD can cancel with `assistant.transfer` action `cancel`. The CLI's `assistant transfer cancel` uses its current seat credentials.

Rollback after commit is a **new authorized reverse transfer to fresh generations**. Never restore old tokens or an old database snapshot over live authority. A binary downgrade that lacks the retirement fences is unsafe while old credentials or processes could act.

## Emergency revocation

A compromised FD need not cooperate. On the daemon host, the existing independently authenticated local-admin emergency channel supports an opt-in exact-generation retirement:

```sh
agent-orch lifecycle revoke --emergency --retire-fd-generation \
  --composite-stream-id example:assistant --target source:fd \
  --generation EXACT_GENERATION --expected-revision CURRENT_MANAGER_REVISION \
  --request-id stable-revocation-id --reason 'Operator-directed emergency retirement'
```

Read the current binding and manager first. Both must match the explicit target/generation. The transaction revokes the manager, retires that generation, cancels pending migrations and pins the assistant binding unavailable without environment fallback. Replays bind the flag, named binding, target, generation and revision. Ordinary emergency revoke without this flag keeps its existing semantics. This emergency operation adds no step to a normal switch. Revoking the originating operator credential independently also prevents pending commands from committing.

## Recovery and accepted risks

Living-FD standing authority is implemented below. An unavailable or dead FD cannot mint it through a helper. The separate cold-recovery path retains its independently authenticated biometric designation, fixed standby scope and existing retirement fences. Unknown availability does not establish death; this protocol does not activate or replace a standby or native-wake cache.

The daemon verifies provenance and authority, not English meaning. FD can misread quoted or unrelated text inside a genuine operator message. Compromise of the authenticated operator session, authorized FD or trusted daemon/host can misuse transfer authority. Omitting per-switch biometrics deliberately accepts that risk, bounded by the one-use, freshness, generation and trusted-destination checks.

A daemon binding change cannot revoke an external native-wake cache. Before any live move its owner must disable/revoke **and drain** that route. It remains unavailable until a fresh authenticated successor identity is captured and installed under the separate activation gate. Source acceptance and isolated tests do not authorize a live migration or replacement of existing standby pins.


## Standing authorization and bounded continuation

Use explicit `standing-transfer/v1` mode to record the original authenticated instruction before lengthy preparation. Legacy requests keep their 600-second claim and commit window. Authorization recording also requires an input younger than 600 seconds; it cannot revive an expired instruction.

```sh
agent-orch assistant transfer authorize --authorization-id grant-unique-id \
  --command-id move-unique-id --request-id authorize-unique-id \
  --composite-stream-id example:assistant --dispatch-id ORIGINAL_DISPATCH \
  --destination trusted-peer --member source:fd --member source:advisor
agent-orch assistant transfer authorization-status --authorization-id grant-unique-id \
  --composite-stream-id example:assistant
agent-orch assistant transfer request --authorization-id grant-unique-id \
  --expected-revision 1 --command-id move-unique-id --request-id start-unique-id \
  --composite-stream-id example:assistant --pointers-file /private/path/transfer-pointers.json
```

The grant freezes one command, destination, complete source generations, roles, parent relationships, provider/model/effort tuples, binding and manager revisions. Recording it creates no successor. Default expiry is six hours from the original input, with a hard limit of 24 hours. `--expires-at` takes a timezone-aware RFC3339 timestamp; a shorter window is allowed. A window longer than six hours also requires `--extension-evidence-file` containing `{body_start,body_end,quote,timezone,requested_expires_at}`. Offsets count Unicode code points in the original authenticated message. The quote must exactly match the nonempty span, the timezone must be unambiguous, and requested expiry must equal the normalized grant expiry. The FD must identify the operator's own explicit window or event; the daemon verifies provenance, span and bounds rather than interpreting prose.

The first request binds a nonempty host/path/hash manifest immutably and increments the grant revision before spawning. Each prepared successor's prompt supplies its fresh attempt identity and pointer digest. It uses its own token with `assistant transfer ready --command-id … --attempt-id … --pointer-digest …`. Old attempt readiness never satisfies a new attempt. Only status/readiness is permitted before commit, and commit remains automatic after all original fences pass.

Only `transfer_uncertain_notice`, `transfer_effect_busy` and `transfer_preparation_failed` may resume. Settle the original cause, then use the same command and grant:

```sh
agent-orch assistant transfer resume --authorization-id grant-unique-id \
  --expected-revision CURRENT_GRANT_REVISION --command-id move-unique-id \
  --expected-attempt FAILED_ATTEMPT_NUMBER --request-id resume-unique-id \
  --composite-stream-id example:assistant
```

The daemon rereads exact target generations, terminal owned cleanup and original spawn outcomes/reservations; a caller-supplied cleanup assertion confers no authority. Resume rechecks unchanged source scope, absolute expiry, credentials and current prerequisites. It preserves the manifest and original expiry, and creates fresh target names/generations. Three total preparation attempts are allowed, including the initial attempt. Replaying the same request returns its saved response without consuming an attempt; a changed payload conflicts. Arbitrary permanent failures, cancellation, revocation, expiry, unknown cleanup or a prior commit cannot resume. Status exposes attempt history and the audited prior-cause/cleanup evidence. Restart never implicitly starts a new attempt.

Cancellation in standing mode requires `--expected-attempt` and stable `--request-id`; it permanently revokes the unused grant. Independent emergency grant revocation requires the daemon host's existing local-admin credentials, with no seat-token fallback:

```sh
agent-orch assistant transfer authorization-revoke --authorization-id grant-unique-id \
  --expected-revision CURRENT_GRANT_REVISION --request-id revoke-unique-id \
  --composite-stream-id example:assistant --reason 'Operator-directed revocation'
```

The wire API also accepts independently authenticated operator revocation. Revocation and commit serialize under the existing Store, credential and authority locks: one wins. A spent grant returns `transfer_authorization_consumed`; revocation never undoes a committed migration. A revoked originating credential, retired FD generation or changed source/binding/manager scope refuses unused authority. Persisted high-water time rejects clock regression as `transfer_clock_regressed`; retries and restart cannot extend expiry. Redacted authorization/status receipts remain available under the original caller scope.

Authenticated rejected revocation requests retain their identity and error without changing authority. Repeating that request returns the same refusal; changing its payload under the same request id conflicts. Standing command operations must name the grant's frozen composite, and standing cancellation requires an explicit stable request id.

The wire type remains `assistant.transfer`, with `assistant.transfer.ok` responses. New actions are `authorize`, `authorization-status`, `authorization-revoke` and `resume`; the CLI sends explicit mode for them and for grant-backed requests. Scalar flags map to snake_case fields. `--member` becomes `members`, `--pointers-file` loads `pointers`, and `--extension-evidence-file` loads `extension_evidence`; local filenames are never authority fields. Unknown fields or action/mode combinations fail closed. There is no manual commit action.

Before a downgrade, revoke/cancel and drain all uncommitted standing grants using a compatible version. Preserve terminal receipts and retirement fences. A successful transfer can be reversed only by a new authorized transfer to fresh generations.
