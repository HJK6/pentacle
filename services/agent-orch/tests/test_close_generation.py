"""Client cleanup must carry its owned generation to the daemon fence."""
import asyncio
import json

from agent_orch import wsclient
from agent_orch.config import Config


def test_close_owned_generation_reaches_wire(monkeypatch, tmp_path):
    class Socket:
        sent = None
        closed = False

        async def send(self, raw):
            self.sent = json.loads(raw)

        async def recv(self):
            return json.dumps({"type": "close.ok", "request_id": self.sent["request_id"], "stale_generation": True})

        async def close(self):
            self.closed = True

    socket = Socket()

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setattr(wsclient, "_connect_rpc_ready", connect)
    result = asyncio.run(wsclient.close_once(Config("ws://fixture", "", "fixture", tmp_path),
                                             "fixture:worker", expected_generation="owned-old"))
    assert socket.sent["expected_generation"] == "owned-old"
    assert result["stale_generation"] and socket.closed
