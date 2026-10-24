"""Run the supported-entry self-test under the v2 unit gate and pin its scratch layout to the isolation guard."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "test_gate_env.py"


def test_supported_child_owns_ssh_and_imports_pytest_in_isolation():
    """Exercise the actual SSH helper and -I interpreter, not a mocked chmod."""
    import json

    spec = importlib.util.spec_from_file_location("gate_env_isolation", ROOT / "scripts" / "gate_env.py")
    gate_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate_env)
    config = gate_env.load_config()
    profile = config["profiles"]["pentacle"]
    with tempfile.TemporaryDirectory(prefix="ge-ssh-", dir="/tmp") as folder:
        home = Path(folder) / "h"
        home.mkdir()
        env = gate_env.build_env(
            config, profile, profile["targets"]["v2-pytest"], checkout=ROOT,
            toolchain={}, venv=Path(sys.prefix), home=home, tmp=Path(folder) / "t",
            locale_name="C.UTF-8", evidence=Path(folder) / "e", attempt_id="isolation",
            pass_env={}, ambient={"HOME": "/forbidden-home", "PENTACLE_SSH_CONTROL_DIR": "/forbidden-ssh"})
        assert "PENTACLE_SSH_CONTROL_DIR" not in env
        code = (
            "import json,sys,site; from pathlib import Path; import pytest; "
            "sys.path.insert(0,sys.argv[1]); import machines; "
            "p=machines.ssh_control_path('fixture@peer'); "
            "print(json.dumps({'socket':str(p),'pytest':pytest.__file__,"
            "'user_site':site.ENABLE_USER_SITE,'sys_path':sys.path}))"
        )
        done = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "services/chat-stream-v2")],
                              env=env, text=True, capture_output=True, timeout=10)
        assert done.returncode == 0, done.stderr
        proof = json.loads(done.stdout)
        assert Path(proof["socket"]).parent == home / ".ssh" / "cm"
        assert (home / ".ssh/cm").stat().st_mode & 0o777 == 0o700
        assert proof["user_site"] is False
        assert Path(proof["pytest"]).is_relative_to(Path(sys.prefix))
        assert all("/forbidden-" not in path for path in proof["sys_path"])


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
