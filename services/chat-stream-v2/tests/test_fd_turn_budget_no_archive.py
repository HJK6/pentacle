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
