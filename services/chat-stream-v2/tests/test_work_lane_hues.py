"""One shipped palette, atomic open-set hue identity and bounded repairs."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from _shared.lane_hues import LANE_HUE_KEYS, resolve_hue
from store_lane_hues import ensure_lane_hues_schema, enter_open_conn, hue_transaction, prepare_hues_conn, repair_hues_conn
from test_work_lanes import run


def connection(path=':memory:'):
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def legacy_schema(conn):
    conn.execute('CREATE TABLE v2_assistant_composite_lanes (lane_id TEXT PRIMARY KEY,work_state TEXT,created_at TEXT,first_admitted_at TEXT)')
    conn.execute('CREATE TABLE v2_work_lane_events (lane_id TEXT,prior_state TEXT,next_state TEXT,created_at TEXT)')


def legacy(conn, identity, when, state='paused'):
    conn.execute('INSERT INTO v2_assistant_composite_lanes VALUES (?,?,?,?)', (identity, state, when, when))


def new_lane(conn, identity, state='paused'):
    conn.execute('INSERT INTO v2_assistant_composite_lanes(lane_id,work_state,created_at) VALUES (?,?,?)',
                 (identity, state, '2026-11-08T00:00:00Z'))
    enter_open_conn(conn, identity)
    prepare_hues_conn(conn, [identity])


def hues(conn):
    return {row['lane_id']: row['hue'] for row in conn.execute('SELECT lane_id,hue FROM v2_assistant_composite_lanes')}


def test_palette_bytes_keys_order_and_invalid_legacy():
    path = Path(__file__).resolve().parents[3] / 'pentacle-chat-core/lane_hues.json'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == 'ddb7671dd5e6b16592dfa63a359adc23b0a7d1dfa0a26c04ffbaa485eaa78ca5'
    assert tuple(cell['key'] for cell in json.loads(path.read_text())['palette']) == LANE_HUE_KEYS
    assert len(set(LANE_HUE_KEYS)) == 16
    assert resolve_hue('chartreuse') == 'chartreuse'
    for value in (None, 'unknown', [], 7):
        assert resolve_hue(value) is None


def test_all_historical_order_precedes_new_entries_regardless_of_projection_batch():
    conn = connection()
    legacy_schema(conn)
    legacy(conn, 'wl-old', '2026-01-01T00:00:00Z')
    legacy(conn, 'wl-older-done', '2025-01-01T00:00:00Z', 'done')
    ensure_lane_hues_schema(conn)
    for i in range(16):
        with hue_transaction(conn):
            new_lane(conn, f'wl-new{i:02}')
    # Reading an old uncoloured lane MUST NOT make it the latest entrant.
    prepare_hues_conn(conn, ['wl-old'])
    assert hues(conn)['wl-old'] == hues(conn)['wl-new00'] == 'violet'
    with hue_transaction(conn):
        conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='done' WHERE lane_id='wl-new01'")
        repair_hues_conn(conn)
    assert hues(conn)['wl-old'] == 'violet'
    assert hues(conn)['wl-new00'] == 'cyan'
    older_order = conn.execute("SELECT hue_open_order FROM v2_assistant_composite_lanes WHERE lane_id='wl-old'").fetchone()[0]
    prepare_hues_conn(conn, ['wl-older-done'])
    assert conn.execute("SELECT hue_open_order FROM v2_assistant_composite_lanes WHERE lane_id='wl-old'").fetchone()[0] == older_order
    conn.close()


def test_historical_reopen_order_is_global_and_migration_failure_is_atomic():
    conn = connection();legacy_schema(conn)
    legacy(conn, 'wl-a', '2026-01-01T00:00:00Z');legacy(conn, 'wl-b', '2026-02-01T00:00:00Z')
    conn.execute("INSERT INTO v2_work_lane_events VALUES ('wl-a','done','paused','2026-03-01T00:00:00Z')")
    ensure_lane_hues_schema(conn)
    assert [row[0] for row in conn.execute('SELECT lane_id FROM v2_assistant_composite_lanes ORDER BY hue_open_order')] == ['wl-b', 'wl-a']
    prepare_hues_conn(conn, ['wl-a']);prepare_hues_conn(conn, ['wl-b'])
    assert [row[0] for row in conn.execute('SELECT lane_id FROM v2_assistant_composite_lanes ORDER BY hue_open_order')] == ['wl-b', 'wl-a']
    bad = connection();legacy_schema(bad);legacy(bad, 'wl-bad', 'invalid timestamp')
    with pytest.raises(ValueError, match='history_invalid'):
        ensure_lane_hues_schema(bad)
    assert not {'hue', 'hue_open_order'} & {row[1] for row in bad.execute('PRAGMA table_info(v2_assistant_composite_lanes)')}
    conn.close();bad.close()


def test_store_creation_17_to_16_closure_and_reopen_repair_without_extra_progress_events():
    async def body(env):
        lanes = [(await env.adopt(key=f'request:hue{i}', state='paused', owner='operator'))['lane'] for i in range(17)]
        assert [lane['hue'] for lane in lanes[:16]] == list(LANE_HUE_KEYS)
        assert lanes[16]['hue'] == 'violet'
        closed = await env.op('set_state', {'to': 'done', 'outcome': 'Fixture complete', 'reason': 'Fixture lifecycle'},
                              lane=lanes[1]['lane_id'], version=1)
        last = await env.store.get_work_lane(lanes[16]['lane_id'])
        assert last['lane']['hue'] == 'cyan' and last['lane']['version'] == 1
        assert last['lane']['updated_at'] == lanes[16]['updated_at']
        assert len(last['events']) == 1 and last['events'][0]['operation'] == 'adopt'
        before = (await env.store.get_work_lane(lanes[0]['lane_id']))['lane']
        assert before['hue'] == 'violet'
        assert closed['lane']['hue'] == 'cyan'
        for field in ('hue', 'hue_open_order'):
            with pytest.raises(ValueError, match='payload_invalid'):
                await env.op('set_text', {field: 'rose'}, lane=before['lane_id'], version=before['version'])
            with pytest.raises(ValueError, match='payload_invalid'):
                await env.adopt(key='request:manual-' + field, state='paused', owner='operator', **{field: 'rose'})
    run(body)


def test_actual_reopen_moves_the_recent_entrant_only_and_fault_rolls_back():
    async def body(env):
        first = (await env.adopt(key='request:first', state='paused', owner='operator'))['lane']
        second = (await env.adopt(key='request:second', state='paused', owner='operator'))['lane']
        closed = await env.op('set_state', {'to': 'done', 'outcome': 'Fixture complete', 'reason': 'Fixture lifecycle'},
                              lane=first['lane_id'], version=1)
        third = (await env.adopt(key='request:third', state='paused', owner='operator'))['lane']
        assert third['hue'] == first['hue'] == 'violet'
        def fail(_):
            raise RuntimeError('synthetic post-update fault')
        env.store._work_lane_fault = fail
        with pytest.raises(RuntimeError, match='post-update fault'):
            await env.op('set_state', {'to': 'paused', 'reason': 'Fixture reopen'}, lane=first['lane_id'], version=2)
        env.store._work_lane_fault = None
        assert (await env.store.get_work_lane(first['lane_id']))['lane']['work_state'] == 'done'
        reopened = await env.op('set_state', {'to': 'paused', 'reason': 'Fixture reopen'}, lane=first['lane_id'], version=2)
        assert reopened['lane']['hue'] == 'yellow'
        assert (await env.store.get_work_lane(second['lane_id']))['lane']['hue'] == 'cyan'
        assert (await env.store.get_work_lane(third['lane_id']))['lane']['hue'] == 'violet'
    run(body)


def test_two_connections_racing_final_key_and_same_lane_keep_the_winner(tmp_path):
    path = str(tmp_path / 'race.db');conn = connection(path);legacy_schema(conn);ensure_lane_hues_schema(conn)
    for i in range(15):
        with hue_transaction(conn):
            new_lane(conn, f'wl-filled{i:02}')
    with hue_transaction(conn):
        for identity in ('wl-race-a', 'wl-race-b'):
            conn.execute('INSERT INTO v2_assistant_composite_lanes(lane_id,work_state) VALUES (?,?)', (identity, 'paused'))
            enter_open_conn(conn, identity)
    def allocate(identity):
        other = connection(path)
        try:
            prepare_hues_conn(other, [identity])
            return hues(other)[identity]
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(allocate, ['wl-race-a', 'wl-race-b']))
    assert set(values) == {'chartreuse', 'violet'}
    winner = hues(conn)['wl-race-a']
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(allocate, ['wl-race-a', 'wl-race-a'])) == [winner, winner]
    assert len(set(hues(conn).values())) == 16
    conn.close()


def test_hue_and_entry_order_survive_actual_host_custody_transfer_and_restart(tmp_path):
    import asyncio
    from test_assistant_recovery_command import fixture, OLD, CHAT
    from test_host_transfer_authority import claim_bundle, prepare_bundle
    async def body():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, *_):
            old = await store.fetch_session(*OLD.split(':', 1))
            def seed(conn):
                conn.execute('INSERT INTO v2_assistant_composite_lanes '
                             '(lane_id,stream_id,phase,bound_stream_id,bound_generation,title,hue,hue_open_order,work_state,'
                             'owner_kind,created_at,updated_at,no_spec_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             ('wl-hue-transfer', CHAT, 'execution', OLD, old['session_generation'], 'Fixture', 'rose', 42,
                              'paused', 'fd', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'Synthetic transfer'))
                conn.commit()
            await store.submit(seed)
            command, *_ = await claim_bundle(store, server, gateway, composite)
            parts = await prepare_bundle(store, server, command)
            result = await server.host_transfer.commit(command)
            assert result['state'] == 'committed'
            target = next(part['target'] for part in parts if part['source'] == OLD)
            store.stop();store.start()
            shown = await store.get_work_lane('wl-hue-transfer')
            assert shown['lane']['hue'] == 'rose' and shown['lane']['bound_stream_id'] == target
            order = await store.submit(lambda conn: conn.execute(
                "SELECT hue_open_order FROM v2_assistant_composite_lanes WHERE lane_id='wl-hue-transfer'").fetchone()[0])
            assert order == 42
    asyncio.run(body())
