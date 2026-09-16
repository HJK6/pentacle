"""Native Claude resume contract, lifecycle guards, and recovery evidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import launch
import pytest
import spawnctl as spawnctl_mod
from satellite import Satellite, SatelliteConfig, _DiscoveredPane
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store


RESUME_ID = "531c40b0-706d-47d7-b2f9-24e61b50b216"
LOCAL = launch.local_machine(
    "fixture-host",
    cwd="/tmp/public-test",
    claude_bin="/opt/example/bin/claude",
    codex_bin="/opt/example/bin/codex",
    projects_root="/tmp/projects",
)


class ResumeTmux:
    def __init__(self) -> None:
        self.alive: set[str] = set()
        self.commands: list[str] = []
        self.staged: list[tuple[str, bytes]] = []

    async def has_session(self, name: str) -> bool:
        return name in self.alive

    async def session_state(self, name: str) -> str:
        return "alive" if name in self.alive else "gone"

    async def stage_text(self, path: str, _data: bytes) -> None:
        self.staged.append((path, _data))


def _machine(tmp_path: Path) -> launch.LocalMachine:
    cwd = tmp_path / "agent-workspace"
    cwd.mkdir()
    return launch.local_machine(
        "localhost",
        cwd=str(cwd),
        claude_bin="/bin/echo",
        codex_bin="/bin/echo",
        projects_root=str(tmp_path / ".claude" / "projects"),
        agent_orch_bin_dir="/bin",
    )


def _write_transcript(
    machine: launch.LocalMachine,
    *,
    session_id: str = RESUME_ID,
    embedded_id: str | None = None,
    transcript_cwd: str | None = None,
) -> Path:
    bound_cwd = transcript_cwd or machine.cwd
    path = Path(launch.jsonl_path_for(machine, bound_cwd, session_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "mode", "mode": "normal", "sessionId": embedded_id or session_id},
        {
            "type": "user", "sessionId": embedded_id or session_id, "uuid": "u1",
            "cwd": bound_cwd,
            "message": {"role": "user", "content": "Discuss the Fable Altum client flow."},
        },
        {
            "type": "assistant", "sessionId": embedded_id or session_id, "uuid": "a1",
            "cwd": bound_cwd,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "The original context is available."}],
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


async def _closed_prior(store: Store, path: Path, *, host: str = "localhost") -> dict:
    row = await store.open_session(
        host,
        "v2-original-fable",
        provider="claude",
        claude_session_id=RESUME_ID,
        jsonl_path=str(path),
        role="lead",
        visibility="default",
        title="Fable Altum",
        objective="Preserve the original client-flow discussion",
        objective_source="explicit",
        no_watch=True,
    )
    assert row is not None
    closed = await store.mark_closed(
        host,
        "v2-original-fable",
        closed_at="2026-09-15T00:00:00Z",
        pane_status="pane_dead",
        expected_generation=str(row["session_generation"]),
        close_kind="operator_close",
    )
    assert closed is not None
    return closed


def _install_successful_spawn(ctl: SpawnCtl, sessions: Sessions, tmux: ResumeTmux) -> list[str]:
    commands: list[str] = []

    async def _spawn_fenced(
        host, name, request_id, command, _brief, _ready_marker, _msg, created,
        _creation_uncertain, open_fields, _resolution, _tmux, delivery_receipt,
        _nonce="", *, admission=None, attempt=0, total_attempts=1,
    ):
        del attempt, total_attempts
        commands.append(command)
        created[0] = True
        tmux.alive.add(name)
        session = await sessions.open(
            host, name, **open_fields, pane_pid="4321",
            pane_status="pane_alive", fence=request_id,
        )
        reply = {
            "type": "spawn.ok", "ok": True, "stream_id": f"{host}:{name}",
            "state": "ready", "session": session,
            "initial_prompt_delivery": delivery_receipt,
        }
        if admission is not None and not admission.done():
            admission.set_result(reply)
        return reply

    ctl._spawn_fenced = _spawn_fenced  # type: ignore[method-assign]
    return commands


def test_resume_payload_builds_native_resume_launch_without_fresh_session_id() -> None:
    """The accepted RPC field must control the native provider launch identity."""
    controller = SpawnCtl(
        store=None,
        sessions=None,
        tmux=object(),
        machine=LOCAL,
        hosts=None,
    )

    command, _resolution, overrides = asyncio.run(
        controller._resolve_launch(
            {
                "provider": "claude",
                "model": "claude-fable-5-1",
                "effort": "high",
                "resume_session_id": RESUME_ID,
            },
            "fixture-host",
            "v2-resumed",
        )
    )

    assert f"--resume {RESUME_ID}" in command
    assert "--session-id" not in command
    assert overrides["claude_session_id"] == RESUME_ID
    assert overrides["jsonl_path"].endswith(f"/{RESUME_ID}.jsonl")


def test_fresh_claude_spawn_still_mints_session_id() -> None:
    plan = launch.build_launch(
        LOCAL,
        provider="claude",
        tmux_session="v2-fresh",
        launch_model=None,
        launch_effort=None,
    )
    assert "--session-id" in plan.command
    assert "--resume" not in plan.command
    assert plan.session_id != RESUME_ID


def test_resume_discovers_original_project_cwd_when_machine_default_moved(tmp_path: Path) -> None:
    async def _go() -> tuple[str, str, str]:
        machine = _machine(tmp_path)
        original_cwd = tmp_path / "original-agent-workspace"
        original_cwd.mkdir()
        path = _write_transcript(machine, transcript_cwd=str(original_cwd))
        store = Store(":memory:")
        store.start()
        try:
            tmux = ResumeTmux()
            ctl = SpawnCtl(
                store, Sessions(store, tmux=tmux, local_host="localhost"),
                tmux=tmux, machine=machine,
            )
            _name, found_path, found_cwd, _prior = await ctl._resolve_resume_target(
                {"provider": "claude", "resume_session_id": RESUME_ID},
                "localhost", tmux,
            )
            command, _resolution, _overrides = await ctl._resolve_launch(
                {
                    "provider": "claude", "resume_session_id": RESUME_ID,
                    "_resume_jsonl_path": found_path, "_resume_cwd": found_cwd,
                },
                "localhost", "v2-resume-test",
            )
            return found_path, found_cwd, command
        finally:
            store.stop()

    found_path, found_cwd, command = asyncio.run(_go())
    assert found_path.endswith(f"/{RESUME_ID}.jsonl")
    assert found_cwd == str((tmp_path / "original-agent-workspace").resolve())
    assert command.startswith(f"cd {found_cwd} && ")


def test_remote_resume_probe_binds_unique_transcript_and_original_cwd(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    original_cwd = tmp_path / "remote-original-workspace"
    original_cwd.mkdir()
    path = _write_transcript(machine, transcript_cwd=str(original_cwd))
    result = subprocess.run(
        [
            sys.executable, "-c", spawnctl_mod._REMOTE_RESUME_PROBE,
            machine.projects_root, RESUME_ID,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence = json.loads(result.stdout)
    assert evidence == {
        "state": "ok",
        "path": str(path.resolve()),
        "cwd": str(original_cwd.resolve()),
    }


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ({"provider": "codex", "resume_session_id": RESUME_ID}, "resume_unsupported_provider"),
        ({"provider": "claude", "resume_session_id": RESUME_ID, "parent_stream_id": "h:p"}, "resume_conflicts_with_lineage"),
        ({"provider": "claude", "resume_session_id": RESUME_ID, "handoff": True}, "resume_conflicts_with_lineage"),
        ({"provider": "claude", "resume_session_id": RESUME_ID, "session_name": "chosen"}, "resume_conflicts_with_identity"),
        ({"provider": "claude", "resume_session_id": RESUME_ID, "command": "claude --resume"}, "resume_requires_native_launch"),
    ],
)
def test_resume_rejects_provider_lineage_and_launcher_overrides(
    tmp_path: Path, message: dict, code: str,
) -> None:
    async def _go() -> str:
        store = Store(":memory:")
        store.start()
        try:
            machine = _machine(tmp_path)
            _write_transcript(machine)
            tmux = ResumeTmux()
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host="localhost"), tmux=tmux, machine=machine)
            with pytest.raises(VerbError) as raised:
                await ctl._resolve_resume_target(message, "localhost", tmux)
            return raised.value.code
        finally:
            store.stop()

    assert asyncio.run(_go()) == code


def test_resume_rejects_missing_foreign_cross_host_and_live_targets(tmp_path: Path) -> None:
    async def _go() -> list[str]:
        codes: list[str] = []
        machine = _machine(tmp_path)
        tmux = ResumeTmux()
        store = Store(":memory:")
        store.start()
        try:
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host="localhost"), tmux=tmux, machine=machine)
            msg = {"provider": "claude", "resume_session_id": RESUME_ID}
            with pytest.raises(VerbError) as raised:
                await ctl._resolve_resume_target(msg, "localhost", tmux)
            codes.append(raised.value.code)

            _write_transcript(
                machine,
                embedded_id="11111111-1111-4111-8111-111111111111",
            )
            with pytest.raises(VerbError) as raised:
                await ctl._resolve_resume_target(msg, "localhost", tmux)
            codes.append(raised.value.code)

            path = _write_transcript(machine)
            await _closed_prior(store, path, host="other-host")
            with pytest.raises(VerbError) as raised:
                await ctl._resolve_resume_target(msg, "localhost", tmux)
            codes.append(raised.value.code)
        finally:
            store.stop()

        live_store = Store(":memory:")
        live_store.start()
        try:
            await live_store.open_session(
                "localhost", "v2-live", provider="claude",
                claude_session_id=RESUME_ID, jsonl_path=str(path),
            )
            ctl = SpawnCtl(
                live_store,
                Sessions(live_store, tmux=tmux, local_host="localhost"),
                tmux=tmux,
                machine=machine,
            )
            with pytest.raises(VerbError) as raised:
                await ctl._resolve_resume_target(msg, "localhost", tmux)
            codes.append(raised.value.code)
        finally:
            live_store.stop()
        return codes

    assert asyncio.run(_go()) == [
        "resume_transcript_not_found",
        "resume_foreign_target",
        "resume_target_host_mismatch",
        "resume_session_already_live",
    ]


def test_retained_row_resume_reuses_identity_and_descriptive_metadata(tmp_path: Path) -> None:
    async def _go() -> tuple[dict, list[str]]:
        machine = _machine(tmp_path)
        path = _write_transcript(machine)
        store = Store(":memory:")
        store.start()
        try:
            prior = await _closed_prior(store, path)
            tmux = ResumeTmux()
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            ctl = SpawnCtl(store, sessions, tmux=tmux, machine=machine)
            commands = _install_successful_spawn(ctl, sessions, tmux)
            await ctl.spawn(
                {
                    "provider": "claude", "model": "claude-fable-5-1",
                    "effort": "high", "resume_session_id": RESUME_ID,
                    "request_id": "resume-retained",
                    "objective": "A caller must not replace retained metadata",
                },
                "localhost",
            )
            row = await store.fetch_session("localhost", "v2-original-fable")
            assert row is not None
            assert row["created_at"] != prior["created_at"]
            return row, commands
        finally:
            store.stop()

    row, commands = asyncio.run(_go())
    assert row["status"] == "open"
    assert row["claude_session_id"] == RESUME_ID
    assert row["title"] == "Fable Altum"
    assert row["objective"] == "Preserve the original client-flow discussion"
    assert row["role"] == "lead"
    assert row["no_watch"] is True
    assert len(commands) == 1
    assert f"--resume {RESUME_ID}" in commands[0]
    assert "--session-id" not in commands[0]


def test_purged_row_fallback_uses_normal_lifecycle_and_surfaces_history(tmp_path: Path) -> None:
    async def _go() -> tuple[str, dict, str]:
        machine = _machine(tmp_path)
        path = _write_transcript(machine)
        store = Store(":memory:")
        store.start()
        try:
            tmux = ResumeTmux()
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            ctl = SpawnCtl(store, sessions, tmux=tmux, machine=machine)
            commands = _install_successful_spawn(ctl, sessions, tmux)
            reply = await ctl.spawn(
                {
                    "provider": "claude", "model": "claude-fable-5-1",
                    "effort": "high", "resume_session_id": RESUME_ID,
                    "request_id": "resume-purged", "objective": "Restore a retained transcript",
                },
                "localhost",
            )
            name = reply["stream_id"].split(":", 1)[1]
            row = await store.fetch_session("localhost", name)
            assert row is not None
            return name, row, commands[0]
        finally:
            store.stop()

    name, row, command = asyncio.run(_go())
    assert name == f"v2-resume-{RESUME_ID}"
    assert row["status"] == "open"
    assert row["claude_session_id"] == RESUME_ID
    assert row["title"] is None and row["parent_stream_id"] is None
    assert f"--resume {RESUME_ID}" in command

    satellite = Satellite(SatelliteConfig(
        host="localhost", checkout=str(tmp_path), history_bytes=-1,
        max_events_per_pass=20, max_read_bytes=1_000_000,
    ))
    events, _offsets, _capped = satellite._collect({
        name: _DiscoveredPane(
            session_name=name, provider="claude", transcript_path=row["jsonl_path"], pane_pid=4321,
        ),
    })
    assert ("USER", "Discuss the Fable Altum client flow.") in {
        (event["kind"], event["text"]) for event in events
    }
    assert ("ASSIST_TEXT", "The original context is available.") in {
        (event["kind"], event["text"]) for event in events
    }


def test_concurrent_resume_admits_only_one_native_launch(tmp_path: Path) -> None:
    async def _go() -> tuple[list[object], int]:
        machine = _machine(tmp_path)
        _write_transcript(machine)
        store = Store(":memory:")
        store.start()
        try:
            tmux = ResumeTmux()
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            ctl = SpawnCtl(store, sessions, tmux=tmux, machine=machine)
            commands = _install_successful_spawn(ctl, sessions, tmux)
            base = {
                "provider": "claude", "model": "claude-fable-5-1",
                "effort": "high", "resume_session_id": RESUME_ID,
                "objective": "Restore the original transcript",
            }
            results = await asyncio.gather(
                ctl._spawn_guarded({**base, "request_id": "resume-a"}, "localhost"),
                ctl._spawn_guarded({**base, "request_id": "resume-b"}, "localhost"),
                return_exceptions=True,
            )
            return results, len(commands)
        finally:
            store.stop()

    results, launch_count = asyncio.run(_go())
    assert launch_count == 1
    assert sum(isinstance(result, dict) and result.get("ok") for result in results) == 1
    errors = [result for result in results if isinstance(result, VerbError)]
    assert len(errors) == 1 and errors[0].code == "resume_session_already_live"


def test_cross_controller_loser_cannot_overwrite_winner_token(tmp_path: Path) -> None:
    async def _go() -> tuple[list[object], list[tuple[str, bytes]]]:
        machine = _machine(tmp_path)
        _write_transcript(machine)
        store = Store(":memory:")
        store.start()
        try:
            tmux = ResumeTmux()
            sessions_a = Sessions(store, tmux=tmux, local_host="localhost")
            sessions_b = Sessions(store, tmux=tmux, local_host="localhost")
            ctl_a = SpawnCtl(store, sessions_a, tmux=tmux, machine=machine)
            ctl_b = SpawnCtl(store, sessions_b, tmux=tmux, machine=machine)
            winner_entered = asyncio.Event()
            release_winner = asyncio.Event()

            async def _winner_fenced(
                host, name, request_id, _command, _brief, _ready_marker, _msg, created,
                _creation_uncertain, open_fields, _resolution, _tmux, delivery_receipt,
                _nonce="", *, admission=None, attempt=0, total_attempts=1,
            ):
                del attempt, total_attempts
                winner_entered.set()
                await release_winner.wait()
                created[0] = True
                tmux.alive.add(name)
                session = await sessions_a.open(
                    host, name, **open_fields, pane_pid="4321",
                    pane_status="pane_alive", fence=request_id,
                )
                return {
                    "type": "spawn.ok", "ok": True, "stream_id": f"{host}:{name}",
                    "state": "ready", "session": session,
                    "initial_prompt_delivery": delivery_receipt,
                }

            ctl_a._spawn_fenced = _winner_fenced  # type: ignore[method-assign]
            base = {
                "provider": "claude", "model": "claude-fable-5-1",
                "effort": "high", "resume_session_id": RESUME_ID,
                "objective": "Restore the original transcript",
            }
            winner = asyncio.create_task(ctl_a._spawn_guarded(
                {**base, "request_id": "cross-controller-winner"}, "localhost",
            ))
            await winner_entered.wait()
            loser = await asyncio.gather(
                ctl_b._spawn_guarded(
                    {**base, "request_id": "cross-controller-loser"}, "localhost",
                ),
                return_exceptions=True,
            )
            release_winner.set()
            winner_result = await winner
            return [winner_result, *loser], list(tmux.staged)
        finally:
            store.stop()

    results, staged = asyncio.run(_go())
    assert isinstance(results[0], dict) and results[0]["ok"] is True
    assert isinstance(results[1], VerbError)
    assert results[1].code == "stream_id_unavailable"
    assert len(staged) == 1
    assert staged[0][0].endswith(".token") and staged[0][1]


def test_launch_failure_releases_resume_reservation_for_new_request(tmp_path: Path) -> None:
    async def _go() -> tuple[list[dict], dict, int]:
        machine = _machine(tmp_path)
        path = _write_transcript(machine)
        store = Store(":memory:")
        store.start()
        try:
            await _closed_prior(store, path)
            tmux = ResumeTmux()
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            ctl = SpawnCtl(store, sessions, tmux=tmux, machine=machine)
            attempts = 0

            async def _spawn_fenced(
                host, name, request_id, _command, _brief, _ready_marker, _msg, created,
                _creation_uncertain, open_fields, _resolution, _tmux, delivery_receipt,
                _nonce="", *, admission=None, attempt=0, total_attempts=1,
            ):
                nonlocal attempts
                del attempt, total_attempts
                attempts += 1
                if attempts == 1:
                    raise VerbError("tmux_create_failed", "synthetic launch failure")
                created[0] = True
                tmux.alive.add(name)
                session = await sessions.open(
                    host, name, **open_fields, pane_pid="4321",
                    pane_status="pane_alive", fence=request_id,
                )
                return {
                    "type": "spawn.ok", "ok": True, "stream_id": f"{host}:{name}",
                    "state": "ready", "session": session,
                    "initial_prompt_delivery": delivery_receipt,
                }

            ctl._spawn_fenced = _spawn_fenced  # type: ignore[method-assign]
            base = {
                "provider": "claude", "model": "claude-fable-5-1",
                "effort": "high", "resume_session_id": RESUME_ID,
                "objective": "Restore the original transcript",
            }
            with pytest.raises(VerbError) as raised:
                await ctl._spawn_guarded({**base, "request_id": "failed-launch"}, "localhost")
            assert raised.value.code == "tmux_create_failed"
            after_failure = await store.reservations(include_expired=True)
            reply = await ctl._spawn_guarded(
                {**base, "request_id": "replacement-launch"}, "localhost",
            )
            return after_failure, reply, attempts
        finally:
            store.stop()

    reservations, reply, attempts = asyncio.run(_go())
    assert reservations == []
    assert reply["ok"] is True and reply["stream_id"] == "localhost:v2-original-fable"
    assert attempts == 2
