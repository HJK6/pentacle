"""One separately credentialed WMI principal, Updates visibility only."""
from __future__ import annotations

import asyncio
import json

import pytest

from notify import Notify
from server import Server
from test_system_notification_producer import Peer, SocketPeer

WMI = 'amaterasu:wmi-pg-dailybackup'
TOKEN = 'synthetic-wmi-backup-token'
CD_TOKEN = 'synthetic-independent-cd-token'


@pytest.fixture
def credentials(monkeypatch, tmp_path):
    wmi = tmp_path / 'wmi-token'
    cd = tmp_path / 'cd-token'
    for path, value in ((wmi, TOKEN), (cd, CD_TOKEN)):
        path.write_text(value)
        path.chmod(0o600)
    monkeypatch.setenv('PENTACLE_WMI_BACKUP_STREAM_TOKEN_FILE', str(wmi))
    monkeypatch.setenv('PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE', str(cd))
    monkeypatch.setenv('PENTACLE_SYSTEM_PRODUCER_STREAM_ID', 'altum-bot-cd')
    return wmi, cd


def frame(kind='notification.create', **extra):
    payload = dict(type=kind, request_id='synthetic-wmi-request', from_stream_id=WMI, stream_token=TOKEN)
    if kind == 'hello':
        payload['subscribe'] = {'snapshot': False, 'mode': 'rpc'}
    elif kind == 'notification.create':
        payload.update(producer=WMI, destination='pentacle-updates', title='Synthetic WMI backup failure',
                       body='Synthetic receipt; no real failure forced.', severity='critical',
                       dedup_key='wmi-backup|synthetic|2026-10-06', actions=[])
    return {**payload, **extra}


async def dispatch(server, peer, payload):
    return (await server._dispatch(json.dumps(payload), websocket=peer))[0]


async def authenticate(server, peer):
    assert (await dispatch(server, peer, frame('hello')))['type'] == 'ready'


def test_wmi_store_broadcast_restart_and_stable_retry(credentials, tmp_path):
    async def run():
        db = str(tmp_path / 'notifications.db')
        server = Server()
        ui = SocketPeer()
        producer = SocketPeer('192.0.2.10')
        server._register_client(ui)
        server._register_client(producer)
        server._activate_client(ui)
        notify = Notify(db, broadcast=server.broadcast)
        await notify.start()
        server.handlers.update(notify.wire_handlers())
        try:
            await authenticate(server, producer)
            first = await dispatch(server, producer, frame())
            assert first['type'] == 'notification.create.ok'
            nid = first['notification']['notification_id']
            for _ in range(10):
                await asyncio.sleep(0)
            assert any(f['type'] == 'notification' and f['notification']['notification_id'] == nid
                       and f['notification']['producer'] == WMI and 'Synthetic receipt' in f['notification']['body']
                       for f in ui.sent)
            assert producer.sent == []  # Restricted RPC principal never subscribes to the feed.
        finally:
            await notify.stop()
            server._unregister_client(ui)
            server._unregister_client(producer)
        restarted = Server()
        notify = Notify(db)
        await notify.start()
        restarted.handlers.update(notify.wire_handlers())
        try:
            peer = Peer()
            await authenticate(restarted, peer)
            retry = await dispatch(restarted, peer, frame(request_id='synthetic-retry'))
            assert retry['notification']['notification_id'] == nid
            assert retry['notification']['firing_count'] == 2
            rows = await notify._db.call('list_notifications')
            assert len(rows) == 1 and rows[0]['actions'] == []
            assert rows[0]['answer_to_stream_id'] is None
        finally:
            await notify.stop()
    asyncio.run(run())


@pytest.mark.parametrize('patch', [
    {'producer': 'altum-bot-cd'}, {'producer': 'unrelated-producer'},
    {'destination': 'thoth:v2-other'}, {'destination': None},
    {'actions': [{'kind': 'run_command', 'command': 'echo forbidden'}]},
    {'actions': None}, {'answer_to_stream_id': 'other'}, {'ttl_seconds': 60},
    {'_auth_context': {'operator_authenticated': True}}, {'severity': 'info'},
    {'title': 'x'*121}, {'title': ' '}, {'body': 'x'*1201}, {'body': ''},
    {'body': None}, {'request_id': 'x'*121}, {'dedup_key': 'pipeline|synthetic|2026-10-06'},
    {'dedup_key': 'wmi-backup|synthetic|2026-02-30'},
])
def test_payload_rejected_before_store_or_broadcast(credentials, tmp_path, patch):
    async def run():
        broadcasts = []
        async def broadcast(msg): broadcasts.append(msg)
        notify = Notify(str(tmp_path / 'notifications.db'), broadcast=broadcast)
        await notify.start()
        server = Server()
        server.handlers.update(notify.wire_handlers())
        try:
            peer = Peer()
            await authenticate(server, peer)
            result = await dispatch(server, peer, frame(**patch))
            assert result['error_code'] == 'system_producer_payload_invalid'
            assert await notify._db.call('list_notifications') == []
            assert broadcasts == []
        finally:
            await notify.stop()
    asyncio.run(run())


@pytest.mark.parametrize('address', ['192.0.2.10', '127.0.0.1'])
@pytest.mark.parametrize('patch', [
    {'from_stream_id': 'altum-bot-cd'}, {'from_stream_id': 'unrelated'},
    {'stream_token': CD_TOKEN}, {'stream_token': ''},
    {'subscribe': {'snapshot': True, 'mode': 'rpc'}},
    {'subscribe': {'snapshot': False, 'mode': 'full'}},
])
def test_wrong_identity_token_or_hello_fails_closed(credentials, address, patch):
    async def run():
        server = Server()
        peer = Peer(address)
        assert (await dispatch(server, peer, frame('hello', **patch)))['error_code'] == 'system_producer_auth_required'
        assert peer not in server._client_system_producers
    asyncio.run(run())


@pytest.mark.parametrize('state', ['unset', 'removed', 'empty', 'insecure', 'shared_cd'])
def test_absent_or_unsafe_file_never_borrows_cd_or_loopback(credentials, monkeypatch, state):
    wmi, cd = credentials
    if state == 'unset': monkeypatch.delenv('PENTACLE_WMI_BACKUP_STREAM_TOKEN_FILE')
    elif state == 'removed': wmi.unlink()
    elif state == 'empty': wmi.write_text('')
    elif state == 'insecure': wmi.chmod(0o644)
    else: wmi.write_text(CD_TOKEN)
    async def run():
        for address in ('192.0.2.10', '127.0.0.1'):
            server = Server()
            reply = await dispatch(server, Peer(address), frame('hello', stream_token=CD_TOKEN if state=='shared_cd' else TOKEN))
            assert reply['error_code'] == 'system_producer_auth_required'
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['remove', 'rotate'])
def test_revoked_file_invalidates_bound_and_fresh_connections(credentials, mutation):
    async def run():
        server = Server()
        calls = []
        async def create(msg): calls.append(msg); return {'type':'notification.create.ok'}
        server.handlers['notification.create'] = create
        peer = Peer()
        await authenticate(server, peer)
        wmi, _ = credentials
        if mutation == 'remove': wmi.unlink()
        else: wmi.write_text('synthetic-rotated-token')
        assert (await dispatch(server, peer, frame()))['error_code'] == 'system_producer_auth_required'
        tokenless = frame(); tokenless.pop('stream_token')
        assert (await dispatch(server, peer, tokenless))['error_code'] == 'system_producer_auth_required'
        assert (await dispatch(server, Peer(), frame('hello')))['error_code'] == 'system_producer_auth_required'
        assert calls == []
        if mutation == 'rotate':
            assert (await dispatch(server, Peer(), frame('hello', stream_token='synthetic-rotated-token')))['type'] == 'ready'
    asyncio.run(run())


def test_wmi_forbidden_verbs_and_cd_remains_independent(credentials):
    async def run():
        server = Server()
        peer = Peer()
        await authenticate(server, peer)
        calls = []
        for verb in ('tell', 'spawn', 'notification.list', 'notification.resolve', 'prompt.ask', 'grant_token', 'list_sessions'):
            async def handler(msg): calls.append(msg); return {'type':'unexpected'}
            server.handlers[verb] = handler
            assert (await dispatch(server, peer, frame(verb)))['error_code'] == 'system_producer_forbidden'
        assert calls == []
        cd_peer = Peer()
        cd_hello = frame('hello', from_stream_id='altum-bot-cd', stream_token=CD_TOKEN)
        assert (await dispatch(server, cd_peer, cd_hello))['type'] == 'ready'
        credentials[0].unlink()
        assert (await dispatch(server, Peer(), cd_hello))['type'] == 'ready'
    asyncio.run(run())


@pytest.mark.parametrize('privilege', ['actions', 'answer'])
def test_wmi_retry_refuses_privileged_collision(credentials, tmp_path, privilege):
    async def run():
        notify = Notify(str(tmp_path / 'notifications.db'))
        await notify.start()
        server = Server(); server.handlers.update(notify.wire_handlers())
        try:
            existing = await notify._db.call('create_notification', producer=WMI, title='privileged preimage',
                dedup_key=frame()['dedup_key'], actions=[{'kind':'ack'}] if privilege=='actions' else [],
                answer_to_stream_id='test:seat' if privilege=='answer' else None)
            peer = Peer(); await authenticate(server, peer)
            assert (await dispatch(server, peer, frame()))['error_code'] == 'notification_invalid'
            unchanged = await notify._db.call('get_notification', existing['notification_id'])
            assert unchanged['title'] == 'privileged preimage' and unchanged['firing_count'] == 1
        finally:
            await notify.stop()
    asyncio.run(run())
