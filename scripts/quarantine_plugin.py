"""Select exact quarantined pytest cases and record tier membership."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

import pytest
from _pytest.mark import MarkMatcher, KeywordMatcher
from _pytest.mark.expression import Expression
from quarantine_list import ExpiredQuarantine, load_quarantine, mark_quarantined

SUITES = {'daemon': 'services/chat-stream-v2', 'cli': 'services/agent-orch', 'shared': 'services/_shared'}


def pytest_addoption(parser):
    group = parser.getgroup('quarantine')
    group.addoption('--quarantine-root', required=True)
    group.addoption('--quarantine-suite', choices=tuple(SUITES), required=True)
    group.addoption('--quarantine-manifest', required=True)
    group.addoption('--quarantine-separate-marker', action='append', default=[], choices=('repin_exhaustive',))


def pytest_configure(config):
    config.addinivalue_line('markers', 'quarantined: exact case in the quarantine list')
    try:
        config._quarantine_ids = {row['id'] for row in load_quarantine(config.getoption('quarantine_root'))['nonblocking_tests']}
        config._quarantine_error = None
        config._quarantine_error_kind = None
    except ExpiredQuarantine as exc:
        config._quarantine_ids = {row['id'] for row in exc.data['nonblocking_tests']}
        config._quarantine_error = str(exc)
        config._quarantine_error_kind = 'expired'
        print(f'quarantine inventory unavailable: {exc}', flush=True)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        config._quarantine_ids = set()
        config._quarantine_error = str(exc)
        config._quarantine_error_kind = 'invalid'
        print(f'quarantine inventory unavailable: {exc}', flush=True)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    separate, kept, removed, blocking, quarantined = {}, [], [], [], []
    suite = config.getoption('quarantine_suite')
    for item in items:
        marker = next((name for name in config.getoption('quarantine_separate_marker') if item.get_closest_marker(name)), None)
        if marker:
            separate[item.nodeid] = marker
            removed.append(item)
        else:
            kept.append(item)
    if removed:
        config.hook.pytest_deselected(items=removed)
        items[:] = kept
    mark_quarantined(items, suite, config._quarantine_ids)
    markexpr = getattr(config.option, 'markexpr', '')
    keyword = getattr(config.option, 'keyword', '')
    marker_filter = Expression.compile(markexpr) if markexpr else None
    keyword_filter = Expression.compile(keyword) if keyword else None
    for item in items:
        matcher = MarkMatcher.from_markers(item.iter_markers())
        if marker_filter and not marker_filter.evaluate(lambda name, **kwargs: False if name == 'quarantined' else matcher(name, **kwargs)):
            continue
        if keyword_filter and not keyword_filter.evaluate(KeywordMatcher.from_item(item)):
            continue
        index = getattr(config.option, 'gate_shard_index', None)
        count = getattr(config.option, 'gate_shard_count', None)
        if index is not None and count and int(hashlib.sha256(item.nodeid.encode()).hexdigest(), 16) % count != index:
            continue
        (quarantined if item.get_closest_marker('quarantined') else blocking).append(item.nodeid)
    destination = Path(config.getoption('quarantine_manifest'))
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.quarantine-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'complete': config._quarantine_error is None, 'error': config._quarantine_error,
                           'error_kind': config._quarantine_error_kind, 'blocking': blocking, 'quarantined': quarantined, 'separate_tiers': separate}, stream)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError as exc:
        config._quarantine_error = str(exc)


def pytest_sessionfinish(session, exitstatus):
    if session.config._quarantine_error and exitstatus in (0, 5):
        session.exitstatus = 1
