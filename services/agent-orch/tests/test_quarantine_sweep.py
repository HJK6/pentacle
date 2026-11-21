from datetime import datetime, timezone
import json

from agent_orch import quarantine_sweep


TEST = 'daemon::tests/test_example.py::test_case'
NODE = 'tests/test_example.py::test_case'
RAW = 'tests.test_example::test_case'


class Adapter:
    def __init__(self, register):
        self.register = register
        self.relayed, self.notices, self.alerts = {}, {}, {}

    def decision(self, test, occurrences):
        register = json.loads(self.register.read_text())
        if not register['records']:
            register['records'].append({'test': test, 'item': 'existing',
                                        'occurrences': [r['id'] for r in occurrences]})
            self.register.write_text(json.dumps(register))
        return {'item': 'existing', 'owner': 'owner', 'coverage': 'test execution'}

    def prepare_relay(self, selected, key, pending):
        self.relayed.setdefault(key, selected)
        return {'path': 'bundle', 'sha256': 'a' * 64}

    def send_relay(self, test, entry):
        pass

    def notify(self, key, payload):
        self.notices.setdefault(key, payload)

    def alert(self, key, message):
        self.alerts.setdefault(key, message)

    def verify_restoration(self, test, restoration):
        return False


def row(n):
    return {'id': str(n), 'sha': 'a' * 40, 'created': f'2026-11-0{n}T00:00:00Z',
            'first': 'failure', 'source': f'run/{n}',
            'failures': [{'job': 'unit', 'step': 'tests', 'runner': ['ubuntu']}],
            'causes': [{'test': RAW, 'kind': 'non_code', 'job': 'unit', 'step': 'tests',
                        'reviewer': 'reviewer', 'reason': 'fixture', 'revision': 'a' * 40,
                        'evidence': f'run/{n}', 'cause': 'fixture', 'review_source': 'review'}]}


def test_replay_retains_one_first_attempt_until_second_and_notifies_on_activation(tmp_path, monkeypatch):
    register = tmp_path / 'register.json'
    register.write_text(json.dumps({'records': []}))
    sources = {'hosted': {'repository': 'org/repo'},
               'work': {'register': str(register), 'selector_checkout': str(tmp_path)}}
    current_rows, selected = [row(1)], set()
    monkeypatch.setattr(quarantine_sweep, 'hosted_score', lambda *_: ({}, list(current_rows)))
    monkeypatch.setattr(quarantine_sweep, 'effective_tests', lambda _: set(selected))
    def bind(rows, *args, **kwargs):
        return (rows, {(r['id'], 'unit', 'tests', RAW): {'test': NODE, 'phase': 'setup'} for r in rows}, [])
    monkeypatch.setattr(quarantine_sweep, 'enrich_other_python', bind)
    adapter = Adapter(register)
    kwargs = dict(ledger_path=tmp_path/'ledger.json', state_path=tmp_path/'state.json',
                  adapter=adapter, now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    quarantine_sweep.sweep(sources, **kwargs)
    assert not adapter.relayed
    current_rows[:] = [row(2)]
    quarantine_sweep.sweep(sources, **kwargs)
    quarantine_sweep.sweep(sources, **kwargs)
    assert len(adapter.relayed) == 1 and not adapter.notices
    selected.add(TEST)
    quarantine_sweep.sweep(sources, **kwargs)
    quarantine_sweep.sweep(sources, **kwargs)
    assert len(adapter.notices) == 1
