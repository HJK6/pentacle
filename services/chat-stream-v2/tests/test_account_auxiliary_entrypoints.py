"""Additional installed-capable public tools carry caller account intent."""
from argparse import Namespace
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path

import pytest
from tools import busy_seat_send_probe


def test_bootstrap_cli_accepts_explicit_account():
    path = Path(__file__).resolve().parents[3]/'tools/bootstrap_assistant.py'
    spec = importlib.util.spec_from_file_location('account_bootstrap', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.parser().parse_args(['--url', 'ws://127.0.0.1:12345',
        '--credential-file', '/private/fixture/credential', '--physical-host', 'fixture',
        '--name', 'Fixture', '--provider', 'codex', '--model', 'gpt-6.1-sol', '--effort', 'high',
        '--private-workspace', '/private/fixture/workspace', '--instructions-file', '/private/fixture/brief',
        '--account', 'spare'])
    assert args.account == 'spare'


def test_busy_probe_builder_carries_explicit_account(monkeypatch):
    seen = []
    def capture(command, **kwargs):
        seen.append(command)
        raise OSError('isolated admission boundary')
    monkeypatch.setattr(busy_seat_send_probe, 'run', capture)
    args = Namespace(provider='codex', host='fixture', account='spare', spec_id=[])
    with pytest.raises(OSError, match='isolated admission'):
        busy_seat_send_probe._probe(args, {}, 'fixture', 'gpt-6.1-sol', 'high', 'probe')
    assert seen[0][-2:] == ['--account', 'spare']


@pytest.mark.parametrize('actual_alias', ['spare', 'primary'])
def test_bootstrap_builder_and_actual_binding_receipt(tmp_path, monkeypatch, actual_alias):
    import tools.live_window
    path = Path(__file__).resolve().parents[3]/'tools/bootstrap_assistant.py'
    spec = importlib.util.spec_from_file_location('account_bootstrap_journey', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    credential = tmp_path/'credential'; credential.write_text('fixture'); credential.chmod(0o600)
    brief = tmp_path/'brief'; brief.write_text('Fixture assistant instructions.')
    arguments = module.parser().parse_args(['--url', 'ws://127.0.0.1:1',
        '--credential-file', str(credential), '--physical-host', 'fixture', '--name', 'Fixture',
        '--provider', 'codex', '--model', 'gpt-6.1-sol', '--effort', 'high',
        '--private-workspace', str(tmp_path/'workspace'), '--instructions-file', str(brief), '--account', 'spare'])
    frames = []
    class Operator:
        snapshot = {'sessions': []}
        def rpc(self, message):
            frames.append(message)
            if message['type'] == 'assistant.binding':
                return {'type': 'assistant.binding.ok'}
            if message['type'] == 'spawn':
                self.stream = 'fixture:'+message['session_name']
                assert message['account'] == 'spare'
                return {'type': 'spawn.ok', 'stream_id': self.stream}
            if message['type'] == 'inspect_stream':
                return {'session': {'stream_id': self.stream, 'session_generation': 'fixture-generation',
                    'role': 'assistant', 'status': 'open', 'bootstrap_state': 'ready', 'provider': 'codex',
                    'effective_model': 'gpt-6.1-sol', 'effective_effort': 'high',
                    'account_binding': {'host': 'fixture', 'provider': 'codex', 'alias': actual_alias,
                        'generation': 'login-one'}}}
            return {'target': {'eligible': True, 'session_generation': 'fixture-generation'}}
    @contextmanager
    def connection(*_):
        yield Operator()
    monkeypatch.setattr(tools.live_window, 'authenticated_operator_connection', connection)
    if actual_alias == 'spare':
        receipt = module.bootstrap(arguments)
        assert receipt['account'] == receipt['account_binding']['alias'] == 'spare'
    else:
        with pytest.raises(RuntimeError, match='account binding mismatch'):
            module.bootstrap(arguments)
        assert not (arguments.private_workspace/'assistant.env').exists()
    intent = json.loads((arguments.private_workspace/'assistant-bootstrap-intent.json').read_text())
    assert intent['account'] == 'spare' and len([f for f in frames if f['type'] == 'spawn']) == 1


def test_busy_probe_refuses_mismatched_binding_before_provider_send(monkeypatch):
    calls = []
    def capture(command, **kwargs):
        calls.append(command)
        return 0, json.dumps({'stream_id': 'fixture:owned', 'session': {'account_binding': {
            'host': 'fixture', 'provider': 'codex', 'alias': 'primary', 'generation': 'login-one'}}})
    monkeypatch.setattr(busy_seat_send_probe, 'run', capture)
    args = Namespace(provider='codex', host='fixture', account='spare', spec_id=[])
    report = {}
    assert busy_seat_send_probe._probe(args, report, 'fixture', 'gpt-6.1-sol', 'high', 'probe') == 3
    assert report['stage'] == 'account_binding_mismatch' and report['stream_id'] == 'fixture:owned'
    assert len(calls) == 1
