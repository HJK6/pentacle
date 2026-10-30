"""The composite's sidebar preview advances on every committed composite row.

Composite rows reach the store without touching the session registry, so
last_text stayed frozen at the startup rebuild (spec_pentacle__web_sidebar_preview_stale).
One test per composite path, each driving AssistantComposite with a real Sessions
handle after Sessions.refresh().
"""
from __future__ import annotations

import asyncio

import pytest

from assistant_composite import AssistantComposite
from ingest import broadcast_assistant_mirror
from sessions import Sessions
from store import Store
from test_assistant_prose_mirror import ASSISTANT, ROOT, _config, _normalized_turn_event
from test_assistant_lane_rulings import BlockedSpawn, RecordingSpawn, _chat_lines, _rulings_rig, _spawn_msg
from test_work_lanes import Env


def _run(body):
    async def go():
        store = Store(":memory:")
        store.start()
        composite = None
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            generation = root["session_generation"]
            sessions = Sessions(store, local_host="fixture-chat")
            composite = AssistantComposite(store, config=_config(generation), sessions=sessions)
            await composite.ensure_projection()
            await sessions.refresh()
            await body(store, composite, sessions, generation)
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()
    asyncio.run(go())


def _row(sessions):
    return next(row for row in sessions.list_open() if row["stream_id"] == ASSISTANT)


def _assert_advanced(sessions, before, *, text, kind):
    after = _row(sessions)
    assert (after["last_text"], after["last_kind"]) == (text, kind)
    assert (after["last_event_at"] or "") > (before["last_event_at"] or "")


def test_reply_publication_advances_preview():
    async def body(store, composite, sessions, generation):
        before = _row(sessions)
        assert not before.get("last_text")
        await asyncio.sleep(0.01)
        result = await composite.publish({
            "type": "assistant.publish", "request_id": "publish:summary-1", "composite_stream_id": ASSISTANT,
            "publish_kind": "prose", "response_state": "final", "message": "First reply.",
            "_auth_context": {"token_verified": True, "stream_id": ROOT, "session_generation": generation},
        }, actor_stream_id=ROOT)
        assert result["duplicate"] is False
        _assert_advanced(sessions, before, text="First reply.", kind="ASSIST_TEXT")
        await composite.publish({
            "type": "assistant.publish", "request_id": "publish:summary-2", "composite_stream_id": ASSISTANT,
            "publish_kind": "prose", "response_state": "final", "message": "Second reply.",
            "_auth_context": {"token_verified": True, "stream_id": ROOT, "session_generation": generation},
        }, actor_stream_id=ROOT)
        assert _row(sessions)["last_text"] == "Second reply."
        # A replayed publication is a duplicate and leaves the preview where it is.
        replay = await composite.publish({
            "type": "assistant.publish", "request_id": "publish:summary-1", "composite_stream_id": ASSISTANT,
            "publish_kind": "prose", "response_state": "final", "message": "First reply.",
            "_auth_context": {"token_verified": True, "stream_id": ROOT, "session_generation": generation},
        }, actor_stream_id=ROOT)
        assert replay["duplicate"] is True
        assert _row(sessions)["last_text"] == "Second reply."
    _run(body)


def test_work_lane_publication_advances_preview():
    async def body(store, composite, sessions, generation):
        env = Env(store, composite, generation)
        lane = (await env.adopt(state="paused", lead=False, owner="operator"))["lane"]
        before = _row(sessions)
        await asyncio.sleep(0.01)
        await env.op("set_state", {"to": "blocked", "blocker": "Need keys", "reason": "Test override"}, lane=lane["lane_id"], version=lane["version"])
        _assert_advanced(sessions, before, text="Need keys", kind="ASSIST_TEXT")
    _run(body)


def test_operator_input_admission_advances_preview():
    async def body(store, composite, sessions, generation):
        before = _row(sessions)
        await asyncio.sleep(0.01)
        receipt = await composite.accept_input({
            "text": "Please pin the build.", "request_id": "rpc-1", "optimistic_id": "input-1", "attachments": [],
        })
        assert not receipt.get("duplicate")
        _assert_advanced(sessions, before, text="Please pin the build.", kind="USER")
        replay = await composite.accept_input({
            "text": "Please pin the build.", "request_id": "rpc-2", "optimistic_id": "input-1", "attachments": [],
        })
        assert replay["duplicate"] is True
        assert _row(sessions)["last_text"] == "Please pin the build."
    _run(body)


def test_mirror_projection_advances_preview():
    async def body(store, composite, sessions, generation):
        before = _row(sessions)
        await asyncio.sleep(0.01)
        seq = await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT", text="Mirrored final.",
                                           identity="mirror-final", final=True)
        frames = []

        async def broadcast(frame):
            frames.append(frame)
        await broadcast_assistant_mirror(store, broadcast, seq, sessions=sessions)
        assert [f["event"]["text"] for f in frames] == ["Mirrored final."]
        _assert_advanced(sessions, before, text="Mirrored final.", kind="ASSIST_TEXT")
    _run(body)


@pytest.mark.parametrize("spawnctl", [BlockedSpawn, RecordingSpawn])
def test_unruled_ruling_status_advances_preview(spawnctl):
    """The authority-unavailable status line (AssistantLaneRulings._mirror_unruled) is a visible composite row."""
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            root, _advisor, server, _spawned = await _rulings_rig(
                store, advisor_pane="pane_dead", spawnctl=spawnctl())
            await server.sessions.refresh()
            server.assistant_composite.sessions = server.sessions
            before = _row(server.sessions)
            assert not before.get("last_text")
            await asyncio.sleep(0.01)
            await server._on_spawn(_spawn_msg(root, "unruled"))
            lines = await _chat_lines(store)
            assert len(lines) == 1
            _assert_advanced(server.sessions, before, text=lines[0], kind="ASSIST_TEXT")
            # A later tick re-reports nothing and leaves the preview where it is.
            await server.lane_rulings.tick()
            assert _row(server.sessions)["last_text"] == lines[0]
        finally:
            await server.assistant_composite.stop()
            store.stop()
    asyncio.run(go())
