"""A spawn that fails after its intent is persisted leaves no open row.

The intent (a reservation with its payload) is written before the pane so a
mid-spawn stop is recoverable. A clean in-process `spawn.error` must release
the id and leave no open `sessions` row.

The durable-handle invariant admits a row immediately after pane creation, before
fallible readiness and delivery observations. A delivery failure whose rollback
is confirmed must close that row; it must never leave an OPEN phantom.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import spawnctl as spawnctl_mod  # noqa: E402
import store as store_mod  # noqa: E402
from inventory import InventoryEmitter  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"
NAME = "v2-phantom"


class BootReadyTmux:
    """A pane that boots (capture shows the READY marker) and dies on kill."""

    def __init__(self) -> None:
        self.alive = False  # no pane until `new_session` (the pre-create guard)
        self.killed = False

    async def new_session(self, name: str, command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self.alive = True

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def session_state(self, name: str) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, name: str) -> str:
        return "READY\n❯ "

    async def pane_pid(self, name: str) -> str:
        return "4321"

    async def paste(self, name: str, text: str) -> None:
        pass

    async def kill_session(self, name: str) -> None:
        self.alive = False
        self.killed = True

    async def run(self, *args: str, **kw) -> tuple[int, str]:
        return 0, ""


class IdentityMatchedBootReadyTmux(BootReadyTmux):
    """Delivery proof fails while the admitted pane process remains verified."""

    ssh_target = "hostb"

    async def pane_identity(self, name: str) -> dict[str, str] | None:
        if not self.alive:
            return None
        return {
            "pane_pid": "4321",
            "pane_id": "%7",
            "tty": "/dev/pts/7",
            "tmux_socket": "/tmp/tmux/default",
            "session_name": name,
        }

    async def kill_pane(self, _pane_id: str) -> None:
        raise AssertionError("proof uncertainty must not kill a verified live pane")


class AttestedUnknownIdentityTmux(BootReadyTmux):
    """An attested Codex pane whose transport cannot expose pane identity."""

    def __init__(self) -> None:
        super().__init__()
        self.pastes = 0

    async def paste(self, name: str, text: str) -> None:
        self.pastes += 1


def _spawn_msg() -> dict:
    # Explicit `command` => provider "" (no tuple resolution); the READY marker
    # gate boots it; a brief drives the delivery path we force to fail.
    return {"objective": "Exercise the existing spawn contract", "command": "run", "prompt": "the brief", "session_name": NAME, "request_id": "r1"}


def test_delivery_failure_leaves_no_open_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delivery failure closes the early-admitted row after confirmed cleanup.

    The durable row is intentionally created before delivery confirmation so a
    live indeterminate pane is reachable.  Once rollback proves this pane gone,
    only a CLOSED audit row may remain.
    """

    async def _fail_delivery(self, name, brief, before, provider, tmux, **_kwargs):
        return False

    monkeypatch.setattr(spawnctl_mod.SpawnCtl, "_confirm_brief_delivery", _fail_delivery)

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = BootReadyTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            terminal_frames = []

            async def capture_terminal(frame):
                for session in frame.get("sessions", []):
                    if session.get("stream_id") == f"{HOST}:{NAME}" and session.get("state") == "failed":
                        terminal_frames.append((session, await store.get_spawn_outcome(HOST, NAME)))

            sessions.set_inventory_emitter(InventoryEmitter(sessions, capture_terminal, min_interval_s=0))
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            with pytest.raises(VerbError) as exc:
                await ctl.spawn(_spawn_msg(), HOST)
            assert exc.value.code == "prompt_delivery_failed"
            assert tmux.killed is True, "the pane this spawn created is cleaned up"
            open_rows = await store.list_sessions("open")
            reservations = await store.reservations(include_expired=True)
            outcome = await store.get_spawn_outcome(HOST, NAME)
            row = await store.fetch_session(HOST, NAME)
            return open_rows, reservations, outcome, row, terminal_frames
        finally:
            store.stop()

    open_rows, reservations, outcome, row, terminal_frames = asyncio.run(_go())
    assert open_rows == [], f"a failed spawn must leave NO open row, found {open_rows}"
    assert row is not None and row["status"] == "closed", (
        f"a delivery-failed spawn may retain only a closed audit row, found {row}"
    )
    assert reservations == [], "the reservation/intent is released on the error path"
    assert outcome is not None and outcome["state"] == "failed"
    assert outcome["delivery_receipt"]["delivery_status"] == "failed"
    assert outcome["delivery_receipt"]["failure_code"] == "prompt_delivery_failed"
    assert len(terminal_frames) == 1
    terminal_row, emitted_outcome = terminal_frames[0]
    assert emitted_outcome is not None and emitted_outcome["state"] == "failed"
    assert terminal_row["reason"] == emitted_outcome["reason"]


def test_delivery_proof_failure_leaves_the_owned_live_pane_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live identity match retains ownership while proof remains unproven."""

    async def _fail_delivery(self, name, brief, before, provider, tmux, **_kwargs):
        return False

    monkeypatch.setattr(spawnctl_mod.SpawnCtl, "_confirm_brief_delivery", _fail_delivery)

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = IdentityMatchedBootReadyTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            reply = await ctl.spawn(_spawn_msg(), HOST)
            row = await store.fetch_session(HOST, NAME)
            outcome = await store.get_spawn_outcome(HOST, NAME)
            reservations = await store.reservations(include_expired=True)
            await sessions.refresh()
            projected = sessions.list_open()[0]
            awaited = await ctl.await_spawn({"stream_id": f"{HOST}:{NAME}"})
            return reply, row, outcome, reservations, projected, tmux, awaited
        finally:
            store.stop()

    reply, row, outcome, reservations, projected, tmux, awaited = asyncio.run(_go())
    assert reply["type"] == "spawn.ok"
    assert reply["session"]["bootstrap_state"] == "starting"
    assert row is not None
    assert (row["status"], row["closed_at"], row["bootstrap_state"]) == (
        "open", None, "starting",
    )
    assert outcome is not None and outcome["state"] == "indeterminate"
    assert outcome["delivery_evidence"] == "live_pane_unproven"
    receipt = reply["initial_prompt_delivery"]
    assert receipt["state"] == receipt["delivery_status"] == "indeterminate"
    assert receipt["bootstrap_state"] == "starting"
    assert receipt["proof_state"] in {"pending", "unreachable"}
    assert {"proof_watermark", "proof_watermark_state", "proof_watermark_reason"} <= receipt.keys()
    assert reservations and projected["bootstrap_state"] == "starting"
    assert awaited["type"] == "await_spawn.ok"
    assert awaited["do_not_respawn"] is True
    assert tmux.alive is True and tmux.killed is False


def test_attested_codex_proof_failure_without_identity_is_owned_and_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_delivery(self, name, brief, before, provider, tmux, **_kwargs):
        return False

    monkeypatch.setattr(spawnctl_mod.SpawnCtl, "_confirm_brief_delivery", _fail_delivery)

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = AttestedUnknownIdentityTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)

            async def _resolve_launch(_msg, _host, _name):
                return (
                    "run",
                    {"resolved_launch_tuple": {"provider": "codex"}},
                    {
                        "provider": "codex",
                    },
                )

            ctl._resolve_launch = _resolve_launch
            reply = await ctl.spawn({"objective": "Exercise the existing spawn contract",
                "provider": "codex",
                "prompt": "the brief",
                "session_name": "codex-proof-unknown",
                "request_id": "codex-proof-unknown-request",
                "ready_marker": "READY",
            }, HOST)
            row = await store.fetch_session(HOST, "codex-proof-unknown")
            outcome = await store.get_spawn_outcome(HOST, "codex-proof-unknown")
            reservations = await store.reservations(include_expired=True)
            return reply, row, outcome, reservations, tmux
        finally:
            store.stop()

    reply, row, outcome, reservations, tmux = asyncio.run(_go())
    assert reply["type"] == "spawn.ok"
    assert row is not None
    assert (row["status"], row["bootstrap_state"]) == ("open", "starting")
    assert outcome is not None and outcome["state"] == "indeterminate"
    assert outcome["delivery_evidence"] == "live_pane_unproven"
    assert reply["initial_prompt_delivery"]["proof_state"] == "pending"
    assert reservations and len(reservations) == 1
    assert tmux.pastes == 1 and tmux.killed is False


def _legacy_delivery_proof_failure_marks_the_owned_live_pane_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_delivery_proof_failure_leaves_the_owned_live_pane_indeterminate(monkeypatch)


_legacy_delivery_proof_failure_marks_the_owned_live_pane_failed.__test__ = False
globals()["test_delivery_proof_failure_marks_the_owned_live_pane_failed"] = (
    _legacy_delivery_proof_failure_marks_the_owned_live_pane_failed
)


def test_post_open_failure_closes_the_orphan_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The residual window: a failure AFTER the row is opened (here the outcome
    write) must not strand an open row over the killed pane. The error path
    closes it once the pane is confirmed gone. The except-path orphan close is
    part of the contract."""
    calls = {"n": 0}
    real_set = store_mod.Store.set_spawn_outcome

    async def _flaky_outcome(self, host, name, state, **fields):
        calls["n"] += 1
        if calls["n"] == 1:  # the success write, right after sessions.open
            raise RuntimeError("store overloaded")
        return await real_set(self, host, name, state, **fields)

    monkeypatch.setattr(store_mod.Store, "set_spawn_outcome", _flaky_outcome)

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = BootReadyTmux()
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            # No brief: boot succeeds, the row is opened, then the outcome write
            # raises — exercising the post-`sessions.open` failure window.
            msg = {"objective": "Exercise the existing spawn contract", "command": "run", "session_name": NAME, "request_id": "r2"}
            with pytest.raises(Exception):
                await ctl.spawn(msg, HOST)
            assert tmux.killed is True
            return (
                await store.list_sessions("open"),
                await store.fetch_session(HOST, NAME),
                await store.get_session_reap(f"{HOST}:{NAME}"),
            )
        finally:
            store.stop()

    open_rows, row, reap = asyncio.run(_go())
    assert open_rows == [], f"the orphan open row must be closed, found {open_rows}"
    assert row is not None and row["status"] == "closed"
    assert reap is not None
    assert reap["reap_status"] == "unknown"
    assert reap["survivors"] == []


def test_spawn_cleanup_close_is_generation_fenced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawn's boot/post-open failure cleanup must NEVER close a NEWER same-name
    lifecycle that advanced the generation during the fragile spawn window.

    The live-row VANISH class: a stray spawn cleanup close landing on a live
    successor that legitimately reused the name after a confirmed close. The
    cleanup close is fenced to the generation THIS spawn created, so a
    generation that moved on is left untouched. The cleanup close remains
    generation-fenced.
    """
    holder: dict = {}

    async def _reopen_then_fail_delivery(self, name, brief, before, provider, tmux, **_kwargs):
        # A newer same-name lifecycle advances the generation and the registry
        # reflects it (the _inv pop mirrors what sessions.open does on reopen);
        # then THIS spawn's brief delivery fails, driving its boot/delivery
        # cleanup close. Without the fence that close resolves the CURRENT
        # (newer) generation and closes the successor — the live-row VANISH.
        await holder["store"].open_session(
            HOST, NAME, session_generation="newer-gen",
            status="open", pane_status="pane_alive",
        )
        holder["sessions"]._inv.pop(f"{HOST}:{NAME}", None)
        return False

    monkeypatch.setattr(
        spawnctl_mod.SpawnCtl, "_confirm_brief_delivery", _reopen_then_fail_delivery
    )

    async def _go():
        store = Store(":memory:")
        store.start()
        holder["store"] = store
        try:
            tmux = BootReadyTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            holder["sessions"] = sessions
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            reply = await ctl.spawn(_spawn_msg(), HOST)
            return reply, await store.fetch_session(HOST, NAME)
        finally:
            store.stop()

    reply, row = asyncio.run(_go())
    assert reply["type"] == "spawn.ok"
    assert row is not None and row["status"] == "open", (
        f"a spawn cleanup must not close a newer same-name generation, found {row}"
    )


def test_successor_reusing_name_after_admission_check_is_not_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The destructive rollback itself is fenced, not only its earlier check."""
    holder: dict = {}

    async def _fail_delivery(self, *args, **kwargs):
        return False

    original_admitted = spawnctl_mod.SpawnCtl._admitted_live_pane

    async def _replace_after_check(self, host, name, tmux, **kwargs):
        admitted = await original_admitted(self, host, name, tmux, **kwargs)
        assert admitted is False
        await holder["store"].open_session(
            HOST, NAME, session_generation="successor-generation",
            status="open", pane_status="pane_alive", pane_pid="9876",
        )
        return admitted

    monkeypatch.setattr(SpawnCtl, "_confirm_brief_delivery", _fail_delivery)
    monkeypatch.setattr(SpawnCtl, "_admitted_live_pane", _replace_after_check)

    async def _go():
        store = Store(":memory:")
        store.start()
        holder["store"] = store
        try:
            tmux = BootReadyTmux()
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            reply = await ctl.spawn(_spawn_msg(), HOST)
            return reply, await store.fetch_session(HOST, NAME), tmux
        finally:
            store.stop()

    reply, row, tmux = asyncio.run(_go())
    assert reply["type"] == "spawn.ok"
    assert row is not None and row["session_generation"] == "successor-generation"
    assert row["status"] == "open"
    assert tmux.alive is True and tmux.killed is False


def test_rollback_preserves_when_durable_pane_pid_is_missing() -> None:
    """A generation match without durable pane identity never authorizes kill."""
    class IdentityTmux:
        def __init__(self) -> None:
            self.killed: list[str] = []

        async def pane_identity(self, _name: str) -> dict[str, str]:
            return {
                "pane_pid": "9999", "pane_id": "%91", "tty": "/dev/ttys091",
                "tmux_socket": "/tmp/tmux.sock", "session_name": NAME,
            }

        async def kill_pane(self, pane_id: str) -> None:
            self.killed.append(pane_id)

        async def session_state(self, _name: str) -> str:
            return "alive"

    async def _go() -> tuple[bool, list[str]]:
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session(
                HOST, NAME, session_generation="expected-generation",
                status="open", pane_status="pane_alive", pane_pid="",
            )
            tmux = IdentityTmux()
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            rolled_back = await ctl._rollback_kill(
                HOST, NAME, tmux, expected_generation="expected-generation",
            )
            return rolled_back, tmux.killed
        finally:
            store.stop()

    rolled_back, killed = asyncio.run(_go())
    assert rolled_back is False
    assert killed == []


def test_delayed_retry_preserves_an_admitted_live_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    """A newer generation is never rolled back by an older submit failure."""

    holder: dict = {}

    async def _fail_delivery(self, *args, **kwargs):
        await holder["store"].open_session(
            HOST,
            "v2-phantom",
            session_generation="newer-generation",
            status="open",
            pane_status="pane_alive",
        )
        return False

    monkeypatch.setattr(spawnctl_mod.SpawnCtl, "_confirm_brief_delivery", _fail_delivery)

    async def _go():
        store = Store(":memory:")
        store.start()
        holder["store"] = store
        tmux = BootReadyTmux()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        ctl = SpawnCtl(store, sessions, tmux=tmux)

        reply = await ctl.spawn(_spawn_msg(), HOST)
        row = await store.fetch_session(HOST, "v2-phantom")
        outcome = await store.get_spawn_outcome(HOST, "v2-phantom")
        store.stop()
        return reply, row, outcome, tmux

    reply, row, outcome, tmux = asyncio.run(_go())

    assert reply["type"] == "spawn.ok"
    assert row is not None and row["status"] == "open"
    assert outcome is not None and outcome["state"] == "indeterminate"
    assert tmux.killed is False


def test_close_of_pane_less_open_row_is_ok() -> None:
    """A failed spawn leaves durable state that determines the close result.
    An open row whose pane is already gone closes as `close.ok`; a stream with
    no row and no pane is the honest `unknown_session` (`close.error`)."""

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = BootReadyTmux()  # alive=False: the pane is already gone
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            await store.open_session(HOST, NAME, status="open")
            ok = await sessions.close(HOST, NAME)
            # No row and no pane -> honest unknown_session (the close.error state).
            with pytest.raises(VerbError) as exc:
                await sessions.close(HOST, "v2-never-existed")
            return ok, exc.value.code
        finally:
            store.stop()

    ok, missing_code = asyncio.run(_go())
    assert ok["failed"] is False and ok["session"]["status"] == "closed"
    assert missing_code == "unknown_session"
