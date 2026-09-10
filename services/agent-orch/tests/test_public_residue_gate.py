import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


def test_fixture_allowlist_cannot_hide_new_product_residue(tmp_path):
    script = Path(__file__).resolve().parents[3] / "scripts/check_public_residue.py"
    spec = importlib.util.spec_from_file_location("public_residue_gate", script)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/fixture.txt").write_text("host" + "a")
    product = tmp_path / "product.py"
    product.write_text("local")
    manifest = tmp_path / "allowlist.json"
    manifest.write_text(json.dumps({"version": 1, "fixtures": {"tests/fixture.txt": "Synthetic routing identity"}}))
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    assert gate.check(tmp_path, manifest)["passed"]
    product.write_text("host" + "b")
    assert product.read_text() == "host" + "b"  # applied mutation, then measured red
    assert gate.check(tmp_path, manifest)["violations"] == [{"path": "product.py", "lines": [1]}]
    manifest.write_text(json.dumps({"version": 1, "fixtures": {"product.py": "Invalid exemption"}}))
    with pytest.raises(ValueError, match="shipped source"):
        gate.check(tmp_path, manifest)
