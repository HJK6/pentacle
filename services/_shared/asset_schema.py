"""Vendored asset content validation for session-scoped review assets."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any


CONTENT_TYPES = frozenset({"report", "dashboard-catalog"})
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

# dashboard-catalog (schema_version 1): bounded JSON data naming dashboard
# boards for the generic dashboard hosts. No executable expressions, no URLs
# except hosted.url, no daemon verb names. Clients mirror these rules.
CATALOG_SCHEMA_VERSION = 1
CATALOG_ASSET_ID = "dashboard-catalog"
CATALOG_MAX_LIBS = 8
CATALOG_MAX_BOARDS = 64
CATALOG_MAX_ENTRY_BYTES = 8 * 1024
CATALOG_BOARD_KINDS = frozenset({"report", "web-adapter", "hosted-view"})
CATALOG_HOST_ACTIONS = frozenset({"household", "assistantState", "assetList", "assetGet"})
CATALOG_VERSION_RE = re.compile(r"[0-9A-Za-z.+-]{1,64}")
CATALOG_BOARD_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
CATALOG_WEB_PATH_RE = re.compile(r"web/[a-z0-9][a-z0-9._-]{0,80}\.(js|css)")
CATALOG_SHA256_RE = re.compile(r"[0-9a-f]{64}")
CATALOG_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
CATALOG_SPEC_ID_RE = re.compile(r"[A-Za-z0-9_-]+__[A-Za-z0-9_-]+")
CATALOG_ASSET_ID_PREFIX_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
CATALOG_STREAM_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}:[A-Za-z0-9._-]{1,128}")
# key_format tokens: a fixed-count class over [0-9] or [A-Z], or one literal
# from [0-9A-Z_-] or an escaped dot. Anything else (ranges, alternation,
# optional parts, other classes, \d, a bare ".") is refused, so every admitted
# key has the same width and an ASCII-order-aligned alphabet.
_KEY_FORMAT_TOKEN_RE = re.compile(r"\[(0-9|A-Z)\](?:\{([0-9]{1,2})\})?|([0-9A-Z_-])|(\\\.)")
CATALOG_KEY_WIDTH_MIN = 4
CATALOG_KEY_WIDTH_MAX = 32
CATALOG_TITLE_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


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
    if content_type == "dashboard-catalog":
        if isinstance(payload, str):
            _check_body_size(payload)
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise AssetValidationError(
                    f"dashboard-catalog payload is not valid JSON: {exc}"
                ) from exc
        else:
            parsed = payload
        validate_dashboard_catalog(parsed)
        # Key order is preserved: board order is the catalog's view order.
        normalized = json.dumps(parsed, ensure_ascii=False, indent=2) + "\n"
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


def key_format_width(key_format: Any) -> int:
    """Width of a supported fixed-width key_format; raises otherwise."""
    if not isinstance(key_format, str) or not key_format:
        raise AssetValidationError("key_format must be a non-empty string")
    width = 0
    pos = 0
    while pos < len(key_format):
        match = _KEY_FORMAT_TOKEN_RE.match(key_format, pos)
        if match is None:
            raise AssetValidationError(
                f"key_format: unsupported token at offset {pos} "
                "(only [0-9]{n}, [A-Z]{n}, literals [0-9A-Z_-] and \\. are allowed)"
            )
        count = match.group(2)
        if match.group(1) is not None and count is not None:
            if int(count) < 1:
                raise AssetValidationError("key_format: class count must be >= 1")
            width += int(count)
        else:
            width += 1
        pos = match.end()
    if not CATALOG_KEY_WIDTH_MIN <= width <= CATALOG_KEY_WIDTH_MAX:
        raise AssetValidationError(
            f"key_format: width {width} outside {CATALOG_KEY_WIDTH_MIN}-{CATALOG_KEY_WIDTH_MAX}"
        )
    return width


def report_id_grammar(asset_id_prefix: str, key_format: str, rev_width: int) -> re.Pattern:
    """Reader grammar ^<prefix><key>(-r<rev>)?$ with all-zero revisions excluded."""
    key_format_width(key_format)
    suffix = f"(-r(?!0+$)[0-9]{{{rev_width}}})?" if rev_width else ""
    return re.compile(f"^{re.escape(asset_id_prefix)}({key_format}){suffix}$")


def validate_dashboard_catalog(value: Any) -> None:
    path = "catalog"
    if not isinstance(value, Mapping):
        _raise_path(path, "must be an object")
    _reject_unknown_keys(
        value, path, {"schema_version", "catalog_version", "package", "requires", "libs", "boards"}
    )
    if value.get("schema_version") != CATALOG_SCHEMA_VERSION or isinstance(
        value.get("schema_version"), bool
    ):
        _raise_path(f"{path}.schema_version", f"must be {CATALOG_SCHEMA_VERSION}")
    _require_match(value, "catalog_version", CATALOG_VERSION_RE, f"{path}.catalog_version")

    package = value.get("package")
    if not isinstance(package, Mapping):
        _raise_path(f"{path}.package", "must be an object")
    _reject_unknown_keys(package, f"{path}.package", {"repo", "commit"})
    repo = _require_string(package, "repo", f"{path}.package.repo")
    if len(repo) > 128:
        _raise_path(f"{path}.package.repo", "must be at most 128 characters")
    _require_match(package, "commit", CATALOG_COMMIT_RE, f"{path}.package.commit")

    requires = value.get("requires")
    if not isinstance(requires, Mapping):
        _raise_path(f"{path}.requires", "must be an object")
    _reject_unknown_keys(requires, f"{path}.requires", {"host_api"})
    host_api = requires.get("host_api")
    if not _is_int(host_api) or host_api < 1:
        _raise_path(f"{path}.requires.host_api", "must be an integer >= 1")

    libs = value.get("libs", [])
    if not isinstance(libs, list) or len(libs) > CATALOG_MAX_LIBS:
        _raise_path(f"{path}.libs", f"must be a list of at most {CATALOG_MAX_LIBS} entries")
    for index, lib in enumerate(libs):
        lib_path = f"{path}.libs[{index}]"
        if not isinstance(lib, Mapping):
            _raise_path(lib_path, "must be an object")
        _reject_unknown_keys(lib, lib_path, {"path", "sha256"})
        _require_match(lib, "path", CATALOG_WEB_PATH_RE, f"{lib_path}.path")
        _require_match(lib, "sha256", CATALOG_SHA256_RE, f"{lib_path}.sha256")

    boards = value.get("boards")
    if not isinstance(boards, list) or len(boards) > CATALOG_MAX_BOARDS:
        _raise_path(f"{path}.boards", f"must be a list of at most {CATALOG_MAX_BOARDS} entries")
    seen_ids: set[str] = set()
    for index, board in enumerate(boards):
        _validate_catalog_board(board, f"{path}.boards[{index}]", seen_ids)


def _validate_catalog_board(board: Any, path: str, seen_ids: set[str]) -> None:
    if not isinstance(board, Mapping):
        _raise_path(path, "must be an object")
    if len(json.dumps(board, ensure_ascii=False).encode("utf-8")) > CATALOG_MAX_ENTRY_BYTES:
        _raise_path(path, f"entry exceeds {CATALOG_MAX_ENTRY_BYTES} bytes serialized")
    board_id = _require_match(board, "id", CATALOG_BOARD_ID_RE, f"{path}.id")
    if board_id in seen_ids:
        _raise_path(f"{path}.id", f"duplicate board id {board_id!r}")
    seen_ids.add(board_id)
    name = _require_string(board, "name", f"{path}.name")
    if len(name) > 64:
        _raise_path(f"{path}.name", "must be at most 64 characters")
    if "description" in board:
        description = board.get("description")
        if not isinstance(description, str) or len(description) > 200:
            _raise_path(f"{path}.description", "must be a string of at most 200 characters")
    kind = board.get("kind")
    if kind not in CATALOG_BOARD_KINDS:
        _raise_path(f"{path}.kind", "must be one of: " + ", ".join(sorted(CATALOG_BOARD_KINDS)))
    common = {"id", "name", "description", "kind"}
    if kind == "report":
        _reject_unknown_keys(board, path, common | {"report"})
        _validate_catalog_report(board.get("report"), f"{path}.report")
    elif kind == "web-adapter":
        _reject_unknown_keys(board, path, common | {"web", "actions", "poll_interval_ms"})
        _validate_catalog_web(board.get("web"), f"{path}.web")
        if "actions" in board:
            actions = board.get("actions")
            if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
                _raise_path(f"{path}.actions", "must be a list of strings")
            unknown = sorted(set(actions) - CATALOG_HOST_ACTIONS)
            if unknown:
                _raise_path(f"{path}.actions", "unknown host actions: " + ", ".join(unknown))
        if "poll_interval_ms" in board:
            interval = board.get("poll_interval_ms")
            if not _is_int(interval) or not 2000 <= interval <= 600000:
                _raise_path(f"{path}.poll_interval_ms", "must be an integer 2000-600000")
    else:
        _reject_unknown_keys(board, path, common | {"hosted"})
        hosted = board.get("hosted")
        if not isinstance(hosted, Mapping):
            _raise_path(f"{path}.hosted", "must be an object")
        _reject_unknown_keys(hosted, f"{path}.hosted", {"url"})
        url = _require_string(hosted, "url", f"{path}.hosted.url")
        if (
            len(url) > 2048
            or not re.fullmatch(r"https?://[^\s/?#@]+(?:[/?#][^\s]*)?", url)
        ):
            _raise_path(f"{path}.hosted.url", "must be an absolute http(s) URL without userinfo")


def _validate_catalog_web(web: Any, path: str) -> None:
    if not isinstance(web, Mapping):
        _raise_path(path, "must be an object")
    _reject_unknown_keys(web, path, {"script", "sha256", "css", "css_sha256"})
    script = _require_match(web, "script", CATALOG_WEB_PATH_RE, f"{path}.script")
    if not script.endswith(".js"):
        _raise_path(f"{path}.script", "must be a .js path")
    _require_match(web, "sha256", CATALOG_SHA256_RE, f"{path}.sha256")
    if "css" in web or "css_sha256" in web:
        css = _require_match(web, "css", CATALOG_WEB_PATH_RE, f"{path}.css")
        if not css.endswith(".css"):
            _raise_path(f"{path}.css", "must be a .css path")
        _require_match(web, "css_sha256", CATALOG_SHA256_RE, f"{path}.css_sha256")


def _validate_catalog_report(report: Any, path: str) -> None:
    if not isinstance(report, Mapping):
        _raise_path(path, "must be an object")
    _reject_unknown_keys(
        report,
        path,
        {
            "spec_id", "asset_id_prefix", "key_format", "rev_width", "producer_stream_id",
            "writer_enforced", "select", "list", "history_limit", "title_template",
        },
    )
    spec_id = _require_match(report, "spec_id", CATALOG_SPEC_ID_RE, f"{path}.spec_id")
    if spec_id.count("__") != 1:
        _raise_path(f"{path}.spec_id", "must be a spec id <repo>__<topic>")
    _require_match(report, "asset_id_prefix", CATALOG_ASSET_ID_PREFIX_RE, f"{path}.asset_id_prefix")
    try:
        key_format_width(report.get("key_format"))
    except AssetValidationError as exc:
        _raise_path(f"{path}.key_format", str(exc))
    rev_width = report.get("rev_width")
    if not _is_int(rev_width) or not 0 <= rev_width <= 3:
        _raise_path(f"{path}.rev_width", "must be an integer 0-3")
    if "producer_stream_id" in report:
        _require_match(report, "producer_stream_id", CATALOG_STREAM_ID_RE, f"{path}.producer_stream_id")
    if not isinstance(report.get("writer_enforced"), bool):
        _raise_path(f"{path}.writer_enforced", "must be a boolean")
    if report.get("select") != "latest":
        _raise_path(f"{path}.select", "must be 'latest'")
    if not isinstance(report.get("list"), bool):
        _raise_path(f"{path}.list", "must be a boolean")
    history_limit = report.get("history_limit")
    if not _is_int(history_limit) or not 1 <= history_limit <= 100:
        _raise_path(f"{path}.history_limit", "must be an integer 1-100")
    if "title_template" in report:
        template = report.get("title_template")
        if not isinstance(template, str) or len(template) > 80:
            _raise_path(f"{path}.title_template", "must be a string of at most 80 characters")
        bad = [p for p in CATALOG_TITLE_PLACEHOLDER_RE.findall(template) if p not in {"key", "rev"}]
        if bad or template.count("{") != template.count("}"):
            _raise_path(f"{path}.title_template", "only {key} and {rev} placeholders are allowed")


def _require_match(value: Mapping, key: str, pattern: re.Pattern, path: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not pattern.fullmatch(item):
        _raise_path(path, f"must match {pattern.pattern}")
    return item


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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
