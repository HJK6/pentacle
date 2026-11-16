"""Explicit FD-only health transition with a durable revision and receipt fence.

The host operation is still a single read-only bound observe. Nothing here logs
in, changes a generation/default, or automatically restores an unhealthy cell.
"""
from __future__ import annotations
import asyncio
from dataclasses import asdict
import hashlib
import json

from account_store import number, transaction
from provider_accounts import AccountError, Binding, _component, policy_manages


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def _payload(message):
    result={key:message.get(key) for key in ("action","host","provider","account","login_generation",
        "expected_revision","reason")}
    for key in ("host","account","login_generation"): _component(result[key])
    if result["provider"] not in {"codex","claude"}: raise AccountError("account_request_invalid")
    if result["login_generation"]=="active": raise AccountError("account_login_generation_stale")
    if type(result["expected_revision"]) is not int or result["expected_revision"]<0:
        raise AccountError("account_health_revision_invalid")
    for text in (message.get("operation_id"),result["reason"]):
        if not isinstance(text,str) or not 1<=len(text)<=160 or any(ord(c)<32 or ord(c)>126 for c in text):
            raise AccountError("account_request_invalid")
    return result


def _receipt(row, *, replay=True):
    data=json.loads(row["data"])
    result=data.get("result") or {"outcome":"revalidation_unresolved","operation_id":row["operation_id"]}
    return dict(result,receipt_replay=replay)


def _existing(ledger, operation_id, payload_hash):
    row=ledger.conn.execute("SELECT * FROM v2_account_health_revalidations WHERE operation_id=?",(operation_id,)).fetchone()
    if row is not None and row["payload_hash"]!=payload_hash:
        raise AccountError("account_revalidation_id_conflict")
    return _receipt(row) if row is not None else None


def _eligible(ledger, payload):
    host=payload["host"]
    if not policy_manages(ledger.policy,host,payload["provider"]):
        raise AccountError("account_feature_off")
    row=ledger._cell(host,payload["provider"],payload["account"])
    if row is None: raise AccountError("account_generation_unknown")
    binding=Binding(**json.loads(row["data"]))
    if not row["active"] or binding.generation!=payload["login_generation"]:
        raise AccountError("account_login_generation_stale")
    if not binding.enabled: raise AccountError("account_disabled")
    if row["health_revision"]!=payload["expected_revision"]:
        raise AccountError("account_health_revision_stale")
    if row["health"]!="identity_conflict": raise AccountError("account_health_not_conflicted")
    return binding


def _begin(ledger, payload, operation_id, payload_hash, epoch, auth, now):
    with transaction(ledger.conn):
        prior=_existing(ledger,operation_id,payload_hash)
        if prior is not None: return {"receipt":prior}
        binding=_eligible(ledger,payload)
        data={"binding_digest":_digest(asdict(binding)),"expected_revision":payload["expected_revision"],
            "reason":payload["reason"],"started_at":now,
            "requested_by":{"stream_id":auth["stream_id"],"generation":auth["session_generation"]}}
        ledger.conn.execute("INSERT INTO v2_account_health_revalidations "
            "(operation_id,payload_hash,binding_key,epoch,state,data) VALUES (?,?,?,?,?,?)",
            (operation_id,payload_hash,binding.key,epoch,"pending",json.dumps(data)))
        return {"binding":binding}


def interrupt_pending(ledger, epoch, *, now):
    """Owned runtime startup only; Store opens never interrupt a live intent."""
    with transaction(ledger.conn):
        rows=ledger.conn.execute("SELECT * FROM v2_account_health_revalidations WHERE state='pending' AND epoch<>?",(epoch,)).fetchall()
        for row in rows:
            data=json.loads(row["data"])
            data.update(interrupted_by_epoch=epoch,finished_at=now,
                result={"outcome":"revalidation_interrupted","operation_id":row["operation_id"]})
            ledger.conn.execute("UPDATE v2_account_health_revalidations SET state='interrupted',data=? WHERE operation_id=?",
                (json.dumps(data),row["operation_id"]))


def _finite(value):
    try: return number(value)
    except (OverflowError,TypeError,ValueError): return False


def _valid_observation(account, value):
    if not isinstance(value,dict): return False
    if (value.get("provider_id")!=account.provider_id or value.get("pool_id")!=account.pool_id
            or value.get("auth_mode")!="subscription" or value.get("collection")!="ok"
            or not _finite(value.get("observed_at"))): return False
    windows=value.get("windows")
    return isinstance(windows,dict) and bool(windows) and all(isinstance(window,dict)
        and _finite(window.get("pct")) and 0<=window["pct"]<=100
        and _finite(window.get("reset_at")) and window["reset_at"]>0
        and isinstance(window.get("reset_id"),str) and bool(window["reset_id"])
        for window in windows.values())


def _finish(ledger, payload, operation_id, payload_hash, epoch, result, now):
    with transaction(ledger.conn):
        row=ledger.conn.execute("SELECT * FROM v2_account_health_revalidations WHERE operation_id=?",(operation_id,)).fetchone()
        if row is None or row["payload_hash"]!=payload_hash: raise AccountError("account_revalidation_unresolved")
        if row["state"]!="pending": return _receipt(row)
        if row["epoch"]!=epoch: raise AccountError("account_revalidation_unresolved")
        data=json.loads(row["data"]); outcome=result.get("outcome","provider_probe_failed")
        applied_revision=None
        if outcome=="ok":
            try:
                account=_eligible(ledger,payload)
                if _digest(asdict(account))!=data["binding_digest"]:
                    raise AccountError("account_launch_binding_mismatch")
                provenance=result.get("lineage_provenance")
                observed=Binding.from_public(provenance["host_binding"]) if provenance else account
                if provenance and (not provenance.get("links") or provenance.get("root")!=asdict(account)):
                    outcome="provider_probe_failed"
                if not _valid_observation(observed,result.get("observation")):
                    outcome="provider_probe_failed"
            except AccountError:
                outcome="account_revalidation_state_changed"
            if outcome=="ok":
                changed=ledger.conn.execute("UPDATE v2_account_bindings SET health='healthy',health_revision=health_revision+1 "
                    "WHERE binding_key=? AND active=1 AND health='identity_conflict' AND health_revision=?",
                    (row["binding_key"],payload["expected_revision"])).rowcount
                if changed!=1: raise AccountError("account_revalidation_state_changed")
                applied_revision=payload["expected_revision"]+1
                data.update(observed_at=result["observation"]["observed_at"],evidence_digest=_digest(result["observation"]))
                if provenance:data["lineage_provenance"]=provenance
                outcome="health_revalidated"
        receipt={"outcome":outcome,"operation_id":operation_id,"applied_revision":applied_revision}
        if applied_revision is not None: receipt["health_at_operation"]="healthy"
        data.update(result=receipt,finished_at=now)
        state="interrupted" if outcome=="revalidation_interrupted" else "done"
        ledger.conn.execute("UPDATE v2_account_health_revalidations SET state=?,data=? WHERE operation_id=?",
            (state,json.dumps(data),operation_id))
        return dict(receipt,receipt_replay=False)


async def revalidate_health(executor, message, auth):
    runtime=executor.runtime
    # Defense in depth for internal callers; every replay authenticates too.
    await runtime._front_desk(auth)
    payload=_payload(message); operation_id=message["operation_id"]; payload_hash=_digest(payload)
    prior=await runtime.run(lambda ledger:_existing(ledger,operation_id,payload_hash))
    if prior is not None: return prior
    async with executor._host_locks.setdefault(payload["host"],asyncio.Lock()):
        await runtime._front_desk(auth)  # a queued old FD generation cannot start work
        try:
            begun=await runtime.run(lambda ledger:_begin(ledger,payload,operation_id,payload_hash,
                runtime._health_epoch,auth,runtime.clock()))
        except asyncio.CancelledError:
            try:
                await asyncio.shield(runtime.run(lambda ledger:_finish(ledger,payload,operation_id,payload_hash,
                    runtime._health_epoch,{"outcome":"revalidation_interrupted"},runtime.clock())))
            except Exception: pass
            raise
        if "receipt" in begun: return begun["receipt"]
        try:
            from account_repin_state import diagnostic_lineage
            provenance=await diagnostic_lineage(executor,payload,auth)
            if provenance:
                result=await executor.execute(begun["binding"],"observe-lineage",operation_id,reconcile=provenance)
                if result.get("binding_key")!=provenance["host_binding"]["host"]+":"+provenance["host_binding"]["provider"]+":"+provenance["host_binding"]["alias"]+":"+provenance["host_binding"]["generation"]:
                    raise AccountError("account_repin_lineage_invalid")
                result={**result,"lineage_provenance":provenance}
            else:result=await executor._observe_locked(begun["binding"],require_match=True,recovery=True)
            await runtime._front_desk(auth)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(runtime.run(lambda ledger:_finish(ledger,payload,operation_id,payload_hash,
                    runtime._health_epoch,{"outcome":"revalidation_interrupted"},runtime.clock())))
            except Exception: pass  # persisted intent remains unresolved; never health success
            raise
        except Exception:
            result={"outcome":"provider_probe_failed"}
        try:
            return await runtime.run(lambda ledger:_finish(ledger,payload,operation_id,payload_hash,
                runtime._health_epoch,result,runtime.clock()))
        except Exception:
            # A lost audit/storage reply must not trigger another native call.
            return {"outcome":"revalidation_unresolved","operation_id":operation_id}
