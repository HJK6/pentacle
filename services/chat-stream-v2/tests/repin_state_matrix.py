"""Deterministic Addendum B scenario product, with real durable boundaries."""
import asyncio
import copy
from dataclasses import dataclass
import hashlib
import itertools
import json
import os
from pathlib import Path

import pytest
from provider_accounts import AccountError,Binding
from provider_cli_pin import digest
from sessions import VerbError
from test_account_runtime import AUTH

CELLS=tuple((p,s,a) for p,s in (("codex","macos"),("codex","wsl"),("claude","macos"),("claude","wsl")) for a in ("ours","sai"))
PINS=((0,"identical"),)+tuple(itertools.product((1,3),("identical","other-provider","version","native","resources","rollback-central","rollback-tip")))
CONNECTIVITY=("online","return","flap")
MODES=("root","reconcile")
# Each row is a genuine public journey. Specialized race/identity/control rows
# in test_account_repin_state_machine.py supply the remaining B4/B5 transitions.
ROWS=("B-N1-verifier","B-N1-unknown","B-N2-before","B-N2-turn","B-N2-after","B-N2-artifact","B-N2-registry",
    "B-U0-absent","B-U0-success","B-U1-dead","B-Z-late","B-N3","B-S0","B-S1-absent","B-S1-failed","B-S1-success","B-hatch-G1","B-N0","B-N1-families","B-origin-reanchor","B-combined-liveness","B-S1-settled-tail","B-S1-mixed","B-fence-union")


# Emitted pre-start families are inventoried from NativeHost._cell/_repin,
# verify/assert_drained, Registry home checks and mutation authorization. The
# effect partition must also handle an authenticated code unknown at release.
PRESTART=("account_cell_disabled","account_login_generation_stale","account_unavailable","account_generation_exists",
    "account_operation_conflict","provider_cli_pin_mismatch","provider_cli_verification_unavailable",
    "account_repin_home_busy","account_repin_occupancy_unavailable","account_mutation_clearance_required",
    "account_home_owner_conflict","novel_authenticated_refusal")
VARIANTS={"B-N0":("missing-hold","expired-hold","bad-auth","stale-pin","wrong-generation","bad-reason","bad-digest"),
    "B-N1-families":PRESTART,"B-origin-reanchor":("default","health","policy","combined"),
    "B-fence-union":("sibling","non-tail","changed-payload"),"B-combined-liveness":("split","non-split")}


@dataclass(frozen=True)
class Case:
    row:str
    provider:str
    platform:str
    alias:str
    mode:str
    moves:int
    target:str
    connection:str
    variant:str=""
    adopted:bool=False
    @property
    def id(self):return ":".join(map(str,(self.row,self.provider,self.platform,self.alias,self.mode,self.moves,self.target,self.connection,self.variant,"adopted" if self.adopted else "managed")))


def cases(row):
    from repin_matrix_extra import VARIANTS as extras
    for variant in VARIANTS.get(row,extras.get(row,("",))):
        for c,m,p,n in itertools.product(CELLS,MODES,PINS,CONNECTIVITY):
            if row=="B-combined-liveness" and variant=="non-split" and m=="reconcile":continue
            if row=="B-generation-exclusion" and variant=="enrollment-first" and m=="reconcile":continue
            if row=="B-admission" and c[0]=="codex" and variant.split("/")[-1] in {"resume","restore"}:continue
            from repin_matrix_evidence import ANCHOR_ROWS
            for adopted in ((False,True) if row in ANCHOR_ROWS and c[0]=="codex" else (False,)):
                if row=="B-invalid-proof" and variant=="adopted-owner" and not adopted:continue
                if row=="B-invalid-proof" and variant=="anchor" and adopted:continue
                yield Case(row,*c,m,*p,n,variant,adopted)


async def checkpoint(j,label):
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    from provider_cli_pin import raw
    def read(accounts):
        from account_repin_state import causes,tail
        value={"host":old.host,"provider":old.provider,"account":old.alias}
        try:address=tail(accounts,value)
        except AccountError:address=None
        selected=accounts._cell(old.host,old.provider,old.alias)
        return {"pin":raw(accounts),"central_generation":selected["generation"] if selected else None,
            "canonical_tail":address,"fence_causes":causes(accounts,value),
            "dispositions":[(row["binding_key"],hashlib.sha256(row["data"].encode()).hexdigest())
                for row in accounts.conn.execute("SELECT binding_key,data FROM v2_account_recovery WHERE binding_key GLOB 'repin-*'")]}
    trace=await runtime.run(read);registry=host.registry._load()
    trace.update(label=label,host_selection=registry["active"],signins=len(logins),
        minimal_turns=sum("Reply exactly OK" in argv for argv in native))
    runtime._matrix_trace.append(trace)


async def snapshot(j):
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    from provider_cli_pin import raw
    def central(a):
        return {"pin":raw(a),"bindings":[dict(r) for r in a.conn.execute("SELECT * FROM v2_account_bindings")],
            "defaults":[dict(r) for r in a.conn.execute("SELECT * FROM v2_account_defaults")],
            "recovery":[dict(r) for r in a.conn.execute("SELECT * FROM v2_account_recovery")],
            "hold":[dict(r) for r in a.conn.execute("SELECT * FROM v2_spawn_admission_hold")]}
    journals={}
    if host.journal.exists():
        for p in sorted(host.journal.rglob("*.json")):journals[str(p.relative_to(host.journal))]=json.loads(p.read_text())
    return {"central":await runtime.run(central),"registry":host.registry._load(),"journals":journals,
        "minimal_turns":sum("Reply exactly OK" in v for v in native),"signins":len(logins),"dispatch":list(calls),
        "phases":copy.deepcopy(getattr(runtime,"_matrix_trace",[]))}


def evidence(case,before,after,outcome,admission=None):
    root=os.environ.get("PENTACLE_GATE_EVIDENCE_DIR")
    if not root:return
    path=Path(root)/"state-machine-cases";path.mkdir(exist_ok=True)
    data={"case_id":case.id,"axes":case.__dict__,"before":before,"after":after,"outcome":outcome,"admission":admission}
    (path/(hashlib.sha256(case.id.encode()).hexdigest()+".json")).write_text(json.dumps(data,sort_keys=True,default=str))


def outage_after_admission(execute):
    """Lose transport after the guard read and reservation, before publication."""
    state={"offline":False}
    async def offline(*args,**kwargs):
        if state["offline"] or args[1] in {"mutation-reserve","repin","repin-reconcile"}:
            state["offline"]=True;raise AccountError("account_host_unreachable")
        return await execute(*args,**kwargs)
    return offline


async def stage(j,case,current,tip,tmp_path):
    from test_account_repin_state_machine import move_pin
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    if not case.moves:return current
    target=copy.deepcopy(current);entry=target["providers"][old.provider]
    roots=cell.setdefault("resource_roots",{})
    for path in (old.executable,cell["executable"]):roots.setdefault(path,path)
    if case.target in {"rollback-central","rollback-tip"}:
        chosen=old if case.target=="rollback-central" else tip
        cell.update(executable=chosen.executable,executable_sha256=chosen.executable_sha256,
            version=chosen.version,resource_root=roots[chosen.executable])
        entry["version"]=chosen.version
        for artifact in entry["artifacts"].values():
            artifact.update(native_sha256=chosen.executable_sha256,resource_sha256=hashlib.sha256(Path(roots[chosen.executable]).read_bytes()).hexdigest())
    elif case.target in {"version","native","resources"}:
        binary=tmp_path/"current-native";binary.write_bytes(Path(cell["executable"]).read_bytes()+(b"different" if case.target=="native" else b""))
        resource=binary
        if case.target=="resources":resource=tmp_path/"current-resource";resource.write_bytes(b"different package resources")
        native_hash=hashlib.sha256(binary.read_bytes()).hexdigest();resource_hash=hashlib.sha256(resource.read_bytes()).hexdigest()
        if case.target=="version":
            entry["version"]="9.9.8";binary.write_bytes(("fixture native version "+entry["version"]).encode())
            native_hash=hashlib.sha256(binary.read_bytes()).hexdigest();resource_hash=native_hash
        for artifact in entry["artifacts"].values():artifact.update(native_sha256=native_hash,resource_sha256=resource_hash)
        cell.update(executable=str(binary),executable_sha256=native_hash,version=entry["version"],resource_root=str(resource))
        roots[str(binary)]=str(resource)
        cell["native_versions"][str(binary)]=entry["version"]
    elif case.target=="other-provider":target["providers"]["claude" if old.provider=="codex" else "codex"]["version"]="9.9.7"
    for index in range(case.moves):
        # Several moves explicitly include A→B→A bytes at increasing revisions.
        chosen=copy.deepcopy(target if index==case.moves-1 or index%2==0 else current)
        chosen["revision"]=current["revision"]+1
        await runtime.operation({"action":"pin-set","pin":chosen,"expected_revision":current["revision"],
            "operation_id":"matrix-move-"+str(chosen["revision"]),"reason":"table fixture","_auth_context":AUTH})
        current=chosen
    return current


async def target_version(j,current,target_version,tmp_path,label):
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    changed=copy.deepcopy(current);changed["revision"]+=1;changed["providers"][old.provider]["version"]=target_version
    binary=tmp_path/(label+"-native");binary.write_bytes(("fixture native version "+target_version).encode())
    sha=hashlib.sha256(binary.read_bytes()).hexdigest()
    for artifact in changed["providers"][old.provider]["artifacts"].values():artifact.update(native_sha256=sha,resource_sha256=sha)
    cell.update(executable=str(binary),executable_sha256=sha,version=target_version,resource_root=str(binary))
    cell["native_versions"][str(binary)]=target_version;cell["resource_roots"][str(binary)]=str(binary)
    await runtime.operation({"action":"pin-set","pin":changed,"expected_revision":current["revision"],
        "operation_id":label,"reason":"matrix immutable target","_auth_context":AUTH})
    return changed


async def run(case,tmp_path,mp):
    from test_account_repin import host_journey
    from test_account_repin_state_machine import move_pin,fresh,recovery
    j=list(await host_journey(tmp_path,mp,case.provider,case.platform,alias=case.alias,adopted=case.adopted,transport=True))
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    runtime._matrix_trace=[]
    before=await snapshot(j);outcome="failed";admitted=None
    execute=runtime.executor.execute;original_run=host.run;observe=host.observation
    import provider_cli_artifact
    verify=provider_cli_artifact.verify;registry_repin=host.registry.repin
    current=pin;parent=None;attempt=request;late_dispatch=None;reanchor=False
    initial_logins=len(logins);expected_logins=initial_logins;staged=False;historical=None;hatch=False;hatch_signins=initial_logins+1;after_check=None
    roots=cell.setdefault("resource_roots",{});roots.update({old.executable:old.executable,cell["executable"]:cell["resource_root"]})
    async def split_root():
        nonlocal current,parent
        # Assignment in the nested dispatch updates this journey's current pin.
        async def dispatch(*args,**kwargs):
            nonlocal current
            result=await execute(*args,**kwargs)
            if args[1]=="repin":current=await move_pin(runtime,current,current["revision"]+1)
            return result
        runtime.executor.execute=dispatch
        with pytest.raises(VerbError):await runtime.operation(request)
        runtime.executor.execute=execute;parent=request["operation_id"]
        await checkpoint(j,"proved-root-split")
    try:
        need_split=case.mode=="reconcile" or case.row in {"B-origin-reanchor","B-combined-liveness","B-S1-settled-tail","B-S1-mixed","B-fence-union","B-S0","B-S1-absent","B-S1-failed","B-S1-success","B-hatch-G1","B-invalid-proof"}
        if case.row=="B-combined-liveness" and case.variant=="non-split":need_split=False
        if need_split:await split_root()
        if need_split and (case.row.startswith("B-N2") or case.row in {"B-S1-failed","B-S1-success"}):
            # A target-changing wrapper really enters its native proof phases.
            current=await target_version(j,current,"8.8.8",tmp_path,"force-target")
        if parent:attempt=fresh(request,current,"matrix-fault",reconcile_of=parent)
        from repin_matrix_extra import ROWS as extra_rows,prepare
        if case.row in extra_rows:
            prepared=await prepare(case,j,mp,current,parent,tmp_path,attempt)
            current=prepared["current"];parent=prepared["parent"];expected_logins=prepared["expected_logins"];staged=prepared.get("staged",False)
            store,runtime=j[:2];execute=runtime.executor.execute;registry_repin=host.registry.repin
            historical=prepared.get("historical");reanchor=prepared.get("reanchor",False)
            hatch=prepared.get("hatch",False);hatch_signins=prepared.get("hatch_signins",initial_logins+1);after_check=prepared.get("after")
        elif case.row=="B-N0":
            bad=dict(attempt);baseline_calls=list(calls)
            states_before=await runtime.run(lambda a:a.conn.execute("SELECT count(*) FROM v2_account_recovery").fetchone()[0])
            if case.variant=="missing-hold":await store.clear_spawn_admission_hold(old.host)
            elif case.variant=="expired-hold":await store.set_spawn_admission_hold(old.host,held_until=runtime.clock(),reason="expired fixture")
            elif case.variant=="bad-auth":bad["_auth_context"]={"role":"front-desk"}
            elif case.variant=="stale-pin":bad["expected_pin_revision"]-=1
            elif case.variant=="wrong-generation":bad["login_generation"]="wrong"
            elif case.variant=="bad-reason":bad["reason"]=""
            elif case.variant=="bad-digest":bad["expected_pin_digest"]="f"*64
            with pytest.raises(VerbError):await runtime.operation(bad)
            assert calls==baseline_calls
            assert await runtime.run(lambda a:a.conn.execute("SELECT count(*) FROM v2_account_recovery").fetchone()[0])==states_before
            if case.variant in {"missing-hold","expired-hold"}:
                await store.set_spawn_admission_hold(old.host,held_until=runtime.clock()+1000,reason="repaired fixture hold")
        elif case.row in {"B-origin-reanchor","B-combined-liveness","B-S1-settled-tail","B-S1-mixed","B-fence-union"}:
            if case.row=="B-combined-liveness" and not need_split:
                def failed_identity(_):raise AccountError("provider_auth_failed")
                host.observation=failed_identity
                with pytest.raises(VerbError):await runtime.operation(request)
                host.observation=observe
                assert not (await recovery(runtime,old))["fence_causes"]
                await runtime.record_failure(old,"auth")
                host._probe_observation=failed_identity
                recovered=await runtime.operation({"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                    "login_generation":old.generation,"operation_id":"matrix-auth-recovery","_auth_context":AUTH})
                assert recovered["result"]["outcome"]=="recovered"
                host._probe_observation=observe;expected_logins+=1
            if need_split and case.row in {"B-S1-mixed","B-combined-liveness"}:
                current=await target_version(j,current,"8.8.9",tmp_path,"mixed-target-change")
                async def lost_success(*a,**kw):
                    nonlocal current
                    result=await execute(*a,**kw)
                    if a[1]=="repin-reconcile":current=await move_pin(runtime,current,current["revision"]+1)
                    return result
                runtime.executor.execute=lost_success
                successful=fresh(request,current,"mixed-success",reconcile_of=parent)
                with pytest.raises(VerbError):await runtime.operation(successful)
                runtime.executor.execute=execute;parent=successful["operation_id"]
            if need_split and case.row in {"B-combined-liveness","B-S1-settled-tail","B-S1-mixed","B-fence-union"}:
                # Three distinct zero-link wrappers; preserve the root split.
                for number in range(3):
                    save=host._save_operation
                    offline=outage_after_admission(execute)
                    def refused(*a,**kw):raise AccountError("novel_authenticated_refusal")
                    def aborted(op,data):
                        save(op,data)
                        if data.get("kind")=="repin-reconcile" and data.get("state")=="started":raise AccountError("account_host_unreachable")
                    if number==0:runtime.executor.execute=offline
                    elif number==1:mp.setattr(provider_cli_artifact,"verify",refused)
                    else:
                        # Force a new target phase so the wrapper durably starts.
                        current=await target_version(j,current,"8.8.7",tmp_path,"aborted-target")
                        host._save_operation=aborted
                    failed=fresh(request,current,"prefix-"+str(number),reconcile_of=parent)
                    with pytest.raises(VerbError):await runtime.operation(failed)
                    runtime.executor.execute=execute;host._save_operation=save;mp.setattr(provider_cli_artifact,"verify",verify)
                    parent=failed["operation_id"]
                    assert (await recovery(runtime,old))["fence_causes"]
                    await checkpoint(j,"mixed-prefix-"+str(number))
                if case.row=="B-fence-union":
                    original=fresh(request,current,"forbidden",reconcile_of=request["operation_id"])
                    if case.variant=="changed-payload":original=dict(failed,reason="changed")
                    with pytest.raises(VerbError):await runtime.operation(original)
                    assert (await recovery(runtime,old))["canonical_tail"]==parent
            if case.row=="B-S1-settled-tail":
                async def stop_after_settlement(*a,**kw):
                    if a[1]=="repin-reconcile":raise AccountError("account_host_unreachable")
                    return await execute(*a,**kw)
                runtime.executor.execute=stop_after_settlement
                settled=fresh(request,current,"settled-reserved-tail",reconcile_of=parent)
                with pytest.raises(VerbError):await runtime.operation(settled)
                runtime.executor.execute=execute;parent=settled["operation_id"]
                status=await recovery(runtime,old)
                assert status["canonical_tail"]==parent
                assert any(d.get("disposition")=="sealed_non_split" for d in status["dispositions"])
                with pytest.raises(VerbError):await runtime.operation(fresh(request,current,"non-tail",reconcile_of=request["operation_id"]))
                assert (await recovery(runtime,old))["canonical_tail"]==parent
            if case.row in {"B-origin-reanchor","B-combined-liveness"}:
                # Following ordinary fresh enrollment the agreed immutable base
                # changed legally; metadata diagnosis names that current base.
                central=await runtime.run(lambda a:Binding(**json.loads(a._cell(old.host,old.provider,old.alias)["data"])))
                variant=case.variant if case.row=="B-origin-reanchor" else "combined"
                if variant in {"default","combined"}:
                    peer=__import__('dataclasses').replace(old,alias="matrix-peer",generation="peer-generation",home=old.home+"-peer")
                    await runtime.run(lambda a:a.register(peer))
                    await runtime.operation({"action":"default-set","host":old.host,"provider":old.provider,"account":peer.alias,"_auth_context":AUTH})
                if variant in {"health","combined"}:
                    await runtime.record_failure(central,"identity")
                    rev=await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["health_revision"])
                    healed=await runtime.operation({"action":"revalidate-health","host":old.host,"provider":old.provider,"account":old.alias,
                        "login_generation":central.generation,"expected_revision":rev,"operation_id":"matrix-health","reason":"identity repaired","_auth_context":AUTH})
                    assert healed["result"]["outcome"]=="health_revalidated"
                if variant in {"policy","combined"}:runtime.policy["matrix-policy-revision"]=1
                # The alternate metadata transition is always explicit.
                if need_split:
                    with pytest.raises(VerbError):await runtime.operation(fresh(request,current,"implicit-origin",reconcile_of=parent))
                    reanchor=True
                    if case.row=="B-combined-liveness":
                        baseline=list(calls)
                        with pytest.raises(VerbError,match="account_repin_reconciliation_required"):
                            await runtime.operation({"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                                "login_generation":old.generation,"operation_id":"excluded-prefix-relogin","_auth_context":AUTH})
                        assert calls==baseline
        elif case.row in {"B-S0","B-hatch-G1"}:
            pass
        elif case.row in {"B-N1-verifier","B-N1-unknown","B-N1-families"}:
            def fail(*a,**kw):raise AccountError(case.variant if case.row=="B-N1-families" else "provider_cli_pin_mismatch" if case.row=="B-N1-verifier" else "novel_authenticated_refusal")
            mp.setattr(provider_cli_artifact,"verify",fail)
            with pytest.raises(VerbError):await runtime.operation(attempt)
            parent=attempt["operation_id"] if need_split else None
        elif case.row.startswith("B-N2") or case.row=="B-S1-failed":
            counter={"n":0}
            def observation(account):
                counter["n"]+=1
                if case.row.endswith("before") or case.row=="B-S1-failed" or (case.row.endswith("after") and counter["n"]==2):
                    raise AccountError("provider_transport_failed")
                return observe(account)
            host.observation=observation
            if case.row.endswith("turn"):
                def fail_turn(argv,**kw):
                    if "Reply exactly OK" in argv:
                        native.append(argv);raise AccountError("provider_capacity")
                    return original_run(argv,**kw)
                host.run=fail_turn
            if case.row.endswith("artifact"):
                count={"n":0}
                def fail_verify(*a,**kw):
                    count["n"]+=1
                    if count["n"]>=2:raise AccountError("provider_cli_pin_mismatch")
                    return verify(*a,**kw)
                mp.setattr(provider_cli_artifact,"verify",fail_verify)
            if case.row.endswith("registry"):
                def fail_registry(*a,**kw):raise AccountError("provider_probe_failed")
                host.registry.repin=fail_registry
            with pytest.raises(VerbError):await runtime.operation(attempt)
            parent=attempt["operation_id"] if need_split else None
        elif case.row in {"B-U0-absent","B-S1-absent"}:
            offline=outage_after_admission(execute)
            runtime.executor.execute=offline
            with pytest.raises(VerbError):await runtime.operation(attempt)
            parent=attempt["operation_id"] if need_split else None
        elif case.row=="B-U0-success":
            async def lost(*a,**kw):
                result=await execute(*a,**kw)
                if a[1] in {"repin","repin-reconcile"}:raise AccountError("account_host_unreachable")
                return result
            runtime.executor.execute=lost
            with pytest.raises(VerbError):await runtime.operation(attempt)
            parent=attempt["operation_id"]
        elif case.row in {"B-U1-dead","B-Z-late"}:
            async def started(*a,**kw):
                nonlocal late_dispatch
                if a[1] in {"repin","repin-reconcile"}:
                    late_dispatch=(a,kw)
                    if case.row=="B-U1-dead":
                        ident={"provider":old.provider,"alias":old.alias,"expected_generation":old.generation,
                            "generation":attempt["new_generation"],"pin_revision":current["revision"],"pin_digest":digest(current),
                            "kind":"repin-reconcile" if a[1]=="repin-reconcile" else "repin","state":"started","outcome":"execution_unresolved"}
                        if a[1]=="repin-reconcile":ident["reconcile"]={k:v for k,v in kw["reconcile"].items() if k!="_clearance"}
                        host._save_operation(hashlib.sha256(a[2].encode()).hexdigest(),ident)
                    raise AccountError("account_host_unreachable")
                return await execute(*a,**kw)
            runtime.executor.execute=started
            with pytest.raises(VerbError):await runtime.operation(attempt)
            parent=attempt["operation_id"]
        elif case.row=="B-S1-success":
            save=host._save_operation
            def missing_wrapper(operation,data):
                if data.get("kind")=="repin-reconcile" and data.get("state")=="done":
                    raise AccountError("account_host_unreachable")
                return save(operation,data)
            host._save_operation=missing_wrapper
            async def lost(*a,**kw):
                if a[1] in {"repin-inspect","repin-settle"} and calls.count("repin-reconcile"):
                    raise AccountError("account_host_unreachable")
                return await execute(*a,**kw)
            runtime.executor.execute=lost
            with pytest.raises(VerbError):await runtime.operation(attempt)
            host._save_operation=save;parent=attempt["operation_id"]
        elif case.row in {"B-N3","B-C0"}:
            result=await runtime.operation(attempt)
            selected=host.registry.active(old.provider,old.alias,old.host)
            if case.row=="B-N3":
                no_op=fresh(dict(request,login_generation=selected.generation),current,"matrix-noop")
                result=await runtime.operation(no_op);assert result["result"]["outcome"]=="no_op";attempt=no_op
            recorded=copy.deepcopy(result);baseline_calls=list(native)
            assert await runtime.operation(attempt)==recorded and native==baseline_calls
            parent=None
        else:raise AssertionError("unimplemented matrix row "+case.row)
        await checkpoint(j,"named-row-boundary")
        runtime.executor.execute=execute;host.run=original_run;host.observation=observe;host.registry.repin=registry_repin
        mp.setattr(provider_cli_artifact,"verify",verify)
        if late_dispatch and case.row=="B-Z-late":
            baseline=list(native)
            with pytest.raises(AccountError,match="account_host_failed"):
                await execute(*late_dispatch[0],**late_dispatch[1])
            # The exact host error is checked independently of the transport's
            # intentionally closed error vocabulary.
            args,kwargs=late_dispatch
            with pytest.raises(AccountError,match="account_operation_sealed"):
                host.handle(args[1],provider=args[0].provider,alias=args[0].alias,expected_generation=args[0].generation,
                    operation_id=hashlib.sha256(args[2].encode()).hexdigest(),generation=args[3],pin=kwargs.get("pin"),reconcile=kwargs.get("reconcile"))
            assert native==baseline
        tip=host.registry.active(old.provider,old.alias,old.host)
        if not staged:current=await stage(j,case,current,tip,tmp_path)
        if historical:
            replay,recorded=historical;native_before=list(native);dispatch_before=list(calls)
            assert await runtime.operation(replay)==recorded
            assert native==native_before and calls==dispatch_before
        await checkpoint(j,"current-target-after-pin-product")
        async def options(parent):
            result={"reconcile_of":parent} if parent else {}
            if reanchor:result["expected_origin_digest"]=(await recovery(runtime,old))["origin_digest"]
            return result
        if case.row=="B-hatch-G1" or hatch:
            if case.row=="B-hatch-G1":
                runtime.policy["watchers"]={"host-a:fixture-watcher":{"host":old.host}}
                await runtime.operation({"action":"observe-failure","host":old.host,"provider":old.provider,"account":old.alias,
                    "login_generation":old.generation,"kind":"auth","_auth_context":{"service_authenticated":True,"service_actor":"host-a:fixture-watcher"}})
            status=await recovery(runtime,old)
            selected=await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["generation"])
            payload={"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":selected,"operation_id":"matrix-hatch","supersede_repin":status["snapshot_digest"],"_auth_context":AUTH}
            if case.connection!="online":
                async def dropped(*a,**kw):
                    if case.connection=="flap" and a[1]=="repin-supersede":
                        await execute(*a,**kw);raise AccountError("account_host_unreachable")
                    if case.connection=="return":raise AccountError("account_host_unreachable")
                    return await execute(*a,**kw)
                runtime.executor.execute=dropped
                with pytest.raises(VerbError):await runtime.operation(payload)
                runtime.executor.execute=execute
                payload.update(operation_id="matrix-hatch-return",supersede_repin=(await recovery(runtime,old))["snapshot_digest"])
            result=await runtime.operation(payload)
            assert result["result"]["binding"]["home"]!=old.home and len(logins)==hatch_signins
        else:
            selected=await runtime.run(lambda a:Binding(**json.loads(a._cell(old.host,old.provider,old.alias)["data"])))
            request_now=dict(request,login_generation=selected.generation)
            # Build the request before installing the outage, so status reads
            # cannot impersonate the boundary the request is meant to exercise.
            if case.connection!="online":
                offline_request=fresh(request_now,current,"offline-return",**(await options(parent)))
                hit={"calls":0}
                async def offline(*a,**kw):
                    hit["calls"]+=1;raise AccountError("account_host_unreachable")
                runtime.executor.execute=offline
                with pytest.raises(VerbError):await runtime.operation(offline_request)
                runtime.executor.execute=execute
                assert hit["calls"]>0,"The recovery request must actually reach the offline boundary"
                status=await recovery(runtime,old);parent=status["canonical_tail"]
            if case.connection=="flap":
                dropped={"once":False}
                async def flap(*a,**kw):
                    if not dropped["once"] and a[1] in {"repin-inspect","mutation-reserve"}:
                        dropped["once"]=True;raise AccountError("account_host_unreachable")
                    return await execute(*a,**kw)
                flap_request=fresh(request_now,current,"flapped",**(await options(parent)))
                runtime.executor.execute=flap
                with pytest.raises(VerbError):await runtime.operation(flap_request)
                assert dropped["once"],"The second outage must hit the named async boundary"
                runtime.executor.execute=execute;parent=(await recovery(runtime,old))["canonical_tail"]
            result=await runtime.operation(fresh(request_now,current,"matrix-converge",**(await options(parent))))
            assert len(logins)==expected_logins
        history=getattr(runtime,"_matrix_hatch_history",None)
        if history:
            prior=await store.fetch_session(old.host,"hatch-prior-session")
            assert prior["account_binding"]==history["session"]["account_binding"]
            with pytest.raises(AccountError):
                await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
                    message={"resume_session_id":"old-native-conversation","_account_resume_binding":history["old"]})
            for binding in history.get("host_only",[]):
                assert await runtime.run(lambda a,binding=binding:a.bound(Binding(**binding).key)) is None
        final=host.registry.active(old.provider,old.alias,old.host)
        assert result["result"]["binding"]==final.public()
        assert (await recovery(runtime,old))["fence_causes"]==[]
        if need_split and case.target=="rollback-central" and case.moves and case.row!="B-hatch-G1" and not hatch:assert final.generation!=old.generation
        await store.clear_spawn_admission_hold(old.host)
        if after_check:await after_check()
        chosen=await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
            message={"account":old.alias,"_auth_context":AUTH})
        row=await store.open_session(old.host,"matrix-admitted",provider=old.provider,session_generation="matrix-final",account_binding=chosen.public())
        await runtime.admit_launch(row);admitted=chosen.public();assert chosen.key==final.key
        await checkpoint(j,"converged-and-admitted")
        outcome="passed"
    finally:
        try:evidence(case,before,await snapshot(j),outcome,admitted)
        finally:j[0].stop()
