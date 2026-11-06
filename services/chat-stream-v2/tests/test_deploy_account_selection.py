"""Deployment captures account intent; synthetic transports, no runtime writes."""
import json
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from deploy.coordinated_runtime import Runtime
from .test_deploy_script import deploy_mod
from .test_deploy_fleet_verdict import _rows
from .test_deploy_coordinated_helpers import transaction
from .test_deploy_account_admission import history
from .test_release_selection import release, instruction_input
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


def test_qualification_identity_captures_selected_account(release):
    _, inputs, prepare, *_ = release
    inputs['activation']['accounts'] = ['codex=spare']
    plan = prepare()
    assert plan['identity']['activation']['accounts'] == ['codex=spare']


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


def test_scheduled_smoke_retains_reviewed_account_map(tmp_path):
    import shutil
    target = tmp_path/deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE
    target.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[3]/deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE, target)
    plist = plistlib.loads(deploy_mod._render_v2_spawn_fleet_smoke_plist(tmp_path,
        accounts=['codex=spare', 'claude=primary']))
    assert plist['EnvironmentVariables']['PENTACLE_SMOKE_ACCOUNTS'] == 'codex=spare,claude=primary'


def test_coordinated_schedule_switch_and_rollback_restore_owned_preimage(tmp_path):
    import shutil
    runtime = Runtime.__new__(Runtime)
    runtime.repo = tmp_path/'release'
    snapshot = tmp_path/'target-source'
    template = snapshot/deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE
    template.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[3]/deploy_mod.SPAWN_FLEET_SMOKE_TEMPLATE, template)
    runtime.d = deploy_mod
    runtime.stopped = True
    calls = []
    runtime.checked = lambda args: calls.append(args)
    source = {'snapshot': str(snapshot), 'sha': 'b'*40}
    after = tmp_path/'schedule.after'
    after.write_bytes(runtime.schedule_definition(source, ['codex=spare', 'claude=primary'], tmp_path/'machines.json'))
    value = plistlib.loads(after.read_bytes())
    assert value['EnvironmentVariables']['PENTACLE_SMOKE_ACCOUNTS'] == 'codex=spare,claude=primary'
    assert str(snapshot) not in value['ProgramArguments'][-1]
    preimages = {}
    for key in ('policy', 'plist', 'collector', 'stamp', 'schedule'):
        before = tmp_path/(key+'.before'); before.write_bytes(b'original '+key.encode())
        preimages[key] = {'path': str(tmp_path/(key+'.live')), 'saved': str(before), 'mode': 0o600}
    receipt = {'source': {'target': source, 'prior': {'sha': 'a'*40, 'original_helpers': {}}},
        'intent': {'policy_file': preimages['policy']['path']}, 'preimages': preimages,
        'post_policy': {'path': str(after)}, 'post_plist': {'path': str(after)},
        'post_schedule': {'path': str(after)}}
    runtime.switch(receipt, rollback=False)
    assert Path(preimages['schedule']['path']).read_bytes() == after.read_bytes()
    runtime.switch(receipt, rollback=True)
    assert Path(preimages['schedule']['path']).read_bytes() == b'original schedule'
