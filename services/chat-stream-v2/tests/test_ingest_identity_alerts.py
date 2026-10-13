"""Identity refusal through real ingest, durable facts and FD notice delivery."""
import asyncio
from types import SimpleNamespace

import pytest
from error_alerts_fixture import ErrorAlertsHarness
from test_ingest_identity_recovery import subject, binding, CHANGED
from test_ingest_provider_root_binding import BIRTH


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "record-only", "on"])
async def test_blocked_incident_first_write_wins_and_delivers_existing_notice(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", mode)
    h = ErrorAlertsHarness(tmp_path / "alert-core")
    h.pump_enabled = False
    await h.start()
    try:
        async with subject(tmp_path / "ingest", monkeypatch, store=h.store) as s:
            s.reader.alerts = h.alerts
            assert await s.h.run(s.reader, s.state) == 1
            assert await s.h.run(s.reader, s.state) == 0  # Healthy idle is silent.
            assert await h.notify._db.call("error_rows") == []
            before = await binding(s)
            s.h.birth = CHANGED; s.h.descriptors = []
            assert await s.h.run(s.reader, s.state) == 0  # Visible even without pending bytes.
            (fact,) = await h.notify._db.call("error_rows")
            ctx = fact["error_context"]
            assert (ctx["family"], ctx["code"], ctx["stage"]) == ("integrity", "ingest_identity_blocked", "proof_unavailable")
            assert ctx["episode_id"] == s.state.identity_incident_id
            assert len(ctx["episode_id"].split(":")[1]) == 64
            for _ in range(2): assert await s.h.run(s.reader, s.state) == 0
            assert await h.notify._db.call("error_get", fact["notification_id"]) == fact
            await h.queue.drain_once()
            if mode == "on":
                async with asyncio.timeout(2):
                    while not h.provider.pastes: await asyncio.sleep(0.01)
                assert len(h.provider.pastes) == 1
                assert "BLOCKER integrity ingest_identity_blocked" in h.provider.pastes[0]
                assert ctx["episode_id"] in h.provider.pastes[0]
                for private in ("h:v2-root", "gen-a", BIRTH, CHANGED, str(s.path)):
                    assert private not in h.provider.pastes[0]
            else:
                assert h.provider.pastes == []
            delivered = await h.notify._db.call("error_get", fact["notification_id"])
            assert delivered["firing_count"] == 1
            assert delivered["error_context"]["operation_revision"] == 1
            fact = delivered
            # Same tuple after a healthy pass remains a historical incident.
            s.h.birth = BIRTH
            assert await s.h.run(s.reader, s.state) == 0
            assert s.state.identity_incident_id is None
            s.h.birth = CHANGED
            assert await s.h.run(s.reader, s.state) == 0
            assert await h.notify._db.call("error_get", fact["notification_id"]) == fact
            # Normal notification acknowledgement cannot be reset by a retry.
            reply = await h.notify.notification({"type": "notification.resolve", "request_id": "ack-incident",
                "notification_id": fact["notification_id"], "action_kind": "ack"})
            assert reply["type"] == "notification.resolve.ok"
            acknowledged = await h.notify._db.call("error_get", fact["notification_id"])
            assert acknowledged["state"] == "acked"
            # Reason changes do not create or revise an episode.
            s.h.command = "/other/provider"
            assert await s.h.run(s.reader, s.state) == 0
            assert await h.notify._db.call("error_get", fact["notification_id"]) == acknowledged
            assert await binding(s) == before
            # A producer restart sees the same durable fact (no new revision).
            fresh = s.h.ingest(); fresh.alerts = h.alerts
            assert await s.h.run(fresh, s.state) == 0
            assert await h.notify._db.call("error_get", fact["notification_id"]) == acknowledged
            await s.store.update_session("h", "v2-root", status="closed")
            await h.queue.drain_once()
            assert await h.notify._db.call("error_get", fact["notification_id"]) == acknowledged
    finally:
        await h.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", ["before_commit", "after_commit", "absent"])
async def test_sink_loss_retries_at_normal_cadence_without_duplicate_fact(tmp_path, monkeypatch, crash, caplog):
    h = ErrorAlertsHarness(tmp_path / "alert-core"); h.pump_enabled = False
    await h.start()
    try:
        async with subject(tmp_path / "ingest", monkeypatch, store=h.store) as s:
            assert await s.h.run(s.reader, s.state) == 1
            s.h.birth = CHANGED; s.h.descriptors = []
            async def lost(fact):
                if crash == "after_commit": await h.alerts.error(fact)
                raise RuntimeError("synthetic producer failure")
            s.reader.alerts = None if crash == "absent" else SimpleNamespace(error=lost)
            assert await s.h.run(s.reader, s.state) == 0
            assert "sink_" in caplog.text
            before = await h.notify._db.call("error_rows")
            assert len(before) == (1 if crash == "after_commit" else 0)
            s.reader.alerts = h.alerts
            assert await s.h.run(s.reader, s.state) == 0
            (fact,) = await h.notify._db.call("error_rows")
            assert fact["firing_count"] == 1 and fact["error_context"]["operation_revision"] == 1
            if before: assert fact == before[0]
            # New observed start is a distinct historical incident.
            s.h.birth = "Wed Sep 9 11:00:08 2026"
            assert await s.h.run(s.reader, s.state) == 0
            assert len(await h.notify._db.call("error_rows")) == 2
    finally:
        await h.stop()


@pytest.mark.asyncio
async def test_close_before_diagnostic_handoff_is_warning_only(tmp_path, monkeypatch):
    async with subject(tmp_path, monkeypatch) as s:
        assert await s.h.run(s.reader, s.state) == 1
        s.h.birth = CHANGED; s.h.descriptors = []
        fetch = s.store.fetch_session
        async def close_then_fetch(host, name):
            await s.store.update_session(host, name, status="closed")
            return await fetch(host, name)
        monkeypatch.setattr(s.store, "fetch_session", close_then_fetch)
        async def forbidden(fact): pytest.fail("retired generation cannot emit a stale diagnostic")
        s.reader.alerts = SimpleNamespace(error=forbidden)
        assert await s.h.run(s.reader, s.state) == 0
        assert s.state.identity_incident_id is None


@pytest.mark.asyncio
async def test_fact_survives_alert_core_restart_and_closed_session(tmp_path, monkeypatch):
    h = ErrorAlertsHarness(tmp_path / "alert-core"); h.pump_enabled = False
    await h.start()
    try:
        async with subject(tmp_path / "ingest", monkeypatch, store=h.store) as s:
            assert await s.h.run(s.reader, s.state) == 1
            s.reader.alerts = h.alerts; s.h.birth = CHANGED; s.h.descriptors = []
            assert await s.h.run(s.reader, s.state) == 0
            (fact,) = await h.notify._db.call("error_rows")
            await h.restart()
            s.store = s.h.store = h.store
            s.h.sessions.store = h.store
            reader = s.h.ingest(); reader.alerts = h.alerts
            assert await s.h.run(reader, s.state) == 0
            assert await h.notify._db.call("error_get", fact["notification_id"]) == fact
            await h.store.update_session("h", "v2-root", status="closed")
            assert await h.notify._db.call("error_get", fact["notification_id"]) == fact
    finally:
        await h.stop()


@pytest.mark.asyncio
async def test_late_preappend_root_refusal_is_visible_in_same_pass(tmp_path, monkeypatch):
    async with subject(tmp_path, monkeypatch) as s:
        opened = __import__('ingest')._open_descriptor
        def change_during_open(path):
            fd = opened(path)
            s.h.birth = CHANGED; s.h.descriptors = []
            return fd
        monkeypatch.setattr('ingest._open_descriptor', change_during_open)
        facts = []
        async def emit(fact): facts.append(fact); return "notification"
        s.reader.alerts = SimpleNamespace(error=emit)
        assert await s.h.run(s.reader, s.state) == 0
        assert len(facts) == 1 and facts[0].code == "ingest_identity_blocked"
        assert await s.store.fetch_session_event_tail("h:v2-root", limit=500) == []
