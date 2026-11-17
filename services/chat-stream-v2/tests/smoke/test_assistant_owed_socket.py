"""Real daemon child + operator credential + CLI, synthetic provider only."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest
from _shared import operator_auth
from websockets.asyncio.client import connect

SERVICE=Path(__file__).resolve().parents[2]
REPO=SERVICE.parents[1]

async def operator_send(port,credential,key):
    credential=operator_auth.decode_envelope(credential)
    async with connect(f'ws://127.0.0.1:{port}') as ws:
        welcome=json.loads(await ws.recv())
        proof=operator_auth.make_proof(credential['proof_key'],welcome['auth']['operator']['nonce'],credential['credential_id'],credential['client_kind'])
        await ws.send(json.dumps({'type':'hello','client':credential['client_kind'],'auth_v2':{'scheme':operator_auth.AUTH_SCHEME,'credential_id':credential['credential_id'],'proof':proof},'subscribe':{'mode':'rpc','snapshot':False}}))
        assert json.loads(await ws.recv())['type']=='ready'
        await ws.send(json.dumps({'type':'send','request_id':key,'msg_id':key,'stream_id':'fixture-host:assistant','text':'Please return a result'}))
        while True:
            row=json.loads(await asyncio.wait_for(ws.recv(),5))
            if row.get('request_id')==key:return row


def eventually(fn,timeout=15):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        value=fn()
        if value:return value
        time.sleep(.05)
    raise AssertionError('observable fixture state did not arrive')

@pytest.mark.parametrize('command',['publish','send-file'])
def test_operator_owed_journey(tmp_path,command):
    env={k:v for k,v in os.environ.items() if not k.startswith(('PENTACLE_','AGENT_ORCH_')) and k not in {'TMUX','TMUX_PANE'}}
    env.update(HOME=str(tmp_path),XDG_CONFIG_HOME=str(tmp_path/'config'),PYTHONDONTWRITEBYTECODE='1',PENTACLE_ASSISTANT_OWES_REQUIRED='1',PYTHONPATH=os.pathsep.join([str(SERVICE),str(SERVICE.parent),str(REPO/'services/agent-orch')]))
    log=(tmp_path/'daemon.log').open('w')
    child=subprocess.Popen([sys.executable,str(SERVICE/'tests/helpers/owed_process.py'),str(tmp_path)],env=env,stdout=log,stderr=subprocess.STDOUT)
    try:
        def ready():
            assert child.poll() is None,(tmp_path/'daemon.log').read_text()
            return json.loads((tmp_path/'ready.json').read_text()) if (tmp_path/'ready.json').exists() else None
        state=eventually(ready)
        response=asyncio.run(operator_send(state['port'],state['credential'],'input-a'));assert response['type']=='send.result',response
        def dispatch():
            rows=json.loads((tmp_path/'pastes.json').read_text()) if (tmp_path/'pastes.json').exists() else []
            return next((r for r in rows if 'dispatch_id: ' in r),None)
        wire=eventually(dispatch);dispatch_id=wire.split('dispatch_id: ',1)[1].splitlines()[0]
        cli_env=dict(env,AGENT_ORCH_WS_URL=f"ws://127.0.0.1:{state['port']}",AGENT_ORCH_STREAM_ID='fixture-host:v2-test',AGENT_ORCH_STREAM_TOKEN=state['token'],AGENT_ORCH_RUNTIME_DIR=str(tmp_path/'runtime'))
        def cli(*args):
            result=subprocess.run([sys.executable,'-m','agent_orch.cli',*args],env=cli_env,text=True,capture_output=True,timeout=15)
            assert result.returncode==0,result.stdout+result.stderr
            return json.loads(result.stdout.splitlines()[-1])
        deadline=(datetime.now(timezone.utc)+timedelta(seconds=4)).isoformat()
        shared=['--dispatch-id',dispatch_id,'--reply-to-message-id','input-a','--publish-kind','prose','--owes','result-a','--owes-due',deadline]
        if command=='publish':published=cli('assistant','publish','--request-id','publish:'+dispatch_id,'--composite-stream-id','fixture-host:assistant','--response-state','final','--message','Result follows',*shared)
        else:
            path=tmp_path/'sample.pdf';path.write_bytes(b'%PDF synthetic result')
            published=cli('send-file',str(path),'--to','fixture-host:assistant','--caption','Result follows',*shared)
        oid=published['owed_id'];assert oid.startswith('owed-')
        listed=cli('assistant','owed','list','--composite-stream-id','fixture-host:assistant');assert listed['items'][0]['owed_id']==oid
        def due_tell():
            return [p for p in json.loads((tmp_path/'pastes.json').read_text()) if 'Owed item due: '+oid in p]
        assert len(eventually(due_tell))==1
        closed=cli('assistant','publish','--proactive','--request-id','result-a','--composite-stream-id','fixture-host:assistant','--publish-kind','prose','--note-type','result','--ref','owed:'+oid,'--message','Here is the result','--closes',oid)
        assert closed['closed_owed_ids']==[oid]
        assert cli('assistant','owed','list','--composite-stream-id','fixture-host:assistant')['items']==[]
        assert len(due_tell())==1
    finally:
        child.terminate();child.wait(timeout=10);log.close()
        assert child.returncode==0,(tmp_path/'daemon.log').read_text()
