"""Fail-first proof-launch regression through the real intake pass."""
import os

import pytest
import ingest as module
import prockill
from ingest import _close_stream
from tmux_transport import Tmux
from test_ingest_provider_root_binding import Harness, Store, PID, BIRTH, EXE, write_log


@pytest.mark.asyncio
async def test_proven_idle_pass_does_not_launch_identity_probes(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_INGEST_IDLE_PROOF_S", "30")
    path = tmp_path / ".codex" / "sessions" / "idle.jsonl"
    write_log(path)
    store = Store(":memory:")
    store.start()
    reader = None
    launches = []
    try:
        harness = Harness(store, monkeypatch, path)
        await harness.open()

        async def run(self, *args, **kwargs):
            assert args[0] == "list-panes"
            launches.append("tmux")
            return 0, PID + "\n"

        async def record(pid):
            launches.append("ps")
            return {"pid": int(pid), "uid": os.getuid(), "start_id": BIRTH, "command": EXE}

        async def tree(pid):
            return [pid]

        monkeypatch.setattr(Tmux, "run", run)
        monkeypatch.setattr(prockill, "process_record", record)
        monkeypatch.setattr(module, "process_record", prockill.process_record)
        monkeypatch.setattr(module, "process_tree", tree)
        reader = harness.ingest()
        reader.tmux = Tmux()
        assert await reader.run_pass() == 1
        state = reader._streams["h:v2-root"]
        assert state.fd is not None and state.offset == path.stat().st_size
        assert state.replay_complete
        launches.clear()
        assert await reader.run_pass() == 0
        assert launches == [], f"proven idle seat still launched {len(launches)} probes: {launches}"
        assert not reader._replay_backlog()
    finally:
        if reader is not None:
            for state in reader._streams.values():
                _close_stream(state)
        store.stop()
