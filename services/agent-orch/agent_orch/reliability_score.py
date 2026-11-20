"""Read-only release health command. All projections derive from the same report."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import re

from . import reliability_hosted as hosted
from .reliability_receipts import instant, nights, read, releases, runtime_history, stamp
from .reliability_work import weekly, work_state


def condition(numerator, denominator, size, meets, issues, bounds, sources, reason):
    missing = list(dict.fromkeys(issues))
    if size is not None and denominator != size:
        missing.append(f"need {size} observations, found {denominator}")
    state = "incomplete" if missing else "pass" if meets else "fail"
    return dict(passed=state == "pass", status=state, numerator=numerator, denominator=denominator,
                reason=reason, missing=missing, bounds=bounds, sources=sources)


def bounds(rows):
    if not rows:
        return []
    return [{k: r[k] for k in ("id", "created")} for r in (rows[0], rows[-1])]


def load_rows(cfg, as_of):
    if cfg.get("snapshot"):
        return read(cfg["snapshot"])
    return hosted.live(cfg["repository"], stamp(as_of))


def hosted_score(settings, as_of):
    issues = []
    rows = load_rows(settings, as_of)
    if settings.get("snapshot") and instant(settings["complete_through"]) < as_of:
        issues.append("hosted snapshot predates as-of")
    decisions = {}
    for d in settings.get("decisions", []):
        key = (str(d["run"]), d["sha"], d["job"], d["step"])
        if (int(d["attempt"]) != 1 or not all(d.get(k) for k in ("reviewer", "reason", "revision", "source", "causes"))
                or any(c.get("kind") not in {"code", "non_code", "unclassified"}
                       or not c.get("cause") or not c.get("cause_class") for c in d["causes"])
                or key in decisions):
            raise ValueError("invalid or duplicate first-attempt decision")
        decisions[key] = d
    retained = {}
    if settings.get("retained_csv"):
        retained = {r["id"]: r for r in hosted.retained(settings["retained_csv"], settings["retained_runs"],
                                                          settings["repository"])}
    unique = {}
    for row in rows:
        if instant(row["created"]) <= as_of:
            if row["id"] in unique and row != unique[row["id"]]:
                raise ValueError("conflicting observations for workflow ID")
            unique[row["id"]] = row
    sample = sorted(unique.values(), key=lambda r: (instant(r["created"]), int(r["id"])))[-100:]
    for row in sample:
        original = retained.get(row["id"])
        if original and original.get("review_category"):
            row["review_category"] = original["review_category"]
            if row["sha"] != original["sha"] or row["first"] != original["first"]:
                row["missing"] = "first attempt conflicts with retained receipt"
        # A rerun updates workflow.updated_at. For reruns the attempts/1 API
        # supplies the original finish time.
        if row.get("finished") and instant(row["finished"]) > as_of:
            row["first"] = "pending"
        row["classification"], row["causes"] = hosted.classifications(row, decisions,
            settings.get("signatures", []), settings.get("checkout"))
        row["occurrence"] = f"public-checks/{row['id']}/1"
        row["first_source"] = row["source"] + "/attempts/1"
        row["later_attempts"] = [dict(number=n, source=f"{row['source']}/attempts/{n}",
                                      outcome=row["latest"] if n == row["attempt"] else None)
                                 for n in range(2, row["attempt"]+1)]
        if row["classification"] == "incomplete" or row.get("missing") or any(
                c["kind"] == "unclassified" for c in row["causes"]):
            issues.append(f"workflow {row['id']}: " + (row.get("missing") or
                          ("pending classification" if row["first"] == "failure" else str(row["first"]))))
    counts = Counter(r["classification"] for r in sample)
    sources = [settings.get("snapshot") or f"{settings['repository']}/actions/workflows/{hosted.WORKFLOW}"]
    score = condition(counts["false_red"], len(sample), 100, counts["false_red"] <= 1, issues, bounds(sample),
                      sources, f"{counts['false_red']} confirmed false reds, allowance 1")
    score["first_attempt_counts"] = dict(Counter(r["first"] or "missing" for r in sample))
    return score, sample


def speed(settings, as_of):
    measurements = []
    for s in settings.get("samples", []):
        if instant(s["at"]) > as_of:
            continue
        row = {k: s[k] for k in ("at", "source", "surface")}
        row["target_seconds"] = {"local": 600, "hosted": 720}[s["surface"]]
        try:
            if "jobs" in s:
                jobs = read(s["jobs"])["jobs"]
                if not jobs or any(j.get("run_attempt") != 1 or j["run_id"] != s["run"] for j in jobs):
                    raise ValueError("attempt binding")
                first = min(instant(j["started_at"]) for j in jobs)
                last = max(instant(j["completed_at"]) for j in jobs)
                seconds = (last - first).total_seconds()
            else:
                evidence = Path(s["source"]).read_text()
                if not re.search(r"(?<![\d.])" + re.escape(str(s["seconds"])) + r"(?![\d.])", evidence):
                    raise ValueError("accepted measurement source unavailable")
                seconds = float(s["seconds"])
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("negative duration")
            row.update(seconds=seconds, within_target=seconds <= row["target_seconds"])
        except (OSError, KeyError, TypeError, ValueError):
            row["missing"] = "timing receipt unavailable"
        measurements.append(row)
    return measurements


def report(config, as_of):
    conditions = {}
    result = dict(as_of=stamp(as_of), conditions=conditions, observations={}, diagnostics={})
    errors = (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError, IndexError)

    def unavailable(name, surface, exc):
        source = config.get(surface, {})
        paths = [source[k] for k in ("register", "selector", "root", "snapshot", "repository", "runtime")
                 if isinstance(source, dict) and source.get(k)]
        # Errors from third-party parsers may include arbitrary source content.
        detail = str(exc) if type(exc) in (FileNotFoundError, KeyError) else type(exc).__name__
        conditions[name] = condition(None, None, None, False, [f"{surface}: {detail}"], [], paths,
                                     f"{surface} sources unavailable or invalid")

    try:
        settings = config["releases"]
        hosts = config["required_hosts"]
        if not hosts:
            raise ValueError("missing required hosts")
        rows, excluded = releases(settings, as_of, hosts)
        issues = [r["id"] + ": " + r["cause"] for r in rows if r["result"] == "incomplete"]
        if not settings.get("complete_through") or instant(settings["complete_through"]) < as_of:
            issues.append("first invocation inventory not reconciled through as-of")
        passes = sum(r["result"] == "passed" for r in rows)
        historical = sum(any(a["attempt"] == 1 and a["historical"] in ("passed", "active and checks passed")
                             for a in r["attempts"]) for r in rows)
        conditions["first_try_releases"] = condition(passes, len(rows), 10, passes >= 9, issues, bounds(rows),
            [r["source"] for r in rows], f"{historical} first-try passes as recorded, {passes} with all required assertions; need 9 of 10")
        conditions["first_try_releases"]["historical_passes"] = historical
        for r in rows:
            r["occurrence"] = "release/" + r["id"] + "/1"
        result["observations"].update(releases=rows, excluded_releases=excluded)
    except errors as exc:
        unavailable("first_try_releases", "releases", exc)
    try:
        conditions["false_reds"], result["observations"]["hosted"] = hosted_score(config["hosted"], as_of)
    except errors as exc:
        unavailable("false_reds", "hosted", exc)
    try:
        if not config.get("required_hosts"):
            raise ValueError("missing required hosts")
        runtime = config.get("releases", {}).get("runtime")
        activations = runtime_history(runtime) if runtime else []
        rows = nights(config["nightly"], as_of, config["required_hosts"], activations)
        issues = [r["id"] + ": " + r["cause"] for r in rows if r["result"] in ("incomplete", "missing")]
        passes = sum(r["result"] == "passed" for r in rows)
        historical = sum(r["historical"] == "PASS" for r in rows)
        conditions["scheduled_nights"] = condition(passes, len(rows), 7, passes == 7, issues, bounds(rows),
            [r["source"] for r in rows], f"{passes} fully covered, {historical} recorded passes, need 7 of 7")
        result["observations"]["nightly"] = rows
        conditions["scheduled_nights"]["historical_passes"] = historical
        for r in rows:
            r["occurrence"] = "nightly-soak/" + r["id"]
    except errors as exc:
        unavailable("scheduled_nights", "nightly", exc)
    try:
        work = work_state(config["work"], as_of)
        required = {r["occurrence"] for r in result["observations"].get("releases", [])
                    if r["result"] == "failed"}
        required.update(r["occurrence"] for r in result["observations"].get("hosted", [])
                        if r["classification"] == "false_red")
        required.update(r["occurrence"] for r in result["observations"].get("nightly", [])
                        if r["result"] in ("failed", "missing"))
        linked = {occ for r in work["rows"] if r.get("source") for occ in r.get("occurrences", [])}
        work["pending"] = sorted(set(work["pending"]) | (required - linked))
        if work["pending"]:
            work["cause_gaps"].append("unresolved scorecard occurrences: " + ", ".join(work["pending"]))
        for name, key, gaps, label in (("open_failure_items", "open_items", "cause_gaps", "open failure items"),
                                      ("open_quarantines", "open_tests", "quarantine_gaps", "quarantined tests")):
            n = len(work[key])
            conditions[name] = condition(n, None, None, n == 0, work[gaps], [stamp(as_of)],
                [s for s in (work["register"], work["selector"] if key == "open_tests" else None) if s],
                f"{n} known {label}, need zero")
        result["observations"]["work"] = {k: v for k, v in work.items() if k != "history"}
        result["diagnostics"]["weekly"] = weekly(work["history"], as_of)
    except errors as exc:
        unavailable("open_failure_items", "work", exc)
        unavailable("open_quarantines", "work", exc)
        result["diagnostics"]["weekly"] = {"missing": "work history unavailable"}
    try:
        result["diagnostics"]["speed"] = speed(config.get("speed", {}), as_of) or {"missing": "timing samples unavailable"}
        for row in result["observations"].get("hosted", []):
            timing = row.get("timing")
            if timing:
                if not isinstance(result["diagnostics"]["speed"], list):
                    result["diagnostics"]["speed"] = []
                measured = dict(surface="hosted", run=row["id"], target_seconds=720, **timing)
                if "started" in timing:
                    duration = (instant(timing["finished"])-instant(timing["started"])).total_seconds()
                    measured.update(seconds=duration, within_target=duration <= 720)
                result["diagnostics"]["speed"].append(measured)
    except errors:
        result["diagnostics"]["speed"] = {"missing": "invalid timing sample"}
    causes = {r["cause"]: r for r in result["observations"].get("work", {}).get("rows", [])}
    for r in result["observations"].get("hosted", []):
        if r["classification"] == "incomplete" and not r["causes"]:
            r["causes"].append(dict(kind="unclassified", cause="first attempt " + str(r["first"]), evidence=r["source"]))
        for c in r["causes"]:
            link = causes.get(c["cause"], {})
            c.update(owner=link.get("owner") or config.get("triage_owner", "release triage"), item=link.get("item"))
    for surface in ("nightly", "releases"):
        for r in result["observations"].get(surface, []):
            if r["result"] != "passed":
                link = causes.get(r["cause"], {})
                r.update(owner=link.get("owner") or config.get("triage_owner", "release triage"), item=link.get("item"))
    states = {s["status"] for s in conditions.values()}
    result["status"] = "incomplete" if "incomplete" in states else "fail" if "fail" in states else "pass"
    return result


def human(data):
    head = f"Release reliability  {data['status'].upper()}  {data['as_of']}"
    lines = [head]
    for name, value in data["conditions"].items():
        lines.append(f"{name:22} {value['status'].upper():10} {value['reason']}")
    # Preserve every observation and evidence field in the human projection.
    for section in ("conditions", "diagnostics", "observations"):
        lines.extend(("", section.capitalize()))
        for name, value in data[section].items():
            lines.append(f"  {name}")
            for row in value if isinstance(value, list) else [value]:
                lines.append("    " + json.dumps(row, sort_keys=True, ensure_ascii=False))
    return "\n".join(lines)


def source_paths(value, base):
    """Keep a private receipt bundle relocatable with its source map."""
    if isinstance(value, list):
        return [source_paths(v, base) for v in value]
    if isinstance(value, dict):
        return {k: source_paths(v, base) for k, v in value.items()}
    if isinstance(value, str) and value.startswith("./"):
        return str(base / value[2:])
    return value


def command(args):
    now = instant(args.as_of) if args.as_of else datetime.now(timezone.utc)
    path = Path(args.sources).expanduser()
    try:
        config = source_paths(read(path), path.resolve().parent)
        if not isinstance(config, dict):
            raise ValueError("invalid source configuration")
    except (ValueError, OSError):
        config = {}
    data = report(config, now)
    data["source_config"] = str(path)
    print(json.dumps(data, sort_keys=True, ensure_ascii=False) if args.json else
          human(data) + "\nSource configuration  " + str(path))
    return {"pass": 0, "fail": 1, "incomplete": 2}[data["status"]]


def add_parser(parent):
    group = parent.add_parser("reliability", help="Inspect release reliability")
    score = group.add_subparsers(dest="reliability_command", required=True).add_parser(
        "score", help="Read the five rolling closure conditions")
    score.add_argument("--sources", default=os.environ.get("PENTACLE_RELIABILITY_SOURCES",
                       "~/.pentacle/reliability/sources.json"), help="Local receipt-source configuration")
    score.add_argument("--as-of", help="UTC cutoff (ISO 8601 with timezone)")
    score.add_argument("--json", action="store_true", help="Emit the structured projection")
    score.set_defaults(func=command)
