# Receipt and route retention

The existing retention cadence archives settled send-receipt history and terminal
assistant routes. This is source behavior, not evidence that a particular daemon
has activated it. Activation requires a paired backup, the compatible recovery
artifact, and the deployment owner's historical-reader and unrelated-request
readbacks.

## Policy and bounded work

A receipt must be at least 30 days old and its latest hot attempt must also be
at least 30 days old and settled: confirmed `landed`, or proven `not_landed`.
Uncertain, accepted, pending-proof and newer attempts remain hot. Current
session-generation proof and receipts referenced by hot routes remain hot.

Routes require old creation **and** update timestamps, terminal routing and
settled delivery. Retained lane/operation references, reopenable completed lanes,
episodes reachable through those lanes, leases, causal dependencies, questions,
current target generations and handoffs remain hot. A prepared question bridge
conservatively pins its stream because the bridge does not retain a queryable
dispatch key. Standing operator authority has no age expiry. A real dispatched
landed input needs a final publication that is also at least 30 days old before
it can be considered; missing or
incomplete authority evidence is not permission to archive it. Active mutation
and authority joins continue to operate only on these protected hot cohorts.

Each pass alternates receipt and route batches, with four batch slots:

- At most 500 candidate keys examined per batch, regardless of how many qualify
- At most 1 MiB of full row payload materialized per batch/transaction
- At most 2,000 receipt-plus-route rows moved per pass
- One Store callback per batch, with loop yields and queued-request interleaving

Primary-key keyset cursors advance through protected and oversized rows, wrap
at exhaustion, and restart conservatively after a daemon restart. They are not
an unbounded scan hidden behind a result limit. Dependency probes have indexes.
Oversized rows stay hot and emit a count-only diagnostic. `PassResult` includes
`receipts_moved`, `routes_moved`, and per-batch examined/byte/oversize counts.

Byte preflight uses SQLite `octet_length`, which reads column byte lengths
without materializing large text. SQLite **3.43 or newer** is required for this
movement path. Check the actual installed interpreter before activation:

```sh
/path/to/daemon/python -c 'import sqlite3; print(sqlite3.sqlite_version); print(sqlite3.connect(":memory:").execute("select octet_length(?)", ("x",)).fetchone())'
```

The existing session/event/schedule retention budgets and cadence are unchanged.
Receipt/route input work has its own fixed ceiling; a low legacy batch setting
can reduce work, never increase the fixed limits. No routine pass runs `VACUUM`
or `wal_checkpoint(TRUNCATE)`. Existing safe checkpoint behavior is unchanged;
freelist and file-size reporting remain available. Compact only in a separately
owned offline maintenance window.

## Archive, identity and readers

The default is the sibling `sessions_archive.db`. Programmatic configuration is
`RetentionConfig.archive_path`; the first pass binds that path persistently.
Use `Store(..., archive_path=...)` when historical reads must use a custom archive
before the first pass. Once bound, restarting Store without an override uses the
persisted path. Different hot databases must not share one archive.

The archive has same-name `v2_send_receipts` and
`v2_assistant_composite_routes` tables. Receipts additionally have
`receipt_rowid INTEGER PRIMARY KEY`, an alias for the original global rowid.
Routes preserve all original columns and unique keys. The hot singleton
`v2_send_receipt_sequence` retains the high-water identity, archive binding,
pair UUID and bootstrap state. The archive format marker carries the same UUID.
These additions do not rebuild the hot receipt table.

Both real insertion paths allocate explicit rowids and advance high water in the
same transaction as insertion. Composite admission does not commit its enclosing
transaction prematurely. Failed transactions may reuse an uncommitted identity;
committed values are never reused, including after highest/all-row removal,
archival, restart and recovery. Signed-64-bit exhaustion fails safely.

A genuinely fresh hot database records permission to bootstrap its empty pair;
a provisioned zero-byte file or auxiliary-only database is fresh too. Freshness
is determined before startup migrations from absence of core Store/history
schema, never from empty current history tables. This pending state survives a
session-only restart. Once initialized, a missing
archive is an outage and is never recreated. Migrating an existing hot database
requires a readable archive and seeds from the maximum identity in both files.
If an installation demonstrably never had an archive, its operator must establish
that fact and deliberately provision the empty archive offline. Do not use an
empty replacement to repair a missing archive.

Movement is ordered:

1. Select bounded keys and byte-preflight their full payloads
2. Insert absent archive identities, or require identical existing payloads
3. Commit the archive with `synchronous=FULL`
4. Lock and verify every committed identity and every original column
5. In a distinct hot transaction, delete only still-identical, still-eligible rows

There is no cross-database WAL atomicity claim and no `INSERT OR REPLACE` collision
handling. A crash between commits leaves identical copies. Readers deduplicate
those copies and reject conflicting payloads, including conflicts selected by
only one side of a filter. Path/inode, pair and schema checks prevent a cached
connection to a disappeared/replaced archive from pretending coverage is intact.

Latest/history, first metadata, late proof, peer counts, coalescing, event stamping,
composite replay/ownership and historical route/activity readers combine the two
stores before ordering and filtering. Archive failure is an explicit error, not
false absence or permission to submit again. An initialized allocator can still
append distinct identities during an archive outage; replay/admission requiring
historical knowledge fails closed.

Managed
attachment GC retains archive-owned bytes, fails closed on unknown archive state,
and never treats retention ownership as fetch authorization.

## Session and event rows

Terminal session rows and archivable `session_event_tail` rows move in the same
order, one batch at a time, on the Store connection with the archive attached.
Selection only chooses candidate keys; nothing it read is trusted later. Both
transactions below hold the hot write lock and re-read the current hot rows:

1. A `BEGIN IMMEDIATE` transaction copies the candidates that are present and
   eligible now into the archive. It changes only the archive, so its commit is
   the archive file's alone. It never deletes or replaces an archived row whose
   hot source is absent.
2. A second `BEGIN IMMEDIATE` transaction also holds the archive reservation.
   It checks that the configured archive (the hot sequence row's path) is the
   attached one, with the same device, inode and pair. It re-reads each hot row
   and keeps only rows that are still eligible, equal to their archived copy in
   every column and, for events, free of an alternate-key collision. It checks
   the binding again, then deletes those rows. Its commit writes the hot file
   only.

Eligibility is decided from the current row in each transaction. A session must
still be terminal, outside the caller's keep-set and outside the lineage
keep-set. An event's stream must still be closed, or the event must still be
below its open stream's tail cutoff, and not an assistant composite's. A crash
at any instant leaves each row hot, archived or both, including when a second
mover or a reopen races the pass. The next pass converges. Hot-side triggers
fire with the delete, never with the copy.

`bind_history_archive` relocates a pair in a hot write transaction, so it is
serialized with the delete. The path, device and inode checks detect an archive
file replaced between the attach and either check. A rename of the archive path
by a process that does not take the hot lock is not serialized beyond those
checks: do not move or replace a live archive file outside
`bind_history_archive` and the offline procedures below.

Events keep their original `event_id`, `event_key`, identity and bytes. An
identical archived event is already copied. A hot event whose current row
collides with a different archived event stays hot, is logged once and is counted
in `events_retained`. The collision can be the same `event_id` with other bytes,
or another archived `event_id` with the same `event_key` or non-null identity
within the same stream and birth, including for an exact copy. Selection advances
past retained rows. A reused session name keeps today's behavior: the newer
terminal row replaces the archived incarnation, through the same order. The
archive still holds one row per `(host, session_name)`.

Late receipt reconciliation treats one empty-identity event that is found in both
files with the same id and bytes as a single match.

`tests/archive_crash/crash_cases.py` checks these boundaries with the real writer
and real process exit. Run it on a named host, not in the hosted workflow:

```sh
python -m pytest tests/archive_crash/crash_cases.py -v
```

## Offline backup and compatible recovery

Keep this version of `retention.py` and its matching source/interpreter with the
release artifact **before movement is enabled**. Stop every daemon writer and
retention process first. `--offline` is an operator assertion; the utility does
not stop a daemon and does not claim to be a live multi-process backup protocol.
Use an owned destination directory that does not already exist:

```sh
cd services/chat-stream-v2
/path/to/daemon/python retention.py backup --hot /data/sessions.db \
  --archive /data/sessions_archive.db --output /backup/paired-before-movement --offline
```

This uses SQLite backup APIs, including committed WAL contents, copies both
files, verifies integrity, and prints the pair UUID and high water. Do not copy
only the main files with a running writer or discard WAL files. Keep both backup
files and their allocator together.

To rehydrate archived receipt/routes into the **current** hot database while
retaining intervening writes:

```sh
/path/to/daemon/python retention.py restore --hot /data/sessions.db \
  --archive /data/sessions_archive.db --offline
```

Restore is bounded and restartable. It uses explicit original identities,
requires full equality for existing identities, refuses differing collisions,
retains the archive, and advances high water to the maximum across both stores.
It never overwrites intervening payloads. Resolve a collision with both copies
preserved. It does not restore unrelated session/event tables into the hot set;
those remain available through their existing archive interfaces.

For a relocated paired backup, pass its exact new hot and archive paths to the
same command. Explicit rebinding requires the matching pair UUID, or a verified
empty previous archive. Switching away from any populated archive-owned table,
including original sessions/events, is refused for a different pair. A pair UUID
is not a freshness guarantee: select the intended coherent backup deliberately.
Rehydrating an old snapshot cannot recover writes absent from both supplied files.

Disable movement first when rolling back. Preserve both files and sequence.
Rehydration is compatible recovery, **not** permission to run old code that ignores
archive history or allocates implicit rowids. Blind rollback to those readers is
unsafe even when some archived rows have been restored.

## Verification boundaries

The synthetic acceptance suite is `tests/test_receipt_route_retention.py`, with
existing retention, send, composite, authority, episode, retro and attachment
regressions. It exercises crash/retry/equality controls, actual both-path allocator
journeys, paired backup/relocation/recovery, and a 14,469-receipt/928-route fixture.
Its 20 interleaved reads report p50/p95/max loop, queue and callback timing, queue
high water, RSS and host load. Separate 1x/10x unrelated-history controls count
SQLite VM steps. These controlled-fixture results are not arbitrary-load latency
promises or installed/fleet acceptance. The synthetic paired-copy/restore rehearsal
does not replace the sender-owned live-pair migration and recovery rehearsal.
