"""The interruption settle's own exits, and the ordinary failure write's exit.

Journey: public spawns held at named boundaries and drained by the real
`drain_background_spawns`, then a distinct (or the same) controller reconciles,
reads `spawn_status` and refires with the same key.
Subject: `Store.settle_spawn_claim` (one write transaction, the two tombstone
dispositions, the bounded cancellation lookup and its partial index), the
`_spawn_impl` failure-write flag, and the claim wrapper's settle for a nonce it
never acquired.
Faults: a drain before, during and after the ordinary failed-outcome write and
at its publish; a cross-name tombstone over an owned empty claim; a Store
error in local cancellation cleanup; a denied native COMMIT; unrelated
tombstones; a conflicting, refused or failing claim.
Runner: Python 3.13 and pytest, the real Store worker, a per-name tmux fake.
Barriers only: no sleeps and no elapsed-time thresholds; `asyncio.wait_for`
limits are stuck detectors.
"""

from __future__ import annotations

import asyncio
import itertools
import sqlite3
from typing import Any

import pytest

from spawnctl import VerbError
from store import Store
from tests.test_spawn_intent_cancel_settle import (
    HOST, KEY, PAYLOAD, Hold, HostTmux, WorkerHold, drain_held_spawn, new_ctl, open_rows,
    refire_then_replay, reservations, rows, spawn_msg, status, stuck,
)

SEAT = "seat"
FAILURE = "synthetic_failure: ordinary error after the intent"


def seat_msg(request_id: str, key: str = KEY) -> dict[str, Any]:
    return {**spawn_msg(request_id), "idempotency_key": key, "session_name": SEAT}


def run(go) -> None:
    async def main():
        store = Store(":memory:")
        store.start()
        try:
            await go(store)
        finally:
            store.stop()
    asyncio.run(main())


async def outcomes(store: Store) -> list[dict[str, Any]]:
    return await rows(store, "SELECT state, reason, request_id, session_name FROM v2_spawn_outcomes")


async def tombstones(store: Store) -> list[dict[str, Any]]:
    return await rows(store, "SELECT host, session_name, request_id, state FROM v2_spawn_cancellations")


async def seed_row(store: Store, *, name: str = SEAT, request_id: str = "req", nonce: str | None = "n-a",
                   owner: str | None = "daemon-A", payload: str | None = None,
                   tmux_created: int = 0, pane_pid: int | None = None) -> None:
    def op(conn):
        conn.execute(
            "INSERT INTO v2_stream_reservations (host, session_name, request_id, expires_at, payload, "
            "tmux_created, nonce, owner_instance_id, pane_pid) VALUES (?,?,?,?,?,?,?,?,?)",
            (HOST, name, request_id, 9e12, payload, tmux_created, nonce, owner, pane_pid),
        )
        conn.commit()
    await store.submit(op)


async def seed_tombstone(store: Store, *, name: str, request_id: str) -> None:
    await store.submit(lambda c: (c.execute(
        "INSERT INTO v2_spawn_cancellations (host, session_name, request_id, state, reason, updated_at) "
        "VALUES (?,?,?,'cancelled','cancelled_before_bind',1)", (HOST, name, request_id)), c.commit()))


def settle(store: Store, *, nonce: str = "n-a", owner: str = "daemon-A", hand_off: bool = False):
    return store.settle_spawn_claim(HOST, SEAT, request_id="req", nonce=nonce,
                                    owner_instance_id=owner, hand_off_cancelled=hand_off)


async def disposition(call) -> tuple[str, Any]:
    try:
        reply = await stuck(call)
    except VerbError as exc:
        return ("error", exc.code)
    return ("reply", reply.get("type"), reply.get("state"))


def record_settles(store: Store) -> list[str]:
    """The result of every settle the code under test submits."""
    results: list[str] = []
    real = getattr(store, "settle_spawn_claim", None)
    if real is None:  # the base daemon, where the exit tests also run for comparison
        return results

    async def wrapper(*a, **kw):
        result = await real(*a, **kw)
        results.append(result)
        return result

    store.settle_spawn_claim = wrapper
    return results


# -- Exit 3: a drain at or after the ordinary failure write ---------------------


def fail_first_fenced(ctl) -> None:
    """The first protected spawn body raises an ordinary error; later ones run."""
    real = ctl._spawn_fenced
    state = {"failed": False}

    async def fenced(*a, **kw):
        if not state["failed"]:
            state["failed"] = True
            raise VerbError("synthetic_failure", "ordinary error after the intent")
        return await real(*a, **kw)

    ctl._spawn_fenced = fenced  # type: ignore[method-assign]


def fail_after_admission(ctl) -> None:
    """The protected body admits the session row, then fails ordinarily."""
    async def marker(*_a, **_k):
        raise VerbError("synthetic_post_admission", "fails after the session row is admitted")
    ctl._await_marker = marker  # type: ignore[method-assign]


def hold_failed_write(store: Store, ctl, hold: Hold, boundary: str) -> None:
    """Hold the ordinary failed-outcome write, or the publish after it."""
    if boundary == "publish":
        real_publish = ctl._publish_spawn_state

        async def publish(host, name, state, **kw):
            if state == "failed":
                ctl._publish_spawn_state = real_publish
                await hold.wait()
            return await real_publish(host, name, state, **kw)

        ctl._publish_spawn_state = publish  # type: ignore[method-assign]
        return
    real = store.set_spawn_outcome

    async def wrapper(host, name, state, **kw):
        if state != "failed":
            return await real(host, name, state, **kw)
        store.set_spawn_outcome = real
        if boundary == "pre-submission":
            await hold.wait()
            return await real(host, name, state, **kw)
        result = await real(host, name, state, **kw)
        hold.observed["written"] = result
        await hold.wait()
        return result

    store.set_spawn_outcome = wrapper


async def assert_failed_and_free(store: Store, ctl, tmux: HostTmux, reason: str) -> None:
    assert await reservations(store) == []
    assert [(o["state"], o["reason"]) for o in await outcomes(store)] == [("failed", reason)]
    found = await status(ctl, "orig")
    assert found["found"] is True and found["reservations"] == []
    assert [(o["request_id"], o["state"]) for o in found["outcomes"]] == [("orig", "failed")]
    assert await stuck(new_ctl(store, tmux, "daemon-B").reconcile_spawn_intents()) == {
        "adopted": 0, "released": 0}


@pytest.mark.parametrize("boundary", ["pre-submission", "post-commit", "publish"])
def test_drain_at_the_failure_write_takes_the_base_teardown(boundary: str) -> None:
    async def go(store):
        tmux = HostTmux()
        ctl = new_ctl(store, tmux, "daemon-A")
        fail_first_fenced(ctl)
        settles = record_settles(store)
        hold = Hold()
        hold_failed_write(store, ctl, hold, boundary)
        await drain_held_spawn(ctl, hold, spawn_msg("orig"))
        assert settles == [] and tmux.created == 0
        if boundary == "pre-submission":
            # Base: the write was never submitted and the request-only release ran.
            assert await reservations(store) == [] and await outcomes(store) == []
            assert (await status(ctl, "orig"))["found"] is False
        else:
            await assert_failed_and_free(store, ctl, tmux, FAILURE)
        await refire_then_replay(store, tmux, new_ctl(store, tmux, "daemon-R"))
    run(go)


def test_drain_with_the_failure_write_queued_is_decided_by_the_ordered_store() -> None:
    async def go(store):
        tmux = HostTmux()
        ctl = new_ctl(store, tmux, "daemon-A")
        fail_first_fenced(ctl)
        settles = record_settles(store)
        worker = WorkerHold()
        queued, waiter_cancelled = asyncio.Event(), asyncio.Event()
        real = store.set_spawn_outcome

        async def wrapper(host, name, state, **kw):
            if state != "failed":
                return await real(host, name, state, **kw)
            store.set_spawn_outcome = real
            await worker.start(store)
            asyncio.get_running_loop().call_soon(queued.set)
            try:
                return await real(host, name, state, **kw)
            except asyncio.CancelledError:
                waiter_cancelled.set()
                raise

        store.set_spawn_outcome = wrapper
        try:
            client = asyncio.create_task(ctl.spawn(spawn_msg("orig"), HOST))
            await stuck(queued.wait())
            drain = asyncio.create_task(ctl.drain_background_spawns())
            await stuck(waiter_cancelled.wait())
            await worker.release()
            assert await stuck(drain) == {"cancelled": 1, "unfinished": 0}
            client.cancel()
            await asyncio.gather(client, return_exceptions=True)
            # The Store ran the queued write after its waiter was cancelled.
            assert settles == []
            await assert_failed_and_free(store, ctl, tmux, FAILURE)
            await refire_then_replay(store, tmux, new_ctl(store, tmux, "daemon-R"))
        finally:
            await worker.release()
    run(go)


def test_admitted_open_row_drained_after_the_failure_commit_matches_the_base() -> None:
    """The base's order is outcome, publish, close: a drain after the commit
    leaves the admitted row open and the name free of reservations; no
    immediate readiness is promised while that row is open."""
    async def go(store):
        tmux = HostTmux()
        ctl = new_ctl(store, tmux, "daemon-A")
        fail_after_admission(ctl)
        settles = record_settles(store)
        hold = Hold()
        hold_failed_write(store, ctl, hold, "post-commit")
        await drain_held_spawn(ctl, hold, seat_msg("orig"))
        assert settles == []
        assert tmux.created == 1 and not tmux.live
        assert await reservations(store) == []
        assert [(o["state"], o["reason"]) for o in await outcomes(store)] == [
            ("failed", "synthetic_post_admission: fails after the session row is admitted")]
        assert [f"{r['host']}:{r['session_name']}" for r in await open_rows(store)] == [f"{HOST}:{SEAT}"]
    run(go)


# -- Tombstone disposition 1: an owned empty unbound claim is released ----------


def test_cross_name_tombstone_over_a_drained_empty_claim_frees_the_name() -> None:
    async def go(store):
        tmux = HostTmux()
        ctl = new_ctl(store, tmux, "daemon-A")
        settles = record_settles(store)
        hold = Hold()
        real = store.record_spawn_intent

        async def held_intent(*a, **kw):
            store.record_spawn_intent = real
            await hold.wait()
            return await real(*a, **kw)

        store.record_spawn_intent = held_intent
        client = asyncio.create_task(ctl.spawn(seat_msg("orig"), HOST))
        await stuck(hold.reached.wait())
        [claim] = await reservations(store)
        assert claim["payload"] is None and claim["request_id"] == "orig"
        # A cancellation of this request recorded under another name (a cross-
        # name or legacy record) coexists with the claim; same-name cancel
        # would have deleted the empty claim in its own transaction.
        await seed_tombstone(store, name="elsewhere", request_id="orig")
        assert await stuck(ctl.drain_background_spawns()) == {"cancelled": 1, "unfinished": 0}
        assert hold.cancelled
        client.cancel()
        await asyncio.gather(client, return_exceptions=True)
        assert settles == ["released"]
        assert await reservations(store) == []
        assert await tombstones(store) == [
            {"host": HOST, "session_name": "elsewhere", "request_id": "orig", "state": "cancelled"}]
        assert await store.spawn_cancelled(HOST, SEAT, "orig") is True
        assert tmux.created == 0
        # The cancelled request is still refused, with no pane and no row.
        refused = await disposition(new_ctl(store, tmux, "daemon-B").spawn(seat_msg("orig", key="k2"), HOST))
        assert refused == ("error", "stream_id_unavailable")
        assert await reservations(store) == [] and tmux.created == 0
        # A different request takes the freed name at once.
        fresh = await stuck(new_ctl(store, tmux, "daemon-B").spawn(seat_msg("fresh", key="k3"), HOST))
        assert fresh["type"] == "spawn.ok" and fresh["stream_id"] == f"{HOST}:{SEAT}"
        assert tmux.created == 1
    run(go)


@pytest.mark.parametrize("case", ["own", "other-owner", "other-nonce", "pid-bound", "tmux-bound", "legacy-nonce"])
def test_tombstoned_empty_claim_is_released_only_when_owned_and_unbound(case: str) -> None:
    async def go(store):
        await seed_row(store, owner="daemon-B" if case == "other-owner" else "daemon-A",
                       nonce="n-z" if case == "other-nonce" else None if case == "legacy-nonce" else "n-a",
                       pane_pid=4242 if case == "pid-bound" else None,
                       tmux_created=int(case == "tmux-bound"))
        await seed_tombstone(store, name="elsewhere", request_id="req")
        before, marks = await reservations(store), await tombstones(store)
        nonce = "" if case == "legacy-nonce" else "n-a"
        result = await settle(store, nonce=nonce)
        assert await tombstones(store) == marks
        if case == "own":
            assert result == "released" and await reservations(store) == []
            assert await store.reserve_stream_id(HOST, SEAT, ttl_s=100, request_id="fresh",
                                                 nonce="n-f", owner_instance_id="daemon-C")
        else:
            assert result == "untouched" and await reservations(store) == before
    run(go)


# -- Tombstone disposition 2: an acknowledged cancelled payload is handed off --


@pytest.mark.parametrize("hand_off", [False, True], ids=["no-handoff", "handoff"])
def test_tombstoned_payload_owner_is_cleared_only_on_a_handoff_exit(hand_off: bool) -> None:
    async def go(store):
        await seed_row(store, payload='{"open_fields":{}}', pane_pid=4242, tmux_created=1)
        await seed_tombstone(store, name=SEAT, request_id="req")
        [before], marks = await reservations(store), await tombstones(store)
        assert await settle(store, hand_off=hand_off) == ("retained" if hand_off else "untouched")
        [after] = await reservations(store)
        assert after == {**before, "owner_instance_id": None if hand_off else "daemon-A"}
        assert await tombstones(store) == marks
        # Another generation's row is never handed off.
        assert await settle(store, nonce="n-z", hand_off=True) == "untouched"
        assert await settle(store, owner="daemon-Z", hand_off=True) == "untouched"
    run(go)


def test_store_error_in_cancelled_cleanup_hands_off_and_the_same_controller_cleans_up() -> None:
    async def go(store):
        tmux = HostTmux()
        ctl = new_ctl(store, tmux, "daemon-A")
        settles = record_settles(store)
        reached, proceed = asyncio.Event(), asyncio.Event()

        async def fenced(*_a, **_k):
            reached.set()
            await proceed.wait()
            raise VerbError("synthetic_failure", "ordinary error after cancellation")

        ctl._spawn_fenced = fenced  # type: ignore[method-assign]
        real_cleanup = ctl._cleanup_cancelled_spawn

        async def failing_cleanup(_res):
            raise sqlite3.OperationalError("store failure in cancellation cleanup")

        ctl._cleanup_cancelled_spawn = failing_cleanup  # type: ignore[method-assign]
        client = asyncio.create_task(ctl.spawn(seat_msg("orig"), HOST))
        await stuck(reached.wait())
        [held] = await reservations(store)
        assert held["payload"] is not None and held["owner_instance_id"] == "daemon-A"
        cancelled = await stuck(ctl.spawn_cancel({"target": "orig"}, HOST))
        assert cancelled.get("state") == "cancelled" or cancelled.get("status") == "cancelled", cancelled
        proceed.set()
        await asyncio.gather(client, return_exceptions=True)
        await stuck(ctl.drain_background_spawns())
        assert settles == ["retained"]
        [handed] = await reservations(store)
        assert handed == {**held, "owner_instance_id": None}
        assert await store.spawn_cancelled(HOST, SEAT, "orig") is True
        # The same controller's next eligible pass confirms the pane gone and
        # consumes the obligation; the tombstone stays.
        ctl._cleanup_cancelled_spawn = real_cleanup  # type: ignore[method-assign]
        assert await stuck(ctl.reconcile_spawn_intents()) == {"adopted": 0, "released": 1}
        assert await reservations(store) == [] and tmux.created == 0
        assert await store.spawn_cancelled(HOST, SEAT, "orig") is True
    run(go)


# -- The claim wrapper's settle for a nonce it never acquired -------------------


async def settled_world(store: Store) -> dict[str, Any]:
    return {
        "reservations": await reservations(store),
        "outcomes": await outcomes(store),
        "sessions": await rows(store, "SELECT host, session_name, status FROM sessions"),
    }


@pytest.mark.parametrize("exit", ["conflict", "refusal", "replay-error"])
def test_claim_wrapper_exit_without_a_row_settles_nothing(exit: str) -> None:
    async def go(store):
        tmux = HostTmux()
        ctl = new_ctl(store, tmux, "daemon-A")
        first = await stuck(ctl.spawn(seat_msg("first"), HOST))
        assert first["type"] == "spawn.ok"
        world = await settled_world(store)
        settles = record_settles(store)
        # The early key lookup misses, as when another caller's claim commits
        # between it and this claim, so the claim itself meets the record.
        async def no_record(*_a, **_k):
            return None
        store.spawn_record_for_key = no_record
        if exit == "conflict":
            msg = {**seat_msg("second"), "objective": "a different payload under the same key"}
        elif exit == "refusal":
            msg = {**PAYLOAD, "request_id": "second", "session_name": SEAT}
        else:
            msg = seat_msg("second")

            async def broken(*_a, **_k):
                raise RuntimeError("replay response could not be built")
            ctl._reply_from_replay = broken  # type: ignore[method-assign]
        try:
            result = await disposition(ctl.spawn(msg, HOST))
        except RuntimeError as exc:
            result = ("raised", str(exc))
        assert result == {
            "conflict": ("error", "idempotency_key_conflict"),
            "refusal": ("error", "stream_id_unavailable"),
            "replay-error": ("raised", "replay response could not be built"),
        }[exit]
        assert settles == ["untouched"]
        assert await settled_world(store) == world and tmux.created == 1
    run(go)


# -- The bounded cancellation lookup and the settle transaction -----------------


def _old_cancelled(conn, host, name, request_id) -> bool:
    return conn.execute(
        "SELECT 1 FROM v2_spawn_cancellations WHERE host=? "
        "AND COALESCE(request_id, '')=? AND (request_id != '' OR session_name=?)",
        (host, request_id, name),
    ).fetchone() is not None or conn.execute(
        "SELECT 1 FROM v2_spawn_outcomes WHERE host=? AND session_name=? "
        "AND COALESCE(request_id, '')=? AND state='cancelled'",
        (host, name, request_id),
    ).fetchone() is not None


def test_bounded_cancellation_lookup_matches_the_old_predicate() -> None:
    """Different hosts, mixed records and a reused name, over every probe."""
    from store import _spawn_cancelled
    hosts, names, requests = ("h1", "h2"), ("n1", "n2"), (None, "", "r1", "r2")
    records = [(h, n, r) for h in hosts for n in names for r in requests]

    async def go(store):
        def check(conn):
            mismatches = probes = 0
            for size in (0, 1, 2, 3):
                for tombs in itertools.islice(itertools.combinations(records, size), 0, None, 7):
                    for outcome in (None, ("h1", "n1", "r1", "cancelled"), ("h1", "n1", None, "cancelled"),
                                    ("h2", "n2", "r2", "failed"), ("h1", "n2", "", "cancelled")):
                        conn.execute("DELETE FROM v2_spawn_cancellations")
                        conn.execute("DELETE FROM v2_spawn_outcomes")
                        for h, n, r in tombs:
                            conn.execute("INSERT OR IGNORE INTO v2_spawn_cancellations (host, session_name, "
                                         "request_id, state, updated_at) VALUES (?,?,?,'cancelled',1)", (h, n, r))
                        if outcome:
                            conn.execute("INSERT INTO v2_spawn_outcomes (host, session_name, request_id, "
                                         "state, updated_at) VALUES (?,?,?,?,1)", outcome)
                        for h, n, r in itertools.product(hosts, names, ("", "r1", "r2")):
                            probes += 1
                            mismatches += _spawn_cancelled(conn, h, n, r) != _old_cancelled(conn, h, n, r)
            conn.rollback()
            return mismatches, probes
        mismatches, probes = await store.submit(check)
        assert mismatches == 0 and probes > 1000
    run(go)


async def _retain(store):
    await seed_row(store, payload='{"open_fields":{}}')
    return settle(store), "retained"


async def _tombstoned_payload(store):
    await seed_row(store, payload='{"open_fields":{}}')
    await seed_tombstone(store, name="elsewhere", request_id="req")
    return settle(store), "untouched"


async def _handoff(store):
    await seed_row(store, payload='{"open_fields":{}}')
    await seed_tombstone(store, name=SEAT, request_id="req")
    return settle(store, hand_off=True), "retained"


async def _tombstoned_empty(store):
    await seed_row(store)
    await seed_tombstone(store, name="elsewhere", request_id="req")
    return settle(store), "released"


CASES = {"retain": _retain, "tombstoned-payload": _tombstoned_payload,
         "handoff": _handoff, "tombstoned-empty": _tombstoned_empty}


@pytest.mark.parametrize("case", list(CASES))
def test_settle_reads_inside_one_transaction_with_keyed_plans(case: str) -> None:
    async def go(store):
        call, expected = await CASES[case](store)
        trace: list[tuple[bool, str]] = []
        await store.submit(lambda c: c.set_trace_callback(lambda sql: trace.append((c.in_transaction, sql))))
        assert await call == expected
        await store.submit(lambda c: c.set_trace_callback(None))
        statements = [(inside, sql) for inside, sql in trace if sql.split()[0] in {"SELECT", "UPDATE", "DELETE"}]
        assert trace[0][1].startswith("BEGIN IMMEDIATE") and statements
        assert all(inside for inside, _ in statements), trace
        assert trace[-1][1] in {"COMMIT", "ROLLBACK"}

        def plans(conn):
            return [row[3] for _, sql in statements for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        details = await store.submit(plans)
        assert not [d for d in details if d.startswith("SCAN ")], details
        assert not [d for d in details if d.endswith("(host=?)")], details
        if case == "tombstoned-payload":
            assert any("v2_spawn_cancellations_request (host=? AND request_id=?)" in d for d in details), details
    run(go)


@pytest.mark.parametrize("case", ["retain", "tombstoned-payload"])
def test_settle_does_equal_work_with_unrelated_tombstones(case: str) -> None:
    def measure(unrelated: int) -> int:
        steps = [0]

        async def go(store):
            call, expected = await CASES[case](store)
            await store.submit(lambda c: (c.executemany(
                "INSERT INTO v2_spawn_cancellations (host, session_name, request_id, state, updated_at) "
                "VALUES (?,?,?,'cancelled',1)",
                [(HOST, f"other{i}", f"unrelated-{i}") for i in range(unrelated)]), c.commit()))

            def count(conn):
                def tick():
                    steps[0] += 1
                    return 0
                conn.set_progress_handler(tick, 1)
            await store.submit(count)
            assert await call == expected
            await store.submit(lambda c: c.set_progress_handler(None, 0))
        run(go)
        return steps[0]

    # From one unrelated record on, the work is constant: no host-range scan.
    assert len({measure(n) for n in (1, 256, 4096)}) == 1


def install_denied_commit():
    """Deny only the first native COMMIT on the Store's connection."""
    events: list[str] = []

    def install(conn):
        def authorize(op, arg1, *_):
            if op == sqlite3.SQLITE_TRANSACTION:
                events.append(arg1)
                if arg1 == "COMMIT" and events.count("COMMIT") == 1:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
    return install, events


@pytest.mark.parametrize("case", ["retain", "tombstoned-empty"])
def test_settle_denied_commit_leaves_durable_state_and_a_clean_connection(tmp_path, case: str) -> None:
    async def main():
        path = tmp_path / "disposable.sqlite"
        store = Store(str(path))
        store.start()
        try:
            call, _expected = await CASES[case](store)
            assert await store.reserve_stream_id(HOST, "other", ttl_s=100, request_id="other",
                                                 nonce="other-nonce", owner_instance_id="daemon-B")
            install, events = install_denied_commit()
            await store.submit(install)
            with pytest.raises(sqlite3.DatabaseError):
                await call
            assert events == ["BEGIN", "COMMIT", "ROLLBACK"]
            assert await store.submit(lambda c: (c.set_authorizer(None), c.in_transaction)[1]) is False
            with sqlite3.connect(path) as reader:
                assert reader.execute("SELECT owner_instance_id FROM v2_stream_reservations "
                                      "WHERE request_id='req'").fetchall() == [("daemon-A",)]
            # The first unrelated writer after the fault succeeds.
            assert await store.record_spawn_intent(HOST, "other", {"open_fields": {}},
                                                   request_id="other", nonce="other-nonce") is True
        finally:
            store.stop()
    asyncio.run(main())


def test_existing_store_gains_the_request_index_on_open(tmp_path) -> None:
    path = str(tmp_path / "existing.sqlite")
    store = Store(path)
    store.start()
    store.stop()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX v2_spawn_cancellations_request")
    store = Store(path)
    store.start()
    store.stop()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='v2_spawn_cancellations_request'"
                            ).fetchone() == ("CREATE INDEX v2_spawn_cancellations_request "
                                             "ON v2_spawn_cancellations (host, request_id) WHERE request_id != ''",)
