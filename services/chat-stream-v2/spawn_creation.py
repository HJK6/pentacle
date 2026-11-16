"""Local durable evidence about remote creation commands, never pane authority."""
from __future__ import annotations

import json


KEY = "remote_creation"


def initial():
    # Only a freshly persisted intent may initialize a provenance-complete set.
    return {"version": 1, "attempts": []}


def witness(payload):
    try:
        payload = json.loads(payload) if isinstance(payload, str) else payload
        value = payload.get(KEY) if isinstance(payload, dict) else None
        if not isinstance(value, dict) or set(value) != {"version", "attempts"}:
            return None
        if type(value["version"]) is not int or value["version"] != 1:
            return None
        attempts = value["attempts"]
        if not isinstance(attempts, list):
            return None
        seen = set()
        for attempt in attempts:
            if not isinstance(attempt, dict) or set(attempt) != {"id", "owner", "terminal"}:
                return None
            identity = attempt["id"]
            if not isinstance(identity, str) or not identity or identity in seen:
                return None
            # Empty owner is the existing explicitly unowned controller mode;
            # its exact value is still compared on every transaction.
            if not isinstance(attempt["owner"], str) or type(attempt["terminal"]) is not bool:
                return None
            seen.add(identity)
        return value
    except (ValueError, TypeError):
        return None


def terminal(value):
    return value is not None and all(attempt["terminal"] for attempt in value["attempts"])


def cleanup_status(payload):
    value = witness(payload)
    return {
        "state": "pending" if terminal(value) else "unknown",
        "reason": "remote_cleanup_pending" if terminal(value) else "remote_creation_completion_unknown",
        "recovery_owner": "Nexus/FD",
    }
