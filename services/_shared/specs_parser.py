from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_STATUSES = [
    {
        "name": "backlog",
        "order": 1,
        "display_label": "Backlog",
        "color": "#8aa097",
        "is_terminal": False,
        "default_visible": True,
        "description": "Created; still needs requirements from the user.",
    },
    {
        "name": "analysis",
        "order": 2,
        "display_label": "Analysis",
        "color": "#a890ff",
        "is_terminal": False,
        "default_visible": True,
        "description": "Spec QA and pre-development investigation. After backlog, before ready_for_dev.",
    },
    {
        "name": "ready_for_dev",
        "order": 3,
        "display_label": "Ready for Dev",
        "color": "#a8b8ff",
        "is_terminal": False,
        "default_visible": True,
        "description": "Spec is QA'd and implementable; awaiting a dev to pick it up.",
    },
    {
        "name": "in_progress",
        "order": 4,
        "display_label": "In Progress",
        "color": "#56d364",
        "is_terminal": False,
        "default_visible": True,
        "description": "Being actively worked on. Code dev, code QA, and the documentation phase all live here.",
    },
    {
        "name": "needs_qa",
        "order": 5,
        "display_label": "Needs QA",
        "color": "#f5b78a",
        "is_terminal": False,
        "default_visible": True,
        "description": (
            "Implementation done; a genuine human-only gate (real device, real-money/external "
            "side effect, real-world event, or subjective visual judgment) is the sole remaining "
            "acceptance. Items fully covered by green automation close directly to completed "
            "instead of parking here for a redundant manual eyeball."
        ),
    },
    {
        "name": "blocked",
        "order": 6,
        "display_label": "Blocked",
        "color": "#d4a300",
        "is_terminal": False,
        "default_visible": True,
        "description": "Externally stuck; not progressing. Mutually exclusive with in_progress.",
    },
    {
        "name": "completed",
        "order": 7,
        "display_label": "Completed",
        "color": "#2dd4bf",
        "is_terminal": True,
        "default_visible": False,
        "description": "Shipped and accepted.",
    },
    {
        "name": "deprecated",
        "order": 8,
        "display_label": "Deprecated",
        "color": "#7a7a7a",
        "is_terminal": True,
        "default_visible": False,
        "description": (
            "Dropped before shipping (not pursued, superseded by another approach, no longer relevant)."
        ),
    },
]

SYNTHETIC_SPEC_PROGRESS = {"total": 0, "done": 0, "percent": 0}


def is_sync_conflict(path: str | Path) -> bool:
    return "sync-conflict" in str(path)


def parse_statuses_json(memory_root: str | Path):
    path = Path(memory_root) / "work" / "statuses.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        statuses = list(payload.get("statuses") or [])
    except Exception:
        statuses = list(DEFAULT_STATUSES)
    order = {str(item["name"]): int(item["order"]) for item in statuses}
    return statuses, order


def _frontmatter(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    result = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"')
    return result


def _heading_text(path: Path, heading: str) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(heading)}\s*\n+(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1).strip() if match else ""


def declared_spec_id(folder: str | Path) -> str:
    """Return the declared id for a work folder, falling back to its slug."""
    folder = Path(folder)
    spec_fm = _frontmatter(folder / "spec.md")
    return str(spec_fm.get("id") or folder.name)


def has_declared_spec_id(folder: str | Path) -> bool:
    folder = Path(folder)
    return bool(_frontmatter(folder / "spec.md").get("id"))


def parse_work_folder(folder: str | Path, lifecycle: str, statuses: list[dict]):
    folder = Path(folder)
    spec_fm = _frontmatter(folder / "spec.md")
    summary_fm = _frontmatter(folder / "summary.md")
    spec_id = folder.name
    declared_id = declared_spec_id(folder)
    known = {str(item["name"]): item for item in statuses}
    status = str(lifecycle)
    front_statuses = {
        value
        for value in (spec_fm.get("status"), summary_fm.get("status"))
        if value
    }
    frontmatter_drift = bool(front_statuses and front_statuses != {status})
    folder_terminal = bool(known.get(status, {}).get("is_terminal"))
    front_terminal = any(bool(known.get(value, {}).get("is_terminal")) for value in front_statuses)
    return {
        "spec_id": spec_id,
        "synthetic": False,
        "unresolved_reason": None,
        "id": declared_id,
        "title": spec_fm.get("title") or spec_id,
        "summary": _heading_text(folder / "spec.md", "Goal"),
        "next_action": _heading_text(folder / "summary.md", "Next action"),
        "repo": spec_id.split("__", 1)[0] if "__" in spec_id else spec_id,
        "topic": spec_id.split("__", 1)[1] if "__" in spec_id else "",
        "machine": spec_fm.get("machine"),
        "owner": spec_fm.get("owner"),
        "epic": spec_fm.get("epic") or summary_fm.get("epic"),
        "status": status,
        "lifecycle": status,
        "frontmatter_status": spec_fm.get("status"),
        "created_at": spec_fm.get("created_at"),
        "updated_at": spec_fm.get("updated_at"),
        "completed_at": spec_fm.get("completed_at"),
        "goal_excerpt": _heading_text(folder / "spec.md", "Goal"),
        "blockers": _heading_text(folder / "summary.md", "Blockers"),
        "status_unknown": status not in known,
        "frontmatter_drift": frontmatter_drift,
        "terminal_state_drift": frontmatter_drift and folder_terminal != front_terminal,
        "path": str(folder),
        "progress": dict(SYNTHETIC_SPEC_PROGRESS),
    }
