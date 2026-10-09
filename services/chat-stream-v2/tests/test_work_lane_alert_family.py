"""Lane facts use the shared core without owning transport or episode state."""
import asyncio
import json
import time

import pytest

import error_adapters
import error_alerts
from alerts import Alerts
from error_adapters import ErrorFact
from error_alerts import ErrorAlerts
from test_error_alerts import subject  # noqa: F401


def test_lane_family_registered():
    assert error_adapters.FAMILY_CODES.get("work_lane.v1") == frozenset({"lane_stale", "lane_completed"})


@pytest.fixture
def lane_registry(monkeypatch):
    # Isolate downstream RED predicates from the missing registration.
    monkeypatch.setitem(error_adapters.FAMILY_CODES, "work_lane.v1", frozenset({"lane_stale", "lane_completed"}))


async def open_lane(s, code="lane_stale", episode="lane:stale:1"):
    return await s.service.emit(ErrorFact("work_lane.v1", code, episode), principal="system:work-lanes")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "record-only", "on"])
@pytest.mark.parametrize("code", ["lane_stale", "lane_completed"])
async def test_lane_digest_only_mode_independent(subject, lane_registry, monkeypatch, mode, code):
    s = subject
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", mode)
    monkeypatch.setenv("PENTACLE_VOICE_MODE", "off")
    await open_lane(s, code)
    await s.service.reconcile()
    (member,) = await s.service.notice_rows()
    assert member["kind"] == "front_desk_held"
    assert member["body"].startswith("Work update ") and "BLOCKER" not in member["body"]
    assert s.provider.pastes == []
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    await s.queue.drain_once()
    assert len(s.provider.pastes) == 1
    notices = {r["notice_id"]: r for r in await s.service.notice_rows()}
    digest = next(r for r in notices.values() if json.loads(r["metadata"]).get("typed_digest"))
    assert digest["body"].startswith("Work update digest.")
    assert s.service.delivery(notices[member["notice_id"]], notices)["state"] == "delivered"


@pytest.mark.asyncio
async def test_lane_replay_two_step_ref_latch_and_silent_clear(subject, lane_registry, monkeypatch):
    s = subject
    alerts = Alerts()
    alerts.sink = s.service
    fact = ErrorFact("work_lane.v1", "lane_completed", "lane:completed:1")
    await s.store.submit(lambda c: c.execute("CREATE TABLE lane_stub(episode TEXT PRIMARY KEY, emitted_ref TEXT)"))
    await s.store.submit(lambda c: c.execute("INSERT INTO lane_stub VALUES(?,NULL)", (fact.episode_id,)))
    # Sink runs outside Store transactions. Simulate commit then lost caller result.
    nid = await alerts.error(fact, principal="system:work-lanes")
    s.service = ErrorAlerts(s.server, s.queue)
    await s.service.start()
    alerts.sink = s.service
    refs = await asyncio.gather(*(alerts.error(fact, principal="system:work-lanes") for _ in range(3)))
    assert refs == [nid] * 3
    await s.store.submit(lambda c: c.execute("UPDATE lane_stub SET emitted_ref=? WHERE episode=? AND emitted_ref IS NULL", (refs[0], fact.episode_id)))
    (row,) = await s.notify._db.call("error_rows")
    assert row["firing_count"] == 1
    await s.service.reconcile()
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    await s.queue.drain_once()
    before = await s.service.notice_rows()
    await s.service.emit(ErrorFact(fact.family, fact.code, fact.episode_id, condition="recovered"), principal="system:work-lanes")
    await s.service.reconcile()
    await s.queue.drain_once()
    assert len(await s.service.notice_rows()) == len(before)
    assert len(s.provider.pastes) == 1
    assert await alerts.error(fact, principal="system:work-lanes") == nid
    assert await open_lane(s, "lane_completed", "lane:completed:2") != nid


@pytest.mark.asyncio
async def test_lane_settles_only_after_digest_proof(subject, lane_registry, monkeypatch):
    s = subject
    nid = await open_lane(s)
    await s.service.reconcile()
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    await s.queue.front_desk_digest.tick()
    await s.service._prune(time.time())
    assert (await s.notify._db.call("error_get", nid))["state"] == "open"
    await s.queue.drain_once()
    await s.service._prune(time.time())
    row = await s.notify._db.call("error_get", nid)
    assert row["state"] == "resolved" and row["error_context"]["condition"] == "active"


@pytest.mark.asyncio
async def test_lane_digest_partition_ignores_error_policy(subject, lane_registry, monkeypatch):
    s = subject
    monkeypatch.setitem(error_alerts.POLICY, "effective_delivery_mode", "digest")
    monkeypatch.setitem(error_adapters.FAMILY_CODES, "probe.v1", frozenset({"probe_failed"}))
    await s.service.emit(ErrorFact("probe.v1", "probe_failed", "p1"))
    await open_lane(s)
    await s.service.reconcile()
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    await s.queue.front_desk_digest.tick()
    digests = [r for r in await s.service.notice_rows() if json.loads(r["metadata"]).get("typed_digest")]
    assert len(digests) == 2
    lane = next(r for r in digests if r["body"].startswith("Work update"))
    monkeypatch.setitem(error_alerts.POLICY, "effective_delivery_mode", "muted")
    assert await s.service.guard(lane) is None


@pytest.mark.asyncio
async def test_lane_neutral_member_line(subject, lane_registry):
    await open_lane(subject)
    await subject.service.reconcile()
    (row,) = await subject.service.notice_rows()
    assert row["body"].startswith("Work update ")


@pytest.mark.asyncio
async def test_lane_clear_after_proof_is_silent(subject, lane_registry, monkeypatch):
    s = subject
    await open_lane(s)
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    await s.queue.drain_once()
    await s.service.emit(ErrorFact("work_lane.v1", "lane_stale", "lane:stale:1", condition="recovered"), principal="system:work-lanes")
    await s.queue.drain_once()
    assert len(s.provider.pastes) == 1


@pytest.mark.asyncio
async def test_lane_rebind_retains_one_digest_and_ref(subject, lane_registry, monkeypatch):
    s = subject
    nid = await open_lane(s)
    await s.service.reconcile()
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    await s.queue.front_desk_digest.tick()
    new = await s.server.sessions.open("fixture", "v2-next", provider="codex", visibility="visible")

    def bind(conn):
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) VALUES(?,?,?,1,'2026-10-09T00:00:00Z')",
                (s.server.assistant_composite.config.name, "fixture:v2-next", new["session_generation"]),
            )
    await s.store.submit(bind)
    await s.server.assistant_composite.load_binding()
    await s.service.reconcile()
    assert await open_lane(s) == nid
    await s.queue.front_desk_digest.tick()
    rows = await s.service.notice_rows()
    pending = [r for r in rows if r["kind"] == "error_alert" and not r["terminal_at"]]
    assert len(pending) == 1 and pending[0]["recipient_stream_id"] == "fixture:v2-next"
    assert pending[0]["body"].startswith("Work update digest.")
    assert json.loads(pending[0]["metadata"])["members"] == [nid]
    assert (await s.notify._db.call("error_get", nid))["firing_count"] == 1
