"""Observed delivery failures remain distinct from publication and infrastructure."""
import asyncio
from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace

import pytest

from tools import release_client_delivery as probe

SHA = 'a' * 40


@pytest.mark.parametrize('fault,phase,reason', [
    (None, 'complete', ''),
    ('live_drop', 'receive', 'missing_live_event'),
    ('live_duplicate', 'receive', 'duplicate_live_event'),
    ('replay_drop', 'recover', 'missing_recovery_event'),
    ('replay_duplicate', 'recover', 'duplicate_recovery_event'),
    ('replay_wrong_id', 'recover', 'wrong_recovery_event_or_anchor'),
    ('replay_wrong_body', 'recover', 'wrong_recovery_event_or_anchor'),
])
def test_cli_uses_real_wire_for_publish_live_and_recovery(monkeypatch, capsys, fault, phase, reason):
    # Patch only the owned fixture's fanout/replay for deterministic wire faults.
    from server import Server

    original_broadcast = Server.broadcast

    async def broadcast(self, frame, *args, **kwargs):
        note = frame.get('type') == 'chat.event' and frame.get('event', {}).get('note_type') == 'relay'
        if not note or fault != 'live_drop':
            await original_broadcast(self, frame, *args, **kwargs)
        if note and fault == 'live_duplicate':
            await original_broadcast(self, frame, *args, **kwargs)

    original_history = Server._on_request_stream_events

    async def request_history(self, message):
        if message.get('request_id', '').startswith('recovery-') and fault and fault.startswith('replay_'):
            original_stream = self._stream_request_stream_events

            async def mutated(**kwargs):
                # In a one-note fixture there is one small terminal history frame.
                async for frame in original_stream(**kwargs):
                    body = json.loads(frame.payload)
                    rows = []
                    for row in body['events']:
                        if row.get('note_type') != 'relay':
                            rows.append(row)
                        elif fault == 'replay_duplicate':
                            rows.extend([row, row])
                        elif fault == 'replay_wrong_id':
                            rows.append({**row, 'message_id': 'wrong'})
                        elif fault == 'replay_wrong_body':
                            rows.append({**row, 'text': 'wrong'})
                    body['events'] = rows
                    yield replace(frame, payload=json.dumps(body))

            self._stream_request_stream_events = mutated
            try:
                return await original_history(self, message)
            finally:
                self._stream_request_stream_events = original_stream
        return await original_history(self, message)

    monkeypatch.setattr(Server, 'broadcast', broadcast)
    monkeypatch.setattr(Server, '_on_request_stream_events', request_history)
    monkeypatch.setattr(probe, 'LIVE_WINDOW_SECONDS', 0.25)
    monkeypatch.setattr(probe, 'installed_check', lambda sha, pid: asyncio.run(probe.run_probe(sha)))

    assert probe.main(['--target-sha', SHA, '--active-pid', '1']) == int(fault is not None)
    result = json.loads(capsys.readouterr().out)
    assert (result['phase'], result['reason']) == (phase, reason)
    assert result['assertions']['publish'] == result['assertions']['reconnect'] == 'passed'
    assert result['teardown'] == 'passed'
    assert result['publication_seq'] > result['saved_cursor']
    assert result['message_id'] == 'publication:' + result['correlation_id']
    if fault is None:
        assert result['live_count'] == result['recovered_count'] == result['merged_count'] == 1
        assert all(value == 'passed' for value in result['assertions'].values())
    else:
        assert result['classification'] == 'delivery_failure'
        assert result['assertions'][phase] == 'failed'
        assert result['assertions']['recover' if phase == 'receive' else 'receive'] == 'passed'


def test_precondition_failure_does_not_enter_journey(monkeypatch):
    monkeypatch.setattr(probe, 'run_probe', lambda *a: pytest.fail('must not open fixture'))
    result = probe.installed_check(SHA, 0)
    assert result['classification'] == 'infrastructure'
    assert result['phase'] == 'precondition'
    assert result['reason'] == 'active_pid_missing'
    assert result['teardown'] == 'not_run'


def test_delivery_red_is_independent_in_release_and_nightly(tmp_path, monkeypatch):
    from deploy import deploy as d
    from tools import nightly_journey as nightly

    failure = {**probe.new_receipt(SHA), 'classification': 'delivery_failure',
               'phase': 'receive', 'teardown': 'passed', 'active_instance': {'sha': SHA, 'pid': 999}}
    failure['assertions']['publish'] = 'passed'
    failure['assertions']['receive'] = 'failed'
    service = d.SERVICES['chat-streamd-v2']
    machines = tmp_path / 'machines.json'
    machines.write_text(json.dumps({'machines': [{'name': 'fixture', 'ssh_target': None}],
                                    'client_delivery_incident': 'fixture__existing_delivery'}))
    notices = []
    monkeypatch.setattr(d, '_release_machines_file', lambda *a: str(machines))
    monkeypatch.setattr(d, '_launchd_state', lambda *a: d.LaunchdState(loaded=True, pid=999))
    monkeypatch.setattr(d, '_daemon_log_paths', lambda *a: (tmp_path / 'out', tmp_path / 'err'))
    monkeypatch.setattr(d, '_managed_log_config', lambda *a: None)
    monkeypatch.setattr(d, '_process_start', lambda *a: 'fixture start')
    monkeypatch.setattr(d, '_scan_slow_consumer_window', lambda *a, **k: {'outcome': 'passed'})
    monkeypatch.setattr(d, '_notify_failed_checks', lambda *args: notices.append(args[3]))

    def runner(argv, cwd):
        if any(arg.endswith('release_client_delivery.py') for arg in argv):
            assert argv[-4:] == ('--target-sha', SHA, '--active-pid', '999')
            return subprocess.CompletedProcess(argv, 1, json.dumps(failure), '')
        if any(arg.endswith('spawn_fleet_smoke.py') for arg in argv):
            cells = [dict(host='fixture', provider=provider, prompt_mode=mode, outcome='passed')
                     for provider in ('claude', 'codex') for mode in ('prompted', 'promptless')]
            return subprocess.CompletedProcess(argv, 0, json.dumps({'cells': cells}), '')
        return subprocess.CompletedProcess(argv, 0, '{"outcome":"passed"}', '')

    original = {'sha': SHA}
    release_stamp = dict(original)
    checks = d.run_post_release_checks(service, tmp_path, SHA, release_stamp, runner=runner)
    assert release_stamp['disposition'] == 'active with failed checks'
    assert checks['client_delivery'] == {
        **failure,
        'active_instance': {**failure['active_instance'], 'process_start': 'fixture start'},
        'cause_target': 'fixture__existing_delivery',
        'receipt': str(d._stamp_path(tmp_path, service)),
    }
    assert all(result['outcome'] == 'passed' for name, result in checks.items() if name != 'client_delivery')

    monkeypatch.setattr(d, '_run', runner)
    monkeypatch.setattr(d, '_read_stamp', lambda *a: original)
    args = SimpleNamespace(checkout=tmp_path, service=service.name, evidence=tmp_path / 'evidence')
    assert nightly.check_release(args, deploy_module=d) == 1
    record = json.loads((args.evidence / 'receipt.json').read_text())
    nightly_receipt = str(args.evidence / 'receipt.json')
    assert record['checks'] == {**checks, 'client_delivery': {**checks['client_delivery'], 'receipt': nightly_receipt}}
    assert record['result'] == 'FAIL'
    # Nightly's report is the sole notification route for the recurrence.
    assert notices == [checks, {name: check for name, check in checks.items() if name != 'client_delivery'}]
    assert original == {'sha': SHA}


def test_nightly_delivery_incident_preserves_notification_and_skips_generic_writer(tmp_path, monkeypatch):
    from tools import nightly_journey as nightly

    path = tmp_path / 'receipt.json'
    failure = {'outcome': 'failed', 'classification': 'delivery_failure', 'phase': 'receive',
               'cause_target': 'fixture__existing_delivery'}
    nightly.atomic(path, {'checks': {'client_delivery': failure, 'fleet_smoke': {'outcome': 'passed'}}})
    published = []

    def command(command, *args, **kwargs):
        if command == 'publisher':
            pytest.fail('delivery failure belongs to the existing incident')
        published.append(command)
        assert args[args.index('--dedup-key') + 1] == 'nightly-soak:2026-11-21'
        assert str(path) in args[args.index('--message') + 1]
        assert failure['cause_target'] in args[args.index('--message') + 1]
        return {'type': 'notification.create.ok', 'notification': {'notification_id': 'notice'}}

    monkeypatch.setattr(nightly, 'command_json', command)
    monkeypatch.setattr(nightly, 'deliver_report', lambda *a: None)
    monkeypatch.setattr(nightly, 'notice_proof', lambda *a: {'notice_id': 'notice'})
    args = SimpleNamespace(evidence=tmp_path, date='2026-11-21', job='nightly-soak',
                           result='FAIL', primary_error='post-release checks failed',
                           publisher_cmd='publisher', report_send_cmd='', notify_cmd='notify',
                           notice_wait_s=1, notifications_db='notifications', notices_db='notices')
    assert nightly.report(args) == 0
    assert published == ['notify']
    record = json.loads(path.read_text())
    assert record['checks']['client_delivery'] == failure
    assert record['item'] == failure['cause_target']
