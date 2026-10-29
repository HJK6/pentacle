"""Daemon-owned release pair and immutable operation receipts in existing storage."""
from __future__ import annotations
import hashlib
import json
import sys
import logging
from pathlib import Path

SERVICES_ROOT=str(Path(__file__).resolve().parents[1])
if SERVICES_ROOT not in sys.path: sys.path.insert(0,SERVICES_ROOT)
from _shared.provider_cli_release import canonical, digest, loads, validate, version
from account_store import transaction
from provider_accounts import AccountError

KEY = "provider_cli.release_pin"
INITIALIZED = "provider_cli.pin_initialized"
PREFIX = "provider-cli-pin:"
ABSENT = "absent"
log=logging.getLogger("chat_streamd_v2.provider_cli_pin")


def privileged(auth):
    return (not auth.get("restricted") and not auth.get("dot_principal") and not auth.get("service_authenticated") and not auth.get("scoped_principal") and not auth.get("service_attempted")
            and (auth.get("operator_authenticated") is True or
                 (auth.get("peer_loopback") is True and auth.get("local_admin_verified") is True)))


def raw(ledger):
    row = ledger.conn.execute("SELECT v FROM kv WHERE k=?", (KEY,)).fetchone()
    return row[0] if row else None


def raw_digest(value):
    if value is None: return ABSENT
    data=value if isinstance(value,bytes) else value.encode() if isinstance(value,str) else canonical(value).encode()
    return hashlib.sha256(data).hexdigest()


def history(ledger):
    receipts = {}
    try:
        for row in ledger.conn.execute("SELECT binding_key,state,data FROM v2_account_recovery WHERE substr(binding_key,1,?)=?",
                                       (len(PREFIX), PREFIX)):
            receipt = json.loads(row["data"])
            if (row["state"] != "done" or receipt["operation_id"] != row["binding_key"][len(PREFIX):]
                    or receipt["digest"] != digest(validate(receipt["pin"]))): raise ValueError()
            receipts[receipt["operation_id"]] = receipt
    except (ValueError, KeyError, TypeError):
        raise AccountError("provider_cli_pin_unavailable", reason="receipt_history_corrupt") from None
    return receipts


def current(ledger):
    value = raw(ledger)
    if value is None: raise AccountError("provider_cli_pin_unavailable", reason="pin_absent")
    try:
        if not isinstance(value,str): raise ValueError()
        pin=loads(value)
    except (ValueError, TypeError):
        raise AccountError("provider_cli_pin_unavailable", reason="pin_corrupt") from None
    receipts=history(ledger)
    high_water=max((r["pin"]["revision"] for r in receipts.values()),default=0)
    if not any(r["pin"]==pin for r in receipts.values()) or pin["revision"]!=high_water:
        raise AccountError("provider_cli_pin_unavailable",reason="receipt_history_lost")
    return pin


def check(ledger, binding):
    pin = current(ledger)
    try: observed = version(binding.provider, binding.version)
    except ValueError: observed = binding.version
    expected = pin["providers"][binding.provider]
    if observed != expected["version"]:
        log.warning("subsystem=provider_cli_pin bug_ref=fleet_cli_version_pin action=refused host=%s provider=%s account=%s revision=%s",
                    binding.host,binding.provider,binding.alias,pin["revision"])
        raise AccountError("provider_cli_pin_mismatch", host=binding.host, provider=binding.provider,
            account=binding.alias, expected_version=expected["version"], observed_version=observed,
            pin_revision=pin["revision"], next_action="review pin and reconcile this cell")
    if not any(a["native_sha256"] == binding.executable_sha256 for a in expected["artifacts"].values()):
        raise AccountError("provider_cli_pin_mismatch", host=binding.host, provider=binding.provider, account=binding.alias,
            pin_revision=pin["revision"], reason="native_hash_mismatch")
    return pin


def consumer_current(ledger,host,local_host,now):
    if host==local_host: return
    target=ledger.conn.execute("SELECT v FROM kv WHERE k=?",("event_push.target_sha",)).fetchone()
    receipt=ledger.conn.execute("SELECT v FROM kv WHERE k=?",("event_push.runtime."+host,)).fetchone()
    try:
        state=json.loads(receipt[0]) if receipt else {}
        if (not target or not target[0] or state.get("sha")!=target[0] or state.get("admitted") is not True
                or not state.get("incarnation") or not state.get("pid")
                or not 0<=now-state.get("observed_at_epoch",0)<=120): raise ValueError()
    except (ValueError,TypeError):
        raise AccountError("provider_cli_verification_unavailable",host=host,reason="satellite_not_current") from None


def set_pin(ledger, message, *, recovery_principal=False):
    operation_id, reason = message.get("operation_id"), message.get("reason")
    if any(not isinstance(x, str) or not 1 <= len(x) <= 160 or not x.strip()
           or any(ord(c) < 32 or ord(c) > 126 for c in x) for x in (operation_id, reason)):
        raise AccountError("provider_cli_pin_request_invalid")
    recovery = message.get("recover_receipt") is not None
    allowed = ({"recover_receipt", "expected_raw_digest", "expected_high_water_revision"} if recovery else {"pin", "expected_revision"})
    if any(k in message for k in ({"pin", "expected_revision", "recover_receipt", "expected_raw_digest", "expected_high_water_revision"} - allowed)):
        raise AccountError("provider_cli_pin_request_invalid")
    arguments = {k: message.get(k) for k in sorted(allowed | {"operation_id", "reason"})}
    with transaction(ledger.conn):
        receipts = history(ledger)
        prior = receipts.get(operation_id)
        if prior:
            if prior["arguments"] != arguments: raise AccountError("provider_cli_pin_id_conflict")
            return prior
        record = raw(ledger)
        initialized = ledger.conn.execute("SELECT v FROM kv WHERE k=?", (INITIALIZED,)).fetchone()
        high_water = max((r["pin"]["revision"] for r in receipts.values()), default=0)
        if (initialized and not receipts) or (receipts and not initialized):
            raise AccountError("provider_cli_pin_unavailable", reason="receipt_history_lost")
        if recovery:
            if not recovery_principal: raise AccountError("account_control_unauthorized")
            source = receipts.get(message["recover_receipt"])
            if not source: raise AccountError("provider_cli_pin_recovery_receipt_unknown")
            if (message.get("expected_raw_digest") != raw_digest(record)
                    or type(message.get("expected_high_water_revision")) is not int
                    or message["expected_high_water_revision"] != high_water): raise AccountError("provider_cli_pin_stale")
            pin = dict(source["pin"], revision=high_water + 1)
        else:
            if type(message.get("expected_revision")) is not int or message["expected_revision"] < 0:
                raise AccountError("provider_cli_pin_request_invalid")
            if record is None:
                if receipts or initialized: raise AccountError("provider_cli_pin_unavailable", reason="pin_absent")
                revision = 0
            else:
                revision = current(ledger)["revision"]
                if revision != high_water: raise AccountError("provider_cli_pin_unavailable", reason="receipt_history_lost")
            if message["expected_revision"] != revision: raise AccountError("provider_cli_pin_stale")
            try: pin = validate(message.get("pin"))
            except ValueError: raise AccountError("provider_cli_pin_request_invalid") from None
            if pin["revision"] != revision + 1: raise AccountError("provider_cli_pin_stale")
        previous=None
        if record is not None:
            try: previous=loads(record)
            except (ValueError,TypeError,UnicodeError): pass
        receipt = {"operation_id": operation_id, "arguments": arguments, "pin": pin, "digest": digest(pin),
                   "previous": previous, "previous_raw_digest": raw_digest(record), "outcome": "pin_set"}
        ledger.conn.execute("INSERT INTO v2_account_recovery VALUES (?,?,?)",
                            (PREFIX + operation_id, "done", canonical(receipt)))
        ledger.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (KEY, canonical(pin)))
        ledger.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (INITIALIZED, "1"))
        log.info("subsystem=provider_cli_pin bug_ref=fleet_cli_version_pin action=pin_set revision=%s",pin["revision"])
        return receipt


def status(ledger):
    value = raw(ledger)
    receipts = history(ledger)
    try: pin = current(ledger)
    except AccountError: pin = None
    return {"pin": pin, "pin_digest": digest(pin) if pin else None, "raw_digest": raw_digest(value),
            "receipt_ids": sorted(receipts),
            "high_water_revision": max((r["pin"]["revision"] for r in receipts.values()), default=0)}
