"""Input-bound admission for the named integration seams, alongside merge evidence.

This is deliberately not a qualification authority. Accepted boundary evidence
must be reviewed and registered here before any real tuple can use selection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts/lib"))
from instruction_snapshot import ENV_NAME, instruction_snapshot


PUBLIC_REPO = "https://github.com/HJK6/pentacle.git"
EXTERNAL = {"client_versions", "provider_config", "satellite_config", "deploy_config"}
ROLES = {"daemon", "web", "mobile", "satellite"}
BOUNDARY_INPUT = {"client_wire_render": ("client_versions", "satellite_config"),
                  "provider_startup": ("provider_config",),
                  "satellite_transition": ("satellite_config",),
                  "deploy_invocation": ("deploy_config",)}
# Only independently accepted finite scopes belong here. A larger suite cannot
# resolve a known gap. Never populate this registry from a release caller.
QUALIFICATION_PATH = Path(__file__).with_name("qualified_seams.json")
QUALIFIED_SEAMS: dict[str, dict] = json.loads(QUALIFICATION_PATH.read_bytes())
LOADED_QUALIFICATION_SHA256 = hashlib.sha256(QUALIFICATION_PATH.read_bytes()).hexdigest()
LOADED_ADMISSION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
INSTRUCTION_PATH = Path(__file__).resolve().parents[3] / "scripts/lib/instruction_snapshot.py"
LOADED_INSTRUCTION_SHA256 = hashlib.sha256(INSTRUCTION_PATH.read_bytes()).hexdigest()
TOOLCHAIN_QUERY = """
import json, sys
sys.path.insert(0, sys.argv[1] + '/scripts')
import gate_env
tools, failures = gate_env.resolve_toolchain(gate_env.load_config(), ['python', 'node', 'npm', 'tmux'])
print(json.dumps({'tools': {k: {f: v[f] for f in ('realpath', 'version', 'sha256')} for k,v in tools.items()}, 'failures': failures}))
"""


class Invalid(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Invalid("release selection: " + reason)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def read(path):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            require(name not in result, "duplicate JSON key " + name)
            result[name] = value
        return result
    try:
        return json.loads(Path(path).read_bytes(), object_pairs_hook=unique)
    except (OSError, ValueError) as exc:
        raise Invalid(f"release selection: invalid input {path}: {exc}") from exc


def file_binding(binding):
    require(isinstance(binding, dict) and set(binding) == {"path", "sha256"}, "invalid file binding")
    path = Path(binding["path"])
    require(path.is_absolute(), "binding path must be absolute")
    try:
        actual = digest(path.read_bytes())
    except OSError as exc:
        raise Invalid(f"release selection: missing bound file {path}") from exc
    require(actual == binding["sha256"], f"changed bound file {path}")
    return binding


def full_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is not None


def helper_manifest(peer):
    """Configured helper disk/invocation identity; never loaded-runtime proof."""
    sha = peer.get("installed_helpers_sha256")
    require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha), "missing helper manifest SHA256")
    binding = peer.get("installed_helpers_manifest")
    require(isinstance(binding, dict) and binding.get("sha256") == sha, "helper manifest digest mismatch")
    data = read(file_binding(binding)["path"])
    require(isinstance(data, dict) and set(data) == {"host", "configured_helpers", "files", "limits"}
            and data["host"] == peer.get("host") and isinstance(data["limits"], list) and data["limits"],
            "malformed helper identity manifest")
    identity, files = data["configured_helpers"], data["files"]
    require(isinstance(identity, dict) and identity.get("host") == data["host"]
            and isinstance(identity.get("invocations"), list) and identity["invocations"]
            and isinstance(identity.get("closures"), list) and identity["closures"]
            and isinstance(identity.get("interpreters"), dict) and identity["interpreters"]
            and isinstance(identity.get("loaded_module_provenance"), dict) and identity["loaded_module_provenance"]
            and isinstance(files, list) and files, "incomplete configured helper identity")
    for item in [binding, *files]:
        file_binding(item)
        path = Path(item["path"])
        require(not path.is_symlink() and not path.stat().st_mode & 0o222, "helper identity must be retained read-only")
    return [binding, *files]


def installed_receipt(row, candidate):
    receipt = read(file_binding(row["receipt"])["path"])
    require(isinstance(receipt, dict), "invalid installed baseline receipt")
    kind = row.get("receipt_format", "observed_component")
    if kind == "client_inventory":
        require(row["role"] in {"web", "mobile"}, "unsupported client inventory role")
        consumer = receipt.get("installed_" + row["role"], {})
        require(consumer.get("source") == row["sha"], "consumer source mismatch")
        require((type(consumer.get("active_pid")) is int and consumer["active_pid"] > 0 and consumer.get("files"))
                if row["role"] == "web" else bool(consumer.get("build")), "missing installed consumer identity")
        for binding in receipt.get("receipts", {}).values():
            file_binding({k: binding[k] for k in ("path", "sha256")})
    elif kind == "daemon_stamp":
        runtime = receipt.get("daemon_runtime_readback", {})
        require(row["role"] == "daemon" and runtime.get("outcome") == "observed"
                and runtime.get("sha") == row["sha"] and runtime.get("target_sha") == row["sha"]
                and type(runtime.get("pid")) is int and runtime["pid"] > 0,
                "missing observed daemon baseline")
    elif kind == "satellite_participants":
        age = time.time() - receipt.get("observed_at_epoch", 0)
        require(receipt.get("candidate") == candidate and 0 <= age <= 900, "stale satellite participant receipt")
        participants = [p for p in receipt.get("participants", []) if p.get("host") == row["component"]]
        require(len(participants) == 1, "missing/duplicate satellite participant")
        peer = participants[0]
        require(row["role"] == "satellite" and peer.get("online") is True
                and peer.get("installed_source_sha") == row["sha"], "missing observed satellite baseline")
        require(all(isinstance(peer.get(k), str) and re.fullmatch(r"[0-9a-f]{64}", peer[k])
                    for k in ("dependency_digest", "consumer_payload_sha256", "configuration_sha256", "installed_cli_sha256")),
                "missing satellite tuple identity")
        helper_manifest(peer)
    else:
        require(kind == "observed_component" and isinstance(receipt, dict)
                and all(receipt.get(name) == row[name] for name in ("component", "repository", "sha"))
                and receipt.get("observed") is True, "installed baseline receipt mismatch")


def observed_web_consumer(identity):
    rows = [row for row in identity["installed"] if row["role"] == "web"]
    require(len(rows) == 1, "unsupported installed web consumer set")
    row = rows[0]
    external = identity["external_inputs"]["client_versions"]["installed"]
    consumer = external["identity"].get("web")
    require(isinstance(consumer, dict) and consumer.get("source") == row["sha"], "consumer source mismatch")
    require(row.get("receipt_format") == "client_inventory", "missing installed web artifact observation")
    observed = read(file_binding(row["receipt"])["path"]).get("installed_web")
    require(observed == consumer, "consumer observation mismatch")
    artifacts = consumer.get("files")
    require(type(consumer.get("active_pid")) is int and consumer["active_pid"] > 0
            and isinstance(artifacts, dict) and artifacts
            and all(isinstance(name, str) and name and isinstance(sha, str)
                    and re.fullmatch(r"[0-9a-f]{64}", sha) for name, sha in artifacts.items()),
            "missing installed web artifact observation")
    require(set(artifacts.values()) <= {b["sha256"] for b in external["files"]},
            "missing installed web artifact binding")
    return observed


def prepare(repo, sha, input_path, python, runner):
    """Union actual installed receipts, not the last PR or runtime_manifest."""
    inputs = read(input_path)
    require(isinstance(inputs, dict) and inputs.get("schema") == "pentacle.release-inputs.v1", "invalid input schema")
    require(full_sha(sha) and inputs.get("candidate") == sha, "candidate mismatch")
    require(digest(Path(__file__).read_bytes()) == LOADED_ADMISSION_SHA256, "admission code changed after load")
    require(digest(QUALIFICATION_PATH.read_bytes()) == LOADED_QUALIFICATION_SHA256, "qualification changed after load")
    installed = inputs.get("installed")
    require(isinstance(installed, list) and installed, "missing installed baselines")
    seen, roles, bases = set(), set(), set()
    for row in installed:
        require(isinstance(row, dict) and {"component", "role", "repository", "sha", "receipt"} <= set(row)
                and set(row) <= {"component", "role", "repository", "sha", "receipt", "receipt_format"},
                "invalid installed baseline")
        require(isinstance(row["component"], str) and row["component"] and row["component"] not in seen,
                "duplicate/invalid installed component")
        seen.add(row["component"])
        require(row["role"] in ROLES and full_sha(row["sha"]), "invalid installed source identity")
        roles.add(row["role"])
        installed_receipt(row, sha)
        expected_repo = "https://github.com/HJK6/pentacle-mobile.git" if row["role"] == "mobile" else PUBLIC_REPO
        require(row["repository"] == expected_repo, "unsupported installed repository")
        if row["repository"] == PUBLIC_REPO:
            bases.add(row["sha"])
    require(roles == ROLES and bases, "missing installed component roles")
    current = runner(("git", "rev-parse", "HEAD"), repo)
    require(current.returncode == 0 and current.stdout.strip() == sha, "candidate checkout mismatch")
    status = runner(("git", "status", "--porcelain"), repo)
    require(status.returncode == 0 and not any(line and not line.startswith("?? .pentacle-deploy/")
            for line in status.stdout.splitlines()), "changed candidate source")
    external = inputs.get("external_inputs")
    require(isinstance(external, dict) and set(external) == EXTERNAL, "missing external input identity")
    changed = []
    for name, pair in external.items():
        require(isinstance(pair, dict) and set(pair) == {"installed", "candidate"}, "invalid external input " + name)
        for value in pair.values():
            require(isinstance(value, dict) and set(value) == {"identity", "files"}
                    and isinstance(value["identity"], dict) and value["identity"]
                    and isinstance(value["files"], list) and value["files"], "unsupported external identity " + name)
            for binding in value["files"]:
                file_binding(binding)
        if pair["installed"] != pair["candidate"]:
            changed.append(name)
    for row in installed:
        if row.get("receipt_format") == "satellite_participants":
            peer = next(p for p in read(row["receipt"]["path"])["participants"] if p["host"] == row["component"])
            bindings = helper_manifest(peer)
            require(all(b in external["satellite_config"][side]["files"] for side in ("installed", "candidate") for b in bindings),
                    "unbound configured helper identity")
    # Mobile belongs to a different repository; never pass its SHA to Pentacle Git.
    mobile = sorted(row["sha"] for row in installed if row["role"] == "mobile")
    require(external["client_versions"]["installed"]["identity"].get("mobile_source_shas") == mobile,
            "consumer source mismatch")
    observed_web_consumer({"installed": installed, "external_inputs": external})
    candidate_mobile = external["client_versions"]["candidate"]["identity"].get("mobile_source_shas")
    require(isinstance(candidate_mobile, list) and candidate_mobile and all(full_sha(v) for v in candidate_mobile),
            "missing candidate consumer source")
    activation = inputs.get("activation")
    require(isinstance(activation, dict) and set(activation) == {"url", "host"}
            and isinstance(activation["host"], str) and activation["host"]
            and isinstance(activation["url"], str) and activation["url"].startswith(("ws://", "wss://")),
            "unsupported activation scope (requires explicit Codex promptless endpoint/host)")
    selector_hash = digest((repo / "scripts/select_tests.py").read_bytes())
    map_hash = digest((repo / "scripts/test-selection.json").read_bytes())
    config_hash = digest((repo / "scripts/gate-env.json").read_bytes())
    tree = runner(("git", "ls-tree", "-r", sha), repo)
    require(tree.returncode == 0, "candidate source is unavailable")
    # Exclude only qualification data to avoid a circular source pin. Admission
    # implementation remains included; result receipts bind registry bytes too.
    source_hash = digest("\n".join(line for line in tree.stdout.splitlines()
        if not line.endswith("\tservices/chat-stream-v2/deploy/qualified_seams.json")).encode())
    discovered = runner((python, "-I", "-B", "-c", TOOLCHAIN_QUERY, str(repo)), repo)
    require(discovered.returncode == 0, "toolchain discovery failed")
    try:
        toolchain = json.loads(discovered.stdout)
    except ValueError as exc:
        raise Invalid("release selection: invalid toolchain discovery") from exc
    require(not toolchain.get("failures") and set(toolchain.get("tools", {})) == {"python", "node", "npm", "tmux"},
            "missing required toolchain")
    tools = toolchain["tools"]
    reports = []
    with tempfile.TemporaryDirectory(prefix="release-select-") as scratch:
        for index, base in enumerate(sorted(bases)):
            out = Path(scratch) / f"{index}.json"
            command = [python, str(repo / "scripts/select_tests.py"), "--base", base, "--head", sha, "--out", str(out)]
            for name in sorted(changed):
                command.extend(("--changed-input", name))
            result = runner(command, repo)
            require(result.returncode == 0, "selector refused installed baseline " + base)
            report = read(out)
            require(report.get("schema") == "pentacle.test-selection-report.v1"
                    and report.get("classification") in {"mapped", "broad_fallback", "docs_only", "no_changes"}
                    and report.get("inputs") == {"base": base, "head": sha, "changed_inputs": sorted(changed)}
                    and report.get("selector_sha256") == selector_hash
                    and report.get("map", {}).get("sha256") == map_hash
                    and report.get("gate_config_sha256") == config_hash, "invalid selector result/identity")
            require(not report.get("missing_target_files"), "missing/deleted seam target")
            require(isinstance(report.get("boundaries"), list) and isinstance(report.get("selected_targets"), list),
                    "invalid selector coverage")
            reports.append(report)
    boundaries = {}
    targets = {}
    for report in reports:
        for boundary in report["boundaries"]:
            require(boundary.get("id") in BOUNDARY_INPUT, "invalid selected boundary")
            boundaries[boundary["id"]] = boundary
        for target in report["selected_targets"]:
            require(target.get("available_in_head") is True, "missing selected target")
            # The mandatory merge gate is independently required and reused once.
            if target["target"] != "v2-merge":
                targets[key(target)] = target
    required_cells = [{"host": activation["host"], "provider": "codex", "prompt_mode": "promptless"}]
    identity = {"candidate": sha, "inputs_sha256": digest(Path(input_path).read_bytes()),
                "selector_sha256": selector_hash, "map_sha256": map_hash, "gate_config_sha256": config_hash,
                "admission_sha256": LOADED_ADMISSION_SHA256,
                "qualification_sha256": LOADED_QUALIFICATION_SHA256,
                "source_tree_sha256": source_hash,
                "gate_env_sha256": digest((repo / "scripts/gate_env.py").read_bytes()),
                "toolchain": tools,
                "target_needs": {t["target"]: read(repo / "scripts/gate-env.json")["profiles"]["pentacle"]["targets"][t["target"]]["needs"]
                                 for t in targets.values()},
                "installed": installed, "external_inputs": external, "activation": activation,
                "required_cells": required_cells}
    plan = {"identity": identity, "reports": reports, "boundaries": [boundaries[k] for k in sorted(boundaries)],
            "targets": [targets[k] for k in sorted(targets)],
            "classification": "broad_fallback" if any(r["classification"] == "broad_fallback" for r in reports)
            else "mapped" if boundaries else "docs_only"}
    if any(t["target"] == "cli-pytest" for t in plan["targets"]):
        require(digest(INSTRUCTION_PATH.read_bytes()) == LOADED_INSTRUCTION_SHA256, "changed CLI instruction validator")
        try:
            snapshot = instruction_snapshot(os.environ.get(ENV_NAME))
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
        bound = external["deploy_config"]["candidate"]["files"]
        require(all(b in bound for b in [snapshot["manifest"], *snapshot["files"]]),
                "unbound CLI instruction input")
        identity["cli_instruction_snapshot"] = snapshot
        identity["cli_instruction_code_sha256"] = LOADED_INSTRUCTION_SHA256
    plan["qualification_contexts"] = {
        name: qualification_context(repo, plan, name, proof, runner)
        for name, proof in QUALIFIED_SEAMS.items() if name in boundaries}
    return plan


def source_scope(repo, revision, paths, runner):
    require(full_sha(revision) and isinstance(paths, list) and paths, "invalid qualification source scope")
    require(all(isinstance(p, str) and p and not p.startswith("/") and ".." not in PurePosixPath(p).parts for p in paths),
            "invalid qualification source path")
    result = runner(("git", "ls-tree", "-r", "-z", "--full-tree", revision), repo)
    require(result.returncode == 0, "qualification source reference unavailable")
    records = []
    for line in result.stdout.split("\0"):
        if not line:
            continue
        path = line.split("\t", 1)[1]
        if any(path.startswith(p) if p.endswith("/") else PurePosixPath(path).match(p) for p in paths):
            records.append(line)
    require(records, "empty qualification source scope")
    return digest("\0".join(records).encode())


def qualification_context(repo, plan, name, proof, runner):
    """One digest predicate per accepted boundary; no deployment filtering."""
    identity = plan["identity"]
    reference = source_scope(repo, proof["source_ref"], proof["source_paths"], runner)
    current = source_scope(repo, identity["candidate"], proof["source_paths"], runner)
    boundary = next(b for b in plan["boundaries"] if b["id"] == name)
    labels = proof["input_labels"]
    require(isinstance(labels, list) and labels and set(labels) <= EXTERNAL, "invalid qualification input scope")
    # File locations remain private; bytes and the exact nonsecret identities
    # determine the finite tested tuple. All files are checked by prepare().
    tuples = {label: {side: {"identity": value["identity"], "file_sha256": sorted(b["sha256"] for b in value["files"])}
                     for side, value in identity["external_inputs"][label].items()} for label in labels}
    return {"source_reference_sha256": reference, "source_sha256": current,
            "tuple_sha256": digest(key(tuples).encode()),
            **({"activation_sha256": digest(key(identity["activation"]).encode())}
               if name == "provider_startup" else {}),
            **({"installed_web_sha256": digest(key(observed_web_consumer(identity)).encode())}
               if name == "client_wire_render" else {}),
            "toolchain_sha256": digest(key(identity["toolchain"]).encode()),
            **{k: identity[k] for k in ("selector_sha256", "map_sha256", "gate_config_sha256", "gate_env_sha256")},
            "missing_coverage_sha256": digest(key(boundary["missing_coverage"]).encode())}


def require_qualification(plan):
    for boundary in plan["boundaries"]:
        name = boundary["id"]
        proof = QUALIFIED_SEAMS.get(name)
        require(isinstance(proof, dict), "unqualified required seam " + name)
        # Registry entries are code-reviewed exact source/input scopes, not a
        # caller assertion that a broad passing suite filled a coverage hole.
        context = plan.get("qualification_contexts", {}).get(name, {})
        require(context and context.get("source_sha256") == context.get("source_reference_sha256")
                and proof.get("scope_sha256") == digest(key(context).encode())
                and proof.get("assertion_scope") and proof.get("remaining_coverage")
                and proof.get("qa_report") and proof.get("evidence_sha256"), "unqualified source/tuple " + name)


def check_result(row):
    require(isinstance(row, dict) and row.get("passed") is True and type(row.get("returncode")) is int
            and row["returncode"] == 0 and row.get("outcome") == "pass"
            and ("timed_out" not in row or row["timed_out"] is False), "non-green seam result")
    counts = row.get("counts")
    require(isinstance(counts, dict) and all(type(counts.get(k)) is int and counts[k] == 0
            for k in ("failures", "errors", "skipped")), "non-green seam counts")


def execution_scope(plan, target):
    return "installed-client-seam" if (target["target"] == "web-gate"
        and plan.get("classification") == "mapped"
        and any(b["id"] == "client_wire_render" for b in plan.get("boundaries", []))) else "default"


def execution_env(plan, target):
    if target["target"] == "cli-pytest":
        return {ENV_NAME: plan["identity"]["cli_instruction_snapshot"]["root"]}
    return {"PENTACLE_WEB_GATE_SCENARIOS": "installed-client-seam"} if execution_scope(plan, target) != "default" else {}


def scenario_verdict(terminal, scope):
    """Bind the scenario actually executed, using the existing browser artifacts."""
    logs = [b for b in terminal.get("evidence", []) if Path(b["path"]).name == "web-browser.log"]
    require(len(logs) == 1, "missing/duplicate browser scenario log")
    log = Path(file_binding({k: logs[0][k] for k in ("path", "sha256")})["path"])
    lines = log.read_text().splitlines()
    require([line[2:] for line in lines if line.startswith("▸ ")] == [scope]
            and [line for line in lines if line.startswith("◀ web_gate:")] == ["◀ web_gate: PASS"],
            "browser scenario mismatch/non-green")
    artifacts = [line.removeprefix("  artifacts: ") for line in lines if line.startswith("  artifacts: ")]
    require(len(artifacts) == 1, "missing/duplicate browser scenario verdict")
    directory = Path(artifacts[0])
    require(directory.is_absolute() and directory.resolve().is_relative_to(log.parent.resolve()),
            "browser scenario verdict outside execution evidence")
    path = directory / "verdict.json"
    verdict = read(path)
    require(verdict.get("scenario") == "web_gate" and verdict.get("status") == "PASS"
            and type(verdict.get("failedScenarios")) is int and verdict["failedScenarios"] == 0
            and isinstance(verdict.get("steps"), list) and verdict["steps"]
            and all(isinstance(step, dict) and step.get("ok") is True for step in verdict["steps"]),
            "non-green browser scenario verdict")
    auth, daemon = verdict.get("fixture_auth_cleanup", {}), verdict.get("daemon_cleanup", {})
    require(type(auth.get("remaining_credential_count")) is int and auth["remaining_credential_count"] == 0
            and auth.get("registry_removed") is True and auth.get("token_removed") is True
            and daemon.get("exited") is True and daemon.get("forced") is False,
            "non-green browser scenario cleanup")
    return {"path": str(path), "sha256": digest(path.read_bytes())}


def validate_results(plan, section):
    require(isinstance(section, dict) and set(section) == {"identity", "results"}
            and section.get("identity") == plan["identity"], "stale seam identity")
    rows = section.get("results")
    require(isinstance(rows, list), "missing seam results")
    expected = {key(t) for t in plan["targets"]}
    seen = set()
    require(len({key(r.get("target")) for r in rows if isinstance(r, dict)}) == len(rows), "duplicate seam result")
    for row in rows:
        require(isinstance(row, dict), "invalid seam result")
        target = key(row.get("target"))
        require(target not in seen, "duplicate seam result")
        seen.add(target)
        require(target in expected, "unexpected seam target")
        check_result(row)
        scope = execution_scope(plan, row["target"])
        require(row.get("execution_scope") == scope, "seam execution scope mismatch")
        evidence = row.get("evidence")
        require(isinstance(evidence, dict) and set(evidence) == {"started", "prepared", "terminal"}, "missing seam execution evidence")
        started, prepared, terminal = (read(file_binding(evidence[k])["path"]) for k in ("started", "prepared", "terminal"))
        require(all(e.get("schema") == "pentacle.test-attempt-event.v1" for e in (started, prepared, terminal))
                and started.get("state") == "started" and prepared.get("state") == "prepared" and terminal.get("state") == "terminal"
                and started.get("attempt_id") == terminal.get("attempt_id")
                and prepared.get("attempt_id") == terminal.get("attempt_id")
                and started.get("target") == row["target"]["target"]
                and started.get("selected", {}).get("select") == row["target"]["select"]
                and started.get("selected", {}).get("pass_env_names", []) ==
                    list(execution_env(plan, row["target"]))
                and started.get("identities", {}).get("source") == {"sha": plan["identity"]["candidate"], "dirty": False},
                "seam execution source/selection mismatch")
        if row["target"]["target"] == "cli-pytest":
            snapshot = plan["identity"]["cli_instruction_snapshot"]
            try:
                require(instruction_snapshot(snapshot["root"]) == snapshot, "changed CLI instruction input")
            except ValueError as exc:
                raise Invalid(str(exc)) from exc
            expected_env = {ENV_NAME: snapshot}
            require(prepared.get("passed_environment") == expected_env
                    and terminal.get("secondary", {}).get("passed_environment") == expected_env,
                    "CLI instruction execution binding mismatch")
        require(terminal.get("outcome") == "pass" and terminal.get("first_failure") is None
                and terminal.get("secondary", {}).get("cleanup") != "failed"
                and type(terminal.get("detail", {}).get("returncode")) is int
                and terminal["detail"]["returncode"] == 0, "non-green seam execution")
        require(isinstance(prepared.get("toolchain"), dict)
                and set(prepared["toolchain"]) == set(plan["identity"]["target_needs"][row["target"]["target"]]),
                "missing execution toolchain")
        for name, tool in prepared["toolchain"].items():
            require(tool == plan["identity"]["toolchain"].get(name), "changed execution toolchain")
            file_binding({"path": tool["realpath"], "sha256": tool["sha256"]})
        counts = terminal.get("secondary", {}).get("counts")
        if counts is not None:
            check_result({**row, "counts": counts})
        require(terminal.get("evidence"), "missing subject evidence")
        for binding in terminal["evidence"]:
            file_binding({k: binding[k] for k in ("path", "sha256")})
        if scope != "default":
            require(row.get("scenario_verdict") == scenario_verdict(terminal, scope),
                    "changed/missing browser scenario verdict binding")
    require(seen == expected, "missing required seam result")
    require_qualification(plan)
    return section


def admit(repo, sha, input_path, gate_evidence, python, runner):
    plan = prepare(repo, sha, input_path, python, runner)
    validate_results(plan, gate_evidence.get("seam_results"))
    return plan


def revalidate(plan, input_path):
    identity = plan["identity"]
    require(digest(Path(input_path).read_bytes()) == identity["inputs_sha256"], "changed release inputs before activation")
    require(digest(Path(__file__).read_bytes()) == identity["admission_sha256"], "changed admission code before activation")
    require(digest(QUALIFICATION_PATH.read_bytes()) == identity["qualification_sha256"], "changed qualification before activation")
    if "cli_instruction_snapshot" in identity:
        require(digest(INSTRUCTION_PATH.read_bytes()) == identity["cli_instruction_code_sha256"],
                "changed CLI instruction validator before activation")
        try:
            require(instruction_snapshot(identity["cli_instruction_snapshot"]["root"]) == identity["cli_instruction_snapshot"],
                    "changed CLI instruction input before activation")
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
    for row in identity["installed"]:
        installed_receipt(row, identity["candidate"])
    for pair in identity["external_inputs"].values():
        for value in pair.values():
            for binding in value["files"]:
                file_binding(binding)
    for tool in identity["toolchain"].values():
        file_binding({"path": tool["realpath"], "sha256": tool["sha256"]})


def run_selected(plan, repo, python, runner, workspace, evidence_dir):
    """Use gate_env's existing named targets; never execute map command strings."""
    require_qualification(plan)
    rows = []
    for target in plan["targets"]:
        require(target["target"] in {"v2-pytest", "cli-pytest", "shared-pytest", "npm-test", "web-gate"},
                "unsupported seam target")
        out = evidence_dir / uuid.uuid4().hex
        command = [python, str(repo / "scripts/gate_env.py"), "run", "--profile", "pentacle",
                   "--workspace", str(workspace), "--evidence-dir", str(out), "--target", target["target"],
                   "--spec", "spec_pentacle__selective_release_gate_2026_10", "--series", "release-seams",
                   "--purpose", "acceptance"]
        for item in target["select"]:
            command.extend(("--select", item))
        scope = execution_scope(plan, target)
        passed = execution_env(plan, target)
        for name in passed:
            command.extend(("--pass-env", name))
        if passed:
            command = ["/usr/bin/env", *[name + "=" + value for name, value in passed.items()], *command]
        result = runner(command, repo)
        require(result.returncode == 0, "non-green selected target " + target["target"])
        bound = {}
        for state in ("started", "prepared", "terminal"):
            paths = list(out.glob("attempts/*/*-" + state + ".json"))
            require(len(paths) == 1, "missing/duplicate execution " + state)
            bound[state] = {"path": str(paths[0]), "sha256": digest(paths[0].read_bytes())}
        terminal = read(bound["terminal"]["path"])
        rows.append({"target": target, "passed": True, "returncode": result.returncode,
                     "execution_scope": scope,
                     "outcome": terminal.get("outcome"), "evidence": bound,
                     "counts": terminal.get("secondary", {}).get("counts", {"failures": 0, "errors": 0, "skipped": 0})})
        if scope != "default":
            rows[-1]["scenario_verdict"] = scenario_verdict(terminal, scope)
    section = {"identity": plan["identity"], "results": rows}
    validate_results(plan, section)
    return section


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare conservative release selection; qualification remains separate.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--release-inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gate-evidence", type=Path, help="Existing passing merge receipt to supplement (never reruns merge).")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = prepare(args.repo.resolve(), args.candidate, args.release_inputs, sys.executable,
                       lambda cmd, cwd: subprocess.run(cmd, cwd=cwd, capture_output=True, text=True))
        args.out.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        require_qualification(plan)
        if args.gate_evidence:
            require(args.workspace is not None and args.evidence_dir is not None, "execution requires workspace/evidence-dir")
            # Import the existing validator rather than inventing another merge contract.
            service_dir = str(Path(__file__).resolve().parent.parent)
            if service_dir not in sys.path:
                sys.path.insert(0, service_dir)
            from deploy.deploy import _load_gate_evidence
            receipt = _load_gate_evidence(args.gate_evidence, args.candidate)
            receipt["seam_results"] = run_selected(plan, args.repo.resolve(), sys.executable,
                lambda cmd, cwd: subprocess.run(cmd, cwd=cwd, capture_output=True, text=True),
                args.workspace.resolve(), args.evidence_dir.resolve())
            args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except (Invalid, OSError, TypeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
