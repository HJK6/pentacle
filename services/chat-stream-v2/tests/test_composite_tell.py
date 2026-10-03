"""Composite tell routing (B): tell <name>:assistant reaches the bound pane,
is authorized to operator/agent only, generation-checked, and queued/flushed
across a rebind."""
from __future__ import annotations

import asyncio

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions, VerbError
from store import Store


LOCAL = "fixture-host"
BART_CHAT = "bart:assistant"
DAFF_CHAT = "daff:assistant"
BART_SEAT = "fixture-barts:visible"
DAFF_SEAT = "fixture-daffs:visible"


class _Comms:
    def __init__(self):
        self.told = []

    async def tell(self, msg):
        self.told.append(dict(msg))
        return {"type": "tell.ok", "delivery_status": "delivered", "submission_confirmed": True}


async def _seat(store, stream_id):
    host, name = stream_id.split(":", 1)
    return await store.open_session(
        host, name, provider="codex", role="assistant", visibility="default",
        pane_status="pane_alive", effective_model="gpt-6-sol", effective_effort="high",
    )


def _config(name, env_prefix, stream_id, seat_stream, generation):
    return AssistantCompositeConfig.from_env({
        f"PENTACLE_ASSISTANT_{env_prefix}COMPOSITE_ENABLED": "1",
        f"PENTACLE_ASSISTANT_{env_prefix}COMPOSITE_STREAM_ID": stream_id,
        f"PENTACLE_ASSISTANT_{env_prefix}DIRECT_PRIMARY_STREAM_ID": seat_stream,
        f"PENTACLE_ASSISTANT_{env_prefix}DIRECT_PRIMARY_GENERATION": generation,
    }, name=name, env_prefix=env_prefix)


async def _server_with(store, composites):
    sessions = Sessions(store, tmux=None, local_host=LOCAL)
    comms = _Comms()
    for c in composites.values():
        await c.load_binding()
        await c.ensure_projection()
    await sessions.refresh()
    server = Server(store=store, sessions=sessions, comms=comms, local_host=LOCAL)
    server.assistant_composites = composites
    server.assistant_composite = composites.get("bart")
    return server, comms, sessions


def test_composite_tell_delivers_both_directions_to_bound_pane():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            bart_seat = await _seat(store, BART_SEAT)
            daff_seat = await _seat(store, DAFF_SEAT)
            bart = AssistantComposite(store, config=_config(
                "bart", "", BART_CHAT, BART_SEAT, bart_seat["session_generation"]))
            daff = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, daff_seat["session_generation"]))
            server, comms, sessions = await _server_with(store, {"bart": bart, "daff": daff})
            await sessions.refresh()

            # operator -> daff:assistant
            reply = await server._on_tell({
                "to_stream_id": DAFF_CHAT, "text": "hi daff",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            assert reply["delivery_status"] == "delivered"
            assert comms.told[-1]["to_stream_id"] == DAFF_SEAT
            assert comms.told[-1]["message"] == "hi daff"

            # daff -> bart:assistant (agent stream token)
            await server._on_tell({
                "to_stream_id": BART_CHAT, "text": "hi bart",
                "from_stream_id": DAFF_CHAT,
                "_auth_context": {"token_verified": True, "stream_id": DAFF_SEAT},
            })
            assert comms.told[-1]["to_stream_id"] == BART_SEAT
            assert comms.told[-1]["from_stream_id"] == DAFF_CHAT

            # bart -> daff:assistant
            await server._on_tell({
                "to_stream_id": DAFF_CHAT, "text": "ack", "from_stream_id": BART_CHAT,
                "_auth_context": {"token_verified": True, "stream_id": BART_SEAT},
            })
            assert comms.told[-1]["to_stream_id"] == DAFF_SEAT
        finally:
            store.stop()

    asyncio.run(run())


def test_scoped_and_dot_clients_may_not_tell_composite():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            seat = await _seat(store, BART_SEAT)
            bart = AssistantComposite(store, config=_config(
                "bart", "", BART_CHAT, BART_SEAT, seat["session_generation"]))
            server, comms, _ = await _server_with(store, {"bart": bart})
            for auth in ({"dot_principal": True}, {"scoped_principal": True, "token_verified": True}):
                try:
                    await server._on_tell({"to_stream_id": BART_CHAT, "text": "x", "_auth_context": auth})
                except VerbError as exc:
                    assert exc.code == "dot_scope_denied"
                else:
                    raise AssertionError("scoped/dot tell to composite must be refused")
            assert not comms.told
        finally:
            store.stop()

    asyncio.run(run())


def test_unauthenticated_composite_tell_refused():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            seat = await _seat(store, BART_SEAT)
            bart = AssistantComposite(store, config=_config(
                "bart", "", BART_CHAT, BART_SEAT, seat["session_generation"]))
            server, comms, _ = await _server_with(store, {"bart": bart})
            try:
                await server._on_tell({"to_stream_id": BART_CHAT, "text": "x", "_auth_context": {}})
            except VerbError as exc:
                assert exc.code == "assistant_tell_unauthorized"
            else:
                raise AssertionError("unauthenticated composite tell must be refused")
            assert not comms.told
        finally:
            store.stop()

    asyncio.run(run())


def test_stale_generation_refused():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            seat = await _seat(store, BART_SEAT)
            # Bind to a generation that does not match the live seat.
            bart = AssistantComposite(store, config=_config(
                "bart", "", BART_CHAT, BART_SEAT, "stalegen0000"))
            server, comms, _ = await _server_with(store, {"bart": bart})
            try:
                await server._on_tell({
                    "to_stream_id": BART_CHAT, "text": "x",
                    "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
                })
            except VerbError as exc:
                assert exc.code == "assistant_direct_generation_conflict"
            else:
                raise AssertionError("a stale binding generation must be refused")
            assert not comms.told
        finally:
            store.stop()

    asyncio.run(run())


def test_tell_queued_while_unbound_then_flushed_in_order():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            # daff bound to a seat that is not open -> unbound for delivery.
            bart_seat = await _seat(store, BART_SEAT)
            bart = AssistantComposite(store, config=_config(
                "bart", "", BART_CHAT, BART_SEAT, bart_seat["session_generation"]))
            daff = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, "g-daff-pending"))
            server, comms, sessions = await _server_with(store, {"bart": bart, "daff": daff})

            # Two tells arrive while daff's pane is absent -> both queue, in order.
            for i, text in enumerate(["first", "second"]):
                reply = await server._on_tell({
                    "to_stream_id": DAFF_CHAT, "text": text, "tell_id": f"t{i}",
                    "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
                })
                assert reply["queued"] is True
                assert reply["delivery_status"] == "queued_unbound"
            assert not comms.told
            queued = await store.claim_composite_tells(name="daff")
            assert [r["body"] for r in queued] == ["first", "second"]

            # The daff pane comes up at the bound generation; flush delivers in order.
            await store.open_session(
                "fixture-daffs", "visible", provider="codex", role="assistant",
                visibility="default", pane_status="pane_alive",
                effective_model="gpt-6-sol", effective_effort="high",
                claude_session_id=None)
            # Rebind daff's binding generation to the live seat so delivery matches.
            live = await store.fetch_session("fixture-daffs", "visible")
            daff2 = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, live["session_generation"]))
            await daff2.load_binding()
            server.assistant_composites["daff"] = daff2

            delivered = await server._flush_composite_tells(daff2)
            assert delivered == 2
            assert [m["message"] for m in comms.told] == ["first", "second"]
            assert [m["to_stream_id"] for m in comms.told] == [DAFF_SEAT, DAFF_SEAT]
            assert await store.claim_composite_tells(name="daff") == []
        finally:
            store.stop()

    asyncio.run(run())
