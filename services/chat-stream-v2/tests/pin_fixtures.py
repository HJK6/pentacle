"""Synthetic counterpart receipts for the pin operation; no host/SSH/provider calls."""
import json
import time


def preflight(root, candidate, hosts=('workstation',), optional=()):
    rows = []
    for host in hosts:
        checkout = '/isolated/satellite'
        rows.append({'host': host, 'required': host not in optional, 'online': host not in optional,
                     'checkout': checkout, 'script': checkout + '/services/chat-stream-v2/satellite.py',
                     'interpreter': checkout + '/.venv/bin/python', 'python_version': '3.13.2',
                     'imports_passed': True, 'compatibility_passed': True, 'recovery_ready': True,
                     'previous_incarnation': 'prior-boot', 'dependency_digest': 'd' * 64,
                     'installed_helpers_sha': 'e' * 40, 'consumer_payload_sha256': 'f' * 64,
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
