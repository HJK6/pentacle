import asyncio
import logging
from types import SimpleNamespace

import pytest

from ingest import Ingest
from ledger import NUDGE_DEFAULT_BACKOFF_MAX_S, NudgeJob
from mirror import Mirror
from outbound_notices import NOTICE_BACKOFF_MAX_S, OutboundNoticeQueue
from retention import RetentionJob


async def check_ingest_stream_backoff(job, failures, max_delay, monkeypatch):
    rows = [
        {"stream_id": f"test:{name}", "host": "test", "provider": "fixture"}
        for name in ("failing", "healthy")
    ]
    attempts = {row["stream_id"]: 0 for row in rows}

    def split(stream_id):
        attempts[stream_id] += 1
        if stream_id == "test:failing":
            raise RuntimeError("persistent stream failure")
        return stream_id.split(":", 1)

    monkeypatch.setattr(job, "sessions", SimpleNamespace(list_open=lambda: rows, split=split))
    now = 0.0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: now)

    for _ in range(failures):
        await job.run_pass()
        now += max_delay

    now -= 0.5
    await job.run_pass()
    assert attempts == {"test:failing": failures, "test:healthy": failures + 1}
    now += 0.5
    await job.run_pass()
    assert attempts == {"test:failing": failures + 1, "test:healthy": failures + 2}


@pytest.mark.parametrize(
    ("job", "pass_name", "max_delay"),
    [
        pytest.param(Ingest(None, None, None, None, local_host="test", recent_limit=1),
                     "run_pass", 30.0, id="ingest"),
        pytest.param(Ingest(None, None, None, None, local_host="test", recent_limit=1),
                     "split", 30.0, id="ingest-stream"),
        pytest.param(Mirror(None, None, None, None, local_host="test"),
                     "run_pass", 30.0, id="mirror"),
        pytest.param(NudgeJob(None, None, None), "run_pass", NUDGE_DEFAULT_BACKOFF_MAX_S,
                     id="nudge"),
        pytest.param(RetentionJob(None), "run_pass", 3600.0, id="retention"),
        pytest.param(OutboundNoticeQueue(None), "drain_once", NOTICE_BACKOFF_MAX_S,
                     id="outbound-notices"),
    ],
)
def test_retry_loop_keeps_running_at_max_delay(job, pass_name, max_delay, monkeypatch, caplog):
    # 2 ** 1024 cannot be converted to a float. Drive two retries past that point.
    failures = 1026
    caplog.set_level(logging.CRITICAL)
    if pass_name == "split":
        asyncio.run(check_ingest_stream_backoff(job, failures, max_delay, monkeypatch))
        return

    passes = 0
    delays = []

    async def fail_pass():
        nonlocal passes
        passes += 1
        if passes > failures:
            raise asyncio.CancelledError
        raise RuntimeError("persistent pass failure")

    async def record_delay(delay):
        delays.append(delay)
        if len(delays) > failures + 1:
            pytest.fail("retry loop stopped making passes")

    monkeypatch.setattr(job, pass_name, fail_pass)
    monkeypatch.setattr(asyncio, "sleep", record_delay)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(job.run_forever())

    assert delays[-2:] == [max_delay, max_delay]
