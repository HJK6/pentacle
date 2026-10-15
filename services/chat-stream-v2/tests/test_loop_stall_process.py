"""Actual daemon faults with an independent synthetic checker and HTTP sink."""
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

import pytest


# Only the synthetic helper's existing evidence, never its runtime or databases.
HELPER_ARTIFACTS = ("helper-output.log", "result.json", *(
    "result-" + name for name in (
        "checker.json", "checker-journal.json", "sink-receipt.json",
        "daemon-progress.json.episodes.json", "daemon-progress.json.ack.json",
        "daemon.log", "daemon.stderr", "checker.stderr", "sink.stderr",
    )
))


def _stage_helper_artifacts(raw, upload):
    selected = []
    for name in HELPER_ARTIFACTS:
        path = raw / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise RuntimeError("unsafe synthetic helper evidence")
        selected.append(path)
    upload.mkdir(mode=0o700)
    for path in selected:
        target = upload / path.name
        with target.open("xb") as output:
            os.chmod(target, 0o600)
            output.write(path.read_bytes())


def _run_retained_helper(command, env, output, *, timeout=100):
    """Keep complete output even when the helper fails before its result JSON."""
    timed_out = False
    with output.open("x", encoding="utf-8") as log:
        os.chmod(output, 0o600)
        proc = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Preserve the existing owned process-group TERM/KILL cleanup.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    if sig == signal.SIGKILL:
                        raise
    return proc.returncode, timed_out


@pytest.mark.timeout(120)
@pytest.mark.parametrize("scenario", ["held", "publisher", "process"])
def test_real_daemon_independent_submission_before_release(tmp_path, scenario):
    source = Path(os.environ.get('U4_TEST_SOURCE') or Path(__file__).resolve().parents[3]).resolve()
    helper = Path(__file__).with_name('helpers') / 'u4_process_harness.py'
    sink = helper.with_name('u4_sink_server.py')
    tmux = shutil.which('tmux')
    assert tmux, 'supported-VM prerequisite: actual tmux executable on PATH'
    evidence = Path(os.environ.get('V2_GATE_EVIDENCE_DIR') or tmp_path)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = Path(tempfile.mkdtemp(prefix=f'u4-{scenario}-', dir=evidence))
    output = raw / 'result.json'
    combined = raw / 'helper-output.log'
    env = {key:value for key,value in os.environ.items()
           if not key.startswith(('PENTACLE_', 'AGENT_ORCH_')) and key not in ('TMUX','TMUX_PANE')}
    command = [sys.executable, str(helper), '--source', str(source), '--output', str(output),
               '--tmux-bin', tmux, '--sink-script', str(sink), '--scenario', scenario]
    try:
        code, timed_out = _run_retained_helper(command, env, combined)
    finally:
        uploads = evidence / 'u4-upload'
        uploads.mkdir(mode=0o700, exist_ok=True)
        _stage_helper_artifacts(raw, uploads / raw.name)
    stdout = combined.read_text(encoding='utf-8')
    if timed_out:
        pytest.fail(f'held-process harness timed out; complete evidence: {raw}\n{stdout}')
    assert code == 0, f'helper exited {code}; complete evidence: {raw}\n{stdout}'
    result = json.loads(output.read_text())
    assert result['classification'] == 'PASS'
    assert all(result['acceptance'].values()), result['acceptance']
    assert result['runtime_removed']
    assert len(result['cells']) == (2 if scenario == 'held' else 1)
