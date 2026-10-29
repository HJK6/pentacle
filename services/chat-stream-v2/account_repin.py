"""FD-only same-home successors, using existing account and host receipt storage."""
from __future__ import annotations
import asyncio
from dataclasses import asdict,replace
import json

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
    return snapshot


def _begin(a,value,now):
    with transaction(a.conn):
        prior=_read(a,value)
        if prior and prior["status"]=="done": return prior,False
        snapshot=_state_snapshot(a,value,now)
        if prior:
            # A renewed hold may reconcile this exact host receipt; no native action is repeated.
            before=dict(prior["snapshot"]); before["hold"]=snapshot["hold"]
            if before!=snapshot: raise AccountError("account_repin_state_changed")
            prior=dict(prior,snapshot=snapshot)
            _write(a,value,prior)
            return prior,False
        old=Binding(**snapshot["binding"])
        state={"arguments":value,"status":"pending","snapshot":snapshot,"old_key":old.key,
               "new_key":":".join((old.host,old.provider,old.alias,value["new_generation"]))}
        _write(a,value,state)
        return state,True


def _successor(old,value,receipt,pin):
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
    if (not artifact or artifact!=proof.get("artifact") or proof.get("binding_key")!=new.key
            or not platform_matches
            or proof.get("pin_revision")!=pin["revision"] or proof.get("pin_digest")!=digest(pin)
            or proof.get("code_identity")!=code_identity() or artifact["native_sha256"]!=new.executable_sha256
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


async def repin(executor,message,auth):
    runtime=executor.runtime
    await runtime._front_desk(auth)
    value=_payload(message)
    async with executor._host_locks.setdefault(value["host"],asyncio.Lock()):
        await runtime._front_desk(auth)
        state,created=await runtime.run(lambda a:_begin(a,value,runtime.clock()),settle_on_cancel=True)
        if state["status"]=="done": return state["result"]
        old=Binding(**state["snapshot"]["binding"])
        pin=state["snapshot"]["pin"]
        op="repin:"+value["operation_id"]
        receipt=await executor.execute(old,"repin" if created else "repin-status",op,value["new_generation"],pin=pin)
        if not created and receipt.get("state")=="absent":
            # Absence is proven under the same host registry lock; no native attempt began.
            receipt=await executor.execute(old,"repin",op,value["new_generation"],pin=pin)
        if receipt.get("state")!="done": return {"outcome":"execution_unresolved","operation_id":value["operation_id"]}
        await runtime._front_desk(auth)
        async with runtime.pin_lock:
            return await runtime.run(lambda a:_commit(a,value,state,receipt,runtime.clock()),settle_on_cancel=True)


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
        if (state.get("status")!="done" or state.get("old_key")!=old.key or state.get("new_key")!=link.get("new_key")
                or state.get("result",{}).get("outcome")!="repinned"): raise AccountError("account_resume_lineage_invalid")
        saved=a.bound(old.key)
        if saved is None or asdict(saved)!=state.get("snapshot",{}).get("binding"):
            raise AccountError("account_resume_lineage_invalid")
        new=a.bound(link["new_key"])
        if new is None or new.public()!=state.get("result",{}).get("binding"):
            raise AccountError("account_resume_lineage_invalid")
        if any(getattr(saved,k)!=getattr(new,k) for k in
               ("host","provider","alias","home","transcript_root","provider_id","pool_id","platform","profile_ref")):
            raise AccountError("account_resume_lineage_invalid")
        old=new
    return replace(current_binding,default_revision=0)
