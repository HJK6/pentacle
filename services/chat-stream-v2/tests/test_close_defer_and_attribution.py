"""Close guard and attribution for a live synthetic session.

These tests cover two invariants:

1. A `close` verb carrying the client's busy-seat guard refuses a working pane
   with a distinct `close.deferred`
   (not `close.ok`, not a generic `close.failed`) and the row stays open.
2. A close reaching `mark_closed` records caller identity in a durable
   `v2_close_audit` row (actor_kind, closed_by, auth_kind, peer, reason,
   request_id, close_kind, disposition) is written on every audited close and
   surfaced in `inspect`.

Unchanged semantics guarded here: `operator_override` force close still kills a
working pane; the `requires_idle`/`reap` path still fences (reap_fenced), not
defers; self-close and the reconciler sweep still close.
"""

from __future__ import annotations

import asyncio

import pytest

from sessions import Sessions  # noqa: E402
from server import Server  # noqa: E402
from store import Store  # noqa: E402

HOST = "hosta"


class _BaseTmux:
    def __init__(self, *, pid: str = "") -> None:
        self.pid = pid
        self.alive = True
        self.kills = 0

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def pane_pid(self, name: str) -> str:
        return self.pid

    async def pane_identity(self, name: str) -> dict[str, str] | None:
        if not self.pid:
            return None
        return {"pane_pid": self.pid, "pane_id": "%1", "tty": "/dev/ttys001",
                "tmux_socket": "/tmp/tmux-example/default", "session_name": name}

    async def kill_session(self, name: str) -> None:
        self.kills += 1
        self.alive = False


class WorkingTmux(_BaseTmux):
    """Capture parses as a busy Codex/Claude pane (working spinner present)."""

    async def capture_checked(self, _name: str, **_: object) -> tuple[bool, str]:
        return True, "Working (esc to interrupt)\n"


class IdleTmux(_BaseTmux):
    async def capture_checked(self, _name: str, **_: object) -> tuple[bool, str]:
        return True, "ready\n"


def _operator() -> dict:
    return {"operator_authenticated": True, "operator_principal": "operator:TEST",
            "connection_client": "mobile", "transport": "v2"}


async def _build(tmux: _BaseTmux, name: str) -> tuple[Store, Sessions, Server]:
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    server = Server(store=store, sessions=sessions, local_host=HOST)
    await store.open_session(HOST, name, visibility="hidden")
    await sessions.refresh()
    return store, sessions, server


# --- Runtime close generation fence ----------------------------------------- #

def test_operator_close_forwards_expected_generation_and_never_kills_replacement() -> None:
    """A close frame captured before a same-name respawn is CAS-fenced inside
    ``Sessions.close``.  This is deliberately an interleaving at the server
    boundary: without forwarding ``expected_generation`` the handler reads the
    replacement as its target and kills its pane."""
    async def go() -> None:
        tmux = IdleTmux()
        store, sessions, server = await _build(tmux, "generation-race")
        try:
            original = await store.fetch_session(HOST, "generation-race")
            assert original is not None
            replacement = await store.open_session(HOST, "generation-race", visibility="hidden")
            assert replacement["session_generation"] != original["session_generation"]
            await sessions.refresh()

            reply = await server._on_close({
                "_auth_context": _operator(),
                "stream_id": f"{HOST}:generation-race",
                "expected_generation": original["session_generation"],
            })

            assert reply["type"] == "close.already_closed", reply
            assert tmux.kills == 0
            current = await store.fetch_session(HOST, "generation-race")
            assert current is not None
            assert current["status"] == "open"
            assert current["session_generation"] == replacement["session_generation"]
        finally:
            store.stop()

    asyncio.run(go())


def test_hello_advertises_generation_fenced_close_capability() -> None:
    """Authenticated harnesses can fail closed on runtimes predating the CAS."""
    async def go() -> None:
        store, _sessions, server = await _build(IdleTmux(), "capability")
        try:
            frames = await server._on_hello({"subscribe": {"snapshot": True}})
            snapshot = next(frame for frame in frames if frame.get("type") == "snapshot")
            assert snapshot["capabilities"]["close_expected_generation"] is True
        finally:
            store.stop()

    asyncio.run(go())


# --- Working-session guard --------------------------------------------------- #

def test_on_close_defer_if_working_defers_working_seat() -> None:
    """RED on unfixed tree: the working pane is killed and the reply is
    close.ok. The expected result is close.deferred, with the pane and row open."""
    async def go() -> None:
        tmux = WorkingTmux()
        store, sessions, server = await _build(tmux, "busy")
        try:
            reply = await server._on_close({
                "_auth_context": _operator(),
                "stream_id": f"{HOST}:busy",
                "defer_if_working": True,
            })
            assert reply["type"] == "close.deferred", reply
            assert reply.get("ok") is False
            assert reply.get("deferred") is True
            assert tmux.kills == 0
            row = await store.fetch_session(HOST, "busy")
            assert str(row["status"]) == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_on_close_force_override_still_kills_working_seat() -> None:
    """operator_override force close is unaffected by defer_if_working."""
    async def go() -> None:
        tmux = WorkingTmux()
        store, sessions, server = await _build(tmux, "force")
        try:
            reply = await server._on_close({
                "_auth_context": _operator(),
                "stream_id": f"{HOST}:force",
                "defer_if_working": True,
                "operator_override": True,
            })
            assert reply["type"] in ("close.ok", "close.already_closed"), reply
            assert tmux.kills == 1
        finally:
            store.stop()

    asyncio.run(go())


def test_reap_idle_working_pane_still_reap_fenced_not_deferred() -> None:
    """The requires_idle/reap path keeps its reap_fenced refusal (not the new
    deferred disposition)."""
    async def go() -> None:
        tmux = WorkingTmux()
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        try:
            await store.open_session(HOST, "reap", visibility="hidden")
            await sessions.refresh()
            result = await sessions.reap_idle(HOST, "reap")
            assert result["failed"] is True
            assert result.get("fenced") is True
            assert result.get("deferred") is not True
            assert tmux.kills == 0
        finally:
            store.stop()

    asyncio.run(go())


# --- Attribution on every audited close ------------------------------------- #

def test_operator_close_writes_attribution_audit_and_inspect_exposes_it() -> None:
    """RED on unfixed tree: no close_audit exists (no table/method, inspect has
    no close_audit block). Fixed: an audited close records caller identity and
    inspect surfaces closed_by/actor_kind/reason."""
    async def go() -> None:
        tmux = IdleTmux()
        store, sessions, server = await _build(tmux, "idle")
        try:
            reply = await server._on_close({
                "_auth_context": _operator(),
                "stream_id": f"{HOST}:idle",
                "reason": "operator asked",
                "request_id": "req-abc",
            })
            assert reply["type"] == "close.ok", reply

            audit = await store.latest_close_audit(f"{HOST}:idle")
            assert audit is not None
            assert audit["disposition"] == "closed"
            assert audit["close_kind"] == "operator_close"
            assert audit["actor_kind"] == "operator"
            assert audit["auth_kind"] == "operator_authenticated"
            assert audit["reason"] == "operator asked"
            assert audit["request_id"] == "req-abc"
            # closed_by falls back to the operator principal when the operator
            # connection carries no caller stream id.
            assert audit["closed_by"] == "operator:TEST"

            inspect = await server._on_inspect_stream({"stream_id": f"{HOST}:idle"})
            block = inspect.get("close_audit")
            assert isinstance(block, dict)
            assert block["closed_by"] == "operator:TEST"
            assert block["actor_kind"] == "operator"
            assert block["reason"] == "operator asked"
        finally:
            store.stop()

    asyncio.run(go())


def test_deferred_close_writes_deferred_audit_row() -> None:
    """A deferred refusal is durably auditable (disposition=deferred) without a
    second retry queue."""
    async def go() -> None:
        tmux = WorkingTmux()
        store, sessions, server = await _build(tmux, "busy2")
        try:
            await server._on_close({
                "_auth_context": _operator(),
                "stream_id": f"{HOST}:busy2",
                "defer_if_working": True,
            })
            audit = await store.latest_close_audit(f"{HOST}:busy2")
            assert audit is not None
            assert audit["disposition"] == "deferred"
            assert audit["actor_kind"] == "operator"
        finally:
            store.stop()

    asyncio.run(go())


class _PeerHosts:
    """Minimal peer pool: 'hostb' is the one known peer host."""

    local_host = HOST

    def __init__(self, tmux: _BaseTmux) -> None:
        self.tmux = tmux

    def known(self, host: str) -> bool:
        return host == "hostb"

    def is_local(self, host: str) -> bool:
        return host == self.local_host

    async def probe_once(self, host: str) -> bool:
        return host == "hostb"

    def tmux_for(self, host: str) -> object:
        assert host == "hostb"
        return self.tmux


def test_peer_defer_if_working_defers_working_seat() -> None:
    """The peer close arm honors defer_if_working too."""
    async def go() -> None:
        tmux = WorkingTmux()
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, tmux=None, local_host=HOST, hosts=_PeerHosts(tmux))
        try:
            await store.open_session("hostb", "peerbusy", visibility="hidden")
            await sessions.refresh()
            result = await sessions.close(
                "hostb", "peerbusy", "", defer_if_working=True,
            )
            assert result.get("deferred") is True
            assert result.get("failed") is not True
            assert tmux.kills == 0
            row = await store.fetch_session("hostb", "peerbusy")
            assert str(row["status"]) == "open"
            audit = await store.latest_close_audit("hostb:peerbusy")
            assert audit is not None and audit["disposition"] == "deferred"
        finally:
            store.stop()

    asyncio.run(go())


def test_reconciler_death_reap_writes_presumed_dead_audit() -> None:
    """The presumed-dead reconciler path bypasses mark_closed but is still
    attributed (disposition=presumed_dead, actor_kind=reconciler_dead)."""
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            opened = await store.open_session(HOST, "ghost", visibility="hidden")
            row = await store.mark_reconciled_dead(
                HOST, "ghost",
                expected_generation=opened["session_generation"],
                presumed_dead_at="2026-09-07T00:00:00Z",
                closed_at="2026-09-07T00:00:00Z",
            )
            assert row is not None
            audit = await store.latest_close_audit(f"{HOST}:ghost")
            assert audit is not None
            assert audit["disposition"] == "presumed_dead"
            assert audit["actor_kind"] == "reconciler_dead"
        finally:
            store.stop()

    asyncio.run(go())


def test_self_close_still_closes_and_audits_as_self() -> None:
    """Unchanged semantics: a self-close still closes; it now also audits with a
    close_kind-derived actor when no rich attribution is threaded."""
    async def go() -> None:
        tmux = IdleTmux()
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        try:
            await store.open_session(HOST, "selfclose", visibility="hidden")
            await sessions.refresh()
            result = await sessions.close(HOST, "selfclose", "done",
                                          close_kind="session_close")
            assert result.get("failed") is not True
            audit = await store.latest_close_audit(f"{HOST}:selfclose")
            assert audit is not None
            assert audit["disposition"] == "closed"
            assert audit["close_kind"] == "session_close"
            assert audit["actor_kind"] == "self_or_parent"
        finally:
            store.stop()

    asyncio.run(go())
