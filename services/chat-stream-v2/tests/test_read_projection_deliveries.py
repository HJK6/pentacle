"""Read projections amortize delivery reads without changing origin proof."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from store import Store

BIRTH = '2026-10-01T00:00:00Z'


async def seed(store, messages=4, deliveries=3000):
    def write(conn):
        for i in range(26):
            stream = f'host-a:seat-{i}'
            conn.execute("INSERT INTO sessions(host,session_name,provider,visibility,status,created_at) "
                         "VALUES('host-a',?,'codex','hidden','open',?)", (f'seat-{i}', BIRTH))
            conn.execute('INSERT INTO v2_session_generations VALUES(?,?,?)', ('host-a', f'seat-{i}', f'gen-{i}'))
            for j in range(messages):
                event = {'kind': 'USER', 'stream_id': stream, 'text': f'input-{j}', 'timestamp': BIRTH}
                conn.execute('INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at) '
                             'VALUES(?,?,?,?,?,?)', (stream, BIRTH, str(j), json.dumps(event), BIRTH, 1.0))
        conn.executemany('INSERT INTO v2_tell_deliveries(tell_id,reply,created_at) VALUES(?,?,?)',
                         ((str(i), json.dumps({'delivery': {'to_stream_id': f'host-b:seat-{i % 7}', 'text': f'other-{i}'}}), 1.0)
                          for i in range(deliveries)))
        conn.commit()
    await store.submit(write)


async def measure(store, action):
    statements = []
    rows = []
    def row_factory(cursor, row):
        if {'tell_id', 'reply'} <= {column[0] for column in cursor.description} or {column[0] for column in cursor.description} == {'reply'}:
            rows.append(1)
        return sqlite3.Row(cursor, row)
    def begin(conn):
        conn.set_trace_callback(statements.append)
        conn.row_factory = row_factory
    def end(conn):
        conn.set_trace_callback(None)
        conn.row_factory = sqlite3.Row
    await store.submit(begin)
    try:
        result = await action()
    finally:
        await store.submit(end)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT') and 'v2_tell_deliveries' in sql]
    return result, len(reads), len(rows)


@pytest.mark.parametrize('messages,deliveries', [(4, 3000), (8, 3000), (8, 6000)])
def test_inventory_reads_deliveries_once(messages, deliveries):
    async def run():
        store = Store()
        store.start()
        try:
            await seed(store, messages, deliveries)
            result, statements, rows = await measure(store, store.list_open_sessions_with_event_summary)
            assert len(result) == 26
            assert statements == 1
            assert rows == deliveries
        finally:
            store.stop()
    asyncio.run(run())


async def corpus(store):
    """Real proof anchors and independent origin expectations, in one lifecycle."""
    def write(conn):
        conn.execute("INSERT INTO sessions(host,session_name,provider,visibility,status,created_at) "
                     "VALUES('host-a','proof','codex','hidden','open',?)", (BIRTH,))
        conn.execute("INSERT INTO v2_session_generations VALUES('host-a','proof','gen')")
        events, payloads = [], {}
        cases = ['operator', 'peer', 'service', 'unknown', 'overlap', 'generation',
                 'birth', 'unrecorded', 'quoted', 'own-row']
        for case in cases:
            text = f'input {case}'
            if case in {'peer', 'unrecorded', 'own-row'}:
                text = f'[from host-b:sender] [tell:{case}]\n{text}'
            if case == 'service':
                text = '[pentacle-notice:fixture]\nservice input'
            event = {'kind': 'TELL' if case in {'peer', 'unknown', 'generation'} else 'USER',
                     'stream_id': 'host-a:proof', 'text': text, 'timestamp': BIRTH, 'raw': {'fixture': case}}
            if event['kind'] == 'TELL':
                event['raw'].update(peer_wire_text=text, sender='host-b:sender', peer_payload=case)
                event['message_envelope'] = {'kind': 'peer_delivery'}
            if case == 'quoted':
                conn.execute('INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at) '
                             'VALUES(?,?,?,?,?,?)', ('host-a:proof', BIRTH, 'unsplit', json.dumps({
                                 'kind': 'USER', 'stream_id': 'host-a:proof', 'text': 'quoted '+text+' suffix', 'timestamp': BIRTH}), BIRTH, 1.0))
            seq = conn.execute('INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at) '
                               'VALUES(?,?,?,?,?,?)', ('host-a:proof', BIRTH, case, json.dumps(event), BIRTH, 1.0)).lastrowid
            event['daemon_seq'] = seq
            events.append(event)
            if case == 'operator':
                conn.execute('INSERT INTO v2_assistant_composite_routes(route_id,stream_id,input_identity,payload_digest,'
                             'input_request_id,body,routing_state,dispatch_id,route_target,route_target_generation,route_json,created_at,updated_at) '
                             "VALUES('route','assistant:primary','identity','digest','request',?,'resolved','dispatch','host-a:proof','gen',?,?,?)",
                             (text, json.dumps({'admission_mode': 'direct_primary', 'direct_envelope': {'wire_body': text}}), BIRTH, BIRTH))
                continue
            if case == 'unknown':
                continue
            delivery = {'tell_id': case, 'to_stream_id': 'host-a:proof', 'text': text,
                        'proof_generation': 'wrong' if case == 'generation' else 'gen',
                        'proof_session_created_at': 'wrong' if case == 'birth' else BIRTH,
                        'proof_watermark': 0, 'proof_watermark_state': 'reachable',
                        'request_payload_hash': case, 'sender_kind': 'service' if case == 'service' else 'peer',
                        'sender_verified': True, 'from_stream_id': 'host-b:sender', 'sender_generation': 'sender-gen'}
            if case not in {'unrecorded', 'quoted'}:
                delivery['proof_event_id'] = seq
            payloads[case] = {'payload_digest': case, 'delivery': delivery}
            conn.execute('INSERT INTO v2_tell_deliveries VALUES(?,?,?)', (case, json.dumps(payloads[case]), 1.0))
            if case == 'overlap':
                twin = {**payloads[case], 'delivery': {**delivery, 'tell_id': 'twin'}}
                conn.execute("INSERT INTO v2_tell_deliveries VALUES('twin',?,1)", (json.dumps(twin),))
        conn.commit()
        return events, payloads
    return await store.submit(write)


def test_oracle_source_is_pinned():
    import hashlib
    from pathlib import Path
    import read_projection_legacy
    assert hashlib.sha256(Path(read_projection_legacy.__file__).read_bytes()).hexdigest() == (
        '29cc4e6c1f84051d28442d2dc094a41596569eda6fe824e7721198f30dfcef75')


# Every uncertain parser/equality class is injected at an unrelated target.
# Standard clean shapes also exercise rows the overlap proof must safely ignore.
SHAPES = [
    '{}', '{"delivery":null}', '{"delivery":{}}',
    '{"delivery":{"to_stream_id":"host-b:other","text":"other"}}',
    '{"delivery":false}', '{"delivery":0}', '{"delivery":""}', '{"delivery":[]}',
    '{"delivery":true}', '{"delivery":1}', '{"delivery":"truthy"}', '{"delivery":[1]}',
    'not json', 'null', 'false', '1', '"scalar"', '[]', '[{}]',
    '{"delivery":{"to_stream_id":"host-b:other","text":"a","text":"b"}}',
    '{"delivery":{"to_stream_id":"host-b:other","text":"other"},"nested":{"a":1,"a":2}}',
    '{"delivery":{"to_stream_id":"host-b:other","text":"a\\u0000b"}}',
    '{"delivery":{"to_stream_id":"host-b:other","text":"\\ud800"}}',
    '{"delivery":{"to_stream_id":"host-b:other\\u0000","text":"other"}}',
    '{"delivery":{"to_stream_id":"host-b:other\\udfff","text":"other"}}',
    '{"delivery":{"to_stream_id":"host-b:other","text":7}}',
    '{"delivery":{"to_stream_id":false,"text":"other"}}',
    '{"delivery":{"to_stream_id":[],"text":"other"}}',
    '{"delivery":{"to_stream_id":"host-b:other","text":{}}}',
    '{"delivery":{"to_stream_id":"host-b:other","text":"other"},"x":NaN}',
    '{"delivery":{"to_stream_id":"host-b:other","text":"other"},"x":Infinity}',
    '{"delivery":{"to_stream_id":"host-b:other","text":"other"},"x":-Infinity}',
    '{"delivery":{"to_stream_id":"host-b:other","text":"other"},}',
    '{/* comment */"delivery":{"to_stream_id":"host-b:other","text":"other"}}',
    '{delivery:{to_stream_id:"host-b:other",text:"other"}}',
    '{"delivery":{"to_stream_id":"host-b:other","text":"\\ud83d\\ude00"}}',
    '{"delivery":{"to_stream_id":"host-b:other","text":null}}',
]


@pytest.mark.parametrize('shape', [None, *SHAPES])
def test_full_ordered_oracle_equivalence(monkeypatch, shape):
    import copy
    import tell_proof
    import read_projection_legacy as legacy
    from store import _project_session_events_conn

    async def run():
        store = Store(); store.start()
        try:
            events, _ = await corpus(store)
            if shape is not None:
                await store.submit(lambda conn: (conn.execute("INSERT INTO v2_tell_deliveries VALUES('unrelated',?,1)", (shape,)), conn.commit()))
            def compare(conn):
                def outcome(fn):
                    try:
                        return ('ok', fn())
                    except Exception as error:
                        return ('error', type(error).__name__, str(error))
                # Compare complete ordered logs, including outcomes after a bad row.
                oracle = [(e['stream_id'], e['daemon_seq'], outcome(lambda e=e: legacy.input_origin(conn, e, e['daemon_seq']))) for e in events]
                table = tell_proof.ProjectionDeliveries()
                candidate = [(e['stream_id'], e['daemon_seq'], outcome(lambda e=e: tell_proof.input_origin(conn, e, e['daemon_seq'], deliveries=table))) for e in events]
                assert candidate == oracle
                original = tell_proof.input_origin
                try:
                    tell_proof.input_origin = lambda c, e, i, **kwargs: legacy.input_origin(c, e, i)
                    expected = outcome(lambda: _project_session_events_conn(conn, copy.deepcopy(events)))
                finally:
                    tell_proof.input_origin = original
                actual = outcome(lambda: _project_session_events_conn(conn, copy.deepcopy(events), deliveries=tell_proof.ProjectionDeliveries()))
                assert actual == expected
            await store.submit(compare)
        finally:
            store.stop()
    asyncio.run(run())


def test_independent_expected_origins_and_history_bounds():
    async def run():
        store = Store(); store.start()
        try:
            events, _ = await corpus(store)
            result, statements, rows = await measure(store, lambda: store.fetch_session_event_page('host-a:proof', before_daemon_seq=None, limit=100))
            expected = {'operator': {'kind': 'operator', 'verified': True, 'generation': None},
                        'peer': {'kind': 'peer', 'verified': True, 'sender': 'host-b:sender', 'generation': 'sender-gen'},
                        'service': {'kind': 'service', 'verified': True, 'sender': 'host-b:sender', 'generation': 'sender-gen'},
                        'unknown': {'kind': 'unknown', 'verified': False},
                        'overlap': {'kind': 'unknown', 'verified': False},
                        'generation': {'kind': 'unknown', 'verified': False}}
            for event in result:
                case = event.get('raw', {}).get('fixture')
                if case in expected:
                    assert event['raw']['input_provenance'] == expected[case]
            assert set(expected) <= {e.get('raw', {}).get('fixture') for e in result}
            # The service marker retains its separate indexed notice/delivery join.
            assert statements == 2 and rows == 9
            assert [e['daemon_seq'] for e in result] == sorted(e['daemon_seq'] for e in result)
            # Proof not yet recorded and own-row exclusion still grant peer origin.
            for case in ('unrecorded', 'own-row'):
                assert next(e for e in result if e.get('raw', {}).get('fixture') == case)['raw']['input_provenance']['kind'] == 'peer'
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('messages', [4, 8])
def test_history_page_reads_one_snapshot(messages):
    async def run():
        store = Store(); store.start()
        try:
            await seed(store, messages=messages)
            result, statements, rows = await measure(store, lambda: store.fetch_session_event_tail('host-a:seat-0', limit=100))
            assert len(result) == messages and statements == 1 and rows == 3000
            assert await store.submit(lambda conn: not conn.in_transaction)
        finally:
            store.stop()
    asyncio.run(run())


def test_operator_dispatch_does_not_build_deliveries():
    import tell_proof
    async def run():
        store = Store(); store.start()
        try:
            events, _ = await corpus(store)
            def prove(conn):
                statements = []
                conn.set_trace_callback(statements.append)
                try:
                    event = events[0]
                    result = tell_proof.input_origin(conn, event, event['daemon_seq'], deliveries=tell_proof.ProjectionDeliveries())
                finally:
                    conn.set_trace_callback(None)
                assert result == {'kind': 'operator', 'verified': True, 'generation': None}
                assert not any('v2_tell_deliveries' in sql for sql in statements)
            await store.submit(prove)
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('text', ['nul\x00text', '\ud800'])
def test_unclean_lookup_disables_reuse_for_later_lookups(text):
    import tell_proof
    import read_projection_legacy as legacy
    async def run():
        store = Store(); store.start()
        try:
            events, _ = await corpus(store)
            def prove(conn):
                table = tell_proof.ProjectionDeliveries()
                event = {**events[1], 'kind': 'USER', 'text': text, 'raw': {}}
                def outcome(fn):
                    try:
                        return ('ok', fn())
                    except Exception as error:
                        return ('error', type(error).__name__, str(error))
                assert outcome(lambda: tell_proof.input_origin(conn, event, event['daemon_seq'], deliveries=table)) == outcome(lambda: legacy.input_origin(conn, event, event['daemon_seq']))
                # A surrogate can fail in the earlier dispatch SQL binding.
                # Once an unclean value reaches the table boundary it poisons reuse.
                assert table.candidates(conn, 'host-a:proof', text) is None
                statements = []
                conn.set_trace_callback(statements.append)
                try:
                    follow = events[1]
                    assert tell_proof.input_origin(conn, follow, follow['daemon_seq'], deliveries=table) == legacy.input_origin(conn, follow, follow['daemon_seq'])
                finally:
                    conn.set_trace_callback(None)
                assert sum("WHERE json_extract(reply,'$.delivery.to_stream_id')" in sql for sql in statements) == 2
            await store.submit(prove)
        finally:
            store.stop()
    asyncio.run(run())


def test_projections_refresh_and_promotions_read_current_rows(monkeypatch):
    import tell_proof
    async def run():
        store = Store(); store.start()
        try:
            events, payloads = await corpus(store)
            async def page():
                return await store.fetch_session_event_tail('host-a:proof', limit=100)
            def peer_kind(result):
                return next(e for e in result if e.get('raw', {}).get('fixture') == 'peer')['raw']['input_provenance']['kind']
            assert peer_kind(await page()) == 'peer'
            changed = json.loads(json.dumps(payloads['peer']))
            changed['delivery']['proof_generation'] = 'different'
            await store.submit(lambda conn: (conn.execute("UPDATE v2_tell_deliveries SET reply=? WHERE tell_id='peer'", (json.dumps(changed),)), conn.commit()))
            assert peer_kind(await page()) == 'unknown'
            # After two read projections, a promotion uses a newly restored row.
            fresh = payloads['peer']
            fresh['reply'] = {'delivery_status': 'committed_pending_proof'}
            fresh['delivery']['delivery_status'] = 'committed_pending_proof'
            await store.submit(lambda conn: (conn.execute("UPDATE v2_tell_deliveries SET reply=? WHERE tell_id='peer'", (json.dumps(fresh),)), conn.commit()))
            calls = []
            original = tell_proof.retained_proof
            def promotion(conn, payload, **kwargs):
                calls.append(kwargs)
                return original(conn, payload, **kwargs)
            monkeypatch.setattr(tell_proof, 'retained_proof', promotion)
            promoted = await store.promote_tell_delivery('peer', 'peer', reconcile=True)
            assert promoted['reply']['delivery_status'] == 'delivered'
            assert calls == [{}]
        finally:
            store.stop()
    asyncio.run(run())


def test_raising_projection_releases_snapshot_and_call_reuse(monkeypatch):
    import gc
    import weakref
    import tell_proof
    async def run():
        store = Store(); store.start()
        references = []
        real = tell_proof.ProjectionDeliveries
        class Observed(real):
            def __init__(self):
                super().__init__()
                references.append(weakref.ref(self))
            def candidates(self, conn, target, text):
                assert conn.in_transaction
                super().candidates(conn, target, text)
                raise RuntimeError('projection fixture failure')
        monkeypatch.setattr(tell_proof, 'ProjectionDeliveries', Observed)
        try:
            await seed(store)
            for action in (store.list_open_sessions_with_event_summary,
                           lambda: store.fetch_session_event_tail('host-a:seat-0', limit=10)):
                try:
                    await action()
                except RuntimeError as error:
                    assert str(error) == 'projection fixture failure'
                    error.__traceback__ = None
                else:
                    pytest.fail('projection did not raise')
                assert await store.submit(lambda conn: not conn.in_transaction)
            # Move the worker past the raising operation before collecting frames.
            await store.submit(lambda conn: None)
            gc.collect()
            assert len(references) == 1 and references[0]() is not None
            assert await store.submit(lambda conn: conn.delivery_lookup.table.reuse is None)
        finally:
            store.stop()
        gc.collect()
        assert all(ref() is None for ref in references)
    asyncio.run(run())


def test_single_event_and_write_projection_never_receive_table(monkeypatch):
    import tell_proof
    async def run():
        store = Store(); store.start()
        try:
            events, _ = await corpus(store)
            class Forbidden:
                def __init__(self):
                    pytest.fail('single-event or write projection built a delivery snapshot')
            monkeypatch.setattr(tell_proof, 'ProjectionDeliveries', Forbidden)
            event = events[1]
            result = await store.fetch_projected_session_event('host-a:proof', event['daemon_seq'])
            assert result['raw']['input_provenance']['kind'] == 'peer'
            assert (await store.project_session_events([event]))[0]['raw']['input_provenance']['kind'] == 'peer'
        finally:
            store.stop()
    asyncio.run(run())


def alias_payload(payload, case, key_order="alias-first"):
    import copy
    value = copy.deepcopy(payload)
    delivery = value['delivery']
    if case == 'target-number':
        value['delivery']['to_stream_id'] = 7
    elif case == 'text-number':
        value['delivery']['text'] = 7
    elif case.startswith('root'):
        alias = {**delivery, 'to_stream_id': 'host-b:other'}
        value = {'delivery\x00suffix': alias, **({} if case.endswith('only') else value)}
    elif case.startswith('target'):
        value['delivery'] = {'to_stream_id\x00suffix': 'host-b:other', **delivery}
        if case.endswith('only'):
            value['delivery'].pop('to_stream_id')
    elif case.startswith('text'):
        value['delivery'] = {'text\x00suffix': 'other text', **delivery}
        if case.endswith('only'):
            value['delivery'].pop('text')
    if key_order == 'normal-first':
        for mapping in (value, value.get('delivery', {})):
            for key in list(mapping):
                if '\x00' in key:
                    item = mapping.pop(key)
                    mapping[key] = item
    return value


@pytest.mark.parametrize('case', ['root-only', 'root-both', 'target-only', 'target-both',
                                  'text-only', 'text-both', 'target-number', 'text-number'])
@pytest.mark.parametrize('position', ['before', 'after'])
@pytest.mark.parametrize('key_order', ['alias-first', 'normal-first'])
def test_agreement_rule_full_ordered_oracle(case, position, key_order):
    import copy
    import tell_proof
    import read_projection_legacy as legacy
    from store import _project_session_events_conn
    async def run():
        store = Store(); store.start()
        try:
            events, payloads = await corpus(store)
            alias = alias_payload(payloads['peer'], case, key_order)
            def compare(conn):
                # Place the changed row on either side of a valid row for this target.
                originals = list(conn.execute('SELECT tell_id,reply,created_at FROM v2_tell_deliveries'))
                normal = ('normal-other', json.dumps({'delivery': {'to_stream_id': 'host-a:proof', 'text': 'other'}}), 1.0)
                rows = [('peer', json.dumps(alias), 1.0), normal]
                if position == 'after':
                    rows.reverse()
                conn.execute('DELETE FROM v2_tell_deliveries')
                conn.executemany('INSERT INTO v2_tell_deliveries VALUES(?,?,?)', rows + [tuple(r) for r in originals if r['tell_id'] != 'peer'])
                conn.commit()
                def outcome(fn):
                    try:
                        return ('ok', fn())
                    except Exception as error:
                        return ('error', type(error).__name__, str(error))
                conn.execute('SAVEPOINT agreement_projection')
                try:
                    expected = [(e['stream_id'], e['daemon_seq'], outcome(lambda e=e: legacy.input_origin(conn, e, e['daemon_seq']))) for e in events]
                    table = tell_proof.ProjectionDeliveries()
                    actual = [(e['stream_id'], e['daemon_seq'], outcome(lambda e=e: tell_proof.input_origin(conn, e, e['daemon_seq'], deliveries=table))) for e in events]
                    assert actual == expected
                    real = tell_proof.input_origin
                    try:
                        tell_proof.input_origin = lambda c, e, i, **kwargs: legacy.input_origin(c, e, i)
                        expected_events = outcome(lambda: _project_session_events_conn(conn, copy.deepcopy(events)))
                    finally:
                        tell_proof.input_origin = real
                    actual_events = outcome(lambda: _project_session_events_conn(conn, copy.deepcopy(events), deliveries=tell_proof.ProjectionDeliveries()))
                    assert actual_events == expected_events
                    # Agreement rejection is sticky even for a later clean message.
                    statements = []
                    conn.set_trace_callback(statements.append)
                    try:
                        follow = events[-1]
                        tell_proof.input_origin(conn, follow, follow['daemon_seq'], deliveries=table)
                    finally:
                        conn.set_trace_callback(None)
                    legacy_lookup = any("WHERE json_extract(reply,'$.delivery.to_stream_id')" in sql for sql in statements)
                    # With the ordinary key first, both engines choose the same
                    # value; a harmless alias remains eligible for reuse.
                    assert legacy_lookup == (not (case.endswith('both') and key_order == 'normal-first'))
                finally:
                    conn.execute('RELEASE agreement_projection')
            await store.submit(compare)
        finally:
            store.stop()
    asyncio.run(run())


def test_nul_key_alias_independent_expected_unknown():
    async def run():
        store = Store(); store.start()
        try:
            events, payloads = await corpus(store)
            payload = alias_payload(payloads['peer'], 'target-both')
            await store.submit(lambda conn: (conn.execute("UPDATE v2_tell_deliveries SET reply=? WHERE tell_id='peer'", (json.dumps(payload),)), conn.commit()))
            projected = await store.fetch_session_event_tail('host-a:proof', limit=100)
            peer = next(e for e in projected if e.get('raw', {}).get('fixture') == 'peer')
            assert peer['raw']['input_provenance'] == {'kind': 'unknown', 'verified': False}
            assert peer['kind'] == 'USER'
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['execute', 'fetch'])
def test_build_sql_or_fetch_error_closes_cursor_then_uses_legacy(failure):
    import tell_proof
    import read_projection_legacy as legacy
    async def run():
        store = Store(); store.start()
        try:
            events, _ = await corpus(store)
            def prove(conn):
                state = {'attempts': 0, 'closed': False, 'legacy_calls': 0}
                class Cursor:
                    def __init__(self, wrapped):
                        self.wrapped = wrapped
                        self.count = 0
                    def __iter__(self):
                        return self
                    def __next__(self):
                        if self.count == 1:
                            raise sqlite3.OperationalError('injected build fetch failure')
                        self.count += 1
                        return next(self.wrapped)
                    def close(self):
                        self.wrapped.close()
                        state['closed'] = True
                class Connection:
                    def __getattr__(self, name):
                        return getattr(conn, name)
                    def execute(self, sql, params=()):
                        if sql.startswith('SELECT tell_id,reply,json_extract'):
                            state['attempts'] += 1
                            if failure == 'execute':
                                raise sqlite3.OperationalError('injected build execute failure')
                            return Cursor(conn.execute(sql, params))
                        if "WHERE json_extract(reply,'$.delivery.to_stream_id')" in sql:
                            assert failure == 'execute' or state['closed']
                            state['legacy_calls'] += 1
                        return conn.execute(sql, params)
                event = events[1]
                table = tell_proof.ProjectionDeliveries()
                expected = legacy.input_origin(conn, event, event['daemon_seq'])
                assert expected['kind'] == 'peer'
                for _ in range(2):
                    assert tell_proof.input_origin(Connection(), event, event['daemon_seq'], deliveries=table) == expected
                assert state['attempts'] == 1 and state['legacy_calls'] == 2
                assert failure == 'execute' or state['closed']
            await store.submit(prove)
        finally:
            store.stop()
    asyncio.run(run())
