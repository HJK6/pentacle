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
    if message.get("expected_origin_digest") is not None:
        expected=message["expected_origin_digest"]
        if not value.get("reconcile_of") or not isinstance(expected,str) or not SHA256.fullmatch(expected):
            raise AccountError("account_repin_request_invalid")
        value["expected_origin_digest"]=expected
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


def _states(a,value):
    from account_repin_state import rows
    return rows(a,PREFIX,value)


def _resolved(a,key):
    from account_repin_state import resolved
    return bool(resolved(a,key))


def fenced(a,binding):
    from account_repin_state import hatch_causes
    if hatch_causes(a,{"host":binding.host,"provider":binding.provider,"account":binding.alias}):return True
    value={"host":binding.host,"provider":binding.provider,"account":binding.alias}
    return any(s.get("status")!="done" and not _resolved(a,k) for k,s in _states(a,value).items())


def _save_host(a,value,receipt,reason=None):
    with transaction(a.conn):
        state=_read(a,value)
        if not state or state["status"]=="done" or _resolved(a,_key(value)): return state
        state=dict(state)
        if receipt is not None: state["host_receipt"]=receipt
        if reason: state.update(status="parked",park_reason=reason)
        _write(a,value,state)
        return state


def _host_id(value):
    return hashlib.sha256(("repin:"+value["operation_id"]).encode()).hexdigest()


def _link(state):
    value=state["arguments"]
    return {"operation_id":_host_id(value),"old":state["snapshot"]["binding"],
            "arguments":value,"pin":state["snapshot"]["pin"],"receipt":state.get("host_receipt"),
            "consumer_code_identity":state["snapshot"]["consumer_code_identity"]}


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


async def repin(executor,message,auth):
    from account_repin_state import repin as recover
    return await recover(executor,message,auth)


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
        if not row or not str(link.get("operation_key","")).startswith((PREFIX,"repin-hatch:")): raise AccountError("account_resume_lineage_invalid")
        state=json.loads(row[0])
        saved=a.bound(old.key)
        new=a.bound(link["new_key"])
        if state.get("status")!="done" or saved is None or new is None or link.get("old_key")!=old.key:
            raise AccountError("account_resume_lineage_invalid")
        if state.get("arguments",{}).get("reconcile_of") or str(link.get("operation_key","")).startswith("repin-hatch:"):
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
