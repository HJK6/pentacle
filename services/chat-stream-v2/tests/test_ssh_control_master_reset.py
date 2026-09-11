from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

import hosts as hosts_module
import machines
from hosts import Hosts, HostsConfig
from machines import MachineConfig


class FakeSSH:
    def __init__(self, root: Path) -> None:
        self.target = "user@peer"
        self.state = root / "state.json"
        self.events_file = root / "events.jsonl"
        self.bin = root / "ssh"
        self.control_dir = Path(tempfile.mkdtemp(prefix="pentacle-cm-min-", dir="/tmp"))
        self.state.write_text(json.dumps({
            "fresh_rc": 0, "mux": "nonzero", "exit": "success", "pid": 4242,
            "check": "master", "auto_create": "ok",
        }))
        self.bin.write_text(textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys, time
            from pathlib import Path

            state_path = Path(os.environ["FAKE_SSH_STATE"])
            events_path = Path(os.environ["FAKE_SSH_EVENTS"])
            state = json.loads(state_path.read_text())
            args = sys.argv[1:]
            with events_path.open("a") as fh:
                fh.write(json.dumps(args) + "\\n")
            path = args[args.index("-S") + 1] if "-S" in args else None
            for arg in args:
                if arg.startswith("ControlPath="):
                    path = arg.split("=", 1)[1]
            operation = args[args.index("-O") + 1] if "-O" in args else None
            if operation == "check":
                if state.get("check") == "nomaster":
                    print("Control socket connect: No such file", file=sys.stderr)
                    raise SystemExit(255)
                print(f"Master running (pid={state['pid']})", file=sys.stderr)
                raise SystemExit(0)
            if operation == "exit":
                if state["exit"] == "timeout":
                    time.sleep(60)
                if state["exit"] == "nomaster":
                    raise SystemExit(255)
                raise SystemExit(0 if state["exit"] == "success" else 1)
            # Fresh reachability leg: ControlMaster=no with ControlPath=none.
            if path in (None, "none"):
                raise SystemExit(state["fresh_rc"])
            exists = Path(path).exists()
            if "ControlMaster=no" in args:
                # Attach-only health leg: drive an existing master, else connect
                # directly. It NEVER creates a master.
                if exists:
                    if state["mux"] == "timeout":
                        time.sleep(60)
                    if state["mux"] == "nonzero":
                        raise SystemExit(255)
                    raise SystemExit(0)
                raise SystemExit(state["fresh_rc"])
            # ControlMaster=auto leg (real verbs, and the OLD probe mux leg):
            # use an existing master, else CREATE one.
            if exists:
                if state["mux"] == "timeout":
                    time.sleep(60)
                if state["mux"] == "nonzero":
                    raise SystemExit(255)
                raise SystemExit(0)
            if state.get("auto_create") == "timeout":
                time.sleep(60)  # the production churn: create+connect hangs
            Path(path).touch()
            state["mux"] = "healthy"
            state_path.write_text(json.dumps(state))
            raise SystemExit(0)
            """
        ))
        self.bin.chmod(0o755)

    def configure(self, **changes: object) -> None:
        state = json.loads(self.state.read_text())
        state.update(changes)
        self.state.write_text(json.dumps(state))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setenv("FAKE_SSH_STATE", str(self.state))
        monkeypatch.setenv("FAKE_SSH_EVENTS", str(self.events_file))
        monkeypatch.setenv("PENTACLE_SSH_CONTROL_DIR", str(self.control_dir))
        path = machines.ssh_control_path(self.target)
        assert path is not None
        path.touch()
        return path

    def events(self) -> list[list[str]]:
        if not self.events_file.exists():
            return []
        return [json.loads(line) for line in self.events_file.read_text().splitlines()]


@pytest.fixture()
def fake_ssh(tmp_path: Path) -> FakeSSH:
    fake = FakeSSH(tmp_path)
    try:
        yield fake
    finally:
        shutil.rmtree(fake.control_dir, ignore_errors=True)


def _hosts(fake: FakeSSH) -> Hosts:
    peer = MachineConfig(name="peer", ssh_target=fake.target, tmux_bin="tmux")
    return Hosts(
        "local", {"peer": peer}, ssh_bin=str(fake.bin),
        # The executable fixture starts a Python interpreter for each leg.
        # Give it scheduling headroom; timeout/reset decisions have explicit
        # deterministic transport cases below, independent of host load.
        config=HostsConfig(probe_timeout_s=2.0, breaker_threshold=3),
    )


def _install_deterministic_probe_runner(
    fake: FakeSSH,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mux: tuple[int, str, bool],
    control_exit: tuple[int, str, bool],
    pid: int,
) -> list[list[str]]:
    """Replace only the two probe legs whose 0.3 s wall clock used to flake.

    The bounded control-operation contract remains explicit: the runner returns
    the same `(rc, output, timed_out)` tuples that `_bounded_exec` produces,
    records the exact argv, and lets the reset assertions prove the exit,
    unlink, and subsequent auto-master rebuild. Other tests retain the real
    subprocess fixture, including the actual kill/reap path.
    """
    path = machines.ssh_control_path(fake.target)
    assert path is not None
    calls: list[list[str]] = []
    real_exec = hosts_module._exec

    async def deterministic_exec(*args: str, timeout: float) -> tuple[int, str]:
        calls.append(list(args))
        if "ControlPath=none" in args and args[-1] == "true":
            return 0, ""
        # The post-reset run_command still uses the real fake SSH executable so
        # its event and socket assertions cover the rebuild path unchanged.
        return await real_exec(*args, timeout=timeout)

    async def deterministic_bounded_exec(*args: str, timeout: float) -> tuple[int, str, bool]:
        del timeout
        calls.append(list(args))
        if "-O" in args:
            operation = args[args.index("-O") + 1]
            if operation == "check":
                return 0, f"Master running (pid={pid})\n", False
            if operation == "exit":
                return control_exit
        if "ControlMaster=no" in args:
            return mux
        raise AssertionError(f"unexpected bounded probe argv: {args!r}")

    monkeypatch.setattr(hosts_module, "_exec", deterministic_exec)
    monkeypatch.setattr(hosts_module, "_bounded_exec", deterministic_bounded_exec)
    return calls


def _track_unlinks(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []
    real_unlink = hosts_module._unlink_control_socket

    def track(path: Path) -> None:
        calls.append(path)
        real_unlink(path)

    monkeypatch.setattr(hosts_module, "_unlink_control_socket", track)
    return calls


def _exit_events(fake: FakeSSH) -> list[list[str]]:
    return [event for event in fake.events() if "-O" in event and event[event.index("-O") + 1] == "exit"]


def _mux_leg_events(fake: FakeSSH, path: Path) -> list[list[str]]:
    """The probe's mux HEALTH leg: a `true` command bound to the named socket
    (not the `-O` control ops, not the ControlPath=none fresh leg)."""
    return [
        event for event in fake.events()
        if "-O" not in event and event[-1] == "true" and f"ControlPath={path}" in event
    ]


def test_check_failure_resets_even_when_session_would_succeed(
    fake_ssh: FakeSSH, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    path = fake_ssh.install(monkeypatch)
    fake_ssh.configure(check="nomaster", mux="healthy")
    unlinks = _track_unlinks(monkeypatch)
    hosts = _hosts(fake_ssh)

    assert asyncio.run(hosts.probe_once("peer")) is True
    assert _exit_events(fake_ssh) and len(_exit_events(fake_ssh)) == 1
    assert unlinks == [path] and not path.exists()
    assert _mux_leg_events(fake_ssh, path) == []
    reset_logs = [r.getMessage() for r in caplog.records if "SSH ControlMaster reset" in r.getMessage()]
    assert len(reset_logs) == 1 and "control_check=rc_255" in reset_logs[0]


def test_fresh_ok_mux_failure_resets_exact_socket_and_next_call_rebuilds(
    fake_ssh: FakeSSH, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = fake_ssh.install(monkeypatch)
    assert machines.ssh_control_path(fake_ssh.target) == path
    assert machines.ssh_control_path("other@peer") != path
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert f"ControlPath={path}" in machines.ssh_tmux_command(
        fake_ssh.target, "tmux", ("has-session", "-t", "=x:"),
    )
    fake_ssh.configure(mux="timeout")
    unlinks = _track_unlinks(monkeypatch)
    hosts = _hosts(fake_ssh)

    assert asyncio.run(hosts.probe_once("peer")) is True
    state = hosts._state["peer"]
    assert state.online is True and state.consecutive_failures == 0
    assert state.breaker_open is False
    assert unlinks == [path] and not path.exists()
    exits = _exit_events(fake_ssh)
    assert len(exits) == 1 and exits[0][exits[0].index("-S") + 1] == str(path)

    assert asyncio.run(hosts.run_command("peer", "true")) == (0, "")
    assert path.exists()
    mux_events = [event for event in fake_ssh.events() if f"ControlPath={path}" in event]
    assert len(mux_events) == 2

    # The probe HEALTH leg must attach-only (ControlMaster=no on the named
    # path); only the later run_command rebuild uses ControlMaster=auto. A stub
    # that keeps auto on the health leg would churn a throwaway master the reset
    # can't address, and fails these counts.
    legs = _mux_leg_events(fake_ssh, path)
    health = [e for e in legs if "ControlMaster=no" in e]
    rebuild = [e for e in legs if "ControlMaster=auto" in e]
    assert len(health) == 1 and f"ControlPath={path}" in health[0]
    assert len(rebuild) == 1

    monkeypatch.setenv("PENTACLE_SSH_CONTROL_DIR", "/tmp/" + "x" * 120)
    assert machines.ssh_control_path(fake_ssh.target) is None
    assert not any(
        arg.startswith("ControlPath=")
        for arg in machines.ssh_command(fake_ssh.target, "true")
    )


def test_down_host_records_only_fresh_health_and_never_resets(
    fake_ssh: FakeSSH, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = fake_ssh.install(monkeypatch)
    fake_ssh.configure(fresh_rc=255, mux="nonzero")
    unlinks = _track_unlinks(monkeypatch)
    hosts = _hosts(fake_ssh)
    calls: list[list[str]] = []

    async def down_peer(*args: str, timeout: float) -> tuple[int, str]:
        # This test measures breaker and no-reset decisions. A Python fixture
        # can miss the 0.3s subprocess deadline before writing its event; make
        # the peer's failed result deterministic at the transport boundary.
        calls.append(list(args))
        assert "ControlPath=none" in args and args[-1] == "true"
        return 255, "peer offline"

    monkeypatch.setattr(hosts_module, "_exec", down_peer)

    assert asyncio.run(hosts.probe_once("peer")) is False
    assert hosts._state["peer"].consecutive_failures == 1
    assert hosts._state["peer"].breaker_open is False
    assert [asyncio.run(hosts.probe_once("peer")) for _ in range(2)] == [False, False]
    state = hosts._state["peer"]
    assert state.online is False and state.consecutive_failures == 3
    assert state.breaker_open is True
    assert unlinks == [] and path.exists()
    assert _exit_events(fake_ssh) == []
    mux_events = [event for event in fake_ssh.events() if f"ControlPath={path}" in event]
    assert mux_events == []
    assert len(calls) == 3
    assert fake_ssh.events() == []  # no multiplexed/control subprocess at all
    assert all("ControlMaster=no" in event for event in calls)


def test_control_exit_timeout_still_unlinks_within_bound_and_rebuilds(
    fake_ssh: FakeSSH,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = fake_ssh.install(monkeypatch)
    calls = _install_deterministic_probe_runner(
        fake_ssh, monkeypatch,
        mux=(255, "", False), control_exit=(1, "", True),
        pid=4242,
    )
    unlinks = _track_unlinks(monkeypatch)
    hosts = _hosts(fake_ssh)

    started = time.monotonic()
    assert asyncio.run(hosts.probe_once("peer")) is True
    assert time.monotonic() - started < 1.2
    state = hosts._state["peer"]
    assert state.online is True and state.consecutive_failures == 0
    assert state.breaker_open is False
    assert unlinks == [path] and not path.exists()
    exits = [event for event in calls if "-O" in event and event[event.index("-O") + 1] == "exit"]
    assert len(exits) == 1 and exits[0][exits[0].index("-S") + 1] == str(path)
    reset_logs = [r.getMessage() for r in caplog.records if "SSH ControlMaster reset" in r.getMessage()]
    assert len(reset_logs) == 1
    assert f"socket={path}" in reset_logs[0]
    assert "mux_failure=rc_255" in reset_logs[0]
    assert "master_pid=4242" in reset_logs[0]
    assert "control_exit=timeout" in reset_logs[0] and "unlinked=True" in reset_logs[0]

    assert asyncio.run(hosts.run_command("peer", "true")) == (0, "")
    assert path.exists()


def test_present_wedged_master_answers_check_but_hangs_sessions_is_killed(
    fake_ssh: FakeSSH,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The production wedge shape: a master answers `-O check` ("Master
    running") yet every session through it hangs. The attach-only health leg
    reaches that master, times out, and the bounded `-O exit` actually kills it
    (control_exit=rc_0, a real master_pid) — not the hostc `rc_255`/`pid=None`
    no-op the old auto leg produced when it had churned the master away."""
    path = fake_ssh.install(monkeypatch)
    calls = _install_deterministic_probe_runner(
        fake_ssh, monkeypatch,
        mux=(1, "", True), control_exit=(0, "", False),
        pid=50887,
    )
    unlinks = _track_unlinks(monkeypatch)
    hosts = _hosts(fake_ssh)

    assert asyncio.run(hosts.probe_once("peer")) is True
    state = hosts._state["peer"]
    assert state.online is True and state.consecutive_failures == 0 and state.breaker_open is False
    legs = [
        event for event in calls
        if "-O" not in event and event[-1] == "true" and f"ControlPath={path}" in event
    ]
    assert len(legs) == 1 and "ControlMaster=no" in legs[0] and "ControlMaster=auto" not in legs[0]
    exits = [event for event in calls if "-O" in event and event[event.index("-O") + 1] == "exit"]
    assert len(exits) == 1 and exits[0][exits[0].index("-S") + 1] == str(path)
    assert unlinks == [path] and not path.exists()
    reset_logs = [r.getMessage() for r in caplog.records if "SSH ControlMaster reset" in r.getMessage()]
    assert len(reset_logs) == 1
    assert "mux_failure=timeout" in reset_logs[0]
    assert "master_pid=50887" in reset_logs[0]
    assert "control_exit=rc_0" in reset_logs[0]


def test_no_persistent_master_reachable_host_does_not_churn_or_falsely_reset(
    fake_ssh: FakeSSH, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No master at the path + reachable host: the attach-only health leg
    connects directly (never creating a master) and succeeds, so NO reset fires.
    Regression — the old auto health leg would instead create+hang on the fresh
    connect (`auto_create=timeout`), time out, and fire a `-O exit` against a
    master that no longer exists (the hostc rc_255 churn)."""
    path = fake_ssh.install(monkeypatch)
    path.unlink()  # no persistent master occupies the deterministic path
    # If the health leg were ControlMaster=auto it would create+hang here.
    fake_ssh.configure(fresh_rc=0, auto_create="timeout", check="nomaster", exit="nomaster")
    unlinks = _track_unlinks(monkeypatch)
    hosts = _hosts(fake_ssh)

    assert asyncio.run(hosts.probe_once("peer")) is True
    state = hosts._state["peer"]
    assert state.online is True and state.consecutive_failures == 0 and state.breaker_open is False
    legs = _mux_leg_events(fake_ssh, path)
    assert len(legs) == 1 and "ControlMaster=no" in legs[0] and "ControlMaster=auto" not in legs[0]
    assert _exit_events(fake_ssh) == []  # no reset attempted
    assert unlinks == [] and not path.exists()  # nothing churned into existence
