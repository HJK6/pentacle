"""Committed historical batches must give existing socket writers a turn."""
from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from ingest import _identity_key
from server import CLIENT_SEND_QUEUE_MAX, Server
from sessions import Sessions
from store import Store


async def _subscribe(daemon, ws):
    assert json.loads(await ws.recv())["type"] == "welcome"
    await ws.send(json.dumps({"type": "hello", "client": "pentacle", "subscribe": {
        "snapshot": False, "include_subagents": True, "events_mode": "full",
    }}))
    assert json.loads(await ws.recv())["type"] == "ready"
    return next(p for p in daemon._clients if p.remote_address[1] == ws.local_address[1])


async def _committed_batch(store, count):
    lifecycle = await store.fetch_open_session_lifecycle("h:history", pane_pid="8123")
    assert lifecycle is not None
    entries = []
    for i in range(count):
        event = {"stream_id": "h:history", "host": "h", "session_name": "history",
                 "provider": "codex", "kind": "ASSIST_TEXT", "text": f"historical message {i}",
                 "timestamp": "2026-09-13T10:17:16.329Z",
                 "raw": {"jsonl_record_uuid": f"historical-{i}", "jsonl_event_index": 0}}
        entries.append({"stream_id": "h:history", "event": event,
                        "identity": _identity_key(event), "lifecycle": lifecycle})
    seqs = await store.append_session_events_lifecycle_cas(entries, limit=2000)
    assert seqs and len(seqs) == count and all(isinstance(s, int) for s in seqs)
    return entries, seqs


async def _read_events(ws, count):
    events = []
    try:
        async with asyncio.timeout(5):
            while len(events) < count:
                frame = json.loads(await ws.recv())
                if frame.get("type") == "chat.event":
                    events.append(frame["event"])
    except ConnectionClosed as exc:
        return events, str(exc)
    return events, None


async def _exercise(count, blocked=False):
    assert CLIENT_SEND_QUEUE_MAX == 256
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, local_host="h")
    daemon = Server(port=0, store=store, sessions=sessions, local_host="h")
    held_peer = None
    try:
        await store.open_session("h", "history", visibility="visible", pane_pid="8123")
        await sessions.refresh()
        await daemon.bind()
        async with connect(f"ws://127.0.0.1:{daemon.port}") as a, connect(f"ws://127.0.0.1:{daemon.port}") as b:
            peers = [await _subscribe(daemon, a), await _subscribe(daemon, b)]
            readers = [asyncio.create_task(_read_events(a, count))]
            if blocked:
                # Exercise the real websockets send/drain path under transport
                # backpressure. The writer remains suspended until resumed;
                # no fake broadcast, queue, send-success or second writer.
                held_peer = peers[1]
                held_peer.pause_writing()
            else:
                readers.append(asyncio.create_task(_read_events(b, count)))
            entries, seqs = await _committed_batch(store, count)
            # This is the production Codex post-commit broadcast loop: no
            # per-event Store await can incidentally let socket writers run.
            for index, (entry, seq) in enumerate(zip(entries, seqs)):
                await daemon.broadcast({"type": "chat.event", "event": {**entry["event"], "daemon_seq": seq}})
                if blocked and index == 2:
                    assert held_peer.paused
                    assert not daemon._client_writer_tasks[held_peer].done()
            results = await asyncio.gather(*readers)
            for events, error in results:
                assert error is None, error
                assert [e["daemon_seq"] for e in events] == seqs
                assert [e["text"] for e in events] == [e["event"]["text"] for e in entries]
            assert peers[0] in daemon._clients
            if blocked:
                assert peers[1] not in daemon._clients, "a blocked reader must still be isolated"
                if held_peer.paused:
                    held_peer.resume_writing()
                held_peer = None
                with pytest.raises(ConnectionClosed):
                    while True:
                        await asyncio.wait_for(b.recv(), 2)
                assert b.close_code == 1011
            # Replaying identical durable input never becomes another physical
            # delivery. Check the real store identity arm and the healthy wire.
            assert await store.append_session_events_lifecycle_cas(entries, limit=2000) == [None] * count
            for ws in ([a] if blocked else [a, b]):
                await ws.send(json.dumps({"type": "ping", "request_id": "after-replay"}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
                assert reply["type"] == "pong", reply
                assert reply["request_id"] == "after-replay"
            durable = await store.fetch_session_event_tail("h:history", limit=2000)
            assert len(durable) == count
    finally:
        if held_peer is not None and held_peer.paused:
            held_peer.resume_writing()
        await daemon.close()
        store.stop()


@pytest.mark.parametrize("count", [342, 370, 351])
def test_committed_history_keeps_healthy_readers_connected(count):
    asyncio.run(_exercise(count))


def test_committed_history_still_isolates_blocked_reader():
    asyncio.run(_exercise(370, blocked=True))
