"""Exact-profile Seatbelt preflight. Stub tests never constitute OS proof."""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

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

def profile_text(read_paths, write_paths, socket_dir):
    reads, writes = sorted(set(map(resolved, read_paths))), sorted(set(map(resolved, write_paths)))
    for path in reads + writes:
        parts = Path(path).parts
        home_root = len(parts) == 3 and parts[1] in ('Users', 'home')
        if home_root or path in ('/', '/Users', '/home', '/tmp', '/private/tmp', '/opt/homebrew', '/usr/local', str(Path.home())) or Path(path).name in ('.local', 'agent-workspace', '.cache', 'Caches', 'cache', 'caches'):
            raise ValueError('broad sandbox allowance refused: ' + path)
    rules = ['(version 1)', '(deny default)',
             '(allow process-fork)', '(allow process-exec ' + ' '.join('(subpath %s)' % quote(p) for p in reads) + ')',
             '(deny process-exec ' + ' '.join('(literal %s)' % quote(resolved(p)) for p in FORBIDDEN_EXEC) + ')',
             '(allow file-read* ' + ' '.join('(subpath %s)' % quote(p) for p in reads + writes) + ')',
             '(allow file-write* ' + ' '.join('(subpath %s)' % quote(p) for p in writes) + ')',
             '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
             '(allow sysctl-read)', '(allow signal (target self))',
             '(allow system-socket (socket-domain AF_UNIX) (socket-domain AF_INET) (socket-domain AF_INET6))',
             # Network policy remains unchanged for IP, not ambient Unix IPC.
             '(allow network-bind (local ip "*:*"))',
             '(allow network-inbound (local ip "*:*"))',
             '(allow network-outbound (remote ip "*:*"))',
             '(allow network-bind (local unix-socket (subpath %s)))' % quote(resolved(socket_dir)),
             '(allow network-inbound (local unix-socket (subpath %s)))' % quote(resolved(socket_dir)),
             '(allow network-outbound (remote unix-socket (subpath %s)))' % quote(resolved(socket_dir))]
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
    @property
    def receipt_path(self):
        return self.evidence / 'sandbox-profile.json'
    def write_receipt(self, status, reason=None):
        body = {'schema': 'pentacle.sandbox-profile.v1', 'status': status, 'reason': reason,
                'profile_path': str(self.profile_path), 'profile_sha256': self.profile_sha256,
                'attempt_root': str(self.root), 'root': str(self.root), 'passed': status == 'PASS', 'resolved_allow_paths': {'read_exec': self.read_paths, 'write': self.write_paths},
                'socket_dir': str(self.socket_dir), 'launcher_argv': [LAUNCHER, '-f', str(self.profile_path)],
                'probes': self.probes, 'controls': self.controls, 'execution_mode': self.execution_mode}
        self.receipt_path.write_text(json.dumps(body, sort_keys=True, indent=2) + '\n')
        return body
    def _probe_wrap(self, argv):
        try:
            valid = digest(self.profile_path) == self.profile_sha256
        except OSError:
            valid = False
        if not valid:
            self.passed = False
            self.write_receipt('INCOMPLETE', 'profile_changed_or_unreadable')
            raise Incomplete('sandbox profile changed or is unreadable')
        return [LAUNCHER, '-f', str(self.profile_path), *map(str, argv)]
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

def prepare(checkout, venv, toolchain, root, evidence, env):
    """Generate once. No denial log is read, and no path is learned from probes."""
    evidence = Path(evidence).resolve(); evidence.mkdir(parents=True, exist_ok=True)
    try:
        checkout, venv, root = [Path(p).resolve() for p in (checkout, venv, root)]
        root.mkdir(parents=True, exist_ok=True)
        socket_dir = root / 'tmux-sockets'; socket_dir.mkdir(exist_ok=True)
        tools = {name: resolved(value['path']) for name, value in toolchain.items()}
        # Exact package roots for resolved Homebrew/venv tools, never /opt/homebrew.
        packages = []
        for path in tools.values():
            p = Path(path)
            package = p.parent.parent if p.parent.name in ('bin', 'sbin') else p.parent
            if str(package) not in ('/', '/usr'):
                packages.append(str(package))
        read_paths = sorted(set(map(resolved, [checkout, venv, *packages, *SYSTEM_READ])))
        write_paths = sorted(set(map(resolved, [root, evidence, socket_dir])))
        profile = profile_text(read_paths, write_paths, socket_dir)
        profile_path = evidence / 'sandbox-profile.sb'
        profile_path.write_text(profile)
        child_env = dict(env)
        child_env.pop('TMUX', None)
        child_env['TMUX_TMPDIR'] = str(socket_dir)
        context = Context(checkout, venv, root, evidence, child_env, tools, profile_path,
                          digest(profile_path), read_paths, write_paths, socket_dir)
        context.write_receipt('INCOMPLETE', 'self_probe_not_run')
        return context
    except (OSError, ValueError, KeyError, TypeError) as exc:
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
            command = c._probe_wrap(argv) if sandboxed else list(map(str, argv))
            out = run(command, env=c.env, cwd=str(c.root), capture_output=True, text=True, timeout=20)
            stdout, stderr = out.stdout or '', out.stderr or ''
            entry.update(returncode=out.returncode, stdout=stdout[-2048:], stderr=stderr[-2048:])
            if expected == 'DENIED_SQLITE':
                ok = out.returncode != 0 and any(word in stderr.lower() for word in ('authorization denied', 'permission denied', 'operation not permitted', 'unable to open database'))
            elif expected == 'DENIED_EXEC':
                ok = out.returncode != 0 and any(word in stderr.lower() for word in ('operation not permitted', 'permission denied', 'authorization denied'))
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
        required = [python, *ALTERNATE_PYTHONS, '/usr/bin/sqlite3', '/bin/sh', c.tools.get('node', ''), c.tools.get('tmux', ''), *FORBIDDEN_EXEC]
        missing = [path for path in required if not path or not available(path)]
        if missing:
            c.controls.append({'name': 'required_openers', 'passed': False, 'missing': missing})
            raise Incomplete('required probe executable unavailable')
        # Only these harness-created databases are opened outside the root.
        # They contain no personal data and stand in for forbidden locations.
        import sqlite3
        from contextlib import closing
        sentinels = []
        for name in ('home-equivalent', 'unowned-temp'):
            folder = Path(tempfile.mkdtemp(prefix='sandbox-' + name + '-', dir=str(c.root.parent))).resolve()
            sentinel_folders.append(folder)
            if any(Path(allowed) == folder or Path(allowed) in folder.parents for allowed in c.read_paths + c.write_paths):
                raise Incomplete('forbidden sentinel would fall inside an allow rule')
            path = folder / 'SENTINEL.sqlite'
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
        for index, alternate in enumerate(ALTERNATE_PYTHONS):
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
                invoke('owned-tmux-cleanup',[c.tools['tmux'],'-S',str(socket),'kill-server'],'',control=True)
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
