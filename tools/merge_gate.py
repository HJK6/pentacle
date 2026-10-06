#!/usr/bin/env python3
"""The sole guarded promotion path for a chat-stream-v2 candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import os
import re
import shlex
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
PUBLIC_WORKFLOW_SHA256 = "401ac4338e921171d7037966d1b671b8feb42722cff395952bc7034e60767104"
WORKFLOWS = {
    "HJK6/pentacle": (".github/workflows/predeploy-tests.yml", "Public checks"),
    "HJK6/pentacle-private": (".github/workflows/chat-stream-v2-smoke.yml", "chat-stream-v2-smoke"),
}
PUBLIC_REQUIRED_STEPS = frozenset({
    "Run npm test",
    "Run python3 scripts/test_check_public_residue.py",
    "Run python3 scripts/check_public_residue.py",
    "Source integrity after Chrome install",
    "Portable microphone contracts", "Bounded dashboard dependency",
    "Web mode E2E gate",
    "Daemon and CLI checks",
    "Source integrity at gate end",
})

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


def _remote_tag_identity(tag: str, *, timeout: float | None = None) -> tuple[str, str] | None:
    """Return an exact annotated remote tag object and peel, or unambiguous absence."""
    ref = f"refs/tags/{tag}"
    peeled = f"{ref}^{{}}"
    raw = _git("ls-remote", "--tags", "origin", ref, peeled, timeout=timeout)
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


def checkout_tag_proof(log: str, candidate: str, tag: str) -> bool:
    """Parse only the audited checkout step's first fetch/checkout groups."""
    if len(log.encode("utf-8")) > 1024 * 1024:
        return False
    lines = [re.sub(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ", "", line)
             for line in log.splitlines()]
    def first_group(name):
        try:
            start = lines.index("##[group]" + name)
            end = lines.index("##[endgroup]", start + 1)
        except ValueError:
            return None
        return start, end, lines[start + 1:end]
    fetched, checked = first_group("Fetching the repository"), first_group("Checking out the ref")
    if fetched is None or checked is None or fetched[1] >= checked[0]:
        return False
    def commands(rows):
        out = []
        for line in rows:
            if line.startswith("[command]"):
                try: args = shlex.split(line[len("[command]"):])
                except ValueError: return []
                if not args or not re.fullmatch(r"/(?:[A-Za-z0-9._-]+/)*git", args[0]):
                    return []
                out.append(args[1:])
        return out
    fetches = commands(fetched[2])
    checkouts = commands(checked[2])
    if not fetches or checkouts != [["checkout", "--progress", "--force", "refs/tags/" + tag]]:
        return False
    for args in fetches:
        if args[:3] != ["-c", "protocol.version=2", "fetch"]:
            return False
        if args[-2:] != ["origin", "+" + candidate + ":refs/tags/" + tag]:
            return False
        options = args[3:-2]
        if set(options) - {"--no-tags", "--prune", "--no-recurse-submodules", "--progress", "--depth=1"}:
            return False
        if not {"--no-tags", "--prune", "--no-recurse-submodules", "--depth=1"}.issubset(options):
            return False
    return True


def _historical_promotion_runs(repository, workflow_id, candidate, tag, *, deadline, clock):
    """A fresh tag cannot reuse evidence from a deleted/recreated tag name."""
    remaining = deadline - clock()
    if remaining <= 0:
        raise GateError("promotion checks timeout before tag history")
    endpoint = (f"repos/{repository}/actions/workflows/{workflow_id}/runs"
                f"?event=push&head_sha={candidate}&per_page=100&page=1")
    try:
        raw = _require(_command(["gh", "api", endpoint], timeout=remaining), "promotion tag history")
    except subprocess.TimeoutExpired as exc:
        raise GateError("promotion checks timeout during tag history") from exc
    if clock() >= deadline:
        raise GateError("promotion checks timeout during tag history")
    try: payload = json.loads(raw)
    except ValueError as exc: raise GateError("promotion tag history malformed") from exc
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if (not isinstance(runs, list) or type(payload.get("total_count")) is not int
            or payload["total_count"] != len(runs) or any(
                not isinstance(row, dict) or not isinstance(row.get("head_sha"), str)
                or not SHA_RE.fullmatch(row["head_sha"]) or not isinstance(row.get("head_branch"), str)
                or not row["head_branch"].strip() for row in runs)):
        raise GateError("promotion tag history incomplete or ambiguous")
    if any(row.get("head_sha") == candidate and row.get("head_branch") == tag for row in runs):
        raise GateError("promotion tag absent but historical matching runs exist; refusing ambiguous tag recreation")


def wait_for_promotion_checks(candidate: str, tag: str, repository: str, workflow_id: int,
                              *, timeout_s: float = 300, poll_s: float = 2,
                              clock=time.monotonic, sleep=time.sleep, deadline: float | None = None) -> dict[str, object]:
    """Only a stable newest exact-tag attempt may satisfy this absolute budget."""
    if not math.isfinite(timeout_s) or timeout_s <= 0 or not math.isfinite(poll_s) or poll_s <= 0:
        raise GateError("promotion checks require positive finite timeout and poll interval")
    if repository != "HJK6/pentacle":
        raise GateError("promotion tag-ref proof is supported only for the audited public workflow")
    deadline = clock() + timeout_s if deadline is None else deadline
    if not math.isfinite(deadline): raise GateError("promotion checks require finite deadline")
    state = "required_checks_missing"

    def remaining():
        left = deadline - clock()
        if left <= 0:
            raise GateError(f"promotion checks timeout: {state}")
        return left

    def read(endpoint, *, text=False):
        try:
            response = _command(["gh", "api", endpoint], timeout=remaining())
            raw = _require(response, "promotion checks")
            if text: raw = response.stdout
        except subprocess.TimeoutExpired as exc:
            raise GateError(f"promotion checks timeout: {state}") from exc
        remaining()  # A late green response is not evidence within the budget.
        if text: return raw
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise GateError("promotion checks: malformed API response") from exc
        if not isinstance(value, dict):
            raise GateError("promotion checks: expected API object")
        return value

    endpoint = (f"repos/{repository}/actions/workflows/{workflow_id}/runs"
                f"?event=push&head_sha={candidate}&per_page=100")

    high_water = None

    def latest():
        nonlocal high_water
        payload = read(endpoint)
        runs = payload.get("workflow_runs")
        if not isinstance(runs, list) or type(payload.get("total_count")) is not int or payload["total_count"] != len(runs):
            raise GateError("promotion checks: incomplete or ambiguous run coverage")
        matches = []
        seen = set()
        seen_numbers = set()
        for run in runs:
            if not isinstance(run, dict):
                raise GateError("promotion checks: malformed run")
            if (run.get("head_sha"), run.get("head_branch"), run.get("event"), run.get("workflow_id")) != (
                    candidate, tag, "push", workflow_id):
                continue
            if any(not isinstance(run.get(key), dict) or run[key].get("full_name") != repository
                   for key in ("repository", "head_repository")):
                raise GateError("promotion checks: exact-tag run has wrong repository")
            identity = tuple(run.get(key) for key in ("run_number", "run_attempt", "id"))
            if any(type(value) is not int or value < 1 for value in identity):
                raise GateError("promotion checks: missing immutable run identity")
            if run["id"] in seen or run["run_number"] in seen_numbers:
                raise GateError("promotion checks: duplicate run identity")
            seen.add(run["id"])
            seen_numbers.add(run["run_number"])
            if run.get("path") not in {WORKFLOWS[repository][0], WORKFLOWS[repository][0] + "@" + tag, WORKFLOWS[repository][0] + "@refs/tags/" + tag} or run.get("html_url") != f"https://github.com/{repository}/actions/runs/{run['id']}":
                raise GateError("promotion checks: workflow path or URL mismatch")
            matches.append((identity, run))
        if not matches:
            return None
        identity, chosen = max(matches, key=lambda row: row[0])
        if high_water is not None and identity < high_water:
            return None  # An omitted superseding run never revives older green.
        high_water = identity
        return chosen

    def verdict(run):
        if run is None:
            return "required_checks_missing"
        if run.get("status") in {"queued", "in_progress", "waiting", "pending", "requested"}:
            return "still_running"
        if run.get("status") != "completed":
            raise GateError("promotion checks: unknown run status")
        if run.get("conclusion") != "success":
            return "final_red"
        payload = read(f"repos/{repository}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list) or type(payload.get("total_count")) is not int or payload["total_count"] != len(jobs):
            raise GateError("promotion checks: incomplete job coverage")
        if not jobs:
            return "required_checks_missing"
        if repository == "HJK6/pentacle":
            if len(jobs) != 1 or not isinstance(jobs[0], dict) or jobs[0].get("name") != "checks":
                return "required_checks_missing"
        for job in jobs:
            if not isinstance(job, dict) or job.get("head_sha") != candidate:
                raise GateError("promotion checks: job candidate mismatch")
            if job.get("run_id") != run["id"] or ("run_attempt" in job and job["run_attempt"] != run["run_attempt"]):
                raise GateError("promotion checks: job run/attempt mismatch")
            if job.get("status") != "completed":
                return "still_running"
            if job.get("conclusion") != "success":
                return "final_red"
        if repository == "HJK6/pentacle":
            steps = jobs[0].get("steps")
            if not isinstance(steps, list):
                return "required_checks_missing"
            for name in PUBLIC_REQUIRED_STEPS:
                found = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
                if len(found) != 1:
                    return "required_checks_missing"
                if found[0].get("status") != "completed":
                    return "still_running"
                if found[0].get("conclusion") != "success":
                    return "final_red"
        job = jobs[0]
        if type(job.get("id")) is not int or job["id"] < 1:
            raise GateError("promotion checks: missing job identity")
        checkout = [step for step in job["steps"] if isinstance(step, dict) and step.get("name") == "Run actions/checkout@v4"]
        setup = [step for step in job["steps"] if isinstance(step, dict) and step.get("name") == "Set up job"]
        if (len(checkout) != 1 or checkout[0].get("number") != 2
                or checkout[0].get("status") != "completed" or checkout[0].get("conclusion") != "success"
                or len(setup) != 1 or setup[0].get("number") != 1):
            return "required_checks_missing"
        try:
            log = read(f"repos/{repository}/actions/jobs/{job['id']}/steps/1/logs", text=True)
        except GateError as exc:
            if "timeout" in str(exc): raise
            raise GateError("promotion checks required_checks_missing: checkout step logs unavailable") from exc
        if not checkout_tag_proof(log, candidate, tag):
            raise GateError("promotion checks required_checks_missing: exact tag checkout proof absent")
        return "green"

    last_report = None
    while True:
        run = latest()
        state = verdict(run)
        report = (state, None if run is None else run.get("id"), None if run is None else run.get("run_attempt"))
        if report != last_report:
            print(f"promotion checks {state}: candidate={candidate} tag={tag} run={report[1]} attempt={report[2]}", file=sys.stderr)
            last_report = report
        if state in {"green", "final_red"}:
            newest = latest()
            identity = lambda row: None if row is None else tuple(row.get(k) for k in ("id", "run_attempt", "run_number", "status", "conclusion"))
            if identity(newest) != identity(run):
                state = "still_running"
                continue
            if state == "final_red":
                raise GateError(f"promotion checks final_red: run={run['id']} attempt={run['run_attempt']}")
            remaining()
            return {"run_id": run["id"], "run_attempt": run["run_attempt"], "tag": tag,
                    "candidate": candidate, "status": "green", "deadline": deadline}
        sleep(min(poll_s, remaining()))


def promote(candidate: str, run_id: int, *, checks_timeout_s: float = 300,
            poll_s: float = 2, clock=time.monotonic, sleep=time.sleep) -> dict[str, object]:
    """Tag verified exact-head smoke evidence, then CAS fast-forward main."""
    if not math.isfinite(checks_timeout_s) or checks_timeout_s <= 0 or not math.isfinite(poll_s) or poll_s <= 0:
        raise GateError("promotion checks require positive finite timeout and poll interval")
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
    repository, _, _ = _repository_contract()
    checks_deadline = clock() + checks_timeout_s
    if remote_tag is None:
        if repository == "HJK6/pentacle":
            _historical_promotion_runs(repository, workflow_id, candidate, tag, deadline=checks_deadline, clock=clock)
        _git("push", "origin", tag_ref)
        remote_tag = _remote_tag_identity(tag)
    if remote_tag != (local_object, candidate):
        raise GateError(f"origin {tag} differs from the validated local annotated tag")
    check_receipt = None
    if repository == "HJK6/pentacle":
        check_receipt = wait_for_promotion_checks(candidate, tag, repository, workflow_id,
            timeout_s=checks_timeout_s, poll_s=poll_s, clock=clock, sleep=sleep, deadline=checks_deadline)
        remaining = check_receipt["deadline"] - clock()
        if remaining <= 0:
            raise GateError("promotion checks timeout before tag revalidation")
        try:
            final_tag = _remote_tag_identity(tag, timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise GateError("promotion checks timeout during tag revalidation") from exc
        if clock() >= check_receipt["deadline"]:
            raise GateError("promotion checks timeout during tag revalidation")
        if final_tag != (local_object, candidate):
            raise GateError("origin promotion tag changed while checks were pending")
    if _remote_main() != old_main:
        raise GateError("origin/main moved before the fast-forward CAS")
    _git("push", f"--force-with-lease=refs/heads/main:{old_main}", "origin", f"{candidate}:refs/heads/main")
    if _remote_main() != candidate:
        raise GateError("origin/main did not resolve to the promoted candidate")
    result = {"candidate": candidate, "old_main": old_main, "run_id": run_id, "tag": tag}
    if check_receipt is not None:
        result["promotion_checks"] = {key: value for key, value in check_receipt.items() if key != "deadline"}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    promote_parser = commands.add_parser("promote", help="guardedly fast-forward origin/main")
    promote_parser.add_argument("--candidate", required=True)
    promote_parser.add_argument("--run-id", type=int, required=True)
    promote_parser.add_argument("--checks-timeout-seconds", type=float, default=300)
    verify_parser = commands.add_parser("verify-tag", help="verify a gate-passed candidate tag")
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
