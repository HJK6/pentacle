from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import usage_state
from usage_publisher import UsageStatePublisher
from usage_state import UsageStateStore


NOW = "2026-09-02T00:00:00Z"


def _row(identifier: str, label: str, pct: int | None) -> dict:
    return {
        "id": identifier, "label": label, "pct": pct,
        "resets_text": "Mar 16 at 7pm (America/Chicago)" if pct is not None else None,
        "resets_at_iso": None, "probed_at": None, "upstream_reported_at": None,
    }


def _health() -> dict:
    return {
        "outcome": "ok", "attempted_at": NOW, "probed_at": NOW,
        "upstream_reported_at": NOW, "stale_after_seconds": 600, "error": None,
    }


def _state(store: UsageStateStore) -> list[dict]:
    rows = [_row("claude", "Claude", 40), _row("fable", "Fable", 10)]
    codex = _row("codex", "Codex", 42)
    codex.update(resets_at_iso="2026-08-23T00:00:00Z", upstream_reported_at=NOW, probed_at=NOW)
    store.save(rows, _health(), codex_lkg=codex, codex_health=_health())
    return [*rows, codex]


def test_publisher_emits_three_row_golden_frame_and_keeps_hello_health(tmp_path: Path) -> None:
    state_path = tmp_path / "usage_state.json"
    _state(UsageStateStore(state_path))
    # The golden frame is the validated state read back — byte-order included.
    loaded = UsageStateStore(state_path).load()
    expected_limits = [*loaded.lkg, loaded.codex_lkg]
    frames: list[dict] = []

    async def broadcast(frame: dict) -> None:
        frames.append(frame)

    publisher = UsageStatePublisher(broadcast, state_path=state_path)
    assert asyncio.run(publisher.publish_if_changed())
    expected = {"type": "limits.update", "limits": expected_limits}
    # Byte-golden: exact key order, no sort_keys normalization.
    assert json.dumps(frames, separators=(",", ":")) == json.dumps(
        [expected], separators=(",", ":")
    )
    # All three rows share one canonical key order (the codex row must not drift).
    assert list(frames[0]["limits"][2].keys()) == list(frames[0]["limits"][0].keys())
    assert publisher.health_snapshot() == {"schema_version": 1, "claude": _health()}

    _state(UsageStateStore(state_path))
    os.utime(state_path, None)
    assert not asyncio.run(publisher.publish_if_changed())


def test_publisher_never_writes_and_v1_normalizes_codex_to_never(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "usage_state.json"
    UsageStateStore(state_path).save([_row("claude", "Claude", 40), _row("fable", "Fable", 10)], _health())
    # The store binds fsync_fn=os.fsync at import time, so patching
    # usage_state.os.fsync cannot fire. Patch save() itself so ANY write attempt
    # by the publisher fails the test.
    def _forbidden_save(*_args, **_kwargs):
        raise AssertionError("publisher wrote usage state")

    monkeypatch.setattr(usage_state.UsageStateStore, "save", _forbidden_save)
    frames: list[dict] = []

    async def broadcast(frame: dict) -> None:
        frames.append(frame)

    assert asyncio.run(UsageStatePublisher(broadcast, state_path=state_path).publish_if_changed())
    assert [row["id"] for row in frames[0]["limits"]] == ["claude", "fable", "codex"]
    assert frames[0]["limits"][-1]["pct"] is None


def test_publisher_holds_prior_frame_on_unreadable_state(tmp_path: Path) -> None:
    state_path = tmp_path / "usage_state.json"
    _state(UsageStateStore(state_path))
    frames: list[dict] = []

    async def broadcast(frame: dict) -> None:
        frames.append(frame)

    publisher = UsageStatePublisher(broadcast, state_path=state_path)
    assert asyncio.run(publisher.publish_if_changed())
    good = publisher.snapshot()
    good_health = publisher.health_snapshot()

    # Corrupt the file: load() returns the empty sentinel (health is None).
    state_path.write_text("{ not json", encoding="utf-8")
    os.utime(state_path, None)
    assert not asyncio.run(publisher.publish_if_changed())
    assert len(frames) == 1  # no degraded republish
    assert publisher.snapshot() == good  # prior frame held
    assert publisher.health_snapshot() == good_health  # limits_health not dropped
