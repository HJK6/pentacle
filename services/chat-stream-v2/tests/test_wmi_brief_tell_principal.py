"""The daily brief's dedicated credential reaches only two configured routes."""
from __future__ import annotations

import asyncio
import json

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from comms import Comms
from server import Server
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from test_system_notification_producer import Peer
from test_tell_immediate_delivery import ScriptedClaude, IDLE_PLACEHOLDER

ACTOR = "examplehost:wmi-daily-brief"
TOKEN = "synthetic-independent-brief-token"
COMPOSITE = "desk:assistant"
OPS = "localhost:ops"
FD = "localhost:frontdesk"
CONFIG_ENV = "PENTACLE_WMI_BRIEF_PRODUCER_CONFIG"


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    token = tmp_path / "brief-token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    config = tmp_path / "brief-config.json"
    config.write_text(json.dumps({
        "stream_id": ACTOR, "token_file": str(token),
        "composite_stream_id": COMPOSITE, "ops_stream_id": OPS,
    }))
    config.chmod(0o600)
    monkeypatch.setenv(CONFIG_ENV, str(config))
    return token, config


def frame(verb="tell", **changes):
    msg = {"type": verb, "request_id": "brief-request", "from_stream_id": ACTOR,
           "stream_token": TOKEN}
    if verb == "hello":
        msg["subscribe"] = {"snapshot": False, "mode": "rpc"}
    elif verb == "tell":
        msg.update(tell_id="brief-tell", to_stream_id=COMPOSITE, text="GATE synthetic brief",
                   ttl_seconds=3600, sanitize=True)
    return {**msg, **changes}


async def dispatch(server, peer, msg):
    return (await server._dispatch(json.dumps(msg), websocket=peer))[0]


async def authenticate(server, peer):
    reply = await dispatch(server, peer, frame("hello"))
    assert reply["type"] == "ready", reply


class Harness:
    def __init__(self, path, *, bound=True):
        self.store = Store(str(path))
        self.bound = bound

    async def __aenter__(self):
        self.store.start()
        self.tmux = ScriptedClaude(start=IDLE_PLACEHOLDER)
        self.sessions = Sessions(self.store, tmux=self.tmux, local_host="localhost")
        self.comms = Comms(self.store, self.sessions, SpawnCtl(self.store, self.sessions, tmux=self.tmux))
        await self.sessions.open("localhost", "ops", provider="claude")
        if self.bound:
            await self.sessions.open("localhost", "frontdesk", provider="claude")
        seat = await self.store.fetch_session("localhost", "frontdesk")
        self.composite = AssistantComposite(self.store, config=AssistantCompositeConfig(
            enabled=True, name="briefdesk", stream_id=COMPOSITE,
            direct_primary_stream_id=FD,
            direct_primary_generation=seat["session_generation"] if seat else "pending",
        ))
        await self.composite.load_binding()
        await self.composite.ensure_projection()
        await self.sessions.refresh()
        self.server = Server(store=self.store, sessions=self.sessions, comms=self.comms, local_host="localhost")
        self.server.assistant_composites = {"briefdesk": self.composite}
        return self

    async def __aexit__(self, *exc):
        self.store.stop()


@pytest.mark.parametrize("target", [COMPOSITE, OPS])
def test_file_principal_tell_uses_real_delivery_ledger_and_sender_stamp(credentials, tmp_path, target):
    async def run():
        async with Harness(tmp_path / "brief.db") as h:
            peer = Peer()
            await authenticate(h.server, peer)
            result = await dispatch(h.server, peer, frame(to_stream_id=target))
            assert result["type"] == "tell.ok", result
            assert result["delivery_status"] == "delivered"
            expected = f"[from {ACTOR}] [tell:brief-tell]\nGATE synthetic brief"
            assert h.tmux.pastes == [expected]
            ledger = await h.store.get_tell_delivery("brief-tell")
            assert ledger["delivery"]["from_stream_id"] == ACTOR
            assert ledger["delivery"]["text"] == expected
            assert ledger["delivery"]["to_stream_id"] == (FD if target == COMPOSITE else OPS)
    asyncio.run(run())
