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
