"""Addendum B state transitions through real account and host boundaries."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest
from provider_accounts import AccountError
from provider_cli_pin import digest, set_pin, check
from sessions import VerbError
from test_account_repin import host_journey
from test_account_runtime import AUTH


async def move_pin(runtime,pin,revision,*,old=None):
    target=copy.deepcopy(pin);target["revision"]=revision
    if old:
        target["providers"][old.provider]["version"]=old.version
        for artifact in target["providers"][old.provider]["artifacts"].values():
            artifact.update(native_sha256=old.executable_sha256,resource_sha256=old.executable_sha256)
    await runtime.operation({"action":"pin-set","pin":target,"expected_revision":revision-1,
        "operation_id":"table-pin-"+str(revision),"reason":"table fixture","_auth_context":AUTH})
    return target


def fresh(request,pin,name,**kw):
    return dict(request,operation_id=name,new_generation="generation-"+name,
        expected_pin_revision=pin["revision"],expected_pin_digest=digest(pin),**kw)


async def recovery(runtime,old):
    status=await runtime.operation({"action":"status","host":old.host,"provider":old.provider,
        "account":old.alias,"_auth_context":AUTH})
    return status["recovery"][0]


@pytest.mark.parametrize("code",["provider_cli_pin_mismatch","provider_cli_verification_unavailable","novel_authenticated_refusal"])
def test_b_n1_effect_proof_survives_pin_move(tmp_path,monkeypatch,code):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        import provider_cli_artifact
        verify=provider_cli_artifact.verify
        def refuse(*args,**kwargs):raise AccountError(code)
        monkeypatch.setattr(provider_cli_artifact,"verify",refuse)
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            monkeypatch.setattr(provider_cli_artifact,"verify",verify)
            pin3=await move_pin(runtime,pin,3)
            result=await runtime.operation(fresh(request,pin3,"after-refusal"))
            assert result["result"]["outcome"]=="repinned"
            assert host.registry.active(old.provider,old.alias,old.host).generation=="generation-after-refusal"
            assert len([v for v in native if "Reply exactly OK" in v])==1 and len(logins)==1
        finally:store.stop()
    asyncio.run(run())


def test_b_s1_absent_wrapper_one_latest_tail_request(tmp_path,monkeypatch):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        execute=runtime.executor.execute
        async def race(*a,**kw):
            result=await execute(*a,**kw)
            if a[1]=="repin":await move_pin(runtime,pin,3)
            return result
        runtime.executor.execute=race
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            pin3=copy.deepcopy(pin);pin3["revision"]=3
            async def offline(*a,**kw):
                if a[1].startswith("repin-reconcile"):raise AccountError("account_host_unreachable")
                return await execute(*a,**kw)
            runtime.executor.execute=offline
            wrapper=fresh(request,pin3,"offline-wrapper",reconcile_of=request["operation_id"])
            with pytest.raises(VerbError):await runtime.operation(wrapper)
            pin4=await move_pin(runtime,pin3,4)
            runtime.executor.execute=execute
            result=await runtime.operation(fresh(request,pin4,"latest",reconcile_of="offline-wrapper"))
            assert result["result"]["binding"]["generation"]=="successor"
            assert len([v for v in native if "Reply exactly OK" in v])==1 and len(logins)==1
            assert (await recovery(runtime,old))["fence_causes"]==[]
        finally:store.stop()
    asyncio.run(run())


def test_b_u1_quiescent_started_is_sealed_and_not_replayed(tmp_path,monkeypatch):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        execute=runtime.executor.execute
        async def lost(*a,**kw):
            if a[1]=="repin":
                from provider_cli_pin import digest
                op=hashlib.sha256(a[2].encode()).hexdigest()
                host._save_operation(op,{"kind":"repin","provider":old.provider,"alias":old.alias,
                    "expected_generation":old.generation,"generation":"successor","pin_revision":pin["revision"],
                    "pin_digest":digest(pin),"state":"started","outcome":"execution_unresolved"})
                raise AccountError("account_host_unreachable")
            return await execute(*a,**kw)
        runtime.executor.execute=lost
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            pin3=await move_pin(runtime,pin,3)
            runtime.executor.execute=execute
            await runtime.operation(fresh(request,pin3,"after-crash",reconcile_of=request["operation_id"]))
            before=list(native)
            with pytest.raises(AccountError):await execute(old,"repin","repin:"+request["operation_id"],"successor",pin=pin)
            assert native==before and len(logins)==1
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider,platform",[("codex","macos"),("codex","wsl"),("claude","macos"),("claude","wsl")])
def test_b_hatch_g1_fresh_home_supersedes_split_without_old_import(tmp_path,monkeypatch,provider,platform):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch,provider,platform)
        execute=runtime.executor.execute
        async def race(*a,**kw):
            result=await execute(*a,**kw)
            if a[1]=="repin":await move_pin(runtime,pin,3)
            return result
        runtime.executor.execute=race
        try:
            historical=await store.open_session(old.host,"historical",provider=provider,
                session_generation="old-session",account_binding=old.public())
            with pytest.raises(VerbError):await runtime.operation(request)
            host_only=host.registry.active(provider,old.alias,old.host)
            runtime.policy["watchers"]={"host-a:fixture-watcher":{"host":old.host}}
            await runtime.operation({"action":"observe-failure","host":old.host,"provider":provider,"account":old.alias,
                "login_generation":old.generation,"kind":"auth","_auth_context":{"service_authenticated":True,"service_actor":"host-a:fixture-watcher"}})
            runtime.executor.execute=execute
            status=await recovery(runtime,old)
            result=await runtime.operation({"action":"relogin","host":old.host,"provider":provider,"account":old.alias,
                "login_generation":old.generation,"operation_id":"fresh-enrollment","reason":"table fixture",
                "supersede_repin":status["snapshot_digest"],"_auth_context":AUTH})
            new=result["result"]["binding"]
            assert new["home"]!=old.home and new["generation"] not in (old.generation,host_only.generation)
            assert await runtime.run(lambda a:a.bound(host_only.key)) is None
            assert (await store.fetch_session(old.host,"historical"))["account_binding"]==historical["account_binding"]
            assert len(logins)==2 and (await recovery(runtime,old))["fence_causes"]==[]
            assert all(v["disposition"]=="superseded" for v in (await recovery(runtime,old))["dispositions"])
        finally:store.stop()
    asyncio.run(run())


def test_b_hatch_empty_set_refuses_without_bypassing_healthy_guard(tmp_path,monkeypatch):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        try:
            status=await recovery(runtime,old)
            with pytest.raises(VerbError,match="account_repin_supersession_empty"):
                await runtime.operation({"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                    "login_generation":old.generation,"operation_id":"empty-hatch","supersede_repin":status["snapshot_digest"],
                    "_auth_context":AUTH})
            assert not calls or set(calls)<={"recovery-status"}
            assert len(logins)==1
        finally:store.stop()
    asyncio.run(run())
