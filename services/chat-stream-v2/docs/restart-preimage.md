# Restart preimage capture

`tools/restart_preimage.py` prepares clone capability while serving, captures a
stationary main/whole-WAL set under the caller's existing writer fence, and
normalizes/verifies the captured set after serving resumes. It uses Python 3.11+
and the standard library only. It does not control a service, open a source with
SQLite, checkpoint or migrate a source, install a recovery snapshot, or discover
configuration and writers.

This is a physical restart preimage helper, not a disaster-backup integrity
proof. Preparation is not a preimage. A captured but unverified set is ineligible
for restore. A receipt found on disk is never sufficient evidence of success.
No performance claim follows from the portable tests.

## Scope and prerequisites

* The first integration must be schema-unchanged and use a pinned compatible prior
  artifact that can read the **current** databases. Before and after reopen,
  recovery must preserve all accepted writes. Never automatically install an
  older snapshot to recover from a compatible restart.
* Establish the complete configured participant set, including sessions,
  notifications, assets, archive and schedules, plus every additional database.
  The helper cannot infer missing writers or participants from SQLite files.
  Preserve the hot/archive pair and its identity. No new cross-database atomicity
  is claimed.
* Use Darwin with a filesystem and permissions supporting libc
  `clonefile(source, destination, 0)`. All source mains, sidecars and destination
  directories must be on the same device. Unsupported platforms/volumes,
  unavailable clonefile, `EXDEV`, `ENOTSUP`, permissions and existing clone
  targets fail closed. There is no `cp`, byte-copy, hardlink or SQLite-backup
  fallback inside capture.
* The destination's parent must already exist. Destination must not exist and
  must be outside **every** source directory. Use absolute paths without `..`;
  symlinks in any component, including source files, are rejected. Files must be
  regular, and participant paths/inodes must be distinct. Source-directory
  containment is checked against destination-ancestor device/inode identities,
  so alternate path spelling cannot bypass it.
* Maintain exclusive cooperative custody of the destination and receipts. No
  other process may mutate raw files or receipts. Do not open raw with SQLite.
  Trust correct native clone/filesystem behavior; privileged changes to files or
  receipts and latent metadata-preserving damage are outside this proof.

If preparation cannot clone, retain the existing stopped SQLite-backup and
validation procedure; never skip backup. Migrations, destructive maintenance,
archive-format changes, incomplete inventories and snapshot-install recovery
also keep that existing procedure. This helper does not integrate itself into
restart or recovery code.

## Input records and commands

Commands run from the repository root. The paths below are illustrative, not
host discovery. Replace them with the caller's explicitly configured paths on
one supported device. No implicit home, host or default database is consulted.

An inventory JSON file has this shape:

```json
{
  "schema": "restart-preimage.inventory.v1",
  "participants": {
    "sessions": "/srv/pentacle/state/sessions.db",
    "notifications": "/srv/pentacle/state/notifications.db",
    "assets": "/srv/pentacle/state/assets.db",
    "archive": "/srv/pentacle/history/archive.db",
    "schedules": "/srv/pentacle/state/schedules.db",
    "analytics": "/srv/pentacle/analytics/events.db"
  },
  "required_labels": ["sessions", "notifications", "assets", "archive", "schedules", "analytics"],
  "compatible_restart": true
}
```

All configured participants are captured, not only `required_labels`.
`required_labels` must be a nonempty unique list of present labels; the helper
always requires its five minimum participants as well. Labels match
`[A-Za-z][A-Za-z0-9_-]{0,63}`. Duplicate JSON keys, non-finite JSON numbers,
unknown schemas and non-boolean compatibility assertions are rejected.

### 1. Prepare while serving

```sh
python3 services/chat-stream-v2/tools/restart_preimage.py prepare \
  /srv/preimage-input/inventory.json /srv/preimages/restart-001
```

Prepare checks source identity and filesystem placement without opening,
cloning, hashing or integrity-checking any database. It creates mode-0700 DEST,
`raw/` and `normalized/`. Capability probes use only unique tiny owned files:
one representative source directory per distinct source device to DEST, then
DEST to each output subdirectory. Probe entries alone are cleaned up. A failed
preparation directory is retained and must not be reused.

`PREPARED.json` is mode 0600. It records a UUID capture id, destination/directory
identities, source device/inode identities and the complete inventory including
its compatibility declaration. `inventory_sha256` hashes the canonical inventory
encoding: sorted JSON keys, compact separators, ASCII escapes, no NaN and one
trailing newline. It is not the hash of arbitrary input-file whitespace.
Prepare fsyncs the parent entry creating DEST, each output subdirectory, its
receipt temp file and DEST after atomic receipt publication.

Keep the successful preparation acknowledgment and compare its receipt hash to
the exact PREPARED bytes. Databases may keep changing during preparation and
until the fence. The source inode/device must remain unchanged, while capture
measures sizes/times afresh.

### 2. Capture inside the existing complete writer-exclusion window

After establishing the fence and stopping the configured writers, create an
exclusion JSON record with the preparation's exact capture id:

```json
{
  "schema": "restart-preimage.exclusion.v1",
  "capture_id": "REPLACE-WITH-PREPARED-CAPTURE-ID",
  "fence_ref": "caller-owned-fence-or-restart-reference",
  "stopped_at": "2026-10-11T06:00:00Z",
  "writer_pids": [42001, 42002],
  "all_configured_writers_excluded": true
}
```

`stopped_at` is an ISO-8601 timestamp with timezone. The fence reference must be
nonempty. PIDs must be unique integers greater than 1, never booleans. Each PID
must be absent both before and after capture; permission denied or unknown
status is not absence. PID checks only detect obvious broken exclusion. They do
not establish completeness, prevent new writers or independently enforce the
fence. The caller guarantees no old/new writer, checkpointer, archiver or
source-changing SQLite reader can run through capture completion.

```sh
python3 services/chat-stream-v2/tools/restart_preimage.py capture \
  /srv/preimages/restart-001/PREPARED.json /srv/preimage-input/exclusion.json
```

Before the first clone the helper checks **every** main and its sidecars. Any
`-journal`, including an empty file or dangling symlink, rejects capture. Main
replacement, sidecar symlinks/nonregular files, device differences and duplicate
main/sidecar path or inode identities reject it. The complete inventory records
main/WAL stat tuples `(dev, ino, size, mtime_ns, ctime_ns)`, WAL presence/absence,
SHM state and journal absence. SHM is checked for identity/stability but never
copied; SQLite reconstructs it from the captured WAL.

Each main and its entire existing WAL is cloned to `raw/<label>.db` and matching
`-wal`. No arbitrary WAL suffix is taken. Raw files are chmod 0400, fsynced, and
`raw/` is fsynced. The helper then independently lstats each raw file after
chmod/fsync, retaining its own full stat tuple rather than copying the source
tuple. It rechecks the complete source/sidecar inventory and absent PIDs before
publishing `CAPTURE.json`.

CAPTURE has schema `restart-preimage.capture.v1`, state `captured_unverified`,
prepared receipt digest, exclusion record, complete source inventory, per-file
source/raw stat baselines, mappings and monotonic capture timing. Capture uses
no source SQLite connection, database hash, SQLite backup or integrity scan.
It has no implicit retry. Existing CAPTURE or a nonempty partial raw directory
is refused. Failed or unknown capture requires a fresh prepared destination and
a newly valid fence; do not resume it inside the outage.

The recorded capture interval begins at command work entry and ends after
physical cloning and final checks, **before receipt publication**. Publication,
final directory synchronization, output delivery and the full writer-exclusion
window are additional time. Measure the complete stopped window separately.

### 3. Verify after service resumes

```sh
python3 services/chat-stream-v2/tools/restart_preimage.py verify \
  /srv/preimages/restart-001/CAPTURE.json
```

Verify reconciles the whole receipt against PREPARED and all participants. It
checks **every raw lstat tuple against the original capture baseline before the
first database hash**, rejecting missing/replaced/nonregular/symlink/changed
files and unexpected entries. It hashes raw, checks the original baseline
again, and clones each raw pair into a unique owned `scratch-*` directory under
DEST using the same native adapter. Only scratch clones become writable (0600).

Ordinary read-write SQLite recovery operates only on scratch, applying committed
WAL frames and ignoring uncommitted tails. `SQLite.backup` writes each fresh
`normalized/<label>.db`. The output switches to DELETE journal mode to make it
self-contained, and `integrity_check` must return exactly one `ok` row. All
connections close; normalized files become mode 0400, are fsynced, and their
directory is fsynced before hashing. Raw is rehashed and its complete original
metadata baseline checked again before publication. Source and raw databases
are never opened with SQLite by the helper.

`VERIFIED.json` has schema `restart-preimage.verified.v1`, state `verified`, the
exact CAPTURE digest and embedded capture record (including original raw
metadata), complete raw and normalized SHA-256 values, normalized sizes, SQLite
version and monotonic per-stage timings. Stage names are `initial_raw_hash`,
`normalization`, `normalized_hash`, `final_raw_hash` and overall `verification`.
The overall interval includes prerequisite checks; publication and output
acknowledgment occur after the recorded end. Neither interval proves production
performance or lack of serving interference.

Scratch is retained for diagnosis, including on success. All failures preserve
raw and other existing evidence. Partial normalized output is not resumed or
overwritten automatically. Operator diagnosis or a new capture is required.

If VERIFIED already exists, explicit verify reentry validates receipt
completeness, the original raw baseline and every raw/normalized hash. It
fsyncs raw, normalized and receipt files, `raw/`, `normalized/`, DEST and DEST's
parent, then acknowledges success without rewriting backup bytes or the
receipt. This can re-establish **verification-stage** durability after a previous
post-rename failure. It cannot reconstruct an unobserved capture acknowledgment.

## Publication, adoption and failure outcomes

Each receipt is written to an exclusive mode-0600 temp file, flushed/fsynced,
atomically replaced into its success-named path, then followed by DEST fsync.
Directory entries required by its dependencies are synchronized first.
Receipt visibility at replace is **not** the adoption point.

A stage is adoptable only when all its required file/directory fsyncs succeed
**and** the caller observes exit 0 with complete JSON:

```json
{"eligible":true,"capture_id":"...","receipt_sha256":"..."}
```

The SHA-256 must match the exact bytes of that stage's receipt, including its
trailing newline, and the id must match the expected capture. Python functions
`prepare(inventory, dest)`, `capture(prepared, exclusion)` and `verify(capture)`
accept parsed records or explicit JSON paths (DEST is a path). They return this
acknowledgment only after synchronization, or raise. Parsed PREPARED/CAPTURE
records must match their persisted receipts. Programmatic callers must retain
the successful function result just as CLI callers retain exit/stdout.

* Before rename, failure leaves no new success-named receipt; temp evidence may
  remain. Never infer success from partial raw/normalized contents.
* After rename, directory-fsync failure or interruption can leave CAPTURE or
  VERIFIED visible. The CLI returns nonzero and `eligible=false` when able.
  It does not erase a visible receipt or raw preimage to disguise the failure.
* Absent/truncated output, broken stdout, an unknown exit or abrupt termination
  is non-adoptable even if a receipt is visible and valid JSON.
* A successful `verify` says only that its verification stage completed. It
  cannot know whether the caller observed the **original successful capture**
  exit/stdout. An unacknowledged capture still cannot authorize activation.
* Prepare success authorizes only preparation. A successful CAPTURE remains
  `captured_unverified`. Restoration eligibility additionally requires successful
  verification, the original capture acknowledgment and the caller's recovery
  policy and adoption gates. The helper never activates or installs anything.

The cut is equivalent to sequential stopped SQLite backups only under the
caller's complete stationary fence and correct native clone behavior. COW
isolates later source writes; it does not create cross-database atomicity that
was absent before the fence. Metadata detects ordinary raw changes before
hashing; hashes detect changes during verification. Neither detects all
preexisting source corruption, latent metadata-preserving damage before the
first hash, malicious/privileged receipt forgery or faulty native/filesystem
behavior. Integrity `ok` does not mean the data is the intended preimage.

## Validation and requester-owned adoption gates

Portable tests use tiny invented data and a test-only byte-copy clone adapter.
The production helper has no copy fallback or test fault switches. The RED at the
pinned base is missing helper/API feature absence, not corruption or a fabricated
timing failure. Run declared dependencies and direct tests from the root:

```sh
python3 -m venv /tmp/pentacle-preimage-venv
/tmp/pentacle-preimage-venv/bin/python -m pip install \
  -r services/chat-stream-v2/requirements.txt -e services/agent-orch
env -u TMUX /tmp/pentacle-preimage-venv/bin/python -m pytest \
  services/chat-stream-v2/tests/test_restart_preimage.py -q -rs
/tmp/pentacle-preimage-venv/bin/python -m compileall -q \
  services/chat-stream-v2/tools/restart_preimage.py \
  services/chat-stream-v2/tests/test_restart_preimage.py
```

The matrix covers six participants, multiple commits after prepare, checkpointed
missing-WAL data, committed WAL with observed uncommitted spill, paired hot/archive
IDs/metadata, and complete ordered rows/schema/user_version against a separate
SQLite.backup oracle at the same frozen cut. It tests source changes/checkpoints
after capture, all-file metadata checks before hashing, same-size checksummed
committed-WAL row/index mutation that still passes SQLite integrity, restored
mtime and replacement inode, malformed manifests, PID ambiguity, unsupported
cloning and interruption after every captured file. Publication tests assert
actual receipt visibility independently from failed/unknown acknowledgment,
including normalized-directory and final DEST fsync. Reentry tests verify
hashes, original baselines and required resynchronization without rewriting.

Portable proof does **not** satisfy these requester-owned requirements:

1. **Native clone/filesystem parity:** run on the actual supported Darwin volume
   with real `clonefile`, matching receipts and retained native COW evidence.
2. **Representative restored application set:** construct a tiny complete
   configured Store/notifications/assets/archive/schedules set with realistic
   receipt/route schema. Compare every participant's full ordered canonical row
   digests, schema, user_version, receipt/route IDs and retained history against
   the same-frozen-instant stopped SQLite-backup oracle. Do this **before**
   relocating persisted archive-path metadata.
3. **Restored hot/archive lookup:** make a disposable writable restored set,
   explicitly pass its restored archive path through the existing Store
   `archive_path`/`bind_history_archive` flow, preserve pair identity, and make the
   original fixture archive inaccessible. Check the intentional path metadata
   change separately. On that isolated set, a disposable daemon must resolve an
   accepted request and retained route without a real-provider injection, proving
   that retained lookup accesses only the restored archive. Record actual reads,
   SQLite version, bytes, source/candidate identity, raw/normalized digests, pair
   identity and integrity. The portable row fixtures are not this daemon proof.
4. **Certified merge gate:** run
   `python services/chat-stream-v2/tools/run_gate.py merge` on the exact candidate
   in the requester environment. Direct synthetic pytest is not that gate.
5. **Representative-size performance and interference:** capture at most 1 second
   is an engineering adoption limit, not a predicted result. Measure full native
   capture and usable recovery/request latency while verify runs. New serving
   interference blocks adoption. No speculative throttling framework is added.

The measured motivation was 12.79 seconds spent copying/checking/hashing five
databases totaling roughly 6.64 GB during a 27.53-second stopped restart. Tiny native
feasibility and byte-copy stand-ins do not establish full-size speed. Production
size/timing, actual restore parity and fleet integration remain requester-owned.
