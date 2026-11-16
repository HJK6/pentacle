"""Scoped single-stream credential (C): deny-by-default wire verbs, one-stream
filtering, blob/request ownership, and live revocation."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from _shared import operator_auth
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import SCOPE_DENIED_CODE, Server
from sessions import Sessions
from store import Store


LOCAL = "fixture-host"
DAFF_CHAT = "daff:assistant"
DAFF_SEAT = "fixture-daffs:visible"
OTHER = "fixture-other:lead"


class Peer:
    def __init__(self, address="198.51.100.7"):
        self.remote_address = (address, 5111)


async def _registry_with_scoped(tmp_path):
    reg = operator_auth.OperatorCredentialRegistry(tmp_path / "creds.json")
    cid, _code = reg.issue("pentacle-mobile", label="cosmo", scope={"stream": DAFF_CHAT})
    return reg, cid


def _scoped_server(store, reg, cid):
    sessions = Sessions(store, tmux=None, local_host=LOCAL)
    server = Server(store=store, sessions=sessions, local_host=LOCAL)
    server.operator_credential_registry = reg
    peer = Peer()
    server._connection_trust[peer] = operator_auth.ConnectionTrust(
        transport="v2", credential_id=cid, client_kind="pentacle-mobile",
        operator_trusted=True, scope={"stream": DAFF_CHAT},
    )
    return server, peer


def _frame(verb, **extra):
    return json.dumps({"type": verb, "request_id": f"{verb}-req", **extra})


def test_scope_matrix_allowlist_denies_everything_else():
    async def run():
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            store = Store(":memory:")
            store.start()
            try:
                reg, cid = await _registry_with_scoped(tmp)
                server, peer = _scoped_server(store, reg, cid)
                # Allowed handshake/keepalive verb reaches its handler.
                ok = await server._dispatch(_frame("ping"), websocket=peer)
                assert ok[0]["type"] == "pong"
                # Every non-allowlisted verb is denied before its handler.
                for verb in ("spawn", "close", "tell", "list_sessions", "inspect_stream",
                             "assistant.rebind"):
                    reply = await server._dispatch(_frame(verb, to_stream_id=OTHER), websocket=peer)
                    assert reply[0]["error_code"] == SCOPE_DENIED_CODE, verb
            finally:
                store.stop()
    asyncio.run(run())


def test_send_restricted_to_scope_stream():
    async def run():
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            store = Store(":memory:")
            store.start()
            try:
                reg, cid = await _registry_with_scoped(tmp)
                # daff composite bound to a live seat so an in-scope send is accepted.
                seat = await store.open_session(
                    "fixture-daffs", "visible", provider="codex", role="assistant",
                    visibility="default", pane_status="pane_alive",
                    effective_model="gpt-6-sol", effective_effort="high")
                daff = AssistantComposite(store, config=AssistantCompositeConfig.from_env({
                    "PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED": "1",
                    "PENTACLE_ASSISTANT_DAFF_COMPOSITE_STREAM_ID": DAFF_CHAT,
                    "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_STREAM_ID": DAFF_SEAT,
                    "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_GENERATION": seat["session_generation"],
                }, name="daff", env_prefix="DAFF_"))
                await daff.load_binding()
                await daff.ensure_projection()
                server, peer = _scoped_server(store, reg, cid)
                server.assistant_composites = {"daff": daff}
                server.assistant_composite = daff
                await server.sessions.refresh()

                # In-scope send -> accepted into the composite; request id recorded.
                ok = await server._dispatch(_frame(
                    "send", to_stream_id=DAFF_CHAT, text="hi", request_id="s1",
                    optimistic_id="in1"), websocket=peer)
                assert ok[0]["type"] == "send.result"
                assert await store.scoped_owner(kind="request", key="s1") == cid

                # Out-of-scope send -> scope_denied.
                denied = await server._dispatch(_frame(
                    "send", to_stream_id=OTHER, text="x", request_id="s2",
                    optimistic_id="in2"), websocket=peer)
                assert denied[0]["error_code"] == SCOPE_DENIED_CODE
            finally:
                store.stop()
    asyncio.run(run())


def test_request_stream_events_and_receipt_scoped():
    async def run():
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            store = Store(":memory:")
            store.start()
            try:
                reg, cid = await _registry_with_scoped(tmp)
                server, peer = _scoped_server(store, reg, cid)
                # request_stream_events to another stream -> scope_denied.
                denied = await server._dispatch(_frame(
                    "request_stream_events", to_stream_id=OTHER), websocket=peer)
                assert denied[0]["error_code"] == SCOPE_DENIED_CODE
                # send.receipt.get for another credential's request id -> scope_denied.
                await store.record_scoped_owner(kind="request", key="other-req", credential_id="someone-else")
                denied2 = await server._dispatch(_frame(
                    "send.receipt.get", to_stream_id=DAFF_CHAT, request_id="other-req"), websocket=peer)
                assert denied2[0]["error_code"] == SCOPE_DENIED_CODE
                # Reading another stream's receipt -> scope_denied.
                denied3 = await server._dispatch(_frame(
                    "send.receipt.get", to_stream_id=OTHER, request_id="x"), websocket=peer)
                assert denied3[0]["error_code"] == SCOPE_DENIED_CODE
            finally:
                store.stop()
    asyncio.run(run())


def test_fetch_blob_ownership_enforced_at_gate():
    async def run():
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            store = Store(":memory:")
            store.start()
            try:
                reg, cid = await _registry_with_scoped(tmp)
                server, peer = _scoped_server(store, reg, cid)
                async def _fetch_ok(m):
                    return {"type": "fetch_blob.ok", "blob_sha": m.get("blob_sha")}
                server.handlers["fetch_blob"] = _fetch_ok  # production merges this from blobs
                sha_other = "a" * 64
                sha_own = "b" * 64
                await store.record_scoped_owner(kind="blob", key=sha_other, credential_id="someone-else")
                await store.record_scoped_owner(kind="blob", key=sha_own, credential_id=cid)
                # Another credential's blob -> blob_forbidden before any handler.
                denied = await server._dispatch(_frame("fetch_blob", blob_sha=sha_other), websocket=peer)
                assert denied[0]["error_code"] == "blob_forbidden"
                # Own blob passes the ownership gate and reaches the handler.
                owned = await server._dispatch(_frame("fetch_blob", blob_sha=sha_own), websocket=peer)
                assert owned[0]["type"] == "fetch_blob.ok"
            finally:
                store.stop()
    asyncio.run(run())


def test_transcribe_blob_owner_only():
    async def run():
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            store = Store(":memory:")
            store.start()
            try:
                reg, cid = await _registry_with_scoped(tmp)
                server, peer = _scoped_server(store, reg, cid)
                sha_other = "c" * 64
                await store.record_scoped_owner(kind="blob", key=sha_other, credential_id="someone-else")
                denied = await server._dispatch(_frame("transcribe_blob", blob_sha=sha_other), websocket=peer)
                assert denied[0]["error_code"] == "blob_forbidden"
            finally:
                store.stop()
    asyncio.run(run())


def test_live_revocation_drops_next_rpc():
    async def run():
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            store = Store(":memory:")
            store.start()
            try:
                reg, cid = await _registry_with_scoped(tmp)
                server, peer = _scoped_server(store, reg, cid)
                assert (await server._dispatch(_frame("ping"), websocket=peer))[0]["type"] == "pong"
                reg.revoke(cid)
                # Next RPC (even the allowlisted ping) is refused within one heartbeat.
                refused = await server._dispatch(_frame("ping"), websocket=peer)
                assert refused[0]["error_code"] == "authentication_required"
            finally:
                store.stop()
    asyncio.run(run())


def test_scoped_hello_discloses_no_fleet():
    """A scoped (Cosmo) credential authenticates but is NOT an operator: its
    hello returns an empty, fleet-free snapshot (no sessions/hosts/working_states,
    no hosts.stats), never the full operator inventory."""
    async def run():
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            store = Store(":memory:")
            store.start()
            try:
                reg, cid = await _registry_with_scoped(tmp)
                # A populated fleet that must not leak through a scoped hello.
                await store.open_session(
                    "fixture-other", "lead", provider="claude", role="lead",
                    visibility="default", pane_status="pane_alive",
                    effective_model="claude-opus-5-5", effective_effort="high")
                server, peer = _scoped_server(store, reg, cid)
                await server.sessions.refresh()
                frames = await server._dispatch(_frame("hello"), websocket=peer)
                assert frames[0]["type"] == "hello"  # admitted, not authentication_required
                snap = next(f for f in frames if f.get("type") == "snapshot")
                assert snap["sessions"] == []
                assert snap["hosts"] == {}
                assert snap["working_states"] == {}
                # The host-telemetry frame bypasses _frame_for_client, so it must
                # never be appended for a scoped connection.
                assert all(f.get("type") != "hosts.stats" for f in frames)
                # The scoped credential is not elevated to an operator.
                assert not server._operator_authenticated(peer)
            finally:
                store.stop()
    asyncio.run(run())


def test_rotation_preserves_scope(tmp_path):
    """A routine credential rotation must NOT widen a scoped (Cosmo) credential
    into a full unscoped operator credential."""
    import operator_auth_cli
    reg = operator_auth.OperatorCredentialRegistry(tmp_path / "creds.json")
    reg.initialize()
    cid, _ = reg.issue("pentacle-mobile", label="cosmo", scope={"stream": DAFF_CHAT})
    operator_auth_cli._rotate(reg, credential_id=cid, label="rotated")
    creds = reg.load().credentials
    assert creds[cid]["revoked_at"] is not None  # prior revoked
    active = [c for c in creds.values() if c.get("revoked_at") is None]
    assert len(active) == 1
    assert active[0]["scope"] == {"stream": DAFF_CHAT}  # scope carried over


def test_scope_is_server_authoritative_and_legacy_is_unscoped(tmp_path):
    reg = operator_auth.OperatorCredentialRegistry(tmp_path / "creds.json")
    cid, _ = reg.issue("pentacle-mobile", label="x", scope={"stream": DAFF_CHAT})
    # The stored scope round-trips through verify (scope is never taken from the wire).
    record = reg.load().credentials[cid]
    assert record["scope"] == {"stream": DAFF_CHAT}
    # A legacy record without a scope key loads as unscoped (full rights).
    raw = json.loads((tmp_path / "creds.json").read_text())
    legacy_id = "11111111-1111-1111-1111-111111111111"
    raw["credentials"][legacy_id] = {
        "client_kind": "pentacle-mobile", "proof_key": record["proof_key"],
        "label": "legacy", "created_at": record["created_at"], "revoked_at": None,
        "replaces_credential_id": None,
    }
    (tmp_path / "creds.json").write_text(json.dumps(raw))
    loaded = reg.load().credentials
    assert loaded[legacy_id]["scope"] is None
    assert loaded[cid]["scope"] == {"stream": DAFF_CHAT}
