"""Authenticated proactive-note journey on an owned disposable composite."""
import asyncio
from dataclasses import replace
import json

import websockets

from _shared.operator_auth import AUTH_SCHEME, decode_envelope, make_proof


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
    socket = await stack.enter_async_context(websockets.connect(
        harness['cli_env']['AGENT_ORCH_WS_URL'], open_timeout=3, close_timeout=1,
    ))
    welcome = await receive(socket, lambda frame: frame.get('type') == 'welcome')
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
    row = next((row for row in snapshot['sessions'] if row['stream_id'] == harness['target']), {})
    if (row.get('session_kind') != 'assistant_composite'
            or row.get('visibility', 'default') != 'default' or row.get('opened_by_host_id')):
        raise ValueError('disposable subscription was not acknowledged')
    if not any(server._operator_authenticated(peer) and trust.credential_id == credential['credential_id']
               for peer, trust in server._connection_trust.items()):
        raise ValueError('disposable subscriber authentication was not confirmed')
    return socket


async def prepare(harness):
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
    if not (await harness['store'].strict_publication_state(harness['target']))['enabled']:
        raise ValueError('strict publication is disabled')
    await composite.accept_input({'text': 'Synthetic trigger', 'optimistic_id': 'note-trigger'})
    await server.sessions.refresh()
    # Require real v2 proof/auth even for our loopback sockets. Exercise the
    # remote fanout branch without exposing the fixture listener to a LAN.
    server._is_loopback_client = lambda _socket: False
    if server._frame_for_client(object(), 'chat.event', {'type': 'chat.event'}) is not None:
        raise ValueError('unauthenticated client can receive probe events')
    return composite


def note(harness, key, note_type='relay', **extra):
    return {'type': 'assistant.publish', 'request_id': key, 'composite_stream_id': harness['target'],
            'publish_kind': 'prose', 'message': f'Synthetic {key} https://example.invalid/note #test',
            'note_type': note_type, 'ref': 'message:note-trigger', **extra}


async def history(socket, target, request_id):
    await socket.send(json.dumps({'type': 'request_stream_events', 'request_id': request_id,
                                 'stream_id': target, 'limit': 100}))
    events = []
    async with asyncio.timeout(3):
        while True:
            frame = await receive(socket, lambda frame: frame.get('request_id') == request_id)
            if frame['type'].endswith('.error'):
                raise ValueError('subscriber history request failed')
            events.extend(frame.get('events', []))
            if frame['type'] == 'request_stream_events.ok':
                return events
