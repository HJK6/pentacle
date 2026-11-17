"""Owned real-pane restore scenario; only native provider bytes are inert fixtures."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys

from usage_caps_socket_fixture import HOST, DESK, NOW, harness, client, AccountRuntime, SpawnCtl, binding
from assistant_authority_restore import AuthorityRestore
from provider_cli_fixtures import seed_pin
from test_provider_cli_pin import artifact_executor
from store import STREAM_TOKEN_HASH_VERSION
from tmux_transport import Tmux
from ingest import Ingest


async def run(scratch, tmux_binary):
    async with harness(scratch) as h:
        store,server,sessions=h['store'],h['server'],h['sessions']
        tmux=Tmux(tmux_binary)
        sessions.tmux=tmux
        spawn=SpawnCtl(store,sessions,tmux=tmux)
        server.spawnctl=h['comms'].spawnctl=spawn
        clock=[NOW]
        runtime=AccountRuntime(store,sessions,policy={'enabled':True},clock=lambda:clock[0])
        runtime.attach(server,spawn,h['comms'])
        runtime.cap_monitor.attach(h['queue'])
        account=binding(host=HOST)
        await runtime.run(lambda a:a.register(account))
        await runtime.run(lambda a:a.set_default(host=HOST,provider='codex',alias='primary'))
        await seed_pin(runtime)
        await artifact_executor(runtime)
        python=shutil.which(sys.executable)
        assert python
        stub=scratch/'provider.py'
        stub.write_text('''import datetime,json,sys
out=open(sys.argv[1],'a',buffering=1)
def record(kind,payload):
 out.write(json.dumps({'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'type':kind,'payload':payload})+'\\n')
record('session_meta',{'id':sys.argv[2]})
print('READY\\nOpenAI Codex\\n› \\n  gpt-6-luna low',flush=True)
for index,line in enumerate(sys.stdin):
 text=line.strip()
 if not text: continue
 record('response_item',{'type':'message','id':str(index),'role':'user','content':[{'type':'input_text','text':text}]})
 print('ECHO '+text+'\\n› ',flush=True)
''')
        def command(name):
            path=scratch/(name+'.jsonl')
            return 'exec '+shlex.join([python,str(stub),str(path),name]),path
        # Replace native executable construction only. Production spawn,
        # account selection/final admission, receipts and restoration remain real.
        async def native_fixture(msg,host,name):
            selected=await runtime.choose_launch(host=host,provider='codex',model='gpt-6-luna',effort='low',message=msg)
            cmd,path=command(name)
            fields={'provider':'codex','effective_model':'gpt-6-luna','effective_effort':'low',
                    'account_binding':selected.public(),'jsonl_path':str(path),
                    'observer_binding':{'executable':python}}
            return cmd,{'account_binding':selected.public()},fields
        spawn._resolve_launch=native_fixture
        old_name='advisor-bound'
        old_stream=HOST+':'+old_name
        cmd,path=command(old_name)
        await tmux.new_session(old_name,cmd)
        await sessions.open(HOST,old_name,provider='codex',account_binding=account.public(),
            pane_pid=await tmux.pane_pid(old_name),pane_status='pane_alive',bootstrap_state='ready',jsonl_path=str(path),
            effective_model='gpt-6-luna',effective_effort='low')
        old=await store.fetch_session(HOST,old_name)
        old_token='fixture-bound-advisor-token'
        await store.grant_stream_token(HOST,old_name,hashlib.sha256(old_token.encode()).hexdigest(),STREAM_TOKEN_HASH_VERSION)
        await sessions.refresh()
        brief=scratch/'advisor-brief';brief.write_text('Answer only the isolated fixture ruling request')
        await store.update_session(HOST,'v2-test',spec_ids=[],assigned_spec_id='')
        desk=await store.fetch_session(HOST,'v2-test')
        await server.lane_rulings.configure(old_stream,actor_stream_id=DESK,
            actor_generation=desk['session_generation'],brief_file=str(brief))
        restore=AuthorityRestore(server,inhibit_path=scratch/'inhibit',clock=lambda:clock[0])
        server.authority_restore=restore
        await restore.initialize()
        async def broadcast(frame): pass
        ingest=Ingest(store,sessions,tmux,broadcast,local_host=HOST,recent_limit=500)
        stop=asyncio.Event()
        async def ingest_loop():
            while not stop.is_set():
                await ingest.run_pass()
                await asyncio.sleep(.03)
        task=asyncio.create_task(ingest_loop())
        server.port,server.binds=0,['127.0.0.1']
        port=await server.bind();url=f'ws://127.0.0.1:{port}'
        try:
            async with client(url,DESK,h['tokens']['desk']) as rpc:
                result=await rpc({'type':'account','action':'pool-control-set','request_id':'block',
                    'provider':'codex','pool_id':'pool-primary','expected_revision':0,'operation_id':'block-fixture',
                    'blocked':True,'stop_pct':None,'window':None,'reason':'advisor fixture','authority_ref':'fixture-order'})
                assert result['type']=='account.ok',result
                async def answer(stream,token,key):
                    response=await rpc({'type':'assistant.advise','request_id':'request-'+key,
                                        'work_item':'none','body':'Review this isolated fixture'})
                    request=response['ruling_request_id']
                    await h['queue'].drain_once(force=True,kinds=['assistant_lane_ruling_request'])
                    pane=await tmux.capture(stream.split(':',1)[1])
                    assert request in pane,pane
                    async with client(url,stream,token) as advisor:
                        response=await advisor({'type':'assistant.answer','request_id':'answer-'+key,
                            'ruling_request_id':request,'body':'Within the isolated scope'})
                        assert response['type']=='assistant.answer.ok',response
                await answer(old_stream,old_token,'before')
                await tmux.kill_session(old_name)
                assert await tmux.session_state(old_name)=='gone'
                await store.update_session(HOST,old_name,pane_status='pane_dead')
                await sessions.refresh()
                await restore.observe(old_stream,old['session_generation'],'pane_dead',old)
                clock[0]+=61
                await restore.observe(old_stream,old['session_generation'],'pane_dead',old)
                if restore.task: await restore.task
                for _ in range(100):
                    await restore.run()
                    episode=await restore.current(old_stream,old['session_generation'])
                    if episode and episode['state']=='restored': break
                    await asyncio.sleep(.1)
                assert episode['state']=='restored',episode
                current=await server.lane_rulings.binding()
                successor=current['stream_id'];name=successor.split(':',1)[1]
                token='fixture-successor-token'
                await store.grant_stream_token(HOST,name,hashlib.sha256(token.encode()).hexdigest(),STREAM_TOKEN_HASH_VERSION)
                await answer(successor,token,'after')
                status=await spawn.spawn_status({'target':episode['spawn_key'],'host':HOST},HOST)
                assert any(o['state']=='delivered' for o in status['outcomes']),status
                ordinary=await rpc({'type':'spawn','request_id':'ordinary','host':HOST,'session_name':'seat-a',
                    'provider':'codex','model':'gpt-6-luna','effort':'low','provision_only':True,'objective':'Negative control'})
                assert 'account_pool_blocked' in (ordinary.get('error_code'),ordinary.get('error')),ordinary
                print(json.dumps({'proof':'advisor_restore','initial_answer':True,'pane_killed':True,
                                  'restore_admitted':True,'successor_answer':True,'ordinary_refused':True}),flush=True)
        finally:
            stop.set();task.cancel();await asyncio.gather(task,return_exceptions=True)
            await server.close()
            await tmux.run('kill-server')


if __name__=='__main__':
    asyncio.run(run(Path(sys.argv[1]),sys.argv[2]))
