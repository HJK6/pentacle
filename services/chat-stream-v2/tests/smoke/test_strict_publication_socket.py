"""One operator dispatch through a real socket, queue, digest and isolated store."""
import asyncio
import hashlib
import json
import shlex

import websockets

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from codex_rollout_norm import normalize_codex_rollout_record
from notification_answer_fixture import fixture
from server import Server
from store import STREAM_TOKEN_HASH_VERSION
from test_front_desk_digest import _production_dispatcher
from v2_runtime import iso_now


def test_socket_missing_reply_notice_and_copied_ack(tmp_path):
    async def run():
        async with fixture(tmp_path, host='fixture-host') as (_, queue, comms, provider, sessions, store):
            target, chat = 'fixture-host:v2-test', 'fixture-host:assistant'
            generation = sessions.get(target)['session_generation']
            await store.update_session('fixture-host', 'v2-test', pane_pid='4242')
            token = 'socket-seat-token'
            await store.grant_stream_token('fixture-host', 'v2-test', hashlib.sha256(token.encode()).hexdigest(),
                                           STREAM_TOKEN_HASH_VERSION)
            composite = AssistantComposite(store, config=AssistantCompositeConfig(
                enabled=True, name='assistant', stream_id=chat, direct_primary_stream_id=target,
                direct_primary_generation=generation, strict_publication_default=True),
                dispatch=_production_dispatcher(comms))
            comms.front_desk_digest = queue.front_desk_digest = composite.front_desk_digest
            comms.assistant_ingress_policy = composite.suppress_routine_backend_ingress
            server = Server(host='127.0.0.1', port=0, store=store, sessions=sessions,
                            comms=comms, local_host='fixture-host')
            server.assistant_composite = composite
            await composite.ensure_projection()
            await sessions.refresh()
            port = await server.bind()
            counter = 0
            async def ingest(role, text):
                nonlocal counter
                counter += 1
                identity = f'record-{counter}'
                event = normalize_codex_rollout_record({
                    'type': 'response_item', 'timestamp': iso_now(), 'payload': {
                        'type': 'message', 'id': identity, 'role': role,
                        'phase': 'final_answer' if role == 'assistant' else 'commentary',
                        'content': [{'type': 'output_text' if role == 'assistant' else 'input_text', 'text': text}]}},
                    host='fixture-host', session_name='v2-test', session_id='dispatch-transcript')[0]
                result = await store.append_session_events_lifecycle_cas([{'stream_id': target, 'event': event,
                    'identity': identity, 'lifecycle': await store.fetch_open_session_lifecycle(target, pane_pid='4242')}], limit=100)
                assert result and isinstance(result[0], int)
            async def user(text):
                await ingest('user', text)
            provider.user = user
            try:
                async with websockets.connect(f'ws://127.0.0.1:{port}') as socket:
                    assert json.loads(await socket.recv())['type'] == 'welcome'
                    await socket.send(json.dumps({'type':'hello', 'client':'strict-socket',
                                                   'stream_token':token, 'from_stream_id':target}))
                    frames = [json.loads(await socket.recv()) for _ in range(2)]
                    assert any(frame['type'] == 'snapshot' for frame in frames)
                    await server._on_send({'stream_id':chat, 'text':'Original operator question',
                        'msg_id':'operator-input', 'request_id':'send-request',
                        '_auth_context':{'operator_authenticated':True, 'operator_principal':'operator:fixture'}})
                    for _ in range(100):
                        route = await store.get_assistant_composite_route(stream_id=chat, input_identity='operator-input')
                        if route and route['delivery_state'] == 'landed':
                            break
                        await asyncio.sleep(.01)
                    assert route['delivery_state'] == 'landed'
                    await ingest('assistant', 'done')
                    notice_id = 'dispatch_no_reply:' + route['dispatch_id']
                    assert await queue.deliver_now(notice_id)
                    assert not await composite.front_desk_digest._rows(target)
                    assert len(provider.pastes) == 2
                    command = provider.pastes[-1].split('Acknowledgment: ', 1)[1].splitlines()[0]
                    store.stop()
                    store.start()
                    await ingest('user', json.loads(route['route_json'])['direct_envelope']['wire_body'])
                    await ingest('assistant', 'done again')
                    count = await store.submit(lambda conn: conn.execute(
                        "SELECT count(*) FROM v2_outbound_notices WHERE notice_id=?", (notice_id,)).fetchone()[0])
                    assert count == 1
                    words = shlex.split(command)[3:]
                    payload = {words[i][2:].replace('-', '_'): words[i + 1] for i in range(0, len(words), 2)}
                    payload['type'] = 'assistant.publish'
                    await socket.send(json.dumps(payload))
                    async with asyncio.timeout(3):
                        while True:
                            response = json.loads(await socket.recv())
                            if response.get('request_id') == payload['request_id']:
                                break
                    assert response['type'] == 'assistant.publish.ok', response
                    previous_id = response['event_id']
                    duplicate = {'type': 'assistant.publish', 'request_id': 'duplicate-note',
                        'composite_stream_id': chat, 'publish_kind': 'status',
                        'message': 'I received **your message.**', 'note_type': 'relay',
                        'ref': 'message:operator-input'}
                    await socket.send(json.dumps(duplicate))
                    async with asyncio.timeout(3):
                        while True:
                            rejected = json.loads(await socket.recv())
                            if rejected.get('request_id') == 'duplicate-note':
                                break
                    assert rejected['error_code'] == 'assistant_publish_duplicate_text'
                    assert str(previous_id) in rejected['error']
                    events = await store.fetch_session_event_tail(chat, limit=20)
                    assert [(e['kind'], e['text']) for e in events] == [
                        ('USER', 'Original operator question'), ('ASSIST_TEXT', 'I received your message.')]
            finally:
                await composite.stop()
                await server.close()
    asyncio.run(run())
