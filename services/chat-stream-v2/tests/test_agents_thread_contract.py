"""D1 observable admission, roster, retained-pair and wire contracts."""
import asyncio
import json
from pathlib import Path

import pytest

from _shared.spawn_objective import objective_error
from agent_orch import cli
from inventory import InventoryEmitter
from server import Server
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from store_exchange import source
from tmux_transport import open_fields
from tests.test_spawn_error_no_phantom_row import BootReadyTmux


def wire_fixture():
    # Byte-identical synthetic public chat-core contract fixture.
    # Daemon-only CI checkouts do not initialize frontend submodules.
    import hashlib
    exported = Path(__file__).with_name("fixtures") / "daemon_updates_v1.json"
    payload = exported.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == "8b7f65de33e1904f2615f8770141f5c18293603e5d9b071ae62bc33c71f2765f"
    core = Path(__file__).resolve().parents[3] / "pentacle-chat-core/tests/fixtures/daemon_updates_v1.json"
    if core.exists():
        assert core.read_bytes() == payload
    return json.loads(payload)


@pytest.mark.parametrize("value,code", [(None, "objective_required"), (" \t", "objective_required"),
    ("one\ntwo", "objective_invalid"), ("x" * 121, "objective_invalid"),
    (23, "objective_invalid"), ("🌈" * 120, None), (" supplied exactly ", None)])
def test_objective_contract(value, code):
    assert objective_error(value) == code


def test_spawn_admission_before_effects_and_immutable_generation():
    async def run():
        store = Store(); store.start()
        tmux = BootReadyTmux()
        sessions = Sessions(store, tmux=tmux)
        ctl = SpawnCtl(store, sessions, tmux=tmux)
        try:
            for objective in (None, "", "a\nb", "x" * 121):
                with pytest.raises(VerbError) as exc:
                    await ctl.spawn({"command": "run", "objective": objective}, "localhost")
                assert exc.value.code == objective_error(objective)
                assert not tmux.alive and not await store.reservations()
                assert not await store.list_sessions()
            msg = {"command": "run", "session_name": "child", "request_id": "spawn-objective",
                   "objective": " Preserve this objective ", "parent_stream_id": "localhost:parent"}
            await sessions.open("localhost", "parent")
            response = await ctl.spawn(msg, "localhost")
            for task in list(ctl._background_spawns):
                await task
            assert response["type"] == "spawn.ok"
            row = await store.fetch_session("localhost", "child")
            assert row["objective"] == msg["objective"] and row["visibility"] == "hidden"
            with pytest.raises(ValueError, match="immutable"):
                await store.update_session("localhost", "child", objective="changed")
        finally:
            if tmux.alive:
                await tmux.kill_session("child")
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("working", [False, True])
def test_progress_transition_since_survives_cold_start_and_later_working_change(tmp_path, monkeypatch, working):
    from ledger import Ledger
    from routing_integrity import RoutingIntegrity
    clock = {"now": "2026-09-08T00:00:00Z"}
    for module in ("store", "ledger", "agents_roster"):
        monkeypatch.setattr(f"{module}.iso_now", lambda: clock["now"])

    async def run():
        path = str(tmp_path / "progress.db")
        store = Store(path); store.start()
        try:
            sessions = Sessions(store)
            await sessions.open("hosta", "parent")
            await sessions.open("hosta", "child", parent_stream_id="hosta:parent", provider="codex",
                                requested_model="gpt-5.6-luna", effective_model="gpt-5.6-luna",
                                requested_effort="max", effective_effort="max")
            sessions.apply_live("hosta:child", working=working)
            assert sessions.get("hosta:parent")["agents"][0]["state"] == ("working" if working else "idle")
            ledger = Ledger(store, sessions=sessions, routing_integrity=RoutingIntegrity(store, sessions))
            clock["now"] = "2026-09-08T00:01:00Z"
            await ledger.report(dict(report_id="blocked", from_stream_id="hosta:child", msg_id=1,
                                     status="error", reason="need_input", summary="Need input", findings=[], next_action="wait"))
            assert sessions.get("hosta:parent")["agents"][0]["state"] == "blocked"
            clock["now"] = "2026-09-08T00:02:00Z"
            await ledger.report(dict(report_id="resumed", from_stream_id="hosta:child", msg_id=1,
                                     status="progress", summary="Input received", findings=[], next_action="continue"))
            # Reading later must preserve the report transition's source time.
            clock["now"] = "2026-09-08T00:02:30Z"
            assert sessions.get("hosta:parent")["agents"][0]["since"] == "2026-09-08T00:02:00Z"
            store.stop(); store = Store(path); store.start()
            sessions = Sessions(store); await sessions.refresh()
            sessions.apply_live("hosta:child", working=working)
            agent = sessions.get("hosta:parent")["agents"][0]
            assert agent["state"] == ("working" if working else "idle")
            assert agent["since"] == "2026-09-08T00:02:00Z"
            # A later owner transition must not reuse the old progress timestamp.
            clock["now"] = "2026-09-08T00:03:00Z"
            sessions.apply_live("hosta:child", working=not working)
            assert sessions.get("hosta:parent")["agents"][0]["since"] == clock["now"]
            clock["now"] = "2026-09-08T00:04:00Z"
            assert sessions.get("hosta:parent")["agents"][0]["since"] == "2026-09-08T00:03:00Z"
        finally:
            store.stop()
    asyncio.run(run())


def test_retained_sources_restart_repair_and_report_state(tmp_path, monkeypatch):
    from comms import Comms
    from ledger import Ledger
    from routing_integrity import RoutingIntegrity
    from tests.test_tell_ledger_row_id import EchoTmux
    monkeypatch.setattr("store.TELL_RETENTION", 1)

    async def run():
        path = str(tmp_path / "source.db")
        store = Store(path); store.start()
        try:
            tmux = EchoTmux()
            sessions = Sessions(store, tmux=tmux, local_host="hosta")
            await sessions.open("hosta", "parent")
            await sessions.open("hosta", "child", parent_stream_id="hosta:parent", provider="codex",
                                requested_model="gpt-5.6-luna", effective_model="gpt-5.6-luna",
                                requested_effort="max", effective_effort="max")
            comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
            msg = dict(stream_id="hosta:child", from_stream_id="hosta:parent", message="Plain brief question", tell_id="tell-source")
            first = await comms.tell(msg)
            assert (await comms.tell(msg))["duplicate"]
            assert first["type"] == "tell.ok"
            for index in range(3):
                await store.put_tell_delivery(f"unrelated-{index}", {"reply": {"type": "tell.ok"}})
            assert (await comms.tell(msg))["duplicate"]  # current pair survives ordinary dedupe eviction
            ledger = Ledger(store, sessions=sessions, routing_integrity=RoutingIntegrity(store, sessions))
            report = dict(report_id="actual-report-id", from_stream_id="hosta:child", msg_id=999,
                          status="error", reason="input_missing", summary="BLOCKER: need an answer", findings=[{"severity": "major", "where": "", "issue": "The input is missing", "suggested_fix": None}], next_action="wait")
            await ledger.report(report)
            await ledger.report(report)
            assert sessions.get("hosta:parent")["agents"][0]["state"] == "blocked"
            request = dict(parent_stream_id="hosta:parent", child_stream_id="hosta:child", request_id="source-page")
            auth = {"operator_authenticated": True}
            page = await store.read_child_thread(request, auth)
            assert [r["row_id"] for r in page["rows"]] == ["tell:tell-source", "report:actual-report-id"]
            assert page["rows"][0]["text"] == "Plain brief question"
            assert page["rows"][1]["text"] == "BLOCKER: need an answer\nThe input is missing"
            await store.submit(lambda conn: (conn.execute("DELETE FROM v2_child_exchange"), conn.commit()))
            store.stop(); store = Store(path); store.start()
            repaired = await store.read_child_thread(request, auth)
            assert repaired["rows"] == page["rows"]
            sessions = Sessions(store); await sessions.refresh()
            assert sessions.get("hosta:parent")["agents"][0]["state"] == "blocked"
            await store.update_session("hosta", "child", status="closed")
            store.stop(); store = Store(path); store.start()
            assert (await store.read_child_thread(request, auth))["error_code"] == "pair_closed"
            assert await store.submit(lambda conn: conn.execute("SELECT COUNT(*) FROM v2_child_exchange").fetchone()[0]) == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_committed_core_fixture_through_daemon_summary_and_thread_handler():
    fixture = wire_fixture()
    for case in fixture["inventory_cases"]:
        actual = Server._summary_snapshot_sessions(case["frame"]["sessions"])
        assert [row.get("agents") for row in actual] == [row.get("agents") for row in case["frame"]["sessions"]]

    async def run(case):
        store = Store(); store.start()
        try:
            expected = fixture["thread_cases"][0]["response"]
            parent, child = expected["parent_stream_id"], expected["child_stream_id"]
            for sid, parent_sid, generation in ((parent, None, expected["parent_generation"]),
                (child, parent, expected["child_generation"]), ("host_b:v2-fixture-grandchild", child, "3" * 32)):
                host, name = sid.split(":", 1)
                await store.open_session(host, name, parent_stream_id=parent_sid, session_generation=generation)
            rows = fixture["thread_cases"][1]["response"]["rows"] + expected["rows"]
            binding = await store.exchange_binding(parent, child)
            for row in rows:
                await store.append_child_exchange(source({**binding, "direction": row["direction"]}, row["kind"], row["ref_id"], row["text"], row["ts"]))
            if case.get("given", {}).get("child_status") == "closed":
                host, name = child.split(":", 1)
                await store.update_session(host, name, status="closed")
            assert await store.read_child_thread(case["request"], case["auth"]) == case["response"]
        finally:
            store.stop()
    for case in fixture["thread_cases"] + fixture["error_cases"]:
        asyncio.run(run(case))


@pytest.mark.parametrize("transport", ["native_argv", "staged"])
def test_confirmed_brief_source_is_atomic_and_recoverable(transport, tmp_path):
    async def run():
        store = Store(str(tmp_path / "brief.db")); store.start()
        try:
            sessions = Sessions(store)
            await sessions.open("hosta", "parent")
            await store.reserve_stream_id("hosta", "child", request_id="brief-request", ttl_s=30)
            await store.record_spawn_intent("hosta", "child", {
                "open_fields": {"parent_stream_id": "hosta:parent", "session_generation": "child-generation", "objective": "Check brief"},
                "brief": "Human initial brief", "delivery_receipt": {"transport": transport},
            })
            await sessions.open("hosta", "child", parent_stream_id="hosta:parent", session_generation="child-generation", fence="brief-request")
            await store.set_spawn_outcome("hosta", "child", "delivered", request_id="brief-request",
                                          delivery_receipt={"state": "delivered", "transport": transport, "delivery_ack_at": "2026-09-08T00:00:00Z"})
            request = dict(parent_stream_id="hosta:parent", child_stream_id="hosta:child")
            first = await store.read_child_thread(request, {"operator_authenticated": True})
            assert [r["row_id"] for r in first["rows"]] == ["brief:brief-request"]
            await store.release_stream_id_fenced("hosta", "child", "brief-request")
            await store.set_spawn_outcome("hosta", "child", "delivered", request_id="brief-request", delivery_receipt={"state": "delivered"})
            assert (await store.read_child_thread(request, {"operator_authenticated": True}))["rows"] == first["rows"]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("role", [None, "worker", "nexus"])
@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("visibility", [None, "default", "hidden", "nested"])
@pytest.mark.parametrize("self_close", [False, True])
def test_visibility_and_lifecycle_are_independent(role, handoff, visibility, self_close):
    msg = dict(parent_stream_id="hosta:parent", role=role, handoff=handoff,
               visibility=visibility, self_close_on_completion=self_close, objective="Check overrides")
    fields = open_fields(msg)
    expected = visibility or ("default" if handoff or role == "nexus" else "hidden")
    assert fields["visibility"] == expected
    assert fields["self_close_on_completion"] is self_close


def test_cli_objective_rejects_before_rpc(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: pytest.fail("configuration/RPC before admission"))
    args = cli.build_parser().parse_args(["spawn", "--provider", "codex"])
    assert cli.spawn(args) == 2
    assert "objective_required" in capsys.readouterr().err
    assert cli.build_parser().parse_args(["thread", "hosta:child", "--limit", "2"]).limit == 2


async def seed(store):
    sessions = Sessions(store)
    for name, parent, generation in (("parent", None, "1" * 32), ("child", "hosta:parent", "2" * 32),
                                     ("grandchild", "hosta:child", "3" * 32), ("other", None, "4" * 32)):
        await sessions.open("hosta", name, parent_stream_id=parent, session_generation=generation,
                            objective="Check pair", visibility="hidden" if parent else "default")
    return sessions


def test_roster_all_states_hidden_close_reopen_reparent_and_dedupe():
    async def run():
        store = Store(); store.start()
        try:
            sessions = await seed(store)
            def roster():
                return sessions.get("hosta:parent")["agents"]
            assert [r["stream_id"] for r in roster()] == ["hosta:child"]
            assert roster()[0]["state"] == "idle"
            sessions.apply_live("hosta:child", working=True)
            assert roster()[0]["state"] == "working"
            for status, state in (("error", "blocked"), ("progress", "working"), ("done", "done"), ("progress", "working")):
                await sessions.apply_report_state(dict(from_stream_id="hosta:child", session_generation="2" * 32,
                                                       status=status, ingested_at="2026-09-08T00:00:00Z"))
                assert roster()[0]["state"] == state
            snapshot = sessions.list_open()
            summary = Server._summary_snapshot_sessions(snapshot)
            assert next(r for r in summary if r["stream_id"] == "hosta:parent")["agents"] == roster()
            signature = InventoryEmitter.signature_for_sessions(snapshot)
            next(r for r in snapshot if r["stream_id"] == "hosta:parent")["agents"][0]["since"] = "changed"
            assert InventoryEmitter.signature_for_sessions(snapshot) == signature
            await store.update_session("hosta", "child", parent_stream_id="hosta:other")
            await sessions.refresh()
            assert "agents" not in sessions.get("hosta:parent")
            assert sessions.get("hosta:other")["agents"][0]["stream_id"] == "hosta:child"
            await store.mark_closed("hosta", "child", closed_at="2026-09-08T01:00:00Z", pane_status="pane_dead")
            await sessions.refresh()
            assert "agents" not in sessions.get("hosta:other")
            await sessions.open("hosta", "child", parent_stream_id="hosta:parent")
            assert roster()[0]["state"] == "idle"
        finally:
            store.stop()
    asyncio.run(run())


def test_exchange_paging_conflict_unicode_auth_and_lifecycle(tmp_path):
    async def run():
        store = Store(str(tmp_path / "sessions.db")); store.start()
        try:
            await seed(store)
            binding = await store.exchange_binding("hosta:parent", "hosta:child")
            for index in range(20):
                observation = source(binding, "tell", str(index), "🌈" * 3000, "2026-09-08T00:00:00Z")
                assert await store.append_child_exchange(observation)
                assert await store.append_child_exchange(observation)
            with pytest.raises(ValueError, match="exchange_ref_conflict"):
                await store.append_child_exchange({**observation, "text": "different"})
            msg = dict(request_id="page", parent_stream_id="hosta:parent", child_stream_id="hosta:child", limit=50)
            auth = {"operator_authenticated": True}
            first = await store.read_child_thread(msg, auth)
            assert first["type"] == "thread.read.ok"
            assert len(json.dumps(first).encode()) <= 65536
            assert first["rows"][-1]["ref_id"] == "19"
            assert first["rows"][-1]["truncated"]
            assert len(first["rows"][-1]["text"].encode()) <= 8192
            assert first["next_cursor"]
            seen = [row["row_id"] for row in first["rows"]]
            await store.append_child_exchange(source(binding, "tell", "new", "new append", "2026-09-08T00:00:01Z"))
            page = first
            while page["next_cursor"]:
                page = await store.read_child_thread({**msg, "cursor": page["next_cursor"]}, auth)
                seen = [r["row_id"] for r in page["rows"]] + seen
            assert seen == [f"tell:{i}" for i in range(20)]
            for patch, context, error in (({}, {}, "unauthorized"), ({"parent_stream_id": None}, auth, "parent_required"),
                ({}, {"token_verified": True, "stream_id": "hosta:other"}, "not_direct_child"),
                ({"child_stream_id": "hosta:grandchild"}, auth, "not_direct_child"),
                ({"cursor": "bad"}, auth, "cursor_invalid"), ({"limit": True}, auth, "limit_invalid"),
                ({"limit": 51}, auth, "limit_invalid")):
                assert (await store.read_child_thread({**msg, **patch}, context))["error_code"] == error
            await store.update_session("hosta", "child", parent_stream_id="hosta:other")
            assert not await store.append_child_exchange(observation)
            assert (await store.read_child_thread(msg, auth))["error_code"] == "not_direct_child"
            await store.update_session("hosta", "child", parent_stream_id="hosta:parent")
            assert not await store.append_child_exchange(observation)
            assert (await store.read_child_thread({**msg, "cursor": first["next_cursor"]}, auth))["error_code"] == "cursor_invalid"
            assert (await store.read_child_thread(msg, auth))["rows"] == []
        finally:
            store.stop()
    asyncio.run(run())


def test_real_rpc_admission_and_verified_parent_from_committed_fixture():
    import hashlib
    import websockets
    from store import STREAM_TOKEN_HASH_VERSION

    async def run():
        store = Store(); store.start()
        daemon = None
        try:
            sessions = await seed(store)
            token = "D1-private-test-token"
            await store.update_session("hosta", "parent", token_hash=hashlib.sha256(token.encode()).hexdigest(),
                                       token_hash_version=STREAM_TOKEN_HASH_VERSION)
            ctl = SpawnCtl(store, sessions, tmux=BootReadyTmux())
            daemon = Server(port=0, store=store, sessions=sessions, spawnctl=ctl, local_host="hosta")
            await daemon.bind()
            port = daemon._ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # welcome
                async def rpc(payload):
                    await ws.send(json.dumps(payload))
                    while True:
                        frame = json.loads(await asyncio.wait_for(ws.recv(), 3))
                        if frame.get("request_id") == payload["request_id"]:
                            return frame
                fixture = wire_fixture()
                bad_spawn = next(case["request"] for case in fixture["spawn_cases"] if case["label"] == "missing_objective")
                result = await rpc({**bad_spawn, "objective_supported": True, "request_id": "rpc-objective"})
                assert result["type"] == "spawn.error" and result["error_code"] == "objective_required"
                assert not await store.reservations()
                request = dict(type="thread.read", request_id="rpc-thread", child_stream_id="hosta:child", stream_token=token)
                assert (await rpc(request))["type"] == "thread.read.ok"
                assert (await rpc({**request, "parent_stream_id": "hosta:other"}))["error_code"] == "not_direct_child"
                assert (await rpc({**request, "child_stream_id": "hosta:grandchild"}))["error_code"] == "not_direct_child"
                assert (await rpc({**request, "stream_token": "forged", "_auth_context": {"token_verified": True}}))["error_code"] == "unauthorized"
        finally:
            if daemon:
                await daemon.close()
            store.stop()
    asyncio.run(run())


def test_ten_thousand_retained_rows_have_complete_stable_pages():
    import store_exchange
    async def run():
        store = Store(); store.start()
        try:
            await seed(store)
            binding = await store.exchange_binding("hosta:parent", "hosta:child")
            def insert(conn):
                for index in range(10000):
                    store_exchange.append(conn, source(binding, "tell", str(index), "retained", "2026-09-08T00:00:00Z"))
                conn.commit()
            await store.submit(insert)
            request = dict(parent_stream_id="hosta:parent", child_stream_id="hosta:child", limit=50)
            seen = []
            while True:
                page = await store.read_child_thread(request, {"operator_authenticated": True})
                seen = [int(row["ref_id"]) for row in page["rows"]] + seen
                if not page["next_cursor"]:
                    break
                request["cursor"] = page["next_cursor"]
            assert seen == list(range(10000))
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["close", "reconciled_dead", "replace_parent", "replace_child"])
def test_every_authoritative_lifecycle_boundary_invalidates_exchange(boundary):
    async def run():
        store = Store(); store.start()
        try:
            await seed(store)
            binding = await store.exchange_binding("hosta:parent", "hosta:child")
            observation = source(binding, "tell", "lifecycle", "hello", "2026-09-08T00:00:00Z")
            await store.append_child_exchange(observation)
            if boundary == "close":
                await store.mark_closed("hosta", "parent", closed_at="2026-09-08T01:00:00Z", pane_status="pane_dead")
            elif boundary == "reconciled_dead":
                await store.mark_reconciled_dead("hosta", "child", expected_generation="2" * 32,
                                                 presumed_dead_at="2026-09-08T01:00:00Z", closed_at="2026-09-08T01:00:00Z")
            else:
                name = "parent" if boundary == "replace_parent" else "child"
                await store.open_session("hosta", name, session_generation="replacement")
            assert not await store.append_child_exchange(observation)
            assert await store.submit(lambda conn: conn.execute("SELECT COUNT(*) FROM v2_child_exchange").fetchone()[0]) == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_schedule_objective_admission_round_trip_and_legacy_retirement(tmp_path):
    from tests.test_window_schedule_contract import harness, schedule_insert, message
    store, _sessions, _comms, spawn, surface = harness(tmp_path)
    try:
        for value in (None, " ", "two\nlines", "x" * 121):
            with pytest.raises(VerbError) as exc:
                schedule_insert(surface, objective=value)
            assert exc.value.code == objective_error(value)
        assert asyncio.run(store.submit(lambda conn: conn.execute("SELECT COUNT(*) FROM v2_schedules").fetchone()[0])) == 0
        inserted = schedule_insert(surface, objective="Vérifier 東京 🌈")
        row = inserted["schedule"]
        assert row["objective"] == "Vérifier 東京 🌈"
        asyncio.run(surface._fire_schedule(row["schedule_id"]))
        assert spawn.calls[-1]["objective"] == "Vérifier 東京 🌈"
        legacy = schedule_insert(surface)["schedule"]["schedule_id"]
        asyncio.run(store.submit(lambda conn: (
            conn.execute("UPDATE v2_schedules SET objective=NULL WHERE schedule_id=?", (legacy,)), conn.commit())))
        calls = len(spawn.calls)
        asyncio.run(surface.recover())
        stored = asyncio.run(store.submit(lambda conn: dict(conn.execute("SELECT * FROM v2_schedules WHERE schedule_id=?", (legacy,)).fetchone())))
        assert stored["state"] == "failed" and stored["last_error_code"] == "objective_required"
        with pytest.raises(VerbError, match="objective_required"):
            asyncio.run(surface._fire_schedule(legacy))
        assert len(spawn.calls) == calls
    finally:
        store.stop()


@pytest.mark.parametrize("already_running", [False, True])
def test_legacy_intents_never_launch_or_replay_a_brief(already_running):
    from tests.test_spawn_intent_field_fidelity import _AlwaysAliveTmux

    async def run():
        store = Store(); store.start()
        tmux = _AlwaysAliveTmux(); tmux.nonce = "legacy-nonce"
        sessions = Sessions(store, tmux=tmux, local_host="hosta")
        ctl = SpawnCtl(store, sessions, tmux=tmux)
        try:
            await store.reserve_stream_id("hosta", "legacy", request_id="legacy-request", nonce=tmux.nonce, ttl_s=30)
            await store.record_spawn_intent("hosta", "legacy", {"open_fields": {}, "brief": "Must never be replayed"})
            await store.mark_tmux_created("hosta", "legacy")
            if already_running:
                await sessions.open("hosta", "legacy", fence="legacy-request")
            result = await ctl.reconcile_spawn_intents()
            row = await store.fetch_session("hosta", "legacy")
            outcome = await store.get_spawn_outcome("hosta", "legacy")
            assert outcome["reason"] == "objective_required"
            assert tmux.screen == ""
            if already_running:
                assert row["status"] == "open" and row["objective"] is None and tmux.alive
                assert result["adopted"] == 1
            else:
                assert row is None and not tmux.alive
                assert result["released"] == 1
        finally:
            if tmux.alive:
                await tmux.kill_session("legacy")
            store.stop()
    asyncio.run(run())
