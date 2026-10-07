"""First-class work lanes: store operations, CAS, idempotency, lifecycle, owner kind (V1-V9, V12, V13)."""
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from assistant_composite import AssistantComposite
from store import Store
from test_assistant_prose_mirror import ASSISTANT, ROOT, _config

FD = ROOT


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

    async def op(self, operation, payload, *, lane=None, version=None, request_id=None, actor=FD, gen=None):
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
        row = await self.store.open_session("amaterasu", name, **fields)
        return f"amaterasu:{name}", row["session_generation"]

    async def adopt(self, key="stream:x", state="active", owner="fd", lead=None, chat=None, **extra):
        if lead is None and state == "active":
            lead = await self.seat("lead-" + key.replace(":", "-"), role="lead", parent_stream_id=FD)
        payload = {"adoption_key": key, "title": "Lane " + key, "summary": "Goal line", "owner_kind": owner,
                   "work_state": state, "visible_chat": chat or {"stream_id": ASSISTANT}, **extra}
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
        await env.op("set_state", {"to": "paused"}, lane=lane["lane_id"], version=1, request_id="p1")
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "blocked", "blocker": "x"}, lane=lane["lane_id"], version=1)
        assert code(e) == "assistant_lane_version_conflict"
        again = await env.op("set_state", {"to": "paused"}, lane=lane["lane_id"], version=1, request_id="p1")
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
        r = await env.op("set_state", {"to": "paused"}, lane=lid, version=1)
        assert r["update"] is None  # pause emits nothing
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "paused"}, lane=lid, version=2)
        assert code(e) == "work_lane_state_unchanged"
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "blocked"}, lane=lid, version=2)
        assert code(e) == "work_lane_blocker_required"
        r = await env.op("set_state", {"to": "blocked", "blocker": "Need keys"}, lane=lid, version=2)
        assert r["lane"]["blocker"] == "Need keys" and r["update"]["update_id"].startswith("lane-update:" + lid)
        r = await env.op("set_state", {"to": "active"}, lane=lid, version=3)
        assert r["lane"]["blocker"] is None and r["event"]["payload"]["cleared_blocker"] == "Need keys"
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "done"}, lane=lid, version=4)
        assert code(e) == "work_lane_outcome_required"
        r = await env.op("set_state", {"to": "done", "outcome": "Shipped"}, lane=lid, version=4)
        assert r["lane"]["done_at"]
        assert [l["lane_id"] for l in await env.store.work_lane_rows()] == []
        assert [l["lane_id"] for l in await env.store.work_lane_rows(include_done=True)] == [lid]
        r = await env.op("set_state", {"to": "paused"}, lane=lid, version=5)
        assert r["lane"]["work_state_reason"] == "reopened" and r["update"] is None
        kinds = [u["raw"]["lane_update"]["kind"] for u in await lane_updates(env.store)]
        assert kinds == ["lane_blocked", "lane_unblocked", "lane_completed"]
        texts = [u["text"] for u in await lane_updates(env.store)]
        assert texts == ["Need keys", "Blocker cleared: Need keys", "Shipped"]
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
            await env.op("set_state", {"to": "active"}, lane=lid, version=1)
        assert code(e) == "work_lane_visible_lead_required"
        lead = await env.seat("real-lead", role="lead", parent_stream_id=FD)
        r = await env.op("set_lead", {"lead": {"stream_id": lead[0], "generation": lead[1]}}, lane=lid, version=1)
        assert r["lane"]["work_state"] == "paused"  # binding never resumes
        r = await env.op("set_state", {"to": "active"}, lane=lid, version=2)
        assert r["lane"]["work_state"] == "active"
    run(body)


def test_protected_assistant_seat_is_ineligible(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "bart")
    async def body(env):
        lid = (await env.adopt(state="paused", lead=False, owner="operator"))["lane"]["lane_id"]
        bart = await env.seat("bart-seat", role="bart")
        with pytest.raises(ValueError) as e:
            await env.op("set_lead", {"lead": {"stream_id": bart[0], "generation": bart[1]}}, lane=lid, version=1)
        assert code(e) == "work_lane_lead_ineligible"
        with pytest.raises(ValueError) as e:
            await env.op("set_chat", {"visible_chat": {"stream_id": bart[0], "generation": bart[1]}},
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
                       {"stream_id": "amaterasu:missing", "generation": "g"}):
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
        await env.store.mark_closed("amaterasu", "lead-b", closed_at="2026-10-07T20:00:00Z",
                                    pane_status="pane_dead", close_kind="manager_close")
        rows = await env.store.work_lane_rows()
        assert rows[0]["work_state"] == "active" and rows[0]["_qualifies"] is False
        assert [c["lane_id"] for c in await env.store.reconcile_work_lanes()] == [lid]
        assert await env.store.reconcile_work_lanes() == []  # once
        state["lid"] = lid

    async def second(env):
        shown = await env.store.get_work_lane(state["lid"])
        assert shown["lane"]["work_state"] == "paused"
        assert shown["lane"]["work_state_reason"] == "lead_lost"
        assert [e["operation"] for e in shown["events"]] == ["adopt", "set_lead", "lead_lost"]
        assert shown["events"][1]["payload"]["prior_lead"]["stream_id"] == "amaterasu:lead-a"

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "v1.db")
        run(first, path)
        run(second, path)


def test_blocked_survives_lead_loss_v4():
    async def body(env):
        lead = await env.seat("lead-blk", role="lead", parent_stream_id=FD)
        lid = (await env.adopt(key="stream:blk", state="blocked", lead=lead, blocker="keys"))["lane"]["lane_id"]
        await env.store.mark_closed("amaterasu", "lead-blk", closed_at="2026-10-07T20:00:00Z",
                                    pane_status="pane_dead")
        assert await env.store.reconcile_work_lanes() == []
        lane = (await env.store.get_work_lane(lid))["lane"]
        assert lane["work_state"] == "blocked" and lane["blocker"] == "keys"
    run(body)


@pytest.mark.parametrize("kind", ["lane_started", "lane_blocked", "lane_unblocked", "lane_completed",
                                  "major_decision", "milestone"])
def test_each_update_kind_atomic_retry_restart_v12(kind):
    async def setup_lane(env):
        if kind == "lane_started":
            return None
        lid = (await env.adopt(key="stream:k"))["lane"]["lane_id"]
        if kind == "lane_unblocked":
            await env.op("set_state", {"to": "blocked", "blocker": "dep"}, lane=lid, version=1)
            return lid, 2
        return lid, 1

    def request(env, ctx):
        if kind == "lane_started":
            lead_payload = None
            return ("adopt", {"adoption_key": "stream:started", "title": "Started", "summary": "Goal",
                              "owner_kind": "operator", "work_state": "paused", "emit_started": True,
                              "visible_chat": {"stream_id": ASSISTANT}}, None, None, "adopt:stream:started")
        lid, version = ctx
        payload = {"lane_blocked": {"to": "blocked", "blocker": "Waiting on keys"},
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
        assert code(e) == "work_lane_operator_confirmation_required"
        bad = [
            env.confirm("q-other-lane", "wl-other", "set_state:done"),
            env.confirm("q-other-action", lid, "set_owner:fd"),
            env.confirm("q-not-yet", lid, "set_state:done", answer="Not yet"),
            env.confirm("q-relay", lid, "set_state:done", actor_class="verified_agent_relay"),
            env.confirm("q-producer", lid, "set_state:done", producer="amaterasu:someone"),
        ]
        for c in bad:
            with pytest.raises(ValueError) as e:
                await env.op("set_state", {"to": "done", "outcome": "x", "operator_confirmation": c},
                             lane=lid, version=1)
            assert code(e) == "work_lane_operator_confirmation_mismatch", c
        # mismatched-target refusals for unguarded targets: test_supplied_confirmation_must_match_operation_qa_f2
        good = env.confirm("q-good", lid, "set_state:done")
        r = await env.op("set_state", {"to": "done", "outcome": "x", "operator_confirmation": good},
                         lane=lid, version=1)
        assert r["lane"]["work_state"] == "done" and r["event"]["consumed_question_id"] == "q-good"
        r = await env.op("set_state", {"to": "paused"}, lane=lid, version=2)
        with pytest.raises(ValueError) as e:
            await env.op("set_state", {"to": "done", "outcome": "y", "operator_confirmation": good},
                         lane=lid, version=3)
        assert code(e) == "work_lane_operator_confirmation_consumed"
        # owner handoff operator -> fd
        with pytest.raises(ValueError) as e:
            await env.op("set_owner", {"to": "fd"}, lane=lid, version=3)
        assert code(e) == "work_lane_operator_confirmation_required"
        wrong = env.confirm("q-owner-wrong", lid, "set_state:done")
        with pytest.raises(ValueError) as e:
            await env.op("set_owner", {"to": "fd", "operator_confirmation": wrong}, lane=lid, version=3)
        assert code(e) == "work_lane_operator_confirmation_mismatch"
        own = env.confirm("q-owner", lid, "set_owner:fd")
        r = await env.op("set_owner", {"to": "fd", "operator_confirmation": own}, lane=lid, version=3)
        assert r["lane"]["owner_kind"] == "fd"
        kinds = [u["raw"]["lane_update"]["kind"] for u in await lane_updates(env.store)]
        assert kinds == ["lane_completed", "major_decision"]
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


@pytest.mark.parametrize("path", ["visibility_hidden", "role_qa", "new_generation", "reconciled_dead"])
def test_each_lead_loss_path_reconciles_once_v4(path):
    async def body(env):
        lead = await env.seat("loss-lead", role="lead", parent_stream_id=FD)
        lid = (await env.adopt(key="stream:loss", lead=lead))["lane"]["lane_id"]
        blk_lead = await env.seat("loss-blk", role="lead", parent_stream_id=FD)
        blk = (await env.adopt(key="stream:loss-blk", state="blocked", lead=blk_lead, blocker="b"))["lane"]
        for name in ("loss-lead", "loss-blk"):
            if path == "visibility_hidden":
                await env.store.update_session("amaterasu", name, visibility="hidden")
            elif path == "role_qa":
                await env.store.update_session("amaterasu", name, role="qa")
            elif path == "new_generation":
                await env.store.mark_closed("amaterasu", name, closed_at="2026-10-07T20:00:00Z",
                                            pane_status="pane_dead")
                await env.store.open_session("amaterasu", name, provider="claude", role="lead",
                                             parent_stream_id=FD)
            else:
                row = await env.store.fetch_session("amaterasu", name)
                gen = lead[1] if name == "loss-lead" else blk_lead[1]
                await env.store.mark_reconciled_dead("amaterasu", name, expected_generation=gen,
                                                     presumed_dead_at="2026-10-07T20:00:00Z",
                                                     closed_at="2026-10-07T20:00:00Z")
                assert row is not None
        rows = {r["lane_id"]: r for r in await env.store.work_lane_rows()}
        assert rows[lid]["_qualifies"] is False and rows[lid]["work_state"] == "active"
        assert [c["lane_id"] for c in await env.store.reconcile_work_lanes()] == [lid]
        assert await env.store.reconcile_work_lanes() == []
        shown = await env.store.get_work_lane(lid)
        assert (shown["lane"]["work_state"], shown["lane"]["work_state_reason"]) == ("paused", "lead_lost")
        assert [e["operation"] for e in shown["events"]].count("lead_lost") == 1
        assert not [e for e in shown["events"] if e.get("update_kind")]  # lead loss posts no update
        blocked = (await env.store.get_work_lane(blk["lane_id"]))["lane"]
        assert (blocked["work_state"], blocked["blocker"]) == ("blocked", "b")
        # a later qualifying set_lead does not resume
        fresh = await env.seat("fresh-lead", role="lead", parent_stream_id=FD)
        r = await env.op("set_lead", {"lead": {"stream_id": fresh[0], "generation": fresh[1]}},
                         lane=lid, version=shown["lane"]["version"])
        assert r["lane"]["work_state"] == "paused"
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
        await env.store.mark_closed("amaterasu", "ho-pred", closed_at="2026-10-07T20:00:00Z",
                                    pane_status="pane_dead", close_kind="handed_off")
        assert await env.store.reconcile_work_lanes() == []
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
            try:  # guard passes; the routing rule then decides
                result = await routing(operation, payload, env.confirmations[ok["question_id"]],
                                       op_id="rt-ok-" + action)
            except ValueError as exc:
                assert not str(exc).startswith("work_lane_operator")
            else:
                assert operation == "lane.decision" and result["lane"]["phase"] == "cancelled"
                consumed = [e for e in (await env.store.get_work_lane(lid))["events"]
                            if e.get("consumed_question_id") == ok["question_id"]]
                assert len(consumed) == 1
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
            await env.op("set_state", {"to": "blocked", "blocker": "b", "operator_confirmation": done_q},
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
        await env.op("set_state", {"to": "done", "outcome": "ok"}, lane=lid, version=1)
        r = await env.op("set_state", {"to": "blocked", "blocker": "again"}, lane=lid, version=2)
        assert r["update"] is None and r["lane"]["work_state_reason"] == "reopened"
        assert r["lane"]["blocker"] == "again"
    run(body)


def test_projection_rechecks_visible_chat_eligibility_qa_f3():
    async def body(env):
        chat = await env.seat("f3-chat")
        await env.adopt(key="stream:f3", state="paused", lead=False, owner="operator",
                        chat={"stream_id": chat[0], "generation": chat[1]})
        assert (await env.store.work_lane_rows())[0]["_chat_available"] == "open"
        await env.store.update_session("amaterasu", "f3-chat", visibility="hidden")
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
