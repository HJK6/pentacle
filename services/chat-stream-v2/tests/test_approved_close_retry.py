"""Approved busy closes reuse the durable ruling release loop and close fences."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions, VerbError
from store import Store


HOST = "retry-host"
ROOT = f"{HOST}:primary"
ADVISOR = f"{HOST}:advisor"
TARGET = f"{HOST}:lane"


class ControlledTmux:
    """Only the external capture/kill counterpart is synthetic."""

    def __init__(self):
        self.alive = True
        self.capture_ok = True
        self.text = "Working (esc to interrupt)\n"
        self.kills = 0

    async def has_session(self, _name):
        return self.alive

    async def pane_pid(self, _name):
        return ""

    async def pane_identity(self, _name):
        return None

    async def capture_checked(self, _name, **_kwargs):
        return self.capture_ok, self.text

    async def kill_session(self, _name):
        self.kills += 1
        self.alive = False


class Journey:
    def __init__(self, store, tmux, config):
        self.store, self.tmux, self.config = store, tmux, config
        self.sessions = Sessions(store, tmux=tmux, local_host=HOST)
        self.server = Server(store=store, sessions=self.sessions, local_host=HOST)
        self.server.assistant_composite = AssistantComposite(store, config=config)
        self.rulings = self.server.lane_rulings
        self.rid = None

    async def auth(self, name):
        row = await self.store.fetch_session(HOST, name)
        return {"token_verified": True, "stream_id": f"{HOST}:{name}",
                "session_generation": row["session_generation"]}

    async def report(self, report_id="terminal-lane"):
        row = await self.store.fetch_session(HOST, "lane")
        def op(conn):
            with conn:
                conn.execute("INSERT INTO v2_reports (report_id,from_stream_id,session_generation,status,"
                             "summary,created_at,ingested_at) VALUES (?,?,?,'done','complete',?,?)",
                             (report_id, TARGET, row["session_generation"], "2026-09-27T00:00:00Z",
                              "2026-09-27T00:00:00Z"))
        await self.store.submit(op)

    async def request(self):
        result = await self.server._on_close({
            "type": "close", "host": HOST, "session_name": "lane",
            "request_id": "close-lane", "reason": "accepted lane finished",
            "defer_if_working": True, "_auth_context": await self.auth("primary"),
        })
        await self.rulings.stop()
        self.rid = result["ruling_request_id"]
        return result

    async def approve(self):
        return await self.server._on_assistant_ruling({
            "ruling_request_id": self.rid, "request_id": "approve-lane", "ruling": "approve",
            "_auth_context": await self.auth("advisor"),
        })

    async def row(self):
        return await self.rulings._fetch(self.rid)


@asynccontextmanager
async def journey(path=":memory:"):
    store = Store(str(path))
    store.start()
    tmux = ControlledTmux()
    env = None
    try:
        root = await store.open_session(HOST, "primary", provider="codex", role="lead",
                                       pane_status="pane_alive", bootstrap_state="ready")
        await store.open_session(HOST, "advisor", provider="codex", pane_status="pane_alive")
        lane = await store.open_session(HOST, "lane", provider="codex", role="lead",
                                       parent_stream_id=ROOT, pane_status="pane_alive")
        config = AssistantCompositeConfig.from_env({
            "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": f"{HOST}:assistant",
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
            "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
        })
        env = Journey(store, tmux, config)
        await env.sessions.refresh()
        await env.rulings._record_ownership({
            "requester_stream_id": ROOT, "requester_generation": root["session_generation"],
            "ruling_request_id": "admit-lane",
        }, TARGET, lane["session_generation"])
        await env.report()
        yield env
    finally:
        if env is not None:
            await env.rulings.stop()
        store.stop()


def test_approved_busy_close_retries_after_idle_exactly_once():
    async def go():
        async with journey() as env:
            await env.request()
            accepted = await env.approve()
            assert accepted["outcome"]["type"] == "close.deferred"
            assert env.tmux.kills == 0
            env.tmux.text = "ready\n"
            await env.rulings.tick()
            target = await env.store.fetch_session(HOST, "lane")
            assert target["status"] == "closed", (await env.row())["state"]
            assert (await env.row())["state"] == "done"
            await asyncio.gather(env.rulings.tick(), env.rulings.tick(), env.approve())
            assert env.tmux.kills == 1
            audits = await env.store.submit(lambda conn: [dict(row) for row in conn.execute(
                "SELECT * FROM v2_close_audit WHERE session_name='lane' ORDER BY rowid")])
            assert [row["disposition"] for row in audits] == ["deferred", "closed"]
            assert {row["request_id"] for row in audits} == {"close-lane"}
            assert {row["closed_by"] for row in audits} == {ROOT}
            assert await env.store.submit(lambda conn: conn.execute(
                "SELECT count(*) FROM v2_outbound_notices WHERE kind='assistant_lane_ruling_result'"
            ).fetchone()[0]) == 1
    asyncio.run(go())


def test_approved_deferred_close_survives_store_and_daemon_restart(tmp_path):
    async def go():
        async with journey(tmp_path / "sessions.db") as env:
            await env.request()
            accepted = await env.approve()
            before = await env.row()
            assert accepted["state"] == "approved"
            env.store.stop()
            restarted_store = Store(str(tmp_path / "sessions.db"))
            restarted_store.start()
            restarted = Journey(restarted_store, env.tmux, env.config)
            restarted.rid = env.rid
            try:
                env.tmux.text = "ready\n"
                await restarted.sessions.refresh()
                await restarted.rulings.start()
                await restarted.rulings.stop()
                after = await restarted.row()
                assert after["state"] == "done"
                for key in ("ruling_request_id", "request_key", "ruling_key", "intent_digest",
                            "target_generation", "requester_generation", "authority_generation"):
                    assert after[key] == before[key]
                await restarted.rulings.tick()
                assert env.tmux.kills == 1
            finally:
                await restarted.rulings.stop()
                restarted_store.stop()
    asyncio.run(go())


def test_duplicate_request_and_ruling_reuse_approved_deferred_intent():
    async def go():
        async with journey() as env:
            first = await env.request()
            await env.approve()
            duplicate = await env.request()
            assert duplicate["ruling_request_id"] == first["ruling_request_id"]
            assert duplicate["state"] == "approved"
            ruled = await env.approve()
            assert ruled["duplicate"] is True
            assert ruled["state"] == "approved" and env.tmux.kills == 0
            counts = await env.store.submit(lambda conn: (
                conn.execute("SELECT count(*) FROM v2_assistant_lane_rulings").fetchone()[0],
                conn.execute("SELECT count(*) FROM v2_assistant_lane_ruling_audit WHERE event='ruling'").fetchone()[0]))
            assert counts == (1, 1)
            env.tmux.text = "ready\n"
            await asyncio.gather(env.rulings.tick(), env.rulings.tick(), env.approve())
            assert env.tmux.kills == 1 and (await env.row())["state"] == "done"
            assert (await env.request())["type"] == "close.ok"
            assert env.tmux.kills == 1
    asyncio.run(go())


@pytest.mark.parametrize("capture_ok,text", [
    (True, "Working (esc to interrupt)\n"),
    (False, "ready\n"),
    (True, ""),
])
def test_busy_or_unknown_capture_stays_actionable_without_killing(capture_ok, text):
    async def go():
        async with journey() as env:
            env.tmux.capture_ok, env.tmux.text = capture_ok, text
            await env.request()
            await env.approve()
            await env.rulings.tick()
            assert env.tmux.kills == 0
            assert (await env.row())["state"] == "approved"
            assert json.loads((await env.row())["outcome_json"])["type"] == "close.deferred"
            env.tmux.capture_ok, env.tmux.text = True, "ready\n"
            await env.rulings.tick()
            assert env.tmux.kills == 1
    asyncio.run(go())


@pytest.mark.parametrize("change", ["generation", "reopened", "missing_report", "changed_report",
                                  "requester_generation", "unauthorized", "protected", "live_children"])
def test_deferred_retry_preserves_definitive_close_fences(change, tmp_path):
    async def go():
        async with journey() as env:
            await env.request()
            await env.approve()
            original = await env.row()
            expected_state, expected_error = "approved_but_not_closed", ""
            if change == "generation":
                await env.store.open_session(HOST, "lane", session_generation="replacement",
                                             parent_stream_id=ROOT)
                expected_error = "target_generation_changed"
            elif change == "reopened":
                await env.store.mark_closed(HOST, "lane", closed_at="2026-09-27T00:00:00Z",
                                            pane_status="pane_dead")
                await env.store.open_session(HOST, "lane", parent_stream_id=ROOT, pane_status="pane_alive")
                expected_error = "target_generation_changed"
            elif change == "missing_report":
                await env.store.submit(lambda conn: conn.execute("DELETE FROM v2_reports"))
                expected_error = "report_fence_moved"
            elif change == "changed_report":
                await env.report("replacement-report")
                expected_error = "report_fence_moved"
            elif change == "requester_generation":
                await env.store.open_session(HOST, "primary", session_generation="replacement")
                expected_state, expected_error = "release_blocked", "requester_generation_changed"
            elif change == "unauthorized":
                await env.store.update_session(HOST, "lane", parent_stream_id=f"{HOST}:other")
                expected_error = "close requires a verified self, direct-parent, or authenticated operator"
            elif change == "protected":
                env.sessions.assistant.role = "lead"
                expected_error = "assistant session is protected"
            elif change == "live_children":
                # Manager policy has the live-child refusal. Parent policy is
                # unchanged; reparenting forces ordinary manager admission.
                await env.store.update_session(HOST, "lane", parent_stream_id=None)
                await env.store.open_session(HOST, "child", parent_stream_id=TARGET)
                from test_consent import Ceremony
                ceremony = Ceremony(env, tmp_path)
                await ceremony.enroll()
                pending = await ceremony.call("consent.request",
                    action="lifecycle.designate", target_stream_id=ROOT,
                    target_generation=original["requester_generation"], expected_revision=0,
                    reason="test manager designation")
                opened = await ceremony.call("consent.open", intent_id=pending["intent"]["request_id"], key_id=ceremony.key_id)
                await ceremony.approve(opened["challenge"])
                expected_error = "close_live_children"
            env.tmux.text = "ready\n"
            await env.sessions.refresh()
            await env.rulings.tick()
            row = await env.row()
            assert row["state"] == expected_state
            assert expected_error in json.loads(row["outcome_json"])["error"]
            assert env.tmux.kills == 0
            await env.rulings.tick()
            assert env.tmux.kills == 0
    asyncio.run(go())


def test_missing_report_or_unauthorized_requester_cannot_request_close():
    async def go():
        async with journey() as env:
            with pytest.raises(VerbError, match="close requires"):
                await env.server._on_close({"host": HOST, "session_name": "lane", "request_id": "forged"})
            await env.store.submit(lambda conn: conn.execute("DELETE FROM v2_reports"))
            with pytest.raises(VerbError, match="assistant_lane_close_report_required"):
                await env.request()
            assert env.tmux.kills == 0
            assert await env.store.submit(lambda conn: conn.execute(
                "SELECT count(*) FROM v2_assistant_lane_rulings").fetchone()[0]) == 0
    asyncio.run(go())


def test_existing_unruled_deadline_release_also_preserves_safe_deferral(monkeypatch):
    monkeypatch.delenv("PENTACLE_RULING_SLA_S", raising=False)
    async def go():
        async with journey() as env:
            assert env.rulings.sla_s == 600
            await env.request()
            def expire(conn):
                with conn:
                    conn.execute("UPDATE v2_assistant_lane_rulings SET deadline=0 WHERE ruling_request_id=?",
                                 (env.rid,))
            await env.store.submit(expire)
            await env.rulings.tick()
            row = await env.row()
            assert row["state"] == "unruled" and row["reason"] == "deadline"
            assert row["ruling"] is None and row["mirror_sent"] == 1
            assert env.tmux.kills == 0
            env.tmux.text = "ready\n"
            await env.rulings.tick()
            assert (await env.row())["state"] == "done" and env.tmux.kills == 1
    asyncio.run(go())
