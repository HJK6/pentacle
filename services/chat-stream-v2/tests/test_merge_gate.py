"""Retired promotion refusal and retained legacy tag verification contracts."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = Path(os.environ.get("PENTACLE_MERGE_GATE_TEST_MODULE", REPO_ROOT / "tools" / "merge_gate.py"))
SPEC = importlib.util.spec_from_file_location("v2_merge_gate", MODULE_PATH)
assert SPEC and SPEC.loader
merge_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge_gate)
OLD = "a" * 40
CANDIDATE = "b" * 40


def _result(args: list[str], stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_verify_tag_refuses_annotation_for_a_different_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    annotation = "\n".join((
        "workflow_id: 7", "run_id: 123", "run_url: https://github.com/HJK6/pentacle/actions/runs/123",
        f"headSha: {OLD}", f"old_main_sha: {OLD}", f"candidate_sha: {OLD}", "timestamp: 2026-08-21T00:00:00Z",
    ))

    def command(args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if args[0:2] == ["git", "show-ref"]:
            return _result(args)
        if args[0:2] == ["git", "ls-remote"]:
            tag = f"refs/tags/v2-gate/{CANDIDATE}"
            return _result(args, f"d{'0' * 39}\t{tag}\n{CANDIDATE}\t{tag}^{{}}\n")
        if args[0:2] == ["git", "rev-parse"]:
            return _result(args, f"d{'0' * 39}" if args[-1] == f"v2-gate/{CANDIDATE}" else CANDIDATE)
        if args[0:2] == ["git", "for-each-ref"]:
            return _result(args, annotation)
        return _result(args)

    monkeypatch.setattr(merge_gate, "_command", command)
    with pytest.raises(merge_gate.GateError, match="does not bind"):
        merge_gate.verify_tag(CANDIDATE)

@pytest.mark.parametrize("candidate", [CANDIDATE, "malformed-candidate"])
def test_retired_promote_refuses_before_any_command(monkeypatch, capsys, candidate):
    calls = []

    def forbidden(args, **kwargs):
        calls.append(args)
        raise AssertionError("retired promotion reached an external command")

    monkeypatch.setattr(merge_gate, "_command", forbidden)
    monkeypatch.setattr(merge_gate.subprocess, "run", forbidden)
    with pytest.raises(merge_gate.GateError, match="retired.*pull request"):
        merge_gate.promote(candidate, 123, checks_timeout_s=0)
    assert merge_gate.main(["promote", "--candidate", candidate, "--run-id", "123",
                            "--checks-timeout-seconds", "0"]) == 1
    assert "retired" in capsys.readouterr().err
    assert calls == []


def test_verify_tag_accepts_matching_existing_tag(monkeypatch, capsys):
    tag = "v2-gate/" + CANDIDATE
    tag_object = "c" * 40
    fields = {"workflow_id": "7", "run_id": "123",
              "run_url": "https://github.com/HJK6/pentacle/actions/runs/123",
              "headSha": CANDIDATE, "old_main_sha": OLD, "candidate_sha": CANDIDATE,
              "timestamp": "2026-08-21T00:00:00Z"}
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["git", "ls-remote"]:
            return _result(args, f"{tag_object}\trefs/tags/{tag}\n{CANDIDATE}\trefs/tags/{tag}^{{}}\n")
        if args[:2] == ["git", "show-ref"]:
            return _result(args)
        if args[:2] == ["git", "rev-parse"]:
            return _result(args, tag_object if args[-1] == tag else CANDIDATE)
        if args[:2] == ["git", "for-each-ref"]:
            return _result(args, "\n".join(f"{key}: {value}" for key, value in fields.items()))
        assert args[1].endswith("tools/check_governance_tripwires.py")
        return _result(args)

    monkeypatch.setattr(merge_gate, "_command", command)
    assert merge_gate.main(["verify-tag", "--candidate", CANDIDATE]) == 0
    assert json.loads(capsys.readouterr().out) == fields
    assert not any(args[:2] in (["git", "push"], ["git", "tag"]) for args in calls)
