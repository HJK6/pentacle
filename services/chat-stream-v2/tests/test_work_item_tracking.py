"""Exercise tracking through the inventory, generation-fenced outbox and synthetic recipient."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from unittest.mock import AsyncMock

import pytest

from store import Store
from store_work_lanes import record_latest_report_conn
from test_work_lane_alert_sink import adopt, drain, lane_member, subject
from test_work_lane_episodes import seed_observations
from work_item_tracking import REPEAT_SECONDS

FIRST, SECOND, LATER = "spec_demo__span", "spec_demo__second", "spec_demo__later"
BEFORE = "2026-11-23T18:59:59Z"
EXPIRY = "2026-11-23T19:00:00Z"


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    monkeypatch.setenv("PENTACLE_VOICE_MODE", "off")
    monkeypatch.delenv("PENTACLE_ITEM_TRACKING_CLEARANCE_SHA256", raising=False)
    monkeypatch.delenv("PENTACLE_ITEM_TRACKING_RELEASE", raising=False)
    monkeypatch.setattr("store_capacity_retry.load", lambda *_: None)
    monkeypatch.setattr(Store, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(Store, "_strict_publication_state_conn", lambda *a, **k: {"enabled": False})


@asynccontextmanager
async def instance(root):
    async with subject(root) as s:
        s.store.lifecycle_front_desk = {"name": s.composite.config.name, "env_binding": s.composite._env_binding()}
        yield s


async def seed_reports(store, *specs):
    def seed(conn):
        with conn:
            for spec in specs:
                rid = spec + "-review"
                at = min(time.time(), datetime.fromisoformat(BEFORE).timestamp()) - 2400
                from work_report_freshness import apply_report_gate
                report = dict(status="done", report_id=rid, qa_verdict="accept")
                if spec != SECOND:
                    reviewer = dict(stream_id="fixture:reviewer",session_generation="review-generation",
                                    role="qa",status="open",assigned_spec_id=spec)
                    apply_report_gate(conn, report, reviewer, {"authenticated_generation":"review-generation"}, at)
                receipt = report.get("status_freshness", {})
                conn.execute("""INSERT INTO v2_reports(report_id,from_stream_id,status,qa_verdict,
                             ingested_at,created_at,status_freshness) VALUES (?,?,'done','accept',?,?,?)""",
                             (rid,"fixture:reviewer",BEFORE,at,json.dumps(receipt)))
                record_latest_report_conn(conn, dict(report_id=rid, created_at=at, status="done", qa_verdict="accept",
                    usage_snapshot={"attribution":{"mode":"exclusive_primary", "spec_id":spec}}))
    await store.submit(seed)


def observed(spec, status="in_progress"):
    return dict(lane_member("behind"), spec_id=spec, status=status)


async def notices(s):
    return await s.store.submit(lambda c: [dict(r) | {"meta":json.loads(r["metadata"])} for r in c.execute(
        "SELECT * FROM v2_outbound_notices WHERE kind='work_item_tracking' ORDER BY rowid")])


async def dispatch(s):
    return await s.queue.drain_once(kinds=["work_item_tracking"], force=True)


async def activate(s, monkeypatch, basis):
    monkeypatch.setenv("PENTACLE_ITEM_TRACKING_RELEASE", "1")
    await s.store.reconcile_work_lane_episodes(None, now_iso=BEFORE)
    assert not await notices(s)
    receipt = hashlib.sha256(b'{"status_behind":0,"unattended":0,"owed":0}').hexdigest()
    if basis == "clearance":
        monkeypatch.setenv("PENTACLE_ITEM_TRACKING_CLEARANCE_SHA256", receipt)
    await s.store.reconcile_work_lane_episodes(None, now_iso=BEFORE if basis == "clearance" else EXPIRY)
    cursor = await s.store.submit(lambda c: tuple(c.execute(
        "SELECT activated_at,basis,clearance_sha256 FROM v2_item_tracking_activation").fetchone()))
    assert cursor == ((BEFORE,basis,receipt) if basis == "clearance" else (EXPIRY,basis,None))


async def start(s):
    await adopt(s, members=[FIRST,SECOND], kind="behind")
    await seed_observations(s.store, [observed(FIRST),observed(SECOND)])
    await seed_reports(s.store,FIRST,SECOND)
    await drain(s)


@pytest.mark.asyncio
async def test_activation_and_receipts(tmp_path, monkeypatch, isolated_config):
    async with instance(tmp_path) as s:
        await start(s)
        await activate(s, monkeypatch, "expiry")
        assert REPEAT_SECONDS == 86400
        assert sum(await asyncio.gather(dispatch(s), dispatch(s))) == 2
        rows = await notices(s)
        assert {r["meta"]["spec_id"] for r in rows} == {FIRST,SECOND}
        for r in rows:
            causes = {c["condition"]:c for c in r["meta"]["tracking_causes"]}
            assert set(causes) == {"status_behind","unattended"}
            # An older or idle-reaped owner can have no attendance-gate row.
            assert (causes["unattended"]["gate"], causes["unattended"]["gate_outcome"]) == ("legacy/unknown","legacy/unknown")
            actor = causes["status_behind"]
            assert actor["gate_outcome"] == ("legacy/unknown" if r["meta"]["spec_id"] == SECOND else "review_exemption")
            assert actor["seat_role"] == ("unknown" if r["meta"]["spec_id"] == SECOND else "qa")
            assert r["delivered_at"] and r["meta"]["delivery_type"] == "initial"
            assert any(r["notice_id"] in text and r["meta"]["spec_id"] in text and "agent-orch work" in text
                       and "Awareness only" not in text for text in s.provider.pastes)
        await drain(s)
        assert await dispatch(s) == 0
        print(json.dumps({"basis":"expiry","repeat_seconds":REPEAT_SECONDS,
                          "receipts":[[r["notice_id"],r["delivered_at"]] for r in rows]}))


@pytest.mark.asyncio
async def test_restart_rebind_source_and_recurrence(tmp_path, monkeypatch, isolated_config):
    async with instance(tmp_path) as s:
        await start(s)
        await activate(s, monkeypatch, "clearance")
        initial = [r["notice_id"] for r in await notices(s)]
        # Transport outage before provider input.
        live = s.queue.comms
        s.queue.comms = None
        assert await dispatch(s) == 0
        s.queue.comms = live
        assert all(r["attempts"] == 1 and not r["delivered_at"] for r in await notices(s))

    async with instance(tmp_path) as s:
        s.store.tracking_repeat_seconds = 2
        new = await s.server.sessions.open("fixture","next-desk",provider="codex",visibility="visible")
        target, gen = "fixture:next-desk", new["session_generation"]
        def rebind(c):
            with c:
                c.execute("INSERT OR REPLACE INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) "
                          "VALUES(?,?,?,1,?)",(s.composite.config.name,target,gen,BEFORE))
        await s.store.submit(rebind)
        await s.composite.load_binding()
        async def proof(text):
            await s.store.append_session_event(target, dict(stream_id=target,host="fixture",provider="codex",
                session_name="next-desk", session_id=gen,timestamp=datetime.now(timezone.utc).isoformat(),kind="USER",text=text),
                identity="fixture-recipient:" + text, limit=500)
        s.provider.user = proof
        await asyncio.gather(drain(s),s.store.reconcile_work_lane_episodes(None))
        assert await dispatch(s) == 2
        rows = await notices(s)
        assert [r["notice_id"] for r in rows] == initial
        assert all(r["recipient_stream_id"] == target and r["meta"]["root_generation"] == gen for r in rows)
        assert len(s.provider.pastes) == 2
        await asyncio.sleep(2.1)
        await drain(s)
        assert await dispatch(s) == 2
        assert [r["meta"]["delivery_type"] for r in (await notices(s))[2:]] == ["repeat","repeat"]
        # A later item in the same goal cannot be hidden by the open incidents.
        await seed_observations(s.store,[observed(LATER,"analysis")])
        lane, = await s.store.work_lane_rows()
        await s.store.apply_work_lane_operation(stream_id="fixture:assistant",request_id="add-later",
            operation="set_members",lane_id=lane["lane_id"],expected_lane_version=lane["version"],
            payload={"members":[FIRST,SECOND,LATER]},actor_stream_id=target,actor_generation=gen,
            binding_name=s.composite.config.name,env_binding=s.composite._env_binding())
        await seed_reports(s.store,LATER)
        await drain(s)
        assert await dispatch(s) == 1
        lead = await s.store.open_session("fixture","lead",role="lead",provider="shell",
            assigned_spec_id=LATER,parent_stream_id=target,pane_status="pane_alive")
        await seed_observations(s.store,[observed(LATER)])
        await s.store.mark_reconciled_dead("fixture","lead",expected_generation=lead["session_generation"],
                                          presumed_dead_at=BEFORE,closed_at=BEFORE)
        await drain(s)
        assert await dispatch(s) == 1
        latest = (await notices(s))[-1]["meta"]
        assert latest["delivery_type"] == "changed" and latest["spec_id"] == LATER
        assert {c["condition"] for c in latest["tracking_causes"]} == {"unattended","owed","status_behind"}
        assert any(c["gate_outcome"] == "confirmed_death" and c["seat_role"] == "lead" for c in latest["tracking_causes"])
        await seed_observations(s.store,[observed(FIRST,"completed")])
        await drain(s)
        for _ in range(2):
            await asyncio.sleep(2.1)
            await drain(s)
            assert await dispatch(s) == 2
        assert sum(r["meta"]["spec_id"] == FIRST for r in await notices(s)) == 2
        await seed_observations(s.store,[observed(FIRST)])
        await drain(s)
        assert await dispatch(s) == 1
        recurrence = (await notices(s))[-1]["meta"]
        assert (recurrence["spec_id"],recurrence["occurrence"],recurrence["delivery_type"]) == (FIRST,2,"initial")
        rows = await notices(s)
        assert len(set(s.provider.pastes)) == len(rows) == len(s.provider.pastes)
        query = (Path(__file__).resolve().parents[1]/"tools/work_tracking_counts.sql").read_text()
        result = await s.store.submit(lambda c: [tuple(r) for r in c.execute(query)])
        from collections import Counter
        expected = Counter((c["condition"],c["event_kind"],c["seat_role"],c["gate"],c["gate_outcome"],r["meta"]["delivery_type"])
                           for r in rows for c in r["meta"]["tracking_causes"])
        assert {r[2:-1]:r[-1] for r in result if r[0]=="alert"} == expected
        print(json.dumps(dict(basis="clearance",repeat_seconds=s.store.tracking_repeat_seconds,route=target,
            notices=[(r["meta"]["spec_id"],r["meta"]["occurrence"],r["meta"]["delivery_type"],r["notice_id"]) for r in rows],
            grouped_counts=result)))



@pytest.mark.asyncio
async def test_source_repair_unknown_index_and_overdue(tmp_path, monkeypatch, isolated_config):
    from _shared.work_observations import read_work_candidate
    async with instance(tmp_path) as s:
        await adopt(s, members=[FIRST,SECOND],kind="behind")
        folders = {}
        for spec in (FIRST,SECOND):
            folder = tmp_path/"work/analysis"/spec.removeprefix("spec_")
            folder.mkdir(parents=True)
            (folder/"spec.md").write_text(f"---\nid: {spec}\ntitle: A fixture\n---\n## Acceptance Criteria\n- [ ] Review.\n")
            (folder/"summary.md").write_text("**Status** — The review is pending.\n\n**Next action** — Review the fixture.\n")
            folders[spec] = folder
        async def scan(available=True):
            now = time.time()
            candidates = {spec:[read_work_candidate(path,"analysis",now=now,quiet_s=0)]
                          for spec,path in folders.items()} if available else {}
            await s.store.reconcile_work_observations(dict(available=available, root_configured=True,
                candidates=candidates, errors={},scanned_at=now), settle_s=0, sweep=True)
        await scan()
        await seed_reports(s.store,FIRST,SECOND)
        await activate(s,monkeypatch,"clearance")
        pending = await notices(s)
        assert len(pending) == 2
        await scan(False)
        await drain(s)
        assert await dispatch(s) == 0
        assert [r["notice_id"] for r in pending] == [r["notice_id"] for r in await notices(s)]
        await scan()
        await drain(s)
        (folders[FIRST]/"summary.md").write_text("**Status** — The review passed with a recorded receipt.\n\n**Next action** — Ship.\n")
        await scan()
        # A source edit before the next inventory sweep still fences the paste.
        assert await dispatch(s) == 1
        await drain(s)
        rows = await notices(s)
        cleared, = [r for r in rows if r["meta"]["spec_id"] == FIRST]
        assert cleared["terminal_reason"] == "tracking_superseded" and not cleared["delivered_at"]
        survivor, = [r for r in rows if r["meta"]["spec_id"] == SECOND]
        # Simulate days of offline time at the owned instance's tick boundary.
        later = datetime.fromtimestamp(datetime.fromisoformat(survivor["delivered_at"]).timestamp()+4*86400,timezone.utc).isoformat()
        await asyncio.gather(*(s.store.reconcile_work_lane_episodes(None,now_iso=later) for _ in range(3)))
        assert len(await notices(s)) == 3 and await dispatch(s) == 1
        assert len(s.provider.pastes) == 2 and all(SECOND in x for x in s.provider.pastes)


@pytest.mark.asyncio
async def test_legacy_pagination_overlap_and_mixed_digest(tmp_path,monkeypatch,isolated_config):
    from store_routing import _insert_outbound_notice_conn
    from front_desk_digest import HELD_KIND
    async with instance(tmp_path) as s:
        fillers = [f"spec_demo__clean_{i}" for i in range(35)]
        lane = await adopt(s,members=[*fillers,FIRST,SECOND],kind="behind")
        await seed_observations(s.store,[observed(FIRST,"analysis"),observed(SECOND,"analysis")])
        await seed_reports(s.store,FIRST,SECOND)
        await s.store.reconcile_work_lane_episodes(None)
        def legacy(c):
            with c:
                c.execute("""INSERT INTO v2_assistant_composite_lanes(lane_id,stream_id,phase,created_at,updated_at,
                    work_state,members_json) VALUES ('fixture-overlap','fixture:assistant','discussion',?,?,'paused',?)""",
                    (BEFORE,BEFORE,json.dumps([SECOND])))
                ep=c.execute("SELECT episode_id FROM v2_work_lane_episodes WHERE kind='status_behind'").fetchone()[0]
                def put(nid,kind,body,**meta):
                    _insert_outbound_notice_conn(c,notice_id=nid,tell_id=nid,kind=kind,dedupe_key=nid,
                        recipient_stream_id="fixture:v2-test",body=body,metadata=meta)
                for nid,episode in (("old-behind",ep),("old-other","other:completed:1")):
                    put(nid,HELD_KIND,nid,episode_id=episode,family="work_lane.v1",error_alert_v1=True,
                        notification_id=nid,intent_kind="initial",policy_mode="digest",root_generation=s.origin["session_generation"])
                    c.execute("UPDATE v2_outbound_notices SET terminal_at=?,terminal_reason='folded_into_digest',"
                              "metadata=json_set(metadata,'$.folded_into_notice_id','mixed') WHERE notice_id=?",(BEFORE,nid))
                put("mixed","error_alert","old-behind and old-other",typed_digest=True,member_notice_ids=["old-behind","old-other"])
                put("history","error_alert","a prior receipt",family="work_lane.v1",episode_id=ep)
                c.execute("UPDATE v2_outbound_notices SET delivered_at=? WHERE notice_id='history'",(BEFORE,))
        await s.store.submit(legacy)
        await activate(s,monkeypatch,"clearance")
        rows=await notices(s)
        assert len(rows)==2 and {r["meta"]["spec_id"] for r in rows}=={FIRST,SECOND}
        def migrated(c):
            return {r[0]:tuple(r[1:]) for r in c.execute("SELECT notice_id,terminal_reason,delivered_at "
                        "FROM v2_outbound_notices WHERE notice_id IN ('old-behind','old-other','mixed','history')")}
        assert await s.store.submit(migrated)==dict(history=(None,BEFORE),mixed=("tracking_superseded",None),
            **{"old-behind":("folded_into_digest",None),"old-other":(None,None)})



@pytest.mark.asyncio
async def test_ended_assignment_with_missing_lane_and_unknown_source(tmp_path,monkeypatch,isolated_config):
    async with instance(tmp_path) as s:
        await seed_observations(s.store,[observed(FIRST)])
        owner = await s.store.open_session("fixture","owner",role="lead",provider="shell",
            assigned_spec_id=FIRST,pane_status="pane_alive")
        await s.store.mark_work_index_pending()
        await s.store.update_session("fixture","owner",role="worker",
            attendance_gate="set_role",attendance_available=False)
        monkeypatch.setenv("PENTACLE_ITEM_TRACKING_RELEASE","1")
        await s.store.reconcile_work_lane_episodes(None,now_iso=EXPIRY)
        assert await dispatch(s) == 1
        owed, = (await notices(s))[0]["meta"]["tracking_causes"]
        assert (owed["condition"],owed["seat_role"]) == ("owed","lead")
        assert "Verify the source status" in owed["action"]
        # The prior role comes from the ended assignment, even after demotion.
        assert owner["session_generation"]
        await seed_observations(s.store,[observed(FIRST)])
        await drain(s)
        assert await dispatch(s) == 1
        meta = (await notices(s))[-1]["meta"]
        assert meta["delivery_type"] == "changed" and meta["occurrence"] == 1
        unattended, = [c for c in meta["tracking_causes"] if c["condition"] == "unattended"]
        assert (unattended["seat_role"],unattended["gate_outcome"]) == ("lead","source_unverified")

@pytest.mark.asyncio
async def test_new_cause_while_original_input_awaits_proof(tmp_path, monkeypatch, isolated_config):
    async with instance(tmp_path) as s:
        await adopt(s, members=[FIRST], kind="behind")
        await seed_observations(s.store, [observed(FIRST,"analysis")])
        await seed_reports(s.store, FIRST)
        await activate(s, monkeypatch, "clearance")
        initial, = await notices(s)
        # Simulate the durable boundary immediately before a possible paste.
        # The existing outbox owns subsequent proof lookup for this tell.
        def possible_input(conn):
            with conn:
                conn.execute("INSERT INTO v2_tell_deliveries(tell_id,reply,created_at) VALUES (?,'{}',?)",
                             (initial["tell_id"], time.time()))
        await s.store.submit(possible_input)
        await seed_observations(s.store, [observed(FIRST)])
        await drain(s)
        old, changed = await notices(s)
        assert old["notice_id"] == initial["notice_id"] and not old["terminal_at"]
        assert changed["meta"]["delivery_type"] == "changed"
        assert {c["condition"] for c in changed["meta"]["tracking_causes"]} == {"status_behind","unattended"}
        assert await s.queue.deliver_now(changed["notice_id"])
        await drain(s)
        assert len(await notices(s)) == 2


async def seed_unchanged_report(store, spec):
    def seed(conn):
        with conn:
            from work_report_freshness import apply_report_gate
            rid, line, generation = spec + "-unchanged", "The lead is still working.", "lead-generation"
            at = min(time.time(), datetime.fromisoformat(BEFORE).timestamp()) - 2400
            conn.execute("INSERT INTO v2_report_status_boundaries VALUES (?,?,?,?,?,?,?)",
                         ("fixture:lead", generation, spec, at - 60, 1, line, "old-revision"))
            report = dict(status="done", report_id=rid, usage_snapshot={})
            lead = dict(stream_id="fixture:lead", session_generation=generation, role="lead",
                        status="open", assigned_spec_id=spec)
            checkpoint = dict(spec_id=spec, session_generation=generation, state="valid",
                              facts={"status_line": line}, source_revisions={"summary.md": "new-revision"})
            apply_report_gate(conn, report, lead, {"authenticated_generation": generation, "checkpoint": checkpoint}, at)
            conn.execute("""INSERT INTO v2_reports(report_id,from_stream_id,status,ingested_at,created_at,
                         status_freshness) VALUES (?,?,'done',?,?,?)""",
                         (rid, "fixture:lead", BEFORE, at, json.dumps(report["status_freshness"])))
            record_latest_report_conn(conn, dict(report_id=rid, created_at=at, status="done",
                usage_snapshot={"attribution": {"mode": "exclusive_primary", "spec_id": spec}}))
    await store.submit(seed)


@pytest.mark.asyncio
async def test_unchanged_status_report_reaches_front_desk_as_status_behind(tmp_path, monkeypatch, isolated_config):
    async with instance(tmp_path) as s:
        await adopt(s, members=[FIRST], kind="behind")
        await seed_observations(s.store, [observed(FIRST)])
        await seed_unchanged_report(s.store, FIRST)
        await drain(s)
        await activate(s, monkeypatch, "expiry")
        assert await dispatch(s) == 1
        [row] = await notices(s)
        causes = {c["condition"]: c for c in row["meta"]["tracking_causes"]}
        assert causes["status_behind"]["gate_outcome"] == "unchanged"
