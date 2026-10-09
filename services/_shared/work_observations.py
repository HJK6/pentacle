"""File reads and last-good observation transitions for the shared specs index.

The specs subsystem owns discovery and debounce. The daemon Store owns durable
snapshots and commits these transitions alongside lane history.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .specs_parser import is_sync_conflict, load_frontmatter, parse_work_facts

MATERIAL_FIELDS = ("status", "ac_checked", "ac_total", "estimate", "status_text", "next_action_text")
SPEC_ID = re.compile(r"^spec_[a-z0-9_]+$")


def timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _signature(path: Path) -> tuple:
    if path.is_symlink():
        raise ValueError("work_source_symlink")
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def read_work_candidate(folder: Path, status: str, *, now: float, quiet_s: float) -> dict:
    """Read exactly the two source files, checking identities around the read."""
    paths = (folder / "spec.md", folder / "summary.md")
    before = [_signature(p) for p in paths]
    texts = [p.read_text(encoding="utf-8") for p in paths]
    after = [_signature(p) for p in paths]
    fm = load_frontmatter(texts[0])
    identity = fm.get("id")
    if not isinstance(identity, str) or not SPEC_ID.fullmatch(identity):
        raise ValueError("work_declared_id_invalid")
    if (before != after or any(now - p.stat().st_mtime < quiet_s for p in paths)
            or not all(text.strip() for text in texts)):
        return {"spec_id": identity, "quality": "stale", "error": "work_files_unsettled", "path": str(folder)}
    facts = parse_work_facts(*texts, status)
    facts["title"] = facts["title"] if isinstance(facts["title"], str) else None
    digest = hashlib.sha256(json.dumps([status, *texts], ensure_ascii=False).encode()).hexdigest()
    return {"spec_id": identity, "quality": "fresh", "facts": facts,
            "source_hash": digest, "path": str(folder), "error": None}


def unresolved_member(spec_id: str, quality: str = "missing", error: str | None = None) -> dict:
    return {"spec_id": spec_id, "title": None, "status": quality if quality in ("missing", "ambiguous") else "missing",
            "terminal": None, "ac_checked": None, "ac_total": None, "estimate": None,
            "status_text": None, "next_action_text": None, "source_changed_at": None,
            "observation": {"quality": quality, "observed_at": None, "error": error}, "obs_rev": 1}


def advance_observation(spec_id: str, previous: dict | None, candidate: dict, *, now: float,
                        settle_s: float, sweep: bool) -> tuple[dict, dict | None]:
    """Return a persistent record and optional material transition, without I/O.

    Incident timing/counters survive restart. Watcher callbacks cannot count as
    multiple periodic sweeps. Losing the entire root never settles items missing.
    """
    record = copy.deepcopy(previous) if previous else {
        "member": unresolved_member(spec_id), "last_good": None, "source_hash": None,
        "path": None, "incident": None, "bad_since": None, "bad_sweeps": 0,
    }
    old = record.get("last_good")
    quality = candidate["quality"]
    error = candidate.get("error")
    change = None
    if quality == "fresh":
        facts = candidate["facts"]
        material = old is not None and any(old.get(k) != facts.get(k) for k in MATERIAL_FIELDS)
        rev = record["member"]["obs_rev"] + int(material)
        source_changed = (timestamp(now) if record.get("source_hash") != candidate["source_hash"]
                          else record["member"]["source_changed_at"])
        member = {"spec_id": spec_id, **facts, "source_changed_at": source_changed,
                  "observation": {"quality": "fresh", "observed_at": timestamp(now), "error": None}, "obs_rev": rev}
        if material:
            fields = ("status", "ac_checked", "ac_total", "estimate")
            change = {"spec_id": spec_id, "obs_rev": rev,
                      "prior": {k: old.get(k) for k in fields}, "next": {k: facts.get(k) for k in fields},
                      "source_changed_at": source_changed}
        record.update(member=member, last_good=copy.deepcopy(facts), path=candidate["path"],
                      source_hash=candidate["source_hash"], incident=None, bad_since=None, bad_sweeps=0)
    else:
        if quality in ("missing", "ambiguous"):
            if record.get("incident") != quality:
                record.update(incident=quality, bad_since=now, bad_sweeps=0)
            record["bad_sweeps"] += int(sweep)
            settled = record["bad_sweeps"] >= 2 and now - record["bad_since"] >= settle_s
            if not settled and (old is not None or quality == "ambiguous"):
                quality = "stale"
        else:
            record.update(incident=None, bad_since=None, bad_sweeps=0)
        member = record["member"]
        if quality in ("missing", "ambiguous"):
            member = {**unresolved_member(spec_id, quality), "obs_rev": member["obs_rev"],
                      "source_changed_at": member["source_changed_at"], "observation": dict(member["observation"])}
        elif old is not None:
            member = {**member, **old}
        member["observation"] = {**member["observation"], "quality": quality, "error": error}
        record["member"] = member
    return record, change
