"""Nightly uses the deployed release checks and only its standing window."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import nightly_journey as nj
import scheduled_admission as sa


@pytest.mark.parametrize('history', ['current', 'legacy', 'rotated', 'split_marker', 'missing_marker', 'rotation_gap', 'conflicting_marker', 'legacy_after'])
@pytest.mark.parametrize('diagnostic', ['', 'INFO ws client=reader close code=1011\n'])
def test_nightly_scans_the_existing_process_generation(tmp_path, monkeypatch, diagnostic, history):
    from managed_logs import Config
    from .test_deploy_script import deploy_mod as d
    from .test_deploy_log_guard import _boot, _ACTIVE_START

    config = Config(tmp_path / 'daemon.out', tmp_path / 'daemon.err')
    legacy = 'chat_streamd_v2 listening on 127.0.0.1:1234\n'
    old_failure = 'WARNING slow_consumer overflow client=old\n'
    config.stdout.write_text(legacy + _boot() if history == 'legacy' else _boot())
    config.stderr.write_text(_boot() + diagnostic)
    if history == 'legacy':
        config.stderr.write_text(legacy + old_failure + _boot() + diagnostic)
    elif history in ('rotated', 'split_marker', 'rotation_gap'):
        raw = (old_failure + _boot()).encode()
        split = raw.index(b'generation=') + 7 if history == 'split_marker' else len(raw)
        Path(str(config.stderr) + ('.2' if history == 'rotation_gap' else '.1')).write_bytes(raw[:split])
        config.stderr.write_bytes(raw[split:] + diagnostic.encode())
    elif history == 'missing_marker':
        Path(str(config.stderr) + '.1').write_text(legacy)
        config.stderr.write_text(diagnostic)
    elif history == 'conflicting_marker':
        config.stderr.write_text(_boot(boot_uuid='d84cb03f-ff3e-4461-8650-e30a3832373e') + diagnostic)
    elif history == 'legacy_after':
        config.stderr.write_text(_boot() + legacy + diagnostic)
    monkeypatch.setattr(d, '_daemon_log_paths', lambda _: (config.stdout, config.stderr))
    monkeypatch.setattr(d, '_managed_log_config', lambda _: config)
    monkeypatch.setattr(d, '_launchd_state', lambda *_: d.LaunchdState(loaded=True, pid=222))
    monkeypatch.setattr(d, '_process_start', lambda *_: _ACTIVE_START)
    monkeypatch.setattr(d, '_notify_failed_checks', lambda *a: None)
    runner = lambda argv, cwd: subprocess.CompletedProcess(argv, 0, '{"outcome":"passed"}', '')
    checks = d.run_post_release_checks(d.SERVICES['chat-streamd-v2'], tmp_path, 'a' * 40, {},
        runner=runner, fleet_smoke_check=lambda: {'outcome': 'passed'})

    scan = checks['slow_consumer']
    if history in ('missing_marker', 'rotation_gap', 'conflicting_marker', 'legacy_after'):
        assert scan['outcome'] == 'failed', scan
        assert scan['attribution']['outcome'] == 'attribution_ambiguous'
    else:
        assert scan['attribution']['outcome'] == 'no_outgoing_writer', scan
        assert scan['outcome'] == ('failed' if diagnostic else 'passed')
        if history in ('legacy', 'rotated', 'split_marker'):
            assert scan['old_generation']['classifier']['failure_count'] == 1
            assert scan['failure_count'] == bool(diagnostic)


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
def test_nightly_uses_serving_identity_for_both_checks(tmp_path, monkeypatch, outcome, result, rc):
    from deploy import deploy as d
    service = SimpleNamespace(name='v2', launchd_label='fixture')
    monkeypatch.setattr(d, 'SERVICES', {'v2': service})
    monkeypatch.setattr(d, '_read_stamp', lambda *a: pytest.fail('nightly must not read stamp'))
    monkeypatch.setattr(d, '_launchd_state', lambda *a: SimpleNamespace(pid=42))
    monkeypatch.setattr(d, '_process_start', lambda *a: 'start')
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def recv(self, **kwargs): return json.dumps({'type': 'welcome', 'runtime_sha': 'b'*40})
    monkeypatch.setattr(d, 'connect', lambda *a, **k: Socket())
    monkeypatch.setattr(d, '_daemon_log_paths', lambda *a: ())
    monkeypatch.setattr(d, '_managed_log_config', lambda *a: None)
    monkeypatch.setattr(d, '_capture_log_offsets', lambda *a, **k: {})
    seen = []
    monkeypatch.setattr(d, '_scan_slow_consumer_window', lambda *a, **k: seen.append(k) or {'outcome': outcome})
    monkeypatch.setattr(d, '_check_client_delivery', lambda *a: seen.append(a) or {'outcome': outcome})
    monkeypatch.setattr(d, '_client_delivery_cause', lambda *a: None)
    args = SimpleNamespace(checkout=str(tmp_path), evidence=str(tmp_path/'evidence'), service='v2')
    assert nj.check_release(args, deploy_module=d) == rc
    assert seen[0]['expected_pid'] == 42 and seen[0]['expected_start'] == 'start'
    assert seen[1][2:5] == ('b'*40, 42, 'start')
    record = json.loads((tmp_path/'evidence/receipt.json').read_text())
    assert record['source_sha'] == 'b'*40 and record['result'] == result
    assert set(record['checks']) == {'slow_consumer', 'client_delivery'}


def test_wrapper_uses_deployed_checkout_and_existing_schedule():
    service = Path(__file__).resolve().parents[1]
    wrapper = (service / 'deploy/nightly-soak/run-nightly-soak.sh').read_text()
    assert 'PY=$CHECKOUT/services/chat-stream-v2/.venv/bin/python' in wrapper
    assert 'TOOLS=$CHECKOUT/services/chat-stream-v2/tools' in wrapper
    assert 'check-release --checkout "$CHECKOUT"' in wrapper
    assert '--wait-within-window' in wrapper
    assert 'TOOLS_ROOT' not in wrapper and 'soak_gate.py' not in wrapper
    assert not (service / 'deploy/com.pentacle.spawn-fleet-smoke.plist').exists()

@pytest.mark.parametrize('soak_result', ['passed', 'failed', 'timeout', 'missing_bound'])
def test_soak_result_does_not_change_serving_verdict(tmp_path, soak_result):
    import os
    import sys

    checkout = tmp_path / 'checkout'
    python = checkout / 'services/chat-stream-v2/.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
if args[0] == '-':
    os.execv(sys.executable, [sys.executable, *args])
if args[0].endswith('nightly_journey.py'):
    if args[1] == 'admit': print('ADMIT')
    sys.exit(0)
if args[0].endswith('scheduled_admission.py'):
    receipt = pathlib.Path(args[args.index('--receipt') + 1])
    if 'check-release' in args:
        receipt.write_text(json.dumps({{'terminated_at_bound': False}}))
        (receipt.parent / 'receipt.json').write_text(json.dumps({{'result': 'PASS', 'primary_error': ''}}))
        sys.exit(0)
    scenario = os.environ['SOAK_CASE']
    if scenario != 'missing_bound':
        receipt.write_text(json.dumps({{'terminated_at_bound': scenario == 'timeout', 'elapsed_s': 12.5}}))
    if scenario != 'missing_bound':
        (receipt.parent / 'soak.xml').write_text('<testsuites><testsuite tests="2" failures="1" errors="0" skipped="0"/></testsuites>')
    sys.exit(0 if scenario == 'passed' else 1)
sys.exit(99)
''')
    python.chmod(0o755)
    config = tmp_path / 'config.zsh'
    config.write_text(f'CHECKOUT={checkout}\nMEMORY_ROOT={tmp_path}\n')
    window = tmp_path / '.pentacle/nightly-journey/window.json'
    window.parent.mkdir(parents=True)
    window.write_text(json.dumps({'start_utc': '00:00', 'duration_s': 86400}))
    launcher = Path(__file__).resolve().parents[1] / 'deploy/nightly-soak/run-nightly-soak.sh'
    run = subprocess.run(['zsh', str(launcher)], capture_output=True, text=True, timeout=30,
                         env={**os.environ, 'HOME': str(tmp_path), 'NIGHTLY_SOAK_CONFIG': str(config),
                              'SOAK_CASE': soak_result})
    assert run.returncode == 0, run.stdout + run.stderr
    receipt_path, = (tmp_path / '.pentacle/soak').glob('*/evidence/receipt.json')
    record = json.loads(receipt_path.read_text())
    assert (record['result'], record['primary_error']) == ('PASS', '')
    assert record['soak']['result'] == ('failed' if soak_result == 'missing_bound' else soak_result)
    assert record['soak']['duration_s'] == (None if soak_result == 'missing_bound' else 12.5)
    assert record['soak']['counts'] == (None if soak_result == 'missing_bound' else
        {'tests': 2, 'failures': 1, 'errors': 0, 'skipped': 0})
