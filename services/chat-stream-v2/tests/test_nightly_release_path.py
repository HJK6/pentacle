"""Nightly uses the deployed release checks and only its standing window."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import nightly_journey as nj
import scheduled_admission as sa


@pytest.mark.parametrize('diagnostic', ['', 'INFO ws client=reader close code=1011\n'])
def test_nightly_scans_the_existing_process_generation(tmp_path, monkeypatch, diagnostic):
    from managed_logs import Config
    from .test_deploy_script import deploy_mod as d
    from .test_deploy_log_guard import _boot, _ACTIVE_START

    config = Config(tmp_path / 'daemon.out', tmp_path / 'daemon.err')
    config.stdout.write_text(_boot())
    config.stderr.write_text(_boot() + diagnostic)
    monkeypatch.setattr(d, '_daemon_log_paths', lambda _: (config.stdout, config.stderr))
    monkeypatch.setattr(d, '_managed_log_config', lambda _: config)
    monkeypatch.setattr(d, '_launchd_state', lambda *_: d.LaunchdState(loaded=True, pid=222))
    monkeypatch.setattr(d, '_process_start', lambda *_: _ACTIVE_START)
    monkeypatch.setattr(d, '_notify_failed_checks', lambda *a: None)
    runner = lambda argv, cwd: subprocess.CompletedProcess(argv, 0, '{"outcome":"passed"}', '')
    checks = d.run_post_release_checks(d.SERVICES['chat-streamd-v2'], tmp_path, 'a' * 40, {},
        runner=runner, fleet_smoke_check=lambda: {'outcome': 'passed'})

    assert checks['slow_consumer']['attribution']['outcome'] == 'no_outgoing_writer', checks['slow_consumer']
    assert checks['slow_consumer']['outcome'] == ('failed' if diagnostic else 'passed')


def test_held_admission_retries_inside_same_window(tmp_path, monkeypatch):
    now = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
    window = tmp_path / 'window.json'
    window.write_text(json.dumps(dict(host='test-host', start_utc='05:30', duration_s=21600,
        valid_until='2026-11-30', held_until='2026-11-15T09:01:00Z', issued_by='test')))
    monkeypatch.setattr(sa.platform, 'node', lambda: 'test-host')
    monkeypatch.setattr(nj, 'fire_guard', lambda: 'ADMIT')
    sleeps = []
    def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += timedelta(seconds=seconds)
    assert nj.wait_for_admission(window, 'nightly-soak', now=lambda: now, sleep=sleep) == 'ADMIT'
    assert sleeps == [60]


def test_gate_wait_does_not_escape_the_existing_window(tmp_path, monkeypatch):
    now = datetime(2026, 11, 15, 10, 39, 59, tzinfo=timezone.utc)
    window = tmp_path / 'window.json'
    window.write_text(json.dumps(dict(host='test-host', start_utc='05:30', duration_s=21600,
        valid_until='2026-11-30', held_until=None, issued_by='test')))
    monkeypatch.setattr(sa.platform, 'node', lambda: 'test-host')
    monkeypatch.setattr(nj, 'fire_guard', lambda: 'BLOCKED:active_gate_or_deploy')
    def sleep(seconds):
        nonlocal now
        now += timedelta(seconds=seconds)
    assert nj.wait_for_admission(window, 'nightly-soak', now=lambda: now, sleep=sleep) == 'BLOCKED:insufficient_remaining'
    assert now.date().isoformat() == '2026-11-15'


def test_held_window_that_never_fits_becomes_blocked(tmp_path, monkeypatch):
    window = tmp_path / 'window.json'
    window.write_text(json.dumps(dict(host='test-host', start_utc='05:30', duration_s=21600,
        valid_until='2026-11-30', held_until='2026-11-16T09:00:00Z', issued_by='test')))
    monkeypatch.setattr(sa.platform, 'node', lambda: 'test-host')
    assert sa.admit(window, 'nightly-soak', 2400, '2026-11-15T11:00:00Z') == 'BLOCKED:insufficient_remaining'


def test_missing_window_never_sleeps(tmp_path):
    assert nj.wait_for_admission(tmp_path/'absent', 'nightly-soak',
        sleep=lambda _: pytest.fail('must not wait on invalid authorization')) == 'BLOCKED:missing'


@pytest.mark.parametrize('outcome,result,rc', [('passed', 'PASS', 0), ('failed', 'FAIL', 1),
                                           ('blocked', 'FAIL', 1), ('skipped', 'FAIL', 1)])
def test_nightly_calls_shared_checks_on_deployed_stamp(tmp_path, monkeypatch, outcome, result, rc):
    from deploy import deploy as d
    stamp = {'sha': 'a'*40}
    service = SimpleNamespace(name='v2')
    monkeypatch.setattr(d, 'SERVICES', {'v2': service}, raising=False)
    monkeypatch.setattr(d, '_read_stamp', lambda repo, svc: stamp)
    monkeypatch.setattr(d, 'release_exclusions', lambda repo, svc: [])
    monkeypatch.setattr(d, '_git_head', lambda repo, runner: 'a'*40, raising=False)
    seen = []
    def checks(svc, repo, sha, value, **kwargs):
        seen.append((svc, repo, sha, value))
        return {name: {'outcome': outcome} for name in ('fleet_smoke', 'slow_consumer', 'front_desk')}
    monkeypatch.setattr(d, 'run_post_release_checks', checks, raising=False)
    args = SimpleNamespace(checkout=str(tmp_path), evidence=str(tmp_path/'evidence'), service='v2')
    assert nj.check_release(args, deploy_module=d) == rc
    assert seen == [(service, tmp_path, 'a'*40, {**stamp, 'release_exclusions': []})]
    assert stamp == {'sha': 'a'*40}
    record = json.loads((tmp_path/'evidence/receipt.json').read_text())
    assert record['source_sha'] == 'a'*40
    assert record['result'] == result
    assert set(record['checks']) == {'fleet_smoke', 'slow_consumer', 'front_desk'}


def test_wrapper_uses_deployed_checkout_and_existing_schedule():
    service = Path(__file__).resolve().parents[1]
    wrapper = (service / 'deploy/nightly-soak/run-nightly-soak.sh').read_text()
    assert 'PY=$CHECKOUT/services/chat-stream-v2/.venv/bin/python' in wrapper
    assert 'TOOLS=$CHECKOUT/services/chat-stream-v2/tools' in wrapper
    assert 'check-release --checkout "$CHECKOUT"' in wrapper
    assert '--wait-within-window' in wrapper
    assert 'TOOLS_ROOT' not in wrapper and 'soak_gate.py' not in wrapper
    assert not (service / 'deploy/com.pentacle.spawn-fleet-smoke.plist').exists()
