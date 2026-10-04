"""Front desk admission and durable digest behavior, through production store paths."""
import asyncio

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from store import Store


def test_direct_front_desk_holds_start_but_other_parents_keep_delivery():
    async def run():
        store=Store(':memory:'); store.start()
        try:
            composite=AssistantComposite(store, config=AssistantCompositeConfig(enabled=True,
                name='bart', stream_id='h:assistant', direct_primary_stream_id='h:desk',
                direct_primary_generation='g', astra_stream_id='h:desk'))
            held=await composite.suppress_routine_backend_ingress(target_stream_id='h:desk',
                body='START: working', msg={'tell_id':'start-1'}, verb='tell')
            assert held is not None and held['delivery_status']=='persisted'
            assert await composite.suppress_routine_backend_ingress(target_stream_id='h:parent',
                body='START: working',msg={'tell_id':'start-2'},verb='tell') is None
        finally: store.stop()
    asyncio.run(run())

import json
import time
from types import SimpleNamespace

import pytest

from front_desk_digest import DIGEST_TOKEN, FrontDeskDigest, HELD_KIND
from outbound_notices import OutboundNoticeQueue
from test_context_nudges import _context_harness
from test_nudges import HOST, _iso, _new_store


def _desk(store, target, generation):
    composite=AssistantComposite(store, config=AssistantCompositeConfig(enabled=True,
        name='bart',stream_id=f'{HOST}:assistant',direct_primary_stream_id=target,
        direct_primary_generation=generation,astra_stream_id=target))
    return composite


@pytest.mark.parametrize('body,msg', [
    ('GATE: ready', {}), ('BLOCKER: missing decision', {}),
    ('operator input', {'_auth_context':{'operator_authenticated':True}}),
    ('[notification_answer] answer', {'_outbound_notice_kind':'notification_answer'}),
    ('[child_report_ready] ready', {'_outbound_notice_kind':'report'}),
    ('timed wake', {'_outbound_notice_kind':'wake'}),
    ('DOT EMAIL HANDOFF — [email from Dot]', {'_auth_context':{'operator_authenticated':True}}),
    ('[Assistant lane ruling] '+json.dumps({'state':'release_blocked','action':'spawn'}),
        {'_outbound_notice_kind':'assistant_lane_ruling_result'}),
    ('[Assistant lane ruling] '+json.dumps({'state':'revised','action':'spawn'}),
        {'_outbound_notice_kind':'assistant_lane_ruling_result'}),
    ('[Assistant lane ruling] '+json.dumps({'state':'denied','action':'spawn'}),
        {'_outbound_notice_kind':'assistant_lane_ruling_result'}),
])
def test_wake_is_byte_identical_and_alone_while_digest_is_held(body,msg,tmp_path):
    async def run():
        store=Store(str(tmp_path/'store.db'));store.start()
        try:
            h=await _context_harness(store,parent=False)
            target=f'{HOST}:child'
            composite=_desk(store,target,h.sessions.get(target)['session_generation'])
            h.comms.assistant_ingress_policy=composite.suppress_routine_backend_ingress
            h.comms.front_desk_digest=composite.front_desk_digest
            await h.comms.tell({'stream_id':target,'message':'START: work','tell_id':'held-start'})
            await h.comms.tell({'stream_id':target,'message':'END: done','tell_id':'held-end'})
            assert not h.tmux.pasted
            await h.comms.tell({'stream_id':target,'message':body,'tell_id':'wake',**msg})
            assert h.tmux.pasted==[body]
            assert len(await composite.front_desk_digest._rows(target))==2
            store.stop();store.start()
            await composite.front_desk_digest.tick(now=time.time()+100)
            assert await store.list_outbound_notice_ids(force=True)==[]
            assert len(await composite.front_desk_digest._rows(target))==2
            assert h.tmux.pasted==[body]
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('body,msg,dropped',[
    ('START: begin',{},False),('END: done',{},False),('receipt: landed',{},False),
    ('concur: proceed',{},False),('child inactivity threshold',{'_outbound_notice_kind':'watch'},False),
    ('tree idle',{'_outbound_notice_kind':'tree_idle'},True),
    ('context_advisory: h:child crossed threshold',{},True),
    ('[Assistant lane ruling] '+json.dumps({'state':'done','action':'spawn','conditions':None}),
        {'_outbound_notice_kind':'assistant_lane_ruling_result'},True),
])
def test_digest_or_dropped_class_never_submits_immediately(body,msg,dropped):
    async def run():
        store=Store(':memory:');store.start()
        try:
            desk=_desk(store,'h:desk','g').front_desk_digest
            reply=await desk.ingress(target_stream_id='h:desk',body=body,
                msg={'tell_id':'held',**msg},verb='tell')
            assert reply['delivery_status']=='persisted'
            assert len(await desk._rows('h:desk'))==(0 if dropped else 1)
            assert await store.list_outbound_notice_ids(force=True)==[]
        finally:store.stop()
    asyncio.run(run())


def test_deadline_anchored_to_oldest_restart_safe_no_empty_digest(tmp_path,monkeypatch):
    async def run():
        store=Store(str(tmp_path/'store.db'));store.start()
        try:
            desk=_desk(store,'h:desk','g').front_desk_digest
            async def hold(identity,stamp):
                await desk.ingress(target_stream_id='h:desk',body='START: '+identity,
                    msg={'tell_id':identity},verb='tell')
                await store.submit(lambda conn: (conn.execute('UPDATE v2_outbound_notices SET created_at=? WHERE body=?',
                    (_iso(stamp),'START: '+identity)),conn.commit()))
            await desk.tick(now=10000)
            assert not await store.list_outbound_notice_ids(force=True)
            await hold('first',1000);await hold('second',4500)
            await desk.tick(now=4599)
            assert not await store.list_outbound_notice_ids(force=True)
            store.stop();store.start()
            await desk.tick(now=4600)
            ids=await store.list_outbound_notice_ids(force=True)
            assert len(ids)==1
            row=await store.outbound_notice_for_dedupe(ids[0])
            assert row['kind']=='lane_digest'
            assert 'START: first' in row['body'] and 'START: second' in row['body']
            await desk.tick(now=9000)
            assert await store.list_outbound_notice_ids(force=True)==ids
        finally:store.stop()
    asyncio.run(run())


def test_successful_spawn_ruling_emits_no_frontdesk_notice_but_others_do():
    from assistant_lane_rulings import AssistantLaneRulings
    async def run():
        store=Store(':memory:');store.start()
        try:
            server=SimpleNamespace(store=store,assistant_composite=_desk(store,'h:desk','g'))
            rulings=AssistantLaneRulings(server)
            request={'ruling_request_id':'r1','requester_stream_id':'h:desk','requester_generation':'g',
                'authority_stream_id':'h:advisor','state':'done','action':'spawn','conditions':None,
                'target_stream_id':'h:child','ruling':'approve','reason':'okay','outcome_json':'{}'}
            await rulings._result_notice(request)
            assert not await store.list_outbound_notice_ids(force=True)
            for state in ['release_blocked','revised','denied']:
                await rulings._result_notice({**request,'ruling_request_id':state,'state':state})
            await rulings._result_notice({**request,'ruling_request_id':'other-parent','requester_stream_id':'h:other'})
            assert len(await store.list_outbound_notice_ids(force=True))==4
        finally:store.stop()
    asyncio.run(run())


def test_notification_answer_with_held_items_preserves_trusted_envelope(tmp_path):
    from notification_answer_fixture import fixture
    from test_notify_answer_resolution import _seed_live_shaped_question
    from message_envelopes import match_message_envelope
    async def run():
        async with fixture(tmp_path,host='fixture-host') as (notify,queue,comms,provider,sessions,store):
            target='fixture-host:v2-test'
            composite=_desk(store,target,sessions.get(target)['session_generation'])
            comms.assistant_ingress_policy=composite.suppress_routine_backend_ingress
            comms.front_desk_digest=composite.front_desk_digest
            await comms.tell({'stream_id':target,'message':'END: finished','tell_id':'held-end'})
            question=await _seed_live_shaped_question(notify,producer_stream_id=target,question_id='q-digest-answer')
            await notify.notification({'type':'notification.resolve','request_id':'digest-answer',
                'notification_id':question['notification_id'],'action_kind':'yes_no','choice':True,
                'selections':['approve'],'_auth_context':{'operator_authenticated':True}})
            assert await queue.drain_once(force=True)==1
            assert len(provider.pastes)==1
            tail=await store.fetch_session_event_tail(target,limit=500)
            answer=next(e for e in tail if e['kind']=='USER' and '[notification.answer]' in e.get('text',''))
            assert answer.get('raw',{}).get('daemon_notice',{}).get('kind')=='notification_answer'
            assert match_message_envelope(answer['text'])['kind']=='notification_answer'
            assert '[Front desk held digest]' not in provider.pastes[0]
            assert len(await composite.front_desk_digest._rows(target))==1
    asyncio.run(run())


def test_due_digest_uses_one_normal_outbound_submission_and_never_repeats(tmp_path,monkeypatch):
    from notification_answer_fixture import fixture
    from message_envelopes import match_message_envelope
    from watch_wake import WatchWake
    async def run():
        async with fixture(tmp_path,host='fixture-host') as (_notify,queue,comms,provider,sessions,store):
            target='fixture-host:v2-test'
            composite=_desk(store,target,sessions.get(target)['session_generation'])
            comms.assistant_ingress_policy=composite.suppress_routine_backend_ingress
            comms.front_desk_digest=composite.front_desk_digest
            queue.front_desk_digest=composite.front_desk_digest
            # The inherited lane-digest switch must not disable the new deadline.
            monkeypatch.setenv('PENTACLE_LANE_DIGEST_S','0')
            sessions.apply_live(target,online=True,pane_status='pane_alive')
            await store.update_session('fixture-host','v2-test',pane_status='pane_alive')
            WatchWake(store,sessions,queue,root_binding=composite.front_desk_digest.binding)
            await comms.tell({'stream_id':target,'message':'START: work','tell_id':'held-start'})
            await comms.tell({'stream_id':target,'message':'END: done','tell_id':'held-end'})
            assert await queue.drain_once(force=True)==0 and provider.pastes==[]
            monkeypatch.setenv('PENTACLE_FRONT_DESK_DIGEST_S','0')
            assert await queue.drain_once(force=True)==1
            assert len(provider.pastes)==1
            envelope=match_message_envelope(provider.pastes[0])
            assert envelope['kind']=='lane_digest' and len(envelope['lanes'])==2
            assert await queue.drain_once(force=True)==0
            assert len(provider.pastes)==1
    asyncio.run(run())


def test_other_assistant_keeps_original_ingress_behavior():
    async def run():
        store=Store(':memory:');store.start()
        try:
            composite=AssistantComposite(store,config=AssistantCompositeConfig(enabled=True,name='daff',
                stream_id='h:daff',direct_primary_stream_id='h:desk',direct_primary_generation='g'))
            assert await composite.suppress_routine_backend_ingress(target_stream_id='h:desk',
                body='START: begin',msg={'tell_id':'start'},verb='tell') is None
        finally:store.stop()
    asyncio.run(run())


def test_operator_send_stays_alone_and_peer_send_is_held(tmp_path):
    from notification_answer_fixture import fixture
    async def run():
        async with fixture(tmp_path,host='fixture-host') as (_notify,_queue,comms,provider,sessions,store):
            target='fixture-host:v2-test'
            composite=_desk(store,target,sessions.get(target)['session_generation'])
            comms.front_desk_digest=composite.front_desk_digest
            first=await comms.send({'stream_id':target,'text':'START: peer','request_id':'peer-send'})
            assert first['delivery']=='persisted' and provider.pastes==[]
            again=await comms.send({'stream_id':target,'text':'START: peer','request_id':'peer-send'})
            assert again['delivery']=='persisted' and len(await composite.front_desk_digest._rows(target))==1
            result=await comms.send({'stream_id':target,'text':'operator command','request_id':'operator-send',
                '_auth_context':{'operator_authenticated':True}})
            assert result['delivery']=='landed' and provider.pastes==['operator command']
            assert len(await composite.front_desk_digest._rows(target))==1
    asyncio.run(run())


def test_held_sends_preserve_rotated_request_dedupe_and_correlated_receipts(tmp_path):
    from notification_answer_fixture import fixture
    async def run():
        async with fixture(tmp_path,host='fixture-host') as (_notify,_queue,comms,provider,sessions,store):
            target='fixture-host:v2-test'
            composite=_desk(store,target,sessions.get(target)['session_generation'])
            comms.front_desk_digest=composite.front_desk_digest
            common={'stream_id':target,'text':'START: one logical send','optimistic_id':'stable-optimistic'}
            await comms.send({**common,'request_id':'r1'})
            retry=await comms.send({**common,'request_id':'r2'})
            assert len(await composite.front_desk_digest._rows(target))==1
            assert retry['coalesced'] is True and retry['delivery']=='persisted'
            for request in ['r1','r2']:
                rows=await store.submit(lambda conn, request=request: [dict(r) for r in conn.execute(
                    'SELECT * FROM v2_send_receipts WHERE request_id=?',(request,))])
                assert rows and rows[-1]['delivery']=='persisted'
            assert provider.pastes==[]
            # The existing 60-second identity window still admits a new send.
            await store.submit(lambda conn: (conn.execute('UPDATE v2_send_receipts SET created_at=?',
                ('2000-01-01T00:00:00Z',)),conn.commit()))
            await comms.send({**common,'request_id':'r3'})
            assert len(await composite.front_desk_digest._rows(target))==2
            assert provider.pastes==[]
    asyncio.run(run())


def _production_dispatcher(comms):
    """Execute main's exact closure with only its transport dependency replaced."""
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                and n.name == '_dispatch_assistant_route')
    namespace = {'comms': comms, 'json': json}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'main.py', 'exec'), namespace)
    return namespace[node.name]


def test_canonical_composite_dispatch_reaches_front_desk_end_to_end(tmp_path):
    from notification_answer_fixture import fixture
    from server import Server
    async def run():
        async with fixture(tmp_path, host='fixture-host') as (_notify, _queue, comms, provider, sessions, store):
            target = 'fixture-host:v2-test'
            generation = sessions.get(target)['session_generation']
            await store.update_session('fixture-host', 'v2-test', pane_pid='4242')
            composite = AssistantComposite(store, config=AssistantCompositeConfig(
                enabled=True, name='bart', stream_id='fixture-host:assistant',
                direct_primary_stream_id=target, direct_primary_generation=generation),
                dispatch=_production_dispatcher(comms))
            comms.front_desk_digest = composite.front_desk_digest
            comms.assistant_ingress_policy = composite.suppress_routine_backend_ingress
            server = Server(store=store, sessions=sessions, comms=comms, local_host='fixture-host')
            server.assistant_composite = composite
            await composite.ensure_projection()
            from codex_rollout_norm import normalize_codex_rollout_record
            from v2_runtime import iso_now
            async def ingest(role, text, identity):
                event = normalize_codex_rollout_record({
                    'type':'response_item', 'timestamp':iso_now(),
                    'payload':{'type':'message', 'id':identity, 'role':role,
                        'phase':'final_answer' if role == 'assistant' else 'commentary',
                        'content':[{'type':'output_text' if role == 'assistant' else 'input_text',
                                    'text':text}]}},
                    host='fixture-host', session_name='v2-test', session_id='dispatch-transcript')[0]
                inserted = await store.append_session_events_lifecycle_cas([
                    {'stream_id':target, 'event':event, 'identity':identity,
                     'lifecycle':await store.fetch_open_session_lifecycle(target, pane_pid='4242')}], limit=100)
                assert inserted and isinstance(inserted[0], int)
                return inserted[0]
            # The provider counterpart emits the real normalized USER shape so
            # the mirror can correlate the eventual final to this dispatch.
            async def user(text):
                await ingest('user', text, 'dispatch-user')
            provider.user = user
            try:
                await server._on_send({'stream_id':'fixture-host:assistant', 'text':'Original operator question',
                    'msg_id':'logical-direct', 'request_id':'transport-direct',
                    '_auth_context':{'operator_authenticated':True, 'operator_principal':'operator:fixture'}})
                for _ in range(100):
                    route = await store.get_assistant_composite_route(
                        stream_id='fixture-host:assistant', input_identity='logical-direct')
                    if route and route['dispatch_id'] and route['delivery_state'] not in {'pending', 'intent', 'queued'}:
                        break
                    await asyncio.sleep(.01)
                envelope = json.loads(route['route_json'])['direct_envelope']
                assert provider.pastes == [envelope['wire_body']]
                assert route['delivery_state'] == 'landed'
                assert not await composite.front_desk_digest._rows(target)
                publication = await composite.publish({
                    'request_id':'publish:'+route['dispatch_id'],
                    'composite_stream_id':'fixture-host:assistant', 'dispatch_id':route['dispatch_id'],
                    'reply_to_message_id':'logical-direct', 'reply_to_question_id':None,
                    'publish_kind':'prose', 'response_state':'final', 'message':'**The answer**',
                    'attachment_ids':[], 'evidence_refs':[]}, actor_stream_id=target)
                assert publication['duplicate'] is False
                # A differently formatted provider final exercises structured
                # turn correlation rather than exact-text deduplication.
                source_seq = await ingest('assistant', 'The answer', 'dispatch-final')
                assert await store.assistant_mirror_event_for_source(source_seq) is None
                canonical = await store.fetch_session_event_tail('fixture-host:assistant', limit=100)
                answers = [e for e in canonical if e['kind'] == 'ASSIST_TEXT']
                assert len(answers) == 1 and answers[0]['text'] == '**The answer**'
                publications = await store.submit(lambda conn: list(conn.execute(
                    'SELECT publication_key FROM v2_assistant_composite_publications WHERE stream_id=?',
                    ('fixture-host:assistant',))))
                assert len(publications) == 1
                # A peer can copy every byte of the daemon header; wire-private
                # fields are stripped and must not authorize that tell.
                reply = await server._dispatch(json.dumps({'type':'tell', 'to_stream_id':target, 'tell_id':'peer-copy',
                    'message':envelope['wire_body'], 'from_stream_id':'fixture-host:peer',
                    '_assistant_composite_backend_dispatch':True}))
                assert reply[0]['type'] == 'tell.ok', reply
                assert provider.pastes == [envelope['wire_body']]
                assert len(await composite.front_desk_digest._rows(target)) == 1
                # The addressed-composite tell path introduces the same private
                # marker itself. It still must not promote a peer's body.
                reply = await server._on_tell({'to_stream_id':'fixture-host:assistant',
                    'tell_id':'peer-composite-copy', 'message':envelope['wire_body'],
                    '_auth_context':{'token_verified':True, 'stream_id':'fixture-host:peer'}})
                assert reply['delivery_status'] == 'persisted'
                assert provider.pastes == [envelope['wire_body']]
                assert len(await composite.front_desk_digest._rows(target)) == 2
            finally:
                await composite.stop()
    asyncio.run(run())


@pytest.mark.parametrize('body,msg', [
    ('START: work', {}), ('END: done', {}),
    ('tree idle', {'_outbound_notice_kind':'tree_idle'}),
    ('context_advisory: h:child high', {}),
    ('[Assistant lane ruling] '+json.dumps({'state':'done','action':'spawn'}),
     {'_outbound_notice_kind':'assistant_lane_ruling_result'}),
])
def test_disabled_digest_passes_every_input_through(body,msg,monkeypatch):
    monkeypatch.setenv('PENTACLE_FRONT_DESK_DIGEST_ENABLED','0')
    async def run():
        store=Store(':memory:');store.start()
        try:
            composite=_desk(store,'h:desk','g')
            assert await composite.suppress_routine_backend_ingress(target_stream_id='h:desk',
                body=body,msg=msg,verb='tell') is None
            assert not await composite.front_desk_digest._rows('h:desk')
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('disabled,changed_generation', [(True,False),(False,False),(False,True)])
def test_old_held_rows_delivered_after_restart_or_disable(tmp_path,monkeypatch,disabled,changed_generation):
    from notification_answer_fixture import fixture
    from message_envelopes import match_message_envelope
    async def run():
        async with fixture(tmp_path,host='fixture-host') as (_notify,queue,comms,provider,sessions,store):
            target='fixture-host:v2-test'
            generation=sessions.get(target)['session_generation']
            composite=_desk(store,target,generation)
            comms.assistant_ingress_policy=composite.suppress_routine_backend_ingress
            comms.front_desk_digest=queue.front_desk_digest=composite.front_desk_digest
            await comms.tell({'stream_id':target,'message':'END: old held item','tell_id':'old-held'})
            if changed_generation:
                await store.submit(lambda conn: (conn.execute(
                    'UPDATE v2_outbound_notices SET metadata=? WHERE kind=?',
                    (json.dumps({'root_generation':'prior-generation'}),HELD_KIND)),conn.commit()))
            store.stop();store.start()
            # Reconstruct the feature object, as daemon startup does.
            composite=_desk(store,target,generation)
            comms.assistant_ingress_policy=composite.suppress_routine_backend_ingress
            comms.front_desk_digest=queue.front_desk_digest=composite.front_desk_digest
            if disabled:
                monkeypatch.setenv('PENTACLE_FRONT_DESK_DIGEST_ENABLED','false')
            else:
                monkeypatch.setenv('PENTACLE_FRONT_DESK_DIGEST_S','0')
            assert await queue.drain_once(force=True)==1
            assert not await composite.front_desk_digest._rows(target)
            assert len(provider.pastes)==1
            assert match_message_envelope(provider.pastes[0])['lanes'][0]['text']=='END: old held item'
            assert await queue.drain_once(force=True)==0
    asyncio.run(run())
