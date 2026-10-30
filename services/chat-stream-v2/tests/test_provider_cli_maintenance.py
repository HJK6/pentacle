"""Home-scoped maintenance must stop input, including after process restart."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

import local_admin
from account_runtime import AccountRuntime
from provider_accounts import AccountError
from test_account_rotation import binding, observation, NOW
from test_loopback_privileged_rpc import scratch_daemon, rpc


@asynccontextmanager
async def ready_daemon(tmp_path,monkeypatch,*,serving_seat=False):
    from test_provider_cli_initial_activation import ready_fixture
    from server import Server
    from spawnctl import SpawnCtl
    store,runtime,host,selected,target,row,native=await ready_fixture(tmp_path,monkeypatch,serving_seat=serving_seat)
    token_path=tmp_path/"admin.token"
    local_admin.initialize(token_path)
    monkeypatch.setattr(local_admin.verify,"__defaults__",(token_path,))
    ctl=SpawnCtl(store,runtime.sessions)
    daemon=Server(port=0,store=store,sessions=runtime.sessions,spawnctl=ctl,
        local_host=selected.host,seat_operator_authority=False)
    runtime.attach(daemon,ctl,SimpleNamespace())
    await daemon.bind()
    request={"host":selected.host,"reason":"scoped maintenance","ttl_s":60,
        "input_provider":selected.provider,"input_account":selected.alias,
        "expected_login_generation":selected.generation,"maintenance_id":"fixture-slot",
        "expected_hold_digest":"absent","local_admin_token":local_admin.read(token_path)}
    try:yield daemon,store,runtime,host,selected,row,request
    finally:
        await daemon.close()
        store.stop()


def clear_request(request,result,**changes):
    return {"host":request["host"],"local_admin_token":request["local_admin_token"],
        "maintenance_id":request["maintenance_id"],"expected_maintenance_digest":result["maintenance_digest"],
        "expected_hold_digest":result["hold_digest"],**changes}


@pytest.mark.parametrize("case",["normal","restart","expired","foreign","slot-stale","hold-stale","host-offline","pending"])
def test_scoped_clear_requires_exact_slot_hold_and_authenticated_current_proof(tmp_path,monkeypatch,case):
    async def run():
        async with ready_daemon(tmp_path,monkeypatch) as (daemon,store,runtime,host,selected,row,request):
            result=await rpc(daemon,"spawn_freeze",**request)
            assert result["type"]=="spawn_freeze.ok"
            assert (await rpc(daemon,"spawn_freeze",**request))["receipt"]==result["receipt"]
            conflict=await rpc(daemon,"spawn_freeze",**dict(request,reason="changed"))
            assert conflict["error_code"]=="provider_cli_maintenance_id_conflict"
            clear=clear_request(request,result)
            if case=="restart":
                replacement=AccountRuntime(store,runtime.sessions,policy=runtime.policy,clock=runtime.clock)
                replacement.executor=runtime.executor
                runtime.executor.runtime=replacement
                replacement.attach(daemon,runtime.spawnctl,SimpleNamespace())
                runtime=replacement
            if case in {"expired","foreign"}:
                runtime.clock=lambda:NOW+120
                await store.clear_spawn_admission_hold(selected.host)
                clear["expected_hold_digest"]="absent"
            if case=="foreign":
                await store.set_spawn_admission_hold(selected.host,held_until=NOW+1000,reason="foreign-owner")
                from provider_cli_maintenance import status
                foreign=await runtime.run(lambda a:status(a,selected.host))
                clear["expected_hold_digest"]=foreign["hold_digest"]
            if case=="slot-stale":clear["expected_maintenance_digest"]="f"*64
            if case=="hold-stale":clear["expected_hold_digest"]="f"*64
            if case=="host-offline":
                async def offline(*args,**kwargs):raise AccountError("account_host_unreachable")
                runtime.executor.execute=offline
            if case=="pending":
                await store.reserve_stream_id(selected.host,"late-dispatch",ttl_s=300,refuse_if_held=False)
            response=await rpc(daemon,"spawn_unfreeze",**clear)
            from provider_cli_maintenance import slot
            if case in {"normal","restart","expired","foreign"}:
                assert response["type"]=="spawn_freeze.ok"
                assert response["receipt"]["state"]=="cleared"
                assert await runtime.run(lambda a:slot(a,selected.host)) is None
                if case=="foreign":
                    assert (await store.get_spawn_admission_hold(selected.host))["reason"]=="foreign-owner"
                    assert response["receipt"]["clear_proof"]["foreign_hold_preserved"]
                assert (await rpc(daemon,"spawn_unfreeze",**clear))["receipt"]==response["receipt"]
                # An old freeze ID is terminal, never a rearm after clear.
                assert (await rpc(daemon,"spawn_freeze",**request))["receipt"]["state"]=="cleared"
                assert await runtime.run(lambda a:slot(a,selected.host)) is None
            else:
                assert response["type"]=="spawn_unfreeze.error"
                assert await runtime.run(lambda a:slot(a,selected.host)) is not None
                if case=="foreign":assert (await store.get_spawn_admission_hold(selected.host))["reason"]=="foreign-owner"
    asyncio.run(run())


def test_home_writer_orders_freeze_after_paste_and_before_any_following_enter(tmp_path,monkeypatch):
    async def run():
        async with ready_daemon(tmp_path,monkeypatch,serving_seat=True) as (daemon,store,runtime,host,selected,row,request):
            pasted=asyncio.Event();finish=asyncio.Event();actions=[]
            async def paste():
                async with runtime.input_guard(row):
                    actions.append("paste");pasted.set();await finish.wait()
            physical=asyncio.create_task(paste());await pasted.wait()
            freezing=asyncio.create_task(rpc(daemon,"spawn_freeze",**request))
            for _ in range(10):await asyncio.sleep(0)
            assert not freezing.done()
            # An unrelated home has a distinct barrier and stays available.
            other=replace(selected,alias="other",home=selected.home+"-other",generation="other")
            await runtime.run(lambda a:a.register(other))
            await runtime.run(lambda a:a.observe(other.key,observation(other),now=NOW))
            unrelated=await store.open_session(other.host,"other",provider=other.provider,account_binding=other.public())
            async with runtime.input_guard(unrelated):actions.append("other-home")
            finish.set();await physical
            frozen=await freezing
            assert frozen["type"]=="spawn_freeze.ok"
            with pytest.raises(AccountError,match="provider_cli_input_maintenance"):
                async with runtime.input_guard(row):actions.append("enter")
            assert actions==["paste","other-home"]
            assert not runtime.pin_lock.locked()
    asyncio.run(run())


@pytest.mark.parametrize("verb",["tell","send","prepare"])
@pytest.mark.parametrize("closed",[False,True])
def test_input_ingress_refuses_before_acceptance_even_for_temporarily_closed_row(tmp_path,monkeypatch,verb,closed):
    from comms import Comms
    from sessions import VerbError
    from test_send_semantics import FakeTmux
    async def run():
        async with ready_daemon(tmp_path,monkeypatch,serving_seat=True) as (daemon,store,runtime,host,selected,row,request):
            tmux=FakeTmux()
            comms=Comms(store,runtime.sessions,runtime.spawnctl)
            runtime.spawnctl.tmux=tmux
            runtime.attach(daemon,runtime.spawnctl,comms)
            assert (await rpc(daemon,"spawn_freeze",**request))["type"]=="spawn_freeze.ok"
            if closed:await store.update_session(selected.host,"current-ready",status="closed")
            message={"stream_id":row["stream_id"],"text":"sender retains this body","request_id":"refused-request","tell_id":"refused-tell"}
            method={"tell":comms.tell,"send":comms.send,"prepare":comms._prepare_send_plan}[verb]
            with pytest.raises(VerbError) as refused:await method(message)
            assert refused.value.code=="provider_cli_input_maintenance"
            assert refused.value.extra["delivery"]=="not_landed" and refused.value.extra["attempts"]==0
            assert not tmux.pastes and not tmux.enters
            assert await store.get_tell_delivery("refused-tell") is None
            receipts=await store.get_send_receipt(row["stream_id"],"refused-request")
            assert receipts is None or receipts["state"]=="not_landed"
    asyncio.run(run())


def test_tell_freeze_between_paste_and_enter_retains_receipt_and_never_repastes(tmp_path,monkeypatch):
    from comms import Comms
    from test_tell_immediate_delivery import ScriptedCodex
    async def run():
        async with ready_daemon(tmp_path,monkeypatch,serving_seat=True) as (daemon,store,runtime,host,selected,row,request):
            tmux=ScriptedCodex(submit_on_retry=False)
            runtime.spawnctl.tmux=tmux
            comms=Comms(store,runtime.sessions,runtime.spawnctl)
            runtime.attach(daemon,runtime.spawnctl,comms)
            frozen=None
            async def evidence(*args,**kwargs):
                nonlocal frozen
                frozen=await rpc(daemon,"spawn_freeze",**request)
                assert frozen["type"]=="spawn_freeze.ok"
                return False
            comms._submission_evidence=evidence
            message={"stream_id":row["stream_id"],"text":"one physical paste","tell_id":"paste-once"}
            result=await comms.tell(message)
            assert result["delivery_status"]=="pasted_unsubmitted" and not result["submission_confirmed"]
            assert len(tmux.pastes)==1 and tmux.enter_only==0
            assert (await store.get_tell_delivery("paste-once"))["reply"]==result
            assert (await rpc(daemon,"spawn_unfreeze",**clear_request(request,frozen)))["type"]=="spawn_freeze.ok"
            replay=await comms.tell(message)
            assert replay["duplicate"] and len(tmux.pastes)==1 and tmux.enter_only==0
    asyncio.run(run())


@pytest.mark.parametrize("case",["anonymous","missing-field","foreign-home","stale-generation","foreign-hold","incomplete-clear"])
def test_scope_and_authority_refusals_do_not_overwrite_a_hold_or_create_a_slot(tmp_path,monkeypatch,case):
    from provider_cli_maintenance import slot
    async def run():
        async with ready_daemon(tmp_path,monkeypatch) as (daemon,store,runtime,host,selected,row,request):
            if case=="anonymous":request.pop("local_admin_token")
            elif case=="missing-field":request.pop("input_account")
            elif case=="foreign-home":request["home"]="/caller-supplied/home"
            elif case=="stale-generation":request["expected_login_generation"]="stale"
            if case in {"foreign-hold","incomplete-clear"}:
                await store.set_spawn_admission_hold(selected.host,held_until=NOW+1000,reason="foreign")
            before=await store.get_spawn_admission_hold(selected.host)
            if case=="incomplete-clear":
                result=await rpc(daemon,"spawn_unfreeze",host=selected.host,
                    expected_hold_digest="absent",local_admin_token=request["local_admin_token"])
                assert result["type"]=="spawn_unfreeze.error"
            else:
                result=await rpc(daemon,"spawn_freeze",**request)
                assert result["type"]=="spawn_freeze.error"
            assert await runtime.run(lambda a:slot(a,selected.host)) is None
            assert await store.get_spawn_admission_hold(selected.host)==before
    asyncio.run(run())


@pytest.mark.parametrize("race",["origin","slot","hold","pending","proof-expiry"])
def test_clear_rechecks_slot_origin_and_hold_after_remote_readback(tmp_path,monkeypatch,race):
    from provider_cli_maintenance import PREFIX,slot
    import json
    async def run():
        async with ready_daemon(tmp_path,monkeypatch) as (daemon,store,runtime,host,selected,row,request):
            frozen=await rpc(daemon,"spawn_freeze",**request)
            verify=runtime.executor.verify_launchers
            async def raced(account):
                proof=await verify(account)
                if race=="origin":await runtime.record_failure(selected,"network")
                elif race=="hold":await store.set_spawn_admission_hold(selected.host,held_until=NOW+2000,reason="new-owner")
                elif race=="pending":await store.reserve_stream_id(selected.host,"late",ttl_s=600,refuse_if_held=False)
                elif race=="slot":
                    def changed(a):
                        current=slot(a,selected.host);current["owner"]={"stream_id":"different-owner"}
                        a.conn.execute("UPDATE kv SET v=? WHERE k=?",(json.dumps(current),PREFIX+selected.host));a.conn.commit()
                    await runtime.run(changed)
                elif race=="proof-expiry":runtime.clock=lambda:NOW+121
                return proof
            runtime.executor.verify_launchers=raced
            result=await rpc(daemon,"spawn_unfreeze",**clear_request(request,frozen))
            assert result["type"]=="spawn_unfreeze.error"
            assert await runtime.run(lambda a:slot(a,selected.host)) is not None
            if race=="hold":assert (await store.get_spawn_admission_hold(selected.host))["reason"]=="new-owner"
    asyncio.run(run())


def test_expired_slot_can_acquire_a_fresh_real_hold_for_unresolved_B_recovery(tmp_path,monkeypatch):
    from test_account_runtime import AUTH
    from provider_cli_fixtures import release_pair
    from provider_cli_artifact import resource_digest
    from provider_cli_pin import digest
    from sessions import VerbError
    async def run():
        async with ready_daemon(tmp_path,monkeypatch) as (daemon,store,runtime,host,selected,row,request):
            await store.open_session(selected.host,"front-desk",provider="claude",session_generation=AUTH["session_generation"])
            daemon.assistant_composite=SimpleNamespace(enabled=True,
                env_config=SimpleNamespace(direct_primary=True,name="front-desk"),
                _env_binding=lambda:{"stream_id":AUTH["stream_id"],"generation":AUTH["session_generation"]})
            frozen=await rpc(daemon,"spawn_freeze",**request)
            target=release_pair()
            for entry in target["providers"][selected.provider]["artifacts"].values():
                entry.update(native_sha256=selected.executable_sha256,resource_sha256=resource_digest(selected.executable))
            repin={"action":"repin","host":selected.host,"provider":selected.provider,"account":selected.alias,
                "login_generation":selected.generation,"new_generation":"unused-recovery","operation_id":"lost-no-op-proof",
                "expected_pin_revision":0,"expected_pin_digest":digest(target),"target_pin":target,"reason":"fixture","_auth_context":AUTH}
            execute=runtime.executor.execute;offline=False
            async def lost(account,action,*args,**kwargs):
                nonlocal offline
                if offline:raise AccountError("account_host_unreachable")
                result=await execute(account,action,*args,**kwargs)
                if action=="repin":offline=True;raise AccountError("account_host_unreachable")
                return result
            runtime.executor.execute=lost
            with pytest.raises(VerbError):await runtime.operation(repin)
            runtime.executor.execute=execute;runtime.clock=lambda:NOW+61
            await store.clear_spawn_admission_hold(selected.host)  # The ordinary expiry sweep.
            with pytest.raises(VerbError):await runtime.operation(repin)
            renewed=await rpc(daemon,"spawn_freeze",host=selected.host,reason="fresh actual B recovery hold",ttl_s=600,
                local_admin_token=request["local_admin_token"])
            assert renewed["type"]=="spawn_freeze.ok"
            assert renewed["hold"]["reason"]=="fresh actual B recovery hold"
            assert (await runtime.operation(repin))["result"]["outcome"]=="no_op"
            clear=clear_request(request,frozen,expected_hold_digest=renewed["hold_digest"])
            cleared=await rpc(daemon,"spawn_unfreeze",**clear)
            assert cleared["type"]=="spawn_freeze.ok"
            # Clearing the old slot never clears this newer actual hold.
            assert (await store.get_spawn_admission_hold(selected.host))["reason"]=="fresh actual B recovery hold"
            assert (await rpc(daemon,"spawn_unfreeze",host=selected.host,local_admin_token=request["local_admin_token"]))["type"]=="spawn_freeze.ok"
            assert await store.get_spawn_admission_hold(selected.host) is None
    asyncio.run(run())


def test_ui_interrupt_cannot_send_escape_through_a_scoped_input_fence(tmp_path,monkeypatch):
    from uiverbs import UIVerbs
    from sessions import VerbError
    from test_send_semantics import FakeTmux
    class Terminal(FakeTmux):
        async def has_session(self,name):return True
    async def run():
        async with ready_daemon(tmp_path,monkeypatch,serving_seat=True) as (daemon,store,runtime,host,selected,row,request):
            tmux=Terminal();runtime.spawnctl.tmux=tmux
            ui=UIVerbs(store,runtime.sessions,runtime.spawnctl)
            assert (await rpc(daemon,"spawn_freeze",**request))["type"]=="spawn_freeze.ok"
            with pytest.raises(VerbError,match="provider_cli_input_maintenance"):
                await ui.send_interrupt({"host":selected.host,"session_name":"current-ready",
                    "expected_session_generation":row["session_generation"],"request_id":"ui-escape"})
            assert tmux.escapes==0
    asyncio.run(run())


def test_authenticated_freeze_stops_only_derived_home_and_survives_expiry(tmp_path, monkeypatch):
    async def run():
        async with scratch_daemon(tmp_path, monkeypatch) as (daemon, store, ctl, token):
            runtime=AccountRuntime(store,daemon.sessions,policy={"enabled":True},clock=lambda:NOW)
            runtime.attach(daemon,ctl,SimpleNamespace())
            selected=binding(host="fixture-host")
            other=replace(selected,alias="other",home=selected.home+"-other",generation="other-generation")
            for account in (selected,other):
                await runtime.run(lambda a,account=account:a.register(account))
                await runtime.run(lambda a,account=account:a.observe(account.key,observation(account),now=NOW))
            row=await store.open_session(selected.host,"affected",provider=selected.provider,
                session_generation="affected-generation",account_binding=selected.public())
            unrelated=await store.open_session(other.host,"unrelated",provider=other.provider,
                session_generation="other-seat-generation",account_binding=other.public())
            from provider_cli_fixtures import seed_pin
            from test_provider_cli_pin import artifact_executor
            await seed_pin(runtime)
            await artifact_executor(runtime)
            await runtime.before_input(row)
            await runtime.before_input(unrelated)
            request={"host":selected.host,"reason":"scoped maintenance","ttl_s":60,
                "input_provider":selected.provider,"input_account":selected.alias,
                "expected_login_generation":selected.generation,"maintenance_id":"fixture-slot",
                "expected_hold_digest":"absent","local_admin_token":local_admin.read(token)}
            reply=await rpc(daemon,"spawn_freeze",**request)
            assert reply["type"]=="spawn_freeze.ok"
            with pytest.raises(AccountError,match="provider_cli_input_maintenance"):
                await runtime.before_input(row)
            await runtime.before_input(unrelated)
            # Expired/swept spawn hold must not forget the durable input fence.
            await store.clear_spawn_admission_hold(selected.host)
            restarted=AccountRuntime(store,daemon.sessions,policy=runtime.policy,clock=lambda:NOW+120)
            with pytest.raises(AccountError,match="provider_cli_input_maintenance"):
                await restarted.before_input(row)
            refused=await rpc(daemon,"spawn_unfreeze",host=selected.host,local_admin_token=local_admin.read(token))
            assert refused["error_code"]=="provider_cli_maintenance_compare_required"
    asyncio.run(run())
