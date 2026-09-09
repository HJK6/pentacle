"""Recorded-fixture tests for tools/ci_minutes_audit.py.

Covers classification, billing (incl. rounding + incomplete jobs), rerun
attempts, the frozen smoke process-metric cohort, paginated multi-repo
collection, and fail-closed behaviour on a gh API error.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "tools" / "ci_minutes_audit.py"
_spec = importlib.util.spec_from_file_location("ci_minutes_audit", MODULE_PATH)
assert _spec and _spec.loader
audit = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass can resolve the module via sys.modules.
sys.modules[_spec.name] = audit
_spec.loader.exec_module(audit)


UTC = timezone.utc
SINCE = datetime(2026, 9, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 8, tzinfo=UTC)


def _job(
    *,
    labels,
    started="2026-09-02T00:00:00Z",
    completed="2026-09-02T00:03:30Z",
    status="completed",
    conclusion="success",
    run_attempt=1,
    gate_conclusion="success",
):
    steps = []
    if gate_conclusion is not None:
        steps.append({"name": audit.SMOKE_GATE_STEP, "conclusion": gate_conclusion})
    return {
        "name": "v2 unit + smoke gate",
        "status": status,
        "conclusion": conclusion,
        "started_at": started,
        "completed_at": completed,
        "labels": labels,
        "run_attempt": run_attempt,
        "steps": steps,
    }


def _rec(**kwargs):
    defaults = dict(
        repo="example-org/example-service",
        workflow_id=audit.SMOKE_WORKFLOW_ID,
        workflow_path=".github/workflows/example-smoke.yml",
        event="push",
        head_branch="feature/x",
        run_id=1,
        run_attempt=1,
    )
    job = kwargs.pop("job")
    defaults.update(kwargs)
    return audit.JobRecord(job=job, **defaults)


# --------------------------------------------------------------------------- #
# classification + billing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "labels,expected",
    [
        (["ubuntu-latest"], "hosted"),
        (["ubuntu-24.04"], "hosted"),
        (["macos-14"], "hosted"),
        (["self-hosted", "Linux", "X64", "example-repo-cd"], "self_hosted"),
        (["self-hosted"], "self_hosted"),
        (["ghrunner"], "unknown"),
        ([], "unknown"),
    ],
)
def test_classify_runner(labels, expected):
    assert audit.classify_runner(labels) == expected


def test_billed_minutes_rounds_up():
    # 3m30s -> ceil = 4
    assert audit.billed_minutes(_job(labels=["ubuntu-latest"])) == 4


def test_billed_minutes_exact_minute():
    job = _job(labels=["ubuntu-latest"], started="2026-09-02T00:00:00Z",
               completed="2026-09-02T00:02:00Z")
    assert audit.billed_minutes(job) == 2


def test_billed_minutes_incomplete_returns_none():
    assert audit.billed_minutes(_job(labels=["ubuntu-latest"], completed=None,
                                      status="in_progress")) is None
    assert audit.billed_minutes(_job(labels=["ubuntu-latest"], status="queued",
                                      started=None, completed=None)) is None


def test_billed_minutes_zero_wall_is_zero_not_none():
    job = _job(labels=["ubuntu-latest"], started="2026-09-02T00:00:00Z",
               completed="2026-09-02T00:00:00Z")
    assert audit.billed_minutes(job) == 0


# --------------------------------------------------------------------------- #
# aggregation: mixed hosted / self-hosted / incomplete
# --------------------------------------------------------------------------- #


def test_aggregate_mixed_runner_classes():
    records = [
        _rec(job=_job(labels=["ubuntu-latest"])),  # hosted 4
        _rec(job=_job(labels=["ubuntu-latest"], started="2026-09-02T00:00:00Z",
                      completed="2026-09-02T00:05:00Z")),  # hosted 5
        _rec(
            repo="example-org/example-repo",
            workflow_path=".github/workflows/example.yml",
            job=_job(labels=["self-hosted", "Linux", "X64", "example-repo-cd"],
                     started="2026-09-02T00:00:00Z", completed="2026-09-02T00:10:00Z"),
        ),  # self-hosted 10
        _rec(job=_job(labels=["ubuntu-latest"], status="in_progress", completed=None)),
    ]
    totals = audit.aggregate(records)
    assert totals.hosted_minutes == 9
    assert totals.hosted_jobs == 2
    assert totals.self_hosted_minutes == 10
    assert totals.self_hosted_jobs == 1
    assert totals.incomplete_jobs == 1
    assert totals.by_workflow[("example-org/example-service",
                               ".github/workflows/example-smoke.yml")] == [9, 2]


def test_rerun_attempts_billed_separately():
    # Two attempts of the same run (as filter=all returns) are both billed.
    records = [
        _rec(run_id=42, run_attempt=1,
             job=_job(labels=["ubuntu-latest"], run_attempt=1)),
        _rec(run_id=42, run_attempt=2,
             job=_job(labels=["ubuntu-latest"], run_attempt=2)),
    ]
    totals = audit.aggregate(records)
    assert totals.hosted_jobs == 2
    assert totals.hosted_minutes == 8


# --------------------------------------------------------------------------- #
# smoke process-metric cohort
# --------------------------------------------------------------------------- #


def test_smoke_cohort_excludes_main_tag_and_cancelled_before_gate():
    records = [
        # executed branch attempts
        _rec(head_branch="feat/a", job=_job(labels=["ubuntu-latest"], gate_conclusion="success")),
        _rec(head_branch="feat/a", job=_job(labels=["ubuntu-latest"], gate_conclusion="failure")),
        _rec(head_branch="feat/b", job=_job(labels=["ubuntu-latest"], gate_conclusion="success")),
        # excluded: main
        _rec(head_branch="main", job=_job(labels=["ubuntu-latest"], gate_conclusion="success")),
        # excluded: tag push
        _rec(head_branch="v2-gate/abc", job=_job(labels=["ubuntu-latest"], gate_conclusion="success")),
        # excluded: cancelled before the gate step ran
        _rec(head_branch="feat/c", job=_job(labels=["ubuntu-latest"], gate_conclusion=None,
                                            status="completed", conclusion="cancelled")),
        # excluded: non-push event
        _rec(head_branch="feat/d", event="pull_request",
             job=_job(labels=["ubuntu-latest"], gate_conclusion="success")),
        # excluded: different workflow
        _rec(head_branch="feat/e", workflow_id=999,
             job=_job(labels=["ubuntu-latest"], gate_conclusion="success")),
    ]
    m = audit.smoke_cohort_metrics(records)
    assert m["executed_attempts"] == 3
    assert m["completed_attempts"] == 3
    assert m["failed_attempts"] == 1
    assert m["branch_cohort_size"] == 2  # feat/a, feat/b
    assert m["per_branch_counts"] == {"feat/a": 2, "feat/b": 1}
    assert m["median_attempts_per_branch"] == 1.5
    # below the 20-attempt threshold: no failure rate reported
    assert m["sufficient_sample"] is False
    assert m["failure_rate"] is None


def test_smoke_cohort_failure_rate_with_sufficient_sample():
    records = []
    # 21 executed attempts across 3 branches; 3 failures.
    for i in range(21):
        records.append(_rec(
            head_branch=f"feat/{i % 3}",
            run_id=i,
            job=_job(labels=["ubuntu-latest"],
                     gate_conclusion="failure" if i < 3 else "success"),
        ))
    m = audit.smoke_cohort_metrics(records)
    assert m["executed_attempts"] == 21
    assert m["completed_attempts"] == 21
    assert m["failed_attempts"] == 3
    assert m["sufficient_sample"] is True
    assert m["failure_rate"] == pytest.approx(3 / 21)


# --------------------------------------------------------------------------- #
# I/O: paginated multi-repo collection + fail-closed
# --------------------------------------------------------------------------- #


class FakeGh:
    """Replays recorded gh --jq NDJSON output keyed by a substring of the path."""

    def __init__(self, responses: dict[str, list]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, args):
        path = next(a for a in args if a.startswith(("repos/", "user/")))
        self.calls.append(path)
        # Match the most specific (longest) key first: the runs path is a prefix
        # of the jobs path, so a plain "in" check would mis-route jobs to runs.
        for key in sorted(self.responses, key=len, reverse=True):
            if key in path:
                # gh --jq prints scalar strings raw and objects as JSON.
                return [
                    item if isinstance(item, str) else json.dumps(item)
                    for item in self.responses[key]
                ]
        return []


def test_run_audit_paginated_multi_repo():
    run_smoke = {
        "id": 1, "name": "chat-stream-v2-smoke",
        "path": ".github/workflows/example-smoke.yml",
        "event": "push", "status": "completed", "conclusion": "success",
        "run_started_at": "2026-09-02T00:00:00Z", "head_branch": "feat/a",
        "run_attempt": 1, "workflow_id": audit.SMOKE_WORKFLOW_ID,
    }
    run_deploy = {
        "id": 2, "name": "deploy", "path": ".github/workflows/example.yml",
        "event": "push", "status": "completed", "conclusion": "success",
        "run_started_at": "2026-09-03T00:00:00Z", "head_branch": "master",
        "run_attempt": 1, "workflow_id": 111,
    }
    # A run outside the window must be dropped by the exact-window filter.
    run_old = dict(run_smoke, id=3, run_started_at="2026-08-30T00:00:00Z")

    responses = {
        "user/repos": ["example-org/example-service", "example-org/example-repo"],
        "repos/example-org/example-service/actions/runs": [run_smoke, run_old],
        "repos/example-org/example-repo/actions/runs": [run_deploy],
        "repos/example-org/example-service/actions/runs/1/jobs": [
            _job(labels=["ubuntu-latest"], run_attempt=1),
            _job(labels=["ubuntu-latest"], run_attempt=2,
                 started="2026-09-02T00:10:00Z", completed="2026-09-02T00:15:00Z"),
        ],
        "repos/example-org/example-service/actions/runs/3/jobs": [_job(labels=["ubuntu-latest"])],
        "repos/example-org/example-repo/actions/runs/2/jobs": [
            _job(labels=["self-hosted", "Linux", "X64", "example-repo-cd"],
                 started="2026-09-03T00:00:00Z", completed="2026-09-03T00:08:00Z"),
        ],
    }
    gh = FakeGh(responses)
    report = audit.run_audit(SINCE, UNTIL, gh=gh)

    # run_old (id 3) is outside the window -> its jobs are never fetched.
    assert not any("actions/runs/3/jobs" in c for c in gh.calls)
    # hosted = run 1 attempt1 (4) + attempt2 (5) = 9; self-hosted = 8
    assert report["hosted_minutes"] == 9
    assert report["self_hosted_minutes"] == 8
    assert report["hosted_jobs"] == 2
    assert report["self_hosted_jobs"] == 1
    # both attempts of run 1 counted as executed smoke attempts on feat/a
    assert report["smoke_process_metrics"]["executed_attempts"] == 2
    assert report["smoke_process_metrics"]["per_branch_counts"] == {"feat/a": 2}


def test_gh_api_error_is_fail_closed(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "gh: HTTP 403 rate limit"

    monkeypatch.setattr(audit.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(audit.AuditError):
        audit._default_gh_runner(["api", "user/repos"])


def test_main_returns_nonzero_on_api_error(monkeypatch):
    def _boom(_args):
        raise audit.AuditError("gh exploded")

    monkeypatch.setattr(audit, "_default_gh_runner", _boom)
    rc = main_rc = audit.main(
        ["--since", "2026-09-01T00:00Z", "--until", "2026-09-08T00:00Z"]
    )
    assert rc == 2


def test_gh_runner_retries_transient_then_succeeds(monkeypatch):
    calls = {"n": 0}

    class _Proc:
        def __init__(self, rc, out="", err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    def _fake_run(*_a, **_k):
        calls["n"] += 1
        if calls["n"] < 3:  # two transient failures, then success
            return _Proc(1, err="dial tcp 10.0.0.0:443: connect: network is unreachable")
        return _Proc(0, out='"example-org/example-service"\n')

    monkeypatch.setattr(audit.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(audit.subprocess, "run", _fake_run)
    monkeypatch.setattr(audit.time, "sleep", lambda _s: None)
    out = audit._default_gh_runner(["api", "user/repos"], base_backoff=0)
    assert out == ['"example-org/example-service"']
    assert calls["n"] == 3


def test_gh_runner_fails_fast_on_non_transient(monkeypatch):
    calls = {"n": 0}

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "gh: Not Found (HTTP 404)"

    def _fake_run(*_a, **_k):
        calls["n"] += 1
        return _Proc()

    monkeypatch.setattr(audit.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(audit.subprocess, "run", _fake_run)
    monkeypatch.setattr(audit.time, "sleep", lambda _s: None)
    with pytest.raises(audit.AuditError):
        audit._default_gh_runner(["api", "repos/x/y/actions/runs"])
    assert calls["n"] == 1  # no retry on a 404


def test_main_rejects_inverted_window(capsys):
    rc = audit.main(["--since", "2026-09-08T00:00Z", "--until", "2026-09-01T00:00Z"])
    assert rc == 2
    assert "until must be after" in capsys.readouterr().err
