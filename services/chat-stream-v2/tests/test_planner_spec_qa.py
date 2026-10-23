"""Planner SPEC-QA through real daemon admission; only provider/tmux is fake."""
import asyncio
import pytest
from sessions import VerbError
from test_qa_dispatch_counter import LEAD, SPEC, TOKEN, issue, report_msg, send_msg, state


def test_planner_spec_send_adjudicate_diagnose(monkeypatch):
    monkeypatch.setenv('PENTACLE_QA_DISPATCH_MODE', 'enforce')
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
    monkeypatch.setenv('PENTACLE_QA_DISPATCH_MODE', 'enforce')
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


@pytest.mark.parametrize('case', ['missing_phase', 'implementation_phase', 'phase_override',
                                  'unassigned', 'different_assignment', 'secondary_tag',
                                  'unbound', 'worker', 'closed', 'forged', 'self_review'])
def test_planner_admission_boundaries(monkeypatch, case):
    monkeypatch.setenv('PENTACLE_QA_DISPATCH_MODE', 'enforce')
    async def run():
        store, _, tmux, comms, _, server = await state()
        try:
            await store.update_session('localhost', 'lead', role='planner')
            await store.update_session('localhost', 'qa1', phase='spec')
            msg = send_msg('qa1')
            if case in ('missing_phase', 'implementation_phase', 'phase_override'):
                await store.update_session('localhost', 'qa1',
                                           phase=None if case == 'missing_phase' else 'implementation')
                if case == 'phase_override':
                    msg['phase'] = 'spec'
            elif case == 'unassigned':
                await store.update_session('localhost', 'lead', assigned_spec_id='')
            elif case == 'different_assignment':
                await store.update_session('localhost', 'lead', assigned_spec_id='spec_other')
            elif case == 'secondary_tag':
                await store.update_session('localhost', 'lead', spec_ids=[SPEC, 'spec_other'],
                                           assigned_spec_id=None)
            elif case == 'unbound':
                await store.update_session('localhost', 'lead', spec_id='spec_other',
                                           spec_ids=['spec_other'], assigned_spec_id=SPEC)
            elif case in ('worker', 'closed'):
                await store.update_session('localhost', 'lead',
                                           **({'role': 'worker'} if case == 'worker' else {'status': 'closed'}))
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
    monkeypatch.setenv('PENTACLE_QA_DISPATCH_MODE', 'enforce')
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
            with pytest.raises(VerbError) as exc:
                await comms.send(send_msg('qa1'))
            assert exc.value.code in ('qa_unauthorized', 'qa_commission_conflict')
            assert (await issue(server, 'show'))['commissions'][0]['phase'] == original_phase
        finally:
            store.stop()
    asyncio.run(run())


def test_legacy_commission_migration_is_idempotent_and_preserves_authority(tmp_path, monkeypatch):
    import sqlite3
    from server import Server
    from store import Store
    monkeypatch.setenv('PENTACLE_QA_DISPATCH_MODE', 'enforce')
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
def test_planner_revalidates_changes_during_resolution(monkeypatch, change):
    monkeypatch.setenv('PENTACLE_QA_DISPATCH_MODE', 'enforce')
    async def run():
        store, _, tmux, comms, _, server = await state()
        try:
            await store.update_session('localhost', 'lead', role='planner')
            await store.update_session('localhost', 'qa1', phase='spec')
            original = store._qa_spec_lookup
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
            monkeypatch.setattr(store, '_qa_spec_lookup', lookup)
            with pytest.raises(VerbError) as exc:
                await comms.send(send_msg('qa1'))
            assert exc.value.code == 'qa_unauthorized'
            assert raced and not tmux.pastes
            assert not (await issue(server, 'show'))['commissions']
        finally:
            store.stop()
    asyncio.run(run())


def test_planner_cannot_diagnose_mixed_phases(monkeypatch):
    monkeypatch.setenv('PENTACLE_QA_DISPATCH_MODE', 'enforce')
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
    monkeypatch.setenv('PENTACLE_QA_DISPATCH_MODE', 'enforce')
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
