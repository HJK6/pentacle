import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from server import Server, VerbError
from test_system_notification_producer import Peer

DISPATCH = "assistant-direct-" + "a" * 32
INPUT = "test-input-1"
FD = "test:frontdesk"
GEN = "test-generation"


def fixture_row():
    row = {"stream_id": "bart:assistant", "input_identity": INPUT,
            "routing_state": "resolved", "delivery_state": "landed",
            "route_target": FD, "route_target_generation": GEN,
            "actor_stream_id": "operator:12345678-1234-1234-1234-123456789abc",
            "body": "add Test Person, +12025550123, garden",
            "route_json": json.dumps({"admission_mode": "direct_primary"}), "created_at": "2026-11-20T00:00:00Z"}
    canonical = json.dumps({"body": row["body"], "attachments": [], "reply_to_message_id": None,
                            "reply_to_question_id": None}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    row["payload_digest"] = hashlib.sha256(canonical.encode()).hexdigest()
    return row


def test_proof_requires_current_fd_and_matching_operator_dispatch():
    async def run():
        row = fixture_row()
        async def find(dispatch):
            return row if dispatch == DISPATCH else None
        server = Server(store=SimpleNamespace(find_assistant_composite_route_by_dispatch=find))
        config = SimpleNamespace(direct_primary=True, direct_primary_stream_id=FD, direct_primary_generation=GEN)
        server._composite_for = lambda stream: SimpleNamespace(enabled=True, config=config) if stream == "bart:assistant" else None
        msg = {"dispatch_id": DISPATCH, "input_id": INPUT,
               "_auth_context": {"token_verified": True, "stream_id": FD, "session_generation": GEN}}
        result = await server._on_contact_dispatch_proof(msg)
        assert result["text_sha256"] == hashlib.sha256(row["body"].encode()).hexdigest()
        assert "body" not in result and "actor_stream_id" not in result
        for field, value in [("actor_stream_id", "desk:sms"), ("delivery_state", "uncertain"),
                             ("route_target_generation", "other"), ("route_json", '{"desk_dispatch":true}')]:
            old = row[field]; row[field] = value
            with pytest.raises(VerbError):
                await server._on_contact_dispatch_proof(msg)
            row[field] = old
        with pytest.raises(VerbError):
            await server._on_contact_dispatch_proof(msg | {"_auth_context": {"token_verified": False}})
    asyncio.run(run())


def test_proof_connection_refuses_other_verbs_and_does_not_emit_tokens(caplog):
    async def run():
        server = Server()
        peer = Peer("127.0.0.1")
        server._contact_proof_clients.add(peer)
        for verb in ("send", "report", "hello", "list_sessions", "unknown.verb"):
            frame = {"type": verb, "request_id": "test", "stream_token": "synthetic-contact-token"}
            result = (await server._dispatch(json.dumps(frame), websocket=peer))[0]
            assert result["error_code"] == "contact_proof_only"
            assert "synthetic-contact-token" not in json.dumps(result)
        assert server._frame_for_client(peer, "chat.event", {}) is None
        assert "synthetic-contact-token" not in caplog.text
    asyncio.run(run())


def test_proof_connection_uses_seat_token_once_without_logging(tmp_path, caplog):
    from store import Store, STREAM_TOKEN_HASH_VERSION
    async def run():
        store = Store(str(tmp_path / "state.db"))
        store.start()
        server = Server(store=store)
        peer = Peer("127.0.0.1")
        token = "synthetic-proof-seat-token"
        try:
            await store.open_session("test", "frontdesk")
            await store.grant_stream_token("test", "frontdesk", hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            seat = await store.fetch_session("test", "frontdesk")
            generation = seat["session_generation"]
            row = fixture_row() | {"route_target_generation": generation}
            async def find(dispatch):
                return row if dispatch == DISPATCH else None
            store.find_assistant_composite_route_by_dispatch = find
            config = SimpleNamespace(direct_primary=True, direct_primary_stream_id=FD, direct_primary_generation=generation)
            server._composite_for = lambda stream: SimpleNamespace(enabled=True, config=config) if stream == "bart:assistant" else None
            server._register_client(peer)
            hello = {"type": "hello", "request_id": "hello", "client": "fast-contact-proof-v1",
                     "from_stream_id": FD, "stream_token": token, "subscribe": {"snapshot": False, "mode": "rpc"}}
            ready = (await server._dispatch(json.dumps(hello), websocket=peer))[0]
            assert ready["type"] == "ready"
            request = {"type": "contact.dispatch.proof", "request_id": "proof", "dispatch_id": DISPATCH, "input_id": INPUT}
            result = (await server._dispatch(json.dumps(request), websocket=peer))[0]
            assert result["type"] == "contact.dispatch.proof.ok", result
            second = (await server._dispatch(json.dumps(request), websocket=peer))[0]
            assert second["error_code"] == "contact_proof_consumed"
            assert token not in json.dumps([ready, result, second]) and token not in caplog.text
        finally:
            server._unregister_client(peer)
            store.stop()
    asyncio.run(run())
