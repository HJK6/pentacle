#!/usr/bin/env python3
"""Run the fixture daemon with its own real operator credential registry.

Only the registry's storage location is injected; challenge/proof verification
and command authorization use the production implementation unchanged.
"""
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'services'), str(ROOT / 'services/chat-stream-v2')]
from _shared import operator_auth

scratch = Path(sys.argv[1])
auth_dir = scratch / 'operator-auth'
auth_dir.mkdir(mode=0o700, exist_ok=True)
registry_path = auth_dir / 'registry.json'
if sys.argv[2:] == ['--issue']:
    registry = operator_auth.OperatorCredentialRegistry(registry_path)
    _, envelope = registry.issue('pentacle', label='isolated web gate')
    token_path = auth_dir / 'token'
    descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as token:
        token.write(envelope)
else:
    original = operator_auth.OperatorCredentialRegistry

    class FixtureRegistry(original):
        def __init__(self, path=None):
            super().__init__(path or registry_path)

    operator_auth.OperatorCredentialRegistry = FixtureRegistry
    sys.argv = [str(ROOT / 'services/chat-stream-v2/main.py'), *sys.argv[2:]]
    runpy.run_path(sys.argv[0], run_name='__main__')
