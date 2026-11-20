"""Exercise the report writer, accepted source history, projection and closure lookup."""
import asyncio
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

import pytest

from _shared.work_observations import read_work_candidate, timestamp
from ledger import Ledger
from server import Server
from sessions import Sessions, VerbError
from store import Store
from store_work_lanes import enrich_latest_reports_conn
from work_lane_progress import member_status_behind


SPEC = "spec_example__reports"
OTHER = "spec_example__second"
ORIGINAL = "The review is waiting."
CHANGED = "The reviewer checked the draft."


def summary(line, next_action="Review the draft."):
    return f"**Status** — {line}\n\n**Next action** — {next_action}\n"


class Fixture:
    def __init__(self, root):
        self.root = root
        self.path = str(root / "fixture.db")
        self.clock = int(time.time()) + 10
        self.folders = {}
        for spec in (SPEC, OTHER):
            folder = root / "work" / "backlog" / spec.removeprefix("spec_")
            folder.mkdir(parents=True)
            self.folders[spec] = folder
            (folder / "spec.md").write_text(f"---\nid: {spec}\ntitle: Review\n---\n## Acceptance Criteria\n- [ ] Review.\n")
            (folder / "summary.md").write_text(summary(ORIGINAL))
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.publish()
        self.open_store()

    def git(self, *args):
        stamp = datetime.fromtimestamp(self.clock,timezone.utc).isoformat()
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                       env={**os.environ,"GIT_AUTHOR_DATE":stamp,"GIT_COMMITTER_DATE":stamp})

    def publish(self, text=None):
        if text is not None:
            (self.folders[SPEC] / "summary.md").write_text(text)
        self.git("add", "work")
        self.git("commit", "-qm", "Update fixture")

    def open_store(self):
        self.store = Store(self.path)
        self.store.start()
        self.sessions = Sessions(self.store, local_host="fixture")
        self.ledger = Ledger(self.store, sessions=self.sessions)

    async def scan(self):
        candidates = {spec:[read_work_candidate(folder, "backlog", now=self.clock, quiet_s=0)]
                      for spec,folder in self.folders.items()}
        await self.store.reconcile_work_observations(
            dict(available=True, root_configured=True, candidates=candidates, errors={}, scanned_at=self.clock),
            settle_s=0, sweep=True)

    async def seat(self, name, spec=SPEC, **fields):
        with patch("time.time",return_value=self.clock):
            row = await self.sessions.open("fixture",name,provider="shell", no_watch=True, pane_status="pane_alive",
                                           spec_ids=[spec],assigned_spec_id=spec, **fields)
        return row["session_generation"]

    async def report(self, name, identity, **fields):
        row = await self.store.fetch_session("fixture", name)
        stream = f"fixture:{name}"
        msg = dict(report_id=identity, from_stream_id=stream, msg_id=0, status="done",
                   summary="Reviewed the disposable item.", findings=[], no_findings=True,
                   next_action="The parent reads the result.",
                   _auth_context=dict(token_verified=True, stream_id=stream, session_generation=row["session_generation"]))
        with patch("time.time",return_value=self.clock):
            return await self.ledger.report({**msg,**fields})

    async def behind(self):
        def read(conn):
            from store_work_index import members_conn
            members = members_conn(conn, {"members_json":json.dumps([SPEC])})
            enrich_latest_reports_conn(conn,members)
            return member_status_behind(members[0],timestamp(self.clock),True)
        return await self.store.submit(read)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT",str(tmp_path))
    f = Fixture(tmp_path)
    try:
        yield f
    finally:
        f.store.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("age,covered",[(10,True),(1800,False),(1801,False)])
async def test_exact_publication_age_and_single_use(fixture, age, covered):
    f = fixture
    await f.scan()
    await f.seat("reviewer")
    with pytest.raises(VerbError,match="unchanged"):
        await f.report("reviewer","unchanged")
    f.clock += 2
    f.publish(summary(CHANGED))
    f.clock += age
    # A later unrelated edit and first observation at report time cannot redate Status.
    f.publish(summary(CHANGED, "Read the result."))
    received = await f.report("reviewer","first")
    proof = received["status_freshness"]
    assert proof["outcome"] == "changed" and proof["covers_event"] is covered
    assert proof["published_at"] == f.clock-age
    f.clock += 1
    await f.scan()
    with pytest.raises(VerbError,match="unchanged"):
        await f.report("reviewer","second")
    f.clock += 1800
    assert await f.behind() is (not covered)
    assert (await f.report("reviewer","first"))["status_freshness"] == proof
    await f.seat("qa",role="qa")
    reply = await f.report("qa","verdict",qa_verdict="accept",target_sha="a"*40)
    assert reply["status_freshness"]["outcome"] == "review_exemption"
    f.clock += 1801
    assert await f.behind()
    f.publish(summary("The lead reflected the review."))
    await f.scan()
    assert not await f.behind()


@pytest.mark.asyncio
async def test_helpers_witness_replay_and_item_closure(fixture):
    f = fixture
    await f.scan()
    await f.seat("helper")
    with pytest.raises(VerbError,match="sole live assignee"):
        await f.report("helper","sole",completion_kind="seat_only")
    assert await f.store.get_report("sole") is None
    owner = await f.seat("owner",role="lead")
    with pytest.raises(VerbError,match="owns this item"):
        await f.report("owner","owner",completion_kind="seat_only")
    receipt = await f.report("helper","errand",completion_kind="seat_only")
    assert receipt["status_freshness"]["witness"] == {"stream_id":"fixture:owner","generation":owner}
    saved = await f.store.get_report("errand")
    assert saved["usage_snapshot"]["attribution"] == {"mode":"exclusive_primary","spec_id":SPEC}
    assert await f.store.submit(lambda c:c.execute("SELECT count(*) FROM v2_work_item_latest_report").fetchone()[0]) == 0
    lookup = await Server(store=f.store)._on_work_report_get({"report_id":"errand","_auth_context":{
        "service_authenticated":True,"service_actor":"work-report-reader"}})
    assert lookup["provenance_qualified_spec_ids"] == []
    await f.store.update_session("fixture","owner",status="closed")
    f.store.stop()
    f.open_store()
    await f.sessions.refresh()
    replay = await f.report("helper","errand",completion_kind="seat_only")
    assert replay["ledger_row_id"] == receipt["ledger_row_id"] and replay["status_freshness"] == receipt["status_freshness"]
    with pytest.raises(VerbError) as collision:
        await f.report("helper","errand")
    assert collision.value.code == "report_id_replay_conflict"
    await f.seat("wrong",OTHER)
    with pytest.raises(VerbError,match="sole live assignee"):
        await f.report("helper","departed",completion_kind="seat_only")
    await f.seat("peer")
    assert (await f.report("helper","second-errand",completion_kind="seat_only"))["status_freshness"]["witness"]["stream_id"] == "fixture:peer"
    await f.seat("other-peer",OTHER)
    await f.report("wrong","other-errand",completion_kind="seat_only")
    query = (Path(__file__).resolve().parents[1] / "tools/work_tracking_counts.sql").read_text()
    def counts(conn):
        # One delivered item with two identical cause entries still counts once.
        metadata = json.dumps({"tracking_causes":[{"condition":"behind"}]*2,"delivery_type":"initial"})
        conn.execute("""INSERT INTO v2_outbound_notices
            (notice_id,kind,dedupe_key,recipient_stream_id,tell_id,body,payload_digest,metadata,created_at,delivered_at)
            VALUES ('notice','work_item_tracking','notice','fixture:owner','notice','fixture','digest',?,'a','b')""",
                     (metadata,))
        conn.commit()
        return [tuple(r) for r in conn.execute(query)]
    rows = await f.store.submit(counts)
    assert {(r[1],r[-1]) for r in rows if r[0]=="seat_only"} == {(SPEC,2),(OTHER,1)}
    assert [r for r in rows if r[0]=="alert"] == [("alert",None,"behind",None,None,None,None,"initial",1)]
    await f.store.update_session("fixture","peer",presumed_dead_at=timestamp(f.clock))
    with pytest.raises(VerbError,match="sole live assignee"):
        await f.report("helper","dead",completion_kind="seat_only")


@pytest.mark.asyncio
async def test_unreadable_source_and_reassignment(fixture, monkeypatch):
    f = fixture
    await f.scan()
    await f.seat("worker")
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT",str(f.root/"unavailable"))
    receipt = await f.report("worker","unavailable")
    assert receipt["status_freshness"]["cause"] == "source_unavailable"
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT",str(f.root))
    await f.store.update_session("fixture","worker",assigned_spec_id=OTHER)
    with pytest.raises(VerbError,match=OTHER):
        await f.report("worker","reassigned")
    assert (await f.report("worker","unavailable"))["status_freshness"] == receipt["status_freshness"]


@pytest.mark.asyncio
async def test_witness_removed_during_report_preparation(fixture, monkeypatch):
    f = fixture
    await f.scan()
    await f.seat("helper")
    await f.seat("peer")
    started, resume = asyncio.Event(), asyncio.Event()
    put = f.store.put_report
    async def paused(*args, **kwargs):
        started.set()
        await resume.wait()
        return await put(*args, **kwargs)
    monkeypatch.setattr(f.store,"put_report",paused)
    request = asyncio.create_task(f.report("helper","racing",completion_kind="seat_only"))
    await asyncio.wait_for(started.wait(),5)
    await f.store.update_session("fixture","peer",assigned_spec_id="")
    resume.set()
    with pytest.raises(VerbError,match="sole live assignee"):
        await request
    assert await f.store.get_report("racing") is None


@pytest.mark.asyncio
async def test_unrelated_edit_reversion_and_uncertain_witness(fixture):
    f = fixture
    await f.scan()
    await f.seat("helper")
    f.clock += 2
    f.publish(summary(ORIGINAL,"Check the new note."))
    with pytest.raises(VerbError,match="unchanged"):
        await f.report("helper","next-only")
    f.clock += 2
    f.publish(summary(CHANGED))
    await f.scan()
    f.clock += 2
    f.publish(summary(ORIGINAL))
    with pytest.raises(VerbError,match="unchanged"):
        await f.report("helper","reverted")
    await f.seat("peer")
    await f.store.update_session("fixture","peer",pane_status="unknown")
    with pytest.raises(VerbError,match="unverifiable"):
        await f.report("helper","uncertain",completion_kind="seat_only")
