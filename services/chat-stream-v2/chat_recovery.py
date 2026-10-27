"""Metadata-only checkpoints for interrupted satellite transcript reads.

The daemon issues a checkpoint only against a live generation. Its fixed
recovery range is consumed transactionally with the existing transcript sink.
No event payloads are queued here and closed names never acquire new bindings.
"""
from __future__ import annotations

import json
import hashlib
import re
import time
import uuid

HEX64 = re.compile(r'[0-9a-f]{64}')
MAX_SOURCES = 500
DDL = '''CREATE TABLE IF NOT EXISTS v2_chat_checkpoints (
    checkpoint_id TEXT PRIMARY KEY, host TEXT NOT NULL, session_name TEXT NOT NULL,
    generation TEXT NOT NULL, session_created_at TEXT NOT NULL, provider TEXT NOT NULL,
    pane_pid TEXT NOT NULL, native_session_id TEXT NOT NULL, file_digest TEXT NOT NULL,
    origin_offset INTEGER NOT NULL, ack_offset INTEGER NOT NULL,
    recovery_range TEXT, recovery_cursor INTEGER, recorded_at REAL NOT NULL,
    successor_checkpoint_id TEXT)'''
SOURCE_FIELDS = ('stream_id', 'provider', 'generation', 'session_created_at',
                 'pane_pid', 'native_session_id', 'file_digest')
RANGE_FIELDS = ('start', 'end', 'digest')


def offset(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def source_valid(source, host):
    return (isinstance(source, dict)
            and all(isinstance(source.get(k), str) and source[k] for k in SOURCE_FIELDS)
            and source['stream_id'].startswith(host + ':')
            and source['provider'] in {'claude', 'codex'}
            and HEX64.fullmatch(source['file_digest']) is not None
            and source['pane_pid'].isdigit())


def successor_conn(conn, row, end):
    """Link replacement proof only while the exact original lifecycle is live.

    A lost ACK can retrieve that same recorded link after closure. Neither its
    identity nor the predecessor's immutable range is reconstructed or widened.
    """
    token = row['successor_checkpoint_id']
    if token is None:
        live = conn.execute('''SELECT 1 FROM sessions s
            JOIN v2_session_generations g USING(host,session_name)
            JOIN v2_session_generation_history h USING(host,session_name,generation)
            WHERE s.host=? AND s.session_name=? AND s.status='open'
              AND s.created_at=? AND g.generation=? AND h.closed_at IS NULL
              AND s.provider=? AND CAST(s.pane_pid AS TEXT)=?''',
            (row['host'], row['session_name'], row['session_created_at'], row['generation'],
             row['provider'], row['pane_pid'])).fetchone()
        if live is None:
            return None
        token = uuid.uuid4().hex
        conn.execute('''INSERT INTO v2_chat_checkpoints
            (checkpoint_id,host,session_name,generation,session_created_at,provider,pane_pid,
             native_session_id,file_digest,origin_offset,ack_offset,recorded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (token, row['host'], row['session_name'],
            row['generation'], row['session_created_at'], row['provider'], row['pane_pid'],
            row['native_session_id'], row['file_digest'], end, end, time.time()))
        conn.execute('UPDATE v2_chat_checkpoints SET successor_checkpoint_id=? WHERE checkpoint_id=?',
                     (token, row['checkpoint_id']))
    next_row = conn.execute('SELECT * FROM v2_chat_checkpoints WHERE checkpoint_id=?', (token,)).fetchone()
    if (next_row is None or token == row['checkpoint_id'] or next_row['origin_offset'] != end
            or any(next_row[k] != row[k] for k in ('host', 'session_name', *SOURCE_FIELDS[1:]))):
        return {'error': 'recovery_successor_invalid'}
    return {'binding': {'checkpoint_id': token, 'stream_id': f"{row['host']}:{row['session_name']}",
                       **{k: next_row[k] for k in SOURCE_FIELDS if k != 'stream_id'},
                       'predecessor_checkpoint_id': row['checkpoint_id']},
            'origin_offset': next_row['origin_offset'],
            'range': json.loads(next_row['recovery_range']) if next_row['recovery_range'] else None,
            'through': next_row['recovery_cursor'] if next_row['recovery_range'] else next_row['origin_offset']}


def bind_live_conn(conn, host, sources):
    """Issue/advance proof only after live appends have committed, before ACK.

    A crash between append and this update can only cause deduplicated replay.
    Generation, pane and native source are checked again at transaction time.
    """
    import store_usage
    results = []
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for source in sources:
            sid = source.get('stream_id', '') if isinstance(source, dict) else ''
            result = {'stream_id': sid, 'status': 'source_identity_unproven'}
            results.append(result)
            if (not source_valid(source, host) or not offset(source.get('start_offset'))
                    or not offset(source.get('next_offset'))
                    or source['next_offset'] < source['start_offset']):
                continue
            name = sid[len(host) + 1:]
            row = conn.execute('''SELECT s.*, g.generation FROM sessions s
                JOIN v2_session_generations g USING(host,session_name)
                WHERE s.host=? AND s.session_name=? AND s.status='open'
                AND s.created_at=? AND g.generation=? AND CAST(s.pane_pid AS TEXT)=?
                AND s.provider=?''', (host, name, source['session_created_at'],
                source['generation'], source['pane_pid'], source['provider'])).fetchone()
            history = conn.execute('''SELECT * FROM v2_session_generation_history
                WHERE host=? AND session_name=? AND generation=? AND closed_at IS NULL''',
                (host, name, source['generation'])).fetchone()
            if row is None or history is None:
                continue
            if (history['native_session_id'] not in (None, source['native_session_id'])
                    or history['source_file_identity_digest'] not in (None, source['file_digest'])):
                continue
            try:
                observer = json.loads(row['observer_binding']) if row['observer_binding'] else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(observer, dict):
                continue
            transcript = observer.get('transcript') or {}
            if not isinstance(transcript, dict):
                continue
            recorded_digest = transcript.get('source_file_identity_digest')
            file_id, source_path = transcript.get('file_id'), transcript.get('path')
            if (recorded_digest is None and isinstance(file_id, list) and len(file_id) == 2
                    and all(type(value) is int for value in file_id)
                    and isinstance(source_path, str) and source_path.startswith('/')):
                identity = f'{source_path}\0{file_id[0]}\0{file_id[1]}'
                recorded_digest = hashlib.sha256(identity.encode('utf-8', 'surrogateescape')).hexdigest()
            if recorded_digest is not None and recorded_digest != source['file_digest']:
                continue
            native = transcript.get('native_session_id') or transcript.get('session_id')
            if source['provider'] == 'claude':
                native = native or row['claude_session_id']
            if native and native != source['native_session_id']:
                continue
            prior = conn.execute('''SELECT * FROM v2_chat_checkpoints WHERE host=?
                AND session_name=? AND generation=? AND native_session_id=? AND file_digest=?
                ORDER BY recorded_at DESC,rowid DESC LIMIT 1''', (host, name, source['generation'],
                source['native_session_id'], source['file_digest'])).fetchone()
            if prior is not None and prior['recovery_range'] is not None:
                fixed = json.loads(prior['recovery_range'])
                if prior['recovery_cursor'] != fixed['end']:
                    result['status'] = 'recovery_pending'
                    continue
                prior = None  # new live checkpoint; old fixed range remains immutable
            if prior is None:
                token = uuid.uuid4().hex
                conn.execute('''INSERT INTO v2_chat_checkpoints
                    (checkpoint_id,host,session_name,generation,session_created_at,provider,pane_pid,
                    native_session_id,file_digest,origin_offset,ack_offset,recorded_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (token, host, name, source['generation'],
                    source['session_created_at'], source['provider'], source['pane_pid'],
                    source['native_session_id'], source['file_digest'], source['start_offset'],
                    source['next_offset'], time.time()))
            else:
                token = prior['checkpoint_id']
                conn.execute('UPDATE v2_chat_checkpoints SET ack_offset=MAX(ack_offset,?) WHERE checkpoint_id=?',
                             (source['next_offset'], token))
            store_usage.history_bind_identity_conn(conn, host, name, source['generation'],
                native_session_id=source['native_session_id'], digest=source['file_digest'])
            result.update(status='bound', checkpoint_id=token, **{k: source[k] for k in SOURCE_FIELDS})
    return results


def append_recovery_conn(conn, host, batches, insert):
    """Validate each source's range/order/identity and append in one transaction.

    A bad source has no writes and cannot prevent a different source draining.
    Only the authenticated satellite with a previously issued live checkpoint
    can assert the retained file's EOF/digest. Retries cannot change that range.
    """
    results = []
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for batch in batches:
            result = {'checkpoint_id': batch.get('checkpoint_id'),
                      'stream_id': batch.get('stream_id'), 'status': 'recovery_identity_unproven'}
            results.append(result)
            if batch.get('_envelope_invalid'):
                result['status'] = 'recovery_envelope_invalid'
                continue
            if not source_valid(batch, host):
                continue
            if not isinstance(batch.get('checkpoint_id'), str):
                continue
            row = conn.execute('SELECT * FROM v2_chat_checkpoints WHERE checkpoint_id=?',
                               (batch.get('checkpoint_id'),)).fetchone()
            if row is None or row['host'] != host:
                continue
            proof = dict(row)
            proof['stream_id'] = f"{row['host']}:{row['session_name']}"
            if any(batch[k] != proof[k] for k in SOURCE_FIELDS):
                continue
            history = conn.execute('''SELECT * FROM v2_session_generation_history
                WHERE host=? AND session_name=? AND generation=?''',
                (host, row['session_name'], row['generation'])).fetchone()
            if (history is None or history['native_session_id'] != row['native_session_id']
                    or history['source_file_identity_digest'] != row['file_digest']
                    or history['provider'] != row['provider']
                    or str(history['pane_pid']) != row['pane_pid']):
                continue
            if conn.execute("SELECT 1 FROM v2_chat_checkpoints WHERE host=? AND session_name=? "
                    "AND generation<>? AND native_session_id=? AND file_digest=? LIMIT 1",
                    (host, row['session_name'], row['generation'], row['native_session_id'], row['file_digest'])).fetchone():
                result['status'] = 'recovery_source_reused'
                continue
            fixed, lo, hi = batch.get('range'), batch.get('from_offset'), batch.get('next_offset')
            result['status'] = 'recovery_range_invalid'
            if (not isinstance(fixed, dict) or set(fixed) != set(RANGE_FIELDS)
                    or not offset(fixed.get('start')) or not offset(fixed.get('end'))
                    or not isinstance(fixed.get('digest'), str) or HEX64.fullmatch(fixed['digest']) is None
                    or not offset(lo) or not offset(hi)
                    or not row['origin_offset'] <= fixed['start'] <= row['ack_offset']
                    or not fixed['start'] <= lo <= hi <= fixed['end']):
                continue
            encoded = json.dumps(fixed, sort_keys=True)
            if row['recovery_range'] is not None and row['recovery_range'] != encoded:
                result['status'] = 'recovery_range_changed'
                continue
            cursor = row['recovery_cursor'] if row['recovery_range'] is not None else fixed['start']
            if lo > cursor:
                continue
            entries = batch.get('entries')
            if not isinstance(entries, list):
                continue
            last = lo
            valid = True
            for entry in entries:
                event, end = entry.get('event'), entry.get('offset')
                if (not isinstance(event, dict) or not offset(end) or not lo < end <= hi
                        or end < last or event.get('stream_id') != batch['stream_id']
                        or event.get('provider') != row['provider']
                        or event.get('session_id') != row['native_session_id']
                        or not isinstance(event.get('raw'), dict)
                        or event['raw'].get('source_session_identity') != row['native_session_id']):
                    valid = False
                    break
                last = end
            if not valid:
                continue
            successor = successor_conn(conn, row, fixed['end']) if max(cursor, hi) == fixed['end'] else None
            if successor and successor.get('error'):
                result['status'] = successor['error']
                continue
            inserted = 0
            for entry in entries:
                inserted += insert(conn, batch['stream_id'], row['session_created_at'],
                                   entry['event'], entry['identity']) is not None
            cursor = max(cursor, hi)
            conn.execute('UPDATE v2_chat_checkpoints SET recovery_range=?,recovery_cursor=? WHERE checkpoint_id=?',
                         (encoded, cursor, row['checkpoint_id']))
            result.update(status='drained' if cursor == fixed['end'] else 'pending',
                          through=cursor, inserted=inserted)
            if cursor == fixed['end']:
                result['successor'] = successor
    return results
