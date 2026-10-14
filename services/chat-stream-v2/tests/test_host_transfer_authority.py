"""One-use operator-dispatch transfer and effect-time authority controls."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from hosts import Hosts

import store_host_transfer as transfer

import pytest

from store import STREAM_TOKEN_HASH_VERSION
from sessions import VerbError
from store_lifecycle_authority import AuthorityError
from test_assistant_recovery_command import fixture, OLD, NEW, CHILD, ADVISOR, CHAT


def checkpoint_pointers(store,members=(OLD,ADVISOR)):
    path=Path(store.path).parent/'checkpoint.md'
    path.write_text('Commission: fixture transfer; spec: fixture; stage: preparation; obligations: none; owner: fixture.\n')
    ref={'host':'node-a','path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    return {sid:[ref] for sid in members}


async def source_token(store):
    token = 'fixture-transfer-source-token'
    await store.grant_stream_token('node-a', 'old', hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
    return token


async def dispatch(store, source, principal):
    route = await store.admit_assistant_composite_input(
        stream_id=CHAT, input_identity='operator-move', input_request_id='operator-move',
        body='Move the whole desk to the other trusted host.', attachments=[],
        reply_to_message_id=None, reply_to_question_id=None, actor_stream_id=principal,
        direct_primary=True, direct_target_stream_id=OLD,
        direct_target_generation=source['session_generation'])
    await store.update_assistant_composite_route(route['route_id'], routing_state='resolved',
        delivery_state='landed', dispatch_id='dispatch-move', route_target=OLD,
        route_target_generation=source['session_generation'], route_payload={'admission_mode':'direct_primary'})
    return route


def test_current_fd_can_request_one_daemon_transfer(tmp_path):
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, manifest, recovery):
            token = await source_token(store)
            old = await store.fetch_session('node-a', 'old')
            await dispatch(store, old, 'operator:'+gateway.ceremony.cid)
            server.hosts = Hosts(local_host='node-a',peers={'node-b':object()})
            gateway.actor, gateway.token = OLD, token
            reply = await gateway.rpc({'type':'assistant.transfer','action':'request',
                'request_id':'migration-1','command_id':'migration-1','composite_stream_id':CHAT,
                'dispatch_id':'dispatch-move','destination':'node-b',
                'members':[OLD, ADVISOR], 'pointers':checkpoint_pointers(store)})
            assert reply['type'] == 'assistant.transfer.ok', reply
            assert reply['command']['state'] == 'preparing'
    asyncio.run(run())


def test_old_parent_cannot_close_child_after_custody_changes(tmp_path):
    async def run():
        async with fixture(tmp_path) as (store, server, composite, gateway, manifest, recovery):
            # Use a local child so the test exercises the actual close/kill path.
            row = await store.open_session('node-a', 'owned', parent_stream_id=OLD,
                pane_status='pane_alive', bootstrap_state='ready', role='worker')
            server.sessions.tmux.live.add('owned')
            await server.sessions.refresh()
            old = await store.fetch_session('node-a', 'old')
            admitted = asyncio.Event()
            original_close = server.sessions.close
            async def observe_admission(*args, **kwargs):
                admitted.set()
                return await original_close(*args, **kwargs)
            server.sessions.close = observe_admission
            lock = server.sessions._lifecycle_lock('node-a', 'owned')
            await lock.acquire()
            task = asyncio.create_task(server._on_close({'host':'node-a','session_name':'owned',
                'request_id':'old-parent-close','reason':'fixture close', 'operator_confirm':True,
                'expected_generation':row['session_generation'],
                '_auth_context':{'token_verified':True,'stream_id':OLD,
                                 'session_generation':old['session_generation']}}))
            try:
                await asyncio.wait_for(admitted.wait(), 2)
                await server.sessions.reparent('node-a','owned',NEW,
                    auth_context={'token_verified':True,'stream_id':OLD,
                                  'session_generation':old['session_generation']})
            finally:
                lock.release()
            try:
                await task
            except VerbError as exc:
                assert exc.code in {'close_unauthorized','authority_retired','actor_generation_changed'}
            assert 'owned' not in server.sessions.tmux.killed, 'stale parent closed a child after custody moved'
            assert (await store.fetch_session('node-a','owned'))['status'] == 'open'
    asyncio.run(run())


async def claim_bundle(store,server,gateway,composite,**overrides):
    if server.hosts is None: server.hosts=Hosts(local_host='node-a',peers={'node-b':object()})
    old=await store.fetch_session('node-a','old')
    await dispatch(store,old,'operator:'+gateway.ceremony.cid)
    auth={'token_verified':True,'stream_id':OLD,'session_generation':old['session_generation']}
    msg={'command_id':'bundle-1','dispatch_id':'dispatch-move','destination':'node-b',
         'members':[OLD,ADVISOR],'pointers':checkpoint_pointers(store),**overrides}
    def claim(c):
        return transfer.claim(c,msg,auth,name='fixture',composite=CHAT,
            env_binding=composite._env_binding(),advisor=ADVISOR,helpers={},
            trusted_hosts={'node-a','node-b'},registry=server.operator_credential_registry)
    row=await store.submit(lambda c:transfer.transaction(c,claim,server.operator_credential_registry))
    return row,msg,auth,claim


async def prepare_bundle(store,server,row):
    parts=await store.submit(lambda c:transfer.seats(c,row['command_id']))
    for p in parts:
        host,name=p['target'].split(':',1)
        source=row['sources'][p['source']]
        target=await store.open_session(host,name,role='worker',visibility='hidden',
            provider=source['provider'],effective_model=source['effective_model'],
            effective_effort=source['effective_effort'],pane_status='pane_alive',bootstrap_state='ready')
        server.sessions.tmux.live.add(name)
        await store.submit(lambda c:transfer.transaction(c,lambda c:transfer.prepared(c,row['command_id'],p['target'],target['session_generation'],row.get('attempt_id'))))
        await store.submit(lambda c:transfer.transaction(c,lambda c:transfer.ready(c,row['command_id'],
            {'token_verified':True,'stream_id':p['target'],'session_generation':target['session_generation']},transfer.digest(row['pointers'].get(p['source'],[])),row.get('attempt_id'))))
    return await store.submit(lambda c:transfer.seats(c,row['command_id']))


def test_atomic_custody_including_live_retired_source_and_restart(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,msg,auth,claim=await claim_bundle(store,server,gateway,composite)
            parts=await prepare_bundle(store,server,row)
            for p in parts:
                assert await store.transfer_authority_state(p['target'],p['target_generation'])=='prepared'
            result=await server.host_transfer.commit(row)
            assert result['state']=='committed'
            fd=next(p for p in parts if p['source']==OLD)
            advisor=next(p for p in parts if p['source']==ADVISOR)
            assert (await store.lifecycle_authority_current())['stream_id']==fd['target']
            assert (await composite.binding())['stream_id']==fd['target']
            assert await store.get('assistant.authority.stream_id')==advisor['target']
            assert (await store.fetch_session('node-b','child'))['parent_stream_id']==fd['target']
            assert (await store.fetch_session(*advisor['target'].split(':',1)))['parent_stream_id']==fd['target']
            assert (await store.fetch_session('node-a','old'))['status']=='open'
            for p in parts:
                assert await store.transfer_authority_state(p['source'],p['source_generation'])=='retired'
                assert await store.transfer_authority_state(p['target'],p['target_generation']) is None
            # Replay remains a read of the same receipt after source retirement.
            assert (await store.submit(lambda c:transfer.transaction(c,claim,server.operator_credential_registry)))['receipt']==result['receipt']
            store.stop();store.start()
            assert await store.transfer_authority_state(OLD,auth['session_generation'])=='retired'
            assert (await store.lifecycle_authority_current())['stream_id']==fd['target']
    asyncio.run(run())


@pytest.mark.parametrize('change,code',[
    ('expired','transfer_dispatch_expired'),('revoked','transfer_operator_revoked'),
    ('binding','transfer_authority_changed'),('generation','transfer_source_changed'),
    ('target_closed','transfer_seat_unavailable'),('pending_spawn','transfer_pending_spawn'),
])
def test_failed_prepare_or_revalidation_retains_source(tmp_path,change,code):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,auth,_=await claim_bundle(store,server,gateway,composite)
            parts=await prepare_bundle(store,server,row)
            if change=='revoked': server.operator_credential_registry.revoke(gateway.ceremony.cid)
            def alter(c):
                if change=='expired':
                    current=transfer.load(c,row['command_id']);current['expires_at']=0;transfer.save(c,current)
                elif change=='binding': c.execute('UPDATE v2_lifecycle_manager SET revision=revision+1')
                elif change=='generation': c.execute("UPDATE v2_session_generations SET generation='reopened' WHERE host='node-a' AND session_name='old'")
                elif change=='target_closed': c.execute("UPDATE sessions SET status='closed' WHERE host=? AND session_name=?",parts[0]['target'].split(':',1))
                elif change=='pending_spawn': c.execute('INSERT INTO v2_stream_reservations(host,session_name,expires_at,payload) VALUES(?,?,?,?)',
                    ('node-b','pending',9999999999,json.dumps({'open_fields':{'parent_stream_id':OLD}})))
            await store.submit(lambda c:transfer.transaction(c,alter))
            with pytest.raises(transfer.TransferError,match=code):
                await server.host_transfer.commit(row)
            assert (await store.lifecycle_authority_current())['stream_id']==OLD
            assert (await composite.binding())['stream_id']==OLD
            assert (await store.fetch_session('node-b','child'))['parent_stream_id']==OLD
            assert await store.transfer_authority_state(OLD,auth['session_generation']) is None
    asyncio.run(run())


def test_unfinished_direct_dispatch_finishes_once_after_succession(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,auth,_=await claim_bundle(store,server,gateway,composite)
            parts=await prepare_bundle(store,server,row)
            fd=next(p for p in parts if p['source']==OLD)
            await server.host_transfer.commit(row)
            token='fresh-successor-fixture-token'
            await store.grant_stream_token(*fd['target'].split(':',1),hashlib.sha256(token.encode()).hexdigest(),STREAM_TOKEN_HASH_VERSION)
            gateway.actor,gateway.token=fd['target'],token
            reply=await gateway.rpc({'type':'assistant.publish','request_id':'publish:dispatch-move',
                'composite_stream_id':CHAT,'dispatch_id':'dispatch-move','reply_to_message_id':'operator-move',
                'publish_kind':'prose','response_state':'final','message':'The isolated move completed.',
                'attachment_ids':[],'evidence_refs':[]})
            assert reply['type']=='assistant.publish.ok',reply
            route=await store.get_assistant_composite_route(stream_id=CHAT,input_identity='operator-move')
            assert (route['route_target'],route['route_target_generation'])==(OLD,auth['session_generation'])
            assert (await store.fetch_session_event_tail(CHAT,limit=20))[-1]['text']=='The isolated move completed.'
    asyncio.run(run())


def test_question_survives_commit_projection_gap_and_source_death(tmp_path):
    from test_question_contract_d3 import _ask
    async def run():
        async with fixture(tmp_path,authority=True,questions=True) as (store,server,composite,gateway,_,__):
            asked=await server.notify.prompt(_ask('migration-question',producer=OLD,body='Synthetic pending action',
                options=[{'label':'Done','value':'done'},{'label':'Not yet','value':'notyet'}]))
            assert asked['type']=='prompt.ask.ok',asked
            row,_,_,_=await claim_bundle(store,server,gateway,composite)
            parts=await prepare_bundle(store,server,row)
            fd=next(p for p in parts if p['source']==OLD)
            await server.host_transfer.commit(row)
            question=await server.notify._db.call('get_agent_question','migration-question')
            assert question['producer_stream_id']==OLD  # projection deliberately absent
            assert await server.notify._producer_is_current(question)
            await server.notify.project_host_transfer(parts)
            await server.notify.project_host_transfer(parts)
            await store.update_session('node-a','old',status='closed',pane_status='pane_dead')
            await server.sessions.refresh()
            await server.notify.expire_questions_for_closed_producer(OLD,generation=row['actor_generation'])
            question=await server.notify._db.call('get_agent_question','migration-question')
            assert question['state']=='open'
            assert question['producer_stream_id']==fd['target']
            assert question['producer_session_generation']==fd['target_generation']
    asyncio.run(run())


@pytest.mark.parametrize('principal,code',[
    ('agent:node-a:old','transfer_operator_origin_required'),
    ('scoped:11111111-1111-4111-8111-111111111111','transfer_operator_origin_required'),
    ('tell:operator','transfer_operator_origin_required'),
    ('operator:11111111-1111-4111-8111-111111111111','transfer_operator_revoked'),
])
def test_nonoperator_origin_cannot_claim(tmp_path,principal,code):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,_,claim=await claim_bundle(store,server,gateway,composite)
            def reset(c):
                c.execute('DELETE FROM v2_host_transfer_seats');c.execute('DELETE FROM v2_host_transfers')
                c.execute('UPDATE v2_assistant_composite_routes SET actor_stream_id=?',(principal,))
            await store.submit(lambda c:transfer.transaction(c,reset))
            with pytest.raises(transfer.TransferError,match=code):
                await store.submit(lambda c:transfer.transaction(c,claim,server.operator_credential_registry))
            assert (await store.lifecycle_authority_current())['stream_id']==OLD
    asyncio.run(run())


def test_opted_in_emergency_revoke_retires_pin_and_pending_migration(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,auth,_=await claim_bundle(store,server,gateway,composite)
            result=await server._on_assistant_lifecycle({'action':'revoke','emergency':True,
                'retire_fd_generation':True,'composite_stream_id':CHAT,
                'target_stream_id':OLD,'target_generation':auth['session_generation'],
                'expected_revision':row['grant']['revision'],'request_id':'emergency-fd','reason':'Synthetic compromised FD',
                '_auth_context':{'peer_loopback':True,'local_admin_verified':True}})
            assert result['type']=='assistant.lifecycle.ok',result
            assert await store.transfer_authority_state(OLD,auth['session_generation'])=='retired'
            assert (await composite.binding())['stream_id']==OLD
            assert (await server.host_transfer.read(row['command_id']))['state']=='cancelled'
            with pytest.raises(ValueError): await composite._direct_target_generation()
    asyncio.run(run())


@pytest.mark.parametrize('change,code',[
    ('child','transfer_fd_required'),('unverified','transfer_fd_required'),
    ('stale','transfer_fd_required'),('off_fleet','transfer_host_untrusted'),
    ('roster','transfer_roster_incomplete'),('future','transfer_dispatch_expired'),
    ('digest','transfer_dispatch_corrupt'),('tell','transfer_dispatch_required'),
    ('source_target','transfer_dispatch_not_addressed'),
])
def test_dispatch_and_fd_binding_predicates(tmp_path,change,code):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,msg,auth,claim=await claim_bundle(store,server,gateway,composite)
            def reset(c):
                c.execute('DELETE FROM v2_host_transfer_seats');c.execute('DELETE FROM v2_host_transfers')
                if change=='future': c.execute("UPDATE v2_assistant_composite_routes SET created_at='2099-01-01T00:00:00Z'")
                if change=='digest': c.execute("UPDATE v2_assistant_composite_routes SET body='changed'")
                if change=='tell': c.execute('DELETE FROM v2_assistant_composite_routes')
                if change=='source_target': c.execute("UPDATE v2_assistant_composite_routes SET route_target_generation='stale'")
            await store.submit(lambda c:transfer.transaction(c,reset))
            if change=='child':
                child=await store.fetch_session('node-b','child');auth.update(stream_id=CHILD,session_generation=child['session_generation'])
            if change=='unverified': auth['token_verified']=False
            if change=='stale': auth['session_generation']='stale'
            if change=='off_fleet': msg['destination']='external-host'
            if change=='roster':
                msg['members']=[OLD];msg['pointers']={OLD:msg['pointers'][OLD]}
            with pytest.raises(transfer.TransferError,match=code):
                await store.submit(lambda c:transfer.transaction(c,claim,server.operator_credential_registry))
            assert (await store.lifecycle_authority_current())['stream_id']==OLD
    asyncio.run(run())


def test_one_use_command_and_changed_retry_fail_closed(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,msg,auth,claim=await claim_bundle(store,server,gateway,composite)
            assert (await store.submit(lambda c:transfer.transaction(c,claim,server.operator_credential_registry)))==row
            msg['command_id']='new-request-id'
            with pytest.raises(transfer.TransferError,match='transfer_input_consumed'):
                await store.submit(lambda c:transfer.transaction(c,claim,server.operator_credential_registry))
            msg.update(command_id='bundle-1',destination='node-a')
            with pytest.raises(transfer.TransferError,match='transfer_command_conflict'):
                await store.submit(lambda c:transfer.transaction(c,claim,server.operator_credential_registry))
            await server.host_transfer.mutate(lambda c:transfer.cancel(c,'bundle-1',auth))
            msg.update(command_id='replacement-command',destination='node-b')
            with pytest.raises(transfer.TransferError,match='transfer_input_consumed'):
                await store.submit(lambda c:transfer.transaction(c,claim,server.operator_credential_registry))
    asyncio.run(run())


def test_successor_has_immediate_close_and_adoption_old_socket_denied(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            oldtoken=await source_token(store)
            row,_,auth,_=await claim_bundle(store,server,gateway,composite)
            parts=await prepare_bundle(store,server,row)
            fd=next(p for p in parts if p['source']==OLD)
            child=await store.open_session('node-a','owned',parent_stream_id=OLD,role='worker',pane_status='pane_alive',bootstrap_state='ready')
            server.sessions.tmux.live.add('owned')
            await server.host_transfer.commit(row)
            gateway.actor,gateway.token=OLD,oldtoken
            msg={'type':'close','request_id':'retired-close','host':'node-a','session_name':'owned',
                 'expected_generation':child['session_generation'],'operator_confirm':True,'reason':'Synthetic close'}
            denied=await gateway.rpc(msg)
            assert denied['error_code']=='authority_retired',denied
            assert 'owned' not in server.sessions.tmux.killed
            token='successor-lifecycle-token'
            await store.grant_stream_token(*fd['target'].split(':',1),hashlib.sha256(token.encode()).hexdigest(),STREAM_TOKEN_HASH_VERSION)
            gateway.actor,gateway.token=fd['target'],token
            adopted=await gateway.rpc({'type':'reparent','request_id':'successor-adopt','stream_id':CHILD,
                'new_parent_stream_id':NEW,'expected_generation':(await store.fetch_session('node-b','child'))['session_generation'],
                'reason':'Synthetic immediate custody operation'})
            assert adopted['type']=='reparent.ok',adopted
            closed=await gateway.rpc({**msg,'request_id':'successor-close'})
            assert closed['type']=='close.ok',closed
            assert 'owned' in server.sessions.tmux.killed
    asyncio.run(run())


def emergency_message(row, auth):
    return {'action':'revoke','emergency':True,'retire_fd_generation':True,
        'composite_stream_id':CHAT,'target_stream_id':OLD,
        'target_generation':auth['session_generation'],
        'expected_revision':row['grant']['revision'],'request_id':'emergency-fd',
        'reason':'Synthetic compromised FD',
        '_auth_context':{'peer_loopback':True,'local_admin_verified':True}}


@pytest.mark.parametrize('change',['generation','target','revision','seat'])
def test_emergency_revoke_bad_binding_preserves_every_authority(tmp_path,change):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,auth,_=await claim_bundle(store,server,gateway,composite)
            msg=emergency_message(row,auth)
            if change=='generation': msg['target_generation']='stale'
            if change=='target': msg['target_stream_id']=ADVISOR
            if change=='revision': msg['expected_revision']+=1
            if change=='seat': msg['_auth_context'].update(auth)
            with pytest.raises((transfer.TransferError,AuthorityError,VerbError)) as caught:
                await server._on_assistant_lifecycle(msg)
            assert caught.value.code in {'transfer_revoke_target_conflict','authority_revision_conflict','emergency_local_only'}
            assert (await store.lifecycle_authority_current())==row['grant']
            assert await store.transfer_authority_state(OLD,auth['session_generation']) is None
            assert (await server.host_transfer.read(row['command_id']))['state']=='preparing'
    asyncio.run(run())


def test_emergency_revoke_receipt_replay_and_atomic_fault(tmp_path,monkeypatch):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,auth,_=await claim_bundle(store,server,gateway,composite)
            msg=emergency_message(row,auth)
            original=transfer.audit
            def fault(c,cid,event,*args,**kwargs):
                if event=='fd_generation_revoked': raise RuntimeError('synthetic_fault_after_mutation')
                return original(c,cid,event,*args,**kwargs)
            monkeypatch.setattr(transfer,'audit',fault)
            with pytest.raises(RuntimeError,match='synthetic_fault'):
                await server._on_assistant_lifecycle(msg)
            assert await store.lifecycle_authority_current()==row['grant']
            assert await store.transfer_authority_state(OLD,auth['session_generation']) is None
            assert (await server.host_transfer.read(row['command_id']))['state']=='preparing'
            monkeypatch.setattr(transfer,'audit',original)
            first=await server._on_assistant_lifecycle(msg)
            replay=await server._on_assistant_lifecycle(msg)
            assert first['type']==replay['type']=='assistant.lifecycle.ok',(first,replay)
            with pytest.raises(VerbError) as conflict:
                await server._on_assistant_lifecycle({**msg,'retire_fd_generation':False})
            assert conflict.value.code=='authority_request_conflict'
            store.stop();store.start();await composite.load_binding()
            assert await store.transfer_authority_state(OLD,auth['session_generation'])=='retired'
            with pytest.raises(ValueError): await composite._direct_target_generation()
    asyncio.run(run())


@pytest.mark.parametrize('winner',['commit','revoke'])
def test_concurrent_transfer_and_emergency_revoke_has_one_winner(tmp_path,winner):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,auth,_=await claim_bundle(store,server,gateway,composite)
            parts=await prepare_bundle(store,server,row)
            lock=server.host_transfer.actor_lock((OLD,auth['session_generation']))
            await lock.acquire()
            async def commit():
                try: return await server.host_transfer.commit(row)
                except transfer.TransferError as e:return e.code
            async def revoke():
                try: return await server._on_assistant_lifecycle(emergency_message(row,auth))
                except transfer.TransferError as e:return e.code
            actions={'commit':commit,'revoke':revoke}
            first=asyncio.create_task(actions[winner]());await asyncio.sleep(0)
            second=asyncio.create_task(actions['revoke' if winner=='commit' else 'commit']());await asyncio.sleep(0)
            lock.release()
            outcomes=await asyncio.wait_for(asyncio.gather(first,second),5)
            command=await server.host_transfer.read(row['command_id'])
            grant=await store.lifecycle_authority_current()
            if winner=='commit':
                assert command['state']=='committed',outcomes
                assert grant['stream_id']==next(p['target'] for p in parts if p['source']==OLD)
            else:
                assert command['state']=='cancelled',outcomes
                assert grant['stream_id'] is None
            assert await store.transfer_authority_state(OLD,auth['session_generation'])=='retired'
    asyncio.run(run())


def test_successor_preserves_full_spec_and_watch_metadata(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            def seed(c):
                c.execute("UPDATE sessions SET spec_ids=?,spec_id=?,spec_resolution=?,objective=?,objective_source=?,no_watch=? WHERE host='node-a' AND session_name='old'",
                    (json.dumps(['spec_one','spec_two']),'spec_one','resolved','Synthetic retained objective','explicit',0))
            await store.submit(lambda c:transfer.transaction(c,seed))
            row,_,_,_=await claim_bundle(store,server,gateway,composite)
            parts=await prepare_bundle(store,server,row)
            await server.host_transfer.commit(row)
            fd=next(p for p in parts if p['source']==OLD)
            source=await store.fetch_session('node-a','old')
            target=await store.fetch_session(*fd['target'].split(':',1))
            for field in ('spec_ids','spec_id','spec_resolution','objective','objective_source','no_watch'):
                assert target[field]==source[field],field
    asyncio.run(run())


def test_emergency_retirement_drains_detached_spawn_effect(tmp_path):
    from spawnctl import SpawnCtl
    from test_lifecycle_continuity import Panes
    async def run():
        panes = Panes()
        async with fixture(tmp_path, authority=True, tmux=panes) as (store, server, composite, gateway, _, __):
            row, _, auth, _ = await claim_bundle(store, server, gateway, composite)
            ctl = SpawnCtl(store, server.sessions, tmux=panes)
            server.spawnctl = ctl
            reached, release = asyncio.Event(), asyncio.Event()
            original = panes.new_session
            async def paused(*args, **kwargs):
                reached.set()
                await release.wait()
                await original(*args, **kwargs)
            panes.new_session = paused
            request = asyncio.create_task(ctl.spawn({'command':'run', 'session_name':'detached',
                'request_id':'detached-spawn', 'objective':'Synthetic spawn race', '_auth_context':auth}, 'node-a'))
            revoke = None
            try:
                await asyncio.wait_for(reached.wait(), 3)
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                revoke = asyncio.create_task(server._on_assistant_lifecycle(emergency_message(row, auth)))
                await asyncio.sleep(.05)
                assert not revoke.done(), 'retirement crossed an unsettled detached physical spawn'
                release.set()
                await asyncio.wait_for(asyncio.gather(*ctl._background_spawns), 5)
                assert (await asyncio.wait_for(revoke, 3))['type']=='assistant.lifecycle.ok'
                assert await store.transfer_authority_state(OLD, auth['session_generation'])=='retired'
                assert not await store.reservations(include_expired=True)
            finally:
                release.set()
                await asyncio.gather(request, *ctl._background_spawns, *([revoke] if revoke else []), return_exceptions=True)
    asyncio.run(run())


def test_retired_parent_does_not_rollback_unrelated_due_wake(tmp_path):
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            row, _, auth, _ = await claim_bundle(store, server, gateway, composite)
            unrelated = await store.open_session('node-a','unrelated',no_watch=True)
            wake = await store.register_watch_wake('wake','node-a:unrelated',unrelated['session_generation'],
                {'request_id':'unrelated-due','due_at':110,'note':'fixture','urgent':False},now=100)
            await store.evaluate_watch_wake({},now=100)
            await server._on_assistant_lifecycle(emergency_message(row,auth))
            assert await store.evaluate_watch_wake({},now=110)==1
            saved = await store.list_watch_wake('wake','node-a:unrelated',unrelated['session_generation'])
            assert next(w for w in saved if w['id']==wake['id'])['state']=='consumed'
            assert (await store.fetch_session('node-a','old'))['status']=='open'
    asyncio.run(run())


@pytest.mark.parametrize('claimed',[False,True])
def test_pending_notices_keep_generation_digest_and_uncertain_effect_fence(tmp_path,claimed):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,_,_=await claim_bundle(store,server,gateway,composite)
            parts=await prepare_bundle(store,server,row)
            source=row['sources'][ADVISOR]
            args={'notice_id':'pending-advisor','kind':'assistant_lane_ruling_request','dedupe_key':'pending-advisor',
                  'recipient_stream_id':ADVISOR,'tell_id':'pending-advisor','body':'Synthetic ruling',
                  'metadata':{'authority_generation':source['session_generation'],'ruling_request_id':'pending-ruling'}}
            await store.enqueue_outbound_notice(**args)
            if claimed:
                await store.claim_outbound_notice('pending-advisor',owner='fixture-delivery',lease_s=30,force=True)
                with pytest.raises(transfer.TransferError,match='transfer_uncertain_notice'):
                    await server.host_transfer.commit(row)
                assert (await store.lifecycle_authority_current())['stream_id']==OLD
            else:
                await server.host_transfer.commit(row)
                target=next(p for p in parts if p['source']==ADVISOR)
                replay=await store.enqueue_outbound_notice(**{**args,'recipient_stream_id':target['target'],
                    'metadata':{**args['metadata'],'authority_generation':target['target_generation']}})
                assert replay['created'] is False
    asyncio.run(run())


def test_commit_never_waits_for_second_actor_while_holding_first(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,_,_=await claim_bundle(store,server,gateway,composite)
            await prepare_bundle(store,server,row)
            first=row['actor']
            second=next(sid for sid in row['sources'] if sid!=first)
            first_lock=server.host_transfer.actor_lock((first,row['sources'][first]['session_generation']))
            second_lock=server.host_transfer.actor_lock((second,row['sources'][second]['session_generation']))
            async with second_lock:
                with pytest.raises(transfer.TransferError,match='transfer_effect_busy'):
                    await asyncio.wait_for(server.host_transfer.commit(row),.2)
            assert not first_lock.locked()
            assert (await server.host_transfer.commit(row))['state']=='committed'
    asyncio.run(run())


@pytest.mark.parametrize('already_delivered',[False,True])
def test_pending_ruling_delivery_resolves_successor_and_preserves_identity(tmp_path,already_delivered):
    from comms import Comms
    from outbound_notices import ASSISTANT_AUTHORITY_REQUEST_TOKEN
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,_,_=await claim_bundle(store,server,gateway,composite)
            rulings=server.lane_rulings
            request=await rulings._create(action='session_close',key='pending-close',intent={'reason':'fixture'},
                requester=OLD,requester_generation=row['sources'][OLD]['session_generation'],
                binding=await rulings.binding(),target=ADVISOR,target_generation=row['sources'][ADVISOR]['session_generation'])
            await rulings._enqueue_notice(request)
            notice_id='assistant-lane-ruling:'+request['ruling_request_id']
            if already_delivered:
                await store.claim_outbound_notice(notice_id,owner='fixture',lease_s=30,force=True)
                assert await store.complete_outbound_notice(notice_id,owner='fixture')
            before=await store.submit(lambda c:dict(c.execute('SELECT * FROM v2_outbound_notices WHERE notice_id=?',(notice_id,)).fetchone()))
            parts=await prepare_bundle(store,server,row)
            await server.host_transfer.commit(row)
            effective=await rulings._fetch(request['ruling_request_id'])
            await rulings._enqueue_notice(effective)
            after=await store.submit(lambda c:dict(c.execute('SELECT * FROM v2_outbound_notices WHERE notice_id=?',(notice_id,)).fetchone()))
            for key in ('notice_id','tell_id','body','source_stream_id','attempts','delivered_at'):
                assert after[key]==before[key]
            if already_delivered:
                assert after==before
            else:
                advisor=next(p for p in parts if p['source']==ADVISOR)
                transport=Comms.__new__(Comms);transport.store=store;transport.sessions=server.sessions
                await transport._validate_assistant_request_target({
                    '_assistant_authority_request_token':ASSISTANT_AUTHORITY_REQUEST_TOKEN,
                    '_assistant_authority_request_generation':json.loads(after['metadata'])['authority_generation'],
                    '_assistant_lane_ruling_request_id':request['ruling_request_id'],
                    'stream_id':after['recipient_stream_id']},{'final_target':advisor['target']})
                original=await store.submit(lambda c:dict(c.execute('SELECT * FROM v2_assistant_lane_rulings WHERE ruling_request_id=?',(request['ruling_request_id'],)).fetchone()))
                assert original['authority_stream_id']==ADVISOR
    asyncio.run(run())


def test_close_hook_before_question_projection_keeps_committed_successor_question(tmp_path):
    from test_question_contract_d3 import _ask
    async def run():
        async with fixture(tmp_path,authority=True,questions=True) as (store,server,composite,gateway,_,__):
            assert (await server.notify.prompt(_ask('close-gap',producer=OLD)))['type']=='prompt.ask.ok'
            row,_,_,_=await claim_bundle(store,server,gateway,composite)
            parts=await prepare_bundle(store,server,row)
            await server.host_transfer.commit(row)
            before=await server.notify._db.call('get_agent_question','close-gap')
            assert before['state']=='open' and before['producer_stream_id']==OLD
            result=await server.sessions.close('node-a','old',reason='Synthetic death before projection',
                expected_generation=row['actor_generation'],operator_confirm=True)
            assert result['closed']
            await server.notify.project_host_transfer(parts)
            after=await server.notify._db.call('get_agent_question','close-gap')
            fd=next(p for p in parts if p['source']==OLD)
            assert after['state']=='open'
            assert (after['producer_stream_id'],after['producer_session_generation'])==(fd['target'],fd['target_generation'])
    asyncio.run(run())


async def governed_close(store,server,row,auth,*,report=False):
    from types import SimpleNamespace
    import time
    async def probe(host):
        return host == 'node-b'
    async def state(name):
        return 'alive' if name in server.sessions.tmux.live else 'gone'
    async def identity(name):
        return {'pane_id':name,'pane_pid':'synthetic-owned-process','pane_started_at':'synthetic-start'}
    server.sessions.tmux.session_state=state
    server.sessions.tmux.pane_identity=identity
    server.sessions.hosts=SimpleNamespace(known=lambda h:h in {'node-a','node-b'},
        is_local=lambda h:h=='node-a',probe_once=probe,tmux_for=lambda h:server.sessions.tmux)
    rulings=server.lane_rulings
    gen=row['sources'][ADVISOR]['session_generation']
    await rulings._record_ownership({'requester_stream_id':OLD,'requester_generation':row['actor_generation'],
        'ruling_request_id':'original-governed-admission'},ADVISOR,gen)
    if report:
        from routing_integrity import RoutingIntegrity
        snapshot=await RoutingIntegrity(store,server.sessions).report_write_snapshot(ADVISOR)
        await store.put_report({'report_id':'governed-report','from_stream_id':ADVISOR,'msg_id':0,'status':'done','ingested_at':time.time(),
            'summary':'Synthetic retained completion','findings':[],'next_action':'Parent disposition'},
            routing_snapshot=snapshot,announce=False)
    msg={'type':'close','host':'node-a','session_name':'advisor','request_id':'governed-close',
        'reason':'Synthetic manager closure','expected_generation':gen,'operator_confirm':True}
    return msg,await rulings.request_close(msg,target_stream_id=ADVISOR,target_generation=gen,auth=auth)


@pytest.mark.parametrize('report',[False,True])
def test_pending_close_executes_exact_successor_and_preserves_original_intent(tmp_path,report):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,auth,_=await claim_bundle(store,server,gateway,composite)
            _,pending=await governed_close(store,server,row,auth,report=report)
            assert pending['type']=='close.pending_ruling'
            rid=pending['ruling_request_id'];rulings=server.lane_rulings
            original=(await rulings._fetch(rid))['intent_json']
            parts=await prepare_bundle(store,server,row)
            await server.host_transfer.commit(row)
            advisor=next(p for p in parts if p['source']==ADVISOR)
            reply=await rulings.ruling({'ruling_request_id':rid,'ruling':'approve','request_id':'approve-after-transfer'},
                actor_stream_id=advisor['target'],actor_generation=advisor['target_generation'])
            saved=await rulings._fetch(rid)
            assert saved['state']=='done',reply
            assert saved['intent_json']==original
            assert (await store.fetch_session(*advisor['target'].split(':',1)))['status']=='closed'
            assert (await store.fetch_session(*ADVISOR.split(':',1)))['status']=='open'
    asyncio.run(run())


def test_new_close_for_successor_retains_advisor_gate_with_exact_generation(tmp_path):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,composite,gateway,_,__):
            row,_,auth,_=await claim_bundle(store,server,gateway,composite)
            msg,_=await governed_close(store,server,row,auth)
            parts=await prepare_bundle(store,server,row)
            await server.host_transfer.commit(row)
            advisor=next(p for p in parts if p['source']==ADVISOR);fd=next(p for p in parts if p['source']==OLD)
            host,name=advisor['target'].split(':',1)
            auth={'token_verified':True,'stream_id':fd['target'],'session_generation':fd['target_generation']}
            pending=await server.lane_rulings.request_close({**msg,'host':host,'session_name':name,
                'request_id':'new-governed-close','expected_generation':advisor['target_generation']},
                target_stream_id=advisor['target'],target_generation=advisor['target_generation'],auth=auth)
            assert pending and pending['type']=='close.pending_ruling'
            # An unrelated generation of the same name is not succession.
            unrelated_generation='unrelated-generation'
            unrelated=await server.lane_rulings.request_close({**msg,'host':host,'session_name':name,
                'request_id':'unrelated-reopened-close','expected_generation':unrelated_generation},
                target_stream_id=advisor['target'],target_generation=unrelated_generation,auth=auth)
            assert unrelated is None
    asyncio.run(run())
