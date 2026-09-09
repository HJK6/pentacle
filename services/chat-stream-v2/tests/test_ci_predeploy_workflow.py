"""Static contract for the hosted hermetic v2 predeploy tier."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PREDEPLOY = ROOT / ".github" / "workflows" / "predeploy-tests.yml"


def test_predeploy_runs_only_the_v2_gate_and_agent_orch_suite() -> None:
    workflow = PREDEPLOY.read_text(encoding="utf-8")

    assert "services/chat-stream-v2/tools/run_gate.py merge" in workflow
    assert "services/agent-orch/tests" in workflow
    assert "services/chat-stream/tests" not in workflow
    assert "services/chat-stream/harness" not in workflow
    assert "${GITHUB_WORKSPACE}/services/chat-stream:" not in workflow


def test_predeploy_uses_the_shared_services_import_root() -> None:
    workflow = PREDEPLOY.read_text(encoding="utf-8")

    assert "${GITHUB_WORKSPACE}/services:${GITHUB_WORKSPACE}/services/agent-orch" in workflow
