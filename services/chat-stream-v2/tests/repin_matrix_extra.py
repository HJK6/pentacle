"""Additional public-journey matrix boundaries (no production classifier calls)."""
import copy
from dataclasses import replace
import json
import asyncio
import threading

import pytest
from provider_accounts import AccountError,Binding
from sessions import VerbError
from test_account_runtime import AUTH

ROWS=("B-U0-outcomes","B-generation-exclusion","B-guard-required","B-U1-live","B-Z-crash","B-C0","B-race-pin","B-race-origin")
VARIANTS={"B-U0-outcomes":tuple(effect+"/"+transport for effect in ("absent","started","failed","success","no-op")
    for transport in ("unreachable","timeout","ssh-auth","malformed")),
    "B-generation-exclusion":("repin-first","enrollment-first")+tuple("parked/"+kind for kind in ("N1","N2","U0","U1","S0","S1")),"B-guard-required":("missing","unset"),"B-U1-live":("stable-hold","expired-hold","replaced-hold"),"B-Z-crash":("seal-before-disposition","delayed-dispatch-after-restart"),
    "B-C0":("before-transaction","during-transaction","after-transaction","lost-reply"),
    "B-race-pin":("guard","host-selection","permit","native-result","artifact","final-host","final-cas","ancestry-inspect","zero-seal"),
    "B-race-origin":tuple(kind+"/"+boundary for kind in ("default","health","policy","auth-principal","hold-expired","hold-replaced","host-selection")
        for boundary in ("artifact","final-host")) }
EXCLUSIONS=[{"row":"B-admission","provider":"codex","consumers":["resume","restore"],
    "reason":"launch.build_launch supports process-starting resume only for Claude; assistant_restore is Claude-session-only. No new Codex resume capability is authorized."},
    {"row":"B-invalid-proof","variant":"adopted-owner","adopted":False,"reason":"An adopted ownership conflict requires an adopted Codex home."},
    {"row":"B-invalid-proof","variant":"anchor","adopted":True,"reason":"Adopted homes use their retained configured ownership rather than a generated-home registry anchor; adopted-owner is the applicable row."},
{"row":"B-generation-exclusion","variant":"enrollment-first","mode":"reconcile",
    "reason":"An earlier unresolved split already excludes ordinary enrollment; the reverse order starts without a repin attempt."},
    {"row":"B-combined-liveness","variant":"non-split","mode":"reconcile",
    "reason":"This row proves the no-split branch; reconciliation mode is independently covered by the split variant."}]

from repin_matrix_evidence import ROWS as EVIDENCE_ROWS,VARIANTS as EVIDENCE_VARIANTS
ROWS+=EVIDENCE_ROWS
VARIANTS.update(EVIDENCE_VARIANTS)
from repin_matrix_hatch import ROWS as HATCH_ROWS,VARIANTS as HATCH_VARIANTS
ROWS+=HATCH_ROWS
VARIANTS.update(HATCH_VARIANTS)
from repin_matrix_admission import ROWS as ADMISSION_ROWS,VARIANTS as ADMISSION_VARIANTS
ROWS+=ADMISSION_ROWS
VARIANTS.update(ADMISSION_VARIANTS)
from repin_matrix_topology import ROWS as TOPOLOGY_ROWS,VARIANTS as TOPOLOGY_VARIANTS
ROWS+=TOPOLOGY_ROWS
VARIANTS.update(TOPOLOGY_VARIANTS)


async def restart(j,tmp_path):
    """Reload both real stores from durable files without carrying process locks."""
    import sqlite3
    from store import Store
    from sessions import Sessions
    from account_runtime import AccountRuntime
    from account_executor import AccountExecutor
    from provider_accounts import Registry
    store,runtime,host=j[:3];database=tmp_path/"restarted-central.db"
    def persist(accounts):
        connection=sqlite3.connect(database)
        try:accounts.conn.backup(connection)
        finally:connection.close()
    await runtime.run(persist)
    assert store.stop()
    reopened=Store(str(database));reopened.start()
    resumed=AccountRuntime(reopened,Sessions(reopened,local_host="host-a"),policy=runtime.policy,clock=runtime.clock)
    resumed.attach(runtime.server,runtime.spawnctl,runtime.comms)
    resumed.executor=AccountExecutor(resumed,runtime.executor.hosts)
    host.registry=Registry(host.registry.path,host.registry.root,host.registry.adopted)
    resumed._matrix_trace=getattr(runtime,"_matrix_trace",[])
    j[0]=reopened;j[1]=resumed
    await resumed.start()
    return resumed


async def prepare(case,j,mp,current,parent,tmp_path,attempt):
    from repin_state_matrix import target_version,outage_after_admission
    from test_account_repin_state_machine import fresh,recovery
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    execute=runtime.executor.execute
    expected_logins=len(logins)
    if case.row in TOPOLOGY_ROWS:
        from repin_matrix_topology import prepare as topology_prepare
        return await topology_prepare(case,j,mp,current,parent,tmp_path,attempt)
    if case.row=="B-generation-exclusion" and case.variant.startswith("parked/"):
        from repin_matrix_hatch import prepare as state_prepare
        prepared=await state_prepare(replace(case,row="B-hatch-all-states",variant=case.variant.split("/")[1]),j,mp,current,parent,tmp_path,attempt)
        runtime._matrix_hatch_history=None
        runtime=await restart(j,tmp_path)
        current=prepared["current"];parent=prepared["parent"]
        before=list(calls);count=len(logins)
        payload={"host":old.host,"provider":old.provider,"account":old.alias,"login_generation":old.generation,
            "operation_id":"excluded-after-restart","_auth_context":AUTH}
        with pytest.raises(VerbError,match="account_repin_reconciliation_required"):
            await runtime.operation(dict(payload,action="relogin"))
        if old.provider=="codex":
            with pytest.raises(VerbError,match="account_repin_reconciliation_required"):
                await runtime.operation(dict(payload,action="upgrade",new_generation="excluded-upgrade",expected_version=cell["version"],
                    expected_executable_sha256=cell["executable_sha256"],reason="matrix exclusion"))
        assert calls==before and len(logins)==count
        for action in ("enroll","adopt"):
            with pytest.raises(AccountError,match="account_mutation_clearance_required"):
                host.handle(action,provider=old.provider,alias=old.alias,expected_generation=old.generation,
                    operation_id="excluded-helper-"+action,generation="excluded-helper-generation")
        prepared.update(hatch=False)
        return prepared
    if case.row in ADMISSION_ROWS:
        from repin_matrix_admission import prepare as admission_prepare
        return await admission_prepare(case,j,mp,current,parent,tmp_path,attempt)
    if case.row in HATCH_ROWS:
        from repin_matrix_hatch import prepare as hatch_prepare
        return await hatch_prepare(case,j,mp,current,parent,tmp_path,attempt)
    if case.row in EVIDENCE_ROWS:
        from repin_matrix_evidence import prepare as evidence_prepare
        return await evidence_prepare(case,j,mp,current,parent,tmp_path,attempt)
    if case.row=="B-race-pin":
        import account_repin_state as state
        from provider_cli_pin import set_pin
        from test_account_repin_state_machine import move_pin
        hit={"once":False,"status":0};commit=state._commit
        # Inspection and sealing require a real prior attempt. The zero node
        # is deliberately absent, while any ancestor split stays intact.
        if case.variant=="zero-seal":
            runtime.executor.execute=outage_after_admission(execute)
            with pytest.raises(VerbError):await runtime.operation(attempt)
            runtime.executor.execute=execute;parent=attempt["operation_id"]
            attempt=fresh(request,current,"pin-race-seal",reconcile_of=parent)
        if case.variant=="ancestry-inspect" and parent is None:
            runtime.executor.execute=outage_after_admission(execute)
            with pytest.raises(VerbError):await runtime.operation(attempt)
            runtime.executor.execute=execute;parent=attempt["operation_id"]
            attempt=fresh(request,current,"pin-race-inspection",reconcile_of=parent)
        actions={"guard":("recovery-status",1),"host-selection":("recovery-status",2),"permit":("mutation-reserve",0),
            "native-result":("repin-reconcile" if parent else "repin",0),"artifact":("verify-artifact",0),
            "final-host":("recovery-status",3),"ancestry-inspect":("repin-inspect",0),"zero-seal":("repin-settle",0)}
        async def raced(*args,**kwargs):
            nonlocal current
            result=await execute(*args,**kwargs)
            if args[1]=="recovery-status":hit["status"]+=1
            action,count=actions.get(case.variant,(None,None))
            if not hit["once"] and args[1]==action and (not count or hit["status"]==count):
                hit["once"]=True;current=await move_pin(runtime,current,current["revision"]+1)
            return result
        def final_cas(accounts,*args,**kwargs):
            nonlocal current
            changed=copy.deepcopy(current);changed["revision"]+=1
            set_pin(accounts,{"pin":changed,"expected_revision":current["revision"],"operation_id":"race-final-cas","reason":"fixture"})
            current=changed;hit["once"]=True
            return commit(accounts,*args,**kwargs)
        runtime.executor.execute=raced
        if case.variant=="final-cas":mp.setattr(state,"_commit",final_cas)
        try:
            with pytest.raises(VerbError):await runtime.operation(attempt)
            assert hit["once"],"Every named pin race must reach its actual boundary"
            assert await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["generation"])==old.generation
        finally:runtime.executor.execute=execute;mp.setattr(state,"_commit",commit)
        return {"current":current,"parent":(await recovery(runtime,old))["canonical_tail"],"expected_logins":expected_logins}
    if case.row=="B-race-origin":
        kind,boundary=case.variant.split("/");hit={"once":False,"status":0};auth=dict(AUTH)
        attempt=dict(attempt,_auth_context=auth);saved_registry=None
        async def change():
            nonlocal saved_registry
            if kind=="default":await runtime.run(lambda a:a.set_default(host=old.host,provider=old.provider,alias=old.alias))
            elif kind=="policy":runtime.policy["race-policy-revision"]=1
            elif kind=="health":await runtime.record_failure(old,"identity")
            elif kind=="auth-principal":auth["session_generation"]="revoked-during-proof"
            elif kind=="hold-expired":await store.set_spawn_admission_hold(old.host,held_until=runtime.clock(),reason="expired during proof")
            elif kind=="hold-replaced":await store.set_spawn_admission_hold(old.host,held_until=runtime.clock()+1400,reason="replaced during proof")
            elif kind=="host-selection":
                saved_registry=host.registry._load();changed=copy.deepcopy(saved_registry)
                key=next(k for k,v in changed["active"].items() if v==host.registry.active(old.provider,old.alias,old.host).key)
                changed["active"][key]=old.key;host.registry._save(changed)
        async def raced(*args,**kwargs):
            if args[1]=="recovery-status":hit["status"]+=1
            trigger=(args[1]=="verify-artifact" if boundary=="artifact" else args[1]=="recovery-status" and hit["status"]==3)
            # Inject selection before the final authenticated read; the host
            # reports its actual new state rather than a fabricated old reply.
            if trigger and not hit["once"] and kind=="host-selection":hit["once"]=True;await change()
            result=await execute(*args,**kwargs)
            if trigger and not hit["once"]:hit["once"]=True;await change()
            return result
        runtime.executor.execute=raced
        try:
            with pytest.raises(VerbError):await runtime.operation(attempt)
            assert hit["once"] and await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["generation"])==old.generation
        finally:
            runtime.executor.execute=execute;auth.update(AUTH)
            if saved_registry:host.registry._save(saved_registry)
        parent=(await recovery(runtime,old))["canonical_tail"]
        if kind=="health":
            revision=await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["health_revision"])
            healed=await runtime.operation({"action":"revalidate-health","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"expected_revision":revision,"operation_id":"race-health-repaired","reason":"real L-aware identity proof","_auth_context":AUTH})
            assert healed["result"]["outcome"]=="health_revalidated"
        if kind in {"hold-expired","hold-replaced"}:
            await store.set_spawn_admission_hold(old.host,held_until=runtime.clock()+1500,reason="fresh recovery after hold race")
        return {"current":current,"parent":parent,"expected_logins":expected_logins,"reanchor":kind in {"health","default","policy"}}
    if case.row=="B-Z-crash":
        import account_repin_state as state
        from account_repin import _host_id
        from provider_account_mutation import read
        settle=state._settle;captured=[]
        def crash(*args,**kwargs):raise AccountError("fixture_crash_after_durable_seal")
        async def delayed(*args,**kwargs):
            if args[1] in {"repin","repin-reconcile"}:
                captured.append((args,kwargs));raise AccountError("account_host_unreachable")
            return await execute(*args,**kwargs)
        mp.setattr(state,"_settle",crash);runtime.executor.execute=delayed
        try:
            with pytest.raises(VerbError):await runtime.operation(attempt)
            assert len(captured)==1 and read(host,"seals",_host_id(attempt))
        finally:mp.setattr(state,"_settle",settle);runtime.executor.execute=execute
        before=list(native)
        runtime=await restart(j,tmp_path);execute=runtime.executor.execute
        with pytest.raises(AccountError):await execute(*captured[0][0],**captured[0][1])
        assert native==before and (await recovery(runtime,old))["canonical_tail"]==attempt["operation_id"]
        return {"current":current,"parent":attempt["operation_id"],"expected_logins":expected_logins}
    if case.row=="B-C0":
        import account_repin_state as state
        from account_store import AccountStore
        commit=state._commit;register=AccountStore._register;hit={"once":False}
        def boundary(*args,**kwargs):
            if case.variant=="before-transaction":
                hit["once"]=True;raise AccountError("fixture_crash_before_commit")
            result=commit(*args,**kwargs)
            hit["once"]=True;raise AccountError("fixture_lost_committed_reply")
        def partial(accounts,binding):
            result=register(accounts,binding)
            if binding.key!=old.key:
                hit["once"]=True;raise AccountError("fixture_crash_during_commit")
            return result
        if case.variant=="during-transaction":mp.setattr(AccountStore,"_register",partial)
        elif case.variant!="lost-reply":mp.setattr(state,"_commit",boundary)
        try:
            if case.variant=="lost-reply":
                completed=await runtime.operation(attempt);hit["once"]=True
            else:
                with pytest.raises(VerbError):await runtime.operation(attempt)
            assert hit["once"]
            selected=await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["generation"])
            if case.variant in {"before-transaction","during-transaction"}:
                assert selected==old.generation
                assert await runtime.run(lambda a:a.bound(host.registry.active(old.provider,old.alias,old.host).key)) is None
            else:assert selected==host.registry.active(old.provider,old.alias,old.host).generation
        finally:mp.setattr(state,"_commit",commit);mp.setattr(AccountStore,"_register",register)
        runtime=await restart(j,tmp_path)
        before=sum("Reply exactly OK" in argv for argv in native)
        completed=await runtime.operation(attempt)
        assert sum("Reply exactly OK" in argv for argv in native)==before
        return {"current":current,"parent":None,"expected_logins":expected_logins,"historical":(attempt,completed)}
    if case.row=="B-U1-live":
        # A real host executor owns the registry flock while paused after its
        # started journal. The coordinator loses its reply, not the worker.
        current=await target_version(j,current,"8.8.5",tmp_path,"live-worker-target")
        attempt=fresh(request,current,"live-attempt",**({"reconcile_of":parent} if parent else {}))
        host.separate_dispatch_registries=True
        entered=threading.Event();release=threading.Event();finished=[];failed=[]
        save=host._save_operation;workers=[];dispatched={"once":False}
        def paused(operation,data):
            save(operation,data)
            if data.get("kind")=="repin" and data.get("state")=="started":
                entered.set()
                if not release.wait(15):raise AssertionError("owned live-worker barrier timed out")
        host._save_operation=paused
        def worker(args,kwargs):
            try:finished.append(asyncio.run(execute(*args,**kwargs)))
            except BaseException as exc:failed.append(exc)
        async def detached(*args,**kwargs):
            if args[1] in {"repin","repin-reconcile"} and not dispatched["once"]:
                dispatched["once"]=True
                thread=threading.Thread(target=worker,args=(args,kwargs),name="repin-owned-live-worker")
                workers.append(thread);thread.start()
                assert await asyncio.to_thread(entered.wait,5),"Real target must own the host lock"
                raise AccountError("account_host_unreachable")
            return await execute(*args,**kwargs)
        runtime.executor.execute=detached
        try:
            with pytest.raises(VerbError):await runtime.operation(attempt)
            assert entered.is_set() and workers[0].is_alive()
            runtime.executor.execute=execute
            turn_count=sum("Reply exactly OK" in argv for argv in native)
            before=host.registry.active(old.provider,old.alias,old.host)
            current=await __import__('repin_state_matrix').stage(j,case,current,before,tmp_path)
            if case.variant=="expired-hold":
                await store.set_spawn_admission_hold(old.host,held_until=runtime.clock(),reason="expired while publisher live")
            elif case.variant=="replaced-hold":
                await store.set_spawn_admission_hold(old.host,held_until=runtime.clock()+1200,reason="replacement while publisher live")
            for address in (None,attempt["operation_id"]):
                payload=fresh(request,current,"live-sibling-"+str(address),**({"reconcile_of":address} if address else {}))
                with pytest.raises(VerbError):await runtime.operation(payload)
            status=await recovery(runtime,old)
            hatch={"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"operation_id":"live-hatch","supersede_repin":status["snapshot_digest"],"_auth_context":AUTH}
            with pytest.raises(VerbError):await runtime.operation(hatch)
            assert sum("Reply exactly OK" in argv for argv in native)==turn_count and len(logins)==expected_logins
            assert host.registry.active(old.provider,old.alias,old.host).public()==before.public()
        finally:
            release.set()
            for thread in workers:await asyncio.to_thread(thread.join,5)
            host._save_operation=save;runtime.executor.execute=execute
            assert all(not thread.is_alive() for thread in workers),"Owned publisher must be drained"
        assert not failed and len(finished)==1
        assert sum("Reply exactly OK" in argv for argv in native)==turn_count+1
        # A failed hatch before sealing may reserve an intent; use its fresh
        # snapshot-authorized continuation rather than bypassing that intent.
        await store.set_spawn_admission_hold(old.host,held_until=runtime.clock()+1300,reason="drained worker recovery")
        status=await recovery(runtime,old)
        assert status["fence_causes"]
        return {"current":current,"parent":attempt["operation_id"],"expected_logins":expected_logins,"staged":True}
    if case.row=="B-guard-required":
        if case.variant=="missing":host.config.pop("mutation_clearance",None)
        else:host.config["mutation_clearance"]="unset"
        baseline=await runtime.run(lambda a:a.conn.execute("SELECT count(*) FROM v2_account_recovery").fetchone()[0])
        try:
            with pytest.raises(VerbError,match="account_mutation_clearance_required"):await runtime.operation(attempt)
            assert await runtime.run(lambda a:a.conn.execute("SELECT count(*) FROM v2_account_recovery").fetchone()[0])==baseline
            if parent:
                status=await recovery(runtime,old)
                with pytest.raises(VerbError,match="account_mutation_clearance_required"):
                    await runtime.operation({"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                        "login_generation":old.generation,"operation_id":"guardless-hatch","supersede_repin":status["snapshot_digest"],"_auth_context":AUTH})
            assert len(logins)==expected_logins
        finally:host.config["mutation_clearance"]="required"
        return {"current":current,"parent":parent,"expected_logins":expected_logins}
    if case.row=="B-U0-outcomes":
        effect,transport=case.variant.split("/")
        if effect=="no-op" and parent is None:
            await runtime.operation(request)
            selected=host.registry.active(old.provider,old.alias,old.host)
            attempt=fresh(dict(request,login_generation=selected.generation),current,"lost-noop")
        elif parent and effect in {"started","failed"}:
            current=await target_version(j,current,"8.8.6",tmp_path,"transport-target")
            attempt=fresh(request,current,"transport-fault",reconcile_of=parent)
        before=host.registry.active(old.provider,old.alias,old.host)
        boundary={"offline":False,"hit":False}
        original=runtime.executor.hosts.run_command;save=host._save_operation;observe=host.observation
        def started(op,data):
            save(op,data)
            if data.get("state")=="started" and data.get("kind") in {"repin","repin-reconcile"}:
                raise AccountError("account_host_unreachable")
        def failed(_):raise AccountError("provider_transport_failed")
        if effect=="started":host._save_operation=started
        if effect=="failed":host.observation=failed
        async def broken(hostname,*argv,timeout):
            if boundary["offline"]:raise OSError("fixture offline")
            if argv[2] in {"repin","repin-reconcile"}:
                boundary.update(hit=True,offline=True)
                if effect!="absent":await original(hostname,*argv,timeout=timeout)
                if transport=="timeout":raise TimeoutError()
                if transport=="ssh-auth":return 255,json.dumps({"outcome":"account_host_unreachable"})
                if transport=="malformed":return 0,json.dumps([])
                raise OSError("fixture unreachable")
            return await original(hostname,*argv,timeout=timeout)
        runtime.executor.hosts.run_command=broken
        try:
            with pytest.raises(VerbError):await runtime.operation(attempt)
            assert boundary["hit"],"The named transport boundary must actually execute"
            status=await recovery(runtime,old)
            cause=next(c for c in status["fence_causes"] if c["operation_id"]==attempt["operation_id"])
            assert cause["cause"]=="uncertain","An unauthenticated/malformed outcome is U0 until inspected"
            if effect in {"absent","started","failed","no-op"}:
                assert host.registry.active(old.provider,old.alias,old.host).public()==before.public()
        finally:
            host._save_operation=save;host.observation=observe;runtime.executor.hosts.run_command=original
        return {"current":current,"parent":attempt["operation_id"],"expected_logins":expected_logins}
    if case.row=="B-generation-exclusion":
        if case.variant=="repin-first":
            runtime.executor.execute=outage_after_admission(execute)
            with pytest.raises(VerbError):await runtime.operation(attempt)
            runtime.executor.execute=execute;parent=attempt["operation_id"]
            before=list(calls)
            payload={"host":old.host,"provider":old.provider,"account":old.alias,"login_generation":old.generation,
                "operation_id":"excluded-enrollment","_auth_context":AUTH}
            with pytest.raises(VerbError,match="account_repin_reconciliation_required"):
                await runtime.operation(dict(payload,action="relogin"))
            if old.provider=="codex":
                with pytest.raises(VerbError,match="account_repin_reconciliation_required"):
                    await runtime.operation(dict(payload,action="upgrade",new_generation="excluded",expected_version=cell["version"],
                        expected_executable_sha256=cell["executable_sha256"],reason="matrix"))
            assert calls==before
            for action in ("enroll","adopt"):
                with pytest.raises(AccountError,match="account_mutation_clearance_required"):
                    host.handle(action,provider=old.provider,alias=old.alias,expected_generation=old.generation,
                        operation_id="no-coordinator-"+action,generation="no-clearance")
        else:
            observe=host._probe_observation
            def expired(_):raise AccountError("provider_auth_failed")
            host._probe_observation=expired;delayed=[]
            async def pause(*a,**kw):
                if a[1]=="enroll":delayed.append((a,kw));raise AccountError("account_host_unreachable")
                return await execute(*a,**kw)
            runtime.executor.execute=pause
            payload={"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"operation_id":"pending-enrollment","_auth_context":AUTH}
            result=await runtime.operation(payload)
            assert result["result"]["outcome"]=="execution_unresolved" and len(delayed)==1
            before=list(calls);count=await runtime.run(lambda a:a.conn.execute("SELECT count(*) FROM v2_account_recovery WHERE binding_key LIKE 'repin:%'").fetchone()[0])
            with pytest.raises(VerbError):await runtime.operation(request)
            assert calls==before and await runtime.run(lambda a:a.conn.execute("SELECT count(*) FROM v2_account_recovery WHERE binding_key LIKE 'repin:%'").fetchone()[0])==count
            runtime.executor.execute=execute
            await execute(*delayed[0][0],**delayed[0][1])
            imported=await runtime.operation(payload)
            assert imported["result"]["outcome"]=="recovered"
            host._probe_observation=observe;expected_logins+=1
        return {"current":current,"parent":parent,"expected_logins":expected_logins}
    raise AssertionError("unimplemented extra row "+case.row)
