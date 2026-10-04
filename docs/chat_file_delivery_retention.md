# Managed attachment retention and runtime gate

## Managed-only deletion

The existing retention job adds a separate bounded managed-attachment batch. It
does not delete chat history or change schedule retention durations. Eligibility
is 24 hours since every surviving upload receipt's server `uploaded_at`, never
file mtime. A fresh receipt for the same digest protects deduplicated bytes.
The job advances a digest cursor across passes so retained rows cannot starve
later candidates. The existing retention cadence and kill switch still apply;
24 hours is an eligibility boundary, not an exact wall-clock deletion promise.

The batch takes the persistent per-digest advisory lock before the SQLite write
transaction. It repeats owner/fresh-upload checks under that lock, unlinks and
fsyncs the containing directory, then deletes provenance and commits. Managed
upload materialization and publication already use that same lock. Input
admission now holds it through the event/route/receipt commit as well. Generic,
report and prompt upload completion participates in the lock and permanently
marks any shared managed digest as excluded before materialization.

All processes writing this shared root must use the reviewed implementation;
old writers do not participate in the new serialization contract. Never remove
persistent lock files while a writer or retention job may be running.

Owner checks include retained live and archived transcript events, publication
metadata/attachment IDs/evidence references, input routes, operation evidence,
send receipts, schedules and report text fields. JSON text is decoded for escaped
references. Conservative text matches may retain extra storage; they never grant
read permission. The scoped authorization path remains the exact validated
publication ledger, not these retention queries. Report/prompt reuse is excluded
permanently, including before an expired schedule drops its last prompt reference.
Legacy/untracked files and generic/report/prompt bytes are never GC candidates.

## Recovery

Pending receipts are not referenceable. Once their `updated_at` is at least five
minutes old, the next managed retention pass reconciles them: absent bytes remove
an unowned pending row; valid bytes are reverified and promoted; invalid bytes
and unowned stale rows are removed safely. This is an eligibility timeout on the
existing job cadence, not a new five-minute background scheduler. A normal
upload retry can repair the row sooner through the upload path.

Unowned ready rows with absent bytes are reconciled. A retained owner's missing
file keeps its validated receipt as a tombstone so an authorized fetch continues
to return `blob_unknown`. That preserves honest unavailable UI and does not
silently remove the retained publication. GC opens every blob-root ancestor,
managed lock directory and digest shard with no-follow directory descriptors.
Verification and unlink use the pinned shard descriptor, so replacing a shard
with a symlink cannot redirect deletion outside the root. Missing directories,
symlink components and non-regular leaves retain provenance for recovery.
Retention never creates an empty replacement root to infer that files disappeared.

A supplied archive path is required owner inventory: direct GC opens it read-only
and fails if unavailable. The cadence job checks an explicitly configured archive
before purging schedules or moving sessions, so a missing archive cannot silently
be replaced with an empty one. With `archive_path=None`, direct GC has no archive;
the cadence job retains its existing default-archive initialization behavior.
An unavailable configured archive must be restored before retrying retention.

The deletion sequence can crash after unlink but before row commit. Repeating
reconciliation handles that ready/missing state. The add sequence remains
pending-row commit, materialize, ready-row commit under one digest lock. There is
no migration that infers read authority from historical event content.

## Tests

`tests/test_managed_attachment_retention.py` covers age boundaries, mtime
independence, dedup/shared owners, archived owners, report/prompt/evidence pins,
generic/report/prompt transport reuse, pending/ready crash points, invalid bytes,
missing-root/ancestor/shard/lock symlink guards, descriptor-pinned shard replacement,
configured missing archive failures at direct and cadence entry points, cursor
progress, failed-delete recovery and two
independent Store workers contending across both upload/GC and publication/GC
orders. These use only disposable SQLite stores and synthetic files.

Run the new tests alongside existing retention, managed upload/publication,
scoped access, receipt admission, CLI roundtrip, external-principal, photo/voice,
direct binding and rebind suites. Existing CI remains unchanged. A local
configured-CI prerequisite such as tmux is not replaced by a test mock or counted
as a full-suite pass.

## Prepare the mobile fixture

From the reviewed daemon checkout, with its Python dependencies installed:

    python3 test/e2e/file_delivery/prepare_mobile_fixture.py --root /tmp/NEW_PRIVATE_FIXTURE

The parent directory must exist and the final directory must not exist. The
preparer refuses an existing directory. It creates only synthetic DB/blob data
there, using real managed upload and authorized publication handlers with a
synthetic verified seat context. It starts no daemon, agent or network connection,
and creates no credentials. The output explicitly says runtime and handshake
are not run.

The resulting fixture contains `fixture:file-chat`, `fixture-present.pdf` with
exact bytes `%PDF synthetic mobile file gate`, and an actually published
`fixture-expired.pdf` whose generated bytes were then removed. The manifest
contains receipt IDs, hashes, event IDs and the required synthetic composite
environment. Do not seed or point this tool at a live data root.

The fleet runs its already qualified isolated loopback daemon with this fixture's
DB/blob paths and manifest environment. Its notifications/assets/credential
stores must also be disposable, provider/remote/background workers disabled, and
its harness app authenticated only to that fixture. This preparer does not
configure or enroll the app. Reuse the isolated gate launcher rather than the
production service configuration.

In the matching mobile checkout, use `test/e2e/file_delivery/run.py` and its README
for the fresh installed-artifact receipt, exact simulator UUID and local Maestro
command. The runner checks actual cached download bytes, unavailable state and
the native share sheet. Source tests and an iOS JavaScript export are not native
qualification. The physical-device save acceptance remains separate. Retain
runtime evidence privately; commit only synthetic fixture source.
