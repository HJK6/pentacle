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


def test_ci_unit_budget_and_failure_artifacts_keep_scratch_separate():
    import yaml
    workflow = yaml.safe_load(PREDEPLOY.read_text())
    job = workflow['jobs']['checks']
    assert job['timeout-minutes'] == 40
    gate = next(step for step in job['steps'] if step.get('name') == 'Daemon and CLI checks')
    assert gate['env']['V2_UNIT_TIMEOUT_S'] == '1200'
    assert 'V2_SMOKE_TIMEOUT_S' not in gate['env'] and 'V2_SOAK_TIMEOUT_S' not in gate['env']
    assert '--basetemp "${RUNNER_TEMP}/pentacle-scratch-' in gate['run']
    assert 'V2_GATE_EVIDENCE_DIR' in gate['env']
    upload = next(step for step in job['steps'] if step.get('uses') == 'actions/upload-artifact@v4')
    assert upload['if'] == 'failure()'
    assert upload['with']['retention-days'] == 7
    paths = upload['with']['path'].splitlines()
    assert len(paths) == 4
    assert all('/pentacle-gate-' in path for path in paths)
    assert all('scratch' not in path and '.db' not in path for path in paths)
    assert paths[-1].endswith('/u4-upload/**')
    assert not any(path.endswith('/**') for path in paths[:-1])
