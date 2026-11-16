"""Incremental, transactionally maintained index of native input segments."""
from __future__ import annotations

import json
import time

PREPARE_SECONDS = 0.2
PAGE_ROWS = 128


def _has_identity(value):
    # CASE guarantees malformed JSON cannot reach json_type/json_extract.
    # SQLite reuses the parse for the typed read. A string prefilter scans large
    # rows more slowly and must also account for escaped keys.
    return f"""CASE WHEN json_valid({value})
                   THEN json_type({value}, '$.raw.native_input_identity') = 'text'
                   ELSE 0 END"""


_INSERT = f"""INSERT OR REPLACE INTO v2_native_input_lookup
    (event_id, stream_id, session_created_at, native_identity)
    SELECT NEW.event_id, NEW.stream_id, NEW.session_created_at,
           json_extract(NEW.event_json, '$.raw.native_input_identity')
    WHERE {_has_identity('NEW.event_json')};"""

SCHEMA = {
    'v2_native_input_lookup': """CREATE TABLE IF NOT EXISTS v2_native_input_lookup (
        event_id INTEGER PRIMARY KEY, stream_id TEXT NOT NULL,
        session_created_at TEXT NOT NULL, native_identity TEXT NOT NULL)""",
    'idx_native_input_lookup': """CREATE INDEX IF NOT EXISTS idx_native_input_lookup
        ON v2_native_input_lookup(stream_id, session_created_at, native_identity, event_id DESC)""",
    'v2_native_input_coverage': """CREATE TABLE IF NOT EXISTS v2_native_input_coverage (
        stream_id TEXT NOT NULL, session_created_at TEXT NOT NULL,
        before_id INTEGER, complete INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(stream_id, session_created_at))""",
    'v2_native_input_insert': f"""CREATE TRIGGER IF NOT EXISTS v2_native_input_insert
        AFTER INSERT ON session_event_tail BEGIN {_INSERT} END""",
    'v2_native_input_delete': """CREATE TRIGGER IF NOT EXISTS v2_native_input_delete
        AFTER DELETE ON session_event_tail BEGIN
        DELETE FROM v2_native_input_lookup WHERE event_id=OLD.event_id; END""",
    'v2_native_input_update': f"""CREATE TRIGGER IF NOT EXISTS v2_native_input_update
        AFTER UPDATE OF event_id, stream_id, session_created_at, event_json ON session_event_tail
        BEGIN DELETE FROM v2_native_input_lookup WHERE event_id=OLD.event_id;
        {_INSERT} END""",
}


def ensure_schema(conn):
    """No event scan at startup; missing maintenance invalidates all coverage."""
    conn.execute('SAVEPOINT native_input_schema')
    try:
        existing = {row[0] for row in conn.execute(
            'SELECT name FROM sqlite_master WHERE name IN (' + ','.join('?' for _ in SCHEMA) + ')',
            tuple(SCHEMA))}
        for ddl in SCHEMA.values():
            conn.execute(ddl)
        if existing != SCHEMA.keys():
            conn.execute('DELETE FROM v2_native_input_coverage')
        conn.execute('RELEASE native_input_schema')
    except BaseException:
        conn.execute('ROLLBACK TO native_input_schema')
        conn.execute('RELEASE native_input_schema')
        raise


def _prepare(conn, target, birth, deadline):
    key = (target, birth)
    coverage = conn.execute('SELECT before_id,complete FROM v2_native_input_coverage '
                            'WHERE stream_id=? AND session_created_at=?', key).fetchone()
    if coverage and coverage[1]:
        return True
    before = coverage[0] if coverage else None
    complete = False
    # RELEASE commits only a top-level savepoint, never a caller's transaction.
    conn.execute('SAVEPOINT native_input_prepare')
    try:
        while time.monotonic() < deadline:
            bound = ' AND event_id<?' if before is not None else ''
            args = (*key, before) if before is not None else key
            rows = conn.execute(
                'SELECT event_id, CASE WHEN ' + _has_identity('event_json') +
                " THEN json_extract(event_json, '$.raw.native_input_identity') END "
                'FROM session_event_tail WHERE stream_id=? AND session_created_at=?' + bound +
                ' ORDER BY event_id DESC LIMIT ?', (*args, PAGE_ROWS)).fetchall()
            if rows:
                # Also repair stale entries left while maintenance was absent.
                conn.execute('DELETE FROM v2_native_input_lookup WHERE event_id IN (' +
                             ','.join('?' for _ in rows) + ')', tuple(row[0] for row in rows))
                conn.executemany('INSERT INTO v2_native_input_lookup VALUES(?,?,?,?)',
                    ((row[0], *key, row[1]) for row in rows if row[1] is not None))
                before = rows[-1][0]
            complete = len(rows) < PAGE_ROWS
            conn.execute('INSERT OR REPLACE INTO v2_native_input_coverage VALUES(?,?,?,?)',
                         (*key, before, int(complete)))
            if complete:
                break
        conn.execute('RELEASE native_input_prepare')
    except BaseException:
        conn.execute('ROLLBACK TO native_input_prepare')
        conn.execute('RELEASE native_input_prepare')
        raise
    return complete


def _original(conn, target, birth, identity, text):
    # Preserve the original row choice. The sole exception is malformed JSON,
    # now skipped instead of allowing a damaged historical row to stop intake.
    return conn.execute(
        'SELECT event_json FROM session_event_tail WHERE stream_id=? AND session_created_at=? '
        "AND CASE WHEN json_valid(event_json) THEN json_extract(event_json,'$.raw.native_input_identity')=? "
        "AND json_extract(event_json,'$.raw.native_input_text')=? ELSE 0 END LIMIT 1",
        (target, birth, identity, text)).fetchone()


def prior_input(conn, target, birth, identity, text, *, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + PREPARE_SECONDS
    if not _prepare(conn, target, birth, deadline):
        return _original(conn, target, birth, identity, text)
    rows = conn.execute(
        'SELECT e.event_json FROM v2_native_input_lookup n '
        'JOIN session_event_tail e ON e.event_id=n.event_id '
        'WHERE n.stream_id=? AND n.session_created_at=? AND n.native_identity=? '
        'AND e.stream_id=n.stream_id AND e.session_created_at=n.session_created_at '
        "AND CASE WHEN json_valid(e.event_json) THEN json_extract(e.event_json,'$.raw.native_input_identity')=? "
        "AND json_extract(e.event_json,'$.raw.native_input_text')=? ELSE 0 END "
        'ORDER BY n.event_id DESC', (target, birth, identity, identity, text)).fetchall()
    if not rows:
        return None
    cuts = [(json.loads(row[0]).get('raw') or {}).get('native_segment_spans') for row in rows]
    if any(value != cuts[0] for value in cuts[1:]):
        return _original(conn, target, birth, identity, text)
    return rows[0]
