from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SERVICES_ROOT = ROOT.parent
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))

import _ao_basetemp  # noqa: E402
from _shared import xfail_lint  # noqa: E402
from _shared.test_resource_ledger import ResourceLedger, resource_cleanup_audit  # noqa: E402


def pytest_collection_modifyitems(config, items):
    """Apply generic collection checks for the public test suite."""
    xfail_lint.pytest_collection_modifyitems(config, items)


@pytest.fixture(autouse=True)
def _scrub_ambient_stream_id_env(monkeypatch):
    """Keep stream identity tests hermetic when run inside another process."""
    monkeypatch.delenv("AGENT_ORCH_STREAM_ID", raising=False)
    monkeypatch.delenv("PENTACLE_STREAM_ID", raising=False)


@pytest.fixture
def resource_ledger(request):
    """Audit resources created by a test using synthetic host labels."""
    marker = request.node.get_closest_marker("resource_ledger_remote_hosts")
    hosts = tuple(str(host) for host in (marker.args if marker else ()))
    ledger = ResourceLedger(test_id=request.node.nodeid)
    with resource_cleanup_audit(ledger, remote_hosts=hosts):
        yield ledger


@pytest.fixture
def af_unix_safe_workspace():
    """Provide a short-lived workspace for tests that exercise Unix sockets."""
    base = Path(tempfile.mkdtemp(prefix="public-test-", dir="/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


_TMPDIR_OVERRIDDEN = False
_CREATED_BASETEMP: str | None = None


def pytest_configure(config):
    """Use a short temporary root so socket-path tests work consistently."""
    global _TMPDIR_OVERRIDDEN, _CREATED_BASETEMP
    xfail_lint.pytest_configure(config)
    config.addinivalue_line(
        "markers",
        "resource_ledger_remote_hosts(*hosts): optional synthetic host labels for resource audits",
    )
    if _TMPDIR_OVERRIDDEN:
        return
    _TMPDIR_OVERRIDDEN = True
    is_controller = getattr(config, "workerinput", None) is None
    if is_controller and not getattr(config.option, "basetemp", None):
        short_base = _ao_basetemp.create_basetemp()
        config.option.basetemp = short_base
        _CREATED_BASETEMP = short_base
        if not _ao_basetemp.keep_tmp():
            _ao_basetemp._prune_stale_ao_basetemps(current_basetemp=short_base)


def pytest_sessionfinish(session, exitstatus):
    del session, exitstatus
    global _CREATED_BASETEMP
    if _CREATED_BASETEMP and not _ao_basetemp.keep_tmp():
        _ao_basetemp.cleanup_basetemp(_CREATED_BASETEMP)
        _CREATED_BASETEMP = None
