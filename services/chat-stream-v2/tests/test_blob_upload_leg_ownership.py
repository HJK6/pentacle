"""Upload-leg contract: a partial upload is owned by its connection and torn
down when that connection closes.

The synthetic cases cover a socket drop or a never-arriving final chunk
must not leave a silent, unbounded partial upload; (b) a re-init on the same
request_id must not leak the prior upload's fd; (c) a chunk after the upload is
gone stays an explicit `upload_blob_unknown_request_id`.

These cases protect the public connection cleanup contract: without
`abort_connection`
and never forgot a partial upload except on completion or error-abort.
"""

from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path

import pytest

from blobs import BlobStore, Promptless


def _chunk(payload: bytes) -> str:
    return base64.b64encode(payload).decode()


async def _store(tmp: Path) -> BlobStore:
    store = BlobStore(str(tmp / "blobs"))
    await store.start()
    return store


def _init(store, rid, owner):
    return store._on_init(
        {"request_id": rid, "size_hint_bytes": 4096, "_client_websocket": owner},
        Promptless,
    )


def _chunk_msg(store, rid, payload, final):
    return store._on_chunk(
        {"request_id": rid, "data_b64": _chunk(payload), "final": final},
        Promptless,
    )


def test_connection_close_tears_down_orphaned_upload(tmp_path: Path) -> None:
    """(a)/(d): a dropped connection's partial upload is discarded — state gone,
    temp file unlinked — and a later chunk for the same rid gets the explicit
    `upload_blob_unknown_request_id`, not silence and not a stranger completion."""

    async def run() -> None:
        store = await _store(tmp_path)
        rid = "orphan-rid"
        conn = object()  # stand-in websocket
        init = await _init(store, rid, conn)
        assert init["type"] == "upload_blob.init.ok"
        assert await _chunk_msg(store, rid, b"partial-bytes", final=False) == []

        up = store._uploads[rid]
        assert Path(up.tmp_path).exists()  # partial upload is on disk
        assert store._by_conn.get(conn) == {rid}

        await store.abort_connection(conn)

        assert rid not in store._uploads
        assert rid not in store._locks
        assert conn not in store._by_conn
        assert not Path(up.tmp_path).exists()  # bounded lifetime: temp gone

        late = await _chunk_msg(store, rid, b"too-late", final=True)
        assert late["type"] == "upload_blob.error"
        assert late["error_code"] == "upload_blob_unknown_request_id"

    asyncio.run(run())


def test_reinit_same_request_id_discards_prior_upload(tmp_path: Path) -> None:
    """(b): re-init on a live request_id resets it — the prior upload is
    discarded (its fd closed + temp released, no leak) and ownership moves to the
    new connection; completion ok. Discard is asserted by object identity so
    fd-number reuse by the fresh open cannot mask a leak."""

    async def run() -> None:
        store = await _store(tmp_path)
        discarded: list = []
        real_discard = BlobStore._discard

        def spy(up):
            discarded.append(up)
            return real_discard(up)

        store._discard = spy  # instance attr shadows the staticmethod

        rid = "reinit-rid"
        conn1, conn2 = object(), object()
        await _init(store, rid, conn1)
        await _chunk_msg(store, rid, b"first-attempt", final=False)
        prior = store._uploads[rid]

        reinit = await _init(store, rid, conn2)  # same rid, new connection
        assert reinit["type"] == "upload_blob.init.ok"

        # The reset discarded the PRIOR upload object (fd closed, temp released).
        assert prior in discarded, "prior upload leaked on re-init"
        assert store._uploads[rid] is not prior  # a fresh upload replaced it

        assert store._by_conn.get(conn1) is None  # conn1 no longer owns anything
        assert store._by_conn.get(conn2) == {rid}

        done = await _chunk_msg(store, rid, b"second-attempt", final=True)
        assert done["type"] == "upload_blob.ok"
        assert conn2 not in store._by_conn  # ownership cleared on completion

    asyncio.run(run())


def test_late_chunk_after_completion_is_explicit_error(tmp_path: Path) -> None:
    """(c): once an upload completes the daemon forgets it; a further chunk is an
    explicit error, and completion leaves no dangling connection ownership."""

    async def run() -> None:
        store = await _store(tmp_path)
        rid = "complete-rid"
        conn = object()
        await _init(store, rid, conn)
        ok = await _chunk_msg(store, rid, b"whole-blob", final=True)
        assert ok["type"] == "upload_blob.ok"
        assert conn not in store._by_conn  # no ownership leak after completion

        late = await _chunk_msg(store, rid, b"after", final=False)
        assert late["type"] == "upload_blob.error"
        assert late["error_code"] == "upload_blob_unknown_request_id"

    asyncio.run(run())


def test_abort_connection_only_touches_that_connection(tmp_path: Path) -> None:
    """Tearing one connection down must not disturb another connection's upload."""

    async def run() -> None:
        store = await _store(tmp_path)
        conn1, conn2 = object(), object()
        await _init(store, "a-rid", conn1)
        await _chunk_msg(store, "a-rid", b"a", final=False)
        await _init(store, "b-rid", conn2)
        await _chunk_msg(store, "b-rid", b"b", final=False)

        await store.abort_connection(conn1)

        assert "a-rid" not in store._uploads
        assert "b-rid" in store._uploads  # untouched
        assert store._by_conn.get(conn2) == {"b-rid"}
        done = await _chunk_msg(store, "b-rid", b"bb", final=True)
        assert done["type"] == "upload_blob.ok"

    asyncio.run(run())


def test_abort_unknown_connection_is_a_noop(tmp_path: Path) -> None:
    async def run() -> None:
        store = await _store(tmp_path)
        await store.abort_connection(object())  # never owned anything

    asyncio.run(run())


def _assert_owner_map_consistent(store) -> None:
    """`_by_conn` must never name a request_id whose live upload is owned by a
    different connection or is gone — that dangling entry is the corruption a
    reset racing a chunk produced before the lock made it serial."""
    for owner, rids in store._by_conn.items():
        for rid in rids:
            up = store._uploads.get(rid)
            assert up is not None and up.owner is owner, (
                f"stale ownership: {owner!r} names {rid!r}"
            )


def test_reinit_reuses_lock_and_transfers_ownership(tmp_path: Path) -> None:
    """The per-upload lock is reused (not replaced) across a reset, and ownership
    moves cleanly from the prior connection to the new one."""

    async def run() -> None:
        store = await _store(tmp_path)
        rid = "reuse-rid"
        c1, c2 = object(), object()
        await _init(store, rid, c1)
        await _chunk_msg(store, rid, b"first", final=False)
        lock1 = store._locks[rid]

        await _init(store, rid, c2)  # reset
        assert store._locks[rid] is lock1, "lock replaced instead of reused"
        assert store._by_conn.get(c1) is None
        assert store._by_conn.get(c2) == {rid}
        _assert_owner_map_consistent(store)

        done = await _chunk_msg(store, rid, b"second", final=True)
        assert done["type"] == "upload_blob.ok"
        assert c2 not in store._by_conn

    asyncio.run(run())


def test_reinit_concurrent_with_inflight_chunk_keeps_owner_map_consistent(tmp_path: Path) -> None:
    """A re-init racing a prior attempt's in-flight final chunk must never leave
    dangling `_by_conn` ownership — the reset is serialized under the shared lock,
    so whichever wins, the owner map stays consistent with `_uploads`."""

    async def run() -> None:
        store = await _store(tmp_path)
        rid = "race-rid"
        c1, c2 = object(), object()
        await _init(store, rid, c1)
        t_chunk = asyncio.create_task(_chunk_msg(store, rid, b"attempt1", final=True))
        t_reset = asyncio.create_task(_init(store, rid, c2))
        await asyncio.gather(t_chunk, t_reset, return_exceptions=True)

        _assert_owner_map_consistent(store)
        # c1's attempt is either completed (owner dropped) or was discarded by the
        # reset; either way c1 must not retain phantom ownership.
        assert store._by_conn.get(c1) is None

    asyncio.run(run())


def test_cancelled_chunk_joins_inflight_write_before_unwinding(tmp_path: Path) -> None:
    """A chunk task cancelled while its `_append` executor worker is mid-write
    must JOIN that write before it unwinds and releases the per-upload lock — so
    teardown cannot close the fd out from under an in-flight write. Discriminator:
    with the join, `await task` cannot complete until the blocked write is
    released; without it, cancellation returns immediately (write still running)."""

    async def run() -> None:
        store = await _store(tmp_path)
        rid = "join-rid"
        c = object()
        await _init(store, rid, c)

        entered = threading.Event()
        gate = threading.Event()
        real_append = BlobStore._append

        def blocking_append(up, data):  # runs in the blob executor thread
            entered.set()
            gate.wait(5)
            real_append(up, data)

        store._append = blocking_append

        payload = b"joined-bytes"
        task = asyncio.create_task(_chunk_msg(store, rid, payload, final=False))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, entered.wait, 5)  # write is in flight

        task.cancel()
        await asyncio.sleep(0.05)
        # The join keeps the task alive until the (still-blocked) write completes.
        assert not task.done(), "cancel abandoned the in-flight write (no join)"

        gate.set()  # let the write finish
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store._uploads[rid].size == len(payload)  # write actually landed

    asyncio.run(run())


def test_abort_connection_serializes_with_inflight_chunk(tmp_path: Path) -> None:
    """Teardown that races an in-flight chunk takes the per-upload lock, so it
    never closes/unlinks an fd or temp file the chunk task is still using; the
    end state is clean (no leaked temp, no dangling ownership)."""

    async def run() -> None:
        store = await _store(tmp_path)
        rid = "teardown-rid"
        c = object()
        await _init(store, rid, c)
        t_chunk = asyncio.create_task(_chunk_msg(store, rid, b"bytes", final=False))
        t_abort = asyncio.create_task(store.abort_connection(c))
        await asyncio.gather(t_chunk, t_abort, return_exceptions=True)

        assert rid not in store._uploads
        assert rid not in store._locks
        assert c not in store._by_conn
        tmp = tmp_path / "blobs" / ".tmp"
        assert list(tmp.iterdir()) == []  # temp file cleaned, not leaked

    asyncio.run(run())
