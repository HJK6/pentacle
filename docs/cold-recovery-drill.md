# Isolated recovery drill

`scripts/recovery_drill.py` checks a disposable, private state copy through the
ordinary authenticated WebSocket API. It starts no provider, tmux, scheduler,
fleet discovery or push listener. Dispatch is recording only. This procedure
proves the copied-state contract; operational failover has separate authority.

Prepare a mode0700 working directory containing read-only SQLite backups of
`sessions.db`, `notifications.db`, `assets.db`, `schedules.db`, and the matching
`sessions_archive.db` when the sessions sequence references an existing archive.
Copy the referenced blob bytes into `blobs/`. Preserve original backups separately
until verification ends, and remove every private backup and working copy finally.
The caller owns capture authority, exact source paths, bounded transfer, config /
PID / binding parity and matching source/destination hashes. Sequential backups do
not establish a cross-database atomic snapshot.

Use Python3.13 with the daemon dependencies and a private temporary HOME/TMPDIR.
Pass the resolved absolute path (on macOS, `/private/tmp/...`, not `/tmp/...`).
Run with an empty environment, adding only the required PATH and PYTHONPATH:

```sh
python scripts/recovery_drill.py /absolute/private/working-copy \
  --composite-stream fixture-chat:assistant --binding-name fixture-binding \
  --source-archive-path /approved/source/sessions_archive.db
```

`--source-archive-path` is the capture owner's approved literal. The copied
sequence must equal it; the drill never opens that source path. Before Store
construction, it validates the copied archive's full integrity, pair/schema,
receipt identities/high-water and duplicate hot/archive semantics, then changes
only the working copy's existing archive_path to the copied archive. Missing,
corrupt, mismatched, escaping or aliased copies fail before startup. A fresh
archive_ready=0 tuple without a file is explicitly `rewrite_only`: any bootstrap
occurs in the working copy. For an external source, that omission also requires
`--archive-source-absent`, backed by the capture manifest's source-file stat.
A missing required archive never becomes empty history.

All four component DBs receive full integrity and foreign-key checks, plus named
binding, lane, asset-comment, attachment-provenance and content-addressed byte
checks. The drill records source expectations before Store startup, then calls
`request_stream_events`, `work.report.get`, `work_lanes.list` and `work_lanes.show`
with capability negotiation and the real inventory dependency. History uses one
supported page of eight current-lifecycle events and checks exact durable IDs and
frame ordering. This does not qualify multi-page history limits. Reports expose
only their supported metadata; lane state and membership are checked through RPC.
Empty captured report/lane tuples receive separately labelled copy-only controls.

The fixture sequence replaces the current holder's token hash only in the
working copy, rebinds as that holder, then publishes as the new holder using
`publish:<dispatch_id>`. Different-seat and stale credentials must fail without a
binding mutation. One input uses a disposable auth-v2 operator credential through
public `send`; an identical publish retry must produce exactly one final reply.
A real Store/server restart and new authenticated connection repeat the reads.

The CLI requires the approved source literal and proves source/working root
separation before arming cleanup. An input rejected during admission stays
untouched; its caller must clean any known owned scratch. Pre-existing SQLite
sidecars and aliased data roots are refused before SQLite opens.

For an admitted copy, the script removes the working copy, fixture credentials and listeners in finally,
including cancellation and a reported Store shutdown failure. Sanitized receipts contain counts, identities, hashes and
named predicates; they contain no message bodies, bearer secrets or full config.
The caller must independently remove retained source/transfer backup scratch on
success or failure. A successful script run alone is not capture or cleanup proof
for that outer scratch.

Run `tests/test_recovery_drill.py` through the supported gate environment. Controls
cover populated copies, wrong public projections despite readable rows, startup
data loss, forbidden original SQLite opens and ATTACH, wrong archive pairs and
bytes, retained duplicates, cancellation, and zero external process actions. The
companion smoke is `tests/smoke/test_assistant_hot_rebind_socket.py`.
