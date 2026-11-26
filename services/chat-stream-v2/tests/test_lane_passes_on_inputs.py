"""Lane reconcile passes run when a lane input changes, not on seat activity.

A session-inventory emit re-emits the lane frame; the index scan, lifecycle
reconcile and episode evaluation run only for a lane input: a durable seat
field the reconciles read, the open set, a lane operation, a report, a close,
the watcher, boot, the periodic pass, an external packet or a lane completion.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from store import Store
from work_lanes_projection import WorkLanesInventory
from test_external_work import exercise, mail, packet, packet_index
from work_lane_link import FIXTURE_LANE_ID, link_specs_conn

SEAT = {"stream_id": "fixture:v2-seat", "session_generation": "g1", "status": "open",
        "pane_status": "pane_alive", "role": "lead", "visibility": "default",
        "working": False, "busy": False, "context_tokens": 1}


class Seats:
    def __init__(self, *rows):
        self.rows = [dict(row) for row in rows]

    def list_open(self):
        return [dict(row) for row in self.rows]


def _session_wrapper(work_lanes):
    """The production main.py wrapper around the session-inventory emit."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
    wrapper = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == "_emit_sessions_and_lanes")

    async def session_emit(*, immediate=False):
        return True
    namespace = {"_session_emit": session_emit, "work_lanes": work_lanes}
    exec(compile(ast.Module(body=[wrapper], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["_emit_sessions_and_lanes"]


class Counted:
    """A real Store and inventory, counting reconcile passes and lane emits."""

    def __init__(self, seats):
        self.store = Store(":memory:"); self.store.start()
        self.counts = {"lifecycle": 0, "episodes": 0, "emits": 0}
        lifecycle, episodes = self.store.reconcile_lane_lifecycle, self.store.reconcile_work_lane_episodes

        async def counted_lifecycle(*args, **kwargs):
            self.counts["lifecycle"] += 1
            return await lifecycle(*args, **kwargs)

        async def counted_episodes(*args, **kwargs):
            self.counts["episodes"] += 1
            return await episodes(*args, **kwargs)
        self.store.reconcile_lane_lifecycle = counted_lifecycle
        self.store.reconcile_work_lane_episodes = counted_episodes
        self.frames = []

        async def broadcast(frame):
            self.frames.append(frame)
        self.seats = seats
        self.inv = WorkLanesInventory(self.store, seats, broadcast, refresh_min_s=0)
        emit = self.inv.emit_if_changed

        async def counted_emit():
            self.counts["emits"] += 1
            return await emit()
        self.inv.emit_if_changed = counted_emit
        self.emit_sessions = _session_wrapper(self.inv)

    async def settle(self):
        while self.inv._task is not None and not self.inv._task.done():
            await self.inv._task

    def passes(self):
        assert self.counts["lifecycle"] == self.counts["episodes"]
        return self.counts["lifecycle"]

    async def booted(self):
        self.inv.refresh()
        await self.settle()
        assert self.passes() == 1
        return self

    def close(self):
        self.store.stop()


def run(body, *rows):
    async def go():
        rig = await Counted(Seats(*rows) if rows else Seats(SEAT)).booted()
        try:
            await body(rig)
        finally:
            rig.close()
    asyncio.run(go())


@pytest.mark.timeout(30)
def test_activity_only_inventory_changes_run_no_reconcile_and_still_emit():
    async def body(rig):
        emits = rig.counts["emits"]
        for flip in range(10):
            rig.seats.rows[0].update(working=bool(flip % 2), busy=not flip % 2, context_tokens=flip)
            await rig.emit_sessions(immediate=True)
            await rig.settle()
        print(f"activity burst of 10: reconcile passes {rig.passes() - 1}, lane emits {rig.counts['emits'] - emits}")
        assert rig.passes() == 1, "seat activity must not reconcile lanes"
        assert rig.counts["emits"] > emits, "the lane frame is still re-emitted"
    run(body)


@pytest.mark.timeout(30)
@pytest.mark.parametrize("change", [
    ("open", lambda rows: rows.append({**SEAT, "stream_id": "fixture:v2-new"})),
    ("close", lambda rows: rows.pop()),
    ("pane_status", lambda rows: rows[0].update(pane_status="pane_dead")),
    ("presumed_dead", lambda rows: rows[0].update(presumed_dead_at="2026-11-26T00:00:00Z")),
    ("offline", lambda rows: rows[0].update(offline_since_ts=1)),
    ("generation", lambda rows: rows[0].update(session_generation="g2")),
    ("assignment", lambda rows: rows[0].update(assigned_spec_id="spec_fixture")),
    ("role", lambda rows: rows[0].update(role="worker")),
    ("visibility", lambda rows: rows[0].update(visibility="hidden")),
    ("provider", lambda rows: rows[0].update(provider="composite")),
    ("grant", lambda rows: rows[0].update(spec_binding_provenance=[
        {"spec_id": "spec_fixture", "provenance": "operator_v2", "granting_principal": "operator",
         "granted_at": "2026-11-26T00:00:00Z"}])),
], ids=lambda change: change[0] if isinstance(change, tuple) else None)
def test_each_lane_input_field_on_the_session_emit_reconciles_once(change):
    async def body(rig):
        change[1](rig.seats.rows)
        await rig.emit_sessions(immediate=True)
        await rig.settle()
        assert rig.passes() == 2
        await rig.emit_sessions(immediate=True)
        await rig.settle()
        assert rig.passes() == 2, "an unchanged signature does not reconcile again"
    run(body, SEAT, {**SEAT, "stream_id": "fixture:v2-other"})


@pytest.mark.timeout(30)
def test_direct_reconcile_requests_still_run_one_pass():
    """Lane operations, reports, close attendance, watcher, periodic and boot call refresh."""
    async def body(rig):
        for expected in (2, 3):
            rig.inv.refresh()
            await rig.settle()
            assert rig.passes() == expected
        await rig.inv.observe_for_close()
        await rig.settle()
        assert rig.passes() == 4
    run(body)


@pytest.mark.timeout(30)
def test_worked_time_change_re_emits_without_reconcile():
    async def body(rig):
        emits = rig.counts["emits"]
        rig.inv.worked.changed()
        await rig.settle()
        assert rig.passes() == 1 and rig.counts["emits"] == emits + 1
    run(body)


@pytest.mark.timeout(30)
def test_external_packet_and_mail_writes_request_a_lane_reconcile():
    async def check(rig):
        requests = []
        rig.runtime.lanes_changed = lambda: requests.append(1)
        await rig.seat()
        await packet_index(rig)
        assert (await packet(rig, state='queued'))['ok']
        assert (await packet(rig, 'set', state='working'))['ok']
        await mail(rig, 'fixture')
        assert len(requests) == 3
    exercise(check, packet_mode=True)


@pytest.mark.timeout(30)
@pytest.mark.parametrize("completed", [{"phase": "completed"}, None])
def test_lane_completion_through_routing_requests_a_lane_reconcile(completed):
    async def go():
        store = Store(":memory:"); store.start()
        try:
            composite = AssistantComposite(store, config=AssistantCompositeConfig(
                enabled=True, stream_id="fixture-host-chat:assistant",
                router_endpoint="ssh://fixture-router/assistant-router-v1"))
            requests = []

            async def lanes_changed():
                requests.append(1)

            async def found(dispatch_id):
                return {"dispatch_id": dispatch_id}

            async def current(route, source):
                return True

            async def complete(**kwargs):
                return completed

            async def noop():
                return None
            composite.work_lanes_changed = lanes_changed
            composite._is_current_dispatch_actor = current
            composite.refresh_activity = noop
            store.find_assistant_composite_route_by_dispatch = found
            store.complete_assistant_composite_lane = complete
            result = await composite.terminal_report({
                "status": "done", "actor_stream_id": "fixture-host-lead:lead", "report_id": "report-1",
                "lane_id": "lane-1", "dispatch_id": "dispatch-1"}, actor_generation="g1")
            assert result == completed
            assert len(requests) == (1 if completed else 0)
        finally:
            store.stop()
    asyncio.run(go())


GRANT = {"spec_id": "spec_fixture", "provenance": "operator_v2", "granting_principal": "operator",
         "granted_at": "2026-11-26T00:00:00Z"}


@pytest.mark.timeout(30)
def test_a_grant_time_alone_does_not_reconcile():
    async def body(rig):
        rig.seats.rows[0]["spec_binding_provenance"] = [{**GRANT, "granted_at": "2026-11-26T01:00:00Z"}]
        await rig.emit_sessions(immediate=True)
        await rig.settle()
        assert rig.passes() == 1
    run(body, {**SEAT, "spec_binding_provenance": [GRANT]})


async def _lead_lost(store):
    return await store.submit(lambda conn: conn.execute(
        "SELECT COUNT(*) FROM v2_work_lane_episodes WHERE lane_id=? AND kind='lead_lost'",
        (FIXTURE_LANE_ID,)).fetchone()[0])


@pytest.mark.timeout(30)
def test_bind_away_and_back_with_a_constant_roster_records_lead_lost():
    """The bind's own reconcile request opens the episode; seat emits cannot see it."""
    async def body(rig):
        await rig.store.submit(lambda conn: link_specs_conn(conn, "spec_fixture"))
        rig.inv.refresh()
        await rig.settle()
        bound = await rig.store.submit(lambda conn: tuple(conn.execute(
            "SELECT bound_stream_id,bound_generation FROM v2_assistant_composite_lanes WHERE lane_id=?",
            (FIXTURE_LANE_ID,)).fetchone()))
        assert await _lead_lost(rig.store) == 0

        def bind(stream, generation):
            def write(conn):
                conn.execute("UPDATE v2_assistant_composite_lanes SET bound_stream_id=?,bound_generation=?,"
                             "version=version+1 WHERE lane_id=?", (stream, generation, FIXTURE_LANE_ID))
                conn.commit()
            return rig.store.submit(write)
        await bind("lane-fixture:assistant-backend", "g-backend")
        await rig.emit_sessions(immediate=True)
        await rig.settle()
        assert await _lead_lost(rig.store) == 0, "the constant roster gives the emit no lane input"
        rig.inv.refresh()  # what lane.bind's work_lanes_changed requests
        await rig.settle()
        assert await _lead_lost(rig.store) == 1
        await bind(*bound)
        rig.inv.refresh()
        await rig.settle()
        assert await _lead_lost(rig.store) == 1
    run(body)


@pytest.mark.timeout(30)
def test_routing_lane_bind_and_direct_rebind_request_a_lane_reconcile():
    from test_assistant_composite import (AUTHORITY_STREAM, COMPOSITE_STREAM, LEAD_STREAM, ROUTER_ENDPOINT,
                                          _open_backend, _seed_dispatch)
    from test_assistant_hot_rebind import A, B, _config, _request, _seat

    async def go():
        store = Store(":memory:"); store.start()
        try:
            requests = []

            async def lanes_changed():
                requests.append(1)
            astra = await _open_backend(store, AUTHORITY_STREAM)
            lead = await _open_backend(store, LEAD_STREAM)
            composite = AssistantComposite(store, config=AssistantCompositeConfig(
                enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
                astra_stream_id=AUTHORITY_STREAM))
            composite.work_lanes_changed = lanes_changed
            await composite.ensure_projection()
            await _seed_dispatch(store, input_identity="msg-admit", dispatch_id="dispatch-admit",
                                 target=AUTHORITY_STREAM, generation=astra["session_generation"])
            admitted = await composite.operation({
                "operation": "lane.admit", "request_id": "admit", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-admit", "lane_id": None, "expected_lane_version": None,
                "evidence_refs": [], "payload": {"mode": "new", "subject": "test", "request_message_id": "msg-admit"},
            }, actor_stream_id=AUTHORITY_STREAM)
            assert requests == []
            await _seed_dispatch(store, input_identity="msg-bind", dispatch_id="dispatch-bind",
                                 target=AUTHORITY_STREAM, generation=astra["session_generation"],
                                 lane_id=admitted["lane_id"])
            await composite.operation({
                "operation": "lane.bind", "request_id": "bind", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-bind", "lane_id": admitted["lane_id"], "expected_lane_version": 1,
                "evidence_refs": [], "payload": {"backend_kind": "assistant_conversation",
                                                 "backend_stream_id": LEAD_STREAM,
                                                 "backend_generation": lead["session_generation"]},
            }, actor_stream_id=AUTHORITY_STREAM)
            assert requests == [1]
        finally:
            store.stop()

        store = Store(":memory:"); store.start()
        try:
            requests = []

            async def lanes_changed():
                requests.append(1)
            a, b = await _seat(store, A), await _seat(store, B)
            composite = AssistantComposite(store, config=_config(a["session_generation"]))
            composite.work_lanes_changed = lanes_changed
            await composite.load_binding()
            await composite.rebind(_request("move-a-b", 0, B), actor_stream_id=A)
            assert requests == [1]
            await composite.rebind(_request("move-a-b", 0, B), actor_stream_id=A)
            assert requests == [1], "a replayed rebind changes nothing"
        finally:
            store.stop()
    asyncio.run(go())


@pytest.mark.timeout(30)
@pytest.mark.parametrize("cols, requested", [({"pane_status": "pane_dead"}, 1), ({"pane_pid": "1"}, 0)])
def test_durable_pane_state_write_requests_a_lane_reconcile(cols, requested):
    from types import SimpleNamespace
    from mirror import Mirror

    async def go():
        requests = []

        async def update_session(host, name, **fields):
            return {}
        mirror = Mirror.__new__(Mirror)
        mirror.store = SimpleNamespace(update_session=update_session)
        mirror.sessions = SimpleNamespace(request_lane_reconcile=lambda: requests.append(1))
        await mirror._persist("fixture", "v2-seat", **cols)
        assert len(requests) == requested
    asyncio.run(go())


@pytest.mark.timeout(30)
def test_attendance_attach_requests_a_lane_reconcile_after_its_write():
    from types import SimpleNamespace
    from sessions import Sessions
    from uiverbs import UIVerbs

    async def go():
        store = Store(":memory:"); store.start()
        try:
            await store.open_session("fixture", "v2-seat", role="lead", pane_status="pane_alive")
            sessions = Sessions(store, local_host="fixture")
            await sessions.refresh()
            requests = []
            sessions.request_lane_reconcile = lambda: requests.append(1)

            async def checkpoint(*args):
                return None
            ui = UIVerbs(store=store, sessions=sessions, spawnctl=SimpleNamespace(work_checkpoint_warning=checkpoint),
                         specs=SimpleNamespace(resolution_for=lambda spec: "resolved",
                                               canonical_spec_identity=lambda spec: spec))
            reply = await ui.session_spec_update({"action": "attach", "host": "fixture", "session_name": "v2-seat",
                                                  "spec_id": "spec_fixture__attach",
                                                  "_auth_context": {"operator_authenticated": True}})
            assert reply["type"] == "session.spec_update.ok", reply
            assert requests == [1]
        finally:
            store.stop()
    asyncio.run(go())


@pytest.mark.timeout(30)
def test_periodic_reconcile_and_close_waiter_do_not_wait_on_endless_emit_work():
    """Emit work is always pending; the index and episode passes still run on their deadline."""
    from types import SimpleNamespace

    async def go():
        rig = Counted(Seats(SEAT))
        try:
            applies = []

            async def reconcile_work_observations(scan, **kwargs):
                applies.append(1)
            rig.store.reconcile_work_observations = reconcile_work_observations
            specs = SimpleNamespace(scan_work_observations=lambda: {}, start=lambda: None, stop=lambda: None,
                                    changed_callback=None)
            inv = WorkLanesInventory(rig.store, rig.seats, rig.inv.broadcast, specs=specs,
                                     sweep_interval_s=0.05, refresh_min_s=0)

            async def always_pending():
                rig.counts["emits"] += 1
                inv.request_emit()
                await asyncio.sleep(0.001)
            inv.emit_if_changed = always_pending
            inv.start(interval_s=0.05)
            # Generous stuck detector: several 0.05 s deadlines must pass while emits never stop.
            async with asyncio.timeout(10):
                while rig.counts["episodes"] < 4 or len(applies) < 3:
                    await asyncio.sleep(0.01)
            assert not inv._task.done(), "emit work stayed pending throughout"
            async with asyncio.timeout(10):
                await inv.refresh_for_close()
            await inv.stop()
        finally:
            rig.close()
    asyncio.run(go())
