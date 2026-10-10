"""Real v2 stores/ingress with faults at actual filesystem durability boundaries."""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import threading

import pytest

from test_voice_upload_resume import (
    voice_harness, metadata, init, send_chunk, chunk, path_for, checkpoint,
)


@pytest.mark.asyncio
async def test_restart_truncates_fsynced_uncheckpointed_tail(voice_harness):
    h = voice_harness
    m = metadata(h, b"abcdefgh")
    async with h.client() as c:
        await init(c, m)
        await send_chunk(c, m, b"abcd")
    part = path_for(h, m) / "audio.part"
    with part.open("ab") as out:
        out.write(b"uncheckpointed-tail")
        out.flush()
        os.fsync(out.fileno())
    # An incomplete temporary checkpoint is never promoted on recovery.
    (path_for(h, m) / "checkpoint.json.tmp").write_text('{"next_offset":999')
    await h.restart()
    assert part.read_bytes() == b"abcd"
    async with h.client() as c:
        assert (await init(c, m))["next_offset"] == 4
        assert (await send_chunk(c, m, b"efgh", 4, True))["complete"]
        assert await h.blobs.read_verified(m["sha256"]) == b"abcdefgh"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["shorter", "corrupt", "checkpoint", "missing"])
async def test_restart_quarantines_corrupt_or_short_acknowledged_data(voice_harness, damage):
    h = voice_harness
    m = metadata(h, b"abcdefgh")
    async with h.client() as c:
        await init(c, m)
        await send_chunk(c, m, b"abcd")
    path = path_for(h, m)
    if damage == "checkpoint":
        (path / "checkpoint.json").write_text("not json")
    elif damage == "missing":
        (path / "audio.part").unlink()
    else:
        (path / "audio.part").write_bytes(b"a" if damage == "shorter" else b"wxyz")
    await h.restart()
    async with h.client() as c:
        result = await init(c, m)
        assert result["error_code"] == "upload_conflict"
        assert "next_offset" not in result and "blob_sha" not in result
        assert (path / "quarantine.json").exists()
        assert not h.blobs._path_for(m["sha256"]).exists()


@pytest.mark.asyncio
async def test_checkpoint_failure_does_not_ack_and_retry_uses_last_committed_prefix(voice_harness, monkeypatch):
    h = voice_harness
    m = metadata(h, b"abcdefgh")
    async with h.client() as c:
        await init(c, m)
        real = h.blobs.voice_uploads._save
        def full(path, state):
            if state["next_offset"]:
                raise OSError(errno.ENOSPC, "synthetic private disk details must not escape")
            return real(path, state)
        monkeypatch.setattr(h.blobs.voice_uploads, "_save", full)
        failed = await send_chunk(c, m, b"abcd")
        assert failed["error_code"] == "upload_blob_disk_full"
        assert set(failed) == {"type", "request_id", "protocol", "error_code"}
        assert checkpoint(h, m)["next_offset"] == 0
        assert (path_for(h, m) / "audio.part").read_bytes() == b"abcd"
        monkeypatch.setattr(h.blobs.voice_uploads, "_save", real)
        assert (await init(c, m))["next_offset"] == 0
        assert (path_for(h, m) / "audio.part").read_bytes() == b""
        assert (await send_chunk(c, m, b"abcdefgh", final=True))["complete"]


@pytest.mark.asyncio
async def test_initial_checkpoint_failure_is_retryable_without_false_ack(voice_harness, monkeypatch):
    h = voice_harness
    m = metadata(h)
    real = h.blobs.voice_uploads._save
    def full(*args):
        raise OSError(errno.ENOSPC, "synthetic")
    monkeypatch.setattr(h.blobs.voice_uploads, "_save", full)
    async with h.client() as c:
        assert (await init(c, m))["error_code"] == "upload_blob_disk_full"
        assert not (path_for(h, m) / "checkpoint.json").exists()
        monkeypatch.setattr(h.blobs.voice_uploads, "_save", real)
        assert (await init(c, m))["next_offset"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["fsync", "checkpoint"])
async def test_real_disconnect_joins_write_and_checkpoint_before_second_socket_resumes(voice_harness, monkeypatch, boundary):
    h = voice_harness
    m = metadata(h, b"abcdefgh")
    entered, release = threading.Event(), threading.Event()
    first = await h.client().__aenter__()
    try:
        await init(first, m)
        part = path_for(h, m) / "audio.part"
        if boundary == "checkpoint":
            original = h.blobs.voice_uploads._save
            def blocked(path, state):
                if state["next_offset"] == 4 and not entered.is_set():
                    entered.set()
                    assert release.wait(5), "test did not release owned checkpoint"
                return original(path, state)
            monkeypatch.setattr(h.blobs.voice_uploads, "_save", blocked)
        else:
            original = os.fsync
            inode = part.stat().st_ino
            def blocked(fd):
                if os.fstat(fd).st_ino == inode and not entered.is_set():
                    entered.set()
                    assert release.wait(5), "test did not release owned fsync"
                return original(fd)
            monkeypatch.setattr(os, "fsync", blocked)
        await first._sock.send(json.dumps({"type": "upload_blob_chunk", "request_id": "lost-ack", **chunk(m, b"abcd")}))
        assert await asyncio.to_thread(entered.wait, 5)
        await first._sock.close()  # server cancels the real in-flight handler
        async with h.client() as second:
            waiting = asyncio.create_task(init(second, m))
            try:
                await asyncio.sleep(0.03)
                assert not waiting.done(), "reconnect raced the still-running cancelled disk writer"
                assert checkpoint(h, m)["next_offset"] == 0
                release.set()
                resumed = await waiting
                assert resumed["next_offset"] == 4
                assert (path_for(h, m) / "audio.part").read_bytes() == b"abcd"
                assert (await send_chunk(second, m, b"efgh", 4, True))["complete"]
            finally:
                release.set()
                await asyncio.gather(waiting, return_exceptions=True)
    finally:
        release.set()
        await first.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_canonical", [False, True])
async def test_crash_after_canonical_move_before_receipt_rehashes_recovery(voice_harness, monkeypatch, corrupt_canonical):
    h = voice_harness
    data = b"durable canonical synthetic audio"
    m = metadata(h, data)
    async with h.client() as c:
        await init(c, m)
        real = h.blobs._materialize_verified
        def moved_then_crashed(*args):
            result = real(*args)
            raise OSError(errno.EIO, "synthetic crash after move")
        monkeypatch.setattr(h.blobs, "_materialize_verified", moved_then_crashed)
        assert (await send_chunk(c, m, data, final=True))["error_code"] == "upload_blob_disk_full"
        assert checkpoint(h, m)["next_offset"] == len(data)
        assert checkpoint(h, m)["final_requested"] and not checkpoint(h, m)["complete"]
        assert not (path_for(h, m) / "audio.part").exists()
        canonical = h.blobs._path_for(m["sha256"])
        assert canonical.read_bytes() == data
        if corrupt_canonical:
            canonical.write_bytes(b"x" * len(data))
    await h.restart()
    async with h.client() as c:
        response = await init(c, m)
        if corrupt_canonical:
            assert response["error_code"] == "upload_conflict"
            assert "blob_sha" not in response
        else:
            assert response["complete"] is True and response["blob_sha"] == m["sha256"]
            assert await h.blobs.read_verified(m["sha256"]) == data


@pytest.mark.asyncio
async def test_complete_checkpoint_failure_recovers_same_blob_and_operation(voice_harness, monkeypatch):
    h = voice_harness
    data = b"one final audio"
    m = metadata(h, data)
    async with h.client() as c:
        await init(c, m)
        real = h.blobs.voice_uploads._save
        def fail_receipt(path, state):
            if state["complete"]:
                raise OSError(errno.ENOSPC, "synthetic final receipt failure")
            return real(path, state)
        monkeypatch.setattr(h.blobs.voice_uploads, "_save", fail_receipt)
        assert (await send_chunk(c, m, data, final=True))["error_code"] == "upload_blob_disk_full"
        assert not checkpoint(h, m)["complete"]
        op = await h.store.voice_get("operator:" + h.credential_id, m["voice_operation"]["operation_id"])
        prior_milestone = op["data"]["upload_committed"]
    await h.restart()
    async with h.client() as c:
        assert (await init(c, m))["blob_sha"] == m["sha256"]
        assert (await send_chunk(c, m, data, final=True))["blob_sha"] == m["sha256"]
        op = await h.store.voice_get("operator:" + h.credential_id, m["voice_operation"]["operation_id"])
        assert op["data"]["upload_committed"] == prior_milestone
        assert len(list(h.blobs._root.glob("[0-9a-f][0-9a-f]/*"))) == 1


@pytest.mark.asyncio
async def test_final_ack_lost_on_wire_restart_returns_identical_receipt(voice_harness, monkeypatch):
    h = voice_harness
    data = b"final ack deliberately unread"
    m = metadata(h, data)
    committed = threading.Event()
    real = h.blobs.voice_uploads._save
    def observe(path, state):
        result = real(path, state)
        if state["complete"]:
            committed.set()
        return result
    monkeypatch.setattr(h.blobs.voice_uploads, "_save", observe)
    async with h.client() as c:
        await init(c, m)
        await c._sock.send(json.dumps({"type": "upload_blob_chunk", "request_id": "unread-final", **chunk(m, data, final=True)}))
        assert await asyncio.to_thread(committed.wait, 5)
        # Close without consuming the RPC response, then destroy all process state.
    await h.restart()
    async with h.client() as c:
        result = await init(c, m)
        assert result["complete"] and result["blob_sha"] == m["sha256"]
        assert result["next_offset"] == len(data)


@pytest.mark.asyncio
async def test_simultaneous_admissions_do_not_overreserve(voice_harness):
    h = voice_harness
    clients = [await h.client().__aenter__() for _ in range(11)]
    try:
        replies = await asyncio.gather(*(init(c, metadata(h)) for c in clients))
        assert sum(r.get("next_offset") == 0 for r in replies) == 10
        assert sum(r.get("error_code") == "upload_quota" for r in replies) == 1
        assert len(list(h.blobs.voice_uploads.root.rglob("checkpoint.json"))) == 10
    finally:
        await asyncio.gather(*(c.__aexit__(None, None, None) for c in clients))


@pytest.mark.asyncio
async def test_hash_materialization_and_checkpoints_are_off_loop_with_fsync_before_ack(voice_harness, monkeypatch):
    h = voice_harness
    m = metadata(h, b"off-loop durability evidence")
    main_thread = threading.get_ident()
    calls = []
    real_fsync = os.fsync
    def fsync(fd):
        assert threading.get_ident() != main_thread
        calls.append("fsync")
        return real_fsync(fd)
    real_save = h.blobs.voice_uploads._save
    def save(path, state):
        assert threading.get_ident() != main_thread
        assert "fsync" in calls
        result = real_save(path, state)
        calls.append("complete_checkpoint" if state["complete"] else "checkpoint")
        return result
    real_hash = h.blobs._file_identity
    def identity(path):
        assert threading.get_ident() != main_thread
        calls.append("hash")
        return real_hash(path)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(h.blobs.voice_uploads, "_save", save)
    monkeypatch.setattr(h.blobs, "_file_identity", identity)
    async with h.client() as c:
        assert (await init(c, m))["next_offset"] == 0
        calls.clear()
        assert (await send_chunk(c, m, b"off-loop durability evidence", final=True))["complete"]
        assert calls[-1] == "complete_checkpoint"
        assert calls.index("checkpoint") < calls.index("complete_checkpoint")


@pytest.mark.asyncio
async def test_v2_dedup_reverifies_and_fsyncs_existing_legacy_canonical(voice_harness, monkeypatch):
    h = voice_harness
    data = b"shared legacy canonical"
    m = metadata(h, data)
    async with h.client() as c:
        import base64
        await c.rpc("upload_blob_init", request_id="legacy-complete")
        legacy = await c.rpc("upload_blob_chunk", request_id="legacy-complete",
                             data_b64=base64.b64encode(data).decode(), final=True)
        assert legacy["blob_sha"] == m["sha256"]
        await init(c, m)
        real = os.fsync
        canonical = h.blobs._path_for(m["sha256"])
        inode = canonical.stat().st_ino
        synced = []
        def fsync(fd):
            if os.fstat(fd).st_ino == inode:
                synced.append(True)
            return real(fd)
        monkeypatch.setattr(os, "fsync", fsync)
        assert (await send_chunk(c, m, data, final=True))["blob_sha"] == m["sha256"]
        assert synced, "v2 canonical durability must also cover legacy dedup"
        assert not (path_for(h, m) / "audio.part").exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_still_joins_the_real_checkpoint_writer(voice_harness, monkeypatch):
    h = voice_harness
    m = metadata(h, b"abcdefgh")
    entered, release = threading.Event(), threading.Event()
    async with h.client() as c:
        await init(c, m)
        ws = next(w for w in h.server._clients if h.server._operator_authenticated(w))
        real = h.blobs.voice_uploads._save
        def blocked(path, state):
            if state["next_offset"] == 4 and not entered.is_set():
                entered.set()
                assert release.wait(5)
            return real(path, state)
        monkeypatch.setattr(h.blobs.voice_uploads, "_save", blocked)
        task = asyncio.create_task(h.server._dispatch(json.dumps(
            {"type": "upload_blob_chunk", "request_id": "cancel-twice", **chunk(m, b"abcd")}), websocket=ws))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done(), "a second cancellation released an in-flight writer"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await init(c, m))["next_offset"] == 4
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
