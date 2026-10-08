"""Login-service units for the daemon: one rendered command, no accidental activation."""
from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .test_client_contract_probe import _process_group_popen_kwargs, terminate_process_group
from .test_deploy_script import deploy_mod

SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO = SERVICE_DIR.parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "install_daemon_service", SERVICE_DIR / "deploy" / "install_daemon_service.py",
)
installer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = installer
_SPEC.loader.exec_module(installer)


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture()
def release(tmp_path: Path) -> Path:
    checkout = tmp_path / "release"
    (checkout / installer.DAEMON).parent.mkdir(parents=True)
    (checkout / installer.DAEMON).write_text("")
    _exe(checkout / installer.VENV_PYTHON)
    return checkout


@pytest.fixture()
def argv(tmp_path: Path, release: Path) -> list[str]:
    return ["--release-checkout", str(release), "--tmux-bin", str(_exe(tmp_path / "tools/tmux"))]


@pytest.fixture()
def params(argv: list[str], home: Path):
    return _params(argv, home)


def _params(argv: list[str], home: Path):
    parser_args = _parse(argv)
    return installer.build_params(parser_args, home=home)


def _parse(argv: list[str]):
    import argparse

    parser = argparse.ArgumentParser()
    for flag in ("--release-checkout", "--python", "--tmux-bin", "--state-dir", "--spawn-cwd", "--local-host"):
        parser.add_argument(flag)
    parser.add_argument("--port", type=int, default=installer.DEFAULT_PORT)
    return parser.parse_args(argv)


class _Runner:
    """Records commands. `status` is what the service manager answers when asked
    for the service state, as `(exit code, stdout)`; other commands succeed."""

    def __init__(self, status: tuple[int, str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.status = status

    def __call__(self, command):
        self.calls.append(tuple(command))
        if command[:2] == ("launchctl", "print") or command[:3] == ("systemctl", "--user", "show"):
            assert self.status is not None, f"unexpected state query: {command}"
            return subprocess.CompletedProcess(command, self.status[0], self.status[1], "")
        return subprocess.CompletedProcess(command, 0, "", "")


LAUNCHD_STOPPED = (113, "")
SYSTEMD_QUERY = ("systemctl", "--user", "show", "--property=ActiveState", "--value", "pentacle-chat-streamd-v2.service")


def _unit_lines(unit: bytes, key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in unit.decode().splitlines() if line.startswith(f"{key}=")]


def _unit_environment(unit: bytes) -> dict[str, str]:
    return dict(line.split("=", 1) for line in _unit_lines(unit, "Environment"))


def test_plist_is_a_login_agent_with_explicit_stores_and_logs(params, home: Path, release: Path) -> None:
    rendered = installer.render_launchd(params)["com.pentacle.chat-streamd-v2.plist"]
    plist = plistlib.loads(rendered)

    assert plist["Label"] == deploy_mod.V2_DAEMON_LABEL == "com.pentacle.chat-streamd-v2"
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    state = home / ".local/share/pentacle-stream"
    assert plist["ProgramArguments"] == params.command == [
        str(release / installer.VENV_PYTHON), str(release / installer.DAEMON),
        "--host", "127.0.0.1", "--port", "7791",
        "--db", str(state / "sessions.db"), "--notifications-db", str(state / "notifications.db"),
        "--assets-db", str(state / "assets.db"), "--blob-root", str(state / "blobs"),
        "--spawn-cwd", str(home),
    ]
    logs = home / "Library/Logs/pentacle/chat-streamd-v2"
    assert plist["StandardOutPath"] == str(logs / "daemon.out.log")
    assert plist["StandardErrorPath"] == str(logs / "daemon.err.log")
    assert plist["EnvironmentVariables"]["PATH"].split(":")[0] == str(params.tmux_bin.parent)
    assert b"__PENTACLE_" not in rendered


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS only")
def test_plist_passes_plutil_lint(params, tmp_path: Path) -> None:
    path = tmp_path / "rendered.plist"
    path.write_bytes(installer.render_launchd(params)["com.pentacle.chat-streamd-v2.plist"])
    result = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_readers_accept_the_rendered_plist(params, release: Path, home: Path, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "com.pentacle.chat-streamd-v2.plist"
    path.write_bytes(installer.render_launchd(params)["com.pentacle.chat-streamd-v2.plist"])
    monkeypatch.setattr(deploy_mod, "_launchd_plist_path", lambda _label: path)
    service = deploy_mod.SERVICES["chat-streamd-v2"]

    program = deploy_mod._validate_v2_launchd_program(release, plistlib.loads(path.read_bytes())["ProgramArguments"])

    assert program == release / installer.VENV_PYTHON
    logs = home / "Library/Logs/pentacle/chat-streamd-v2"
    assert deploy_mod._daemon_log_paths(service) == (logs / "daemon.out.log", logs / "daemon.err.log")


def test_systemd_unit_runs_the_same_command_as_the_plist(params) -> None:
    unit = installer.render_systemd(params)["pentacle-chat-streamd-v2.service"]
    plist = plistlib.loads(installer.render_launchd(params)["com.pentacle.chat-streamd-v2.plist"])

    (exec_start,) = _unit_lines(unit, "ExecStart")
    assert shlex.split(exec_start) == plist["ProgramArguments"] == params.command
    assert _unit_environment(unit) == {"PATH": plist["EnvironmentVariables"]["PATH"]}
    assert _unit_lines(unit, "Restart") == ["always"]
    assert _unit_lines(unit, "WantedBy") == ["default.target"]
    assert _unit_lines(unit, "EnvironmentFile") == ["-%h/.config/pentacle/daemon.env"]
    assert b"__PENTACLE_" not in unit


@pytest.mark.parametrize("systemd", [False, True], ids=["launchd", "systemd"])
def test_print_writes_nothing_and_runs_nothing(argv, home: Path, monkeypatch, capsys, systemd) -> None:
    monkeypatch.setattr(installer, "_systemd", lambda: systemd)
    runner = _Runner()
    before = sorted(home.rglob("*"))

    assert installer.main([*argv, "--print"], home=home, runner=runner) == 0

    assert "ExecStart=" in capsys.readouterr().out if systemd else True
    assert sorted(home.rglob("*")) == before
    assert runner.calls == []


@pytest.mark.parametrize("systemd, unit, default_calls", [
    (False, "Library/LaunchAgents/com.pentacle.chat-streamd-v2.plist", []),
    (True, ".config/systemd/user/pentacle-chat-streamd-v2.service", [("systemctl", "--user", "daemon-reload")]),
], ids=["launchd", "systemd"])
def test_default_install_writes_the_unit_and_starts_nothing(
    argv, home: Path, monkeypatch, capsys, systemd, unit, default_calls,
) -> None:
    monkeypatch.setattr(installer, "_systemd", lambda: systemd)
    runner = _Runner()

    assert installer.main(argv, home=home, runner=runner) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["written"] == [str(home / unit)]
    assert (home / unit).read_bytes() == installer.target_files(_params(argv, home))[home / unit]
    manifest = json.loads((home / ".pentacle/daemon-service-preimage/manifest.json").read_text())
    assert manifest == {str(home / unit): "absent"}
    assert (home / ".local/share/pentacle-stream").is_dir()
    assert (home / "Library/Logs/pentacle/chat-streamd-v2").is_dir() is (not systemd)
    assert runner.calls == default_calls


def test_enable_and_rollback_issue_only_the_documented_launchctl_commands(argv, home: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(installer, "_systemd", lambda: False)
    plist = home / "Library/LaunchAgents/com.pentacle.chat-streamd-v2.plist"
    domain = f"gui/{os.getuid()}"
    runner = _Runner()
    assert installer.main(["--enable"], home=home, runner=runner) == 2  # nothing installed yet
    assert runner.calls == []
    assert installer.main(argv, home=home, runner=runner) == 0

    assert installer.main(["--enable"], home=home, runner=runner) == 0
    assert runner.calls == [
        ("launchctl", "bootout", f"{domain}/com.pentacle.chat-streamd-v2"),
        ("launchctl", "bootstrap", domain, str(plist)),
    ]

    runner.calls.clear()
    runner.status = LAUNCHD_STOPPED
    assert installer.main(["--rollback"], home=home, runner=runner) == 0
    assert runner.calls == [("launchctl", "print", f"{domain}/com.pentacle.chat-streamd-v2")]
    assert not plist.exists()  # the preimage was "absent"
    capsys.readouterr()


def test_enable_and_rollback_issue_only_the_documented_systemctl_commands(argv, home: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(installer, "_systemd", lambda: True)
    unit = home / ".config/systemd/user/pentacle-chat-streamd-v2.service"
    runner = _Runner()
    assert installer.main(argv, home=home, runner=runner) == 0
    runner.calls.clear()

    assert installer.main(["--enable"], home=home, runner=runner) == 0
    assert runner.calls == [("systemctl", "--user", "enable", "--now", "pentacle-chat-streamd-v2.service")]

    runner.calls.clear()
    runner.status = (0, "inactive\n")
    assert installer.main(["--rollback"], home=home, runner=runner) == 0
    assert runner.calls == [SYSTEMD_QUERY, ("systemctl", "--user", "daemon-reload")]
    assert not unit.exists()
    capsys.readouterr()


LAUNCHD_UNIT = "Library/LaunchAgents/com.pentacle.chat-streamd-v2.plist"
SYSTEMD_UNIT = ".config/systemd/user/pentacle-chat-streamd-v2.service"


@pytest.mark.parametrize("systemd, unit, status", [
    # launchd: only exit 113 ("could not find service") proves the job is gone.
    (False, LAUNCHD_UNIT, (0, "state = running\n")),      # loaded and running
    (False, LAUNCHD_UNIT, (1, "")),                       # query failed while the daemon lives
    (False, LAUNCHD_UNIT, (5, "")),                       # input/output error
    # systemd: only a successful query answering inactive or failed proves it.
    (True, SYSTEMD_UNIT, (0, "active\n")),
    (True, SYSTEMD_UNIT, (0, "activating\n")),            # restart loop
    (True, SYSTEMD_UNIT, (0, "deactivating\n")),
    (True, SYSTEMD_UNIT, (0, "")),                        # no answer
    (True, SYSTEMD_UNIT, (1, "")),                        # query failed while the daemon lives
    (True, SYSTEMD_UNIT, (1, "inactive\n")),              # an answer from a failed query is not trusted
])
def test_rollback_changes_nothing_unless_the_service_is_explicitly_stopped(
    argv, home: Path, monkeypatch, capsys, systemd, unit, status,
) -> None:
    """A running daemon, or a state that cannot be read, must keep its unit: a
    query error is not evidence that the service stopped."""
    monkeypatch.setattr(installer, "_systemd", lambda: systemd)
    assert installer.main(argv, home=home, runner=_Runner()) == 0
    installed = (home / unit).read_bytes()
    manifest = home / ".pentacle/daemon-service-preimage/manifest.json"
    saved_manifest = manifest.read_bytes()
    capsys.readouterr()
    runner = _Runner(status)

    assert installer.main(["--rollback"], home=home, runner=runner) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not confirmed stopped, so nothing was restored or removed" in captured.err
    assert ("systemctl --user disable --now" if systemd else "launchctl bootout") in captured.err
    assert (home / unit).read_bytes() == installed
    assert manifest.read_bytes() == saved_manifest
    assert len(runner.calls) == 1  # the state query only: no stop, no reload


@pytest.mark.parametrize("systemd, unit, status", [
    (False, LAUNCHD_UNIT, LAUNCHD_STOPPED),
    (True, SYSTEMD_UNIT, (0, "inactive\n")),
    (True, SYSTEMD_UNIT, (0, "failed\n")),
])
def test_rollback_proceeds_on_an_explicit_stopped_answer_and_never_stops_the_service(
    argv, home: Path, monkeypatch, capsys, systemd, unit, status,
) -> None:
    monkeypatch.setattr(installer, "_systemd", lambda: systemd)
    assert installer.main(argv, home=home, runner=_Runner()) == 0
    runner = _Runner(status)

    assert installer.main(["--rollback"], home=home, runner=runner) == 0

    assert not (home / unit).exists()
    assert not (home / ".pentacle/daemon-service-preimage/manifest.json").exists()
    assert not any({"bootout", "disable", "stop", "kill"} & set(call) for call in runner.calls)
    capsys.readouterr()


def test_an_existing_different_unit_is_kept_unless_replace_and_rollback_restores_it(
    argv, home: Path, monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(installer, "_systemd", lambda: False)
    plist = home / "Library/LaunchAgents/com.pentacle.chat-streamd-v2.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"hand-built unit")
    runner = _Runner()

    assert installer.main(argv, home=home, runner=runner) == 2
    assert "pass --replace" in capsys.readouterr().err
    assert plist.read_bytes() == b"hand-built unit"
    assert not (home / ".pentacle").exists()

    assert installer.main([*argv, "--replace"], home=home, runner=runner) == 0
    assert plistlib.loads(plist.read_bytes())["Label"] == "com.pentacle.chat-streamd-v2"
    assert installer.main(argv, home=home, runner=runner) == 0  # identical content is not a conflict

    runner.status = LAUNCHD_STOPPED
    assert installer.main(["--rollback"], home=home, runner=runner) == 0
    assert plist.read_bytes() == b"hand-built unit"
    capsys.readouterr()


def test_invalid_inputs_are_refused(tmp_path: Path, release: Path, home: Path, argv, capsys) -> None:
    tmux = argv[-1]
    not_executable = tmp_path / "plain-file"
    not_executable.write_text("")
    spaced = tmp_path / "with space"
    spaced.mkdir()
    cases = [
        (["--release-checkout", str(tmp_path / "empty"), "--tmux-bin", tmux], "must contain"),
        ([*argv, "--python", str(not_executable)], "--python must be an absolute path to an executable"),
        ([*argv, "--state-dir", "relative/state"], "--state-dir must be an absolute directory"),
        ([*argv, "--spawn-cwd", str(tmp_path / "missing")], "--spawn-cwd must be an existing absolute directory"),
        ([*argv, "--spawn-cwd", str(spaced)], "are not supported"),
        ([*argv, "--port", "0"], "--port must be between"),
    ]
    for case, message in cases:
        assert installer.main(case, home=home, runner=_Runner()) == 2, case
        assert message in capsys.readouterr().err, case
    assert not (home / "Library").exists() and not (home / ".config").exists()


def test_an_unbound_placeholder_is_refused(params) -> None:
    with pytest.raises(installer.InstallError, match="unbound placeholder"):
        installer.render(b"ExecStart=__PENTACLE_EXEC_START__ __PENTACLE_UNKNOWN__", params)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize("unit_format", ["launchd", "systemd"])
def test_installed_unit_command_starts_the_daemon_and_the_documented_health_check_passes(
    tmp_path: Path, home: Path, isolated_tmux_env, monkeypatch, capsys, unit_format,
) -> None:
    """Install (without enabling), then run exactly what the installed unit would
    run: its command with its environment."""
    monkeypatch.setattr(installer, "_systemd", lambda: unit_format == "systemd")
    port = _free_port()
    runner = _Runner()
    assert installer.main([
        "--release-checkout", str(REPO), "--python", sys.executable, "--tmux-bin", isolated_tmux_env,
        "--state-dir", str(tmp_path / "state"), "--spawn-cwd", str(home),
        "--port", str(port), "--local-host", "testhost",
    ], home=home, runner=runner) == 0
    (installed,) = json.loads(capsys.readouterr().out)["written"]
    if unit_format == "launchd":
        plist = plistlib.loads(Path(installed).read_bytes())
        command, environment = plist["ProgramArguments"], plist["EnvironmentVariables"]
    else:
        unit = Path(installed).read_bytes()
        command, environment = shlex.split(_unit_lines(unit, "ExecStart")[0]), _unit_environment(unit)
    assert command[0] == sys.executable and command[1] == str(REPO / installer.DAEMON)
    # Only what the unit provides, plus the login session's HOME (an empty one here).
    env = {**environment, "HOME": str(home), "PYTHONUNBUFFERED": "1"}

    log = tmp_path / "daemon.log"
    health_env = {
        **env, "AGENT_ORCH_WS_URL": f"ws://127.0.0.1:{port}", "AGENT_ORCH_HOST_ID": "testhost",
        "PYTHONPATH": os.pathsep.join([str(REPO / "services/agent-orch"), str(REPO / "services")]),
    }
    with log.open("w") as output:
        proc = subprocess.Popen(
            command, cwd=str(SERVICE_DIR), env=env, stdout=output, stderr=subprocess.STDOUT,
            **_process_group_popen_kwargs(),
        )
    try:
        boot_line = f"chat_streamd_v2 listening on 127.0.0.1:{port}"
        deadline = time.monotonic() + 30.0
        while boot_line not in log.read_text() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert boot_line in log.read_text(), log.read_text()[-2000:]

        # The documented health check; the daemon answers once startup finishes.
        while True:
            health = subprocess.run(
                [sys.executable, "-c", "from agent_orch.cli import main; raise SystemExit(main(['list']))"],
                env=health_env, capture_output=True, text=True, timeout=60, check=False,
            )
            if health.returncode == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        assert health.returncode == 0, health.stdout + health.stderr + log.read_text()[-2000:]
        assert json.loads(health.stdout) == []
    finally:
        terminate_process_group(proc)
    assert proc.poll() is not None
