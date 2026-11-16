"""D4 regression: pane input and sends to the FD must not be refused as lane execution.

The FD's assigned spec can be a lane member. Lane state and FD role do not
fence physical input or send receipt admission.
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
async def test_send_to_fd_with_assigned_lane_member_is_claimed(store, tmp_path):
    _target, fd = await fd_holding_lane_member(store, tmp_path)
    claim = await store.claim_or_coalesce_send(
        to_stream_id=fd[0], request_id="req-fd-1", receipt_id="rcpt-fd-1", wire_text="operator dispatch",
        display_text="operator dispatch", attachments=[], optimistic_id=None, window_floor="2026-11-10T00:00:00Z")
    assert claim["coalesced"] is False and claim["receipt"]["state"] == "accepted"


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
async def test_physical_input_to_former_fd_is_admitted(store, tmp_path):
    _target, fd = await fd_holding_lane_member(store, tmp_path, state="paused")
    other = await seat(store, "new-fd")
    await store.submit(lambda conn: (conn.execute(
        "UPDATE v2_assistant_direct_binding SET stream_id=?, generation=? WHERE name='assistant'", other), conn.commit()))
    assert await _physical_input(store, fd[0]) == "submitted"


@pytest.mark.asyncio
async def test_role_or_title_only_fd_is_admitted_without_exemption(store, tmp_path):
    target = item(tmp_path / "memory", "demo__fake")
    await lane(store, "wl-fake", target, state="paused")
    fake = await seat(store, "front-desk", target, assigned=target, role="front_desk", visibility="visible")
    assert await _physical_input(store, fake[0]) == "submitted"


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
