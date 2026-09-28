"""Focused bootstrap safety, cold process-copy and offline report contracts."""
from __future__ import annotations
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'services/chat-stream-v2'), str(ROOT / 'services/agent-orch'), str(ROOT / 'services')]
spec = importlib.util.spec_from_file_location('own_bootstrap', ROOT / 'tools/bootstrap_assistant.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def args(tmp_path, *extra):
    credential = tmp_path / 'operator.token'
    credential.write_text('synthetic credential, never used by these unit checks')
    credential.chmod(0o600)
    instructions = tmp_path / 'instructions.md'
    instructions.write_text('Synthetic private instructions.')
    return bootstrap.parser().parse_args(['--url', 'ws://127.0.0.1:1',
        '--credential-file', str(credential), '--physical-host', 'local', '--name', 'Nova',
        '--provider', 'codex', '--model', 'gpt-6-sol', '--effort', 'medium',
        '--private-workspace', str(tmp_path / 'private'), '--instructions-file', str(instructions), *extra])


def test_dry_run_ignores_inherited_endpoint_and_has_no_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_ORCH_WS_URL', 'wss://production.invalid')
    monkeypatch.setenv('AGENT_ORCH_STREAM_TOKEN', 'synthetic inherited secret')
    arguments = args(tmp_path, '--dry-run')
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    result = bootstrap.bootstrap(arguments)
    assert result['url'] == 'ws://127.0.0.1:1' and result['mutated'] is False
    assert not arguments.private_workspace.exists()
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert 'synthetic inherited secret' not in json.dumps(result)


def test_existing_output_refuses_before_network_and_preserves_bytes(tmp_path):
    arguments = args(tmp_path)
    arguments.private_workspace.mkdir()
    existing = arguments.private_workspace / 'assistant.env'
    existing.write_bytes(b'owner config')
    with pytest.raises(ValueError, match='outputs already exist'):
        bootstrap.bootstrap(arguments)
    assert existing.read_bytes() == b'owner config'


def test_insecure_credential_and_embedded_url_secret_refused(tmp_path):
    arguments = args(tmp_path, '--dry-run')
    arguments.credential_file.chmod(0o644)
    with pytest.raises(ValueError, match='owner-only'):
        bootstrap.bootstrap(arguments)
    arguments.credential_file.chmod(0o600)
    arguments.url = 'ws://user:secret@127.0.0.1:1'
    with pytest.raises(ValueError, match='embedded credentials'):
        bootstrap.bootstrap(arguments)


def test_atomic_private_output_never_follows_or_replaces_symlink(tmp_path):
    target = tmp_path / 'owner-file'
    target.write_text('preserve')
    destination = tmp_path / 'config'
    destination.symlink_to(target)
    with pytest.raises(FileExistsError):
        bootstrap.private_new(destination, 'replacement')
    assert target.read_text() == 'preserve'
    assert destination.is_symlink()
    assert not list(tmp_path.glob('.bootstrap-*'))
    fresh = tmp_path / 'fresh'
    bootstrap.private_new(fresh, 'complete')
    assert fresh.read_text() == 'complete' and fresh.stat().st_mode & 0o777 == 0o600


def test_ambiguous_remote_spawn_retains_intent_and_prevents_second_spawn(tmp_path, monkeypatch):
    import tools.live_window
    frames = []
    class Operator:
        snapshot = {'sessions': []}
        def rpc(self, payload):
            frames.append(payload)
            if payload['type'] == 'assistant.binding':
                return {'type': 'assistant.binding.ok', 'stream_id': '', 'generation': ''}
            raise TimeoutError('synthetic connection loss after spawn transmission')
    @contextmanager
    def connection(*_):
        yield Operator()
    monkeypatch.setattr(tools.live_window, 'authenticated_operator_connection', connection)
    arguments = args(tmp_path)
    with pytest.raises(TimeoutError):
        bootstrap.bootstrap(arguments)
    intent = json.loads((arguments.private_workspace / 'assistant-bootstrap-intent.json').read_text())
    assert intent['proposed_backend_stream_id'] == 'local:' + frames[-1]['session_name']
    assert intent['idempotency_key'] == frames[-1]['idempotency_key']
    assert frames[-1]['role'] == 'assistant' and frames[-1]['parent_stream_id'] is None
    with pytest.raises(ValueError, match='outputs already exist'):
        bootstrap.bootstrap(arguments)
    assert len(frames) == 2


def test_configured_name_is_shell_data_not_a_command(tmp_path):
    name = "Nova $(touch unwanted); 'quoted' `false`"
    env = tmp_path / 'assistant.env'
    bootstrap.private_new(env, 'export PENTACLE_ASSISTANT_COMPOSITE_TITLE=' + shlex.quote(name) + '\n')
    result = subprocess.run(['/bin/sh', '-c', '. "$1"; printf %s "$PENTACLE_ASSISTANT_COMPOSITE_TITLE"', 'sh', str(env)],
                            cwd=tmp_path, capture_output=True, text=True, check=True)
    assert result.stdout == name and not (tmp_path / 'unwanted').exists()


@pytest.mark.parametrize('defect', ['effective_tuple', 'role_protection'])
def test_backend_mismatch_refuses_activation_and_preserves_recovery_intent(tmp_path, monkeypatch, defect):
    import tools.live_window
    class Operator:
        snapshot = {'sessions': []}
        def rpc(self, payload):
            if payload['type'] == 'assistant.binding':
                return {'type': 'assistant.binding.ok', 'stream_id': ''}
            if payload['type'] == 'spawn':
                self.stream = 'local:' + payload['session_name']
                return {'type': 'spawn.ok', 'stream_id': self.stream}
            if payload['type'] == 'assistant.lifecycle':
                return {'target': {'eligible': defect != 'role_protection', 'session_generation': 'fixture-generation'}}
            return {'session': {'bootstrap_state': 'ready', 'session_generation': 'fixture-generation',
                'role': 'assistant', 'status': 'open', 'parent_stream_id': None, 'provider': 'codex',
                'effective_model': 'gpt-6-sol', 'effective_effort': 'high' if defect == 'effective_tuple' else 'medium'}}
    @contextmanager
    def connection(*_):
        yield Operator()
    monkeypatch.setattr(tools.live_window, 'authenticated_operator_connection', connection)
    arguments = args(tmp_path)
    with pytest.raises(RuntimeError, match='protected top-level tuple'):
        bootstrap.bootstrap(arguments)
    assert (arguments.private_workspace / 'assistant-bootstrap-intent.json').exists()
    assert not (arguments.private_workspace / 'assistant.env').exists()
    assert not (arguments.private_workspace / 'assistant-client.cjs').exists()


def test_restart_readback_refuses_a_changed_generation_without_writing(tmp_path, monkeypatch):
    import tools.live_window
    arguments = args(tmp_path, '--readback')
    workspace, _, _, _, paths = bootstrap.prepare(arguments)
    workspace.mkdir()
    plan = bootstrap.bootstrap(args(tmp_path, '--dry-run'))
    receipt = {**plan, 'backend_stream_id': 'local:assistant-backend-fixture',
               'backend_generation': 'accepted-generation',
               'effective_tuple': plan['requested_tuple']}
    paths[2].write_text(json.dumps(receipt))
    before = paths[2].read_bytes()
    class Operator:
        def rpc(self, payload):
            if payload['type'] == 'assistant.binding':
                return {'type': 'assistant.binding.ok', 'stream_id': receipt['backend_stream_id'], 'generation': 'stale-generation'}
            return {}
    @contextmanager
    def connection(*_):
        yield Operator()
    monkeypatch.setattr(tools.live_window, 'authenticated_operator_connection', connection)
    with pytest.raises(RuntimeError, match='restart readback failed'):
        bootstrap.bootstrap(arguments)
    assert paths[2].read_bytes() == before and not paths[0].exists()


def test_public_process_copy_creation_validation_and_discovery(tmp_path):
    kit = tmp_path / 'process'
    shutil.copytree(ROOT / 'process', kit, ignore=shutil.ignore_patterns('__pycache__', '.venv'))
    def run(script, *arguments):
        return subprocess.run([sys.executable, str(kit / 'scripts' / script), *arguments], cwd=kit,
                              capture_output=True, text=True, check=True).stdout
    # Pristine kit is independently valid after generated catalog publication.
    run('generate_catalog.py')
    run('validate_memory_v2.py')
    run('validate_memory_v2.py', '--source-only')
    run('new_work_item.py', 'example-app', 'explicit_change', '--title', 'Explicit change', '--summary',
        'Synthetic explicit work.', '--status', 'analysis', '--machine', 'local', '--owner', 'maintainer')
    run('new_work_item.py', 'example-app', 'default_change', '--title', 'Default change', '--summary', 'Synthetic default work.')
    personal = kit / 'docs/personal'
    personal.mkdir()
    shutil.copyfile(kit / 'templates/document.md', personal / 'example_note.md')
    run('generate_catalog.py')
    run('validate_memory_v2.py')
    run('validate_memory_v2.py', '--source-only')
    assert 'note_example' in run('search_memory_v2.py', 'Example note')
    assert 'explicit_change' in run('search_memory_v2.py', 'Explicit change')
    assert 'default_change' in run('search_memory_v2.py', 'Default change')
    assert 'meta_memory' in run('search_memory_v2.py', 'Memory discovery')
    assert (kit / 'schema/document.schema.json').read_bytes() == (ROOT / 'process/schema/document.schema.json').read_bytes()
    assert (kit / 'work/statuses.json').read_bytes() == (ROOT / 'process/work/statuses.json').read_bytes()


def test_documented_report_examples_parse_and_validate_offline():
    from agent_orch.cli import build_parser, _report_payload_from_args
    text = (ROOT / 'process/docs/config/agent_orchestration.md').read_text()
    examples = [block for block in re.findall(r'```sh\n(.*?)\n```', text, re.S) if block.startswith('agent-orch report')]
    assert len(examples) == 3
    replacements = {'$CANDIDATE_SHA': 'a' * 40, '$REVIEWED_SCOPE': 'tools/bootstrap_assistant.py and focused checks',
                    '$GATE_DIGEST': 'b' * 64, '$INDEPENDENT_QA_STREAM': 'local:independent-qa',
                    '$INDEPENDENT_QA_REPORT': '00000000-0000-4000-8000-000000000001'}
    payloads = []
    for example in examples:
        for key, value in replacements.items():
            example = example.replace(key, value)
        parsed = build_parser().parse_args(shlex.split(example.replace('\\\n', ' '))[1:])
        payload, blob = _report_payload_from_args(parsed)
        assert blob is None
        payloads.append(payload)
    assert 'qa_verdict' not in payloads[0] and 'completion_kind' not in payloads[0]
    assert payloads[1]['qa_verdict'] == 'accept'
    assert payloads[1]['extras']['qa_review']['candidate_identity'] == 'a' * 40
    assert payloads[1]['extras']['qa_review']['gate_evidence_digest'] == 'b' * 64
    assert payloads[2]['completion_kind'] == 'implementation_ready'
    assert payloads[2]['qa_attestation']['stream_id'] == 'local:independent-qa'
