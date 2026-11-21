"""Read Public checks metadata. Fetch only GET resources, never job logs."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from . import reliability_artifacts as artifacts

SUCCESS = "success"
WORKFLOW = "predeploy-tests.yml"


def download(repo, route):
    # repo is validated before it can reach gh. Errors can contain authentication
    # or provider diagnostics and are deliberately omitted from the score.
    proc = subprocess.run(
        ["gh", "api", "--method", "GET", f"repos/{repo}/{route}"],
        env={**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False,
    )
    if proc.returncode:
        raise ValueError("GitHub GET unavailable: " + route.split("?")[0])
    return proc.stdout


def api(repo, route):
    return json.loads(download(repo, route))


def first_attempt(repo, run):
    rid = run["id"]
    attempt = int(run["run_attempt"])
    href = f"https://github.com/{repo}/actions/runs/{rid}"
    row = dict(id=rid, sha=run["head_sha"], created=run["created_at"], source=href,
               attempt=attempt, latest=run.get("conclusion"), first=None, failures=[])
    base = f"actions/runs/{rid}/attempts/1"
    try:
        first = api(repo, base) if attempt > 1 else run
        row.update(first=first.get("conclusion") or first["status"], finished=first.get("updated_at"))
        if row["first"] != "failure":
            return row
        jobs = api(repo, base + "/jobs?per_page=100")
        if len(jobs["jobs"]) != jobs["total_count"]:
            raise ValueError("incomplete first-attempt jobs")
        blockers = []
        for job in jobs["jobs"]:
            if job["run_id"] != rid or job["run_attempt"] != 1 or job["head_sha"] != row["sha"]:
                raise ValueError("first-attempt job identity mismatch")
            if job["conclusion"] in ("success", "skipped", "neutral"):
                continue
            steps = [s for s in job.get("steps", []) if s["conclusion"] not in
                     ("success", "skipped", "neutral")]
            aggregate = (job["name"] == "checks" and len(steps) == 1 and
                         steps[0]["name"] == "Require every partition to succeed")
            for step in steps or [{"name": "missing failing step"}]:
                assertion = None
                mirror_timeout = False
                if step["name"] == "System packages" and any(x.startswith("ubuntu-") for x in job["labels"]):
                    annotations = api(repo, f"check-runs/{job['id']}/annotations?per_page=100")
                    if any(a["annotation_level"] == "failure" and
                           re.search(r"(?i)(?:step|action) ['\"]?System packages['\"]? (?:has )?timed out\b", a["message"])
                           for a in annotations):
                        assertion = "System packages timed out"
                    mirror_timeout = any(re.search(r"(?i)(?:archive\.ubuntu\.com|ubuntu\.com/ubuntu|apt-mirrors).*?(?:timed out|timeout)|(?:timed out|timeout).*?archive\.ubuntu\.com",
                                                   a["message"]) for a in annotations)
                blockers.append(dict(job=job["name"], step=step["name"], runner=job["labels"],
                                     source=job["html_url"], assertion=assertion, mirror_timeout=mirror_timeout,
                                     aggregate=aggregate))
        # The required aggregate reflects upstream blockers. On its own its
        # failure is unresolved (a missing or skipped partition can also fail it).
        upstream = [b for b in blockers if not b["aggregate"]]
        row["failures"] = upstream or blockers
        partitions = [b for b in upstream if b["step"] == "Daemon unit partition"]
        if partitions:
            records = api(repo, f"actions/runs/{rid}/artifacts?per_page=100")["artifacts"]
            for failure in partitions:
                for artifact in artifacts.job_artifacts(failure["job"], records, rid):
                    try:
                        fields = artifacts.details(repo, artifact, download)
                    except (ValueError, KeyError, OSError, ParseError, BadZipFile):
                        fields = None
                    if fields:
                        failure.update(fields, artifact_id=artifact["id"])
                        break
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        row["missing"] = f"first-attempt evidence unavailable ({type(exc).__name__}): {href}/attempts/1"
    return row


def live(repo, cutoff):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repo):
        raise ValueError("invalid GitHub repository")
    route = f"actions/workflows/{WORKFLOW}/runs?per_page=100&created=%3C%3D{cutoff}"
    # GitHub paginates newest first. Include a boundary tie even on the next page.
    page, runs = 1, {}
    while True:
        data = api(repo, route + f"&page={page}")["workflow_runs"]
        for run in data:
            if run["name"] != "Public checks":
                raise ValueError("unexpected workflow scope")
            runs[run["id"]] = run
        newest = sorted(runs.values(), key=lambda r: (r["created_at"], r["id"]), reverse=True)
        if len(data) < 100 or (len(newest) > 100 and newest[-1]["created_at"] < newest[99]["created_at"]):
            break
        page += 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda run: first_attempt(repo, run), newest[:100]))
    green = next((r for r in rows if r["first"] == SUCCESS and r.get("finished") and r["finished"] <= cutoff), None)
    if green:
        route = f"actions/runs/{green['id']}/attempts/1/jobs?per_page=100"
        try:
            batch = api(repo, route)
            jobs = batch["jobs"]
            timed = [j for j in jobs if j["conclusion"] != "skipped"]
            if (timed and len(jobs) == batch["total_count"] and
                    all(j["run_attempt"] == 1 and j["run_id"] == green["id"] for j in jobs)):
                green["timing"] = {"started": min(j["started_at"] for j in timed),
                                   "finished": max(j["completed_at"] for j in timed), "source": route}
        except (OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired):
            green["timing"] = {"missing": "first-attempt job timing unavailable", "source": route}
    return rows


def retained(csv_path, runs_path, repo):
    with Path(csv_path).open(newline="") as handle:
        reviewed = {int(r["databaseId"]): r for r in csv.DictReader(handle)}
    runs = json.loads(Path(runs_path).read_text())
    rows = {}
    for run in [*reviewed.values(), *runs]:
        rid = int(run["databaseId"])
        original = reviewed.get(rid)
        attempt = int(run["attempt"])
        first = run["conclusion"] if attempt == 1 else None
        if original:
            first = SUCCESS if original["classification"] == "first_attempt_pass" else "failure"
        row = dict(id=rid, sha=run["headSha"], created=run["createdAt"], attempt=attempt,
                   source=f"https://github.com/{repo}/actions/runs/{rid}", first=first,
                   latest=run["conclusion"], failures=[])
        if original:
            row.update(review_category=original["classification"], review_source=str(csv_path))
        rows[rid] = row
    return list(rows.values())


def classifications(row, decisions, signatures=(), repo_root=None):
    """One decision covers one failed job and step on one SHA and attempt."""
    first = row["first"]
    if first == SUCCESS and not row.get("missing"):
        return "pass", []
    if first != "failure":
        return "incomplete", []
    causes = []
    for failure in row.get("failures", []):
        key = (str(row["id"]), row["sha"], failure["job"], failure["step"])
        override = decisions.get(key)
        if override:
            causes.extend(dict(**c, evidence=failure["source"], job=failure["job"], step=failure["step"],
                               reviewer=override["reviewer"], reason=override["reason"],
                               revision=override["revision"], review_source=override["source"])
                          for c in override["causes"])
            def test_key(name):
                return name.replace(".py::", "::").replace("/", ".")
            reviewed = {test_key(c.get("test", "")) for c in override["causes"]}
            for case in failure.get("tests", []):
                if test_key(case["test"]) not in reviewed:
                    causes.append(dict(kind="unclassified", cause="additional failed assertion",
                                       job=failure["job"], step=failure["step"], evidence=failure["source"], **case))
        elif (failure["step"] == "System packages" and failure.get("assertion") == "System packages timed out"
              and failure.get("mirror_timeout") is True
              and any(label.startswith("ubuntu-") for label in failure["runner"])):
            causes.append(dict(kind="non_code", cause="runner-system-package-timeout",
                               cause_class="dependency source", evidence=failure["source"],
                               job=failure["job"], step=failure["step"], reason=failure["assertion"]))
        elif facts := artifacts.apply_rules(failure, row["sha"], signatures, repo_root):
            causes.extend(dict(**fact, evidence=failure["source"], job=failure["job"], step=failure["step"])
                          for fact in facts)
        else:
            causes.append(dict(kind="unclassified", cause="pending classification", evidence=failure["source"],
                               job=failure["job"], step=failure["step"]))
    if not causes:
        causes.append(dict(kind="unclassified", cause="first-attempt failing jobs and assertions required",
                           evidence=row["source"]))
    kinds = {c["kind"] for c in causes}
    if "code" in kinds:
        return "genuine_red", causes
    if kinds == {"non_code"} and not row.get("missing"):
        return "false_red", causes
    return "incomplete", causes
