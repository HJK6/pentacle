"""Complete first-cause retention with owned subprocesses, no live daemon."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
import test_loop_stall_process as subject


@pytest.mark.parametrize("timeout", [False, True])
def test_actual_failure_path_keeps_first_middle_last_and_still_fails(tmp_path, monkeypatch, timeout):
    gate = tmp_path / "gate"
    monkeypatch.setenv("V2_GATE_EVIDENCE_DIR", str(gate))
    monkeypatch.setenv("PRIVATE_SENTINEL", "DO_NOT_UPLOAD_ENV_VALUE")
    helper = tmp_path / "failed_helper.py"
    helper.write_text(
        "import sys,time\n"
        "print('FIRST_CAUSE_SYNTHETIC',flush=True)\n"
        "print('owned output '*1500,flush=True)\n"
        "print('MIDDLE_EVIDENCE',flush=True)\n"
        "print('LAST_EVIDENCE',flush=True)\n" +
        ("time.sleep(30)\n" if timeout else "sys.exit(2)\n")
    )
    original = subprocess.Popen
    children = []
    def counterpart(command, **kwargs):
        proc = original([sys.executable, str(helper)], **kwargs)
        children.append(proc)
        return proc
    monkeypatch.setattr(subject.subprocess, "Popen", counterpart)
    if timeout:
        monkeypatch.setitem(subject._run_retained_helper.__kwdefaults__, "timeout", 0.2)
    with pytest.raises(pytest.fail.Exception if timeout else AssertionError, match="FIRST_CAUSE_SYNTHETIC"):
        subject.test_real_daemon_independent_submission_before_release(tmp_path / "scratch", "held")
    logs = list((gate / "u4-upload").glob("*/helper-output.log"))
    assert len(logs) == 1
    text = logs[0].read_text()
    assert len(text) > 10000
    assert all(marker in text for marker in ("FIRST_CAUSE_SYNTHETIC", "MIDDLE_EVIDENCE", "LAST_EVIDENCE"))
    assert "DO_NOT_UPLOAD_ENV_VALUE" not in text
    assert logs[0].stat().st_mode & 0o077 == 0
    assert all(child.poll() is not None for child in children)


def test_artifact_allowlist_excludes_database_environment_and_arbitrary_state(tmp_path):
    raw, upload = tmp_path / "raw", tmp_path / "upload"
    raw.mkdir(mode=0o700)
    for name in subject.HELPER_ARTIFACTS:
        (raw / name).write_text("owned synthetic evidence")
    for name in ("sessions.db", ".env", "provider-transcript.jsonl", "unrelated.json"):
        (raw / name).write_text("DO_NOT_UPLOAD_PRIVATE")
    subject._stage_helper_artifacts(raw, upload)
    assert {p.name for p in upload.iterdir()} == set(subject.HELPER_ARTIFACTS)
    assert all("DO_NOT_UPLOAD_PRIVATE" not in p.read_text() for p in upload.iterdir())
    assert all(p.stat().st_mode & 0o077 == 0 for p in upload.iterdir())


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_unsafe_allowlisted_entry_blocks_upload_before_any_copy(tmp_path, kind):
    raw, upload = tmp_path / "raw", tmp_path / "upload"
    raw.mkdir(mode=0o700)
    (raw / "helper-output.log").write_text("safe evidence")
    private = tmp_path / "private-state"
    private.write_text("DO_NOT_UPLOAD_PRIVATE")
    target = raw / "result.json"
    if kind == "symlink":
        target.symlink_to(private)
    elif kind == "hardlink":
        os.link(private, target)
    else:
        target.mkdir()
    with pytest.raises(RuntimeError, match="unsafe synthetic"):
        subject._stage_helper_artifacts(raw, upload)
    assert not upload.exists()
    assert private.read_text() == "DO_NOT_UPLOAD_PRIVATE"


@pytest.mark.parametrize("parent_ignores_term", [False, True])
def test_timeout_reaps_group_even_if_parent_exits_before_child(tmp_path, parent_ignores_term):
    import signal
    import time

    heartbeat = tmp_path / "heartbeat"
    pidfile = tmp_path / "child.pid"
    child = tmp_path / "child.py"
    child.write_text(
        "import os,pathlib,signal,sys,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True:\n"
        " pathlib.Path(sys.argv[2]).write_text(str(time.monotonic_ns()))\n"
        " time.sleep(.02)\n")
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import pathlib,signal,subprocess,sys,time\n" +
        ("signal.signal(signal.SIGTERM,signal.SIG_IGN)\n" if parent_ignores_term else "") +
        "subprocess.Popen([sys.executable,*sys.argv[1:]])\n"
        "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n"
        "print('FIRST_CAUSE_GROUP',flush=True)\n"
        "time.sleep(30)\n")
    try:
        code, timed_out = subject._run_retained_helper(
            [sys.executable, str(parent), str(child), str(pidfile), str(heartbeat)],
            dict(os.environ), tmp_path / "output.log", timeout=.5)
        assert timed_out and code != 0
        assert pidfile.exists()
        before = heartbeat.read_text()
        time.sleep(.15)
        assert heartbeat.read_text() == before, "owned child still running after timeout cleanup"
        assert "FIRST_CAUSE_GROUP" in (tmp_path / "output.log").read_text()
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
