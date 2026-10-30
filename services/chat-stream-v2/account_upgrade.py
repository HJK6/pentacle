"""Explicit FD import of a completed healthy Codex enrollment. Never logs in."""
from __future__ import annotations
import asyncio
from dataclasses import asdict
import json
from pathlib import PurePosixPath
import re

from account_store import transaction
from account_revalidation import _valid_observation
from provider_accounts import AccountError, Binding, _component


def _payload(message):
    value = {k: message.get(k) for k in ("host", "provider", "account", "login_generation",
        "new_generation", "operation_id", "expected_version", "expected_executable_sha256", "reason")}
    for key in ("host", "account", "login_generation", "new_generation"):
        _component(value[key])
    if (value["provider"] != "codex"
            or value["login_generation"] == "active" or value["new_generation"] in {"active", value["login_generation"]}):
        raise AccountError("account_upgrade_request_invalid")
    for key in ("operation_id", "expected_version", "reason"):
        text = value[key]
        if not isinstance(text, str) or not 1 <= len(text) <= 160 or not text.strip() or any(ord(c) < 32 or ord(c) > 126 for c in text):
            raise AccountError("account_upgrade_request_invalid")
    if not isinstance(value["expected_executable_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["expected_executable_sha256"]):
        raise AccountError("account_upgrade_request_invalid")
    return value


def _key(payload):
    return "upgrade:" + ":".join(payload[k] for k in ("host", "provider", "account", "operation_id"))


def _read(ledger, payload):
    row = ledger.conn.execute("SELECT data FROM v2_account_recovery WHERE binding_key=?", (_key(payload),)).fetchone()
    state = json.loads(row[0]) if row else None
    if state and state["arguments"] != payload:
        raise AccountError("account_upgrade_id_conflict")
    return state


def _write(ledger, payload, state):
    ledger.conn.execute("INSERT OR REPLACE INTO v2_account_recovery VALUES (?,?,?)",
        (_key(payload), state["status"], json.dumps(state, allow_nan=False)))


def _snapshot(ledger, payload, now):
    if not ledger.policy.get("enabled") or not ledger.policy:
        raise AccountError("account_feature_off")
    from provider_accounts import policy_manages
    if not policy_manages(ledger.policy, payload["host"], payload["provider"]):
        raise AccountError("account_feature_off")
    row = ledger._cell(payload["host"], payload["provider"], payload["account"])
    old = ledger._available(row)
    if old.enabled is not True or old.healthy is not True:
        raise AccountError("account_unavailable")
    if old.generation != payload["login_generation"]:
        raise AccountError("account_login_generation_stale")
    default = ledger.conn.execute("SELECT * FROM v2_account_defaults WHERE host=? AND provider=?",
        (old.host, old.provider)).fetchone()
    hold = ledger.conn.execute("SELECT * FROM v2_spawn_admission_hold WHERE host=?", (old.host,)).fetchone()
    if not hold or hold["held_until"] <= now:
        raise AccountError("account_upgrade_hold_required")
    from provider_cli_maintenance import require_mutation
    require_mutation(ledger,old,now)
    return dict(binding=asdict(old), health_revision=row["health_revision"],
        default=dict(default) if default else None, hold=dict(hold))


def _begin(ledger, payload, now):
    with transaction(ledger.conn):
        state = _read(ledger, payload)
        if state and state["status"] == "done": return state
        from account_repin_state import require_generation_clear
        require_generation_clear(ledger,payload)
        snapshot = _snapshot(ledger, payload, now)
        if state:
            if state["snapshot"] != snapshot: raise AccountError("account_upgrade_state_changed")
            return state
        old = Binding(**snapshot["binding"])
        state = dict(arguments=payload, status="pending", snapshot=snapshot, old_key=old.key, reserved_at_ns=__import__("time").time_ns(),
            new_key=":".join((old.host, old.provider, old.alias, payload["new_generation"])))
        _write(ledger, payload, state)
        return state


def _binding(old, payload, receipt):
    try:
        return _checked_binding(old, payload, receipt)
    except (TypeError, AttributeError):
        raise AccountError("account_host_binding_invalid") from None


def _checked_binding(old, payload, receipt):
    if (not isinstance(receipt, dict) or receipt.get("state") != "done" or receipt.get("outcome") != "ok"
            or any(receipt.get(k) != v for k, v in dict(provider=old.provider, alias=old.alias,
                expected_generation=old.generation, generation=payload["new_generation"]).items())):
        raise AccountError("account_upgrade_receipt_invalid")
    new = Binding.from_public(receipt.get("binding"))
    # The host owns the root; a new login must stay in the same cell subtree.
    old_home, new_home = PurePosixPath(old.home), PurePosixPath(new.home)
    if (any(getattr(new, k) != getattr(old, k) for k in ("host", "provider", "alias", "provider_id", "pool_id", "platform"))
            or new.generation != payload["new_generation"] or new.enabled is not True or new.healthy is not True
            or new.default_revision != 0 or not new_home.is_absolute() or ".." in new_home.parts
            or new_home == old_home or new_home.parent != old_home.parent or new_home.name != new.generation
            or not PurePosixPath(new.executable).is_absolute() or ".." in PurePosixPath(new.executable).parts
            or new.version != payload["expected_version"] or new.executable_sha256 != payload["expected_executable_sha256"]
            or new.executable_sha256 == old.executable_sha256 or new.executable == old.executable
            or new.observer_executable or new.observer_executable_sha256):
        raise AccountError("account_host_binding_invalid")
    return new


def _commit(ledger, payload, state, new, observation, now):
    with transaction(ledger.conn):
        from account_repin_state import require_generation_clear
        require_generation_clear(ledger,payload)
        current = _read(ledger, payload)
        if current and current["status"] == "done": return current["result"]
        if current != state or _snapshot(ledger, payload, now) != state["snapshot"]:
            raise AccountError("account_upgrade_state_changed")
        # Never resurrect a preexisting historical generation.
        if ledger.bound(new.key) is not None: raise AccountError("account_binding_immutable")
        ledger._register(new)
        if not ledger._observe(new.key, observation, now=now):
            raise AccountError("account_upgrade_observation_invalid")
        result = dict(outcome="upgraded", operation_id=payload["operation_id"], binding=new.public())
        state = dict(state, status="done", result=result)
        _write(ledger, payload, state)
        from account_repin_state import complete_generation
        complete_generation(ledger,Binding(**state["snapshot"]["binding"]),payload["operation_id"])
        return result


async def upgrade(executor, message, auth):
    runtime = executor.runtime
    await runtime._front_desk(auth)
    payload = _payload(message)
    if not runtime.manages(payload["host"], payload["provider"]): raise AccountError("account_feature_off")
    async with executor._host_locks.setdefault(payload["host"], asyncio.Lock()):
        await runtime._front_desk(auth)
        state = await runtime.run(lambda a: _begin(a, payload, runtime.clock()))
        if state["status"] == "done": return state["result"]
        old = Binding(**state["snapshot"]["binding"])
        if message.get("prepare_enrollment") is True:
            from account_repin_state import host_status,permit
            from provider_cli_pin import current
            pin=await runtime.run(current)
            artifact=pin["providers"][old.provider]["artifacts"].get("darwin-arm64" if old.platform=="macos" else "linux-x64")
            # The helper still stages/validates its exact native platform before login.
            if (pin["providers"][old.provider]["version"]!=payload["expected_version"]
                    or not any(v["native_sha256"]==payload["expected_executable_sha256"] for v in pin["providers"][old.provider]["artifacts"].values())
                    or payload["expected_executable_sha256"]==old.executable_sha256):
                raise AccountError("account_upgrade_receipt_invalid")
            remote=await host_status(executor,old)
            if remote["binding"]!=old.public():raise AccountError("account_login_generation_stale")
            clearance=await permit(executor,old,"enroll",payload["operation_id"],payload["new_generation"],remote,state)
            await runtime._front_desk(auth)
            await runtime.run(lambda a:_snapshot(a,payload,runtime.clock())==state["snapshot"] or (_ for _ in ()).throw(AccountError("account_upgrade_state_changed")))
            return {"outcome":"enrollment_prepared","clearance":clearance,"pin":pin,"arguments":payload}
        try:
            receipt = await executor.execute(old, "status", payload["operation_id"], payload["new_generation"])
            new = _binding(old, payload, receipt)
            result = await executor.execute(new, "observe", payload["operation_id"])
        except AccountError:
            raise
        if (not isinstance(result, dict) or result.get("outcome") != "ok" or result.get("binding_key") != new.key
                or not _valid_observation(new, result.get("observation"))):
            raise AccountError("account_upgrade_observation_invalid")
        await runtime._front_desk(auth)
        result = await runtime.run(lambda a: _commit(a, payload, state, new, result["observation"], runtime.clock()))
        import logging
        logging.getLogger("chat_streamd_v2.provider_accounts").info(
            "subsystem=provider_accounts bug_ref=bound_executable_upgrade action=upgrade host=%s provider=%s account=%s",
            old.host, old.provider, old.alias)
        return result
