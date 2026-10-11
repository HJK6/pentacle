"""R5/R9 through real authenticated ingress and disposable Store/blob files.

Only the remote transcription/provider boundary in ErrorAlertsHarness is fake.
No upload store, principal, authentication result, or dispatcher is substituted.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid

import pytest
import pytest_asyncio
import websockets

from error_alerts_fixture import ErrorAlertsHarness
from cosmo_e2e.harness import ScopedMobileClient
from voice_uploads import CHUNK_BYTES, MAX_BYTES, EXPIRY_SECONDS


@pytest_asyncio.fixture
async def voice_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("PENTACLE_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", "record-only")
    h = ErrorAlertsHarness(tmp_path)
    try:
        await h.start()
        yield h
    finally:
        await h.stop()
        assert h.cleanup["errors"] == []


def metadata(h, data=b"synthetic voice bytes", **overrides):
    return {"protocol": 2, "upload_id": str(uuid.uuid4()), "total_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "mime": "audio/mp4",
            "voice_operation": {"version": 1, "operation_id": str(uuid.uuid4()),
                "origin_stream_id": "fixture:v2-test", "origin_generation": h.origin["session_generation"],
                "operation_kind": "voice_chat", "client_build": "voice-v2-fixture"}, **overrides}


def chunk(meta, data, offset=0, final=False, **overrides):
    return {"protocol": 2, "upload_id": meta["upload_id"], "offset": offset,
            "data_b64": base64.b64encode(data).decode(), "chunk_sha256": hashlib.sha256(data).hexdigest(),
            "final": final, **overrides}


async def init(client, meta, rid=None):
    return await client.rpc("upload_blob_init", request_id=rid or "init-" + uuid.uuid4().hex, **meta)


async def send_chunk(client, meta, data, offset=0, final=False, **overrides):
    return await client.rpc("upload_blob_chunk", request_id="chunk-" + uuid.uuid4().hex,
                            **chunk(meta, data, offset, final, **overrides))


def path_for(h, meta, credential_id=None):
    return h.blobs.voice_uploads._path("operator:" + (credential_id or h.credential_id), meta["upload_id"])


def checkpoint(h, meta):
    return json.loads((path_for(h, meta) / "checkpoint.json").read_text())


@pytest.mark.asyncio
async def test_r5_ack_disconnect_resume_lost_final_and_restart(voice_harness):
    h = voice_harness
    data = bytes(range(256)) * 600
    m = metadata(h, data)
    async with h.client() as c:
        started = await init(c, m)
        assert started == {"type": "upload_blob.init.ok", "request_id": started["request_id"],
                           "protocol": 2, "upload_id": m["upload_id"], "next_offset": 0, "complete": False}
        first = await send_chunk(c, m, data[:CHUNK_BYTES])
        assert first["next_offset"] == CHUNK_BYTES and first["complete"] is False
        assert "blob_sha" not in first
        assert checkpoint(h, m)["next_offset"] == CHUNK_BYTES
        assert (path_for(h, m) / "audio.part").read_bytes() == data[:CHUNK_BYTES]
    async with h.client() as c:
        assert (await init(c, m))["next_offset"] == CHUNK_BYTES
        replay = await send_chunk(c, m, data[:CHUNK_BYTES])
        assert replay["next_offset"] == CHUNK_BYTES
        await send_chunk(c, m, data[CHUNK_BYTES:2*CHUNK_BYTES], CHUNK_BYTES)
        done = await send_chunk(c, m, data[2*CHUNK_BYTES:], 2*CHUNK_BYTES, True)
        assert done["complete"] is True and done["blob_sha"] == m["sha256"]
        assert checkpoint(h, m)["blob_sha"] == m["sha256"]
        assert (await send_chunk(c, m, data[2*CHUNK_BYTES:], 2*CHUNK_BYTES, True))["blob_sha"] == m["sha256"]
        assert (await send_chunk(c, m, b"", len(data), True))["complete"] is True
    await h.restart()
    async with h.client() as c:
        recovered = await init(c, m)
        assert recovered["complete"] is True and recovered["next_offset"] == len(data)
        assert recovered["blob_sha"] == m["sha256"]
        assert await h.blobs.read_verified(m["sha256"]) == data
        operation = await h.store.voice_get("operator:" + h.credential_id, m["voice_operation"]["operation_id"])
        assert operation["data"]["upload_request_id"] == m["upload_id"]
        assert operation["data"]["upload_protocol"] == 2
        assert operation["data"]["upload_committed"]
        assert operation["data"]["blob_sha"] == m["sha256"]


@pytest.mark.asyncio
async def test_total_nonfinal_needs_explicit_empty_final(voice_harness):
    h = voice_harness
    data = b"whole but not yet finalized"
    m = metadata(h, data)
    async with h.client() as c:
        await init(c, m)
        ack = await send_chunk(c, m, data)
        assert ack["next_offset"] == len(data) and ack["complete"] is False
    await h.restart()
    async with h.client() as c:
        assert (await init(c, m))["complete"] is False
        done = await send_chunk(c, m, b"", len(data), True)
        assert done["complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"sha256": "0" * 64}, {"total_bytes": 99}, {"mime": "audio/wav"},
])
async def test_conflicting_metadata_retains_original(voice_harness, change):
    h = voice_harness
    data = b"immutable audio"
    m = metadata(h, data)
    async with h.client() as c:
        await init(c, m)
        await send_chunk(c, m, data[:4])
        assert (await init(c, {**m, **change}))["error_code"] == "upload_conflict"
        assert (await init(c, m))["next_offset"] == 4
        assert (await send_chunk(c, m, data[4:], 4, True))["complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [
    ({"offset": 2}, "offset_mismatch"),
    ({"data_b64": "@@@"}, "invalid_request"),
    ({"chunk_sha256": "0" * 64}, "digest_mismatch"),
    ({"offset": True}, "invalid_request"),
    ({"final": "yes"}, "invalid_request"),
    ({"credential_id": "forged"}, "invalid_request"),
    ({"principal": "operator:forged"}, "invalid_request"),
    ({"data_b64": base64.b64encode(b"a" * (CHUNK_BYTES + 1)).decode()}, "invalid_request"),
    ({"final": True}, "offset_mismatch"),
])
async def test_invalid_chunks_do_not_change_checkpoint(voice_harness, change, code):
    h = voice_harness
    m = metadata(h, b"abcdefgh")
    async with h.client() as c:
        await init(c, m)
        before = checkpoint(h, m)
        result = await send_chunk(c, m, b"abcd", **change)
        assert result["error_code"] == code
        assert set(result) == {"type", "request_id", "protocol", "error_code"}
        assert checkpoint(h, m) == before
        assert (path_for(h, m) / "audio.part").read_bytes() == b""


@pytest.mark.asyncio
async def test_conflicting_duplicate_overlap_hole_and_whole_digest(voice_harness):
    h = voice_harness
    m = metadata(h, b"abcdefgh")
    async with h.client() as c:
        await init(c, m)
        await send_chunk(c, m, b"abcd")
        for data, offset, code in ((b"xxxx", 0, "upload_conflict"), (b"cdef", 2, "offset_mismatch"), (b"h", 7, "offset_mismatch")):
            assert (await send_chunk(c, m, data, offset))["error_code"] == code
        assert (await send_chunk(c, m, b"zzzz", 4, True))["error_code"] == "digest_mismatch"
        assert checkpoint(h, m)["next_offset"] == 4
        assert (await init(c, m))["next_offset"] == 4
        assert (path_for(h, m) / "audio.part").read_bytes() == b"abcd"
        assert (await send_chunk(c, m, b"efgh", 4, True))["complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"total_bytes": 0}, {"total_bytes": MAX_BYTES + 1}, {"total_bytes": True},
    {"mime": "image/png"}, {"sha256": "A" * 64}, {"upload_id": "../../escape"},
    {"purpose": "chat_attachment"}, {"credential_id": "forged"},
    {"voice_operation": {}}, {"voice_operation": None},
])
async def test_invalid_init_never_reserves_or_registers(voice_harness, change):
    h = voice_harness
    async with h.client() as c:
        result = await init(c, metadata(h, **change))
        assert result["error_code"] == "invalid_request"
        assert list(h.blobs.voice_uploads.root.rglob("checkpoint.json")) == []
        assert await h.store.voice_list() == []


@pytest.mark.asyncio
async def test_real_revocation_and_anonymous_wire_forgery(voice_harness):
    h = voice_harness
    m = metadata(h, b"abcdefgh")
    async with h.client() as c:
        await init(c, m)
        await send_chunk(c, m, b"abcd")
        h.server.operator_credential_registry.revoke(h.credential_id)
        assert (await send_chunk(c, m, b"efgh", 4, True))["error_code"] == "upload_forbidden"
        assert (await init(c, m))["error_code"] == "upload_forbidden"
        assert checkpoint(h, m)["next_offset"] == 4
    async with websockets.connect(h.url) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "upload_blob_init", "request_id": "forged", **m,
            "_auth_context": {"operator_authenticated": True, "transport": "v2", "credential_id": h.credential_id}}))
        async with asyncio.timeout(5):
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("request_id") == "forged":
                    break
        assert reply["error_code"] == "upload_forbidden"
        assert "next_offset" not in reply and "blob_sha" not in reply


@pytest.mark.asyncio
async def test_other_valid_credential_cannot_read_or_complete_prior_session(voice_harness):
    h = voice_harness
    m = metadata(h, b"abcdefgh")
    async with h.client() as c:
        await init(c, m)
        await send_chunk(c, m, b"abcd")
    cid, envelope = h.server.operator_credential_registry.issue("pentacle-mobile", label="other-synthetic")
    async with ScopedMobileClient(h.url, envelope) as other:
        denied = await send_chunk(other, m, b"efgh", 4, True)
        assert denied["error_code"] == "upload_expired" and "next_offset" not in denied
        # The composite namespace permits independent ownership, never a resume.
        own = await init(other, m)
        assert own["next_offset"] == 0 and not own["complete"]
        assert path_for(h, m, cid) != path_for(h, m)
    async with h.client() as c:
        assert (await init(c, m))["next_offset"] == 4
        assert (await send_chunk(c, m, b"efgh", 4, True))["complete"]


@pytest.mark.asyncio
async def test_per_principal_quota_reserves_advertised_bytes_and_releases_completion(voice_harness):
    h = voice_harness
    async with h.client() as c:
        active = [metadata(h, b"x", total_bytes=MAX_BYTES) for _ in range(9)]
        for m in active:
            assert (await init(c, m))["next_offset"] == 0
        last = metadata(h, b"small")
        assert (await init(c, last))["next_offset"] == 0
        assert (await init(c, metadata(h)))["error_code"] == "upload_quota"
        assert (await send_chunk(c, last, b"small", final=True))["complete"]
        assert (await init(c, metadata(h)))["next_offset"] == 0


@pytest.mark.asyncio
async def test_global_quota_across_real_principals(voice_harness):
    h = voice_harness
    # Sixty-four 16MiB reservations fill 1GiB without allocating sparse audio.
    for index in range(7):
        _, envelope = h.server.operator_credential_registry.issue("pentacle-mobile", label=f"quota-{index}")
        async with ScopedMobileClient(h.url, envelope) as c:
            for _ in range(10 if index < 6 else 4):
                assert (await init(c, metadata(h, total_bytes=MAX_BYTES)))["next_offset"] == 0
            if index == 6:
                assert (await init(c, metadata(h)))["error_code"] == "upload_quota"
    assert sum(p.stat().st_size for p in h.blobs.voice_uploads.root.rglob("audio.part")) == 0


@pytest.mark.asyncio
async def test_expiry_restarts_zero_and_never_removes_canonical(voice_harness):
    h = voice_harness
    partial = metadata(h, b"abcdefgh")
    complete = metadata(h, b"complete")
    async with h.client() as c:
        await init(c, partial)
        await send_chunk(c, partial, b"abcd")
        await init(c, complete)
        await send_chunk(c, complete, b"complete", final=True)
        for m in (partial, complete):
            s = checkpoint(h, m)
            s["last_activity"] -= EXPIRY_SECONDS + 1
            h.blobs.voice_uploads._save(path_for(h, m), s)
        assert (await send_chunk(c, partial, b"efgh", 4, True))["error_code"] == "upload_expired"
        assert (await init(c, partial))["next_offset"] == 0
        assert (await init(c, complete))["next_offset"] == 0
        assert await h.blobs.read_verified(complete["sha256"]) == b"complete"


@pytest.mark.asyncio
async def test_stale_writer_cannot_overwrite_current_accepted_bytes(voice_harness):
    h = voice_harness
    m = metadata(h, b"abcdefgh")
    async with h.client() as first, h.client() as second:
        await init(first, m)
        await init(second, m)
        results = await asyncio.gather(send_chunk(first, m, b"abcd"), send_chunk(second, m, b"xxxx"))
        assert sum(r.get("next_offset") == 4 for r in results) == 1
        assert sum(r.get("error_code") == "upload_conflict" for r in results) == 1
        assert (path_for(h, m) / "audio.part").read_bytes() in (b"abcd", b"xxxx")


@pytest.mark.asyncio
async def test_v1_nonfinal_wire_unchanged_and_disconnect_discards(voice_harness):
    h = voice_harness
    async with h.client() as c:
        assert (await c.rpc("upload_blob_init", request_id="legacy"))["type"] == "upload_blob.init.ok"
        ws = next(w for w in h.server._clients if h.server._operator_authenticated(w))
        result = await h.server._dispatch(json.dumps({"type": "upload_blob_chunk", "request_id": "legacy",
            "data_b64": base64.b64encode(b"legacy").decode(), "final": False}), websocket=ws)
        assert result == []
        await h.blobs.abort_connection(ws)
        assert not h.blobs._uploads
        assert not list(h.blobs._tmp.iterdir())


async def raw_rpc(ws, verb, **fields):
    rid = "raw-" + uuid.uuid4().hex
    await ws.send(json.dumps({"type": verb, "request_id": rid, **fields}))
    async with asyncio.timeout(5):
        while True:
            response = json.loads(await ws.recv())
            if response.get("request_id") == rid:
                return response


@pytest.mark.asyncio
async def test_real_scoped_credential_cannot_use_protocol_two(voice_harness):
    h = voice_harness
    _, envelope = h.server.operator_credential_registry.issue(
        "pentacle-mobile", label="scoped-synthetic", scope={"stream": "fixture:assistant"})
    async with ScopedMobileClient(h.url, envelope) as c:
        m = metadata(h)
        assert (await init(c, m))["error_code"] == "upload_forbidden"
        assert (await send_chunk(c, m, b"x"))["error_code"] == "upload_forbidden"
    assert not list(h.blobs.voice_uploads.root.rglob("checkpoint.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["seat", "service"])
async def test_real_nonoperator_token_cannot_use_protocol_two(voice_harness, monkeypatch, kind):
    from server import FIXED_SYSTEM_PRODUCER_STREAM_ID
    from store import STREAM_TOKEN_HASH_VERSION
    h = voice_harness
    token = "synthetic-voice-nonoperator-token"
    if kind == "seat":
        origin = await h.sessions.open("fixture", "voice-seat", provider="codex", visibility="visible")
        assert await h.store.grant_stream_token("fixture", "voice-seat", hashlib.sha256(token.encode()).hexdigest(),
                                                STREAM_TOKEN_HASH_VERSION) == "ok"
        actor = "fixture:voice-seat"
    else:
        token_file = h.root / "synthetic-service-token"
        token_file.write_text(token)
        token_file.chmod(0o600)
        monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_ID", FIXED_SYSTEM_PRODUCER_STREAM_ID)
        monkeypatch.setenv("PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE", str(token_file))
        actor = FIXED_SYSTEM_PRODUCER_STREAM_ID
    async with websockets.connect(h.url) as ws:
        await ws.recv()
        hello = await raw_rpc(ws, "hello", from_stream_id=actor, stream_token=token,
                              subscribe={"mode": "rpc", "snapshot": False})
        assert hello["type"] == "ready", hello
        m = metadata(h)
        assert (await raw_rpc(ws, "upload_blob_init", **m))["error_code"] == "upload_forbidden"
        assert (await raw_rpc(ws, "upload_blob_chunk", **chunk(m, b"x")))["error_code"] == "upload_forbidden"
    assert not list(h.blobs.voice_uploads.root.rglob("checkpoint.json"))
