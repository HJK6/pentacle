from __future__ import annotations

import asyncio

import pytest

import launch
import spawnctl as spawnctl_mod
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from submission_events import (
    EventProof,
    EventWatermark,
    SPAWN_SUBMISSION_PROOF_BOUND_S,
)


HOST = "localhost"


class NoHostCommands:
    local_host = HOST

    def __init__(self, tmux: "CodexTui") -> None:
        self.tmux = tmux

    def tmux_for(self, host: str) -> "CodexTui":
        assert host == HOST
        return self.tmux

    async def run_command(self, *_args, **_kwargs):
        raise AssertionError("local Codex readiness must not run a host command")


class CodexTui:
    def __init__(self, *, nonce_matches: bool = True) -> None:
        self.alive = False
        self.nonce = ""
        self.nonce_matches = nonce_matches
        self.pastes: list[str] = []
        self.staged: dict[str, bytes] = {}

    async def new_session(self, _name, _command, cwd=None, env=None) -> None:
        self.alive = True
        self.nonce = str((env or {}).get("PENTACLE_SPAWN_NONCE") or "")

    async def has_session(self, _name) -> bool:
        return self.alive

    async def session_state(self, _name) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, _name) -> str:
        return "READY\n› "

    async def pane_pid(self, _name) -> str:
        return "4321"

    async def run(self, *_args, **_kwargs) -> tuple[int, str]:
        nonce = self.nonce if self.nonce_matches else "wrong-nonce"
        return 0, f"PENTACLE_SPAWN_NONCE={nonce}\n"

    async def paste(self, _name, text) -> None:
        self.pastes.append(text)

    async def stage_text(self, path: str, data: bytes) -> None:
        self.staged[path] = data

    async def kill_session(self, _name) -> None:
        self.alive = False


class NativeUserProof:
    def __init__(self, clock=None) -> None:
        self.clock = clock
        self.timeouts: list[float] = []

    async def wait_for_initial_user_event(
        self, stream_id: str, *, expected_text: str, timeout_s: float,
    ) -> EventProof:
        assert expected_text == "native user prompt"
        self.timeouts.append(timeout_s)
        if self.clock is not None:
            self.clock.now = 279.0  # delivery arrives at the 180s deadline minus one second.
        return EventProof("proven", stream_id, 0, event_id=1)


class ProductionTailStagedUserProof:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    async def watermark(self, stream_id: str) -> EventWatermark:
        return EventWatermark(stream_id, 7, "reachable")

    async def wait(
        self, stream_id: str, *, expected_text: str,
        watermark: EventWatermark, timeout_s: float,
    ) -> EventProof:
        assert expected_text == "claude user prompt"
        self.timeouts.append(timeout_s)
        if timeout_s < 106.4:
            return EventProof("pending", stream_id, watermark.daemon_seq)
        return EventProof(
            "proven", stream_id, watermark.daemon_seq, event_id=8,
        )


async def _resolved_codex(_msg, _host, _name):
    return (
        "run",
        {"resolved_launch_tuple": {"provider": "codex"}},
        {
            "provider": "codex",
        },
    )


async def _resolved_native_codex(_msg, _host, _name):
    return (
        f"run {launch.NATIVE_INITIAL_PROMPT_LAUNCHER}",
        {"resolved_launch_tuple": {"provider": "codex"}},
        {
            "provider": "codex",
        },
    )


async def _resolved_claude(_msg, _host, _name):
    return (
        "run",
        {"resolved_launch_tuple": {"provider": "claude"}},
        {"provider": "claude"},
    )


async def _confirmed_delivery(*_args, **_kwargs) -> bool:
    return True


def _spawn(
    msg: dict,
    tmux: CodexTui,
    *,
    native: bool = False,
    proof: NativeUserProof | ProductionTailStagedUserProof | None = None,
    resolver=None,
) -> tuple[dict, dict | None, dict]:
    async def go() -> tuple[dict, dict | None, dict]:
        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(
                store,
                Sessions(store, tmux=tmux, local_host=HOST),
                tmux=tmux,
                hosts=NoHostCommands(tmux),
                submission_proof=proof,
            )
            ctl._resolve_launch = resolver or (
                _resolved_native_codex if native else _resolved_codex
            )
            ctl._pane_started_at = lambda *_args, **_kwargs: asyncio.sleep(0, result="")
            if not native and proof is None:
                ctl._confirm_brief_delivery = _confirmed_delivery
            reply = await ctl.spawn(msg, HOST)
            visible = await ctl.await_spawn({"spawn_request_id": msg["request_id"]})
            return reply, await store.fetch_session(HOST, msg["session_name"]), visible
        finally:
            store.stop()

    return asyncio.run(go())


def test_promptless_codex_tui_nonce_identity_becomes_submission_pending(monkeypatch) -> None:
    async def forbidden_process(*_args, **_kwargs):
        raise AssertionError("readiness must not execute a non-tmux local process")

    monkeypatch.setattr(spawnctl_mod.asyncio, "create_subprocess_exec", forbidden_process)
    reply, row, _visible = _spawn({"objective": "Exercise the existing spawn contract",
        "provider": "codex", "session_name": "promptless", "request_id": "promptless-request",
        "ready_marker": "READY",
    }, CodexTui())

    assert reply["state"] == "ready"
    assert row is not None
    assert row["pane_pid"] == "4321"


def test_staged_codex_durable_user_proof_becomes_ready() -> None:
    prompt = "staged proof " + ("x" * 300)
    reply, row, _visible = _spawn({"objective": "Exercise the existing spawn contract",
        "provider": "codex", "prompt": prompt, "session_name": "staged", "request_id": "staged-request",
        "ready_marker": "READY",
    }, CodexTui())

    assert reply["state"] == "ready"
    assert reply["initial_prompt_delivery"]["transport"] == "staged"
    assert row is not None


def test_native_argv_user_at_deadline_minus_one_becomes_ready_without_boot_binding(monkeypatch) -> None:
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    proof = NativeUserProof(clock)
    monkeypatch.setattr(spawnctl_mod.time, "monotonic", clock.monotonic)
    reply, row, _visible = _spawn({"objective": "Exercise the existing spawn contract",
        "provider": "codex", "initial_prompt": "native user prompt",
        "session_name": "native", "request_id": "native-request", "ready_marker": "READY",
    }, CodexTui(), native=True, proof=proof)

    assert reply["state"] == "ready"
    assert proof.timeouts == [SPAWN_SUBMISSION_PROOF_BOUND_S]
    assert clock.now == 279.0
    assert row is not None


def test_production_tail_claude_proof_is_cli_visible_ready() -> None:
    proof = ProductionTailStagedUserProof()
    spawned, _row, visible = _spawn({"objective": "Exercise the existing spawn contract",
        "provider": "claude", "prompt": "claude user prompt",
        "session_name": "production-tail-claude",
        "request_id": "production-tail-claude-request", "ready_marker": "READY",
    }, CodexTui(), proof=proof, resolver=_resolved_claude)

    assert len(proof.timeouts) == 1
    assert 106.4 <= proof.timeouts[0] <= SPAWN_SUBMISSION_PROOF_BOUND_S
    assert spawned["type"] == "spawn.ok" and spawned["state"] == "ready"
    assert visible["type"] == "await_spawn.ok" and visible["state"] == "ready"
    assert visible["session"]["bootstrap_state"] == "ready"


def test_wrong_nonce_pane_not_ready() -> None:
    async def go() -> tuple[VerbError, dict | None]:
        tmux = CodexTui(nonce_matches=False)
        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
            ctl._resolve_launch = _resolved_codex
            ctl._pane_started_at = lambda *_args, **_kwargs: asyncio.sleep(0, result="")
            with pytest.raises(VerbError) as exc:
                await ctl.spawn({"objective": "Exercise the existing spawn contract",
                    "provider": "codex", "session_name": "wrong-nonce", "request_id": "wrong-nonce-request",
                    "ready_marker": "READY",
                }, HOST)
            return exc.value, await store.fetch_session(HOST, "wrong-nonce")
        finally:
            store.stop()

    error, row = asyncio.run(go())
    assert error.code == "boot_not_ready"
    assert row is not None
    assert row["bootstrap_state"] == "failed"
