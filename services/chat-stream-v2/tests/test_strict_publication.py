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
        landed = datetime.fromisoformat(json.loads(route['route_json'])['timings']['landed_at'].replace('Z','+00:00')).timestamp()
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


def test_existing_reconciler_marks_landed_unanswered():
    from assistant_lane_rulings import AssistantLaneRulings
    from types import SimpleNamespace
    import json
    async def body(h):
        route = await h.route('existing-pass')
        def old_landing(conn):
            payload = json.loads(route['route_json'])
            payload['timings']['landed_at'] = '2000-01-01T00:00:00Z'
            conn.execute('UPDATE v2_assistant_composite_routes SET route_json=? WHERE route_id=?',
                         (json.dumps(payload),route['route_id']))
            conn.commit()
        await h.store.submit(old_landing)
        manager = AssistantLaneRulings(SimpleNamespace(store=h.store,assistant_composite=h.composite,broadcast=h.broadcast))
        await manager.tick()
        assert [r.get('publish_kind') for r in await h.answers()] == ['unanswered']
        await manager.tick()
        assert len(await h.answers()) == 1
    run(body)


@pytest.mark.parametrize('note_type,kind', [('result',k) for k in ['message','lane','report','wake','tell']] + [('problem','message'),('reminder','wake'),('relay','tell')])
def test_reference_kinds_roundtrip(note_type,kind):
    import json
    from test_assistant_duplicate_reply_sources import _proactive
    async def body(h):
        await h.route('record')
        def fixtures(c):
            c.execute("INSERT INTO v2_assistant_composite_lanes(lane_id,stream_id,phase,created_at,updated_at) VALUES('record',?,'discussion','now','now')",(h.composite.config.stream_id,))
            c.execute("INSERT INTO v2_reports(report_id,from_stream_id,status,ingested_at,created_at) VALUES('record','fixture-root:visible','done','now',0)")
            c.execute("INSERT INTO v2_watch_wake(id,kind,owner,owner_generation,request_id,fingerprint,state,data) VALUES('record','test','fixture-root:visible','g','r','f','pending','{}')")
            c.execute("INSERT INTO v2_tell_deliveries(tell_id,reply,created_at) VALUES('record','{}',0)")
            c.commit()
        await h.store.submit(fixtures)
        msg = _proactive(h.generation,'note','A deliberate note')
        msg.update(note_type=note_type,ref=kind+':record')
        await h.composite.publish(msg,actor_stream_id='fixture-root:visible')
        row = (await h.answers())[0]
        assert (row['note_type'],row['ref']) == (note_type,kind+':record')
        pub = await h.store.submit(lambda c:c.execute("SELECT canonical_payload_json FROM v2_assistant_composite_publications WHERE publication_key='note'").fetchone()[0])
        assert json.loads(pub)['note_type'] == note_type
        assert json.loads(pub)['ref'] == kind+':record'
    run(body)


@pytest.mark.parametrize('kind',['prose','status'])
def test_duplicate_time_boundaries(kind,monkeypatch):
    import strict_publication
    from datetime import datetime,timezone
    from test_assistant_duplicate_reply_sources import _proactive
    async def body(h):
        await h.route('record')
        first = _proactive(h.generation,'first','**ＦＯＯ**   _bar_')
        first.update(note_type='result',ref='message:record',publish_kind=kind)
        await h.composite.publish(first,actor_stream_id='fixture-root:visible')
        stamp = await h.store.submit(lambda c:c.execute("SELECT created_at FROM v2_assistant_composite_publications WHERE publication_key='first'").fetchone()[0])
        now = datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
        next_msg = dict(first,request_id='second',message='foo BAR')
        monkeypatch.setattr(strict_publication.time,'time',lambda:now+3599)
        with pytest.raises(ValueError,match='assistant_publish_duplicate_text'):
            await h.composite.publish(next_msg,actor_stream_id='fixture-root:visible')
        monkeypatch.setattr(strict_publication.time,'time',lambda:now+3660)
        await h.composite.publish(next_msg,actor_stream_id='fixture-root:visible')
    run(body)


def test_batch_closing_receipt_does_not_invent_empty_publication_text():
    """Attachment-only proactive notes must not match a textless batch receipt."""
    from test_assistant_duplicate_reply_sources import _final
    from strict_publication import validate_publication

    async def body(h):
        head = await h.route('batch-head')
        await h.route('batch-member')
        result = await h.composite.publish(
            _final(head['dispatch_id'], 'batch-head', 'Nonempty batch answer',
                   reply_to_message_ids=['batch-head', 'batch-member']),
            actor_stream_id='fixture-root:visible')
        assert len(result['closed_dispatch_ids']) == 1
        assert [row['text'] for row in await h.answers()] == ['Nonempty batch answer']
        # This is the transactional validator used for a proactive file with an
        # empty caption; attachments are validated separately before this point.
        payload = dict(note_type='result', ref='message:batch-head', message='')
        await h.store.submit(lambda conn: validate_publication(
            conn, h.composite.config.stream_id, '', payload))
        with pytest.raises(ValueError, match='assistant_publish_duplicate_text'):
            await h.store.submit(lambda conn: validate_publication(
                conn, h.composite.config.stream_id, '',
                dict(payload, message='Nonempty batch answer')))
    run(body)


@pytest.mark.parametrize('enabled', [False, True])
def test_periodic_reconciliation_covers_nonprimary_strict_composite(enabled):
    import asyncio
    import json
    from dataclasses import replace
    from types import SimpleNamespace
    from assistant_composite import AssistantComposite
    from strict_publication import reconcile_unanswered

    async def body(h):
        async def dispatch(route):
            return {'delivery': 'landed'}
        daff = AssistantComposite(h.store, config=replace(
            h.composite.config, name='daff', stream_id='daff:assistant',
            strict_publication_default=False), dispatch=dispatch)
        try:
            await daff.ensure_projection()
            await h.store.put('assistant.strict_publication.daff.enabled', 'on' if enabled else 'off')
            await daff.accept_input({'message': 'Question', 'msg_id': 'daff-unanswered',
                                     'request_id': 'daff-unanswered'},
                                    operator_principal='operator:fixture')
            for _ in range(200):
                route = await h.store.get_assistant_composite_route(
                    stream_id='daff:assistant', input_identity='daff-unanswered')
                if route and route['delivery_state'] == 'landed':
                    break
                await asyncio.sleep(.01)
            assert route['delivery_state'] == 'landed'
            def age(conn):
                payload = json.loads(route['route_json'])
                payload['timings']['landed_at'] = '2000-01-01T00:00:00Z'
                conn.execute('UPDATE v2_assistant_composite_routes SET route_json=? WHERE route_id=?',
                             (json.dumps(payload), route['route_id']))
                conn.commit()
            await h.store.submit(age)
            server = SimpleNamespace(store=h.store, assistant_composite=h.composite,
                assistant_composites={h.composite.config.name: h.composite, 'daff': daff},
                broadcast=h.broadcast)
            await reconcile_unanswered(server)
            await reconcile_unanswered(server)
            rows = await h.store.fetch_session_event_tail('daff:assistant', limit=100)
            markers = [row for row in rows if row.get('publish_kind') == 'unanswered']
            assert len(markers) == int(enabled)
            if enabled:
                assert markers[0]['reply_to_message_id'] == 'daff-unanswered'
        finally:
            await daff.stop()
    run(body)
