"""Universal hatch exits from real parked account-operation states."""
import copy
from dataclasses import asdict

import pytest
from provider_accounts import AccountError
from sessions import VerbError
from test_account_runtime import AUTH

ROWS=("B-hatch-all-states","B-hatch-prior-findings","B-hatch-races","B-hatch-refusals")
VARIANTS={"B-hatch-all-states":("N1","N2","N3","U0","U1","S0","S1","X-missing","X-fork","X-origin","sticky-auth"),
    "B-hatch-prior-findings":("R1","R2","F1-default","F1-health","F1-policy","F2","F3","combined")}

from repin_matrix_hatch_boundaries import VARIANTS as BOUNDARY_VARIANTS
VARIANTS.update(BOUNDARY_VARIANTS)


async def prepare(case,j,mp,current,parent,tmp_path,attempt):
    from repin_state_matrix import target_version,outage_after_admission
    from test_account_repin_state_machine import fresh,recovery,move_pin
    import account_repin_state as state
    import provider_cli_artifact
    from account_repin import _host_id,_key,_read,_write,_payload
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    execute=runtime.executor.execute;save=host._save_operation;observe=host._probe_observation
    verify=provider_cli_artifact.verify;settle=state._settle;expected_logins=len(logins)
    historical=await store.open_session(old.host,"hatch-prior-session",provider=old.provider,
        session_generation="hatch-old-attribution",account_binding=old.public())
    runtime._matrix_hatch_history={"session":historical,"old":old.public()}
    variant=case.variant
    split_kind=case.row in {"B-hatch-races","B-hatch-refusals"} or variant in {"S0","S1","X-missing","X-fork","X-origin","R2","F1-default","F1-health","F1-policy","F2","F3","combined","sticky-auth"}
    if split_kind and parent is None:
        async def split(*args,**kwargs):
            nonlocal current
            result=await execute(*args,**kwargs)
            if args[1]=="repin":current=await move_pin(runtime,current,current["revision"]+1)
            return result
        runtime.executor.execute=split
        with pytest.raises(VerbError):await runtime.operation(request)
        runtime.executor.execute=execute;parent=request["operation_id"]
    if variant in {"S1","R2","F3","combined"}:
        runtime.executor.execute=outage_after_admission(execute)
        node=fresh(request,current,"hatch-zero-wrapper",reconcile_of=parent)
        with pytest.raises(VerbError):await runtime.operation(node)
        runtime.executor.execute=execute;parent=node["operation_id"]
        if variant in {"F3","combined"}:
            # A crash after settlement keeps the reserved child as canonical
            # tail, even though its parent acquired a durable zero disposition.
            async def stop_new(*args,**kwargs):
                if args[1] in {"mutation-reserve","repin","repin-reconcile"}:raise AccountError("account_host_unreachable")
                return await execute(*args,**kwargs)
            runtime.executor.execute=stop_new
            child=fresh(request,current,"hatch-after-settled-tail",reconcile_of=parent)
            with pytest.raises(VerbError):await runtime.operation(child)
            runtime.executor.execute=execute;parent=child["operation_id"]
    if variant in {"N1","N2","U0","U1","R1"}:
        if parent:
            current=await target_version(j,current,"8.8.3",tmp_path,"hatch-fault-target")
        node=fresh(request,current,"hatch-fault",**({"reconcile_of":parent} if parent else {}))
        # Fault after the durable host seal prevents the original coordinator
        # from recording its Z disposition, preserving the named unresolved row.
        def crash(*args,**kwargs):raise AccountError("fixture_central_crash_before_disposition")
        mp.setattr(state,"_settle",crash)
        def refused(*args,**kwargs):raise AccountError("novel_authenticated_refusal")
        def failed(_):raise AccountError("provider_transport_failed")
        def stopped(operation,data):
            save(operation,data)
            if data.get("state")=="started" and data.get("kind")=="repin":raise AccountError("account_host_unreachable")
        if variant in {"N1","R1"}:mp.setattr(provider_cli_artifact,"verify",refused)
        elif variant=="N2":host._probe_observation=failed
        elif variant=="U0":runtime.executor.execute=outage_after_admission(execute)
        elif variant=="U1":host._save_operation=stopped
        try:
            with pytest.raises(VerbError):await runtime.operation(node)
        finally:
            mp.setattr(state,"_settle",settle);mp.setattr(provider_cli_artifact,"verify",verify)
            host._probe_observation=observe;host._save_operation=save;runtime.executor.execute=execute
        parent=node["operation_id"]
    elif variant=="N3":
        # First agree the current pair, then lose a genuinely successful no-op.
        agreed=await runtime.operation(fresh(request,current,"before-hatch-noop",**({"reconcile_of":parent} if parent else {})))
        selected=agreed["result"]["binding"]
        node=fresh(dict(request,login_generation=selected["generation"]),current,"hatch-noop-lost")
        lost={"offline":False}
        async def drop(*args,**kwargs):
            if lost["offline"]:raise AccountError("account_host_unreachable")
            result=await execute(*args,**kwargs)
            if args[1]=="repin":lost["offline"]=True;raise AccountError("account_host_unreachable")
            return result
        runtime.executor.execute=drop
        with pytest.raises(VerbError):await runtime.operation(node)
        runtime.executor.execute=execute;parent=node["operation_id"]
    if variant=="X-missing":
        # Old evidence is genuinely unrecoverable; hatch must not depend on it.
        (host.journal/(_host_id(request)+".json")).unlink()
    elif variant=="X-fork":
        def fork(accounts):
            original=_read(accounts,_payload(request))
            broken=copy.deepcopy(original);broken["arguments"]["operation_id"]="untrusted-fork"
            broken["arguments"]["reconcile_of"]=request["operation_id"]
            _write(accounts,broken["arguments"],broken)
            sibling=copy.deepcopy(broken);sibling["arguments"]["operation_id"]="untrusted-fork-sibling"
            _write(accounts,sibling["arguments"],sibling)
            accounts.conn.commit()
        await runtime.run(fork)
    if variant in {"X-origin","F1-default","combined"}:
        await runtime.run(lambda a:a.set_default(host=old.host,provider=old.provider,alias=old.alias))
    if variant in {"F1-health","combined","sticky-auth"}:
        await runtime.record_failure(old,"auth")
        if variant=="sticky-auth":
            def asserted_healthy(accounts):
                accounts.conn.execute("UPDATE v2_account_bindings SET health='healthy' WHERE binding_key=?",(old.key,));accounts.conn.commit()
            await runtime.run(asserted_healthy)
            status=await recovery(runtime,old)
            with pytest.raises(VerbError):
                await runtime.operation(fresh(request,current,"sticky-auth-inplace",reconcile_of=status["canonical_tail"],expected_origin_digest=status["origin_digest"]))
            parent=(await recovery(runtime,old))["canonical_tail"]
    if variant in {"F1-policy","combined"}:runtime.policy["hatch-policy-revision"]=1
    if variant in {"F2","combined"}:
        before=list(calls)
        with pytest.raises(VerbError,match="account_repin_reconciliation_required"):
            await runtime.operation({"action":"relogin","host":old.host,"provider":old.provider,"account":old.alias,
                "login_generation":old.generation,"operation_id":"hatch-excluded-relogin","_auth_context":AUTH})
        assert calls==before
    status=await recovery(runtime,old)
    assert status["fence_causes"] and status["snapshot_digest"]
    runtime._matrix_hatch_history["host_only"]=[v for key,v in host.registry._load()["generations"].items()
        if await runtime.run(lambda a,key=key:a.bound(key)) is None]
    result={"current":current,"parent":parent,"expected_logins":expected_logins,
        "hatch":True,"hatch_signins":expected_logins+1}
    if case.row in BOUNDARY_VARIANTS:
        from repin_matrix_hatch_boundaries import prepare as boundary_prepare
        return await boundary_prepare(case,j,mp,result,tmp_path)
    return result
