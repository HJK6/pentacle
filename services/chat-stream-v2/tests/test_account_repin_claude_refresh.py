"""Exercise real Claude cache throttling through the supported host repin path."""
import asyncio
from functools import partial
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from provider_accounts import AccountError
from sessions import VerbError
from test_account_repin import host_journey
from test_account_rotation import NOW


async def throttled_journey(tmp_path, monkeypatch, *, stale_phase='before', identity_fault=None,
                            never_refresh=False, expire_wait=False):
    import provider_account_native as native
    store, runtime, host, old, request, calls, commands, logins, cell, pin = await host_journey(
        tmp_path, monkeypatch, provider='claude')
    wall = [float(NOW)]
    waits, collections = [], []
    identity = {'provider_id': 'native-repin', 'pool_id': 'pool-repin', 'auth_mode': 'subscription'}
    login = {'accountUuid': identity['provider_id'], 'organizationUuid': identity['pool_id']}
    cache = {'accountUuid': identity['provider_id'],
             'fetchedAtMs': (wall[0] - (30 if stale_phase == 'before' else 61)) * 1000,
             'utilization': {'five_hour': {'utilization': 1,
                 'resets_at': datetime.fromtimestamp(NOW + 18000, timezone.utc).isoformat()}}}
    def clock(): return wall[0]
    def sleep(seconds):
        waits.append(seconds)
        if expire_wait: raise AccountError('provider_timeout')
        assert 0 < seconds <= 61, 'refresh must keep the existing finite wait budget'
        wall[0] += seconds
    def collect(binding, environment):
        collections.append(clock())
        if not never_refresh and clock() - cache['fetchedAtMs'] / 1000 >= 60:
            cache['fetchedAtMs'] = clock() * 1000
        if identity_fault == 'before': login['accountUuid'] = 'different-account'
    original_run = host.run
    def run(argv, **kwargs):
        value = original_run(argv, **kwargs)
        if 'Reply exactly OK' in argv:
            wall[0] += 1
            if identity_fault == 'after': login['organizationUuid'] = 'different-pool'
        return value
    monkeypatch.setattr(native, 'time', SimpleNamespace(monotonic=clock, sleep=sleep))
    monkeypatch.setattr(native, 'claude_identity', lambda *args, **kwargs: dict(identity))
    monkeypatch.setattr(native, '_claude_cache', lambda binding: (cache, login))
    def observe(binding, *, wait_for_refresh=False):
        return native.claude_observation(binding, collect_usage=collect, run=run,
                                         now=clock, wait_for_refresh=wait_for_refresh)
    # These are the real default constructor's two observation policies. Only
    # external identity/collector/clock IO is replaced; cache validation is real.
    host.observation = observe
    host._probe_observation = partial(observe, wait_for_refresh=True)
    host.run = run
    runtime.clock = clock
    return store, runtime, host, old, request, commands, waits, collections


@pytest.mark.parametrize('stale_phase', ['before', 'after'])
def test_claude_repin_waits_for_native_cache_before_and_after_its_one_turn(tmp_path, monkeypatch, stale_phase):
    async def run():
        store, runtime, host, old, request, commands, waits, collections = await throttled_journey(
            tmp_path, monkeypatch, stale_phase=stale_phase)
        try:
            result = await runtime.operation(request)
            assert result['result']['outcome'] == 'repinned'
            selected = host.registry.active('claude', old.alias, old.host)
            assert selected.home == old.home and selected.generation == 'successor'
            assert len(collections) == 2 and waits
            assert len([c for c in commands if 'Reply exactly OK' in c]) == 1
            assert all(seconds <= 61 for seconds in waits) and sum(waits) <= 122
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['before', 'after', 'never-refresh', 'timeout'])
def test_refresh_never_activates_an_unverified_or_timed_out_identity(tmp_path, monkeypatch, fault):
    async def run():
        store, runtime, host, old, request, commands, waits, collections = await throttled_journey(
            tmp_path, monkeypatch, identity_fault=fault if fault in {'before', 'after'} else None,
            never_refresh=fault == 'never-refresh', expire_wait=fault == 'timeout')
        try:
            with pytest.raises(VerbError): await runtime.operation(request)
            assert host.registry.active('claude', old.alias, old.host) == old
            assert host.registry._load()['generations'].get('host-a:claude:repin:successor') is None
            assert len([c for c in commands if 'Reply exactly OK' in c]) <= 1
        finally: store.stop()
    asyncio.run(run())
