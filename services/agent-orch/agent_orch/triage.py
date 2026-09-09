"""Deterministic triage state machine for generic work folders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import shutil
from pathlib import Path
from typing import Any


TRIAGE_SCHEMA_VERSION = 1
TRIAGE_TAG = "triage"
TRIAGE_STATE_FILE = "triage.json"
ACTIVE_STATUS_DIRS = ("backlog", "analysis", "ready_for_dev", "in_progress", "needs_qa", "blocked")
SOURCE_ACTION_STATUS_DIRS = ACTIVE_STATUS_DIRS + ("deprecated",)
MERGE_TARGET_STATUS_DIRS = ("backlog", "analysis", "ready_for_dev", "in_progress")
DEFAULT_DEFER_HOURS = 24


class TriageError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkItem:
    root: Path
    folder: Path
    spec_path: Path
    summary_path: Path
    frontmatter: dict[str, Any]

    @property
    def spec_id(self) -> str:
        return str(self.frontmatter.get("id") or "")

    @property
    def title(self) -> str:
        return str(self.frontmatter.get("title") or self.spec_id)

    @property
    def status(self) -> str:
        return self.folder.parent.name

    @property
    def repo(self) -> str:
        return self.folder.name.split("__", 1)[0]

    @property
    def tags(self) -> list[str]:
        tags = self.frontmatter.get("tags")
        return list(tags) if isinstance(tags, list) else []

    @property
    def triage_path(self) -> Path:
        return self.folder / TRIAGE_STATE_FILE


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TriageError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _frontmatter_bounds(text: str) -> tuple[int, int]:
    if not text.startswith("---\n"):
        raise TriageError("missing frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise TriageError("unterminated frontmatter")
    return 4, end


def parse_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    start, end = _frontmatter_bounds(text)
    lines = text[start:end].splitlines()
    data: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line or line.startswith("#"):
            index += 1
            continue
        if ":" not in line:
            index += 1
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value == "":
            values: list[str] = []
            lookahead = index + 1
            while lookahead < len(lines) and lines[lookahead].startswith("-"):
                values.append(_strip_yaml_scalar(lines[lookahead][1:].strip()))
                lookahead += 1
            data[key] = values
            index = lookahead
            continue
        data[key] = _strip_yaml_scalar(raw_value)
        index += 1
    return data


def _strip_yaml_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _replace_scalar_field(text: str, field: str, value: str) -> str:
    pattern = re.compile(rf"^({re.escape(field)}:\s*).*$", re.MULTILINE)
    replacement = rf"\g<1>{value}"
    next_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise TriageError(f"missing frontmatter field: {field}")
    return next_text


def _replace_tags(text: str, tags: list[str]) -> str:
    start, end = _frontmatter_bounds(text)
    fm_lines = text[start:end].splitlines()
    next_lines: list[str] = []
    index = 0
    replaced = False
    while index < len(fm_lines):
        line = fm_lines[index]
        if line.startswith("tags:"):
            next_lines.append("tags:")
            next_lines.extend(f"- {tag}" for tag in tags)
            replaced = True
            index += 1
            while index < len(fm_lines) and fm_lines[index].startswith("-"):
                index += 1
            continue
        next_lines.append(line)
        index += 1
    if not replaced:
        raise TriageError("missing tags field")
    return text[:start] + "\n".join(next_lines) + text[end:]


def _append_section(path: Path, heading: str, body: str) -> None:
    text = path.read_text(encoding="utf-8")
    if heading in text:
        text = text.rstrip() + "\n\n" + body.strip() + "\n"
    else:
        text = text.rstrip() + f"\n\n{heading}\n\n{body.strip()}\n"
    path.write_text(text, encoding="utf-8")


def update_document_status(path: Path, *, status: str, source_path: str) -> None:
    text = path.read_text(encoding="utf-8")
    text = _replace_scalar_field(text, "status", status)
    text = _replace_scalar_field(text, "source_path", source_path)
    path.write_text(text, encoding="utf-8")


def set_spec_tags(path: Path, tags: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(_replace_tags(text, tags), encoding="utf-8")


def iter_work_items(memory_root: Path, *, statuses: tuple[str, ...] = ACTIVE_STATUS_DIRS) -> list[WorkItem]:
    items: list[WorkItem] = []
    work_root = memory_root / "work"
    for status in statuses:
        status_root = work_root / status
        if not status_root.exists():
            continue
        for folder in sorted(path for path in status_root.iterdir() if path.is_dir()):
            spec_path = folder / "spec.md"
            summary_path = folder / "summary.md"
            if not spec_path.exists() or not summary_path.exists():
                continue
            frontmatter = parse_frontmatter(spec_path)
            items.append(
                WorkItem(
                    root=memory_root,
                    folder=folder,
                    spec_path=spec_path,
                    summary_path=summary_path,
                    frontmatter=frontmatter,
                )
            )
    return items


def discover_items(memory_root: Path, *, statuses: tuple[str, ...] = ("backlog",)) -> list[WorkItem]:
    items: list[WorkItem] = []
    for item in iter_work_items(memory_root, statuses=statuses):
        if TRIAGE_TAG in item.tags:
            items.append(item)
    return items


def _triage_generation(item: WorkItem) -> str:
    updated_at = str(item.frontmatter.get("updated_at") or "unknown")
    return f"{item.spec_id}:{updated_at}"


def ensure_state(item: WorkItem, *, now: str | None = None, write: bool = True) -> dict[str, Any]:
    state = load_json(item.triage_path)
    if state:
        return state
    generation = _triage_generation(item)
    state = {
        "schema_version": TRIAGE_SCHEMA_VERSION,
        "spec_id": item.spec_id,
        "source_path": str(item.spec_path.relative_to(item.root)),
        "triage_generation": generation,
        "status": "pending",
        "notification_dedup_key": f"triage:{item.spec_id}:{generation}",
        "last_notified_at": None,
        "decided_at": None,
        "decided_by": None,
        "defer_until": None,
        "target_spec_id": None,
        "reason": None,
        "created_at": now or utc_now(),
    }
    if write:
        write_json(item.triage_path, state)
    return state


def item_is_publishable(item: WorkItem, state: dict[str, Any], *, now: str | None = None) -> bool:
    status = state.get("status") or "pending"
    if status in {"pending", "needs_target"}:
        return True
    if status != "deferred":
        return False
    defer_until = state.get("defer_until")
    if not isinstance(defer_until, str) or not defer_until:
        return True
    try:
        current = _parse_utc(now or utc_now())
        deferred = _parse_utc(defer_until)
    except ValueError:
        return False
    return current >= deferred


def _title_tokens(title: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]{4,}", title.lower())}


def merge_candidates(item: WorkItem, *, limit: int = 3) -> list[WorkItem]:
    tokens = _title_tokens(item.title)
    candidates: list[tuple[int, WorkItem]] = []
    for other in iter_work_items(item.root, statuses=MERGE_TARGET_STATUS_DIRS):
        if other.spec_id == item.spec_id or other.repo != item.repo:
            continue
        score = len(tokens & _title_tokens(other.title)) + len(set(item.tags) & set(other.tags))
        if score:
            candidates.append((score, other))
    candidates.sort(key=lambda pair: (-pair[0], pair[1].title))
    return [candidate for _, candidate in candidates[:limit]]


def _action_value(item: WorkItem, action: str, *, target_spec_id: str | None = None) -> dict[str, str]:
    value = {"consumer": "triage", "spec_id": item.spec_id, "action": action}
    if target_spec_id:
        value["target_spec_id"] = target_spec_id
    return value


def notification_payload(
    item: WorkItem,
    state: dict[str, Any],
    *,
    candidates: list[WorkItem] | None = None,
    answer_to_stream_id: str | None = None,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = [
        {"kind": "yes_no", "action_id": "keep", "label": "Keep", "choice": True, "value": _action_value(item, "keep")},
        {"kind": "yes_no", "action_id": "defer", "label": "Ask later", "choice": True, "value": _action_value(item, "defer")},
        {
            "kind": "yes_no",
            "action_id": "deprecate",
            "label": "Deprecate",
            "choice": True,
            "value": _action_value(item, "deprecate"),
        },
    ]
    for candidate in candidates if candidates is not None else merge_candidates(item):
        actions.append(
            {
                "kind": "yes_no",
                "action_id": f"merge-{candidate.spec_id}",
                "label": f"Merge into {candidate.title[:48]}",
                "choice": True,
                "value": _action_value(item, "merge", target_spec_id=candidate.spec_id),
            }
        )
    payload = {
        "type": "notification.create",
        "producer": "triage",
        "severity": "info",
        "title": f"Triage follow-up: {item.title}",
        "body": f"{item.spec_id} needs operator disposition.",
        "dedup_key": state["notification_dedup_key"],
        "actions": actions,
    }
    if answer_to_stream_id:
        payload["answer_to_stream_id"] = answer_to_stream_id
    return payload


def _find_item_by_spec_id(memory_root: Path, spec_id: str) -> WorkItem:
    for item in discover_items(memory_root, statuses=ACTIVE_STATUS_DIRS):
        if item.spec_id == spec_id:
            return item
    raise TriageError(f"triage item not found: {spec_id}")


def _find_any_spec(memory_root: Path, spec_id: str, *, statuses: tuple[str, ...] = ACTIVE_STATUS_DIRS) -> WorkItem:
    for status in statuses:
        status_root = memory_root / "work" / status
        if not status_root.exists():
            continue
        for folder in sorted(path for path in status_root.iterdir() if path.is_dir()):
            spec_path = folder / "spec.md"
            summary_path = folder / "summary.md"
            if not spec_path.exists() or not summary_path.exists():
                continue
            frontmatter = parse_frontmatter(spec_path)
            if frontmatter.get("id") == spec_id:
                return WorkItem(memory_root, folder, spec_path, summary_path, frontmatter)
    raise TriageError(f"target spec not found or terminal: {spec_id}")


def _remove_triage_tag(item: WorkItem) -> None:
    tags = [tag for tag in item.tags if tag != TRIAGE_TAG]
    set_spec_tags(item.spec_path, tags)


def apply_action(
    memory_root: Path,
    *,
    spec_id: str,
    action_value: str,
    decided_by: str,
    reason: str | None = None,
    defer_until: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    item = _find_any_spec(memory_root, spec_id, statuses=SOURCE_ACTION_STATUS_DIRS)
    if item.status == "deprecated":
        state = load_json(item.triage_path)
        if state.get("status") in {"merged", "deprecated"}:
            return state
        raise TriageError(f"terminal triage item has no terminal state: {spec_id}")
    state = ensure_state(item, now=now)
    if state.get("status") in {"kept", "merged", "deprecated"}:
        return state
    timestamp = now or utc_now()
    if action_value == "keep":
        _remove_triage_tag(item)
        _append_section(item.spec_path, "## Triage", f"Kept by {decided_by} at {timestamp}.")
        state.update({"status": "kept", "decided_at": timestamp, "decided_by": decided_by, "reason": reason})
        write_json(item.triage_path, state)
        return state
    if action_value == "defer":
        effective_defer_until = defer_until or _format_utc(_parse_utc(timestamp) + timedelta(hours=DEFAULT_DEFER_HOURS))
        _parse_utc(effective_defer_until)
        state.update(
            {
                "status": "deferred",
                "defer_until": effective_defer_until,
                "reason": reason or "operator_deferred",
                "decided_at": timestamp,
                "decided_by": decided_by,
            }
        )
        write_json(item.triage_path, state)
        return state
    if action_value == "deprecate":
        return _deprecate_item(item, state, decided_by=decided_by, reason=reason or "operator_deprecated", now=timestamp)
    if action_value.startswith("merge:"):
        target_spec_id = action_value.split(":", 1)[1]
        return _merge_item(item, state, target_spec_id=target_spec_id, decided_by=decided_by, reason=reason, now=timestamp)
    raise TriageError(f"unknown triage action: {action_value}")


def _deprecate_item(
    item: WorkItem,
    state: dict[str, Any],
    *,
    decided_by: str,
    reason: str,
    now: str,
) -> dict[str, Any]:
    target_folder = item.root / "work" / "deprecated" / item.folder.name
    target_folder.parent.mkdir(parents=True, exist_ok=True)
    if not target_folder.exists():
        shutil.move(str(item.folder), str(target_folder))
    spec_path = target_folder / "spec.md"
    summary_path = target_folder / "summary.md"
    update_document_status(spec_path, status="deprecated", source_path=f"work/deprecated/{target_folder.name}/spec.md")
    update_document_status(summary_path, status="deprecated", source_path=f"work/deprecated/{target_folder.name}/summary.md")
    _append_section(spec_path, "## Triage", f"Deprecated by {decided_by} at {now}. Reason: {reason}.")
    state.update({"status": "deprecated", "decided_at": now, "decided_by": decided_by, "reason": reason})
    write_json(target_folder / TRIAGE_STATE_FILE, state)
    return state


def _merge_item(
    item: WorkItem,
    state: dict[str, Any],
    *,
    target_spec_id: str,
    decided_by: str,
    reason: str | None,
    now: str,
) -> dict[str, Any]:
    target = _find_any_spec(item.root, target_spec_id, statuses=MERGE_TARGET_STATUS_DIRS)
    if target.spec_id == item.spec_id:
        raise TriageError("cannot merge a triage item into itself")
    note = reason or f"Merged from {item.spec_id}"
    _append_section(target.spec_path, "## Triage Merges", f"{now}: {note}. Source: `{item.spec_id}`.")
    state.update(
        {
            "status": "merged",
            "decided_at": now,
            "decided_by": decided_by,
            "target_spec_id": target_spec_id,
            "reason": note,
        }
    )
    write_json(item.triage_path, state)
    _deprecate_item(
        item,
        state,
        decided_by=decided_by,
        reason=f"merged into {target_spec_id}",
        now=now,
    )
    state["status"] = "merged"
    state["target_spec_id"] = target_spec_id
    write_json(item.root / "work" / "deprecated" / item.folder.name / TRIAGE_STATE_FILE, state)
    return state
