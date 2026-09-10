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


def test_stage_treats_quota_exhausted_smoke_as_non_blocking(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 (pin): a post-deploy fleet smoke that exits 0 with only
    `quota_exhausted` cells does NOT fail the pin (does not block, does not count
    as a daemon failure); the exhausted host surfaces on the returned record."""
    import subprocess as _subprocess

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
                        "sha": TARGET, "pid": 4321,
                        "observed_at": "2026-09-05T00:00:00Z",
                        "observed_at_epoch": 9_999_999_999,
                    }),
                )
        finally:
            store.stop()

        quota_stdout = json.dumps({
            "ok": True, "failures": [],
            "quota_exhausted": [{"host": "hostb", "reset_at": "Jan 1st, 2030 12:00 PM"}],
        })
        monkeypatch.setattr(
            event_push_pin.subprocess, "run",
            lambda *_a, **_k: _subprocess.CompletedProcess([], 0, quota_stdout, ""),
        )
        result = await event_push_pin._run(db, stage=TARGET, rollback=False)
        assert result["action"] == "stage"
        assert result["quota_exhausted"] == [{"host": "hostb", "reset_at": "Jan 1st, 2030 12:00 PM"}]

    asyncio.run(go())


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
