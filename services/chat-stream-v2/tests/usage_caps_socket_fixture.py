"""Separate-process socket cap proof with an isolated store and inert provider."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
for path in (ROOT/'services', ROOT/'services/chat-stream-v2', ROOT/'services/chat-stream-v2/tests',
             ROOT/'services/chat-stream-v2/tests/smoke', ROOT/'services/_shared', ROOT/'services/agent-orch'):
    sys.path.insert(0,str(path))

from account_runtime import AccountRuntime
from account_cap_monitor import WARNING
from front_desk_ingress_replay import harness, HOST, DESK
from spawnctl import SpawnCtl
from test_account_rotation import binding, observation, NOW
from test_advisor_sessions_socket import client


async def run(scratch):
    async with harness(scratch) as h:
        server,store=h['server'],h['store']
        spawn=SpawnCtl(store,h['sessions'],tmux=h['provider'])
        server.spawnctl=spawn
        clock=[NOW]
        runtime=AccountRuntime(store,h['sessions'],policy={'enabled':True,'usage_reading_grace_seconds':10},clock=lambda:clock[0])
        runtime.attach(server,spawn,h['comms'])
        runtime.cap_monitor.attach(h['queue'])
        account=binding(host=HOST)
        await runtime.run(lambda a:a.register(account))
        await runtime.run(lambda a:a.set_default(host=HOST,provider='codex',alias='primary'))
        async def sample(pct):
            value=observation(account,clock[0])
            value['windows']['weekly'].update(pct=pct,reset_at=NOW+1000)
            await runtime.record_observation(account,value)
        await sample(18)
        server.port,server.binds=0,['127.0.0.1']
        port=await server.bind()
        try:
            async with client(f'ws://127.0.0.1:{port}',DESK,h['tokens']['desk']) as rpc:
                reply=await rpc({'type':'account','action':'pool-control-set','request_id':'set-cap',
                    'provider':'codex','pool_id':'pool-primary','expected_revision':0,'operation_id':'fixture-cap',
                    'blocked':False,'stop_pct':21,'window':'weekly','reason':'socket fixture','authority_ref':'fixture-order'})
                assert reply['type']=='account.ok',reply
                await runtime.cap_monitor.tick()
                assert not [n for n in await store.submit(lambda c:[dict(r) for r in c.execute(
                    "SELECT kind FROM v2_outbound_notices WHERE kind=?",(WARNING,))])]
                await sample(19)
                await runtime.cap_monitor.tick()
                await h['queue'].drain_once(force=True,kinds=[WARNING])
                notices=await store.submit(lambda c:[dict(r) for r in c.execute(
                    "SELECT tell_id FROM v2_outbound_notices WHERE kind=?",(WARNING,))])
                assert len(notices)==1,notices
                receipt=await store.get_tell_delivery(notices[0]['tell_id'])
                assert receipt and receipt['reply']['submission_confirmed'],(receipt,await store.outbound_notice_for_dedupe(notices[0]['tell_id']))
                await sample(21)
                reply=await rpc({'type':'spawn','request_id':'cap-spawn','host':HOST,'session_name':'seat-a',
                    'provider':'codex','model':'gpt-6-luna','effort':'low','provision_only':True,
                    'objective':'Isolated cap refusal'})
                assert reply.get('error_code')=='account_pool_cap_reached',reply
                await sample(18)
                clock[0]+=10
                async def status(key):
                    result=await rpc({'type':'account','action':'status','request_id':key})
                    return next(p for p in result['quota_pools'] if p['pool_id']=='pool-primary')
                assert (await status('at-grace'))['eligibility']['eligible']
                clock[0]+=1
                assert (await status('past-grace'))['eligibility']['code']=='account_pool_meter_unavailable'
                print(json.dumps({'proof':'cap_socket','warning_submitted':True,'spawn_refused':'account_pool_cap_reached',
                                  'grace_boundary_eligible':True,'past_grace':'account_pool_meter_unavailable'}),flush=True)
        finally:
            await server.close()


if __name__=='__main__':
    scratch=Path(sys.argv[1]).resolve()
    os.environ['HOME']=str(scratch/'home')
    (scratch/'home').mkdir(exist_ok=True)
    asyncio.run(run(scratch))
