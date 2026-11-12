"""Synthetic non-socket controls for the separate proposed integration patch."""
import asyncio
import json
from contextlib import asynccontextmanager
import pytest
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions, VerbError
from store import Store

@asynccontextmanager
async def fixture(tmp_path, monkeypatch):
    folder=tmp_path/'memory/work/in_progress/demo__linked'; folder.mkdir(parents=True)
    (folder/'spec.md').write_text('---\nid: spec_demo__linked\ntitle: Fixture\nstatus: in_progress\n---\n## Goal\nExercise retries.\n')
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path/'memory'))
    store=Store(':memory:'); store.start()
    try:
        root=await store.open_session('fixture','primary',provider='codex')
        advisor=await store.open_session('fixture','advisor',provider='codex',pane_status='pane_alive')
        class Spawn:
            calls=0
            failures=['invalid_cwd','provider_unavailable']
            async def spawn(self,msg,host):
                self.calls+=1
                if self.failures: raise VerbError(self.failures.pop(0),'Synthetic transient failure')
                return {'type':'spawn.ok','stream_id':'fixture:created'}
        spawned=Spawn()
        sessions=Sessions(store,tmux=None,local_host='fixture')
        server=Server(store=store,sessions=sessions,spawnctl=spawned,local_host='fixture')
        server.assistant_composite=AssistantComposite(store,config=AssistantCompositeConfig.from_env({
            'PENTACLE_ASSISTANT_COMPOSITE_ENABLED':'1','PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID':'fixture:assistant',
            'PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID':'fixture:primary',
            'PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION':root['session_generation'],
            'PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID':'fixture:advisor'}))
        msg={'type':'spawn','spec_id':'spec_demo__linked','host':'fixture','session_name':'created','role':'lead',
             'request_id':'retry-fixture','idempotency_key':'retry-fixture','objective':'Exercise approved retry',
             '_auth_context':{'token_verified':True,'stream_id':'fixture:primary','session_generation':root['session_generation']}}
        server.lane_rulings._ensure_worker = lambda: None  # deterministic explicit release only
        first=await server._on_spawn(msg); rid=first['ruling_request_id']
        async def rule(value='approve'):
            return await server._on_assistant_ruling({'ruling_request_id':rid,'request_id':'rule-fixture','ruling':value,
                'conditions':'Keep the approved conditions','reason':'Synthetic approval',
                '_auth_context':{'token_verified':True,'stream_id':'fixture:advisor','session_generation':advisor['session_generation']}})
        yield store,server,spawned,msg,rid,rule
    finally:
        if 'server' in locals(): await server.lane_rulings.stop()
        store.stop()

def test_approved_retry_multiple_failures_conditions_and_duplicate(tmp_path,monkeypatch):
    async def go():
        async with fixture(tmp_path,monkeypatch) as (store,server,spawned,msg,rid,rule):
            assert (await rule())['state']=='release_blocked'
            before=await server.lane_rulings._fetch(rid)
            again=await server._on_spawn({**msg,'request_id':'fresh-rpc-2'})
            assert spawned.calls==2, again
            assert again['state']=='release_blocked'
            row=await server.lane_rulings._fetch(rid)
            assert json.loads(row['outcome_json'])['error_code']=='provider_unavailable'
            results=await asyncio.gather(server._on_spawn({**msg,'request_id':'fresh-rpc-3'}),server._on_spawn({**msg,'request_id':'fresh-rpc-4'}))
            assert all(r['type'] in {'spawn.ok','spawn.pending_ruling'} for r in results),results
            assert spawned.calls==3
            assert (await server._on_spawn(msg))['type']=='spawn.ok' and spawned.calls==3
            after=await server.lane_rulings._fetch(rid)
            for field in ('intent_json','intent_digest','ruling','ruling_key','ruling_digest','conditions'):
                assert after[field]==before[field]
            rows=await store.submit(lambda c:list(c.execute("SELECT notice_id,body FROM v2_outbound_notices WHERE kind='assistant_lane_ruling_result'")))
            assert len(rows)==3,rows
            bodies=[json.loads(r[1].split('] ',1)[1]) for r in rows]
            assert [b['state'] for b in bodies]==['release_blocked','release_blocked','done']
            assert all(b['conditions']=='Keep the approved conditions' for b in bodies)
            await server.lane_rulings._result_notice(after)
            assert await store.submit(lambda c:c.execute("SELECT count(*) FROM v2_outbound_notices WHERE kind='assistant_lane_ruling_result'").fetchone()[0])==3
    asyncio.run(go())

@pytest.mark.parametrize('ruling',['deny','revise',None])
def test_nonapproved_never_releases(tmp_path,monkeypatch,ruling):
    async def go():
        async with fixture(tmp_path,monkeypatch) as (store,server,spawned,msg,rid,rule):
            if ruling: await rule(ruling)
            else:
                await store.submit(lambda c:c.execute("UPDATE v2_assistant_lane_rulings SET state='release_blocked' WHERE ruling_request_id=?",(rid,)))
            reply=await server._on_spawn(msg)
            assert reply['type']=='spawn.ruling_refused' and spawned.calls==0
    asyncio.run(go())

def test_changed_intent_and_generation_cannot_retry(tmp_path,monkeypatch):
    async def go():
        async with fixture(tmp_path,monkeypatch) as (store,server,spawned,msg,rid,rule):
            await rule()
            with pytest.raises(VerbError): await server._on_spawn({**msg,'objective':'Different intent'})
            # _on_spawn is post-auth; malformed wire custody is rejected by dispatch.
            # At the ruling seam it must never recover this known approval.
            assert await server.lane_rulings.request_spawn({**msg,'_auth_context':{**msg['_auth_context'],'session_generation':'stale'}}) is None
            assert spawned.calls==1
            await store.update_session('fixture','primary',status='closed')
            reply=await server._on_spawn(msg)
            assert reply['type']=='spawn.ruling_refused' and spawned.calls==1
            assert json.loads((await server.lane_rulings._fetch(rid))['outcome_json'])['error']=='requester_generation_changed'
    asyncio.run(go())

def test_unauthenticated_wire_cannot_recover_approval(tmp_path,monkeypatch):
    async def go():
        async with fixture(tmp_path,monkeypatch) as (store,server,spawned,msg,rid,rule):
            await rule()
            class Unauthenticated:
                remote_address=('192.0.2.1',4444)
                transport=None
            wire={k:v for k,v in msg.items() if k!='_auth_context'}
            reply=await server._dispatch(json.dumps(wire),websocket=Unauthenticated())
            assert reply[0]['type'].endswith('.error'),reply
            assert spawned.calls==1
            assert (await server.lane_rulings._fetch(rid))['state']=='release_blocked'
    asyncio.run(go())
