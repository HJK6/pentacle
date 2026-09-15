"""Saved answers use the existing durable queue, including crash ambiguity."""
import asyncio
from contextlib import asynccontextmanager
import pytest
from comms import Comms
from notify import Notify, ANSWER_TELL_ID_PREFIX
from outbound_notices import OutboundNoticeQueue, OutboundNoticeConfig
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from test_notify_answer_resolution import _seed_live_shaped_question

from notification_answer_fixture import fixture

async def answer(notify, question, **changes):
    return await notify.notification({'type':'notification.resolve','notification_id':question['notification_id'],
        'action_kind':'yes_no','choice':True,'_auth_context':{'operator_authenticated':True,'connection_client':'pentacle-mobile'},**changes})

async def state(notify, question):
    return await notify._db.call('get_notification',question['notification_id'])

def test_saved_ack_precedes_provider_and_same_retry_does_not_dispatch_twice(tmp_path):
    async def run():
        async with fixture(tmp_path) as (notify,queue,comms,provider,sessions,store):
            q=await _seed_live_shaped_question(notify)
            reply=await answer(notify,q)
            assert reply['notification']['resolution']['delivery_status']=='pending'
            assert provider.pastes==[]
            assert await queue.drain_once(force=True)==1
            assert (await state(notify,q))['resolution']['delivery_status']=='delivered'
            confirmed=await store.get_tell_delivery(ANSWER_TELL_ID_PREFIX+q['notification_id'])
            assert confirmed['reply']['submission_attempts']==1
            assert confirmed['delivery']['submission_attempts']==1
            assert (await notify._db.call('get_agent_question',q['question_id']))['state']=='consumed'
            assert (await answer(notify,q))['replayed'] is True
            assert (await answer(notify,q,choice=False))['type']=='notification.error'
            await queue.drain_once(force=True)
            assert len(provider.pastes)==1
    asyncio.run(run())

def test_restart_bridges_saved_owned_answer_without_queue_row(tmp_path):
    async def run():
        async with fixture(tmp_path) as (notify,queue,comms,provider,sessions,store):
            q=await _seed_live_shaped_question(notify)
            original=queue.enqueue
            async def crash(**kwargs): raise asyncio.CancelledError
            queue.enqueue=crash
            with pytest.raises(asyncio.CancelledError): await answer(notify,q)
            assert (await state(notify,q))['resolution']['v2_answer_delivery']
            queue.enqueue=original
            await notify.stop()
            restarted=Notify(str(tmp_path/'notifications.db'),comms=comms,sessions=sessions,notice_store=store,outbound=queue)
            await restarted.start()
            try:
                await queue.drain_once(force=True)
                assert len(provider.pastes)==1
                assert (await state(restarted,q))['resolution']['delivery_status']=='delivered'
            finally: await restarted.stop()
    asyncio.run(run())

@pytest.mark.parametrize('before', [False,True])
def test_cancelled_paste_boundary_reconciles_never_repeats_and_late_proof_promotes(tmp_path,before):
    async def run():
        async with fixture(tmp_path) as (notify,queue,comms,provider,sessions,store):
            q=await _seed_live_shaped_question(notify);await answer(notify,q)
            provider.pause_before=before;provider.pause_after=not before
            task=asyncio.create_task(queue.drain_once(force=True));await asyncio.wait_for(provider.entered.wait(),2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            intent=await store.get_tell_delivery(ANSWER_TELL_ID_PREFIX+q['notification_id'])
            assert intent['reply']['action_committed'] is False
            assert intent['reply']['submission_attempts']==0
            assert intent['delivery']['submission_attempts']==0
            assert intent['reply']['delivery_status']=='proof_unavailable'
            await asyncio.sleep(.12)
            await queue.drain_once(force=True)
            if before:
                assert provider.pastes==[]
                current=await state(notify,q)
                assert current['resolution']['delivery_status']=='unconfirmed'
                assert current['resolution']['delivery_reason']=='unconfirmed_after_bound'
                await provider.user(intent['delivery']['text'])
                await notify.recover_once()
            else:
                assert len(provider.pastes)==1
            assert (await state(notify,q))['resolution']['delivery_status']=='delivered'
    asyncio.run(run())

def test_replaced_producer_fails_before_input_and_foreign_answer_cannot_enqueue(tmp_path):
    async def run():
        async with fixture(tmp_path) as (notify,queue,comms,provider,sessions,store):
            q=await _seed_live_shaped_question(notify);await answer(notify,q)
            await sessions.open('hosta','v2-test',provider='codex',visibility='visible')
            await queue.drain_once(force=True)
            assert provider.pastes==[]
            assert (await state(notify,q))['resolution']['delivery_status']=='failed'
            foreign=await _seed_live_shaped_question(notify,question_id='q-foreign')
            await notify._db.call('resolve_notification',foreign['notification_id'],action_kind='yes_no',by='operator',choice=True,selections=['approve'])
            await answer(notify,foreign,selections=['approve'])
            await notify.recover_once();await queue.drain_once(force=True)
            assert provider.pastes==[]
            assert 'v2_answer_delivery' not in (await state(notify,foreign))['resolution']
    asyncio.run(run())

def test_dedup_dismissal_has_no_answer_intent_and_cannot_replace_an_answer(tmp_path):
    async def run():
        async with fixture(tmp_path) as (notify,queue,comms,provider,sessions,store):
            q=await _seed_live_shaped_question(notify,question_id='q-dismiss')
            n=await state(notify,q)
            request={'type':'notification.resolve_by_dedup','producer':n['producer'],'dedup_key':n['dedup_key'],
                     '_auth_context':{'operator_authenticated':True}}
            result=await notify.notification(request)
            assert result['resolved'] is True
            assert 'v2_answer_delivery' not in result['notification']['resolution']
            assert await queue.drain_once(force=True)==0
            assert provider.pastes==[]
            answered=await _seed_live_shaped_question(notify,question_id='q-already-answered')
            await answer(notify,answered)
            original=await state(notify,answered)
            result=await notify.notification({**request,'dedup_key':original['dedup_key']})
            assert result['resolved'] is False
            assert (await state(notify,answered))['resolution']==original['resolution']
            conflict=await answer(notify,answered,choice=False)
            assert conflict['type']=='notification.error'
            await queue.drain_once(force=True)
            assert len(provider.pastes)==1
    asyncio.run(run())

@pytest.mark.parametrize('mode', ['single_choice','free_text'])
def test_prompt_answer_uses_same_owned_queue_and_pending_ack(tmp_path,mode):
    async def run():
        async with fixture(tmp_path) as (notify,queue,comms,provider,sessions,store):
            if mode=='single_choice':
                q=await _seed_live_shaped_question(notify)
                selection={'selections':['approve']}
            else:
                envelope={'schema_version':1,'question_id':'q-text','title':'Owned free text','body':'Answer text',
                          'dedup_key':'owned-text','producer_stream_id':'hosta:v2-test','response_mode':'free_text','options':[]}
                q=await notify._db.call('create_agent_question',envelope=envelope,actions=[])
                selection={'text':'the owned answer'}
            request={'type':'prompt.answer','question_id':q['question_id'],'_auth_context':{'operator_authenticated':True},**selection}
            result=await notify.prompt(request)
            assert result['type']=='prompt.answer.ok'
            assert result['notification']['resolution']['delivery_status']=='pending'
            assert provider.pastes==[]
            assert (await notify.prompt(request))['already_answered'] is True
            await queue.drain_once(force=True)
            assert len(provider.pastes)==1
            assert (await state(notify,q))['resolution']['delivery_status']=='delivered'
    asyncio.run(run())

def test_tell_retention_cannot_erase_possible_answer_input(tmp_path,monkeypatch):
    async def run():
        async with fixture(tmp_path) as (notify,queue,comms,provider,sessions,store):
            q=await _seed_live_shaped_question(notify);await answer(notify,q)
            provider.pause_before=True
            task=asyncio.create_task(queue.drain_once(force=True));await provider.entered.wait();task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            monkeypatch.setattr('store.TELL_RETENTION',1)
            for i in range(3):await store.put_tell_delivery('unrelated-'+str(i),{'reply':{},'delivery':{}})
            assert await store.get_tell_delivery(ANSWER_TELL_ID_PREFIX+q['notification_id']) is not None
            await asyncio.sleep(.12);await queue.drain_once(force=True)
            assert provider.pastes==[]
            assert (await state(notify,q))['resolution']['delivery_status']=='unconfirmed'
    asyncio.run(run())

@pytest.mark.parametrize('entrypoint', ['notification.resolve', 'prompt.answer'])
@pytest.mark.parametrize('transition', ['replace', 'close'])
@pytest.mark.parametrize('stamped', [True,False])
def test_answer_stamp_keeps_asking_generation_across_selection_lookup(tmp_path,entrypoint,transition,stamped):
    async def run():
        async with fixture(tmp_path) as (notify,queue,comms,provider,sessions,store):
            sid='hosta:v2-test'
            original_generation=sessions.get(sid)['session_generation']
            envelope={'schema_version':1,'question_id':'q-generation-interleave','title':'Select a result',
                      'body':'Choose the result.','dedup_key':'generation-interleave','producer_stream_id':sid,
                      'producer_session_generation':original_generation,'response_mode':'single_choice',
                      'options':[{'label':'Approve','value':'approve'}]}
            if not stamped:envelope.pop('producer_session_generation')
            actions=[{'kind':'yes_no','action_id':'a0','label':'Approve','choice':True,
                      'value':{'schema_version':1,'question_id':envelope['question_id'],'answer':'approve'}}]
            q=await notify._db.call('create_agent_question',envelope=envelope,actions=actions)
            original_call=notify._db.call
            interleaved=False
            async def call(method,*args,**kwargs):
                nonlocal interleaved
                result=await original_call(method,*args,**kwargs)
                if method=='get_notification' and not interleaved:
                    interleaved=True
                    if transition=='replace':
                        await sessions.open('hosta','v2-test',provider='codex',visibility='visible')
                    else:
                        await sessions.mark_closed('hosta','v2-test',expected_generation=original_generation)
                return result
            notify._db.call=call
            request={'type':entrypoint,'notification_id':q['notification_id'],'question_id':q['question_id'],
                     'action_kind':'yes_no','selections':['approve'],'_auth_context':{'operator_authenticated':True}}
            reply=await (notify.notification(request) if entrypoint=='notification.resolve' else notify.prompt(request))
            assert interleaved
            assert reply['type']==entrypoint+'.ok'
            saved=await state(notify,q)
            assert saved['resolution']['v2_answer_delivery']['producer_session_generation']==original_generation
            await queue.drain_once(force=True)
            final=await state(notify,q)
            assert final['resolution']['delivery_status']=='failed'
            assert final['resolution']['delivery_reason']=='question_producer_gone'
            assert provider.pastes==[]
            assert await store.get_tell_delivery(ANSWER_TELL_ID_PREFIX+q['notification_id']) is None
    asyncio.run(run())


def test_late_proof_holds_original_generation_until_lookup_finishes(tmp_path):
    async def run():
        async with fixture(tmp_path) as (notify,queue,comms,provider,sessions,store):
            q=await _seed_live_shaped_question(notify)
            await answer(notify,q)
            provider.pause_before=True
            task=asyncio.create_task(queue.drain_once(force=True))
            await provider.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            await asyncio.sleep(.12)
            await queue.drain_once(force=True)
            assert (await state(notify,q))['resolution']['delivery_status']=='unconfirmed'
            original_tail=store.fetch_session_event_tail
            replacement=None
            blocked=None
            async def tail(*args,**kwargs):
                nonlocal replacement,blocked
                if replacement is None:
                    replacement=asyncio.create_task(sessions.open('hosta','v2-test',provider='codex',visibility='visible'))
                    await asyncio.sleep(.03)
                    blocked=not replacement.done()
                return await original_tail(*args,**kwargs)
            store.fetch_session_event_tail=tail
            await notify.recover_once()
            assert replacement is not None
            await replacement
            assert blocked is True
            intent=await store.get_tell_delivery(ANSWER_TELL_ID_PREFIX+q['notification_id'])
            await provider.user(intent['delivery']['text'])
            await notify.recover_once()
            assert (await state(notify,q))['resolution']['delivery_status']=='unconfirmed'
            assert provider.pastes==[]
    asyncio.run(run())


def test_save_diagnostic_precedes_queue_and_omits_answer(tmp_path,caplog):
    async def run():
        async with fixture(tmp_path) as (notify,queue,comms,provider,sessions,store):
            q=await _seed_live_shaped_question(notify)
            async def crash(**kwargs): raise asyncio.CancelledError
            queue.enqueue=crash
            with caplog.at_level('INFO',logger='chat_streamd_v2.notify'):
                with pytest.raises(asyncio.CancelledError):
                    await answer(notify,q,request_id='request-save-diagnostic',selections=['approve'],note='private answer contents')
            assert 'answer saved nid='+q['notification_id'] in caplog.text
            assert 'request_id=request-save-diagnostic' in caplog.text
            assert 'private answer contents' not in caplog.text
    asyncio.run(run())


@pytest.mark.parametrize('sent',[True,False])
def test_answer_response_diagnostic_is_socket_outcome_only(caplog,sent):
    import json
    from server import Server
    async def run():
        server=Server(port=0)
        async def dispatch(*args,**kwargs):
            return [{'type':'notification.resolve.ok','request_id':'request-send-diagnostic',
                     'notification':{'notification_id':'diagnostic-notification','resolution':{'text':'private answer contents'}}}]
        async def send(*args,**kwargs): return sent
        server._dispatch=dispatch
        server._send_direct=send
        with caplog.at_level('INFO',logger='chat_streamd_v2.server'):
            await server._serve(object(),json.dumps({'type':'notification.resolve','request_id':'request-send-diagnostic'}))
        assert 'request_id=request-send-diagnostic' in caplog.text
        assert 'socket_send='+('completed' if sent else 'failed') in caplog.text
        assert 'private answer contents' not in caplog.text
    asyncio.run(run())
