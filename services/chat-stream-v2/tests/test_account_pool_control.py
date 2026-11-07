"""Fixed pool controls: exercise the existing RPC and launch journey."""
import asyncio
import json

import pytest

from sessions import VerbError
from provider_accounts import AccountError
from test_account_runtime import setup, AUTH
from test_account_rotation import NOW, binding, observation

OPERATOR={"operator_authenticated":True,"credential_id":"fixture-credential",
          "operator_principal":"operator:fixture-credential"}
PEER={"token_verified":True,"stream_id":"host-a:peer","session_generation":"peer-one"}


def control(**changes):
    return dict(version=1, revision=1, blocked=False, stop_pct=21, window="weekly",
                reason="bounded fixture ceiling", authority_ref="fixture-order",
                set_by={"stream_id": AUTH["stream_id"], "session_generation": AUTH["session_generation"]},
                set_at=NOW) | changes


async def seed(runtime, value=None, pct=21):
    obs=observation(binding()); obs["windows"]["weekly"]["pct"]=pct
    await runtime.record_observation(binding(),obs)
    def write(a):
        a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",(
            "account.pool-control:codex:pool-primary",json.dumps(value or control())))
        a.conn.commit()
    await runtime.run(write)


def setter(**changes):
    return dict(action="pool-control-set", provider="codex", pool_id="pool-primary",
                expected_revision=0, operation_id="fixture-set", blocked=False,
                stop_pct=21, window="weekly", reason="bounded fixture ceiling",
                authority_ref="fixture-order", _auth_context=AUTH) | changes


def test_cap_shaped_current_row_refuses_actual_launch_selection():
    async def run():
        store,runtime=await setup()
        try:
            await seed(runtime)
            with pytest.raises(AccountError,match="account_pool_cap_reached"):
                await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",message={})
        finally: store.stop()
    asyncio.run(run())


def test_pool_setter_and_effective_readback_replace_informational_policy_cap():
    async def run():
        store,runtime=await setup()
        try:
            runtime.policy["account_caps"]={"codex":{"pool-primary":{"stop_pct":1}}}
            await runtime.record_observation(binding(),observation(binding()))
            result=await runtime.operation(setter())
            assert result["receipt"]["control"]["revision"]==1
            read=await runtime.operation({"action":"status","_auth_context":AUTH})
            pool=next(p for p in read["quota_pools"] if p["pool_id"]=="pool-primary")
            assert pool["cap"]["stop_pct"]==21
            assert pool["legacy_cap"]=={"stop_pct":1}
            assert pool["eligibility"]["code"]=="account_pool_cap_reached"
        finally: store.stop()
    asyncio.run(run())


def test_at_cap_final_physical_input_guard_refuses_before_action():
    async def run():
        store,runtime=await setup(initialize_pin=False)
        try:
            await seed(runtime)
            row=await store.open_session("host-a","worker",provider="codex",session_generation="worker-one",
                                         account_binding=binding().public())
            with pytest.raises(AccountError,match="pool_at_cap"):
                async with runtime.input_guard(row):
                    pytest.fail("physical input permitted")
        finally: store.stop()
    asyncio.run(run())


def test_autonomous_work_gets_one_reminder_on_existing_account_tick():
    async def run():
        from account_cap_monitor import CapMonitor,REMINDER
        class Notices:
            def __init__(self): self.notices={}
            def register_kind(self,*args,**kwargs): pass
            async def enqueue(self,**row): self.notices[row['tell_id']]=row
        store,runtime=await setup(initialize_pin=False)
        try:
            await seed(runtime)
            row=await runtime.sessions.open('host-a','worker',provider='codex',session_generation='worker-one',
                                            account_binding=binding().public(),pane_status='pane_alive')
            runtime.sessions.apply_live('host-a:worker',online=True,working=True,capture_generation=row['session_generation'],
                capture_liveness='idle',capture_observed_at=NOW)
            notices=Notices(); runtime.cap_monitor=CapMonitor(runtime); runtime.cap_monitor.attach(notices)
            await runtime.tick(); await runtime.tick()
            assert [n['kind'] for n in notices.notices.values()]==[REMINDER]
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('auth',[{},PEER,dict(AUTH,session_generation='old'),
    dict(OPERATOR,operator_authority_source='stream_token'),dict(OPERATOR,credential_id=''),
    dict(OPERATOR,operator_principal='operator:other')])
def test_control_writers_require_current_primary_or_real_operator(auth):
    async def run():
        store,runtime=await setup()
        try:
            await runtime.record_observation(binding(),observation(binding()))
            with pytest.raises(VerbError,match='account_control_unauthorized'):
                await runtime.operation(setter(_auth_context=auth))
            assert (await runtime.run(lambda a:a.readback(now=NOW)))['quota_pools'][0]['cap'] is None
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('auth',[AUTH,OPERATOR])
def test_control_cas_supersession_lost_reply_and_old_operation_replay(auth):
    async def run():
        store,runtime=await setup()
        try:
            await runtime.record_observation(binding(),observation(binding()))
            first=setter(_auth_context=auth)
            saved=await runtime.operation(first)
            with pytest.raises(VerbError,match='revision_conflict'):
                await runtime.operation(setter(operation_id='stale',_auth_context=auth))
            with pytest.raises(VerbError,match='supersession_required'):
                await runtime.operation(setter(expected_revision=1,operation_id='relax',stop_pct=100,_auth_context=auth))
            newer=await runtime.operation(setter(expected_revision=1,operation_id='relax',stop_pct=100,
                supersedes_authority_ref='fixture-order',authority_ref='new-order',_auth_context=auth))
            replay=await runtime.operation(first)
            assert replay['receipt']==saved['receipt'] and replay['current']==newer['current']
            assert replay['replayed'] and replay['current']['revision']==2
            with pytest.raises(VerbError,match='operation_id_conflict'):
                await runtime.operation(dict(first,stop_pct=22))
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('change',[{'stop_pct':True},{'stop_pct':float('nan')},{'stop_pct':float('inf')},
    {'stop_pct':-1},{'stop_pct':101},{'blocked':1},{'expected_revision':False},
    {'stop_pct':None,'window':'weekly'},{'stop_pct':21,'window':'unknown'},
    {'pool_id':'invented'},{'reason':''},{'authority_ref':''}])
def test_bad_controls_never_mutate_current_or_operation_receipts(change):
    async def run():
        store,runtime=await setup()
        try:
            await runtime.record_observation(binding(),observation(binding()))
            with pytest.raises(VerbError): await runtime.operation(setter(**change))
            assert await runtime.run(lambda a:a.conn.execute("SELECT COUNT(*) FROM kv WHERE k LIKE 'account.control-operation:%'").fetchone()[0])==0
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('cell',['stale','future','expired','no_reset','missing_window','failed','bad_pct','bad_max_age','missing'])
def test_unknown_is_distinct_from_exhaustion_and_only_capped_pool_refuses(cell):
    async def run():
        store,runtime=await setup()
        try:
            await seed(runtime)
            obs=observation(binding())
            if cell=='stale': obs['observed_at']=NOW-601
            if cell=='future': obs['observed_at']=NOW+1
            if cell=='expired': obs['windows']['weekly']['reset_at']=NOW
            if cell=='no_reset': obs['windows']['weekly']['reset_id']=''
            if cell=='missing_window': obs['windows']={}
            if cell=='failed': obs['collection']='failed'
            if cell=='bad_pct': obs['windows']['weekly']['pct']=float('nan')
            if cell=='bad_max_age': runtime.policy['observation_max_age']=0
            if cell=='missing':
                def remove(a):
                    a.conn.execute('DELETE FROM v2_account_observations');a.conn.commit()
                await runtime.run(remove)
            else: await runtime.record_observation(binding(),obs)
            with pytest.raises(AccountError,match='account_pool_meter_unavailable'):
                await runtime.choose_launch(host='host-a',provider='codex',model='fixture',effort='high',message={})
            other=await runtime.choose_launch(host='host-a',provider='codex',model='fixture',effort='high',
                                            message={'account':'spare','_auth_context':AUTH})
            assert other.alias=='spare'
        finally: store.stop()
    asyncio.run(run())


def test_final_admit_rechecks_cap_committed_after_selection_and_recovers_on_fresh_reset():
    async def run():
        store,runtime=await setup(initialize_pin=False)
        try:
            selected=await runtime.choose_launch(host='host-a',provider='codex',model='fixture',effort='high',message={})
            row=await store.open_session('host-a','pending',provider='codex',session_generation='pending-one',account_binding=selected.public())
            await seed(runtime)
            with pytest.raises(AccountError,match='account_pool_cap_reached'): await runtime.admit_launch(row)
            await seed(runtime,pct=20)
            await runtime.admit_launch(row)
            await seed(runtime,pct=100)
            await runtime.admit_launch(row)  # Existing admitted receipt: no second launch.
        finally: store.stop()
    asyncio.run(run())


def test_primary_does_not_implicitly_override_children_and_one_spawn_override_is_revision_bound():
    async def run():
        store,runtime=await setup(initialize_pin=False)
        try:
            await seed(runtime)
            args=dict(host='host-a',provider='codex',model='fixture',effort='high')
            with pytest.raises(AccountError,match='account_pool_cap_reached'):
                await runtime.choose_launch(**args,message={'_auth_context':AUTH})
            message={'_auth_context':AUTH,'pool_override_reason':'owned drill','pool_authority_ref':'one-spawn-order'}
            account=await runtime.choose_launch(**args,message=message)
            row=await store.open_session('host-a','one-spawn',provider='codex',session_generation='one-spawn-gen',account_binding=account.public())
            await seed(runtime,value=control(revision=2))
            with pytest.raises(AccountError,match='override_stale'): await runtime.admit_launch(row,message=message)
            await seed(runtime)
            await runtime.admit_launch(row,message=message)
            with pytest.raises(AccountError,match='pool_at_cap'):
                async with runtime.input_guard(row,message={'_auth_context':PEER}): pass
            with pytest.raises(AccountError,match='account_control_unauthorized'):
                await runtime.choose_launch(**args,message=dict(message,_auth_context=PEER))
        finally: store.stop()
    asyncio.run(run())


def test_input_exemptions_and_exact_generation_override_revoke_without_child_inheritance():
    async def run():
        store,runtime=await setup(initialize_pin=False)
        try:
            await seed(runtime)
            row=await store.open_session('host-a','worker',provider='codex',session_generation='worker-one',account_binding=binding().public())
            for auth in (AUTH,OPERATOR):
                async with runtime.input_guard(row,message={'_auth_context':auth}): pass
            for auth in (PEER,dict(AUTH,session_generation='old'),dict(OPERATOR,operator_authority_source='stream_token')):
                with pytest.raises(AccountError,match='pool_at_cap'):
                    async with runtime.input_guard(row,message={'_auth_context':auth}): pass
            primary=await store.fetch_session('host-a','front-desk')
            primary=dict(primary,account_binding=binding().public())
            async with runtime.input_guard(primary,message={'_auth_context':PEER}): pass
            command=dict(action='seat-override-set',stream=row['stream_id'],expected_generation='worker-one',
                expected_revision=0,operation_id='seat-on',enabled=True,reason='bounded work',authority_ref='seat-order',_auth_context=AUTH)
            on=await runtime.operation(command)
            assert on['current']['revision']==1
            async with runtime.input_guard(row,message={'_auth_context':PEER}): pass
            await seed(runtime,value=control(revision=2))
            async with runtime.input_guard(row,message={'_auth_context':PEER}): pass
            child=await store.open_session('host-a','child',provider='codex',session_generation='child-one',account_binding=binding().public())
            with pytest.raises(AccountError,match='pool_at_cap'):
                async with runtime.input_guard(child,message={'_auth_context':dict(PEER,stream_id=row['stream_id'])}): pass
            await runtime.operation(dict(command,expected_revision=1,operation_id='seat-off',enabled=False))
            with pytest.raises(AccountError,match='pool_at_cap'):
                async with runtime.input_guard(row,message={'_auth_context':PEER}): pass
            with pytest.raises(VerbError,match='operation_id_conflict'):
                await runtime.operation(dict(command,enabled=False))
        finally: store.stop()
    asyncio.run(run())
