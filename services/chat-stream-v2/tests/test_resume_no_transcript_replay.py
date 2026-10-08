"""A seat reopened on the same transcript must not replay its history as new.

Event de-duplication is keyed by the seat's lifecycle (`session_created_at`).
A resume reopens the row with a new lifecycle, and ingest re-reads the
transcript from the start. Records an earlier lifecycle of the same stream
already recorded are kept for the new lifecycle's history, but they are not
live events: no `chat.event` frame and no mirror publication.
"""

import asyncio

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from ingest import append_ingested_event
from store import Store

ASSISTANT = "fixture-chat:assistant"
ROOT = "fixture-root:visible"


def _config(generation: str) -> AssistantCompositeConfig:
    return AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": ASSISTANT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ROOT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": generation,
    })


def _event(text: str, message_id: str) -> dict:
    return {"stream_id": ROOT, "provider": "claude", "kind": "ASSIST_TEXT", "text": text,
            "timestamp": "2026-10-07T13:05:00.000Z",
            "raw": {"source_session_identity": "fixture-session", "message_id": message_id}}


async def _open(store: Store, pane_pid: str) -> dict:
    row = await store.open_session("fixture-root", "visible", provider="claude", pane_pid=pane_pid,
                                   claude_session_id="11111111-2222-4333-8444-555555555555")
    composite = AssistantComposite(store, config=_config(row["session_generation"]))
    await composite.ensure_projection()
    await composite.stop()
    return await store.fetch_open_session_lifecycle(ROOT, pane_pid=pane_pid)


async def _mirrored(store: Store) -> list[str]:
    return [e["text"] for e in await store.fetch_session_event_tail(ASSISTANT, limit=50)
            if e["kind"] == "ASSIST_TEXT"]


def test_resumed_seat_rereads_history_silently_and_new_records_are_live():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            frames: list[dict] = []

            async def broadcast(frame):
                frames.append(frame)

            first = await _open(store, "4242")
            old = _event("Earlier front-desk answer", "m-1")
            assert await append_ingested_event(store, broadcast, old, recent_limit=50, lifecycle=first)
            assert await _mirrored(store) == ["Earlier front-desk answer"]
            live_before = len(frames)
            assert live_before >= 1

            # Resume: the same stream and transcript, a new lifecycle.
            await asyncio.sleep(1.1)  # the row's created_at has one-second resolution here
            second = await _open(store, "4343")
            assert second["session_created_at"] != first["session_created_at"]
            assert second["generation"] != first["generation"]

            replayed = await append_ingested_event(store, broadcast, old, recent_limit=50, lifecycle=second)
            assert replayed is None, "a record an earlier lifecycle recorded is not a new event"
            assert len(frames) == live_before, "no live frame for re-read history"
            assert await _mirrored(store) == ["Earlier front-desk answer"], "no second mirror publication"
            # The new lifecycle still has its history.
            history = [e["text"] for e in await store.fetch_session_event_tail(ROOT, limit=50)]
            assert history == ["Earlier front-desk answer"]

            fresh = _event("First answer after the resume", "m-2")
            assert await append_ingested_event(store, broadcast, fresh, recent_limit=50, lifecycle=second)
            assert len(frames) > live_before
            assert await _mirrored(store) == ["Earlier front-desk answer", "First answer after the resume"]
            assert [e["text"] for e in await store.fetch_session_event_tail(ROOT, limit=50)] == [
                "Earlier front-desk answer", "First answer after the resume"]
            # Re-reading within the same lifecycle stays a plain duplicate.
            assert await append_ingested_event(store, broadcast, fresh, recent_limit=50, lifecycle=second) is None
        finally:
            store.stop()

    asyncio.run(_go())


def test_same_stream_name_with_other_records_is_not_treated_as_replay():
    """A different conversation under the same stream name has other identities."""
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            async def broadcast(frame):
                return None

            first = await _open(store, "4242")
            assert await append_ingested_event(store, broadcast, _event("old", "m-1"), recent_limit=50, lifecycle=first)
            await asyncio.sleep(1.1)
            second = await _open(store, "4343")
            assert second["session_created_at"] != first["session_created_at"]
            assert await append_ingested_event(store, broadcast, _event("old", "m-other"), recent_limit=50,
                                               lifecycle=second)
            assert await _mirrored(store) == ["old", "old"]
        finally:
            store.stop()

    asyncio.run(_go())
