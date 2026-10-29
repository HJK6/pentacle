"""One durable recovery protocol: attempts, successful links, and fence causes.

This module uses existing account recovery rows. A settlement never imports a
binding; only an atomic current-pin import consumes a chain. Host seals prevent
old dispatch from resurrecting an attempt after it has been settled.
"""
import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import time

from account_store import transaction
from provider_accounts import AccountError, Binding, policy_manages
from provider_cli_pin import canonical, current, digest

EFFECT="repin-resolved:"
CONSUMED="repin-consumed:"
HATCH="repin-hatch:"
MUTATION="account-mutation:"
SUPERSEDED="account-superseded:"


def cell(value):
    return {k:value[k] for k in ("host","provider","account")}


def _prefix(namespace,value):
    return namespace+":".join(value[k] for k in ("host","provider","account"))+":"


def rows(a,namespace,value):
    prefix=_prefix(namespace,value);result={}
    for row in a.conn.execute("SELECT binding_key,data FROM v2_account_recovery WHERE substr(binding_key,1,?)=?",(len(prefix),prefix)):
        try:
            state=json.loads(row["data"])
            if (not isinstance(state,dict) or not isinstance(state.get("arguments"),dict)
                    or any(state["arguments"].get(k)!=v for k,v in cell(value).items())
                    or state["arguments"].get("operation_id")!=row["binding_key"][len(prefix):]):raise ValueError()
        except (ValueError,TypeError):
            # A diagnosis address, never fabricated original arguments/proof.
            state={"arguments":{**cell(value),"operation_id":row["binding_key"][len(prefix):]},"status":"parked",
                "park_reason":"account_operation_corrupt","_untrusted":True,"raw_digest":record_digest(a,row["binding_key"])}
        result[row["binding_key"]]=state
    return result


def record_digest(a,key):
    row=a.conn.execute("SELECT state,data FROM v2_account_recovery WHERE binding_key=?",(key,)).fetchone()
    return digest(dict(row)) if row else None


def superseded(a,key):
    proof=get(a,SUPERSEDED+key)
    if not proof:return False
    if proof.get("disposition") not in {"superseded","continued"}:raise AccountError("account_repin_receipt_invalid")
    owner=get(a,proof.get("resolved_by"))
    captured=owner.get("captured",{}).get(key) if owner else None
    if (not captured or proof.get("record_digest")!=captured.get("record_digest")
            or record_digest(a,key)!=proof["record_digest"]):raise AccountError("account_repin_receipt_invalid")
    return True


def get(a,key):
    row=a.conn.execute("SELECT data FROM v2_account_recovery WHERE binding_key=?",(key,)).fetchone()
    return json.loads(row[0]) if row else None


def put(a,key,status,data,*,immutable=False):
    prior=get(a,key)
    if immutable and prior is not None:
        if prior!=data:raise AccountError("account_repin_receipt_invalid")
        return
    a.conn.execute("INSERT OR REPLACE INTO v2_account_recovery VALUES (?,?,?)",(key,status,canonical(data)))


def origin(a,value):
    row=a._cell(value["host"],value["provider"],value["account"])
    if row is None:raise AccountError("account_generation_unknown")
    binding=Binding(**json.loads(row["data"]))
    default=a.conn.execute("SELECT * FROM v2_account_defaults WHERE host=? AND provider=?",(binding.host,binding.provider)).fetchone()
    failure=a.conn.execute("SELECT * FROM v2_account_failures WHERE binding_key=?",(binding.key,)).fetchone()
    validations=[dict(r) for r in a.conn.execute("SELECT * FROM v2_account_health_revalidations WHERE binding_key=? ORDER BY operation_id",(binding.key,))]
    return json.loads(canonical({"binding":asdict(binding),"health":row["health"],"health_revision":row["health_revision"],
        "default":dict(default) if default else None,"policy":a.policy,"failure":dict(failure) if failure else None,
        "health_revalidations":validations}))


def hold(a,value,now):
    row=a.conn.execute("SELECT * FROM v2_spawn_admission_hold WHERE host=?",(value["host"],)).fetchone()
    if not row or row["held_until"]<=now:raise AccountError("account_upgrade_hold_required")
    return dict(row)


def healthy_origin(value):
    binding=Binding(**value["binding"])
    if not binding.enabled or not binding.healthy or value["health"]!="healthy":raise AccountError("account_unavailable")
    failure=value.get("failure")
    if failure and failure["kind"] in {"auth","identity"}:
        cleared=False
        if failure["kind"]=="identity":
            for row in value["health_revalidations"]:
                data=json.loads(row["data"]);result=data.get("result",{})
                if (row["state"]=="done" and result.get("outcome")=="health_revalidated"
                        and result.get("applied_revision",-1)<=value["health_revision"]
                        and data.get("finished_at",0)>=failure["observed_at"]):cleared=True
        if not cleared:raise AccountError("account_unavailable")
    if not policy_manages(value["policy"],binding.host,binding.provider):raise AccountError("account_feature_off")
    return binding


def immutable_origin(value):
    # Health/default/policy are separately CASed metadata, never binding lineage.
    return value["binding"]


def resolved(a,key):
    """Return None for legacy records; all new dispositions validate their owner."""
    from account_repin import _key
    if superseded(a,key):return True
    value=get(a,EFFECT+key)
    if not value:return None
    if value.get("disposition") not in {"sealed_non_split","superseded","reconciled"}:raise AccountError("account_repin_receipt_invalid")
    state=get(a,key)
    if not state or value.get("arguments_digest")!=digest(state["arguments"]):raise AccountError("account_repin_receipt_invalid")
    if value["disposition"]=="sealed_non_split":
        proof=value.get("host_seal") or {}
        if (proof.get("state")!="sealed" or proof.get("worker_quiescent") is not True
                or proof.get("arguments_digest")!=digest(state["arguments"])
                or not (proof.get("successor_absent") is True or proof.get("collision"))):
            raise AccountError("account_repin_receipt_invalid")
    elif value["disposition"]=="superseded":
        hatch=get(a,value.get("resolved_by"))
        if not hatch or key not in hatch.get("captured",{}):raise AccountError("account_repin_receipt_invalid")
    else:
        owner=get(a,value.get("resolved_by"))
        if not owner or owner.get("status")!="done" or key not in owner.get("resolved_predecessors",[]):
            raise AccountError("account_repin_receipt_invalid")
    return True


def hatch_causes(a,value):
    return {k:v for k,v in rows(a,HATCH,value).items() if v.get("status")!="done" and not v.get("continued_by") and not v.get("superseded_by") and not superseded(a,k)}


def causes(a,value):
    from account_repin import _states,_resolved
    result=[]
    for key,state in _states(a,value).items():
        reason=state.get("park_reason","")
        try:
            if state.get("status")=="done" or _resolved(a,key):continue
        except AccountError:reason="account_repin_receipt_invalid"
        receipt=state.get("host_receipt") or {}
        if not isinstance(receipt,dict):receipt={}
        if state.get("_untrusted"):reason="account_operation_corrupt"
        effect="split" if receipt.get("outcome")=="ok" and receipt.get("state")=="done" else "uncertain"
        if any(word in reason for word in ("invalid","conflict","corrupt","state_changed","unresolved_origin")):effect="conflict"
        result.append({"operation_id":state["arguments"]["operation_id"],"cause":effect,"key":key,"reason":reason or "awaiting classification"})
    result.extend({"operation_id":s["arguments"]["operation_id"],"cause":"fresh_enrollment_pending","key":k,
        "reason":s.get("park_reason","fresh enrollment pending")} for k,s in hatch_causes(a,value).items())
    return result


def tail(a,value,*,address=None):
    from account_repin import _states
    states=_states(a,value)
    live={k:s for k,s in states.items() if s.get("status")!="done" and not get(a,CONSUMED+k)}
    if address is not None:
        key="repin:"+":".join(value[k] for k in ("host","provider","account"))+":"+address
        if key not in live:raise AccountError("account_repin_lineage_invalid")
        root=key;seen=set()
        while live[root]["arguments"].get("reconcile_of"):
            if root in seen:raise AccountError("account_repin_lineage_invalid")
            seen.add(root);parent=live[root]["arguments"]["reconcile_of"]
            root=_prefix("repin:",value)+parent
            if root not in live:raise AccountError("account_repin_lineage_invalid")
        family={root}
        while True:
            added={k for k,s in live.items() if _prefix("repin:",value)+str(s["arguments"].get("reconcile_of")) in family}
            if added<=family:break
            family|=added
        live={k:s for k,s in live.items() if k in family}
    else:
        active={x["key"] for x in causes(a,value) if x["key"].startswith("repin:")}
        if not active:return None
        # Include settled descendants, since settlement does not change the address.
        family=set(active)
        while True:
            added={k for k,s in live.items() if _prefix("repin:",value)+str(s["arguments"].get("reconcile_of")) in family}
            if added<=family:break
            family|=added
        live={k:s for k,s in live.items() if k in family}
    parents={_prefix("repin:",value)+s["arguments"]["reconcile_of"] for s in live.values() if s["arguments"].get("reconcile_of")}
    tips=[k for k in live if k not in parents]
    if len(tips)!=1:raise AccountError("account_repin_lineage_invalid",canonical_tail=None)
    return live[tips[0]]["arguments"]["operation_id"]


def ancestry(a,value):
    from account_repin import _states
    states=_states(a,value);parent=value.get("reconcile_of");seen=set();result=[]
    while parent:
        key=_prefix("repin:",value)+parent
        if key in seen or key not in states or get(a,CONSUMED+key):raise AccountError("account_repin_lineage_invalid")
        seen.add(key);state=states[key]
        if state.get("status")=="done":raise AccountError("account_repin_lineage_invalid")
        result.append(state);parent=state["arguments"].get("reconcile_of")
    return list(reversed(result))


def generation_intents(a,value):
    result={k:s for k,s in rows(a,MUTATION,value).items() if s.get("status")!="done" and not s.get("superseded_by")}
    for row in a.conn.execute("SELECT binding_key,data FROM v2_account_recovery"):
        key=row["binding_key"]
        if key.startswith("upgrade:"+":".join(value[k] for k in ("host","provider","account"))+":"):
            state=json.loads(row["data"])
            if state.get("status")!="done":result[key]=state
        binding=a.bound(key)
        if binding and (binding.host,binding.provider,binding.alias)==tuple(value[k] for k in ("host","provider","account")):
            state=json.loads(row["data"])
            if state.get("inflight"):result[key]=state
    return result


async def host_status(executor,old):
    result=await executor.execute(old,"recovery-status","recovery-status:"+old.key)
    from provider_cli_artifact import code_identity
    if (not isinstance(result,dict) or result.get("outcome")!="ok" or result.get("code_identity")!=code_identity()
            or "binding" not in result):raise AccountError("account_host_response_invalid")
    return result


def public_status(a,value,remote,now):
    from account_repin import _states
    from provider_cli_pin import raw,raw_digest
    origins=origin(a,value)
    try:pin=current(a)
    except AccountError:pin=None
    held=a.conn.execute("SELECT * FROM v2_spawn_admission_hold WHERE host=?",(value["host"],)).fetchone()
    held=dict(held) if held else None
    attempts=_states(a,value);hatches=rows(a,HATCH,value)
    dispositions={}
    for k in (*attempts,*hatches):
        disposition=get(a,SUPERSEDED+k) or get(a,EFFECT+k)
        if disposition:dispositions[k]=disposition
    snapshot={"origin":origins,"host":remote,"pin":pin,"pin_raw_digest":raw_digest(raw(a)),"hold":held,
        "attempts":{k:digest(v) for k,v in attempts.items()},"hatches":{k:digest(v) for k,v in hatches.items()},
        "dispositions":{k:digest(v) for k,v in dispositions.items()},
        "generation_intents":{k:digest(v) for k,v in generation_intents(a,value).items()}}
    try:canonical_tail=tail(a,value)
    except AccountError:canonical_tail=None
    fences=causes(a,value)
    missing=("host/source verification" if not remote or remote.get("mutation_clearance_required") is not True else
        "real admission hold" if not held or held["held_until"]<=now else "valid current pin" if pin is None else None)
    return {**cell(value),"snapshot_digest":digest(snapshot),"origin_digest":digest(origins),"canonical_tail":canonical_tail,
        "fence_causes":fences,"dispositions":list(dispositions.values()),"current_pin":pin,
        "missing_prerequisite":missing,"next_action":"repair "+missing if missing else
            "FD reconcile canonical tail or explicitly relogin with supersede_repin" if fences else "ordinary current-pin work",
        "_snapshot":snapshot}


async def recovery_status(runtime,value):
    old=await runtime.run(lambda a:Binding(**origin(a,value)["binding"]))
    remote=None
    needs_host=await runtime.run(lambda a:bool(causes(a,value)))
    if runtime.executor and needs_host:
        try:remote=await host_status(runtime.executor,old)
        except AccountError:pass
    return await runtime.run(lambda a:public_status(a,value,remote,runtime.clock()))


async def permit(executor,old,action,operation,generation,remote,intent):
    if remote.get("mutation_clearance_required") is not True:raise AccountError("account_mutation_clearance_required")
    context={"action":action,"expected_generation":old.generation,"generation":generation,
        "clearance_id":digest({"intent":intent,"operation":operation}),"host_binding":remote["binding"],
        "central_intent":digest(intent),"reserved_at_ns":intent.get("reserved_at_ns",0)}
    result=await executor.execute(old,"mutation-reserve",operation,generation,reconcile=context)
    if result.get("outcome")!="ok" or result.get("permit")!=dict(context,provider=old.provider,alias=old.alias):
        raise AccountError("account_mutation_clearance_required")
    return context["clearance_id"]


def snapshot(a,value,now):
    origins=origin(a,value);old=healthy_origin(origins)
    if old.generation!=value["login_generation"]:raise AccountError("account_login_generation_stale")
    pin=current(a)
    if (pin["revision"],digest(pin))!=(value["expected_pin_revision"],value["expected_pin_digest"]):
        raise AccountError("provider_cli_pin_stale")
    from provider_cli_artifact import code_identity
    return {"binding":asdict(old),"health_revision":origins["health_revision"],"default":origins["default"],
        "policy":origins["policy"],"origin":origins,"hold":hold(a,value,now),"pin":pin,"consumer_code_identity":code_identity()}


def same_historical_origin(before,after):
    return all(before.get(k)==after.get(k) for k in ("binding","health_revision","default","policy"))


def _begin(a,value,now,auth=None):
    from account_repin import _key,_read,_write,_states
    with transaction(a.conn):
        previous=_read(a,value)
        if previous:return previous,False
        before=snapshot(a,value,now)
        if generation_intents(a,value) or hatch_causes(a,value):raise AccountError("account_mutation_unresolved")
        if any(s.get("_untrusted") for s in _states(a,value).values()):raise AccountError("account_repin_lineage_invalid")
        ancestors=ancestry(a,value)
        expected=value.get("expected_origin_digest")
        if expected and expected!=digest(before["origin"]):raise AccountError("account_repin_origin_stale",origin_digest=digest(before["origin"]))
        if ancestors:
            canonical_tail=tail(a,value,address=value["reconcile_of"])
            if canonical_tail!=value["reconcile_of"]:raise AccountError("account_repin_reconciliation_required",canonical_tail=canonical_tail)
            if any(not same_historical_origin(s["snapshot"],before) for s in ancestors):
                if expected!=digest(before["origin"]):
                    raise AccountError("account_repin_state_changed",canonical_tail=canonical_tail,origin_digest=digest(before["origin"]))
                if any(s["snapshot"]["binding"]!=before["binding"] for s in ancestors):
                    raise AccountError("account_repin_state_changed")
        elif value.get("reconcile_of"):
            raise AccountError("account_repin_lineage_invalid")
        allowed={_key(s["arguments"]) for s in ancestors}
        if any(c["key"] not in allowed for c in causes(a,value)):
            raise AccountError("account_repin_reconciliation_required",canonical_tail=tail(a,value))
        if ancestors and any(s["arguments"].get("reconcile_of")==value["reconcile_of"] for s in _states(a,value).values()):
            raise AccountError("account_repin_reconciliation_required",canonical_tail=tail(a,value,address=value["reconcile_of"]))
        state={"arguments":value,"status":"pending","snapshot":before,"old_key":Binding(**before["binding"]).key,
            "new_key":":".join(value[k] for k in ("host","provider","account","new_generation")),
            "principal":{"stream_id":(auth or {}).get("stream_id"),"generation":(auth or {}).get("session_generation")},
            "reserved_at_ns":time.time_ns(),"origin_reanchor":bool(ancestors and not same_historical_origin(ancestors[0]["snapshot"],before))}
        _write(a,value,state)
        return state,True


def _settle(a,state,proof,origins,held,now,links):
    from account_repin import _key,_read
    value=state["arguments"];key=_key(value)
    with transaction(a.conn):
        prior=_read(a,value)
        if prior is None:raise AccountError("account_repin_receipt_invalid")
        if resolved(a,key):return
        if origin(a,value)!=origins or hold(a,value,now)!=held:raise AccountError("account_repin_state_changed")
        base=(Binding.from_public(links[-1]["receipt"]["binding"]) if links else Binding(**state["snapshot"]["binding"]))
        seal=proof.get("seal") or {}
        if (seal.get("arguments_digest")!=digest(value) or seal.get("base")!=base.public()
                or seal.get("worker_quiescent") is not True
                or not (seal.get("successor_absent") is True or seal.get("collision"))):
            raise AccountError("account_repin_receipt_invalid")
        # A wrapper's G1 base is proved by L, while C remains its immutable G0.
        if origins["binding"]!=state["snapshot"]["binding"]:raise AccountError("account_repin_state_changed")
        put(a,EFFECT+key,"done",{"disposition":"sealed_non_split","arguments_digest":digest(value),
            "host_seal":seal,"origin":origins,"hold":held,"lineage":links},immutable=True)


async def recover_links(executor,states,auth):
    """Fold only actual successful binding effects, traversing every zero-link node."""
    from account_repin import _key,_successor,_host_id,_link,_save_host
    runtime=executor.runtime;links=[]
    if not states:return links
    root=Binding(**states[0]["snapshot"]["binding"])
    base=root
    for state in states:
        value=state["arguments"];key=_key(value)
        disposition=await runtime.run(lambda a:get(a,EFFECT+key))
        if disposition and disposition.get("disposition")=="sealed_non_split":continue
        if disposition and disposition.get("disposition")=="superseded":raise AccountError("account_repin_lineage_invalid")
        context={"arguments":value,"pin":state["snapshot"]["pin"],"base":asdict(base),"root":asdict(root),
            "links":links,"consumer_code_identity":state["snapshot"]["consumer_code_identity"]}
        inspected=await executor.execute(root,"repin-inspect","repin:"+value["operation_id"],value["new_generation"],reconcile=context)
        await runtime._front_desk(auth)
        if not isinstance(inspected,dict) or inspected.get("outcome")!="ok":raise AccountError("account_repin_execution_unresolved")
        receipt=inspected.get("receipt") or {}
        if inspected.get("effect")=="conflict":raise AccountError("account_repin_lineage_invalid")
        if receipt.get("state")=="done" and receipt.get("outcome") in {"ok","no_op"}:
            if receipt.get("kind")=="repin-reconcile":
                candidate=receipt.get("links")
                if not isinstance(candidate,list) or candidate[:len(links)]!=links:raise AccountError("account_repin_lineage_invalid")
                new_links=candidate[len(links):]
            else:
                new_links=[{**_link(state),"receipt":receipt}]
            for link in new_links:
                if link.get("old")!=asdict(base):raise AccountError("account_repin_lineage_invalid")
                successor,no_op=_successor(base,link["arguments"],link["receipt"],link["pin"],consumer_identity=link["consumer_code_identity"])
                if no_op:
                    if inspected.get("binding")!=base.public():raise AccountError("account_repin_lineage_invalid")
                else:links.append(link);base=successor
            await runtime.run(lambda a:_save_host(a,value,receipt),settle_on_cancel=True)
            continue
        if inspected.get("effect")=="success":
            raise AccountError("account_repin_lineage_unresolved")
        if inspected.get("effect")!="zero":raise AccountError("account_repin_execution_unresolved")
        origins,held=await runtime.run(lambda a:(origin(a,value),hold(a,value,runtime.clock())))
        sealed=await executor.execute(root,"repin-settle","repin:"+value["operation_id"],value["new_generation"],reconcile=context)
        await runtime._front_desk(auth)
        await runtime.run(lambda a:_settle(a,state,sealed,origins,held,runtime.clock(),links),settle_on_cancel=True)
    return links


def _pre_dispatch(a,state,now):
    value=state["arguments"]
    if snapshot(a,value,now)!=state.get("commit_snapshot",state["snapshot"]):raise AccountError("account_repin_state_changed")


def _commit(a,state,receipt,links,proof,now):
    from account_repin import _key,_read,_write,_successor,LINEAGE
    from provider_cli_artifact import validate_proof
    value=state["arguments"];key=_key(value)
    with transaction(a.conn):
        prior=_read(a,value)
        if prior and prior["status"]=="done":return prior["result"]
        if prior!=state:raise AccountError("account_repin_state_changed")
        if superseded(a,key) or (get(a,EFFECT+key) or {}).get("disposition")=="superseded" or hatch_causes(a,value):
            raise AccountError("account_repin_superseded")
        _pre_dispatch(a,state,now)
        old=Binding(**state["snapshot"]["binding"]);initial=old;seen={old.key};verified=[]
        for link in links:
            if link.get("old")!=asdict(old):raise AccountError("account_repin_lineage_invalid")
            new,no_op=_successor(old,link["arguments"],link["receipt"],link["pin"],consumer_identity=link["consumer_code_identity"])
            if no_op or new.key in seen or a.bound(new.key):raise AccountError("account_repin_lineage_invalid")
            seen.add(new.key);verified.append((old,new,link));old=new
        final=old
        if receipt.get("binding")!=final.public():raise AccountError("account_repin_lineage_invalid")
        validate_proof(final,state["snapshot"]["pin"],proof)
        ancestors=ancestry(a,value)
        for ancestor in ancestors:
            k=_key(ancestor["arguments"])
            if get(a,EFFECT+k) and get(a,EFFECT+k).get("disposition")=="superseded":raise AccountError("account_repin_lineage_invalid")
        for old,new,link in verified:
            a._register(new)
            if not a._observe(new.key,link["receipt"]["observation"],now=now):raise AccountError("account_repin_observation_invalid")
            data={"operation_key":key,"old_key":old.key,"new_key":new.key}
            if value.get("reconcile_of"):data["evidence"]=link
            put(a,LINEAGE+old.key,"done",data,immutable=True)
        a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",("provider_cli.verified:"+final.key,canonical({"proof":proof,"verified_at":now})))
        result={"outcome":"repinned" if links else "no_op","operation_id":value["operation_id"],"binding":final.public()}
        completed={**state,"status":"done","new_key":final.key,"result":result,"host_receipt":receipt,
            "lineage":links,"resolved_predecessors":[_key(s["arguments"]) for s in ancestors]}
        _write(a,value,completed)
        for ancestor in ancestors:
            k=_key(ancestor["arguments"])
            if get(a,EFFECT+k) is None:
                put(a,EFFECT+k,"done",{"disposition":"reconciled","arguments_digest":digest(ancestor["arguments"]),"resolved_by":key},immutable=True)
            put(a,CONSUMED+k,"done",{"consumed_by":key},immutable=True)
        if state.get("origin_reanchor"):
            put(a,"repin-origin:"+key,"done",{"disposition":"origin_reanchored","original":ancestors[0]["snapshot"],
                "accepted":state["snapshot"]["origin"],"principal":state["principal"],"canonical_tail":value["reconcile_of"],"expected_origin_digest":value["expected_origin_digest"],"lineage":links},immutable=True)
        return result


async def repin(executor,message,auth):
    from account_repin import _payload,_read,_states,_resolved,_save_host,_link
    runtime=executor.runtime
    await runtime._front_desk(auth);value=_payload(message)
    async with executor._host_locks.setdefault(value["host"],asyncio.Lock()):
        await runtime._front_desk(auth)
        known=await runtime.run(lambda a:_read(a,value))
        if known and known["status"]=="done":return known["result"]
        if known and await runtime.run(lambda a:_resolved(a,__import__('account_repin')._key(value))):
            return {"outcome":"settled","operation_id":value["operation_id"],"historical":True}
        if not value.get("reconcile_of") and not known:
            # A fresh ordinary request also classifies earlier non-split roots.
            previous=await runtime.run(lambda a:[s for k,s in _states(a,value).items()
                if s.get("status")!="done" and not _resolved(a,k)])
            if any(s.get("_untrusted") for s in previous):raise AccountError("account_repin_lineage_invalid")
            if previous:
                try:await recover_links(executor,previous,auth)
                except AccountError:pass
        await runtime._front_desk(auth)
        state,created=await runtime.run(lambda a:_begin(a,value,runtime.clock(),auth),settle_on_cancel=True)
        if state["status"]=="done":return state["result"]
        if not created:
            # A fresh real hold may authorize import of a historical success.
            # Preserve the original attempt snapshot and proof without relabeling.
            def renew(a):
                from account_repin import _write
                with transaction(a.conn):
                    fresh=snapshot(a,value,runtime.clock())
                    original=state["snapshot"]
                    if {k:v for k,v in fresh.items() if k!="hold"}!={k:v for k,v in original.items() if k!="hold"}:
                        raise AccountError("account_repin_state_changed")
                    updated={**state,"commit_snapshot":fresh}
                    _write(a,value,updated);return updated
            state=await runtime.run(renew,settle_on_cancel=True)
        old=Binding(**state["snapshot"]["binding"]);receipt=None
        try:
            states=await runtime.run(lambda a:ancestry(a,value))
            if not created:states=states+[state]
            links=await recover_links(executor,states,auth)
            await runtime._front_desk(auth)
            if not created:
                disposition=await runtime.run(lambda a:get(a,EFFECT+__import__('account_repin')._key(value)))
                if disposition:return {"outcome":"settled","operation_id":value["operation_id"],"historical":True}
                receipt=await runtime.run(lambda a:_read(a,value).get("host_receipt"))
                if not receipt:raise AccountError("account_repin_execution_unresolved")
            else:
                await runtime.run(lambda a:_pre_dispatch(a,state,runtime.clock()))
                remote=await host_status(executor,old)
                action="repin-reconcile" if links else "repin"
                context={"root":asdict(old),"links":links} if links else {}
                clearance=await permit(executor,old,action,"repin:"+value["operation_id"],value["new_generation"],remote,state)
                await runtime._front_desk(auth)
                await runtime.run(lambda a:_pre_dispatch(a,state,runtime.clock()))
                receipt=await executor.execute(old,action,"repin:"+value["operation_id"],value["new_generation"],
                    pin=state["snapshot"]["pin"],reconcile={**context,"_clearance":clearance})
                if not isinstance(receipt,dict):raise AccountError("account_host_response_invalid")
                state=await runtime.run(lambda a:_save_host(a,value,receipt),settle_on_cancel=True)
                if receipt.get("state")!="done" or receipt.get("outcome") not in {"ok","no_op"}:
                    raise AccountError("account_repin_execution_failed")
                if links:
                    candidate=receipt.get("links")
                    if not isinstance(candidate,list) or candidate[:len(links)]!=links:raise AccountError("account_repin_lineage_invalid")
                    links=candidate
                elif receipt.get("outcome")=="ok":links=[{**_link(state),"receipt":receipt}]
            final=Binding.from_public(receipt.get("binding"))
            proof=await executor.verify_artifact(final,state["snapshot"]["pin"])
            await runtime._front_desk(auth)
            async with runtime.pin_lock:
                return await runtime.run(lambda a:_commit(a,state,receipt,links,proof,runtime.clock()),settle_on_cancel=True)
        except (AccountError,asyncio.CancelledError) as exc:
            parked=await runtime.run(lambda a:_save_host(a,value,receipt if isinstance(receipt,dict) else None,str(exc) or "cancelled"),settle_on_cancel=True)
            if not isinstance(exc,asyncio.CancelledError):
                try:
                    await runtime._front_desk(auth)
                    ancestors=await runtime.run(lambda a:ancestry(a,value))
                    await recover_links(executor,ancestors+[parked],auth)
                except AccountError:pass
            raise


def require_generation_clear(a,value):
    fences=causes(a,value)
    if fences:raise AccountError("account_repin_reconciliation_required",canonical_tail=tail(a,value),
        next_action="settle/reconcile first or explicitly choose fresh enrollment with supersede_repin")


async def diagnostic_lineage(executor,value,auth):
    """Read L without sealing, settling, creating generations or probing G0."""
    from account_repin import _states,_successor,_link
    runtime=executor.runtime
    states=await runtime.run(lambda a:_states(a,value))
    old=await runtime.run(lambda a:Binding(**origin(a,value)["binding"]))
    address=await runtime.run(lambda a:tail(a,value))
    if not address:return None
    lineage=await runtime.run(lambda a:ancestry(a,{**value,"reconcile_of":address}))
    links=[];base=old
    for state in lineage:
        args=state["arguments"]
        context={"arguments":args,"pin":state["snapshot"]["pin"],"base":asdict(base),"root":asdict(old),
            "links":links,"consumer_code_identity":state["snapshot"]["consumer_code_identity"]}
        inspected=await executor.execute(old,"repin-inspect","repin:"+args["operation_id"],args["new_generation"],reconcile=context)
        await runtime._front_desk(auth)
        if inspected.get("effect")=="zero":continue
        if inspected.get("effect")!="success":raise AccountError("account_repin_lineage_invalid")
        receipt=inspected.get("receipt") or {}
        if receipt.get("state")!="done" or receipt.get("outcome") not in {"ok","no_op"}:raise AccountError("account_repin_lineage_invalid")
        if receipt.get("kind")=="repin-reconcile":
            extended=receipt.get("links")
            if not isinstance(extended,list) or extended[:len(links)]!=links:raise AccountError("account_repin_lineage_invalid")
            candidates=extended[len(links):]
        else:candidates=[{**_link(state),"receipt":receipt}]
        for link in candidates:
            if link.get("old")!=asdict(base):raise AccountError("account_repin_lineage_invalid")
            base,no_op=_successor(base,link["arguments"],link["receipt"],link["pin"],consumer_identity=link["consumer_code_identity"])
            if not no_op:links.append(link)
    if not links:return None
    return {"root":asdict(old),"links":links,"host_binding":base.public(),"canonical_tail":address}


def reserve_generation(a,account,operation,generation,kind):
    value={"host":account.host,"provider":account.provider,"account":account.alias}
    with transaction(a.conn):
        require_generation_clear(a,value)
        key=_prefix(MUTATION,value)+operation
        prior=get(a,key)
        if prior:return prior
        if generation_intents(a,value):raise AccountError("account_mutation_unresolved")
        before=origin(a,value)
        if before["binding"]!=asdict(account):raise AccountError("account_login_generation_stale")
        state={"arguments":{**value,"operation_id":operation,"generation":generation,"kind":kind},
            "status":"pending","origin":before,"reserved_at_ns":time.time_ns()}
        put(a,key,"pending",state);return state


def complete_generation(a,account,operation):
    value={"host":account.host,"provider":account.provider,"account":account.alias}
    key=_prefix(MUTATION,value)+operation
    state=get(a,key)
    if state:put(a,key,"done",{**state,"status":"done"})
