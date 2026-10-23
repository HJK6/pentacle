"""Bounded per-run progress journal for one v2 gate tier (pytest plugin).

run_gate.py loads this with ``-p tools.gate_progress --gate-progress-journal=PATH``;
the option exists only on that subject's argv, so nested pytest processes never
inherit it. Each record is one JSON line written unbuffered (short writes
completed) to an O_APPEND descriptor, so a pytest-timeout thread hard exit
(``os._exit``) leaves every prior record plus the last started node on disk.

Only identities, phases, outcomes and monotonic times are recorded: never test
output, locals, environment or full parameter payloads.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

import pytest


SCHEMA = "pentacle.v2.gate-progress.v1"
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
NODEID_MAX = 300
PARAM_MAX = 48
# Bound before any test runs: suites monkeypatch os.write/os.open (short-write and
# disk-full controls), and the journal must not share those patches.
_OPEN, _WRITE, _CLOSE = os.open, os.write, os.close


def bounded_nodeid(nodeid: str) -> str:
    """Keep a node id recognisable without carrying a full parameter payload."""
    base, bracket, params = nodeid.partition("[")
    if bracket and len(params) > PARAM_MAX:
        digest = hashlib.sha256(params.encode("utf-8", "replace")).hexdigest()[:12]
        nodeid = f"{base}[{params[:PARAM_MAX]}...#{digest}]"
    if len(nodeid) > NODEID_MAX:
        digest = hashlib.sha256(nodeid.encode("utf-8", "replace")).hexdigest()[:12]
        nodeid = f"{nodeid[:NODEID_MAX]}...#{digest}"
    return nodeid


class ProgressJournal:
    def __init__(self, path: str, run_id: str, max_bytes: int) -> None:
        self.run_id, self.max_bytes = run_id, max_bytes
        self.started = time.monotonic()
        self.seq = self.written = 0
        self.exhausted = False
        self.fd: int | None = _OPEN(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_TRUNC, 0o600)

    def emit(self, kind: str, **fields: Any) -> None:
        if self.fd is None:
            return
        self.seq += 1
        record = {"seq": self.seq, "kind": kind, "t": round(time.monotonic() - self.started, 6), **fields}
        line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        # Reserve room for the one budget-exhausted marker so the cut is explicit.
        if self.written + len(line) > self.max_bytes - 256:
            line = (json.dumps({"seq": self.seq, "kind": "journal_budget_exhausted", "t": record["t"]},
                               sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            self.exhausted = True
        try:
            view = memoryview(line)
            while view:
                view = view[_WRITE(self.fd, view):]
            self.written += len(line)
        except OSError:
            self.close()
            return
        if self.exhausted:
            self.close()

    def close(self) -> None:
        if self.fd is not None:
            try:
                _CLOSE(self.fd)
            except OSError:
                pass
            self.fd = None


# logstart/logreport/logfinish hooks receive no config, so the active journal lives here.
_ACTIVE: list[ProgressJournal] = []


def _journal() -> ProgressJournal | None:
    return _ACTIVE[0] if _ACTIVE else None


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("gate-progress")
    group.addoption("--gate-progress-journal", default=None, help="runner-owned progress journal path")
    group.addoption("--gate-progress-run-id", default="", help="runner-owned run identity")
    group.addoption("--gate-progress-max-bytes", type=int, default=DEFAULT_MAX_BYTES)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("--gate-progress-journal")
    if not path or _ACTIVE:
        return
    journal = ProgressJournal(path, config.getoption("--gate-progress-run-id"), config.getoption("--gate-progress-max-bytes"))
    _ACTIVE.append(journal)
    journal.emit("run_start", schema=SCHEMA, run_id=journal.run_id, pid=os.getpid())


def pytest_collection_finish(session: pytest.Session) -> None:
    journal = _journal()
    if journal is None:
        return
    digest = hashlib.sha256()
    for item in session.items:
        digest.update(item.nodeid.encode("utf-8", "replace") + b"\n")
    journal.emit("collected", count=len(session.items), nodeids_sha256=digest.hexdigest())


def pytest_runtest_logstart(nodeid: str, location: object) -> None:
    if (journal := _journal()) is not None:
        journal.emit("start", node=bounded_nodeid(nodeid))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if (journal := _journal()) is not None:
        journal.emit("phase", node=bounded_nodeid(report.nodeid), phase=report.when, outcome=report.outcome,
                     duration=round(float(report.duration), 6))


def pytest_runtest_logfinish(nodeid: str, location: object) -> None:
    if (journal := _journal()) is not None:
        journal.emit("finish", node=bounded_nodeid(nodeid))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if (journal := _journal()) is not None:
        journal.emit("run_finish", exitstatus=int(exitstatus))


def pytest_unconfigure(config: pytest.Config) -> None:
    if (journal := _journal()) is not None:
        journal.close()
        _ACTIVE.clear()


def summarize(path: os.PathLike[str] | str, *, max_bytes: int = DEFAULT_MAX_BYTES, max_listed: int = 50) -> dict[str, Any]:
    """Fold a journal into bounded facts; never raises for a missing or damaged file."""
    summary: dict[str, Any] = {"journal": str(path), "present": False, "records": 0, "malformed_lines": 0,
                               "truncated_tail": False, "budget_exhausted": False, "run_started": False,
                               "run_finished": False, "exitstatus": None, "collected": None, "started_nodes": 0,
                               "completed_nodes": 0, "active_nodes": [], "failed": [], "failed_overflow": 0,
                               "last_started": None, "last_completed": None, "elapsed_s": None, "complete": False}
    try:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        summary["read_error"] = type(exc).__name__
        return summary
    summary["present"] = True
    if len(data) > max_bytes:
        data, summary["truncated_tail"] = data[:max_bytes], True
    lines = data.split(b"\n")
    if lines and lines[-1]:
        summary["truncated_tail"] = True  # final record never got its newline
    active: dict[str, None] = {}
    for raw in lines:
        if not raw:
            continue
        try:
            record = json.loads(raw)
            kind = record["kind"]
        except (ValueError, KeyError, TypeError):
            summary["malformed_lines"] += 1
            continue
        summary["records"] += 1
        if isinstance(record.get("t"), (int, float)):
            summary["elapsed_s"] = record["t"]
        node = record.get("node")
        if kind == "run_start":
            summary["run_started"], summary["run_id"] = True, record.get("run_id")
        elif kind == "collected":
            summary["collected"] = record.get("count")
        elif kind == "start" and node:
            summary["started_nodes"] += 1
            active[node] = None
            summary["last_started"] = node
        elif kind == "phase" and node and record.get("outcome") == "failed":
            if len(summary["failed"]) < max_listed:
                summary["failed"].append({"node": node, "phase": record.get("phase")})
            else:
                summary["failed_overflow"] += 1
        elif kind == "finish" and node:
            summary["completed_nodes"] += 1
            active.pop(node, None)
            summary["last_completed"] = node
        elif kind == "run_finish":
            summary["run_finished"], summary["exitstatus"] = True, record.get("exitstatus")
        elif kind == "journal_budget_exhausted":
            summary["budget_exhausted"] = True
    summary["active_nodes"] = list(active)[:max_listed]
    summary["complete"] = (summary["run_started"] and summary["run_finished"] and not active
                           and not summary["malformed_lines"] and not summary["truncated_tail"]
                           and not summary["budget_exhausted"])
    return summary
