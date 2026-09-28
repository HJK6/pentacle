"""Fail-first coverage for v2 gate ownership and hermetic soak setup."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("isolated_tmux_env")

from tools.run_gate import _process_group_exists


SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_DIR.parents[1]
TOOLS_DIR = SERVICE_DIR / "tools"
OWNER_SCRIPT = r'''
import json
import os
import signal
import sys
import time
from pathlib import Path

from tests.soak.harness import Daemon, TmuxNamespace
from tools.gate_owner_manifest import (
    initialize_manifest,
    mark_owner_stopping,
    reap_manifest,
)

root, manifest = (Path(value) for value in sys.argv[1:3])
initialize_manifest(manifest)
namespace = TmuxNamespace(root)
namespace.start()
daemon = Daemon(str(root / "sessions.db"), namespace)
daemon.start()

def stop(_signum, _frame):
    mark_owner_stopping(manifest)
    try:
        daemon.kill()
    finally:
        namespace.kill_server()
        reap_manifest(manifest)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
print(json.dumps({
    "pid": os.getpid(),
    "daemon": daemon.proc.pid,
    "daemon_pgid": os.getpgid(daemon.proc.pid),
    "socket": namespace.socket,
}), flush=True)
while True:
    time.sleep(1)
'''


def _start_owner(manifest: Path) -> tuple[subprocess.Popen[str], dict]:
    env = dict(
        os.environ,
        PYTHONPATH=str(SERVICE_DIR.parent),
        V2_GATE_OWNER_MANIFEST=str(manifest),
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", OWNER_SCRIPT, str(manifest.parent), str(manifest)],
        cwd=SERVICE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    if not line:
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        raise AssertionError(f"owner failed to start: {stderr}")
    return proc, json.loads(line)


def _socket_path(socket_name: str) -> Path:
    from tools.gate_owner_manifest import resolve_tmux_socket

    return resolve_tmux_socket(socket_name)


def _descendant_commands(root_pid: int) -> list[str]:
    rows = subprocess.check_output(
        ["ps", "axww", "-o", "pid=,ppid=,command="], text=True,
    ).splitlines()
    children: dict[int, list[tuple[int, str]]] = {}
    for row in rows:
        fields = row.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append((pid, fields[2]))
    commands: list[str] = []
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for pid, command in children.get(parent, []):
            commands.append(command)
            pending.append(pid)
    return commands


def _process_command(pid: int) -> str:
    """Read the complete argv without ps's default output-width truncation."""
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.is_file():
        try:
            return " ".join(
                part.decode(errors="replace")
                for part in proc_cmdline.read_bytes().split(b"\0")
                if part
            )
        except OSError:
            pass
    return subprocess.check_output(
        ["ps", "-ww", "-p", str(pid), "-o", "command="], text=True,
    )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_sigterm_reaps_owned_processes(tmp_path: Path) -> None:
    manifest = tmp_path / ".owned.json"
    proc, info = _start_owner(manifest)
    child_pgid = int(info["daemon_pgid"])
    try:
        time.sleep(2)
        os.kill(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        assert not manifest.exists()
        assert not _process_group_exists(child_pgid)
        assert not _socket_path(info["socket"]).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        subprocess.run(["tmux", "-L", info["socket"], "kill-server"], check=False)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.skip(
    reason=(
        "2026-09-03: intermittent v2 tmux-socket cleanup race tracked by "
        "spec_pentacle__v2_gate_harness_reaping_socket_race_2026_09"
    )
)
def test_sigkill_then_reap_manifest_cleans(tmp_path: Path) -> None:
    manifest = tmp_path / ".owned.json"
    proc, info = _start_owner(manifest)
    child_pgid = int(info["daemon_pgid"])
    try:
        os.kill(proc.pid, signal.SIGKILL)
        assert proc.wait(timeout=10) == -signal.SIGKILL
        assert manifest.exists()
        fake_bin = tmp_path / "fake-bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then exit 0; fi\n"
            f"exec {shlex.quote(shutil.which('git') or '/usr/bin/git')} \"$@\"\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        next_env = dict(
            os.environ,
            PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
            PYTHONPATH=str(SERVICE_DIR.parent),
        )
        outer_evidence = tmp_path / "outer-evidence"
        outer_evidence.mkdir()
        outer_log = outer_evidence / "unit.log"
        outer_log.write_text("outer evidence\n", encoding="utf-8")
        next_env["V2_GATE_EVIDENCE_DIR"] = str(outer_evidence)
        next_env["V2_GATE_OWNER_MANIFEST"] = str(manifest)
        # The nested gate must never write into the outer gate's evidence
        # bundle: that bundle is this program's acceptance instrument.
        next_env.pop("V2_GATE_EVIDENCE_DIR", None)
        next_env.pop("V2_GATE_OWNER_MANIFEST", None)
        assert "V2_GATE_EVIDENCE_DIR" not in next_env
        assert "V2_GATE_OWNER_MANIFEST" not in next_env
        nested_evidence = tmp_path / "nested-evidence"
        next_run = subprocess.Popen(
            [sys.executable, str(TOOLS_DIR / "run_gate.py"), "unit",
             "--basetemp", str(tmp_path), "--evidence-dir", str(nested_evidence),
             "--timeout-seconds", "30"],
            cwd=REPO_ROOT,
            env=next_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        saw_new_owner = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if manifest.exists():
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if data["owner_pid"] != info["pid"] and data["owner_state"] == "active":
                    saw_new_owner = True
                    break
            if next_run.poll() is not None:
                break
            time.sleep(0.05)
        if not saw_new_owner:
            output, _ = next_run.communicate(timeout=5)
            raise AssertionError(output)
        assert not _process_group_exists(child_pgid)
        assert not _socket_path(info["socket"]).exists()
        assert outer_log.read_text(encoding="utf-8") == "outer evidence\n"
        assert not (outer_evidence / "v2-unit-evidence.json").exists()
        os.kill(next_run.pid, signal.SIGTERM)
        next_run.wait(timeout=30)
        assert not manifest.exists()
    finally:
        if "next_run" in locals() and next_run.poll() is None:
            next_run.kill()
            next_run.wait()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.timeout(0)
def test_hermetic_soak_child_env_makes_no_remote_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.soak.harness import Daemon, TmuxNamespace

    for name, value in {
        "PENTACLE_MACHINES_JSON": "{\"machines\":[{\"name\":\"remote\",\"ssh_target\":\"host\"}]}",
        "PENTACLE_MACHINES_FILE": "/ambient/machines.json",
        "PENTACLE_USAGE_PROBE_HOST": "remote",
        "PENTACLE_SSH_CONTROL_DIR": "/ambient/cm",
        "PENTACLE_SSH_CONTROL_PERSIST": "1",
        "SSH_AUTH_SOCK": "/ambient/agent.sock",
    }.items():
        monkeypatch.setenv(name, value)
    namespace = TmuxNamespace(tmp_path)
    env = namespace.child_env()
    try:
        assert "PENTACLE_MACHINES_JSON" not in env
        assert json.loads(Path(env["PENTACLE_MACHINES_FILE"]).read_text(encoding="utf-8")) == {
            "machines": [{"name": "soakhost"}],
        }
        for name in (
            "PENTACLE_USAGE_PROBE_HOST",
            "PENTACLE_SSH_CONTROL_DIR",
            "PENTACLE_SSH_CONTROL_PERSIST",
            "SSH_AUTH_SOCK",
        ):
            assert name not in env
        daemon = Daemon(str(tmp_path / "sessions.db"), namespace)
        daemon.start()
        try:
            command = _process_command(daemon.proc.pid)
            assert "--disable-hosts" in command
            assert "--disable-usage-state-publisher" in command
            assert str((SERVICE_DIR / "tests" / "smoke" / "stub_cli.py").resolve()) in command
            for path in (
                tmp_path / "notifications.db", tmp_path / "assets.db", tmp_path / "blobs",
            ):
                assert str(path) in command
            assert str(Path.home() / ".local" / "share" / "pentacle-stream") not in command
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                descendants = _descendant_commands(daemon.proc.pid)
                assert not any("ssh " in command for command in descendants)
                assert not any(
                    "claude" in command and "stub_cli.py" not in command
                    for command in descendants
                )
                time.sleep(1.0)
        finally:
            daemon.kill()
    finally:
        namespace.kill_server()


def _manifest_with_entry(path: Path, entry: dict) -> None:
    """Write a v-current manifest owned by this process holding one entry."""
    from tools.gate_owner_manifest import (
        MANIFEST_SCHEMA,
        MANIFEST_VERSION,
        process_start_identity,
    )

    path.write_text(
        json.dumps({
            "schema": MANIFEST_SCHEMA, "version": MANIFEST_VERSION,
            "owner_pid": os.getpid(),
            "owner_start_identity": process_start_identity(os.getpid()),
            "owner_state": "stopping", "run_id": "run-under-test",
            "entries": [entry],
        }),
        encoding="utf-8",
    )


def _sleeper() -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            start_new_session=True)


def test_recycled_pgid_with_different_start_time_is_not_signalled(tmp_path: Path) -> None:
    """A pgid whose recorded leader is gone must never be signalled.

    The number alone cannot distinguish the original group from an unrelated
    one that inherited it after pid reuse, so reaping must be identity-bound.
    """
    from tools.gate_owner_manifest import reap_manifest

    victim = _sleeper()
    try:
        # The entry claims the victim's pgid but a dead leader with a start
        # identity that can never match the live process now holding it.
        dead_pid = victim.pid
        manifest = tmp_path / ".owned.json"
        _manifest_with_entry(manifest, {
            "pgid": os.getpgid(victim.pid), "socket": None, "socket_path": None,
            "leader_pid": dead_pid, "leader_start_identity": "Thu Jan  1 00:00:00 1970",
            "run_id": "run-under-test",
        })
        reap_manifest(manifest, "run-under-test")
        time.sleep(1.5)
        assert victim.poll() is None, "reaper signalled a group it could not identify"
    finally:
        victim.kill()
        victim.wait()


@pytest.mark.parametrize("identity", [None, ""])
def test_missing_leader_start_identity_is_not_signalled(
    tmp_path: Path, identity: str | None,
) -> None:
    """An incomplete identity record is never sufficient to signal a group."""
    from tools.gate_owner_manifest import reap_manifest

    victim = _sleeper()
    try:
        manifest = tmp_path / ".owned.json"
        socket = tmp_path / "untrusted.sock"
        socket.touch()
        _manifest_with_entry(manifest, {
            "pgid": os.getpgid(victim.pid), "socket": str(socket), "socket_path": str(socket),
            "leader_pid": victim.pid, "leader_start_identity": identity,
            "run_id": "run-under-test",
        })
        reap_manifest(manifest, "run-under-test")
        time.sleep(1.5)
        assert victim.poll() is None, "reaper signalled an incompletely identified group"
        assert socket.exists(), "reaper removed a socket for an incompletely identified group"
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait()


def test_reap_signals_entry_whose_identity_still_matches(tmp_path: Path) -> None:
    """The positive half: a fully-matching entry is still reaped."""
    from tools.gate_owner_manifest import process_start_identity, reap_manifest

    victim = _sleeper()
    try:
        manifest = tmp_path / ".owned.json"
        _manifest_with_entry(manifest, {
            "pgid": os.getpgid(victim.pid), "socket": None, "socket_path": None,
            "leader_pid": victim.pid,
            "leader_start_identity": process_start_identity(victim.pid),
            "run_id": "run-under-test",
        })
        reap_manifest(manifest, "run-under-test")
        victim.wait(timeout=10)
        assert victim.poll() is not None
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait()


def test_reap_is_scoped_to_the_calling_run(tmp_path: Path) -> None:
    """A foreign run's entry survives, and so does the manifest holding it."""
    from tools.gate_owner_manifest import process_start_identity, reap_manifest

    victim = _sleeper()
    try:
        manifest = tmp_path / ".owned.json"
        _manifest_with_entry(manifest, {
            "pgid": os.getpgid(victim.pid), "socket": None, "socket_path": None,
            "leader_pid": victim.pid,
            "leader_start_identity": process_start_identity(victim.pid),
            "run_id": "some-other-run",
        })
        reap_manifest(manifest, "run-under-test")
        time.sleep(1.5)
        assert victim.poll() is None, "reaped an entry belonging to another run"
        assert manifest.exists(), "removed a manifest still holding foreign entries"
    finally:
        victim.kill()
        victim.wait()


def test_record_refuses_unavailable_leader_start_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder must not persist an entry when it cannot bind process identity."""
    from tools import gate_owner_manifest as owner

    victim = _sleeper()
    try:
        manifest = tmp_path / ".owned.json"
        owner.initialize_manifest(manifest)
        monkeypatch.setattr(owner, "process_start_identity", lambda _pid: None)
        with pytest.raises(owner.ManifestError, match="requires a group leader and start identity"):
            owner.record(manifest, os.getpgid(victim.pid), None, leader_pid=victim.pid)
    finally:
        victim.kill()
        victim.wait()


def test_record_refuses_the_recorders_own_process_group(tmp_path: Path) -> None:
    """Closes the getpgid/setsid race that could record the test session."""
    from tools.gate_owner_manifest import ManifestError, initialize_manifest, record

    manifest = tmp_path / ".owned.json"
    initialize_manifest(manifest)
    with pytest.raises(ManifestError, match="own process group"):
        record(manifest, os.getpgrp(), None, leader_pid=os.getpid())
