"""Daff uses ordinary hidden-seat presentation without changing direct access."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import Store


HOST = "fixture-host"
ROLE = "partner-assistant"
BACKEND = f"{HOST}:current"


async def _fixture(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "primary-assistant")
    monkeypatch.setenv("PENTACLE_ASSISTANT_DAFF_ROLE", ROLE)
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, local_host=HOST)
    old = await sessions.open(HOST, "old", provider="claude", role=ROLE, visibility="default", pane_status="pane_alive")
    current = await sessions.open(HOST, "current", provider="claude", role="worker", visibility="default", pane_status="pane_alive")
    await sessions.open(HOST, "primary", provider="claude", role="primary-assistant", visibility="default")
    await sessions.open("other-host", "unrelated", provider="claude", role=ROLE, visibility="default")
    await sessions.open(HOST, "ordinary", provider="claude", role="worker", visibility="default", title="Daff assistant")
    config = AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_DAFF_COMPOSITE_STREAM_ID": "daff:assistant",
        "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_STREAM_ID": old["stream_id"],
        "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_GENERATION": old["session_generation"],
    }, name="daff", env_prefix="DAFF_")
    daff = AssistantComposite(store, config=config)
    await daff.load_binding()
    await store.recover_assistant_binding(name="daff", target_stream_id=BACKEND,
                                          target_generation=current["session_generation"])
    await daff.load_binding()
    await daff.ensure_projection()
    await sessions.refresh()
    server = Server(store=store, sessions=sessions, local_host=HOST)
    server.assistant_composites = {"daff": daff}
    return store, sessions, server, daff, current


def test_daff_projection_uses_configured_local_role_and_loaded_generation(monkeypatch):
    async def run():
        store, sessions, server, daff, current = await _fixture(monkeypatch)
        try:
            assert daff.env_config.direct_primary_stream_id != daff.config.direct_primary_stream_id
            projected = server._filter_sessions_for_client(sessions.list_open(), True, None, True)
            by_id = {row["stream_id"]: row for row in projected}
            assert by_id[f"{HOST}:old"]["visibility"] == "hidden"
            assert by_id[BACKEND]["visibility"] == "hidden"
            for sid in (f"{HOST}:primary", "other-host:unrelated", f"{HOST}:ordinary"):
                assert by_id[sid]["visibility"] == "default"
            # Presentation does not repair persisted rows or alter diagnostics.
            assert sessions.get(BACKEND)["visibility"] == "default"
            assert (await store.fetch_session(HOST, "current"))["visibility"] == "default"
            ordinary = server._filter_sessions_for_client(sessions.list_open(), False, None, True)
            assert {row["stream_id"] for row in ordinary} == {
                f"{HOST}:primary", "other-host:unrelated", f"{HOST}:ordinary",
            }
            assert server._stream_is_visible_to_client(BACKEND, False, None, True)
            reused = {**current, "session_generation": "unrelated-new-generation"}
            assert server._filter_sessions_for_client([reused], True, None, True)[0]["visibility"] == "default"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("frame_type", ["chat.event", "working.state", "completion.report", "session.died"])
def test_default_visible_daff_backend_fanout_matches_hidden_seat_policy(monkeypatch, frame_type):
    class Peer:
        remote_address = ("127.0.0.1", 12345)

    async def run():
        store, sessions, server, daff, current = await _fixture(monkeypatch)
        try:
            peer = Peer()
            server._client_assistant_composite_v1[peer] = True
            frame = {"type": frame_type, "stream_id": BACKEND}
            if frame_type == "chat.event":
                frame["event"] = {"stream_id": BACKEND, "kind": "ASSIST_TEXT", "text": "fixture reply"}
            server._client_include_subagents[peer] = False
            assert server._frame_for_client(peer, frame_type, frame) is None
            server._client_include_subagents[peer] = True
            assert server._frame_for_client(peer, frame_type, frame) == frame
            # Direct access still checks the authoritative row, independent of
            # ordinary discovery/fanout presentation.
            assert server._stream_is_visible_to_client(BACKEND, False, None, True)
        finally:
            store.stop()
    asyncio.run(run())


def test_disabled_or_stale_binding_does_not_hide_unrelated_row(monkeypatch):
    async def run():
        store, sessions, server, daff, current = await _fixture(monkeypatch)
        try:
            server.assistant_composites = {}
            server.assistant_composite = None
            rows = server._filter_sessions_for_client(sessions.list_open(), True, None, True)
            by_id = {row["stream_id"]: row for row in rows}
            assert by_id[BACKEND]["visibility"] == "default"
            # The explicitly protected Daff role stays hidden even if its
            # composite is temporarily unavailable; primary roles stay intact.
            assert by_id[f"{HOST}:old"]["visibility"] == "hidden"
            assert by_id[f"{HOST}:primary"]["visibility"] == "default"
        finally:
            store.stop()
    asyncio.run(run())


def test_ordinary_hello_omits_hidden_backend_working_state(monkeypatch):
    async def run():
        store, sessions, server, daff, current = await _fixture(monkeypatch)
        try:
            server.working_state_trackers = [SimpleNamespace(snapshot=lambda _now: {
                BACKEND: {"stream_id": BACKEND, "working": True},
                f"{HOST}:ordinary": {"stream_id": f"{HOST}:ordinary", "working": True},
            })]
            frames = await server._on_hello_inner({"subscribe": {"include_subagents": False}})
            snapshot = next(frame for frame in frames if frame["type"] == "snapshot")
            assert set(snapshot["working_states"]) == {f"{HOST}:ordinary"}
        finally:
            store.stop()
    asyncio.run(run())
