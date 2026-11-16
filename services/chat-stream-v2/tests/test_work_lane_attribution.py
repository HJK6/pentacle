"""D1: member rows carry only the seats assigned to them and their open questions."""
from __future__ import annotations

from work_lanes_projection import attribute_members, frame_signature, project_lane

MEMBER = {"spec_id": "spec_demo__span", "status": "in_progress"}


def seat(role, *, spec_ids=("spec_demo__span",), **presence):
    return {"role": role, "host": "host-a", "visibility": "hidden", "parent_stream_id": "host-a:fd",
            "status": "open", "spec_id": spec_ids[0], "spec_ids": list(spec_ids), **presence}


def test_only_effective_assignments_attribute_and_portfolios_never_do():
    # Rows carry no persisted assignment: a single-spec seat's only spec is its
    # assignment, and a multi-spec portfolio row is unassigned (spec D1/A1).
    sessions = {
        "host-a:qa": seat("qa", online=True),
        "host-a:lead": seat("lead", online=True, busy=True, last_activity="t1"),
        "host-a:worker": seat("worker", online=False),
        "host-a:fd": seat("lead", spec_ids=("spec_demo__span", "spec_demo__other", "spec_demo__third")),
        "host-a:elsewhere": seat("lead", spec_ids=("spec_demo__other", "spec_demo__span")),
        "host-a:other": seat("worker", spec_ids=("spec_demo__other",), online=True),
        "host-a:closed": seat("worker", status="closed"),
    }
    (row,) = attribute_members([MEMBER], sessions, {"spec_demo__span": {"count": 2, "first_question_id": "q-1"}})
    assert [(s["stream_id"], s["role"], s["activity"]) for s in row["seats"]] == [
        ("host-a:lead", "lead", "working"), ("host-a:worker", "worker", "offline"), ("host-a:qa", "qa", "idle")]
    assert row["seats_total"] == 3
    assert row["open_questions"] == {"count": 2, "first_question_id": "q-1"}


def test_unknown_presence_and_no_questions():
    (row,) = attribute_members([MEMBER], {"host-a:x": seat("worker")}, None)
    assert row["seats"][0]["activity"] == "unknown"
    assert row["open_questions"] == {"count": 0, "first_question_id": None}


def test_dead_pane_seat_is_offline_even_if_last_seen_working():
    (row,) = attribute_members([MEMBER], {"host-a:x": seat("worker", working=True, pane_status="pane_dead")}, None)
    assert row["seats"][0]["activity"] == "offline"


def test_seat_cap_and_signature_ignores_last_activity():
    sessions = {f"host-a:w{i}": seat("worker", last_activity=str(i)) for i in range(10)}
    (row,) = attribute_members([MEMBER], sessions, None)
    assert len(row["seats"]) == 8 and row["seats_total"] == 10
    lane = {"lanes": [{"members": [row]}]}
    moved = {"lanes": [{"members": [{**row, "seats": [{**s, "last_activity": "later"} for s in row["seats"]]}]}]}
    assert frame_signature(lane) == frame_signature(moved)


def test_project_lane_adds_fields_only_with_session_index():
    lane = {"lane_id": "wl-1", "work_state": "paused", "work_state_reason": "fd", "version": 1,
            "_members": [{**MEMBER, "observation": {"quality": "fresh"}}], "_work_index_available": True}
    assert "seats" not in project_lane(lane, None, "2026-10-09T00:00:00Z")["members"][0]
    projected = project_lane(lane, None, "2026-10-09T00:00:00Z", sessions_by_stream={}, questions_by_spec={})
    assert projected["members"][0]["seats"] == [] and projected["members"][0]["seats_total"] == 0


def test_question_defaults_to_asker_assignment_and_groups_by_spec(tmp_path):
    import asyncio
    from test_question_contract_d3 import _FakeSessions, _ask, _notify

    lead, free = _ask("q-0")["from_stream_id"], "host-a:v2-free"  # the shared default asker, and a portfolio seat

    async def go():
        sessions = _FakeSessions({
            lead: {"visibility": "visible", "status": "open", "session_generation": "g1",
                              "spec_id": "spec_demo__span", "spec_ids": ["spec_demo__span"]},
            free: {"visibility": "visible", "status": "open", "session_generation": "g2",
                              "spec_ids": ["spec_demo__a", "spec_demo__b"]},
        })
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            assert (await notify.prompt(_ask("q-1")))["type"] == "prompt.ask.ok"
            named = _ask("q-2")
            named["envelope"]["spec_id"] = "spec_demo__other"  # an explicit spec always wins
            assert (await notify.prompt(named))["type"] == "prompt.ask.ok"
            assert (await notify.prompt(_ask("q-3", producer=free)))["type"] == "prompt.ask.ok"
            assert (await notify._db.call("get_agent_question", "q-1"))["spec_id"] == "spec_demo__span"
            assert (await notify._db.call("get_agent_question", "q-3"))["spec_id"] is None  # portfolio: no guess
            # A retry replays even when the default it would now derive differs (assignment
            # changed, or the stored question predates the default).
            sessions._rows[lead].update(spec_id="spec_demo__moved", spec_ids=["spec_demo__moved"])
            sessions._rows[free].update(spec_id="spec_demo__a", spec_ids=["spec_demo__a"])
            assert (await notify.prompt(_ask("q-1")))["type"] == "prompt.ask.ok"
            assert (await notify.prompt(_ask("q-3", producer=free)))["type"] == "prompt.ask.ok"
            assert (await notify._db.call("get_agent_question", "q-1"))["spec_id"] == "spec_demo__span"
            assert (await notify._db.call("get_agent_question", "q-3"))["spec_id"] is None
            # The asker became a portfolio (no assignment): an omitted spec_id still replays.
            sessions._rows[lead].update(spec_ids=["spec_demo__a", "spec_demo__b"])
            assert (await notify.prompt(_ask("q-1")))["type"] == "prompt.ask.ok"
            retry = _ask("q-1")
            retry["envelope"]["spec_id"] = "spec_demo__other"  # a different explicit spec still conflicts
            assert (await notify.prompt(retry))["type"] == "prompt.error"
            grouped = await notify.open_questions_by_spec()
            assert grouped == {"spec_demo__span": {"count": 1, "first_question_id": "q-1"},
                               "spec_demo__other": {"count": 1, "first_question_id": "q-2"}}
        finally:
            await notify.stop()
    asyncio.run(go())
