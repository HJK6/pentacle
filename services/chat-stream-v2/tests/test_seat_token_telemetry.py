"""Regression coverage for generic seat-token delivery and telemetry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import subprocess
import pytest

import launch  # noqa: E402
from seat_token_telemetry import SeatTokenTelemetry  # noqa: E402
from server import Server  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from tmux_transport import Tmux
from store import STREAM_TOKEN_HASH_VERSION, Store  # noqa: E402
from uiverbs import UIVerbs  # noqa: E402
from agent_orch.wsclient import stream_token_from_env  # noqa: E402


def test_issuance_event_is_debug_and_token_free(caplog: pytest.LogCaptureFixture) -> None:
    telemetry = SeatTokenTelemetry()
    with caplog.at_level(logging.DEBUG, logger="chat_streamd_v2.seat_token"):
        telemetry.record_issuance(stream_id="hosta:v2-seat", operation="spawn")

    events = [record for record in caplog.records if "seat_token event=" in record.getMessage()]
    assert events and events[-1].levelno == logging.DEBUG
    assert "stream_token" not in events[-1].getMessage()


def test_launch_command_contains_no_token_material() -> None:
    machine = launch.local_machine(
        "hosta",
        cwd="/tmp/public-test",
        claude_bin="/bin/echo",
        codex_bin="/bin/echo",
        projects_root="/tmp/public-test",
        agent_orch_bin_dir="/tmp/public-test",
    )
    plan = launch.build_launch(
        machine,
        provider="codex",
        tmux_session="fresh-seat",
        launch_model=None,
        launch_effort=None,
    )

    assert plan.stream_token not in plan.command
    assert "AGENT_ORCH_STREAM_TOKEN=" not in plan.command
    assert "AGENT_ORCH_STREAM_TOKEN_FILE=" in plan.command
    assert plan.stream_token_file.endswith(".token")


def test_staged_token_is_generic_and_never_part_of_new_session_argv(tmp_path: Path) -> None:
    token = "tok"
    token_file = tmp_path / ".pentacle-stream-tokens" / "seat.token"
    tmux = Tmux()

    assert not hasattr(Tmux, "stage_secret")
    asyncio.run(tmux.stage_text(str(token_file), token.encode("utf-8")))

    assert token_file.read_text(encoding="utf-8") == token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700

    command = f"AGENT_ORCH_STREAM_TOKEN_FILE={token_file} exec /bin/sleep 2"
    process = subprocess.Popen(
        ["/bin/sh", "-c", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ps = subprocess.run(
            ["/bin/ps", "-eo", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            check=False,
        )
        assert ps.returncode == 0
        processes: dict[int, tuple[int, str]] = {}
        for line in ps.stdout.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3:
                processes[int(fields[0])] = (int(fields[1]), fields[2])
        tree = {process.pid}
        changed = True
        while changed:
            changed = False
            for pid, (ppid, _command) in processes.items():
                if ppid in tree and pid not in tree:
                    tree.add(pid)
                    changed = True
        assert process.pid in processes
        assert all(token not in processes[pid][1] for pid in tree if pid in processes)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_cli_reads_only_generic_token_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = "tok"
    token_file = tmp_path / "seat.token"
    token_file.write_text(token, encoding="utf-8")
    os.chmod(token_file, 0o600)
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN_FILE", str(token_file))

    assert stream_token_from_env() == token

    os.chmod(token_file, 0o644)
    assert stream_token_from_env() is None


class _TokenStageTmux:
    def __init__(self) -> None:
        self.staged: list[tuple[str, bytes]] = []
        self.commands: list[str] = []
        self.live: set[str] = set()
        self.ready = True
        self.nonce = ""

    async def stage_text(self, path: str, data: bytes) -> None:
        self.staged.append((path, data))

    async def new_session(
        self, name: str, command: str, env: dict | None = None, **_kwargs: object,
    ) -> None:
        self.commands.append(command)
        self.live.add(name)
        self.nonce = str((env or {}).get("PENTACLE_SPAWN_NONCE") or "")

    async def has_session(self, name: str) -> bool:
        return name in self.live

    async def session_state(self, name: str) -> str:
        return "alive" if name in self.live else "gone"

    async def capture(self, _name: str) -> str:
        return "READY" if self.ready else ""

    async def pane_pid(self, _name: str) -> str:
        return "4242"

    async def kill_session(self, name: str) -> None:
        self.live.discard(name)

    async def run(self, *_args: object, **_kwargs: object) -> tuple[int, str]:
        # Promptless Codex readiness reads the pane nonce from tmux.
        return 0, f"PENTACLE_SPAWN_NONCE={self.nonce}\n"


def test_tuple_spawn_stages_token_and_records_issuance(tmp_path: Path) -> None:
    async def run() -> None:
        tmux = _TokenStageTmux()
        telemetry = SeatTokenTelemetry()
        machine = launch.local_machine(
            "hosta",
            cwd=str(tmp_path),
            claude_bin="/bin/echo",
            codex_bin="/bin/echo",
            projects_root=str(tmp_path / "projects"),
            agent_orch_bin_dir=str(tmp_path / "bin"),
        )
        ctl = SpawnCtl(
            store=None,
            sessions=None,
            tmux=tmux,
            machine=machine,
            token_telemetry=telemetry,
        )

        from tmux_transport import _ACTIVE_LAUNCH_TMUX

        marker = _ACTIVE_LAUNCH_TMUX.set(tmux)
        try:
            command, _resolution, _overrides = await ctl._resolve_launch(
                {"provider": "codex", "model": "gpt-5.6-luna", "effort": "max"},
                "hosta",
                "fresh-seat",
            )
        finally:
            _ACTIVE_LAUNCH_TMUX.reset(marker)

        assert len(tmux.staged) == 1
        staged_path, staged_token = tmux.staged[0]
        assert staged_path in command
        assert staged_token.decode("utf-8") not in command
        snapshot = telemetry.snapshot()
        assert snapshot["counts"] == {
            "issuance": 1,
            "verification_success": 0,
            "verification_failure": 0,
        }

    asyncio.run(run())


def test_fresh_tuple_spawn_enrolls_only_its_staged_token() -> None:
    async def run() -> tuple[dict, dict, bytes, bytes, dict, list[dict]]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = _TokenStageTmux()
            telemetry = SeatTokenTelemetry()
            sessions = Sessions(store, tmux=tmux, local_host="hosta")
            ctl = SpawnCtl(
                store,
                sessions,
                tmux=tmux,
                machine=launch.local_machine(
                    "hosta",
                    cwd="/tmp",
                    claude_bin="/bin/echo",
                    codex_bin="/bin/echo",
                ),
                token_telemetry=telemetry,
            )
            intents: list[dict] = []
            record_intent = store.record_spawn_intent

            async def capture_intent(host: str, name: str, payload: dict, *args, **kwargs):
                # record_spawn_intent is now a request+nonce CAS returning bool
                # : forward the new request_id/nonce kwargs and
                # return the underlying result so the caller's CAS check holds.
                intents.append(payload)
                return await record_intent(host, name, payload, *args, **kwargs)

            store.record_spawn_intent = capture_intent  # type: ignore[method-assign]
            await sessions.open("hosta", "legacy", provider="codex")
            first = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract",
                    "session_name": "fresh",
                    "request_id": "fresh-1",
                    "ready_marker": "READY",
                    "provider": "codex",
                    "model": "gpt-5.6-luna",
                    "effort": "max",
                },
                "hosta",
            )
            first_token = tmux.staged[-1][1]
            first_row = await store.fetch_session("hosta", "fresh")
            assert tmux.staged[-1][0] in tmux.commands[-1]
            assert first_token.decode("utf-8") not in tmux.commands[-1]
            server = Server(store=store, sessions=sessions, token_telemetry=telemetry)

            async def auth(token: str | None) -> dict:
                return await server._auth_context(
                    object(),
                    {"from_stream_id": "hosta:fresh", "stream_token": token},
                )

            assert (await auth(first_token.decode("utf-8")))["token_verified"] is True
            assert (await auth("bad"))["reason_code"] == "expired"
            assert (await auth(None))["reason_code"] == "absent"
            await tmux.kill_session("fresh")
            await sessions.mark_closed("hosta", "fresh", "test close")
            assert (await auth(first_token.decode("utf-8")))["reason_code"] == "expired"
            second = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract",
                    "session_name": "fresh",
                    "request_id": "fresh-2",
                    "ready_marker": "READY",
                    "provider": "codex",
                    "model": "gpt-5.6-luna",
                    "effort": "max",
                },
                "hosta",
            )
            second_token = tmux.staged[-1][1]
            assert second_token != first_token
            assert telemetry.snapshot()["counts"]["issuance"] == 2
            assert (await auth(second_token.decode("utf-8")))["token_verified"] is True
            assert (await auth(first_token.decode("utf-8")))["reason_code"] == "expired"
            tmux.ready = False

            async def not_ready(*_args: object, **_kwargs: object) -> bool:
                return False

            ctl._await_marker = not_ready  # type: ignore[method-assign]
            with pytest.raises(VerbError) as exc:
                await ctl.spawn(
                    {"objective": "Exercise the existing spawn contract",
                        "session_name": "aborted",
                        "request_id": "aborted-1",
                        "ready_marker": "READY",
                        "provider": "codex",
                        "model": "gpt-5.6-luna",
                        "effort": "max",
                    },
                    "hosta",
                )
            assert exc.value.code == "boot_not_ready"
            aborted = await store.fetch_session("hosta", "aborted")
            assert aborted is not None and aborted["status"] == "closed"
            assert all(row["session_name"] != "aborted" for row in await store.list_sessions("open"))
            legacy = await store.fetch_session("hosta", "legacy")
            assert first_row is not None and legacy is not None
            assert legacy["token_hash"] is None and legacy["token_hash_version"] is None
            return first, second, first_token, second_token, first_row, intents
        finally:
            store.stop()

    first, second, first_token, second_token, first_row, intents = asyncio.run(run())
    assert first_row["token_hash"] == hashlib.sha256(first_token).hexdigest()
    assert first_row["token_hash_version"] == STREAM_TOKEN_HASH_VERSION
    serialized = json.dumps({"first": first, "second": second, "row": first_row, "intents": intents})
    assert first_token.decode("utf-8") not in serialized
    assert second_token.decode("utf-8") not in serialized


class _BrokenTokenStore:
    async def stream_token_state(self, _token_hash: str) -> dict[str, str] | None:
        raise RuntimeError("token lookup unavailable")


def test_token_verification_reason_codes_and_no_placeholder_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host="hosta")
        token = "tok"
        try:
            await sessions.open("hosta", "parent", provider="claude", pane_status="pane_alive")
            await store.grant_stream_token(
                "hosta",
                "parent",
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                STREAM_TOKEN_HASH_VERSION,
            )
            server = Server(store=store, sessions=sessions)

            async def gate(msg: dict) -> dict:
                context = msg["_auth_context"]
                if not context["token_verified"]:
                    raise VerbError(
                        "routing_integrity_actor_token_unverified",
                        "token verification failed",
                        token_reason_code=context.get("reason_code") or "absent",
                    )
                return {"type": "unpark.ok"}

            server.handlers["seat-token-probe"] = gate

            with caplog.at_level(logging.INFO, logger="chat_streamd_v2.seat_token"):
                cases = [
                    ({"type": "seat-token-probe"}, "absent"),
                    ({"type": "seat-token-probe", "stream_token": 17}, "malformed"),
                    ({"type": "seat-token-probe", "stream_token": "bad"}, "expired"),
                    (
                        {
                            "type": "seat-token-probe",
                            "stream_token": token,
                            "from_stream_id": "hosta:other-seat",
                        },
                        "wrong-seat",
                    ),
                    (
                        {"type": "seat-token-probe", "stream_token": token, "from_stream_id": "hosta:parent"},
                        "verified",
                    ),
                ]
                replies = []
                for message, reason in cases:
                    replies.append((await server._dispatch(json.dumps(message), websocket=object()))[0])
                    if reason == "verified":
                        assert replies[-1]["type"] == "unpark.ok"
                    else:
                        assert replies[-1]["error_code"] == "routing_integrity_actor_token_unverified"
                        assert replies[-1]["token_reason_code"] == reason

                broken = Server(
                    store=_BrokenTokenStore(),
                    sessions=sessions,
                    token_telemetry=server.token_telemetry,
                )
                broken.handlers["seat-token-probe"] = gate
                broken_reply = (
                    await broken._dispatch(
                        json.dumps({"type": "seat-token-probe", "stream_token": token}),
                        websocket=object(),
                    )
                )[0]
                assert broken_reply["token_reason_code"] == "internal-error"

            stats = (await server._dispatch(json.dumps({"type": "daemon.stats"})))[0]
            telemetry = stats["stats"]["seat_tokens"]
            assert telemetry["counts"]["verification_success"] == 1
            assert telemetry["counts"]["verification_failure"] == 4
            assert set(telemetry["failure_reasons"]) == {
                "malformed",
                "expired",
                "wrong-seat",
                "internal-error",
            }
            serialized = json.dumps(telemetry, sort_keys=True)
            assert token not in serialized
            assert token not in caplog.text
        finally:
            store.stop()

    asyncio.run(run())


def test_grant_token_records_issuance_without_recording_plaintext() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host="hosta")
        telemetry = SeatTokenTelemetry()
        try:
            await sessions.open("hosta", "grant-seat", provider="codex", pane_status="pane_alive")
            verbs = UIVerbs(store, sessions, SpawnCtl(store, sessions, token_telemetry=telemetry))
            reply = await verbs.grant_token({"stream_id": "hosta:grant-seat"})
            assert reply["type"] == "grant_token.ok"
            assert telemetry.snapshot()["counts"]["issuance"] == 1
            assert reply["stream_token"] not in json.dumps(telemetry.snapshot(), sort_keys=True)
        finally:
            store.stop()

    asyncio.run(run())
