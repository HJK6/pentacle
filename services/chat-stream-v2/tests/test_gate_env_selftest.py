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


def test_gate_env_deadline_is_accepted_by_the_real_runner_envelope(tmp_path) -> None:
    """The first real smoke run was refused because gate_env's deadline equalled the runner's bare required sum."""
    import json
    import time

    from tools import run_gate

    spec = importlib.util.spec_from_file_location("gate_env_under_test2", ROOT / "scripts" / "gate_env.py")
    gate_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate_env)
    config = json.loads((ROOT / "scripts" / "gate-env.json").read_text())
    service = ROOT / "services" / "chat-stream-v2"
    for name in ("unit", "smoke", "merge"):
        target = config["profiles"]["pentacle"]["targets"]["v2-" + name]
        command, results = gate_env.build_command(config, target, tmp_path / "venv", ROOT, service, tmp_path / "ev", tmp_path / "t" / "b", [], target["timeout_s"], {})
        deadline_s = float(command[command.index("--deadline-seconds") + 1])
        tier_s = float(command[command.index("--timeout-seconds") + 1])
        tiers = [("unit", tier_s), ("smoke", tier_s)] if name == "merge" else [(name, tier_s)]
        for startup_s in (0.0, 5.0, 25.0):  # runner startup before its first check
            deadline = {"deadline_s": deadline_s, "reserve_s": float(config["deadline"]["reserve_s"]), "checks": []}
            assert run_gate._deadline_check(deadline, time.monotonic() - startup_s, "deadline", tiers, usage_pending=False) is None
        assert results["wall_s"] > deadline_s
