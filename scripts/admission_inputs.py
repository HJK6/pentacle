"""Load only accepted-base attestations and validate quarantine history."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess

ROLLOUT_BASE = 'f3a76f479e14a390cd3d61234e1fd6e8a7d228d1'
ROLLOUT_INVENTORY_SHA256 = '5d2625e391425a73bbbe39ee1bdc0ecfafbd6b6211be465e604ca536dec4333f'
INVENTORY = 'scripts/test-admission-inventory.json'
RECORDS = 'scripts/test-admission-records.json'
QUARANTINE = 'scripts/quarantine.json'
HEX = re.compile(r'^[a-f0-9]{64}$')


def _git(root, *args):
    try:
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('accepted Git base unavailable') from exc


def _file(root, ref, path):
    names = _git(root, 'ls-tree', '-r', '--name-only', ref, '--', path).decode().splitlines()
    return _git(root, 'show', ref + ':' + path) if names else None


def _parse(raw, name):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(name + ' unreadable') from exc
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError(name + ' invalid')
    return value


def _history(data):
    active = data.get('nonblocking_tests')
    history = data.get('history')
    if (not isinstance(active, list) or any(not isinstance(test, str) or '::' not in test for test in active)
            or len(set(active)) != len(active) or not isinstance(history, dict)):
        raise ValueError('quarantine inventory invalid')
    for key, entry in history.items():
        if (not HEX.fullmatch(str(key)) or not isinstance(entry, dict)
                or not isinstance(entry.get('activation'), str) or not entry['activation']
                or not HEX.fullmatch(str(entry.get('file', '')))
                or not isinstance(entry.get('parents'), list)
                or any(not isinstance(parent, str) or not parent for parent in entry['parents'])):
            raise ValueError('quarantine history invalid')
    for test in active:
        key = hashlib.sha256(test.encode()).hexdigest()
        raw = test.split('::', 1)[1] if test.startswith(('daemon::', 'cli::', 'shared::')) else test
        file_hash = hashlib.sha256(raw.split('::', 1)[0].encode()).hexdigest()
        if key not in history or history[key]['file'] != file_hash:
            raise ValueError('active quarantine history missing')
    return active, history


def load_inputs(repo):
    root = Path(repo).resolve()
    base = _git(root, 'merge-base', 'HEAD', 'refs/remotes/origin/main').decode().strip()
    if not re.fullmatch(r'[a-f0-9]{40}', base):
        raise ValueError('accepted Git base invalid')
    raw_inventory = _file(root, base, INVENTORY)
    rollout = raw_inventory is None
    if raw_inventory is None:
        try:
            subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', ROLLOUT_BASE, base],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('rollout base unavailable') from exc
        raw_inventory = (root / INVENTORY).read_bytes()
        if hashlib.sha256(raw_inventory).hexdigest() != ROLLOUT_INVENTORY_SHA256:
            raise ValueError('rollout inventory identity mismatch')
    legacy = _parse(raw_inventory, 'admission inventory')
    if legacy.get('base_sha') != ROLLOUT_BASE or not isinstance(legacy.get('python'), dict) or not isinstance(legacy.get('javascript'), dict):
        raise ValueError('admission inventory invalid')
    raw_records = _file(root, base, RECORDS)
    if raw_records is None:
        if not rollout:
            raise ValueError('accepted admission attestations unavailable')
        records = {}
    else:
        records_data = _parse(raw_records, 'admission attestations')
        records = records_data.get('records')
        if not isinstance(records, dict):
            raise ValueError('admission attestations invalid')
    current = _parse((root / QUARANTINE).read_bytes(), 'quarantine inventory')
    active, history = _history(current)
    prior_raw = _file(root, base, QUARANTINE)
    if prior_raw is not None:
        previous = _parse(prior_raw, 'accepted quarantine inventory')
        old_active, old_history = _history(previous)
        if any(key not in history for key in old_history):
            raise ValueError('quarantine history was removed')
        old_keys = {hashlib.sha256(test.encode()).hexdigest() for test in old_active}
        new_keys = {hashlib.sha256(test.encode()).hexdigest() for test in active}
        if any(history[key] != old_history[key] for key in old_keys & new_keys):
            raise ValueError('active quarantine history was rewritten')
    return legacy, records, current
