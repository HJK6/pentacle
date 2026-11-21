import json
from pathlib import Path

import pytest

from agent_orch.quarantine_decisions import DecisionStore, MemoryPublisher


TEST = 'tests/test_a.py::test_case'
OCCURRENCES = [
    {'id': 'public-checks/10/1', 'source': 'https://example.test/10', 'created': '2026-11-01T00:00:00Z'},
    {'id': 'public-checks/11/1', 'source': 'https://example.test/11', 'created': '2026-11-02T00:00:00Z'},
]


class Publisher:
    def __init__(self, root):
        self.root = root
        self.creates = 0
        self.updates = 0

    def create(self, item, test):
        folder = self.root / 'backlog' / item
        if folder.exists():
            return
        self.creates += 1
        folder.mkdir(parents=True)
        (folder / 'summary.md').write_text(f"---\nid: work_{item}\nstatus: backlog\nowner: owner\nupdated_at: '2026-11-01'\n---\n")
        (folder / 'spec.md').write_text('---\nid: spec_x\n---\n## Tracking\n')

    def attach(self, item, test, occurrences):
        self.updates += 1


def test_new_decision_is_reused_and_register_has_one_occurrence_each(tmp_path):
    root = tmp_path / 'work'
    register = tmp_path / 'register.json'
    register.write_text(json.dumps({'records': [], 'pending': []}))
    publisher = Publisher(root)
    store = DecisionStore(root, register, publisher)
    first = store.upsert(TEST, OCCURRENCES)
    second = store.upsert(TEST, OCCURRENCES)
    assert first == second
    assert publisher.creates == publisher.updates == 1
    records = json.loads(register.read_text())['records']
    assert len(records) == 1 and records[0]['occurrences'] == [o['id'] for o in OCCURRENCES]


def test_missing_existing_decision_fails_before_register_update(tmp_path):
    root = tmp_path / 'work'
    root.mkdir()
    register = tmp_path / 'register.json'
    original = {'records': [{'test': TEST, 'item': 'missing'}], 'pending': []}
    register.write_text(json.dumps(original))
    with pytest.raises(ValueError):
        DecisionStore(root, register, Publisher(root)).upsert(TEST, OCCURRENCES)
    assert json.loads(register.read_text()) == original


def test_memory_attachment_retries_conflict_and_replay_is_inert(tmp_path):
    path = tmp_path / 'work/backlog/pentacle__case/spec.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nid: spec_x\n---\n## Tracking\n')
    calls = []
    def submit(request):
        calls.append(request)
        if request['operation'] == 'read':
            return {'ok': True, 'content': path.read_text(), 'revision': str(len(calls))}
        if sum(c['operation'] == 'update' for c in calls) == 1:
            return {'ok': False, 'code': 'REVISION_CONFLICT'}
        body = request['documents']['work/backlog/pentacle__case/spec.md']['body']
        path.write_text('---\nid: spec_x\n---\n' + body)
        return {'ok': True}
    publisher = MemoryPublisher(tmp_path, 'owner', 'epic_goal', 'host', submit=submit)
    publisher.attach('pentacle__case', TEST, OCCURRENCES)
    publisher.attach('pentacle__case', TEST, OCCURRENCES)
    assert sum(c['operation'] == 'update' for c in calls) == 2
    assert path.read_text().count('Quarantine observation:') == 2


def test_create_success_then_attachment_failure_recovers_without_duplicate(tmp_path):
    root = tmp_path / 'work'
    register = tmp_path / 'register.json'
    register.write_text(json.dumps({'records': [], 'pending': []}))
    publisher = Publisher(root)
    attach = publisher.attach
    def fail_once(*args):
        publisher.attach = attach
        raise OSError('injected publication failure')
    publisher.attach = fail_once
    store = DecisionStore(root, register, publisher)
    with pytest.raises(OSError):
        store.upsert(TEST, OCCURRENCES)
    assert json.loads(register.read_text())['records'] == []
    store.upsert(TEST, OCCURRENCES)
    assert publisher.creates == 1
    assert len(json.loads(register.read_text())['records']) == 1


def test_terminal_decision_reopens_only_with_verified_restoration(tmp_path):
    root = tmp_path / 'work'
    register = tmp_path / 'register.json'
    register.write_text(json.dumps({'records': [], 'pending': []}))
    publisher = Publisher(root)
    store = DecisionStore(root, register, publisher)
    result = store.upsert(TEST, OCCURRENCES)
    item = result['item']
    old = root / 'backlog' / item
    terminal = root / 'completed' / item
    terminal.parent.mkdir()
    old.rename(terminal)
    summary = terminal / 'summary.md'
    summary.write_text(summary.read_text().replace('status: backlog', 'status: completed'))
    with pytest.raises(ValueError, match='verified recurrence'):
        store.upsert(TEST, OCCURRENCES)

    def reopen(name, test, proof):
        assert name == item and test == TEST and proof == {'sha256': 'a' * 64}
        terminal.rename(old)
        summary = old / 'summary.md'
        summary.write_text(summary.read_text().replace('status: completed', 'status: backlog'))
    publisher.reopen = reopen
    newer = [{'id': 'public-checks/12/1', 'source': 'https://example.test/12',
              'created': '2026-11-03T00:00:00Z'},
             {'id': 'public-checks/13/1', 'source': 'https://example.test/13',
              'created': '2026-11-04T00:00:00Z'}]
    assert store.upsert(TEST, newer, restoration={'sha256': 'a' * 64})['item'] == item
    assert json.loads(register.read_text())['records'][0]['occurrences'] == [
        *(row['id'] for row in OCCURRENCES), *(row['id'] for row in newer)]
