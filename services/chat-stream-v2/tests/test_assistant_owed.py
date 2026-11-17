"""Owed items stay separate from immutable publication payloads."""
import asyncio
import json
from dataclasses import replace
import pytest
from assistant_composite import AssistantComposite
from store import Store
from test_assistant_dispatch_turn_final import Harness
from test_assistant_prose_mirror import ASSISTANT, ROOT, _config


def run(tmp_path, body):
    async def go():
        store=Store(str(tmp_path/'store.db'));store.start()
        composite=None
        try:
            seat=await store.open_session(*ROOT.split(':',1),provider='claude',pane_pid='4242')
            async def dispatch(route): return {'delivery':'landed'}
            composite=AssistantComposite(store,config=replace(_config(seat['session_generation']),strict_publication_default=True),dispatch=dispatch)
            await composite.ensure_projection()
            await body(Harness(store,composite,seat['session_generation']))
        finally:
            if composite: await composite.stop()
            store.stop()
    asyncio.run(go())


def reply(h,r,**fields):
    return dict(type='assistant.publish',request_id='publish:'+r['dispatch_id'],composite_stream_id=ASSISTANT,
        dispatch_id=r['dispatch_id'],reply_to_message_id=r['input_identity'],publish_kind='prose',response_state='final',
        message='The result will follow',_auth_context={'token_verified':True,'stream_id':ROOT,'session_generation':h.generation},**fields)


def test_open_replay_and_canonical_payload(tmp_path):
    async def body(h):
        r=await h.route('request-a');m=reply(h,r,owes='result',owes_due='+1h')
        first=await h.composite.publish(m,actor_stream_id=ROOT)
        assert first['owed_id'].startswith('owed-')
        assert (await h.composite.publish(m,actor_stream_id=ROOT))['owed_id']==first['owed_id']
        assert (await h.composite.publish(reply(h,r),actor_stream_id=ROOT))['owed_id']==first['owed_id']
        with pytest.raises(ValueError,match='assistant_publish_conflict'):
            await h.composite.publish(dict(m,owes='different'),actor_stream_id=ROOT)
        pub=(await h.publications(r['dispatch_id']))[0]
        assert not {'owes','owes_due','closes'} & json.loads(pub['canonical_payload_json']).keys()
    run(tmp_path,body)


def test_required_switch_none_and_duplicate(tmp_path,monkeypatch):
    monkeypatch.setenv('PENTACLE_ASSISTANT_OWES_REQUIRED','1')
    async def body(h):
        r=await h.route('request-a')
        with pytest.raises(ValueError,match='assistant_publish_owes_required'):
            await h.composite.publish(reply(h,r),actor_stream_id=ROOT)
        assert (await h.composite.publish(reply(h,r,owes='none'),actor_stream_id=ROOT))['owed_id'] is None
        assert (await h.composite.publish(reply(h,r),actor_stream_id=ROOT))['duplicate']
    run(tmp_path,body)


@pytest.mark.parametrize('due',['yesterday','2020-01-01T00:00:00Z','2035-01-01T00:00:00'])
def test_invalid_due_is_atomic(tmp_path,due):
    async def body(h):
        r=await h.route('request-a')
        with pytest.raises(ValueError,match='assistant_owed_due_invalid'):
            await h.composite.publish(reply(h,r,owes='result',owes_due=due),actor_stream_id=ROOT)
        assert await h.publications(r['dispatch_id'])==[]
    run(tmp_path,body)


def auth(h): return {'token_verified':True,'stream_id':ROOT,'session_generation':h.generation}
async def owed(h,action,**args):
    return await h.composite.owed.request(action,args,auth(h))
def note(h,key,**fields):
    return dict(type='assistant.publish',request_id=key,composite_stream_id=ASSISTANT,publish_kind='prose',
        message='Completed '+key,note_type='result',_auth_context=auth(h),**fields)


@pytest.mark.parametrize('case',['off','scoped','outside','desk','batch_member','ack','note'])
def test_required_exclusions(tmp_path,monkeypatch,case):
    monkeypatch.setenv('PENTACLE_ASSISTANT_OWES_REQUIRED','0' if case=='off' else '1')
    async def body(h):
        r=await h.route('request-a');m=reply(h,r)
        if case in {'scoped','outside','desk','batch_member'}:
            target=r
            if case=='batch_member':
                target=await h.route('request-b');m['reply_to_message_ids']=['request-a','request-b']
            def change(conn):
                data=json.loads(target['route_json'])
                if case=='desk': data['desk_dispatch']=True
                conn.execute('UPDATE v2_assistant_composite_routes SET actor_stream_id=?,route_json=? WHERE route_id=?',
                    ('operator:test' if case=='desk' else 'scoped:test',json.dumps(data),target['route_id']));conn.commit()
            await h.store.submit(change)
            if case=='desk':
                # Existing strict publication blocks desk replies separately; test owed rule at store layer.
                from assistant_owed import operator_route
                assert await h.store.submit(lambda c:not operator_route(c,ASSISTANT,target['input_identity']))
                return
        if case=='ack': m.update(request_id=m['request_id']+':ack',publish_kind='status',response_state='acknowledged')
        if case=='note': m=note(h,'note-a',ref='message:request-a')
        result=await h.composite.publish(m,actor_stream_id=ROOT)
        assert result['owed_id'] is None
    run(tmp_path,body)


@pytest.mark.parametrize('mode',['delivered','withdrawn','superseded','void'])
def test_explicit_close_and_source_show(tmp_path,mode):
    async def body(h):
        r=await h.route('request-a')
        item=(await owed(h,'open',label='result',message_id='request-a'))['item']
        assert (await owed(h,'open',label='result',message_id='request-a'))['item']['owed_id']==item['owed_id']
        assert 'Operator question request-a' in (await owed(h,'show',owed_id=item['owed_id']))['source_text']
        if mode=='delivered':
            await h.composite.publish(reply(h,r,owes='none'),actor_stream_id=ROOT)
            args={'delivered_by':'publish:'+r['dispatch_id']}
        elif mode=='void': args={'void':True,'reason':'This item was opened in error'}
        else:
            await h.route('request-b');args={mode+'_by':'request-b'}
        closed=(await owed(h,'close',owed_id=item['owed_id'],**args))['item']
        assert closed['closed_kind']==mode and closed['state']=='closed'
        assert (await owed(h,'list'))['items']==[]
        assert (await owed(h,'list',**({'void':True} if mode=='void' else {'closed':True})))['items'][0]['owed_id']==item['owed_id']
    run(tmp_path,body)


def test_closes_atomic_and_replays(tmp_path):
    async def body(h):
        r=await h.route('request-a');first=await h.composite.publish(reply(h,r,owes='result'),actor_stream_id=ROOT)
        oid=first['owed_id'];m=note(h,'result-a',ref='owed:'+oid,closes=[oid])
        result=await h.composite.publish(m,actor_stream_id=ROOT)
        assert result['closed_owed_ids']==[oid]
        assert (await h.composite.publish(m,actor_stream_id=ROOT))['closed_owed_ids']==[oid]
        bare=dict(m);bare.pop('closes');assert (await h.composite.publish(bare,actor_stream_id=ROOT))['closed_owed_ids']==[oid]
        with pytest.raises(ValueError,match='assistant_publish_conflict'):
            await h.composite.publish(dict(m,closes=[]),actor_stream_id=ROOT)
        with pytest.raises(ValueError,match='assistant_owed_not_open'):
            await h.composite.publish(note(h,'result-b',ref='owed:'+oid,closes=[oid]),actor_stream_id=ROOT)
    run(tmp_path,body)


@pytest.mark.parametrize('kind',['result','problem','reminder','other','relay','started','change'])
def test_owed_ref_kinds(tmp_path,kind):
    async def body(h):
        await h.route('request-a');oid=(await owed(h,'open',label='result',message_id='request-a'))['item']['owed_id']
        m=note(h,'note-a',ref='owed:'+oid);m['note_type']=kind
        if kind=='other':m['note_reason']='Progress detail'
        if kind in {'relay','started','change'}:
            with pytest.raises(ValueError,match='assistant_publish_ref_kind_not_allowed'):await h.composite.publish(m,actor_stream_id=ROOT)
        else:await h.composite.publish(m,actor_stream_id=ROOT)
        assert (await owed(h,'show',owed_id=oid))['item']['state']=='open'
    run(tmp_path,body)


@pytest.mark.parametrize('mode',['missing','foreign','old','not_operator','reason','not_open'])
def test_closing_requires_evidence(tmp_path,mode):
    async def body(h):
        old=await h.route('old');await h.composite.publish(reply(h,old,owes='none'),actor_stream_id=ROOT)
        await h.route('request-a');oid=(await owed(h,'open',label='result',message_id='request-a'))['item']['owed_id']
        args={'delivered_by':'missing'}
        if mode=='old':args={'delivered_by':'publish:'+old['dispatch_id']}
        if mode=='reason':args={'void':True,'reason':'short'}
        if mode in {'foreign','not_operator'}:
            row=await h.route('request-b')
            def change(c):
                c.execute('UPDATE v2_assistant_composite_routes SET '+('stream_id' if mode=='foreign' else 'actor_stream_id')+'=? WHERE route_id=?',('other:seat',row['route_id']));c.commit()
            await h.store.submit(change);args={'withdrawn_by':'request-b'}
        if mode=='not_open':await owed(h,'close',owed_id=oid,void=True,reason='This was opened by mistake')
        with pytest.raises(ValueError):await owed(h,'close',owed_id=oid,**args)
    run(tmp_path,body)


def test_batch_and_publication_sources(tmp_path):
    async def body(h):
        a=await h.route('a');await h.route('b')
        result=await h.composite.publish(reply(h,a,owes='batch',reply_to_message_ids=['a','b']),actor_stream_id=ROOT)
        row=(await owed(h,'show',owed_id=result['owed_id']))['item'];assert row['source_ids']==['a','b']
        n=await h.composite.publish(note(h,'note-a',ref='message:a',owes='followup'),actor_stream_id=ROOT)
        assert (await owed(h,'show',owed_id=n['owed_id']))['item']['source_kind']=='publication'
    run(tmp_path,body)


def test_due_tick_guard_and_delivery(tmp_path):
    async def body(h):
        from outbound_notices import OutboundNoticeQueue
        queue=OutboundNoticeQueue(h.store,None)
        h.composite.owed.attach(queue)
        await h.route('a');item=(await owed(h,'open',label='result',message_id='a',due='+1m'))['item']
        def mature(c):c.execute("UPDATE v2_assistant_owed SET due_at='2000-01-01T00:00:00.000000Z'");c.commit()
        await h.store.submit(mature)
        await h.composite.owed.tick();await h.composite.owed.tick()
        rows=await h.store.submit(lambda c:[dict(r) for r in c.execute("SELECT * FROM v2_outbound_notices WHERE kind='owed_due'")])
        assert len(rows)==1 and rows[0]['notice_id']==f"owed_due:{item['owed_id']}:{ROOT}:{h.generation}"
        assert await h.composite.owed.guard(rows[0]) is None
        await h.composite.owed.delivered(rows[0]);await h.composite.owed.tick()
        assert (await owed(h,'show',owed_id=item['owed_id']))['item']['due_notice_at']
        await owed(h,'close',owed_id=item['owed_id'],void=True,reason='This item was opened in error')
        assert await h.composite.owed.guard(rows[0]) is not None
    run(tmp_path,body)


@pytest.mark.parametrize('caller,write,allowed',[('operator',True,True),('other',True,False),('other',False,False),('advisor',False,True),('advisor',True,False),('stale',True,False),('forged',False,False)])
def test_authorization(tmp_path,caller,write,allowed):
    async def body(h):
        from types import SimpleNamespace
        await h.route('a')
        a=auth(h)
        if caller in {'other','advisor'}:
            seat=await h.store.open_session('peer','seat-a',provider='claude',pane_pid='4243')
            a=dict(a,stream_id='peer:seat-a',session_generation=seat['session_generation'])
            if caller=='advisor':h.composite.owed.server=SimpleNamespace(lane_rulings=SimpleNamespace(binding_conn=lambda c:dict(state='ready',stream_id=a['stream_id'],generation=a['session_generation'])))
        if caller=='operator':a={'operator_authenticated':True,'operator_principal':'operator:fixture'}
        if caller=='stale':a['session_generation']='old'
        if caller=='forged':a={'operator_authenticated':True,'operator_principal':'agent:fixture'}
        action='open' if write else 'list';args={'label':'result','message_id':'a'} if write else {}
        if allowed:await h.composite.owed.request(action,args,a)
        else:
            with pytest.raises(ValueError):await h.composite.owed.request(action,args,a)
    run(tmp_path,body)


def test_null_owes_cannot_bypass_switch(tmp_path,monkeypatch):
    monkeypatch.setenv('PENTACLE_ASSISTANT_OWES_REQUIRED','1')
    async def body(h):
        r=await h.route('a')
        with pytest.raises(ValueError):await h.composite.publish(reply(h,r,owes=None),actor_stream_id=ROOT)
    run(tmp_path,body)


def test_open_retry_after_original_due_returns_same_item(tmp_path,monkeypatch):
    async def body(h):
        import assistant_owed as module
        await h.route('a');item=(await owed(h,'open',label='result',message_id='a',due='2030-01-01T00:00:00Z'))['item']
        monkeypatch.setattr(module,'stamp',lambda:'2031-01-01T00:00:00.000000Z')
        assert (await owed(h,'open',label='result',message_id='a',due='2030-01-01T00:00:00Z'))['item']['owed_id']==item['owed_id']
    run(tmp_path,body)


def test_due_completion_reconciles_callback_loss(tmp_path):
    async def body(h):
        from outbound_notices import OutboundNoticeQueue
        q=OutboundNoticeQueue(h.store,None);h.composite.owed.attach(q)
        await h.route('a');item=(await owed(h,'open',label='result',message_id='a',due='+1m'))['item']
        def mature(c):c.execute("UPDATE v2_assistant_owed SET due_at='2000-01-01T00:00:00.000000Z'");c.commit()
        await h.store.submit(mature);await h.composite.owed.tick()
        ids=await h.store.submit(lambda c:[r[0] for r in c.execute("SELECT notice_id FROM v2_outbound_notices WHERE kind='owed_due'")])
        assert await h.store.claim_outbound_notice(ids[0],owner='fixture',lease_s=10,force=True)
        assert await h.store.complete_outbound_notice(ids[0],owner='fixture')
        await h.composite.owed.tick()
        assert (await owed(h,'show',owed_id=item['owed_id']))['item']['due_notice_at']
    run(tmp_path,body)


def test_archived_source_and_withdrawal(tmp_path):
    async def body(h):
        import sqlite3
        import retention
        await h.route('a');item=(await owed(h,'open',label='result',message_id='a'))['item']
        await h.route('b')
        def archive(c):
            # Reproduce a historical paired archive without changing current retention policy.
            retention.initialize_history_pair(c);c.commit()
            rows=c.execute("SELECT * FROM v2_assistant_composite_routes WHERE input_identity IN ('a','b')").fetchall()
            cold=sqlite3.connect(c.history_archive_path)
            try:
                for row in rows:
                    names=list(row.keys());cold.execute('INSERT INTO v2_assistant_composite_routes('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')',tuple(row))
                cold.commit()
            finally:cold.close()
            c.execute("DELETE FROM v2_assistant_composite_routes WHERE input_identity IN ('a','b')");c.commit()
        await h.store.submit(archive)
        assert (await owed(h,'show',owed_id=item['owed_id']))['source_text']=='Operator question a'
        assert (await owed(h,'close',owed_id=item['owed_id'],withdrawn_by='b'))['item']['state']=='closed'
    run(tmp_path,body)


def test_prior_reader_opens_candidate_store(tmp_path):
    import io,os,subprocess,sys,tarfile,shutil
    from pathlib import Path
    base='5b96751a2266d450faeea01df5e2a680bd043ec8'
    repo=Path(__file__).resolve().parents[3]
    git=shutil.which('git');assert git
    prior=tmp_path/'prior';prior.mkdir()
    data=subprocess.check_output([git,'archive',base,'services/chat-stream-v2','services/_shared','scripts/lib','pentacle-chat-core'],cwd=repo)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:archive.extractall(prior,filter='data')
    async def body(h):
        r=await h.route('a');first=await h.composite.publish(reply(h,r,owes='result'),actor_stream_id=ROOT)
        await h.composite.publish(note(h,'note-a',ref='owed:'+first['owed_id']),actor_stream_id=ROOT)
    run(tmp_path,body)
    script='''
import asyncio,json,sys
from store import Store
async def run():
 s=Store(sys.argv[1]);s.start()
 try:
  rows=await s.fetch_session_event_tail(sys.argv[2],limit=20)
  assert len([r for r in rows if r['kind']=='ASSIST_TEXT'])==2
  p=await s.get_assistant_composite_publication(stream_id=sys.argv[2],publication_key='note-a')
  assert json.loads(p['canonical_payload_json'])['ref'].startswith('owed:')
  assert not {'owes','closes'} & json.loads(p['canonical_payload_json']).keys()
 finally:s.stop()
asyncio.run(run())
'''
    env=dict(os.environ,PYTHONPATH=os.pathsep.join([str(prior/'services/chat-stream-v2'),str(prior/'services')]),PYTHONDONTWRITEBYTECODE='1')
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path/'store.db'),ASSISTANT],env=env,cwd=prior,text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('value',['+1m','+1h','+1d','2035-01-01T04:00:00+04:00'])
def test_due_formats_normalize_utc(value):
    from assistant_owed import due
    assert due(value,'2026-01-01T00:00:00.000000Z').endswith('Z')


def test_unknown_ref_and_atomic_multi_close(tmp_path):
    async def body(h):
        r=await h.route('a');oid=(await h.composite.publish(reply(h,r,owes='result'),actor_stream_id=ROOT))['owed_id']
        with pytest.raises(ValueError,match='assistant_publish_ref_not_found'):
            await h.composite.publish(note(h,'missing',ref='owed:missing'),actor_stream_id=ROOT)
        with pytest.raises(ValueError):
            await h.composite.publish(note(h,'invalid-close',ref='owed:'+oid,closes=[oid,'missing']),actor_stream_id=ROOT)
        assert (await owed(h,'show',owed_id=oid))['item']['state']=='open'
    run(tmp_path,body)


def test_due_rebind_no_binding_and_digest(tmp_path,monkeypatch):
    async def body(h):
        from outbound_notices import OutboundNoticeQueue
        q=OutboundNoticeQueue(h.store,None);h.composite.owed.attach(q)
        await h.route('a');item=(await owed(h,'open',label='result',message_id='a',due='+1m'))['item']
        def mature(c):c.execute("UPDATE v2_assistant_owed SET due_at='2000-01-01T00:00:00.000000Z'");c.commit()
        await h.store.submit(mature)
        original=h.composite._env_binding
        monkeypatch.setattr(h.composite,'_env_binding',lambda:{})
        await q.drain_once()
        rows=lambda c:[dict(r) for r in c.execute("SELECT * FROM v2_outbound_notices WHERE kind='owed_due'")]
        assert await h.store.submit(rows)==[]
        monkeypatch.setattr(h.composite,'_env_binding',original)
        await h.composite.owed.tick();old=(await h.store.submit(rows))[0]
        seat=await h.store.open_session('peer','front-desk',provider='claude',pane_pid='4244')
        def bind(c):
            c.execute('INSERT OR REPLACE INTO v2_assistant_direct_binding VALUES(?,?,?,?,?)',(h.composite.config.name,'peer:front-desk',seat['session_generation'],1,'2030-01-01T00:00:00Z'));c.commit()
        await h.store.submit(bind)
        assert await h.composite.owed.guard(old) is not None
        await h.composite.owed.tick();new=(await h.store.submit(rows))[1]
        assert new['recipient_stream_id']=='peer:front-desk' and await h.composite.owed.guard(new) is None
        # Typed due notices bypass digest admission; ordinary notes do not.
        assert await h.composite.front_desk_digest.ingress(target_stream_id=ROOT,body='Due',msg={'tell_id':'owed-a','_outbound_notice_kind':'owed_due'},verb='tell') is None
    run(tmp_path,body)


@pytest.mark.parametrize('kind',['question','result','status'])
def test_nonprose_store_reply_is_excluded(tmp_path,monkeypatch,kind):
    monkeypatch.setenv('PENTACLE_ASSISTANT_OWES_REQUIRED','1')
    async def body(h):
        r=await h.route('a')
        result=await h.store.record_assistant_composite_publication(stream_id=ASSISTANT,publication_key='other-kind',dispatch_id=r['dispatch_id'],reply_to_message_id='a',reply_to_question_id=None,publish_kind=kind,attachment_ids=[],evidence_refs=[],canonical_payload={'dispatch_id':r['dispatch_id'],'reply_to_message_id':'a','publish_kind':kind,'message':'Answer'},event={'stream_id':ASSISTANT,'kind':'ASSIST_TEXT','text':'Answer'})
        assert result['owed_id'] is None
    run(tmp_path,body)


def test_pre_switch_key_replay_and_ordering(tmp_path,monkeypatch):
    async def body(h):
        r=await h.route('a');await h.composite.publish(reply(h,r),actor_stream_id=ROOT)
        monkeypatch.setenv('PENTACLE_ASSISTANT_OWES_REQUIRED','1')
        assert (await h.composite.publish(reply(h,r),actor_stream_id=ROOT))['duplicate']
        await owed(h,'open',label='undated',message_id='a')
        await owed(h,'open',label='later',message_id='a',due='+1d')
        first=await owed(h,'open',label='sooner',message_id='a',due='+1h')
        assert [r['label'] for r in (await owed(h,'list'))['items']]==['sooner','later','undated']
        await owed(h,'close',owed_id=first['item']['owed_id'],void=True,reason='This was opened in error')
        assert len((await owed(h,'list',void=True,since='2020-01-01T00:00:00Z'))['items'])==1
    run(tmp_path,body)

@pytest.mark.parametrize('partial',['owes','closes','null'])
def test_duplicate_effects_must_match_whole_set(tmp_path,partial):
    async def body(h):
        r=await h.route('request-a')
        oid=(await h.composite.publish(reply(h,r,owes='first result'),actor_stream_id=ROOT))['owed_id']
        m=note(h,'result-and-next',ref='owed:'+oid,owes='next result',closes=[oid])
        await h.composite.publish(m,actor_stream_id=ROOT)
        retry=dict(m)
        if partial=='null':retry['owes']=None
        else:retry.pop(partial)
        with pytest.raises(ValueError,match='assistant_publish_conflict'):
            await h.composite.publish(retry,actor_stream_id=ROOT)
    run(tmp_path,body)

@pytest.mark.parametrize('replacement',['generation','closed'])
def test_due_rejects_stale_live_seat_without_rebind(tmp_path,replacement):
    async def body(h):
        from outbound_notices import OutboundNoticeQueue
        h.composite.owed.attach(OutboundNoticeQueue(h.store,None))
        await h.route('a');await owed(h,'open',label='result',message_id='a',due='+1m')
        def mature(c):c.execute("UPDATE v2_assistant_owed SET due_at='2000-01-01T00:00:00.000000Z'");c.commit()
        await h.store.submit(mature);await h.composite.owed.tick()
        row=await h.store.submit(lambda c:dict(c.execute("SELECT * FROM v2_outbound_notices WHERE kind='owed_due'").fetchone()))
        def replace_seat(c):
            host,name=ROOT.split(':',1)
            if replacement=='generation':c.execute('UPDATE v2_session_generations SET generation=? WHERE host=? AND session_name=?',('replacement',host,name))
            else:c.execute("UPDATE sessions SET status='closed' WHERE host=? AND session_name=?",(host,name))
            c.commit()
        await h.store.submit(replace_seat)
        assert await h.composite.owed.guard(row) is not None
    run(tmp_path,body)
