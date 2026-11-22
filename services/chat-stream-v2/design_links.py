"""Validate new Design link publications at the chat producer boundary."""
import re

from sessions import VerbError

_DESIGN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
_REVISION_ID = re.compile(r"[0-9a-f]{64}")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def validate_design_link(value):
    if (not isinstance(value, dict)
            or set(value) != {"dashboard", "target", "title", "subtitle", "revision_id"}
            or value["dashboard"] != "design"):
        raise VerbError("dashboard_link_invalid", "A Design link needs a target and recorded preview")
    target = value["target"]
    if (not isinstance(target, dict)
            or set(target) not in ({"project_id", "design_id"}, {"project_id", "design_id", "view_id"})
            or any(not isinstance(part, str) or not _DESIGN_ID.fullmatch(part) for part in target.values())):
        raise VerbError("dashboard_link_invalid", "Invalid Design project, board or screen ID")
    revision = value["revision_id"]
    if (not isinstance(revision, str) or not _REVISION_ID.fullmatch(revision)
            or any(not isinstance(value[key], str) or not value[key].strip() for key in ("title", "subtitle"))):
        raise VerbError("dashboard_link_invalid", "A Design link needs titles and a recorded revision")
    return {**value, "target": dict(target), "title": value["title"].strip(), "subtitle": value["subtitle"].strip()}


def require_design_png(attachments):
    if len(attachments) != 1 or attachments[0].get("mime") != "image/png":
        raise VerbError("dashboard_link_png_required", "A Design link requires one recorded PNG")
