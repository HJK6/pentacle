"""Synthetic pre-input cancellation through real file Store/Comms/Sessions.

No daemon listeners or native tmux are used. The close integration exercises
Server's real drain, then reopens the database; it is not a process-kill proof.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest

from comms import Comms
from server import Server
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from test_send_semantics import (
    FakeBlobStore, FakeHosts, FakeTmux, HOST, NAME, REMOTE, VALID,
)


pytestmark = pytest.mark.timeout(20)

STREAM = f"{REMOTE}:{NAME}"
REQUEST = "synthetic-materialization-cancel"
REASON = "cancelled_before_submission"


def _payload() -> dict:
    return {
        "stream_id": STREAM,
        "text": "Invented attachment caption for cancellation coverage",
        "attachments": [dict(VALID)],
        "request_id": REQUEST,
        "optimistic_id": "synthetic-materialization-optimistic",
    }


class StagingBarrier(FakeTmux):
    def __init__(self) -> None:
        super().__init__()
        self.staging = asyncio.Event()
        self.release_staging = asyncio.Event()
        self.staging_cancelled = asyncio.Event()

    async def stage_text(self, path: str, data: bytes) -> None:
        self.staging.set()
        try:
            await self.release_staging.wait()
        except asyncio.CancelledError:
            self.staging_cancelled.set()
            raise
        await super().stage_text(path, data)


def _file_comms(tmp_path: Path, remote: FakeTmux) -> tuple[Comms, Store, Sessions]:
    """File-backed equivalent of test_send_semantics._new_comms, also on reopen."""
    local = FakeTmux()
    store = Store(str(tmp_path / "send.sqlite3"))
    store.start()
    sessions = Sessions(store, tmux=local, local_host=HOST)
    comms = Comms(
        store, sessions, SpawnCtl(store, sessions, tmux=local),
        hosts=FakeHosts(local, remote_tmux=remote), blob_store=FakeBlobStore(),
        attachment_root=tmp_path / "attachments",
    )
    return comms, store, sessions


async def _history(store: Store) -> list[dict]:
    return await store.submit(lambda conn: [
        dict(row) for row in conn.execute(
            "SELECT rowid AS receipt_rowid, * FROM v2_send_receipts "
            "WHERE to_stream_id=? AND request_id=? ORDER BY rowid",
            (STREAM, REQUEST),
        )
    ])


def _no_input(comms: Comms, remote: FakeTmux) -> None:
    for tmux in (comms.spawnctl.tmux, remote):
        assert tmux.pastes == []
        assert tmux.enters == 0


async def _settle(task: asyncio.Task | None) -> None:
    if task is not None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _no_new_tasks(before: set[asyncio.Task]) -> None:
    # Let done callbacks run, but do not hide a detached receipt-write task.
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - before)


async def _at_staging(comms: Comms, store: Store, remote: StagingBarrier) -> dict:
    await asyncio.wait_for(remote.staging.wait(), 2)
    rows = await _history(store)
    assert len(rows) == 1
    assert rows[0]["state"] == rows[0]["delivery"] == "accepted"
    assert rows[0]["request_id"] == REQUEST
    assert comms.blob_store.reads == [VALID["key"]]
    _no_input(comms, remote)
    return rows[0]


async def _cancel(task: asyncio.Task, message: str = "synthetic-first-cancel") -> None:
    task.cancel(message)
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.args == (message,)
    assert task.cancelled()


async def _assert_not_landed(store: Store, claim: dict) -> list[dict]:
    latest = await store.get_send_receipt(STREAM, REQUEST)
    assert latest["state"] == latest["delivery"] == "not_landed"
    assert latest["reason"] == REASON
    assert latest["submission_confirmed"] is False
    assert "attempts" not in latest
    assert latest["receipt_id"] == claim["receipt_id"]
    assert latest["request_id"] == claim["request_id"] == REQUEST
    assert latest["to_stream_id"] == STREAM
    rows = await _history(store)
    assert len(rows) == 2
    assert rows[0] == claim  # The accepted row was not rewritten.
    assert rows[1]["receipt_rowid"] > rows[0]["receipt_rowid"]
    for field in (
        "receipt_id", "request_id", "to_stream_id", "wire_digest",
        "display_text", "attachments_json", "optimistic_id", "meta_json",
    ):
        assert rows[1][field] == claim[field]
    assert rows[1]["attempts"] is None
    return rows


async def _retry_once_and_coalesce(comms: Comms, store: Store, remote: FakeTmux, claim: dict) -> None:
    landed = await comms.send(_payload())  # Unchanged payload, SAME request id.
    assert landed.get("coalesced") is not True
    assert landed["delivery"] == "landed"
    assert landed["submission_confirmed"] is True
    assert landed["attempt"] == 1
    assert len(remote.pastes) == 1 and remote.enters == 0
    rows = await _history(store)
    assert [(row["state"], row["delivery"]) for row in rows] == [
        ("accepted", "accepted"), ("not_landed", "not_landed"),
        ("accepted", "accepted"), ("accepted", "accepted"), ("landed", "landed"),
    ]
    assert rows[0] == claim
    assert rows[1]["reason"] == REASON
    assert rows[0]["receipt_id"] == rows[1]["receipt_id"]
    assert rows[2]["receipt_id"] == rows[3]["receipt_id"] == rows[4]["receipt_id"] == landed["receipt_id"]
    assert landed["receipt_id"] != claim["receipt_id"]
    assert rows[2]["wire_digest"] == claim["wire_digest"]
    assert rows[3]["wire_digest"] == rows[4]["wire_digest"] != claim["wire_digest"]
    duplicate = await comms.send(_payload())
    assert duplicate["coalesced"] is True
    assert duplicate["delivery"] == "landed"
    assert duplicate["submission_confirmed"] is True
    assert duplicate["receipt_id"] == landed["receipt_id"]
    assert len(remote.pastes) == 1 and remote.enters == 0
    final = await _history(store)
    assert len(final) == 6 and final[:5] == rows
    assert final[-1]["state"] == final[-1]["delivery"] == "landed"
    assert final[-1]["reason"] == f"coalesced_replay:{REQUEST}"
    assert all(row["request_id"] == REQUEST for row in final)
    assert [row["receipt_rowid"] for row in final] == sorted({row["receipt_rowid"] for row in final})


def test_materialization_cancellation_persists_not_landed_after_reopen(tmp_path: Path) -> None:
    async def go() -> None:
        remote = StagingBarrier()
        comms, store, sessions = _file_comms(tmp_path, remote)
        task = None
        before = asyncio.all_tasks()
        try:
            await sessions.open(REMOTE, NAME, provider="claude")
            task = asyncio.create_task(comms.send(_payload()))
            claim = await _at_staging(comms, store, remote)
            await _cancel(task)
            assert remote.staging_cancelled.is_set()
            _no_input(comms, remote)
            rows = await _assert_not_landed(store, claim)
            await _no_new_tasks(before)
            store.stop()
            comms, store, sessions = _file_comms(tmp_path, remote)
            assert await _assert_not_landed(store, claim) == rows
        finally:
            await _settle(task)
            store.stop()

    asyncio.run(go())


def test_materialization_cancellation_same_key_retries_once_after_reopen(tmp_path: Path) -> None:
    async def go() -> None:
        remote = StagingBarrier()
        comms, store, sessions = _file_comms(tmp_path, remote)
        task = None
        try:
            await sessions.open(REMOTE, NAME, provider="claude")
            task = asyncio.create_task(comms.send(_payload()))
            claim = await _at_staging(comms, store, remote)
            await _cancel(task)
            _no_input(comms, remote)
            store.stop()
            comms, store, sessions = _file_comms(tmp_path, remote)
            remote.release_staging.set()
            await _retry_once_and_coalesce(comms, store, remote, claim)
        finally:
            await _settle(task)
            store.stop()

    asyncio.run(go())


def test_cancellation_after_paste_keeps_receipt_uncertain(tmp_path: Path) -> None:
    class ProofBarrier(FakeTmux):
        def __init__(self) -> None:
            super().__init__()
            self.proof = asyncio.Event()
            self.release_proof = asyncio.Event()

        async def capture(self, name: str) -> str:
            if self.pastes:
                self.proof.set()
                await self.release_proof.wait()
            return await super().capture(name)

    async def go() -> None:
        remote = ProofBarrier()
        comms, store, sessions = _file_comms(tmp_path, remote)
        task = None
        try:
            await sessions.open(REMOTE, NAME, provider="claude")
            task = asyncio.create_task(comms.send(_payload()))
            await asyncio.wait_for(remote.proof.wait(), 2)
            assert len(remote.pastes) == 1
            rows = await _history(store)
            assert len(rows) == 2  # Initial claim and materialized wire update.
            await _cancel(task)
            assert await _history(store) == rows
            remote.release_proof.set()
            duplicate = await comms.send(_payload())
            assert duplicate["coalesced"] is True
            assert duplicate["submission_confirmed"] is False
            assert duplicate["do_not_resubmit"] is True
            assert len(remote.pastes) == 1 and remote.enters == 0
            final = await _history(store)
            assert len(final) == 3 and final[:2] == rows
            assert all(row["state"] == row["delivery"] == "accepted" for row in final)
            assert all(row["reason"] != REASON for row in final)
        finally:
            remote.release_proof.set()
            await _settle(task)
            store.stop()

    asyncio.run(go())


def test_cancellation_cleanup_store_failure_preserves_original_cancel(tmp_path: Path, monkeypatch, caplog) -> None:
    class SyntheticStoreUnavailable(RuntimeError):
        pass

    async def go() -> None:
        remote = StagingBarrier()
        comms, store, sessions = _file_comms(tmp_path, remote)
        task = None
        writes = []
        append = store.append_send_receipt

        async def unavailable(**kwargs):
            if kwargs.get("reason") == REASON:
                writes.append(kwargs)
                raise SyntheticStoreUnavailable("SECRET-EXCEPTION-TEXT " + _payload()["text"])
            return await append(**kwargs)

        monkeypatch.setattr(store, "append_send_receipt", unavailable)
        try:
            await sessions.open(REMOTE, NAME, provider="claude")
            task = asyncio.create_task(comms.send(_payload()))
            claim = await _at_staging(comms, store, remote)
            await _cancel(task)
            assert len(writes) == 1
            assert writes[0]["receipt_id"] == claim["receipt_id"]
            assert writes[0]["request_id"] == REQUEST
            assert writes[0]["state"] == writes[0]["delivery"] == "not_landed"
            assert writes[0]["submission_confirmed"] is False
            assert writes[0]["attempts"] is None
            assert await _history(store) == [claim]
            _no_input(comms, remote)
            scoped = [record for record in caplog.records if "restart_materialization_cancel" in record.getMessage()]
            assert len(scoped) == 1
            message = scoped[0].getMessage()
            assert "subsystem=send" in message
            assert "bug_ref=restart_materialization_cancel" in message
            assert "SyntheticStoreUnavailable" in message and REQUEST in message
            assert scoped[0].exc_info is None
            for secret in ("SECRET-EXCEPTION-TEXT", _payload()["text"], VALID["key"], "image/png"):
                assert secret not in caplog.text
            # A failed write did not become permission to resend.
            remote.release_staging.set()
            duplicate = await comms.send(_payload())
            assert duplicate["coalesced"] is True
            _no_input(comms, remote)
        finally:
            await _settle(task)
            store.stop()

    with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.comms"):
        asyncio.run(go())


def test_second_cancellation_does_not_detach_cleanup(tmp_path: Path, monkeypatch) -> None:
    async def go() -> None:
        remote = StagingBarrier()
        comms, store, sessions = _file_comms(tmp_path, remote)
        before = asyncio.all_tasks()
        cleanup = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_cancelled = asyncio.Event()
        cleanup_owners = []
        append = store.append_send_receipt
        task = None

        async def pause_before_submission(**kwargs):
            if kwargs.get("reason") == REASON:
                cleanup_owners.append(asyncio.current_task())
                cleanup.set()
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    cleanup_cancelled.set()
                    raise
            return await append(**kwargs)

        monkeypatch.setattr(store, "append_send_receipt", pause_before_submission)
        try:
            await sessions.open(REMOTE, NAME, provider="claude")
            task = asyncio.create_task(comms.send(_payload()))
            claim = await _at_staging(comms, store, remote)
            task.cancel("synthetic-first-cancel")
            await asyncio.wait_for(cleanup.wait(), 2)
            assert cleanup_owners == [task]
            await _cancel(task, "synthetic-second-cancel")
            assert cleanup_cancelled.is_set()
            _no_input(comms, remote)
            assert await _history(store) == [claim]
            await _no_new_tasks(before)
        finally:
            release_cleanup.set()
            await _settle(task)
            store.stop()

    asyncio.run(go())


def test_server_close_materialization_cancellation(tmp_path: Path) -> None:
    async def go() -> None:
        remote = StagingBarrier()
        comms, store, sessions = _file_comms(tmp_path, remote)
        before = asyncio.all_tasks()
        task = None
        try:
            await sessions.open(REMOTE, NAME, provider="claude")
            server = Server(store=store, sessions=sessions, comms=comms)
            server.spawn_ready.set()
            assert server._ws_server is None and server._tls_ws_server is None
            assert not server._client_writer_tasks
            task = asyncio.create_task(comms.send(_payload()))
            server._detached_send_tasks.add(task)
            task.add_done_callback(server._detached_send_tasks.discard)
            claim = await _at_staging(comms, store, remote)
            started = time.monotonic()
            await server.close(deadline=started + 8.0)
            elapsed = time.monotonic() - started
            assert 5.0 <= elapsed < 8.5  # Fixture tolerance, not a production SLA.
            assert task.done() and task.cancelled()
            assert remote.staging_cancelled.is_set()
            _no_input(comms, remote)
            rows = await _assert_not_landed(store, claim)
            await _no_new_tasks(before)
            assert not server._detached_send_tasks
            store.stop()
            comms, store, sessions = _file_comms(tmp_path, remote)
            assert await _assert_not_landed(store, claim) == rows
            remote.release_staging.set()
            await _retry_once_and_coalesce(comms, store, remote, claim)
            await _no_new_tasks(before)
        finally:
            await _settle(task)
            store.stop()

    asyncio.run(go())


def test_held_ingress_does_not_materialize_or_become_retryable(tmp_path: Path) -> None:
    class HeldIngress:
        async def ingress(self, **_kwargs):
            return {"held": True}

    async def go() -> None:
        remote = StagingBarrier()
        comms, store, sessions = _file_comms(tmp_path, remote)
        comms.front_desk_digest = HeldIngress()
        try:
            await sessions.open(REMOTE, NAME, provider="claude")
            held = await comms.send(_payload())
            assert held["delivery"] == "persisted"
            assert held["reason"] == "front_desk_held"
            assert held["submission_confirmed"] is False
            assert held["action_committed"] is True
            rows = await _history(store)
            assert [(row["state"], row["delivery"]) for row in rows] == [
                ("accepted", "accepted"), ("accepted", "persisted"),
            ]
            assert rows[-1]["reason"] == "front_desk_held"
            assert not remote.staging.is_set() and comms.blob_store.reads == []
            duplicate = await comms.send(_payload())
            assert duplicate["coalesced"] is True
            assert duplicate["delivery"] == "persisted"
            assert len(await _history(store)) == 3
            _no_input(comms, remote)
        finally:
            store.stop()

    asyncio.run(go())
