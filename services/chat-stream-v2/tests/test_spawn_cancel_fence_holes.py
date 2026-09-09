"""Spawn cancellation and reservation fencing contract tests.

These tests assert observable invariants with disposable stores and terminal
doubles, leaving the implementation's fencing mechanism unconstrained:

  * a `cancelled` logical spawn NEVER ends with a live pane + open row +
    delivered/admitted outcome, by ANY path (in-process bind, reconciler
    adoption, codex boot-permit queue, late outcome write, same-name reuse,
    a crash before the bind commit);
  * a cancel that cannot confirm the pane gone leaves a durable handle the
    reconciler settles by identity -- never a silent orphan;
  * a TTL-swept reservation aborts the spawn BEFORE new_session;
  * a cancelled spawn skips handoff post-steps;
  * cancel after a real bind names the stream (cancel_after_bind);
  * a cancelled request stays terminal across a successful same-name reuse
    (tombstone replay).

Every gate is per-instance and restored in ``finally``; no global or class
mutation is required.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

import tmux_transport  # noqa: E402
from _shared.spawn_profiles import boot_limits  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import PANE_NONCE_ENV, SpawnCtl, VerbError  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"


class ReconTmux:
    """Rich fake for reconcile/adoption/cleanup: records the creation nonce from
    `new_session` env, answers `show-environment`/`pane_identity`/`kill_pane`, and
    honors a per-instance `kill_works` flag (never a class mutation)."""

    def __init__(self, *, kill_works: bool = True) -> None:
        self.live: set[str] = set()
        self.created = 0
        self.kill_calls = 0
        self.nonces: dict[str, str] = {}
        self.kill_works = kill_works

    async def has_session(self, name: str) -> bool:
        return name in self.live

    async def new_session(self, name: str, _command: str, cwd=None, env=None) -> None:
        self.live.add(name)
        self.created += 1
        if env and PANE_NONCE_ENV in env:
            self.nonces[name] = env[PANE_NONCE_ENV]

    async def capture(self, _name: str) -> str:
        return "READY"

    async def pane_pid(self, _name: str) -> str:
        return "1234"

    async def session_state(self, name: str) -> str:
        return "alive" if name in self.live else "gone"

    async def kill_session(self, name: str) -> None:
        self.kill_calls += 1
        if self.kill_works:
            self.live.discard(name)

    async def kill_pane(self, pane_id: str) -> None:
        self.kill_calls += 1
        if self.kill_works:
            self.live.discard(str(pane_id).lstrip("%"))

    async def pane_identity(self, name: str):
        if name in self.live:
            return {"pane_id": "%" + name, "pane_pid": "1234", "session_name": name}
        return None

    async def run(self, *args, timeout: float = 10.0, **_kw):
        if args and args[0] == "show-environment":
            target = args[2] if len(args) > 2 else ""
            nm = str(target).lstrip("=").rstrip(":")
            nonce = self.nonces.get(nm, "")
            return (0, f"{PANE_NONCE_ENV}={nonce}\n") if nonce else (1, "")
        return (0, "")


class NewSessionGatedTmux(ReconTmux):
    """Blocks inside `new_session` (pane created + nonce recorded, reservation not
    yet bound) so a test can interleave a concurrent `spawn_cancel`."""

    def __init__(self, *, kill_works: bool = True) -> None:
        super().__init__(kill_works=kill_works)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def new_session(self, name: str, _command: str, cwd=None, env=None) -> None:
        self.live.add(name)
        self.created += 1
        if env and PANE_NONCE_ENV in env:
            self.nonces[name] = env[PANE_NONCE_ENV]
        self.entered.set()
        await self.release.wait()


class HasSessionGatedTmux(ReconTmux):
    """Blocks inside `has_session` (which runs AFTER reserve but BEFORE
    record_spawn_intent) so a test can drop the reservation in that window."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def has_session(self, name: str) -> bool:
        self.entered.set()
        await self.release.wait()
        return name in self.live


def _ctl(tmux, *, db: str = ":memory:"):
    store = Store(db)
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    ctl = SpawnCtl(store, sessions, tmux=tmux)
    return store, sessions, ctl


def _intent_payload(name: str):
    open_flds = {**tmux_transport.open_fields({"objective": "Exercise interrupted spawn fencing", "command": "stub", "provider": "claude"}),
                 "session_generation": "g-" + name}
    return {"open_fields": open_flds, "brief": "", "delivery_receipt": {"state": "not_requested"}}


async def _drain(ctl):
    if getattr(ctl, "_background_spawns", None):
        await asyncio.gather(*list(ctl._background_spawns), return_exceptions=True)


# ---------------------------------------------------------------------------
# Hole 1: reconciler adoption ignores the cancel fence.
# ---------------------------------------------------------------------------
def test_reconcile_does_not_adopt_a_cancelled_request() -> None:
    """A cancelled reservation with a live nonce-bearing pane must not be
    adopted into an open and delivered seat.
    reservation with a live nonce-bearing pane and a terminal `cancelled` outcome
    """
    async def go():
        name = "v2-adopt-cancel"
        tmux = ReconTmux()
        store, _s, ctl = _ctl(tmux)
        try:
            await store.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="rr1", nonce="nn1",
                owner_instance_id="other-inst", idempotency_key="ka1",
                request_payload_hash="ph1",
            )
            await store.record_spawn_intent(HOST, name, _intent_payload(name))
            await store.set_spawn_outcome(
                HOST, name, "cancelled", request_id="rr1",
                idempotency_key="ka1", reason="cancelled_before_bind",
            )
            tmux.live.add(name)
            tmux.nonces[name] = "nn1"  # pane exposes the reservation nonce
            result = await ctl.reconcile_spawn_intents(limit=5, recurring=False)
            assert result["adopted"] == 0, f"adopted a cancelled request: {result}"
            oc = await store.get_spawn_outcome(HOST, name)
            assert (oc or {}).get("state") == "cancelled", oc
            assert await store.fetch_session(HOST, name) is None, "cancelled request got an open seat"
        finally:
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Hole 2: a late admission/queued outcome resurrects a cancelled request.
# ---------------------------------------------------------------------------
def test_late_admitted_outcome_cannot_resurrect_a_cancelled_bind() -> None:
    """A late admission outcome must not resurrect a cancelled request."""
    async def go():
        name = "v2-late-admit"
        tmux = ReconTmux()
        store, _s, ctl = _ctl(tmux)
        try:
            await store.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="r2", nonce="n2",
                owner_instance_id="", idempotency_key="k2", request_payload_hash="ph2",
            )
            assert (await ctl.spawn_cancel({"target": "r2", "host": HOST}, HOST))["type"] == "spawn_cancel.ok"
            await store.set_spawn_outcome(
                HOST, name, "admitted", request_id="r2", idempotency_key="k2",
                reason="codex_boot_permit_admitted",
            )
            committed = await ctl._commit_pane_bound(HOST, name, "r2", tmux)
            assert committed is False, "bind resurrected a cancelled request after a late admitted outcome"
        finally:
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Hole 2b: cancellation while a Codex spawn is queued.
# ---------------------------------------------------------------------------
def test_cancel_while_codex_queued_is_not_resurrected() -> None:
    """A request cancelled before the boot permit is acquired must not be
    resurrected by queued or admitted outcome writes."""
    async def go():
        name = "v2-codex-queued"
        tmux = ReconTmux()
        store, _s, ctl = _ctl(tmux)
        cap, _timeout = boot_limits(HOST)
        sem = ctl._codex_boot_semaphore(HOST, cap)
        held = 0
        permit_task = None
        try:
            await store.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="rq", nonce="nq",
                owner_instance_id="", idempotency_key="kq", request_payload_hash="phq",
            )
            await store.record_spawn_intent(HOST, name, _intent_payload(name))
            # Cancel BEFORE the boot-permit is acquired (public's window).
            assert (await ctl.spawn_cancel({"target": "kq", "host": HOST}, HOST))["type"] == "spawn_cancel.ok"
            # Saturate the host boot semaphore so the next request QUEUES. The
            # semaphore starts with `cap` permits, so `acquire()` returns
            # immediately `cap` times, then it is locked.
            for _ in range(cap):
                await sem.acquire()
                held += 1
            open_flds = {**tmux_transport.open_fields({"objective": "Exercise interrupted spawn fencing", "command": "stub", "provider": "codex"}),
                         "session_generation": "g-" + name}
            permit_task = asyncio.create_task(ctl._acquire_codex_boot_permit(
                HOST, name, "rq", open_flds,
                idempotency_key="kq", payload_hash="phq", admission=None,
            ))
            # Let write_queued run (it opens the row + writes the queued outcome).
            for _ in range(10):
                await asyncio.sleep(0)
            oc = await store.get_spawn_outcome(HOST, name)
            state = (oc or {}).get("state")
            assert state not in {"queued", "admitted"}, f"cancelled codex request resurrected to {state}"
            assert await store.fetch_session(HOST, name) is None, "cancelled codex request got a seat"
        finally:
            if permit_task is not None:
                permit_task.cancel()
                await asyncio.gather(permit_task, return_exceptions=True)
            for _ in range(held):
                sem.release()
            await _drain(ctl)
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Holes 3+5: a failed / crashed kill after a winning cancel leaves an orphan.
# ---------------------------------------------------------------------------
def test_cancel_with_unconfirmed_kill_leaves_a_reconcilable_handle_not_an_orphan() -> None:
    """A cancel that cannot confirm the pane gone must leave a durable handle
    the reconciler settles, never a live pane with no recoverable reservation."""
    async def go():
        name = "v2-orphan"
        tmux = NewSessionGatedTmux(kill_works=False)
        store, _s, ctl = _ctl(tmux)
        try:
            base = {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "k3", "request_id": "r3", "session_name": name}
            task = asyncio.create_task(ctl.spawn(dict(base), HOST))
            await asyncio.wait_for(tmux.entered.wait(), timeout=5)
            assert (await ctl.spawn_cancel({"target": "k3", "host": HOST}, HOST))["type"] == "spawn_cancel.ok"
            tmux.release.set()
            reply = await asyncio.wait_for(task, timeout=5)
            assert reply.get("state") == "cancelled"
            tmux.kill_works = True  # the pane becomes killable on the reconcile pass
            recon = await ctl.reconcile_spawn_intents(limit=5, recurring=False)
            pane_alive = name in tmux.live
            has_res = any(r["session_name"] == name for r in await store.reservations(include_expired=True))
            # A release counter is NOT a substitute
            # for the invariant. Once the kill is available, a released handle must
            # not leave a live pane -- the pane is gone, OR a durable recovery
            # handle (reservation) still points at it. `recon["released"] >= 1`
            # with a live pane is exactly the orphan this must catch.
            assert (not pane_alive) or has_res, (
                f"orphan pane with no reconcilable handle: pane_alive={pane_alive} "
                f"reservations={has_res} recon={recon}"
            )
        finally:
            tmux.release.set()
            await _drain(ctl)
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Store stop/reopen after cancel plus a precommit pane.
# ---------------------------------------------------------------------------
def test_crash_after_cancel_reconciles_not_adopts_across_restart() -> None:
    """A pane is created, cancellation wins, and the daemon stops before the bind
    commit; after restart (reopen the same DB file) reconcile must clean the
    cancelled request by identity -- never adopt it to delivered or orphan it."""
    async def go():
        tmpdir = tempfile.mkdtemp(prefix="d3crash-")
        db = str(Path(tmpdir) / "store.db")
        name = "v2-crash"
        tmux = ReconTmux()  # the pane survives the daemon crash
        store1, _s1, ctl1 = _ctl(tmux, db=db)
        try:
            await store1.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="rc", nonce="nc",
                owner_instance_id="dead-inst", idempotency_key="kc",
                request_payload_hash="phc",
            )
            await store1.record_spawn_intent(HOST, name, _intent_payload(name))
            tmux.live.add(name)          # new_session created the pane...
            tmux.nonces[name] = "nc"
            # ...cancel wins before the bind commit...
            assert (await ctl1.spawn_cancel({"target": "kc", "host": HOST}, HOST))["type"] == "spawn_cancel.ok"
        finally:
            store1.stop()                # ...daemon crashes here.
        # Restart on the same DB file.
        store2, _s2, ctl2 = _ctl(tmux, db=db)
        try:
            result = await ctl2.reconcile_spawn_intents(limit=5, recurring=False)
            assert result["adopted"] == 0, f"adopted a cancelled request after crash: {result}"
            row = await store2.fetch_session(HOST, name)
            assert row is None or str(row.get("status")) != "open", "cancelled request got an open seat after crash"
            assert name not in tmux.live, "orphan pane survived crash+reconcile"
        finally:
            store2.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Hole 4: a TTL sweep before record_spawn_intent still creates a pane.
# ---------------------------------------------------------------------------
def test_swept_reservation_aborts_before_new_session() -> None:
    """A
    reservation whose TTL LAPSED (payload-less, but NOT yet swept away) in the
    has_session/brief window BEFORE record_spawn_intent must abort the spawn
    before new_session -- the pre-pane record CAS fails closed on the expired
    reservation. Uses real expiry (not a helper that deletes the row): the
    stale-but-present reservation must still prevent pane creation."""
    async def go():
        name = "v2-swept"
        tmux = HasSessionGatedTmux()
        store, _s, ctl = _ctl(tmux)
        try:
            base = {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "k4", "request_id": "r4", "session_name": name}
            task = asyncio.create_task(ctl.spawn(dict(base), HOST))
            await asyncio.wait_for(tmux.entered.wait(), timeout=5)
            # Expire (do NOT delete) the reservation in this pre-intent window, as
            # an unswept TTL lapse would: the row is still present + payload-less.
            def _expire(conn):
                conn.execute(
                    "UPDATE v2_stream_reservations SET expires_at=? WHERE host=? AND session_name=?",
                    (1.0, HOST, name),
                )
                conn.commit()
            await store.submit(_expire)
            present = [r for r in await store.reservations(include_expired=True)
                       if r["session_name"] == name]
            assert present and (present[0].get("payload") is None), "precondition: stale payload-less reservation"
            tmux.release.set()
            await asyncio.gather(task, return_exceptions=True)
            assert tmux.created == 0, "spawn created a pane on an expired (unswept) reservation"
        finally:
            tmux.release.set()
            await _drain(ctl)
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Hole 6: commit drops request_id/generation -> old spawn binds a new reservation.
# ---------------------------------------------------------------------------
def test_stale_commit_does_not_bind_a_reused_name_reservation() -> None:
    """A stale commit must not bind a reservation created by a reused name."""
    async def go():
        name = "v2-reuse"
        tmux = ReconTmux()
        store, _s, ctl = _ctl(tmux)
        try:
            await store.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="old", nonce="nold",
                owner_instance_id="", idempotency_key="kold", request_payload_hash="pho",
            )
            assert (await ctl.spawn_cancel({"target": "old", "host": HOST}, HOST))["type"] == "spawn_cancel.ok"
            await store.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="new", nonce="nnew",
                owner_instance_id="", idempotency_key="knew", request_payload_hash="phn",
            )
            await ctl._commit_pane_bound(HOST, name, "old", tmux)
            res = [r for r in await store.reservations(include_expired=True) if r["session_name"] == name]
            assert res, "new reservation vanished"
            assert not res[0].get("tmux_created"), "old spawn's commit bound the new reservation"
        finally:
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# A cancelled request stays terminal across a successful reuse.
# ---------------------------------------------------------------------------
def test_cancelled_request_stays_terminal_across_same_name_reuse() -> None:
    """Cancel request "old" for name N; a new request binds N; a
    LATE write from "old" must not hijack the new seat, and "old" must still be
    retrievable as `cancelled` (tombstone replay)."""
    async def go():
        name = "v2-reuse-replay"
        tmux = ReconTmux()
        store, _s, ctl = _ctl(tmux)
        try:
            await store.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="old", nonce="nold",
                owner_instance_id="", idempotency_key="kold", request_payload_hash="pho",
            )
            assert (await ctl.spawn_cancel({"target": "old", "host": HOST}, HOST))["type"] == "spawn_cancel.ok"
            new = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "command": "stub", "session_name": name, "idempotency_key": "knew", "request_id": "new"}, HOST
            )
            assert new["type"] == "spawn.ok" and new["stream_id"] == f"{HOST}:{name}"
            # A late write from the cancelled OLD request must not corrupt the new seat.
            await ctl._commit_pane_bound(HOST, name, "old", tmux)
            row = await store.fetch_session(HOST, name)
            assert row is not None and str(row.get("status")) == "open", "new seat was corrupted by a late old write"
            old_oc = await store.get_spawn_outcome_by_request_id("old")
            assert old_oc is not None and old_oc.get("state") == "cancelled", (
                f"cancelled 'old' status was lost on same-name reuse: {old_oc}"
            )
        finally:
            await _drain(ctl)
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Key-only spawns (request_id minted per invocation, cancel by
# key) with same-name reuse.
# ---------------------------------------------------------------------------
def test_key_only_cancel_then_reuse_keeps_tombstone() -> None:
    """A key-only request (distinct minted request_id, cancelled by
    KEY) on name N must stay terminal when a DIFFERENT key-only request reuses N.
    The new seat is healthy; the cancelled key replays `cancelled`, even when
    the same name is reused."""
    async def go():
        name = "v2-keyonly-reuse"
        tmux = NewSessionGatedTmux()
        store, _s, ctl = _ctl(tmux)
        try:
            # #1 key-only (CLI mints "mint-A"), gated mid-bind, cancelled BY KEY.
            base1 = {"objective": "Exercise the existing spawn contract", "command": "stub", "session_name": name,
                     "idempotency_key": "kA", "request_id": "mint-A"}
            t1 = asyncio.create_task(ctl.spawn(dict(base1), HOST))
            await asyncio.wait_for(tmux.entered.wait(), timeout=5)
            assert (await ctl.spawn_cancel({"target": "kA", "host": HOST}, HOST))["type"] == "spawn_cancel.ok"
            tmux.release.set()
            r1 = await asyncio.wait_for(t1, timeout=5)
            assert r1.get("state") == "cancelled"
            # #2 a DIFFERENT key-only request reuses the same name -> binds cleanly
            # (the gate is already released, so new_session proceeds).
            base2 = {"objective": "Exercise the existing spawn contract", "command": "stub", "session_name": name,
                     "idempotency_key": "kB", "request_id": "mint-B"}
            r2 = await asyncio.wait_for(asyncio.create_task(ctl.spawn(dict(base2), HOST)), timeout=5)
            assert r2["type"] == "spawn.ok" and r2["stream_id"] == f"{HOST}:{name}"
            row = await store.fetch_session(HOST, name)
            assert row is not None and str(row.get("status")) == "open", "new key-only seat missing"
            # #1's cancel stays terminal, retrievable by its minted request_id.
            old_oc = await store.get_spawn_outcome_by_request_id("mint-A")
            assert old_oc is not None and old_oc.get("state") == "cancelled", (
                f"cancelled key-only 'mint-A' lost after same-name reuse: {old_oc}"
            )
        finally:
            tmux.release.set()
            await _drain(ctl)
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Regression 7: a cancelled spawn must skip handoff post-steps (public contract case 8).
# ---------------------------------------------------------------------------
def test_cancelled_spawn_skips_handoff_poststeps() -> None:
    """A cancelled spawn must not fall through to handoff finalization
    (reparent plus predecessor close). The predecessor is seeded
    with a real tuple so the successor actually reaches new_session."""
    async def go():
        name = "v2-handoff-cancel"
        tmux = NewSessionGatedTmux()
        store, sessions, ctl = _ctl(tmux)
        await store.open_session(
            HOST, "v2-pred", provider="claude",
            effective_model="claude-opus-4-8", effective_effort="high", role="lead",
        )
        calls = {"reparent": 0, "close": 0}
        orig_reparent = sessions.reparent_children
        orig_close = sessions.close

        async def spy_reparent(*a, **k):
            calls["reparent"] += 1
            return await orig_reparent(*a, **k)

        async def spy_close(*a, **k):
            calls["close"] += 1
            return await orig_close(*a, **k)

        sessions.reparent_children = spy_reparent  # type: ignore[assignment]
        sessions.close = spy_close  # type: ignore[assignment]
        try:
            base = {"objective": "Exercise the existing spawn contract",
                "command": "stub", "idempotency_key": "kh", "request_id": "rh",
                "session_name": name, "handoff": True,
                "handoff_from_stream_id": f"{HOST}:v2-pred",
                "provider": "claude", "model": "claude-opus-4-8", "effort": "high",
            }
            task = asyncio.create_task(ctl.spawn(dict(base), HOST))
            await asyncio.wait_for(tmux.entered.wait(), timeout=5)
            assert (await ctl.spawn_cancel({"target": "kh", "host": HOST}, HOST))["type"] == "spawn_cancel.ok"
            tmux.release.set()
            reply = await asyncio.wait_for(task, timeout=5)
            assert reply.get("state") == "cancelled", reply
            assert calls["reparent"] == 0, "cancelled spawn reparented children"
            assert calls["close"] == 0, "cancelled spawn closed the predecessor"
        finally:
            tmux.release.set()
            sessions.reparent_children = orig_reparent  # type: ignore[assignment]
            sessions.close = orig_close  # type: ignore[assignment]
            await _drain(ctl)
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Regression 8: cancel after a real bind names the stream (public contract case 9).
# ---------------------------------------------------------------------------
def test_cancel_after_successful_bind_names_stream() -> None:
    """After a successful bind, cancel
    returns `cancel_after_bind` naming the exact stream_id -- never `not_found`."""
    async def go():
        tmux = ReconTmux()
        store, _s, ctl = _ctl(tmux)
        try:
            first = await ctl.spawn({"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kb", "request_id": "rb"}, HOST)
            assert first["type"] == "spawn.ok"
            reply = await ctl.spawn_cancel({"target": "kb", "host": HOST}, HOST)
            assert reply["type"] == "spawn_cancel.error"
            assert reply["error_code"] == "cancel_after_bind"
            assert reply.get("stream_id") == first["stream_id"]
        finally:
            await _drain(ctl)
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Compatibility guard: normal adoption.
# ---------------------------------------------------------------------------
def test_old_reservation_row_without_fence_still_reconciles() -> None:
    """An uncancelled interrupted spawn is still adopted;
    the fence must not break normal adoption."""
    async def go():
        name = "v2-legacy-adopt"
        tmux = ReconTmux()
        store, _s, ctl = _ctl(tmux)
        try:
            await store.reserve_stream_id(
                HOST, name, ttl_s=600, request_id="rl1", nonce="nl1",
                owner_instance_id="other-inst", idempotency_key="kl1",
                request_payload_hash="phl",
            )
            await store.record_spawn_intent(HOST, name, _intent_payload(name))
            tmux.live.add(name)
            tmux.nonces[name] = "nl1"
            result = await ctl.reconcile_spawn_intents(limit=5, recurring=False)
            assert result["adopted"] == 1, f"normal interrupted spawn was not adopted: {result}"
            assert await store.fetch_session(HOST, name) is not None
        finally:
            store.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Host-scoped cancellation and admission checks.
# ---------------------------------------------------------------------------
def test_cancel_of_reused_name_uses_request_tombstone_not_stale_outcome() -> None:
    """After name N is cancelled for
    request A, a NEW key B reusing N must be cancelled via its OWN tombstone/CAS
    -- the resolver must not short-circuit on A's stale name-keyed cancelled
    outcome and report success without tombstoning B (which would let B bind)."""
    async def go():
        name = "v2-reuse"
        tmux = NewSessionGatedTmux()
        store, _s, ctl = _ctl(tmux)
        try:
            await store.reserve_stream_id(HOST, name, ttl_s=600, request_id="rA",
                                          nonce="nA", owner_instance_id="i", idempotency_key="kA")
            assert (await ctl.spawn_cancel({"target": "kA", "host": HOST}, HOST))["state"] == "cancelled"
            baseB = {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kB", "request_id": "rB", "session_name": name}
            taskB = asyncio.create_task(ctl.spawn(dict(baseB), HOST))
            await asyncio.wait_for(tmux.entered.wait(), timeout=5)
            reply = await ctl.spawn_cancel({"target": "kB", "host": HOST}, HOST)
            assert reply["type"] == "spawn_cancel.ok" and reply["state"] == "cancelled"
            assert await store.spawn_cancelled(HOST, name, "rB"), "B (reused name) was not tombstoned"
            tmux.release.set()
            outB = await asyncio.wait_for(taskB, timeout=5)
            assert outB.get("state") == "cancelled", outB
            assert await store.fetch_session(HOST, name) is None, "B bound despite being cancelled"
        finally:
            tmux.release.set()
            await _drain(ctl)
            store.stop()
    asyncio.run(go())


def test_cancel_by_request_id_is_host_scoped() -> None:
    """A
    request id is only per-host unique; resolving a cancel/status target on host
    X must never import another host's row that shares the id."""
    async def go():
        store, _s, ctl = _ctl(ReconTmux())
        try:
            name = "v2-hs"
            await store.set_spawn_outcome(HOST, name, "cancelled", request_id="Rshared",
                                          idempotency_key="kx", reason="cancelled_before_bind")
            # localhost resolves its own row; a DIFFERENT host must not import it.
            here = await ctl._resolve_spawn_target(HOST, "Rshared")
            assert any(str(oc.get("session_name")) == name for oc in here["outcomes"])
            there = await ctl._resolve_spawn_target("peer", "Rshared")
            assert not any(str(oc.get("session_name")) == name for oc in there["outcomes"]), \
                "cross-host request-id import: peer resolved localhost's row"
        finally:
            store.stop()
    asyncio.run(go())


def test_freeze_check_is_serialized_into_admission(monkeypatch) -> None:
    """The hold check runs INSIDE the
    atomic claim, so a freeze a separate pre-check missed still refuses the new
    admission (no pane). Simulate the race: the pre-check sees no hold while the
    hold is really set; the atomic claim (reading the table directly) refuses."""
    async def go():
        tmux = ReconTmux()
        store, _s, ctl = _ctl(tmux)
        try:
            await ctl.set_spawn_freeze(HOST, reason="maintenance-window", ttl_s=600)
            async def blind(_host):
                return None
            monkeypatch.setattr(store, "get_spawn_admission_hold", blind)
            with pytest.raises(VerbError) as exc:
                await ctl.spawn({"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kfz"}, HOST)
            assert exc.value.code == "spawn_frozen"
            assert tmux.created == 0, "a new seat was admitted during an active freeze"
        finally:
            await _drain(ctl)
            store.stop()
    asyncio.run(go())


def test_cancellation_tombstone_retains_nonce() -> None:
    """The immutable tombstone must
    carry the creation nonce so a recovery/identity path holding only the
    tombstone can reconstruct identity across name reuse -- the reservation (the
    other nonce source) is deleted at cancel."""
    async def go():
        store, _s, ctl = _ctl(ReconTmux())
        try:
            name = "v2-tnonce"
            await store.reserve_stream_id(HOST, name, ttl_s=600, request_id="rN",
                                          nonce="NONCE-XYZ", owner_instance_id="i", idempotency_key="kN")
            assert (await ctl.spawn_cancel({"target": "kN", "host": HOST}, HOST))["state"] == "cancelled"
            tombs = await store.spawn_cancellations(HOST, "rN")
            assert tombs, "no tombstone recorded"
            assert str(tombs[0].get("nonce") or "") == "NONCE-XYZ", tombs[0]
        finally:
            store.stop()
    asyncio.run(go())
