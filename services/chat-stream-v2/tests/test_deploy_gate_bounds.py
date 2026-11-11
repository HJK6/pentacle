"""The inline deploy gate runs with the v2-merge-capacity tier bounds.

`deploy.py` launches `run_gate.py merge` under `env -i`, so a bound it does not
set explicitly falls back to run_gate's 900 s default.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools import run_gate

from .test_deploy_script import deploy_mod


V2_SERVICE = deploy_mod.SERVICES["chat-streamd-v2"]
SHA = "d" * 40
REPO_ROOT = Path(__file__).resolve().parents[3]
BOUND_NAMES = ("V2_UNIT_TIMEOUT_S", "V2_SMOKE_TIMEOUT_S")

STUB_RUN_GATE = textwrap.dedent(
    """
    import json, os, sys
    out = sys.argv[sys.argv.index("--evidence-out") + 1]
    sha = "%s"
    row = lambda tier: {"tier": tier, "returncode": 0, "timed_out": False, "passed": True,
                        "counts": {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}}
    json.dump({"schema": "pentacle.v2.gate-evidence.v1", "gate": "merge", "sha": sha,
               "source": {"sha_before": sha, "sha_after": sha, "clean_before": True, "clean_after": True},
               "tiers": [row("unit"), row("smoke")], "passed": True,
               "observed_env": {name: os.environ.get(name) for name in %r},
               "observed_argv": sys.argv[1:], "observed_addopts": os.environ.get("PYTEST_ADDOPTS")},
              open(out, "w"))
    """
    % (SHA, BOUND_NAMES)
)


@pytest.fixture(autouse=True)
def _no_host_executables(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stub gate needs neither git nor tmux; keep these tests host-independent.
    monkeypatch.setattr(deploy_mod, "GATE_REQUIRED_EXECUTABLES", ())


def _fake_release(tmp_path: Path) -> Path:
    repo = tmp_path / "release"
    python = deploy_mod._venv_python(repo, V2_SERVICE)
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    tools = repo / deploy_mod.V2_SERVICE_PATH / "tools"
    tools.mkdir(parents=True)
    (tools / "run_gate.py").write_text(STUB_RUN_GATE, encoding="utf-8")
    return repo


def _captured_bounds(tmp_path: Path) -> dict[str, str]:
    repo = _fake_release(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(command, cwd):
        calls.append(tuple(command))
        return deploy_mod._run(command, cwd)

    deploy_mod.run_gate_and_write_evidence(repo, V2_SERVICE, ("merge",), tmp_path / "gate.json", SHA, runner)
    (command,) = calls
    assert command[:2] == ("/usr/bin/env", "-i")
    assignments = dict(part.split("=", 1) for part in command[2:] if "=" in part and not part.startswith("/"))
    return {name: assignments.get(name) for name in BOUND_NAMES}


def _subject_profile(repo: Path, *, workers=None, unit=1800) -> Path:
    target = {"kind": "run_gate", "gate": "merge", "tier_timeouts": {"unit": unit, "smoke": 900}, "timeout_s": unit + 900}
    if workers is not None:
        target["unit_workers"] = workers
    path = repo / "scripts" / "gate-env.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"schema": "pentacle.gate-env.v1", "profiles": {"pentacle": {"targets": {"v2-merge-capacity": target}}}}))
    return path


def test_inline_gate_uses_capacity_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in BOUND_NAMES:
        monkeypatch.delenv(name, raising=False)

    assert _captured_bounds(tmp_path) == {"V2_UNIT_TIMEOUT_S": "1800", "V2_SMOKE_TIMEOUT_S": "900"}


def test_inline_gate_forwards_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("V2_UNIT_TIMEOUT_S", "1500")
    monkeypatch.delenv("V2_SMOKE_TIMEOUT_S", raising=False)

    assert _captured_bounds(tmp_path) == {"V2_UNIT_TIMEOUT_S": "1500", "V2_SMOKE_TIMEOUT_S": "900"}
    repo = tmp_path / "release"
    _subject_profile(repo, workers=2, unit=600)
    evidence = deploy_mod.run_gate_and_write_evidence(repo, V2_SERVICE, ("merge",), tmp_path / "override.json", SHA, deploy_mod._run)
    assert evidence["observed_env"] == {"V2_UNIT_TIMEOUT_S": "1500", "V2_SMOKE_TIMEOUT_S": "900"}
    assert evidence["observed_argv"][:3] == ["merge", "--unit-workers", "2"]


@pytest.mark.parametrize("value", ["abc", "", "0", "-5", "inf", "nan"])
def test_invalid_override_refuses_before_the_gate_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("V2_SMOKE_TIMEOUT_S", value)
    calls: list[tuple[str, ...]] = []

    with pytest.raises(deploy_mod.DeployError, match="gate_timeout_invalid: V2_SMOKE_TIMEOUT_S"):
        deploy_mod.run_gate_and_write_evidence(
            _fake_release(tmp_path), V2_SERVICE, ("merge",), tmp_path / "gate.json", SHA,
            lambda command, _cwd: calls.append(tuple(command)),
        )
    assert calls == []
    monkeypatch.delenv("V2_SMOKE_TIMEOUT_S")
    repo = tmp_path / "release"
    path = _subject_profile(repo, workers=2)
    valid = json.loads(path.read_text())
    target = valid["profiles"]["pentacle"]["targets"]["v2-merge-capacity"]
    for invalid in ("{", "null", json.dumps({**valid, "schema": "future"}),
                    *(json.dumps({**valid, "profiles": {"pentacle": {"targets": {"v2-merge-capacity": {**target, "unit_workers": v}}}}})
                      for v in (True, 0, -1, "2", 1.5, None)),
                    json.dumps({**valid, "profiles": {"pentacle": {"targets": {"v2-merge-capacity": {**target, "timeout_s": 1}}}}})):
        path.write_text(invalid)
        with pytest.raises(deploy_mod.DeployError, match="gate_profile_invalid"):
            deploy_mod.run_gate_and_write_evidence(repo, V2_SERVICE, ("merge",), tmp_path / "bad.json", SHA,
                                                  lambda command, _cwd: calls.append(tuple(command)))
        assert calls == []


def test_inline_gate_bound_reaches_run_gate_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in BOUND_NAMES:
        monkeypatch.delenv(name, raising=False)
    repo = _fake_release(tmp_path)

    evidence = deploy_mod.run_gate_and_write_evidence(
        repo, V2_SERVICE, ("merge",), tmp_path / "gate.json", SHA, deploy_mod._run
    )

    assert evidence["observed_env"] == {"V2_UNIT_TIMEOUT_S": "1800", "V2_SMOKE_TIMEOUT_S": "900"}
    assert "--unit-workers" not in evidence["observed_argv"]
    # The same loaded deploy module observes each checked-out subject, including
    # a forward parallel profile followed by a legacy serial rollback profile.
    for workers, unit in ((2, 600), (None, 1800), (1, 700)):
        _subject_profile(repo, workers=workers, unit=unit)
        evidence = deploy_mod.run_gate_and_write_evidence(repo, V2_SERVICE, ("merge",), tmp_path / "gate.json", SHA, deploy_mod._run)
        assert evidence["observed_env"] == {"V2_UNIT_TIMEOUT_S": str(unit), "V2_SMOKE_TIMEOUT_S": "900"}
        assert evidence["observed_addopts"] is None
        assert ("--unit-workers" in evidence["observed_argv"]) == (workers is not None)
        if workers is not None:
            assert evidence["observed_argv"][:3] == ["merge", "--unit-workers", str(workers)]


def test_defaults_match_capacity_profile() -> None:
    profile = json.loads((REPO_ROOT / "scripts" / "gate-env.json").read_text(encoding="utf-8"))
    capacity = profile["profiles"]["pentacle"]["targets"]["v2-merge-capacity"]["tier_timeouts"]

    assert deploy_mod._subject_gate_settings(REPO_ROOT)[0] == {
        f"V2_{tier.upper()}_TIMEOUT_S": str(bound) for tier, bound in capacity.items()
    }
    assert deploy_mod.DEPLOY_GATE_TIER_TIMEOUTS == {"V2_UNIT_TIMEOUT_S": "1800", "V2_SMOKE_TIMEOUT_S": "900"}
    target = profile["profiles"]["pentacle"]["targets"]["v2-merge-capacity"]
    assert deploy_mod._subject_gate_settings(REPO_ROOT)[1] == ["--unit-workers", str(target["unit_workers"])]


def test_hung_tier_still_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_gate, "_TERMINATION_REQUESTED", False)

    code, timed_out = run_gate._run_process(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={"PATH": "/usr/bin:/bin"}, log_path=tmp_path / "hung.log", timeout=0.5,
    )

    assert (code, timed_out) == (124, True)
    assert run_gate._LAST_PROCESS["termination"] == {"kind": "internal_timeout", "signal": None, "bound_s": 0.5}


def test_failing_gate_still_refuses_deploy(tmp_path: Path) -> None:
    def runner(command, _cwd):
        return subprocess.CompletedProcess(command, 1, "", "unit tier failed")

    repo = _fake_release(tmp_path)
    with pytest.raises(deploy_mod.DeployError, match="v2 merge gate failed rc=1"):
        deploy_mod.run_gate_and_write_evidence(repo, V2_SERVICE, ("merge",), tmp_path / "gate.json", SHA, runner)
