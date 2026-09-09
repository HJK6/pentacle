from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import hosts as hosts_module  # noqa: E402
from hosts import Hosts, HostsConfig  # noqa: E402
from machines import MachineConfig, ssh_command  # noqa: E402
from sessions import VerbError  # noqa: E402


def _peer(name: str, target: str = "user@example.local", tmux_bin: str = "tmux") -> MachineConfig:
    return MachineConfig(name=name, ssh_target=target, tmux_bin=tmux_bin)


def _hosts(**cfg) -> Hosts:
    return Hosts("localhost", {"hosta": _peer("hosta", "user@example.local", "/usr/bin/tmux")},
                 config=HostsConfig(breaker_threshold=3, backoff_base_s=5.0, backoff_cap_s=300.0, **cfg))


def test_binary_reachability_flips_online_and_offline() -> None:
    h = _hosts()
    assert h.snapshot()["hosta"]["host_status"] == "offline"  # unprobed = offline, not unknown
    assert h.snapshot()["hosta"]["host_status_reason"] == "pending_probe"
    assert h._record("hosta", True, 12) is True
    assert h.snapshot()["hosta"]["host_status"] == "online"
    assert h._record("hosta", False, 0) is False
    assert h.snapshot()["hosta"]["host_status"] == "degraded"
    assert h.snapshot()["hosta"]["host_status_reason"] == "unreachable_after_online"


def test_breaker_opens_after_threshold_and_backoff_grows() -> None:
    h = _hosts()
    for _ in range(2):
        h._record("hosta", False, 0)
    assert h._state["hosta"].breaker_open is False
    assert h._state["hosta"].reason == "unreachable"
    first_backoff = h._state["hosta"].next_probe_at
    h._record("hosta", False, 0)  # third failure trips it
    st = h._state["hosta"]
    assert st.breaker_open is True
    assert st.reason == "circuit_open"
    assert h.snapshot()["hosta"]["host_status"] == "offline"
    assert h.snapshot()["hosta"]["host_status_reason"] == "circuit_open"
    h._record("hosta", False, 0)
    assert h._state["hosta"].next_probe_at > first_backoff


def test_a_success_closes_the_breaker() -> None:
    h = _hosts()
    for _ in range(4):
        h._record("hosta", False, 0)
    assert h._state["hosta"].breaker_open is True
    assert h._record("hosta", True, 5) is True
    st = h._state["hosta"]
    assert st.breaker_open is False and st.consecutive_failures == 0 and st.reason == ""


def test_starved_shared_ssh_control_command_cannot_trip_probe_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy peer probe must not queue behind multiplexed SSH control work."""
    h = _hosts(probe_timeout_s=0.05)
    control_started = asyncio.Event()
    release_control = asyncio.Event()

    async def fake_exec(*args: str, timeout: float) -> tuple[int, str]:
        command = args[-1]
        isolated = "ControlPath=none" in args and "ControlMaster=no" in args
        if command != "true":
            control_started.set()
            await release_control.wait()
            return 0, "control-ok"
        return (0, "") if isolated else (1, "shared-transport-starved")

    async def healthy_mux(*_args: str, **_kwargs: object) -> tuple[int, str, bool]:
        return 0, "", False

    monkeypatch.setattr(hosts_module, "_exec", fake_exec)
    monkeypatch.setattr(hosts_module, "_bounded_exec", healthy_mux)

    async def go() -> None:
        control = asyncio.create_task(h.run_command("hosta", "control", "work"))
        await control_started.wait()
        assert [await h.probe_once("hosta") for _ in range(3)] == [True, True, True]
        assert h._state["hosta"].breaker_open is False
        release_control.set()
        assert await control == (0, "control-ok")

    asyncio.run(go())


def test_open_breaker_closes_on_next_success_and_announces_both_flips(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    outcomes = iter([(1, "") for _ in range(3)] + [(0, "")])
    frames: list[dict[str, object]] = []

    async def fake_exec(*_args: str, **_kwargs: object) -> tuple[int, str]:
        return next(outcomes)

    async def healthy_mux(*_args: str, **_kwargs: object) -> tuple[int, str, bool]:
        return 0, "", False

    async def on_status(payload: dict[str, object]) -> None:
        frames.append(payload)

    monkeypatch.setattr(hosts_module, "_exec", fake_exec)
    monkeypatch.setattr(hosts_module, "_bounded_exec", healthy_mux)
    h = Hosts(
        "localhost", {"hosta": _peer("hosta")},
        config=HostsConfig(breaker_threshold=3),
        on_status_change=on_status,
    )

    async def go() -> None:
        for _ in range(3):
            assert await h.probe_once("hosta") is False
        assert h._state["hosta"].breaker_open is True
        assert await h.probe_once("hosta") is True
        await asyncio.sleep(0)

    with caplog.at_level("INFO", logger="chat_streamd_v2.hosts"):
        asyncio.run(go())

    assert h.snapshot()["hosta"]["online"] is True
    assert h._state["hosta"].breaker_open is False
    assert any(frame["host_status_reason"] == "circuit_open" for frame in frames)
    assert frames[-1]["online"] is True
    assert "host probe breaker opened host=hosta" in caplog.text
    assert "host probe breaker closed host=hosta" in caplog.text
    assert "reason=successful_half_open_probe" in caplog.text


def test_ensure_reachable_fails_fast_when_breaker_open_without_probing() -> None:
    h = _hosts()
    for _ in range(3):
        h._record("hosta", False, 0)
    assert h._state["hosta"].breaker_open is True

    async def _go() -> None:
        # No SSH is attempted (the breaker IS the fast path) — a raise, not a hang.
        with pytest.raises(VerbError) as exc:
            await h.ensure_reachable("hosta", "spawn")
        assert exc.value.code == "host_offline"

    asyncio.run(_go())


def test_breaker_after_positive_evidence_is_named_degraded() -> None:
    h = _hosts()
    h._record("hosta", True, 5)
    for _ in range(3):
        h._record("hosta", False, 0)

    async def _go() -> None:
        with pytest.raises(VerbError) as exc:
            await h.ensure_reachable("hosta", "tell")
        assert exc.value.code == "host_degraded"

    asyncio.run(_go())
    assert h.snapshot()["hosta"]["host_status"] == "degraded"
    assert h.snapshot()["hosta"]["host_status_reason"] == "circuit_open_after_online"


def test_ensure_reachable_rejects_an_unknown_host() -> None:
    h = _hosts()

    async def _go() -> None:
        with pytest.raises(VerbError) as exc:
            await h.ensure_reachable("nobody", "tell")
        assert exc.value.code == "unsupported_host"

    asyncio.run(_go())


def test_local_host_is_always_reachable() -> None:
    h = _hosts()
    asyncio.run(h.ensure_reachable("localhost", "close"))  # no raise, no probe


def test_snapshot_always_carries_the_local_online_entry() -> None:
    h = _hosts()
    snap = h.snapshot()
    assert snap["localhost"]["online"] is True
    assert snap["localhost"]["host_status"] == "online"
    assert set(snap) == {"localhost", "hosta"}


def test_tmux_for_uses_the_peers_own_binary_and_ssh_target() -> None:
    h = _hosts()
    local = h.tmux_for("localhost")
    assert local.ssh_target is None
    peer = h.tmux_for("hosta")
    assert peer.ssh_target == "user@example.local"
    assert peer.bin == "/usr/bin/tmux"  # the PEER's tmux path, not the local one
    # And its argv is the lifted ssh construction driving the peer's tmux.
    argv = peer._argv(("has-session", "-t", "=x:"))
    assert argv[0] == "ssh" and "user@example.local" in argv
    assert argv[-1].startswith("/usr/bin/tmux has-session")


def test_peer_tmux_quotes_exact_target_for_zsh() -> None:
    """zsh treats an unquoted ``=name`` word as command-path expansion."""
    h = _hosts()
    argv = h.tmux_for("hosta")._argv(("has-session", "-t", "=x:"))
    assert "-t '=x:'" in argv[-1]


def test_ssh_command_drops_multiplexing_when_the_socket_path_would_overflow(tmp_path) -> None:
    long_dir = tmp_path / ("x" * 120)
    os.environ["EXAMPLE_SSH_CONTROL_DIR"] = str(long_dir)
    try:
        argv = ssh_command("user@example.local", "true")
        assert "ControlMaster=auto" not in argv  # multiplexing skipped, ssh still valid
        assert "BatchMode=yes" in argv
    finally:
        del os.environ["EXAMPLE_SSH_CONTROL_DIR"]
