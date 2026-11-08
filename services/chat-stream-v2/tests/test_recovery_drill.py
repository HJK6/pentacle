"""Real isolated sockets, persisted restart, integrity and wrong-projection controls."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from _shared.assets_store import AssetStore
from _shared.notifications_store import NotificationStore
from store import Store

DRILL = Path(__file__).resolve().parents[3] / "scripts" / "recovery_drill.py"
spec = importlib.util.spec_from_file_location("recovery_drill", DRILL)
recovery_drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery_drill)
CHAT = "fixture-chat:assistant"


def _seed(state: Path, *, populated=False):
    state.mkdir(mode=0o700)
    AssetStore(state / "assets.db").close()
    NotificationStore(state / "notifications.db").close()
    with sqlite3.connect(state / "schedules.db") as conn:
        conn.execute("CREATE TABLE schedules (id TEXT PRIMARY KEY)")
    async def run():
        store = Store(str(state / "sessions.db"))
        store.start()
        try:
            primary = await store.open_session("fixture-a", "assistant", provider="codex", role="assistant",
                effective_model="gpt-6-sol", effective_effort="high", pane_status="pane_alive")
            await store.submit(lambda conn: conn.execute(
                "INSERT INTO v2_assistant_direct_binding (name,stream_id,generation,revision,updated_at) "
                "VALUES ('fixture-binding','fixture-a:assistant',?,1,'2026-01-01T00:00:00Z')",
                (primary["session_generation"],)))
            await store.submit(lambda conn: conn.commit())
            if populated:
                from assistant_composite import AssistantComposite, AssistantCompositeConfig
                from ledger import Ledger
                from sessions import Sessions
                config = AssistantCompositeConfig.from_env({
                    "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1", "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": CHAT,
                    "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": "fixture-a:assistant",
                    "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": primary["session_generation"]}, name="fixture-binding")
                composite = AssistantComposite(store, config=config)
                await composite.load_binding()
                await composite.ensure_projection()
                try:
                    await store.append_session_events_lifecycle_cas([
                        {"stream_id": CHAT, "identity": f"captured-{i}",
                         "event": {"stream_id": CHAT, "kind": "ASSIST_TEXT", "text": f"Private fixture {i}", "raw": {}}}
                        for i in range(13)], limit=100)
                    sessions = Sessions(store, local_host="fixture-chat")
                    await sessions.refresh()
                    await store.put_report({"report_id": "captured-fixture-report", "from_stream_id": "fixture-a:assistant",
                        "to_stream_id": "fixture-a:assistant", "msg_id": 0, "status": "done", "summary": "Private fixture",
                        "findings": [], "next_action": "Fixture", "ingested_at": "2026-01-01T00:00:00Z"},
                        routing_snapshot=await Ledger(store, sessions=sessions)._report_routing_snapshot("fixture-a:assistant"),
                        announce=False)
                    await composite.operation({"composite_stream_id": CHAT, "operation": "work_lane.adopt",
                        "request_id": "captured-fixture-lane", "payload": {"adoption_key": "request:captured-fixture",
                            "title": "Private fixture lane", "summary": "Fixture", "work_state": "paused",
                            "owner_kind": "operator", "visible_chat": {"stream_id": CHAT},
                            "no_spec_reason": "Isolated copied-state fixture"},
                        "_auth_context": {"token_verified": True, "stream_id": "fixture-a:assistant",
                            "session_generation": primary["session_generation"]}}, actor_stream_id="fixture-a:assistant")
                finally:
                    await composite.stop()
        finally:
            assert store.stop()
    asyncio.run(run())
    return state


def test_restore_public_rpc_reconnect_restart_once_only_and_teardown(tmp_path):
    state = _seed(tmp_path / "state")
    result = asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={}))
    assert all(result["integrity"][name]["integrity_check"] for name in ("sessions", "notifications", "assets", "schedules"))
    assert result["captured_rpc"]["reports"] == result["captured_rpc"]["lanes"] == 0
    assert result["seeded_controls"] == ["report", "lane"]
    assert result["rpc"]["reports"] == result["restart_rpc"]["reports"] == 1
    assert result["rpc"]["lanes"] == result["restart_rpc"]["lanes"] == 1
    assert result["foreign_stale_refused"]
    assert result["binding_after"]["stream_id"] == recovery_drill.DRILL_STREAM
    assert result["rebind_caller"] == "fixture-a:assistant"
    assert result["final_replies"] == 1 and result["external_dispatches"] == 0
    assert result["port_closed"] and result["state_dir_removed"] and not state.exists()
    encoded = json.dumps(result)
    assert "recovery drill reply" not in encoded and "token_hash" not in encoded


@pytest.mark.parametrize("key", recovery_drill.PRODUCER_KEYS)
def test_refuses_each_producer_and_push_key_independently(tmp_path, key):
    with pytest.raises(recovery_drill.DrillRefused):
        asyncio.run(recovery_drill.run_drill(tmp_path, composite_stream=CHAT, binding_name="fixture-binding",
            env={key: "fixture"}, remove_state_dir=False))
    assert not (tmp_path / "sessions.db").exists()


def test_refuses_real_host_roster(tmp_path):
    with pytest.raises(recovery_drill.DrillRefused):
        asyncio.run(recovery_drill.run_drill(tmp_path, composite_stream=CHAT, binding_name="fixture-binding",
            env={}, roster={"fixture-real-roster"}, remove_state_dir=False))


@pytest.mark.parametrize("component", ["sessions", "notifications", "assets", "schedules"])
def test_missing_component_is_never_mislabeled_pass_and_copy_cleaned(tmp_path, component):
    state = _seed(tmp_path / "state")
    (state / f"{component}.db").unlink()
    with pytest.raises(recovery_drill.DrillFailed, match="missing_component"):
        asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={}))
    assert not state.exists()


def test_missing_attachment_pointer_fails_despite_readable_database(tmp_path):
    state = _seed(tmp_path / "state")
    with sqlite3.connect(state / "sessions.db") as conn:
        conn.execute("INSERT INTO v2_attachment_refs VALUES ('publication','fixture','fixture:a','missing',?)", ('a' * 64,))
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with pytest.raises(recovery_drill.DrillFailed, match="reference:attachment_receipts"):
        recovery_drill.validate_copy(state)


def test_wrong_public_projection_fails_with_underlying_rows_still_readable(tmp_path, monkeypatch):
    from work_lanes_projection import WorkLanesInventory
    state = _seed(tmp_path / "state")
    original = WorkLanesInventory.list
    async def omit(self, **kwargs):
        frame = await original(self, **kwargs)
        frame["lanes"] = []
        return frame
    monkeypatch.setattr(WorkLanesInventory, "list", omit)
    with pytest.raises(recovery_drill.DrillFailed, match="rpc:lane_inventory"):
        asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={},
            remove_state_dir=False))
    with sqlite3.connect(state / "sessions.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM v2_assistant_composite_lanes WHERE work_state IS NOT NULL").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("point", ["before_bind", "after_bind", "during_rpc"])
def test_cancellation_cleans_private_state_and_listeners(tmp_path, monkeypatch, point):
    from server import Server
    state = _seed(tmp_path / "state")
    ports = []
    original_bind = Server.bind
    async def bind(self):
        if point == "before_bind":
            raise asyncio.CancelledError()
        port = await original_bind(self)
        ports.append(port)
        if point == "after_bind":
            raise asyncio.CancelledError()
        return port
    monkeypatch.setattr(Server, "bind", bind)
    if point == "during_rpc":
        async def cancel(*args, **kwargs):
            raise asyncio.CancelledError()
        monkeypatch.setattr(recovery_drill, "_projections", cancel)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={})
        for port in ports:
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", port)
    asyncio.run(run())
    assert not state.exists()


def test_populated_copy_is_checked_before_startup_without_replacing_captured_proof(tmp_path):
    state = _seed(tmp_path / "state", populated=True)
    result = asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={}))
    assert result["captured_rpc"] == {"history_events": 8, "reports": 1, "lanes": 1}
    assert result["seeded_controls"] == []
    assert result["restart_rpc"]["history_events"] == 8


def test_startup_dropping_copied_history_fails_against_pre_start_snapshot(tmp_path, monkeypatch):
    from assistant_composite import AssistantComposite
    state = _seed(tmp_path / "state", populated=True)
    original = AssistantComposite.ensure_projection
    async def drop(self):
        await original(self)
        def delete(conn):
            conn.execute("DELETE FROM session_event_tail WHERE stream_id=?", (CHAT,))
            conn.commit()
        await self.store.submit(delete)
    monkeypatch.setattr(AssistantComposite, "ensure_projection", drop)
    with pytest.raises(recovery_drill.DrillFailed, match="rpc:history_identity_order"):
        asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={}))
    assert not state.exists()


@pytest.mark.parametrize("damage", [None, "missing", "size", "digest"])
def test_ready_managed_attachment_requires_actual_blob_bytes(tmp_path, damage):
    import hashlib
    state = _seed(tmp_path / "state")
    content = b"fixture blob"
    digest = hashlib.sha256(content).hexdigest()
    target = state / "blobs" / digest[:2] / digest
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    with sqlite3.connect(state / "sessions.db") as conn:
        conn.execute("INSERT INTO v2_attachment_uploads "
            "(upload_id,request_id,auth_fingerprint,blob_sha,size_bytes,media_type,filename,purpose,auth_kind,"
            "principal_id,seat_stream_id,seat_generation,uploaded_at,updated_at,state) "
            "VALUES ('fixture-upload','fixture-request','fixture-fingerprint',?,?,'image/png','fixture.png',"
            "'chat_attachment','seat','fixture-a:assistant','fixture-a:assistant','fixture-generation','t','t','ready')",
            (digest, len(content)))
    if damage == "missing":
        target.unlink()
    elif damage == "size":
        target.write_bytes(content + b"x")
    elif damage == "digest":
        target.write_bytes(b"x" * len(content))
    if damage:
        with pytest.raises(recovery_drill.DrillFailed, match="reference:"):
            recovery_drill.validate_copy(state)
    else:
        assert recovery_drill.validate_copy(state)["references"]["content_addressed_files"]["checked"] == 1


def test_recording_only_drill_never_signals_or_launches_external_processes(tmp_path, monkeypatch):
    import os
    import subprocess
    state = _seed(tmp_path / "state")
    def prohibited(*args, **kwargs):
        pytest.fail("external process action attempted")
    monkeypatch.setattr(os, "kill", prohibited)
    monkeypatch.setattr(os, "killpg", prohibited)
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    result = asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={}))
    assert result["external_dispatches"] == 0


def _paired_fixture(tmp_path):
    import shutil
    from test_receipt_route_retention import append, seed_routes, move
    import retention
    original = _seed(tmp_path / "original", populated=True)
    async def populate():
        store = Store(str(original / "sessions.db")); store.start()
        try:
            await append(store)
            await seed_routes(store)
            for table in retention.HISTORY_TABLES:
                assert (await move(store, table))["moved"] == 1
        finally:
            assert store.stop()
    asyncio.run(populate())
    working = tmp_path / "working"
    shutil.copytree(original, working)
    return original, working


def _forbid_original_sqlite(monkeypatch, original):
    from urllib.parse import unquote, urlparse
    connect = sqlite3.connect
    attempts = []
    def check(value):
        if isinstance(value, (str, Path)):
            path = Path(unquote(urlparse(str(value)).path) if str(value).startswith("file:") else value)
            if path.is_absolute() and path.is_relative_to(original):
                attempts.append("original_open")
                raise RuntimeError("prohibited_original_open")
    def guarded(database, *args, **kwargs):
        check(database)
        conn = connect(database, *args, **kwargs)
        def authorize(action, arg1, arg2, dbname, trigger):
            if action == sqlite3.SQLITE_ATTACH:
                check(arg1)
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn
    monkeypatch.setattr(sqlite3, "connect", guarded)
    return attempts


def test_populated_archive_ordinary_restore_never_opens_original_and_survives_restart(tmp_path, monkeypatch):
    import hashlib
    original, state = _paired_fixture(tmp_path)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in original.glob("*.db")}
    attempts = _forbid_original_sqlite(monkeypatch, original)
    result = asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={},
        source_archive_path=original / "sessions_archive.db"))
    assert attempts == []
    assert result["archive"]["mode"] == "paired" and result["archive"]["integrity_check"]
    assert result["archive"]["rows"] == {"v2_send_receipts": 1, "v2_assistant_composite_routes": 1}
    assert result["archive"]["only_archive_path_changed"]
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in original.glob("*.db")} == before
    assert result["restart_rpc"]["reports"] == result["restart_rpc"]["lanes"] == 1
    assert not state.exists()


@pytest.mark.parametrize("damage", ["missing", "pair", "corrupt", "symlink", "hardlink", "escape", "unapproved", "high_water", "conflicting_duplicate"])
def test_archive_refuses_before_startup_or_original_access(tmp_path, monkeypatch, damage):
    original, state = _paired_fixture(tmp_path)
    archive = state / "sessions_archive.db"
    if damage == "missing":
        archive.unlink()
    elif damage == "pair":
        with sqlite3.connect(archive) as conn:
            conn.execute("UPDATE v2_receipt_route_archive SET pair_id='other'")
    elif damage == "corrupt":
        archive.write_bytes(b"not sqlite")
    elif damage == "symlink":
        archive.unlink()
        archive.symlink_to(original / "sessions_archive.db")
    elif damage == "hardlink":
        import os
        archive.unlink()
        os.link(original / "sessions_archive.db", archive)
    elif damage == "escape":
        with sqlite3.connect(state / "sessions.db") as conn:
            conn.execute("UPDATE v2_send_receipt_sequence SET archive_path='/fixture/../escape.db'")
    elif damage == "high_water":
        with sqlite3.connect(state / "sessions.db") as conn:
            conn.execute("UPDATE v2_send_receipt_sequence SET high_water=0")
    elif damage == "conflicting_duplicate":
        with sqlite3.connect(state / "sessions.db") as conn:
            conn.execute("ATTACH DATABASE ? AS copied", (str(archive),))
            columns = [r[1] for r in conn.execute("PRAGMA main.table_info(v2_send_receipts)")]
            names = ','.join(columns)
            conn.execute(f"INSERT INTO main.v2_send_receipts(rowid,{names}) "
                f"SELECT receipt_rowid,{names} FROM copied.v2_send_receipts")
            conn.execute("UPDATE main.v2_send_receipts SET display_text='conflicting'")
    attempts = _forbid_original_sqlite(monkeypatch, original)
    starts = []
    def prohibited_start(self):
        starts.append(True)
        pytest.fail("Store started before archive validation")
    monkeypatch.setattr(Store, "start", prohibited_start)
    approved = tmp_path / "unapproved.db" if damage == "unapproved" else original / "sessions_archive.db"
    with pytest.raises((recovery_drill.DrillFailed, sqlite3.DatabaseError)):
        asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={},
            source_archive_path=approved))
    assert starts == attempts == [] and not state.exists()


def test_fresh_no_file_archive_is_rewrite_only_and_bootstraps_inside_copy(tmp_path):
    state = _seed(tmp_path / "state")
    assert not (state / "sessions_archive.db").exists()
    result = asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={}))
    assert result["archive"]["mode"] == "rewrite_only" and result["archive"]["rows"] == {}
    assert result["restart_rpc"]["reports"] == 1 and not state.exists()


def test_same_pair_equal_duplicates_keep_existing_union_semantics(tmp_path):
    original, state = _paired_fixture(tmp_path)
    archive = state / "sessions_archive.db"
    with sqlite3.connect(state / "sessions.db") as conn:
        conn.execute("ATTACH DATABASE ? AS copied", (str(archive),))
        columns = [r[1] for r in conn.execute("PRAGMA main.table_info(v2_send_receipts)")]
        names = ','.join(columns)
        conn.execute(f"INSERT INTO main.v2_send_receipts(rowid,{names}) "
            f"SELECT receipt_rowid,{names} FROM copied.v2_send_receipts")
    result = recovery_drill.prepare_archive(state, original / "sessions_archive.db")
    assert result["rows"]["v2_send_receipts"] == 1 and result["only_archive_path_changed"]


def test_copy_fence_removal_mutation_exposes_original_open_without_allowing_it(tmp_path, monkeypatch):
    original, state = _paired_fixture(tmp_path)
    attempts = _forbid_original_sqlite(monkeypatch, original)
    monkeypatch.setattr(recovery_drill, "prepare_archive", lambda *args, **kwargs: {"mode": "mutated_guard_disabled"})
    with pytest.raises(RuntimeError, match="prohibited_original_open"):
        asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={},
            source_archive_path=original / "sessions_archive.db"))
    assert attempts and not state.exists()


def test_primary_failure_survives_cleanup_failure_and_remaining_resources_are_released(tmp_path, monkeypatch):
    from assistant_composite import AssistantComposite
    state = _seed(tmp_path / "state")
    async def primary(*args, **kwargs):
        raise recovery_drill.DrillFailed("fixture_primary")
    async def cleanup(self):
        raise recovery_drill.DrillFailed("fixture_cleanup")
    monkeypatch.setattr(recovery_drill, "_projections", primary)
    monkeypatch.setattr(AssistantComposite, "stop", cleanup)
    with pytest.raises(BaseExceptionGroup) as caught:
        asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={}))
    assert [str(error) for error in caught.value.exceptions] == ["fixture_primary", "fixture_cleanup"]
    assert not state.exists()


@pytest.mark.parametrize("qualified_absence", [False, True])
def test_ready_zero_absence_needs_capture_qualification_for_external_source(tmp_path, monkeypatch, qualified_absence):
    import shutil
    original = _seed(tmp_path / "original")
    state = tmp_path / "working"
    shutil.copytree(original, state)
    attempts = _forbid_original_sqlite(monkeypatch, original)
    def run():
        return asyncio.run(recovery_drill.run_drill(state, composite_stream=CHAT, binding_name="fixture-binding", env={},
            source_archive_path=original / "sessions_archive.db", archive_source_absent=qualified_absence))
    if qualified_absence:
        assert run()["archive"]["mode"] == "rewrite_only"
    else:
        with pytest.raises(recovery_drill.DrillFailed, match="archive:absence_not_qualified"):
            run()
    assert attempts == [] and not state.exists()
