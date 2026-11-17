"""Exact-profile Seatbelt preflight. Stub tests never constitute OS proof."""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import grp
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

_deps_spec = importlib.util.spec_from_file_location("_gate_sandbox_dependencies", Path(__file__).with_name("sandbox_dependencies.py"))
deps = importlib.util.module_from_spec(_deps_spec)
sys.modules[_deps_spec.name] = deps
_deps_spec.loader.exec_module(deps)

BASE_PROFILE = '/System/Library/Sandbox/Profiles/bsd.sb'
EXTRA_BINARIES = ('/bin/cat', '/bin/bash', '/usr/bin/git', '/bin/ps', '/usr/sbin/lsof', '/usr/bin/uname', '/usr/bin/env', '/usr/bin/true', '/usr/bin/dirname', '/sbin/ifconfig', '/usr/bin/awk', '/bin/sleep')

LAUNCHER = '/usr/bin/sandbox-exec'
ALTERNATE_PYTHONS = ('/opt/homebrew/opt/python@3.13/bin/python3.13', '/usr/bin/python3')
FORBIDDEN_EXEC = ('/usr/bin/sudo', '/usr/bin/su', '/bin/launchctl', '/usr/bin/open', '/usr/bin/osascript')
SYSTEM_READ = ('/System', '/usr/lib', '/usr/bin', '/usr/sbin', '/usr/share', '/bin', '/sbin', '/Library/Apple/System/Library')

class Incomplete(RuntimeError):
    pass

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def resolved(path):
    return str(Path(path).resolve())

def quote(path):
    # SBPL strings accept escaped slash-free JSON strings; reject control chars
    # instead of introducing ambiguous Scheme escape syntax.
    text = str(path)
    if any(ord(ch) < 32 for ch in text):
        raise ValueError('control character in sandbox path')
    return json.dumps(text, ensure_ascii=False)

def available(path):
    return Path(path).is_file() and os.access(path, os.X_OK)

def profile_text(read_paths, write_paths, socket_dir, *, dependencies=None, socket_root=None, pty=False):
    reads, writes = sorted(set(map(resolved, read_paths))), sorted(set(map(resolved, write_paths)))
    for path in reads + writes:
        parts = Path(path).parts
        home_root = len(parts) == 3 and parts[1] in ('Users', 'home')
        if home_root or path in ('/', '/Users', '/home', '/tmp', '/private/tmp', '/opt/homebrew', '/usr/local', str(Path.home())) or Path(path).name in ('.local', 'agent-workspace', '.cache', 'Caches', 'cache', 'caches'):
            raise ValueError('broad sandbox allowance refused: ' + path)
    rules = ['(version 1)', '(deny default)', '(import ' + quote(BASE_PROFILE) + ')',
             '(allow process-fork)', '(allow process-exec ' + ' '.join('(subpath %s)' % quote(p) for p in reads) + ')',
             '(deny process-exec ' + ' '.join('(literal %s)' % quote(resolved(p)) for p in FORBIDDEN_EXEC) + ')',
             '(allow file-read* ' + ' '.join('(subpath %s)' % quote(p) for p in reads + writes) + ')',
             '(allow file-write* ' + ' '.join('(subpath %s)' % quote(p) for p in writes) + ')',
             '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
             '(allow sysctl-read)',
             # Signals only to processes inside this same sandbox, which are exactly this tree's own descendants.
             '(allow signal (target same-sandbox))',
             '(allow system-socket (socket-domain AF_UNIX) (socket-domain AF_INET) (socket-domain AF_INET6))',
             # Network policy remains unchanged for IP, not ambient Unix IPC.
             '(allow network-bind (local ip "*:*"))',
             '(allow network-inbound (local ip "*:*"))',
             '(allow network-outbound (remote ip "*:*"))',
             # Unix sockets only below the owned short-path TMPDIR (the attempt root, which contains the tmux socket dir).
             '(allow network-bind (local unix-socket (subpath %s)))' % quote(resolved(socket_root or socket_dir)),
             '(allow network-inbound (local unix-socket (subpath %s)))' % quote(resolved(socket_root or socket_dir)),
             '(allow network-outbound (remote unix-socket (subpath %s)))' % quote(resolved(socket_root or socket_dir))]
    if pty:
        # Only for a separate unprivileged gate identity: its own terminals are the only ones it can open.
        rules += ['(allow file-read* file-write* file-ioctl (literal "/dev/ptmx") (literal "/dev/tty") (regex #"^/dev/ttys[0-9]+$"))']
    dependencies = dependencies or {'files': [], 'metadata_paths': []}
    if dependencies['files']:
        exact = ' '.join('(literal %s)' % quote(resolved(p)) for p in dependencies['files'])
        rules += ['(allow file-read-data file-map-executable ' + exact + ')']
        forbidden = {resolved(p) for p in FORBIDDEN_EXEC}
        executables = sorted({item['executable'] for item in dependencies.get('roots', [])} - forbidden)
        if executables:
            rules += ['(allow process-exec ' + ' '.join('(literal %s)' % quote(resolved(p)) for p in executables) + ')']
    if dependencies['metadata_paths']:
        rules += ['(allow file-read-metadata ' + ' '.join('(literal %s)' % quote(p) for p in dependencies['metadata_paths']) + ')']
    # /bin/ps is setuid root; a sandboxed exec of it is refused. This exact literal runs unsandboxed so the
    # process-listing capabilities work; it takes no file arguments and only prints process information.
    # It must come after the exact dependency exec grants: the last matching rule wins.
    rules += ['(allow process-exec (with no-sandbox) (literal "/bin/ps"))']
    # Keep explicit execution denials final, after imported and exact allow rules.
    rules += ['(deny process-exec ' + ' '.join('(literal %s)' % quote(resolved(p)) for p in FORBIDDEN_EXEC) + ')']
    return '\n'.join(rules) + '\n'

@dataclass
class Context:
    checkout: Path
    venv: Path
    root: Path
    evidence: Path
    env: dict
    tools: dict
    profile_path: Path
    profile_sha256: str
    read_paths: list
    write_paths: list
    socket_dir: Path
    probes: list = field(default_factory=list)
    controls: list = field(default_factory=list)
    passed: bool = False
    execution_mode: str = 'not-run'
    imports: list = field(default_factory=list)
    dependencies: dict = field(default_factory=dict)
    alternates: list = field(default_factory=list)
    @property
    def receipt_path(self):
        return self.evidence / 'sandbox-profile.json'
    def write_receipt(self, status, reason=None):
        body = {'schema': 'pentacle.sandbox-profile.v1', 'status': status, 'reason': reason,
                'profile_path': str(self.profile_path), 'profile_sha256': self.profile_sha256,
                'attempt_root': str(self.root), 'root': str(self.root), 'passed': status == 'PASS', 'resolved_allow_paths': {'read_exec': self.read_paths, 'write': self.write_paths},
                'socket_dir': str(self.socket_dir), 'launcher_argv': [LAUNCHER, '-f', str(self.profile_path)],
                'probes': self.probes, 'controls': self.controls, 'execution_mode': self.execution_mode, 'imported_profiles': self.imports,
                'shared_cache_provided': self.dependencies.get('shared_cache_provided', []),
                'alternate_interpreters': getattr(self, 'alternates', []), 'dependencies': self.dependencies, 'descriptor_policy': {'close_fds': True, 'pass_fds': []},
                'cwd': str(self.root)}
        self.receipt_path.write_text(json.dumps(body, sort_keys=True, indent=2) + '\n')
        return body
    def _probe_wrap(self, argv):
        try:
            valid = (digest(self.profile_path) == self.profile_sha256
                     and all(digest(item['path']) == item['sha256'] for item in self.imports))
        except OSError:
            valid = False
        if not valid:
            self.passed = False
            self.write_receipt('INCOMPLETE', 'profile_changed_or_unreadable')
            raise Incomplete('sandbox profile changed or is unreadable')
        return [LAUNCHER, '-f', str(self.profile_path), *map(str, argv)]
    def validate_cwd(self, cwd):
        path = Path(cwd).resolve()
        allowed = [Path(p) for p in self.read_paths + self.write_paths]
        if not path.is_dir() or not any(path == base or base in path.parents for base in allowed):
            self.passed = False
            self.write_receipt('INCOMPLETE', 'cwd_outside_allowed_roots')
            raise Incomplete('sandbox child cwd is outside allowed roots')
        return path

    def launch_kwargs(self, cwd):
        return {'cwd': str(self.validate_cwd(cwd)), 'close_fds': True, 'pass_fds': ()}

    def wrap(self, argv):
        if not self.passed:
            self.write_receipt('INCOMPLETE', 'self_probe_not_passed')
            raise Incomplete('sandbox self-probe has not passed')
        if self.execution_mode != 'native':
            raise Incomplete('injected test runner cannot authorize subject execution')
        self.proof()
        return self._probe_wrap(argv)
    def proof(self):
        if not self.passed:
            raise Incomplete('current attempt has no passing sandbox self-probe')
        self._probe_wrap([])
        try:
            receipt = json.loads(self.receipt_path.read_text())
            valid = (receipt['status'] == 'PASS' and receipt['profile_sha256'] == self.profile_sha256
                     and receipt['attempt_root'] == str(self.root) and receipt['passed'] is True
                     and receipt['execution_mode'] == self.execution_mode)
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            self.passed = False
            self.write_receipt('INCOMPLETE', 'receipt_identity_mismatch')
            raise Incomplete('sandbox receipt identity mismatch')
        return {'profile_path': str(self.profile_path), 'profile_sha256': self.profile_sha256,
                'attempt_root': str(self.root), 'receipt_path': str(self.receipt_path),
                'receipt_sha256': digest(self.receipt_path), 'verified': True, 'root': str(self.root),
                'passed': True, 'execution_mode': self.execution_mode}

def python_runtime_paths(venv, root, *, executable=None):
    """Discover stdlib and dlopen extensions from the exact interpreter, not logs."""
    code = "import json,sysconfig; print(json.dumps({k:sysconfig.get_path(k) for k in ('stdlib','platstdlib')}))"
    out = subprocess.run([str(executable or (Path(venv)/'bin/python')), '-I', '-S', '-B', '-c', code],
                         cwd=str(root), env={'PATH':'/usr/bin:/bin'}, close_fds=True, pass_fds=(),
                         capture_output=True, text=True, timeout=20, check=True)
    directories = sorted({resolved(p) for p in json.loads(out.stdout).values() if p})
    if not directories or any(not Path(p).is_dir() for p in directories):
        raise deps.DependencyError('unreadable Python runtime directory')
    extensions = set()
    for directory in [*directories, *([str(venv)] if venv is not None else [])]:
        for pattern in ('*.so', '*.dylib'):
            extensions.update(str(p.resolve()) for p in Path(directory).rglob(pattern) if p.is_file())
    return directories, sorted(extensions)


NATIVE_ADDON_DIRS = ('node_modules', 'build/Release', 'prebuilds')

def addon_files(checkout, directories=NATIVE_ADDON_DIRS):
    """Node addons from the checkout's native directories only; the rest of the tree is never walked."""
    found = []
    for name in directories:
        top = Path(checkout) / name
        if not top.is_dir():
            continue
        for current, _dirs, files in os.walk(top):
            found += [str(Path(current, f).resolve()) for f in files if f.endswith('.node')]
    return sorted(set(found))


def separate_gate_identity(uid=None, groups=None, console_uid=None, admin_gid=None):
    """True only for an unprivileged identity that is not root, not the console owner and not in the admin group."""
    try:
        admin_gid = grp.getgrnam('admin').gr_gid if admin_gid is None else admin_gid
        console_uid = os.stat('/dev/console').st_uid if console_uid is None else console_uid
    except (KeyError, OSError):
        return False
    uid = os.getuid() if uid is None else uid
    groups = os.getgroups() if groups is None else groups
    return uid not in (0, console_uid) and admin_gid not in groups


def real_tool_file(name):
    """The real developer-tool file behind an xcrun shim such as /usr/bin/git, derived before the profile exists."""
    out = subprocess.run(['/usr/bin/xcrun', '-f', name], env={'PATH': '/usr/bin:/bin'}, cwd='/', close_fds=True, pass_fds=(),
                         capture_output=True, text=True, timeout=20, check=True)
    path = Path(out.stdout.strip()).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise deps.DependencyError('real tool not found for ' + name)
    return str(path)


def real_interpreter_files(executable, root):
    """Exact real interpreter files behind a launcher stub or shim, derived by running it before the profile exists.

    A framework python3.13 execs Resources/Python.app/Contents/MacOS/Python, and /usr/bin/python3 is an
    xcrun shim whose real interpreter lives under the selected developer directory. Neither is visible to otool."""
    code = ("import json,os,sys;b=os.path.join(sys.base_prefix,'Resources/Python.app/Contents/MacOS/Python');"
            "print(json.dumps([os.path.realpath(sys.executable),os.path.realpath(b) if os.path.isfile(b) else None]))")
    out = subprocess.run([str(executable), '-I', '-S', '-B', '-c', code], cwd=str(root), env={'PATH': '/usr/bin:/bin'},
                         close_fds=True, pass_fds=(), capture_output=True, text=True, timeout=20, check=True)
    files = [p for p in json.loads(out.stdout) if p]
    if not files or any(not Path(p).is_file() for p in files):
        raise deps.DependencyError('real interpreter not found for ' + str(executable))
    return files


def alternate_interpreters(candidates, root):
    """Probe targets: the real interpreter for each candidate (the xcrun shim is never probed itself)."""
    return [real_interpreter_files(candidate, root)[0] for candidate in candidates]


def prepare(checkout, venv, toolchain, root, evidence, env):
    """Generate once. No denial log is read, and no path is learned from probes."""
    evidence = Path(evidence).resolve(); evidence.mkdir(parents=True, exist_ok=True)
    try:
        checkout, venv, root = [Path(p).resolve() for p in (checkout, venv, root)]
        root.mkdir(parents=True, exist_ok=True)
        socket_dir = root / 'tmux-sockets'; socket_dir.mkdir(exist_ok=True)
        tools = {name: resolved(value['path']) for name, value in toolchain.items()}
        imports = deps.import_closure([BASE_PROFILE])
        runtime_reads, extensions = python_runtime_paths(venv, root)
        alternates = alternate_interpreters(ALTERNATE_PYTHONS, root)
        git = real_tool_file('git')
        interpreter_files = [f for e in (str(venv/'bin/python'), *ALTERNATE_PYTHONS) for f in real_interpreter_files(e, root)]
        executables = [str(venv/'bin/python'), *[tools[k] for k in ('python','node','tmux') if k in tools], *alternates, *interpreter_files, git,
                       '/usr/bin/sqlite3', '/bin/sh', LAUNCHER, *EXTRA_BINARIES]  # FORBIDDEN_EXEC is denied, so it gets no dependency grants
        extra = [{'path':p, 'executable':str(venv/'bin/python')} for p in extensions]
        for alternate in alternates:
            alternate_reads, alternate_extensions = python_runtime_paths(None, root, executable=alternate)
            runtime_reads += alternate_reads
            extra += [{'path':p, 'executable':alternate} for p in alternate_extensions]
        extra += [{'path':p, 'executable':tools['node']} for p in addon_files(checkout) if 'node' in tools]
        closure = deps.dependency_closure(executables, extra_roots=extra)
        read_paths = sorted(set(map(resolved, [checkout, venv, *runtime_reads, *SYSTEM_READ])))
        write_paths = sorted(set(map(resolved, [root, evidence, socket_dir])))
        profile = profile_text(read_paths, write_paths, socket_dir, dependencies=closure, socket_root=root, pty=separate_gate_identity())
        profile_path = evidence / 'sandbox-profile.sb'
        profile_path.write_text(profile)
        child_env = dict(env)
        child_env.pop('TMUX', None)
        child_env['TMUX_TMPDIR'] = str(socket_dir)
        # node reads an OpenSSL config at startup; an empty owned file avoids granting the system one.
        (root / 'empty-openssl.cnf').write_text('')
        child_env['OPENSSL_CONF'] = str(root / 'empty-openssl.cnf')
        # Real binaries first on PATH: exact symlinks, not xcrun shims (which load an unreadable developer directory).
        bin_dir = root / 'bin'; bin_dir.mkdir(exist_ok=True)
        for name, target in (('git', git), ('python3', str(venv / 'bin/python'))):
            link = bin_dir / name
            if link.is_symlink() or link.exists(): link.unlink()
            link.symlink_to(target)
        child_env['PATH'] = os.pathsep.join([str(bin_dir), child_env.get('PATH', '/usr/bin:/bin')])
        # git must not read a user or system config outside the allowed roots (that access is denied, and git treats it as fatal).
        child_env['GIT_CONFIG_GLOBAL'] = os.devnull
        child_env['GIT_CONFIG_NOSYSTEM'] = '1'
        context = Context(checkout, venv, root, evidence, child_env, tools, profile_path,
                          digest(profile_path), read_paths, write_paths, socket_dir,
                          imports=imports, dependencies=closure)
        context.alternates = alternates
        context.write_receipt('INCOMPLETE', 'self_probe_not_run')
        return context
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, deps.DependencyError) as exc:
        (evidence / 'sandbox-profile.json').write_text(json.dumps({'schema': 'pentacle.sandbox-profile.v1',
            'status': 'INCOMPLETE', 'reason': 'profile_generation_failed', 'error': type(exc).__name__,
            'probes': [], 'controls': [], 'launcher_argv': [LAUNCHER]}, sort_keys=True) + '\n')
        raise Incomplete('sandbox profile generation failed: ' + str(exc)) from exc

PY_OPEN = """import sys
try:
 with open(sys.argv[1], 'rb') as f: f.read()
 print('READABLE')
except PermissionError:
 print('DENIED')
"""
NODE_OPEN = """try {require('fs').readFileSync(process.argv[1]); console.log('READABLE')}
catch(e) {if (e.code === 'EACCES' || e.code === 'EPERM') console.log('DENIED'); else throw e}
"""
SH_OPEN = 'if /bin/cat "$1" > "$2"; then printf "READABLE\\n"; else printf "DENIED\\n"; fi'
OWNED_CONTROL = """import sqlite3,sys,subprocess
from pathlib import Path
p=Path(sys.argv[1]); c=sqlite3.connect(p); c.execute('create table t(v)'); c.execute('insert into t values(7)'); c.commit(); c.close()
assert sqlite3.connect(p).execute('select v from t').fetchone()==(7,)
code="import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); assert c.execute('select v from t').fetchone()==(7,); c.close(); c=sqlite3.connect(sys.argv[2]); c.execute('create table child(v)'); c.close()"
subprocess.run([sys.executable,'-I','-B','-c',code,str(p),str(p)+'.child'],check=True)
assert sqlite3.connect(str(p)+'.child').execute('select name from sqlite_master').fetchone()==('child',)
print('CONTROL_OK')
"""

def run_probes(context, run=subprocess.run):
    """Real launcher by default; injected unit runners are explicitly not proof.

    Caller runs this before activating its Python policy. Every opener has an
    owned positive control and two outside negative controls, all using the
    exact same generated profile. Never run a merge gate here.
    """
    c = context
    c.passed = False
    c.execution_mode = 'native' if run is subprocess.run else 'injected-test-runner'
    c.probes.clear(); c.controls.clear()
    failures = []
    sentinel_folders = []
    final_reason = None
    def failure(reason):
        failures.append(reason)
    def invoke(name, argv, expected, *, control=False, sandboxed=True):
        entry = {'name': name, 'expected': expected, 'sandboxed': sandboxed, 'argv': list(map(str, argv))}
        dest = c.controls if control else c.probes
        dest.append(entry)
        try:
            if sandboxed: c.validate_cwd(c.root)
            command = c._probe_wrap(argv) if sandboxed else list(map(str, argv))
            out = run(command, env=c.env, capture_output=True, text=True, timeout=20, **c.launch_kwargs(c.root))
            stdout, stderr = out.stdout or '', out.stderr or ''
            entry.update(returncode=out.returncode, stdout=stdout[-2048:], stderr=stderr[-2048:])
            if expected == 'DENIED_SQLITE':
                ok = out.returncode != 0 and any(word in stderr.lower() for word in ('authorization denied', 'permission denied', 'operation not permitted', 'unable to open database'))
            elif expected == 'DENIED_EXEC':
                ok = out.returncode != 0 and any(word in stderr.lower() for word in ('operation not permitted', 'permission denied', 'authorization denied'))
            elif expected == 'CLEANED':
                # A probe pane that finishes ends its owned server; a stale socket then reports no server.
                ok = out.returncode == 0 or 'no server running' in stderr.lower()
            elif expected == 'VERSION':
                ok = out.returncode == 0 and bool((stdout + stderr).strip())
            else:
                ok = out.returncode == 0 and stdout.strip() == expected
            entry['passed'] = ok
            if not ok: failure(name)
        except (OSError, subprocess.SubprocessError, Incomplete) as exc:
            entry.update(passed=False, error=type(exc).__name__)
            failure(name)
        return entry
    try:
        if not available(LAUNCHER):
            raise Incomplete('sandbox-exec missing or not executable')
        # An inherited active Python hook would measure Python policy rather
        # than Seatbelt, especially for alternate/-S/native opener probes.
        for path in c.venv.glob('lib/python*/site-packages/pentacle-sqlite-attempt.json'):
            if json.loads(path.read_text()).get('active') is not False:
                raise Incomplete('Python guard must be inactive during sandbox self-probe')
        c._probe_wrap([])
        python = str(c.venv / 'bin/python')
        required = [python, *c.alternates, '/usr/bin/sqlite3', '/bin/sh', c.tools.get('node', ''), c.tools.get('tmux', ''), *FORBIDDEN_EXEC]
        missing = [path for path in required if not path or not available(path)]
        if missing:
            c.controls.append({'name': 'required_openers', 'passed': False, 'missing': missing})
            raise Incomplete('required probe executable unavailable')
        # Only these harness-created databases are opened outside the root.
        # They contain no personal data and stand in for forbidden locations.
        import sqlite3
        from contextlib import closing
        sentinels = []
        for name in ('home-equivalent', 'unowned-temp', 'protected-credential', 'protected-database'):
            parent = Path.home() if name.startswith('protected-') else c.root.parent
            folder = Path(tempfile.mkdtemp(prefix='sandbox-' + name + '-', dir=str(parent))).resolve()
            sentinel_folders.append(folder)
            if any(Path(allowed) == folder or Path(allowed) in folder.parents for allowed in c.read_paths + c.write_paths):
                raise Incomplete('forbidden sentinel would fall inside an allow rule')
            filename = {'protected-credential':'credentials.json', 'protected-database':'sessions.db'}.get(name, 'SENTINEL.sqlite')
            path = folder / filename
            with closing(sqlite3.connect(path)) as db:
                db.execute('create table sentinel(value)'); db.execute("insert into sentinel values('READABLE')"); db.commit()
            sentinels.append((name, path))
        owned = c.root / 'owned-SENTINEL.sqlite'
        with closing(sqlite3.connect(owned)) as db:
            db.execute('create table sentinel(value)'); db.execute("insert into sentinel values('READABLE')"); db.commit()
        for name, path in sentinels:
            invoke('unsandboxed-' + name, [python, '-I', '-B', '-c', PY_OPEN, str(path)], 'READABLE', control=True, sandboxed=False)
        invoke('owned-parent-child-create-reopen', [python, '-I', '-B', '-c', OWNED_CONTROL, str(c.root/'owned-control.sqlite')], 'CONTROL_OK', control=True)
        for name, argv in [('python',[python,'--version']), ('node',[c.tools['node'],'--version']), ('tmux',[c.tools['tmux'],'-V'])]:
            invoke('tool-' + name, argv, 'VERSION', control=True)
        openers = [('venv-python', [python,'-c',PY_OPEN]), ('venv-isolated', [python,'-I','-B','-c',PY_OPEN])]
        for index, alternate in enumerate(c.alternates):
            openers += [('alternate-%d'%index,[alternate,'-c',PY_OPEN]), ('alternate-%d-no-site'%index,[alternate,'-S','-c',PY_OPEN])]
        openers += [('node', [c.tools['node'],'-e',NODE_OPEN])]
        targets = [('owned', owned), *sentinels]
        for opener, argv in openers:
            for name, path in targets:
                invoke(opener+'-'+name, [*argv,str(path)], 'READABLE' if name=='owned' else 'DENIED', control=name=='owned')
        for name, path in targets:
            invoke('sqlite3-'+name, ['/usr/bin/sqlite3',str(path),'select value from sentinel;'],
                   'READABLE' if name=='owned' else 'DENIED_SQLITE', control=name=='owned')
            invoke('shell-'+name, ['/bin/sh','-c',SH_OPEN,'probe',str(path),str(c.root/('shell-copy-'+name))],
                   'READABLE' if name=='owned' else 'DENIED', control=name=='owned')
        socket = c.socket_dir / 'probe.sock'
        try:
            for index,(name,path) in enumerate(targets):
                result, error = c.root/('tmux-result-'+name), c.root/('tmux-error-'+name)
                script = SH_OPEN + ' > "$3" 2> "$4"'
                pane = shlex.join(['/bin/sh','-c',script,'probe',str(path),str(c.root/('tmux-copy-'+name)),str(result),str(error)])
                entry = invoke('tmux-start-'+name, [c.tools['tmux'],'-S',str(socket),'-f','/dev/null','new-session','-d','-s','probe'+str(index),pane], '', control=True)
                deadline = time.monotonic()+10
                while entry.get('passed') and not result.exists() and time.monotonic()<deadline:
                    time.sleep(.05)
                value = result.read_text().strip() if result.exists() else None
                expected = 'READABLE' if name=='owned' else 'DENIED'
                row = {'name':'tmux-pane-'+name,'expected':expected,'stdout':value,'passed':value==expected,'sandboxed':True}
                (c.controls if name=='owned' else c.probes).append(row)
                if not row['passed']: failure(row['name'])
        finally:
            # It is a NEW dedicated server on one exact owned socket. Never
            # address the default socket or an ambient TMUX session.
            if socket.exists():
                invoke('owned-tmux-cleanup',[c.tools['tmux'],'-S',str(socket),'kill-server'],'CLEANED',control=True)
        for executable in FORBIDDEN_EXEC:
            args = [executable]
            if executable.endswith('sudo'): args.append('-V')
            elif executable.endswith('su'): args.append('-h')
            elif executable.endswith('launchctl'): args.append('version')
            elif executable.endswith('osascript'): args.extend(['-e','return 0'])
            invoke('forbidden-exec-'+Path(executable).name,args,'DENIED_EXEC')
        c._probe_wrap([])
        if failures:
            raise Incomplete('self-probe failures: ' + ', '.join(failures))
        c.passed = True
    except Exception as exc:
        c.passed = False
        final_reason = str(exc)
        c.write_receipt('INCOMPLETE', final_reason)
        raise Incomplete(str(exc)) from exc
    finally:
        cleanup_failures = []
        for folder in sentinel_folders:
            row = {'name': 'sentinel-cleanup', 'path': str(folder), 'passed': False}
            c.controls.append(row)
            try:
                shutil.rmtree(folder)
                row['passed'] = True
            except OSError as exc:
                cleanup_failures.append(type(exc).__name__)
                row['error'] = type(exc).__name__
        if cleanup_failures:
            c.passed = False
            c.write_receipt('INCOMPLETE', 'owned sentinel cleanup failed: ' + ', '.join(cleanup_failures))
            raise Incomplete('owned sentinel cleanup failed')
        if not c.passed:
            c.write_receipt('INCOMPLETE', final_reason)
    c.write_receipt('PASS')
    return c.proof()
