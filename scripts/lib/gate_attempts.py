"""Append-only test-attempt receipts (spec: test attempts and delivery churn).

One root attempt is a directory of immutable event files
`<evidence>/attempts/<attempt_id>/<seq:04d>-<state>.json`, each created with
O_EXCL and fsynced. States fold started -> prepared -> subject_started ->
terminal; an early terminal is allowed from any nonterminal state. The
`test-attempts.v1.json` index is derived and can be rebuilt from the events.
Stdlib only; reads never execute or fetch anything.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "pentacle.test-attempt-event.v1"
INDEX_SCHEMA = "test-attempts.v1"
MANIFEST_SCHEMA = "pentacle.test-attempt-manifest.v1"
STATES = ("started", "prepared", "subject_started", "terminal")
PURPOSES = ("acceptance", "diagnostic", "negative_control")
OUTCOMES = (
    "pass", "product_failure", "setup_failure", "harness_failure", "timeout_unclassified",
    "cleanup_failure", "cancelled", "incomplete", "unknown",
)
UNKNOWN = "unknown"
_EVENT_NAME = re.compile(r"^(\d{4})-(started|prepared|subject_started|terminal)\.json$")
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_EVENT_BYTES = 256 * 1024


class AttemptError(Exception):
    """A receipt could not be written or violates the event contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_attempt_id() -> str:
    return str(uuid.uuid4())


def canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def digest_of(data: Any) -> str:
    return hashlib.sha256(canonical(data)).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exclusive(path: Path, payload: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def atomic_write(path: Path, payload: bytes) -> None:
    """tmp + fsync + rename publication for derived files."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    _fsync_dir(path.parent)


class AttemptWriter:
    """Single owner of one root attempt's sequence."""

    def __init__(self, evidence_dir: Path, attempt_id: str, *, identity: Dict[str, Any]) -> None:
        if not _ID.match(attempt_id):
            raise AttemptError("invalid attempt id")
        self.evidence_dir = Path(evidence_dir)
        self.attempt_id = attempt_id
        self.dir = self.evidence_dir / "attempts" / attempt_id
        self.identity = dict(identity)
        self._seq = 0
        self._state = None  # last state written
        self._t0 = time.monotonic()
        self._started_at: Optional[str] = None

    @property
    def terminal(self) -> bool:
        return self._state == "terminal"

    def _next_allowed(self, state: str) -> bool:
        order = {"started": 0, "prepared": 1, "subject_started": 2}
        if self._state is None:
            return state == "started"
        if self._state == "terminal":
            return False
        if state == "terminal":
            return True
        return state in order and self._state in order and order[state] == order[self._state] + 1

    def emit(self, state: str, **fields: Any) -> Path:
        if state not in STATES:
            raise AttemptError("unknown state: %s" % state)
        if not self._next_allowed(state):
            raise AttemptError("invalid transition %s -> %s" % (self._state, state))
        if state == "terminal" and fields.get("outcome") not in OUTCOMES:
            raise AttemptError("terminal event needs an outcome from the closed vocabulary")
        if state == "started":
            self.dir.mkdir(parents=True, exist_ok=True)
            self._started_at = utc_now()
        body = {
            "schema": SCHEMA,
            "attempt_id": self.attempt_id,
            "seq": self._seq + 1,
            "state": state,
            "at": utc_now(),
            "monotonic_s": round(time.monotonic() - self._t0, 6),
        }
        if state == "started":
            body.update(self.identity)
            body["started_at"] = self._started_at
        body.update(fields)
        body["event_digest"] = digest_of({k: v for k, v in body.items() if k != "event_digest"})
        payload = json.dumps(body, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        if len(payload) > _MAX_EVENT_BYTES:
            raise AttemptError("event too large")
        path = self.dir / ("%04d-%s.json" % (self._seq + 1, state))
        try:
            _write_exclusive(path, payload)
        except FileExistsError:
            existing = json.loads(path.read_text("utf-8"))
            if existing.get("event_digest") != body["event_digest"]:
                raise AttemptError("conflicting event for sequence %d" % (self._seq + 1))
            # Identical digest only happens if the clock repeats; treat as idempotent.
        self._seq += 1
        self._state = state
        return path


def fold_attempt(attempt_dir: Path) -> Dict[str, Any]:
    """Fold event files into state. Never raises on bad evidence; reports it."""
    attempt_dir = Path(attempt_dir)
    problems: List[str] = []
    events: List[Dict[str, Any]] = []
    names = sorted(p.name for p in attempt_dir.iterdir()) if attempt_dir.is_dir() else []
    seqs = []
    for name in names:
        match = _EVENT_NAME.match(name)
        if not match:
            if not name.startswith(".") and name != "observations":
                problems.append("unexpected file: %s" % name)
            continue
        path = attempt_dir / name
        try:
            if path.stat().st_size > _MAX_EVENT_BYTES:
                raise ValueError("oversized")
            event = json.loads(path.read_text("utf-8"))
            if not isinstance(event, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as exc:
            problems.append("corrupt event %s: %s" % (name, type(exc).__name__))
            seqs.append(int(match.group(1)))
            continue
        if digest_of({k: v for k, v in event.items() if k != "event_digest"}) != event.get("event_digest"):
            problems.append("digest mismatch in %s" % name)
        if event.get("seq") != int(match.group(1)) or event.get("state") != match.group(2):
            problems.append("name/content mismatch in %s" % name)
        seqs.append(int(match.group(1)))
        events.append(event)
    if seqs and sorted(seqs) != list(range(1, len(seqs) + 1)):
        problems.append("missing or duplicate sequence")
    state, terminal_seen = None, False
    order = {"started": 0, "prepared": 1, "subject_started": 2}
    for event in events:
        st = event.get("state")
        if terminal_seen:
            problems.append("event after terminal")
            break
        if state is None:
            if st != "started":
                problems.append("first event is not started")
        elif st == "terminal":
            pass
        elif st not in order or order[st] != order.get(state, -9) + 1:
            problems.append("invalid transition %s -> %s" % (state, st))
        state = st
        terminal_seen = st == "terminal"
    started = events[0] if events and events[0].get("state") == "started" else {}
    terminal = events[-1] if events and events[-1].get("state") == "terminal" else None
    if not events:
        status = "invalid"
    elif problems:
        status = "invalid"
    elif terminal:
        status = "complete"
    else:
        status = "incomplete"
    result: Dict[str, Any] = {
        "attempt_id": started.get("attempt_id") or attempt_dir.name,
        "status": status,
        "problems": problems,
        "last_state": state,
        "event_count": len(events),
        "primary_outcome": (terminal or {}).get("outcome") if terminal else ("incomplete" if status != "invalid" else UNKNOWN),
    }
    for key in ("spec_id", "series_id", "prior_attempt_id", "purpose", "host", "owner_stream_id",
                "owner_generation", "history", "started_at", "identities", "target", "profile"):
        if key in started:
            result[key] = started[key]
    if terminal:
        for key in ("at", "monotonic_s", "secondary", "first_failure", "stage", "evidence"):
            if key in terminal:
                result["terminal_" + key if key in ("at", "monotonic_s") else key] = terminal[key]
    return result


def build_index(evidence_dir: Path) -> Dict[str, Any]:
    root = Path(evidence_dir) / "attempts"
    attempts = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir():
                attempts.append(fold_attempt(child))
    return {"schema": INDEX_SCHEMA, "generated_at": utc_now(), "attempts": attempts}


def write_index(evidence_dir: Path) -> Path:
    """Derived index; failing here never touches authoritative events."""
    path = Path(evidence_dir) / "test-attempts.v1.json"
    atomic_write(path, json.dumps(build_index(evidence_dir), sort_keys=True, indent=2).encode("utf-8") + b"\n")
    return path


def validate_index(evidence_dir: Path) -> List[str]:
    """Report drift between the derived index and the event files."""
    path = Path(evidence_dir) / "test-attempts.v1.json"
    try:
        recorded = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return ["index missing or unreadable"]
    fresh = build_index(evidence_dir)
    drift = []
    have = {a["attempt_id"]: a for a in recorded.get("attempts", [])}
    for attempt in fresh["attempts"]:
        if have.get(attempt["attempt_id"]) != attempt:
            drift.append("index drift for %s" % attempt["attempt_id"])
    for attempt_id in set(have) - {a["attempt_id"] for a in fresh["attempts"]}:
        drift.append("index lists missing attempt %s" % attempt_id)
    return drift


def record_observation(attempt_dir: Path, child_id: str, payload: Dict[str, Any]) -> Path:
    """Linked child receipt; identical redelivery is idempotent, conflict fails."""
    if not _ID.match(child_id):
        raise AttemptError("invalid child id")
    folder = Path(attempt_dir) / "observations"
    folder.mkdir(parents=True, exist_ok=True)
    body = dict(payload, child_id=child_id)
    data = json.dumps(body, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    path = folder / (child_id + ".json")
    try:
        _write_exclusive(path, data)
    except FileExistsError:
        if json.loads(path.read_text("utf-8")) != json.loads(data):
            raise AttemptError("conflicting observation for %s" % child_id)
    return path


def register_in_manifest(manifest: Path, entry: Dict[str, Any]) -> None:
    """Owner-appended manifest (atomic rewrite under a lock); dedupes by attempt_id."""
    manifest = Path(manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with open(str(manifest) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current = json.loads(manifest.read_text("utf-8"))
            if current.get("schema") != MANIFEST_SCHEMA:
                raise AttemptError("manifest schema mismatch")
        except FileNotFoundError:
            current = {"schema": MANIFEST_SCHEMA, "attempts": []}
        except ValueError:
            raise AttemptError("manifest is not valid JSON; refusing to overwrite")
        known = {a["attempt_id"]: a for a in current["attempts"]}
        prior = known.get(entry["attempt_id"])
        if prior is not None and prior != entry:
            raise AttemptError("conflicting manifest entry for %s" % entry["attempt_id"])
        if prior is None:
            current["attempts"].append(entry)
            atomic_write(manifest, json.dumps(current, sort_keys=True, indent=2).encode("utf-8") + b"\n")
