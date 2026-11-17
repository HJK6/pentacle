"""Isolated cap readings, notification episodes and restore admission."""
import asyncio
from dataclasses import replace

import pytest

from account_cap_monitor import CapMonitor
from account_pool_control import write, pool_key
from account_store import transaction
from provider_accounts import AccountError
from test_account_runtime import setup
from test_account_rotation import NOW, binding, observation
from test_account_pool_control import control
from test_account_cap_monitor import Notices


async def rig(tmp_path):
    store, runtime = await setup(str(tmp_path / 'state.db'), initialize_pin=False)
    clock = [NOW]
    runtime.clock = lambda: clock[0]
    notices = Notices()
    runtime.cap_monitor = CapMonitor(runtime)
    runtime.cap_monitor.attach(notices)
    return store, runtime, clock, notices


async def ceiling(runtime, **changes):
    def save(accounts):
        with transaction(accounts.conn):
            write(accounts, pool_key('codex', 'pool-primary'), control(**changes))
    await runtime.run(save)


async def reading(runtime, clock, pct=18, *, account=None, **changes):
    account = account or binding()
    value = observation(account, clock[0])
    value['windows']['weekly'].update(pct=pct, reset_at=NOW + 20000)
    value.update(changes)
    await runtime.record_observation(account, value)


async def pool(runtime):
    return next(p for p in (await runtime.run(lambda a: a.readback(now=runtime.clock())))['quota_pools']
                if p['pool_id'] == 'pool-primary')


def kinds(notices, kind):
    return [n for n in notices.notices.values() if n['kind'] == kind]


@pytest.mark.parametrize('failed', [False, True])
def test_last_healthy_reading_grace_and_expiry(tmp_path, failed):
    async def run():
        store, runtime, clock, notices = await rig(tmp_path)
        try:
            await ceiling(runtime)
            await reading(runtime, clock)
            clock[0] += 7200
            if failed:
                await reading(runtime, clock, collection='failed', error_code='collection_failed')
            assert (await pool(runtime))['eligibility']['eligible']
            clock[0] += 1
            assert (await pool(runtime))['eligibility']['code'] == 'account_pool_meter_unavailable'
            await runtime.cap_monitor.tick(); await runtime.cap_monitor.tick()
            problem = kinds(notices, 'account_pool_meter_problem')
            assert len(problem) == 1
            assert '7201' in problem[0]['body'] and 'pool-primary' in problem[0]['body']
            if failed: assert 'collection_failed' in problem[0]['body']
            await reading(runtime, clock)
            await runtime.cap_monitor.tick(); await runtime.cap_monitor.tick()
            assert len(kinds(notices, 'account_pool_meter_recovered')) == 1
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('case', ['expired', 'conflict', 'uncapped', 'missing', 'custom_grace'])
def test_missing_reading_boundaries(tmp_path, case):
    async def run():
        store, runtime, clock, notices = await rig(tmp_path)
        try:
            await ceiling(runtime, **({'stop_pct': None, 'window': None} if case == 'uncapped' else {}))
            if case not in {'missing', 'uncapped'}:
                await reading(runtime, clock)
                if case == 'expired':
                    await reading(runtime, clock, windows={'weekly': {'pct': 18, 'reset_at': NOW+10, 'reset_id': 'period'}})
                    clock[0] = NOW+11
                if case == 'conflict': await reading(runtime, clock, provider_id='conflicting-identity')
                if case == 'custom_grace':
                    runtime.policy['usage_reading_grace_seconds'] = 10
                    clock[0] += 11
            result = (await pool(runtime))['eligibility']
            assert result['eligible'] is (case == 'uncapped')
            if case != 'uncapped': assert result['code'] == 'account_pool_meter_unavailable'
            await runtime.cap_monitor.tick(); await runtime.cap_monitor.tick()
            assert len(kinds(notices, 'account_pool_meter_problem')) == (0 if case == 'uncapped' else 1)
        finally: store.stop()
    asyncio.run(run())


def test_warning_rearms_only_below_margin_or_revision(tmp_path):
    async def run():
        store, runtime, clock, notices = await rig(tmp_path)
        try:
            await ceiling(runtime)
            await reading(runtime, clock, 18)
            await runtime.cap_monitor.tick()
            assert not kinds(notices, 'account_pool_cap_warning')
            await reading(runtime, clock, 19)
            await runtime.cap_monitor.tick(); await runtime.cap_monitor.tick()
            assert len(kinds(notices, 'account_pool_cap_warning')) == 1
            other = replace(binding(host='host-b'), pool_id=binding().pool_id)
            await runtime.run(lambda a: a.register(other))
            for index in range(4):
                clock[0] += 1
                account = other if index % 2 else binding()
                value = observation(account, clock[0])
                value['windows']['weekly'].update(pct=20, reset_id=f'source-{index}', reset_at=NOW + 20000)
                await runtime.record_observation(account, value)
                await runtime.cap_monitor.tick()
            assert len(kinds(notices, 'account_pool_cap_warning')) == 1
            clock[0] += 1
            await reading(runtime, clock, 18)
            await runtime.cap_monitor.tick()
            clock[0] += 1
            await reading(runtime, clock, 20)
            await runtime.cap_monitor.tick()
            assert len(kinds(notices, 'account_pool_cap_warning')) == 2
            await ceiling(runtime, revision=2)
            await runtime.cap_monitor.tick()
            warnings = kinds(notices, 'account_pool_cap_warning')
            assert len(warnings) == 3
            assert all(str(x) in warnings[-1]['body'] for x in ['pool-primary', 20, 21, NOW + 20000])
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('case', ['blocked', 'uncapped', 'margin', 'failed'])
def test_warning_suppression_and_shared_reading(tmp_path, case):
    async def run():
        store, runtime, clock, notices = await rig(tmp_path)
        try:
            changes = {'blocked': True} if case == 'blocked' else {'stop_pct': None, 'window': None} if case == 'uncapped' else {}
            await ceiling(runtime, **changes)
            if case == 'margin': runtime.policy['usage_cap_warning_margin'] = 1
            await reading(runtime, clock, 19)
            if case == 'failed':
                clock[0] += 7000
                await reading(runtime, clock, collection='failed')
            await runtime.cap_monitor.tick()
            assert len(kinds(notices, 'account_pool_cap_warning')) == (1 if case == 'failed' else 0)
            assert 'legacy_cap' not in await pool(runtime)
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('case', ['restore', 'ordinary', 'spoofed', 'actor', 'key', 'generation', 'payload', 'ended', 'extra', 'normalized'])
def test_only_authenticated_frozen_restore_spawn_bypasses_block(tmp_path, case):
    import json
    from assistant_authority_restore import DDL, SERVICE_ACTOR
    async def run():
        store, runtime, clock, notices = await rig(tmp_path)
        try:
            await ceiling(runtime, blocked=True)
            payload = {'type': 'spawn', 'request_id': 'authority-restore:1:1',
                       'idempotency_key': 'authority-restore:1:1',
                       '_auth_context': {'service_authenticated': True, 'service_actor': SERVICE_ACTOR},
                       '_session_generation': 'restored-generation', 'host': 'host-a', 'provider': 'codex',
                       'account': 'primary', 'initial_prompt': 'Restore the advisor',
                       'parent_stream_id': 'host-a:front-desk'}
            def save(conn):
                conn.execute(DDL)
                conn.execute("INSERT INTO v2_authority_restore_episode "
                             "(stream_id,generation,state,trigger,spawn_key,attempt_generation,payload,created_at,updated_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?)", ('host-a:advisor','old-generation',
                             'restored' if case == 'ended' else 'spawning', 'seat_ended',
                             payload['idempotency_key'], payload['_session_generation'], json.dumps(payload), NOW, NOW))
                conn.commit()
            await store.submit(save)
            message = json.loads(json.dumps(payload))
            if case == 'ordinary': message = {}
            if case == 'spoofed': message['_auth_context']['service_authenticated'] = False
            if case == 'actor': message['_auth_context']['service_actor'] = 'daemon:other'
            if case == 'key': message['idempotency_key'] = 'authority-restore:2:1'
            if case == 'generation': message['_session_generation'] = 'other-generation'
            if case == 'payload': message['initial_prompt'] = 'Unrelated work'
            if case == 'extra': message['command']='unrelated-command'
            if case == 'normalized':
                message.update(visibility='default',objective_source='provided',_defer_launch_token_stage=True,_native_initial_prompt_path='/fixture/prompt')
            if case in {'restore','normalized'}:
                await runtime._pool_launch(binding(), message)
                from provider_cli_fixtures import seed_pin
                from test_provider_cli_pin import artifact_executor
                await seed_pin(runtime)
                await artifact_executor(runtime)
                await runtime.admit_launch({'stream_id':'host-a:restored','session_generation':'restored-generation',
                                            'account_binding':binding().public()},message=message)
            else:
                with pytest.raises(AccountError, match='account_pool_blocked'):
                    await runtime._pool_launch(binding(), message)
        finally: store.stop()
    asyncio.run(run())


def test_cap_monitor_uses_retained_reading_and_stops_at_grace(tmp_path):
    async def run():
        store, runtime, clock, notices = await rig(tmp_path)
        try:
            await ceiling(runtime)
            await reading(runtime, clock, 21)
            row=await runtime.sessions.open('host-a','seat-a',provider='codex',session_generation='seat-generation',
                account_binding=binding().public(),pane_status='pane_alive')
            clock[0]+=7000
            await reading(runtime, clock, collection='failed')
            runtime.sessions.apply_live(row['stream_id'],online=True,working=True,capture_generation=row['session_generation'],
                capture_liveness='idle',capture_observed_at=clock[0])
            await runtime.cap_monitor.tick()
            reminders=kinds(notices,'account_cap_reminder')
            assert len(reminders)==1
            assert await runtime.cap_monitor.eligible(reminders[0]['metadata'])
            clock[0]+=201
            runtime.sessions.apply_live(row['stream_id'],capture_observed_at=clock[0])
            assert not await runtime.cap_monitor.eligible(reminders[0]['metadata'])
            await runtime.cap_monitor.tick()
            assert len(kinds(notices,'account_pool_meter_problem'))==1
        finally: store.stop()
    asyncio.run(run())


def test_policy_loader_discards_obsolete_cap_key(tmp_path):
    import json
    from account_runtime import load_policy
    path=tmp_path/'policy.json'
    path.write_text(json.dumps({'enabled':False,'account_caps':{'codex':{'pool-a':12}}}))
    path.chmod(0o600)
    assert load_policy(path)=={'enabled':False}


def test_failed_first_sample_is_not_a_healthy_reading(tmp_path):
    async def run():
        store,runtime,clock,notices=await rig(tmp_path)
        try:
            await ceiling(runtime)
            await reading(runtime,clock,19,collection='failed')
            assert (await pool(runtime))['eligibility']['code']=='account_pool_meter_unavailable'
            await runtime.cap_monitor.tick()
            assert not kinds(notices,'account_pool_cap_warning')
        finally: store.stop()
    asyncio.run(run())


def test_pending_warning_survives_reading_gap_and_retargets(tmp_path):
    async def run():
        store,runtime,clock,notices=await rig(tmp_path)
        try:
            await ceiling(runtime)
            await reading(runtime,clock,19)
            await runtime.cap_monitor.tick()
            warning=kinds(notices,'account_pool_cap_warning')[0]
            clock[0]+=7201
            await runtime.cap_monitor.tick()
            assert await runtime.cap_monitor.guard(warning) is not None
            await reading(runtime,clock,19)
            await runtime.cap_monitor.tick()
            assert await runtime.cap_monitor.guard(warning) is None
            async def replacement(): return ('host-a:front-desk','replacement-generation')
            runtime.primary_pair=replacement
            await runtime.cap_monitor.tick()
            warnings=kinds(notices,'account_pool_cap_warning')
            assert len(warnings)==2
            assert warnings[-1]['metadata']['recipient_generation']=='replacement-generation'
            assert warnings[-1]['tell_id']!=warning['tell_id']
        finally: store.stop()
    asyncio.run(run())


def test_pool_notice_guard_accepts_bound_front_desk(tmp_path):
    from account_pool_control import NOTICE_TOKEN
    async def run():
        store,runtime,clock,notices=await rig(tmp_path)
        try:
            await ceiling(runtime)
            await reading(runtime,clock,19)
            row=await runtime.sessions.open('host-a','bound-front-desk',session_generation='bound-generation',
                account_binding=binding().public())
            async def primary(): return (row['stream_id'],row['session_generation'])
            runtime.primary_pair=primary
            await runtime.cap_monitor.tick()
            warning=kinds(notices,'account_pool_cap_warning')[0]
            assert await runtime.cap_input(row,{'_pool_notice_token':NOTICE_TOKEN,
                '_pool_notice_kind':warning['kind'],'_pool_notice_metadata':warning['metadata'],
                'tell_id':warning['tell_id']})=='pool_control_notice'
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('change',[{'blocked':True,'revision':2},{'stop_pct':None,'window':None,'revision':2},
                                   {'revision':2},{'below':True}])
def test_warning_dispatch_rechecks_control_before_next_tick(tmp_path,change):
    async def run():
        store,runtime,clock,notices=await rig(tmp_path)
        try:
            await ceiling(runtime)
            await reading(runtime,clock,19)
            await runtime.cap_monitor.tick()
            warning=kinds(notices,'account_pool_cap_warning')[0]
            if change.get('below'): await reading(runtime,clock,18)
            else: await ceiling(runtime,**change)
            decision=await runtime.cap_monitor.guard(warning)
            assert decision and decision.action==('defer' if change.get('below') else 'terminal')
            if change.get('below'):
                await reading(runtime,clock,19)
                await runtime.cap_monitor.tick()
                assert await runtime.cap_monitor.guard(warning) is None
                assert len(kinds(notices,'account_pool_cap_warning'))==1
        finally: store.stop()
    asyncio.run(run())
