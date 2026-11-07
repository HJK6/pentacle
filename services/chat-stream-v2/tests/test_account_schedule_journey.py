"""Durable account choice crosses real schedule and spawn admission boundaries."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid

import pytest
import launch
from account_runtime import AccountRuntime
from sessions import VerbError
from spawnctl import SpawnCtl
from store import Store
from window_schedule import WindowSchedule
from work_lane_link import link_specs
from provider_cli_fixtures import seed_pin
from test_account_rotation import binding
from test_codex_ssh_free_readiness import CodexTui, NativeUserProof
from test_provider_cli_pin import artifact_executor
from test_qa_dispatch_counter import state, LEAD, SPEC, TOKEN
from test_window_schedule_contract import FakeComms


async def cap_fixture_pool(runtime,*,pct=21,revision=0):
    from test_account_rotation import observation
    from test_account_pool_control import OPERATOR
    account=await runtime.run(lambda a:a.bound(binding('spare',host='localhost').key))
    now=runtime.clock();obs=observation(account)
    obs.update(observed_at=now)
    obs['windows']['weekly'].update(pct=pct,reset_at=now+1000)
    await runtime.record_observation(account,obs)
    if revision==0:
        await runtime.operation(dict(action='pool-control-set',provider='codex',pool_id=account.pool_id,
            expected_revision=0,operation_id='fixture-cap',blocked=False,stop_pct=21,window='weekly',
            reason='owned cap drill',authority_ref='fixture-order',_auth_context=OPERATOR))


@pytest.mark.parametrize('race',[False,True])
def test_current_pool_cap_refuses_real_spawn_at_selection_or_final_admit(tmp_path,race):
    async def run():
        store,runtime,pane,ctl,surface,message=await fixture(tmp_path)
        try:
            if race:
                choose=runtime.choose_launch
                async def crossed(**kwargs):
                    selected=await choose(**kwargs)
                    await cap_fixture_pool(runtime)
                    return selected
                runtime.choose_launch=crossed
            else: await cap_fixture_pool(runtime)
            with pytest.raises(VerbError) as refused:
                await ctl._spawn_guarded(message('spawn',provider='codex',account='spare',model='gpt-6.1-sol',effort='high',
                    initial_prompt='native user prompt',objective='Owned final-admission cap drill',idempotency_key='cap-race'), 'localhost')
            assert refused.value.code=='account_pool_cap_reached'
            assert pane.launches==0
            assert not [row for row in await store.list_sessions() if (row.get('account_binding') or {}).get('alias')=='spare']
        finally:
            for task in tuple(ctl._background_spawns): task.cancel()
            await asyncio.gather(*tuple(ctl._background_spawns),return_exceptions=True)
            store.stop()
    asyncio.run(run())


def test_admitted_spawn_replay_at_cap_keeps_one_process_and_original_receipt(tmp_path):
    async def run():
        store,runtime,pane,ctl,surface,message=await fixture(tmp_path)
        try:
            msg=message('spawn',provider='codex',account='spare',model='gpt-6.1-sol',effort='high',
                initial_prompt='native user prompt',objective='Owned receipt replay',idempotency_key='cap-replay')
            first=await ctl._spawn_guarded(dict(msg),'localhost')
            await cap_fixture_pool(runtime)
            replay=await ctl._spawn_guarded(dict(msg),'localhost')
            assert first['stream_id']==replay['stream_id'] and pane.launches==1
        finally:
            for task in tuple(ctl._background_spawns): task.cancel()
            await asyncio.gather(*tuple(ctl._background_spawns),return_exceptions=True)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('recurring',[False,True])
def test_schedule_materialized_before_cap_checks_current_control_at_fire(tmp_path,recurring):
    async def run():
        store,runtime,pane,ctl,surface,message=await fixture(tmp_path)
        try:
            template=dict(provider='codex',account='spare',model='gpt-6.1-sol',effort='high',
                spec_ids=[SPEC],initial_prompt='native user prompt',objective='Owned scheduled cap drill')
            if recurring:
                created=await surface.recurring_create(message('schedule.recurring.create',name='owned-rule',every='daily',
                    at_time='00:00',timezone='UTC',parent='none',target_host='localhost',**template))
                materialized=await surface.recurring_run(message('schedule.recurring.run',recurring_id=created['recurring']['recurring_id']))
                schedule_id=materialized['schedule_id']
                await surface.recurring_pause(message('schedule.recurring.pause',recurring_id=created['recurring']['recurring_id']))
            else:
                created=await surface.schedule_insert(message('schedule.insert',fires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
                    target_host='localhost',**template))
                schedule_id=created['schedule']['schedule_id']
            await cap_fixture_pool(runtime)
            with pytest.raises(VerbError) as refused: await surface._fire_schedule(schedule_id)
            assert refused.value.extra['spawn']['error_code']=='account_pool_cap_reached'
            assert pane.launches==0
            pending=await surface.schedule_insert(message('schedule.insert',fires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),target_host='localhost',**template))
            cancelled=await surface.schedule_cancel(message('schedule.cancel',schedule_id=pending['schedule']['schedule_id']))
            assert cancelled['type']=='schedule.cancel.ok'
        finally:
            for task in tuple(ctl._background_spawns): task.cancel()
            await asyncio.gather(*tuple(ctl._background_spawns),return_exceptions=True)
            store.stop()
    asyncio.run(run())


async def fixture(tmp_path):
    store, sessions, _, comms, _, server = await state(tmp_path/'state.db')
    class Catalog:
        def resolution_for(self, value):
            return 'resolved' if value == SPEC else 'zero_matches'
        def canonical_spec_identity(self, value):
            return value if value == SPEC else None
    class Pane(CodexTui):
        launches = 0
        async def new_session(self, *args, **kwargs):
            self.launches += 1
            await super().new_session(*args, **kwargs)
        async def pretrust_codex_cwd(self, path, **kwargs):
            assert kwargs['code_home'] == str(tmp_path/'spare')
    pane = Pane()
    ctl = SpawnCtl(store, sessions, tmux=pane, specs=Catalog(),
        machine=launch.local_machine('localhost', cwd=str(tmp_path), codex_bin='/bin/echo'),
        submission_proof=NativeUserProof())
    runtime = AccountRuntime(store, sessions, policy={'enabled': True})
    runtime.attach(server, ctl, comms)
    await link_specs(store, SPEC)
    await store.update_session('localhost', 'lead', spec_ids=[SPEC], qualified_spec_ids=[SPEC],
        spec_binding_provenance=[{'spec_id': SPEC, 'provenance': 'spawn_explicit',
            'granting_principal': 'operator', 'granted_at': '2026-11-06T00:00:00Z'}])
    await sessions.refresh()
    for alias in ('primary', 'spare'):
        await runtime.run(lambda a, alias=alias: a.register(replace(
            binding(alias, host='localhost'), home=str(tmp_path/alias))))
    await runtime.run(lambda a: a.set_default(host='localhost', provider='codex', alias='primary'))
    await seed_pin(runtime)
    await artifact_executor(runtime)
    owner = await store.fetch_session('localhost', 'lead')
    auth = {'token_verified': True, 'stream_id': LEAD,
            'session_generation': owner['session_generation']}
    def message(verb, **values):
        return {'type': verb, 'request_id': str(uuid.uuid4()), 'from_stream_id': LEAD,
                'stream_token': TOKEN, '_auth_context': auth, **values}
    surface = WindowSchedule(store, sessions, FakeComms(), ctl, local_host='localhost')
    surface.mark_store_ready()
    return store, runtime, pane, ctl, surface, message


@pytest.mark.parametrize('recurring', [False, True])
@pytest.mark.parametrize('drift', ['generation', 'owner', 'unavailable'])
def test_explicit_schedule_survives_restart_and_refuses_drift(tmp_path, recurring, drift):
    async def run():
        store, runtime, pane, ctl, surface, message = await fixture(tmp_path)
        try:
            template = {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high',
                'account': 'spare', 'spec_ids': [SPEC], 'objective': 'Exercise retained account',
                'initial_prompt': 'native user prompt'}
            if recurring:
                created = await surface.recurring_create(message('schedule.recurring.create',
                    name='account-rule', every='daily', at_time='00:00', timezone='UTC',
                    parent='none', target_host='localhost', **template))
                materialized = await surface.recurring_run(message('schedule.recurring.run',
                    recurring_id=created['recurring']['recurring_id']))
                schedule_id = materialized['schedule_id']
            else:
                created = await surface.schedule_insert(message('schedule.insert',
                    fires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
                    target_host='localhost', **template))
                schedule_id = created['schedule']['schedule_id']
            store.stop()
            store = Store(str(tmp_path/'state.db'))
            store.set_spec_identity_resolver(lambda value: value if value == SPEC else None)
            store.start()
            runtime.store = runtime.sessions.store = ctl.store = store
            await runtime.sessions.refresh()
            # The scheduler is reconstructed from its durable row, with a different default.
            await runtime.run(lambda a: a.set_default(host='localhost', provider='codex', alias='spare'))
            await runtime.run(lambda a: a.set_default(host='localhost', provider='codex', alias='primary'))
            if drift == 'generation':
                await runtime.run(lambda a: a.register(replace(binding('spare', host='localhost'),
                    generation='login-two', home=str(tmp_path/'spare'))))
            elif drift == 'owner':
                await store.open_session('localhost', 'lead', session_generation='replacement-owner')
            else:
                await runtime.run(lambda a: a.failure(binding('spare', host='localhost').key,
                    'auth', now=2000000000))
            restarted = WindowSchedule(store, runtime.sessions, FakeComms(), ctl, local_host='localhost')
            restarted.mark_store_ready()
            if drift == 'generation':
                await restarted._fire_schedule(schedule_id)
            else:
                with pytest.raises(VerbError) as refused:
                    await restarted._fire_schedule(schedule_id)
                assert refused.value.extra['spawn']['error_code'] == (
                    'stale_owner_generation' if drift == 'owner' else 'account_unavailable')
            row = await store.submit(lambda c: dict(c.execute(
                'SELECT * FROM v2_schedules WHERE schedule_id=?', (schedule_id,)).fetchone()))
            assert row['account'] == 'spare'
            children = [r for r in await store.list_sessions() if (r.get('account_binding') or {}).get('alias') == 'spare']
            if drift == 'generation':
                assert pane.launches == 1, row
                assert len(children) == 1
                assert children[0]['account_binding']['generation'] == 'login-two'
            else:
                assert pane.launches == 0, row
                assert not children
            assert (await runtime.run(lambda a: a.defaults()))[0]['alias'] == 'primary'
        finally:
            for task in tuple(ctl._background_spawns): task.cancel()
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            store.stop()
    asyncio.run(run())
