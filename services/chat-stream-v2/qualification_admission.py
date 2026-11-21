"""Bind the first qualification hold to four disposable smoke requests."""
from __future__ import annotations

import json
import os


RECORD = "deploy.first_qualification"
HOLD_PREFIX = "First qualification "
HOLD_UNTIL = 253402300799.0
OBJECTIVE = "Exercise the existing spawn contract"


def smoke_identity(key, provider):
    return f"v2-fleet-smoke-{provider}-{key[:12]}", f"PENTACLE_FLEET_SMOKE_{key}"


def smoke_request(host, key, provider, mode, model, effort):
    name, marker = smoke_identity(key, provider)
    request = {
        "type": "spawn", "objective": OBJECTIVE, "host": host,
        "session_name": name, "provider": provider, "visibility": "hidden",
        "schema": "SpawnRequestV2", "spawn_profile": "desktop_manual",
        "model": model, "effort": effort, "resolution_source": "explicit_override",
        "idempotency_key": key, "account": "ours",
    }
    if mode == "prompted":
        request["initial_prompt"] = f"Reply exactly {marker} and do nothing else."
    return request


def hold_row(conn, host):
    row = conn.execute(
        "SELECT host, held_until, reason, set_at FROM v2_spawn_admission_hold WHERE host=?",
        (host,),
    ).fetchone()
    return dict(zip(("host", "held_until", "reason", "set_at"), row)) if row else None


def record(conn):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (RECORD,)).fetchone()
    return json.loads(row[0]) if row else None


def active_cell(conn, message, now, *, claimed=False, binding=None):
    # No new RPC credential. Unauthenticated and delegated callers never
    # inherit a deployer's pre-sealed request, even if they learn its key.
    auth = message.get("_auth_context") or {}
    key = message.get("idempotency_key")
    host = message.get("host")
    if (not isinstance(host, str) or not isinstance(key, str) or len(key) != 32
            or auth.get("operator_authenticated") is not True
            or auth.get("scoped_principal") or auth.get("token_verified") or auth.get("service_authenticated")):
        return None
    state = record(conn)
    if (not state or state.get("phase") != "testing" or state.get("pid") != os.getpid()
            or state.get("hold", {}).get("host") != host
            or now >= state["expires_at"] or hold_row(conn, host) != state["hold"]):
        return None
    cell = state["cells"].get(key)
    if cell is None:
        return None
    # The server sets its own underscore fields. The catalog and RPC request
    # IDs are separately validated by their normal handlers.
    wire_fields = {k: v for k, v in message.items()
                   if not k.startswith("_") and k not in {"catalog_version", "request_id", "objective_source"}}
    if wire_fields != cell["request"]:
        return None
    request_id = message.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    if claimed:
        if (cell.get("claim") != request_id or binding is None
                or any(binding.get(k) != v for k, v in cell["binding"].items())):
            return None
    elif "claim" in cell:
        return None
    return state


def claim(conn, state, message):
    state["cells"][message["idempotency_key"]]["claim"] = message["request_id"]
    conn.execute("UPDATE kv SET v=? WHERE k=?", (json.dumps(state, sort_keys=True), RECORD))
