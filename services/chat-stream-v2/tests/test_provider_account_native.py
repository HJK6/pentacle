from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from provider_accounts import AccountError, Binding, Registry
from provider_account_native import codex_observation, claude_observation, enroll


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


def test_claude_all_windows_from_one_fresh_named_home_cache(tmp_path):
    account = binding(tmp_path, "claude")
    def run(argv, **kwargs):
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai",
            "apiProvider": "firstParty", "configDirectory": str(tmp_path), "orgId": "pool-a", "email": "person@example.test", "subscriptionType": "max"}))
    def collect(selected, env):
        assert selected == account and env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
        (tmp_path / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"accountUuid": "login-user", "organizationUuid": "pool-a", "emailAddress": "person@example.test"},
            "cachedUsageUtilization": {"accountUuid": "login-user", "fetchedAtMs": 1000500,
                "utilization": {key: {"utilization": 12, "resets_at": "2027-01-01T00:00:00Z"}
                                for key in ["five_hour", "seven_day", "seven_day_model"]}}}))
    # Native login account cache exists before quota collection.
    collect(account, {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    ticks = iter([1000, 1001])
    row = claude_observation(account, collect_usage=collect, run=run, now=lambda: next(ticks))
    assert row["provider_id"] == "login-user" and row["pool_id"] == "pool-a" and row["observed_at"] == 1000.5
    assert len(row["windows"]) == 3


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
