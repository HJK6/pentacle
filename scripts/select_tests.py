#!/usr/bin/env python3
"""Report advisory test selections and compare blocking merge coverage."""
from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time


BOUNDARIES = {"client_wire_render", "provider_startup", "satellite_transition", "deploy_invocation"}
DOCS_ALLOWLIST = {"README.md", "CHANGELOG.md", "LICENSE", "docs/*.md", "process/docs/*.md"}
HERE = Path(__file__).resolve().parent


class Invalid(ValueError):
    pass


def git(*args, repo=None, timeout=None):
    result = subprocess.run(["git", *args], cwd=repo, timeout=timeout,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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


def changes(base, head, *, repo=None, timeout=None):
    # Consume NUL fields directly. Rename/copy records have TWO path fields.
    fields = git("diff", "--name-status", "-z", "--find-renames", "--no-ext-diff",
                 "--no-textconv", "--ignore-submodules=none", base, head, "--",
                 repo=repo, timeout=timeout).split(b"\0")
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


def _collect_coverage(repo, revision, root, python, remaining, *, historical, errors, suites):
    from gate_env import ensure_npm_env, group_alive, stop_group

    def command_budget(available):
        # Reserve two cleanup phases and keep short compare deadlines usable.
        return available - min(40.0, available / 2)

    def run(argv, *, timeout, cwd=root, env=None, capture_output=True, text=True):
        available = remaining()
        deadline = time.monotonic() + available
        bound = min(timeout, command_budget(available))
        require(bound > 0, "coverage comparison timed out")
        with subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True, text=text,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            try:
                out, err = proc.communicate(timeout=bound)
            except subprocess.TimeoutExpired as exc:
                raise Invalid(f"{argv[0]} timed out after {bound:.1f}s") from exc
            finally:
                if group_alive(proc.pid):
                    stop_group(proc, grace=min(20.0, max(0.0, deadline - time.monotonic()) / 2))
                    if group_alive(proc.pid):
                        raise Invalid(f"{argv[0]} collection process group did not stop")
            return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    def checked(result):
        if result.returncode:
            detail = (result.stderr or result.stdout).strip().splitlines()[-4:]
            raise Invalid(" | ".join(detail) or f"exit {result.returncode}")
        return result.stdout

    scratch = root.parent
    home, temp = scratch / f"{root.name}-home", scratch / f"{root.name}-tmp"
    home.mkdir()
    temp.mkdir()
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "npm_config_offline",
                                           "V2_GATE_OWNER_MANIFEST") if key in os.environ}
    env.update(HOME=str(home), TMPDIR=str(temp), TMP=str(temp), TEMP=str(temp),
               PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               NPM_CONFIG_CACHE=str(scratch / "npm-cache"), NPM_CONFIG_AUDIT="false", NPM_CONFIG_FUND="false",
               PENTACLE_PYTHON=python, V2_PYTHON_BIN=python, PENTACLE_WEB_GATE_REPORT_ROOT=str(temp / "web"))
    failures = (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError)
    inventory = {}
    try:
        checked(run(["git", "clone", "-q", "--local", "--shared", "--no-checkout",
                     "--", str(repo), str(root)], cwd=scratch, env=env, timeout=30))
        checked(run(["git", "checkout", "-q", "--detach", revision], env=env, timeout=30))
        profile = object_json((root / "scripts/gate-env.json").read_bytes())["profiles"]["pentacle"]
        targets = profile["targets"]
        for path in [profile["service_dir"], *profile["pythonpath"]]:
            path_pattern(path)
        env["PYTHONPATH"] = os.pathsep.join(str(root / path) for path in
                                          ["scripts", profile["service_dir"], *profile["pythonpath"]])
    except failures as exc:
        errors.append(f"{revision} checkout: {exc}")
        return inventory

    for suite in ("daemon", "cli", "shared"):
        if suite not in suites:
            continue
        try:
            if suite == "daemon":
                # One collection spans unit, smoke, soak and separately executed repin tests.
                path, selection = profile["service_dir"], ["tests"]
            else:
                target = targets[suite + "-pytest"]
                path, selection = target["select"]["root"], target["directory_select"]
            path_pattern(path)
            strings(selection, "full pytest selection")
            for directory in selection:
                path_pattern(directory)
            manifest = scratch / f"{root.name}-{suite}.json"
            result = run([python, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=",
                          f"--basetemp={temp / suite}",
                          "-p", "quarantine_plugin", f"--quarantine-root={root}",
                          f"--quarantine-suite={suite}", f"--quarantine-manifest={manifest}", "--", *selection],
                         cwd=root / path, env=env, timeout=120)
            if not manifest.is_file():
                checked(result)
                raise Invalid("missing pytest collection")
            data = object_json(manifest.read_bytes())
            require(isinstance(data, dict), "invalid pytest collection")
            inventory_error = result.returncode == 1 and data.get("complete") is False
            expired_base = historical and inventory_error and data.get("error_kind") == "expired"
            if not expired_base:
                if inventory_error:
                    raise Invalid(data.get("error") or "incomplete pytest collection")
                checked(result)
                require(data.get("complete") is True, data.get("error") or "incomplete pytest collection")
            for key in ("blocking", "quarantined"):
                strings(data[key], "pytest collection", nonempty=False)
            require(data["blocking"] or data["quarantined"], "empty pytest collection")
            ids = (*data["blocking"], *data["quarantined"])
            inventory[suite] = (
                Counter(f"{suite}::{node}" for node in data["blocking"]),
                Counter(f"{suite}::{node}" for node in data["quarantined"]),
                {f"{suite}::{node}": str(PurePosixPath(path) / node.split("::", 1)[0]) for node in ids}, {},
            )
        except failures as exc:
            errors.append(f"{revision} {suite}: {exc}")

    if "npm" in suites:
        try:
            npm = targets["npm-test"]
            patterns = npm["default_patterns"]
            strings(patterns, "npm selection")
            for pattern in patterns:
                path_pattern(pattern)
            _, setup_errors = ensure_npm_env(run, root, scratch / f"{root.name}-npm", env, profile, False,
                                             lambda message: print(f"[coverage] {revision} {message}", flush=True))
            require(not setup_errors, " | ".join(item["detail"] for item in setup_errors))
            for build in npm.get("npm_build", []):
                checked(run(["npm", "run", build], env=env, timeout=300))
            script = """
    const {collectNodeTests} = require('./scripts/lib/test-runner');
    const controller = new AbortController();
    process.once('SIGTERM', () => controller.abort());
    const options = JSON.parse(process.argv[2]);
    collectNodeTests(process.cwd(), JSON.parse(process.argv[1]), {...options, abortSignal: controller.signal})
      .then(rows => process.stdout.write(JSON.stringify(rows)))
      .catch(error => { console.error(error.message); process.exitCode = 1; });
    """
            collection_bound = min(npm["timeout_s"], command_budget(remaining()) - 5)
            require(collection_bound >= 0.001, "coverage comparison timed out before npm collection")
            options = {"runTimeoutMs": int(collection_bound * 1000),
                       "fileTimeoutMs": npm.get("test_file_timeout_ms", 60000)}
            rows = json.loads(checked(run(["node", "-e", script, json.dumps(patterns), json.dumps(options)],
                                          env=env, timeout=remaining())))
            files, invocations = {}, Counter()
            for row in rows:
                file = row["file"]
                path_pattern(file)
                require(all(type(case["skipped"]) is bool and case["id"].startswith(file + "::")
                            for case in row["cases"]), f"invalid npm collection for {file}")
                all_cases = Counter((case["id"], case["skipped"]) for case in row["cases"])
                require(files.setdefault(file, all_cases) == all_cases,
                        f"npm collection varied across invocations of {file}")
                invocations[file] += 1
            blocking, skipped, paths = Counter(), Counter(), {}
            for file, cases in files.items():
                for (test, nonblocking), count in cases.items():
                    (skipped if nonblocking else blocking)[test] += count
                    paths[test] = file
            require(blocking or skipped, "empty npm collection")
            inventory["npm"] = blocking, skipped, paths, invocations
        except failures as exc:
            errors.append(f"{revision} npm: {exc}")
    try:
        require(not checked(run(["git", "status", "--porcelain", "--untracked-files=all"],
                                env=env, timeout=30)), "collection changed the tracked source")
        require(checked(run(["git", "rev-parse", "HEAD"], env=env, timeout=30)).strip() == revision,
                "collection changed the pinned revision")
    except failures as exc:
        errors.append(f"{revision} source integrity: {exc}")
        return {}
    return inventory


def check_coverage(repo: Path, base: str, head: str, *, python: str, timeout: float) -> None:
    """Compare full blocking membership at pinned merge refs."""
    repo = repo.resolve()
    deadline = time.monotonic() + timeout

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise Invalid("coverage comparison timed out")
        return budget

    try:
        base, head = (git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}",
                          repo=repo, timeout=remaining()).decode().strip() for ref in (base, head))
        from quarantine_list import QUARANTINE_PATH, validate_quarantine
        candidate_list = object_json(git("show", f"{head}:{QUARANTINE_PATH}", repo=repo, timeout=remaining()))
        markers = git("ls-tree", "--name-only", base, "--", QUARANTINE_PATH,
                      "scripts/quarantine_list.py", "scripts/admission_inputs.py",
                      repo=repo, timeout=remaining()).decode().splitlines()
        base_list = QUARANTINE_PATH if QUARANTINE_PATH in markers else "scripts/quarantine.json"
        version = object_json(git("show", f"{base}:{base_list}", repo=repo, timeout=remaining()))["version"]
        if markers == ["scripts/admission_inputs.py"] and type(version) is int and version == 1:
            # No collectors run in this branch, so validate the proposed list here.
            validate_quarantine(candidate_list)
            print("Coverage comparison unavailable: merge base predates gate cutover", flush=True)
            return
        # Pre-split simplified bases used v2 at the old path. Their own collector
        # reads it. A retained v1 snapshot never substitutes for a missing v2 list.
        require(type(version) is int and version == 2, "merge base lacks an active v2 quarantine list")
        scopes = {path: " ".join([change["status"], " -> ".join(change["paths"])])
                  for change in changes(base, head, repo=repo, timeout=remaining())
                  for path in change["paths"]}
        suites = set()
        # Suite roots correspond to the targets in scripts/gate-env.json.
        roots = {"daemon": "services/chat-stream-v2/", "cli": "services/agent-orch/",
                 "shared": "services/_shared/"}
        common = {"scripts/select_tests.py", "scripts/gate_env.py", "scripts/gate-env.json",
                  "scripts/quarantine_plugin.py", "scripts/quarantine_list.py",
                  "scripts/quarantine-v2.json", "scripts/quarantine.json"}
        for path in scopes:
            if path in common:
                suites.update((*roots, "npm"))
            if path in {"pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py", "services/conftest.py",
                        "services/chat-stream-v2/native_input_lookup.py",
                        "services/chat-stream-v2/retention.py"}:
                suites.update(roots)
            for suite, root in roots.items():
                if path.startswith(root + "tests/") or path in {root + name for name in
                        ("pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py", "requirements.txt")}:
                    suites.add(suite)
            if path.startswith(("test/", "scripts/lib/")) or path in {"package.json", "package-lock.json"}:
                suites.add("npm")
        if not scopes:  # Full nightly on main retains its existing coverage collection.
            suites.update((*roots, "npm"))
        for suite in (*roots, "npm"):
            if suite not in suites:
                print(f"coverage: {suite} unchanged, not collected", flush=True)
        trailers = git("log", "-1", "--format=%(trailers:key=Coverage-Drop,valueonly,separator=%x00,unfold=true)",
                       head, "--", repo=repo, timeout=remaining()).decode().removesuffix("\n")
        declarations = Counter(trailers.split("\0")) if trailers else Counter()
        errors = []
        if not suites:
            return
        with tempfile.TemporaryDirectory(prefix="pc-cover-") as tmp:
            scratch = Path(tmp)
            before = _collect_coverage(repo, base, scratch / "base", python, remaining,
                                       historical=base != head, errors=errors, suites=suites)
            after = before if base == head else _collect_coverage(
                repo, head, scratch / "head", python, remaining, historical=False, errors=errors, suites=suites)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Coverage comparison inconclusive: {exc}") from exc

    undeclared = 0
    for suite in sorted(before.keys() & after.keys()):
        prior, prior_advisory, paths, prior_invocations = before[suite]
        current, advisory, _, invocations = after[suite]
        for test, loss in sorted((prior - current).items()):
            suppressed = min(loss, max(0, advisory[test] - prior_advisory[test]))
            reason = "skipped/todo" if suite == "npm" else "quarantined"
            causes = ([f"{suppressed} {reason}"] if suppressed else [])
            if loss > suppressed:
                causes.append(f"{loss - suppressed} missing")
            declared = min(loss, declarations[test])
            undeclared += loss - declared
            declarations[test] -= declared
            path = paths[test]
            context = [scopes[path]] if path in scopes else []
            if suite == "npm" and max(prior_invocations[path], invocations[path]) > 1:
                context.append(f"{prior_invocations[path]} -> {invocations[path]} invocations")
            print(f"Coverage loss {test}: {prior[test]} -> {current[test]}, lost {loss}"
                  f" ({', '.join(causes)}), declared {declared}/{loss}"
                  f"{' [' + '; '.join(context) + ']' if context else ''}", flush=True)
    if errors:
        for error in errors:
            print(f"Coverage collection unavailable: {error}", flush=True)
        raise RuntimeError(f"Coverage comparison inconclusive: {len(errors)} unavailable collections"
                           f", {undeclared} undeclared lost occurrences")
    if undeclared:
        raise RuntimeError(f"Coverage loss undeclared: add a Coverage-Drop trailer for each of "
                           f"the {undeclared} undeclared lost occurrences listed above")


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
