"""Successor FD retirement through authenticated transport and durable binding."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions, VerbError
from store import Store
from window_schedule import WindowSchedule
from test_window_schedule_contract import FakeComms, FakeSpawn, SPEC

A, B, C, OTHER = "fixture-node:prior", "fixture-node:current", "fixture-node:next", "fixture-node:ordinary"


class RetirementHarness:
    async def open(self, actor, **fields):
        token = uuid.uuid4().hex + uuid.uuid4().hex
        row = await self.store.open_session(
            *actor.split(":", 1), provider="codex", role="assistant",
            visibility="default", pane_status="pane_alive", effective_model="gpt-6.1-sol",
            effective_effort="medium", token_hash=hashlib.sha256(token.encode()).hexdigest(),
            token_hash_version="sha256:v1", spec_id=SPEC, spec_ids=[SPEC],
            qualified_spec_ids=[SPEC], spec_binding_provenance=[{
                "spec_id": SPEC, "provenance": "spawn_explicit",
                "granting_principal": "operator", "granted_at": "2026-10-01T00:00:00Z",
            }], **fields,
        )
        self.tokens[actor] = token
        self.rows[actor] = row
        return row

    async def rpc(self, verb, actor=B, **values):
        msg = {"type": verb if verb.startswith("schedule.") else "schedule.recurring." + verb,
               "request_id": str(uuid.uuid4()),
               "from_stream_id": actor, "stream_token": self.tokens[actor], **values}
        replies = await self.server._dispatch(json.dumps(msg), websocket=object())
        assert len(replies) == 1
        return replies[0]

    async def create(self, actor=A, **values):
        reply = await self.rpc(
            "create", actor, name="Daily fixture", every="daily", at_time="11:30", tz="UTC",
            provider="codex", model="gpt-6.1-sol", effort="medium", spec_ids=[SPEC],
            target_host="fixture-node", initial_prompt="Private fixture prompt", objective="Daily fixture",
            **{"parent": "none", **values},
        )
        assert reply["type"] == "schedule.recurring.create.ok", reply
        return reply["recurring"]

    async def bind(self, actor, target):
        current = await self.composite.load_binding()
        # The composite owns the supported binding transition and its primary audit.
        return await self.composite.rebind(
            {"request_id": str(uuid.uuid4()), "expected_revision": current["revision"],
             "target_stream_id": target}, actor_stream_id=actor,
        )

    async def row(self, rid):
        return await self.store.submit(lambda conn: dict(conn.execute(
            "SELECT * FROM v2_recurring_schedules WHERE recurring_id=?", (rid,),
        ).fetchone()))

    async def write_fixture(self, sql, args=()):
        def op(conn):
            conn.execute(sql, args)
            conn.commit()
        await self.store.submit(op)

    async def context(self, actor=B):
        return await self.server._auth_context(object(), {
            "type": "schedule.recurring.get", "from_stream_id": actor, "stream_token": self.tokens[actor],
        })


@asynccontextmanager
async def retirement_harness(tmp_path):
    h = RetirementHarness()
    h.store = Store(str(tmp_path / "retirement.db"))
    h.store.start()
    h.tokens, h.rows = {}, {}
    try:
        for actor in (A, B, C, OTHER):
            await h.open(actor)
        h.sessions = Sessions(h.store, local_host="fixture-node")
        config = AssistantCompositeConfig.from_env({
            "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-node:assistant",
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": A,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": h.rows[A]["session_generation"],
        })
        h.composite = AssistantComposite(h.store, config=config)
        h.sessions.assistant.front_desk = h.composite
        await h.composite.load_binding()
        h.spawn = FakeSpawn()
        h.surface = WindowSchedule(h.store, h.sessions, FakeComms(), h.spawn, local_host="fixture-node")
        h.surface.mark_store_ready()
        h.server = Server(store=h.store, sessions=h.sessions, local_host="fixture-node")
        h.server.window_schedule = h.surface
        h.server.handlers.update(h.surface.wire_handlers())
        yield h
    finally:
        h.store.stop()


def test_current_fd_can_retire_closed_predecessor_through_transport(tmp_path):
    async def go():
        async with retirement_harness(tmp_path) as h:
            rule = await h.create()
            await h.bind(A, B)
            await h.store.mark_closed(*A.split(":", 1), closed_at="2026-10-02T00:00:00Z",
                                      pane_status="pane_dead")
            listed = await h.rpc("list")
            assert [row["recurring_id"] for row in listed["recurring"]] == [rule["recurring_id"]]
            got = await h.rpc("get", recurring_id=rule["recurring_id"])
            assert got["recurring"]["created_by_stream_id"] == A
            paused = await h.rpc("pause", recurring_id=rule["recurring_id"])
            assert paused["recurring"]["state"] == "paused"
            assert (await h.rpc("delete", recurring_id=rule["recurring_id"]))["type"].endswith("delete.ok")
            assert (await h.rpc("get", recurring_id=rule["recurring_id"]))["error_code"] == "not_found"
    asyncio.run(go())


def test_history_spans_generations_and_open_predecessors_without_changing_templates(tmp_path):
    async def go():
        async with retirement_harness(tmp_path) as h:
            first = await h.create(parent="assistant")
            original = await h.row(first["recurring_id"])
            await h.bind(A, B)
            second = await h.create(B)
            # A later generation of the same prior seat also leaves durable history.
            old_generation = h.rows[A]["session_generation"]
            await h.open(A)
            assert h.rows[A]["session_generation"] != old_generation
            await h.bind(B, A)
            third = await h.create(A)
            await h.bind(A, C)
            own = await h.create(C)
            foreign = await h.create(OTHER, parent="assistant")
            listed = await h.rpc("list", C)
            assert {r["recurring_id"] for r in listed["recurring"]} == {
                r["recurring_id"] for r in (first, second, third, own)}
            assert foreign["recurring_id"] not in {r["recurring_id"] for r in listed["recurring"]}
            for rule in (first, second, third):
                got = await h.rpc("get", C, recurring_id=rule["recurring_id"])
                assert got["recurring"]["created_by_stream_id"] == rule["created_by_stream_id"]
                assert (await h.rpc("pause", C, recurring_id=rule["recurring_id"]))["recurring"]["state"] == "paused"
            after = await h.row(first["recurring_id"])
            for field in original.keys() - {"state", "generation", "updated_at"}:
                assert after[field] == original[field]
    asyncio.run(go())


@pytest.mark.parametrize("verb", ["update", "resume", "run"])
def test_successor_cannot_rearm_or_edit_predecessor(tmp_path, verb):
    async def go():
        async with retirement_harness(tmp_path) as h:
            rid = (await h.create())["recurring_id"]
            await h.bind(A, B)
            before = await h.row(rid)
            reply = await h.rpc(verb, recurring_id=rid, name="Changed")
            assert reply["error_code"] == "not_found"
            assert await h.row(rid) == before
            assert h.spawn.calls == []
    asyncio.run(go())


@pytest.mark.parametrize("case", ["ordinary", "forged", "wrong_token", "absent_token", "wrong_from",
                                  "closed", "noncurrent", "retired", "revoked", "foreign_composite"])
def test_ineligible_actor_cannot_disclose_or_mutate(tmp_path, case):
    async def go():
        async with retirement_harness(tmp_path) as h:
            rid = (await h.create())["recurring_id"]
            await h.bind(A, B)
            before = await h.row(rid)
            actor, claims = B, {}
            if case in {"ordinary", "forged"}:
                actor = OTHER
            if case == "forged":
                claims = {"role": "nexus", "title": "Front desk", "created_by_stream_id": A,
                          "is_fd": True, "_auth_context": {"operator_authenticated": True}}
            if case == "wrong_token":
                claims["stream_token"] = h.tokens[OTHER]
            if case == "absent_token":
                claims["stream_token"] = None
            if case == "wrong_from":
                claims["from_stream_id"] = A
            if case == "closed":
                await h.store.mark_closed(*B.split(":", 1), closed_at="2026-10-02T00:00:00Z", pane_status="pane_dead")
            if case == "noncurrent":
                await h.bind(B, C)
            if case == "retired":
                await h.write_fixture("INSERT INTO v2_host_transfer_retirements VALUES (?,?,?,?)",
                                      (B, h.rows[B]["session_generation"], "fixture-retirement", 1.0))
            if case == "revoked":
                await h.write_fixture("INSERT OR REPLACE INTO v2_lifecycle_manager VALUES (1,NULL,NULL,1,1)")
            if case == "foreign_composite":
                await h.store.recover_assistant_binding(name="secondary", target_stream_id=B)
                await h.bind(B, C)
            for verb in ("list", "get", "pause", "delete"):
                reply = await h.rpc(verb, actor, recurring_id=rid, **claims)
                if verb == "list" and "recurring" in reply:
                    assert reply["recurring"] == []
                else:
                    assert reply["type"].endswith(".error"), reply
                assert "Private fixture prompt" not in json.dumps(reply)
                assert "initial_prompt_b64" not in json.dumps(reply)
            assert await h.row(rid) == before
    asyncio.run(go())


@pytest.mark.parametrize("case", ["missing_generation", "stale_generation", "unconfigured", "binding_failure",
                                  "history_failure", "unknown_history", "foreign_creator", "legacy_grant",
                                  "blank_creator", "unavailable_binding", "legacy_history"])
def test_missing_proof_denies_exception_and_keeps_creator_access(tmp_path, monkeypatch, case):
    async def go():
        async with retirement_harness(tmp_path) as h:
            # B remains the authenticated requester throughout; only the proof changes.
            rid = (await h.create())["recurring_id"]
            await h.bind(A, B)
            own = (await h.create(B))["recurring_id"]
            auth = await h.context()
            if case == "missing_generation":
                auth.pop("session_generation")
            if case == "stale_generation":
                await h.open(B)
                await h.store.recover_assistant_binding(name=h.composite.env_config.name, target_stream_id=B)
            if case == "unavailable_binding":
                await h.open(B)  # durable binding still names the previous generation
            if case in {"unconfigured", "legacy_grant"}:
                h.store.lifecycle_front_desk = None
            if case == "legacy_grant":
                await h.write_fixture("INSERT OR REPLACE INTO v2_lifecycle_manager VALUES (1,?,?,1,1)",
                                      (B, h.rows[B]["session_generation"]))
            def unavailable(*args, **kwargs):
                raise ValueError("fixture unavailable")
            if case == "binding_failure":
                monkeypatch.setattr("store_lifecycle_authority.front_desk_principal", unavailable)
            if case == "history_failure":
                monkeypatch.setattr("store_assistant_binding.front_desk_aliases_conn", unavailable)
            if case == "unknown_history":
                await h.write_fixture("UPDATE v2_recurring_schedules SET created_by_stream_id=? WHERE recurring_id=?", (OTHER, rid))
            if case == "blank_creator":
                await h.write_fixture("UPDATE v2_recurring_schedules SET created_by_stream_id=NULL WHERE recurring_id=?", (rid,))
            if case == "legacy_history":
                await h.write_fixture("UPDATE v2_recurring_schedules SET created_by_stream_id=? WHERE recurring_id=?", (OTHER, rid))
                await h.write_fixture(
                    "INSERT INTO v2_lifecycle_authority_audit(action,actor_kind,target_stream_id,result,created_at) "
                    "VALUES ('designate','operator',?,'applied',1)", (OTHER,))
            if case == "foreign_creator":
                await h.store.recover_assistant_binding(name="secondary", target_stream_id=A)
            msg = {"from_stream_id": B, "_auth_context": auth, "recurring_id": rid}
            for verb in ("get", "pause", "delete"):
                with pytest.raises(VerbError) as exc:
                    await h.surface.wire_handlers()["schedule.recurring." + verb](msg)
                assert exc.value.code == "not_found"
            listed = await h.surface.recurring_list(msg)
            assert [r["recurring_id"] for r in listed["recurring"]] == [own]
            assert (await h.surface.recurring_get({**msg, "recurring_id": own}))["recurring"]["recurring_id"] == own
    asyncio.run(go())


@pytest.mark.parametrize("kind", ["creator", "operator", "service", "nexus"])
def test_existing_authority_still_controls_nonhistorical_rules(tmp_path, kind):
    async def go():
        async with retirement_harness(tmp_path) as h:
            rid = (await h.create(OTHER))["recurring_id"]
            await h.bind(A, B)
            actor = OTHER if kind == "creator" else B
            auth = await h.context(actor)
            if kind == "operator":
                auth = {"operator_authenticated": True, "operator_principal": "operator:fixture"}
            if kind == "service":
                auth = {"service_authenticated": True, "service_actor": "fixture:service"}
            if kind == "nexus":
                await h.write_fixture("UPDATE sessions SET role='nexus' WHERE host='fixture-node' AND session_name='current'")
            msg = {"from_stream_id": actor, "_auth_context": auth, "recurring_id": rid}
            assert [r["recurring_id"] for r in (await h.surface.recurring_list(msg))["recurring"]] == [rid]
            assert (await h.surface.recurring_get(msg))["recurring"]["recurring_id"] == rid
            assert (await h.surface.recurring_pause(msg))["recurring"]["state"] == "paused"
            assert (await h.surface.recurring_delete(msg))["type"].endswith("delete.ok")
    asyncio.run(go())


def test_successor_does_not_gain_one_shot_schedule_access(tmp_path):
    async def go():
        async with retirement_harness(tmp_path) as h:
            created = await h.rpc("schedule.insert", A, objective="One shot fixture",
                                  fires_at_utc=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                                  target_host="fixture-node", provider="codex", model="gpt-6.1-sol",
                                  effort="medium", spec_ids=[SPEC])
            assert created["type"] == "schedule.insert.ok", created
            sid = created["schedule"]["schedule_id"]
            await h.bind(A, B)
            assert (await h.rpc("schedule.list"))["schedules"] == []
            for verb in ("get", "cancel", "run"):
                assert (await h.rpc("schedule." + verb, schedule_id=sid))["error_code"] == "not_found"
    asyncio.run(go())


@pytest.mark.parametrize("verb", ["pause", "delete"])
def test_rebind_before_durable_mutation_refuses_stale_fd(tmp_path, monkeypatch, verb):
    async def go():
        async with retirement_harness(tmp_path) as h:
            rid = (await h.create())["recurring_id"]
            await h.bind(A, B)
            before = await h.row(rid)
            reached, release = asyncio.Event(), asyncio.Event()
            original = h.store.front_desk_fence
            request_task = None
            count = 0
            @asynccontextmanager
            async def delayed_fence():
                nonlocal count
                if asyncio.current_task() is request_task:
                    count += 1
                    # Pause first reads the target, then rechecks in its write;
                    # delete performs its only target read inside its write.
                    if count == (2 if verb == "pause" else 1):
                        reached.set()
                        await release.wait()
                async with original():
                    yield
            monkeypatch.setattr(h.store, "front_desk_fence", delayed_fence)
            request_task = asyncio.create_task(h.rpc(verb, recurring_id=rid))
            try:
                await asyncio.wait_for(reached.wait(), 2)
                await h.bind(B, C)
            finally:
                release.set()
            reply = await asyncio.wait_for(request_task, 2)
            assert reply["error_code"] == "not_found"
            assert await h.row(rid) == before
            assert (await h.rpc(verb, C, recurring_id=rid))["type"].endswith(verb + ".ok")
    asyncio.run(go())


def test_pause_generation_conflict_and_response_loss_retries(tmp_path):
    async def go():
        async with retirement_harness(tmp_path) as h:
            rid = (await h.create())["recurring_id"]
            await h.bind(A, B)
            reply = await h.rpc("pause", recurring_id=rid, expected_generation=0)
            assert reply["error_code"] == "generation_conflict"
            assert (await h.row(rid))["state"] == "active"
            request_id = str(uuid.uuid4())
            await h.rpc("pause", recurring_id=rid, request_id=request_id)  # response lost
            first = await h.row(rid)
            assert (await h.rpc("pause", recurring_id=rid, request_id=request_id))["recurring"]["state"] == "paused"
            assert await h.row(rid) == first
            await h.rpc("delete", recurring_id=rid, request_id=request_id)  # response lost
            assert (await h.rpc("delete", recurring_id=rid, request_id=request_id))["error_code"] == "not_found"
    asyncio.run(go())


@pytest.mark.parametrize("verb", ["pause", "delete"])
def test_retirement_survives_due_boundary_and_restart_without_retracting_occurrence(tmp_path, monkeypatch, verb):
    async def go():
        async with retirement_harness(tmp_path) as h:
            rid = (await h.create())["recurring_id"]
            due = datetime(2026, 10, 21, 11, 30, tzinfo=timezone.utc)
            await h.write_fixture("UPDATE v2_recurring_schedules SET next_fire_at_utc=? WHERE recurring_id=?",
                                  ("2026-10-21T11:30:00.000000Z", rid))
            assert await h.surface.materialise_due_recurring(now=due) == 1
            # A completed earlier occurrence is independent of retirement.
            await h.write_fixture("UPDATE v2_schedules SET state='fired',terminal_at=updated_at")
            async def occurrences():
                return await h.store.submit(lambda conn: [dict(r) for r in conn.execute("SELECT * FROM v2_schedules")])
            before = await occurrences()
            assert len(before) == 1
            await h.bind(A, B)
            assert (await h.rpc(verb, recurring_id=rid))["type"].endswith(verb + ".ok")
            assert await h.surface.materialise_due_recurring(now=due + timedelta(days=1)) == 0
            restarted = WindowSchedule(h.store, h.sessions, FakeComms(), h.spawn, local_host="fixture-node")
            restarted.mark_store_ready()
            assert await restarted.materialise_due_recurring(now=due + timedelta(days=2)) == 0
            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return (due + timedelta(days=3)).astimezone(tz)
            class PassComplete(Exception):
                pass
            async def finish_pass(_delay):
                raise PassComplete
            monkeypatch.setattr("window_schedule.datetime", Clock)
            monkeypatch.setattr("window_schedule.asyncio.sleep", finish_pass)
            # Drive recovery and one complete real loop, including the due dispatch
            # scan. Only its final polling sleep ends this disposable scheduler.
            with pytest.raises(PassComplete):
                await restarted.run_forever()
            assert await occurrences() == before
            assert h.spawn.calls == []
    asyncio.run(go())
