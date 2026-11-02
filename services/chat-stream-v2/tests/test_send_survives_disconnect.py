"""An accepted send survives the submitting connection closing.

spec-disconnect

These tests drive `_handle_client` at the server dispatch layer with a fake
socket and an event-held `send` handler. They exercise task lifetime
independently of a terminal transport: completion is the signal that accepted work survives the submitting connection
closing (not proof of durable storage).

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
        self.result_received = asyncio.Event()

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
        if json.loads(data).get("type") == "send.result":
            self.result_received.set()


async def _join_owned(tasks: list[asyncio.Task]) -> None:
    """Release happens at the caller; bound shutdown and inspect every outcome."""
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=2)
    for task in pending:
        task.cancel()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert not pending, "owned tasks did not finish after release"
    assert not [o for o in outcomes if isinstance(o, Exception)], outcomes


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
        entered = asyncio.Event()
        release = asyncio.Event()
        disconnect = asyncio.Event()
        sends: list[asyncio.Task] = []

        async def fake_send(msg: dict) -> dict:
            sends.append(asyncio.current_task())
            if len(sends) == 3:
                entered.set()
            await release.wait()
            completed.append(str(msg.get("request_id")))
            return {
                "type": "send.result",
                "request_id": msg.get("request_id"),
                "delivery": "landed",
            }

        server.handlers["send"] = fake_send
        ws = _FakeWebSocket([_send_frame(i) for i in range(3)], hold=disconnect)
        client = asyncio.create_task(server._handle_client(ws))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert completed == []
            disconnect.set()
            await asyncio.wait_for(asyncio.shield(client), 2)
            assert not release.is_set() and completed == [], "completion must remain held across disconnect"
            release.set()
            # A cancelled accepted send is a survival failure below, not a
            # cleanup exception that masks the missing completion oracle.
            outcomes = await asyncio.wait_for(asyncio.gather(*sends, return_exceptions=True), 2)
            assert not [o for o in outcomes if isinstance(o, Exception)], outcomes
            assert sorted(completed) == ["r0", "r1", "r2"], (
                f"accepted sends were LOST when the submitter disconnected: "
                f"completed={completed!r} (accepted send tasks must survive disconnect)"
            )
        finally:
            release.set()
            disconnect.set()
            await _join_owned([client, *sends])

    asyncio.run(go())


def test_held_connection_send_still_delivers_result_v2() -> None:
    """AC3 regression: on a still-open connection a send's result is produced
    and enqueued to the client — detaching the task must not drop delivery for
    the connected case. (v2 runs sends concurrently and orders pastes via
    comms._pane_input_lock, so send.result FRAME order is intentionally not
    asserted here.)"""

    async def go() -> None:
        server = Server()

        handler_completed = asyncio.Event()

        async def fake_send(msg: dict) -> dict:
            handler_completed.set()
            return {
                "type": "send.result",
                "request_id": msg.get("request_id"),
                "delivery": "landed",
            }

        server.handlers["send"] = fake_send
        hold = asyncio.Event()
        ws = _FakeWebSocket([_send_frame(0)], hold=hold)
        client_task = asyncio.create_task(server._handle_client(ws))
        try:
            await asyncio.wait_for(handler_completed.wait(), 2)
            try:
                await asyncio.wait_for(ws.result_received.wait(), 2)
            except TimeoutError:
                raise AssertionError(
                    f"held-connection send.result was not delivered after handler completed: sent={ws.sent!r}"
                ) from None
            assert not hold.is_set() and not client_task.done()
            assert any("send.result" in frame for frame in ws.sent), (
                f"held-connection send.result was not delivered: sent={ws.sent!r}"
            )
            result = json.loads(next(f for f in ws.sent if "send.result" in f))
            assert result["request_id"] == "r0"
            assert result["delivery"] == "landed"
        finally:
            hold.set()
            await _join_owned([client_task, *server._detached_send_tasks])

    asyncio.run(go())
