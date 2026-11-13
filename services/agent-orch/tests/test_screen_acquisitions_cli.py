"""The acquisitions job is resolved as a fixed file principal before credentials."""
from agent_orch import send_boundary as boundary

def test_v4_fixed_identity_admission():
    identity = boundary.resolve_outbound_identity(environ={'AGENT_ORCH_STREAM_ID': 'altum-acquisitions-browser'})
    assert identity.kind == 'service'
    assert identity.stream_id == 'altum-acquisitions-browser'
    assert boundary.FIXED_SERVICE_IDENTITIES[identity.stream_id] == ('file', 'AGENT_ORCH_STREAM_TOKEN_FILE')

import json
import os
from pathlib import Path
import pytest
from agent_orch import cli, wsclient
from send_boundary_capture import Socket, Connection

@pytest.mark.parametrize('argv', [
    ['acquire', '--host', 'test', '--reason', 'fixture'],
    ['renew', 'lease-fixture'], ['release', 'lease-fixture']])
@pytest.mark.parametrize('state', ['valid', 'missing', 'unreadable'])
def test_v4_real_cli_file_identity_without_credential_fallback(monkeypatch, tmp_path, capsys, argv, state):
    token = tmp_path / 'service-token'
    if state != 'missing':
        token.write_text('FILE-service-fixture'); token.chmod(0o600)
    operator = tmp_path / '.config/pentacle-stream/token'
    operator.parent.mkdir(parents=True); operator.write_text('FILE-operator-never-use')
    monkeypatch.setattr(os, 'environ', {
        'HOME': str(tmp_path), 'AGENT_ORCH_HOST_ID': 'fixture',
        'AGENT_ORCH_STREAM_ID': 'altum-acquisitions-browser',
        'AGENT_ORCH_STREAM_TOKEN_FILE': str(token),
        'AGENT_ORCH_TOKEN': 'ENV-operator-never-use',
        'AGENT_ORCH_STREAM_TOKEN': 'ENV-seat-never-use'})
    if state == 'unreadable':
        original = Path.read_text
        def read(path, *a, **kw):
            if path == token: raise PermissionError('synthetic unreadable file')
            return original(path, *a, **kw)
        monkeypatch.setattr(Path, 'read_text', read)
    def forbidden(*a, **kw):
        pytest.fail('operator or seat fallback attempted')
    monkeypatch.setattr(boundary, 'resolve_operator_token', forbidden)
    monkeypatch.setattr(wsclient, 'resolve_operator_token', forbidden)
    monkeypatch.setattr(boundary, '_file_token', forbidden)
    monkeypatch.setattr(boundary, 'stream_token_from_env', forbidden)
    sockets = []
    def connect(*a, **kw):
        socket = Socket(); sockets.append(socket); return Connection(socket)
    monkeypatch.setattr(wsclient.websockets, 'connect', connect)
    assert cli.main(['screen', *argv, '--json']) == (0 if state == 'valid' else 2)
    capsys.readouterr()
    assert sockets and all(s.closed for s in sockets)
    frames = [json.loads(raw) for s in sockets for raw in s.raw]
    if state != 'valid':
        assert frames == []
    else:
        for frame in frames:
            if frame['type'] == 'pong': continue
            assert frame['from_stream_id'] == 'altum-acquisitions-browser'
            assert frame['stream_token'] == 'FILE-service-fixture'
            assert 'token' not in frame and 'auth_v2' not in frame
        assert any(f['type'] == 'screen.lease.' + argv[0] for f in frames)
