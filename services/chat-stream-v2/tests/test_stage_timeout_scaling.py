"""Stage-transfer timeout scaling for small prompts and large binary fixtures.

The tests pin a size-aware deadline and distinguishable transfer failure
reasons without requiring a live peer.
"""
from __future__ import annotations

import tmux_transport

import asyncio

import pytest

from tmux_transport import Tmux
from sessions import VerbError

MB = 1024 * 1024


def test_stage_timeout_floor_for_small_prompts():
    # A small prompt brief keeps the 10s floor.
    assert tmux_transport._stage_timeout(4096) == pytest.approx(10.0)
    assert tmux_transport._stage_timeout(0) == pytest.approx(10.0)


def test_stage_timeout_scales_for_multi_mb_attachment():
    # A representative 6MB fixture must get well more than 10s.
    t6 = tmux_transport._stage_timeout(6 * MB)
    assert t6 > 10.0
    assert t6 >= 60.0  # generous enough to clear a slow peer transport transfer
    # Monotonic in size, and bounded by the ceiling.
    assert tmux_transport._stage_timeout(2 * MB) < t6
    assert tmux_transport._stage_timeout(500 * MB) <= tmux_transport.STAGE_TIMEOUT_CEILING_S


def _make_remote_tmux():
    return Tmux(ssh_target="user@example.local", ssh_bin="ssh", connect_timeout=5.0)


def test_stage_text_wires_scaled_timeout_for_large_payload(monkeypatch):
    """The scaled deadline actually reaches asyncio.wait_for (not a hardcoded 10)."""
    captured = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            return (b"", b"")

    async def _fake_exec(*a, **k):
        return _FakeProc()

    async def _fake_wait_for(coro, timeout):
        captured["timeout"] = timeout
        return await coro

    monkeypatch.setattr(tmux_transport.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(tmux_transport.asyncio, "wait_for", _fake_wait_for)

    data = b"\x00" * (6 * MB)
    asyncio.run(_make_remote_tmux().stage_text("/tmp/public-test", data))

    assert captured["timeout"] == pytest.approx(tmux_transport._stage_timeout(len(data)))
    assert captured["timeout"] > 10.0


def test_stage_text_reports_slow_transfer_on_timeout(monkeypatch):
    """A transfer that exceeds the deadline yields a size-annotated timeout error."""
    class _FakeProc:
        returncode = None

        async def communicate(self, stdin=None):
            raise AssertionError("should be cancelled by wait_for")

    async def _fake_exec(*a, **k):
        return _FakeProc()

    async def _fake_wait_for(coro, timeout):
        coro.close()  # avoid 'never awaited' warning
        raise asyncio.TimeoutError

    async def _fake_reap(proc):
        return None

    monkeypatch.setattr(tmux_transport.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(tmux_transport.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(tmux_transport, "terminate_and_reap", _fake_reap)

    data = b"\x00" * (6 * MB)
    with pytest.raises(VerbError) as ei:
        asyncio.run(_make_remote_tmux().stage_text("/tmp/public-test", data))
    msg = str(ei.value)
    assert "timed out" in msg
    assert str(len(data)) in msg  # payload size is surfaced


@pytest.mark.parametrize(
    ("returncode", "detail", "unreachable"),
    [
        (255, "ssh transport failed without a known diagnostic", True),
        (1, "ssh: connect to host hostc port 22: Operation timed out", False),
    ],
)
def test_stage_text_classifies_ssh_exit_code_not_diagnostic(
    monkeypatch, returncode, detail, unreachable,
):
    """Only SSH exit 255 classifies a staging failure as unreachable."""
    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self, stdin=None):
            return (detail.encode(), b"")

    async def _fake_exec(*a, **k):
        return _FakeProc()

    async def _fake_wait_for(coro, timeout):
        return await coro

    monkeypatch.setattr(tmux_transport.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(tmux_transport.asyncio, "wait_for", _fake_wait_for)

    data = b"\x00" * 4096
    with pytest.raises(VerbError) as ei:
        asyncio.run(_make_remote_tmux().stage_text("/tmp/public-test", data))
    assert ("remote host unreachable" in str(ei.value).lower()) is unreachable
