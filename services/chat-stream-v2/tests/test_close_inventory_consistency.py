"""An accepted close never leaves an open in-memory row behind.

spec_pentacle__migration_chat_reappears_2026_09

The store thread commits a queued close even when the awaiting request task is
cancelled (the server cancels a connection's in-flight tasks on disconnect, and
a self-close kills its own caller's pane). The inventory eviction must still run,
or every snapshot keeps serving the closed generation as open. A close that
finds the durable generation already closed or archived must evict the stale
in-memory copy instead of replying ``already_closed`` while it stays visible.
"""
from __future__ import annotations

import asyncio

import pytest

from sessions import Sessions
from store import Store

HOST = "localhost"


class _NoPaneTmux:
    async def has_session(self, name: str) -> bool:
        return False

    async def pane_pid(self, name: str) -> str:
        return ""

    async def pane_identity(self, name: str) -> None:
        return None

    async def kill_session(self, name: str) -> None:
        return None


async def _fixture(name: str) -> tuple[Store, Sessions, dict]:
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=_NoPaneTmux(), local_host=HOST)
    row = await store.open_session(HOST, name, visibility="visible")
    await sessions.refresh()
    return store, sessions, row


def _open_in_inventory(sessions: Sessions, sid: str) -> bool:
    row = sessions.get(sid)
    return row is not None and str(row.get("status") or "open") == "open"


def test_close_cancelled_after_durable_commit_still_evicts_inventory() -> None:
    """The migration-driver ghost: commit landed, the caller vanished, the row stayed."""

    async def go() -> None:
        name = "s-self-close-cancelled"
        sid = f"{HOST}:{name}"
        store, sessions, _row = await _fixture(name)
        committed = asyncio.Event()
        original = store.mark_closed
        request: list[asyncio.Task] = []

        async def commit_then_requester_disconnects(*args, **kwargs):
            row = await original(*args, **kwargs)
            committed.set()
            # The server's disconnect cleanup cancels the request task after the
            # store thread already committed, before the close resumes.
            request[0].cancel()
            await asyncio.sleep(0)
            return row

        store.mark_closed = commit_then_requester_disconnects  # type: ignore[method-assign]
        try:
            assert _open_in_inventory(sessions, sid)
            request.append(asyncio.create_task(sessions.close(HOST, name, "handed over")))
            with pytest.raises(asyncio.CancelledError):
                await request[0]
            assert committed.is_set()
            durable = await store.fetch_session(HOST, name)
            assert durable is not None and durable["status"] == "closed"
            assert not _open_in_inventory(sessions, sid), (
                "durably closed generation is still served as open from inventory"
            )
            assert sid not in {r["stream_id"] for r in sessions.list_open()}
        finally:
            store.stop()

    asyncio.run(go())


def _drop_durable_row(store: Store, name: str) -> None:
    """Model retention archival of the closed row out of `sessions`."""

    def _op(conn):
        conn.execute("DELETE FROM sessions WHERE host=? AND session_name=?", (HOST, name))
        conn.commit()

    return store.submit(_op)


@pytest.mark.parametrize("archived", [False, True])
def test_close_of_stale_inventory_generation_evicts_it(archived: bool) -> None:
    """Web delete of the ghost: durable row closed (or archived), inventory still open."""

    async def go() -> None:
        name = f"s-ghost-{'archived' if archived else 'closed'}"
        sid = f"{HOST}:{name}"
        store, sessions, row = await _fixture(name)
        try:
            # Durable close committed behind the inventory's back (the lost
            # eviction above), optionally followed by retention archival.
            closed = await store.mark_closed(
                HOST, name, closed_at="2026-09-23T09:51:21Z", pane_status="pane_dead",
                expected_generation=row["session_generation"], close_kind="session_close",
            )
            assert closed is not None
            if archived:
                await _drop_durable_row(store, name)
            assert _open_in_inventory(sessions, sid)

            result = await sessions.close(
                HOST, name, "", close_kind="operator_close",
                attribution={"actor_kind": "operator", "closed_by": "operator:test",
                             "auth_kind": "operator_authenticated", "peer": "web/v2",
                             "request_id": "r-ghost", "defer_if_working": False},
            )
            assert result["failed"] is False
            assert result["already_closed"] is True
            assert not _open_in_inventory(sessions, sid), (
                "close replied already_closed but the ghost stays in every snapshot"
            )
            assert sid not in {r["stream_id"] for r in sessions.list_open()}
            # A reload is a fresh snapshot of the same process inventory.
            assert sessions.get(sid) is None
        finally:
            store.stop()

    asyncio.run(go())


def test_stale_close_keeps_reopened_generation() -> None:
    """Control: a close fenced to an old generation never evicts a live reopen."""

    async def go() -> None:
        name = "s-reopened"
        sid = f"{HOST}:{name}"
        store, sessions, first = await _fixture(name)
        try:
            await sessions.close(HOST, name, "first close")
            reopened = await sessions.open(HOST, name, visibility="visible")
            assert reopened["session_generation"] != first["session_generation"]

            result = await sessions.close(
                HOST, name, "stale", expected_generation=first["session_generation"],
            )
            assert result.get("stale_generation") is True
            live = sessions.get(sid)
            assert live is not None and live["status"] == "open"
            assert live["session_generation"] == reopened["session_generation"]
            durable = await store.fetch_session(HOST, name)
            assert durable["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


class _GonePeerTmux:
    async def session_state(self, _name: str) -> str:
        return "gone"

    async def kill_session(self, _name: str) -> None:
        return None


class _PeerHosts:
    local_host = HOST

    def known(self, host: str) -> bool:
        return host == "peer"

    def is_local(self, host: str) -> bool:
        return host == HOST

    async def probe_once(self, host: str) -> bool:
        return host == "peer"

    def tmux_for(self, host: str) -> object:
        return _GonePeerTmux()


@pytest.mark.parametrize("archived", [False, True])
def test_remote_close_of_stale_inventory_generation_evicts_it(archived: bool) -> None:
    async def go() -> None:
        name = f"s-remote-ghost-{'archived' if archived else 'closed'}"
        sid = f"peer:{name}"
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, tmux=_NoPaneTmux(), local_host=HOST, hosts=_PeerHosts())
        try:
            row = await store.open_session("peer", name, visibility="visible")
            await sessions.refresh()
            assert await store.mark_closed(
                "peer", name, closed_at="2026-09-23T09:51:21Z", pane_status="pane_dead",
                expected_generation=row["session_generation"], close_kind="session_close",
            ) is not None
            if archived:
                def _op(conn):
                    conn.execute("DELETE FROM sessions WHERE host=? AND session_name=?", ("peer", name))
                    conn.commit()
                await store.submit(_op)
            assert _open_in_inventory(sessions, sid)
            result = await sessions.close("peer", name, "")
            assert result["failed"] is False
            assert result["already_closed"] is True
            assert not _open_in_inventory(sessions, sid)
            assert sid not in {r["stream_id"] for r in sessions.list_open()}
        finally:
            store.stop()

    asyncio.run(go())


def test_remote_stale_close_keeps_reopened_generation() -> None:
    async def go() -> None:
        name = "s-remote-reopened"
        sid = f"peer:{name}"
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, tmux=_NoPaneTmux(), local_host=HOST, hosts=_PeerHosts())
        try:
            first = await store.open_session("peer", name, visibility="visible")
            await store.mark_closed("peer", name, closed_at="t", pane_status="pane_dead",
                                    expected_generation=first["session_generation"])
            reopened = await store.open_session("peer", name, visibility="visible")
            await sessions.refresh()
            result = await sessions.close("peer", name, "stale",
                                          expected_generation=first["session_generation"])
            assert result.get("stale_generation") is True
            live = sessions.get(sid)
            assert live is not None and live["session_generation"] == reopened["session_generation"]
        finally:
            store.stop()

    asyncio.run(go())


def test_caller_cancellation_survives_inner_completion_race() -> None:
    from sessions import _finish_despite_cancel

    async def go() -> None:
        gate = asyncio.Event()

        async def inner() -> int:
            await gate.wait()
            return 7

        wrapper = asyncio.create_task(_finish_despite_cancel(inner()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()
        wrapper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrapper

    asyncio.run(go())


def test_inner_failure_propagates_after_caller_cancel() -> None:
    from sessions import _finish_despite_cancel

    async def go() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def inner() -> int:
            started.set()
            await release.wait()
            raise RuntimeError("store failed")

        wrapper = asyncio.create_task(_finish_despite_cancel(inner()))
        await started.wait()
        wrapper.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(RuntimeError, match="store failed"):
            await wrapper

    asyncio.run(go())
