import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

from agent_orch.reliability_work import work_state


def test_fresh_child_reads_list_without_synthetic_decision(tmp_path):
    test = 'daemon::tests/test_example.py::test_case'
    memory = tmp_path / 'memory'
    (memory / 'work').mkdir(parents=True)
    checkout = tmp_path / 'checkout'
    (checkout / 'scripts').mkdir(parents=True)
    inventory = checkout / 'scripts/quarantine-v2.json'
    row = {'id': test, 'owner': 'team', 'reason': 'flake', 'expires': '2027-01-01'}
    inventory.write_text(json.dumps({'version': 2, 'nonblocking_tests': [row]}))
    register = tmp_path / 'register.json'
    register.write_text(json.dumps({'records': [], 'pending': [], 'complete_through': '2099-01-01T00:00:00Z'}))
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text('[]')
    sources = tmp_path / 'sources.json'
    work = {'work_root': str(memory / 'work'), 'register': str(register), 'selector_checkout': str(checkout)}
    sources.write_text(json.dumps({'hosted': {'repository': 'HJK6/pentacle', 'snapshot': str(snapshot),
                                              'complete_through': '2099-01-01T00:00:00Z'}, 'work': work}))
    config = tmp_path / 'config.json'
    cfg = {'sources': str(sources), 'memory_root': str(memory), 'owner': 'team', 'epic': 'epic',
           'machine': 'fixture', 'checker': str(tmp_path / 'checker'), 'packet_root': str(tmp_path / 'packet'),
           'ledger': str(tmp_path / 'ledger.json'), 'state': str(tmp_path / 'state.json'),
           'agent_orch': str(tmp_path / 'forbidden-transport')}
    config.write_text(json.dumps(cfg))
    def run():
        result = subprocess.run([sys.executable, '-m', 'agent_orch.quarantine_run', '--config', str(config)],
                                capture_output=True, text=True, env=os.environ.copy(), timeout=10)
        assert result.returncode == 0, result.stderr
    run()
    current = work_state(work, datetime.now(timezone.utc))
    assert current['open_tests'] == [test] and not current['quarantine_gaps']
    assert json.loads(register.read_text())['records'] == []
    assert not (tmp_path / 'packet').exists()
    item = memory / 'work/backlog/repo__old/summary.md'
    item.parent.mkdir(parents=True)
    item.write_text("---\nid: work_repo__old\nstatus: backlog\nowner: team\nupdated_at: '2026-01-01'\n---\n")
    prior = {'test': test, 'item': 'repo__old', 'cause': 'flake', 'first_seen': '2026-01-01T00:00:00Z', 'occurrences': []}
    register.write_text(json.dumps({'records': [prior], 'pending': [], 'complete_through': '2099-01-01T00:00:00Z'}))
    inventory.write_text(json.dumps({'version': 2, 'nonblocking_tests': []}))
    run()
    current = work_state(work, datetime.now(timezone.utc))
    assert current['open_tests'] == [] and not current['quarantine_gaps']
    assert json.loads(register.read_text())['records'] == [prior]
    assert current['open_decisions'] == [test] and current['rows'][0]['item'] == 'repo__old'


def test_fresh_child_recurrence_uses_only_post_restoration_occurrences(tmp_path):
    test = 'daemon::tests/test_example.py::test_case'
    memory = tmp_path / 'memory'
    (memory / 'work').mkdir(parents=True)
    checkout = tmp_path / 'checkout'
    (checkout / 'scripts').mkdir(parents=True)
    inventory = checkout / 'scripts/quarantine-v2.json'
    inventory.write_text(json.dumps({'version': 2, 'nonblocking_tests': [
        {'id': test, 'owner': 'team', 'reason': 'flake', 'expires': '2027-01-01'}]}))
    register = tmp_path / 'register.json'
    register.write_text(json.dumps({'records': [], 'pending': []}))
    sources = tmp_path / 'sources.json'
    sources.write_text(json.dumps({'hosted': {'repository': 'HJK6/pentacle'},
        'work': {'work_root': str(memory / 'work'), 'register': str(register), 'selector_checkout': str(checkout)}}))
    rows = tmp_path / 'rows.json'
    rows.write_text('[]')
    restoration = tmp_path / 'restoration.json'
    restoration.write_text(json.dumps({test: {'at': '2026-11-02T12:00:00Z'}}))
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'sources': str(sources), 'memory_root': str(memory), 'owner': 'team',
        'epic': 'epic', 'machine': 'fixture', 'checker': str(tmp_path / 'checker'),
        'packet_root': str(tmp_path / 'packet'), 'ledger': str(tmp_path / 'ledger.json'),
        'state': str(tmp_path / 'state.json'), 'restorations_file': str(restoration)}))
    # Fresh interpreter, real entry and transition; all external transports and
    # the independently verified restoration are isolated fixture adapters.
    (tmp_path / 'sitecustomize.py').write_text('''
import json, os
from pathlib import Path
from agent_orch import quarantine_sweep as s, quarantine_adapter as a
root = Path(os.environ["REPLAY_ROOT"])
test = "daemon::tests/test_example.py::test_case"
def log(kind, value):
    with (root / "transport.jsonl").open("a") as out:
        out.write(json.dumps([kind, value]) + "\\n")
s.hosted_score = lambda *args: ({}, json.loads((root / "rows.json").read_text()))
def enrich(rows, hosted):
    return rows, {(r["id"], "unit", "tests", "raw"): {"test": test.removeprefix("daemon::"), "phase": "call"} for r in rows}, []
s.enrich_rows = enrich
s.reviewed_bindings = lambda rows, bindings, signatures: bindings
a.ProductionAdapter.verify_restoration = lambda self, test, proof: True
a.ProductionAdapter.decision = lambda self, test, occurrences: (log("decision", [test, [o["id"] for o in occurrences]]) or {"item": "old", "owner": "team", "coverage": "test"})
a.ProductionAdapter.prepare_relay = lambda self, selected, key, pending: (log("relay", selected) or {"path": "fake", "sha256": "a" * 64})
a.ProductionAdapter.send_relay = lambda self, test, entry: log("send", test)
a.ProductionAdapter.alert = lambda self, key, message: log("alert", message)
''')
    env = {**os.environ, 'REPLAY_ROOT': str(tmp_path),
           'PYTHONPATH': str(tmp_path) + os.pathsep + os.environ['PYTHONPATH']}
    def run():
        result = subprocess.run([sys.executable, '-m', 'agent_orch.quarantine_run', '--config', str(config)],
                                capture_output=True, text=True, env=env, timeout=10)
        assert result.returncode == 0, result.stderr
    def row(n):
        return {'id': str(n), 'sha': 'a' * 40, 'created': f'2026-11-0{n}T00:00:00Z',
                'first': 'failure', 'source': f'run/{n}',
                'failures': [{'job': 'unit', 'step': 'tests', 'runner': ['fixture']}],
                'causes': [{'test': 'raw', 'kind': 'non_code', 'job': 'unit', 'step': 'tests',
                            'reviewer': 'reviewer', 'reason': 'fixture', 'revision': 'a' * 40,
                            'evidence': f'run/{n}', 'cause': 'fixture'}]}
    rows.write_text(json.dumps([row(1), row(2)]))
    run()
    assert not (tmp_path / 'transport.jsonl').exists()
    inventory.write_text(json.dumps({'version': 2, 'nonblocking_tests': []}))
    run()
    assert not (tmp_path / 'transport.jsonl').exists()
    rows.write_text(json.dumps([row(3), row(4)]))
    run()
    events = [json.loads(line) for line in (tmp_path / 'transport.jsonl').read_text().splitlines()]
    assert [event[0] for event in events] == ['decision', 'relay', 'send']
    assert events[0][1] == [test, ['public-checks/3/1', 'public-checks/4/1']]
    assert events[1][1] == [test]
