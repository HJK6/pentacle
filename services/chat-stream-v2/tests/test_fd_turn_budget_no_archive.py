"""Generated-file regression evidence: observer admission before preparation."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from test_fd_turn_budget import budget_store, normalized, _identity_key, peer_wire, peer_receipt
from store import SESSION_EVENT_TAIL_DDL


def archive_probe(store, monkeypatch):
    archive = Path(store.path).with_name('sessions_archive.db')
    with sqlite3.connect(archive) as conn:
        conn.execute(SESSION_EVENT_TAIL_DDL)
        conn.execute("INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,recorded_at,identity) VALUES ('generated:other','generated','generated','{}',0,'generated')")
    opens, queries, handoffs = [], [], []
    real_connect, real_thread = sqlite3.connect, asyncio.to_thread
    def connect(path, *args, **kwargs):
        conn = real_connect(path, *args, **kwargs)
        if archive.name in str(path):
            opens.append(str(path)); conn.set_trace_callback(queries.append)
        return conn
    async def to_thread(fn, *args, **kwargs):
        if getattr(fn, '__name__', '') == '_fd_archive_evidence':
            handoffs.append(fn.__name__)
        return await real_thread(fn, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    monkeypatch.setattr(asyncio, 'to_thread', to_thread)
    return opens, queries, handoffs


async def admit(store, item, batch=False, twice=False):
    entry = {'stream_id':'fixture:fd','event':item,'identity':_identity_key(item)}
    if batch:
        session = await store.fetch_session('fixture','fd')
        entry['lifecycle'] = {'pane_pid':'123','session_created_at':session['created_at'],'generation':session['session_generation']}
        return await store.append_session_events_lifecycle_cas([entry, entry] if twice else [entry], limit=100)
    return await store.append_session_event('fixture:fd', item, identity=entry['identity'], limit=100)


@pytest.mark.asyncio
@pytest.mark.parametrize('batch', [False, True])
async def test_ingest_never_opens_archive_including_batch_duplicates(budget_store, monkeypatch, batch):
    store, _ = budget_store
    probes = archive_probe(store, monkeypatch)
    for n in range(8):
        item = normalized('USER' if n == 0 else 'TOOL_USE', n)
        await admit(store, item, batch=batch, twice=batch)
        await admit(store, item, batch=batch)
    assert probes == ([], [], [])


@pytest.mark.asyncio
@pytest.mark.parametrize('batch', [False, True])
async def test_ignored_insert_has_no_observer_preparation_or_cursor_write(budget_store, monkeypatch, batch):
    store, _ = budget_store
    archive_probe(store, monkeypatch)
    item = normalized('USER', 0)
    await admit(store, item, batch=batch)
    before = await store.get(store._fd_cursor_key('fixture:fd'))
    calls = []
    original = store._fd_prepare
    def prepare(*args, **kwargs):
        calls.append('prepare'); return original(*args, **kwargs)
    monkeypatch.setattr(store, '_fd_prepare', prepare)
    observer = store._fd_observe_conn
    def observe(*args, **kwargs):
        calls.append('observe'); return observer(*args, **kwargs)
    monkeypatch.setattr(store, '_fd_observe_conn', observe)
    statements = []
    await store.submit(lambda c: c.set_trace_callback(statements.append))
    try:
        await admit(store, item, batch=batch, twice=batch)
    finally:
        await store.submit(lambda c: c.set_trace_callback(None))
    assert calls == []
    assert not any('kv' in sql.lower() for sql in statements)
    assert await store.get(store._fd_cursor_key('fixture:fd')) == before


@pytest.mark.asyncio
async def test_saved_permanent_gap_skips_all_history_on_later_ingest(budget_store, monkeypatch):
    store, _ = budget_store
    probes = archive_probe(store, monkeypatch)
    await admit(store, normalized('TOOL_USE', 0))
    state = await store.fd_turn_budget_status('fixture:fd')
    assert state['continuity_gap']
    probes[0].clear(); probes[1].clear(); probes[2].clear()
    statements = []
    await store.submit(lambda c: c.set_trace_callback(statements.append))
    try:
        for n in range(1,30):
            await admit(store, normalized('TOOL_USE', n), batch=bool(n % 2))
    finally:
        await store.submit(lambda c: c.set_trace_callback(None))
    assert probes == ([], [], [])
    # INSERT's duplicate index probes are SQLite-internal. Observer SELECTs
    # against historical source rows are not needed once coverage is unknown.
    assert not any(sql.lstrip().upper().startswith('SELECT') and 'session_event_tail' in sql for sql in statements)
    assert await store.submit(lambda c:c.execute('SELECT count(*) FROM session_event_tail').fetchone()[0]) == 30


@pytest.mark.asyncio
async def test_receipt_replay_does_not_prepare_or_rewrite_cursor(budget_store, monkeypatch):
    store, _ = budget_store
    await admit(store, normalized('USER', 0, body=peer_wire()))
    await peer_receipt(store)
    before = await store.get(store._fd_cursor_key('fixture:fd'))
    calls = []
    original = store._fd_prepare
    def prepare(*args, **kwargs):
        calls.append('prepare'); return original(*args, **kwargs)
    monkeypatch.setattr(store, '_fd_prepare', prepare)
    observer = store._fd_observe_conn
    def observe(*args, **kwargs):
        calls.append('observe'); return observer(*args, **kwargs)
    monkeypatch.setattr(store, '_fd_observe_conn', observe)
    statements = []
    await store.submit(lambda c: c.set_trace_callback(statements.append))
    try:
        await admit(store, normalized('USER', 0, body=peer_wire()))
        await peer_receipt(store)
    finally:
        await store.submit(lambda c: c.set_trace_callback(None))
    assert calls == []
    assert not any(('INSERT' in sql.upper() or 'UPDATE' in sql.upper()) and 'kv' in sql.lower() for sql in statements)
    assert await store.get(store._fd_cursor_key('fixture:fd')) == before


@pytest.mark.asyncio
async def test_generation_change_cannot_override_same_lifecycle_hot_dedup(budget_store, monkeypatch):
    from fd_turn_budget import FdTurnBudgetConfig
    from test_fd_turn_budget import counts
    store, old_binding = budget_store
    for n in range(7):
        await admit(store, normalized('USER' if n == 0 else 'TOOL_USE', n))
    assert await counts(store) == (1, 1)
    old = await store.fetch_session('fixture', 'fd')
    new = await store.open_session('fixture', 'fd', provider='claude', pane_status='pane_alive',
                                  pane_pid='123', created_at=old['created_at'])
    binding = {'stream_id':'fixture:fd', 'generation':new['session_generation']}
    assert binding['generation'] != old_binding['generation']
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
    def forbidden(*args, **kwargs):
        raise AssertionError('ignored new-generation replay performed observer work')
    original = store._fd_prepare_conn
    monkeypatch.setattr(store, '_fd_prepare_conn', forbidden)
    monkeypatch.setattr(store, '_fd_archive_evidence', forbidden)
    for n in range(7):
        assert await admit(store, normalized('USER' if n == 0 else 'TOOL_USE', n)) is None
    state = await store.fd_turn_budget_status('fixture:fd')
    assert state['generation'] == binding['generation'] and state['ordinal'] == 0
    assert 'recovery' not in state and await counts(store) == (1, 1)
    monkeypatch.setattr(store, '_fd_prepare_conn', original)
    assert await admit(store, normalized('USER', 20))
    state = await store.fd_turn_budget_status('fixture:fd')
    assert state['ordinal'] == 0 and state['continuity_gap'] == 'ordinal_history_unproven'
    assert 'recovery' not in state and await counts(store) == (1, 1)


@pytest.mark.asyncio
async def test_harmless_work_has_no_generation_length_cutoff(budget_store):
    from test_fd_turn_budget import counts
    store, _ = budget_store
    for n in range(7):
        await admit(store, normalized('USER' if n == 0 else 'TOOL_USE', n))
    for n in range(7,145):
        item = normalized('TOOL_USE', n)
        if n % 3 == 0:
            item['kind'] = 'SYSTEM'
        elif n % 3 == 1:
            item['kind'] = 'THINKING'
        await admit(store, item)
    state = await store.fd_turn_budget_status('fixture:fd')
    assert state['ordinal'] == 1 and state['crossed'] and not state['continuity_gap']
    assert state['history_truncated'] and len(state['history_proofs']) == 128
    assert await counts(store) == (1,1)
    await admit(store, normalized('USER', 145))
    state = await store.fd_turn_budget_status('fixture:fd')
    assert state['ordinal'] == 1 and state['continuity_gap'] == 'dispatch_proof_delta_exceeds_bound'
    assert await counts(store) == (1,1)


@pytest.mark.asyncio
async def test_memory_store_recovery_is_spent_unknown():
    from fd_turn_budget import FdTurnBudgetConfig
    from store import Store
    from test_fd_turn_budget import counts
    store = Store(':memory:'); store.start()
    try:
        row = await store.open_session('fixture', 'fd', provider='claude', pane_status='pane_alive')
        store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True),
            env_binding={'stream_id':'fixture:fd', 'generation':row['session_generation']})
        await admit(store, normalized('USER', 0, body=peer_wire()))
        await peer_receipt(store)
        state = await store.fd_turn_budget_status('fixture:fd')
        assert state['continuity_gap'] == 'recovery_unavailable'
        assert state['recovery']['spent'] == 100000 and state['recovery']['status'] == 'failed'
        assert state['ordinal'] == 0 and await counts(store) == (0,0)
    finally:
        store.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('batch', [False, True])
async def test_rebind_waits_until_admitted_source_and_observer_commit(budget_store, monkeypatch, batch):
    import threading
    from fd_turn_budget import FdTurnBudgetConfig
    from test_fd_turn_budget import counts
    store, _ = budget_store
    for n in range(6):
        await admit(store, normalized('USER' if n == 0 else 'TOOL_USE', n), batch=batch)
    reached, release, configured, attempting = (threading.Event() for _ in range(4))
    original = store._recorded_by_other_lifecycle_conn
    def paused(*args, **kwargs):
        reached.set()
        assert release.wait(2), 'test failed to release Store'
        return original(*args, **kwargs)
    monkeypatch.setattr(store, '_recorded_by_other_lifecycle_conn', paused)
    task = asyncio.create_task(admit(store, normalized('TOOL_USE', 6), batch=batch))
    config_task = None
    try:
        while not reached.is_set():
            await asyncio.sleep(0)
        def rebind():
            attempting.set()
            store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True),
                env_binding={'stream_id':'fixture:elsewhere', 'generation':'replacement'})
            configured.set()
        config_task = asyncio.create_task(asyncio.to_thread(rebind))
        while not attempting.is_set():
            await asyncio.sleep(0)
        # The rejected candidate returned from configure here while the old
        # observer effects were still uncommitted. The repaired fence serializes
        # reconfiguration after the entire Store transaction instead.
        await asyncio.sleep(0.01)
        assert not configured.is_set()
        release.set()
        await task
        await config_task
        assert configured.is_set()
        assert await counts(store) == (1, 1)
        notice = await store.submit(lambda c:dict(c.execute("SELECT * FROM v2_outbound_notices WHERE kind='fd_turn_budget'").fetchone()))
        assert not await store.fd_turn_budget_notice_valid(notice)
    finally:
        release.set()
        await task
        if config_task:
            await config_task
