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


def _config_for(stream_id: str, generation: str) -> AssistantCompositeConfig:
    return AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": ASSISTANT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": stream_id,
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
                            "source_generation": root["session_generation"],
                            "source_binding": "env"},
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


def test_remote_claude_binding_fenced_prose_is_projected():
    """A remote (satellite event.push) claude bound seat carries a
    ``claude_binding``, not a ``lifecycle`` snapshot — the lifecycle CAS is
    codex-only. Its dispatch-free turn text must still mirror to the canonical
    chat, exactly as a daemon-local seat's does through local ingest.

    Regression: pentacle__bart_proactive_chat_status_publish_2026_09 — Bart's
    bound front desk stopped reaching the chat after moving from a Thoth-local
    seat to a remote Amaterasu (WSL) seat, whose events arrive via event.push
    with lifecycle=None."""
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session(
                "fixture-root", "visible", provider="claude", pane_pid="4242",
            )
            composite = AssistantComposite(store, config=_config(root["session_generation"]))
            await composite.ensure_projection()
            event = {
                "stream_id": ROOT,
                "provider": "claude",
                "kind": "ASSIST_TEXT",
                "text": "A remote proactive milestone",
                "session_id": "fixture-session",
                "timestamp": "2026-09-27T05:00:00.000Z",
                "raw": {
                    "source_session_identity": "fixture-session",
                    "jsonl_record_uuid": "fixture-record",
                    "transport": "claude-jsonl",
                    "stop_reason": "end_turn",
                },
            }
            inserted = await store.append_session_events_lifecycle_cas(
                [{"stream_id": ROOT, "event": event, "identity": "remote-message",
                  "claude_binding": "fixture-session"}], limit=20,
            )
            assert inserted and isinstance(inserted[0], int)
            mirrored = [e for e in await store.fetch_session_event_tail(ASSISTANT, limit=20)
                        if e["kind"] == "ASSIST_TEXT"]
            assert len(mirrored) == 1
            assert mirrored[0]["text"] == event["text"]
            assert mirrored[0]["publish_kind"] == "status"
            assert mirrored[0]["raw"]["mirrored_from"]["stream_id"] == ROOT
            assert mirrored[0]["raw"]["mirrored_from"]["generation"] == root["session_generation"]
            # Idempotent: the same event.push replay must not double-post.
            replay = await store.append_session_events_lifecycle_cas(
                [{"stream_id": ROOT, "event": event, "identity": "remote-message",
                  "claude_binding": "fixture-session"}], limit=20,
            )
            assert replay == [None]
            assert len([e for e in await store.fetch_session_event_tail(ASSISTANT, limit=20)
                        if e["kind"] == "ASSIST_TEXT"]) == 1
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


async def _set_durable_binding(store: Store, stream_id: str, generation: str):
    """Seed the committed durable rebind state (what a hot rebind leaves behind).

    The rebind verb, its authorization and audit are tested by the hot-rebind
    lane; here we only need the post-commit binding row to prove the mirror
    admission follows it without a restart or reconfigure.
    """
    def _op(conn):
        conn.execute(
            "INSERT INTO v2_assistant_direct_binding(id,stream_id,generation,revision,updated_at) "
            "VALUES(1,?,?,1,?) ON CONFLICT(id) DO UPDATE SET "
            "stream_id=excluded.stream_id,generation=excluded.generation,"
            "revision=excluded.revision,updated_at=excluded.updated_at",
            (stream_id, generation, "2026-09-27T12:45:33.000Z"),
        )
        conn.commit()
    await store.submit(_op)


async def _append_from(store: Store, stream_id: str, pane_pid: str, text: str, identity: str):
    event = {**_source_event(text), "stream_id": stream_id,
             "raw": {"source_session_identity": "fixture-session", "message_id": identity}}
    result = await store.append_session_events_lifecycle_cas(
        [{"stream_id": stream_id, "event": event, "identity": identity,
          "lifecycle": await store.fetch_open_session_lifecycle(stream_id, pane_pid=pane_pid)}], limit=20,
    )
    assert result is not None
    return result[0]


def test_durable_hot_rebind_moves_mirror_source_without_restart():
    """A no-restart durable rebind must carry the mirror to the new seat."""
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            # Startup binds the mirror to the env source seat A.
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]))
            await composite.ensure_projection()

            # A new front-desk seat B is opened and the daemon is hot-rebound to
            # it via the durable binding table -- no restart, no reconfigure.
            newseat = await store.open_session("fixture-newdesk", "visible", provider="codex", pane_pid="5252")
            await _set_durable_binding(store, "fixture-newdesk:visible", newseat["session_generation"])

            # B's dispatch-free prose now mirrors, with B's effective provenance.
            b_id = await _append_from(store, "fixture-newdesk:visible", "5252",
                                      "update from the rebound desk", "b-line")
            rows = await _answer_rows(store)
            assert [row["text"] for row in rows] == ["update from the rebound desk"]
            assert rows[-1]["raw"]["mirrored_from"] == {
                "stream_id": "fixture-newdesk:visible",
                "generation": newseat["session_generation"],
                "event_id": b_id,
                "event_ts": "2026-09-27T05:00:00.000Z",
            }

            # The retired env-pinned seat A no longer mirrors after the rebind.
            await _append(store, "stale line from the old desk", identity="a-line")
            assert [row["text"] for row in await _answer_rows(store)] == ["update from the rebound desk"]

            # Readback reflects the live durable source.
            state = await store.assistant_mirror_state()
            assert state["binding"]["source_stream_id"] == "fixture-newdesk:visible"
            assert state["binding"]["source_generation"] == newseat["session_generation"]
            assert state["binding"]["source_binding"] == "durable"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_dispatched_reply_under_durable_binding_suppresses_and_publishes_once():
    """When the effective source is the durable-bound seat, a dispatched reply
    still suppresses the source line and leaves exactly one published row: the
    open-route suppression and 120s dedupe joins must key on the effective
    (durable) generation, not the startup env pair."""
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            desk = await store.open_session("fixture-newdesk", "visible", provider="codex", pane_pid="5252")
            # Routing and the mirror are bound to seat B through the durable table.
            await _set_durable_binding(store, "fixture-newdesk:visible", desk["session_generation"])
            release = asyncio.Future()
            async def dispatch(_route):
                return await release
            composite = AssistantComposite(
                store, config=_config_for("fixture-newdesk:visible", desk["session_generation"]),
                dispatch=dispatch)
            await composite.ensure_projection()
            assert (await store.assistant_mirror_state())["binding"]["source_binding"] == "durable"

            route = await _route_for(composite, store, "question-1")
            assert route["route_target"] == "fixture-newdesk:visible"
            assert route["route_target_generation"] == desk["session_generation"]
            assert route["delivery_state"] == "intent"

            # The dispatched reply arrives from B while the dispatch is open.
            assert await _append_from(store, "fixture-newdesk:visible", "5252", "The direct answer", "b-reply")
            assert await _answer_rows(store) == []

            release.set_result({"delivery": "landed"})
            published = await composite.publish({
                "request_id": "publish:" + route["dispatch_id"],
                "composite_stream_id": ASSISTANT, "dispatch_id": route["dispatch_id"],
                "reply_to_message_id": "question-1", "reply_to_question_id": None,
                "publish_kind": "prose", "response_state": "final",
                "message": "The direct answer", "attachment_ids": [], "evidence_refs": [],
            }, actor_stream_id="fixture-newdesk:visible")
            assert published["publication_key"] == "publish:" + route["dispatch_id"]
            rows = await _answer_rows(store)
            assert len(rows) == 1
            assert rows[0]["text"] == "The direct answer" and rows[0]["publish_kind"] == "prose"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_corrupt_durable_binding_fails_closed_without_breaking_ingest():
    """A malformed durable row suppresses the mirror but never breaks ingest."""
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]))
            await composite.ensure_projection()

            # A partial durable row (stream present, generation blank) is corrupt.
            def _corrupt(conn):
                conn.execute(
                    "INSERT INTO v2_assistant_direct_binding(id,stream_id,generation,revision,updated_at) "
                    "VALUES(1,?,?,1,?)", ("fixture-root:visible", "", "2026-09-27T12:45:33.000Z"),
                )
                conn.commit()
            await store.submit(_corrupt)

            # Source ingest still commits; nothing mirrors (fail closed).
            src_id = await _append(store, "line during corrupt binding", identity="corrupt-line")
            assert isinstance(src_id, int)
            assert await _answer_rows(store) == []
            state = await store.assistant_mirror_state()
            assert state["binding"]["source_binding"] == "unavailable"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_published_dispatch_final_with_different_markdown_is_not_mirrored():
    """Replay the installed journey on an isolated composite, with real normalization."""
    from claude_jsonl_norm import normalize_claude_jsonl_record

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="claude", pane_pid="4242")
            async def dispatch(_route):
                return {"delivery": "landed"}
            composite = AssistantComposite(store, config=_config(root["session_generation"]), dispatch=dispatch)
            await composite.ensure_projection()
            route = await _route_for(composite, store, "markdown-question")
            import json
            envelope = json.loads(route["route_json"])["direct_envelope"]["wire_body"]
            async def ingest(record):
                event = normalize_claude_jsonl_record(record, host="fixture-root", session_name="visible")[0]
                seqs = await store.append_session_events_lifecycle_cas([
                    {"stream_id": ROOT, "event": event, "identity": record["uuid"],
                     "lifecycle": await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")}
                ], limit=50)
                return seqs[0]
            await ingest({"type": "user", "uuid": "dispatch-user", "sessionId": "fixture-transcript",
                          "timestamp": "2026-09-28T15:42:09.423Z",
                          "message": {"role": "user", "content": envelope}})
            published = await composite.publish({
                "request_id": "publish:" + route["dispatch_id"], "composite_stream_id": ASSISTANT,
                "dispatch_id": route["dispatch_id"], "reply_to_message_id": "markdown-question",
                "reply_to_question_id": None, "publish_kind": "prose", "response_state": "final",
                "message": "**Published answer**", "attachment_ids": [], "evidence_refs": [],
            }, actor_stream_id=ROOT)
            seq = await ingest({"type": "assistant", "uuid": "dispatch-final", "parentUuid": "dispatch-user",
                                "sessionId": "fixture-transcript", "timestamp": "2026-09-28T15:42:34.211Z",
                                "message": {"role": "assistant", "id": "final-message", "stop_reason": "end_turn",
                                            "content": [{"type": "text", "text": "**Published answer**\n"}]}})
            rows = await _answer_rows(store)
            assert len(rows) == 1, [(r["publish_kind"], r["text"]) for r in rows]
            assert rows[0]["publish_kind"] == "prose" and rows[0]["text"] == "**Published answer**"
            assert await store.assistant_mirror_event_for_source(seq) is None
            assert published["publication_key"] == "publish:" + route["dispatch_id"]
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


async def _normalized_turn_event(store, *, provider, kind, text, identity, session="turn-session", final=False, legacy=False, timestamp=None, sidechain=False):
    from claude_jsonl_norm import normalize_claude_jsonl_record
    from codex_rollout_norm import normalize_codex_rollout_record
    stamp = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if provider == "claude":
        record = {"type": "assistant" if kind == "ASSIST_TEXT" else "user", "uuid": identity,
                  "sessionId": session, "timestamp": stamp, "isSidechain": sidechain,
                  "message": {"role": "assistant" if kind == "ASSIST_TEXT" else "user",
                              "id": identity, "stop_reason": "end_turn" if final else "tool_use",
                              "content": [{"type": "text", "text": text}]}}
        event = normalize_claude_jsonl_record(record, host="fixture-root", session_name="visible")[0]
    else:
        record = {"type": "response_item", "timestamp": stamp,
                  "payload": {"type": "message", "id": identity,
                              "role": "assistant" if kind == "ASSIST_TEXT" else "user",
                              "phase": "final_answer" if final else "commentary",
                              "content": [{"type": "output_text" if kind == "ASSIST_TEXT" else "input_text", "text": text}]}}
        event = normalize_codex_rollout_record(record, host="fixture-root", session_name="visible", session_id=session)[0]
    if legacy:
        event["raw"].pop("phase", None)
        event["raw"].pop("stop_reason", None)
    result = await store.append_session_events_lifecycle_cas([
        {"stream_id": ROOT, "event": event, "identity": identity,
         "lifecycle": await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")}
    ], limit=100)
    return result[0]


def test_sidechain_final_does_not_fence_primary_published_turn():
    import json

    async def _go():
        store = Store(":memory:")
        store.start()
        composite = None
        try:
            root = await store.open_session("fixture-root", "visible", provider="claude", pane_pid="4242")
            async def dispatch(_route):
                return {"delivery": "landed"}
            composite = AssistantComposite(store, config=_config(root["session_generation"]), dispatch=dispatch)
            await composite.ensure_projection()
            route = await _route_for(composite, store, "sidechain-question")
            envelope = json.loads(route["route_json"])["direct_envelope"]["wire_body"]
            await _normalized_turn_event(store, provider="claude", kind="USER", text=envelope, identity="primary-user")
            await composite.publish({
                "request_id": "publish:" + route["dispatch_id"], "composite_stream_id": ASSISTANT,
                "dispatch_id": route["dispatch_id"], "reply_to_message_id": "sidechain-question",
                "publish_kind": "prose", "response_state": "final", "message": "**Published answer**",
                "attachment_ids": [], "evidence_refs": [],
            }, actor_stream_id=ROOT)
            # Preserve sidechain ingestion/fallback; its provider final is not a primary boundary.
            side = await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT",
                text="**Published answer**", identity="sidechain-final", final=True, sidechain=True)
            seq = await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT",
                text="Primary answer with different formatting", identity="primary-final", final=True)
            assert await store.assistant_mirror_event_for_source(seq) is None
            assert len(await _answer_rows(store)) == 1
            assert side is not None and seq is not None  # source transcript preserved
            next_seq = await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT",
                text="**Published answer**", identity="ordinary-final", final=True)
            assert await store.assistant_mirror_event_for_source(next_seq) is not None
            assert len(await _answer_rows(store)) == 2
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()
    asyncio.run(_go())


def test_structured_dispatch_turn_correlation_and_next_non_dispatch_reply():
    import json

    async def _go():
        for provider in ("claude", "codex"):
            store = Store(":memory:")
            store.start()
            try:
                root = await store.open_session("fixture-root", "visible", provider=provider, pane_pid="4242")
                async def dispatch(_route):
                    return {"delivery": "landed"}
                composite = AssistantComposite(store, config=_config(root["session_generation"]), dispatch=dispatch)
                await composite.ensure_projection()
                # A prior final must delimit this turn even if its ingestion lagged publication.
                await _normalized_turn_event(store, provider=provider, kind="ASSIST_TEXT", text="Earlier proactive reply",
                                             identity="previous-final", final=True)
                route = await _route_for(composite, store, "turn-question")
                envelope = json.loads(route["route_json"])["direct_envelope"]["wire_body"]
                await _normalized_turn_event(store, provider=provider, kind="USER", text=envelope, identity="turn-user")
                payload = {"request_id": "publish:" + route["dispatch_id"], "composite_stream_id": ASSISTANT,
                           "dispatch_id": route["dispatch_id"], "reply_to_message_id": "turn-question",
                           "reply_to_question_id": None, "publish_kind": "prose", "response_state": "final",
                           "message": "**The answer**", "attachment_ids": [], "evidence_refs": []}
                # Classified commentary is not a final/legacy turn boundary.
                await _normalized_turn_event(store, provider=provider, kind="ASSIST_TEXT", text="Working on the answer",
                                             identity="turn-commentary", final=False)
                await composite.publish(payload, actor_stream_id=ROOT)
                assert (await composite.publish(payload, actor_stream_id=ROOT))["duplicate"] is True
                # Other input in this same provider turn must not hide the dispatch identity.
                await _normalized_turn_event(store, provider=provider, kind="USER", text="Peer context", identity="peer-input")
                def age_publication(conn):
                    conn.execute("UPDATE v2_assistant_composite_publications SET created_at=? WHERE dispatch_id=?",
                                 ((datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(), route["dispatch_id"]))
                    conn.commit()
                await store.submit(age_publication)
                seq = await _normalized_turn_event(store, provider=provider, kind="ASSIST_TEXT", text="The answer, formatted differently",
                                                  identity="turn-final", final=True)
                assert await store.assistant_mirror_event_for_source(seq) is None
                assert len(await _answer_rows(store)) == 2  # prior proactive + one publication
                if provider == "claude":
                    from claude_jsonl_norm import normalize_claude_jsonl_record
                    blocks = normalize_claude_jsonl_record({
                        "type": "assistant", "uuid": "turn-final", "sessionId": "turn-session",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "message": {"id": "turn-final", "stop_reason": "end_turn", "content": [
                            {"type": "text", "text": "The answer, formatted differently"},
                            {"type": "text", "text": "Second block from the same final"},
                        ]},
                    }, host="fixture-root", session_name="visible")
                    seqs = await store.append_session_events_lifecycle_cas([
                        {"stream_id": ROOT, "event": blocks[1], "identity": "turn-final-block-1",
                         "lifecycle": await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")}
                    ], limit=100)
                    assert await store.assistant_mirror_event_for_source(seqs[0]) is None
                    assert len(await _answer_rows(store)) == 2
                def fresh_publication(conn):
                    conn.execute("UPDATE v2_assistant_composite_publications SET created_at=? WHERE dispatch_id=?",
                                 (datetime.now(timezone.utc).isoformat(), route["dispatch_id"]))
                    conn.commit()
                await store.submit(fresh_publication)
                # No new USER is needed: a new final boundary alone ends the published turn.
                seq = await _normalized_turn_event(store, provider=provider, kind="ASSIST_TEXT", text="**The answer**",
                                                  identity="non-dispatch-final", final=True)
                assert await store.assistant_mirror_event_for_source(seq) is not None
                assert len(await _answer_rows(store)) == 3
                # Replay does not broadcast or insert another answer.
                assert await _normalized_turn_event(store, provider=provider, kind="ASSIST_TEXT", text="**The answer**",
                                                    identity="non-dispatch-final", final=True) is None
                # The old envelope in another transcript cannot suppress this transcript's final.
                seq = await _normalized_turn_event(store, provider=provider, kind="ASSIST_TEXT", text="New transcript final",
                                                  identity="foreign-final", session="other-transcript", final=True)
                assert await store.assistant_mirror_event_for_source(seq) is not None
                await composite.stop()
            finally:
                store.stop()
    asyncio.run(_go())


def test_isolated_socket_publishes_one_markdown_reply_and_keeps_next_mirror(tmp_path, isolated_tmux_env):
    """Exercise publication authentication, mirror broadcasting and history on a real socket."""
    import hashlib
    import json
    import websockets
    from server import Server
    from sessions import Sessions
    from store import STREAM_TOKEN_HASH_VERSION
    from ingest import broadcast_assistant_mirror

    async def _go():
        store = Store(str(tmp_path / "isolated-replies.db"))
        store.start()
        server = composite = None
        try:
            root = await store.open_session("fixture-root", "visible", provider="claude", pane_pid="4242")
            token = "isolated-reply-token"
            await store.grant_stream_token("fixture-root", "visible", hashlib.sha256(token.encode()).hexdigest(),
                                           STREAM_TOKEN_HASH_VERSION)
            async def dispatch(_route):
                return {"delivery": "landed"}
            composite = AssistantComposite(store, config=_config(root["session_generation"]), dispatch=dispatch)
            await composite.ensure_projection()
            sessions = Sessions(store, local_host="fixture-chat")
            await sessions.refresh()
            server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, local_host="fixture-chat")
            server.assistant_composite = composite
            composite.broadcast = server.broadcast
            port = await server.bind()
            async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
                assert json.loads(await socket.recv())["type"] == "welcome"
                await socket.send(json.dumps({"type": "hello", "client": "isolated-reply-proof",
                                              "stream_token": token, "from_stream_id": ROOT,
                                              "capabilities": {"assistant_composite_v1": True}}))
                hello_frames = [json.loads(await socket.recv()) for _ in range(2)]
                assert any(frame["type"] == "snapshot" for frame in hello_frames)
                canonical_pushes = []
                async def rpc(payload):
                    await socket.send(json.dumps(payload))
                    async with asyncio.timeout(3):
                        while True:
                            frame = json.loads(await socket.recv())
                            if frame.get("type") == "chat.event" and frame.get("event", {}).get("stream_id") == ASSISTANT:
                                canonical_pushes.append(frame["event"])
                            if frame.get("request_id") == payload["request_id"]:
                                return frame
                route = await _route_for(composite, store, "socket-question")
                envelope = json.loads(route["route_json"])["direct_envelope"]["wire_body"]
                await _normalized_turn_event(store, provider="claude", kind="USER", text=envelope, identity="socket-user")
                payload = {"type": "assistant.publish", "request_id": "publish:" + route["dispatch_id"],
                           "composite_stream_id": ASSISTANT, "dispatch_id": route["dispatch_id"],
                           "reply_to_message_id": "socket-question", "publish_kind": "prose", "response_state": "final",
                           "message": "**One answer**\n\n- A list item\n\n`code`", "attachment_ids": [], "evidence_refs": []}
                assert (await rpc(payload))["type"] == "assistant.publish.ok"
                assert (await rpc(payload))["type"] == "assistant.publish.ok"
                seq = await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT", text="Different final formatting",
                                                  identity="socket-final", final=True)
                await broadcast_assistant_mirror(store, server.broadcast, seq)
                # A barrier drains all earlier socket pushes, proving no duplicate broadcast.
                await rpc({"type": "assistant.binding", "request_id": "after-final"})
                assert len([e for e in canonical_pushes if e["kind"] == "ASSIST_TEXT"]) == 1
                assert len(await _answer_rows(store)) == 1
                assert (await _answer_rows(store))[0]["text"] == payload["message"]
                seq = await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT", text="Next proactive reply",
                                                  identity="socket-proactive", final=True)
                await broadcast_assistant_mirror(store, server.broadcast, seq)
                await rpc({"type": "assistant.binding", "request_id": "after-proactive"})
                assert [e["text"] for e in canonical_pushes if e["kind"] == "ASSIST_TEXT"] == [payload["message"], "Next proactive reply"]
                assert len(await _answer_rows(store)) == 2
        finally:
            if composite is not None:
                await composite.stop()
            if server is not None:
                await server.close()
            store.stop()
    asyncio.run(_go())


def test_legacy_final_fences_old_dispatch_after_phase_upgrade():
    import json

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            async def dispatch(_route):
                return {"delivery": "landed"}
            composite = AssistantComposite(store, config=_config(root["session_generation"]), dispatch=dispatch)
            await composite.ensure_projection()
            route = await _route_for(composite, store, "legacy-question")
            envelope = json.loads(route["route_json"])["direct_envelope"]["wire_body"]
            await _normalized_turn_event(store, provider="codex", kind="USER", text=envelope, identity="legacy-user")
            await composite.publish({
                "request_id": "publish:" + route["dispatch_id"], "composite_stream_id": ASSISTANT,
                "dispatch_id": route["dispatch_id"], "reply_to_message_id": "legacy-question",
                "publish_kind": "prose", "response_state": "final", "message": "Legacy reply",
                "attachment_ids": [], "evidence_refs": [],
            }, actor_stream_id=ROOT)
            await _normalized_turn_event(store, provider="codex", kind="ASSIST_TEXT", text="Legacy reply",
                                         identity="legacy-final", final=True, legacy=True)
            seq = await _normalized_turn_event(store, provider="codex", kind="ASSIST_TEXT", text="First upgraded proactive reply",
                                              identity="upgraded-final", final=True)
            assert await store.assistant_mirror_event_for_source(seq) is not None
            assert [r["text"] for r in await _answer_rows(store)] == ["Legacy reply", "First upgraded proactive reply"]
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_publication_precedes_delayed_source_transcript_rows():
    import json

    async def _go():
        for provider in ("claude", "codex"):
            store = Store(":memory:")
            store.start()
            try:
                root = await store.open_session("fixture-root", "visible", provider=provider, pane_pid="4242")
                async def dispatch(_route):
                    return {"delivery": "landed"}
                composite = AssistantComposite(store, config=_config(root["session_generation"]), dispatch=dispatch)
                await composite.ensure_projection()
                route = await _route_for(composite, store, "late-question")
                user_stamp = datetime.now(timezone.utc).isoformat()
                prior_stamp = (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
                await composite.publish({
                    "request_id": "publish:" + route["dispatch_id"], "composite_stream_id": ASSISTANT,
                    "dispatch_id": route["dispatch_id"], "reply_to_message_id": "late-question",
                    "publish_kind": "prose", "response_state": "final", "message": "**Late answer**",
                    "attachment_ids": [], "evidence_refs": [],
                }, actor_stream_id=ROOT)
                # Even an earlier turn's source final can arrive after the canonical publication.
                await _normalized_turn_event(store, provider=provider, kind="ASSIST_TEXT", text="Earlier source reply",
                                             identity="late-previous-final", final=True, timestamp=prior_stamp)
                envelope = json.loads(route["route_json"])["direct_envelope"]["wire_body"]
                await _normalized_turn_event(store, provider=provider, kind="USER", text=envelope, identity="late-user", timestamp=user_stamp)
                seq = await _normalized_turn_event(store, provider=provider, kind="ASSIST_TEXT", text="Late answer with different formatting",
                                                  identity="late-final", final=True)
                assert await store.assistant_mirror_event_for_source(seq) is None
                rows = await _answer_rows(store)
                assert len(rows) == 2 and {r["text"] for r in rows} == {"**Late answer**", "Earlier source reply"}
                assert [r["text"] for r in rows if r["publish_kind"] == "prose"] == ["**Late answer**"]
                source_rows = await store.fetch_session_event_tail(ROOT, limit=100)
                assert any(r["daemon_seq"] == seq and r["text"] == "Late answer with different formatting" for r in source_rows)
                seq = await _normalized_turn_event(store, provider=provider, kind="ASSIST_TEXT", text="Later ordinary reply",
                                                  identity="late-next-final", final=True)
                assert await store.assistant_mirror_event_for_source(seq) is not None
                await composite.stop()
            finally:
                store.stop()
    asyncio.run(_go())


def test_other_dispatch_publication_does_not_suppress_released_turn():
    import json

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="claude", pane_pid="4242")
            async def dispatch(_route):
                return {"delivery": "failed", "reason": "fixture-no-submit"}
            composite = AssistantComposite(store, config=_config(root["session_generation"]), dispatch=dispatch)
            await composite.ensure_projection()
            a = await _route_for(composite, store, "dispatch-a")
            await composite.publish({
                "request_id": "publish:" + a["dispatch_id"], "composite_stream_id": ASSISTANT,
                "dispatch_id": a["dispatch_id"], "reply_to_message_id": "dispatch-a",
                "publish_kind": "prose", "response_state": "final", "message": "A's publication",
                "attachment_ids": [], "evidence_refs": [],
            }, actor_stream_id=ROOT)
            b = await _route_for(composite, store, "dispatch-b")
            for _ in range(100):
                b = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="dispatch-b")
                if b["delivery_state"] == "failed":
                    break
                await asyncio.sleep(0.01)
            assert b["delivery_state"] == "failed"  # existing open-route suppression has been released
            assert a["dispatch_id"] != b["dispatch_id"] and a["route_target_generation"] == b["route_target_generation"]
            envelope = json.loads(b["route_json"])["direct_envelope"]["wire_body"]
            await _normalized_turn_event(store, provider="claude", kind="USER", text=envelope, identity="b-user")
            seq = await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT", text="B's reply without its own publication",
                                              identity="b-final", final=True)
            assert await store.assistant_mirror_event_for_source(seq) is not None
            assert [r["text"] for r in await _answer_rows(store)] == ["A's publication", "B's reply without its own publication"]
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())


def test_unknown_turn_metadata_preserves_dispatch_free_ingest():
    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="claude", pane_pid="4242")
            composite = AssistantComposite(store, config=_config(root["session_generation"]))
            await composite.ensure_projection()
            for i, raw in enumerate((None, [], "unstructured", {
                "transport": "claude-jsonl", "stop_reason": "end_turn",
                "source_session_identity": ["invalid-type"], "jsonl_record_uuid": "record",
            }, {
                "transport": "codex-rollout", "phase": "final_answer",
                "source_session_identity": "session", "jsonl_record_uuid": {"invalid": "type"},
            })):
                event = {**_source_event(f"Unknown metadata reply {i}"), "raw": raw}
                seqs = await store.append_session_events_lifecycle_cas([
                    {"stream_id": ROOT, "event": event, "identity": f"unknown:{i}",
                     "lifecycle": await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")}
                ], limit=20)
                assert seqs[0] and await store.assistant_mirror_event_for_source(seqs[0]) is not None
            assert len(await _answer_rows(store)) == 5
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(_go())
