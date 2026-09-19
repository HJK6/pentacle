"""Server transport edge: composite intercept only; direct panes stay direct."""

from __future__ import annotations

import asyncio
import json

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from notify import Notify
from server import Server
from sessions import Sessions, VerbError
from store import Store


COMPOSITE_HOST = "fixture-host-chat"
COMPOSITE_STREAM = "fixture-host-chat:assistant"
AUTHORITY_STREAM = "fixture-host-authority:authority"
LEAD_STREAM = "fixture-host-lead:lead"
CONVERSATION_STREAM = "fixture-host-conversation:conversation"
ROUTER_ENDPOINT = "ssh://fixture-router/assistant-router-v1"


class _Comms:
    def __init__(self):
        self.sent = []
        self.told = []

    async def send(self, msg):
        self.sent.append(dict(msg))
        return {"type": "send.result", "delivery": "landed"}

    async def tell(self, msg):
        self.told.append(dict(msg))
        return {"type": "tell.ok"}


async def _seed_dispatch(store, *, input_identity: str, dispatch_id: str, target: str, generation: str, lane_id=None):
    route = await store.admit_assistant_composite_input(
        stream_id=COMPOSITE_STREAM, input_identity=input_identity, input_request_id=input_identity,
        body="fixture dispatch", attachments=[], reply_to_message_id=None,
        reply_to_question_id=None, actor_stream_id="operator:fixture",
    )
    await store.update_assistant_composite_route(
        route["route_id"], routing_state="resolved", delivery_state="landed", dispatch_id=dispatch_id,
        route_target=target, route_target_generation=generation,
        route_payload={"schema_version": "assistant-router/v1", "disposition": "lane", "lane_id": lane_id,
                       "depends_on_message_id": None, "reason": "fixture"},
    )


def test_composite_intercepts_send_and_refuses_tell_without_changing_ordinary_panes() -> None:
    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=COMPOSITE_HOST)
            comms = _Comms()
            config = AssistantCompositeConfig(
                enabled=True, stream_id=COMPOSITE_STREAM,
                router_endpoint=ROUTER_ENDPOINT,
            )
            composite = AssistantComposite(store, config=config)
            await composite.ensure_projection()
            await store.open_session(COMPOSITE_HOST, "v2-direct", provider="codex")
            await sessions.refresh()
            server = Server(store=store, sessions=sessions, comms=comms, local_host=COMPOSITE_HOST)
            server.assistant_composite = composite

            accepted = await server._on_send({
                "host": COMPOSITE_HOST, "session_name": "assistant", "text": "hello",
                "request_id": "rpc-1", "optimistic_id": "stable-1",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            assert accepted["delivery"] == "accepted"
            assert not comms.sent

            try:
                await server._on_send({
                    "host": COMPOSITE_HOST, "session_name": "assistant", "text": "forged",
                    "request_id": "token-input", "optimistic_id": "token-input",
                    "_auth_context": {"token_verified": True, "stream_id": "fixture-host-lead:lead"},
                })
            except VerbError as exc:
                assert exc.code == "assistant_send_unauthorized"
            else:  # pragma: no cover - server-only operator intake is the assertion
                raise AssertionError("a backend token must not submit operator composite input")
            try:
                await server._on_send({
                    "host": COMPOSITE_HOST, "session_name": "assistant", "text": "attachment",
                    "request_id": "bad-attachment", "optimistic_id": "bad-attachment",
                    "attachments": [{"key": "not-a-blob", "mime": "text/plain"}],
                    "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
                })
            except VerbError as exc:
                assert exc.code == "attachment_invalid"
            else:  # pragma: no cover - attachment ids are not arbitrary dicts
                raise AssertionError("invalid attachment ref was accepted")

            direct = await server._on_send({
                "host": COMPOSITE_HOST, "session_name": "v2-direct", "text": "ordinary",
                "_assistant_composite_backend_dispatch": True,
            })
            assert direct["delivery"] == "landed"
            assert comms.sent and comms.sent[0]["text"] == "ordinary"
            assert "_assistant_composite_backend_dispatch" not in comms.sent[0]

            try:
                await server._on_tell({"to_stream_id": COMPOSITE_STREAM, "text": "no pane"})
            except VerbError as exc:
                assert exc.code == "assistant_composite_no_pane"
            else:  # pragma: no cover
                raise AssertionError("tell to a pane-less composite must be refused")
            assert not comms.told
        finally:
            store.stop()

    asyncio.run(_go())


def test_hello_advertises_capability_but_projects_composite_only_to_capable_client() -> None:
    class Socket:
        remote_address = ("127.0.0.1", 12345)

        async def send(self, _payload):
            return None

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=COMPOSITE_HOST)
            composite = AssistantComposite(store, config=AssistantCompositeConfig(
                enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
            ))
            await composite.ensure_projection()
            await sessions.refresh()
            server = Server(store=store, sessions=sessions, comms=_Comms(), local_host=COMPOSITE_HOST)
            server.assistant_composite = composite
            plain, capable = Socket(), Socket()
            server._register_client(plain)
            server._register_client(capable)
            try:
                hidden = await server._dispatch(json.dumps({"type": "hello"}), websocket=plain)
                shown = await server._dispatch(json.dumps({
                    "type": "hello", "capabilities": {"assistant_composite_v1": True},
                }), websocket=capable)
            finally:
                server._unregister_client(plain)
                server._unregister_client(capable)
            assert hidden[0]["capabilities"] == {"assistant_composite_v1": True}
            assert hidden[1]["capabilities"]["assistant_composite_v1"] is True
            assert all(row["stream_id"] != COMPOSITE_STREAM for row in hidden[1]["sessions"])
            row = next(row for row in shown[1]["sessions"] if row["stream_id"] == COMPOSITE_STREAM)
            assert row["session_kind"] == "assistant_composite"
            assert row["capabilities"]["pane"] is False
        finally:
            store.stop()

    asyncio.run(_go())


def test_configured_backend_suppresses_routine_ingress_but_forwards_explicit_escalation() -> None:
    """The filter is limited to configured hidden backends; direct panes remain direct."""
    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=COMPOSITE_HOST)
            await store.open_session("fixture-host-authority", "authority", provider="codex")
            await store.open_session(COMPOSITE_HOST, "ordinary", provider="codex")
            await sessions.refresh()
            comms = _Comms()
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM, luna_stream_id=CONVERSATION_STREAM,
                ),
            )
            await composite.ensure_projection()
            server = Server(store=store, sessions=sessions, comms=comms, local_host=COMPOSITE_HOST)
            server.assistant_composite = composite

            routine = await server._on_tell({
                "to_stream_id": AUTHORITY_STREAM, "tell_id": "routine-tell", "text": "status: worker heartbeat",
                "_auth_context": {"token_verified": True, "stream_id": "fixture-host-worker:worker"},
            })
            sent = await server._on_send({
                "host": "fixture-host-authority", "session_name": "authority", "request_id": "routine-send",
                "text": "routine worker update", "_auth_context": {"token_verified": True, "stream_id": "fixture-host-worker:worker"},
            })
            notice = await composite.suppress_routine_backend_ingress(
                target_stream_id=AUTHORITY_STREAM, body="routine durable notice",
                msg={"tell_id": "routine-notice", "from_stream_id": "fixture-host-worker:worker"}, verb="notice",
            )
            assert routine["assistant_backend_ingress"] == "persisted_suppressed"
            assert sent["assistant_backend_ingress"] == "persisted_suppressed"
            assert notice is not None and notice["assistant_backend_ingress"] == "persisted_suppressed"
            assert not comms.told and not comms.sent
            persisted = await store.fetch_session_event_tail(AUTHORITY_STREAM, limit=10)
            assert [event["raw"]["verb"] for event in persisted] == ["tell", "send", "notice"]

            forwarded = await server._on_tell({
                "to_stream_id": AUTHORITY_STREAM, "tell_id": "blocker-tell", "text": "BLOCKER: evidence unavailable",
                "_auth_context": {"token_verified": True, "stream_id": "fixture-host-worker:worker"},
            })
            assert forwarded["type"] == "tell.ok" and len(comms.told) == 1
            ordinary = await server._on_tell({
                "to_stream_id": f"{COMPOSITE_HOST}:ordinary", "tell_id": "ordinary-tell", "text": "routine remains direct",
                "_auth_context": {"token_verified": True, "stream_id": "fixture-host-worker:worker"},
            })
            assert ordinary["type"] == "tell.ok" and len(comms.told) == 2
            decision = await composite.suppress_routine_backend_ingress(
                target_stream_id=AUTHORITY_STREAM, body="[assistant composite authority decision]\nlane_id=fixture",
                msg={"tell_id": "decision-notice"}, verb="notice",
            )
            assert decision is None
        finally:
            store.stop()

    asyncio.run(_go())


def test_hidden_lead_question_reuses_existing_store_and_reply_follows_current_lane(tmp_path) -> None:
    """A handoff never auto-sends consent to the historical hidden producer."""

    def _actions(question_id: str) -> list[dict]:
        return [{
            "kind": "yes_no", "action_id": "yes", "label": "Yes", "choice": True,
            "value": {"schema_version": 1, "question_id": question_id, "answer": "yes"},
        }]

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        notify = None
        try:
            sessions = Sessions(store, tmux=None, local_host=COMPOSITE_HOST)
            original = await store.open_session(
                "fixture-host-lead", "lead", provider="codex", visibility="hidden",
            )
            authority = await store.open_session("fixture-host-authority", "authority", provider="codex", visibility="hidden")
            await sessions.refresh()
            comms = _Comms()
            server = Server(store=store, sessions=sessions, comms=comms, local_host=COMPOSITE_HOST)
            notify = Notify(str(tmp_path / "notifications.db"), sessions=sessions)
            server.notify = notify
            await notify.start()
            dispatched: list[dict] = []

            async def dispatch(route):
                dispatched.append(dict(route))
                return {"delivery": "landed"}

            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM, luna_stream_id=CONVERSATION_STREAM,
                ),
                dispatch=dispatch,
                question_operation=server._assistant_question_operation,
                question_answer=server._assistant_question_answer,
            )
            server.assistant_composite = composite
            await composite.ensure_projection()
            await _seed_dispatch(
                store, input_identity="admit-input", dispatch_id="dispatch-admit", target=AUTHORITY_STREAM,
                generation=authority["session_generation"],
            )
            admitted = await composite.operation({
                "operation": "lane.admit", "request_id": "admit", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-admit", "lane_id": None, "expected_lane_version": None,
                "evidence_refs": [],
                "payload": {"mode": "new", "subject": "handoff", "request_message_id": "admit-input"},
            }, actor_stream_id=AUTHORITY_STREAM)
            lane_id = admitted["lane_id"]
            await _seed_dispatch(
                store, input_identity="bind-old-input", dispatch_id="dispatch-bind-old", target=AUTHORITY_STREAM,
                generation=authority["session_generation"], lane_id=lane_id,
            )
            await composite.operation({
                "operation": "lane.bind", "request_id": "bind-old", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-bind-old", "lane_id": lane_id, "expected_lane_version": 1,
                "evidence_refs": [],
                "payload": {
                    "backend_kind": "assistant_conversation",
                    "backend_stream_id": LEAD_STREAM, "backend_generation": original["session_generation"],
                },
            }, actor_stream_id=AUTHORITY_STREAM)
            await _seed_dispatch(
                store, input_identity="question-open-input", dispatch_id="dispatch-question-open", target=LEAD_STREAM,
                generation=original["session_generation"], lane_id=lane_id,
            )
            opened = await composite.operation({
                "operation": "question.open", "request_id": "open-question", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-question-open", "lane_id": lane_id, "expected_lane_version": 2,
                "evidence_refs": [],
                "payload": {
                    "envelope": {
                        "schema_version": 1, "question_id": "q-current-lane", "title": "Continue?",
                        "body": "Please confirm.", "dedup_key": "question-current-lane",
                        "producer_stream_id": "forged:producer", "response_mode": "single_choice",
                        "options": [{"label": "Yes", "value": "yes"}],
                    },
                    "actions": _actions("q-current-lane"),
                },
                "_auth_context": {"token_verified": True, "stream_id": LEAD_STREAM},
            }, actor_stream_id=LEAD_STREAM)
            assert opened["question"]["question_id"] == "q-current-lane"
            persisted = await notify._db.call("get_agent_question", "q-current-lane")
            assert persisted["producer_stream_id"] == LEAD_STREAM
            assert persisted["producer_session_generation"] == original["session_generation"]
            bypass = await notify.notification({
                "type": "notification.resolve", "request_id": "wrong-answer-path",
                "notification_id": persisted["notification_id"], "selections": ["yes"],
                "_auth_context": {"operator_authenticated": True},
            })
            assert bypass["error_code"] == "assistant_question_reply_requires_chat"
            assert (await notify._db.call("get_agent_question", "q-current-lane"))["state"] == "open"

            # A new generation now owns the lane. The original issuer is gone,
            # but the special existing-store row remains answerable only through
            # a current-lane composite send.
            await store.mark_closed(
                "fixture-host-lead", "lead", closed_at="2026-09-19T00:00:00Z", pane_status="closed",
                expected_generation=original["session_generation"],
            )
            current = await store.open_session("fixture-host-lead", "lead", provider="codex", visibility="hidden")
            await sessions.refresh()
            lane = await store.get_assistant_composite_lane(stream_id=COMPOSITE_STREAM, lane_id=lane_id)
            await _seed_dispatch(
                store, input_identity="bind-new-input", dispatch_id="dispatch-bind-new", target=AUTHORITY_STREAM,
                generation=authority["session_generation"], lane_id=lane_id,
            )
            await composite.operation({
                "operation": "lane.bind", "request_id": "bind-new", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-bind-new", "lane_id": lane_id, "expected_lane_version": lane["version"],
                "evidence_refs": [],
                "payload": {
                    "backend_kind": "assistant_conversation",
                    "backend_stream_id": LEAD_STREAM, "backend_generation": current["session_generation"],
                },
            }, actor_stream_id=AUTHORITY_STREAM)
            accepted = await server._on_send({
                "host": COMPOSITE_HOST, "session_name": "assistant", "text": "yes, continue",
                "request_id": "rpc-question", "optimistic_id": "answer-question",
                "reply_to_question_id": "q-current-lane",
                "_auth_context": {"operator_authenticated": True},
            })
            assert accepted["assistant_composite"]["routing_state"] == "resolved"
            for _ in range(30):
                if dispatched:
                    break
                await asyncio.sleep(0.01)
            assert len(dispatched) == 1
            assert dispatched[0]["route_target"] == LEAD_STREAM
            assert dispatched[0]["route_target_generation"] == current["session_generation"]
            assert (await notify._db.call("get_agent_question", "q-current-lane"))["state"] == "answered"
            lane = await store.get_assistant_composite_lane(stream_id=COMPOSITE_STREAM, lane_id=lane_id)
            assert lane["pending_question_id"] is None

            # The same current binding can cancel a later question despite its
            # historical issuer generation; no second question store exists.
            await _seed_dispatch(
                store, input_identity="cancel-open-input", dispatch_id="dispatch-cancel-open", target=LEAD_STREAM,
                generation=current["session_generation"], lane_id=lane_id,
            )
            await composite.operation({
                "operation": "question.open", "request_id": "open-cancel", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-cancel-open", "lane_id": lane_id,
                "expected_lane_version": lane["version"], "evidence_refs": [],
                "payload": {
                    "envelope": {
                        "schema_version": 1, "question_id": "q-cancel-current", "title": "Stop?",
                        "body": "Please decide.", "dedup_key": "question-cancel-current",
                        "producer_stream_id": "forged:producer", "response_mode": "single_choice",
                        "options": [{"label": "Yes", "value": "yes"}],
                    },
                    "actions": _actions("q-cancel-current"),
                },
                "_auth_context": {"token_verified": True, "stream_id": LEAD_STREAM},
            }, actor_stream_id=LEAD_STREAM)
            pending = await store.get_assistant_composite_lane(stream_id=COMPOSITE_STREAM, lane_id=lane_id)
            await _seed_dispatch(
                store, input_identity="cancel-input", dispatch_id="dispatch-cancel", target=LEAD_STREAM,
                generation=current["session_generation"], lane_id=lane_id,
            )
            cancelled = await composite.operation({
                "operation": "question.cancel", "request_id": "cancel-current", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-cancel", "lane_id": lane_id,
                "expected_lane_version": pending["version"], "evidence_refs": [],
                "payload": {"question_id": "q-cancel-current"},
                "_auth_context": {"token_verified": True, "stream_id": LEAD_STREAM},
            }, actor_stream_id=LEAD_STREAM)
            assert cancelled["question"]["state"] == "dismissed"
            assert (await store.get_assistant_composite_lane(
                stream_id=COMPOSITE_STREAM, lane_id=lane_id,
            ))["pending_question_id"] is None
        finally:
            if notify is not None:
                await notify.stop()
            store.stop()

    asyncio.run(_go())


def test_broadcast_keeps_composite_capability_groups_separate() -> None:
    """A mixed live fleet must preserve each socket's snapshot visibility."""
    class Socket:
        remote_address = ("127.0.0.1", 12345)

    async def _go(events_mode: str, capable_first: bool) -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host=COMPOSITE_HOST)
            composite = AssistantComposite(store, config=AssistantCompositeConfig(
                enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
            ))
            await composite.ensure_projection()
            await store.open_session(COMPOSITE_HOST, "ordinary", provider="codex")
            await sessions.refresh()
            server = Server(store=store, sessions=sessions, comms=_Comms(), local_host=COMPOSITE_HOST)
            server.assistant_composite = composite
            capable, plain = Socket(), Socket()
            # Pin the first representative in both orders, instead of relying on set order.
            server._clients = [capable, plain] if capable_first else [plain, capable]
            received = {capable: [], plain: []}
            server._enqueue = lambda socket, _kind, encoded: received[socket].append(json.loads(encoded))
            for socket in (capable, plain):
                frames = await server._dispatch(json.dumps({
                    "type": "hello", "subscribe": {"include_subagents": True, "events_mode": events_mode},
                    "capabilities": {"assistant_composite_v1": socket is capable},
                }), websocket=socket)
                snapshot = next(frame for frame in frames if frame["type"] == "snapshot")
                assert any(row["stream_id"] == COMPOSITE_STREAM for row in snapshot["sessions"]) == (socket is capable)
            await server.broadcast({"type": "session.inventory", "sessions": sessions.list_open()})
            for socket in (capable, plain):
                rows = received[socket][-1]["sessions"]
                assert any(row["stream_id"] == COMPOSITE_STREAM for row in rows) == (socket is capable)
                assert any(row["stream_id"] == f"{COMPOSITE_HOST}:ordinary" for row in rows)
            for stream_id in (COMPOSITE_STREAM, f"{COMPOSITE_HOST}:ordinary"):
                for messages in received.values():
                    messages.clear()
                await server.broadcast({"type": "chat.event", "event": {
                    "stream_id": stream_id, "kind": "ASSIST_TEXT", "text": "fixture reply",
                }})
                assert len(received[capable]) == 1
                assert len(received[plain]) == (0 if stream_id == COMPOSITE_STREAM else 1)
        finally:
            store.stop()

    for events_mode in ("summary", "full"):
        for capable_first in (False, True):
            asyncio.run(_go(events_mode, capable_first))
