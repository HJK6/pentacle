"""work_lanes.show fits one frame however long a lane's event history grows.

A busy lane gathered thousands of item_change events; the show reply carried
all of them, crossed the 4 MB ceiling and refused every page, even members_limit 1.
"""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from server import Server, VerbError
from store import Store
from work_lane_link import FIXTURE_LANE_ID, link_specs_conn
from work_lanes_projection import _iso_now, page_lane_detail, project_lane

EVENTS = 1200
PAYLOAD = json.dumps({"spec_id": "spec_fixture", "next": {"status_line": "x" * 4000}})


def _long_history(conn):
    link_specs_conn(conn, "spec_fixture")
    conn.executemany(
        "INSERT INTO v2_work_lane_events(event_id,lane_id,stream_id,operation,source_id,payload_json,payload_digest,"
        "created_at) VALUES (?,?,?,?,?,?,?,?)",
        [(f"item:{i:05}", FIXTURE_LANE_ID, "fixture:assistant", "item_change", f"src-{i}", PAYLOAD, "d",
          f"2026-11-26T{i // 3600:02}:{i // 60 % 60:02}:{i % 60:02}.000Z") for i in range(EVENTS)])
    conn.commit()


async def _shown():
    store = Store(":memory:"); store.start()
    try:
        await store.submit(_long_history)
        return await store.get_work_lane(FIXTURE_LANE_ID)
    finally:
        store.stop()


@pytest.mark.timeout(60)
def test_show_reply_carries_the_newest_events_and_fits_the_frame():
    shown = asyncio.run(_shown())
    assert len(shown["events"]) == EVENTS, "the store read keeps the whole history"
    projected = project_lane(shown["_projection_row"], None, _iso_now(), all_members=True, sessions_by_stream={})
    reply = page_lane_detail(shown, projected, members_limit=1)
    assert len(json.dumps(reply).encode()) <= 4 * 1024 * 1024
    assert reply["events"] == shown["events"][-100:]
    assert reply["events_total"] == len(shown["events"])
    assert reply["events"][-1]["event_id"] == f"item:{EVENTS - 1:05}", "newest last, as before"


@pytest.mark.timeout(30)
def test_a_refused_show_is_logged(caplog):
    async def get_work_lane(lane_id, **_):
        return {"_projection_row": {"bound_stream_id": None}}
    server = SimpleNamespace(_work_lanes_reader=lambda msg: None, store=SimpleNamespace(get_work_lane=get_work_lane),
                             sessions=SimpleNamespace(list_open=lambda: []), work_lanes=None)
    import work_lanes_projection
    with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.server"):
        project = work_lanes_projection.project_lane
        work_lanes_projection.project_lane = lambda *a, **k: {"members": []}
        try:
            with pytest.raises(VerbError):
                asyncio.run(Server._on_work_lanes_show(server, {"lane_id": "wl-fixture", "members_limit": 0}))
        finally:
            work_lanes_projection.project_lane = project
    assert "work_lanes.show refused lane=wl-fixture error=work_lane_members_limit_invalid" in caplog.text
