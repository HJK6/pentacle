"""Admission-time membership, using real lane operations and spawn lifecycle."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

import pytest

from assistant_composite import AssistantComposite
from sessions import VerbError
from store import Store
from test_assistant_prose_mirror import ROOT, _config
from test_spawn_wip_cap import setup, message
from test_work_lanes import Env
from work_lane_admission import WorkLaneRequired

SPEC = "spec_fixture__admission"
OTHER = "spec_fixture__other"


def fields(*ids):
    return {"spec_id": ids[0] if ids else None, "spec_ids": list(ids),
            "qualified_spec_ids": list(ids), "spec_resolution": "resolved" if ids else None,
            "spec_binding_provenance": [{"spec_id": identity, "provenance": "spawn_explicit",
                "granting_principal": "operator", "granted_at": "2026-01-01T00:00:00Z"}
                for identity in ids]}


@asynccontextmanager
async def environment(tmp_path, monkeypatch, *, enabled=True):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "off")
    for identity in (SPEC, OTHER):
        folder = tmp_path / "work" / "backlog" / identity.removeprefix("spec_")
        folder.mkdir(parents=True)
        (folder / "spec.md").write_text(f"---\nid: {identity}\nowner: fixture\n---\n"
                                      "## Acceptance Criteria\n### Custom\n- [ ] Admission\n")
    store = Store(":memory:")
    store.start()
    ctl, sessions, tmux = await setup(tmp_path, store, monkeypatch)
    ctl.work_lane_admission_enforce = enabled
    root = await store.open_session(*ROOT.split(":"), provider="codex", pane_pid="4242")
    composite = AssistantComposite(store, config=_config(root["session_generation"]))
    await composite.ensure_projection()
    env = Env(store, composite, root["session_generation"])
    try:
        yield env, ctl, tmux
    finally:
        for name in list(tmux.names):
            await tmux.kill_session(name)
            row = await store.fetch_session("fixture", name)
            if row:
                await store.mark_closed("fixture", name, closed_at="2026-01-01T00:00:00Z", pane_status="pane_dead",
                                        expected_generation=row["session_generation"])
        assert not tmux.names and not tmux.alive
        await composite.stop()
        store.stop()


async def lane(env, members=(SPEC,), *, key="spec:fixture", state="paused"):
    return (await env.adopt(key=key, state=state, owner="operator", members=list(members),
                           **({"blocker": "Fixture dependency"} if state == "blocked" else {})))["lane"]


async def intent(store, name="intent", *, ids=(SPEC,), prepared_qa=None, enabled=True):
    assert await store.reserve_stream_id("fixture", name, ttl_s=60, request_id=name, nonce=name)
    return await store.record_spawn_intent("fixture", name, {"open_fields": fields(*ids)},
        request_id=name, nonce=name, prepared_qa=prepared_qa, require_work_lane=enabled)


@pytest.mark.parametrize("role", ["lead", "nexus", "planner", "qa", "evaluator", "advisory", "worker", "assistant", None])
@pytest.mark.parametrize("visibility", ["default", "hidden"])
def test_all_spec_bound_roles_refuse_before_pane_and_release_reservation(tmp_path, monkeypatch, role, visibility):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            with pytest.raises(VerbError) as error:
                await ctl.spawn({**message(SPEC, role=role), "visibility": visibility}, "fixture")
            assert error.value.code == "work_lane_required"
            assert error.value.extra["missing_spec_ids"] == [SPEC]
            assert "work-lane show LANE_ID --json" in str(error.value)
            assert "set-members" in str(error.value) and "--member " + SPEC in str(error.value)
            assert "preserve every existing member" in str(error.value)
            assert "fresh --idempotency-key" in str(error.value)
            assert tmux.created == 0
            assert await env.store.fetch_session("fixture", "candidate") is None
            assert await env.store.reservations(include_expired=True) == []
    asyncio.run(go())


@pytest.mark.parametrize("state", ["active", "paused", "blocked"])
def test_valid_membership_admits_without_rebinding_lane(tmp_path, monkeypatch, state):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            before = await lane(env, state=state)
            result = await ctl.spawn(message(SPEC), "fixture")
            assert result["type"] == "spawn.ok" and tmux.created == 1
            assert (await env.store.get_work_lane(before["lane_id"]))["lane"] == before
    asyncio.run(go())


@pytest.mark.parametrize("kind", ["missing", "done", "routing", "unrelated", "secondary"])
def test_membership_is_exact_nonterminal_and_covers_all_specs(tmp_path, monkeypatch, kind):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            if kind != "missing":
                created = await lane(env, members=(OTHER,) if kind == "unrelated" else (SPEC,))
                if kind in ("done", "routing"):
                    # Model historical terminal/routing rows without bypassing completion in a live operation.
                    def historical(conn):
                        conn.execute("UPDATE v2_assistant_composite_lanes SET work_state=? WHERE lane_id=?",
                                     ("done" if kind == "done" else None, created["lane_id"]))
                        conn.commit()
                    await env.store.submit(historical)
            msg = message(SPEC)
            if kind == "secondary":
                msg["spec_ids"] = [SPEC, OTHER]
            with pytest.raises(VerbError) as error:
                await ctl.spawn(msg, "fixture")
            assert error.value.code == "work_lane_required"
            assert error.value.extra["missing_spec_ids"] == ([OTHER] if kind == "secondary" else [SPEC])
            assert tmux.created == 0
    asyncio.run(go())


def test_multi_lane_coverage_and_inherited_advisory(tmp_path, monkeypatch):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            first = await lane(env)
            second = await lane(env, (OTHER,), key="spec:other")
            parent, _ = await env.seat("parent", role="lead", **fields(SPEC, OTHER))
            result = await ctl.spawn({**message(None, role="advisory"), "parent_stream_id": parent}, "fixture")
            assert result["session"]["spec_ids"] == [SPEC, OTHER]
            assert tmux.created == 1
            for original in (first, second):
                assert (await env.store.get_work_lane(original["lane_id"]))["lane"] == original
    asyncio.run(go())


@pytest.mark.parametrize("role,extra", [
    (None, {"_auth_context": {"operator_authenticated": True}}),
    ("assistant", {}), ("lead", {"harness_run_id": "synthetic-admission"}),
    ("worker", {}),
])
def test_empty_effective_specs_need_no_lane(tmp_path, monkeypatch, role, extra):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            result = await ctl.spawn({**message(None, role=role), **extra}, "fixture")
            assert result["type"] == "spawn.ok" and tmux.created == 1
            assert await env.store.work_lane_rows(include_done=True) == []
    asyncio.run(go())


def test_inherited_missing_spec_is_not_unspecced(tmp_path, monkeypatch):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            parent, _ = await env.seat("parent", role="lead", **fields(SPEC))
            with pytest.raises(VerbError, match=SPEC) as error:
                await ctl.spawn({**message(None, role="qa"), "parent_stream_id": parent}, "fixture")
            assert error.value.code == "work_lane_required" and tmux.created == 0
    asyncio.run(go())


def test_member_repair_preserves_full_list_then_fresh_key_admits_and_binds(tmp_path, monkeypatch):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            members = [f"spec_fixture__existing_{i}" for i in range(10)]
            original = await lane(env, members)
            with pytest.raises(VerbError):
                await ctl.spawn(message(SPEC), "fixture")
            changed = await env.op("set_members", {"members": members + [SPEC]},
                                   lane=original["lane_id"], version=original["version"])
            with pytest.raises(VerbError):  # terminal failed key remains truthful
                await ctl.spawn(message(SPEC), "fixture")
            result = await ctl.spawn({**message(SPEC), "idempotency_key": "repaired"}, "fixture")
            row = result["session"]
            bound = await env.op("set_lead", {"lead": {"stream_id": row["stream_id"],
                "generation": row["session_generation"]}}, lane=original["lane_id"],
                version=changed["lane"]["version"])
            assert json.loads(bound["lane"]["members_json"]) == members + [SPEC]
            assert bound["lane"]["bound_stream_id"] == row["stream_id"]
            assert tmux.created == 1
    asyncio.run(go())


def test_transaction_refusal_does_not_commit_qa_or_intent(tmp_path, monkeypatch):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            calls = []
            with pytest.raises(WorkLaneRequired):
                await intent(env.store, prepared_qa=lambda conn: calls.append(True))
            assert calls == []
            reservations = await env.store.reservations(include_expired=True)
            assert reservations[0]["payload"] is None
            await lane(env)
            def fail_qa(conn):
                conn.execute("CREATE TABLE fixture_qa_rollback(value TEXT)")
                raise RuntimeError("fixture QA failure")
            with pytest.raises(RuntimeError, match="fixture QA failure"):
                await intent(env.store, name="qa-fail", prepared_qa=fail_qa)
            assert await env.store.submit(lambda conn: conn.execute(
                "SELECT name FROM sqlite_master WHERE name='fixture_qa_rollback'").fetchone()) is None
            assert all(row["payload"] is None for row in await env.store.reservations(include_expired=True))
    asyncio.run(go())


@pytest.mark.parametrize("intent_first", [True, False])
def test_membership_removal_and_intent_refresh_have_serial_order(tmp_path, monkeypatch, intent_first):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            original = await lane(env)
            async def remove():
                return await env.op("set_members", {"members": [OTHER]},
                                    lane=original["lane_id"], version=original["version"])
            operations = [intent(env.store), remove()] if intent_first else [remove(), intent(env.store)]
            # Explicit order is controlled at the writer, not guessed from event-loop timing.
            if intent_first:
                assert await operations[0]
                await operations[1]
                assert await env.store.record_spawn_intent("fixture", "intent", {"open_fields": fields(SPEC)},
                    request_id="intent", nonce="intent", require_work_lane=True)
            else:
                await operations[0]
                with pytest.raises(WorkLaneRequired):
                    await operations[1]
    asyncio.run(go())


def test_concurrent_adoption_and_intent_serialize_without_orphan(tmp_path, monkeypatch):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            results = await asyncio.gather(lane(env), intent(env.store), return_exceptions=True)
            assert isinstance(results[0], dict)
            assert results[1] is True or isinstance(results[1], WorkLaneRequired)
            row = (await env.store.reservations(include_expired=True))[0]
            assert (row["payload"] is not None) == (results[1] is True)
            assert tmux.created == 0
    asyncio.run(go())


def test_admitted_same_key_replay_survives_later_member_removal(tmp_path, monkeypatch):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            original = await lane(env)
            first = await ctl.spawn(message(SPEC), "fixture")
            await env.op("set_members", {"members": [OTHER]}, lane=original["lane_id"], version=original["version"])
            replay = await ctl.spawn(message(SPEC), "fixture")
            assert replay["stream_id"] == first["stream_id"] and tmux.created == 1
    asyncio.run(go())


def test_default_off_and_explicit_activation(tmp_path, monkeypatch):
    async def go():
        async with environment(tmp_path, monkeypatch, enabled=False) as (env, ctl, tmux):
            assert (await ctl.spawn(message(SPEC), "fixture"))["type"] == "spawn.ok"
            monkeypatch.setenv("PENTACLE_WORK_LANE_ADMISSION_ENFORCE", "1")
            enabled, _, _ = await setup(tmp_path, env.store, monkeypatch)
            assert enabled.work_lane_admission_enforce is True
            monkeypatch.delenv("PENTACLE_WORK_LANE_ADMISSION_ENFORCE")
            disabled, _, _ = await setup(tmp_path, env.store, monkeypatch)
            assert disabled.work_lane_admission_enforce is False
    asyncio.run(go())


@pytest.mark.parametrize("covered", [False, True])
def test_resume_uses_preserved_specs_even_when_request_has_none(tmp_path, monkeypatch, covered):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            prior = await env.store.open_session("fixture", "candidate", role="lead", **fields(SPEC))
            await env.store.mark_closed("fixture", "candidate", closed_at="2026-01-01T00:00:00Z", pane_status="pane_dead",
                                        expected_generation=prior["session_generation"])
            prior = await env.store.fetch_session("fixture", "candidate")
            async def target(*args):
                return "candidate", "/tmp/fixture-resume.jsonl", "/tmp", prior
            monkeypatch.setattr(ctl, "_resolve_resume_target", target)
            msg = {**message(None), "resume_session_id": "11111111-1111-4111-8111-111111111111"}
            if covered:
                await lane(env)
                result = await ctl.spawn(msg, "fixture")
                assert result["session"]["spec_ids"] == [SPEC]
                assert result["session"]["session_generation"] != prior["session_generation"]
                assert tmux.created == 1
            else:
                with pytest.raises(VerbError) as error:
                    await ctl.spawn(msg, "fixture")
                assert error.value.code == "work_lane_required" and tmux.created == 0
                assert (await env.store.fetch_session("fixture", "candidate"))["status"] == "closed"
    asyncio.run(go())


def test_handoff_successor_checks_membership_without_rebinding_precondition(tmp_path, monkeypatch):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            parent, generation = await env.seat("prior", role="lead", **fields(SPEC))
            original = await lane(env)
            original = (await env.op("set_lead", {"lead": {"stream_id": parent, "generation": generation}},
                                    lane=original["lane_id"], version=original["version"]))["lane"]
            result = await ctl.spawn({**message(None), "handoff_from_stream_id": parent}, "fixture")
            assert result["session"]["spec_ids"] == [SPEC] and tmux.created == 1
            # Existing handoff machinery owns any binding move; membership is retained.
            assert (await env.store.get_work_lane(original["lane_id"]))["lane"]["members_json"] == original["members_json"]
    asyncio.run(go())


def test_launch_failure_keeps_lane_and_releases_owned_reservation(tmp_path, monkeypatch):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            original = await lane(env)
            async def launch_failure(*args, **kwargs):
                raise RuntimeError("fixture launch failure")
            monkeypatch.setattr(tmux, "new_session", launch_failure)
            with pytest.raises(RuntimeError, match="fixture launch failure"):
                await ctl.spawn(message(SPEC), "fixture")
            assert (await env.store.get_work_lane(original["lane_id"]))["lane"] == original
            assert await env.store.reservations(include_expired=True) == []
            assert await env.store.fetch_session("fixture", "candidate") is None
            assert tmux.created == 0
    asyncio.run(go())


def test_existing_unlaned_seat_and_durable_intent_remain_operational(tmp_path, monkeypatch):
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            original = await env.store.open_session("fixture", "legacy", **fields(SPEC))
            assert await intent(env.store, enabled=False)
            assert await env.store.record_spawn_intent("fixture", "intent", {"open_fields": fields(SPEC)},
                request_id="intent", nonce="intent", require_work_lane=True)
            assert (await env.store.fetch_session("fixture", "legacy"))["status"] == "open"
            assert (await env.store.fetch_session("fixture", "legacy"))["session_generation"] == original["session_generation"]
            assert await env.store.work_lane_rows(include_done=True) == []
    asyncio.run(go())


@pytest.mark.parametrize("intent_first", [True, False])
def test_contending_writer_transactions_have_one_admission_order(tmp_path, monkeypatch, intent_first):
    import threading
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            original = await lane(env)
            assert await env.store.reserve_stream_id("fixture", "race", ttl_s=60, request_id="race")
            entered, release = threading.Event(), threading.Event()
            def hold(*args):
                entered.set()
                assert release.wait(5), "test failed to release its writer transaction"
            async def admit():
                return await env.store.record_spawn_intent("fixture", "race", {"open_fields": fields(SPEC)},
                    request_id="race", require_work_lane=True, prepared_qa=hold if intent_first else None)
            async def remove():
                return await env.op("set_members", {"members": [OTHER]},
                                    lane=original["lane_id"], version=original["version"])
            if not intent_first:
                env.store._work_lane_fault = hold
            first = asyncio.create_task(admit() if intent_first else remove())
            second = None
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                second = asyncio.create_task(remove() if intent_first else admit())
                await asyncio.sleep(0)
                assert not second.done()
            finally:
                release.set()
                results = await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
                env.store._work_lane_fault = None
            assert len(results) == 2
            if intent_first:
                assert results[0] is True and isinstance(results[1], dict)
            else:
                assert isinstance(results[0], dict) and isinstance(results[1], WorkLaneRequired)
            reservation = (await env.store.reservations(include_expired=True))[0]
            assert (reservation["payload"] is not None) == intent_first
    asyncio.run(go())


def test_crash_adoption_preserves_pre_activation_spec_bound_intent(tmp_path, monkeypatch):
    import os
    from test_spawn_reservation_identity import IdentityTmux, _held_transcript, _new_ctl, _stub_tmux_nonce
    monkeypatch.setenv("PENTACLE_WORK_LANE_ADMISSION_ENFORCE", "1")
    brief = "Review the synthetic work item."
    transcript = _held_transcript(tmp_path, json.dumps({"type": "user", "message": {"content": brief}}) + "\n")
    async def go():
        store = Store(":memory:")
        store.start()
        tmux = IdentityTmux(alive=True, pane_pid=str(os.getpid()), screen="boot")
        try:
            ctl = await _new_ctl(store, tmux, instance_id="new-daemon")
            assert ctl.work_lane_admission_enforce is True
            _stub_tmux_nonce(ctl, readable=True, value="fixture-nonce")
            assert await store.reserve_stream_id("localhost", "legacy-intent", ttl_s=60,
                request_id="legacy", nonce="fixture-nonce", owner_instance_id="old-daemon")
            assert await store.record_spawn_intent("localhost", "legacy-intent", {
                "open_fields": {**fields(SPEC), "objective": "Recover the existing fixture intent"}, "brief": brief})
            result = await ctl.reconcile_spawn_intents()
            assert result == {"adopted": 1, "released": 0}
            row = await store.fetch_session("localhost", "legacy-intent")
            assert row["status"] == "open" and row["spec_ids"] == [SPEC]
            assert await store.work_lane_rows(include_done=True) == []
            assert not tmux.killed
        finally:
            await tmux.kill_session("legacy-intent")
            await store.mark_closed("localhost", "legacy-intent", closed_at="2026-01-01T00:00:00Z",
                                    pane_status="pane_dead")
            store.stop()
    try:
        asyncio.run(go())
    finally:
        transcript.close()


@pytest.mark.parametrize("existing_stage", [False, True])
def test_lane_refusal_never_stages_or_overwrites_token(tmp_path, monkeypatch, existing_stage):
    import launch
    from tmux_transport import Tmux
    token_path = tmp_path / "owned-staging" / "fixture-token"
    if existing_stage:
        token_path.parent.mkdir()
        token_path.write_bytes(b"retained-fixture-token")
    before = (token_path.read_bytes(), token_path.stat().st_mtime_ns) if existing_stage else None
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            plan = launch.LaunchPlan(command="fixture-command", session_id="", jsonl_path="",
                stream_token="new-fixture-token", stream_token_file=str(token_path))
            async def resolve(*args):
                return plan.command, {"_deferred_launch_plan": plan}, {}
            stages = []
            async def stage(path, data):
                stages.append(path)
                await Tmux().stage_text(path, data)
            monkeypatch.setattr(ctl, "_resolve_launch", resolve)
            monkeypatch.setattr(tmux, "stage_text", stage, raising=False)
            with pytest.raises(VerbError) as error:
                await ctl.spawn(message(SPEC), "fixture")
            assert error.value.code == "work_lane_required"
            assert stages == [] and tmux.created == 0
            assert await env.store.reservations(include_expired=True) == []
            if existing_stage:
                assert (token_path.read_bytes(), token_path.stat().st_mtime_ns) == before
            else:
                assert not token_path.exists()
    asyncio.run(go())


@pytest.mark.parametrize("stage_fails", [False, True])
def test_enabled_token_stage_follows_intent_and_precedes_pane(tmp_path, monkeypatch, stage_fails):
    import launch
    from tmux_transport import Tmux
    token_path = tmp_path / "owned-staging" / "fixture-token"
    async def go():
        async with environment(tmp_path, monkeypatch) as (env, ctl, tmux):
            original = await lane(env)
            plan = launch.LaunchPlan(command="fixture-command", session_id="", jsonl_path="",
                stream_token="new-fixture-token", stream_token_file=str(token_path))
            async def resolve(*args):
                return plan.command, {"_deferred_launch_plan": plan}, {}
            stages = []
            async def stage(path, data):
                reservations = await env.store.reservations(include_expired=True)
                assert len(reservations) == 1 and reservations[0]["payload"] is not None
                assert tmux.created == 0
                stages.append(path)
                if stage_fails:
                    raise OSError("fixture staging failure")
                await Tmux().stage_text(path, data)
            monkeypatch.setattr(ctl, "_resolve_launch", resolve)
            monkeypatch.setattr(tmux, "stage_text", stage, raising=False)
            if stage_fails:
                with pytest.raises(VerbError) as error:
                    await ctl.spawn(message(SPEC), "fixture")
                assert error.value.code == "token_stage_failed"
                assert tmux.created == 0
                assert await env.store.reservations(include_expired=True) == []
                assert await env.store.fetch_session("fixture", "candidate") is None
            else:
                assert (await ctl.spawn(message(SPEC), "fixture"))["type"] == "spawn.ok"
                assert token_path.read_bytes() == b"new-fixture-token"
                assert tmux.created == 1
            assert stages == [str(token_path)]
            assert (await env.store.get_work_lane(original["lane_id"]))["lane"] == original
    asyncio.run(go())
