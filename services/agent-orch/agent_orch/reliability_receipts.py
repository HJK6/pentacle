"""Project deploy transactions and scheduled receipts without invoking their writers."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import re
import subprocess
from zoneinfo import ZoneInfo

UTC = timezone.utc


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return parsed.astimezone(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read(path):
    return json.loads(Path(path).read_bytes())


def checked(path, digest=None, retained=None, *, verify_only=False):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        if not retained:
            raise
        raw = None
    if raw is not None and (not digest or hashlib.sha256(raw).hexdigest() == digest):
        return None if verify_only else json.loads(raw)
    # A rotating stamp or aged-out directory can have a pinned field projection.
    # Bind both its bytes and the digest of the original evidence.
    if not retained or not digest:
        raise ValueError("receipt digest mismatch")
    archive = checked(retained["path"], retained["sha256"])
    if archive["original_sha256"] != digest or archive["source"] != str(path):
        raise ValueError("archived receipt identity mismatch")
    return archive["fields"]


def coverage(checks, hosts):
    fleet = checks.get("fleet_smoke") or {}
    present = {(c.get("host"), c.get("provider"), c.get("prompt_mode"))
               for c in fleet.get("cells", []) if c.get("outcome") == "passed"}
    missing = [name for name in ("front_desk", "slow_consumer", "fleet_smoke")
               if (checks.get(name) or {}).get("outcome") != "passed"]
    missing.extend(f"{host}/{provider}/{mode}" for host in hosts
                   for provider in ("claude", "codex") for mode in ("prompted", "promptless")
                   if (host, provider, mode) not in present)
    delivery = checks.get("client_delivery") or {}
    if delivery.get("outcome") != "passed":
        missing.append("client_delivery/outcome")
    for name in ("publish", "receive", "reconnect", "recover"):
        if (delivery.get("assertions") or {}).get(name) != "passed":
            missing.append("client_delivery/" + name)
    return missing


def nightly_slots(settings, as_of, *, since=None):
    zone = ZoneInfo(settings["timezone"])
    clock = time.fromisoformat(settings["local_time"])
    day = as_of.astimezone(zone).date()
    if datetime.combine(day, clock, zone) > as_of:
        day -= timedelta(days=1)
    days = 7 if since is None else max(0, (day - since.astimezone(zone).date()).days + 1)
    slots = [datetime.combine(day - timedelta(days=offset), clock, zone).astimezone(UTC)
             for offset in reversed(range(days))]
    return slots if since is None else [slot for slot in slots if slot >= since]


def scheduled_starts(settings, root):
    """Keep only the scheduler's start/run identities, never its log payloads."""
    if not settings.get("plist"):
        return {}
    with Path(settings["plist"]).open("rb") as fh:
        job = plistlib.load(fh)
    clock = time.fromisoformat(settings["local_time"])
    if (job["Label"] != "com.pentacle.nightly-soak"
            or job["StartCalendarInterval"] != {"Hour": clock.hour, "Minute": clock.minute}):
        raise ValueError("nightly scheduler definition mismatch")
    by_minute = {}
    with Path(job["StandardOutPath"]).open(errors="replace") as log:
        for line in log:
            m = re.fullmatch(r"=== nightly-soak (?:scheduled )?start (\S+) run=(\S+)\n?", line)
            if m and Path(m[2]).parent == root:
                started = instant(m[1])
                by_minute.setdefault(stamp(started.replace(second=0, microsecond=0)), []).append(Path(m[2]).name)
    return by_minute


def nights(settings, as_of, hosts, activations=(), *, since=None):
    # Older runner receipts have no occurrence ID. A reviewed scheduler binding
    # names the exact original directory, digest and released SHA for that slot.
    bindings = dict(settings.get("bindings", {}))
    if settings.get("occurrence_index"):
        bindings.update(read(settings["occurrence_index"]))
    root = Path(settings["root"])
    starts = scheduled_starts(settings, root)
    result = []
    for due in nightly_slots(settings, as_of, since=since):
        slot = stamp(due)
        row = dict(id=slot, created=slot, result="missing", historical=None, missing=[],
                   cause="missing scheduled first receipt", source=str(root), exclusions=[])
        bound = bindings.get(slot)
        if not bound and starts.get(slot):
            directories = starts[slot]
            prior = [a for a in activations if instant(a["created"]) <= due]
            if len(directories) == 1 and prior:
                bound = dict(directory=directories[0], release_sha=prior[-1]["sha"],
                             source=settings["plist"])
            else:
                row.update(result="incomplete", cause="ambiguous scheduler occurrence or release identity")
        if bound:
            path = root / bound["directory"] / "evidence" / "receipt.json"
            row["source"] = str(path)
            try:
                if (not re.fullmatch(r"\d{8}T\d{6}Z", bound["directory"])
                        or (not bound.get("sha256") and not (settings.get("plist") and
                                                             bound.get("source") == settings["plist"]))
                        or not bound.get("release_sha")):
                    raise ValueError("missing scheduler or release binding")
                receipt = checked(path, bound.get("sha256"), bound.get("retained"))
                if (receipt.get("job") != "nightly-soak" or receipt.get("date") != slot[:10]
                        or receipt.get("source_sha") != bound["release_sha"]):
                    raise ValueError("scheduler or release identity mismatch")
                hist = receipt.get("result")
                row.update(sha=bound["release_sha"], historical=hist, scheduler_source=bound.get("source"),
                           exclusions=[
                    {k: x.get(k) for k in ("host", "reason", "readiness_item")}
                    for x in receipt.get("release_exclusions", [])])
                row["missing"] = coverage(receipt.get("checks", {}), hosts)
                if hist in {"FAIL", "BLOCKED", "SKIPPED"}:
                    row.update(result="failed", cause="scheduled first receipt " + hist,
                               failure_checks=failed_checks(receipt.get("checks", {})),
                               work_item=receipt.get("item"), observed_at=receipt.get("finalized_at"))
                elif hist == "PASS" and receipt.get("finished") is True:
                    passed = not row["missing"]
                    row.update(result="passed" if passed else "incomplete",
                               cause=None if passed else "required installed assertions missing or failed")
                else:
                    row.update(result="incomplete", cause="scheduled first result incomplete")
            except (OSError, TypeError, ValueError, KeyError) as exc:
                row.update(result="incomplete", cause=f"cannot verify scheduled receipt ({type(exc).__name__})")
        result.append(row)
    return result



def failed_checks(checks):
    failed = {}
    for name in ("client_delivery", "front_desk", "slow_consumer", "fleet_smoke"):
        check = checks.get(name)
        if isinstance(check, dict) and check.get("outcome") != "passed":
            failed[name] = {key: check[key] for key in ("classification", "cause_target", "phase",
                                                        "reason", "correlation_id") if key in check}
    return failed

def release_attempt(entry, sha, prior, hosts):
    source = entry["path"]
    result = dict(source=source, attempt=entry["attempt"], historical=None, result="incomplete",
                  cause="missing first-attempt result", missing=[])
    try:
        if entry["type"] == "review":
            # Older releases also have signed-off human readbacks. A bound
            # decision can establish their historical verdict, not assertions
            # which were never executed. Preserve the evidence and revision.
            review = entry["decision"]
            checked(source, entry["digest"], entry.get("retained"), verify_only=True)
            if (review["sha"] != sha or review["prior_sha"] != prior
                    or not all(review.get(k) for k in ("reviewer", "reason", "revision"))
                    or review["historical"] not in ("passed", "failed", "refused_before_activation")):
                raise ValueError("invalid reviewed release decision")
            result.update(historical=review["historical"], review=review,
                          result="failed", cause=review["reason"])
            if review["historical"] == "passed":
                result.update(result="incomplete", missing=["installed coverage assertions"],
                              cause="historical pass without current assertion evidence")
            return result
        doc = checked(source, entry.get("digest"), entry.get("retained"))
        if entry["type"] == "refusal":
            if (doc["attempt"] != entry["attempt"] or doc["target_sha"] != sha
                    or doc["evidence"]["preimage"]["source_sha"] != prior
                    or doc["outcome"] != "refused_before_activation" or doc["exit_code"] == 0):
                raise ValueError("refusal identity mismatch")
            return {**result, "historical": "refused_before_activation", "result": "failed",
                    "cause": "refused before activation"}
        if entry["type"] == "ordinary_forward":
            if (doc["target_sha"], doc["prior_sha"]) != (sha, prior):
                raise ValueError("ordinary forward identity mismatch")
            if doc["result"] == "refused":
                result.update(historical="refused_before_activation", result="failed",
                              cause="refused before activation")
                return result
            if doc["result"] != "complete":
                return {**result, "pending": doc["result"] == "pending"}
            result["observed_at"] = doc.get("completed_at")
            checks = doc["stamp"]
            active = checks.get("serving") is True
            historical = checks["disposition"]
            failed = historical in ("active with failed checks", "target not active, prior restored",
                                    "target not active, restore unproven")
        elif entry["type"] == "journal":
            receipt = checked(entry.get("receipt_copy") or doc["receipt"], entry.get("receipt_digest"))
            intent = receipt["intent"]
            if (intent["target_sha"] != sha or intent["prior_sha"] != prior
                    or doc["transaction_id"] != intent["transaction_id"]
                    or doc["transaction_id"] != entry["transaction"]
                    or doc["receipt_sha256"] != receipt["receipt_sha256"]):
                raise ValueError("journal identity mismatch")
            if doc.get("first_forward_at"):
                sealed = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
                canonical = json.dumps(sealed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                if hashlib.sha256(canonical).hexdigest() != receipt["receipt_sha256"]:
                    raise ValueError("coordinated receipt seal mismatch")
            if doc.get("first_forward_refused") is True:
                return {**result, "historical": "refused_before_activation", "result": "failed",
                        "cause": "coordinated release refused before activation"}
            checks = doc.get("result") or doc.get("partial_result") or {}
            result["observed_at"] = doc.get("forward_result_at")
            active = doc["phase"] == "committed" and (doc.get("postimage") or {}).get("sha") == sha
            if (doc.get("first_forward_at") and doc["phase"] not in ("rolled_back", "recovery_required")
                    and not doc.get("post_checks_recorded") and not doc.get("forward_failure")):
                return {**result, "pending": True}
            historical = checks.get("outcome")
            failed = bool(doc.get("forward_failure")) or doc["phase"] in {"rolled_back", "recovery_required"}
            result["transaction"] = intent["transaction_id"]
        elif entry["type"] == "stamp":
            activation = doc["restart_activation"]
            if (doc["sha"] != sha or doc["prior_sha"] != prior or activation["target_sha"] != sha
                    or activation["activation_id"] != entry["transaction"]):
                raise ValueError("activation identity mismatch")
            checks = doc
            active = doc.get("serving") is True
            historical = doc["disposition"]
            failed = historical == "active with failed checks"
            result["transaction"] = activation["activation_id"]
        else:
            raise ValueError("unknown attempt evidence type")
        result["historical"] = historical
        result["missing"] = coverage(checks, hosts)
        result["exclusions"] = [x.get("host") for x in doc.get("release_exclusions", [])]
        if failed or historical in ("failed", "active with failed checks"):
            result.update(result="failed", cause="first deployment or installed checks failed",
                          failure_checks=failed_checks(checks))
        elif active and historical in ("passed", "active and checks passed"):
            result.update(result="incomplete" if result["missing"] else "passed",
                          cause="required installed assertions missing or failed" if result["missing"] else None)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        result["cause"] = f"cannot verify deploy attempt ({type(exc).__name__})"
    return result


def runtime_history(runtime):
    log = subprocess.run(["git", "-C", str(runtime), "reflog", "-32", "--date=iso-strict",
                          "--format=%H %gD", "HEAD"], check=True, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, timeout=10).stdout
    refs = re.findall(r"^([0-9a-f]{40}) HEAD@\{([^}]+)\}$", log, re.M)
    return [dict(sha=sha, created=stamp(instant(date))) for sha, date in reversed(refs)]


def live_intents(settings):
    entries = [dict(e, attempts=list(e.get("attempts", []))) for e in settings.get("intents", [])]
    by_pair = {(e["sha"], e["prior_sha"]): e for e in entries if e["kind"] == "ordinary"}
    automatic = []
    for feed in settings.get("attempt_sources", []):
        root = Path(feed["root"])
        if not root.is_dir():
            raise FileNotFoundError("deploy invocation source unavailable: " + str(root))
        for path in sorted(root.glob(feed["pattern"])):
            # A refused invocation may never appear in either Git or a stamp.
            # Join by actual target and prior SHA; copies are not new releases.
            if feed["type"] == "ordinary_forward":
                doc = read(path)
                sha, prior = doc["target_sha"], doc["prior_sha"]
                if sha != prior and all(re.fullmatch(r"[a-f0-9]{40}", v) for v in (sha, prior)):
                    automatic.append((instant(doc["created_at"]), str(path), sha, prior,
                                      {"type": "ordinary_forward", "path": str(path)}))
                continue
            if feed["type"] == "refusal":
                doc = read(path)
                if doc.get("outcome") != "refused_before_activation":
                    continue
                sha = doc["target_sha"]
                prior = doc["evidence"]["preimage"]["source_sha"]
                number = doc["attempt"]
                when = doc.get("created_at")
            elif feed["type"] == "fd_result":
                body = path.read_text()
                heading = re.match(r"^# RESULT-[\w-]+: release ([0-9a-f]{40}) [^\n]*REFUSED[^\n]*NOT DEPLOYED", body)
                preimage = re.search(r"(?:live )?checkout HEAD ([0-9a-f]{40})\b", body)
                timing = re.search(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d)Z to", body)
                if not (heading and preimage and timing):
                    continue
                sha, prior = heading[1], preimage[1]
                when = timing[1] + "T" + timing[2] + ":00Z"
                number = 1
            else:
                raise ValueError("unsupported release invocation source")
            pair = (sha, prior)
            row = by_pair.get(pair)
            if row and any(a["path"] == str(path) for a in row["attempts"]):
                continue
            # Receipt mtimes are only upper bounds on observation. An unbound
            # invocation stays unresolved until its owner supplies chronology.
            if row is None:
                row = dict(id=sha + "/" + prior, sha=sha, prior_sha=prior, source=str(path),
                           created=when or stamp(datetime.fromtimestamp(path.stat().st_mtime, UTC)),
                           kind="ordinary" if when else "unknown", attempts=[])
                entries.append(row)
                by_pair[pair] = row
            if feed["type"] == "fd_result":
                if row["attempts"]:
                    # A new FD report can be a later retry. Without a numbered
                    # link to the first invocation do not promote either result.
                    row["kind"] = "unknown"
                attempt = dict(type="review", attempt=number, path=str(path),
                               digest=hashlib.sha256(body.encode()).hexdigest(),
                               decision=dict(sha=sha, prior_sha=prior, historical="refused_before_activation",
                                             reviewer="FD deployment receipt", revision=path.name,
                                             reason="FD controller recorded a release refusal"))
            else:
                attempt = dict(type="refusal", attempt=number, path=str(path))
            row["attempts"].append(attempt)
    if settings.get("coordinated_root"):
        root = Path(settings["coordinated_root"])
        if not root.is_dir():
            raise FileNotFoundError("coordinated invocation source unavailable")
        for path in root.glob("*/journal.json"):
            doc = read(path)
            when = doc.get("first_forward_at")
            if not when:
                continue
            receipt = read(doc["receipt"])
            intent = receipt["intent"]
            sha, prior = intent["target_sha"], intent["prior_sha"]
            if sha != prior:
                automatic.append((instant(when), str(path), sha, prior,
                                  dict(type="journal", path=str(path), transaction=doc["transaction_id"])))
    for when, path, sha, prior, attempt in sorted(automatic):
        row = by_pair.get((sha, prior))
        if row is None:
            row = dict(id=sha + "/" + prior, sha=sha, prior_sha=prior, source=path,
                       created=stamp(when), kind="ordinary", attempts=[])
            entries.append(row)
            by_pair[(sha, prior)] = row
        if any(a["path"] == path for a in row["attempts"]):
            continue
        # Explicit attempts are retained with their original number. An
        # independent uncovered invocation preceding those facts requires a review.
        if instant(row["created"]) > when.replace(microsecond=0):
            row["kind"] = "unknown"
        number = max((a["attempt"] for a in row["attempts"]), default=0) + 1
        row["attempts"].append(dict(attempt, attempt=number, created=stamp(when)))
    if "runtime" not in settings:
        return entries
    runtime = Path(settings["runtime"])
    path = runtime / ".pentacle-deploy" / "chat-streamd-v2.json"
    latest = read(path)
    # A changed runtime is evidence of a release, but a HEAD move cannot prove
    # which invocation was first. Leave uncovered activations unresolved.
    known = {(r["sha"], r["prior_sha"]) for r in entries}
    refs = [(r["sha"], r["created"]) for r in reversed(runtime_history(runtime))]
    for (sha, date), (prior, _) in zip(refs, refs[1:]):
        if sha != prior and (sha, prior) not in known:
            entries.append(dict(id="activation-" + sha + "-" + date, created=date, sha=sha, prior_sha=prior,
                                source=str(runtime / ".git/logs/HEAD"), kind="unknown", attempts=[]))
    activation = latest.get("restart_activation") or {}
    if activation.get("target_sha") == latest["sha"]:
        for entry in entries:
            if (entry["sha"], entry["prior_sha"]) == (latest["sha"], latest["prior_sha"]):
                entry.setdefault("last_activation", {})["source"] = str(path)
                entry["last_activation"].update(transaction=activation.get("activation_id"),
                                                created=latest["timestamp"], disposition=latest["disposition"])
    return entries


def releases(settings, as_of, hosts, *, since=None):
    rows, excluded = [], []
    seen = set()
    for entry in sorted(live_intents(settings), key=lambda x: (instant(x["created"]), x["id"])):
        if instant(entry["created"]) > as_of or (since is not None and instant(entry["created"]) < since):
            continue
        if entry["id"] in seen:
            raise ValueError("duplicate release intent")
        seen.add(entry["id"])
        row = {k: entry[k] for k in ("id", "sha", "prior_sha", "source")}
        row["created"] = stamp(instant(entry["created"]))
        if entry.get("last_activation") and instant(entry["last_activation"]["created"]) <= as_of:
            latest = entry["last_activation"]
            row["last_activation"] = {**latest, "created": stamp(instant(latest["created"]))}
        if entry["kind"] in ("rehearsal", "same_ref_restart", "historical_exclusion") and entry.get("reason"):
            excluded.append({**row, "reason": entry["reason"]})
            continue
        attempts = [release_attempt(a, row["sha"], row["prior_sha"], hosts) for a in
                    sorted(entry.get("attempts", []), key=lambda a: a["attempt"])
                    if instant(a.get("created", entry["created"])) <= as_of]
        first = [a for a in attempts if a["attempt"] == 1]
        row.update(attempts=attempts, result="incomplete", cause="first deploy invocation not identified")
        if entry["kind"] == "ordinary" and row["sha"] != row["prior_sha"] and len(first) == 1:
            row.update(result=first[0]["result"], cause=first[0]["cause"])
        if entry["kind"] != "ordinary":
            row["cause"] = "unresolved forward-release intent"
        rows.append(row)
    return (rows[-10:] if since is None else rows), excluded
