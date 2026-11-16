"""Regression coverage for spawn parameter-fidelity through crash adoption.

`spec-spawn-registration`: a durably
registered session must faithfully reflect every requested spawn parameter
(model/effort, visibility, parent, role, spec tag) or the spawn must fail
truthfully — it must never silently admit a bare, wrong-tuple, unbriefed seat.

This targets `_adopt_interrupted_spawn`'s crash-recovery path, distinct from
the closed durable-handle-gap (which owns rowless strands): here a row IS
durably created, but the fields it carries can be wrong. Every pre-existing
reconciler test in this suite records its intent as `{"open_fields": {}}`,
so none of them exercise whether a POPULATED intent survives adoption, nor
what happens when the persisted intent cannot be read at all.
"""

from __future__ import annotations

import asyncio

from reconciler import SessionReconciler
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store

HOST = "localhost"


class _AlwaysAliveTmux:
    """A pane that is already alive when the reconciler inspects it — the
    live-pane/no-open-row precondition `_reconcile_one_spawn_intent` hands to
    `_adopt_interrupted_spawn`. Rollback is never exercised by these tests."""

    def __init__(self) -> None:
        self.alive = True
        self.nonce = ""
        self.screen = ""

    async def new_session(
        self, _name: str, _command: str, cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.alive = True
        self.nonce = str((env or {}).get("PENTACLE_SPAWN_NONCE") or "")

    async def has_session(self, _name: str) -> bool:
        return self.alive

    async def session_state(self, _name: str) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, _name: str) -> str:
        return self.screen

    async def pane_pid(self, _name: str) -> str:
        return "4242"

    async def kill_session(self, _name: str) -> None:
        self.alive = False

    async def paste(self, _name: str, text: str) -> None:
        self.screen += f"\n{text}"

    async def send_enter(self, _name: str) -> None:
        return None

    async def run(self, *args: str, **_kwargs: object) -> tuple[int, str]:
        if args and args[0] == "show-environment" and self.nonce:
            return 0, f"PENTACLE_SPAWN_NONCE={self.nonce}\n"
        return 0, ""


class LocalHosts:
    local_host = HOST

# The 02:44:59Z contract scenario tuple: non-default model/effort, hidden visibility,
# a parent, a role, and a spec tag.
OPEN_FIELDS = {
    "objective": "Preserve the requested spawn fields",
    "parent_stream_id": f"{HOST}:parent-lead",
    "role": "lead",
    "spec_id": "spec_example__topic",
    "spec_ids": ["spec_example__topic"],
    "visibility": "hidden",
    "requested_model": "claude-opus-4-8",
    "requested_effort": "high",
    "self_close_on_completion": True,
}


def test_adoption_with_a_populated_intent_preserves_every_requested_field() -> None:
    """PASS: a fully-fielded persisted intent survives crash adoption intact.

    FAIL form: any contract scenario-tuple field is silently dropped or defaulted on
    the adopted row.
    """
    async def run() -> dict | None:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _AlwaysAliveTmux()
            tmux.alive = True
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            tmux.nonce = "nonce-fidelity-populated"
            assert await store.reserve_stream_id(
                HOST, "fidelity-populated", ttl_s=60, request_id="r-fidelity-populated",
                nonce=tmux.nonce,
            )
            await store.record_spawn_intent(
                HOST, "fidelity-populated",
                {"open_fields": OPEN_FIELDS, "brief": ""},
            )
            await store.mark_tmux_created(HOST, "fidelity-populated")
            await SessionReconciler(sessions, hosts=LocalHosts(), spawnctl=ctl).reconcile_once()
            return await store.fetch_session(HOST, "fidelity-populated")
        finally:
            store.stop()

    row = asyncio.run(run())
    assert row is not None and row["status"] == "open"
    for key, expected in OPEN_FIELDS.items():
        if key == "spec_ids":
            continue  # stored JSON-encoded; spec_id already covers the assertion
        actual = row[key]
        if isinstance(expected, bool):
            actual = bool(actual)
        assert actual == expected, f"{key}: expected {expected!r}, got {actual!r}"


def test_adoption_with_an_unreadable_intent_never_admits_a_bare_defaulted_row() -> None:
    """PASS: an unreadable persisted intent fails truthfully — no open row
    with defaulted/dropped fields is ever admitted for the caller to mistake
    as a faithful registration.

    FAIL form (the contract scenario shape): the reservation's `payload` cannot be
    read back, yet adoption proceeds anyway and durably registers an OPEN
    session row with every descriptive field silently defaulted to None/
    "default" — a wrong-tuple, unparented, unbriefed seat that looks like a
    normal successful spawn to anyone inspecting the row.
    """
    async def run() -> tuple[dict | None, dict | None]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _AlwaysAliveTmux()
            tmux.alive = True
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            tmux.nonce = "nonce-fidelity-unreadable"
            assert await store.reserve_stream_id(
                HOST, "fidelity-unreadable", ttl_s=60, request_id="r-fidelity-unreadable",
                nonce=tmux.nonce,
            )
            # Simulate a persisted intent that cannot be read back (payload
            # never written / corrupted) while the pane is nonetheless alive
            # — the exact precondition `_reconcile_one_spawn_intent` hands to
            # `_adopt_interrupted_spawn`. No `record_spawn_intent` call: the
            # reservation's `payload` column stays NULL.
            await store.mark_tmux_created(HOST, "fidelity-unreadable")
            await SessionReconciler(sessions, hosts=LocalHosts(), spawnctl=ctl).reconcile_once()
            return (
                await store.fetch_session(HOST, "fidelity-unreadable"),
                await store.get_spawn_outcome(HOST, "fidelity-unreadable"),
            )
        finally:
            store.stop()

    row, outcome = asyncio.run(run())
    # The row must never be left OPEN with defaulted fields impersonating a
    # faithful registration. Either no row was admitted, or it was admitted
    # and immediately, truthfully closed/failed — never a silent success.
    assert not (row is not None and row["status"] == "open"), (
        "adoption silently admitted an OPEN row from an unreadable intent: "
        f"row={row!r}"
    )
    assert outcome is not None and outcome["state"] == "failed"
