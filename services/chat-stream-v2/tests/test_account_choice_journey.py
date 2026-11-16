"""Resolve real spawn commands and run concurrent fake native providers."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import pytest
import launch
from spawnctl import SpawnCtl
from test_account_runtime import setup, AUTH
from test_account_rotation import binding

@pytest.mark.parametrize('provider,model',[('codex','gpt-5.6-sol'),('claude','claude-sonnet-5')])
def test_default_change_and_explicit_choice_run_independent_process_homes(tmp_path,provider,model):
    async def run():
        store,runtime=await setup();processes=[]
        fake=tmp_path/'fake-provider'
        fake.write_text('#!'+sys.executable+'\nimport json,os,time\nfrom pathlib import Path\n'
            'home=Path(os.environ.get("CODEX_HOME") or os.environ["CLAUDE_CONFIG_DIR"])\n'
            '(home/(os.environ["PENTACLE_STREAM_ID"].split(":")[1]+".json")).write_text(json.dumps({"home":str(home),"os_home":os.environ["HOME"]}))\n'
            'time.sleep(.3)\n')
        fake.chmod(0o700)
        ctl=SpawnCtl(store,runtime.sessions,tmux=object(),machine=launch.local_machine('host-a',cwd=str(tmp_path),codex_bin=str(fake),claude_bin=str(fake)))
        ctl.account_runtime=runtime
        try:
            for alias in ('primary','spare'):
                home=tmp_path/alias;home.mkdir(mode=0o700)
                account=replace(binding(alias,provider=provider),generation='fixture-live',home=str(home),executable=str(fake))
                await runtime.run(lambda a,account=account:a.register(account))
            await runtime.run(lambda a:a.set_default(host='host-a',provider=provider,alias='primary'))
            async def plan(name,**extra):
                command,_,fields=await ctl._resolve_launch({'provider':provider,'model':model,'effort':'high',
                    '_defer_launch_token_stage':True,**extra},'host-a',name)
                row=await store.open_session('host-a',name,session_generation=name,provider=provider,account_binding=fields['account_binding'])
                await runtime.bind_launch(row)
                return command
            first=await plan('first')
            await runtime.operation({'action':'default-set','host':'host-a','provider':provider,'account':'spare','_auth_context':AUTH})
            second=await plan('second')
            explicit=await plan('explicit',account='primary',_auth_context=AUTH)
            for command in (first,second,explicit):
                processes.append(subprocess.Popen(['/bin/sh','-c',command],env={'PATH':os.environ['PATH'],
                    'HOME':str(tmp_path/'os-home'),'USER':'fixture'},stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True))
            assert all(p.poll() is None for p in processes)
            for process in processes:
                stdout,stderr=process.communicate(timeout=5)
                assert process.returncode==0,stderr.decode()
            for name,alias in [('first','primary'),('second','spare'),('explicit','primary')]:
                observed=json.loads((tmp_path/alias/(name+'.json')).read_text())
                row=await store.fetch_session('host-a',name)
                assert observed['home']==row['account_binding']['home']==str(tmp_path/alias)
                assert observed['os_home']==str(tmp_path/'os-home')
            assert next(d for d in await runtime.run(lambda a:a.defaults()) if d['provider']==provider)['alias']=='spare'
        finally:
            for process in processes:
                if process.poll() is None: os.killpg(process.pid,signal.SIGKILL)
                process.communicate(timeout=5)
            store.stop()
    asyncio.run(run())
