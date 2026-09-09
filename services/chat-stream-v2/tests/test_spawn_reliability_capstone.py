"""Contract coverage for spawn-confirmation reliability."""

from __future__ import annotations

import tmux_transport

import asyncio
import json
import sys
import time as real_time
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]

import spawnctl as spawnctl_mod  # noqa: E402
from agent_orch import cli as agent_orch_cli  # noqa: E402
from agent_orch.cli import _classify_self_terminate  # noqa: E402
from comms import Comms  # noqa: E402
from ledger import Ledger  # noqa: E402
from server import Server  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value


class _ClockShim:
    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock

    def monotonic(self) -> float:
        return self.clock.monotonic()

    def __getattr__(self, name: str):
        return getattr(real_time, name)


class _SlowMarkerTmux:
    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.calls = 0

    async def capture(self, _name: str) -> str:
        self.calls += 1
        if self.calls == 1:
            self.clock.value = 0.25
            return "provider boot step 1"
        return "READY"

    async def has_session(self, _name: str) -> bool:
        return True

    async def session_state(self, _name: str) -> str:
        return "alive"


def test_marker_wait_extends_for_a_live_slow_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live pane that is still changing after the nominal deadline gets read again."""
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)
    clock = _FakeClock()
    monkeypatch.setattr(spawnctl_mod, "time", _ClockShim(clock))
    tmux = _SlowMarkerTmux(clock)
    ctl = SpawnCtl(Store(":memory:"), Sessions(Store(":memory:"), tmux=tmux, local_host=HOST), tmux=tmux)

    ready = asyncio.run(ctl._await_marker("slow", "READY", 0.2, tmux=tmux))

    assert ready is True
    assert tmux.calls == 2


class _ChangingProviderReceiptTmux:
    """A provider-shaped peer pane whose submission proof arrives late."""

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.calls = 0

    async def capture(self, _name: str) -> str:
        self.calls += 1
        self.clock.value = 0.25 if self.calls == 1 else 0.26
        if self.calls == 1:
            return "⏵⏵ bypass permissions on (bypass)\n❯ deliver this brief now"
        return "⏺ deliver this brief now\n❯ "

    async def session_state(self, _name: str) -> str:
        return "alive"

    async def has_session(self, _name: str) -> bool:
        return True


def test_provider_submission_confirmation_has_fixed_deadline_for_changing_remote_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuous pane repainting cannot extend submit confirmation forever."""
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(tmux_transport, "RECEIPT_TIMEOUT_S", 0.2)
    clock = _FakeClock()
    monkeypatch.setattr(spawnctl_mod, "time", _ClockShim(clock))
    tmux = _ChangingProviderReceiptTmux(clock)
    ctl = SpawnCtl(Store(":memory:"), Sessions(Store(":memory:"), tmux=tmux, local_host=HOST), tmux=tmux)

    async def no_transcript(_name: str, _needle: str) -> str:
        return "no_transcript"

    ctl._transcript_status = no_transcript  # type: ignore[method-assign]
    confirmed = asyncio.run(
        ctl._confirm_submission("remote", "deliver this brief now", "", "claude", tmux)
    )

    assert confirmed is False
    assert tmux.calls == 0


class _TimeoutAfterCreateTmux:
    def __init__(self) -> None:
        self.alive = False
        self.killed = False

    async def has_session(self, _name: str) -> bool:
        return self.alive

    async def new_session(self, _name: str, _command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self.alive = True
        raise VerbError("tmux_timeout", "new-session reply timed out after creation")

    async def session_state(self, _name: str) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, _name: str) -> str:
        return "READY"

    async def pane_pid(self, _name: str) -> str:
        return "1234"

    async def kill_session(self, _name: str) -> None:
        self.alive = False
        self.killed = True


class _UnreachableAfterCreateTmux:
    """Creation may have happened, but every immediate liveness read is remote-unknown."""

    def __init__(self) -> None:
        self.created = False
        self.reachable = False

    async def has_session(self, _name: str) -> bool:
        return self.created and self.reachable

    async def new_session(self, _name: str, _command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self.created = True
        raise VerbError("tmux_timeout", "new-session reply timed out after creation")

    async def session_state(self, _name: str) -> str:
        return "alive" if self.reachable and self.created else "unreachable"

    async def pane_pid(self, _name: str) -> str:
        return "4321"

    async def capture(self, _name: str) -> str:
        return "READY"

    async def kill_session(self, _name: str) -> None:
        self.created = False


def test_new_session_timeout_with_unreachable_probe_stays_reconcilable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown creation cannot become a terminal tmux_timeout or lose its intent."""

    async def run() -> tuple[VerbError, _UnreachableAfterCreateTmux, list[dict], dict | None, dict, dict | None]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _UnreachableAfterCreateTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)

            # The live pane carries its reservation's creation nonce in its env
            # (spec §public contract); model that read so adoption is exercised.
            async def _match_nonce(*_args: object) -> tuple[bool, str]:
                rows = await store.reservations(include_expired=True)
                return (True, str(rows[0]["nonce"]) if rows else "")

            ctl._tmux_nonce = _match_nonce  # type: ignore[method-assign]
            with pytest.raises(VerbError) as raised:
                await ctl.spawn(
                    {"objective": "Exercise the existing spawn contract", "command": "stub", "session_name": "unknown-create", "request_id": "r-unknown-create"},
                    HOST,
                )
            reservations = await store.reservations(include_expired=True)
            before = await store.get_spawn_outcome(HOST, "unknown-create")
            tmux.reachable = True
            reconciled = await ctl.reconcile_spawn_intents()
            outcome = await store.get_spawn_outcome(HOST, "unknown-create")
            return raised.value, tmux, reservations, before, reconciled, outcome
        finally:
            store.stop()

    monkeypatch.setattr(spawnctl_mod, "CREATION_PROBE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)
    error, _tmux, reservations, before, reconciled, outcome = asyncio.run(run())
    assert error.code == "spawn_launch_unconfirmed"
    assert reservations and reservations[0]["tmux_created"] == 1
    assert before is None
    assert reconciled == {"adopted": 1, "released": 0}
    assert outcome is not None and outcome["state"] == "delivered"


def test_new_session_timeout_adopts_the_pane_that_was_created() -> None:
    """A successful tmux create must not be reported as a failed spawn."""

    async def run() -> tuple[dict, _TimeoutAfterCreateTmux]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _TimeoutAfterCreateTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            reply = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "command": "stub", "session_name": "timeout-created", "request_id": "r-timeout"},
                HOST,
            )
            return reply, tmux
        finally:
            store.stop()

    reply, tmux = asyncio.run(run())
    assert reply["type"] == "spawn.ok"
    assert tmux.alive is True
    assert tmux.killed is False


class _RetryTmux:
    def __init__(self) -> None:
        self.alive = False
        self.kills = 0

    async def has_session(self, _name: str) -> bool:
        return self.alive

    async def session_state(self, _name: str) -> str:
        return "alive" if self.alive else "gone"

    async def kill_session(self, _name: str) -> None:
        self.kills += 1
        self.alive = False


def test_prompt_submit_failure_does_not_spawn_a_second_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Enter-only retry is final; failure must not duplicate the whole spawn."""

    async def run() -> tuple[VerbError, _RetryTmux, int]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _RetryTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            calls = 0

            async def fenced(self, host, name, request_id, command, brief, ready_marker, msg, created,
                             creation_uncertain, open_fields, resolution, tmux, delivery_receipt, nonce="",
                             *, admission=None, attempt=0, total_attempts=1):
                nonlocal calls
                calls += 1
                created[0] = True
                tmux.alive = True
                if calls == 1:
                    # Match the real post-create invariant: admission precedes
                    # delivery, so rollback has a generation-fenced row.
                    await sessions.open(
                        host, name, **open_fields, pane_pid="4321",
                        pane_status="pane_alive", fence=request_id,
                    )
                    raise VerbError("prompt_delivery_failed", "first attempt was not confirmed")
                return {"type": "spawn.ok", "ok": True, "stream_id": f"{host}:{name}"}

            monkeypatch.setattr(SpawnCtl, "_spawn_fenced", fenced)
            with pytest.raises(VerbError) as raised:
                await ctl.spawn(
                    {"objective": "Exercise the existing spawn contract",
                        "command": "stub",
                        "session_name": "retry-prompt",
                        "request_id": "r-retry",
                        "prompt": "deliver this",
                    },
                    HOST,
                )
            return raised.value, tmux, calls
        finally:
            store.stop()

    error, tmux, calls = asyncio.run(run())
    assert error.code == "prompt_delivery_failed"
    assert calls == 1
    assert tmux.kills == 1


class _CancelledSpawnTmux:
    def __init__(self) -> None:
        self.alive = False
        self.created = asyncio.Event()
        self.pastes = 0

    async def has_session(self, _name: str) -> bool:
        return self.alive

    async def new_session(self, _name: str, _command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self.alive = True
        self.created.set()

    async def capture(self, _name: str) -> str:
        return "still booting"

    async def has_session_never(self, _name: str) -> bool:
        return True

    async def pane_pid(self, _name: str) -> str:
        return "1234"

    async def paste(self, _name: str, _text: str) -> None:
        self.pastes += 1

    async def session_state(self, _name: str) -> str:
        return "alive" if self.alive else "gone"

    async def kill_session(self, _name: str) -> None:
        self.alive = False


def test_disconnect_mid_spawn_rolls_back_in_background(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling the RPC cannot strand a promptless created pane."""
    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.05)
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.01)

    async def run() -> tuple[_CancelledSpawnTmux, dict | None, list[dict]]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _CancelledSpawnTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            task = asyncio.create_task(
                Server(spawnctl=ctl, sessions=sessions, local_host=HOST)._serve(
                    None,
                    json.dumps({"objective": "Exercise the existing spawn contract",
                        "type": "spawn",
                        "command": "stub",
                        "session_name": "disconnect-mid-spawn",
                        "request_id": "r-disconnect",
                        "prompt": "must not become a promptless shell",
                    }),
                )
            )
            await asyncio.wait_for(tmux.created.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # The failure outcome precedes row closure. Await the owned
            # obligation before observing cleanup or stopping its store.
            await asyncio.wait_for(
                asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True), timeout=1.0,
            )
            outcome = await store.get_spawn_outcome(HOST, "disconnect-mid-spawn")
            open_rows = await store.list_sessions("open")
            return tmux, outcome, open_rows
        finally:
            store.stop()

    tmux, outcome, open_rows = asyncio.run(run())
    assert tmux.alive is False
    assert tmux.pastes == 0
    assert outcome is not None and outcome["state"] == "failed"
    assert open_rows == []


class _BareReportTmux:
    def __init__(self) -> None:
        self.alive = False
        self.screen = ""
        self.sent: list[str] = []

    async def has_session(self, _name: str) -> bool:
        return self.alive

    async def new_session(self, _name: str, _command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self.alive = True
        self.screen = "READY"

    async def capture(self, _name: str) -> str:
        return self.screen

    async def paste(self, _name: str, text: str) -> None:
        self.sent.append(text)
        self.screen += "\n" + text

    async def pane_pid(self, _name: str) -> str:
        return "1234"

    async def session_state(self, _name: str) -> str:
        return "alive" if self.alive else "gone"

    async def kill_session(self, _name: str) -> None:
        self.alive = False


def test_bare_spawn_send_report_terminate_closes_the_row() -> None:
    """A no-initial-prompt worker still carries the terminate-close state."""

    async def run() -> tuple[dict, dict, dict, _BareReportTmux]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _BareReportTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            spawnctl = SpawnCtl(store, sessions, tmux=tmux)
            spawned = await spawnctl.spawn(
                {"objective": "Exercise the existing spawn contract",
                    "command": "stub",
                    "session_name": "bare-report",
                    "request_id": "r-bare-report",
                    "parent_stream_id": f"{HOST}:leader",
                    "role": "qa",
                    "visibility": "hidden",
                    "self_close_on_completion": True,
                },
                HOST,
            )
            stream_id = spawned["stream_id"]
            sent = await Comms(store, sessions, spawnctl).send(
                {"to_stream_id": stream_id, "message": "do the work", "tell_id": "tell-bare-report"}
            )
            row = await store.fetch_session(HOST, "bare-report")
            assert row is not None
            assert bool(row["self_close_on_completion"]) is True
            decision = _classify_self_terminate({stream_id: row}, stream_id, grace_s=0)
            assert decision["path"] == "worker_authorized_self_close"
            report = await Ledger(store, sessions=sessions).report(
                {
                    "type": "report",
                    "report_id": "report-bare-report",
                    "from_stream_id": stream_id,
                    "msg_id": 0,
                    "status": "done",
                    "summary": "bare worker finished",
                    "findings": [],
                    "next_action": "done",
                    "qa_verdict": "accept",
                    "target_sha": "0" * 40,
                    "terminate": True,
                    "close_on_ingest": True,
                }
            )
            return spawned, sent, report, tmux
        finally:
            store.stop()

    spawned, sent, report, tmux = asyncio.run(run())
    assert spawned["type"] == "spawn.ok"
    assert sent["type"] == "send.result"
    assert tmux.sent == ["do the work"]
    assert report["type"] == "report.ok"
    assert report["closed"] is True


def test_negative_spawn_flag_survives_cli_resolution_and_persists_false() -> None:
    """An explicit CLI opt-out must reach the durable admission row."""

    async def run() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _BareReportTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            spawnctl = SpawnCtl(store, sessions, tmux=tmux)
            args = SimpleNamespace(self_close_on_completion=False)
            resolved = agent_orch_cli._resolve_self_close_on_completion(
                args, visibility="hidden", parent=f"{HOST}:leader", handoff=False
            )
            rpc_payload = {"objective": "Exercise the existing spawn contract",
                "command": "stub",
                "session_name": "negative-self-close",
                "request_id": "r-negative-self-close",
                "parent_stream_id": f"{HOST}:leader",
                "role": "qa",
                "visibility": "hidden",
            }
            if resolved is not None:
                rpc_payload["self_close_on_completion"] = resolved
            spawned = await spawnctl.spawn(rpc_payload, HOST)
            row = await store.fetch_session(HOST, "negative-self-close")
            assert spawned["stream_id"] == f"{HOST}:negative-self-close"
            assert row is not None
            assert row["self_close_on_completion"] is False
            assert resolved is False
            return {"stream_id": spawned["stream_id"], "self_close": row["self_close_on_completion"]}
        finally:
            store.stop()

    result = asyncio.run(run())
    assert result == {"stream_id": f"{HOST}:negative-self-close", "self_close": False}


def test_await_spawn_resolves_a_null_stream_by_spawn_request_id() -> None:
    """The request-id-only reconcile path is the indeterminate-path contract."""

    async def run() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            name = "request-only"
            await store.open_session(HOST, name, provider="codex", pane_status="pane_alive")
            await store.set_spawn_outcome(
                HOST,
                name,
                "delivered",
                request_id="spawn-request-only",
                delivery_evidence="confirmed",
            )
            ctl = SpawnCtl(store, Sessions(store, local_host=HOST), tmux=object())
            return await ctl.await_spawn({"spawn_request_id": "spawn-request-only"})
        finally:
            store.stop()

    reply = asyncio.run(run())
    assert reply["type"] == "await_spawn.ok"
    assert reply["stream_id"] == f"{HOST}:request-only"


def test_await_spawn_indeterminate_reports_starting_until_reconciled_ready() -> None:
    async def run() -> tuple[dict, dict]:
        store = Store(":memory:")
        store.start()
        try:
            name = "indeterminate-request"
            await store.open_session(HOST, name, provider="codex", pane_status="pane_alive")
            await store.set_spawn_outcome(
                HOST,
                name,
                "indeterminate",
                request_id="spawn-indeterminate",
                reason="confirmation pending",
            )
            ctl = SpawnCtl(store, Sessions(store, local_host=HOST), tmux=object())
            pending = await ctl.await_spawn({"spawn_request_id": "spawn-indeterminate"})
            await store.set_spawn_outcome(
                HOST,
                name,
                "delivered",
                request_id="spawn-indeterminate",
                delivery_evidence="confirmed",
            )
            delivered = await ctl.await_spawn({"spawn_request_id": "spawn-indeterminate"})
            return pending, delivered
        finally:
            store.stop()

    pending, delivered = asyncio.run(run())
    assert pending["type"] == "await_spawn.ok"
    assert pending["state"] == "starting"
    assert pending["pending_reconcile"] is True
    assert pending["action_status"] == "committed"
    assert pending["confirmation_status"] == "pending"
    assert pending["do_not_respawn"] is True
    assert "DO NOT RESPAWN" in pending["retry_guidance"]
    assert delivered["type"] == "await_spawn.ok"
    assert delivered["state"] == "ready"


def test_staged_delivery_budget_scales_for_a_large_fixture() -> None:
    """Large staged payloads receive a bounded budget without live transport."""
    payload = b"x" * 500_000
    deadline = tmux_transport._stage_timeout(len(payload))
    assert deadline > 10.0
    assert deadline <= tmux_transport.STAGE_TIMEOUT_CEILING_S
