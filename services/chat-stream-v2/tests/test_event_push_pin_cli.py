"""The manual pin writer cannot stage an untagged candidate."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import time

import pytest


SERVICE_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("event_push_pin", SERVICE_DIR / "tools" / "event_push_pin.py")
assert SPEC and SPEC.loader
event_push_pin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(event_push_pin)
TARGET = "b" * 40


def _seed_runtime(db: Path) -> None:
    async def go() -> None:
        store = event_push_pin.Store(str(db))
        store.start()
        try:
            for host in event_push_pin.SATELLITE_HOSTS:
                await store.put(
                    f"event_push.runtime.{host}",
                    json.dumps({
                        "sha": TARGET,
                        "pid": 4321,
                        "observed_at": "2026-08-31T00:00:00Z",
                        "observed_at_epoch": time.time() + 60,
                    }),
                )
        finally:
            store.stop()

    asyncio.run(go())


def test_stage_requires_gate_tag_before_the_store_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verified: list[str] = []
    monkeypatch.setattr(event_push_pin, "_require_gate_passed_sha", verified.append)
    monkeypatch.setattr(
        event_push_pin.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    db = tmp_path / "sessions.db"
    _seed_runtime(db)

    assert event_push_pin.main(["--db", str(db), "--stage", TARGET]) == 0
    assert verified == [TARGET]


def test_stage_refuses_an_untagged_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_candidate: str) -> None:
        raise RuntimeError("candidate has no verified v2-gate tag")

    monkeypatch.setattr(event_push_pin, "_require_gate_passed_sha", refuse)
    with pytest.raises(SystemExit) as raised:
        event_push_pin.main(["--db", str(tmp_path / "sessions.db"), "--stage", TARGET])
    assert raised.value.code == 2
