"""Voluntary lead closes use observed disposition and current assignment."""
import asyncio
import hashlib
import json

import pytest

from websockets.asyncio.client import connect

from server import Server
from sessions import Sessions, VerbError
from store import Store, STREAM_TOKEN_HASH_VERSION
from test_close_defer_and_attribution import HOST, IdleTmux
from test_work_unattended import cached


def test_real_socket_sole_active_lead_close_requires_disposition(tmp_path):
    async def run():
        store = Store(":memory:")
        store.start()
        tmux = IdleTmux()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        server = Server(store=store, sessions=sessions, local_host=HOST, port=0)
        name = "v2-close-fixture"
        stream = f"{HOST}:{name}"
        try:
            from _shared.work_observations import read_work_candidate
            import time
            folder = tmp_path / "work/in_progress/demo__item_00"
            folder.mkdir(parents=True)
            spec = b"---\nid: spec_demo__item_00\ntitle: Active fixture\n---\n## Acceptance Criteria\n- [ ] Deliver.\n"
            summary = b"**Status** - Working.\n"
            (folder / "spec.md").write_bytes(spec)
            (folder / "summary.md").write_bytes(summary)
            candidate = read_work_candidate(folder, "in_progress", now=time.time()+1, quiet_s=0)
            await store.reconcile_work_observations({"available": True, "root_configured": True,
                "candidates": {"spec_demo__item_00": [candidate]}, "errors": {}, "scanned_at": 100},
                settle_s=0, sweep=True)
            row = await store.open_session(HOST, name, role="lead", visibility="hidden",
                assigned_spec_id="spec_demo__item_00", spec_ids=["spec_demo__item_00"],
                pane_status="pane_alive", bootstrap_state="ready")
            await store.grant_stream_token(HOST, name,
                hashlib.sha256(b"disposable-close-token").hexdigest(), STREAM_TOKEN_HASH_VERSION)
            await sessions.refresh()
            port = await server.bind()
            async with connect(f"ws://127.0.0.1:{port}", close_timeout=0) as ws:
                await ws.send(json.dumps({"type": "hello", "from_stream_id": stream,
                    "stream_token": "disposable-close-token",
                    "subscribe": {"snapshot": False, "mode": "rpc"}}))
                await ws.send(json.dumps({"type": "close", "request_id": "sole-lead-close",
                    "stream_id": stream, "expected_generation": row["session_generation"]}))
                async with asyncio.timeout(5):
                    while True:
                        reply = json.loads(await ws.recv())
                        if reply.get("request_id") == "sole-lead-close":
                            break
            current = await store.fetch_session(HOST, name)
            assert reply.get("error_code") == "lead_close_requires_disposition", {
                "reply": reply, "row_status": current["status"], "termination_calls": tmux.kills}
            assert current["status"] == "open"
            assert tmux.kills == 0
            assert reply["spec_id"] == "spec_demo__item_00"
            assert reply["observed_status"] == "in_progress"
            assert reply["observed_at"]
            assert reply["next_commands"]
            command = next(c for c in reply["next_commands"] if " move <<'JSON'" in c)
            move = json.loads(command.split("\n", 1)[1].rsplit("\n", 1)[0])
            assert move["item_id"] == "spec_demo__item_00"
            assert move["requesting_stream_id"] == stream and move["reason"] and move["next_action"]
            assert move["expected"] == {
                "work/in_progress/demo__item_00/spec.md": hashlib.sha256(spec).hexdigest(),
                "work/in_progress/demo__item_00/summary.md": hashlib.sha256(summary).hexdigest()}
        finally:
            await server.close()
            store.stop()
    asyncio.run(run())


async def environment(**fields):
    store = Store(); store.start()
    tmux = IdleTmux()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    server = Server(store=store, sessions=sessions, local_host=HOST)
    await cached(store)
    row = await store.open_session(HOST, "v2-lead", pane_status="pane_alive",
        **({"role": "lead", "assigned_spec_id": "spec_demo__item_00"} | fields))
    await sessions.refresh()
    return store, sessions, server, tmux, row


def request(row, **extra):
    stream = f"{HOST}:{row['session_name']}"
    return {"stream_id": stream, "expected_generation": row["session_generation"],
            "_auth_context": {"token_verified": True, "stream_id": stream,
                              "session_generation": row["session_generation"]}, **extra}


@pytest.mark.parametrize("action", ["unchanged", "move", "successor", "unavailable", "publisher_down", "same_id_pending"])
def test_one_bounded_refresh_observes_current_disposition_and_pending_recovery(action):
    async def run():
        store, sessions, server, tmux, row = await environment()
        calls = []
        async def refresh():
            calls.append(1)
            # The global graph stays available while the close waits for IO.
            await asyncio.wait_for(sessions._graph_lock.acquire(), 0.1)
            sessions._graph_lock.release()
            if action == "move":
                from test_work_owed import observe
                await observe(store, "backlog", "accepted-move-revision", 101, identity="spec_demo__item_00")
            elif action == "successor":
                await store.open_session(HOST, "successor", role="nexus", assigned_spec_id="spec_demo__item_00")
            elif action == "unavailable":
                await store.mark_work_index_pending()
        sessions.refresh_work_for_close = refresh
        try:
            if action in {"unchanged", "publisher_down", "same_id_pending"}:
                with pytest.raises(VerbError) as exc:
                    await server._on_close(request(row, operator_confirm=True, force=True,
                                                    disposition_waived_reason="must not bypass"))
                assert exc.value.code == "lead_close_requires_disposition"
                assert exc.value.extra["pending"] is True
                assert "operator-confirm" in str(exc.value.extra["next_commands"])
                assert tmux.kills == 0
            else:
                assert (await server._on_close(request(row)))["type"] == "close.ok"
                assert tmux.kills == 1
                owed = await store.work_owed()
                assert owed["total"] == int(action == "unavailable")
                if action == "unavailable":
                    assert owed["unknown_count"] == 1
            assert calls == [1]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("fields", [
    {"role": "qa"}, {"role": "planner"}, {"role": "worker"}, {"role": "advisor"},
    {"role": "evaluator"}, {"assigned_spec_id": ""},
])
def test_nonlead_and_unassigned_seats_are_not_gated(fields):
    async def run():
        store, sessions, server, tmux, row = await environment(**fields)
        try:
            assert (await server._on_close(request(row)))["type"] == "close.ok"
            assert tmux.kills == 1
            assert (await store.work_owed())["total"] == 0
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("fields", [
    {"assigned_spec_id": "", "spec_ids": ["spec_demo__item_00"]},
    {"assigned_spec_id": "spec_demo__item_00", "role": "qa"},
    {"assigned_spec_id": "spec_demo__item_00", "status": "closed"},
    {"assigned_spec_id": "spec_demo__item_00", "presumed_dead_at": "2026-10-25T00:00:00Z"},
    {"assigned_spec_id": "spec_demo__other"},
])
def test_tagged_closed_dead_and_differently_assigned_seats_are_not_successors(fields):
    async def run():
        store, sessions, server, tmux, row = await environment()
        try:
            await store.open_session(HOST, "other", **({"role": "lead"} | fields))
            if fields.get("status") == "closed":
                await store.update_session(HOST, "other", status="closed")
            with pytest.raises(VerbError) as exc:
                await server._on_close(request(row))
            assert exc.value.code == "lead_close_requires_disposition"
            assert tmux.kills == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_authorized_parent_operator_route_closes_and_records_debt():
    async def run():
        store, sessions, server, tmux, row = await environment(parent_stream_id=f"{HOST}:parent")
        try:
            parent = await store.open_session(HOST, "parent", role="worker")
            msg = request(row, operator_confirm=True)
            msg["_auth_context"] = {"token_verified": True, "stream_id": f"{HOST}:parent",
                                     "session_generation": parent["session_generation"]}
            assert (await server._on_close(msg))["type"] == "close.ok"
            assert (await store.fetch_session(HOST, "v2-lead"))["close_kind"] == "operator_close"
            assert (await store.work_owed())["total"] == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_refresh_timeout_is_pending_and_cancels_only_its_waiter(monkeypatch):
    monkeypatch.setattr("sessions.LEAD_CLOSE_REFRESH_TIMEOUT_S", 0.01)
    async def run():
        store, sessions, server, tmux, row = await environment()
        cancelled = []
        async def refresh():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        sessions.refresh_work_for_close = refresh
        try:
            with pytest.raises(VerbError) as exc:
                await server._on_close(request(row))
            assert exc.value.code == "lead_close_requires_disposition"
            assert cancelled == [True] and tmux.kills == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_generation_replaced_during_refresh_is_never_closed():
    async def run():
        store, sessions, server, tmux, row = await environment()
        async def refresh():
            await store.mark_closed(HOST, "v2-lead", closed_at="2026-10-25T02:00:00Z", pane_status="pane_dead",
                                    expected_generation=row["session_generation"], close_kind="operator_close")
            await store.open_session(HOST, "v2-lead", role="lead", assigned_spec_id="spec_demo__item_00")
        sessions.refresh_work_for_close = refresh
        try:
            with pytest.raises(VerbError) as exc:
                await server._on_close(request(row))
            assert exc.value.code == "actor_generation_changed"
            current = await store.fetch_session(HOST, "v2-lead")
            assert current["session_generation"] != row["session_generation"] and current["status"] == "open"
            assert tmux.kills == 0 and (await store.work_owed())["total"] == 0
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("cached_target", [True, False])
def test_parent_socket_close_without_generation_never_closes_refresh_replacement(cached_target):
    async def run():
        parent_stream = f"{HOST}:parent"
        store, sessions, server, tmux, row = await environment(parent_stream_id=parent_stream)
        stream = f"{HOST}:v2-lead"
        try:
            await store.open_session(HOST, "parent", role="worker")
            await store.grant_stream_token(HOST, "parent",
                hashlib.sha256(b"disposable-parent-token").hexdigest(), STREAM_TOKEN_HASH_VERSION)
            if not cached_target:
                sessions._inv.pop(stream)
            async def refresh():
                await store.mark_closed(HOST, "v2-lead", expected_generation=row["session_generation"],
                    closed_at="2026-10-25T02:00:00Z", pane_status="pane_dead", close_kind="operator_close")
                await store.open_session(HOST, "v2-lead", role="lead", parent_stream_id=parent_stream,
                                         assigned_spec_id="spec_demo__item_00")
            sessions.refresh_work_for_close = refresh
            port = await server.bind()
            async with connect(f"ws://127.0.0.1:{port}", close_timeout=0) as ws:
                await ws.send(json.dumps({"type": "hello", "from_stream_id": parent_stream,
                    "stream_token": "disposable-parent-token",
                    "subscribe": {"snapshot": False, "mode": "rpc"}}))
                await ws.send(json.dumps({"type": "close", "request_id": "parent-unpinned-close",
                                          "stream_id": stream}))
                async with asyncio.timeout(5):
                    while True:
                        reply = json.loads(await ws.recv())
                        if reply.get("request_id") == "parent-unpinned-close":
                            break
            current = await store.fetch_session(HOST, "v2-lead")
            assert current["session_generation"] != row["session_generation"]
            assert current["status"] == "open" and tmux.kills == 0, {
                "reply_type": reply.get("type"), "status": current["status"], "kills": tmux.kills}
            assert reply["type"] == "close.already_closed"
        finally:
            await server.close()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("probe_result", ["raises", "unknown", "unreachable", None])
def test_uncertain_manager_probe_cannot_skip_gate_before_a_later_live_probe(probe_result):
    async def run():
        store, sessions, server, tmux, row = await environment()
        probes, refreshes = [], []
        async def state(_name):
            probes.append(probe_result)
            if probe_result == "raises":
                raise OSError("transient synthetic liveness failure")
            return probe_result
        async def refresh():
            refreshes.append(True)
        tmux.session_state = state
        tmux.alive = True  # The later termination-path has_session sees a live, idle pane.
        sessions.refresh_work_for_close = refresh
        await store.update_session(HOST, "v2-lead", pane_status="pane_unknown")
        try:
            try:
                result = await sessions.close(HOST, "v2-lead", expected_generation=row["session_generation"],
                                              close_kind="manager_close", requires_idle=True)
            except VerbError as exc:
                assert exc.code == "lead_close_requires_disposition"
            else:
                current = await store.fetch_session(HOST, "v2-lead")
                pytest.fail(f"manager close bypassed disposition: closed={result.get('closed')}, "
                            f"failed={result.get('failed')}, "
                            f"status={current['status']}, kills={tmux.kills}, {refreshes=}")
            assert probes == [probe_result] and refreshes == [True]
            assert (await store.fetch_session(HOST, "v2-lead"))["status"] == "open"
            assert tmux.kills == 0
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("live", [True, False])
def test_manager_close_gates_an_actually_live_pane_even_with_unknown_cached_presence(live):
    async def run():
        store, sessions, server, tmux, row = await environment()
        tmux.alive = live
        async def state(_name):
            return "alive" if tmux.alive else "gone"
        tmux.session_state = state
        await store.update_session(HOST, "v2-lead", pane_status="pane_unknown")
        try:
            if live:
                with pytest.raises(VerbError) as exc:
                    await sessions.close(HOST, "v2-lead", expected_generation=row["session_generation"],
                                         close_kind="manager_close")
                assert exc.value.code == "lead_close_requires_disposition"
                assert tmux.kills == 0
            else:
                result = await sessions.close(HOST, "v2-lead", expected_generation=row["session_generation"],
                                              close_kind="manager_close")
                assert not result["already_closed"] and not result.get("failed")
                assert (await store.work_owed())["total"] == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_close_uses_existing_dirty_drain_to_observe_one_accepted_move():
    from work_lanes_projection import WorkLanesInventory
    class Specs:
        calls = 0
        def scan_work_observations(self):
            self.calls += 1
            return {"available": True, "root_configured": True, "errors": {}, "scanned_at": 101,
                "candidates": {"spec_demo__item_00": [{"quality": "fresh", "source_hash": "accepted-move",
                    "path": "/fixture/work/backlog/demo__item_00", "facts": {"status": "backlog", "title": "Moved",
                        "terminal": None, "ac_checked": 0, "ac_total": 1, "estimate": None}}]}}
        def stop(self):
            pass
    async def run():
        store, sessions, server, tmux, row = await environment()
        specs = Specs()
        inventory = WorkLanesInventory(store, sessions, server.broadcast, specs=specs, refresh_min_s=0)
        inventory._running = True
        sessions.refresh_work_for_close = inventory.refresh_for_close
        try:
            assert (await server._on_close(request(row)))["type"] == "close.ok"
            assert specs.calls == 1 and tmux.kills == 1
            assert (await store.work_owed())["total"] == 0
        finally:
            await inventory.stop()
            store.stop()
    asyncio.run(run())
