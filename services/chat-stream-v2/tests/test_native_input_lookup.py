"""Event parity against a frozen oracle, plus non-timing scan regressions."""
import asyncio
import json
import random
import sqlite3
import types

import pytest

import native_input_legacy as legacy
import tell_proof
from ingest import _identity_key
from store import Store
import native_input_lookup as lookup

BIRTH = '2026-10-01T00:00:00Z'
TARGET = 'fixture:input'
WIRE_A = '[from fixture:peer] [tell:a]\npeer alpha'
WIRE_B = '[from fixture:peer] [tell:b]\npeer beta'


def with_store(action):
    async def run():
        store = Store()
        store.start()
        try:
            return await store.submit(action)
        finally:
            store.stop()
    return asyncio.run(run())


def seat(conn, target=TARGET, birth=BIRTH):
    host, name = target.split(':', 1)
    conn.execute('INSERT INTO sessions(host,session_name,provider,visibility,status,created_at) '
                 "VALUES(?,?,'claude','hidden','open',?)", (host, name, birth))
    conn.execute('INSERT INTO v2_session_generations VALUES(?,?,?)', (host, name, 'generation'))


def source(text, token='source', target=TARGET):
    return {'stream_id': target, 'provider': 'claude', 'kind': 'USER', 'text': text,
            'timestamp': BIRTH, 'raw': {'jsonl_record_uuid': token, 'jsonl_event_index': 0}}


def put(conn, event, birth=BIRTH):
    encoded = json.dumps(event)
    return conn.execute('INSERT INTO session_event_tail '
        '(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity) '
        'VALUES(?,?,?,?,?,1,?)',
        (event['stream_id'], birth, encoded, encoded, BIRTH, _identity_key(event))).lastrowid


def proof(conn, wire, key, target=TARGET, birth=BIRTH):
    payload = {'payload_digest': key, 'delivery': {
        'text': wire, 'to_stream_id': target, 'proof_generation': 'generation',
        'proof_session_created_at': birth, 'proof_watermark_state': 'reachable',
        'proof_watermark': 0, 'sender_verified': True, 'request_payload_hash': key}}
    conn.execute('INSERT INTO v2_tell_deliveries VALUES(?,?,1)', (key, json.dumps(payload)))


def background(conn, count):
    conn.executemany('INSERT INTO session_event_tail '
        '(stream_id,session_created_at,event_key,event_json,recorded_at) VALUES(?,?,?,?,1)',
        ((TARGET, BIRTH, f'background-{i}', json.dumps({'kind': 'ASSIST_TEXT',
          'text': 'x' * 1024, 'raw': {'ordinal': i}})) for i in range(count)))


def steps(conn, action):
    calls = 0
    def progress():
        nonlocal calls
        calls += 1
        return 0
    conn.set_progress_handler(progress, 100)
    try:
        action()
    finally:
        conn.set_progress_handler(None, 0)
    return calls * 100


@pytest.mark.parametrize('text', ['plain operator', WIRE_A,
    WIRE_A + '\noperator between\n' + WIRE_B, WIRE_A + '\n' + WIRE_A, 'no earlier copy'])
def test_original_event_equality_first_sight_replay_and_retention(text):
    def check(conn):
        seat(conn)
        proof(conn, WIRE_A, 'a')
        proof(conn, WIRE_B, 'b')
        event = source(text)
        expected = legacy.split_native_input(conn, event)
        assert tell_proof.split_native_input(conn, event) == expected
        ids = [put(conn, segment) for segment in expected]
        # Consumed delivery proofs cannot recut a replay. Siblings retain cuts.
        conn.execute('DELETE FROM v2_tell_deliveries')
        assert tell_proof.split_native_input(conn, event) == legacy.split_native_input(conn, event)
        for event_id in ids:
            conn.execute('DELETE FROM session_event_tail WHERE event_id=?', (event_id,))
            assert tell_proof.split_native_input(conn, event) == legacy.split_native_input(conn, event)
    with_store(check)


def test_original_event_equality_other_seat_and_recreated_session():
    def check(conn):
        seat(conn)
        seat(conn, 'fixture:other')
        proof(conn, WIRE_A, 'a')
        event = source('operator\n' + WIRE_A)
        for segment in legacy.split_native_input(conn, event):
            put(conn, segment)
        other = source(event['text'], target='fixture:other')
        assert tell_proof.split_native_input(conn, other) == legacy.split_native_input(conn, other)
        conn.execute("UPDATE sessions SET created_at='new-birth' WHERE session_name='input'")
        assert tell_proof.split_native_input(conn, event) == legacy.split_native_input(conn, event)
    with_store(check)


def test_seeded_original_event_equality_inserts_replays_deletes():
    def check(conn):
        seat(conn)
        proof(conn, WIRE_A, 'a')
        proof(conn, WIRE_B, 'b')
        rng = random.Random(8128)
        events = [source(f'operator {i}\n' + WIRE_A + '\n' + WIRE_B, str(i)) for i in range(16)]
        for _ in range(120):
            if rng.randrange(3) == 0:
                rows = conn.execute('SELECT event_id FROM session_event_tail').fetchall()
                if rows:
                    conn.execute('DELETE FROM session_event_tail WHERE event_id=?', (rng.choice(rows)[0],))
            event = rng.choice(events)
            expected = legacy.split_native_input(conn, event)
            assert tell_proof.split_native_input(conn, event) == expected
            for segment in expected:
                if not conn.execute('SELECT 1 FROM session_event_tail WHERE identity=?',
                                    (_identity_key(segment),)).fetchone():
                    put(conn, segment)
    with_store(check)


def test_prepared_miss_work_does_not_scale_with_seat_history():
    counts = []
    for size in (256, 4096):
        def check(conn):
            seat(conn)
            background(conn, size)
            event = source('brand new input')
            tell_proof.split_native_input(conn, event)  # prepare outside measurement
            return steps(conn, lambda: tell_proof.split_native_input(conn, event))
        counts.append(with_store(check))
    assert counts[1] <= max(1000, counts[0] * 2), counts


def test_replay_does_not_walk_history_for_each_past_input():
    def check(conn):
        seat(conn)
        background(conn, 4096)
        events = [source(f'past input {i}', str(i)) for i in range(12)]
        for event in events:
            put(conn, event)
        tell_proof.split_native_input(conn, events[0])
        count = steps(conn, lambda: [tell_proof.split_native_input(conn, e) for e in events])
        assert count < 12000, count
    with_store(check)


def remove_lookup_schema(conn):
    for name in lookup.SCHEMA:
        if name.startswith('v2_native_input_') and name.endswith(('insert', 'update', 'delete')):
            conn.execute(f'DROP TRIGGER {name}')
    conn.execute('DROP TABLE v2_native_input_lookup')
    conn.execute('DROP TABLE v2_native_input_coverage')


@pytest.mark.parametrize('state', ['not-started', 'partial', 'complete'])
@pytest.mark.parametrize('text', ['plain', WIRE_A, WIRE_A + '\nbetween\n' + WIRE_B,
                                WIRE_A + '\n' + WIRE_A, 'absent'])
def test_preexisting_rows_equal_at_every_preparation_state(monkeypatch, state, text):
    def check(conn):
        seat(conn)
        proof(conn, WIRE_A, 'a')
        proof(conn, WIRE_B, 'b')
        remove_lookup_schema(conn)
        event = source(text)
        for segment in legacy.split_native_input(conn, event):
            put(conn, segment)
        # Old segments lie beyond the first page, exercising real fallback.
        background(conn, 300)
        lookup.ensure_schema(conn)
        if state == 'partial':
            ticks = iter([0, 2])
            with monkeypatch.context() as patch:
                patch.setattr(lookup, 'time', types.SimpleNamespace(monotonic=lambda: next(ticks)))
                assert lookup._prepare(conn, TARGET, BIRTH, 1) is False
        if state == 'complete':
            assert lookup._prepare(conn, TARGET, BIRTH, float('inf')) is True
        with monkeypatch.context() as patch:
            patch.setattr(lookup, 'PREPARE_SECONDS', 0)
            assert tell_proof.split_native_input(conn, event) == legacy.split_native_input(conn, event)
    with_store(check)


@pytest.mark.parametrize('missing', list(lookup.SCHEMA))
def test_recreated_schema_invalidates_coverage_and_recovers_missing_writes(missing):
    def check(conn):
        seat(conn)
        proof(conn, WIRE_A, 'a')
        event = source('operator\n' + WIRE_A)
        assert lookup._prepare(conn, TARGET, BIRTH, float('inf')) is True
        kind = conn.execute('SELECT type FROM sqlite_master WHERE name=?', (missing,)).fetchone()[0]
        conn.execute(f'DROP {kind} {missing}')
        # Older code may write while maintenance is absent. Remove the insert
        # trigger as well if its referenced table was removed.
        conn.execute('DROP TRIGGER IF EXISTS v2_native_input_insert')
        for segment in legacy.split_native_input(conn, event):
            put(conn, segment)
        lookup.ensure_schema(conn)
        assert conn.execute('SELECT count(*) FROM v2_native_input_coverage').fetchone()[0] == 0
        conn.execute('DELETE FROM v2_tell_deliveries')
        assert tell_proof.split_native_input(conn, event) == legacy.split_native_input(conn, event)
    with_store(check)


def test_conflicting_retained_cuts_preserve_original_row_choice():
    def check(conn):
        seat(conn)
        proof(conn, WIRE_A, 'a')
        event = source('operator\n' + WIRE_A)
        segments = legacy.split_native_input(conn, event)
        for segment in segments:
            put(conn, segment)
        # Deliberately inconsistent old data must fall back, not choose a new cut.
        changed = json.loads(json.dumps(segments[-1]))
        changed['raw']['native_segment_spans'] = [[0, len(event['text'])]]
        conn.execute('UPDATE session_event_tail SET event_json=? WHERE identity=?',
                     (json.dumps(changed), _identity_key(segments[-1])))
        conn.execute('DELETE FROM v2_tell_deliveries')
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            assert tell_proof.split_native_input(conn, event) == legacy.split_native_input(conn, event)
        finally:
            conn.set_trace_callback(None)
        assert any('CASE WHEN json_valid(event_json)' in sql and 'LIMIT 1' in sql for sql in statements)
    with_store(check)


def test_malformed_writes_never_fail_and_lookup_skips_bad_rows():
    def check(conn):
        seat(conn)
        event = source('new input')
        for malformed in ['not json', '{"raw":{"native_input_identity":', 'x' * (1024 * 1024)]:
            row = conn.execute('INSERT INTO session_event_tail '
                '(stream_id,session_created_at,event_key,event_json,recorded_at) VALUES(?,?,?,?,1)',
                (TARGET, BIRTH, 'damaged', malformed)).lastrowid
            conn.execute('UPDATE session_event_tail SET event_json=? WHERE event_id=?', (malformed, row))
            with pytest.raises(sqlite3.OperationalError, match='malformed JSON'):
                legacy.split_native_input(conn, event)
            assert tell_proof.split_native_input(conn, event) == [event]
            assert lookup._original(conn, TARGET, BIRTH, _identity_key(event), event['text']) is None
            conn.execute('DELETE FROM session_event_tail WHERE event_id=?', (row,))
    with_store(check)


def test_escaped_keys_and_updated_source_fields_remain_indexed():
    def check(conn):
        seat(conn)
        seat(conn, 'fixture:other')
        proof(conn, WIRE_A, 'a')
        event = source('operator\n' + WIRE_A)
        segments = legacy.split_native_input(conn, event)
        row = put(conn, segments[0])
        escaped = json.dumps(segments[0]).replace('native_input_identity', '\\u006eative_input_identity')
        conn.execute('UPDATE session_event_tail SET event_json=? WHERE event_id=?', (escaped, row))
        conn.execute('DELETE FROM v2_tell_deliveries')
        assert tell_proof.split_native_input(conn, event) == legacy.split_native_input(conn, event)
        conn.execute("UPDATE session_event_tail SET stream_id='fixture:other',session_created_at='other' WHERE event_id=?", (row,))
        assert tell_proof.split_native_input(conn, event) == legacy.split_native_input(conn, event)
        conn.execute('UPDATE session_event_tail SET stream_id=?,session_created_at=? WHERE event_id=?', (TARGET, BIRTH, row))
        assert tell_proof.split_native_input(conn, event) == legacy.split_native_input(conn, event)
    with_store(check)


def test_preparation_does_not_commit_caller_transaction_and_rolls_back():
    def check(conn):
        seat(conn)
        conn.commit()
        conn.execute('BEGIN')
        put(conn, source('uncommitted'))
        assert lookup._prepare(conn, TARGET, BIRTH, float('inf')) is True
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute('SELECT count(*) FROM session_event_tail').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM v2_native_input_coverage').fetchone()[0] == 0
    with_store(check)


def test_preparation_and_index_survive_store_restart(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'restart.db'))
        store.start()
        try:
            def seed(conn):
                seat(conn)
                background(conn, 256)
                conn.commit()
            await store.submit(seed)
            event = source('after restart')
            assert await store.prepare_native_input_events([event]) == [event]
            store.stop()
            store.start()
            def check(conn):
                assert conn.execute('SELECT complete FROM v2_native_input_coverage').fetchone()[0] == 1
                assert steps(conn, lambda: tell_proof.split_native_input(conn, event)) < 1000
            await store.submit(check)
        finally:
            store.stop()
    asyncio.run(run())
