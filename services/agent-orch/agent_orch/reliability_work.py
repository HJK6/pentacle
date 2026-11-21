"""Reconcile the effective selector, linked work records, and weekly observations."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path
import re
import subprocess

from .reliability_receipts import instant, read, stamp
from .reliability_selection import effective_tests


def lifecycle_view(root, as_of):
    if not (root.parent / ".git").exists():
        return None
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root.parent), *args], text=True,
                                       stderr=subprocess.DEVNULL, timeout=15)
    rev = git("rev-list", "-1", "--before=" + stamp(as_of), "HEAD").strip()
    if not rev:
        raise ValueError("no published work revision at cutoff")
    names = git("ls-tree", "-r", "--name-only", rev, "--", root.name).splitlines()
    paths = {}
    for name in names:
        parts = Path(name).parts
        if len(parts) == 4 and parts[-1] == "summary.md":
            paths.setdefault(parts[-2], []).append(Path(name))
    return rev, paths, git


def item(root, item_id, view=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
        raise ValueError("invalid work item ID")
    matches = view[1].get(item_id, []) if view else list(root.glob("*/" + item_id + "/summary.md"))
    if len(matches) != 1:
        raise ValueError("missing or ambiguous lifecycle record: " + item_id)
    path = matches[0]
    # Read only scalar frontmatter fields used by the existing lifecycle.
    source = str(path)
    if view:
        source = f"{view[0]}:{path}"
    body = view[2]("show", source) if view else path.read_text()
    parts = body.split("---", 2)
    if parts[0] or len(parts) < 3:
        raise ValueError("invalid work frontmatter: " + item_id)
    fields = dict(re.findall(r"^(id|status|owner|updated_at):[ \t]*([^\n]+)$", parts[1], re.M))
    status = path.parent.parent.name
    if (fields.get("status") != status or fields.get("id") != "work_" + item_id
            or not fields.get("owner") or not fields.get("updated_at")):
        raise ValueError("inconsistent lifecycle record: " + item_id)
    return dict(item=item_id, status=status, owner=fields["owner"].strip("'\""), source=source,
                updated=fields["updated_at"].strip("'\""))


def covered(data, now, name):
    if not data.get("complete_through") or instant(data["complete_through"]) < now:
        return [name + " census not reconciled through as-of"]
    return []


def work_state(settings, now):
    root = Path(settings["work_root"])
    if not root.is_dir():
        raise FileNotFoundError("work lifecycle tree unavailable: " + str(root))
    view = lifecycle_view(root, now)
    rows, gaps, quarantine_gaps = [], [], []
    register = read(settings["register"])
    gaps.extend(covered(register, now, "scorecard cause"))
    pending = set()
    open_items = set()
    decisions = set()
    for record in register["records"]:
        if instant(record["first_seen"]) > now:
            continue
        row = dict(record)
        try:
            row.update(item(root, row["item"], view))
            if row["updated"] > now.date().isoformat():
                raise ValueError("current lifecycle is newer than as-of: " + row["item"])
            terminal = row["status"] in ("completed", "deprecated")
            if terminal:
                disposition = row.get("disposition", "")
                source = Path(row.get("disposition_source", ""))
                if (not disposition or not source.is_file()
                        or disposition.casefold() not in source.read_text().casefold()):
                    raise ValueError("terminal disposition evidence required: " + row["item"])
                if disposition == "unreproduced, on watch" and not (row.get("recurrence_signature")
                                                                    and row.get("occurrences")):
                    raise ValueError("on-watch recurrence signature and original evidence required: " + row["item"])
            if not terminal:
                open_items.add(row["item"])
                if row.get("test"):
                    decisions.add(row["test"])
        except (ValueError, KeyError, OSError) as exc:
            gaps.append(str(exc))
            if row.get("test"):
                quarantine_gaps.append("unresolved decision: " + row["test"])
        rows.append(row)
    for occurrence in register.get("pending", []):
        if instant(occurrence["created"]) <= now:
            pending.add(occurrence["id"])
    if pending:
        gaps.append("outstanding cause dispositions: " + ", ".join(sorted(pending)))
    try:
        if settings.get("selector_checkout"):
            selected = effective_tests(settings["selector_checkout"])
        else:
            selector = read(settings["selector"])
            selected = set(selector["nonblocking_tests"])
        if not all(isinstance(key, str) and key for key in selected):
            raise ValueError("invalid selector test IDs")
        if selected != decisions:
            quarantine_gaps.append("effective selection and open decisions disagree")
        open_tests = sorted(selected | decisions)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        selected = None
        open_tests = sorted(decisions)
        quarantine_gaps.append("effective nonblocking test selection unavailable")
    quarantine_gaps.extend(covered(register, now, "quarantine decision"))
    return dict(rows=rows, open_items=sorted(open_items), open_tests=open_tests,
                effective_tests=sorted(selected) if selected is not None else None, open_decisions=sorted(decisions),
                pending=sorted(pending), cause_gaps=gaps, quarantine_gaps=quarantine_gaps,
                register=settings["register"], selector=settings.get("selector_checkout") or settings.get("selector"),
                history=register.get("history", {}))


def canonical(name, aliases):
    seen = set()
    while name in aliases:
        if name in seen:
            raise ValueError("cause class alias cycle")
        seen.add(name)
        name = aliases[name]
    return name


def weekly(history, now):
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    aliases = history.get("class_aliases", {})
    seen, events = {}, {}
    for entry in history.get("events", []):
        if instant(entry["at"]) > now:
            continue
        key = (entry["id"], entry["kind"], entry.get("test"), canonical(entry.get("class"), aliases))
        if key in events and events[key]["at"] != entry["at"]:
            raise ValueError("conflicting occurrence history")
        events[key] = entry
        if key[3]:
            seen[key[3]] = min(instant(entry["at"]), seen.get(key[3], now))
    ordered = sorted(events.values(), key=lambda e: (instant(e["at"]), e["id"]))
    rows = []
    earliest = min([monday-timedelta(weeks=2), *(instant(e["at"]) for e in ordered)])
    first_monday = earliest.date() - timedelta(days=earliest.weekday())
    for offset in range((monday.date()-first_monday).days // 7, -1, -1):
        start = monday - timedelta(weeks=offset)
        end = min(now, start + timedelta(days=7))
        tests = set()
        count = 0
        for e in ordered:
            at = instant(e["at"])
            if at > end or (offset and at == end):
                continue
            if e["kind"] == "quarantined":
                tests.add(e["test"])
            elif e["kind"] == "restored" or e["kind"] == "deleted":
                tests.discard(e["test"])
            if at >= start:
                count += 1
        classes = sorted(name for name, at in seen.items() if start <= at < end or (offset == 0 and at == end))
        complete = bool(history.get("from_inception") is True and history.get("complete_through")
                        and instant(history["complete_through"]) >= end)
        rows.append(dict(start=stamp(start), end=stamp(end), partial_week=offset == 0, complete=complete,
                         open_quarantines=len(tests), new_classes=len(classes), classes=classes,
                         evidence_count=count))
    older, prior = rows[-3:-1]
    signals = [k for k in ("new_classes", "open_quarantines")
               if older["complete"] and prior["complete"] and 0 < older[k] <= prior[k]]
    review = None
    if signals:
        key = "weekly/" + older["start"] + "/" + prior["end"]
        matched = [e for e in ordered if e.get("class")
                   and instant(older["start"]) <= instant(e["at"]) < instant(prior["end"])]
        counts = Counter(canonical(e["class"], aliases) for e in matched)
        dominant = sorted(k for k, n in counts.items() if n == max(counts.values())) if counts else []
        existing = history.get("reviews", {}).get(key)
        review = dict(id=key, status="recorded" if existing else "due", dominant_classes=dominant,
                      existing_review=existing)
    return dict(weeks=rows, review_signals=signals, review=review, prior_comparable_week=prior["start"],
                reason=None if rows[-1]["complete"] else "incomplete historical decision and class census")
