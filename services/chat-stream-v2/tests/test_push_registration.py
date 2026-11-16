import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
import uiverbs
from uiverbs import PUSH_TTL_SECONDS, UIVerbs

def test_register_push_writes_v1_dynamo_shape_off_loop(monkeypatch) -> None:
    table, dynamo, resource = Mock(), Mock(), Mock()
    dynamo.Table.return_value = table
    resource.return_value = dynamo
    thread_calls = []
    async def to_thread(fn):
        thread_calls.append(fn)
        return fn()

    monkeypatch.setattr(uiverbs, "boto3", SimpleNamespace(resource=resource))
    monkeypatch.setattr(uiverbs.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(uiverbs, "PUSH_TOKENS_TABLE", "PushTokens-test")
    monkeypatch.setattr(uiverbs, "AWS_REGION", "us-test-1")
    monkeypatch.setattr(uiverbs, "iso_now", lambda: "2026-09-02T15:00:00Z")
    monkeypatch.setattr(uiverbs.time, "time", lambda: 1_700_000_000)
    response = asyncio.run(UIVerbs(None, None, None).register_push({
        "request_id": "rp1", "push_token": "TEST",
        "platform": "ios", "device_name": "Example Device",
    }))
    assert response == {"type": "register_push.ok", "request_id": "rp1"}
    assert resource.call_args.args == ("dynamodb",)
    assert resource.call_args.kwargs == {"region_name": "us-test-1"}
    assert dynamo.Table.call_args.args == ("PushTokens-test",)
    assert len(thread_calls) == 1
    assert table.put_item.call_args.kwargs == {"Item": {
        "push_token": "TEST", "platform": "ios",
        "device_name": "Example Device", "registered_at": "2026-09-02T15:00:00Z",
        "active": True, "ttl": 1_700_000_000 + PUSH_TTL_SECONDS,
    }}
