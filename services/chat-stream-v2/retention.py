"""retention.py — the daemon's bounded archival passes.

The store runs these passes on a cadence through its worker thread, keeping the
hot set O(open) without a second offline operator surface. The daemon is the
process holding the DB open and uses bounded batches rather than a whole-file
copy for crash safety.

The existing cadence performs these bounded passes:

  1. `archive_rows`   — terminal `sessions` rows move to `sessions_archive.db`,
                        except those a live row still descends from (lineage).
     Then `v2_session_generation_history` rows closed more than 30 days ago
     are deleted (never archived; open rows are kept), so per-generation usage
     history outlives the satellite held-span delivery horizon.
  2. `archive_events` — `session_event_tail` rows of terminal / already-archived
                        / orphan sessions move wholesale, and each still-open
                        session keeps only its newest `tail_keep` rows. This is
                        the pass that actually shrinks the file: measured on the
                        live DB, `sessions` archival alone took 1.7 -> 1.5 GiB
                        while the event tail held 1.18 GB + 208 MB of indexes.
  3. Receipt/route histories move with an independent durable archive commit,
     full equality verification, and a separate revalidated hot deletion.
     Per transaction: at most 500 rows / 1 MiB payload. Per pass: 2,000 keys
     examined across alternating families. Compaction is offline maintenance.

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


def default_archive_path(db: Path) -> Path:
    return Path(db).parent / "sessions_archive.db"


class RetentionError(Exception):
    """A pass cannot proceed safely (bad schema, mismatched archive, ...)."""


# Receipt/route history is a paired store, not an attached-database transaction.
# The archive commits first. Its rows are immutable and identities are never reused.
RECEIPT_TABLE = "v2_send_receipts"
ROUTE_TABLE = "v2_assistant_composite_routes"
HISTORY_TABLES = (RECEIPT_TABLE, ROUTE_TABLE)
_ROUTE_BATCH_COLUMNS = (("batched_into_dispatch_id", "TEXT"), ("answered_by_event_id", "INTEGER"))
HISTORY_MAX_ROWS = 500
HISTORY_MAX_BYTES = 1024 * 1024
HISTORY_PASS_ROWS = 2000
MAX_RECEIPT_ROWID = 2**63 - 1
HISTORY_SEQUENCE_DDL = """CREATE TABLE IF NOT EXISTS v2_send_receipt_sequence (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    high_water INTEGER NOT NULL CHECK(high_water>=0),
    pair_id TEXT NOT NULL, archive_path TEXT NOT NULL,
    archive_ready INTEGER NOT NULL CHECK(archive_ready IN (0,1)))"""
HISTORY_MARKER_DDL = """CREATE TABLE v2_receipt_route_archive (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL CHECK(version=1), pair_id TEXT NOT NULL)"""


class HistoryConnection(sqlite3.Connection):
    """Store-owned connection state; never accessed from the asyncio thread."""
    history_archive_path: Path | None = None
    history_attachment_identity: tuple[int, int] | None = None
    history_schema_version: int | None = None


def _columns(conn, table, schema="main"):
    return [r[1] for r in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _sequence(conn):
    return conn.execute("SELECT * FROM v2_send_receipt_sequence WHERE singleton=1").fetchone()


def prepare_history_schema(conn, *, fresh: bool, archive_path: Path | None) -> None:
    """Additive migration only; an existing missing archive is never bootstrapped."""
    import uuid
    conn.execute(HISTORY_SEQUENCE_DDL)
    # Keys also bound dependency probes. Do not predicate-scan unrelated history.
    indexes = (
        (RECEIPT_TABLE, "receipt_key", "to_stream_id,request_id"),
        (RECEIPT_TABLE, "receipt_request", "request_id"),
        (RECEIPT_TABLE, "receipt_optimistic", "to_stream_id,optimistic_id"),
        (ROUTE_TABLE, "route_request", "stream_id,input_request_id"),
        (ROUTE_TABLE, "route_request_actor", "input_request_id"),
        (ROUTE_TABLE, "route_dependency", "stream_id,depends_on_message_id"),
        (ROUTE_TABLE, "route_reply", "stream_id,reply_to_message_id"),
        (ROUTE_TABLE, "route_target", "route_target,route_target_generation"),
        ("v2_assistant_composite_operations", "operation_dispatch", "stream_id,dispatch_id"),
        ("v2_assistant_composite_operations", "operation_reply", "stream_id,reply_to_message_id"),
        ("v2_assistant_composite_publications", "publication_input", "stream_id,reply_to_message_id,created_at DESC"),
        ("v2_assistant_composite_publications", "publication_dispatch", "stream_id,dispatch_id"),
        ("v2_assistant_composite_question_bridges", "bridge_scope", "stream_id,lane_id"),
        ("v2_assistant_composite_question_bridges", "bridge_state", "stream_id,state"),
        ("sessions", "session_handoff", "handoff_from_stream_id"),
    )
    for table, name, columns in indexes:
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_history_{name} ON {table}({columns})")
    if fresh and _sequence(conn) is None:
        conn.execute("INSERT INTO v2_send_receipt_sequence VALUES(1,0,?,?,?)",
                     (uuid.uuid4().hex, str(archive_path or ""), int(archive_path is None)))
    row = _sequence(conn)
    if isinstance(conn, HistoryConnection):
        conn.history_archive_path = Path(row["archive_path"]) if row and row["archive_path"] else archive_path
    conn.create_function("history_identity_conflict", 0, _history_identity_conflict)


def _history_identity_conflict():
    raise RetentionError("conflicting hot/archive history identity")


def _archive_schema(conn, archive, *, initialize: bool, pair_id: str, migrate_route_batch: bool = False) -> None:
    """Require the exact receipt/route schema, including the rowid alias and keys."""
    tables = {r[0] for r in archive.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    marker = "v2_receipt_route_archive" in tables
    if marker:
        row = archive.execute("SELECT version,pair_id FROM v2_receipt_route_archive WHERE singleton=1").fetchone()
        if row is None or tuple(row) != (1, pair_id):
            raise RetentionError("archive pair/format mismatch")
    elif not initialize:
        raise RetentionError("archive pair marker missing")
    for table in HISTORY_TABLES:
        expected = [tuple(r)[1:] for r in conn.execute(f'PRAGMA main.table_info("{table}")')]
        if table == RECEIPT_TABLE:
            expected = [("receipt_rowid", "INTEGER", 0, None, 1), *expected]
        if table not in tables:
            if not initialize or marker:
                raise RetentionError(f"archive schema missing {table}")
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            if table == RECEIPT_TABLE:
                left = ddl.index("(") + 1
                ddl = ddl[:left] + "receipt_rowid INTEGER PRIMARY KEY," + ddl[left:]
            archive.execute(ddl)
        actual = [tuple(r)[1:] for r in archive.execute(f'PRAGMA table_info("{table}")')]
        if migrate_route_batch and table == ROUTE_TABLE and actual != expected:
            additions = _ROUTE_BATCH_COLUMNS
            names = {column[0] for column in actual}
            missing = [(name, ddl) for name, ddl in additions if name not in names]
            missing_names = {name for name, _ in missing}
            # Only the exact known nullable batch-column delta is migratable.
            if (missing and actual == [column for column in expected if column[0] not in missing_names]
                    and all((name, ddl, 0, None, 0) in expected for name, ddl in missing)):
                for name, ddl in missing:
                    archive.execute(f'ALTER TABLE "{table}" ADD COLUMN {name} {ddl}')
                actual = [tuple(r)[1:] for r in archive.execute(f'PRAGMA table_info("{table}")')]
        if actual != expected:
            raise RetentionError(f"incompatible archive schema: {table}")
        if table == RECEIPT_TABLE:
            # INTEGER PRIMARY KEY DESC is not a rowid alias. Verify actual identity.
            sql = archive.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            import re
            if (re.search(r"receipt_rowid\s+INTEGER\s+PRIMARY\s+KEY\s+DESC", sql, re.I)
                    or any(index[3] == "pk" for index in archive.execute(f'PRAGMA index_list("{table}")'))):
                raise RetentionError("archive receipt identity is not a rowid alias")
        else:
            keys = {tuple(r[2] for r in archive.execute(f'PRAGMA index_info("{i[1]}")'))
                    for i in archive.execute(f'PRAGMA index_list("{table}")') if i[2]}
            if not {("route_id",), ("stream_id", "input_identity"), ("dispatch_id",)} <= keys:
                raise RetentionError("archive route identity keys incompatible")
    if initialize and not marker and has_table(conn, EVENT_TABLE) and EVENT_TABLE not in tables:
        # A fresh pair has known empty event history. The existing late-proof
        # scanner requires the event table to distinguish empty from a gap.
        archive.execute(conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (EVENT_TABLE,)).fetchone()[0])
    if not marker:
        archive.execute(HISTORY_MARKER_DDL)
        archive.execute("INSERT INTO v2_receipt_route_archive VALUES(1,1,?)", (pair_id,))
    if initialize:
        archive.execute(f"CREATE INDEX IF NOT EXISTS idx_archive_receipt_key ON {RECEIPT_TABLE}(to_stream_id,request_id,receipt_rowid)")
        archive.execute(f"CREATE INDEX IF NOT EXISTS idx_archive_receipt_request ON {RECEIPT_TABLE}(request_id)")
        archive.execute(f"CREATE INDEX IF NOT EXISTS idx_archive_receipt_optimistic ON {RECEIPT_TABLE}(to_stream_id,optimistic_id)")
        archive.execute(f"CREATE INDEX IF NOT EXISTS idx_archive_route_request ON {ROUTE_TABLE}(input_request_id)")
        archive.commit()



def migrate_route_batch_archive(conn) -> None:
    """Extend a matched pre-batching archive after the hot migration commits."""
    path = getattr(conn, "history_archive_path", None)
    row = _sequence(conn)
    if path is None or row is None or not row["archive_ready"] or not Path(path).exists():
        return
    try:
        archive = _open_archive(path, readonly=True)
    except RetentionError:
        return  # Preserve hot-only allocation during an archive outage.
    try:
        expected = [tuple(column)[1:] for column in conn.execute(f'PRAGMA table_info("{ROUTE_TABLE}")')]
        actual = [tuple(column)[1:] for column in archive.execute(f'PRAGMA table_info("{ROUTE_TABLE}")')]
        names = {column[0] for column in actual}
        missing = [(name, ddl) for name, ddl in _ROUTE_BATCH_COLUMNS if name not in names]
        missing_names = {name for name, _ in missing}
        if (not missing or actual != [column for column in expected if column[0] not in missing_names]
                or not all((name, ddl, 0, None, 0) in expected for name, ddl in missing)):
            return  # Other schemas retain the existing strict read-time refusal.
    except sqlite3.Error:
        return  # A corrupt archive remains a strict read gap, not a new archive.
    finally:
        archive.close()
    archive = None
    try:
        archive = _open_archive(path)
        archive.execute("BEGIN IMMEDIATE")
        _archive_schema(conn, archive, initialize=False, pair_id=row["pair_id"],
                        migrate_route_batch=True)
        archive.commit()
    except (RetentionError, sqlite3.Error):
        return  # Unavailable/unsupported archives remain strict read-time gaps.
    finally:
        if archive is not None:
            archive.close()  # Any schema/identity refusal rolls back the entire upgrade.


def _open_archive(path: Path, *, create=False, readonly=False):
    mode = "rwc" if create else "ro" if readonly else "rw"
    try:
        result = sqlite3.connect(Path(path).absolute().as_uri() + f"?mode={mode}", uri=True, timeout=0.1)
        result.row_factory = sqlite3.Row
        if not readonly:
            result.execute("PRAGMA synchronous=FULL")
        return result
    except (sqlite3.Error, OSError) as exc:
        raise RetentionError("required history archive unavailable") from exc


def initialize_history_pair(conn) -> None:
    """Seed within the caller's transaction; never commit a composite admission."""
    import uuid
    row = _sequence(conn)
    path = getattr(conn, "history_archive_path", None)
    if path is None:
        if row is None:
            conn.execute("INSERT INTO v2_send_receipt_sequence VALUES(1,0,?,'',1)", (uuid.uuid4().hex,))
        return
    if row is not None and row["archive_ready"]:
        return
    pair_id = row["pair_id"] if row else None
    archive = _open_archive(path, create=bool(row and not row["archive_ready"]))
    try:
        if pair_id is None:
            marker = archive.execute("SELECT 1 FROM sqlite_master WHERE name='v2_receipt_route_archive'").fetchone()
            pair_id = (archive.execute("SELECT pair_id FROM v2_receipt_route_archive WHERE singleton=1").fetchone()[0]
                       if marker else uuid.uuid4().hex)
        _archive_schema(conn, archive, initialize=True, pair_id=pair_id)
        maximum = max(int(conn.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {RECEIPT_TABLE}").fetchone()[0]),
                      int(archive.execute(f"SELECT COALESCE(MAX(receipt_rowid),0) FROM {RECEIPT_TABLE}").fetchone()[0]),
                      int(row["high_water"]) if row else 0)
        if row is None:
            conn.execute("INSERT INTO v2_send_receipt_sequence VALUES(1,?,?,?,1)",
                         (maximum, pair_id, str(path)))
        else:
            conn.execute("UPDATE v2_send_receipt_sequence SET high_water=?,archive_ready=1 WHERE singleton=1", (maximum,))
    finally:
        archive.close()


def bind_history_archive(conn, path: Path) -> None:
    """Bind configuration, or explicitly relocate a validated matching pair."""
    path = Path(path).absolute()
    current = getattr(conn, "history_archive_path", None)
    if current == path:
        return
    row = _sequence(conn)
    if row is None or not row["archive_ready"]:
        # Explicit configuration requires an existing readable archive. Default
        # fresh-pair bootstrap is the only path allowed to create a missing file.
        target = _open_archive(path, readonly=True)
        target.close()
    else:
        target = _open_archive(path, readonly=True)
        try:
            marker = target.execute("SELECT 1 FROM sqlite_master WHERE name='v2_receipt_route_archive'").fetchone()
            same_pair = bool(marker and target.execute("SELECT pair_id FROM v2_receipt_route_archive WHERE singleton=1").fetchone()[0] == row["pair_id"])
        finally:
            target.close()
        if not same_pair:
            old = _open_archive(Path(row["archive_path"]), readonly=True)
            try:
                _archive_schema(conn, old, initialize=False, pair_id=row["pair_id"])
                owner_tables = [entry[0] for entry in old.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "AND name!='v2_receipt_route_archive'")]
                if any(old.execute(f'SELECT 1 FROM "{table.replace(chr(34), chr(34)*2)}" LIMIT 1').fetchone()
                       for table in owner_tables):
                    raise RetentionError("cannot switch a populated history archive")
            finally:
                old.close()
    if getattr(conn, "history_attachment_identity", None) is not None:
        if conn.in_transaction:
            raise RetentionError("archive rebinding requires a transaction boundary")
        conn.execute("DETACH DATABASE receipt_archive")
        conn.history_attachment_identity = None
    conn.history_archive_path = path
    if row:
        conn.execute("UPDATE v2_send_receipt_sequence SET archive_path=?,archive_ready=0 WHERE singleton=1", (str(path),))
    initialize_history_pair(conn)
    conn.commit()


def _attached_history(conn):
    initialize_history_pair(conn)
    path = getattr(conn, "history_archive_path", None)
    if path is None:
        return None
    # Check the name as well as the open SQLite handle: an unlinked inode is an
    # outage, not an indefinitely healthy historical snapshot.
    try:
        stat = Path(path).stat()
        identity = (stat.st_dev, stat.st_ino)
    except OSError as exc:
        raise RetentionError("history archive path unavailable") from exc
    previous = getattr(conn, "history_attachment_identity", None)
    if previous is not None and previous != identity:
        raise RetentionError("history archive replaced; reopen the Store after validating the pair")
    if previous is None:
        archive = _open_archive(path, readonly=True)
        try:
            _archive_schema(conn, archive, initialize=False, pair_id=_sequence(conn)["pair_id"])
        finally:
            archive.close()
        conn.execute("ATTACH DATABASE ? AS receipt_archive", (Path(path).absolute().as_uri() + "?mode=ro",))
        conn.history_attachment_identity = identity
    version = conn.execute("PRAGMA receipt_archive.schema_version").fetchone()[0]
    if version != getattr(conn, "history_schema_version", None):
        archive = _open_archive(path, readonly=True)
        try:
            _archive_schema(conn, archive, initialize=False, pair_id=_sequence(conn)["pair_id"])
        finally:
            archive.close()
        conn.history_schema_version = version
    marker = conn.execute("SELECT version,pair_id FROM receipt_archive.v2_receipt_route_archive WHERE singleton=1").fetchone()
    if marker is None or tuple(marker) != (1, _sequence(conn)["pair_id"]):
        raise RetentionError("history archive pair mismatch")
    return "receipt_archive"


def next_receipt_rowid(conn) -> int:
    """The caller owns BEGIN/COMMIT/ROLLBACK. Initialized allocation needs no archive."""
    initialize_history_pair(conn)
    row = _sequence(conn)
    highest = int(conn.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {RECEIPT_TABLE}").fetchone()[0])
    value = max(int(row["high_water"]), highest)
    if value >= MAX_RECEIPT_ROWID:
        raise RetentionError("send receipt identity exhausted")
    value += 1
    conn.execute("UPDATE v2_send_receipt_sequence SET high_water=? WHERE singleton=1", (value,))
    return value


def history_union_sql(conn, table, archive_schema, *, receipt_stream=False):
    """Combine before ordering/filtering; validate selected duplicate payloads in BOTH arms."""
    columns = _columns(conn, table)
    key = "receipt_rowid" if table == RECEIPT_TABLE else "route_id"
    hot_key = "rowid" if table == RECEIPT_TABLE else "route_id"
    equal = " AND ".join(f'h."{c}" IS a."{c}"' for c in columns)
    hot = ("h.rowid AS rowid," if table == RECEIPT_TABLE else "") + ",".join(f'h."{c}"' for c in columns)
    cold = ("a.receipt_rowid AS rowid," if table == RECEIPT_TABLE else "") + ",".join(f'a."{c}"' for c in columns)
    hot_scope = " AND h.to_stream_id=?" if receipt_stream else ""
    cold_scope = " AND a.to_stream_id=?" if receipt_stream else ""
    join = f'a."{key}"=h."{hot_key}"'
    if table == ROUTE_TABLE:
        join += " OR (a.stream_id=h.stream_id AND a.input_identity=h.input_identity) OR a.dispatch_id=h.dispatch_id"
    return (f'SELECT {hot} FROM main."{table}" h LEFT JOIN {archive_schema}."{table}" a '
            f'ON ({join}) WHERE CASE WHEN a."{key}" IS NULL THEN 1 '
            f'WHEN {equal} THEN 1 ELSE history_identity_conflict() END {hot_scope} '
            f'UNION ALL SELECT {cold} FROM {archive_schema}."{table}" a LEFT JOIN main."{table}" h '
            f'ON ({join}) WHERE CASE WHEN h."{hot_key}" IS NULL THEN 1 '
            f'WHEN {equal} THEN 0 ELSE history_identity_conflict() END {cold_scope}')


def history_query(conn, sql, parameters=(), *, receipt_stream=None):
    """Explicit historical reader seam. Mutators keep their protected hot joins."""
    import re
    owned = not conn.in_transaction
    try:
        schema = _attached_history(conn)
        if owned and conn.in_transaction:
            conn.commit()  # only initialization; never a caller's transaction
        if schema is None:
            return conn.execute(sql, parameters)
        definitions = []
        prefix_parameters = []
        for table in HISTORY_TABLES:
            if re.search(r"\b" + table + r"\b", sql):
                alias = "retained_" + table
                scoped = table == RECEIPT_TABLE and receipt_stream is not None
                definitions.append(f'{alias} AS NOT MATERIALIZED ({history_union_sql(conn, table, schema, receipt_stream=scoped)})')
                if scoped:
                    prefix_parameters.extend((receipt_stream, receipt_stream))
                sql = re.sub(r"\b" + table + r"\b", alias, sql)
        return conn.execute("WITH " + ",".join(definitions) + " " + sql, (*prefix_parameters, *parameters))
    except sqlite3.Error as exc:
        raise RetentionError("history archive coverage gap or conflicting identity") from exc


def _settled_receipt(conn, row, cutoff) -> bool:
    latest = conn.execute(f"SELECT state,delivery,submission_confirmed,created_at FROM {RECEIPT_TABLE} "
                          "WHERE to_stream_id=? AND request_id=? ORDER BY rowid DESC LIMIT 1",
                          (row["to_stream_id"], row["request_id"])).fetchone()
    if not latest or not _old_enough(latest["created_at"], cutoff):
        return False
    if not ((latest["state"] == "landed" and latest["delivery"] == "landed" and latest["submission_confirmed"])
            or (latest["state"] == "not_landed" and latest["delivery"] == "not_landed")):
        return False
    host, sep, name = row["to_stream_id"].partition(":")
    if sep:
        session = conn.execute("SELECT created_at FROM sessions WHERE host=? AND session_name=?", (host, name)).fetchone()
        if session and (_epoch(session[0]) is None or _epoch(row["created_at"]) >= _epoch(session[0])):
            return False  # current generation proof, including closed-but-retained seats
    if conn.execute(f"SELECT 1 FROM {ROUTE_TABLE} WHERE stream_id=? AND input_request_id=? LIMIT 1",
                    (row["to_stream_id"], row["request_id"])).fetchone():
        return False
    if row["optimistic_id"] and conn.execute(f"SELECT 1 FROM {ROUTE_TABLE} WHERE stream_id=? AND input_identity=?",
                                             (row["to_stream_id"], row["optimistic_id"])).fetchone():
        return False
    return True


def _settled_route(conn, row, cutoff) -> bool:
    if (row["routing_state"] not in ("resolved", "routing_failed")
            or row["delivery_state"] not in ("landed", "failed")
            or not _old_enough(row["updated_at"], cutoff)
            or row["lease_owner"] or row["lease_until"] is not None
            or row["depends_on_message_id"] or row["reply_to_question_id"]):
        return False
    # Standing operator authority has no age expiry. Completed lanes can reopen.
    if str(row["actor_stream_id"] or "").startswith("operator:"):
        return False
    import json
    try:
        payload = json.loads(row["route_json"] or "{}")
        if not isinstance(payload, dict) or payload.get("lane_id"):
            return False
    except (ValueError, TypeError):
        return False
    stream, identity, dispatch = row["stream_id"], row["input_identity"], row["dispatch_id"]
    if dispatch and (not row["route_target"] or not row["route_target_generation"]):
        return False  # incomplete dispatch scope is not proof of immutability
    if conn.execute("SELECT 1 FROM v2_assistant_composite_question_bridges WHERE stream_id=? AND state='prepared' LIMIT 1", (stream,)).fetchone():
        return False  # prepared bridges do not retain a separately queryable dispatch key
    for field in ("depends_on_message_id", "reply_to_message_id"):
        if conn.execute(f"SELECT 1 FROM {ROUTE_TABLE} WHERE stream_id=? AND {field}=? LIMIT 1", (stream, identity)).fetchone():
            return False
    if conn.execute("SELECT 1 FROM v2_assistant_composite_operations WHERE stream_id=? AND reply_to_message_id=? LIMIT 1", (stream, identity)).fetchone():
        return False
    if dispatch and conn.execute("SELECT 1 FROM v2_assistant_composite_operations WHERE stream_id=? AND dispatch_id=? LIMIT 1", (stream, dispatch)).fetchone():
        return False
    if row["route_target"]:
        host, sep, name = row["route_target"].partition(":")
        if not sep:
            return False
        current = conn.execute("SELECT g.generation FROM sessions s LEFT JOIN v2_session_generations g "
                               "ON s.host=g.host AND s.session_name=g.session_name WHERE s.host=? AND s.session_name=?", (host, name)).fetchone()
        if current and (not row["route_target_generation"] or current[0] == row["route_target_generation"]):
            return False
        if conn.execute("SELECT 1 FROM sessions WHERE handoff_from_stream_id=? LIMIT 1", (row["route_target"],)).fetchone():
            return False
    # Real dispatched inputs retain publication obligations until a final result.
    if dispatch and row["delivery_state"] == "landed":
        final = conn.execute(
            "SELECT created_at FROM v2_assistant_composite_publications WHERE stream_id=? AND reply_to_message_id=? "
            "AND (publish_kind='result' OR json_extract(CASE WHEN json_valid(canonical_payload_json) "
            "THEN canonical_payload_json ELSE '{}' END,'$.response_state')='final') "
            "ORDER BY created_at DESC LIMIT 1", (stream, identity)).fetchone()
        if final is None or not _old_enough(final[0], cutoff):
            return False
    return True


def _epoch(stamp):
    try:
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return value.timestamp() if value.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _old_enough(stamp, cutoff):
    value = _epoch(stamp)
    return value is not None and value <= cutoff.timestamp()


def history_checkpoint(stage, table, rows):
    """Crash-test seam at durable boundaries; production has no side effects."""


def move_history_batch(conn, table, *, after, cutoff, limit=HISTORY_MAX_ROWS):
    """Bound input keys AND materialized bytes; archive commit -> verify -> hot CAS."""
    initialize_history_pair(conn)
    conn.commit()
    _attached_history(conn)
    path = conn.history_archive_path
    columns = _columns(conn, table)
    key = "rowid" if table == RECEIPT_TABLE else "route_id"
    archive_key = "receipt_rowid" if table == RECEIPT_TABLE else key
    budget = min(HISTORY_MAX_ROWS, max(0, int(limit)))
    sizes = "+".join(f'COALESCE(octet_length("{c}"),0)' for c in columns) + "+8"
    candidates = conn.execute(f'SELECT {key} AS identity,({sizes}) AS bytes FROM "{table}" '
                              f'WHERE {key}>? ORDER BY {key} LIMIT ?', (after, budget)).fetchall()
    rows, materialized, oversized, examined = [], 0, 0, 0
    cursor = after
    eligible = _settled_receipt if table == RECEIPT_TABLE else _settled_route
    for candidate in candidates:
        size = int(candidate["bytes"])
        if size > HISTORY_MAX_BYTES:
            oversized += 1
            examined += 1
            cursor = candidate["identity"]
            continue
        if materialized + size > HISTORY_MAX_BYTES:
            break
        cursor = candidate["identity"]
        examined += 1
        row = conn.execute(f'SELECT {key} AS history_identity,* FROM "{table}" WHERE {key}=?', (cursor,)).fetchone()
        materialized += size  # includes protected candidates, not just moved rows
        if row and _old_enough(row["created_at"], cutoff) and eligible(conn, row, cutoff):
            rows.append(dict(row))
    if not candidates or (examined == len(candidates) and len(candidates) < budget):
        cursor = 0 if table == RECEIPT_TABLE else ""
    result = dict(moved=0, examined=examined, materialized_bytes=materialized, oversized=oversized, cursor=cursor)
    if oversized:
        log.warning("retention history retained %d oversized %s row(s)", oversized, table)
    if not rows:
        return result
    archive = _open_archive(path)
    try:
        _archive_schema(conn, archive, initialize=False, pair_id=_sequence(conn)["pair_id"])
        archive.execute("BEGIN IMMEDIATE")
        try:
            archive_columns = (["receipt_rowid"] if table == RECEIPT_TABLE else []) + columns
            for row in rows:
                values = ([row["history_identity"]] if table == RECEIPT_TABLE else []) + [row[c] for c in columns]
                equality = " AND ".join(f'"{c}" IS ?' for c in archive_columns)
                prior = archive.execute(f'SELECT ({equality}) FROM "{table}" WHERE "{archive_key}"=?',
                                        (*values, row["history_identity"])).fetchone()
                if prior is not None:
                    if not prior[0]:
                        raise RetentionError("conflicting archive history identity; no hot rows deleted")
                else:
                    archive.execute(f'INSERT INTO "{table}" ({_q(archive_columns)}) VALUES ({",".join("?" for _ in values)})', values)
            archive.commit()
        except BaseException:
            archive.rollback()
            raise
        history_checkpoint("archive_committed", table, rows)
        # Pin the committed archive against a second writer through hot
        # deletion, including configured WAL archives. This is a new archive
        # transaction after the durable copy, not cross-database atomicity.
        archive.execute("BEGIN IMMEDIATE")
        for row in rows:
            expected = ([row["history_identity"]] if table == RECEIPT_TABLE else []) + [row[c] for c in columns]
            equality = " AND ".join(f'"{c}" IS ?' for c in archive_columns)
            verified = archive.execute(f'SELECT 1 FROM "{table}" WHERE "{archive_key}"=? AND {equality}',
                                       (row["history_identity"], *expected)).fetchone()
            if verified is None:
                raise RetentionError("archive full-payload verification failed")
        history_checkpoint("verified", table, rows)
        _attached_history(conn)  # detect disappeared/replaced archive before deletion
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                equality = " AND ".join(f'"{c}" IS ?' for c in columns)
                current = conn.execute(f'SELECT 1 FROM "{table}" WHERE {key}=? AND {equality}',
                                       (row["history_identity"], *[row[c] for c in columns])).fetchone()
                if current is not None and _old_enough(row["created_at"], cutoff) and eligible(conn, row, cutoff):
                    conn.execute(f'DELETE FROM "{table}" WHERE {key}=?', (row["history_identity"],))
                    result["moved"] += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        history_checkpoint("hot_deleted", table, rows)
        return result
    finally:
        archive.close()


def restore_history_batch(conn, *, table, after, limit=HISTORY_MAX_ROWS):
    """Compatible offline recovery. Retain archive and intervening hot writes."""
    initialize_history_pair(conn)
    conn.commit()
    _attached_history(conn)
    columns = _columns(conn, table)
    key = "receipt_rowid" if table == RECEIPT_TABLE else "route_id"
    hot_key = "rowid" if table == RECEIPT_TABLE else key
    archive = _open_archive(conn.history_archive_path, readonly=True)
    try:
        sizes = "+".join(f'COALESCE(octet_length("{c}"),0)' for c in columns) + "+8"
        candidates = archive.execute(f'SELECT "{key}",({sizes}) AS bytes FROM "{table}" WHERE "{key}">? '
                                     f'ORDER BY "{key}" LIMIT ?', (after, min(HISTORY_MAX_ROWS, limit))).fetchall()
        rows, materialized, cursor = [], 0, after
        for candidate in candidates:
            size = int(candidate["bytes"])
            if size > HISTORY_MAX_BYTES:
                raise RetentionError("oversized archived row requires offline recovery review")
            if materialized + size > HISTORY_MAX_BYTES:
                break
            cursor = candidate[key]
            row = archive.execute(f'SELECT * FROM "{table}" WHERE "{key}"=?', (cursor,)).fetchone()
            rows.append(dict(row)); materialized += size
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                equality = " AND ".join(f'"{c}" IS ?' for c in columns)
                current = conn.execute(f'SELECT ({equality}) FROM "{table}" WHERE {hot_key}=?',
                                       (*[row[c] for c in columns], row[key])).fetchone()
                if current is not None:
                    if not current[0]:
                        raise RetentionError("recovery identity collision; intervening writes preserved")
                else:
                    names = (["rowid"] if table == RECEIPT_TABLE else []) + columns
                    values = ([row[key]] if table == RECEIPT_TABLE else []) + [row[c] for c in columns]
                    conn.execute(f'INSERT INTO "{table}" ({_q(names)}) VALUES ({",".join("?" for _ in values)})', values)
            maximum = max(int(_sequence(conn)["high_water"]),
                          int(conn.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {RECEIPT_TABLE}").fetchone()[0]),
                          int(archive.execute(f"SELECT COALESCE(MAX(receipt_rowid),0) FROM {RECEIPT_TABLE}").fetchone()[0]))
            conn.execute("UPDATE v2_send_receipt_sequence SET high_water=? WHERE singleton=1", (maximum,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return dict(restored=len(rows), cursor=cursor, materialized_bytes=materialized, done=not candidates)
    finally:
        archive.close()


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


def ensure_archive_table(archive_path: Path, create_sql: str, table: str, *, require_existing: bool = False) -> None:
    """Create `table`, or append missing source columns when the archive is a prefix."""
    target = archive_path.absolute().as_uri() + '?mode=rw' if require_existing else str(archive_path.absolute())
    conn = sqlite3.connect(target, uri=require_existing)
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


def ensure_archive(archive_path: Path, schema: Schema, *, require_existing: bool = False) -> None:
    ensure_archive_table(archive_path, schema.create_sql, "sessions", require_existing=require_existing)


def archive_rows(
    conn: sqlite3.Connection,
    schema: Schema,
    archive_path: Path,
    statuses: Sequence[str],
    keep: set[str],
    batch_size: int,
    max_rows: int | None = None,
    *, require_existing: bool = False,
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
    ensure_archive(archive_path, schema, require_existing=require_existing)
    # ATTACH is illegal inside a transaction, and the daemon's store connection
    # is long-lived and shared with every other write.
    conn.commit()
    # Store enables URI handling, so mode=rw forbids creation even if the
    # archive disappears after preflight or table preparation.
    target = archive_path.absolute().as_uri() + '?mode=rw' if require_existing else str(archive_path.absolute())
    conn.execute("ATTACH DATABASE ? AS arch", (target,))
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
    protected_stream_ids: set[str] | None = None,
    *, require_existing: bool = False,
) -> tuple[int, int]:
    """Move archivable event rows out. Requires temp.hot_streams loaded.

    Returns (terminal_moved, tail_moved). Both passes are batched INSERT-then-
    DELETE inside bounded transactions, so a 1 GiB table never becomes one
    unbounded write; a crash mid-run leaves an already-inserted batch that the
    INSERT OR REPLACE makes harmless on re-run.

    `max_rows` is a budget shared across BOTH passes, so a capped call spends it
    on terminal rows first and only then on trimming open sessions' tails.
    """
    ensure_archive_table(archive_path, ev.create_sql, ev.table, require_existing=require_existing)
    conn.commit()  # see archive_rows: ATTACH cannot run inside a transaction
    # Store enables URI handling, so mode=rw forbids creation even if the
    # archive disappears after preflight or table preparation.
    target = archive_path.absolute().as_uri() + '?mode=rw' if require_existing else str(archive_path.absolute())
    conn.execute("ATTACH DATABASE ? AS arch", (target,))
    cols = _q(ev.columns)
    key = f'"{ev.row_key}"' if ev.row_key != "rowid" else "rowid"
    tbl = f'"{ev.table}"'
    remaining = max_rows
    protected_stream_ids = protected_stream_ids or set()

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
            # The daemon-owned assistant composite is one durable, pane-less
            # conversation.  Its open history is cursor-paged from the hot
            # table, so do not tail-move it to an archive the live fetch path
            # does not union.  Ordinary session retention is unchanged.
            if str(row[0] or "") in protected_stream_ids:
                continue
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
    # Managed chat uploads have a separate GC class. Generic/report/prompt
    # and legacy blobs remain excluded from deletion.
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
    managed_blobs_deleted: int = 0
    managed_uploads_reconciled: int = 0
    managed_uploads_promoted: int = 0
    history_pruned: int = 0
    receipts_moved: int = 0
    routes_moved: int = 0
    history_batches: list[dict[str, Any]] = field(default_factory=list)

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
        archive_path = cfg.archive_path or await self._submit(
            lambda conn: conn.history_archive_path or default_archive_path(db))
        # An explicitly configured archive is part of the owner inventory.
        # Never replace an unavailable archive with a new empty database, even
        # before GC: schedule/session mutations must also wait for recovery.
        if cfg.archive_path is not None:
            def check_archive(_conn):
                try:
                    existing = sqlite3.connect(Path(archive_path).absolute().as_uri() + '?mode=ro', uri=True)
                    try:
                        existing.execute('SELECT name FROM sqlite_master LIMIT 1').fetchall()
                    finally:
                        existing.close()
                except sqlite3.Error as exc:
                    raise RetentionError('configured archive unavailable; retention aborted') from exc
            await self._submit(check_archive)
        result.size_before = db_size(db)
        await self._submit(lambda conn: bind_history_archive(conn, archive_path))
        # Persist allocator seeding before any archive commit, then alternate the
        # two families so a receipt backlog never starves routes.
        def prepare_pair(conn):
            initialize_history_pair(conn)
            conn.commit()
            _attached_history(conn)
        await self._submit(prepare_pair)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for turn in range(HISTORY_PASS_ROWS // HISTORY_MAX_ROWS):
            table = HISTORY_TABLES[turn % 2]
            cursor_name = "_receipt_cursor" if table == RECEIPT_TABLE else "_route_cursor"
            cursor = getattr(self, cursor_name, 0 if table == RECEIPT_TABLE else "")
            batch = await self._submit(lambda conn, t=table, a=cursor: move_history_batch(
                conn, t, after=a, cutoff=cutoff, limit=min(HISTORY_MAX_ROWS, max(1, cfg.batch_size))))
            setattr(self, cursor_name, batch["cursor"])
            result.history_batches.append({"table": table, **batch})
            if table == RECEIPT_TABLE:
                result.receipts_moved += batch["moved"]
            else:
                result.routes_moved += batch["moved"]
            await asyncio.sleep(0)

        # Existing session/event archival attaches the same file for writes.
        # Release our read-only alias first; duplicate attachments to one SQLite
        # file would otherwise contend with each other at commit.
        def release_history_reader(conn):
            conn.commit()
            if conn.history_attachment_identity is not None:
                conn.execute("DETACH DATABASE receipt_archive")
                conn.history_attachment_identity = None
                conn.history_schema_version = None
        await self._submit(release_history_reader)

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
                conn, Schema(conn), archive_path, cfg.statuses, keep, cfg.batch_size, max_rows=limit,
                require_existing=True
            ),
        )
        result.sessions_moved = moved
        budget -= moved

        # Usage generation history (AC10) is never archived with its session:
        # a row is deleted only 30 days after its close, open rows never.
        from store_usage import prune_generation_history_conn
        result.history_pruned = await self._submit(
            lambda conn: prune_generation_history_conn(conn, time.time())
        )

        if wants_events and budget > 0:
            def _events(conn: sqlite3.Connection, limit: int) -> tuple[int, int]:
                # Rebuild the hot set each chunk: rows the sessions pass just
                # archived are no longer in `sessions`, and their events must
                # archive with them.
                schema = Schema(conn)
                _load_hot_table(conn, hot_stream_ids(conn, schema, cfg.statuses))
                protected = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT host || ':' || session_name FROM sessions "
                        "WHERE status='open' AND provider='composite'"
                    )
                }
                return archive_events(
                    conn, EventSchema(conn), archive_path, cfg.tail_keep,
                    cfg.batch_size, max_rows=limit, protected_stream_ids=protected,
                    require_existing=True,
                )

            ev_moved = await self._drain_pairs(budget, _events)
            result.events_terminal_moved, result.events_tail_moved = ev_moved
            budget -= result.events_moved

        if cfg.blob_root is not None:
            from attachment_retention import sweep_managed
            # Advance past pinned digests so bounded batches cannot starve later
            # orphans. Inspect archive owners only after this pass's migration.
            cursor = getattr(self, "_managed_attachment_cursor", "")
            counts = await self._submit(lambda conn: sweep_managed(
                conn, cfg.blob_root, archive_path=archive_path,
                limit=min(cfg.batch_size, 500), after_sha=cursor))
            self._managed_attachment_cursor = counts["next_cursor"]
            result.managed_blobs_deleted = counts["deleted"]
            result.managed_uploads_reconciled = counts["reconciled"]
            result.managed_uploads_promoted = counts["promoted"]

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
                if result.receipts_moved or result.routes_moved:
                    log.info("retention history: receipts=%d routes=%d examined=%d bytes=%d oversized=%d",
                             result.receipts_moved, result.routes_moved,
                             sum(b["examined"] for b in result.history_batches),
                             sum(b["materialized_bytes"] for b in result.history_batches),
                             sum(b["oversized"] for b in result.history_batches))
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
            # Preserve prompt exclusion before the last schedule owner is purged.
            if blob_ids and has_table(conn, "v2_attachment_uploads"):
                marks = ",".join("?" for _ in blob_ids)
                conn.execute(f"UPDATE v2_attachment_uploads SET legacy_protected=1 WHERE blob_sha IN ({marks})", blob_ids)
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
        """Live retention never rewrites the database or blocks on a truncation."""
        conn.commit()
        return False, "offline_maintenance_only", freelist_bytes(conn)


def history_recovery_main(argv=None) -> int:
    """Offline, identity-preserving backup/rehydration for an initialized pair."""
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Offline paired receipt/route recovery; never a rollback to old readers")
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("--hot", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="new directory for paired backup")
    parser.add_argument("--offline", action="store_true", help="confirm all writers and retention are stopped")
    args = parser.parse_args(argv)
    if not args.offline:
        parser.error("stop all writers and pass --offline; this is not a live backup utility")
    if args.operation == "backup" and args.output is None:
        parser.error("backup requires --output")
    conn = sqlite3.connect(args.hot.absolute().as_uri() + "?mode=rw", uri=True, factory=HistoryConnection)
    conn.row_factory = sqlite3.Row
    try:
        row = _sequence(conn)
        if row is None or not row["archive_ready"]:
            raise RetentionError("recovery requires an initialized, paired allocator")
        conn.history_archive_path = Path(row["archive_path"])
        conn.create_function("history_identity_conflict", 0, _history_identity_conflict)
        bind_history_archive(conn, args.archive)
        _attached_history(conn)
        result = {"operation": args.operation, "pair_id": _sequence(conn)["pair_id"]}
        if args.operation == "backup":
            args.output.mkdir(parents=True, exist_ok=False)
            for source, filename in ((args.hot, "hot.db"), (args.archive, "archive.db")):
                with sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True) as src:
                    with sqlite3.connect(str(args.output / filename)) as dest:
                        src.backup(dest)
                        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise RetentionError("backup integrity check failed")
            result["backup_directory"] = str(args.output)
        else:
            result["verified_rows"] = {}
            for table in HISTORY_TABLES:
                cursor = 0 if table == RECEIPT_TABLE else ""
                count = 0
                while True:
                    batch = restore_history_batch(conn, table=table, after=cursor)
                    cursor = batch["cursor"]
                    count += batch["restored"]
                    if batch["done"]:
                        break
                result["verified_rows"][table] = count
        result["high_water"] = _sequence(conn)["high_water"]
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(history_recovery_main())
