"""First-class work lanes: store operations, CAS, idempotency, lifecycle, owner kind (V1-V9, V12, V13)."""
import asyncio
import json
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

from assistant_composite import AssistantComposite
from store import Store
from _shared.work_goals import derive_lane_id
from test_assistant_prose_mirror import ASSISTANT, ROOT, _config

FD = ROOT


async def seed_pending_goal(store, epic_id, lane_id, *, title="Synthetic goal", rank=1000):
    """Record a valid, unique charter naming a not-yet-known lane, so adopt can accept it.

    This is the observable state the work-index scan leaves for a charter that
    names a lane before that lane exists; adopt reads its ``pending`` facts."""
    def write(conn):
        from store_work_index import _state_conn, ensure_work_index_schema
        ensure_work_index_schema(conn)
        state = _state_conn(conn)
        # A seeded charter means the scan ran and the index is available; without
        # this the lane reads as uncertain and is never completed or reopened.
        state.setdefault("available", True)
        state.setdefault("root_configured", True)
        state.setdefault("snapshot_at", "2026-01-01T00:00:00.000Z")
        state["available"] = True
        observations = dict(state.get("goal_observations", {}))
        observations[epic_id] = {
            "quality": "error", "error": "work_goal_lane_unknown",
            "pending": {"facts": {"epic_id": epic_id, "title": title, "lane_id": lane_id, "rank": rank,
                                  "_detail": {"summary_text": "Synthetic goal.", "summary_text_truncated": False,
                                              "summary_line": None, "goal_text": "Synthetic.",
                                              "goal_text_truncated": False}},
                        "source_hash": "synthetic:" + epic_id, "path": f"epics/{epic_id}.md"}}
        state["goal_observations"] = observations
        conn.execute("INSERT INTO v2_work_index_state(singleton,state_json) VALUES (1,?) "
                     "ON CONFLICT(singleton) DO UPDATE SET state_json=excluded.state_json",
                     (json.dumps(state, separators=(",", ":")),))
        conn.commit()
    await store.submit(write)


async def seed_lane_work(store, lane_id, status="completed"):
    """Give synthetic goals and explicit members fresh indexed work at a chosen stage."""
    from test_work_lane_episodes import member
    shown = await store.get_work_lane(lane_id)
    goals = shown["_projection_row"]["_goals"]
    records = {spec: {"member": {**member(status), "spec_id": spec}} for spec in shown["lane"]["members"]}
    for epic in goals:
        spec = "spec_fixture__" + epic
        records[spec] = {"member": {**member(status), "spec_id": spec}, "last_good": {"_epic": epic, "status": status}}

    def write(conn):
        conn.executemany("INSERT OR REPLACE INTO v2_work_item_observations VALUES (?,?)",
                         [(spec, json.dumps(record)) for spec, record in records.items()])
        conn.commit()
    await store.submit(write)


async def seed_legacy_done(env, lane):
    """Restore a lane state produced by a manual close before the content check."""
    def write(conn):
        conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='done',work_state_reason='fd',"
                     "done_at=updated_at,version=version+1 WHERE lane_id=?", (lane["lane_id"],))
        conn.commit()
    await env.store.submit(write)
    return (await env.store.get_work_lane(lane["lane_id"]))["lane"]


def synthetic_epic(key):
    return "epic_fixture_" + re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


class Env:
    def __init__(self, store, composite, generation):
        self.store, self.composite, self.gen = store, composite, generation
        self.seq = 0
        self.broadcasts = []
        self.confirmations = {}
        composite.broadcast = self._broadcast
        composite.work_lane_confirmation_reader = self._confirmation

    async def _broadcast(self, frame):
        self.broadcasts.append(frame)

    async def _confirmation(self, question_id):
        return self.confirmations.get(question_id)

    def confirm(self, question_id, lane_id, action, *, answer="Confirm", actor_class="direct_operator",
                producer=FD):
        self.confirmations[question_id] = {
            "question_id": question_id, "producer_stream_id": producer,
            "work_lane_confirmation": {"lane_id": lane_id, "action": action},
            "answer": answer, "actor_class": actor_class}
        return {"question_id": question_id}

    async def op(self, operation, payload, *, lane=None, version=None, request_id=None, actor=FD, gen=None,
                 inject_goals=True):
        # Every lane now needs an observed goal that names it. Seed one synthetic
        # empty goal for any adopt that does not set `goals`, so fixtures that
        # exercise other adoption rules still create their lane. A test of the
        # goal requirement itself passes inject_goals=False.
        if (operation == "adopt" and inject_goals and isinstance(payload, dict)
                and "goals" not in payload and payload.get("adoption_key")):
            key = payload["adoption_key"]
            target = payload.get("lane_id") or derive_lane_id(ASSISTANT, key)
            epic = synthetic_epic(key)
            await seed_pending_goal(self.store, epic, target)
            payload = {**payload, "goals": [epic]}
        self.seq += 1
        msg = {"type": "assistant.operation", "request_id": request_id or f"req-{self.seq}",
               "composite_stream_id": ASSISTANT, "dispatch_id": "none", "operation": "work_lane." + operation,
               "payload": payload, "_auth_context": {"token_verified": True, "stream_id": actor,
                                                     "session_generation": gen or self.gen}}
        if lane is not None:
            msg["lane_id"] = lane
            msg["expected_lane_version"] = version
        return await self.composite.operation(msg, actor_stream_id=actor)

    async def seat(self, name, **fields):
        fields.setdefault("provider", "claude")
        row = await self.store.open_session("host-a", name, **fields)
        return f"host-a:{name}", row["session_generation"]

    async def adopt(self, key="stream:x", state="active", owner="fd", lead=None, chat=None, goals=None, **extra):
        if lead is None and state == "active":
            lead = await self.seat("lead-" + key.replace(":", "-"), role="lead", parent_stream_id=FD)
        if goals is None:
            # A lane cannot be created without an observed goal that names it.
            # Seed one synthetic empty goal so existing fixtures still adopt; an
            # empty goal keeps the lane open (it is never auto-completed).
            target = extra.get("lane_id") or derive_lane_id(ASSISTANT, key)
            epic = synthetic_epic(key)
            await seed_pending_goal(self.store, epic, target)
            goals = [epic]
        payload = {"adoption_key": key, "title": "Lane " + key, "summary": "Goal line", "owner_kind": owner,
                   "work_state": state, "visible_chat": chat or {"stream_id": ASSISTANT}, "goals": goals,
                   "no_spec_reason": None if extra.get("members") else "Synthetic test lane.", **extra}
        if lead:
            payload["lead"] = {"stream_id": lead[0], "generation": lead[1]}
        return await self.op("adopt", payload, request_id="adopt:" + key)


def run(body, path=":memory:"):
    async def go():
        store = Store(path)
        store.start()
        root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
        composite = AssistantComposite(store, config=_config(root["session_generation"]))
        await composite.ensure_projection()
        env = Env(store, composite, root["session_generation"])
        try:
            await body(env)
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(go())


def code(exc_info):
    return str(exc_info.value)


async def lead_lost_episodes(store, lane_id):
    """Drain the lane episodes once, then return this lane's lead_lost openings."""
    await store.reconcile_work_lane_episodes(None)

    def q(conn):
        return [dict(r) for r in conn.execute(
            "SELECT * FROM v2_work_lane_episodes WHERE lane_id=? AND kind='lead_lost' ORDER BY sequence",
            (lane_id,))]
    return await store.submit(q)


async def lane_updates(store):
    def q(conn):
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT event_json FROM session_event_tail WHERE stream_id=? ORDER BY event_id", (ASSISTANT,))
            if json.loads(r[0]).get("publish_kind") == "lane_update"]
    return await store.submit(q)


def test_actor_must_be_direct_primary_binding():
    async def body(env):
        other = await env.seat("intruder", role="lead")
        with pytest.raises(ValueError) as e:
            await env.op("adopt", {"adoption_key": "stream:a", "title": "A", "owner_kind": "fd",
                                   "work_state": "paused", "visible_chat": {"stream_id": ASSISTANT}},
                         actor=other[0], gen=other[1])
        assert code(e) == "work_lane_actor_unverified"
    run(body)


def test_adopt_set_state_cas_and_replay_v9():
    async def body(env):
        out = await env.adopt()
        lane = out["lane"]
        assert lane["work_state"] == "active" and lane["version"] == 1 and out["update"] is None  # D-4
        await env.op("set_state", {"to": "paused", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lane["lane_id"], version=1, request_id="p1")
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "blocked", "blocker": "x", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lane["lane_id"], version=1)
        assert code(e) == "assistant_lane_version_conflict"
        again = await env.op("set_state", {"to": "paused", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lane["lane_id"], version=1, request_id="p1")
        assert again["duplicate"] is True
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "done", "outcome": "x"}, lane=lane["lane_id"], version=1,
                         request_id="p1")
        assert code(e) == "work_lane_idempotency_conflict"
        shown = await env.store.get_work_lane(lane["lane_id"])
        assert [e["operation"] for e in shown["events"]] == ["adopt", "set_state"]
    run(body)


def test_lifecycle_invariants_v3_v6_v7():
    async def body(env):
        lane = (await env.adopt())["lane"]
        lid = lane["lane_id"]
        r = await env.op("set_state", {"to": "paused", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lid, version=1)
        assert r["update"] is None  # pause emits nothing
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "paused", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lid, version=2)
        assert code(e) == "work_lane_state_unchanged"
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "blocked", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lid, version=2)
        assert code(e) == "work_lane_blocker_required"
        r = await env.op("set_state", {"to": "blocked", "blocker": "Need keys", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lid, version=2)
        assert r["lane"]["blocker"] == "Need keys" and r["update"]["update_id"].startswith("lane-update:" + lid)
        r = await env.op("set_state", {"to": "active", "resolution": "Keys arrived"}, lane=lid, version=3)
        assert r["lane"]["blocker"] is None and r["event"]["payload"]["cleared_blocker"] == "Need keys"
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "done"}, lane=lid, version=4)
        assert code(e) == "work_lane_outcome_required"
        await seed_lane_work(env.store, lid)
        r = await env.op("set_state", {"to": "done", "outcome": "Shipped"}, lane=lid, version=4)
        assert r["lane"]["done_at"]
        assert [l["lane_id"] for l in await env.store.work_lane_rows()] == []
        assert [l["lane_id"] for l in await env.store.work_lane_rows(include_done=True)] == [lid]
        await seed_lane_work(env.store, lid, "in_progress")
        r = await env.op("set_state", {"to": "paused", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lid, version=5)
        assert r["lane"]["work_state_reason"] == "reopened" and r["update"] is None
        kinds = [u["raw"]["lane_update"]["kind"] for u in await lane_updates(env.store)]
        assert kinds == ["lane_blocked", "lane_unblocked", "lane_completed"]
        texts = [u["text"] for u in await lane_updates(env.store)]
        assert texts == ["Need keys", "Keys arrived", "Shipped"]
    run(body)


def test_lead_eligibility_v5():
    async def body(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator"))["lane"]
        lid = lane["lane_id"]
        hidden = await env.seat("hidden-worker", parent_stream_id=FD, visibility="hidden")
        qa = await env.seat("qa-seat", role="qa")
        child = await env.seat("plain-child", parent_stream_id=FD)
        for seat in (hidden, qa, child):
            with pytest.raises(ValueError) as e:
                await env.op("set_lead", {"lead": {"stream_id": seat[0], "generation": seat[1]}},
                             lane=lid, version=1)
            assert code(e) == "work_lane_lead_ineligible"
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "active", "reason": "Resume managed work"}, lane=lid, version=1)
        assert code(e) == "work_lane_visible_lead_required"
        lead = await env.seat("real-lead", role="lead", parent_stream_id=FD)
        r = await env.op("set_lead", {"lead": {"stream_id": lead[0], "generation": lead[1]}}, lane=lid, version=1)
        assert r["lane"]["work_state"] == "paused"  # binding never resumes
        r = await env.op("set_state", {"to": "active", "reason": "Resume managed work"}, lane=lid, version=2)
        assert r["lane"]["work_state"] == "active"
    run(body)


def test_protected_assistant_seat_is_ineligible(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def body(env):
        lid = (await env.adopt(state="paused", lead=False, owner="operator"))["lane"]["lane_id"]
        assistant = await env.seat("assistant-seat", role="assistant")
        with pytest.raises(ValueError) as e:
            await env.op("set_lead", {"lead": {"stream_id": assistant[0], "generation": assistant[1]}}, lane=lid, version=1)
        assert code(e) == "work_lane_lead_ineligible"
        with pytest.raises(ValueError) as e:
            await env.op("set_chat", {"visible_chat": {"stream_id": assistant[0], "generation": assistant[1]}},
                         lane=lid, version=1)
        assert code(e) == "work_lane_visible_chat_invalid"
    run(body)


def test_visible_chat_pointer_validation_v8():
    async def body(env):
        lid = (await env.adopt(state="paused", lead=False, owner="operator"))["lane"]["lane_id"]
        hidden = await env.seat("hidden-chat", parent_stream_id=FD, visibility="hidden")
        other_composite = await env.seat("other:assistant", provider="composite")
        for target in ({"stream_id": hidden[0], "generation": hidden[1]},
                       {"stream_id": other_composite[0], "generation": other_composite[1]},
                       {"stream_id": FD, "generation": env.gen},
                       {"stream_id": "host-a:missing", "generation": "g"}):
            with pytest.raises(ValueError) as e:
                await env.op("set_chat", {"visible_chat": target}, lane=lid, version=1)
            assert code(e) == "work_lane_visible_chat_invalid"
        chat = await env.seat("op-chat")
        r = await env.op("set_chat", {"visible_chat": {"stream_id": chat[0], "generation": chat[1]}},
                         lane=lid, version=1)
        assert r["lane"]["visible_chat_stream_id"] == chat[0]
    run(body)


def test_lead_loss_reconcile_and_restart_v1_v4():
    state = {}

    async def first(env):
        lead_a = await env.seat("lead-a", role="lead", parent_stream_id=FD)
        lid = (await env.adopt(key="stream:v1", lead=lead_a))["lane"]["lane_id"]
        lead_b = await env.seat("lead-b", role="lead", parent_stream_id=FD)
        await env.op("set_lead", {"lead": {"stream_id": lead_b[0], "generation": lead_b[1]}},
                     lane=lid, version=1)
        await env.store.mark_closed("host-a", "lead-b", closed_at="2026-10-07T20:00:00Z",
                                    pane_status="pane_dead", close_kind="manager_close")
        rows = await env.store.work_lane_rows()
        assert rows[0]["work_state"] == "active" and rows[0]["_qualifies"] is False
        assert len(await lead_lost_episodes(env.store, lid)) == 1
        assert len(await lead_lost_episodes(env.store, lid)) == 1  # once
        state["lid"] = lid

    async def second(env):
        shown = await env.store.get_work_lane(state["lid"])
        # Lead loss is a fault episode, never a stored pause or lane event.
        assert shown["lane"]["work_state"] == "active"
        assert [e["operation"] for e in shown["events"]] == ["adopt", "set_lead"]
        assert [e["cleared_at"] for e in await lead_lost_episodes(env.store, state["lid"])] == [None]
        assert shown["events"][1]["payload"]["prior_lead"]["stream_id"] == "host-a:lead-a"

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "v1.db")
        run(first, path)
        run(second, path)


def test_blocked_survives_lead_loss_v4():
    async def body(env):
        lead = await env.seat("lead-blk", role="lead", parent_stream_id=FD)
        lid = (await env.adopt(key="stream:blk", state="blocked", lead=lead, blocker="keys"))["lane"]["lane_id"]
        await env.store.mark_closed("host-a", "lead-blk", closed_at="2026-10-07T20:00:00Z",
                                    pane_status="pane_dead")
        assert await lead_lost_episodes(env.store, lid) == []
        lane = (await env.store.get_work_lane(lid))["lane"]
        assert lane["work_state"] == "blocked" and lane["blocker"] == "keys"
    run(body)


@pytest.mark.parametrize("kind", ["lane_started", "lane_blocked", "lane_unblocked", "lane_completed",
                                  "major_decision", "milestone"])
def test_each_update_kind_atomic_retry_restart_v12(kind):
    async def setup_lane(env):
        if kind == "lane_started":
            await seed_pending_goal(env.store, "epic_fixture_started",
                                    derive_lane_id(ASSISTANT, "stream:started"))
            return None
        lid = (await env.adopt(key="stream:k"))["lane"]["lane_id"]
        if kind == "lane_completed":
            await seed_lane_work(env.store, lid)
        if kind == "lane_unblocked":
            await env.op("set_state", {"to": "blocked", "blocker": "dep", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lid, version=1)
            return lid, 2
        return lid, 1

    def request(env, ctx):
        if kind == "lane_started":
            lead_payload = None
            return ("adopt", {"adoption_key": "stream:started", "title": "Started", "summary": "Goal",
                              "owner_kind": "operator", "work_state": "paused", "emit_started": True,
                              "goals": ["epic_fixture_started"],
                              "no_spec_reason": "Synthetic publication fixture.",
                              "visible_chat": {"stream_id": ASSISTANT}}, None, None, "adopt:stream:started")
        lid, version = ctx
        payload = {"lane_blocked": {"to": "blocked", "blocker": "Waiting on keys", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"},
                   "lane_unblocked": {"to": "active", "resolution": "Keys arrived"},
                   "lane_completed": {"to": "done", "outcome": "Shipped it"},
                   "major_decision": {"kind": "major_decision", "source_id": "dec-1", "summary": "Go web first"},
                   "milestone": {"kind": "milestone", "source_id": "m-1", "summary": "M1 merged",
                                 "grouped_source_ids": ["m-1a", "m-1b"]}}[kind]
        operation = "update" if kind in ("major_decision", "milestone") else "set_state"
        return operation, payload, lid, version, "kind-req"

    expected_text = {"lane_started": "Started — Goal", "lane_blocked": "Waiting on keys",
                     "lane_unblocked": "Keys arrived", "lane_completed": "Shipped it",
                     "major_decision": "Go web first", "milestone": "M1 merged"}[kind]

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "k.db")
        ctx_box = {}

        async def phase1(env):
            ctx = await setup_lane(env)
            ctx_box["ctx"] = ctx
            operation, payload, lid, version, rid = request(env, ctx)
            before = len(await lane_updates(env.store))

            def boom(_op):
                raise RuntimeError("injected")
            env.store._work_lane_fault = boom
            with pytest.raises(RuntimeError):
                await env.op(operation, payload, lane=lid, version=version, request_id=rid)
            env.store._work_lane_fault = None
            assert len(await lane_updates(env.store)) == before
            if lid:
                assert (await env.store.get_work_lane(lid))["lane"]["version"] == version
            out = await env.op(operation, payload, lane=lid, version=version, request_id=rid)
            assert out["update"] and out["duplicate"] is False
            ctx_box["lid"] = out["lane"]["lane_id"]
            ctx_box["req"] = (operation, payload, lid, version, rid)
        run(phase1, path)

        async def phase2(env):
            ups = [u for u in await lane_updates(env.store) if u["raw"]["lane_update"]["kind"] == kind]
            assert len(ups) == 1
            lu = ups[0]["raw"]["lane_update"]
            assert ups[0]["text"] == expected_text and lu["summary"] == expected_text
            assert lu["lane_id"] == ctx_box["lid"] and lu["update_id"] == ups[0]["message_id"][len("publication:"):]
            assert set(lu) == {"update_id", "lane_id", "kind", "summary", "source", "state", "prior_state",
                               "owner_kind", "title", "ts"}
            if kind == "milestone":
                assert lu["source"]["grouped_ids"] == ["m-1a", "m-1b"]
            operation, payload, lid, version, rid = ctx_box["req"]
            replay = await env.op(operation, payload, lane=lid, version=version, request_id=rid)
            assert replay["duplicate"] is True
            assert len([u for u in await lane_updates(env.store)
                        if u["raw"]["lane_update"]["kind"] == kind]) == 1
            if kind in ("major_decision", "milestone"):
                lane = (await env.store.get_work_lane(ctx_box["lid"]))["lane"]
                with pytest.raises(ValueError) as e:
                    await env.op(operation, payload, lane=lid, version=lane["version"], request_id="other-req")
                assert code(e) == "work_lane_update_duplicate"
        run(phase2, path)


def test_emit_started_once_and_owner_kind_v13():
    async def body(env):
        lead = await env.seat("op-lead", role="lead")
        lid = (await env.adopt(key="stream:op", owner="operator", lead=lead))["lane"]["lane_id"]
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "done", "outcome": "x"}, lane=lid, version=1)
        assert code(e) == "work_lane_override_reason_required"
        bad = [
            env.confirm("q-other-lane", "wl-other", "set_state:done"),
            env.confirm("q-other-action", lid, "set_owner:fd"),
            env.confirm("q-not-yet", lid, "set_state:done", answer="Not yet"),
            env.confirm("q-relay", lid, "set_state:done", actor_class="verified_agent_relay"),
            env.confirm("q-producer", lid, "set_state:done", producer="host-a:someone"),
        ]
        for c in bad:
            with pytest.raises(ValueError) as e:
                await env.op("set_state", {"to": "done", "outcome": "x", "operator_confirmation": c},
                             lane=lid, version=1)
            assert code(e) == "work_lane_operator_confirmation_mismatch", c
        # mismatched-target refusals for unguarded targets: test_supplied_confirmation_must_match_operation_qa_f2
        await seed_lane_work(env.store, lid)
        good = env.confirm("q-good", lid, "set_state:done")
        r = await env.op("set_state", {"to": "done", "outcome": "x", "operator_confirmation": good},
                         lane=lid, version=1)
        assert r["lane"]["work_state"] == "done" and r["event"]["consumed_question_id"] == "q-good"
        await seed_lane_work(env.store, lid, "in_progress")
        r = await env.op("set_state", {"to": "paused", "reason": "Reopen managed work", "next_action": "Resume after the fixture dependency clears"}, lane=lid, version=2)
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "done", "outcome": "y", "operator_confirmation": good},
                         lane=lid, version=3)
        assert code(e) == "work_lane_operator_confirmation_consumed"
        # owner handoff operator -> fd
        with pytest.raises(ValueError) as e:
            await env.op("set_owner", {"to": "fd"}, lane=lid, version=3)
        assert code(e) == "work_lane_override_reason_required"
        wrong = env.confirm("q-owner-wrong", lid, "set_state:done")
        with pytest.raises(ValueError) as e:
            await env.op("set_owner", {"to": "fd", "operator_confirmation": wrong}, lane=lid, version=3)
        assert code(e) == "work_lane_operator_confirmation_mismatch"
        own = env.confirm("q-owner", lid, "set_owner:fd")
        r = await env.op("set_owner", {"to": "fd", "operator_confirmation": own}, lane=lid, version=3)
        assert r["lane"]["owner_kind"] == "fd"
        kinds = [u["raw"]["lane_update"]["kind"] for u in await lane_updates(env.store)]
        assert kinds == ["lane_completed", "major_decision"]
        await seed_lane_work(env.store, lid)
        # fd lane: done needs no confirmation
        r = await env.op("set_state", {"to": "done", "outcome": "z"}, lane=lid, version=4)
        assert r["lane"]["work_state"] == "done"
    run(body)


def test_fd_lane_unaffected_and_adoption_owner_rules_v15():
    async def body(env):
        top = await env.seat("operator-top", role="lead")
        with pytest.raises(ValueError) as e:
            await env.adopt(key="stream:top", owner="fd", lead=top,
                            chat={"stream_id": top[0], "generation": top[1]})
        assert code(e) == "work_lane_owner_kind_unverified"
        payload = {"adoption_key": "stream:none", "title": "T", "work_state": "paused",
                   "visible_chat": {"stream_id": ASSISTANT}}
        with pytest.raises(ValueError) as e:
            await env.op("adopt", payload)
        assert code(e) == "work_lane_owner_kind_required"
        r = await env.adopt(key="stream:top-op", owner="operator", lead=top,
                            chat={"stream_id": top[0], "generation": top[1]})
        assert r["lane"]["owner_kind"] == "operator"
        child = await env.seat("fd-child", role="lead", parent_stream_id=FD)
        r = await env.adopt(key="stream:child", owner="fd", lead=child,
                            chat={"stream_id": child[0], "generation": child[1]})
        assert r["lane"]["owner_kind"] == "fd"
        again = await env.adopt(key="stream:child", owner="fd", lead=child,
                                chat={"stream_id": child[0], "generation": child[1]})
        assert again["duplicate"] is True
    run(body)


@pytest.mark.parametrize("path", ["visibility_subagent", "role_qa", "new_generation", "reconciled_dead"])
def test_each_lead_loss_path_reconciles_once_v4(path):
    # A hidden lead still leads an FD-owned lane (test_work_lane_hidden_fd_leads.py);
    # hiding is a lead loss only on an operator-owned lane.
    owner = "operator" if path == "visibility_subagent" else "fd"

    async def body(env):
        lead = await env.seat("loss-lead", role="lead", parent_stream_id=FD)
        lid = (await env.adopt(key="stream:loss", lead=lead, owner=owner))["lane"]["lane_id"]
        blk_lead = await env.seat("loss-blk", role="lead", parent_stream_id=FD)
        blk = (await env.adopt(key="stream:loss-blk", state="blocked", lead=blk_lead, blocker="b",
                               owner=owner))["lane"]
        for name in ("loss-lead", "loss-blk"):
            if path == "visibility_subagent":
                await env.store.update_session("host-a", name, visibility="subagent")
            elif path == "role_qa":
                await env.store.update_session("host-a", name, role="qa")
            elif path == "new_generation":
                await env.store.mark_closed("host-a", name, closed_at="2026-10-07T20:00:00Z",
                                            pane_status="pane_dead")
                await env.store.open_session("host-a", name, provider="claude", role="lead",
                                             parent_stream_id=FD)
            else:
                row = await env.store.fetch_session("host-a", name)
                gen = lead[1] if name == "loss-lead" else blk_lead[1]
                await env.store.mark_reconciled_dead("host-a", name, expected_generation=gen,
                                                     presumed_dead_at="2026-10-07T20:00:00Z",
                                                     closed_at="2026-10-07T20:00:00Z")
                assert row is not None
        rows = {r["lane_id"]: r for r in await env.store.work_lane_rows()}
        assert rows[lid]["_qualifies"] is False and rows[lid]["work_state"] == "active"
        assert len(await lead_lost_episodes(env.store, lid)) == 1
        assert len(await lead_lost_episodes(env.store, lid)) == 1
        shown = await env.store.get_work_lane(lid)
        assert shown["lane"]["work_state"] == "active"
        assert not [e for e in shown["events"] if e.get("update_kind")]  # lead loss posts no update
        assert await lead_lost_episodes(env.store, blk["lane_id"]) == []
        blocked = (await env.store.get_work_lane(blk["lane_id"]))["lane"]
        assert (blocked["work_state"], blocked["blocker"]) == ("blocked", "b")
        # a later qualifying set_lead is the repair: the lane never paused, and the fault clears
        fresh = await env.seat("fresh-lead", role="lead", parent_stream_id=FD)
        r = await env.op("set_lead", {"lead": {"stream_id": fresh[0], "generation": fresh[1]}},
                         lane=lid, version=shown["lane"]["version"])
        assert r["lane"]["work_state"] == "active"
        assert (await lead_lost_episodes(env.store, lid))[0]["cleared_at"] is not None
    run(body)


def test_handoff_follows_successor_d2():
    async def body(env):
        lead = await env.seat("ho-pred", role="lead", parent_stream_id=FD)
        lid = (await env.adopt(key="stream:ho", lead=lead,
                               chat={"stream_id": lead[0], "generation": lead[1]}))["lane"]["lane_id"]
        succ = await env.seat("ho-succ", role="lead", parent_stream_id=FD, handoff_from_stream_id=lead[0])
        moved = await env.store.work_lane_handoff(lead[0], succ[0])
        assert [m["lane_id"] for m in moved] == [lid]
        assert await env.store.work_lane_handoff(lead[0], succ[0]) == []
        await env.store.mark_closed("host-a", "ho-pred", closed_at="2026-10-07T20:00:00Z",
                                    pane_status="pane_dead", close_kind="handed_off")
        assert await lead_lost_episodes(env.store, lid) == []
        shown = await env.store.get_work_lane(lid)
        lane = shown["lane"]
        assert (lane["bound_stream_id"], lane["work_state"]) == (succ[0], "active")
        assert lane["visible_chat_stream_id"] == succ[0]
        handoff = [e for e in shown["events"] if e["operation"] == "lead_handoff"]
        assert len(handoff) == 1 and handoff[0]["payload"]["from"]["stream_id"] == lead[0]
    run(body)


def test_routing_close_and_cancel_guarded_on_operator_lane_v13():
    async def body(env):
        lead = await env.seat("rt-lead", role="lead")
        lid = (await env.adopt(key="stream:rt", owner="operator", lead=lead))["lane"]["lane_id"]

        async def routing(operation, payload, confirmation=None, op_id="rt-1"):
            return await env.store.apply_assistant_composite_operation(
                stream_id=ASSISTANT, operation_id=op_id, operation=operation, lane_id=lid,
                actor_stream_id=FD, payload=payload, dispatch_id="d", expected_lane_version=1,
                operator_confirmation=confirmation)

        close = {"completion_message_id": "m", "completion_disposition": "accepted"}
        cancel = {"decision_id": "x", "transition": "cancel", "from_phase": "discussion",
                  "to_phase": "cancelled", "operator_basis_message_ids": ["m"]}
        for operation, payload, action in (("lane.close", close, "lane.close"),
                                           ("lane.decision", cancel, "lane.decision:cancel")):
            with pytest.raises(ValueError) as e:
                await routing(operation, payload)
            assert code(e) == "work_lane_operator_confirmation_required"
            wrong = env.confirm("q-wrong-" + action, lid, "set_state:done")
            with pytest.raises(ValueError) as e:
                await routing(operation, payload, env.confirmations[wrong["question_id"]])
            assert code(e) == "work_lane_operator_confirmation_mismatch"
            ok = env.confirm("q-ok-" + action, lid, action)
            confirmation = env.confirmations[ok["question_id"]]
            if operation == "lane.close":
                with pytest.raises(ValueError, match="assistant_lane_close_state_invalid"):
                    await routing(operation, payload, confirmation)
                continue
            from store_work_lanes import WorkLaneCloseBlocked
            with pytest.raises(WorkLaneCloseBlocked) as blocked:
                await routing(operation, payload, confirmation)
            assert blocked.value.code == "work_lane_close_unfinished"
            assert not any(e["consumed_question_id"] for e in (await env.store.get_work_lane(lid))["events"])
            await seed_lane_work(env.store, lid)
            result = await routing(operation, payload, confirmation)
            assert result["lane"]["phase"] == "cancelled"
            events = (await env.store.get_work_lane(lid))["events"]
            assert sum(e.get("consumed_question_id") == ok["question_id"] for e in events) == 1
        # fd lane: routing guard never fires
        fd_lead = await env.seat("rt-fd", role="lead", parent_stream_id=FD)
        fd_lane = (await env.adopt(key="stream:rt-fd", lead=fd_lead))["lane"]["lane_id"]
        with pytest.raises(ValueError) as e:
            await env.store.apply_assistant_composite_operation(
                stream_id=ASSISTANT, operation_id="rt-fd", operation="lane.close", lane_id=fd_lane,
                actor_stream_id=FD, payload=close, dispatch_id="d", expected_lane_version=1)
        assert not code(e).startswith("work_lane_operator")
    run(body)


def test_real_notify_confirmation_round_trip_d5(tmp_path):
    from notify import Notify
    from test_question_contract_d3 import _FakeSessions, _ask

    async def go():
        producer = "fixture-fd:v2-fd"
        sessions = _FakeSessions({producer: {"visibility": "visible", "status": "open", "session_generation": "g1"}})
        notify = Notify(str(tmp_path / "notifications.db"), sessions=sessions)
        await notify.start()
        try:
            results = {}
            for qid, auth in (("q-op", {"operator_authenticated": True}),
                              ("q-relay", {"token_verified": True, "stream_id": "fixture-relay:v2-other"})):
                ask = _ask(qid, producer=producer, options=[{"label": "Confirm", "value": "Confirm"},
                                                             {"label": "Not yet", "value": "Not yet"}])
                ask["envelope"]["context"] = {"schema": "WorkLaneConfirmationV1", "lane_id": "wl-1",
                                              "action": "set_state:done"}
                assert (await notify.prompt(ask))["type"] == "prompt.ask.ok"
                reply = await notify.prompt({"type": "prompt.answer", "request_id": "a-" + qid,
                                             "question_id": qid, "selections": ["Confirm"],
                                             "_auth_context": auth})
                results[qid] = (reply, await notify.work_lane_confirmation(qid))
            reply, facts = results["q-op"]
            assert reply["type"] == "prompt.answer.ok", reply
            assert facts["work_lane_confirmation"] == {"lane_id": "wl-1", "action": "set_state:done"}
            assert facts["answer"] == "Confirm" and facts["actor_class"] == "direct_operator"
            assert facts["producer_stream_id"] == producer
            reply, facts = results["q-relay"]
            if reply["type"] == "prompt.answer.ok":
                assert facts["actor_class"] != "direct_operator"
            assert await notify.work_lane_confirmation("missing") is None
        finally:
            await notify.stop()
    asyncio.run(go())


def test_supplied_confirmation_must_match_operation_qa_f2():
    async def body(env):
        lead = await env.seat("f2-lead", role="lead")
        lid = (await env.adopt(key="stream:f2", owner="operator", lead=lead))["lane"]["lane_id"]
        done_q = env.confirm("q-f2-done", lid, "set_state:done")
        with pytest.raises(ValueError) as e:  # a done confirmation cannot ride on blocked
            await env.op("set_state", {"to": "blocked", "blocker": "b", "operator_confirmation": done_q, "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"},
                         lane=lid, version=1)
        assert code(e) == "work_lane_operator_confirmation_mismatch"
        fd_lead = await env.seat("f2-fd", role="lead", parent_stream_id=FD)
        fd_lane = (await env.adopt(key="stream:f2-fd", lead=fd_lead))["lane"]["lane_id"]
        own_q = env.confirm("q-f2-own", fd_lane, "set_owner:fd")
        with pytest.raises(ValueError) as e:  # set_owner:fd cannot authorize set_owner:operator
            await env.op("set_owner", {"to": "operator", "operator_confirmation": own_q},
                         lane=fd_lane, version=1)
        assert code(e) == "work_lane_operator_confirmation_mismatch"
        close_q = env.confirm("q-f2-close", fd_lane, "set_state:done")
        with pytest.raises(ValueError) as e:  # routing: a done confirmation is not lane.close
            await env.store.apply_assistant_composite_operation(
                stream_id=ASSISTANT, operation_id="f2-close", operation="lane.close", lane_id=fd_lane,
                actor_stream_id=FD, payload={"completion_message_id": "m", "completion_disposition": "accepted"},
                dispatch_id="d", expected_lane_version=1, operator_confirmation=env.confirmations["q-f2-close"])
        assert code(e) == "work_lane_operator_confirmation_mismatch"
        del close_q
    run(body)


def test_reopen_emits_no_update_even_to_blocked_qa_f4():
    async def body(env):
        lid = (await env.adopt(key="stream:f4"))["lane"]["lane_id"]
        await seed_lane_work(env.store, lid)
        await env.op("set_state", {"to": "done", "outcome": "ok"}, lane=lid, version=1)
        await seed_lane_work(env.store, lid, "in_progress")
        r = await env.op("set_state", {"to": "blocked", "blocker": "again", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lid, version=2)
        assert r["update"] is None and r["lane"]["work_state_reason"] == "reopened"
        assert r["lane"]["blocker"] == "again"
    run(body)


def test_projection_rechecks_visible_chat_eligibility_qa_f3():
    async def body(env):
        chat = await env.seat("f3-chat")
        await env.adopt(key="stream:f3", state="paused", lead=False, owner="operator",
                        chat={"stream_id": chat[0], "generation": chat[1]})
        assert (await env.store.work_lane_rows())[0]["_chat_available"] == "open"
        await env.store.update_session("host-a", "f3-chat", visibility="hidden")
        assert (await env.store.work_lane_rows())[0]["_chat_available"] == "unavailable"
    run(body)


def test_list_paging_is_tie_safe_qa_f1():
    async def body(env):
        from work_lanes_projection import WorkLanesInventory
        for key in ("stream:t1", "stream:t2", "stream:t3"):
            await env.adopt(key=key, state="paused", lead=False, owner="operator")

        def tie(conn):
            conn.execute("UPDATE v2_assistant_composite_lanes SET updated_at='2026-10-07T17:00:00.000Z' "
                         "WHERE work_state IS NOT NULL")
            conn.commit()
        await env.store.submit(tie)

        class _NoSessions:
            def list_open(self):
                return []
        inv = WorkLanesInventory(env.store, _NoSessions(), None)
        seen, cursor = [], {}
        for _ in range(5):
            page = await inv.list(limit=1, **cursor)
            seen += [lane["lane_id"] for lane in page["lanes"]]
            if not page["next_before_updated_at"]:
                break
            cursor = {"before_updated_at": page["next_before_updated_at"],
                      "before_lane_id": page["next_before_lane_id"]}
        assert len(seen) == 3 and len(set(seen)) == 3
    run(body)


def test_park_paused_lane_and_plain_pause_unparks():
    async def body(env):
        lane = (await env.adopt(state="paused", owner="operator"))["lane"]
        parked = await env.op("set_state", {"to": "paused", "parked": True, "reason": "  Await a new season  "},
                              lane=lane["lane_id"], version=1, request_id="park")
        assert parked["lane"]["work_state"] == "paused"
        assert parked["lane"]["parked_at"] == parked["event"]["created_at"]
        assert parked["lane"]["parked_reason"] == "Await a new season"
        assert parked["event"]["payload"]["reason"] == "  Await a new season  "
        assert parked["update"] is None
        unparked = await env.op("set_state", {"to": "paused", "reason": "Resume soon", "next_action": "Schedule work"},
                                lane=lane["lane_id"], version=2)
        assert unparked["lane"]["parked_at"] is None
        assert unparked["lane"]["parked_reason"] is None
    run(body)


@pytest.mark.parametrize("state", ["active", "blocked", "done", "paused"])
def test_parking_transitions_replay_and_version(state):
    async def body(env):
        lane = (await env.adopt())["lane"]
        lid = lane["lane_id"]
        if state == "done":
            await seed_lane_work(env.store, lid)
        if state != "active":
            lane = (await env.op("set_state", {"to": state, "reason": "Wait", "next_action": "Resume",
                          "blocker": "Dependency", "outcome": "Complete"}, lane=lid, version=lane["version"]))["lane"]
        if state == "done":
            await seed_lane_work(env.store, lid, "in_progress")
        payload = {"to": "paused", "parked": True, "reason": "\t Later season \t"}
        result = await env.op("set_state", payload, lane=lid, version=lane["version"], request_id="park")
        assert result["lane"]["parked_reason"] == "Later season"
        assert result["lane"]["blocker"] is None
        assert result["event"]["update_kind"] == ("lane_unblocked" if state == "blocked" else None)
        if state == "blocked":
            assert result["event"]["payload"]["cleared_blocker"] == "Dependency"
        replay = await env.op("set_state", payload, lane=lid, version=lane["version"], request_id="park")
        assert replay["duplicate"] is True
        with pytest.raises(ValueError, match="assistant_lane_version_conflict"):
            await env.op("set_state", payload, lane=lid, version=lane["version"])
        with pytest.raises(ValueError, match="work_lane_state_unchanged"):
            await env.op("set_state", payload, lane=lid, version=result["lane"]["version"])
    run(body)


@pytest.mark.parametrize("reason,error", [(None, "work_lane_reason_required"), (" \t", "work_lane_reason_required"),
    ("x" * 161, "work_lane_parked_reason_invalid"), ("a\nb", "work_lane_parked_reason_invalid"),
    ("a\x7fb", "work_lane_parked_reason_invalid"), (42, "work_lane_parked_reason_invalid")])
def test_parking_reason_validation(reason, error):
    async def body(env):
        lane = (await env.adopt())["lane"]
        with pytest.raises(ValueError, match=error):
            await env.op("set_state", {"to": "paused", "parked": True, "reason": reason},
                         lane=lane["lane_id"], version=1)
        assert (await env.store.get_work_lane(lane["lane_id"]))["lane"]["version"] == 1
    run(body)


@pytest.mark.parametrize("target,flag", [("active", True), ("done", False), ("blocked", True),
                                          ("paused", "true"), ("paused", 1), ("paused", None)])
def test_parked_payload_is_boolean_and_pause_only(target, flag):
    async def body(env):
        lane = (await env.adopt())["lane"]
        with pytest.raises(ValueError, match="work_lane_payload_invalid"):
            await env.op("set_state", {"to": target, "parked": flag, "reason": "Later"},
                         lane=lane["lane_id"], version=1)
    run(body)


def test_adopt_cannot_park():
    async def body(env):
        with pytest.raises(ValueError, match="work_lane_payload_invalid"):
            await env.adopt(parked=True)
    run(body)


@pytest.mark.parametrize("target,kind", [("active", None), ("blocked", "lane_blocked"), ("done", "lane_completed"),
                                       ("paused", None)])
def test_unparking_obeys_transition_rules(target, kind):
    async def body(env):
        lane = (await env.adopt())["lane"]
        lid = lane["lane_id"]
        await env.op("set_state", {"to": "paused", "parked": True, "reason": "Later"}, lane=lid, version=1)
        if target == "done":
            await seed_lane_work(env.store, lid)
        if target == "paused":
            for payload, error in [({"to": target}, "work_lane_reason_required"),
                                   ({"to": target, "reason": "Resume"}, "work_lane_next_action_required")]:
                with pytest.raises(ValueError, match=error):
                    await env.op("set_state", payload, lane=lid, version=2)
        result = await env.op("set_state", {"to": target, "reason": "Resume", "next_action": "Start",
                                  "blocker": "Dependency", "outcome": "Complete"}, lane=lid, version=2)
        assert result["lane"]["parked_at"] is None and result["lane"]["parked_reason"] is None
        assert result["event"]["update_kind"] == kind
    run(body)


def test_parked_operator_confirmation_and_unqualified_lead():
    async def body(env):
        lane = (await env.adopt(state="paused", owner="operator"))["lane"]
        lid = lane["lane_id"]
        with pytest.raises(ValueError, match="work_lane_reason_required"):
            await env.op("set_state", {"to": "paused", "parked": True}, lane=lid, version=1)
        confirm = env.confirm("park-confirm", lid, "set_state:paused")
        result = await env.op("set_state", {"to": "paused", "parked": True, "reason": "Later",
                                           "operator_confirmation": confirm}, lane=lid, version=1)
        assert result["event"]["consumed_question_id"] == "park-confirm"
        with pytest.raises(ValueError, match="work_lane_visible_lead_required"):
            await env.op("set_state", {"to": "active", "reason": "Start"}, lane=lid, version=2)
        assert (await env.store.get_work_lane(lid))["lane"]["parked_at"]
    run(body)


def test_park_record_survives_other_mutations_and_preserves_membership_and_hue():
    async def body(env):
        lane = (await env.adopt(members=["spec_demo__parked"]))["lane"]
        lid = lane["lane_id"]
        hue_before = await env.store.submit(lambda conn: tuple(conn.execute(
            "SELECT hue,hue_open_order FROM v2_assistant_composite_lanes WHERE lane_id=?", (lid,)).fetchone()))
        parked = (await env.op("set_state", {"to": "paused", "parked": True, "reason": "Later"}, lane=lid, version=1))["lane"]
        lead = await env.seat("replacement", role="lead", parent_stream_id=FD)
        for operation, payload in [("set_text", {"title": "New name"}),
            ("set_members", {"members": ["spec_demo__parked", "spec_demo__extra"]}),
            ("set_lead", {"lead": {"stream_id": lead[0], "generation": lead[1]}}),
            ("set_owner", {"to": "operator"})]:
            current = (await env.store.get_work_lane(lid))["lane"]
            result = await env.op(operation, payload, lane=lid, version=current["version"])
            assert result["lane"]["parked_at"] == parked["parked_at"]
            assert result["lane"]["parked_reason"] == "Later"
        other = (await env.adopt(key="stream:other"))["lane"]
        with pytest.raises(ValueError, match="work_lane_member_conflict"):
            await env.op("set_members", {"members": ["spec_demo__parked"]}, lane=other["lane_id"], version=1)
        current = (await env.store.get_work_lane(lid))["lane"]
        await env.op("set_state", {"to": "paused", "parked": False, "reason": "Resume", "next_action": "Start"},
                     lane=lid, version=current["version"])
        hue_after = await env.store.submit(lambda conn: tuple(conn.execute(
            "SELECT hue,hue_open_order FROM v2_assistant_composite_lanes WHERE lane_id=?", (lid,)).fetchone()))
        assert hue_after == hue_before
    run(body)


def test_parking_done_lane_preserves_reopen_member_conflict_guard():
    async def body(env):
        lane = (await env.adopt(members=["spec_demo__parked"]))["lane"]
        await seed_lane_work(env.store, lane["lane_id"])
        await env.op("set_state", {"to": "done", "outcome": "Complete"}, lane=lane["lane_id"], version=1)
        await env.adopt(key="stream:other", members=["spec_demo__parked"])
        with pytest.raises(ValueError, match="work_lane_member_conflict"):
            await env.op("set_state", {"to": "paused", "parked": True, "reason": "Later"},
                         lane=lane["lane_id"], version=2)
    run(body)


def test_set_text_new_icon_and_clear_restores_fallback():
    from work_lanes_projection import project_lane
    async def body(env):
        lane = (await env.adopt(title="Business acquisition"))["lane"]
        for version, icon, expected in [(1, "headset", "headset"), (2, None, "briefcase")]:
            result = await env.op("set_text", {"icon": icon}, lane=lane["lane_id"], version=version)
            assert result["lane"]["icon"] == icon
            shown = await env.store.get_work_lane(lane["lane_id"])
            assert project_lane(shown["_projection_row"], None, "2026-01-01T00:00:00Z")["icon"] == expected
    run(body)


def test_manual_activation_accepts_work_seat_and_refuses_without_it():
    from test_work_owed import observe
    async def body(env):
        spec = 'spec_demo__activate'
        await observe(env.store, 'in_progress', 'activate', 100, identity=spec)
        lane = (await env.adopt(key='stream:manual-seat', state='paused', owner='operator', members=[spec]))['lane']
        with pytest.raises(ValueError, match='work_lane_visible_lead_required'):
            await env.op('set_state', {'to': 'active', 'reason': 'Work started'}, lane=lane['lane_id'], version=lane['version'])
        await env.seat('manual-worker', role='worker', assigned_spec_id=spec)
        result = await env.op('set_state', {'to': 'active', 'reason': 'Work started'}, lane=lane['lane_id'], version=lane['version'])
        assert result['lane']['work_state'] == 'active'
    run(body)


def test_done_activation_work_seat_still_checks_member_conflict():
    from test_work_owed import observe
    async def body(env):
        spec = 'spec_demo__conflicting'
        await observe(env.store, 'in_progress', 'conflicting', 100, identity=spec)
        lane = (await env.adopt(key='stream:closed-seat', state='paused', owner='operator', members=[spec]))['lane']
        await seed_lane_work(env.store, lane["lane_id"])
        done = (await env.op('set_state', {'to': 'done', 'outcome': 'Finished', 'operator_confirmation': env.confirm('close-seat', lane['lane_id'], 'set_state:done')}, lane=lane['lane_id'], version=lane['version']))['lane']
        await seed_lane_work(env.store, lane["lane_id"], "in_progress")
        await env.adopt(key='stream:competing-seat', state='paused', owner='operator', members=[spec])
        await env.seat('conflict-worker', role='worker', assigned_spec_id=spec)
        before = await env.store.get_work_lane(lane['lane_id'])
        with pytest.raises(ValueError, match='work_lane_member_conflict'):
            await env.op('set_state', {'to': 'active', 'reason': 'Work started'}, lane=lane['lane_id'], version=done['version'])
        after = await env.store.get_work_lane(lane['lane_id'])
        assert after['lane']['work_state'] == 'done' and after['lane']['version'] == done['version']
        assert after['events'] == before['events']
    run(body)


def test_done_lane_activates_without_lead_when_work_seat_exists():
    from test_work_owed import observe
    async def body(env):
        spec = 'spec_demo__done_resume'
        await observe(env.store, 'in_progress', 'done-resume', 100, identity=spec)
        lane = (await env.adopt(key='stream:done-resume', state='paused', owner='operator', members=[spec]))['lane']
        confirmation = env.confirm('close-done-resume', lane['lane_id'], 'set_state:done')
        await seed_lane_work(env.store, lane["lane_id"])
        done = (await env.op('set_state', {'to': 'done', 'outcome': 'Completed prior work', 'operator_confirmation': confirmation},
                             lane=lane['lane_id'], version=lane['version']))['lane']
        await seed_lane_work(env.store, lane["lane_id"], "in_progress")
        await env.seat('done-worker', role='worker', assigned_spec_id=spec)
        active = (await env.op('set_state', {'to': 'active', 'reason': 'New work'}, lane=lane['lane_id'], version=done['version']))['lane']
        assert active['work_state'] == 'active'
    run(body)

@pytest.mark.parametrize('packet_state', ['working', 'queued', 'review'])
def test_external_packet_resumes_and_allows_activation_without_lead(packet_state):
    from external_work import ExternalWorkConfig
    from test_work_owed import observe
    async def body(env):
        spec = 'spec_demo__external_resume'
        await observe(env.store, 'in_progress', 'external-resume', 100, identity=spec)
        lane = (await env.adopt(key='stream:external-resume', state='paused', owner='operator', members=[spec]))['lane']
        env.store.external_work_config = ExternalWorkConfig(status='enabled', watch_id='dot', label='Dot')
        def insert(conn):
            conn.execute('INSERT INTO v2_external_work_packets VALUES (?,?,?,?,?,?,?,?)',
                         ('dot', 'packet', spec, packet_state, 1, 1, 1, None))
            conn.commit()
        await env.store.submit(insert)
        result = await env.op('set_state', {'to': 'active', 'reason': 'Work started'}, lane=lane['lane_id'], version=lane['version'])
        assert result['lane']['work_state'] == 'active'
        rows = await env.store.work_lane_rows()
        assert next(x for x in rows if x['lane_id'] == lane['lane_id'])['_has_external_work']
        if packet_state == 'working':
            with pytest.raises(ValueError, match='work_lane_pause_working'):
                await _pause_actual(env, result['lane'])
            assert (await env.store.get_work_lane(lane['lane_id']))['lane'] == result['lane']
        else:
            paused = (await _pause_actual(env, result['lane']))['lane']
            assert lane['lane_id'] not in (await env.store.reconcile_lane_lifecycle())['resumed']
            after = (await env.store.get_work_lane(lane['lane_id']))['lane']
            assert after['work_state'] == 'paused' and after['version'] == paused['version']
    run(body)


def test_packet_allows_done_lane_activation_but_preserves_member_conflict():
    from external_work import ExternalWorkConfig
    from test_work_owed import observe
    async def body(env):
        spec = 'spec_demo__packet_conflict'
        await observe(env.store, 'in_progress', 'packet-conflict', 100, identity=spec)
        lane = (await env.adopt(key='stream:packet-done', state='paused', owner='operator', members=[spec]))['lane']
        confirm = env.confirm('packet-done-confirm', lane['lane_id'], 'set_state:done')
        await seed_lane_work(env.store, lane["lane_id"])
        done = (await env.op('set_state', {'to':'done', 'outcome':'Prior work', 'operator_confirmation':confirm},
                             lane=lane['lane_id'], version=lane['version']))['lane']
        await seed_lane_work(env.store, lane["lane_id"], "in_progress")
        await env.adopt(key='stream:packet-conflict', state='paused', owner='operator', members=[spec])
        env.store.external_work_config = ExternalWorkConfig(status='enabled', watch_id='dot', label='Dot')
        def insert(conn):
            conn.execute('INSERT INTO v2_external_work_packets VALUES (?,?,?,?,?,?,?,?)',
                         ('dot', 'packet', spec, 'queued', 1, 1, 1, None))
            conn.commit()
        await env.store.submit(insert)
        with pytest.raises(ValueError, match='work_lane_member_conflict'):
            await env.op('set_state', {'to':'active', 'reason':'Start'}, lane=lane['lane_id'], version=done['version'])
        assert (await env.store.get_work_lane(lane['lane_id']))['lane']['work_state'] == 'done'
    run(body)


async def _actual_work_fixture(env, *, state="active", key="stream:actual", count=1):
    from test_work_owed import observe
    identities = [f"spec_demo__actual_{n}" for n in range(count)]
    for identity in identities:
        await observe(env.store, "in_progress", identity, 100, identity=identity)
    lane = (await env.adopt(key=key, state=state, members=identities))["lane"]
    return lane, identities


def _lane_activity(stream, generation, *, working=False, background=False, age=0, dead=False):
    import time
    return dict(stream_id=stream, session_generation=generation, capture_generation=generation,
                capture_observed_at=time.time() - age, capture_liveness="idle", online=not dead,
                pane_status="pane_dead" if dead else "pane_alive", working=working, background_busy=background)


async def _pause_actual(env, lane):
    return await env.op("set_state", {"to": "paused", "reason": "Waiting for documents",
                                    "next_action": "Resume on execution"},
                        lane=lane["lane_id"], version=lane["version"])


@pytest.mark.parametrize("state", ["queued", "review"])
def test_actual_work_idle_and_queued_stays_paused_then_resumes_once(tmp_path, state):
    from external_work import ExternalWorkConfig
    async def exercise(env):
        lane, ids = await _actual_work_fixture(env)
        worker, generation = await env.seat("actual-idle", role="worker", assigned_spec_id=ids[0])
        env.store.lane_presence_reader = lambda: [_lane_activity(worker, generation)]
        env.store.external_work_config = ExternalWorkConfig("enabled", "dot", "Dot")
        await env.store.submit(lambda c: _put_actual_packet(c, ids[0], state))
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
        paused = (await _pause_actual(env, lane))["lane"]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
        await _assert_lane_surfaces(env, paused, "paused")
        before = await env.store.get_work_lane(lane["lane_id"])
        return paused, worker, generation, before["events"], ids[0]

    async def scenario(env):
        state_data.update(zip(("paused", "worker", "generation", "events", "spec"), await exercise(env)))

    db = str(tmp_path/"actual-lanes.db")
    state_data = {}
    run(scenario, db)

    async def after_restart(env):
        paused = state_data["paused"]
        worker, gen = state_data["worker"], state_data["generation"]
        env.store.external_work_config = ExternalWorkConfig("enabled", "dot", "Dot")
        env.store.lane_presence_reader = lambda: [_lane_activity(worker, gen)]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
        await _assert_lane_surfaces(env, paused, "paused")
        env.store.lane_presence_reader = lambda: [_lane_activity(worker, gen, working=True)]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == [paused["lane_id"]]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
        await _assert_lane_surfaces(env, paused, "active")
        actual = await env.store.get_work_lane(paused["lane_id"])
        assert actual["lane"]["version"] == paused["version"] + 1
        assert actual["events"][:-1] == state_data["events"]
        assert actual["events"][-1]["payload"] == {"to": "active", "auto": "work_started"}
    run(after_restart, db)


def _put_actual_packet(conn, spec, state):
    import time
    with conn:
        now = time.time()
        conn.execute("INSERT INTO v2_external_work_packets VALUES (?,?,?,?,?,?,?,?)",
                     ("dot", "fixture-actual-packet", spec, state, now, now, now, None))


async def _assert_lane_surfaces(env, lane, expected):
    from work_lanes_projection import project_lane, _iso_now
    lid = lane["lane_id"]
    listed = next(row for row in await env.store.work_lane_rows() if row["lane_id"] == lid)
    detail = (await env.store.get_work_lane(lid))["_projection_row"]
    for row in (listed, detail):
        assert project_lane(row, None, _iso_now())["state"] == expected


@pytest.mark.parametrize("evidence", ["local", "background", "external", "ninth"])
def test_actual_work_refuses_pause_without_mutation(evidence):
    from external_work import ExternalWorkConfig
    async def scenario(env):
        lane, ids = await _actual_work_fixture(env, count=9 if evidence=="ninth" else 1)
        if evidence=="external":
            env.store.external_work_config = ExternalWorkConfig("enabled", "dot", "Dot")
            await env.store.submit(lambda c: _put_actual_packet(c, ids[-1], "working"))
        else:
            worker, gen = await env.seat("actual-busy", role="worker", assigned_spec_id=ids[-1])
            env.store.lane_presence_reader = lambda: [_lane_activity(worker, gen,
                working=evidence!="background", background=evidence=="background")]
        before = await env.store.get_work_lane(lane["lane_id"])
        with pytest.raises(ValueError, match="work_lane_pause_working") as exc:
            await _pause_actual(env, lane)
        assert ids[-1] in str(exc.value)
        if evidence=="external":
            assert "fixture-actual-packet" in str(exc.value)
        after = await env.store.get_work_lane(lane["lane_id"])
        assert after["lane"] == before["lane"] and after["events"] == before["events"]
    run(scenario)


@pytest.mark.parametrize("fault", ["membership", "absent", "stale", "generation", "presence_read", "external_config", "external_read"])
def test_actual_work_unknown_refuses_pause_and_defers_resume(monkeypatch, fault):
    from external_work import ExternalWorkConfig
    import store_external_work
    import store_work_lanes
    async def scenario(env):
        lane, ids = await _actual_work_fixture(env)
        worker, gen = await env.seat("actual-unknown", role="worker", assigned_spec_id=ids[0])
        env.store.lane_presence_reader = lambda: [_lane_activity(worker, gen)]
        before = await env.store.get_work_lane(lane["lane_id"])
        with monkeypatch.context() as patch:
            if fault == "membership":
                original = store_work_lanes.effective_membership_ids_conn
                def unknown(conn):
                    snapshot = original(conn)
                    snapshot["state"]["available"] = False
                    return snapshot
                patch.setattr(store_work_lanes, "effective_membership_ids_conn", unknown)
            elif fault == "presence_read":
                def failed_presence():
                    raise RuntimeError("fixture observer unavailable")
                patch.setattr(env.store, "lane_presence_reader", failed_presence)
            elif fault == "external_read":
                patch.setattr(env.store, "external_work_config",
                              ExternalWorkConfig("enabled", "dot", "Dot"), raising=False)
                def failed(*args, **kwargs):
                    raise sqlite3.OperationalError("fixture read unavailable")
                patch.setattr(store_external_work, "current_packets_conn", failed)
            elif fault == "external_config":
                patch.setattr(env.store, "external_work_config", ExternalWorkConfig("config_invalid"), raising=False)
            else:
                rows = [] if fault == "absent" else [_lane_activity(
                    worker, "replaced" if fault == "generation" else gen,
                    age=3600 if fault == "stale" else 0)]
                patch.setattr(env.store, "lane_presence_reader", lambda: rows)
            with pytest.raises(ValueError, match="work_lane_pause_activity_unavailable") as exc:
                await _pause_actual(env, lane)
            assert getattr(exc.value, "details", {}).get("retryable") is True
            # Independent lifecycle control: a pre-existing paused row must
            # stay paused while exactly the same input remains unknown.
            await env.store.submit(lambda c: _seed_actual_paused(c, lane["lane_id"]))
            assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
        actual = await env.store.get_work_lane(lane["lane_id"])
        assert actual["lane"]["version"] == before["lane"]["version"] and actual["events"] == before["events"]
        assert actual["lane"]["work_state"] == "paused"
        await env.store.submit(lambda c: _seed_actual_paused(c, lane["lane_id"], "active"))
        paused = (await _pause_actual(env, lane))["lane"]
        env.store.lane_presence_reader = lambda: [_lane_activity(worker, gen, working=True)]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == [paused["lane_id"]]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
    run(scenario)


def _seed_actual_paused(conn, lid, state="paused"):
    with conn:
        conn.execute("UPDATE v2_assistant_composite_lanes SET work_state=? WHERE lane_id=?", (state, lid))


def test_actual_work_explicitly_disabled_and_dead_assignment_allow_pause():
    from external_work import ExternalWorkConfig
    async def scenario(env):
        lane, ids = await _actual_work_fixture(env)
        worker, gen = await env.seat("actual-dead", role="worker", assigned_spec_id=ids[0])
        env.store.lane_presence_reader = lambda: [_lane_activity(worker, gen, dead=True, working=True)]
        env.store.external_work_config = ExternalWorkConfig("disabled")
        paused = (await _pause_actual(env, lane))["lane"]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
        await _assert_lane_surfaces(env, paused, "paused")
    run(scenario)


def test_actual_work_external_starts_once_without_local_assignment():
    from external_work import ExternalWorkConfig
    async def scenario(env):
        lane, ids = await _actual_work_fixture(env)
        env.store.external_work_config = ExternalWorkConfig("enabled", "dot", "Dot")
        await env.store.submit(lambda c: _put_actual_packet(c, ids[0], "queued"))
        paused = (await _pause_actual(env, lane))["lane"]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
        def start(conn):
            with conn:
                conn.execute("UPDATE v2_external_work_packets SET state='working' WHERE tag='fixture-actual-packet'")
        await env.store.submit(start)
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == [lane["lane_id"]]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
        shown = await env.store.get_work_lane(lane["lane_id"])
        assert shown["lane"]["version"] == paused["version"] + 1
        await _assert_lane_surfaces(env, lane, "active")
    run(scenario)


def test_actual_work_start_at_pause_write_boundary_is_seen_next_reconcile():
    async def scenario(env):
        lane, ids = await _actual_work_fixture(env)
        worker, gen = await env.seat("actual-racing", role="worker", assigned_spec_id=ids[-1])
        running = False
        def snapshot():
            return [_lane_activity(worker, gen, working=running)]
        env.store.lane_presence_reader = snapshot
        def start(operation):
            nonlocal running
            assert operation == "set_state"
            running = True  # Observer sees the start after the pause's Store UPDATE.
        env.store._work_lane_fault = start
        paused = (await _pause_actual(env, lane))["lane"]
        env.store._work_lane_fault = None
        assert paused["work_state"] == "paused" and running
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == [lane["lane_id"]]
        active = (await env.store.get_work_lane(lane["lane_id"]))["lane"]
        assert active["version"] == paused["version"] + 1 and active["work_state"] == "active"
        with pytest.raises(ValueError, match="work_lane_pause_working"):
            await _pause_actual(env, active)
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
    run(scenario)


def test_actual_work_never_auto_resumes_operator_park():
    async def scenario(env):
        lane, ids = await _actual_work_fixture(env)
        worker, gen = await env.seat("actual-park-busy", role="worker", assigned_spec_id=ids[0])
        env.store.lane_presence_reader = lambda: [_lane_activity(worker, gen, working=True)]
        parked = (await env.op("set_state", {"to": "paused", "parked": True,
              "reason": "Explicit deliberate hold"}, lane=lane["lane_id"], version=lane["version"]))["lane"]
        assert parked["parked_at"] and parked["work_state"] == "paused"
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
        assert (await env.store.get_work_lane(lane["lane_id"]))["lane"] == parked
    run(scenario)


def test_actual_work_unknown_local_source_defers_even_a_working_external_packet():
    from external_work import ExternalWorkConfig
    async def scenario(env):
        lane, ids = await _actual_work_fixture(env)
        await _pause_actual(env, lane)
        worker, gen = await env.seat("unknown-local-external-working", role="worker", assigned_spec_id=ids[0])
        env.store.lane_presence_reader = lambda: []
        env.store.external_work_config = ExternalWorkConfig("enabled", "dot", "Dot")
        await env.store.submit(lambda c: _put_actual_packet(c, ids[0], "working"))
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
        env.store.lane_presence_reader = lambda: [_lane_activity(worker, gen)]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == [lane["lane_id"]]
        assert (await env.store.reconcile_lane_lifecycle())["resumed"] == []
    run(scenario)
