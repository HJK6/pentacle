"""Admission/refusal through the real deploy transaction, with isolated launchd."""
from pathlib import Path
import hashlib
import json

import pytest

from .test_deploy_log_guard import isolated_release


@pytest.mark.parametrize("reason", [
    "missing installed baselines", "consumer source mismatch", "stale seam identity",
    "unqualified required seam provider_startup", "missing/deleted seam target",
    "duplicate seam result", "non-green seam execution", "invalid selector result/identity",
])
def test_bad_selection_refuses_before_activation_and_restores_preimage(isolated_release, monkeypatch, reason):
    module, service, repo, prior, _, before, calls, activate = isolated_release
    original = module.deploy
    monkeypatch.setattr(module, "deploy", lambda *a, **kw: original(*a, release_inputs=Path("/synthetic-inputs.json"), **kw))
    def refuse(*_):
        raise module.release_selection.Invalid("release selection: " + reason)
    monkeypatch.setattr(module.release_selection, "admit", refuse)
    with pytest.raises(module.DeployError, match=reason):
        activate()
    assert not any(c[0] == "launchctl" for c in calls)
    assert module._stamp_path(repo, service).read_bytes() == before
    assert module._git(repo, "rev-parse", "HEAD", runner=module._run) == prior


def test_supplied_merge_receipt_is_reused_once_and_seams_admitted(isolated_release, monkeypatch):
    module, _, _, _, target, _, calls, activate = isolated_release
    original = module.deploy
    monkeypatch.setattr(module, "deploy", lambda *a, **kw: original(*a, release_inputs=Path("/synthetic-inputs.json"), **kw))
    admitted = []
    def admit(*args):
        evidence = args[3]
        assert evidence["sha"] == target and evidence["passed"] is True
        evidence["seam_results"] = {"identity": {"candidate": target}, "results": []}
        admitted.append(True)
        return {"identity": {"candidate": target}}
    monkeypatch.setattr(module.release_selection, "admit", admit)
    monkeypatch.setattr(module.release_selection, "revalidate", lambda *_: None)
    monkeypatch.setattr(module, "_validate_selected_activation", lambda *_: None)
    monkeypatch.setattr(module, "run_gate_and_write_evidence", lambda *_: pytest.fail("duplicate merge gate"))
    stamp = activate()
    assert admitted == [True]
    assert stamp["selective_qualification"] == "admitted"
    assert stamp["gates"]["evidence_source"] == "file"
    assert len([c for c in calls if "kickstart" in c or "bootstrap" in c]) == 1


def test_legacy_mode_never_claims_selective_qualification(isolated_release):
    *_, activate = isolated_release
    assert activate()["selective_qualification"] == "not_enabled"


@pytest.mark.parametrize("mismatch", [None, "endpoint", "host", "configuration"])
def test_activation_floor_is_bound_to_actual_local_runtime_config(tmp_path, monkeypatch, mismatch):
    from .test_deploy_script import deploy_mod, V2_SERVICE
    machines = tmp_path / "machines.json"
    machines.write_text(json.dumps([{"name": "primary", "ssh_target": None},
                                    {"name": "secondary", "ssh_target": "secondary.example.invalid"}]))
    monkeypatch.setattr(deploy_mod, "_launchd_environment", lambda _: {"PENTACLE_MACHINES_FILE": str(machines)})
    binding = {"path": str(machines), "sha256": hashlib.sha256(machines.read_bytes()).hexdigest()}
    plan = {"identity": {"activation": {"url": "ws://127.0.0.1:7791", "host": "primary"},
                         "external_inputs": {"deploy_config": {"candidate": {"files": [binding]}}}}}
    if mismatch == "endpoint":
        plan["identity"]["activation"]["url"] = "ws://127.0.0.1:12345"
    elif mismatch == "host":
        plan["identity"]["activation"]["host"] = "secondary"
    elif mismatch == "configuration":
        binding["sha256"] = "0" * 64
    if mismatch:
        with pytest.raises(deploy_mod.DeployError, match="release selection"):
            deploy_mod._validate_selected_activation(plan, tmp_path, V2_SERVICE)
    else:
        deploy_mod._validate_selected_activation(plan, tmp_path, V2_SERVICE)
