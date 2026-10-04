"""Managed upload provenance, executed exclusively on Store's SQLite worker.

No bearer secrets are persisted. A receipt's upload_id is a lookup identifier,
not authority to publish or fetch. Pending rows are not referenceable.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat
from pathlib import Path
import uuid

from attachment_locks import digest_lock
from chat_attachment_types import ATTACHMENT_MAX_BYTES, validated_media_type, sanitized_filename

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
    """CREATE TABLE IF NOT EXISTS v2_attachment_refs (
        owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL,
        stream_id TEXT NOT NULL, upload_id TEXT NOT NULL, blob_sha TEXT NOT NULL,
        PRIMARY KEY(owner_kind,owner_id,upload_id)
    )""",
    "CREATE INDEX IF NOT EXISTS v2_attachment_refs_digest ON v2_attachment_refs(blob_sha)",
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


def publication_attachment(row, stream_id, publisher=None):
    """Only persisted, ready, correctly scoped provenance can become an event."""
    if (not row or row['state'] != 'ready' or row['purpose'] != 'chat_attachment'
            or not re.fullmatch(r'[0-9a-f]{64}', row['blob_sha'])
            or not 0 < row['size_bytes'] <= ATTACHMENT_MAX_BYTES
            or (row['auth_kind'] == 'scoped' and row['assistant_scope'] != stream_id)):
        raise ValueError('assistant_publish_attachment_unverified')
    result = dict(key=row['blob_sha'], mime=row['media_type'], size=row['size_bytes'],
        filename=row['filename'], upload_id=row['upload_id'], uploaded_at=row['uploaded_at'],
        uploader=dict(auth_kind=row['auth_kind'], principal_id=row['principal_id'],
            stream_id=row['seat_stream_id'], generation=row['seat_generation'],
            credential_id=row['credential_id'], assistant_scope=row['assistant_scope']))
    if publisher is not None:
        result['publisher'] = publisher
    return result


def verify_publication_bytes(root, row):
    """Final bounded byte verification runs on Store worker inside digest lock."""
    sha = row['blob_sha']
    path = Path(root) / sha[:2] / sha
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != row['size_bytes']:
                raise ValueError('assistant_publish_attachment_unverified')
            hasher, size, prefix = hashlib.sha256(), 0, b''
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > ATTACHMENT_MAX_BYTES:
                    raise ValueError('assistant_publish_attachment_unverified')
                hasher.update(chunk)
                if not prefix: prefix = chunk[:16]
            after = os.fstat(handle.fileno())
            if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or size != row['size_bytes'] or hasher.hexdigest() != sha
                    or sanitized_filename(row['filename']) != row['filename']
                    or validated_media_type(row['filename'], prefix) != row['media_type']):
                raise ValueError('assistant_publish_attachment_unverified')
    except (OSError, ValueError) as exc:
        raise ValueError('assistant_publish_attachment_unverified') from exc


class AttachmentStoreMixin:
    def configure_attachment_root(self, root):
        root = Path(root).resolve()
        previous = getattr(self, '_attachment_root', None)
        if previous is not None and previous != root:
            raise ValueError('attachment_root_conflict')
        self._attachment_root = root

    @contextmanager
    def publication_attachment_guard(self, conn, upload_ids, stream_id, expected, publisher):
        if not upload_ids:
            yield
            return
        root = getattr(self, '_attachment_root', None)
        if root is None:
            raise ValueError('assistant_publish_attachment_validation_unavailable')
        rows = [conn.execute('SELECT * FROM v2_attachment_uploads WHERE upload_id=?', (key,)).fetchone() for key in upload_ids]
        if any(row is None for row in rows):
            raise ValueError('assistant_publish_attachment_unverified')
        with ExitStack() as stack:
            for sha in sorted({row['blob_sha'] for row in rows}):
                stack.enter_context(digest_lock(root, sha))
            # Recheck after locking. GC or another process may have changed rows
            # while the lock was acquired. Never publish from a stale lookup.
            current = [conn.execute('SELECT * FROM v2_attachment_uploads WHERE upload_id=?', (key,)).fetchone() for key in upload_ids]
            for original, row in zip(rows, current):
                if row is None or dict(original) != dict(row):
                    raise ValueError('assistant_publish_attachment_unverified')
            actual = [publication_attachment(row, stream_id, publisher) for row in current]
            if actual != expected:
                raise ValueError('assistant_publish_attachment_unverified')
            for row in current:
                verify_publication_bytes(root, row)
            yield  # Held through BEGIN IMMEDIATE, event/reference insert, commit.

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
