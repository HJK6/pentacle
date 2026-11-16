"""G1: a server-admitted send survives disconnect before durable completion.

Use the foundation's real loopback/auth/store/composite path, with a file-local
barrier before composite admission completes. The consumer is the fixture's
recording dispatcher, not a provider pane or a deployed-runtime receipt.
"""
from __future__ import annotations

import asyncio
import json

from tests.cosmo_e2e import harness as H


def test_pending_send_survives_disconnect_and_receipt_reconnect(tmp_path, monkeypatch):
    async def run():
        hz = await H.FoundationHarness(tmp_path).start()
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        disconnected = asyncio.Event()
        completions = []
        send_task = None
        accept_input = hz.composite.accept_input
        unregister = hz.server._unregister_client

        async def pending_accept(msg, **kwargs):
            nonlocal send_task
            if msg.get("request_id") != "g1-pending":
                return await accept_input(msg, **kwargs)
            send_task = asyncio.current_task()
            entered.set()
            try:
                await release.wait()
                result = await accept_input(msg, **kwargs)
                completions.append(result)
                return result
            finally:
                finished.set()

        def observe_disconnect(websocket):
            unregister(websocket)
            disconnected.set()

        monkeypatch.setattr(hz.composite, "accept_input", pending_accept)
        monkeypatch.setattr(hz.server, "_unregister_client", observe_disconnect)
        try:
            async with hz.mobile_client() as first:
                # Do not await the response: hold the actual authenticated send
                # task before durable USER/receipt creation, then lose its socket.
                await first._sock.send(json.dumps({
                    "type": "send", "request_id": "g1-pending",
                    "to_stream_id": H.DAFF_CHAT, "text": "pending delivery",
                    "msg_id": "g1-input",
                }))
                await asyncio.wait_for(entered.wait(), timeout=3)
                assert send_task is not None and not send_task.done()
                assert not finished.is_set() and completions == []
                assert await hz.store.get_send_receipt(H.DAFF_CHAT, "g1-pending") is None
            # Observe server-side teardown, not just the client close handshake.
            await asyncio.wait_for(disconnected.wait(), timeout=3)
            assert not finished.is_set(), "disconnect cancelled the admitted send"
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=3)
            assert len(completions) == 1, completions
            assert not send_task.cancelled()
            route = await hz.await_route(
                "g1-input", lambda r: r["routing_state"] == "resolved" and bool(hz.dispatched))
            assert route is not None and route["routing_state"] == "resolved", route
            assert len(hz.dispatched) == 1, hz.dispatched
            assert hz.dispatched[0]["route_id"] == route["route_id"]
            users = [event for event in await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=50)
                     if event["raw"].get("input_identity") == "g1-input"]
            assert len(users) == 1, users
            async with hz.mobile_client() as second:
                receipt = await second.rpc("send.receipt.get", request_id="g1-pending",
                                           to_stream_id=H.DAFF_CHAT)
                assert receipt["type"] == "send.receipt.get.ok", receipt
                assert receipt["found"] is True and len(receipt["receipts"]) == 1, receipt
                assert receipt["receipts"][0]["receipt_id"] == completions[0]["receipt_id"], receipt
            assert len(completions) == 1 and len(hz.dispatched) == 1
        finally:
            release.set()
            await hz.stop()
    asyncio.run(run())
