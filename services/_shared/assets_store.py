"""Durable store for session-scoped review assets."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .asset_schema import (
    report_block_text,
    report_has_block,
    normalize_content_type,
    normalize_tags,
    validate_asset_payload,
)


DEFAULT_DB_PATH = Path.home() / ".local/share/pentacle-stream/assets.db"
DB_PATH_ENV = "PENTACLE_STREAM_ASSETS_DB"
REVIEW_STATUSES = frozenset({"pending_review", "changes_requested", "approved"})
SCHEMA_VERSION = 1

ASSET_COLUMNS = (
    "host",
    "session_name",
    "stream_id",
    "asset_id",
    "title",
    "content_type",
    "body",
    "tags",
    "producer",
    "review_status",
    "read_at",
    "spec_id",
    "created_at",
    "updated_at",
)

COMMENT_COLUMNS = (
    "comment_id",
    "host",
    "session_name",
    "asset_id",
    "section_id",
    "block_id",
    "run_index",
    "excerpt",
    "body",
    "author",
    "created_at",
    "updated_at",
    "resolved",
    "resolved_by",
    "resolved_at",
    "resolution_note",
    "parent_comment_id",
)


class AssetStoreError(Exception):
    """Base for asset store errors."""


class AssetStoreUnhealthy(AssetStoreError):
    """Raised when ``PRAGMA integrity_check`` fails on store open."""


class AssetNotFound(AssetStoreError):
    """Raised when an asset lookup misses."""


class InvalidAsset(AssetStoreError):
    """Raised for invalid asset metadata."""


def _default_path() -> str:
    return os.environ.get(DB_PATH_ENV) or str(DEFAULT_DB_PATH)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_to_dict(row: sqlite3.Row) -> dict:
    record = {column: row[column] for column in ASSET_COLUMNS}
    tags_raw = record.get("tags")
    if tags_raw:
        decoded = json.loads(tags_raw)
        record["tags"] = decoded if isinstance(decoded, list) else []
    else:
        record["tags"] = []
    return record


def asset_metadata(record: dict) -> dict:
    return {
        "asset_id": record["asset_id"],
        "title": record["title"],
        "content_type": record["content_type"],
        "tags": list(record.get("tags") or []),
        "review_status": record.get("review_status") or "pending_review",
        "read": bool(record.get("read_at")),
        "read_at": record.get("read_at"),
        "spec_id": record.get("spec_id"),
        "updated_at": record["updated_at"],
    }


def _comment_row_to_dict(row: sqlite3.Row, *, asset_body: str | None = None) -> dict:
    record = {column: row[column] for column in COMMENT_COLUMNS}
    record["resolved"] = bool(record.get("resolved"))
    if asset_body is not None:
        record["anchored"] = report_has_block(
            asset_body, str(record.get("section_id") or ""), str(record.get("block_id") or "")
        )
    return record


class AssetStore:
    """SQLite-backed review asset store."""

    DEFAULT_DB_PATH = DEFAULT_DB_PATH

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = str(path or _default_path())
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            integrity_rows = self._conn.execute("PRAGMA integrity_check").fetchall()
            integrity = [str(row[0]) for row in integrity_rows]
            if integrity != ["ok"]:
                try:
                    self._conn.close()
                finally:
                    self._closed = True
                raise AssetStoreUnhealthy(
                    f"sqlite integrity_check failed on {self.path}: {integrity!r}"
                )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    host TEXT NOT NULL,
                    session_name TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    producer TEXT NOT NULL,
                    read_at TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (host, session_name, asset_id)
                )
                """
            )
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(assets)").fetchall()
            }
            for column, ddl in (
                ("stream_id", "ALTER TABLE assets ADD COLUMN stream_id TEXT"),
                ("tags", "ALTER TABLE assets ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"),
                ("producer", "ALTER TABLE assets ADD COLUMN producer TEXT NOT NULL DEFAULT ''"),
                (
                    "review_status",
                    "ALTER TABLE assets ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending_review'",
                ),
                ("read_at", "ALTER TABLE assets ADD COLUMN read_at TEXT"),
                ("spec_id", "ALTER TABLE assets ADD COLUMN spec_id TEXT"),
            ):
                if column not in columns:
                    self._conn.execute(ddl)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_assets_session_updated "
                "ON assets(host, session_name, updated_at DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_assets_spec_updated "
                "ON assets(spec_id, updated_at DESC)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS asset_comments (
                    comment_id TEXT NOT NULL PRIMARY KEY,
                    host TEXT NOT NULL,
                    session_name TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    section_id TEXT NOT NULL,
                    block_id TEXT NOT NULL,
                    run_index INTEGER,
                    excerpt TEXT NOT NULL,
                    body TEXT NOT NULL,
                    author TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    resolved_by TEXT,
                    resolved_at TEXT,
                    resolution_note TEXT,
                    parent_comment_id TEXT
                )
                """
            )
            comment_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(asset_comments)").fetchall()
            }
            if "resolution_note" not in comment_columns:
                self._conn.execute("ALTER TABLE asset_comments ADD COLUMN resolution_note TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_asset_comments_asset "
                "ON asset_comments(host, session_name, asset_id, resolved, created_at)"
            )
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()

    def identity(self) -> dict[str, int | str | None]:
        """Return non-sensitive durable-store identity for split-brain checks."""
        if self.path == ":memory:":
            return {"path": self.path, "device": None, "inode": None, "schema_version": SCHEMA_VERSION}
        path = Path(self.path).expanduser().resolve(strict=False)
        stat = path.stat()
        return {
            "path": str(path),
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "schema_version": SCHEMA_VERSION,
        }

    def publish_asset(
        self,
        *,
        host: str,
        session_name: str,
        stream_id: str,
        title: str,
        content_type: str,
        body: str,
        tags: list[str] | None = None,
        producer: str | None = None,
        spec_id: str | None = None,
        asset_id: str | None = None,
        now: str | None = None,
    ) -> dict:
        host = str(host or "").strip()
        session_name = str(session_name or "").strip()
        stream_id = str(stream_id or "").strip()
        title = str(title or "").strip()
        producer = str(producer or stream_id or "asset.publish").strip()
        spec_id = str(spec_id or "").strip() or None
        if not host:
            raise InvalidAsset("host must be non-empty")
        if not session_name:
            raise InvalidAsset("session_name must be non-empty")
        if not stream_id:
            raise InvalidAsset("stream_id must be non-empty")
        if not title:
            raise InvalidAsset("title must be non-empty")
        content_type = normalize_content_type(content_type)
        body = validate_asset_payload(content_type, body)
        normalized_tags = normalize_tags(tags)
        aid = str(asset_id or uuid.uuid4()).strip()
        if not aid:
            raise InvalidAsset("asset_id must be non-empty")
        ts = now or _iso_now()
        tags_json = json.dumps(normalized_tags, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._require_open()
            if spec_id:
                existing = self._conn.execute(
                    """
                    SELECT host, session_name, created_at FROM assets
                    WHERE spec_id = ? AND asset_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (spec_id, aid),
                ).fetchone()
                if existing is not None:
                    host = existing["host"]
                    session_name = existing["session_name"]
            else:
                existing = self._conn.execute(
                    """
                    SELECT host, session_name, created_at FROM assets
                    WHERE host = ? AND session_name = ? AND asset_id = ?
                    """,
                    (host, session_name, aid),
                ).fetchone()
            created_at = existing["created_at"] if existing is not None else ts
            self._conn.execute(
                """
                INSERT INTO assets (
                    host, session_name, stream_id, asset_id, title, content_type,
                    body, tags, producer, review_status, read_at, spec_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host, session_name, asset_id) DO UPDATE SET
                    title = excluded.title,
                    content_type = excluded.content_type,
                    body = excluded.body,
                    tags = excluded.tags,
                    review_status = excluded.review_status,
                    read_at = excluded.read_at,
                    updated_at = excluded.updated_at
                """,
                (
                    host,
                    session_name,
                    stream_id,
                    aid,
                    title,
                    content_type,
                    body,
                    tags_json,
                    producer,
                    "pending_review",
                    None,
                    spec_id,
                    created_at,
                    ts,
                ),
            )
            # Feedback model: re-publishing an asset does not retain prior comments.
            # A revision is a fresh document; the operator re-comments if needed.
            if existing is not None:
                self._conn.execute(
                    "DELETE FROM asset_comments WHERE host = ? AND session_name = ? AND asset_id = ?",
                    (host, session_name, aid),
                )
            self._conn.commit()
            return self._get_locked(host, session_name, aid)

    def list_assets(
        self,
        *,
        host: str,
        session_name: str,
        limit: int | None = None,
    ) -> list[dict]:
        if limit is not None and int(limit) < 0:
            raise ValueError(f"limit must be >= 0 or None; got {limit}")
        cols = ", ".join(ASSET_COLUMNS)
        sql = (
            f"SELECT {cols} FROM assets "
            "WHERE host = ? AND session_name = ? "
            "ORDER BY updated_at DESC, asset_id DESC"
        )
        params: list[Any] = [host, session_name]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            self._require_open()
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_by_spec_id(self, spec_id: str, *, limit: int | None = None) -> list[dict]:
        spec_id = str(spec_id or "").strip()
        if not spec_id:
            raise InvalidAsset("spec_id must be non-empty")
        if limit is not None and int(limit) < 0:
            raise ValueError(f"limit must be >= 0 or None; got {limit}")
        cols = ", ".join(ASSET_COLUMNS)
        sql = (
            f"SELECT {cols} FROM assets "
            "WHERE spec_id = ? "
            "ORDER BY updated_at DESC, asset_id DESC"
        )
        params: list[Any] = [spec_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            self._require_open()
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_all_assets(self, *, limit: int | None = None) -> list[dict]:
        if limit is not None and int(limit) < 0:
            raise ValueError(f"limit must be >= 0 or None; got {limit}")
        cols = ", ".join(ASSET_COLUMNS)
        sql = f"SELECT {cols} FROM assets ORDER BY updated_at DESC, asset_id DESC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self._lock:
            self._require_open()
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def find_assets_by_id(self, asset_id: str) -> list[dict]:
        cols = ", ".join(ASSET_COLUMNS)
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                f"SELECT {cols} FROM assets WHERE asset_id = ? ORDER BY updated_at DESC",
                (str(asset_id or ""),),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_for_session_and_specs(
        self,
        *,
        host: str,
        session_name: str,
        spec_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        records = self.list_assets(host=host, session_name=session_name, limit=None)
        seen = {(record["host"], record["session_name"], record["asset_id"]) for record in records}
        for spec_id in spec_ids or []:
            for record in self.list_by_spec_id(spec_id):
                key = (record["host"], record["session_name"], record["asset_id"])
                if key not in seen:
                    records.append(record)
                    seen.add(key)
        records.sort(key=lambda item: (item.get("updated_at") or "", item.get("asset_id") or ""), reverse=True)
        if limit is not None:
            records = records[: int(limit)]
        return records

    def get_asset(self, *, host: str, session_name: str, asset_id: str) -> dict:
        with self._lock:
            self._require_open()
            record = self._get_locked(host, session_name, asset_id)
        if record is None:
            raise AssetNotFound(asset_id)
        return record

    def get_asset_for_session_and_specs(
        self,
        *,
        host: str,
        session_name: str,
        asset_id: str,
        spec_ids: list[str] | None = None,
    ) -> dict:
        with self._lock:
            self._require_open()
            record = self._get_locked(host, session_name, asset_id)
            if record is None:
                record = self._get_by_asset_id_and_specs_locked(asset_id, spec_ids or [])
        if record is None:
            raise AssetNotFound(asset_id)
        return record

    def set_review_status(
        self,
        *,
        host: str,
        session_name: str,
        asset_id: str,
        review_status: str,
        now: str | None = None,
    ) -> dict:
        if review_status not in REVIEW_STATUSES:
            raise InvalidAsset("review_status must be one of: " + ", ".join(sorted(REVIEW_STATUSES)))
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            record = self._get_locked(host, session_name, asset_id)
            if record is None:
                raise AssetNotFound(asset_id)
            self._conn.execute(
                """
                UPDATE assets
                SET review_status = ?, updated_at = ?
                WHERE host = ? AND session_name = ? AND asset_id = ?
                """,
                (review_status, ts, host, session_name, asset_id),
            )
            self._conn.commit()
            return self._get_locked(host, session_name, asset_id)

    def set_read(
        self,
        *,
        host: str,
        session_name: str,
        asset_id: str,
        read: bool,
        now: str | None = None,
    ) -> dict:
        if not isinstance(read, bool):
            raise InvalidAsset("read must be a boolean")
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            record = self._get_locked(host, session_name, asset_id)
            if record is None:
                raise AssetNotFound(asset_id)
            self._conn.execute(
                """
                UPDATE assets
                SET read_at = ?, updated_at = ?
                WHERE host = ? AND session_name = ? AND asset_id = ?
                """,
                (ts if read else None, ts, host, session_name, asset_id),
            )
            self._conn.commit()
            return self._get_locked(host, session_name, asset_id)

    def add_comment(
        self,
        *,
        host: str,
        session_name: str,
        asset_id: str,
        section_id: str,
        block_id: str,
        body: str,
        author: str,
        run_index: int | None = None,
        excerpt: str | None = None,
        parent_comment_id: str | None = None,
        comment_id: str | None = None,
        now: str | None = None,
    ) -> dict:
        section_id = str(section_id or "").strip()
        block_id = str(block_id or "").strip()
        body = str(body or "").strip()
        author = str(author or "").strip()
        if not section_id:
            raise InvalidAsset("section_id must be non-empty")
        if not block_id:
            raise InvalidAsset("block_id must be non-empty")
        if not body:
            raise InvalidAsset("comment body must be non-empty")
        if not author:
            raise InvalidAsset("comment author must be non-empty")
        if run_index is not None and int(run_index) < 0:
            raise InvalidAsset("run_index must be >= 0 when present")
        cid = str(comment_id or uuid.uuid4()).strip()
        if not cid:
            raise InvalidAsset("comment_id must be non-empty")
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            asset = self._get_locked(host, session_name, asset_id)
            if asset is None:
                raise AssetNotFound(asset_id)
            snapshot = excerpt
            if snapshot is None and asset.get("content_type") == "report":
                snapshot = report_block_text(asset.get("body") or "", section_id, block_id)
            snapshot = str(snapshot or "").strip()
            if not snapshot:
                raise InvalidAsset("excerpt must be non-empty for an unknown block")
            self._conn.execute(
                """
                INSERT INTO asset_comments (
                    comment_id, host, session_name, asset_id, section_id, block_id,
                    run_index, excerpt, body, author, created_at, updated_at,
                    resolved, resolved_by, resolved_at, parent_comment_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?)
                """,
                (
                    cid,
                    host,
                    session_name,
                    asset_id,
                    section_id,
                    block_id,
                    int(run_index) if run_index is not None else None,
                    snapshot,
                    body,
                    author,
                    ts,
                    ts,
                    parent_comment_id,
                ),
            )
            self._conn.commit()
            row = self._get_comment_locked(cid)
        return _comment_row_to_dict(row, asset_body=asset.get("body") if asset else None)

    def edit_comment(
        self,
        *,
        comment_id: str,
        body: str,
        host: str | None = None,
        session_name: str | None = None,
        asset_id: str | None = None,
        now: str | None = None,
    ) -> dict:
        body = str(body or "").strip()
        if not body:
            raise InvalidAsset("comment body must be non-empty")
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            row = self._get_comment_locked(comment_id)
            if row is None:
                raise AssetNotFound(comment_id)
            self._require_comment_scope(row, host, session_name, asset_id)
            self._conn.execute(
                "UPDATE asset_comments SET body = ?, updated_at = ? WHERE comment_id = ?",
                (body, ts, comment_id),
            )
            self._conn.commit()
            row = self._get_comment_locked(comment_id)
            asset = self._get_locked(row["host"], row["session_name"], row["asset_id"])
        return _comment_row_to_dict(row, asset_body=asset.get("body") if asset else None)

    def delete_comment(
        self,
        *,
        comment_id: str,
        host: str | None = None,
        session_name: str | None = None,
        asset_id: str | None = None,
    ) -> dict:
        with self._lock:
            self._require_open()
            row = self._get_comment_locked(comment_id)
            if row is None:
                raise AssetNotFound(comment_id)
            self._require_comment_scope(row, host, session_name, asset_id)
            self._conn.execute("DELETE FROM asset_comments WHERE comment_id = ?", (comment_id,))
            self._conn.commit()
        return _comment_row_to_dict(row)

    def delete_asset(self, *, host: str, session_name: str, asset_id: str) -> dict:
        """Delete an asset and all of its comments. Returns the deleted record."""
        with self._lock:
            self._require_open()
            record = self._get_locked(host, session_name, asset_id)
            if record is None:
                raise AssetNotFound(asset_id)
            self._conn.execute(
                "DELETE FROM asset_comments WHERE host = ? AND session_name = ? AND asset_id = ?",
                (host, session_name, asset_id),
            )
            self._conn.execute(
                "DELETE FROM assets WHERE host = ? AND session_name = ? AND asset_id = ?",
                (host, session_name, asset_id),
            )
            self._conn.commit()
        return record

    def resolve_comment(
        self,
        *,
        comment_id: str,
        resolved: bool = True,
        resolved_by: str | None = None,
        resolution_note: str | None = None,
        host: str | None = None,
        session_name: str | None = None,
        asset_id: str | None = None,
        now: str | None = None,
    ) -> dict:
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            row = self._get_comment_locked(comment_id)
            if row is None:
                raise AssetNotFound(comment_id)
            self._require_comment_scope(row, host, session_name, asset_id)
            self._conn.execute(
                """
                UPDATE asset_comments
                SET resolved = ?, resolved_by = ?, resolved_at = ?, resolution_note = ?, updated_at = ?
                WHERE comment_id = ?
                """,
                (
                    1 if resolved else 0,
                    str(resolved_by or "").strip() or None if resolved else None,
                    ts if resolved else None,
                    str(resolution_note or "").strip() or None if resolved else None,
                    ts,
                    comment_id,
                ),
            )
            self._conn.commit()
            row = self._get_comment_locked(comment_id)
            asset = self._get_locked(row["host"], row["session_name"], row["asset_id"])
        return _comment_row_to_dict(row, asset_body=asset.get("body") if asset else None)

    def list_comments(
        self,
        *,
        host: str,
        session_name: str,
        asset_id: str,
        unresolved_only: bool = False,
    ) -> list[dict]:
        with self._lock:
            self._require_open()
            asset = self._get_locked(host, session_name, asset_id)
            if asset is None:
                raise AssetNotFound(asset_id)
            sql = (
                "SELECT "
                + ", ".join(COMMENT_COLUMNS)
                + " FROM asset_comments WHERE host = ? AND session_name = ? AND asset_id = ?"
            )
            params: list[Any] = [host, session_name, asset_id]
            if unresolved_only:
                sql += " AND resolved = 0"
            sql += " ORDER BY created_at ASC, comment_id ASC"
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_comment_row_to_dict(row, asset_body=asset.get("body")) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("asset store is closed")

    def _get_locked(self, host: str, session_name: str, asset_id: str) -> dict | None:
        cols = ", ".join(ASSET_COLUMNS)
        row = self._conn.execute(
            f"SELECT {cols} FROM assets WHERE host = ? AND session_name = ? AND asset_id = ?",
            (host, session_name, asset_id),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def _get_by_asset_id_and_specs_locked(
        self, asset_id: str, spec_ids: list[str]
    ) -> dict | None:
        cleaned = [str(spec_id).strip() for spec_id in spec_ids if str(spec_id).strip()]
        if not cleaned:
            return None
        cols = ", ".join(ASSET_COLUMNS)
        placeholders = ", ".join("?" for _ in cleaned)
        row = self._conn.execute(
            f"""
            SELECT {cols} FROM assets
            WHERE asset_id = ? AND spec_id IN ({placeholders})
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (asset_id, *cleaned),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def _get_comment_locked(self, comment_id: str) -> sqlite3.Row | None:
        cols = ", ".join(COMMENT_COLUMNS)
        return self._conn.execute(
            f"SELECT {cols} FROM asset_comments WHERE comment_id = ?",
            (comment_id,),
        ).fetchone()

    def _require_comment_scope(
        self,
        row: sqlite3.Row,
        host: str | None,
        session_name: str | None,
        asset_id: str | None,
    ) -> None:
        if host is not None and row["host"] != host:
            raise AssetNotFound(row["comment_id"])
        if session_name is not None and row["session_name"] != session_name:
            raise AssetNotFound(row["comment_id"])
        if asset_id is not None and row["asset_id"] != asset_id:
            raise AssetNotFound(row["comment_id"])


def open_store(path: str | os.PathLike[str] | None = None) -> AssetStore:
    return AssetStore(path)
