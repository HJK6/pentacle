"""Partition unit tests deterministically by file path."""
from __future__ import annotations

import hashlib

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("gate-shard")
    group.addoption("--gate-shard-index", type=int)
    group.addoption("--gate-shard-count", type=int)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    index, count = config.getoption("gate_shard_index"), config.getoption("gate_shard_count")
    kept, dropped = [], []
    for item in items:
        file = item.nodeid.split("::", 1)[0]
        cell = int(hashlib.sha256(file.encode("utf-8")).hexdigest(), 16) % count
        (kept if cell == index else dropped).append(item)
    config.hook.pytest_deselected(items=dropped)
    items[:] = kept
