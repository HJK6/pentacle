"""An accepted send survives the submitting connection closing.

spec-disconnect

These tests drive `_handle_client` at the server dispatch layer with a fake
socket and an intentionally delayed `send` handler. They exercise task lifetime
independently of a terminal transport: completion is the signal that an
accepted send remains durable after the submitting connection closes.

The first case covers disconnect survival; the second covers result delivery on
an open connection. Durable receipt lookup is covered by the separate receipt
contract and is intentionally out of scope here.
"""
from __future__ import annotations

import asyncio
import json

from server import Server


class _FakeWebSocket:
    """Yields the given raw frames then ends (clean disconnect). `send` feeds
    the per-client writer loop. Optionally blocks after the last frame until
    `hold` is released, to model a still-open connection."""

    remote_address = ("127.0.0.1", 54321)

    def __init__(self, frames: list[str], *, hold: asyncio.Event | None = None) -> None:
        self._frames = list(frames)
        self._hold = hold
        self.sent: list[str] = []

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        if self._hold is not None:
            await self._hold.wait()
        raise StopAsyncIteration

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _send_frame(index: int) -> str:
    return json.dumps(
        {
            "type": "send",
            "request_id": f"r{index}",
            "host": "hosta",
            "session_name": "codex-target",
            "text": f"m{index}",
        }
    )


def test_send_survives_submitter_disconnect_v2() -> None:
    """Several accepted sends complete after the client disconnects."""

    async def go() -> None:
        server = Server()
        completed: list[str] = []

        async def fake_send(msg: dict) -> dict:
            # The delay makes the task remain in flight while the client closes.
            await asyncio.sleep(0.05)
            completed.append(str(msg.get("request_id")))
            return {
                "type": "send.result",
                "request_id": msg.get("request_id"),
                "delivery": "landed",
            }

        server.handlers["send"] = fake_send
        ws = _FakeWebSocket([_send_frame(i) for i in range(3)])

        await server._handle_client(ws)

        # Servers that detach accepted work expose the tasks for deterministic
        # test cleanup; older implementations may finish them before return.
        pending = tuple(getattr(server, "_detached_send_tasks", ()) or ())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        assert sorted(completed) == ["r0", "r1", "r2"], (
            f"accepted sends were LOST when the submitter disconnected: "
            f"completed={completed!r} (accepted send tasks must survive disconnect)"
        )

    asyncio.run(go())


def test_held_connection_send_still_delivers_result_v2() -> None:
    """AC3 regression: on a still-open connection a send's result is produced
    and enqueued to the client — detaching the task must not drop delivery for
    the connected case. (v2 runs sends concurrently and orders pastes via
    comms._pane_input_lock, so send.result FRAME order is intentionally not
    asserted here.)"""

    async def go() -> None:
        server = Server()

        async def fake_send(msg: dict) -> dict:
            await asyncio.sleep(0.02)
            return {
                "type": "send.result",
                "request_id": msg.get("request_id"),
                "delivery": "landed",
            }

        server.handlers["send"] = fake_send
        hold = asyncio.Event()
        ws = _FakeWebSocket([_send_frame(0)], hold=hold)

        client_task = asyncio.create_task(server._handle_client(ws))
        # Let the (held-open) connection process + complete the send.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if any("send.result" in frame for frame in ws.sent):
                break
        # The connection is still open (hold not released), so the result is
        # delivered to the client writer.
        assert any("send.result" in frame for frame in ws.sent), (
            f"held-connection send.result was not delivered: sent={ws.sent!r}"
        )
        result = json.loads(next(f for f in ws.sent if "send.result" in f))
        assert result["request_id"] == "r0"
        assert result["delivery"] == "landed"

        hold.set()  # release -> clean disconnect
        await client_task

    asyncio.run(go())
