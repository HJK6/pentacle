"""Real isolated tmux + inert native writer + ingest + Comms + rendered DOM.

No provider binary or account is invoked; every pane/socket/file is test-owned.
"""
import asyncio
from datetime import datetime, timezone
import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from capacity_retry import CapacityRetry
from comms import Comms
from ingest import Ingest
from mirror import Mirror
from prockill import process_record
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from tmux_transport import Tmux
import store_capacity_retry as policy


@pytest.mark.timeout(30)
def test_capacity_journey_with_real_native_frontier_and_single_pastes(tmp_path, isolated_tmux_env):
    async def run():
        tmux = Tmux(isolated_tmux_env)
        name = 'v2-capacity-fixture'; sid = f'fixture:{name}'
        folder = tmp_path/'.codex/sessions'; folder.mkdir(parents=True)
        rollout = folder/'rollout-fixture.jsonl'
        stub = tmp_path/'inert_provider.py'
        stub.write_text('''import json,sys,datetime
out=open(sys.argv[1],'a',buffering=1)
def record(kind,payload):
 out.write(json.dumps({'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'type':kind,'payload':payload})+'\\n')
def fail(n):
 record('event_msg',{'type':'task_complete','turn_id':f'fixture-turn-{n}','error':{'codex_error_info':'server_overloaded'}})
 print('OpenAI Codex\\n› \\n  gpt-6-test high',flush=True)
record('session_meta',{'id':'fixture-native'})
fail(1)
for n,line in enumerate(sys.stdin,2):
 text=line.strip()
 if not text: continue
 record('response_item',{'type':'message','id':f'fixture-user-{n}','role':'user','content':[{'type':'input_text','text':text}]})
 fail(n)
''')
        store=Store(str(tmp_path/'sessions.sqlite3')); store.start()
        tasks=[]; stop=asyncio.Event()
        async def broadcast(frame): pass
        class LocalHosts:
            def tmux_for(self, host): return tmux
            def is_local(self, host): return True
            async def run_command(self, host, *args, timeout=10):
                child=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, _=await asyncio.wait_for(child.communicate(),timeout)
                    return child.returncode,stdout.decode()
                finally:
                    if child.returncode is None:
                        child.kill(); await child.wait()
        try:
            await tmux.new_session(name, 'exec '+shlex.join([sys.executable,str(stub),str(rollout)]))
            for _ in range(100):
                if rollout.exists() and len(rollout.read_text().splitlines()) >= 2: break
                await asyncio.sleep(.01)
            pid=await tmux.pane_pid(name); process=await process_record(pid)
            assert process
            await store.open_session('fixture',name,provider='codex',pane_pid=pid,pane_status='pane_alive',
                bootstrap_state='ready',session_generation='fixture-generation',
                observer_binding={'generation':'fixture-generation','pane_pid':pid,
                                  'pane_started_at':process['start_id'],'executable':sys.executable})
            sessions=Sessions(store,tmux=tmux,local_host='fixture'); await sessions.refresh()
            ingest=Ingest(store,sessions,tmux,broadcast,local_host='fixture',recent_limit=500)
            await ingest.run_pass()
            mirror=Mirror(store,sessions,tmux,broadcast,local_host='fixture')
            await mirror.run_pass()
            observed = sessions.get(sid)
            assert observed.get('online') is True, observed
            assert observed.get('working') is False, observed
            assert observed.get('mirror_generation') == 'fixture-generation', observed
            hosts=LocalHosts()
            comms=Comms(store,sessions,SpawnCtl(store,sessions,tmux=tmux),hosts=hosts)
            now=[time.time()]
            retry=CapacityRetry(store,sessions,comms,hosts,clock=lambda:now[0])
            async def ingest_loop():
                while not stop.is_set():
                    await ingest.run_pass(); await asyncio.sleep(.01)
            tasks.append(asyncio.create_task(ingest_loop()))
            async def state(): return await store.submit(lambda c:policy.load(c,sid))
            for number in (1,2,3):
                pending=await state()
                assert pending['state']=='waiting',pending
                now[0]=pending['next_retry_at']-1; await retry.tick()
                assert await store.submit(lambda c:c.execute('SELECT count(*) FROM v2_tell_deliveries').fetchone()[0])==number-1
                now[0]=pending['next_retry_at']; await retry.tick()
                for _ in range(100):
                    current=await state()
                    if current['state'] in ('waiting','exhausted'): break
                    await asyncio.sleep(.01)
                assert (await state())['attempts_used']==number
                await mirror.run_pass()
            assert (await state())['state']=='exhausted'
            records=[json.loads(line) for line in rollout.read_text().splitlines()]
            users=[r for r in records if r.get('payload',{}).get('role')=='user']
            assert len(users)==3 and all(r['payload']['content'][0]['text']=='continue' for r in users)
            assert await store.submit(lambda c:c.execute('SELECT count(*) FROM v2_tell_deliveries').fetchone()[0])==3
            await retry.refresh(sid)
            row=sessions.get(sid)
            root=Path(__file__).resolve().parents[3]
            render="const ui=require('./renderer/chat_ui_state'); const s=JSON.parse(process.argv[1]); const html=ui.renderSessionStatusCardView(s); if(!/exhausted.*manual action needed/.test(html)||!html.includes('3/3'))process.exit(1);"
            child=await asyncio.create_subprocess_exec('node','-e',render,json.dumps(row),cwd=str(root))
            assert await child.wait()==0
        finally:
            stop.set()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await tmux.kill_session(name)
            assert not await tmux.has_session(name)
            store.stop()
    asyncio.run(run())
