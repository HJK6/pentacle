"""Finite-run safety controls; live journey evidence is recorded separately."""
import json
import base64
import hashlib

import pytest

from tools import installed_journey as subject


def manifest():
    return dict(schema='pentacle.installed-journey.v1', web_url='https://fixture.invalid',
        expected_build_id='accepted', source_sha='a' * 40, ws_url='ws://fixture.invalid',
        operator_token_path='/secure/operator', operator_registry_path='/secure/registry',
        local_host='fixture', machine_cwd='/fixture',
        spec_hashes={'/fixture/spec.md':'d' * 64},
        cli={'executable': '/fixture/cli', 'sha256': 'b' * 64, 'version':'a' * 40,
             'manifest_path':'/fixture/release.json','manifest_sha256':'e' * 64,'modules':{'cli':{},'wsclient':{}}},
        daemon={'pid': 123, 'birth': 'fixture-birth', 'boot': 'fixture-boot', 'checkout': '/fixture', 'executable':'/fixture/python'},
        provider=dict(alias='ours', generation='fixture-generation', executable='/fixture/native',
            executable_sha256='c' * 64, version='fixture-version', model='fixture-model', effort='high'))


def invoke(tmp_path, value, *extra):
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(value))
    out = tmp_path / 'evidence'
    code = subject.main(['--manifest', str(path), '--evidence-dir', str(out), *extra])
    return code, json.loads((out / 'rows.json').read_text())


def test_wrong_served_build_precedes_all_runtime_and_fixture_actions(monkeypatch, tmp_path):
    monkeypatch.setattr(subject, 'served_build', lambda url: 'accepted')
    def forbidden(*args):
        pytest.fail('wrong build reached installed identity or fixture admission')
    monkeypatch.setattr(subject.Journey, 'identity', forbidden)
    monkeypatch.setattr(subject.Journey, 'spawn', forbidden)
    code, rows = invoke(tmp_path, manifest(), '--expect-build-id', 'deliberately-wrong')
    assert code == 1
    assert rows[0]['row'] == 'build' and rows[0]['result'] == 'FAIL'
    assert 'served build mismatch' in rows[0]['primary_error']
    assert rows[0]['fixture_ids'] == []
    assert rows[0]['cleanup']['expected'] == rows[0]['cleanup']['closed'] == 0


def test_inline_credential_refuses_before_network(monkeypatch, tmp_path):
    value = manifest()
    value['provider']['token'] = 'private-fixture-value'
    monkeypatch.setattr(subject, 'served_build', lambda url: pytest.fail('network reached'))
    code, rows = invoke(tmp_path, value)
    assert code == 2 and rows[0]['result'] == 'BLOCKED'
    assert 'private-fixture-value' not in json.dumps(rows)


def test_cleanup_failure_never_overwrites_primary_failure(monkeypatch, tmp_path):
    def fail(run):
        run.cleanup['errors'].append('owned browser cleanup unproved')
        raise AssertionError('native response missing')
    monkeypatch.setattr(subject.Journey, 'run', fail)
    code, rows = invoke(tmp_path, manifest())
    assert code == 1
    assert rows[0]['primary_error'] == 'native response missing'
    assert rows[0]['harness_error'] == 'owned browser cleanup unproved'


def test_pending_spawn_cannot_pass_without_owned_cleanup(monkeypatch, tmp_path):
    def ambiguous(run):
        run.pending_spawns.append({'idempotency_key': 'immutable-fixture-key'})
        run.row('P2.spawn', 'BLOCKED', primary_error='spawn reply unavailable')
    monkeypatch.setattr(subject.Journey, 'run', ambiguous)
    code, rows = invoke(tmp_path, manifest())
    assert code == 1
    assert 'FD reconciliation required' in rows[0]['harness_error']
    assert rows[0]['primary_error'] == 'spawn reply unavailable'


def test_readiness_failure_retains_admitted_credential_binding(tmp_path):
    from types import SimpleNamespace
    from tools.live_window import OwnedSession
    run = subject.Journey(manifest(), tmp_path)
    run.registry = SimpleNamespace(_owned={})
    class Register:
        def prepare_owned_spawn(self, payload):
            pass
        def __call__(self, payload, reply):
            run.registry._owned[reply['stream_id']] = OwnedSession(
                payload['request_id'], payload['idempotency_key'], reply['stream_id'],
                payload['host'], payload['session_name'], 'owned-generation')
    run.call = lambda payload: {'stream_id': 'fixture:owned', 'state': 'starting'}
    run.inspect = lambda stream: {'session': {'session_generation': 'owned-generation', 'token_hash': 'f' * 64}}
    def not_ready(stream):
        raise RuntimeError('owned readiness fault')
    with pytest.raises(RuntimeError, match='owned readiness fault'):
        run.spawn('fixture', Register(), not_ready, 'fixture-catalog')
    saved = json.loads((tmp_path / 'run-manifest.json').read_text())
    assert saved['pending_spawns'] == []
    assert saved['fixtures'][0]['stream_token_sha256'] == 'f' * 64
    assert saved['fixtures'][0]['spawn_reply']['stream_id'] == 'fixture:owned'
    assert saved['cleanup']['expected'] == 1


def test_dirty_evidence_is_retired_without_overwriting_original(tmp_path):
    out = tmp_path / 'evidence'
    out.mkdir()
    original = '[{"primary_error":"original failure"}]\n'
    (out / 'rows.json').write_text(original)
    assert subject.main(['--manifest', str(tmp_path / 'unused'), '--evidence-dir', str(out)]) == 2
    assert (out / 'rows.json').read_text() == original


@pytest.mark.parametrize('fault', [None, 'bytes', 'later-turn', 'client-id'])
def test_native_image_proof_binds_loaded_bytes_to_exact_input_turn(tmp_path, fault):
    png = b'owned-test-PNG-bytes'
    image = {'sha256': hashlib.sha256(png).hexdigest(), 'bytes':len(png)}
    user = {'type':'response_item','payload':{'type':'message','role':'user',
        'content':[{'type':'input_text','text':'owned image caption'}]}}
    data = b'foreign bytes' if fault == 'bytes' else png
    output = {'type':'response_item','payload':{'type':'custom_tool_call_output','call_id':'owned',
        'output':repr([{'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(data).decode()}])}}
    other = {'type':'response_item','payload':{'type':'message','role':'user',
        'content':[{'type':'input_text','text':'later input'}]}}
    records = [user,other,output] if fault == 'later-turn' else [user,output,other]
    path = tmp_path / 'native.jsonl'
    path.write_text(''.join(json.dumps(r)+'\n' for r in records))
    info = path.stat()
    run = subject.Journey(manifest(),tmp_path)
    run.inspect = lambda stream: {'session':{'observer_binding':{'transcript':{
        'path':str(path),'file_id':[info.st_dev,info.st_ino]}}},'recent_events':[
        {'kind':'USER','text':'owned image caption','daemon_seq':12,'request_id':'send-1','optimistic_id':'input-1'}]}
    requests = [{'text':'owned image caption','requestId':'wrong' if fault == 'client-id' else 'send-1','optimisticId':'input-1'}]
    if fault:
        with pytest.raises(AssertionError):
            run.native_proof({'stream_id':'fixture:owned'},['owned image caption'],[],image,requests)
    else:
        proof = run.native_proof({'stream_id':'fixture:owned'},['owned image caption'],[],image,requests)
        assert proof['inputs'][0]['daemon_seq']==12
        path.write_text(path.read_text()+'{"type":"later-append"}\n')
        assert subject.digest(proof['captured_path']) == proof['sha256'] != subject.digest(path)


def test_owned_child_exits_if_authenticated_receiver_fails(monkeypatch, tmp_path):
    import os
    import subprocess
    import sys
    real_popen = subprocess.Popen
    children = []
    def owned_child(argv, **kwargs):
        process = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(subject.subprocess, 'Popen', owned_child)
    run = subject.Journey(manifest(), tmp_path)
    def refuse(payload):
        raise AssertionError('authenticated receiver failed')
    run.call = refuse
    with pytest.raises(AssertionError, match='authenticated receiver failed'):
        run.child(['owned-adapter'], env=dict(os.environ), stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, timeout=1)
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.parametrize('value', [None, [], {'schema':'wrong'}, {**manifest(),'provider':[]}])
def test_invalid_manifest_is_blocked_before_any_network(tmp_path, monkeypatch, value):
    monkeypatch.setattr(subject, 'served_build', lambda url: pytest.fail('network reached'))
    code, rows = invoke(tmp_path,value)
    assert code == 2 and rows[0]['result'] == 'BLOCKED'
