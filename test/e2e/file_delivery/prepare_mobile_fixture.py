#!/usr/bin/env python3
"""Seed a NEW offline synthetic store for the mobile file gate.

Starts no daemon/agent, makes no network call and creates no credentials. Uses
real managed upload/publication handlers with a synthetic verified seat context;
the fleet's isolated daemon/app separately qualifies authenticated native reads.
"""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'services/chat-stream-v2'), str(ROOT/'services')]
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from blobs import BlobStore, Promptless
from server import Server
from sessions import Sessions
from store import Store

BODY = b'%PDF synthetic mobile file gate'
CHAT = 'fixture:file-chat'
SEAT = 'fixture:publisher'


async def prepare(destination):
    root = Path(destination).absolute()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    (root/'synthetic-fixture.json').write_text(json.dumps({'schema': 1, 'synthetic_only': True}))
    store = Store(str(root/'sessions.db'))
    store.start()
    blobs = BlobStore(str(root/'blobs'), attachment_store=store)
    await blobs.start()
    try:
        seat = await store.open_session('fixture','publisher',provider='codex',role='assistant',visibility='default',pane_status='pane_alive',effective_model='gpt-6-sol',effective_effort='high')
        generation = seat['session_generation']
        env = {'PENTACLE_ASSISTANT_COMPOSITE_ENABLED':'1','PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID':CHAT,
               'PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID':SEAT,'PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION':generation}
        composite = AssistantComposite(store,config=AssistantCompositeConfig.from_env(env))
        await composite.ensure_projection()
        server = Server(store=store,sessions=Sessions(store,tmux=None,local_host='fixture'),comms=SimpleNamespace(blob_store=blobs),local_host='fixture')
        server.assistant_composite = composite
        composite.publication_attachments = server._assistant_publication_attachments
        auth = {'token_verified':True,'stream_id':SEAT,'session_generation':generation}
        results = []
        for index, (filename, body) in enumerate([('fixture-present.pdf',BODY),('fixture-expired.pdf',b'%PDF synthetic expired file gate')]):
            rid = f'fixture-upload-{index}'
            owner = object()
            init = await blobs._on_init(dict(request_id=rid,purpose='chat_attachment',filename=filename,_auth_context=auth,_client_websocket=owner),Promptless)
            if init['type'] != 'upload_blob.init.ok':
                raise ValueError('fixture upload init failed')
            receipt = await blobs._on_chunk(dict(request_id=rid,data_b64=base64.b64encode(body).decode(),final=True,_auth_context=auth,_client_websocket=owner),Promptless)
            if receipt['type'] != 'upload_blob.ok':
                raise ValueError('fixture upload failed')
            identity = f'fixture-input-{index}'
            dispatch = f'fixture-dispatch-{index}'
            route = await store.admit_assistant_composite_input(stream_id=CHAT,input_identity=identity,input_request_id=identity,body='Synthetic attachment request',attachments=[],reply_to_message_id=None,reply_to_question_id=None,actor_stream_id='operator:synthetic-fixture')
            await store.update_assistant_composite_route(route['route_id'],routing_state='resolved',delivery_state='landed',dispatch_id=dispatch,route_target=SEAT,route_target_generation=generation,route_payload={'admission_mode':'direct_primary'})
            published = await server._on_assistant_publish(dict(type='assistant.publish',request_id=f'publish:{dispatch}',composite_stream_id=CHAT,dispatch_id=dispatch,reply_to_message_id=identity,publish_kind='prose',response_state='final',message='',attachment_ids=[receipt['upload_id']],_auth_context=auth))
            results.append(dict(filename=filename,sha256=receipt['blob_sha'],bytes=len(body),upload_id=receipt['upload_id'],event_id=published['event_id']))
        expired = results[1]['sha256']
        # Only bytes just produced under the exclusively created fixture root.
        (root/'blobs'/expired[:2]/expired).unlink()
        manifest = {'schema':1,'prepared_at':datetime.now(timezone.utc).isoformat(),'synthetic_only':True,
                    'stream_id':CHAT,'present_sha256':hashlib.sha256(BODY).hexdigest(),'present_bytes':len(BODY),
                    'publications':results,'daemon_environment':env,'runtime':'not_run','authentication_handshake':'not_run'}
        (root/'fixture-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        return manifest
    finally:
        store.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,help='New nonexistent directory below an existing private scratch parent')
    print(json.dumps(asyncio.run(prepare(parser.parse_args().root)),indent=2))

if __name__ == '__main__':
    main()
