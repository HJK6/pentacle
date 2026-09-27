"""Fail-first lane journey for tree idle and material digest notices."""
import asyncio

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from sessions import Sessions
from store import Store
from ledger import apply_status_card_update, StatusCardError
import pytest
from message_envelopes import build_message_envelope, match_message_envelope
from outbound_notices import OutboundNoticeQueue
from store_watch_wake import _fleet_overrun, _fleet_reports
from watch_wake import WatchWake


def _live_root(root):
    return {"session_generation": root["session_generation"],
            "online": True, "pane_status": "pane_alive"}


def test_fleet_envelopes_round_trip():
    nid = "d2:" + "c" * 64
    tree = build_message_envelope(
        "tree_idle", notice_id=nid, lane_stream_id="hosta:lead",
        open_seats=2, oldest_idle_s=1200, open_terminal_reports=1,
    )
    assert match_message_envelope(tree) == {
        "kind": "tree_idle", "id": nid, "lane_stream_id": "hosta:lead",
        "open_seats": 2, "oldest_idle_s": 1200, "open_terminal_reports": 1,
    }
    digest = build_message_envelope(
        "lane_digest", notice_id=nid, evaluated_at="2026-09-27T05:00:00Z",
        lanes=[{"stream_id": "hosta:lead", "working": False}],
    )
    assert match_message_envelope(digest)["lanes"][0]["stream_id"] == "hosta:lead"
    assert match_message_envelope(digest + " trailing") is None
    spoof = tree[:-1] + ',"kind":"child_report_ready","id":"spoofed"}'
    assert match_message_envelope(spoof)["kind"] == "tree_idle"
    assert match_message_envelope(spoof)["id"] == nid
    invalid = build_message_envelope(
        "tree_idle", notice_id=nid, lane_stream_id="hosta:lead",
        open_seats=True, oldest_idle_s=1200, open_terminal_reports=1,
    )
    assert match_message_envelope(invalid) is None


def test_eta_baseline_survives_unrelated_card_update_and_clear():
    now = "2026-09-27T05:00:00Z"
    card = apply_status_card_update(None, {"eta": "30m"}, now_iso=now)
    assert card["eta_at"] == "2026-09-27T05:30:00Z"
    assert card["eta_set_at"] == now
    updated = apply_status_card_update(card, {"update": "working"}, now_iso="2026-09-27T05:10:00Z")
    assert (updated["eta_at"], updated["eta_set_at"]) == (card["eta_at"], now)
    cleared = apply_status_card_update(updated, {"eta": "none"}, now_iso="2026-09-27T05:11:00Z")
    assert cleared["eta_at"] is None and cleared["eta_set_at"] is None
    with pytest.raises(StatusCardError):
        apply_status_card_update(card, {"eta": "2026-09-27T05:50:00"}, now_iso=now)


def test_eta_replacement_offset_and_invalid_values_preserve_prior_card():
    card = apply_status_card_update(None, {"eta": "30m"}, now_iso="2026-09-27T05:00:00Z")
    replacement = apply_status_card_update(
        card, {"eta": "2026-09-27T02:40:00-03:00"}, now_iso="2026-09-27T05:10:00Z",
    )
    assert replacement["eta_at"] == "2026-09-27T05:40:00Z"
    assert replacement["eta_set_at"] == "2026-09-27T05:10:00Z"
    assert card["eta_at"] == "2026-09-27T05:30:00Z"
    baseline = dict(replacement)
    for invalid in ("0m", "-5m", "2026-09-27T05:05:00Z",
                    "2026-09-27T05:50:00", "bogus"):
        with pytest.raises(StatusCardError):
            apply_status_card_update(replacement, {"eta": invalid},
                                     now_iso="2026-09-27T05:10:00Z")
        assert replacement == baseline


def test_eta_overrun_exact_boundary():
    card = {"eta_set_at": "2026-09-27T05:00:00Z", "eta_at": "2026-09-27T05:10:00Z"}
    assert _fleet_overrun(card, 1790486099.4) < 50
    assert _fleet_overrun(card, 1790486100) == 50


def test_working_interval_and_short_turn_reset_tree_episode():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                       no_watch=True, parent_stream_id="hosta:root")
            binding = ("hosta:root", root["session_generation"])
            obs = {"hosta:lead": {"session_generation": lead["session_generation"], "working": True}}
            obs["hosta:root"] = _live_root(root)
            await store.evaluate_watch_wake(obs, now=100, root_binding=binding)
            await store.evaluate_watch_wake(obs, now=1600, root_binding=binding)
            obs["hosta:lead"]["working"] = False
            await store.evaluate_watch_wake(obs, now=1601, root_binding=binding)
            await store.evaluate_watch_wake(obs, now=2800, root_binding=binding)
            assert await count(store) == 0
            await store.evaluate_watch_wake(obs, now=2801, root_binding=binding)
            assert await count(store) == 1
            obs["hosta:lead"]["watch_working_at"] = 3990
            await store.evaluate_watch_wake(obs, now=4000, root_binding=binding)
            await store.evaluate_watch_wake(obs, now=5199, root_binding=binding)
            assert await count(store) == 1
            await store.evaluate_watch_wake(obs, now=5200, root_binding=binding)
            assert await count(store) == 2
        finally:
            store.stop()

    async def count(store):
        return await store.submit(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
        ).fetchone()[0])

    asyncio.run(run())


def test_report_proxy_and_disable_switch(monkeypatch):
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                       no_watch=True, parent_stream_id="hosta:root")
            binding = ("hosta:root", root["session_generation"])
            await store.submit(lambda conn: conn.execute(
                """INSERT INTO v2_reports
                   (report_id,from_stream_id,msg_id,session_generation,status,ingested_at,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                ("report-1", "hosta:lead", 0, lead["session_generation"], "done",
                 "2026-09-27T05:00:00Z", 100.0),
            ))
            await OutboundNoticeQueue(store).enqueue(
                kind="report", dedupe_key="report:report-1",
                recipient_stream_id="hosta:root", source_stream_id="hosta:lead",
                tell_id="report-notice-1", body="child report ready",
                metadata={"report_id": "report-1"},
            )
            obs = {"hosta:lead": {"session_generation": lead["session_generation"], "working": False}}
            obs["hosta:root"] = _live_root(root)
            await store.evaluate_watch_wake(obs, now=100, root_binding=binding)
            await store.evaluate_watch_wake(obs, now=1300, root_binding=binding)
            notice = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()))
            assert match_message_envelope(notice["body"])["open_terminal_reports"] == 1
            assert await store.submit(lambda conn: conn.execute(
                "SELECT delivered_at FROM v2_outbound_notices WHERE kind='report'"
            ).fetchone()[0]) is None
            monkeypatch.setenv("PENTACLE_TREE_IDLE_S", "0")
            await store.evaluate_watch_wake(obs, now=1301, root_binding=binding)
            terminal = await store.submit(lambda conn: conn.execute(
                "SELECT terminal_reason FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0])
            assert terminal == "fleet_disabled"
            monkeypatch.delenv("PENTACLE_TREE_IDLE_S")
            await store.evaluate_watch_wake(obs, now=1302, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 2
            def report_proxy(conn):
                members = {
                    f"{row['host']}:{row['session_name']}": {"session_generation": row["generation"]}
                    for row in conn.execute(
                        """SELECT s.host,s.session_name,g.generation FROM sessions s
                           JOIN v2_session_generations g USING(host,session_name)
                           WHERE s.status='open'"""
                    )
                }
                return _fleet_reports(conn, members)
            # Delivery and await have no durable "reviewed" meaning for reports.
            await store.submit(lambda conn: conn.execute(
                "UPDATE v2_outbound_notices SET delivered_at=? WHERE kind IN ('report','tree_idle') AND terminal_at IS NULL",
                ("2026-09-27T05:00:00Z",),
            ))
            assert (await store.submit(report_proxy))["hosta:lead"] == 1
            awaited = await store.register_awaiter("hosta:lead", None)
            assert awaited["outcome"] == "report"
            assert (await store.submit(report_proxy))["hosta:lead"] == 1
            monkeypatch.setenv("PENTACLE_TREE_IDLE_S", "0")
            await store.evaluate_watch_wake(obs, now=1303, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT terminal_at FROM v2_outbound_notices WHERE kind='tree_idle' AND delivered_at IS NOT NULL"
            ).fetchone()[0]) is None
            await store.mark_closed("hosta", "lead", closed_at="2026-09-27T05:01:00Z", pane_status="pane_dead")
            assert "hosta:lead" not in await store.submit(report_proxy)
        finally:
            store.stop()

    asyncio.run(run())


def test_digest_emits_once_on_eta_threshold_crossing(monkeypatch):
    monkeypatch.setenv("PENTACLE_LANE_DIGEST_S", "5")

    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                       no_watch=True, parent_stream_id="hosta:root")
            await store.update_session("hosta", "lead", status_card={
                "eta_set_at": "1970-01-01T00:01:40Z",
                "eta_at": "1970-01-01T00:02:10Z",
                "updated_at": "1970-01-01T00:01:40Z",
            })
            binding = ("hosta:root", root["session_generation"])
            obs = {"hosta:lead": {"session_generation": lead["session_generation"], "working": False}}
            obs["hosta:root"] = _live_root(root)
            for now in (100, 105, 110, 140):
                await store.evaluate_watch_wake(obs, now=now, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='lane_digest'"
            ).fetchone()[0]) == 1
            await store.evaluate_watch_wake(obs, now=145, root_binding=binding)
            bodies = await store.submit(lambda conn: [row[0] for row in conn.execute(
                "SELECT body FROM v2_outbound_notices WHERE kind='lane_digest' ORDER BY created_at"
            )])
            assert len(bodies) == 2
            assert match_message_envelope(bodies[0])["lanes"][0]["eta_overrun_50"] is False
            assert match_message_envelope(bodies[1])["lanes"][0]["eta_overrun_50"] is True
        finally:
            store.stop()

    asyncio.run(run())


def test_stale_generation_and_offline_root_defer_idle_notice():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                       no_watch=True, parent_stream_id="hosta:root")
            binding = ("hosta:root", root["session_generation"])
            obs = {"hosta:lead": {"session_generation": lead["session_generation"], "working": False}}
            obs["hosta:root"] = _live_root(root)
            await store.evaluate_watch_wake(obs, now=100, root_binding=binding)
            obs["hosta:lead"]["session_generation"] = "stale"
            await store.evaluate_watch_wake(obs, now=1300, root_binding=binding)
            obs["hosta:lead"]["session_generation"] = lead["session_generation"]
            await store.update_session("hosta", "root", offline_since_ts="1970-01-01T00:00:00Z")
            await store.evaluate_watch_wake(obs, now=2500, root_binding=binding)
            await store.update_session("hosta", "root", offline_since_ts=None)
            await store.evaluate_watch_wake(obs, now=2501, root_binding=binding)
            await store.evaluate_watch_wake(obs, now=3700, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 0
            await store.evaluate_watch_wake(obs, now=3701, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_tree_idle_and_changed_digest_journey():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", role="assistant", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead", no_watch=True,
                                       parent_stream_id="hosta:root")
            child = await sessions.open("hostb", "child", provider="shell", role="worker", no_watch=True,
                                        parent_stream_id="hosta:lead", visibility="hidden")
            observations = {
                "hosta:root": _live_root(root),
                "hosta:lead": {"session_generation": lead["session_generation"], "working": False},
                "hostb:child": {"session_generation": child["session_generation"], "working": False},
            }
            binding = ("hosta:root", root["session_generation"])
            await store.evaluate_watch_wake(observations, now=100, root_binding=binding)
            await store.evaluate_watch_wake(observations, now=1299, root_binding=binding)
            assert not await notices(store, "tree_idle")
            await store.evaluate_watch_wake(observations, now=1300, root_binding=binding)
            assert len(await notices(store, "tree_idle")) == 1
            await store.evaluate_watch_wake(observations, now=1900, root_binding=binding)
            assert len(await notices(store, "lane_digest")) == 1
            await store.evaluate_watch_wake(observations, now=3700, root_binding=binding)
            assert len(await notices(store, "lane_digest")) == 1
            await store.update_session("hosta", "lead", title="New title")
            await store.evaluate_watch_wake(observations, now=5500, root_binding=binding)
            assert len(await notices(store, "lane_digest")) == 2
        finally:
            store.stop()

    async def notices(store, kind):
        return await store.submit(lambda conn: [dict(row) for row in conn.execute(
            "SELECT * FROM v2_outbound_notices WHERE kind=? ORDER BY created_at", (kind,)
        )])

    asyncio.run(run())


def test_restart_keeps_fired_episode_and_digest_deadline(tmp_path):
    async def run():
        db = str(tmp_path / "fleet.db")
        store = Store(db)
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                       no_watch=True, parent_stream_id="hosta:root")
            binding = ("hosta:root", root["session_generation"])
            observations = {"hosta:lead": {
                "session_generation": lead["session_generation"], "working": False,
            }}
            observations["hosta:root"] = _live_root(root)
            for now in (100, 1300, 1900):
                await store.evaluate_watch_wake(observations, now=now, root_binding=binding)
        finally:
            store.stop()
        store = Store(db)
        store.start()
        try:
            for now in (1901, 3100, 3700):
                await store.evaluate_watch_wake(observations, now=now, root_binding=binding)
            counts = await store.submit(lambda conn: dict(conn.execute(
                "SELECT kind,COUNT(*) FROM v2_outbound_notices GROUP BY kind"
            ).fetchall()))
            assert counts == {"tree_idle": 1, "lane_digest": 1}
            await store.update_session("hosta", "lead", title="Changed after restart")
            await store.evaluate_watch_wake(observations, now=5500, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='lane_digest'"
            ).fetchone()[0]) == 2
        finally:
            store.stop()

    asyncio.run(run())


def test_rebind_rejects_old_root_notice_and_starts_new_root_episode():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            old_root = await sessions.open("hosta", "old-root", provider="shell", no_watch=True)
            new_root = await sessions.open("hosta", "new-root", provider="shell", no_watch=True)
            await store.update_session("hosta", "old-root", pane_status="pane_alive")
            await store.update_session("hosta", "new-root", pane_status="pane_alive")
            old_lead = await sessions.open("hosta", "old-lead", provider="shell", role="lead",
                                           no_watch=True, parent_stream_id="hosta:old-root")
            old_binding = ("hosta:old-root", old_root["session_generation"])
            new_binding = ("hosta:new-root", new_root["session_generation"])
            observations = {"hosta:old-lead": {
                "session_generation": old_lead["session_generation"], "working": False,
            }}
            observations["hosta:old-root"] = _live_root(old_root)
            observations["hosta:new-root"] = _live_root(new_root)
            await store.evaluate_watch_wake(observations, now=100, root_binding=old_binding)
            await store.evaluate_watch_wake(observations, now=1300, root_binding=old_binding)
            old_notice = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()))
            effective = [old_binding]
            wake = WatchWake(store, sessions, root_binding=lambda: effective[0])
            sessions.apply_live("hosta:old-root", online=True, pane_status="pane_alive")
            sessions.apply_live("hosta:new-root", online=True, pane_status="pane_alive")
            assert await wake.delivery_guard(old_notice) is None
            effective[0] = new_binding
            assert (await wake.delivery_guard(old_notice)).action == "terminal"
            new_lead = await sessions.open("hosta", "new-lead", provider="shell", role="lead",
                                           no_watch=True, parent_stream_id="hosta:new-root")
            observations["hosta:new-lead"] = {
                "session_generation": new_lead["session_generation"], "working": False,
            }
            await store.evaluate_watch_wake(observations, now=1301, root_binding=new_binding)
            await store.evaluate_watch_wake(observations, now=2501, root_binding=new_binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 2
        finally:
            store.stop()

    asyncio.run(run())


def test_durable_hot_rebind_moves_notice_root_while_old_root_remains_open():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            old_root = await sessions.open(
                "hosta", "old-root", provider="codex", role="assistant", no_watch=True,
                pane_status="pane_alive", effective_model="gpt-6-sol", effective_effort="high",
            )
            new_root = await sessions.open(
                "hosta", "new-root", provider="codex", role="assistant", no_watch=True,
                pane_status="pane_alive", effective_model="gpt-6-sol", effective_effort="high",
            )
            for sid in ("hosta:old-root", "hosta:new-root"):
                sessions.apply_live(sid, online=True, pane_status="pane_alive")
            old_lead = await sessions.open(
                "hosta", "old-lead", provider="shell", role="lead", no_watch=True,
                parent_stream_id="hosta:old-root",
            )
            sessions.apply_live("hosta:old-lead", working=False, capture_liveness="idle",
                                capture_generation=old_lead["session_generation"])
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "hosta:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": "hosta:old-root",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": old_root["session_generation"],
            })
            composite = AssistantComposite(store, config=config)
            await composite.load_binding()
            current = [100]
            watch = WatchWake(
                store, sessions, clock=lambda: current[0],
                root_binding=lambda: (
                    (composite.config.direct_primary_stream_id,
                     composite.config.direct_primary_generation)
                    if composite.config.direct_primary else None
                ),
            )
            await watch.tick()
            current[0] = 1300
            await watch.tick()
            old_notice = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()))
            assert old_notice["recipient_stream_id"] == "hosta:old-root"
            receipt = await composite.rebind({
                "type": "assistant.rebind", "request_id": "move-notice-root",
                "expected_revision": 0, "target_stream_id": "hosta:new-root", "clear": False,
            }, actor_stream_id="hosta:old-root")
            assert receipt["new_binding"]["generation"] == new_root["session_generation"]
            assert composite.config.direct_primary_stream_id == "hosta:new-root"
            assert (await watch.delivery_guard(old_notice)).action == "terminal"
            assert (await store.fetch_session("hosta", "old-root"))["status"] == "open"
            new_lead = await sessions.open(
                "hosta", "new-lead", provider="shell", role="lead", no_watch=True,
                parent_stream_id="hosta:new-root",
            )
            sessions.apply_live("hosta:new-lead", working=False, capture_liveness="idle",
                                capture_generation=new_lead["session_generation"])
            current[0] = 1301
            await watch.tick()
            current[0] = 2501
            await watch.tick()
            targets = await store.submit(lambda conn: [row[0] for row in conn.execute(
                "SELECT recipient_stream_id FROM v2_outbound_notices WHERE kind='tree_idle' ORDER BY created_at"
            )])
            assert targets == ["hosta:old-root", "hosta:new-root"]
        finally:
            store.stop()

    asyncio.run(run())


def test_watch_tick_requires_current_generation_capture_before_idle():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            sessions.apply_live("hosta:root", online=True, pane_status="pane_alive")
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                       no_watch=True, parent_stream_id="hosta:root")
            current = [100]
            wake = WatchWake(
                store, sessions, clock=lambda: current[0],
                root_binding=lambda: ("hosta:root", root["session_generation"]),
            )
            await wake.tick()
            current[0] = 1300
            await wake.tick()
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 0
            sessions.apply_live("hosta:lead", working=False, local_mirror=True,
                                mirror_generation=lead["session_generation"],
                                capture_liveness="idle",
                                capture_generation=lead["session_generation"])
            remote = await sessions.open("hostb", "child", provider="shell", role="worker",
                                         no_watch=True, parent_stream_id="hosta:lead")
            sessions.apply_live("hostb:child", working=False, capture_liveness="idle",
                                capture_generation="stale")
            current[0] = 1301
            await wake.tick()
            current[0] = 2501
            await wake.tick()
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 0
            sessions.apply_live("hostb:child", working=False, capture_liveness="idle",
                                capture_generation=remote["session_generation"])
            current[0] = 2502
            await wake.tick()
            current[0] = 3702
            await wake.tick()
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_local_mirror_without_capture_does_not_start_idle_clock():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            sessions.apply_live("hosta:root", online=True, pane_status="pane_alive")
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                       no_watch=True, parent_stream_id="hosta:root")
            sessions.apply_live("hosta:lead", working=False, local_mirror=True,
                                mirror_generation=lead["session_generation"])
            current = [100]
            wake = WatchWake(store, sessions, clock=lambda: current[0],
                             root_binding=lambda: ("hosta:root", root["session_generation"]))
            await wake.tick()
            current[0] = 1300
            await wake.tick()
            assert not await notices(store, "tree_idle")
            sessions.apply_live("hosta:lead", working=False, capture_liveness="idle",
                                capture_generation=lead["session_generation"])
            current[0] = 1301
            await wake.tick()
            current[0] = 2501
            await wake.tick()
            assert len(await notices(store, "tree_idle")) == 1
        finally:
            store.stop()

    async def notices(store, kind):
        return await store.submit(lambda conn: list(conn.execute(
            "SELECT notice_id FROM v2_outbound_notices WHERE kind=?", (kind,)
        )))

    asyncio.run(run())


def test_digest_sequence_survives_last_lane_closing_and_reentry(monkeypatch):
    monkeypatch.setenv("PENTACLE_LANE_DIGEST_S", "5")

    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            first = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                        no_watch=True, parent_stream_id="hosta:root")
            binding = ("hosta:root", root["session_generation"])
            observed = {"hosta:lead": {"session_generation": first["session_generation"],
                                       "working": False}}
            observed["hosta:root"] = _live_root(root)
            await store.evaluate_watch_wake(observed, now=100, root_binding=binding)
            await store.evaluate_watch_wake(observed, now=105, root_binding=binding)
            await store.mark_closed("hosta", "lead", closed_at="1970-01-01T00:01:46Z",
                                    pane_status="pane_dead")
            await store.evaluate_watch_wake({"hosta:root": _live_root(root)}, now=106,
                                            root_binding=binding)
            second = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                         no_watch=True, parent_stream_id="hosta:root")
            assert second["session_generation"] != first["session_generation"]
            observed["hosta:lead"]["session_generation"] = second["session_generation"]
            await store.evaluate_watch_wake(observed, now=107, root_binding=binding)
            await store.evaluate_watch_wake(observed, now=112, root_binding=binding)
            ids = await store.submit(lambda conn: [row[0] for row in conn.execute(
                "SELECT notice_id FROM v2_outbound_notices WHERE kind='lane_digest' ORDER BY created_at"
            )])
            assert len(ids) == 2 and ids[0] != ids[1]
        finally:
            store.stop()

    asyncio.run(run())


def test_unknown_root_pane_defers_evaluation_and_queued_delivery():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                       no_watch=True, parent_stream_id="hosta:root")
            binding = ("hosta:root", root["session_generation"])
            observed = {"hosta:lead": {"session_generation": lead["session_generation"],
                                       "working": False}}
            await store.evaluate_watch_wake(observed, now=100, root_binding=binding)
            await store.evaluate_watch_wake(observed, now=1300, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 0
            await store.update_session("hosta", "root", pane_status="pane_alive")
            observed["hosta:root"] = {"session_generation": root["session_generation"],
                                      "online": False, "pane_status": "pane_alive"}
            await store.evaluate_watch_wake(observed, now=1301, root_binding=binding)
            await store.evaluate_watch_wake(observed, now=2501, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 0
            observed["hosta:root"]["online"] = True
            await store.evaluate_watch_wake(observed, now=2502, root_binding=binding)
            await store.evaluate_watch_wake(observed, now=3702, root_binding=binding)
            notice = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()))
            sessions.apply_live("hosta:root", online=True, pane_status="pane_alive")
            await store.update_session("hosta", "root", pane_status="pane_unknown")
            decision = await WatchWake(store, sessions, root_binding=lambda: binding).delivery_guard(notice)
            assert decision is not None and decision.action == "retry"
            await store.update_session("hosta", "root", pane_status="pane_alive")
            sessions.apply_live("hosta:root", online=False, pane_status="pane_alive")
            decision = await WatchWake(store, sessions, root_binding=lambda: binding).delivery_guard(notice)
            assert decision is not None and decision.action == "retry"
        finally:
            store.stop()

    asyncio.run(run())


def test_root_offline_gap_restarts_unfired_idle_interval():
    async def run():
        store = Store()
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                       no_watch=True, parent_stream_id="hosta:root")
            binding = ("hosta:root", root["session_generation"])
            observed = {"hosta:root": _live_root(root),
                        "hosta:lead": {"session_generation": lead["session_generation"],
                                       "working": False}}
            await store.evaluate_watch_wake(observed, now=100, root_binding=binding)
            observed["hosta:root"]["online"] = False
            await store.evaluate_watch_wake(observed, now=600, root_binding=binding)
            observed["hosta:root"]["online"] = True
            await store.evaluate_watch_wake(observed, now=1300, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 0
            await store.evaluate_watch_wake(observed, now=2499, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 0
            await store.evaluate_watch_wake(observed, now=2500, root_binding=binding)
            assert await store.submit(lambda conn: conn.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'"
            ).fetchone()[0]) == 1
        finally:
            store.stop()

    asyncio.run(run())
