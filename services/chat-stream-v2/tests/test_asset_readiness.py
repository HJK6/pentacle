"""Asset RPCs must settle when the asset store never becomes ready."""

from __future__ import annotations

import asyncio
import json

import pytest

from assets import Assets


ASSET_VERBS = (
    "asset.publish",
    "asset.health",
    "asset.list",
    "asset.get",
    "asset.delete",
    "asset.comment.add",
    "asset.comment.edit",
    "asset.comment.delete",
    "asset.comment.resolve",
    "asset.comments.list",
    "asset.read.set",
    "asset.review.set",
    "asset.comments.send_to_chat",
)


@pytest.mark.parametrize("verb", ASSET_VERBS)
def test_every_asset_verb_replies_when_store_never_becomes_ready(
    tmp_path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    async def scenario() -> dict:
        assets = Assets(str(tmp_path / "assets.db"))

        async def never_ready() -> None:
            raise asyncio.TimeoutError

        monkeypatch.setattr(assets, "_await_ready", never_ready)
        return await assets.asset({"type": verb, "request_id": "readiness-timeout"})

    reply = asyncio.run(scenario())

    assert reply["type"] == "asset.error", reply
    assert reply["request_id"] == "readiness-timeout", reply
    assert reply["error_code"] == "asset_store_not_ready", reply
    assert "Traceback" not in json.dumps(reply)


def test_unexpected_readiness_failure_returns_safe_error_frame(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> dict:
        assets = Assets(str(tmp_path / "assets.db"))

        async def readiness_failed() -> None:
            raise RuntimeError("diagnostic readiness details")

        monkeypatch.setattr(assets, "_await_ready", readiness_failed)
        return await assets.asset({"type": "asset.get", "request_id": "unexpected"})

    reply = asyncio.run(scenario())

    assert reply["type"] == "asset.error", reply
    assert reply["request_id"] == "unexpected", reply
    assert reply["error_code"] == "asset_store_error", reply
    assert "diagnostic readiness details" not in json.dumps(reply)
    assert "Traceback" not in json.dumps(reply)
