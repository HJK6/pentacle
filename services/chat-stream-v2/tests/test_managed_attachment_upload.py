"""Synthetic managed-upload contracts; no fleet credentials, sockets or live DB."""
import asyncio
import base64
from pathlib import Path

from store import Store

from blobs import BlobStore, Promptless, CHUNK_MAX_BYTES, TOTAL_MAX_BYTES

AUTH = {"token_verified": True, "stream_id": "test-host:producer", "session_generation": "synthetic-generation-1", "operator_authenticated": False}


def test_chat_attachment_running_cap_does_not_trust_size_hint(tmp_path):
    async def run():
        async with fixture(tmp_path) as (store, persistent):
            owner = object()
            init = await store._on_init({"request_id": "synthetic-large", "purpose": "chat_attachment", "filename": "sample.pdf", "size_hint_bytes": 1, "_auth_context": AUTH, "_client_websocket": owner}, Promptless)
            assert init["type"] == "upload_blob.init.ok"
            for index in range(26):
                body = (b"%PDF" + b"x" * (CHUNK_MAX_BYTES - 4)) if index == 0 else b"x" * CHUNK_MAX_BYTES
                result = await store._on_chunk({"request_id": "synthetic-large", "data_b64": base64.b64encode(body).decode(), "final": index == 25, "_auth_context": AUTH, "_client_websocket": owner}, Promptless)
            assert result.get("error_code") == "upload_blob_too_large"
            assert TOTAL_MAX_BYTES == 64 * 1024 * 1024
            assert not store._uploads
            assert not list((tmp_path / "blobs" / ".tmp").iterdir())
    asyncio.run(run())


import hashlib
import json
from contextlib import asynccontextmanager
import pytest


@asynccontextmanager
async def fixture(tmp_path):
    persistent = Store(str(tmp_path / "fixture.db")); persistent.start()
    blobs = BlobStore(str(tmp_path / "blobs"), attachment_store=persistent)
    await blobs.start()
    try:
        yield blobs, persistent
    finally:
        for owner in list(blobs._by_conn):
            await blobs.abort_connection(owner)
        persistent.stop()


async def upload(blobs, body=b"%PDF synthetic", *, rid="synthetic-upload", filename="sample.pdf", auth=None, owner=None, purpose="chat_attachment", **extra):
    auth = dict(AUTH if auth is None else auth)
    owner = object() if owner is None else owner
    init = await blobs._on_init({"request_id": rid, "purpose": purpose, "filename": filename,
                                "_auth_context": auth, "_client_websocket": owner, **extra}, Promptless)
    if init["type"] != "upload_blob.init.ok":
        return init
    for offset in range(0, max(len(body), 1), CHUNK_MAX_BYTES):
        result = await blobs._on_chunk({"request_id": rid, "data_b64": base64.b64encode(body[offset:offset + CHUNK_MAX_BYTES]).decode(),
                                       "final": offset + CHUNK_MAX_BYTES >= len(body), "_auth_context": auth,
                                       "_client_websocket": owner}, Promptless)
        if isinstance(result, dict):
            return result
    raise AssertionError("missing final receipt")


@pytest.mark.parametrize("auth,kind,principal,generation", [
    (AUTH, "seat", "test-host:producer", "synthetic-generation-1"),
    ({**AUTH, "operator_authenticated": True, "operator_principal": "operator:synthetic-operator"}, "seat", "test-host:producer", "synthetic-generation-1"),
    ({"scoped_principal": True, "credential_id": "synthetic-scoped", "scope_stream": "fixture:assistant"}, "scoped", "credential:synthetic-scoped", None),
    ({"operator_authenticated": True, "operator_principal": "operator:synthetic-operator"}, "operator", "operator:synthetic-operator", None),
])
def test_server_receipt_persists_verified_principal_kind(tmp_path, auth, kind, principal, generation):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            result = await upload(blobs, auth={**auth, "stream_token": "synthetic-secret-not-to-persist"}, content_type="image/png")
            assert result["type"] == "upload_blob.ok", result
            assert result["blob_sha"] == hashlib.sha256(b"%PDF synthetic").hexdigest()
            assert result["media_type"] == "application/pdf"  # caller content-type ignored
            assert result["bytes"] == len(b"%PDF synthetic")
            assert result["uploader"] == principal and result["auth_kind"] == kind
            assert result["generation"] == generation
            row = await persistent.attachment_upload(result["upload_id"])
            assert row["state"] == "ready" and row["purpose"] == "chat_attachment"
            assert row["principal_id"] == principal and row["seat_generation"] == generation
            if kind != "seat": assert row["seat_stream_id"] is None
            if kind == "scoped": assert row["assistant_scope"] == "fixture:assistant" and row["principal_id"] != row["assistant_scope"]
            assert "synthetic-secret-not-to-persist" not in json.dumps(row)
            assert await blobs.read_verified(result["blob_sha"]) == b"%PDF synthetic"
    asyncio.run(run())


@pytest.mark.parametrize("changes", [{"stream_id": ""}, {"stream_id": "bad"}, {"session_generation": None}, {"session_generation": ""}, {"session_generation": True}])
def test_incomplete_seat_identity_never_downgrades_to_operator(tmp_path, changes):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            result = await upload(blobs, auth={**AUTH, "operator_authenticated": True, "operator_principal": "operator:synthetic", **changes})
            assert result["error_code"] == "upload_identity_invalid"
            assert not blobs._uploads
    asyncio.run(run())


@pytest.mark.parametrize("auth", [None, {}, {"operator_authenticated": True, "operator_principal": "operator"}, {"scoped_principal": True, "credential_id": "", "scope_stream": "fixture:assistant"}, {"scoped_principal": True, "credential_id": "synthetic"}])
def test_unverified_or_incomplete_credential_identity_refused(tmp_path, auth):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            result = await upload(blobs, auth=auth or {})
            assert result["type"] == "upload_blob.error"
            assert not blobs._uploads
    asyncio.run(run())


def test_generic_and_report_uploads_between_25_and_64_mib_unchanged(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            body = b"x" * (26 * 1024 * 1024)
            for purpose in [None, "report"]:
                result = await upload(blobs, body, rid="synthetic-" + str(purpose), purpose=purpose)
                assert result["type"] == "upload_blob.ok" and result["size_bytes"] == len(body)
                assert set(result) == {"type", "request_id", "blob_sha", "size_bytes"}
            count = await persistent.submit(lambda conn: conn.execute("SELECT COUNT(*) FROM v2_attachment_uploads").fetchone()[0])
            assert count == 0 and TOTAL_MAX_BYTES == 64 * 1024 * 1024
    asyncio.run(run())


def test_upload_replay_resolves_same_server_id_and_conflict_is_refused(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            first = await upload(blobs)
            again = await upload(blobs)
            assert first == again
            conflict = await upload(blobs, b"%PDF changed")
            assert conflict["error_code"] == "upload_request_conflict"
            assert await blobs.read_verified(first["blob_sha"]) == b"%PDF synthetic"
    asyncio.run(run())


@pytest.mark.parametrize("filename,body,mime", [("a.png", b"\x89PNG synthetic", "image/png"), ("a.jpeg", b"\xff\xd8\xffx", "image/jpeg"), ("a.pdf", b"%PDF fixture", "application/pdf"), ("a.zip", b"PK\x03\x04fixture", "application/zip"), ("a.3mf", b"PK\x03\x04fixture", "model/3mf"), ("a.stl", b"solid fixture", "model/stl"), ("a.stp", b"ISO-10303 fixture", "model/step"), ("a.scad", b"cube(1);", "application/x-openscad")])
def test_canonical_media_types(tmp_path, filename, body, mime):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            result = await upload(blobs, body, filename=filename)
            assert result["type"] == "upload_blob.ok", result
            assert result["media_type"] == mime
    asyncio.run(run())


@pytest.mark.parametrize("filename,body,code", [("a.exe", b"%PDF", "unsupported_type"), ("a.png", b"%PDF", "type_mismatch"), ("a.pdf", b"", "type_mismatch")])
def test_unsupported_or_mismatched_type_never_materializes(tmp_path, filename, body, code):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            result = await upload(blobs, body, filename=filename)
            assert result["error_code"] == code
            assert not list(blobs._tmp.iterdir())
            assert await persistent.submit(lambda conn: conn.execute("SELECT COUNT(*) FROM v2_attachment_uploads").fetchone()[0]) == 0
    asyncio.run(run())


@pytest.mark.parametrize("after_materialize", [False, True])
def test_pending_row_precedes_bytes_and_is_not_referenceable(tmp_path, monkeypatch, after_materialize):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            original = blobs._materialize_managed
            seen = {}
            def crash(up, sha):
                import sqlite3
                with sqlite3.connect(tmp_path / "fixture.db") as external:
                    row = external.execute("SELECT upload_id,state FROM v2_attachment_uploads WHERE blob_sha=?", (sha,)).fetchone()
                assert row and row[1] == "pending"
                assert not blobs._path_for(sha).exists()
                seen.update(upload_id=row[0], sha=sha)
                if after_materialize: original(up, sha)
                raise OSError("synthetic crash point")
            monkeypatch.setattr(blobs, "_materialize_managed", crash)
            result = await upload(blobs)
            assert result["error_code"] == "upload_provenance_failed"
            assert await persistent.attachment_upload(seen["upload_id"]) is None
            row = await persistent.attachment_upload(seen["upload_id"], ready_only=False)
            assert row["state"] == "pending"
            assert blobs._path_for(seen["sha"]).exists() is after_materialize
            for sha in [seen["sha"], seen["sha"].upper()]:
                fetched = await blobs._on_fetch({"request_id": "fetch-pending", "blob_sha": sha})
                assert isinstance(fetched, dict) and fetched.get("error_code") == "blob_unknown"
    asyncio.run(run())


@pytest.mark.parametrize("field", ["uploader", "generation", "seat_stream_id", "uploaded_at", "origin", "credential_id"])
def test_caller_asserted_provenance_refused(tmp_path, field):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            result = await upload(blobs, **{field: "forged-synthetic"})
            assert result["error_code"] == "upload_identity_not_allowed"
            assert not blobs._uploads
    asyncio.run(run())


def test_wrong_connection_cannot_complete_or_reset_managed_upload(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            owner, stranger = object(), object()
            init = {"request_id": "owned", "purpose": "chat_attachment", "filename": "a.pdf", "_auth_context": AUTH, "_client_websocket": owner}
            assert (await blobs._on_init(init, Promptless))["type"] == "upload_blob.init.ok"
            chunk = {"request_id": "owned", "data_b64": base64.b64encode(b"%PDF owned").decode(), "final": True, "_auth_context": AUTH, "_client_websocket": stranger}
            assert (await blobs._on_chunk(chunk, Promptless))["error_code"] == "upload_blob_forbidden"
            assert (await blobs._on_init({**init, "_client_websocket": stranger}, Promptless))["error_code"] == "upload_blob_forbidden"
            assert (await blobs._on_init({"request_id": "owned", "_client_websocket": stranger}, Promptless))["error_code"] == "upload_blob_forbidden"
            assert blobs._uploads["owned"].size == 0
            result = await blobs._on_chunk({**chunk, "_client_websocket": owner}, Promptless)
            assert result["uploader"] == AUTH["stream_id"]
    asyncio.run(run())


def test_generation_change_cannot_complete_prior_upload(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            owner = object()
            init = {"request_id": "generation-bound", "purpose": "chat_attachment", "filename": "a.pdf", "_auth_context": AUTH, "_client_websocket": owner}
            await blobs._on_init(init, Promptless)
            result = await blobs._on_chunk({**init, "data_b64": base64.b64encode(b"%PDF").decode(), "final": True, "_auth_context": {**AUTH, "session_generation": "synthetic-new-generation"}}, Promptless)
            assert result["error_code"] == "upload_blob_forbidden"
            assert blobs._uploads["generation-bound"].size == 0
    asyncio.run(run())


def test_metadata_name_sanitized_and_time_server_issued(tmp_path, monkeypatch):
    import store_attachments
    monkeypatch.setattr(store_attachments, "now_iso", lambda: "2040-03-04T05:06:07+00:00")
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            result = await upload(blobs, filename="../../nested\\report.PDF", auth={**AUTH, "uploaded_at": "1900-01-01"})
            assert result["filename"] == "report.PDF"
            assert result["uploaded_at"] == "2040-03-04T05:06:07+00:00"
            assert result["media_type"] == "application/pdf"
    asyncio.run(run())


def test_provenance_survives_store_restart(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            first = await upload(blobs)
        again = Store(str(tmp_path / "fixture.db")); again.start()
        try:
            row = await again.attachment_upload(first["upload_id"])
            assert row["blob_sha"] == first["blob_sha"] and row["state"] == "ready"
        finally:
            again.stop()
    asyncio.run(run())


def test_existing_legacy_blob_is_marked_protected(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            legacy = await upload(blobs, rid="legacy", purpose=None)
            managed = await upload(blobs, rid="managed")
            assert legacy["blob_sha"] == managed["blob_sha"]
            row = await persistent.attachment_upload(managed["upload_id"])
            assert row["legacy_protected"] == 1
    asyncio.run(run())


def test_wire_client_cannot_inject_internal_auth_context(tmp_path):
    from server import Server
    from sessions import Sessions
    class Peer:
        remote_address = ("127.0.0.1", 5555)
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            server = Server(store=persistent, sessions=Sessions(persistent, tmux=None, local_host="test-host"), local_host="test-host")
            server.handlers.update(blobs.wire_handlers())
            forged = {"type": "upload_blob_init", "request_id": "forged", "purpose": "chat_attachment", "filename": "a.pdf", "_auth_context": AUTH, "_client_websocket": "forged-owner"}
            result = await server._dispatch(json.dumps(forged), websocket=Peer())
            assert result[0]["error_code"] == "authentication_required"
            assert not blobs._uploads
    asyncio.run(run())


def test_scoped_wire_upload_records_credential_and_cannot_publish(tmp_path):
    from test_scoped_credential import _registry_with_scoped, _scoped_server
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            registry, cid = await _registry_with_scoped(tmp_path)
            server, peer = _scoped_server(persistent, registry, cid)
            server.handlers.update(blobs.wire_handlers()); server.blobs = blobs
            init = {"type": "upload_blob_init", "request_id": "scoped-wire", "purpose": "chat_attachment", "filename": "a.pdf"}
            assert (await server._dispatch(json.dumps(init), websocket=peer))[0]["type"] == "upload_blob.init.ok"
            final = {"type": "upload_blob_chunk", "request_id": "scoped-wire", "data_b64": base64.b64encode(b"%PDF scoped").decode(), "final": True}
            result = (await server._dispatch(json.dumps(final), websocket=peer))[0]
            assert result["credential_id"] == cid and result["seat_stream_id"] is None
            assert result["uploader"] == "credential:" + cid and result["assistant_scope"] == "daff:assistant"
            assert await persistent.scoped_owner(kind="blob", key=result["blob_sha"]) == cid
            denied = await server._dispatch(json.dumps({"type": "assistant.publish", "request_id": "cannot-publish", "attachment_ids": [result["upload_id"]], "composite_stream_id": "daff:assistant"}), websocket=peer)
            assert denied[0]["error_code"] == "scope_denied"
    asyncio.run(run())


def test_pending_materialization_cancellation_is_joined_before_cleanup(tmp_path, monkeypatch):
    import threading
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            owner = object(); started = threading.Event(); release = threading.Event()
            original = blobs._materialize_managed
            def pause(up, sha):
                started.set()
                if not release.wait(5): raise AssertionError("synthetic materialize release timed out")
                original(up, sha)
            monkeypatch.setattr(blobs, "_materialize_managed", pause)
            await blobs._on_init({"request_id": "cancel-final", "purpose": "chat_attachment", "filename": "a.pdf", "_auth_context": AUTH, "_client_websocket": owner}, Promptless)
            task = asyncio.create_task(blobs._on_chunk({"request_id": "cancel-final", "data_b64": base64.b64encode(b"%PDF cancellation").decode(), "final": True, "_auth_context": AUTH, "_client_websocket": owner}, Promptless))
            try:
                assert await asyncio.to_thread(started.wait, 3)
                task.cancel()
                cleanup = asyncio.create_task(blobs.abort_connection(owner))
                await asyncio.sleep(0)
                assert not cleanup.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError): await task
            await cleanup
            assert not blobs._uploads and not list(blobs._tmp.iterdir())
            rows = await persistent.submit(lambda conn: [dict(r) for r in conn.execute("SELECT * FROM v2_attachment_uploads")])
            assert len(rows) == 1 and rows[0]["state"] == "ready"
            assert await blobs.read_verified(rows[0]["blob_sha"]) == b"%PDF cancellation"
    asyncio.run(run())


def test_new_managed_blob_hash_is_not_other_credential_read_authority(tmp_path):
    from test_scoped_credential import _registry_with_scoped, _scoped_server, Peer
    from _shared import operator_auth
    async def run():
        async with fixture(tmp_path) as (blobs, persistent):
            registry, cid = await _registry_with_scoped(tmp_path)
            server, peer = _scoped_server(persistent, registry, cid)
            server.handlers.update(blobs.wire_handlers()); server.blobs = blobs
            await server._dispatch(json.dumps({"type": "upload_blob_init", "request_id": "owned-upload", "purpose": "chat_attachment", "filename": "a.pdf"}), websocket=peer)
            receipt = (await server._dispatch(json.dumps({"type": "upload_blob_chunk", "request_id": "owned-upload", "data_b64": base64.b64encode(b"%PDF owned").decode(), "final": True}), websocket=peer))[0]
            other_id, _ = registry.issue("pentacle-mobile", label="synthetic-other", scope={"stream": "other:assistant"})
            other = Peer(); server._connection_trust[other] = operator_auth.ConnectionTrust(transport="v2", credential_id=other_id, client_kind="pentacle-mobile", operator_trusted=True, scope={"stream": "other:assistant"})
            request = json.dumps({"type": "fetch_blob", "request_id": "fetch-owned", "blob_sha": receipt["blob_sha"]})
            denied = await server._dispatch(request, websocket=other)
            assert denied[0]["error_code"] == "blob_forbidden"
            allowed = await server._dispatch(request, websocket=peer)
            frames = [frame async for frame in allowed]
            assert base64.b64decode(frames[0]["content_b64"]) == b"%PDF owned"
    asyncio.run(run())
