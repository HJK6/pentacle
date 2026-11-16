"""Shared command wrapper and historical synthetic promotion-fixture integrity."""
import json
import pytest
from test_merge_gate import merge_gate, _result

pytestmark = pytest.mark.timeout(10)

def test_command_passes_remaining_timeout_to_subprocess(monkeypatch):
    seen={}
    def run(args,**kwargs):
        seen.update(kwargs);return _result(args)
    monkeypatch.setattr(merge_gate.subprocess,"run",run)
    merge_gate._command(["synthetic-command"],timeout=0.25)
    assert seen["timeout"]==0.25

def test_h5_synthetic_fixture_manifest_is_complete_and_hash_pinned():
    import hashlib
    from pathlib import Path
    root=Path(__file__).parents[1]
    manifest=json.loads((root/"tests/fixtures/th_h5/MANIFEST.json").read_text())
    assert {row["path"] for row in manifest["files"]} == {
        "tests/fixtures/th_h5/tag_checkout.txt","tests/test_merge_gate_polling.py","tests/test_deploy_fleet_verdict.py"}
    for row in manifest["files"]:
        assert row["synthetic"] is True and row["contains_real_traffic"] is False
        assert hashlib.sha256((root/row["path"]).read_bytes()).hexdigest()==row["sha256"]
