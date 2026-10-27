"""Observable CLI controls using disposable repositories and real Git objects."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


HERE = Path(__file__).resolve().parent
ALL = {"client_wire_render", "provider_startup", "satellite_transition", "deploy_invocation"}
V2 = "services/chat-stream-v2/"


class Repo:
    def __init__(self, root, name):
        self.root, self.name, self.calls = root, name.replace("/", "_"), 0
        self.git("init", "-q")
        self.git("config", "user.name", "Test Agent")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        for name in ("select_tests.py", "test-selection.json", "gate-env.json"):
            self.write("scripts/" + name, (HERE / name).read_text())
        mapping = json.loads((HERE / "test-selection.json").read_text())
        for item in mapping["full_gates"] + [t for b in mapping["boundaries"].values() for t in b["targets"]]:
            for path in item["files"]:
                self.write(path + "/test_placeholder.py" if path.endswith("/tests") else path)
        for path in (V2 + "provider_account_native.py", V2 + "satellite.py", V2 + "event_push.py",
                     V2 + "deploy/deploy.py", "renderer/view.js", "README.md", "package-lock.json"):
            self.write(path)
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root).decode().strip()

    def write(self, path, content="original\n"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture change")
        return self.git("rev-parse", "HEAD")

    def run(self, base=None, head=None, inputs=(), code=0):
        self.calls += 1
        out = self.root.parent / (self.name + "-report.json")
        command = [sys.executable, str(self.root / "scripts/select_tests.py"),
                   "--base=" + (base or self.base), "--head=" + (head or self.git("rev-parse", "HEAD")), "--out", str(out)]
        for label in inputs:
            command += ["--changed-input", label]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        report = json.loads(out.read_text())
        if os.environ.get("TEST_SELECTION_EVIDENCE_DIR"):
            evidence = Path(os.environ["TEST_SELECTION_EVIDENCE_DIR"])
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / (self.name + "-%d.json" % self.calls)).write_text(json.dumps({
                "command": command, "exit": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr, "report": report}, indent=2) + "\n")
        assert result.returncode == code, result.stderr
        assert report["advisory"] and not report["selective_safe"] and not report["tests_executed"]
        return report


@pytest.fixture
def repo(tmp_path, request):
    root = tmp_path / "repo"
    root.mkdir()
    return Repo(root, request.node.name)


def ids(report):
    return {b["id"] for b in report["boundaries"]}


@pytest.mark.parametrize("path,boundaries", [
    ("renderer/view.js", {"client_wire_render"}),
    (V2 + "provider_account_native.py", {"provider_startup"}),
    (V2 + "satellite.py", {"satellite_transition", "client_wire_render"}),
    (V2 + "event_push.py", {"satellite_transition", "client_wire_render"}),
    (V2 + "deploy/deploy.py", {"deploy_invocation"}),
    ("pentacle-chat-core/src/state.ts", {"client_wire_render"}),
    (V2 + "tests/test_provider_account_native.py", {"provider_startup"}),
])
def test_producers_consumers_and_tests(repo, path, boundaries):
    repo.write(path, "changed\n")
    head = repo.commit()
    report = repo.run(head=head)
    assert ids(report) == boundaries
    assert report["classification"] == "mapped"
    assert report["inputs"]["base"] == repo.base and report["inputs"]["head"] == head
    assert all(t["available_in_head"] for t in report["selected_targets"])
    assert all(b["covered_behavior"] and b["missing_coverage"] for b in report["boundaries"])


def test_spaces_newlines_and_rename_endpoints(repo):
    old = "renderer/a file\nwith newline.js"
    repo.write(old, "unique rename body\n" * 20)
    base = repo.commit()
    new = "unmapped/renamed file\n.js"
    (repo.root / "unmapped").mkdir()
    repo.git("mv", old, new)
    head = repo.commit()
    report = repo.run(base=base, head=head)
    assert report["changes"] == [{"status": "R100", "paths": [old, new]}]
    assert report["changed_paths"] == sorted([old, new])
    assert report["classification"] == "broad_fallback" and ids(report) == ALL
    assert "unmapped path: " + new in report["fallback_reasons"]


def test_delete_and_add(repo):
    (repo.root / "renderer/view.js").unlink()
    repo.write("renderer/added file.js", "unrelated new content\n")
    repo.commit()
    report = repo.run()
    assert {(c["status"], c["paths"][0]) for c in report["changes"]} == {
        ("D", "renderer/view.js"), ("A", "renderer/added file.js")}
    assert ids(report) == {"client_wire_render"}


@pytest.mark.parametrize("path", [V2 + "unmapped_runtime.py", "docs/runtime.json", "docs/run.js"])
def test_unknown_runtime_is_broad(repo, path):
    repo.write(path)
    repo.commit()
    report = repo.run()
    assert report["classification"] == "broad_fallback" and ids(report) == ALL


@pytest.mark.parametrize("path", ["package-lock.json", "services/_shared/wire.py", "scripts/select_tests.py",
                                      "scripts/test-selection.json", "tools/new_gate.py", ".github/workflows/check.yml"])
def test_shared_dependency_and_harness_edits_select_full_gates(repo, path):
    target = repo.root / path
    if target.exists():
        repo.write(path, target.read_text() + "\n")
    else:
        repo.write(path)
    repo.commit()
    report = repo.run()
    assert ids(report) == ALL and report["classification"] == "broad_fallback"
    assert {t["target"] for t in report["selected_targets"]} >= {
        "v2-merge", "cli-pytest", "shared-pytest", "npm-test", "web-gate"}


def test_deleted_reference_even_with_docs_diff_is_broad(repo):
    missing = V2 + "tests/test_provider_account_native.py"
    (repo.root / missing).unlink()
    head = repo.commit()
    report = repo.run()
    assert missing in report["missing_target_files"] and ids(report) == ALL
    assert any(not t["available_in_head"] for t in report["selected_targets"])
    # A later docs-only baseline still cannot hide a missing target in HEAD.
    repo.write("README.md", "docs\n")
    repo.commit()
    assert repo.run(base=head)["classification"] == "broad_fallback"


def test_target_presence_uses_requested_head_not_worktree(repo):
    missing = V2 + "tests/test_provider_account_native.py"
    repo.write("README.md", "docs\n")
    head = repo.commit()
    (repo.root / missing).unlink()
    assert repo.run(head=head)["classification"] == "docs_only"
    deleted = repo.commit()
    repo.write(missing)
    assert repo.run(head=deleted)["classification"] == "broad_fallback"


@pytest.mark.parametrize("label,expected", [
    ("provider_config", {"provider_startup"}), ("client_versions", {"client_wire_render"}),
    ("satellite_config", {"satellite_transition", "client_wire_render"}),
    ("deploy_config", {"deploy_invocation"}), ("unrecognized", ALL)])
def test_external_inputs_with_identical_source(repo, label, expected):
    report = repo.run(inputs=[label])
    assert report["changes"] == [] and ids(report) == expected
    assert report["selected_targets"]


def test_multiple_external_inputs_union_and_determinism(repo):
    first = repo.run(inputs=["provider_config", "client_versions", "provider_config"])
    second = repo.run(inputs=["client_versions", "provider_config"])
    assert first == second
    assert ids(first) == {"provider_startup", "client_wire_render"}


def test_actual_baseline_older_than_one_commit(repo):
    repo.write(V2 + "provider_account_native.py", "provider\n")
    previous = repo.commit()
    repo.write(V2 + "deploy/deploy.py", "deploy\n")
    repo.commit()
    assert ids(repo.run()) == {"provider_startup", "deploy_invocation"}
    assert ids(repo.run(base=previous)) == {"deploy_invocation"}


def test_gitlink_add_change_delete_and_type_change_are_broad(repo):
    path = "renderer/vendor"
    repo.git("update-index", "--add", "--cacheinfo", "160000," + repo.base + "," + path)
    repo.git("commit", "-qm", "add gitlink")
    first = repo.git("rev-parse", "HEAD")
    report = repo.run(head=first)
    assert "unknown gitlink contents: " + path in report["fallback_reasons"]
    repo.git("update-index", "--cacheinfo", "160000," + first + "," + path)
    repo.git("commit", "-qm", "change gitlink")
    second = repo.git("rev-parse", "HEAD")
    assert repo.run(base=first)["classification"] == "broad_fallback"
    repo.git("update-index", "--force-remove", path)
    repo.git("commit", "-qm", "delete gitlink")
    third = repo.git("rev-parse", "HEAD")
    assert repo.run(base=second)["classification"] == "broad_fallback"
    repo.write(path)
    repo.commit()
    report = repo.run(base=second)
    assert report["changes"] == [{"status": "T", "paths": [path]}]
    assert report["classification"] == "broad_fallback"
    assert third != second


@pytest.mark.parametrize("revision", ["f" * 40, "HEAD", "--help"])
def test_invalid_revision_is_nonzero_invalid_report(repo, revision):
    report = repo.run(head=revision, code=2)
    assert report["classification"] == "invalid" and report["selected_targets"] == []
    assert report["error"]


@pytest.mark.parametrize("mutation", ["target", "missing_fields", "docs_runtime", "selection", "empty_gates", "duplicate_key"])
def test_malformed_map_fails_closed(repo, mutation):
    path = repo.root / "scripts/test-selection.json"
    data = json.loads(path.read_text())
    if mutation == "target":
        data["boundaries"]["provider_startup"]["targets"][0]["target"] = "shell-command"
    elif mutation == "missing_fields":
        del data["boundaries"]["provider_startup"]["missing_coverage"]
    elif mutation == "docs_runtime":
        data["docs_only"] = ["*"]
    elif mutation == "selection":
        data["boundaries"]["provider_startup"]["targets"][0]["select"] = ["../../outside.py"]
    elif mutation == "empty_gates":
        data["full_gates"] = []
    path.write_text(json.dumps(data) if mutation != "duplicate_key" else '{"schema":1,"schema":2}')
    repo.commit()
    report = repo.run(code=2)
    assert report["classification"] == "invalid" and report["error"]


def test_docs_only_empty_diff_and_map_identity(repo):
    report = repo.run()
    assert report["classification"] == "no_changes" and report["selected_targets"] == []
    repo.write("docs/guide.md", "guide\n")
    repo.commit()
    report = repo.run()
    assert report["classification"] == "docs_only" and report["selected_targets"] == []
    assert report["map"]["sha256"] == hashlib.sha256((repo.root / "scripts/test-selection.json").read_bytes()).hexdigest()
    assert report["docs_only_paths"] == ["docs/guide.md"]


def test_chat_core_gap_is_explicit_and_not_a_fictitious_npm_selection(repo):
    repo.write("pentacle-chat-core/src/state.ts")
    repo.commit()
    report = repo.run()
    client = report["boundaries"][0]
    assert any("standalone" in gap and "npm-test" in gap for gap in client["missing_coverage"])
    assert all(not selection.startswith("pentacle-chat-core/") for t in report["selected_targets"] for selection in t["select"])
