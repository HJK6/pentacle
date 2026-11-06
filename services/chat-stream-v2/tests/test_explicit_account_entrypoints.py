"""Explicit selection reaches production builders without changing defaults."""
import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest

import launch
import spawn_fleet_smoke as smoke
from spawnctl import SpawnCtl
from test_account_runtime import setup


def test_commissioned_planner_selects_account_through_launch_builder(tmp_path):
    async def run():
        store, runtime = await setup()
        ctl = SpawnCtl(store, runtime.sessions, tmux=object(),
                       machine=launch.local_machine('host-a', cwd=str(tmp_path), codex_bin='/bin/echo'))
        ctl.account_runtime = runtime
        await store.open_session('host-a', 'planner', role='planner',
                                 session_generation='planner-generation', spec_id='spec_example')
        auth = {'token_verified': True, 'stream_id': 'host-a:planner',
                'session_generation': 'planner-generation'}
        try:
            command, _, fields = await ctl._resolve_launch({
                'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high',
                'account': 'spare', '_auth_context': auth, '_defer_launch_token_stage': True,
                'parent_stream_id': 'host-a:planner', 'role': 'qa', 'phase': 'spec',
            }, 'host-a', 'review')
            assert fields['account_binding']['alias'] == 'spare'
            assert fields['account_binding']['home'] in command
            assert (await runtime.run(lambda a: a.defaults()))[0]['alias'] == 'primary'
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('requested,expected', [(None, 'spare'), ('primary', 'primary')])
def test_fresh_handoff_retains_source_alias_unless_explicitly_overridden(tmp_path, requested, expected):
    from test_account_runtime import AUTH
    from test_account_rotation import binding
    async def run():
        store, runtime = await setup()
        ctl = SpawnCtl(store, runtime.sessions, tmux=object(),
            machine=launch.local_machine('host-a', cwd=str(tmp_path), codex_bin='/bin/echo'))
        ctl.account_runtime = runtime
        try:
            auth = {**AUTH, 'session_generation': 'bound-fixture'}
            await store.open_session('host-a', 'front-desk', session_generation=auth['session_generation'],
                provider='codex', effective_model='gpt-6.1-sol', effective_effort='high',
                account_binding=binding('spare').public())
            msg = {'handoff': True, 'handoff_from_stream_id': AUTH['stream_id'],
                'initial_prompt': 'Checkpoint: continue the current commission.',
                '_auth_context': auth, '_defer_launch_token_stage': True}
            if requested is not None:
                msg['account'] = requested
            command, _, fields = await ctl._resolve_launch(msg, 'host-a', 'successor')
            assert fields['account_binding']['alias'] == expected
            assert fields['account_binding']['home'] in command and '--resume' not in command
            assert (await runtime.run(lambda a: a.defaults()))[0]['alias'] == 'primary'
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['prompted', 'promptless'])
def test_canonical_cell_carries_and_checks_explicit_account(mode):
    seen, closed = [], []
    binding = {'host': 'host-a', 'provider': 'codex', 'alias': 'spare', 'generation': 'login-one'}
    def rpc(payload, expected):
        seen.append(payload)
        if expected == 'spawn_catalog_get':
            return {'type': 'spawn_catalog_get.ok', 'catalog_version': 'fixture',
                    'profiles': {'desktop_manual': {'codex': ['gpt-6.1-sol', 'high']}}}
        if expected == 'spawn':
            return {'type': 'spawn.ok', 'stream_id': 'host-a:owned',
                    'session': {'account_binding': binding}}
        if expected == 'list_sessions':
            return {'type': 'list_sessions.ok', 'active': [{'stream_id': 'host-a:owned', 'account_binding': binding}]}
        return {'type': 'send.result', 'delivery': 'accepted', 'receipt_id': 'fixture'}
    result = smoke.run_cell('host-a', 'codex', mode, account='spare', rpc=rpc,
        wait_ready=lambda _: None, wait_event=lambda *_: None, verify_teardown=lambda _: None,
        register_owned=lambda *_: None, close_owned=closed.append)
    assert next(p for p in seen if p['type'] == 'spawn')['account'] == 'spare'
    assert result['account'] == 'spare'
    assert result['account_binding'] == binding
    assert closed == ['host-a:owned']


def test_omitted_selector_keeps_default_builder_compatibility():
    seen = []
    def rpc(payload, expected):
        if expected == 'spawn_catalog_get':
            return {'type': 'spawn_catalog_get.ok', 'catalog_version': 'fixture',
                    'profiles': {'desktop_manual': {'codex': ['gpt-6.1-sol', 'high']}}}
        seen.append(payload)
        raise RuntimeError('isolated admission capture; zero provider invocations')
    with pytest.raises(RuntimeError, match='isolated admission capture'):
        smoke.run_cell('host-a', 'codex', 'prompted', rpc=rpc,
            wait_ready=lambda _: None, wait_event=lambda *_: None, verify_teardown=lambda _: None)
    assert len(seen) == 1
    assert 'account' not in seen[0]


def test_account_map_requires_complete_unique_known_cells():
    cells = (('host-a', 'codex', 'prompted'), ('host-b', 'codex', 'promptless'), ('host-a', 'claude', 'prompted'))
    assert smoke.account_map(['codex=primary', 'host-b/codex=spare', 'claude=primary'], cells) == {
        ('host-a', 'codex'): 'primary', ('host-b', 'codex'): 'spare', ('host-a', 'claude'): 'primary'}
    assert smoke.account_map([], cells) == {}
    for entries in (['codex=primary'], ['codex=primary', 'codex=primary'], ['other=primary'],
                    ['host-c/codex=primary'], ['codex='], ['codex=../home']):
        with pytest.raises(ValueError):
            smoke.account_map(entries, cells)


@pytest.mark.parametrize('auth', [{}, {'operator_authenticated': True},
    {'service_authenticated': True, 'service_actor': 'watcher'},
    {'service_authenticated': True, 'service_actor': 'daemon:scheduler', 'account_intent': 'spare'}])
def test_choice_does_not_grant_administration_and_forged_service_intent_refuses(auth):
    async def run():
        from provider_accounts import AccountError
        from sessions import VerbError
        store, runtime = await setup()
        try:
            request = {'account': 'spare', '_auth_context': auth}
            if auth.get('operator_authenticated'):
                chosen = await runtime.choose_launch(host='host-a', provider='codex', model='fixture', effort='high', message=request)
                assert chosen.alias == 'spare'
            else:
                with pytest.raises(AccountError, match='account_control_unauthorized'):
                    await runtime.choose_launch(host='host-a', provider='codex', model='fixture', effort='high', message=request)
            with pytest.raises(VerbError, match='account_control_unauthorized'):
                await runtime.operation({**request, 'action': 'default-set', 'host': 'host-a', 'provider': 'codex'})
            assert (await runtime.run(lambda a: a.defaults()))[0]['alias'] == 'primary'
        finally:
            store.stop()
    asyncio.run(run())


def test_wrong_account_receipt_is_closed_before_promptless_send():
    seen, closed = [], []
    def rpc(payload, prefix):
        seen.append(prefix)
        if prefix == 'spawn_catalog_get':
            return {'type': 'spawn_catalog_get.ok', 'catalog_version': 'fixture',
                    'profiles': {'desktop_manual': {'codex': ['gpt-6.1-sol', 'high']}}}
        return {'stream_id': 'host-a:owned', 'session': {'account_binding': {
            'host': 'host-a', 'provider': 'codex', 'alias': 'primary', 'generation': 'login-one'}}}
    with pytest.raises(RuntimeError, match='account binding was not honored'):
        smoke.run_cell('host-a', 'codex', 'promptless', account='spare', rpc=rpc,
            wait_ready=lambda _: None, wait_event=lambda *_: None, verify_teardown=None,
            register_owned=lambda *_: None, close_owned=closed.append)
    assert seen == ['spawn_catalog_get', 'spawn']
    assert closed == ['host-a:owned']


@pytest.mark.parametrize('account', [None, 'primary', 'spare'])
@pytest.mark.parametrize('kind', ['failure', 'quota', 'untested'])
def test_smoke_notice_keys_preserve_omission_and_separate_accounts(monkeypatch, account, kind):
    cards = []

    @contextmanager
    def connection(*_args):
        yield (None, None, None, None, None)

    def failed_cell(*_args, **_kwargs):
        if kind == 'quota':
            raise smoke.CodexQuotaExhausted('host-a', None)
        if kind == 'untested':
            raise smoke.ProviderTurnError('provider_throttled', 'fixture')
        raise smoke.ProviderTurnError('provider_error', 'fixture')

    monkeypatch.setattr(smoke, '_operator_connection', connection)
    monkeypatch.setattr(smoke, 'run_cell', failed_cell)
    monkeypatch.setattr(smoke, 'publish_smoke_card', lambda payload: cards.append(payload) or {})
    monkeypatch.setattr(smoke, 'notify_operator_failure', lambda _payload: {})
    accounts = {('host-a', 'codex'): account} if account is not None else {}
    (row,) = smoke.run_matrix('ws://fixture', Path('unused'), 1,
        (('host-a', 'codex', 'promptless'),), accounts=accounts)
    suffix = '' if account is None else ':' + account
    expected = {
        'failure': f"spawn-fleet-smoke:host-a:codex{suffix}:promptless:{row['stage']}",
        'quota': f'spawn-fleet-smoke-quota:host-a{suffix}',
        'untested': f'spawn-fleet-smoke-untested:host-a:codex{suffix}:promptless',
    }
    assert [card['dedup_key'] for card in cards] == [expected[kind]]
