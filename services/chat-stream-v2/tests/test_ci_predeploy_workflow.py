"""Static contract for the hosted hermetic v2 predeploy tier."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PREDEPLOY = ROOT / ".github" / "workflows" / "predeploy-tests.yml"


def test_predeploy_runs_only_the_v2_gate_and_agent_orch_suite() -> None:
    workflow = PREDEPLOY.read_text(encoding="utf-8")

    # The merge gate runs through the supported gate_env entrypoint, which invokes run_gate.py merge.
    assert "scripts/gate_env.py run --profile pentacle" in workflow
    assert "--target v2-merge" in workflow
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
    # gate_env owns the closed child environment: no ambient timeout/soak variables, and a 1200 s unit tier budget.
    assert set(gate['env']) == {'GATE_EVIDENCE'}
    assert '--timeout-seconds 2400' in gate['run']  # two tiers (unit, smoke) at 1200 s each
    assert '--purpose acceptance' in gate['run'] and '--attempt-manifest' in gate['run']
    assert '--basetemp' not in gate['run']  # scratch is owned by gate_env, not an ambient path
    upload = next(step for step in job['steps'] if step.get('uses') == 'actions/upload-artifact@v4')
    assert upload['if'] == 'failure()'
    assert upload['with']['retention-days'] == 7
    paths = upload['with']['path'].splitlines()
    assert len(paths) == 6
    assert all('/pentacle-gate-' in path for path in paths)
    assert all('scratch' not in path and '.db' not in path for path in paths)
    assert [p for p in paths if p.endswith('/**')] == [p for p in paths if p.endswith(('/gate/u4-upload/**', '/attempts/**'))]
    assert any(p.endswith('/gate/v2-merge-evidence.json') for p in paths)
    assert any(p.endswith('/attempts/**') for p in paths) and any(p.endswith('/test-attempts.v1.json') for p in paths)


def test_gate_env_scratch_is_accepted_by_real_isolation_guard(monkeypatch, tmp_path):
    import importlib.util
    import tempfile
    from tests.cosmo_e2e import harness

    spec = importlib.util.spec_from_file_location('gate_env', ROOT / 'scripts' / 'gate_env.py')
    gate_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate_env)
    import yaml
    workflow = yaml.safe_load(PREDEPLOY.read_text())
    assert workflow['jobs']['checks']['runs-on'] == 'ubuntu-latest'
    # The child gets TMPDIR = scratch tmp; pytest's basetemp lives under it, so the harness guard accepts it.
    home, tmp, basetemp = gate_env.scratch_paths(Path('/opt/ci-runner/_temp/gate-env'), Path('/tmp/ge-1001'), '12345678-aaaa-bbbb-cccc-dddddddddddd')
    monkeypatch.delenv('COSMO_E2E_DISPOSABLE_BASE', raising=False)
    monkeypatch.delenv('V2_GATE_EVIDENCE_DIR', raising=False)
    monkeypatch.setattr(harness, '_disposable_base', lambda: tmp.resolve())
    accepted = harness.assert_disposable_root(basetemp / 'unit' / 'owned-case')
    assert accepted == (basetemp / 'unit' / 'owned-case').resolve()
    assert home.resolve() != tmp.resolve() and tmp.resolve() not in home.resolve().parents
