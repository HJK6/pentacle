"""Actual immutable-journal, collision and private-anchor matrix controls."""
import copy
from dataclasses import asdict,replace
from pathlib import Path

import pytest
from provider_accounts import AccountError,Binding
from sessions import VerbError
from test_account_runtime import AUTH

ROWS=("B-N1-collision","B-invalid-proof")
VARIANTS={"B-N1-collision":("known-collision","partial-collision","changed-host","host-disabled","health","policy-disabled","unsafe-anchor","operation-id"),
    "B-invalid-proof":("missing-journal","journal-arguments","nested-target","consumer-identity","pin","registry-row","anchor","private-component",
        "adopted-owner","provider","pool","profile","platform","auth-store","proof","receipt","enrollment-ancestry")}
ANCHOR_ROWS=set(ROWS)


async def prepare(case,j,mp,current,parent,tmp_path,attempt):
    from repin_state_matrix import target_version
    from test_account_repin_state_machine import fresh,recovery,move_pin
    from account_repin import _host_id
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    execute=runtime.executor.execute;expected_logins=len(logins)
    if case.row=="B-N1-collision":
        base=host.registry.active(old.provider,old.alias,old.host)
        registry=copy.deepcopy(host.registry._load());policy=copy.deepcopy(runtime.policy);saved_cell=copy.deepcopy(cell)
        journal=host.journal/(_host_id(attempt)+".json");prior_journal=journal.read_bytes() if journal.exists() else None
        home=Path(base.home);mode=home.stat().st_mode & 0o777
        known=None
        if case.variant=="known-collision":
            # A completed independent enrollment predates this repin's permit.
            # It is fixture setup through the supported native enrollment stack.
            host.config.pop("mutation_clearance")
            try:
                known=host.handle("enroll",provider=old.provider,alias=old.alias,expected_generation=base.generation,
                    operation_id="independent-prior-enrollment",generation=attempt["new_generation"],pin=current)
            finally:host.config["mutation_clearance"]="required"
            expected_logins+=1
            modified=host.registry._load();modified["active"][f"{old.host}:{old.provider}:{old.alias}"]=base.key
            host.registry._save(modified)
        elif case.variant in {"partial-collision","changed-host"}:
            proposed=replace(base,generation=attempt["new_generation"])
            modified=copy.deepcopy(registry);modified["generations"][proposed.key]=asdict(proposed)
            if case.variant=="changed-host":modified["active"][f"{old.host}:{old.provider}:{old.alias}"]=proposed.key
            host.registry._save(modified)
        elif case.variant=="host-disabled":cell["enabled"]=False
        elif case.variant=="health":await runtime.record_failure(old,"identity")
        elif case.variant=="policy-disabled":runtime.policy["enabled"]=False
        elif case.variant=="unsafe-anchor":home.chmod(0o777)
        elif case.variant=="operation-id":host._save_operation(_host_id(attempt),{"kind":"enroll","state":"done","outcome":"ok"})
        native_before=list(native)
        try:
            with pytest.raises(VerbError):await runtime.operation(attempt)
            state_now=host.registry._load()
            selected_key=state_now["active"][f"{old.host}:{old.provider}:{old.alias}"]
            assert state_now["generations"][selected_key]["generation"]==(
                attempt["new_generation"] if case.variant=="changed-host" else base.generation)
            if case.variant in {"partial-collision","changed-host","operation-id"}:
                assert (await recovery(runtime,old))["fence_causes"],"Unknown provenance is not proof of non-mutation"
            # All independent safety defects still forbid launch. Release the
            # hold here so it cannot mask their actual admission predicate.
            if case.variant in {"health","policy-disabled","unsafe-anchor","host-disabled"}:
                await store.clear_spawn_admission_hold(old.host)
                with pytest.raises((AccountError,VerbError)):
                    await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
                        message={"account":old.alias,"_auth_context":AUTH})
                await store.set_spawn_admission_hold(old.host,held_until=runtime.clock()+1000,reason="repaired safety fixture")
            assert not any("Reply exactly OK" in argv for argv in native[len(native_before):])
        finally:
            home.chmod(mode);cell.clear();cell.update(saved_cell);runtime.policy.clear();runtime.policy.update(policy)
            # Keep an authenticated pre-existing collision row as immutable
            # history; only restore the deliberately injected corrupt rows.
            if case.variant!="known-collision":host.registry._save(registry)
            if case.variant=="operation-id":
                if prior_journal is None:journal.unlink()
                else:journal.write_bytes(prior_journal)
        if case.variant=="health":
            revision=await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["health_revision"])
            healed=await runtime.operation({"action":"revalidate-health","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"expected_revision":revision,"operation_id":"collision-health-repaired","reason":"real read-only proof","_auth_context":AUTH})
            assert healed["result"]["outcome"]=="health_revalidated"
        status=await recovery(runtime,old)
        return {"current":current,"parent":status["canonical_tail"] if status["fence_causes"] else None,
            "expected_logins":expected_logins,"reanchor":bool(parent and case.variant=="health")}
    # Every invalid-proof row starts with actual successful host work and stale
    # central selection; wrapper mode adds a real completed target suboperation.
    if case.mode=="reconcile" or case.variant=="nested-target":
        current=await target_version(j,current,"8.8.4",tmp_path,"invalid-proof-wrapper")
        next_attempt=fresh(request,current,"invalid-proof-wrapper",reconcile_of=parent)
        async def split(*args,**kwargs):
            nonlocal current
            result=await execute(*args,**kwargs)
            if args[1]=="repin-reconcile":current=await move_pin(runtime,current,current["revision"]+1)
            return result
        runtime.executor.execute=split
        with pytest.raises(VerbError):await runtime.operation(next_attempt)
        runtime.executor.execute=execute;parent=next_attempt["operation_id"]
    registry=copy.deepcopy(host.registry._load());saved_cell=copy.deepcopy(cell)
    actual=host.registry.active(old.provider,old.alias,old.host)
    operation=_host_id(request if case.mode=="root" and case.variant!="nested-target" else next_attempt)
    if case.variant=="nested-target" or (case.mode=="reconcile" and case.variant in {
            "missing-journal","consumer-identity","pool","proof","receipt","enrollment-ancestry"}):operation+="-target"
    path=host.journal/(operation+".json");original=path.read_bytes();record=host._read_operation(operation)
    home=Path(actual.home);home_mode=home.stat().st_mode & 0o777
    auth_path=home/("auth.json" if old.provider=="codex" else ".credentials.json")
    auth_bytes=auth_path.read_bytes() if auth_path.exists() else None
    auth_mode=auth_path.stat().st_mode & 0o777 if auth_path.exists() else None
    modified=copy.deepcopy(registry)
    if case.variant=="missing-journal":path.unlink()
    elif case.variant=="anchor":modified["generations"].pop(old.key);host.registry._save(modified)
    elif case.variant=="registry-row":modified["generations"][actual.key]["pool_id"]="foreign-pool";host.registry._save(modified)
    elif case.variant=="adopted-owner":
        foreign=replace(old,alias="foreign-owner",generation="foreign-generation")
        modified["generations"][foreign.key]=asdict(foreign);host.registry._save(modified)
    elif case.variant=="private-component":home.chmod(0o777)
    elif case.variant=="auth-store":
        if auth_bytes is None:auth_path.write_text("fixture unexpected credential store");auth_path.chmod(0o600)
        else:auth_path.chmod(0o644)
    elif case.variant in {"profile","platform","provider"}:
        cell[{"profile":"profile_ref","platform":"platform","provider":"expected_provider_id"}[case.variant]]="foreign"
    else:
        if case.variant=="journal-arguments":record["expected_generation"]="wrong-origin"
        elif case.variant=="consumer-identity":record["proof"]["code_identity"]="f"*64
        elif case.variant=="pin":record["pin_digest"]="f"*64
        elif case.variant=="pool":record["binding"]["pool_id"]="foreign"
        elif case.variant=="proof":record.pop("proof",None)
        elif case.variant=="receipt":record["outcome"]="provider_auth_failed"
        elif case.variant=="enrollment-ancestry":record["kind"]="enroll";record["new_home"]=True
        elif case.variant=="nested-target":record["proof"]["pin_digest"]="f"*64
        host._save_operation(operation,record)
    try:
        before=sum("Reply exactly OK" in argv for argv in native)
        with pytest.raises(VerbError):await runtime.operation(fresh(request,current,"invalid-evidence-recovery",reconcile_of=parent))
        assert (await recovery(runtime,old))["fence_causes"]
        assert await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["generation"])==old.generation
        assert sum("Reply exactly OK" in argv for argv in native)==before and len(logins)==expected_logins
    finally:
        path.write_bytes(original);path.chmod(0o600);home.chmod(home_mode)
        host.registry._save(registry);cell.clear();cell.update(saved_cell)
        if auth_bytes is None:
            if auth_path.exists():auth_path.unlink()
        else:auth_path.write_bytes(auth_bytes);auth_path.chmod(auth_mode)
    status=await recovery(runtime,old)
    return {"current":current,"parent":status["canonical_tail"],"expected_logins":expected_logins}
