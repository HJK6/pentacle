import asyncio
import logging

import pytest

from ingest import Ingest
from mirror import Mirror
from outbound_notices import NOTICE_BACKOFF_MAX_S, OutboundNoticeQueue
from retention import RetentionJob


@pytest.mark.parametrize(
    ("job", "pass_name", "max_delay"),
    [
        pytest.param(Ingest(None, None, None, None, local_host="test", recent_limit=1),
                     "run_pass", 30.0, id="ingest"),
        pytest.param(Mirror(None, None, None, None, local_host="test"),
                     "run_pass", 30.0, id="mirror"),
        pytest.param(RetentionJob(None), "run_pass", 3600.0, id="retention"),
        pytest.param(OutboundNoticeQueue(None), "drain_once", NOTICE_BACKOFF_MAX_S,
                     id="outbound-notices"),
    ],
)
def test_retry_loop_keeps_running_at_max_delay(job, pass_name, max_delay, monkeypatch, caplog):
    # 2 ** 1024 cannot be converted to a float. Drive two retries past that point.
    failures = 1026
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

    caplog.set_level(logging.CRITICAL)
    monkeypatch.setattr(job, pass_name, fail_pass)
    monkeypatch.setattr(asyncio, "sleep", record_delay)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(job.run_forever())

    assert delays[-2:] == [max_delay, max_delay]
