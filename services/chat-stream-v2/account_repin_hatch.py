"""FD opt-in fresh enrollment supersedes causes, never old repin lineage."""
import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import time
import uuid

from account_store import transaction
from account_revalidation import _valid_observation
from provider_accounts import AccountError,Binding
from provider_cli_pin import current,digest,canonical
import account_repin_state as recovery


def _key(value):return recovery._prefix(recovery.HATCH,value)+value["operation_id"]


def _payload(message):
    from provider_accounts import _component
    value={k:message.get(k) for k in ("host","provider","account","login_generation","operation_id","supersede_repin")}
    for name in ("host","account","login_generation"):_component(value[name])
    if value["provider"] not in {"codex","claude"}:raise AccountError("account_request_invalid")
    from _shared.provider_cli_release import SHA256
    if not isinstance(value["supersede_repin"],str) or not SHA256.fullmatch(value["supersede_repin"]):raise AccountError("account_request_invalid")
    op=value["operation_id"]
    if not isinstance(op,str) or not 1<=len(op)<=160 or any(ord(c)<32 or ord(c)>126 for c in op):raise AccountError("account_operation_id_required")
    return value


def _read(a,value):
    state=recovery.get(a,_key(value))
    if state and state["arguments"]!=value:raise AccountError("account_repin_id_conflict")
    return state


def _begin(a,value,status,auth,now):
    from account_repin import _states
    with transaction(a.conn):
        previous=_read(a,value)
        if previous:return previous,False
        actual=recovery.public_status(a,value,status["_snapshot"]["host"],now)
        if actual["snapshot_digest"]!=value["supersede_repin"]:raise AccountError("account_repin_supersession_stale",snapshot_digest=actual["snapshot_digest"])
        snapshot=actual["_snapshot"];origins=snapshot["origin"];old=Binding(**origins["binding"])
        if value["login_generation"] not in {"active",old.generation}:raise AccountError("account_login_generation_stale")
        if not old.enabled or not recovery.policy_manages(origins["policy"],old.host,old.provider):raise AccountError("account_feature_off")
        if snapshot["pin"] is None:raise AccountError("provider_cli_pin_unavailable")
        held=recovery.hold(a,value,now)
        if not actual["fence_causes"]:raise AccountError("account_repin_supersession_empty")
        remote=snapshot["host"]
        if not remote or remote.get("mutation_clearance_required") is not True:raise AccountError("account_mutation_clearance_required")
        captured={k:s for k,s in _states(a,value).items() if any(c["key"]==k for c in actual["fence_causes"])}
        captured.update(recovery.hatch_causes(a,value))
        captured={k:{"digest":digest(v),"record_digest":recovery.record_digest(a,k),"arguments":v["arguments"],"arguments_digest":digest(v["arguments"])}
            for k,v in captured.items()}
        # The snapshot addresses the complete set, even if ancestry is invalid.
        state={"arguments":value,"status":"pending","snapshot":snapshot,"captured":captured,
            "generation":uuid.uuid4().hex,"reserved_at_ns":time.time_ns(),"stage":"reserved",
            "principal":{"stream_id":auth.get("stream_id"),"generation":auth.get("session_generation")}}
        recovery.put(a,_key(value),"pending",state)
        return state,True


def _save(a,value,**fields):
    with transaction(a.conn):
        state=_read(a,value)
        if not state or state.get("status")=="done" or state.get("continued_by") or state.get("superseded_by") or recovery.superseded(a,_key(value)):return state
        state={**state,**fields};recovery.put(a,_key(value),state["status"],state);return state


def _check(a,state,now):
    value=state["arguments"];snapshot=state["snapshot"]
    if recovery.origin(a,value)!=snapshot["origin"] or recovery.hold(a,value,now)!=snapshot["hold"]:
        raise AccountError("account_repin_state_changed")
    if current(a)!=snapshot["pin"]:raise AccountError("provider_cli_pin_stale")
    for key,before in state["captured"].items():
        if recovery.record_digest(a,key)!=before["record_digest"]:raise AccountError("account_repin_state_changed")
    # No new unresolved reservation may have slipped in before this intent won.
    current_causes={c["key"] for c in recovery.causes(a,value)}
    allowed=set(state["captured"])|{_key(value)}
    if not current_causes<=allowed:raise AccountError("account_repin_state_changed")


def _superseded(a,state,sealed,retained,now):
    value=state["arguments"];key=_key(value)
    with transaction(a.conn):
        _check(a,state,now)
        if sealed.get("outcome")!="ok" or sealed.get("snapshot_digest")!=value["supersede_repin"] or sealed.get("worker_quiescent") is not True:
            raise AccountError("account_repin_receipt_invalid")
        for old,before in state["captured"].items():
            recovery.put(a,recovery.SUPERSEDED+old,"done",{"disposition":"continued" if retained and old==retained[0] else "superseded",
                "arguments_digest":before["arguments_digest"],"record_digest":before["record_digest"],"resolved_by":key},immutable=True)
        updated={**state,"stage":"superseded","supersession_proof":sealed}
        if retained:updated["retained"]=retained[0]
        recovery.put(a,key,"pending",updated)
        return updated


def _validate_receipt(state,receipt,old):
    from provider_cli_artifact import validate_proof
    if (not isinstance(receipt,dict) or receipt.get("state")!="done" or receipt.get("outcome")!="ok"
            or receipt.get("generation")!=state["generation"] or receipt.get("new_home") is not True):
        raise AccountError("account_host_binding_invalid")
    new=Binding.from_public(receipt.get("binding"))
    if any(getattr(new,k)!=getattr(old,k) for k in ("host","provider","alias","provider_id","pool_id","platform")):
        raise AccountError("account_host_binding_invalid")
    if new.home==old.home or not new.enabled or not new.healthy or new.default_revision:
        raise AccountError("account_host_binding_invalid")
    host_before=state["snapshot"]["host"]["binding"]
    if host_before and new.home==host_before["home"]:raise AccountError("account_host_binding_invalid")
    if not _valid_observation(new,receipt.get("observation")):raise AccountError("account_repin_observation_invalid")
    validate_proof(new,state["snapshot"]["pin"],receipt.get("proof") or {},
        consumer_identity=state["snapshot"]["host"]["code_identity"])
    return new


def _login_attempt(a,state,now):
    from account_executor import AccountExecutor
    value=state["arguments"];limits_key="cell:"+":".join(value[k] for k in ("host","provider","account"))
    with transaction(a.conn):
        _check(a,state,now)
        limits=AccountExecutor._read_recovery(a,limits_key)
        attempts=[t for t in limits.get("attempts",[]) if 0<=now-t<3600]
        if len(attempts)>=3:raise AccountError("recovery_capped")
        if limits.get("last_attempt") is not None and now-limits["last_attempt"]<900:raise AccountError("recovery_cooldown")
        AccountExecutor._write_recovery(a,limits_key,dict(limits,attempts=attempts+[now],last_attempt=now))
        updated={**state,"stage":"started"};recovery.put(a,_key(value),"pending",updated);return updated


def _commit(a,state,root,final,proof,links,now):
    from account_repin import LINEAGE
    value=state["arguments"];key=_key(value)
    with transaction(a.conn):
        previous=_read(a,value)
        if previous and previous["status"]=="done":return previous["result"]
        if previous!=state or previous.get("continued_by") or previous.get("superseded_by") or recovery.superseded(a,key):
            raise AccountError("account_repin_state_changed")
        _check(a,state,now)
        from provider_cli_artifact import validate_proof
        validate_proof(final,state["snapshot"]["pin"],proof)
        # This is fresh enrollment; no edge ever joins the old C/home to root.
        if a.bound(root.key):raise AccountError("account_binding_immutable")
        a._register(root)
        if not a._observe(root.key,state["enrollment_receipt"]["observation"],now=now):raise AccountError("account_repin_observation_invalid")
        for link in links:
            old=Binding(**link["old"])
            from account_repin import _successor
            new,no_op=_successor(old,link["arguments"],link["receipt"],link["pin"],consumer_identity=link["consumer_code_identity"])
            if no_op or a.bound(new.key):raise AccountError("account_repin_lineage_invalid")
            a._register(new)
            if not a._observe(new.key,link["receipt"]["observation"],now=now):raise AccountError("account_repin_observation_invalid")
            recovery.put(a,LINEAGE+old.key,"done",{"operation_key":key,"old_key":old.key,"new_key":new.key,"evidence":link},immutable=True)
        if final.key!=(new.key if links else root.key):raise AccountError("account_repin_lineage_invalid")
        a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",("provider_cli.verified:"+final.key,canonical({"proof":proof,"verified_at":now})))
        result={"outcome":"recovered","operation_id":value["operation_id"],"binding":final.public(),"superseded":list(state["captured"])}
        recovery.put(a,key,"done",{**state,"status":"done","result":result,"root":asdict(root),"lineage":links})
        return result


async def _retained(executor,state):
    """Read exact enrollment/target journals; never dispatch a second sign-in."""
    runtime=executor.runtime
    for key in reversed(list(state["captured"])):
        if not key.startswith(recovery.HATCH):continue
        try:
            candidate=await runtime.run(lambda a,key=key:recovery.get(a,key))
            origin=candidate.get("enrollment_origin",candidate)
            old=Binding(**origin["snapshot"]["origin"]["binding"])
            receipt=candidate.get("enrollment_receipt")
            if not receipt and candidate.get("stage") in {"started","host_success"}:
                base=origin["snapshot"]["host"]["binding"]
                base=Binding.from_public(base) if base else replace(old,generation="unenrolled")
                receipt=await executor.execute(base,"status","hatch:"+origin["arguments"]["operation_id"],origin["generation"],pin=origin["snapshot"]["pin"])
            if not isinstance(receipt,dict) or receipt.get("state")!="done" or receipt.get("outcome")!="ok":continue
            root=_validate_receipt(origin,receipt,old)
            links=candidate.get("post_enrollment_links",[])
            context=candidate.get("target_context")
            if context:
                result=await executor.execute(root,"repin-reconcile-status",candidate["target_operation"],candidate["target_generation"],
                    pin=candidate["snapshot"]["pin"],reconcile=context)
                if result.get("state")=="done" and result.get("outcome")=="ok":links=result["links"]
            final=root
            from account_repin import _successor
            for link in links:
                if link["old"]!=asdict(final):raise AccountError("account_repin_lineage_invalid")
                final,no_op=_successor(final,link["arguments"],link["receipt"],link["pin"],consumer_identity=link["consumer_code_identity"])
                if no_op:raise AccountError("account_repin_lineage_invalid")
            if state["snapshot"]["host"]["binding"]!=final.public():raise AccountError("account_repin_lineage_invalid")
            # Keep only the immutable enrollment origin, avoiding recursive captures.
            compact={k:origin[k] for k in ("arguments","snapshot","generation")}
            return key,{**candidate,"enrollment_origin":compact,"enrollment_receipt":receipt,"post_enrollment_links":links},root
        except (AccountError,KeyError,TypeError,ValueError):
            continue
    return None


async def hatch(executor,message,auth):
    runtime=executor.runtime;await runtime._front_desk(auth);value=_payload(message)
    async with executor._host_locks.setdefault(value["host"],asyncio.Lock()):
        await runtime._front_desk(auth)
        previous=await runtime.run(lambda a:_read(a,value))
        if previous and previous.get("status")=="done":return previous["result"]
        if previous and (previous.get("continued_by") or previous.get("superseded_by") or await runtime.run(lambda a:recovery.superseded(a,_key(value)))):
            raise AccountError("account_repin_superseded")
        old=await runtime.run(lambda a:Binding(**recovery.origin(a,value)["binding"]))
        if previous:state=previous
        else:
            status=await recovery.recovery_status(runtime,value)
            await runtime._front_desk(auth)
            state,_=await runtime.run(lambda a:_begin(a,value,status,auth,runtime.clock()),settle_on_cancel=True)
        receipt=None
        try:
            retained=await _retained(executor,state)
            operations=[]
            for key,candidate in state["captured"].items():
                op=candidate["arguments"]["operation_id"]
                if retained and key==retained[0]:
                    # Retain the successful root and links; seal an abandoned
                    # zero-link target so it cannot later change the new home.
                    candidate=retained[1]
                    if candidate.get("target_context") and not candidate.get("post_enrollment_links"):
                        operations.append(hashlib.sha256(candidate["target_operation"].encode()).hexdigest())
                    continue
                operations.append(hashlib.sha256((("hatch:" if key.startswith(recovery.HATCH) else "repin:")+op).encode()).hexdigest())
                if key.startswith(recovery.HATCH):operations.append(hashlib.sha256(("hatch:"+op+"-continuation").encode()).hexdigest())
            keep=set()
            if retained:
                kept=retained[1];origin=kept["enrollment_origin"]
                keep.add(hashlib.sha256(("hatch:"+origin["arguments"]["operation_id"]).encode()).hexdigest())
                for link in kept["post_enrollment_links"]:
                    keep.add(link["operation_id"])
                    if link["operation_id"].endswith("-target"):keep.add(link["operation_id"][:-7])
            operations=sorted((set(operations)|set(state["snapshot"]["host"].get("permits",{})))-keep)
            if state.get("stage")=="reserved":
                context={"snapshot_digest":value["supersede_repin"],"host_binding":state["snapshot"]["host"]["binding"],
                    "operations":operations,"retained":retained[1].get("enrollment_receipt") if retained else None}
                if retained:
                    candidate=retained[1];origin=candidate["enrollment_origin"]
                    context["retained_lineage"]={"root":asdict(retained[2]),"links":candidate["post_enrollment_links"],
                        "enrollment":{"operation_id":hashlib.sha256(("hatch:"+origin["arguments"]["operation_id"]).encode()).hexdigest(),
                            "receipt":candidate["enrollment_receipt"],"pin":origin["snapshot"]["pin"],
                            "consumer_code_identity":origin["snapshot"]["host"]["code_identity"]}}
                sealed=await executor.execute(old,"repin-supersede","hatch:"+value["operation_id"],state["generation"],reconcile=context)
                await runtime._front_desk(auth)
                state=await runtime.run(lambda a:_superseded(a,state,sealed,retained,runtime.clock()),settle_on_cancel=True)
            if retained:
                root=retained[2];receipt=retained[1]["enrollment_receipt"]
                state=await runtime.run(lambda a:_save(a,value,enrollment_receipt=receipt,
                    enrollment_origin=retained[1]["enrollment_origin"],post_enrollment_links=retained[1]["post_enrollment_links"],stage="host_success"),settle_on_cancel=True)
            else:
                actual=state["snapshot"]["host"]["binding"]
                host_base=Binding.from_public(actual) if actual else replace(old,generation="unenrolled")
                op="hatch:"+value["operation_id"]
                if state.get("enrollment_receipt"):receipt=state["enrollment_receipt"]
                elif state.get("stage")=="started":receipt=await executor.execute(host_base,"status",op,state["generation"],pin=state["snapshot"]["pin"])
                else:
                    await runtime.run(lambda a:_check(a,state,runtime.clock()))
                    clearance=await recovery.permit(executor,host_base,"enroll",op,state["generation"],state["snapshot"]["host"],state)
                    await runtime._front_desk(auth)
                    state=await runtime.run(lambda a:_login_attempt(a,state,runtime.clock()),settle_on_cancel=True)
                    receipt=await executor.execute(host_base,"enroll",op,state["generation"],pin=state["snapshot"]["pin"],reconcile={"_clearance":clearance})
                await runtime._front_desk(auth)
                root=_validate_receipt(state,receipt,old)
                state=await runtime.run(lambda a:_save(a,value,enrollment_receipt=receipt,stage="host_success"),settle_on_cancel=True)
            links=state.get("post_enrollment_links",[]);final=root
            from account_repin import _successor
            for link in links:
                final,_=_successor(final,link["arguments"],link["receipt"],link["pin"],consumer_identity=link["consumer_code_identity"])
            try:proof=await executor.verify_artifact(final,state["snapshot"]["pin"])
            except AccountError as exc:
                if str(exc)!="provider_cli_pin_mismatch":raise
                remote=await recovery.host_status(executor,old)
                enrollment_origin=state.get("enrollment_origin",state)
                context={"root":asdict(root),"links":links,"enrollment":{
                    "operation_id":hashlib.sha256(("hatch:"+enrollment_origin["arguments"]["operation_id"]).encode()).hexdigest(),
                    "receipt":receipt,"pin":enrollment_origin["snapshot"]["pin"],
                    "consumer_code_identity":enrollment_origin["snapshot"]["host"]["code_identity"]}}
                op="hatch:"+value["operation_id"]+"-continuation"
                if state.get("target_context"):
                    result=await executor.execute(root,"repin-reconcile-status",op,state["generation"],pin=state["snapshot"]["pin"],reconcile=context)
                else:
                    clearance=await recovery.permit(executor,root,"repin-reconcile",op,state["generation"],remote,state)
                    state=await runtime.run(lambda a:_save(a,value,target_context=context,target_operation=op,target_generation=state["generation"]),settle_on_cancel=True)
                    await runtime._front_desk(auth)
                    await runtime.run(lambda a:_check(a,state,runtime.clock()))
                    result=await executor.execute(root,"repin-reconcile",op,state["generation"],pin=state["snapshot"]["pin"],reconcile={**context,"_clearance":clearance})
                if result.get("state")!="done" or result.get("outcome")!="ok":raise AccountError("account_repin_execution_unresolved")
                if result.get("links",[])[:len(links)]!=links:raise AccountError("account_repin_lineage_invalid")
                links=result["links"];final=Binding.from_public(result["binding"])
                state=await runtime.run(lambda a:_save(a,value,post_enrollment_links=links,final_binding=final.public()),settle_on_cancel=True)
                proof=await executor.verify_artifact(final,state["snapshot"]["pin"])
            remote=await recovery.host_status(executor,old)
            if remote["binding"]!=final.public():raise AccountError("account_repin_state_changed")
            await runtime._front_desk(auth)
            async with runtime.pin_lock:
                return await runtime.run(lambda a:_commit(a,state,root,final,proof,links,runtime.clock()),settle_on_cancel=True)
        except (AccountError,asyncio.CancelledError) as exc:
            await runtime.run(lambda a:_save(a,value,status="parked",park_reason=str(exc) or "cancelled",last_host_receipt=receipt),settle_on_cancel=True)
            raise
