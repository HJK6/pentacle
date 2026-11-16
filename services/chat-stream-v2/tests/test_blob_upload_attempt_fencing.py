"""Same-request_id upload attempts are fenced to their owning attempt.

Three barrier-driven races on the real `BlobStore` handlers:
(1) a lock dropped at completion while a queued re-init still holds it lets a
    later same-rid init skip the in-flight chunk's critical section;
(2) a closing connection's teardown, snapshotted before another connection's
    re-init of the same rid, must not abort that newer attempt;
(3) cancelling init between `_open_tmp` and state install must not leak the
    fd, the temp file or a stale per-rid lock.
"""

from __future__ import annotations

import asyncio
import base64
import os
import threading
from pathlib import Path

import pytest

from blobs import BlobStore, Promptless


async def _store(tmp: Path) -> BlobStore:
    store = BlobStore(str(tmp / "blobs"))
    await store.start()
    return store


def _init(store, rid, owner):
    return store._on_init(
        {"request_id": rid, "size_hint_bytes": 4096, "_client_websocket": owner},
        Promptless,
    )


def _chunk(store, rid, payload, final):
    return store._on_chunk(
        {"request_id": rid, "data_b64": base64.b64encode(payload).decode(), "final": final},
        Promptless,
    )


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def _wait(event: threading.Event) -> None:
    assert await asyncio.get_running_loop().run_in_executor(None, event.wait, 5)


class _GatedAppend:
    """Block the Nth `_append` in the executor until its gate opens."""

    def __init__(self, count: int) -> None:
        self.entered = [threading.Event() for _ in range(count)]
        self.gates = [threading.Event() for _ in range(count)]
        self.calls = 0
        self._guard = threading.Lock()

    def __call__(self, up, data) -> None:
        with self._guard:
            index = self.calls
            self.calls += 1
        if index < len(self.gates):
            self.entered[index].set()
            self.gates[index].wait(5)
        BlobStore._append(up, data)


def test_reinit_behind_completing_chunk_keeps_one_lock_for_the_rid(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        rid, conn = "e1-rid", object()
        await _init(store, rid, conn)
        gated = _GatedAppend(2)
        store._append = gated

        final = asyncio.create_task(_chunk(store, rid, b"attempt-1", final=True))
        await _wait(gated.entered[0])
        reinit = asyncio.create_task(_init(store, rid, conn))
        await _settle()
        chunk = asyncio.create_task(_chunk(store, rid, b"attempt-2", final=False))
        await _settle()

        gated.gates[0].set()
        assert (await final)["type"] == "upload_blob.ok"
        assert (await reinit)["type"] == "upload_blob.init.ok"
        await _wait(gated.entered[1])  # attempt-2 chunk holds the rid's lock

        racing = asyncio.create_task(_init(store, rid, conn))
        # The chunk's gate stays shut until after this check, so a fenced init
        # cannot finish; an unfenced one discards/reopens within milliseconds.
        finished, _ = await asyncio.wait({racing}, timeout=1.0)
        assert not finished, "same-rid init bypassed the in-flight chunk's lock"

        gated.gates[1].set()
        await asyncio.gather(chunk, racing)
        store._discard(store._uploads.pop(rid))

    asyncio.run(run())


def test_reinit_after_completion_race_is_still_addressable(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        rid, conn = "e1b-rid", object()
        await _init(store, rid, conn)
        gated = _GatedAppend(1)
        store._append = gated

        final = asyncio.create_task(_chunk(store, rid, b"attempt-1", final=True))
        await _wait(gated.entered[0])
        reinit = asyncio.create_task(_init(store, rid, conn))
        await _settle()
        gated.gates[0].set()
        assert (await final)["type"] == "upload_blob.ok"
        assert (await reinit)["type"] == "upload_blob.init.ok"

        done = await _chunk(store, rid, b"attempt-2", final=True)
        assert done["type"] == "upload_blob.ok", done
        assert rid not in store._uploads and rid not in store._locks
        assert list((tmp_path / "blobs" / ".tmp").iterdir()) == []

    asyncio.run(run())


def test_closing_connection_teardown_spares_other_connections_reinit(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        rid, old, new = "e2-rid", object(), object()
        await _init(store, rid, old)
        gated = _GatedAppend(1)
        store._append = gated

        inflight = asyncio.create_task(_chunk(store, rid, b"old-bytes", final=False))
        await _wait(gated.entered[0])
        reinit = asyncio.create_task(_init(store, rid, new))
        await _settle()
        teardown = asyncio.create_task(store.abort_connection(old))
        await _settle()

        gated.gates[0].set()
        assert await inflight == []
        assert (await reinit)["type"] == "upload_blob.init.ok"
        await teardown

        current = store._uploads.get(rid)
        assert current is not None and current.owner is new, "teardown aborted another connection's attempt"
        assert store._by_conn.get(new) == {rid} and old not in store._by_conn
        done = await _chunk(store, rid, b"new-bytes", final=True)
        assert done["type"] == "upload_blob.ok", done

    asyncio.run(run())


def test_cancelled_init_between_open_and_install_leaks_nothing(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        rid = "e3-rid"
        entered, gate, opened = threading.Event(), threading.Event(), threading.Event()
        fds: list[int] = []
        real_open = store._open_tmp

        def blocking_open(name):
            entered.set()
            gate.wait(5)
            fd, path = real_open(name)
            fds.append(fd)
            opened.set()
            return fd, path

        store._open_tmp = blocking_open
        init = asyncio.create_task(_init(store, rid, object()))
        await _wait(entered)
        init.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await init
        await _wait(opened)
        await _settle()

        try:
            assert rid not in store._uploads
            assert rid not in store._locks, "stale per-rid lock after cancelled init"
            assert list((tmp_path / "blobs" / ".tmp").iterdir()) == [], "temp file leaked"
            with pytest.raises(OSError):
                os.fstat(fds[0])
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    asyncio.run(run())


def test_stale_owner_chunk_cannot_touch_successor_attempt(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        rid, old, new = "e4-rid", object(), object()
        await _init(store, rid, old)
        gated = _GatedAppend(1)
        store._append = gated

        def chunk_from(owner, payload, final):
            return store._on_chunk(
                {"request_id": rid, "data_b64": base64.b64encode(payload).decode(),
                 "final": final, "_client_websocket": owner},
                Promptless,
            )

        inflight = asyncio.create_task(chunk_from(old, b"old-1", False))
        await _wait(gated.entered[0])  # old owner's chunk holds the rid's lock
        reinit = asyncio.create_task(_init(store, rid, new))
        await _settle()
        stale = asyncio.create_task(chunk_from(old, b"stale-old-bytes", True))
        await _settle()

        gated.gates[0].set()
        assert await inflight == []
        assert (await reinit)["type"] == "upload_blob.init.ok"
        reply = await stale
        current = store._uploads.get(rid)
        assert current is not None and current.owner is new and current.size == 0, "stale owner chunk touched the successor attempt"
        assert reply.get("error_code") == "upload_blob_unknown_request_id", reply
        done = await chunk_from(new, b"new-bytes", True)
        assert done["type"] == "upload_blob.ok" and done["size_bytes"] == len(b"new-bytes"), done

    asyncio.run(run())



def test_shutdown_cancelled_init_joins_open_worker(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        entered, gate, opened = threading.Event(), threading.Event(), threading.Event()
        fds: list[int] = []
        real_open = store._open_tmp

        def blocking_open(name):
            entered.set()
            assert gate.wait(5)
            fd, path = real_open(name)
            fds.append(fd)
            opened.set()
            return fd, path

        store._open_tmp = blocking_open
        before = asyncio.all_tasks()
        init = asyncio.create_task(_init(store, "shutdown-rid", object()))
        owned = {init}
        try:
            await _wait(entered)
            owned = asyncio.all_tasks() - before
            for task in owned:  # shutdown cancels the owner and any opening Task
                task.cancel()
            await _settle()
            held = not init.done() and store._lock_users.get("shutdown-rid") == 1
            gate.set()
            await asyncio.gather(*owned, return_exceptions=True)
            await _wait(opened)
            try:
                os.fstat(fds[0])
                fd_closed = False
            except OSError:
                fd_closed = True
            assert fd_closed and not list(store._tmp.iterdir()), "shutdown cancellation leaked the opened fd/temp"
            assert held and init.cancelled(), "shutdown released ownership before the open worker settled"
            assert not (store._uploads or store._locks or store._lock_users or store._by_conn)
        finally:
            gate.set()
            await asyncio.gather(*owned, return_exceptions=True)
            await _wait(opened)
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            for path in store._tmp.iterdir():
                path.unlink()

    asyncio.run(run())


async def _cancelled_discard_spares_successor(tmp_path: Path, *, abort: bool) -> None:
    store = await _store(tmp_path)
    rid, old, middle, new = "discard-rid", object(), object(), object()
    await _init(store, rid, old)
    prior, lock = store._uploads[rid], store._locks[rid]
    entered, gate, finished = threading.Event(), threading.Event(), threading.Event()
    real_discard = store._discard

    def blocking_discard(up):
        if up is prior:
            entered.set()
            assert gate.wait(5)
        try:
            real_discard(up)
        finally:
            if up is prior:
                finished.set()

    store._discard = blocking_discard
    operation = asyncio.create_task(store.abort_connection(old) if abort else _init(store, rid, middle))
    successor = None
    try:
        await _wait(entered)
        operation.cancel()
        await _settle()
        held = not operation.done() and store._locks.get(rid) is lock
        successor = asyncio.create_task(_init(store, rid, new))
        await _settle()
        same_lock = store._locks.get(rid) is lock
        if operation.done():  # RED: install the successor before the old unlink
            assert (await successor)["type"] == "upload_blob.init.ok"
        gate.set()
        await asyncio.gather(operation, return_exceptions=True)
        assert (await successor)["type"] == "upload_blob.init.ok"
        await _wait(finished)
        current = store._uploads.get(rid)
        assert current is not None and Path(current.tmp_path).exists(), "cancelled discard unlinked the successor temp"
        assert held and same_lock and operation.cancelled(), "cancelled discard released the rid lock before completion"
        assert current.owner is new and store._by_conn.get(new) == {rid}
        done = await store._on_chunk(
            {"request_id": rid, "data_b64": base64.b64encode(b"successor").decode(),
             "final": True, "_client_websocket": new}, Promptless,
        )
        assert done["type"] == "upload_blob.ok" and done["size_bytes"] == len(b"successor"), done
    finally:
        gate.set()
        await asyncio.gather(operation, return_exceptions=True)
        if successor is not None:
            await asyncio.gather(successor, return_exceptions=True)
        await _wait(finished)
        await store.abort_connection(new)


def test_cancelled_reset_discard_spares_successor(tmp_path: Path) -> None:
    asyncio.run(_cancelled_discard_spares_successor(tmp_path, abort=False))


def test_cancelled_abort_discard_spares_successor(tmp_path: Path) -> None:
    asyncio.run(_cancelled_discard_spares_successor(tmp_path, abort=True))
