"""Synthetic counterpart receipts for the pin operation; no host/SSH/provider calls."""
import json
import hashlib
import time

import pytest


def helper_identity(root, host):
    """Explicitly synthetic mixed helper/configured-absence identity."""
    proof = root / (host + '-helper-proof.json')
    if not proof.exists():
        proof.write_text(json.dumps({'synthetic': True}))
    proof.chmod(0o444)
    path = root / (host + '-helper-manifest.json')
    payload = json.dumps({'host': host, 'configured_helpers': {
        'host': host, 'invocations': [{'role': 'configured_account_executor', 'status': 'not configured'}],
        'closures': [{'root': '/synthetic/usage', 'actual_checkout_head': 'a' * 40}],
        'interpreters': {'/synthetic/python': {'version': 'synthetic'}},
        'loaded_module_provenance': {'status': 'not_attested'}},
        'files': [{'path': str(proof), 'sha256': hashlib.sha256(proof.read_bytes()).hexdigest()}],
        'limits': ['Synthetic fixture only; no loaded/native/runtime proof.']})
    if not path.exists():
        path.write_text(payload)
    else:
        assert path.read_text() == payload
    path.chmod(0o444)
    binding = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return {'installed_helpers_sha256': binding['sha256'], 'installed_helpers_manifest': binding}


@pytest.fixture(autouse=True)
def pin_wall_clock(monkeypatch):
    # These fixtures seed counterpart telemetry before invoking the pin writer.
    # Freeze wall time so store setup cannot make that synthetic row stale.
    # Activation deadlines still use the real event-loop monotonic clock.
    now = time.time()
    monkeypatch.setattr(time, 'time', lambda: now)


def preflight(root, candidate, hosts=('workstation',), optional=()):
    rows = []
    for host in hosts:
        checkout = '/isolated/satellite'
        rows.append({'host': host, 'required': host not in optional, 'online': host not in optional,
                     'checkout': checkout, 'script': checkout + '/services/chat-stream-v2/satellite.py',
                     'interpreter': checkout + '/.venv/bin/python', 'python_version': '3.13.2',
                     'imports_passed': True, 'compatibility_passed': True, 'recovery_ready': True,
                     'previous_incarnation': 'prior-boot', 'dependency_digest': 'd' * 64,
                     **helper_identity(root, host), 'consumer_payload_sha256': 'f' * 64,
                     'installed_cli_version': 'fixture-cli', 'installed_source_sha': 'a' * 40,
                     'configuration_sha256': 'c' * 64, 'installed_cli_sha256': 'b' * 64})
    path = root / ('preflight-' + candidate + '.json')
    path.write_text(json.dumps({'candidate': candidate, 'observed_at_epoch': time.time(), 'participants': rows}))
    return path


def runtime(sha):
    return {'sha': sha, 'pid': 4321, 'incarnation': 'candidate-boot', 'admitted': True,
            'observed_at': 'fixture-now', 'observed_at_epoch': time.time() + 0.1,
            'useful_events': 1, 'useful_observed_at_epoch': time.time() + 0.1,
            'recovery_pending': 0, 'recovery_gaps': []}
