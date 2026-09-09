"""`role set|get`: post-hoc role on a live seat, privileged authority gate, baseline
delivery through the existing tell path, and role_source projection.

Storage contract this pins: `role` stays on the existing `sessions.role` column
(no migration); the post-hoc marker + actor + timestamp live in the v2-prefixed
aux table `v2_session_role`; `role_source` is derived for spawn/handoff and
stored for role_set. The privileged-role gate is the safety check — granting
the protected role post-hoc changes scheduler authority (window_schedule reads
`sessions.role`)."""

from __future__ import annotations

import asyncio

import pytest

from comms import Comms  # noqa: E402
from sessions import Sessions  # noqa: E402
from server import Server  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402
from sessions import VerbError  # noqa: E402

# `role_source_for` is imported lazily inside the one test that needs it, not at
# module scope: a module-level import of a new production symbol turns the whole
# file's regression RED into a collection ImportError (vacuous), instead of each
# authority / delivery / projection cell failing at its behavioral predicate.

HOST = "hosta"


def _operator() -> dict:
    return {"operator_authenticated": True, "operator_principal": "operator:TEST"}


def _seat(stream_id: str) -> dict:
    return {"token_verified": True, "stream_id": stream_id}


async def _open(store: Store, sessions: Sessions, name: str, **fields) -> None:
    await store.open_session(HOST, name, visibility="hidden", **fields)
    await sessions.refresh()


# -- derivation (migration-safe read: no aux row -> derived) -------------------


def test_role_source_derivation_spawn_handoff_and_stored() -> None:
    from store import role_source_for  # deferred: see module header note

    spawn_row = {"role": "lead"}
    handoff_row = {"role": "nexus", "handoff_from_stream_id": "hosta:old"}
    roleless = {}
    assert role_source_for(spawn_row, None) == "spawn"
    assert role_source_for(handoff_row, None) == "handoff"
    assert role_source_for(roleless, None) is None
    # a stored marker always wins over derivation
    assert role_source_for(spawn_row, "role_set") == "role_set"


# -- store aux round-trip -----------------------------------------------------


def test_store_role_source_round_trip() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session(HOST, "seat", visibility="hidden")
            assert await store.fetch_role_source(HOST, "seat") is None
            await store.set_session_role_source(
                HOST, "seat", role_source="role_set",
                actor="hosta:nexus", changed_at="2026-09-07T00:00:00Z",
            )
            got = await store.fetch_role_source(HOST, "seat")
            assert got is not None
            assert got["role_source"] == "role_set"
            assert got["actor"] == "hosta:nexus"
            assert got["changed_at"] == "2026-09-07T00:00:00Z"
        finally:
            store.stop()

    asyncio.run(run())


# -- non-nexus role: any authenticated caller ---------------------------------


def test_set_non_nexus_role_updates_row_and_records_source() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=HOST)
            await _open(store, sessions, "seat")
            row = await sessions.set_role(
                HOST, "seat", "lead", auth_context=_seat("hosta:someone"),
            )
            assert row["role"] == "lead"
            stored = await store.fetch_role_source(HOST, "seat")
            assert stored["role_source"] == "role_set"
            assert stored["actor"] == "hosta:someone"
            assert stored["changed_at"]
            # the live sessions.role column is what window_schedule authority reads
            persisted = await store.fetch_session(HOST, "seat")
            assert persisted["role"] == "lead"
        finally:
            store.stop()

    asyncio.run(run())


# -- nexus authority matrix ---------------------------------------------------


def test_set_nexus_allowed_for_operator() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=HOST)
            await _open(store, sessions, "seat")
            row = await sessions.set_role(
                HOST, "seat", "nexus", auth_context=_operator(),
            )
            assert row["role"] == "nexus"
        finally:
            store.stop()

    asyncio.run(run())


def test_set_nexus_allowed_for_nexus_seat() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=HOST)
            await _open(store, sessions, "caller", role="nexus")
            await _open(store, sessions, "seat")
            row = await sessions.set_role(
                HOST, "seat", "nexus", auth_context=_seat(f"{HOST}:caller"),
            )
            assert row["role"] == "nexus"
        finally:
            store.stop()

    asyncio.run(run())


def test_set_nexus_allowed_for_parent() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=HOST)
            await _open(store, sessions, "parent")
            await _open(store, sessions, "seat", parent_stream_id=f"{HOST}:parent")
            row = await sessions.set_role(
                HOST, "seat", "nexus", auth_context=_seat(f"{HOST}:parent"),
            )
            assert row["role"] == "nexus"
        finally:
            store.stop()

    asyncio.run(run())


def test_set_nexus_refused_for_unrelated_seat_leaves_row_unchanged() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=HOST)
            await _open(store, sessions, "rando", role="lead")
            await _open(store, sessions, "seat", role="lead")
            with pytest.raises(VerbError) as exc:
                await sessions.set_role(
                    HOST, "seat", "nexus", auth_context=_seat(f"{HOST}:rando"),
                )
            assert exc.value.code == "role_authority_denied"
            persisted = await store.fetch_session(HOST, "seat")
            assert persisted["role"] == "lead"  # unchanged
            assert await store.fetch_role_source(HOST, "seat") is None
        finally:
            store.stop()

    asyncio.run(run())


def test_set_role_refused_for_unverified_caller() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=HOST)
            await _open(store, sessions, "seat")
            with pytest.raises(VerbError) as exc:
                await sessions.set_role(HOST, "seat", "lead", auth_context={})
            assert exc.value.code == "stream_ownership_unverified"
        finally:
            store.stop()

    asyncio.run(run())


# -- baseline delivery through the existing tell path -------------------------


class ScriptedClaude:
    def __init__(self) -> None:
        self.screen = "OpenAI\n❯ \n"
        self.pastes: list[str] = []
        self.alive = True

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def pane_pid(self, name: str) -> str:
        return ""

    async def capture(self, name: str) -> str:
        return self.screen

    async def paste(self, name: str, text: str) -> None:
        self.pastes.append(text)
        self.screen = f"⏺ {text}\n❯ \n"

    async def run(self, *args, **kw) -> tuple[int, str]:
        return 0, ""


def test_baseline_delivered_into_pane_with_stamp() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            tmux = ScriptedClaude()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
            server = Server(store=store, sessions=sessions, comms=comms)
            await sessions.open(HOST, "seat", provider="claude")
            reply = await server._on_role_set({
                "_auth_context": _operator(),
                "stream_id": f"{HOST}:seat",
                "role": "lead",
                "baseline_content": "Lead baseline body.",
            })
            assert reply["type"] == "role.set.ok"
            assert reply["session"]["role"] == "lead"
            assert reply["session"]["role_source"] == "role_set"
            assert reply["role_baseline"] is not None
            assert tmux.pastes, "baseline must be pasted into the pane"
            body = tmux.pastes[0]
            assert body.startswith("[role baseline: lead]")
            assert "Lead baseline body." in body
        finally:
            store.stop()

    asyncio.run(run())


def test_missing_baseline_sets_role_and_reports_null() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            tmux = ScriptedClaude()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
            server = Server(store=store, sessions=sessions, comms=comms)
            await sessions.open(HOST, "seat", provider="claude")
            reply = await server._on_role_set({
                "_auth_context": _operator(),
                "stream_id": f"{HOST}:seat",
                "role": "lead",
                "baseline_content": None,
            })
            assert reply["session"]["role"] == "lead"
            assert reply["role_baseline"] is None
            assert tmux.pastes == []  # nothing delivered
        finally:
            store.stop()

    asyncio.run(run())


# -- get + projection ---------------------------------------------------------


def test_role_get_returns_role_and_source() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=HOST)
            server = Server(store=store, sessions=sessions)
            await _open(store, sessions, "seat")
            await sessions.set_role(HOST, "seat", "qa", auth_context=_seat(f"{HOST}:x"))
            reply = await server._on_role_get({"stream_id": f"{HOST}:seat"})
            assert reply["type"] == "role.get.ok"
            assert reply["role"] == "qa"
            assert reply["role_source"] == "role_set"
            assert reply["role_actor"] == f"{HOST}:x"
            assert reply["role_changed_at"]
        finally:
            store.stop()

    asyncio.run(run())


def test_inspect_and_list_expose_role_source() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=HOST)
            server = Server(store=store, sessions=sessions)
            server.inventory_ready.set()
            # a spawned lead (derived) and a role_set qa
            await store.open_session(HOST, "spawned", visibility="default", role="lead")
            await store.open_session(HOST, "adopted", visibility="default")
            await sessions.refresh()
            await sessions.set_role(HOST, "adopted", "qa", auth_context=_operator())

            inspect = await server._on_inspect_stream({"stream_id": f"{HOST}:spawned"})
            assert inspect["session"]["role_source"] == "spawn"

            listed = await server._on_list_sessions({})
            by_name = {r["session_name"]: r for r in listed["active"]}
            assert by_name["spawned"]["role_source"] == "spawn"
            assert by_name["adopted"]["role_source"] == "role_set"
        finally:
            store.stop()

    asyncio.run(run())


def test_hello_summary_snapshot_exposes_role_source() -> None:
    """`coordination-tool list` consumes the summary hello snapshot, not the
    list_sessions RPC — the projection must ride the snapshot rows too."""
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=HOST)
            server = Server(store=store, sessions=sessions)
            await store.open_session(HOST, "spawned", visibility="default", role="lead")
            await store.open_session(HOST, "adopted", visibility="default")
            await sessions.refresh()
            await sessions.set_role(HOST, "adopted", "qa", auth_context=_operator())

            frames = await server._on_hello(
                {"subscribe": {"events_mode": "summary", "snapshot": True}}
            )
            snap = next(f for f in frames if f.get("type") == "snapshot")
            by_name = {r["session_name"]: r for r in snap["sessions"]}
            assert by_name["spawned"]["role_source"] == "spawn"
            assert by_name["adopted"]["role_source"] == "role_set"
        finally:
            store.stop()

    asyncio.run(run())


def test_reopen_reused_name_clears_stale_role_source() -> None:
    """A closed same-name seat reopened as a fresh spawn must not inherit the
    prior lifecycle's `role_set` aux marker (it would falsely read role_set)."""
    async def run() -> None:
        from store import role_source_for  # deferred: see module header note

        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=HOST)
            await _open(store, sessions, "reused")
            await sessions.set_role(HOST, "reused", "qa", auth_context=_operator())
            assert (await store.fetch_role_source(HOST, "reused"))["role_source"] == "role_set"

            await store.mark_closed(
                HOST, "reused", closed_at="2026-09-07T01:00:00Z", pane_status="pane_dead",
            )
            await store.open_session(HOST, "reused", visibility="hidden", role="lead")

            assert await store.fetch_role_source(HOST, "reused") is None
            row = await store.fetch_session(HOST, "reused")
            assert role_source_for(row, None) == "spawn"
        finally:
            store.stop()

    asyncio.run(run())
