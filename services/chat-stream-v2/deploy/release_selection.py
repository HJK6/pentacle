"""Input-bound admission for the named integration seams, alongside merge evidence.

This is deliberately not a qualification authority. Accepted boundary evidence
must be reviewed and registered here before any real tuple can use selection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid


PUBLIC_REPO = "https://github.com/HJK6/pentacle.git"
EXTERNAL = {"client_versions", "provider_config", "satellite_config", "deploy_config"}
ROLES = {"daemon", "web", "mobile", "satellite"}
BOUNDARY_INPUT = {"client_wire_render": ("client_versions", "satellite_config"),
                  "provider_startup": ("provider_config",),
                  "satellite_transition": ("satellite_config",),
                  "deploy_invocation": ("deploy_config",)}
# No client/provider/satellite live tuple has final accepted proof yet. A larger
# suite cannot resolve these known gaps. Never populate this from a release file.
QUALIFICATION_PATH = Path(__file__).with_name("qualified_seams.json")
QUALIFIED_SEAMS: dict[str, dict] = json.loads(QUALIFICATION_PATH.read_bytes())
LOADED_QUALIFICATION_SHA256 = hashlib.sha256(QUALIFICATION_PATH.read_bytes()).hexdigest()
LOADED_ADMISSION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
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
    else:
        require(kind == "observed_component" and isinstance(receipt, dict)
                and all(receipt.get(name) == row[name] for name in ("component", "repository", "sha"))
                and receipt.get("observed") is True, "installed baseline receipt mismatch")


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
    # Mobile belongs to a different repository; never pass its SHA to Pentacle Git.
    mobile = sorted(row["sha"] for row in installed if row["role"] == "mobile")
    require(external["client_versions"]["installed"]["identity"].get("mobile_source_shas") == mobile,
            "consumer source mismatch")
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
    return {"identity": identity, "reports": reports, "boundaries": [boundaries[k] for k in sorted(boundaries)],
            "targets": [targets[k] for k in sorted(targets)],
            "classification": "broad_fallback" if any(r["classification"] == "broad_fallback" for r in reports)
            else "mapped" if boundaries else "docs_only"}


def require_qualification(plan):
    for boundary in plan["boundaries"]:
        name = boundary["id"]
        proof = QUALIFIED_SEAMS.get(name)
        require(isinstance(proof, dict), "unqualified required seam " + name)
        # Registry entries are code-reviewed exact source/input scopes, not a
        # caller assertion that a broad passing suite filled a coverage hole.
        scope = {k: v for k, v in plan["identity"].items()
                 if k not in {"candidate", "inputs_sha256", "qualification_sha256"}}
        require(proof.get("scope") == scope and proof.get("resolved_missing_coverage") == boundary["missing_coverage"]
                and proof.get("qa_report") and proof.get("evidence_sha256"), "unqualified source/tuple " + name)


def check_result(row):
    require(isinstance(row, dict) and row.get("passed") is True and type(row.get("returncode")) is int
            and row["returncode"] == 0 and row.get("outcome") == "pass"
            and ("timed_out" not in row or row["timed_out"] is False), "non-green seam result")
    counts = row.get("counts")
    require(isinstance(counts, dict) and all(type(counts.get(k)) is int and counts[k] == 0
            for k in ("failures", "errors", "skipped")), "non-green seam counts")


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
        evidence = row.get("evidence")
        require(isinstance(evidence, dict) and set(evidence) == {"started", "prepared", "terminal"}, "missing seam execution evidence")
        started, prepared, terminal = (read(file_binding(evidence[k])["path"]) for k in ("started", "prepared", "terminal"))
        require(all(e.get("schema") == "pentacle.test-attempt-event.v1" for e in (started, prepared, terminal))
                and started.get("state") == "started" and prepared.get("state") == "prepared" and terminal.get("state") == "terminal"
                and started.get("attempt_id") == terminal.get("attempt_id")
                and prepared.get("attempt_id") == terminal.get("attempt_id")
                and started.get("target") == row["target"]["target"]
                and started.get("selected", {}).get("select") == row["target"]["select"]
                and started.get("identities", {}).get("source") == {"sha": plan["identity"]["candidate"], "dirty": False},
                "seam execution source/selection mismatch")
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
        result = runner(command, repo)
        require(result.returncode == 0, "non-green selected target " + target["target"])
        bound = {}
        for state in ("started", "prepared", "terminal"):
            paths = list(out.glob("attempts/*/*-" + state + ".json"))
            require(len(paths) == 1, "missing/duplicate execution " + state)
            bound[state] = {"path": str(paths[0]), "sha256": digest(paths[0].read_bytes())}
        terminal = read(bound["terminal"]["path"])
        rows.append({"target": target, "passed": True, "returncode": result.returncode,
                     "outcome": terminal.get("outcome"), "evidence": bound,
                     "counts": terminal.get("secondary", {}).get("counts", {"failures": 0, "errors": 0, "skipped": 0})})
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
