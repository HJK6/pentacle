"""Exercise bundle admission and the peer's real lock through a fake SSH transport."""
import importlib.util
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gate_peer", SERVICE / "tools/gate_peer.py")
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=x", "-c", "user.email=x",
                    "commit", "--allow-empty", "-m", "initial"], check=True, capture_output=True)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    bundle = tmp_path / "candidate.bundle"
    subprocess.run(["git", "-C", str(repo), "bundle", "create", str(bundle), "HEAD"],
                   check=True, capture_output=True)
    machine = SimpleNamespace(name="x", ssh_target="x")
    monkeypatch.setattr(peer.machines, "load_machines", lambda: (machine,))
    monkeypatch.setattr(peer.subprocess, "check_output", lambda *args, **kwargs: str(repo))
    return ["--peer", "x", "--tier", "contract", "--bundle", str(bundle), "--commit", sha,
            "--venv", "/missing", "--evidence-dir", str(tmp_path / "evidence")]


def fake_ssh(monkeypatch, tmp_path, takeover=False):
    def command(_target, remote, **_kwargs):
        parts = shlex.split(remote)
        code = parts[2].replace('Path("/tmp")', f'Path({str(tmp_path)!r})')
        code = code.replace('dir="/tmp"', f'dir={str(tmp_path)!r}')
        if takeover:
            code = code.replace('(lock / "owner").write_text(run_id)',
                                '(lock / "owner").write_text(run_id)\n    (lock / "owner").write_text("other")')
        return [sys.executable, "-c", code, *parts[3:]]

    monkeypatch.setattr(peer.machines, "ssh_command", command)


def test_bundle_missing_commit_refuses_before_ssh(candidate, monkeypatch, capsys):
    monkeypatch.setattr(peer.machines, "ssh_command", lambda *args, **kwargs: pytest.fail("unexpected SSH"))
    candidate[candidate.index("--commit") + 1] = "a" * 40
    assert peer.main(candidate) != 0
    assert "commit" in capsys.readouterr().err.lower()


def test_busy_preserves_other_owners_lock(candidate, monkeypatch, tmp_path, capsys):
    lock = tmp_path / "pentacle-peer-gate.lock"
    lock.mkdir()
    (lock / "owner").write_text("other")
    fake_ssh(monkeypatch, tmp_path)
    assert peer.main(candidate) == 73
    assert "busy" in capsys.readouterr().err.lower()
    assert (lock / "owner").read_text() == "other"


def test_remote_failure_removes_owned_lock(candidate, monkeypatch, tmp_path, capsys):
    fake_ssh(monkeypatch, tmp_path)
    assert peer.main(candidate) != 0
    output = capsys.readouterr()
    assert "pass" not in output.out.lower()
    assert "failure" in output.err.lower()
    assert not (tmp_path / "pentacle-peer-gate.lock").exists()


def test_remote_failure_preserves_changed_owner(candidate, monkeypatch, tmp_path):
    fake_ssh(monkeypatch, tmp_path, takeover=True)
    assert peer.main(candidate) != 0
    assert (tmp_path / "pentacle-peer-gate.lock" / "owner").read_text() == "other"


def test_closed_environment_discards_ambient(monkeypatch, tmp_path):
    monkeypatch.setenv("PENTACLE_UNRELATED", "secret")
    monkeypatch.setenv("TMUX", "ambient")
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "scripts").symlink_to(peer.ROOT / "scripts", target_is_directory=True)
    env = peer.closed_env(tmp_path, tmp_path / "venv")
    assert "TMUX" not in env
    assert "PENTACLE_UNRELATED" not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["TMPDIR"] == str(tmp_path / "tmp")


def test_worker_limit_refuses_before_ssh(candidate, monkeypatch, capsys):
    monkeypatch.setattr(peer.machines, "ssh_command", lambda *args, **kwargs: pytest.fail("unexpected SSH"))
    with pytest.raises(SystemExit) as exc:
        peer.main([*candidate, "--workers", "5"])
    assert exc.value.code != 0
    assert "worker" in capsys.readouterr().err.lower()
