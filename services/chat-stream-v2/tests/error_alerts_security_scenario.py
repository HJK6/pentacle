"""Actual auth_v2 and typed negative frames on the one owned loopback daemon."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import secrets
import time
import uuid

import websockets
from _shared import operator_auth
from store import STREAM_TOKEN_HASH_VERSION
from error_alerts_fixture import produce_voice, require


async def reply(socket, frame):
    await socket.send(json.dumps(frame))
    async with asyncio.timeout(5):
        while True:
            received = json.loads(await socket.recv())
            if received.get("request_id") == frame["request_id"]:
                return received


@asynccontextmanager
async def peer(h, envelope=None):
    async with websockets.connect(h.url) as socket:
        welcome = json.loads(await socket.recv())
        assert welcome["type"] == "welcome", "fixture welcome missing"
        frames = []
        if envelope:
            identity = operator_auth.decode_envelope(envelope)
            nonce = welcome["auth"]["operator"]["nonce"]
            await socket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "client": identity["client_kind"],
                        "auth_v2": {
                            "scheme": operator_auth.AUTH_SCHEME,
                            "credential_id": identity["credential_id"],
                            "proof": operator_auth.make_proof(
                                identity["proof_key"],
                                nonce,
                                identity["credential_id"],
                                identity["client_kind"],
                            ),
                        },
                    }
                )
            )
            async with asyncio.timeout(5):
                while not any(f.get("type") == "snapshot" for f in frames):
                    frames.append(json.loads(await socket.recv()))
        yield socket, welcome, frames


def commands(h):
    oid = str(uuid.uuid4())
    intent = {
        "version": 1,
        "operation_id": oid,
        "origin_stream_id": "fixture:v2-test",
        "origin_generation": h.origin["session_generation"],
        "operation_kind": "voice_chat",
        "client_build": "auth-fixture",
    }
    return [
        (
            "error.report",
            {
                "version": 1,
                "operation_id": oid,
                "event_id": str(uuid.uuid4()),
                "sequence": 1,
                "stage": "upload",
                "outcome": "failed",
                "code": "upload_read_failed",
                "retry_state": "none",
                "client_build": "auth-fixture",
                "voice_operation": intent,
            },
        ),
    ]


async def denied(h, socket, fields, role):
    accepted_errors = {
        "operator_required",
        "credential_revoked",
        "scope_denied",
        "external_requires_tls",
        "dot_scope_denied",
        "system_producer_forbidden",
        "operator_auth_invalid",
        "hello_required",
        "authentication_required",
    }
    before = len(await h.store.voice_list())
    results = []
    for verb, body in commands(h):
        result = await reply(
            socket, {"type": verb, "request_id": str(uuid.uuid4()), **body, **fields}
        )
        require(
            result.get("error_code") in accepted_errors,
            "A01 unauthorized surface admitted or refused for wrong reason: "
            + role
            + "/"
            + verb,
        )
        require(
            not any(
                key in result
                for key in ("items", "detail", "settings", "alert", "accepted_at")
            ),
            "A01 unauthorized response leaked alert data",
        )
        results.append({"verb": verb, "error_code": result["error_code"]})
    require(
        len(await h.store.voice_list()) == before,
        "A01 denied report created an operation",
    )
    return {"role": role, "refusals": results}


async def authentication(h):
    cells = []
    async with peer(h, h.envelope) as (socket, _, frames):
        snapshot = next(f for f in frames if f["type"] == "snapshot")
        require(
            snapshot.get("capabilities", {}).get("error_alerts_v1") is True,
            "A01 real operator lacks admitted capability",
        )
        require(
            snapshot.get("error_reporting_credential_id") == h.credential_id,
            "A01 reporter principal does not match authenticated credential",
        )
        require(
            not any(
                r.get("producer") == "voice_operation.v1" or r.get("error_context")
                for r in snapshot.get("notifications", [])
            ),
            "A01 global snapshot leaks typed alert facts",
        )
        oid = str(uuid.uuid4())
        read = await reply(
            socket,
            {
                "type": "error.report",
                "request_id": str(uuid.uuid4()),
                "version": 1,
                "operation_id": oid,
                "event_id": str(uuid.uuid4()),
                "sequence": 1,
                "stage": "upload",
                "outcome": "failed",
                "code": "upload_read_failed",
                "retry_state": "none",
                "client_build": "auth-fixture",
                "voice_operation": {
                    "version": 1,
                    "operation_id": oid,
                    "origin_stream_id": "fixture:v2-test",
                    "origin_generation": h.origin["session_generation"],
                    "operation_kind": "voice_chat",
                    "client_build": "auth-fixture",
                },
            },
        )
        require(read.get("type") == "error.report.ok", "A01 actual operator denied")
        # Recover inside the grace so this auth probe never becomes a delivery.
        quiet = await reply(
            socket,
            {
                "type": "error.report",
                "request_id": str(uuid.uuid4()),
                "version": 1,
                "operation_id": oid,
                "event_id": str(uuid.uuid4()),
                "sequence": 2,
                "stage": "upload",
                "outcome": "recovered",
                "code": "recovered",
                "retry_state": "none",
                "client_build": "auth-fixture",
            },
        )
        require(quiet.get("type") == "error.report.ok", "A01 operator recovery denied")
    async with peer(h) as (socket, _, _):
        cells.append(
            await denied(
                h,
                socket,
                {
                    "from_stream_id": "operator:forged",
                    "_auth_context": {
                        "transport": "v2",
                        "operator_authenticated": True,
                        "credential_id": h.credential_id,
                    },
                },
                "anonymous-loopback-forged",
            )
        )
    token = secrets.token_urlsafe(32)
    await h.store.grant_stream_token(
        "fixture",
        "v2-test",
        hashlib.sha256(token.encode()).hexdigest(),
        STREAM_TOKEN_HASH_VERSION,
    )
    old_elevation, old_dot = (
        h.server._seat_operator_authority,
        h.server.dot_principal_stream_ids,
    )
    try:
        for elevation in (False, True):
            h.server._seat_operator_authority = elevation
            async with peer(h) as (socket, _, _):
                cells.append(
                    await denied(
                        h,
                        socket,
                        {"from_stream_id": "fixture:v2-test", "stream_token": token},
                        "elevated-seat" if elevation else "ordinary-seat",
                    )
                )
        h.server.dot_principal_stream_ids = frozenset({"fixture:v2-test"})
        async with peer(h) as (socket, _, _):
            cells.append(
                await denied(
                    h,
                    socket,
                    {"from_stream_id": "fixture:v2-test", "stream_token": token},
                    "dot-on-plain-loopback",
                )
            )
    finally:
        h.server._seat_operator_authority, h.server.dot_principal_stream_ids = (
            old_elevation,
            old_dot,
        )

    _, scoped = h.server.operator_credential_registry.issue(
        "pentacle-mobile",
        label="auth-fixture-scoped",
        scope={"stream": "fixture:v2-test"},
    )
    async with peer(h, scoped) as (socket, _, frames):
        require(
            not next(f for f in frames if f["type"] == "snapshot")
            .get("capabilities", {})
            .get("error_alerts_v1"),
            "A01 scoped credential advertised operator alert capability",
        )
        cells.append(await denied(h, socket, {}, "scoped-operator-credential"))
    cid, revocable = h.server.operator_credential_registry.issue(
        "pentacle-mobile", label="auth-fixture-revoked"
    )
    async with peer(h, revocable) as (socket, _, _):
        h.server.operator_credential_registry.revoke(cid)
        cells.append(await denied(h, socket, {}, "revoked-on-existing-connection"))

    service_file = h.root / "fixture-service-token"
    service_token = secrets.token_urlsafe(32)
    descriptor = os.open(service_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(service_token)
    key = "PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE"
    previous = os.environ.get(key)
    os.environ[key] = str(service_file)
    try:
        async with peer(h) as (socket, _, _):
            await socket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "from_stream_id": "altum-bot-cd",
                        "stream_token": service_token,
                        "subscribe": {"snapshot": False, "mode": "rpc"},
                    }
                )
            )
            hello = json.loads(await asyncio.wait_for(socket.recv(), 5))
            assert (
                hello.get("type") == "hello"
            ), "fixture service credential did not authenticate"
            cells.append(await denied(h, socket, {}, "fixed-system-producer"))
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous

    # Operator registry credentials have revocation, not an invented expiry
    # field. Exercise the actual expiring auth_v2 challenge on its real clock.
    async with peer(h) as (socket, welcome, _):
        challenge = welcome["auth"]["operator"]
        await asyncio.sleep(max(0, challenge["expires_at"] - time.time()) + 0.1)
        identity = operator_auth.decode_envelope(h.envelope)
        result = await reply(
            socket,
            {
                "type": "hello",
                "request_id": str(uuid.uuid4()),
                "client": identity["client_kind"],
                "auth_v2": {
                    "scheme": operator_auth.AUTH_SCHEME,
                    "credential_id": identity["credential_id"],
                    "proof": operator_auth.make_proof(
                        identity["proof_key"],
                        challenge["nonce"],
                        identity["credential_id"],
                        identity["client_kind"],
                    ),
                },
            },
        )
        require(
            result.get("error_code") == "operator_auth_invalid",
            "A01 expired challenge admitted",
        )
        cells.append(
            {"role": "expired-auth-v2-challenge", "error_code": result["error_code"]}
        )
    return {
        "classification": "PASS",
        "scope": "actual loopback auth_v2 and the error.report verb",
        "cases": cells,
        "limitations": [
            "No external TLS/network endpoint claimed; Dot denied at actual plain-transport boundary"
        ],
    }


async def negative_reports(h):
    made = await produce_voice(h)
    oid = made["operation_id"]
    principal = "operator:" + h.credential_id
    frame = {
        "version": 1,
        "operation_id": oid,
        "event_id": str(uuid.uuid4()),
        "sequence": 1,
        "stage": "upload",
        "outcome": "failed",
        "code": "upload_transport_failed",
        "retry_state": "exhausted",
        "client_build": "negative-fixture",
    }
    count = 0
    async with h.client() as client:
        accepted = await client.rpc(
            "error.report", request_id="negative-control", **frame
        )
        require(
            accepted.get("type") == "error.report.ok", "A02 valid control not accepted"
        )
        duplicate = await client.rpc(
            "error.report", request_id="negative-duplicate", **frame
        )
        require(duplicate.get("duplicate") is True, "A02 duplicate report recounted")
        for changes, code in (
            ({"outcome": "unconfirmed"}, "event_conflict"),
            ({"event_id": str(uuid.uuid4())}, "stale_sequence"),
            ({"severity": "critical"}, "invalid_request"),
            ({"recipient": "fixture:private-target"}, "invalid_request"),
            ({"body": "synthetic-private-sentinel"}, "invalid_request"),
            ({"stack": "synthetic-private-sentinel"}, "invalid_request"),
            ({"path": "synthetic-private-sentinel"}, "invalid_request"),
            ({"client_build": "x" * 4097}, "invalid_request"),
            ({"code": "synthetic-private-sentinel"}, "invalid_request"),
        ):
            result = await client.rpc(
                "error.report", request_id="negative-case", **{**frame, **changes}
            )
            require(
                result.get("error_code") == code, "A02 wrong negative report outcome"
            )
            require(
                "synthetic-private-sentinel" not in json.dumps(result),
                "A02 refusal echoed private input",
            )
            count += 1
        # Recover within grace, so negative fixtures cannot create a stale wake.
        await client.rpc(
            "error.report",
            request_id="negative-recovery",
            **{
                **frame,
                "event_id": str(uuid.uuid4()),
                "sequence": 2,
                "outcome": "cancelled",
                "code": "operation_cancelled",
                "retry_state": "none",
            }
        )
    operation = await h.store.voice_get(principal, oid)
    require(
        len(operation["events"]) == 2 and operation["highest_sequence"] == 2,
        "A02 refused frames mutated operation evidence",
    )
    require(
        "synthetic-private-sentinel" not in json.dumps(operation),
        "A02 operation retained private input",
    )
    return {
        "classification": "PASS",
        "scope": "strict frame/idempotency/privacy subcell",
        "negative_count": count,
        "accepted_event_count": 2,
        "remaining": [
            "cross-credential/blob/origin actual wire cases",
            "mobile HTTPS origin held",
        ],
    }
