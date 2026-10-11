"""Fixture stage of the cold-recovery drill: the restore runs against a seeded state dir."""
from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from store import Store

DRILL = Path(__file__).resolve().parents[3] / "scripts" / "recovery_drill.py"
spec = importlib.util.spec_from_file_location("recovery_drill", DRILL)
recovery_drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery_drill)

CHAT = "fixture-chat:assistant"


def _seed(state_dir: Path) -> str:
    async def run():
        store = Store(str(state_dir / "sessions.db"))
        store.start()
        try:
            primary = await store.open_session("fixture-a", "assistant", provider="codex", role="assistant",
                                               effective_model="gpt-6-sol", effective_effort="high",
                                               pane_status="pane_alive")
            return primary["session_generation"]
        finally:
            if hasattr(store, "close"):
                store.close()
    return asyncio.run(run())


def _seed_binding(state_dir: Path, generation: str) -> None:
    """A captured store carries a durable, name-keyed assistant binding row (DDL at the pin)."""
    conn = sqlite3.connect(state_dir / "sessions.db")
    try:
        conn.execute("INSERT INTO v2_assistant_direct_binding (name, stream_id, generation, revision, updated_at) "
                     "VALUES ('fixture-binding', 'fixture-a:assistant', ?, 1, '2026-10-11T00:00:00Z')", (generation,))
        conn.commit()
    finally:
        conn.close()


def test_restore_stage_answers_one_message_and_tears_down(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _seed_binding(state, _seed(state))
    result = asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={}))
    # (a) history newest id equals the direct read
    assert result["daemon_newest_seq"] == result["direct_newest_seq"]
    # (b) lanes and reports are read and reported (fixture store has none)
    assert result["direct_reports"] == 0 and result["lanes_reports_nonempty"] is False
    # (c) rebind moved the binding to the drill holder
    assert result["binding_after"]["stream_id"] == recovery_drill.DRILL_STREAM
    # (d) one correlated reply, timed
    assert result["time_to_first_reply_s"] > 0
    # (e) port closed and copy dir removed
    assert result["port_closed"] is True
    assert result["state_dir_removed"] is True and not state.exists()


@pytest.mark.parametrize("key", recovery_drill.PRODUCER_KEYS)
def test_refuses_any_producer_or_push_key(tmp_path, key):
    # (f) each key individually blocks the start; nothing is opened
    with pytest.raises(recovery_drill.DrillRefused):
        asyncio.run(recovery_drill.run_drill(tmp_path, composite_stream=CHAT, binding_name="fixture-binding", env={key: "x"},
                                             remove_state_dir=False))
    assert not (tmp_path / "sessions.db").exists()


def test_refuses_real_host_roster(tmp_path):
    with pytest.raises(recovery_drill.DrillRefused):
        asyncio.run(recovery_drill.run_drill(tmp_path, composite_stream=CHAT, binding_name="fixture-binding", env={},
                                             roster={"host-a"}, remove_state_dir=False))
