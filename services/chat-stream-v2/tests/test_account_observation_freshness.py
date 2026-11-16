"""Fake native cache/clock reproductions; never touch enrolled accounts."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

import provider_account_native as native
import provider_account_host as host_module
from provider_account_host import NativeHost
from provider_accounts import AccountError
from account_executor import AccountExecutor
from account_runtime import AccountRuntime
from sessions import Sessions
from store import Store
from test_account_rotation import binding, observation, policy, NOW


class Clock:
    def __init__(self): self.value = 1000.; self.elapsed = 0.; self.sleeps = []; self.on_sleep = None
    def wall(self): return self.value
    def monotonic(self): return self.elapsed
    def sleep(self, seconds):
        assert 0 < seconds <= 61
        self.sleeps.append(seconds)
        self.value += seconds; self.elapsed += seconds
        if self.on_sleep: self.on_sleep()


def fixture_native(tmp_path, monkeypatch):
    clock = Clock(); calls = []
    private = tmp_path/'private'; private.mkdir(mode=0o700)
    cell = {'enabled':True,'expected_provider_id':'fixture-user','expected_email':'fixture@example.test',
            'profile_ref':'fixture-profile','executable':'/bin/echo','version':'fixture',
            'executable_sha256':hashlib.sha256(Path('/bin/echo').read_bytes()).hexdigest(),
            'platform':'wsl','model':'fixture-model','effort':'high','cwd':str(tmp_path)}
    config = {'host':'host-a','registry':str(private/'registry.json'),'root':str(private/'homes'),
              'cells':{'claude:primary':cell},'claude_usage_command':['fixture-usage']}
    def run(argv, **kw):
        if argv[-1] == '--version': return SimpleNamespace(returncode=0,stdout='fixture')
        assert argv[-3:] == ['auth','status','--json']
        calls.append(('status',clock.wall(),kw.get('timeout')))
        return SimpleNamespace(returncode=0,stdout=json.dumps({'loggedIn':True,'authMethod':'claude.ai',
            'apiProvider':'firstParty','configDirectory':account.home,'orgId':'fixture-pool',
            'email':'fixture@example.test','subscriptionType':'max'}))
    host = NativeHost(config,run=run)
    draft = host.registry.prepare(provider='claude',alias='primary',host='host-a',generation='login-a',
        profile_ref='fixture-profile',executable='/bin/echo',version='fixture',platform='wsl')
    credentials = Path(draft.home)/'.credentials.json'; credentials.write_text('fake-only'); credentials.chmod(0o600)
    account = host.registry.activate(draft,{'provider_id':'fixture-user','pool_id':'fixture-pool',
        'auth_mode':'subscription','store':'file','minimal_turn':True,'independent_login':True},expected_provider_id='fixture-user')
    cache_path = Path(account.home)/'.claude.json'
    def write(stamp=999., cached_user='fixture-user'):
        cache_path.write_text(json.dumps({'oauthAccount':{'accountUuid':'fixture-user','organizationUuid':'fixture-pool','emailAddress':'fixture@example.test'},
            'cachedUsageUtilization':{'accountUuid':cached_user,'fetchedAtMs':stamp*1000 if isinstance(stamp,(float,int)) else stamp,
             'utilization':{'five_hour':{'utilization':12,'resets_at':'2027-01-01T00:00:00Z'}}}}))
    write()
    def collect(selected, env):
        assert selected == account and env['CLAUDE_CONFIG_DIR'] == account.home
        calls.append(('collect',clock.wall()))
        stamp=json.loads(cache_path.read_text())['cachedUsageUtilization']['fetchedAtMs']
        if clock.wall()-stamp/1000 >= 60: write(clock.wall())
        clock.value += .1
    original = native.claude_observation
    def observe(*args, **kwargs):
        kwargs['now']=clock.wall
        return original(*args,**kwargs)
    monkeypatch.setattr(native,'time',SimpleNamespace(time=clock.wall,monotonic=clock.monotonic,sleep=clock.sleep))
    monkeypatch.setattr(host_module,'claude_observation',observe)
    host._claude_usage=collect
    return SimpleNamespace(host=host,account=account,clock=clock,calls=calls,write=write,path=cache_path,run=run,collect=collect,observe=observe)


def test_real_host_probe_waits_for_throttled_cache_and_collects_once(tmp_path,monkeypatch):
    f=fixture_native(tmp_path,monkeypatch)
    def waiting(): assert not any(x[0]=='collect' for x in f.calls)
    f.clock.on_sleep=waiting
    result=f.host.handle('observe',provider='claude',alias='primary',expected_generation='login-a',operation_id='probe')
    assert result['outcome']=='ok' and result['observation']['observed_at']>=1000
    assert len([x for x in f.calls if x[0]=='collect'])==1
    assert 59 <= sum(f.clock.sleeps) <= 61
    assert all(x[2]==10 for x in f.calls if x[0]=='status')


@pytest.mark.parametrize('stamp',[999.,None,'bad',float('nan'),float('inf'),1002.])
def test_stale_or_invalid_cache_is_not_an_identity_error(tmp_path,monkeypatch,stamp):
    f=fixture_native(tmp_path,monkeypatch);f.write(stamp)
    with pytest.raises(AccountError,match='^provider_cache_stale$'):
        f.observe(f.account,collect_usage=lambda *_:None,run=f.run)


@pytest.mark.parametrize('cached_user',[None,'',123])
def test_missing_cached_identity_is_collection_unavailable(tmp_path,monkeypatch,cached_user):
    f=fixture_native(tmp_path,monkeypatch);f.write(1000.,cached_user)
    with pytest.raises(AccountError,match='^provider_cache_stale$'):
        f.observe(f.account,collect_usage=lambda *_:None,run=f.run)


def test_actual_cached_account_mismatch_still_refuses_identity(tmp_path,monkeypatch):
    f=fixture_native(tmp_path,monkeypatch);f.write(1000.,'another-fixture-user')
    with pytest.raises(AccountError,match='provider_identity_or_cache_unverified'):
        f.observe(f.account,collect_usage=lambda *_:None,run=f.run)


@pytest.mark.parametrize('prior',['healthy','auth_unhealthy','identity_conflict'])
def test_actual_stale_observer_preserves_existing_daemon_health(tmp_path,monkeypatch,prior):
    f=fixture_native(tmp_path,monkeypatch)
    async def journey():
        store=Store(':memory:');store.start()
        runtime=AccountRuntime(store,Sessions(store),policy=policy(),clock=lambda:NOW)
        async def execute(account,action,*args):
            try: f.observe(f.account,collect_usage=lambda *_:None,run=f.run)
            except AccountError as exc:return {'outcome':str(exc)}
            raise AssertionError('stale fixture unexpectedly accepted')
        executor=AccountExecutor(runtime,None,execute=execute)
        try:
            await runtime.run(lambda a:a.register(f.account))
            if prior!='healthy':await runtime.record_failure(f.account,'auth' if prior=='auth_unhealthy' else 'identity')
            result=await executor.observe(f.account)
            health=await runtime.run(lambda a:a.readback(now=NOW)['accounts'][0]['health'])
            assert health==prior
            assert result['outcome']=='provider_cache_stale'
            result=await runtime.run(lambda a:a.readback(now=NOW)['accounts'][0])
            assert result['observation']['collection']=='failed'
            if prior=='healthy': assert result['last_failure'] is None
            else: assert result['last_failure']['kind']==('auth' if prior=='auth_unhealthy' else 'identity')
        finally:store.stop()
    asyncio.run(journey())


def test_host_stale_outcome_is_allowlisted_and_observe_budget_is_200():
    async def journey():
        seen=[]
        class Hosts:
            async def run_command(self,*args,**kw):seen.append(kw['timeout']);return 1,json.dumps({'outcome':'provider_cache_stale'})
        runtime=SimpleNamespace(policy={'executors':{'host-a':{'python':'/fixture/python','script':'/fixture/host.py'}}})
        executor=AccountExecutor(runtime,Hosts())
        from provider_cli_fixtures import release_pair
        result=await executor._execute(binding(),'observe','fixture-op',pin=release_pair())
        assert result['outcome']=='provider_cache_stale' and seen==[200]
    asyncio.run(journey())


def test_host_alarm_observe_180_others_unchanged(tmp_path,monkeypatch):
    private=tmp_path/'.config/pentacle';private.mkdir(mode=0o700,parents=True)
    # main reads its existing fixed host-owned config path.
    config=tmp_path/'.config/pentacle/provider-account-host.json';config.write_text('{}');config.chmod(0o600)
    monkeypatch.setattr(host_module.Path,'home',lambda:tmp_path)
    alarms=[];monkeypatch.setattr(host_module.signal,'alarm',alarms.append);monkeypatch.setattr(host_module.signal,'signal',lambda *_:None)
    def native_host(_, *, operator_browser=None):
        assert operator_browser is None  # ordinary actions retain the existing browser path
        return SimpleNamespace(handle=lambda *a,**k:{'outcome':'ok'})
    monkeypatch.setattr(host_module,'NativeHost',native_host)
    for action,expected in [('observe',180),('status',90),('enroll',300)]:
        alarms.clear();assert host_module.main([action,'--provider','claude','--account','primary','--login-generation','login-a','--operation-id','fixture'])==0
        assert alarms==[expected,0]


def test_usage_timeout_reaps_owned_tmux_via_adapter_finally(tmp_path,monkeypatch):
    f=fixture_native(tmp_path,monkeypatch);socket='rotation-fake-'+uuid.uuid4().hex;done=tmp_path/'cleaned'
    script=tmp_path/'fake_usage.py';script.write_text('import subprocess,time,pathlib\n'+
        'subprocess.run(["tmux","-L",'+repr(socket)+',"new-session","-d","-s","probe","sleep 60"],check=True)\n'+
        'try:time.sleep(30)\nfinally:\n subprocess.run(["tmux","-L",'+repr(socket)+',"kill-server"])\n pathlib.Path('+repr(str(done))+').write_text("cleaned")\n')
    f.host.config['claude_usage_command']=[sys.executable,str(script)]
    def timeout_run(argv,**kwargs):
        assert kwargs.pop('timeout')==70
        return native.native_run(argv,timeout=1,**kwargs)
    f.host.run=timeout_run
    try:
        with pytest.raises(AccountError):NativeHost._claude_usage(f.host,f.account,{'PATH':__import__('os').environ['PATH']})
        assert done.exists(),'adapter finally was bypassed by unconditional SIGKILL'
        assert subprocess.run(['tmux','-L',socket,'has-session'],capture_output=True).returncode!=0
    finally:subprocess.run(['tmux','-L',socket,'kill-server'],capture_output=True)


def test_probe_cli_budget_and_explicit_shorter_deadline(monkeypatch):
    from agent_orch import cli,config,stream_id,wsclient
    monkeypatch.setattr(config,'load_config',lambda:object())
    monkeypatch.setattr(stream_id,'discover_leader_stream_id_short',lambda _:'host-a:front-desk')
    seen=[]
    async def rpc(*args,**kwargs):seen.append(kwargs['timeout']);return {'type':'account.ok'}
    monkeypatch.setattr(wsclient,'account_once',rpc)
    for args in (['probe'],['probe','--timeout','11'],['status']):
        ns=cli.build_parser().parse_args(['account',*args]);assert ns.func(ns)==0
    assert seen==[210,11,30]


def test_external_same_identity_fetch_after_host_fence_is_eligible(tmp_path,monkeypatch):
    f=fixture_native(tmp_path,monkeypatch)
    f.clock.on_sleep=lambda:f.write(f.clock.wall()-.1)
    result=f.host.handle('observe',provider='claude',alias='primary',expected_generation='login-a',operation_id='external')
    assert result['outcome']=='ok' and result['observation']['observed_at']>=1000
    assert 0 < sum(f.clock.sleeps) <= 61
    assert len([x for x in f.calls if x[0]=='collect'])==1


def test_wait_cancellation_never_calls_usage_and_releases_registry_lock(tmp_path,monkeypatch):
    f=fixture_native(tmp_path,monkeypatch)
    def cancelled(_):raise AccountError('provider_timeout')
    native.time.sleep=cancelled
    with pytest.raises(AccountError,match='^provider_timeout$'):
        f.host.handle('observe',provider='claude',alias='primary',expected_generation='login-a',operation_id='cancel')
    assert not any(x[0]=='collect' for x in f.calls)
    assert f.host.registry.active('claude','primary','host-a')==f.account


def test_wait_has_monotonic_limit_even_if_wall_clock_stalls(tmp_path,monkeypatch):
    f=fixture_native(tmp_path,monkeypatch)
    def stalled(seconds):
        f.clock.elapsed+=seconds;f.clock.sleeps.append(seconds)
    native.time.sleep=stalled
    with pytest.raises(AccountError,match='^provider_cache_stale$'):
        f.host.handle('observe',provider='claude',alias='primary',expected_generation='login-a',operation_id='stalled')
    assert sum(f.clock.sleeps)<=61 and not any(x[0]=='collect' for x in f.calls)


def test_host_clock_reversal_is_collection_failure(tmp_path,monkeypatch):
    f=fixture_native(tmp_path,monkeypatch);f.write(1000.)
    def collect(*_):f.clock.value=999.5
    with pytest.raises(AccountError,match='^provider_cache_stale$'):
        f.observe(f.account,collect_usage=collect,run=f.run)


def test_daemon_timeout_never_becomes_identity_failure():
    async def journey():
        class Hosts:
            async def run_command(self,*args,**kwargs):raise TimeoutError()
        runtime=SimpleNamespace(policy={'executors':{'host-a':{'python':'/fixture/python','script':'/fixture/host.py'}}})
        with pytest.raises(AccountError,match='^provider_timeout$'):
            from provider_cli_fixtures import release_pair
            await AccountExecutor(runtime,Hosts())._execute(binding(),'observe','timeout',pin=release_pair())
    asyncio.run(journey())


def test_existing_host_slot_serializes_cells_other_host_progresses_after_caller_timeout():
    async def journey():
        store=Store(':memory:');store.start();runtime=AccountRuntime(store,Sessions(store),policy=policy(),clock=lambda:NOW)
        first=binding();peer=binding('spare');other=binding(host='host-b');entered=asyncio.Event();release=asyncio.Event();seen=[]
        async def execute(account,*_):
            seen.append(account.key)
            if account.key==first.key:entered.set();await release.wait()
            return {'outcome':'ok','binding_key':account.key,'observation':observation(account)}
        executor=AccountExecutor(runtime,None,execute=execute)
        tasks=[]
        try:
            for x in (first,peer,other):await runtime.run(lambda a,x=x:a.register(x))
            task=asyncio.create_task(executor.observe(first));tasks.append(task);await entered.wait()
            queued=asyncio.create_task(executor.observe(peer));tasks.append(queued)
            result=await executor.observe(other);assert result['outcome']=='ok' and peer.key not in seen
            with pytest.raises(TimeoutError):await asyncio.wait_for(asyncio.shield(task),.01)
            assert not task.done();release.set()
            assert all(x['outcome']=='ok' for x in await asyncio.wait_for(asyncio.gather(*tasks),1))
            assert seen.index(first.key)<seen.index(peer.key)
            assert all(r['health']=='healthy' for r in await runtime.run(lambda a:a.readback(now=NOW)['accounts']))
        finally:
            release.set()
            for t in tasks:
                if not t.done():t.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            store.stop()
    asyncio.run(journey())


def test_missing_cache_does_not_wait_before_one_repair_collection(tmp_path,monkeypatch):
    f=fixture_native(tmp_path,monkeypatch);f.write(999.,None)
    calls=[]
    def repair(*_):calls.append('collect');f.write(f.clock.wall())
    f.host._claude_usage=repair
    result=f.host.handle('observe',provider='claude',alias='primary',expected_generation='login-a',operation_id='missing')
    assert result['outcome']=='ok' and calls==['collect'] and f.clock.sleeps==[]
