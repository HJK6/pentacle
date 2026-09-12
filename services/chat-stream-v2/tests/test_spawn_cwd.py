"""Fail-first source-level proof for spawn cwd validation and ordering."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import tmux_transport
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store


LOCAL = "localhost"
REMOTE = "target-host"


def test_local_tmux_cwd_exists_distinguishes_directory_and_file(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("marker", encoding="utf-8")
    tmux = tmux_transport.Tmux()

    async def run() -> tuple[bool, bool]:
        return await tmux.cwd_exists(str(tmp_path)), await tmux.cwd_exists(str(file_path))

    directory, regular_file = asyncio.run(run())
    assert directory is True
    assert regular_file is False


class CwdProbeTmux:
    def __init__(self, *, exists: bool) -> None:
        self.exists = exists
        self.cwd_checks: list[str] = []
        self.new_sessions: list[tuple[str, str, str | None]] = []

    async def cwd_exists(self, cwd: str) -> bool:
        self.cwd_checks.append(cwd)
        return self.exists

    async def new_session(
        self, name: str, command: str, cwd: str | None = None, env=None,
    ) -> None:
        self.new_sessions.append((name, command, cwd))


class TargetHosts:
    local_host = LOCAL

    def __init__(self, tmux: CwdProbeTmux) -> None:
        self.tmux = tmux
        self.reachable: list[tuple[str, str]] = []

    async def ensure_reachable(self, host: str, what: str) -> None:
        self.reachable.append((host, what))

    def tmux_for(self, host: str) -> CwdProbeTmux:
        assert host == REMOTE
        return self.tmux


def _message(cwd: str, *, host: str = LOCAL) -> dict[str, object]:
    return {
        "type": "spawn",
        "objective": "Exercise the existing spawn contract",
        "command": "provider",
        "host": host,
        "session_name": "cwd-ordering",
        "request_id": "cwd-ordering-request",
        "cwd": cwd,
    }


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    tmux: CwdProbeTmux,
    *,
    hosts: TargetHosts | None = None,
) -> tuple[Store, SpawnCtl, list[dict[str, object]]]:
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=LOCAL)
    ctl = SpawnCtl(store, sessions, tmux=tmux, hosts=hosts)

    async def resolve_launch(_msg, _host, _name):
        return "provider", {}, {}

    ctl._resolve_launch = resolve_launch
    reservations: list[dict[str, object]] = []

    async def reserve(*args, **kwargs):
        reservations.append({"args": args, "kwargs": kwargs})
        return False

    monkeypatch.setattr(store, "reserve_stream_id", reserve)
    return store, ctl, reservations


@pytest.mark.parametrize("directory_kind", ["missing", "file"])
def test_invalid_cwd_fails_before_reservation_open_or_new_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, directory_kind: str,
) -> None:
    candidate = tmp_path / ("missing" if directory_kind == "missing" else "not-a-directory")
    if directory_kind == "file":
        candidate.write_text("marker", encoding="utf-8")
    tmux = CwdProbeTmux(exists=False)
    store, ctl, reservations = _controller(monkeypatch, tmux)

    async def run() -> tuple[VerbError, dict | None]:
        try:
            with pytest.raises(VerbError) as raised:
                await ctl._spawn_impl(_message(str(candidate)), LOCAL)
            row = await store.fetch_session(LOCAL, "cwd-ordering")
            return raised.value, row
        finally:
            store.stop()

    error, row = asyncio.run(run())
    assert error.code == "invalid_cwd"
    assert reservations == []
    assert tmux.cwd_checks == [str(candidate)]
    assert tmux.new_sessions == []
    assert row is None


def test_remote_invalid_cwd_checks_target_transport_before_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    candidate = str(tmp_path / "remote-missing")
    tmux = CwdProbeTmux(exists=False)
    hosts = TargetHosts(tmux)
    store, ctl, reservations = _controller(monkeypatch, tmux, hosts=hosts)

    async def run() -> VerbError:
        try:
            with pytest.raises(VerbError) as raised:
                await ctl._spawn_impl(_message(candidate, host=REMOTE), LOCAL)
            return raised.value
        finally:
            store.stop()

    error = asyncio.run(run())
    assert error.code == "invalid_cwd"
    assert hosts.reachable == [(REMOTE, "spawn")]
    assert tmux.cwd_checks == [candidate]
    assert reservations == []
    assert tmux.new_sessions == []
