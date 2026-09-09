"""Vendored asset content validation for session-scoped review assets."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from typing import Any


CONTENT_TYPES = frozenset({"report"})
ASSET_BODY_MAX_BYTES_ENV = "PENTACLE_ASSET_BODY_MAX_BYTES"
DEFAULT_ASSET_BODY_MAX_BYTES = 1024 * 1024

REPORT_SCHEMA_VERSION = 1
REPORT_SECTION_STATUSES = frozenset(
    {"dispatched", "in_progress", "stalled", "blocked", "reference"}
)
REPORT_BLOCK_TYPES = frozenset({"para", "list", "table", "callout"})
REPORT_CALLOUT_KINDS = frozenset({"warn", "info"})
REPORT_RUN_TYPES = frozenset({"text", "chip", "link", "code"})
REPORT_CHIP_VARIANTS = frozenset({"plain", "typed", "status", "link"})
REPORT_CHIP_KINDS = frozenset(
    {"generic", "epic", "branch", "db", "database", "story", "file"}
)
REPORT_CHIP_STATUSES = frozenset({"ok", "warn", "stop", "info"})


class AssetValidationError(ValueError):
    """Raised when an asset payload fails schema or size validation."""


class AssetBodyTooLarge(AssetValidationError):
    """Raised when a UTF-8 asset body exceeds the configured size cap."""


def asset_body_max_bytes() -> int:
    raw = os.environ.get(ASSET_BODY_MAX_BYTES_ENV)
    if raw is None or raw == "":
        return DEFAULT_ASSET_BODY_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_ASSET_BODY_MAX_BYTES
    return value if value > 0 else DEFAULT_ASSET_BODY_MAX_BYTES


def validate_asset_payload(content_type: str, payload: Any) -> str:
    content_type = normalize_content_type(content_type)
    if content_type == "report":
        if isinstance(payload, str):
            _check_body_size(payload)
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise AssetValidationError(f"report payload is not valid JSON: {exc}") from exc
        else:
            parsed = payload
        _validate_report(parsed)
        normalized = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _check_body_size(normalized)
        return normalized

    raise AssetValidationError(f"unsupported content_type: {content_type}")


def normalize_content_type(value: Any) -> str:
    content_type = str(value or "")
    if content_type not in CONTENT_TYPES:
        raise AssetValidationError(
            "content_type must be one of: " + ", ".join(sorted(CONTENT_TYPES))
        )
    return content_type


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise AssetValidationError("tags must be a list of strings")
    tags = list(value)
    if not all(isinstance(item, str) for item in tags):
        raise AssetValidationError("tags must be a list of strings")
    return tags


def _check_body_size(body: str) -> None:
    size = len(body.encode("utf-8"))
    cap = asset_body_max_bytes()
    if size > cap:
        raise AssetBodyTooLarge(f"asset body exceeds {cap} UTF-8 bytes")


def report_block_text(body: str, section_id: str, block_id: str) -> str | None:
    try:
        report = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(report, Mapping):
        return None
    for section in report.get("sections") or []:
        if not isinstance(section, Mapping) or section.get("id") != section_id:
            continue
        for block in section.get("blocks") or []:
            if isinstance(block, Mapping) and block.get("id") == block_id:
                return _block_text(block)
    return None


def report_has_block(body: str, section_id: str, block_id: str) -> bool:
    return report_block_text(body, section_id, block_id) is not None


def _validate_report(value: Any) -> None:
    if not isinstance(value, Mapping):
        _raise_path("$", "report payload must be an object")
    _reject_unknown_keys(value, "$", {"schema_version", "title", "sections"})
    if value.get("schema_version") != REPORT_SCHEMA_VERSION:
        _raise_path("$.schema_version", "must be 1")
    _require_string(value, "title", "$.title")
    sections = value.get("sections")
    if not isinstance(sections, list) or not sections:
        _raise_path("$.sections", "must be a non-empty list")
    seen_ids: set[str] = set()
    for section_index, section in enumerate(sections):
        section_path = f"$.sections[{section_index}]"
        if not isinstance(section, Mapping):
            _raise_path(section_path, "must be an object")
        _reject_unknown_keys(section, section_path, {"id", "title", "status", "blocks"})
        section_id = _require_stable_id(section, section_path, seen_ids)
        _require_string(section, "title", f"{section_path}.title")
        status = section.get("status")
        if status not in REPORT_SECTION_STATUSES:
            _raise_path(
                f"{section_path}.status",
                "must be one of: " + ", ".join(sorted(REPORT_SECTION_STATUSES)),
            )
        blocks = section.get("blocks")
        if not isinstance(blocks, list):
            _raise_path(f"{section_path}.blocks", "must be a list")
        for block_index, block in enumerate(blocks):
            _validate_report_block(
                block,
                path=f"{section_path}.blocks[{block_index}]",
                seen_ids=seen_ids,
                section_id=section_id,
            )


def _validate_report_block(
    block: Any, *, path: str, seen_ids: set[str], section_id: str
) -> None:
    if not isinstance(block, Mapping):
        _raise_path(path, "must be an object")
    _require_stable_id(block, path, seen_ids)
    block_type = block.get("type")
    if block_type not in REPORT_BLOCK_TYPES:
        _raise_path(
            f"{path}.type",
            "must be one of: " + ", ".join(sorted(REPORT_BLOCK_TYPES)),
        )
    if block_type == "para":
        _reject_unknown_keys(block, path, {"id", "type", "runs", "section_id"})
        _validate_runs(block.get("runs"), f"{path}.runs")
    elif block_type == "list":
        _reject_unknown_keys(block, path, {"id", "type", "ordered", "items", "section_id"})
        if "ordered" in block and not isinstance(block.get("ordered"), bool):
            _raise_path(f"{path}.ordered", "must be a boolean when present")
        items = block.get("items")
        if not isinstance(items, list):
            _raise_path(f"{path}.items", "must be a list")
        for item_index, item in enumerate(items):
            _validate_runs(item, f"{path}.items[{item_index}]")
    elif block_type == "table":
        _reject_unknown_keys(block, path, {"id", "type", "columns", "rows", "section_id"})
        _validate_table_block(block, path)
    elif block_type == "callout":
        _reject_unknown_keys(block, path, {"id", "type", "kind", "title", "runs", "section_id"})
        kind = block.get("kind", "info")
        if kind not in REPORT_CALLOUT_KINDS:
            _raise_path(
                f"{path}.kind",
                "must be one of: " + ", ".join(sorted(REPORT_CALLOUT_KINDS)),
            )
        if "title" in block:
            _require_string(block, "title", f"{path}.title")
        _validate_runs(block.get("runs"), f"{path}.runs")
    if "section_id" in block and block.get("section_id") != section_id:
        _raise_path(f"{path}.section_id", "must match containing section id")


def _validate_table_block(block: Mapping, path: str) -> None:
    columns = block.get("columns")
    if not isinstance(columns, list) or not columns or not all(isinstance(item, str) and item for item in columns):
        _raise_path(f"{path}.columns", "must be a non-empty list of strings")
    rows = block.get("rows")
    if not isinstance(rows, list):
        _raise_path(f"{path}.rows", "must be a list")
    width = len(columns)
    for row_index, row in enumerate(rows):
        row_path = f"{path}.rows[{row_index}]"
        if not isinstance(row, list):
            _raise_path(row_path, "must be a list")
        if len(row) != width:
            _raise_path(row_path, "length must match columns")
        for cell_index, cell in enumerate(row):
            _validate_runs(cell, f"{row_path}[{cell_index}]")


def _validate_runs(runs: Any, path: str) -> None:
    if not isinstance(runs, list):
        _raise_path(path, "must be a list")
    for run_index, run in enumerate(runs):
        run_path = f"{path}[{run_index}]"
        if isinstance(run, str):
            continue
        if not isinstance(run, Mapping):
            _raise_path(run_path, "must be a string or run object")
        run_type = run.get("type")
        if run_type is None and "chip" in run:
            run_type = "chip"
        if run_type not in REPORT_RUN_TYPES:
            _raise_path(
                f"{run_path}.type",
                "must be one of: " + ", ".join(sorted(REPORT_RUN_TYPES)),
            )
        if run_type == "text":
            _reject_unknown_keys(run, run_path, {"type", "text"})
            _require_string(run, "text", f"{run_path}.text")
        elif run_type == "code":
            _reject_unknown_keys(run, run_path, {"type", "text"})
            _require_string(run, "text", f"{run_path}.text")
        elif run_type == "link":
            _reject_unknown_keys(run, run_path, {"type", "text", "href"})
            _require_string(run, "text", f"{run_path}.text")
            href = _require_string(run, "href", f"{run_path}.href")
            if not (href.startswith("http://") or href.startswith("https://")):
                _raise_path(f"{run_path}.href", "must start with http:// or https://")
        elif run_type == "chip":
            _reject_unknown_keys(
                run,
                run_path,
                {"type", "text", "chip", "variant", "kind", "status", "href"},
            )
            text_key = "text" if "text" in run else "chip"
            _require_string(run, text_key, f"{run_path}.{text_key}")
            variant = run.get("variant")
            if variant is None:
                variant = "status" if run.get("status") is not None else ("typed" if run.get("kind") is not None else "plain")
            if variant not in REPORT_CHIP_VARIANTS:
                _raise_path(
                    f"{run_path}.variant",
                    "must be one of: " + ", ".join(sorted(REPORT_CHIP_VARIANTS)),
                )
            kind = run.get("kind")
            if kind is not None and kind not in REPORT_CHIP_KINDS:
                _raise_path(
                    f"{run_path}.kind",
                    "must be one of: " + ", ".join(sorted(REPORT_CHIP_KINDS)),
                )
            status = run.get("status")
            if status is not None and status not in REPORT_CHIP_STATUSES:
                _raise_path(
                    f"{run_path}.status",
                    "must be one of: " + ", ".join(sorted(REPORT_CHIP_STATUSES)),
                )
            if variant == "link":
                href = _require_string(run, "href", f"{run_path}.href")
                if not (href.startswith("http://") or href.startswith("https://")):
                    _raise_path(f"{run_path}.href", "must start with http:// or https://")


def _require_stable_id(value: Mapping, path: str, seen_ids: set[str]) -> str:
    stable_id = _require_string(value, "id", f"{path}.id")
    if stable_id in seen_ids:
        _raise_path(f"{path}.id", f"duplicate id {stable_id!r}")
    seen_ids.add(stable_id)
    return stable_id


def _require_string(value: Mapping, key: str, path: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        _raise_path(path, "must be a non-empty string")
    return item


def _reject_unknown_keys(value: Mapping, path: str, allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in value.keys() if key not in allowed)
    if unknown:
        _raise_path(path, "unknown keys: " + ", ".join(unknown))


def _raise_path(path: str, message: str) -> None:
    raise AssetValidationError(f"{path}: {message}")


def _block_text(block: Mapping) -> str:
    block_type = block.get("type")
    if block_type in {"para", "callout"}:
        return _runs_text(block.get("runs") or [])
    if block_type == "list":
        return "\n".join(_runs_text(item) for item in block.get("items") or [])
    if block_type == "table":
        lines = []
        columns = block.get("columns") or []
        if columns:
            lines.append(" | ".join(str(column) for column in columns))
        for row in block.get("rows") or []:
            if isinstance(row, list):
                lines.append(" | ".join(_runs_text(cell) for cell in row))
        return "\n".join(line for line in lines if line)
    return ""


def _runs_text(runs: Any) -> str:
    if isinstance(runs, str):
        return runs
    if not isinstance(runs, list):
        return ""
    parts: list[str] = []
    for run in runs:
        if isinstance(run, str):
            parts.append(run)
        elif isinstance(run, Mapping):
            if isinstance(run.get("text"), str):
                parts.append(run["text"])
            elif isinstance(run.get("chip"), str):
                parts.append(run["chip"])
    return "".join(parts)
