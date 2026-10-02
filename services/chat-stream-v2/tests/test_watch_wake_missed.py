"""Wakes survive fleet-card faults; an overdue wake raises one delivered alert.

Incident 2026-10-02: a lane card cleared with `status --eta none` made every
tick raise in `_fleet_overrun`, rolling back due wakes for ~19h.
"""
import asyncio
import json
import os
import tempfile
from datetime import datetime

from ledger import apply_status_card_update
from outbound_notices import OutboundNoticeConfig, OutboundNoticeQueue
from sessions import Sessions
from store import Store
from store_watch_wake import _fleet_overrun
from watch_wake import WatchWake, run_reconcile_callbacks

CLEARED = apply_status_card_update(
    apply_status_card_update(None, {"eta": "30m"}, now_iso="2026-10-02T03:00:00Z"),
    {"eta": "none"}, now_iso="2026-10-02T03:50:00Z",
)


class _RecordingComms:
    def __init__(self):
        self.calls = []

    async def deliver_outbound_notice(self, message, *, check_existing=False):
        self.calls.append(dict(message))
        return {"type": "tell.ok", "delivery_status": "delivered", "submission_confirmed": True,
                "delivery_ack_at": "2026-10-02T12:00:00Z"}


async def _fleet(store, sessions):
    """A fleet root with one lane whose card has a cleared ETA (the live shape)."""
    root = await sessions.open("local", "root", provider="shell", no_watch=True)
    await store.update_session("local", "root", pane_status="pane_alive")
    lead = await sessions.open("local", "lead", provider="shell", role="lead", no_watch=True,
                               parent_stream_id="local:root")
    await store.update_session("local", "lead", status_card=json.dumps(CLEARED))
    binding = ("local:root", root["session_generation"])
    obs = {"local:root": {"session_generation": root["session_generation"], "online": True,
                          "pane_status": "pane_alive"},
           "local:lead": {"session_generation": lead["session_generation"], "working": False}}
    return root, binding, obs


async def _notices(store, kind):
    return await store.submit(lambda c: [dict(r) for r in c.execute(
        "SELECT * FROM v2_outbound_notices WHERE kind=? ORDER BY created_at", (kind,))])


def test_cleared_or_malformed_eta_is_no_overrun():
    assert CLEARED["eta_at"] is None and CLEARED["eta_set_at"] is None
    for card in (CLEARED, {}, {"eta_set_at": "2026-10-02T03:00:00Z", "eta_at": None},
                 {"eta_set_at": 5, "eta_at": "2026-10-02T03:30:00Z"}, {"eta_set_at": "bogus", "eta_at": "x"}):
        assert _fleet_overrun(card, 1790950000) is None
    valid = {"eta_set_at": "2026-10-02T03:00:00Z", "eta_at": "2026-10-02T03:10:00Z"}
    at_20 = datetime.fromisoformat("2026-10-02T03:20:00+00:00").timestamp()
    assert _fleet_overrun(valid, at_20) == 100.0


def test_due_wake_fires_despite_cleared_lane_eta():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="local")
            root, binding, obs = await _fleet(store, sessions)
            gen = root["session_generation"]
            await store.register_watch_wake("wake", "local:root", gen,
                                            {"request_id": "w", "due_at": 110, "note": "resume", "urgent": False}, now=100)
            assert await store.evaluate_watch_wake(obs, now=109, root_binding=binding) == 0
            await store.evaluate_watch_wake(obs, now=110, root_binding=binding)
            await store.evaluate_watch_wake(obs, now=111, root_binding=binding)
            assert len(await _notices(store, "wake")) == 1
            assert (await store.list_watch_wake("wake", "local:root", gen))[0]["state"] == "consumed"
        finally:
            store.stop()
    asyncio.run(run())


def test_fleet_failure_rolls_back_only_fleet_work(monkeypatch, caplog):
    """SAVEPOINT isolation, independent of the null-ETA input fix."""
    import store_watch_wake

    def boom(conn, *args, **kwargs):
        conn.execute("INSERT INTO v2_fleet_switch_state VALUES ('probe', 1, 0)")
        raise RuntimeError("fleet fault")

    monkeypatch.setattr(store_watch_wake, "_evaluate_fleet_conn", boom)

    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="local")
            root, binding, obs = await _fleet(store, sessions)
            await store.register_watch_wake("wake", "local:root", root["session_generation"],
                                            {"request_id": "w", "due_at": 110, "note": "", "urgent": False}, now=100)
            assert await store.evaluate_watch_wake(obs, now=110, root_binding=binding) == 1
            assert len(await _notices(store, "wake")) == 1
            assert (await store.list_watch_wake("wake", "local:root", root["session_generation"]))[0]["state"] == "consumed"
            probe = await store.submit(lambda c: c.execute(
                "SELECT COUNT(*) FROM v2_fleet_switch_state WHERE kind='probe'").fetchone()[0])
            assert probe == 0  # the fleet write rolled back with its savepoint
            assert "fleet evaluation failed" in caplog.text
        finally:
            store.stop()
    asyncio.run(run())


def test_missed_wake_alarm_delivered_once_across_restart(monkeypatch):
    """Scheduler stalled: alarm fires after grace, once, survives restart; the late
    wake still fires exactly once when the scheduler recovers."""
    import store_watch_wake

    def stalled(conn, *args, **kwargs):
        raise RuntimeError("scheduler stalled")

    async def phase(path, clock, *, stall, deliver=False):
        store = Store(path)
        store.start()
        try:
            sessions = Sessions(store, local_host="local")
            comms = _RecordingComms()
            outbound = OutboundNoticeQueue(store, comms, config=OutboundNoticeConfig(lease_s=0.1, max_attempts=3))
            ww = WatchWake(store, sessions, outbound, clock=lambda: clock[0])
            if stall:
                # Stall the whole wake pass the way the live AttributeError did.
                monkeypatch.setattr(store_watch_wake, "_evaluate_conn", stalled)
            else:
                monkeypatch.undo()
            await run_reconcile_callbacks(ww.missed_wake_alarm, ww.tick)  # main.py order
            if deliver:
                for notice in await _notices(store, "wake_missed"):
                    await outbound.deliver_now(notice["notice_id"])
            return comms.calls, await _notices(store, "wake_missed"), await _notices(store, "wake")
        finally:
            store.stop()

    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.db")
            store = Store(path)
            store.start()
            sessions = Sessions(store, local_host="local")
            owner = await sessions.open("local", "owner", provider="shell", no_watch=True)
            await store.register_watch_wake("wake", "local:owner", owner["session_generation"],
                                            {"request_id": "w", "due_at": 1000, "note": "E1 window", "urgent": False},
                                            now=100)
            store.stop()

            clock = [1000 + 299]  # due but inside grace: no alarm yet
            _, missed, wakes = await phase(path, clock, stall=True)
            assert missed == [] and wakes == []
            clock[0] = 1000 + 301
            calls, missed, wakes = await phase(path, clock, stall=True, deliver=True)
            assert len(missed) == 1 and wakes == []
            assert missed[0]["recipient_stream_id"] == "local:owner"
            assert missed[0]["delivered_at"] is not None
            assert len(calls) == 1 and calls[0]["urgent"] is False
            assert "Missed wake" in calls[0]["message"] and "E1 window" in calls[0]["message"]
            clock[0] = 1000 + 900  # restart, still stalled: no duplicate alarm
            _, missed, wakes = await phase(path, clock, stall=True)
            assert len(missed) == 1 and wakes == []
            clock[0] = 1000 + 960  # recovered: the wake itself fires once, no new alarm
            _, missed, wakes = await phase(path, clock, stall=False)
            assert len(missed) == 1 and len(wakes) == 1
            _, missed, wakes = await phase(path, clock, stall=False)
            assert len(missed) == 1 and len(wakes) == 1
    asyncio.run(run())


def test_late_recovery_pass_alerts_and_fires_once():
    """Daemon down past due+grace: the first pass both alerts and fires, once."""
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="local")
            comms = _RecordingComms()
            outbound = OutboundNoticeQueue(store, comms, config=OutboundNoticeConfig(lease_s=0.1, max_attempts=3))
            clock = [100]
            ww = WatchWake(store, sessions, outbound, clock=lambda: clock[0])
            owner = await sessions.open("local", "owner", provider="shell", no_watch=True)
            await store.register_watch_wake("wake", "local:owner", owner["session_generation"],
                                            {"request_id": "w", "due_at": 1000, "note": "late", "urgent": False}, now=100)
            clock[0] = 1000 + 600
            await run_reconcile_callbacks(ww.missed_wake_alarm, ww.tick)
            await run_reconcile_callbacks(ww.missed_wake_alarm, ww.tick)
            missed, wakes = await _notices(store, "wake_missed"), await _notices(store, "wake")
            assert len(missed) == 1 and len(wakes) == 1
            for notice in missed + wakes:
                assert await outbound.deliver_now(notice["notice_id"]) is True
            assert sorted(c["message"].split("]")[-1].split(":")[0].strip() for c in comms.calls) == ["Missed wake", "Timed wake"]
        finally:
            store.stop()
    asyncio.run(run())


def test_on_time_wake_never_alerts():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="local")
            clock = [100]
            ww = WatchWake(store, sessions, clock=lambda: clock[0])
            owner = await sessions.open("local", "owner", provider="shell", no_watch=True)
            await store.register_watch_wake("wake", "local:owner", owner["session_generation"],
                                            {"request_id": "w", "due_at": 1000, "note": "", "urgent": False}, now=100)
            for t in (1000, 1070, 1400, 5000):
                clock[0] = t
                await run_reconcile_callbacks(ww.missed_wake_alarm, ww.tick)
            assert await _notices(store, "wake_missed") == [] and len(await _notices(store, "wake")) == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_no_alarm_for_fired_cancelled_or_dead_owner_wakes():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="local")
            owner = await sessions.open("local", "owner", provider="shell", no_watch=True)
            gone = await sessions.open("local", "gone", provider="shell", no_watch=True)
            gen = owner["session_generation"]
            for rid in ("fired", "cancelled"):
                await store.register_watch_wake("wake", "local:owner", gen,
                                                {"request_id": rid, "due_at": 1000, "note": "", "urgent": False}, now=100)
            await store.register_watch_wake("wake", "local:gone", gone["session_generation"],
                                            {"request_id": "dead", "due_at": 1000, "note": "", "urgent": False}, now=100)
            cancelled = [r for r in await store.list_watch_wake("wake", "local:owner", gen) if r["request_id"] == "cancelled"][0]
            await store.cancel_watch_wake("wake", "local:owner", gen, cancelled["id"], request_id="c1")
            await store.update_session("local", "gone", status="closed")
            await store.evaluate_watch_wake({}, now=1000)  # fires "fired"; retires the dead owner's wake
            assert await store.missed_wake_alarm(now=5000, grace_s=300) == 0
            assert await _notices(store, "wake_missed") == []
        finally:
            store.stop()
    asyncio.run(run())
