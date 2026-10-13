"""Spec membership at the first durable spawn-intent transaction."""
from __future__ import annotations

import shlex

from store_specs import normalize_spec_ids
from store_work_lanes import OPEN_WORK_STATES


class WorkLaneRequired(ValueError):
    code = "work_lane_required"

    def __init__(self, spec_ids: list[str]):
        self.spec_ids = spec_ids
        additions = " ".join("--member " + shlex.quote(identity) for identity in spec_ids)
        super().__init__(
            "No non-terminal work lane contains commissioned spec(s): "
            + ", ".join(spec_ids)
            + ". Ask the lane owner to select the product lane with "
            "`agent-orch work-lane list --json`, read ALL members with "
            "`agent-orch work-lane show LANE_ID --json`, then run "
            "`agent-orch work-lane set-members LANE_ID --expected-version VERSION "
            "--request-id REQUEST_ID --member EXISTING_MEMBER ... " + additions
            + "`. set-members replaces the list: preserve every existing member "
            "from lane.members_json. Retry spawn with a fresh --idempotency-key."
        )


def require_work_lane_membership(conn, fields: dict) -> None:
    """Use final resolved fields and the caller's existing writer transaction.

    Roles, visibility and recovery labels confer no membership bypass. The
    empty effective spec set covers conversation/bootstrap/rehearsal starts.
    """
    identities = normalize_spec_ids(fields.get("spec_ids"), fields.get("spec_id"))
    identities = normalize_spec_ids(identities + normalize_spec_ids(fields.get("qualified_spec_ids")))
    if not identities:
        return
    placeholders = ",".join("?" for _ in OPEN_WORK_STATES)
    present = {row[0] for row in conn.execute(
        "SELECT member.value FROM v2_assistant_composite_lanes AS lane, "
        "json_each(lane.members_json) AS member "
        f"WHERE lane.work_state IN ({placeholders})", OPEN_WORK_STATES)}
    missing = [identity for identity in identities if identity not in present]
    if missing:
        raise WorkLaneRequired(missing)
