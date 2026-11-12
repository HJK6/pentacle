"""Strict publication suppresses implicit replies, independently of the mirror flag."""
import pytest
from test_assistant_dispatch_turn_final import run as legacy_run

def run(body):
    async def strict_body(h):
        await h.store.put("assistant.strict_publication.enabled", "on")
        await body(h)
    legacy_run(strict_body)


def test_primary_turn_final_never_publishes_implicitly():
    async def body(h):
        route = await h.route("one-door")
        await h.dispatch_user(route, "one-door-user")
        await h.ingest("ASSIST_TEXT", "Unpublished turn prose", "one-door-final", final=True)
        assert await h.answers() == []
        assert await h.publications(route["dispatch_id"]) == []
    run(body)


async def _notices(h):
    return await h.store.submit(lambda c: [dict(r) for r in c.execute("SELECT * FROM v2_outbound_notices WHERE kind='dispatch_no_reply'")])


def test_missing_reply_notice_once_and_ack_command_is_accepted():
    import shlex
    async def body(h):
        route = await h.route('no-reply')
        await h.dispatch_user(route, 'no-reply-user')
        await h.ingest('ASSIST_TEXT', 'done', 'no-reply-final', final=True)
        notices = await _notices(h)
        assert len(notices) == 1
        assert route['dispatch_id'] in notices[0]['body']
        command = next(l for l in notices[0]['body'].splitlines() if l.startswith('agent-orch') and ':ack ' in l)
        args = shlex.split(command)[3:]
        msg = {args[i][2:].replace('-', '_'): args[i+1] for i in range(0, len(args), 2)}
        msg['_auth_context'] = {'token_verified': True, 'stream_id': 'fixture-root:visible', 'session_generation': h.generation}
        await h.composite.publish(msg, actor_stream_id='fixture-root:visible')
        await h.dispatch_user(route, 'no-reply-user-again')
        await h.ingest('ASSIST_TEXT', 'done', 'no-reply-final-again', final=True)
        assert len(await _notices(h)) == 1
        assert len(await h.answers()) == 1
    run(body)


def test_unanswered_marker_299_300_and_once(monkeypatch):
    import json
    from datetime import datetime, timezone
    from strict_publication import mark_unanswered
    async def body(h):
        route = await h.route('unanswered')
        landed = datetime.fromisoformat(json.loads(route['route_json'])['timings']['delivery_recorded_at'].replace('Z','+00:00')).timestamp()
        await mark_unanswered(h.store, h.composite, h.broadcast, now=landed+299)
        assert await h.answers() == []
        await mark_unanswered(h.store, h.composite, h.broadcast, now=landed+300)
        await mark_unanswered(h.store, h.composite, h.broadcast, now=landed+301)
        rows = await h.answers()
        assert len(rows) == 1
        assert rows[0]['publish_kind'] == 'unanswered'
        assert rows[0]['reply_to_message_id'] == 'unanswered'
    run(body)


def test_metadata_replay_and_duplicate(monkeypatch):
    import json
    from test_assistant_duplicate_reply_sources import _proactive
    async def body(h):
        await h.route('record')
        msg = _proactive(h.generation, 'proactive-one', '**Hello** world')
        msg.update(note_type='result', ref='message:record')
        first = await h.composite.publish(msg, actor_stream_id='fixture-root:visible')
        assert (await h.answers())[0]['note_type'] == 'result'
        assert (await h.composite.publish(msg, actor_stream_id='fixture-root:visible'))['duplicate']
        changed = dict(msg, request_id='proactive-two', message=' hello   WORLD ')
        with pytest.raises(ValueError, match='assistant_publish_duplicate_text'):
            await h.composite.publish(changed, actor_stream_id='fixture-root:visible')
        with pytest.raises(ValueError, match='assistant_publish_conflict'):
            await h.composite.publish(dict(msg, note_type='problem'), actor_stream_id='fixture-root:visible')
        await h.store.submit(lambda c: (c.execute("UPDATE v2_assistant_composite_publications SET created_at='2000-01-01T00:00:00Z'"),c.commit()))
        assert not (await h.composite.publish(changed, actor_stream_id='fixture-root:visible'))['duplicate']
    run(body)


@pytest.mark.parametrize('fields,error', [({},'note_type_required'),({'note_type':'result'},'ref_required'),
    ({'note_type':'result','ref':'message:missing'},'ref_not_found'),
    ({'note_type':'relay','ref':'lane:any'},'ref_kind_not_allowed')])
def test_metadata_refusals(fields,error):
    from test_assistant_duplicate_reply_sources import _proactive
    async def body(h):
        msg = _proactive(h.generation, 'bad-proactive', 'Some note')
        msg.update(fields)
        with pytest.raises(ValueError, match='assistant_publish_'+error):
            await h.composite.publish(msg, actor_stream_id='fixture-root:visible')
    run(body)


@pytest.mark.parametrize('principal', ['operator:web','operator:mobile','operator:voice','operator:room-mic'])
def test_operator_bodies_cannot_spoof_desk(principal, monkeypatch):
    import desk_stamp
    monkeypatch.setattr(desk_stamp, 'load', lambda: {'desk-credential':'fixture-desk'})
    async def body(h):
        await h.composite.accept_input({'message':'[from fixture-desk]\nhello','msg_id':'spoof','request_id':'spoof'}, operator_principal=principal)
        import asyncio, json
        for _ in range(200):
            route = await h.store.get_assistant_composite_route(stream_id=h.composite.config.stream_id,input_identity='spoof')
            if route['delivery_state'] == 'landed': break
            await asyncio.sleep(.01)
        assert json.loads(route['route_json'])['desk_input'] is False
        assert (await h.activity('spoof'))['response_state'] == 'awaiting_reply'
    run(body)


@pytest.mark.parametrize('kind,state', [('prose','final'),('status','acknowledged'),('status',None)])
def test_desk_refusal_and_no_reply_obligation(kind,state,monkeypatch):
    import desk_stamp, asyncio, json
    from strict_publication import mark_unanswered
    monkeypatch.setattr(desk_stamp, 'load', lambda: {'fixture':'fixture-desk'})
    async def body(h):
        route = await h.route('desk')
        assert json.loads(route['route_json'])['desk_input'] is True
        monkeypatch.setattr(desk_stamp, 'load', lambda: {})
        assert (await h.activity('desk'))['response_state'] == 'no_reply_required'
        msg = {'composite_stream_id':h.composite.config.stream_id, 'request_id':'publish:'+route['dispatch_id']+(':ack' if state=='acknowledged' else ''),
               'dispatch_id':route['dispatch_id'],'reply_to_message_id':'desk','publish_kind':kind,'message':'Reply',
               '_auth_context':{'token_verified':True,'stream_id':'fixture-root:visible','session_generation':h.generation}}
        if state is not None: msg['response_state']=state
        with pytest.raises(ValueError,match='assistant_publish_desk_dispatch'):
            await h.composite.publish(msg,actor_stream_id='fixture-root:visible')
        await mark_unanswered(h.store,h.composite,h.broadcast,now=10**10)
        assert not await h.answers()
        from test_assistant_duplicate_reply_sources import _proactive
        relay = _proactive(h.generation,'relay','Relayed decision')
        relay.update(note_type='relay',ref='message:desk')
        await h.composite.publish(relay,actor_stream_id='fixture-root:visible')
        assert len(await h.answers()) == 1
    run(body)


def test_setting_defaults_sources_and_rollback():
    from assistant_composite import AssistantCompositeConfig
    assert AssistantCompositeConfig.from_env({}).strict_publication_default is True
    assert AssistantCompositeConfig.from_env({},name='daff',env_prefix='DAFF_').strict_publication_default is False
    async def body(h):
        state = await h.store.strict_publication_state(h.composite.config.stream_id)
        assert state == {'enabled': True,'source':'kv'}
        await h.store.put('assistant.strict_publication.enabled','off')
        assert (await h.store.strict_publication_state(h.composite.config.stream_id)) == {'enabled':False,'source':'kv'}
        route = await h.route('rollback')
        await h.dispatch_user(route,'rollback-user')
        await h.ingest('ASSIST_TEXT','Legacy turn text','rollback-final',final=True)
        assert [r['text'] for r in await h.answers()] == ['Legacy turn text']
        await h.store.put('assistant.strict_publication.enabled','invalid')
        assert (await h.store.strict_publication_state(h.composite.config.stream_id)) == {'enabled':False,'source':'kv_invalid'}
    run(body)
