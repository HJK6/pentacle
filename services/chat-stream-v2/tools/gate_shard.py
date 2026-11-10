"""Explicit, serial, whole-file unit partitions with source-collection evidence.

Loaded only on run_gate's unit argv. No environment options are inherited by
nested pytest processes, and the ordinary progress journal is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from tools.gate_progress import bounded_nodeid

SCHEMA = "pentacle.v2.gate-shard-manifest.v1"
ASSIGNMENT = "sha256(nodeid file prefix) mod shard_count"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("gate-shard")
    group.addoption("--gate-shard-index", type=int)
    group.addoption("--gate-shard-count", type=int)
    group.addoption("--gate-shard-manifest")


def pytest_configure(config: pytest.Config) -> None:
    index, count = config.getoption("gate_shard_index"), config.getoption("gate_shard_count")
    if (type(index) is not int or type(count) is not int or count < 2 or not 0 <= index < count
            or not config.getoption("gate_shard_manifest") or config.getoption("numprocesses", default=0)
            or hasattr(config, "workerinput")):
        raise pytest.UsageError("gate shards require an index/count pair, count >= 2, a manifest and serial pytest")


def _digest(node: str) -> str:
    return hashlib.sha256(node.encode("utf-8", "replace")).hexdigest()


def _collection(nodes: list[str]) -> dict:
    return {"count": len(nodes),
            "nodeids_sha256": hashlib.sha256(b"".join(n.encode("utf-8", "replace") + b"\n" for n in nodes)).hexdigest(),
            "files": sorted({node.split("::", 1)[0] for node in nodes})}


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    index, count = config.getoption("gate_shard_index"), config.getoption("gate_shard_count")
    nodes = [item.nodeid for item in items]
    kept, dropped = [], []
    for item in items:
        file = item.nodeid.split("::", 1)[0]
        cell = int(hashlib.sha256(file.encode("utf-8")).hexdigest(), 16) % count
        (kept if cell == index else dropped).append(item)
    if not kept:
        raise pytest.UsageError("gate shard has an empty assignment")
    selected = [item.nodeid for item in kept]
    data = {"schema": SCHEMA, "shard_index": index, "shard_count": count, "assignment": ASSIGNMENT,
            "rootpath": str(config.rootpath),
            "eligible": {**_collection(nodes), "node_digests": sorted(_digest(node) for node in nodes)},
            "assigned": {**_collection(selected),
                         "nodes": [{"node": bounded_nodeid(node), "sha256": _digest(node)} for node in selected]}}
    try:
        # Refuse reuse of another assignment. A new owned gate always has a fresh evidence root.
        fd = os.open(config.getoption("gate_shard_manifest"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise pytest.UsageError(f"cannot write gate shard manifest ({type(exc).__name__})") from exc
    config.hook.pytest_deselected(items=dropped)
    items[:] = kept
