"""Run the supported-entry self-test under the v2 unit gate and pin its scratch layout to the isolation guard."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "test_gate_env.py"


def test_gate_env_selftest_passes() -> None:
    done = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


def test_gate_env_scratch_is_accepted_by_real_isolation_guard(monkeypatch) -> None:
    from tests.cosmo_e2e import harness

    spec = importlib.util.spec_from_file_location("gate_env_under_test", ROOT / "scripts" / "gate_env.py")
    gate_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate_env)
    # The child gets TMPDIR = scratch tmp; pytest's basetemp lives under it, so the harness guard accepts it.
    home, tmp, basetemp = gate_env.scratch_paths(Path("/opt/ci-runner/_temp/gate-env"), Path("/tmp/ge-1001"), "12345678-aaaa-bbbb-cccc-dddddddddddd")
    monkeypatch.delenv("COSMO_E2E_DISPOSABLE_BASE", raising=False)
    monkeypatch.delenv("V2_GATE_EVIDENCE_DIR", raising=False)
    monkeypatch.setattr(harness, "_disposable_base", lambda: tmp.resolve())
    root = basetemp / "unit" / "owned-case"
    assert harness.assert_disposable_root(root) == root.resolve()
    assert home.resolve() != tmp.resolve() and tmp.resolve() not in home.resolve().parents
