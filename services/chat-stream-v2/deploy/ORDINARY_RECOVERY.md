# Ordinary ancestry bootstrap: recovery gap packet

Status: **BLOCKED for bootstrap/recovery GO**, source-only proposal. Owner: FD
for sequencing/authorization; fleet for exact-head gate, independent final QA,
reviewed recovery repair and rehearsal. No command below authorizes a live action.
All host paths, process identities and account values remain placeholders.

## Supported entries and why neither closes the gap

The pinned reviewed controller's `deploy.py chat-streamd-v2 --rollback` uses the
current stamp's `prior_sha` and retained `prior_qualification`, ordinary identity
and ancestry guards, and zero-input restoration. It is not an original operation's
durable source/file preimage replay entry. A historical ambiguous stamp cannot
become qualified by rollback.

The controller's `--rollback --coordinated-receipt <exact-receipt>` is supported
only for that coordinated operation, with applicable existing replacement-
controller recovery authorization. Its journal, preimages, accounts, source and
postimage checks remain mandatory. A committed inherited receipt cannot recover
a subsequent ordinary operation: source/file postimage drift refuses, and an
ordinary stamp does not satisfy the direct receipt-seal exception. Do not bypass
that refusal or fill in a manual seal.

`_PreActivationTransaction` stores bytes/modes and rollback callbacks only in
memory and is committed before restart. `_apply_post_activation` leaves a proven
serving target active if smoke or final publication fails. `_restore_prior` is
invoked only when target serving is not proven and the original controller still
exists. It proves stop before source/file restoration, then boot/SHA; this is not
a full binding qualification. After controller loss those memory preimages cannot
be reconstructed from the current target files. Neither existing entry is a safe
generic recovery route for the two missing branches.

## Smallest separately scoped repair proposal (not implemented here)

Preserve serving policy. Extend the ordinary transaction with an operation-specific
durable bundle and journal, fsynced before first mutation, plus one narrowly scoped
recovery entry in the pinned controller. Do not reuse the ancestor's coordinated
receipt, add a privileged service, or implement generic unguarded restore.

The future reviewed bundle must contain:

- Controller, candidate target and prior full source SHAs; clean checkout/origin
  identity and exact code identities; retained immutable source objects/archives.
- Durable original bytes, full hashes, path, existence/absence and modes for stamp,
  policy, daemon plist, collector/usage-probe plist, applicable schedules and
  test-settings/profile artifacts. Record original immutable helper release paths,
  manifests/proofs, ownership and installed references; do not copy secrets into
  public evidence. Each applicable mutable file must be inventoried explicitly.
- Exact ancestry receipt path/seal and retained stamp bytes, selected bindings,
  exclusions, CLI hashes/versions/account generations, and relevant helper proofs.
- Original process PID/birth/source identity, admitted postimages and atomically
  durable phase transitions sufficient to recover at each mutation boundary.
- Separate FD recovery authorization bound to operation, bundle digest, controller,
  intended prior/target and expected current state; stale approval must refuse.

Required recovery order, tested through the actual future entry:

1. Validate bundle integrity/modes, controller identity, available source objects,
   receipt, operation/journal state and separate FD recovery grant. Compare current
   source, all files/helper references, accounts/pins and owned process identity to
   explicitly admitted pre/postimages. Check quiescence and release exclusions.
   Unknown drift, missing bundle or absent authority means no mutation and GATE.
2. Stop only the operation-owned daemon/helpers through supported mechanisms and
   prove their exit before changing source. Unproven stop means no checkout/file
   restoration, preserve evidence and GATE.
3. Restore exact prior source first, then complete original files/modes, helper
   references and schedule state. Verify every hash, mode and source readback.
   Never start a partially restored tree. Partial restoration stays stopped/GATE.
4. Start from the exact restored definition. Prove fresh boot, PID/birth and SHA,
   then selected exact binding/dispatch/owned-close zero-input restoration. Do not
   run model smoke or claim a new forward acceptance. Original stamp bytes remain
   original; ambiguous history remains unqualified. A proven prior serving with a
   late optional proof failure remains active, recorded unqualified and GATE.
5. Persist final outcome and reconcile owned cleanup. Repeat entry must be
   idempotent from recorded phase, never infer success from a marker alone.

## Offline rehearsal delivered in this milestone

`tests/test_deploy_ordinary_provenance.py` uses synthetic identities, temporary
files and actual writer/ordinary controller code with external runtime probes
replaced. It proves:

- Same/different-target eight-cell ordinary forward writer/verifier round trips;
  exact provenance retention and tampered ordinary restoration refusal.
- Failed target-serving smoke and final stamp publication leave the target source
  active, do not invoke automatic restore, and leave no accepted durable new stamp.
- Real child `os._exit` during partial temporary write, before replacement and
  after replacement: earlier bytes survive before replace, complete provenance
  revalidates after replace; orphan temporary files are not success evidence.
- Real child controller loss after transaction commit leaves only mutated files,
  no durable ordinary preimages: the recovery prerequisite is **not satisfied**.

Existing no-input/coordinated suites exercise their existing recovery entries,
stop/source ordering, retained witness validation and drift refusal. These tests
are not a rehearsal of the proposed new ordinary recovery entry, which does not
yet exist. Before bootstrap GO, FD must commission that bounded repair, independent
review and a separate true-crash/partial-write rehearsal through its exact entry,
covering all listed source/stamp/policy/plist/helper/schedule preimages and modes.
No live restore route, host validation, runtime proof or bootstrap readiness is
claimed by this packet.
