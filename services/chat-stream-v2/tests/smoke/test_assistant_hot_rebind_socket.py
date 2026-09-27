"""An isolated WebSocket daemon keeps serving while its direct pin moves."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os

import websockets

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import STREAM_TOKEN_HASH_VERSION, Store


CHAT = "fixture-chat:assistant"
A = "fixture-a:assistant"
B = "fixture-b:assistant"


def test_socket_rebind_preserves_old_dispatch_and_daemon_pid(tmp_path):
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        server = None
        composite = None
        try:
            a = await store.open_session("fixture-a", "assistant", provider="codex",
                                         role="assistant", pane_status="pane_alive",
                                         effective_model="gpt-6-sol", effective_effort="high")
            b = await store.open_session("fixture-b", "assistant", provider="codex",
                                         role="assistant", pane_status="pane_alive",
                                         effective_model="gpt-6-sol", effective_effort="high")
            token = "fixture-a-token"
            await store.grant_stream_token("fixture-a", "assistant",
                                           hashlib.sha256(token.encode()).hexdigest(),
                                           STREAM_TOKEN_HASH_VERSION)
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": CHAT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": A,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": a["session_generation"],
            })
            dispatched = []

            async def dispatch(route):
                dispatched.append((route["input_identity"], route["route_target"]))
                return {"delivery": "landed"}

            composite = AssistantComposite(store, config=config, dispatch=dispatch)
            await composite.load_binding()
            await composite.ensure_projection()
            sessions = Sessions(store, local_host="fixture-chat")
            await sessions.refresh()
            server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions,
                            local_host="fixture-chat")
            server.assistant_composite = composite
            port = await server.bind()
            pid = os.getpid()

            async def resolved(input_id):
                for _ in range(100):
                    row = await store.get_assistant_composite_route(
                        stream_id=CHAT, input_identity=input_id)
                    if row and row["routing_state"] == "resolved":
                        return row
                    await asyncio.sleep(0.01)
                raise AssertionError("direct route did not resolve")

            async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
                assert json.loads(await socket.recv())["type"] == "welcome"
                await socket.send(json.dumps({"type": "hello", "client": "hot-rebind-smoke",
                                              "stream_token": token, "from_stream_id": A}))
                hello_frames = [json.loads(await socket.recv()) for _ in range(2)]
                assert any(frame["type"] == "snapshot" for frame in hello_frames)

                async def rpc(payload):
                    await socket.send(json.dumps(payload))
                    async with asyncio.timeout(3):
                        while True:
                            frame = json.loads(await socket.recv())
                            if frame.get("request_id") == payload["request_id"]:
                                return frame

                before = await rpc({"type": "assistant.binding", "request_id": "binding-before"})
                assert before["source"] == "env" and before["stream_id"] == A
                await server._on_send({
                    "host": "fixture-chat", "session_name": "assistant", "text": "old",
                    "request_id": "old-rpc", "optimistic_id": "old-input",
                    "_auth_context": {"operator_authenticated": True,
                                      "operator_principal": "operator:fixture"},
                })
                old = await resolved("old-input")
                assert old["route_target"] == A
                changed = await rpc({
                    "type": "assistant.rebind", "request_id": "move-a-b",
                    "expected_revision": 0, "target_stream_id": B,
                    "target_generation": b["session_generation"],
                })
                assert changed["type"] == "assistant.rebind.ok"
                assert changed["new_binding"]["stream_id"] == B
                after = await rpc({"type": "assistant.binding", "request_id": "binding-after"})
                assert after["source"] == "durable" and after["stream_id"] == B
                await server._on_send({
                    "host": "fixture-chat", "session_name": "assistant", "text": "new",
                    "request_id": "new-rpc", "optimistic_id": "new-input",
                    "_auth_context": {"operator_authenticated": True,
                                      "operator_principal": "operator:fixture"},
                })
                new = await resolved("new-input")
                assert new["route_target"] == B
                published = await rpc({
                    "type": "assistant.publish", "request_id": "publish:" + old["dispatch_id"],
                    "composite_stream_id": CHAT, "dispatch_id": old["dispatch_id"],
                    "reply_to_message_id": "old-input", "publish_kind": "prose",
                    "response_state": "final", "message": "old final",
                    "attachment_ids": [], "evidence_refs": [],
                })
                assert published["type"] == "assistant.publish.ok", published
                events = await store.fetch_session_event_tail(CHAT, limit=10)
                assert any(event["text"] == "old final" for event in events)
                assert ("old-input", A) in dispatched
                assert ("new-input", B) in dispatched
                assert os.getpid() == pid and server.port == port
        finally:
            if composite is not None:
                await composite.stop()
            if server is not None:
                await server.close()
            store.stop()

    asyncio.run(run())
