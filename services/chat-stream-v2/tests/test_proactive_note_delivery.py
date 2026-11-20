"""Proactive notes survive live delivery retries and summary-mode reconnects."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
import json

import pytest
import websockets

from _shared.operator_auth import AUTH_SCHEME, decode_envelope, make_proof
from tools.disposable_smoke import disposable_composite


async def receive(socket, predicate):
    async with asyncio.timeout(3):
        while True:
            frame = json.loads(await socket.recv())
            if predicate(frame):
                return frame


async def connect_client(stack, harness, kind):
    server = harness['server']
    _, envelope = server.operator_credential_registry.issue(kind, label='synthetic note client')
    credential = decode_envelope(envelope)
    socket = await stack.enter_async_context(websockets.connect(harness['cli_env']['AGENT_ORCH_WS_URL']))
    welcome = json.loads(await socket.recv())
    capabilities = {'assistant_composite_v1': True, 'work_lanes_v1': True}
    subscribe = {'events_mode': 'summary', 'include_subagents': kind == 'pentacle'}
    if kind == 'pentacle':
        subscribe['all'] = True
    else:
        capabilities.update(consent_enrollment_offer_v1=True, consent_open_v1=True)
    await socket.send(json.dumps({
        'type': 'hello', 'client': kind, 'capabilities': capabilities, 'subscribe': subscribe,
        'auth_v2': {'scheme': AUTH_SCHEME, 'credential_id': credential['credential_id'],
                    'proof': make_proof(credential['proof_key'], welcome['auth']['operator']['nonce'],
                                        credential['credential_id'], kind)},
    }))
    snapshot = await receive(socket, lambda frame: frame.get('type') == 'snapshot')
    row = next(row for row in snapshot['sessions'] if row['stream_id'] == harness['target'])
    assert row['session_kind'] == 'assistant_composite'
    assert row.get('visibility', 'default') == 'default'
    assert not row.get('opened_by_host_id')
    assert any(server._operator_authenticated(peer) and trust.credential_id == credential['credential_id']
               for peer, trust in server._connection_trust.items())
    return socket


@asynccontextmanager
async def fixture():
    async with disposable_composite(operator_composite='operator-excluded:assistant') as harness:
        server = harness['server']
        composite = server.assistant_composite
        # disposable_smoke's card helper deliberately has no broadcast and strict=0.
        # Match the production construction in main.py for this delivery journey.
        composite.config = replace(composite.config, strict_publication_default=True)
        composite.env_config = composite.config
        composite.broadcast = server.broadcast
        composite.sessions = server.sessions
        composite._wake_worker = lambda: None  # The synthetic trigger must not launch a provider.
        await composite.ensure_projection()
        assert composite.broadcast == server.broadcast
        assert composite.sessions is server.sessions
        assert (await harness['store'].strict_publication_state(harness['target']))['enabled']
        await composite.accept_input({'text': 'Synthetic trigger', 'optimistic_id': 'note-trigger'})
        await server.sessions.refresh()
        # Require real v2 proof/auth even for our loopback sockets. Exercise the
        # remote fanout branch without exposing the fixture listener to a LAN.
        server._is_loopback_client = lambda _socket: False
        assert server._frame_for_client(object(), 'chat.event', {'type': 'chat.event'}) is None
        async with AsyncExitStack() as stack:
            clients = [await connect_client(stack, harness, kind) for kind in ('pentacle', 'pentacle-mobile')]
            harness.update(clients=clients, stack=stack, composite=composite)
            yield harness
    assert not harness['state_path'].exists()
    assert harness['server']._ws_server is None


def note(harness, key, note_type='relay', **extra):
    return {'type': 'assistant.publish', 'request_id': key, 'composite_stream_id': harness['target'],
            'publish_kind': 'prose', 'message': f'Synthetic {key} https://example.invalid/note #test',
            'note_type': note_type, 'ref': 'message:note-trigger', **extra}


async def live(harness, message):
    frames = await asyncio.gather(*(receive(socket, lambda frame:
        frame.get('type') == 'chat.event' and frame.get('event', {}).get('message_id') ==
        'publication:' + message['request_id']) for socket in harness['clients']))
    assert frames[0]['event']['daemon_seq'] == frames[1]['event']['daemon_seq']
    assert all(frame['event']['text'] == message['message'] for frame in frames)
    return frames[0]['event']


async def history(harness, kind):
    socket = await connect_client(harness['stack'], harness, kind)
    request_id = 'history-' + kind
    await socket.send(json.dumps({'type': 'request_stream_events', 'request_id': request_id,
                                 'stream_id': harness['target'], 'limit': 100}))
    events = []
    while True:
        frame = await receive(socket, lambda frame: frame.get('request_id') == request_id)
        assert not frame['type'].endswith('.error')
        events.extend(frame.get('events', []))
        if frame['type'] == 'request_stream_events.ok':
            return events


def test_every_note_type_reaches_web_mobile_and_reconnect_history():
    async def run():
        async with fixture() as h:
            pushes = []
            async def push(**payload):
                pushes.append(payload)
            h['composite'].reply_push = push
            published = []
            for kind in ('result', 'problem', 'reminder', 'relay', 'started', 'change', 'other'):
                message = note(h, 'normal-' + kind, kind,
                               **({'note_reason': 'Protocol coverage'} if kind == 'other' else {}))
                await h['rpc'](message)
                event = await live(h, message)
                assert event['note_type'] == kind
                published.append(event)
            assert pushes == []  # Proactive notes without response_state=final do not push.
            for client_kind in ('pentacle', 'pentacle-mobile'):
                events = await history(h, client_kind)
                for event in published:
                    assert [row for row in events if row.get('daemon_seq') == event['daemon_seq']] == [event]
    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['activity', 'broadcast'])
def test_retry_recovers_a_committed_note_after_interruption(interruption):
    async def run():
        async with fixture() as h:
            composite = h['composite']
            message = note(h, 'interrupted-' + interruption)
            attribute = 'refresh_activity' if interruption == 'activity' else 'broadcast'
            original = getattr(composite, attribute)
            async def fail(*args, **kwargs):
                raise RuntimeError('controlled publication interruption')
            setattr(composite, attribute, fail)
            try:
                with pytest.raises(RuntimeError):
                    await h['rpc'](message)
            finally:
                setattr(composite, attribute, original)
            persisted = [row for row in await h['store'].fetch_session_event_tail(h['target'], limit=100)
                         if row.get('message_id') == 'publication:' + message['request_id']]
            assert len(persisted) == 1
            if interruption == 'activity':
                assert (await live(h, message))['daemon_seq'] == persisted[0]['daemon_seq']
            receipt = await h['rpc'](message)
            assert receipt['duplicate'] is True
            assert (await live(h, message))['daemon_seq'] == persisted[0]['daemon_seq']
            for client_kind in ('pentacle', 'pentacle-mobile'):
                assert len([row for row in await history(h, client_kind)
                            if row.get('message_id') == persisted[0]['message_id']]) == 1
    asyncio.run(run())


def test_successful_retry_replays_same_identity_without_a_second_push():
    async def run():
        async with fixture() as h:
            pushes = []
            async def push(**payload):
                pushes.append(payload)
            h['composite'].reply_push = push
            message = note(h, 'final-note', response_state='final')
            await h['rpc'](message)
            first = await live(h, message)
            assert len(pushes) == 1
            assert (await h['rpc'](message))['duplicate'] is True
            assert await live(h, message) == first
            assert len(pushes) == 1
    asyncio.run(run())
