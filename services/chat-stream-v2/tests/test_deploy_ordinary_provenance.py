"""Amendment-1 synthetic writer and verifier evidence, never live fixtures."""
import copy
import pytest
from deploy import restoration as r
from deploy import coordinated as c
from .test_deploy_prior_ancestry import BINDING, PRIOR, ANCESTOR, sealed_receipt, forward_qualified, with_ancestry


def ordinary(tmp_path, target=PRIOR):
    path = tmp_path / 'receipt.json'
    receipt = sealed_receipt(path, PRIOR)
    stamp = with_ancestry(forward_qualified(target), str(path))
    stamp['prior_sha'] = PRIOR
    r.complete_ordinary_qualification(stamp, receipt['receipt_sha256'])
    return stamp


@pytest.mark.parametrize('target', [PRIOR, ANCESTOR])
def test_new_writer_roundtrip(tmp_path, target):
    stamp = ordinary(tmp_path, target)
    result = r.qualified_prior(stamp, target, [BINDING])
    assert result['ancestry_receipt'] == stamp['coordinated_receipt']
    assert 'receipt' not in result
    retained = r.qualification_stamp(stamp)
    assert r.qualified_prior(retained, target, [BINDING])['ancestry_receipt'] == result['ancestry_receipt']


def test_actual_old_same_target_shape_stays_rejected(tmp_path):
    sealed_receipt(tmp_path/'receipt.json', PRIOR)
    stamp = with_ancestry(fleet_stamp(PRIOR), str(tmp_path/'receipt.json'))
    stamp['prior_qualification']['stamp']['fleet_smoke'] = {'outcome':'failed', 'cells':[]}
    with pytest.raises(c.Invalid):
        r.qualified_prior(stamp, PRIOR, [BINDING])

from .test_deploy_log_guard import isolated_release


def fleet_stamp(sha):
    stamp = forward_qualified(sha)
    stamp['fleet_smoke']['cells'] = []
    for host in ('fixture', 'peer'):
        for provider in ('claude', 'codex'):
            binding = dict(BINDING, host=host, provider=provider)
            for mode in ('prompted', 'promptless'):
                stamp['fleet_smoke']['cells'].append(dict(host=host, provider=provider,
                    prompt_mode=mode, outcome='passed', account_binding=binding))
    return stamp


@pytest.mark.parametrize('same', [True, False])
def test_actual_ordinary_writer_roundtrip(isolated_release, monkeypatch, tmp_path, same):
    d, service, repo, prior, target, before, calls, activate = isolated_release
    path = tmp_path/'inherited.json'
    receipt = sealed_receipt(path, prior)
    previous = d._read_stamp(repo, service)
    previous.update(sha=prior, coordinated_receipt=str(path))
    # Bootstrap is allowed to retain unqualified history, never qualify it.
    previous.update(fleet_smoke={'outcome':'failed', 'cells':[]})
    d._write_stamp(repo, service, previous)
    def checks(service, repo, sha, stamp, **kwargs):
        stamp.update(fleet_smoke=fleet_stamp(sha)['fleet_smoke'], disposition='active and checks passed')
    monkeypatch.setattr(d, 'run_post_release_checks', checks)
    result = activate(same_ref=same)
    saved = d._read_stamp(repo, service)
    assert saved == result
    bindings = [row['account_binding'] for row in saved['fleet_smoke']['cells'] if row['prompt_mode']=='prompted']
    assert len(bindings) == 4
    reference = r.qualified_prior(saved, prior if same else target, bindings)
    assert reference['ancestry_receipt_sha256'] == receipt['receipt_sha256']
    assert 'receipt_sha256' not in saved
    assert saved['ordinary_provenance']['prior_sha'] == prior


@pytest.mark.parametrize('field', ['version', 'kind', 'sha', 'prior_sha', 'receipt', 'receipt_sha256', 'qualification_sha256'])
@pytest.mark.parametrize('operation', ['remove', 'alter'])
def test_record_each_field_rejected(tmp_path, field, operation):
    stamp = ordinary(tmp_path)
    if operation == 'remove': del stamp['ordinary_provenance'][field]
    else: stamp['ordinary_provenance'][field] = 'bad'
    with pytest.raises(c.Invalid): r.qualified_prior(stamp, PRIOR, [BINDING])


@pytest.mark.parametrize('field', ('host', 'provider', 'alias', 'generation', 'executable_sha256', 'version'))
def test_every_binding_mismatch_rejected(tmp_path, field):
    stamp = ordinary(tmp_path)
    binding = dict(BINDING); binding[field] = 'other'
    with pytest.raises(c.Invalid): r.qualified_prior(stamp, PRIOR, [binding])


@pytest.mark.parametrize('change', ['strip', 'null', 'extra', 'bool_version', 'direct', 'source',
    'prior_source', 'nested_source', 'nested_pointer', 'pointer', 'digest', 'boot', 'runtime',
    'cell', 'failed_extra_cell', 'missing_mode', 'duplicate', 'receipt_bytes', 'missing_receipt', 'stale_receipt'])
def test_inconsistent_shapes_rejected(tmp_path, change):
    stamp = ordinary(tmp_path)
    record = stamp['ordinary_provenance']
    if change == 'strip': del stamp['ordinary_provenance']
    elif change == 'null': stamp['ordinary_provenance'] = None
    elif change == 'extra': record['unknown'] = True
    elif change == 'bool_version': record['version'] = True
    elif change == 'direct': stamp['receipt_sha256'] = record['receipt_sha256']
    elif change == 'source': stamp['sha'] = ANCESTOR
    elif change == 'prior_source': stamp['prior_sha'] = ANCESTOR
    elif change == 'nested_source': stamp['prior_qualification']['stamp']['sha'] = ANCESTOR
    elif change == 'nested_pointer': stamp['prior_qualification']['stamp']['coordinated_receipt'] = 'other'
    elif change == 'pointer': del stamp['coordinated_receipt']
    elif change == 'digest': record['qualification_sha256'] = '0'*64
    elif change == 'boot': stamp['daemon_boot_readback']['pid'] = 123
    elif change == 'runtime': stamp['daemon_runtime_readback']['pid'] = 123
    elif change == 'cell': stamp['fleet_smoke']['cells'][0]['account_binding']['version'] = 'different'
    elif change == 'failed_extra_cell': stamp['fleet_smoke']['cells'].append({'outcome':'failed'})
    elif change == 'missing_mode': stamp['fleet_smoke']['cells'].pop()
    elif change == 'duplicate': stamp['fleet_smoke']['cells'].append(copy.deepcopy(stamp['fleet_smoke']['cells'][0]))
    elif change == 'receipt_bytes': (tmp_path/'receipt.json').write_text('{"truncated":')
    elif change == 'missing_receipt': (tmp_path/'receipt.json').unlink()
    elif change == 'stale_receipt': sealed_receipt(tmp_path/'receipt.json', ANCESTOR)
    with pytest.raises(c.Invalid): r.qualified_prior(stamp, PRIOR, [BINDING])


def test_receipt_changes_between_capture_and_writer_refuses(tmp_path):
    stamp = ordinary(tmp_path)
    captured = stamp.pop('ordinary_provenance')['receipt_sha256']
    sealed_receipt(tmp_path/'receipt.json', ANCESTOR)
    with pytest.raises(c.Invalid): r.complete_ordinary_qualification(stamp, captured)
    assert 'ordinary_provenance' not in stamp


def test_identical_records_have_identical_verdicts(tmp_path):
    stamp = ordinary(tmp_path)
    assert r.qualified_prior(copy.deepcopy(stamp), PRIOR, [BINDING]) == r.qualified_prior(stamp, PRIOR, [BINDING])

import json
import os
import subprocess
import sys
from pathlib import Path
from .test_deploy_script import deploy_mod as d


@pytest.mark.parametrize('stage', ['partial', 'before_replace', 'after_replace'])
def test_true_process_loss_at_atomic_publication(tmp_path, stage):
    stamp = ordinary(tmp_path)
    service = d.SERVICES['chat-streamd-v2']
    d._write_stamp(tmp_path, service, {'sha':PRIOR, 'prepared_only':True})
    path = d._stamp_path(tmp_path, service)
    before = path.read_bytes()
    payload = tmp_path/'payload.json'; payload.write_text(json.dumps(stamp))
    script = r'''
import json, os, sys
from pathlib import Path
from deploy import deploy as d
root, payload, stage = map(str, sys.argv[1:])
replace = os.replace
if stage == 'partial':
    original = os.fdopen
    class Interrupted:
        def __init__(self, *a, **kw): self.stream = original(*a, **kw)
        def __enter__(self): return self
        def __exit__(self, *a): self.stream.close()
        def write(self, data):
            self.stream.write(data[:len(data)//2]); self.stream.flush(); os.fsync(self.stream.fileno())
            os._exit(71)
    os.fdopen = Interrupted
else:
    def interrupted(src, dst):
        if stage == 'after_replace': replace(src, dst)
        os._exit(71)
    os.replace = interrupted
d._write_stamp(Path(root), d.SERVICES['chat-streamd-v2'], json.loads(Path(payload).read_text()))
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), str(payload), stage],
        env={**os.environ, 'PYTHONPATH':os.pathsep.join(sys.path)}, capture_output=True, timeout=10)
    assert result.returncode == 71, result.stderr.decode()
    if stage == 'after_replace':
        assert r.qualified_prior(d._read_stamp(tmp_path, service), PRIOR, [BINDING])['ancestry_receipt']
    else:
        assert path.read_bytes() == before
        with pytest.raises(c.Invalid): r.qualified_prior(d._read_stamp(tmp_path, service), PRIOR, [BINDING])
        # A partially written temporary file is not treated as the stamp.
        assert list(path.parent.glob('.*.tmp'))


@pytest.mark.parametrize('failure', ['smoke', 'publish'])
def test_serving_failure_stays_active_but_unqualified(isolated_release, monkeypatch, tmp_path, failure):
    module, service, repo, prior, target, before, calls, activate = isolated_release
    path = tmp_path/'inherited.json'; sealed_receipt(path, prior)
    previous = module._read_stamp(repo, service)
    previous.update(sha=prior, coordinated_receipt=str(path))
    module._write_stamp(repo, service, previous)
    def checks(service, repo, sha, stamp, **kwargs):
        smoke = fleet_stamp(sha)['fleet_smoke']
        if failure == 'smoke': smoke['outcome'] = 'failed'
        stamp.update(fleet_smoke=smoke, disposition='active with failed checks' if failure=='smoke' else 'active and checks passed')
    monkeypatch.setattr(module, 'run_post_release_checks', checks)
    actual_write = module._write_stamp
    def write(repo, service, stamp):
        if failure == 'publish' and stamp.get('serving'): raise OSError('synthetic disk full')
        actual_write(repo, service, stamp)
    monkeypatch.setattr(module, '_write_stamp', write)
    result = activate()
    assert result['serving'] and result['disposition'] == 'active with failed checks'
    assert 'automatic_restore' not in result
    assert subprocess.check_output(['git','rev-parse','HEAD'], cwd=repo, text=True).strip() == target
    saved = module._read_stamp(repo, service)
    with pytest.raises(c.Invalid): r.qualified_prior(saved, target, [BINDING])
    assert 'ordinary_provenance' not in saved


def test_controller_loss_does_not_leave_durable_ordinary_preimages(tmp_path):
    """Recovery GAP rehearsal: os._exit bypasses in-memory context cleanup."""
    script = r'''
import os, sys
from pathlib import Path
from deploy import deploy as d
root = Path(sys.argv[1]); stamp = root/'stamp.json'; policy=root/'policy.json'
stamp.write_bytes(b'prior stamp'); policy.write_bytes(b'prior policy'); policy.chmod(0o640)
with d._PreActivationTransaction(root, 'a'*40, stamp, stamp.read_bytes(), None) as tx:
    tx.remember_file(policy)
    stamp.write_bytes(b'target prepared'); policy.write_bytes(b'target policy')
    tx.commit()
    os._exit(72)
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
        env={**os.environ, 'PYTHONPATH':os.pathsep.join(sys.path)}, capture_output=True, timeout=10)
    assert result.returncode == 72, result.stderr.decode()
    assert (tmp_path/'stamp.json').read_bytes() == b'target prepared'
    assert (tmp_path/'policy.json').read_bytes() == b'target policy'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['policy.json','stamp.json']
    # No recovery success or accepted qualification can be inferred from these bytes.
    with pytest.raises(c.Invalid): c.read_json(tmp_path/'stamp.json')


@pytest.mark.parametrize('tamper', [False, True])
def test_explicit_ordinary_restoration_retains_forward_provenance(tmp_path, monkeypatch, tamper):
    from types import SimpleNamespace
    from deploy import coordinated_runtime as cr
    forward = ordinary(tmp_path)
    retained = r.qualification_stamp(forward)
    if tamper: retained['ordinary_provenance']['qualification_sha256'] = '0'*64
    restoring = {'sha':PRIOR, 'restored_qualification':{'sha':PRIOR, 'stamp':retained}}
    policy = tmp_path/'policy.json'; c.atomic_json(policy, {})
    monkeypatch.setattr(d, '_launchd_plist', lambda *a:{'EnvironmentVariables':{'PENTACLE_PROVIDER_ACCOUNT_POLICY':str(policy)}})
    monkeypatch.setattr(c, 'host_scope', lambda *a:{'included_hosts':['fixture']})
    monkeypatch.setattr(cr, 'code_identity_at', lambda *a:'synthetic-identity')
    calls = []
    runtime = SimpleNamespace(database=lambda *a: 'synthetic-db',
        account_snapshot=lambda *a, **kw:{'selected_bindings':[BINDING]},
        restoration_proof=lambda *a: calls.append('zero_input') or {'outcome':'passed', 'mode':'zero_input'})
    if tamper:
        with pytest.raises(c.Invalid): d._verify_direct_restoration(d.SERVICES['chat-streamd-v2'], tmp_path, restoring, None, None, runtime)
        assert not calls
    else:
        result = d._verify_direct_restoration(d.SERVICES['chat-streamd-v2'], tmp_path, restoring, None, None, runtime)
        assert result['mode'] == 'zero_input' and calls == ['zero_input']
        assert restoring['restored_qualification']['stamp'] == retained
        assert 'fleet_smoke' not in restoring


@pytest.mark.parametrize('shape', [None, [], {'fleet_smoke':None}, {'sha':PRIOR,'fleet_smoke':{'outcome':'passed','cells':[None]}}])
def test_malformed_qualification_refuses(shape):
    with pytest.raises(c.Invalid): r.qualified_prior(shape, PRIOR, [BINDING])


def test_direct_claim_cannot_contradict_ordinary_ancestry(tmp_path):
    stamp = ordinary(tmp_path)
    seal = stamp.pop('ordinary_provenance')['receipt_sha256']
    stamp['receipt_sha256'] = seal
    with pytest.raises(c.Invalid): r.qualified_prior(stamp, PRIOR, [BINDING])


def test_stripping_record_and_pointer_does_not_hide_retained_ancestry(tmp_path):
    stamp = ordinary(tmp_path)
    del stamp['ordinary_provenance']; del stamp['coordinated_receipt']
    with pytest.raises(c.Invalid): r.qualified_prior(stamp, PRIOR, [BINDING])

from .test_deploy_prior_helper_reuse import legacy_prior
from .test_deploy_coordinated_helpers import runtime_preparation


@pytest.mark.parametrize('tamper', [False, True])
def test_coordinated_prepare_and_rollback_revalidate_inherited_record(legacy_prior, tamper):
    case = legacy_prior
    runtime, fixture, intent, root = case.runtime, case.fixture, case.intent, case.root
    stamp = runtime.d._read_stamp(fixture.repo, runtime.service)
    seal = stamp.pop('receipt_sha256')
    stamp.update(prior_sha=fixture.prior, prior_qualification={'sha':fixture.prior,
        'stamp':{'sha':fixture.prior, 'coordinated_receipt':stamp['coordinated_receipt']}})
    r.complete_ordinary_qualification(stamp, seal)
    runtime.d._write_stamp(fixture.repo, runtime.service, stamp)
    folder = root/'ordinary-coordinated'; folder.mkdir(mode=0o700)
    prepared = runtime.prepare(intent, folder)
    assert prepared['prior_qualification']['ancestry_receipt_sha256'] == seal
    receipt = {'intent':intent, 'transaction_directory':str(folder), **prepared}
    runtime.guard = lambda *a, **k:{'sole_deployer':True}
    runtime.revalidate(receipt, forward=False)
    if tamper:
        # Reach the semantic verifier, not merely the saved-file hash check.
        row = receipt['preimages']['stamp']
        altered = c.read_json(row['saved'])
        altered['ordinary_provenance']['qualification_sha256'] = '0'*64
        c.atomic_json(row['saved'], altered)
        row['sha256'] = c.file_digest(row['saved'])
        with pytest.raises(c.Invalid, match='receipt reference invalid'):
            runtime.validate_common(receipt, forward=False)
    assert not runtime.stopped


# --- declared host exclusions (SKIPPED_DECLARED) ------------------------------------------------

AUTHORITY = 'pentacle__merlin_native_provider_readiness_failure_2026_11'


def declared_rows(host='merlin'):
    return [dict(host=host, provider=provider, prompt_mode=mode, outcome='SKIPPED_DECLARED',
                 reason='native provider readiness failure', authority=AUTHORITY)
            for provider in ('claude', 'codex') for mode in ('prompted', 'promptless')]


def declared_stamp(sha=PRIOR, hosts=('thoth', 'amaterasu')):
    """The live 4f67fceb shape: 8 passed cells plus 4 declared merlin skips, no skip binding."""
    stamp = forward_qualified(sha)
    smoke = stamp['fleet_smoke']
    smoke['cells'] = []
    bindings = []
    for host in hosts:
        for provider in ('claude', 'codex'):
            binding = dict(BINDING, host=host, provider=provider)
            bindings.append(binding)
            for mode in ('prompted', 'promptless'):
                smoke['cells'].append(dict(host=host, provider=provider, prompt_mode=mode,
                                           outcome='passed', account_binding=dict(binding)))
    skips = declared_rows()
    smoke['cells'].extend(copy.deepcopy(skips))
    smoke.update(skipped=skips, not_selected=[])
    return stamp, bindings


def write_declared(tmp_path, mutate=None):
    path = tmp_path / 'receipt.json'
    receipt = sealed_receipt(path, PRIOR)
    stamp, bindings = declared_stamp()
    stamp = with_ancestry(stamp, str(path))
    stamp['prior_sha'] = PRIOR
    if mutate:
        mutate(stamp)
    r.complete_ordinary_qualification(stamp, receipt['receipt_sha256'])
    return stamp, bindings


def test_declared_skips_yield_record_and_direct_restoration_accepts(tmp_path):
    stamp, bindings = write_declared(tmp_path)
    assert stamp['ordinary_provenance']['kind'] == 'ordinary_inherited'
    assert len(bindings) == 4
    reference = r.qualified_prior(stamp, PRIOR, bindings)
    assert reference['ancestry_receipt'] == stamp['coordinated_receipt']
    retained = r.qualification_stamp(stamp)
    assert r.qualified_prior(retained, PRIOR, bindings) == reference


def test_declared_skip_hosts_may_be_not_selected(tmp_path):
    def move(stamp):
        smoke = stamp['fleet_smoke']
        smoke['not_selected'], smoke['skipped'] = smoke['skipped'], []
    stamp, bindings = write_declared(tmp_path, move)
    assert r.qualified_prior(stamp, PRIOR, bindings)['ancestry_receipt'] == stamp['coordinated_receipt']


def test_declared_skip_record_matches_all_passed_digest_rule(tmp_path):
    """The skip rows are part of the digested payload; tampering with them refuses."""
    stamp, bindings = write_declared(tmp_path)
    stamp['fleet_smoke']['cells'][-1]['reason'] = 'edited after sealing'
    with pytest.raises(c.Invalid):
        r.qualified_prior(stamp, PRIOR, bindings)


def undeclared_host(stamp):
    stamp['fleet_smoke']['skipped'] = []


def other_outcome(stamp):
    stamp['fleet_smoke']['cells'][-1]['outcome'] = 'SKIPPED_OFFLINE'


def no_authority(stamp):
    for row in stamp['fleet_smoke']['cells'][-4:]:
        del row['authority']


def blank_authority(stamp):
    stamp['fleet_smoke']['cells'][-1]['authority'] = ''


def no_reason(stamp):
    del stamp['fleet_smoke']['cells'][-1]['reason']


def nonstring_reason(stamp):
    stamp['fleet_smoke']['cells'][-1]['reason'] = 7


def failed_cell(stamp):
    stamp['fleet_smoke']['cells'][0]['outcome'] = 'failed'


def unpaired_mode(stamp):
    del stamp['fleet_smoke']['cells'][1]


def all_skipped(stamp):
    smoke = stamp['fleet_smoke']
    smoke['cells'] = [dict(row, outcome='SKIPPED_DECLARED', reason='r', authority=AUTHORITY)
                      for row in smoke['cells']]
    smoke['skipped'] = copy.deepcopy(smoke['cells'])
    for row in smoke['cells']:
        row.pop('account_binding', None)


@pytest.mark.parametrize('mutate', [undeclared_host, other_outcome, no_authority, blank_authority, no_reason,
                                    nonstring_reason, failed_cell, unpaired_mode, all_skipped])
def test_writer_refuses_undeclared_or_failed_cells(tmp_path, mutate):
    path = tmp_path / 'receipt.json'
    receipt = sealed_receipt(path, PRIOR)
    stamp, _ = declared_stamp()
    stamp = with_ancestry(stamp, str(path))
    stamp['prior_sha'] = PRIOR
    mutate(stamp)
    with pytest.raises(c.Invalid, match='invalid ordinary inherited qualification'):
        r.complete_ordinary_qualification(stamp, receipt['receipt_sha256'])
    assert 'ordinary_provenance' not in stamp


@pytest.mark.parametrize('mutate', [undeclared_host, other_outcome, no_authority, no_reason, failed_cell,
                                    all_skipped])
def test_prior_refuses_undeclared_or_failed_cells_even_after_sealing(tmp_path, mutate):
    """A good record sealed first, then its smoke edited: the verifier refuses on its own."""
    stamp, bindings = write_declared(tmp_path)
    mutate(stamp)
    with pytest.raises(c.Invalid):
        r.qualified_prior(stamp, PRIOR, bindings)


@pytest.mark.parametrize('mutate', [undeclared_host, other_outcome, no_authority, no_reason, failed_cell])
def test_direct_prior_refuses_undeclared_or_failed_cells(tmp_path, mutate):
    """Direct (receipt-sealed) priors share the coverage rule; no ordinary record to catch it."""
    path = tmp_path / 'receipt.json'
    receipt = sealed_receipt(path, PRIOR)
    stamp, bindings = declared_stamp()
    stamp.update(coordinated_receipt=str(path), receipt_sha256=receipt['receipt_sha256'])
    assert r.qualified_prior(stamp, PRIOR, bindings)['receipt_sha256'] == receipt['receipt_sha256']
    mutate(stamp)
    with pytest.raises(c.Invalid):
        r.qualified_prior(stamp, PRIOR, bindings)
