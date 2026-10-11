"""Observation-only direct-primary turn budgets.

The fold is pure: callers supply admitted event facts and proved dispatch
identities. The Store owns provenance, cursor persistence and atomic effects.
No timer, provider control, enforcement or notification I/O lives here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from typing import Any, Mapping


@dataclass(frozen=True)
class FdTurnBudgetConfig:
    calls: int = 5
    seconds: float = 60
    enabled: bool = False
    observer_enabled: bool = True

    def __post_init__(self) -> None:
        if type(self.calls) is not int or not 0 <= self.calls <= 10000:
            raise ValueError("fd_turn_budget.calls must be an integer from 0 to 10000")
        if (isinstance(self.seconds, bool) or not isinstance(self.seconds, (int, float))
                or not math.isfinite(self.seconds) or self.seconds < 0):
            raise ValueError("fd_turn_budget.seconds must be finite and nonnegative")
        if type(self.observer_enabled) is not bool:
            raise ValueError("fd_turn_budget.observer_enabled must be boolean")
        if type(self.enabled) is not bool:
            raise ValueError("fd_turn_budget.enabled must be boolean")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FdTurnBudgetConfig:
        values = os.environ if env is None else env
        enabled = values.get("PENTACLE_FD_TURN_BUDGET_ENABLED", "false").lower().strip()
        if enabled not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("invalid PENTACLE_FD_TURN_BUDGET_ENABLED")
        observer = values.get("PENTACLE_FD_TURN_BUDGET_OBSERVER_ENABLED", "false").lower().strip()
        if observer not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("invalid PENTACLE_FD_TURN_BUDGET_OBSERVER_ENABLED")
        return cls(calls=int(values.get("PENTACLE_FD_TURN_BUDGET_CALLS", "5")),
                   seconds=float(values.get("PENTACLE_FD_TURN_BUDGET_SECONDS", "60")),
                   enabled=enabled in {"true", "1", "yes", "on"},
                   observer_enabled=observer in {"true", "1", "yes", "on"})


def timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        result = parsed.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError, OSError):
        return None


def turn_key(stream_id: str, generation: str, ordinal: int) -> str:
    value = json.dumps([stream_id, generation, ordinal], separators=(",", ":"))
    return "fd-turn-budget:" + hashlib.sha256(value.encode()).hexdigest()


def initial_state(stream_id: str, generation: str, lifecycle: str) -> dict[str, Any]:
    return {"schema": 1, "stream_id": stream_id, "generation": generation,
            "lifecycle": lifecycle, "ordinal": 0, "cursor": 0,
            "cursor_identity": None, "boundary": None, "tool_ids": [],
            "first": None, "latest": None, "crossed": False,
            "gap": None, "continuity_gap": None, "source_session_identity": None}


def decode_state(value: str, stream_id: str, generation: str, lifecycle: str) -> dict[str, Any]:
    """Refuse corrupt state rather than turning a lost cursor into ordinal one."""
    state = json.loads(value)
    if (not isinstance(state, dict) or state.get("schema") != 1
            or not initial_state(stream_id, generation, lifecycle).keys() <= state.keys()):
        raise ValueError("cursor_schema_invalid")
    for key, expected in (("stream_id", stream_id), ("generation", generation), ("lifecycle", lifecycle)):
        if state.get(key) != expected:
            raise ValueError("cursor_identity_changed")
    for key in ("ordinal", "cursor"):
        if type(state.get(key)) is not int or not 0 <= state[key] <= 2**63 - 1:
            raise ValueError("cursor_invalid")
    ids = state.get("tool_ids")
    if (not isinstance(ids, list) or len(ids) > 10001
            or any(not isinstance(i, str) or not i or len(i) > 512 for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("cursor_tool_ids_invalid")
    if type(state.get("crossed")) is not bool:
        raise ValueError("cursor_crossing_invalid")
    for key in ("first", "latest"):
        if state.get(key) is not None and (type(state[key]) not in (float, int) or not math.isfinite(state[key])):
            raise ValueError("cursor_timestamp_invalid")
    for key in ("cursor_identity", "boundary", "gap", "continuity_gap", "source_session_identity"):
        if state.get(key) is not None and not isinstance(state[key], str):
            raise ValueError("cursor_fact_invalid")
    if state["ordinal"] and not state.get("boundary"):
        raise ValueError("cursor_boundary_missing")
    if state["cursor"] and not state.get("cursor_identity"):
        raise ValueError("cursor_identity_missing")
    if (state["cursor"] == 0 and (state["ordinal"] != 0 or state["cursor_identity"] is not None)
            or state["ordinal"] == 0 and (state["boundary"] is not None or ids or state["first"] is not None
                                         or state["latest"] is not None or state["crossed"])
            or state["first"] is None and state["latest"] is not None
            or state["first"] is not None and state["latest"] is not None and state["latest"] < state["first"]):
        raise ValueError("cursor_invariant_invalid")
    if "cursor_hash" in state and (not isinstance(state["cursor_hash"], str) or len(state["cursor_hash"]) != 64):
        raise ValueError("cursor_hash_invalid")
    if "pending_state" in state:
        proofs = state.get("pending_proofs")
        if (not isinstance(state["pending_state"], dict) or not isinstance(proofs, list) or not 1 <= len(proofs) <= 128
                or any(not isinstance(p, list) or len(p) != 4 or type(p[0]) is not int
                       or not isinstance(p[1], str) or not isinstance(p[2], str) or len(p[2]) != 64
                       or type(p[3]) is not bool for p in proofs)):
            raise ValueError("pending_proof_invalid")
        if "pending_state" in state["pending_state"]:
            raise ValueError("pending_checkpoint_nested")
        checkpoint = decode_state(json.dumps(state["pending_state"]), stream_id, generation, lifecycle)
        if (proofs[0][0] <= checkpoint["cursor"] or proofs[-1][0] != state["cursor"]
                or proofs[-1][1] != state["cursor_identity"]
                or any(left[0] >= right[0] for left, right in zip(proofs, proofs[1:]))):
            raise ValueError("pending_interval_invalid")
    return state


def observe(event: dict[str, Any], state: dict[str, Any], *, config: FdTurnBudgetConfig,
            identity: str | None, cursor: int, boundary: str | None = None,
            provenance_gap: str | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Fold one accepted source event; return new small state and first crossing.

    ``boundary`` is an already-proved inbound dispatch identity, never inferred
    from the TELL label here. A continuity gap is sticky until the authoritative
    generation changes; a malformed work fact invalidates only its current turn.
    Calls win the deterministic dimension tie when both budgets cross together.
    """
    result = {**state, "tool_ids": list(state["tool_ids"])}
    if cursor <= result["cursor"]:
        return result, None
    result.update(cursor=cursor, cursor_identity=identity)
    if not isinstance(identity, str) or not identity or len(identity) > 2048:
        result["cursor_identity"] = "invalid"
        result["continuity_gap"] = "source_identity_missing_or_invalid"
    if boundary is not None and (not isinstance(boundary, str) or not boundary or len(boundary) > 2048):
        result["continuity_gap"] = "dispatch_identity_invalid"
    if provenance_gap:
        result["continuity_gap"] = provenance_gap
    if result["continuity_gap"]:
        return result, None
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    if raw.get("is_sidechain") is True or event.get("kind") in {"SYSTEM", "THINKING"}:
        return result, None
    native = raw.get("source_session_identity")
    if isinstance(native, str) and native:
        if result["source_session_identity"] not in (None, native):
            result["continuity_gap"] = "transcript_rebind_continuity_unproved"
            return result, None
        result["source_session_identity"] = native
    if boundary is not None:
        if boundary == result["boundary"]:
            return result, None
        result.update(ordinal=result["ordinal"] + 1, boundary=boundary, tool_ids=[],
                      first=None, latest=None, crossed=False,
                      gap=None if timestamp(event.get("timestamp")) is not None else "dispatch_timestamp_invalid")
        return result, None
    kind = event.get("kind")
    if kind not in {"ASSIST_TEXT", "TOOL_USE", "TOOL_RESULT"} or result["crossed"]:
        return result, None
    if not result["ordinal"]:
        result["continuity_gap"] = "work_before_proved_dispatch"
        return result, None
    when = timestamp(event.get("timestamp"))
    if when is None:
        result["gap"] = "work_timestamp_invalid"
        return result, None
    if kind == "TOOL_USE":
        tool_id = raw.get("tool_use_id")
        if not isinstance(tool_id, str) or not tool_id or len(tool_id) > 512:
            result["gap"] = "tool_use_id_invalid"
            return result, None
        if tool_id not in result["tool_ids"]:
            result["tool_ids"].append(tool_id)
            result["tool_ids"] = result["tool_ids"][:config.calls + 1]
    if result["first"] is None and kind in {"ASSIST_TEXT", "TOOL_USE"}:
        result["first"] = when
    if result["first"] is not None:
        if when < result["first"]:
            result["gap"] = "work_timestamp_regressed"
            return result, None
        result["latest"] = max(when, result["latest"] or when)
    if result["gap"] or result["first"] is None:
        # A malformed turn must still remain bounded until the next dispatch.
        result["tool_ids"] = result["tool_ids"][:config.calls + 1]
        return result, None
    calls = len(result["tool_ids"])
    seconds = result["latest"] - result["first"]
    if calls <= config.calls and seconds <= config.seconds:
        return result, None
    result["crossed"] = True
    crossing = {"kind": "fd_turn_budget", "turn_id": [result["stream_id"], result["generation"], result["ordinal"]],
                "stream_id": result["stream_id"], "session_generation": result["generation"],
                "calls": calls, "seconds": seconds,
                "dimension": "calls" if calls > config.calls else "seconds",
                "at": event["timestamp"], "timestamp": event["timestamp"],
                "record_only": not config.enabled}
    return result, crossing


def crossings(events: list[dict[str, Any]], *, state: dict[str, Any], config: FdTurnBudgetConfig,
              facts: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Pure list adapter; each event has caller-proved identity/cursor facts."""
    if len(events) != len(facts):
        raise ValueError("event/fact count differs")
    found = []
    for event, fact in zip(events, facts, strict=True):
        state, crossing = observe(event, state, config=config, **fact)
        if crossing is not None:
            found.append(crossing)
    return state, found
