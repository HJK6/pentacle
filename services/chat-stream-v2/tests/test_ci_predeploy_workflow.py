"""Hosted supported-entry contract, traced through profiles to actual commands."""
from __future__ import annotations

import fnmatch
import importlib.util
import os
import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PREDEPLOY = ROOT / '.github/workflows/predeploy-tests.yml'
_spec = importlib.util.spec_from_file_location('ci_gate_env_contract', ROOT / 'scripts/gate_env.py')
ge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ge)
CONFIG = ge.load_config()
PROFILE = CONFIG['profiles']['pentacle']


def _step():
    workflow = yaml.safe_load(PREDEPLOY.read_text())
    return workflow, next(s for s in workflow['jobs']['checks']['steps'] if s.get('name') == 'Daemon and CLI checks')


def _invocations(step):
    commands = [shlex.split(line) for line in step['run'].splitlines() if 'scripts/gate_env.py' in line]
    assert len(commands) == 3
    assert all(argv[:3] == ['python3', 'scripts/gate_env.py', 'run'] for argv in commands)
    assert all(argv[argv.index('--profile') + 1] == 'pentacle' for argv in commands)
    return {argv[argv.index('--target') + 1]: argv for argv in commands}


def _trace(name, argv, tmp_path, *, scratch=None, workspace=None, evidence=None):
    target = PROFILE['targets'][name]
    selection = [argv[i+1] for i,v in enumerate(argv) if v == '--select']
    selection = ge.validate_selection(target, selection, ROOT)
    workspace = workspace or tmp_path / 'gate-env'
    evidence = evidence or tmp_path / 'gate-evidence' / name
    home, tmp, base = ge.scratch_paths(workspace, Path('/tmp/ge-ci'), '12345678-owned')
    env = ge.build_env(CONFIG, PROFILE, target, checkout=ROOT, toolchain={}, venv=tmp_path / 'venv',
                       home=home, tmp=tmp, locale_name='C.UTF-8', evidence=evidence, attempt_id='ci', pass_env={},
                       ambient={'PYTHONPATH':'/wrong', 'AWS_SECRET_ACCESS_KEY':'fixture-secret', 'HOME':'/operator'})
    command, result = ge.build_command(CONFIG, target, tmp_path / 'venv', ROOT, ROOT / PROFILE['service_dir'],
                                       tmp_path / 'command-evidence' / name, scratch or base, selection, target['timeout_s'], env)
    return target, command, result, env, (home,tmp,base)


def test_predeploy_runs_only_the_v2_gate_and_agent_orch_suite(tmp_path):
    _, step = _step()
    invocations = _invocations(step)
    assert set(invocations) == {'v2-merge-ci', 'cli-pytest', 'shared-pytest'}
    target, command, result, _, _ = _trace('v2-merge-ci', invocations['v2-merge-ci'], tmp_path)
    assert target['kind'] == 'run_gate' and target['gate'] == 'merge'
    assert command[1:3] == [str(ROOT / 'services/chat-stream-v2/tools/run_gate.py'), 'merge']
    assert result['cwd'] == ROOT
    for name, service in [('cli-pytest','agent-orch'), ('shared-pytest','_shared')]:
        argv = invocations[name]
        assert argv[argv.index('--select')+1] == 'tests'
        target, command, result, _, _ = _trace(name, argv, tmp_path)
        assert target['select']['root'] == 'services/' + service
        assert command[1:4] == ['-m','pytest','tests']
        assert result['cwd'] == ROOT / 'services' / service
    workflow = PREDEPLOY.read_text()
    assert 'services/chat-stream/tests' not in workflow
    assert 'services/chat-stream/harness' not in workflow
    assert '${GITHUB_WORKSPACE}/services/chat-stream:' not in workflow
    assert all('services/chat-stream/' not in str(part) for part in command)


def test_predeploy_uses_the_shared_services_import_root(tmp_path):
    _, step = _step()
    for name, argv in _invocations(step).items():
        _, _, _, env, _ = _trace(name, argv, tmp_path)
        assert env['PYTHONPATH'].split(os.pathsep) == [str(ROOT / 'services'), str(ROOT / 'services/agent-orch')]
        assert 'AWS_SECRET_ACCESS_KEY' not in env
        assert env['HOME'] != '/operator'
        assert 'fixture-secret' not in env.values()


def test_ci_unit_budget_and_failure_artifacts_keep_scratch_separate(tmp_path):
    workflow, step = _step()
    job = workflow['jobs']['checks']
    assert job['timeout-minutes'] == 50
    argv = _invocations(step)['v2-merge-ci']
    target, command, _, env, _ = _trace('v2-merge-ci', argv, tmp_path)
    assert target['tier_timeouts'] == {'unit':1500, 'smoke':900}
    assert target['timeout_s'] == target['max_timeout_s'] == 2400
    assert command[command.index('--deadline-seconds') + 1] == '2520'
    assert command[command.index('--deadline-reserve-seconds') + 1] == '60'
    assert env['V2_UNIT_TIMEOUT_S'] == '1500' and env['V2_SMOKE_TIMEOUT_S'] == '900'
    assert 'V2_SOAK_TIMEOUT_S' not in env and '--timeout-seconds' not in command
    assert CONFIG['profiles']['pentacle']['targets']['v2-unit']['timeout_s'] == 900
    assert CONFIG['profiles']['pentacle']['targets']['v2-smoke']['timeout_s'] == 900
    assert CONFIG['profiles']['pentacle']['targets']['v2-merge']['timeout_s'] == 1800
    upload = next(s for s in job['steps'] if s.get('uses') == 'actions/upload-artifact@v4')
    assert upload['if'] == 'failure()' and upload['with']['retention-days'] == 7
    runner_temp = tmp_path / 'runner-temp'
    root = runner_temp / 'gate-evidence'
    patterns = upload['with']['path'].replace('${{ runner.temp }}', str(runner_temp)).splitlines()
    assert patterns and all(p.startswith(str(root) + '/') and '..' not in Path(p).parts for p in patterns)
    included = lambda p: any(fnmatch.fnmatch(str(p), pattern) for pattern in patterns)
    for required in ['daemon/gate/v2-merge-evidence.json','daemon/gate/unit.log','daemon/gate/unit.junit.xml',
                     'daemon/attempts/a/0001-started.json','attempt-manifest.json','web/gate/web/verdict.json']:
        assert included(root / required), required
    # Trace all invocation paths: database/scratch and credential HOME live outside the uploaded roots.
    for name, args in _invocations(step).items():
        workspace = Path(args[args.index('--workspace')+1].replace('${RUNNER_TEMP}',str(runner_temp)))
        evidence = Path(args[args.index('--evidence-dir')+1].replace('${RUNNER_TEMP}',str(runner_temp)))
        assert evidence.is_relative_to(root) and not workspace.is_relative_to(root)
        _, _, _, _, (home,tmp,base) = _trace(name,args,tmp_path,workspace=workspace,evidence=evidence)
        for excluded in [base/'unit/owned-case/sessions.db',tmp/'scratch',home/'.aws/credentials',home/'.env',
                         home/'Library/Keychains/login.keychain-db',workspace/'prepared/venv']:
            assert not included(excluded), str(excluded)


def test_hosted_gate_scratch_is_accepted_by_real_isolation_guard(tmp_path, monkeypatch):
    from tests.cosmo_e2e import harness
    workflow, step = _step()
    assert workflow['jobs']['checks']['runs-on'] == 'ubuntu-latest'
    monkeypatch.delenv('COSMO_E2E_DISPOSABLE_BASE', raising=False)
    monkeypatch.setattr(harness, '_disposable_base', lambda: Path('/tmp').resolve())
    workspace = Path('/opt/ci-runner/_temp/gate-env')
    evidence = Path('/opt/ci-runner/_temp/gate-evidence/daemon')
    home,tmp,base = ge.scratch_paths(workspace, Path('/tmp/ge-ci'), '12345678-owned')
    argv = _invocations(step)['v2-merge-ci']
    _, command, _, _, _ = _trace('v2-merge-ci',argv,tmp_path,scratch=base,workspace=workspace,evidence=evidence)
    scratch = Path(command[command.index('--basetemp')+1])
    accepted = harness.assert_disposable_root(scratch/'unit'/'owned-case')
    assert accepted == (scratch/'unit'/'owned-case').resolve()
    assert home.resolve() != tmp.resolve() and not home.resolve().is_relative_to(tmp.resolve())
    assert not evidence.resolve().is_relative_to(scratch.resolve())
    assert not ge.check_layout(ROOT,workspace,evidence,home,tmp,base,CONFIG['max_tmpdir_bytes'])
