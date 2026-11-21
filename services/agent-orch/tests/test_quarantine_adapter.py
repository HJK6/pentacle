import json
from types import SimpleNamespace

from agent_orch import quarantine_adapter


def test_service_notice_strips_seat_identity_and_reuses_test_decision_key(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv('AGENT_ORCH_STREAM_ID', 'private-seat')
    monkeypatch.setenv('TMUX', 'private-tmux')
    def run(args, **kwargs):
        calls.append((args, kwargs['env']))
        return SimpleNamespace(returncode=0, stdout=json.dumps({'type': 'notification.create.ok', 'notification': {}}))
    monkeypatch.setattr(quarantine_adapter.subprocess, 'run', run)
    relay = {'path': '/tmp/bundle', 'sha256': 'a' * 64}
    monkeypatch.setattr(quarantine_adapter, 'prepare_bundle', lambda *a, **k: {'state': 'ready_for_fd_relay', 'bundle': relay})
    adapter = quarantine_adapter.ProductionAdapter(None, checker=tmp_path/'checker', packet_root=tmp_path)
    entry = {'decision': {'item': 'decision', 'owner': 'owner', 'coverage': 'coverage'},
             'occurrences': [{'source': 'run/1'}, {'source': 'run/2'}]}
    entry['key'] = 'activation:abc'
    entry['relay'] = adapter.prepare_relay(['test/file.js::case'], 'relay:abc', {'test/file.js::case': entry})
    adapter.send_relay('test/file.js::case', entry)
    adapter.notify('activation:abc', {'test': 'test/file.js::case', **entry['decision'],
                   'occurrences': ['run/1', 'run/2'], 'relay': entry['relay']})
    first, second = calls
    assert 'AGENT_ORCH_STREAM_ID' not in first[1] and 'TMUX' not in first[1]
    assert first[0][first[0].index('--dedup-key') + 1] == second[0][second[0].index('--dedup-key') + 1]
    assert 'run/1' in first[0][first[0].index('--message') + 1]
    assert relay['sha256'] in first[0][first[0].index('--message') + 1]
