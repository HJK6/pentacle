"""Bounded, evidence-only retirement of an old open-recipient lane digest.

The original tell remains immutable on a negative. No caller can supply an
absence verdict, and this module has no transport/input capability.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from receipt_proof import ARCHIVE_SECONDS, ARCHIVE_USER_LIMIT, HOT_ROW_LIMIT
from submission_events import (COMMITTED_PENDING_PROOF_STATUSES, PROOF_TERMINAL_BOUND_S,
                               submission_text_matches, system_submission_matches)
from v2_runtime import iso_now


def unknown(reason):
    return {'state': 'unknown', 'reason': reason}


def _file_identity(path):
    if path is None:
        return None
    try:
        s = path.stat()
        main = (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    except FileNotFoundError:
        return None
    try:
        w = Path(str(path) + '-wal').stat()
        return main, (w.st_dev, w.st_ino, w.st_size, w.st_mtime_ns)
    except FileNotFoundError:
        return main, None


def _read_rows(conn, target, birth, anchor, tip, cap, deadline):
    rows = []
    for r in conn.execute('SELECT event_id,event_json FROM session_event_tail WHERE stream_id=? '
                          'AND session_created_at=? AND event_id>=? AND event_id<=? '
                          'ORDER BY event_id LIMIT ?', (target, birth, anchor, tip, cap + 1)):
        if len(rows) >= cap or time.monotonic() >= deadline:
            raise ValueError('readback_bound')
        rows.append((int(r[0]), str(r[1])))
    return rows


def _archive_rows(path, target, birth, anchor, tip, deadline):
    if path is None or not path.is_file():
        return [], None
    identity = _file_identity(path)
    remaining = max(0.001, deadline - time.monotonic())
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                 timeout=remaining)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        rows = _read_rows(conn, target, birth, anchor, tip, ARCHIVE_USER_LIMIT, deadline)
    if _file_identity(path) != identity:
        raise ValueError('archive_changed')
    return rows, identity


def _current(conn, nid, owner, max_attempts):
    row = conn.execute('SELECT * FROM v2_outbound_notices WHERE notice_id=?', (nid,)).fetchone()
    if (row is None or row['kind'] != 'lane_digest' or row['lease_owner'] != owner
            or row['delivered_at'] or row['terminal_at'] or (row['lease_until'] or 0) <= time.time()):
        raise ValueError('notice_lease_changed')
    if row['attempts'] < max_attempts:
        raise ValueError('retry_bound_not_reached')
    from datetime import datetime
    created = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')).timestamp()
    if time.time() - created < PROOF_TERMINAL_BOUND_S:
        raise ValueError('age_bound_not_reached')
    meta = json.loads(row['metadata'])
    if not isinstance(meta, dict):
        raise ValueError('metadata_malformed')
    target = row['recipient_stream_id']
    host, sep, name = target.partition(':')
    session = conn.execute('SELECT s.created_at,s.status,g.generation FROM sessions s '
        'JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name '
        'WHERE s.host=? AND s.session_name=?', (host, name)).fetchone() if sep else None
    if (session is None or session['status'] != 'open'
            or not meta.get('root_generation') or session['generation'] != meta['root_generation']):
        raise ValueError('recipient_generation_changed')
    tell = conn.execute('SELECT reply,created_at FROM v2_tell_deliveries WHERE tell_id=?', (row['tell_id'],)).fetchone()
    if tell is None:
        raise ValueError('tell_missing')
    payload = json.loads(tell['reply'])
    if not isinstance(payload, dict):
        raise ValueError('tell_malformed')
    delivery, reply = payload.get('delivery', {}), payload.get('reply', {})
    if not isinstance(delivery, dict) or not isinstance(reply, dict):
        raise ValueError('tell_malformed')
    if time.time() - float(tell['created_at']) < PROOF_TERMINAL_BOUND_S:
        raise ValueError('original_attempt_age_bound_not_reached')
    digest = hashlib.sha256((target + '\0' + row['body']).encode()).hexdigest()
    anchor = delivery.get('proof_watermark')
    if (payload.get('payload_digest') != digest or delivery.get('text') != row['body']
            or delivery.get('to_stream_id') != target or reply.get('to_stream_id') != target
            or delivery.get('delivery_status') not in COMMITTED_PENDING_PROOF_STATUSES
            or reply.get('delivery_status') not in COMMITTED_PENDING_PROOF_STATUSES):
        raise ValueError('tell_identity_changed')
    if (type(anchor) is not int or anchor <= 0
            or delivery.get('proof_watermark_state') != 'reachable'):
        raise ValueError('watermark_unknown')
    birth = session['created_at']
    tip = conn.execute('SELECT MAX(event_id) FROM session_event_tail WHERE stream_id=? '
                       'AND session_created_at=?', (target, birth)).fetchone()[0]
    if tip is None or tip < anchor:
        raise ValueError('anchor_missing')
    return dict(row), meta, target, birth, anchor, tip, digest, tell['reply']


async def reconcile(store, notice_id, owner, *, max_attempts):
    """Return proven/terminal/typed unknown, using the existing notice lease."""
    deadline = time.monotonic() + ARCHIVE_SECONDS

    def snapshot(conn):
        values = _current(conn, notice_id, owner, max_attempts)
        row, meta, target, birth, anchor, tip, digest, tell = values
        rows = _read_rows(conn, target, birth, anchor, tip, HOT_ROW_LIMIT, deadline)
        archive_path = getattr(conn, 'history_archive_path', None)
        return values, rows, Path(archive_path) if archive_path else None

    try:
        values, hot, archive_path = await store.submit(snapshot)
        row, meta, target, birth, anchor, tip, digest, tell = values
        archived, archive_identity = await asyncio.to_thread(
            _archive_rows, archive_path, target, birth, anchor, tip, deadline)
        merged = {}
        for eid, body in hot + archived:
            if eid in merged and merged[eid] != body:
                return unknown('conflicting_event_copies')
            merged[eid] = body
        if anchor not in merged:
            return unknown('archive_unavailable' if archive_identity is None else 'anchor_missing')
        events = []
        for eid, body in sorted(merged.items()):
            if time.monotonic() >= deadline:
                return unknown('readback_bound')
            event = json.loads(body)
            if not isinstance(event, dict) or event.get('stream_id', target) != target:
                return unknown('malformed_event')
            if event.get('kind') == 'TELL' and not isinstance(event.get('raw') or {}, dict):
                return unknown('malformed_event')
            if eid > anchor:
                events.append((eid, event))
        system = [(eid, event) for eid, event in events if system_submission_matches(event, row['body'])]
        if len(system) > 1:
            return unknown('ambiguous_system_proof')
        positive = system or [(eid, event) for eid, event in events
            if (event.get('kind') == 'USER' or (event.get('kind') == 'TELL'
                and isinstance((event.get('raw') or {}).get('peer_wire_text'), str)))
            and submission_text_matches(event, row['body'])]

        def finish(conn):
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                current = _current(conn, notice_id, owner, max_attempts)
                current_archive = getattr(conn, 'history_archive_path', None)
                if (current != values or _file_identity(archive_path) != archive_identity
                        or (Path(current_archive) if current_archive else None) != archive_path):
                    return unknown('evidence_changed')
                if (_read_rows(conn, target, birth, anchor, tip, HOT_ROW_LIMIT, deadline) != hot
                        or time.monotonic() >= deadline):
                    return unknown('evidence_changed')
                if positive:
                    eid, event = positive[0]
                    return {'state': 'proven', 'digest': digest, 'tell_id': row['tell_id'],
                            'proof': {'proof_state': 'proven', 'proof_watermark': anchor,
                                      'proof_event_id': eid, 'proof_event_at': event.get('timestamp')}}
                evidence = {'outcome': 'absent', 'watermark': anchor, 'through_daemon_seq': tip,
                            'generation': meta['root_generation'], 'checked_at': iso_now(),
                            'hot_rows': len(hot), 'archive_rows': len(archived)}
                metadata = {**meta, 'digest_absence_readback': evidence}
                conn.execute('UPDATE v2_outbound_notices SET terminal_at=?,terminal_reason=?, '
                    'last_error=?,next_action=?,metadata=?,lease_owner=NULL,lease_until=NULL '
                    'WHERE notice_id=? AND lease_owner=?',
                    (iso_now(), 'delivery_absent_after_bound', 'delivery_absent_after_bound',
                     'retain original tell; current owed digest uses the next ordinary deadline',
                     json.dumps(metadata, sort_keys=True), notice_id, owner))
                return {'state': 'terminal'}
        return await store.submit(finish)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and type(exc) is ValueError else type(exc).__name__
        return unknown(reason[:80])
