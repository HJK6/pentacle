"""Owed dispositions follow accepted source and witnessed assignment episodes."""
import asyncio
import json

import pytest

from store import Store
from test_work_unattended import notices, root
from message_envelopes import match_message_envelope

SPEC = "spec_demo__owed"
HOST = "host-a"


async def observe(store, status, revision, now, *, parked=None, available=True, identity=SPEC):
    candidate = {"quality": "fresh", "path": "/fixture/work/" + status + "/demo__owed",
        "source_hash": revision, "facts": {"status": status, "title": "Owed item", "terminal": None,
            "ac_checked": 0, "ac_total": 1, "estimate": None, "parked_declaration": parked,
            "status_text": "Working", "next_action_text": "Continue"}}
    return await store.reconcile_work_observations({"available": available, "root_configured": True,
        "candidates": {identity: [candidate]} if available else {}, "errors": {},
        "scanned_at": now, "error": None if available else "root_unavailable"}, settle_s=0, sweep=True)


async def lead(store, name="lead", **fields):
    return await store.open_session(HOST, name, role="lead", assigned_spec_id=SPEC,
                                   parent_stream_id=fields.pop("parent_stream_id", None), **fields)


async def end(store, row, kind="operator_close"):
    return await store.mark_closed(HOST, row["session_name"], closed_at="2026-10-25T02:00:00Z",
        pane_status="pane_dead", expected_generation=row["session_generation"], close_kind=kind)


@pytest.mark.parametrize("status", ["completed", "deprecated", "backlog"])
@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("arrival", ["before_assignment", "during_assignment", "after_close"])
def test_fresh_settled_source_prevents_or_clears_debt(status, known, arrival):
    from work_owed import owes_on_close_conn
    async def run():
        store = Store(); store.start()
        try:
            if known:
                await observe(store, "blocked", "baseline", 90)
            if arrival == "before_assignment":
                await observe(store, status, "settled", 100)
            owner = await lead(store)
            if arrival == "during_assignment":
                await observe(store, status, "settled", 101)
            predicted = await store.submit(lambda c: owes_on_close_conn(
                c, f"{HOST}:lead", owner["session_generation"]))
            assert predicted is (arrival == "after_close")
            await end(store, owner)
            assert (await store.work_owed())["total"] == int(arrival == "after_close")
            await observe(store, status, "settled", 102)
            assert (await store.work_owed())["total"] == 0
            await observe(store, status, "settled", 103)
            assert (await store.work_owed())["quality"] == "fresh"
            # A genuine reopening is a new episode and may owe again.
            await observe(store, "in_progress", "reopened", 104)
            reopened = await lead(store)
            await end(store, reopened)
            assert (await store.work_owed())["total"] == 1
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["completed", "deprecated", "backlog"])
@pytest.mark.parametrize("known", [False, True])
def test_old_registry_repairs_on_unchanged_healthy_scan_after_restart(tmp_path, status, known):
    from work_owed import STATE_KEY
    path = str(tmp_path / "legacy.db")
    async def seed():
        store = Store(path); store.start()
        try:
            await observe(store, status, "unchanged-source", 100)
            await lead(store)
            def legacy(conn):
                with conn:
                    state = json.loads(conn.execute("SELECT state_json FROM v2_work_index_state").fetchone()[0])
                    registry = state[STATE_KEY]
                    key, episode = next(iter(registry["episodes"].items()))
                    episode.update(baseline_known=known, disposition=None, closed_at="2026-10-25T02:00:00Z")
                    registry["owed"][key] = registry["episodes"].pop(key)
                    conn.execute("UPDATE sessions SET status='closed' WHERE session_name='lead'")
                    conn.execute("UPDATE v2_work_index_state SET state_json=?", (json.dumps(state),))
            await store.submit(legacy)
            assert (await store.work_owed())["total"] == 1  # read queries cannot repair storage
        finally:
            store.stop()
    async def repair():
        store = Store(path); store.start()
        try:
            await observe(store, status, "unchanged-source", 101)
            assert (await store.work_owed())["total"] == 0
            persisted = await store.submit(lambda c: json.loads(c.execute(
                "SELECT state_json FROM v2_work_index_state").fetchone()[0]))
            assert persisted[STATE_KEY]["owed"] == {}
            await observe(store, status, "unchanged-source", 102)
            assert (await store.work_owed())["total"] == 0
        finally:
            store.stop()
    asyncio.run(seed()); asyncio.run(repair())


@pytest.mark.parametrize("status", ["completed", "deprecated", "backlog"])
@pytest.mark.parametrize("quality", ["stale", "missing", "ambiguous", "unavailable", "no_revision"])
def test_cached_settled_status_cannot_clear_unusable_source(status, quality):
    async def run():
        store = Store(); store.start()
        try:
            await lead(store)  # unknown baseline must remain degraded until real recovery
            def uncertain(conn):
                from _shared.work_observations import advance_observation
                record, _ = advance_observation(SPEC, None, {"quality": "fresh", "path": "/fixture/item",
                    "source_hash": "cached", "facts": {"status": status}}, now=100, settle_s=0, sweep=True)
                if quality != "no_revision":
                    record["member"]["observation"]["quality"] = quality
                else:
                    record["source_hash"] = None
                with conn:
                    conn.execute("INSERT INTO v2_work_item_observations VALUES (?,?)", (SPEC, json.dumps(record)))
                    state = json.loads(conn.execute("SELECT state_json FROM v2_work_index_state").fetchone()[0])
                    state["available"] = quality != "unavailable"
                    conn.execute("UPDATE v2_work_index_state SET state_json=?", (json.dumps(state),))
            await store.submit(uncertain)
            await end(store, await store.fetch_session(HOST, "lead"))
            owed = await store.work_owed()
            assert owed["total"] == owed["unknown_count"] == 1
            await observe(store, status, "recovered", 110)
            assert (await store.work_owed())["total"] == 0
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("baseline,park,changes,owed", [
    ("backlog", None, [("in_progress", None)], 1),
    ("in_progress", None, [("blocked", None), ("in_progress", None)], 1),
    ("backlog", None, [("in_progress", None), ("backlog", None)], 0),
    ("blocked", "2026-10-25 authority — old reason", [("blocked", "2026-10-25 authority — old reason")], 1),
    ("blocked", "2026-10-25 authority — old reason", [("blocked", "2026-10-25 authority — new reason")], 0),
    ("analysis", None, [("backlog", None)], 0),
    ("blocked", None, [("blocked", None)], 1),
])
def test_accepted_revision_counterexamples(baseline, park, changes, owed):
    async def run():
        store = Store(); store.start()
        try:
            await observe(store, baseline, "baseline", 100, parked=park)
            owner = await lead(store)
            for i, (status, declaration) in enumerate(changes, 1):
                await observe(store, status, f"accepted-{i}", 100 + i, parked=declaration)
            await end(store, owner)
            result = await store.work_owed()
            assert result["total"] == owed
            if owed:
                assert result["items"][0]["session_generation"] == owner["session_generation"]
            # Unchanged scans and delivery/report metadata cannot invent evidence.
            await observe(store, changes[-1][0], f"accepted-{len(changes)}", 200, parked=changes[-1][1])
            assert (await store.work_owed())["total"] == owed
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["backlog", "blocked", "analysis", "ready_for_dev", "needs_qa"])
def test_untouched_work_has_no_assignment_debt(status):
    async def run():
        store = Store(); store.start()
        try:
            await observe(store, status, "untouched", 100)
            assert (await store.work_owed())["total"] == 0
            owner = await lead(store)
            await end(store, owner, "session_close")
            assert (await store.work_owed())["total"] == int(status != "backlog")
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["blocked", "analysis", "ready_for_dev", "needs_qa"])
def test_other_dispositions_still_require_witnessed_status_transition(status):
    async def run():
        store = Store(); store.start()
        try:
            await observe(store, "in_progress", "baseline", 100)
            owner = await lead(store)
            await observe(store, status, "accepted-transition", 101)
            await end(store, owner)
            assert (await store.work_owed())["total"] == 0
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["idle_reap", "operator_close", "operator_offline_close", "manager_close", "spawn_rollback", "handed_off"])
def test_every_involuntary_close_keeps_debt(kind):
    async def run():
        store = Store(); store.start()
        try:
            await observe(store, "in_progress", "start", 100)
            owner = await lead(store)
            await end(store, owner, kind)
            assert (await store.work_owed())["total"] == 1
            await observe(store, "backlog", "accepted-disposition", 110)
            assert (await store.work_owed())["total"] == 0
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("change", [{"assigned_spec_id": ""}, {"role": "qa"}, {"presumed_dead_at": "2026-10-25T02:00:00Z"}])
def test_assignment_loss_creates_debt_and_only_real_lead_clears(change):
    async def run():
        store = Store(); store.start()
        try:
            await observe(store, "in_progress", "start", 100)
            await lead(store)
            await store.update_session(HOST, "lead", **change)
            assert (await store.work_owed())["total"] == 1
            await store.open_session(HOST, "qa", role="qa", assigned_spec_id=SPEC)
            assert (await store.work_owed())["total"] == 1
            await lead(store, "successor")
            assert (await store.work_owed())["total"] == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_unknown_baseline_first_discovery_stale_and_restart_retain_debt(tmp_path):
    path = str(tmp_path / "sessions.db")
    async def first():
        store = Store(path); store.start()
        try:
            owner = await lead(store)
            await observe(store, "blocked", "first-discovery", 100)
            await end(store, owner)
            result = await store.work_owed()
            assert result["total"] == result["unknown_count"] == 1
            assert result["quality"] == "degraded"
            await observe(store, "blocked", "first-discovery", 110, available=False)
            assert (await store.work_owed())["unknown_count"] == 1
        finally:
            store.stop()
    async def second():
        store = Store(path); store.start()
        try:
            assert (await store.work_owed())["total"] == 1
            await observe(store, "blocked", "first-discovery", 120)
            assert (await store.work_owed())["unknown_count"] == 1
            await observe(store, "analysis", "accepted-post-start-transition", 130)
            assert (await store.work_owed())["total"] == 0
        finally:
            store.stop()
    asyncio.run(first()); asyncio.run(second())


def test_same_name_old_generation_cannot_end_reopened_assignment():
    async def run():
        store = Store(); store.start()
        try:
            await observe(store, "in_progress", "start", 100)
            old = await lead(store)
            await end(store, old)
            assert (await store.work_owed())["total"] == 1
            new = await lead(store)
            assert new["session_generation"] != old["session_generation"]
            assert (await store.work_owed())["total"] == 0
            assert await end(store, old) is None
            assert (await store.fetch_session(HOST, "lead"))["status"] == "open"
            await end(store, new)
            items = (await store.work_owed())["items"]
            assert len(items) == 1 and items[0]["session_generation"] == new["session_generation"]
        finally:
            store.stop()
    asyncio.run(run())


def test_owed_digest_recurrence_inflight_clear_disabled_and_root_generation(monkeypatch):
    monkeypatch.setenv("PENTACLE_LANE_DIGEST_S", "5")
    async def run():
        store = Store(); store.start()
        try:
            _, binding, presence = await root(store)
            await observe(store, "in_progress", "start", 100)
            owner = await lead(store, parent_stream_id=binding[0])
            await end(store, owner)
            await store.evaluate_watch_wake(presence, now=100, root_binding=binding)
            await store.evaluate_watch_wake(presence, now=105, root_binding=binding)
            first = (await notices(store))[0]
            assert match_message_envelope(first["body"])["owed_dispositions"]["total"] == 1
            await store.evaluate_watch_wake(presence, now=110, root_binding=binding)
            assert len(await notices(store)) == 1
            await store.submit(lambda c: c.execute("UPDATE v2_outbound_notices SET delivered_at='delivered' WHERE notice_id=?", (first["notice_id"],)))
            await store.evaluate_watch_wake(presence, now=115, root_binding=binding)
            assert len(await notices(store)) == 2
            await observe(store, "backlog", "accepted-move", 120)
            await store.evaluate_watch_wake(presence, now=120, root_binding=binding)
            rows = await notices(store)
            assert match_message_envelope(rows[-1]["body"])["owed_dispositions"]["total"] == 0
            await store.evaluate_watch_wake(presence, now=125, root_binding=binding)
            assert len(await notices(store)) == len(rows)
            monkeypatch.setenv("PENTACLE_LANE_DIGEST_S", "0")
            assert (await store.work_owed())["digest_delivery"] == "disabled"
        finally:
            store.stop()
    asyncio.run(run())


def test_digest_uses_post_reconcile_debt_and_relevance_projection(monkeypatch):
    from test_work_unattended import missing_record
    monkeypatch.setenv("PENTACLE_LANE_DIGEST_S", "5")
    async def run():
        store = Store(); store.start()
        try:
            _, binding, presence = await root(store)
            await observe(store, "in_progress", "active", 100)
            await end(store, await lead(store))
            for i, former in enumerate([None, None, "analysis"]):
                await missing_record(store, f"spec_demo__orphan_{i}", former)
            await store.evaluate_watch_wake(presence, now=100, root_binding=binding)
            await store.evaluate_watch_wake(presence, now=105, root_binding=binding)
            first = match_message_envelope((await notices(store))[0]["body"])
            assert first["owed_dispositions"] == await store.work_owed(root_binding=binding)
            work = await store.work_unattended(now=105)
            assert first["unattended_work"] == {k: v for k, v in work.items() if k != "omitted"}
            assert work["count"] == 1 and work["omitted_count"] == 0
            await observe(store, "backlog", "settled", 106)
            await store.evaluate_watch_wake(presence, now=110, root_binding=binding)
            rows = await notices(store)
            assert len(rows) == 2
            cleared = match_message_envelope(rows[-1]["body"])
            assert cleared["owed_dispositions"]["total"] == 0
            assert cleared["unattended_work"]["count"] == cleared["unattended_work"]["omitted_count"] == 0
            assert cleared["unattended_work"]["quality"] == "fresh"
            await store.evaluate_watch_wake(presence, now=115, root_binding=binding)
            assert len(await notices(store)) == 2
        finally:
            store.stop()
    asyncio.run(run())


def test_unknown_debt_survives_fd_generation_change_and_retires_old_digest(monkeypatch):
    monkeypatch.setenv("PENTACLE_LANE_DIGEST_S", "5")
    async def run():
        store = Store(); store.start()
        try:
            _, binding, presence = await root(store)
            owner = await lead(store)  # no usable work observation at assignment
            await end(store, owner)
            await store.evaluate_watch_wake(presence, now=100, root_binding=binding)
            await store.evaluate_watch_wake(presence, now=105, root_binding=binding)
            first = (await notices(store))[0]
            section = match_message_envelope(first["body"])["owed_dispositions"]
            assert section["total"] == section["unknown_count"] == 1 and section["quality"] == "degraded"
            await store.mark_closed(HOST, "root", closed_at="2026-10-25T02:00:00Z", pane_status="pane_dead",
                                    expected_generation=binding[1], close_kind="operator_close")
            replacement = await store.open_session(HOST, "root", pane_status="pane_alive")
            new_binding = (binding[0], replacement["session_generation"])
            assert new_binding[1] != binding[1]
            presence[binding[0]]["session_generation"] = new_binding[1]
            await store.evaluate_watch_wake(presence, now=110, root_binding=new_binding)
            await store.evaluate_watch_wake(presence, now=115, root_binding=new_binding)
            rows = await notices(store)
            assert len(rows) == 2 and rows[0]["terminal_reason"] == "root_generation_replaced"
            import json
            assert json.loads(rows[1]["metadata"])["root_generation"] == new_binding[1]
            assert match_message_envelope(rows[1]["body"])["owed_dispositions"]["unknown_count"] == 1
            await store.evaluate_watch_wake(presence, now=120, root_binding=new_binding)
            assert len(await notices(store)) == 2  # one current-generation in-flight notice
            owed = await store.work_owed(root_binding=new_binding)
            assert owed["total"] == 1 and owed["items"][0]["parent_generation"] == new_binding[1]
        finally:
            store.stop()
    asyncio.run(run())


def test_debt_parent_is_the_parent_at_episode_end():
    async def run():
        store = Store(); store.start()
        try:
            await observe(store, "in_progress", "start", 100)
            await store.open_session(HOST, "first-parent")
            await store.open_session(HOST, "next-parent")
            owner = await lead(store, parent_stream_id=f"{HOST}:first-parent")
            await store.update_session(HOST, "lead", parent_stream_id=f"{HOST}:next-parent")
            await end(store, owner)
            assert (await store.work_owed())["items"][0]["parent_stream_id"] == f"{HOST}:next-parent"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("status,tracking,parked", [
    ("blocked", "- parked: 2026-10-25 authority — Wait   for input  ", "2026-10-25 authority — Wait for input"),
    ("blocked", "- parked: 2026-10-25 old — old\n- parked: 2026-02-30 new — invalid", None),
    ("blocked", "```\n- parked: 2026-10-25 authority — example\n```", None),
    ("blocked", "> - parked: 2026-10-25 authority — quoted", None),
    ("in_progress", "- parked: 2026-10-25 authority — previous pause", None),
])
def test_reader_uses_valid_park_and_exact_two_document_revisions(tmp_path, status, tracking, parked):
    import hashlib
    import time
    from _shared.work_observations import read_work_candidate
    folder = tmp_path / "demo__owed"; folder.mkdir()
    spec = (f"---\nid: {SPEC}\ntitle: Owed\n---\n## Tracking\n{tracking}\n## Other\n"
            "- parked: 2026-10-25 outside — ignored\n").encode()
    summary = "**Status** — Waiting.\r\n".encode()
    (folder / "spec.md").write_bytes(spec); (folder / "summary.md").write_bytes(summary)
    candidate = read_work_candidate(folder, status, now=time.time()+1, quiet_s=0)
    assert candidate["facts"].get("parked_declaration") == parked
    assert candidate["document_revisions"] == {"spec.md": hashlib.sha256(spec).hexdigest(),
                                                "summary.md": hashlib.sha256(summary).hexdigest()}


def test_done_report_predicts_owed_close_and_never_clears_its_debt():
    from ledger import Ledger
    from test_report_disposition_context import setup, payload, report_notices
    async def run():
        store = Store(); store.start()
        try:
            sessions = await setup(store)
            await store.update_session(HOST, "reporter", role="lead", assigned_spec_id="spec_demo__item_00")
            snapshot = await Ledger(store, sessions=sessions)._report_routing_snapshot(f"{HOST}:reporter")
            saved = await store.put_report(payload("owed-report"), routing_snapshot=snapshot)
            notice = match_message_envelope((await report_notices(store))[0]["body"])
            assert notice["owes_disposition_on_close"] is True
            reporter = await store.fetch_session(HOST, "reporter")
            await end(store, reporter)
            assert (await store.work_owed())["total"] == 1
            assert (await store.get_report(saved["report_id"]))["status"] == "done"
        finally:
            store.stop()
    asyncio.run(run())


def test_readonly_work_owed_rpc_and_real_cli_transport(tmp_path):
    import hashlib
    import json
    import os
    from pathlib import Path
    import sys
    from server import Server
    from sessions import Sessions
    from store import STREAM_TOKEN_HASH_VERSION
    async def run():
        store = Store(); store.start()
        sessions = Sessions(store, local_host=HOST)
        server = Server(store=store, sessions=sessions, local_host=HOST, port=0)
        try:
            await observe(store, "in_progress", "start", 100)
            owner = await lead(store)
            await end(store, owner)
            await store.open_session(HOST, "reader", role="worker")
            await store.grant_stream_token(HOST, "reader", hashlib.sha256(b"owed-read-token").hexdigest(), STREAM_TOKEN_HASH_VERSION)
            await sessions.refresh()
            port = await server.bind()
            env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8",
                "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "agent-orch"),
                "AGENT_ORCH_WS_URL": f"ws://127.0.0.1:{port}", "AGENT_ORCH_HOST_ID": HOST,
                "AGENT_ORCH_STREAM_ID": f"{HOST}:reader", "AGENT_ORCH_STREAM_TOKEN": "owed-read-token",
                "AGENT_ORCH_TOKEN": "", "AGENT_ORCH_RUNTIME_DIR": str(tmp_path / "cli")}
            async def cli():
                proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_orch.cli", "work", "owed", "--json",
                    env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    out, err = await asyncio.wait_for(proc.communicate(), 10)
                finally:
                    if proc.returncode is None:
                        proc.kill(); await proc.wait()
                assert proc.returncode == 0, err.decode()
                return json.loads(out)
            response = await cli()
            assert response["type"] == "work.owed.ok"
            owed = response["owed_dispositions"]
            assert owed["total"] == 1 and owed["counts_by_repo"] == {"demo": 1}
            assert owed["items"][0]["session_generation"] == owner["session_generation"]
            await observe(store, "completed", "settled", 101)
            assert (await cli())["owed_dispositions"] == await store.work_owed()
            assert (await store.work_owed())["total"] == 0
        finally:
            await server.close()
            store.stop()
    asyncio.run(run())
