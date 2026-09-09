"""Idempotency-key honoring: a same-key spawn replays its first outcome instead
of minting a second pane or stream.

The fixtures cover repeated requests, retained intents, and terminal outcomes.
"""

from __future__ import annotations

import asyncio

import pytest

from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl, VerbError  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"


class PerNameTmux:
    """Tmux fake that tracks live sessions per NAME (not one global flag) so a
    second minted name really would create a distinct pane — the defect R1 must
    be able to observe."""

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


def _two_spawns(second_msg_overrides: dict | None = None):
    """Drive two spawns through ONE ctl/store/tmux; return both replies + tmux."""

    async def go():
        tmux = PerNameTmux()
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            base = {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "k1", "request_id": "r1"}
            first = await ctl.spawn(dict(base), HOST)
            second_msg = {**base, **(second_msg_overrides or {})}
            try:
                second = await ctl.spawn(second_msg, HOST)
                err = None
            except VerbError as exc:  # conflict path
                second, err = None, exc
            return first, second, err, tmux
        finally:
            store.stop()

    return asyncio.run(go())


def test_same_key_refire_replays_and_creates_no_second_pane() -> None:
    first, second, err, tmux = _two_spawns()
    assert err is None
    assert first["type"] == "spawn.ok"
    # Exactly ONE pane created across both calls (the defect creates two).
    assert tmux.created == 1
    # The re-fire returns the SAME session, not a duplicate.
    assert second["type"] == "spawn.ok"
    assert second["stream_id"] == first["stream_id"]
    assert second.get("replayed") is True


def test_same_request_id_replays_and_creates_no_second_pane() -> None:
    async def go():
        tmux = PerNameTmux()
        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            message = {"objective": "Exercise the existing spawn contract", "command": "stub", "request_id": "same-request"}
            return await ctl.spawn(dict(message), HOST), await ctl.spawn(dict(message), HOST), tmux
        finally:
            store.stop()

    first, second, tmux = asyncio.run(go())
    assert first["type"] == second["type"] == "spawn.ok"
    assert first["stream_id"] == second["stream_id"]
    assert second["replayed"] is True
    assert tmux.created == 1


def test_same_key_different_payload_is_a_conflict() -> None:
    # Same key, materially different request (a different command) must not
    # silently dedupe nor spawn a second pane — it is a typed conflict.
    _first, second, err, tmux = _two_spawns({"command": "different-stub"})
    assert second is None
    assert isinstance(err, VerbError)
    assert err.code == "idempotency_key_conflict"
    assert tmux.created == 1


def _open_session_count(store: Store) -> int:
    async def op():
        return await store.submit(
            lambda c: c.execute("SELECT COUNT(*) FROM sessions WHERE status='open'").fetchone()[0]
        )

    return asyncio.run(op())


def test_N_concurrent_same_key_fires_collapse_to_one_pane_and_one_row() -> None:
    """The load-bearing case (Multi-fire #4): every real instance was CONCURRENT.
    N same-key fires launched together must collapse to exactly ONE pane and ONE
    row via the atomic single-writer claim — a lookup-then-reserve would lose this
    race and mint N panes."""

    async def go():
        tmux = PerNameTmux()
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            base = {"objective": "Exercise idempotent spawn", "command": "stub", "idempotency_key": "kC"}  # no session_name -> minted
            # Fire 5 concurrently; each mints its own name before the atomic claim.
            replies = await asyncio.gather(
                *[ctl.spawn(dict(base, request_id=f"r{i}"), HOST) for i in range(5)],
                return_exceptions=True,
            )
            rows = await store.submit(
                lambda c: c.execute("SELECT COUNT(*) FROM sessions WHERE status='open'").fetchone()[0]
            )
            return replies, tmux, rows
        finally:
            store.stop()

    replies, tmux, rows = asyncio.run(go())
    # Exactly one pane and one open row, regardless of interleaving.
    assert tmux.created == 1, f"expected 1 pane, got {tmux.created}"
    assert rows == 1, f"expected 1 open session row, got {rows}"
    # Every reply resolves to the SAME stream id (no divergent second session).
    stream_ids = {r.get("stream_id") for r in replies if isinstance(r, dict)}
    assert len(stream_ids) == 1, f"replies diverged: {stream_ids}"
    # The winning spawn.ok reports a truthful admitted count of exactly 1.
    oks = [r for r in replies if isinstance(r, dict) and r.get("type") == "spawn.ok"]
    assert oks, "at least one spawn.ok expected"
    for ok in oks:
        assert ok.get("admitted_count") == 1, f"under-reported duplicates: {ok.get('admitted_count')}"


def test_admitted_set_declares_key_scope() -> None:
    first, second, err, tmux = _two_spawns()

    assert err is None
    assert tmux.created == 1
    for reply in (first, second):
        assert reply["admitted_sessions"] == [first["stream_id"]]
        assert reply["admitted_scope"] == "idempotency_key"
        assert reply["admitted_set_authoritative"] is True


def test_distinct_keys_same_payload_are_distinct_and_scoped() -> None:
    async def go():
        tmux = PerNameTmux()
        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            first = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "key-a", "request_id": "request-a"},
                HOST,
            )
            second = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "key-b", "request_id": "request-b"},
                HOST,
            )
            return first, second, tmux
        finally:
            store.stop()

    first, second, tmux = asyncio.run(go())
    assert tmux.created == 2
    assert first["stream_id"] != second["stream_id"]
    for reply in (first, second):
        assert reply["admitted_sessions"] == [reply["stream_id"]]
        assert reply["admitted_scope"] == "idempotency_key"
        assert reply["admitted_set_authoritative"] is True


def test_same_key_replay_keeps_reserved_id() -> None:
    async def go():
        tmux = PerNameTmux()
        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            base = {"objective": "Exercise the existing spawn contract",
                "command": "stub",
                "session_name": "reserved-name",
                "idempotency_key": "reserved-key",
                "request_id": "request-one",
            }
            first = await ctl.spawn(dict(base), HOST)
            replay = await ctl.spawn({"objective": "Exercise the existing spawn contract", **base, "request_id": "request-two"}, HOST)
            conflict = None
            try:
                await ctl.spawn({"objective": "Exercise the existing spawn contract",
                    **base,
                    "idempotency_key": "foreign-key",
                    "request_id": "request-three",
                }, HOST)
            except VerbError as exc:
                conflict = exc
            return first, replay, conflict, tmux
        finally:
            store.stop()

    first, replay, conflict, tmux = asyncio.run(go())
    assert first["stream_id"] == f"{HOST}:reserved-name"
    assert replay["stream_id"] == first["stream_id"]
    assert replay.get("replayed") is True
    assert tmux.created == 1
    assert isinstance(conflict, VerbError)
    assert conflict.code == "stream_id_unavailable"


def test_in_flight_same_key_replays_starting_without_a_second_pane() -> None:
    """AC2 in-flight branch: a same-key fire arriving while the first is still
    in flight (reservation held, no terminal outcome yet) replays starting and
    creates no pane."""

    async def go():
        tmux = PerNameTmux()
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            # Simulate the winner holding an in-flight reservation for key kF.
            claim = await store.atomic_claim_or_replay(
                HOST, "winner", idempotency_key="kF", request_payload_hash="",
                ttl_s=60.0, request_id="rw",
            )
            assert claim["status"] == "claimed"
            reply = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kF", "request_id": "r2"}, HOST
            )
            return reply, tmux
        finally:
            store.stop()

    reply, tmux = asyncio.run(go())
    assert reply["type"] == "spawn.ok"
    assert reply["state"] == "starting"
    assert reply["stream_id"] == f"{HOST}:winner"
    assert tmux.created == 0
    assert reply.get("admitted_count") == 1


def test_expired_retained_intent_still_replays_same_key() -> None:
    """A pane/intent reservation is TTL-exempt and remains authoritative.

    The live canary reached the 180-second manifest deadline at the same instant
    as the 180-second reservation TTL. A same-key retry must still replay that
    retained claim rather than mint a second session name.
    """

    async def go():
        store = Store(":memory:")
        store.start()
        try:
            first = await store.atomic_claim_or_replay(
                HOST,
                "winner",
                idempotency_key="retained-key",
                request_payload_hash="same-payload",
                ttl_s=60.0,
                request_id="original-request",
            )
            assert first["status"] == "claimed"
            await store.record_spawn_intent(
                HOST,
                "winner",
                {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": "first durable prompt"},
            )
            await store.mark_tmux_created(HOST, "winner")
            await store.submit(
                lambda conn: conn.execute(
                    "UPDATE v2_stream_reservations SET expires_at=0 "
                    "WHERE host=? AND session_name=?",
                    (HOST, "winner"),
                ).connection.commit()
            )
            replay = await store.atomic_claim_or_replay(
                HOST,
                "duplicate",
                idempotency_key="retained-key",
                request_payload_hash="same-payload",
                ttl_s=60.0,
                request_id="retry-request",
            )
            reservations = await store.reservations(include_expired=True)
            return replay, reservations
        finally:
            store.stop()

    replay, reservations = asyncio.run(go())
    assert replay["status"] == "replay"
    assert replay["kind"] == "in_flight"
    assert replay["row"]["session_name"] == "winner"
    assert [row["session_name"] for row in reservations] == ["winner"]


def test_expired_empty_claim_does_not_block_new_same_key_claim() -> None:
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            first = await store.atomic_claim_or_replay(
                HOST,
                "empty-stale",
                idempotency_key="empty-key",
                request_payload_hash="same-payload",
                ttl_s=60.0,
            )
            assert first["status"] == "claimed"
            await store.submit(
                lambda conn: conn.execute(
                    "UPDATE v2_stream_reservations SET expires_at=0 "
                    "WHERE host=? AND session_name=?",
                    (HOST, "empty-stale"),
                ).connection.commit()
            )
            replacement = await store.atomic_claim_or_replay(
                HOST,
                "replacement",
                idempotency_key="empty-key",
                request_payload_hash="same-payload",
                ttl_s=60.0,
            )
            reservations = await store.reservations(include_expired=True)
            return replacement, reservations
        finally:
            store.stop()

    replacement, reservations = asyncio.run(go())
    assert replacement["status"] == "claimed"
    assert [row["session_name"] for row in reservations] == ["replacement"]


def test_terminal_failed_same_key_replays_failed_not_pending() -> None:
    """test contract case 3 / Outcome Matrix class B: a keyed terminal FAILED outcome is
    replayed as a truthful failure, never as pending, and never a new pane."""

    async def go():
        tmux = PerNameTmux()
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            await store.set_spawn_outcome(
                HOST, "deadname", "failed", request_id="rf", reason="boot_not_ready: nope",
                idempotency_key="kG", request_payload_hash="",
                delivery_receipt={"state": "failed"},
            )
            err = None
            try:
                await ctl.spawn({"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kG", "request_id": "r2"}, HOST)
            except VerbError as exc:
                err = exc
            return err, tmux
        finally:
            store.stop()

    err, tmux = asyncio.run(go())
    assert isinstance(err, VerbError)
    assert err.code == "boot_not_ready"  # replayed the recorded failure code
    assert getattr(err, "extra", {}).get("replayed") is True
    assert tmux.created == 0


def test_reconciler_terminal_failure_preserves_same_key_replay_identity() -> None:
    """A terminal outcome written by spawn-intent reconciliation must retain
    the original key and payload hash.  Otherwise the atomic claim sees no
    terminal record and a same-key retry can mint a new pane after a daemon
    restart.
    """

    async def go():
        tmux = PerNameTmux()  # no live entry => explicit ``gone`` observation
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            msg = {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "k-reconciled", "request_id": "r-original"}
            payload_hash = ctl._spawn_payload_hash(msg)
            assert await store.reserve_stream_id(
                HOST, "reconciled-gone", ttl_s=60.0, request_id="r-original",
                idempotency_key="k-reconciled", request_payload_hash=payload_hash,
            )
            await store.record_spawn_intent(HOST, "reconciled-gone", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
            reconciled = await ctl.reconcile_spawn_intents()
            outcome = await store.get_spawn_outcome(HOST, "reconciled-gone")
            replay_error = None
            try:
                await ctl.spawn({"objective": "Exercise the existing spawn contract", **msg, "request_id": "r-retry"}, HOST)
            except VerbError as exc:
                replay_error = exc
            return reconciled, outcome, replay_error, tmux
        finally:
            store.stop()

    reconciled, outcome, replay_error, tmux = asyncio.run(go())
    assert reconciled == {"adopted": 0, "released": 1}
    assert outcome is not None
    assert outcome["idempotency_key"] == "k-reconciled"
    assert outcome["request_payload_hash"]
    assert isinstance(replay_error, VerbError)
    assert replay_error.code == "spawn_interrupted"
    assert replay_error.extra.get("replayed") is True
    assert tmux.created == 0, "same-key replay after reconciliation must not create a pane"


def test_delivered_outcome_with_purged_row_replays_starting_not_failure() -> None:
    """Retention purges `sessions` rows while `v2_spawn_outcomes` persists longer
    (shadow's own contract case). A same-key replay against a DELIVERED outcome whose
        row is gone returns the durable starting handle, NOT fabricate a failure and NOT mint a
    second pane."""

    async def go():
        tmux = PerNameTmux()
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            # A delivered outcome keyed kP, but no sessions row (purged).
            await store.set_spawn_outcome(
                HOST, "gonerow", "delivered", request_id="rp", reason="delivered",
                idempotency_key="kP", request_payload_hash="",
                delivery_receipt={"state": "delivered"},
            )
            err = None
            reply = None
            try:
                reply = await ctl.spawn(
                    {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kP", "request_id": "r2"}, HOST
                )
            except VerbError as exc:
                err = exc
            return reply, err, tmux
        finally:
            store.stop()

    reply, err, tmux = asyncio.run(go())
    assert err is None, f"delivered-but-purged must not raise a failure: {err}"
    assert reply["type"] == "spawn.ok"
    assert reply["state"] == "starting"
    assert reply["stream_id"] == f"{HOST}:gonerow"
    assert tmux.created == 0


def test_delayed_same_key_fire_after_success_noops_via_persisted_outcome() -> None:
    """The atomic claim must hold across TIME, not just across a burst (Multi-fire
    #5: fires arrive late, ~30s apart). After a spawn SUCCEEDS its reservation is
    RELEASED — so a same-key fire arriving 30-60s later dedupes on the persisted
    keyed DELIVERED outcome, not the reservation. It must no-op and replay the
    existing session."""

    async def go():
        tmux = PerNameTmux()
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            first = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kL", "request_id": "r1"}, HOST
            )
            # The success path releases the reservation: prove the key is NOT held
            # by any live reservation, so the late fire can only dedupe on the
            # persisted outcome.
            res_for_key = await store.submit(
                lambda c: c.execute(
                    "SELECT COUNT(*) FROM v2_stream_reservations WHERE idempotency_key=?",
                    ("kL",),
                ).fetchone()[0]
            )
            # A later same-key fire (a fresh request_id, as a real late re-issue
            # would carry) must replay the first session, not spawn a second.
            second = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "command": "stub", "idempotency_key": "kL", "request_id": "r2-late"}, HOST
            )
            return first, second, res_for_key, tmux
        finally:
            store.stop()

    first, second, res_for_key, tmux = asyncio.run(go())
    assert res_for_key == 0, "success must release the reservation (dedupe survives on the outcome)"
    assert first["type"] == "spawn.ok"
    assert second["type"] == "spawn.ok"
    assert second.get("replayed") is True
    assert second["stream_id"] == first["stream_id"]
    assert second.get("admitted_count") == 1
    assert tmux.created == 1, "the late same-key fire must NOT create a second pane"
