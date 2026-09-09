#!/usr/bin/env python3
"""The sole guarded promotion path for a chat-stream-v2 candidate."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = os.environ.get("PENTACLE_GITHUB_REPOSITORY", "example-org/pentacle")
WORKFLOW_PATH = ".github/workflows/chat-stream-v2-smoke.yml"
WORKFLOW_NAME = "chat-stream-v2-smoke"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

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


def _run_evidence(run_id: int, candidate: str) -> tuple[int, dict[str, object]]:
    run = json.loads(_require(
        _command(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{run_id}"]),
        f"read workflow run {run_id}",
    ))
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise GateError(f"workflow run {run_id} is not a completed success")
    if run.get("event") != "push" or run.get("head_sha") != candidate:
        raise GateError(f"workflow run {run_id} is not a successful branch-push run for {candidate}")
    workflow_id = run.get("workflow_id")
    if not isinstance(workflow_id, int) or not isinstance(run.get("id"), int) or not isinstance(run.get("html_url"), str):
        raise GateError(f"workflow run {run_id} is missing immutable evidence fields")
    workflow = json.loads(_require(
        _command(["gh", "api", f"repos/{REPOSITORY}/actions/workflows/{workflow_id}"]),
        f"read workflow {workflow_id}",
    ))
    if workflow.get("name") != WORKFLOW_NAME or workflow.get("path") != WORKFLOW_PATH:
        raise GateError(f"workflow run {run_id} is not {WORKFLOW_NAME}")
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
    _git("push", "origin", f"refs/tags/{tag}")
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
