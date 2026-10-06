"""Regression coverage for the repository-native v2 promotion guard."""

from __future__ import annotations

import hashlib
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
TAG_OBJECT = "c" * 40
WORKFLOW_TEXT = "name: Public checks\non:\n  push:\njobs:\n  checks:\n    steps:\n      - run: npm test\n"
EXPECTED_PUBLIC_SHA256 = "401ac4338e921171d7037966d1b671b8feb42722cff395952bc7034e60767104"
CHECKOUT_LOG = (Path(__file__).parent / "fixtures/th_h5/tag_checkout.txt").read_text()
EXPECTED_PUBLIC_STEPS = {
    "Run npm test", "Run python3 scripts/test_check_public_residue.py",
    "Run python3 scripts/check_public_residue.py", "Source integrity after Chrome install",
    "Portable microphone contracts", "Bounded dashboard dependency",
    "Web mode E2E gate", "Daemon and CLI checks", "Source integrity at gate end",
}
EXISTING_ANNOTATION = "\n".join((
    "workflow_id: 7", "run_id: 123",
    "run_url: https://github.com/HJK6/pentacle/actions/runs/123",
    f"headSha: {CANDIDATE}", f"old_main_sha: {OLD}",
    f"candidate_sha: {CANDIDATE}", "timestamp: 2026-09-26T16:22:06Z",
))


def _result(args: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, "")


def _fake_evidence(monkeypatch: pytest.MonkeyPatch, *, repository: str = "HJK6/pentacle") -> tuple[dict, dict, dict, list[list[str]]]:
    path, name = (
        (".github/workflows/predeploy-tests.yml", "Public checks")
        if repository == "HJK6/pentacle" else
        (".github/workflows/chat-stream-v2-smoke.yml", "chat-stream-v2-smoke")
    )
    run = {
        "id": 123, "workflow_id": 7, "status": "completed", "conclusion": "success",
        "event": "push", "head_sha": CANDIDATE, "head_branch": "codex/candidate",
        "run_attempt": 1, "repository": {"full_name": repository},
        "head_repository": {"full_name": repository},
        "html_url": f"https://github.com/{repository}/actions/runs/123",
    }
    workflow = {"id": 7, "name": name, "path": path}
    jobs = {"total_count": 1, "jobs": [{
        "name": "checks", "head_sha": CANDIDATE, "status": "completed", "conclusion": "success",
        "steps": [{"name": step, "status": "completed", "conclusion": "success"}
                  for step in sorted(EXPECTED_PUBLIC_STEPS)],

    }]}
    calls: list[list[str]] = []
    monkeypatch.setenv("PENTACLE_GITHUB_REPOSITORY", repository)
    monkeypatch.setattr(merge_gate, "PUBLIC_WORKFLOW_SHA256", hashlib.sha256(WORKFLOW_TEXT.encode()).hexdigest())

    def command(args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["git", "remote", "get-url", "origin"]:
            return _result(args, f"git@github.com:{repository}.git")
        if args[:3] == ["git", "check-ref-format", "--branch"]:
            return _result(args)
        if args == ["git", "ls-remote", "--exit-code", "origin", "refs/heads/codex/candidate"]:
            return _result(args, f"{CANDIDATE}\trefs/heads/codex/candidate\n")
        if args == ["git", "show", f"{CANDIDATE}:{path}"]:
            return _result(args, WORKFLOW_TEXT)
        if args[:2] == ["git", "rev-parse"]:
            return _result(args, OLD if args[-1] == "origin/main" else CANDIDATE)
        if args == ["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"]:
            return _result(args, f"{OLD}\trefs/heads/main\n")
        if args == ["gh", "api", f"repos/{repository}/actions/workflows/7/runs?event=push&head_sha={CANDIDATE}&per_page=100&page=1"]:
            return _result(args,json.dumps({"total_count":0,"workflow_runs":[]}))
        if args == ["gh", "api", f"repos/{repository}/actions/workflows/7/runs?event=push&head_sha={CANDIDATE}&per_page=100"]:
            promoted = {**run, "id":124, "run_number":2, "head_branch":f"v2-gate/{CANDIDATE}",
                        "path":path, "html_url":f"https://github.com/{repository}/actions/runs/124"}
            return _result(args, json.dumps({"total_count":1,"workflow_runs":[promoted]}))
        if args == ["gh", "api", f"repos/{repository}/actions/runs/124/attempts/1/jobs?per_page=100"]:
            return _result(args, json.dumps({**jobs, "jobs":[{**job, "id":1240, "run_id":124, "run_attempt":1, "steps":job["steps"] + [{"name":"Set up job","number":1,"status":"completed","conclusion":"success"},{"name":"Run actions/checkout@v4","number":2,"status":"completed","conclusion":"success"}]} for job in jobs["jobs"]]}))
        if args == ["gh", "api", f"repos/{repository}/actions/jobs/1240/steps/1/logs"]:
            return _result(args, CHECKOUT_LOG)
        if args == ["gh", "api", f"repos/{repository}/actions/runs/123"]:
            return _result(args, json.dumps(run))
        if args == ["gh", "api", f"repos/{repository}/actions/workflows/7"]:
            return _result(args, json.dumps(workflow))
        if args == ["gh", "api", f"repos/{repository}/actions/runs/123/attempts/1/jobs?per_page=100"]:
            return _result(args, json.dumps(jobs))
        if args[:2] == ["git", "show-ref"]:
            return _result(args, returncode=1)
        return _result(args)

    monkeypatch.setattr(merge_gate, "_command", command)
    return run, workflow, jobs, calls


def test_public_contract_is_bound_to_audited_commands_and_steps() -> None:
    assert merge_gate.PUBLIC_WORKFLOW_SHA256 == EXPECTED_PUBLIC_SHA256
    assert merge_gate.PUBLIC_REQUIRED_STEPS == EXPECTED_PUBLIC_STEPS


@pytest.mark.parametrize("repository", ["HJK6/pentacle", "HJK6/pentacle-private"])
def test_mapped_push_run_is_admitted(monkeypatch: pytest.MonkeyPatch, repository: str) -> None:
    _fake_evidence(monkeypatch, repository=repository)
    workflow_id, run = merge_gate._run_evidence(123, CANDIDATE)
    assert workflow_id == 7
    assert run["id"] == 123


def test_promote_tags_before_cas_fast_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, calls = _fake_evidence(monkeypatch)
    main_reads = iter((OLD, CANDIDATE))
    tag = f"refs/tags/v2-gate/{CANDIDATE}"
    tag_object = TAG_OBJECT
    tag_reads = iter(("", f"{tag_object}\t{tag}\n{CANDIDATE}\t{tag}^{{}}\n", f"{tag_object}\t{tag}\n{CANDIDATE}\t{tag}^{{}}\n"))
    original = merge_gate._command

    def command(args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if args == ["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"]:
            calls.append(args)
            return _result(args, next(main_reads) + "\trefs/heads/main\n")
        if args == ["git", "ls-remote", "--tags", "origin", tag, f"{tag}^{{}}"]:
            calls.append(args)
            return _result(args, next(tag_reads))
        if args == ["git", "rev-parse", f"{tag}^{{tag}}"]:
            calls.append(args)
            return _result(args, tag_object)
        return original(args, input_text=input_text, timeout=timeout)

    monkeypatch.setattr(merge_gate, "_command", command)
    result = merge_gate.promote(CANDIDATE, 123)
    assert result["tag"] == f"v2-gate/{CANDIDATE}"
    tag_push = ["git", "push", "origin", f"refs/tags/v2-gate/{CANDIDATE}"]
    main_push = ["git", "push", f"--force-with-lease=refs/heads/main:{OLD}", "origin", f"{CANDIDATE}:refs/heads/main"]
    assert calls.index(tag_push) < calls.index(main_push)


def test_existing_exact_remote_tag_skips_noop_push_before_cas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-entry must not offer an unchanged tag to the fail-closed pre-push hook."""
    _, _, _, calls = _fake_evidence(monkeypatch)
    tag = f"v2-gate/{CANDIDATE}"
    tag_ref = f"refs/tags/{tag}"
    tag_object = TAG_OBJECT
    annotation = EXISTING_ANNOTATION
    main_reads = iter((OLD, CANDIDATE))
    original = merge_gate._command

    def command(args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if args == ["git", "show-ref", "--verify", "--quiet", tag_ref]:
            calls.append(args)
            return _result(args)
        if args == ["git", "for-each-ref", "--format=%(contents)", tag_ref]:
            calls.append(args)
            return _result(args, annotation)
        if args == ["git", "rev-parse", f"{tag_ref}^{{tag}}"]:
            calls.append(args)
            return _result(args, tag_object)
        if args == ["git", "rev-parse", f"{tag_ref}^{{commit}}"]:
            calls.append(args)
            return _result(args, CANDIDATE)
        if args == ["git", "ls-remote", "--tags", "origin", tag_ref, f"{tag_ref}^{{}}"]:
            calls.append(args)
            return _result(args, f"{tag_object}\t{tag_ref}\n{CANDIDATE}\t{tag_ref}^{{}}\n")
        if args == ["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"]:
            calls.append(args)
            return _result(args, next(main_reads) + "\trefs/heads/main\n")
        if args == ["git", "push", "origin", tag_ref]:
            calls.append(args)
            return _result(args, returncode=1)  # installed hook rejects no-op stdin
        return original(args, input_text=input_text, timeout=timeout)

    monkeypatch.setattr(merge_gate, "_command", command)
    result = merge_gate.promote(CANDIDATE, 123)
    assert result["candidate"] == CANDIDATE
    assert ["git", "push", "origin", tag_ref] not in calls
    assert ["git", "push", f"--force-with-lease=refs/heads/main:{OLD}", "origin", f"{CANDIDATE}:refs/heads/main"] in calls


def _existing_tag_case(
    monkeypatch: pytest.MonkeyPatch, remote_rows: str | None, *,
    annotation: str = EXISTING_ANNOTATION, annotated_local: bool = True,
) -> list[list[str]]:
    _, _, _, calls = _fake_evidence(monkeypatch)
    ref = f"refs/tags/v2-gate/{CANDIDATE}"
    original = merge_gate._command

    def command(args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if args == ["git", "show-ref", "--verify", "--quiet", ref]:
            calls.append(args)
            return _result(args)
        if args == ["git", "for-each-ref", "--format=%(contents)", ref]:
            calls.append(args)
            return _result(args, annotation)
        if args == ["git", "rev-parse", f"{ref}^{{tag}}"]:
            calls.append(args)
            return _result(args, TAG_OBJECT, 0 if annotated_local else 1)
        if args == ["git", "rev-parse", f"{ref}^{{commit}}"]:
            calls.append(args)
            return _result(args, CANDIDATE)
        if args == ["git", "ls-remote", "--tags", "origin", ref, f"{ref}^{{}}"]:
            calls.append(args)
            return _result(args, remote_rows or "", 0 if remote_rows is not None else 1)
        return original(args, input_text=input_text, timeout=timeout)

    monkeypatch.setattr(merge_gate, "_command", command)
    return calls


@pytest.mark.parametrize("kind", [
    "same_peel_different_object", "wrong_peel", "partial_object", "partial_peel",
    "duplicate", "malformed", "unavailable", "absent_after_push",
])
def test_conflicting_or_unreadable_remote_tag_refuses_before_main(
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    ref = f"refs/tags/v2-gate/{CANDIDATE}"
    object_row = f"{TAG_OBJECT}\t{ref}\n"
    peel_row = f"{CANDIDATE}\t{ref}^{{}}\n"
    cases = {
        "same_peel_different_object": f"{'d' * 40}\t{ref}\n{peel_row}",
        "wrong_peel": f"{object_row}{OLD}\t{ref}^{{}}\n",
        "partial_object": object_row,
        "partial_peel": peel_row,
        "duplicate": f"{object_row}{object_row}{peel_row}",
        "malformed": f"{object_row}not-a-sha\t{ref}^{{}}\n",
        "unavailable": None,
        "absent_after_push": "",
    }
    calls = _existing_tag_case(monkeypatch, cases[kind])
    with pytest.raises(merge_gate.GateError):
        merge_gate.promote(CANDIDATE, 123)
    assert not any(args[:2] == ["git", "push"] and args[-1] == f"{CANDIDATE}:refs/heads/main" for args in calls)


@pytest.mark.parametrize("kind", ["lightweight_local", "wrong_annotation"])
def test_invalid_local_tag_refuses_before_any_push(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    ref = f"refs/tags/v2-gate/{CANDIDATE}"
    rows = f"{TAG_OBJECT}\t{ref}\n{CANDIDATE}\t{ref}^{{}}\n"
    annotation = EXISTING_ANNOTATION.replace("run_id: 123", "run_id: 124") if kind == "wrong_annotation" else EXISTING_ANNOTATION
    calls = _existing_tag_case(monkeypatch, rows, annotation=annotation, annotated_local=kind != "lightweight_local")
    with pytest.raises(merge_gate.GateError):
        merge_gate.promote(CANDIDATE, 123)
    assert not any(args[:2] == ["git", "push"] for args in calls)


def test_tag_check_queued_then_green_succeeds_in_one_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tag-triggered check is awaited before the first and only main push."""
    _, _, _, calls = _fake_evidence(monkeypatch)
    ref = f"refs/tags/v2-gate/{CANDIDATE}"
    state: dict[str, object] = {
        "local_tag": False, "remote_tag": False, "tag_check": "queued",
        "main": OLD, "main_pushes": 0, "annotation": "", "polls":0,
    }
    original = merge_gate._command

    def command(args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["gh", "api"] and "/workflows/7/runs?" in args[2] and not args[2].endswith("&page=1"):
            response = original(args, input_text=input_text, timeout=timeout)
            payload = json.loads(response.stdout)
            state["polls"] += 1
            if state["polls"] == 1:
                payload["workflow_runs"][0].update(status="queued", conclusion=None)
            else:
                state["tag_check"] = "success"
            return _result(args, json.dumps(payload))
        if args == ["git", "show-ref", "--verify", "--quiet", ref]:
            calls.append(args)
            return _result(args, returncode=0 if state["local_tag"] else 1)
        if args[:3] == ["git", "tag", "-a"]:
            calls.append(args)
            state["local_tag"] = True
            state["annotation"] = args[-1]
            return _result(args)
        if args == ["git", "for-each-ref", "--format=%(contents)", ref]:
            calls.append(args)
            return _result(args, str(state["annotation"]))
        if args == ["git", "rev-parse", f"{ref}^{{tag}}"]:
            calls.append(args)
            return _result(args, TAG_OBJECT)
        if args == ["git", "rev-parse", f"{ref}^{{commit}}"]:
            calls.append(args)
            return _result(args, CANDIDATE)
        if args == ["git", "ls-remote", "--tags", "origin", ref, f"{ref}^{{}}"]:
            calls.append(args)
            rows = f"{TAG_OBJECT}\t{ref}\n{CANDIDATE}\t{ref}^{{}}\n" if state["remote_tag"] else ""
            return _result(args, rows)
        if args == ["git", "push", "origin", ref]:
            calls.append(args)
            state["remote_tag"] = True
            return _result(args)
        if args == ["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"]:
            calls.append(args)
            return _result(args, f"{state['main']}\trefs/heads/main\n")
        if args == ["git", "push", f"--force-with-lease=refs/heads/main:{OLD}", "origin", f"{CANDIDATE}:refs/heads/main"]:
            calls.append(args)
            state["main_pushes"] = int(state["main_pushes"]) + 1
            if state["tag_check"] == "queued":
                return _result(args, returncode=1)  # GH006 required check queued
            state["main"] = CANDIDATE
            return _result(args)
        return original(args, input_text=input_text, timeout=timeout)

    monkeypatch.setattr(merge_gate, "_command", command)
    clock = [0.0]
    result = merge_gate.promote(CANDIDATE, 123, checks_timeout_s=10, poll_s=1,
                               clock=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    assert result["candidate"] == CANDIDATE and state["main"] == CANDIDATE
    assert state["main_pushes"] == 1
    assert state["polls"] >= 3
    assert calls.count(["git", "push", "origin", ref]) == 1


@pytest.mark.parametrize("change", [
    "unknown_origin", "non_github_origin", "missing_selector", "repo_selector", "run_id", "run_repo", "head_repo", "ref",
    "event", "sha", "run_status", "run_conclusion", "job_status", "job_conclusion",
    "workflow_path", "workflow_name", "workflow_id",
    "workflow_content", "missing_step", "failed_step", "skipped_step", "job_sha",
])
def test_bad_public_evidence_refuses_before_push(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    run, workflow, jobs, calls = _fake_evidence(monkeypatch)
    if change in ("unknown_origin", "non_github_origin"):
        original = merge_gate._command

        def command(args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
            if args == ["git", "remote", "get-url", "origin"]:
                calls.append(args)
                origin = "git@github.com:other/pentacle.git" if change == "unknown_origin" else "git@example.com:HJK6/pentacle.git"
                return _result(args, origin)
            return original(args, input_text=input_text, timeout=timeout)

        monkeypatch.setattr(merge_gate, "_command", command)
    elif change == "missing_selector":
        monkeypatch.delenv("PENTACLE_GITHUB_REPOSITORY")
    elif change == "repo_selector":
        monkeypatch.setenv("PENTACLE_GITHUB_REPOSITORY", "HJK6/pentacle-private")
    elif change == "run_id":
        run["id"] = 124
    elif change == "run_repo":
        run["repository"]["full_name"] = "HJK6/pentacle-private"
    elif change == "head_repo":
        run["head_repository"]["full_name"] = "HJK6/pentacle-private"
    elif change == "ref":
        run["head_branch"] = "main"
    elif change == "event":
        run["event"] = "pull_request"
    elif change == "sha":
        run["head_sha"] = OLD
    elif change == "run_status":
        run["status"] = "in_progress"
    elif change == "run_conclusion":
        run["conclusion"] = "failure"
    elif change == "job_status":
        jobs["jobs"][0]["status"] = "in_progress"
    elif change == "job_conclusion":
        jobs["jobs"][0]["conclusion"] = "failure"
    elif change == "workflow_path":
        workflow["path"] = ".github/workflows/other.yml"
    elif change == "workflow_name":
        workflow["name"] = "Other checks"
    elif change == "workflow_id":
        workflow["id"] = 8
    elif change == "workflow_content":
        monkeypatch.setattr(merge_gate, "PUBLIC_WORKFLOW_SHA256", "0" * 64)
    elif change == "missing_step":
        jobs["jobs"][0]["steps"].pop()
    elif change in ("failed_step", "skipped_step"):
        jobs["jobs"][0]["steps"][0]["conclusion"] = "failure" if change == "failed_step" else "skipped"
    elif change == "job_sha":
        jobs["jobs"][0]["head_sha"] = OLD
    with pytest.raises(merge_gate.GateError):
        merge_gate.promote(CANDIDATE, 123)
    assert not any(args[:2] == ["git", "push"] or args[:2] == ["git", "tag"] for args in calls)


def test_superseded_branch_tip_refuses_before_push(monkeypatch: pytest.MonkeyPatch) -> None:
    _run, _workflow, _jobs, calls = _fake_evidence(monkeypatch)
    original = merge_gate._command

    def command(args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if args == ["git", "ls-remote", "--exit-code", "origin", "refs/heads/codex/candidate"]:
            calls.append(args)
            return _result(args, f"{OLD}\trefs/heads/codex/candidate\n")
        return original(args, input_text=input_text, timeout=timeout)

    monkeypatch.setattr(merge_gate, "_command", command)
    with pytest.raises(merge_gate.GateError, match="branch no longer resolves"):
        merge_gate.promote(CANDIDATE, 123)
    assert not any(args[:2] == ["git", "push"] or args[:2] == ["git", "tag"] for args in calls)


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


@pytest.mark.parametrize("required", ["Portable microphone contracts", "Bounded dashboard dependency"])
@pytest.mark.parametrize("change", ["missing", "skipped", "failed"])
def test_portable_candidate_coverage_cannot_be_omitted(monkeypatch, required, change):
    _, _, jobs, calls = _fake_evidence(monkeypatch)
    steps = jobs["jobs"][0]["steps"]
    if change == "missing":
        steps[:] = [step for step in steps if step["name"] != required]
    else:
        next(step for step in steps if step["name"] == required)["conclusion"] = change
    with pytest.raises(merge_gate.GateError, match=required):
        merge_gate.promote(CANDIDATE, 123)
    assert not any(call[:2] == ["git", "push"] for call in calls)
