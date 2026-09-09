"""Reply delivery stays independent from lossy broadcast fanout."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import server
from server import CLIENT_SEND_QUEUE_MAX, Server, _EncodedFrame


class Socket:
    def __init__(self, *, peer: tuple[str, int] = ("10.0.0.0", 8765)) -> None:
        self.remote_address = peer
        self.sent: list[dict] = []
        self.close_calls = 0

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self, **_kwargs: object) -> None:
        self.close_calls += 1


def _attached(daemon: Server, ws: Socket, *, queue: asyncio.Queue | None = None) -> asyncio.Queue:
    queue = queue or asyncio.Queue(maxsize=CLIENT_SEND_QUEUE_MAX)
    daemon._clients.add(ws)
    daemon._client_send_queues[ws] = queue
    daemon._client_send_locks[ws] = asyncio.Lock()
    return queue


def test_reply_bypasses_saturated_event_queue() -> None:
    async def run() -> None:
        daemon = Server()
        ws = Socket()
        queue = _attached(daemon, ws)
        for index in range(CLIENT_SEND_QUEUE_MAX):
            queue.put_nowait(("chat.event", json.dumps({"type": "chat.event", "seq": index})))

        async def dispatch(_raw: object, *, websocket: object) -> list[dict]:
            assert websocket is ws
            return [{"type": "spawn_catalog_get.ok", "request_id": "catalog"}]

        daemon._dispatch = dispatch  # type: ignore[method-assign]
        await daemon._serve(ws, "catalog")

        assert ws.sent == [{"type": "spawn_catalog_get.ok", "request_id": "catalog"}]
        assert queue.qsize() == CLIENT_SEND_QUEUE_MAX
        assert ws in daemon._clients

    asyncio.run(run())


def test_event_overflow_never_counts_replies(caplog) -> None:
    async def run() -> None:
        daemon = Server()
        ws = Socket(peer=("10.0.0.0", 9911))
        queue = _attached(daemon, ws)
        daemon._client_identities[ws] = "example-service"
        dropped: list[Socket] = []
        daemon._drop_slow_consumer = dropped.append  # type: ignore[method-assign]
        for index in range(CLIENT_SEND_QUEUE_MAX):
            queue.put_nowait(("chat.event", json.dumps({"type": "chat.event", "seq": index})))

        async def dispatch(_raw: object, *, websocket: object) -> list[dict]:
            assert websocket is ws
            return [{"type": "spawn_catalog_get.ok", "request_id": "catalog"}]

        daemon._dispatch = dispatch  # type: ignore[method-assign]
        await daemon._serve(ws, "catalog")
        assert daemon._enqueue(ws, "chat.event", '{"type":"chat.event"}') is False

        assert ws.sent == [{"type": "spawn_catalog_get.ok", "request_id": "catalog"}]
        assert all(frame_type == "chat.event" for frame_type, _payload in list(queue._queue))
        assert dropped == [ws]

    caplog.set_level("WARNING", logger=server.log.name)
    asyncio.run(run())
    assert "client=example-service" in caplog.text
    assert "peer=('10.0.0.0', 9911)" in caplog.text


def test_streamed_reply_chunks_stay_contiguous_under_broadcast() -> None:
    async def run() -> None:
        daemon = Server()
        ws = Socket()
        queue = _attached(daemon, ws)
        first_chunk_sent = asyncio.Event()
        release_rest = asyncio.Event()

        async def chunks():
            yield _EncodedFrame("request_stream_events.chunk", '{"type":"chunk","n":1}')
            first_chunk_sent.set()
            await release_rest.wait()
            yield _EncodedFrame("request_stream_events.chunk", '{"type":"chunk","n":2}')
            yield _EncodedFrame("request_stream_events.ok", '{"type":"done","n":3}')

        async def dispatch(_raw: object, *, websocket: object):
            assert websocket is ws
            return chunks()

        daemon._dispatch = dispatch  # type: ignore[method-assign]
        writer = asyncio.create_task(daemon._client_writer_loop(ws))
        daemon._client_writer_tasks[ws] = writer
        try:
            serving = asyncio.create_task(daemon._serve(ws, "stream"))
            await first_chunk_sent.wait()
            assert daemon._enqueue(ws, "chat.event", '{"type":"chat.event"}') is True
            # Let the broadcast writer race for the shared send lock while the
            # streamed reply is paused after its first chunk. It must not send
            # its frame between direct-reply chunks.
            await asyncio.sleep(0)
            assert [frame["type"] for frame in ws.sent] == ["chunk"]
            release_rest.set()
            await serving
            await asyncio.sleep(0)
            frame_types = [frame["type"] for frame in ws.sent]
            assert frame_types[:3] == ["chunk", "chunk", "done"]
            assert frame_types.index("chat.event") > frame_types.index("done")
            assert queue.qsize() == 0
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    asyncio.run(run())


def test_broadcast_emitted_during_request_precedes_its_reply() -> None:
    async def run() -> None:
        daemon = Server()
        ws = Socket()
        _attached(daemon, ws)

        async def dispatch(_raw: object, *, websocket: object) -> list[dict]:
            assert websocket is ws
            await daemon.broadcast({"type": "notification", "notification": {"id": "n1"}})
            await asyncio.sleep(0)
            return [{"type": "notification.await.ok", "request_id": "await"}]

        daemon._dispatch = dispatch  # type: ignore[method-assign]
        writer = asyncio.create_task(daemon._client_writer_loop(ws))
        daemon._client_writer_tasks[ws] = writer
        try:
            await daemon._serve(ws, "await")
            await asyncio.sleep(0)
            assert [frame["type"] for frame in ws.sent] == ["notification", "notification.await.ok"]
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    asyncio.run(run())


def test_reply_to_gone_client_is_noop() -> None:
    class Gone(Exception):
        pass

    class GoneSocket(Socket):
        async def send(self, _payload: str) -> None:
            raise Gone()

    async def run() -> None:
        daemon = Server()
        ws = GoneSocket()
        _attached(daemon, ws)
        unregister_calls = 0
        original_unregister = daemon._unregister_client

        def counted_unregister(*args: object, **kwargs: object) -> int:
            nonlocal unregister_calls
            unregister_calls += 1
            return original_unregister(*args, **kwargs)

        daemon._unregister_client = counted_unregister  # type: ignore[method-assign]

        async def dispatch(_raw: object, *, websocket: object) -> list[dict]:
            assert websocket is ws
            return [{"type": "ping.ok"}]

        daemon._dispatch = dispatch  # type: ignore[method-assign]
        with patch.object(server, "ConnectionClosed", Gone):
            await daemon._serve(ws, "ping")
        assert unregister_calls == 1
        assert ws not in daemon._clients

    asyncio.run(run())


def test_welcome_precedes_first_reply() -> None:
    class OneRequestSocket(Socket):
        def __init__(self) -> None:
            super().__init__()
            self.two_frames = asyncio.Event()

        async def send(self, payload: str) -> None:
            await super().send(payload)
            if len(self.sent) == 2:
                self.two_frames.set()

        def __aiter__(self):
            return self._incoming()

        async def _incoming(self):
            yield json.dumps({"type": "ping", "request_id": "first"})
            await self.two_frames.wait()

    async def run() -> None:
        daemon = Server()
        ws = OneRequestSocket()

        async def stopped_writer(_ws: object) -> None:
            return None

        daemon._client_writer_loop = stopped_writer  # type: ignore[method-assign]
        await asyncio.wait_for(daemon._handle_client(ws), timeout=0.2)
        assert [frame["type"] for frame in ws.sent[:2]] == ["welcome", "pong"]

    asyncio.run(run())
