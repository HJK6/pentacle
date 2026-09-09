"""Shipped pre-objective clients remain spawnable without weakening new callers."""
import asyncio
import sqlite3

import pytest

from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from tests.test_spawn_error_no_phantom_row import BootReadyTmux


class EchoPane(BootReadyTmux):
    def __init__(self):
        super().__init__()
        self.text = ""

    async def paste(self, name, text):
        self.text += text

    async def capture(self, name):
        return "READY\n" + self.text + "\n❯ "


def test_support_metadata_preserves_existing_explicit_retry_hash():
    request = {"type": "spawn", "objective": "Already explicit", "host": "hosta", "provider": "codex"}
    old_hash = SpawnCtl._spawn_payload_hash(request)
    assert SpawnCtl._spawn_payload_hash({**request, "objective_supported": True,
                                        "objective_source": "explicit"}) == old_hash
    assert SpawnCtl._spawn_payload_hash({**request, "objective": "Different task"}) != old_hash


@pytest.mark.parametrize("fields,expected,source", [
    ({"schema": "SpawnRequestV2"}, "New session", "derived"),
    ({"prompt": " \n Vérifier 東京 🌈\nLater example detail"}, "Vérifier 東京 🌈", "derived"),
    ({"title": "🌈" * 121}, "🌈" * 120, "derived"),
    ({"prompt": "", "title": " \nUse this title\nnot this"}, "Use this title", "derived"),
    ({"title": "A\x00B\tC"}, "ABC", "derived"),
    ({"prompt": "", "title": " "}, "New session", "derived"),
    ({"objective": " Explicit exactly ", "objective_source": "derived"}, " Explicit exactly ", "explicit"),
])
def test_legacy_admission_persists_and_projects_provenance(tmp_path, fields, expected, source):
    async def run():
        path = str(tmp_path / "sessions.db")
        store = Store(path); store.start()
        pane = EchoPane()
        sessions = Sessions(store, tmux=pane)
        ctl = SpawnCtl(store, sessions, tmux=pane)
        try:
            await sessions.open("localhost", "parent")
            response = await ctl.spawn({"command": "run", "session_name": "child",
                "request_id": "legacy-compat", "parent_stream_id": "localhost:parent", **fields}, "localhost")
            await asyncio.gather(*list(ctl._background_spawns))
            assert response["type"] == "spawn.ok"
            row = await store.fetch_session("localhost", "child")
            assert (row["objective"], row["objective_source"]) == (expected, source)
            agent = sessions.get("localhost:parent")["agents"][0]
            assert (agent["objective"], agent["objective_source"]) == (expected, source)
            with pytest.raises(ValueError, match="immutable"):
                await store.update_session("localhost", "child", objective_source="forged")
            with pytest.raises(ValueError, match="immutable"):
                await store.open_session("localhost", "child", session_generation=row["session_generation"],
                                         objective=expected, objective_source="forged")
            store.stop(); store = Store(path); store.start()
            row = await store.fetch_session("localhost", "child")
            assert (row["objective"], row["objective_source"]) == (expected, source)
        finally:
            if pane.alive:
                await pane.kill_session("child")
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("fields,code", [
    ({"objective_supported": True}, "objective_required"),
    ({"objective": ""}, "objective_required"),
    ({"objective": None}, "objective_required"),
    ({"objective": " \t"}, "objective_required"),
    ({"objective": "line\nbreak"}, "objective_invalid"),
    ({"objective": "x" * 121}, "objective_invalid"),
    ({"objective": 23}, "objective_invalid"),
])
def test_strict_requests_reject_before_effects(fields, code):
    async def run():
        store = Store(); store.start()
        pane = EchoPane()
        ctl = SpawnCtl(store, Sessions(store, tmux=pane), tmux=pane)
        try:
            with pytest.raises(VerbError) as error:
                await ctl.spawn({"command": "run", "prompt": "A useful derivation exists", **fields}, "localhost")
            assert error.value.code == code
            assert not pane.alive
            assert not await store.reservations()
            assert not await store.list_sessions()
        finally:
            store.stop()
    asyncio.run(run())


def test_objective_source_additive_migration_and_reopen(tmp_path):
    async def run():
        path = tmp_path / "old.db"
        store = Store(str(path)); store.start()
        await store.open_session("localhost", "old", objective="Already explicit")
        store.stop()
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE sessions DROP COLUMN objective_source")
        store = Store(str(path)); store.start()
        try:
            row = await store.fetch_session("localhost", "old")
            assert row["objective_source"] == "explicit"
            await store.mark_closed("localhost", "old", closed_at="2026-09-08T18:00:00Z", pane_status="pane_dead")
            row = await store.open_session("localhost", "old", objective="New session", objective_source="derived")
            assert row["objective_source"] == "derived"
        finally:
            store.stop()
    asyncio.run(run())


def test_fresh_inventory_working_is_boolean_before_capture():
    from server import Server

    async def run():
        store = Store(); store.start()
        sessions = Sessions(store, local_host="hosta")
        try:
            await sessions.open("hosta", "parent", bootstrap_state="ready")
            await sessions.open("hostc", "child", parent_stream_id="hosta:parent", bootstrap_state="ready")
            for after_restart in (False, True):
                if after_restart:
                    await sessions.refresh()
                row = sessions.get("hostc:child")
                assert row["working"] is False
                assert row.get("capture_liveness") is None
                assert all(r["working"] is False for r in sessions.list_open())
                assert all(r["working"] is False for r in Server._summary_snapshot_sessions(sessions.list_open()))
                assert sessions.get("hosta:parent")["agents"][0]["state"] == "idle"
            sessions.apply_live("hostc:child", working=True, capture_liveness="working")
            assert sessions.get("hostc:child")["working"] is True
            assert sessions.get("hosta:parent")["agents"][0]["state"] == "working"
            sessions.apply_live("hostc:child", working=False, capture_liveness="idle")
            assert sessions.get("hostc:child")["working"] is False
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("source", ["explicit", "derived"])
def test_reconciled_restore_preserves_objective_and_provenance(tmp_path, source):
    async def run():
        path = str(tmp_path / "restore.db")
        store = Store(path); store.start()
        pane = EchoPane(); pane.alive = True
        sessions = Sessions(store, tmux=pane, local_host="hosta")
        try:
            await sessions.open("hosta", "parent")
            original = await sessions.open("hosta", "child", parent_stream_id="hosta:parent",
                objective="Restore this objective", objective_source=source, bootstrap_state="ready")
            await sessions.mark_reconciled_dead("hosta", "child",
                expected_generation=original["session_generation"],
                presumed_dead_at="2026-09-08T18:00:00Z", closed_at="2026-09-08T18:00:01Z")
            restored = await sessions.restore_reconciled("hosta", "child")
            assert restored["restored"] is True
            assert restored["session"]["session_generation"] != original["session_generation"]
            for after_restart in (False, True):
                if after_restart:
                    store.stop(); store = Store(path); store.start()
                    sessions = Sessions(store, tmux=pane, local_host="hosta")
                    await sessions.refresh()
                row = await store.fetch_session("hosta", "child")
                assert (row["objective"], row["objective_source"]) == ("Restore this objective", source)
                agent = sessions.get("hosta:parent")["agents"][0]
                assert (agent["objective"], agent["objective_source"]) == ("Restore this objective", source)
                with pytest.raises(ValueError, match="immutable"):
                    await store.update_session("hosta", "child", objective="Changed after restore")
        finally:
            await pane.kill_session("child")
            store.stop()
    asyncio.run(run())
