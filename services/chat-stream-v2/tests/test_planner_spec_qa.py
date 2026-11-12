"""Planner SPEC-QA through real daemon admission; only provider/tmux is fake."""
import asyncio
import pytest
from sessions import VerbError
from test_qa_dispatch_counter import LEAD, SPEC, TOKEN, issue, report_msg, send_msg, state


def test_planner_spec_send_adjudicate_diagnose(monkeypatch):
    async def run():
        store, _, tmux, comms, ledger, server = await state()
        try:
            await store.update_session('localhost', 'lead', role='planner')
            for name in ('qa1', 'qa2', 'qa3'):
                await store.update_session('localhost', name, phase='spec')
            for name in ('qa1', 'qa2'):
                assert (await comms.send(send_msg(name)))['delivery'] == 'landed'
                await ledger.ingest(report_msg(name))
                result = await issue(server, 'adjudicate', report_id=f'reject-{name}',
                                     adjudicated_valid=True, reason='Spec acceptance defect')
                assert result['type'].endswith('.ok'), result
            before = len(tmux.pastes)
            with pytest.raises(VerbError) as exc:
                await comms.send(send_msg('qa3'))
            assert exc.value.code == 'qa_dispatch_reject_limit'
            assert len(tmux.pastes) == before
            params = dict(diagnosis_id='planner-pivot', diagnosis='Review exposed a missing assumption',
                          pivot='Correct the assumption before review')
            result = await issue(server, 'diagnose', **params)
            assert result['next_cycle'] == 2, result
            assert (await issue(server, 'diagnose', **params))['next_cycle'] == 2
            assert (await comms.send(send_msg('qa3', qa_cycle=2)))['delivery'] == 'landed'
            assert all(row['phase'] == 'spec' for row in (await issue(server, 'show'))['commissions'])
        finally:
            store.stop()
    asyncio.run(run())


def test_planner_spec_spawn(monkeypatch):
    from spawnctl import SpawnCtl
    from test_spec_binding_provenance import SpawnTmux
    class Catalog:
        def resolution_for(self, value):
            return 'resolved' if value == SPEC else 'zero_matches'
        def canonical_spec_identity(self, value):
            return value if value == SPEC else None
    async def run():
        store, sessions, _, _, _, server = await state()
        tmux = SpawnTmux()
        ctl = SpawnCtl(store, sessions, tmux=tmux, specs=Catalog())
        try:
            await store.update_session('localhost', 'lead', role='planner')
            result = await ctl.spawn({
                'objective': 'Review the assigned spec', 'command': 'provider',
                'session_name': 'planner-spec-review', 'request_id': 'planner-spec-spawn',
                'parent_stream_id': LEAD, 'from_stream_id': LEAD, 'stream_token': TOKEN,
                'role': 'qa', 'phase': 'spec', 'spec_id': SPEC,
                'qa_spec_id': SPEC, 'qa_surface': 'admission', 'qa_cycle': 1,
            }, 'localhost')
            assert result['type'].endswith('.ok'), result
            assert (await issue(server, 'show'))['commissions'][0]['phase'] == 'spec'
        finally:
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            for name in tuple(tmux.live):
                await tmux.kill_session(name)
            store.stop()
    asyncio.run(run())


def test_planner_explicit_account_keeps_spec_phase_admission(tmp_path, monkeypatch):
    from dataclasses import replace
    import launch
    from account_runtime import AccountRuntime
    from spawnctl import SpawnCtl
    from test_account_rotation import binding
    from test_spec_binding_provenance import SpawnTmux
    from test_provider_cli_pin import artifact_executor
    from provider_cli_fixtures import seed_pin
    from work_lane_link import link_specs
    class Catalog:
        def resolution_for(self, value):
            return 'resolved' if value == SPEC else 'zero_matches'
        def canonical_spec_identity(self, value):
            return value if value == SPEC else None
    class ProviderTmux(SpawnTmux):
        async def new_session(self, name, command, cwd=None, env=None):
            self.nonce = (env or {}).get('PENTACLE_SPAWN_NONCE', '')
            await super().new_session(name, command, cwd=cwd, env=env)
        async def run(self, *_args, **_kwargs):
            return 0, f'PENTACLE_SPAWN_NONCE={self.nonce}\n'
        async def stage_text(self, *_args): pass
        async def pretrust_codex_cwd(self, path, **kwargs):
            assert kwargs.get('code_home') == str(tmp_path/'spare'), 'pretrust lost selected account home'
    async def run():
        store, sessions, _, comms, _, server = await state()
        tmux = ProviderTmux()
        ctl = SpawnCtl(store, sessions, tmux=tmux, specs=Catalog(),
            machine=launch.local_machine('localhost', cwd=str(tmp_path), codex_bin='/bin/echo'))
        runtime = AccountRuntime(store, sessions, policy={'enabled': True})
        runtime.attach(server, ctl, comms)
        try:
            await store.update_session('localhost', 'lead', role='planner')
            await link_specs(store, SPEC)
            for alias in ('primary', 'spare'):
                account = replace(binding(alias, host='localhost'), home=str(tmp_path/alias))
                await runtime.run(lambda a, account=account: a.register(account))
            await runtime.run(lambda a: a.set_default(host='localhost', provider='codex', alias='primary'))
            await seed_pin(runtime)
            await artifact_executor(runtime)
            owner = await store.fetch_session('localhost', 'lead')
            result = await ctl.spawn({
                'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high', 'account': 'spare',
                'objective': 'Review the assigned spec', 'visibility': 'hidden',
                'ready_marker': 'READY',
                'session_name': 'account-spec-review', 'request_id': 'account-spec-spawn',
                'parent_stream_id': LEAD, 'from_stream_id': LEAD, 'stream_token': TOKEN,
                'role': 'qa', 'phase': 'spec', 'spec_id': SPEC,
                'qa_spec_id': SPEC, 'qa_surface': 'admission', 'qa_cycle': 1,
                '_auth_context': {'token_verified': True, 'stream_id': LEAD,
                                  'session_generation': owner['session_generation']},
            }, 'localhost')
            assert result['type'] == 'spawn.ok', result
            row = await store.fetch_session('localhost', 'account-spec-review')
            assert row['account_binding']['alias'] == 'spare'
            assert (await issue(server, 'show'))['commissions'][0]['phase'] == 'spec'
            assert (await runtime.run(lambda a: a.defaults()))[0]['alias'] == 'primary'
        finally:
            for task in tuple(ctl._background_spawns): task.cancel()
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            for name in tuple(tmux.live): await tmux.kill_session(name)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('case', ['closed', 'forged', 'self_review'])
def test_planner_admission_boundaries(monkeypatch, case):
    async def run():
        store, _, tmux, comms, _, server = await state()
        try:
            await store.update_session('localhost', 'lead', role='planner')
            await store.update_session('localhost', 'qa1', phase='spec')
            msg = send_msg('qa1')
            if case == 'closed':
                await store.update_session('localhost', 'lead', status='closed')
            elif case == 'forged':
                msg['from_stream_id'] = 'localhost:qa2'
            elif case == 'self_review':
                msg['stream_id'] = LEAD
            with pytest.raises(VerbError) as exc:
                await comms.send(msg)
            assert exc.value.code in ('qa_unauthorized', 'qa_spec_mismatch', 'invalid_session',
                                      'caller_stream_mismatch'), exc.value.code
            assert not tmux.pastes
            assert not (await issue(server, 'show'))['commissions']
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('original_phase', ['implementation', None])
def test_planner_cannot_upgrade_existing_commission(monkeypatch, original_phase):
    async def run():
        store, _, _, comms, ledger, server = await state()
        try:
            await store.update_session('localhost', 'qa1', phase=original_phase)
            await comms.send(send_msg('qa1'))  # Lead retains all phases.
            await ledger.ingest(report_msg('qa1'))
            await store.update_session('localhost', 'lead', role='planner')
            await store.update_session('localhost', 'qa1', phase='spec')
            result = await issue(server, 'adjudicate', report_id='reject-qa1',
                                 adjudicated_valid=True, reason='Attempt upgrade')
            assert result['error_code'] == 'qa_unauthorized', result
            assert (await comms.send(send_msg('qa1')))['delivery'] == 'landed'
            assert (await issue(server, 'show'))['commissions'][0]['phase'] == original_phase
        finally:
            store.stop()
    asyncio.run(run())


def test_legacy_commission_migration_is_idempotent_and_preserves_authority(tmp_path, monkeypatch):
    import sqlite3
    from server import Server
    from store import Store
    path = tmp_path / 'legacy.db'
    async def run():
        store, _, _, comms, ledger, _ = await state(path)
        await comms.send(send_msg('qa1'))
        await ledger.ingest(report_msg('qa1'))
        store.stop()
        with sqlite3.connect(path) as conn:
            if 'phase' in {row[1] for row in conn.execute('PRAGMA table_info(v2_qa_commissions)')}:
                conn.execute('ALTER TABLE v2_qa_commissions DROP COLUMN phase')
            before = conn.execute('SELECT * FROM v2_qa_commissions').fetchall()
        for _ in range(2):
            store = Store(str(path))
            store.set_spec_identity_resolver(lambda value: value if value == SPEC else None)
            store.start()
            try:
                server = Server(store=store)
                commissions = (await issue(server, 'show'))['commissions']
                assert len(commissions) == 1 and commissions[0]['phase'] is None
                with sqlite3.connect(path) as conn:
                    cols = [r[1] for r in conn.execute('PRAGMA table_info(v2_qa_commissions)') if r[1] != 'phase']
                    assert conn.execute('SELECT '+','.join(cols)+' FROM v2_qa_commissions').fetchall() == before
                result = await issue(server, 'adjudicate', report_id='reject-qa1',
                                     adjudicated_valid=True, reason='Legacy lead review')
                assert result['type'].endswith('.ok'), result
                await store.update_session('localhost', 'lead', role='planner')
                result = await issue(server, 'adjudicate', report_id='reject-qa1',
                                     adjudicated_valid=False, reason='Legacy planner refuses')
                assert result['error_code'] == 'qa_unauthorized', result
                await store.update_session('localhost', 'lead', role='lead')
                await issue(server, 'adjudicate', report_id='reject-qa1',
                            adjudicated_valid=False, reason='Restore legacy lead review')
                with sqlite3.connect(path) as conn:
                    before = conn.execute('SELECT '+','.join(cols)+' FROM v2_qa_commissions').fetchall()
            finally:
                store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['assignment', 'phase'])
def test_admission_uses_current_scope_after_resolution(monkeypatch, change):
    async def run():
        store, _, _, comms, _, _ = await state()
        try:
            await store.update_session('localhost', 'lead', role='planner')
            await store.update_session('localhost', 'qa1', phase='spec')
            original = store._resolve_spec_inputs
            raced = False
            async def lookup(*args, **kwargs):
                nonlocal raced
                result = await original(*args, **kwargs)
                if not raced:
                    raced = True
                    if change == 'assignment':
                        await store.update_session('localhost', 'lead', assigned_spec_id='')
                    else:
                        await store.update_session('localhost', 'qa1', phase='implementation')
                return result
            monkeypatch.setattr(store, '_resolve_spec_inputs', lookup)
            assert (await comms.send(send_msg('qa1')))['delivery'] == 'landed'
            record = await store.submit(lambda conn: conn.execute(
                "SELECT spec_id,phase FROM v2_qa_commissions WHERE reviewer=?", ('localhost:qa1',)).fetchone())
            assert raced and record[0] == SPEC
            assert record[1] == ('implementation' if change == 'phase' else 'spec')
        finally:
            store.stop()
    asyncio.run(run())


def test_planner_cannot_diagnose_mixed_phases(monkeypatch):
    async def run():
        store, _, _, comms, ledger, server = await state()
        try:
            for name, phase in (('qa1', 'spec'), ('qa2', 'implementation')):
                await store.update_session('localhost', name, phase=phase)
                await comms.send(send_msg(name))
                await ledger.ingest(report_msg(name))
                result = await issue(server, 'adjudicate', report_id=f'reject-{name}',
                                     adjudicated_valid=True, reason='Substantive defect')
                assert result['type'].endswith('.ok'), result
            await store.update_session('localhost', 'lead', role='planner')
            params = dict(diagnosis_id='mixed-pivot', diagnosis='Different phases share counter',
                          pivot='Coordinator owns mixed-phase diagnosis')
            result = await issue(server, 'diagnose', **params)
            assert result['error_code'] == 'qa_unauthorized', result
            assert (await issue(server, 'show'))['cycle'] == 1
            await store.update_session('localhost', 'lead', role='lead')
            assert (await issue(server, 'diagnose', **params))['next_cycle'] == 2
        finally:
            store.stop()
    asyncio.run(run())


def test_shared_spec_planner_cannot_adjudicate_unowned_commission(monkeypatch):
    import hashlib
    async def run():
        store, sessions, _, comms, ledger, server = await state()
        try:
            await store.update_session('localhost', 'qa1', phase='SPEC')
            await comms.send(send_msg('qa1'))
            await ledger.ingest(report_msg('qa1'))
            await sessions.open('localhost', 'other-planner', role='planner', spec_id=SPEC,
                                token_hash=hashlib.sha256(b'other-token').hexdigest(),
                                token_hash_version='sha256:v1')
            result = await issue(server, 'adjudicate', report_id='reject-qa1',
                                 adjudicated_valid=True, reason='Unrelated planner',
                                 from_stream_id='localhost:other-planner', stream_token='other-token')
            assert result['error_code'] == 'qa_unauthorized', result
            assert (await issue(server, 'show'))['commissions'][0]['adjudicated_valid'] == 0
        finally:
            store.stop()
    asyncio.run(run())
