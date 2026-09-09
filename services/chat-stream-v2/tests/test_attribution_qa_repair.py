"""Exact final-QA predicates: recorded-path proof and capture revocation."""
import asyncio

import pytest

from ingest import _StreamIngest, _close_stream
from ledger import NudgeJob
from presence import PresenceConfig, RemotePresence
from store import Store
from test_ingest_provider_root_binding import Harness as RootHarness, BIRTH, EXE, PID, write_log
from test_nudges import Harness
from test_remote_presence import (
    _BlockingCheckedPreviewTmux, _CheckedPreviewTmux, _FakeHosts, _FakeTmux,
    _observe_and_apply,
)


@pytest.mark.parametrize("birth, expected", [("", 0), (None, 0), (BIRTH, 1)])
def test_recorded_path_cannot_bypass_tuple_birth(tmp_path, monkeypatch, birth, expected):
    async def run():
        store = Store(":memory:"); store.start(); state = _StreamIngest()
        try:
            path = tmp_path / ".codex" / "sessions" / "own.jsonl"
            write_log(path)
            h = RootHarness(store, monkeypatch, path)
            binding = {"executable": EXE, "pane_pid": PID}
            if birth is not None:
                binding["pane_started_at"] = birth
            await h.sessions.open("h", "v2-root", provider="codex", pane_pid=PID,
                jsonl_path=str(path), observer_binding=binding, session_generation="gen-a",
                created_at="2026-09-09T16:19:32Z")
            row = await store.fetch_session("h", "v2-root")
            assert row["jsonl_path"] and row["observer_binding"]
            assert await h.run(h.ingest(), state) == expected
            assert len(await store.fetch_session_event_tail("h:v2-root", limit=10)) == expected
        finally:
            _close_stream(state); store.stop()
    asyncio.run(run())


async def _apply_inventory_only(presence):
    observations = await presence.observe_once()
    for host, rows in presence.last_rows.items():
        presence.apply_observation(rows, observations[host])


class _Comms:
    async def tell(self, msg):
        pass


@pytest.mark.parametrize("failure", ["dead", "no_server", "unknown_inventory", "no_capture"])
def test_revoked_remote_authority_needs_a_fresh_capture(failure):
    async def run():
        store = Store(":memory:"); store.start()
        try:
            h = Harness(store); await h.open("remote", host="hostb")
            sid = "hostb:remote"
            transport = _CheckedPreviewTmux((0, "remote\t1234\n"), h.tmux.IDLE)
            hosts = _FakeHosts("hosta", {"hostb": transport}, {"hostb": True})
            presence = RemotePresence(h.sessions, hosts, config=PresenceConfig(preview_cache_ttl_s=0))
            job = NudgeJob(h.sessions, _Comms(), store)
            await _observe_and_apply(presence)
            assert job._known_capture(h.sessions.get(sid))
            if failure == "no_capture":
                hosts._tmux["hostb"] = _FakeTmux((0, "remote\t1234\n"))
                await _observe_and_apply(presence)
            else:
                transport.result = {
                    "dead": (0, "v2-other\t9876\n"),
                    "no_server": (1, "no server running"),
                    "unknown_inventory": (0, ""),
                }[failure]
                await _apply_inventory_only(presence)
                transport.result = (0, "remote\t1234\n")
                await _apply_inventory_only(presence)
            assert (await job.run_pass()).sent == 0, "stale capture authorized a reminder"
            hosts._tmux["hostb"] = transport
            await _observe_and_apply(presence)
            assert (await job.run_pass()).sent == 1, "fresh capture did not restore eligibility"
        finally:
            store.stop()
    asyncio.run(run())


def test_capture_started_before_death_cannot_authorize_reappearance():
    async def run():
        store = Store(":memory:"); store.start()
        try:
            h = Harness(store); await h.open("remote", host="hostb")
            sid = "hostb:remote"
            transport = _CheckedPreviewTmux((0, "remote\t1234\n"), h.tmux.IDLE)
            hosts = _FakeHosts("hosta", {"hostb": transport}, {"hostb": True})
            presence = RemotePresence(h.sessions, hosts, config=PresenceConfig(preview_cache_ttl_s=0))
            await _observe_and_apply(presence)
            blocked = _BlockingCheckedPreviewTmux((0, "remote\t1234\n"), h.tmux.IDLE)
            hosts._tmux["hostb"] = blocked
            task = asyncio.create_task(presence._capture_pane("hostb", h.sessions.get(sid), asyncio.Semaphore(1)))
            await blocked.capture_started.wait()
            blocked.result = (0, "v2-other\t9876\n")
            await _apply_inventory_only(presence)
            blocked.result = (0, "remote\t1234\n")
            await _apply_inventory_only(presence)
            blocked.release_capture.set()
            result = await task
            await presence._apply_capture_result(result, {sid: presence._preview_key(h.sessions.get(sid))})
            assert (await NudgeJob(h.sessions, _Comms(), store).run_pass()).sent == 0
        finally:
            store.stop()
    asyncio.run(run())
