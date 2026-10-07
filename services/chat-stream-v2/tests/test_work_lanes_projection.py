"""Work-lanes projection: frozen fixture parity, counts vs sessions, server read surface (V8, V10, V11)."""
import asyncio
import json
import random
from pathlib import Path

import pytest

from assistant_composite import AssistantComposite
from server import Server
from sessions import Sessions, VerbError
from work_lanes_projection import WorkLanesInventory, build_frame, project_lanes

from test_work_lanes import FD, Env, run
from test_assistant_prose_mirror import ASSISTANT

FIXTURE = Path(__file__).resolve().parents[3] / "pentacle-chat-core" / "tests" / "fixtures" / "work-lanes-inventory.json"


def _stored_rows(fixture):
    """Invert the fixture's wire lanes into stored rows + presence (the projection's inputs)."""
    rows, presence = [], {}
    for lane in fixture["list_reply"]["lanes"]:
        unreconciled = lane["state_reason"] == "lead_lost_unreconciled"
        lead = lane["lead"]
        row = {
            "lane_id": lane["lane_id"], "stream_id": ASSISTANT, "title": lane["title"], "summary": lane["summary"],
            "work_state": "active" if unreconciled else lane["state"],
            "work_state_reason": "fd" if unreconciled else lane["state_reason"],
            "blocker": lane["blocker"], "owner_kind": lane["owner_kind"], "version": lane["version"],
            "updated_at": lane["updated_at"], "first_admitted_at": lane["first_admitted_at"],
            "done_at": lane["done_at"],
            "bound_stream_id": lead["stream_id"] if lead else None,
            "bound_generation": lead["generation"] if lead else None,
            "visible_chat_stream_id": lane["visible_chat"]["stream_id"],
            "visible_chat_generation": lane["visible_chat"]["generation"],
            "_qualifies": bool(lead and lead["qualifies"]),
            "_lead_row": ({"status": lead["status"], "visibility": lead["visibility"]} if lead else None),
            "_chat_kind": lane["visible_chat"]["kind"], "_chat_available": lane["visible_chat"]["available"],
            "_last_update": lane["last_update"],
        }
        rows.append(row)
        if lead and lead["presence"]["online"]:
            card = lead["status_card"]
            presence[lead["stream_id"]] = {
                **lead["presence"],
                "status_card": {"goal": card["goal"], "update": card["update"], "eta_at": card["eta_at"],
                                "eta_set_at": card["eta_set_at"], "updated_at": card["updated_at"],
                                "plan": ([{"text": card["active_step"], "status": "in_progress"}]
                                         if card["active_step"] else [])}}
    return rows, presence


def _offline_card(fixture):
    """Offline leads read their status card from the stored session row."""
    out = {}
    for lane in fixture["list_reply"]["lanes"]:
        lead = lane["lead"]
        if lead and not lead["presence"]["online"]:
            card = lead["status_card"]
            out[lane["lane_id"]] = {"goal": card["goal"], "eta_at": card["eta_at"], "eta_set_at": card["eta_set_at"],
                                    "updated_at": card["updated_at"], "update": card["update"], "plan": []}
    return out


def test_projection_matches_frozen_fixture_v10():
    fixture = json.loads(FIXTURE.read_text())
    rows, presence = _stored_rows(fixture)
    for lane_id, card in _offline_card(fixture).items():
        row = next(r for r in rows if r["lane_id"] == lane_id)
        row["_lead_row"]["status_card"] = card
    now = fixture["inventory_frame"]["generated_at"]
    for seed in range(5):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        frame = build_frame([r for r in shuffled if r["work_state"] != "done"], presence, now_iso=now)
        assert frame == fixture["inventory_frame"]
        assert frame == fixture["hello_field"]["work_lanes"]
    assert [l["lane_id"] for l in fixture["inventory_frame"]["lanes"]] == fixture["expected"]["order"]
    assert fixture["inventory_frame"]["counts"]["open"] == fixture["expected"]["header_count"]
    listed = project_lanes(rows, presence, now)
    assert [l["lane_id"] for l in listed] == [l["lane_id"] for l in fixture["list_reply"]["lanes"]]
    for lane in fixture["inventory_frame"]["lanes"]:
        assert lane["lead"]["eta_stale"] == fixture["expected"]["eta_stale"][lane["lane_id"]]
        tap = fixture["expected"]["tap"][lane["lane_id"]]
        available = lane["visible_chat"]["available"]
        assert tap["action"] == {"open": "open_chat", "history": "history", "unavailable": "unavailable"}[available]


def test_fixture_update_events_match_store_shape():
    fixture = json.loads(FIXTURE.read_text())
    kinds = {e["event"]["raw"]["lane_update"]["kind"] for e in fixture["lane_update_events"]}
    assert kinds == set(fixture["enums"]["update_kind"])

    async def body(env):
        lid = (await env.adopt(key="stream:shape"))["lane"]["lane_id"]
        out = await env.op("update", {"kind": "milestone", "source_id": "m", "summary": "S",
                                      "grouped_source_ids": ["a"]}, lane=lid, version=1)
        published = next(f["event"] for f in env.broadcasts if f.get("type") == "chat.event")
        sample = fixture["lane_update_events"][-1]["event"]
        assert set(published) - {"daemon_seq"} == set(sample) - {"daemon_seq"}
        assert set(published["raw"]) == set(sample["raw"])
        assert set(published["raw"]["lane_update"]) == set(sample["raw"]["lane_update"])
        assert published["message_id"] == "publication:" + out["update"]["update_id"]
    run(body)


def test_lane_count_is_not_session_count_v11():
    async def body(env):
        lead = await env.seat("count-lead", role="lead", parent_stream_id=FD)
        for i in range(3):
            await env.seat(f"worker-{i}", parent_stream_id=lead[0], visibility="hidden")
        await env.seat("unrelated-1")
        await env.seat("unrelated-2")
        await env.adopt(key="stream:count", lead=lead)
        sessions = Sessions(env.store, tmux=None, local_host="amaterasu")
        await sessions.refresh()
        frames = []

        async def broadcast(frame):
            frames.append(frame)
        inv = WorkLanesInventory(env.store, sessions, broadcast)
        frame = await inv.current()
        assert frame["counts"] == {"open": 1, "active": 1, "paused": 0, "blocked": 0}
        assert len([s for s in sessions.list_open() if s["stream_id"].startswith("amaterasu:")]) == 6
        assert await inv.emit_if_changed() is True
        assert await inv.emit_if_changed() is False  # signature dedupe
        assert frames[0]["type"] == "work_lanes.inventory"
    run(body)


def test_presented_paused_before_reconcile_and_server_reads_v4_v8():
    async def body(env):
        lead = await env.seat("hist-lead", role="lead", parent_stream_id=FD, pane_pid="5151")
        lid = (await env.adopt(key="stream:hist", lead=lead,
                               chat={"stream_id": lead[0], "generation": lead[1]}))["lane"]["lane_id"]
        await env.store.append_session_events_lifecycle_cas([
            {"stream_id": lead[0], "event": {"stream_id": lead[0], "provider": "claude", "kind": "ASSIST_TEXT",
                                             "text": "history line", "raw": {}},
             "identity": "h1",
             "lifecycle": await env.store.fetch_open_session_lifecycle(lead[0], pane_pid="5151")}], limit=100)
        await env.store.mark_closed("amaterasu", "hist-lead", closed_at="2026-10-07T20:00:00Z",
                                    pane_status="pane_dead", close_kind="operator_close")
        sessions = Sessions(env.store, tmux=None, local_host="amaterasu")
        await sessions.refresh()
        server = Server(store=env.store, sessions=sessions, comms=None, local_host="amaterasu")
        server.assistant_composite = env.composite
        server.work_lanes = WorkLanesInventory(env.store, sessions, server.broadcast)
        frame = await server.work_lanes.current()
        lane = frame["lanes"][0]
        assert (lane["state"], lane["state_reason"]) == ("paused", "lead_lost_unreconciled")
        assert lane["visible_chat"]["available"] == "history"
        operator = {"operator_authenticated": True, "operator_principal": "operator:fixture"}
        msg = {"stream_id": lead[0], "generation": lead[1], "_auth_context": operator}
        assert sessions.get(lead[0]) is None, sessions.get(lead[0])
        events = await server._on_request_stream_events(msg)
        assert events is not None
        # Scoped clients stay confined to their scope stream.
        with pytest.raises(VerbError):
            await server._on_request_stream_events({"stream_id": lead[0], "generation": lead[1],
                                                    "_auth_context": {"scoped_principal": True,
                                                                      "scope_stream": "amaterasu:other"}})
        # The lane exception itself admits only the exact operator pointer.
        assert await server._work_lane_history_readable(msg, lead[0], operator) is True
        for auth, generation in (({"token_verified": True, "stream_id": FD}, lead[1]),
                                 ({"operator_authenticated": True, "scoped_principal": True}, lead[1]),
                                 (operator, "other-generation")):
            probe = {"stream_id": lead[0], "generation": generation, "_auth_context": auth}
            assert await server._work_lane_history_readable(probe, lead[0], auth) is False
        listed = await server._on_work_lanes_list({"include_done": True, "_auth_context": operator})
        assert [l["lane_id"] for l in listed["lanes"]] == [lid]
        shown = await server._on_work_lanes_show({"lane_id": lid, "_auth_context": operator})
        assert shown["projection"]["lane_id"] == lid and shown["events"]
        with pytest.raises(VerbError):
            await server._on_work_lanes_list({"_auth_context": {"scoped_principal": True}})
        reply = await server._on_list_sessions({"capabilities": {"work_lanes_v1": True}, "_auth_context": operator})
        assert reply["work_lanes"]["counts"]["open"] == 1
        plain = await server._on_list_sessions({"_auth_context": operator})
        assert "work_lanes" not in plain
    run(body)


def test_unavailable_when_tail_missing_or_generation_changed_v8():
    async def body(env):
        chat = await env.seat("empty-chat")
        lid = (await env.adopt(key="stream:empty", state="paused", lead=False, owner="operator",
                               chat={"stream_id": chat[0], "generation": chat[1]}))["lane"]["lane_id"]
        await env.store.mark_closed("amaterasu", "empty-chat", closed_at="2026-10-07T20:00:00Z",
                                    pane_status="pane_dead")
        rows = await env.store.work_lane_rows()
        assert rows[0]["_chat_available"] == "unavailable"
        await env.store.open_session("amaterasu", "empty-chat", provider="claude")  # new generation
        rows = await env.store.work_lane_rows()
        assert rows[0]["lane_id"] == lid and rows[0]["_chat_available"] == "unavailable"
    run(body)
