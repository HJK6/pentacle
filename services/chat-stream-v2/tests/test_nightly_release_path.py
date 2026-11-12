"""Nightly uses the deployed release checks and only its standing window."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import nightly_journey as nj
import scheduled_admission as sa


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
    monkeypatch.setattr(d, '_git_head', lambda repo, runner: 'a'*40, raising=False)
    seen = []
    def checks(svc, repo, sha, value, **kwargs):
        seen.append((svc, repo, sha, value))
        return {name: {'outcome': outcome} for name in ('fleet_smoke', 'slow_consumer', 'front_desk')}
    monkeypatch.setattr(d, 'run_post_release_checks', checks, raising=False)
    args = SimpleNamespace(checkout=str(tmp_path), evidence=str(tmp_path/'evidence'), service='v2')
    assert nj.check_release(args, deploy_module=d) == rc
    assert seen == [(service, tmp_path, 'a'*40, stamp)]
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
