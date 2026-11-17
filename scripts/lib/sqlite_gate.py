"""Prepared-venv SQLite guard and conservative child-launch envelope.

Not a sandbox against malicious Python; fail closed on child runtimes that
cannot inherit this hook. Configuration is a file beside this module, never
an environment variable (isolated Python ignores PYTHON*).
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

CONFIG = 'pentacle-sqlite-attempt.json'
HOOK = '00_pentacle_sqlite_guard.pth'
BOOT = '_pentacle_sqlite_gate.py'
_booted = False


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def assert_inactive(venv):
    """Refuse stale activation BEFORE running even a dependency probe."""
    for path in Path(venv).glob('lib/python*/site-packages/' + CONFIG):
        if json.loads(path.read_text()).get('active') is not False:
            raise RuntimeError('previous SQLite guard activation remains; resolve descendants before reuse')


def prepare(venv, guard):
    """Called while the caller holds the prepared-environment lock."""
    sites = list((Path(venv) / 'lib').glob('python*/site-packages'))
    if len(sites) != 1:
        raise RuntimeError('expected one owned venv site-packages directory')
    site = sites[0]
    (site / BOOT).write_bytes(Path(__file__).read_bytes())
    boot_code = ("try:\n import importlib.util, sys\n"
                 " name = '_pentacle_sqlite_gate'\n"
                 " module = sys.modules.get(name)\n"
                 " if module is None:\n"
                 "  spec = importlib.util.spec_from_file_location(name, " + repr(str(site / BOOT)) + ")\n"
                 "  module = importlib.util.module_from_spec(spec)\n"
                 "  sys.modules[name] = module\n"
                 "  spec.loader.exec_module(module)\n"
                 " module.boot()\nexcept BaseException:\n os._exit(86)")
    (site / HOOK).write_text("import os; exec(" + repr(boot_code) + ")\n")
    (site / CONFIG).write_text(json.dumps({'active': False}))
    return site


def activate(site, venv, root, guard):
    root = Path(root).resolve(strict=True)
    config = {'active': True, 'root': str(root), 'guard': str(Path(guard).resolve()),
              'guard_sha256': digest(guard), 'venv': str(Path(venv).resolve()),
              'hook_path': str(Path(site) / HOOK)}
    (Path(site) / CONFIG).write_text(json.dumps(config, sort_keys=True))
    return config


def deactivate(site):
    (Path(site) / CONFIG).write_text(json.dumps({'active': False}))


def receipt(config, evidence):
    root = Path(config['root'])
    registry, refused = root / '.sqlite-created.jsonl', root / '.sqlite-refusals.jsonl'
    report = dict(config, registry_sha256=digest(registry) if registry.exists() else hashlib.sha256(b'').hexdigest(),
                  refusal_count=len(refused.read_text().splitlines()) if refused.exists() else 0)
    evidence = Path(evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    for source in (registry, refused):
        if source.exists():
            shutil.copyfile(source, evidence / source.name.lstrip('.'))
    (evidence / 'sqlite-guard.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return report


def boot():
    """A .pth error normally only warns; abort interpreter on ANY hook error."""
    global _booted
    if _booted:
        return
    try:
        config = json.loads(Path(__file__).with_name(CONFIG).read_text())
        if not config['active']:
            return
        if digest(config['guard']) != config['guard_sha256']:
            raise RuntimeError('SQLite guard source drift')
        spec = importlib.util.spec_from_file_location('_pentacle_attempt_sqlite_guard', config['guard'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._install_guard(Path(config['root']), attempt=True)
        install_launch_guard(config)
        _booted = True
    except BaseException as exc:
        sys.stderr.write('SQLite guard startup failed: %s\n' % type(exc).__name__)
        sys.stderr.flush()
        os._exit(86)


def install_launch_guard(config):
    root = Path(config['root'])
    venv_bin = Path(config['venv']) / 'bin'
    python_real = os.path.realpath(venv_bin / 'python')

    def refuse(reason):
        row = json.dumps({'pid': os.getpid(), 'reason': reason}) + '\n'
        fd = os.open(root / '.sqlite-refusals.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, row.encode())
        finally:
            os.close(fd)
        raise PermissionError('SQLite guard refused child: ' + reason)

    def check(executable, argv, env):
        if not isinstance(argv, (list, tuple)) or not argv:
            refuse('unresolved child argv')
        executable = os.fsdecode(executable)
        environment = os.environ if env is None else env
        path = shutil.which(executable, path=environment.get('PATH')) if '/' not in executable else os.path.abspath(executable)
        if not path:
            refuse('unresolved child executable')
        p = Path(path)
        # Do not collapse venv/bin symlinks: interpreter prefix determines hook.
        if p.parent == venv_bin and os.path.realpath(p) == python_real:
            if environment.get('PYTHONHOME'):
                refuse('PYTHONHOME redirects the guarded interpreter')
            argv0 = os.fsdecode(argv[0])
            argv0_path = shutil.which(argv0, path=environment.get('PATH')) if '/' not in argv0 else os.path.abspath(argv0)
            if argv0_path != str(p):
                refuse('Python argv0 differs from guarded executable')
            skip_option_value = False
            for arg in argv[1:]:
                arg = os.fsdecode(arg)
                if skip_option_value:
                    skip_option_value = False
                    continue
                if arg in ('-X', '-W', '--check-hash-based-pycs'):
                    skip_option_value = True
                    continue
                if arg in ('-c', '-m', '-') or not arg.startswith('-'):
                    break
                if arg == '--' or arg.startswith('--'):
                    continue
                if 'S' in arg[1:]:
                    refuse('python -S cannot load the venv hook')
            return
        # Pure observation only. No shell, executable scripts, git hooks,
        # tmux server, node, sqlite3, other interpreter, or opaque native tool.
        allowed = {'ps', 'lsof', 'uname'}
        if p.name in allowed and str(p) in ('/bin/' + p.name, '/usr/bin/' + p.name):
            return
        if p.name == 'git' and str(p) in ('/bin/git', '/usr/bin/git'):
            args = [os.fsdecode(a) for a in argv[1:]]
            if args and args[0] in ('rev-parse',):
                return
        refuse('uncovered runtime: ' + str(p))

    def audit(event, args):
        if event == 'subprocess.Popen':
            check(args[0], args[1], args[3])
        elif event in ('os.exec', 'os.posix_spawn'):
            check(args[0], args[1], args[2])
        elif event == 'os.system':
            refuse('shell command cannot inherit the SQLite hook')
    sys.addaudithook(audit)
