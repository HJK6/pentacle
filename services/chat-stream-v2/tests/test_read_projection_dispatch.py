"""Read-scoped operator provenance through the real Store snapshot readers."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3

import pytest

from store import Store
from tests.test_read_projection_deliveries import BIRTH, corpus

TARGET = 'host-a:proof'
EMPTY = 'host-a:empty'
ROUTES = 'v2_assistant_composite_routes'


def route(conn, key, wire, *, target=TARGET, generation='gen',
          state='resolved', mode='direct_primary', raw=None):
    body = json.dumps({'admission_mode': mode, 'direct_envelope': {'wire_body': wire}})
    conn.execute(f"INSERT INTO {ROUTES}(route_id,stream_id,input_identity,payload_digest,"
                 "input_request_id,body,routing_state,dispatch_id,route_target,"
                 "route_target_generation,route_json,created_at,updated_at) "
                 "VALUES(?,'assistant:primary',?,'digest',?,'body',?,?,?,?,?,?,?)",
                 (key, key, key, state, key, target, generation, body if raw is None else raw, BIRTH, BIRTH))


def event(conn, key, wire, *, target=TARGET, birth=BIRTH, kind='USER', raw_json=None):
    record = {'kind': kind, 'stream_id': target, 'timestamp': BIRTH, 'text': wire, 'raw': {'fixture': key}}
    if kind == 'TELL':
        record['raw']['peer_wire_text'] = wire
        record['message_envelope'] = {'kind': 'peer_delivery'}
    return conn.execute(
        "INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at) "
        "VALUES(?,?,?,?,?,1)", (target, birth, key, json.dumps(record) if raw_json is None else raw_json, BIRTH)
    ).lastrowid


def repeat_inputs(conn, begin, count):
    for i in range(begin, begin + count):
        event(conn, f'op-{i}', 'input operator', kind='TELL')
        event(conn, f'miss-{i}', 'input with no dispatch')
        event(conn, f'empty-{i}', 'input outside any route range', target=EMPTY)
    conn.commit()


async def traced(store, action):
    statements = []
    await store.submit(lambda c: c.set_trace_callback(statements.append))
    try:
        answer = await action()
    finally:
        await store.submit(lambda c: c.set_trace_callback(None))
    dispatch = [s for s in statements if s.lstrip().upper().startswith('SELECT') and
                f'FROM {ROUTES}' in s and '$.direct_envelope.wire_body' in s]
    return answer, dispatch


def counts(sql):
    legacy = sum(s.startswith('SELECT dispatch_id') for s in sql)
    return legacy, len(sql) - legacy


async def seed(store):
    await corpus(store)
    def write(conn):
        conn.execute("INSERT INTO sessions(host,session_name,provider,visibility,status,created_at) "
                     "VALUES('host-a','empty','codex','hidden','open',?)", (BIRTH,))
        conn.execute("INSERT INTO v2_session_generations VALUES('host-a','empty','gen')")
        # None of these rows is an authoritative match for the misses.
        route(conn, 'queued', 'input with no dispatch', state='queued')
        route(conn, 'indirect', 'input with no dispatch', mode='indirect')
        route(conn, 'old-gen', 'input with no dispatch', generation='stale')
        repeat_inputs(conn, 0, 2)
    await store.submit(write)


async def page(store, target=TARGET, **cursor):
    return await store.fetch_session_event_page(target, before_daemon_seq=cursor.pop('before_daemon_seq', None),
                                                limit=100, **cursor)


@pytest.mark.parametrize('reader', ['inventory', 'page', 'empty-page'])
def test_dispatch_sql_scales_by_range_not_event(reader):
    async def run():
        store = Store(); store.start()
        try:
            await seed(store)
            if reader == 'inventory':
                action = store.list_open_sessions_with_event_summary
            else:
                def action():
                    return page(store, TARGET if reader == 'page' else EMPTY)

            observed = []
            for extra in (0, 6):
                if extra:
                    await store.submit(lambda c: repeat_inputs(c, 2, extra))
                rows, sql = await traced(store, action)
                if reader == 'inventory':
                    by_id = {r['stream_id']: r for r in rows}
                    assert len(rows) == 2
                    assert by_id[TARGET]['last_text'] == 'input with no dispatch'
                    assert by_id[TARGET]['last_kind'] == 'USER'
                    assert by_id[EMPTY]['last_text'] == 'input outside any route range'
                    assert by_id[TARGET]['_last_message_seq'] > 0
                else:
                    assert len(rows) == (11 + (2 + extra) * 2 if reader == 'page' else 2 + extra)
                    assert [e['daemon_seq'] for e in rows] == sorted(e['daemon_seq'] for e in rows)
                    origins = {e['raw'].get('fixture'): e['raw']['input_provenance']['kind'] for e in rows}
                    if reader == 'page':
                        assert origins['operator'] == origins['op-0'] == 'operator'
                        assert origins['peer'] == 'peer'  # retained proof also checks dispatch
                        assert origins['miss-0'] == origins['overlap'] == 'unknown'
                    else:
                        assert set(origins.values()) == {'unknown'}
                observed.append(counts(sql))
            print(f'{reader}: (legacy probes, lookup loads) {observed}')
            expected = (0, 2 if reader == 'inventory' else 1)
            assert observed == [expected, expected]
        finally:
            store.stop()
    asyncio.run(run())


def test_targeted_vm_work_is_independent_of_unrelated_hot_and_cold_routes(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'hot.db')); store.start()
        try:
            await seed(store)
            from retention import initialize_history_pair

            def pair(conn):
                initialize_history_pair(conn)
                conn.commit()
                return conn.history_archive_path
            archive = await store.submit(pair)
            assert archive.is_file()
            with closing(sqlite3.connect(archive)) as cold:
                # A matching archived route is not dispatch authority.
                route(cold, 'cold-authority', 'input with no dispatch')
                cold.commit()
                observed = []
                for low, high in ((0, 40), (40, 400)):
                    def noisy(conn, prefix):
                        for i in range(low, high):
                            route(conn, f'{prefix}-{i}', 'unrelated', target=f'noise:seat-{i}')
                        conn.commit()
                    await store.submit(lambda c: noisy(c, 'hot'))
                    noisy(cold, 'cold')
                    rows, sql = await traced(store, lambda: page(store))
                    assert counts(sql) == (0, 1)
                    assert next(row for row in rows if row['raw'].get('fixture') == 'miss-0')[
                        'raw']['input_provenance']['kind'] == 'unknown'

                    def work(conn):
                        plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql[0])]
                        assert any('SEARCH' in op and 'idx_history_route_target' in op
                                   and 'route_target=? AND route_target_generation=?' in op for op in plan), plan
                        steps = 0

                        def count():
                            nonlocal steps
                            steps += 1
                            return 0

                        conn.execute('SAVEPOINT dispatch_vm_sample')
                        try:
                            conn.set_progress_handler(count, 1)
                            conn.execute(sql[0]).fetchall()
                            return steps
                        finally:
                            conn.set_progress_handler(None, 0)
                            conn.execute('RELEASE dispatch_vm_sample')
                    observed.append((rows, await store.submit(work)))
                assert observed[0][0] == observed[1][0]
                small, large = (result[1] for result in observed)
                print(f'dispatch VM steps: 40 to 400 unrelated rows per tier: {small} -> {large}')
                assert small > 0 and large <= small * 2
        finally:
            store.stop()
    asyncio.run(run())


async def projection_pair(store, monkeypatch, **cursor):
    import tell_proof

    async def read():
        return await store.list_open_sessions_with_event_summary(), await page(store, **cursor)

    with monkeypatch.context() as patch:
        patch.setattr(tell_proof.ProjectionReuse, 'enable_dispatch', lambda self: None)
        expected = await read()
    actual = await read()
    assert actual == expected
    assert await store.submit(lambda c: c.delivery_lookup.table.reuse is None)
    return actual


@pytest.mark.parametrize('limit', ['rows', 'bytes', 'nontext'])
def test_range_fallback_is_sticky_and_other_ranges_still_load(monkeypatch, limit):
    import tell_proof

    async def run():
        store = Store(); store.start()
        try:
            await seed(store)
            if limit == 'rows':
                monkeypatch.setattr(tell_proof, '_DISPATCH_REUSE_MAX_ROWS', 1)
                wire = 'input operator'  # duplicate still consumes a source row
            elif limit == 'bytes':
                monkeypatch.setattr(tell_proof, '_DISPATCH_REUSE_MAX_BYTES', 80)
                wire = 'é' * 33  # 66 UTF-8 bytes plus the other wire and key
            else:
                wire = 7  # SQL numeric extraction does not equal the bound text '7'
            def write(conn):
                route(conn, 'overflow', wire)
                event(conn, 'numeric-text', '7')
                conn.commit()
            await store.submit(write)
            _, rows = await projection_pair(store, monkeypatch)
            assert next(r for r in rows if r['raw'].get('fixture') == 'op-0')['raw']['input_provenance']['kind'] == 'operator'
            assert next(r for r in rows if r['raw'].get('fixture') == 'numeric-text')['raw']['input_provenance']['kind'] == 'unknown'
            _, sql = await traced(store, store.list_open_sessions_with_event_summary)
            legacy, loads = counts(sql)
            assert legacy > 10 and loads == 2
            assert len([s for s in sql if not s.startswith('SELECT dispatch_id') and "'host-a:proof'" in s]) == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_malformed_route_uses_legacy_short_circuit_and_same_sql_error(monkeypatch):
    import tell_proof

    async def run():
        store = Store(); store.start()
        try:
            await seed(store)
            def write(conn):
                route(conn, 'malformed', None, raw='{')
                first = event(conn, 'only-hit', 'input operator')
                second = event(conn, 'only-miss', 'no exact dispatch')
                conn.commit()
                return first, second
            hit, miss = await store.submit(write)
            async def hit_only():
                return await page(store, after_daemon_seq=hit - 1, before_daemon_seq=miss)
            async def miss_only():
                return await page(store, after_daemon_seq=hit)
            rows, sql = await traced(store, hit_only)
            assert rows[0]['raw']['input_provenance']['kind'] == 'operator'
            assert counts(sql) == (1, 1)
            with monkeypatch.context() as patch:
                patch.setattr(tell_proof.ProjectionReuse, 'enable_dispatch', lambda self: None)
                assert await hit_only() == rows
                with pytest.raises(sqlite3.OperationalError) as legacy:
                    await miss_only()
            with pytest.raises(sqlite3.OperationalError) as candidate:
                await miss_only()
            assert str(legacy.value) == str(candidate.value) == 'malformed JSON'
            assert await store.submit(lambda c: c.delivery_lookup.table.reuse is None and not c.in_transaction)
        finally:
            store.stop()
    asyncio.run(run())


def test_complete_projection_parity_and_commit_visibility(monkeypatch, tmp_path):
    async def run():
        hot = tmp_path / 'hot.db'
        store = Store(str(hot)); store.start()
        try:
            await seed(store)
            def write(conn):
                prior = event(conn, 'reused-name', 'old lifecycle', birth='2026-09-01T00:00:00Z')
                invalid = event(conn, 'unparseable', '', raw_json='{')
                notice = event(conn, 'notice', '', raw_json=json.dumps({
                    'kind': 'USER', 'stream_id': TARGET, 'timestamp': BIRTH, 'text': 'fixture notice',
                    'raw': {'fixture': 'notice', 'daemon_notice': True},
                }))
                conn.commit()
                return prior, invalid, notice
            prior, invalid, notice = await store.submit(write)
            by_origin = lambda rows: {r['raw'].get('fixture'): r['raw']['input_provenance']['kind'] for r in rows}
            (inventory, latest) = await projection_pair(store, monkeypatch)
            assert by_origin(latest)['service'] == 'service'
            assert by_origin(latest)['notice'] == 'unknown'
            assert not latest[-1]['raw'].get('daemon_notice')  # reserved marker is not authority
            assert {prior, invalid}.isdisjoint(r['daemon_seq'] for r in latest)
            assert 'reused-name' not in by_origin(latest)
            assert next(s for s in inventory if s['stream_id'] == TARGET)['last_text'] == 'fixture notice'
            (_, earlier) = await projection_pair(store, monkeypatch, before_daemon_seq=notice)
            assert earlier == latest[:-1]
            assert by_origin(latest)['op-0'] == 'operator'
            assert by_origin(latest)['peer'] == 'peer'
            assert by_origin(latest)['overlap'] == 'unknown'
            # Simulate a different connection committing a new generation and its
            # route while retained events remain in the same lifecycle.
            with closing(sqlite3.connect(hot)) as writer, writer:
                writer.execute("UPDATE v2_session_generations SET generation='next' WHERE host='host-a' AND session_name='proof'")
                route(writer, 'next-route', 'input with no dispatch', generation='next')
            (_, next_page) = await projection_pair(store, monkeypatch)
            origin = by_origin(next_page)
            assert origin['op-0'] == origin['peer'] == 'unknown'
            assert origin['miss-0'] == 'operator'
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('cap', ['rows', 'bytes'])
def test_bounds_apply_across_ranges_and_direct_dispatch_still_returns_id(monkeypatch, cap):
    import tell_proof

    async def run():
        store = Store(); store.start()
        try:
            def check(conn):
                other = 'host-a:other'
                route(conn, 'a', 'é', generation='g')
                route(conn, 'b', 'é', target=other, generation='g')
                conn.commit()
                if cap == 'rows':
                    monkeypatch.setattr(tell_proof, '_DISPATCH_REUSE_MAX_ROWS', 1)
                else:
                    keys = len(TARGET) + len(other) + 2 * len('g')
                    # Both keys plus 3 bytes leave room for one two-byte wire,
                    # but not both. Either range fits independently.
                    monkeypatch.setattr(tell_proof, '_DISPATCH_REUSE_MAX_BYTES', keys + 3)
                reuse = tell_proof.ProjectionReuse()
                reuse.enable_dispatch()
                conn.execute('SAVEPOINT dispatch_bound')
                try:
                    assert reuse.dispatch_contains(conn, TARGET, 'é', 'g') is True
                    assert reuse.dispatch_contains(conn, TARGET, 'e\u0301', 'g') is False
                    assert reuse.dispatch_contains(conn, other, 'é', 'g') is None
                    assert reuse.dispatch_contains(conn, other, 'é', 'g') is None
                    assert tell_proof.operator_dispatch(conn, other, 'é', 'g')['dispatch_id'] == 'b'
                    assert not tell_proof.operator_dispatch(conn, other, 'e\u0301', 'g')
                    fresh = tell_proof.ProjectionReuse()
                    fresh.enable_dispatch()
                    assert fresh.dispatch_contains(conn, other, 'é', 'g') is True
                finally:
                    conn.execute('RELEASE dispatch_bound')
            await store.submit(check)
        finally:
            store.stop()
    asyncio.run(run())


def test_paired_archive_receipt_and_proof_overflow_keep_legacy_results(monkeypatch, tmp_path):
    import hashlib
    from retention import initialize_history_pair

    async def run():
        store = Store(str(tmp_path / 'hot.db')); store.start()
        try:
            await seed(store)
            wire = 'archived dispatch-shaped receipt'
            def add_event(conn):
                seq = event(conn, 'archive-proof', wire)
                conn.commit()
                return seq
            seq = await store.submit(add_event)
            proof = {'to_stream_id': TARGET, 'proof_generation': 'gen',
                     'proof_session_created_at': BIRTH, 'proof_watermark': seq - 1,
                     'proof_watermark_state': 'reachable', 'proof_event_id': seq,
                     'wire_text': wire, 'wire_length': len(wire),
                     'wire_sha256': hashlib.sha256(wire.encode()).hexdigest(),
                     'sender_kind': 'operator', 'sender_verified': True,
                     'from_stream_id': 'fixture:sender', 'sender_generation': 'sender-gen'}
            await store.append_send_receipt(
                to_stream_id=TARGET, request_id='archived-proof', receipt_id='receipt-proof',
                state='landed', wire_text=wire, display_text=wire, attachments=[],
                delivery='landed', submission_confirmed=True, created_at=BIRTH,
                meta_json=json.dumps({'_input_proof': proof}),
            )

            def move(conn):
                initialize_history_pair(conn)
                source = conn.execute('SELECT rowid,* FROM v2_send_receipts').fetchone()
                columns = ['receipt_rowid'] + [d[1] for d in conn.execute('PRAGMA table_info(v2_send_receipts)')]
                insert = ("INSERT INTO v2_send_receipts(" + ",".join(columns) + ") VALUES(" +
                          ",".join('?' for _ in columns) + ")")
                with closing(sqlite3.connect(conn.history_archive_path)) as archive, archive:
                    archive.execute(insert, tuple(source))
                conn.execute('DELETE FROM v2_send_receipts')
                conn.commit()
                return conn.history_archive_path, insert, columns, tuple(source)

            archive, insert, columns, source = await store.submit(move)
            def origin(rows):
                return next(r for r in rows if r['daemon_seq'] == seq)['raw']['input_provenance']

            _, rows = await projection_pair(store, monkeypatch)
            assert origin(rows) == {'kind': 'operator', 'verified': True,
                                    'sender': 'fixture:sender', 'generation': 'sender-gen'}
            # Beyond 512 matching receipts, the legacy proof path fails closed.
            with closing(sqlite3.connect(archive)) as cold, cold:
                for i in range(2, 514):
                    record = list(source)
                    record[0] = i
                    record[columns.index('receipt_id')] = f'overflow-{i}'
                    record[columns.index('request_id')] = f'overflow-{i}'
                    cold.execute(insert, record)
            _, overflow = await projection_pair(store, monkeypatch)
            assert origin(overflow) == {'kind': 'unknown', 'verified': False}
        finally:
            store.stop()
    asyncio.run(run())
