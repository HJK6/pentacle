# Durable ordinary recovery, version 1

This is an operation-scoped, opt-in recovery path for an ordinary deployment with
unchanged code identity and dependencies. It does not authorize deployment or
recovery. Fleet must separately review the exact candidate, run its exact-head
gate, independently rehearse the actual entry and issue each bootstrap/recovery
GO. All examples below are placeholders, not instructions to act on a live host.

## Entries and scope

Enroll **before the first mutation**, from a clean separate pinned controller:

```sh
python services/chat-stream-v2/deploy/deploy.py chat-streamd-v2 \
  --release-checkout <runtime> --ref <exact-target-sha> \
  --gate-evidence <exact-target-gate> --read-timing-receipt <receipt> \
  --deployer-stream-id <owner> --ordinary-recovery-bundle <new-private-directory>
```

Normal forward guards and account selection still apply. Enrollment adds no
coordinated acceptance and never qualifies ambiguous history. It requires a
pre-provisioned daemon interpreter, identical prior/target requirements and code
identity, immutable managed helper releases, a proven prior daemon identity and
an unloaded retired fleet-smoke schedule. It refuses unsupported dependency
changes or helper migration rather than pretend file restoration reverses pip or
remote installation. Unenrolled legacy ordinary invocations are unchanged; they
are **not crash-recovery-capable and are not the approved bootstrap route**.

Read the exact current state without changing source/runtime:

```sh
python services/chat-stream-v2/deploy/deploy.py chat-streamd-v2 \
  --release-checkout <runtime> --ordinary-recovery-status <bundle-directory> \
  --deployer-stream-id <owner>
```

After a separate FD decision, invoke the actual recovery entry:

```sh
python services/chat-stream-v2/deploy/deploy.py chat-streamd-v2 \
  --release-checkout <runtime> --recover-ordinary <bundle-directory> \
  --ordinary-recovery-authorization <private-authorization.json> \
  --recovery-go-tell-id <exact-go-id> --deployer-stream-id <owner>
```

These modes cannot combine with coordinated plans/receipts, ordinary `--rollback`,
prepare-only, another service or skip-kickstart. Existing ordinary and coordinated
rollback routes are not substitutes for a failed enrolled ordinary operation.
The inherited coordinated receipt never authorizes this recovery.

## Recording the named legacy manual recovery

FD may record a manually recovered serving target for the one legacy operation
pinned by the controller. Use a clean, separately reviewed controller checkout
and the original private bundle and operator recovery receipt.

```sh
python services/chat-stream-v2/deploy/deploy.py chat-streamd-v2 \
  --release-checkout <serving-runtime> \
  --record-ordinary-manual-recovery <original-bundle-directory> \
  --manual-recovery-authorization <private-fd-authority.json> \
  --operator-recovery-receipt <original-operator-recovery-receipt> \
  --manual-recovery-operator-credential <private-operator-v2-envelope> \
  --recovery-go-tell-id <exact-delivered-fd-tell-id> \
  --deployer-stream-id <exact-go-recipient>
```

The 0600 FD JSON has exactly these fields.

- `schema` is `pentacle.ordinary-manual-recovery.v1`.
- `operation`, `bundle_sha256`, `bundle_bytes_sha256`, `pointer_bytes_sha256`,
  `journal_sha256`, `journal_bytes_sha256` and `operator_receipt_sha256` match
  the controller's incident pins. The unmodified failed journal is at
  `failed_sequence` 9 with phase `recovery_required`.
- `original_controller_sha`, `prior_sha` and `target_sha` match the sealed
  bundle. `replacement_controller_sha` is the reviewed controller commit.
  `deployer_stream_id` is the physical stream receiving this GO.
- `expected_serving` has `source` (clean runtime HEAD), `process` (exact
  `loaded`, `pid`, `birth` and `sha` for the current launchd job),
  `boot_uuid` (paired stdout/stderr generation markers for that PID and UTC
  birth), `listener` (`127.0.0.1:7791`), `launchd_label`,
  `launch_arguments_sha256` (the canonical JSON SHA-256 of the loaded plist
  argument list), `files` (each captured bundle role mapped to current
  `sha256` and numeric `mode`, using null for verified absence) and
  `collector` (current `loaded`, `pid` and `birth`). FD records this from
  current host evidence. The stamp entry binds exact observed stamp bytes.

After reviewing that authority, the currently bound FD must deliver this exact
single-line tell body to the named physical recipient.

```text
GO record-ordinary-manual-recovery <operation> <authority-file-bytes-sha256> <replacement-controller-sha>
```

The controller reads the matching delivered tell through the serving daemon's
Store on a nonce-authenticated operator connection to the loopback listener it
just verified. The tell must have a verified peer sender and generation matching
the current open FD binding, and must have been delivered in the preceding
10 minutes. The controller rechecks managed files, process and binding within a
30-second monotonic budget. The common deployment lock covers the exact pointer,
bundle and journal comparisons, persistence and readback.

The result `manual_recovered_serving_target` records the old operation as
`aborted` and unqualified. One atomic terminal journal retains the original
failed journal bytes, its failure result and a separately sealed receipt of
the verified serving generation, authority and controller. Source, daemon,
active pointer, stamp and immutable bundle remain untouched. Repeating the
identical authority and GO on the same active pointer returns the same
terminal receipt. A different grant cannot replace it. A later forward requires
its own enrollment, rollback preimage and authorization.

## Immutable bundle, authority and mutable journal

Schema `pentacle.ordinary-recovery.v1` has a unique operation identifier and a
private canonical directory (0700). `bundle.json`, `journal.json`, preimages and
`source.bundle` are private regular files (0600). Destinations must be canonical,
owned regular files or recorded absence; symlinks and overlapping paths refuse.
The bundle binds the release checkout, exact controller/origin, prior and target
SHAs, source objects, requirement/code identities, actual prior PID/birth/SHA,
collector loaded state, exact accounts/generations/CLI pins, scope and ancestry
receipt/seal. The source archive is a Git bundle containing prior/target reachable
objects, created through an isolated bare staging repository, not modified runtime
refs. Validation reads its objects in an isolated bare clone.

Every captured destination records bytes or absence, full hash and mode:

- Deployment stamp and sibling test-profile snapshot
- Daemon and collector plists
- Retired fleet-smoke schedule plist and its unloaded state
- Account policy, machine catalog and local spawn settings

Original immutable helpers retain exact manifest/archive bytes, hashes and modes,
plus existing installer source verification, module/dependency and CLI artifact
proofs. Their references are restored with the original policy; helpers themselves
are never migrated or rewritten. Missing/altered immutable helpers refuse rather
than silently rebuilding them. The daemon interpreter and dependency closure are
pinned separately from helper interpreters and revalidated before restoration.
Private bundle data is not printed in the return. No credentials are introduced.

Both JSON records carry canonical SHA-256 consistency digests. The immutable
bundle digest is bound by the journal and authorization; each journal replacement
increments a sequence number. These checks are **not authentication against an
arbitrary same-uid editor**. Authority remains an independently issued FD decision,
not a digest, file name or copied coordinated seal.

The authorization JSON must have exactly these fields:

- `schema`: `pentacle.ordinary-recovery.v1`
- `operation`, `bundle_sha256`, `controller_sha`, `prior_sha`, `target_sha`
- `expected_state`: the complete status result's `ordinary_recovery_status` value,
  including exact source, file hashes/modes, process/collector identities and current
  journal digest
- `go_tell_id`: equal to the separately supplied CLI GO id
- `fd_gate_reference`: nonempty reference to the separate FD decision

After any interrupted attempt or journal transition, **fresh exact-current-state
FD authorization is required for reentry**. The earlier approval is not widened.
A completed recovery can be read back idempotently only under fresh matching state
and authority. A successful qualified forward operation is not eligible for this
failure-recovery entry.

## Publication and admitted states

An active ordinary-operation pointer is written only after complete durable
bundle/journal capture and readback. The common deployment lock fences unfinished
ordinary operations against both new ordinary and coordinated transactions.

Enrolled file mutations use write-ahead exact bytes/hash/mode intent, file fsync,
atomic rename and directory fsync. The journal admits only the current image and
its immediate intended replacement, then collapses to the verified new image.
It never accumulates an allowlist of historical images or adopts arbitrary bytes
observed after a crash. Enrolled plist environment/stderr edits use deterministic
whole-plist rendering instead of unjournaled external `plutil` writes. Unlink and
mode restoration are journaled too. Source admission is restricted to the recorded
prior/target transition; restoration never selects an arbitrary ref.

Phases are `forward`, `serving_unqualified`, `recovering`, `restoring`, `starting`,
`recovery_required`, `qualified` and `restored_unqualified`. Initial partial bundle
creation has no active pointer and no source/runtime mutation. Missing, partial,
unsupported or corrupt evidence refuses. A partial temporary write is not a
published image. Rename-before-checkpoint loss accepts only its already-journaled
next image, not an inferred success.

Fresh process identities are recorded only after runtime proof. **Loss after a
restart but before its new PID/birth/SHA is durably recorded is an unsupported,
ambiguous window and refuses recovery**, even if a process appears to serve the
target SHA. Likewise, loss after stop but before the stopped identity is journaled
refuses instead of assuming the observed absence belongs to this operation.
Loss after the recorded identity or during journaled file publication
is recoverable under fresh exact-state authority. The implementation does not
claim all possible controller-loss windows are automatically recoverable.

## Recovery order and outcomes

1. Validate all bundle/preimage/source integrity, exact controller and current
   source/images, receipt linkage, immutable helpers, daemon dependencies,
   accounts/CLI pins, schedule state and ordinary quiescence. Validate separate
   authority against the exact current journal/state. Unknown drift or absent
   authority means no source/file/process mutation.
2. Recheck daemon and collector identities immediately before stopping. Bind the
   daemon stop to the admitted PID/birth, unload it and prove daemon/descendant and
   relevant local old-tree helper exit. Stop and prove collector exit. Remote
   immutable helper releases are not changed; their integrity is revalidated.
   Unproven exit means no source/file restoration.
3. Recheck source/files/accounts/helpers after stop. Journal restoration before
   fetching the preserved source objects and checking out the exact prior. Restore
   every captured file/absence/mode with write-ahead intents and verify all hashes,
   modes and source. Partial restoration stays stopped and `recovery_required`.
4. Only after the complete tree passes readback, bootstrap the prior daemon, prove
   fresh boot/PID/birth/SHA, and restore the collector's prior loaded/unloaded state.
   Use the existing authenticated zero-input restoration/owned-close and exact
   helper dispatch/binding proof. Recheck the exact boot-proven identity after
   proof before declaring restoration; a replacement is not adopted as success.
5. Keep the original stamp byte-exact. Result `restored_unqualified` (exit 4) reports
   restoration, **not a new full-fleet forward acceptance**. Ambiguous legacy stamps
   remain unqualified. Late proof failure leaves a proven prior serving but reports
   failed checks/recovery required. Unproven or partial outcomes remain fenced.
   A serving forward target with failed smoke/final publication remains active and
   unqualified until separately authorized recovery; no automatic rollback policy
   was added. Lost bookkeeping cannot relabel a known serving target as inactive.

## Offline evidence and limits

`tests/test_deploy_ordinary_recovery.py` exercises the actual new CLI/dispatch and
bundle/journal controller with real temporary Git repositories, source bundles,
files, modes, atomic replacement and child-process exits. It covers actual enrolled
ordinary smoke/final-publication failure followed by the recovery entry, controller
loss, before/after rename and partial publication, bad/stale authority, bundle and
preimage corruption, drift, stop-before-source ordering, byte/mode/absence restore,
reentry and no legacy qualification. OS/process/boot/account probes in those entry
fixtures are explicitly synthetic, not runtime proof.

A concrete-adapter fixture separately uses the existing real synthetic account
SQLite/helper subprocess environment and provisioned interpreters (including
websockets) to exercise capture and revalidation. No live account or kv data is
read by this milestone. Fleet must resolve every live-data authorization and run
its independent actual-environment rehearsal before GO. Source tests, hosted CI
and this packet do not grant bootstrap, recovery, merge, deployment or restart.
