"""Authenticated dispatch through real Comms/Store; only pane transport is fake."""
import asyncio
import hashlib
import json

import pytest

from ledger import CONTEXT_COMPACT_COMMAND
from server import Server
from store import STREAM_TOKEN_HASH_VERSION
from test_send_semantics import CodexQueueTmux, FakeTmux, HOST, NAME, _new_comms

SID = f"{HOST}:{NAME}"
TOKEN = "synthetic-compact-token"
pytestmark = pytest.mark.usefixtures("virtual_submission_clock")


class LocalPeer:
    remote_address = ("127.0.0.1", 12345)


async def setup(tmp_path, provider="codex", tmux=None):
    tmux = tmux or (CodexQueueTmux() if provider == "codex" else FakeTmux())
    comms, store, sessions = _new_comms(tmux, tmp_path)
    await sessions.open(HOST, NAME, provider=provider, context_level="none")
    await store.grant_stream_token(HOST, NAME, hashlib.sha256(TOKEN.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
    sessions.apply_live(SID, working=True)
    server = Server(store=store, sessions=sessions, comms=comms)
    return server, tmux, store, sessions


async def request(server, **overrides):
    msg = {"type": "compact", "request_id": "one", "from_stream_id": SID,
           "stream_token": TOKEN, **overrides}
    return (await server._dispatch(json.dumps(msg), websocket=LocalPeer()))[0]


@pytest.mark.parametrize("provider,command", [("codex", "/compact"), ("claude", CONTEXT_COMPACT_COMMAND)])
@pytest.mark.parametrize("front_desk", [False, True])
def test_busy_below_threshold_self_compact_is_bare_ordinary_input(tmp_path, provider, command, front_desk):
    async def run():
        server, tmux, store, sessions = await setup(tmp_path, provider)
        try:
            if front_desk:
                from front_desk_digest import FrontDeskDigest
                from types import SimpleNamespace
                from v2_runtime import primary_assistant_name
                server.comms.front_desk_digest = FrontDeskDigest(store, lambda: SimpleNamespace(
                    enabled=True, name=primary_assistant_name(), direct_primary=True,
                    direct_primary_stream_id=SID,
                    direct_primary_generation=sessions.get(SID)["session_generation"],
                ))
            # A forged force or body field cannot alter daemon-owned input.
            reply = await request(server, urgent=True, text="untrusted", force=True)
            assert reply["type"] == "compact.ok", reply
            assert tmux.pastes == [command]
            assert tmux.escapes == 0
            assert sessions.get(SID)["working"] is True
            if provider == "codex":
                assert reply["provider_queued"] is True
            receipt = await store.get_send_receipt(SID, reply["send_request_id"], include_display_text=True)
            assert receipt["actor_stream_id"] == SID and receipt["actor_trusted"]
            assert receipt["display_text"] == command
            assert await store.nudge_state(SID, "context_compact") is None
            if front_desk:
                held = await request(server, type="send", request_id="ordinary", text="routine update",
                                     stream_id=SID, _self_compact_stream_id=SID)
                assert held["reason"] == "front_desk_held"
                assert tmux.pastes == [command]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("overrides", [
    {"stream_token": None}, {"stream_token": "invalid"}, {"from_stream_id": "elsewhere:seat"},
    {"stream_id": "elsewhere:seat"}, {"to_stream_id": "elsewhere:seat"},
    {"host": "elsewhere"}, {"session_name": "another"},
    {"stream_token": None, "_auth_context": {"token_verified": True, "stream_id": SID}},
])
def test_dispatch_refuses_unverified_or_other_seat_without_input(tmp_path, overrides):
    async def run():
        server, tmux, store, _ = await setup(tmp_path)
        try:
            reply = await request(server, **overrides)
            assert reply["error_code"] == "compact_self_only", reply
            assert tmux.pastes == [] and tmux.escapes == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_replay_after_restart_and_distinct_request(tmp_path):
    async def run():
        server, tmux, store, sessions = await setup(tmp_path)
        try:
            await request(server)
            # New dispatch/Comms instances reuse the durable send claim.
            from comms import Comms
            server = Server(store=store, sessions=sessions,
                            comms=Comms(store, sessions, server.comms.spawnctl))
            replay = await request(server)
            assert replay["coalesced"] is True
            assert tmux.pastes == ["/compact"]
            await request(server, request_id="two")
            assert tmux.pastes == ["/compact", "/compact"]
        finally:
            store.stop()
    asyncio.run(run())


def test_concurrent_same_request_pastes_once(tmp_path):
    async def run():
        class HeldTmux(CodexQueueTmux):
            async def paste(self, name, text):
                entered.set()
                await release.wait()
                await super().paste(name, text)
        entered, release = asyncio.Event(), asyncio.Event()
        server, tmux, store, _ = await setup(tmp_path, tmux=HeldTmux())
        tasks = []
        try:
            winner = asyncio.create_task(request(server))
            tasks.append(winner)
            await asyncio.wait_for(entered.wait(), 2)
            retry = asyncio.create_task(request(server))
            tasks.append(retry)
            await asyncio.sleep(0)
            release.set()
            _, replay = await asyncio.wait_for(asyncio.gather(*tasks), 2)
            assert replay["coalesced"] is True
            assert tmux.pastes == ["/compact"]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.stop()
    asyncio.run(run())


def test_retirement_between_auth_and_route_never_compacts_successor(tmp_path, monkeypatch):
    async def run():
        server, tmux, store, sessions = await setup(tmp_path)
        original = server.comms.send
        async def retire_then_send(msg):
            await store.update_session(HOST, NAME, status="closed")
            await sessions.refresh()
            await sessions.open(HOST, "successor", provider="codex", handoff_from_stream_id=SID)
            return await original(msg)
        monkeypatch.setattr(server.comms, "send", retire_then_send)
        try:
            reply = await request(server)
            assert reply["error_code"] == "compact_self_only", reply
            assert tmux.pastes == [] and tmux.escapes == 0
        finally:
            store.stop()
    asyncio.run(run())
