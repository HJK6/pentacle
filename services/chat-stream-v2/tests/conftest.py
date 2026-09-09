"""Public test bootstrap for the pure chat-stream examples."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


SERVICE_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
SERVICES_ROOT = SERVICE_DIR.parent
AGENT_ORCH_DIR = SERVICES_ROOT / "agent-orch"
TOOLS_DIR = SERVICE_DIR / "tools"
PUBLIC_SHARED_DIR = Path(__file__).resolve().parents[2] / "_shared"

for _path in reversed((SERVICE_DIR, TESTS_DIR, SERVICES_ROOT, AGENT_ORCH_DIR, TOOLS_DIR, PUBLIC_SHARED_DIR)):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)


@pytest.fixture(scope="session", autouse=True)
def _public_test_environment():
    """Keep the fixture namespace explicit without installing runtime hooks."""
    yield


def pytest_configure(config) -> None:
    del config
