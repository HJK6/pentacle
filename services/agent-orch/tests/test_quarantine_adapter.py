import json
from types import SimpleNamespace

from agent_orch import quarantine_adapter


def test_service_notice_strips_seat_identity_and_reuses_test_decision_key(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(quarantine_adapter.os, 'environ', {
        'HOME': str(tmp_path), 'PATH': '/usr/bin', 'AGENT_ORCH_WS_URL': 'ws://127.0.0.1:7791',
        'AGENT_ORCH_STREAM_ID': 'synthetic-seat', 'PENTACLE_STREAM_ID': 'synthetic-seat',
        'AGENT_ORCH_INTERNAL_LEADER_STREAM_ID': 'synthetic-seat',
        'AGENT_ORCH_STREAM_TOKEN': 'synthetic-token',
        'AGENT_ORCH_STREAM_TOKEN_FILE': '/synthetic/seat-token',
        'PENTACLE_WORK_REPORT_READER_TOKEN': 'synthetic-reader-token',
        'AGENT_ORCH_TOKEN': 'synthetic-operator-token',
        'TMUX': 'synthetic-tmux', 'TMUX_PANE': '%1', 'UNRELATED': 'synthetic-unrelated-value',
    })

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
    assert first[1] == second[1] == {
        'HOME': str(tmp_path), 'PATH': '/usr/bin', 'AGENT_ORCH_WS_URL': 'ws://127.0.0.1:7791',
    }
    assert first[0][first[0].index('--dedup-key') + 1] == second[0][second[0].index('--dedup-key') + 1]
    assert 'run/1' in first[0][first[0].index('--message') + 1]
    assert relay['sha256'] in first[0][first[0].index('--message') + 1]
