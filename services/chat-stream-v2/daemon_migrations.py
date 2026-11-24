"""Explicit, resumable database preparation metadata for candidate releases.

Preparation is an operator action on a disposable copy. The serving path remains
unchanged: it still opens databases using Store's established, idempotent schema
helpers. This module records a versioned authoritative marker only after those
helpers and the paired-history archive have completed and been verified.
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Callable

from retention import (
    HISTORY_SEQUENCE_DDL, HistoryConnection, _archive_schema, _open_archive, _sequence,
    default_archive_path, initialize_history_pair,
)
from store import SCHEMA_VERSION, Store

REGISTRY = "daemon_migration_registry"
PREPARE_STEP_ID = "prepare-origin-v1"
MAIN_STEP_ID = "store-layout-v2"
PAIR_STEP_ID = "history-pair-v1"
RUNNER_VERSION = 1
REGISTRY_DDL = f'''CREATE TABLE IF NOT EXISTS {REGISTRY} (
    step_id TEXT PRIMARY KEY,
    runner_version INTEGER NOT NULL,
    store_schema_version INTEGER NOT NULL,
    pair_id TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)'''


class MigrationError(RuntimeError):
    pass


def _readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise MigrationError(f"database does not exist: {path}")
    conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


def inspect(database: str | Path) -> dict:
    """Read only schema/registry metadata; never scans application rows."""
    db = Path(database).expanduser().absolute()
    try:
        conn = _readonly(db)
    except (MigrationError, OSError, sqlite3.Error):
        return {"prepared": False, "reason": "main database unavailable"}
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if REGISTRY not in tables or "v2_send_receipt_sequence" not in tables:
            return {"prepared": False, "reason": "preparation metadata missing"}
        markers = {
            row["step_id"]: row for row in conn.execute(
            f"SELECT step_id,runner_version,store_schema_version,pair_id,archive_path "
            f"FROM {REGISTRY} WHERE step_id IN (?,?)", (MAIN_STEP_ID, PAIR_STEP_ID)
            )
        }
        main_marker = markers.get(MAIN_STEP_ID)
        marker = markers.get(PAIR_STEP_ID)
        sequence = conn.execute(
            "SELECT pair_id,archive_path,archive_ready FROM v2_send_receipt_sequence WHERE singleton=1"
        ).fetchone()
        store_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if main_marker is None or marker is None or sequence is None:
            return {"prepared": False, "reason": "authoritative preparation marker missing"}
        archive_path = Path(sequence["archive_path"]) if sequence["archive_path"] else None
        if (not main_marker["runner_version"] == RUNNER_VERSION
                or not main_marker["store_schema_version"] == SCHEMA_VERSION
                or not marker["runner_version"] == RUNNER_VERSION
                or not marker["store_schema_version"] == SCHEMA_VERSION
                or store_version < SCHEMA_VERSION
                or not sequence["archive_ready"]
                or main_marker["pair_id"] != sequence["pair_id"]
                or main_marker["archive_path"] != str(archive_path or "")
                or marker["pair_id"] != sequence["pair_id"]
                or marker["archive_path"] != str(archive_path or "")):
            return {"prepared": False, "reason": "main marker and pair metadata disagree"}
        if archive_path is None or not archive_path.is_file():
            return {"prepared": False, "reason": "archive metadata unavailable"}
        try:
            archive = _readonly(archive_path)
            try:
                _archive_schema(conn, archive, initialize=False, pair_id=marker["pair_id"])
            finally:
                archive.close()
        except (MigrationError, OSError, sqlite3.Error):
            return {"prepared": False, "reason": "archive marker or schema mismatch"}
    finally:
        conn.close()
    return {
        "prepared": True, "step_id": PAIR_STEP_ID, "runner_version": RUNNER_VERSION,
        "store_schema_version": SCHEMA_VERSION, "pair_id": marker["pair_id"],
        "archive_path": str(archive_path),
    }


def _commit_authoritative_marker(conn: sqlite3.Connection) -> None:
    row = _sequence(conn)
    if row is None or not row["archive_ready"]:
        raise MigrationError("archive pair is not ready")
    path = Path(row["archive_path"])
    archive = _open_archive(path, readonly=True)
    try:
        _archive_schema(conn, archive, initialize=False, pair_id=row["pair_id"])
    finally:
        archive.close()
    conn.execute(REGISTRY_DDL)
    conn.execute(
        f"INSERT INTO {REGISTRY}(step_id,runner_version,store_schema_version,pair_id,archive_path) "
        "VALUES(?,?,?,?,?) ON CONFLICT(step_id) DO UPDATE SET "
        "runner_version=excluded.runner_version,store_schema_version=excluded.store_schema_version,"
        "pair_id=excluded.pair_id,archive_path=excluded.archive_path,"
        "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (PAIR_STEP_ID, RUNNER_VERSION, SCHEMA_VERSION, row["pair_id"], str(path)),
    )
    conn.commit()


def _ensure_prepare_origin(db: Path) -> None:
    """Persist whether Store saw a fresh pair before its first schema commit."""
    conn = sqlite3.connect(db, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(REGISTRY_DDL)
        recorded = conn.execute(
            f"SELECT 1 FROM {REGISTRY} WHERE step_id=?", (PREPARE_STEP_ID,)
        ).fetchone()
        if recorded:
            return
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        pair_tables = {
            "sessions", "session_event_tail", "v2_send_receipts",
            "v2_assistant_composite_routes", "v2_send_receipt_sequence",
        }
        fresh = not (tables & pair_tables)
        pair_id = archive_path = ""
        if fresh:
            archive = default_archive_path(db)
            conn.execute(HISTORY_SEQUENCE_DDL)
            existing = conn.execute(
                "SELECT pair_id,archive_path FROM v2_send_receipt_sequence WHERE singleton=1"
            ).fetchone()
            if existing is None:
                pair_id = uuid.uuid4().hex
                archive_path = str(archive)
                conn.execute(
                    "INSERT INTO v2_send_receipt_sequence VALUES(1,0,?,?,0)",
                    (pair_id, archive_path),
                )
            else:
                pair_id, archive_path = existing[0], existing[1]
        conn.execute(
            f"INSERT INTO {REGISTRY}(step_id,runner_version,store_schema_version,pair_id,archive_path) "
            "VALUES(?,?,?,?,?)",
            (PREPARE_STEP_ID, RUNNER_VERSION, 0, pair_id, archive_path),
        )
        conn.commit()
    finally:
        conn.close()


def _has_main_step(db: Path) -> bool:
    try:
        conn = _readonly(db)
    except (MigrationError, OSError, sqlite3.Error):
        return False
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (REGISTRY,)
        ).fetchone()
        if not exists:
            return False
        row = conn.execute(
            f"SELECT runner_version,store_schema_version FROM {REGISTRY} WHERE step_id=?",
            (MAIN_STEP_ID,),
        ).fetchone()
        return bool(row and row["runner_version"] == RUNNER_VERSION
                    and row["store_schema_version"] == SCHEMA_VERSION)
    finally:
        conn.close()


def _main_schema_commit_present(db: Path) -> bool:
    """Recognize Store's completed schema transaction if its receipt was lost.

    Store writes ``user_version`` and the final set of schema columns in the
    same transaction. Checking those sentinels lets prepare record the missing
    step receipt without running Store.start a second time after a crash in
    the receipt gap.
    """
    try:
        conn = _readonly(db)
    except (MigrationError, OSError, sqlite3.Error):
        return False
    try:
        if int(conn.execute("PRAGMA user_version").fetchone()[0]) < SCHEMA_VERSION:
            return False
        required = {
            "sessions": {"observer_binding", "account_binding", "assigned_spec_id"},
            "v2_reports": {"usage_snapshot"},
            "v2_schedules": {"objective_source", "no_watch"},
        }
        for table, columns in required.items():
            present = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if not columns <= present:
                return False
        return True
    except (sqlite3.Error, TypeError, ValueError):
        return False
    finally:
        conn.close()


def _record_main_step(db: Path) -> None:
    conn = sqlite3.connect(db, timeout=5.0)
    try:
        conn.execute(REGISTRY_DDL)
        sequence = conn.execute(
            "SELECT pair_id,archive_path FROM v2_send_receipt_sequence WHERE singleton=1"
        ).fetchone()
        conn.execute(
            f"INSERT INTO {REGISTRY}(step_id,runner_version,store_schema_version,pair_id,archive_path) "
            "VALUES(?,?,?,?,?) ON CONFLICT(step_id) DO UPDATE SET "
            "runner_version=excluded.runner_version,store_schema_version=excluded.store_schema_version,"
            "pair_id=excluded.pair_id,archive_path=excluded.archive_path,"
            "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (MAIN_STEP_ID, RUNNER_VERSION, SCHEMA_VERSION,
             sequence[0] if sequence else "", sequence[1] if sequence else ""),
        )
        conn.commit()
    finally:
        conn.close()


def prepare(database: str | Path, *, before_main_step_record: Callable[[], None] | None = None,
            after_main_commit: Callable[[], None] | None = None,
            after_archive_commit: Callable[[], None] | None = None) -> dict:
    """Converge a disposable DB using Store helpers, then seal its pair marker.

    Store's schema transaction and the main-step receipt are separate commits.
    If a crash falls between them, the schema version and final-column sentinels
    identify the completed transaction so resume records its receipt without
    running Store.start again. Archive initialization then commits
    independently, and the final pair receipt follows metadata verification.
    """
    db = Path(database).expanduser().absolute()
    db.parent.mkdir(parents=True, exist_ok=True)
    if inspect(db).get("prepared"):
        return inspect(db)
    _ensure_prepare_origin(db)
    if not _has_main_step(db):
        if not _main_schema_commit_present(db):
            store = Store(str(db))
            store.start()
            if not store.stop(timeout=10):
                raise MigrationError("Store worker did not stop after main preparation")
            if before_main_step_record is not None:
                before_main_step_record()
        _record_main_step(db)
        if after_main_commit is not None:
            after_main_commit()

    conn = sqlite3.connect(str(db), timeout=5.0, factory=HistoryConnection)
    conn.row_factory = sqlite3.Row
    row = _sequence(conn)
    conn.history_archive_path = (
        Path(row["archive_path"]) if row and row["archive_path"]
        else default_archive_path(db)
    )
    try:
        if row and row["archive_ready"]:
            pass
        else:
            initialize_history_pair(conn)
            # The archive commit is complete; the main marker is still pending.
            if after_archive_commit is not None:
                after_archive_commit()
            conn.commit()
        _commit_authoritative_marker(conn)
    finally:
        conn.close()
    result = inspect(db)
    if not result["prepared"]:
        raise MigrationError(result["reason"])
    return result
