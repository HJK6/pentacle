"""retention.py — the daemon's bounded archival passes.

The store runs these passes on a cadence through its worker thread, keeping the
hot set O(open) without a second offline operator surface. The daemon is the
process holding the DB open and uses bounded batches rather than a whole-file
copy for crash safety.

The passes, in the order a full run applies them:

  1. `archive_rows`   — terminal `sessions` rows move to `sessions_archive.db`,
                        except those a live row still descends from (lineage).
  2. `archive_events` — `session_event_tail` rows of terminal / already-archived
                        / orphan sessions move wholesale, and each still-open
                        session keeps only its newest `tail_keep` rows. This is
                        the pass that actually shrinks the file: measured on the
                        live DB, `sessions` archival alone took 1.7 -> 1.5 GiB
                        while the event tail held 1.18 GB + 208 MB of indexes.
  3. VACUUM           — reclaims the freelist the moves created. In WAL mode a
                        VACUUM rewrites the DB *into the WAL*, so it must be
                        followed by `wal_checkpoint(TRUNCATE)` or the on-disk
                        footprint grows instead of shrinking.

Errors raise `RetentionError`, never `SystemExit`: a `SystemExit` escaping into
an asyncio background task is a `BaseException` that bypasses ordinary handling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from v2_runtime import env_number

TERMINAL_STATUSES = ("closed", "closed_without_report")
LINEAGE_COLUMNS = ("parent_stream_id", "handoff_from_stream_id")
DEFAULT_BATCH = 500
EVENT_TABLE = "session_event_tail"
DEFAULT_TAIL_KEEP = 2000
SCHEDULE_RETENTION_DAYS = 30
SCHEDULE_RETENTION_BATCH = 500
SCHEDULE_TERMINAL_STATES = ("fired", "cancelled", "failed", "indeterminate", "expired")

log = logging.getLogger("chat_streamd_v2.retention")


class RetentionError(Exception):
    """A pass cannot proceed safely (bad schema, mismatched archive, ...)."""


# --------------------------------------------------------------------------- #
# schema introspection
# --------------------------------------------------------------------------- #


class Schema:
    """What we learned from the target DB's own `sessions` definition."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
        if not row or not row[0]:
            raise RetentionError("error: target DB has no `sessions` table")
        self.create_sql: str = row[0]
        self.columns = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        if "status" not in self.columns:
            raise RetentionError("error: `sessions` has no `status` column")
        self.pk = [r[1] for r in conn.execute("PRAGMA table_info(sessions)") if r[5]]
        self.lineage_columns = [c for c in LINEAGE_COLUMNS if c in self.columns]

        # A stream id is `<host>:<session_name>` in this schema; a future schema
        # may carry it as a literal column instead.
        if "stream_id" in self.columns:
            self.stream_id_expr = "stream_id"
        elif "host" in self.columns and "session_name" in self.columns:
            self.stream_id_expr = "host || ':' || session_name"
        else:
            self.stream_id_expr = None

    @property
    def key_columns(self) -> list[str]:
        return self.pk or self.columns


class EventSchema:
    """What we learned from the target DB's own `session_event_tail` definition.

    Event rows key to a session by `stream_id` (`<host>:<session_name>`), not by
    the sessions PK, plus `session_created_at` which distinguishes successive
    incarnations reusing a stream id. `event_id` is monotonic, so "newest N" is
    an ORDER BY on it -- and the live index (stream_id, session_created_at,
    event_id DESC) serves exactly that.
    """

    def __init__(self, conn: sqlite3.Connection, table: str = EVENT_TABLE) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row or not row[0]:
            raise RetentionError(f"error: target DB has no `{table}` table (needed for --events)")
        self.table = table
        self.create_sql: str = row[0]
        info = list(conn.execute(f'PRAGMA table_info("{table}")'))
        self.columns = [r[1] for r in info]
        if "stream_id" not in self.columns:
            raise RetentionError(f"error: `{table}` has no `stream_id` column")
        pk = [r[1] for r in info if r[5]]
        self.row_key = pk[0] if len(pk) == 1 else "rowid"
        self.group_columns = ["stream_id"]
        if "session_created_at" in self.columns:
            self.group_columns.append("session_created_at")


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    """Whether `name` exists. The daemon may run against a fresh v2 DB that has
    no v1 `session_event_tail` yet, which is a skip, not an error."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _q(names: Iterable[str]) -> str:
    return ", ".join(f'"{n}"' for n in names)


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #


def _terminal_predicate(statuses: Sequence[str]) -> tuple[str, list]:
    marks = ", ".join("?" for _ in statuses)
    return f'"status" IN ({marks})', list(statuses)


def lineage_keepset(conn: sqlite3.Connection, schema: Schema, statuses: Sequence[str]) -> set[str]:
    """Stream ids of terminal rows that a live (non-terminal) row descends from.

    Walks ancestors transitively: a hot session's terminal parent is kept, and
    so is that parent's own terminal parent, so the chain never dangles.
    """
    if not schema.lineage_columns or not schema.stream_id_expr:
        return set()

    lineage_sel = ", ".join(f'"{c}"' for c in schema.lineage_columns)
    # id -> its ancestor ids, for every row in the table
    edges: dict[str, list[str]] = {}
    terminal_ids: set[str] = set()
    frontier: set[str] = set()
    for row in conn.execute(
        f"SELECT {schema.stream_id_expr} AS sid, \"status\", {lineage_sel} FROM sessions"
    ):
        sid, status = row[0], row[1]
        ancestors = [a for a in row[2:] if a]
        if sid is not None:
            edges[sid] = ancestors
            if status in statuses:
                terminal_ids.add(sid)
            else:
                frontier.update(ancestors)

    keep: set[str] = set()
    seen: set[str] = set()
    while frontier:
        sid = frontier.pop()
        if sid in seen:
            continue
        seen.add(sid)
        if sid in terminal_ids:
            keep.add(sid)
        frontier.update(a for a in edges.get(sid, ()) if a not in seen)
    return keep


# --------------------------------------------------------------------------- #
# event tail analysis
# --------------------------------------------------------------------------- #


def hot_stream_ids(conn: sqlite3.Connection, schema: Schema, statuses: Sequence[str]) -> set[str]:
    """Stream ids of sessions still non-terminal in the hot DB.

    Everything else -- terminal rows, rows the sessions pass already moved to
    the archive, and orphan stream ids with no session row at all -- is bulk
    history whose events belong in the archive. Lineage retention keeps a
    terminal *session row* hot so ancestry resolves; its events are not needed
    for that and are archived.
    """
    if not schema.stream_id_expr:
        return set()
    marks = ", ".join("?" for _ in statuses)
    return {
        r[0]
        for r in conn.execute(
            f'SELECT {schema.stream_id_expr} FROM sessions WHERE "status" NOT IN ({marks})',
            list(statuses),
        )
        if r[0] is not None
    }


def _load_hot_table(conn: sqlite3.Connection, hot: set[str]) -> None:
    conn.execute("DROP TABLE IF EXISTS temp.hot_streams")
    conn.execute("CREATE TEMP TABLE hot_streams (stream_id TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO temp.hot_streams VALUES (?)", [(h,) for h in hot])
    conn.commit()


def hot_group_counts(conn: sqlite3.Connection, ev: EventSchema) -> list[tuple]:
    """(group key..., row count) for each open session's event group."""
    sel = ", ".join(f't."{c}"' for c in ev.group_columns)
    grp = ", ".join(str(i + 1) for i in range(len(ev.group_columns)))
    return list(
        conn.execute(
            f'SELECT {sel}, COUNT(*) FROM main."{ev.table}" t '
            f"JOIN temp.hot_streams h ON t.stream_id = h.stream_id GROUP BY {grp}"
        )
    )


# --------------------------------------------------------------------------- #
# the passes
# --------------------------------------------------------------------------- #


def _detach(conn: sqlite3.Connection) -> None:
    """DETACH without masking an in-flight exception.

    An error inside the move loop leaves the batch transaction open, so a bare
    DETACH in `finally` raises "database arch is locked" and hides the real
    failure. Roll back first, and swallow a DETACH error on the way out.
    """
    try:
        conn.rollback()
    except sqlite3.Error:
        pass
    try:
        conn.execute("DETACH DATABASE arch")
    except sqlite3.Error:
        pass


def ensure_archive_table(archive_path: Path, create_sql: str, table: str) -> None:
    """Create `table`, or append missing source columns when the archive is a prefix."""
    conn = sqlite3.connect(str(archive_path))
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            conn.execute(create_sql)
            conn.commit()
            return
        have = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        src = sqlite3.connect(":memory:")
        try:
            src.execute(create_sql)
            want = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
            want_info = list(src.execute(f'PRAGMA table_info("{table}")'))
        finally:
            src.close()
        if table == "sessions" and len(have) < len(want) and have == want[:len(have)]:
            for column in want_info[len(have):]:
                name, declared_type = column[1], column[2]
                type_clause = f" {declared_type}" if declared_type else ""
                conn.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "{name}"{type_clause}'
                )
            conn.commit()
            have = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        if have != want:
            raise RetentionError(
                f"error: archive `{table}` columns differ from source "
                f"({len(have)} vs {len(want)}); migrate the archive DB first."
            )
    finally:
        conn.close()


def ensure_archive(archive_path: Path, schema: Schema) -> None:
    ensure_archive_table(archive_path, schema.create_sql, "sessions")


def archive_rows(
    conn: sqlite3.Connection,
    schema: Schema,
    archive_path: Path,
    statuses: Sequence[str],
    keep: set[str],
    batch_size: int,
    max_rows: int | None = None,
) -> int:
    """Move non-retained terminal rows to the archive DB, batch by batch.

    Each batch is one transaction spanning both files (INSERT into archive,
    DELETE from source) so a crash can at worst duplicate an already-archived
    row -- which the INSERT OR REPLACE makes harmless on re-run.

    `max_rows` caps the call and is what makes this reusable as the daemon's
    bounded unit of work: the CLI leaves it None and drains the table, while the
    cadence job passes a batch-sized cap so each trip through the store thread
    is short and the remainder simply falls to the next call.
    """
    ensure_archive(archive_path, schema)
    # ATTACH is illegal inside a transaction, and the daemon's store connection
    # is long-lived and shared with every other write.
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS arch", (str(archive_path),))
    cols = _q(schema.columns)
    keys = schema.key_columns
    key_sel = _q(keys)
    pred, params = _terminal_predicate(statuses)
    sid = schema.stream_id_expr

    moved = 0
    try:
        while max_rows is None or moved < max_rows:
            limit = batch_size if max_rows is None else min(batch_size, max_rows - moved)
            where = pred
            args = list(params)
            if keep and sid:
                marks = ", ".join("?" for _ in keep)
                where += f" AND ({sid}) NOT IN ({marks})"
                args.extend(sorted(keep))
            rows = conn.execute(
                f"SELECT {key_sel} FROM sessions WHERE {where} LIMIT ?",
                (*args, limit),
            ).fetchall()
            if not rows:
                break
            key_pred = " OR ".join(
                "(" + " AND ".join(f'"{k}" IS ?' for k in keys) + ")" for _ in rows
            )
            flat = [v for r in rows for v in r]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT OR REPLACE INTO arch.sessions ({cols}) "
                f"SELECT {cols} FROM main.sessions WHERE {key_pred}",
                flat,
            )
            conn.execute(f"DELETE FROM main.sessions WHERE {key_pred}", flat)
            conn.commit()
            moved += len(rows)
    finally:
        _detach(conn)
    return moved


def archive_events(
    conn: sqlite3.Connection,
    ev: EventSchema,
    archive_path: Path,
    tail_keep: int,
    batch_size: int,
    max_rows: int | None = None,
) -> tuple[int, int]:
    """Move archivable event rows out. Requires temp.hot_streams loaded.

    Returns (terminal_moved, tail_moved). Both passes are batched INSERT-then-
    DELETE inside bounded transactions, so a 1 GiB table never becomes one
    unbounded write; a crash mid-run leaves an already-inserted batch that the
    INSERT OR REPLACE makes harmless on re-run.

    `max_rows` is a budget shared across BOTH passes, so a capped call spends it
    on terminal rows first and only then on trimming open sessions' tails.
    """
    ensure_archive_table(archive_path, ev.create_sql, ev.table)
    conn.commit()  # see archive_rows: ATTACH cannot run inside a transaction
    conn.execute("ATTACH DATABASE ? AS arch", (str(archive_path),))
    cols = _q(ev.columns)
    key = f'"{ev.row_key}"' if ev.row_key != "rowid" else "rowid"
    tbl = f'"{ev.table}"'
    remaining = max_rows

    def move(where: str, params: list) -> int:
        nonlocal remaining
        total = 0
        while True:
            if remaining is not None:
                if remaining <= 0:
                    return total
                limit = min(batch_size, remaining)
            else:
                limit = batch_size
            ids = [
                r[0]
                for r in conn.execute(
                    f"SELECT {key} FROM main.{tbl} WHERE {where} LIMIT ?",
                    (*params, limit),
                )
            ]
            if not ids:
                return total
            marks = ", ".join("?" for _ in ids)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT OR REPLACE INTO arch.{tbl} ({cols}) "
                f"SELECT {cols} FROM main.{tbl} WHERE {key} IN ({marks})",
                ids,
            )
            conn.execute(f"DELETE FROM main.{tbl} WHERE {key} IN ({marks})", ids)
            conn.commit()
            total += len(ids)
            if remaining is not None:
                remaining -= len(ids)

    try:
        terminal_moved = move(
            '"stream_id" NOT IN (SELECT stream_id FROM temp.hot_streams)', []
        )
        tail_moved = 0
        for row in hot_group_counts(conn, ev):
            if remaining is not None and remaining <= 0:
                break
            count = row[-1]
            if count <= tail_keep:
                continue
            keys = list(row[:-1])
            grp_pred = " AND ".join(f'"{c}" IS ?' for c in ev.group_columns)
            cutoff = conn.execute(
                f"SELECT {key} FROM main.{tbl} WHERE {grp_pred} "
                f"ORDER BY {key} DESC LIMIT 1 OFFSET ?",
                (*keys, tail_keep - 1),
            ).fetchone()
            if not cutoff:
                continue
            tail_moved += move(f"{grp_pred} AND {key} < ?", [*keys, cutoff[0]])
    finally:
        _detach(conn)
    return terminal_moved, tail_moved


# --------------------------------------------------------------------------- #
# sizes
# --------------------------------------------------------------------------- #


def db_size(db_path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            total += p.stat().st_size
    return total


def human(n: int) -> str:
    val = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if val < 1024 or unit == "TiB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024
    return f"{val:.1f} TiB"


def freelist_bytes(conn: sqlite3.Connection) -> int:
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return int(page) * int(free)


# --------------------------------------------------------------------------- #
# the daemon's cadence job
# --------------------------------------------------------------------------- #

DEFAULT_INTERVAL_S = 6 * 3600.0
DEFAULT_MAX_ROWS_PER_PASS = 20_000
DEFAULT_VACUUM_MIN_BYTES = 64 * 1024 * 1024
DEFAULT_BACKOFF_BASE_S = 60.0
DEFAULT_BACKOFF_MAX_S = 3600.0

ENV_PREFIX = "PENTACLE_RETENTION_"


@dataclass
class RetentionConfig:
    """Loop-rule knobs (v2_design.md § Event loop rules, rule 2).

    cadence      `interval_s`, default 6h, env `PENTACLE_RETENTION_INTERVAL_S`
    per-pass cap `max_rows_per_pass`, split into `batch_size` store-thread trips
    backoff      exponential from `backoff_base_s`, capped at `backoff_max_s`
    kill switch  `--disable-retention` (main.py never constructs the job)
    """

    interval_s: float = DEFAULT_INTERVAL_S
    # None => wait a full interval before the first pass. Tests set this to a
    # fraction of a second; it is the "forced pass" trigger for a real daemon
    # process, so no test-only RPC verb has to exist on the wire.
    first_delay_s: float | None = None
    batch_size: int = DEFAULT_BATCH
    max_rows_per_pass: int = DEFAULT_MAX_ROWS_PER_PASS
    tail_keep: int = DEFAULT_TAIL_KEEP
    vacuum_min_bytes: int = DEFAULT_VACUUM_MIN_BYTES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S
    statuses: tuple[str, ...] = TERMINAL_STATUSES
    archive_path: Path | None = None
    # Read-only visibility into unreclaimed schedule prompt blobs.  Retention
    # never deletes from this shared content-addressed root.
    blob_root: Path | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "RetentionConfig":
        e = os.environ if env is None else env
        first = env_number(e, "FIRST_DELAY_S", None, float, prefix=ENV_PREFIX)
        return cls(
            interval_s=env_number(e, "INTERVAL_S", DEFAULT_INTERVAL_S, float, prefix=ENV_PREFIX),
            first_delay_s=first,
            batch_size=env_number(e, "BATCH", DEFAULT_BATCH, int, prefix=ENV_PREFIX),
            max_rows_per_pass=env_number(
                e, "MAX_ROWS", DEFAULT_MAX_ROWS_PER_PASS, int, prefix=ENV_PREFIX,
            ),
            tail_keep=env_number(e, "TAIL_KEEP", DEFAULT_TAIL_KEEP, int, prefix=ENV_PREFIX),
            vacuum_min_bytes=env_number(
                e, "VACUUM_MIN_BYTES", DEFAULT_VACUUM_MIN_BYTES, int, prefix=ENV_PREFIX,
            ),
        )


@dataclass
class PassResult:
    sessions_moved: int = 0
    events_terminal_moved: int = 0
    events_tail_moved: int = 0
    vacuumed: bool = False
    vacuum_skip_reason: str = ""
    reclaimable_bytes: int = 0
    size_before: int = 0
    size_after: int = 0
    skipped: str = ""
    schedule_dispatches_purged: int = 0
    schedules_purged: int = 0
    schedule_receipts_purged: int = 0
    schedule_blobs_unreclaimed: int = 0
    schedule_blob_bytes_unreclaimed: int = 0

    @property
    def events_moved(self) -> int:
        return self.events_terminal_moved + self.events_tail_moved

    _capped: bool = field(default=False, repr=False)

    @property
    def capped(self) -> bool:
        """True when the pass spent its whole per-pass cap, so work may remain."""
        return self._capped


class RetentionJob:
    """The archival passes on a cadence, driven from the asyncio loop but
    executed entirely on `store.py`'s worker thread.

    Loop rule 1 says the event loop does WS I/O and dispatch only, so not one
    sqlite call here happens inline: every unit of work is a callable handed to
    `Store.submit`. The unit is deliberately ONE BATCH, not one whole pass --
    the store thread is single-threaded and shared with every RPC, so a pass
    that drained a 1 GiB table in a single submitted callable would stall the
    daemon for minutes even though it never touched the event loop. Chunking
    lets queued requests interleave between batches.
    """

    def __init__(self, store: Any, config: RetentionConfig | None = None) -> None:
        self._store = store
        self.config = config or RetentionConfig()

    # -- one pass ----------------------------------------------------------

    async def run_pass(self) -> PassResult:
        """Run one bounded pass. This is also the test-only forced trigger:
        unit tests call it directly instead of waiting out the cadence."""
        cfg = self.config
        result = PassResult()
        db_path = getattr(self._store, "path", ":memory:")
        if not db_path or db_path == ":memory:":
            result.skipped = "in_memory_db"
            return result
        db = Path(db_path)
        archive_path = cfg.archive_path or db.parent / "sessions_archive.db"
        result.size_before = db_size(db)

        # Schedule durability shares this 6h cadence but has an independent
        # locked 500-row cap for each class.  One store-thread transaction keeps
        # the required dispatch -> schedule -> receipt deletion order atomic.
        schedule_counts = await self._submit(
            lambda conn: self._purge_schedule_classes(conn, cfg)
        )
        (
            result.schedule_dispatches_purged,
            result.schedules_purged,
            result.schedule_receipts_purged,
            result.schedule_blobs_unreclaimed,
            result.schedule_blob_bytes_unreclaimed,
        ) = schedule_counts

        # The lineage keep-set is computed ONCE per pass rather than per batch:
        # it is a full scan of `sessions`, and re-deriving it for every 500-row
        # chunk would make the pass quadratic. A session that closes mid-pass is
        # simply picked up by the next pass.
        prep = await self._submit(lambda conn: self._prepare(conn, cfg))
        keep, wants_events = prep

        budget = cfg.max_rows_per_pass
        moved = await self._drain(
            budget,
            lambda conn, limit: archive_rows(
                conn, Schema(conn), archive_path, cfg.statuses, keep, cfg.batch_size, max_rows=limit
            ),
        )
        result.sessions_moved = moved
        budget -= moved

        if wants_events and budget > 0:
            def _events(conn: sqlite3.Connection, limit: int) -> tuple[int, int]:
                # Rebuild the hot set each chunk: rows the sessions pass just
                # archived are no longer in `sessions`, and their events must
                # archive with them.
                schema = Schema(conn)
                _load_hot_table(conn, hot_stream_ids(conn, schema, cfg.statuses))
                return archive_events(
                    conn, EventSchema(conn), archive_path, cfg.tail_keep,
                    cfg.batch_size, max_rows=limit,
                )

            ev_moved = await self._drain_pairs(budget, _events)
            result.events_terminal_moved, result.events_tail_moved = ev_moved
            budget -= result.events_moved

        result._capped = budget <= 0 or any(
            count >= SCHEDULE_RETENTION_BATCH
            for count in schedule_counts[:3]
        )
        vac = await self._submit(lambda conn: self._maybe_vacuum(conn, cfg))
        result.vacuumed, result.vacuum_skip_reason, result.reclaimable_bytes = vac
        result.size_after = db_size(db)
        return result

    # -- background loop ---------------------------------------------------

    async def run_forever(self) -> None:
        """Cadence + backoff. Cancelled at shutdown; never swallows CancelledError."""
        cfg = self.config
        delay = cfg.interval_s if cfg.first_delay_s is None else cfg.first_delay_s
        failures = 0
        while True:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            try:
                started = time.monotonic()
                result = await self.run_pass()
                failures = 0
                # A capped pass has work left over; come back promptly rather
                # than sleeping out a full cadence on a backlog.
                delay = min(cfg.interval_s, 60.0) if result._capped else cfg.interval_s
                log.info(
                    "retention schedules: dispatches=%d schedules=%d receipts=%d "
                    "schedule_blobs_unreclaimed=%d bytes=%d",
                    result.schedule_dispatches_purged,
                    result.schedules_purged,
                    result.schedule_receipts_purged,
                    result.schedule_blobs_unreclaimed,
                    result.schedule_blob_bytes_unreclaimed,
                )
                if result.sessions_moved or result.events_moved or result.vacuumed:
                    log.info(
                        "retention: %d session row(s), %d event row(s), vacuum=%s in %.1fs "
                        "(%s -> %s)",
                        result.sessions_moved, result.events_moved,
                        result.vacuumed or result.vacuum_skip_reason,
                        time.monotonic() - started,
                        human(result.size_before), human(result.size_after),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** (failures - 1)))
                log.warning("retention pass failed (%d): %s; retrying in %.0fs", failures, exc, delay)

    # -- store-thread plumbing --------------------------------------------

    async def _submit(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        return await self._store.submit(fn)

    @staticmethod
    def _prepare(conn: sqlite3.Connection, cfg: RetentionConfig) -> tuple[set[str], bool]:
        schema = Schema(conn)
        keep = lineage_keepset(conn, schema, cfg.statuses)
        return keep, has_table(conn, EVENT_TABLE)

    @staticmethod
    def _purge_schedule_classes(
        conn: sqlite3.Connection, cfg: RetentionConfig,
    ) -> tuple[int, int, int, int, int]:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        cutoff = (now - timedelta(days=SCHEDULE_RETENTION_DAYS)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        terminal_marks = ",".join("?" for _ in SCHEDULE_TERMINAL_STATES)
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Class 1: dispatch evidence belonging to schedules old enough to
            # purge.  A schedule with more dispatch generations waits until all
            # its dispatch rows have drained in bounded batches.
            dispatch_rows = conn.execute(
                "SELECT d.rowid FROM v2_schedule_dispatches d "
                "JOIN v2_schedules s ON s.schedule_id=d.schedule_id "
                f"WHERE s.state IN ({terminal_marks}) AND s.terminal_at IS NOT NULL "
                "AND s.terminal_at<=? ORDER BY s.terminal_at,d.schedule_id,d.generation "
                "LIMIT ?",
                (*SCHEDULE_TERMINAL_STATES, cutoff, SCHEDULE_RETENTION_BATCH),
            ).fetchall()
            dispatch_ids = [int(row[0]) for row in dispatch_rows]
            if dispatch_ids:
                marks = ",".join("?" for _ in dispatch_ids)
                conn.execute(
                    f"DELETE FROM v2_schedule_dispatches WHERE rowid IN ({marks})",
                    dispatch_ids,
                )

            # Class 2: only rows whose dispatch evidence is fully gone.  Capture
            # blob ids for observability; the shared blob files are never mutated.
            schedule_rows = conn.execute(
                "SELECT s.schedule_id,s.prompt_blob_id FROM v2_schedules s "
                f"WHERE s.state IN ({terminal_marks}) AND s.terminal_at IS NOT NULL "
                "AND s.terminal_at<=? AND NOT EXISTS ("
                "SELECT 1 FROM v2_schedule_dispatches d WHERE d.schedule_id=s.schedule_id"
                ") ORDER BY s.terminal_at,s.schedule_id LIMIT ?",
                (*SCHEDULE_TERMINAL_STATES, cutoff, SCHEDULE_RETENTION_BATCH),
            ).fetchall()
            schedule_ids = [str(row[0]) for row in schedule_rows]
            blob_ids = [str(row[1]) for row in schedule_rows if row[1]]
            blob_bytes = 0
            if cfg.blob_root is not None:
                for blob_id in blob_ids:
                    try:
                        blob_bytes += int(
                            (Path(cfg.blob_root) / blob_id[:2] / blob_id).stat().st_size
                        )
                    except OSError:
                        pass
            if schedule_ids:
                marks = ",".join("?" for _ in schedule_ids)
                conn.execute(
                    f"DELETE FROM v2_schedules WHERE schedule_id IN ({marks})",
                    schedule_ids,
                )

            # Class 3: schedule receipts are pinned for as long as their target
            # exists; terminal settlement advances retain_until to row
            # terminal+30d, and a gone target lets the age predicate collect them.
            receipt_rows = conn.execute(
                "SELECT r.rowid FROM v2_operation_receipts r WHERE r.retain_until<=? AND "
                "r.surface='schedule' AND NOT EXISTS ("
                "SELECT 1 FROM v2_schedules s WHERE s.schedule_id=r.target_id"
                ") ORDER BY r.retain_until,r.receipt_id LIMIT ?",
                (now_iso, SCHEDULE_RETENTION_BATCH),
            ).fetchall()
            receipt_ids = [int(row[0]) for row in receipt_rows]
            if receipt_ids:
                marks = ",".join("?" for _ in receipt_ids)
                conn.execute(
                    f"DELETE FROM v2_operation_receipts WHERE rowid IN ({marks})",
                    receipt_ids,
                )
            conn.commit()
            return (
                len(dispatch_ids), len(schedule_ids), len(receipt_ids),
                len(blob_ids), blob_bytes,
            )
        except Exception:
            conn.rollback()
            raise

    async def _drain(self, budget: int, chunk: Callable[[sqlite3.Connection, int], int]) -> int:
        """Submit `chunk` one batch at a time until the budget or the work runs
        out, yielding to the loop between trips so RPCs are not starved."""
        moved = 0
        cfg = self.config
        while moved < budget:
            limit = min(cfg.batch_size, budget - moved)
            n = await self._submit(lambda conn, _l=limit: chunk(conn, _l))
            if not n:
                break
            moved += n
            await asyncio.sleep(0)
        return moved

    async def _drain_pairs(
        self, budget: int, chunk: Callable[[sqlite3.Connection, int], tuple[int, int]]
    ) -> tuple[int, int]:
        a = b = 0
        cfg = self.config
        while a + b < budget:
            limit = min(cfg.batch_size, budget - a - b)
            got_a, got_b = await self._submit(lambda conn, _l=limit: chunk(conn, _l))
            if not (got_a or got_b):
                break
            a += got_a
            b += got_b
            await asyncio.sleep(0)
        return a, b

    def _maybe_vacuum(self, conn: sqlite3.Connection, cfg: RetentionConfig) -> tuple[bool, str, int]:
        """VACUUM, but only when it is worth what it costs.

        Tradeoff, stated plainly: VACUUM rewrites the entire database and holds
        it for the duration -- and because every DB call in this daemon is
        serialized through the one store thread, a VACUUM is a hard stall for
        every queued RPC, not merely slow I/O. It is therefore gated twice:

          * by value  -- skipped unless the freelist the passes just created
                         exceeds `vacuum_min_bytes`, so a pass that reclaimed
                         nothing never pays the cost; and
          * by timing -- skipped outright if ANY request is already queued
                         behind us. Deferring costs one cadence interval; not
                         deferring costs live requests their latency.

        Running it here (on the store thread) rather than on a private
        connection is deliberate: a second connection would contend for the same
        write lock and the stall would happen anyway, minus the queue check.

        In WAL mode the VACUUM rewrite lands in the WAL, so the truncating
        checkpoint is what actually returns the space to the filesystem.
        """
        conn.commit()
        reclaimable = freelist_bytes(conn)
        if reclaimable < cfg.vacuum_min_bytes:
            return False, "below_threshold", reclaimable
        pending = getattr(self._store, "pending", None)
        if callable(pending) and pending() > 0:
            return False, "requests_queued", reclaimable
        prior = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.isolation_level = prior
        return True, "", reclaimable
