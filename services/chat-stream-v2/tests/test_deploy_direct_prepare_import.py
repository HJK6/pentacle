"""The documented direct entry must import smoke helpers without pytest paths."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from .test_deploy_gate_evidence import read_timing_receipt


# Execute the real script/main/intent validation, stopping at the first adapter
# call before staging. The child never receives conftest's tools/ sys.path.
PROBE = r'''
import json, os, runpy, sys
from pathlib import Path
script, scratch, intent, receipt, repo, timing = map(Path, sys.argv[1:])
scratch = scratch.resolve()
denials = []
def guard(event, args):
    if event.startswith(('socket.', 'subprocess.')) or event in (
            'os.system', 'os.exec', 'os.posix_spawn', 'os.kill', 'os.killpg'):
        denials.append(event)
        raise PermissionError('external effect denied: ' + event)
    if event == 'open':
        name, mode, flags = args
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
        if writing and not isinstance(name, int) and not Path(name).resolve().is_relative_to(scratch):
            denials.append('external-write')
            raise PermissionError('write outside fixture denied')
sys.addaudithook(guard)
class ReachedPrepare(BaseException): pass
def boundary(frame, event, arg):
    if (event == 'call' and frame.f_code.co_name == 'prepare'
            and frame.f_code.co_filename == str(script.parent / 'coordinated_runtime.py')):
        sys.setprofile(None)
        assert not denials, denials
        print(json.dumps({'reached': 'prepare-adapter',
                          'accounts': frame.f_locals['intent'].get('smoke_accounts'),
                          'denied_effects': denials}))
        raise ReachedPrepare()
sys.setprofile(boundary)
# Match python /absolute/deploy/deploy.py, without adding service/tools paths.
sys.path.insert(0, str(script.parent))
sys.argv = [str(script), 'chat-streamd-v2', '--release-checkout', str(repo),
            '--coordinated-plan', str(intent), '--coordinated-receipt', str(receipt),
            '--prepare-only', '--read-timing-receipt', str(timing)]
try:
    runpy.run_path(str(script), run_name='__main__')
except ReachedPrepare:
    pass
else:
    raise AssertionError('direct prepare never reached the guarded adapter boundary')
'''


@pytest.mark.parametrize('accounts', [None, ['codex=spare', 'claude=primary']],
                         ids=['omitted', 'explicit'])
def test_direct_prepare_imports_without_tools_path(tmp_path, accounts):
    service = Path(__file__).resolve().parents[1]
    def private_json(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return str(path)
    machines = private_json('machines.json', {'machines': [{'name': 'node-a'}]})
    policy = private_json('policy.json', {})
    intent = {'schema': 'pentacle.coordinated-deploy.intent.v1',
              'transaction_id': 'direct-import-fixture',
              'controller_sha': 'a' * 40, 'target_sha': 'b' * 40, 'prior_sha': 'c' * 40,
              'machines_file': machines, 'policy_file': policy, 'excluded_hosts': [],
              'gate_evidence': str(tmp_path / 'target-gate.json'),
              'prior_gate_evidence': str(tmp_path / 'prior-gate.json')}
    if accounts is not None:
        intent['smoke_accounts'] = accounts
    intent_path = private_json('intent.json', intent)
    timing = private_json('read-timing.json', read_timing_receipt('b' * 40))
    result = subprocess.run([sys.executable, '-I', '-B', '-c', PROBE,
        str(service / 'deploy/deploy.py'), str(tmp_path), intent_path,
        str(tmp_path / 'receipt.json'), str(tmp_path / 'runtime'), timing],
        cwd=tmp_path, env={'PATH': os.defpath, 'HOME': str(tmp_path),
                          'TMPDIR': str(tmp_path)},
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {'reached': 'prepare-adapter',
                                         'accounts': accounts, 'denied_effects': []}
