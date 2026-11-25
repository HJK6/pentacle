"""Serialize quarantine decisions and retry relay and activation notices.

Adapters must make their operations idempotent by the supplied stable keys.
The persisted intent precedes every relay or notification, so a lost reply is
safe to retry. The effective list is supplied from the authoritative reader.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .reliability_receipts import instant


def _key(kind, value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return kind + ":" + hashlib.sha256(raw).hexdigest()


def _save(path, state):
    fd, name = tempfile.mkstemp(prefix=".quarantine-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(state, out, sort_keys=True, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def reconcile(state_path, occurrence_map, effective, adapter, *, restorations=None, history=()):
    restorations = restorations or {}
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {"version": 1, "tests": {}}
        if state.get("version") != 1 or not isinstance(state.get("tests"), dict):
            raise ValueError("invalid quarantine transition state")
        pending = {}
        # Remember reviewed membership without inventing a decision. A removed
        # row needs a verified restoration before old evidence can recur.
        for test in effective:
            state["tests"].setdefault(test, {"phase": "adopted"})
        _save(path, state)
        for test, occurrences in sorted(occurrence_map.items()):
            unique = {}
            for occurrence in occurrences:
                oid = occurrence.get("id")
                if not isinstance(oid, str) or not oid or not occurrence.get("source"):
                    raise ValueError("invalid quarantine occurrence")
                if oid in unique and unique[oid] != occurrence:
                    raise ValueError("conflicting quarantine occurrence")
                unique[oid] = occurrence
            if test in effective and not state["tests"][test].get("decision"):
                continue
            if len(unique) < 2:
                continue
            if test.startswith("test/"):
                adapter.alert(_key("node-recurrence", test), "Node test needs review: " + test)
                continue
            all_occurrences = sorted(unique.values(), key=lambda item: item["id"])
            chosen = all_occurrences[:2]
            key = _key("activation", [test, [item["id"] for item in chosen]])
            entry = state["tests"].get(test)
            previous = None
            if test not in effective and ((entry and entry.get("phase") in ("adopted", "notified"))
                                          or (entry is None and test in history)):
                restoration = restorations.get(test)
                try:
                    if (not restoration or not adapter.verify_restoration(test, restoration)
                            or any(instant(item["created"]) <= instant(restoration["at"]) for item in chosen)
                            or (entry is not None and entry.get("key") == key)):
                        raise ValueError("verified restoration and new series required")
                except (KeyError, ValueError, TypeError, AttributeError):
                    adapter.alert(_key("restoration", test), f"Restoration or recurrence evidence incomplete for {test}")
                    continue
                previous, entry = entry or {"phase": "adopted"}, None
            try:
                decision = adapter.decision(test, all_occurrences)
                if (not all(decision.get(k) for k in ("item", "owner", "coverage"))
                        or (entry and entry["decision"]["item"] != decision["item"])):
                    raise ValueError("decision incomplete or mismatched")
            except Exception as exc:
                adapter.alert(_key("decision", test), f"Decision incomplete for {test}: {type(exc).__name__}")
                continue
            if entry is None:
                entry = {"key": key, "decision": decision, "occurrences": chosen,
                         "phase": "adopted" if test in effective else "proposed"}
                if previous:
                    entry["previous"] = previous
                    entry["restoration"] = restoration
                state["tests"][test] = entry
                _save(path, state)
            else:
                entry["decision"] = decision
            if test in effective:
                if entry["phase"] in ("proposed", "prepared", "queued"):
                    payload = {"test": test, **entry["decision"],
                               "occurrences": [item["source"] for item in entry["occurrences"]],
                               "relay": entry.get("relay")}
                    try:
                        adapter.notify(entry["key"], payload)
                    except Exception as exc:
                        adapter.alert(_key("notice", entry["key"]), f"Activation notice incomplete: {type(exc).__name__}")
                        continue
                    entry["phase"] = "notified"
                    _save(path, state)
            else:
                pending[test] = entry
        if pending:
            desired = sorted(set(effective) | set(pending))
            key = _key("relay", desired)
            try:
                receipt = adapter.prepare_relay(desired, key, pending)
            except Exception as exc:
                adapter.alert(_key("relay-alert", key), f"Quarantine relay incomplete: {type(exc).__name__}")
            else:
                for entry in pending.values():
                    entry["phase"] = "prepared"
                    entry["relay"] = receipt
                _save(path, state)
                for test, entry in pending.items():
                    try:
                        adapter.send_relay(test, entry)
                    except Exception as exc:
                        adapter.alert(_key("relay-notice", test), f"Quarantine relay notice incomplete: {type(exc).__name__}")
                        continue
                    entry["phase"] = "queued"
                    _save(path, state)
        return state
