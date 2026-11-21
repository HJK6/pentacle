"""Adapters for memory decisions, FD relay packets and durable notices."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from .quarantine_relay import prepare_bundle

PUBLIC_REPOSITORY = 'https://github.com/HJK6/pentacle.git'
FLEET_CHECKER_SHA256 = '6816589b7e3f41c0a411215a9e55581c7bede0079cb69cc7b20219fa4e34891c'
_NOTIFY_ENV = (
    'HOME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE',
    'AGENT_ORCH_WS_URL', 'AGENT_ORCH_HOST_ID', 'AGENT_ORCH_RUNTIME_DIR',
)


class ProductionAdapter:
    def __init__(self, decisions, *, checker, packet_root, agent_orch='agent-orch',
                 restoration_verifier=None):
        self.decisions = decisions
        self.checker = Path(checker)
        self.packet_root = Path(packet_root)
        self.agent_orch = agent_orch
        self.restoration_verifier = restoration_verifier
        self.verified_restorations = {}

    def decision(self, test, occurrences):
        restoration = self.verified_restorations.get(test)
        if restoration:
            return self.decisions.upsert(test, occurrences, restoration=restoration)
        return self.decisions.upsert(test, occurrences)

    @staticmethod
    def _card_key(test, decision):
        value = json.dumps([test, decision], ensure_ascii=False, separators=(',', ':')).encode()
        return 'flaky-test-quarantine:' + hashlib.sha256(value).hexdigest()

    def _notify(self, key, title, message, severity='warning', *, producer='flaky-test-quarantine'):
        env = {name: os.environ[name] for name in _NOTIFY_ENV if name in os.environ}
        proc = subprocess.run([self.agent_orch, 'notify', '--producer', producer,
                               '--severity', severity, '--title', title, '--message', message,
                               '--dedup-key', key], env=env, capture_output=True, text=True,
                              timeout=30, check=False)
        try:
            response = json.loads(proc.stdout)
        except ValueError as exc:
            raise ValueError('notification receipt unavailable') from exc
        if (proc.returncode or response.get('type') != 'notification.create.ok'
                or not isinstance(response.get('notification'), dict)):
            raise ValueError('notification not confirmed')

    @staticmethod
    def _message(payload, relay, state):
        if not isinstance(relay, dict) or not relay.get('path') or not relay.get('sha256'):
            raise ValueError('relay bundle evidence unavailable')
        return (state + ': ' + payload['test'] + '. Decision ' + payload['item']
                + ', owner ' + payload['owner'] + ', coverage loss: ' + payload['coverage']
                + '. Occurrences: ' + ', '.join(payload['occurrences'])
                + '. Relay bundle: ' + relay['path'] + ' SHA256 ' + relay['sha256'])

    def prepare_relay(self, selected, key, pending):
        directory = self.packet_root / key.split(':', 1)[-1]
        activations = {}
        for test, entry in pending.items():
            parents = [item.get('parents', []) for item in entry['occurrences']]
            if any(value != parents[0] for value in parents[1:]):
                raise ValueError('quarantine test ancestry is ambiguous')
            activations[test] = {'activation': entry['key'], 'parents': parents[0]}
        packet = prepare_bundle(PUBLIC_REPOSITORY, self.checker, FLEET_CHECKER_SHA256, directory,
                                selected, activations=activations)
        if packet['state'] == 'already_present':
            raise ValueError('list changed while preparing relay; refresh required')
        return packet['bundle']

    def send_relay(self, test, entry):
        payload = {'test': test, **entry['decision'],
                   'occurrences': [item['source'] for item in entry['occurrences']]}
        self._notify(self._card_key(test, payload['item']), 'Flaky test quarantine relay',
                     self._message(payload, entry['relay'], 'Relay ready'))

    def notify(self, key, payload):
        self._notify(self._card_key(payload['test'], payload['item']), 'Flaky test quarantined',
                     self._message(payload, payload.get('relay'), 'Quarantine activated'))

    def alert(self, key, message):
        self._notify('flaky-test-quarantine-alert:' + hashlib.sha256(key.encode()).hexdigest(),
                     'Flaky test quarantine needs attention', message)

    def scorecard_alert(self, key, message):
        self._notify(key, 'Scorecard work needs front desk triage', message, producer='release-scorecard')

    def verify_restoration(self, test, restoration):
        valid = bool(self.restoration_verifier and self.restoration_verifier(test, restoration))
        if valid:
            self.verified_restorations[test] = restoration
        else:
            self.verified_restorations.pop(test, None)
        return valid
