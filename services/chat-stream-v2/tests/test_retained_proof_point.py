"""Exact retained proof on synthetic snapshots, with the ordered walk as control."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3

import pytest

import tell_proof
from retention import initialize_history_pair
from store import Store
from tests.test_read_projection_deliveries import BIRTH
from tests.test_read_projection_dispatch import TARGET, event, route

OLD = '2026-09-01T00:00:00Z'


def session(conn):
    conn.execute("INSERT INTO sessions(host,session_name,provider,visibility,status,created_at) "
                 "VALUES('host-a','proof','codex','hidden','open',?)", (BIRTH,))
    conn.execute("INSERT INTO v2_session_generations VALUES('host-a','proof','gen')")


def delivery(conn, key, wire, **overrides):
    proof = dict(tell_id=key, to_stream_id=TARGET, text=wire, proof_generation='gen',
                 proof_session_created_at=BIRTH, proof_watermark=0,
                 proof_watermark_state='reachable', request_payload_hash=key,
                 sender_kind='service', sender_verified=True,
                 from_stream_id='fixture:sender', sender_generation='fixture-gen')
    proof.update(overrides)
    payload = dict(payload_digest=key, delivery=proof)
    conn.execute("INSERT INTO v2_tell_deliveries VALUES(?,?,1)", (key, json.dumps(payload)))
    return payload


def page(store, **bounds):
    return store.fetch_session_event_page(TARGET, before_daemon_seq=None, limit=100, **bounds)


async def legacy_read(action, monkeypatch):
    # Test-only switch present on r2; on r1 the old path is already in use.
    with monkeypatch.context() as patch:
        patch.setattr(tell_proof.ProjectionReuse, 'enable_exact_anchors', lambda self: None, raising=False)
        return await action()


def proof_walk(sql):
    return sql.startswith('SELECT event_id,event_json FROM session_event_tail WHERE stream_id=')


def proof_point(sql):
    return sql.startswith('SELECT event_id,event_json FROM session_event_tail WHERE event_id=')


async def measured(store, action, monkeypatch):
    statements, visits, decodes = [], [], []
    original = tell_proof._TailEvent.event

    def observe(tail):
        visits.append(tail.event_id)
        if tail._event is tell_proof._UNPARSED:
            decodes.append(tail.event_id)
        return original(tail)

    with monkeypatch.context() as patch:
        patch.setattr(tell_proof._TailEvent, 'event', observe)
        await store.submit(lambda c: c.set_trace_callback(statements.append))
        try:
            answer = await action()
        finally:
            await store.submit(lambda c: c.set_trace_callback(None))
    return answer, visits, decodes, [sql for sql in statements if proof_point(sql) or proof_walk(sql)]


def sqlite_work(conn, statements):
    count = 0
    plan = []

    def step():
        nonlocal count
        count += 1
        return 0

    conn.execute('SAVEPOINT proof_vm_fixture')
    try:
        for sql in statements:
            plan.extend(row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql))
            conn.set_progress_handler(step, 1)
            try:
                conn.execute(sql).fetchall()
            finally:
                conn.set_progress_handler(None, 0)
    finally:
        conn.execute('RELEASE proof_vm_fixture')
    return count, plan


@pytest.mark.parametrize('reader', ['inventory', 'page'])
def test_exact_anchor_work_is_independent_of_unrelated_history(tmp_path, monkeypatch, reader):
    async def run():
        store = Store(str(tmp_path / 'hot.db')); store.start()
        try:
            def setup(conn):
                session(conn)
                # Fixed exact anchors above all fixture noise; the cursor and
                # inventory preview stay identical as older rows are added.
                for key, seq in [('a', 1001), ('b', 1002)]:
                    row = event(conn, key, f'proof {key}')
                    conn.execute('UPDATE session_event_tail SET event_id=? WHERE event_id=?', (seq, row))
                    delivery(conn, key, f'proof {key}', proof_event_id=seq)
                initialize_history_pair(conn)
                conn.commit()
                return conn.history_archive_path

            archive = await store.submit(setup)
            if reader == 'inventory':
                action = store.list_open_sessions_with_event_summary
            else:
                async def action():
                    return await page(store, after_daemon_seq=1000)
            actual, control = [], []
            with closing(sqlite3.connect(archive)) as cold:
                for lower, upper in [(0, 40), (40, 400)]:
                    def add_noise(conn, *, archive=False):
                        for i in range(lower, upper):
                            tier = 'cold' if archive else 'hot'
                            route(conn, f'{tier}-noise-{i}', 'not a match', target=f'noise:seat-{i}')
                            if not archive:
                                conn.execute("INSERT INTO session_event_tail(event_id,stream_id,session_created_at,"
                                             "event_key,event_json,event_ts,recorded_at) VALUES(?,?,?,?,?,?,1)",
                                             (10+i, TARGET, BIRTH, f'noise-{i}', json.dumps({
                                                 'kind':'TOOL', 'stream_id':TARGET, 'timestamp':BIRTH}), BIRTH))
                        conn.commit()

                    await store.submit(add_noise)
                    add_noise(cold, archive=True)
                    want, old_visits, old_decodes, old_sql = await legacy_read(
                        lambda: measured(store, action, monkeypatch), monkeypatch)
                    rows, visits, decodes, sql = await measured(store, action, monkeypatch)
                    assert rows == want
                    if reader == 'inventory':
                        assert rows[0]['last_text'] == 'proof b' and rows[0]['_last_message_seq'] == 1002
                    else:
                        assert [r['daemon_seq'] for r in rows] == [1001, 1002]
                        assert [r['raw']['input_provenance']['kind'] for r in rows] == ['service', 'service']
                    old_vm, old_plan = await store.submit(lambda c: sqlite_work(c, old_sql))
                    vm, plan = await store.submit(lambda c: sqlite_work(c, sql))
                    control.append((len(old_visits), len(old_decodes), old_vm, old_plan))
                    actual.append((visits, decodes, vm, plan, rows))
            print(f'{reader}: control (visits, decodes, VM, plan) {control}')
            print(f'{reader}: candidate (visits, decodes, VM, plan) '
                  f'{[(len(v), len(d), w, p) for v, d, w, p, _ in actual]}')
            small, large = actual
            assert small[4] == large[4]
            assert [sample[:2] for sample in control] == [(84, 42), (804, 402)]
            for visits, decodes, vm, plan, _ in actual:
                assert sorted(visits) == sorted(decodes) == [1001, 1002]
                assert len(plan) == 2 and all(
                    'SEARCH' in p and ('INTEGER PRIMARY KEY' in p or 'event_id=?' in p) for p in plan), plan
                assert vm > 0
            assert large[2] <= small[2] * 2
        finally:
            store.stop()
    asyncio.run(run())


def test_legacy_anchor_shapes_and_complete_read_parity(monkeypatch, tmp_path):
    async def run():
        hot = tmp_path / 'hot.db'
        store = Store(str(hot)); store.start()
        try:
            def setup(conn):
                session(conn)
                expected = {}
                def register(key, anchor, kind='unknown', **overrides):
                    wire = f'input {key}'
                    seq = event(conn, key, wire)
                    anchors = {} if anchor == 'absent' else {
                        'proof_event_id': seq if anchor == 'own' else float(seq) if anchor == 'integral' else anchor}
                    delivery(conn, key, wire, **{**anchors, **overrides})
                    expected[key] = (seq, kind)
                    return seq

                assert register('boolean', True, 'service') == 1
                register('integral-float', 'integral', 'service')
                register('fraction-float', 2.25)
                register('false', False)
                register('string', '1')
                register('huge', 2**63)
                register('tiny', -(2**63)-1)
                register('object', {'event': 1})
                register('absent', 'absent', 'service')
                register('null', None, 'service')
                exact = register('exact', 'own', 'service')
                register('missing', 5000)
                register('pre-watermark', 'own', proof_watermark=5000)
                register('stale-generation', 'own', proof_generation='old')
                register('stale-birth', 'own', proof_session_created_at=OLD)
                old = event(conn, 'old-lifecycle-only', 'input reused', birth=OLD)
                register('reused', old)
                other = event(conn, 'off-stream', 'input other-stream', target='other:seat')
                register('other-stream', other)
                register('conflicting', exact)
                malformed = event(conn, 'bad-json', '', raw_json='{')
                register('malformed-json', malformed)
                event(conn, 'quoted-unsplit', 'earlier quote: input quoted (not split)')
                register('quoted', 'absent')
                overlap = register('overlap', 'own')
                delivery(conn, 'duplicate', 'input overlap', proof_event_id=overlap)
                register('operator', 'own', 'operator')
                route(conn, 'operator-route', 'input operator')
                digest_seq = register('bad-digest', 'own')
                src = conn.execute('SELECT event_json FROM session_event_tail WHERE event_id=?', (digest_seq,)).fetchone()
                conn.execute('UPDATE session_event_tail SET event_json=? WHERE event_id=?',
                             (json.dumps({**json.loads(src[0]), 'provider_text_digest':'wrong'}), digest_seq))
                conn.commit()
                return expected

            expected = await store.submit(setup)

            async def read():
                return await store.list_open_sessions_with_event_summary(), await page(store)

            pair = await read()
            assert pair == await legacy_read(read, monkeypatch)
            observed = {r['raw']['fixture']: (r['daemon_seq'], r['raw']['input_provenance']['kind'])
                        for r in pair[1] if r['raw']['fixture'] in expected}
            assert observed == expected
            assert [r['daemon_seq'] for r in pair[1]] == sorted(r['daemon_seq'] for r in pair[1])
            assert {r['raw']['fixture'] for r in pair[1]}.isdisjoint({'bad-json', 'old-lifecycle-only', 'off-stream'})

            # An external commit invalidates the proof without reusing the prior
            # read's selected row or result. The displayed input is still there.
            with closing(sqlite3.connect(hot)) as writer, writer:
                seq, _ = expected['exact']
                (raw,) = writer.execute('SELECT event_json FROM session_event_tail WHERE event_id=?', (seq,)).fetchone()
                writer.execute('UPDATE session_event_tail SET event_json=? WHERE event_id=?',
                               (json.dumps({**json.loads(raw), 'provider_text_digest':'changed'}), seq))
            after = await read()
            assert after == await legacy_read(read, monkeypatch)
            assert next(r for r in after[1] if r['daemon_seq'] == seq)['raw']['input_provenance'] == {
                'kind':'unknown', 'verified':False}
            assert await store.submit(lambda c: not c.in_transaction and c.delivery_lookup.table.reuse is None)
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['execute', 'fetch', 'both-paths'])
def test_point_read_failure_uses_legacy_result_or_error(monkeypatch, failure):
    from retention import HistoryConnection

    async def run():
        store = Store(); store.start()
        try:
            def seed(conn):
                session(conn)
                seq = event(conn, 'exact', 'proof with reader failure')
                delivery(conn, 'exact', 'proof with reader failure', proof_event_id=seq)
                conn.commit()
            await store.submit(seed)

            async def read():
                return await store.list_open_sessions_with_event_summary(), await page(store)

            expected = await legacy_read(read, monkeypatch)
            original = HistoryConnection.execute
            attempted = []
            closed = []

            class FailedCursor:
                def __init__(self, cursor):
                    self.cursor = cursor

                def fetchone(self):
                    raise sqlite3.OperationalError('fixture point fetch refused')

                def close(self):
                    self.cursor.close()
                    closed.append(True)

            def execute(conn, sql, *args):
                if proof_point(sql):
                    assert conn.in_transaction
                    attempted.append(True)
                    if failure != 'fetch':
                        raise sqlite3.OperationalError('fixture point execute refused')
                    return FailedCursor(original(conn, sql, *args))
                if failure == 'both-paths' and proof_walk(sql):
                    raise sqlite3.OperationalError('fixture legacy walk refused')
                return original(conn, sql, *args)

            monkeypatch.setattr(HistoryConnection, 'execute', execute)
            if failure == 'both-paths':
                for action in (store.list_open_sessions_with_event_summary, lambda: page(store)):
                    with pytest.raises(sqlite3.OperationalError, match='fixture legacy walk refused'):
                        await action()
            else:
                assert await read() == expected
            if hasattr(tell_proof.ProjectionReuse, 'enable_exact_anchors'):
                assert len(attempted) == 2
                assert len(closed) == (2 if failure == 'fetch' else 0)
            assert await store.submit(lambda c: not c.in_transaction and c.delivery_lookup.table.reuse is None)
        finally:
            store.stop()
    asyncio.run(run())


def test_uncertain_dispatch_keeps_earlier_legacy_error():
    async def run():
        store = Store(); store.start()
        try:
            def prove(conn):
                session(conn)
                wire = 'also appears in a non-anchor row'
                event(conn, 'earlier', wire)
                selected = event(conn, 'selected', 'not the proof wire')
                payload = delivery(conn, 'proof', wire, proof_event_id=selected)
                # The legacy dispatch probe considers the earlier matching
                # wire before its post-filter rejects the non-anchor ID.
                route(conn, 'malformed-route', None, raw='{')
                conn.commit()
                table = tell_proof.ProjectionDeliveries()
                table.reuse.enable_dispatch()
                if hasattr(table.reuse, 'enable_exact_anchors'):
                    table.reuse.enable_exact_anchors()
                conn.execute('SAVEPOINT fixture_read')
                try:
                    with pytest.raises(sqlite3.OperationalError, match='malformed JSON'):
                        tell_proof.retained_proof(conn, payload, deliveries=table)
                finally:
                    conn.execute('RELEASE fixture_read')
            await store.submit(prove)
        finally:
            store.stop()
    asyncio.run(run())
