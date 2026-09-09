"""Durable notification store for the Pentacle notifications foundation.

This is the storage layer for operator-facing notifications produced by the
daemon and its subsystems (schedule failures, recovery prompts, etc.). It is
intentionally self-contained — it does not import the daemon, emit
observability events, or know anything about the WebSocket broadcast layer.
Later stages (chat_streamd wiring + renderer) consume these records; this
module only persists them.

Storage idiom matches ``schedule_store.py`` / ``session_store.py``:

* a SQLite file co-located under ``~/.local/share/pentacle-stream`` with a
  ``PENTACLE_STREAM_NOTIFICATIONS_DB`` env override;
* WAL journal mode (skipped for ``:memory:``);
* ``PRAGMA integrity_check`` on open with a ``NotificationStoreUnhealthy``
  raise so a caller can convert it into an observability event;
* a single ``threading.Lock`` around the shared connection
  (``check_same_thread=False``) so the daemon's asyncio loop and sync callers
  can share it.

Notifications have a small lifecycle. They are born ``open`` and move to
exactly one terminal state when an operator (or the system) resolves them:

    open ──▶ acked        (ack action)
        ├──▶ answered     (yes_no action)
        ├──▶ spawned      (spawn_worker action)
        ├──▶ resolved     (generic resolve)
        └──▶ expired      (TTL elapsed; system-driven)

``dedup_key`` lets a producer collapse repeated notifications about the same
condition into a single ``open`` row (partial unique index on
``(producer, dedup_key) WHERE state='open'``). Re-creating an open dedup row
refreshes its presentation fields rather than inserting a duplicate.

Retention is bounded by ``prune_resolved`` so the table does not grow without
limit. ``open`` rows are never pruned.
"""

from __future__ import annotations

import json
import math
import numbers
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DB_PATH = Path.home() / ".local/share/pentacle-stream/notifications.db"
DB_PATH_ENV = "PENTACLE_STREAM_NOTIFICATIONS_DB"
RESOLVED_KEEP_ENV = "PENTACLE_NOTIFICATIONS_RESOLVED_KEEP"
RESOLVED_MAX_AGE_DAYS_ENV = "PENTACLE_NOTIFICATIONS_RESOLVED_MAX_AGE_DAYS"

DEFAULT_RESOLVED_KEEP = 200
DEFAULT_RESOLVED_MAX_AGE_DAYS = 30
DEFAULT_RESURFACE_SECONDS = 24 * 60 * 60
DEFAULT_FIRING_HISTORY_LIMIT = 50
DEFAULT_NOTIFICATION_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_CRITICAL_NOTIFICATION_TTL_SECONDS = 30 * 24 * 60 * 60


# Notification lifecycle states.
STATE_OPEN = "open"
STATE_ACKED = "acked"
STATE_ANSWERED = "answered"
STATE_SPAWNED = "spawned"
STATE_RESOLVED = "resolved"
STATE_EXPIRED = "expired"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_INDETERMINATE = "indeterminate"

# Everything except ``open``/``running`` is terminal (resolved/closed). The
# store never re-opens a row; resolution is a one-way transition.
TERMINAL_STATES = frozenset(
    {
        STATE_ACKED,
        STATE_ANSWERED,
        STATE_SPAWNED,
        STATE_RESOLVED,
        STATE_EXPIRED,
        STATE_DONE,
        STATE_FAILED,
        STATE_INDETERMINATE,
    }
)
ALL_STATES = frozenset({STATE_OPEN, STATE_RUNNING}) | TERMINAL_STATES

SEVERITIES = frozenset({"info", "warning", "critical"})

# Action kinds an operator can take on an open notification.
ACTION_KINDS = frozenset({"ack", "yes_no", "spawn_worker", "run_command", "resolved"})

# ``resolve_notification`` accepts the operator action kinds plus the generic
# ``resolved`` kind (a dismiss with no specific action attached).
RESOLVE_ACTION_KINDS = (ACTION_KINDS - frozenset({"run_command"})) | frozenset(
    {"resolved"}
)

# action_kind -> terminal state mapping for resolve_notification.
_ACTION_TO_STATE = {
    "ack": STATE_ACKED,
    "yes_no": STATE_ANSWERED,
    "spawn_worker": STATE_SPAWNED,
    "resolved": STATE_RESOLVED,
}


# Columns in stable order for SELECT and dict conversion. Kept module-level so
# tests and schema-introspection callers can assert the shape without touching
# internal state.
NOTIFICATION_COLUMNS = (
    "notification_id",
    "created_at",
    "updated_at",
    "producer",
    "severity",
    "title",
    "body",
    "dedup_key",
    "state",
    "actions",
    "resolution",
    "ttl_seconds",
    "expires_at",
    "resolved_at",
    "answer_to_stream_id",
    "first_fired_at",
    "last_fired_at",
    "firing_count",
    "firing_history",
    "last_resurfaced_at",
)

QUESTION_STATE_OPEN = "open"
QUESTION_STATE_ANSWERED = "answered"
QUESTION_STATE_CONSUMED = "consumed"
QUESTION_STATE_DISMISSED = "dismissed"
QUESTION_STATE_EXPIRED = "expired"
QUESTION_STATES = frozenset(
    {
        QUESTION_STATE_OPEN,
        QUESTION_STATE_ANSWERED,
        QUESTION_STATE_CONSUMED,
        QUESTION_STATE_DISMISSED,
        QUESTION_STATE_EXPIRED,
    }
)

AGENT_QUESTION_COLUMNS = (
    "question_id",
    "schema_version",
    "created_at",
    "updated_at",
    "answered_at",
    "producer_stream_id",
    "producer_provider",
    "producer_session_generation",
    "spec_id",
    "dedup_key",
    "notification_id",
    "state",
    "envelope",
    "answer",
)

# D3 (daemon_updates_2026_09): the one-time bounce-A open-question clear stamps
# this marker inside the same write transaction as the expiries, so a retry
# after commit is a no-op and post-clear questions are never swept.
QUESTION_CLEAR_MARKER_A = "daemon_updates_A_question_clear_v1"


class NotificationStoreError(Exception):
    """Base for notification store errors."""


class NotificationStoreUnhealthy(NotificationStoreError):
    """Raised when ``PRAGMA integrity_check`` fails on store open.

    Callers should translate this into an observability event signalling the
    notification store is unsafe to use.
    """


class NotificationNotFound(NotificationStoreError):
    """Raised when an operation targets a non-existent ``notification_id``."""


class NotificationTerminalState(NotificationStoreError):
    """Raised when ``resolve_notification`` targets an already-resolved row.

    ``args[0]`` carries the current (terminal) state for the caller's
    convenience.
    """


class NotificationResolutionConflict(NotificationStoreError):
    """Raised when a notification id is replayed with different canonical intent."""


class NotificationResolutionInProgress(NotificationStoreError):
    """Raised when an identical external-effect resolution is still claimed."""


class InvalidNotification(NotificationStoreError):
    """Raised on invalid create/resolve input (bad severity, action, etc.)."""


def _default_path() -> str:
    return os.environ.get(DB_PATH_ENV) or str(DEFAULT_DB_PATH)


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _iso_now() -> str:
    """ISO-8601 UTC timestamp with a trailing ``Z``.

    Matches ``schedule_store._utc_now_iso`` so timestamps are uniform across
    the shared pentacle-stream DB ecosystem. All internal comparisons go
    through :func:`_parse_iso`, which tolerates both ``Z`` and explicit
    offsets, so the shape change is purely cosmetic for correctness.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    # Tolerate a trailing ``Z`` as well as explicit offsets so seeded test
    # timestamps and production values both compare correctly.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso_z(dt: datetime) -> str:
    """Render a datetime as ISO-8601 with a trailing ``Z`` (house shape)."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_ttl_seconds(ttl_seconds: Any) -> int | None:
    """Validate ``ttl_seconds`` and normalize "no expiry" to ``None``.

    * ``bool`` is rejected explicitly (``True``/``False`` are ``int`` in
      Python and would otherwise coerce to a ttl of 1/0).
    * negative values are rejected.
    * ``0`` and ``None`` both mean "no expiry" and collapse to ``None`` so the
      stored column reflects intent (NULL) rather than a coerced sentinel.
    """
    if ttl_seconds is None:
        return None
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise InvalidNotification(
            f"ttl_seconds must be an int or None; got {type(ttl_seconds).__name__}"
        )
    if ttl_seconds < 0:
        raise InvalidNotification(f"ttl_seconds must be >= 0; got {ttl_seconds}")
    return ttl_seconds or None


def _default_ttl_seconds_for(severity: str) -> int:
    if severity == "critical":
        return DEFAULT_CRITICAL_NOTIFICATION_TTL_SECONDS
    return DEFAULT_NOTIFICATION_TTL_SECONDS


def _validate_spawn_worker_action(action: dict) -> None:
    if action.get("kind") != "spawn_worker":
        raise InvalidNotification("on_failure must be a spawn_worker action")
    if not action.get("provider"):
        raise InvalidNotification("spawn_worker action requires a 'provider'")
    if not (action.get("prompt") or action.get("spec_id")):
        raise InvalidNotification(
            "spawn_worker action requires one of 'prompt' or 'spec_id'"
        )


def _validate_run_command_action(action: dict) -> None:
    command_id = action.get("command_id")
    if not isinstance(command_id, str) or not command_id.strip():
        raise InvalidNotification("run_command action requires a non-empty 'command_id'")
    if "args" in action and not isinstance(action["args"], dict):
        raise InvalidNotification("run_command action 'args' must be an object")
    if "timeout_seconds" in action:
        timeout_seconds = action["timeout_seconds"]
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, numbers.Real)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise InvalidNotification(
                "run_command action 'timeout_seconds' must be a positive number"
            )
    if "on_failure" in action:
        on_failure = action["on_failure"]
        if not isinstance(on_failure, dict):
            raise InvalidNotification("run_command action 'on_failure' must be an object")
        _validate_spawn_worker_action(on_failure)


def _validate_run_command_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise InvalidNotification("run_command result must be a dict")
    required = {
        "command_id",
        "exit_code",
        "stdout_tail",
        "stderr_tail",
        "timed_out",
        "ran_at",
    }
    missing = required - set(result)
    if missing:
        missing_keys = ", ".join(sorted(missing))
        raise InvalidNotification(f"run_command result missing keys: {missing_keys}")
    if not isinstance(result["command_id"], str) or not result["command_id"].strip():
        raise InvalidNotification("run_command result command_id must be non-empty")
    if result["exit_code"] is not None and (
        isinstance(result["exit_code"], bool) or not isinstance(result["exit_code"], int)
    ):
        raise InvalidNotification("run_command result exit_code must be an int or null")
    if not isinstance(result["stdout_tail"], str):
        raise InvalidNotification("run_command result stdout_tail must be a string")
    if not isinstance(result["stderr_tail"], str):
        raise InvalidNotification("run_command result stderr_tail must be a string")
    if not isinstance(result["timed_out"], bool):
        raise InvalidNotification("run_command result timed_out must be a bool")
    if not isinstance(result["ran_at"], str) or not result["ran_at"].strip():
        raise InvalidNotification("run_command result ran_at must be non-empty")
    if "spawned_stream_id" in result and not isinstance(result["spawned_stream_id"], str):
        raise InvalidNotification("run_command result spawned_stream_id must be a string")
    return dict(result)


def _validate_actions(actions: Any) -> list[dict]:
    """Validate and normalize an actions list. Returns a list of dicts.

    Each action must be a dict with ``kind`` in :data:`ACTION_KINDS`. A
    ``spawn_worker`` action must additionally carry ``provider`` and at least
    one of ``prompt`` / ``spec_id``.
    """
    if actions is None:
        return []
    if not isinstance(actions, (list, tuple)):
        raise InvalidNotification("actions must be a list of action dicts")
    normalized: list[dict] = []
    seen_action_ids: set[str] = set()
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise InvalidNotification(f"action must be a dict; got {type(action).__name__}")
        kind = action.get("kind")
        if kind not in ACTION_KINDS:
            raise InvalidNotification(f"invalid action kind: {kind!r}")
        if kind == "spawn_worker":
            _validate_spawn_worker_action(action)
        if kind == "run_command":
            _validate_run_command_action(action)
        normalized_action = dict(action)
        if "action_id" in normalized_action:
            action_id = normalized_action["action_id"]
            if not isinstance(action_id, str) or not action_id.strip():
                raise InvalidNotification("action_id must be a non-empty string")
            action_id = action_id.strip()
        else:
            action_id = f"a{index}"
        if action_id in seen_action_ids:
            raise InvalidNotification(f"duplicate action_id: {action_id!r}")
        seen_action_ids.add(action_id)
        normalized_action["action_id"] = action_id
        normalized.append(normalized_action)
    return normalized


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidNotification(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


_ACTOR_PROVENANCE_KEYS = (
    "actor_class",
    "actor_stream_id",
    "actor_client",
    "actor_verified",
    "claimed_by",
)


def _normalize_actor_provenance(value: Any) -> dict[str, Any]:
    """Keep the additive actor fields closed and JSON-safe at the store edge."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidNotification("actor_provenance must be an object")
    normalized: dict[str, Any] = {}
    for key in _ACTOR_PROVENANCE_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key in {"actor_class", "actor_stream_id", "actor_client", "claimed_by"}:
            if item is not None and not isinstance(item, str):
                raise InvalidNotification(f"{key} must be a string or null")
            if isinstance(item, str):
                item = item.strip() or None
        elif key == "actor_verified" and not isinstance(item, bool):
            raise InvalidNotification("actor_verified must be a bool")
        if key == "claimed_by" and item is None:
            continue
        normalized[key] = item
    return normalized


def _with_legacy_actor_provenance(value: dict[str, Any]) -> dict[str, Any]:
    """Expose old rows as unknown/system without rewriting their JSON."""
    normalized = dict(value)
    # ``run_command`` claims are a separate in-progress state and use
    # ``claimed_by``/``claimed_at`` rather than user-resolution ``by``/``at``.
    if "by" not in normalized and "claimed_by" in normalized:
        return normalized
    actor_class = normalized.get("actor_class")
    if not isinstance(actor_class, str) or not actor_class:
        prior_by = normalized.get("by")
        action_kind = str(normalized.get("action_kind") or "")
        normalized["actor_class"] = (
            "system" if prior_by == "system" or action_kind == "expired" else "legacy_unknown"
        )
    normalized.setdefault("actor_stream_id", None)
    normalized.setdefault("actor_client", None)
    normalized.setdefault("actor_verified", False)
    return normalized


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a row into the public dict shape, decoding JSON columns.

    ``actions`` decodes to a list (default ``[]``) and ``resolution`` decodes
    to a dict or ``None``.
    """
    record = dict(row)
    raw_actions = record.get("actions")
    if raw_actions is None or raw_actions == "":
        record["actions"] = []
    else:
        decoded = json.loads(raw_actions)
        record["actions"] = decoded if isinstance(decoded, list) else []
    raw_resolution = record.get("resolution")
    if raw_resolution is None or raw_resolution == "":
        record["resolution"] = None
    else:
        decoded = json.loads(raw_resolution)
        record["resolution"] = (
            _with_legacy_actor_provenance(decoded) if isinstance(decoded, dict) else None
        )
    raw_history = record.get("firing_history")
    if raw_history is None or raw_history == "":
        record["firing_history"] = []
    else:
        decoded = json.loads(raw_history)
        record["firing_history"] = decoded if isinstance(decoded, list) else []
    if record.get("firing_count") is None:
        record["firing_count"] = len(record["firing_history"]) or 1
    record.setdefault("should_resurface", False)
    return record


def _append_firing_history(raw_history: str | None, ts: str) -> tuple[list[str], str]:
    history: list[str] = []
    if raw_history:
        try:
            decoded = json.loads(raw_history)
        except json.JSONDecodeError:
            decoded = []
        if isinstance(decoded, list):
            history = [str(item) for item in decoded if isinstance(item, str) and item]
    history.append(ts)
    history = history[-DEFAULT_FIRING_HISTORY_LIMIT:]
    return history, json.dumps(history, separators=(",", ":"))


def _resurface_due(last_resurfaced_at: str | None, ts: str) -> bool:
    if not last_resurfaced_at:
        return True
    try:
        return (_parse_iso(ts) - _parse_iso(last_resurfaced_at)).total_seconds() >= DEFAULT_RESURFACE_SECONDS
    except (TypeError, ValueError):
        return True


def _question_row_to_dict(row: sqlite3.Row) -> dict:
    record = dict(row)
    raw_envelope = record.get("envelope")
    if raw_envelope is None or raw_envelope == "":
        record["envelope"] = {}
    else:
        decoded = json.loads(raw_envelope)
        record["envelope"] = decoded if isinstance(decoded, dict) else {}
    raw_answer = record.get("answer")
    if raw_answer is None or raw_answer == "":
        record["answer"] = None
    else:
        decoded = json.loads(raw_answer)
        record["answer"] = (
            _with_legacy_actor_provenance(decoded) if isinstance(decoded, dict) else None
        )
    return record


def _agent_question_response_mode(question: dict) -> str:
    envelope = question.get("envelope") if isinstance(question.get("envelope"), dict) else {}
    return str(envelope.get("response_mode") or "")


def _agent_question_option_values(question: dict) -> set[str]:
    envelope = question.get("envelope") if isinstance(question.get("envelope"), dict) else {}
    values: set[str] = set()
    for option in envelope.get("options") or []:
        if isinstance(option, dict) and isinstance(option.get("value"), str):
            values.add(str(option["value"]))
    return values


def _validate_agent_question_selections(
    question: dict, selections: Any, note: Any
) -> list[str]:
    if note is not None and not isinstance(note, str):
        raise InvalidNotification("note must be a string")
    if not isinstance(selections, list):
        raise InvalidNotification("selections must be a list")
    normalized = selections[:]
    if not normalized:
        raise InvalidNotification("selections must contain at least one value")
    if any(not isinstance(item, str) or not item for item in normalized):
        raise InvalidNotification("selections must contain non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise InvalidNotification("selections must be unique")
    option_values = _agent_question_option_values(question)
    invalid = [item for item in normalized if item not in option_values]
    if invalid:
        raise InvalidNotification("selections must be option values for the question")
    response_mode = _agent_question_response_mode(question)
    if response_mode == "single_choice" and len(normalized) != 1:
        raise InvalidNotification("single_choice questions require exactly one selection")
    if response_mode == "multi_choice" and len(normalized) < 1:
        raise InvalidNotification("multi_choice questions require at least one selection")
    if response_mode not in {"single_choice", "multi_choice"}:
        raise InvalidNotification("question response_mode is invalid")
    return normalized


def _normalize_agent_question_text(question: dict, answer: dict[str, Any]) -> str:
    text = answer.get("text")
    if not isinstance(text, str):
        raise InvalidNotification("answer text is required")
    if not text.strip():
        raise InvalidNotification("answer text must be non-empty")
    return text


def _derive_agent_question_selections(answer: dict[str, Any]) -> list[str] | None:
    raw_selections = answer.get("selections")
    if raw_selections is not None:
        return raw_selections if isinstance(raw_selections, list) else []
    raw_value = answer.get("value")
    if isinstance(raw_value, dict) and isinstance(raw_value.get("answer"), str):
        return [raw_value["answer"]]
    if isinstance(raw_value, str):
        return [raw_value]
    return None


def _normalize_agent_question_answer(question: dict, answer: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(answer, dict):
        raise InvalidNotification("answer must be an object")
    response_mode = _agent_question_response_mode(question)
    note = answer.get("note")
    if note is not None and not isinstance(note, str):
        raise InvalidNotification("note must be a string")
    custom_text = answer.get("custom_text")
    if custom_text is not None:
        if not isinstance(custom_text, str):
            raise InvalidNotification("custom_text must be a string")
        if not custom_text.strip():
            raise InvalidNotification("custom_text must be non-empty")
    if response_mode == "free_text":
        if custom_text is not None:
            raise InvalidNotification("custom_text is only valid for choice questions")
        normalized = dict(answer)
        normalized["text"] = _normalize_agent_question_text(question, answer)
        normalized["note"] = note if note is not None else None
        normalized["selections"] = []
        return normalized
    if answer.get("text") is not None:
        raise InvalidNotification("text is only valid for free_text questions")
    if custom_text is not None:
        envelope = question.get("envelope") if isinstance(question.get("envelope"), dict) else {}
        if envelope.get("allow_custom") is not True:
            raise InvalidNotification("custom_text is not allowed for this question")
        if response_mode not in {"single_choice", "multi_choice"}:
            raise InvalidNotification("custom_text is only valid for choice questions")
    selections = _derive_agent_question_selections(answer)
    if custom_text is not None and selections == []:
        selections = None
    if selections is None and custom_text is None:
        raise InvalidNotification("answer selections are required")
    normalized_selections = (
        _validate_agent_question_selections(question, selections, note)
        if selections is not None
        else []
    )
    normalized = dict(answer)
    normalized["selections"] = normalized_selections
    normalized["note"] = note if note is not None else None
    if custom_text is not None:
        normalized["custom_text"] = custom_text
    if response_mode == "single_choice" and normalized_selections:
        first = normalized_selections[0]
        normalized.setdefault(
            "value",
            {
                "schema_version": 1,
                "question_id": question.get("question_id"),
                "answer": first,
            },
        )
        if not isinstance(normalized.get("choice"), bool):
            normalized["choice"] = True
    return normalized


def _canonical_selections(question: dict | None, selections: list[str] | None) -> list[str]:
    values = list(selections or [])
    if not question or not values:
        return values
    envelope = question.get("envelope") if isinstance(question.get("envelope"), dict) else {}
    order = {
        str(option.get("value")): index
        for index, option in enumerate(envelope.get("options") or [])
        if isinstance(option, dict) and isinstance(option.get("value"), str)
    }
    return sorted(values, key=lambda value: order.get(value, len(order)))


def _canonical_resolution_intent(
    *,
    notification_id: str,
    action_kind: str,
    action_id: str | None,
    choice: bool | None,
    value: Any,
    selections: list[str] | None,
    text: str | None,
    custom_text: str | None,
    note: str | None,
    effective_spawn_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "notification_id": notification_id,
        "action_id": action_id,
        "action_kind": action_kind,
        "choice": choice,
        "value": value,
        "selections": list(selections or []),
        "text": text,
        "custom_text": custom_text,
        "note": note,
        "effective_spawn_spec": effective_spawn_spec,
    }


def _intent_from_resolution(notification_id: str, resolution: dict[str, Any]) -> dict[str, Any]:
    canonical = resolution.get("canonical_intent")
    if isinstance(canonical, dict):
        return canonical
    return _canonical_resolution_intent(
        notification_id=notification_id,
        action_kind=str(resolution.get("action_kind") or ""),
        action_id=resolution.get("action_id") if isinstance(resolution.get("action_id"), str) else None,
        choice=resolution.get("choice") if isinstance(resolution.get("choice"), bool) else None,
        value=resolution.get("value"),
        selections=resolution.get("selections") if isinstance(resolution.get("selections"), list) else None,
        text=resolution.get("text") if isinstance(resolution.get("text"), str) else None,
        custom_text=resolution.get("custom_text") if isinstance(resolution.get("custom_text"), str) else None,
        note=resolution.get("note") if isinstance(resolution.get("note"), str) else None,
        effective_spawn_spec=(
            resolution.get("effective_spawn_spec")
            if isinstance(resolution.get("effective_spawn_spec"), dict)
            else None
        ),
    )


def _notification_resolution_payload(
    notification_id: str,
    resolution: dict[str, Any],
    *,
    terminal_state: str,
) -> dict[str, Any]:
    resolution = _with_legacy_actor_provenance(resolution)
    payload: dict[str, Any] = {
        "notification_id": notification_id,
        "action_kind": str(resolution.get("action_kind") or terminal_state),
        "by": resolution.get("by") or resolution.get("claimed_by") or "system",
        "at": resolution.get("at") or resolution.get("claimed_at"),
        "state": terminal_state,
    }
    for key in (
        "action_id",
        "label",
        "note",
        "choice",
        "selections",
        "custom_text",
        "value",
        "spawned_stream_id",
        "result",
        "actor_class",
        "actor_stream_id",
        "actor_client",
        "actor_verified",
        "claimed_by",
    ):
        if key in resolution:
            payload[key] = resolution[key]
    return payload


class NotificationStore:
    """SQLite-backed durable notification store.

    Thread-safe via a single ``threading.Lock`` around the connection. The
    connection is opened with ``check_same_thread=False`` so the daemon's
    asyncio loop and any sync callers can share it. ``:memory:`` is supported
    for tests (WAL is skipped for it).

    Retention bounds (``resolved_keep`` / ``resolved_max_age_days``) follow the
    precedence: explicit constructor arg > env override > hardcoded default.
    """

    DEFAULT_DB_PATH = DEFAULT_DB_PATH

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        resolved_keep: int | None = None,
        resolved_max_age_days: int | None = None,
    ) -> None:
        self.path = str(path or _default_path())

        if resolved_keep is not None:
            self.resolved_keep = int(resolved_keep)
        else:
            env_keep = _env_int(RESOLVED_KEEP_ENV)
            self.resolved_keep = env_keep if env_keep is not None else DEFAULT_RESOLVED_KEEP

        if resolved_max_age_days is not None:
            self.resolved_max_age_days = int(resolved_max_age_days)
        else:
            env_age = _env_int(RESOLVED_MAX_AGE_DAYS_ENV)
            self.resolved_max_age_days = (
                env_age if env_age is not None else DEFAULT_RESOLVED_MAX_AGE_DAYS
            )

        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            integrity_rows = self._conn.execute("PRAGMA integrity_check").fetchall()
            integrity = [str(row[0]) for row in integrity_rows]
            if integrity != ["ok"]:
                try:
                    self._conn.close()
                finally:
                    self._closed = True
                raise NotificationStoreUnhealthy(
                    f"sqlite integrity_check failed on {self.path}: {integrity!r}"
                )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id TEXT NOT NULL PRIMARY KEY,
                    created_at TEXT,
                    updated_at TEXT,
                    producer TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT,
                    dedup_key TEXT,
                    state TEXT NOT NULL,
                    actions TEXT,
                    resolution TEXT,
                    ttl_seconds INTEGER,
                    expires_at TEXT,
                    resolved_at TEXT,
                    answer_to_stream_id TEXT,
                    first_fired_at TEXT,
                    last_fired_at TEXT,
                    firing_count INTEGER,
                    firing_history TEXT,
                    last_resurfaced_at TEXT
                )
                """
            )
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(notifications)").fetchall()
            }
            if "answer_to_stream_id" not in columns:
                self._conn.execute(
                    "ALTER TABLE notifications ADD COLUMN answer_to_stream_id TEXT"
                )
            for column, ddl in {
                "first_fired_at": "ALTER TABLE notifications ADD COLUMN first_fired_at TEXT",
                "last_fired_at": "ALTER TABLE notifications ADD COLUMN last_fired_at TEXT",
                "firing_count": "ALTER TABLE notifications ADD COLUMN firing_count INTEGER",
                "firing_history": "ALTER TABLE notifications ADD COLUMN firing_history TEXT",
                "last_resurfaced_at": "ALTER TABLE notifications ADD COLUMN last_resurfaced_at TEXT",
            }.items():
                if column not in columns:
                    self._conn.execute(ddl)
            # Partial unique index: at most one OPEN notification per
            # (producer, dedup_key). Resolved rows are exempt so a new dedup
            # row can be created after the open one is resolved.
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_open_dedup "
                "ON notifications(producer, dedup_key) "
                "WHERE state='open' AND dedup_key IS NOT NULL"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_state "
                "ON notifications(state)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_questions (
                    question_id TEXT NOT NULL PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    answered_at TEXT,
                    producer_stream_id TEXT,
                    producer_provider TEXT,
                    spec_id TEXT,
                    dedup_key TEXT,
                    notification_id TEXT,
                    state TEXT NOT NULL,
                    envelope TEXT NOT NULL,
                    answer TEXT,
                    producer_session_generation TEXT
                )
                """
            )
            # Additive column for existing databases (D3): the verified producer
            # session generation, so a stale/replaced asker's open question is
            # distinguishable and answers cannot target a replacement generation.
            question_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(agent_questions)"
                ).fetchall()
            }
            if "producer_session_generation" not in question_columns:
                self._conn.execute(
                    "ALTER TABLE agent_questions "
                    "ADD COLUMN producer_session_generation TEXT"
                )
            # One-shot markers (D3 bounce-A clear): a marker row means the
            # associated one-time write already committed and must never repeat.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_markers (
                    marker TEXT NOT NULL PRIMARY KEY,
                    cutoff TEXT,
                    counts TEXT,
                    created_at TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_questions_notification_id "
                "ON agent_questions(notification_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_questions_producer_stream "
                "ON agent_questions(producer_stream_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_questions_spec_id "
                "ON agent_questions(spec_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_questions_state "
                "ON agent_questions(state)"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_questions_open_dedup "
                "ON agent_questions(dedup_key) "
                "WHERE state='open' AND dedup_key IS NOT NULL"
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_notification(
        self,
        *,
        producer: str,
        title: str,
        body: str | None = None,
        severity: str = "info",
        dedup_key: str | None = None,
        actions: Iterable[dict] | None = None,
        ttl_seconds: int | None = None,
        answer_to_stream_id: str | None = None,
        notification_id: str | None = None,
        now: str | None = None,
    ) -> dict:
        """Create a new ``open`` notification, or refresh an open dedup row.

        Validation:
        * ``producer`` and ``title`` must be non-empty.
        * ``severity`` must be one of ``info`` / ``warning`` / ``critical``.
        * each action (if given) must be a valid action dict.
        * ``ttl_seconds`` must be a non-negative int or ``None``; ``bool`` is
          rejected. ``0`` means explicit "no expiry"; ``None`` gets the
          severity default unless this is an ``agent_question.v1`` notification.

        Dedup: when ``dedup_key`` is given and an ``open`` row already exists
        with the same ``(producer, dedup_key)``, that row's
        ``title``/``body``/``severity``/``actions`` are refreshed and
        ``updated_at`` is bumped (original ``created_at`` and ``state='open'``
        are preserved); the refreshed row is returned without inserting a
        duplicate. The refresh always rewrites ``actions``, so omitting
        ``actions`` on a refresh resets the stored actions to ``[]``. Otherwise
        a new row is inserted.

        ``now`` (ISO string) is injectable for tests.
        """
        if not producer:
            raise InvalidNotification("producer must be non-empty")
        if not title:
            raise InvalidNotification("title must be non-empty")
        if severity not in SEVERITIES:
            raise InvalidNotification(f"invalid severity: {severity!r}")
        normalized_actions = _validate_actions(actions)
        ttl_seconds_omitted = ttl_seconds is None
        # Normalize ttl_seconds: reject bool (a stray ``True`` must not coerce
        # to ttl=1) and negatives; 0 means explicit "no expiry" and is stored
        # as NULL so the column reflects intent rather than a coerced sentinel.
        ttl_seconds = _normalize_ttl_seconds(ttl_seconds)
        if ttl_seconds_omitted and producer != "agent_question.v1":
            ttl_seconds = _default_ttl_seconds_for(severity)

        ts = now or _iso_now()
        actions_json = json.dumps(normalized_actions)
        history, history_json = _append_firing_history(None, ts)

        expires_at: str | None = None
        if ttl_seconds is not None:
            expires_at = _iso_z(_parse_iso(ts) + timedelta(seconds=ttl_seconds))

        with self._lock:
            self._require_open()
            if dedup_key is not None:
                existing = self._conn.execute(
                    "SELECT notification_id, first_fired_at, firing_count, firing_history, last_resurfaced_at FROM notifications "
                    "WHERE producer = ? AND dedup_key = ? AND state = ?",
                    (producer, dedup_key, STATE_OPEN),
                ).fetchone()
                if existing is not None:
                    existing_id = existing["notification_id"]
                    history, history_json = _append_firing_history(existing["firing_history"], ts)
                    first_fired_at = existing["first_fired_at"] or history[0]
                    firing_count = int(existing["firing_count"] or 0) + 1
                    should_resurface = _resurface_due(existing["last_resurfaced_at"], ts)
                    last_resurfaced_at = ts if should_resurface else existing["last_resurfaced_at"]
                    self._conn.execute(
                        """
                        UPDATE notifications
                        SET title = ?, body = ?, severity = ?, actions = ?,
                            updated_at = ?, answer_to_stream_id = ?,
                            first_fired_at = ?, last_fired_at = ?,
                            firing_count = ?, firing_history = ?,
                            last_resurfaced_at = ?
                        WHERE notification_id = ?
                        """,
                        (
                            title,
                            body,
                            severity,
                            actions_json,
                            ts,
                            answer_to_stream_id,
                            first_fired_at,
                            ts,
                            firing_count,
                            history_json,
                            last_resurfaced_at,
                            existing_id,
                        ),
                    )
                    self._conn.commit()
                    refreshed = self._get_locked(existing_id)
                    refreshed["should_resurface"] = should_resurface
                    return refreshed

            nid = notification_id or str(uuid.uuid4())
            self._conn.execute(
                """
                INSERT INTO notifications (
                    notification_id, created_at, updated_at, producer, severity,
                    title, body, dedup_key, state, actions, resolution,
                    ttl_seconds, expires_at, resolved_at, answer_to_stream_id,
                    first_fired_at, last_fired_at, firing_count, firing_history,
                    last_resurfaced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    nid,
                    ts,
                    ts,
                    producer,
                    severity,
                    title,
                    body,
                    dedup_key,
                    STATE_OPEN,
                    actions_json,
                    ttl_seconds,
                    expires_at,
                    answer_to_stream_id,
                    ts,
                    ts,
                    1,
                    history_json,
                    ts,
                ),
            )
            self._conn.commit()
            return self._get_locked(nid)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_notification(self, notification_id: str) -> dict | None:
        with self._lock:
            self._require_open()
            return self._get_locked(notification_id)

    def get_latest_by_dedup(self, *, producer: str, dedup_key: str) -> dict | None:
        """Return the newest matching row across all lifecycle states."""
        if not producer:
            raise InvalidNotification("producer must be non-empty")
        if not dedup_key:
            raise InvalidNotification("dedup_key must be non-empty")
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT notification_id FROM notifications "
                "WHERE producer = ? AND dedup_key = ? "
                "ORDER BY created_at DESC, notification_id DESC LIMIT 1",
                (producer, dedup_key),
            ).fetchone()
            return self._get_locked(str(row["notification_id"])) if row is not None else None

    def list_notifications(
        self,
        states: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Return notifications newest-first by ``created_at``.

        ``states`` filters by lifecycle state when given; ``limit`` caps the
        result count when given.

        Unknown state strings raise ``ValueError`` rather than silently
        returning ``[]`` (matches ``schedule_store.list_schedules``), so a
        caller typo surfaces instead of masquerading as "no matches".

        ``limit=None`` means no limit; ``limit=0`` returns an empty list
        (the natural SQLite ``LIMIT 0`` semantics). A negative ``limit``
        raises ``ValueError`` because SQLite treats a negative LIMIT as
        unlimited, which would silently return everything.
        """
        if limit is not None and int(limit) < 0:
            raise ValueError(f"limit must be >= 0 or None; got {limit}")
        cols = ", ".join(NOTIFICATION_COLUMNS)
        sql = f"SELECT {cols} FROM notifications"
        params: list[Any] = []
        if states is not None:
            states_list = list(states)
            for s in states_list:
                if s not in ALL_STATES:
                    raise ValueError(f"unknown notification state: {s!r}")
            if states_list:
                placeholders = ", ".join("?" * len(states_list))
                sql += f" WHERE state IN ({placeholders})"
                params.extend(states_list)
            else:
                # An empty states filter matches nothing.
                return []
        # Tie-break on notification_id (not rowid): SQLite reuses rowids after
        # deletes, so after a prune_resolved a fresh row can take a lower rowid
        # than an older survivor with an identical created_at, inverting the
        # intended newest-first order. notification_id is stable.
        sql += " ORDER BY created_at DESC, notification_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            self._require_open()
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_dict(row) for row in rows]

    def count_open(self) -> int:
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE state = ?",
                (STATE_OPEN,),
            ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # Agent questions
    # ------------------------------------------------------------------

    def create_agent_question(
        self,
        *,
        envelope: dict[str, Any],
        actions: Iterable[dict] | None = None,
        severity: str = "info",
        now: str | None = None,
    ) -> dict:
        """Create or refresh a durable ``agent_question.v1`` prompt.

        ``question_id`` is the stable public key. An already-answered question
        is returned as-is; an open question refreshes its linked notification.
        """
        actions = list(actions or [])
        question_id = _require_text(envelope.get("question_id"), "question_id")
        title = _require_text(envelope.get("title"), "title")
        body = _require_text(envelope.get("body"), "body")
        raw_dedup_key = envelope.get("dedup_key")
        dedup_key = _optional_text(raw_dedup_key)
        producer_stream_id = _optional_text(envelope.get("producer_stream_id"))
        producer_provider = _optional_text(envelope.get("producer_provider"))
        producer_session_generation = _optional_text(
            envelope.get("producer_session_generation")
        )
        spec_id = _optional_text(envelope.get("spec_id"))
        schema_version = int(envelope.get("schema_version") or 1)
        ttl_seconds = envelope.get("ttl_seconds")
        raw_context = envelope.get("context")
        try:
            handoff_context = json.loads(raw_context) if isinstance(raw_context, str) else raw_context
        except ValueError:
            handoff_context = None
        is_handoff_approval = (
            not dedup_key
            and isinstance(handoff_context, dict)
            and handoff_context.get("schema") == "HandoffModelChangeApprovalV1"
        )
        if is_handoff_approval:
            dedup_key = f"handoff-approval:{question_id}"
        if not dedup_key:
            raise InvalidNotification("dedup_key is required")
        answer_to_stream_id = producer_stream_id
        ts = now or _iso_now()

        existing = self.get_agent_question(question_id)
        def approval_projection(candidate: dict[str, Any], candidate_actions: object) -> str | None:
            raw_context = candidate.get("context")
            try:
                context = json.loads(raw_context) if isinstance(raw_context, str) else raw_context
            except ValueError:
                context = None
            if not isinstance(context, dict) or context.get("schema") != "HandoffModelChangeApprovalV1":
                return None
            projected = {
                key: candidate.get(key)
                for key in (
                    "question_id", "title", "body", "dedup_key", "producer_stream_id",
                    "producer_provider", "response_mode", "options", "allow_custom",
                    "ttl_seconds", "default_action", "context", "creator",
                )
            }
            projected["actions"] = list(candidate_actions or [])
            return json.dumps(projected, separators=(",", ":"), sort_keys=True)

        if existing is not None:
            old_notification = self.get_notification(str(existing.get("notification_id") or ""))
            old_actions = old_notification.get("actions") if isinstance(old_notification, dict) else []
            previous = approval_projection(
                existing.get("envelope") if isinstance(existing.get("envelope"), dict) else {},
                old_actions,
            )
            proposed = approval_projection(envelope, actions)
            if previous is not None and proposed != previous:
                raise InvalidNotification("prompt_question_immutable_conflict")
        if existing is not None and existing.get("state") != QUESTION_STATE_OPEN:
            return existing
        if existing is not None:
            dedup_key = str(existing.get("dedup_key") or dedup_key)
            envelope = dict(envelope)
            envelope["dedup_key"] = None if is_handoff_approval else dedup_key
            envelope["created_at"] = existing.get("created_at") or envelope.get("created_at")
            envelope["answered_at"] = None
            envelope["answer"] = None
        with self._lock:
            self._require_open()
            conflicting = self._conn.execute(
                "SELECT question_id FROM agent_questions "
                "WHERE dedup_key = ? AND state = ? AND question_id != ?",
                (dedup_key, QUESTION_STATE_OPEN, question_id),
            ).fetchone()
        if conflicting is not None:
            raise InvalidNotification(
                "dedup_key already belongs to open question_id: "
                f"{conflicting['question_id']}"
            )

        record = self.create_notification(
            producer="agent_question.v1",
            title=title,
            body=body,
            severity=severity,
            dedup_key=dedup_key,
            actions=actions,
            ttl_seconds=ttl_seconds if isinstance(ttl_seconds, int) else None,
            answer_to_stream_id=answer_to_stream_id,
            now=ts,
        )
        notification_id = str(record["notification_id"])
        created_at = str(envelope.get("created_at") or ts)
        envelope = dict(envelope)
        envelope["created_at"] = created_at
        envelope["updated_at"] = ts
        envelope["dedup_key"] = None if is_handoff_approval else dedup_key
        envelope["answer"] = None
        envelope["answered_at"] = None
        envelope_json = json.dumps(envelope, separators=(",", ":"), sort_keys=True)

        with self._lock:
            self._require_open()
            self._conn.execute(
                """
                INSERT INTO agent_questions (
                    question_id, schema_version, created_at, updated_at,
                    answered_at, producer_stream_id, producer_provider,
                    producer_session_generation, spec_id,
                    dedup_key, notification_id, state, envelope, answer
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(question_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    updated_at = excluded.updated_at,
                    producer_stream_id = excluded.producer_stream_id,
                    producer_provider = excluded.producer_provider,
                    producer_session_generation = excluded.producer_session_generation,
                    spec_id = excluded.spec_id,
                    dedup_key = excluded.dedup_key,
                    notification_id = excluded.notification_id,
                    state = excluded.state,
                    envelope = excluded.envelope,
                    answer = NULL,
                    answered_at = NULL
                """,
                (
                    question_id,
                    schema_version,
                    created_at,
                    ts,
                    producer_stream_id,
                    producer_provider,
                    producer_session_generation,
                    spec_id,
                    dedup_key,
                    notification_id,
                    QUESTION_STATE_OPEN,
                    envelope_json,
                ),
            )
            self._conn.commit()
            question = self._get_agent_question_locked(question_id)
            if question is not None and is_handoff_approval:
                question["dedup_key"] = None
            return question

    def get_agent_question(self, question_id: str) -> dict | None:
        with self._lock:
            self._require_open()
            return self._get_agent_question_locked(question_id)

    def get_agent_question_for_notification(self, notification_id: str) -> dict | None:
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                f"SELECT {', '.join(AGENT_QUESTION_COLUMNS)} "
                "FROM agent_questions WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            question = _question_row_to_dict(row) if row is not None else None
            if question is not None:
                envelope = question.get("envelope")
                context = envelope.get("context") if isinstance(envelope, dict) else None
                if isinstance(context, dict) and context.get("schema") == "HandoffModelChangeApprovalV1":
                    question["dedup_key"] = None
            return question

    def stamp_agent_question_answer_operator_auth(
        self, question_id: str, operator_auth: dict[str, Any]
    ) -> dict | None:
        """Server-stamp the answer provenance after a v2 websocket response."""
        with self._lock:
            self._require_open()
            question = self._get_agent_question_locked(question_id)
            if question is None or question.get("state") not in {
                QUESTION_STATE_ANSWERED,
                QUESTION_STATE_CONSUMED,
            }:
                return question
            answer = question.get("answer") if isinstance(question.get("answer"), dict) else {}
            answer = {**answer, "operator_auth": dict(operator_auth)}
            envelope = question.get("envelope") if isinstance(question.get("envelope"), dict) else {}
            envelope = {**envelope, "answer": answer}
            self._conn.execute(
                """
                UPDATE agent_questions
                   SET envelope = ?, answer = ?
                 WHERE question_id = ? AND state = ?
                """,
                (
                    json.dumps(envelope, separators=(",", ":"), sort_keys=True),
                    json.dumps(answer, separators=(",", ":"), sort_keys=True),
                    question_id,
                    question["state"],
                ),
            )
            self._conn.commit()
            return self._get_agent_question_locked(question_id)

    def list_agent_questions(
        self,
        *,
        producer_stream_id: str | None = None,
        producer_stream_ids: Iterable[str] | None = None,
        spec_id: str | None = None,
        open_only: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        if limit is not None and int(limit) < 0:
            raise ValueError(f"limit must be >= 0 or None; got {limit}")
        sql = f"SELECT {', '.join(AGENT_QUESTION_COLUMNS)} FROM agent_questions"
        clauses: list[str] = []
        params: list[Any] = []
        stream_ids: list[str] = []
        if producer_stream_ids is not None:
            seen_stream_ids: set[str] = set()
            for value in producer_stream_ids:
                text = str(value or "").strip()
                if not text or text in seen_stream_ids:
                    continue
                seen_stream_ids.add(text)
                stream_ids.append(text)
            if not stream_ids:
                clauses.append("1 = 0")
            else:
                placeholders = ",".join("?" for _ in stream_ids)
                clauses.append(f"producer_stream_id IN ({placeholders})")
                params.extend(stream_ids)
        elif producer_stream_id:
            clauses.append("producer_stream_id = ?")
            params.append(producer_stream_id)
        if spec_id:
            clauses.append("spec_id = ?")
            params.append(spec_id)
        if open_only:
            clauses.append("state = ?")
            params.append(QUESTION_STATE_OPEN)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, question_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            self._require_open()
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_question_row_to_dict(row) for row in rows]

    def answer_agent_question_for_notification(
        self, notification_id: str, answer: dict[str, Any], *, now: str | None = None
    ) -> dict | None:
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                f"SELECT {', '.join(AGENT_QUESTION_COLUMNS)} "
                "FROM agent_questions WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                return None
            question = _question_row_to_dict(row)
            if question.get("state") != QUESTION_STATE_OPEN:
                return question
            answer = _normalize_agent_question_answer(question, answer)
            envelope = dict(question.get("envelope") or {})
            envelope["updated_at"] = ts
            envelope["answered_at"] = ts
            envelope["answer"] = answer
            self._conn.execute(
                """
                UPDATE agent_questions
                SET state = ?, updated_at = ?, answered_at = ?, envelope = ?, answer = ?
                WHERE question_id = ?
                """,
                (
                    QUESTION_STATE_ANSWERED,
                    ts,
                    ts,
                    json.dumps(envelope, separators=(",", ":"), sort_keys=True),
                    json.dumps(answer, separators=(",", ":"), sort_keys=True),
                    question["question_id"],
                ),
            )
            self._conn.commit()
            return self._get_agent_question_locked(str(question["question_id"]))

    def consume_answered_agent_question_for_notification(
        self, notification_id: str, *, now: str | None = None
    ) -> dict | None:
        """Mark an answered question consumed after its deterministic tell lands.

        The caller owns the proof that its delivery transaction succeeded.  The
        shared state machine deliberately has no daemon-kind or origin filter:
        ownership belongs at the acting transaction boundary, not in a
        fail-open shared-row classifier.
        """
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                f"SELECT {', '.join(AGENT_QUESTION_COLUMNS)} "
                "FROM agent_questions WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                return None
            question = _question_row_to_dict(row)
            if question.get("state") != QUESTION_STATE_ANSWERED:
                return question
            envelope = dict(question.get("envelope") or {})
            envelope["updated_at"] = ts
            envelope["consumed_at"] = ts
            self._conn.execute(
                """
                UPDATE agent_questions
                SET state = ?, updated_at = ?, envelope = ?
                WHERE question_id = ? AND state = ?
                """,
                (
                    QUESTION_STATE_CONSUMED,
                    ts,
                    json.dumps(envelope, separators=(",", ":"), sort_keys=True),
                    question["question_id"],
                    QUESTION_STATE_ANSWERED,
                ),
            )
            self._conn.commit()
            return self._get_agent_question_locked(str(question["question_id"]))

    def list_answered_agent_questions(self, *, limit: int) -> list[dict]:
        """Return a bounded recovery page of delivery-pending answers."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a nonnegative integer")
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                f"SELECT {', '.join(AGENT_QUESTION_COLUMNS)} "
                "FROM agent_questions WHERE state = ? "
                "ORDER BY answered_at ASC, question_id ASC LIMIT ?",
                (QUESTION_STATE_ANSWERED, limit),
            ).fetchall()
        return [_question_row_to_dict(row) for row in rows]

    def _terminalize_agent_question_for_notification_locked(
        self,
        notification_id: str,
        *,
        state: str,
        resolution: dict[str, Any],
        now: str,
    ) -> dict | None:
        row = self._conn.execute(
            f"SELECT {', '.join(AGENT_QUESTION_COLUMNS)} "
            "FROM agent_questions WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if row is None:
            return None
        question = _question_row_to_dict(row)
        if question.get("state") != QUESTION_STATE_OPEN:
            return question
        payload = _notification_resolution_payload(
            notification_id,
            resolution,
            terminal_state=state,
        )
        if not payload.get("at"):
            payload["at"] = now
        envelope = dict(question.get("envelope") or {})
        envelope["updated_at"] = now
        if state == QUESTION_STATE_DISMISSED:
            envelope["answered_at"] = now
            envelope["dismissed_at"] = now
            envelope["dismissal"] = payload
        elif state == QUESTION_STATE_EXPIRED:
            envelope["expired_at"] = now
            envelope["expiry"] = payload
        self._conn.execute(
            """
            UPDATE agent_questions
            SET state = ?, updated_at = ?, answered_at = ?, envelope = ?, answer = ?
            WHERE question_id = ? AND state = ?
            """,
            (
                state,
                now,
                now if state == QUESTION_STATE_DISMISSED else None,
                json.dumps(envelope, separators=(",", ":"), sort_keys=True),
                json.dumps(payload, separators=(",", ":"), sort_keys=True)
                if state == QUESTION_STATE_DISMISSED
                else None,
                question["question_id"],
                QUESTION_STATE_OPEN,
            ),
        )
        return self._get_agent_question_locked(str(question["question_id"]))

    def reconcile_agent_questions_with_terminal_notifications(
        self, *, now: str | None = None
    ) -> list[str]:
        """Close open question rows whose linked notification is already terminal."""
        ts = now or _iso_now()
        reconciled: list[str] = []
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT question_id, notification_id FROM agent_questions "
                "WHERE state = ? AND notification_id IS NOT NULL",
                (QUESTION_STATE_OPEN,),
            ).fetchall()
            for row in rows:
                notification_id = str(row["notification_id"] or "")
                if not notification_id:
                    continue
                notification = self._get_locked(notification_id)
                if notification is None or notification.get("state") not in TERMINAL_STATES:
                    continue
                resolution = notification.get("resolution")
                if not isinstance(resolution, dict):
                    resolution = {
                        "by": "system",
                        "at": notification.get("resolved_at") or ts,
                        "action_kind": notification.get("state") or "terminal",
                    }
                action_kind = str(resolution.get("action_kind") or "")
                if action_kind == "resolved" and "text" not in resolution:
                    state = QUESTION_STATE_DISMISSED
                elif action_kind == "expired" or notification.get("state") == STATE_EXPIRED:
                    state = QUESTION_STATE_EXPIRED
                else:
                    continue
                updated = self._terminalize_agent_question_for_notification_locked(
                    notification_id,
                    state=state,
                    resolution=resolution,
                    now=str(notification.get("resolved_at") or ts),
                )
                if updated is not None and updated.get("question_id"):
                    reconciled.append(str(updated["question_id"]))
            if reconciled:
                self._conn.commit()
        return reconciled

    # ------------------------------------------------------------------
    # Shared terminalization (D3): TTL, asker close/replacement and the
    # one-shot bounce-A clear all expire the notification AND its paired
    # agent_question through this single helper. Caller holds ``self._lock``
    # and owns the commit.
    # ------------------------------------------------------------------

    def _expire_notification_and_question_locked(
        self,
        notification_id: str,
        *,
        now: str,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Move an OPEN notification and its paired open question to expired.

        Preserves already-terminal rows (answers/history are never overwritten):
        a non-open notification is skipped, and the paired-question terminalizer
        only touches an OPEN question. Returns True when the notification row
        itself transitioned open->expired.
        """
        system_resolution: dict[str, Any] = {
            "by": "system",
            "at": now,
            "action_kind": "expired",
            "actor_class": "system",
            "actor_stream_id": None,
            "actor_client": None,
            "actor_verified": False,
        }
        if extra:
            system_resolution.update(extra)
        row = self._conn.execute(
            "SELECT state FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        transitioned = False
        if row is not None and str(row["state"]) == STATE_OPEN:
            self._conn.execute(
                """
                UPDATE notifications
                SET state = ?, resolution = ?, resolved_at = ?, updated_at = ?
                WHERE notification_id = ? AND state = ?
                """,
                (
                    STATE_EXPIRED,
                    json.dumps(system_resolution),
                    now,
                    now,
                    notification_id,
                    STATE_OPEN,
                ),
            )
            transitioned = True
        self._terminalize_agent_question_for_notification_locked(
            notification_id,
            state=QUESTION_STATE_EXPIRED,
            resolution=system_resolution,
            now=now,
        )
        return transitioned

    def expire_open_questions_for_producer(
        self,
        producer_stream_id: str,
        *,
        only_generation: str | None = None,
        superseding_generation: str | None = None,
        now: str | None = None,
        reason: str = "asker_closed",
    ) -> list[str]:
        """Expire the open questions asked by a producer whose asker is gone.

        Selection (at most one of the two generation modes):
        - ``only_generation`` — expire only questions stamped with THAT generation
          (or with no generation). Used on a confirmed-dead close so a handoff
          successor already open at a newer generation keeps its own questions.
        - ``superseding_generation`` — expire only questions stamped with a
          DIFFERENT generation (the asker was replaced); the successor's own
          questions survive.
        - neither — expire every open question for the producer.

        Answered/terminal rows are preserved. Returns the notification ids that
        transitioned so the caller can broadcast the terminal cards.
        """
        producer_stream_id = _optional_text(producer_stream_id) or ""
        if not producer_stream_id:
            return []
        only_generation = _optional_text(only_generation) if only_generation is not None else None
        superseding_generation = (
            _optional_text(superseding_generation)
            if superseding_generation is not None else None
        )
        ts = now or _iso_now()
        expired_notification_ids: list[str] = []
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT notification_id, producer_session_generation "
                "FROM agent_questions "
                "WHERE producer_stream_id = ? AND state = ? "
                "AND notification_id IS NOT NULL",
                (producer_stream_id, QUESTION_STATE_OPEN),
            ).fetchall()
            for row in rows:
                nid = str(row["notification_id"] or "")
                if not nid:
                    continue
                stored = _optional_text(row["producer_session_generation"])
                if only_generation is not None:
                    # Expire the closed generation's questions and any generationless
                    # rows; never a distinct successor generation's questions.
                    if stored is not None and stored != only_generation:
                        continue
                elif superseding_generation is not None:
                    if stored is not None and stored == superseding_generation:
                        continue
                if self._expire_notification_and_question_locked(
                    nid, now=ts, extra={"reason": reason}
                ):
                    expired_notification_ids.append(nid)
            if expired_notification_ids:
                self._conn.commit()
        return expired_notification_ids

    def clear_open_agent_questions_once(
        self,
        marker: str = QUESTION_CLEAR_MARKER_A,
        *,
        cutoff: str | None = None,
        now: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """One-shot: expire every stored OPEN agent_question (and its paired
        notification) created at/before ``cutoff``, then stamp ``marker`` in the
        SAME transaction. Idempotent: once the marker exists this is a no-op and
        post-clear questions are never swept. Includes NULL producers and legacy
        ack; preserves terminal answers/history. ``dry_run`` counts without
        writing and never stamps the marker.
        """
        ts = now or _iso_now()
        cutoff_ts = cutoff or ts
        with self._lock:
            self._require_open()
            existing = self._conn.execute(
                "SELECT marker, cutoff, counts, created_at "
                "FROM notification_markers WHERE marker = ?",
                (marker,),
            ).fetchone()
            open_before = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM agent_questions WHERE state = ?",
                    (QUESTION_STATE_OPEN,),
                ).fetchone()[0]
            )
            if existing is not None:
                try:
                    banked = json.loads(existing["counts"]) if existing["counts"] else {}
                except ValueError:
                    banked = {}
                return {
                    "marker": marker,
                    "already": True,
                    "dry_run": bool(dry_run),
                    "cutoff": existing["cutoff"],
                    "cleared": int(banked.get("cleared", 0)),
                    "open_before": open_before,
                    "open_after": open_before,
                }
            due = self._conn.execute(
                "SELECT notification_id FROM agent_questions "
                "WHERE state = ? AND notification_id IS NOT NULL "
                "AND (created_at IS NULL OR created_at <= ?)",
                (QUESTION_STATE_OPEN, cutoff_ts),
            ).fetchall()
            due_ids = [str(r["notification_id"]) for r in due if r["notification_id"]]
            if dry_run:
                return {
                    "marker": marker,
                    "already": False,
                    "dry_run": True,
                    "cutoff": cutoff_ts,
                    "cleared": len(due_ids),
                    "open_before": open_before,
                    "open_after": open_before,
                }
            cleared_ids: list[str] = []
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for nid in due_ids:
                    self._expire_notification_and_question_locked(
                        nid, now=ts, extra={"reason": marker}
                    )
                    cleared_ids.append(nid)
                counts = {"cleared": len(cleared_ids), "open_before": open_before}
                self._conn.execute(
                    "INSERT INTO notification_markers (marker, cutoff, counts, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (marker, cutoff_ts, json.dumps(counts, sort_keys=True), ts),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            open_after = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM agent_questions WHERE state = ?",
                    (QUESTION_STATE_OPEN,),
                ).fetchone()[0]
            )
            return {
                "marker": marker,
                "already": False,
                "dry_run": False,
                "cutoff": cutoff_ts,
                "cleared": len(cleared_ids),
                "cleared_notification_ids": cleared_ids,
                "open_before": open_before,
                "open_after": open_after,
            }

    # ------------------------------------------------------------------
    # Resolve / expire
    # ------------------------------------------------------------------

    def resolve_notification(
        self,
        notification_id: str,
        *,
        action_kind: str,
        by: str,
        choice: bool | None = None,
        selections: list[str] | None = None,
        note: str | None = None,
        spawned_stream_id: str | None = None,
        action_id: str | None = None,
        label: str | None = None,
        value: Any | None = None,
        text: str | None = None,
        custom_text: str | None = None,
        effective_spawn_spec: dict[str, Any] | None = None,
        actor_provenance: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> dict:
        """Resolve an ``open`` notification via an operator/system action.

        Raises ``NotificationNotFound`` if the row is missing and
        ``NotificationTerminalState`` if it is already resolved. ``action_kind``
        must be in :data:`RESOLVE_ACTION_KINDS`; for ``yes_no`` the ``choice``
        must be a bool.
        """
        if action_kind not in RESOLVE_ACTION_KINDS:
            raise InvalidNotification(f"invalid action_kind: {action_kind!r}")
        if action_kind == "yes_no" and not isinstance(choice, bool):
            raise InvalidNotification("yes_no resolution requires a bool 'choice'")
        if note is not None and not isinstance(note, str):
            raise InvalidNotification("note must be a string")
        if text is not None and not isinstance(text, str):
            raise InvalidNotification("text must be a string")
        if custom_text is not None and not isinstance(custom_text, str):
            raise InvalidNotification("custom_text must be a string")
        if text is not None and custom_text is not None:
            raise InvalidNotification("text cannot be combined with custom_text")
        if effective_spawn_spec is not None and not isinstance(effective_spawn_spec, dict):
            raise InvalidNotification("effective_spawn_spec must be an object")

        ts = now or _iso_now()
        new_state = _ACTION_TO_STATE[action_kind]
        actor_provenance = _normalize_actor_provenance(actor_provenance)

        resolution: dict[str, Any] = {
            "by": by,
            "at": ts,
            "action_kind": action_kind,
        }
        resolution.update(actor_provenance)
        if choice is not None:
            resolution["choice"] = choice
        if selections is not None:
            resolution["selections"] = selections
        if note is not None:
            resolution["note"] = note
        if spawned_stream_id is not None:
            resolution["spawned_stream_id"] = spawned_stream_id
        if action_id is not None:
            resolution["action_id"] = action_id
        if label is not None:
            resolution["label"] = label
        if value is not None:
            resolution["value"] = value
        if text is not None:
            resolution["text"] = text
        if custom_text is not None:
            resolution["custom_text"] = custom_text
        if effective_spawn_spec is not None:
            resolution["effective_spawn_spec"] = effective_spawn_spec

        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT state, resolution, answer_to_stream_id FROM notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise NotificationNotFound(notification_id)
            if row["state"] != STATE_OPEN:
                existing_resolution = json.loads(row["resolution"] or "{}")
                existing_intent = _intent_from_resolution(notification_id, existing_resolution)
                replay_question_row = self._conn.execute(
                    f"SELECT {', '.join(AGENT_QUESTION_COLUMNS)} "
                    "FROM agent_questions WHERE notification_id = ?",
                    (notification_id,),
                ).fetchone()
                replay_question = (
                    _question_row_to_dict(replay_question_row) if replay_question_row is not None else None
                )
                requested_intent = _canonical_resolution_intent(
                    notification_id=notification_id,
                    action_kind=action_kind,
                    action_id=action_id,
                    choice=choice,
                    value=value,
                    selections=_canonical_selections(replay_question, selections),
                    text=text,
                    custom_text=custom_text,
                    note=note,
                    effective_spawn_spec=effective_spawn_spec,
                )
                if existing_intent == requested_intent and row["state"] in TERMINAL_STATES:
                    replayed = self._get_locked(notification_id)
                    replayed["_resolution_replayed"] = True
                    return replayed
                if row["state"] == STATE_RUNNING and existing_intent == requested_intent:
                    raise NotificationResolutionInProgress(notification_id)
                raise NotificationResolutionConflict(notification_id)
            question: dict[str, Any] | None = None
            normalized_answer: dict[str, Any] | None = None
            if selections is not None or text is not None or custom_text is not None or (
                note is not None and action_kind != "resolved"
            ):
                question_row = self._conn.execute(
                    f"SELECT {', '.join(AGENT_QUESTION_COLUMNS)} "
                    "FROM agent_questions WHERE notification_id = ?",
                    (notification_id,),
                ).fetchone()
                if question_row is not None:
                    question = _question_row_to_dict(question_row)
                    response_mode = _agent_question_response_mode(question)
                    if text is not None:
                        if response_mode != "free_text":
                            raise InvalidNotification("text is only valid for free_text questions")
                        normalized_answer = _normalize_agent_question_answer(
                            question,
                            {
                                "notification_id": notification_id,
                                "action_id": action_id or "",
                                "action_kind": action_kind,
                                "label": label or action_kind,
                                "by": by,
                                "at": ts,
                                **actor_provenance,
                                "selections": [],
                                "text": text,
                                "note": note,
                            },
                        )
                    else:
                        normalized_answer = _normalize_agent_question_answer(
                            question,
                            {
                                "notification_id": notification_id,
                                "action_id": action_id or "",
                                "action_kind": action_kind,
                                "label": label or action_kind,
                                "by": by,
                                "at": ts,
                                **actor_provenance,
                                "selections": selections,
                                "note": note,
                                "custom_text": custom_text,
                                "value": value,
                                "choice": choice,
                            },
                        )
            canonical_selections = _canonical_selections(question, selections)
            if selections is not None:
                resolution["selections"] = canonical_selections
            canonical_intent = _canonical_resolution_intent(
                notification_id=notification_id,
                action_kind=action_kind,
                action_id=action_id,
                choice=choice,
                value=value,
                selections=canonical_selections,
                text=text,
                custom_text=custom_text,
                note=note,
                effective_spawn_spec=effective_spawn_spec,
            )
            resolution["canonical_intent"] = canonical_intent
            if normalized_answer is not None and row["answer_to_stream_id"]:
                resolution["delivery_status"] = "pending"
            resolution_json = json.dumps(resolution)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    UPDATE notifications
                    SET state = ?, resolution = ?, resolved_at = ?, updated_at = ?
                    WHERE notification_id = ?
                    """,
                    (new_state, resolution_json, ts, ts, notification_id),
                )
                if normalized_answer is not None and question is not None:
                    envelope = dict(question.get("envelope") or {})
                    envelope.update({"updated_at": ts, "answered_at": ts, "answer": normalized_answer})
                    self._conn.execute(
                        """
                        UPDATE agent_questions
                        SET state = ?, updated_at = ?, answered_at = ?, envelope = ?, answer = ?
                        WHERE question_id = ? AND state = ?
                        """,
                        (
                            QUESTION_STATE_ANSWERED,
                            ts,
                            ts,
                            json.dumps(envelope, separators=(",", ":"), sort_keys=True),
                            json.dumps(normalized_answer, separators=(",", ":"), sort_keys=True),
                            question["question_id"],
                            QUESTION_STATE_OPEN,
                        ),
                    )
                elif action_kind == "resolved" and text is None and custom_text is None:
                    self._terminalize_agent_question_for_notification_locked(
                        notification_id,
                        state=QUESTION_STATE_DISMISSED,
                        resolution=resolution,
                        now=ts,
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self._get_locked(notification_id)

    def resolve_open_dedup(
        self,
        *,
        producer: str,
        dedup_key: str,
        by: str,
        actor_provenance: dict[str, Any] | None = None,
        expected_notification_id: str | None = None,
        now: str | None = None,
    ) -> dict | None:
        """Resolve the matching open notification if present.

        ``expected_notification_id`` binds callers that authorize a prior
        read to that immutable row. If dedup rollover replaced it before this
        call, the replacement is left open rather than inheriting the prior
        row's authorization.
        """
        if not producer:
            raise InvalidNotification("producer must be non-empty")
        if not dedup_key:
            raise InvalidNotification("dedup_key must be non-empty")
        with self._lock:
            self._require_open()
            if expected_notification_id is None:
                row = self._conn.execute(
                    "SELECT notification_id FROM notifications "
                    "WHERE producer = ? AND dedup_key = ? AND state = ?",
                    (producer, dedup_key, STATE_OPEN),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT notification_id FROM notifications "
                    "WHERE producer = ? AND dedup_key = ? AND state = ? "
                    "AND notification_id = ?",
                    (producer, dedup_key, STATE_OPEN, expected_notification_id),
                ).fetchone()
        if row is None:
            return None
        return self.resolve_notification(
            str(row["notification_id"]),
            action_kind="resolved",
            by=by,
            actor_provenance=actor_provenance,
            now=now,
        )

    def claim_run_command(
        self,
        notification_id: str,
        *,
        by: str,
        now: str | None = None,
    ) -> dict:
        """Atomically claim an ``open`` run-command notification.

        The claim is the side-effect guard for daemon execution: only the
        caller that transitions ``open`` -> ``running`` should run the command.
        Missing rows raise ``NotificationNotFound``; any non-open state raises
        ``NotificationTerminalState`` carrying the current state.
        """
        ts = now or _iso_now()
        resolution_json = json.dumps(
            {
                "claimed_by": by,
                "claimed_at": ts,
                "action_kind": "run_command",
            }
        )

        with self._lock:
            self._require_open()
            cursor = self._conn.execute(
                """
                UPDATE notifications
                SET state = ?, resolution = ?, updated_at = ?
                WHERE notification_id = ? AND state = ?
                """,
                (STATE_RUNNING, resolution_json, ts, notification_id, STATE_OPEN),
            )
            if cursor.rowcount != 1:
                row = self._conn.execute(
                    "SELECT state FROM notifications WHERE notification_id = ?",
                    (notification_id,),
                ).fetchone()
                if row is None:
                    raise NotificationNotFound(notification_id)
                raise NotificationTerminalState(row["state"])
            self._conn.commit()
            return self._get_locked(notification_id)

    def claim_external_resolution(
        self,
        notification_id: str,
        *,
        canonical_intent: dict[str, Any],
        by: str,
        action_kind: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        if action_kind not in {"spawn_worker", "run_command"}:
            raise InvalidNotification("external resolution action must be spawn_worker or run_command")
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            record = self._get_locked(notification_id)
            if record is None:
                raise NotificationNotFound(notification_id)
            resolution = record.get("resolution") if isinstance(record.get("resolution"), dict) else {}
            if record["state"] != STATE_OPEN:
                existing_intent = _intent_from_resolution(notification_id, resolution)
                if existing_intent != canonical_intent:
                    raise NotificationResolutionConflict(notification_id)
                if record["state"] == STATE_RUNNING:
                    return {"state": "in_progress", "record": record}
                if record["state"] == STATE_INDETERMINATE:
                    return {"state": "indeterminate", "record": record}
                return {"state": "completed", "record": record}
            claim = {
                "claimed_by": by,
                "claimed_at": ts,
                "action_kind": action_kind,
                "resolution_status": "claimed",
                "canonical_intent": canonical_intent,
            }
            cursor = self._conn.execute(
                """
                UPDATE notifications
                SET state = ?, resolution = ?, updated_at = ?
                WHERE notification_id = ? AND state = ?
                """,
                (STATE_RUNNING, json.dumps(claim), ts, notification_id, STATE_OPEN),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                raise NotificationResolutionInProgress(notification_id)
            self._conn.commit()
            return {"state": "claimed", "record": self._get_locked(notification_id)}

    def finish_external_resolution(
        self,
        notification_id: str,
        *,
        canonical_intent: dict[str, Any],
        terminal_state: str,
        outcome: dict[str, Any],
        by: str,
        now: str | None = None,
    ) -> dict:
        if terminal_state not in {STATE_SPAWNED, STATE_DONE, STATE_FAILED}:
            raise InvalidNotification("invalid external resolution terminal state")
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            record = self._get_locked(notification_id)
            if record is None:
                raise NotificationNotFound(notification_id)
            existing = record.get("resolution") if isinstance(record.get("resolution"), dict) else {}
            if _intent_from_resolution(notification_id, existing) != canonical_intent:
                raise NotificationResolutionConflict(notification_id)
            if record["state"] != STATE_RUNNING:
                if record["state"] == terminal_state:
                    return record
                raise NotificationTerminalState(record["state"])
            resolution = dict(existing)
            resolution.update(outcome)
            resolution.update(
                {
                    "by": by,
                    "at": ts,
                    "resolution_status": "completed",
                    "canonical_intent": canonical_intent,
                }
            )
            cursor = self._conn.execute(
                """
                UPDATE notifications
                SET state = ?, resolution = ?, resolved_at = ?, updated_at = ?
                WHERE notification_id = ? AND state = ?
                """,
                (
                    terminal_state,
                    json.dumps(resolution),
                    ts,
                    ts,
                    notification_id,
                    STATE_RUNNING,
                ),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                raise NotificationResolutionInProgress(notification_id)
            self._conn.commit()
            return self._get_locked(notification_id)

    def recover_claimed_external_resolutions(
        self,
        *,
        now: str | None = None,
        stale_after_s: float = 0.0,
    ) -> int:
        """Finish abandoned external claims without stealing active work.

        ``stale_after_s=0`` preserves the v1/direct-store recovery contract.
        Daemon startup and the periodic recovery pass provide a positive lease
        age, so a claim updated recently by another worker remains running.
        """
        if stale_after_s < 0:
            raise ValueError("stale_after_s must be non-negative")
        ts = now or _iso_now()
        cutoff = _parse_iso(ts) - timedelta(seconds=float(stale_after_s))
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT notification_id, resolution, updated_at FROM notifications WHERE state = ?",
                (STATE_RUNNING,),
            ).fetchall()
            stale_rows = []
            for row in rows:
                if stale_after_s > 0:
                    try:
                        updated_at = _parse_iso(str(row["updated_at"] or ""))
                    except (TypeError, ValueError):
                        # A malformed lease timestamp cannot prove that work is
                        # active; recover it with an explicit audit reason.
                        updated_at = None
                    if updated_at is not None and updated_at > cutoff:
                        continue
                stale_rows.append(row)
            for row in stale_rows:
                resolution = json.loads(row["resolution"] or "{}")
                resolution.update(
                    {
                        "resolution_status": "indeterminate",
                        "indeterminate_at": ts,
                        "indeterminate_reason": "stale_external_resolution_claim",
                        "recovery_reason": "daemon_restart_or_lease_expired",
                    }
                )
                self._conn.execute(
                    """
                    UPDATE notifications
                    SET state = ?, resolution = ?, resolved_at = ?, updated_at = ?
                    WHERE notification_id = ? AND state = ?
                    """,
                    (
                        STATE_INDETERMINATE,
                        json.dumps(resolution),
                        ts,
                        ts,
                        row["notification_id"],
                        STATE_RUNNING,
                    ),
                )
            self._conn.commit()
            return len(stale_rows)

    def mark_external_resolution_indeterminate(
        self,
        notification_id: str,
        *,
        canonical_intent: dict[str, Any],
        reason: str,
        now: str | None = None,
    ) -> dict:
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            record = self._get_locked(notification_id)
            if record is None:
                raise NotificationNotFound(notification_id)
            resolution = record.get("resolution") if isinstance(record.get("resolution"), dict) else {}
            if _intent_from_resolution(notification_id, resolution) != canonical_intent:
                raise NotificationResolutionConflict(notification_id)
            if record["state"] == STATE_INDETERMINATE:
                return record
            if record["state"] != STATE_RUNNING:
                raise NotificationTerminalState(record["state"])
            resolution = dict(resolution)
            resolution.update(
                {
                    "resolution_status": "indeterminate",
                    "indeterminate_at": ts,
                    "indeterminate_reason": reason,
                }
            )
            self._conn.execute(
                """
                UPDATE notifications
                SET state = ?, resolution = ?, resolved_at = ?, updated_at = ?
                WHERE notification_id = ? AND state = ?
                """,
                (
                    STATE_INDETERMINATE,
                    json.dumps(resolution),
                    ts,
                    ts,
                    notification_id,
                    STATE_RUNNING,
                ),
            )
            self._conn.commit()
            return self._get_locked(notification_id)

    def update_delivery_status(
        self,
        notification_id: str,
        *,
        status: str,
        now: str | None = None,
    ) -> dict:
        if status not in {"pending", "queued", "failed", "delivered"}:
            raise InvalidNotification("invalid notification delivery status")
        ts = now or _iso_now()
        with self._lock:
            self._require_open()
            record = self._get_locked(notification_id)
            if record is None:
                raise NotificationNotFound(notification_id)
            resolution = record.get("resolution") if isinstance(record.get("resolution"), dict) else {}
            resolution = dict(resolution)
            resolution["delivery_status"] = status
            resolution["delivery_updated_at"] = ts
            self._conn.execute(
                "UPDATE notifications SET resolution = ?, updated_at = ? WHERE notification_id = ?",
                (json.dumps(resolution), ts, notification_id),
            )
            self._conn.commit()
            return self._get_locked(notification_id)

    def finish_run_command(
        self,
        notification_id: str,
        *,
        terminal_state: str,
        result: dict[str, Any],
        by: str,
        action_id: str | None = None,
        label: str | None = None,
        value: Any | None = None,
        now: str | None = None,
    ) -> dict:
        """Finish a claimed run-command notification as ``done`` or ``failed``."""
        if terminal_state not in {STATE_DONE, STATE_FAILED}:
            raise ValueError(f"terminal_state must be 'done' or 'failed'; got {terminal_state!r}")
        normalized_result = _validate_run_command_result(result)
        ts = now or _iso_now()

        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT state, resolution FROM notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise NotificationNotFound(notification_id)
            if row["state"] != STATE_RUNNING:
                raise NotificationTerminalState(row["state"])

            resolution: dict[str, Any] = {}
            if row["resolution"]:
                decoded = json.loads(row["resolution"])
                if isinstance(decoded, dict):
                    resolution.update(decoded)
            resolution.update(
                {
                    "by": by,
                    "at": ts,
                    "action_kind": "run_command",
                    "result": normalized_result,
                }
            )
            if action_id is not None:
                resolution["action_id"] = action_id
            if label is not None:
                resolution["label"] = label
            if value is not None:
                resolution["value"] = value
            resolution_json = json.dumps(resolution)
            cursor = self._conn.execute(
                """
                UPDATE notifications
                SET state = ?, resolution = ?, resolved_at = ?, updated_at = ?
                WHERE notification_id = ? AND state = ?
                """,
                (terminal_state, resolution_json, ts, ts, notification_id, STATE_RUNNING),
            )
            if cursor.rowcount != 1:
                current = self._conn.execute(
                    "SELECT state FROM notifications WHERE notification_id = ?",
                    (notification_id,),
                ).fetchone()
                if current is None:
                    raise NotificationNotFound(notification_id)
                raise NotificationTerminalState(current["state"])
            self._conn.commit()
            return self._get_locked(notification_id)

    def expire_due(self, now: str | None = None) -> list[str]:
        """Expire every ``open`` row whose ``expires_at`` is due (``<= now``).

        Returns the ids that transitioned to ``expired``. Expiry is
        system-driven: each expired row gets a
        ``resolution={"by": "system", "action_kind": "expired", ...}``.
        """
        ts = now or _iso_now()
        now_dt = _parse_iso(ts)
        expired_ids: list[str] = []
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT notification_id, expires_at FROM notifications "
                "WHERE state = ? AND expires_at IS NOT NULL",
                (STATE_OPEN,),
            ).fetchall()
            for row in rows:
                if _parse_iso(row["expires_at"]) <= now_dt:
                    expired_ids.append(row["notification_id"])
            for nid in expired_ids:
                system_resolution = {
                    "by": "system",
                    "at": ts,
                    "action_kind": "expired",
                    "actor_class": "system",
                    "actor_stream_id": None,
                    "actor_client": None,
                    "actor_verified": False,
                }
                resolution_json = json.dumps(
                    system_resolution
                )
                self._conn.execute(
                    """
                    UPDATE notifications
                    SET state = ?, resolution = ?, resolved_at = ?, updated_at = ?
                    WHERE notification_id = ?
                    """,
                    (STATE_EXPIRED, resolution_json, ts, ts, nid),
                )
                self._terminalize_agent_question_for_notification_locked(
                    nid,
                    state=QUESTION_STATE_EXPIRED,
                    resolution=system_resolution,
                    now=ts,
                )
            if expired_ids:
                self._conn.commit()
        return expired_ids

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def prune_resolved(self, now: str | None = None) -> int:
        """Bound the size/age of terminal rows. Non-terminal rows are never pruned.

        Among terminal rows, delete:
        * everything beyond the most recent ``resolved_keep`` (ordered by
          ``resolved_at`` DESC, tie-break rowid DESC); and
        * any terminal row whose ``resolved_at`` is older than
          ``resolved_max_age_days``.

        Returns the number of rows deleted. Idempotent: a second call with no
        new data deletes 0.
        """
        ts = now or _iso_now()
        cutoff = _parse_iso(ts) - timedelta(days=self.resolved_max_age_days)
        with self._lock:
            self._require_open()
            # Terminal rows newest-first. resolved_at should always be set for
            # terminal rows, but tolerate NULL by sorting it last. Tie-break on
            # notification_id (not rowid) because rowids are reused after
            # deletes, which would make the keep-N survivor set unstable across
            # successive prunes.
            terminal_placeholders = ", ".join("?" for _ in TERMINAL_STATES)
            rows = self._conn.execute(
                "SELECT notification_id, resolved_at FROM notifications "
                f"WHERE state IN ({terminal_placeholders}) "
                "ORDER BY (resolved_at IS NULL), resolved_at DESC, notification_id DESC",
                tuple(TERMINAL_STATES),
            ).fetchall()

            to_delete: set[str] = set()
            for index, row in enumerate(rows):
                nid = row["notification_id"]
                # Keep-N: anything past the most recent resolved_keep is pruned.
                if index >= self.resolved_keep:
                    to_delete.add(nid)
                    continue
                # Age bound: prune rows older than the max age regardless of
                # their keep-N position.
                resolved_at = row["resolved_at"]
                if resolved_at is not None and _parse_iso(resolved_at) < cutoff:
                    to_delete.add(nid)

            if not to_delete:
                return 0
            self._conn.executemany(
                "DELETE FROM notifications WHERE notification_id = ?",
                [(nid,) for nid in to_delete],
            )
            self._conn.commit()
        return len(to_delete)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("notification store is closed")

    def _get_locked(self, notification_id: str) -> dict | None:
        row = self._conn.execute(
            f"SELECT {', '.join(NOTIFICATION_COLUMNS)} "
            "FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def _get_agent_question_locked(self, question_id: str) -> dict | None:
        row = self._conn.execute(
            f"SELECT {', '.join(AGENT_QUESTION_COLUMNS)} "
            "FROM agent_questions WHERE question_id = ?",
            (question_id,),
        ).fetchone()
        if row is None:
            return None
        question = _question_row_to_dict(row)
        envelope = question.get("envelope")
        context = envelope.get("context") if isinstance(envelope, dict) else None
        if isinstance(context, dict) and context.get("schema") == "HandoffModelChangeApprovalV1":
            # The internal question-id dedup key is deliberately not part of
            # the strict, operator-visible approval envelope.
            question["dedup_key"] = None
        return question


def open_store(path: str | os.PathLike[str] | None = None) -> NotificationStore:
    return NotificationStore(path)
