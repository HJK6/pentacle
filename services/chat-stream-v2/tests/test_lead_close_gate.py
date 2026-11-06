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


@pytest.mark.parametrize("elevated", [False, True])
def test_real_socket_sole_active_lead_close_requires_disposition(tmp_path, elevated):
    async def run():
        store = Store(":memory:")
        store.start()
        tmux = IdleTmux()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        server = Server(store=store, sessions=sessions, local_host=HOST, port=0, seat_operator_authority=elevated)
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
    server = Server(store=store, sessions=sessions, local_host=HOST, port=0)
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
            if action in {"unchanged", "publisher_down", "same_id_pending", "unavailable"}:
                with pytest.raises(VerbError) as exc:
                    await server._on_close(request(row, operator_confirm=True, force=True,
                                                    disposition_waived_reason="must not bypass"))
                assert exc.value.code == "lead_close_requires_disposition"
                assert exc.value.extra["pending"] is True
                assert "spec assign" in str(exc.value.extra["next_commands"])
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
    {"role": "qa"}, {"role": "worker"}, {"role": "advisor"},
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


def test_authorized_parent_confirmation_does_not_waive_disposition():
    async def run():
        store, sessions, server, tmux, row = await environment(parent_stream_id=f"{HOST}:parent")
        try:
            parent = await store.open_session(HOST, "parent", role="worker")
            msg = request(row, operator_confirm=True)
            msg["_auth_context"] = {"token_verified": True, "stream_id": f"{HOST}:parent",
                                     "session_generation": parent["session_generation"]}
            with pytest.raises(VerbError, match="disposition"):
                await server._on_close(msg)
            assert (await store.fetch_session(HOST, "v2-lead"))["status"] == "open"
            assert tmux.kills == 0
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
            assert server.port == 0, "socket fixture must use an owned ephemeral port"
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


@pytest.mark.parametrize("role", ["lead", "planner"])
@pytest.mark.parametrize("live", [True, False])
def test_manager_close_gates_an_actually_live_pane_even_with_unknown_cached_presence(live, role):
    async def run():
        store, sessions, server, tmux, row = await environment(role=role)
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


@pytest.mark.parametrize("role", ["lead", "nexus", "planner"])
@pytest.mark.parametrize("status", ["analysis", "in_progress", "unknown"])
def test_accountable_owner_active_or_unknown_refuses_before_kill(role, status):
    async def run():
        from test_work_owed import observe
        store, sessions, server, tmux, row = await environment(role=role, visibility="hidden")
        try:
            if status == "unknown":
                await store.mark_work_index_pending()
            else:
                await observe(store, status, "active-source", 101, identity="spec_demo__item_00")
            refreshes = []
            async def refresh():
                refreshes.append(True)
            sessions.refresh_work_for_close = refresh
            with pytest.raises(VerbError) as exc:
                await server._on_close(request(row))
            assert exc.value.code == "lead_close_requires_disposition"
            assert exc.value.extra["pending"] is True
            assert refreshes == [True] and tmux.kills == 0
            assert (await store.fetch_session(HOST, "v2-lead"))["status"] == "open"
            await observe(store, "ready_for_dev", "accepted-disposition", 102, identity="spec_demo__item_00")
            assert (await server._on_close(request(row)))["type"] == "close.ok"
            assert (await store.work_owed())["total"] == 0
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"assigned_spec_id": ""}, {"assigned_spec_id": "spec_demo__other"},
    {"role": "qa"}, {"spec_id": None, "spec_ids": [], "assigned_spec_id": ""},
])
def test_former_owner_cannot_launder_active_obligation(change):
    async def run():
        from test_work_owed import observe
        store, sessions, server, tmux, row = await environment()
        try:
            await store.update_session(HOST, row["session_name"], **change)
            with pytest.raises(VerbError) as exc:
                await server._on_close(request(row))
            assert exc.value.code == "lead_close_requires_disposition" and tmux.kills == 0
            await observe(store, "blocked", "accepted-disposition", 101, identity="spec_demo__item_00")
            if change.get("assigned_spec_id") == "spec_demo__other":
                await observe(store, "backlog", "other-disposition", 102, identity="spec_demo__other")
            assert (await server._on_close(request(row)))["type"] == "close.ok"
        finally:
            store.stop()
    asyncio.run(run())


def test_elevated_seat_is_gated_but_connection_operator_recovery_records_debt():
    async def run():
        store, sessions, server, tmux, row = await environment(parent_stream_id=f"{HOST}:parent")
        try:
            parent = await store.open_session(HOST, "parent", role="worker")
            msg = request(row, operator_confirm=True)
            msg["_auth_context"] = {"token_verified": True, "operator_authenticated": True,
                "stream_id": f"{HOST}:parent", "session_generation": parent["session_generation"]}
            with pytest.raises(VerbError) as exc:
                await server._on_close(msg)
            assert exc.value.code == "lead_close_requires_disposition" and tmux.kills == 0
            msg["_auth_context"] = {"operator_authenticated": True, "token_verified": False,
                "operator_principal": "operator:fixture", "credential_id": "fixture"}
            assert (await server._on_close(msg))["type"] == "close.ok"
            assert (await store.fetch_session(HOST, "v2-lead"))["close_kind"] == "operator_close"
            assert (await store.work_owed())["total"] == 1
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("other_role", ["lead", "nexus", "planner"])
def test_two_owner_closes_recheck_at_final_graph_fence(other_role):
    async def run():
        store, sessions, server, tmux, row = await environment()
        other = await store.open_session(HOST, "other", role=other_role, assigned_spec_id="spec_demo__item_00")
        # Both requests pass the initial check while a successor still exists.
        ready, release = asyncio.Event(), asyncio.Event()
        initial, passed = sessions._guard_lead_disposition, []
        async def barrier(*args, **kwargs):
            result = await initial(*args, **kwargs)
            passed.append(True)
            if len(passed) == 2:
                ready.set()
            await release.wait()
            return result
        sessions._guard_lead_disposition = barrier
        tasks = [asyncio.create_task(server._on_close(request(owner))) for owner in (row, other)]
        try:
            await asyncio.wait_for(ready.wait(), 2)
            release.set()
            replies = await asyncio.gather(*tasks, return_exceptions=True)
            assert sum(isinstance(r, VerbError) and r.code == "lead_close_requires_disposition" for r in replies) == 1
            assert sum(isinstance(r, dict) and r.get("type") == "close.ok" for r in replies) == 1
            assert tmux.kills == 1
            owners = [await store.fetch_session(HOST, owner["session_name"]) for owner in (row, other)]
            assert sum(owner["status"] == "open" for owner in owners) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("assignment", ["", "spec_demo__other"])
def test_close_racing_assignment_removal_at_final_fence_retains_obligation(assignment):
    async def run():
        from uiverbs import UIVerbs
        store, sessions, server, tmux, row = await environment()
        other = await store.open_session(HOST, "other", role="lead", assigned_spec_id="spec_demo__item_00")
        # A real assignment update must queue behind the close's graph fence.
        entered, release = asyncio.Event(), asyncio.Event()
        original = sessions._close_locked
        async def fenced(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        sessions._close_locked = fenced
        close = asyncio.create_task(server._on_close(request(row)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            from types import SimpleNamespace
            async def checkpoint(*args):
                return None
            catalog = SimpleNamespace(resolution_for=lambda spec: "resolved",
                                      canonical_spec_identity=lambda spec: spec)
            ui = UIVerbs(store=store, sessions=sessions,
                         spawnctl=SimpleNamespace(work_checkpoint_warning=checkpoint), specs=catalog)
            mutation = asyncio.create_task(ui.session_spec_update({"action": "assign" if assignment else "unassign",
                "host": HOST, "session_name": "other", "spec_id": assignment,
                "_auth_context": {"operator_authenticated": True}}))
            await asyncio.sleep(0.02)
            assert not mutation.done(), "assignment removal escaped the final graph fence"
            release.set()
            assert (await close)["type"] == "close.ok"
            await mutation
            assert (await store.fetch_session(HOST, "other"))["assigned_spec_id"] == assignment
            with pytest.raises(VerbError) as exc:
                await server._on_close(request(other))
            assert exc.value.code == "lead_close_requires_disposition"
            owed = await store.work_owed()
            assert any(item["spec_id"] == "spec_demo__item_00" for item in owed["items"])
        finally:
            release.set()
            await asyncio.gather(close, return_exceptions=True)
            if "mutation" in locals():
                await asyncio.gather(mutation, return_exceptions=True)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("settled,active", [("backlog", "in_progress"), ("ready_for_dev", "analysis")])
def test_close_refreshes_initially_settled_assignment_before_authorizing(settled, active):
    async def run():
        from test_work_owed import observe
        store, sessions, server, tmux, row = await environment()
        try:
            await observe(store, settled, "settled", 101, identity="spec_demo__item_00")
            calls = []
            async def refresh():
                calls.append(True)
                await observe(store, active, "reopened", 102, identity="spec_demo__item_00")
            sessions.refresh_work_for_close = refresh
            with pytest.raises(VerbError) as exc:
                await server._on_close(request(row))
            assert exc.value.code == "lead_close_requires_disposition"
            assert calls == [True] and tmux.kills == 0
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["raises", "drain_error"])
def test_failed_refresh_cannot_authorize_from_cached_settled_status(failure):
    from work_lanes_projection import WorkLanesInventory
    async def run():
        from test_work_owed import observe
        store, sessions, server, tmux, row = await environment()
        inventory = None
        try:
            await observe(store, "backlog", "cached-settled", 101, identity="spec_demo__item_00")
            if failure == "drain_error":
                class Specs:
                    def scan_work_observations(self):
                        raise OSError("synthetic index unavailable")
                    def stop(self):
                        pass
                inventory = WorkLanesInventory(store, sessions, server.broadcast, specs=Specs(), refresh_min_s=0)
                inventory._running = True
                sessions.refresh_work_for_close = inventory.refresh_for_close
            else:
                async def refresh():
                    raise OSError("synthetic index unavailable")
                sessions.refresh_work_for_close = refresh
            with pytest.raises(VerbError) as exc:
                await server._on_close(request(row))
            assert exc.value.code == "lead_close_requires_disposition"
            assert exc.value.extra["pending"] is True and tmux.kills == 0
            assert (await store.fetch_session(HOST, row["session_name"]))["status"] == "open"
        finally:
            if inventory:
                await inventory.stop()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("online", [False, True])
def test_parent_confirmed_offline_recovery_preserves_planner_debt(online):
    async def run():
        from test_offline_operator_close import build
        from test_work_owed import observe
        store, peer, sessions, server, row = await build()
        try:
            peer.online = online
            await observe(store, "analysis", "planning", 100, identity="spec_demo__item_00")
            parent = await store.open_session("local-peer", "parent", role="worker")
            await store.update_session("remote-peer", "v2-offline", role="planner",
                assigned_spec_id="spec_demo__item_00", parent_stream_id="local-peer:parent")
            msg = {"stream_id": "remote-peer:v2-offline", "expected_generation": row["session_generation"],
                "operator_confirm": True, "_auth_context": {"token_verified": True,
                "stream_id": "local-peer:parent", "session_generation": parent["session_generation"]}}
            if online:
                with pytest.raises(VerbError) as exc:
                    await server._on_close(msg)
                assert exc.value.code == "lead_close_requires_disposition"
                assert (await store.fetch_session("remote-peer", "v2-offline"))["status"] == "open"
            else:
                reply = await server._on_close(msg)
                assert reply["type"] == "close.ok" and reply["reap_status"] == "deferred_host_offline"
                owed = await store.work_owed()
                assert owed["total"] == 1
                assert owed["items"][0]["session_generation"] == row["session_generation"]
                assert (await store.get_deferred_reap("remote-peer:v2-offline"))["done_at"] is None
            assert peer.kills == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_approved_ruling_and_done_report_do_not_waive_owner_disposition():
    async def run():
        from test_approved_close_retry import journey, HOST as lane_host
        from test_work_owed import observe
        async with journey() as env:
            await observe(env.store, "analysis", "active-planning", 100, identity="spec_demo__item_00")
            await env.store.update_session(lane_host, "lane", role="planner", assigned_spec_id="spec_demo__item_00")
            env.tmux.text = "ready\n"
            assert (await env.request())["type"] == "close.pending_ruling"
            approved = await env.approve()
            assert approved["state"] == "approved_but_not_closed"
            assert json.loads((await env.row())["outcome_json"])["error_code"] == "lead_close_requires_disposition"
            assert env.tmux.kills == 0
            assert (await env.store.fetch_session(lane_host, "lane"))["status"] == "open"
    asyncio.run(run())


@pytest.mark.parametrize("role,phase,status,allowed", [
    ("qa", "spec", "backlog", True),
    ("planner", None, "backlog", True),
    ("planner", None, "ready_for_dev", True),
    ("lead", None, "in_progress", False),
])
@pytest.mark.parametrize("bound_fd", [False, True], ids=["parent", "configured-fd"])
def test_fd_parent_closes_reported_literal_work_without_extra_disposition(role, phase, status, allowed, bound_fd):
    """Real typed reports do not add a move for settled QA/planner work."""
    async def run():
        from ledger import Ledger
        from test_work_owed import observe
        spec = "spec_demo__item_00"
        # The status predates ownership: no post-assignment move is required.
        store, sessions, server, tmux, row = await environment(role="worker", assigned_spec_id="", spec_ids=[spec])
        try:
            store.set_spec_identity_resolver(lambda identity: identity if identity == spec else None)
            await observe(store, status, "already-settled-source", 101, identity=spec)
            parent = await store.open_session(HOST, "fd", role="lead", assigned_spec_id="")
            await store.update_session(HOST, row["session_name"], role=role, phase=phase,
                assigned_spec_id=spec, parent_stream_id=f"{HOST}:fd", visibility="hidden")
            await sessions.refresh()
            stream = f"{HOST}:{row['session_name']}"
            report = {"report_id": "reported-" + role + "-" + status, "from_stream_id": stream,
                "msg_id": 0, "status": "done", "summary": "Assigned review or planning work finished",
                "findings": [], "next_action": "Parent reviews the existing disposition",
                "_auth_context": {"token_verified": True, "stream_id": stream,
                                  "session_generation": row["session_generation"]}}
            if role == "qa":
                report.update(qa_verdict="accept", target_sha="c" * 40,
                    extras={"qa_review": {"reviewed_scope": "spec", "candidate_identity": "c" * 40,
                                           "gate_evidence_digest": "d" * 64}})
            stored = await Ledger(store, sessions=sessions).ingest(report)
            assert stored["status"] == "done"
            if role == "qa":
                assert stored["qa_verdict"] == "accept" and stored["target_sha"] == "c" * 40
            if bound_fd:
                from assistant_composite import AssistantComposite, AssistantCompositeConfig
                composite = AssistantComposite(store, config=AssistantCompositeConfig.from_env({
                    "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                    "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": f"{HOST}:assistant",
                    "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": f"{HOST}:fd",
                    "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": parent["session_generation"],
                }))
                server.assistant_composite = composite
                sessions.assistant.front_desk = composite
            close = request(row, operator_confirm=True, reason="Review finished; close owned fixture",
                            request_id="fd-typed-close")
            close["_auth_context"] = {"token_verified": True, "stream_id": f"{HOST}:fd",
                                      "session_generation": parent["session_generation"]}
            if allowed:
                assert (await server._on_close(close))["type"] == "close.ok"
                assert tmux.kills == 1 and (await store.work_owed())["total"] == 0
                closed = await store.fetch_session(HOST, row["session_name"])
                assert closed["close_kind"] == ("manager_close" if bound_fd else "session_close")
            else:
                with pytest.raises(VerbError) as exc:
                    await server._on_close(close)
                assert exc.value.code == "lead_close_requires_disposition" and tmux.kills == 0
                assert (await store.fetch_session(HOST, row["session_name"]))["status"] == "open"
            observed = await store.submit(lambda conn: json.loads(conn.execute(
                "SELECT record_json FROM v2_work_item_observations WHERE spec_id=?", (spec,)).fetchone()[0]))
            assert observed["member"]["status"] == status and observed["source_hash"] == "already-settled-source"
        finally:
            store.stop()
    asyncio.run(run())


def test_bound_fd_ordinary_owner_close_refuses_then_accepts_real_disposition():
    from test_fd_close_visible import setup, close, FD
    from test_lifecycle_authority import HOST as FD_HOST, scenario, _refused
    from test_work_owed import observe
    async def run(env):
        auth = await setup(env)
        await cached(env.store)
        await env.store.update_session(FD_HOST, "target", role="lead",
            assigned_spec_id="spec_demo__item_00", parent_stream_id=FD)
        await env.sessions.refresh()
        await _refused(env.server._on_close(close(auth, operator_confirm=True)), "lead_close_requires_disposition")
        assert env.tmux.killed == []
        assert (await env.store.fetch_session(FD_HOST, "target"))["status"] == "open"
        await observe(env.store, "backlog", "accepted-disposition", 101, identity="spec_demo__item_00")
        assert (await env.server._on_close(close(auth, request_id="after-disposition")))["type"] == "close.ok"
        assert env.tmux.killed == ["target"] and (await env.store.work_owed())["total"] == 0
    scenario(run)


@pytest.mark.parametrize("case", ["busy", "idle", "unknown", "no_force", "nonchild", "children", "pending_spawn", "missing_reason", "missing_request", "stale_fd"])
def test_bound_fd_busy_override_is_explicit_recovery_with_debt(case):
    from test_fd_close_visible import setup, close, FD
    from test_lifecycle_authority import HOST as FD_HOST, scenario
    async def run(env):
        auth = await setup(env)
        await cached(env.store)
        await env.store.update_session(FD_HOST, "target", role="lead", assigned_spec_id="spec_demo__item_00",
            parent_stream_id=FD if case != "nonchild" else None)
        await env.sessions.refresh()
        if case != "idle":
            env.tmux.working.add("target")
        if case == "unknown":
            async def unavailable(*args, **kwargs):
                return False, ""
            env.tmux.capture_checked = unavailable
        if case == "children":
            await env.open("grandchild", parent_stream_id=FD_HOST + ":target")
        if case == "pending_spawn":
            assert await env.store.reserve_stream_id(FD_HOST, "pending", ttl_s=60, request_id="sp", nonce="n")
            assert await env.store.record_spawn_intent(FD_HOST, "pending",
                {"open_fields": {"parent_stream_id": FD_HOST + ":target"}}, request_id="sp", nonce="n")
        msg = close(auth, force=case != "no_force", operator_confirm=True)
        if case == "missing_reason":
            msg.pop("reason")
        if case == "missing_request":
            msg.pop("request_id")
        if case == "stale_fd":
            auth["session_generation"] = "stale"
        if case == "busy":
            assert (await env.server._on_close(msg))["type"] == "close.ok"
            assert env.tmux.killed == ["target"]
            owed = await env.store.work_owed()
            assert [(i["spec_id"], i["closing_stream_id"]) for i in owed["items"]] == [
                ("spec_demo__item_00", FD_HOST + ":target")]
            from store_work_index import _state_conn
            state = await env.store.submit(_state_conn)
            debt, = state["_lead_dispositions"]["owed"].values()
            assert debt["front_desk_owned"] is True
        else:
            with pytest.raises(VerbError):
                await env.server._on_close(msg)
            assert env.tmux.killed == []
            assert (await env.store.fetch_session(FD_HOST, "target"))["status"] == "open"
    scenario(run)


def test_bound_fd_busy_override_socket_uses_cli_force_wire_shape():
    from test_fd_close_visible import setup, FD
    from test_lifecycle_authority import HOST as FD_HOST, scenario
    async def run(env):
        auth = await setup(env)
        await cached(env.store)
        await env.store.update_session(FD_HOST, "target", role="lead", assigned_spec_id="spec_demo__item_00",
                                       parent_stream_id=FD)
        env.tmux.working.add("target")
        await env.sessions.refresh()
        env.server.port = 0  # Env normally never binds; this socket fixture owns an ephemeral port.
        token = "synthetic-fd-recovery"
        await env.store.grant_stream_token(FD_HOST, "front-desk", hashlib.sha256(token.encode()).hexdigest(),
                                           STREAM_TOKEN_HASH_VERSION)
        try:
            port = await env.server.bind()
            async with connect(f"ws://127.0.0.1:{port}", close_timeout=0) as ws:
                await ws.send(json.dumps({"type": "hello", "from_stream_id": FD, "stream_token": token,
                                         "subscribe": {"snapshot": False, "mode": "rpc"}}))
                await ws.send(json.dumps({"type": "close", "request_id": "fd-cli-force", "host": FD_HOST,
                    "session_name": "target", "force": True, "reason": "Recover owned busy fixture"}))
                async with asyncio.timeout(5):
                    while True:
                        reply = json.loads(await ws.recv())
                        if reply.get("request_id") == "fd-cli-force":
                            break
            assert reply["type"] == "close.ok", reply
            assert env.tmux.killed == ["target"]
            assert (await env.store.work_owed())["total"] == 1
            audit = await env.store.latest_close_audit(FD_HOST + ":target")
            assert audit["actor_kind"] == "front_desk" and audit["request_id"] == "fd-cli-force"
        finally:
            await env.server.close()
    scenario(run)


@pytest.mark.parametrize("change,code", [("parent", "close_unauthorized"),
    ("binding", "authority_holder_required"), ("grandchild", "close_live_children")])
def test_bound_fd_busy_override_rechecks_final_admission(change, code):
    from test_fd_close_visible import setup, close, FD
    from test_lifecycle_authority import HOST as FD_HOST, scenario, _refused
    async def run(env):
        auth = await setup(env)
        await cached(env.store)
        await env.store.update_session(FD_HOST, "target", role="lead", assigned_spec_id="spec_demo__item_00",
                                       parent_stream_id=FD)
        env.tmux.working.add("target")
        await env.sessions.refresh()
        reached, resume = asyncio.Event(), asyncio.Event()
        original = env.sessions._probe_capture_liveness
        async def paused(*args, **kwargs):
            value = await original(*args, **kwargs)
            reached.set()
            await resume.wait()
            return value
        env.sessions._probe_capture_liveness = paused
        task = asyncio.create_task(env.server._on_close(close(auth, force=True)))
        await asyncio.wait_for(reached.wait(), 5)
        try:
            if change == "parent":
                await env.store.update_session(FD_HOST, "target", parent_stream_id=None)
            elif change == "binding":
                await env.open("replacement", provider="codex")
                await env.store.recover_assistant_binding(name=env.server.assistant_composite.env_config.name,
                    target_stream_id=FD_HOST + ":replacement", target_generation=await env.gen("replacement"))
            else:
                await env.open("grandchild", parent_stream_id=FD_HOST + ":target")
        finally:
            resume.set()
        await _refused(task, code)
        assert env.tmux.killed == []
        assert (await env.store.fetch_session(FD_HOST, "target"))["status"] == "open"
    scenario(run)
