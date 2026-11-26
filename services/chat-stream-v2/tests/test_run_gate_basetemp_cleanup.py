"""run_gate removes its explicit pytest basetemps; pytest never cleans them."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import run_gate


@pytest.fixture(autouse=True)
def capable_host(monkeypatch):
    monkeypatch.setattr(run_gate, "_preflight", lambda *args, **kwargs: (0, False))


@pytest.fixture
def merge_checks(monkeypatch):
    # Cleanup tests inject tier outcomes. Supply the successful prerequisite
    # stages explicitly so the receipt still exercises the merge sequence.
    monkeypatch.setattr(run_gate, "_merge_contract_base", lambda timeout: "b" * 40)
    monkeypatch.setitem(sys.modules, "select_tests", SimpleNamespace(check_coverage=lambda *args, **kwargs: None))
    monkeypatch.setattr(run_gate, "_run_daemon_contract", lambda evidence, base, timeout:
                        run_gate._result("daemon_contract", code=0, passed=True))


@pytest.mark.parametrize("failure", [pytest.param(None, id="True"),
                                    pytest.param("later-tiers", id="False"), "contract"])
def test_gate_removes_tier_basetemps_and_keeps_evidence(tmp_path: Path, monkeypatch, merge_checks, failure) -> None:
    evidence, basetemp = tmp_path / "evidence", tmp_path / "base"
    seen: list[str] = []

    def fake_tier(tier, evidence_dir, _limit, *, basetemp, manifest, unit_workers=1, advisory_bound=None):
        (basetemp / tier / "test_x0").mkdir(parents=True)
        (basetemp / tier / "test_x0" / "state.db").write_text("x", encoding="utf-8")
        (evidence_dir / f"{tier}.junit.xml").write_text("<testsuite/>", encoding="utf-8")
        seen.append(tier)
        passed = failure is None or (failure == "later-tiers" and tier == "contract")
        return run_gate._result(tier, code=0 if passed else 1, passed=passed)

    monkeypatch.setattr(run_gate, "_run_tier", fake_tier)
    monkeypatch.setattr(run_gate, "_sha", lambda: "c" * 40)
    monkeypatch.setattr(run_gate, "_git_status", lambda: "")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_gate.py", "merge", "--evidence-dir", str(evidence),
                                      "--basetemp", str(basetemp)])
    assert run_gate.main() == (0 if failure is None else 1)
    assert seen == (["contract"] if failure == "contract" else ["contract", "unit", "smoke"])
    assert all(not (basetemp / tier).exists() for tier in ("contract", "unit", "smoke"))
    assert all((evidence / f"{tier}.junit.xml").exists() == (tier in seen)
               for tier in ("contract", "unit", "smoke"))
    assert (evidence / "v2-merge-evidence.json").exists()


@pytest.mark.parametrize("reap_raises", [True, False])
def test_gate_retains_unresolved_cleanup_and_original_failure(tmp_path, monkeypatch, merge_checks, reap_raises):
    evidence, basetemp = tmp_path / "evidence", tmp_path / "base"
    def tier(name, evidence_dir, limit, *, basetemp, manifest, unit_workers=1, advisory_bound=None):
        (basetemp / name).mkdir()
        (basetemp / name / "original.log").write_text("first failure evidence")
        if name == "contract":
            return run_gate._result(name, code=0, passed=True)
        return run_gate._result(name, code=1, passed=False, reason="original_assertion")
    real_reap = run_gate.reap_manifest
    def reap(path, run_id=None):
        if run_id is None:
            return real_reap(path)
        if reap_raises:
            raise run_gate.ManifestError("synthetic unresolved socket")
        return False  # unresolved manifest also prevents destructive cleanup
    monkeypatch.setattr(run_gate, "_run_tier", tier)
    monkeypatch.setattr(run_gate, "reap_manifest", reap)
    monkeypatch.setattr(run_gate, "_sha", lambda: "c" * 40)
    monkeypatch.setattr(run_gate, "_git_status", lambda: "")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_gate.py", "merge", "--evidence-dir", str(evidence), "--basetemp", str(basetemp)])
    assert run_gate.main() != 0
    data = json.loads((evidence / "v2-merge-evidence.json").read_text())
    assert [(r["tier"], r["reason"]) for r in data["tiers"]] == [
        ("daemon_contract", None),
        ("contract", None), ("unit", "original_assertion"), ("smoke", "original_assertion"),
        ("cleanup", "cleanup_failed"),
    ]
    assert data["tiers"][-1]["reason"] == "cleanup_failed"
    assert (basetemp / ".owned.json").exists()
    assert (basetemp / "contract" / "original.log").read_text() == "first failure evidence"
    assert (basetemp / "unit" / "original.log").read_text() == "first failure evidence"
    assert (basetemp / "smoke" / "original.log").read_text() == "first failure evidence"


def test_direct_gate_refuses_untrusted_basetemp_before_creating_it(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence"
    base = tmp_path / "outside" / "base"
    trusted = tmp_path / "os-temp"
    trusted.mkdir()
    monkeypatch.setattr(run_gate.tempfile, "tempdir", str(trusted))
    monkeypatch.delenv("COSMO_E2E_DISPOSABLE_BASE", raising=False)
    monkeypatch.delenv("V2_GATE_EVIDENCE_DIR", raising=False)
    monkeypatch.setattr(run_gate, "_sha", lambda: "c" * 40)
    monkeypatch.setattr(run_gate, "_git_status", lambda: "")
    monkeypatch.setattr(run_gate, "_run_tier", lambda *_a, **_kw: pytest.fail("unsafe tier launched"))
    monkeypatch.setattr(sys, "argv", ["run_gate.py", "unit", "--evidence-dir", str(evidence),
                                      "--basetemp", str(base)])
    assert run_gate.main() != 0
    receipt = json.loads((evidence / "v2-unit-evidence.json").read_text())
    assert receipt["tiers"][0]["reason"] == "invalid_basetemp"
    assert not base.exists()


def test_default_scratch_uses_os_temp_independently_of_evidence(tmp_path, monkeypatch):
    trusted = tmp_path / "system-temp"
    trusted.mkdir()
    evidence = tmp_path / "durable-evidence"
    seen = []
    def tier(name, evidence_dir, limit, *, basetemp, **kwargs):
        assert name == "unit" and evidence_dir == evidence and basetemp.parent == trusted
        seen.append(basetemp)
        return run_gate._result(name, code=0, passed=True)

    monkeypatch.setattr(run_gate.tempfile, "tempdir", str(trusted))
    monkeypatch.setattr(run_gate, "_run_tier", tier)
    monkeypatch.setattr(run_gate, "_sha", lambda: "c" * 40)
    monkeypatch.setattr(run_gate, "_git_status", lambda: "")
    monkeypatch.setattr(sys, "argv", ["run_gate.py", "unit", "--evidence-dir", str(evidence)])
    assert run_gate.main() == 0
    assert len(seen) == 1 and not seen[0].exists()
    assert (evidence / "v2-unit-evidence.json").is_file()
