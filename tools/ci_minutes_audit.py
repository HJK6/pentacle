#!/usr/bin/env python3
"""Read-only audit of GitHub Actions billed minutes across the account's repos.

Reconstructs hosted-runner billing from the jobs API (GitHub does not expose a
per-run billed-minutes field for private repos), classifies hosted vs
self-hosted work by runner labels, and reports two frozen process metrics for
the pentacle ``chat-stream-v2-smoke`` gate (executed push-head failure rate and
the branch cohort).

Design contract:

* ``--since`` / ``--until`` are inclusive-start / exclusive-end UTC bounds.
* Repo inventory is the *full* private-repo set (never filtered by pushedAt);
  a repo that ran CI in the window but was not pushed recently is still counted.
* Runs are paginated and every attempt is billed (``?filter=all`` on the jobs
  endpoint returns one job row per attempt, so reruns are counted, not merged).
* Billing per job is ``ceil(seconds / 60)`` over a completed job's wall time;
  incomplete jobs (missing timestamps or non-terminal status) are never billed
  and are reported separately.
* Every ``gh`` invocation is fail-closed: a non-zero exit, missing binary, or
  unparseable payload aborts the run with a non-zero exit code. A partial audit
  is never printed as if it were complete.

This tool only ever reads (``gh api`` GET). It performs no writes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

# Configure the repository smoke workflow ID to keep the metric stable across
# workflow-file renames. The default is a synthetic example workflow ID.
SMOKE_WORKFLOW_ID = int(os.environ.get("PENTACLE_SMOKE_WORKFLOW_ID", "1"))
SMOKE_GATE_STEP = "Run complete v2 merge gate"

# A push whose head_branch matches one of these prefixes is a tag push, not a
# branch push. These are the promotion / deploy tags minted by
# tools/merge_gate.py promote (v2-gate/<sha>) and deploy-mac.sh
# (deploy/rollback-desktop-*); this matches the 2026-09-07 audit methodology so
# the metric reproduces the recorded numbers. After the tier-1 trigger fix tags
# no longer start smoke runs, so future windows contain no tag pushes at all.
TAG_REF_PREFIXES = ("v2-gate/", "deploy/")

# Below this many executed attempts the failure-rate metric is not reported as a
# number; the cohort is too small to be meaningful.
INSUFFICIENT_SAMPLE = 20

GhRunner = Callable[[Sequence[str]], list[str]]


class AuditError(RuntimeError):
    """Any failure that must abort the audit with a non-zero exit (fail-closed)."""


# --------------------------------------------------------------------------- #
# Pure classification / billing helpers (unit-tested without any I/O).
# --------------------------------------------------------------------------- #


def classify_runner(labels: Iterable[str]) -> str:
    """Return "hosted", "self_hosted", or "unknown" from a job's runs-on labels.

    GitHub-hosted jobs request an OS label (ubuntu*/windows*/macos*) and never
    carry the ``self-hosted`` label; self-hosted jobs always carry it.
    """
    low = [str(label).lower() for label in labels]
    if "self-hosted" in low:
        return "self_hosted"
    if any(label.startswith(("ubuntu", "windows", "macos")) for label in low):
        return "hosted"
    return "unknown"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def billed_minutes(job: dict) -> int | None:
    """Billed minutes for a completed job, or None if the job is incomplete.

    A job is billable only when it has both a start and completion timestamp and
    reports ``status == "completed"``; billing is ceil(wall-seconds / 60).
    """
    if job.get("status") != "completed":
        return None
    started = _parse_ts(job.get("started_at"))
    completed = _parse_ts(job.get("completed_at"))
    if started is None or completed is None:
        return None
    seconds = (completed - started).total_seconds()
    if seconds <= 0:
        return 0
    return math.ceil(seconds / 60)


def _step_conclusion(job: dict, step_name: str) -> str | None:
    for step in job.get("steps") or []:
        if step.get("name") == step_name:
            return step.get("conclusion")
    return None


def is_tag_ref(head_branch: str | None) -> bool:
    if not head_branch:
        return True
    return head_branch.startswith(TAG_REF_PREFIXES)


@dataclass
class JobRecord:
    """One billed job execution (a single attempt of a single job)."""

    repo: str
    workflow_id: int | None
    workflow_path: str
    event: str
    head_branch: str | None
    run_id: int
    run_attempt: int
    job: dict

    @property
    def runner_class(self) -> str:
        return classify_runner(self.job.get("labels") or [])

    @property
    def minutes(self) -> int | None:
        return billed_minutes(self.job)


@dataclass
class BillingTotals:
    hosted_minutes: int = 0
    self_hosted_minutes: int = 0
    unknown_minutes: int = 0
    hosted_jobs: int = 0
    self_hosted_jobs: int = 0
    unknown_jobs: int = 0
    incomplete_jobs: int = 0
    # (repo, workflow_path) -> [hosted_minutes, hosted_jobs]
    by_workflow: dict[tuple[str, str], list[int]] = field(default_factory=dict)

    def add(self, rec: JobRecord) -> None:
        minutes = rec.minutes
        if minutes is None:
            self.incomplete_jobs += 1
            return
        klass = rec.runner_class
        if klass == "hosted":
            self.hosted_minutes += minutes
            self.hosted_jobs += 1
            key = (rec.repo, rec.workflow_path)
            slot = self.by_workflow.setdefault(key, [0, 0])
            slot[0] += minutes
            slot[1] += 1
        elif klass == "self_hosted":
            self.self_hosted_minutes += minutes
            self.self_hosted_jobs += 1
        else:
            self.unknown_minutes += minutes
            self.unknown_jobs += 1


def aggregate(records: Iterable[JobRecord]) -> BillingTotals:
    totals = BillingTotals()
    for rec in records:
        totals.add(rec)
    return totals


def smoke_cohort_metrics(
    records: Iterable[JobRecord],
    *,
    workflow_id: int = SMOKE_WORKFLOW_ID,
    gate_step: str = SMOKE_GATE_STEP,
) -> dict:
    """Frozen process metrics for the smoke gate.

    Executed push-head attempts = smoke-workflow job attempts where the run was a
    branch push (event == push, ref not a tag, ref not main) and the merge-gate
    step reached a conclusion (i.e. the gate actually ran; cancelled-before-gate
    attempts are excluded). Failure rate = failed / completed executed attempts.
    Branch cohort = branches with >= 1 executed attempt.
    """
    per_branch: dict[str, int] = {}
    completed = 0
    failed = 0
    for rec in records:
        if rec.workflow_id != workflow_id:
            continue
        if rec.event != "push":
            continue
        if rec.head_branch == "main" or is_tag_ref(rec.head_branch):
            continue
        conclusion = _step_conclusion(rec.job, gate_step)
        if conclusion is None:
            continue  # gate never ran (cancelled before the step); not executed
        per_branch[rec.head_branch] = per_branch.get(rec.head_branch, 0) + 1
        # "reached a conclusion" == completed; success or failure are terminal.
        if conclusion in ("success", "failure"):
            completed += 1
            if conclusion == "failure":
                failed += 1

    executed = sum(per_branch.values())
    counts = sorted(per_branch.values())
    metrics: dict = {
        "executed_attempts": executed,
        "completed_attempts": completed,
        "failed_attempts": failed,
        "branch_cohort_size": len(per_branch),
        "median_attempts_per_branch": statistics.median(counts) if counts else 0,
        "per_branch_counts": dict(sorted(per_branch.items())),
        "sufficient_sample": executed >= INSUFFICIENT_SAMPLE,
    }
    if completed and executed >= INSUFFICIENT_SAMPLE:
        metrics["failure_rate"] = failed / completed
    else:
        metrics["failure_rate"] = None
    return metrics


# --------------------------------------------------------------------------- #
# I/O layer: gh subprocess, fail-closed. Injected as a callable for tests.
# --------------------------------------------------------------------------- #


# Transient failure signatures worth a bounded retry: flaky network / gateway
# errors, not application errors (a 404/422/403 fails fast and closed).
_TRANSIENT_SIGNATURES = (
    "network is unreachable",
    "dial tcp",
    "connection reset",
    "connection refused",
    "tls handshake",
    "i/o timeout",
    "timeout",
    "timed out",
    "no such host",
    "unexpected eof",
    "502",
    "503",
    "504",
)


def _is_transient(message: str) -> bool:
    low = message.lower()
    return any(sig in low for sig in _TRANSIENT_SIGNATURES)


def _default_gh_runner(
    args: Sequence[str], *, attempts: int = 4, base_backoff: float = 1.0
) -> list[str]:
    """Run ``gh <args>`` and return stdout lines; fail closed on any error.

    A transient network/gateway failure is retried with exponential backoff up
    to ``attempts`` times; any other non-zero exit fails immediately. Once the
    retries are exhausted the audit still aborts (fail-closed) — a partial audit
    is never returned as if complete.
    """
    if shutil.which("gh") is None:
        raise AuditError("gh CLI not found on PATH")
    last_error = ""
    for attempt in range(attempts):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
        if proc.returncode == 0:
            return [line for line in proc.stdout.splitlines() if line.strip()]
        last_error = proc.stderr.strip() or proc.stdout.strip()
        if attempt < attempts - 1 and _is_transient(last_error):
            time.sleep(base_backoff * (2 ** attempt))
            continue
        break
    raise AuditError(f"gh {' '.join(args)} failed: {last_error}")


def _parse_lines(lines: Iterable[str]) -> list[dict]:
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:  # fail closed on garbled payloads
            raise AuditError(f"unparseable gh output line: {exc}") from exc
    return out


def list_private_repos(gh: GhRunner) -> list[str]:
    """Full private-repo inventory for the authenticated account.

    Uses the paginated repos endpoint (not ``gh repo list``'s pushedAt-sorted,
    truncatable view) so no repo that billed minutes in the window is missed.
    """
    lines = gh([
        "api",
        "--paginate",
        "user/repos?per_page=100&affiliation=owner,organization_member",
        "--jq",
        ".[] | select(.private) | .full_name",
    ])
    # gh --jq prints string results raw (unquoted), so the lines are the repo
    # names themselves — not JSON to parse.
    return [line.strip() for line in lines if line.strip()]


def list_runs(gh: GhRunner, repo: str, since: datetime, until: datetime) -> list[dict]:
    created = f"{since.date().isoformat()}..{until.date().isoformat()}"
    lines = gh([
        "api",
        "--paginate",
        f"repos/{repo}/actions/runs?per_page=100&created={created}",
        "--jq",
        ".workflow_runs[] | {id, name, path, event, status, conclusion, "
        "run_started_at, head_branch, run_attempt, workflow_id}",
    ])
    runs = _parse_lines(lines)
    # Server-side `created` is date-granular; enforce the exact UTC window here.
    kept = []
    for run in runs:
        started = _parse_ts(run.get("run_started_at"))
        if started is None:
            continue
        if since <= started < until:
            kept.append(run)
    return kept


def list_jobs(gh: GhRunner, repo: str, run_id: int) -> list[dict]:
    lines = gh([
        "api",
        "--paginate",
        f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&filter=all",
        "--jq",
        ".jobs[] | {name, status, conclusion, started_at, completed_at, "
        "labels, run_attempt, steps: [.steps[] | {name, conclusion}]}",
    ])
    return _parse_lines(lines)


def collect(
    gh: GhRunner,
    repos: Sequence[str],
    since: datetime,
    until: datetime,
    *,
    max_workers: int = 12,
) -> list[JobRecord]:
    """Fetch every billed job attempt across ``repos`` within the window.

    Runs and jobs are fetched concurrently (one gh call per run is the dominant
    cost). ``ThreadPoolExecutor.map`` preserves input order, so the result is
    deterministic, and re-raises the first worker exception — an ``AuditError``
    from any gh call aborts the whole audit (fail-closed).
    """
    import concurrent.futures as cf

    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        per_repo_runs = list(ex.map(lambda r: list_runs(gh, r, since, until), repos))
    run_index: list[tuple[str, dict]] = [
        (repo, run) for repo, runs in zip(repos, per_repo_runs) for run in runs
    ]

    def _fetch(item: tuple[str, dict]) -> tuple[str, dict, list[dict]]:
        repo, run = item
        return repo, run, list_jobs(gh, repo, run["id"])

    records: list[JobRecord] = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for repo, run, jobs in ex.map(_fetch, run_index):
            for job in jobs:
                records.append(
                    JobRecord(
                        repo=repo,
                        workflow_id=run.get("workflow_id"),
                        workflow_path=run.get("path", ""),
                        event=run.get("event", ""),
                        head_branch=run.get("head_branch"),
                        run_id=run["id"],
                        run_attempt=job.get("run_attempt", run.get("run_attempt", 1)),
                        job=job,
                    )
                )
    return records


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def build_report(totals: BillingTotals, cohort: dict, since: datetime, until: datetime) -> dict:
    workflows = [
        {
            "repo": repo,
            "workflow": path,
            "hosted_minutes": mins,
            "hosted_jobs": jobs,
        }
        for (repo, path), (mins, jobs) in sorted(
            totals.by_workflow.items(), key=lambda kv: -kv[1][0]
        )
    ]
    return {
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "hosted_minutes": totals.hosted_minutes,
        "self_hosted_minutes": totals.self_hosted_minutes,
        "unknown_minutes": totals.unknown_minutes,
        "hosted_jobs": totals.hosted_jobs,
        "self_hosted_jobs": totals.self_hosted_jobs,
        "unknown_jobs": totals.unknown_jobs,
        "incomplete_jobs": totals.incomplete_jobs,
        "by_workflow": workflows,
        "smoke_process_metrics": cohort,
    }


def render_text(report: dict) -> str:
    w = report["window"]
    lines = [
        f"GitHub Actions minutes audit  {w['since']} .. {w['until']}",
        "",
        f"  hosted (billed)   : {report['hosted_minutes']:>7} min  "
        f"({report['hosted_jobs']} jobs)",
        f"  self-hosted (free): {report['self_hosted_minutes']:>7} min  "
        f"({report['self_hosted_jobs']} jobs)",
    ]
    if report["unknown_jobs"]:
        lines.append(
            f"  unknown runner    : {report['unknown_minutes']:>7} min  "
            f"({report['unknown_jobs']} jobs)"
        )
    if report["incomplete_jobs"]:
        lines.append(f"  incomplete jobs   : {report['incomplete_jobs']} (not billed)")
    lines += ["", "  Hosted minutes by workflow:"]
    for row in report["by_workflow"]:
        lines.append(
            f"    {row['hosted_minutes']:>6} min  {row['hosted_jobs']:>4} jobs  "
            f"{row['repo']} / {row['workflow'].split('/')[-1]}"
        )
    m = report["smoke_process_metrics"]
    lines += [
        "",
        "  smoke gate process metrics (executed push-head attempts):",
        f"    executed attempts     : {m['executed_attempts']}",
        f"    completed attempts    : {m['completed_attempts']}",
        f"    failed attempts       : {m['failed_attempts']}",
        f"    branch cohort         : {m['branch_cohort_size']} branches",
        f"    median attempts/branch: {m['median_attempts_per_branch']}",
    ]
    if m["failure_rate"] is None:
        lines.append(
            f"    failure rate          : insufficient sample "
            f"(< {INSUFFICIENT_SAMPLE} executed attempts)"
        )
    else:
        lines.append(f"    failure rate          : {m['failure_rate'] * 100:.1f}%")
    return "\n".join(lines)


def _parse_bound(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run_audit(
    since: datetime,
    until: datetime,
    *,
    gh: GhRunner | None = None,
    repos: Sequence[str] | None = None,
) -> dict:
    gh = gh or _default_gh_runner  # late-bound: module attr, monkeypatchable
    repo_list = list(repos) if repos is not None else list_private_repos(gh)
    records = collect(gh, repo_list, since, until)
    totals = aggregate(records)
    cohort = smoke_cohort_metrics(records)
    return build_report(totals, cohort, since, until)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True, help="UTC start (inclusive), ISO 8601")
    parser.add_argument("--until", required=True, help="UTC end (exclusive), ISO 8601")
    parser.add_argument(
        "--repo",
        action="append",
        help="Limit to this repo (owner/name); repeatable. Default: all private repos.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args(argv)

    try:
        since = _parse_bound(args.since)
        until = _parse_bound(args.until)
        if until <= since:
            raise AuditError("--until must be after --since")
        report = run_audit(since, until, repos=args.repo)
    except AuditError as exc:
        print(f"ci_minutes_audit: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
