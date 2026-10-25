#!/usr/bin/env python3
"""Verify existing legacy gate tags; promotion now uses the protected pull-request route."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RETIRED_PROMOTE_MESSAGE = (
    "promote is retired. Open a pull request and have the release owner merge "
    "the reviewed head after required checks; see docs/repo_workflow.md."
)


class GateError(RuntimeError):
    """A promotion or tag evidence precondition was not met."""


def _command(args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=REPO_ROOT, input=input_text, text=True, capture_output=True, check=False, timeout=timeout
    )


def _require(result: subprocess.CompletedProcess[str], context: str) -> str:
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise GateError(f"{context}: {detail}")
    return result.stdout.strip()


def _git(*args: str, timeout: float | None = None) -> str:
    result = _command(["git", *args], timeout=timeout) if timeout is not None else _command(["git", *args])
    return _require(result, "git " + " ".join(args))


def _sha(value: str, label: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise GateError(f"{label} must be an exact lowercase 40-character SHA")
    return value


def _tag_name(candidate: str) -> str:
    return f"v2-gate/{candidate}"


def _verify_annotation(annotation: str) -> dict[str, str]:
    checker = REPO_ROOT / "tools" / "check_governance_tripwires.py"
    _require(
        _command([sys.executable, str(checker), "--kind", "v2-gate-tag", "-"], input_text=annotation),
        "v2-gate tag schema",
    )
    return dict(line.split(": ", 1) for line in annotation.strip().splitlines())


def _tag_contents(tag: str) -> str:
    return _git("for-each-ref", "--format=%(contents)", f"refs/tags/{tag}")


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


def promote(candidate: str, run_id: int, **_retired_options) -> None:
    """Refuse before candidate validation or any external command."""
    raise GateError(RETIRED_PROMOTE_MESSAGE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    promote_parser = commands.add_parser("promote", help="retired; use a protected pull request")
    promote_parser.add_argument("--candidate", required=True)
    promote_parser.add_argument("--run-id", type=int, required=True)
    # Accept the old spelling so old callers receive the retirement explanation.
    promote_parser.add_argument("--checks-timeout-seconds", type=float, help=argparse.SUPPRESS)
    verify_parser = commands.add_parser("verify-tag", help="verify an existing legacy candidate tag")
    verify_parser.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
    try:
        result = promote(args.candidate, args.run_id, checks_timeout_s=args.checks_timeout_seconds) if args.command == "promote" else verify_tag(args.candidate)
    except GateError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
