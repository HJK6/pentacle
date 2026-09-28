"""Actual sender serialization/receipts with local HTTP, real credential/Store admission."""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import consent_push
import store_consent_push as push
from test_consent_host_offers import setup, refuses
from test_lifecycle_authority import scenario

PROJECT = '27b688f0-fde7-4755-8cd3-29fb8e6e2637'


def fields(token):
    return dict(push_token=token, project_id=PROJECT, platform='ios', environment='production')


def test_registration_rotation_refuses_token_takeover_and_removal_is_own_only(tmp_path, monkeypatch):
    monkeypatch.setenv('EXPO_PROJECT_ID', PROJECT)
    async def check(env):
        a, b = await setup(env, tmp_path)
        await a.call('consent.push_register', **fields('ExpoPushToken[one]'))
        await refuses(b.call('consent.push_register', **fields('ExpoPushToken[one]')), 'consent_push_token_bound')
        await b.call('consent.push_register', **fields('ExpoPushToken[two]'))
        await a.call('consent.push_register', **fields('ExpoPushToken[rotated]'))
        await b.call('consent.push_unregister', credential_id=a.cid)
        rows = await env.store.submit(lambda conn: conn.execute('SELECT credential_id,token FROM v2_consent_push_registrations').fetchall())
        assert [tuple(r) for r in rows] == [(a.cid, 'ExpoPushToken[rotated]')]
    scenario(check)


def test_real_sender_payload_is_opaque_and_provider_receipt_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv('EXPO_PROJECT_ID', PROJECT)
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.path, data))
            result = {'data': {'status': 'ok', 'id': 'test-ticket'}} if self.path == '/send' else {'data': {'test-ticket': {'status': 'ok'}}}
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(consent_push, 'SEND_URL', f'http://127.0.0.1:{server.server_port}/send')
    monkeypatch.setattr(consent_push, 'RECEIPTS_URL', f'http://127.0.0.1:{server.server_port}/receipts')
    try:
        async def check(env):
            a, _ = await setup(env, tmp_path)
            await a.call('consent.push_register', **fields('ExpoPushToken[one]'))
            offer = await a.offer()
            async with env.sessions.assistant.authority_lock:
                jobs = await env.store.consent_push_due(a.registry, env.sessions.assistant.role)
            assert len(jobs) == 1
            outcome = await consent_push.send(jobs[0])
            assert outcome['ticket_id'] == 'test-ticket'
            await env.store.consent_push_finish(jobs[0], outcome)
            def advance(conn):
                conn.execute('UPDATE v2_consent_push_jobs SET next_at=0'); conn.commit()
            await env.store.submit(advance)
            async with env.sessions.assistant.authority_lock:
                receipts = await env.store.consent_push_due(a.registry, env.sessions.assistant.role)
            outcome = await consent_push.send(receipts[0])
            assert outcome['receipt_ok']
            await env.store.consent_push_finish(receipts[0], outcome)
            assert not await env.store.consent_push_due(a.registry, env.sessions.assistant.role)  # reconnect cannot dispatch again
            status = (await a.call('consent_key.host_status', {'peer_loopback': True, 'local_admin_verified': True}, offer_id=offer['offer_id']))['delivery']
            assert status[0]['state'] == 'delivered'
            data = requests[0][1]['data']
            assert data == {'kind': 'enrollment', 'host_id': 'node-a', 'request_id': offer['offer_id']}
            assert not any(secret in json.dumps(requests) for secret in ['signature','nonce','spki','credential_id'])
        scenario(check)
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


@pytest.mark.parametrize('reason', ['revoked', 'terminal', 'invalid_token', 'transient'])
def test_dispatch_rechecks_credentials_and_terminal_state_and_bounds_failures(tmp_path, monkeypatch, reason):
    monkeypatch.setenv('EXPO_PROJECT_ID', PROJECT)
    async def check(env):
        a, b = await setup(env, tmp_path)
        await a.call('consent.push_register', **fields('ExpoPushToken[one]'))
        await b.call('consent.push_register', **fields('ExpoPushToken[two]'))
        offer = await a.offer()
        if reason == 'revoked': a.registry.revoke(a.cid)
        if reason == 'terminal': await a.call('consent_key.decline', offer_id=offer['offer_id'])
        jobs = await env.store.consent_push_due(a.registry, env.sessions.assistant.role)
        if reason in {'revoked', 'terminal'}:
            assert not jobs; return
        if reason == 'invalid_token':
            await env.store.consent_push_finish(jobs[0], {'code': 'DeviceNotRegistered'})
            rows = await env.store.submit(lambda conn: conn.execute('SELECT credential_id FROM v2_consent_push_registrations').fetchall())
            assert [r[0] for r in rows] == [b.cid]
        else:
            for _ in range(3):
                await env.store.consent_push_finish(jobs[0], {'code': 'transport_unavailable', 'transient': True})
                await env.store.submit(lambda conn: (conn.execute('UPDATE v2_consent_push_jobs SET next_at=0'), conn.commit()))
                jobs = await env.store.consent_push_due(a.registry, env.sessions.assistant.role)
                if not jobs: break
            assert not jobs
    scenario(check)


def test_receipt_is_delayed_and_can_complete_after_terminal_and_token_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv('EXPO_PROJECT_ID', PROJECT)
    async def check(env):
        a, _ = await setup(env, tmp_path)
        await a.call('consent.push_register', **fields('ExpoPushToken[old]'))
        offer = await a.offer()
        job = (await env.store.consent_push_due(a.registry, env.sessions.assistant.role))[0]
        before = time.time()
        await env.store.consent_push_finish(job, {'ticket_id': 'accepted-ticket'})
        row = await env.store.submit(lambda conn: conn.execute('SELECT next_at FROM v2_consent_push_jobs').fetchone())
        assert row[0] >= before + 899  # Expo recommends allowing fifteen minutes for receipts.
        await a.call('consent_key.decline', offer_id=offer['offer_id'])
        await a.call('consent.push_register', **fields('ExpoPushToken[new]'))
        await env.store.submit(lambda conn: (conn.execute('UPDATE v2_consent_push_jobs SET next_at=0'), conn.commit()))
        receipt = (await env.store.consent_push_due(a.registry, env.sessions.assistant.role))[0]
        assert receipt['state'] == 'receipt' and receipt['token'] == 'ExpoPushToken[old]'
        await env.store.consent_push_finish(receipt, {'code': 'DeviceNotRegistered'})
        token = await env.store.submit(lambda conn: conn.execute('SELECT token FROM v2_consent_push_registrations').fetchone()[0])
        assert token == 'ExpoPushToken[new]'
    scenario(check)


def test_crash_recovery_has_a_durable_send_bound(tmp_path, monkeypatch):
    monkeypatch.setenv('EXPO_PROJECT_ID', PROJECT)
    async def check(env):
        a, _ = await setup(env, tmp_path)
        await a.call('consent.push_register', **fields('ExpoPushToken[one]'))
        await a.offer()
        for _ in range(push.MAX_ATTEMPTS):
            assert len(await env.store.consent_push_due(a.registry, env.sessions.assistant.role)) == 1
            # Simulate process loss after admission: no finish callback.
            await env.store.submit(lambda conn: (conn.execute('UPDATE v2_consent_push_jobs SET next_at=0'), conn.commit()))
        assert not await env.store.consent_push_due(a.registry, env.sessions.assistant.role)
    scenario(check)
