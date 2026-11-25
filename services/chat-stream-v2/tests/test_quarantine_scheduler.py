import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from quarantine_scheduler import run_child
from tools import run_gate


def test_worker_exit_and_timeout_are_bounded():
    async def exercise():
        code = await run_child([sys.executable, '-c', 'raise SystemExit(3)'], dict(os.environ), timeout=3)
        assert code == 3
        with pytest.raises(asyncio.TimeoutError):
            await run_child([sys.executable, '-c', 'import time; time.sleep(30)'], dict(os.environ), timeout=0.1)
    asyncio.run(exercise())


@pytest.mark.parametrize('tier', ['unit', 'smoke', 'soak'])
@pytest.mark.parametrize('blocking_failure', [False, True])
@pytest.mark.parametrize('advisory_timeout', [False, True])
def test_advisory_tail_preserves_blocking_verdict(tmp_path, monkeypatch, tier, blocking_failure, advisory_timeout):
    monkeypatch.delenv('TMUX', raising=False)
    monkeypatch.setattr(run_gate, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(run_gate, 'summarize_progress', lambda _: {'present': True, 'complete': True})
    monkeypatch.setattr(run_gate, '_TERMINATION_REQUESTED', False)
    case = ('tests' if tier == 'unit' else f'tests/{tier}') + '/test_a.py::test_flaky'
    calls = []
    def fake(command, *, env, log_path, timeout, advisory=False):
        calls.append(advisory)
        junit = Path(next(x.split('=', 1)[1] for x in command if x.startswith('--junitxml=')))
        if advisory:
            assert not junit.exists()
            assert not (tmp_path/f'{tier}-advisory.progress.jsonl').exists()
            assert f'--quarantine-manifest={tmp_path / (tier + "-advisory.quarantine.json")}' in command
            if not advisory_timeout:
                junit.write_text('<testsuite tests="1" failures="1" errors="0" skipped="0"/>')
            return (124, True) if advisory_timeout else (1, False)
        if tier == 'soak':
            assert command[command.index('-m', 3) + 1] == 'soak and not quarantined'
        junit.write_text(f'<testsuite tests="1" failures="{int(blocking_failure)}" errors="0" skipped="0"/>')
        manifest = next(x.split('=', 1)[1] for x in command if x.startswith('--quarantine-manifest='))
        Path(manifest).write_text(json.dumps({'complete': True, 'quarantined': [case]}))
        return int(blocking_failure), False
    monkeypatch.setattr(run_gate, '_run_process', fake)
    result = run_gate._run_tier(tier, tmp_path, 10, basetemp=tmp_path/'temp', manifest=tmp_path/'owner')
    assert calls == [False]
    (tmp_path/f'{tier}-advisory.junit.xml').write_text('<testsuite tests="99"/>')
    (tmp_path/f'{tier}-advisory.progress.jsonl').write_text('{}\n')
    run_gate._run_advisory(result, tmp_path, tmp_path/'temp', tmp_path/'owner', 5)
    assert calls == [False, True]
    assert result['passed'] is not blocking_failure
    assert result['reason'] == ('test_failures' if blocking_failure else None)
    assert result['advisory']['timed_out'] is advisory_timeout


def test_advisory_without_time_is_recorded(tmp_path):
    (tmp_path / 'unit.quarantine.json').write_text(json.dumps({'complete': True, 'quarantined': ['tests/test_a.py::test_a']}))
    result = {'tier': 'unit', 'passed': True}
    run_gate._run_advisory(result, tmp_path, tmp_path, tmp_path/'owner', 0)
    assert result == {'tier': 'unit', 'passed': True, 'advisory': {'status': 'no_time_left'}}


@pytest.mark.parametrize('blocked', [False, True])
def test_tail_interruption_keeps_final_blocking_result(tmp_path, monkeypatch, blocked):
    def mandatory(tier, *args, **kwargs):
        return run_gate._result(tier, code=int(blocked), passed=not blocked,
                                reason='test_failures' if blocked else None)

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_gate, '_preflight', lambda *args, **kwargs: (0, False))
    monkeypatch.setattr(run_gate, '_git_status', lambda: '')
    monkeypatch.setattr(run_gate, '_run_tier', mandatory)
    monkeypatch.setattr(run_gate, '_run_advisory', interrupt)
    monkeypatch.setattr(sys, 'argv', ['run_gate.py', 'unit', '--evidence-dir', str(tmp_path)])
    assert run_gate.main() == int(blocked)
    final = json.loads((tmp_path / 'v2-unit-evidence.json').read_text())
    assert final['blocking_frozen'] and final['passed'] is not blocked
    assert final['tiers'][0]['advisory']['status'] == 'interrupted'


@pytest.mark.parametrize('inherited', [None, '1'])
def test_preflight_failure_stops_before_git_and_tiers(tmp_path, monkeypatch, inherited):
    if inherited is None:
        monkeypatch.delenv('PENTACLE_GATE_PREFLIGHT_DONE', raising=False)
    else:
        monkeypatch.setenv('PENTACLE_GATE_PREFLIGHT_DONE', inherited)
    monkeypatch.setattr(run_gate, '_preflight', lambda *args, **kwargs: (2, False))
    monkeypatch.setattr(run_gate, '_sha', lambda: pytest.fail('Git ran before preflight'))
    monkeypatch.setattr(run_gate, '_run_tier', lambda *args, **kwargs: pytest.fail('tier started'))
    monkeypatch.setattr(sys, 'argv', ['run_gate.py', 'merge', '--evidence-dir', str(tmp_path)])
    assert run_gate.main() == 1
    evidence = json.loads((tmp_path/'v2-merge-evidence.json').read_text())
    assert [(row['tier'], row['reason']) for row in evidence['tiers']] == [('setup', 'preflight_failed')]


def test_blocking_tiers_finish_before_frozen_advisory(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(run_gate, '_preflight', lambda *a, **kw: (0, False))
    monkeypatch.setattr(run_gate, '_sha', lambda: 'c' * 40)
    monkeypatch.setattr(run_gate, '_git_status', lambda: '')
    monkeypatch.setattr(run_gate, 'reap_manifest', lambda *a: None)
    monkeypatch.setattr(run_gate, 'initialize_manifest', lambda *a: 'owned')
    monkeypatch.setattr(run_gate, 'mark_owner_stopping', lambda *a: None)
    monkeypatch.setitem(sys.modules, 'quarantine_list', SimpleNamespace(load_quarantine=lambda root: {}))
    seen = []
    def tier(name, *args, **kwargs):
        seen.append(name)
        return run_gate._result(name, code=0, passed=True)
    def advisory(result, evidence, *args):
        frozen = json.loads((evidence / 'v2-unit-evidence.json').read_text())
        assert frozen['blocking_frozen'] and frozen['passed']
        seen.append('advisory')
        result['advisory'] = {'timed_out': True}
    monkeypatch.setattr(run_gate, '_run_tier', tier)
    monkeypatch.setattr(run_gate, '_run_advisory', advisory)
    monkeypatch.setattr(sys, 'argv', ['run_gate.py', 'unit', '--evidence-dir', str(tmp_path/'evidence'),
                                     '--basetemp', str(tmp_path/'base'), '--timeout-seconds', '10'])
    assert run_gate.main() == 0
    assert seen == ['unit', 'advisory']
