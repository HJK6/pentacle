"""Release expectations come from an external contract, not candidate labels."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import jsonschema
import pytest

from tools import usage_manifest as gate

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def fixture(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], env=env, stderr=subprocess.DEVNULL).decode().strip()
    git("init", "-q")
    paths = {
        "test_sha256": gate.SELECTOR[2],
        "fixture_sha256": "services/chat-stream-v2/tests/fixtures/usage_telemetry_cases.json",
        "manifest_schema_sha256": "services/chat-stream-v2/tests/fixtures/usage_stage1_manifest_schema.json",
    }
    overlay = {}
    for key, name in paths.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
        overlay[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    sha = git("rev-parse", "HEAD")
    receipt = tmp_path / "receipt.log"
    receipt.write_text("owned fixture evidence\n")
    ref = {"path": str(receipt), "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}
    contract = {"schema": "pentacle.usage-deployment", "schema_version": 1,
                "coordinator_host": "local", "satellite_hosts": ["edge"],
                "pin_owner": "desk:lead", "prechange_hosts": ["local", "edge"]}
    payload = {
        "schema": gate.SCHEMA, "schema_version": 2, "candidate_sha": sha, "base_sha": "a" * 40,
        "selector": gate.SELECTOR, "overlay": overlay,
        "runtime": {"coordinator": {"host": "local", "pid": 10, "checkout": str(repo), "sha": sha},
                    "satellites": {"edge": {"checkout_sha": "b" * 40, "pid": 20,
                                             "event_push_runtime_sha": "b" * 40,
                                             "observed_at": "2026-01-01T00:00:00Z"}}},
        "pin": {"owner": "desk:lead", "mutation_status": "deferred", "target_sha": None, "previous_sha": None},
        "streams": [{"stream_id": "edge:session", "authenticated_source_host": "edge", "provider": "codex",
                     "session_generation": "generation-1", "source_file_identity_digest": "c" * 64,
                     "snapshot_digest": "d" * 64, "revision": 1, "request_ids": ["request-1"],
                     "usage_recorded": [0], "usage_replayed": [1], "replay_zero_new_records": True}],
        "receipts": {"red": ref, "focused": ref, "junit": ref,
                     "prechange": {"local": ref, "edge": ref}},
    }
    manifest_path, contract_path = tmp_path / "manifest.json", tmp_path / "deployment.json"
    def write():
        manifest_path.write_text(json.dumps(payload))
        contract_path.write_text(json.dumps(contract))
    write()
    return repo, payload, contract, manifest_path, contract_path, write


def test_generic_deployment_full_validation_and_schema_match(fixture):
    repo, payload, _, manifest, contract, _ = fixture
    jsonschema.validate(payload, json.loads((ROOT / "services/chat-stream-v2/tests/fixtures/usage_stage1_manifest_schema.json").read_text()))
    result = gate.validate(manifest, repo=repo, deployment_contract=contract)
    assert result == {"passed": True, "candidate_sha": payload["candidate_sha"], "file_checks": True,
                      "deployment_contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest()}
    command = [sys.executable, str(ROOT / "services/chat-stream-v2/tools/usage_manifest.py"), "validate",
               "--repo", str(repo), "--manifest", str(manifest), "--deployment-contract", str(contract)]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == result


@pytest.mark.parametrize("change", ["owner", "satellites", "prechange", "source", "candidate", "overlay",
                                    "receipt", "pid", "generation", "replay", "version"])
def test_candidate_cannot_weaken_external_expectations_or_evidence(fixture, change):
    repo, payload, _, manifest, contract, write = fixture
    if change == "owner": payload["pin"]["owner"] = "desk:other"
    elif change == "satellites": payload["runtime"]["satellites"] = {}
    elif change == "prechange": payload["receipts"]["prechange"].pop("edge")
    elif change == "source": payload["streams"][0].update(stream_id="outsider:session", authenticated_source_host="outsider")
    elif change == "candidate": payload["candidate_sha"] = "e" * 40
    elif change == "overlay": payload["overlay"]["test_sha256"] = "e" * 64
    elif change == "receipt": payload["receipts"]["red"]["sha256"] = "e" * 64
    elif change == "pid": payload["runtime"]["satellites"]["edge"]["pid"] = 0
    elif change == "generation": payload["streams"][0]["session_generation"] = ""
    elif change == "replay": payload["streams"][0]["usage_recorded"] = [1]
    elif change == "version": payload["schema_version"] = 1
    write()
    with pytest.raises(gate.ManifestError):
        gate.validate(manifest, repo=repo, deployment_contract=contract)


def test_single_host_explicit_inventory_and_structural_only_receipt(fixture):
    repo, payload, expected, manifest, contract, write = fixture
    expected["satellite_hosts"] = []
    expected["prechange_hosts"] = ["local"]
    payload["runtime"]["satellites"] = {}
    payload["receipts"]["prechange"].pop("edge")
    payload["streams"][0].update(stream_id="local:session", authenticated_source_host="local")
    write()
    assert gate.validate(manifest, repo=repo, deployment_contract=contract)["file_checks"] is True
    payload["candidate_sha"] = "e" * 40
    write()
    assert gate.validate(manifest, repo=repo, deployment_contract=contract, verify_files=False)["file_checks"] is False


def test_contract_must_be_external_complete_and_unique(fixture):
    repo, _, expected, manifest, contract, write = fixture
    inside = repo / "deployment.json"
    inside.write_bytes(contract.read_bytes())
    with pytest.raises(gate.ManifestError, match="outside"):
        gate.validate(manifest, repo=repo, deployment_contract=inside)
    original = copy.deepcopy(expected)
    for field, value in [("satellite_hosts", ["edge", "edge"]), ("prechange_hosts", []),
                         ("satellite_hosts", ["local"]), ("pin_owner", "invalid")]:
        expected.clear(); expected.update(original); expected[field] = value; write()
        with pytest.raises(gate.ManifestError):
            gate.validate(manifest, repo=repo, deployment_contract=contract)
