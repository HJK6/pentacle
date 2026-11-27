"""Reconciliation wakes and deadlines through the assistant ruling verbs."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

import assistant_lane_rulings as rulings_module
from store import Store
from test_assistant_lane_rulings import ROOT, _loss_notices, _rulings_rig, _spawn_msg

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("ruling_spec_root")]


class ManualTimeouts:
    def __init__(self):
        self.waits = asyncio.Queue()

    def __getattr__(self, name):
        return getattr(asyncio, name)

    @asynccontextmanager
    async def timeout(self, delay):
        async with asyncio.timeout(None) as timeout:
            self.waits.put_nowait((delay, timeout))
            yield

    async def sleep(self, delay):
        # Also drive the original polling loop when running against main.
        try:
            async with self.timeout(delay):
                await asyncio.Event().wait()
        except TimeoutError:
            pass

    async def next_wait(self):
        return await asyncio.wait_for(self.waits.get(), timeout=3)


@asynccontextmanager
async def running_rig(monkeypatch):
    timeouts = ManualTimeouts()
    monkeypatch.setattr(rulings_module, "asyncio", timeouts)
    store = Store(":memory:")
    store.start()
    try:
        root, _advisor, server, _spawned = await _rulings_rig(store)
        try:
            yield root, server, timeouts
        finally:
            await server.lane_rulings.stop()
    finally:
        store.stop()


async def test_new_request_wakes_rulings_before_idle_timeout(monkeypatch):
    async with running_rig(monkeypatch) as (root, server, timeouts):
        rulings = server.lane_rulings
        tick = AsyncMock(wraps=rulings.tick)
        monkeypatch.setattr(rulings, "tick", tick)
        await rulings.start()
        tick.assert_awaited_once()
        await timeouts.next_wait()

        reply = await rulings.request_spawn(_spawn_msg(root, "wake-ruling"))
        assert reply["type"] == "spawn.pending_ruling"
        # The idle timeout is still held. Only the request can start this tick.
        delay, _ = await timeouts.next_wait()
        assert delay == 1
        assert tick.await_count == 2


async def test_next_idle_check_detects_authority_loss(monkeypatch):
    async with running_rig(monkeypatch) as (_root, server, timeouts):
        await server.lane_rulings.start()
        delay, timeout = await timeouts.next_wait()
        assert await _loss_notices(server.store) == []

        await server.store.update_session("fixture-advisor", "astra", pane_status="pane_dead")
        timeout.reschedule(asyncio.get_running_loop().time())
        next_delay, _ = await timeouts.next_wait()

        notices = await _loss_notices(server.store)
        assert len(notices) == 1 and notices[0]["recipient_stream_id"] == ROOT
        assert delay == next_delay == 15


async def test_pending_request_expires_on_timeout_without_ruling(monkeypatch):
    clock = Mock(wraps=rulings_module.time)
    clock.time.return_value = rulings_module.time.time()
    monkeypatch.setattr(rulings_module, "time", clock)
    async with running_rig(monkeypatch) as (root, server, timeouts):
        rulings = server.lane_rulings
        target = "fixture-chat:closing"
        lane = await server.store.open_session("fixture-chat", "closing", provider="codex",
                                               role="lead", parent_stream_id=ROOT)
        await rulings._record_ownership({"requester_stream_id": ROOT,
            "requester_generation": root["session_generation"], "ruling_request_id": "earlier"},
            target, lane["session_generation"])
        close = AsyncMock(return_value={"already_closed": False, "session": lane, "reap_status": "done"})
        monkeypatch.setattr(server.sessions, "close", close)
        auth = {"token_verified": True, "stream_id": ROOT, "session_generation": root["session_generation"]}
        held = await rulings.request_close({"type": "close", "host": "fixture-chat",
            "session_name": "closing", "request_id": "close-overdue"}, target_stream_id=target,
            target_generation=lane["session_generation"], auth=auth)
        assert held["type"] == "close.pending_ruling"

        # Restart with the pending request already persisted.
        await rulings.stop()
        rulings = server.lane_rulings = rulings_module.AssistantLaneRulings(server)
        await rulings.start()
        delay, timeout = await timeouts.next_wait()
        assert delay == 1
        pending = await rulings.latest_for_target(target)
        assert pending["state"] == "pending"
        close.assert_not_awaited()

        clock.time.return_value = pending["deadline"]
        timeout.reschedule(asyncio.get_running_loop().time())
        delay, timeout = await timeouts.next_wait()

        expired = await rulings.latest_for_target(target)
        assert (expired["state"], expired["reason"], expired["ruling"]) == ("done", "deadline", None)
        close.assert_awaited_once()
        if delay == 1:  # The expiry tick may still carry its pending snapshot.
            timeout.reschedule(asyncio.get_running_loop().time())
            delay, _ = await timeouts.next_wait()
        assert delay == 15
