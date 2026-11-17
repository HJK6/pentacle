"""Real authenticated daemon socket; disposable stub advisor/provider sessions."""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import websockets

from advisor_sessions import KINDS_KEY, SHAPE_KEY
from assistant_guidelines import AssistantGuidelines
from front_desk_ingress_replay import harness, HOST, DESK
from store import STREAM_TOKEN_HASH_VERSION


@asynccontextmanager
async def client(url, stream, token):
    async with websockets.connect(url) as socket:
        assert json.loads(await socket.recv())["type"] == "welcome"
        await socket.send(json.dumps({"type": "hello", "client": "advisor-acceptance",
            "subscribe": {"mode": "rpc", "snapshot": False}, "from_stream_id": stream, "stream_token": token}))
        assert json.loads(await socket.recv())["type"] == "ready"
        async def rpc(payload):
            await socket.send(json.dumps(payload))
            async with asyncio.timeout(10):
                while True:
                    frame = json.loads(await socket.recv())
                    if frame.get("request_id") == payload["request_id"]:
                        return frame
        yield rpc


def test_spawn_ruling_and_advice_answer_over_socket(tmp_path, monkeypatch):
    async def run():
        async with harness(tmp_path) as h:
            server, store, sessions = h["server"], h["store"], h["sessions"]
            r = server.lane_rulings
            r._ensure_worker = lambda: None
            h["comms"].guidelines = AssistantGuidelines(store, h["composite"], r, h["comms"], env={})
            h["comms"].guidelines.load = lambda role: ("socket-guidelines", "Review the requested scope.")
            prior_spawn = server.spawnctl.spawn
            outcomes, prompts, tokens = {}, {}, {}
            async def spawn(msg, host):
                if not msg.get("_advisor_session"):
                    return await prior_spawn(msg, host)
                name = msg["session_name"]
                await sessions.open(host, name, provider="codex", pane_status="pane_alive", bootstrap_state="ready",
                    session_generation=msg["_session_generation"], visibility="hidden", parent_stream_id=DESK)
                stream, token = host + ":" + name, "fixture-token-" + name
                await store.grant_stream_token(host, name, hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
                tokens[stream], prompts[stream] = token, [msg["initial_prompt"]]
                outcomes[msg["request_id"]] = {"state": "delivered", "request_id": msg["request_id"], "stream_id": stream}
                return {"type": "spawn.ok", "stream_id": stream}
            async def status(msg, host):
                return {"outcomes": [outcomes[msg["target"]]] if msg["target"] in outcomes else [], "reservations": []}
            async def close_stub(host, name, **kwargs):
                seat = await store.fetch_session(host, name)
                assert seat["session_generation"] == kwargs["expected_generation"]
                await store.update_session(host, name, status="closed")
                await sessions.refresh()
                return {"closed": True}
            monkeypatch.setattr(sessions, "close", close_stub)
            server.spawnctl = SimpleNamespace(spawn=spawn, spawn_status=status)
            await store.put(KINDS_KEY, json.dumps(["spawn", "advice"]))
            await store.put(SHAPE_KEY, json.dumps({"provider": "codex", "model": "gpt-6-astra", "effort": "high", "account": "fixture"}))
            server.port, server.binds = 0, ["127.0.0.1"]
            port = await server.bind()
            url = f"ws://127.0.0.1:{port}"
            try:
                async with client(url, DESK, h["tokens"]["desk"]) as desk:
                    pending = await desk({"type": "spawn", "request_id": "socket-spawn", "host": HOST,
                        "session_name": "spawn-target", "role": "lead", "parent_stream_id": DESK,
                        "objective": "Implement the approved fixture", "spec_id": "spec_fixture__ingress"})
                    assert pending["type"] == "spawn.pending_ruling", pending
                    await r.tick()
                    await asyncio.gather(*r.advisor_sessions.tasks.values())
                    row = await r._fetch(pending["ruling_request_id"])
                    stream = row["authority_stream_id"]
                    async with client(url, stream, tokens[stream]) as advisor:
                        ruled = await advisor({"type": "assistant.ruling", "request_id": "socket-rule",
                            "ruling_request_id": row["ruling_request_id"], "ruling": "approve"})
                        assert ruled["state"] == "done", ruled
                    advice = await desk({"type": "assistant.advise", "request_id": "socket-advice",
                        "work_item": "spec_fixture__ingress", "body": "GATE review the result"})
                    assert advice["type"] == "assistant.advise.ok", advice
                    await r.tick()
                    await asyncio.gather(*r.advisor_sessions.tasks.values())
                    row = await r._fetch(advice["ruling_request_id"])
                    stream = row["authority_stream_id"]
                    assert len(prompts[stream]) == 1 and pending["ruling_request_id"] in prompts[stream][0]
                    async with client(url, stream, tokens[stream]) as advisor:
                        answered = await advisor({"type": "assistant.answer", "request_id": "socket-answer",
                            "ruling_request_id": row["ruling_request_id"], "body": "The result is within scope."})
                        assert answered["type"] == "assistant.answer.ok", answered
                    await h["queue"].drain_once(force=True, kinds=["assistant_lane_ruling_result"])
                    assert sum("[Advisor answer]" in text for text in h["provider"].pastes) == 1
                    assert "The result is within scope." in h["provider"].pastes[-1]
            finally:
                await server.close()
    asyncio.run(run())
