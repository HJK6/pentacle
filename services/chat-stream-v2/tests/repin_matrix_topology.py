"""Attempt topology, reservation races and explicit origin refusal products."""
import asyncio
import copy
import json
from dataclasses import asdict,replace

import pytest
from provider_accounts import AccountError
from sessions import VerbError
from test_account_runtime import AUTH

ROWS=("B-fence-topology","B-origin-refusals")
VARIANTS={"B-fence-topology":("cycle","cross-cell","fork","competing-child","consumed"),
    "B-origin-refusals":("missing-digest","wrong-digest","identity-unhealthy","auth-unhealthy","disabled","policy-off","unmanaged","changed-central")}


async def prepare(case,j,mp,current,parent,tmp_path,attempt):
    from test_account_repin_state_machine import fresh,recovery,move_pin
    from account_repin import _key,_payload,_read,_write
    store,runtime,host,old,request,calls,native,logins,cell,pin=j
    execute=runtime.executor.execute;expected_logins=len(logins)
    if parent is None:
        async def split(*args,**kwargs):
            nonlocal current
            result=await execute(*args,**kwargs)
            if args[1]=="repin":current=await move_pin(runtime,current,current["revision"]+1)
            return result
        runtime.executor.execute=split
        with pytest.raises(VerbError):await runtime.operation(request)
        runtime.executor.execute=execute;parent=request["operation_id"]
    if case.row=="B-fence-topology":
        if case.variant in {"competing-child","consumed"}:
            first=fresh(request,current,"topology-winner",reconcile_of=parent)
            if case.variant=="competing-child":
                second=fresh(request,current,"topology-competitor",reconcile_of=parent)
                results=await asyncio.gather(runtime.operation(first),runtime.operation(second),return_exceptions=True)
                assert sum(isinstance(r,dict) and r.get("result",{}).get("outcome")=="repinned" for r in results)==1
                assert sum(isinstance(r,VerbError) for r in results)==1
            else:
                selected=(await runtime.operation(first))["result"]["binding"]
                replay=fresh(dict(request,login_generation=selected["generation"]),current,"consumed-parent",reconcile_of=parent)
                before=list(native)
                with pytest.raises(VerbError):await runtime.operation(replay)
                assert native==before
            return {"current":current,"parent":None,"expected_logins":expected_logins}
        backups=await runtime.run(lambda a:[dict(row) for row in a.conn.execute("SELECT * FROM v2_account_recovery")])
        def inject(accounts):
            original=_read(accounts,_payload(request))
            if case.variant=="cycle":
                original["arguments"]["reconcile_of"]=request["operation_id"]
                _write(accounts,original["arguments"],original)
            elif case.variant=="cross-cell":
                other=copy.deepcopy(original);other["arguments"].update(account="other-cell",operation_id="foreign-parent")
                _write(accounts,other["arguments"],other)
            else:
                for ident in ("fork-one","fork-two"):
                    child=copy.deepcopy(original);child["arguments"].update(operation_id=ident,reconcile_of=parent)
                    _write(accounts,child["arguments"],child)
            accounts.conn.commit()
        await runtime.run(inject)
        before=list(native)
        try:
            with pytest.raises(VerbError):
                await runtime.operation(fresh(request,current,"invalid-topology",reconcile_of="foreign-parent" if case.variant=="cross-cell" else parent))
            assert native==before and (await recovery(runtime,old))["fence_causes"]
        finally:
            def restore(accounts):
                # Only this isolated fixture's injected rows are reverted.
                accounts.conn.execute("DELETE FROM v2_account_recovery")
                accounts.conn.executemany("INSERT INTO v2_account_recovery VALUES (?,?,?)",[(r["binding_key"],r["state"],r["data"]) for r in backups])
                accounts.conn.commit()
            await runtime.run(restore)
        return {"current":current,"parent":parent,"expected_logins":expected_logins}
    # Force a real monotonic metadata change: even restoring a default cannot
    # silently make the historical origin current again.
    await runtime.run(lambda a:a.set_default(host=old.host,provider=old.provider,alias=old.alias))
    saved_policy=copy.deepcopy(runtime.policy)
    central=await runtime.run(lambda a:dict(a._cell(old.host,old.provider,old.alias)))
    if case.variant in {"identity-unhealthy","auth-unhealthy"}:await runtime.record_failure(old,"identity" if case.variant=="identity-unhealthy" else "auth")
    elif case.variant in {"disabled","changed-central"}:
        def alter(accounts):
            value=json.loads(central["data"])
            value["enabled" if case.variant=="disabled" else "generation"]=False if case.variant=="disabled" else "out-of-protocol"
            accounts.conn.execute("UPDATE v2_account_bindings SET data=? WHERE binding_key=?",(json.dumps(value),old.key));accounts.conn.commit()
        await runtime.run(alter)
    elif case.variant=="policy-off":runtime.policy["enabled"]=False
    elif case.variant=="unmanaged":runtime.policy["managed_hosts"]=[]
    status=await recovery(runtime,old)
    payload=fresh(request,current,"origin-refusal",reconcile_of=parent)
    if case.variant!="missing-digest":payload["expected_origin_digest"]="f"*64 if case.variant=="wrong-digest" else status["origin_digest"]
    before=list(native)
    try:
        with pytest.raises(VerbError):await runtime.operation(payload)
        assert native==before and (await recovery(runtime,old))["fence_causes"]
    finally:
        runtime.policy.clear();runtime.policy.update(saved_policy)
        if case.variant in {"disabled","changed-central"}:
            def restore(accounts):
                accounts.conn.execute("UPDATE v2_account_bindings SET data=? WHERE binding_key=?",(central["data"],old.key));accounts.conn.commit()
            await runtime.run(restore)
    parent=(await recovery(runtime,old))["canonical_tail"]
    if case.variant=="identity-unhealthy":
        revision=await runtime.run(lambda a:a._cell(old.host,old.provider,old.alias)["health_revision"])
        healed=await runtime.operation({"action":"revalidate-health","host":old.host,"provider":old.provider,"account":old.alias,
            "login_generation":old.generation,"expected_revision":revision,"operation_id":"origin-refusal-revalidated","reason":"real read-only lineage identity","_auth_context":AUTH})
        assert healed["result"]["outcome"]=="health_revalidated"
    return {"current":current,"parent":parent,"expected_logins":expected_logins,"reanchor":True,
        "hatch":case.variant=="auth-unhealthy","hatch_signins":expected_logins+1}
