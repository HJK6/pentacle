"""SYNTHETIC public-contract oracle; never a deployed checker or SMS adapter.

Standard-library fixture for controlled-clock tests. No production imports,
network, subprocess, PID probing, or private configuration. Its injectable caller
is a test double, so accepted replies prove model behavior, never SMS delivery.

Conservative choices where the public prose is not a complete implementation:
missing/invalid observations must persist for five seconds after a valid bind;
age-bearing loop/Store/sample faults reach threshold at age five, without a
second five-second delay. New boots get five-second activation grace, but may
recover an old episode after two distinct healthy one-second samples. Pending
recovery blocks replacement of its opening incident until acknowledged.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import uuid

VERSION = "daemon-progress.v1"
LIMIT = 4096
THRESHOLD = 5.0
FAILURE_STATUSES = {"failed", "undelivered", "canceled", "cancelled", "rejected", "error"}


def finite_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def counter(value):
    return type(value) is int and value >= 0


def validate(value, now):
    """Independent schema/semantic check of the public progress wire format."""
    if not finite_time(now) or not isinstance(value, dict):
        raise ValueError("invalid_document")
    required = {"version", "instance_id", "boot_id", "pid", "sample_mono_s",
                "loop_seq", "loop_mono_s", "store"}
    if set(value) != required or value["version"] != VERSION:
        raise ValueError("invalid_version_or_shape")
    instance = value["instance_id"]
    if not isinstance(instance, str) or not 1 <= len(instance) <= 128:
        raise ValueError("invalid_instance")
    if not isinstance(value["boot_id"], str):
        raise ValueError("invalid_boot")
    try:
        if str(uuid.UUID(value["boot_id"])) != value["boot_id"]:
            raise ValueError("invalid_boot")
    except (ValueError, AttributeError) as error:
        raise ValueError("invalid_boot") from error
    if not counter(value["pid"]) or value["pid"] == 0 or not counter(value["loop_seq"]):
        raise ValueError("invalid_counter")
    sample, loop = value["sample_mono_s"], value["loop_mono_s"]
    if not finite_time(sample) or sample > now or not finite_time(loop) or loop > sample:
        raise ValueError("invalid_time")
    store = value["store"]
    fields = {"enqueued_seq", "started_seq", "finished_seq", "pending_count",
              "oldest_pending_mono_s", "current_started_mono_s"}
    if not isinstance(store, dict) or set(store) != fields:
        raise ValueError("invalid_store_shape")
    if not all(counter(store[name]) for name in fields if name.endswith("seq") or name == "pending_count"):
        raise ValueError("invalid_store_counter")
    enqueued, started, finished = (store[name] for name in ("enqueued_seq", "started_seq", "finished_seq"))
    if not (finished <= started <= enqueued and started - finished <= 1
            and store["pending_count"] == enqueued - started):
        raise ValueError("impossible_store_counter")
    for name, present in (("oldest_pending_mono_s", enqueued > started),
                          ("current_started_mono_s", started > finished)):
        timestamp = store[name]
        if present:
            if not finite_time(timestamp) or timestamp > sample:
                raise ValueError("invalid_store_time")
        elif timestamp is not None:
            raise ValueError("idle_store_has_time")
    return value


def classify(value, now):
    validate(value, now)
    if now - value["sample_mono_s"] >= THRESHOLD:
        return "unavailable"
    loop = now - value["loop_mono_s"] >= THRESHOLD
    store = any(timestamp is not None and now - timestamp >= THRESHOLD
                for timestamp in (value["store"]["current_started_mono_s"],
                                  value["store"]["oldest_pending_mono_s"]))
    return "loop_store" if loop and store else "loop" if loop else "store" if store else None


def _parent_fd(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe_path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("insecure_parent")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _regular_owner_only(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("insecure_file")


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def read_owner_json(path):
    """Actual local-only bounded file read; never follows directory/file links."""
    path = Path(path)
    parent = _parent_fd(path)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            _regular_owner_only(info)
            if info.st_size > LIMIT:
                raise ValueError("oversized_file")
            raw = os.read(descriptor, LIMIT + 1)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    if len(raw) > LIMIT:
        raise ValueError("oversized_file")
    value = json.loads(raw, object_pairs_hook=_no_duplicates)
    if not isinstance(value, dict):
        raise ValueError("invalid_document")
    return value


class AtomicJournal:
    """Real local owner-only atomic JSON persistence for synthetic state only."""
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return read_owner_json(self.path)
        except FileNotFoundError:
            return None

    def save(self, value):
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > LIMIT:
            raise ValueError("bounded_journal_exceeded")
        parent = _parent_fd(self.path)
        temporary = ".reference-" + uuid.uuid4().hex
        try:
            try:
                _regular_owner_only(os.stat(self.path.name, dir_fd=parent, follow_symlinks=False))
            except FileNotFoundError:
                pass
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)


class DefinitelyUnsubmitted(Exception):
    """Allowed only from the separate local preflight, before caller invocation."""


class SyntheticChecker:
    """Bounded state-machine fixture; clock/caller supplied exclusively by tests."""
    def __init__(self, journal, clock):
        self.journal, self.clock = journal, clock
        saved = journal.load()
        self.state = saved if saved is not None else {
            "version": "synthetic-checker-journal.v1", "ordinal": 0,
            "instance_id": None, "boot_id": None, "boot_seen": None,
            "last_snapshot": None, "last_observed": None, "healthy_count": 0,
            "unavailable_since": None, "current_cause": None,
            "active": None, "recovery": None, "last_terminal": None,
        }
        if self.state.get("version") != "synthetic-checker-journal.v1":
            raise ValueError("unknown_journal")
        # Persisted monotonic values are meaningful only on this host boot.
        # A regressed clock is unavailable until a new daemon boot is observed.
        self.state["healthy_count"] = 0
        for condition in ("active", "recovery"):
            row = self.state[condition]
            if row and row["delivery"]["state"] == "inflight":
                row["delivery"]["state"] = "submission_unknown"
        self._save()

    def _save(self):
        self.journal.save(self.state)

    @property
    def active(self):
        return self.state["active"]

    @property
    def recovery(self):
        return self.state["recovery"]

    def poll_file(self, path):
        try:
            value = read_owner_json(path)
        except (OSError, ValueError, UnicodeError):
            value = None
        return self.observe(value)

    def _event(self, condition, cause, source=None):
        if source is None:
            self.state["ordinal"] += 1
            boot, ordinal = self.state["boot_id"], self.state["ordinal"]
            identity = hashlib.sha256(
                f'{self.state["instance_id"]}\0{boot}\0{ordinal}'.encode()).hexdigest()
        else:
            boot, ordinal, identity = (source[name] for name in ("boot_id", "ordinal", "episode_id"))
        return {"boot_id": boot, "ordinal": ordinal, "episode_id": identity,
                "condition": condition, "cause": cause,
                "delivery": {"state": "pending", "attempts": 0, "next_attempt": 0,
                             "sid": None, "status": None}}

    def _activate(self, cause, now):
        if self.active is not None and self.recovery is None:
            # Handoff diagnostics evolve; delivery state prevents re-submission
            # of a previously accepted/unknown active event with a new payload.
            self.active["cause"] = cause
        if self.active is None and now - self.state["boot_seen"] >= THRESHOLD:
            self.state["active"] = self._event("active", cause)

    def _unavailable(self, now):
        self.state["current_cause"] = "unavailable"
        self.state["healthy_count"] = 0
        if self.state["instance_id"] is None:
            self._save()
            return "unbound"
        if self.state["unavailable_since"] is None:
            self.state["unavailable_since"] = now
        if now - self.state["unavailable_since"] >= THRESHOLD:
            self._activate("unavailable", now)
        self._save()
        return "unavailable"

    def observe(self, value):
        now = self.clock()
        try:
            validate(value, now)
            if self.state["instance_id"] not in (None, value["instance_id"]):
                raise ValueError("installation_changed")
            previous = self.state["last_snapshot"]
            same_boot = self.state["boot_id"] == value["boot_id"]
            if previous is not None and same_boot:
                if (self.state["last_observed"] is not None and now < self.state["last_observed"]):
                    raise ValueError("host_clock_regressed")
                for name in ("sample_mono_s", "loop_seq", "loop_mono_s"):
                    if value[name] < previous[name]:
                        raise ValueError("progress_regressed")
                for name in ("enqueued_seq", "started_seq", "finished_seq"):
                    if value["store"][name] < previous["store"][name]:
                        raise ValueError("store_progress_regressed")
            if not same_boot:
                self.state.update(instance_id=value["instance_id"], boot_id=value["boot_id"],
                                  boot_seen=now, healthy_count=0, last_snapshot=None,
                                  unavailable_since=None, last_observed=None)
                previous = None
        except (ValueError, TypeError, KeyError):
            return self._unavailable(now)
        cause = classify(value, now)
        fresh = (previous is None or (
            value["sample_mono_s"] > previous["sample_mono_s"]
            and value["loop_seq"] > previous["loop_seq"]
            and value["loop_mono_s"] > previous["loop_mono_s"]
            and now - self.state["last_observed"] >= 1.0))
        self.state["current_cause"] = cause
        self.state["unavailable_since"] = None
        if cause:
            self.state["healthy_count"] = 0
            self._activate(cause, now)
        else:
            self.state["healthy_count"] = min(2, self.state["healthy_count"] + 1) if fresh else 0
            if self.state["healthy_count"] >= 2 and self.active and self.recovery is None:
                self.state["recovery"] = self._event("recovered", self.active["cause"], self.active)
        self.state["last_snapshot"] = copy.deepcopy(value)
        self.state["last_observed"] = now
        self._save()
        return cause or "healthy"

    def deliver(self, condition, caller, preflight=None):
        """At most one synthetic invocation. An invoked caller can never retry."""
        if condition not in ("active", "recovery"):
            raise ValueError("invalid_condition")
        row = self.state[condition]
        if row is None:
            return "absent"
        delivery = row["delivery"]
        if delivery["state"] not in ("pending", "retry_wait"):
            return delivery["state"]
        if condition == "recovery" and self.active["delivery"]["state"] != "accepted":
            return "blocked_on_incident"
        if self.clock() < delivery["next_attempt"]:
            return "retry_wait"
        if delivery["attempts"] >= 3:
            delivery["state"] = "exhausted"
            self._save()
            return "exhausted"
        delivery["attempts"] += 1
        self._save()  # Attempt budget survives a preflight crash/restart.
        try:
            if preflight:
                preflight()
        except DefinitelyUnsubmitted:
            exhausted = delivery["attempts"] >= 3
            delivery["state"] = "exhausted" if exhausted else "retry_wait"
            if not exhausted:
                delivery["next_attempt"] = self.clock() + (2, 5)[delivery["attempts"] - 1]
            self._save()
            return delivery["state"]
        except Exception:
            delivery["state"] = "submission_unknown"
            self._save()
            return delivery["state"]
        delivery["state"] = "inflight"
        self._save()  # Failure here MUST prevent caller invocation.
        body = {key: row[key] for key in ("episode_id", "condition", "cause")}
        try:
            result = caller(body)
            if isinstance(result, str):
                result = json.loads(result)
            if (not isinstance(result, dict) or not isinstance(result.get("sid"), str)
                    or not result["sid"].strip() or not isinstance(result.get("status"), str)
                    or not result["status"].strip() or result["status"].strip().lower() in FAILURE_STATUSES):
                raise ValueError("uncertain_caller_result")
            delivery.update(state="accepted", sid=result["sid"], status=result["status"])
            self._save()
        except Exception:
            # Includes storage failure after acceptance. Disk may still contain
            # inflight; both that and this state are restart-safe unknowns.
            delivery.update(state="submission_unknown", sid=None, status=None)
            try:
                self._save()
            except OSError:
                pass
        return delivery["state"]

    def acknowledge_terminal(self, identity):
        if not self.active or not self.recovery or identity != self.active["episode_id"]:
            return False
        if any(row["delivery"]["state"] != "accepted" for row in (self.active, self.recovery)):
            return False
        self.state["last_terminal"] = {
            "episode_id": identity, "ordinal": self.active["ordinal"],
            "boot_id": self.active["boot_id"],
            "incident_sid": self.active["delivery"]["sid"],
            "recovery_sid": self.recovery["delivery"]["sid"],
        }
        self.state["active"] = self.state["recovery"] = None
        self._save()
        return True

    def handoff(self):
        def wire(row):
            return None if row is None else {key: value for key, value in row.items() if key != "delivery"}
        return {"version": "daemon-episodes.v1", "instance_id": self.state["instance_id"],
                "active": wire(self.active), "recovery": wire(self.recovery)}
