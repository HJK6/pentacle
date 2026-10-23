"""The gate progress journal survives hard exits and folds into bounded facts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tools import gate_progress


SERVICE_DIR = Path(__file__).resolve().parents[1]
SUITE = '''
import os, time, pytest

@pytest.fixture
def broken():
    raise RuntimeError("setup boom")

def test_pass():
    pass

def test_fail():
    assert 1 == 2

def test_setup_error(broken):
    pass

@pytest.mark.skip(reason="synthetic")
def test_skip():
    pass

@pytest.mark.parametrize("payload", ["x" * 400])
def test_long_param(payload):
    pass
'''
HANG = '''
import time

def test_first():
    pass

def test_hangs():
    time.sleep(30)

def test_never_reached():
    pass
'''


def _run_suite(tmp_path: Path, source: str, *extra: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "pytest.ini").write_text("[pytest]\ntimeout = 2\ntimeout_method = thread\n", encoding="utf-8")
    (suite / "test_synthetic.py").write_text(source, encoding="utf-8")
    journal = tmp_path / "progress.jsonl"
    env = {**os.environ, "PYTHONPATH": str(SERVICE_DIR)}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tools.gate_progress",
                           f"--gate-progress-journal={journal}", "--gate-progress-run-id=run-1:unit", *extra],
                          cwd=suite, env=env, capture_output=True, text=True, timeout=45)
    return proc, journal


def test_journal_records_outcomes_and_completes(tmp_path: Path) -> None:
    proc, journal = _run_suite(tmp_path, SUITE)
    assert proc.returncode == 1, proc.stdout
    summary = gate_progress.summarize(journal)
    assert summary["complete"] is True and summary["run_id"] == "run-1:unit"
    assert summary["collected"] == 5 and summary["completed_nodes"] == 5 and summary["active_nodes"] == []
    assert {"node": "test_synthetic.py::test_fail", "phase": "call"} in summary["failed"]
    assert {"node": "test_synthetic.py::test_setup_error", "phase": "setup"} in summary["failed"]
    assert summary["exitstatus"] == 1
    raw = journal.read_text(encoding="utf-8")
    assert "x" * 100 not in raw and "setup boom" not in raw  # no payloads or error text


def test_per_test_timeout_hard_exit_keeps_last_started_node(tmp_path: Path) -> None:
    proc, journal = _run_suite(tmp_path, HANG)
    assert proc.returncode != 0
    summary = gate_progress.summarize(journal)
    assert summary["complete"] is False and summary["run_finished"] is False
    assert summary["last_completed"] == "test_synthetic.py::test_first"
    assert summary["active_nodes"] == ["test_synthetic.py::test_hangs"]
    assert summary["truncated_tail"] is False and summary["malformed_lines"] == 0


def test_journal_budget_exhaustion_is_explicit(tmp_path: Path) -> None:
    _proc, journal = _run_suite(tmp_path, SUITE, "--gate-progress-max-bytes=700")
    summary = gate_progress.summarize(journal)
    assert summary["budget_exhausted"] is True and summary["complete"] is False
    assert journal.stat().st_size <= 700


def test_summarize_reports_missing_malformed_and_truncated(tmp_path: Path) -> None:
    missing = gate_progress.summarize(tmp_path / "absent.jsonl")
    assert missing["present"] is False and missing["complete"] is False
    damaged = tmp_path / "damaged.jsonl"
    rows = [{"seq": 1, "kind": "run_start", "t": 0.0, "run_id": "r"}, {"seq": 2, "kind": "start", "t": 0.1, "node": "a::b"}]
    damaged.write_text("".join(json.dumps(row) + "\n" for row in rows) + "not json\n" + '{"seq": 3, "kind": "fin', encoding="utf-8")
    summary = gate_progress.summarize(damaged)
    assert summary["malformed_lines"] == 2 and summary["truncated_tail"] is True
    assert summary["active_nodes"] == ["a::b"] and summary["complete"] is False
    big = tmp_path / "big.jsonl"
    big.write_text("".join(json.dumps({"seq": i, "kind": "start", "t": 0, "node": f"n{i}"}) + "\n" for i in range(200)), encoding="utf-8")
    bounded = gate_progress.summarize(big, max_bytes=1000, max_listed=5)
    assert bounded["truncated_tail"] is True and len(bounded["active_nodes"]) == 5


def test_bounded_nodeid_hides_long_parameters() -> None:
    node = gate_progress.bounded_nodeid("t.py::test_x[" + "p" * 500 + "]")
    assert len(node) < 120 and node.startswith("t.py::test_x[ppp") and "#" in node
    assert gate_progress.bounded_nodeid("t.py::test_x[short]") == "t.py::test_x[short]"
    assert len(gate_progress.bounded_nodeid("t.py::" + "y" * 1000)) <= gate_progress.NODEID_MAX + 20
