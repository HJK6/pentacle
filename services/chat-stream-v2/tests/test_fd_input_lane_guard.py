"""D4 regression: pane input and sends to the FD must not be refused as lane execution.

The FD's own assigned spec can be a member of an active lane (close custody). Physical
input admission (linked_only) to the authenticated FD is operator traffic, while
assigning lane execution to the FD stays refused.
"""
from __future__ import annotations

import pytest

from sessions import VerbError
from store import Store
from test_work_lane_assignment import HOST, item, lane, seat


@pytest.fixture
def store():
    value = Store(":memory:")
    value.start()
    yield value
    value.stop()


async def fd_holding_lane_member(store, tmp_path):
    target = item(tmp_path / "memory", "demo__close_custody")
    await lane(store, "wl-defects", target)
    fd = await seat(store, "fd", target, assigned=target)
    await store.submit(lambda conn: (
        conn.execute("INSERT INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) "
                     "VALUES ('assistant',?,?,1,'2026-11-10T00:00:00Z')", fd), conn.commit()))
    return target, fd


@pytest.mark.asyncio
async def test_input_to_fd_with_assigned_lane_member_is_admitted(store, tmp_path):
    target, fd = await fd_holding_lane_member(store, tmp_path)
    assert await store.require_lane_execution(target, target_stream_id=fd[0], linked_only=True) is None


@pytest.mark.asyncio
async def test_send_to_fd_with_assigned_lane_member_is_claimed(store, tmp_path):
    _target, fd = await fd_holding_lane_member(store, tmp_path)
    claim = await store.claim_or_coalesce_send(
        to_stream_id=fd[0], request_id="req-fd-1", receipt_id="rcpt-fd-1", wire_text="operator dispatch",
        display_text="operator dispatch", attachments=[], optimistic_id=None, window_floor="2026-11-10T00:00:00Z")
    assert claim["coalesced"] is False and claim["receipt"]["state"] == "accepted"


@pytest.mark.asyncio
async def test_assigning_lane_execution_to_fd_stays_forbidden(store, tmp_path):
    target, fd = await fd_holding_lane_member(store, tmp_path)
    with pytest.raises(VerbError) as exc:
        await store.require_lane_execution(target, target_stream_id=fd[0])
    assert exc.value.code == "work_lane_fd_execution_forbidden"


@pytest.mark.asyncio
async def test_input_to_non_fd_seat_in_paused_lane_still_refused(store, tmp_path):
    target = item(tmp_path / "memory", "demo__paused")
    await lane(store, "wl-paused", target, state="paused")
    worker = await seat(store, "worker", target, assigned=target, role="worker")
    with pytest.raises(VerbError) as exc:
        await store.require_lane_execution(target, target_stream_id=worker[0], linked_only=True)
    assert exc.value.code == "work_lane_not_active"
