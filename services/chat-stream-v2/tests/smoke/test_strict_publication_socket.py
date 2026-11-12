"""One-door journey against real Store/Server/Composite, owned loopback only."""
import asyncio
import hashlib
import json
import shlex

import websockets
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from outbound_notices import OutboundNoticeQueue
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
from test_assistant_dispatch_turn_final import Harness, ASSISTANT, ROOT


def test_operator_turn_notice_ack_is_the_only_reply(tmp_path):
    async def journey():
        store = Store(str(tmp_path / 'fixture.db'))
        store.start()
        composite = server = None
        try:
            root = await store.open_session('fixture-root','visible',provider='claude',pane_pid='4242',role='assistant')
            generation = root['session_generation']
            token = 'one-door-fixture-token'
            await store.grant_stream_token('fixture-root','visible',hashlib.sha256(token.encode()).hexdigest(),STREAM_TOKEN_HASH_VERSION)
            config = AssistantCompositeConfig.from_env({
                'PENTACLE_ASSISTANT_COMPOSITE_ENABLED':'1','PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID':ASSISTANT,
                'PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID':ROOT,'PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION':generation})
            async def dispatch(route):
                return {'delivery':'landed'}
            composite = AssistantComposite(store,config=config,dispatch=dispatch)
            await composite.ensure_projection()
            sessions = Sessions(store,local_host='fixture-chat')
            await sessions.refresh()
            server = Server(host='127.0.0.1',port=0,store=store,sessions=sessions,local_host='fixture-chat')
            server.assistant_composite = composite
            port = await server.bind()
            h = Harness(store,composite,generation)
            received = []
            class OwnedSeatTransport:
                async def deliver_outbound_notice(self,msg,check_existing=False):
                    # Exercise production composite/digest ingress before the
                    # synthetic seat receives input. No native pane is touched.
                    held = await composite.suppress_routine_backend_ingress(
                        target_stream_id=ROOT,body=msg['message'],msg=msg,verb='tell')
                    assert held is None, held
                    received.append(msg)
                    return {'delivery_status':'delivered'}
            queue = OutboundNoticeQueue(store,OwnedSeatTransport())
            async with websockets.connect(f'ws://127.0.0.1:{port}') as socket:
                assert json.loads(await socket.recv())['type']=='welcome'
                await socket.send(json.dumps({'type':'hello','stream_token':token,'from_stream_id':ROOT,'client':'one-door-fixture'}))
                frames=[json.loads(await socket.recv()) for _ in range(2)]
                assert any(f['type']=='snapshot' for f in frames)
                await server._on_send({'host':'fixture-chat','session_name':'assistant','text':'Please answer deliberately',
                    'request_id':'operator-rpc','optimistic_id':'operator-input',
                    '_auth_context':{'operator_authenticated':True,'operator_principal':'operator:fixture'}})
                for _ in range(200):
                    route=await store.get_assistant_composite_route(stream_id=ASSISTANT,input_identity='operator-input')
                    if route and route['delivery_state']=='landed':break
                    await asyncio.sleep(.01)
                assert route['delivery_state']=='landed'
                await h.dispatch_user(route,'socket-user')
                await h.ingest('ASSIST_TEXT','done','socket-final',final=True)
                assert await h.answers()==[]
                assert await queue.drain_once(kinds={'dispatch_no_reply'},force=True)==1
                assert len(received)==1
                command=next(l for l in received[0]['message'].splitlines() if l.startswith('agent-orch') and ':ack ' in l)
                args=shlex.split(command)[3:]
                msg={args[i][2:].replace('-','_'):args[i+1] for i in range(0,len(args),2)}
                msg['type']='assistant.publish'
                await socket.send(json.dumps(msg))
                async with asyncio.timeout(3):
                    while True:
                        frame=json.loads(await socket.recv())
                        if frame.get('request_id')==msg['request_id']:break
                assert frame['type']=='assistant.publish.ok',frame
                events=await store.fetch_session_event_tail(ASSISTANT,limit=20)
                assert [e['kind'] for e in events]==['USER','ASSIST_TEXT']
                assert events[1]['reply_to_message_id']=='operator-input'
                assert events[1]['text']=='I am working on your message.'
                assert not await composite.front_desk_digest._rows(ROOT)
                (tmp_path/'transcript.json').write_text(json.dumps(events,indent=2))
        finally:
            if composite is not None:await composite.stop()
            if server is not None:await server.close()
            store.stop()
    asyncio.run(journey())
