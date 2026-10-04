"""Conservative managed-only GC; content retention never grants read access.

The shared digest lock covers the provenance rows and filesystem operation.
Unknown/legacy files are never candidates. Retained owner text can only retain
extra storage, never authorize fetching it.
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os
import re
import sqlite3
from attachment_locks import digest_lock
from store_attachments import verify_publication_bytes

ORPHAN_AGE = timedelta(hours=24)
PENDING_AGE = timedelta(minutes=5)
OWNER_COLUMNS = {
    'session_event_tail': ('event_json',),
    'v2_assistant_composite_publications': ('canonical_payload_json', 'attachment_ids_json', 'evidence_refs_json'),
    'v2_assistant_composite_routes': ('attachments_json',),
    'v2_assistant_composite_operations': ('payload_json', 'evidence_refs_json'),
    'v2_schedules': ('prompt_blob_id',),
    'v2_send_receipts': ('attachments_json',),
    'v2_reports': None,
}


def _stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError, AttributeError):
        return None


def retained_owner(conn, sha, upload_ids, *, only=None):
    """Conservative retention lookup, explicitly not an authorization predicate."""
    tables = {r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type="table"')}
    for table, wanted in OWNER_COLUMNS.items():
        if table not in tables or (only is not None and table not in only):
            continue
        columns = {r[1]: r[2] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        names = [n for n, t in columns.items() if 'TEXT' in t.upper()] if wanted is None else [n for n in wanted if n in columns]
        for column in names:
            quoted = column.replace('"', '""')
            for token in (sha, *upload_ids):
                if conn.execute(f"""SELECT 1 FROM "{table}"
                    WHERE instr(lower(CAST("{quoted}" AS TEXT)),?)>0 OR EXISTS (
                      SELECT 1 FROM json_tree(CASE WHEN json_valid("{quoted}")
                        THEN "{quoted}" ELSE 'null' END)
                      WHERE type='text' AND instr(lower(CAST(atom AS TEXT)),?)>0)
                    LIMIT 1""", (token.lower(), token.lower())).fetchone():
                    return True
    if only is None and 'v2_attachment_refs' in tables:
        if conn.execute("""SELECT 1 FROM v2_attachment_refs r JOIN v2_assistant_composite_publications p
            ON p.publication_key=r.owner_id AND p.stream_id=r.stream_id
            WHERE r.owner_kind='publication' AND r.blob_sha=? LIMIT 1""", (sha,)).fetchone():
            return True
    return False


def _unlink_owned(path):
    path.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def sweep_managed(conn, root, *, archive_path=None, now=None, limit=100, after_sha=''):
    """One bounded batch; reconciliation is safe to repeat after a crash."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('timezone required')
    root = Path(root)
    counts = dict(examined=0, deleted=0, promoted=0, reconciled=0, retained=0, next_cursor='')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_attachment_uploads'").fetchone():
        return counts
    # A missing/replaced root must not turn a mount/path outage into removal
    # of provenance. Every managed upload has already created its lock directory.
    if not root.is_dir() or root.is_symlink() or not (root / '.attachment-locks').is_dir():
        return counts
    archive = None
    if archive_path is not None and Path(archive_path).exists():
        # Never create an archive. Main's write transaction also serializes the
        # existing retention migration into this archive.
        archive = sqlite3.connect(Path(archive_path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        bounded = max(0, min(limit, 500))
        candidates = conn.execute('SELECT DISTINCT blob_sha FROM v2_attachment_uploads WHERE blob_sha>? ORDER BY blob_sha LIMIT ?', (after_sha, bounded)).fetchall()
        if candidates and len(candidates) == bounded:
            counts['next_cursor'] = candidates[-1][0]
        for candidate in candidates:
            sha = candidate[0]
            if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
                continue
            counts['examined'] += 1
            with digest_lock(root, sha):
                conn.execute('BEGIN IMMEDIATE')
                try:
                    rows = conn.execute('SELECT * FROM v2_attachment_uploads WHERE blob_sha=?', (sha,)).fetchall()
                    if not rows:
                        conn.commit()
                        continue
                    ids = [r['upload_id'] for r in rows]
                    owner = retained_owner(conn, sha, ids) or (archive is not None and retained_owner(archive, sha, ids))
                    protected = any(r['legacy_protected'] for r in rows)
                    nonmanaged = retained_owner(conn, sha, ids, only={'v2_reports', 'v2_schedules'}) or (archive is not None and retained_owner(archive, sha, ids, only={'v2_reports', 'v2_schedules'}))
                    if nonmanaged:
                        conn.execute('UPDATE v2_attachment_uploads SET legacy_protected=1 WHERE blob_sha=?', (sha,))
                        protected = True
                    path = root / sha[:2] / sha
                    if path.is_symlink():
                        counts['retained'] += 1
                        conn.commit()
                        continue
                    pending = [r for r in rows if r['state'] == 'pending']
                    changed = False
                    for row in pending:
                        stamp = _stamp(row['updated_at'])
                        if stamp is None or now - stamp < PENDING_AGE:
                            continue
                        if path.is_file():
                            try:
                                verify_publication_bytes(root, row)
                            except ValueError:
                                if not owner and not protected:
                                    conn.execute('DELETE FROM v2_attachment_uploads WHERE upload_id=?', (row['upload_id'],))
                                    counts['reconciled'] += 1
                                    changed = True
                                continue
                            conn.execute("UPDATE v2_attachment_uploads SET state='ready',updated_at=? WHERE upload_id=?", (now.isoformat(), row['upload_id']))
                            counts['promoted'] += 1
                            changed = True
                        elif not path.exists() and not owner and not protected:
                            conn.execute('DELETE FROM v2_attachment_uploads WHERE upload_id=?', (row['upload_id'],))
                            counts['reconciled'] += 1
                            changed = True
                    if changed:
                        remaining = conn.execute('SELECT 1 FROM v2_attachment_uploads WHERE blob_sha=? LIMIT 1', (sha,)).fetchone()
                        if remaining is None and not owner and not protected and path.is_file():
                            _unlink_owned(path)
                        conn.commit()
                        continue
                    # Owned missing-byte receipts remain tombstones, preserving
                    # authorized blob_unknown rather than changing read scope.
                    if not path.exists() and not owner and not protected and not pending:
                        conn.execute('DELETE FROM v2_attachment_refs WHERE blob_sha=?', (sha,))
                        conn.execute('DELETE FROM v2_attachment_uploads WHERE blob_sha=?', (sha,))
                        counts['reconciled'] += len(rows)
                        conn.commit()
                        continue
                    stamps = [_stamp(r['uploaded_at']) for r in rows]
                    if owner or protected or pending or any(s is None or now - s < ORPHAN_AGE for s in stamps):
                        counts['retained'] += 1
                        conn.commit()
                        continue
                    if not path.is_file():
                        counts['retained'] += 1
                        conn.commit()
                        continue
                    _unlink_owned(path)
                    conn.execute('DELETE FROM v2_attachment_refs WHERE blob_sha=?', (sha,))
                    conn.execute('DELETE FROM v2_attachment_uploads WHERE blob_sha=?', (sha,))
                    conn.commit()
                    counts['deleted'] += 1
                except BaseException:
                    conn.rollback()
                    raise
    finally:
        if archive is not None:
            archive.close()
    return counts
