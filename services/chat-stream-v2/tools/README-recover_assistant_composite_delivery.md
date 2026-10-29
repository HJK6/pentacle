# Assistant composite delivery recovery

`recover_assistant_composite_delivery.py` promotes older direct-primary routes
only when a final publication proves delivery for that route's exact dispatch
and input message ID, and the event actor matches the route target. It is an
offline, audited data repair tool. Run it on an isolated copy for rehearsal;
for the authoritative database, stop the daemon and confirm that `lsof` holds
neither the database nor its WAL or SHM files before any stopped-file command.
The tool never starts or stops the daemon.

The preview lists candidates and skipped routes by ID, with reason
`actor_mismatch` or `no_exact_final`. It contains no message bodies. Save it as
the exact preimage for `--apply --expect-ids`; a changed ID set is refused before
backup or promotion. The backup is mode `0600` and its SHA-256 is recorded in
the run. The tool checks SQLite `quick_check`, `user_version=1`, base tables,
and an open composite session. Version 1 is intentionally pinned here: this
offline tool must refuse a future database schema until its recovery proof and
CAS are reviewed for that version. It imports the daemon's recovery table DDL
strings, so writable initialization uses the same additive definitions.

| Operation | Open mode | Database change | Run state and verification |
|---|---|---|---|
| `--dry-run --immutable` on a backup | immutable, read only | None; no DDL | Preview only; permitted before stop on a copied file |
| `--dry-run` on stopped database | read only | None; no DDL | Preview and runs needing attention; missing audit tables mean no runs yet |
| `--apply --run-id R --backup-dir DIR --expect-ids FILE` | read/write | Creates audit tables if needed; writes a `prepared` run, then exact five-field CAS promotions and audit rows in one transaction | `applied`, `noop` for a fresh empty preview, or `aborted`; verify R next |
| `--verify --run-id R` for applied run | read/write | Only `verify_outcome` and `verified_at` | Compares each audited postimage byte for byte; exit 0 on `ok`, 1 on `mismatch` |
| `--rollback --run-id R --new-run-id RB` | read/write | New `prepared` rollback run; restores only R's audited five-field preimages with full postimage CAS | R becomes `rolled_back`, RB becomes `applied`; verify RB and R |
| `--verify --run-id RB` or rolled-back R | read/write | Only verification fields | Confirms preimages restored and candidates available again |
| `--reconcile --run-id R` | read/write | Marks an unaudited `prepared` run `abandoned` | Use after a crash left T1 committed; refuses if an audit row exists |

The five CAS fields are `routing_state`, `delivery_state`, `error_code`,
`updated_at`, and the exact `route_json` bytes. An apply changes only the
candidate route's delivery evidence. A rollback restores all five fields,
including non-canonical JSON, and leaves unrelated rows and later publications
alone. A crash after the `prepared` transaction leaves no promotion; reconcile
it before another apply. A crash after promotion may leave `applied` without a
verify outcome; run `--verify` on that run.

## Ordered rehearsal sequence

Use new run IDs (`R`, `R2`, `RB`, `R3`) and an unused backup path for each apply.
`DB` is the stopped database; `COPY` is a separate backup made while the daemon
was running. The first command is the only one allowed before stop.

```sh
PY=~/pentacle/.venv/bin/python
TOOL=~/pentacle/services/chat-stream-v2/tools/recover_assistant_composite_delivery.py
S=local:assistant
$PY $TOOL --db "$COPY" --stream "$S" --immutable --dry-run --out preview-pre.json
# Stop daemon; confirm lsof -t "$DB" "$DB-wal" "$DB-shm" is empty.
$PY $TOOL --db "$DB" --stream "$S" --dry-run --out preview.json
$PY $TOOL --db "$DB" --stream "$S" --apply --run-id R --backup-dir "$BACKUPS" --expect-ids preview.json
$PY $TOOL --db "$DB" --stream "$S" --verify --run-id R
$PY $TOOL --db "$DB" --stream "$S" --dry-run --out preview-empty.json
$PY $TOOL --db "$DB" --stream "$S" --apply --run-id R2 --backup-dir "$BACKUPS" --expect-ids preview-empty.json
$PY $TOOL --db "$DB" --stream "$S" --rollback --run-id R --new-run-id RB
$PY $TOOL --db "$DB" --stream "$S" --verify --run-id RB
$PY $TOOL --db "$DB" --stream "$S" --verify --run-id R
$PY $TOOL --db "$DB" --stream "$S" --dry-run --out preview-again.json
$PY $TOOL --db "$DB" --stream "$S" --apply --run-id R3 --backup-dir "$BACKUPS" --expect-ids preview-again.json
```

The empty preview at step 5 is required for the no-op apply; reusing the first
preview is refused. A real rollback runs **while the candidate tool still
exists**, before changing code. Whole-file restore would discard every write
since the backup and is outside this tool's rollback contract.
