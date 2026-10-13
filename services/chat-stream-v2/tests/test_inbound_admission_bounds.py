"""Admission limits allocation while preserving work already in progress."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

import pytest

from server import Server, WS_MAX_SIZE


class Socket:
    remote_address = ("127.0.0.1", 12345)

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = asyncio.Event()
        self.close_code = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def send(self, frame):
        self.sent.append(json.loads(frame))

    async def close(self, code=1000, reason=""):
        self.close_code = code
        self.close_reason = reason
        self.closed.set()
        self.incoming.put_nowait(None)

    async def wait_closed(self):
        await self.closed.wait()

    def request(self, verb, rid, **fields):
        self.incoming.put_nowait(json.dumps({"type": verb, "request_id": rid, **fields}, ensure_ascii=False))


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


def request_tasks():
    return [t for t in asyncio.all_tasks() if "Server._serve_after_hello" in t.get_coro().__qualname__]


@asynccontextmanager
async def clients(server, count, principals=None):
    sockets = [Socket() for _ in range(count)]
    tasks = [asyncio.create_task(server._handle_client(ws)) for ws in sockets]
    try:
        await eventually(lambda: all(ws.sent or ws.close_code for ws in sockets))
        if principals:
            # Established bindings, as produced by hello; real token/Store
            # validation is covered by the private socket rehearsal.
            for ws, principal in zip(sockets, principals):
                server._client_authenticated_streams[ws] = principal
        yield sockets
    finally:
        for ws in sockets:
            await ws.close()
        await asyncio.gather(*tasks)


def refusals(ws):
    return [f for f in ws.sent if f.get("error_code") == "server_overloaded"]


def test_global_allocation_limit_and_control_reserve(monkeypatch):
    monkeypatch.setenv("PENTACLE_ADMIT_ORDINARY", "4")
    monkeypatch.setenv("PENTACLE_ADMIT_PARKED", "2")

    async def run():
        daemon = Server()
        release = asyncio.Event()
        entered = []

        async def hold(msg):
            entered.append(msg["request_id"])
            await release.wait()
            return {"type": "hold.ok"}

        daemon.handlers["hold"] = hold
        daemon.handlers["await_report"] = hold
        async with clients(daemon, 3, ["seat:a", "seat:b", "seat:c"]) as (a, b, control):
            for i in range(12):
                a.request("hold", f"ordinary-{i}")
                b.request("await_report", f"parked-{i}")
            try:
                await eventually(lambda: len(refusals(a)) == 8 and len(refusals(b)) == 10)
                assert len(request_tasks()) == 6
                assert len(entered) == 6
                control.request("ping", "control")
                await eventually(lambda: any(f.get("type") == "pong" for f in control.sent))
                assert all(f["retryable"] is True for f in refusals(a) + refusals(b))
            finally:
                release.set()
            await eventually(lambda: not request_tasks())
            a.request("hold", "after-release")
            await eventually(lambda: any(f.get("request_id") == "after-release" for f in a.sent))
            assert not refusals(a)[-1].get("request_id") == "after-release"
    asyncio.run(run())


def test_shared_principal_cannot_multiply_ordinary_budget():
    async def run():
        daemon = Server()
        release = asyncio.Event()
        entered = []

        async def hold(msg):
            entered.append(msg["request_id"])
            await release.wait()
            return {"type": "hold.ok"}

        daemon.handlers["hold"] = hold
        async with clients(daemon, 4, ["seat:one"] * 3 + ["seat:two"]) as sockets:
            try:
                for index, ws in enumerate(sockets[:3]):
                    for i in range(8):
                        ws.request("hold", f"{index}-{i}", from_stream_id=f"forged:{index}-{i}")
                await eventually(lambda: sum(map(lambda ws: len(refusals(ws)), sockets)) == 8)
                assert len(entered) == 16
                sockets[3].request("hold", "independent")
                await eventually(lambda: "independent" in entered)
            finally:
                release.set()
    asyncio.run(run())


def test_detached_sends_keep_credit_until_terminal(monkeypatch):
    monkeypatch.setenv("PENTACLE_ADMIT_DURABLE", "2")

    async def run():
        daemon = Server()
        release = asyncio.Event()
        effects = []

        async def send(msg):
            await release.wait()
            effects.append(msg["request_id"])
            return {"type": "send.result", "delivery": "landed"}

        daemon.handlers["send"] = send
        try:
            async with clients(daemon, 1, ["seat:sender"]) as (ws,):
                for i in range(10):
                    ws.request("send", str(i))
                await eventually(lambda: len(refusals(ws)) == 8)
                assert len(daemon._detached_send_tasks) == 2
            assert len(daemon._detached_send_tasks) == 2
            async with clients(daemon, 1, ["seat:sender"]) as (retry,):
                retry.request("send", "while-pending")
                await eventually(lambda: len(refusals(retry)) == 1)
                release.set()
                await eventually(lambda: not daemon._detached_send_tasks)
                retry.request("send", "after-terminal")
                await eventually(lambda: "after-terminal" in effects)
            assert sorted(effects) == ["0", "1", "after-terminal"]
        finally:
            release.set()
            await asyncio.gather(*daemon._detached_send_tasks, return_exceptions=True)
    asyncio.run(run())


def test_connection_admission_precedes_writer_allocation(monkeypatch):
    monkeypatch.setenv("PENTACLE_ADMIT_CONNECTIONS", "2")

    async def run():
        daemon = Server()
        async with clients(daemon, 3) as sockets:
            assert len(daemon._client_writer_tasks) == 2
            assert len(daemon._client_send_queues) == 2
            assert sockets[2].close_code == 1013
            assert sockets[2].sent == []
        async with clients(daemon, 1) as (ws,):
            assert ws.sent[0]["type"] == "welcome"
    asyncio.run(run())


def test_byte_budget_counts_utf8_and_bounds_refusal_echo():
    async def run():
        daemon = Server()
        release = asyncio.Event()
        entered = []

        async def hold(msg):
            entered.append(msg["request_id"])
            await release.wait()
            return {"type": "hold.ok"}

        daemon.handlers["hold"] = hold
        async with clients(daemon, 1, ["seat:bytes"]) as (ws,):
            try:
                data = "\u00e9" * ((WS_MAX_SIZE - 1024) // 2)
                for i in range(3):
                    ws.request("hold", str(i), data=data)
                await eventually(lambda: len(refusals(ws)) == 1)
                assert len(entered) == 2
                assert refusals(ws)[0]["request_id"] == "2"
                # A rejected envelope must not echo an arbitrarily large ID.
                ws.request("hold", "x" * 100_000, data=data[:1_000_000])
                await eventually(lambda: len(refusals(ws)) == 2)
                assert len(json.dumps(refusals(ws)[1])) < 1024
            finally:
                release.set()
    asyncio.run(run())


def test_cancel_before_start_and_handler_error_return_credit(monkeypatch):
    monkeypatch.setenv("PENTACLE_ADMIT_ORDINARY", "1")

    async def run():
        daemon = Server()
        cancelled = []
        real_create_task = asyncio.create_task

        def cancel_request(coro, *args, **kwargs):
            task = real_create_task(coro, *args, **kwargs)
            if "Server._serve_after_hello" in coro.__qualname__:
                task.cancel()
                cancelled.append(task)
            return task

        async def broken(msg):
            raise RuntimeError("synthetic handler failure")

        daemon.handlers["broken"] = broken
        async with clients(daemon, 1) as (ws,):
            with monkeypatch.context() as patch:
                patch.setattr(asyncio, "create_task", cancel_request)
                ws.request("broken", "cancelled-before-start")
                await eventually(lambda: cancelled and cancelled[0].done())
            for rid in ("error-one", "error-two"):
                ws.request("broken", rid)
                await eventually(lambda: any(f.get("request_id") == rid for f in ws.sent))
                frame = next(f for f in ws.sent if f.get("request_id") == rid)
                assert frame["error_code"] == "internal_error"
            assert not refusals(ws)
    asyncio.run(run())


def test_hello_barrier_rechecks_established_principal_budget():
    async def run():
        daemon = Server()
        release = asyncio.Event()
        entered = []

        async def hold(msg):
            entered.append(msg["request_id"])
            await release.wait()
            return {"type": "hold.ok"}

        async def hello(msg):
            daemon._client_authenticated_streams[msg["_client_websocket"]] = "seat:shared"
            return {"type": "ready"}

        daemon.handlers["hold"] = hold
        daemon.handlers["hello"] = hello
        async with clients(daemon, 3, ["seat:shared", "seat:shared", "seat:unbound"]) as (a, b, newcomer):
            daemon._client_authenticated_streams.pop(newcomer)
            try:
                for ws in (a, b):
                    for i in range(8):
                        ws.request("hold", f"existing-{id(ws)}-{i}")
                await eventually(lambda: len(entered) == 16)
                newcomer.request("hello", "hello")
                newcomer.request("hold", "pipelined")
                await eventually(lambda: any(f.get("request_id") == "pipelined" for f in newcomer.sent))
                assert "pipelined" not in entered
                assert refusals(newcomer)[0]["request_id"] == "pipelined"
            finally:
                release.set()
    asyncio.run(run())


def test_protocol_two_refusal_preserves_existing_error_shape(monkeypatch):
    monkeypatch.setenv("PENTACLE_ADMIT_ORDINARY", "1")

    async def run():
        daemon = Server()
        release = asyncio.Event()
        entered = []

        async def hold(msg):
            entered.append(msg["request_id"])
            await release.wait()
            return {"type": "hold.ok"}

        daemon.handlers["hold"] = hold
        async with clients(daemon, 1) as (ws,):
            try:
                ws.request("hold", "occupy")
                await eventually(lambda: entered)
                ws.request("upload_blob_chunk", "voice", protocol=2)
                await eventually(lambda: any(f.get("request_id") == "voice" for f in ws.sent))
                assert next(f for f in ws.sent if f.get("request_id") == "voice") == {
                    "type": "upload_blob.error", "request_id": "voice", "protocol": 2,
                    "error_code": "upload_quota",
                }
            finally:
                release.set()
    asyncio.run(run())


def test_preauth_and_malformed_share_budget_without_claimed_identity():
    async def run():
        daemon = Server()
        release = asyncio.Event()
        entered = []

        async def hold(ws, raw):
            entered.append(raw)
            await release.wait()

        # Isolate allocation before dispatch: unauthenticated malformed work
        # cannot allocate beyond the shared preauth budget, even if it stalls.
        daemon._serve = hold
        async with clients(daemon, 4) as sockets:
            for ws in sockets:
                ws.remote_address = ("192.0.2.1", 12345)
            try:
                for index, ws in enumerate(sockets[:3]):
                    for i in range(8):
                        ws.incoming.put_nowait("{" if i % 2 else json.dumps({
                            "type": ["invalid"], "protocol": 2,
                            "from_stream_id": f"forged:{index}:{i}"}))
                await eventually(lambda: sum(len(refusals(ws)) for ws in sockets) == 8)
                assert len(entered) == 16
                # Unbound remote ping has no reserved-control authority.
                sockets[3].request("ping", "preauth-ping")
                await eventually(lambda: len(refusals(sockets[3])) == 1)
                assert len(entered) == 16
                assert all(frame["type"] == "protocol.error"
                           for ws in sockets[:3] for frame in refusals(ws))
            finally:
                release.set()
        await eventually(lambda: not request_tasks())
        assert all(pool.total == [0, 0] and not pool.connections and not pool.principals
                   for pool in daemon._admission_pools.values())
    asyncio.run(run())


def test_bootstrap_waiting_connections_are_bounded(monkeypatch):
    monkeypatch.setenv("PENTACLE_ADMIT_CONNECTIONS", "2")

    async def run():
        daemon = Server()
        daemon.bootstrap_ready.clear()
        sockets = [Socket() for _ in range(3)]
        tasks = [asyncio.create_task(daemon._handle_client(ws)) for ws in sockets]
        try:
            await eventually(lambda: sockets[2].close_code == 1013)
            assert not any(ws.sent for ws in sockets)
            assert len(daemon._client_writer_tasks) == 2
            assert len(daemon._connection_diagnostics) == 2
            await sockets[0].close()
            await tasks[0]
            replacement = Socket()
            sockets.append(replacement)
            tasks.append(asyncio.create_task(daemon._handle_client(replacement)))
            await eventually(lambda: replacement in daemon._client_writer_tasks)
            daemon.bootstrap_ready.set()
            await eventually(lambda: bool(replacement.sent))
        finally:
            for ws in sockets:
                await ws.close()
            await asyncio.gather(*tasks)
        assert daemon._admission_connections == 0
    asyncio.run(run())


def test_valid_cancel_has_reserved_admission_while_store_is_held(tmp_path, monkeypatch):
    import threading
    from store import Store
    from sessions import Sessions
    from spawnctl import SpawnCtl

    monkeypatch.setenv("PENTACLE_ADMIT_ORDINARY", "1")

    async def run():
        store = Store(str(tmp_path / "cancel.db"))
        store.start()
        release = threading.Event()
        entered = threading.Event()
        blocker = None
        try:
            sessions = Sessions(store, local_host="synthetic")
            spawnctl = SpawnCtl(store, sessions)
            daemon = Server(store=store, sessions=sessions, spawnctl=spawnctl,
                            local_host="synthetic")
            assert await store.reserve_stream_id("synthetic", "pending", ttl_s=60,
                                                 request_id="pending-request")

            def barrier(conn):
                entered.set()
                assert release.wait(5)

            blocker = asyncio.create_task(store.submit(barrier))
            await eventually(entered.is_set)
            async with clients(daemon, 2) as (ordinary, control):
                ordinary.request("list_sessions", "held-ordinary")
                await eventually(lambda: daemon._admission_pools["ordinary"].total[0] == 1)
                ordinary.request("list_sessions", "refused-ordinary")
                await eventually(lambda: len(refusals(ordinary)) == 1)
                control.request("spawn_cancel", "cancel", target="pending-request")
                # Valid target resolution is admitted and waits on Store; it
                # cannot complete until release and is not a latency claim.
                await eventually(lambda: daemon._admission_pools["control"].total[0] == 1
                                 and store._queue.qsize() >= 2)
                assert not any(f.get("request_id") == "cancel" for f in control.sent)
                release.set()
                await eventually(lambda: any(f.get("request_id") == "cancel" for f in control.sent))
                result = next(f for f in control.sent if f.get("request_id") == "cancel")
                assert result["type"] == "spawn_cancel.ok"
                assert result["state"] == "cancelled"
                assert (await store.spawn_cancellations("synthetic", "pending-request"))[0]["state"] == "cancelled"
        finally:
            release.set()
            if blocker:
                await blocker
            assert store.stop(timeout=5)
    asyncio.run(run())


def test_repeated_bursts_return_all_accounting_to_baseline(monkeypatch):
    monkeypatch.setenv("PENTACLE_ADMIT_ORDINARY", "4")

    async def run():
        daemon = Server()
        release = asyncio.Event()
        entered = []

        async def hold(msg):
            entered.append(msg["request_id"])
            await release.wait()
            return {"type": "hold.ok"}

        daemon.handlers["hold"] = hold
        for cycle in range(12):
            release.clear()
            entered.clear()
            async with clients(daemon, 1, ["seat:reconnect"]) as (ws,):
                for i in range(32):
                    ws.request("hold", f"{cycle}-{i}")
                await eventually(lambda: len(refusals(ws)) == 28)
                assert len(entered) == len(request_tasks()) == 4
                release.set()
                await eventually(lambda: not request_tasks())
            assert daemon._admission_connections == 0
            assert not daemon._client_writer_tasks
            assert all(pool.total == [0, 0] and not pool.connections and not pool.principals
                       for pool in daemon._admission_pools.values())
    asyncio.run(run())


@pytest.mark.parametrize("shared_full", [False, True])
def test_detached_known_credit_survives_binding_removal(shared_full):
    async def run():
        daemon = Server()
        ws = Socket()
        ws.remote_address = ("192.0.2.1", 12345)
        principal = ("seat", "seat:captured")
        pool = daemon._admission_pools["durable"]
        credit = pool.acquire(ws, principal, 12)
        assert credit is not None
        daemon._client_authenticated_streams[ws] = principal[1]
        barrier = asyncio.create_task(asyncio.sleep(0))
        await barrier
        other = []
        if shared_full:
            for _ in range(8):
                held = pool.acquire(Socket(), ("shared", "preauth"), 1)
                assert held is not None
                other.append(held)
        daemon._unregister_client(ws)
        seen = []

        async def serve(socket, raw):
            seen.append(raw)
            assert credit.principal == principal
            assert pool.principals[principal] == [1, 12]

        daemon._serve = serve
        try:
            await daemon._serve_after_hello(ws, '{"type":"send"}', barrier, credit)
            assert seen == ['{"type":"send"}']
            assert credit.principal == principal
        finally:
            credit.release()
            credit.release()  # terminal accounting is exactly once
            for held in other:
                held.release()
        assert pool.total == [0, 0] and not pool.principals and not pool.connections
    asyncio.run(run())


def test_refusal_bounds_utf8_correlation_and_known_verb_bytes():
    daemon = Server()
    frames = []
    daemon._enqueue = lambda ws, kind, raw: frames.append(json.loads(raw))
    long_verb = "synthetic_" + "v" * 100_000
    daemon.handlers[long_verb] = lambda msg: None
    daemon._refuse_request(Socket(), {"type": long_verb, "request_id": "😀" * 128})
    frame = frames[-1]
    assert len(frame["request_id"].encode("utf-8")) <= 128
    assert len(frame["type"].encode("utf-8")) <= 80
    assert frame["error_code"] == "server_overloaded"
    assert frame["retryable"] is True


def test_refusal_preserves_small_numeric_request_id():
    daemon = Server()
    frames = []
    daemon._enqueue = lambda ws, kind, raw: frames.append(json.loads(raw))
    daemon._refuse_request(Socket(), {"type": "ping", "request_id": 7})
    assert frames[-1]["type"] == "ping.error"
    assert frames[-1]["request_id"] == 7


def test_hello_transfer_moves_only_principal_task_and_byte_charge():
    async def run():
        daemon = Server()
        ws = Socket()
        pool = daemon._admission_pools["ordinary"]
        credit = pool.acquire(ws, ("shared", "preauth"), 123)
        assert credit is not None
        async def hello():
            daemon._client_authenticated_streams[ws] = "seat:hello"
        barrier = asyncio.create_task(hello())
        seen = []
        async def serve(socket, raw):
            seen.append(raw)
            assert pool.total == [1, 123]
            assert pool.connections[ws] == [1, 123]
            assert pool.principals == {("seat", "seat:hello"): [1, 123]}
        daemon._serve = serve
        try:
            await daemon._serve_after_hello(ws, "{}", barrier, credit)
            assert seen == ["{}"]
        finally:
            credit.release()
        assert pool.total == [0, 0] and not pool.connections and not pool.principals
    asyncio.run(run())


def test_hello_transfer_byte_refusal_precedes_handler():
    async def run():
        daemon = Server()
        ws, other = Socket(), Socket()
        pool = daemon._admission_pools["parked"]
        held = pool.acquire(other, ("seat", "seat:full"), 256 << 10)
        credit = pool.acquire(ws, ("shared", "preauth"), 1)
        assert held is not None and credit is not None
        async def hello():
            daemon._client_authenticated_streams[ws] = "seat:full"
        barrier = asyncio.create_task(hello())
        frames = []
        daemon._enqueue = lambda socket, kind, raw: frames.append(json.loads(raw))
        async def forbidden(socket, raw):
            raise AssertionError("refused request reached handler")
        daemon._serve = forbidden
        try:
            await daemon._serve_after_hello(ws, '{"type":"await_report","request_id":"full"}', barrier, credit)
            assert frames[-1]["request_id"] == "full"
            assert frames[-1]["error_code"] == "server_overloaded"
            assert credit.principal == ("shared", "preauth")
        finally:
            credit.release(); held.release()
        assert pool.total == [0, 0] and not pool.principals
    asyncio.run(run())


def test_direct_first_request_keeps_preauth_credit_after_binding():
    async def run():
        daemon = Server()
        ws = Socket()
        pool = daemon._admission_pools["durable"]
        credit = pool.acquire(ws, ("shared", "preauth"), 42)
        assert credit is not None
        async def serve(socket, raw):
            daemon._client_authenticated_streams[socket] = "seat:new"
            assert credit.principal == ("shared", "preauth")
            assert pool.principals == {("shared", "preauth"): [1, 42]}
        daemon._serve = serve
        try:
            await daemon._serve_after_hello(ws, '{"type":"send"}', None, credit)
            assert credit.principal == ("shared", "preauth")
        finally:
            credit.release()
    asyncio.run(run())


def test_detached_send_outlives_cancelled_hello_barrier():
    async def run():
        daemon = Server()
        hello_entered = asyncio.Event()
        release = asyncio.Event()
        effects = []
        async def hello(msg):
            hello_entered.set()
            await release.wait()
            return {"type": "ready"}
        async def send(msg):
            effects.append(msg["request_id"])
            return {"type": "send.result", "delivery": "landed"}
        daemon.handlers["hello"] = hello
        daemon.handlers["send"] = send
        async with clients(daemon, 1) as (ws,):
            ws.request("hello", "pending-hello")
            await hello_entered.wait()
            ws.request("send", "pipelined-send")
            await eventually(lambda: len(daemon._detached_send_tasks) == 1)
            detached = list(daemon._detached_send_tasks)
        await asyncio.gather(*detached, return_exceptions=True)
        assert not detached[0].cancelled()
        assert effects == ["pipelined-send"]
        await eventually(lambda: daemon._admission_pools["durable"].total == [0, 0])
    asyncio.run(run())


@pytest.mark.parametrize("proof", ["none", "bad_token", "wrong_seat", "closed_seat", "valid"])
def test_cancelled_hello_follower_still_requires_real_auth(tmp_path, proof):
    import hashlib
    from store import Store, STREAM_TOKEN_HASH_VERSION
    async def run():
        store = Store(str(tmp_path / "barrier-auth.db")); store.start()
        daemon = Server(store=store)
        ws = Socket(); ws.remote_address = ("192.0.2.1", 12345)
        token = "synthetic-barrier-proof"
        effects, replies = [], []
        dispatch = daemon._dispatch
        async def observe_dispatch(*args, **kwargs):
            result = await dispatch(*args, **kwargs)
            replies.extend(result)
            return result
        daemon._dispatch = observe_dispatch
        try:
            await store.open_session("synthetic", "seat")
            await store.grant_stream_token("synthetic", "seat", hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            if proof == "closed_seat":
                await store.mark_closed("synthetic", "seat", closed_at="2026-01-01T00:00:00Z", pane_status="missing")
            async def send(msg):
                assert msg["_auth_context"]["token_verified"] is True
                effects.append(msg["request_id"])
                return {"type": "send.ok"}
            daemon.handlers["send"] = send
            barrier = asyncio.create_task(asyncio.sleep(60)); barrier.cancel()
            await asyncio.gather(barrier, return_exceptions=True)
            request = {"type": "send", "request_id": "proof", "from_stream_id": "synthetic:seat"}
            if proof != "none": request["stream_token"] = "wrong" if proof == "bad_token" else token
            if proof == "wrong_seat": request["from_stream_id"] = "synthetic:other"
            raw = json.dumps(request)
            credit = daemon._admission_pools["durable"].acquire(ws, ("shared", "preauth"), len(raw))
            assert credit is not None
            try:
                await daemon._serve_after_hello(ws, raw, barrier, credit)
                if proof == "valid":
                    assert effects == ["proof"] and replies[-1]["type"] == "send.ok"
                else:
                    assert effects == []
                    assert replies[-1]["error_code"] == "authentication_required"
            finally: credit.release()
        finally:
            daemon._unregister_client(ws)
            assert store.stop(timeout=5)
    asyncio.run(run())


@pytest.mark.parametrize("durable,cancel_request", [(False, False), (True, True)])
def test_barrier_repair_preserves_ordinary_and_explicit_cancellation(durable, cancel_request):
    async def run():
        daemon = Server(); ws = Socket()
        barrier = asyncio.create_task(asyncio.sleep(60))
        pool = daemon._admission_pools["durable" if durable else "ordinary"]
        credit = pool.acquire(ws, ("shared", "local"), 1)
        assert credit is not None
        async def forbidden(socket, raw):
            raise AssertionError("cancelled request reached dispatch")
        daemon._serve = forbidden
        task = asyncio.create_task(daemon._serve_after_hello(ws, json.dumps({"type": "send" if durable else "ping"}), barrier, credit))
        task.add_done_callback(credit.release)
        await asyncio.sleep(0)
        barrier.cancel()
        if cancel_request: task.cancel()
        await asyncio.gather(task, barrier, return_exceptions=True)
        assert task.cancelled()
        await asyncio.sleep(0)
        assert pool.total == [0, 0] and not pool.connections and not pool.principals
    asyncio.run(run())
