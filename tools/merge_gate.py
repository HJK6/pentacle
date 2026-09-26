#!/usr/bin/env python3
"""The sole guarded promotion path for a chat-stream-v2 candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ORIGIN_RE = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
    r"(HJK6/(?:pentacle|pentacle-private))(?:\.git)?$"
)
PUBLIC_WORKFLOW_SHA256 = "92e5b508dfd16921d6aad6a7df49bd90dc2295eab9821cc5b817e3d58302bc0f"
WORKFLOWS = {
    "HJK6/pentacle": (".github/workflows/predeploy-tests.yml", "Public checks"),
    "HJK6/pentacle-private": (".github/workflows/chat-stream-v2-smoke.yml", "chat-stream-v2-smoke"),
}
PUBLIC_REQUIRED_STEPS = frozenset({
    "Run npm test",
    "Run python3 scripts/test_check_public_residue.py",
    "Run python3 scripts/check_public_residue.py",
    "Source integrity after Chrome install",
    "Web mode E2E gate",
    "Daemon and CLI checks",
    "Source integrity at gate end",
})

class GateError(RuntimeError):
    """A promotion or tag evidence precondition was not met."""


def _command(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=REPO_ROOT, input=input_text, text=True, capture_output=True, check=False
    )


def _require(result: subprocess.CompletedProcess[str], context: str) -> str:
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise GateError(f"{context}: {detail}")
    return result.stdout.strip()


def _git(*args: str) -> str:
    return _require(_command(["git", *args]), "git " + " ".join(args))


def _sha(value: str, label: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise GateError(f"{label} must be an exact lowercase 40-character SHA")
    return value


def _tag_name(candidate: str) -> str:
    return f"v2-gate/{candidate}"


def _tag_annotation(candidate: str, old_main: str, workflow_id: int, run: dict[str, object]) -> str:
    return "\n".join((
        f"workflow_id: {workflow_id}",
        f"run_id: {run['id']}",
        f"run_url: {run['html_url']}",
        f"headSha: {candidate}",
        f"old_main_sha: {old_main}",
        f"candidate_sha: {candidate}",
        f"timestamp: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
    ))


def _verify_annotation(annotation: str) -> dict[str, str]:
    checker = REPO_ROOT / "tools" / "check_governance_tripwires.py"
    _require(
        _command([sys.executable, str(checker), "--kind", "v2-gate-tag", "-"], input_text=annotation),
        "v2-gate tag schema",
    )
    return dict(line.split(": ", 1) for line in annotation.strip().splitlines())


def _tag_contents(tag: str) -> str:
    return _git("for-each-ref", "--format=%(contents)", f"refs/tags/{tag}")


def _remote_main() -> str:
    line = _git("ls-remote", "--exit-code", "origin", "refs/heads/main").splitlines()[0]
    return _sha(line.split()[0], "origin/main")


def _remote_tag_identity(tag: str) -> tuple[str, str] | None:
    """Return an exact annotated remote tag object and peel, or unambiguous absence."""
    ref = f"refs/tags/{tag}"
    peeled = f"{ref}^{{}}"
    raw = _git("ls-remote", "--tags", "origin", ref, peeled)
    if not raw:
        return None
    rows: dict[str, str] = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[1] not in (ref, peeled) or fields[1] in rows:
            raise GateError(f"origin {tag} has ambiguous tag refs")
        rows[fields[1]] = _sha(fields[0], f"origin {fields[1]}")
    if set(rows) != {ref, peeled}:
        raise GateError(f"origin {tag} is missing its annotated tag object or peel")
    return rows[ref], rows[peeled]


def _repository_contract() -> tuple[str, str, str]:
    origin = _git("remote", "get-url", "origin")
    match = ORIGIN_RE.fullmatch(origin)
    if match is None:
        raise GateError("origin is not a mapped GitHub repository")
    repository = match.group(1)
    if os.environ.get("PENTACLE_GITHUB_REPOSITORY") != repository:
        raise GateError("configured API repository does not match origin")
    workflow_path, workflow_name = WORKFLOWS[repository]
    return repository, workflow_path, workflow_name


def _branch_tip(branch: str) -> str:
    if not branch or branch == "main" or _command(["git", "check-ref-format", "--branch", branch]).returncode:
        raise GateError("workflow run does not name a valid non-main branch")
    ref = f"refs/heads/{branch}"
    rows = _git("ls-remote", "--exit-code", "origin", ref).splitlines()
    fields = rows[0].split() if len(rows) == 1 else []
    if len(fields) != 2 or fields[1] != ref:
        raise GateError("workflow branch readback is ambiguous")
    return _sha(fields[0], "workflow branch tip")


def _public_workflow(candidate: str, path: str) -> None:
    result = _command(["git", "show", f"{candidate}:{path}"])
    if result.returncode:
        raise GateError("public candidate is missing the mapped workflow")
    actual = hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
    if actual != PUBLIC_WORKFLOW_SHA256:
        raise GateError("public candidate workflow commands differ from the audited contract")


def _api_object(path: str, context: str) -> dict[str, object]:
    raw = _require(_command(["gh", "api", path]), context)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise GateError(f"{context}: invalid JSON") from exc
    if not isinstance(value, dict):
        raise GateError(f"{context}: expected an object")
    return value


def _public_jobs(repository: str, run_id: int, attempt: object, candidate: str) -> None:
    if type(attempt) is not int or attempt < 1:
        raise GateError("public workflow run is missing a valid attempt")
    payload = _api_object(
        f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
        f"read public workflow jobs for run {run_id}",
    )
    jobs = payload.get("jobs")
    if type(payload.get("total_count")) is not int or payload["total_count"] != 1 or not isinstance(jobs, list) or len(jobs) != 1:
        raise GateError("public workflow job coverage is incomplete or ambiguous")
    job = jobs[0]
    if not isinstance(job, dict) or job.get("name") != "checks" or job.get("head_sha") != candidate:
        raise GateError("public checks job is not bound to the candidate")
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise GateError("public checks job is not a completed success")
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise GateError("public checks job has no step evidence")
    for name in PUBLIC_REQUIRED_STEPS:
        matches = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
        if len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success":
            raise GateError(f"public checks run lacks successful required coverage: {name}")


def _run_evidence(run_id: int, candidate: str) -> tuple[int, dict[str, object]]:
    repository, workflow_path, workflow_name = _repository_contract()
    run = _api_object(f"repos/{repository}/actions/runs/{run_id}", f"read workflow run {run_id}")
    if type(run.get("id")) is not int or run["id"] != run_id:
        raise GateError(f"workflow run {run_id} returned a different run ID")
    if any(not isinstance(run.get(key), dict) or run[key].get("full_name") != repository
           for key in ("repository", "head_repository")):
        raise GateError(f"workflow run {run_id} belongs to a different repository")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise GateError(f"workflow run {run_id} is not a completed success")
    if run.get("event") != "push" or run.get("head_sha") != candidate:
        raise GateError(f"workflow run {run_id} is not a successful branch-push run for {candidate}")
    branch = run.get("head_branch")
    if not isinstance(branch, str) or _branch_tip(branch) != candidate:
        raise GateError(f"workflow run {run_id} branch no longer resolves to {candidate}")
    workflow_id = run.get("workflow_id")
    if type(workflow_id) is not int or run.get("html_url") != f"https://github.com/{repository}/actions/runs/{run_id}":
        raise GateError(f"workflow run {run_id} is missing immutable evidence fields")
    workflow = _api_object(
        f"repos/{repository}/actions/workflows/{workflow_id}", f"read workflow {workflow_id}"
    )
    if workflow.get("id") != workflow_id or workflow.get("name") != workflow_name or workflow.get("path") != workflow_path:
        raise GateError(f"workflow run {run_id} is not {workflow_name}")
    if repository == "HJK6/pentacle":
        _public_workflow(candidate, workflow_path)
        _public_jobs(repository, run_id, run.get("run_attempt"), candidate)
    return workflow_id, run


def verify_tag(candidate: str) -> dict[str, str]:
    """Fail closed unless ``candidate`` has a matching remote annotated v2 tag."""
    candidate = _sha(candidate, "candidate")
    tag = _tag_name(candidate)
    remote = {ref: sha for sha, ref in (line.split(None, 1) for line in _git(
        "ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"
    ).splitlines())}
    if remote.get(f"refs/tags/{tag}^{{}}") != candidate:
        raise GateError(f"origin {tag} is not an annotated tag for {candidate}")
    if _command(["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"]).returncode:
        _git("fetch", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
    elif _git("rev-parse", tag) != remote[f"refs/tags/{tag}"]:
        raise GateError(f"local {tag} differs from origin")
    if _git("rev-parse", f"{tag}^{{commit}}") != candidate:
        raise GateError(f"{tag} does not point at candidate {candidate}")
    fields = _verify_annotation(_tag_contents(tag))
    if fields["headSha"] != candidate or fields["candidate_sha"] != candidate:
        raise GateError(f"{tag} annotation does not bind candidate {candidate}")
    return fields


def promote(candidate: str, run_id: int) -> dict[str, object]:
    """Tag verified exact-head smoke evidence, then CAS fast-forward main."""
    candidate = _sha(candidate, "candidate")
    _git("fetch", "origin", "main", "--tags")
    if _git("rev-parse", f"{candidate}^{{commit}}") != candidate:
        raise GateError(f"candidate {candidate} is not a local commit")
    old_main = _git("rev-parse", "origin/main")
    if _command(["git", "merge-base", "--is-ancestor", old_main, candidate]).returncode:
        raise GateError("candidate is below the merged-lane floor or would not fast-forward origin/main")
    workflow_id, run = _run_evidence(run_id, candidate)
    annotation = _tag_annotation(candidate, old_main, workflow_id, run)
    fields = _verify_annotation(annotation)
    tag = _tag_name(candidate)
    if _command(["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"]).returncode:
        _git("tag", "-a", tag, candidate, "-m", annotation)
    else:
        existing = _verify_annotation(_tag_contents(tag))
        if {key: existing[key] for key in fields if key != "timestamp"} != {
            key: fields[key] for key in fields if key != "timestamp"
        }:
            raise GateError(f"refusing conflicting existing tag {tag}")
    tag_ref = f"refs/tags/{tag}"
    local_object = _sha(_git("rev-parse", f"{tag_ref}^{{tag}}"), f"local {tag} object")
    if _git("rev-parse", f"{tag_ref}^{{commit}}") != candidate:
        raise GateError(f"local {tag} does not peel to candidate {candidate}")
    remote_tag = _remote_tag_identity(tag)
    if remote_tag is None:
        _git("push", "origin", tag_ref)
        remote_tag = _remote_tag_identity(tag)
    if remote_tag != (local_object, candidate):
        raise GateError(f"origin {tag} differs from the validated local annotated tag")
    if _remote_main() != old_main:
        raise GateError("origin/main moved before the fast-forward CAS")
    _git("push", f"--force-with-lease=refs/heads/main:{old_main}", "origin", f"{candidate}:refs/heads/main")
    if _remote_main() != candidate:
        raise GateError("origin/main did not resolve to the promoted candidate")
    return {"candidate": candidate, "old_main": old_main, "run_id": run_id, "tag": tag}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    promote_parser = commands.add_parser("promote", help="guardedly fast-forward origin/main")
    promote_parser.add_argument("--candidate", required=True)
    promote_parser.add_argument("--run-id", type=int, required=True)
    verify_parser = commands.add_parser("verify-tag", help="verify a gate-passed candidate tag")
    verify_parser.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
    try:
        result = promote(args.candidate, args.run_id) if args.command == "promote" else verify_tag(args.candidate)
    except GateError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
