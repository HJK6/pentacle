"""Public-contract fail-first probes; baseline absence is never a safety pass."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest
from provider_accounts import AccountError
from provider_cli_pin import digest
from sessions import VerbError
from test_account_repin import host_journey
from test_account_runtime import AUTH

CELLS=[(p,s,a) for p in ("codex","claude") for s in ("macos","wsl") for a in ("ours","sai")]


async def status(runtime,old):
    return await runtime.operation({"action":"status","host":old.host,"provider":old.provider,"account":old.alias,"_auth_context":AUTH})


async def split(runtime,request,pin):
    current=copy.deepcopy(pin);current["revision"]+=1;execute=runtime.executor.execute
    async def moved(*args,**kwargs):
        result=await execute(*args,**kwargs)
        if args[1]=="repin":
            await runtime.operation({"action":"pin-set","pin":current,"expected_revision":pin["revision"],
                "operation_id":"protocol-split-pin","reason":"protocol feature probe","_auth_context":AUTH})
        return result
    runtime.executor.execute=moved
    try:
        with pytest.raises(VerbError):await runtime.operation(request)
    finally:runtime.executor.execute=execute
    return current


def record(name,old,request,response,error,before,after):
    root=os.environ.get("PENTACLE_GATE_EVIDENCE_DIR")
    if not root:return
    path=Path(root)/"public-protocol-probes";path.mkdir(exist_ok=True)
    data={"feature":name,"cell":[old.provider,old.platform,old.alias],"request":request,"response":response,
        "error":error,"before":before,"after":after,"scope":"public contract only; no claim of baseline reachability of new internal boundaries"}
    (path/(name+"-"+old.provider+"-"+old.platform+"-"+old.alias+".json")).write_text(json.dumps(data,sort_keys=True,default=str))


@pytest.mark.parametrize("feature",["status","origin-reanchor","fresh-hatch","host-status","mutation-clearance"])
@pytest.mark.parametrize("provider,platform,alias",CELLS)
def test_required_public_recovery_protocol(tmp_path,monkeypatch,feature,provider,platform,alias):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin=await host_journey(tmp_path,monkeypatch,provider,platform,alias=alias,transport=True)
        result=None;error=None;payload=None
        try:
            current=pin
            if feature in {"origin-reanchor","fresh-hatch"}:
                current=await split(runtime,request,pin)
                if feature=="origin-reanchor":await runtime.run(lambda a:a.set_default(host=old.host,provider=old.provider,alias=old.alias))
                else:
                    runtime.policy["watchers"]={"host-a:fixture-watcher":{"host":old.host}}
                    await runtime.operation({"action":"observe-failure","host":old.host,"provider":old.provider,"account":old.alias,
                        "login_generation":old.generation,"kind":"auth","_auth_context":{"service_authenticated":True,"service_actor":"host-a:fixture-watcher"}})
            public=await status(runtime,old)
            entries=public.get("recovery")
            entry=entries[0] if isinstance(entries,list) and entries else None
            # Baseline lacks this protocol. A syntactically valid proposed
            # snapshot lets its REAL request parser report feature absence;
            # it is explicitly not claimed to be a baseline approval receipt.
            proposal=digest({"public_status":public,"host_selection":host.registry.active(provider,alias,old.host).public(),"pin":current})
            before={"signins":len(logins),"host":host.registry.active(provider,alias,old.host).public(),"native_calls":len(native)}
            if feature=="status":payload={"action":"status","host":old.host,"provider":provider,"account":alias,"_auth_context":AUTH}
            elif feature=="origin-reanchor":
                payload=dict(request,operation_id="protocol-reanchor",new_generation="protocol-current",reconcile_of=request["operation_id"],
                    expected_pin_revision=current["revision"],expected_pin_digest=digest(current),
                    expected_origin_digest=entry["origin_digest"] if entry else proposal)
            elif feature=="fresh-hatch":
                payload={"action":"relogin","host":old.host,"provider":provider,"account":alias,"login_generation":old.generation,
                    "operation_id":"protocol-hatch","supersede_repin":entry["snapshot_digest"] if entry else proposal,"_auth_context":AUTH}
            else:
                payload={"host_action":"recovery-status" if feature=="host-status" else "enroll","provider":provider,"account":alias,
                    "login_generation":old.generation,"operation_id":"protocol-native","new_generation":"protocol-uncleared"}
            try:
                if feature=="host-status":result=await runtime.executor.execute(old,"recovery-status","protocol-native")
                elif feature=="mutation-clearance":
                    result=host.handle("enroll",provider=provider,alias=alias,expected_generation=old.generation,
                        operation_id="protocol-native",generation="protocol-uncleared")
                else:result=await runtime.operation(payload)
            except (AccountError,VerbError) as exc:error={"type":type(exc).__name__,"code":str(exc)}
            after={"signins":len(logins),"host":host.registry.active(provider,alias,old.host).public(),"native_calls":len(native)}
            record(feature,old,payload,result,error,before,after)
            if feature=="status":
                assert error is None
                assert entry is not None,"Public account status lacks the required recovery metadata contract"
                assert {"snapshot_digest","origin_digest","canonical_tail","fence_causes","current_pin","missing_prerequisite","next_action"}<=set(entry)
            elif feature=="origin-reanchor":
                assert error is None,f"Public origin-reanchor protocol rejected: {error}"
                assert result["result"]["binding"]==after["host"] and after["signins"]==before["signins"]
            elif feature=="fresh-hatch":
                assert error is None,f"Public fresh-enrollment protocol rejected: {error}"
                assert result["result"]["binding"]==after["host"] and after["host"]["home"]!=old.home
                assert after["signins"]==before["signins"]+1
            elif feature=="host-status":
                assert error is None,f"Authenticated host recovery capability missing: {error}"
                assert result["mutation_clearance_required"] is True and result["binding"]==old.public()
            else:
                assert error is not None and error["code"]=="account_mutation_clearance_required",f"Host ignored the required coordinator clearance: {error}"
                assert after==before
        finally:store.stop()
    asyncio.run(run())
