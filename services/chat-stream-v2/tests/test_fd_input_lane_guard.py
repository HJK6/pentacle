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


async def fd_holding_lane_member(store, tmp_path, state="active"):
    target = item(tmp_path / "memory", "demo__close_custody")
    await lane(store, "wl-defects", target, state=state)
    fd = await seat(store, "fd", target, assigned=target)
    await store.submit(lambda conn: (
        conn.execute("INSERT INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) "
                     "VALUES ('assistant',?,?,1,'2026-11-10T00:00:00Z')", fd), conn.commit()))
    return target, fd


@pytest.mark.asyncio
async def test_input_to_fd_with_assigned_lane_member_is_admitted(store, tmp_path):
    target, fd = await fd_holding_lane_member(store, tmp_path)
    assert await store.require_lane_execution(target, target_stream_id=fd[0], linked_only=True, fd_input=True) is None


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
        await store.require_lane_execution(target, target_stream_id=worker[0], linked_only=True, fd_input=True)
    assert exc.value.code == "work_lane_not_active"


class _Sessions:
    def __init__(self):
        import asyncio
        self._graph_lock = asyncio.Lock()

    @staticmethod
    def split(target):
        host, name = target.split(":", 1)
        return host, name


def _comms(store, runtime=None):
    from types import SimpleNamespace
    return SimpleNamespace(store=store, sessions=_Sessions(), account_runtime=runtime)


async def _physical_input(store, target, runtime=None):
    from comms import Comms
    async with Comms._account_input_guard(_comms(store, runtime), target, execution=True):
        return "submitted"


@pytest.mark.asyncio
async def test_physical_input_to_fd_with_assigned_lane_member_passes_guard(store, tmp_path):
    _target, fd = await fd_holding_lane_member(store, tmp_path)
    assert await _physical_input(store, fd[0]) == "submitted"


@pytest.mark.asyncio
async def test_physical_input_to_fd_admitted_even_when_lane_paused(store, tmp_path):
    _target, fd = await fd_holding_lane_member(store, tmp_path, state="paused")
    assert await _physical_input(store, fd[0]) == "submitted"


@pytest.mark.asyncio
async def test_physical_input_to_former_fd_is_gated_again(store, tmp_path):
    _target, fd = await fd_holding_lane_member(store, tmp_path, state="paused")
    other = await seat(store, "new-fd")
    await store.submit(lambda conn: (conn.execute(
        "UPDATE v2_assistant_direct_binding SET stream_id=?, generation=? WHERE name='assistant'", other), conn.commit()))
    with pytest.raises(VerbError) as exc:
        await _physical_input(store, fd[0])
    assert exc.value.code == "work_lane_not_active"


@pytest.mark.asyncio
async def test_role_or_title_only_fd_gets_no_exemption(store, tmp_path):
    target = item(tmp_path / "memory", "demo__fake")
    await lane(store, "wl-fake", target, state="paused")
    fake = await seat(store, "front-desk", target, assigned=target, role="front_desk", visibility="visible")
    with pytest.raises(VerbError) as exc:
        await _physical_input(store, fake[0])
    assert exc.value.code == "work_lane_not_active"


@pytest.mark.asyncio
async def test_spawn_or_assignment_admission_for_fd_stays_forbidden(store, tmp_path):
    target, fd = await fd_holding_lane_member(store, tmp_path)
    for kwargs in ({"target_stream_id": fd[0]}, {"target_stream_id": fd[0], "linked_only": True}):
        with pytest.raises(VerbError) as exc:
            await store.require_lane_execution(target, **kwargs)
        assert exc.value.code == "work_lane_fd_execution_forbidden"


@pytest.mark.asyncio
async def test_repeated_send_to_fd_coalesces(store, tmp_path):
    _target, fd = await fd_holding_lane_member(store, tmp_path)
    args = dict(to_stream_id=fd[0], request_id="req-fd-dup", receipt_id="rcpt-fd-a", wire_text="hi", display_text="hi",
                attachments=[], optimistic_id=None, window_floor="2026-11-10T00:00:00Z")
    first = await store.claim_or_coalesce_send(**args)
    second = await store.claim_or_coalesce_send(**{**args, "receipt_id": "rcpt-fd-b"})
    assert first["coalesced"] is False and second["coalesced"] is True


@pytest.mark.asyncio
async def test_account_maintenance_still_refuses_input_to_fd(store, tmp_path):
    from contextlib import asynccontextmanager
    from provider_accounts import AccountError
    _target, fd = await fd_holding_lane_member(store, tmp_path)

    class Runtime:
        @asynccontextmanager
        async def input_guard(self, row, **kwargs):
            raise AccountError("provider_cli_input_maintenance")
            yield

    with pytest.raises(VerbError) as exc:
        await _physical_input(store, fd[0], Runtime())
    assert exc.value.code == "provider_cli_input_maintenance"
