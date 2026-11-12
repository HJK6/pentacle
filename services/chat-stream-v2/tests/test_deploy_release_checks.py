"""A serving coordinated candidate is never rolled back by late checks."""
import json
import pytest
from deploy import coordinated as c
from .test_deploy_coordinated_helpers import transaction


@pytest.mark.parametrize('late', ['failed', 'raises'])
def test_committed_candidate_survives_late_checks(transaction, late):
    tx, adapter, intent, receipt = transaction
    tx.prepare(intent, receipt)
    def post_checks(receipt, result):
        assert tx.load(receipt['receipt_path'])[1]['phase'] == 'committed'
        if late == 'raises':
            raise RuntimeError('late checker unavailable')
        return {'outcome': 'failed', 'fleet_smoke': {'outcome': 'failed'}}
    adapter.post_checks = post_checks
    result = tx.apply(intent, receipt)
    assert result['phase'] == 'committed'
    assert result['outcome'] == 'failed'
    assert result['disposition'] == 'active with failed checks'
    assert adapter.source == 'target'
    assert 'switch_prior' not in adapter.calls


def test_refused_prepare_removes_only_new_transaction(transaction):
    tx, adapter, intent, receipt = transaction
    adapter.failures.add('prepare')
    with pytest.raises(RuntimeError, match='prepare'):
        tx.prepare(intent, receipt)
    folder = tx.root / json.loads(intent.read_text())['transaction_id']
    assert not folder.exists()
    adapter.failures.clear()
    assert tx.prepare(intent, receipt)['phase'] == 'prepared'


def test_coordinated_ancestry_allows_identity_equal_ordinary_rollback(transaction):
    tx, adapter, intent, receipt = transaction
    assert c.ordinary_guard(adapter.repo, {'coordinated_receipt': '/old/receipt'},
                            rollback=True, helper_identity_equal=True) == '/old/receipt'
    with pytest.raises(c.Invalid, match='coordinated ancestry'):
        c.ordinary_guard(adapter.repo, {'coordinated_receipt': '/old/receipt'}, rollback=True)


@pytest.mark.parametrize('row,smoke,logs', [
    ('f099-first', None, 'failed'),
    ('f099-second', None, 'failed'),
    ('eadfb', None, 'failed'),
    ('151067', 'failed', 'passed'),
])
def test_historical_reports_cannot_veto_explicit_serving_precondition(transaction, row, smoke, logs):
    """Sanitized replay, not a claim that reconstructed historic boots passed.

    Missing original checks remain unknown. The replay explicitly assumes the
    boot/exact-SHA/helper-dispatch serving precondition has passed.
    """
    tx, adapter, intent, receipt = transaction
    tx.prepare(intent, receipt)
    def reports(*args):
        return {'outcome':'failed', 'historical_row':row,
                'fleet_smoke': {'outcome':smoke},
                'slow_consumer': {'outcome':logs},
                'front_desk': {'outcome':None}}
    adapter.post_checks = reports
    result = tx.apply(intent, receipt)
    journal = tx.load(receipt)[1]
    assert result['phase'] == 'committed'
    assert result['disposition'] == 'active with failed checks'
    assert journal['result']['fleet_smoke']['outcome'] == smoke
    assert journal['result']['front_desk']['outcome'] is None
    assert adapter.source == 'target' and 'switch_prior' not in adapter.calls


def test_refused_prepare_preserves_existing_directory(transaction):
    tx, adapter, intent, receipt = transaction
    folder = tx.root / json.loads(intent.read_text())['transaction_id']
    folder.mkdir(mode=0o700)
    sentinel = folder / 'unowned'; sentinel.write_text('preserve')
    with pytest.raises(c.Invalid, match='already exists'):
        tx.prepare(intent, receipt)
    assert sentinel.read_text() == 'preserve'


def test_prepared_transaction_has_private_modes(transaction):
    import stat
    tx, adapter, intent, receipt = transaction
    tx.prepare(intent, receipt)
    folder = tx.root / json.loads(intent.read_text())['transaction_id']
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert stat.S_IMODE((folder/'journal.json').stat().st_mode) == 0o600


@pytest.mark.parametrize('old_pid,expected', [(None, True), (123, False)])
def test_coordinated_reports_preserve_outgoing_writer_basis(tmp_path, old_pid, expected):
    from types import SimpleNamespace
    from deploy.coordinated_runtime import Runtime
    runtime = object.__new__(Runtime)
    runtime.repo, runtime.service = tmp_path, object()
    runtime.runner = lambda *a: pytest.fail('no native execution')
    runtime.old_pid, runtime.old_start, runtime.log_offsets = old_pid, None, {}
    observed = []
    def checks(*args, **kwargs):
        observed.append(kwargs['no_outgoing_writer'])
        return {'fleet_smoke':{'outcome':'passed'}, 'slow_consumer':{'outcome':'passed'}, 'front_desk':{'outcome':'passed'}}
    runtime.d = SimpleNamespace(_read_stamp=lambda *a:{}, _write_stamp=lambda *a:None,
                               BootReadback=lambda **k:object(), run_post_release_checks=checks)
    result = runtime.post_checks({'source':{'target':{'sha':'a'*40}}}, {'boot':{}})
    assert result['outcome'] == 'passed' and observed == [expected]
