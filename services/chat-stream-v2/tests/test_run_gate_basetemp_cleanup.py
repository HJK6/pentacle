"""run_gate removes its explicit pytest basetemps; pytest never cleans them."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tools import run_gate


@pytest.mark.parametrize("passed", [True, False])
def test_gate_removes_tier_basetemps_and_keeps_evidence(tmp_path: Path, monkeypatch, passed: bool) -> None:
    evidence, basetemp = tmp_path / "evidence", tmp_path / "base"
    seen: list[str] = []

    def fake_tier(tier, evidence_dir, _limit, *, basetemp, manifest):
        (basetemp / tier / "test_x0").mkdir(parents=True)
        (basetemp / tier / "test_x0" / "state.db").write_text("x", encoding="utf-8")
        (evidence_dir / f"{tier}.junit.xml").write_text("<testsuite/>", encoding="utf-8")
        seen.append(tier)
        return run_gate._result(tier, code=0 if passed else 1, passed=passed)

    monkeypatch.setattr(run_gate, "_run_tier", fake_tier)
    monkeypatch.setattr(run_gate, "_sha", lambda: "c" * 40)
    monkeypatch.setattr(run_gate, "_git_status", lambda: "")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_gate.py", "merge", "--evidence-dir", str(evidence),
                                      "--basetemp", str(basetemp)])
    assert run_gate.main() == (0 if passed else 1)
    assert seen == ["unit", "smoke"]
    assert not (basetemp / "unit").exists() and not (basetemp / "smoke").exists()
    assert (evidence / "unit.junit.xml").exists() and (evidence / "v2-merge-evidence.json").exists()


@pytest.mark.parametrize("reap_raises", [True, False])
def test_gate_retains_unresolved_cleanup_and_original_failure(tmp_path, monkeypatch, reap_raises):
    import json
    evidence, basetemp = tmp_path / "evidence", tmp_path / "base"
    def tier(name, evidence_dir, limit, *, basetemp, manifest):
        (basetemp / name).mkdir()
        (basetemp / name / "original.log").write_text("first failure evidence")
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
    assert [r["reason"] for r in data["tiers"][:2]] == ["original_assertion", "original_assertion"]
    assert data["tiers"][-1]["reason"] == "cleanup_failed"
    assert (basetemp / ".owned.json").exists()
    assert (basetemp / "unit" / "original.log").read_text() == "first failure evidence"
    assert (basetemp / "smoke" / "original.log").read_text() == "first failure evidence"
