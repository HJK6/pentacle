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

This release enables witness producers after qualification of the compatible
reader. Each physical remote creation registers a fresh pending attempt before
dispatch, including retries and resumed launches. Cancellation seals issuance;
only successful command return acknowledges its exact attempt. Unreadable or
malformed witness history refuses issuance and dispatch. Existing metadata is
preserved through payload updates. The prior reader's member-present dispatch
refusal is replaced by this durable issuance fence.

The reader understands per-attempt successful-command acknowledgements, which
are distinct from binding or adoption. Cancelled cleanup releases after all
attempts are terminal, a fresh absence observation, and an atomic
identity/owner/witness comparison. Missing, historical or malformed metadata
stays unknown. Historical unwitnessed remote cancellations can therefore retain
their names indefinitely under this release. Non-cancelled bound release and local
cancellation keep their existing behavior.

Deploy only after the compatible reader is qualified live. Seal that exact
reader SHA as this release's rollback prior. Both automatic recovery and explicit
rollback must return
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
