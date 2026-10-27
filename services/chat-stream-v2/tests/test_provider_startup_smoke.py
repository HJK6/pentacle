"""Fail closed at the native/installed/daemon binding bridge, before input."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from provider_accounts import AccountError, Binding
from tools import provider_startup_probe as probe_module
from tools import spawn_fleet_smoke as smoke


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    executable = tmp_path / 'codex'
    executable.write_text('#!/bin/sh\nprintf "codex-cli fixture\\n"\n')
    executable.chmod(0o700)
    config = tmp_path / 'config.toml'
    config.write_text('cli_auth_credentials_store = "file"\n')
    binding = Binding('codex', 'named-account', 'fixture-host', str(tmp_path), 'login-a', '',
                      str(executable), 'codex-cli fixture', 'macos', 'identity-a', 'identity-a',
                      True, True, hashlib.sha256(executable.read_bytes()).hexdigest())
    value = {'binding': binding.public(), 'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
             'model': 'gpt-6.1-sol', 'effort': 'high'}
    path = tmp_path / 'tuple.json'
    path.write_text(json.dumps(value))
    monkeypatch.setattr(probe_module, 'configured_local_host', lambda: 'fixture-host')
    return path, value, binding


def test_native_observation_uses_pinned_binding_and_compares_owner(pinned, monkeypatch):
    path, value, binding = pinned
    calls = []
    def observe(actual):
        calls.append(actual)
        return {'provider_id': 'identity-a', 'pool_id': 'identity-a'}
    monkeypatch.setattr(probe_module, 'codex_observation', observe)
    probe = probe_module.ProviderStartupProbe(path, binding.host)
    assert probe.observe()['native_identity'] == 'matched'
    assert calls == [binding]
    monkeypatch.setattr(probe_module, 'codex_observation', lambda _: {'provider_id': 'foreign', 'pool_id': 'identity-a'})
    with pytest.raises(AccountError, match='provider_identity_changed'):
        probe.observe()


def test_frozen_native_tui_config_is_valid_probe_input(pinned, monkeypatch):
    path, value, binding = pinned
    config = Path(binding.home) / 'config.toml'
    config.write_text('cli_auth_credentials_store = "file"\n[tui]\nnotifications = false\n')
    value['config_sha256'] = hashlib.sha256(config.read_bytes()).hexdigest()
    path.write_text(json.dumps(value))
    monkeypatch.setattr(probe_module, 'codex_observation', lambda _: {
        'provider_id': binding.provider_id, 'pool_id': binding.pool_id})
    assert probe_module.ProviderStartupProbe(path, binding.host).observe()['native_identity'] == 'matched'


@pytest.mark.parametrize('drift,code', [
    ('binary', 'provider_executable_changed'), ('version', 'provider_version_changed'),
    ('config', 'provider_config_changed'),
])
def test_changed_installed_inputs_refuse_before_native_observation(pinned, monkeypatch, drift, code):
    path, value, binding = pinned
    probe = probe_module.ProviderStartupProbe(path, binding.host)
    if drift == 'binary':
        Path(binding.executable).write_text('#!/bin/sh\nexit 0\n')
    elif drift == 'version':
        probe.binding = replace(binding, version='codex-cli other')
    else:
        (Path(binding.home) / 'config.toml').write_text('model = "other"\n')
    monkeypatch.setattr(probe_module, 'codex_observation', lambda _: pytest.fail('drift must refuse before native RPC'))
    with pytest.raises(AccountError, match=code):
        probe.observe()


@pytest.mark.parametrize('change', ['provider', 'host', 'disabled', 'unknown-identity', 'unknown-key'])
def test_unknown_or_ambiguous_tuple_refuses_without_native_use(pinned, change):
    path, value, binding = pinned
    if change == 'unknown-key': value['extra'] = True
    elif change == 'provider': value['binding']['provider'] = 'claude'
    elif change == 'host': value['binding']['host'] = 'remote-host'
    elif change == 'disabled': value['binding']['enabled'] = False
    else: value['binding']['provider_id'] = ''
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        probe_module.ProviderStartupProbe(path, binding.host)


@pytest.mark.parametrize('mismatch', [None, 'account', 'executable', 'model', 'missing'])
def test_spawn_binding_is_checked_before_send_and_owned_child_is_closed(pinned, mismatch):
    path, value, binding = pinned
    probe = probe_module.ProviderStartupProbe(path, binding.host)
    calls = []
    account = dict(binding.public(), default_revision=1)
    session = {'account_binding': account, 'effective_model': probe.model, 'effective_effort': probe.effort}
    if mismatch == 'account': account['alias'] = 'foreign'
    if mismatch == 'executable': account['executable_sha256'] = '0' * 64
    if mismatch == 'model': session['effective_model'] = 'other'
    if mismatch == 'missing': session.pop('account_binding')
    def rpc(payload, prefix):
        calls.append(prefix)
        if prefix == 'spawn_catalog_get':
            return {'type': 'spawn_catalog_get.ok', 'catalog_version': 'fixture',
                    'profiles': {'desktop_manual': {'codex': ['catalog-default', 'medium']}}}
        if prefix == 'spawn':
            assert payload['model'] == probe.model and payload['effort'] == probe.effort
            assert 'account' not in payload
            return {'stream_id': 'fixture-host:v2-owned', 'session': session}
        if prefix == 'send':
            return {'type': 'send.result', 'receipt_id': 'receipt', 'delivery': 'landed'}
        pytest.fail(prefix)
    kwargs = dict(rpc=rpc, wait_ready=lambda _: calls.append('ready'),
                  wait_event=lambda *_: calls.append('reply'), verify_teardown=None,
                  register_owned=lambda *_: calls.append('owned'),
                  close_owned=lambda _: calls.append('closed'), provider_probe=probe)
    if mismatch:
        with pytest.raises(RuntimeError, match='provider_launch_'):
            smoke.run_cell(binding.host, 'codex', 'promptless', **kwargs)
        assert 'send' not in calls and 'ready' not in calls
    else:
        smoke.run_cell(binding.host, 'codex', 'promptless', **kwargs)
        assert calls.count('send') == calls.count('reply') == 1
    assert calls[-1] == 'closed' and calls.count('owned') == 1


@pytest.mark.parametrize('argv', [
    ['--provider-tuple', 'tuple.json'], ['ws://candidate', '--provider-tuple', 'tuple.json'],
    ['--token-path', 'token'], ['--dry-run', '--provider-tuple', 'tuple.json'],
])
def test_seam_options_never_default_to_full_fleet(monkeypatch, argv):
    monkeypatch.setattr(smoke, 'run_matrix', lambda *_a, **_k: pytest.fail('must not spawn'))
    with pytest.raises(SystemExit) as caught:
        smoke.main(argv)
    assert caught.value.code == 2


def test_native_failure_is_non_green_and_never_spawns(pinned, monkeypatch, capsys):
    path, value, binding = pinned
    monkeypatch.setattr(probe_module, 'codex_observation', lambda _: (_ for _ in ()).throw(AccountError('provider_capacity')))
    monkeypatch.setattr(smoke, 'run_matrix', lambda *_a, **_k: pytest.fail('must stop on diagnosed environment failure'))
    assert smoke.main(['ws://candidate', binding.host, '--provider-tuple', str(path)]) == smoke.EXIT_UNTESTED
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'UNTESTED' and not result['ok'] and result['cells'] == []
    assert result['native_identity']['reason'] == 'provider_capacity'


def test_explicit_endpoint_credential_and_single_tuple_are_forwarded(pinned, monkeypatch, capsys):
    path, value, binding = pinned
    monkeypatch.setattr(probe_module, 'codex_observation', lambda _: {'provider_id': 'identity-a', 'pool_id': 'identity-a'})
    def matrix(url, token, timeout, cells, *, evidence, **kwargs):
        assert url == 'ws://candidate' and token == path.parent / 'candidate.token'
        assert cells == ((binding.host, 'codex', 'promptless'),)
        assert kwargs['provider_probe'].binding == binding
        evidence.append({'host': binding.host, 'provider': 'codex', 'prompt_mode': 'promptless', 'outcome': 'passed'})
        return []
    monkeypatch.setattr(smoke, 'run_matrix', matrix)
    assert smoke.main(['ws://candidate', binding.host, '--provider-tuple', str(path),
                       '--token-path', str(path.parent / 'candidate.token')]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['native_identity']['native_identity'] == 'matched' and result['ok']


def test_pinned_tuple_cannot_run_as_a_matrix_or_foreign_cell(pinned, monkeypatch):
    path, value, binding = pinned
    probe = probe_module.ProviderStartupProbe(path, binding.host)
    monkeypatch.setattr(smoke, '_operator_connection', lambda *_a: pytest.fail('must not connect'))
    with pytest.raises(ValueError, match='exactly its local Codex promptless cell'):
        smoke.run_matrix('ws://candidate', path, 1, ((binding.host, 'claude', 'promptless'),), provider_probe=probe)


def test_config_drift_during_native_read_cannot_qualify(pinned, monkeypatch):
    path, value, binding = pinned
    def observe(_):
        (Path(binding.home) / 'config.toml').write_text('model = "changed"\n')
        return {'provider_id': binding.provider_id, 'pool_id': binding.pool_id}
    monkeypatch.setattr(probe_module, 'codex_observation', observe)
    with pytest.raises(AccountError, match='provider_config_changed'):
        probe_module.ProviderStartupProbe(path, binding.host).observe()
