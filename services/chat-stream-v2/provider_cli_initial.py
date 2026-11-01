"""Server-derived readiness for the one transition out of pristine operation."""
from __future__ import annotations

import asyncio
import json

from account_store import transaction
from provider_accounts import AccountError, Binding
from provider_cli_pin import canonical, digest, pristine, validate, version

PAUSE = "provider_cli.initial_admission_pause"


def _rows(conn, query, arguments=()):
    return [dict(row) for row in conn.execute(query, arguments)]


def snapshot(a, now, *, composite_for=None, counts=None):
    """Body-free census and input custody; readiness uses one comparison projection."""
    if not pristine(a):
        raise AccountError("provider_cli_pin_stale")
    accounts = a.readback(now=now)
    before = {
        "accounts": accounts["accounts"], "defaults": accounts["defaults"],
        "policy": json.loads(canonical(a.policy)),
        "sessions": _rows(a.conn, "SELECT s.host,s.session_name,g.generation AS session_generation,s.provider,"
            "s.account_binding,s.observer_binding,s.pane_pid,s.pane_status,s.bootstrap_state,s.routing_integrity,s.parent_stream_id "
            "FROM sessions s LEFT JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name "
            "WHERE s.status='open' ORDER BY s.host,s.session_name"),
        "reservations": _rows(a.conn, "SELECT * FROM v2_stream_reservations ORDER BY host,session_name"),
        "holds": _rows(a.conn, "SELECT * FROM v2_spawn_admission_hold ORDER BY host"),
        "account_operations": _rows(a.conn, "SELECT binding_key,state,data FROM v2_account_recovery "
            "WHERE substr(binding_key,1,6)='repin:' OR substr(binding_key,1,12)='repin-hatch:' "
            "OR substr(binding_key,1,17)='account-mutation:' ORDER BY binding_key"),
        "maintenance": _rows(a.conn, "SELECT k,v FROM kv WHERE substr(k,1,?)=? ORDER BY k",
            (len("provider_cli.maintenance_input:"), "provider_cli.maintenance_input:")),
        "input_obligations": input_obligations(a),
        "closed_parent_obligations": _rows(a.conn,"SELECT child.host,child.session_name,child.parent_stream_id "
            "FROM sessions child JOIN sessions parent ON child.parent_stream_id=parent.host||':'||parent.session_name "
            "WHERE child.status='open' AND parent.status='closed' AND parent.account_binding IS NOT NULL "
            "ORDER BY child.host,child.session_name"),
    }
    from sessions import Sessions
    open_ids = {row["host"] + ":" + row["session_name"] for row in before["sessions"]}
    blocking = []
    not_open = 0
    for obligation in before["input_obligations"]:
        target = obligation.get("to_stream_id")
        concrete = False
        if isinstance(target, str):
            host, name = Sessions.split(target)
            concrete = bool(host and name and ":" not in name
                and not any(c.isspace() or ord(c) < 32 for c in target)
                and not (composite_for and composite_for(target) is not None))
        if (not concrete or obligation.get("delivery") == "unverifiable" or target in open_ids):
            blocking.append(obligation)
        else:
            not_open += 1
    before["input_obligations"] = blocking
    if counts is not None:
        counts.update(blocking=len(blocking), not_open=not_open)
    return before


def _readiness_projection(value):
    projected={k:v for k,v in value.items() if k!="input_obligations"}
    projected["accounts"]=[]
    for account in value["accounts"]:
        row=dict(account)
        if isinstance(row.get("observation"),dict):
            row["observation"]={k:v for k,v in row["observation"].items()
                                if k not in {"observed_at","windows","collection"}}
        projected["accounts"].append(row)
    return projected


def input_obligations(a):
    """Project only unresolved identities/states; never retain message bodies."""
    result=[]
    for row in a.conn.execute("SELECT r.to_stream_id,r.request_id,r.state,r.delivery FROM v2_send_receipts r "
            "WHERE r.rowid IN (SELECT MAX(rowid) FROM v2_send_receipts GROUP BY to_stream_id,request_id) "
            "AND (r.state='accepted' OR r.delivery IN ('pasted_unsubmitted','proof_unavailable','committed_pending_proof')) "
            "ORDER BY r.to_stream_id,r.request_id"):
        result.append(dict(row))
    for row in a.conn.execute("SELECT tell_id,reply FROM v2_tell_deliveries ORDER BY tell_id"):
        try:
            value=json.loads(row["reply"]);reply=value.get("reply",{});delivery=value.get("delivery",{})
            state=delivery.get("delivery_status") or reply.get("delivery_status")
            if not isinstance(state, str) or not state:
                raise ValueError("tell_state_unverifiable")
            if state in {"pasted_unsubmitted","proof_unavailable","committed_pending_proof","pending_input","persisted_suppressed"}:
                result.append({"to_stream_id":delivery.get("to_stream_id") or reply.get("to_stream_id"),
                    "tell_id":row["tell_id"],"delivery":state})
        except (TypeError,ValueError,AttributeError):
            result.append({"tell_id":row["tell_id"],"delivery":"unverifiable"})
    return result


def population(before):
    cells = {}
    for row in before["accounts"]:
        key = (row["host"], row["provider"], row["alias"])
        if key not in cells or row.get("active"):
            cells[key] = row
    return cells


def parking(value, cells):
    """The authorized decision is recorded in the eventual pin receipt."""
    if value is None:
        return {}
    if not isinstance(value, list):
        raise AccountError("provider_cli_pin_request_invalid")
    result = {}
    required = {"host", "provider", "account", "generation", "reason", "decision_ref"}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != required:
            raise AccountError("provider_cli_pin_request_invalid")
        if any(not isinstance(entry[k],str) for k in ("host","provider","account")):
            raise AccountError("provider_cli_pin_request_invalid")
        key = (entry["host"], entry["provider"], entry["account"])
        if (key in result or key not in cells or entry["generation"] != cells[key].get("generation")
                or any(not isinstance(entry[k], str) or not entry[k].strip() or len(entry[k]) > 512
                       or any(ord(c) < 32 or ord(c) > 126 for c in entry[k])
                       for k in ("reason", "decision_ref"))):
            raise AccountError("provider_cli_pin_request_invalid")
        result[key] = entry
    return result


def structural(a, before, pin, parked, now):
    from account_repin_state import causes, generation_intents
    missing = []
    cells = population(before)
    enrolled = {(r["host"], r["provider"]) for r in before["accounts"]}
    if before["reservations"]:
        missing.append({"reason": "launch_reservation_in_flight"})
    if any(row["held_until"] > now for row in before["holds"]):
        missing.append({"reason": "admission_hold"})
    if before["maintenance"]:
        missing.append({"reason": "input_maintenance_pending"})
    if before["input_obligations"]:
        missing.append({"reason":"input_submission_unresolved","obligations":before["input_obligations"]})
    if before["closed_parent_obligations"]:
        missing.append({"reason":"custody_transfer_unresolved","children":before["closed_parent_obligations"]})
    for key, row in cells.items():
        cell = {"host":key[0], "provider":key[1], "account":key[2]}
        if causes(a, cell) or generation_intents(a, cell):
            missing.append({**cell, "reason":"account_mutation_unresolved"})
        if key in parked:
            continue
        expected = pin["providers"][row["provider"]]
        try:
            matches = (version(row["provider"], row.get("version")) == expected["version"]
                and any(v["native_sha256"] == row.get("executable_sha256")
                        for v in expected["artifacts"].values()))
        except (TypeError, ValueError):
            matches = False
        if not row.get("active") or not row.get("enabled") or row.get("health") != "healthy":
            missing.append({**cell, "reason":"cell_unavailable"})
        elif not a.policy.get("enabled") or not __import__('provider_accounts').policy_manages(a.policy,key[0],key[1]):
            missing.append({**cell, "reason":"account_policy_off"})
        elif not matches:
            missing.append({**cell, "reason":"pin_mismatch"})
    for seat in before["sessions"]:
        if (seat["host"], seat["provider"]) not in enrolled:
            continue
        sid = seat["host"] + ":" + seat["session_name"]
        if seat["bootstrap_state"]!="ready" or seat["routing_integrity"]=="mismatch":
            missing.append({"stream_id":sid,"reason":"seat_not_ready_or_routing_mismatch"})
        try:
            raw = seat["account_binding"]
            binding = Binding.from_public(json.loads(raw) if isinstance(raw,str) else raw)
            key = (binding.host,binding.provider,binding.alias)
            selected = cells[key]
            if key in parked:
                raise AccountError("parked_cell_has_seat")
            if (binding.host != seat["host"] or binding.provider != seat["provider"]
                    or binding.generation != selected.get("generation")
                    or dict(binding.public(),default_revision=0) != dict(a._available(a._cell(*key)).public(),default_revision=0)):
                raise AccountError("seat_binding_stale")
        except (AccountError,KeyError,TypeError,ValueError):
            missing.append({"stream_id":sid,"reason":"seat_binding_unverified"})
    return missing


async def prepare(runtime, message):
    from _shared.provider_cli_release import SHA256
    expected=message.get("expected_readiness_digest")
    if expected is not None and (not isinstance(expected,str) or not SHA256.fullmatch(expected)):
        raise AccountError("provider_cli_pin_request_invalid")
    try:
        pin = validate(message["pin"])
    except (KeyError,TypeError,ValueError):
        raise AccountError("provider_cli_pin_request_invalid") from None
    if pin["revision"] != 1:
        raise AccountError("provider_cli_pin_stale")
    counts = {}
    before = await runtime.run(lambda a:snapshot(a,runtime.clock(),
        composite_for=getattr(runtime.server,"_composite_for",None), counts=counts))
    cells = population(before)
    parked = parking(message.get("initial_parked_cells"), cells)
    missing = await runtime.run(lambda a:structural(a,before,pin,parked,runtime.clock()))
    proofs = {}
    if not missing:
        from account_repin_state import host_status
        from provider_cli_pin import consumer_current
        for key,row in cells.items():
            cell = {"host":key[0],"provider":key[1],"account":key[2]}
            try:
                if runtime.executor is None or not row.get("active"):
                    raise AccountError("provider_cli_verification_unavailable")
                async with runtime.executor._host_locks.setdefault(key[0],asyncio.Lock()):
                    verified_at=runtime.clock()
                    bound = await runtime.run(lambda a,key=key:(
                        Binding(**json.loads(a._cell(*key)["data"])) if key in parked
                        else a._available(a._cell(*key))))
                    remote = await host_status(runtime.executor,bound)
                    if (remote.get("mutation_clearance_required") is not True
                            or remote.get("binding") != bound.public()):
                        raise AccountError("account_mutation_clearance_required")
                    await runtime.run(lambda a,key=key:consumer_current(a,key[0],
                        getattr(runtime.sessions,"local_host",None),runtime.clock()))
                    launchers = await runtime.executor.verify_launchers(bound)
                    proofs[bound.key]={"launchers":launchers,"verified_at":verified_at}
                    if key not in parked:
                        proof = await runtime.executor.verify_artifact(bound,pin)
                        if launchers.get("unmanaged"):
                            raise AccountError("account_repin_home_busy")
                        proofs[bound.key]["artifact"]=proof
            except (AccountError,AttributeError) as exc:
                missing.append({**cell,"reason":str(exc) if isinstance(exc,AccountError)
                                else "launcher_verification_unavailable"})
    ready = {"target_digest":digest(pin),"snapshot_digest":digest(_readiness_projection(before)),
             "proofs":proofs,"parked_cells":list(parked.values()),"missing":missing,
             "input_obligation_counts":counts,
             "input_obligations":{"prepare":before["input_obligations"]}}
    ready["digest"] = digest({k:ready[k] for k in ("target_digest","snapshot_digest","parked_cells")})
    if expected is not None and expected != ready["digest"]:
        ready["missing"].append({"reason":"readiness_digest_changed"})
    return before,ready


def _readiness_refusal(missing):
    first=missing[0]
    return AccountError("provider_cli_initial_readiness_required",**{
        k:first[k] for k in ("reason","host","provider","account","next_action") if k in first})


def _snapshot_change(before,after):
    identity=lambda row:tuple(row.get(k) for k in ("host","provider","alias","generation"))
    left={identity(row):row for row in before["accounts"]}
    right={identity(row):row for row in after["accounts"]}
    for key in dict.fromkeys([*left,*right]):
        old,new=left.get(key),right.get(key)
        if old==new:continue
        row=old or new
        fields=["population"] if old is None or new is None else [
            k for k in old.keys()|new.keys() if k not in old or k not in new or old[k]!=new[k]]
        if ("observation" in fields and isinstance(old.get("observation"),dict)
                and isinstance(new.get("observation"),dict)):
            fields.remove("observation")
            a,b=old.get("observation") or {},new.get("observation") or {}
            fields.extend("observation."+k for k in a.keys()|b.keys() if k not in a or k not in b or a[k]!=b[k])
        return {"reason":"readiness_state_changed","host":row["host"],"provider":row["provider"],
                "account":row["alias"],"next_action":"Inspect changed fields: "+",".join(sorted(fields))}
    sections=sorted(k for k in before.keys()|after.keys() if before.get(k)!=after.get(k))
    return {"reason":"readiness_state_changed","next_action":"Inspect changed sections: "+",".join(sections)}


def commit(a, message, before, ready, now, *, local_host,owner_epoch,composite_for=None):
    """Called inside the same transaction as the initial pin write."""
    from provider_cli_artifact import validate_proof
    from provider_cli_pin import consumer_current
    owner=a.conn.execute("SELECT v FROM kv WHERE k=?",(PAUSE,)).fetchone()
    try: owner_matches=bool(owner and json.loads(owner[0])=={"operation_id":message["operation_id"],"owner_epoch":owner_epoch})
    except (ValueError,TypeError):owner_matches=False
    if not owner_matches:
        raise AccountError("provider_cli_initial_readiness_required",reason="promotion_pause_changed")
    if ready["missing"]:
        raise _readiness_refusal(ready["missing"])
    after=snapshot(a,now,composite_for=composite_for)
    projected_before,projected_after=_readiness_projection(before),_readiness_projection(after)
    if projected_after != projected_before:
        raise _readiness_refusal([_snapshot_change(projected_before,projected_after)])
    ready["input_obligations"].update(commit=after["input_obligations"],
        added=[row for row in after["input_obligations"] if row not in before["input_obligations"]],
        removed=[row for row in before["input_obligations"] if row not in after["input_obligations"]])
    pin = validate(message["pin"])
    if ready["target_digest"] != digest(pin):
        raise AccountError("provider_cli_initial_readiness_required",reason="target_changed")
    cells = population(before)
    parked = parking(message.get("initial_parked_cells"),cells)
    missing = structural(a,before,pin,parked,now)
    if missing:
        raise _readiness_refusal(missing)
    for key,row in cells.items():
        consumer_current(a,key[0],local_host,now)
        bound=Binding(**json.loads(a._cell(*key)["data"]))
        item=ready["proofs"].get(bound.key)
        if not item or not 0<=now-item["verified_at"]<=120:
            raise AccountError("provider_cli_initial_readiness_required",reason="proof_expired")
        if key in parked:
            continue
        bound = a._available(a._cell(*key))
        validate_proof(bound,pin,item["artifact"])
        a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",("provider_cli.verified:"+bound.key,
            canonical({"proof":item["artifact"],"verified_at":item["verified_at"]})))
    # These are observations of actual current launchers/artifacts, never new
    # historical admissions for a process that was created before enforcement.
    for seat in before["sessions"]:
        raw=seat.get("account_binding")
        if not raw:continue
        binding=Binding.from_public(json.loads(raw) if isinstance(raw,str) else raw)
        item=ready["proofs"].get(binding.key)
        if item:
            key="provider_cli.observed:"+seat["host"]+":"+seat["session_name"]+":"+str(seat["session_generation"])
            a.conn.execute("INSERT OR IGNORE INTO kv VALUES (?,?)",(key,
                canonical({"artifact":item["artifact"],"binding_key":binding.key,
                           "initial_readiness_digest":ready["digest"],"verified_at":item["verified_at"]})))


def pause(a, operation_id, owner_epoch, *, release=False):
    """A pause is creation coordination, never a second release authority."""
    with transaction(a.conn):
        row = a.conn.execute("SELECT v FROM kv WHERE k=?",(PAUSE,)).fetchone()
        value=canonical({"operation_id":operation_id,"owner_epoch":owner_epoch})
        if release and row and row[0]!=value:
            return False  # A stale actor cannot clear its successor's pause.
        if row and row[0] != value:
            raise AccountError("provider_cli_initial_readiness_required",reason="promotion_in_progress")
        if release:
            if row:
                a.conn.execute("DELETE FROM kv WHERE k=?",(PAUSE,))
        else:
            a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",(PAUSE,value))


def startup(a,owner_epoch):
    """An owned daemon restart aborts old promotion, never assumes it completed.

    Reservations remain durable and still block any later first CAS. Removing
    the old actor's coordination pause merely restores pristine availability.
    Its old commit is fenced by the owner epoch above.
    """
    with transaction(a.conn):
        row=a.conn.execute("SELECT v FROM kv WHERE k=?",(PAUSE,)).fetchone()
        if not row:return
        try:
            value=json.loads(row[0])
            if (not isinstance(value,dict) or set(value)!={"operation_id","owner_epoch"}
                    or any(not isinstance(v,str) or not v for v in value.values())):
                raise ValueError()
        except (TypeError,ValueError):
            raise AccountError("provider_cli_initial_readiness_required",reason="promotion_pause_corrupt") from None
        if value["owner_epoch"]!=owner_epoch:
            a.conn.execute("DELETE FROM kv WHERE k=?",(PAUSE,))


async def set_pair(runtime,message,*,recovery_principal):
    from provider_cli_pin import history,operation_identity,set_pin
    operation_id,_=operation_identity(message)
    initial=(type(message.get("expected_revision")) is int and message["expected_revision"]==0
             and "recover_receipt" not in message)
    prior=await runtime.run(lambda a:history(a).get(operation_id))
    initial=initial and prior is None and await runtime.run(pristine)
    before=ready=None
    if initial:
        await runtime.run(lambda a:pause(a,operation_id,runtime._health_epoch),settle_on_cancel=True)
    try:
        if initial:
            # Already-dispatched work keeps its owner. Never cancel a remote
            # creation to manufacture quiescence, nor wait under the pin lock.
            tasks={task for task in getattr(runtime.spawnctl,"_background_spawns",set())
                   if task is not asyncio.current_task() and not task.done()}
            if tasks:
                await asyncio.wait(tasks,timeout=30)
            before,ready=await prepare(runtime,message)
            if ready["missing"]:
                raise _readiness_refusal(ready["missing"])
        async with runtime.pin_inputs.write():
            async with runtime.pin_lock:
                return await runtime.run(lambda a:set_pin(a,message,recovery_principal=recovery_principal,
                    initial_check=(lambda ledger:commit(ledger,message,before,ready,runtime.clock(),
                        local_host=getattr(runtime.sessions,"local_host",None),owner_epoch=runtime._health_epoch,
                        composite_for=getattr(runtime.server,"_composite_for",None))) if initial else None,
                    initial_readiness=ready),settle_on_cancel=True)
    finally:
        if initial:
            await runtime.run(lambda a:pause(a,operation_id,runtime._health_epoch,release=True),settle_on_cancel=True)
