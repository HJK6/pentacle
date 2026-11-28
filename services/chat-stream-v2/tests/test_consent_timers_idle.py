"""Consent maintenance idle probes and their existing positive paths."""
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import time

import pytest
from _shared import operator_auth
from test_consent_host_offers import setup
from test_consent_push import PROJECT, fields
from test_lifecycle_authority import scenario


class RegistryProbe:
    def __init__(self, monkeypatch, conn, registry=None, *, db_path=None):
        self.path = registry.path if registry else Path('credentials.json')
        self.registry = registry
        self.locks = self.loads = 0
        self.transactions = []
        original = operator_auth.file_lock

        @contextmanager
        def lock(path):
            self.locks += 1
            with original(path):
                self.transactions.append(conn.in_transaction)
                if db_path is not None:
                    # A live read cursor can hold a WAL snapshot while in_transaction is false.
                    with closing(sqlite3.connect(db_path, timeout=0)) as reader:
                        assert reader.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
                        busy, _, _ = reader.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                        assert busy == 0
                yield
        monkeypatch.setattr(operator_auth, 'file_lock', lock)

    def load(self):
        self.loads += 1
        return self.registry.load() if self.registry else SimpleNamespace(credentials={})


async def probe(env, monkeypatch, registry=None, *, db_path=None):
    conn = await env.store.submit(lambda conn: conn)
    return RegistryProbe(monkeypatch, conn, registry, db_path=db_path)


@pytest.mark.parametrize('timer', ['expire', 'push'])
def test_empty_timers_skip_registry(tmp_path, monkeypatch, timer):
    async def check(env):
        registry = await probe(env, monkeypatch)
        registry.path = tmp_path / 'credentials.json'
        result = await (env.store.consent_expire(registry) if timer == 'expire' else env.store.consent_push_due(registry, 'assistant'))
        assert result == []
        assert (registry.locks, registry.loads) == (0, 0)
    scenario(check)


@pytest.mark.parametrize('kind', ['offer', 'intent'])
def test_revoked_pending_is_superseded(tmp_path, monkeypatch, kind):
    async def check(env):
        a, _ = await setup(env, tmp_path)
        if kind == 'offer':
            row = await a.offer()
            table, key = 'v2_consent_offers', row['offer_id']
        else:
            await a.enroll(); await env.open('assistant', role='lead')
            row = (await a.call('consent.request', action='lifecycle.designate', target_stream_id='node-a:assistant',
                target_generation=await env.gen('assistant'), expected_revision=0, reason='revocation'))['intent']
            table, key = 'v2_consent_intents', row['request_id']
        assert row['expires_at'] > time.time()
        a.registry.revoke(a.cid)
        registry = await probe(env, monkeypatch, a.registry)
        await env.store.consent_expire(registry, env.sessions.assistant.role)
        state = await env.store.submit(lambda conn: conn.execute(f'SELECT state FROM {table} WHERE data LIKE ?', ('%' + key + '%',)).fetchone()[0])
        assert state == 'superseded'
        assert (registry.locks, registry.loads, registry.transactions) == (1, 1, [False])
    scenario(check)


@pytest.mark.parametrize('kind', ['offer', 'intent', 'challenge', 'push'])
def test_deadline_equal_now(tmp_path, monkeypatch, kind):
    db_path = tmp_path / 'consent.sqlite3'

    async def check(env):
        a, _ = await setup(env, tmp_path)
        if kind == 'offer' or kind == 'push':
            row = await a.offer()
            table, keycol, key = 'v2_consent_offers', 'offer_id', row['offer_id']
        else:
            await a.enroll(); await env.open('assistant', role='lead')
            row = (await a.call('consent.request', action='lifecycle.designate', target_stream_id='node-a:assistant',
                target_generation=await env.gen('assistant'), expected_revision=0, reason='deadline'))['intent']
            table, keycol, key = 'v2_consent_intents', 'request_id', row['request_id']
            if kind == 'challenge':
                challenge = (await a.call('consent.open', intent_id=key, key_id=a.key_id))['challenge']
                await env.store.submit(lambda conn: (conn.execute("UPDATE v2_consent_intents SET state='accepted' WHERE request_id=?", (key,)), conn.commit()))
                table, keycol, key = 'v2_consent_challenges', 'challenge_id', challenge['challenge_id']
        # An integer survives SQLite's JSON number formatting exactly.
        now = int(time.time())
        def set_deadline(conn):
            if kind == 'push':
                conn.execute("UPDATE v2_consent_push_jobs SET next_at=? WHERE request_id=?", (now, key))
            elif kind == 'challenge':
                conn.execute(f'UPDATE {table} SET expires_at=? WHERE {keycol}=?', (now, key))
            else:
                conn.execute(f'UPDATE {table} SET expires_at=?, data=json_set(data,\'$.expires_at\',?) WHERE {keycol}=?', (now, now, key))
            conn.commit()
        await env.store.submit(set_deadline)
        registry = await probe(env, monkeypatch, a.registry, db_path=db_path)
        run_timer = env.store.consent_push_due if kind == 'push' else env.store.consent_expire
        with monkeypatch.context() as clock:
            clock.setattr(time, 'time', lambda: now)
            await run_timer(registry, env.sessions.assistant.role)
        if kind == 'push':
            state = await env.store.submit(lambda conn: conn.execute('SELECT state FROM v2_consent_push_jobs WHERE request_id=?', (key,)).fetchone()[0])
            assert state == 'unavailable'
        else:
            state = await env.store.submit(lambda conn: conn.execute(f'SELECT state FROM {table} WHERE {keycol}=?', (key,)).fetchone()[0])
            assert state == 'expired'
        assert (registry.locks, registry.loads, registry.transactions) == (1, 1, [False])
    scenario(check, path=str(db_path))


@pytest.mark.parametrize('state, terminal', [
    pytest.param('retry', False, id='retry'),
    pytest.param('retry', True, id='terminal-retry'),
    pytest.param('receipt', True, id='receipt'),
])
def test_retry_and_terminal_receipt_are_taken_and_finished(tmp_path, monkeypatch, state, terminal):
    monkeypatch.setenv('EXPO_PROJECT_ID', PROJECT)
    async def check(env):
        a, _ = await setup(env, tmp_path)
        await a.call('consent.push_register', **fields('ExpoPushToken[one]'))
        offer = await a.offer()
        job = (await env.store.consent_push_due(a.registry, env.sessions.assistant.role))[0]
        await env.store.consent_push_finish(job, {'ticket_id': 'ticket'} if state == 'receipt' else {'transient': True})
        if terminal:
            parent = (await a.call('consent_key.decline', offer_id=offer['offer_id']))['offer']
            assert parent['state'] == 'declined'
        await env.store.submit(lambda conn: (conn.execute('UPDATE v2_consent_push_jobs SET next_at=0'), conn.commit()))
        registry = await probe(env, monkeypatch, a.registry)
        jobs = await env.store.consent_push_due(registry, env.sessions.assistant.role)
        if state == 'retry' and terminal:
            assert jobs == []
            expected = ('unavailable', 'request_terminal', 1)
        else:
            assert len(jobs) == 1 and jobs[0]['state'] == state and jobs[0]['attempts'] == (1 if state == 'receipt' else 2)
            await env.store.consent_push_finish(jobs[0], {'receipt_ok': True} if state == 'receipt' else {'ticket_id': 'ticket'})
            expected = ('delivered', 'receipt_ok', 1) if state == 'receipt' else ('receipt', 'accepted', 2)
        saved = await env.store.submit(lambda conn: conn.execute(
            'SELECT state, outcome, attempts FROM v2_consent_push_jobs WHERE request_id=?', (offer['offer_id'],)).fetchone())
        assert tuple(saved) == expected
        assert (registry.locks, registry.loads, registry.transactions) == (1, 1, [False])
    scenario(check)
