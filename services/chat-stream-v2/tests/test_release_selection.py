"""Synthetic release admission; never launches a provider or activates a daemon."""
import copy
import hashlib
import json
import subprocess
import sys
import shutil
from pathlib import Path

import pytest

from deploy import release_selection as selection


SHA = "a" * 40
BASE = "b" * 40


def test_missing_installed_baselines_refuses(tmp_path):
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"schema": "pentacle.release-inputs.v1", "candidate": SHA,
                                 "installed": [], "external_inputs": {}}))
    with pytest.raises(selection.Invalid, match="installed"):
        selection.prepare(Path(__file__).parents[3], SHA, inputs, sys.executable, lambda *_: None)


def test_initial_real_qualification_is_empty():
    assert selection.QUALIFIED_SEAMS == {}


def test_required_seam_cannot_be_cured_by_a_green_broad_gate():
    plan = {"boundaries": [{"id": "provider_startup", "missing_coverage": ["live reply"]}],
            "classification": "broad_fallback"}
    with pytest.raises(selection.Invalid, match="unqualified.*provider_startup"):
        selection.require_qualification(plan)


@pytest.mark.parametrize("row", [
    {"passed": False}, {"passed": True, "returncode": 1},
    {"passed": True, "returncode": 0, "outcome": "UNTESTED"},
    {"passed": True, "returncode": 0, "outcome": "pass", "counts": {"failures": 1, "errors": 0, "skipped": 0}},
])
def test_non_green_result_refuses(row):
    with pytest.raises(selection.Invalid, match="non-green"):
        selection.check_result(row)


def test_duplicate_passing_rows_are_not_coverage():
    plan = {"identity": {"candidate": SHA}, "targets": [{"target": "web-gate", "select": [], "files": []}]}
    row = {"target": plan["targets"][0], "passed": True, "returncode": 0,
           "outcome": "pass", "counts": {"failures": 0, "errors": 0, "skipped": 0}}
    with pytest.raises(selection.Invalid, match="duplicate"):
        selection.validate_results(plan, {"identity": plan["identity"], "results": [row, row]})


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def release(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = Path(__file__).resolve().parents[3]
    for name in ("select_tests.py", "test-selection.json", "gate-env.json", "gate_env.py"):
        target = repo / "scripts" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source / "scripts" / name, target)
    (repo / "scripts/lib").mkdir()
    shutil.copyfile(source / "scripts/lib/gate_attempts.py", repo / "scripts/lib/gate_attempts.py")
    mapping = json.loads((repo / "scripts/test-selection.json").read_text())
    for target in mapping["full_gates"] + [t for b in mapping["boundaries"].values() for t in b["targets"]]:
        for filename in target["files"]:
            path = repo / filename
            if filename.endswith("/tests"):
                path = path / "test_fixture.py"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# synthetic target\n")
    calls = []
    def runner(cmd, cwd):
        calls.append(tuple(str(p) for p in cmd))
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    def git(*args):
        result = runner(("git", *args), repo)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    git("init")
    git("config", "user.name", "Pentacle Agent")
    git("config", "user.email", "agent@users.noreply.github.com")
    def commit(name):
        git("add", ".")
        git("commit", "-m", name)
        return git("rev-parse", "HEAD")
    old = commit("baseline")
    (repo / "renderer").mkdir()
    (repo / "renderer/fixture.js").write_text("// changed client source\n")
    client = commit("client change")
    (repo / "README.md").write_text("docs\n")
    head = commit("docs change")
    installed = []
    for role, sha in (("daemon", client), ("web", old), ("satellite", client), ("mobile", SHA)):
        repository = selection.PUBLIC_REPO if role != "mobile" else "https://github.com/HJK6/pentacle-mobile.git"
        receipt = _write(tmp_path / (role + ".json"), {"component": role, "repository": repository,
                                                       "sha": sha, "observed": True})
        installed.append({"component": role, "role": role, "repository": repository, "sha": sha, "receipt": receipt})
    external = {}
    for label in selection.EXTERNAL:
        value = {"identity": {"revision": "fixture"}, "files": [_write(tmp_path / (label + ".json"), {"fixture": True})]}
        if label == "client_versions":
            value["identity"]["mobile_source_shas"] = [SHA]
        external[label] = {"installed": copy.deepcopy(value), "candidate": copy.deepcopy(value)}
    inputs = {"schema": "pentacle.release-inputs.v1", "candidate": head,
              "installed": installed, "external_inputs": external,
              "activation": {"url": "ws://127.0.0.1:12345", "host": "primary"}}
    input_path = tmp_path / "release-inputs.json"
    def prepare():
        _write(input_path, inputs)
        return selection.prepare(repo, inputs["candidate"], input_path, sys.executable, runner)
    return repo, inputs, prepare, calls, commit, runner, input_path


def _qualify(monkeypatch, plan):
    scope = {k: v for k, v in plan["identity"].items()
             if k not in {"candidate", "inputs_sha256", "qualification_sha256"}}
    monkeypatch.setattr(selection, "QUALIFIED_SEAMS", {
        b["id"]: {"scope": scope, "resolved_missing_coverage": b["missing_coverage"],
                  "qa_report": "synthetic-only", "evidence_sha256": "f" * 64}
        for b in plan["boundaries"]})


def test_union_uses_old_installed_web_baseline_even_when_latest_diff_is_docs(release):
    _, _, prepare, calls, *_ = release
    plan = prepare()
    assert [b["id"] for b in plan["boundaries"]] == ["client_wire_render"]
    selector_calls = [c for c in calls if any(p.endswith("/select_tests.py") for p in c)]
    assert len(selector_calls) == 2  # distinct actual installed same-repo baselines
    assert all(SHA not in c for c in selector_calls)  # mobile SHA is external
    assert {r["classification"] for r in plan["reports"]} == {"docs_only", "mapped"}


def test_config_only_drift_selects_affected_provider(release):
    _, inputs, prepare, *_ = release
    inputs["external_inputs"]["provider_config"]["candidate"]["identity"]["revision"] = "new"
    plan = prepare()
    assert {b["id"] for b in plan["boundaries"]} == {"client_wire_render", "provider_startup"}


@pytest.mark.parametrize("mutation,message", [
    (lambda i: i["installed"].pop(), "missing installed component"),
    (lambda i: i["installed"].append(copy.deepcopy(i["installed"][0])), "duplicate"),
    (lambda i: i["external_inputs"].pop("deploy_config"), "missing external"),
    (lambda i: i["external_inputs"]["client_versions"]["installed"]["identity"].update(mobile_source_shas=[BASE]), "consumer source mismatch"),
    (lambda i: i.update(candidate=SHA), "candidate checkout mismatch"),
])
def test_incomplete_inputs_are_precise_refusals(release, mutation, message):
    _, inputs, prepare, *_ = release
    mutation(inputs)
    with pytest.raises(selection.Invalid, match=message):
        prepare()


def test_changed_external_file_invalidates_tuple_before_selection(release):
    _, inputs, prepare, calls, *_ = release
    path = Path(inputs["external_inputs"]["provider_config"]["candidate"]["files"][0]["path"])
    path.write_text("drift")
    with pytest.raises(selection.Invalid, match="changed bound file"):
        prepare()
    assert not any(any(p.endswith("select_tests.py") for p in c) for c in calls)


def test_unknown_change_selects_broad_but_missing_proof_is_still_non_green(release):
    repo, inputs, prepare, _, commit, *_ = release
    (repo / "unknown-runtime.py").write_text("# unknown\n")
    inputs["candidate"] = commit("unknown source")
    plan = prepare()
    assert plan["classification"] == "broad_fallback"
    assert {b["id"] for b in plan["boundaries"]} == set(selection.BOUNDARY_INPUT)
    assert not any(t["target"] == "v2-merge" for t in plan["targets"])
    with pytest.raises(selection.Invalid, match="unqualified"):
        selection.require_qualification(plan)


def test_missing_seam_file_never_becomes_a_safe_empty_selection(release):
    repo, inputs, prepare, _, commit, *_ = release
    (repo / "test/e2e/web_gate.js").unlink()
    inputs["candidate"] = commit("delete seam")
    with pytest.raises(selection.Invalid, match="missing/deleted seam"):
        prepare()


def test_dirty_source_refuses_instead_of_reusing_commit_label(release):
    repo, _, prepare, *_ = release
    (repo / "scripts/test-selection.json").write_text("{}")
    with pytest.raises(selection.Invalid, match="changed candidate source"):
        prepare()


def _execution_runner(plan, calls):
    def runner(cmd, cwd):
        calls.append(tuple(cmd))
        out = Path(cmd[cmd.index("--evidence-dir") + 1])
        target = cmd[cmd.index("--target") + 1]
        selects = [cmd[n + 1] for n, p in enumerate(cmd) if p == "--select"]
        proof = _write(out / "subject.json", {"synthetic": True})
        _write(out / "attempts/owned/0001-started.json", {
            "schema": "pentacle.test-attempt-event.v1", "state": "started", "attempt_id": "owned", "target": target, "selected": {"select": selects},
            "identities": {"source": {"sha": plan["identity"]["candidate"], "dirty": False}}})
        _write(out / "attempts/owned/0002-prepared.json", {
            "schema": "pentacle.test-attempt-event.v1", "state": "prepared", "attempt_id": "owned",
            "toolchain": {k: plan["identity"]["toolchain"][k] for k in plan["identity"]["target_needs"][target]}})
        _write(out / "attempts/owned/0004-terminal.json", {
            "schema": "pentacle.test-attempt-event.v1", "state": "terminal", "attempt_id": "owned", "outcome": "pass", "first_failure": None,
            "secondary": {"counts": {"failures": 0, "errors": 0, "skipped": 0}},
            "detail": {"returncode": 0}, "evidence": [proof]})
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return runner


def test_qualified_selection_executes_exact_targets_and_reuses_result(release, tmp_path, monkeypatch):
    repo, _, prepare, *_ = release
    plan = prepare()
    _qualify(monkeypatch, plan)
    calls = []
    receipt = selection.run_selected(plan, repo, sys.executable, _execution_runner(plan, calls),
                                     tmp_path / "work", tmp_path / "proof")
    assert len(calls) == len(plan["targets"]) == 3
    assert all("run_gate.py" not in " ".join(c) and "v2-merge" not in c for c in calls)
    assert {c[c.index("--target") + 1] for c in calls} == {"v2-pytest", "npm-test", "web-gate"}
    assert selection.validate_results(plan, receipt) == receipt
    assert len(calls) == 3
    # Same candidate SHA, different external config: old result cannot qualify it.
    changed = copy.deepcopy(plan)
    changed["identity"]["external_inputs"]["provider_config"]["candidate"]["identity"]["revision"] = "new"
    with pytest.raises(selection.Invalid, match="stale seam identity"):
        selection.validate_results(changed, receipt)


def test_broad_with_exact_complete_synthetic_proof_is_distinct_from_gap(release, tmp_path, monkeypatch):
    repo, inputs, prepare, _, commit, *_ = release
    (repo / "unknown.py").write_text("# broad\n")
    inputs["candidate"] = commit("unknown")
    plan = prepare()
    _qualify(monkeypatch, plan)
    calls = []
    section = selection.run_selected(plan, repo, sys.executable, _execution_runner(plan, calls),
                                     tmp_path / "work", tmp_path / "proof")
    assert len(section["results"]) == len(plan["targets"])
    assert selection.validate_results(plan, section) == section


@pytest.mark.parametrize("field", ["map_sha256", "source_tree_sha256", "gate_env_sha256", "admission_sha256"])
def test_changed_source_or_harness_invalidates_result(release, tmp_path, monkeypatch, field):
    repo, _, prepare, *_ = release
    plan = prepare()
    _qualify(monkeypatch, plan)
    section = selection.run_selected(plan, repo, sys.executable, _execution_runner(plan, []),
                                     tmp_path / "work", tmp_path / "proof")
    changed = copy.deepcopy(plan)
    changed["identity"][field] = "0" * 64
    with pytest.raises(selection.Invalid, match="stale seam identity"):
        selection.validate_results(changed, section)


def test_existing_client_census_fields_are_consumed_without_rewriting_receipt(release, tmp_path):
    _, inputs, prepare, *_ = release
    census = {"installed_web": {"source": inputs["installed"][1]["sha"], "active_pid": 123, "files": {"bundle.js": "f" * 64}},
              "installed_mobile": {"source": SHA, "build": "123"},
              "receipts": {"artifact": _write(tmp_path / "artifact.json", {"fixture": True})}}
    binding = _write(tmp_path / "inventory.json", census)
    for row in inputs["installed"]:
        if row["role"] in {"web", "mobile"}:
            row.update(receipt=binding, receipt_format="client_inventory")
    plan = prepare()
    assert {b["id"] for b in plan["boundaries"]} == {"client_wire_render"}


def test_satellite_receipt_uses_running_source_and_fresh_tuple(tmp_path):
    import time
    peer = {"host": "secondary", "online": True, "installed_source_sha": BASE,
            **{k: "f" * 64 for k in ("dependency_digest", "consumer_payload_sha256", "configuration_sha256", "installed_cli_sha256")}}
    payload = {"candidate": SHA, "observed_at_epoch": time.time(), "participants": [peer]}
    path = tmp_path / "participants.json"
    row = {"component": "secondary", "role": "satellite", "sha": BASE,
           "receipt_format": "satellite_participants", "receipt": _write(path, payload)}
    selection.installed_receipt(row, SHA)
    peer["installed_source_sha"] = SHA
    row["receipt"] = _write(path, payload)
    with pytest.raises(selection.Invalid, match="missing observed satellite baseline"):
        selection.installed_receipt(row, SHA)
    peer["installed_source_sha"] = BASE
    payload["observed_at_epoch"] -= 901
    row["receipt"] = _write(path, payload)
    with pytest.raises(selection.Invalid, match="stale satellite"):
        selection.installed_receipt(row, SHA)


def test_docs_only_is_empty_only_when_all_actual_baselines_agree(release):
    _, inputs, prepare, *_ = release
    for row in inputs["installed"]:
        if row["role"] != "mobile":
            row["sha"] = inputs["candidate"]
            row["receipt"] = _write(Path(row["receipt"]["path"]),
                {"component": row["component"], "repository": row["repository"], "sha": row["sha"], "observed": True})
    plan = prepare()
    assert plan["classification"] == "docs_only" and plan["targets"] == []
    selection.require_qualification(plan)


def test_same_candidate_tuple_drift_runs_affected_provider_target(release, tmp_path, monkeypatch):
    repo, inputs, prepare, *_ = release
    # Establish source-equivalent installed components: only external drift selects.
    for row in inputs["installed"]:
        if row["role"] != "mobile":
            row["sha"] = inputs["candidate"]
            row["receipt"] = _write(Path(row["receipt"]["path"]),
                {"component": row["component"], "repository": row["repository"], "sha": row["sha"], "observed": True})
    inputs["external_inputs"]["provider_config"]["candidate"]["identity"]["revision"] = "changed"
    plan = prepare()
    _qualify(monkeypatch, plan)
    calls = []
    selection.run_selected(plan, repo, sys.executable, _execution_runner(plan, calls), tmp_path / "work", tmp_path / "proof")
    assert len(calls) == 1 and calls[0][calls[0].index("--target") + 1] == "v2-pytest"
    assert "tests/test_provider_account_native.py" in calls[0]


@pytest.mark.parametrize("corruption", ["source", "toolchain", "terminal", "artifact"])
def test_execution_artifact_drift_cannot_hide_behind_a_passing_result(release, tmp_path, monkeypatch, corruption):
    repo, _, prepare, *_ = release
    plan = prepare()
    _qualify(monkeypatch, plan)
    section = selection.run_selected(plan, repo, sys.executable, _execution_runner(plan, []), tmp_path / "work", tmp_path / "proof")
    evidence = section["results"][0]["evidence"]
    state = {"source": "started", "toolchain": "prepared", "terminal": "terminal", "artifact": "terminal"}[corruption]
    path = Path(evidence[state]["path"])
    event = json.loads(path.read_text())
    if corruption == "source":
        event["identities"]["source"]["sha"] = BASE
    elif corruption == "toolchain":
        event["toolchain"] = {}
    elif corruption == "terminal":
        event["secondary"]["counts"]["errors"] = 1
    else:
        Path(event["evidence"][0]["path"]).write_text("changed subject evidence")
    evidence[state] = _write(path, event)
    with pytest.raises(selection.Invalid):
        selection.validate_results(plan, section)
