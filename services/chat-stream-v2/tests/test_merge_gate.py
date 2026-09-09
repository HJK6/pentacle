"""Regression coverage for the repository-native v2 promotion guard."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "tools" / "merge_gate.py"
SPEC = importlib.util.spec_from_file_location("v2_merge_gate", MODULE_PATH)
assert SPEC and SPEC.loader
merge_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge_gate)

OLD = "a" * 40
CANDIDATE = "b" * 40


def _result(args: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, "")


def test_promote_tags_before_cas_fast_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    main_reads = iter((OLD, CANDIDATE))

    def command(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0:2] == ["git", "rev-parse"]:
            return _result(args, OLD if args[-1] == "origin/main" else CANDIDATE)
        if args[0:2] == ["git", "ls-remote"]:
            return _result(args, next(main_reads) + "\trefs/heads/main\n")
        if args[0] == "gh" and args[-1].endswith("/runs/123"):
            return _result(args, json.dumps({"id": 123, "workflow_id": 7, "status": "completed", "conclusion": "success", "event": "push", "head_sha": CANDIDATE, "html_url": "https://github.com/example-org/pentacle/actions/runs/123"}))
        if args[0] == "gh":
            return _result(args, json.dumps({"name": "chat-stream-v2-smoke", "path": ".github/workflows/chat-stream-v2-smoke.yml"}))
        if args[0:2] == ["git", "show-ref"]:
            return _result(args, returncode=1)
        return _result(args)

    monkeypatch.setattr(merge_gate, "_command", command)
    result = merge_gate.promote(CANDIDATE, 123)

    assert result["tag"] == f"v2-gate/{CANDIDATE}"
    tag_push = ["git", "push", "origin", f"refs/tags/v2-gate/{CANDIDATE}"]
    main_push = ["git", "push", f"--force-with-lease=refs/heads/main:{OLD}", "origin", f"{CANDIDATE}:refs/heads/main"]
    assert calls.index(tag_push) < calls.index(main_push)


def test_verify_tag_refuses_annotation_for_a_different_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    annotation = "\n".join((
        "workflow_id: 7", "run_id: 123", "run_url: https://github.com/example-org/pentacle/actions/runs/123",
        f"headSha: {OLD}", f"old_main_sha: {OLD}", f"candidate_sha: {OLD}", "timestamp: 2026-08-21T00:00:00Z",
    ))

    def command(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
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
