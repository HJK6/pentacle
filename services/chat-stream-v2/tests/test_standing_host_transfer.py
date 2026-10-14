"""Standing grants preserve dispatch provenance and exact attempt custody."""
import asyncio
from datetime import datetime, timezone
import time

import pytest
import store_host_transfer as t
from hosts import Hosts
from test_host_transfer_authority import dispatch, prepare_bundle, checkpoint_pointers, source_token
from test_assistant_recovery_command import fixture, OLD, ADVISOR, CHAT


def iso(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat()


async def grant(s,v,c,g,**extra):
    if v.hosts is None: v.hosts=Hosts(local_host='node-a',peers={'node-b':object()})
    old=await s.fetch_session('node-a','old')
    route=await dispatch(s,old,'operator:'+g.ceremony.cid)
    auth={'token_verified':True,'stream_id':OLD,'session_generation':old['session_generation']}
    msg={'mode':t.STANDING_MODE,'action':'authorize','authorization_id':'grant-1','command_id':'standing-1',
         'request_id':'authorize-1','dispatch_id':'dispatch-move','destination':'node-b','members':[OLD,ADVISOR],
         'composite_stream_id':CHAT,**extra}
    context=dict(name='fixture',composite=CHAT,env_binding=c._env_binding(),advisor=ADVISOR,helpers={},
                 trusted_hosts={'node-a','node-b'},registry=v.operator_credential_registry)
    a=await v.host_transfer.mutate(lambda db:t.authorize(db,msg,auth,**context),credential=True)
    return a,msg,auth,context


async def request(s,v,a,auth,**extra):
    msg={'mode':t.STANDING_MODE,'action':'request','authorization_id':a['authorization_id'],'command_id':a['command_id'],
         'expected_revision':a['revision'],'request_id':'request-1','composite_stream_id':CHAT,
         'pointers':checkpoint_pointers(s),**extra}
    row=await v.host_transfer.mutate(lambda db:t.start_authorized(db,msg,auth,v.operator_credential_registry,{'node-a','node-b'}),credential=True)
    return row,msg


async def failed(s,v,row,code='transfer_uncertain_notice'):
    parts=await s.submit(lambda db:t.seats(db,row['command_id']))
    for p in parts:
        if p['target_generation']:
            await s.update_session(*p['target'].split(':',1),status='closed',pane_status='pane_dead')
    def finalize(db):
        r=t.load(db,row['command_id']);t.fail_attempt(db,r,code)
        r.update(cleanup_complete=True,cleanup={p['target']:'closed_or_not_created' for p in parts})
        t.save(db,r);t.finalize_attempt(db,r)
    await v.host_transfer.mutate(finalize)


async def resume(s,v,row,auth,**extra):
    a=await s.submit(lambda db:t.authorization(db,row['authorization_id']))
    msg={'mode':t.STANDING_MODE,'action':'resume','authorization_id':a['authorization_id'],'command_id':a['command_id'],
         'expected_revision':a['revision'],'expected_attempt':row['attempt_number'],
         'request_id':'resume-'+str(row['attempt_number']),'composite_stream_id':CHAT,**extra}
    r=await v.host_transfer.mutate(lambda db:t.resume_authorized(db,msg,auth,v.operator_credential_registry,{'node-a','node-b'}),credential=True)
    return r,msg


def test_default_six_hours_commit_after_600_and_single_use(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            a,msg,auth,ctx=await grant(s,v,c,g)
            assert t.expiry_value(a['expires_at'])==t.expiry_value(a['original_input_at'])+21600 and a['attempts_started']==0
            assert 'operator' not in a and 'scope_row' not in a
            assert await s.submit(lambda db:t.seats(db,a['command_id']))==[]
            row,req=await request(s,v,a,auth)
            await prepare_bundle(s,v,row)
            result=await v.host_transfer.mutate(lambda db:t.commit(db,row['command_id'],v.operator_credential_registry,
                now=t.expiry_value(a['original_input_at'])+601,attempt_id=row['attempt_id']),credential=True)
            assert result['state']=='committed'
            assert (await s.submit(lambda db:t.authorization(db,a['authorization_id'])))['state']=='consumed'
            assert (await s.fetch_session('node-b','child'))['parent_stream_id']!=OLD
            assert (await v.host_transfer.mutate(lambda db:t.commit(db,row['command_id'],v.operator_credential_registry,attempt_id=row['attempt_id']),credential=True))['receipt']==result['receipt']
            s.stop();s.start()
            assert await s.transfer_authority_state(OLD,auth['session_generation'])=='retired'
            assert (await s.submit(lambda db:t.request_identity(db,req,auth)[2]))['attempt_number']==1
    asyncio.run(run())


@pytest.mark.parametrize('hours,evidence,code',[(5,None,None),(7,None,'transfer_expiry_evidence_required'),
    (24,None,'transfer_expiry_evidence_required'),(25,None,'transfer_expiry_invalid'),
    (7,'valid',None),(24,'valid',None),(7,'false','transfer_expiry_evidence_required'),
    (7,'bad_timezone','transfer_expiry_evidence_required')])
def test_expiry_bounds_and_operator_span(tmp_path,hours,evidence,code):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            old=await s.fetch_session('node-a','old')
            route=await dispatch(s,old,'operator:'+g.ceremony.cid)
            original=datetime.fromisoformat(route['created_at'].replace('Z','+00:00')).timestamp()
            expires=iso(original+hours*3600)
            extra={'expires_at':expires}
            if evidence:
                extra['extension_evidence']={'body_start':0,'body_end':4,'quote':'Move' if evidence!='false' else 'fake',
                    'timezone':'Invalid/Zone' if evidence=='bad_timezone' else 'UTC','requested_expires_at':expires}
            if code:
                with pytest.raises(t.TransferError,match=code): await grant(s,v,c,g,**extra)
            else:
                a,*_=await grant(s,v,c,g,**extra)
                assert t.expiry_value(a['expires_at'])==original+hours*3600
    asyncio.run(run())


def test_notice_settlement_cleanup_and_three_attempts(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            a,_,auth,_=await grant(s,v,c,g);row,_=await request(s,v,a,auth)
            old_parts=await prepare_bundle(s,v,row)
            await s.enqueue_outbound_notice(notice_id='fixture',kind='report',dedupe_key='fixture',
                recipient_stream_id=OLD,tell_id='fixture',body='Fixture report',metadata={})
            await s.claim_outbound_notice('fixture',owner='fixture',lease_s=30,force=True)
            with pytest.raises(t.TransferError,match='transfer_uncertain_notice'): await v.host_transfer.commit(row)
            await failed(s,v,row)
            with pytest.raises(t.TransferError,match='transfer_uncertain_notice'): await resume(s,v,row,auth)
            assert await s.complete_outbound_notice('fixture',owner='fixture')
            fresh,res=await resume(s,v,row,auth)
            assert fresh['attempt_number']==2 and fresh['attempt_id']!=row['attempt_id']
            assert fresh['pointers']==row['pointers'] and fresh['expires_at']==row['expires_at']
            replay=await v.host_transfer.mutate(lambda db:t.resume_authorized(db,res,auth,v.operator_credential_registry,{'node-a','node-b'}),credential=True)
            assert replay==fresh
            for p in old_parts:
                assert await s.transfer_authority_state(p['target'],p['target_generation'])=='prepared'
                with pytest.raises(t.TransferError,match='transfer_attempt_conflict'):
                    await v.host_transfer.mutate(lambda db:t.ready(db,row['command_id'],
                        {'token_verified':True,'stream_id':p['target'],'session_generation':p['target_generation']},t.digest(row['pointers'][p['source']]),row['attempt_id']))
            await failed(s,v,fresh);third,_=await resume(s,v,fresh,auth)
            assert third['attempt_number']==3
            await failed(s,v,third)
            with pytest.raises(t.TransferError,match='transfer_attempt_limit'): await resume(s,v,third,auth)
            attempts=await s.submit(lambda db:t.attempts(db,row['command_id']))
            assert len(attempts)==3 and attempts[1]['resume_evidence']['cleanup_digest']
    asyncio.run(run())


@pytest.mark.parametrize('change,code',[('pointers','transfer_checkpoint_required'),('revision','transfer_authorization_revision_conflict'),
    ('destination','transfer_authorization_conflict'),('command','transfer_authorization_conflict'),
    ('credential','transfer_operator_revoked'),('source','transfer_source_changed'),('binding','transfer_authority_changed')])
def test_request_negative_controls(tmp_path,change,code):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            a,_,auth,_=await grant(s,v,c,g);extra={}
            if change=='pointers': extra['pointers']={OLD:[],ADVISOR:[]}
            if change=='revision': extra['expected_revision']=99
            if change=='destination': extra['destination']='node-a'
            if change=='command': extra['command_id']='another'
            if change=='credential': v.operator_credential_registry.revoke(g.ceremony.cid)
            if change=='source': await s.update_session('node-a','advisor',effective_effort='high')
            if change=='binding': await v.host_transfer.mutate(lambda db:db.execute('UPDATE v2_lifecycle_manager SET revision=revision+1'))
            with pytest.raises(t.TransferError,match=code): await request(s,v,a,auth,**extra)
            assert (await s.lifecycle_authority_current())['stream_id']==OLD
            assert await s.submit(lambda db:t.seats(db,a['command_id']))==[]
    asyncio.run(run())


@pytest.mark.parametrize('order',['revoke','commit'])
def test_independent_revoke_serializes_with_commit_and_replays(tmp_path,order):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            a,_,auth,_=await grant(s,v,c,g);row,_=await request(s,v,a,auth);await prepare_bundle(s,v,row)
            a=await s.submit(lambda db:t.authorization(db,a['authorization_id']))
            msg={'action':'authorization-revoke','mode':t.STANDING_MODE,'authorization_id':a['authorization_id'],
                 'expected_revision':a['revision'],'request_id':'revoke-1','reason':'Fixture emergency','composite_stream_id':CHAT}
            admin={'peer_loopback':True,'local_admin_verified':True}
            with pytest.raises(t.TransferError,match='transfer_revoke_unauthorized'):
                await v.host_transfer.mutate(lambda db:t.revoke_authorization(db,msg,auth),credential=True)
            if order=='commit': await v.host_transfer.commit(row)
            result=await v.host_transfer.mutate(lambda db:t.revoke_authorization(db,msg,admin),credential=True)
            if order=='commit':
                assert result['_error']=='transfer_authorization_consumed'
                with pytest.raises(t.TransferError,match='transfer_authorization_consumed'):
                    await v.host_transfer.mutate(lambda db:t.revoke_authorization(db,msg,admin),credential=True)
                assert (await s.lifecycle_authority_current())['stream_id']!=OLD
            else:
                assert result['state']=='revoked'
                assert await v.host_transfer.mutate(lambda db:t.revoke_authorization(db,msg,admin),credential=True)==result
                with pytest.raises(t.TransferError,match='transfer_authorization_revoked'): await v.host_transfer.commit(row)
                assert (await s.lifecycle_authority_current())['stream_id']==OLD
            with pytest.raises(t.TransferError,match='transfer_authorization_conflict'):
                await v.host_transfer.mutate(lambda db:t.revoke_authorization(db,{**msg,'reason':'different'},admin),credential=True)
    asyncio.run(run())


@pytest.mark.parametrize('change,code',[('cleanup','transfer_cleanup_unproved'),('pending','transfer_cleanup_unproved'),
    ('permanent','transfer_attempt_limit'),('cancel','transfer_authorization_revoked'),('old_attempt','transfer_attempt_conflict')])
def test_resume_does_not_trust_caller_cleanup(tmp_path,change,code):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            a,_,auth,_=await grant(s,v,c,g);row,_=await request(s,v,a,auth)
            parts=await prepare_bundle(s,v,row)
            await failed(s,v,row,'permanent_error' if change=='permanent' else 'transfer_uncertain_notice')
            if change=='cleanup':
                await s.update_session(*parts[0]['target'].split(':',1),status='open')
            if change=='pending':
                await v.host_transfer.mutate(lambda db:db.execute('INSERT INTO v2_stream_reservations(host,session_name,expires_at,payload) VALUES(?,?,?,?)',(*parts[0]['target'].split(':',1),time.time()+600,'{}')))
            if change=='cancel':
                await v.host_transfer.mutate(lambda db:t.cancel(db,row['command_id'],auth,{'expected_attempt':1,'request_id':'cancel-1'}))
            with pytest.raises(t.TransferError,match=code): await resume(s,v,row,auth,**({'expected_attempt':0} if change=='old_attempt' else {}))
    asyncio.run(run())


def test_expiry_clock_regression_and_cross_mode_input_uniqueness(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            a,am,auth,ctx=await grant(s,v,c,g)
            with pytest.raises(t.TransferError,match='transfer_input_consumed'):
                await v.host_transfer.mutate(lambda db:t.claim(db,{**am,'command_id':'legacy','pointers':checkpoint_pointers(s)},auth,**ctx),credential=True)
            row,_=await request(s,v,a,auth);await prepare_bundle(s,v,row)
            def at(now): return v.host_transfer.mutate(lambda db:t.commit(db,row['command_id'],v.operator_credential_registry,now=now,attempt_id=row['attempt_id']),credential=True)
            with pytest.raises(t.TransferError,match='transfer_clock_regressed'): await at(t.expiry_value(a['issued_at'])-1)
            with pytest.raises(t.TransferError,match='transfer_authorization_expired'): await at(t.expiry_value(a['expires_at']))
            assert (await s.lifecycle_authority_current())['stream_id']==OLD
    asyncio.run(run())


def test_wire_authorize_status_request_ready_cancel_and_unknown_fields(tmp_path,monkeypatch):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            token=await source_token(s);old=await s.fetch_session('node-a','old')
            await dispatch(s,old,'operator:'+g.ceremony.cid)
            v.hosts=Hosts(local_host='node-a',peers={'node-b':object()})
            # Lifecycle/spawn tests exercise the orchestrator separately; keep
            # this authenticated wire contract test at the admission boundary.
            monkeypatch.setattr(v.host_transfer,'schedule',lambda *args:None)
            async def pointers(row):
                assert all(row['pointers'].values())
            monkeypatch.setattr(v.host_transfer,'verify_pointers',pointers)
            g.actor,g.token=OLD,token
            msg={'type':'assistant.transfer','action':'authorize','mode':t.STANDING_MODE,'authorization_id':'wire-grant',
                 'command_id':'wire-command','request_id':'wire-authorize','composite_stream_id':CHAT,
                 'dispatch_id':'dispatch-move','destination':'node-b','members':[OLD,ADVISOR]}
            reply=await g.rpc(msg);assert reply['type']=='assistant.transfer.ok',reply
            status=await g.rpc({'type':'assistant.transfer','action':'authorization-status','mode':t.STANDING_MODE,
                'authorization_id':'wire-grant','composite_stream_id':CHAT,'request_id':'wire-status'})
            assert status['authorization']['state']=='issued'
            bad=await g.rpc({**msg,'request_id':'wire-bad','unexpected':True})
            assert bad['error_code']=='transfer_action_invalid'
            req={'type':'assistant.transfer','action':'request','mode':t.STANDING_MODE,'authorization_id':'wire-grant',
                 'expected_revision':1,'command_id':'wire-command','request_id':'wire-request','composite_stream_id':CHAT,
                 'pointers':checkpoint_pointers(s)}
            reply=await g.rpc(req);assert reply['type']=='assistant.transfer.ok',reply
            assert reply['command']['attempt_number']==1 and reply['authorization']['revision']==2
            assert (await g.rpc(req))['command']['attempt_id']==reply['command']['attempt_id']
            cancelled=await g.rpc({'type':'assistant.transfer','action':'cancel','command_id':'wire-command',
                'request_id':'wire-cancel','composite_stream_id':CHAT,'expected_attempt':1})
            assert cancelled['command']['state']=='cancelled'
            assert cancelled['authorization']['state']=='revoked'
    asyncio.run(run())


def test_concurrent_request_and_revoke_exactly_one_effect(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            a,_,auth,_=await grant(s,v,c,g)
            row,_=await request(s,v,a,auth);await prepare_bundle(s,v,row)
            def revoke(db):
                return t.revoke_authorization(db,{'authorization_id':a['authorization_id'],'expected_revision':2,
                    'request_id':'raced-revoke','composite_stream_id':CHAT,'reason':'Fixture'},
                    {'peer_loopback':True,'local_admin_verified':True})
            results=await asyncio.gather(v.host_transfer.commit(row),v.host_transfer.mutate(revoke,credential=True),return_exceptions=True)
            saved=await s.submit(lambda db:t.authorization(db,a['authorization_id']))
            assert saved['state'] in {'consumed','revoked'}
            assert ((await s.lifecycle_authority_current())['stream_id']!=OLD)==(saved['state']=='consumed')
            assert len(await s.submit(lambda db:t.attempts(db,row['command_id'])))<=1
    asyncio.run(run())


def test_crash_at_reservation_rolls_back_and_restart_replays_once(tmp_path,monkeypatch):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            a,_,auth,_=await grant(s,v,c,g)
            audit=t.audit
            def crash(db,cid,event,*args,**kw):
                if event=='attempt_started': raise RuntimeError('fixture crash after reservation')
                return audit(db,cid,event,*args,**kw)
            monkeypatch.setattr(t,'audit',crash)
            with pytest.raises(RuntimeError,match='after reservation'): await request(s,v,a,auth)
            assert (await s.submit(lambda db:t.authorization(db,a['authorization_id'])))['attempts_started']==0
            assert await s.submit(lambda db:t.seats(db,a['command_id']))==[]
            monkeypatch.setattr(t,'audit',audit)
            row,msg=await request(s,v,a,auth)
            s.stop();s.start()
            replay=await v.host_transfer.mutate(lambda db:t.start_authorized(db,msg,auth,v.operator_credential_registry,{'node-a','node-b'}),credential=True)
            assert replay['attempt_id']==row['attempt_id']
            assert (await s.submit(lambda db:t.authorization(db,a['authorization_id'])))['attempts_started']==1
            await prepare_bundle(s,v,row)
            assert (await v.host_transfer.commit(row))['state']=='committed'
    asyncio.run(run())


def test_pointer_hash_verification_before_any_attempt(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            a,_,auth,_=await grant(s,v,c,g)
            v.hosts=Hosts(local_host='node-a',peers={'node-b':object()})
            pointers=checkpoint_pointers(s)
            for refs in pointers.values(): refs[0]['sha256']='0'*64
            async def reachable(*args): return None
            # Counterpart reachability only; file hashing still uses real local
            # subprocess execution on the host recorded by each pointer.
            v.hosts.ensure_reachable=reachable
            with pytest.raises(t.TransferError,match='transfer_pointer_unavailable'):
                await v.host_transfer.operation({'type':'assistant.transfer','action':'request','mode':t.STANDING_MODE,
                    'authorization_id':a['authorization_id'],'expected_revision':1,'command_id':a['command_id'],
                    'request_id':'bad-pointer','composite_stream_id':CHAT,'pointers':pointers,'_auth_context':auth})
            assert (await s.submit(lambda db:t.authorization(db,a['authorization_id'])))['attempts_started']==0
            assert await s.submit(lambda db:t.seats(db,a['command_id']))==[]
    asyncio.run(run())


@pytest.mark.parametrize('change',['environment_binding','destination','advisor'])
def test_changed_runtime_scope_refuses_commit(tmp_path,change):
    import dataclasses
    from test_assistant_recovery_command import NEW
    async def run():
        async with fixture(tmp_path,authority=True) as (s,v,c,g,*_):
            a,_,auth,_=await grant(s,v,c,g);row,_=await request(s,v,a,auth);await prepare_bundle(s,v,row)
            if change=='environment_binding':
                fresh=await s.fetch_session('node-b','new')
                c.env_config=dataclasses.replace(c.env_config,direct_primary_stream_id=NEW,direct_primary_generation=fresh['session_generation'])
                await c.load_binding()
            elif change=='destination': v.hosts.peers.clear()
            else: c.env_config=dataclasses.replace(c.env_config,authority_stream_id=NEW)
            with pytest.raises(t.TransferError,match='transfer_authority_changed|transfer_host_untrusted|transfer_helper_changed'):
                await v.host_transfer.commit(row)
            assert (await s.lifecycle_authority_current())['stream_id']==OLD
    asyncio.run(run())
