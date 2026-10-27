"""Admission/refusal through the real deploy transaction, with isolated launchd."""
from pathlib import Path
import hashlib
import json

import pytest

from .test_deploy_log_guard import isolated_release


@pytest.mark.parametrize("reason", [
    "missing installed baselines", "consumer source mismatch", "stale seam identity",
    "unqualified required seam provider_startup", "missing/deleted seam target",
    "duplicate seam result", "non-green seam execution", "invalid selector result/identity",
])
def test_bad_selection_refuses_before_activation_and_restores_preimage(isolated_release, monkeypatch, reason):
    module, service, repo, prior, _, before, calls, activate = isolated_release
    original = module.deploy
    monkeypatch.setattr(module, "deploy", lambda *a, **kw: original(*a, release_inputs=Path("/synthetic-inputs.json"), **kw))
    def refuse(*_):
        raise module.release_selection.Invalid("release selection: " + reason)
    monkeypatch.setattr(module.release_selection, "admit", refuse)
    with pytest.raises(module.DeployError, match=reason):
        activate()
    assert not any(c[0] == "launchctl" for c in calls)
    assert module._stamp_path(repo, service).read_bytes() == before
    assert module._git(repo, "rev-parse", "HEAD", runner=module._run) == prior


def test_supplied_merge_receipt_is_reused_once_and_seams_admitted(isolated_release, monkeypatch):
    module, _, _, _, target, _, calls, activate = isolated_release
    original = module.deploy
    monkeypatch.setattr(module, "deploy", lambda *a, **kw: original(*a, release_inputs=Path("/synthetic-inputs.json"), **kw))
    admitted = []
    def admit(*args):
        evidence = args[3]
        assert evidence["sha"] == target and evidence["passed"] is True
        evidence["seam_results"] = {"identity": {"candidate": target}, "results": []}
        admitted.append(True)
        return {"identity": {"candidate": target}}
    monkeypatch.setattr(module.release_selection, "admit", admit)
    monkeypatch.setattr(module.release_selection, "revalidate", lambda *_: None)
    monkeypatch.setattr(module, "_validate_selected_activation", lambda *_: None)
    monkeypatch.setattr(module, "run_gate_and_write_evidence", lambda *_: pytest.fail("duplicate merge gate"))
    stamp = activate()
    assert admitted == [True]
    assert stamp["selective_qualification"] == "admitted"
    assert stamp["gates"]["evidence_source"] == "file"
    assert len([c for c in calls if "kickstart" in c or "bootstrap" in c]) == 1


def test_legacy_mode_never_claims_selective_qualification(isolated_release):
    *_, activate = isolated_release
    assert activate()["selective_qualification"] == "not_enabled"


@pytest.mark.parametrize("mismatch", [None, "endpoint", "host", "configuration", "missing_baseline"])
def test_activation_floor_is_bound_to_actual_local_runtime_config(tmp_path, monkeypatch, mismatch):
    from .test_deploy_script import deploy_mod, V2_SERVICE
    machines = tmp_path / "machines.json"
    machines.write_text(json.dumps([{"name": "primary", "ssh_target": None},
                                    {"name": "secondary", "ssh_target": "secondary.example.invalid"}]))
    monkeypatch.setattr(deploy_mod, "_launchd_environment", lambda _: {"PENTACLE_MACHINES_FILE": str(machines)})
    binding = {"path": str(machines), "sha256": hashlib.sha256(machines.read_bytes()).hexdigest()}
    plan = {"identity": {"activation": {"url": "ws://127.0.0.1:7791", "host": "primary"},
                         "installed": [{"component": "secondary", "role": "satellite"}],
                         "external_inputs": {"deploy_config": {"candidate": {"files": [binding]}}}}}
    if mismatch == "endpoint":
        plan["identity"]["activation"]["url"] = "ws://127.0.0.1:12345"
    elif mismatch == "host":
        plan["identity"]["activation"]["host"] = "secondary"
    elif mismatch == "configuration":
        binding["sha256"] = "0" * 64
    elif mismatch == "missing_baseline":
        plan["identity"]["installed"] = []
    if mismatch:
        with pytest.raises(deploy_mod.DeployError, match="release selection"):
            deploy_mod._validate_selected_activation(plan, tmp_path, V2_SERVICE)
    else:
        deploy_mod._validate_selected_activation(plan, tmp_path, V2_SERVICE)


@pytest.fixture
def admitted_transaction(isolated_release, tmp_path, monkeypatch):
    """Real admission/transaction/subprocesses; synthetic source, receipts and OS/provider peers."""
    import copy
    from functools import partial
    from .test_release_selection import SHA, _write, _execution_runner
    module, service, repo, prior, target, before, calls, activate = isolated_release
    s = module.release_selection
    machines = tmp_path / 'machines.json'
    machines.write_text(json.dumps([{'name': 'primary', 'ssh_target': None},
                                    {'name': 'secondary', 'ssh_target': 'secondary.example.invalid'}]))
    monkeypatch.setattr(module, '_launchd_environment', lambda _: {'PENTACLE_MACHINES_FILE': str(machines)})
    # Stabilize the existing managed stderr path before freezing its input bytes.
    module._ensure_launchd_standard_error_path(service, module._run)
    plist = module._launchd_plist_path(service.launchd_label)
    inputs = {'schema': 'pentacle.release-inputs.v1', 'candidate': target, 'installed': [],
              'external_inputs': {}, 'activation': {'url': 'ws://127.0.0.1:7791', 'host': 'primary'}}
    for role, component, sha in [('daemon', 'primary', prior), ('web', 'web', target),
                                 ('satellite', 'secondary', target), ('mobile', 'mobile', SHA)]:
        repository = s.PUBLIC_REPO if role != 'mobile' else 'https://github.com/HJK6/pentacle-mobile.git'
        row = {'component': component, 'role': role, 'repository': repository, 'sha': sha}
        row['receipt'] = _write(tmp_path / (role + '-observed.json'), {**row, 'observed': True})
        inputs['installed'].append(row)
    for label in s.EXTERNAL:
        value = {'identity': {'synthetic': True}, 'files': [_write(tmp_path / (label + '.json'), {'synthetic': True})]}
        if label == 'client_versions':
            value['identity']['mobile_source_shas'] = [SHA]
        if label == 'deploy_config':
            value['files'] += [{'path': str(p), 'sha256': s.digest(p.read_bytes())} for p in (machines, plist)]
        inputs['external_inputs'][label] = {'installed': copy.deepcopy(value), 'candidate': copy.deepcopy(value)}
    inputs['external_inputs']['deploy_config']['candidate']['identity']['revision'] = 'fixture-change'
    input_path = tmp_path / 'actual-inputs.json'
    python = str(module._venv_python(repo, service))
    subprocess_runner = module._run
    selected_calls = []
    gate_path = tmp_path / 'gate.json'
    def freeze():
        # Fixture scopes never alter production data or self-certify real qualification.
        candidate = inputs['candidate']
        _write(input_path, inputs)
        monkeypatch.setattr(s, 'QUALIFIED_SEAMS', {})
        module._checked(('git', 'checkout', '--detach', candidate), repo, subprocess_runner)
        plan = s.prepare(repo, candidate, input_path, python, subprocess_runner)
        synthetic = {}
        for boundary in plan['boundaries']:
            proof = {'source_ref': candidate, 'source_paths': ['scripts/', 'services/', 'test/'],
                     'input_labels': sorted(s.EXTERNAL), 'assertion_scope': ['synthetic fixture mechanics'],
                     'remaining_coverage': ['all runtime counterparts are synthetic'],
                     'qa_report': 'synthetic-only-not-qualification', 'evidence_sha256': 'f' * 64}
            context = s.qualification_context(repo, plan, boundary['id'], proof, subprocess_runner)
            proof['scope_sha256'] = s.digest(s.key(context).encode())
            synthetic[boundary['id']] = proof
        monkeypatch.setattr(s, 'QUALIFIED_SEAMS', synthetic)
        plan = s.prepare(repo, candidate, input_path, python, subprocess_runner)
        section = s.run_selected(plan, repo, python, _execution_runner(plan, selected_calls),
                                 tmp_path / 'workspace', tmp_path / 'synthetic-seams')
        gate = json.loads(gate_path.read_text())
        gate.update(sha=candidate, seam_results=section)
        gate['source'].update(sha_before=candidate, sha_after=candidate)
        _write(gate_path, gate)
        return section
    section = freeze()
    module._checked(('git', 'checkout', '--detach', prior), repo, subprocess_runner)
    real_apply = module._apply_post_activation.func
    monkeypatch.setattr(module, '_apply_post_activation', partial(real_apply,
        verify_boot=lambda *_a, **_k: module.BootReadback(module.BOOT_OBSERVED, 'synthetic boot peer', 0, 222, 111),
        verify_runtime=lambda sha, *_: (True, {'sha': sha, 'pid': 222, 'process_state': 'synthetic'})))
    monkeypatch.setattr(module, '_scan_slow_consumer_window', lambda *_a, **_k: {'outcome': 'passed', 'synthetic': True})
    monkeypatch.setattr(module, 'run_gate_and_write_evidence', lambda *_a, **_k: pytest.fail('duplicate merge gate'))
    monkeypatch.setattr(module, '_install_fleet_smoke_schedule', lambda *_: pytest.fail('canary cannot install full-matrix schedule'))
    cell = {'host': 'primary', 'provider': 'codex', 'prompt_mode': 'promptless', 'outcome': 'passed'}
    payload = {'returncode': 0, 'result': {'ok': True, 'status': 'PASS', 'cells': [cell]}}
    result_path = tmp_path / 'synthetic-provider-result.json'
    _write(result_path, payload)
    return {'module': module, 'service': service, 'repo': repo, 'prior': prior, 'target': target,
            'before': before, 'calls': calls, 'activate': lambda **kw: activate(release_inputs=input_path, **kw),
            'inputs': inputs, 'input_path': input_path, 'gate_path': gate_path, 'section': section,
            'plist': plist, 'plist_before': plist.read_bytes(), 'machines': machines,
            'payload': payload, 'result_path': result_path, 'selected_calls': selected_calls,
            'child': tmp_path / 'observed-child.json', 'freeze': freeze}


@pytest.mark.parametrize('isolated_release', ['selection'], indirect=True)
def test_real_admission_reuses_merge_and_dispatches_exact_affected_child(admitted_transaction, monkeypatch, request):
    t = admitted_transaction
    monkeypatch.setenv('PENTACLE_MACHINES_JSON', 'must-not-reach-child')
    monkeypatch.setenv('PENTACLE_SMOKE_HOSTS', 'must-not-reach-child')
    stamp = t['activate']()
    assert 'post_activation_error' not in stamp
    assert stamp['gates']['evidence_source'] == 'file' and stamp['selective_qualification'] == 'admitted'
    assert stamp['release_selection']['identity'] == t['section']['identity']
    assert {b['id'] for b in stamp['release_selection']['boundaries']} == {'client_wire_render', 'deploy_invocation'}
    child = json.loads(t['child'].read_text())
    assert child['argv'][1:] == ['ws://127.0.0.1:7791', 'primary']
    assert child['machines_file'] == str(t['machines'])
    assert child['ambient_hosts'] is None and child['ambient_json'] is None
    verdict = stamp['fleet_smoke']
    assert verdict['outcome'] == 'passed' and verdict['scope'] == 'qualified_affected'
    assert verdict['full_fleet_acceptance'] is False
    assert len(verdict['not_selected']) == 7 and {r['outcome'] for r in verdict['not_selected']} == {'UNTESTED'}
    assert len([c for c in t['calls'] if 'kickstart' in c]) == 1
    assert t['module']._read_stamp(t['repo'], t['service']) == stamp
    _record_transaction(request, t, stamp)


def _record_transaction(request, t, stamp=None):
    """Retain concrete counterpart calls/preimage hashes in the existing JUnit receipt."""
    module = t['module']
    request.node.user_properties.append(('isolated_deploy_control', json.dumps({
        'counterparts': ['synthetic installed/merge/seam/qualification receipts and source fixture',
                         'synthetic launchd/boot/runtime/slow-consumer/provider responses; real child argv/env capture'],
        'real': ['Git/fetch/checkout/source union', 'receipt admission/revalidation', 'transaction and preimage files',
                 'provider counterpart subprocess and saved merge loader'],
        'candidate': t['target'], 'prior_checkout': t['prior'],
        'after_checkout': module._git(t['repo'], 'rev-parse', 'HEAD', runner=module._run),
        'before_stamp_sha256': hashlib.sha256(t['before']).hexdigest(),
        'after_stamp_sha256': module._sha256(module._stamp_path(t['repo'], t['service'])),
        'before_plist_sha256': hashlib.sha256(t['plist_before']).hexdigest(),
        'after_plist_sha256': module._sha256(t['plist']),
        'selected_calls': t['selected_calls'], 'deployment_calls': t['calls'],
        'observed_child': json.loads(t['child'].read_text()) if t['child'].exists() else None,
        'fleet_verdict': stamp.get('fleet_smoke') if stamp else None,
    }, sort_keys=True)))


@pytest.mark.parametrize('isolated_release', ['selection'], indirect=True)
@pytest.mark.parametrize('corruption', ['missing_baseline', 'scenario', 'tuple'])
def test_real_admission_refuses_and_restores_before_any_activation(admitted_transaction, corruption, request):
    from .test_release_selection import _write
    t = admitted_transaction
    if corruption == 'scenario':
        gate = json.loads(t['gate_path'].read_text())
        row = next(r for r in gate['seam_results']['results'] if r['target']['target'] == 'web-gate')
        terminal_path = Path(row['evidence']['terminal']['path'])
        terminal = json.loads(terminal_path.read_text())
        log = Path(terminal['evidence'][0]['path'])
        log.write_text(log.read_text().replace('▸ installed-client-seam', '▸ another-valid-scenario'))
        terminal['evidence'][0]['sha256'] = t['module'].release_selection.digest(log.read_bytes())
        row['evidence']['terminal'] = _write(terminal_path, terminal)
        _write(t['gate_path'], gate)
        reason = 'scenario'
    elif corruption == 'missing_baseline':
        t['inputs']['installed'].pop()
        _write(t['input_path'], t['inputs'])
        reason = 'missing installed component'
    else:
        t['machines'].write_text('changed configuration')
        reason = 'changed bound file'
    with pytest.raises(t['module'].DeployError, match=reason):
        t['activate']()
    assert not any(c[0] == 'launchctl' for c in t['calls'])
    assert not t['child'].exists()
    assert t['module']._stamp_path(t['repo'], t['service']).read_bytes() == t['before']
    assert t['plist'].read_bytes() == t['plist_before']
    assert t['module']._git(t['repo'], 'rev-parse', 'HEAD', runner=t['module']._run) == t['prior']
    _record_transaction(request, t)


@pytest.mark.parametrize('isolated_release', ['selection'], indirect=True)
def test_real_revalidation_failure_restores_plist_stamp_and_checkout(admitted_transaction, monkeypatch, request):
    import plistlib
    t = admitted_transaction
    def drift(*_):
        # Mutation occurs after the real rollback preimage was registered and after admission.
        changed = plistlib.loads(t['plist'].read_bytes())
        changed['Label'] = 'changed-after-initial-admission'
        t['plist'].write_bytes(plistlib.dumps(changed))
        return False
    monkeypatch.setattr(t['module'], '_ensure_v2_usage_probe_launchd', drift)
    # The current plist was bound before admission, so the last input revalidation refuses.
    with pytest.raises(t['module'].DeployError, match='changed bound file'):
        t['activate']()
    assert t['plist'].read_bytes() == t['plist_before']
    assert t['module']._stamp_path(t['repo'], t['service']).read_bytes() == t['before']
    assert t['module']._git(t['repo'], 'rev-parse', 'HEAD', runner=t['module']._run) == t['prior']
    assert not any('kickstart' in c or (c[1:2] == ('bootstrap',) and c[-1] == str(t['plist'])) for c in t['calls'])
    assert any(c[1:2] == ('bootout',) and c[-1].endswith('/' + t['module'].USAGE_COLLECTOR_LABEL) for c in t['calls'])
    assert not t['child'].exists()
    _record_transaction(request, t)


@pytest.mark.parametrize('isolated_release', ['selection'], indirect=True)
@pytest.mark.parametrize('state', ['quota', 'cleanup', 'missing'])
def test_actual_child_non_green_cannot_certify_or_rollback_healthy_source(admitted_transaction, state, request):
    from .test_release_selection import _write
    t = admitted_transaction
    payload = t['payload']
    payload['returncode'] = 1 if state == 'cleanup' else 2
    payload['result']['status'] = 'FAIL' if state == 'cleanup' else 'UNTESTED'
    payload['result']['ok'] = False
    if state == 'missing':
        payload['result']['cells'] = []
    else:
        payload['result']['cells'][0].update(outcome='failed' if state == 'cleanup' else 'untested', reason=state)
    _write(t['result_path'], payload)
    stamp = t['activate']()
    assert stamp['fleet_smoke']['outcome'] in {'untested', 'failed'}
    assert 'post_activation_error' not in stamp
    assert t['module']._git(t['repo'], 'rev-parse', 'HEAD', runner=t['module']._run) == t['target']
    assert len([c for c in t['calls'] if 'kickstart' in c]) == 1
    assert t['module']._read_stamp(t['repo'], t['service']) == stamp
    _record_transaction(request, t, stamp)


@pytest.mark.parametrize('isolated_release', ['selection'], indirect=True)
def test_rollback_needs_fresh_receipts_and_uses_actual_recorded_preimage(admitted_transaction, request):
    from .test_release_selection import _write
    t = admitted_transaction
    first = t['activate']()
    assert first['prior_sha'] == t['prior'] and first['sha'] == t['target']
    # A target receipt cannot qualify the actual recorded rollback source.
    with pytest.raises(t['module'].DeployError, match='stale seam identity|candidate mismatch'):
        t['activate'](rollback=True)
    assert len([c for c in t['calls'] if 'kickstart' in c]) == 1
    t['inputs']['candidate'] = t['prior']
    for row in t['inputs']['installed']:
        if row['role'] != 'mobile':
            row['sha'] = t['target']
            row['receipt'] = _write(Path(row['receipt']['path']),
                {**{k: row[k] for k in ('component', 'repository', 'sha')}, 'observed': True})
    t['freeze']()
    t['module']._checked(('git', 'checkout', '--detach', t['target']), t['repo'], t['module']._run)
    rolled = t['activate'](rollback=True)
    assert rolled['sha'] == t['prior'] and rolled['prior_sha'] == t['target']
    assert 'post_activation_error' not in rolled
    assert rolled['fleet_smoke']['outcome'] == 'passed'
    assert t['module']._git(t['repo'], 'rev-parse', 'HEAD', runner=t['module']._run) == t['prior']
    assert len([c for c in t['calls'] if 'kickstart' in c]) == 2
    _record_transaction(request, t, rolled)
