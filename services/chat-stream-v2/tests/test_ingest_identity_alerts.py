"""Identity refusal through real ingest, durable facts and FD notice delivery."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import ingest as module
from error_alerts_fixture import ErrorAlertsHarness
from test_ingest_identity_recovery import subject, binding, append, CHANGED
from test_ingest_provider_root_binding import BIRTH


@asynccontextmanager
async def lifecycle_subject(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=0.0, alive=True)
    monkeypatch.setattr(module, "monotonic", lambda: clock.now, raising=False)
    monkeypatch.setattr(module, "pid_alive", lambda pid: clock.alive, raising=False)
    h = ErrorAlertsHarness(tmp_path / "alert-core")
    h.pump_enabled = False
    await h.start()
    try:
        async with subject(tmp_path / "ingest", monkeypatch, store=h.store) as s:
            s.reader.alerts = h.alerts
            s.clock = clock
            s.alert_core = h
            yield s
    finally:
        await h.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy_first", [False, True])
async def test_unavailable_start_gap_recovers_without_fact_or_unproved_append(tmp_path, monkeypatch, healthy_first):
    async with lifecycle_subject(tmp_path, monkeypatch) as s:
        if healthy_first:
            assert await s.h.run(s.reader, s.state) == 1
            append(s.path, "codex", "two")
        before = await binding(s)
        s.h.birth = " "
        for now in (0, 2):
            s.clock.now = now
            assert await s.h.run(s.reader, s.state) == 0
            assert await s.alert_core.notify._db.call("error_rows") == []
            assert s.state.identity_incident_id is None
            assert await binding(s) == before
            assert len(await s.store.fetch_session_event_tail("h:v2-root", limit=500)) == int(healthy_first)
        s.h.birth = BIRTH
        assert await s.h.run(s.reader, s.state) == 1
        assert await s.h.run(s.reader, s.state) == 0
        assert await s.alert_core.notify._db.call("error_rows") == []
        assert s.alert_core.provider.pastes == []


@pytest.mark.asyncio
async def test_unavailable_start_dead_pid_while_row_open_is_silent(tmp_path, monkeypatch):
    async with lifecycle_subject(tmp_path, monkeypatch) as s:
        before = await binding(s)
        s.h.birth = ""
        s.clock.alive = False
        for now in (0, 10, 20, 30):
            s.clock.now = now
            assert await s.h.run(s.reader, s.state) == 0
            assert await s.alert_core.notify._db.call("error_rows") == []
            assert s.state.identity_incident_id is None
        assert (await s.store.fetch_session("h", "v2-root"))["status"] == "open"
        assert await binding(s) == before
        assert await s.store.fetch_session_event_tail("h:v2-root", limit=500) == []
        assert s.alert_core.provider.pastes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("times", [(0, 20, 20), (0, 1, 9.999, 10)])
async def test_unavailable_start_requires_distinct_attempts_and_elapsed_time(tmp_path, monkeypatch, times):
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", "on")
    async with lifecycle_subject(tmp_path, monkeypatch) as s:
        before = await binding(s)
        s.h.birth = ""
        for now in times[:-1]:
            s.clock.now = now
            assert await s.h.run(s.reader, s.state) == 0
            assert await s.alert_core.notify._db.call("error_rows") == []
            assert s.state.identity_incident_id is None
        s.clock.now = times[-1]
        assert await s.h.run(s.reader, s.state) == 0
        (fact,) = await s.alert_core.notify._db.call("error_rows")
        assert fact["error_context"]["stage"] == "proof_unavailable"
        for _ in range(2):
            assert await s.h.run(s.reader, s.state) == 0
        assert await s.alert_core.notify._db.call("error_get", fact["notification_id"]) == fact
        assert await binding(s) == before
        assert await s.store.fetch_session_event_tail("h:v2-root", limit=500) == []
        await s.alert_core.queue.drain_once()
        async with asyncio.timeout(2):
            while not s.alert_core.provider.pastes:
                await asyncio.sleep(0.01)
        assert len(s.alert_core.provider.pastes) == 1


@pytest.mark.asyncio
async def test_repeated_late_unavailable_refusals_accumulate_once_per_attempt(tmp_path, monkeypatch):
    async with lifecycle_subject(tmp_path, monkeypatch) as s:
        assert await s.h.run(s.reader, s.state) == 1
        append(s.path, "codex", "two")
        before = await binding(s)
        read = module._read_bound_span
        def lose_proof_during_read(*args):
            result = read(*args)
            s.h.birth = ""
            return result
        monkeypatch.setattr(module, "_read_bound_span", lose_proof_during_read)
        for i, now in enumerate((0, 5, 10), 1):
            s.h.birth = BIRTH  # entry proof is healthy on every attempt
            s.clock.now = now
            assert await s.h.run(s.reader, s.state) == 0
            facts = await s.alert_core.notify._db.call("error_rows")
            assert len(facts) == int(i == 3)
            assert len(await s.store.fetch_session_event_tail("h:v2-root", limit=500)) == 1
            assert await binding(s) == before
        monkeypatch.setattr(module, "_read_bound_span", read)
        s.h.birth = BIRTH
        assert await s.h.run(s.reader, s.state) == 1
        assert s.state.identity_grace is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["healthy", "recovery", "binding", "pid", "generation", "dead", "restart"])
async def test_unavailable_start_grace_resets_between_episodes(tmp_path, monkeypatch, reset):
    async with lifecycle_subject(tmp_path, monkeypatch) as s:
        assert await s.h.run(s.reader, s.state) == 1
        s.h.birth = ""
        for now in (0, 20):
            s.clock.now = now
            assert await s.h.run(s.reader, s.state) == 0
        assert await s.alert_core.notify._db.call("error_rows") == []
        before = await binding(s)
        if reset in {"healthy", "recovery"}:
            s.h.birth = BIRTH if reset == "healthy" else CHANGED
            assert await s.h.run(s.reader, s.state) == 0
            assert (await binding(s))["pane_started_at"] == s.h.birth
        elif reset in {"binding", "pid", "generation"}:
            changed = {**before, "pane_started_at": CHANGED}
            fields = {}
            if reset == "pid":
                s.h.pid = "8124"
                changed["pane_pid"] = "8124"
                fields["pane_pid"] = "8124"
            if reset == "generation":
                changed["generation"] = "gen-b"
                await s.store.update_session("h", "v2-root", status="closed")
                reopened = await s.h.sessions.open("h", "v2-root", provider="codex", pane_pid=s.h.pid,
                                                   session_generation="gen-b", observer_binding=changed)
                assert reopened["session_generation"] == "gen-b"
            else:
                await s.store.update_session("h", "v2-root", observer_binding=changed, **fields)
            await s.h.sessions.refresh()
        elif reset == "dead":
            s.clock.alive = False
            assert await s.h.run(s.reader, s.state) == 0
            s.clock.alive = True
        else:
            s.state = module._StreamIngest()
            s.reader = s.h.ingest()
            s.reader.alerts = s.alert_core.alerts
        s.h.birth = ""
        for now in (30, 50):
            s.clock.now = now
            assert await s.h.run(s.reader, s.state) == 0
            assert await s.alert_core.notify._db.call("error_rows") == []
        assert await s.h.run(s.reader, s.state) == 0
        assert len(await s.alert_core.notify._db.call("error_rows")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["close", "generation", "binding", "pid"])
async def test_deferred_unavailable_refusal_refetch_fences_retired_tuple(tmp_path, monkeypatch, race):
    async with lifecycle_subject(tmp_path, monkeypatch) as s:
        s.h.birth = ""
        for now in (0, 20):
            s.clock.now = now
            assert await s.h.run(s.reader, s.state) == 0
        fetch = s.store.fetch_session
        before = await binding(s)
        async def raced(host, name):
            fields = {"close": {"status": "closed"},
                      "binding": {"observer_binding": {**before, "executable": "/different/provider"}},
                      "pid": {"pane_pid": "8124"}}
            if race == "generation":
                await s.store.update_session(host, name, status="closed")
                await s.h.sessions.open(host, name, provider="codex", pane_pid=s.h.pid,
                                       session_generation="gen-b", observer_binding={**before, "generation": "gen-b"})
            else:
                await s.store.update_session(host, name, **fields[race])
            return await fetch(host, name)
        monkeypatch.setattr(s.store, "fetch_session", raced)
        assert await s.h.run(s.reader, s.state) == 0
        assert await s.alert_core.notify._db.call("error_rows") == []
        assert s.state.identity_grace is None
        assert s.state.identity_incident_id is None
        assert await s.store.fetch_session_event_tail("h:v2-root", limit=500) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", ["before_commit", "after_commit", "absent"])
async def test_eligible_unavailable_refusal_retries_sink_without_new_grace(tmp_path, monkeypatch, crash):
    async with lifecycle_subject(tmp_path, monkeypatch) as s:
        s.h.birth = ""
        async def lost(fact):
            if crash == "after_commit":
                await s.alert_core.alerts.error(fact)
            raise RuntimeError("synthetic producer failure")
        s.reader.alerts = None if crash == "absent" else SimpleNamespace(error=lost)
        for now in (0, 20, 20):
            s.clock.now = now
            assert await s.h.run(s.reader, s.state) == 0
        prior = await s.alert_core.notify._db.call("error_rows")
        assert len(prior) == int(crash == "after_commit")
        s.reader.alerts = s.alert_core.alerts
        assert await s.h.run(s.reader, s.state) == 0
        (fact,) = await s.alert_core.notify._db.call("error_rows")
        assert fact["firing_count"] == 1
        assert fact["error_context"]["operation_revision"] == 1
        if prior:
            assert fact == prior[0]
        await s.store.update_session("h", "v2-root", status="closed")
        assert await s.alert_core.notify._db.call("error_get", fact["notification_id"]) == fact


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "record-only", "on"])
async def test_eligible_unavailable_fact_retains_delivery_and_ack_contract(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", mode)
    async with lifecycle_subject(tmp_path, monkeypatch) as s:
        assert await s.h.run(s.reader, s.state) == 1
        s.h.birth = ""
        for now in (0, 20, 20):
            s.clock.now = now
            assert await s.h.run(s.reader, s.state) == 0
        (fact,) = await s.alert_core.notify._db.call("error_rows")
        await s.alert_core.queue.drain_once()
        if mode == "on":
            async with asyncio.timeout(2):
                while not s.alert_core.provider.pastes:
                    await asyncio.sleep(0.01)
            assert len(s.alert_core.provider.pastes) == 1
        else:
            assert s.alert_core.provider.pastes == []
        reply = await s.alert_core.notify.notification({"type": "notification.resolve", "request_id": "ack-lifecycle",
            "notification_id": fact["notification_id"], "action_kind": "ack"})
        assert reply["type"] == "notification.resolve.ok"
        acknowledged = await s.alert_core.notify._db.call("error_get", fact["notification_id"])
        assert acknowledged["state"] == "acked"
        s.h.birth = BIRTH
        assert await s.h.run(s.reader, s.state) == 0
        s.state = module._StreamIngest()  # producer restart gets a fresh grace
        s.h.birth = ""
        for now in (30, 50, 50):
            s.clock.now = now
            assert await s.h.run(s.reader, s.state) == 0
        assert len(await s.alert_core.notify._db.call("error_rows")) == 1
        assert await s.alert_core.notify._db.call("error_get", fact["notification_id"]) == acknowledged


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign_empty_start", "row_pid_changed"])
async def test_empty_start_outside_lifecycle_tuple_still_alerts_immediately(tmp_path, monkeypatch, case):
    async with lifecycle_subject(tmp_path, monkeypatch) as s:
        s.h.birth = ""
        if case == "foreign_empty_start":
            s.h.command = "/different/provider"
        else:
            s.h.pid = "8124"
            await s.store.update_session("h", "v2-root", pane_pid="8124")
            await s.h.sessions.refresh()
        # A known mismatch must not be hidden even when the old process is gone.
        s.clock.alive = False
        assert await s.h.run(s.reader, s.state) == 0
        (fact,) = await s.alert_core.notify._db.call("error_rows")
        assert fact["error_context"]["stage"] == ("foreign_identity" if case == "foreign_empty_start" else "proof_unavailable")
        assert s.state.identity_incident_id


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
