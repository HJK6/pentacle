"""The large backfill must not monopolize the daemon event loop."""
from __future__ import annotations

import asyncio
import json

import pytest

from server import Server


# Fixed harness budget, deliberately stricter than the daemon keepalive
# deadline so a synchronous stall is observable before it becomes a timeout.
KEEPALIVE_PING_PONG_BUDGET_SECONDS = 0.05
STREAM_ID = "testhost:v2-loop"
EVENT_COUNT = 500
EVENT_TEXT_BYTES = 36 * 1024


def _events() -> list[dict]:
    return [
        {
            "stream_id": STREAM_ID,
            "kind": "USER",
            "timestamp": "2026-08-05T00:00:00Z",
            "daemon_seq": index + 1,
            "text": f"m{index}" + ("x" * (EVENT_TEXT_BYTES - len(f"m{index}"))),
        }
        for index in range(EVENT_COUNT)
    ]


class _Sessions:
    def get(self, _stream_id: str) -> None:
        return None

    async def resolve(self, _msg: dict) -> tuple[str, str]:
        return "testhost", "v2-loop"


class _Store:
    def __init__(self, events: list[dict]) -> None:
        self.events = events

    async def fetch_session_event_tail(self, _stream_id: str, *, limit: int) -> list[dict]:
        return self.events[-limit:]

    async def fetch_session_event_page(
        self,
        _stream_id: str,
        *,
        before_daemon_seq: int | None,
        limit: int,
    ) -> list[dict]:
        eligible = [
            event for event in self.events
            if before_daemon_seq is None or event["daemon_seq"] < before_daemon_seq
        ]
        return eligible[-limit:]


def test_history_page_wait_does_not_block_inventory_or_pong() -> None:
    """Exercise socket writes, not just the runnable event-loop ping handler."""
    async def run() -> None:
        page_waiting, release_page = asyncio.Event(), asyncio.Event()
        ready_seen, pong_seen = asyncio.Event(), asyncio.Event()

        class PausedStore(_Store):
            async def fetch_session_event_page(self, stream_id, **kwargs):
                if kwargs['before_daemon_seq'] is not None:
                    page_waiting.set()
                    await release_page.wait()
                return await super().fetch_session_event_page(stream_id, **kwargs)

        class Socket:
            remote_address = ('127.0.0.1', 12345)

            def __init__(self):
                self.frames = []

            async def send(self, raw):
                frame = json.loads(raw)
                self.frames.append(frame)
                if frame['type'] == 'session.inventory':
                    assert frame['sessions'][0]['bootstrap_state'] == 'ready'
                    ready_seen.set()
                if frame['type'] == 'pong':
                    pong_seen.set()

        daemon = Server()
        daemon.sessions = _Sessions()
        daemon.store = PausedStore(_events()[:4])
        socket = Socket()
        daemon._register_client(socket)
        daemon._client_include_subagents[socket] = True
        writer = daemon._client_writer_tasks[socket]
        history = asyncio.create_task(daemon._serve(socket, json.dumps({
            'type': 'request_stream_events', 'request_id': 'history',
            'stream_id': STREAM_ID, 'limit': 4, 'chunk_limit': 2,
        })))
        ping = None
        try:
            await asyncio.wait_for(page_waiting.wait(), 1)
            await daemon.broadcast({'type': 'session.inventory', 'sessions': [{
                'stream_id': 'testhost:v2-new', 'host': 'testhost',
                'session_name': 'v2-new', 'bootstrap_state': 'ready',
            }]})
            ping = asyncio.create_task(daemon._serve(socket, json.dumps({
                'type': 'ping', 'request_id': 'control',
            })))
            await asyncio.wait_for(asyncio.gather(ready_seen.wait(), pong_seen.wait()), 0.25)
            assert not history.done(), 'control frames must precede history completion'
            release_page.set()
            await asyncio.wait_for(history, 1)
            await ping
            frames = [f for f in socket.frames if f.get('request_id') == 'history']
            assert [f['type'] for f in frames] == [
                'request_stream_events.chunk', 'request_stream_events.chunk',
                'request_stream_events.ok',
            ]
            assert [e['daemon_seq'] for f in frames for e in f.get('events', [])] == [3, 4, 1, 2]
            assert all(f['stream_id'] == STREAM_ID for f in frames)
            assert len([f for f in socket.frames if f.get('request_id') == 'control']) == 1
        finally:
            release_page.set()
            tasks = [history, writer] + ([ping] if ping else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            daemon._unregister_client(socket)

    asyncio.run(run())


def _server(events: list[dict]) -> Server:
    server = Server.__new__(Server)
    server.sessions = _Sessions()
    server.store = _Store(events)
    server.handlers = {"request_stream_events": server._on_request_stream_events}
    return server


class _RecordingSocket:
    remote_address = ('127.0.0.1', 12345)

    def __init__(self):
        self.frames = []

    async def send(self, raw):
        self.frames.append(json.loads(raw))


def test_blob_stream_stays_contiguous_when_broadcast_waits() -> None:
    async def run():
        paused, resume = asyncio.Event(), asyncio.Event()
        daemon, socket = Server(), _RecordingSocket()

        async def blob(_msg):
            async def frames():
                yield {'type': 'fetch_blob.chunk', 'request_id': 'blob', 'index': 0}
                paused.set()
                await resume.wait()
                yield {'type': 'fetch_blob.chunk', 'request_id': 'blob', 'index': 1}
                yield {'type': 'fetch_blob.ok', 'request_id': 'blob'}
            return frames()

        daemon.handlers['fetch_blob'] = blob
        daemon._register_client(socket)
        writer = daemon._client_writer_tasks[socket]
        task = asyncio.create_task(daemon._serve(socket, json.dumps({'type': 'fetch_blob'})))
        try:
            await asyncio.wait_for(paused.wait(), 1)
            await daemon.broadcast({'type': 'host.status', 'host': 'hosta'})
            for _ in range(5):
                await asyncio.sleep(0)
            assert [f['type'] for f in socket.frames] == ['fetch_blob.chunk']
            resume.set()
            await asyncio.wait_for(task, 1)
            for _ in range(5):
                await asyncio.sleep(0)
            assert [f['type'] for f in socket.frames] == [
                'fetch_blob.chunk', 'fetch_blob.chunk', 'fetch_blob.ok', 'host.status',
            ]
        finally:
            task.cancel()
            writer.cancel()
            await asyncio.gather(task, writer, return_exceptions=True)
            daemon._unregister_client(socket)
    asyncio.run(run())


def test_history_and_sustained_broadcasts_both_make_progress() -> None:
    async def run():
        daemon, socket = Server(), _RecordingSocket()

        async def history(_msg):
            async def frames():
                for index in range(20):
                    yield {'type': 'request_stream_events.chunk', 'request_id': 'history', 'index': index}
                yield {'type': 'request_stream_events.ok', 'request_id': 'history'}
            return frames()

        async def broadcast_until_done():
            index = 0
            while True:
                await daemon.broadcast({'type': 'host.status', 'host': 'hosta', 'index': index})
                index += 1
                await asyncio.sleep(0)

        daemon.handlers['request_stream_events'] = history
        daemon._register_client(socket)
        writer = daemon._client_writer_tasks[socket]
        flood = asyncio.create_task(broadcast_until_done())
        try:
            await asyncio.wait_for(daemon._serve(socket, json.dumps({'type': 'request_stream_events'})), 1)
            history_frames = [f for f in socket.frames if f.get('request_id') == 'history']
            assert [f['index'] for f in history_frames[:-1]] == list(range(20))
            assert history_frames[-1]['type'] == 'request_stream_events.ok'
            start = socket.frames.index(history_frames[0])
            end = socket.frames.index(history_frames[-1])
            assert any(f['type'] == 'host.status' for f in socket.frames[start:end])
        finally:
            flood.cancel()
            writer.cancel()
            await asyncio.gather(flood, writer, return_exceptions=True)
            daemon._unregister_client(socket)
    asyncio.run(run())


def test_cancelled_history_releases_writer_and_closes_producer() -> None:
    async def run():
        paused, finalized = asyncio.Event(), asyncio.Event()
        daemon, socket = Server(), _RecordingSocket()

        async def history(_msg):
            async def frames():
                try:
                    yield {'type': 'request_stream_events.chunk', 'request_id': 'history'}
                    paused.set()
                    await asyncio.Event().wait()
                finally:
                    finalized.set()
            return frames()

        daemon.handlers['request_stream_events'] = history
        daemon._register_client(socket)
        writer = daemon._client_writer_tasks[socket]
        task = asyncio.create_task(daemon._serve(socket, json.dumps({'type': 'request_stream_events'})))
        try:
            await asyncio.wait_for(paused.wait(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert finalized.is_set()
            assert not daemon._client_send_locks[socket].locked()
            await asyncio.wait_for(daemon._serve(socket, json.dumps({'type': 'ping', 'request_id': 'after-cancel'})), 1)
            assert socket.frames[-1]['type'] == 'pong'
            assert not any(f['type'] == 'request_stream_events.ok' for f in socket.frames)
        finally:
            task.cancel()
            writer.cancel()
            await asyncio.gather(task, writer, return_exceptions=True)
            daemon._unregister_client(socket)
    asyncio.run(run())


@pytest.mark.parametrize('disconnect', ['unregister', 'socket-close'])
def test_disconnected_history_stops_and_closes_producer(monkeypatch, disconnect) -> None:
    async def run():
        import server as module
        class Gone(Exception):
            pass
        monkeypatch.setattr(module, 'ConnectionClosed', Gone)
        finalized = asyncio.Event()
        daemon = Server()

        class Socket(_RecordingSocket):
            async def send(self, raw):
                if self.frames:
                    raise Gone()
                await super().send(raw)
                if disconnect == 'unregister':
                    daemon._unregister_client(self)

        socket = Socket()
        async def frames():
            try:
                yield {'type': 'request_stream_events.chunk', 'index': 0}
                yield {'type': 'request_stream_events.chunk', 'index': 1}
            finally:
                finalized.set()
        daemon._register_client(socket)
        writer = daemon._client_writer_tasks[socket]
        try:
            assert not await daemon._send_direct(socket, frames(), interleave_history=True)
            assert finalized.is_set()
            assert len(socket.frames) == 1
            assert socket not in daemon._clients
            assert socket not in daemon._client_send_locks
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            daemon._unregister_client(socket)
    asyncio.run(run())


def test_large_backfill_keeps_ping_pong_inside_deadline() -> None:
    """A known ~18 MiB pane leaves an application ping/pong runnable promptly."""
    async def _go() -> float:
        daemon = _server(_events())
        loop = asyncio.get_running_loop()
        stop = False
        latencies: list[float] = []

        async def ping_pong_probe() -> None:
            while not stop:
                sent_at = loop.time()
                await asyncio.sleep(0)
                await daemon._on_ping({})
                latencies.append(loop.time() - sent_at)

        probe = asyncio.create_task(ping_pong_probe())
        # Let the probe establish its first pending ping before the backfill
        # starts; a synchronous whole-history assembly then makes this assertion
        # fail against the known-bad implementation.
        await asyncio.sleep(0)
        result = await daemon._dispatch(json.dumps({
            "request_id": "loop-probe",
            "stream_id": STREAM_ID,
            "limit": EVENT_COUNT,
            "type": "request_stream_events",
        }))
        if hasattr(result, "__aiter__"):
            async for _frame in result:
                pass
        else:
            # The pre-fix handler returned one dict; emulate _serve's synchronous
            # websocket json encoding so the RED run covers the shipped path.
            for frame in result:
                json.dumps(frame)
        stop = True
        await probe
        return max(latencies, default=0.0)

    max_latency = asyncio.run(_go())
    assert max_latency < KEEPALIVE_PING_PONG_BUDGET_SECONDS, (
        f"backfill blocked ping/pong for {max_latency:.3f}s "
        f"(budget {KEEPALIVE_PING_PONG_BUDGET_SECONDS:.3f}s)"
    )
