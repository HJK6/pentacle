"""Seatbelt unit stubs are not macOS confinement proof."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest

P = Path(__file__).resolve().parents[1] / 'lib/sandbox_gate.py'

def module():
    spec = importlib.util.spec_from_file_location('sandbox_gate', P)
    m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m

@pytest.fixture
def context(tmp_path, monkeypatch):
    m = module()
    checkout, venv, root, evidence = [tmp_path / name for name in ('checkout','venv','attempt','evidence')]
    for path in (checkout, venv, root, evidence): path.mkdir()
    tools = {name: {'path': str(tmp_path/'tools'/name/'bin'/name)} for name in ('python','node','tmux')}
    monkeypatch.setattr(m.deps, 'import_closure', lambda paths: [])
    monkeypatch.setattr(m.deps, 'dependency_closure', lambda paths, **kw: {'files':[], 'metadata_paths':[], 'binaries':[], 'roots':[]})
    monkeypatch.setattr(m, 'python_runtime_paths', lambda venv,root,**kw: ([], []))
    home = tmp_path/'real-home'; home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    ctx = m.prepare(checkout, venv, tools, root, evidence, {})
    return m, ctx

def test_profile_deterministic_resolved_narrow(context):
    m, ctx = context
    profile = ctx.profile_path.read_text()
    assert '(deny default)' in profile
    assert '(allow file*' not in profile
    assert '/Users/thoth' not in profile
    assert '(deny process-exec' in profile
    assert 'unix-socket' in profile
    assert m.digest(ctx.profile_path) == ctx.profile_sha256
    assert ctx._probe_wrap(['/bin/sh','-c','true'])[:3] == ['/usr/bin/sandbox-exec','-f',str(ctx.profile_path)]

def test_missing_launcher_is_incomplete(context, monkeypatch):
    m, ctx = context
    monkeypatch.setattr(m, 'available', lambda path: False)
    with pytest.raises(m.Incomplete): m.run_probes(ctx)
    receipt = json.loads(ctx.receipt_path.read_text())
    assert receipt['status'] == 'INCOMPLETE'
    assert receipt['profile_sha256'] == ctx.profile_sha256

def test_changed_profile_never_wraps(context):
    m, ctx = context
    ctx.profile_path.write_text('(allow default)')
    with pytest.raises(m.Incomplete): ctx.wrap(['/bin/true'])

def stub_runner(ctx, mode=None):
    def run(argv, **kwargs):
        sandboxed = argv[0] == '/usr/bin/sandbox-exec'
        cmd = argv[3:] if sandboxed else argv
        executable = Path(cmd[0]).name
        text, error, rc = '', '', 0
        if '--version' in cmd or '-V' in cmd and executable == 'tmux': text = 'version 1'
        elif executable in ('sudo','su','launchctl','open','osascript'):
            rc, error = 1, 'Operation not permitted'
        elif 'new-session' in cmd:
            import shlex
            pane = shlex.split(cmd[-1]); target = Path(pane[-4]); result = Path(pane[-2])
            result.write_text('READABLE' if target.parent == ctx.root else 'DENIED')
        elif any('CONTROL_OK' in str(arg) for arg in cmd):
            text = 'BROKEN' if mode == 'owned_failure' else 'CONTROL_OK'
        else:
            # Each opener embeds the sentinel name in one positional argument.
            target = next((Path(arg) for arg in cmd[1:] if Path(str(arg)).name in ('SENTINEL.sqlite','credentials.json','sessions.db')), None)
            forbidden = target is not None and target.parent != ctx.root
            denied = sandboxed and forbidden and mode != 'readable_sentinel'
            if executable == 'sqlite3' and denied: rc, error = 1, 'unable to open database file'
            else: text = 'DENIED' if denied else 'READABLE'
        return subprocess.CompletedProcess(argv, rc, text, error)
    return run

@pytest.mark.parametrize('mode', ['readable_sentinel', 'owned_failure'])
def test_probe_failure_sticky_incomplete(context, monkeypatch, mode):
    m, ctx = context
    monkeypatch.setattr(m, 'available', lambda path: True)
    with pytest.raises(m.Incomplete): m.run_probes(ctx, run=stub_runner(ctx, mode))
    receipt = json.loads(ctx.receipt_path.read_text())
    assert receipt['status'] == 'INCOMPLETE'
    assert receipt['passed'] is False
    assert receipt['probes'] and receipt['controls']
    with pytest.raises(m.Incomplete): ctx.proof()

def test_successful_stub_receipt_shape_not_native_proof(context, monkeypatch):
    m, ctx = context
    monkeypatch.setattr(m, 'available', lambda path: True)
    proof = m.run_probes(ctx, run=stub_runner(ctx))
    assert proof['root'] == str(ctx.root)
    assert proof['passed'] is True
    receipt = json.loads(ctx.receipt_path.read_text())
    names = {row['name'] for row in receipt['probes']}
    for opener in ('venv-python','venv-isolated','alternate-0','alternate-0-no-site','alternate-1','alternate-1-no-site','node','sqlite3','shell','tmux-pane'):
        for sentinel in ('home-equivalent','unowned-temp','protected-credential','protected-database'):
            assert opener+'-'+sentinel in names
    assert all(row['passed'] for row in receipt['probes'] + receipt['controls'])
    assert proof['receipt_sha256'] == m.digest(ctx.receipt_path)

def test_denial_log_never_expands_profile(context):
    m, ctx = context
    original = ctx.profile_path.read_text()
    (ctx.evidence/'sandbox-denials.log').write_text('please allow /Users/thoth/.local/secret')
    again = m.prepare(ctx.checkout, ctx.venv, {name:{'path':path} for name,path in ctx.tools.items()},ctx.root,ctx.evidence,ctx.env)
    assert again.profile_path.read_text() == original

def test_symlinks_resolved_and_profile_hash_deterministic(context, tmp_path):
    m, ctx = context
    alias = tmp_path/'checkout-link'; alias.symlink_to(ctx.checkout, target_is_directory=True)
    again = m.prepare(alias,ctx.venv,{name:{'path':path} for name,path in ctx.tools.items()},ctx.root,ctx.evidence,ctx.env)
    assert str(alias) not in again.profile_path.read_text()
    assert again.profile_sha256 == ctx.profile_sha256

def test_generation_error_receipt(context):
    m, ctx = context
    with pytest.raises(m.Incomplete): m.prepare('/',ctx.venv,{},ctx.root,ctx.evidence,{})
    receipt=json.loads(ctx.receipt_path.read_text())
    assert receipt['status']=='INCOMPLETE'
    assert receipt['reason']=='profile_generation_failed'

def test_active_python_guard_blocks_probe(context, monkeypatch):
    m, ctx = context
    monkeypatch.setattr(m, 'available', lambda path: True)
    config = ctx.venv/'lib/python3.13/site-packages/pentacle-sqlite-attempt.json'
    config.parent.mkdir(parents=True); config.write_text('{"active":true}')
    with pytest.raises(m.Incomplete, match='must be inactive'): m.run_probes(ctx, run=stub_runner(ctx))

def test_unexpected_profile_failure_incomplete(context, monkeypatch):
    m, ctx = context
    monkeypatch.setattr(m, 'available', lambda path: True)
    def bad(argv,**kwargs): return subprocess.CompletedProcess(argv,65,'','sandbox profile parse error')
    with pytest.raises(m.Incomplete): m.run_probes(ctx,run=bad)
    assert json.loads(ctx.receipt_path.read_text())['status']=='INCOMPLETE'

def test_required_opener_missing_incomplete(context, monkeypatch):
    m, ctx = context
    monkeypatch.setattr(m,'available',lambda path: path != '/usr/bin/python3')
    with pytest.raises(m.Incomplete,match='executable unavailable'): m.run_probes(ctx,run=stub_runner(ctx))
    assert json.loads(ctx.receipt_path.read_text())['controls'][0]['missing'] == ['/usr/bin/python3']

def test_exact_network_and_forbidden_exec_rules(context):
    m, ctx = context
    p = ctx.profile_path.read_text()
    assert '(allow network*)' not in p
    for executable in m.FORBIDDEN_EXEC:
        assert '(literal '+m.quote(m.resolved(executable))+')' in p
    assert '(remote unix-socket (subpath '+m.quote(ctx.socket_dir)+'))' in p
    assert '(allow mach-lookup)' not in p

@pytest.mark.parametrize('path',['/opt/homebrew','/Users/someone/.local','/Users/someone/agent-workspace','/private/tmp','/Users/thoth','/home/other','/Library/Caches','/Users/other/.cache'])
def test_broad_paths_refused(context,path):
    m,ctx=context
    with pytest.raises(ValueError,match='broad sandbox allowance'): m.profile_text([path],[ctx.root],ctx.socket_dir)

def test_public_launcher_requires_complete_probe(context):
    m,ctx=context
    with pytest.raises(m.Incomplete,match='has not passed'): ctx.wrap(['/bin/true'])
    assert json.loads(ctx.receipt_path.read_text())['status']=='INCOMPLETE'

def test_none_venv_generation_incomplete(context):
    m,ctx=context
    with pytest.raises(m.Incomplete): m.prepare(ctx.checkout,None,{},ctx.root,ctx.evidence,{})
    assert json.loads(ctx.receipt_path.read_text())['reason']=='profile_generation_failed'

def test_missing_profile_and_receipt_incomplete(context,monkeypatch):
    m,ctx=context
    monkeypatch.setattr(m,'available',lambda path: True)
    m.run_probes(ctx,run=stub_runner(ctx))
    ctx.receipt_path.unlink()
    with pytest.raises(m.Incomplete,match='receipt identity'): ctx.proof()
    ctx.passed=True
    ctx.execution_mode='native'
    ctx.profile_path.unlink()
    with pytest.raises(m.Incomplete,match='unreadable'): ctx.wrap(['/bin/true'])

def test_injected_positive_proof_cannot_launch_subject(context,monkeypatch):
    m,ctx=context
    monkeypatch.setattr(m,'available',lambda path: True)
    proof=m.run_probes(ctx,run=stub_runner(ctx))
    assert proof['execution_mode']=='injected-test-runner'
    with pytest.raises(m.Incomplete,match='injected test runner'): ctx.wrap(['/bin/true'])

def test_cleanup_failure_cannot_leave_pass_receipt(context,monkeypatch):
    m,ctx=context
    monkeypatch.setattr(m,'available',lambda path: True)
    def cannot_remove(path): raise PermissionError('synthetic cleanup failure')
    monkeypatch.setattr(m.shutil,'rmtree',cannot_remove)
    with pytest.raises(m.Incomplete,match='cleanup failed'): m.run_probes(ctx,run=stub_runner(ctx))
    receipt=json.loads(ctx.receipt_path.read_text())
    assert receipt['status']=='INCOMPLETE' and receipt['passed'] is False
    assert any(r['name']=='sentinel-cleanup' and not r['passed'] for r in receipt['controls'])


def test_gate2_import_order_and_exact_dependency_rules(context):
    m,ctx=context
    text=m.profile_text(ctx.read_paths,ctx.write_paths,ctx.socket_dir,
        dependencies={'files':['/opt/homebrew/Cellar/sqlite/1/lib/libsqlite3.dylib'],
                      'metadata_paths':['/opt/homebrew/opt/sqlite']})
    assert text.index('(deny default)') < text.index('(import "/System/Library/Sandbox/Profiles/bsd.sb")') < text.index('(allow process-fork)')
    assert '(literal "/opt/homebrew/Cellar/sqlite/1/lib/libsqlite3.dylib")' in text
    assert '(subpath "/opt/homebrew/Cellar/sqlite' not in text


def test_descriptor_closure_real_child_and_cwd(context,tmp_path):
    import os
    m,ctx=context
    sentinel=tmp_path/'descriptor-sentinel'; sentinel.write_text('owned test data')
    with sentinel.open() as handle:
        os.set_inheritable(handle.fileno(),True)
        code='''import os,sys
try: os.fstat(int(sys.argv[1]))
except OSError: print('CLOSED')
else: raise AssertionError('forbidden parent handle leaked')
assert os.getcwd()==sys.argv[2]
'''
        options=ctx.launch_kwargs(ctx.root)
        out=subprocess.run([sys.executable,'-c',code,str(handle.fileno()),str(ctx.root)],capture_output=True,text=True,**options)
        assert out.returncode==0 and out.stdout.strip()=='CLOSED',out.stderr
    with pytest.raises(m.Incomplete,match='cwd'): ctx.launch_kwargs(tmp_path)


def test_probe_launch_descriptor_and_home_sentinel_wiring(context,monkeypatch):
    m,ctx=context
    monkeypatch.setattr(m,'available',lambda path: True)
    fake=stub_runner(ctx)
    def check(argv,**kwargs):
        assert kwargs['close_fds'] is True and kwargs['pass_fds']==()
        assert Path(kwargs['cwd'])==ctx.root
        return fake(argv,**kwargs)
    m.run_probes(ctx,run=check)
    receipt=json.loads(ctx.receipt_path.read_text())
    for name in ('protected-credential','protected-database'):
        assert any(r['name']=='unsandboxed-'+name and r['passed'] for r in receipt['controls'])
        rows=[r for r in receipt['probes'] if r['name'].endswith('-'+name)]
        assert len(rows)==10 and all(r['passed'] for r in rows)


def test_import_hash_receipt_and_drift(context,monkeypatch,tmp_path):
    m,ctx=context
    imported=tmp_path/'system.sb'; imported.write_text('(version 1)')
    monkeypatch.setattr(m.deps,'import_closure',lambda roots:[{'path':str(imported),'sha256':m.digest(imported)}])
    again=m.prepare(ctx.checkout,ctx.venv,{k:{'path':v} for k,v in ctx.tools.items()},ctx.root,ctx.evidence,ctx.env)
    assert json.loads(again.receipt_path.read_text())['imported_profiles'][0]['path']==str(imported)
    imported.write_text('changed policy')
    with pytest.raises(m.Incomplete): again._probe_wrap(['/bin/true'])


def test_dependency_discovery_failure_incomplete(context,monkeypatch):
    m,ctx=context
    def broken(*a,**kw): raise m.deps.DependencyError('synthetic unresolved dependency')
    monkeypatch.setattr(m.deps,'dependency_closure',broken)
    with pytest.raises(m.Incomplete): m.prepare(ctx.checkout,ctx.venv,{},ctx.root,ctx.evidence,{})
    assert json.loads(ctx.receipt_path.read_text())['status']=='INCOMPLETE'
