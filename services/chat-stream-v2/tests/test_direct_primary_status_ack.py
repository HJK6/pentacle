"""Direct-primary first-visible acknowledgment: no lane operation exists, so none is required."""
import asyncio

import pytest

from test_assistant_direct_primary import ASSISTANT
from test_managed_attachment_publish import publication_fixture


def ack(msg, **changes):
    return {**msg, "request_id": "publish:dispatch-1:ack", "publish_kind": "status",
            "response_state": "acknowledged", "message": "On it; result follows.", **changes}


async def texts(store):
    return [e["text"] for e in await store.fetch_session_event_tail(ASSISTANT, limit=10) if e["kind"] == "ASSIST_TEXT"]


def test_direct_primary_acknowledgment_publishes_once_without_operation_receipt_then_final(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs, store, server, msg):
            first = await server._on_assistant_publish(ack(msg))
            retry = await server._on_assistant_publish(ack(msg))
            assert retry["duplicate"] is True and retry["event_id"] == first["event_id"]
            final = await server._on_assistant_publish({**msg, "message": "Done."})
            assert final["duplicate"] is False and final["event_id"] != first["event_id"]
            assert await texts(store) == ["On it; result follows.", "Done."]
    asyncio.run(run())


@pytest.mark.parametrize("changes", [
    {"request_id": "publish:dispatch-1:ack2"},
    {"request_id": "publish:dispatch-1"},
    {"response_state": "final"},
    {"response_state": None},
    {"publish_kind": "result", "response_state": None},
    {"publish_kind": "prose"},
])
def test_only_the_exact_ack_identity_skips_the_operation_receipt(tmp_path, changes):
    async def run():
        async with publication_fixture(tmp_path) as (blobs, store, server, msg):
            body = ack(msg, **changes)
            if body["response_state"] is None:
                del body["response_state"]
            with pytest.raises(Exception, match="operation_receipt_required|direct_publication_identity_invalid"):
                await server._on_assistant_publish(body)
            assert await texts(store) == []
    asyncio.run(run())
