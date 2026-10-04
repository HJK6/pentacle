"""Managed file reads require typed stream references, never text/hash possession."""
import asyncio
import base64
import json
from _shared import operator_auth
from test_scoped_credential import Peer
from test_managed_attachment_publish import publication_fixture, ASSISTANT
from test_managed_attachment_upload import upload


def scoped_peer(server,registry,scope):
    credential,_=registry.issue('pentacle-mobile',label='synthetic-file-reader',scope={'stream':scope})
    peer=Peer()
    server._connection_trust[peer]=operator_auth.ConnectionTrust(transport='v2',credential_id=credential,
        client_kind='pentacle-mobile',operator_trusted=True,scope={'stream':scope})
    return peer,credential


async def fetch(server,peer,key):
    result=await server._dispatch(json.dumps(dict(type='fetch_blob',request_id='file-fetch',blob_sha=key)),websocket=peer)
    return [frame async for frame in result] if hasattr(result,'__aiter__') else result


def test_plaintext_hash_is_not_attachment_read_authority(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            server.handlers.update(blobs.wire_handlers())
            registry=operator_auth.OperatorCredentialRegistry(tmp_path/'synthetic-registry.json')
            server.operator_credential_registry=registry
            reader,_=scoped_peer(server,registry,ASSISTANT)
            uploaded=await upload(blobs)
            await store.append_session_event(ASSISTANT,dict(stream_id=ASSISTANT,kind='USER',text=uploaded['blob_sha'],timestamp='2026-01-01T00:00:00Z'),identity='synthetic-hash-text',limit=500)
            frames=await fetch(server,reader,uploaded['blob_sha'])
            assert frames[0]['error_code']=='blob_forbidden'
    asyncio.run(run())


def test_published_file_is_readable_only_in_its_scoped_stream(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            server.handlers.update(blobs.wire_handlers())
            registry=operator_auth.OperatorCredentialRegistry(tmp_path/'synthetic-registry.json')
            server.operator_credential_registry=registry
            own,_=scoped_peer(server,registry,ASSISTANT)
            other,_=scoped_peer(server,registry,'fixture-other:assistant')
            uploaded=await upload(blobs)
            assert (await fetch(server,own,uploaded['blob_sha']))[0]['error_code']=='blob_forbidden'
            msg['attachment_ids']=[uploaded['upload_id']]
            await server._on_assistant_publish(msg)
            frames=await fetch(server,own,uploaded['blob_sha'])
            assert base64.b64decode(frames[0]['content_b64'])==b'%PDF synthetic'
            assert (await fetch(server,other,uploaded['blob_sha']))[0]['error_code']=='blob_forbidden'
    asyncio.run(run())

import pytest

@pytest.mark.parametrize('envelope', ['text','nested','unrelated','forged','malformed'])
def test_untrusted_transcript_envelopes_never_grant_scope(tmp_path,envelope):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            server.handlers.update(blobs.wire_handlers())
            registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json')
            server.operator_credential_registry=registry
            peer,_=scoped_peer(server,registry,ASSISTANT)
            r=await upload(blobs); sha=r['blob_sha']
            attachment=dict(key=sha,mime='application/pdf',size=r['bytes'],upload_id=r['upload_id'])
            event=dict(stream_id=ASSISTANT,kind='ASSIST_TEXT',provider='composite',text='',timestamp='2026-01-01T00:00:00Z')
            event.update({'text':sha} if envelope=='text' else {'raw':{'attachments':[attachment]}} if envelope=='nested' else {'metadata':sha} if envelope=='unrelated' else {'attachments':[attachment]} if envelope=='forged' else {'attachments':{'key':sha}})
            await store.append_session_event(ASSISTANT,event,identity='forged-event',limit=500)
            assert (await fetch(server,peer,sha))[0]['error_code']=='blob_forbidden'
    asyncio.run(run())

@pytest.mark.parametrize('key',['','a','A'*64,'a'*63,'a'*65,'%'+'a'*63,'../'+'a'*64,'a'*64+'\n'])
def test_malformed_owned_key_fails_before_ownership(tmp_path,key):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            server.handlers.update(blobs.wire_handlers())
            registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json')
            server.operator_credential_registry=registry
            peer,cid=scoped_peer(server,registry,ASSISTANT)
            await store.record_scoped_owner(kind='blob',key=key,credential_id=cid)
            assert (await fetch(server,peer,key))[0]['error_code']=='blob_forbidden'
    asyncio.run(run())


def test_own_actual_scoped_upload_and_revoked_read(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            server.handlers.update(blobs.wire_handlers())
            registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json')
            server.operator_credential_registry=registry
            peer,cid=scoped_peer(server,registry,ASSISTANT)
            init=await server._dispatch(json.dumps(dict(type='upload_blob_init',request_id='own-upload',purpose='chat_attachment',filename='own.pdf')),websocket=peer)
            assert init[0]['type']=='upload_blob.init.ok'
            done=await server._dispatch(json.dumps(dict(type='upload_blob_chunk',request_id='own-upload',data_b64=base64.b64encode(b'%PDF own').decode(),final=True)),websocket=peer)
            assert done[0]['type']=='upload_blob.ok',done
            sha=done[0]['blob_sha']
            assert await store.scoped_owner(kind='blob',key=sha)==cid
            assert base64.b64decode((await fetch(server,peer,sha))[0]['content_b64'])==b'%PDF own'
            registry.revoke(cid)
            assert (await fetch(server,peer,sha))[0]['error_code']=='authentication_required'
    asyncio.run(run())


def test_scoped_send_cannot_launder_unowned_attachment(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            server.handlers.update(blobs.wire_handlers())
            registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json')
            server.operator_credential_registry=registry
            peer,_=scoped_peer(server,registry,ASSISTANT)
            r=await upload(blobs,b'\x89PNG synthetic',filename='synthetic.png')
            forged=await server._dispatch(json.dumps(dict(type='send',request_id='forged-send',optimistic_id='forged',to_stream_id=ASSISTANT,text='synthetic',attachments=[dict(key=r['blob_sha'],mime='image/png',bytes=r['bytes'])])),websocket=peer)
            assert forged[0]['error_code']=='blob_forbidden',forged
            assert (await fetch(server,peer,r['blob_sha']))[0]['error_code']=='blob_forbidden'
            assert not await store.get_assistant_composite_route(stream_id=ASSISTANT,input_identity='forged')
    asyncio.run(run())


def test_external_principal_cannot_fetch_published_file(tmp_path):
    async def run():
        from test_dot_principal_scope import _open_token_store, _dot_frame, DOT_ID
        from server import Server, DOT_SCOPE_DENIED_CODE
        auth_store,_=_open_token_store()
        server=Server(store=auth_store,dot_principal_stream_ids=[DOT_ID])
        peer=Peer(); server._tls_connections.add(peer)
        reached=[]
        async def forbidden_handler(msg):
            reached.append(msg); return {'type':'fetch_blob.ok'}
        server.handlers['fetch_blob']=forbidden_handler
        frames=await server._dispatch(_dot_frame('fetch_blob',blob_sha='a'*64),websocket=peer)
        assert frames[0]['error_code']==DOT_SCOPE_DENIED_CODE
        assert not reached
    asyncio.run(run())


def test_published_missing_bytes_is_honest_unavailable(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            server.handlers.update(blobs.wire_handlers())
            registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json')
            server.operator_credential_registry=registry
            peer,_=scoped_peer(server,registry,ASSISTANT)
            r=await upload(blobs); msg['attachment_ids']=[r['upload_id']]
            await server._on_assistant_publish(msg)
            (tmp_path/'blobs'/r['blob_sha'][:2]/r['blob_sha']).unlink()
            assert (await fetch(server,peer,r['blob_sha']))[0]['error_code']=='blob_unknown'
    asyncio.run(run())

@pytest.mark.parametrize('failure',['correlation','rollback','interrupted'])
def test_failed_or_interrupted_publication_never_grants_read(tmp_path,failure):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            server.handlers.update(blobs.wire_handlers())
            registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json');server.operator_credential_registry=registry
            peer,_=scoped_peer(server,registry,ASSISTANT)
            r=await upload(blobs);msg['attachment_ids']=[r['upload_id']]
            if failure=='correlation':msg['reply_to_message_id']='wrong-input'
            elif failure=='rollback':
                await store.submit(lambda c:c.execute("CREATE TRIGGER synthetic_scope_failure BEFORE INSERT ON v2_attachment_refs BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"))
            else:
                async def interrupted(*args,**kwargs):raise asyncio.CancelledError()
                server.assistant_composite.publication_attachments=interrupted
            with pytest.raises((Exception,asyncio.CancelledError)):
                await server._on_assistant_publish(msg)
            assert (await fetch(server,peer,r['blob_sha']))[0]['error_code']=='blob_forbidden'
            assert await store.submit(lambda c:c.execute('SELECT count(*) FROM v2_attachment_refs').fetchone()[0])==0
    asyncio.run(run())

@pytest.mark.parametrize('media',['photo','voice'])
def test_scoped_own_photo_and_voice_journeys_remain_supported(tmp_path,media):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            from transcribe import Transcriber
            from test_transcribe_blob import _StubPoster,_ok_body
            server.handlers.update(blobs.wire_handlers())
            registry=operator_auth.OperatorCredentialRegistry(tmp_path/'registry.json');server.operator_credential_registry=registry
            peer,cid=scoped_peer(server,registry,ASSISTANT)
            other,_=scoped_peer(server,registry,ASSISTANT)
            body=b'\x89PNG synthetic' if media=='photo' else b'synthetic audio bytes'
            extra=dict(purpose='chat_attachment',filename='photo.png') if media=='photo' else {}
            init=await server._dispatch(json.dumps(dict(type='upload_blob_init',request_id='media-upload',**extra)),websocket=peer)
            assert init[0]['type']=='upload_blob.init.ok'
            done=await server._dispatch(json.dumps(dict(type='upload_blob_chunk',request_id='media-upload',data_b64=base64.b64encode(body).decode(),final=True)),websocket=peer)
            sha=done[0]['blob_sha']
            assert (await fetch(server,other,sha))[0]['error_code']=='blob_forbidden'
            assert base64.b64decode((await fetch(server,peer,sha))[0]['content_b64'])==body
            if media=='photo':
                accepted=await server._dispatch(json.dumps(dict(type='send',request_id='photo-input',optimistic_id='photo-input',to_stream_id=ASSISTANT,text='',attachments=[dict(key=sha,mime='image/png',bytes=len(body))])),websocket=peer)
                assert accepted[0]['type']=='send.result',accepted
                # Another credential's USER attachment is not a publication grant.
                assert (await fetch(server,other,sha))[0]['error_code']=='blob_forbidden'
            else:
                poster=_StubPoster(200,_ok_body(text='synthetic transcript'))
                server._transcriber=Transcriber(blobs,mic_api='http://127.0.0.1:7780',http_post=poster)
                transcribed=await server._dispatch(json.dumps(dict(type='transcribe_blob',request_id='voice-transcribe',blob_sha=sha,mime='audio/mp4')),websocket=peer)
                assert transcribed[0]['type']=='transcribe_blob.ok',transcribed
                assert transcribed[0]['text']=='synthetic transcript'
                assert len(poster.calls)==1
    asyncio.run(run())
