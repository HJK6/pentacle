"""Isolated daemon process with a synthetic provider, no model invocation."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sys

SOURCE=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests'),str(SOURCE.parent)]
from assistant_composite import AssistantComposite,AssistantCompositeConfig
from notification_answer_fixture import fixture
from test_front_desk_digest import _production_dispatcher
from blobs import BlobStore
from server import Server
from store import STREAM_TOKEN_HASH_VERSION
from _shared.operator_auth import OperatorCredentialRegistry
from codex_rollout_norm import normalize_codex_rollout_record
from v2_runtime import iso_now


def write_json(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


async def run(root):
    async with fixture(root,host='fixture-host') as (_,queue,comms,provider,sessions,store):
        target='fixture-host:v2-test';chat='fixture-host:assistant'
        generation=sessions.get(target)['session_generation']
        await store.update_session('fixture-host','v2-test',pane_pid='4242')
        token='synthetic-seat-token'
        await store.grant_stream_token('fixture-host','v2-test',hashlib.sha256(token.encode()).hexdigest(),STREAM_TOKEN_HASH_VERSION)
        server=Server(host='127.0.0.1',port=0,store=store,sessions=sessions,comms=comms,local_host='fixture-host',seat_operator_authority=False)
        blobs=BlobStore(str(root/'blobs'),attachment_store=store);await blobs.start()
        server.blobs=blobs;comms.blob_store=blobs;server.handlers.update(blobs.wire_handlers())
        composite=AssistantComposite(store,config=AssistantCompositeConfig(enabled=True,name='assistant',stream_id=chat,direct_primary_stream_id=target,direct_primary_generation=generation,strict_publication_default=True),dispatch=_production_dispatcher(comms),publication_attachments=server._assistant_publication_attachments)
        server.assistant_composite=composite;composite.owed.server=server;composite.owed.attach(queue)
        server.operator_credential_registry=OperatorCredentialRegistry(root/'operator.json')
        credential=server.operator_credential_registry.issue('pentacle')[1]
        await composite.ensure_projection();await sessions.refresh()
        if '--brief' in sys.argv:
            await store.update_session('fixture-host','v2-test',role='assistant',pane_status='pane_alive')
            successor=await sessions.open('fixture-host','desk-next',provider='codex',role='assistant',visibility='visible',pane_status='pane_alive',pane_pid='4244',effective_model='gpt-6-sol',effective_effort='high')
            await sessions.refresh()
        port=await server.bind();counter=0
        async def user(text):
            nonlocal counter
            counter+=1;identity=f'provider-{counter}'
            event=normalize_codex_rollout_record({'type':'response_item','timestamp':iso_now(),'payload':{'type':'message','id':identity,'role':'user','content':[{'type':'input_text','text':text}]}},host='fixture-host',session_name='v2-test',session_id='synthetic-transcript')[0]
            await store.append_session_events_lifecycle_cas([{'stream_id':target,'event':event,'identity':identity,'lifecycle':await store.fetch_open_session_lifecycle(target,pane_pid='4242')}],limit=100)
            write_json(root/'pastes.json',provider.pastes)
        provider.user=user
        write_json(root/'ready.json',dict(port=port,token=token,credential=credential,pid=os.getpid(),generation=generation))
        stop=asyncio.Event();asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set)
        async def sweep():
            while not stop.is_set():
                if '--brief' in sys.argv and (root/'retire-old').exists():
                    (root/'retire-old').unlink()
                    await store.update_session('fixture-host','v2-test',status='closed')
                    await store.evaluate_watch_wake({})
                    await sessions.refresh()
                    write_json(root/'retired.json',True)
                await queue.drain_once();await asyncio.sleep(.1)
        task=asyncio.create_task(sweep())
        try:await stop.wait()
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            await composite.stop();await server.close()

if __name__=='__main__':asyncio.run(run(Path(sys.argv[1])))
