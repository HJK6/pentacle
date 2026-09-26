"""The canonical assistant's direct binding uses the installed mobile send contract."""

import asyncio
import json

from _shared import operator_auth
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import Store


ASSISTANT = "fixture-chat:assistant"
ROOT = "fixture-root:visible"


def _config(generation):
    return AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": ASSISTANT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": generation,
    })


def test_direct_binding_requires_exact_generation_without_router():
    assert _config("generation-1").direct_primary
    for env in (
        {"PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1", "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": ASSISTANT},
        {"PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1", "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": ASSISTANT,
         "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT},
    ):
        try:
            AssistantCompositeConfig.from_env(env)
        except ValueError:
            pass
        else:
            raise AssertionError("incomplete direct/router binding started")


def test_mobile_shape_admission_receipts_and_exact_root_publication():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex")
            generation = root["session_generation"]
            sent = []

            async def dispatch(route):
                sent.append(route)
                return {"delivery": "landed"}

            composite = AssistantComposite(store, config=_config(generation), dispatch=dispatch)
            assert not composite.is_backend_stream(ROOT)
            await composite.ensure_projection()
            sessions = Sessions(store, tmux=None, local_host="fixture-chat")
            await sessions.refresh()
            server = Server(store=store, sessions=sessions, local_host="fixture-chat")
            server.assistant_composite = composite
            mobile = {
                "host": "fixture-chat", "session_name": "assistant", "text": "Original question",
                "msg_id": "logical-1", "request_id": "transport-1",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            }
            first = await server._on_send(mobile)
            assert first["to_stream_id"] == ASSISTANT
            assert first["state"] == first["delivery"] == "landed"
            assert first["submission_confirmed"] is True
            assert first["receipt_id"]
            receipt = await store.get_send_receipt(ASSISTANT, "transport-1")
            assert receipt["state"] == receipt["delivery"] == "landed"
            assert receipt["optimistic_id"] == "logical-1"
            retry = await server._on_send({**mobile, "request_id": "transport-2"})
            assert retry["assistant_composite"]["duplicate"] is True
            assert (await store.get_send_receipt(ASSISTANT, "transport-2"))["state"] == "landed"
            try:
                await server._on_send({**mobile, "request_id": "transport-3", "text": "Changed"})
            except Exception as exc:
                assert "assistant_input_idempotency_conflict" in str(exc)
            else:
                raise AssertionError("conflicting logical input was admitted")
            for _ in range(100):
                route = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="logical-1")
                if route and route["routing_state"] == "resolved" and sent:
                    break
                await asyncio.sleep(0.01)
            assert len(sent) == 1 and sent[0]["route_target"] == ROOT
            assert route["route_target_generation"] == generation
            persisted = json.loads(route["route_json"])["direct_envelope"]
            assert persisted["origin"] == ASSISTANT
            assert persisted["dispatch_id"] == route["dispatch_id"]
            assert persisted["target_stream_id"] == ROOT
            assert persisted["target_generation"] == generation
            assert persisted["reply_to_message_id"] == "logical-1"
            assert persisted["original_input"] == {"text": "Original question", "attachments": []}
            assert f"--request-id publish:{route['dispatch_id']}" in persisted["publish_command"]
            assert persisted["publish_command"] in persisted["wire_body"]
            events = await store.fetch_session_event_tail(ASSISTANT, limit=20)
            assert len([item for item in events if item["kind"] == "USER"]) == 1

            published = {
                "request_id": "publish:" + route["dispatch_id"],
                "composite_stream_id": ASSISTANT, "dispatch_id": route["dispatch_id"],
                "reply_to_message_id": "logical-1", "reply_to_question_id": None,
                "publish_kind": "prose", "response_state": "final",
                "message": "The direct answer", "attachment_ids": [], "evidence_refs": [],
            }
            first_answer = await composite.publish(published, actor_stream_id=ROOT)
            replay = await composite.publish(published, actor_stream_id=ROOT)
            assert not first_answer["duplicate"] and replay["duplicate"]
            authority = await store.open_session("fixture-authority", "old-astra", provider="codex")
            assert authority["session_generation"]
            try:
                await composite.publish(published, actor_stream_id="fixture-authority:old-astra")
            except ValueError as exc:
                assert str(exc) == "assistant_publish_provenance_unverified"
            else:
                raise AssertionError("legacy authority published a direct reply")
            for changed in (
                {**published, "message": "different"},
                {**published, "request_id": "alternate-final"},
                {**published, "reply_to_message_id": "someone-else"},
            ):
                try:
                    await composite.publish(changed, actor_stream_id=ROOT)
                except ValueError:
                    pass
                else:
                    raise AssertionError("incorrect or duplicate canonical answer published")
            events = await store.fetch_session_event_tail(ASSISTANT, limit=20)
            assert [item["kind"] for item in events].count("ASSIST_TEXT") == 1
            assert events[-1]["reply_to_message_id"] == "logical-1"
            assert events[-1]["text"] == "The direct answer"
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_known_direct_delivery_failure_keeps_reason_in_reconnect_activity():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex")

            async def dispatch(_route):
                return {"delivery": "not_landed", "reason": "assistant_direct_generation_conflict"}

            composite = AssistantComposite(store, config=_config(root["session_generation"]), dispatch=dispatch)
            await composite.ensure_projection()
            await composite.accept_input({"message": "Will fail visibly", "msg_id": "failed-input",
                                          "request_id": "failed-transport"}, operator_principal="operator:fixture")
            for _ in range(100):
                route = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="failed-input")
                if route and route["delivery_state"] == "failed":
                    break
                await asyncio.sleep(0.01)
            assert route["delivery_state"] == "failed"
            assert route["error_code"] == "assistant_direct_generation_conflict"
            await composite.refresh_activity()
            activity = composite.activity_snapshot()
            assert activity["inputs"]["failed-input"]["response_state"] == "failed"
            assert activity["inputs"]["failed-input"]["error_code"] == "assistant_direct_generation_conflict"
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_stale_direct_target_refuses_new_input_before_canonical_admission():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session("fixture-root", "visible", provider="codex")
            composite = AssistantComposite(store, config=_config("retired-generation"))
            await composite.ensure_projection()
            try:
                await composite.accept_input({
                    "message": "Do not misroute", "msg_id": "stale-input", "request_id": "stale-rpc",
                }, operator_principal="operator:fixture")
            except ValueError as exc:
                assert str(exc) == "assistant_direct_generation_conflict"
            else:
                raise AssertionError("stale target admitted canonical input")
            assert not await store.fetch_session_event_tail(ASSISTANT, limit=10)
            assert await store.get_send_receipt(ASSISTANT, "stale-rpc") is None
        finally:
            store.stop()

    asyncio.run(_go())


def test_installed_mobile_capability_is_bound_to_verified_credential_kind(tmp_path):
    async def _go():
        directory = tmp_path / "operator-auth"
        directory.mkdir(mode=0o700)
        registry = operator_auth.OperatorCredentialRegistry(directory / "credentials.json")
        credential_id, envelope = registry.issue("pentacle-mobile")
        proof_key = operator_auth.decode_envelope(envelope)["proof_key"]
        server = Server()
        server.operator_credential_registry = registry

        async def hello(client, *, valid=True):
            socket = object()
            nonce, expires_at = operator_auth.new_nonce()
            server._operator_challenges[socket] = (nonce, expires_at)
            message = {
                "type": "hello", "client": client,
                "capabilities": {"assistant_composite_v1": True},
                "_client_websocket": socket,
            }
            if valid:
                message["auth_v2"] = {
                    "scheme": operator_auth.AUTH_SCHEME,
                    "credential_id": credential_id,
                    "proof": operator_auth.make_proof(proof_key, nonce, credential_id, "pentacle-mobile"),
                }
            result = await server._on_hello(message)
            return socket, result

        mobile, accepted = await hello("pentacle-mobile")
        assert accepted[0]["type"] == "hello"
        assert server._operator_authenticated(mobile)
        assert server._connection_trust[mobile].client_kind == "pentacle-mobile"
        assert server._client_assistant_composite_v1[mobile]
        mismatched, denied = await hello("pentacle")
        assert denied == [{"type": "hello.error", "error_code": "operator_auth_invalid"}]
        assert not server._operator_authenticated(mismatched)
        absent, denied = await hello("pentacle-mobile", valid=False)
        assert denied == [{"type": "hello.error", "error_code": "authentication_required"}]
        assert not server._operator_authenticated(absent)

    asyncio.run(_go())
