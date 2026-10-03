"""Cosmo push (D): separate CosmoPushTokens audience + one push per committed
Daff reply, with revocation recheck, DeviceNotRegistered cleanup, and no leakage
either way.  Uses a stub Expo transport (no real notification)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import uiverbs
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from cosmo_push import CosmoPush
from server import Server
from sessions import Sessions
from store import Store


DAFF_CHAT = "daff:assistant"
BART_CHAT = "bart:assistant"


class _FakeTable:
    def __init__(self, items):
        self.items = list(items)
        self.deleted = []

    def scan(self):
        return {"Items": list(self.items)}

    def delete_item(self, Key):
        self.deleted.append(Key["push_token"])
        self.items = [i for i in self.items if i.get("push_token") != Key["push_token"]]

    def put_item(self, Item):
        self.items.append(dict(Item))


def _ok_transport(sent):
    def _t(url, data):
        sent.append(data)
        return {"data": {"status": "ok", "id": "ticket-1"}}
    return _t


# --- CosmoPush.push_reply unit ------------------------------------------------

def test_push_only_matching_active_nonrevoked_tokens():
    sent = []
    table = _FakeTable([
        {"push_token": "t-daff", "scope_stream": DAFF_CHAT, "credential_id": "c1", "active": True},
        {"push_token": "t-wrong-stream", "scope_stream": BART_CHAT, "credential_id": "c2", "active": True},
        {"push_token": "t-inactive", "scope_stream": DAFF_CHAT, "credential_id": "c3", "active": False},
        {"push_token": "t-revoked", "scope_stream": DAFF_CHAT, "credential_id": "revoked", "active": True},
    ])
    cp = CosmoPush(table_factory=lambda: table, transport=_ok_transport(sent),
                   revoked=lambda cid: cid == "revoked")
    count = asyncio.run(cp.push_reply(stream_id=DAFF_CHAT, message_id="m1", text="hello"))
    assert count == 1
    assert [d["to"] for d in sent] == ["t-daff"]
    assert sent[0]["title"] == "Daff"
    assert sent[0]["body"] == "hello"


def test_push_truncates_body_to_120_chars():
    sent = []
    table = _FakeTable([{"push_token": "t", "scope_stream": DAFF_CHAT, "credential_id": "c", "active": True}])
    cp = CosmoPush(table_factory=lambda: table, transport=_ok_transport(sent), revoked=lambda c: False)
    asyncio.run(cp.push_reply(stream_id=DAFF_CHAT, message_id="m", text="x" * 500))
    assert len(sent[0]["body"]) == 120


def test_device_not_registered_token_deleted():
    def _t(url, data):
        return {"data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}}
    table = _FakeTable([{"push_token": "dead", "scope_stream": DAFF_CHAT, "credential_id": "c", "active": True}])
    cp = CosmoPush(table_factory=lambda: table, transport=_t, revoked=lambda c: False)
    count = asyncio.run(cp.push_reply(stream_id=DAFF_CHAT, message_id="m", text="hi"))
    assert count == 0
    assert table.deleted == ["dead"]


# --- register_push routing ----------------------------------------------------

def _boto3_stub(recorder):
    class _Table:
        def __init__(self, name):
            self.name = name
        def put_item(self, Item):
            recorder.append((self.name, Item))
    return SimpleNamespace(resource=lambda *a, **k: SimpleNamespace(Table=lambda n: _Table(n)))


def test_register_push_routes_scoped_to_cosmo_table(monkeypatch):
    recorder = []
    monkeypatch.setattr(uiverbs, "boto3", _boto3_stub(recorder))
    ui = uiverbs.UIVerbs(None, None, None)
    reply = asyncio.run(ui.register_push({
        "request_id": "r1", "push_token": "ExponentPushToken[abc]",
        "_auth_context": {"scoped_principal": True, "credential_id": "cred-1", "scope_stream": DAFF_CHAT},
    }))
    assert reply["type"] == "register_push.ok"
    assert len(recorder) == 1
    table_name, item = recorder[0]
    assert table_name == uiverbs.COSMO_PUSH_TOKENS_TABLE
    assert item["credential_id"] == "cred-1"
    assert item["scope_stream"] == DAFF_CHAT


def test_register_push_operator_stays_on_legacy_table(monkeypatch):
    recorder = []
    monkeypatch.setattr(uiverbs, "boto3", _boto3_stub(recorder))
    ui = uiverbs.UIVerbs(None, None, None)
    reply = asyncio.run(ui.register_push({
        "request_id": "r1", "push_token": "ExponentPushToken[abc]",
        "_auth_context": {"operator_authenticated": True},
    }))
    assert reply["type"] == "register_push.ok"
    assert recorder[0][0] == uiverbs.PUSH_TOKENS_TABLE
    assert "credential_id" not in recorder[0][1]


# --- publish fires exactly one reply push -------------------------------------

def test_committed_daff_reply_pushes_once_and_bart_never(monkeypatch):
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex")
            gen = root["session_generation"]
            sent = []

            async def dispatch(route):
                sent.append(route)
                return {"delivery": "landed"}

            composite = AssistantComposite(store, config=AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_DAFF_COMPOSITE_STREAM_ID": DAFF_CHAT,
                "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_STREAM_ID": "fixture-root:visible",
                "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_GENERATION": gen,
            }, name="daff", env_prefix="DAFF_"), dispatch=dispatch)
            pushes = []

            async def _recorder(*, stream_id, message_id, text):
                pushes.append({"stream_id": stream_id, "message_id": message_id, "text": text})
            composite.reply_push = _recorder
            await composite.ensure_projection()
            sessions = Sessions(store, tmux=None, local_host="fixture-chat")
            await sessions.refresh()
            server = Server(store=store, sessions=sessions, local_host="fixture-chat")
            server.assistant_composite = composite
            server.assistant_composites = {"daff": composite}

            await server._on_send({
                "to_stream_id": DAFF_CHAT, "text": "q",
                "msg_id": "logical-1", "request_id": "t1",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            for _ in range(100):
                route = await store.get_assistant_composite_route(stream_id=DAFF_CHAT, input_identity="logical-1")
                if route and route["routing_state"] == "resolved" and sent:
                    break
                await asyncio.sleep(0.01)
            published = {
                "request_id": "publish:" + route["dispatch_id"], "composite_stream_id": DAFF_CHAT,
                "dispatch_id": route["dispatch_id"], "reply_to_message_id": "logical-1",
                "reply_to_question_id": None, "publish_kind": "prose", "response_state": "final",
                "message": "the reply", "attachment_ids": [], "evidence_refs": [],
            }
            r1 = await composite.publish(published, actor_stream_id="fixture-root:visible")
            assert r1["duplicate"] is False
            # Exactly one push for the committed reply.
            assert len(pushes) == 1
            assert pushes[0]["stream_id"] == DAFF_CHAT
            assert pushes[0]["text"] == "the reply"
            # A replay (duplicate publication) does not re-push.
            r2 = await composite.publish(published, actor_stream_id="fixture-root:visible")
            assert r2["duplicate"] is True
            assert len(pushes) == 1
        finally:
            store.stop()

    asyncio.run(run())
