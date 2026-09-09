"""`boot_not_ready` rollback must leave no orphan pane and deliver no brief.

These contract tests cover two properties:

  (a) On boot_not_ready the reservation is released and the tmux pane is
      eventually removed. A transient terminal stall must not strand a live
      pane, so rollback retries and verifies cleanup.

  (b) No silent half-spawn: a boot_not_ready attempt must never paste the brief
      (delivery is gated behind readiness, so a failed spawn delivers nothing).

The tests use a fake terminal whose first kill can stall, plus a standing
invariant guard for prompt delivery.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import spawnctl as spawnctl_mod  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"
#: A codex pane that never reaches readiness (no chrome, no `›` composer).
NEVER_READY = "Loading codex…"


class NeverReadyTmux:
    """A codex pane that boots the tmux session but whose CLI never becomes
    ready → boot_not_ready. `kill_session` models a host under load: the first
    `kill_stalls` kills raise `tmux_timeout` (the command queued behind a
    saturated host) before one finally takes. `paste` is counted so the test can
    prove a failed spawn delivered nothing."""

    def __init__(self, *, kill_stalls: int, state_override: str = "") -> None:
        self.alive = False  # no pane until `new_session` (the pre-create guard)
        self.kill_stalls = kill_stalls
        self.kill_attempts = 0
        self.new_sessions = 0
        self.pastes = 0
        self.state_override = state_override

    async def new_session(self, name: str, command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self.alive = True
        self.new_sessions += 1

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def session_state(self, name: str) -> str:
        if self.state_override:
            return self.state_override
        return "alive" if self.alive else "gone"

    async def capture(self, name: str) -> str:
        return NEVER_READY

    async def pane_pid(self, name: str) -> str:
        return ""

    async def kill_session(self, name: str) -> None:
        self.kill_attempts += 1
        if self.kill_attempts <= self.kill_stalls:
            raise VerbError("tmux_timeout", "kill-session timed out under load")
        self.alive = False

    async def paste(self, name: str, text: str) -> None:
        self.pastes += 1

    async def run(self, *args: str, **kw) -> tuple[int, str]:
        return 0, ""


def _codex_resolve(msg, host, name):
    # A codex tuple spawn: drives the provider readiness predicate path
    # (`_await_provider_ready`), the surface of the observed flap.
    async def _inner():
        return "codex --tui", {"resolved_launch_tuple": {"provider": "codex"}}, {}

    return _inner()


def _run_boot_not_ready(tmux, msg):
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            ctl._resolve_launch = _codex_resolve  # bypass LocalMachine resolution
            with pytest.raises(VerbError) as exc:
                await ctl.spawn(msg, HOST)
            assert exc.value.code == "boot_not_ready", exc.value
            open_rows = await store.list_sessions("open")
            reservations = await store.reservations(include_expired=True)
            return open_rows, reservations
        finally:
            store.stop()

    return asyncio.run(_go())


def test_boot_not_ready_kills_pane_despite_kill_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled first kill must not orphan the pane: rollback retries until the
    pane is verified gone."""
    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.2)
    tmux = NeverReadyTmux(kill_stalls=1)  # first kill times out, a retry takes
    open_rows, reservations = _run_boot_not_ready(
        tmux, {"objective": "Exercise the existing spawn contract", "session_name": "v2-rb01", "provider": "codex"}
    )
    assert tmux.alive is False, "boot_not_ready rollback left an orphan pane"
    assert tmux.kill_attempts >= 2, "the stalled kill was not retried"
    assert open_rows == [] and reservations == [], "no phantom row / reservation survives"


def test_boot_not_ready_delivers_no_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) A failed spawn must paste nothing — delivery is gated behind readiness,
    so there is no silent half-spawn."""
    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.2)
    tmux = NeverReadyTmux(kill_stalls=0)
    _run_boot_not_ready(
        tmux, {"objective": "Exercise the existing spawn contract", "session_name": "v2-rb02", "provider": "codex", "initial_prompt": "do the thing"}
    )
    assert tmux.pastes == 0, "a boot_not_ready spawn must not deliver the brief"
    assert tmux.alive is False


def test_rollback_confirm_timeout_retains_one_handle_then_reconciles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1/B2 synthetic: an exhausted rollback-confirm loop returns one
    addressable starting handle, never starts another pane or pastes the brief,
    then the existing reconciler writes exactly one terminal outcome once the
    fake pane is explicitly observed gone.

    This is entirely a Store/Tmux double with no external runtime dependency.
    """

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.01)
    monkeypatch.setattr(spawnctl_mod, "ROLLBACK_KILL_RETRY_S", 0)
    monkeypatch.setattr(spawnctl_mod.asyncio, "sleep", _no_sleep)

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = NeverReadyTmux(
                kill_stalls=spawnctl_mod.ROLLBACK_KILL_ATTEMPTS,
                state_override="alive",
            )
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            ctl._resolve_launch = _codex_resolve
            reply = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract",
                    "session_name": "v2-rb-confirm-timeout",
                    "provider": "codex",
                    "initial_prompt": "must not be pasted",
                    "request_id": "rb-timeout",
                },
                HOST,
            )
            reservations = await store.reservations(include_expired=True)
            before = await store.get_spawn_outcome(HOST, "v2-rb-confirm-timeout")

            # Later reconciliation gets an explicit gone observation.  This is
            # a double-state transition, not an input or a runtime pane action.
            tmux.state_override = ""
            tmux.alive = False
            reconciled = await ctl.reconcile_spawn_intents()
            after = await store.get_spawn_outcome(HOST, "v2-rb-confirm-timeout")
            return reply, tmux, reservations, before, reconciled, after
        finally:
            store.stop()

    reply, tmux, reservations, before, reconciled, after = asyncio.run(_go())
    assert reply["type"] == "spawn.ok"
    assert reply["state"] == "starting"
    assert len(reservations) == 1 and reservations[0]["session_name"] == "v2-rb-confirm-timeout"
    assert before is None
    assert tmux.new_sessions == 1
    assert tmux.pastes == 0
    assert reconciled == {"adopted": 0, "released": 1}
    assert after is not None and after["state"] == "failed"


def test_creation_timeout_mark_failure_keeps_preexisting_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A possible timeout-created pane is never released merely because the
    best-effort pane-mark write also fails. The requester receives a typed
    pre-admission error while the retained intent awaits explicit-gone reconciliation.
    """

    class UncertainCreateTmux(NeverReadyTmux):
        def __init__(self) -> None:
            super().__init__(kill_stalls=0, state_override="unreachable")

        async def new_session(self, name: str, command: str, cwd=None, env=None) -> None:
            self.new_sessions += 1
            raise VerbError("tmux_timeout", "new-session confirmation timed out")

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = UncertainCreateTmux()
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)

            async def _unknown(_name, _tmux):
                return "unknown"

            async def _mark_fails(_host, _name, **_kwargs):
                raise RuntimeError("simulated mark write failure")

            ctl._wait_for_created_session = _unknown  # type: ignore[method-assign]
            monkeypatch.setattr(store, "mark_tmux_created", _mark_fails)
            with pytest.raises(VerbError) as raised:
                await ctl.spawn(
                    {"objective": "Exercise the existing spawn contract", "command": "stub", "session_name": "v2-create-uncertain", "request_id": "create-timeout"},
                    HOST,
                )
            reservations = await store.reservations(include_expired=True)

            tmux.state_override = "gone"
            reconciled = await ctl.reconcile_spawn_intents()
            outcome = await store.get_spawn_outcome(HOST, "v2-create-uncertain")
            return raised.value, reservations, reconciled, outcome
        finally:
            store.stop()

    error, reservations, reconciled, outcome = asyncio.run(_go())
    assert error.code == "spawn_launch_unconfirmed"
    assert len(reservations) == 1 and reservations[0]["payload"] is not None
    assert reconciled == {"adopted": 0, "released": 1}
    assert outcome is not None and outcome["state"] == "failed"
