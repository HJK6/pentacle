"""The capacity registry refresh runs after the chat event broadcast.

Capacity is still reduced inside the append; only the registry copy moved.
A refresh failure is logged: the batch stays claimed and its frames sent,
and the reconcile tick repairs the registry from committed state.
"""
from __future__ import annotations

import asyncio

import pytest

from sessions import Sessions
from test_event_push import CURRENT_CODEX_ROLLOUT, _proven_codex_ev, _push, _sink
from test_ingest import FIXTURES, _StreamIngest, _codex_ingest, _codex_row, _open_store

ROLLOUT = FIXTURES / "codex_rollout_first_turn.jsonl"
REMOTE_HOST, REMOTE_NAME = "fixture-remote", "v2-remote-seat"
REMOTE = f"{REMOTE_HOST}:{REMOTE_NAME}"


class Window:
    """Writer submissions after the durable append and before the first chat.event."""

    def __init__(self, store, frames):
        self.frames, self.open, self.names, self.closed_at = frames, False, [], None
        submit, append = store.submit, store.append_session_events_lifecycle_cas

        async def counting_submit(fn, **kwargs):
            if self.open:
                self.names.append(getattr(fn, "__qualname__", repr(fn)))
            return await submit(fn, **kwargs)

        async def marked_append(*args, **kwargs):
            result = await append(*args, **kwargs)
            self.open = True
            return result

        store.submit, store.append_session_events_lifecycle_cas = counting_submit, marked_append

    async def broadcast(self, frame):
        if frame.get("type") == "chat.event" and self.closed_at is None:
            self.closed_at = len(self.names)
        self.frames.append(frame)

    def before_broadcast(self):
        assert self.closed_at is not None, "no chat.event was broadcast"
        return self.names[:self.closed_at]


async def _remote(store, *, sessions=True):
    await store.open_session(REMOTE_HOST, REMOTE_NAME, visibility="visible",
                             provider="codex", pane_pid="8123")
    registry = None
    if sessions:
        registry = Sessions(store, local_host="hostd")
        await registry.refresh()
    ep, frames, _ = _sink(store, sessions=registry)
    return ep, frames


def _failing_refresh(monkeypatch, calls):
    async def fail(store, sessions, sid):
        calls.append(sid)
        raise OSError("forced capacity refresh failure")
    monkeypatch.setattr("store_capacity_retry.refresh_projection", fail)


@pytest.mark.timeout(20)
def test_event_push_broadcasts_before_the_capacity_refresh():
    async def go():
        store = _open_store()
        try:
            ep, frames = await _remote(store)
            window = Window(store, frames)
            ep.broadcast = window.broadcast
            result = await ep.handle_push(_push(ep, [_proven_codex_ev(CURRENT_CODEX_ROLLOUT, 1, stream_id=REMOTE)], host=REMOTE_HOST))
            assert result["type"] == "event.push.ok" and result["inserted"] == 1
            # Only the event projection read remains; the capacity fetch_session moved after.
            assert window.before_broadcast() == ["Store.project_session_events.<locals>.<lambda>"]
            assert any("fetch_session" in name for name in window.names[window.closed_at:])
        finally:
            store.stop()
    asyncio.run(go())


@pytest.mark.timeout(20)
def test_local_codex_ingest_broadcasts_before_the_capacity_refresh():
    async def go():
        store = _open_store()
        frames = []
        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            row = await _codex_row(store, ROLLOUT)
            window = Window(store, frames)
            ingest = _codex_ingest(store, window.broadcast)
            assert await ingest._ingest_stream(row, _StreamIngest(), 500) > 0
            assert window.before_broadcast() == ["Store.project_session_events.<locals>.<lambda>"]
            assert any("fetch_session" in name for name in window.names[window.closed_at:])
        finally:
            store.stop()
    asyncio.run(go())


@pytest.mark.timeout(20)
def test_local_refresh_failure_keeps_the_offset_and_the_frames(monkeypatch):
    async def go():
        store = _open_store()
        frames, calls = [], []
        try:
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            _failing_refresh(monkeypatch, calls)

            async def broadcast(frame):
                frames.append(frame)
            state = _StreamIngest()
            ingest = _codex_ingest(store, broadcast)
            appended = await ingest._ingest_stream(await _codex_row(store, ROLLOUT), state, 500)
            assert calls == ["h:v2-codex"]
            assert appended > 0 and state.offset > 0, "a refresh failure must not hold the position"
            assert sum(frame["type"] == "chat.event" for frame in frames) == appended
        finally:
            store.stop()
    asyncio.run(go())


@pytest.mark.timeout(20)
def test_remote_refresh_failure_still_acknowledges_the_push(monkeypatch):
    async def go():
        store = _open_store()
        calls = []
        try:
            ep, frames = await _remote(store)
            _failing_refresh(monkeypatch, calls)
            result = await ep.handle_push(_push(ep, [_proven_codex_ev(CURRENT_CODEX_ROLLOUT, 1, stream_id=REMOTE)], host=REMOTE_HOST))
            assert calls == [REMOTE]
            assert result["type"] == "event.push.ok" and result["inserted"] == 1
            assert [frame["type"] for frame in frames].count("chat.event") == 1
        finally:
            store.stop()
    asyncio.run(go())


@pytest.mark.timeout(20)
def test_registry_refresh_still_precedes_the_inventory_emit(monkeypatch):
    """Guard: the inventory frame after a batch carries the committed capacity."""
    async def go():
        store = _open_store()
        try:
            await store.open_session(REMOTE_HOST, REMOTE_NAME, visibility="visible",
                                     provider="codex", pane_pid="8123")
            registry = Sessions(store, local_host="hostd")
            await registry.refresh()
            ep, frames, _ = _sink(store, sessions=registry, emit_inventory=True)
            import store_capacity_retry
            real = store_capacity_retry.refresh_projection

            async def marked(store_, sessions, sid):
                frames.append({"type": "capacity.refresh"})
                await real(store_, sessions, sid)
            monkeypatch.setattr("store_capacity_retry.refresh_projection", marked)
            result = await ep.handle_push(_push(ep, [_proven_codex_ev(CURRENT_CODEX_ROLLOUT, 1, stream_id=REMOTE)], host=REMOTE_HOST))
            assert result["type"] == "event.push.ok"
            order = [frame["type"] for frame in frames]
            assert order.index("chat.event") < order.index("capacity.refresh")
            assert order.index("capacity.refresh") < order.index("session.inventory")
        finally:
            store.stop()
    asyncio.run(go())
