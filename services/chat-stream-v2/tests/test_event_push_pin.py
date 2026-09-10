"""Unit coverage for the explicit, manual event.push target-pin window step."""
from __future__ import annotations

import asyncio
import json

import pytest

from tools import event_push_pin
from store import (
    EVENT_PUSH_TARGET_SHA_KEY,
    EVENT_PUSH_TARGET_SHA_PREVIOUS_KEY,
    Store,
)


PREVIOUS = "a" * 40
TARGET = "b" * 40


def test_stage_records_previous_before_target_and_reads_back() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.put(EVENT_PUSH_TARGET_SHA_KEY, PREVIOUS)
            writes: list[tuple[str, str]] = []
            put = store.put

            async def traced_put(key: str, value: str) -> None:
                writes.append((key, value))
                await put(key, value)

            store.put = traced_put  # type: ignore[method-assign]
            assert await store.stage_event_push_target_sha(TARGET) == PREVIOUS
            assert writes == [
                (EVENT_PUSH_TARGET_SHA_PREVIOUS_KEY, PREVIOUS),
                (EVENT_PUSH_TARGET_SHA_KEY, TARGET),
            ]
            assert await store.get(EVENT_PUSH_TARGET_SHA_PREVIOUS_KEY) == PREVIOUS
            assert await store.get(EVENT_PUSH_TARGET_SHA_KEY) == TARGET
        finally:
            store.stop()

    asyncio.run(go())


def test_stage_rejects_invalid_sha_but_ignores_retired_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            with pytest.raises(ValueError, match="exact 40-character"):
                await store.stage_event_push_target_sha("not-a-sha")
            monkeypatch.setenv("PENTACLE_EVENT_PUSH_TARGET_SHA", TARGET)
            assert await store.stage_event_push_target_sha(TARGET) is None
            assert await store.get(EVENT_PUSH_TARGET_SHA_KEY) == TARGET
        finally:
            store.stop()

    asyncio.run(go())


def test_rollback_restores_the_durable_captured_previous_pin() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.put(EVENT_PUSH_TARGET_SHA_KEY, PREVIOUS)
            await store.stage_event_push_target_sha(TARGET)
            assert await store.rollback_event_push_target_sha() == PREVIOUS
            assert await store.get(EVENT_PUSH_TARGET_SHA_KEY) == PREVIOUS
        finally:
            store.stop()

    asyncio.run(go())


def test_rollback_refuses_without_a_captured_previous_pin() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            with pytest.raises(RuntimeError, match="no captured previous"):
                await store.rollback_event_push_target_sha()
        finally:
            store.stop()

    asyncio.run(go())


@pytest.mark.parametrize("rows, accepted", [
    ([{"host": "workstation", "class": "untested", "reason": "quota_exhausted", "reset_at": "2030-01-01T12:00:00Z"}], True),
    ([], True),
    ([{"host": "workstation", "class": "critical", "reason": "spawn_failed"}], False),
    ([{"host": "workstation", "class": "untested", "reason": "host_offline"}], False),
    ([{"host": "workstation", "class": "untested", "reason": "quota_exhausted"},
      {"host": "workstation", "class": "critical", "reason": "spawn_failed"}], False),
])
def test_stage_consumes_actual_smoke_exit_and_json_contract(tmp_path, monkeypatch, capsys, rows, accepted):
    """Use actual smoke.main output; only its provider-spawning matrix is isolated."""
    import contextlib
    import io
    import subprocess
    from tools import spawn_fleet_smoke

    monkeypatch.setattr(event_push_pin, "SATELLITE_HOSTS", ("workstation",))
    monkeypatch.setattr(spawn_fleet_smoke, "run_matrix", lambda *a, **k: rows)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = spawn_fleet_smoke.main([])
    smoke = subprocess.CompletedProcess([], code, output.getvalue(), "")
    monkeypatch.setattr(event_push_pin.subprocess, "run", lambda *a, **k: smoke)

    async def go():
        db = str(tmp_path / "sessions.db")
        store = Store(db)
        store.start()
        try:
            await store.put("event_push.runtime.workstation", json.dumps({
                "sha": TARGET, "pid": 4321, "observed_at": "2030-01-01T00:00:00Z",
                "observed_at_epoch": 9_999_999_999,
            }))
        finally:
            store.stop()
        if not accepted:
            with pytest.raises(RuntimeError):
                await event_push_pin._run(db, stage=TARGET, rollback=False)
            return
        result = await event_push_pin._run(db, stage=TARGET, rollback=False)
        assert result["action"] == "stage" and result["readback"] == TARGET
        assert result["quota_exhausted"] == rows

    asyncio.run(go())
    if accepted and rows:
        assert "quota exhausted" in capsys.readouterr().err


def test_stage_fails_closed_with_last_stale_runtime_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(event_push_pin, "SATELLITE_HOSTS", ("workstation",))
    async def go() -> None:
        db = str(tmp_path / "sessions.db")
        store = Store(db)
        store.start()
        try:
            for host in event_push_pin.SATELLITE_HOSTS:
                await store.put(
                    f"event_push.runtime.{host}",
                    json.dumps({
                        "sha": PREVIOUS,
                        "pid": 4321,
                        "observed_at": "2026-08-31T00:00:00Z",
                        "observed_at_epoch": 1,
                    }),
                )
        finally:
            store.stop()

        monkeypatch.setattr(event_push_pin, "SATELLITE_READBACK_DEADLINE_SECONDS", 0.01)
        with pytest.raises(RuntimeError) as raised:
            await event_push_pin._run(db, stage=TARGET, rollback=False)
        message = str(raised.value)
        assert TARGET in message
        assert PREVIOUS in message
        assert '"process_state": "connected"' in message

    asyncio.run(go())


@pytest.mark.parametrize("returncode, output", [
    (0, ""), (2, "not-json"), (0, "[]"),
    (2, '{"ok":false,"status":"UNTESTED","failures":[],"untested":[]}'),
    (0, '{"ok":true,"status":"PASS","failures":[{"reason":"spawn_failed"}],"untested":[]}'),
    (2, '{"ok":false,"status":"UNTESTED","failures":[],"untested":[null]}'),
    (1, '{"ok":true,"status":"PASS","failures":[],"untested":[]}'),
])
def test_smoke_malformed_or_contradictory_results_fail_closed(returncode, output):
    import subprocess
    with pytest.raises(RuntimeError, match="post-deploy spawn smoke failed"):
        event_push_pin._smoke_quota_note(subprocess.CompletedProcess([], returncode, output, ""))
