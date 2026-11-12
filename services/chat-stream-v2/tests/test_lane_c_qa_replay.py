"""Synthetic QA admission replays; historical outcome inputs were unavailable."""
import asyncio

import pytest

import qa_dispatch
from sessions import VerbError
from test_qa_dispatch_counter import SPEC, LEAD, TOKEN, state, two_rejects, report_msg, issue


def test_worker_without_spec_can_commission_with_stale_cycle():
    async def run():
        store, sessions, _, _, _, _ = await state()
        try:
            await store.update_session('localhost', 'lead', role='worker', spec_id=None, spec_ids=[])
            row = {'role': 'qa', 'phase': 'spec', 'spec_ids': [SPEC]}
            msg = {'stream_token': TOKEN, 'from_stream_id': LEAD, 'qa_spec_id': SPEC,
                   'qa_surface': 'admission', 'qa_cycle': 99, 'initial_prompt': 'Review the change'}
            result = await qa_dispatch.admit(store, msg, row=row, reviewer='localhost:new-reviewer',
                                             generation='reviewer-generation', msg_id=0)
            assert result['spec_id'] == SPEC
            assert result['cycle'] == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_missing_surface_uses_own_spec_phase_counter():
    async def run():
        store, sessions, tmux, comms, ledger, server = await state()
        try:
            # A locked named surface does not lock the fallback key.
            await two_rejects(comms, ledger, server)
            await store.update_session('localhost', 'qa3', phase='spec')
            for name in ('qa4', 'qa5'):
                await sessions.open('localhost', name, role='qa', phase='spec', parent_stream_id=LEAD,
                                    spec_id=SPEC, spec_ids=[SPEC], bootstrap_state='ready')
            row = {'role': 'qa', 'phase': 'spec', 'spec_ids': [SPEC]}
            msg = {'stream_token': TOKEN, 'from_stream_id': LEAD, 'initial_prompt': 'Review the change'}
            fallback = f'{SPEC}:spec'
            for name in ('qa3', 'qa4'):
                target = await store.fetch_session('localhost', name)
                result = await qa_dispatch.admit(store, msg, row=row, reviewer=f'localhost:{name}',
                                                 generation=target['session_generation'], msg_id=0)
                assert result['surface'] == fallback
                await ledger.ingest(report_msg(name))
            target = await store.fetch_session('localhost', 'qa5')
            with pytest.raises(VerbError) as pending:
                await qa_dispatch.admit(store, msg, row=row, reviewer='localhost:qa5',
                                        generation=target['session_generation'], msg_id=0)
            assert pending.value.code == 'qa_adjudication_required'
            assert pending.value.extra['surface'] == fallback
            assert pending.value.extra['cycle'] == 1
            assert pending.value.extra['report_ids'] == ['reject-qa3', 'reject-qa4']
            for name in ('qa3', 'qa4'):
                adjudicated = await issue(server, 'adjudicate', surface=fallback,
                    report_id=f'reject-{name}', adjudicated_valid=True, reason='Valid synthetic reject')
                assert adjudicated['type'] == 'coordination.spec_issue.adjudicate.ok'
            with pytest.raises(VerbError) as exc:
                target = await store.fetch_session('localhost', 'qa5')
                await qa_dispatch.admit(store, msg, row=row, reviewer='localhost:qa5',
                                        generation=target['session_generation'], msg_id=0)
            assert exc.value.code == 'qa_dispatch_reject_limit'
            diagnosed = await issue(server, 'diagnose', surface=fallback, diagnosis_id='fallback-pivot',
                                    diagnosis='Two valid synthetic rejects', pivot='Repair the surface')
            assert diagnosed['next_cycle'] == 2
            result = await qa_dispatch.admit(store, msg, row=row, reviewer='localhost:qa5',
                                             generation=target['session_generation'], msg_id=0)
            assert result['cycle'] == 2
        finally:
            store.stop()
    asyncio.run(run())


def test_unscoped_qa_spawn_warns_and_keeps_commission():
    import hashlib
    from sessions import Sessions
    from spawnctl import SpawnCtl
    from store import Store
    from test_spec_binding_provenance import SpawnTmux

    async def run():
        store = Store(); store.start(); tmux = SpawnTmux()
        sessions = Sessions(store, tmux=tmux, local_host='localhost')
        ctl = SpawnCtl(store, sessions, tmux=tmux)
        try:
            await sessions.open('localhost', 'lead', role='worker', token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
                                token_hash_version='sha256:v1')
            msg = {'objective': 'Review the work', 'command': 'provider',
                'session_name': 'unscoped-reviewer', 'request_id': 'unscoped-reviewer',
                'idempotency_key': 'unscoped-reviewer', 'parent_stream_id': LEAD,
                'from_stream_id': LEAD, 'stream_token': TOKEN, 'role': 'qa'}
            result = await ctl.spawn(msg, 'localhost')
            assert result['type'] == 'spawn.ok'
            assert 'warnings' in result, result
            assert any('scope' in str(w).lower() and 'unknown' in str(w).lower() for w in result['warnings'])
            row = await store.submit(lambda conn: conn.execute(
                'select * from v2_qa_commissions where reviewer=?', ('localhost:unscoped-reviewer',)).fetchone())
            assert row is not None
            replay = await ctl.spawn({**msg, 'request_id': 'unscoped-retry'}, 'localhost')
            assert replay['stream_id'] == result['stream_id']
            assert any('scope' in str(w).lower() and 'unknown' in str(w).lower() for w in replay['warnings'])
        finally:
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            for name in tuple(tmux.live): await tmux.kill_session(name)
            store.stop()
    asyncio.run(run())


def test_unknown_coordinator_is_recorded_but_forged_claim_is_refused():
    async def run():
        store, _, _, _, _, _ = await state()
        try:
            row = {'role': 'qa', 'phase': 'spec', 'spec_ids': [SPEC]}
            result = await qa_dispatch.admit(store, {'initial_prompt': 'Review'}, row=row,
                reviewer='localhost:reviewer', generation='reviewer-generation', msg_id=0)
            assert result['coordinator_generation'] == ''
            stored = await store.submit(lambda conn: conn.execute(
                'select coordinator,coordinator_generation from v2_qa_commissions where reviewer=?',
                ('localhost:reviewer',)).fetchone())
            assert tuple(stored) == ('', '')
            null_token = await qa_dispatch.admit(store, {'stream_token': None, 'initial_prompt': 'Review'}, row=row,
                reviewer='localhost:null-token', generation='null-generation', msg_id=0)
            assert null_token['coordinator_generation'] == ''
            with pytest.raises(VerbError) as exc:
                await qa_dispatch.admit(store, {'from_stream_id': LEAD, 'initial_prompt': 'Review'},
                    row=row, reviewer='localhost:other-reviewer', generation='other-generation', msg_id=0)
            assert exc.value.code == 'qa_unauthorized'
            with pytest.raises(VerbError) as bad_token:
                await qa_dispatch.admit(store, {'stream_token': 'invalid', 'initial_prompt': 'Review'},
                    row=row, reviewer='localhost:other-reviewer', generation='other-generation', msg_id=0)
            assert bad_token.value.code == 'qa_unauthorized'
            with pytest.raises(VerbError) as unverified:
                await qa_dispatch.admit(store, {'_auth_context': {'stream_id': LEAD, 'token_verified': False}},
                    row=row, reviewer='localhost:other-reviewer', generation='other-generation', msg_id=0)
            assert unverified.value.code == 'qa_unauthorized'
        finally:
            store.stop()
    asyncio.run(run())


def test_unknown_coordinator_report_still_binds_and_can_be_adjudicated():
    async def run():
        store, _, _, _, ledger, server = await state()
        try:
            target = await store.fetch_session('localhost', 'qa3')
            row = {'role': 'qa', 'phase': 'spec', 'spec_ids': [SPEC]}
            await qa_dispatch.admit(store, {'qa_surface': 'admission', 'initial_prompt': 'Review'},
                row=row, reviewer='localhost:qa3', generation=target['session_generation'], msg_id=0)
            await ledger.ingest(report_msg('qa3'))
            result = await issue(server, 'adjudicate', report_id='reject-qa3',
                                 adjudicated_valid=True, reason='Valid synthetic reject')
            assert result['type'] == 'coordination.spec_issue.adjudicate.ok'
        finally:
            store.stop()
    asyncio.run(run())


def test_untagged_send_to_qa_remains_ordinary():
    async def run():
        store, _, _, comms, _, _ = await state()
        try:
            result = await comms.send({'stream_id': 'localhost:qa1', 'text': 'Routine update',
                                       'from_stream_id': LEAD, 'stream_token': TOKEN,
                                       'request_id': 'ordinary-update', 'msg_id': 7})
            assert result['delivery'] == 'landed'
            count = await store.submit(lambda conn: conn.execute(
                'select count(*) from v2_qa_commissions').fetchone()[0])
            assert count == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_surface_text_is_not_a_form_refusal():
    async def run():
        store, _, _, _, _, _ = await state()
        try:
            row = {'role': 'qa', 'phase': 'spec', 'spec_ids': [SPEC]}
            result = await qa_dispatch.admit(store, {'stream_token': TOKEN, 'from_stream_id': LEAD,
                'qa_surface': 'Review (client flow): long description', 'initial_prompt': 'Review'},
                row=row, reviewer='localhost:reviewer', generation='reviewer-generation', msg_id=0)
            assert result['surface'] == 'Review (client flow): long description'
        finally:
            store.stop()
    asyncio.run(run())
