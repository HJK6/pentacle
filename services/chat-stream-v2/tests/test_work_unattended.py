"""Whole-board attendance uses cached work and the canonical current assignment."""
import asyncio
import json

import pytest

from message_envelopes import match_message_envelope
from sessions import Sessions
from store import Store
from _shared.specs_service import SpecsSubsystem


async def cached(store, *, count=1, status="in_progress", now=100):
    candidates = {}
    for i in range(count):
        sid = f"spec_demo__item_{i:02}"
        candidates[sid] = [{"quality": "fresh", "path": f"/fixture/demo__item_{i:02}", "source_hash": str(i),
            "facts": {"title": f"Item {i}", "status": status, "terminal": None,
                      "ac_checked": 0, "ac_total": 1, "estimate": None,
                      "status_text": "Working", "next_action_text": "Continue"}}]
    await store.reconcile_work_observations({"available": True, "root_configured": True,
        "candidates": candidates, "errors": {}, "scanned_at": now}, settle_s=0, sweep=True)


async def root(store):
    sessions = Sessions(store, local_host="host-a")
    row = await sessions.open("host-a", "root", provider="shell", no_watch=True)
    await store.update_session("host-a", "root", pane_status="pane_alive")
    return sessions, ("host-a:root", row["session_generation"]), {"host-a:root": {
        "session_generation": row["session_generation"], "online": True, "pane_status": "pane_alive"}}


async def notices(store):
    return await store.submit(lambda conn: [dict(r) for r in conn.execute(
        "SELECT * FROM v2_outbound_notices WHERE kind='lane_digest' ORDER BY created_at")])


async def missing_record(store, identity, former=None, *, quality="missing", settled=True):
    """Replay producer observations; keep the diagnostic row in the isolated Store."""
    from _shared.work_observations import advance_observation
    previous = None
    if former:
        previous, _ = advance_observation(identity, None, {"quality": "fresh", "path": "/fixture/item",
            "source_hash": "former", "facts": {"status": former}}, now=100, settle_s=10, sweep=True)
    record, _ = advance_observation(identity, previous, {"quality": quality},
                                    now=101, settle_s=10, sweep=True)
    if settled:
        record, _ = advance_observation(identity, record, {"quality": quality},
                                        now=112, settle_s=10, sweep=True)
    def save(conn):
        with conn:
            conn.execute("INSERT OR REPLACE INTO v2_work_item_observations VALUES (?,?)",
                         (identity, json.dumps(record)))
    await store.submit(save)
    return record


def test_irrelevant_missing_shapes_do_not_degrade_but_rows_remain():
    async def run():
        store = Store(); store.start()
        try:
            await cached(store)  # genuine ownerless active positive control
            for i, former in enumerate([None, None, "analysis"]):
                await missing_record(store, f"spec_demo__orphan_{i}", former)
            before = await store.submit(lambda c: list(c.execute(
                "SELECT spec_id,record_json FROM v2_work_item_observations ORDER BY spec_id")))
            projection = await store.work_unattended()
            assert projection["count"] == 1 and projection["omitted_count"] == 0
            assert projection["quality"] == "fresh"
            after = await store.submit(lambda c: list(c.execute(
                "SELECT spec_id,record_json FROM v2_work_item_observations ORDER BY spec_id")))
            assert [tuple(r) for r in before] == [tuple(r) for r in after]
            await missing_record(store, "spec_demo__unknown", quality="ambiguous")
            partial = await store.work_unattended()
            assert partial["quality"] == "partial" and partial["omitted_count"] == 1
            assert partial["omitted"][0]["spec_id"] == "spec_demo__unknown"
            await store.mark_work_index_pending()
            unavailable = await store.work_unattended()
            assert unavailable["quality"] == "unavailable" and unavailable["omitted_count"] == 5
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("former,quality,settled,omitted", [
    (None, "missing", False, False),  # never-good first sweep: no extra grace
    ("analysis", "missing", False, True),  # formerly-good producer grace
    ("in_progress", "missing", True, True),
    ("analysis", "missing", True, False),
    ("completed", "missing", True, False),
    ("backlog", "missing", True, False),
    (None, "ambiguous", True, True),
    ("analysis", "stale", True, True),
])
def test_missing_relevance_preserves_quality_and_active_history(former, quality, settled, omitted):
    async def run():
        store = Store(); store.start()
        try:
            await cached(store)
            await missing_record(store, "spec_demo__missing", former, quality=quality, settled=settled)
            work = await store.work_unattended()
            assert work["omitted_count"] == int(omitted)
            assert work["quality"] == ("partial" if omitted else "fresh")
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("reference", ["owner", "lane", "episode", "owed"])
@pytest.mark.parametrize("settled", [False, True])
def test_current_reference_restores_missing_omission_without_recreating_record(reference, settled):
    from test_work_lanes import run
    from work_owed import STATE_KEY
    identity = "spec_demo__missing"
    async def body(env):
        store = env.store
        await cached(store)
        record = await missing_record(store, identity, settled=settled)
        assert (await store.work_unattended())["omitted_count"] == 0
        if reference == "owner":
            await store.open_session("host-a", "explicit", role="nexus", assigned_spec_id="",
                spec_ids=[identity], spec_binding_provenance=[{"spec_id": identity,
                    "provenance": "operator_v2", "granting_principal": "operator",
                    "granted_at": "2026-10-26T00:00:00Z"}])
        elif reference == "lane":
            lane_id = (await env.adopt(state="paused", lead=False, owner="operator", members=[identity]))["lane"]["lane_id"]
        else:
            def add(conn):
                with conn:
                    state = json.loads(conn.execute("SELECT state_json FROM v2_work_index_state").fetchone()[0])
                    registry = state.setdefault(STATE_KEY, {"episodes": {}, "owed": {}})
                    registry["episodes" if reference == "episode" else "owed"]["legacy"] = {"spec_id": identity}
                    conn.execute("UPDATE v2_work_index_state SET state_json=?", (json.dumps(state),))
            await store.submit(add)
        assert (await store.work_unattended())["omitted_count"] == 1
        # Remove the reference without scanning or changing the observation.
        def remove(conn):
            with conn:
                if reference == "owner":
                    conn.execute("UPDATE sessions SET status='closed' WHERE session_name='explicit'")
                elif reference == "lane":
                    conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='done' WHERE lane_id=?", (lane_id,))
                else:
                    state = json.loads(conn.execute("SELECT state_json FROM v2_work_index_state").fetchone()[0])
                    state[STATE_KEY]["episodes" if reference == "episode" else "owed"].clear()
                    conn.execute("UPDATE v2_work_index_state SET state_json=?", (json.dumps(state),))
        await store.submit(remove)
        assert (await store.work_unattended())["omitted_count"] == 0
        saved = await store.submit(lambda c: json.loads(c.execute(
            "SELECT record_json FROM v2_work_item_observations WHERE spec_id=?", (identity,)).fetchone()[0]))
        assert saved == record
    run(body)


@pytest.mark.parametrize("settled_status", ["completed", "deprecated", "backlog"])
def test_real_source_move_and_last_owner_loss(tmp_path, settled_status):
    import time
    root_path = tmp_path / "memory"
    folder = root_path / "work/in_progress/demo__journey"
    folder.mkdir(parents=True)
    identity = "spec_demo__journey"
    (folder / "spec.md").write_text(f"---\nid: {identity}\ntitle: Journey\n---\n")
    (folder / "summary.md").write_text("Working.\n")
    source = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda _: None,
                            memory_root=str(root_path))
    async def run():
        store = Store(); store.start()
        try:
            now = time.time() + 1
            await store.reconcile_work_observations(source.scan_work_observations(now=now), settle_s=0, sweep=True)
            for name in ("first", "second"):
                await store.open_session("host-a", name, role="nexus", assigned_spec_id=identity)
            assert (await store.work_unattended())["count"] == 0
            await store.update_session("host-a", "first", status="closed")
            assert (await store.work_unattended())["count"] == 0
            await store.update_session("host-a", "second", status="closed")
            assert (await store.work_unattended())["count"] == 1
            target = root_path / "work" / settled_status / folder.name
            target.parent.mkdir()
            folder.rename(target)
            await store.reconcile_work_observations(source.scan_work_observations(now=now + 1), settle_s=0, sweep=True)
            assert (await store.work_unattended())["count"] == 0
            assert (await store.work_owed())["total"] == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_zero_lane_real_cached_ingest_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_LANE_DIGEST_S", "5")
    folder = tmp_path / "memory/work/in_progress/demo__recent"
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text("---\nid: spec_demo__recent\ntitle: Recent work\n---\n## Acceptance Criteria\n- [ ] Deliver.\n")
    (folder / "summary.md").write_text("**Status** — Open.\n\n**Next action** — Deliver.\n")
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path / "memory"))
    async def run():
        store = Store(str(tmp_path / "fixture.db")); store.start()
        try:
            _, binding, obs = await root(store)
            scan = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda _: None,
                                  debounce_s=0).scan_work_observations()
            assert scan["available"] and "spec_demo__recent" in scan["candidates"]
            await store.reconcile_work_observations(scan, settle_s=0, sweep=True)
            assert (await store.work_index_status())["available"]  # positive control
            await store.evaluate_watch_wake(obs, now=100, root_binding=binding)
            await store.evaluate_watch_wake(obs, now=105, root_binding=binding)
            rows = await notices(store)
            assert len(rows) == 1, "zero-lane root must receive the whole-board unattended section"
            unattended = match_message_envelope(rows[0]["body"])["unattended_work"]
            assert [r["spec_id"] for r in unattended["items"]] == ["spec_demo__recent"]
        finally:
            store.stop()
    asyncio.run(run())


def test_read_only_rpc_whole_board_without_lanes(tmp_path):
    import hashlib
    import os
    import sys
    from pathlib import Path
    from server import Server
    from store import STREAM_TOKEN_HASH_VERSION
    from test_work_lanes import FD, run
    from work_lanes_projection import WorkLanesInventory

    async def body(env):
        await env.store.grant_stream_token("fixture-root", "visible", hashlib.sha256(b"fixture-token").hexdigest(),
                                           STREAM_TOKEN_HASH_VERSION)
        await cached(env.store, count=10)
        await missing_record(env.store, "spec_demo__irrelevant", "analysis")
        sessions = Sessions(env.store, tmux=None, local_host="fixture-root")
        await sessions.refresh()
        server = Server(port=0, store=env.store, sessions=sessions, local_host="fixture-root")
        server.work_lanes = WorkLanesInventory(env.store, sessions, server.broadcast)
        child_env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "agent-orch"),
            "AGENT_ORCH_HOST_ID": "fixture-root", "AGENT_ORCH_STREAM_ID": FD,
            "AGENT_ORCH_STREAM_TOKEN": "fixture-token", "AGENT_ORCH_TOKEN": "",
            "AGENT_ORCH_RUNTIME_DIR": str(tmp_path / "cli")}
        async def rpc(verb):
            program = ('import asyncio,json; from agent_orch.config import load_config; '
                'from agent_orch.wsclient import _one_shot_rpc; '
                f'print(json.dumps(asyncio.run(_one_shot_rpc(load_config(),{{"type":{verb!r},"request_id":"fixture-rpc"}},'
                f'prefix={verb!r},timeout=5,from_stream_id={FD!r}))))')
            proc = await asyncio.create_subprocess_exec(sys.executable, "-c", program, env=child_env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), 10)
            finally:
                if proc.returncode is None:
                    proc.kill(); await proc.wait()
            assert proc.returncode == 0, err.decode()
            return json.loads(out)
        try:
            port = await server.bind()
            child_env["AGENT_ORCH_WS_URL"] = f"ws://127.0.0.1:{port}"
            assert (await rpc("work_lanes.list"))["type"] == "work_lanes.list.ok"
            response = await rpc("work.unattended")
            assert response["type"] == "work.unattended.ok", response
            assert len(response["unattended_work"]["items"]) == 10
            assert response["unattended_work"]["quality"] == "fresh"
            assert response["unattended_work"]["omitted_count"] == 0
            proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_orch.cli", "work", "unattended", "--json",
                env=child_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), 10)
            finally:
                if proc.returncode is None:
                    proc.kill(); await proc.wait()
            assert proc.returncode == 0, err.decode()
            from work_unattended import unattended_signature
            assert unattended_signature(json.loads(out)["unattended_work"]) == unattended_signature(response["unattended_work"])
        finally:
            await server.close()
    run(body, str(tmp_path / "rpc.db"))


@pytest.mark.parametrize("fields,assigned,attended", [
    ({"role": "lead", "spec_ids": ["demo__item_00"]}, "spec_demo__item_00", True),
    ({"role": "nexus", "spec_ids": ["demo__item_00"]}, "spec_demo__item_00", True),
    ({"role": "lead", "spec_ids": ["demo__item_00"]}, "", False),
    ({"role": "lead", "spec_ids": ["demo__item_00"]}, None, True),
    ({"role": "lead", "spec_ids": ["demo__item_00", "demo__other"]}, None, False),
    ({"role": "lead", "spec_ids": ["demo__item_00", "demo__other"], "title": "Front desk advisor"}, "", False),
    ({"role": "lead", "spec_ids": ["demo__other"], "title": "Front desk"}, "spec_demo__item_00", True),
    ({"role": "lead", "spec_ids": ["demo__item_00"]}, "spec_demo__other", False),
    ({"role": "qa", "spec_ids": ["demo__item_00"]}, "spec_demo__item_00", False),
    ({"role": "planner", "spec_ids": ["demo__item_00"]}, "spec_demo__item_00", False),
    ({"role": "worker", "spec_ids": ["demo__item_00"]}, "spec_demo__item_00", False),
    ({"role": None, "spec_ids": ["demo__item_00"]}, "spec_demo__item_00", False),
    ({"role": "lead", "status": "closed", "spec_ids": ["demo__item_00"]}, "spec_demo__item_00", False),
    ({"role": "lead", "presumed_dead_at": "2026-10-22T20:00:00Z", "spec_ids": ["demo__item_00"]}, "spec_demo__item_00", False),
    ({"role": "lead", "visibility": "hidden", "handoff_from_stream_id": "host-a:old", "spec_ids": ["demo__item_00"]}, "spec_demo__item_00", True),
    ({"role": "lead", "spec_id": "demo__item_00"}, None, True),
])
def test_assignment_matrix(fields, assigned, attended):
    async def run():
        store = Store(); store.start()
        try:
            await cached(store)
            owner_fields = dict(fields)
            status = owner_fields.pop("status", "open")
            await store.open_session("host-a", "owner", **owner_fields)
            # Exercise persisted NULL versus explicit empty independently of
            # current creation defaults, including normalized legacy tags.
            await store.update_session("host-a", "owner", assigned_spec_id=assigned, status=status)
            work = await store.work_unattended(now=105)
            assert work["count"] == (0 if attended else 1)
            assert work["quality"] == "fresh"
        finally:
            store.stop()
    asyncio.run(run())


def test_whole_population_hash_quality_order_recovery_and_clear(monkeypatch):
    from work_unattended import unattended_signature
    monkeypatch.setenv("PENTACLE_LANE_DIGEST_S", "5")
    async def run():
        store = Store(); store.start()
        try:
            _, binding, obs = await root(store)
            await cached(store, count=10)
            def alter(conn, identity, **fields):
                raw = json.loads(conn.execute("SELECT record_json FROM v2_work_item_observations WHERE spec_id=?", (identity,)).fetchone()[0])
                raw["member"].update(fields)
                conn.execute("UPDATE v2_work_item_observations SET record_json=? WHERE spec_id=?", (json.dumps(raw), identity))
                conn.commit()
            await store.submit(lambda c: alter(c, "spec_demo__item_00", source_changed_at=None))
            work = await store.work_unattended(now=100)
            assert work["count"] == 10 and work["counts_by_repo"] == {"demo": 10}
            assert work["items"][-1]["spec_id"] == "spec_demo__item_00" and work["items"][-1]["age_s"] is None
            await store.evaluate_watch_wake(obs, now=100, root_binding=binding)
            await store.evaluate_watch_wake(obs, now=105, root_binding=binding)
            first = (await notices(store))[0]
            section = match_message_envelope(first["body"])["unattended_work"]
            assert section["count"] == 10 and len(section["items"]) == 8
            await store.evaluate_watch_wake(obs, now=110, root_binding=binding)
            assert len(await notices(store)) == 1
            assert unattended_signature(work) == unattended_signature(await store.work_unattended(now=110))
            # The unknown-age row is outside the eight stems, but its removal
            # must still change the digest's full population hash.
            await store.open_session("host-a", "successor", role="lead", visibility="hidden",
                assigned_spec_id="spec_demo__item_00", spec_ids=["demo__item_00", "demo__item_09"])
            await store.evaluate_watch_wake(obs, now=115, root_binding=binding)
            assert len(await notices(store)) == 2
            assert (await store.work_unattended())["count"] == 9
            # Stale cached rows are omitted explicitly; fresh matches survive.
            await store.submit(lambda c: alter(c, "spec_demo__item_09", observation={"quality": "stale", "observed_at": None, "error": "work_files_unsettled"}))
            mixed = await store.work_unattended(now=120)
            assert mixed["count"] == 8 and mixed["omitted_count"] == 1 and mixed["quality"] == "partial"
            await store.evaluate_watch_wake(obs, now=120, root_binding=binding)
            assert len(await notices(store)) == 3
            # Delivery recovery uses the existing episode identity machinery.
            latest = (await notices(store))[-1]["notice_id"]
            await store.submit(lambda c: c.execute("UPDATE v2_outbound_notices SET terminal_at='1970-01-01T00:02:01Z' WHERE notice_id=?", (latest,)))
            await store.evaluate_watch_wake(obs, now=125, root_binding=binding)
            assert len(await notices(store)) == 4
            for i in range(1, 9):
                await store.open_session("host-a", f"owner-{i}", role="nexus", assigned_spec_id=f"spec_demo__item_{i:02}")
            await store.evaluate_watch_wake(obs, now=130, root_binding=binding)
            assert match_message_envelope((await notices(store))[-1]["body"])["unattended_work"]["count"] == 0
            await store.evaluate_watch_wake(obs, now=135, root_binding=binding)
            assert len(await notices(store)) == 5  # one clearing update
            await store.mark_work_index_pending()
            unavailable = await store.work_unattended()
            assert unavailable["quality"] == "unavailable" and unavailable["available"] is False
            assert unavailable["omitted_count"] == 10 and unavailable["error"] == "work_index_unavailable"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["blocked", "backlog", "ready_for_dev", "needs_qa", "completed", "deprecated", "analysis"])
def test_nonactive_work_is_excluded(status):
    async def run():
        store = Store(); store.start()
        try:
            await cached(store, status=status)
            assert (await store.work_unattended())["count"] == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_all_paused_lanes_do_not_limit_board_membership():
    from test_work_lanes import run
    async def body(env):
        await env.adopt(state="paused", lead=False, owner="operator")
        await cached(env.store, count=3)
        work = await env.store.work_unattended()
        assert work["count"] == 3 and work["quality"] == "fresh"
    run(body)


def test_observation_clock_no_churn_and_disabled_digest(monkeypatch):
    from work_unattended import unattended_signature
    monkeypatch.setenv("PENTACLE_LANE_DIGEST_S", "5")
    async def run():
        store = Store(); store.start()
        try:
            _, binding, obs = await root(store)
            unavailable = await store.work_unattended()
            assert unavailable["available"] is False and unavailable["quality"] == "unavailable"
            await cached(store, now=100)
            before = await store.work_unattended(now=101)
            await store.evaluate_watch_wake(obs, now=100, root_binding=binding)
            await store.evaluate_watch_wake(obs, now=105, root_binding=binding)
            await cached(store, now=106)
            after = await store.work_unattended(now=110)
            assert before["items"][0]["observation"]["observed_at"] != after["items"][0]["observation"]["observed_at"]
            assert before["items"][0]["age_s"] != after["items"][0]["age_s"]
            assert unattended_signature(before) == unattended_signature(after)
            await store.evaluate_watch_wake(obs, now=110, root_binding=binding)
            assert len(await notices(store)) == 1
            monkeypatch.setenv("PENTACLE_LANE_DIGEST_S", "0")
            await store.evaluate_watch_wake(obs, now=120, root_binding=binding)
            assert len(await notices(store)) == 1
            assert (await store.work_unattended())["count"] == 1
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("role", ["lead", "nexus"])
@pytest.mark.parametrize("inactive", ["closed", "presumed_dead"])
def test_explicit_attachments_attend_but_inherited_tags_do_not(role, inactive):
    async def run():
        store = Store(); store.start()
        try:
            await cached(store, count=5)
            ids = [f"spec_demo__item_{i:02}" for i in range(5)]
            provenance = [
                {"spec_id": identity, "provenance": kind, "granting_principal": "operator",
                 "granted_at": "2026-10-26T00:00:00Z"}
                for identity, kind in zip(ids, ["spawn_explicit", "operator_v2", "spawn_explicit",
                                                 "parent_inherited", "handoff_inherited"])]
            await store.open_session("host-a", "owner", role=role, assigned_spec_id=ids[0],
                                     spec_ids=ids, spec_binding_provenance=provenance)
            assert [i["spec_id"] for i in (await store.work_unattended())["items"]] == ids[3:]
            from work_unattended import assigned_owners_conn
            assert await store.submit(lambda c: assigned_owners_conn(c, exclude="host-a:owner")) == {}
            fields = {"status": "closed"} if inactive == "closed" else {"presumed_dead_at": "2026-10-26T00:01:00Z"}
            await store.update_session("host-a", "owner", **fields)
            assert [i["spec_id"] for i in (await store.work_unattended())["items"]] == ids
        finally:
            store.stop()
    asyncio.run(run())
