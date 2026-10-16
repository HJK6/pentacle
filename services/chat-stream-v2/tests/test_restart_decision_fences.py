"""Real restore scheduling around real proof reads; only external Rig is fake."""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.test_assistant_restore import Rig, G0


ORDERS = (
    "sequential", "after_proof", "before_proof", "stale_negative_complete",
    "stale_negative_opened", "stale_healthy_opened", "fresh_mismatch_opened", "two_stage",
)


@pytest.mark.parametrize("order", ORDERS)
def test_restore_decisions_keep_accepted_input_across_awaits(tmp_path, monkeypatch, order):
    async def run():
        rig = Rig(tmp_path, monkeypatch, auto=True)
        tasks, sent, observations = [], [], []
        reached, release = asyncio.Event(), asyncio.Event()
        await rig.start()
        proof = rig.restore.input_death_proof

        async def pause(point):
            if point == "after_spawn" and order in {
                "stale_negative_opened", "stale_healthy_opened", "fresh_mismatch_opened", "two_stage",
            }:
                reached.set()
                await release.wait()

        async def dispatch(route):
            sent.append(dict(route))
            return {"delivery": "landed"}

        async def open_attempt():
            tasks.append(asyncio.create_task(rig.restore.advance()))
            await asyncio.wait_for(reached.wait(), 2)

        async def interleave(*args):
            number = len(observations)
            if order == "two_stage" and number == 1:
                release.set()
                assert (await asyncio.wait_for(tasks[0], 2))["state"] == "restored"
                observed = await proof(*args)
            elif number:
                observed = await proof(*args)
            elif order == "before_proof":
                assert (await rig.restore.advance())["state"] == "restored"
                observed = await proof(*args)
            elif order.startswith("stale_negative") or order == "stale_healthy_opened":
                rig.tmux.states["fd"] = "alive" if order == "stale_healthy_opened" else "unknown"
                observed = await proof(*args)
                assert observed[1] == ("healthy" if order == "stale_healthy_opened" else "waiting_evidence")
                rig.tmux.states["fd"] = "gone"
                if order == "stale_negative_complete":
                    assert (await rig.restore.advance())["state"] == "restored"
                else:
                    await open_attempt()
            elif order == "fresh_mismatch_opened":
                await open_attempt()
                observed = await proof(*args)
                assert observed[2] == "generation_mismatch"
            else:
                observed = await proof(*args)
                if order == "after_proof":
                    assert observed[0]
                    assert (await rig.restore.advance())["state"] == "restored"
                elif order == "two_stage":
                    assert observed[0]
                    await open_attempt()
            observations.append((observed[0], observed[1], observed[2], rig.composite._worker_rewake))
            return observed  # The actual proof result is never replaced.

        identity = "decision-" + order
        try:
            rig.restore._fault = pause
            rig.composite.dispatch = dispatch
            rig.composite.input_death_proof = interleave
            message = {"optimistic_id": identity, "text": "synthetic preserved input"}
            accepted = await rig.composite.accept_input(message)
            await asyncio.wait_for(rig.composite._worker, 2)
            if order == "sequential":
                assert (await rig.restore.advance())["state"] == "restored"
            release.set()
            for task in tasks:
                assert (await asyncio.wait_for(task, 2))["state"] == "restored"
            await asyncio.wait_for(rig.composite._worker, 2)
            final = await rig.store.get_assistant_composite_route(
                stream_id=rig.composite.config.stream_id, input_identity=identity)
            assert final["routing_state"] == "resolved", (order, observations, final["error_code"])
            assert final["delivery_state"] == "landed"
            assert len(sent) == 1
            assert final["event_id"] == accepted["event_id"]
            assert final["route_id"] == accepted["route_id"]
            assert json.loads(final["route_json"])["direct_binding_origin"]["generation"] == G0
            assert final["route_target_generation"] == (await rig.composite.binding())["generation"]
            assert (await rig.composite.accept_input(message))["duplicate"] is True
            await asyncio.wait_for(rig.composite._worker, 2)
            assert len(sent) == 1
            if order == "two_stage":
                assert observations[1][3] is True  # Real completion wake at the retry boundary.
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await rig.composite.stop()
            rig.store.stop()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["terminal", "hold"])
def test_decision_invalidated_at_route_write_reclaims_instead_of_losing_input(tmp_path, monkeypatch, outcome):
    async def run():
        rig = Rig(tmp_path, monkeypatch, auto=True)
        await rig.start()
        reached, release = asyncio.Event(), asyncio.Event()
        tasks, sent = [], []
        update = rig.store.update_assistant_composite_route
        proof = rig.restore.input_death_proof
        injected = False

        async def pause(point):
            if outcome == "hold" and point == "after_spawn":
                reached.set()
                await release.wait()

        async def observation(*args):
            if outcome == "terminal" and not injected:
                rig.tmux.states["fd"] = "unknown"
                result = await proof(*args)
                rig.tmux.states["fd"] = "gone"
                return result
            if outcome == "terminal":
                return await proof(*args)
            if not tasks:
                tasks.append(asyncio.create_task(rig.restore.advance()))
                await asyncio.wait_for(reached.wait(), 2)
            return await proof(*args)

        async def at_write(*args, **kwargs):
            nonlocal injected
            if kwargs.get("decision_observation") is not None and not injected:
                injected = True
                if outcome == "terminal":
                    assert (await rig.restore.advance())["state"] == "restored"
                else:
                    release.set()
                    assert (await asyncio.wait_for(tasks[0], 2))["state"] == "restored"
            return await update(*args, **kwargs)

        async def dispatch(route):
            sent.append(route)
            return {"delivery": "landed"}

        try:
            rig.restore._fault = pause
            rig.composite.input_death_proof = observation
            rig.composite.dispatch = dispatch
            monkeypatch.setattr(rig.store, "update_assistant_composite_route", at_write)
            await rig.composite.accept_input({"optimistic_id": "at-write", "text": "synthetic"})
            await asyncio.wait_for(rig.composite._worker, 2)
            final = await rig.store.get_assistant_composite_route(
                stream_id=rig.composite.config.stream_id, input_identity="at-write")
            assert injected
            assert final["delivery_state"] == "landed"
            assert len(sent) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await rig.composite.stop()
            rig.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["none", "holder", "missing_holder", "binding", "episode"])
def test_predispatch_observations_are_compared_in_the_write_transaction(tmp_path, monkeypatch, mutation):
    from tests.test_assistant_restore import LOCAL

    async def run():
        rig = Rig(tmp_path, monkeypatch, auto=True)
        await rig.start()
        try:
            monkeypatch.setattr(rig.composite, "_wake_worker", lambda: None)
            accepted = await rig.composite.accept_input({"optimistic_id": "fenced", "text": "synthetic"})
            binding = await rig.composite.load_binding()
            guard = {"name": rig.composite.config.name,
                     **{k: binding[k] for k in ("stream_id", "generation", "revision")}}
            holder = await rig.store.fetch_session(LOCAL, "fd")
            decision = {"holder": holder}
            if mutation == "holder":
                def alter(conn):
                    conn.execute("UPDATE sessions SET status='closed' WHERE host=? AND session_name='fd'", (LOCAL,))
                    conn.commit()
                await rig.store.submit(alter)
            elif mutation == "missing_holder":
                decision["holder"] = None  # Explicit missing observation is not no predicate.
            elif mutation == "binding":
                guard["revision"] -= 1
            elif mutation == "episode":
                # An exact episode id must exist and remain the same active attempt.
                decision["episode"] = {"episode_id": -1}
            before = await rig.store.get_assistant_composite_route(
                stream_id=rig.composite.config.stream_id, input_identity="fenced")
            kwargs = dict(routing_state="routing_failed", error_code="synthetic_refusal",
                          predispatch_only=True, binding_guard=guard,
                          binding_env=rig.composite._env_binding(), decision_observation=decision)
            if mutation == "none":
                result = await rig.store.update_assistant_composite_route(accepted["route_id"], **kwargs)
                assert result["routing_state"] == "routing_failed"
            else:
                with pytest.raises(ValueError, match="assistant_(route_binding|recovery_observation)_changed"):
                    await rig.store.update_assistant_composite_route(accepted["route_id"], **kwargs)
                after = await rig.store.get_assistant_composite_route(
                    stream_id=rig.composite.config.stream_id, input_identity="fenced")
                assert after == before
        finally:
            await rig.composite.stop()
            rig.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [(None, None), ("state", "superseded"),
    ("attempt_generation", "unrelated-attempt"), ("attempt_seq", 999), ("expected_revision", 999)])
def test_active_hold_fences_the_exact_episode_not_just_the_binding(tmp_path, monkeypatch, field, value):
    from tests.test_assistant_restore import LOCAL

    async def run():
        rig = Rig(tmp_path, monkeypatch, auto=True)
        await rig.start()
        reached, release = asyncio.Event(), asyncio.Event()
        task = None

        async def pause(point):
            if point == "after_spawn":
                reached.set()
                await release.wait()
        try:
            monkeypatch.setattr(rig.composite, "_wake_worker", lambda: None)
            accepted = await rig.composite.accept_input({"optimistic_id": "episode-fence", "text": "synthetic"})
            rig.restore._fault = pause
            task = asyncio.create_task(rig.restore.advance())
            await asyncio.wait_for(reached.wait(), 2)
            binding = await rig.composite.load_binding()
            guard = {"name": rig.composite.config.name,
                     **{k: binding[k] for k in ("stream_id", "generation", "revision")}}
            holder = await rig.store.fetch_session(LOCAL, "fd")
            episode = (await rig.episodes())[-1]
            assert episode["state"] == "spawning"
            if field:
                def alter(conn):
                    # Fixed parameter allowlist, never caller-controlled SQL.
                    conn.execute(f"UPDATE v2_assistant_restore_episode SET {field}=? WHERE episode_id=?",
                                 (value, episode["episode_id"]))
                    conn.commit()
                await rig.store.submit(alter)
            kwargs = dict(routing_state="queued", error_code="assistant_awaiting_recovery",
                          predispatch_only=True, binding_guard=guard,
                          binding_env=rig.composite._env_binding(),
                          decision_observation={"holder": holder, "episode": episode})
            if field:
                with pytest.raises(ValueError, match="assistant_recovery_observation_changed"):
                    await rig.store.update_assistant_composite_route(accepted["route_id"], **kwargs)
                actual = await rig.store.get_assistant_composite_route(
                    stream_id=rig.composite.config.stream_id, input_identity="episode-fence")
                assert actual["error_code"] != "assistant_awaiting_recovery"
            else:
                result = await rig.store.update_assistant_composite_route(accepted["route_id"], **kwargs)
                assert result["error_code"] == "assistant_awaiting_recovery"
        finally:
            if task:
                task.cancel()
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            await rig.composite.stop()
            rig.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("dispatch,delivery", [("allocated", None), (None, "intent"),
    ("allocated", "intent"), ("allocated", "uncertain"),
    ("allocated", "committed_pending"), ("allocated", "landed")])
def test_decision_conflicts_cannot_requeue_any_allocated_action(tmp_path, monkeypatch, dispatch, delivery):
    from tests.test_assistant_restore import LOCAL

    async def run():
        rig = Rig(tmp_path, monkeypatch, auto=True)
        await rig.start()
        try:
            monkeypatch.setattr(rig.composite, "_wake_worker", lambda: None)
            accepted = await rig.composite.accept_input({"optimistic_id": "allocated-fence", "text": "synthetic"})
            def allocate(conn):
                conn.execute("UPDATE v2_assistant_composite_routes SET routing_state='classifying', "
                             "dispatch_id=?,delivery_state=? WHERE route_id=?",
                             (dispatch, delivery, accepted["route_id"]))
                conn.commit()
            await rig.store.submit(allocate)
            binding = await rig.composite.load_binding()
            guard = {"name": rig.composite.config.name,
                     **{k: binding[k] for k in ("stream_id", "generation", "revision")}}
            before = await rig.store.get_assistant_composite_route(
                stream_id=rig.composite.config.stream_id, input_identity="allocated-fence")
            result = await rig.store.update_assistant_composite_route(
                accepted["route_id"], routing_state="queued", error_code="conflict",
                predispatch_only=True, binding_guard=guard, binding_env=rig.composite._env_binding(),
                decision_observation={"holder": await rig.store.fetch_session(LOCAL, "fd")})
            after = await rig.store.get_assistant_composite_route(
                stream_id=rig.composite.config.stream_id, input_identity="allocated-fence")
            assert result is None and before == after
        finally:
            await rig.composite.stop()
            rig.store.stop()
    asyncio.run(run())
