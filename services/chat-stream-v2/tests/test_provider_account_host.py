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
