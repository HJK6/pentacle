"""Bounded metadata for current admission holders, including saturated reads."""
from __future__ import annotations
import asyncio
import json
from types import SimpleNamespace

import pytest
import server as server_module
from server import Server
from test_inbound_admission_bounds import clients, eventually, refusals


def snapshot(daemon):
    return asyncio.run(daemon._on_daemon_stats({}))["stats"]["admission"]


def test_empty_snapshot_exposes_exact_limits_without_holders():
    daemon = Server()
    data = snapshot(daemon)
    assert data["schema"] == 1
    assert data["subsystem"] == "admission"
    assert data["bug_ref"] == "spec_pentacle__admission_observability_web_shared_credits_2026_10"
    assert data["connections"] == {"count": 0, "limit": 32}
    for name, pool in daemon._admission_pools.items():
        view = data["pools"][name]
        assert view["usage"] == {"tasks": 0, "bytes": 0}
        assert view["limits"] == {
            axis: {"tasks": pool.limits[i], "bytes": pool.limits[i + 3]}
            for i, axis in enumerate(("global", "connection", "principal"))
        }
        assert view["held_verbs"] == {} and view["connections"] == [] and view["principals"] == []


def test_holder_correlation_ages_transfer_detach_and_privacy(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(server_module, "_monotonic", lambda: now[0])
    daemon = Server()
    first, second = object(), object()
    daemon._clients.update((first, second))
    principal = ("credential", "CANARY_CREDENTIAL_VALUE")
    ordinary = daemon._admission_pools["ordinary"]
    parked = daemon._admission_pools["parked"]
    credits = [
        ordinary.acquire(first, principal, 37, verb="ping", connection_id="conn-a"),
        ordinary.acquire(second, principal, 19, verb="ping", connection_id="conn-b"),
        parked.acquire(first, principal, 23, verb="await_report", connection_id="conn-a"),
    ]
    try:
        now[0] += 2.5
        view = snapshot(daemon)
        a = view["pools"]["ordinary"]
        p = view["pools"]["parked"]
        assert a["usage"] == {"tasks": 2, "bytes": 56}
        assert a["held_verbs"] == {"ping": {"count": 2, "oldest_age_ms": 2500}}
        assert len(a["principals"]) == 1
        identity = a["principals"][0]["principal_id"]
        assert p["principals"][0]["principal_id"] == identity
        assert set(a["principals"][0]["connection_ids"]) == {"conn-a", "conn-b"}
        assert a["principals"][0]["kind"] == "credential"
        assert all(c["active"] for c in a["connections"])
        assert "CANARY_CREDENTIAL_VALUE" not in json.dumps(view)
        assert credits[0].transfer(("seat", "CANARY_SEAT_VALUE"))
        transferred = snapshot(daemon)["pools"]["ordinary"]
        assert len(transferred["principals"]) == 2
        assert all(r["tasks"] == 1 for r in transferred["principals"])
        assert "CANARY_SEAT_VALUE" not in json.dumps(transferred)
        daemon._clients.remove(first)
        detached = snapshot(daemon)["pools"]["parked"]["connections"]
        assert detached[0]["connection_id"] == "conn-a" and detached[0]["active"] is False
        assert snapshot(Server())["pools"]["ordinary"]["usage"]["tasks"] == 0
    finally:
        for credit in credits:
            credit.release()
            credit.release()  # Existing idempotence also owns metadata cleanup.
    for pool in snapshot(daemon)["pools"].values():
        assert pool["usage"] == {"tasks": 0, "bytes": 0}
        assert not pool["connections"] and not pool["principals"] and not pool["held_verbs"]


def test_stats_survives_global_ordinary_saturation_and_records_safe_holders(monkeypatch):
    monkeypatch.setenv("PENTACLE_ADMIT_ORDINARY", "4")
    async def run():
        daemon = Server()
        release = asyncio.Event()
        async def hold(msg):
            await release.wait()
            return {"type": "hold.ok"}
        daemon.handlers["hold"] = hold
        async with clients(daemon, 2) as (work, diag):
            try:
                for i in range(4):
                    work.request("hold", str(i), text="CANARY_REQUEST_TEXT", blob="CANARY_BASE64")
                await eventually(lambda: daemon._admission_pools["ordinary"].total[0] == 4)
                diag.request("daemon.stats", "stats")
                await eventually(lambda: any(f.get("request_id") == "stats" for f in diag.sent))
                reply = next(f for f in diag.sent if f.get("request_id") == "stats")
                assert reply["type"] == "daemon.stats.ok", reply
                data = reply["stats"]["admission"]
                ordinary = data["pools"]["ordinary"]
                assert ordinary["usage"]["tasks"] == 4
                assert ordinary["held_verbs"]["hold"]["count"] == 4
                assert len(ordinary["connections"]) == len(ordinary["principals"]) == 1
                assert data["pools"]["diag"]["usage"]["tasks"] == 1  # Self-counting.
                serialized = json.dumps(data)
                assert "CANARY_REQUEST_TEXT" not in serialized and "CANARY_BASE64" not in serialized
                diag.request("ping", "after-stats")
                await eventually(lambda: any(f.get("request_id") == "after-stats" for f in diag.sent))
                assert next(f for f in diag.sent if f.get("request_id") == "after-stats")["type"] == "pong"
            finally:
                release.set()
        assert all(pool.total == [0, 0] for pool in daemon._admission_pools.values())
    asyncio.run(run())


def test_diag_is_single_flight_and_never_contends_with_ping(monkeypatch):
    monkeypatch.setenv("PENTACLE_ADMIT_DIAG", "999")
    async def run():
        daemon = Server()
        entered, release = asyncio.Event(), asyncio.Event()
        # Counterpart dependency only: admission, dispatch and its snapshot are real.
        async def lifecycle_snapshot():
            entered.set()
            await release.wait()
            return {}
        daemon.lifecycle = SimpleNamespace(snapshot=lifecycle_snapshot)
        assert daemon._admission_pools["diag"].limits == (1, 1, 1, 16384, 16384, 16384)
        assert daemon._admission_pools["control"].limits == (4, 1, 1, 65536, 16384, 16384)
        async with clients(daemon, 2) as (a, b):
            try:
                a.request("daemon.stats", "first")
                await asyncio.wait_for(entered.wait(), 2)
                b.request("ping", "unaffected")
                await eventually(lambda: any(f.get("type") == "pong" for f in b.sent))
                b.request("daemon.stats", "second")
                await eventually(lambda: any(f.get("request_id") == "second" for f in b.sent))
                error = next(f for f in b.sent if f.get("request_id") == "second")
                assert error["error_code"] == "server_overloaded" and error["retryable"]
                b.request("unknown-no-diag", "unknown")
                await eventually(lambda: any(f.get("request_id") == "unknown" for f in b.sent))
                assert daemon._admission_pools["diag"].total[0] == 1
                assert len(daemon._admission_pools["diag"].credits) == 1
                assert next(iter(daemon._admission_pools["diag"].credits.values())).verb == "daemon.stats"
                release.set()
                await eventually(lambda: daemon._admission_pools["diag"].total == [0, 0])
                b.request("daemon.stats", "after-release")
                await eventually(lambda: any(f.get("request_id") == "after-release" for f in b.sent))
                assert next(f for f in b.sent if f.get("request_id") == "after-release")["type"] == "daemon.stats.ok"
            finally:
                release.set()
        assert all(not pool.credits for pool in daemon._admission_pools.values())
    asyncio.run(run())


def test_cancel_before_start_and_diag_disconnect_release_metadata():
    async def run():
        daemon = Server()
        pool = daemon._admission_pools["ordinary"]
        credit = pool.acquire(object(), ("shared", "local"), 12)
        task = asyncio.create_task(asyncio.sleep(60))
        task.add_done_callback(credit.release)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert pool.total == [0, 0] and not pool.credits
        entered = asyncio.Event()
        async def lifecycle_snapshot():
            entered.set()
            await asyncio.Event().wait()
        daemon.lifecycle = SimpleNamespace(snapshot=lifecycle_snapshot)
        async with clients(daemon, 1) as (ws,):
            ws.request("daemon.stats", "disconnect")
            await asyncio.wait_for(entered.wait(), 2)
            assert daemon._admission_pools["diag"].total[0] == 1
        assert all(p.total == [0, 0] and not p.credits for p in daemon._admission_pools.values())
    asyncio.run(run())


def test_unknown_wire_labels_and_payload_never_enter_metadata():
    async def run():
        daemon = Server()
        entered, release = asyncio.Event(), asyncio.Event()
        async def hello_counterpart(msg):
            entered.set()
            await release.wait()
            return {"type": "hello.ok"}
        daemon.handlers["hello"] = hello_counterpart
        async with clients(daemon, 1) as (ws,):
            try:
                ws.request("hello", "hello")
                await asyncio.wait_for(entered.wait(), 2)
                ws.request("CANARY_UNKNOWN_VERB", "CANARY_REQUEST_ID", text="CANARY_TEXT", blob="CANARY_BLOB")
                await eventually(lambda: daemon._admission_pools["ordinary"].total[0] == 2)
                data = daemon._admission_snapshot()
                verbs = data["pools"]["ordinary"]["held_verbs"]
                assert set(verbs) == {"hello", "unknown"}
                assert "CANARY" not in json.dumps(data)
            finally:
                release.set()
        assert all(not p.credits for p in daemon._admission_pools.values())
    asyncio.run(run())


def test_opaque_principal_is_boot_local_and_failed_transfer_keeps_charge():
    first, second = Server(), Server()
    principal = ("credential", "CANARY_KEY")
    a = first._admission_pools["ordinary"].acquire(object(), principal, 5)
    b = second._admission_pools["ordinary"].acquire(object(), principal, 5)
    held = []
    try:
        left = first._admission_snapshot()["pools"]["ordinary"]["principals"][0]["principal_id"]
        right = second._admission_snapshot()["pools"]["ordinary"]["principals"][0]["principal_id"]
        assert left != right
        pool = first._admission_pools["ordinary"]
        for i in range(16):
            held.append(pool.acquire(object(), ("seat", "full"), 1))
        assert all(held)
        assert not a.transfer(("seat", "full"))
        assert a.principal == principal and pool.principals[principal] == [1, 5]
        assert first._admission_snapshot()["pools"]["ordinary"]["usage"] == {"tasks": 17, "bytes": 21}
    finally:
        for c in [a, b, *held]:
            if c: c.release()
    assert not first._admission_snapshot()["pools"]["ordinary"]["principals"]


@pytest.mark.parametrize("restricted", ["remote-unauthenticated", "dot", "scoped"])
def test_existing_stats_authorization_is_not_widened(restricted):
    async def run():
        daemon = Server()
        from test_remote_rpc_admission import Peer
        peer = Peer("192.0.2.10" if restricted == "remote-unauthenticated" else "127.0.0.1")
        if restricted == "dot":
            daemon._client_dot_connections.add(peer)
        elif restricted == "scoped":
            daemon._client_scoped_connections.add(peer)
        reply = (await daemon._dispatch('{"type":"daemon.stats","request_id":"denied"}', websocket=peer))[0]
        assert reply["type"] == "daemon.stats.error"
        assert "stats" not in reply and "admission" not in reply
    asyncio.run(run())


def test_principal_full_across_sockets_is_visible_without_blocking_diag():
    async def run():
        daemon = Server()
        release = asyncio.Event()
        async def hold(msg):
            await release.wait()
            return {"type": "hold.ok"}
        daemon.handlers["hold"] = hold
        async with clients(daemon, 3, ["seat:shared"] * 3) as (a, b, diag):
            try:
                for i in range(8):
                    a.request("hold", f"a{i}")
                    b.request("hold", f"b{i}")
                await eventually(lambda: daemon._admission_pools["ordinary"].total[0] == 16)
                diag.request("hold", "principal-full")
                await eventually(lambda: len(refusals(diag)) == 1)
                diag.request("daemon.stats", "principal-stats")
                await eventually(lambda: any(f.get("request_id") == "principal-stats" for f in diag.sent))
                reply = next(f for f in diag.sent if f.get("request_id") == "principal-stats")
                assert reply["type"] == "daemon.stats.ok"
                pools = reply["stats"]["admission"]["pools"]
                ordinary = pools["ordinary"]
                principal = ordinary["principals"][0]
                assert len(ordinary["principals"]) == 1 and principal["tasks"] == 16
                assert len(ordinary["connections"]) == len(principal["connection_ids"]) == 2
                assert pools["diag"]["principals"][0]["principal_id"] == principal["principal_id"]
                assert "seat:shared" not in json.dumps(pools)
            finally:
                release.set()
        assert all(not p.credits for p in daemon._admission_pools.values())
    asyncio.run(run())


def test_detached_durable_holder_keeps_conn_id_after_real_disconnect():
    async def run():
        daemon = Server()
        release, entered = asyncio.Event(), asyncio.Event()
        async def send(msg):
            entered.set()
            await release.wait()
            return {"type": "send.result", "delivery": "landed"}
        daemon.handlers["send"] = send
        try:
            async with clients(daemon, 1, ["seat:sender"]) as (ws,):
                ws.request("send", "detached")
                await asyncio.wait_for(entered.wait(), 2)
                original = daemon._connection_diagnostics[ws].conn_id
                view = daemon._admission_snapshot()["pools"]["durable"]
                assert view["connections"][0]["connection_id"] == original
                assert view["connections"][0]["active"]
            assert ws not in daemon._connection_diagnostics
            view = daemon._admission_snapshot()["pools"]["durable"]
            assert view["usage"]["tasks"] == 1 and len(daemon._detached_send_tasks) == 1
            assert view["connections"][0]["connection_id"] == original
            assert view["connections"][0]["active"] is False
        finally:
            release.set()
            await asyncio.gather(*daemon._detached_send_tasks, return_exceptions=True)
            await asyncio.sleep(0)
        assert daemon._admission_snapshot()["pools"]["durable"]["connections"] == []
    asyncio.run(run())
