"""Authenticated RPCs against a child daemon with a synthetic provider."""
import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from _shared import operator_auth
from websockets.asyncio.client import connect
from tests.smoke.test_assistant_owed_socket import eventually, operator_send, SERVICE, REPO


async def operator_rpc(state, payload):
    credential=operator_auth.decode_envelope(state['credential'])
    async with connect(f"ws://127.0.0.1:{state['port']}") as ws:
        welcome=json.loads(await ws.recv())
        proof=operator_auth.make_proof(credential['proof_key'],welcome['auth']['operator']['nonce'],credential['credential_id'],credential['client_kind'])
        await ws.send(json.dumps({'type':'hello','client':credential['client_kind'],'auth_v2':{'scheme':operator_auth.AUTH_SCHEME,'credential_id':credential['credential_id'],'proof':proof},'subscribe':{'mode':'rpc','snapshot':False}}))
        assert json.loads(await ws.recv())['type']=='ready'
        payload={'request_id':'brief-test-'+payload['type'],**payload}
        await ws.send(json.dumps(payload))
        while True:
            row=json.loads(await asyncio.wait_for(ws.recv(),10))
            if row.get('request_id')==payload['request_id']:return row


@contextmanager
def daemon(tmp_path):
    env={k:v for k,v in os.environ.items() if not k.startswith(('PENTACLE_','AGENT_ORCH_')) and k not in {'TMUX','TMUX_PANE'}}
    env.update(HOME=str(tmp_path),XDG_CONFIG_HOME=str(tmp_path/'config'),PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=os.pathsep.join([str(SERVICE),str(SERVICE.parent),str(REPO/'services/agent-orch')]))
    log=(tmp_path/'daemon.log').open('w')
    child=subprocess.Popen([sys.executable,str(SERVICE/'tests/helpers/owed_process.py'),str(tmp_path),'--brief'],env=env,stdout=log,stderr=subprocess.STDOUT)
    try:
        def ready():
            assert child.poll() is None,(tmp_path/'daemon.log').read_text()
            return json.loads((tmp_path/'ready.json').read_text()) if (tmp_path/'ready.json').exists() else None
        state=eventually(ready)
        cli_env=dict(env,AGENT_ORCH_WS_URL=f"ws://127.0.0.1:{state['port']}",AGENT_ORCH_STREAM_ID='fixture-host:v2-test',AGENT_ORCH_STREAM_TOKEN=state['token'],AGENT_ORCH_RUNTIME_DIR=str(tmp_path/'runtime'))
        def cli(*args):
            result=subprocess.run([sys.executable,'-m','agent_orch.cli',*args],env=cli_env,text=True,capture_output=True,timeout=15)
            assert result.returncode==0,result.stdout+result.stderr
            return json.loads(result.stdout.splitlines()[-1])
        yield state,cli
    finally:
        child.terminate();child.wait(timeout=10);log.close()
        assert child.returncode==0,(tmp_path/'daemon.log').read_text()


def test_brief_contains_owed_hold_and_wake(tmp_path):
    with daemon(tmp_path) as (state,cli):
        assert asyncio.run(operator_send(state['port'],state['credential'],'brief-input'))['type']=='send.result'
        response=asyncio.run(operator_rpc(state,{'type':'assistant.owed.open','message_id':'brief-input','label':'socket owed item'}))
        assert response['type']=='assistant.owed.open.ok',response
        held=cli('hold','acquire','socket-resource','--reason','socket freeze','--ttl','300')
        assert held['type']=='coordination.hold.acquire.ok'
        cli('wake','--in','10m','--note','socket wake reminder')
        response=cli('assistant','brief')
        assert response['type']=='assistant.brief.ok',response
        assert 'socket owed item' in response['text']
        assert 'socket-resource' in response['text']
        assert 'socket wake reminder' in response['text']


def test_brief_reports_future_wake_from_previous_binding(tmp_path):
    with daemon(tmp_path) as (state,cli):
        cli('wake','--in','10m','--note','previous binding reminder')
        response=cli('assistant','rebind','--target','fixture-host:desk-next','--request-id','brief-rebind','--expected-revision','0')
        assert response['type']=='assistant.rebind.ok',response
        # Simulate the provider ending its old seat through the store lifecycle API.
        (tmp_path/'retire-old').touch()
        eventually(lambda:(tmp_path/'retired.json').exists())
        response=asyncio.run(operator_rpc(state,{'type':'assistant.brief'}))
        assert response['type']=='assistant.brief.ok',response
        assert 'previous binding reminder' in response['text']
        assert 'ended with previous binding:' in response['text']
