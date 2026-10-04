"""Synthetic managed upload -> current publisher -> durable event contracts."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from assistant_composite import AssistantComposite
from server import Server
from sessions import Sessions
from test_assistant_direct_primary import _config, ASSISTANT, ROOT
from test_managed_attachment_upload import fixture, upload, AUTH


@asynccontextmanager
async def publication_fixture(tmp_path):
    async with fixture(tmp_path) as (blobs, store):
        root=await store.open_session(*ROOT.split(':'),provider='codex',role='assistant',visibility='default',pane_status='pane_alive',effective_model='gpt-6-sol',effective_effort='high')
        generation=root['session_generation']
        composite=AssistantComposite(store,config=_config(generation))
        await composite.ensure_projection()
        sessions=Sessions(store,tmux=None,local_host='fixture-chat')
        server=Server(store=store,sessions=sessions,comms=SimpleNamespace(blob_store=blobs),local_host='fixture-chat')
        server.assistant_composite=composite
        composite.publication_attachments=server._assistant_publication_attachments
        route=await store.admit_assistant_composite_input(stream_id=ASSISTANT,input_identity='input-1',input_request_id='input-1',body='Synthetic file request',attachments=[],reply_to_message_id=None,reply_to_question_id=None,actor_stream_id='operator:fixture')
        await store.update_assistant_composite_route(route['route_id'],routing_state='resolved',delivery_state='landed',dispatch_id='dispatch-1',route_target=ROOT,route_target_generation=generation,route_payload={'admission_mode':'direct_primary'})
        msg=dict(type='assistant.publish',request_id='publish:dispatch-1',composite_stream_id=ASSISTANT,
            dispatch_id='dispatch-1',reply_to_message_id='input-1',publish_kind='prose',
            response_state='final',message='',_auth_context={'token_verified':True,'stream_id':ROOT,'session_generation':generation})
        yield blobs,store,server,msg


def test_attachment_only_receipt_publish_preserves_two_identities_and_dedups(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs)
            msg['attachment_ids']=[r['upload_id']]
            first=await server._on_assistant_publish(msg)
            retry=await server._on_assistant_publish(msg)
            assert retry['duplicate'] is True and retry['event_id']==first['event_id']
            events=[e for e in await store.fetch_session_event_tail(ASSISTANT,limit=10) if e['kind']=='ASSIST_TEXT']
            assert len(events)==1
            a=events[0]['attachments'][0]
            assert a['key']==r['blob_sha'] and a['mime']=='application/pdf'
            assert a['filename']=='sample.pdf' and a['size']==r['bytes']
            assert a['uploader']['principal_id']==AUTH['stream_id']
            assert a['uploader']['generation']==AUTH['session_generation']
            assert a['publisher']['stream_id']==ROOT
            assert a['publisher']['generation']==msg['_auth_context']['session_generation']
    asyncio.run(run())


def test_unrelated_raw_hash_cannot_substitute_for_upload_id(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs)
            msg.update(message='Synthetic reply',attachment_ids=[r['blob_sha']])
            with pytest.raises(Exception,match='attachment_unverified'):
                await server._on_assistant_publish(msg)
            assert not [e for e in await store.fetch_session_event_tail(ASSISTANT,limit=10) if e['kind']=='ASSIST_TEXT']
    asyncio.run(run())


@pytest.mark.parametrize('changes', [
    {'token_verified':False,'operator_authenticated':True},
    {'stream_id':'fixture-other:unauthorized'},
    {'session_generation':'stale-generation'},
])
def test_upload_receipt_does_not_authorize_publisher(tmp_path,changes):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs)
            msg['attachment_ids']=[r['upload_id']]
            msg['_auth_context'].update(changes)
            with pytest.raises(Exception) as caught:
                await server._on_assistant_publish(msg)
            assert caught.value.code=='publish_not_authorized'
    asyncio.run(run())


@pytest.mark.parametrize('filename,body,mime',[
    ('image.png',b'\x89PNG\r\n\x1a\n'+b'\x00\x00\x00\rIHDR'+b'\x00\x00\x00\x01'*2+b'\x08\x02\x00\x00\x00','image/png'),
    ('sample.pdf',b'%PDF synthetic','application/pdf'),
    ('sample.zip',b'PK\x03\x04 synthetic','application/zip'),
    ('sample.3mf',b'PK\x03\x04 synthetic','model/3mf'),
    ('sample.stl',b'solid synthetic','model/stl'),
    ('sample.step',b'ISO synthetic','model/step'),
    ('sample.stp',b'ISO synthetic','model/step'),
    ('sample.scad',b'cube(1);','application/x-openscad'),
])
def test_canonical_metadata_and_reference_commit(tmp_path,filename,body,mime):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs,body,filename=filename)
            assert r['type']=='upload_blob.ok',r
            msg['attachment_ids']=[r['upload_id']]
            result=await server._on_assistant_publish(msg)
            events=await store.fetch_session_event_tail(ASSISTANT,limit=10)
            a=events[-1]['attachments'][0]
            assert a['mime']==mime and a['filename']==filename and a['size']==len(body)
            assert await blobs.read_verified(a['key'])==body
            refs=await store.submit(lambda conn:[dict(r) for r in conn.execute('SELECT * FROM v2_attachment_refs')])
            assert refs==[dict(owner_kind='publication',owner_id=msg['request_id'],stream_id=ASSISTANT,upload_id=r['upload_id'],blob_sha=r['blob_sha'])]
    asyncio.run(run())


@pytest.mark.parametrize('corruption',['absent','bytes','type','size','pending','filename'])
def test_publish_reverifies_persisted_metadata_and_bytes(tmp_path,corruption):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs)
            path=blobs._root/r['blob_sha'][:2]/r['blob_sha']
            if corruption=='absent': path.unlink()
            elif corruption=='bytes': path.write_bytes(b'not the original')
            else:
                key,value={'type':('media_type','image/png'),'size':('size_bytes',1),
                    'pending':('state','pending'),'filename':('filename','x.png')}[corruption]
                await store.submit(lambda conn:conn.execute(f'UPDATE v2_attachment_uploads SET {key}=? WHERE upload_id=?',(value,r['upload_id'])))
            msg['attachment_ids']=[r['upload_id']]
            with pytest.raises(Exception,match='attachment_unverified'):
                await server._on_assistant_publish(msg)
            count=await store.submit(lambda conn:conn.execute('SELECT count(*) FROM v2_attachment_refs').fetchone()[0])
            assert count==0
    asyncio.run(run())


def test_final_store_boundary_rechecks_after_resolver(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs)
            original=server._assistant_publication_attachments
            async def changed(ids,route):
                result=await original(ids,route)
                (blobs._root/r['blob_sha'][:2]/r['blob_sha']).unlink()
                return result
            server.assistant_composite.publication_attachments=changed
            msg['attachment_ids']=[r['upload_id']]
            with pytest.raises(Exception,match='attachment_unverified'):
                await server._on_assistant_publish(msg)
            assert await store.submit(lambda c:c.execute('SELECT count(*) FROM v2_attachment_refs').fetchone()[0])==0
    asyncio.run(run())


@pytest.mark.parametrize('scope',[ASSISTANT,'other:assistant'])
def test_scoped_upload_stays_in_original_assistant_scope(tmp_path,scope):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs,auth={'scoped_principal':True,'credential_id':'synthetic-scope','scope_stream':scope})
            msg['attachment_ids']=[r['upload_id']]
            if scope!=ASSISTANT:
                with pytest.raises(Exception,match='attachment_unverified'):
                    await server._on_assistant_publish(msg)
            else:
                await server._on_assistant_publish(msg)
                a=(await store.fetch_session_event_tail(ASSISTANT,limit=10))[-1]['attachments'][0]
                assert a['uploader']['generation'] is None
                assert a['uploader']['principal_id']=='credential:synthetic-scope'
                assert a['publisher']['stream_id']==ROOT
    asyncio.run(run())


@pytest.mark.parametrize('extra',[
    {'uploader':'FORGED'}, {'blob_sha':'0'*64}, {'publisher':{'stream_id':ROOT}},
    {'attachment_ids':[{'upload_id':'invented','uploader':'FORGED'}]},
])
def test_caller_provenance_cannot_be_injected(tmp_path,extra):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs);msg['attachment_ids']=[r['upload_id']];msg.update(extra)
            with pytest.raises(Exception,match='invalid'):
                await server._on_assistant_publish(msg)
    asyncio.run(run())


def test_attachment_only_remains_bounded_and_empty_post_refused(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            for ids in ([],['invented']*2,[f'id-{i}' for i in range(17)]):
                msg['attachment_ids']=ids
                with pytest.raises(Exception): await server._on_assistant_publish(msg)
    asyncio.run(run())


def test_failed_correlation_commits_neither_reference_nor_card(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs);msg.update(attachment_ids=[r['upload_id']],reply_to_message_id='wrong-input')
            with pytest.raises(Exception,match='reply_unverified'): await server._on_assistant_publish(msg)
            assert await store.submit(lambda c:c.execute('SELECT count(*) FROM v2_attachment_refs').fetchone()[0])==0
    asyncio.run(run())


def test_concurrent_replay_and_hot_rebind_keep_one_file_card(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs);msg['attachment_ids']=[r['upload_id']]
            first,second=await asyncio.gather(server._on_assistant_publish(msg),server._on_assistant_publish(msg))
            assert first['event_id']==second['event_id']
            assert sorted([first['duplicate'],second['duplicate']])==[False,True]
            await store.open_session('fixture-new','publisher',provider='codex',role='assistant',visibility='default',pane_status='pane_alive',effective_model='gpt-6-sol',effective_effort='high')
            composite=server.assistant_composite
            await composite.load_binding()
            await composite.rebind(dict(type='assistant.rebind',request_id='move-root',expected_revision=0,target_stream_id='fixture-new:publisher',clear=False),actor_stream_id=ROOT)
            retry=await server._on_assistant_publish(msg)
            assert retry['duplicate'] and retry['event_id']==first['event_id']
            events=[e for e in await store.fetch_session_event_tail(ASSISTANT,limit=10) if e['kind']=='ASSIST_TEXT']
            assert len(events)==1 and events[0]['attachments'][0]['publisher']['stream_id']==ROOT
    asyncio.run(run())


def test_reference_insert_failure_rolls_back_event_and_idempotency(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs);msg['attachment_ids']=[r['upload_id']]
            await store.submit(lambda conn:conn.execute("CREATE TRIGGER synthetic_ref_failure BEFORE INSERT ON v2_attachment_refs BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"))
            with pytest.raises(Exception,match='synthetic failure'):
                await server._on_assistant_publish(msg)
            assert await store.submit(lambda c:c.execute('SELECT count(*) FROM v2_assistant_composite_publications').fetchone()[0])==0
            assert not [e for e in await store.fetch_session_event_tail(ASSISTANT,limit=10) if e['kind']=='ASSIST_TEXT']
            await store.submit(lambda c:c.execute('DROP TRIGGER synthetic_ref_failure'))
            assert (await server._on_assistant_publish(msg))['duplicate'] is False
    asyncio.run(run())


def test_publisher_generation_must_be_verified_not_inferred(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs);msg['attachment_ids']=[r['upload_id']]
            del msg['_auth_context']['session_generation']
            with pytest.raises(Exception) as caught:
                await server._on_assistant_publish(msg)
            assert caught.value.code=='publish_not_authorized'
    asyncio.run(run())
