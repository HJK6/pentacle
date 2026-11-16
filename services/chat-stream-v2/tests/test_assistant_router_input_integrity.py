"""Router must classify the current input while retaining independent lane context."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from assistant_router import AssistantRouterAdapter
from store import Store


@pytest.mark.parametrize("body", ["Continue it.", "A new unrelated question " * 220], ids=["short", "truncated"])
def test_lane_publications_do_not_replace_current_input(body):
    events = [
        {"kind": "ASSIST_TEXT", "text": text, "daemon_seq": seq,
         "publish_kind": "prose", "raw": {"dispatch_id": dispatch}}
        for seq, text, dispatch in [
            (1, "Older lane A reply", "a"),
            (3, "Latest lane A reply " * 40, "a"),
            (2, "Lane B answer", "b"),
        ]
    ]
    store = SimpleNamespace(
        list_assistant_composite_unresolved=AsyncMock(return_value=[]),
        fetch_session_event_tail=AsyncMock(return_value=events),
        list_assistant_composite_open_lanes=AsyncMock(return_value=[
            {"lane_id": "a", "phase": "execution", "summary": "First topic"},
            {"lane_id": "b", "phase": "discussion", "summary": "Second topic"},
        ]),
        find_assistant_composite_route_by_dispatch=AsyncMock(
            side_effect=lambda dispatch: {"route_json": json.dumps({"lane_id": dispatch})}),
    )
    composite = AssistantComposite(store, config=AssistantCompositeConfig(
        enabled=True, stream_id="fixture:assistant"))
    payload = asyncio.run(composite._router_input({
        "body": body, "input_identity": "new-input", "route_id": "new-route"}))
    assert payload["body_excerpt"] == body[:4000]
    assert payload["body_truncated"] is (len(body) > 4000)
    assert payload["original_length"] == len(body)
    lanes = {lane["lane_id"]: lane for lane in payload["open_lanes"]}
    assert lanes["a"]["last_outbound_excerpt"] == ("Latest lane A reply " * 40)[:384]
    assert lanes["a"]["last_outbound_truncated"] is True
    assert lanes["b"]["last_outbound_excerpt"] == "Lane B answer"
    assert lanes["b"]["last_outbound_truncated"] is False


@pytest.mark.parametrize("rc,stderr,expected", [
    (2, b'{"error":"assistant_router_failed","detail":"ValueError"}', "assistant_router_script_failed"),
    (2, b'{"error":"assistant_router_failed","detail":"TimeoutError"}', "assistant_router_script_failed"),
    (255, b"ssh connection refused", "assistant_router_transport_failed"),
    (2, b"unexpected shell failure", "assistant_router_transport_failed"),
    (255, ("error \u2603" * 1000).encode() + b"\xff", "assistant_router_transport_failed"),
], ids=["script-validation", "script-inference", "ssh", "other-rc2", "utf8-bounds"])
def test_process_failure_diagnostics_survive_real_fallback(monkeypatch, rc, stderr, expected):
    stdout = ("output \u2603" * 1000).encode() + b"\xff"
    class FailedProcess:
        returncode = rc
        async def communicate(self, payload):
            assert json.loads(payload)["body_excerpt"] == "Hello"
            return stdout, stderr
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=FailedProcess()))
    async def run():
        store = Store(":memory:")
        store.start()
        composite = None
        try:
            await store.open_session("fixture", "conversation", provider="codex")
            adapter = AssistantRouterAdapter("ssh://fixture/assistant-router-v1", timeout_s=5,
                                             action_path="/opt/router.py")
            composite = AssistantComposite(store, config=AssistantCompositeConfig(
                enabled=True, stream_id="fixture:assistant", luna_stream_id="fixture:conversation"),
                router=adapter, dispatch=AsyncMock(return_value={"delivery": "landed"}))
            composite._wake_worker = lambda: None
            await composite.ensure_projection()
            await composite.accept_input({"text": "Hello", "optimistic_id": "diagnostics"})
            route = await store.get_assistant_composite_route(
                stream_id="fixture:assistant", input_identity="diagnostics")
            await composite._classify_one(route)
            if composite._dispatch_tasks:
                await asyncio.gather(*tuple(composite._dispatch_tasks))
            saved = await store.get_assistant_composite_route(
                stream_id="fixture:assistant", input_identity="diagnostics")
            payload = json.loads(saved["route_json"])
            failure = payload["router_failure"]
            assert failure["message"] == expected
            assert failure["returncode"] == rc
            assert failure["stdout"].startswith("output")
            assert failure["stderr"]
            assert len(failure["stdout"].encode()) <= 2048
            assert len(failure["stderr"].encode()) <= 2048
            assert payload["fallback_receipt"]["dispatch_id"] == saved["dispatch_id"]
            assert saved["delivery_state"] == "landed"
            await composite.operation({
                "operation": "route.resolve", "request_id": "resolve-diagnostics",
                "composite_stream_id": "fixture:assistant", "dispatch_id": saved["dispatch_id"],
                "reply_to_message_id": "diagnostics", "lane_id": None,
                "expected_lane_version": None, "evidence_refs": [],
                "payload": {"schema_version": "assistant-router/v1", "disposition": "conversation",
                            "lane_id": None, "depends_on_message_id": None, "reason": "Conversation"},
            }, actor_stream_id="fixture:conversation")
            if composite._dispatch_tasks:
                await asyncio.gather(*tuple(composite._dispatch_tasks))
            resolved = await store.get_assistant_composite_route(
                stream_id="fixture:assistant", input_identity="diagnostics")
            assert json.loads(resolved["route_json"])["router_failure"] == failure
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()
    asyncio.run(run())
