"""Generation-bound front-desk authority and manual-only account control."""
import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace
import pytest
from account_runtime import AccountRuntime
from sessions import Sessions, VerbError
from store import Store
from test_account_rotation import binding, policy, NOW

AUTH={"token_verified":True,"stream_id":"host-a:front-desk","session_generation":"generation-one"}

async def setup(path=":memory:"):
    store=Store(path); store.start(); sessions=Sessions(store,local_host="host-a")
    runtime=AccountRuntime(store,sessions,policy=policy(),clock=lambda:NOW)
    composite=SimpleNamespace(enabled=True,env_config=SimpleNamespace(direct_primary=True,name="front-desk"),
        _env_binding=lambda:{"stream_id":AUTH["stream_id"],"generation":AUTH["session_generation"]})
    runtime.attach(SimpleNamespace(assistant_composite=composite,handlers={}),SimpleNamespace(),SimpleNamespace())
    await store.open_session("host-a","front-desk",session_generation="generation-one",provider="codex")
    for alias in ("primary","spare"): await runtime.run(lambda a,alias=alias:a.register(binding(alias)))
    await runtime.run(lambda a:a.set_default(host="host-a",provider="codex",alias="primary"))
    return store,runtime

@pytest.mark.parametrize("auth",[{},dict(AUTH,token_verified=False),dict(AUTH,session_generation="old"),
    dict(AUTH,stream_id="host-a:worker"),{"role":"front-desk","from_stream_id":AUTH["stream_id"]}])
def test_worker_spoofed_and_stale_control_refuse_before_default_mutation(auth):
    async def run():
        store,runtime=await setup()
        try:
            with pytest.raises(VerbError,match="account_control_unauthorized"):
                await runtime.operation({"action":"default-set","host":"host-a","provider":"codex","account":"spare","_auth_context":auth})
            with pytest.raises(Exception,match="account_control_unauthorized"):
                await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",message={"account":"spare","_auth_context":auth})
            assert (await runtime.run(lambda a:a.defaults()))[0]["alias"]=="primary"
        finally: store.stop()
    asyncio.run(run())

def test_front_desk_defaults_and_concurrent_agents_bind_once(tmp_path):
    async def run():
        store,runtime=await setup(str(tmp_path/"state.db"))
        try:
            async def choose(message):
                return await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",message=message)
            first=await choose({})
            await runtime.operation({"action":"default-set","host":"host-a","provider":"codex","account":"spare","_auth_context":AUTH})
            second=await choose({})
            explicit=await choose({"account":"primary","_auth_context":AUTH})
            for name,account in (("one",first),("two",second),("three",explicit)):
                row=await store.open_session("host-a",name,session_generation=name,provider="codex",account_binding=account.public())
                await runtime.bind_launch(row)
            assert first.alias==explicit.alias=="primary" and second.alias=="spare"
            await runtime.operation({"action":"default-set","host":"host-a","provider":"codex","account":"primary","_auth_context":AUTH})
            assert (await store.fetch_session("host-a","two"))["account_binding"]==second.public()
            runtime.policy["enabled"]=False
            await runtime.before_input(await store.fetch_session("host-a","two")) # drain on rollback
            assert await choose({}) is None
        finally: store.stop()
    asyncio.run(run())

def test_watcher_auth_failure_reports_only_cell_and_never_relogs():
    async def run():
        store,runtime=await setup(); calls=[]
        runtime.policy["watchers"]={"host-a:account-watcher":{"host":"host-a"}}
        class Executor:
            async def recover(self,*args,**kwargs): calls.append("relogin"); raise AssertionError()
        runtime.executor=Executor()
        message={"action":"observe-failure","host":"host-a","provider":"codex","account":"primary","kind":"auth",
            "login_generation":"login-one","_auth_context":{"service_authenticated":True,"service_actor":"host-a:account-watcher"}}
        try:
            assert (await runtime.operation(message))["accepted"]
            assert not calls
            other=await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",
                message={"account":"spare","_auth_context":AUTH})
            assert other.alias=="spare"
            with pytest.raises(Exception,match="account_unavailable"):
                await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",message={})
            with pytest.raises(VerbError,match="account_observation_unauthorized"):
                await runtime.operation(dict(message,host="host-b"))
        finally: store.stop()
    asyncio.run(run())

def test_startup_never_reactivates_old_login_from_private_seed():
    async def run():
        store,runtime=await setup()
        try:
            old=binding(); new=replace(old,generation="login-two",home="/private-fixture/new-home")
            await runtime.run(lambda a:a.register(new))
            runtime.policy["bindings"]=[asdict(old)]
            await runtime.start()
            current=await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",message={})
            assert current.generation=="login-two"
        finally: store.stop()
    asyncio.run(run())


def test_auth_failure_records_existing_error_alert_with_cell_identity():
    async def run():
        store,runtime=await setup(); seen=[]
        class Alerts:
            async def error(self,fact,*,principal): seen.append((fact,principal)); return "notice-one"
        runtime.sessions.alerts=Alerts()
        try:
            await runtime.record_failure(binding(),"auth")
            assert len(seen)==1
            fact,principal=seen[0]
            assert fact.family=="provider_account.v1" and fact.code=="login_unavailable"
            assert principal=="system:host-a:codex:primary"
            assert not runtime.executor
        finally: store.stop()
    asyncio.run(run())


def test_rollout_scope_preserves_untouched_hosts_and_never_unbinds_managed_cells():
    async def run():
        store,runtime=await setup()
        try:
            runtime.policy["managed_hosts"]=["host-a"]
            assert await runtime.choose_launch(host="host-untouched",provider="codex",model="fixture",effort="high",message={}) is None
            runtime.policy["managed_hosts"]=[]
            with pytest.raises(Exception,match="account_feature_off"):
                await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",message={})
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("launch_request",[
    {"provider":"codex","model":"gpt-5.6-sol","effort":"high"},
    {"command":"/bin/echo rollback-ready"},
])
def test_policy_off_restores_pre_activation_launch_on_registered_database_copy(tmp_path, launch_request):
    import sqlite3
    import launch
    from spawnctl import SpawnCtl
    async def run():
        original=str(tmp_path/"before.db"); copied=str(tmp_path/"rollback.db")
        store=Store(original); store.start(); sessions=Sessions(store,local_host="host-a")
        runtime=AccountRuntime(store,sessions,policy={"enabled":False})
        machine=launch.local_machine("host-a",cwd=str(tmp_path),codex_bin="/bin/echo")
        def controller():
            ctl=SpawnCtl(store,runtime.sessions,tmux=object(),machine=machine)
            ctl.account_runtime=runtime
            return ctl
        message=dict(launch_request,_defer_launch_token_stage=True)
        try:
            before=await controller()._resolve_launch(message,"host-a","same-name")
            runtime.policy["enabled"]=True
            await runtime.run(lambda a:a.register(binding()))
            await runtime.run(lambda a:a.set_default(host="host-a",provider="codex",alias="primary"))
            existing=await store.open_session("host-a","bound-before-rollback",provider="codex",
                session_generation="generation-one",account_binding=binding().public())
            await runtime.bind_launch(existing)
        finally: store.stop()
        with sqlite3.connect(original) as source, sqlite3.connect(copied) as target:
            source.backup(target)
        store=Store(copied); store.start()
        runtime.store=store; runtime.sessions=Sessions(store,local_host="host-a")
        runtime.policy={"enabled":False}
        try:
            after=await controller()._resolve_launch(message,"host-a","same-name")
            assert after[0]==before[0]
            # Each fresh launch mints a new token/native ID; compare admission
            # and the complete stable tuple/observer fields, not that entropy.
            assert {k:v for k,v in after[1].items() if k!="_deferred_launch_plan"}=={
                k:v for k,v in before[1].items() if k!="_deferred_launch_plan"}
            volatile={"token_hash","claude_session_id","jsonl_path"}
            assert {k:v for k,v in after[2].items() if k not in volatile}=={
                k:v for k,v in before[2].items() if k not in volatile}
            assert "account_binding" not in after[2]
            assert (await store.fetch_session("host-a","bound-before-rollback"))["account_binding"]==binding().public()
            assert (await runtime.run(lambda a:a.defaults()))[0]["alias"]=="primary"
            assert await runtime.run(lambda a:a.bound(binding().key)) is not None
            with pytest.raises(VerbError) as refused:
                await controller()._resolve_launch(dict(message,command="/bin/echo refuse",account="primary"),
                    "host-a","refused-override")
            assert refused.value.code=="managed_command_forbidden"
        finally: store.stop()
    asyncio.run(run())


def test_policy_off_refuses_explicit_choice_and_bound_home_resume():
    async def run():
        store,runtime=await setup()
        try:
            runtime.policy["enabled"]=False
            for message in ({"account":"primary","_auth_context":AUTH},
                            {"resume_session_id":"native-id","_account_resume_binding":binding().public()}):
                with pytest.raises(Exception,match="account_feature_off"):
                    await runtime.choose_launch(host="host-a",provider="codex",model="fixture",effort="high",message=message)
        finally: store.stop()
    asyncio.run(run())


def test_requested_account_handoff_requires_context_then_uses_fresh_native_home(tmp_path):
    import launch
    from spawnctl import SpawnCtl
    async def run():
        store,runtime=await setup()
        ctl=SpawnCtl(store,runtime.sessions,tmux=object(),machine=launch.local_machine("host-a",cwd=str(tmp_path),codex_bin="/bin/echo"))
        ctl.account_runtime=runtime
        try:
            await store.update_session("host-a","front-desk",effective_model="gpt-5.6-sol",effective_effort="high")
            request={"handoff":True,"handoff_from_stream_id":AUTH["stream_id"],"account":"spare","_auth_context":AUTH,
                "_defer_launch_token_stage":True}
            with pytest.raises(VerbError) as error:
                await ctl._resolve_launch(request,"host-a","successor")
            assert error.value.code=="account_handoff_checkpoint_required"
            command,resolution,fields=await ctl._resolve_launch(dict(request,initial_prompt="Checkpoint: keep the pending work."),"host-a","successor")
            assert binding("spare").home in command and fields["account_binding"]["alias"]=="spare"
            assert "--resume" not in command
        finally: store.stop()
    asyncio.run(run())


def test_retired_source_can_only_read_existing_account_handoff_receipt():
    from spawnctl import SpawnCtl
    async def run():
        store,runtime=await setup();ctl=SpawnCtl(store,runtime.sessions,tmux=object());ctl.account_runtime=runtime
        seen=[]
        async def receipt(message,host,owner):
            seen.append(owner)
            return {"type":"spawn.ok","do_not_respawn":True,"stream_id":"host-a:existing-successor"}
        ctl._replay_retired_handoff=receipt # counterpart: existing exact-receipt verifier
        try:
            result=await ctl.spawn({"handoff":True,"account":"spare","_auth_context":{
                "token_verified":False,"retired_handoff_owner":{"stream_id":AUTH["stream_id"]}}},"host-a")
            assert result["do_not_respawn"] and len(seen)==1
            assert (await runtime.run(lambda a:a.defaults()))[0]["alias"]=="primary"
        finally: store.stop()
    asyncio.run(run())


def test_completed_relogin_replays_original_generation_after_restart(tmp_path):
    from account_executor import AccountExecutor
    calls=[]
    async def execute(account,action,operation_id,generation=None):
        calls.append(action)
        if action=="observe": return {"outcome":"provider_auth_failed"}
        return {"state":"done","outcome":"ok","binding":replace(account,generation=generation,
            home="/private-fixture/"+generation).public()}
    async def run():
        path=str(tmp_path/"state.db")
        store,runtime=await setup(path)
        request={"action":"relogin","host":"host-a","provider":"codex","account":"primary",
            "login_generation":"login-one","operation_id":"original","_auth_context":AUTH}
        try:
            runtime.executor=AccountExecutor(runtime,None,execute=execute)
            result=await runtime.operation(request)
        finally: store.stop()
        # A new executor/store must find the durable receipt without another probe/login.
        store=Store(path); store.start(); runtime.store=store; runtime.sessions.store=store
        runtime.executor=AccountExecutor(runtime,None,execute=execute)
        try:
            assert await runtime.operation(request)==result
            assert await runtime.operation(dict(request,login_generation="active"))==result
            assert calls==["observe","enroll"]
            with pytest.raises(VerbError,match="account_login_generation_stale"):
                await runtime.operation(dict(request,operation_id="new-operation"))
            with pytest.raises(VerbError,match="account_control_unauthorized"):
                await runtime.operation(dict(request,_auth_context=dict(AUTH,session_generation="stale")))
            assert calls==["observe","enroll"]
        finally: store.stop()
    asyncio.run(run())


def test_interrupted_receipt_save_inspects_original_generation_after_activation(tmp_path):
    from account_executor import AccountExecutor
    calls=[]
    async def run():
        store,runtime=await setup(str(tmp_path/"state.db"))
        async def execute(account,action,operation_id,generation=None):
            calls.append((action,operation_id,generation))
            if action=="observe": return {"outcome":"provider_auth_failed"}
            return {"state":"done","outcome":"ok","binding":replace(account,generation=generation,
                home="/private-fixture/"+generation).public()}
        executor=AccountExecutor(runtime,None,execute=execute); runtime.executor=executor
        request={"action":"relogin","host":"host-a","provider":"codex","account":"primary",
            "login_generation":"login-one","operation_id":"original","_auth_context":AUTH}
        def interrupted_save(*args): raise RuntimeError("interrupted after activation")
        executor._save_recovery=interrupted_save
        try:
            with pytest.raises(RuntimeError,match="interrupted after activation"):
                await runtime.operation(request)
            assert (await runtime.run(lambda a:a._cell("host-a","codex","primary")))["generation"]!="login-one"
            runtime.executor=AccountExecutor(runtime,None,execute=execute)
            assert (await runtime.operation(dict(request,login_generation="active")))["result"]["outcome"]=="recovered"
            assert [c[0] for c in calls]==["observe","enroll","status"]
            assert calls[-1][1:]==calls[-2][1:]
            await runtime.operation(request)
            assert len(calls)==3
        finally: store.stop()
    asyncio.run(run())
