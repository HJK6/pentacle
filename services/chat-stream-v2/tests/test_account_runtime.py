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
            with pytest.raises(Exception,match="account_feature_off"): await choose({})
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
