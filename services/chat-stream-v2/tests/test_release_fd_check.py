"""Release check observes a canonical linked FD answer, not send admission."""
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools import release_fd_check as check


def event(text='PONG probe-nonce', **changes):
    row = {'stream_id': 'bart:assistant', 'kind': 'ASSIST_TEXT', 'text': text,
           'message_id': 'publication:answer', 'reply_to_message_id': 'input-1',
           'raw': {'assistant_composite': True, 'reply_to_message_id': 'input-1'}}
    return {**row, **changes}


def install(monkeypatch, rows, admission=None, error=None):
    calls = []
    @contextmanager
    def connection(url, token_path, timeout):
        def rpc(payload, prefix, **kwargs):
            calls.append(payload)
            if prefix == 'send':
                if error:
                    raise RuntimeError(error)
                return admission or {'type': 'send.result', 'assistant_composite': {'message_id': 'input-1'}}
            return {'type': 'request_stream_events.ok', 'events': rows}
        yield rpc, None, None, None, None
    monkeypatch.setattr(check, '_operator_connection', connection)
    monkeypatch.setattr(check.uuid, 'uuid4', lambda: type('Nonce', (), {'hex': 'probe-nonce'})())
    ticks = iter([0, 0, 0, 181])
    monkeypatch.setattr(check.time, 'monotonic', lambda: next(ticks, 181))
    monkeypatch.setattr(check.time, 'sleep', lambda _: None)
    return calls


def test_linked_published_reply_passes(monkeypatch):
    calls = install(monkeypatch, [event()])
    result = check.run_check('ws://owned.invalid', Path('/owned-token'), 'a' * 40)
    assert result['outcome'] == 'passed'
    assert result['message_id'] == 'input-1'
    assert result['reply_message_id'] == 'publication:answer'
    assert calls[0] == {'type': 'send', 'to_stream_id': 'bart:assistant',
                        'message': '[release-check aaaaaaaa probe-nonce] Release check. Reply with exactly: PONG probe-nonce'}
    assert calls[1]['stream_id'] == 'bart:assistant'


@pytest.mark.parametrize('rows', [[], [event(text='PONG old-nonce')],
    [event(reply_to_message_id='old-input', raw={'assistant_composite': True, 'reply_to_message_id': 'old-input'})],
    [event(kind='USER')], [event(stream_id='other:assistant')],
    [event(raw={})], [event(message_id=None)]])
def test_admission_stale_unlinked_or_nonpublication_never_passes(monkeypatch, rows):
    install(monkeypatch, rows)
    result = check.run_check('ws://owned.invalid', Path('/owned-token'), 'a' * 40)
    assert result['outcome'] == 'failed'
    assert result['reason'] == 'reply_not_observed'


def test_refused_execution_is_failed_check(monkeypatch):
    install(monkeypatch, [], error='fd_execution_forbidden')
    result = check.run_check('ws://owned.invalid', Path('/owned-token'), 'a' * 40)
    assert result['outcome'] == 'failed'
    assert result['reason'] == 'fd_execution_forbidden'


def test_send_without_input_identity_is_not_admission_proof(monkeypatch):
    install(monkeypatch, [event()], admission={'type': 'send.result', 'delivery': 'accepted'})
    assert check.run_check('ws://owned.invalid', Path('/owned-token'), 'a' * 40)['reason'] == 'input_identity_missing'


def test_existing_operator_rpc_collects_chunked_normal_publication(monkeypatch):
    import json
    from types import SimpleNamespace
    from tools import spawn_fleet_smoke as smoke
    queue = []
    closed = []
    class Socket:
        def send(self, encoded):
            payload = json.loads(encoded)
            request_id = payload['request_id']
            if payload['type'] == 'send':
                nonce = payload['message'].split('PONG ')[1]
                self.answer = event(text='PONG ' + nonce)
                queue.extend([
                    {'type': 'send.result', 'request_id': 'unrelated', 'assistant_composite': {'message_id': 'wrong'}},
                    {'type': 'send.result', 'request_id': request_id, 'assistant_composite': {'message_id': 'input-1'}},
                ])
            else:
                queue.extend([
                    {'type': 'request_stream_events.chunk', 'request_id': request_id, 'events': [self.answer]},
                    {'type': 'request_stream_events.ok', 'request_id': request_id, 'events': []},
                ])
        def recv(self, timeout):
            return json.dumps(queue.pop(0))
    @contextmanager
    def authenticated(url, path, timeout):
        assert url == 'ws://owned.invalid'
        try:
            yield SimpleNamespace(socket=Socket(), snapshot=None)
        finally:
            closed.append(True)
    monkeypatch.setattr(smoke, 'authenticated_operator_connection', authenticated)
    assert check.run_check('ws://owned.invalid', Path('/owned-token'), 'a' * 40)['outcome'] == 'passed'
    assert closed == [True]


def test_cli_uses_existing_smoke_defaults(monkeypatch, capsys):
    from tools import spawn_fleet_smoke as smoke
    calls = []
    def run(url, token_path, sha):
        calls.append((url, token_path, sha))
        return {'outcome': 'passed'}
    monkeypatch.setattr(check, 'run_check', run)
    assert check.main(['--target-sha', 'a' * 40]) == 0
    assert calls == [(smoke.DEFAULT_URL, smoke.DEFAULT_TOKEN_PATH, 'a' * 40)]
    assert '"outcome": "passed"' in capsys.readouterr().out
