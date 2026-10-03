"""Bart lane admission requires an independent authority request."""

import asyncio
import json

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import Store
from sessions import VerbError
from spawnctl import SpawnCtl


ROOT = "fixture-root:primary"
ADVISOR = "fixture-advisor:astra"


class RecordingSpawn:
    def __init__(self):
        self.calls = []

    async def spawn(self, msg, host):
        self.calls.append((msg, host))
        return {"type": "spawn.ok", "stream_id": f"{host}:created"}


class PaneTmux:
    def __init__(self):
        self.live = set()
        self.created = 0

    async def has_session(self, name):
        return name in self.live

    async def new_session(self, name, _command, cwd=None, env=None):
        self.live.add(name)
        self.created += 1

    async def capture(self, _name):
        return "READY"

    async def pane_pid(self, _name):
        return "1234"

    async def session_state(self, name):
        return "alive" if name in self.live else "gone"

    async def kill_session(self, name):
        self.live.discard(name)


def test_bart_lead_spawn_waits_for_independent_ruling_before_spawning():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            advisor = await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            sessions = Sessions(store, tmux=None, local_host="fixture-chat")
            spawned = RecordingSpawn()
            server = Server(store=store, sessions=sessions, spawnctl=spawned, local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)
            reply = await server._on_spawn({
                "type": "spawn", "host": "fixture-chat", "session_name": "new-lead",
                "role": "lead", "request_id": "first-lead", "idempotency_key": "first-lead",
                "objective": "Implement an approved lane",
                "_auth_context": {"token_verified": True, "stream_id": ROOT,
                                  "session_generation": root["session_generation"]},
            })
            assert reply["type"] == "spawn.pending_ruling"
            assert reply["ruling_request_id"]
            assert spawned.calls == []
            assert await store.fetch_session("fixture-chat", "new-lead") is None
            assert advisor["session_generation"]
        finally:
            store.stop()

    asyncio.run(go())


def test_two_requests_are_independent_and_wrong_generation_cannot_rule():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            advisor = await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            sessions = Sessions(store, tmux=None, local_host="fixture-chat")
            spawned = RecordingSpawn()
            server = Server(store=store, sessions=sessions, spawnctl=spawned, local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)

            def request(name):
                return {"type": "spawn", "host": "fixture-chat", "session_name": name,
                        "role": "lead", "request_id": name, "idempotency_key": name,
                        "objective": "Implement an approved lane",
                        "_auth_context": {"token_verified": True, "stream_id": ROOT,
                                          "session_generation": root["session_generation"]}}

            one, two = await asyncio.gather(server._on_spawn(request("one")), server._on_spawn(request("two")))
            assert one["ruling_request_id"] != two["ruling_request_id"]
            assert spawned.calls == []
            with pytest.raises(VerbError, match="assistant_ruling_authority_unverified"):
                await server._on_assistant_ruling({
                    "ruling_request_id": one["ruling_request_id"], "request_id": "forged",
                    "ruling": "approve", "_auth_context": {"token_verified": True,
                    "stream_id": ADVISOR, "session_generation": "retired"},
                })
            approved = await server._on_assistant_ruling({
                "ruling_request_id": one["ruling_request_id"], "request_id": "approve-one",
                "ruling": "approve", "_auth_context": {"token_verified": True,
                "stream_id": ADVISOR, "session_generation": advisor["session_generation"]},
            })
            assert approved["type"] == "assistant.ruling.ok"
            assert len(spawned.calls) == 1 and spawned.calls[0][0]["session_name"] == "one"
            denied = await server._on_assistant_ruling({
                "ruling_request_id": two["ruling_request_id"], "request_id": "deny-two",
                "ruling": "deny", "reason": "Poor acceptance",
                "_auth_context": {"token_verified": True,
                "stream_id": ADVISOR, "session_generation": advisor["session_generation"]},
            })
            assert denied["state"] == "denied"
            assert len(spawned.calls) == 1
            with pytest.raises(VerbError, match="assistant_ruling_conflict"):
                await server._on_assistant_ruling({
                    "ruling_request_id": two["ruling_request_id"], "request_id": "deny-two",
                    "ruling": "approve", "_auth_context": {"token_verified": True,
                    "stream_id": ADVISOR, "session_generation": advisor["session_generation"]},
                })
        finally:
            store.stop()

    asyncio.run(go())


def test_explicit_disable_beats_env_and_releases_pending_once():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            spawned = RecordingSpawn()
            server = Server(store=store, sessions=Sessions(store, tmux=None, local_host="fixture-chat"),
                            spawnctl=spawned, local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)
            auth = {"token_verified": True, "stream_id": ROOT,
                    "session_generation": root["session_generation"]}
            pending = await server._on_spawn({"type": "spawn", "role": "lead", "session_name": "held",
                "request_id": "held", "idempotency_key": "held", "objective": "Implement lane",
                "_auth_context": auth})
            assert pending["type"] == "spawn.pending_ruling"
            disabled = await server._on_assistant_authority({
                "action": "set", "value": "disabled", "_auth_context": auth,
            })
            assert disabled["state"] == "disabled" and disabled["source"] == "kv"
            assert len(spawned.calls) == 1
            assert (await server.lane_rulings._fetch(pending["ruling_request_id"]))["state"] == "done"
            second = await server._on_spawn({"type": "spawn", "role": "lead", "session_name": "immediate",
                "request_id": "immediate", "idempotency_key": "immediate", "objective": "Implement lane",
                "_auth_context": auth})
            assert second["type"] == "spawn.ok"
            assert len(spawned.calls) == 2
        finally:
            store.stop()

    asyncio.run(go())


def test_dead_advisor_proceeds_unruled_and_mirrors_once():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_dead")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            spawned = RecordingSpawn()
            server = Server(store=store, sessions=Sessions(store, tmux=None, local_host="fixture-chat"),
                            spawnctl=spawned, local_host="fixture-chat")
            composite = AssistantComposite(store, config=config)
            server.assistant_composite = composite
            await composite.ensure_projection()
            msg = {"type": "spawn", "role": "lead", "session_name": "unruled",
                   "request_id": "unruled", "idempotency_key": "unruled", "objective": "Implement lane",
                   "_auth_context": {"token_verified": True, "stream_id": ROOT,
                                     "session_generation": root["session_generation"]}}
            first = await server._on_spawn(msg)
            assert first["type"] == "spawn.ok" and first["unruled"] is True
            retry = await server._on_spawn(msg)
            assert retry["ruling_request_id"] == first["ruling_request_id"]
            assert len(spawned.calls) == 1
            events = await store.fetch_session_event_tail("fixture-chat:assistant", limit=20)
            assert len([e for e in events if "proceeded unruled" in str(e.get("text") or "")]) == 1
        finally:
            store.stop()

    asyncio.run(go())


def test_lead_owned_worker_is_never_held():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            lead = await store.open_session("fixture-lead", "owned", provider="codex", parent_stream_id=ROOT)
            await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            spawned = RecordingSpawn()
            server = Server(store=store, sessions=Sessions(store, tmux=None, local_host="fixture-chat"),
                            spawnctl=spawned, local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)
            reply = await server._on_spawn({
                "type": "spawn", "role": "qa", "session_name": "worker", "request_id": "worker",
                "idempotency_key": "worker", "objective": "Review implementation",
                "parent_stream_id": "fixture-lead:owned",
                "_auth_context": {"token_verified": True, "stream_id": "fixture-lead:owned",
                                  "session_generation": lead["session_generation"]},
            })
            assert reply["type"] == "spawn.ok" and len(spawned.calls) == 1
        finally:
            store.stop()

    asyncio.run(go())


def test_reported_owned_lane_close_waits_and_deny_keeps_it_open():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            advisor = await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            lane = await store.open_session("fixture-chat", "lead", provider="codex", role="lead",
                                            parent_stream_id=ROOT, pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            sessions = Sessions(store, tmux=None, local_host="fixture-chat")
            server = Server(store=store, sessions=sessions, local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)
            await server.lane_rulings._record_ownership({
                "requester_stream_id": ROOT, "requester_generation": root["session_generation"],
                "ruling_request_id": "admitted-lane",
            }, "fixture-chat:lead", lane["session_generation"])
            async def terminal_report(*_args, **_kwargs):
                return {"report_id": "terminal-lane", "summary": "Implemented and QA accepted",
                        "qa_verdict": "accept", "next_action": "close"}
            store.find_report = terminal_report
            closed = []
            async def close(*args, **kwargs):
                closed.append((args, kwargs))
                return {"already_closed": False, "session": lane, "reap_status": "done"}
            sessions.close = close
            auth = {"token_verified": True, "stream_id": ROOT,
                    "session_generation": root["session_generation"]}
            first = await server._on_close({"type": "close", "host": "fixture-chat",
                "session_name": "lead", "request_id": "close-first", "_auth_context": auth})
            assert first["type"] == "close.pending_ruling" and closed == []
            close_intent = json.loads((await server.lane_rulings._fetch(first["ruling_request_id"]))["intent_json"])
            assert len(close_intent["_ruling_report"]["digest"]) == 64
            notice = await store.submit(lambda conn: conn.execute(
                "SELECT body FROM v2_outbound_notices WHERE notice_id=?",
                ("assistant-lane-ruling:" + first["ruling_request_id"],)).fetchone()[0])
            assert close_intent["_ruling_report"]["digest"] in notice
            deny = await server._on_assistant_ruling({"request_id": "deny-close",
                "ruling_request_id": first["ruling_request_id"], "ruling": "deny",
                "reason": "Residual gap", "_auth_context": {"token_verified": True,
                    "stream_id": ADVISOR, "session_generation": advisor["session_generation"]}})
            assert deny["state"] == "denied" and closed == []
            second = await server._on_close({"type": "close", "host": "fixture-chat",
                "session_name": "lead", "request_id": "close-second", "_auth_context": auth})
            assert second["type"] == "close.pending_ruling"
            approve = await server._on_assistant_ruling({"request_id": "approve-close",
                "ruling_request_id": second["ruling_request_id"], "ruling": "approve",
                "_auth_context": {"token_verified": True,
                    "stream_id": ADVISOR, "session_generation": advisor["session_generation"]}})
            assert approve["state"] == "done" and len(closed) == 1
        finally:
            store.stop()

    asyncio.run(go())


def test_retry_with_new_rpc_id_reuses_one_pending_intent_and_notice():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            spawned = RecordingSpawn()
            server = Server(store=store, sessions=Sessions(store, tmux=None, local_host="fixture-chat"),
                            spawnctl=spawned, local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)
            base = {"type": "spawn", "role": "lead", "idempotency_key": "stable-key",
                    "objective": "Ship this lane", "_auth_context": {"token_verified": True,
                    "stream_id": ROOT, "session_generation": root["session_generation"]}}
            first = await server._on_spawn({**base, "request_id": "rpc-one"})
            second = await server._on_spawn({**base, "request_id": "rpc-two"})
            assert first["ruling_request_id"] == second["ruling_request_id"]
            assert first["stream_id"] == second["stream_id"]
            assert spawned.calls == []
            await server.lane_rulings.tick()
            notices = await store.submit(lambda conn: conn.execute(
                "SELECT count(*) FROM v2_outbound_notices WHERE kind='assistant_lane_ruling_request'"
            ).fetchone()[0])
            assert notices == 1
        finally:
            store.stop()
    asyncio.run(go())


def test_deadline_wins_over_late_ruling_and_audits_refusal():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            advisor = await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            spawned = RecordingSpawn()
            server = Server(store=store, sessions=Sessions(store, tmux=None, local_host="fixture-chat"),
                            spawnctl=spawned, local_host="fixture-chat")
            composite = AssistantComposite(store, config=config)
            server.assistant_composite = composite
            await composite.ensure_projection()
            first = await server._on_spawn({"type": "spawn", "role": "lead", "request_id": "deadline",
                "idempotency_key": "deadline", "objective": "Ship lane", "_auth_context": {
                "token_verified": True, "stream_id": ROOT,
                "session_generation": root["session_generation"]}})
            rid = first["ruling_request_id"]
            await store.submit(lambda conn: conn.execute(
                "UPDATE v2_assistant_lane_rulings SET deadline=0 WHERE ruling_request_id=?", (rid,)))
            await server.lane_rulings.tick()
            assert len(spawned.calls) == 1
            with pytest.raises(VerbError, match="assistant_ruling_stale"):
                await server._on_assistant_ruling({"request_id": "too-late", "ruling_request_id": rid,
                    "ruling": "deny", "reason": "late", "_auth_context": {"token_verified": True,
                    "stream_id": ADVISOR, "session_generation": advisor["session_generation"]}})
            await server.lane_rulings.tick()
            assert len(spawned.calls) == 1
            # A missed SLA tells the requesting lead only; it is never mirrored to
            # the operator composite surface.
            events = await store.fetch_session_event_tail("fixture-chat:assistant", limit=20)
            assert all("proceeded unruled" not in str(e.get("text") or "") for e in events)
            unruled = await store.submit(lambda conn: conn.execute(
                "SELECT recipient_stream_id, body, kind FROM v2_outbound_notices WHERE notice_id=?",
                ("assistant-lane-ruling-unruled:" + rid,)).fetchone())
            assert unruled is not None
            assert unruled[0] == ROOT and unruled[2] == "assistant_lane_ruling_result"
            assert "Ruling deadline passed (no answer in 10 min)" in unruled[1]
            assert f"proceeded unruled: spawn {first['stream_id']} ({rid})" in unruled[1]
            audit = await store.submit(lambda conn: [row[0] for row in conn.execute(
                "SELECT event FROM v2_assistant_lane_ruling_audit WHERE ruling_request_id=? ORDER BY id", (rid,))])
            assert audit == ["request", "timeout", "release", "refused"]
        finally:
            store.stop()
    asyncio.run(go())


def test_approved_spawn_waits_for_independent_host_freeze_then_creates_one_pane():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            advisor = await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            tmux = PaneTmux()
            sessions = Sessions(store, tmux=tmux, local_host="fixture-chat")
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            server = Server(store=store, sessions=sessions, spawnctl=ctl, local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)
            pending = await server._on_spawn({"type": "spawn", "role": "lead", "host": "fixture-chat",
                "session_name": "freeze-lane", "request_id": "freeze-lane", "idempotency_key": "freeze-lane",
                "objective": "Ship lane", "command": "stub", "_auth_context": {"token_verified": True,
                "stream_id": ROOT, "session_generation": root["session_generation"]}})
            assert pending["type"] == "spawn.pending_ruling" and tmux.created == 0
            await ctl.set_spawn_freeze("fixture-chat", reason="deploy")
            approved = await server._on_assistant_ruling({"request_id": "approve-frozen",
                "ruling_request_id": pending["ruling_request_id"], "ruling": "approve",
                "_auth_context": {"token_verified": True, "stream_id": ADVISOR,
                "session_generation": advisor["session_generation"]}})
            assert approved["state"] == "approved" and tmux.created == 0
            assert await store.get_spawn_admission_hold("fixture-chat") is not None
            await server._on_spawn_unfreeze({"host": "fixture-chat"})
            assert tmux.created == 1
            assert (await server.lane_rulings._fetch(pending["ruling_request_id"]))["state"] == "done"
            await server.lane_rulings.tick()
            assert tmux.created == 1
        finally:
            store.stop()
    asyncio.run(go())


def test_revise_is_terminal_and_new_attempt_links_without_auto_spawning():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            advisor = await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            spawned = RecordingSpawn()
            server = Server(store=store, sessions=Sessions(store, tmux=None, local_host="fixture-chat"),
                            spawnctl=spawned, local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)
            auth = {"token_verified": True, "stream_id": ROOT,
                    "session_generation": root["session_generation"]}
            base = {"type": "spawn", "role": "lead", "session_name": "revised-lane",
                    "objective": "Ship lane", "_auth_context": auth}
            first = await server._on_spawn({**base, "request_id": "old", "idempotency_key": "old"})
            revise = await server._on_assistant_ruling({"request_id": "revise-old",
                "ruling_request_id": first["ruling_request_id"], "ruling": "revise",
                "reason": "Add acceptance criteria", "_auth_context": {"token_verified": True,
                "stream_id": ADVISOR, "session_generation": advisor["session_generation"]}})
            assert revise["state"] == "revised" and spawned.calls == []
            same = await server._on_spawn({**base, "request_id": "old", "idempotency_key": "old"})
            assert same["state"] == "revised"
            second = await server._on_spawn({**base, "request_id": "new", "idempotency_key": "new",
                "objective": "Ship lane with acceptance criteria"})
            assert second["type"] == "spawn.pending_ruling"
            record = await server.lane_rulings._fetch(second["ruling_request_id"])
            assert record["linked_from"] == first["ruling_request_id"]
            assert spawned.calls == []
        finally:
            store.stop()
    asyncio.run(go())


def test_direct_primary_composite_admission_keeps_original_dispatch_and_version():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            advisor = await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            server = Server(store=store, sessions=Sessions(store, tmux=None, local_host="fixture-chat"),
                            local_host="fixture-chat")
            composite = AssistantComposite(store, config=config)
            server.assistant_composite = composite
            await composite.ensure_projection()
            route = await store.admit_assistant_composite_input(
                stream_id="fixture-chat:assistant", input_identity="operator-input",
                input_request_id="operator-input", body="Open a lane", attachments=[],
                reply_to_message_id=None, reply_to_question_id=None,
                actor_stream_id="operator:fixture")
            await store.update_assistant_composite_route(
                route["route_id"], routing_state="resolved", delivery_state="landed",
                dispatch_id="direct-dispatch", route_target=ROOT,
                route_target_generation=root["session_generation"], route_payload={
                    "schema_version": "assistant-router/v1", "disposition": "lane",
                    "lane_id": None, "depends_on_message_id": None, "reason": "fixture"})
            admission = {"operation": "lane.admit", "request_id": "direct-admit",
                "composite_stream_id": "fixture-chat:assistant", "dispatch_id": "direct-dispatch",
                "payload": {"mode": "new", "subject": "Ship lane",
                            "request_message_id": "operator-input"},
                "_auth_context": {"token_verified": True, "stream_id": ROOT,
                                  "session_generation": root["session_generation"]}}
            pending = await server._on_assistant_operation(admission)
            assert pending["type"] == "assistant.operation.pending_ruling"
            assert await store.get_assistant_composite_lane(
                stream_id="fixture-chat:assistant", lane_id=pending["lane_id"]) is None
            row = await server.lane_rulings._fetch(pending["ruling_request_id"])
            assert row["intent_digest"] and row["action"] == "composite_admit"
            approved = await server._on_assistant_ruling({"request_id": "approve-composite",
                "ruling_request_id": pending["ruling_request_id"], "ruling": "approve",
                "_auth_context": {"token_verified": True, "stream_id": ADVISOR,
                                  "session_generation": advisor["session_generation"]}})
            assert approved["state"] == "done"
            lane = await store.get_assistant_composite_lane(
                stream_id="fixture-chat:assistant", lane_id=pending["lane_id"])
            assert lane is not None and lane["version"] == 1
            assert row["requester_stream_id"] == ROOT
            assert row["authority_stream_id"] == ADVISOR
        finally:
            store.stop()
    asyncio.run(go())


@pytest.mark.parametrize("authority", ["disabled", "", ROOT])
def test_owned_lane_close_preserves_ordinary_path_when_rulings_bypassed(authority):
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            lane = await store.open_session("fixture-chat", "lead", provider="codex", role="lead",
                                            parent_stream_id=ROOT, pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
            })
            sessions = Sessions(store, tmux=None, local_host="fixture-chat")
            server = Server(store=store, sessions=sessions, local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)
            await server.lane_rulings._record_ownership({
                "requester_stream_id": ROOT, "requester_generation": root["session_generation"],
                "ruling_request_id": "admitted-lane",
            }, "fixture-chat:lead", lane["session_generation"])
            await store.put("assistant.authority.stream_id", authority)
            async def no_report(*_args, **_kwargs):
                return None
            store.find_report = no_report
            called = []
            async def close(*_args, **_kwargs):
                called.append(True)
                return {"already_closed": False, "session": lane, "reap_status": "done"}
            sessions.close = close
            reply = await server._on_close({"type": "close", "host": "fixture-chat",
                "session_name": "lead", "request_id": "bypass-close", "_auth_context": {
                "token_verified": True, "stream_id": ROOT,
                "session_generation": root["session_generation"]}})
            assert reply["type"] == "close.ok" and called == [True]
        finally:
            store.stop()
    asyncio.run(go())


def test_disable_racing_an_admission_cannot_leave_pending_ruling():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            spawned = RecordingSpawn()
            server = Server(store=store, sessions=Sessions(store, tmux=None, local_host="fixture-chat"),
                            spawnctl=spawned, local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)
            entered = asyncio.Event()
            release = asyncio.Event()
            create = server.lane_rulings._create
            async def paused_create(**kwargs):
                entered.set()
                await release.wait()
                return await create(**kwargs)
            server.lane_rulings._create = paused_create
            auth = {"token_verified": True, "stream_id": ROOT,
                    "session_generation": root["session_generation"]}
            task = asyncio.create_task(server._on_spawn({"type": "spawn", "role": "lead",
                "session_name": "racing", "request_id": "racing", "idempotency_key": "racing",
                "objective": "Ship lane", "_auth_context": auth}))
            await entered.wait()
            disabled = await server._on_assistant_authority({
                "action": "set", "value": "disabled", "_auth_context": auth})
            assert disabled["state"] == "disabled"
            release.set()
            response = await task
            assert response["type"] == "spawn.ok" and len(spawned.calls) == 1
            pending = await store.submit(lambda conn: conn.execute(
                "SELECT count(*) FROM v2_assistant_lane_rulings WHERE state='pending'").fetchone()[0])
            assert pending == 0
        finally:
            store.stop()
    asyncio.run(go())


def test_moved_composite_close_version_remains_open_with_explicit_receipt():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            server = Server(store=store, sessions=Sessions(store, tmux=None, local_host="fixture-chat"),
                            local_host="fixture-chat")
            composite = AssistantComposite(store, config=config)
            server.assistant_composite = composite
            request = await server.lane_rulings._create(
                action="composite_close", key="composite_close:moved",
                intent={"operation": "lane.close", "request_id": "moved", "lane_id": "lane-moved",
                        "expected_lane_version": 3}, requester=ROOT,
                requester_generation=root["session_generation"],
                binding=await server.lane_rulings.binding(), target="lane-moved",
                target_generation="")
            rid = request["ruling_request_id"]
            await store.submit(lambda conn: conn.execute(
                "UPDATE v2_assistant_lane_rulings SET state='approved',ruling='approve' WHERE ruling_request_id=?",
                (rid,)))
            async def moved(*_args, **_kwargs):
                raise ValueError("assistant_lane_version_conflict")
            composite.operation = moved
            await server.lane_rulings._release(request)
            record = await server.lane_rulings._fetch(rid)
            assert record["state"] == "approved_but_not_closed"
            assert "assistant_lane_version_conflict" in record["outcome_json"]
        finally:
            store.stop()
    asyncio.run(go())


def test_spawn_notice_carries_resolved_objective_and_acceptance_beyond_excerpt():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "primary", provider="codex")
            await store.open_session("fixture-advisor", "astra", provider="codex", pane_status="pane_alive")
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
            })
            server = Server(store=store, sessions=Sessions(store, tmux=None, local_host="fixture-chat"),
                            spawnctl=RecordingSpawn(), local_host="fixture-chat")
            server.assistant_composite = AssistantComposite(store, config=config)
            prompt = "Build a new lane.\n" + ("Context.\n" * 300) + "## Acceptance\n- Verified release proof\n"
            pending = await server._on_spawn({"type": "spawn", "role": "lead",
                "session_name": "notice-context", "request_id": "notice-context",
                "idempotency_key": "notice-context", "initial_prompt": prompt,
                "_auth_context": {"token_verified": True, "stream_id": ROOT,
                                  "session_generation": root["session_generation"]}})
            notice = await store.submit(lambda conn: conn.execute(
                "SELECT body FROM v2_outbound_notices WHERE notice_id=?",
                ("assistant-lane-ruling:" + pending["ruling_request_id"],)).fetchone()[0])
            detail = json.loads(notice.split("\n", 1)[1])
            assert detail["brief"]["objective"]
            assert "Verified release proof" in detail["brief"]["acceptance"]
        finally:
            store.stop()
    asyncio.run(go())
