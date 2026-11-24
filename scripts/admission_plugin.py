"""Pytest gate admission, with a retained list of nonblocking cases."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

from admission_fingerprint import python_fingerprint
from admission_inputs import load_inputs
from admission_policy import admission

SUITES = {'daemon': 'services/chat-stream-v2', 'cli': 'services/agent-orch', 'shared': 'services/_shared'}


def pytest_addoption(parser):
    group = parser.getgroup('test-admission')
    group.addoption('--admission-root', required=True)
    group.addoption('--admission-suite', choices=tuple(SUITES), required=True)
    group.addoption('--admission-manifest', required=True)
    group.addoption('--admission-separate-marker', action='append', default=[],
                    choices=('soak', 'repin_exhaustive'),
                    help='record cases owned by this separate tier before admission')


def pytest_configure(config):
    config.addinivalue_line('markers', 'admission_hold: test awaits reviewed blocking-tier admission')
    config.addinivalue_line('markers', 'quarantined: test is in the effective quarantine list')
    config._admission_error = None
    try:
        platform = 'darwin' if sys.platform == 'darwin' else 'linux' if sys.platform.startswith('linux') else None
        if platform is None:
            raise ValueError('unsupported test platform')
        config._admission_inputs = (*load_inputs(config.getoption('admission_root')), platform)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        config._admission_inputs = None
        config._admission_error = type(exc).__name__


def _write_manifest(config, payload):
    destination = Path(config.getoption('admission_manifest'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.admission-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, sort_keys=True)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    held = []
    separate_tiers = {}
    markers = config.getoption('admission_separate_marker')
    if markers:
        kept, separate = [], []
        for item in items:
            marker = next((name for name in markers if item.get_closest_marker(name)), None)
            if marker:
                separate_tiers[item.nodeid] = marker
                separate.append(item)
            else:
                kept.append(item)
        config.hook.pytest_deselected(items=separate)
        items[:] = kept
    if config._admission_inputs is not None:
        legacy, records, quarantine, platform = config._admission_inputs
        suite = config.getoption('admission_suite')
        root = Path(config.getoption('admission_root')).resolve() / SUITES[suite]
        index = getattr(config.option, 'gate_shard_index', None)
        count = getattr(config.option, 'gate_shard_count', None)
        for item in items:
            try:
                fingerprint = python_fingerprint(root, item.nodeid)
                state, reason = admission(suite, item.nodeid, fingerprint, platform, legacy, records, quarantine)
            except (OSError, ValueError, TypeError, KeyError, SyntaxError):
                state, reason = 'advisory', 'test or fixture fingerprint unavailable'
            if state == 'advisory':
                item.add_marker(pytest.mark.admission_hold)
                file = item.nodeid.split('::', 1)[0]
                if index is None or (count and int(hashlib.sha256(file.encode()).hexdigest(), 16) % count == index):
                    held.append({'id': item.nodeid, 'reason': reason})
                    print(f'ADMISSION ADVISORY {item.nodeid}: {reason}', flush=True)
            elif state == 'quarantine':
                item.add_marker(pytest.mark.quarantined)
    try:
        _write_manifest(config, {'complete': config._admission_error is None, 'error': config._admission_error,
                                 'held': held, 'separate_tiers': separate_tiers})
    except OSError:
        config._admission_error = 'manifest_write_failed'


def pytest_sessionfinish(session, exitstatus):
    if session.config._admission_error and not exitstatus:
        session.exitstatus = 1
