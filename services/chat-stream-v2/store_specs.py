"""Spec-binding and handoff-audit persistence helpers for chat-stream-v2.

The public ``Store`` façade imports these helpers and composes the
spec-persistence mixin. The module has no runtime dependency on ``store``
so the import graph stays one-way.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

JSON_COLUMNS = ("status_card", "observer_binding")
SPEC_JSON_COLUMNS = ("spec_ids", "qualified_spec_ids", "spec_binding_provenance")
SPEC_BINDING_PROVENANCE_KINDS = frozenset(
    {"spawn_explicit", "parent_inherited", "handoff_inherited", "operator_v2"}
)

def _enc(column: str, value: Any) -> Any:
    if column in JSON_COLUMNS or column in SPEC_JSON_COLUMNS:
        if isinstance(value, (dict, list, tuple, set)):
            return json.dumps(value, separators=(",", ":"))
    return value

def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    # SQLite stores the lifecycle authorization bit as INTEGER, while the
    # report --terminate classifier consumes it as a boolean. Normalize at the
    # storage boundary so the close leg does not vary by row origin or spawn
    # shape (and so `1 is True` can never silently refuse a valid self-close).
    if "self_close_on_completion" in out:
        out["self_close_on_completion"] = bool(out["self_close_on_completion"])
    if "no_watch" in out:
        out["no_watch"] = bool(out["no_watch"])
    for col in (*JSON_COLUMNS, *SPEC_JSON_COLUMNS):
        raw = out.get(col)
        if isinstance(raw, str) and raw:
            try:
                out[col] = json.loads(raw)
            except ValueError:
                pass
    if "host" in out and "session_name" in out:
        out["stream_id"] = f"{out['host']}:{out['session_name']}"
    return out

def _session_row(conn: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Decode a sessions row and attach its v2 open-generation token."""
    out = _row(row)
    if out is None:
        return None
    out = _hydrate_spec_row(out)
    out["model"] = out.get("effective_model") or out.get("requested_model")
    out["effort"] = out.get("effective_effort") or out.get("requested_effort")
    generation = conn.execute(
        "SELECT generation FROM v2_session_generations WHERE host=? AND session_name=?",
        (out.get("host"), out.get("session_name")),
    ).fetchone()
    out["session_generation"] = str(generation[0]) if generation is not None else ""
    state = conn.execute("SELECT status,ts FROM v2_agent_report_state WHERE stream_id=? AND generation=?", (out["stream_id"], out["session_generation"])).fetchone()
    if state:
        out["_agent_report_status"], out["_agent_report_ts"] = state
    return out

def normalize_spec_ids(spec_ids: object = None, spec_id: object = None) -> list[str]:
    """Return stable, ordered, de-duplicated canonical session identities."""
    values: list[object] = []
    if isinstance(spec_ids, str):
        try:
            decoded = json.loads(spec_ids)
            values.extend(decoded if isinstance(decoded, list) else [spec_ids])
        except (TypeError, ValueError):
            values.extend(part.strip() for part in spec_ids.split(","))
    elif isinstance(spec_ids, (list, tuple, set)):
        values.extend(spec_ids)
    if spec_id:
        values.insert(0, spec_id)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        while text.startswith("spec_"):
            text = text[len("spec_"):]
        if not text:
            continue
        text = f"spec_{text}"
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized

def normalize_spec_binding_provenance(
    value: object, *, spec_ids: object = None
) -> list[dict[str, str]]:
    """Keep only complete, known binding provenance rows for current IDs."""
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    allowed_spec_ids = set(normalize_spec_ids(spec_ids))
    rows = raw if isinstance(raw, list) else []
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        binding_ids = normalize_spec_ids(spec_id=row.get("spec_id"))
        binding_spec_id = binding_ids[0] if binding_ids else ""
        provenance = str(row.get("provenance") or "").strip()
        granting_principal = str(row.get("granting_principal") or "").strip()
        granted_at = str(row.get("granted_at") or "").strip()
        if (
            not binding_spec_id
            or binding_spec_id in seen
            or binding_spec_id not in allowed_spec_ids
            or provenance not in SPEC_BINDING_PROVENANCE_KINDS
            or not granting_principal
            or not granted_at
        ):
            continue
        normalized.append({
            "spec_id": binding_spec_id,
            "provenance": provenance,
            "granting_principal": granting_principal,
            "granted_at": granted_at,
        })
        seen.add(binding_spec_id)
    return normalized

def _hydrate_spec_row(row: dict[str, Any]) -> dict[str, Any]:
    hydrated = dict(row)
    spec_ids = normalize_spec_ids(hydrated.get("spec_ids"), hydrated.get("spec_id"))
    hydrated["spec_ids"] = spec_ids
    hydrated["spec_id"] = spec_ids[0] if spec_ids else None
    bindings = normalize_spec_binding_provenance(
        hydrated.get("spec_binding_provenance"), spec_ids=spec_ids
    )
    hydrated["spec_binding_provenance"] = bindings
    hydrated["qualified_spec_ids"] = [binding["spec_id"] for binding in bindings]
    return hydrated

def _normalize_session_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Normalize binding input before it reaches SQLite or a reservation."""
    normalized = dict(fields)
    if not any(
        key in normalized
        for key in ("spec_id", "spec_ids", "qualified_spec_ids", "spec_binding_provenance")
    ):
        return normalized
    spec_ids = normalize_spec_ids(normalized.get("spec_ids"), normalized.get("spec_id"))
    bindings = normalize_spec_binding_provenance(
        normalized.get("spec_binding_provenance"), spec_ids=spec_ids
    )
    normalized["spec_id"] = spec_ids[0] if spec_ids else None
    normalized["spec_ids"] = spec_ids
    normalized["spec_binding_provenance"] = bindings
    normalized["qualified_spec_ids"] = [binding["spec_id"] for binding in bindings]
    return normalized

class _SpecPersistenceMixin:
    """Spec-binding persistence mixed into Store."""
