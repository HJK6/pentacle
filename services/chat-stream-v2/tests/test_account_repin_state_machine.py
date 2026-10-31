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
    assert isinstance(status.get("recovery"),list) and len(status["recovery"])==1,"FD status must expose the recovery snapshot"
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


def test_b_z_late_dispatch_sealed_even_while_original_generation_is_still_active(tmp_path,monkeypatch):
    from types import SimpleNamespace
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        execute=runtime.executor.execute;run_native=host.run
        def busy(argv,**kw):
            if argv[0]=="ps":return SimpleNamespace(returncode=0,stdout="123 codex /fixture/native")
            return run_native(argv,**kw)
        host.run=busy
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            host.run=run_native
            await move_pin(runtime,pin,3,old=old)
            assert await runtime.run(lambda a:check(a,old))
            # The obsolete prepared native bytes still exist. Delayed dispatch
            # must refuse by its durable seal, even though expected G0 matches.
            with pytest.raises(AccountError,match="account_operation_sealed"):
                await execute(old,"repin","repin:"+request["operation_id"],"successor",pin=pin)
            assert host.registry.active(old.provider,old.alias,old.host).key==old.key
            assert not [v for v in native if "Reply exactly OK" in v]
        finally:store.stop()
    asyncio.run(run())


def test_b_s1_mixed_success_then_absent_wrapper_folds_actual_links_only(tmp_path,monkeypatch):
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
            # A different current target produces a second real host link.
            pin3=copy.deepcopy(pin);pin3["revision"]=3
            pin4=copy.deepcopy(pin);pin4["revision"]=4
            pin4["providers"][old.provider]["version"]="9.9.9"
            await runtime.operation({"action":"pin-set","pin":pin4,"expected_revision":3,
                "operation_id":"changed-current","reason":"table","_auth_context":AUTH})
            cell["version"]="9.9.9"
            async def second_race(*a,**kw):
                result=await execute(*a,**kw)
                if a[1]=="repin-reconcile":await move_pin(runtime,pin4,5)
                return result
            runtime.executor.execute=second_race
            with pytest.raises(VerbError):await runtime.operation(fresh(request,pin4,"second-link",reconcile_of=request["operation_id"]))
            actual=host.registry.active(old.provider,old.alias,old.host)
            assert actual.generation=="generation-second-link"
            pin5=copy.deepcopy(pin4);pin5["revision"]=5
            async def offline(*a,**kw):
                if a[1].startswith("repin-reconcile"):raise AccountError("account_host_unreachable")
                return await execute(*a,**kw)
            runtime.executor.execute=offline
            with pytest.raises(VerbError):await runtime.operation(fresh(request,pin5,"empty-link",reconcile_of="second-link"))
            pin6=await move_pin(runtime,pin5,6)
            runtime.executor.execute=execute
            result=await runtime.operation(fresh(request,pin6,"fold-mixed",reconcile_of="empty-link"))
            assert result["result"]["binding"]==actual.public()
            assert len([v for v in native if "Reply exactly OK" in v])==2 and len(logins)==1
            assert not (await recovery(runtime,old))["fence_causes"]
        finally:store.stop()
    asyncio.run(run())


def _run_b_table(row,tmp_path,monkeypatch,subtests):
    from repin_state_matrix import cases,run
    for index,case in enumerate(cases(row)):
        case_root=tmp_path/str(index);case_root.mkdir()
        with subtests.test(msg=case.id),monkeypatch.context() as isolated:
            asyncio.run(run(case,case_root,isolated))


@pytest.mark.timeout(180)
def test_b_table_n1_verifier(tmp_path,monkeypatch,subtests):
    _run_b_table('B-N1-verifier',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_n1_unknown(tmp_path,monkeypatch,subtests):
    _run_b_table('B-N1-unknown',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_n2_before(tmp_path,monkeypatch,subtests):
    _run_b_table('B-N2-before',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_n2_turn(tmp_path,monkeypatch,subtests):
    _run_b_table('B-N2-turn',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_n2_after(tmp_path,monkeypatch,subtests):
    _run_b_table('B-N2-after',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_n2_artifact(tmp_path,monkeypatch,subtests):
    _run_b_table('B-N2-artifact',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_n2_registry(tmp_path,monkeypatch,subtests):
    _run_b_table('B-N2-registry',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_u0_absent(tmp_path,monkeypatch,subtests):
    _run_b_table('B-U0-absent',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_u0_success(tmp_path,monkeypatch,subtests):
    _run_b_table('B-U0-success',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_u1_dead(tmp_path,monkeypatch,subtests):
    _run_b_table('B-U1-dead',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_z_late(tmp_path,monkeypatch,subtests):
    _run_b_table('B-Z-late',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_n3(tmp_path,monkeypatch,subtests):
    _run_b_table('B-N3',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_s0(tmp_path,monkeypatch,subtests):
    _run_b_table('B-S0',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_s1_absent(tmp_path,monkeypatch,subtests):
    _run_b_table('B-S1-absent',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_s1_failed(tmp_path,monkeypatch,subtests):
    _run_b_table('B-S1-failed',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_c0(tmp_path,monkeypatch,subtests):
    _run_b_table('B-C0',tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_hatch_g1(tmp_path,monkeypatch,subtests):
    _run_b_table('B-hatch-G1',tmp_path,monkeypatch,subtests)




@pytest.mark.parametrize("boundary",["pin","lost-enroll","lost-target"])
def test_b_hatch_retains_fresh_home_across_pin_and_reply_loss(tmp_path,monkeypatch,boundary):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        execute=runtime.executor.execute
        async def split(*a,**kw):
            result=await execute(*a,**kw)
            if a[1]=="repin":await move_pin(runtime,pin,3)
            return result
        runtime.executor.execute=split
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            current=copy.deepcopy(pin);current["revision"]=3
            runtime.executor.execute=execute
            status=await recovery(runtime,old)
            hatch={"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"operation_id":"hatch-root","supersede_repin":status["snapshot_digest"],"_auth_context":AUTH}
            async def raced(*a,**kw):
                result=await execute(*a,**kw)
                if a[1]=="enroll":
                    await move_pin(runtime,current,4)
                    if boundary=="lost-enroll":raise AccountError("account_host_unreachable")
                return result
            runtime.executor.execute=raced
            with pytest.raises(VerbError):await runtime.operation(hatch)
            fresh_home=host.registry.active(old.provider,old.alias,old.host).home
            runtime.executor.execute=execute
            current["revision"]=4
            # Change target on the retained new home; never repeat sign-in.
            changed=copy.deepcopy(current);changed["revision"]=5;changed["providers"][old.provider]["version"]="9.9.9"
            cell["version"]="9.9.9"
            await runtime.operation({"action":"pin-set","pin":changed,"expected_revision":4,"operation_id":"hatch-target","reason":"table","_auth_context":AUTH})
            if boundary=="lost-target":
                async def lost(*a,**kw):
                    result=await execute(*a,**kw)
                    if a[1]=="repin-reconcile":raise AccountError("account_host_unreachable")
                    return result
                runtime.executor.execute=lost
                status=await recovery(runtime,old)
                with pytest.raises(VerbError):await runtime.operation(dict(hatch,operation_id="hatch-lost-target",supersede_repin=status["snapshot_digest"]))
                runtime.executor.execute=execute
            status=await recovery(runtime,old)
            result=await runtime.operation(dict(hatch,operation_id="hatch-finish",supersede_repin=status["snapshot_digest"]))
            new=result["result"]["binding"]
            assert new["home"]==fresh_home and new["home"]!=old.home and new["version"]=="9.9.9"
            assert len(logins)==2 and not (await recovery(runtime,old))["fence_causes"]
            assert await runtime.run(lambda a:a.bound(old.key))==old
        finally:store.stop()
    asyncio.run(run())


def test_b_origin_reanchor_uses_real_l_aware_health_observation(tmp_path,monkeypatch):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        execute=runtime.executor.execute
        async def split(*a,**kw):
            result=await execute(*a,**kw)
            if a[1]=="repin":await move_pin(runtime,pin,3)
            return result
        runtime.executor.execute=split
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            runtime.executor.execute=execute
            actual=host.registry.active(old.provider,old.alias,old.host)
            await runtime.record_failure(old,"identity")
            revision=await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["health_revision"])
            result=await runtime.operation({"action":"revalidate-health","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"expected_revision":revision,"operation_id":"split-health","reason":"identity repaired","_auth_context":AUTH})
            assert result["result"]["outcome"]=="health_revalidated"
            data=await runtime.run(lambda a:json.loads(a.conn.execute("SELECT data FROM v2_account_health_revalidations WHERE operation_id='split-health'").fetchone()[0]))
            assert data["lineage_provenance"]["host_binding"]==actual.public()
            assert data["lineage_provenance"]["links"] and data["binding_digest"]!=digest(data["lineage_provenance"]["host_binding"])
            pin3=copy.deepcopy(pin);pin3["revision"]=3
            with pytest.raises(VerbError):await runtime.operation(fresh(request,pin3,"implicit",reconcile_of=request["operation_id"]))
            status=await recovery(runtime,old)
            result=await runtime.operation(fresh(request,pin3,"explicit",reconcile_of=status["canonical_tail"],expected_origin_digest=status["origin_digest"]))
            assert result["result"]["binding"]==actual.public() and len(logins)==1
            assert len([v for v in native if "Reply exactly OK" in v])==1
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("action",["relogin","upgrade"])
def test_b_generation_exclusion_before_host_action(tmp_path,monkeypatch,action):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        execute=runtime.executor.execute
        from repin_state_matrix import outage_after_admission
        runtime.executor.execute=outage_after_admission(execute)
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            runtime.executor.execute=execute;before=list(calls)
            payload={"action":action,"host":old.host,"provider":old.provider,"account":old.alias,"login_generation":old.generation,
                "operation_id":"excluded-generation","_auth_context":AUTH}
            if action=="upgrade":payload.update(new_generation="excluded",expected_version=cell["version"],expected_executable_sha256=cell["executable_sha256"],reason="exclusion")
            with pytest.raises(VerbError,match="account_repin_reconciliation_required"):await runtime.operation(payload)
            assert calls==before and len(logins)==1
        finally:store.stop()
    asyncio.run(run())


def test_b_prepared_enrollment_blocks_repin_and_requires_host_clearance(tmp_path,monkeypatch):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        try:
            payload={"action":"upgrade","host":old.host,"provider":old.provider,"account":old.alias,"login_generation":old.generation,
                "new_generation":"operator-new","operation_id":"operator-enroll","expected_version":cell["version"],
                "expected_executable_sha256":cell["executable_sha256"],"reason":"operator upgrade","_auth_context":AUTH}
            prepared=(await runtime.operation(dict(payload,prepare_enrollment=True)))["result"]
            assert prepared["outcome"]=="enrollment_prepared"
            with pytest.raises(VerbError,match="account_mutation_unresolved"):await runtime.operation(request)
            execute=runtime.executor.execute
            with pytest.raises(AccountError,match="account_mutation_clearance_required"):
                await execute(old,"enroll",payload["operation_id"],"operator-new",pin=pin)
            await execute(old,"enroll",payload["operation_id"],"operator-new",pin=pin,reconcile={"_clearance":prepared["clearance"]})
            imported=(await runtime.operation(payload))["result"]["binding"]
            assert imported["generation"]=="operator-new" and imported["home"]!=old.home
            fresh_request=dict(fresh(request,pin,"after-generation"),login_generation="operator-new")
            assert (await runtime.operation(fresh_request))["result"]["outcome"]=="no_op"
            assert len(logins)==2
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("damage",["started-journal","invalid-retained-proof","central-proof-recoverable"])
def test_b_hatch_unrecoverable_new_proof_allows_quiescent_second_hatch(tmp_path,monkeypatch,damage):
    from test_account_rotation import NOW
    from account_repin_hatch import _key
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        execute=runtime.executor.execute
        async def split(*a,**kw):
            result=await execute(*a,**kw)
            if a[1]=="repin":await move_pin(runtime,pin,3)
            return result
        runtime.executor.execute=split
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            current=copy.deepcopy(pin);current["revision"]=3
            runtime.executor.execute=execute;status=await recovery(runtime,old)
            hatch={"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"operation_id":"abandon-hatch","supersede_repin":status["snapshot_digest"],"_auth_context":AUTH}
            async def lost(*a,**kw):
                result=await execute(*a,**kw)
                if a[1]=="enroll":
                    if damage=="started-journal":
                        op=hashlib.sha256(a[2].encode()).hexdigest()
                        host._save_operation(op,{k:result[k] for k in ("provider","alias","expected_generation","generation")}|{"state":"started","outcome":"execution_unresolved"})
                        raise AccountError("account_host_unreachable")
                    await move_pin(runtime,current,4)
                return result
            runtime.executor.execute=lost
            with pytest.raises(VerbError):await runtime.operation(hatch)
            abandoned=host.registry.active(old.provider,old.alias,old.host)
            if damage in {"invalid-retained-proof","central-proof-recoverable"}:
                def corrupt(a):
                    from account_repin_state import get,put
                    key=_key(hatch);state=get(a,key);state["enrollment_receipt"]["proof"]["code_identity"]="f"*64;put(a,key,state["status"],state)
                await runtime.run(corrupt)
                if damage=="invalid-retained-proof":
                    op=hashlib.sha256(("hatch:"+hatch["operation_id"]).encode()).hexdigest()
                    damaged=host._read_operation(op);damaged["proof"]["code_identity"]="f"*64;host._save_operation(op,damaged)
            runtime.executor.execute=execute;runtime.clock=lambda:NOW+901
            await store.set_spawn_admission_hold(old.host,held_until=NOW+2000,reason="renewed second hatch fixture")
            status=await recovery(runtime,old)
            result=await runtime.operation(dict(hatch,operation_id="second-explicit-hatch",supersede_repin=status["snapshot_digest"]))
            assert result["result"]["binding"]["home"]!=old.home
            if damage=="central-proof-recoverable":
                assert result["result"]["binding"]["home"]==abandoned.home and len(logins)==2
            else:
                assert result["result"]["binding"]["home"]!=abandoned.home and len(logins)==3
                assert await runtime.run(lambda a:a.bound(abandoned.key)) is None
            assert not (await recovery(runtime,old))["fence_causes"]
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("damage",["host-journal","central-json","resolution"])
def test_b_hatch_status_and_supersession_need_no_trusted_old_evidence(tmp_path,monkeypatch,damage):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch)
        execute=runtime.executor.execute
        async def split(*a,**kw):
            result=await execute(*a,**kw)
            if a[1]=="repin":await move_pin(runtime,pin,3)
            return result
        runtime.executor.execute=split
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            runtime.executor.execute=execute
            from account_repin import _key
            key=_key(request)
            if damage=="host-journal":
                path=host.journal/(hashlib.sha256(("repin:"+request["operation_id"]).encode()).hexdigest()+".json")
                path.write_text("{corrupt fixture")
            else:
                def corrupt(a):
                    if damage=="central-json":a.conn.execute("UPDATE v2_account_recovery SET data=? WHERE binding_key=?",("{corrupt fixture",key))
                    else:a.conn.execute("INSERT INTO v2_account_recovery VALUES (?,?,?)",("repin-resolved:"+key,"done",json.dumps({"disposition":"untrusted"})))
                    a.conn.commit()
                await runtime.run(corrupt)
            status=await recovery(runtime,old)
            assert status["fence_causes"] and status["snapshot_digest"]
            result=await runtime.operation({"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"operation_id":"corrupt-proof-hatch","supersede_repin":status["snapshot_digest"],"_auth_context":AUTH})
            assert result["result"]["binding"]["home"]!=old.home and len(logins)==2
            assert not (await recovery(runtime,old))["fence_causes"]
            if damage=="central-json":assert await runtime.run(lambda a:a.conn.execute("SELECT data FROM v2_account_recovery WHERE binding_key=?",(key,)).fetchone()[0])=="{corrupt fixture"
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_s1_success(tmp_path,monkeypatch,subtests):
    _run_b_table("B-S1-success",tmp_path,monkeypatch,subtests)


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(180)
def test_b_table_representative_boundaries(tmp_path,monkeypatch,subtests):
    from repin_state_matrix import ROWS,cases,run
    from repin_matrix_extra import ROWS as extras
    for row in (*ROWS,*extras):
        # Fixed root/wrapper, both providers/platforms, artifact and outage
        # controls supplement the full fixed pre-action row in the unit gate.
        import itertools
        for variant,group in itertools.groupby(cases(row),lambda c:c.variant):
            selected=list(group)
            for index in sorted({0,min(18,len(selected)-1),len(selected)//2,len(selected)-1}):
                case=selected[index];case_root=tmp_path/(row+"-"+variant.replace("/","-")+"-"+str(index));case_root.mkdir()
                with subtests.test(msg=case.id),monkeypatch.context() as isolated:
                    asyncio.run(run(case,case_root,isolated))


def test_b_non_split_auth_settlement_then_authorized_recovery_uses_real_transport(tmp_path,monkeypatch):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch,transport=True)
        observe=host.observation
        def expired(_):raise AccountError("provider_auth_failed")
        host._probe_observation=expired
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            host.observation=observe;host._probe_observation=expired
            await runtime.record_failure(old,"auth")
            assert not (await recovery(runtime,old))["fence_causes"]
            result=await runtime.operation({"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"operation_id":"settled-auth-recovery","_auth_context":AUTH})
            assert result["result"]["outcome"]=="recovered"
            assert len(logins)==2 and result["result"]["binding"]["home"]!=old.home
            assert (await runtime.operation(dict(fresh(request,pin,"new-agreed-base"),login_generation=result["result"]["binding"]["generation"])))["result"]["outcome"]=="no_op"
        finally:store.stop()
    asyncio.run(run())


def test_b_startup_registration_cannot_supersede_an_unresolved_cell(tmp_path,monkeypatch):
    from dataclasses import replace
    from repin_state_matrix import outage_after_admission
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch,transport=True)
        execute=runtime.executor.execute;runtime.executor.execute=outage_after_admission(execute)
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            runtime.executor.execute=execute
            proposed=replace(old,generation="startup-replacement",home=old.home+"-startup")
            runtime.policy["bindings"]=[proposed.public()]
            def inactive(a):
                a.conn.execute("UPDATE v2_account_bindings SET active=0 WHERE binding_key=?",(old.key,));a.conn.commit()
            await runtime.run(inactive)
            before=list(native)
            await runtime.start()
            assert await runtime.run(lambda a:a.bound(proposed.key)) is None
            assert await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)) is None
            assert native==before
            # Repair only the deliberately removed selection in this fixture.
            def restore(a):
                a.conn.execute("UPDATE v2_account_bindings SET active=1 WHERE binding_key=?",(old.key,));a.conn.commit()
            await runtime.run(restore);runtime.policy.pop("bindings",None)
            result=await runtime.operation(fresh(request,pin,"after-startup-park",reconcile_of=request["operation_id"]))
            assert result["result"]["outcome"]=="repinned" and len(logins)==1
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("damage",["resolution-json","supersession-proof","disposition-race"])
def test_b_hatch_disposition_snapshot_is_complete_and_recoverable(tmp_path,monkeypatch,damage):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch,transport=True)
        execute=runtime.executor.execute
        async def split(*a,**kw):
            result=await execute(*a,**kw)
            if a[1]=="repin":await move_pin(runtime,pin,3)
            return result
        runtime.executor.execute=split
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            runtime.executor.execute=execute
            from account_repin import _key
            from account_repin_state import _supersession_prefix
            key=_key(request)
            def corrupt(a):
                proof_key=(_supersession_prefix(key)+"f"*64 if damage=="supersession-proof" else "repin-resolved:"+key)
                a.conn.execute("INSERT OR REPLACE INTO v2_account_recovery VALUES (?,?,?)",(proof_key,"done",
                    "{corrupt fixture" if damage=="resolution-json" else json.dumps({"disposition":"untrusted"})))
                a.conn.commit()
            if damage!="disposition-race":await runtime.run(corrupt)
            status=await recovery(runtime,old)
            payload={"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,"login_generation":old.generation,
                "operation_id":"snapshot-hatch","supersede_repin":status["snapshot_digest"],"_auth_context":AUTH}
            if damage=="disposition-race":
                async def raced(*a,**kw):
                    result=await execute(*a,**kw)
                    if a[1]=="repin-supersede":await runtime.run(corrupt)
                    return result
                runtime.executor.execute=raced
                with pytest.raises(VerbError,match="account_repin_state_changed"):await runtime.operation(payload)
                assert len(logins)==1
                runtime.executor.execute=execute
                payload.update(operation_id="fresh-after-disposition-race",supersede_repin=(await recovery(runtime,old))["snapshot_digest"])
            result=await runtime.operation(payload)
            assert result["result"]["binding"]["home"]!=old.home and len(logins)==2
            assert not (await recovery(runtime,old))["fence_causes"]
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(90)
def test_b_live_worker_boundaries(tmp_path,monkeypatch,subtests):
    from repin_state_matrix import cases,run
    import itertools
    for variant,group in itertools.groupby(cases("B-U1-live"),lambda c:c.variant):
        selected=list(group)
        for index in (0,18,len(selected)//2,len(selected)-1):
            case=selected[index];root=tmp_path/(variant+str(index));root.mkdir()
            with subtests.test(msg=case.id),monkeypatch.context() as mp:
                asyncio.run(run(case,root,mp))


@pytest.mark.parametrize("changed",["permit","seal"])
def test_b_hatch_rechecks_complete_host_mutation_snapshot_before_sealing(tmp_path,monkeypatch,changed):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch,transport=True)
        execute=runtime.executor.execute
        async def split(*args,**kwargs):
            result=await execute(*args,**kwargs)
            if args[1]=="repin":await move_pin(runtime,pin,3)
            return result
        runtime.executor.execute=split
        try:
            with pytest.raises(VerbError):await runtime.operation(request)
            runtime.executor.execute=execute
            status=await recovery(runtime,old)
            from provider_account_mutation import write,read
            from account_repin import _host_id
            # The authenticated actual selection is unchanged, but an old
            # executor's durable mutation metadata arrived after FD's snapshot.
            async def raced(*args,**kwargs):
                if args[1]=="repin-supersede":
                    write(host,"permits" if changed=="permit" else "seals","foreign-delayed-operation",
                        {"provider":old.provider,"alias":old.alias,"state":"pending","action":"repin"})
                return await execute(*args,**kwargs)
            runtime.executor.execute=raced
            payload={"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"operation_id":"host-snapshot-hatch",
                "supersede_repin":status["snapshot_digest"],"_auth_context":AUTH}
            with pytest.raises(VerbError):await runtime.operation(payload)
            # Refuse before superseding any captured operation, not merely when
            # a later enrollment discovers the new foreign permit.
            assert read(host,"seals",_host_id(request)) is None
            assert len(logins)==1 and (await recovery(runtime,old))["fence_causes"]
        finally:store.stop()
    asyncio.run(run())


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(90)
def test_b_crash_transaction_boundaries(tmp_path,monkeypatch,subtests):
    from repin_state_matrix import cases,run
    import itertools
    for row in ("B-Z-crash","B-C0"):
        for variant,group in itertools.groupby(cases(row),lambda c:c.variant):
            selected=list(group)
            for index in (0,18,len(selected)//2,len(selected)-1):
                case=selected[index];root=tmp_path/(row+variant+str(index));root.mkdir()
                with subtests.test(msg=case.id),monkeypatch.context() as mp:
                    asyncio.run(run(case,root,mp))


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(90)
def test_b_pin_origin_race_boundaries(tmp_path,monkeypatch,subtests):
    from repin_state_matrix import cases,run
    import itertools
    for row in ("B-race-pin","B-race-origin"):
        for variant,group in itertools.groupby(cases(row),lambda c:c.variant):
            selected=list(group)
            for index in (0,18,len(selected)//2,len(selected)-1):
                case=selected[index];root=tmp_path/(row+variant.replace("/","-")+str(index));root.mkdir()
                with subtests.test(msg=case.id),monkeypatch.context() as mp:
                    asyncio.run(run(case,root,mp))


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(90)
def test_b_evidence_collision_boundaries(tmp_path,monkeypatch,subtests):
    from repin_state_matrix import cases,run
    import itertools
    for row in ("B-N1-collision","B-invalid-proof"):
        for variant,group in itertools.groupby(cases(row),lambda c:c.variant):
            selected=list(group)
            for index in (0,18,len(selected)//2,len(selected)-1):
                case=selected[index];root=tmp_path/(row+variant+str(index));root.mkdir()
                with subtests.test(msg=case.id),monkeypatch.context() as mp:
                    asyncio.run(run(case,root,mp))


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(90)
def test_b_hatch_state_boundaries(tmp_path,monkeypatch,subtests):
    from repin_state_matrix import cases,run
    import itertools
    for row in ("B-hatch-all-states","B-hatch-prior-findings"):
        for variant,group in itertools.groupby(cases(row),lambda c:c.variant):
            selected=list(group)
            for index in (0,18,len(selected)//2,len(selected)-1):
                case=selected[index];root=tmp_path/(row+variant+str(index));root.mkdir()
                with subtests.test(msg=case.id),monkeypatch.context() as mp:
                    asyncio.run(run(case,root,mp))


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(90)
def test_b_hatch_race_refusal_boundaries(tmp_path,monkeypatch,subtests):
    from repin_state_matrix import cases,run
    import itertools
    for row in ("B-hatch-races","B-hatch-refusals"):
        for variant,group in itertools.groupby(cases(row),lambda c:c.variant):
            selected=list(group)
            for index in (0,18,len(selected)//2,len(selected)-1):
                case=selected[index];root=tmp_path/(row+variant+str(index));root.mkdir()
                with subtests.test(msg=case.id),monkeypatch.context() as mp:
                    asyncio.run(run(case,root,mp))


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(120)
def test_b_admission_consumer_boundaries(tmp_path,monkeypatch,subtests):
    from repin_state_matrix import cases,run
    import itertools
    for row in ("B-admission","B-status-locks"):
        for variant,group in itertools.groupby(cases(row),lambda c:c.variant):
            selected=list(group)
            for index in (0,18,len(selected)//2,len(selected)-1):
                case=selected[index];root=tmp_path/(row+variant.replace("/","-")+str(index));root.mkdir()
                with subtests.test(msg=case.id),monkeypatch.context() as mp:
                    asyncio.run(run(case,root,mp))


@pytest.mark.repin_exhaustive
@pytest.mark.timeout(120)
def test_b_topology_exclusion_boundaries(tmp_path,monkeypatch,subtests):
    from repin_state_matrix import cases,run
    import itertools
    for row in ("B-fence-topology","B-origin-refusals","B-generation-exclusion"):
        for variant,group in itertools.groupby(cases(row),lambda c:c.variant):
            selected=list(group)
            for index in (0,18,len(selected)//2,len(selected)-1):
                case=selected[index];root=tmp_path/(row+variant.replace("/","-")+str(index));root.mkdir()
                with subtests.test(msg=case.id),monkeypatch.context() as mp:
                    asyncio.run(run(case,root,mp))
