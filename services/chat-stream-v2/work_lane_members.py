"""Validation shared by FD lane adoption and membership replacement."""
import re
import sys
from pathlib import Path

# This module is imported before the daemon's other shared-module consumers.
SERVICES_ROOT = str(Path(__file__).resolve().parents[1])
if SERVICES_ROOT not in sys.path:
    sys.path.insert(0, SERVICES_ROOT)

from _shared.specs_parser import WORK_SPEC_ID_RE as SPEC_ID_RE
TITLE_ID_RE = re.compile(r"(^|\W)(v2-[0-9a-f]{8}|spec_[a-z0-9_]+|wl-[0-9a-f]{24}|assistant-lane-[0-9a-f]+)")


def validate_title(title: str) -> str:
    if TITLE_ID_RE.search(title):
        raise ValueError("work_lane_title_invalid")
    return title


def validate_members(members, reason) -> tuple[list[str], str | None]:
    if (not isinstance(members, list) or len(members) > 32
            or any(not isinstance(s, str) or not SPEC_ID_RE.fullmatch(s) for s in members)
            or len(set(members)) != len(members)):
        raise ValueError("work_lane_members_invalid")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 280):
        raise ValueError("work_lane_no_spec_reason_invalid")
    reason = reason.strip() or None if reason is not None else None
    if not members and not reason:
        raise ValueError("work_lane_no_spec_reason_required")
    return list(members), reason
