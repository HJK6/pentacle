"""Milestone tells reuse the sender's durable status card."""

import asyncio

import pytest

from comms import Comms
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from test_tell_ledger_row_id import EchoTmux
from test_held_tell_identity import _front_desk, _tell
from test_nudges import HOST


OLD_CARD = {"goal": "Ship the lane", "update": "Before", "updated_at": "2020-01-01T00:00:00Z"}


@pytest.mark.parametrize("role", ["lead", "nexus"])
@pytest.mark.parametrize("body", [
    "START work", "GATE ready", "BLOCKER decision", "END complete",
    "GATE " + "x" * 400,
    "[from fixture:lead] [tell:original]\nGATE wrapped",
    "[from fixture:lead]\nGATE bare header",
])
def test_delivered_milestone_stamps_sender_and_replay_does_not(role, body):
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = EchoTmux()
            sessions = Sessions(store, tmux=tmux, local_host="fixture")
            comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
            await sessions.open("fixture", "lead", role=role, status_card=OLD_CARD)
            await sessions.open("fixture", "parent")
            msg = {"stream_id": "fixture:parent", "from_stream_id": "fixture:lead",
                   "message": body, "tell_id": "milestone"}
            reply = await comms.tell(msg)
            assert reply["delivery_status"] == "delivered"
            card = (await store.fetch_session("fixture", "lead"))["status_card"]
            text = body.split("\n", 1)[-1]
            assert card["update"] == text[:300]
            assert card["updated_at"] > OLD_CARD["updated_at"]
            assert card["goal"] == OLD_CARD["goal"]
            assert (await store.fetch_session("fixture", "parent"))["status_card"] is None
            assert (await comms.tell(msg))["duplicate"] is True
            assert (await store.fetch_session("fixture", "lead"))["status_card"] == card
        finally:
            store.stop()
    asyncio.run(go())


@pytest.mark.parametrize("role,body,status", [
    ("lead", "FYI progress", "open"), ("lead", "GATEWAY unrelated", "open"),
    ("worker", "GATE ready", "open"), ("qa", "END done", "open"),
    ("lead", "GATE ready", "closed"),
])
def test_ineligible_tells_leave_card_untouched(role, body, status):
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = EchoTmux()
            sessions = Sessions(store, tmux=tmux, local_host="fixture")
            comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
            await sessions.open("fixture", "lead", role=role, status_card=OLD_CARD)
            await sessions.open("fixture", "parent")
            if status == "closed":
                await store.mark_closed("fixture", "lead", closed_at="2026-10-01T00:00:00Z", pane_status="pane_dead")
                await sessions.refresh()
            await comms.tell({"stream_id": "fixture:parent", "from_stream_id": "fixture:lead",
                              "message": body, "tell_id": "excluded"})
            assert (await store.fetch_session("fixture", "lead"))["status_card"] == OLD_CARD
        finally:
            store.stop()
    asyncio.run(go())


def test_front_desk_held_milestone_stamps_card_after_persistence(tmp_path):
    async def go():
        store, h, composite, target = await _front_desk(tmp_path)
        try:
            await h.open("peer", role="lead", status_card=OLD_CARD)
            reply = await h.server._on_tell(_tell(target, "held-milestone", "START held work"))
            assert reply["delivery_status"] == "persisted"
            assert len(await composite.front_desk_digest._rows(target)) == 1
            assert not h.tmux.pasted
            card = (await store.fetch_session(HOST, "peer"))["status_card"]
            assert card["update"] == "START held work"
            assert card["updated_at"] > OLD_CARD["updated_at"]
        finally:
            store.stop()
    asyncio.run(go())


def test_status_failure_is_logged_without_failing_tell(monkeypatch, caplog):
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = EchoTmux()
            sessions = Sessions(store, tmux=tmux, local_host="fixture")
            comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
            await sessions.open("fixture", "lead", role="lead")
            await sessions.open("fixture", "parent")
            async def broken(*args, **kwargs):
                raise RuntimeError("card write failed")
            monkeypatch.setattr(sessions, "set_status_card", broken)
            reply = await comms.tell({"stream_id": "fixture:parent", "from_stream_id": "fixture:lead",
                                      "message": "GATE ready", "tell_id": "card-fault"})
            assert reply["delivery_status"] == "delivered"
            assert await store.get_tell_delivery("card-fault") is not None
            assert "card write failed" in caplog.text
        finally:
            store.stop()
    asyncio.run(go())
