"""Desk sends are labelled `[from <desk>]` by credential id; operator input is untouched."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

import desk_stamp
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from claude_jsonl_norm import _PEER_TELL_RE
from server import Server
from sessions import Sessions
from store import Store

HOST = "fixture-host-chat"
COMPOSITE = f"{HOST}:assistant"
DESK_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())


class _Comms:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(dict(msg))
        return {"type": "send.result", "delivery": "landed"}

    async def tell(self, msg):  # pragma: no cover - sends only here
        return {"type": "tell.ok"}


def _auth(credential_id: str) -> dict:
    return {"operator_authenticated": True, "operator_principal": f"operator:{credential_id}"}


def _config(tmp_path, monkeypatch, mapping, mode=0o600):
    path = tmp_path / "desks.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    os.chmod(path, mode)
    monkeypatch.setenv(desk_stamp.CONFIG_ENV, str(path))
    return path


def _run(coro):
    return asyncio.run(coro)


def _harness():
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=None, local_host=HOST)
    comms = _Comms()
    composite = AssistantComposite(store, config=AssistantCompositeConfig(
        enabled=True, stream_id=COMPOSITE, router_endpoint="ssh://fixture-router/assistant-router-v1"))
    server = Server(store=store, sessions=sessions, comms=comms, local_host=HOST)
    server.assistant_composite = composite
    return store, sessions, comms, composite, server


async def _setup(store, sessions, composite):
    await composite.ensure_projection()
    await store.open_session(HOST, "v2-direct", provider="codex")
    await sessions.refresh()


def test_mapped_desk_credential_is_stamped_for_plain_seat_and_composite(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {DESK_ID: "sms-desk"})

    async def go():
        store, sessions, comms, composite, server = _harness()
        try:
            await _setup(store, sessions, composite)
            await server._on_send({"host": HOST, "session_name": "v2-direct", "text": "hello\x1b[31m there",
                                   "request_id": "r1", "optimistic_id": "o1", "_auth_context": _auth(DESK_ID)})
            seen = []
            real = composite.accept_input

            async def capture(msg, **kwargs):
                seen.append(msg["text"])
                return await real(msg, **kwargs)

            composite.accept_input = capture
            await server._on_send({"host": HOST, "session_name": "assistant", "text": "to composite",
                                   "request_id": "r2", "optimistic_id": "o2", "_auth_context": _auth(DESK_ID)})
            return comms.sent[0]["text"], seen
        finally:
            store.stop()

    pane_text, composite_text = _run(go())
    assert pane_text.startswith("[from sms-desk]\n") and "\x1b" not in pane_text
    assert composite_text == ["[from sms-desk]\nto composite"]
    assert _PEER_TELL_RE.search(pane_text) is None


def test_stamp_runs_before_the_assistant_alias_return(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {DESK_ID: "bot-desk"})

    async def go():
        store, sessions, comms, composite, server = _harness()
        try:
            captured = []

            async def alias_send(msg):
                captured.append(msg["text"])
                return {"type": "send.result"}

            server._alias_send = alias_send
            from server import ASSISTANT_ALIAS
            await server._on_send({"to_stream_id": ASSISTANT_ALIAS, "text": "via alias", "request_id": "r3",
                                   "_auth_context": _auth(DESK_ID)})
            return captured
        finally:
            store.stop()

    assert _run(go()) == ["[from bot-desk]\nvia alias"]


def test_unmapped_operator_and_other_principals_are_never_stamped(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {DESK_ID: "sms-desk"})
    for auth in (_auth(OTHER_ID), {"operator_authenticated": True, "operator_principal": "operator:local-admin"},
                 {"token_verified": True, "stream_id": "x:y"},
                 {"operator_authenticated": False, "operator_principal": f"operator:{DESK_ID}"}, {}):
        msg = {"text": "plain", "_auth_context": auth}
        assert desk_stamp.stamp_message(msg) is msg


def test_message_field_is_stamped_and_empty_body_is_not(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {DESK_ID: "dot-mail-desk"})
    stamped = desk_stamp.stamp_message({"message": "only message", "_auth_context": _auth(DESK_ID)})
    assert stamped["message"] == "[from dot-mail-desk]\nonly message" and "text" not in stamped
    both = desk_stamp.stamp_message({"text": "a", "message": "b", "_auth_context": _auth(DESK_ID)})
    assert both["text"].startswith("[from dot-mail-desk]\n") and both["message"].startswith("[from dot-mail-desk]\n")
    empty = {"text": "", "_auth_context": _auth(DESK_ID)}
    assert desk_stamp.stamp_message(empty)["text"] == ""


def test_config_validation_and_revert_on_removal(tmp_path, monkeypatch) -> None:
    path = _config(tmp_path, monkeypatch, {DESK_ID: "sms-desk"})
    msg = {"text": "t", "_auth_context": _auth(DESK_ID)}
    assert desk_stamp.stamp_message(msg)["text"].startswith("[from sms-desk]")
    path.write_text(json.dumps({DESK_ID: "Not A Label"}), encoding="utf-8")
    assert desk_stamp.load() == {}
    path.write_text(json.dumps({"not-a-uuid": "sms-desk"}), encoding="utf-8")
    assert desk_stamp.load() == {}
    path.write_text(json.dumps({str(uuid.uuid4()): "a-desk" for _ in range(9)}), encoding="utf-8")
    assert desk_stamp.load() == {}
    os.chmod(path, 0o644)
    path.write_text(json.dumps({DESK_ID: "sms-desk"}), encoding="utf-8")
    assert desk_stamp.load() == {}, "a group/world-readable file is ignored"
    os.chmod(path, 0o600)
    assert desk_stamp.stamp_message(msg)["text"].startswith("[from sms-desk]")
    path.unlink()
    assert desk_stamp.stamp_message(msg) is msg, "removing the file stops stamping on the next send"
    monkeypatch.delenv(desk_stamp.CONFIG_ENV)
    assert desk_stamp.load() == {}


def test_same_request_id_twice_under_unchanged_config_is_one_receipt_text(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {DESK_ID: "sms-desk"})
    msg = {"text": "retry me", "request_id": "same", "_auth_context": _auth(DESK_ID)}
    assert desk_stamp.stamp_message(msg)["text"] == desk_stamp.stamp_message(dict(msg))["text"]
