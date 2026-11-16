"""Managed upload provenance with worker-owned filesystem preparation.

No bearer secrets are persisted. A receipt's upload_id is a lookup identifier,
not authority to publish or fetch. Pending rows are not referenceable.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat
from pathlib import Path
import uuid

from attachment_locks import attachment_io, check_deadline, settle_future, with_digest_locks
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


def verify_publication_bytes(root, row, *, directory_fd=None):
    """Bounded byte verification; caller owns the digest lock outside Store."""
    sha = row['blob_sha']
    path = Path(root) / sha[:2] / sha
    try:
        fd = os.open(path if directory_fd is None else sha,
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != row['size_bytes']:
                raise ValueError('assistant_publish_attachment_unverified')
            hasher, size, prefix = hashlib.sha256(), 0, b''
            while chunk := handle.read(min(1024 * 1024, row['size_bytes'] + 1 - size)):
                size += len(chunk)
                if size > row['size_bytes'] or size > ATTACHMENT_MAX_BYTES:
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

    async def _attachment_submit(self, operation):
        return await self.submit(operation, settle_on_cancel=True)

    @staticmethod
    def _publication_rows(conn, upload_ids):
        rows = [conn.execute('SELECT * FROM v2_attachment_uploads WHERE upload_id=?', (key,)).fetchone() for key in upload_ids]
        if any(row is None for row in rows):
            raise ValueError('assistant_publish_attachment_unverified')
        return [dict(row) for row in rows]

    @contextmanager
    def publication_attachment_guard(self, conn, upload_ids, stream_id, expected, publisher, *, prepared=None):
        """Final provenance fence only; the async owner retains verified locks."""
        if upload_ids:
            current = self._publication_rows(conn, upload_ids)
            if prepared is None or current != prepared:
                raise ValueError('assistant_publish_attachment_unverified')
            if [publication_attachment(row, stream_id, publisher) for row in current] != expected:
                raise ValueError('assistant_publish_attachment_unverified')
        yield

    async def commit_attachment_publication(self, upload_ids, stream_id, expected, publisher, operation):
        if not upload_ids:
            return await self.submit(operation)
        if len(upload_ids) > 16:
            raise ValueError('assistant_publish_attachment_unverified')
        root = getattr(self, '_attachment_root', None)
        if root is None:
            raise ValueError('assistant_publish_attachment_validation_unavailable')
        original = await self._attachment_submit(lambda conn: self._publication_rows(conn, upload_ids))

        async def locked(deadline):
            current = await self._attachment_submit(lambda conn: self._publication_rows(conn, upload_ids))
            if current != original or [publication_attachment(row, stream_id, publisher) for row in current] != expected:
                raise ValueError('assistant_publish_attachment_unverified')
            for row in current:
                await attachment_io(verify_publication_bytes, root, row)
            check_deadline(deadline)
            def commit(conn):
                with self.publication_attachment_guard(conn, upload_ids, stream_id, expected, publisher, prepared=current):
                    return operation(conn)
            return await self._attachment_submit(commit)

        return await with_digest_locks(self, root, [row['blob_sha'] for row in original], locked)

    async def complete_attachment_upload(self, *, root, request_id, sha, size, media_type,
                                         filename, identity, materialize):
        """Durable pending -> worker materialize -> ready under one owned lock."""
        fingerprint = auth_fingerprint(identity)
        async def locked(deadline):
            exists = await attachment_io((Path(root) / sha[:2] / sha).exists)
            check_deadline(deadline)
            def prepare(conn):
                existing = conn.execute("SELECT * FROM v2_attachment_uploads WHERE auth_fingerprint=? AND request_id=?", (fingerprint, request_id)).fetchone()
                if existing and any(existing[k] != v for k, v in {"blob_sha": sha, "size_bytes": size, "media_type": media_type, "filename": filename}.items()):
                    raise ValueError("upload_request_conflict")
                if existing:
                    return dict(existing)
                stamp = now_iso()
                upload_id = str(uuid.uuid4())
                prior = conn.execute("SELECT 1 FROM v2_attachment_uploads WHERE blob_sha=? LIMIT 1", (sha,)).fetchone()
                legacy = int(not prior and exists)
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
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                return dict(conn.execute("SELECT * FROM v2_attachment_uploads WHERE upload_id=?", (upload_id,)).fetchone())

            prepared = await self._attachment_submit(prepare)
            await attachment_io(materialize)
            check_deadline(deadline)
            def ready(conn):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute("SELECT * FROM v2_attachment_uploads WHERE upload_id=?", (prepared['upload_id'],)).fetchone()
                    if row is None or dict(row) != prepared:
                        raise ValueError('upload_provenance_changed')
                    conn.execute("UPDATE v2_attachment_uploads SET state='ready', updated_at=? WHERE upload_id=?", (now_iso(), prepared['upload_id']))
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                return receipt(conn.execute("SELECT * FROM v2_attachment_uploads WHERE upload_id=?", (prepared['upload_id'],)).fetchone())
            return await self._attachment_submit(ready)

        return await with_digest_locks(self, root, [sha], locked)

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

    async def complete_unmanaged_upload(self, *, root, sha, materialize):
        """Protect shared bytes before off-Store materialization, retaining lock."""
        async def locked(deadline):
            check_deadline(deadline)
            def protect(conn):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("UPDATE v2_attachment_uploads SET legacy_protected=1 WHERE blob_sha=?", (sha,))
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            await self._attachment_submit(protect)
            return await attachment_io(materialize)
        return await with_digest_locks(self, root, [sha], locked)

    @staticmethod
    def _input_rows(conn, keys):
        return {sha: [dict(row) for row in conn.execute(
            "SELECT * FROM v2_attachment_uploads WHERE blob_sha=? ORDER BY upload_id", (sha,))] for sha in keys}

    @contextmanager
    def input_attachment_guard(self, conn, attachments, *, prepared=None):
        if getattr(self, '_attachment_root', None) and attachments:
            if prepared is None or self._input_rows(conn, sorted({a['key'] for a in attachments})) != prepared:
                raise ValueError('attachment_missing')
        yield

    async def commit_attachment_input(self, attachments, operation):
        root = getattr(self, '_attachment_root', None)
        if not root or not attachments:
            return await self.submit(operation)
        keys = sorted({a['key'] for a in attachments})
        async def locked(deadline):
            current = await self._attachment_submit(lambda conn: self._input_rows(conn, keys))
            def verify():
                for sha, rows in current.items():
                    ready = [row for row in rows if row['state'] == 'ready']
                    if rows and not ready:
                        raise ValueError('attachment_missing')
                    if ready:
                        verify_publication_bytes(root, ready[0])
                    elif not (Path(root) / sha[:2] / sha).is_file():
                        raise ValueError('attachment_missing')
            await attachment_io(verify)
            check_deadline(deadline)
            def commit(conn):
                with self.input_attachment_guard(conn, attachments, prepared=current):
                    return operation(conn)
            return await self._attachment_submit(commit)
        return await with_digest_locks(self, root, keys, locked)
