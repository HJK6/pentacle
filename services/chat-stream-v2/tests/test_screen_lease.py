"""Screen lease contract through the real SQLite store and wire handler."""

import asyncio
import pytest
from store import Store
from screen_lease import ScreenLease
from test_external_work import Rig
from test_nudges import HOST

DESK = f"{HOST}:desk"
OTHER = f"{HOST}:other"


def exercise(check, path=":memory:"):
    async def run():
        store = Store(str(path))
        store.start()
        try:
            rig = Rig(store)
            await rig.seat("desk")
            await rig.seat("other", bind=False)
            lease = ScreenLease(
                store,
                ["test", "peer"],
                env_binding=lambda: rig.binding,
                clock=lambda: rig.now,
            )

            async def call(verb, actor=DESK, **kw):
                return await lease.handle(
                    {
                        "type": "screen.lease." + verb,
                        "_auth_context": {"stream_id": actor, "token_verified": True},
                        "stream_token": rig.tokens.get(actor),
                        **kw,
                    }
                )

            await check(rig, lease, call)
        finally:
            store.stop()

    asyncio.run(run())


def test_journey_contention_idempotence_and_release():
    async def check(r, lease, call):
        a = await call("acquire", host=" TEST ", reason="first")
        assert a["type"] == "screen.lease.acquire.ok"
        assert (
            a["lease_id"].startswith("lease-")
            and a["expires_at"] == a["acquired_at"] + 900
        )
        shown = await call("status", host="test")
        assert shown["hosts"][0]["lease"]["lease_id"] == a["lease_id"]
        for _ in range(2):
            b = await call("acquire", actor=OTHER, host="test", reason="second")
            assert b["error_code"] == "screen_held" and b["held_by"] == DESK
        rows = await r.rows("v2_outbound_notices")
        assert len(rows) == 1 and rows[0]["kind"] == "screen_lease_contended"
        assert rows[0]["recipient_stream_id"] == DESK
        r.now += 10
        same = await call("acquire", host="test", reason="again")
        assert same["lease_id"] == a["lease_id"] and same["renewed"] is True
        assert same["expires_at"] == r.now + 900
        assert (await call("renew", lease_id=a["lease_id"], force=True))[
            "error_code"
        ] == "invalid_request"
        for verb in ["renew", "release"]:
            assert (await call(verb, actor=OTHER, lease_id=a["lease_id"]))[
                "error_code"
            ] == "screen_lease_not_held"
        assert (await call("renew", lease_id=a["lease_id"], ttl=3600))[
            "expires_at"
        ] == r.now + 3600
        assert (await call("release", lease_id=a["lease_id"]))[
            "release_reason"
        ] == "released"
        assert (await call("release", lease_id=a["lease_id"]))["state"] == "released"
        assert (await call("release", host="test"))["state"] == "free"
        assert (await call("release", lease_id="missing"))["state"] == "unknown"
        assert (await call("acquire", actor=OTHER, host="test", reason="second"))[
            "type"
        ].endswith(".ok")

    exercise(check)


def test_expiry_force_close_and_roster():
    async def check(r, lease, call):
        a = await call("acquire", actor=OTHER, host="test", reason="use", ttl=1)
        denied = await call("release", actor=OTHER, lease_id=a["lease_id"], force=True)
        assert denied["error_code"] == "screen_force_unauthorized"
        forced = await call("release", lease_id=a["lease_id"], force=True)
        assert forced["release_reason"] == "forced" and forced["released_by"] == DESK
        assert (await r.rows("v2_outbound_notices"))[0][
            "kind"
        ] == "screen_lease_revoked"
        for verb in ["renew", "release"]:
            a = await call("acquire", host="test", reason="use", ttl=1)
            r.now += 1
            assert (await call("status", host="test"))["hosts"][0] == {
                "host": "test",
                "state": "free",
                "lease": None,
            }
            assert (await call(verb, lease_id=a["lease_id"]))["state"] == "expired"
        a = await call("acquire", host="test", reason="use", ttl=1)
        r.now += 1
        b = await call("acquire", host="test", reason="new")
        assert b["lease_id"] != a["lease_id"]
        assert (await r.rows("v2_screen_leases"))[-2]["release_reason"] == "expired"
        await r.store.mark_closed(
            HOST, "desk", closed_at="2030-01-01T00:00:00Z", pane_status="pane_dead"
        )
        assert (await r.rows("v2_screen_leases"))[-1][
            "release_reason"
        ] == "holder_closed"
        assert (await call("acquire", actor=OTHER, host="test", reason="next"))[
            "type"
        ].endswith(".ok")
        assert [h["host"] for h in (await call("status"))["hosts"]] == ["test", "peer"]
        for verb in ["acquire", "status", "renew", "release"]:
            assert (await call(verb, host="missing", reason="x"))[
                "error_code"
            ] == "screen_host_invalid"
        for ttl in [0, 3601, -1, True, "no", float("nan")]:
            for verb in ["acquire", "renew"]:
                assert (
                    await call(
                        verb,
                        actor=OTHER,
                        host="peer",
                        reason="x",
                        lease_id=b["lease_id"],
                        ttl=ttl,
                    )
                )["error_code"] == "screen_ttl_invalid"
        assert (await lease.handle({"type": "screen.lease.status"}))["type"].endswith(
            ".ok"
        )
        assert (await lease.handle({"type": "screen.lease.acquire", "host": "test"}))[
            "error_code"
        ] == "not_authenticated"

    exercise(check)


def test_concurrent_acquire_and_partial_index():
    async def check(r, lease, call):
        results = await asyncio.gather(
            call("acquire", host="test", reason="a"),
            call("acquire", actor=OTHER, host="test", reason="b"),
        )
        assert sum(x["type"].endswith(".ok") for x in results) == 1
        assert [x["error_code"] for x in results if "error_code" in x] == [
            "screen_held"
        ]
        import sqlite3

        def duplicate(c):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO v2_screen_leases SELECT 'duplicate',host,holder_stream_id,holder_label,reason,acquired_at,expires_at,renewed_at,released_at,release_reason,released_by FROM v2_screen_leases"
                )
            c.rollback()

        await r.store.submit(duplicate)

    exercise(check)


def test_persistence(tmp_path):
    path = tmp_path / "sessions.db"
    captured = {}

    async def create(r, l, call):
        captured.update(await call("acquire", host="test", reason="persistent"))

    exercise(create, path)

    async def read(r, l, call):
        lease = (await call("status", host="test"))["hosts"][0]["lease"]
        assert (
            lease["lease_id"] == captured["lease_id"]
            and lease["expires_at"] == captured["expires_at"]
        )

    exercise(read, path)


@pytest.mark.parametrize("kind", ["normal", "reaped"])
def test_close_is_atomic_and_generation_safe(kind):
    async def check(r, lease, call):
        a = await call("acquire", host="test", reason="close test")
        seat = await r.store.fetch_session(HOST, "desk")
        # A delayed close of a replaced generation cannot release its screen.
        assert (
            await r.store.mark_closed(
                HOST,
                "desk",
                closed_at="2030-01-01T00:00:00Z",
                pane_status="pane_dead",
                expected_generation="stale",
            )
            is None
        )
        assert (await call("status", host="test"))["hosts"][0]["state"] == "held"
        if kind == "normal":
            await r.store.mark_closed(
                HOST,
                "desk",
                closed_at="2030-01-01T00:00:00Z",
                pane_status="pane_dead",
                expected_generation=seat["session_generation"],
            )
        else:
            await r.store.mark_reconciled_dead(
                HOST,
                "desk",
                closed_at="2030-01-01T00:00:00Z",
                presumed_dead_at="2030-01-01T00:00:00Z",
                expected_generation=seat["session_generation"],
            )
        assert (await r.rows("v2_screen_leases"))[0][
            "release_reason"
        ] == "holder_closed"
        assert (await call("acquire", actor=OTHER, host="test", reason="next"))[
            "type"
        ].endswith(".ok")

    exercise(check)


def test_public_remote_dispatch_and_notice_delivery():
    import json
    from server import Server

    class Remote:
        remote_address = ("192.0.2.1", 12345)

    async def check(r, lease, call):
        server = Server(
            store=r.store, sessions=r.h.sessions, comms=r.h.comms, local_host=HOST
        )
        server.handlers.update(lease.wire_handlers())
        response = await server._dispatch(
            json.dumps({"type": "screen.lease.status", "request_id": "public-read"}),
            websocket=Remote(),
        )
        assert (
            response[0]["type"] == "screen.lease.status.ok"
            and response[0]["request_id"] == "public-read"
        )
        denied = await server._dispatch(
            json.dumps(
                {
                    "type": "screen.lease.acquire",
                    "host": "test",
                    "reason": "spoof",
                    "_auth_context": {
                        "token_verified": True,
                        "stream_id": DESK,
                    },
                }
            ),
            websocket=Remote(),
        )
        assert denied[0]["error_code"] == "authentication_required"
        a = await call("acquire", host="test", reason="holder")
        await call("acquire", actor=OTHER, host="test", reason="requester")
        rows = await r.rows("v2_outbound_notices")
        # The fake pane has no native provider transcript producer: paste is
        # observable, while the outbox correctly retains pending proof.
        await r.outbound.deliver_now(rows[0]["notice_id"])
        assert "[screen_lease_contended]" in r.h.tmux.pasted[-1]
        await call("release", lease_id=a["lease_id"])
        b = await call("acquire", actor=OTHER, host="test", reason="use")
        await call("release", lease_id=b["lease_id"], force=True)
        rows = await r.rows("v2_outbound_notices")
        await r.outbound.deliver_now(rows[-1]["notice_id"])
        assert r.h.tmux.pasted_by_name[-1][0] == "other"
        assert "[screen_lease_revoked]" in r.h.tmux.pasted[-1]

    exercise(check)
