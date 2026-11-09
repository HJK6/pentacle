"""Total request budgets on disk-backed, authenticated WebSocket history reads."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

import pytest
import websockets

from _shared import operator_auth
from server import RECENT_LIMIT, Server
from sessions import Sessions
from store import Store

STREAM = "fixture-host:history"
OTHER = "fixture-host:other"


class History:
    def __init__(self, store, server, registry, operator, port):
        self.store, self.server, self.registry = store, server, registry
        self.operator, self.port = operator, port

    async def seed(self, count, *, stream=STREAM):
        # Returned append IDs are the independent oracle, never a page read.
        entries = [{"stream_id": stream, "identity": f"{stream}-{i}", "event": {
            "stream_id": stream, "kind": "USER", "text": f"event {i}",
            "timestamp": "2026-01-01T00:00:00Z", "raw": {},
        }} for i in range(count)]
        ids = await self.store.append_session_events_lifecycle_cas(entries, limit=10000)
        assert ids is not None and len(ids) == count
        assert all(isinstance(seq, int) and not isinstance(seq, bool) for seq in ids)
        return ids

    @asynccontextmanager
    async def connect(self, credential=None):
        credential = self.operator if credential is None else credential
        async with websockets.connect(f"ws://127.0.0.1:{self.port}", max_size=None) as ws:
            welcome = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert welcome["type"] == "welcome"
            await ws.send(json.dumps({
                "type": "hello", "request_id": "hello", "client": credential["client_kind"],
                "auth_v2": {"scheme": operator_auth.AUTH_SCHEME,
                    "credential_id": credential["credential_id"],
                    "proof": operator_auth.make_proof(credential["proof_key"],
                        welcome["auth"]["operator"]["nonce"], credential["credential_id"],
                        credential["client_kind"])},
                "subscribe": {"snapshot": False, "mode": "rpc", "include_subagents": True},
            }))
            async with asyncio.timeout(5):
                while True:
                    frame = json.loads(await ws.recv())
                    if frame.get("request_id") == "hello":
                        assert frame["type"] in {"hello", "ready"}, frame
                        break
            yield ws

    async def request(self, ws, *, request_id="history", **fields):
        await ws.send(json.dumps({"type": "request_stream_events", "stream_id": STREAM,
            "request_id": request_id, "chunk_limit": 8, **fields}))
        frames = []
        async with asyncio.timeout(5):
            while True:
                raw = await ws.recv()
                frame = json.loads(raw)
                if frame.get("request_id") != request_id:
                    continue
                frames.append(frame)
                if frame["type"] != "request_stream_events.chunk":
                    return frames


@asynccontextmanager
async def history(tmp_path):
    store = Store(str(tmp_path / "sessions.db"))
    store.start()
    server = None
    port = None
    try:
        for name in ("history", "other"):
            await store.open_session("fixture-host", name, provider="codex", pane_status="pane_alive",
                created_at="2026-01-01T00:00:00Z")
        sessions = Sessions(store, tmux=None, local_host="fixture-host")
        await sessions.refresh()
        registry = operator_auth.OperatorCredentialRegistry(tmp_path / "operator.json")
        registry.initialize()
        _, envelope = registry.issue("pentacle", label="History fixture")
        operator = operator_auth.decode_envelope(envelope)
        server = Server(host="127.0.0.1", port=0, dot_tls_port=0, store=store,
            sessions=sessions, local_host="fixture-host")
        server.operator_credential_registry = registry
        port = await server.bind()
        yield History(store, server, registry, operator, port)
    finally:
        if server is not None:
            await server.close()
        assert store.stop(), "CLEANUP_FAIL: owned Store worker did not stop"
        if port is not None:
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", port)


def assert_history(frames, eligible, limit, *, request_id="history"):
    assert frames and frames[-1]["type"] == "request_stream_events.ok", frames
    assert sum(f["type"] == "request_stream_events.ok" for f in frames) == 1, frames
    assert all(f["request_id"] == request_id and f["stream_id"] == STREAM for f in frames), frames
    assert all(f["type"] == "request_stream_events.chunk" for f in frames[:-1]), frames
    actual = [[e["daemon_seq"] for e in f["events"]] for f in frames]
    newest = eligible[-limit:] if limit else []
    # The wire visits newest pages first, ascending within each page.
    expected = [newest[max(0, end - 8):end] for end in range(len(newest), 0, -8)]
    if len(expected) > 1:
        expected.append([])  # one empty OK after chunks
    elif not expected:
        expected = [[]]
    assert actual == expected, f"PRODUCT_FAIL: page identity/order/count: {actual=} {expected=}"
    delivered = [seq for page in actual for seq in page]
    assert len(delivered) == len(set(delivered)) == min(limit, len(eligible))
    assert sorted(delivered) == newest
    assert all(e["stream_id"] == STREAM for f in frames for e in f["events"])
    return delivered


def test_two_page_total_limit(tmp_path):
    async def run():
        async with history(tmp_path) as h:
            ids = await h.seed(13)
            assert ids == list(range(1, 14))
            async with h.connect() as ws:
                frames = await h.request(ws, limit=10)
            assert_history(frames, ids, 10)
            assert [[e["daemon_seq"] for e in f["events"]] for f in frames] == [list(range(6, 14)), [4, 5], []]
    asyncio.run(run())


def test_three_page_total_limit(tmp_path):
    async def run():
        async with history(tmp_path) as h:
            ids = await h.seed(25)
            assert ids == list(range(1, 26))
            async with h.connect() as ws:
                frames = await h.request(ws, limit=17)
            assert_history(frames, ids, 17)
            assert [[e["daemon_seq"] for e in f["events"]] for f in frames] == [list(range(18, 26)), list(range(10, 18)), [9], []]
    asyncio.run(run())


@pytest.mark.parametrize("limit", [0, 1, 7, 8, 9, 10, 16, 17])
@pytest.mark.parametrize("relative", [-1, 0, 1])
def test_limit_and_available_count(tmp_path, limit, relative):
    async def run():
        async with history(tmp_path) as h:
            ids = await h.seed(max(0, limit + relative))
            async with h.connect() as ws:
                assert_history(await h.request(ws, limit=limit), ids, limit)
    asyncio.run(run())


def test_empty_history(tmp_path):
    async def run():
        async with history(tmp_path) as h:
            async with h.connect() as ws:
                assert_history(await h.request(ws, limit=10), [], 10)
    asyncio.run(run())


def test_cursor_walk_sparse_generation_isolation_and_reconnect(tmp_path):
    async def run():
        async with history(tmp_path) as h:
            stale = await h.seed(5)
            await h.store.mark_closed("fixture-host", "history", closed_at="2026-01-02T00:00:00Z",
                pane_status="pane_dead")
            await h.store.open_session("fixture-host", "history", provider="codex", pane_status="pane_alive",
                created_at="2026-01-03T00:00:00Z")
            await h.server.sessions.refresh()
            ids = []
            for i in range(25):
                # Interleave stream rows so history IDs have real gaps.
                for stream in (STREAM, OTHER):
                    seq = await h.store.append_session_event(stream, {"stream_id": stream,
                        "kind": "USER", "text": f"current {i}", "raw": {}},
                        identity=f"current-{i}", limit=1000)
                    assert isinstance(seq, int)
                    if stream == STREAM:
                        ids.append(seq)
            assert set(stale).isdisjoint(ids)
            cursor, seen = None, []
            async with h.connect() as ws:
                while True:
                    fields = {} if cursor is None else {"before_daemon_seq": cursor}
                    eligible = [seq for seq in ids if cursor is None or seq < cursor]
                    frames = await h.request(ws, limit=10, **fields)
                    delivered = assert_history(frames, eligible, 10)
                    if not delivered:
                        break
                    seen.extend(delivered)
                    cursor = min(delivered)
            assert sorted(seen) == ids and len(set(seen)) == len(seen)
            # Cursor is strictly between two eligible event IDs.
            between = ids[12] + 1
            assert between not in ids
            eligible = [seq for seq in ids if seq < between]
            replies = []
            for _ in range(2):
                async with h.connect() as ws:
                    frames = await h.request(ws, limit=10, before_daemon_seq=between)
                    assert_history(frames, eligible, 10)
                    replies.append(frames)
            assert replies[0] == replies[1]
            # A valid scoped credential cannot read a foreign stream.
            _, envelope = h.registry.issue("pentacle-mobile", label="Other fixture", scope={"stream": OTHER})
            async with h.connect(operator_auth.decode_envelope(envelope)) as ws:
                refused = await h.request(ws, limit=10)
                assert len(refused) == 1 and refused[0]["error_code"] == "scope_denied", refused
    asyncio.run(run())


@pytest.mark.parametrize("fields", [{}, {"limit": None}, {"limit": "bad"}, {"limit": RECENT_LIMIT + 1}, {"limit": -1}])
def test_existing_default_and_clamped_limit(tmp_path, fields):
    async def run():
        async with history(tmp_path) as h:
            ids = await h.seed(RECENT_LIMIT + 1)
            async with h.connect() as ws:
                frames = await h.request(ws, **fields)
                effective = 0 if fields.get("limit") == -1 else RECENT_LIMIT
                assert_history(frames, ids, effective)
    asyncio.run(run())
