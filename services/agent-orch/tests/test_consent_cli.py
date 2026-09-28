"""Consent command payloads and local recovery admission."""
from agent_orch import cli


def test_request_uses_fresh_target_and_revision(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, 'load_config', lambda: object())
    monkeypatch.setattr(cli, 'discover_leader_stream_id_short', lambda _: 'fixture:lead')
    async def inspect(*args, **kwargs):
        return {'type': 'assistant.lifecycle.ok', 'grant': {'revision': 7}, 'target': {'session_generation': 'generation'}}
    async def call(config, verb, fields, **kwargs):
        captured.update(verb=verb, fields=fields, options=kwargs)
        return {'type': verb + '.ok', 'code': 'consent_pending'}
    monkeypatch.setattr(cli, 'assistant_lifecycle_once', inspect)
    monkeypatch.setattr(cli, 'consent_once', call)
    args = cli.build_parser().parse_args(['consent', 'request', 'designate', '--target', 'fixture:bart', '--reason', 'Exact manager'])
    assert args.func(args) == 0
    assert captured['verb'] == 'consent.request'
    assert captured['fields']['expected_revision'] == 7
    assert captured['fields']['target_generation'] == 'generation'
    assert captured['options']['local_admin'] is False


def test_key_ceremony_uses_local_admin_without_seat(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, 'load_config', lambda: object())
    monkeypatch.setattr(cli, '_local_admin_token', lambda _: 'test-token')
    async def call(config, verb, fields, **kwargs):
        captured.update(verb=verb, fields=fields, options=kwargs)
        return {'type': verb + '.ok'}
    monkeypatch.setattr(cli, 'consent_once', call)
    args = cli.build_parser().parse_args(['consent-key', 'confirm', 'a'*64])
    assert args.func(args) == 0
    assert captured['fields'] == {'local_admin_token': 'test-token', 'fingerprint': 'a'*64}
    assert captured['options']['from_stream_id'] is None
    assert captured['options']['local_admin'] is True


def test_emergency_revoke_reads_revision_locally_and_labels_claims(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, 'load_config', lambda: object())
    monkeypatch.setattr(cli, 'discover_leader_stream_id_short', lambda _: None)
    monkeypatch.setattr(cli, '_local_admin_token', lambda _: 'test-token')
    async def call(config, verb, fields, **kwargs):
        calls.append((verb, fields, kwargs))
        return {'type': 'assistant.lifecycle.ok', 'grant': {'revision': 4}}
    monkeypatch.setattr(cli, 'consent_once', call)
    args = cli.build_parser().parse_args(['lifecycle', 'revoke', '--emergency', '--reason', 'Lost phone'])
    assert args.func(args) == 0
    assert calls[0][1]['action'] == 'inspect'
    assert calls[1][1]['expected_revision'] == 4
    assert calls[1][1]['emergency'] is True
    assert 'caller_claims' in calls[1][1]
    assert all(c[2]['local_admin'] for c in calls)
