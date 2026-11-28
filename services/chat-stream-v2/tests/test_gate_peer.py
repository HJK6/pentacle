"""Peer gate CLI: local admission, lock contention, environment and transport failure."""
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gate_peer", SERVICE / "tools/gate_peer.py")
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


@pytest.fixture
def args(tmp_path):
    bundle = tmp_path / "candidate.bundle"
    bundle.write_bytes(b"bundle")
    return ["--peer", "x", "--tier", "contract", "--bundle", str(bundle),
            "--commit", "a" * 40, "--venv", "/venv", "--evidence-dir", str(tmp_path / "evidence")]


def test_bundle_missing_commit_refuses_before_ssh(args, tmp_path, capsys):
    repository = tmp_path / "source"
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=x", "-c", "user.email=x",
                    "commit", "--allow-empty", "-m", "initial"], check=True, capture_output=True)
    bundle = Path(args[args.index("--bundle") + 1])
    bundle.unlink()
    subprocess.run(["git", "-C", str(repository), "bundle", "create", str(bundle), "HEAD"], check=True, capture_output=True)
    with patch.object(peer.machines, "ssh_command") as ssh:
        assert peer.main(args) != 0
    ssh.assert_not_called()
    assert "commit" in capsys.readouterr().err.lower()


def test_busy_lock_refuses_without_running(args, capsys):
    machine = SimpleNamespace(name="x", ssh_target="x")
    with patch.object(peer, "bundle_has_commit", return_value=True), \
         patch.object(peer.machines, "load_machines", return_value=(machine,)), \
         patch.object(peer.machines, "ssh_command", return_value=["fake-ssh"]), \
         patch.object(peer.subprocess, "check_output", return_value="origin"), \
         patch.object(peer.subprocess, "run", return_value=SimpleNamespace(returncode=73, stdout=b"", stderr=b"busy")):
        assert peer.main(args) != 0
    assert "busy" in capsys.readouterr().err.lower()


def test_closed_environment_discards_ambient(monkeypatch, tmp_path):
    monkeypatch.setenv("PENTACLE_UNRELATED", "secret")
    monkeypatch.setenv("TMUX", "ambient")
    env = peer.closed_env(tmp_path, tmp_path / "venv")
    assert "TMUX" not in env
    assert not any(key.startswith("PENTACLE_") for key in env)
    assert env["HOME"] == str(tmp_path / "home")
    assert env["TMPDIR"] == str(tmp_path / "tmp")


def test_remote_failure_never_reports_pass(args, capsys):
    machine = SimpleNamespace(name="x", ssh_target="x")
    with patch.object(peer, "bundle_has_commit", return_value=True), \
         patch.object(peer.machines, "load_machines", return_value=(machine,)), \
         patch.object(peer.machines, "ssh_command", return_value=["fake-ssh"]), \
         patch.object(peer.subprocess, "check_output", return_value="origin"), \
         patch.object(peer.subprocess, "run", return_value=SimpleNamespace(returncode=42, stdout=b"", stderr=b"transfer failed")):
        assert peer.main(args) != 0
    output = capsys.readouterr()
    assert "pass" not in output.out.lower()
    assert "transfer failed" in output.err.lower()


def test_worker_limit_refuses_before_ssh(args, capsys):
    with patch.object(peer.machines, "ssh_command") as ssh:
        with pytest.raises(SystemExit) as exc:
            peer.main([*args, "--workers", "5"])
    assert exc.value.code != 0
    ssh.assert_not_called()
    assert "worker" in capsys.readouterr().err.lower()
