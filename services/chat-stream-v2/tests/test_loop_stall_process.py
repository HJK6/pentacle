"""Actual daemon faults with an independent synthetic checker and HTTP sink."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

import pytest


@pytest.mark.timeout(120)
@pytest.mark.parametrize("scenario", ["held", "publisher", "process"])
def test_real_daemon_independent_submission_before_release(tmp_path, scenario):
    source = Path(os.environ.get('U4_TEST_SOURCE') or Path(__file__).resolve().parents[3]).resolve()
    helper = Path(__file__).with_name('helpers') / 'u4_process_harness.py'
    sink = helper.with_name('u4_sink_server.py')
    tmux = shutil.which('tmux')
    assert tmux, 'supported-VM prerequisite: actual tmux executable on PATH'
    output = tmp_path / 'u4-held.json'
    env = {key:value for key,value in os.environ.items()
           if not key.startswith(('PENTACLE_', 'AGENT_ORCH_')) and key not in ('TMUX','TMUX_PANE')}
    command = [sys.executable, str(helper), '--source', str(source), '--output', str(output),
               '--tmux-bin', tmux, '--sink-script', str(sink), '--scenario', scenario]
    proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        stdout, _ = proc.communicate(timeout=100)
    except subprocess.TimeoutExpired:
        # The portable helper keeps all owned children in this new group.
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            stdout, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, _ = proc.communicate(timeout=5)
        pytest.fail('held-process harness timed out: ' + stdout[-6000:])
    assert proc.returncode == 0, stdout[-10000:]
    result = json.loads(output.read_text())
    assert result['classification'] == 'PASS'
    assert all(result['acceptance'].values()), result['acceptance']
    assert result['runtime_removed']
    assert len(result['cells']) == (2 if scenario == 'held' else 1)
