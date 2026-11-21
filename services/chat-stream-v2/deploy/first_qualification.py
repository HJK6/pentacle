"""The sole coordinated first-qualification transition."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import uuid

from deploy.coordinated import (Invalid, absolute, atomic_bytes, canonical, file_digest, host_scope,
    private_file, read_json, smoke_account_map, strict_json)
import qualification_admission


CELLS = {(provider, mode) for provider in ("claude", "codex")
         for mode in ("prompted", "promptless")}
GATE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def validate_intent(intent, target):
    authorization = intent["first_qualification"]
    fields = {"prior_exclusion", "prior_stamp_sha256", "catalog_sha256",
              "expected_live", "fd_gate_reference", "expires_at"}
    if not isinstance(authorization, dict) or set(authorization) - {"catalog_change"} != fields:
        raise Invalid("first qualification requires an exact FD authorization")
    live = authorization["expected_live"]
    if (not isinstance(live, dict) or set(live) != {"sha", "pid", "birth"}
            or live["sha"] != intent["prior_sha"]
            or intent["prior_sha"] == intent["target_sha"]
            or type(live["pid"]) is not int or live["pid"] <= 0
            or not isinstance(live["birth"], str) or not live["birth"].strip()):
        raise Invalid("first qualification requires the exact live prior identity")
    for key in ("prior_stamp_sha256", "catalog_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(authorization[key])):
            raise Invalid("first qualification requires exact stamp and catalog digests")
    if not isinstance(authorization["fd_gate_reference"], str) or not GATE_REFERENCE.fullmatch(authorization["fd_gate_reference"]):
        raise Invalid("first qualification requires an FD gate reference")
    exclusion = authorization["prior_exclusion"]
    if not isinstance(exclusion, dict) or not isinstance(exclusion.get("host"), str):
        raise Invalid("first qualification requires a named prior exclusion")
    host = exclusion["host"]
    if (target["optional_hosts"] != [host] or len(target["required_hosts"]) != 2
            or len(target["local_hosts"]) != 1 or target["excluded_hosts"] or host in target["required_hosts"]):
        raise Invalid("first qualification may only add the catalog's sole optional remote host")
    choices = smoke_account_map(intent.get("smoke_accounts", []), target)
    if any(choices.get((host, provider)) != "ours" for provider, _ in CELLS):
        raise Invalid("first qualification requires explicit ours accounts for the bootstrap host")
    validate_window(authorization, require_current=False)
    if "catalog_change" in authorization:
        catalog = authorization["catalog_change"]
        if (not isinstance(catalog, dict) or set(catalog) != {"postimage", "sha256", "fd_gate_reference"}
                or not re.fullmatch(r"[a-f0-9]{64}", str(catalog["sha256"]))
                or not isinstance(catalog["fd_gate_reference"], str) or not GATE_REFERENCE.fullmatch(catalog["fd_gate_reference"])
                or catalog["fd_gate_reference"] == authorization["fd_gate_reference"]):
            raise Invalid("bootstrap host catalog transition requires a separate FD decision")
        absolute(catalog["postimage"])


def validate_window(authorization, *, require_current=True):
    try:
        expiry = datetime.fromisoformat(authorization["expires_at"])
        if expiry.utcoffset() is None:
            raise ValueError("naive timestamp")
    except (TypeError, ValueError) as exc:
        raise Invalid("first qualification approval expiry is invalid") from exc
    if require_current and expiry <= datetime.now(timezone.utc):
        raise Invalid("first qualification approval expired")
    return expiry.timestamp()


def operation_authorization(path, receipt, operation):
    """The private input carries the independently issued FD decision."""
    if path is None:
        raise Invalid("first qualification requires its separate FD operation authorization")
    path = private_file(path)
    if path.parent != Path(receipt["transaction_directory"]):
        raise Invalid("first qualification FD authorization must be in the transaction directory")
    value = read_json(path)
    intent = receipt["intent"]
    grant = intent["first_qualification"]
    expected = {"operation": operation, "transaction_id": intent["transaction_id"],
                "receipt_sha256": receipt["receipt_sha256"], "controller_sha": intent["controller_sha"],
                "prior_sha": intent["prior_sha"], "target_sha": intent["target_sha"]}
    if (set(value) != {*expected, "expires_at", "go_tell_id", "expected_live"}
            or any(value[key] != item for key, item in expected.items())
            or not isinstance(value["go_tell_id"], str) or not GATE_REFERENCE.fullmatch(value["go_tell_id"])
            or value["go_tell_id"] == grant["fd_gate_reference"]):
        raise Invalid("first qualification FD authorization does not bind this operation and receipt")
    live = value["expected_live"]
    if (not isinstance(live, dict) or set(live) != {"sha", "pid", "birth"}
            or live["sha"] not in {intent["prior_sha"], intent["target_sha"]}
            or (live["pid"] is None and live["birth"] is not None)
            or (live["pid"] is not None and (type(live["pid"]) is not int or live["pid"] <= 0
                or not isinstance(live["birth"], str) or not live["birth"].strip()))
            or (operation == "apply" and live != grant["expected_live"])):
        raise Invalid("first qualification FD authorization does not bind the expected process")
    return {"operation": operation, "go_tell_id": value["go_tell_id"], "sha256": file_digest(path),
            "expires_at": validate_window(value), "expected_live": live}


def prior_scope(intent, machines, stamp, stamp_sha256, catalog_sha256):
    from deploy.restoration import _inherited_receipt

    authorization = intent["first_qualification"]
    if (authorization["prior_stamp_sha256"] != stamp_sha256
            or authorization["catalog_sha256"] != catalog_sha256):
        raise Invalid("first qualification stamp or catalog differs from FD approval")
    exclusion = authorization["prior_exclusion"]
    scope = host_scope(machines, [exclusion])
    if stamp.get("coordinated_phase") != "committed" or not stamp.get("coordinated_receipt"):
        raise Invalid("first qualification requires a committed prior receipt")
    prior_receipt = _inherited_receipt(stamp["coordinated_receipt"])
    if (prior_receipt.get("intent", {}).get("excluded_hosts") != [exclusion]
            or prior_receipt.get("scope", {}).get("included_hosts") != scope["included_hosts"]):
        raise Invalid("prior receipt did not seal the exact bootstrap host exclusion and covered scope")
    smoke = stamp.get("fleet_smoke", {})
    rows = [r for r in smoke.get("cells", []) if r.get("host") == exclusion["host"]]
    if (len(rows) != len(CELLS)
            or {(r.get("provider"), r.get("prompt_mode")) for r in rows} != CELLS
            or any(r.get("outcome") != "SKIPPED_DECLARED"
                   or r.get("authority") != exclusion["authority"]
                   or r.get("reason") != exclusion["reason"] for r in rows)
            or {r.get("host") for r in smoke.get("cells", []) if r.get("outcome") == "passed"}
               != set(scope["included_hosts"])):
        raise Invalid("committed prior does not contain the exact bootstrap host skips and covered hosts")
    return scope


def check_committed_stamp(stamp, receipt):
    journal = read_json(Path(receipt["transaction_directory"]) / "journal.json")
    evidence = journal.get("result", {})
    qualification = stamp.get("first_qualification", {})
    if (journal.get("phase") != "committed" or journal.get("receipt_sha256") != receipt["receipt_sha256"]
            or stamp.get("coordinated_phase") != "committed" or evidence.get("outcome") != "passed"
            or not qualification.get("runtime") or evidence.get("qualification_instance") != qualification["runtime"]
            or evidence.get("fleet_smoke") != stamp.get("fleet_smoke")):
        raise Invalid("bootstrap host qualification has no committed matching fleet proof")


def verify_catalog_delta(before, after, exclusion):
    old = strict_json(before)
    new = strict_json(after)
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise Invalid("first qualification catalog transition requires an object")
    declarations = old.get("release_exclusions", [])
    if not isinstance(declarations, list) or any(not isinstance(row, dict) for row in declarations):
        raise Invalid("invalid first qualification catalog declarations")
    host = exclusion["host"]
    matches = [r for r in declarations if r.get("host") == host]
    if (len(matches) != 1 or matches[0].get("reason") != exclusion["reason"]
            or matches[0].get("readiness_item") != exclusion["authority"]
            or new != {**old, "release_exclusions": [r for r in declarations if r.get("host") != host]}):
        raise Invalid("catalog change may only remove the sealed bootstrap host exclusion")


def catalog_info(path):
    path = absolute(str(path))
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise Invalid("machine catalog must be an owned regular file")
    return {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}


def prepare_catalog(intent, before, folder):
    grant = intent["first_qualification"]
    record = {"metadata": catalog_info(intent["machines_file"]), "target_snapshot": str(before),
              "target_sha256": grant["catalog_sha256"]}
    if "catalog_change" in grant:
        change = grant["catalog_change"]
        postimage = private_file(change["postimage"]).read_bytes()
        verify_catalog_delta(before.read_bytes(), postimage, grant["prior_exclusion"])
        target = folder / "machines.after.json"
        atomic_bytes(target, postimage)
        if file_digest(target) != change["sha256"]:
            raise Invalid("catalog postimage differs from FD approval")
        record.update(target_snapshot=str(target), target_sha256=change["sha256"])
    return record


def check_catalog(receipt):
    grant = receipt["intent"]["first_qualification"]
    record = receipt["qualification_catalog"]
    folder = Path(receipt["transaction_directory"])
    prior = private_file(folder / "machines.json")
    expected_target = folder / "machines.after.json" if "catalog_change" in grant else prior
    target_sha256 = grant["catalog_change"]["sha256"] if "catalog_change" in grant else grant["catalog_sha256"]
    target = private_file(record["target_snapshot"])
    current = receipt["intent"]["machines_file"]
    if (receipt["machines_snapshot"] != str(prior) or target != expected_target
            or receipt["machines_sha256"] != grant["catalog_sha256"]
            or file_digest(prior) != grant["catalog_sha256"]
            or file_digest(target) != record["target_sha256"]
            or record["target_sha256"] != target_sha256
            or catalog_info(current) != record["metadata"]):
        raise Invalid("sealed bootstrap host catalog images or metadata drift")
    if target != prior:
        verify_catalog_delta(prior.read_bytes(), target.read_bytes(), grant["prior_exclusion"])
    observed = file_digest(current)
    if observed not in {grant["catalog_sha256"], record["target_sha256"]}:
        raise Invalid("unexplained bootstrap host catalog drift")
    return observed


def change_catalog(receipt, *, rollback):
    """Compare and swap the two approved catalog images, under the deploy lock."""
    record = receipt["qualification_catalog"]
    live = Path(receipt["intent"]["machines_file"])
    observed = check_catalog(receipt)
    wanted = receipt["machines_sha256"] if rollback else record["target_sha256"]
    if observed == wanted:
        return
    image = receipt["machines_snapshot"] if rollback else record["target_snapshot"]
    with tempfile.NamedTemporaryFile(dir=live.parent, prefix=".qualification-catalog-", delete=False) as scratch:
        temporary = Path(scratch.name)
        try:
            metadata = record["metadata"]
            if os.fstat(scratch.fileno()).st_gid != metadata["gid"]:
                os.fchown(scratch.fileno(), -1, metadata["gid"])
            os.fchmod(scratch.fileno(), metadata["mode"])
            scratch.write(private_file(image).read_bytes())
            scratch.flush()
            os.fsync(scratch.fileno())
            if file_digest(temporary) != wanted or check_catalog(receipt) != observed:
                raise Invalid("machine catalog changed during compare and swap")
            os.replace(temporary, live)
            directory = os.open(live.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    if check_catalog(receipt) != wanted:
        raise Invalid("machine catalog compare and swap readback differs")


def prepare_cells(host, plan, accounts):
    from deploy.restoration import BINDING_FIELDS

    bindings = {b["provider"]: {key: b[key] for key in BINDING_FIELDS}
                for b in accounts["selected_bindings"] if b["host"] == host and b["alias"] == "ours"}
    cells = {}
    for row in plan["cells"]:
        if row["host"] != host:
            continue
        provider, mode = row["provider"], row["prompt_mode"]
        model = {"claude": "claude-haiku-5-5", "codex": "gpt-6-luna"}[provider]
        if (row["model"], row["effort"], row["account"]) != (model, "low", "ours"):
            raise Invalid("bootstrap host qualification requires the approved low-effort model tuples")
        key = uuid.uuid4().hex
        cells[key] = {"request": qualification_admission.smoke_request(host, key, provider, mode, model, "low"),
                      "binding": bindings[provider]}
    if len(cells) != len(CELLS):
        raise Invalid("incomplete first qualification request population")
    return cells


def read_admission(conn, host):
    return qualification_admission.hold_row(conn, host), qualification_admission.record(conn)


def owned_hold(hold, state):
    return bool(hold and isinstance(state, dict) and state.get("hold") == hold
                and isinstance(state.get("seal"), str) and re.fullmatch(r"[0-9a-f]{64}", state["seal"])
                and hold["reason"] == qualification_admission.HOLD_PREFIX + state["seal"]
                and hold["held_until"] == qualification_admission.HOLD_UNTIL)


def read_runtime_admission(database, host):
    with sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("BEGIN")
        return read_admission(conn, host)


def change_admission(receipt, phase, *, pid=None, cells=None, deadline=None):
    """Publish or revoke one exact transaction's permission by SQLite CAS."""
    database = receipt["accounts"]["database"]
    seal = receipt["receipt_sha256"]
    host = receipt["intent"]["first_qualification"]["prior_exclusion"]["host"]
    with sqlite3.connect(database) as conn:
        conn.execute("BEGIN IMMEDIATE")
        hold, state = read_admission(conn, host)
        recorded_hold, recorded_state = receipt["qualification_admission_before"]
        ours = owned_hold(hold, state) and state["seal"] == seal
        captured = [hold, state] == [recorded_hold, recorded_state]
        if phase == "rollback" and recorded_hold is not None and (ours or captured or (hold is None and state is None)):
            conn.execute("INSERT OR REPLACE INTO v2_spawn_admission_hold(host,held_until,reason,set_at) VALUES (?,?,?,?)",
                         tuple(recorded_hold[key] for key in ("host", "held_until", "reason", "set_at")))
            conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (qualification_admission.RECORD, canonical(recorded_state).decode()))
            return
        if phase == "fenced" and captured:
            if conn.execute("SELECT 1 FROM sessions WHERE host=? AND status='open'", (host,)).fetchone():
                raise Invalid("existing bootstrap host work blocks first qualification")
            if conn.execute("SELECT 1 FROM v2_stream_reservations WHERE host=?", (host,)).fetchone():
                raise Invalid("existing bootstrap host reservation blocks first qualification")
            hold = {"host": host, "held_until": qualification_admission.HOLD_UNTIL,
                    "reason": qualification_admission.HOLD_PREFIX + seal, "set_at": time.time()}
            conn.execute("INSERT OR REPLACE INTO v2_spawn_admission_hold(host,held_until,reason,set_at) VALUES (?,?,?,?)",
                         tuple(hold[key] for key in ("host", "held_until", "reason", "set_at")))
        elif phase == "rollback" and [hold, state] == [None, None]:
            # A committed transaction has released its hold. Re-establish it
            # under the stopped-process fence before restoring the old route.
            hold = {"host": host, "held_until": qualification_admission.HOLD_UNTIL,
                    "reason": qualification_admission.HOLD_PREFIX + seal, "set_at": time.time()}
            conn.execute("INSERT INTO v2_spawn_admission_hold(host,held_until,reason,set_at) VALUES (?,?,?,?)",
                         tuple(hold[key] for key in ("host", "held_until", "reason", "set_at")))
        elif not ours:
            raise Invalid("first qualification admission hold changed")
        if phase == "testing" and state["phase"] != "fenced":
            raise Invalid("first qualification can only arm a fresh owned hold")
        if phase == "release":
            if (state["phase"] != "testing" or len(state["cells"]) != len(CELLS)
                    or time.time() >= state["expires_at"]
                    or any("claim" not in cell for cell in state["cells"].values())):
                raise Invalid("cannot release bootstrap host before all qualification claims")
            conn.execute("DELETE FROM kv WHERE k=?", (qualification_admission.RECORD,))
            conn.execute("DELETE FROM v2_spawn_admission_hold WHERE host=?", (host,))
            return
        expires = validate_window(receipt["intent"]["first_qualification"], require_current=False)
        if deadline is not None:
            expires = min(expires, deadline)
        state = {"seal": seal, "phase": phase, "pid": pid, "expires_at": expires,
                 "hold": hold, "cells": cells or {}}
        conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (qualification_admission.RECORD, canonical(state).decode()))
