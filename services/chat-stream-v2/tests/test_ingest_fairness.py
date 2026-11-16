"""Ingest scheduling contracts, observed through actual run_pass attempts."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ingest import Ingest, IngestConfig, _close_stream
from sessions import Sessions
from store import Store
from submission_events import DurableUserEventProof, EventWatermark


def _pump(names, consume):
    inventory = SimpleNamespace(names=list(names))
    inventory.list_open = lambda: [
        {"stream_id": "h:" + name, "host": "h", "provider": "claude"}
        for name in inventory.names
    ]
    pump = Ingest(None, inventory, None, None, local_host="h", recent_limit=500,
                  config=IngestConfig())
    attempts = []

    async def ingest(row, state, budget):
        name = row["stream_id"].split(":")[1]
        attempts.append((name, budget))
        return consume(name, budget)

    pump._ingest_stream = ingest
    return pump, inventory, attempts


@pytest.mark.parametrize("size", [1, 2, 5, 28])
def test_every_eligible_row_gets_service_within_n_passes(size):
    async def go():
        names = [str(i) for i in range(size)]
        pump, _, attempts = _pump(names, lambda name, budget: budget)
        for _ in range(size * 2):
            assert await pump.run_pass() == 500
        assert attempts == [(name, 500) for name in names * 2]
    asyncio.run(go())


def test_shared_cap_and_single_visit_per_snapshot():
    async def go():
        pump, _, attempts = _pump("abcd", lambda name, budget: min(300, budget))
        for _ in range(2):
            assert await pump.run_pass() == 500
        assert attempts == [("a", 500), ("b", 200), ("c", 500), ("d", 200)]
        pump._ingest_stream = lambda *args: asyncio.sleep(0, result=0)
        assert await pump.run_pass() == 0
    asyncio.run(go())


@pytest.mark.parametrize("removed", [("a",), ("a", "b"), ("a", "b", "c")])
def test_removal_uses_surviving_successor_before_new_head(removed):
    async def go():
        pump, inventory, attempts = _pump("abcd", lambda name, budget: budget)
        await pump.run_pass()  # a consumes the cap
        survivors = [name for name in "abcd" if name not in removed]
        inventory.names = ["new-head"] + survivors
        await pump.run_pass()
        assert attempts[-1] == (survivors[0], 500)
        # Remove that successor too; preserve forward progress again.
        inventory.names.remove(survivors[0])
        await pump.run_pass()
        expected = survivors[1] if len(survivors) > 1 else "new-head"
        assert attempts[-1] == (expected, 500)
    asyncio.run(go())


def test_insertion_and_reorder_use_identity_not_old_index():
    async def go():
        pump, inventory, attempts = _pump("abc", lambda name, budget: budget)
        await pump.run_pass()
        inventory.names = ["x", "c", "a", "b"]
        await pump.run_pass()
        assert attempts[-1] == ("b", 500)
        await pump.run_pass()
        assert attempts[-1] == ("x", 500)
    asyncio.run(go())


@pytest.mark.parametrize("last_result", ["zero", "failure", "backoff"])
def test_frontier_advances_on_zero_failure_and_backoff(last_result):
    async def go():
        mode = ["full"]
        def consume(name, budget):
            if mode[0] == "full":
                return budget
            if last_result == "failure" and name == "a":
                raise OSError("owned fixture read failure")
            return 0
        pump, inventory, attempts = _pump("abc", consume)
        await pump.run_pass()  # frontier a
        mode[0] = "empty"
        if last_result == "backoff":
            pump._streams["h:a"].next_attempt_monotonic = float("inf")
        attempts.clear()
        assert await pump.run_pass() == 0  # visits b, c, then a
        assert [name for name, _ in attempts] == (
            ["b", "c"] if last_result == "backoff" else ["b", "c", "a"]
        )
        # Make a eligible again and move it: the next pass starts after its
        # identity even if the previous visit inserted nothing or skipped I/O.
        pump._streams["h:a"].next_attempt_monotonic = 0
        inventory.names = ["c", "a", "b"]
        attempts.clear()
        await pump.run_pass()
        assert [name for name, _ in attempts] == ["b", "c", "a"]
    asyncio.run(go())


def test_remote_and_composite_rows_never_acquire_service():
    async def go():
        pump, inventory, attempts = _pump("ab", lambda name, budget: 0)
        original = inventory.list_open
        inventory.list_open = lambda: original() + [
            {"stream_id": "remote:x", "host": "remote", "provider": "claude"},
            {"stream_id": "h:composite", "host": "h", "provider": "composite"},
        ]
        await pump.run_pass()
        assert attempts == [("a", 500), ("b", 500)]
        inventory.names = []
        assert await pump.run_pass() == 0
        inventory.names = ["fresh"]
        await pump.run_pass()
        assert attempts[-1] == ("fresh", 500)
    asyncio.run(go())


def test_mixed_replay_proves_fresh_users_and_finishes_old_history(tmp_path):
    async def go():
        store = Store(tmp_path / "sessions.db")
        store.start()
        sessions = Sessions(store, tmux=None, local_host="h")
        frames = []
        async def broadcast(frame):
            frames.append(frame)
        pump = Ingest(store, sessions, None, broadcast, local_host="h",
                      recent_limit=500, config=IngestConfig(max_events_per_pass=3))
        paths = {}
        try:
            for age in ("old", "fresh"):
                for provider in ("claude", "codex"):
                    name = age + "-" + provider
                    native = "native-" + name
                    path = tmp_path / (name + ".jsonl")
                    records = []
                    if provider == "codex":
                        records.append({"type": "session_meta", "payload": {"id": native}})
                    for i in range(13 if age == "old" else 1):
                        text = f"historical {i}" if age == "old" else "fresh prompt"
                        if provider == "claude":
                            records.append({"type": "user", "uuid": str(i), "sessionId": native,
                                            "message": {"role": "user", "content": text}})
                        else:
                            records.append({"type": "response_item", "payload": {
                                "type": "message", "id": str(i), "role": "user",
                                "content": [{"type": "input_text", "text": text}]}})
                    path.write_text("".join(json.dumps(record) + "\n" for record in records))
                    await sessions.open("h", name, provider=provider, jsonl_path=str(path),
                                        pane_pid="4321", claude_session_id=native,
                                        session_generation="gen-" + name)
                    paths["h:" + name] = path
            proof = DurableUserEventProof(store, local_host="h")
            for _ in range(3):
                assert await pump.run_pass() <= 3
            for provider in ("claude", "codex"):
                sid = "h:fresh-" + provider
                assert (await proof.lookup(sid, expected_text="fresh prompt",
                                          watermark=EventWatermark(sid, 0, "reachable"))).proven
            for _ in range(30):
                assert await pump.run_pass() <= 3
                if all(pump._streams[sid].offset == path.stat().st_size
                       for sid, path in paths.items()):
                    break
            else:
                pytest.fail("finite mixed history did not finish")
            for sid in ("h:old-claude", "h:old-codex"):
                tail = await store.fetch_session_event_tail(sid, limit=500)
                assert [event["text"] for event in tail] == [f"historical {i}" for i in range(13)]
            assert len([frame for frame in frames if frame["type"] == "chat.event"]) == 28
            frames.clear()
            assert await pump.run_pass() == 0
            assert frames == []
        finally:
            for state in pump._streams.values():
                _close_stream(state)
            store.stop()
    asyncio.run(go())
