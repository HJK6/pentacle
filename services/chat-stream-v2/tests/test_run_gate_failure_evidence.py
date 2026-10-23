"""run_gate keeps the original failure, its reporting/cleanup facts and the last node.

Each case runs the real runner and progress plugin against a disposable tiny suite
through a small driver that only redirects the subject command and git readback.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import run_gate


SERVICE_DIR = Path(__file__).resolve().parents[1]
DRIVER = r'''
import json, os, sys
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text())
sys.path.insert(0, config["service_dir"])
os.environ["PYTHONPATH"] = config["service_dir"]
from tools import run_gate

suite = Path(config["suite"])
run_gate.SERVICE_DIR = suite
run_gate._tier_command = lambda tier, junit, basetemp: [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider",
                                                         f"--junitxml={junit}", "--basetemp", str(basetemp / tier)]
run_gate._sha = lambda: "c" * 40
statuses = iter(config.get("statuses", ["", ""]))
run_gate._git_status = lambda: next(statuses)
if config.get("log_max_bytes"):
    run_gate.LOG_MAX_BYTES = config["log_max_bytes"]
if config.get("reap_fails"):
    real_reap, calls = run_gate.reap_manifest, []
    def reap(manifest, run_id=None):
        calls.append(run_id)
        if run_id is not None:
            raise OSError("injected reap failure")
        return real_reap(manifest)
    run_gate.reap_manifest = reap
sys.argv = ["run_gate.py", *config["argv"]]
raise SystemExit(run_gate.main())
'''
PASSING = "def test_ok():\n    pass\n"


def _prepare(tmp_path: Path, source: str, *, argv: list[str], **config) -> tuple[list[str], Path, dict]:
    suite, evidence = tmp_path / "suite", tmp_path / "evidence"
    suite.mkdir()
    (suite / "pytest.ini").write_text("[pytest]\ntimeout = 3\ntimeout_method = thread\n", encoding="utf-8")
    (suite / "test_synthetic.py").write_text(source, encoding="utf-8")
    (tmp_path / "driver.py").write_text(DRIVER, encoding="utf-8")
    full = {"service_dir": str(SERVICE_DIR), "suite": str(suite),
            "argv": [*argv, "--evidence-dir", str(evidence), "--basetemp", str(tmp_path / "base")], **config}
    (tmp_path / "config.json").write_text(json.dumps(full), encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE", "V2_GATE_OWNER_MANIFEST"}}
    return [sys.executable, str(tmp_path / "driver.py"), str(tmp_path / "config.json")], evidence, env


def _gate(tmp_path: Path, source: str, *, argv: list[str] | None = None, **config) -> tuple[int, dict, str]:
    command, evidence, env = _prepare(tmp_path, source, argv=argv or ["unit"], **config)
    proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=50)
    data = json.loads((evidence / f"v2-{(argv or ['unit'])[0]}-evidence.json").read_text(encoding="utf-8"))
    return proc.returncode, data, proc.stdout + proc.stderr


def _subject_gone(tier: dict) -> bool:
    """The subject's own process group (pid == pgid via start_new_session) no longer exists."""
    journal = Path(tier["progress"]["journal"])
    pid = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])["pid"]
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_pass_with_deadline_records_complete_progress(tmp_path: Path) -> None:
    code, data, out = _gate(tmp_path, PASSING, argv=["unit", "--timeout-seconds", "20", "--deadline-seconds", "120"])
    unit = data["tiers"][0]
    assert code == 0 and data["passed"] is True and data["finished"] is True, out
    assert unit["reason"] is None and unit["secondary_outcomes"] == [] and unit["progress"]["complete"] is True
    assert unit["subprocess_returncode"] == 0 and unit["termination"] is None and unit["output"]["reader_joined"] is True
    check = data["deadline"]["checks"][0]
    assert check["ok"] and check["tier_bounds_s"] == {"unit": 20.0} and check["reserve_s"] == run_gate.DEFAULT_RESERVE_S
    assert not (tmp_path / "base" / "unit").exists()


def test_assertion_and_setup_errors_name_nodes(tmp_path: Path) -> None:
    source = "import pytest\n@pytest.fixture\ndef bad():\n    raise RuntimeError('x')\ndef test_a():\n    assert 0\ndef test_b(bad):\n    pass\n"
    code, data, _ = _gate(tmp_path, source)
    unit = data["tiers"][0]
    assert code == 1 and unit["reason"] == "test_failures" and unit["counts"]["failures"] == 1 and unit["counts"]["errors"] == 1
    assert {"node": "test_synthetic.py::test_a", "phase": "call"} in unit["progress"]["failed"]
    assert {"node": "test_synthetic.py::test_b", "phase": "setup"} in unit["progress"]["failed"]
    assert _subject_gone(unit)


def test_per_test_timeout_hard_exit_keeps_active_node(tmp_path: Path) -> None:
    source = "import time\ndef test_first():\n    pass\ndef test_hang():\n    time.sleep(30)\n"
    code, data, _ = _gate(tmp_path, source)
    unit = data["tiers"][0]
    assert code == 1 and unit["passed"] is False and unit["reason"] == "missing_junit"
    assert "journal_incomplete" in unit["secondary_outcomes"]
    assert unit["progress"]["active_nodes"] == ["test_synthetic.py::test_hang"]
    assert unit["progress"]["last_completed"] == "test_synthetic.py::test_first"


def test_outer_timeout_is_primary_with_missing_junit_secondary(tmp_path: Path) -> None:
    source = "import time, pytest\ndef test_first():\n    pass\n@pytest.mark.timeout(60)\ndef test_slow():\n    time.sleep(40)\n"
    code, data, _ = _gate(tmp_path, source, argv=["unit", "--timeout-seconds", "3"])
    unit = data["tiers"][0]
    assert code == 1 and unit["reason"] == "timeout" and unit["returncode"] == 124 and unit["timed_out"] is True
    assert unit["secondary_outcomes"][:2] == ["missing_junit", "journal_incomplete"]
    assert unit["termination"] == {"kind": "internal_timeout", "signal": None, "bound_s": 3.0}
    assert unit["subprocess_returncode"] in (-signal.SIGTERM, -signal.SIGKILL)
    assert unit["progress"]["active_nodes"] == ["test_synthetic.py::test_slow"]
    assert _subject_gone(unit)


def test_external_sigterm_is_attributed_separately(tmp_path: Path) -> None:
    source = "import time, pytest\n@pytest.mark.timeout(60)\ndef test_slow():\n    time.sleep(40)\n"
    command, evidence, env = _prepare(tmp_path, source, argv=["unit", "--timeout-seconds", "40"])
    proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    journal = evidence / "unit.progress.jsonl"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and '"kind":"start"' not in (journal.read_text(encoding="utf-8") if journal.exists() else ""):
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    unit = json.loads((evidence / "v2-unit-evidence.json").read_text(encoding="utf-8"))["tiers"][0]
    assert proc.returncode == 1 and unit["reason"] == "terminated" and unit["timed_out"] is False
    assert unit["termination"] == {"kind": "external_signal", "signal": "SIGTERM"}
    assert "missing_junit" in unit["secondary_outcomes"] and unit["progress"]["active_nodes"] == ["test_synthetic.py::test_slow"]
    assert _subject_gone(unit)


def test_runner_sigkill_leaves_visibly_unfinished_evidence(tmp_path: Path) -> None:
    source = "import time, pytest\n@pytest.mark.timeout(60)\ndef test_slow():\n    time.sleep(40)\n"
    command, evidence, env = _prepare(tmp_path, source, argv=["unit", "--timeout-seconds", "40"])
    proc = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    journal = evidence / "unit.progress.jsonl"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and '"kind":"start"' not in (journal.read_text(encoding="utf-8") if journal.exists() else ""):
        time.sleep(0.05)
    proc.kill()
    proc.wait(timeout=10)
    subject = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])["pid"]
    try:
        os.killpg(subject, signal.SIGKILL)  # orphaned subject: this test owns it
    except ProcessLookupError:
        pass
    data = json.loads((evidence / "v2-unit-evidence.json").read_text(encoding="utf-8"))
    assert data["finished"] is False and data["passed"] is False and data["tiers"] == []
    from tools import gate_progress
    assert gate_progress.summarize(journal)["active_nodes"] == ["test_synthetic.py::test_slow"]


def test_log_write_failure_never_turns_green(tmp_path: Path) -> None:
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "unit.log").mkdir()  # the log path cannot be opened as a file
    code, data, _ = _gate(tmp_path, PASSING)
    unit = data["tiers"][0]
    assert code == 1 and unit["reason"] == "evidence_incomplete" and "log_write_failed" in unit["secondary_outcomes"]
    assert unit["counts"]["tests"] == 1 and unit["output"]["reader_joined"] is True


def test_output_budget_exhaustion_is_explicit_and_bounded(tmp_path: Path) -> None:
    source = "def test_noisy():\n    for i in range(2000):\n        print('line', i)\n"
    code, data, _ = _gate(tmp_path, source, log_max_bytes=4000)
    unit = data["tiers"][0]
    assert code == 1 and unit["reason"] == "evidence_incomplete" and "output_truncated" in unit["secondary_outcomes"]
    assert Path(unit["log"]).stat().st_size <= 4000 and unit["output"]["dropped_bytes"] > 0


def test_cleanup_failure_after_assertion_keeps_both(tmp_path: Path) -> None:
    code, data, _ = _gate(tmp_path, "def test_a():\n    assert 0\n", reap_fails=True)
    reasons = [(tier["tier"], tier["reason"]) for tier in data["tiers"]]
    assert code == 1 and reasons[0] == ("unit", "test_failures") and ("cleanup", "cleanup_failed") in reasons
    assert data["finished"] is True and data["passed"] is False


def test_missing_junit_after_clean_exit_fails(tmp_path: Path) -> None:
    code, data, _ = _gate(tmp_path, "import os\ndef test_a():\n    os._exit(0)\n")
    unit = data["tiers"][0]
    assert code == 1 and unit["subprocess_returncode"] == 0 and unit["reason"] == "missing_junit"
    assert "journal_incomplete" in unit["secondary_outcomes"]


def test_source_change_is_recorded_beside_green_tier(tmp_path: Path) -> None:
    code, data, _ = _gate(tmp_path, PASSING, statuses=["", " M changed.py"])
    assert code == 1 and data["tiers"][0]["passed"] is True
    assert data["tiers"][-1]["reason"] == "source_changed_during_gate"


def test_deadline_refuses_before_launch(tmp_path: Path) -> None:
    code, data, _ = _gate(tmp_path, PASSING, argv=["merge", "--timeout-seconds", "20", "--deadline-seconds", "60"])
    refusal = data["tiers"][0]
    assert code == 1 and len(data["tiers"]) == 1 and refusal["reason"] == "deadline_envelope_exceeded"
    check = refusal["deadline_check"]
    assert check["tier_bounds_s"] == {"unit": 20.0, "smoke": 20.0} and check["preflight_bounds_s"] == {"smoke": 30.0}
    assert check["required_s"] == 20 + 20 + 30 + run_gate.DEFAULT_RESERVE_S
    assert not (tmp_path / "evidence" / "unit.progress.jsonl").exists()


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-5", "abc"])
def test_invalid_deadline_rejected_before_launch(tmp_path: Path, value: str) -> None:
    command, evidence, env = _prepare(tmp_path, PASSING, argv=["unit", "--deadline-seconds", value])
    proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2 and "--deadline-seconds" in proc.stderr
    assert not evidence.exists()


def test_deadline_refuses_between_tiers(tmp_path: Path, monkeypatch) -> None:
    clock = [1000.0]
    seen: list[str] = []

    def fake_tier(tier, evidence_dir, limit, *, basetemp, manifest):
        seen.append(tier)
        clock[0] += 50.0  # unit overran its 10s bound (terminate/drain), leaving too little for smoke
        return run_gate._result(tier, code=0, passed=True)

    monkeypatch.setattr(run_gate, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(run_gate, "_run_tier", fake_tier)
    monkeypatch.setattr(run_gate, "_sha", lambda: "c" * 40)
    monkeypatch.setattr(run_gate, "_git_status", lambda: "")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_gate.py", "merge", "--evidence-dir", str(tmp_path / "evidence"), "--basetemp",
                                      str(tmp_path / "base"), "--timeout-seconds", "10", "--deadline-seconds", "60",
                                      "--deadline-reserve-seconds", "5"])
    assert run_gate.main() == 1
    data = json.loads((tmp_path / "evidence" / "v2-merge-evidence.json").read_text(encoding="utf-8"))
    assert seen == ["unit"] and [tier["reason"] for tier in data["tiers"]] == [None, "deadline_envelope_exceeded"]
    assert [check["ok"] for check in data["deadline"]["checks"]] == [True, False]
    assert data["deadline"]["checks"][1]["tier_bounds_s"] == {"smoke": 10.0}
