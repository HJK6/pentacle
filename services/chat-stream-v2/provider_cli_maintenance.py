"""Durable home input fencing on the existing privileged spawn-freeze RPC.

The spawn hold may expire. The input fence only ends by an authenticated,
evidence-backed compare-and-clear; it never acquires pin preparation authority.
"""
import asyncio
import json
import math

from account_store import transaction
from provider_accounts import AccountError, Binding, _component
from provider_cli_pin import canonical, digest

PREFIX="provider_cli.maintenance_input:"
RECEIPT="provider-cli-maintenance:"
HOLD_RECEIPT="provider-cli-maintenance-hold:"
FIELDS={"input_provider","input_account","expected_login_generation","maintenance_id","expected_hold_digest"}
# Queueing plus the existing proof-age window fits the unchanged 240s client.
CLEAR_MUTEX_WAIT_SECONDS=90
CLEAR_PROOF_SECONDS=120


def scope(binding):
    return digest({"host":binding.host,"provider":binding.provider,"home":binding.home})


def hold(a,host):
    row=a.conn.execute("SELECT * FROM v2_spawn_admission_hold WHERE host=?",(host,)).fetchone()
    return dict(row) if row else None


def hold_digest(value):
    return digest(value) if value is not None else "absent"


def slot(a,host):
    row=a.conn.execute("SELECT v FROM kv WHERE k=?",(PREFIX+host,)).fetchone()
    if row is None:return None
    try:
        value=json.loads(row[0])
        binding=Binding.from_public(value["binding"])
        if (binding.host!=host or value["scope"]!=scope(binding) or not isinstance(value["arguments"],dict)
                or not value["arguments"]["maintenance_id"] or not isinstance(value["sessions"],list)
                or not isinstance(value["hold"],dict) or value["state"] not in {"settling","ready"}):raise ValueError()
        return value
    except (KeyError,TypeError,ValueError,AccountError):
        raise AccountError("provider_cli_maintenance_corrupt",host=host) from None


def status(a,host):
    current=slot(a,host);held=hold(a,host)
    return {"slot":current,"maintenance_digest":digest(current) if current else "absent",
            "hold":held,"hold_digest":hold_digest(held)}


def matches(value,row):
    raw=row.get("account_binding")
    if raw:
        binding=Binding.from_public(raw)
        if scope(binding)==value["scope"]:return True
    sid=row.get("stream_id") or str(row.get("host") or "")+":"+str(row.get("session_name") or "")
    return any(r["stream_id"]==sid and r["generation"]==row.get("session_generation") for r in value["sessions"])


def assert_input(a,row):
    host=row.get("host") or str(row.get("stream_id") or "").partition(":")[0]
    current=slot(a,host)
    if current and matches(current,row):
        error=AccountError("provider_cli_input_maintenance",host=host)
        # This narrow code owns these derived, typed public delivery fields;
        # keep the general AccountError provider-output whitelist unchanged.
        error.context.update(maintenance_id=current["arguments"]["maintenance_id"],target_stream_id=row.get("stream_id"),
            retry_after_maintenance=True,delivery="not_landed",attempts=0)
        raise error


def require_mutation(a,binding,now):
    current=slot(a,binding.host)
    if current and scope(binding)==current["scope"]:
        held=hold(a,binding.host)
        if current["state"]!="ready" or not held or held["held_until"]<=now or not owned_hold(a,current,held):
            raise AccountError("provider_cli_maintenance_not_ready")


def owned_hold(a,current,held):
    if held==current["hold"]:return True
    from account_repin_state import get
    receipt=get(a,HOLD_RECEIPT+digest(current)+":"+hold_digest(held))
    return receipt=={"state":"done","slot_digest":digest(current),"hold":held}


def _operation_id(value):
    if not isinstance(value,str) or not 1<=len(value)<=160 or any(ord(c)<32 or ord(c)>126 for c in value):
        raise AccountError("provider_cli_maintenance_request_invalid")
    return value


def _arguments(message,host):
    allowed=FIELDS|{"type","request_id","protocol","host","reason","ttl_s","local_admin_token",
        "stream_id","from_stream_id","stream_token"}
    if any(not str(k).startswith("_") and k not in allowed for k in message):
        raise AccountError("provider_cli_maintenance_request_invalid")
    if not FIELDS<=message.keys():raise AccountError("provider_cli_maintenance_request_invalid")
    provider=message["input_provider"]
    if provider not in {"codex","claude"}:raise AccountError("provider_cli_maintenance_request_invalid")
    for k in ("input_account","expected_login_generation"):_component(message[k])
    _operation_id(message["maintenance_id"])
    ttl=message.get("ttl_s",900)
    if isinstance(ttl,bool) or not isinstance(ttl,(float,int)) or not math.isfinite(ttl) or not 0<ttl<=3600:
        raise AccountError("provider_cli_maintenance_request_invalid")
    reason=message.get("reason","")
    if not isinstance(reason,str) or not reason.strip() or len(reason)>512:raise AccountError("provider_cli_maintenance_request_invalid")
    return {"host":host,**{k:message[k] for k in FIELDS},"reason":reason,"ttl_s":ttl}


def _receipt(a,host,operation):
    from account_repin_state import get
    return get(a,RECEIPT+host+":"+operation)


def _write_receipt(a,host,operation,state):
    from account_repin_state import put
    put(a,RECEIPT+host+":"+operation,state["state"],state)


def _binding(a,args):
    row=a._cell(args["host"],args["input_provider"],args["input_account"])
    if row is None:raise AccountError("account_generation_unknown")
    result=Binding(**json.loads(row["data"]))
    if result.generation!=args["expected_login_generation"]:raise AccountError("account_login_generation_stale")
    return result


def _seats(a,binding):
    result=[]
    for row in a.conn.execute("SELECT s.host,s.session_name,s.account_binding,g.generation FROM sessions s "
            "LEFT JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name WHERE s.host=? AND s.status='open'",(binding.host,)):
        if row["account_binding"] and scope(Binding.from_public(json.loads(row["account_binding"])))==scope(binding):
            result.append({"stream_id":row["host"]+":"+row["session_name"],"generation":row["generation"]})
    return sorted(result,key=lambda r:r["stream_id"])


def _begin(a,args,binding,now,owner):
    with transaction(a.conn):
        prior=_receipt(a,binding.host,args["maintenance_id"])
        if prior:
            if prior["arguments"]!=args:raise AccountError("provider_cli_maintenance_id_conflict")
            return prior
        if slot(a,binding.host):raise AccountError("provider_cli_maintenance_busy")
        held=hold(a,binding.host)
        if hold_digest(held)!=args["expected_hold_digest"]:raise AccountError("provider_cli_maintenance_hold_stale")
        # A legacy/foreign hold is never overwritten or silently adopted.
        if held is not None:raise AccountError("provider_cli_maintenance_foreign_hold")
        if _binding(a,args)!=binding:raise AccountError("account_login_generation_stale")
        held={"host":binding.host,"held_until":now+args["ttl_s"],"reason":args["reason"],"set_at":now}
        a.conn.execute("INSERT INTO v2_spawn_admission_hold(host,held_until,reason,set_at) VALUES (?,?,?,?)",
            tuple(held[k] for k in ("host","held_until","reason","set_at")))
        value={"arguments":args,"binding":binding.public(),"scope":scope(binding),"hold":held,
            "sessions":_seats(a,binding),"owner":owner,"state":"settling"}
        a.conn.execute("INSERT INTO kv VALUES (?,?)",(PREFIX+binding.host,canonical(value)))
        _write_receipt(a,binding.host,args["maintenance_id"],value)
        return value


def _ready(a,value,now):
    with transaction(a.conn):
        if slot(a,value["binding"]["host"])!=value:raise AccountError("provider_cli_maintenance_stale")
        held=hold(a,value["binding"]["host"])
        if not held or held["held_until"]<=now or not owned_hold(a,value,held):
            raise AccountError("provider_cli_maintenance_not_ready")
        if a.conn.execute("SELECT 1 FROM v2_stream_reservations WHERE host=? LIMIT 1",(value["binding"]["host"],)).fetchone():
            raise AccountError("provider_cli_maintenance_launch_pending")
        value={**value,"state":"ready"}
        a.conn.execute("UPDATE kv SET v=? WHERE k=?",(canonical(value),PREFIX+value["binding"]["host"]))
        _write_receipt(a,value["binding"]["host"],value["arguments"]["maintenance_id"],value)
        if held!=value["hold"]:
            from account_repin_state import put
            put(a,HOLD_RECEIPT+digest(value)+":"+hold_digest(held),"done",
                {"state":"done","slot_digest":digest(value),"hold":held},immutable=True)
        return value


async def freeze(runtime,message,host):
    args=_arguments(message,host)
    prior=await runtime.run(lambda a:_receipt(a,host,args["maintenance_id"]))
    if prior:
        if prior["arguments"]!=args:raise AccountError("provider_cli_maintenance_id_conflict")
        if prior["state"] in {"cleared","ready"}:
            actual=await runtime.run(lambda a:status(a,host))
            return {"type":"spawn_freeze.ok","ok":True,"host":host,"receipt":prior,**actual,
                "held_until":actual["hold"]["held_until"] if actual["hold"] else None,
                "reason":actual["hold"]["reason"] if actual["hold"] else None}
    binding=await runtime.run(lambda a:_binding(a,args))
    # One short home writer orders the fence after already-admitted physical
    # actions. Waiting for SSH/creation completion happens after it is released.
    async with runtime.home_inputs(binding).write():
        auth=message.get("_auth_context") or {}
        owner={k:auth.get(k) for k in ("stream_id","session_generation","operator_credential_id","local_admin_verified","operator_authenticated")}
        value=await runtime.run(lambda a:_begin(a,args,binding,runtime.clock(),owner),settle_on_cancel=True)
    if value["state"]=="settling":
        pending=list(getattr(runtime.spawnctl,"_background_spawns",set()))
        if pending:
            _,unfinished=await asyncio.wait(pending,timeout=min(30,args["ttl_s"]))
            if unfinished:raise AccountError("provider_cli_maintenance_launch_pending")
        value=await runtime.run(lambda a:_ready(a,value,runtime.clock()),settle_on_cancel=True)
    return {"type":"spawn_freeze.ok","ok":True,"host":host,"receipt":value,
            **await runtime.run(lambda a:status(a,host)),"held_until":value["hold"]["held_until"],"reason":args["reason"]}


def _clear_snapshot(a,value):
    from account_repin_state import origin,causes,generation_intents
    binding=Binding.from_public(value["binding"])
    cell={"host":binding.host,"provider":binding.provider,"account":binding.alias}
    selected=origin(a,cell)
    current=Binding(**selected["binding"])
    if scope(current)!=value["scope"]:raise AccountError("provider_cli_maintenance_home_changed")
    if causes(a,cell) or generation_intents(a,cell):raise AccountError("account_mutation_unresolved")
    if a.conn.execute("SELECT 1 FROM v2_stream_reservations WHERE host=? LIMIT 1",(binding.host,)).fetchone():
        raise AccountError("provider_cli_maintenance_launch_pending")
    return selected


async def unfreeze(runtime,message,host):
    if runtime.executor is None:
        return await _unfreeze_locked(runtime,message,host)
    mutex=runtime.executor._host_locks.setdefault(host,asyncio.Lock())
    try:
        await asyncio.wait_for(mutex.acquire(),timeout=CLEAR_MUTEX_WAIT_SECONDS)
    except TimeoutError:
        raise AccountError("provider_timeout") from None
    try:
        # Read slot, hold and binding only after queued observations finish.
        # Direct execute calls below do not re-enter this non-reentrant mutex.
        try:
            async with asyncio.timeout(CLEAR_PROOF_SECONDS):
                return await _unfreeze_locked(runtime,message,host)
        except TimeoutError:
            raise AccountError("provider_timeout") from None
    finally:
        mutex.release()


async def _unfreeze_locked(runtime,message,host):
    from account_repin_state import host_status
    from account_revalidation import _valid_observation
    allowed={"type","request_id","protocol","host","local_admin_token","stream_id","from_stream_id","stream_token",
        "maintenance_id","expected_maintenance_digest","expected_hold_digest"}
    if any(not str(k).startswith("_") and k not in allowed for k in message):
        raise AccountError("provider_cli_maintenance_request_invalid")
    value=await runtime.run(lambda a:slot(a,host))
    op=message.get("maintenance_id")
    prior=await runtime.run(lambda a:_receipt(a,host,_operation_id(op))) if op is not None else None
    if prior and prior["state"]=="cleared":
        if prior["clear_arguments"]!={k:message.get(k) for k in prior["clear_arguments"]}:
            raise AccountError("provider_cli_maintenance_id_conflict")
        return {"type":"spawn_freeze.ok","ok":True,"host":host,"receipt":prior,**await runtime.run(lambda a:status(a,host))}
    if not value:
        raise AccountError("provider_cli_maintenance_stale")
    clear={k:message.get(k) for k in ("maintenance_id","expected_maintenance_digest","expected_hold_digest")}
    if op!=value["arguments"]["maintenance_id"] or clear["expected_maintenance_digest"]!=digest(value):
        raise AccountError("provider_cli_maintenance_compare_required")
    held=await runtime.run(lambda a:hold(a,host))
    if clear["expected_hold_digest"] is not None and clear["expected_hold_digest"]!=hold_digest(held):
        raise AccountError("provider_cli_maintenance_hold_stale")
    expired=value["hold"]["held_until"]<=runtime.clock()
    before=await runtime.run(lambda a:_clear_snapshot(a,value))
    binding=Binding(**before["binding"])
    if runtime.executor is None:raise AccountError("provider_cli_verification_unavailable")
    proved_at=runtime.clock()
    remote=await host_status(runtime.executor,binding)
    if remote.get("binding")!=binding.public():raise AccountError("account_repin_state_changed")
    # Fixed host read-only observation validates the selected private home and
    # native identity without any login, mutation, or credential transfer.
    observed=await runtime.executor.execute(binding,"observe-bound","maintenance-clear:"+op)
    if observed.get("outcome")!="ok" or observed.get("binding_key")!=binding.key or not _valid_observation(binding,observed.get("observation")):
        raise AccountError("provider_cli_verification_unavailable")
    await runtime.executor.verify_launchers(binding)  # Includes pending host permits.
    async with runtime.home_inputs(binding).write():
        def clear_commit(a):
            with transaction(a.conn):
                if slot(a,host)!=value or hold(a,host)!=held or _clear_snapshot(a,value)!=before or runtime.clock()-proved_at>120:
                    raise AccountError("provider_cli_maintenance_stale")
                if held==value["hold"]:a.conn.execute("DELETE FROM v2_spawn_admission_hold WHERE host=?",(host,))
                a.conn.execute("DELETE FROM kv WHERE k=?",(PREFIX+host,))
                result={**value,"state":"cleared","clear_arguments":clear,"cleared_at":runtime.clock(),
                    "clear_proof":{"binding_key":binding.key,"origin_digest":digest(before),"host_digest":digest(remote),
                                   "verified_at":proved_at,"expired_recovery":expired,"hold":held,
                                   "foreign_hold_preserved":held is not None and held!=value["hold"]}}
                _write_receipt(a,host,op,result)
                return result
        receipt=await runtime.run(clear_commit,settle_on_cancel=True)
    return {"type":"spawn_freeze.ok","ok":True,"host":host,"receipt":receipt,**await runtime.run(lambda a:status(a,host)),
            "held_until":held["held_until"] if held and held!=value["hold"] else None,
            "reason":held["reason"] if held and held!=value["hold"] else None}


async def legacy(runtime,host,*,clear=False,reason="",ttl=900):
    def commit(a):
        with transaction(a.conn):
            current=slot(a,host)
            if current:
                if clear or current["hold"]["held_until"]>runtime.clock():
                    raise AccountError("provider_cli_maintenance_compare_required")
                held=hold(a,host)
                if held and held!=current["hold"]:
                    if not owned_hold(a,current,held):raise AccountError("provider_cli_maintenance_foreign_hold")
                    if held["held_until"]>runtime.clock():
                        if held["reason"]==(reason or None):return held  # Same transport retry.
                        raise AccountError("provider_cli_maintenance_foreign_hold")
            if clear:a.conn.execute("DELETE FROM v2_spawn_admission_hold WHERE host=?",(host,))
            else:
                if not math.isfinite(ttl) or ttl<=0:raise AccountError("provider_cli_maintenance_request_invalid")
                a.conn.execute("INSERT OR REPLACE INTO v2_spawn_admission_hold(host,held_until,reason,set_at) VALUES (?,?,?,?)",
                    (host,runtime.clock()+ttl,reason or None,runtime.clock()))
                if current:
                    # The existing authenticated freeze route creates a real
                    # fresh hold for B reconciliation after expiry. The old
                    # slot/arguments stay immutable; this is only its hold's
                    # creation receipt, never release/preparation authority.
                    from account_repin_state import put
                    held=hold(a,host)
                    put(a,HOLD_RECEIPT+digest(current)+":"+hold_digest(held),"done",
                        {"state":"done","slot_digest":digest(current),"hold":held},immutable=True)
            return hold(a,host)
    held=await runtime.run(commit,settle_on_cancel=True)
    return {"type":"spawn_freeze.ok","ok":True,"host":host,"held_until":held["held_until"] if held else None,
            "reason":held["reason"] if held else None,"hold":held,"hold_digest":hold_digest(held)}
