"""Never route fixture teardown to the operator's ambient tmux socket."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import test_live_window as live
from tools import run_gate


def test_live_fixture_clears_ambient_socket_and_uses_explicit_namespace(tmp_path, monkeypatch):
    receipt = tmp_path / 'tmux-calls.jsonl'
    fake = tmp_path / 'fake-tmux'
    fake.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                    f'with open({str(receipt)!r}, "a") as f: '
                    'f.write(json.dumps({"argv": sys.argv[1:], "TMUX": os.environ.get("TMUX")}) + "\\n")\n')
    fake.chmod(0o755)
    monkeypatch.setenv('TMUX', '/operator/socket,123,0')
    monkeypatch.setattr(live.shutil, 'which', lambda _: str(fake))
    daemon_env = {}
    class FakeDaemon:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
        def start(self):
            daemon_env.update(os.environ)
            daemon_env.update(self.kwargs['extra_env'])
        def stop(self):
            pass
    monkeypatch.setattr(live, 'Daemon', FakeDaemon)
    fixture = live.isolated_live_window_server.__wrapped__(tmp_path, monkeypatch)
    next(fixture)
    with pytest.raises(StopIteration):
        next(fixture)
    calls = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert 'TMUX' not in daemon_env
    assert calls and all(call['TMUX'] is None for call in calls)
    assert all(call['argv'][0] in ('-L', '-S') for call in calls)


@pytest.mark.parametrize('tier', ['unit', 'smoke', 'soak'])
def test_gate_refuses_ambient_tmux_before_launch(tier, tmp_path, monkeypatch):
    monkeypatch.setenv('TMUX', '/operator/socket,123,0')
    def forbidden(*args, **kwargs):
        pytest.fail('launched subprocess with ambient TMUX')
    monkeypatch.setattr(run_gate, '_run_process', forbidden)
    result = run_gate._run_tier(tier, tmp_path, 1, basetemp=tmp_path, manifest=tmp_path / 'owned.json')
    assert result['passed'] is False
    assert result['reason'] == 'ambient_tmux'


def test_shell_preflight_refuses_ambient_tmux():
    result = subprocess.run([str(run_gate.PRE_FLIGHT)], env={**os.environ, 'TMUX': '/operator/socket,123,0'},
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'ambient_tmux' in result.stderr
