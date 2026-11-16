from __future__ import annotations

import json
import re
import math
from datetime import date

import yaml
from pathlib import Path

from .summary_prose import field_line

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

WORK_SPEC_ID_RE = re.compile(r"^spec_[A-Za-z0-9_-]+$")

SYNTHETIC_SPEC_PROGRESS = {"total": 0, "done": 0, "percent": 0}


def is_sync_conflict(path: str | Path) -> bool:
    return "sync-conflict" in str(path)


def parse_statuses_json(memory_root: str | Path):
    path = Path(memory_root) / "work" / "statuses.json"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        text = None
    return parse_statuses_text(text)


def parse_statuses_text(text: str | None):
    """Keep fallback extraction separate from the existing strict conversion."""
    try:
        payload = json.loads(text)
        statuses = list(payload.get("statuses") or [])
    except Exception:
        statuses = list(DEFAULT_STATUSES)
    order = {str(item["name"]): int(item["order"]) for item in statuses}
    return statuses, order


def load_frontmatter(text: str) -> dict:
    """Shared YAML loader; BaseLoader keeps dates and ids as wire-safe strings."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}
    match = re.match(r"\A---[^\S\n]*\n(.*?)^---[^\S\n]*(?:\n|$)", text, re.M | re.S)
    if not match:
        raise ValueError("work_frontmatter_unterminated")
    result = yaml.load(match.group(1), Loader=yaml.BaseLoader)
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ValueError("work_frontmatter_not_mapping")
    return result


def _frontmatter(path: Path) -> dict:
    return load_frontmatter(path.read_text(encoding="utf-8")) if path.exists() else {}


def _without_fences(text: str) -> str:
    lines, fence, width = [], None, 0
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence and len(marker[1]) >= width and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence, width = marker[1][0], len(marker[1])
        else:
            lines.append(line)
    return "\n".join(lines)


def _section(text: str, heading: str) -> str | None:
    match = re.search(rf"^## {re.escape(heading)}[ \t]*\n(.*?)(?=^#(?:#)? |\Z)",
                      text + "\n", re.M | re.S)
    return match[1] if match else None


def _criteria(ac: str | None, parked: str | None) -> list[dict]:
    custom = re.search(r"^### Custom[ \t]*\n(.*?)(?=^### |\Z)", ac or "", re.M | re.S)
    result = []
    for mark, body in re.findall(r"^\s*[-*+] \[([ xX])\][ \t]*(.*)$", custom[1] if custom else "", re.M):
        # Quoted/code examples carry no disposition. Mask them without moving
        # offsets so the original title (including its code spans) is preserved.
        declared = re.sub(r"(`+).*?\1|(?<!\w)([\"']).*?\2(?!\w)",
                          lambda match: " " * len(match[0]) if "waived:" in match[0].lower()
                          or "tracked:" in match[0] else match[0], body)
        waiver = re.search(r"\(waived:\s*[^)\s][^)]*\)\.?\s*$", declared, re.I)
        tracked = re.search(r"\btracked:\s*spec_[A-Za-z0-9_-]+\s*\((?:external_event|operator_action|new_scope)\)\.?\s*$", declared)
        disposition = ("done" if mark.lower() == "x" else "waived" if waiver else
                       "tracked" if tracked else "parked" if parked else "open")
        suffix = waiver or tracked
        text = body[:suffix.start()].rstrip() if suffix else body
        bold = re.match(r"\*\*(.+?)\*\*", text)
        title = " ".join((bold[1] if bold else text).split())
        result.append({"title": title if len(title) <= 120 else title[:119] + "…", "disposition": disposition})
        if len(result) == 40:
            break
    return result


def _transitions(tracking: str, created_at: str | None, status: str) -> list[dict]:
    moves = []
    for at, source in re.findall(r"^- summary transition: ([0-9]{4}-[0-9]{2}-[0-9]{2}); source lifecycle: ([a-z_]+);", tracking, re.M):
        try:
            date.fromisoformat(at)
        except ValueError:
            continue
        moves.append((at, source))
    created = str(created_at or "")[:10]
    try:
        date.fromisoformat(created)
    except ValueError:
        return []
    if not moves:
        return [{"status": status, "at": created}]
    entries = [{"status": moves[0][1], "at": created}]
    entries.extend({"status": source, "at": moves[i - 1][0]} for i, (_, source) in enumerate(moves) if i)
    entries.append({"status": status, "at": moves[-1][0]})
    return entries[-8:]


def _estimate(declaration: str) -> dict | None:
    number = r"(?:[0-9]+(?:\.[0-9]+)?)"
    match = re.fullmatch(rf"({number})[ \t]*[–—-][ \t]*({number})"
                         rf"[ \t]*\(median[ \t]+({number})\)[ \t]+as_of[ \t]+"
                         r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:[ \t]+(provisional))?", declaration)
    if not match:
        return None
    p25, p75, median = map(float, match.groups()[:3])
    try:
        date.fromisoformat(match[4])
    except ValueError:
        return None
    if all(math.isfinite(n) for n in (p25, p75, median)) and 0 < p25 <= median <= p75:
        return {"p25": p25, "p75": p75, "median": median, "as_of": match[4], "provisional": match[5] is not None}
    return None


def _blocked_by(value) -> list[str] | None:
    """Private source fact for derived ``unblocks``; never sent on the wire."""
    if (not isinstance(value, list) or not 1 <= len(value) <= 4
            or not all(isinstance(item, str) and WORK_SPEC_ID_RE.fullmatch(item) for item in value)):
        return None
    return list(value)


def parse_work_facts(spec_text: str, summary_text: str, status: str) -> dict:
    """Parse file-owned lane facts without inferring completion or estimates."""
    fm = load_frontmatter(spec_text)
    summary_fm = load_frontmatter(summary_text)  # Both documents must parse coherently.
    spec = _without_fences(spec_text)
    summary = _without_fences(summary_text)
    ac = _section(spec, "Acceptance Criteria")
    boxes = re.findall(r"^\s*[-*+] \[([ xX])\][ \t]*(.*)$", ac or "", re.M)
    checked = sum(mark.lower() == "x" or bool(re.search(r"\(waived:\s*[^)]+\)", body, re.I))
                  for mark, body in boxes)
    estimate_text = _section(spec, "Estimate") or ""
    # Select the physical declaration BEFORE validation. Invalid latest values
    # suppress history; elapsed_delivery_h remains an immutable delivery baseline.
    declarations = [value.strip() for value in
                    re.findall(r"^[ \t]*(?:- )?remaining_work_h:[ \t]*(.*)$", estimate_text, re.M)]
    selected = declarations[-1] if declarations else ""
    estimate_exempt = bool(re.fullmatch(r"n/a[ \t]+—[ \t]+(?:umbrella|coordination)", selected))
    estimate = _estimate(selected)
    first = next((value for value in map(_estimate, declarations) if value is not None), None)
    estimate_first = {key: first[key] for key in ("p25", "p75", "median")} if first else None

    def label(name):
        found = re.search(rf"^\*\*{re.escape(name)}\*\*[ \t]*[—–-][ \t]*(.+)$", summary, re.M)
        return found[1].strip()[:280] if found else None

    parked = None
    tracking = _section(spec, "Tracking") or ""
    declarations = re.findall(r"^[ \t]*- parked:[ \t]*(.*)$", tracking, re.M)
    if status == "blocked" and declarations:
        declaration = declarations[-1]
        match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2}) (\S+) — (\S.*)", declaration)
        if match:
            try:
                date.fromisoformat(match[1])
            except ValueError:
                pass
            else:
                parked = f"{match[1]} {match[2]} — {' '.join(match[3].split())}"
    status_text, next_text = label("Status"), label("Next action")
    from .checkpoint_policy import parse_checkpoint, bounded_prose
    checkpoint = parse_checkpoint(summary, status)
    goal = bounded_prose(_section(spec, "Goal"))
    organization = {}
    if "epic" in fm or "epic" in summary_fm:
        epic = fm.get("epic")
        organization["_epic"] = epic if isinstance(epic, str) else None
        if (not isinstance(epic, str) or not re.fullmatch(r"epic_[a-z0-9_]+", epic)
                or summary_fm.get("epic") != epic):
            organization["_epic_error"] = "work_goal_pair_invalid"
    return {"title": fm.get("title"), "status": status, **organization, **checkpoint,
            "goal_text": goal["text"], "goal_text_truncated": goal["truncated"],
            "criteria": _criteria(ac, parked), "transitions": _transitions(tracking, fm.get("created_at"), status),
            "status_line": field_line("status", checkpoint.get("status_text")),
            "next_line": field_line("next_action", checkpoint.get("next_action_text")),
            "blocked_line": field_line("blockers", checkpoint.get("blockers_text")),
            "summary_line": field_line("spec_summary", fm.get("summary") if isinstance(fm.get("summary"), str) else None),
            "_blocked_by": _blocked_by(summary_fm.get("blocked_by")),
            **({"parked_declaration": parked} if parked else {}),
            "terminal": status if status in ("completed", "deprecated") else None,
            "ac_checked": checked if ac is not None else None,
            "ac_total": len(boxes) if ac is not None else None,
            "estimate": estimate, "estimate_first": estimate_first, "estimate_exempt": estimate_exempt,
            "status_text": status_text, "next_action_text": next_text}


def _heading_text(path: Path, heading: str) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(heading)}\s*\n+(.*?)(?=^#(?:#)? |\Z)", text, re.M | re.S)
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
        **{key: value for key, value in parse_work_facts(
            (folder / "spec.md").read_text(encoding="utf-8") if (folder / "spec.md").exists() else "",
            (folder / "summary.md").read_text(encoding="utf-8") if (folder / "summary.md").exists() else "",
            status).items() if key not in {"title", "summary_line", "estimate_first", "_blocked_by"}},
    }
