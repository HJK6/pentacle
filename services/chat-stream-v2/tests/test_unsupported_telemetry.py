"""Unsupported-v2 telemetry is recorded without changing the error reply."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

import server as server_module  # noqa: E402
from read_daemon_stats import read_daemon_stats  # noqa: E402
from server import Server  # noqa: E402
from sessions import VerbError  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def test_unsupported_reply_records_per_verb_caller_and_timestamp() -> None:
    async def go() -> None:
        server = Server()
        websocket = object()
        await server._dispatch(json.dumps({"type": "hello", "client": "public-client"}), websocket=websocket)

        first = await server._dispatch(
            json.dumps({"type": "session.spec_update", "request_id": "r1"}),
            websocket=websocket,
        )
        second = await server._dispatch(
            json.dumps({"type": "session.spec_update", "request_id": "r2"}),
            websocket=websocket,
        )
        stats_reply = await server._dispatch(json.dumps({"type": "daemon.stats", "request_id": "stats"}))

        assert first[0]["error_code"] == "unsupported_in_v2"
        assert second[0]["error_code"] == "unsupported_in_v2"
        assert stats_reply[0]["type"] == "daemon.stats.ok"
        assert stats_reply[0]["request_id"] == "stats"
        record = stats_reply[0]["stats"]["unsupported_in_v2"]["verbs"]["session.spec_update"]
        assert record["verb"] == "session.spec_update"
        assert record["count"] == 2
        assert record["caller"] == "public-client"
        assert record["callers"] == {"public-client": 2}
        assert record["first_seen"]
        assert record["last_seen"]
        assert record["last_seen"].endswith("Z")

    _run(go())


def test_unsupported_logs_once_per_verb_during_rate_limit_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def go() -> None:
        server = Server()
        with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.server"):
            for request_id in ("r1", "r2"):
                await server._dispatch(
                    json.dumps({"type": "session.spec_update", "request_id": request_id}),
                    websocket=object(),
                )

        records = [record for record in caplog.records if "unsupported_in_v2" in record.getMessage()]
        assert len(records) == 1
        assert "verb=session.spec_update" in records[0].getMessage()
        assert "caller=<unknown>" in records[0].getMessage()
        assert "timestamp=" in records[0].getMessage()

    _run(go())


def test_unsupported_log_rate_limit_can_expire(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    clock = iter((100.0, 100.0, 161.0))
    monkeypatch.setattr(server_module, "_monotonic", lambda: next(clock))

    async def go() -> None:
        server = Server()
        with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.server"):
            for request_id in ("r1", "r2", "r3"):
                await server._dispatch(
                    json.dumps({"type": "future.verb", "request_id": request_id}),
                    websocket=object(),
                )

        records = [record for record in caplog.records if "unsupported_in_v2" in record.getMessage()]
        assert len(records) == 2

    _run(go())


def test_handler_unsupported_errors_are_recorded_once() -> None:
    async def go() -> None:
        server = Server()

        async def reply_error(_msg: dict) -> dict:
            return {"type": "future.error", "error_code": "unsupported_in_v2"}

        async def raise_error(_msg: dict) -> dict:
            raise VerbError("unsupported_in_v2", "not implemented")

        server.handlers["future.reply"] = reply_error
        server.handlers["future.raise"] = raise_error
        websocket = object()
        await server._dispatch(json.dumps({"type": "hello", "client": "public-client"}), websocket=websocket)

        reply = await server._dispatch(json.dumps({"type": "future.reply", "request_id": "r1"}), websocket=websocket)
        await asyncio.sleep(0.001)
        raised = await server._dispatch(json.dumps({"type": "future.raise", "request_id": "r2"}), websocket=websocket)
        stats = await server._dispatch(json.dumps({"type": "daemon.stats", "request_id": "stats"}))
        records = stats[0]["stats"]["unsupported_in_v2"]["verbs"]

        assert reply[0]["error_code"] == "unsupported_in_v2"
        assert raised[0]["error_code"] == "unsupported_in_v2"
        assert records["future.reply"]["count"] == 1
        assert records["future.raise"]["count"] == 1
        assert records["future.reply"]["caller"] == "public-client"
        assert records["future.raise"]["caller"] == "public-client"
        assert stats[0]["stats"]["unsupported_in_v2"]["recent"][0]["verb"] == "future.raise"

    _run(go())


def test_telemetry_bounds_and_escapes_untrusted_labels(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(server_module, "MAX_UNSUPPORTED_VERBS", 2)
    monkeypatch.setattr(server_module, "MAX_UNSUPPORTED_CALLERS_PER_VERB", 2)

    async def go() -> None:
        server = Server()
        long_verb = "first" + chr(10) + "x" * 500
        unsafe_client = 'client name="alpha"' + chr(13) + "y" * 500
        websocket = object()
        await server._dispatch(json.dumps({"type": "hello", "client": unsafe_client}), websocket=websocket)
        with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.server"):
            await server._dispatch(json.dumps({"type": long_verb}), websocket=websocket)
            await server._dispatch(json.dumps({"type": "second.verb"}), websocket=websocket)
        stats = await server._dispatch(json.dumps({"type": "daemon.stats"}))
        telemetry = stats[0]["stats"]["unsupported_in_v2"]

        assert telemetry["total"] == 2
        assert len(telemetry["verbs"]) <= server_module.MAX_UNSUPPORTED_VERBS
        assert telemetry["verbs"]["<overflow>"]["count"] == 1
        first_record = telemetry["verbs"]["<overflow>"]
        assert "%0A" in caplog.records[0].getMessage()
        assert "%0D" in caplog.records[0].getMessage()
        assert "%20" in caplog.records[0].getMessage()
        assert "%22" in caplog.records[0].getMessage()
        assert all(len(record["verb"]) <= server_module.MAX_UNSUPPORTED_LABEL_LENGTH for record in telemetry["recent"])
        assert all(len(record["callers"]) <= server_module.MAX_UNSUPPORTED_CALLERS_PER_VERB for record in telemetry["recent"])
        assert all(len(record["callers"]) <= server_module.MAX_UNSUPPORTED_CALLERS_PER_VERB for record in telemetry["verbs"].values())
        assert first_record["callers"]
        assert all("\n" not in record.getMessage() and "\r" not in record.getMessage() for record in caplog.records)

    _run(go())


def test_telemetry_caps_are_exact_when_overflow_keys_are_first_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_module, "MAX_UNSUPPORTED_VERBS", 1)
    monkeypatch.setattr(server_module, "MAX_UNSUPPORTED_CALLERS_PER_VERB", 1)

    async def go() -> None:
        server = Server()
        clients = [object(), object(), object()]
        for index, websocket in enumerate(clients):
            await server._dispatch(
                json.dumps({"type": "hello", "client": f"client-{index}"}),
                websocket=websocket,
            )
            await server._dispatch(
                json.dumps({"type": f"verb-{index}"}),
                websocket=websocket,
            )
        stats = await server._dispatch(json.dumps({"type": "daemon.stats"}))
        telemetry = stats[0]["stats"]["unsupported_in_v2"]

        assert len(telemetry["verbs"]) == 1
        assert len(telemetry["verbs"]["<overflow>"]["callers"]) == 1
        assert telemetry["total"] == 3

    _run(go())


def test_reserved_labels_do_not_collide_with_aggregate_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_module, "MAX_UNSUPPORTED_VERBS", 2)

    async def go() -> None:
        server = Server()
        websocket = object()
        await server._dispatch(
            json.dumps({"type": "hello", "client": "<other>"}),
            websocket=websocket,
        )
        await server._dispatch(json.dumps({"type": "<overflow>"}), websocket=websocket)
        await server._dispatch(json.dumps({"type": "second.verb"}), websocket=websocket)
        stats = await server._dispatch(json.dumps({"type": "daemon.stats"}))
        verbs = stats[0]["stats"]["unsupported_in_v2"]["verbs"]

        assert "%3Coverflow%3E" in verbs
        assert "<overflow>" in verbs
        assert verbs["%3Coverflow%3E"]["callers"] == {"%3Cother%3E": 1}
        assert verbs["<overflow>"]["count"] == 1

    _run(go())


def test_fallback_labels_do_not_collide_with_known_values() -> None:
    async def go() -> None:
        server = Server()
        websocket = object()
        await server._dispatch(
            json.dumps({"type": "hello", "client": "<unknown>"}),
            websocket=websocket,
        )
        await server._dispatch(json.dumps({"type": "<missing>"}), websocket=websocket)
        stats = await server._dispatch(json.dumps({"type": "daemon.stats"}))
        verbs = stats[0]["stats"]["unsupported_in_v2"]["verbs"]

        assert "%3Cmissing%3E" in verbs
        assert verbs["%3Cmissing%3E"]["callers"] == {"%3Cunknown%3E": 1}
        assert "<missing>" not in verbs

    _run(go())


def test_stats_reader_accepts_maximal_bounded_snapshot() -> None:
    async def go() -> None:
        server = Server(port=0)
        for verb_index in range(server_module.MAX_UNSUPPORTED_VERBS - 1):
            for caller_index in range(server_module.MAX_UNSUPPORTED_CALLERS_PER_VERB):
                server._record_unsupported(
                    f"verb-{verb_index}",
                    caller=f"caller-{caller_index}",
                )
        for caller_index in range(server_module.MAX_UNSUPPORTED_CALLERS_PER_VERB):
            server._record_unsupported("overflow-trigger", caller=f"caller-{caller_index}")

        await server.bind()
        try:
            frame = await read_daemon_stats(
                ws_url=f"ws://127.0.0.1:{server.port}",
                timeout_s=5.0,
            )
        finally:
            await server.close()

        telemetry = frame["stats"]["unsupported_in_v2"]
        assert len(telemetry["verbs"]) == server_module.MAX_UNSUPPORTED_VERBS
        assert all(
            len(record["callers"]) == server_module.MAX_UNSUPPORTED_CALLERS_PER_VERB
            for record in telemetry["verbs"].values()
        )

    _run(go())
