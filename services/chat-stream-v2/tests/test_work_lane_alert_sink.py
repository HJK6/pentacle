"""M2 lane episodes through real Alerts, ErrorAlerts and the digest/outbox.

Only the external provider is synthetic. Core policy, persistence, delivery
proof, rebind and settlement use the production implementations.
"""
import ast
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from alerts import Alerts
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from test_work_lane_episodes import member, seed_observations, episodes
from notification_answer_fixture import fixture
from work_lanes_projection import WorkLanesInventory


def inventory(s):
    # Execute the actual composition-root expression without booting a daemon.
    import work_lane_alerts
    tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
    creation = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "work_lanes" for t in n.targets))
    return eval(compile(ast.Expression(creation), "main.py", "eval"), {
        "WorkLanesInventory": WorkLanesInventory,
        "WorkLaneAlertSink": work_lane_alerts.WorkLaneAlertSink,
        "store": s.store, "sessions": s.server.sessions, "server": s.server,
        "specs": None, "alerts": s.alerts,
    })


@asynccontextmanager
async def subject(root, *, configured=True):
    async with fixture(root, host="fixture") as (notify, queue, comms, provider, sessions, store):
        server = Server(store=store, sessions=sessions, comms=comms, local_host="fixture")
        server.notify = notify
        # Blob handlers are unrelated to this journey; no upload is performed.
        server.blobs = SimpleNamespace()
        origin = await store.fetch_session("fixture", "v2-test")
        config = AssistantCompositeConfig.from_env({
            "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture:assistant",
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": "fixture:v2-test",
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": origin["session_generation"],
        })
        composite = server.assistant_composite = AssistantComposite(store, config=config)
        await composite.ensure_projection()
        await composite.load_binding()
        comms.assistant_ingress_policy = composite.suppress_routine_backend_ingress
        queue.front_desk_digest = composite.front_desk_digest
        alerts = Alerts(store)
        if configured:
            await server.configure_error_alerts(queue, alerts)
        s = SimpleNamespace(store=store, notify=notify, queue=queue, provider=provider,
                            server=server, alerts=alerts, origin=origin, composite=composite)
        s.inventory = inventory(s)
        try:
            yield s
        finally:
            await s.inventory.stop()
            await composite.stop()


async def adopt(s, *, kind="completed"):
    lead = None
    if kind == "stale":
        row = await s.server.sessions.open("fixture", "v2-lead", provider="codex", role="lead", visibility="visible")
        lead = {"stream_id": "fixture:v2-lead", "generation": row["session_generation"]}
    payload = {"adoption_key": "stream:synthetic-lane", "title": "Build paper bridges",
               "owner_kind": "operator", "work_state": "active" if lead else "paused",
               "visible_chat": {"stream_id": "fixture:assistant"},
               "members": ["spec_demo__span"]}
    if lead:
        payload["lead"] = lead
    result = await s.store.apply_work_lane_operation(
        stream_id="fixture:assistant", request_id="adopt-synthetic-lane", operation="adopt",
        lane_id=None, expected_lane_version=None, payload=payload, actor_stream_id="fixture:v2-test",
        actor_generation=s.origin["session_generation"], binding_name=s.composite.config.name,
        env_binding=s.composite._env_binding())
    await seed_observations(s.store, [member("completed" if kind == "completed" else "in_progress")])
    return result["lane"]


async def drain(s):
    s.inventory.refresh()
    async with asyncio.timeout(3):
        await s.inventory._task


async def facts(s):
    return await s.notify._db.call("error_rows")


async def notices(s):
    return await s.server.error_alerts.notice_rows()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "record-only", "on"])
@pytest.mark.parametrize("kind", ["completed", "stale"])
async def test_inventory_emits_one_neutral_digest_line_per_episode(tmp_path, monkeypatch, mode, kind):
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", mode)
    monkeypatch.setenv("PENTACLE_VOICE_MODE", "off")
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    async with subject(tmp_path) as s:
        lane = await adopt(s, kind=kind)
        if kind == "stale":
            monkeypatch.setattr("work_lanes_projection._iso_now", lambda: "2030-01-01T00:00:00Z")
            # The lane store's existing sweep clock owns episode evaluation.
            monkeypatch.setattr("store_work_lanes._now", lambda: "2030-01-01T00:00:00Z")
        await drain(s)
        await drain(s)
        (episode,) = await episodes(s.store)
        (fact,) = await facts(s)
        assert episode["emitted_ref"] == fact["notification_id"]
        assert fact["firing_count"] == 1
        assert fact["error_context"]["principal"] == "system:work-lanes"
        assert fact["error_context"]["episode_id"] == episode["episode_id"]
        assert fact["error_context"]["code"] == "lane_" + kind
        assert fact["error_context"]["condition"] == "active"
        await s.server.error_alerts.reconcile()
        (held,) = await notices(s)
        assert held["kind"] == "front_desk_held"
        assert held["body"].startswith("Work update ") and "BLOCKER" not in held["body"]
        assert s.provider.pastes == []
        await s.queue.drain_once()
        await s.queue.drain_once()
        assert len(s.provider.pastes) == 1
        rows = {r["notice_id"]: r for r in await notices(s)}
        (digest,) = [r for r in rows.values() if json.loads(r["metadata"]).get("typed_digest")]
        assert digest["body"].startswith("Work update digest.") and "BLOCKER" not in digest["body"]
        assert json.loads(digest["metadata"])["members"] == [fact["notification_id"]]
        assert s.server.error_alerts.delivery(rows[held["notice_id"]], rows)["state"] == "delivered"
        assert (await s.store.get_work_lane(lane["lane_id"]))["lane"]["version"] == lane["version"]
        if kind == "completed":
            assert lane["bound_stream_id"] is None


@pytest.mark.asyncio
async def test_unavailable_boot_sink_replays_legacy_m1_pending_fact(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", "off")
    async with subject(tmp_path, configured=False) as s:
        await adopt(s)
        await drain(s)
        (episode,) = await episodes(s.store)
        assert episode["emitted_ref"] is None and await facts(s) == []
        # An M1 opening may retain its pre-binding principal in immutable JSON.
        legacy = json.loads(episode["fact_json"])
        legacy["principal"] = "daemon:work-lanes"
        def persist_legacy(conn):
            with conn:
                conn.execute("UPDATE v2_work_lane_episodes SET fact_json=? WHERE episode_id=?",
                             (json.dumps(legacy, sort_keys=True, separators=(",", ":")), episode["episode_id"]))
        await s.store.submit(persist_legacy)
        before = (await episodes(s.store))[0]["fact_json"]
        await s.server.configure_error_alerts(s.queue, s.alerts)
        await drain(s)
        await drain(s)
        (stored,) = await episodes(s.store)
        (fact,) = await facts(s)
        assert stored["fact_json"] == before
        assert stored["emitted_ref"] == fact["notification_id"]
        assert fact["error_context"]["principal"] == "system:work-lanes" and fact["firing_count"] == 1


@pytest.mark.asyncio
async def test_completion_settles_only_after_delivery_proof_and_clear_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    async with subject(tmp_path) as s:
        await adopt(s)
        await drain(s)
        (fact,) = await facts(s)
        await s.server.error_alerts.reconcile()
        await s.queue.front_desk_digest.tick()
        await s.server.error_alerts._prune(time.time())
        assert (await facts(s))[0]["state"] == "open"
        await seed_observations(s.store, [member("in_progress")])
        await drain(s)
        before = await notices(s)
        assert (await episodes(s.store))[0]["cleared_at"] is not None
        assert (await facts(s))[0]["state"] == "open"
        assert (await facts(s))[0]["error_context"]["condition"] == "active"
        await s.queue.drain_once()
        await s.server.error_alerts._prune(time.time())
        assert (await facts(s))[0]["state"] == "resolved"
        await drain(s)
        await s.queue.drain_once()
        assert len(await notices(s)) == len(before)
        assert len(s.provider.pastes) == 1
        assert (await facts(s))[0]["notification_id"] == fact["notification_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("deliver_before_restart", [False, True])
async def test_restart_after_real_sink_commit_before_ref_latch_is_idempotent(tmp_path, monkeypatch, deliver_before_restart):
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    first_ref = None
    async with subject(tmp_path) as s:
        await adopt(s)
        submit = s.store.submit
        async def crash_latch(fn):
            if fn.__name__ == "latch":
                raise asyncio.CancelledError()
            return await submit(fn)
        s.store.submit = crash_latch
        s.inventory.refresh()
        with pytest.raises(asyncio.CancelledError):
            await s.inventory._task
        s.store.submit = submit
        (episode,) = await episodes(s.store)
        (fact,) = await facts(s)
        assert episode["emitted_ref"] is None
        first_ref = fact["notification_id"]
        original_payload = episode["fact_json"]
        if deliver_before_restart:
            await s.queue.drain_once()
            assert len(s.provider.pastes) == 1
    # Both SQLite owners and the real core are recreated on their same files.
    async with subject(tmp_path) as s:
        await drain(s)
        await drain(s)
        (episode,) = await episodes(s.store)
        (fact,) = await facts(s)
        assert episode["fact_json"] == original_payload
        assert episode["emitted_ref"] == first_ref == fact["notification_id"]
        assert fact["firing_count"] == 1
        await s.queue.drain_once()
        await s.queue.drain_once()
        assert len(s.provider.pastes) == (0 if deliver_before_restart else 1)
    async with subject(tmp_path) as s:
        await drain(s)
        await s.queue.drain_once()
        assert s.provider.pastes == []
        assert (await facts(s))[0]["notification_id"] == first_ref


@pytest.mark.asyncio
async def test_clear_then_recur_uses_new_fact_and_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    async with subject(tmp_path) as s:
        await adopt(s)
        await drain(s)
        await s.queue.drain_once()
        await seed_observations(s.store, [member("in_progress")])
        await drain(s)
        await s.queue.drain_once()
        assert len(s.provider.pastes) == 1
        await seed_observations(s.store, [member()])
        await drain(s)
        await s.queue.drain_once()
        assert len(s.provider.pastes) == 2
        rows = await episodes(s.store)
        assert [r["sequence"] for r in rows] == [1, 2]
        assert len({r["emitted_ref"] for r in rows}) == 2
        assert len(await facts(s)) == 2


@pytest.mark.asyncio
async def test_held_across_fd_rebind_delivers_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    async with subject(tmp_path) as s:
        await adopt(s)
        await drain(s)
        (episode,) = await episodes(s.store)
        await s.server.error_alerts.reconcile()
        await s.queue.front_desk_digest.tick()
        new = await s.server.sessions.open("fixture", "v2-next", provider="codex", visibility="visible")
        def bind(conn):
            with conn:
                conn.execute("INSERT OR REPLACE INTO v2_assistant_direct_binding "
                             "(name,stream_id,generation,revision,updated_at) VALUES(?,?,?,1,'2026-10-09T00:00:00Z')",
                             (s.composite.config.name, "fixture:v2-next", new["session_generation"]))
        await s.store.submit(bind)
        await s.composite.load_binding()
        # Synthetic provider now records native proof on the rebound recipient.
        async def new_user(text):
            await s.store.append_session_event("fixture:v2-next", {
                "stream_id": "fixture:v2-next", "host": "fixture", "provider": "codex",
                "session_name": "v2-next", "session_id": new["session_generation"],
                "timestamp": "2026-10-09T00:00:00Z", "kind": "USER", "text": text},
                identity="synthetic-rebound-proof:" + text, limit=500)
        s.provider.user = new_user
        await drain(s)
        await s.queue.drain_once()
        await s.queue.drain_once()
        assert len(s.provider.pastes) == 1
        assert (await episodes(s.store))[0]["emitted_ref"] == episode["emitted_ref"]
        assert (await facts(s))[0]["firing_count"] == 1
        rows = {r["notice_id"]: r for r in await notices(s)}
        delivered = [r for r in rows.values() if json.loads(r["metadata"]).get("typed_digest")
                     and s.server.error_alerts.delivery(r, rows)["state"] == "delivered"]
        assert len(delivered) == 1 and delivered[0]["recipient_stream_id"] == "fixture:v2-next"
        proof = s.server.error_alerts.delivery(delivered[0], rows)
        assert proof["recipient_generation"] == new["session_generation"] and proof["proof_at"]


@pytest.mark.asyncio
async def test_real_core_commit_survives_clear_recur_during_sink_await(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    async with subject(tmp_path) as s:
        await adopt(s)
        entered, release = asyncio.Event(), asyncio.Event()
        emit = s.inventory.episode_sink.emit
        async def held_return(fact):
            ref = await emit(fact)  # The real core commits before the injected pause.
            if not entered.is_set():
                entered.set()
                await release.wait()
            return ref
        s.inventory.episode_sink.emit = held_return
        s.inventory.refresh()
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await seed_observations(s.store, [member("in_progress")])
            await s.store.reconcile_work_lane_episodes(None)
            await seed_observations(s.store, [member()])
            await s.store.reconcile_work_lane_episodes(None)
            rows = await episodes(s.store)
            assert len(rows) == 2 and all(r["emitted_ref"] is None for r in rows)
            assert rows[0]["cleared_at"] and rows[1]["cleared_at"] is None
        finally:
            release.set()
            await asyncio.wait_for(s.inventory._task, 3)
        rows = await episodes(s.store)
        assert rows[0]["emitted_ref"] and rows[1]["emitted_ref"] is None
        await drain(s)
        refs = {r["episode_id"]: r["emitted_ref"] for r in await episodes(s.store)}
        assert len(set(refs.values())) == 2
        assert all(refs[f["error_context"]["episode_id"]] == f["notification_id"] for f in await facts(s))
        await s.queue.drain_once()
        assert len(s.provider.pastes) == 1
        digest = next(r for r in await notices(s) if json.loads(r["metadata"]).get("typed_digest"))
        assert set(json.loads(digest["metadata"])["members"]) == set(refs.values())
