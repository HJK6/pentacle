"""Test fixture: put specs in an open FD product lane.

Item (1) of the lane tracking minimal spec admits a
token-verified new commission only when its assigned spec is a member of a
non-done product lane. Fixtures that commission real work link their spec here
instead of bypassing the gate.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

FIXTURE_LANE_ID = "wl-fixture-link"


def link_specs_conn(conn: sqlite3.Connection, *spec_ids: str, lane_id: str = FIXTURE_LANE_ID) -> None:
    """Insert (or extend) an active FD lane whose members include ``spec_ids``."""
    row = conn.execute("SELECT members_json FROM v2_assistant_composite_lanes WHERE lane_id=?",
                       (lane_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO v2_assistant_composite_lanes (lane_id,stream_id,phase,summary,version,created_at,"
            "updated_at,work_state,work_state_reason,owner_kind,title,members_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (lane_id, "fixture:assistant", "execution", "", 1, "2026-10-09T00:00:00Z", "2026-10-09T00:00:00Z",
             "active", "fd", "fd", "Fixture lane", json.dumps(list(spec_ids))))
    else:
        members = json.loads(row[0] or "[]")
        members += [spec for spec in spec_ids if spec not in members]
        conn.execute("UPDATE v2_assistant_composite_lanes SET members_json=? WHERE lane_id=?",
                     (json.dumps(members), lane_id))
    conn.commit()


async def link_specs(store: Any, *spec_ids: str, lane_id: str = FIXTURE_LANE_ID) -> None:
    await store.submit(lambda conn: link_specs_conn(conn, *spec_ids, lane_id=lane_id))
