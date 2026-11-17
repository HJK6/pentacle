"""Advisor request contracts, including the unchanged bound-seat mode."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from store import Store
from test_assistant_lane_rulings import _rulings_rig, _spawn_msg, ROOT, ADVISOR

pytestmark = pytest.mark.usefixtures("ruling_spec_root")


def test_advice_answer_round_trip_is_scoped_and_idempotent():
    async def go():
        store = Store(":memory:")
        store.start()
        server = None
        try:
            root, advisor, server, spawned = await _rulings_rig(store)
            rulings = server.lane_rulings
            requester = dict(actor_stream_id=ROOT, actor_generation=root["session_generation"])
            authority = dict(actor_stream_id=ADVISOR, actor_generation=advisor["session_generation"])
            msg = {"request_id": "gate-1", "work_item": "spec_demo__linked", "body": "GATE inspect scope"}
            reply = await rulings.advise(msg, **requester)
            assert await rulings.advise(msg, **requester) == reply
            rid = reply["ruling_request_id"]
            row = await rulings._fetch(rid)
            assert row["covered"] == 1 and row["work_item"] == msg["work_item"]
            assert "assistant answer" in json.loads(row["intent_json"])["_ruling_packet"]
            with pytest.raises(ValueError, match="requires_answer"):
                await rulings.ruling({"ruling_request_id": rid, "request_id": "wrong", "ruling": "approve"}, **authority)
            answer = {"ruling_request_id": rid, "request_id": "answer-1", "body": "Proceed within scope."}
            with pytest.raises(ValueError, match="authority_unverified"):
                await rulings.answer(answer, **requester)
            with pytest.raises(ValueError, match="too_long"):
                await rulings.answer({**answer, "body": "é" * 3001}, **authority)
            assert (await rulings.answer(answer, **authority))["state"] == "answered"
            assert (await rulings.answer(answer, **authority))["duplicate"]
            await rulings.tick()
            notices = await store.submit(lambda c: [dict(r) for r in c.execute(
                "SELECT * FROM v2_outbound_notices WHERE notice_id=?", ("advisor-answer:" + rid,))])
            assert len(notices) == 1
            assert notices[0]["recipient_stream_id"] == ROOT
            assert notices[0]["body"].startswith("[Advisor answer]")
            assert json.loads(notices[0]["body"].split("] ", 1)[1])["body"] == answer["body"]
            assert spawned.calls == []
            assert (await rulings._fetch(rid))["answer"] == answer["body"]
            spawn = await server._on_spawn(_spawn_msg(root, "request-spawn"))
            with pytest.raises(ValueError, match="advice_required"):
                await rulings.answer({**answer, "ruling_request_id": spawn["ruling_request_id"]}, **authority)
        finally:
            if server:
                await server.lane_rulings.stop()
            store.stop()
    asyncio.run(go())


@asynccontextmanager
async def session_rig(monkeypatch, kinds=("advice",)):
    from advisor_sessions import KINDS_KEY, SHAPE_KEY
    store = Store(":memory:")
    store.start()
    root, advisor, server, spawned = await _rulings_rig(store)
    server.lane_rulings._ensure_worker = lambda: None
    server.comms = SimpleNamespace(guidelines=SimpleNamespace(load=lambda role: ("guideline-sha", "Current advisor guidelines.")))
    outcomes, calls, closed = {}, [], []

    async def spawn(msg, host):
        calls.append(msg)
        await store.open_session(host, msg["session_name"], provider=msg["provider"],
            session_generation=msg["_session_generation"], pane_status="pane_alive", bootstrap_state="ready",
            parent_stream_id=msg["parent_stream_id"], visibility="hidden")
        outcomes[msg["request_id"]] = {"state": "delivered", "request_id": msg["request_id"],
                                     "stream_id": host + ":" + msg["session_name"]}
        return {"type": "spawn.ok"}

    async def status(msg, host):
        return {"outcomes": [outcomes[msg["target"]]] if msg["target"] in outcomes else [], "reservations": []}

    async def close(host, name, **kwargs):
        seat = await store.fetch_session(host, name)
        assert kwargs["expected_generation"] == seat["session_generation"]
        closed.append(host + ":" + name)
        def op(c):
            with c:
                c.execute("UPDATE sessions SET status='closed' WHERE host=? AND session_name=?", (host, name))
        await store.submit(op)
        return {"closed": True}

    server.spawnctl = SimpleNamespace(spawn=spawn, spawn_status=status)
    server.sessions.close = close
    await store.put(KINDS_KEY, json.dumps(list(kinds)))
    await store.put(SHAPE_KEY, json.dumps({"provider": "codex", "model": "gpt-6-astra", "effort": "high", "account": "test"}))
    r = server.lane_rulings

    async def request(key, item="", action="advice", covered=True):
        return await r._create(action=action, key=key, intent={"body": key}, requester=ROOT,
            requester_generation=root["session_generation"], binding=await r.request_binding(action),
            target="", target_generation="", covered=covered, work_item={"spec_id": item} if item else None)

    async def tick():
        await r.tick()
        tasks = list(r.advisor_sessions.tasks.values())
        if tasks:
            await asyncio.gather(*tasks)

    async def answer(row, body="Approved scope"):
        current = await r._fetch(row["ruling_request_id"])
        return await r.answer({"ruling_request_id": current["ruling_request_id"], "request_id": "answer", "body": body},
            actor_stream_id=current["authority_stream_id"], actor_generation=current["authority_generation"])

    try:
        yield SimpleNamespace(**locals())
    finally:
        await r.stop()
        store.stop()


@pytest.mark.parametrize("binding", ["", "disabled", ROOT, "missing:advisor", "sessions"])
def test_session_request_creation_ignores_bound_seat_except_disabled(monkeypatch, binding):
    async def go():
        async with session_rig(monkeypatch) as h:
            await h.store.put("assistant.authority.stream_id", binding)
            if binding == "disabled":
                with pytest.raises(ValueError, match="disabled"):
                    await h.request("req")
                return
            row = await h.request("req")
            assert row["state"] == "pending" and not row["authority_stream_id"]
            await h.tick()
            row = await h.r._fetch(row["ruling_request_id"])
            assert await h.r.advisor_sessions.owns(row)
            assert row["authority_stream_id"] != ADVISOR
    asyncio.run(go())


def test_one_input_thread_serialization_and_terminal_cleanup(monkeypatch):
    async def go():
        async with session_rig(monkeypatch) as h:
            first = await h.request("first", "spec_demo__linked")
            second = await h.request("second", "spec_demo__linked")
            await h.tick()
            assert len(h.calls) == 1
            prompt = h.calls[0]["initial_prompt"]
            assert prompt.count("Current advisor guidelines.") == 1
            assert prompt.index("Skip shared startup") < prompt.index("Current advisor") < prompt.index("[Thread record]") < prompt.index("[Assistant lane ruling request]")
            assert h.calls[0]["no_watch"] and h.calls[0]["visibility"] == "hidden"
            assert h.calls[0].get("role") is None
            first_live = await h.r._fetch(first["ruling_request_id"])
            await h.r._enqueue_notice(first_live)
            await h.store.submit(lambda c: c.execute("UPDATE v2_assistant_lane_rulings SET deadline=0 WHERE ruling_request_id=?", (first["ruling_request_id"],)))
            await h.tick()
            notices = await h.store.submit(lambda c: [dict(r) for r in c.execute(
                "SELECT * FROM v2_outbound_notices WHERE recipient_stream_id=?", (first_live["authority_stream_id"],))])
            assert notices == []
            await h.answer(first, "First decision")
            await h.tick()
            assert h.closed == [first_live["authority_stream_id"]]
            assert len(h.calls) == 2 and "First decision" in h.calls[1]["initial_prompt"]
            assert (await h.r._fetch(second["ruling_request_id"]))["authority_stream_id"]
    asyncio.run(go())


def test_four_slots_and_noncovered_priority(monkeypatch):
    async def go():
        async with session_rig(monkeypatch, ("advice", "session_close")) as h:
            active = [await h.request(str(i)) for i in range(4)]
            await h.tick()
            waiting = await h.request("covered-waiting")
            closing = await h.request("close-waiting", action="session_close", covered=False)
            await h.tick()
            assert len(h.calls) == 4
            await h.answer(active[0])
            await h.tick()
            assert len(h.calls) == 5
            assert closing["ruling_request_id"] in h.calls[-1]["initial_prompt"]
            assert not (await h.r._fetch(waiting["ruling_request_id"]))["authority_stream_id"]
    asyncio.run(go())


def test_covered_failures_replace_once_then_retry_after_900_seconds(monkeypatch):
    async def go():
        async with session_rig(monkeypatch) as h:
            row = await h.request("failures")
            rid = row["ruling_request_id"]
            await h.tick()
            for count in (1, 2):
                await h.store.submit(lambda c: c.execute("UPDATE v2_advisor_sessions SET ready_at=0 WHERE ended_at IS NULL"))
                await h.tick()
                assert len(h.calls) == 2
            current = await h.r._fetch(rid)
            assert current["state"] == "pending" and not current["authority_stream_id"]
            await h.tick()
            notices = await h.store.submit(lambda c: c.execute("SELECT count(*) FROM v2_outbound_notices WHERE notice_id LIKE 'advisor-session-failed:%'").fetchone()[0])
            assert notices == 1  # requester is the front desk
            await h.store.submit(lambda c: c.execute("UPDATE v2_advisor_sessions SET ended_at=ended_at-901 WHERE ruling_request_id=?", (rid,)))
            await h.tick()
            assert len(h.calls) == 3
    asyncio.run(go())


def test_thread_record_has_latest_eight_in_order_and_byte_cap(monkeypatch):
    async def go():
        async with session_rig(monkeypatch) as h:
            for i in range(10):
                row = await h.request("history-" + str(i), "spec_demo__linked")
                await h.store.submit(lambda c: c.execute("UPDATE v2_assistant_lane_rulings SET state='answered',answer=? WHERE ruling_request_id=?",
                    ("reply-" + str(i) + "é" * 3000, row["ruling_request_id"])))
            other = await h.request("foreign", "spec_other__work")
            await h.store.submit(lambda c: c.execute("UPDATE v2_assistant_lane_rulings SET state='answered',answer='foreign' WHERE ruling_request_id=?", (other["ruling_request_id"],)))
            current = await h.request("current", "spec_demo__linked")
            text = await h.r.advisor_sessions.thread(current)
            entries = [json.loads(line) for line in text.splitlines()]
            assert len(text.encode()) <= 12000 and 1 <= len(entries) <= 8
            assert "foreign" not in text and "history-0" not in text and "history-1" not in text
            assert entries[-1]["outcome"]["answer"].startswith("reply-9")
            assert [e["time"] for e in entries] == sorted(e["time"] for e in entries)
    asyncio.run(go())


def test_advice_waits_without_authority_and_cannot_be_released_disabled():
    async def go():
        store = Store(":memory:")
        store.start()
        server = None
        try:
            root, advisor, server, spawned = await _rulings_rig(store, authority="")
            rulings = server.lane_rulings
            requester = dict(actor_stream_id=ROOT, actor_generation=root["session_generation"])
            with pytest.raises(ValueError):
                await rulings.advise({"work_item": "spec_invalid", "body": "GATE"}, **requester)
            reply = await rulings.advise({"request_id": "unbound", "work_item": "none", "body": "GATE"}, **requester)
            rid = reply["ruling_request_id"]
            await store.submit(lambda c: c.execute("UPDATE v2_assistant_lane_rulings SET deadline=0 WHERE ruling_request_id=?", (rid,)))
            await rulings.tick()
            assert (await rulings._fetch(rid))["state"] == "pending"
            await store.put("assistant.authority.stream_id", "disabled")
            assert await rulings._disable_pending() == []
            with pytest.raises(ValueError, match="assistant_authority_disabled"):
                await rulings.advise({"work_item": "none", "body": "GATE"}, **requester)
            assert (await rulings._fetch(rid))["state"] == "pending"
            assert spawned.calls == []
        finally:
            if server:
                await server.lane_rulings.stop()
            store.stop()
    asyncio.run(go())


@pytest.mark.parametrize("remaining,replacement", [(299, False), (600, True)])
def test_noncovered_failure_budget_and_deadline(monkeypatch, remaining, replacement):
    import time
    async def go():
        async with session_rig(monkeypatch, ("session_close",)) as h:
            row = await h.request("close", action="session_close", covered=False)
            rid = row["ruling_request_id"]
            await h.tick()
            def fail(c):
                with c:
                    c.execute("UPDATE v2_advisor_sessions SET ready_at=0 WHERE ended_at IS NULL")
                    c.execute("UPDATE v2_assistant_lane_rulings SET deadline=? WHERE ruling_request_id=?", (time.time() + remaining, rid))
            await h.store.submit(fail)
            await h.tick()
            assert len(h.calls) == (2 if replacement else 1)
            if replacement:
                await h.store.submit(fail)
                await h.tick()
            latest = await h.r._fetch(rid)
            assert latest["state"] != "pending" and latest["reason"] == "authority_unavailable"
            assert (await h.r.advisor_sessions.status())["running_sessions"] == 0
    asyncio.run(go())


def test_configure_keeps_active_session_and_rollback_reissues_both_row_classes(monkeypatch):
    async def go():
        async with session_rig(monkeypatch, ("advice", "session_close")) as h:
            covered = await h.request("advice")
            close = await h.request("close", action="session_close", covered=False)
            await h.tick()
            before = await h.r._fetch(covered["ruling_request_id"])
            actor = dict(actor_stream_id=ROOT, actor_generation=h.root["session_generation"])
            await h.r.configure(ADVISOR, brief_file=__file__, **actor)
            assert (await h.r._fetch(covered["ruling_request_id"]))["authority_stream_id"] == before["authority_stream_id"]
            await h.r.configure_sessions("none", **actor)
            assert len(h.closed) == 2
            for row in (covered, close):
                current = await h.r._fetch(row["ruling_request_id"])
                assert current["authority_stream_id"] == ADVISOR
                await h.r._enqueue_notice(current)
                notices = await h.store.submit(lambda c: [dict(r) for r in c.execute(
                    "SELECT * FROM v2_outbound_notices WHERE kind='assistant_lane_ruling_request' AND recipient_stream_id=? AND json_extract(metadata,'$.ruling_request_id')=?",
                    (ADVISOR, row["ruling_request_id"]))])
                assert len(notices) == 1
                assert notices[0]["notice_id"].endswith(h.advisor["session_generation"])
    asyncio.run(go())


def test_sessions_binding_requires_all_actions_and_clears_old_pending_rows(monkeypatch):
    from assistant_lane_rulings import _ACTION_KINDS
    from advisor_sessions import KINDS_KEY
    async def go():
        async with session_rig(monkeypatch, ()) as h:
            actor = dict(actor_stream_id=ROOT, actor_generation=h.root["session_generation"])
            old = await h.request("old")
            assert old["authority_stream_id"] == ADVISOR
            with pytest.raises(ValueError, match="all_session_kinds_required"):
                await h.r.configure("sessions", **actor)
            await h.r.configure_sessions(",".join(sorted(_ACTION_KINDS)), **actor)
            assert (await h.r._fetch(old["ruling_request_id"]))["authority_stream_id"] == ADVISOR
            await h.r.configure("sessions", **actor)
            assert not (await h.r._fetch(old["ruling_request_id"]))["authority_stream_id"]
            with pytest.raises(ValueError, match="seat_required"):
                await h.r.configure_sessions("none", **actor)
            await h.r._check_authority_loss()
            assert await h.store.get("assistant.authority.loss_notice") is None
            assert (await h.r.binding())["state"] == "sessions"
    asyncio.run(go())


def test_session_shape_is_seeded_once_and_not_overwritten_on_rebind(monkeypatch):
    from advisor_sessions import SHAPE_KEY, KINDS_KEY
    async def go():
        async with session_rig(monkeypatch, ()) as h:
            await h.store.put(SHAPE_KEY, "")
            await h.store.put("assistant.authority.shape", json.dumps({"provider": "codex", "model": "gpt-6-astra",
                "effort": "high", "account_observed": {"alias": "first"}}))
            actor = dict(actor_stream_id=ROOT, actor_generation=h.root["session_generation"])
            await h.r.configure_sessions("advice", **actor)
            before = await h.store.get(SHAPE_KEY)
            await h.r.configure(ADVISOR, brief_file=__file__, **actor)
            assert await h.store.get(SHAPE_KEY) == before
            await h.r.configure_sessions("advice", account="second", **actor)
            shape = json.loads(await h.store.get(SHAPE_KEY))
            assert shape == {"provider": "codex", "model": "gpt-6-astra", "effort": "high", "account": "second"}
    asyncio.run(go())


def test_service_account_choice_is_bound_to_recorded_session(monkeypatch):
    from account_runtime import AccountRuntime
    async def go():
        async with session_rig(monkeypatch) as h:
            await h.request("account")
            await h.tick()
            message = h.calls[0]
            runtime = SimpleNamespace(store=h.store, server=h.server)
            assert await AccountRuntime._carried_choice(runtime, message, message["_auth_context"])
            assert not await AccountRuntime._carried_choice(runtime, {**message, "account": "different"}, message["_auth_context"])
            assert not await AccountRuntime._carried_choice(runtime, {**message, "_session_generation": "wrong"}, message["_auth_context"])
            protected = await h.store.submit(lambda c: AccountRuntime._cap_exemptions_conn(runtime, c))
            assert (message["host"] + ":" + message["session_name"], message["_session_generation"]) in protected
    asyncio.run(go())


@pytest.mark.parametrize("failure", ["spawn_refused", "seat_lost"])
def test_spawn_refusal_and_lost_seat_get_one_replacement(monkeypatch, failure):
    async def go():
        async with session_rig(monkeypatch) as h:
            row = await h.request("failure-path")
            original_spawn = h.server.spawnctl.spawn
            if failure == "spawn_refused":
                async def refused(msg, host):
                    h.outcomes[msg["request_id"]] = {"state": "failed", "request_id": msg["request_id"]}
                    return {"type": "spawn.error"}
                h.server.spawnctl.spawn = refused
            await h.tick()
            if failure == "seat_lost":
                first = await h.r._fetch(row["ruling_request_id"])
                await h.store.update_session(*first["authority_stream_id"].split(":", 1), pane_status="pane_dead")
            h.server.spawnctl.spawn = original_spawn
            await h.tick()
            attempts = await h.store.submit(lambda c: [dict(r) for r in c.execute("SELECT * FROM v2_advisor_sessions ORDER BY attempt")])
            assert len(attempts) == 2 and attempts[0]["end_reason"] == failure
            assert attempts[0]["ended_at"] is not None and attempts[1]["ended_at"] is None
    asyncio.run(go())


def test_restart_adopts_existing_attempt_and_no_guidelines_notice(monkeypatch):
    from advisor_sessions import AdvisorSessions
    from assistant_guidelines import AssistantGuidelines
    async def go():
        async with session_rig(monkeypatch) as h:
            row = await h.request("restart")
            await h.tick()
            await h.r.advisor_sessions.stop()
            h.r.advisor_sessions = AdvisorSessions(h.r)
            await h.tick()
            assert len(h.calls) == 1
            current = await h.r._fetch(row["ruling_request_id"])
            guidelines = AssistantGuidelines(h.store, h.server.assistant_composite, h.r, h.server.comms,
                env={"PENTACLE_ASSISTANT_GUIDELINES_ENABLED": "1"})
            assert await guidelines.advisor_for_target(current["authority_stream_id"]) is None
            with pytest.raises(ValueError, match="authority_unverified"):
                await h.r.answer({"ruling_request_id": row["ruling_request_id"], "request_id": "wrong", "body": "No"},
                    actor_stream_id=ADVISOR, actor_generation=h.advisor["session_generation"])
    asyncio.run(go())


def test_transfer_pause_retains_waiting_work_until_published(monkeypatch):
    async def go():
        async with session_rig(monkeypatch) as h:
            row = await h.request("transfer")
            await h.tick()
            def claim(c):
                with c:
                    c.execute("INSERT INTO v2_host_transfers VALUES('transfer','fixture','input','dispatch','digest','preparing',9999999999,'{}')")
            await h.store.submit(claim)
            assert await h.r.advisor_sessions.close_request(row["ruling_request_id"], "host_transfer")
            await h.tick()
            assert len(h.calls) == 1
            await h.store.submit(lambda c: c.execute("UPDATE v2_host_transfers SET state='committed'"))
            await h.tick()
            assert len(h.calls) == 1
            await h.store.submit(lambda c: c.execute("UPDATE v2_host_transfers SET state='complete'"))
            await h.tick()
            assert len(h.calls) == 2
    asyncio.run(go())
