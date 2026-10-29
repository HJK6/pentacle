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

CELLS=tuple((p,s,a) for p,s in (("codex","macos"),("codex","wsl"),("claude","macos"),("claude","wsl")) for a in ("repin","sai"))
PINS=((0,"identical"),)+tuple(itertools.product((1,3),("identical","other-provider","version","native","resources","rollback-central","rollback-tip")))
CONNECTIVITY=("online","return","flap")
MODES=("root","reconcile")
# Each row is a genuine public journey. Specialized race/identity/control rows
# in test_account_repin_state_machine.py supply the remaining B4/B5 transitions.
ROWS=("B-N1-verifier","B-N1-unknown","B-N2-before","B-N2-turn","B-N2-after","B-N2-artifact","B-N2-registry",
    "B-U0-absent","B-U0-success","B-U1-dead","B-Z-late","B-N3","B-S0","B-S1-absent","B-S1-failed","B-S1-success","B-C0","B-hatch-G1")


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
    @property
    def id(self):return ":".join(map(str,(self.row,self.provider,self.platform,self.alias,self.mode,self.moves,self.target,self.connection)))


def cases(row):
    for c,m,p,n in itertools.product(CELLS,MODES,PINS,CONNECTIVITY):yield Case(row,*c,m,*p,n)


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
        "minimal_turns":sum("Reply exactly OK" in v for v in native),"signins":len(logins),"dispatch":list(calls)}


def evidence(case,before,after,outcome,admission=None):
    root=os.environ.get("PENTACLE_GATE_EVIDENCE_DIR")
    if not root:return
    path=Path(root)/"state-machine-cases";path.mkdir(exist_ok=True)
    data={"case_id":case.id,"axes":case.__dict__,"before":before,"after":after,"outcome":outcome,"admission":admission}
    (path/(hashlib.sha256(case.id.encode()).hexdigest()+".json")).write_text(json.dumps(data,sort_keys=True,default=str))


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
        if case.target=="version":entry["version"]="9.9.8"
        for artifact in entry["artifacts"].values():artifact.update(native_sha256=native_hash,resource_sha256=resource_hash)
        cell.update(executable=str(binary),executable_sha256=native_hash,version=entry["version"],resource_root=str(resource))
        roots[str(binary)]=str(resource)
    elif case.target=="other-provider":target["providers"]["claude" if old.provider=="codex" else "codex"]["version"]="9.9.7"
    for index in range(case.moves):
        # Several moves explicitly include A→B→A bytes at increasing revisions.
        chosen=copy.deepcopy(target if index==case.moves-1 or index%2==0 else current)
        chosen["revision"]=current["revision"]+1
        await runtime.operation({"action":"pin-set","pin":chosen,"expected_revision":current["revision"],
            "operation_id":"matrix-move-"+str(chosen["revision"]),"reason":"table fixture","_auth_context":AUTH})
        current=chosen
    return current


async def run(case,tmp_path,mp):
    from test_account_repin import host_journey
    from test_account_repin_state_machine import move_pin,fresh,recovery
    j=await host_journey(tmp_path,mp,case.provider,case.platform,alias=case.alias)
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    before=await snapshot(j);outcome="failed";admitted=None
    execute=runtime.executor.execute;original_run=host.run;observe=host.observation
    import provider_cli_artifact
    verify=provider_cli_artifact.verify;registry_repin=host.registry.repin
    current=pin;parent=None;attempt=request;late_dispatch=None
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
    try:
        need_split=case.mode=="reconcile" or case.row in {"B-S0","B-S1-absent","B-S1-failed","B-S1-success","B-hatch-G1"}
        if need_split:await split_root()
        if need_split and (case.row.startswith("B-N2") or case.row in {"B-S1-failed","B-S1-success"}):
            # A target-changing wrapper really enters its native proof phases.
            changed=copy.deepcopy(current);changed["revision"]+=1;changed["providers"][old.provider]["version"]="8.8.8"
            cell["version"]="8.8.8"
            await runtime.operation({"action":"pin-set","pin":changed,"expected_revision":current["revision"],
                "operation_id":"force-target","reason":"table fixture","_auth_context":AUTH});current=changed
        if parent:attempt=fresh(request,current,"matrix-fault",reconcile_of=parent)
        if case.row in {"B-S0","B-hatch-G1"}:
            pass
        elif case.row in {"B-N1-verifier","B-N1-unknown"}:
            def fail(*a,**kw):raise AccountError("provider_cli_pin_mismatch" if case.row=="B-N1-verifier" else "novel_authenticated_refusal")
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
            async def offline(*a,**kw):raise AccountError("account_host_unreachable")
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
        runtime.executor.execute=execute;host.run=original_run;host.observation=observe;host.registry.repin=registry_repin
        mp.setattr(provider_cli_artifact,"verify",verify)
        if late_dispatch and case.row=="B-Z-late":
            baseline=list(native)
            with pytest.raises(AccountError,match="account_operation_sealed"):
                await execute(*late_dispatch[0],**late_dispatch[1])
            assert native==baseline
        tip=host.registry.active(old.provider,old.alias,old.host)
        current=await stage(j,case,current,tip,tmp_path)
        if case.row=="B-hatch-G1":
            runtime.policy["watchers"]={"host-a:fixture-watcher":{"host":old.host}}
            await runtime.operation({"action":"observe-failure","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"kind":"auth","_auth_context":{"service_authenticated":True,"service_actor":"host-a:fixture-watcher"}})
            status=await recovery(runtime,old)
            payload={"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"operation_id":"matrix-hatch","supersede_repin":status["snapshot_digest"],"_auth_context":AUTH}
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
            assert result["result"]["binding"]["home"]!=old.home and len(logins)==2
        else:
            # Construct a real outage at this exact pending/effect boundary.
            if case.connection!="online":
                async def offline(*a,**kw):raise AccountError("account_host_unreachable")
                runtime.executor.execute=offline
                with pytest.raises(VerbError):
                    await runtime.operation(fresh(request,current,"offline-return",**({"reconcile_of":parent} if parent else {})))
                runtime.executor.execute=execute
                status=await recovery(runtime,old)
                parent=status["canonical_tail"]
            selected=await runtime.run(lambda a:Binding(**json.loads(a._cell(old.host,old.provider,old.alias)["data"])))
            request_now=dict(request,login_generation=selected.generation)
            if case.connection=="flap":
                dropped={"once":False}
                async def flap(*a,**kw):
                    if not dropped["once"] and a[1] in {"repin-inspect","mutation-reserve"}:
                        dropped["once"]=True;raise AccountError("account_host_unreachable")
                    return await execute(*a,**kw)
                runtime.executor.execute=flap
                with pytest.raises(VerbError):await runtime.operation(fresh(request_now,current,"flapped",**({"reconcile_of":parent} if parent else {})))
                runtime.executor.execute=execute;parent=(await recovery(runtime,old))["canonical_tail"]
            result=await runtime.operation(fresh(request_now,current,"matrix-converge",**({"reconcile_of":parent} if parent else {})))
            assert len(logins)==1
        final=host.registry.active(old.provider,old.alias,old.host)
        assert result["result"]["binding"]==final.public()
        assert (await recovery(runtime,old))["fence_causes"]==[]
        if need_split and case.target=="rollback-central" and case.moves and case.row!="B-hatch-G1":assert final.generation!=old.generation
        await store.clear_spawn_admission_hold(old.host)
        chosen=await runtime.choose_launch(host=old.host,provider=old.provider,model="fixture",effort="high",
            message={"account":old.alias,"_auth_context":AUTH})
        row=await store.open_session(old.host,"matrix-admitted",provider=old.provider,session_generation="matrix-final",account_binding=chosen.public())
        await runtime.admit_launch(row);admitted=chosen.public();assert chosen.key==final.key
        outcome="passed"
    finally:
        try:evidence(case,before,await snapshot(j),outcome,admitted)
        finally:store.stop()
