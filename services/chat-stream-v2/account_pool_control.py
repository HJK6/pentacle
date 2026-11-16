"""Revisioned pool ceilings and exact-generation exceptions in the existing kv."""
from __future__ import annotations

import json

from account_store import number, transaction
from provider_accounts import AccountError

POOL_ERRORS = frozenset({"pool_at_cap", "pool_blocked", "pool_usage_unknown"})
NOTICE_TOKEN = object()  # Private object identity, never accepted from the wire.


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read(accounts, key):
    row=accounts.conn.execute("SELECT v FROM kv WHERE k=?",(key,)).fetchone()
    return json.loads(row[0]) if row else None


def write(accounts, key, value):
    accounts.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",(key,canonical(value)))


def pool_key(provider, pool):
    return f"account.pool-control:{provider}:{pool}"


def seat_key(stream, generation):
    return f"account.seat-override:{stream}:{generation}"


def operator(auth):
    credential=auth.get("credential_id")
    return bool(auth.get("operator_authenticated") is True and isinstance(credential,str) and credential
                and auth.get("operator_principal")=="operator:"+credential
                and auth.get("operator_authority_source")!="stream_token")


def eligibility(accounts, provider, pool, *, now, measured=None):
    control=read(accounts,pool_key(provider,pool))
    result={"eligible":True,"code":None,"control_revision":(control or {}).get("revision",0),
            "provider":provider,"pool_id":pool,"reason":(control or {}).get("reason"),
            "authority_ref":(control or {}).get("authority_ref")}
    if not control: return result
    code=None
    if control["blocked"]: code="account_pool_blocked"
    elif control["stop_pct"] is not None:
        if measured is None:
            measured=next((p for p in accounts.readback(now=now)["quota_pools"]
                           if (p["provider"],p["pool_id"])==(provider,pool)),{})
        obs=measured.get("observation") or {}
        windows=obs.get('windows')
        window=windows.get(control["window"]) if isinstance(windows,dict) else None
        details=window if isinstance(window,dict) else {}
        max_age=accounts.policy.get("observation_max_age",600)
        result.update(observed_at=obs.get("observed_at") if number(obs.get('observed_at')) else None,window=control["window"],
                      reset_at=details.get("reset_at") if number(details.get('reset_at')) else None,
                      reset_id=details.get("reset_id") if isinstance(details.get('reset_id'),str) else None)
        if (not measured.get("fresh") or not number(max_age) or max_age<=0
                or not isinstance(window,dict) or not number(window.get("pct"))
                or not 0<=window["pct"]<=100 or not number(window.get("reset_at"))
                or window["reset_at"]<=now or not isinstance(window.get("reset_id"),str)
                or not window["reset_id"]): code="account_pool_meter_unavailable"
        elif window["pct"]>=control["stop_pct"]: code="account_pool_cap_reached"
    return dict(result,eligible=code is None,code=code)


def assert_eligible(accounts, binding, *, now, override=None):
    result=eligibility(accounts,binding.provider,binding.pool_id,now=now)
    if override:
        if (override.get("provider"),override.get("pool_id"),override.get("control_revision")) != (
                binding.provider,binding.pool_id,result["control_revision"]):
            raise AccountError("account_pool_override_stale")
        return result
    if not result["eligible"]:
        raise AccountError(result["code"],**{k:v for k,v in result.items() if k not in {"code","eligible"}})
    return result


def validate_common(message):
    if (type(message.get("expected_revision")) is not int or message["expected_revision"]<0
            or any(not isinstance(message.get(k),str) or not message[k].strip() or len(message[k])>1024
                   for k in ("operation_id","reason","authority_ref"))):
        raise AccountError("account_request_invalid")


def mutate(accounts, message, actor, *, now, authorize):
    """Caller supplies a current-authority recheck performed inside the transaction."""
    validate_common(message)
    action=message["action"]
    fields=("provider","pool_id","expected_revision","operation_id","blocked","stop_pct","window",
            "reason","authority_ref","supersedes_authority_ref") if action=="pool-control-set" else (
            "stream","expected_generation","expected_revision","operation_id","enabled","reason","authority_ref")
    payload={k:message.get(k) for k in fields}
    payload.update(action=action,actor=actor)
    if action=="pool-control-set":
        provider,pool=payload["provider"],payload["pool_id"]
        if (provider not in {"codex","claude"} or not isinstance(pool,str) or not pool or len(pool)>256
                or type(payload["blocked"]) is not bool or "stop_pct" not in message or "window" not in message):
            raise AccountError("account_request_invalid")
        pct,window=payload["stop_pct"],payload["window"]
        if pct is None:
            if window is not None: raise AccountError("account_request_invalid")
        elif not number(pct) or not 0<=pct<=100 or not isinstance(window,str) or not window:
            raise AccountError("account_request_invalid")
        key=pool_key(provider,pool)
    else:
        if (type(payload["enabled"]) is not bool or not isinstance(payload["stream"],str)
                or ":" not in payload["stream"] or not isinstance(payload["expected_generation"],str)
                or not payload["expected_generation"]): raise AccountError("account_request_invalid")
        key=seat_key(payload["stream"],payload["expected_generation"])
    receipt_key="account.control-operation:"+message["operation_id"]
    with transaction(accounts.conn):
        authorize(accounts.conn)
        old=read(accounts,key)
        prior=read(accounts,receipt_key)
        if prior:
            if prior["payload"]!=payload: raise AccountError("account_operation_id_conflict")
            return {"receipt":prior["receipt"],"current":old,"replayed":True}
        if action=="pool-control-set":
            bindings=accounts.conn.execute("SELECT data FROM v2_account_bindings WHERE provider=? AND pool_id=?",(provider,pool)).fetchall()
            if not bindings: raise AccountError("account_pool_unknown")
            if pct is not None:
                keys=set()
                for row in accounts.conn.execute("SELECT o.data FROM v2_account_observations o JOIN v2_account_bindings b "
                        "USING(binding_key) WHERE b.provider=? AND b.pool_id=?",(provider,pool)):
                    obs=json.loads(row[0])
                    keys.update(obs.get('observed_window_keys') or [])
                    if obs.get("collection")=="ok" and isinstance(obs.get("windows"),dict) and number(obs.get('observed_at')):
                        for name,item in obs['windows'].items():
                            if (isinstance(item,dict) and number(item.get('pct')) and 0<=item['pct']<=100
                                    and number(item.get('reset_at')) and item['reset_at']>obs['observed_at']
                                    and isinstance(item.get('reset_id'),str) and item['reset_id']): keys.add(name)
                if window not in keys: raise AccountError("account_pool_window_unknown")
        else:
            from store_assistant_binding import _seat_conn
            target=_seat_conn(accounts.conn,payload["stream"])
            if not target or target["status"]!="open" or target["session_generation"]!=payload["expected_generation"]:
                raise AccountError("account_seat_generation_stale")
        if (old or {}).get("revision",0)!=payload["expected_revision"]:
            raise AccountError("account_control_revision_conflict",current_revision=(old or {}).get("revision",0))
        if action=="pool-control-set":
            relaxed=old and ((old["blocked"] and not payload["blocked"]) or
                (old["stop_pct"] is not None and (pct is None or pct>old["stop_pct"] or window!=old["window"])))
            if relaxed and (payload["supersedes_authority_ref"]!=old["authority_ref"]
                            or payload["authority_ref"]==old["authority_ref"]):
                raise AccountError("account_control_supersession_required")
            control={k:payload[k] for k in ("blocked","stop_pct","window","reason","authority_ref")}
        else:
            control={k:payload[k] for k in ("enabled","reason","authority_ref")}
            control.update(stream=payload["stream"],generation=payload["expected_generation"])
        control.update(version=1,revision=payload["expected_revision"]+1,set_by=actor,set_at=now)
        receipt={"operation_id":message["operation_id"],"control":control}
        write(accounts,key,control)
        write(accounts,receipt_key,{"payload":payload,"receipt":receipt})
        return {"receipt":receipt,"current":control,"replayed":False}
