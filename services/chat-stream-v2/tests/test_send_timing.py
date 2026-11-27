import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

import store_call_diag
from send_timing import SendTiming


@pytest.mark.asyncio
async def test_store_summary_flushes_quiet_send_window(caplog, monkeypatch):
    loop = asyncio.get_running_loop()
    flushed = loop.create_future()
    timing = SendTiming(clock=lambda: 0)

    def flush(now):
        timing.flush(now)
        flushed.set_result(None)

    monkeypatch.setattr(store_call_diag, "send_counter", SimpleNamespace(flush=flush))
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    diag = store_call_diag.StoreCallDiag()

    diag.record("read", False, loop, 0, 0, 0)
    await asyncio.to_thread(diag.record, "read", False, loop, 3600, 3600, 3600)
    await asyncio.wait_for(flushed, timeout=1)

    store_line, send_line = caplog.messages
    assert store_line.startswith("store_call_top ")
    assert send_line.startswith("send_latency ")
    assert json.loads(send_line.removeprefix("send_latency "))["attempts"] == 0


def test_forced_flush_reports_and_resets_partial_window(caplog):
    times = iter([0, 2])
    timing = SendTiming(clock=times.__next__)
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    timing.record(1.5, {"delivery": "landed", "submission_confirmed": True})

    timing.flush(12.5, force=True)
    timing.flush(13, force=True)

    first, second = [json.loads(line.removeprefix("send_latency ")) for line in caplog.messages]
    assert (first["window_s"], first["attempts"]) == (12.5, 1)
    assert (second["window_s"], second["attempts"]) == (0.5, 0)


def test_send_latency_counts_reply_time_boundaries(caplog):
    timing = SendTiming(clock=lambda: 3)
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    landed = {"delivery": "landed", "submission_confirmed": True}

    for elapsed in (0.3, 0.5, 0.9, 1, 1.5, 2, 2.5):
        timing.record(3 - elapsed, landed)
    timing.record(2.7, {**landed, "coalesced": True})
    timing.flush(4, force=True)

    [line] = caplog.messages
    window = json.loads(line.removeprefix("send_latency "))
    expected = {
        "attempts": 7, "coalesced": 1, "proof_under_1s": 3,
        "reply_under_500ms": 1, "reply_under_1s": 3,
        "reply_under_2s": 5, "reply_2s_or_more": 2,
    }
    assert {key: window.get(key) for key in expected} == expected


def test_send_latency_counts_backend_dispatches_separately(caplog):
    times = iter([0, 1, 3600, 3600, 3600, 3600, 3600])
    timing = SendTiming(clock=times.__next__)
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    landed = {"delivery": "landed", "submission_confirmed": True}

    timing.record(0.75, landed)
    timing.record(3599.75, landed, backend=True)
    timing.record(3599, landed, backend=True)
    timing.record(3597.5, {"delivery": "committed_pending_proof"}, backend=True)
    timing.record(3599.25, {"delivery": "not_landed"}, backend=True)
    timing.record(3599.75, {**landed, "coalesced": True}, backend=True)
    assert not caplog.messages

    timing.flush(3600)
    timing.flush(3601, force=True)
    first, second = [json.loads(line.removeprefix("send_latency ")) for line in caplog.messages]
    expected = {
        "attempts": 1, "reply_under_1s": 1, "proof_under_1s": 1,
        "pending_proof": 0, "failed": 0, "coalesced": 0,
        "backend_attempts": 4, "backend_proof_under_1s": 1,
        "backend_pending_proof": 1, "backend_worst_ms": 2500,
    }
    assert {key: first[key] for key in expected} == expected
    assert {key: value for key, value in second.items() if key.startswith("backend_")} == {
        "backend_attempts": 0, "backend_proof_under_1s": 0,
        "backend_pending_proof": 0, "backend_worst_ms": None,
    }


def test_send_latency_counts_and_resets(caplog):
    times = iter([0, 1, 2, 3, 4, 5, 6, 7, 3601, 7202])
    timing = SendTiming(clock=times.__next__)
    caplog.set_level(logging.INFO, logger="chat_streamd_v2.store")
    landed = {"delivery": "landed", "submission_confirmed": True}
    pending = {"delivery": "committed_pending_proof", "state": "accepted",
               "submission_confirmed": False}

    timing.record(0.75, landed)
    timing.record(1.5, {**landed, "provider_queued": True})
    timing.record(2, landed)
    timing.record(2, pending)
    timing.record(4.5, {**pending, "provider_queued": True})
    timing.record(5.75, {"delivery": "persisted", "submission_confirmed": False})
    timing.record(6.875, {"delivery": "not_landed", "submission_confirmed": False})
    assert not caplog.messages

    timing.record(3400, {**landed, "coalesced": True, "provider_queued": True})
    first_window = {
        "window_s": 3601, "attempts": 7, "reply_under_1s": 5,
        "reply_under_500ms": 3, "reply_under_2s": 6, "reply_2s_or_more": 1,
        "proof_under_1s": 2, "queued": 1, "pending_proof": 3, "failed": 1,
        "coalesced": 1, "reply_p95_ms": 2000, "reply_worst_ms": 2000,
        "backend_attempts": 0, "backend_proof_under_1s": 0,
        "backend_pending_proof": 0, "backend_worst_ms": None,
        "instrumentation_failures": 0,
    }
    assert len(caplog.messages) == 1
    assert json.loads(caplog.messages[0].removeprefix("send_latency ")) == first_window

    timing.record(7201.75, landed)
    assert len(caplog.messages) == 2
    assert json.loads(caplog.messages[1].removeprefix("send_latency ")) == {
        "window_s": 3601, "attempts": 1, "reply_under_1s": 1,
        "reply_under_500ms": 1, "reply_under_2s": 1, "reply_2s_or_more": 0,
        "proof_under_1s": 1, "queued": 0, "pending_proof": 0, "failed": 0,
        "coalesced": 0, "reply_p95_ms": 250, "reply_worst_ms": 250,
        "backend_attempts": 0, "backend_proof_under_1s": 0,
        "backend_pending_proof": 0, "backend_worst_ms": None,
        "instrumentation_failures": 0,
    }


def test_send_timing_failure_does_not_escape(caplog, monkeypatch):
    times = iter([0, 3601, 7202])
    timing = SendTiming(clock=times.__next__)
    logger = logging.getLogger("chat_streamd_v2.store")
    caplog.set_level(logging.INFO, logger=logger.name)
    landed = {"delivery": "landed", "submission_confirmed": True}

    def broken_log(*args):
        raise OSError("diagnostic sink unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(logger, "info", broken_log)
        timing.record(3600.75, landed)
        timing.flush(3601)

    timing.record(7201.75, landed)
    timing.flush(7202)
    assert len(caplog.messages) == 1
    window = json.loads(caplog.messages[0].removeprefix("send_latency "))
    assert window["attempts"] == window["instrumentation_failures"] == 1
