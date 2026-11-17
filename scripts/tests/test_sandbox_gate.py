"""Seatbelt unit stubs are not macOS confinement proof."""
import importlib.util
import os
import json
from pathlib import Path
import subprocess
import sys
import pytest

P = Path(__file__).resolve().parents[1] / 'lib/sandbox_gate.py'
REAL_HOME = str(Path(os.path.expanduser('~')).resolve())
REAL_HOME_PARENT = str(Path(REAL_HOME).parent)  # derived at import, before any home monkeypatch

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
    monkeypatch.setattr(m, 'real_tool_file', lambda name: '/real/' + name)
    m.real_separate_gate_identity = m.separate_gate_identity
    monkeypatch.setattr(m, 'separate_gate_identity', lambda: False)
    monkeypatch.setattr(m, 'real_interpreter_files', lambda e,root: ['/real/'+Path(str(e)).name, '/real/app/'+Path(str(e)).name])
    home = tmp_path/'real-home'; home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    ctx = m.prepare(checkout, venv, tools, root, evidence, {})
    return m, ctx

def test_profile_deterministic_resolved_narrow(context):
    m, ctx = context
    profile = ctx.profile_path.read_text()
    assert '(deny default)' in profile
    assert '(allow file*' not in profile
    assert REAL_HOME not in profile
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
    (ctx.evidence/'sandbox-denials.log').write_text('please allow ' + REAL_HOME + '/.local/secret')
    again = m.prepare(ctx.checkout, ctx.venv, {name:{'path':path} for name,path in ctx.tools.items()},ctx.root,ctx.evidence,ctx.env)
    assert again.profile_path.read_text() == original

def test_symlinks_resolved_and_profile_hash_deterministic(context, tmp_path):
    m, ctx = context
    alias = tmp_path/'checkout-link'; alias.symlink_to(ctx.checkout, target_is_directory=True)
    again = m.prepare(alias,ctx.venv,{name:{'path':path} for name,path in ctx.tools.items()},ctx.root,ctx.evidence,ctx.env)
    assert str(alias) not in again.profile_path.read_text()
    assert again.profile_sha256 == ctx.profile_sha256

def test_generation_error_receipt(context, monkeypatch, tmp_path):
    m, ctx = context
    def broken(*a, **kw): raise m.deps.DependencyError('synthetic unresolved dependency')
    monkeypatch.setattr(m.deps, 'dependency_closure', broken)
    with pytest.raises(m.Incomplete): m.prepare(tmp_path / 'missing-checkout', ctx.venv, {}, ctx.root, ctx.evidence, {})
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
    monkeypatch.setattr(m,'available',lambda path: path != '/real/python3')
    with pytest.raises(m.Incomplete,match='executable unavailable'): m.run_probes(ctx,run=stub_runner(ctx))
    assert json.loads(ctx.receipt_path.read_text())['controls'][0]['missing'] == ['/real/python3']

def test_exact_network_and_forbidden_exec_rules(context):
    m, ctx = context
    p = ctx.profile_path.read_text()
    assert '(allow network*)' not in p
    for executable in m.FORBIDDEN_EXEC:
        assert '(literal '+m.quote(m.resolved(executable))+')' in p
    assert '(remote unix-socket (subpath '+m.quote(m.resolved(ctx.root))+'))' in p
    assert '(allow mach-lookup)' not in p

@pytest.mark.parametrize('path',['/opt/homebrew',REAL_HOME_PARENT + '/someone/.local',REAL_HOME_PARENT + '/someone/agent-workspace','/private/tmp',REAL_HOME,REAL_HOME_PARENT + '/other','/Library/Caches',REAL_HOME_PARENT + '/other/.cache'])
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


def _closure_recording(m, monkeypatch, unreadable):
    seen = []
    def closure(paths, **kw):
        seen.extend(paths)
        for path in paths:
            if path in unreadable: raise m.deps.DependencyError('unreadable dependency: ' + path)
        return {'files': [], 'metadata_paths': [], 'binaries': [], 'roots': []}
    monkeypatch.setattr(m.deps, 'dependency_closure', closure)
    return seen

def test_forbidden_binaries_excluded_from_dependency_closure(context, monkeypatch):
    m, ctx = context
    seen = _closure_recording(m, monkeypatch, set())
    m.prepare(ctx.checkout, ctx.venv, {k: {'path': v} for k, v in ctx.tools.items()}, ctx.root, ctx.evidence, ctx.env)
    assert seen and not set(seen) & set(m.FORBIDDEN_EXEC)
    assert '/usr/bin/sqlite3' in seen and '/bin/sh' in seen

def test_unreadable_forbidden_binary_is_not_incomplete_and_stays_denied(context, monkeypatch):
    m, ctx = context
    _closure_recording(m, monkeypatch, set(m.FORBIDDEN_EXEC))
    again = m.prepare(ctx.checkout, ctx.venv, {k: {'path': v} for k, v in ctx.tools.items()}, ctx.root, ctx.evidence, ctx.env)
    text = again.profile_path.read_text()
    for path in m.FORBIDDEN_EXEC:
        assert '(literal "%s")' % path in text.split('(deny process-exec', 1)[1].split('\n', 1)[0]

def test_unreadable_allowed_binary_is_still_incomplete(context, monkeypatch):
    m, ctx = context
    _closure_recording(m, monkeypatch, {'/usr/bin/sqlite3'})
    with pytest.raises(m.Incomplete):
        m.prepare(ctx.checkout, ctx.venv, {k: {'path': v} for k, v in ctx.tools.items()}, ctx.root, ctx.evidence, ctx.env)
    assert json.loads((ctx.evidence / 'sandbox-profile.json').read_text())['reason'] == 'profile_generation_failed'


def test_addon_scan_is_bounded_to_native_directories(tmp_path, monkeypatch):
    m = module()
    checkout = tmp_path / 'checkout'
    (checkout / 'src' / 'huge' / 'deep').mkdir(parents=True)
    (checkout / 'src' / 'huge' / 'deep' / 'trap.node').write_text('x')
    (checkout / 'node_modules' / 'pkg' / 'build').mkdir(parents=True)
    (checkout / 'node_modules' / 'pkg' / 'build' / 'a.node').write_text('x')
    (checkout / 'prebuilds').mkdir()
    tops = []
    real_walk = os.walk
    def recording(top, *a, **kw):
        tops.append(Path(top)); return real_walk(top, *a, **kw)
    monkeypatch.setattr(m.os, 'walk', recording)
    found = m.addon_files(checkout)
    assert [Path(p).name for p in found] == ['a.node']
    allowed = [checkout / d for d in m.NATIVE_ADDON_DIRS]
    assert tops and all(any(t == a or a in t.parents for a in allowed) for t in tops)
    assert not any('src' in t.parts for t in tops)
    assert m.addon_files(tmp_path / 'does-not-exist') == []


def test_real_interpreter_files_derived_from_running_the_interpreter(tmp_path, monkeypatch):
    m = module()
    real = tmp_path / 'real-python'; app = tmp_path / 'app-python'; real.write_text('x'); app.write_text('x')
    calls = []
    def fake(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([str(real), str(app)]), stderr='')
    monkeypatch.setattr(m.subprocess, 'run', fake)
    assert m.real_interpreter_files('/shim/python3', tmp_path) == [str(real), str(app)]
    assert calls[0][1:4] == ['-I', '-S', '-B']
    monkeypatch.setattr(m.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=json.dumps([str(tmp_path / 'gone'), None]), stderr=''))
    with pytest.raises(m.deps.DependencyError): m.real_interpreter_files('/shim/python3', tmp_path)


def test_shim_replaced_by_real_interpreter_in_probes_and_closure(context, monkeypatch):
    m, ctx = context
    seen = []
    def closure(paths, **kw):
        seen.extend(paths); return {'files': [], 'metadata_paths': [], 'binaries': [], 'roots': []}
    monkeypatch.setattr(m.deps, 'dependency_closure', closure)
    again = m.prepare(ctx.checkout, ctx.venv, {k: {'path': v} for k, v in ctx.tools.items()}, ctx.root, ctx.evidence, ctx.env)
    assert '/usr/bin/python3' not in again.alternates and '/real/python3' in again.alternates
    assert '/real/python3' in seen and '/real/app/python3' in seen and '/real/app/python' in seen
    assert '/usr/bin/python3' not in seen
    assert json.loads(again.receipt_path.read_text())['alternate_interpreters'] == again.alternates


def test_node_uses_empty_owned_openssl_config_without_a_grant(context):
    m, ctx = context
    cfg = Path(ctx.env['OPENSSL_CONF'])
    assert cfg.read_text() == '' and ctx.root in cfg.parents
    assert 'openssl' not in ctx.profile_path.read_text().lower()


def test_same_user_profile_has_no_terminal_device_grant(context):
    m, ctx = context
    text = ctx.profile_path.read_text()
    assert '/dev/ptmx' not in text and '/dev/tty' not in text and 'ttys' not in text


def test_pty_grant_only_for_a_separate_gate_identity(context, monkeypatch):
    m, ctx = context
    plain = m.profile_text(ctx.read_paths, ctx.write_paths, ctx.socket_dir)
    assert '/dev/ptmx' not in plain
    text = m.profile_text(ctx.read_paths, ctx.write_paths, ctx.socket_dir, pty=True)
    line = [l for l in text.splitlines() if '/dev/ptmx' in l]
    assert line == ['(allow file-read* file-write* file-ioctl (literal "/dev/ptmx") (literal "/dev/tty") (regex #"^/dev/ttys[0-9]+$"))']
    assert '(subpath "/dev' not in text
    assert m.real_separate_gate_identity(uid=0, groups=[20], console_uid=501, admin_gid=80) is False
    assert m.real_separate_gate_identity(uid=501, groups=[20], console_uid=501, admin_gid=80) is False
    assert m.real_separate_gate_identity(uid=551, groups=[20, 80], console_uid=501, admin_gid=80) is False
    assert m.real_separate_gate_identity(uid=551, groups=[20], console_uid=501, admin_gid=80) is True


def test_signal_process_info_ps_and_socket_rules_are_exact(context):
    m, ctx = context
    text = m.profile_text(ctx.read_paths, ctx.write_paths, ctx.socket_dir, socket_root=ctx.root)
    assert '(allow signal (target same-sandbox))' in text.splitlines()
    assert '(allow signal (target self))' not in text and 'process-info' not in text
    assert text.count('no-sandbox') == 1 and '(allow process-exec (with no-sandbox) (literal "/bin/ps"))' in text.splitlines()
    lines = text.splitlines()
    ps_rule = lines.index('(allow process-exec (with no-sandbox) (literal "/bin/ps"))')
    exact = [i for i, l in enumerate(lines) if l.startswith('(allow process-exec ') and 'with no-sandbox' not in l]
    assert all(i < ps_rule for i in exact)  # last matching rule wins, so the unsandboxed ps rule must follow every other exec allow
    root = str(ctx.root.resolve())
    for op in ('network-bind (local', 'network-inbound (local', 'network-outbound (remote'):
        assert '(allow %s unix-socket (subpath "%s")))' % (op, root) in text.splitlines()
    assert str(ctx.socket_dir.resolve()) not in ''.join(l for l in text.splitlines() if 'unix-socket' in l)


def test_owned_bin_dir_holds_exact_git_and_python3_links_first_on_path(context):
    m, ctx = context
    bin_dir = ctx.root / 'bin'
    assert (bin_dir / 'git').is_symlink() and os.readlink(bin_dir / 'git') == '/real/git'
    assert os.readlink(bin_dir / 'python3') == str(ctx.venv / 'bin/python')
    assert ctx.env['PATH'].split(os.pathsep)[0] == str(bin_dir)
    assert ctx.env['GIT_CONFIG_GLOBAL'] == os.devnull and ctx.env['GIT_CONFIG_NOSYSTEM'] == '1'


def test_real_tool_file_resolves_through_xcrun_and_fails_closed(tmp_path, monkeypatch):
    m = module()
    tool = tmp_path / 'git'; tool.write_text('x'); tool.chmod(0o755)
    monkeypatch.setattr(m.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=str(tool) + '\n', stderr=''))
    assert m.real_tool_file('git') == str(tool.resolve())
    monkeypatch.setattr(m.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=str(tmp_path / 'gone') + '\n', stderr=''))
    with pytest.raises(m.deps.DependencyError): m.real_tool_file('git')



@pytest.mark.parametrize('rc,error,passed', [(0, '', True), (1, 'no server running on /x/probe.sock', True), (1, 'permission denied', False)])
def test_owned_tmux_cleanup_accepts_an_already_exited_server(context, monkeypatch, rc, error, passed):
    m, ctx = context
    monkeypatch.setattr(m, 'available', lambda path: True)
    inner = stub_runner(ctx)
    def run(argv, **kwargs):
        cmd = argv[3:] if argv[0] == '/usr/bin/sandbox-exec' else argv
        if 'new-session' in cmd:
            (ctx.socket_dir / 'probe.sock').write_text('')
        if 'kill-server' in cmd:
            return subprocess.CompletedProcess(argv, rc, '', error)
        return inner(argv, **kwargs)
    if passed:
        m.run_probes(ctx, run=run)
    else:
        with pytest.raises(m.Incomplete): m.run_probes(ctx, run=run)
    cleanup = [row for row in ctx.controls if row['name'] == 'owned-tmux-cleanup']
    assert cleanup and cleanup[0]['passed'] is passed


def test_bwrap_argv_is_the_proven_boundary(tmp_path):
    m = module()
    owned = tmp_path / 'owned'; owned.mkdir()
    argv = m.bwrap_argv(['/usr', '/etc'], [owned], ['/usr/bin/sudo'])
    for flag in ('--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--unshare-net', '--new-session', '--die-with-parent'):
        assert flag in argv
    assert argv[argv.index('--cap-drop') + 1] == 'ALL'
    assert argv[argv.index('--dev') + 1] == '/dev'  # a private devpts, so tmux needs no separate account
    assert ['--ro-bind', '/usr', '/usr'] == argv[argv.index('/usr') - 1:argv.index('/usr') + 2]
    assert ['--bind', str(owned.resolve()), str(owned.resolve())] == argv[argv.index('--bind'):argv.index('--bind') + 3]
    assert ['--ro-bind', '/dev/null', '/usr/bin/sudo'] == argv[-3:]  # forbidden executables are emptied last
    assert str(Path.home()) not in argv


@pytest.mark.parametrize('path', ['/', '/tmp', str(Path.home()), '/x/.local', '/x/agent-workspace'])
def test_bwrap_argv_refuses_broad_allowances(path):
    with pytest.raises(ValueError, match='broad sandbox allowance refused'):
        module().bwrap_argv([path], [], [])


@pytest.mark.skipif(not sys.platform.startswith('linux') or not os.access('/usr/bin/bwrap', os.X_OK), reason='needs Linux bubblewrap')
def test_bwrap_jail_hides_the_outside_and_keeps_owned_writes(tmp_path):
    m = module()
    owned, outside = tmp_path / 'owned', tmp_path / 'outside'
    owned.mkdir(); outside.mkdir(); (outside / 's.txt').write_text('SECRET')
    jail = m.bwrap_argv(['/usr', '/etc'], [owned], [])
    probe = subprocess.run([*jail, '--', '/bin/sh', '-c', 'cat "$1" 2>&1; echo w > "$2/x" && echo wrote; ls /dev/pts | head -1',
                            'probe', str(outside / 's.txt'), str(owned)], capture_output=True, text=True, timeout=30)
    if probe.returncode and 'namespace' in probe.stderr:
        pytest.skip('unprivileged user namespaces unavailable here')
    assert 'SECRET' not in probe.stdout and 'No such file' in probe.stdout
    assert 'wrote' in probe.stdout and (owned / 'x').read_text() == 'w\n'
