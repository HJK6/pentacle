"""Front-desk routing recovery after seat resume (Addendum A of
spec_pentacle__daemon_restart_continuity_2026_10).

Real `main.py`, real pane death plus daemon restart, real `agent-orch spawn
--resume`, real `--handoff` and real `assistant rebind`; see
`fd_resume_matrix.py`. Explicitly run, never part of the unit gate:

    PENTACLE_FORCE_LIVE_DAEMON=1 python3 -m pytest tests/soak/test_fd_resume_recovery.py -q

Set FD_RESUME_EVIDENCE=<dir> to keep each journey's records, timeline and
daemon log outside pytest's tmp.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.soak import fd_resume_matrix as fm

pytestmark = [pytest.mark.live_daemon, pytest.mark.timeout(1800)]

ROLES = {"role_lead": fm.FD_ROLE, "role_unset": None}


@pytest.fixture
def evidence(tmp_path, request):
    root = os.environ.get("FD_RESUME_EVIDENCE")
    path = Path(root) / request.node.name if root else tmp_path / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _assert_pass(records: list[dict]) -> None:
    failed = [{k: r.get(k) for k in ("cell", "assistant_role", "variant", "classification", "failed_checks")}
              for r in records if r.get("classification") != fm.PASS]
    assert not failed, json.dumps(failed, default=str)


@pytest.mark.parametrize("role", ROLES, ids=list(ROLES))
def test_self_resume_a0_a1_a2_a3(role, tmp_path, evidence):
    """A0 (role unset), then A2 and A3 on the resumed seat; A1 (role == FD row
    role) is the protected-row control: resume refused, row preserved."""
    _assert_pass(fm.run_self_resume(tmp_path / "env", evidence, assistant_role=ROLES[role]))


@pytest.mark.parametrize("role", ROLES, ids=list(ROLES))
def test_actual_fd_shape_a1b(role, tmp_path, evidence):
    _assert_pass(fm.run_actual_fd_shape(tmp_path / "env", evidence, assistant_role=ROLES[role]))


@pytest.mark.parametrize("variant", ["spawn_explicit", "handoff_inherited"])
def test_successor_recovery_a4_to_a8(variant, tmp_path, evidence):
    """The deployed shape: PENTACLE_ASSISTANT_ROLE unset (the Thoth readback), so
    the FD row is unprotected and resume opens a new generation. A protected row
    refuses resume (the A1 control above), so A4-A8 do not apply to it."""
    _assert_pass(fm.run_handoff_recovery(tmp_path / "env", evidence, assistant_role=None, variant=variant))
