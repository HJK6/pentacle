"""Small-item source classification and the store's one-seat claim predicate."""
from __future__ import annotations


def small_tag(text: str) -> bool:
    from _shared.specs_parser import load_frontmatter
    tags = load_frontmatter(text).get("tags")
    return isinstance(tags, list) and "small" in tags


def small_item_message(spec_id: str) -> str:
    return (f"{spec_id} is a small item: one seat, no review round. "
            "Remove its small tag and fill the full spec to add seats.")


def small_item_occupied_conn(conn, spec_id: str, *, own_reservation=None) -> bool:
    """Expired intent-bearing reservations still own their recoverable seat.

    Call under the store transaction when claiming; preflight is advisory.
    Session status is deliberately unrestricted, and only this request's
    reservation is excluded. A bare reservation has not claimed a work item.
    """
    if conn.execute("SELECT 1 FROM sessions WHERE assigned_spec_id=? LIMIT 1", (spec_id,)).fetchone():
        return True
    host, name, request = own_reservation or (None, None, None)
    return conn.execute(
        "SELECT 1 FROM v2_stream_reservations WHERE payload IS NOT NULL "
        "AND json_extract(payload,'$.open_fields.assigned_spec_id')=? "
        "AND NOT (host IS ? AND session_name IS ? AND request_id IS ?) LIMIT 1",
        (spec_id, host, name, request),
    ).fetchone() is not None
