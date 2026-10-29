"""Dedicated ALL-rows B4/B5 acceptance; separately budgeted at 4500 seconds.

The standard merge gate selects its fixed full-axis representative in
 test_account_repin_state_machine.py. This module never samples its ALL run.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path

import pytest

pytestmark=pytest.mark.repin_exhaustive


def test_worker_transport_controls(tmp_path):
    from repin_state_matrix import cases
    from repin_matrix_runner import execute
    # A small diagnostic for process setup/serialization/cleanup, not acceptance.
    selected=[]
    for row in ("B-N1-verifier","B-Z-crash","B-C0","B-U1-live","B-hatch-races","B-admission"):
        all_cases=list(cases(row));selected.extend((all_cases[0],all_cases[-1]))
    receipts,cleanup=execute(selected,tmp_path,workers=2)
    path=Path(os.environ["PENTACLE_GATE_EVIDENCE_DIR"])/"matrix-worker-controls.json"
    path.write_text(json.dumps({"scope":"worker transport controls only","receipts":receipts,"cleanup":cleanup},sort_keys=True))
    assert all(receipt["outcome"]=="passed" for receipt in receipts),path


@pytest.mark.timeout(4400)
def test_all_required_b4_b5_rows(tmp_path):
    from repin_state_matrix import ROWS,cases
    from repin_matrix_extra import ROWS as extra_rows,EXCLUSIONS
    from repin_matrix_runner import execute
    rows=(*ROWS,*extra_rows)
    assert len(rows)==len(set(rows))
    selected=[case for row in rows for case in cases(row)]
    receipts,cleanup=execute(selected,tmp_path,workers=min(8,os.cpu_count() or 1))
    failures=[receipt["case_id"] for receipt in receipts if receipt["outcome"]!="passed"]
    manifest={"scope":"ALL deterministic Addendum B B4/B5 rows; no sampling","rows":list(rows),
        "case_count":len(selected),"counts_by_row":dict(Counter(case.row for case in selected)),
        "structural_exclusions":EXCLUSIONS,"receipts":receipts,"failed_case_ids":failures,"cleanup":cleanup}
    canonical=json.dumps(manifest,sort_keys=True,separators=(",",":"))
    evidence=Path(os.environ["PENTACLE_GATE_EVIDENCE_DIR"])
    (evidence/"exhaustive-matrix.json").write_text(canonical)
    (evidence/"exhaustive-matrix.sha256").write_text(hashlib.sha256(canonical.encode()).hexdigest()+"\n")
    print(f"ALL B4/B5 cases={len(selected)} rows={len(rows)} failed={len(failures)} actual_seconds={cleanup['seconds']:.3f}")
    assert cleanup["seconds"]<=4500
    assert not failures,failures[:10]


def test_catalog_inventory():
    from repin_state_matrix import ROWS,cases
    from repin_matrix_extra import ROWS as extras,EXCLUSIONS
    rows=(*ROWS,*extras)
    selected=[case for row in rows for case in cases(row)]
    assert len(rows)==len(set(rows)) and len(selected)==len({case.id for case in selected})
    required={"B-N0","B-N1-collision","B-U1-live","B-U1-dead","B-Z-late","B-N3","B-S0","B-S1-absent",
        "B-S1-failed","B-S1-success","B-C0","B-race-pin","B-race-origin","B-origin-reanchor","B-generation-exclusion",
        "B-S1-settled-tail","B-combined-liveness","B-fence-union","B-invalid-proof","B-admission","B-status-locks",
        "B-hatch-all-states","B-hatch-G1","B-hatch-prior-findings","B-hatch-races","B-hatch-refusals","B-guard-required"}
    assert required<=set(rows)
    for row in rows:
        present=[case for case in selected if case.row==row]
        assert {case.mode for case in present}=={"root","reconcile"}
        assert {case.moves for case in present}=={0,1,3}
        assert {case.connection for case in present}=={"online","return","flap"}
        assert {case.provider for case in present}=={"claude","codex"}
        assert {case.platform for case in present}=={"macos","wsl"}
        assert {case.alias for case in present}=={"ours","sai"}
    inventory={"scope":"enumeration only; no acceptance claim","case_count":len(selected),"rows":rows,
        "counts_by_row":dict(Counter(case.row for case in selected)),"structural_exclusions":EXCLUSIONS,
        "case_ids":[case.id for case in selected]}
    canonical=json.dumps(inventory,sort_keys=True,separators=(",",":"))
    root=Path(os.environ["PENTACLE_GATE_EVIDENCE_DIR"])
    (root/"matrix-catalog.json").write_text(canonical)
    print(f"B4/B5 inventory rows={len(rows)} cases={len(selected)} sha256={hashlib.sha256(canonical.encode()).hexdigest()}")
