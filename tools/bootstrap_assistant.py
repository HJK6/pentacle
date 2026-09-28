#!/usr/bin/env python3
"""Provision a protected backend; emit private config for an owner-run restart.

No service management, default endpoint, direct database writes or agent-token
role grant. Authentication is the daemon's existing operator challenge/proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', required=True)
    p.add_argument('--credential-file', type=Path, required=True)
    p.add_argument('--physical-host', required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--provider', choices=('claude', 'codex'), required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--effort', required=True)
    p.add_argument('--private-workspace', type=Path, required=True)
    p.add_argument('--instructions-file', type=Path, required=True)
    p.add_argument('--timeout', type=float, default=90)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--readback', action='store_true', help='verify an owner-restarted daemon against the existing receipt')
    return p


def private_new(path: Path, data: str):
    """Publish a complete owner-only file without replacing any existing path."""
    temporary = path.parent / ('.bootstrap-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, path)  # fails closed on an existing file or symlink
    finally:
        temporary.unlink(missing_ok=True)


def prepare(args):
    endpoint = urlsplit(args.url)
    if (endpoint.scheme not in ('ws', 'wss') or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.fragment or endpoint.query):
        raise ValueError('an explicit ws/wss endpoint without embedded credentials is required')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.physical_host):
        raise ValueError('physical host must be one machine identifier')
    if not args.name.strip() or len(args.name) > 120 or any(ord(c) < 32 for c in args.name):
        raise ValueError('name must be 1–120 printable characters')
    if args.name != args.name.strip():
        raise ValueError('name must not have surrounding whitespace')
    if args.timeout <= 0 or not args.model.strip() or not args.effort.strip():
        raise ValueError('positive timeout and explicit model/effort are required')
    workspace = args.private_workspace.expanduser().absolute()
    if workspace.resolve().is_relative_to(ROOT):
        raise ValueError('private workspace must be outside the public checkout')
    if workspace.exists() and (not workspace.is_dir() or workspace.is_symlink()):
        raise ValueError('private workspace must be a directory, not a symlink')
    credential = args.credential_file.expanduser().resolve(strict=True)
    if not credential.is_file() or credential.stat().st_mode & 0o077:
        raise ValueError('operator credential must be an owner-only file')
    instructions = args.instructions_file.expanduser().resolve(strict=True)
    if credential.is_relative_to(ROOT) or instructions.is_relative_to(ROOT):
        raise ValueError('credentials and private instructions must be outside the public checkout')
    text = instructions.read_text(encoding='utf-8')
    if not text.strip():
        raise ValueError('instructions must be nonempty')
    paths = [workspace / name for name in ('assistant.env', 'assistant-config.json',
                                           'assistant-receipt.json', 'assistant-client.cjs',
                                           'assistant-bootstrap-intent.json')]
    if not args.readback and any(path.exists() or path.is_symlink() for path in paths):
        raise ValueError('bootstrap outputs already exist; preserve them and inspect/recover the existing owner')
    return workspace, credential, instructions, text, paths


def bootstrap(args):
    workspace, credential, instructions, text, paths = prepare(args)
    plan = {'schema_version': 1, 'url': args.url, 'physical_host': args.physical_host,
            'name': args.name, 'requested_tuple': {'provider': args.provider,
                'model': args.model, 'effort': args.effort},
            'composite_stream_id': args.physical_host + ':assistant',
            'private_workspace': str(workspace), 'output_paths': [str(p) for p in paths],
            'instructions_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'recovery': 'off', 'requires_owner_restart': True}
    if args.dry_run:
        return {**plan, 'dry_run': True, 'mutated': False}
    # Imports happen only after dry-run; no connection or local state on a plan.
    sys.path[:0] = [str(ROOT / 'services/chat-stream-v2'), str(ROOT / 'services')]
    from tools.live_window import authenticated_operator_connection
    if args.readback:
        receipt = json.loads(paths[2].read_text(encoding='utf-8'))
        if any(receipt.get(key) != plan[key] for key in ('url', 'physical_host', 'name', 'requested_tuple', 'instructions_sha256')):
            raise ValueError('readback inputs differ from the preserved bootstrap receipt')
        with authenticated_operator_connection(args.url, credential, args.timeout) as operator:
            binding = operator.rpc({'type': 'assistant.binding'})
            inspection = operator.rpc({'type': 'inspect_stream', 'stream_id': receipt['backend_stream_id']})
            composite = operator.rpc({'type': 'inspect_stream', 'stream_id': receipt['composite_stream_id']})
            lifecycle = operator.rpc({'type': 'assistant.lifecycle', 'action': 'inspect',
                                      'target_stream_id': receipt['backend_stream_id']})
        session = inspection.get('session') or {}
        effective = {'provider': session.get('provider'), 'model': session.get('effective_model'),
                     'effort': session.get('effective_effort')}
        if (binding.get('type') != 'assistant.binding.ok'
                or binding.get('stream_id') != receipt['backend_stream_id']
                or binding.get('generation') != receipt['backend_generation']
                or session.get('session_generation') != receipt['backend_generation']
                or session.get('bootstrap_state') != 'ready'
                or effective != receipt['effective_tuple'] or session.get('role') != 'assistant'
                or session.get('parent_stream_id') or (lifecycle.get('target') or {}).get('eligible') is not True
                or (composite.get('session') or {}).get('title') != args.name):
            raise RuntimeError('restart readback failed exact binding/readiness/title verification')
        return {**receipt, 'activation': 'restart_binding_verified', 'binding': binding}
    workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
    session_name = 'assistant-backend-' + uuid.uuid4().hex[:12]
    with authenticated_operator_connection(args.url, credential, args.timeout) as operator:
        # An enabled/pinned composite is an ambiguous existing installation.
        binding = operator.rpc({'type': 'assistant.binding'})
        if binding.get('type') != 'assistant.binding.ok':
            raise RuntimeError('initial daemon does not expose authenticated binding inspection')
        if ((operator.snapshot or {}).get('capabilities', {}).get('assistant_composite_v1')
                or binding.get('enabled') or binding.get('stream_id')):
            raise ValueError('existing composite binding: use owner/handoff recovery, not bootstrap')
        sessions = (operator.snapshot or {}).get('sessions', [])
        if any(s.get('role') == 'assistant' and s.get('status') == 'open'
               and s.get('host') == args.physical_host for s in sessions):
            raise ValueError('an assistant holder already exists; inspect/recover it')
        request_id, idempotency_key = str(uuid.uuid4()), str(uuid.uuid4())
        # Durable before RPC: ambiguity/connection loss never becomes an
        # invitation to spawn a second owner. O_EXCL publication also admits
        # at most one bootstrap caller for this private workspace.
        private_new(paths[4], json.dumps({**plan, 'request_id': request_id,
            'idempotency_key': idempotency_key,
            'proposed_backend_stream_id': args.physical_host + ':' + session_name,
            'next_action': 'Inspect this exact request/backend before retry or recovery'}, indent=2) + '\n')
        spawned = operator.rpc({'type': 'spawn', 'host': args.physical_host,
            'session_name': session_name, 'provider': args.provider, 'model': args.model,
            'effort': args.effort, 'role': 'assistant', 'visibility': 'visible',
            'parent_stream_id': None, 'cwd': str(workspace), 'title': args.name,
            'initial_prompt': text, 'request_id': request_id, 'idempotency_key': idempotency_key})
        if spawned.get('type') != 'spawn.ok':
            raise RuntimeError('protected spawn refused: ' + str(spawned.get('error_code') or spawned.get('error')))
        stream_id = spawned.get('stream_id')
        if stream_id != args.physical_host + ':' + session_name:
            raise RuntimeError('spawn returned an unexpected stream identity')
        deadline = time.monotonic() + args.timeout
        while True:
            inspection = operator.rpc({'type': 'inspect_stream', 'stream_id': stream_id})
            session = inspection.get('session') or {}
            if session.get('bootstrap_state') == 'ready':
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('backend not ready; inspect the spawned owner before retrying')
            time.sleep(.1)
        generation = session.get('session_generation')
        effective = {'provider': session.get('provider'), 'model': session.get('effective_model'),
                     'effort': session.get('effective_effort')}
        lifecycle = operator.rpc({'type': 'assistant.lifecycle', 'action': 'inspect',
                                  'target_stream_id': stream_id})
        protected = lifecycle.get('target') or {}
        if (not generation or session.get('role') != 'assistant'
                or session.get('parent_stream_id') or session.get('status') != 'open'
                or effective != plan['requested_tuple']
                or protected.get('eligible') is not True
                or protected.get('session_generation') != generation):
            raise RuntimeError('backend readback does not match the requested protected top-level tuple')
    environment = {'PENTACLE_ASSISTANT_ROLE': 'assistant',
        'PENTACLE_ASSISTANT_COMPOSITE_ENABLED': '1',
        'PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID': plan['composite_stream_id'],
        'PENTACLE_ASSISTANT_COMPOSITE_TITLE': args.name,
        'PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID': stream_id,
        'PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION': generation}
    client = {'features': {'assistantRole': 'assistant', 'mic': False, 'chatUi': True},
              'chatStream': {'url': args.url, 'localHost': args.physical_host,
                             'hosts': [args.physical_host], 'tokenPath': str(credential)}}
    receipt = {**plan, 'dry_run': False, 'mutated': True, 'backend_stream_id': stream_id,
        'backend_generation': generation, 'bootstrap_state': 'ready',
        'protected_role_verified': True,
        'effective_tuple': effective, 'daemon_environment': environment,
        'instructions_file': str(instructions), 'activation': 'owner_restart_required'}
    private_new(paths[0], ''.join(f'export {key}={shlex.quote(value)}\n' for key, value in environment.items()))
    private_new(paths[1], json.dumps({'daemon_environment': environment, 'client': client}, indent=2) + '\n')
    private_new(paths[2], json.dumps(receipt, indent=2) + '\n')
    private_new(paths[3], 'module.exports = ' + json.dumps(client, indent=2) + ';\n')
    return receipt


def main():
    try:
        print(json.dumps(bootstrap(parser().parse_args()), sort_keys=True))
        return 0
    except Exception as error:
        # Do not echo server payloads, instruction text or credentials.
        print(json.dumps({'status': 'error', 'message': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
