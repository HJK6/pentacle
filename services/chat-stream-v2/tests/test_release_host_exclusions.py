"""One installed declaration scopes ordinary/nightly smoke without hiding reds."""
import copy
from datetime import datetime, timezone
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from .test_deploy_script import deploy_mod as d, V2_SERVICE, TARGET_SHA
from .test_deploy_log_guard import isolated_release
import nightly_journey as nj

LEGACY_DECLARATION = {'host': 'optional', 'reason': 'CLI pin mismatch',
                      'readiness_item': 'pentacle__fixture_readiness_2026_11'}
DECLARATION = {**LEGACY_DECLARATION, 'owner': 'release owner',
               'starts_at': '2020-01-01T00:00:00Z', 'expires_at': '2099-01-01T00:00:00Z'}
ACTIVE = {**DECLARATION, 'status': 'active'}


def machines(tmp_path, declarations):
    path=tmp_path/'machines.json'
    path.write_text(json.dumps({'machines':[{'name':'primary','ssh_target':None},
        {'name':'optional','ssh_target':'optional.example.invalid','smoke_optional':True}],
        'release_exclusions':declarations}))
    return path


def rows(host, outcome='passed'):
    return [{'host':host,'provider':p,'prompt_mode':m,'outcome':outcome}
        for p in ('claude','codex') for m in ('prompted','promptless')]


@pytest.mark.parametrize('included_outcome', ['passed','failed'])
@pytest.mark.parametrize('decision', [None, 'active', 'expired', 'migration_required'])
def test_smoke_scope_preserves_included_failures(tmp_path, monkeypatch, included_outcome, decision):
    catalog_row = LEGACY_DECLARATION if decision == 'migration_required' else DECLARATION
    declarations = [catalog_row] if decision else []
    declared = decision == 'active'
    path=machines(tmp_path,declarations);calls=[]
    monkeypatch.setattr(d,'_launchd_environment',lambda _: {'PENTACLE_MACHINES_FILE':str(path)})
    monkeypatch.setattr(d,'_scan_slow_consumer_window',lambda *a,**k:{'outcome':'passed'})
    monkeypatch.setattr(d,'_process_start',lambda *a:'start')
    monkeypatch.setattr(d,'_notify_failed_checks',lambda *a:None)
    def runner(argv,cwd):
        calls.append(argv)
        cells = rows('primary', included_outcome) + ([] if declared else rows('optional'))
        payload={'ok':included_outcome=='passed','cells':cells} if any('spawn_fleet_smoke.py' in v for v in argv) else {'outcome':'passed'}
        return subprocess.CompletedProcess(argv,0,json.dumps(payload),'')
    now = datetime(2100 if decision == 'expired' else 2026, 1, 1, tzinfo=timezone.utc)
    decisions = d.release_exclusions(V2_SERVICE, tmp_path, now=now)
    stamp = {'release_exclusions': decisions}
    result=d.run_post_release_checks(V2_SERVICE,tmp_path,TARGET_SHA,stamp,runner=runner,
        boot=d.BootReadback(d.BOOT_OBSERVED,'fixture',0,99,None))
    smoke=next(c for c in calls if any('spawn_fleet_smoke.py' in v for v in c))
    setting = 'PENTACLE_SMOKE_EXCLUDED_HOSTS=' + ('optional' if declared else '')
    assert setting in smoke
    assert ('-u', 'PENTACLE_SMOKE_EXCLUDED_HOSTS') == smoke[smoke.index('PENTACLE_SMOKE_EXCLUDED_HOSTS')-1:smoke.index('PENTACLE_SMOKE_EXCLUDED_HOSTS')+1]
    from tools.spawn_fleet_smoke import smoke_plan
    monkeypatch.delenv('PENTACLE_MACHINES_JSON', raising=False)
    monkeypatch.delenv('PENTACLE_SMOKE_HOSTS', raising=False)
    monkeypatch.setenv('PENTACLE_MACHINES_FILE', str(path))
    monkeypatch.setenv('PENTACLE_SMOKE_EXCLUDED_HOSTS', setting.partition('=')[2])
    assert {cell[0] for cell in smoke_plan()['cells']} == ({'primary'} if declared else {'primary', 'optional'})
    expected = 'untested' if decision == 'migration_required' and included_outcome == 'passed' else included_outcome
    assert result['fleet_smoke']['outcome'] == expected
    if decision == 'migration_required':
        assert result['fleet_smoke']['exclusion_issues'] == decisions
        assert result['fleet_smoke']['full_fleet_acceptance'] is False
    assert len([c for c in result['fleet_smoke']['cells'] if c['outcome']=='SKIPPED_DECLARED']) == (4 if declared else 0)
    assert stamp['release_exclusions'] == ([{**catalog_row, 'status': decision}] if decision else [])


def test_ordinary_snapshot_is_independent_of_later_home_change(tmp_path, monkeypatch):
    path=machines(tmp_path,[DECLARATION]);monkeypatch.setattr(d,'_launchd_environment',lambda _: {'PENTACLE_MACHINES_FILE':str(path)})
    snapshot=d.release_exclusions(V2_SERVICE,tmp_path)
    machines(tmp_path,[])
    assert snapshot==[ACTIVE]
    assert d.release_exclusions(V2_SERVICE,tmp_path)==[]


def test_ordinary_stamps_scope_before_activation(isolated_release, tmp_path, monkeypatch):
    module, service, repo, _, _, _, _, activate = isolated_release
    path = machines(tmp_path, [DECLARATION])
    monkeypatch.setattr(module, '_launchd_environment', lambda _: {'PENTACLE_MACHINES_FILE': str(path)})
    kickstart = module._kickstart
    def activation(*args):
        assert module._read_stamp(repo, service)['release_exclusions'] == [ACTIVE]
        machines(tmp_path, [])
        return kickstart(*args)
    monkeypatch.setattr(module, '_kickstart', activation)
    stamp = activate()
    assert stamp['serving'] is True
    assert stamp['release_exclusions'] == [ACTIVE]
    assert module._read_stamp(repo, service)['release_exclusions'] == [ACTIVE]


def test_nightly_reads_same_home_afresh_without_mutating_runtime_stamp(tmp_path,monkeypatch):
    path=machines(tmp_path,[DECLARATION]);stamp={'sha':TARGET_SHA,'release_exclusions':[]};original=copy.deepcopy(stamp)
    monkeypatch.setattr(d,'_launchd_environment',lambda _: {'PENTACLE_MACHINES_FILE':str(path)})
    monkeypatch.setattr(d,'_read_stamp',lambda *a:stamp)
    monkeypatch.setattr(d,'_write_stamp',lambda *a:pytest.fail('nightly must not rewrite recovery-bound stamp'))
    seen=[]
    def checks(service,repo,sha,value,**kwargs):
        seen.append(copy.deepcopy(value));return {k:{'outcome':'passed'} for k in ('fleet_smoke','slow_consumer','front_desk')}
    monkeypatch.setattr(d,'run_post_release_checks',checks)
    args=SimpleNamespace(checkout=str(tmp_path),evidence=str(tmp_path/'evidence'),service='chat-streamd-v2')
    assert nj.check_release(args,deploy_module=d)==0
    assert seen[-1]['release_exclusions']==[ACTIVE]
    assert json.loads((tmp_path/'evidence/receipt.json').read_text())['release_exclusions']==[ACTIVE]
    machines(tmp_path, [{**DECLARATION, 'expires_at': '2021-01-01T00:00:00Z'}])
    assert nj.check_release(args, deploy_module=d) == 0
    assert seen[-1]['release_exclusions'][0]['status'] == 'expired'
    assert not d._declared_release_scope(seen[-1]['release_exclusions'])
    assert stamp == original


def test_explicit_rollback_qualifies_only_declared_included_bindings(tmp_path, monkeypatch):
    from deploy import coordinated_runtime
    path = machines(tmp_path, [DECLARATION])
    policy = tmp_path / 'policy.json'
    policy.write_text('{}')
    policy.chmod(0o600)
    monkeypatch.setattr(d, '_launchd_plist', lambda _: {'EnvironmentVariables': {
        'PENTACLE_MACHINES_FILE': str(path), 'PENTACLE_PROVIDER_ACCOUNT_POLICY': str(policy)}})
    monkeypatch.setattr(coordinated_runtime, 'code_identity_at', lambda *a: 'fixture-identity')
    bindings = [{'host': host, 'provider': 'claude', 'alias': 'primary',
        'generation': 'one', 'executable_sha256': '1' * 64, 'version': '1.0'}
        for host in ('primary', 'optional')]
    cells = [{'host': b['host'], 'provider': b['provider'], 'prompt_mode': mode,
              'outcome': 'passed', 'account_binding': dict(b)}
             for b in bindings[:1] for mode in ('prompted', 'promptless')]
    skips = [{'host': 'optional', 'provider': 'claude', 'prompt_mode': mode,
              'outcome': 'SKIPPED_DECLARED', 'reason': DECLARATION['reason'],
              'authority': DECLARATION['readiness_item']} for mode in ('prompted', 'promptless')]
    witness = {'sha': TARGET_SHA, 'daemon_runtime_readback': {'sha': TARGET_SHA},
        'daemon_boot_readback': {'outcome': 'observed'},
        'fleet_smoke': {'outcome': 'passed', 'cells': cells + skips, 'skipped': skips,
                        'not_selected': []}}
    seen = []
    def proof(receipt, source, boot):
        seen.append(receipt['scope'])
        return {'outcome': 'passed'}
    runtime = SimpleNamespace(database=lambda _: 'fixture-db',
        account_snapshot=lambda *a, **k: {'selected_bindings': bindings},
        restoration_proof=proof)
    stamp = {'sha': TARGET_SHA, 'release_exclusions': [LEGACY_DECLARATION],
             'restored_qualification': {'stamp': witness}}
    assert d._verify_direct_restoration(V2_SERVICE, tmp_path, stamp, None,
        lambda *a: None, runtime=runtime)['outcome'] == 'passed'
    assert seen[0]['included_hosts'] == ['primary']


@pytest.mark.parametrize(('declaration', 'at', 'status'), [
    (DECLARATION, '2019-12-31T23:59:59+00:00', 'pending'),
    (DECLARATION, '2020-01-01T00:00:00+00:00', 'active'),
    (DECLARATION, '2098-12-31T23:59:59+00:00', 'active'),
    (DECLARATION, '2099-01-01T00:00:00+00:00', 'expired'),
    (LEGACY_DECLARATION, '2026-01-01T00:00:00+00:00', 'migration_required'),
])
def test_catalog_classifies_exclusions_and_only_active_rows_select(tmp_path, monkeypatch, declaration, at, status):
    path = machines(tmp_path, [declaration])
    monkeypatch.setattr(d, '_launchd_environment', lambda _: {'PENTACLE_MACHINES_FILE': str(path)})
    decisions = d.release_exclusions(V2_SERVICE, tmp_path, now=datetime.fromisoformat(at))
    assert decisions == [{**declaration, 'status': status}]
    assert bool(d._declared_release_scope(decisions)) == (status == 'active')
