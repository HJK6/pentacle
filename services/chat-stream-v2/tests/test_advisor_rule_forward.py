"""Exercise advisor-rule provenance through authenticated disposable sockets."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib

from _shared import operator_auth
from assistant_lane_rulings import AssistantLaneRulings
from codex_rollout_norm import normalize_codex_rollout_record
from smoke.test_connection_logging_socket import _connection
from store import STREAM_TOKEN_HASH_VERSION
from test_assistant_composite_addressing import _Harness, COMPOSITE_CHAT, SEAT_A, SEAT_B, SENDER


WORDS = "Change the café heading.\r\nUse the draft label.  "
MESSAGE_ID = "fixture-operator-message-complete-id"
TELL_ID = "fixture-dispatch-complete-id"
ORIGIN = "registered_operator_ws_v1"


@asynccontextmanager
async def rule_fixture(tmp_path):
    harness = _Harness(tmp_path)
    try:
        await harness.start(front_desk=True)
        server = harness.server
        server.host = "127.0.0.1"
        server.port = 0
        server.operator_credential_registry = operator_auth.OperatorCredentialRegistry(tmp_path / "operators.json")
        harness.composite.env_config = replace(harness.composite.env_config, authority_stream_id=SEAT_B)
        server.lane_rulings = AssistantLaneRulings(server)
        paste = harness.tmux.paste

        async def record_paste(name, text):
            await paste(name, text)
            await record_native(harness, name, text)

        harness.tmux.paste = record_paste
        _, envelope = server.operator_credential_registry.issue("pentacle")
        await server.bind()
        yield harness, envelope
    finally:
        if harness.server is not None:
            await harness.server.close()
        harness.store.stop()


async def record_native(harness, name, text):
    host = SEAT_B.split(":")[0]
    event = normalize_codex_rollout_record({
        "type": "response_item", "timestamp": "2026-10-31T22:00:00Z",
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}]},
    }, host=host, session_name=name, session_id="synthetic-native")[0]
    await harness.store.append_session_event(f"{host}:{name}", event,
                                             identity=f"fixture-native-{len(harness.tmux.inputs)}", limit=100)


@asynccontextmanager
async def operator(harness, envelope):
    async with _connection(harness.server) as socket:
        ready = await socket.hello(envelope=envelope, subscribe={
            "snapshot": False, "mode": "rpc", "assistant_composite_v1": True,
        }, capabilities={"assistant_composite_v1": True})
        assert ready[0]["type"] == "ready"
        yield socket


@asynccontextmanager
async def seat(harness, stream):
    host, name = stream.split(":")
    token = "synthetic-seat-token-" + name
    await harness.store.grant_stream_token(host, name, hashlib.sha256(token.encode()).hexdigest(),
                                          STREAM_TOKEN_HASH_VERSION)
    async with _connection(harness.server) as socket:
        auth = {"from_stream_id": stream, "stream_token": token}
        assert (await socket.hello(client="agent-orch", subscribe={"snapshot": False, "mode": "rpc"},
                                   **auth))[0]["type"] == "ready"

        async def rpc(verb, **fields):
            return await socket.rpc(verb, **auth, **fields)

        yield rpc


async def input_row(harness, identity):
    return await harness.store.get_assistant_composite_route(stream_id=COMPOSITE_CHAT, input_identity=identity)


async def admit(socket, identity=MESSAGE_ID, body=WORDS, **extra):
    return await socket.rpc("send", to_stream_id=COMPOSITE_CHAT, text=body,
                            optimistic_id=identity, **extra)


async def forward(fd, identity=MESSAGE_ID, body=WORDS, tell_id=TELL_ID, target=SEAT_B):
    return await fd("tell", to_stream_id=target, text=body, advisor_rule_message_id=identity, tell_id=tell_id)


def test_only_direct_registered_operator_stamps_the_first_admission(tmp_path):
    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            credential_id = operator_auth.decode_envelope(envelope)["credential_id"]
            principal = "operator:" + credential_id
            await h.composite.accept_input({"text": WORDS, "msg_id": "server-only"}, operator_principal=principal)
            await h.server._on_send({"to_stream_id": COMPOSITE_CHAT, "text": WORDS,
                "msg_id": "finisher", "_auth_context": {
                    "operator_authenticated": True, "operator_principal": principal, "outbound_finisher": True,
                }})
            assert (await admit(socket, "server-only"))["assistant_composite"]["duplicate"]
            assert (await admit(socket, _operator_origin="forged"))["type"] == "send.result"
            assert (await admit(socket, "other"))["type"] == "send.result"
            for identity in ("server-only", "finisher"):
                row = await input_row(h, identity)
                assert row["actor_stream_id"] == principal
                assert row.get("operator_origin") is None
            stamped = await input_row(h, MESSAGE_ID)
            assert stamped["actor_stream_id"] == principal
            assert stamped.get("operator_origin") == ORIGIN
            assert (await input_row(h, "other")).get("operator_origin") == ORIGIN

    asyncio.run(run())


def test_authenticated_fd_requires_exact_original_and_records_dispatch(tmp_path):
    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            assert (await admit(socket))["type"] == "send.result"
            async with seat(h, SEAT_A) as fd:
                ordinary = await fd("tell", to_stream_id=SEAT_B, text="Fixture status.", tell_id="ordinary")
                assert ordinary["delivery_status"] == "delivered"
                assert (await h.store.get_tell_delivery("ordinary")).get("advisor_rule_intent") is None
                wrong = await forward(fd, body=WORDS.rstrip())
                assert wrong.get("error_code") == "advisor_rule_words_mismatch"
                assert await h.store.get_tell_delivery(TELL_ID) is None
                delivered = await forward(fd)
                assert delivered["delivery_status"] == "delivered" and delivered["submission_confirmed"] is True
                evidence = await h.store.get_tell_delivery(TELL_ID)
                assert evidence["advisor_rule_intent"]["operator_message_id"] == MESSAGE_ID
                assert evidence["advisor_rule_intent"]["fd_binding_revision"] == 0
    asyncio.run(run())
