"""Dispatch-free direct-primary prose must reach the canonical chat."""

import asyncio
from datetime import datetime, timedelta, timezone
import time

from ingest import append_ingested_event

from assistant_composite import AssistantComposite, AssistantCompositeConfig
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


def _source_event(text: str = "A proactive milestone") -> dict:
    return {
        "stream_id": ROOT,
        "provider": "codex",
        "kind": "ASSIST_TEXT",
        "text": text,
        "timestamp": "2026-09-27T05:00:00.000Z",
        "raw": {"source_session_identity": "fixture-session", "message_id": "fixture-message"},
    }


def test_bound_dispatch_free_prose_is_projected_into_canonical_chat():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]))
            await composite.ensure_projection()
            source = _source_event()
            inserted = await store.append_session_events_lifecycle_cas(
                [{"stream_id": ROOT, "event": source, "identity": "fixture-message",
                  "lifecycle": await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")}], limit=20,
            )
            assert inserted and isinstance(inserted[0], int)
            canonical = await store.fetch_session_event_tail(ASSISTANT, limit=20)
            mirrored = [event for event in canonical if event["kind"] == "ASSIST_TEXT"]
            assert len(mirrored) == 1
            assert mirrored[0]["text"] == source["text"]
            assert mirrored[0]["publish_kind"] == "status"
            assert mirrored[0]["raw"]["mirrored_from"] == {
                "stream_id": ROOT,
                "generation": root["session_generation"],
                "event_id": inserted[0],
                "event_ts": source["timestamp"],
            }
            replay = await store.append_session_events_lifecycle_cas(
                [{"stream_id": ROOT, "event": source, "identity": "fixture-message",
                  "lifecycle": await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")}], limit=20,
            )
            assert replay == [None]
            assert len([event for event in await store.fetch_session_event_tail(ASSISTANT, limit=20)
                        if event["kind"] == "ASSIST_TEXT"]) == 1
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


async def _append(store: Store, text: str, *, kind: str = "ASSIST_TEXT", identity: str | None = None):
    identity = identity or f"source:{kind}:{text}"
    event = {**_source_event(text), "kind": kind,
             "raw": {"source_session_identity": "fixture-session", "message_id": identity}}
    result = await store.append_session_events_lifecycle_cas(
        [{"stream_id": ROOT, "event": event, "identity": identity,
          "lifecycle": await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")}], limit=20,
    )
    assert result is not None
    return result[0]


async def _answer_rows(store: Store):
    return [event for event in await store.fetch_session_event_tail(ASSISTANT, limit=50)
            if event["kind"] == "ASSIST_TEXT"]


async def _route_for(composite: AssistantComposite, store: Store, input_id: str):
    await composite.accept_input({"message": "Operator question", "msg_id": input_id,
                                  "request_id": input_id}, operator_principal="operator:fixture")
    for _ in range(100):
        route = await store.get_assistant_composite_route(
            stream_id=ASSISTANT, input_identity=input_id,
        )
        if route and route["routing_state"] == "resolved" and route["dispatch_id"]:
            return route
        await asyncio.sleep(0.01)
    raise AssertionError("direct route never became resolved")


def test_held_dispatch_receipt_suppresses_reply_before_final_publish():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            release = asyncio.Future()
            async def dispatch(_route):
                return await release
            composite = AssistantComposite(store, config=_config(root["session_generation"]),
                                           dispatch=dispatch)
            await composite.ensure_projection()
            route = await _route_for(composite, store, "question-1")
            assert route["delivery_state"] == "intent"
            assert await _append(store, "The direct answer")
            assert await _answer_rows(store) == []
            release.set_result({"delivery": "landed"})
            published = await composite.publish({
                "request_id": "publish:" + route["dispatch_id"],
                "composite_stream_id": ASSISTANT, "dispatch_id": route["dispatch_id"],
                "reply_to_message_id": "question-1", "reply_to_question_id": None,
                "publish_kind": "prose", "response_state": "final",
                "message": "The direct answer", "attachment_ids": [], "evidence_refs": [],
            }, actor_stream_id=ROOT)
            assert published["publication_key"] == "publish:" + route["dispatch_id"]
            rows = await _answer_rows(store)
            assert len(rows) == 1 and rows[0]["text"] == "The direct answer"
            assert rows[0]["publish_kind"] == "prose"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_ambiguous_route_states_suppress_and_failed_releases():
    async def _go():
        for state in ("intent", "committed_pending", "uncertain", "landed"):
            store = Store(":memory:")
            store.start()
            try:
                root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
                release = asyncio.Future()
                async def dispatch(_route):
                    return await release
                composite = AssistantComposite(store, config=_config(root["session_generation"]),
                                               dispatch=dispatch)
                await composite.ensure_projection()
                route = await _route_for(composite, store, "question-" + state)
                if state != "intent":
                    await store.update_assistant_composite_route(
                        route["route_id"], routing_state="resolved", delivery_state=state,
                    )
                await _append(store, "reply while " + state)
                assert await _answer_rows(store) == [], state
                await store.update_assistant_composite_route(
                    route["route_id"], routing_state="resolved", delivery_state="failed",
                )
                await _append(store, "standalone after failure " + state)
                rows = await _answer_rows(store)
                assert len(rows) == 1 and rows[0]["publish_kind"] == "status", state
                await composite.stop()
            finally:
                store.stop()
    asyncio.run(_go())


def test_publication_first_reply_is_not_mirrored_and_public_guard_remains():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]),
                                           dispatch=lambda _route: asyncio.sleep(100))
            await composite.ensure_projection()
            try:
                await composite.publish({
                    "request_id": "unauthorized", "composite_stream_id": ASSISTANT,
                    "dispatch_id": "", "publish_kind": "status", "message": "No route",
                }, actor_stream_id=ROOT)
            except ValueError as exc:
                assert str(exc) == "assistant_publish_required_fields"
            else:
                raise AssertionError("public no-dispatch status bypassed its guard")
            route = await _route_for(composite, store, "question-2")
            await composite.publish({
                "request_id": "publish:" + route["dispatch_id"],
                "composite_stream_id": ASSISTANT, "dispatch_id": route["dispatch_id"],
                "reply_to_message_id": "question-2", "reply_to_question_id": None,
                "publish_kind": "prose", "response_state": "final", "message": "Published first",
                "attachment_ids": [], "evidence_refs": [],
            }, actor_stream_id=ROOT)
            await _append(store, "Published first")
            rows = await _answer_rows(store)
            assert len(rows) == 1 and rows[0]["publish_kind"] == "prose"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_kv_disable_invalid_value_and_nonprose_are_excluded():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]))
            await composite.ensure_projection()
            for kind in ("USER", "TOOL_RESULT", "THINKING", "SYSTEM", "TELL"):
                await _append(store, "never show " + kind, kind=kind)
            await _append(store, "   ")
            await _append(store, "❯")
            assert await _answer_rows(store) == []
            await store.put("assistant.mirror.enabled", "0")
            assert (await store.assistant_mirror_state()) == {
                "enabled": False, "source": "kv",
                "binding": {"composite_stream_id": ASSISTANT,
                            "source_stream_id": ROOT,
                            "source_generation": root["session_generation"]},
            }
            await _append(store, "while disabled")
            await store.put("assistant.mirror.enabled", "invalid")
            assert (await store.assistant_mirror_state())["source"] == "kv_invalid"
            await _append(store, "while invalid")
            assert await _answer_rows(store) == []
            await store.put("assistant.mirror.enabled", "on")
            await _append(store, "now visible")
            rows = await _answer_rows(store)
            assert len(rows) == 1 and rows[0]["text"] == "now visible"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_unbound_generation_and_other_seat_never_project():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config("retired-generation"))
            await composite.ensure_projection()
            await _append(store, "from wrong generation")
            await store.open_session("fixture-child", "worker", provider="codex")
            await store.append_session_events_lifecycle_cas([
                {"stream_id": "fixture-child:worker",
                 "event": {**_source_event("from child"), "stream_id": "fixture-child:worker"},
                 "identity": "child-line"},
            ], limit=20)
            assert await _answer_rows(store) == []
            assert root["session_generation"] != "retired-generation"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_unfenced_source_append_never_projects():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]))
            await composite.ensure_projection()
            assert await store.append_session_event(
                ROOT, _source_event("unfenced single"), identity="unfenced-single", limit=20,
            )
            assert await store.append_session_events_lifecycle_cas([
                {"stream_id": ROOT, "event": _source_event("unfenced batch"),
                 "identity": "unfenced-batch"},
            ], limit=20)
            assert await _answer_rows(store) == []
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_restart_replay_and_generation_rebind(tmp_path):
    async def _go():
        database = str(tmp_path / "mirror.sqlite")
        store = Store(database)
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            old_generation = root["session_generation"]
            composite = AssistantComposite(store, config=_config(old_generation))
            await composite.ensure_projection()
            assert await _append(store, "before restart", identity="persisted-source")
            first_id = (await _answer_rows(store))[0]["daemon_seq"]
            await composite.stop()
        finally:
            store.stop()

        store = Store(database)
        store.start()
        try:
            composite = AssistantComposite(store, config=_config(old_generation))
            await composite.ensure_projection()
            replay = await store.append_session_events_lifecycle_cas([
                {"stream_id": ROOT, "event": {**_source_event("before restart"),
                 "raw": {"source_session_identity": "fixture-session", "message_id": "persisted-source"}},
                 "identity": "persisted-source",
                 "lifecycle": await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")},
            ], limit=20)
            assert replay == [None]
            assert [row["daemon_seq"] for row in await _answer_rows(store)] == [first_id]

            await store.update_session("fixture-root", "visible", status="closed")
            replacement = await store.open_session(
                "fixture-root", "visible", provider="codex", pane_pid="4242",
            )
            assert replacement["session_generation"] != old_generation
            await _append(store, "before rebind", identity="new-generation-unbound")
            assert [row["daemon_seq"] for row in await _answer_rows(store)] == [first_id]
            await composite.stop()

            rebound = AssistantComposite(store, config=_config(replacement["session_generation"]))
            await rebound.ensure_projection()
            assert await _append(store, "after rebind", identity="new-generation-bound")
            rows = await _answer_rows(store)
            assert [row["text"] for row in rows] == ["before restart", "after rebind"]
            assert rows[-1]["raw"]["mirrored_from"]["generation"] == replacement["session_generation"]
            await rebound.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_concurrent_source_and_publication_have_one_published_row():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]),
                                           dispatch=lambda _route: asyncio.sleep(100))
            await composite.ensure_projection()
            route = await _route_for(composite, store, "question-race")
            await asyncio.gather(
                _append(store, "serialized reply", identity="race-source"),
                composite.publish({
                    "request_id": "publish:" + route["dispatch_id"],
                    "composite_stream_id": ASSISTANT, "dispatch_id": route["dispatch_id"],
                    "reply_to_message_id": "question-race", "reply_to_question_id": None,
                    "publish_kind": "prose", "response_state": "final",
                    "message": "serialized reply", "attachment_ids": [], "evidence_refs": [],
                }, actor_stream_id=ROOT),
            )
            rows = await _answer_rows(store)
            assert len(rows) == 1 and rows[0]["publish_kind"] == "prose"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_live_projection_reuses_history_sequence_and_replay_does_not_broadcast():
    async def _go():
        store = Store(":memory:")
        store.start()
        frames = []
        async def broadcast(frame):
            frames.append(frame)
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]))
            await composite.ensure_projection()
            event = _source_event("live milestone")
            first = await append_ingested_event(
                store, broadcast, event, recent_limit=20,
                lifecycle=await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242"),
            )
            assert isinstance(first, int)
            assert len(frames) == 2
            assert frames[0]["event"]["stream_id"] == ROOT
            assert frames[1]["event"]["stream_id"] == ASSISTANT
            history = await _answer_rows(store)
            assert len(history) == 1
            assert frames[1]["event"]["daemon_seq"] == history[0]["daemon_seq"]
            second = await append_ingested_event(
                store, broadcast, event, recent_limit=20,
                lifecycle=await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242"),
            )
            assert second is None and len(frames) == 2
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_publication_text_dedupe_expires_at_120_seconds():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]),
                                           dispatch=lambda _route: asyncio.sleep(100))
            await composite.ensure_projection()
            route = await _route_for(composite, store, "question-time")
            key = "publish:" + route["dispatch_id"]
            await composite.publish({
                "request_id": key, "composite_stream_id": ASSISTANT,
                "dispatch_id": route["dispatch_id"], "reply_to_message_id": "question-time",
                "reply_to_question_id": None, "publish_kind": "prose", "response_state": "final",
                "message": "same text", "attachment_ids": [], "evidence_refs": [],
            }, actor_stream_id=ROOT)
            async def set_age(seconds):
                stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
                await store.submit(lambda conn: (conn.execute(
                    "UPDATE v2_assistant_composite_publications SET created_at=? WHERE publication_key=?",
                    (stamp, key)), conn.commit()))
            await set_age(119)
            await _append(store, "same text", identity="same-119")
            assert len(await _answer_rows(store)) == 1
            await _append(store, "same text ", identity="different-byte-119")
            assert len(await _answer_rows(store)) == 2
            await set_age(120.5)
            await _append(store, "same text", identity="same-120")
            rows = await _answer_rows(store)
            assert len(rows) == 3 and rows[-1]["publish_kind"] == "status"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_wrong_dispatch_generation_does_not_suppress_current_source():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]),
                                           dispatch=lambda _route: asyncio.sleep(100))
            await composite.ensure_projection()
            route = await _route_for(composite, store, "question-other-generation")
            await store.update_assistant_composite_route(
                route["route_id"], routing_state="resolved", delivery_state="intent",
                route_target_generation="another-generation",
            )
            await _append(store, "unrelated to stale route")
            rows = await _answer_rows(store)
            assert len(rows) == 1 and rows[0]["publish_kind"] == "status"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_projection_unavailable_rolls_source_insert_back():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]))
            await composite.ensure_projection()
            await store.submit(lambda conn: (conn.execute(
                "DELETE FROM sessions WHERE host='fixture-chat' AND session_name='assistant'"),
                conn.commit()))
            try:
                await _append(store, "must remain atomic")
            except ValueError as exc:
                assert str(exc) == "assistant_mirror_projection_unavailable"
            else:
                raise AssertionError("source insert committed without canonical projection")
            assert await store.fetch_session_event_tail(ROOT, limit=20) == []
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_inspect_readback_exposes_effective_toggle_and_binding():
    from server import Server
    from sessions import Sessions
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]))
            await composite.ensure_projection()
            sessions = Sessions(store, tmux=None, local_host="fixture-chat")
            await sessions.refresh()
            server = Server(store=store, sessions=sessions, local_host="fixture-chat")
            server.assistant_composite = composite
            before = await server._on_inspect_stream({
                "host": "fixture-chat", "session_name": "assistant", "event_tail": 0,
            })
            assert before["session"]["assistant_mirror"]["enabled"] is True
            assert before["session"]["assistant_mirror"]["source"] == "env"
            await store.put("assistant.mirror.enabled", "off")
            after = await server._on_inspect_stream({
                "host": "fixture-chat", "session_name": "assistant", "event_tail": 0,
            })
            assert after["session"]["assistant_mirror"]["enabled"] is False
            assert after["session"]["assistant_mirror"]["source"] == "kv"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())
