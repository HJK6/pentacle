from concurrent.futures import ThreadPoolExecutor

from agent_orch.quarantine_transition import reconcile


TEST = 'tests/test_example.py::test_case'
OCCURRENCES = [{'id': 'public-checks/1/1', 'source': 'run/1'},
               {'id': 'public-checks/2/1', 'source': 'run/2'}]


class Adapter:
    def __init__(self):
        self.items = {}
        self.relayed = {}
        self.notices = {}
        self.alerts = {}
        self.fail_relay = False

    def decision(self, test, occurrences):
        self.items.setdefault(test, {'item': 'repo__decision', 'owner': 'owner', 'coverage': 'example behavior'})
        return self.items[test]

    def prepare_relay(self, selected, key, pending):
        if self.fail_relay:
            raise OSError('relay unavailable')
        self.relayed.setdefault(key, sorted(selected))
        return {'path': 'bundle', 'sha256': 'a' * 64}

    def send_relay(self, test, entry):
        pass

    def notify(self, key, payload):
        self.notices.setdefault(key, payload)

    def alert(self, key, message):
        self.alerts.setdefault(key, message)

    def verify_restoration(self, test, restoration):
        return restoration.get('verified') is True


def test_replay_recovers_relay_after_decision_write(tmp_path):
    adapter = Adapter()
    adapter.fail_relay = True
    state = tmp_path / 'state.json'
    reconcile(state, {TEST: OCCURRENCES}, set(), adapter)
    assert len(adapter.items) == 1 and not adapter.relayed and len(adapter.alerts) == 1
    adapter.fail_relay = False
    reconcile(state, {TEST: OCCURRENCES}, set(), adapter)
    reconcile(state, {TEST: OCCURRENCES}, set(), adapter)
    assert len(adapter.items) == len(adapter.relayed) == 1 and not adapter.notices
    reconcile(state, {TEST: OCCURRENCES}, {TEST}, adapter)
    reconcile(state, {TEST: OCCURRENCES}, {TEST}, adapter)
    assert len(adapter.notices) == 1
    assert adapter.notices[next(iter(adapter.notices))]['occurrences'] == ['run/1', 'run/2']


def test_seed_replay_does_not_reactivate_or_notify(tmp_path):
    adapter = Adapter()
    adapter.items[TEST] = {'item': 'existing', 'owner': 'owner', 'coverage': 'existing'}
    reconcile(tmp_path / 'state.json', {TEST: OCCURRENCES}, {TEST}, adapter)
    assert not adapter.relayed and not adapter.notices


def test_one_or_duplicate_occurrence_never_activates(tmp_path):
    adapter = Adapter()
    reconcile(tmp_path / 'state.json', {TEST: [OCCURRENCES[0], OCCURRENCES[0]]}, set(), adapter)
    assert not adapter.items and not adapter.relayed


def test_concurrent_notifications_converge(tmp_path):
    adapter = Adapter()
    state = tmp_path / 'state.json'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reconcile, state, {TEST: OCCURRENCES}, set(), adapter) for _ in range(2)]
        for future in futures:
            future.result()
    assert len(adapter.items) == len(adapter.relayed) == 1


def test_old_series_does_not_reactivate_after_restoration(tmp_path):
    adapter = Adapter()
    state = tmp_path / 'state.json'
    adapter.items[TEST] = {'item': 'existing', 'owner': 'owner', 'coverage': 'existing'}
    reconcile(state, {TEST: OCCURRENCES}, {TEST}, adapter)
    reconcile(state, {TEST: OCCURRENCES}, set(), adapter)
    assert not adapter.relayed


def test_new_series_requires_verified_restoration_and_later_occurrences(tmp_path):
    adapter = Adapter()
    state = tmp_path / 'state.json'
    adapter.items[TEST] = {'item': 'existing', 'owner': 'owner', 'coverage': 'existing'}
    reconcile(state, {TEST: OCCURRENCES}, {TEST}, adapter)
    newer = [{'id': f'public-checks/{i}/1', 'source': f'run/{i}', 'created': f'2026-11-0{i}T00:00:00Z'}
             for i in (3, 4)]
    restoration = {TEST: {'at': '2026-11-02T12:00:00Z', 'verified': True, 'evidence': 'receipt'}}
    result = reconcile(state, {TEST: newer}, set(), adapter, restorations=restoration)
    assert len(adapter.relayed) == 1
    assert result['tests'][TEST]['previous']['phase'] == 'adopted'


def test_verified_history_without_local_state_can_recur(tmp_path):
    adapter = Adapter()
    test = 'daemon::tests/test_example.py::test_case'
    proof = {test: {'at': '2026-11-02T12:00:00Z', 'verified': True}}
    newer = [dict(row, created='2026-11-03T00:00:00Z') for row in OCCURRENCES]
    result = reconcile(tmp_path / 'state.json', {test: newer}, set(), adapter,
                       restorations=proof, history={test})
    assert result['tests'][test]['previous']['phase'] == 'adopted'
    assert len(adapter.relayed) == 1 and not adapter.alerts


def test_reviewed_row_creates_no_decision_and_removal_requires_restoration(tmp_path):
    adapter = Adapter()
    state = tmp_path / 'state.json'
    reconcile(state, {TEST: OCCURRENCES}, {TEST}, adapter)
    assert not adapter.items and not adapter.relayed and not adapter.alerts
    reconcile(state, {TEST: OCCURRENCES}, set(), adapter)
    assert not adapter.items and not adapter.relayed


def test_node_recurrence_is_notice_only(tmp_path):
    adapter = Adapter()
    test = 'test/example.test.js::["a b","c"]'
    reconcile(tmp_path / 'state.json', {test: OCCURRENCES}, set(), adapter)
    assert not adapter.items and not adapter.relayed
    assert list(adapter.alerts.values()) == ['Node test needs review: ' + test]
