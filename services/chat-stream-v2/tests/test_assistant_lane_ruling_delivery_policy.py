"""Lane-ruling requests reach a separately bound ruling authority.

In direct-primary mode the composite's ``astra_stream_id`` is the primary
itself, while the lane-ruling authority is an independent seat bound through
``assistant.authority``.  The ingress policy must not reject a ruling request
merely because its recipient is that independent seat; comms validates the
pending ruling row's exact authority stream and generation before paste.
"""
import asyncio
from contextlib import asynccontextmanager

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from outbound_notices import ASSISTANT_AUTHORITY_REQUEST_TOKEN
from sessions import VerbError
from store import Store

COMPOSITE = "local:assistant"
PRIMARY = "local:assistant-backend-fixture"
ADVISOR = "local:advisor-fixture"


@asynccontextmanager
async def composite(enabled=True):
    store = Store(":memory:")
    store.start()
    config = AssistantCompositeConfig(
        enabled=enabled, stream_id=COMPOSITE if enabled else "",
        astra_stream_id=PRIMARY if enabled else "",
        direct_primary_stream_id=PRIMARY if enabled else "",
        direct_primary_generation="gen-primary" if enabled else "",
    )
    instance = AssistantComposite(store, config=config)
    try:
        yield instance
    finally:
        await instance.stop()
        store.stop()


def authority_message(target, *, ruling_id=None):
    msg = {"stream_id": target, "to_stream_id": target, "tell_id": "notice-1",
           "_assistant_authority_request_token": ASSISTANT_AUTHORITY_REQUEST_TOKEN,
           "_assistant_authority_request_generation": "gen-advisor"}
    if ruling_id:
        msg["_assistant_lane_ruling_request_id"] = ruling_id
    return msg


def ingress(instance, target, msg):
    return instance.suppress_routine_backend_ingress(
        target_stream_id=target, body="[Assistant lane ruling request]\n{}", msg=msg, verb="tell",
    )


def test_lane_ruling_request_reaches_independent_authority_in_direct_mode():
    async def run():
        async with composite() as instance:
            assert instance.config.direct_primary
            result = await ingress(instance, ADVISOR, authority_message(ADVISOR, ruling_id="ruling-1"))
            assert result is None
    asyncio.run(run())


def test_composite_authority_request_to_other_seat_still_refused():
    async def run():
        async with composite() as instance:
            with pytest.raises(VerbError, match="configured assistant authority changed"):
                await ingress(instance, ADVISOR, authority_message(ADVISOR))
    asyncio.run(run())


def test_lane_ruling_request_refused_when_composite_disabled():
    async def run():
        async with composite(enabled=False) as instance:
            with pytest.raises(VerbError, match="configured assistant authority changed"):
                await ingress(instance, ADVISOR, authority_message(ADVISOR, ruling_id="ruling-1"))
    asyncio.run(run())
