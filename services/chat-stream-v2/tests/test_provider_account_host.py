"""Real host executor/registry with fake native subscription counterparts."""
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace
import pytest
from provider_account_host import NativeHost
from provider_accounts import AccountError

@pytest.mark.parametrize("provider,platform",[("codex","macos"),("claude","macos"),("claude","wsl")])
def test_host_final_home_activation_idempotence_and_wrong_login_isolation(tmp_path,provider,platform):
    private=tmp_path/"private"; private.mkdir(mode=0o700)
    digest=hashlib.sha256(Path('/bin/echo').read_bytes()).hexdigest()
    cells={f"{provider}:{alias}":{"enabled":True,"expected_provider_id":f"id-{alias}","profile_ref":f"google-{alias}.one",
        "executable":"/bin/echo","version":"fixture","executable_sha256":digest,"platform":platform,
        "model":"fixture-model","effort":"high","cwd":str(tmp_path)} for alias in ("primary","spare")}
    config={"host":"host-a","registry":str(private/"registry.json"),"root":str(private/"homes"),"cells":cells}
    calls=[]; wrong=[False]
    def login(account,*args):
        calls.append(account.home)
        if account.provider=='codex' or account.platform=='wsl':
            file=Path(account.home)/('auth.json' if account.provider=='codex' else '.credentials.json')
            file.write_text('fake-only'); file.chmod(0o600)
        return 'ok'
    def observation(account):
        return {"provider_id":"foreign" if wrong[0] else f"id-{account.alias}","pool_id":f"pool-{account.alias}","auth_mode":"subscription"}
    def run(argv,**kwargs): return SimpleNamespace(returncode=0,stdout='fixture' if argv[-1]=='--version' else 'OK')
    host=NativeHost(config,run=run,login=login,observation=observation)
    def request(alias,op,generation,expected='unenrolled',action='enroll'):
        return host.handle(action,provider=provider,alias=alias,expected_generation=expected,operation_id=op,generation=generation)
    first=request('primary','one','first')
    assert first['outcome']=='ok' and '/primary/first' in first['binding']['home']
    assert request('primary','one','first')==first and len(calls)==1
    peer=request('spare','two','peer')
    assert peer['outcome']=='ok'
    wrong[0]=True
    assert request('primary','three','wrong',expected='first')['outcome']=='account_enrollment_failed'
    assert host.registry.active(provider,'primary','host-a').generation=='first'
    assert host.registry.active(provider,'spare','host-a').generation=='peer'
    with pytest.raises(AccountError,match='account_login_generation_stale'):
        request('primary','four','not-started',expected='obsolete')
    assert len(calls)==3


def test_host_crash_receipt_reuses_active_generation_without_another_login(tmp_path):
    private=tmp_path/'private'; private.mkdir(mode=0o700)
    config={'host':'host-a','registry':str(private/'registry.json'),'root':str(private/'homes'),'cells':{
        'codex:primary':{'enabled':True,'expected_provider_id':'id-a','profile_ref':'browser-a','executable':'/bin/echo',
            'version':'fixture','executable_sha256':hashlib.sha256(Path('/bin/echo').read_bytes()).hexdigest()}}}
    host=NativeHost(config)
    draft=host.registry.prepare(provider='codex',alias='primary',host='host-a',generation='new',profile_ref='browser-a',
        executable='/bin/echo',version='fixture',platform='macos')
    path=Path(draft.home)/'auth.json';path.write_text('fake-only');path.chmod(0o600)
    host.registry.activate(draft,{'provider_id':'id-a','pool_id':'pool-a','auth_mode':'subscription','store':'file',
        'minimal_turn':True,'independent_login':True},expected_provider_id='id-a')
    host._save_operation('same',{'provider':'codex','alias':'primary','expected_generation':'old','generation':'new','state':'started'})
    result=host.handle('status',provider='codex',alias='primary',expected_generation='old',operation_id='same',generation='new')
    assert result['state']=='done' and result['outcome']=='ok' and result['binding']['generation']=='new'


def test_operator_pipe_is_one_shot_private_and_non_inheritable(tmp_path):
    import os
    import subprocess
    import sys
    from provider_account_host import OperatorBrowser
    from test_account_rotation import binding
    reader, writer = os.pipe()
    os.set_inheritable(writer, True)
    browser = OperatorBrowser(writer)
    assert os.get_inheritable(writer) is False
    result = subprocess.run([sys.executable, "-c", "import os,sys;os.fstat(int(sys.argv[1]))", str(writer)],
        close_fds=False, capture_output=True)
    assert result.returncode != 0
    url = "https://auth.openai.com/oauth/authorize?state=fixture-only"
    try:
        assert browser(binding(), url) == {"outcome": "ok"}
        assert os.read(reader, 8193) == (url + "\n").encode()
        assert os.read(reader, 1) == b""
        with pytest.raises(AccountError): browser(binding(), url)
    finally:
        browser.close(); os.close(reader)


@pytest.mark.parametrize("kind", ["stdout", "stderr", "regular", "read-pipe", "closed"])
def test_operator_browser_refuses_non_private_write_descriptors(tmp_path, kind):
    import os
    from provider_account_host import OperatorBrowser
    reader, writer = os.pipe()
    file = (tmp_path / "url-must-never-be-written").open("w")
    closed = os.dup(writer); os.close(closed)
    fd = {"stdout":1, "stderr":2, "regular":file.fileno(), "read-pipe":reader, "closed":closed}[kind]
    try:
        with pytest.raises(AccountError, match="account_browser_fd_invalid"): OperatorBrowser(fd)
        assert (tmp_path / "url-must-never-be-written").read_text() == ""
    finally:
        os.close(reader); os.close(writer); file.close()


def test_operator_browser_broken_pipe_is_safe(capsys):
    import os
    from provider_account_host import OperatorBrowser
    from test_account_rotation import binding
    reader, writer = os.pipe(); os.close(reader)
    browser = OperatorBrowser(writer)
    with pytest.raises(AccountError, match="account_browser_delivery_failed"):
        browser(binding(), "https://auth.openai.com/oauth/authorize?state=fixture-secret")
    assert browser.fd is None and capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", ["timeout", "native-failure", "invalid-url", "tampered-pin"])
def test_operator_enrollment_failure_keeps_predecessor_and_secret_free_journal(tmp_path, failure):
    import os
    from functools import partial
    from provider_account_host import OperatorBrowser
    from provider_account_native import native_login
    private = tmp_path / "private"; private.mkdir(mode=0o700)
    executable = tmp_path / "provider"; executable.write_text("fixture-native")
    cell = dict(enabled=True, expected_provider_id="fixture-id", profile_ref="fixture-profile", executable=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(), version="fixture", platform="wsl",
        model="fixture-model", effort="high", cwd=str(tmp_path))
    config = dict(host="host-a", registry=str(private/"registry.json"), root=str(private/"homes"), cells={"codex:primary":cell})
    def login(account, browser):
        path = Path(account.home)/"auth.json"; path.write_text("fake-only"); path.chmod(0o600); return "ok"
    def identity(account): return {"provider_id":"fixture-id", "pool_id":"fixture-pool", "auth_mode":"subscription"}
    def run(argv, **kwargs): return SimpleNamespace(returncode=0, stdout="fixture" if argv[-1] == "--version" else "OK")
    host = NativeHost(config, run=run, login=login, observation=identity)
    assert host.handle("enroll", provider="codex", alias="primary", expected_generation="unenrolled", operation_id="old", generation="old")["outcome"] == "ok"
    closed, turns = [], []
    class RPC:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): closed.append(True)
        def call(self, method, params):
            turns.append(method)
            return {"type":"chatgpt", "authUrl": "https://evil.test/fixture-secret" if failure == "invalid-url" else
                "https://auth.openai.com/oauth/authorize?state=fixture-secret", "loginId":"fixture-login"}
        def read(self, deadline):
            if failure == "timeout": raise AccountError("provider_timeout")
            return {"method":"account/login/completed", "params":{"loginId":"fixture-login", "success":False}}
    reader, writer = os.pipe(); browser = OperatorBrowser(writer)
    host.operator_browser = browser
    host.login = partial(native_login, rpc_factory=RPC)
    if failure == "tampered-pin": executable.write_text("changed")
    try:
        result = host.handle("enroll", provider="codex", alias="primary", expected_generation="old", operation_id="new", generation="new")
        assert result["outcome"] == "account_enrollment_failed"
        assert host.registry.active("codex", "primary", "host-a").generation == "old"
        assert "fixture-secret" not in str(result)
        assert all("fixture-secret" not in p.read_text() for p in host.journal.glob("*.json"))
        if failure == "tampered-pin": assert not turns and not closed
        else: assert turns == ["account/login/start"] and closed == [True]
    finally:
        browser.close(); os.close(reader)
