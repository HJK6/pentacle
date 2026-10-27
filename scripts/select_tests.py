#!/usr/bin/env python3
"""Report advisory test selections; never execute tests or change gate verdicts."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


BOUNDARIES = {"client_wire_render", "provider_startup", "satellite_transition", "deploy_invocation"}
DOCS_ALLOWLIST = {"README.md", "CHANGELOG.md", "LICENSE", "docs/*.md", "process/docs/*.md"}
HERE = Path(__file__).resolve().parent


class Invalid(ValueError):
    pass


def git(*args):
    result = subprocess.run(["git", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise Invalid("git %s: %s" % (args[0], result.stderr.decode(errors="replace").strip()))
    return result.stdout


def digest(data):
    return hashlib.sha256(data).hexdigest()


def object_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Invalid("duplicate map/config key: " + key)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def require(ok, message):
    if not ok:
        raise Invalid(message)


def strings(value, label, nonempty=True):
    require(isinstance(value, list) and (bool(value) or not nonempty)
            and all(isinstance(v, str) and v for v in value), "invalid " + label)


def path_pattern(value):
    require(isinstance(value, str) and value and not value.startswith("/")
            and ".." not in PurePosixPath(value).parts and "\x00" not in value,
            "invalid relative path/pattern")


def tree(revision):
    result = {}
    for entry in git("ls-tree", "-r", "-z", "--full-tree", revision).split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            result[path.decode("utf-8", "surrogateescape")] = meta.split()[0].decode()
    return result


def changes(base, head):
    # Consume NUL fields directly. Rename/copy records have TWO path fields.
    fields = git("diff", "--name-status", "-z", "--find-renames", "--no-ext-diff",
                 "--no-textconv", "--ignore-submodules=none", base, head, "--").split(b"\0")
    require(fields.pop() == b"", "unterminated Git diff")
    result = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii")
        count = 2 if status.startswith(("R", "C")) else 1
        require(re.fullmatch(r"(?:[AMDTU]|[RC][0-9]+)", status) is not None,
                "unsupported Git status: " + status)
        paths = fields[index + 1:index + 1 + count]
        require(len(paths) == count and all(paths), "incomplete Git diff record")
        result.append({"status": status, "paths": [p.decode("utf-8", "surrogateescape") for p in paths]})
        index += count + 1
    return result


def validate_map(mapping, targets):
    require(isinstance(mapping, dict) and set(mapping) == {
        "schema", "docs_only", "broad_paths", "external_inputs", "full_gates", "boundaries"
    }, "invalid map fields")
    require(mapping["schema"] == "pentacle.test-selection.v1", "unsupported map schema")
    require(isinstance(mapping["boundaries"], dict) and set(mapping["boundaries"]) == BOUNDARIES,
            "map must define exactly the four boundaries")
    for label in ("docs_only", "broad_paths"):
        strings(mapping[label], label)
        for pattern in mapping[label]:
            path_pattern(pattern)
    require(set(mapping["docs_only"]) <= DOCS_ALLOWLIST, "docs_only exceeds the explicit documentation allowlist")
    require(isinstance(mapping["external_inputs"], dict), "invalid external_inputs")
    require(set(mapping["external_inputs"]) == {
        "client_versions", "provider_config", "satellite_config", "deploy_config"
    }, "invalid external input labels")
    for values in mapping["external_inputs"].values():
        strings(values, "external boundary list")
        require(set(values) <= BOUNDARIES, "unknown external boundary")

    def requests(items):
        require(isinstance(items, list) and bool(items), "empty/invalid test target list")
        for item in items:
            require(isinstance(item, dict) and set(item) == {"target", "select", "files"},
                    "invalid target entry")
            require(isinstance(item["target"], str) and item["target"] in targets,
                    "unknown gate target")
            strings(item["select"], "target selections", nonempty=False)
            strings(item["files"], "target evidence files")
            target = targets[item["target"]]
            for path in item["files"]:
                path_pattern(path)
                require(not any(c in path for c in "*?["), "target file must be literal")
            for selection in item["select"]:
                rule = target.get("select", {})
                require(bool(rule.get("pattern")) and re.fullmatch(rule["pattern"], selection) is not None,
                        "selection incompatible with gate target")
                file = str(PurePosixPath(rule.get("root", "")) / selection.split("::")[0])
                require(file in item["files"], "selection must name a declared evidence file")
            require(target.get("kind") != "pytest_files" or bool(item["select"]),
                    "pytest target needs explicit selection")

    requests(mapping["full_gates"])
    require({item["target"] for item in mapping["full_gates"]} == {
        "v2-merge", "cli-pytest", "shared-pytest", "npm-test", "web-gate"
    }, "full_gates must retain all existing gate families")
    for boundary in mapping["boundaries"].values():
        require(isinstance(boundary, dict) and set(boundary) == {
            "paths", "targets", "covered_behavior", "missing_coverage"
        }, "invalid boundary fields")
        strings(boundary["paths"], "boundary paths")
        for pattern in boundary["paths"]:
            path_pattern(pattern)
        strings(boundary["covered_behavior"], "covered_behavior")
        strings(boundary["missing_coverage"], "missing_coverage")
        requests(boundary["targets"])


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def select(args, report):
    for name in ("base", "head"):
        revision = getattr(args, name)
        require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) is not None,
                name + " must be an immutable full commit SHA")
        require(git("cat-file", "-t", revision).strip() == b"commit", name + " is not a commit")
    map_bytes = (HERE / "test-selection.json").read_bytes()
    report["map"] = {"path": "scripts/test-selection.json", "sha256": digest(map_bytes)}
    config_bytes = git("show", args.head + ":scripts/gate-env.json")
    report["gate_config_sha256"] = digest(config_bytes)
    config = object_json(config_bytes)
    targets = config["profiles"]["pentacle"]["targets"]
    require(isinstance(targets, dict) and all(isinstance(t, dict) for t in targets.values()),
            "invalid gate target configuration")
    mapping = object_json(map_bytes)
    validate_map(mapping, targets)
    base_tree, head_tree = tree(args.base), tree(args.head)
    changed = changes(args.base, args.head)
    report["changes"] = changed
    paths = sorted({path for change in changed for path in change["paths"]})
    report["changed_paths"] = paths
    selected = set()
    reasons = {name: [] for name in sorted(BOUNDARIES)}
    fallback = []
    docs = []
    for path in paths:
        if base_tree.get(path) == "160000" or head_tree.get(path) == "160000":
            fallback.append("unknown gitlink contents: " + path)
        if matches(path, mapping["broad_paths"]):
            fallback.append("shared dependency/tooling: " + path)
            continue
        owners = [name for name, entry in mapping["boundaries"].items() if matches(path, entry["paths"])]
        if owners:
            for name in owners:
                selected.add(name)
                reasons[name].append("path: " + path)
        elif matches(path, mapping["docs_only"]):
            docs.append(path)
        else:
            fallback.append("unmapped path: " + path)
    for label in sorted(set(args.changed_input)):
        owners = mapping["external_inputs"].get(label)
        if owners is None:
            fallback.append("unknown external input: " + label)
        else:
            for name in owners:
                selected.add(name)
                reasons[name].append("external input: " + label)
    # Validate ALL declared references in HEAD, even for an otherwise docs-only diff.
    requests = mapping["full_gates"] + [item for entry in mapping["boundaries"].values() for item in entry["targets"]]
    def available(item, path):
        target = targets[item["target"]]
        root = target.get("select", {}).get("root", "")
        directories = {str(PurePosixPath(root) / p) for p in target.get("directory_select", [])}
        if path in directories:
            return any(p.startswith(path + "/") for p in head_tree)
        return head_tree.get(path) in {"100644", "100755"}

    missing = sorted({path for item in requests for path in item["files"] if not available(item, path)})
    report["missing_target_files"] = missing
    fallback.extend("missing target file in HEAD: " + path for path in missing)
    if fallback:
        selected = set(BOUNDARIES)
        for name in selected:
            reasons[name].append("conservative broad fallback")
    report["classification"] = ("broad_fallback" if fallback else "mapped" if selected
                                else "docs_only" if paths else "no_changes")
    report["fallback_reasons"] = sorted(set(fallback))
    report["docs_only_paths"] = docs
    report["boundaries"] = [{"id": name, "reasons": sorted(set(reasons[name])),
                             "targets": mapping["boundaries"][name]["targets"],
                             "covered_behavior": mapping["boundaries"][name]["covered_behavior"],
                             "missing_coverage": mapping["boundaries"][name]["missing_coverage"]}
                            for name in sorted(selected)]
    chosen = [item for name in sorted(selected) for item in mapping["boundaries"][name]["targets"]]
    if fallback:
        chosen += mapping["full_gates"]
    report["selected_targets"] = []
    for key in sorted({json.dumps(item, sort_keys=True) for item in chosen}):
        item = json.loads(key)
        item["available_in_head"] = not any(path in missing for path in item["files"])
        report["selected_targets"].append(item)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--changed-input", action="append", default=[])
    args = parser.parse_args()
    report = {"schema": "pentacle.test-selection-report.v1", "advisory": True,
              "selective_safe": False, "tests_executed": False, "classification": "invalid",
              "inputs": {"base": args.base, "head": args.head, "changed_inputs": sorted(set(args.changed_input))},
              "selector_sha256": digest(Path(__file__).read_bytes()),
              "limitations": ["Selection does not certify assertions or discover every coverage gap.",
                              "Existing CI, hooks, merge and deploy gates remain mandatory and unchanged.",
                              "Run once per installed component baseline and union results; no baseline is inferred."],
              "selected_targets": []}
    code = 0
    try:
        select(args, report)
    except (Invalid, OSError, ValueError, KeyError, TypeError) as error:
        report.update(classification="invalid", error=str(error), selected_targets=[])
        print("test selection invalid: " + str(error), file=sys.stderr)
        code = 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
