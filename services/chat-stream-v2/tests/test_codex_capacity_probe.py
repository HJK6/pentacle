"""The metadata-only native frontier probe against owned inert processes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from codex_capacity_probe import probe, program


@pytest.mark.parametrize('variant', ['valid', 'satellite_anchor', 'newer_record', 'partial_tail', 'ambiguous', 'wrong_turn', 'wrong_session', 'changed_file'])
def test_native_frontier_is_exact_and_rejects_ambiguity(tmp_path, variant):
    folder = tmp_path/'.codex/sessions/2026/10/17'
    folder.mkdir(parents=True)
    path = folder/'rollout-fixture.jsonl'
    header = {'type': 'session_meta', 'payload': {'id': 'fixture-native'}}
    terminal = {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'fixture-turn',
                'error': {'codex_error_info': 'server_overloaded', 'message': 'private-detail-never-returned'}}}
    path.write_text(json.dumps(header)+'\n'+json.dumps(terminal)+'\n')
    other = folder/'rollout-other.jsonl'
    other.write_text(json.dumps(header)+'\n')
    worker = "import sys; files=[open(p,'a') for p in sys.argv[1:]]; print('ready',flush=True); sys.stdin.read()"
    paths = [str(path), str(other)] if variant == 'ambiguous' else [str(path)]
    child = subprocess.Popen([sys.executable, '-c', worker, *paths], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        observed = subprocess.check_output(['ps','-ww','-p',str(child.pid),'-o','lstart='], text=True).strip()
        stat = path.stat()
        expected = {'pane_pid': str(child.pid), 'native_session_id': 'fixture-native',
            'error_turn_id': 'fixture-turn', 'binding': {'pane_pid': str(child.pid),
            'pane_started_at': observed, 'executable': sys.executable,
            'transcript': {'path': str(path), 'file_id': [stat.st_dev,stat.st_ino], 'session_id': 'fixture-native'}}}
        if variant == 'satellite_anchor':
            import hashlib
            digest=hashlib.sha256(f'{path}\0{stat.st_dev}\0{stat.st_ino}'.encode()).hexdigest()
            expected['binding']['transcript']={'source_file_identity_digest':digest,'native_session_id':'fixture-native'}
        if variant == 'newer_record':
            with path.open('a') as out: out.write(json.dumps({'type':'event_msg','payload':{'type':'task_started'}})+'\n')
        elif variant == 'partial_tail':
            with path.open('a') as out: out.write('{"type":')
        elif variant == 'wrong_turn': expected['error_turn_id'] = 'stale-turn'
        elif variant == 'wrong_session': expected['native_session_id'] = 'foreign-native'
        elif variant == 'changed_file': expected['binding']['transcript']['file_id'][1] += 1
        result = probe(expected)
        assert result['ok'] is (variant in ('valid','satellite_anchor')), result
        assert 'private-detail' not in json.dumps(result)
        if variant in ('valid','satellite_anchor'):
            # Execute exactly the same standalone source used by Hosts.run_command.
            result = subprocess.run([sys.executable, '-c', program(), json.dumps(expected)], capture_output=True, text=True)
            assert result.returncode == 0 and json.loads(result.stdout)['ok'] is True
    finally:
        child.terminate(); child.communicate(timeout=5)


@pytest.mark.parametrize('composer,expected',[
    ('› ',True),
    ('› operator draft',False),
    ('\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m',True),
    ('› \n  wrapped draft',False),
    ('› [Pasted Content 100 chars]',False),
    ('› \nQueued messages',False),
])
def test_composer_is_empty_or_proven_dim_placeholder(composer,expected):
    from boot_ready import codex_composer_empty
    assert codex_composer_empty('OpenAI Codex\n'+composer+'\n gpt-6-test high') is expected
