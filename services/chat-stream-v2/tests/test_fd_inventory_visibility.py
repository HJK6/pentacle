"""The bound front-desk seat is presented hidden without touching its row."""
from __future__ import annotations

import asyncio

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import Store


HOST = "fixture-host"


async def _fixture(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
    monkeypatch.delenv("PENTACLE_ASSISTANT_DAFF_ROLE", raising=False)
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, local_host=HOST)
    old = await sessions.open(HOST, "old", provider="claude", role="lead", visibility="default", pane_status="pane_alive")
    holder = await sessions.open(HOST, "holder", provider="claude", role="lead", visibility="default", pane_status="pane_alive")
    await sessions.open(HOST, "ordinary", provider="claude", role="lead", visibility="default")
    config = AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture-chat:assistant",
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": old["stream_id"],
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": old["session_generation"],
    })
    composite = AssistantComposite(store, config=config)
    await composite.load_binding()
    await store.recover_assistant_binding(name=config.name, target_stream_id=holder["stream_id"],
                                          target_generation=holder["session_generation"])
    await composite.load_binding()
    await sessions.refresh()
    server = Server(store=store, sessions=sessions, local_host=HOST)
    server.assistant_composite = composite
    return store, sessions, server, holder


def test_bound_front_desk_is_projected_hidden_and_row_is_untouched(monkeypatch):
    async def run():
        store, sessions, server, holder = await _fixture(monkeypatch)
        try:
            projected = server._filter_sessions_for_client(sessions.list_open(), False, None, True)
            by_id = {row["stream_id"]: row for row in projected}
            assert f"{HOST}:holder" not in by_id
            assert by_id[f"{HOST}:old"]["visibility"] == "default"
            assert by_id[f"{HOST}:ordinary"]["visibility"] == "default"
            shown = server._filter_sessions_for_client(sessions.list_open(), True, None, True)
            assert {row["stream_id"]: row["visibility"] for row in shown}[f"{HOST}:holder"] == "hidden"
            assert sessions.get(f"{HOST}:holder")["visibility"] == "default"
            assert (await store.fetch_session(HOST, "holder"))["visibility"] == "default"
            # Direct access and a reused stream name with another generation are unchanged.
            assert server._stream_is_visible_to_client(f"{HOST}:holder", False, None, True)
            reused = {**holder, "session_generation": "unrelated-new-generation"}
            assert server._filter_sessions_for_client([reused], False, None, True)[0]["visibility"] == "default"
        finally:
            store.stop()
    asyncio.run(run())


def test_bound_front_desk_events_are_withheld_from_ordinary_clients(monkeypatch):
    class Peer:
        remote_address = ("127.0.0.1", 12345)

    async def run():
        store, sessions, server, holder = await _fixture(monkeypatch)
        try:
            peer = Peer()
            server._client_include_subagents[peer] = False
            for frame_type in ("working.state", "session.died"):
                assert server._frame_for_client(peer, frame_type, {"type": frame_type, "stream_id": f"{HOST}:holder"}) is None
            assert server._frame_for_client(peer, "working.state", {"type": "working.state", "stream_id": f"{HOST}:old"})
        finally:
            store.stop()
    asyncio.run(run())
