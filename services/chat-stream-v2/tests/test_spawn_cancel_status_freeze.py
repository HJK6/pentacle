"""In-flight spawn cancellation, key/request status lookup, and admission freeze.

The public verbs cover cancellation before binding, lookup by key, and a
temporary admission hold:
  * cancel releases a pre-bind reservation (terminal `cancelled`) and, after a
    bind, refuses with `cancel_after_bind` naming the stream id;
  * status lists the outcome/reservation for a key or request id;
  * a hold refuses new admission with `spawn_frozen` and clears on release.
"""

from __future__ import annotations

import asyncio

import pytest

from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl, VerbError  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"


class PerNameTmux:
    def __init__(self) -> None:
        self.live: set[str] = set()
        self.created = 0

    async def has_session(self, name: str) -> bool:
        return name in self.live

    async def new_session(self, name: str, _command: str, cwd=None, env=None) -> None:
        self.live.add(name)
        self.created += 1

    async def capture(self, _name: str) -> str:
        return "READY"

    async def pane_pid(self, _name: str) -> str:
        return "1234"

    async def session_state(self, name: str) -> str:
        return "alive" if name in self.live else "gone"

    async def kill_session(self, name: str) -> None:
        self.live.discard(name)


def _ctl():
    tmux = PerNameTmux()
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    ctl = SpawnCtl(store, sessions, tmux=tmux)
    return tmux, store, sessions, ctl


def test_cancel_before_bind_releases_reservation_and_is_terminal() -> None:
    async def go():
        tmux, store, _sessions, ctl = _ctl()
        try:
            # A pending reservation with NO pane (the in-flight, pre-bind state).
            name = "v2-pending1"
            ok = await store.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="rq-pending",
                nonce="n1", owner_instance_id="inst", idempotency_key="kc1",
                request_payload_hash="ph1",
            )
            assert ok
            reply = await ctl.spawn_cancel({"target": "kc1", "host": HOST}, HOST)
            assert reply["type"] == "spawn_cancel.ok"
            assert reply["state"] == "cancelled"
            assert reply["stream_id"] == f"{HOST}:{name}"
            # Reservation gone; terminal cancelled visible via status/await.
            assert await store.get_spawn_outcome(HOST, name) is not None
            assert (await store.get_spawn_outcome(HOST, name))["state"] == "cancelled"
            assert [r for r in await store.reservations() if r["session_name"] == name] == []
            assert tmux.created == 0
        finally:
            store.stop()

    asyncio.run(go())


def test_cancel_by_request_id_also_resolves() -> None:
    async def go():
        tmux, store, _sessions, ctl = _ctl()
        try:
            name = "v2-pending2"
            await store.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="rq-xyz",
                nonce="n1", owner_instance_id="inst", idempotency_key="kc2",
                request_payload_hash="ph",
            )
            reply = await ctl.spawn_cancel({"target": "rq-xyz", "host": HOST}, HOST)
            assert reply["type"] == "spawn_cancel.ok"
            assert reply["state"] == "cancelled"
        finally:
            store.stop()

    asyncio.run(go())


def test_cancel_after_bind_refuses_and_names_stream() -> None:
    async def go():
        tmux, store, _sessions, ctl = _ctl()
        try:
            base = {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kb1", "request_id": "rb1"}
            first = await ctl.spawn(dict(base), HOST)
            assert first["type"] == "spawn.ok"
            reply = await ctl.spawn_cancel({"target": "kb1", "host": HOST}, HOST)
            assert reply["type"] == "spawn_cancel.error"
            assert reply["error_code"] == "cancel_after_bind"
            assert reply["stream_id"] == first["stream_id"]
            # The bound pane is untouched.
            assert tmux.created == 1
        finally:
            store.stop()

    asyncio.run(go())


def test_cancel_unknown_target_is_not_found() -> None:
    async def go():
        _tmux, store, _sessions, ctl = _ctl()
        try:
            reply = await ctl.spawn_cancel({"target": "nope", "host": HOST}, HOST)
            assert reply["type"] == "spawn_cancel.error"
            assert reply["error_code"] == "not_found"
        finally:
            store.stop()

    asyncio.run(go())


def test_cancel_is_idempotent_on_already_cancelled() -> None:
    async def go():
        _tmux, store, _sessions, ctl = _ctl()
        try:
            name = "v2-pending3"
            await store.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="rq3",
                nonce="n", owner_instance_id="inst", idempotency_key="kc3",
                request_payload_hash="ph",
            )
            assert (await ctl.spawn_cancel({"target": "kc3", "host": HOST}, HOST))["type"] == "spawn_cancel.ok"
            again = await ctl.spawn_cancel({"target": "kc3", "host": HOST}, HOST)
            assert again["type"] == "spawn_cancel.ok"
            assert again["state"] == "cancelled"
        finally:
            store.stop()

    asyncio.run(go())


def test_status_lists_outcome_for_key_and_request_id() -> None:
    async def go():
        _tmux, store, _sessions, ctl = _ctl()
        try:
            base = {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "ks1", "request_id": "rs1"}
            first = await ctl.spawn(dict(base), HOST)
            for target in ("ks1", "rs1"):
                reply = await ctl.spawn_status({"target": target, "host": HOST}, HOST)
                assert reply["type"] == "spawn_status.ok"
                assert reply["found"] is True
                rows = reply["outcomes"]
                assert any(r["stream_id"] == first["stream_id"] for r in rows)
                assert all("state" in r and "request_payload_hash" in r for r in rows)
        finally:
            store.stop()

    asyncio.run(go())


def test_status_unknown_target_found_false() -> None:
    async def go():
        _tmux, store, _sessions, ctl = _ctl()
        try:
            reply = await ctl.spawn_status({"target": "ghost", "host": HOST}, HOST)
            assert reply["type"] == "spawn_status.ok"
            assert reply["found"] is False
        finally:
            store.stop()

    asyncio.run(go())


def test_freeze_refuses_new_admission_and_clears_on_release() -> None:
    async def go():
        tmux, store, _sessions, ctl = _ctl()
        try:
            await ctl.set_spawn_freeze(HOST, reason="maintenance-window", ttl_s=600)
            # While frozen, a new admission is refused -- no pane created.
            with pytest.raises(VerbError) as exc:
                await ctl.spawn({"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kf1"}, HOST)
            assert exc.value.code == "spawn_frozen"
            assert tmux.created == 0
            # Status surfaces the hold.
            st = await ctl.spawn_status({"target": "kf1", "host": HOST}, HOST)
            assert st["hold"] is not None and st["hold"]["reason"] == "maintenance-window"
            # Cleared -> admission resumes and a pane is created.
            await ctl.clear_spawn_freeze(HOST)
            ok = await ctl.spawn({"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kf1"}, HOST)
            assert ok["type"] == "spawn.ok"
            assert tmux.created == 1
        finally:
            store.stop()

    asyncio.run(go())


def test_expired_freeze_does_not_block_admission() -> None:
    async def go():
        tmux, store, _sessions, ctl = _ctl()
        try:
            await ctl.set_spawn_freeze(HOST, reason="stale", ttl_s=-1)
            ok = await ctl.spawn({"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kf2"}, HOST)
            assert ok["type"] == "spawn.ok"
            assert tmux.created == 1
        finally:
            store.stop()

    asyncio.run(go())


class GatedTmux(PerNameTmux):
    """Blocks inside `new_session` (pane already created, reservation not yet
    marked bound) so a test can interleave a concurrent `spawn_cancel` in the
    exact cancel-vs-bind window test contract case [0] hit."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def new_session(self, name: str, _command: str, cwd=None, env=None) -> None:
        self.live.add(name)
        self.created += 1
        self.entered.set()
        await self.release.wait()


def _ctl_gated():
    tmux = GatedTmux()
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    ctl = SpawnCtl(store, sessions, tmux=tmux)
    return tmux, store, sessions, ctl


def test_cancel_racing_bind_never_leaves_cancelled_and_delivered() -> None:
    """test contract case [0]: a `spawn_cancel` that wins the reservation while a spawn
    is mid-bind must NOT leave the bad triple {cancelled outcome + delivered +
    open pane}. Cancel arrives in the window after the pane is created but
    before it is committed to a session row; the bind must abort. Terminal
    state is exactly one of {cancelled-no-pane, bound-cancel_after_bind}."""
    async def go():
        tmux, store, sessions, ctl = _ctl_gated()
        try:
            base = {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "krace1", "request_id": "rrace1"}
            spawn_task = asyncio.create_task(ctl.spawn(dict(base), HOST))
            await asyncio.wait_for(tmux.entered.wait(), timeout=5)
            # Pane exists, reservation not yet bound -> cancel wins.
            reply = await ctl.spawn_cancel({"target": "krace1", "host": HOST}, HOST)
            assert reply["type"] == "spawn_cancel.ok"
            assert reply["state"] == "cancelled"
            tmux.release.set()
            spawn_reply = await asyncio.wait_for(spawn_task, timeout=5)
            # The bind observed the cancel and aborted: cancelled outcome stands,
            # no open session row, the pane was torn down. NEVER delivered+pane.
            name = reply["stream_id"].split(":", 1)[1]
            outcome = await store.get_spawn_outcome(HOST, name)
            assert outcome is not None and outcome["state"] == "cancelled", outcome
            assert await store.fetch_session(HOST, name) is None
            assert name not in tmux.live
            assert spawn_reply.get("state") == "cancelled"
        finally:
            tmux.release.set()
            if ctl._background_spawns:
                await asyncio.gather(*list(ctl._background_spawns), return_exceptions=True)
            store.stop()

    asyncio.run(go())


def test_hold_set_after_reservation_lets_inflight_bind_complete() -> None:
    """A request already past admission (reserved, binding)
    when a freeze is set has a DEFINED outcome -- its bind COMPLETES; the hold
    only refuses a NEW claim. No undefined state."""
    async def go():
        tmux, store, sessions, ctl = _ctl_gated()
        try:
            base = {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "khold1", "request_id": "rhold1"}
            spawn_task = asyncio.create_task(ctl.spawn(dict(base), HOST))
            await asyncio.wait_for(tmux.entered.wait(), timeout=5)
            # Freeze mid-bind: the in-flight (already reserved) request is unaffected.
            await ctl.set_spawn_freeze(HOST, reason="maintenance-window", ttl_s=600)
            tmux.release.set()
            spawn_reply = await asyncio.wait_for(spawn_task, timeout=5)
            assert spawn_reply["type"] == "spawn.ok"
            # A genuinely NEW key is refused while held.
            with pytest.raises(VerbError) as exc:
                await ctl.spawn({"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "khold-new"}, HOST)
            assert exc.value.code == "spawn_frozen"
        finally:
            tmux.release.set()
            if ctl._background_spawns:
                await asyncio.gather(*list(ctl._background_spawns), return_exceptions=True)
            store.stop()

    asyncio.run(go())


def test_freeze_allows_replay_of_already_admitted_key() -> None:
    """A hold refuses a NEW claim but must still REPLAY an already-admitted key
    (test contract case [1]): an interrupted caller's retry during a maintenance freeze gets
    the existing seat, not spawn_frozen."""
    async def go():
        tmux, store, _sessions, ctl = _ctl()
        try:
            base = {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kfr1", "request_id": "rfr1"}
            first = await ctl.spawn(dict(base), HOST)
            assert first["type"] == "spawn.ok"
            await ctl.set_spawn_freeze(HOST, reason="maintenance-window", ttl_s=600)
            # Same-key retry during the hold -> replay the existing seat.
            replay = await ctl.spawn(dict(base), HOST)
            assert replay["type"] == "spawn.ok"
            assert replay["stream_id"] == first["stream_id"]
            assert tmux.created == 1
            # A genuinely NEW key is still refused while held.
            with pytest.raises(VerbError) as exc:
                await ctl.spawn({"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kfr-new"}, HOST)
            assert exc.value.code == "spawn_frozen"
        finally:
            if ctl._background_spawns:
                await asyncio.gather(*list(ctl._background_spawns), return_exceptions=True)
            store.stop()

    asyncio.run(go())
