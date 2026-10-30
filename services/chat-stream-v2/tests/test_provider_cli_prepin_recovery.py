"""The immutable target in a pre-enforcement repin is its recovery anchor."""
import asyncio
import copy

import pytest

from provider_accounts import AccountError
from provider_cli_pin import digest
from sessions import VerbError
from test_account_repin import host_journey
from test_account_runtime import AUTH
from test_account_rotation import NOW


async def journey(tmp_path,monkeypatch,provider,platform):
    result=await host_journey(tmp_path,monkeypatch,provider,platform)
    store,runtime,host,old,request,calls,native,logins,cell,pin=result
    def never_initialized(a):
        a.conn.execute("DELETE FROM kv WHERE k IN (?,?)",("provider_cli.release_pin","provider_cli.pin_initialized"))
        prefix="provider-cli-pin:"
        a.conn.execute("DELETE FROM v2_account_recovery WHERE substr(binding_key,1,?)=?",(len(prefix),prefix))
        a.conn.commit()
    await runtime.run(never_initialized)
    target=copy.deepcopy(pin);target["revision"]=1
    request.update(expected_pin_revision=0,expected_pin_digest=digest(target),target_pin=target)
    return result


@pytest.mark.parametrize("provider,platform",[(p,s) for p in ("codex","claude") for s in ("macos","wsl")])
@pytest.mark.parametrize("boundary",["same-attempt","reconcile","restart","expired-hold","changed-target"])
def test_host_success_central_stale_recovery_uses_immutable_target(tmp_path,monkeypatch,provider,platform,boundary):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await journey(tmp_path,monkeypatch,provider,platform)
        execute=runtime.executor.execute;offline=False
        async def split(account,action,*args,**kwargs):
            nonlocal offline
            if offline:raise AccountError("account_host_unreachable")
            result=await execute(account,action,*args,**kwargs)
            if action=="repin":
                offline=True
                raise AccountError("account_host_unreachable")
            return result
        runtime.executor.execute=split
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            assert host.registry.active(provider,old.alias,old.host).generation=="successor"
            assert (await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)))["generation"]==old.generation
            assert (await runtime.operation({"action":"pin-status","_auth_context":AUTH}))["pin"] is None
            runtime.executor.execute=execute
            if boundary=="restart":
                from account_runtime import AccountRuntime
                renewed=AccountRuntime(store,runtime.sessions,policy=runtime.policy,clock=runtime.clock)
                renewed.attach(runtime.server,runtime.spawnctl,runtime.comms)
                renewed.executor=runtime.executor;renewed.executor.runtime=renewed
                runtime=renewed
                await runtime.start()
            elif boundary=="expired-hold":
                runtime.clock=lambda:NOW+1001
                with pytest.raises(VerbError):await runtime.operation(request)
                await store.set_spawn_admission_hold(old.host,held_until=NOW+2000,reason="fresh real recovery hold")
            recovered=dict(request)
            if boundary in {"reconcile","changed-target"}:
                recovered.update(operation_id="new-canonical-tail",reconcile_of=request["operation_id"],new_generation="unused-wrapper")
            if boundary=="changed-target":
                # A different immutable pair requires fresh verification. An
                # unapproved digest cannot relabel this historical success.
                bad=copy.deepcopy(recovered);bad["target_pin"]["providers"][provider]["version"]="9.9.9"
                with pytest.raises(VerbError):await runtime.operation(bad)
            result=(await runtime.operation(recovered))["result"]
            assert result["outcome"]=="repinned" and result["binding"]["generation"]=="successor"
            assert result["binding"]["home"]==old.home
            assert (await runtime.operation(recovered))["result"]==result
            assert len([v for v in native if "Reply exactly OK" in v])==1 and len(logins)==1
            assert (await runtime.operation({"action":"pin-status","_auth_context":AUTH}))["pin"] is None
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider,platform",[(p,s) for p in ("codex","claude") for s in ("macos","wsl")])
def test_pre_pin_confirmed_non_split_failure_does_not_become_a_permanent_fence(tmp_path,monkeypatch,provider,platform):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await journey(tmp_path,monkeypatch,provider,platform)
        observe=host.observation
        def network(_):raise AccountError("provider_transport_failed")
        host.observation=network
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            assert host.registry.active(provider,old.alias,old.host).key==old.key
            host.observation=observe
            fresh=dict(request,operation_id="fresh-after-pre-pin-failure",new_generation="fresh-successor")
            result=(await runtime.operation(fresh))["result"]
            assert result["binding"]["generation"]=="fresh-successor"
            assert len([v for v in native if "Reply exactly OK" in v])==1 and len(logins)==1
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider,platform",[(p,s) for p in ("codex","claude") for s in ("macos","wsl")])
def test_pre_pin_noop_preserves_generation_and_exact_operation_arguments(tmp_path,monkeypatch,provider,platform):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await journey(tmp_path,monkeypatch,provider,platform)
        try:
            first=(await runtime.operation(request))["result"]
            noop=dict(request,login_generation=first["binding"]["generation"],operation_id="pre-pin-noop",new_generation="unused-noop")
            result=(await runtime.operation(noop))["result"]
            assert result["outcome"]=="no_op" and result["binding"]["generation"]==first["binding"]["generation"]
            assert (await runtime.operation(noop))["result"]==result
            assert not any(b.generation=="unused-noop" for b in host.registry.bindings())
            changed=copy.deepcopy(noop);changed["target_pin"]["providers"][provider]["version"]="9.9.9"
            changed["expected_pin_digest"]=digest(changed["target_pin"])
            with pytest.raises(VerbError,match="account_repin_id_conflict"):await runtime.operation(changed)
            assert len([v for v in native if "Reply exactly OK" in v])==1 and len(logins)==1
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider,platform",[(p,s) for p in ("codex","claude") for s in ("macos","wsl")])
@pytest.mark.parametrize("refusal",["auth","hold","clearance","policy","digest","partial-target"])
def test_pre_pin_refusals_leave_native_and_coordinator_selection_unchanged(tmp_path,monkeypatch,provider,platform,refusal):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await journey(tmp_path,monkeypatch,provider,platform)
        try:
            if refusal=="auth":request["_auth_context"]={"token_verified":True,"role":"front-desk"}
            elif refusal=="hold":await store.clear_spawn_admission_hold(old.host)
            elif refusal=="clearance":host.config.pop("mutation_clearance")
            elif refusal=="policy":runtime.policy["enabled"]=False
            elif refusal=="digest":request["expected_pin_digest"]="f"*64
            elif refusal=="partial-target":request["target_pin"].pop("providers")
            with pytest.raises(VerbError):await runtime.operation(request)
            assert host.registry.active(provider,old.alias,old.host).key==old.key
            assert (await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)))["generation"]==old.generation
            assert len(logins)==1 and not [v for v in native if "Reply exactly OK" in v]
        finally:store.stop()
    asyncio.run(run())
