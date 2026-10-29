"""FD-only same-home successors, using existing account and host receipt storage."""
from __future__ import annotations
import asyncio
from dataclasses import asdict,replace
import json
import hashlib

from account_store import transaction
from account_upgrade import _snapshot
from account_revalidation import _valid_observation
from provider_accounts import AccountError,Binding,_component
from provider_cli_pin import canonical,current,digest,check

PREFIX="repin:"
LINEAGE="repin-lineage:"


def _payload(message):
    fields=("host","provider","account","login_generation","new_generation","operation_id",
            "expected_pin_revision","expected_pin_digest","reason")
    value={k:message.get(k) for k in fields}
    if message.get("reconcile_of") is not None:
        parent=message["reconcile_of"]
        if not isinstance(parent,str) or not 1<=len(parent)<=160 or any(ord(c)<32 or ord(c)>126 for c in parent):
            raise AccountError("account_repin_request_invalid")
        if parent==value["operation_id"]: raise AccountError("account_repin_request_invalid")
        value["reconcile_of"]=parent
    for k in ("host","account","login_generation","new_generation"): _component(value[k])
    if (value["provider"] not in {"claude","codex"} or value["login_generation"]=="active"
            or value["new_generation"] in {"active",value["login_generation"]}
            or type(value["expected_pin_revision"]) is not int or value["expected_pin_revision"]<1):
        raise AccountError("account_repin_request_invalid")
    from _shared.provider_cli_release import SHA256
    if not isinstance(value["expected_pin_digest"],str) or not SHA256.fullmatch(value["expected_pin_digest"]):
        raise AccountError("account_repin_request_invalid")
    for k in ("operation_id","reason"):
        if (not isinstance(value[k],str) or not 1<=len(value[k])<=160 or not value[k].strip()
                or any(ord(c)<32 or ord(c)>126 for c in value[k])): raise AccountError("account_repin_request_invalid")
    return value


def _key(value):
    return PREFIX+":".join(value[k] for k in ("host","provider","account","operation_id"))


def _read(a,value):
    row=a.conn.execute("SELECT state,data FROM v2_account_recovery WHERE binding_key=?",(_key(value),)).fetchone()
    if not row: return None
    state=json.loads(row["data"])
    if state.get("arguments")!=value: raise AccountError("account_repin_id_conflict")
    if state.get("status")!=row["state"]: raise AccountError("account_repin_receipt_invalid")
    return state


def _write(a,value,state):
    a.conn.execute("INSERT OR REPLACE INTO v2_account_recovery VALUES (?,?,?)",
                   (_key(value),state["status"],canonical(state)))


def _state_snapshot(a,value,now):
    pin=current(a)
    if pin["revision"]!=value["expected_pin_revision"] or digest(pin)!=value["expected_pin_digest"]:
        raise AccountError("provider_cli_pin_stale")
    snapshot=_snapshot(a,value,now)
    if a.bound(":".join((value["host"],value["provider"],value["account"],value["new_generation"]))):
        raise AccountError("account_binding_immutable")
    snapshot["pin"]=pin
    snapshot["policy"]=a.policy
    from provider_cli_artifact import code_identity
    snapshot["consumer_code_identity"]=code_identity()
    return snapshot


def _states(a,value):
    prefix=PREFIX+":".join(value[k] for k in ("host","provider","account"))+":"
    return {r["binding_key"]:json.loads(r["data"]) for r in a.conn.execute(
        "SELECT binding_key,data FROM v2_account_recovery WHERE substr(binding_key,1,?)=?",(len(prefix),prefix))}


def _non_split_status(state,status):
    """Only authenticated terminal failure, or proven pre-action refusal, is conclusive."""
    value=state["arguments"];old=Binding(**state["snapshot"]["binding"])
    identity={"kind":"repin","provider":old.provider,"alias":old.alias,
        "expected_generation":old.generation,"generation":value["new_generation"],
        "pin_revision":state["snapshot"]["pin"]["revision"],"pin_digest":digest(state["snapshot"]["pin"])}
    proof=status.get("non_split") or {}
    terminal=(status.get("state")=="done" and status.get("outcome") not in {"ok","no_op","execution_unresolved"})
    pre_action=(status.get("state")=="absent" and (state.get("park_reason") in {
        "account_repin_home_busy","account_repin_occupancy_unavailable"} or
        state.get("host_receipt",{}).get("outcome") in {"account_repin_home_busy","account_repin_occupancy_unavailable"}))
    return (not value.get("reconcile_of") and (terminal or pre_action)
        and all(status.get(k)==v for k,v in identity.items())
        and proof.get("binding")==old.public() and proof.get("successor_absent") is True)


def _resolved(a,key):
    row=a.conn.execute("SELECT data FROM v2_account_recovery WHERE binding_key=? AND state='done'",("repin-resolved:"+key,)).fetchone()
    if not row: return False
    try:
        resolution=json.loads(row[0])
        if resolution.get("disposition")=="confirmed_non_split":
            original=a.conn.execute("SELECT data FROM v2_account_recovery WHERE binding_key=?",(key,)).fetchone()
            state=json.loads(original[0]) if original else {}
            if resolution.get("operation_key")!=key or not _non_split_status(state,resolution["host_status"]): raise ValueError()
            return True
        reference=resolution["resolved_by"]
        child=a.conn.execute("SELECT data FROM v2_account_recovery WHERE binding_key=? AND state='done'",(reference,)).fetchone()
        state=json.loads(child[0]) if child else {}
        if (not reference.startswith(PREFIX) or state.get("status")!="done"
                or _key(state["arguments"])!=reference or key not in state.get("resolved_predecessors",[])
                or state.get("result",{}).get("outcome")!="repinned"): raise ValueError()
    except (ValueError,KeyError,TypeError): raise AccountError("account_repin_receipt_invalid") from None
    return True


def fenced(a,binding):
    value={"host":binding.host,"provider":binding.provider,"account":binding.alias}
    return any(s.get("status")!="done" and not _resolved(a,k) for k,s in _states(a,value).items())


def _ancestry(a,value):
    states=_states(a,value); chain=[]; seen={_key(value)}
    parent=value.get("reconcile_of")
    while parent:
        key=_key(dict(value,operation_id=parent))
        if key in seen or key not in states or _resolved(a,key): raise AccountError("account_repin_lineage_invalid")
        seen.add(key); state=states[key]
        if state.get("status")!="parked": raise AccountError("account_repin_predecessor_not_parked")
        chain.append(state)
        parent=state["arguments"].get("reconcile_of")
    return chain


def _same_origin(before,after):
    return all(before.get(k)==after.get(k) for k in ("binding","health_revision","default","policy"))


def _begin(a,value,now):
    with transaction(a.conn):
        prior=_read(a,value)
        if prior and (prior["status"]=="done" or _resolved(a,_key(value))): return prior,False
        # A stale retry may inspect only its exact journal. Do not refresh its snapshot.
        if prior and current(a)!=prior["snapshot"]["pin"]: return prior,False
        snapshot=_state_snapshot(a,value,now)
        if prior:
            before=dict(prior["snapshot"]); before["hold"]=snapshot["hold"]
            if before!=snapshot: raise AccountError("account_repin_state_changed")
            prior=dict(prior,snapshot=snapshot,status="pending")
            _write(a,value,prior)
            return prior,False
        ancestors=_ancestry(a,value)
        ancestor_keys={_key(s["arguments"]) for s in ancestors}
        if any(s.get("status")!="done" and not _resolved(a,k) and k not in ancestor_keys
               for k,s in _states(a,value).items()):
            raise AccountError("account_repin_reconciliation_required")
        if ancestors and any(not _same_origin(s["snapshot"],snapshot) for s in ancestors):
            raise AccountError("account_repin_state_changed")
        old=Binding(**snapshot["binding"])
        state={"arguments":value,"status":"pending","snapshot":snapshot,"old_key":old.key,
               "new_key":":".join((old.host,old.provider,old.alias,value["new_generation"]))}
        _write(a,value,state)
        return state,True


def _save_host(a,value,receipt,reason=None):
    with transaction(a.conn):
        state=_read(a,value)
        if not state or state["status"]=="done" or _resolved(a,_key(value)): return state
        state=dict(state)
        if receipt is not None: state["host_receipt"]=receipt
        if reason: state.update(status="parked",park_reason=reason)
        _write(a,value,state)
        return state


def _resolve_non_split(a,value,status):
    with transaction(a.conn):
        state=_read(a,value)
        if not state or state["status"]=="done" or _resolved(a,_key(value)): return
        if not _non_split_status(state,status): return
        old=Binding(**state["snapshot"]["binding"])
        row=a._cell(old.host,old.provider,old.alias)
        selected=a._available(row)
        default=a.conn.execute("SELECT * FROM v2_account_defaults WHERE host=? AND provider=?",(old.host,old.provider)).fetchone()
        now={"binding":asdict(selected),"health_revision":row["health_revision"],
             "default":dict(default) if default else None,"policy":a.policy}
        if not _same_origin(state["snapshot"],now): return
        # This disposition proves that no host/central split exists; it imports no binding.
        a.conn.execute("INSERT INTO v2_account_recovery VALUES (?,?,?)",("repin-resolved:"+_key(value),"done",
            canonical({"disposition":"confirmed_non_split","operation_key":_key(value),"host_status":status})))


async def _inspect_non_split(executor,state,auth):
    value=state["arguments"]
    if (value.get("reconcile_of") or state["status"]=="done"
            or state.get("host_receipt",{}).get("outcome") in {"ok","no_op"}): return
    try:
        old=Binding(**state["snapshot"]["binding"])
        status=await executor.execute(old,"repin-status","repin:"+value["operation_id"],
            value["new_generation"],pin=state["snapshot"]["pin"])
        await executor.runtime._front_desk(auth)
        await executor.runtime.run(lambda a:_resolve_non_split(a,value,status),settle_on_cancel=True)
    except AccountError:
        pass  # Offline, conflicting or uncertain evidence retains its fence.


def _host_id(value):
    return hashlib.sha256(("repin:"+value["operation_id"]).encode()).hexdigest()


def _link(state):
    value=state["arguments"]
    return {"operation_id":_host_id(value),"old":state["snapshot"]["binding"],
            "arguments":value,"pin":state["snapshot"]["pin"],"receipt":state.get("host_receipt"),
            "consumer_code_identity":state["snapshot"]["consumer_code_identity"]}


def _links(states):
    # The newest authenticated reconcile result already contains the complete ancestry.
    newest=states[0]
    receipt=newest.get("host_receipt") or {}
    if newest["arguments"].get("reconcile_of"):
        if receipt.get("state")!="done" or receipt.get("outcome")!="ok" or not receipt.get("links"):
            raise AccountError("account_repin_lineage_invalid")
        return receipt["links"]
    return [_link(newest)]


async def _recover_links(executor,ancestors):
    links=[]
    for state in reversed(ancestors):
        value=state["arguments"];receipt=state.get("host_receipt") or {}
        if receipt.get("state")!="done" or receipt.get("outcome") not in {"ok","no_op"}:
            old=Binding(**state["snapshot"]["binding"])
            extra={"reconcile":{"root":asdict(old),"links":links}} if value.get("reconcile_of") else {}
            receipt=await executor.execute(old,"repin-reconcile-status" if extra else "repin-status",
                "repin:"+value["operation_id"],value["new_generation"],pin=state["snapshot"]["pin"],**extra)
            state=await executor.runtime.run(lambda a:_save_host(a,value,receipt),settle_on_cancel=True)
        if receipt.get("state")!="done" or receipt.get("outcome") not in {"ok","no_op"}:
            raise AccountError("account_repin_lineage_unresolved")
        candidate=_links([state])
        if links and candidate[:len(links)]!=links: raise AccountError("account_repin_lineage_invalid")
        links=candidate
    return links


def _successor(old,value,receipt,pin,*,consumer_identity=None):
    from _shared.provider_cli_release import SHA256
    if consumer_identity is not None and (not isinstance(consumer_identity,str) or not SHA256.fullmatch(consumer_identity)):
        raise AccountError("account_repin_receipt_invalid")
    if (not isinstance(receipt,dict) or receipt.get("state")!="done" or receipt.get("outcome") not in {"ok","no_op"}
            or receipt.get("expected_generation")!=old.generation or receipt.get("generation")!=value["new_generation"]
            or receipt.get("provider")!=old.provider or receipt.get("alias")!=old.alias):
        raise AccountError("account_repin_receipt_invalid")
    new=Binding.from_public(receipt.get("binding"))
    stable=("host","provider","alias","home","provider_id","pool_id","platform","enabled","healthy","transcript_root",
            "observer_executable","observer_executable_sha256")
    if any(getattr(old,k)!=getattr(new,k) for k in stable) or not new.enabled or not new.healthy or new.default_revision!=0:
        raise AccountError("account_repin_binding_invalid")
    no_op=receipt["outcome"]=="no_op"
    if new.generation!=(old.generation if no_op else value["new_generation"]): raise AccountError("account_repin_binding_invalid")
    proof=receipt.get("proof") or {}
    from provider_cli_artifact import code_identity
    artifact=pin["providers"][old.provider]["artifacts"].get(proof.get("platform"))
    from provider_cli_pin import version
    platform_matches=(str(proof.get("platform") or "").startswith("darwin-") if new.platform=="macos"
                      else str(proof.get("platform") or "").startswith("linux-"))
    if (proof.get("outcome")!="ok" or not artifact or artifact!=proof.get("artifact") or proof.get("binding_key")!=new.key
            or not platform_matches
            or proof.get("pin_revision")!=pin["revision"] or proof.get("pin_digest")!=digest(pin)
            or proof.get("code_identity")!=(consumer_identity or code_identity()) or artifact["native_sha256"]!=new.executable_sha256
            or version(new.provider,new.version)!=pin["providers"][new.provider]["version"]):
        raise AccountError("account_repin_receipt_invalid")
    if not no_op and not _valid_observation(new,receipt.get("observation")):
        raise AccountError("account_repin_observation_invalid")
    # Keep the private host profile from the authenticated old binding.
    return replace(new,profile_ref=old.profile_ref),no_op


def _commit(a,value,state,receipt,now):
    with transaction(a.conn):
        prior=_read(a,value)
        if prior and prior["status"]=="done": return prior["result"]
        if prior!=state or _state_snapshot(a,value,now)!=state["snapshot"]:
            raise AccountError("account_repin_state_changed")
        old=Binding(**state["snapshot"]["binding"])
        new,no_op=_successor(old,value,receipt,state["snapshot"]["pin"])
        if not no_op:
            a._register(new)
            if not a._observe(new.key,receipt["observation"],now=now): raise AccountError("account_repin_observation_invalid")
            a.conn.execute("INSERT INTO v2_account_recovery VALUES (?,?,?)",
                (LINEAGE+old.key,"done",canonical({"operation_key":_key(value),"old_key":old.key,"new_key":new.key})))
        a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",("provider_cli.verified:"+new.key,
            canonical({"proof":receipt["proof"],"verified_at":now})))
        result={"outcome":"no_op" if no_op else "repinned","operation_id":value["operation_id"],"binding":new.public()}
        _write(a,value,{**state,"status":"done","new_key":new.key,"result":result,"host_receipt":receipt})
        return result


def _commit_reconcile(a,value,state,receipt,now):
    with transaction(a.conn):
        prior=_read(a,value)
        if prior and prior["status"]=="done": return prior["result"]
        if prior!=state or _state_snapshot(a,value,now)!=state["snapshot"]:
            raise AccountError("account_repin_state_changed")
        ancestors=_ancestry(a,value)
        if any(not _same_origin(s["snapshot"],state["snapshot"]) for s in ancestors):
            raise AccountError("account_repin_state_changed")
        links=receipt.get("links")
        old=Binding(**state["snapshot"]["binding"])
        if receipt.get("state")!="done" or receipt.get("outcome")!="ok" or not links:
            raise AccountError("account_repin_lineage_invalid")
        verified=[];seen={old.key}
        for link in links:
            if link.get("old")!=asdict(old): raise AccountError("account_repin_lineage_invalid")
            new,no_op=_successor(old,link["arguments"],link["receipt"],link["pin"],consumer_identity=link["consumer_code_identity"])
            if no_op or new.key in seen or a.bound(new.key): raise AccountError("account_repin_lineage_invalid")
            verified.append((old,new,link));seen.add(new.key);old=new
        final=old
        if receipt.get("binding")!=final.public(): raise AccountError("account_repin_lineage_invalid")
        proof=receipt.get("current_artifact") or {}
        # Retained successful identity/minimal-turn proof has no age limit for ancestry.
        # It is never relabelled fresh: readiness also requires this new byte verification.
        from provider_cli_artifact import validate_proof
        validate_proof(final,state["snapshot"]["pin"],proof)
        for old,new,link in verified:
            a._register(new)
            if not a._observe(new.key,link["receipt"]["observation"],now=now):
                raise AccountError("account_repin_observation_invalid")
            evidence={"operation_key":_key(value),"old_key":old.key,"new_key":new.key,"evidence":link}
            a.conn.execute("INSERT INTO v2_account_recovery VALUES (?,?,?)",(LINEAGE+old.key,"done",canonical(evidence)))
        a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",("provider_cli.verified:"+final.key,
            canonical({"proof":proof,"verified_at":now,"retained_identity":links[-1]["receipt"]["proof"]})))
        result={"outcome":"repinned","operation_id":value["operation_id"],"binding":final.public()}
        _write(a,value,{**state,"status":"done","new_key":final.key,"result":result,"host_receipt":receipt,"lineage":links,
            "resolved_predecessors":[_key(s["arguments"]) for s in ancestors]})
        for ancestor in ancestors:
            a.conn.execute("INSERT INTO v2_account_recovery VALUES (?,?,?)",("repin-resolved:"+_key(ancestor["arguments"]),
                "done",canonical({"resolved_by":_key(value)})))
        return result


async def repin(executor,message,auth):
    runtime=executor.runtime
    await runtime._front_desk(auth)
    value=_payload(message)
    async with executor._host_locks.setdefault(value["host"],asyncio.Lock()):
        await runtime._front_desk(auth)
        known=await runtime.run(lambda a:_read(a,value))
        if known and known["status"]=="done": return known["result"]
        # A fresh FD attempt may resolve an earlier conclusively failed, unchanged-G0 operation.
        pending=await runtime.run(lambda a:[s for k,s in _states(a,value).items()
            if s.get("status")!="done" and not _resolved(a,k)])
        for previous in pending: await _inspect_non_split(executor,previous,auth)
        await runtime._front_desk(auth)
        state,created=await runtime.run(lambda a:_begin(a,value,runtime.clock()),settle_on_cancel=True)
        if state["status"]=="done": return state["result"]
        old=Binding(**state["snapshot"]["binding"])
        pin=state["snapshot"]["pin"]
        op="repin:"+value["operation_id"]
        receipt=None
        try:
            if value.get("reconcile_of"):
                ancestors=await runtime.run(lambda a:_ancestry(a,value))
                links=await _recover_links(executor,ancestors)
                context={"root":asdict(old),"links":links}
                receipt=await executor.execute(old,"repin-reconcile" if created else "repin-reconcile-status",
                    op,value["new_generation"],pin=pin,reconcile=context)
                if not created and receipt.get("state")=="absent" and await runtime.run(current)==pin:
                    # Journal absence under the host lock proves no native action began.
                    receipt=await executor.execute(old,"repin-reconcile",op,value["new_generation"],pin=pin,reconcile=context)
            else:
                receipt=await executor.execute(old,"repin" if created else "repin-status",op,value["new_generation"],pin=pin)
                if not created and receipt.get("state")=="absent" and await runtime.run(current)==pin:
                    receipt=await executor.execute(old,"repin",op,value["new_generation"],pin=pin)
            state=await runtime.run(lambda a:_save_host(a,value,receipt),settle_on_cancel=True)
            if await runtime.run(current)!=pin: raise AccountError("provider_cli_pin_stale")
            if receipt.get("state")=="done" and receipt.get("outcome") not in {"ok","no_op"}:
                raise AccountError("account_repin_execution_failed")
            if receipt.get("state")!="done":
                parked=await runtime.run(lambda a:_save_host(a,value,receipt,"execution_unresolved"),settle_on_cancel=True)
                await _inspect_non_split(executor,parked,auth)
                return {"outcome":"execution_unresolved","operation_id":value["operation_id"]}
            if value.get("reconcile_of"):
                # Read the immutable host result and current selection again; never another target turn.
                confirmation=await executor.execute(old,"repin-reconcile-verify",op,value["new_generation"],pin=pin,reconcile=context)
                if confirmation!=receipt: raise AccountError("account_repin_state_changed")
            await runtime._front_desk(auth)
            async with runtime.pin_lock:
                commit=_commit_reconcile if value.get("reconcile_of") else _commit
                return await runtime.run(lambda a:commit(a,value,state,receipt,runtime.clock()),settle_on_cancel=True)
        except (AccountError,asyncio.CancelledError) as exc:
            parked=await runtime.run(lambda a:_save_host(a,value,receipt,str(exc) or "cancelled"),settle_on_cancel=True)
            if not isinstance(exc,asyncio.CancelledError):
                await runtime._front_desk(auth)
                await _inspect_non_split(executor,parked,auth)
            raise


def resume_binding(a,prior):
    """Follow only immutable successful repin links; never infer from home strings."""
    old=Binding.from_public(prior)
    original=a.bound(old.key)
    if original is None or original.public()!=dict(old.public(),default_revision=0):
        raise AccountError("account_resume_lineage_invalid",next_action="checkpoint handoff or authorized recovery")
    current_binding=a._available(a._cell(old.host,old.provider,old.alias))
    seen=set()
    while old.key!=current_binding.key:
        if old.key in seen: raise AccountError("account_resume_lineage_invalid")
        seen.add(old.key)
        link=a.conn.execute("SELECT data FROM v2_account_recovery WHERE binding_key=? AND state='done'",(LINEAGE+old.key,)).fetchone()
        if not link: raise AccountError("account_resume_lineage_invalid",next_action="checkpoint handoff or authorized recovery")
        link=json.loads(link[0])
        row=a.conn.execute("SELECT data FROM v2_account_recovery WHERE binding_key=? AND state='done'",(link.get("operation_key"),)).fetchone()
        if not row or not str(link.get("operation_key","")).startswith(PREFIX): raise AccountError("account_resume_lineage_invalid")
        state=json.loads(row[0])
        saved=a.bound(old.key)
        new=a.bound(link["new_key"])
        if state.get("status")!="done" or saved is None or new is None or link.get("old_key")!=old.key:
            raise AccountError("account_resume_lineage_invalid")
        if state.get("arguments",{}).get("reconcile_of"):
            evidence=link.get("evidence")
            if not evidence or evidence not in state.get("lineage",[]) or evidence.get("old")!=asdict(saved):
                raise AccountError("account_resume_lineage_invalid")
            validated,no_op=_successor(saved,evidence["arguments"],evidence["receipt"],evidence["pin"],consumer_identity=evidence["consumer_code_identity"])
            if no_op or validated!=new: raise AccountError("account_resume_lineage_invalid")
        elif (state.get("old_key")!=old.key or state.get("new_key")!=link.get("new_key")
                or state.get("result",{}).get("outcome")!="repinned"
                or asdict(saved)!=state.get("snapshot",{}).get("binding")
                or new.public()!=state.get("result",{}).get("binding")):
            raise AccountError("account_resume_lineage_invalid")
        if any(getattr(saved,k)!=getattr(new,k) for k in
               ("host","provider","alias","home","transcript_root","provider_id","pool_id","platform","profile_ref")):
            raise AccountError("account_resume_lineage_invalid")
        old=new
    return replace(current_binding,default_revision=0)
