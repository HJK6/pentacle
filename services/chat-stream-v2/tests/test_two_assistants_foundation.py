"""Foundation for two named assistants (bart, daff): config, migration, binding,
mirror and policy isolation.  Bart's behaviour must stay byte-identical."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from assistant_policy import AssistantPolicy
from store import Store
from store_assistant_binding import migrate_binding_to_named, rollback_binding_to_single


BART_CHAT = "bart:assistant"
DAFF_CHAT = "daff:assistant"
BART_SEAT = "fixture-bart:visible"
DAFF_SEAT = "fixture-daff:visible"
OTHER_SEAT = "fixture-other:visible"


# --- config: bart byte-identical, daff isolated namespace --------------------

def test_all_from_env_bart_byte_identical_and_daff_prefixed():
    env = {
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": BART_CHAT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": BART_SEAT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": "g-bart",
        "PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_DAFF_COMPOSITE_STREAM_ID": DAFF_CHAT,
        "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_STREAM_ID": DAFF_SEAT,
        "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_GENERATION": "g-daff",
    }
    configs = AssistantCompositeConfig.all_from_env(env)
    assert set(configs) == {"bart", "daff"}
    # bart reads the existing unprefixed keys, identical to the legacy single call
    # (the legacy call defaults name to "bart").
    legacy = AssistantCompositeConfig.from_env(env)
    assert configs["bart"] == legacy
    assert configs["bart"].name == "bart"
    assert configs["bart"].stream_id == BART_CHAT
    assert configs["daff"].name == "daff"
    assert configs["daff"].stream_id == DAFF_CHAT
    assert configs["daff"].direct_primary_stream_id == DAFF_SEAT


def test_daff_inert_when_unconfigured():
    env = {
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": BART_CHAT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": BART_SEAT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": "g-bart",
    }
    configs = AssistantCompositeConfig.all_from_env(env)
    assert configs["bart"].enabled is True
    assert configs["daff"].enabled is False
    assert configs["daff"].stream_id == ""


# --- migration and rollback --------------------------------------------------

def _legacy_binding_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE v2_assistant_direct_binding ("
        "id INTEGER PRIMARY KEY CHECK(id=1),stream_id TEXT,generation TEXT,"
        "revision INTEGER NOT NULL,updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO v2_assistant_direct_binding(id,stream_id,generation,revision,updated_at) "
        "VALUES(1,?,?,?,?)", (BART_SEAT, "g-bart", 7, "2026-10-03T00:00:00Z"),
    )


def test_migration_seeds_bart_and_rollback_restores_single():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _legacy_binding_table(conn)

    assert migrate_binding_to_named(conn) is True
    # idempotent
    assert migrate_binding_to_named(conn) is False
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='v2_assistant_direct_binding'"
    ).fetchone()[0]
    assert "name TEXT PRIMARY KEY" in schema
    row = conn.execute(
        "SELECT name,stream_id,generation,revision,updated_at FROM v2_assistant_direct_binding"
    ).fetchall()
    assert len(row) == 1
    assert dict(row[0]) == {
        "name": "bart", "stream_id": BART_SEAT, "generation": "g-bart",
        "revision": 7, "updated_at": "2026-10-03T00:00:00Z",
    }

    # a daff row can now coexist; rollback discards it and restores the single form
    conn.execute(
        "INSERT INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) "
        "VALUES('daff',?,?,?,?)", (DAFF_SEAT, "g-daff", 3, "2026-10-03T01:00:00Z"),
    )
    assert rollback_binding_to_single(conn) is True
    assert rollback_binding_to_single(conn) is False
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='v2_assistant_direct_binding'"
    ).fetchone()[0]
    assert "CHECK(id=1)" in schema
    restored = conn.execute(
        "SELECT id,stream_id,generation,revision FROM v2_assistant_direct_binding"
    ).fetchall()
    assert len(restored) == 1
    assert dict(restored[0]) == {"id": 1, "stream_id": BART_SEAT, "generation": "g-bart", "revision": 7}


def test_store_startup_migrates_legacy_db(tmp_path):
    db = tmp_path / "sessions.db"
    # Seed a legacy single-row binding before the store's schema setup runs.
    conn = sqlite3.connect(str(db))
    _legacy_binding_table(conn)
    conn.commit()
    conn.close()

    async def run():
        store = Store(str(db))
        store.start()
        try:
            binding = await store.get_assistant_binding(
                env_binding={"stream_id": "", "generation": ""}, name="bart",
            )
            assert binding["stream_id"] == BART_SEAT
            assert binding["generation"] == "g-bart"
            # daff has no row yet -> unconfigured, independent of bart
            daff = await store.get_assistant_binding(
                env_binding={"stream_id": "", "generation": ""}, name="daff",
            )
            assert daff["source"] == "unconfigured"
        finally:
            store.stop()

    asyncio.run(run())


# --- independent per-name binding --------------------------------------------

async def _seat(store, stream_id):
    host, name = stream_id.split(":", 1)
    return await store.open_session(
        host, name, provider="codex", role="assistant", visibility="default",
        pane_status="pane_alive", effective_model="gpt-6-sol", effective_effort="high",
    )


def _config(name, env_prefix, stream_id, seat_stream, generation):
    return AssistantCompositeConfig.from_env({
        f"PENTACLE_ASSISTANT_{env_prefix}COMPOSITE_ENABLED": "1",
        f"PENTACLE_ASSISTANT_{env_prefix}COMPOSITE_STREAM_ID": stream_id,
        f"PENTACLE_ASSISTANT_{env_prefix}DIRECT_PRIMARY_STREAM_ID": seat_stream,
        f"PENTACLE_ASSISTANT_{env_prefix}DIRECT_PRIMARY_GENERATION": generation,
    }, name=name, env_prefix=env_prefix)


def _rebind(request_id, revision, target):
    return {"type": "assistant.rebind", "request_id": request_id,
            "expected_revision": revision, "target_stream_id": target, "clear": False}


def test_independent_bindings_do_not_cross(tmp_path):
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        try:
            bart_seat = await _seat(store, BART_SEAT)
            daff_seat = await _seat(store, DAFF_SEAT)
            other = await _seat(store, OTHER_SEAT)
            bart = AssistantComposite(store, config=_config(
                "bart", "", BART_CHAT, BART_SEAT, bart_seat["session_generation"]))
            daff = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, daff_seat["session_generation"]))
            for c in (bart, daff):
                await c.load_binding()
                await c.ensure_projection()

            # Rebind daff to OTHER; bart's binding and generation are untouched.
            await daff.rebind(_rebind("daff-move", 0, OTHER_SEAT), actor_stream_id=DAFF_SEAT)
            bart_binding = await bart.binding()
            daff_binding = await daff.binding()
            assert bart_binding["stream_id"] == BART_SEAT
            assert bart_binding["generation"] == bart_seat["session_generation"]
            assert daff_binding["stream_id"] == OTHER_SEAT
            assert daff_binding["generation"] == other["session_generation"]

            # Rebind bart; daff unaffected.
            await bart.rebind(_rebind("bart-move", 0, OTHER_SEAT), actor_stream_id=BART_SEAT)
            assert (await bart.binding())["stream_id"] == OTHER_SEAT
            assert (await daff.binding())["stream_id"] == OTHER_SEAT  # daff still where we left it
            assert (await daff.binding())["revision"] == 1  # one rebind only
            assert (await bart.binding())["revision"] == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_per_name_mirror_kv_keys_isolated(tmp_path):
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        try:
            await _seat(store, BART_SEAT)
            await _seat(store, DAFF_SEAT)
            await store.configure_assistant_mirror(
                composite_stream_id=BART_CHAT, source_stream_id=BART_SEAT,
                source_generation="g", enabled_default=True, name="bart")
            await store.configure_assistant_mirror(
                composite_stream_id=DAFF_CHAT, source_stream_id=DAFF_SEAT,
                source_generation="g", enabled_default=True, name="daff")
            # bart uses the legacy key; daff uses a per-name key.
            await store.put("assistant.mirror.enabled", "off")
            await store.put("assistant.mirror.daff.enabled", "on")
            bart_state = await store.assistant_mirror_state(BART_CHAT)
            daff_state = await store.assistant_mirror_state(DAFF_CHAT)
            assert bart_state["enabled"] is False
            assert daff_state["enabled"] is True
        finally:
            store.stop()

    asyncio.run(run())


# --- policy: per-role protection and singleton -------------------------------

def test_policy_protects_both_roles_but_singleton_is_per_role(monkeypatch, tmp_path):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    monkeypatch.setenv("PENTACLE_ASSISTANT_DAFF_ROLE", "daff-assistant")

    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        try:
            policy = AssistantPolicy(store, "fixture-bart")
            assert policy.roles == {"assistant", "daff-assistant"}
            assert policy.protects({"role": "assistant"}) is True
            assert policy.protects({"role": "daff-assistant"}) is True
            assert policy.protects({"role": "lead"}) is False

            # Open a bart-role seat; daff singleton check must still pass.
            await store.open_session(
                "fixture-bart", "visible", provider="claude", role="assistant",
                visibility="default", pane_status="pane_alive",
                effective_model="x", effective_effort="high")
            # bart singleton now occupied -> another bart is refused
            with pytest.raises(Exception, match="already open"):
                await policy.available("fixture-bart", "assistant")
            # daff is a different singleton -> available
            await policy.available("fixture-bart", "daff-assistant")
        finally:
            store.stop()

    asyncio.run(run())
