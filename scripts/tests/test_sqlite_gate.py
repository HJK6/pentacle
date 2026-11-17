"""Real owned-venv startup, not env injection or a mocked interpreter."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('sqlite_gate', REPO / 'scripts/lib/sqlite_gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

@pytest.fixture
def context(tmp_path):
    venv = tmp_path / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
    root = tmp_path / 'attempt'; root.mkdir()
    sentinel = root / 'SENTINEL'; sentinel.write_bytes(b'SENTINEL')
    (tmp_path / 'OUTSIDE_SENTINEL').write_bytes(b'SENTINEL')
    site = gate.prepare(venv, REPO / 'services/chat-stream-v2/tools/sqlite_guard.py')
    config = gate.activate(site, venv, root, REPO / 'services/chat-stream-v2/tools/sqlite_guard.py')
    return venv, root, site, config

@pytest.mark.parametrize('flags', [[], ['-I', '-B']])
def test_startup_and_child_survive_env_clear_and_cwd(context, tmp_path, flags):
    venv, root, site, config = context
    code = '''import os, sqlite3, sys, subprocess
from pathlib import Path
root=Path(sys.argv[1])
a=sqlite3.connect(root/'fresh'); a.close()
sqlite3.connect(root/'fresh').close()
child = """import sqlite3,sys
from pathlib import Path
root=Path(sys.argv[1]); new=root/sys.argv[2]
sqlite3.connect(new).close()
sqlite3.connect(new).close()
sqlite3.connect(root/'fresh').close()
for forbidden in (root/'SENTINEL',root.parent/'OUTSIDE_SENTINEL'):
 try: sqlite3.connect(forbidden)
 except sqlite3.Error: pass
 else: raise AssertionError('child forbidden open accepted')
"""
for i,flags in enumerate(([],['-I','-B'])):
 name='child-'+str(i)
 r=subprocess.run([sys.executable,*flags,'-c',child,str(root),name],env={},cwd='/')
 assert r.returncode == 0
 sqlite3.connect(root/name).close()
try: sqlite3.connect(root/'SENTINEL')
except sqlite3.Error: pass
else: raise AssertionError('preexisting accepted')
'''
    out = subprocess.run([str(venv/'bin/python'), *flags, '-c', code, str(root)], env={}, cwd='/', capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert gate.receipt(config, tmp_path/'evidence')['refusal_count'] >= 5

@pytest.mark.parametrize('command', [['-S'], ['-I', '-S'], ['-X', 'dev', '-S']])
def test_python_without_site_refused(context, command):
    venv, root, site, config = context
    code = 'import subprocess,sys; subprocess.run([sys.executable]+%r+["-c", "pass"])' % command
    out = subprocess.run([str(venv/'bin/python'), '-I', '-c', code], capture_output=True, text=True)
    assert out.returncode != 0
    assert 'cannot load the venv hook' in out.stderr

@pytest.mark.parametrize('command', [['/bin/sh', '-c', 'true'], ['/usr/bin/python3', '-c', 'pass']])
def test_uncovered_runtime_refused(context, command):
    venv, root, site, config = context
    code = 'import subprocess; subprocess.run(%r)' % command
    out = subprocess.run([str(venv/'bin/python'), '-I', '-c', code], capture_output=True, text=True)
    assert out.returncode != 0
    assert 'uncovered runtime' in out.stderr

@pytest.mark.parametrize('damage', ['config', 'bootstrap'])
def test_hook_failure_exits_not_warning(context, damage):
    venv, root, site, config = context
    if damage == 'config': (site/gate.CONFIG).write_text('{')
    else: (site/gate.BOOT).unlink()
    out = subprocess.run([str(venv/'bin/python'), '-I', '-c', 'raise AssertionError("unguarded execution")'], capture_output=True, text=True)
    assert out.returncode == 86, out.stderr

@pytest.mark.parametrize('escape', ['argv0', 'home'])
def test_child_prefix_redirection_refused(context, escape):
    venv, root, site, config = context
    code = '''import subprocess,sys,os
if sys.argv[1] == 'argv0':
 subprocess.run(['/usr/bin/python3','-c','pass'], executable=sys.executable)
else:
 subprocess.run([sys.executable,'-c','pass'],env=dict(os.environ,PYTHONHOME='/usr'))
'''
    out = subprocess.run([str(venv/'bin/python'), '-I', '-c', code, escape], capture_output=True, text=True)
    assert out.returncode != 0
    assert ('argv0 differs' if escape == 'argv0' else 'PYTHONHOME redirects') in out.stderr

def test_bootstrap_not_shadowed_by_pythonpath(context, tmp_path):
    venv, root, site, config = context
    shadow = tmp_path/'shadow'; shadow.mkdir()
    (shadow/gate.BOOT).write_text('raise AssertionError("shadow bootstrap ran")')
    out = subprocess.run([str(venv/'bin/python'), '-c', 'import sqlite3,sys; sqlite3.connect(sys.argv[1])', str(root/'SENTINEL')],
                         env={'PYTHONPATH': str(shadow)}, capture_output=True, text=True)
    assert out.returncode != 0
    assert 'shadow bootstrap ran' not in out.stderr
    assert 'read timing refused' in out.stderr

def test_stale_active_configuration_refuses_before_reuse(context):
    venv, root, site, config = context
    with pytest.raises(RuntimeError, match='previous SQLite guard activation'):
        gate.assert_inactive(venv)
    gate.deactivate(site)
    gate.assert_inactive(venv)


def test_native_relaxation_requires_current_profile_proof(tmp_path):
    # RED: c21a has no proof-bounded native-launch permission API.
    profile = tmp_path / 'profile.sb'; profile.write_text('(version 1)\n(deny default)\n')
    receipt = tmp_path / 'sandbox-profile.json'
    proof = {'profile_path': str(profile), 'profile_sha256': gate.digest(profile),
             'root': str(tmp_path), 'passed': True, 'execution_mode': 'native'}
    receipt.write_text(json.dumps(proof))
    proof['receipt_path'] = str(receipt)
    proof['receipt_sha256'] = gate.digest(receipt)
    assert gate.sandbox_proof_valid(proof, tmp_path)
    assert not gate.sandbox_proof_valid(dict(proof, execution_mode="injected-test-runner"), tmp_path)
    profile.write_text('(version 1)\n(allow default)\n')
    assert not gate.sandbox_proof_valid(proof, tmp_path)
    assert not gate.sandbox_proof_valid(None, tmp_path)


@pytest.mark.parametrize('operation,allowed', [('native', True), ('no_site', False), ('other_python', False), ('system', False), ('drift', False), ('aliased_python', False)])
def test_proven_native_permission_does_not_relax_python_policy(context, tmp_path, operation, allowed):
    venv, root, site, config = context
    profile=tmp_path/'profile.sb'; profile.write_text('(version 1)\n(deny default)\n')
    receipt=tmp_path/'sandbox-profile.json'
    proof={'root': str(root), 'profile_path': str(profile), 'profile_sha256': gate.digest(profile), 'passed': True, 'execution_mode': 'native'}
    receipt.write_text(json.dumps(proof))
    proof.update(receipt_path=str(receipt), receipt_sha256=gate.digest(receipt))
    active = gate.activate(site, venv, root, REPO/'services/chat-stream-v2/tools/sqlite_guard.py', sandbox_proof=proof)
    assert 'sandbox_proof' not in gate.receipt(active, tmp_path/'sqlite-evidence')
    if operation == 'drift': profile.write_text('changed')
    (root/'alternate-helper').symlink_to('/usr/bin/python3')
    code='''import subprocess,sys,os
op=sys.argv[1]
if op in ('native','drift'): subprocess.run(['/bin/sh','-c','true'],check=True)
elif op == 'no_site': subprocess.run([sys.executable,'-S','-c','pass'],check=True)
elif op == 'other_python': subprocess.run(['/usr/bin/python3','-c','pass'],check=True)
elif op == 'aliased_python': subprocess.run([sys.argv[2],'-c','pass'],check=True)
else: os.system('true')
'''
    out=subprocess.run([str(venv/'bin/python'),'-I','-c',code,operation,str(root/'alternate-helper')],capture_output=True,text=True)
    assert (out.returncode == 0) is allowed, out.stderr
