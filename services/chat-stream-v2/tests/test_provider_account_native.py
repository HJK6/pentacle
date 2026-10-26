from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from provider_accounts import AccountError, Binding, Registry
from provider_account_native import codex_observation, claude_observation, enroll


def test_operator_callback_is_not_login_success_until_matching_native_completion(tmp_path):
    from provider_account_host import OperatorBrowser
    from provider_account_native import native_login
    import os
    reader, writer = os.pipe(); callback = OperatorBrowser(writer)
    closed, reads = [], []
    class RPC:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): closed.append(True)
        def call(self, method, params):
            return {"type":"chatgpt", "loginId":"expected", "authUrl":"https://auth.openai.com/oauth/authorize?state=fixture-only"}
        def read(self, deadline):
            reads.append(True)
            return {"method":"account/login/completed", "params":{"loginId":"wrong" if len(reads)==1 else "expected", "success":True}}
    try:
        assert native_login(binding(tmp_path), callback, rpc_factory=RPC) == "ok"
        assert len(reads) == 2 and closed == [True]
        assert os.read(reader, 8193).count(b"\n") == 1
    finally: callback.close(); os.close(reader)


def binding(tmp_path, provider="codex"):
    return Binding(provider, "primary", "host-a", str(tmp_path), "login-a", "browser-a", "/bin/echo", "fixture-1", "macos")


def test_codex_quota_identity_comes_from_same_native_rpc(tmp_path):
    calls = []
    class RPC:
        def __init__(self, account): assert account.home == str(tmp_path)
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def call(self, method, params=None):
            calls.append(method)
            if method == "account/read": return {"account": {"type": "chatgpt", "planType": "pro"}}
            return {"accountId": "identity-a", "rateLimits": {
                "primary": {"usedPercent": 15, "resetsAt": 2000, "windowDurationMins": 300},
                "secondary": {"usedPercent": 40, "resetsAt": 3000, "windowDurationMins": 10080}}}
    row = codex_observation(binding(tmp_path), rpc_factory=RPC, now=lambda: 1000)
    assert row["provider_id"] == row["pool_id"] == "identity-a"
    assert row["observed_at"] == 1000 and set(row["windows"]) == {"codex:300", "codex:10080"}
    assert calls == ["account/read", "account/rateLimits/read", "account/read"]


@pytest.mark.parametrize("case", ["api", "unknown", "changed"])
def test_codex_unverified_or_changed_identity_never_returns_quota(tmp_path, case):
    class RPC:
        def __init__(self, _): self.n = 0
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def call(self, method, params=None):
            self.n += 1
            if method == "account/read":
                return {"account": {"type": "apiKey" if case == "api" else "chatgpt", "planType": "pro",
                                    "email": "changed" if case == "changed" and self.n == 3 else "fixture"}}
            return {"accountId": None if case == "unknown" else "identity-a", "rateLimits": {}}
    with pytest.raises(AccountError): codex_observation(binding(tmp_path), rpc_factory=RPC)


@pytest.mark.parametrize("case", ["plain", "metadata", "null_reset", "numeric_reset", "invalid_reset"])
def test_claude_all_windows_from_one_fresh_named_home_cache(tmp_path, case):
    account = binding(tmp_path, "claude")
    def run(argv, **kwargs):
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai",
            "apiProvider": "firstParty", "configDirectory": str(tmp_path), "orgId": "pool-a", "email": "person@example.test", "subscriptionType": "max"}))
    def collect(selected, env):
        assert selected == account and env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
        usage = {key: {"utilization": 12, "resets_at": "2027-01-01T00:00:00Z"}
                 for key in ["five_hour", "seven_day", "seven_day_model"]}
        if case == "metadata":
            # Native /usage also caches billing and breakdown metadata. These
            # are fake values, not timed quota windows or live account data.
            usage.update({
                "seven_day_opus": None,
                "extra_usage": {"is_enabled": False, "utilization": None, "monthly_limit": None},
                "spend": {"used": 0, "limit": None, "enabled": False},
                "seven_day_breakdown": {"as_of": "2026-01-01T00:00:00Z", "rows": []},
                "limits": [], "member_dashboard_available": False,
            })
        elif case != "plain":
            usage["five_hour"]["resets_at"] = {
                "null_reset": None, "numeric_reset": 1234, "invalid_reset": "not-a-timestamp",
            }[case]
        (tmp_path / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"accountUuid": "login-user", "organizationUuid": "pool-a", "emailAddress": "person@example.test"},
            "cachedUsageUtilization": {"accountUuid": "login-user", "fetchedAtMs": 1000500,
                "utilization": usage}}))
    # Native login account cache exists before quota collection.
    collect(account, {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    ticks = iter([1000, 1001])
    if case not in {"plain", "metadata"}:
        with pytest.raises(AccountError, match="provider_windows_invalid"):
            claude_observation(account, collect_usage=collect, run=run, now=lambda: next(ticks))
        return
    row = claude_observation(account, collect_usage=collect, run=run, now=lambda: next(ticks))
    assert row["provider_id"] == "login-user" and row["pool_id"] == "pool-a" and row["observed_at"] == 1000.5
    assert set(row["windows"]) == {"five_hour", "seven_day", "seven_day_model"}


@pytest.mark.parametrize("outcome", ["cancelled", "locked_keychain", "human_reseed_required", "wrong_account", "ok"])
def test_native_enrollment_writes_only_final_generation(tmp_path, outcome):
    private = tmp_path / "private"; private.mkdir(mode=0o700)
    registry = Registry(private / "registry.json", private / "homes")
    draft = registry.prepare(provider="codex", alias="primary", host="host-a", generation="login-a",
        profile_ref="browser-a", executable="/bin/echo", version="fixture-1", platform="macos")
    calls = []
    def login(account, browser):
        calls.append(("login", account.home))
        assert account.home == draft.home
        if outcome == "ok":
            target = Path(account.home) / "auth.json"; target.write_text("fake-only"); target.chmod(0o600)
        return "ok" if outcome in {"ok", "wrong_account"} else outcome
    def identity(account):
        return {"provider_id": "foreign" if outcome == "wrong_account" else "id-a",
                "pool_id": "pool-a", "auth_mode": "subscription"}
    def run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return SimpleNamespace(returncode=0, stdout="fixture-1" if argv[-1] == "--version" else "OK")
    def attempt():
        return enroll(registry, draft, expected_provider_id="id-a", browser_authorize=lambda *_: None,
                      login=login, identity=identity, model="fixture-model", effort="high", cwd=str(tmp_path), run=run)
    if outcome == "ok": assert attempt() == registry.active("codex", "primary", "host-a")
    else:
        with pytest.raises(AccountError): attempt()
        assert registry.active("codex", "primary", "host-a") is None
        assert not any(isinstance(c[0], list) and "exec" in c[0] for c in calls)


def test_claude_identity_cannot_bind_another_user_in_the_same_organization(tmp_path):
    from provider_account_native import claude_identity
    (tmp_path/".claude.json").write_text(json.dumps({"oauthAccount": {
        "accountUuid":"foreign-user", "organizationUuid":"pool-a", "emailAddress":"foreign@example.test"}}))
    def run(*args,**kwargs):
        return SimpleNamespace(returncode=0,stdout=json.dumps({"loggedIn":True,"authMethod":"claude.ai",
            "apiProvider":"firstParty","configDirectory":str(tmp_path),"orgId":"pool-a",
            "email":"person@example.test","subscriptionType":"max"}))
    with pytest.raises(AccountError,match="subscription_identity_unverified"):
        claude_identity(binding(tmp_path,"claude"),run=run)


def test_claude_native_logged_out_is_an_auth_failure(tmp_path):
    from provider_account_native import claude_identity
    def run(*args,**kwargs): return SimpleNamespace(returncode=0,stdout=json.dumps({"loggedIn":False}))
    with pytest.raises(AccountError,match="provider_auth_failed"):
        claude_identity(binding(tmp_path,"claude"),run=run)


def test_first_enrollment_can_pin_native_uuid_after_verified_login_identity(tmp_path):
    private=tmp_path/"private"; private.mkdir(mode=0o700)
    registry=Registry(private/"registry.json",private/"homes")
    draft=registry.prepare(provider="codex",alias="primary",host="host-a",generation="first",
        profile_ref="google-account.one",executable="/bin/echo",version="fixture",platform="macos")
    def login(account,*args):
        file=Path(account.home)/"auth.json"; file.write_text("fake-only"); file.chmod(0o600); return "ok"
    def run(argv,**kwargs): return SimpleNamespace(returncode=0,stdout="fixture" if argv[-1]=="--version" else "OK")
    def identity(account): return {"provider_id":"new-native-id","pool_id":"pool-a","auth_mode":"subscription","email_verified":True}
    result=enroll(registry,draft,expected_provider_id=None,browser_authorize=lambda *a:None,
        login=login,identity=identity,model="fixture-model",effort="high",cwd=str(tmp_path),run=run)
    assert result.provider_id=="new-native-id"


def test_claude_native_login_uses_exit_and_identity_proof_instead_of_english_banner(tmp_path):
    import sys
    from provider_account_native import native_login
    fake=tmp_path/"fake-claude"
    fake.write_text("#!"+sys.executable+"\nimport sys\nprint('https://claude.ai/oauth/authorize?state=fixture-state',flush=True)\nprint('Paste code:',flush=True)\nsys.stdin.readline()\nprint('Complete.',flush=True)\n")
    fake.chmod(0o700)
    account=replace(binding(tmp_path,"claude"),executable=str(fake))
    assert native_login(account,lambda *_:{"outcome":"ok","code":"fake-code#fake-state"})=="ok"


def test_intended_codex_email_is_verified_before_returning_any_identity(tmp_path):
    calls=[]
    class RPC:
        def __init__(self,*args): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def call(self,method,params=None):
            calls.append(method)
            return {"account":{"type":"chatgpt","planType":"pro","email":"other@example.test"}}
    with pytest.raises(AccountError,match='subscription_identity_unverified'):
        codex_observation(binding(tmp_path),rpc_factory=RPC,expected_email='intended@example.test')
    assert calls==['account/read']


@pytest.mark.parametrize("permission",[True,False,None,"missing"])
def test_codex_permission_retains_unknown_through_daemon_readback(tmp_path,permission):
    import sqlite3
    from account_store import AccountStore
    class RPC:
        def __init__(self,*args): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def call(self,method,params=None):
            if method=="account/read": return {"account":{"type":"chatgpt","planType":"pro"}}
            result={"accountId":"native-a","rateLimits":{"primary":{
                "usedPercent":20,"resetsAt":2000,"windowDurationMins":300}}}
            if permission!="missing": result["ordinaryUsageAllowed"]=permission
            return result
    account=replace(binding(tmp_path),provider_id="native-a",pool_id="native-a",enabled=True,healthy=True)
    observed=codex_observation(account,rpc_factory=RPC,now=lambda:1000)
    expected=None if permission=="missing" else permission
    assert observed["ordinary_usage_allowed"] is expected
    conn=sqlite3.connect(":memory:"); conn.row_factory=sqlite3.Row
    ledger=AccountStore(conn,{"enabled":True})
    try:
        ledger.register(account); ledger.observe(account.key,observed,now=1000)
        assert ledger.readback(now=1000)["quota_pools"][0]["observation"]["ordinary_usage_allowed"] is expected
    finally: conn.close()


# The counterpart emits native-shaped frames; CodexRPC and its transport run unchanged.
@pytest.fixture
def native_rpc(tmp_path):
    import subprocess
    import sys
    from provider_account_native import CodexRPC
    processes = []
    script = r'''
import json, sys, time
scenario = json.loads(sys.argv[1])
reads = 0
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    if 'id' not in request:
        continue
    point = 'quota' if method == 'account/rateLimits/read' else 'second_read' if reads else 'first_read'
    if method == 'initialize':
        result = {'userAgent': 'fixture'}
    elif method == 'account/read':
        assert request['params'] == {'refreshToken': False}
        reads += 1
        if reads == 1:
            print(json.dumps({'method': 'remoteControl/status/changed', 'params': {
                'environmentId': None, 'installationId': None, 'serverName': None, 'status': 'disabled'}}), flush=True)
            for status in scenario.get('startup', []):
                print(json.dumps({'method': 'account/updated', 'params': status}), flush=True)
            if scenario.get('error') and scenario.get('fail_at', 'first_read') == point:
                print(json.dumps({'id': request['id'], 'error': {'message': 'rate_limit'}}), flush=True)
                continue
            if scenario.get('timeout') and scenario.get('fail_at', 'first_read') == point:
                time.sleep(10)
                continue
        if reads == 2 and scenario.get('notify_at') == 'second_read':
            for status in scenario.get('delayed', [{'authMode': 'chatgpt', 'planType': 'pro'}]):
                print(json.dumps({'method': 'account/updated', 'params': status}), flush=True)
            if scenario.get('error'):
                print(json.dumps({'id': request['id'], 'error': {'message': 'rate_limit'}}), flush=True)
                continue
            if scenario.get('timeout'):
                time.sleep(10)
                continue
        result = {'account': dict({'type': 'chatgpt', 'planType': 'pro', 'id': 'identity-a',
                                  'email': 'person@example.test'}, **(scenario.get('after', {}) if reads == 2 else scenario.get('first', {})))}
    elif method == 'account/rateLimits/read':
        if scenario.get('notify_at') == 'quota':
            for status in scenario.get('delayed', [{'authMode': 'chatgpt', 'planType': 'pro'}]):
                print(json.dumps({'method': 'account/updated', 'params': status}), flush=True)
            if scenario.get('error'):
                print(json.dumps({'id': request['id'], 'error': {'message': 'rate_limit'}}), flush=True)
                continue
            if scenario.get('timeout'):
                time.sleep(10)
                continue
        result = {'accountId': scenario.get('quota_identity', 'identity-a'), 'rateLimits': {'primary': {
            'usedPercent': 15, 'resetsAt': 2000, 'windowDurationMins': 300}}}
    else:
        result = {}
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
'''
    def factory(scenario):
        def create(account):
            def popen(argv, **kwargs):
                process = subprocess.Popen([sys.executable, '-u', '-c', script, json.dumps(scenario)], **kwargs)
                processes.append(process)
                return process
            return CodexRPC(account, timeout=2 if not scenario.get('timeout') else .2, popen=popen)
        return create
    yield factory
    assert processes and all(process.poll() is not None for process in processes)
    assert all(stream.closed for process in processes for stream in (process.stdin, process.stdout) if stream)


@pytest.mark.parametrize('startup', [[], [{'authMode': 'chatgpt', 'planType': 'pro'}],
    [{'authMode': 'chatgpt', 'planType': 'pro'}] * 2], ids=['native-0.153.4', 'native-0.160.0', 'repeated-status'])
def test_codex_initial_status_uses_authoritative_account_baseline(tmp_path, native_rpc, startup):
    row = codex_observation(binding(tmp_path), rpc_factory=native_rpc({'startup': startup}), now=lambda: 1000,
                            expected_email='person@example.test')
    assert row['provider_id'] == row['pool_id'] == 'identity-a'
    assert row['windows']['codex:300']['pct'] == 15
    assert row['email_verified'] is True


@pytest.mark.parametrize('status', [
    {'authMode': 'chatgpt', 'planType': 'pro', 'id': 'foreign'},
    {'authMode': 'chatgpt', 'planType': 'pro', 'email': 'other@example.test'},
    {'authMode': 'chatgpt', 'planType': 'pro', 'accountId': 'foreign'},
    {'authMode': 'chatgpt', 'planType': 'team'},
    {'authMode': 'apiKey', 'planType': 'pro'},
    {'authMode': 'chatgpt', 'planType': None},
    {'authMode': 'chatgpt', 'planType': ''},
    {'authMode': 'chatgpt', 'planType': 1},
    {'authMode': 'chatgpt'}, {}, None, [],
])
def test_codex_initial_identity_change_or_malformed_status_refuses(tmp_path, native_rpc, status):
    with pytest.raises(AccountError, match='provider_identity_changed'):
        codex_observation(binding(tmp_path), rpc_factory=native_rpc({'startup': [status]}))


@pytest.mark.parametrize('first', [{'type': 'apiKey'}, {'planType': 'team'}])
def test_codex_startup_status_must_match_first_account_reply(tmp_path, native_rpc, first):
    with pytest.raises(AccountError, match='provider_identity_changed'):
        codex_observation(binding(tmp_path), rpc_factory=native_rpc({
            'startup': [{'authMode': 'chatgpt', 'planType': 'pro'}], 'first': first}))


def test_codex_conflicting_initial_statuses_refuse(tmp_path, native_rpc):
    with pytest.raises(AccountError, match='provider_identity_changed'):
        codex_observation(binding(tmp_path), rpc_factory=native_rpc({'startup': [
            {'authMode': 'chatgpt', 'planType': 'team'}, {'authMode': 'chatgpt', 'planType': 'pro'}]}))


@pytest.mark.parametrize('notify_at', ['quota', 'second_read'])
@pytest.mark.parametrize('early', [False, True])
def test_codex_delayed_matching_status_keeps_authoritative_identity(tmp_path, native_rpc, notify_at, early):
    # Replaces the regression test that formerly required every delayed status to refuse.
    scenario = {'notify_at': notify_at, 'delayed': [{'authMode': 'chatgpt', 'planType': 'pro'}] * 2}
    if early:
        scenario['startup'] = [{'authMode': 'chatgpt', 'planType': 'pro'}]
    row = codex_observation(binding(tmp_path), rpc_factory=native_rpc(scenario))
    assert row['provider_id'] == row['pool_id'] == 'identity-a'
    assert row['windows']['codex:300']['pct'] == 15
    assert row['email_verified'] is False


@pytest.mark.parametrize('notify_at', ['quota', 'second_read'])
@pytest.mark.parametrize('status', [
    {'authMode': 'chatgpt', 'planType': 'team'},
    {'authMode': 'apiKey', 'planType': 'pro'},
    {'authMode': 'chatgpt', 'planType': 'unknown'},
    {'authMode': 'chatgpt', 'planType': 'pro', 'accountId': 'foreign'},
    {'authMode': 'chatgpt', 'planType': 'pro', 'email': 'other@example.test'},
    {'authMode': 'chatgpt', 'planType': None},
    {'authMode': 'chatgpt', 'planType': ''},
    {'authMode': 'chatgpt', 'planType': 1},
    {'authMode': 'chatgpt'}, {}, None, [],
])
def test_codex_delayed_conflicting_or_malformed_status_refuses(tmp_path, native_rpc, notify_at, status):
    with pytest.raises(AccountError, match='provider_identity_changed'):
        codex_observation(binding(tmp_path), rpc_factory=native_rpc({
            'notify_at': notify_at, 'delayed': [{'authMode': 'chatgpt', 'planType': 'pro'}, status]}))


@pytest.mark.parametrize('plan', ['unknown', 'free', 'self_serve_business_usage_based', 'enterprise_cbp_usage_based'])
def test_codex_matching_status_does_not_verify_non_subscription_plan(tmp_path, native_rpc, plan):
    with pytest.raises(AccountError, match='subscription_identity_unverified'):
        codex_observation(binding(tmp_path), rpc_factory=native_rpc({
            'startup': [{'authMode': 'chatgpt', 'planType': plan}], 'first': {'planType': plan}}))


@pytest.mark.parametrize('identity', [None, '', 123])
def test_codex_delayed_matching_status_cannot_supply_missing_quota_identity(tmp_path, native_rpc, identity):
    with pytest.raises(AccountError, match='provider_identity_unavailable'):
        codex_observation(binding(tmp_path), rpc_factory=native_rpc({
            'notify_at': 'quota', 'quota_identity': identity}))


@pytest.mark.parametrize('first', [{'type': 'apiKey'}, {'planType': None}, {'planType': ''}, {'planType': 1}])
def test_codex_invalid_first_status_cannot_authorize_a_later_announcement(tmp_path, native_rpc, first):
    with native_rpc({'first': first, 'notify_at': 'quota'})(binding(tmp_path)) as rpc:
        rpc.call('account/read', {'refreshToken': False})
        with pytest.raises(AccountError, match='provider_identity_changed'):
            rpc.call('account/rateLimits/read')


@pytest.mark.parametrize('after', [{'id': 'identity-b'}, {'email': 'other@example.test'}, {'planType': 'team'}, {'type': 'apiKey'}])
@pytest.mark.parametrize('notify_at', [None, 'quota', 'second_read'])
def test_codex_before_after_identity_mismatch_refuses(tmp_path, native_rpc, after, notify_at):
    with pytest.raises(AccountError, match='provider_identity_changed'):
        codex_observation(binding(tmp_path), rpc_factory=native_rpc({'after': after, 'notify_at': notify_at}))


@pytest.mark.parametrize('scenario,code', [({'error': True}, 'provider_quota_blocked'),
                                        ({'timeout': True}, 'provider_timeout')])
def test_codex_initial_status_does_not_swallow_error_or_timeout(tmp_path, native_rpc, scenario, code):
    with pytest.raises(AccountError, match=code):
        codex_observation(binding(tmp_path), rpc_factory=native_rpc(dict(scenario,
            startup=[{'authMode': 'chatgpt', 'planType': 'pro'}])))


@pytest.mark.parametrize('notify_at', ['quota', 'second_read'])
@pytest.mark.parametrize('failure,code', [('error', 'provider_quota_blocked'), ('timeout', 'provider_timeout')])
def test_codex_delayed_matching_status_does_not_swallow_error_or_timeout(tmp_path, native_rpc, notify_at, failure, code):
    with pytest.raises(AccountError, match=code):
        codex_observation(binding(tmp_path), rpc_factory=native_rpc({
            'notify_at': notify_at, 'fail_at': notify_at, failure: True}))


def test_codex_interrupted_initial_read_reaps_native_process(tmp_path, native_rpc):
    rpc = native_rpc({})(binding(tmp_path))
    with pytest.raises(KeyboardInterrupt):
        with rpc:
            raise KeyboardInterrupt()


@pytest.mark.parametrize('failure', ['wrong-quota-identity', 'conflicting-plan', 'conflicting-type'])
def test_codex_delayed_status_executor_ledger_health_and_attribution(tmp_path, native_rpc, monkeypatch, failure):
    import asyncio
    from functools import partial
    from account_executor import AccountExecutor
    from account_runtime import AccountRuntime
    import provider_account_host
    from provider_account_host import NativeHost
    from sessions import Sessions
    from store import Store

    # Only the native process and version command are counterparts. The host,
    # executor, runtime and persistent ledger run their real observation paths.
    private = tmp_path / 'private'; private.mkdir(mode=0o700)
    scenario = {'notify_at': 'quota'}
    monkeypatch.setattr(provider_account_host, 'codex_observation', partial(codex_observation,
        rpc_factory=lambda account: native_rpc(dict(scenario))(account), now=lambda: 1000))
    host = NativeHost({'host': 'host-a', 'registry': str(private / 'registry.json'),
        'root': str(private / 'homes'), 'cells': {'codex:primary': {
            'enabled': True, 'expected_provider_id': 'identity-a', 'profile_ref': 'fixture-profile',
            'version': 'fixture-1', 'executable_sha256': 'fixture-pin'}}},
        run=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout='fixture-1'))
    draft = host.registry.prepare(provider='codex', alias='primary', host='host-a', generation='fixture-one',
        profile_ref='fixture-profile', executable='/bin/echo', version='fixture-1', platform='macos')
    fake_auth = Path(draft.home) / 'auth.json'; fake_auth.write_text('fake-only'); fake_auth.chmod(0o600)
    account = host.registry.activate(draft, {'provider_id': 'identity-a', 'pool_id': 'identity-a',
        'auth_mode': 'subscription', 'store': 'file', 'minimal_turn': True, 'independent_login': True},
        expected_provider_id='identity-a')

    async def journey():
        store = Store(str(tmp_path / 'state.db')); store.start()
        runtime = AccountRuntime(store, Sessions(store, local_host='host-a'),
            policy={'enabled': True}, clock=lambda: 1000)
        calls = []
        async def execute(selected, action, operation_id, generation=None):
            calls.append(action)
            assert selected == account and action == 'observe' and generation is None
            try:
                return host.handle(action, provider=selected.provider, alias=selected.alias,
                    expected_generation=selected.generation, operation_id=operation_id)
            except AccountError as exc:
                # The native host CLI reports safe errors across the real transport boundary.
                return {'outcome': str(exc)}
        executor = AccountExecutor(runtime, None, execute=execute)
        try:
            await runtime.run(lambda ledger: ledger.register(account))
            await executor.collect_all()
            assert await runtime.run(lambda ledger: ledger.ensure_available(account.key)) == account
            healthy = (await runtime.run(lambda ledger: ledger.readback(now=1000)))['accounts'][0]
            assert healthy['health'] == 'healthy' and healthy['health_revision'] == 0
            assert healthy['observation']['collection'] == 'ok'
            session = await store.open_session('host-a', 'fixture-seat', provider='codex',
                session_generation='fixture-session', account_binding=account.public())
            await runtime.bind_launch(session)

            if failure == 'wrong-quota-identity':
                # Same plan/type and stable account reads cannot override the actual quota owner.
                scenario['quota_identity'] = 'foreign-identity'
            else:
                scenario['delayed'] = [{'authMode': 'apiKey' if failure == 'conflicting-type' else 'chatgpt',
                                        'planType': 'team' if failure == 'conflicting-plan' else 'pro'}]
            result = await executor.observe(account)
            assert result['outcome'] == ('ok' if failure == 'wrong-quota-identity' else 'provider_identity_changed')
            conflicted = (await runtime.run(lambda ledger: ledger.readback(now=1000)))['accounts'][0]
            assert conflicted['health'] == 'identity_conflict' and conflicted['health_revision'] == 1
            assert conflicted['observation']['collection'] == ('identity_conflict' if failure == 'wrong-quota-identity' else 'failed')
            with pytest.raises(AccountError, match='account_unavailable'):
                await runtime.run(lambda ledger: ledger.ensure_available(account.key))

            scenario.clear(); scenario.update(notify_at='second_read')
            await executor.collect_all()
            sticky = (await runtime.run(lambda ledger: ledger.readback(now=1000)))['accounts'][0]
            assert sticky['observation']['collection'] == 'ok'
            assert sticky['health'] == 'identity_conflict' and sticky['health_revision'] == 1
            with pytest.raises(AccountError, match='account_unavailable'):
                await runtime.run(lambda ledger: ledger.ensure_available(account.key))
            assert await runtime.run(lambda ledger: ledger.bound(account.key)) == account
            assert (await store.fetch_session('host-a', 'fixture-seat'))['account_binding'] == account.public()
            assert await runtime.run(lambda ledger: json.loads(ledger.conn.execute(
                'SELECT data FROM v2_account_session_bindings').fetchone()[0])) == account.public()
            assert calls == ['observe'] * 3
        finally:
            store.stop()
    asyncio.run(journey())
