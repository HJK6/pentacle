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
ROLLOUT_SEEDS = (
    'test/web_server.test.js::two concurrent connections keep their local tmux sessions isolated',
    'tests/test_store_call_diag.py::test_slow_ordinary_actor_and_internal_calls_each_log_one_named_line',
    'tests/test_tmux_observation_delimiter.py::test_private_tmux_terminal_pane_refuses_anchor_and_preserves_foreign_sentinel[status]',
)


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


def _base(root):
    base = _git(root, 'merge-base', 'HEAD', 'refs/remotes/origin/main').decode().strip()
    if not re.fullmatch(r'[a-f0-9]{40}', base):
        raise ValueError('accepted Git base invalid')
    return base


def load_quarantine(repo, base=None):
    """Use accepted-base exclusions; candidate additions are never effective."""
    import sys
    root = Path(repo).resolve()
    current = _parse((root / QUARANTINE).read_bytes(), 'quarantine inventory')
    current_active, _ = _history(current)
    base = base or _base(root)
    prior_raw = _file(root, base, QUARANTINE)
    if prior_raw is None:
        try:
            subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', ROLLOUT_BASE, base],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('rollout quarantine base unavailable') from exc
        history = {}
        for test in ROLLOUT_SEEDS:
            history[hashlib.sha256(test.encode()).hexdigest()] = {
                'activation': 'seed-pr606',
                'file': hashlib.sha256(test.split('::', 1)[0].encode()).hexdigest(), 'parents': []}
        accepted = {'version': 1, 'nonblocking_tests': list(ROLLOUT_SEEDS), 'history': history}
    else:
        accepted = _parse(prior_raw, 'accepted quarantine inventory')
        _history(accepted)
    pending = sorted(set(current_active) - set(accepted['nonblocking_tests']))
    for test in pending:
        print('QUARANTINE PENDING, NOT ON BASE: ' + test, file=sys.stderr, flush=True)
    return {**accepted, 'pending': pending}


def load_inputs(repo):
    root = Path(repo).resolve()
    base = _base(root)
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
    return legacy, records, load_quarantine(root, base)
