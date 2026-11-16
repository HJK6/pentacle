"""Synthetic acquisitions principal admission, bounds, revocation and isolation."""
import asyncio
import json
import pytest
from server import Server
from store import Store
from screen_lease import ScreenLease
from test_system_notification_producer import Peer

ACTOR = 'altum-acquisitions-browser'
TOKEN = 'synthetic-acquisitions-token'
MONITOR = 'codex-reset-monitor'
MONITOR_TOKEN = 'synthetic-monitor-token'

def frame(kind, **fields):
    return {'type': kind, 'request_id': 'acq-test', 'from_stream_id': ACTOR,
            'stream_token': TOKEN, **fields}

@pytest.fixture
def credentials(monkeypatch, tmp_path):
    token = tmp_path / 'acq-token'
    token.write_text(TOKEN)
    token.chmod(0o600)
    monitor = tmp_path / 'monitor-token'
    monitor.write_text(MONITOR_TOKEN)
    monitor.chmod(0o600)
    monkeypatch.setenv('PENTACLE_ACQ_BROWSER_TOKEN_FILE', str(token))
    monkeypatch.setenv('PENTACLE_ACQ_BROWSER_HOST', 'test')
    monkeypatch.setenv('PENTACLE_CODEX_RESET_MONITOR_TOKEN_FILE', str(monitor))
    monkeypatch.setenv('PENTACLE_CODEX_RESET_MONITOR_HOST', 'test')
    return token, monitor

async def call(server, peer, kind, **fields):
    return (await server._dispatch(json.dumps(frame(kind, **fields)), websocket=peer))[0]

def test_v1_registered_rpc_only_hello_and_own_lease(credentials):
    assert ACTOR in Server._file_token_sources(), 'fixed acquisitions identity must be registered'
    async def run():
        store = Store(':memory:'); store.start()
        try:
            server = Server(); peer = Peer()
            server.handlers.update(ScreenLease(store, ['test', 'peer']).wire_handlers())
            ready = await call(server, peer, 'hello', subscribe={'snapshot': False, 'mode': 'rpc'})
            assert ready['type'] == 'ready', ready
            lease = await call(server, peer, 'screen.lease.acquire', host='test', reason='fixture')
            assert lease['type'] == 'screen.lease.acquire.ok', lease
            assert lease['holder_stream_id'] == ACTOR
            assert lease['holder_label'] == 'Altum acquisitions browser'
            assert lease['expires_at'] - lease['acquired_at'] == 120
            assert (await call(server, peer, 'screen.lease.renew', lease_id=lease['lease_id'], ttl=60))['type'].endswith('.ok')
            assert (await call(server, peer, 'screen.lease.release', lease_id=lease['lease_id']))['type'].endswith('.ok')
            assert await store.submit(lambda c: c.execute('SELECT count(*) FROM sessions').fetchone()[0]) == 0
        finally:
            store.stop()
    asyncio.run(run())

def test_v1_direct_rpc_hello_admission(credentials):
    async def run():
        response = await call(Server(), Peer(), 'hello', subscribe={'snapshot': False, 'mode': 'rpc'})
        assert response['type'] == 'ready', response
    asyncio.run(run())

@pytest.mark.parametrize('case', ['wrong', 'missing', 'empty', 'permissions', 'monitor-as-acq', 'acq-as-monitor', 'same-file', 'same-value', 'cd-value'])
def test_v1_invalid_credentials_fail_closed(credentials, monkeypatch, case):
    token, monitor = credentials
    actor, presented = ACTOR, TOKEN
    if case == 'wrong': presented = 'synthetic-wrong'
    elif case == 'missing': token.unlink()
    elif case == 'empty': token.write_text('')
    elif case == 'permissions': token.chmod(0o644)
    elif case == 'monitor-as-acq': presented = MONITOR_TOKEN
    elif case == 'acq-as-monitor': actor = MONITOR
    elif case == 'same-file': monkeypatch.setenv('PENTACLE_CODEX_RESET_MONITOR_TOKEN_FILE', str(token))
    elif case == 'same-value': monitor.write_text(TOKEN)
    elif case == 'cd-value': monkeypatch.setenv('PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE', str(token))
    async def run():
        actors = [ACTOR, MONITOR] if case in ('same-file', 'same-value') else [actor]
        for identity in actors:
            response = await call(Server(), Peer(), 'hello', from_stream_id=identity,
                                  stream_token=presented, subscribe={'snapshot': False, 'mode': 'rpc'})
            assert response['error_code'] == 'system_producer_auth_required', response
        if case == 'cd-value':
            assert Server._system_producer_token('altum-bot-cd') == TOKEN
    asyncio.run(run())

@pytest.mark.parametrize('mutation', ['remove', 'empty', 'rotate', 'permissions'])
@pytest.mark.parametrize('verb', ['acquire', 'renew', 'release'])
@pytest.mark.parametrize('include_token', [False, True])
def test_v1_bound_connection_revocation(credentials, mutation, verb, include_token):
    async def run():
        token, _ = credentials
        server, peer = Server(), Peer('127.0.0.1')
        assert (await call(server, peer, 'hello', subscribe={'snapshot': False, 'mode': 'rpc'}))['type'] == 'ready'
        async def forbidden(message): raise AssertionError('revoked call reached handler')
        server.handlers['screen.lease.' + verb] = forbidden
        if mutation == 'remove': token.unlink()
        elif mutation == 'empty': token.write_text('')
        elif mutation == 'rotate': token.write_text('synthetic-rotated-token')
        else: token.chmod(0o644)
        message = frame('screen.lease.' + verb, host='test', reason='fixture', lease_id='fixture-lease')
        if not include_token: message.pop('stream_token')
        response = (await server._dispatch(json.dumps(message), websocket=peer))[0]
        assert response['error_code'] == 'system_producer_auth_required', response
        if mutation == 'rotate':
            assert (await call(server, Peer(), 'hello', stream_token='synthetic-rotated-token',
                               subscribe={'snapshot': False, 'mode': 'rpc'}))['type'] == 'ready'
    asyncio.run(run())

@pytest.mark.parametrize('verb,fields', [
    ('acquire', {'reason': 'fixture'}),
    ('acquire', {'host': 'peer', 'reason': 'fixture'}),
    *[(v, {'host': 'test', 'reason': 'fixture', 'ttl': ttl})
      for v in ('acquire', 'renew') for ttl in (0, 121, 1.5, '10', True)],
    ('acquire', {'host': 'test', 'reason': 'fixture', 'force': True}),
    ('release', {'lease_id': 'fixture', 'force': True}),
    ('acquire', {'host': 'test', 'reason': 'fixture', 'extra': 'forbidden'}),
])
def test_v2_payload_bounds(credentials, verb, fields):
    async def run():
        server, peer = Server(), Peer()
        async def forbidden(message): raise AssertionError('invalid payload reached handler')
        server.handlers['screen.lease.' + verb] = forbidden
        assert (await call(server, peer, 'hello', subscribe={'snapshot': False, 'mode': 'rpc'}))['type'] == 'ready'
        response = await call(server, peer, 'screen.lease.' + verb, **fields)
        assert response['error_code'] == 'system_producer_payload_invalid', response
    asyncio.run(run())

@pytest.mark.parametrize('verb,target', [('tell', 'fixture:assistant'), ('tell', 'assistant'),
    ('tell', 'fixture:seat'), ('send', 'fixture:seat'), ('screen.lease.status', None),
    ('list_sessions', None), ('notification.create', None), ('unknown.verb', None)])
def test_v2_no_message_or_read_authority(credentials, verb, target):
    async def run():
        server, peer, queued = Server(), Peer(), []
        async def forbidden(message): queued.append(message); return {'type': 'unexpected.ok'}
        if verb != 'unknown.verb':
            server.handlers[verb] = forbidden
        assert (await call(server, peer, 'hello', subscribe={'snapshot': False, 'mode': 'rpc'}))['type'] == 'ready'
        response = await call(server, peer, verb, to_stream_id=target, text='fixture')
        assert response['error_code'] == 'system_producer_forbidden', response
        assert queued == []
    asyncio.run(run())


def test_v2_release_terminal_states(credentials):
    async def run():
        store = Store(':memory:'); store.start(); now = [1000]
        try:
            server, peer = Server(), Peer()
            server.handlers.update(ScreenLease(store, ['test'], clock=lambda: now[0]).wire_handlers())
            assert (await call(server, peer, 'hello', subscribe={'snapshot': False, 'mode': 'rpc'}))['type'] == 'ready'
            lease = await call(server, peer, 'screen.lease.acquire', host='test', reason='fixture')
            assert (await call(server, peer, 'screen.lease.release', lease_id=lease['lease_id']))['type'].endswith('.ok')
            released = await call(server, peer, 'screen.lease.release', lease_id=lease['lease_id'])
            lease = await call(server, peer, 'screen.lease.acquire', host='test', reason='fixture', ttl=1)
            now[0] += 2
            expired = await call(server, peer, 'screen.lease.release', lease_id=lease['lease_id'])
            unknown = await call(server, peer, 'screen.lease.release', lease_id='never-existed')
            for reply, state in [(released, 'released'), (expired, 'expired'), (unknown, 'unknown')]:
                assert (reply['error_code'], reply['state']) == ('screen_lease_not_found', state)
        finally: store.stop()
    asyncio.run(run())

@pytest.mark.parametrize('other', ['seat', 'monitor'])
def test_v3_isolation_both_directions_and_ownership(credentials, other):
    from test_screen_lease import exercise, DESK
    async def check(rig, lease_handler, seat_call):
        server, acq, monitor = Server(), Peer(), Peer()
        server.handlers.update(lease_handler.wire_handlers())
        for peer, actor, token in [(acq, ACTOR, TOKEN), (monitor, MONITOR, MONITOR_TOKEN)]:
            assert (await call(server, peer, 'hello', from_stream_id=actor, stream_token=token,
                               subscribe={'snapshot': False, 'mode': 'rpc'}))['type'] == 'ready'
        async def other_call(verb, **kw):
            if other == 'seat': return await seat_call(verb, **kw)
            return await call(server, monitor, 'screen.lease.' + verb,
                              from_stream_id=MONITOR, stream_token=MONITOR_TOKEN, **kw)
        first = await call(server, acq, 'screen.lease.acquire', host='test', reason='fixture')
        assert first['holder_stream_id'] == ACTOR
        status = await seat_call('status', host='test')
        assert status['hosts'][0]['lease']['holder_label'] == 'Altum acquisitions browser'
        assert status['hosts'][0]['lease']['holder_stream_id'] == ACTOR
        assert (await other_call('acquire', host='test', reason='fixture'))['error_code'] == 'screen_held'
        await call(server, acq, 'screen.lease.release', lease_id=first['lease_id'])
        second = await other_call('acquire', host='test', reason='fixture')
        assert second['holder_stream_id'] == (DESK if other == 'seat' else MONITOR)
        if other == 'monitor': assert second['holder_label'] == 'Codex reset monitor'
        assert (await call(server, acq, 'screen.lease.acquire', host='test', reason='fixture'))['error_code'] == 'screen_held'
        for verb in ['renew', 'release']:
            response = await call(server, acq, 'screen.lease.' + verb, lease_id=second['lease_id'])
            assert response['error_code'] == 'screen_lease_not_held', response
        assert len(await rig.rows('v2_outbound_notices')) == 2
    exercise(check)


def test_v2_other_host_lease_ids_refused(credentials):
    from test_screen_lease import exercise
    async def check(rig, lease_handler, seat_call):
        foreign = await seat_call('acquire', host='peer', reason='fixture')
        server, peer = Server(), Peer()
        server.handlers.update(lease_handler.wire_handlers())
        assert (await call(server, peer, 'hello', subscribe={'snapshot': False, 'mode': 'rpc'}))['type'] == 'ready'
        for verb in ['renew', 'release']:
            response = await call(server, peer, 'screen.lease.' + verb, lease_id=foreign['lease_id'])
            assert response['error_code'] == 'screen_host_invalid', response
    exercise(check)
