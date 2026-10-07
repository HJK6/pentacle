"""Daemon restart-continuity matrix (spec_pentacle__daemon_restart_continuity_2026_10).

Real `main.py` processes, killed mid-spawn or mid-await and restarted on the
same DB; see `restart_matrix.py`. Explicitly run, never part of the unit gate:

    PENTACLE_FORCE_LIVE_DAEMON=1 python3 -m pytest tests/soak/test_restart_continuity.py -q

Set RESTART_MATRIX_EVIDENCE=<dir> to keep each cell's raw evidence (records,
timelines, sqlite snapshots, pane captures, daemon logs) outside pytest's tmp.
"""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from tests.soak import restart_matrix as rm

pytestmark = [pytest.mark.live_daemon, pytest.mark.timeout(1800)]

SIGNALS = [signal.SIGTERM, signal.SIGKILL]


@pytest.fixture
def evidence(tmp_path, request):
    root = os.environ.get("RESTART_MATRIX_EVIDENCE")
    path = Path(root) / request.node.name if root else tmp_path / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _assert_pass(record: dict) -> None:
    detail = {k: record.get(k) for k in ("cell", "classification", "failed_checks", "detail", "outcome")}
    assert record["classification"] == rm.PASS, json.dumps(detail, default=str)


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
@pytest.mark.parametrize("stage", ["S1", "S2", "S4"])
def test_daemon_stage_restart(stage, sig, tmp_path, evidence):
    _assert_pass(rm.run_daemon_cell(stage, sig, tmp_path / "env", evidence))


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_daemon_s3_both_windows(sig, tmp_path, evidence):
    records = rm.run_s3(sig, tmp_path, evidence)
    assert [r["cell"] for r in records] == [f"S3a-{sig.name}", f"S3b-{sig.name}"]
    for record in records:
        _assert_pass(record)


def test_c1_await_across_outage(tmp_path, evidence):
    _assert_pass(rm.run_c1(tmp_path / "env", evidence))


@pytest.mark.parametrize("variant", ["pre_admission", "post_admission"])
def test_c2_keyed_spawn_across_restart(variant, tmp_path, evidence):
    _assert_pass(rm.run_c2(tmp_path / "env", evidence, variant=variant))


def test_c3_report_across_restart(tmp_path, evidence):
    _assert_pass(rm.run_c3(tmp_path / "env", evidence))


def test_c4_retro_run_across_restart(tmp_path, evidence):
    _assert_pass(rm.run_c4(tmp_path / "env", evidence))


def test_c4b_retro_run_daemon_stays_down(tmp_path, evidence):
    _assert_pass(rm.run_c4(tmp_path / "env", evidence, stay_down=True))
