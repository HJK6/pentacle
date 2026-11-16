from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("PENTACLE_ALLOW_XFAIL") == "1":
        return

    offenders = [item for item in items if any(item.iter_markers(name="xfail"))]
    if not offenders:
        return

    locations = "\n".join(f"- {item.nodeid}" for item in offenders)
    raise pytest.UsageError(
        "xfail_forbidden: Pentacle chat-stream and agent-orch tests have a zero-xfail policy. "
        "Set PENTACLE_ALLOW_XFAIL=1 only for local debugging.\n"
        f"{locations}"
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "xfail: forbidden in Pentacle chat-stream and agent-orch tests; use an active work item plus a failing test",
    )
