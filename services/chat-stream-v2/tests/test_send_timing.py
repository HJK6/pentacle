import json
import logging

from send_timing import SendTiming


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
        "proof_under_1s": 2, "queued": 1, "pending_proof": 3, "failed": 1,
        "coalesced": 1, "reply_p95_ms": 2000, "reply_worst_ms": 2000,
        "instrumentation_failures": 0,
    }
    assert len(caplog.messages) == 1
    assert json.loads(caplog.messages[0].removeprefix("send_latency ")) == first_window

    timing.record(7201.75, landed)
    assert len(caplog.messages) == 2
    assert json.loads(caplog.messages[1].removeprefix("send_latency ")) == {
        "window_s": 3601, "attempts": 1, "reply_under_1s": 1,
        "proof_under_1s": 1, "queued": 0, "pending_proof": 0, "failed": 0,
        "coalesced": 0, "reply_p95_ms": 250, "reply_worst_ms": 250,
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

    timing.record(7201.75, landed)
    assert len(caplog.messages) == 1
    window = json.loads(caplog.messages[0].removeprefix("send_latency "))
    assert window["attempts"] == window["instrumentation_failures"] == 1
