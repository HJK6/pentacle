"""Resolve-rejection diagnostics.

`Notify.notification` logs one bounded WARNING describing the shape of a
rejected `notification.*` payload (keys and resolve-field types, never
bodies, titles, or choice values).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from notify import Notify  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


async def _notify(tmp_path: Path) -> Notify:
    n = Notify(db_path=str(tmp_path / "notifications.db"))
    await n.start()
    return n


def test_rejected_resolve_logs_one_bounded_warning_with_payload_shape(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def go() -> None:
        n = await _notify(tmp_path)
        try:
            # This invalid action exercises the bounded diagnostic shape. The
            # payload carries title/body values that the diagnostic must not echo.
            with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.notify"):
                reply = await n.notification({
                    "type": "notification.resolve",
                    "request_id": "r1",
                    "notification_id": "n-123",
                    "action_kind": "spawn_worker",
                    "action_id": "a0",
                    "title": "TITLE",
                    "body": "BODY",
                })
        finally:
            await n.stop()
        assert reply["error_code"] == "notification_invalid"

        rejections = [r for r in caplog.records if "rejected" in r.getMessage()]
        assert len(rejections) == 1, rejections
        line = rejections[0].getMessage()
        # The shape is logged: the verb, the code, the payload key names, and the
        # resolve-field discriminators.
        assert "notification.resolve" in line
        assert "notification_invalid" in line
        assert "action_kind='spawn_worker'" in line
        assert "action_id=str" in line          # action_id logged as a TYPE name
        assert "choice=NoneType" in line         # no choice field is present
        assert "notification_id" in line and "title" in line  # KEY names present
        # Privacy floor: never the body/title VALUES nor a choice value.
        assert "TITLE" not in line and "BODY" not in line

    _run(go())


def test_unknown_notification_command_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def go() -> None:
        n = await _notify(tmp_path)
        try:
            with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.notify"):
                reply = await n.notification({"type": "notification.frobnicate", "request_id": "r2"})
        finally:
            await n.stop()
        assert reply["error_code"] == "notification_unknown_command"
        rejections = [r for r in caplog.records if "rejected" in r.getMessage()]
        assert len(rejections) == 1
        assert "notification.frobnicate" in rejections[0].getMessage()

    _run(go())


def test_a_valid_notification_verb_logs_no_rejection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def go() -> None:
        n = await _notify(tmp_path)
        try:
            with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.notify"):
                reply = await n.notification({
                    "type": "notification.list", "request_id": "r3", "states": ["open"],
                })
        finally:
            await n.stop()
        assert reply["type"] == "notification.list.ok"
        assert [r for r in caplog.records if "rejected" in r.getMessage()] == []

    _run(go())
