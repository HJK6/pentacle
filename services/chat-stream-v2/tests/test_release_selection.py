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


def test_real_qualification_never_claims_other_surfaces():
    assert set(selection.QUALIFIED_SEAMS) <= {"client_wire_render", "provider_startup", "satellite_transition", "deploy_invocation"}


def test_required_seam_cannot_be_cured_by_a_green_broad_gate(monkeypatch):
    monkeypatch.setattr(selection, "QUALIFIED_SEAMS", {})
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


def _seed_selector_files(repo):
    """Existing selector bytes with explicitly synthetic target bodies."""
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


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.setattr(selection, "QUALIFIED_SEAMS", {})
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_selector_files(repo)
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
    _matching_web_tuple(inputs, tmp_path)
    external["client_versions"]["candidate"] = copy.deepcopy(external["client_versions"]["installed"])
    input_path = tmp_path / "release-inputs.json"
    def prepare():
        _write(input_path, inputs)
        return selection.prepare(repo, inputs["candidate"], input_path, sys.executable, runner)
    return repo, inputs, prepare, calls, commit, runner, input_path


def _qualify(monkeypatch, plan):
    # Synthetic proof for admission mechanics; never inserted into public data.
    context = {"source_sha256": "f" * 64, "source_reference_sha256": "f" * 64}
    plan["qualification_contexts"] = {b["id"]: context for b in plan["boundaries"]}
    monkeypatch.setattr(selection, "QUALIFIED_SEAMS", {
        b["id"]: {"scope_sha256": selection.digest(selection.key(context).encode()),
                  "assertion_scope": ["synthetic"], "remaining_coverage": ["synthetic"],
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


def _execution_runner(plan, calls, web_scenario="installed-client-seam"):
    def runner(cmd, cwd):
        calls.append(tuple(cmd))
        out = Path(cmd[cmd.index("--evidence-dir") + 1])
        target = cmd[cmd.index("--target") + 1]
        selects = [cmd[n + 1] for n, p in enumerate(cmd) if p == "--select"]
        proof = _write(out / "subject.json", {"synthetic": True})
        if target == "web-gate":
            _write(out / "browser/web_gate/verdict.json", {"scenario": "web_gate", "status": "PASS", "failedScenarios": 0,
                "steps": [{"name": web_scenario, "ok": True}],
                "fixture_auth_cleanup": {"remaining_credential_count": 0, "registry_removed": True, "token_removed": True},
                "daemon_cleanup": {"exited": True, "forced": False}})
            log = out / "web-browser.log"
            log.write_text(f"▸ {web_scenario}\n◀ web_gate: PASS\n  artifacts: {out / 'browser/web_gate'}\n")
            proof = {"path": str(log), "sha256": selection.digest(log.read_bytes())}
        _write(out / "attempts/owned/0001-started.json", {
            "schema": "pentacle.test-attempt-event.v1", "state": "started", "attempt_id": "owned", "target": target,
            "selected": {"select": selects, "pass_env_names": [cmd[n + 1] for n, p in enumerate(cmd) if p == "--pass-env"]},
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
    web = next(c for c in calls if c[c.index("--target") + 1] == "web-gate")
    assert web[:2] == ("/usr/bin/env", "PENTACLE_WEB_GATE_SCENARIOS=installed-client-seam")
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
    census = {"installed_web": inputs["external_inputs"]["client_versions"]["installed"]["identity"]["web"],
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
    _matching_web_tuple(inputs, Path(inputs["installed"][1]["receipt"]["path"]).parent)
    inputs["external_inputs"]["client_versions"]["candidate"] = copy.deepcopy(inputs["external_inputs"]["client_versions"]["installed"])
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
    _matching_web_tuple(inputs, tmp_path)
    inputs["external_inputs"]["client_versions"]["candidate"] = copy.deepcopy(inputs["external_inputs"]["client_versions"]["installed"])
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


def test_boundary_qualification_is_digest_only_and_unrelated_missing_proof_still_blocks(release, monkeypatch):
    repo, _, prepare, _, _, runner, _ = release
    plan = prepare()
    proof = {"source_ref": plan["identity"]["candidate"], "source_paths": ["renderer/"],
             "input_labels": ["client_versions"], "assertion_scope": ["synthetic client fixture"],
             "remaining_coverage": ["native untested"], "qa_report": "synthetic-only", "evidence_sha256": "f" * 64}
    context = selection.qualification_context(repo, plan, "client_wire_render", proof, runner)
    proof["scope_sha256"] = selection.digest(selection.key(context).encode())
    monkeypatch.setattr(selection, "QUALIFIED_SEAMS", {"client_wire_render": proof})
    plan["qualification_contexts"] = {"client_wire_render": context}
    selection.require_qualification(plan)
    changed = copy.deepcopy(plan)
    changed["identity"]["external_inputs"]["provider_config"]["candidate"]["identity"]["revision"] = "changed"
    assert selection.qualification_context(repo, changed, "client_wire_render", proof, runner) == context
    changed["boundaries"].append({"id": "provider_startup", "missing_coverage": ["live reply"]})
    with pytest.raises(selection.Invalid, match="unqualified required seam provider_startup"):
        selection.require_qualification(changed)


@pytest.mark.parametrize("activation", [
    {"url": "ws://127.0.0.1:12345", "host": "another-host"},
    {"url": "ws://127.0.0.1:54321", "host": "primary"},
])
def test_provider_qualification_is_bound_to_actual_activation(release, monkeypatch, activation):
    repo, inputs, prepare, _, _, runner, _ = release
    inputs["external_inputs"]["provider_config"]["candidate"]["identity"]["revision"] = "changed"
    plan = prepare()
    plan["boundaries"] = [b for b in plan["boundaries"] if b["id"] == "provider_startup"]
    proof = {"source_ref": inputs["candidate"],
             "source_paths": ["services/chat-stream-v2/tests/test_provider_account_native.py"],
             "input_labels": ["provider_config"], "assertion_scope": ["synthetic one-cell fixture"],
             "remaining_coverage": ["other hosts/endpoints untested"],
             "qa_report": "synthetic-only", "evidence_sha256": "f" * 64}
    context = selection.qualification_context(repo, plan, "provider_startup", proof, runner)
    proof["scope_sha256"] = selection.digest(selection.key(context).encode())
    monkeypatch.setattr(selection, "QUALIFIED_SEAMS", {"provider_startup": proof})
    plan["qualification_contexts"] = {"provider_startup": context}
    selection.require_qualification(plan)
    changed = copy.deepcopy(plan)
    changed["identity"]["activation"] = activation
    changed["qualification_contexts"]["provider_startup"] = selection.qualification_context(
        repo, changed, "provider_startup", proof, runner)
    with pytest.raises(selection.Invalid, match="unqualified source/tuple provider_startup"):
        selection.require_qualification(changed)


def test_accepted_source_reference_is_not_relabelled_after_product_change(release, monkeypatch):
    repo, inputs, prepare, _, commit, runner, _ = release
    plan = prepare()
    proof = {"source_ref": plan["identity"]["candidate"], "source_paths": ["renderer/"],
             "input_labels": ["client_versions"], "assertion_scope": ["synthetic fixture"],
             "remaining_coverage": ["native untested"], "qa_report": "synthetic-only", "evidence_sha256": "f" * 64}
    context = selection.qualification_context(repo, plan, "client_wire_render", proof, runner)
    proof["scope_sha256"] = selection.digest(selection.key(context).encode())
    monkeypatch.setattr(selection, "QUALIFIED_SEAMS", {"client_wire_render": proof})
    (repo / "renderer/fixture.js").write_text("// changed after accepted source\n")
    inputs["candidate"] = commit("product source changed")
    changed = prepare()
    with pytest.raises(selection.Invalid, match="unqualified source/tuple"):
        selection.require_qualification(changed)


def test_source_scope_includes_git_paths_with_embedded_newlines(release):
    repo, inputs, _, _, commit, runner, _ = release
    before = selection.source_scope(repo, inputs["candidate"], ["renderer/"], runner)
    (repo / "renderer/new\nmodule.js").write_text("// source with a Git-valid path\n")
    changed = commit("add source with newline path")
    assert selection.source_scope(repo, changed, ["renderer/"], runner) != before


def test_valid_same_source_receipt_from_another_named_scenario_refuses(release, tmp_path, monkeypatch):
    repo, _, prepare, *_ = release
    plan = prepare()
    _qualify(monkeypatch, plan)
    # Target, clean SHA, toolchain, pass_env_names and pass outcome all match;
    # only the actual browser scenario differs from the required seam.
    with pytest.raises(selection.Invalid, match="scenario"):
        selection.run_selected(plan, repo, sys.executable, _execution_runner(plan, [], "another-valid-scenario"),
                               tmp_path / "work", tmp_path / "proof")


@pytest.mark.parametrize("corruption", ["missing_log", "extra_scenario", "no_pass", "no_artifacts",
                                       "status", "step", "cleanup", "missing_binding", "changed_verdict"])
def test_browser_scenario_evidence_cannot_be_substituted(release, tmp_path, monkeypatch, corruption):
    repo, _, prepare, *_ = release
    plan = prepare()
    _qualify(monkeypatch, plan)
    section = selection.run_selected(plan, repo, sys.executable, _execution_runner(plan, []),
                                     tmp_path / "work", tmp_path / "proof")
    row = next(r for r in section["results"] if r["target"]["target"] == "web-gate")
    terminal_path = Path(row["evidence"]["terminal"]["path"])
    terminal = json.loads(terminal_path.read_text())
    log = Path(terminal["evidence"][0]["path"])
    verdict_path = Path(row["scenario_verdict"]["path"])
    verdict = json.loads(verdict_path.read_text())
    if corruption == "missing_log":
        terminal["evidence"] = [_write(log.with_name("other.log"), {"passed": True})]
    elif corruption in {"extra_scenario", "no_pass", "no_artifacts"}:
        text = log.read_text()
        text = (text + "▸ another-valid-scenario\n" if corruption == "extra_scenario" else
                text.replace("◀ web_gate: PASS", "◀ web_gate: FAIL") if corruption == "no_pass" else
                "\n".join(line for line in text.splitlines() if not line.startswith("  artifacts:")))
        log.write_text(text)
        terminal["evidence"][0]["sha256"] = selection.digest(log.read_bytes())
    elif corruption == "missing_binding":
        row.pop("scenario_verdict")
    else:
        if corruption == "status":
            verdict["status"] = "FAIL"
        elif corruption == "step":
            verdict["steps"][0]["ok"] = False
        elif corruption == "cleanup":
            verdict["daemon_cleanup"]["forced"] = True
        else:
            verdict["changed_after_receipt"] = True
        # Retain the original section binding: even a later green verdict differs.
        _write(verdict_path, verdict)
    row["evidence"]["terminal"] = _write(terminal_path, terminal)
    with pytest.raises(selection.Invalid, match="scenario"):
        selection.validate_results(plan, section)


def _matching_web_tuple(inputs, tmp_path):
    row = next(r for r in inputs['installed'] if r['role'] == 'web')
    artifact = _write(tmp_path / 'installed-web-bundle.json', {'synthetic': 'accepted web artifact'})
    consumer = {'source': row['sha'], 'active_pid': 123, 'files': {'bundle.js': artifact['sha256']}}
    inputs['external_inputs']['client_versions']['installed']['identity']['web'] = copy.deepcopy(consumer)
    if artifact not in inputs['external_inputs']['client_versions']['installed']['files']:
        inputs['external_inputs']['client_versions']['installed']['files'].append(artifact)
    row.update(receipt_format='client_inventory',
               receipt=_write(tmp_path / 'web-inventory.json', {'installed_web': consumer}))
    return row, consumer


def test_other_observed_web_source_cannot_reuse_unchanged_consumer_proof(release, tmp_path, monkeypatch):
    repo, inputs, prepare, _, commit, runner, input_path = release
    row, _ = _matching_web_tuple(inputs, tmp_path)
    original = prepare()
    proof = {'source_ref': inputs['candidate'], 'source_paths': ['renderer/'], 'input_labels': ['client_versions'],
             'assertion_scope': ['synthetic client control'], 'remaining_coverage': ['synthetic external counterparts'],
             'qa_report': 'synthetic-only', 'evidence_sha256': 'f' * 64}
    context = selection.qualification_context(repo, original, 'client_wire_render', proof, runner)
    proof['scope_sha256'] = selection.digest(selection.key(context).encode())
    monkeypatch.setattr(selection, 'QUALIFIED_SEAMS', {'client_wire_render': proof})
    # Match the independent real-Git control: same candidate and external proof,
    # materially different independently observed installed consumer.
    (repo / 'renderer/other-installed.js').write_text('// another installed web tree\n')
    row['sha'] = commit('different installed web source')
    assert runner(('git', 'checkout', '--detach', inputs['candidate']), repo).returncode == 0
    row['receipt'] = _write(Path(row['receipt']['path']),
        {**{k: row[k] for k in ('component', 'repository', 'sha')}, 'observed': True})
    row['receipt_format'] = 'observed_component'
    with pytest.raises(selection.Invalid, match='consumer source mismatch'):
        changed = prepare()
        selection.require_qualification(changed)
        section = selection.run_selected(changed, repo, sys.executable, _execution_runner(changed, []),
                                         tmp_path / 'work', tmp_path / 'proof')
        selection.admit(repo, inputs['candidate'], input_path, {'seam_results': section}, sys.executable, runner)


def test_observed_web_artifact_cannot_contradict_unchanged_consumer_tuple(release, tmp_path):
    _, inputs, prepare, *_ = release
    row, consumer = _matching_web_tuple(inputs, tmp_path)
    changed = copy.deepcopy(consumer)
    changed['files']['bundle.js'] = _write(tmp_path / 'other-bundle.json', {'different': 'actual artifact'})['sha256']
    row.update(receipt_format='client_inventory',
               receipt=_write(tmp_path / 'different-inventory.json', {'installed_web': changed}))
    with pytest.raises(selection.Invalid, match='consumer observation mismatch'):
        prepare()


@pytest.mark.parametrize('corruption', ['metadata_only', 'multiple_web', 'unbound_artifact'])
def test_unsupported_web_observation_fails_closed(release, corruption):
    _, inputs, prepare, *_ = release
    row = next(r for r in inputs['installed'] if r['role'] == 'web')
    if corruption == 'metadata_only':
        row['receipt_format'] = 'observed_component'
        row['receipt'] = _write(Path(row['receipt']['path']),
            {**{k: row[k] for k in ('component', 'repository', 'sha')}, 'observed': True})
        reason = 'missing installed web artifact observation'
    elif corruption == 'multiple_web':
        another = copy.deepcopy(row)
        another['component'] = 'another-web'
        inputs['installed'].append(another)
        reason = 'unsupported installed web consumer set'
    else:
        value = inputs['external_inputs']['client_versions']['installed']
        artifact_hashes = set(value['identity']['web']['files'].values())
        value['files'] = [b for b in value['files'] if b['sha256'] not in artifact_hashes]
        reason = 'missing installed web artifact binding'
    with pytest.raises(selection.Invalid, match=reason):
        prepare()


def test_exact_observed_web_inventory_is_bound_in_client_qualification(release):
    repo, inputs, prepare, _, _, runner, _ = release
    plan = prepare()
    proof = {'source_ref': inputs['candidate'], 'source_paths': ['renderer/'], 'input_labels': ['client_versions']}
    context = selection.qualification_context(repo, plan, 'client_wire_render', proof, runner)
    observed = selection.read(next(row for row in inputs['installed'] if row['role'] == 'web')['receipt']['path'])['installed_web']
    assert context['installed_web_sha256'] == selection.digest(selection.key(observed).encode())
