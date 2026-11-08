# Cancelled remote creation

Cancelling a pre-bind spawn ends the user request. A remote creation command
already accepted by the peer may still create its pane after cancellation or
daemon restart. Its exact name and creation nonce remain reserved for cleanup.
The reconciler only removes a matching nonce; it never admits or briefs cancelled
work and leaves foreign or unreadable identities untouched.

`spawn status` keeps the terminal `cancelled` outcome and adds `cleanup` to a
retained remote reservation:

| State | Reason | Meaning |
| --- | --- | --- |
| `pending` | `remote_cleanup_pending` | Every issued creation command acknowledged success; verified pane absence is still required. |
| `unknown` | `remote_creation_completion_unknown` | A command acknowledgement or its durable provenance is missing. |

Unknown can persist indefinitely. In particular, restart during remote creation
loses its awaiting task and cannot recover that acknowledgement. The reconciler
can consume a late matching pane while retaining the obligation. No timeout or
force-release operation resolves this uncertainty; Nexus/FD owns recovery and
any separately qualified recovery procedure. Same-key retry remains cancelled,
and same-name reuse stays fenced. Other worker names can proceed normally.
Account status labels retained admitted creation records
`cancelled_cleanup_pending`; destructive CLI maintenance and host-transfer
quiescence still wait for unresolved creation, including cancelled creation.

This release supplies the compatible reader before enabling witness producers.
New remote spawns use the existing unwitnessed flow. A retained intent containing
any `remote_creation` member cannot dispatch another creation under this reader;
it requires a writer-capable release. Unreadable current intents also refuse
dispatch. Existing witness metadata is preserved through payload updates.

The reader understands per-attempt successful-command acknowledgements, which
are distinct from binding or adoption. Cancelled cleanup releases after all
attempts are terminal, a fresh absence observation, and an atomic
identity/owner/witness comparison. Missing, historical or malformed metadata
stays unknown. An ordinary unwitnessed remote cancellation can therefore retain
its name indefinitely under this release. Non-cancelled bound release and local
cancellation keep their existing behavior.

Deploy and qualify this reader first. Enable witness initialization, issuance and
acknowledgement only in a subsequent release whose sealed rollback prior is this
qualified live reader. Both automatic recovery and explicit rollback must return
to a compatible reader. Record the exact controller checkout/SHA, target/prior
identities and reader journey; the smoke-runner hash does not identify the deploy
controller. Future-record fixtures belong in isolated databases, never live rows.

After witness production begins, the release owner must keep every selected
target/prior compatible. Existing receipt and ancestry checks reject stale
receipts and legacy rollback; a new coordinated forward preparation targeting
older code is not version-fenced. Do not select a pre-reader target through that
path. Preparing a newer transaction already makes this release's old receipt
stale, even if preparation is later discarded. Any pre-writer return to older
code then requires a fresh qualified transaction. After witness production, a
further downgrade requires separately accepted drain or compatibility proof.

Before activation, census retained legacy cancelled reservations and record
their recovery disposition. An old startup or recurring reconciler can delete
obligations even when spawn admission is frozen. Never delete obligations or
invent acknowledgements to make a downgrade possible.

Automatic recovery of lost acknowledgements and the analogous uncancelled
creation window are separate work.
