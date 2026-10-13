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

The pointer file maps each source stream to a list of `{ "host": "source", "path": "/absolute/path", "sha256": "64-hex-digest" }` records. Include only the minimal checkpoint, route-manifest and artifact pointers needed to resume. Paths are interpreted on the recorded host and hash-checked there. Secrets remain in provisioned stores. Successor seat tokens are issued and staged by the existing daemon spawn path, never copied from conversation history.

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

When FD is down, the recommended separate recovery design is one up-front biometric authorization covering the entire fixed migration: complete role/generation roster, destination, tuples, intent digest, binding/grant revisions and expiry. A standing pre-approval is an alternative with an explicit single-use id, identical scope, absolute expiry (recommended at most 24 hours) and independent id/revision revocation. Its longer compromise window is the tradeoff. These are design recommendations, not grants implemented by this living-FD operation. Existing cold-recovery standby authority remains separate.

The daemon verifies provenance and authority, not English meaning. FD can misread quoted or unrelated text inside a genuine operator message. Compromise of the authenticated operator session, authorized FD or trusted daemon/host can misuse transfer authority. Omitting per-switch biometrics deliberately accepts that risk, bounded by the one-use, freshness, generation and trusted-destination checks.

A daemon binding change cannot revoke an external native-wake cache. Before any live move its owner must disable/revoke **and drain** that route. It remains unavailable until a fresh authenticated successor identity is captured and installed under the separate activation gate. Source acceptance and isolated tests do not authorize a live migration or replacement of existing standby pins.
