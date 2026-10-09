"""Real authenticated WS checks. Requires an explicitly granted runtime window."""

import base64
import hashlib
import json
import uuid

import pytest
import pytest_asyncio
import websockets
from jsonschema import Draft202012Validator, FormatChecker

from error_alerts import SCHEMA
from error_alerts_fixture import ErrorAlertsHarness


@pytest_asyncio.fixture
async def wire(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("PENTACLE_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", "record-only")
    h = ErrorAlertsHarness(tmp_path)
    try:
        await h.start()
        yield h
    finally:
        await h.stop()


async def marked_upload(h, client):
    intent = {
        "version": 1,
        "operation_id": str(uuid.uuid4()),
        "origin_stream_id": "fixture:v2-test",
        "origin_generation": h.origin["session_generation"],
        "operation_kind": "voice_chat",
        "client_build": "wire-fixture",
    }
    rid = "upload-" + uuid.uuid4().hex
    init = await client.rpc(
        "upload_blob_init", request_id=rid, purpose="generic", voice_operation=intent
    )
    assert init["type"] == "upload_blob.init.ok"
    op = await h.store.voice_get("operator:" + h.credential_id, intent["operation_id"])
    assert op is not None, "operation must commit before init ACK"
    content = b"disposable-voice-bytes"
    done = await client.rpc(
        "upload_blob_chunk",
        request_id=rid,
        data_b64=base64.b64encode(content).decode(),
        final=True,
    )
    assert done["type"] == "upload_blob.ok"
    op = await h.store.voice_get("operator:" + h.credential_id, intent["operation_id"])
    assert op["data"]["blob_sha"] == hashlib.sha256(content).hexdigest()
    assert op["data"]["upload_committed"]
    return intent, done


@pytest.mark.asyncio
async def test_wire_report_durable_ack_and_strict_negative_frames(wire):
    h = wire
    async with h.client() as client:
        intent, _ = await marked_upload(h, client)
        fields = {
            "version": 1,
            "event_id": str(uuid.uuid4()),
            "operation_id": intent["operation_id"],
            "sequence": 1,
            "stage": "upload",
            "outcome": "failed",
            "code": "upload_transport_failed",
            "retry_state": "exhausted",
            "client_build": "wire-fixture",
        }
        first = await client.rpc("error.report", request_id="first", **fields)
        assert first["type"] == "error.report.ok"
        duplicate = await client.rpc("error.report", request_id="retry", **fields)
        assert duplicate["duplicate"] is True
        for addition in (
            {"severity": "critical"},
            {"recipient": "fixture:other"},
            {"body": "invalid"},
            {"client_build": "x" * 4097},
            {"sequence": 0},
            {"code": "arbitrary"},
        ):
            denied = await client.rpc(
                "error.report", request_id="negative", **{**fields, **addition}
            )
            assert denied["error_code"] == "invalid_request"
        op = await h.store.voice_get(
            "operator:" + h.credential_id, intent["operation_id"]
        )
        assert len(op["events"]) == 1
        assert op["highest_sequence"] == 1


@pytest.mark.asyncio
async def test_wire_operator_schema_settings_and_mid_connection_revocation(wire):
    h = wire
    async with h.client() as client:
        for verb in ("error.list", "error.settings.get", "error.settings.audit"):
            reply = await client.rpc(verb, request_id=verb)
            Draft202012Validator(
                {**SCHEMA["responses"][verb], "$defs": SCHEMA["$defs"]},
                format_checker=FormatChecker(),
            ).validate(reply)
        updated = await client.rpc(
            "error.settings.set",
            request_id="set",
            family="voice_operation.v1",
            expected_revision=0,
            delivery_mode="digest",
        )
        assert updated["setting"]["revision"] == 1
        stale = await client.rpc(
            "error.settings.set",
            request_id="stale",
            family="voice_operation.v1",
            expected_revision=0,
            delivery_mode="muted",
        )
        assert stale["error_code"] == "revision_conflict"
        h.server.operator_credential_registry.revoke(h.credential_id)
        assert (await client.rpc("error.list", request_id="revoked"))[
            "error_code"
        ] == "credential_revoked"


@pytest.mark.asyncio
async def test_wire_anonymous_loopback_cannot_forge_operator_context(wire):
    async with websockets.connect(wire.url) as client:
        assert json.loads(await client.recv())["type"] == "welcome"
        await client.send(
            json.dumps(
                {
                    "type": "error.list",
                    "request_id": "forged",
                    "_auth_context": {
                        "operator_authenticated": True,
                        "transport": "v2",
                        "credential_id": wire.credential_id,
                    },
                }
            )
        )
        async with __import__("asyncio").timeout(5):
            while True:
                reply = json.loads(await client.recv())
                if reply.get("request_id") == "forged":
                    break
        assert reply.get("error_code") in (
            "operator_required",
            "hello_required",
            "authentication_required",
        )
        assert "items" not in reply


@pytest.mark.asyncio
async def test_wire_generic_upload_stays_unmarked(wire):
    async with wire.client() as client:
        rid = "legacy-generic"
        assert (
            await client.rpc("upload_blob_init", request_id=rid, purpose="generic")
        )["type"] == "upload_blob.init.ok"
        assert (
            await client.rpc(
                "upload_blob_chunk",
                request_id=rid,
                data_b64=base64.b64encode(b"legacy-fixture").decode(),
                final=True,
            )
        )["type"] == "upload_blob.ok"
        assert await wire.store.voice_list() == []
