"""One operator dispatch and advisor ruling authorize one maintenance hold.

All reads and the reservation insert use the caller's BEGIN IMMEDIATE transaction.
The server-only candidate marker carries the advisor resolver, never wire authority.
Clear deliberately depends only on the immutable reservation, not retained grants.
"""
import math
import re

from provider_accounts import AccountError
from provider_cli_pin import digest

DDL = """
CREATE TABLE IF NOT EXISTS v2_maintenance_delegations (
    ruling_tell_id TEXT PRIMARY KEY NOT NULL,
    source_dispatch_id TEXT NOT NULL UNIQUE,
    host TEXT NOT NULL,
    maintenance_id TEXT NOT NULL,
    actor_stream_id TEXT NOT NULL,
    actor_generation TEXT NOT NULL
)
"""
_SCOPE = re.compile(
    r"DELEGATED-HOLD v1 dispatch=(\S+) actor=(\S+)/(\S+) "
    r"host=(\S+) maintenance=(\S+) ttl_max_s=([1-9][0-9]{0,2})", re.ASCII)


def eligible(auth):
    return (auth.get("token_verified") is True and auth.get("peer_loopback") is True
            and not auth.get("dot_principal") and not auth.get("scoped_principal")
            and bool(auth.get("stream_id")) and bool(auth.get("session_generation")))


def invalid(reason):
    raise AccountError("maintenance_delegation_invalid", reason=reason)


def _identity(msg, auth):
    if not eligible(auth):
        invalid("actor")
    pair = msg.get("delegation")
    if not isinstance(pair, dict) or set(pair) != {"source_dispatch_id", "ruling_tell_id"}:
        invalid("delegation")
    if any(not isinstance(value, str) or not value or len(value) > 512
           or any(c.isspace() for c in value) for value in pair.values()):
        invalid("delegation")
    if not isinstance(msg.get("host"), str) or not msg["host"]:
        invalid("host")
    if not isinstance(msg.get("maintenance_id"), str) or not msg["maintenance_id"]:
        invalid("maintenance_id")
    return {**pair, "host": msg["host"], "maintenance_id": msg["maintenance_id"],
            "actor_stream_id": auth["stream_id"], "actor_generation": auth["session_generation"]}


def verify_clear(conn, msg, auth):
    expected = _identity(msg, auth)
    row = conn.execute("SELECT * FROM v2_maintenance_delegations WHERE ruling_tell_id=? AND source_dispatch_id=?",
                       (expected["ruling_tell_id"], expected["source_dispatch_id"])).fetchone()
    if row is None or dict(row) != expected:
        invalid("reservation")
    return dict(row)


def verify_acquire(conn, msg, auth, now):
    from provider_cli_maintenance import FIELDS
    from store import _decode_tell_envelope

    expected = _identity(msg, auth)
    if not FIELDS <= msg.keys():
        invalid("maintenance_fields")
    # Check both keys before scope validation so any reuse for a different hold
    # has the stable consumed code, even after the first hold was cleared.
    reservations = conn.execute(
        "SELECT * FROM v2_maintenance_delegations WHERE ruling_tell_id=? OR source_dispatch_id=?",
        (expected["ruling_tell_id"], expected["source_dispatch_id"])).fetchall()
    if any(dict(row) != expected for row in reservations):
        raise AccountError("maintenance_delegation_consumed")
    authority = msg.get("_delegation_candidate")
    composite = authority._composite() if authority is not None else None
    if composite is None or not composite.enabled:
        invalid("composite")
    route = conn.execute("SELECT * FROM v2_assistant_composite_routes WHERE dispatch_id=?",
                         (expected["source_dispatch_id"],)).fetchone()
    if route is None:
        invalid("dispatch")
    if route["stream_id"] != composite.config.stream_id:
        invalid("composite")
    principal = route["actor_stream_id"]
    if not isinstance(principal, str) or not principal.startswith("operator:") or not principal[9:]:
        invalid("operator_origin")
    if (route["route_target"] != auth["stream_id"]
            or route["route_target_generation"] != auth["session_generation"]):
        invalid("actor")
    if route["delivery_state"] != "landed":
        invalid("dispatch_not_landed")
    envelope = _decode_tell_envelope(conn.execute(
        "SELECT rowid AS ledger_row_id, reply, created_at FROM v2_tell_deliveries WHERE tell_id=?",
        (expected["ruling_tell_id"],)).fetchone())
    tell = envelope.get("delivery") if envelope else None
    if not isinstance(tell, dict):
        invalid("ruling_missing")
    if (tell.get("sender_verified") is not True or tell.get("sender_kind") != "peer"
            or not isinstance(tell.get("sender_generation"), str) or not tell["sender_generation"]):
        invalid("ruling_provenance")
    if tell.get("to_stream_id") != auth["stream_id"]:
        invalid("ruling_recipient")
    try:
        binding = authority.binding_conn(conn)
    except ValueError:
        invalid("advisor_binding")
    if binding.get("state") != "ready":
        invalid("advisor_binding")
    if (tell.get("from_stream_id") != binding["stream_id"]
            or tell["sender_generation"] != binding["generation"]):
        invalid("advisor_rotated")
    body = tell.get("text")
    lines = [line for line in body.splitlines() if line.lstrip().startswith("DELEGATED-HOLD")] if isinstance(body, str) else []
    match = _SCOPE.fullmatch(lines[0]) if len(lines) == 1 else None
    if match is None:
        invalid("scope_line")
    dispatch, actor, generation, host, maintenance, maximum = match.groups()
    scope = {"dispatch": dispatch, "actor": actor + "/" + generation,
             "host": host, "maintenance": maintenance, "ttl_max_s": int(maximum)}
    for key, actual, wanted in (
        ("dispatch", dispatch, expected["source_dispatch_id"]),
        ("actor", (actor, generation), (auth["stream_id"], auth["session_generation"])),
        ("host", host, expected["host"]), ("maintenance", maintenance, expected["maintenance_id"]),
    ):
        if actual != wanted:
            invalid("scope_" + key)
    ttl = msg.get("ttl_s", 900)
    if (isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl)
            or not 0 < ttl <= scope["ttl_max_s"] <= 900):
        invalid("ttl")
    record = {"source_dispatch_id": expected["source_dispatch_id"], "source_event_id": route["event_id"],
              "source_operator_principal": principal, "ruling_tell_id": expected["ruling_tell_id"],
              "ruling_sender_stream_id": tell["from_stream_id"], "ruling_sender_generation": tell["sender_generation"],
              "scope": scope, "scope_digest": digest(scope),
              "advisor_binding": {k: binding[k] for k in ("stream_id", "generation", "source")}, "verified_at": now}
    if not reservations:
        conn.execute("INSERT INTO v2_maintenance_delegations "
                     "(source_dispatch_id,ruling_tell_id,host,maintenance_id,actor_stream_id,actor_generation) "
                     "VALUES (:source_dispatch_id,:ruling_tell_id,:host,:maintenance_id,:actor_stream_id,:actor_generation)", expected)
    return record
