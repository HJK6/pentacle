"""Deployment captures account intent; synthetic transports, no runtime writes."""
import json
import os
import plistlib
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from deploy.coordinated_runtime import Runtime
from .test_deploy_script import deploy_mod
from .test_deploy_fleet_verdict import _rows
from .test_deploy_coordinated_helpers import transaction
from .test_deploy_account_admission import history
from deploy import coordinated


def test_coordinated_intent_seals_explicit_accounts(transaction):
    _, _, path, _ = transaction
    value = json.loads(path.read_text())
    value['smoke_accounts'] = ['codex=spare', 'claude=primary']
    path.write_text(json.dumps(value))
    assert coordinated.validate_intent(path)['smoke_accounts'] == value['smoke_accounts']


def test_explicit_intent_cannot_change_after_sealing(transaction):
    tx, adapter, path, receipt = transaction
    value = json.loads(path.read_text())
    value['smoke_accounts'] = ['codex=spare', 'claude=primary']
    path.write_text(json.dumps(value))
    tx.prepare(path, receipt)
    value['smoke_accounts'] = ['codex=primary', 'claude=primary']
    path.write_text(json.dumps(value))
    with pytest.raises(coordinated.Invalid):
        tx.apply(path, receipt)
    assert 'stop' not in adapter.calls


@pytest.mark.parametrize('entries', [None, ['codex=spare'], ['codex=spare', 'codex=primary'],
    ['codex=../home', 'claude=primary'], ['unknown=primary'], [None]])
def test_invalid_deploy_map_refuses_before_preparation(transaction, entries):
    tx, adapter, path, receipt = transaction
    value = json.loads(path.read_text()); value['smoke_accounts'] = entries
    path.write_text(json.dumps(value))
    with pytest.raises(coordinated.Invalid):
        tx.prepare(path, receipt)
    assert not adapter.calls


def test_coordinated_smoke_invocation_selects_account_and_clears_ambient(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.repo = tmp_path
    runtime.service = object()
    runtime.d = SimpleNamespace(_venv_python=lambda *_: '/fixture/python')
    scope = {'included_hosts': ['primary'], 'excluded_hosts': []}
    args = runtime.smoke_argv(tmp_path, scope, tmp_path/'machines',
        accounts=['codex=spare', 'claude=primary'], dry_run=True)
    assert args[-5:] == ('--account', 'codex=spare', '--account', 'claude=primary', '--dry-run')
    assert ('-u', 'PENTACLE_SMOKE_ACCOUNTS') == args[args.index('PENTACLE_SMOKE_ACCOUNTS')-1:args.index('PENTACLE_SMOKE_ACCOUNTS')+1]


def test_real_dry_runner_plan_agrees_with_selected_aliases(tmp_path):
    import sys
    runtime = Runtime.__new__(Runtime)
    runtime.repo = Path(__file__).resolve().parents[3]
    runtime.service = object()
    runtime.d = SimpleNamespace(_venv_python=lambda *_: sys.executable)
    runtime.runner = lambda args, cwd: subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    machines = tmp_path/'machines.json'
    machines.write_text(json.dumps([{'name': 'primary', 'ssh_target': None}]))
    scope = {'included_hosts': ['primary'], 'excluded_hosts': [], 'optional_hosts': []}
    plan = runtime.smoke_plan(runtime.repo, scope, machines, accounts=['codex=spare', 'claude=primary'])
    assert {r['account'] for r in plan['cells'] if r['provider'] == 'codex'} == {'spare'}


def test_explicit_helper_proof_selection_is_independent_of_defaults(history):
    runtime, ledger, database, *_ = history
    snapshot = runtime.account_snapshot(ledger.policy, database,
        accounts=['codex=spare', 'claude=spare'], scope={'included_hosts': ['host-a']})
    assert {r['alias'] for r in snapshot['selected_bindings']} == {'spare'}
    assert {r[2] for r in snapshot['defaults']} == {'primary'}


@pytest.mark.parametrize('actual', ['spare', 'primary', None])
def test_deploy_verdict_checks_selected_binding(tmp_path, actual):
    machines = tmp_path/'machines.json'
    machines.write_text(json.dumps([{'name': 'primary', 'ssh_target': None}]))
    rows = _rows('primary')
    for row in rows:
        alias = actual if row['provider'] == 'codex' else 'primary'
        row.update(account=alias, account_binding={'host': 'primary', 'provider': row['provider'],
            'alias': alias, 'generation': 'login-one'})
    result = subprocess.CompletedProcess([], 0, json.dumps({'status': 'PASS', 'cells': rows}), '')
    verdict = deploy_mod._fleet_smoke_verdict(result, str(machines),
        accounts=['codex=spare', 'claude=primary'])
    assert (verdict['outcome'] == 'passed') == (actual == 'spare')


def test_coordinated_schedule_switch_and_rollback_restore_owned_preimage(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.repo = tmp_path/'release'
    runtime.d = deploy_mod
    runtime.stopped = True
    calls = []
    runtime.checked = lambda args: calls.append(args)
    source = {'snapshot': str(tmp_path/'target-source'), 'sha': 'b'*40}
    after = tmp_path/'schedule.after'
    after.write_bytes(b'old sealed schedule')
    preimages = {}
    for key in ('policy', 'plist', 'collector', 'stamp', 'schedule', 'test_settings'):
        before = tmp_path/(key+'.before'); before.write_bytes(b'original '+key.encode())
        preimages[key] = {'path': str(tmp_path/(key+'.live')), 'saved': str(before), 'mode': 0o600}
    receipt = {'source': {'target': source, 'prior': {'sha': 'a'*40, 'original_helpers': {}}},
        'intent': {'policy_file': preimages['policy']['path']}, 'preimages': preimages,
        'post_policy': {'path': str(after)}, 'post_plist': {'path': str(after)},
        'post_schedule': {'path': str(after)}}
    runtime.switch(receipt, rollback=False)
    assert not Path(preimages['schedule']['path']).exists()  # no automatic reinstall
    runtime.switch(receipt, rollback=True)
    assert Path(preimages['schedule']['path']).read_bytes() == b'original schedule'
    assert Path(preimages['test_settings']['path']).read_bytes() == b'original test_settings'
    assert Path(preimages['test_settings']['path']).stat().st_mode & 0o777 == 0o600


def test_explicit_snapshot_and_prior_qualification_survive_hash_seed_changes(history, tmp_path):
    runtime, ledger, database, *_ = history
    scope = {'included_hosts': ['host-b', 'host-a']}
    accounts = ['codex=spare', 'claude=primary']
    snapshot = runtime.account_snapshot(ledger.policy, database, accounts=accounts, scope=scope)
    sha = 'a' * 40
    stamp = {
        'sha': sha, 'daemon_runtime_readback': {'sha': sha},
        'daemon_boot_readback': {'outcome': 'observed'},
        'fleet_smoke': {'outcome': 'passed', 'cells': [
            {'host': binding['host'], 'provider': binding['provider'],
             'prompt_mode': mode, 'outcome': 'passed', 'account_binding': binding}
            for binding in snapshot['selected_bindings'] for mode in ('prompted', 'promptless')]},
    }
    inputs = tmp_path / 'snapshot-inputs.json'
    inputs.write_text(json.dumps({'policy': ledger.policy, 'accounts': accounts,
                                 'scope': scope, 'stamp': stamp}))
    program = textwrap.dedent("""\
        import copy, json, sys
        from pathlib import Path
        service = Path(sys.argv[1])
        sys.path[:0] = [str(service), str(service.parent)]
        from deploy.coordinated import Invalid
        from deploy.coordinated_runtime import Runtime
        from deploy.restoration import qualified_prior
        inputs = json.loads(Path(sys.argv[3]).read_text())
        runtime = Runtime.__new__(Runtime)
        snapshot = runtime.account_snapshot(inputs['policy'], sys.argv[2],
            accounts=inputs['accounts'], scope=inputs['scope'])
        selected = snapshot['selected_bindings']
        stamp = inputs['stamp']
        qualification = qualified_prior(stamp, stamp['sha'], selected)
        for field in ('generation', 'executable_sha256'):
            wrong = copy.deepcopy(selected)
            wrong[0][field] = 'different-from-qualified-binding'
            try:
                qualified_prior(stamp, stamp['sha'], wrong)
            except Invalid:
                pass
            else:
                raise AssertionError(field + ' drift must remain refused')
        print(json.dumps({'accounts': snapshot, 'prior_qualification': qualification}))
    """)
    service = Path(__file__).resolve().parents[1]
    results = [json.loads(subprocess.check_output(
        [sys.executable, '-c', program, str(service), str(database), str(inputs)],
        env={**os.environ, 'PYTHONHASHSEED': str(seed)}, text=True, timeout=20))
        for seed in range(4)]
    assert all(result == results[0] for result in results[1:]), (
        'Account snapshots and prior qualification must match across process hash seeds')
