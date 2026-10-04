"""Managed upload provenance, executed exclusively on Store's SQLite worker.

No bearer secrets are persisted. A receipt's upload_id is a lookup identifier,
not authority to publish or fetch. Pending rows are not referenceable.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from attachment_locks import digest_lock

DDL = (
    """CREATE TABLE IF NOT EXISTS v2_attachment_uploads (
        upload_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL,
        auth_fingerprint TEXT NOT NULL,
        blob_sha TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0 AND size_bytes <= 26214400),
        media_type TEXT NOT NULL,
        filename TEXT NOT NULL,
        purpose TEXT NOT NULL CHECK(purpose = 'chat_attachment'),
        auth_kind TEXT NOT NULL CHECK(auth_kind IN ('seat','scoped','operator')),
        principal_id TEXT NOT NULL CHECK(length(principal_id) > 0),
        seat_stream_id TEXT,
        seat_generation TEXT,
        credential_id TEXT,
        assistant_scope TEXT,
        uploaded_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','ready')),
        legacy_protected INTEGER NOT NULL DEFAULT 0,
        UNIQUE(auth_fingerprint, request_id),
        CHECK((auth_kind = 'seat' AND seat_stream_id IS NOT NULL AND seat_generation IS NOT NULL)
           OR (auth_kind != 'seat' AND seat_stream_id IS NULL AND seat_generation IS NULL AND credential_id IS NOT NULL))
    )""",
    "CREATE INDEX IF NOT EXISTS v2_attachment_uploads_digest ON v2_attachment_uploads(blob_sha, state, uploaded_at)",
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def auth_fingerprint(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def receipt(row):
    return {"upload_id": row["upload_id"], "blob_sha": row["blob_sha"],
            "bytes": row["size_bytes"], "media_type": row["media_type"], "filename": row["filename"],
            "uploader": row["principal_id"], "generation": row["seat_generation"],
            "uploaded_at": row["uploaded_at"], "auth_kind": row["auth_kind"],
            "seat_stream_id": row["seat_stream_id"], "seat_generation": row["seat_generation"],
            "credential_id": row["credential_id"], "assistant_scope": row["assistant_scope"]}


class AttachmentStoreMixin:
    async def complete_attachment_upload(self, *, root, request_id, sha, size, media_type,
                                         filename, identity, materialize):
        """Two-phase add under one digest lock; callback materializes verified bytes."""
        fingerprint = auth_fingerprint(identity)
        def operation(conn):
            with digest_lock(root, sha):
                existing = conn.execute("SELECT * FROM v2_attachment_uploads WHERE auth_fingerprint=? AND request_id=?", (fingerprint, request_id)).fetchone()
                if existing and any(existing[k] != v for k, v in {"blob_sha": sha, "size_bytes": size, "media_type": media_type, "filename": filename}.items()):
                    raise ValueError("upload_request_conflict")
                stamp = now_iso()
                upload_id = existing["upload_id"] if existing else str(uuid.uuid4())
                if not existing:
                    prior = conn.execute("SELECT 1 FROM v2_attachment_uploads WHERE blob_sha=? LIMIT 1", (sha,)).fetchone()
                    legacy = int(not prior and (Path(root) / sha[:2] / sha).exists())
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute("""INSERT INTO v2_attachment_uploads
                            (upload_id,request_id,auth_fingerprint,blob_sha,size_bytes,media_type,filename,purpose,
                             auth_kind,principal_id,seat_stream_id,seat_generation,credential_id,assistant_scope,
                             uploaded_at,updated_at,state,legacy_protected)
                            VALUES (?,?,?,?,?,?,?,'chat_attachment',?,?,?,?,?,?,?,?,'pending',?)""",
                            (upload_id, request_id, fingerprint, sha, size, media_type, filename,
                             identity["auth_kind"], identity["principal_id"], identity["seat_stream_id"],
                             identity["seat_generation"], identity["credential_id"], identity["assistant_scope"],
                             stamp, stamp, legacy))
                        conn.commit()  # pending row is durable before the blob file exists
                    except BaseException:
                        conn.rollback()
                        raise
                materialize()  # same per-digest lock spans the DB row and filesystem
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("UPDATE v2_attachment_uploads SET state='ready', updated_at=? WHERE upload_id=?", (stamp, upload_id))
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                row = conn.execute("SELECT * FROM v2_attachment_uploads WHERE upload_id=?", (upload_id,)).fetchone()
                return receipt(row)
        return await self.submit(operation)

    async def attachment_upload(self, upload_id, *, ready_only=True):
        def operation(conn):
            row = conn.execute("SELECT * FROM v2_attachment_uploads WHERE upload_id=?", (upload_id,)).fetchone()
            if row is None or (ready_only and row["state"] != "ready"):
                return None
            return dict(row)
        return await self.submit(operation)

    async def attachment_blob_ready(self, sha):
        """Pending-only managed bytes are hidden; existing legacy blobs stay readable."""
        def operation(conn):
            rows = conn.execute("SELECT state,legacy_protected FROM v2_attachment_uploads WHERE blob_sha=?", (sha,)).fetchall()
            return not rows or any(row["state"] == "ready" or row["legacy_protected"] for row in rows)
        return await self.submit(operation)
