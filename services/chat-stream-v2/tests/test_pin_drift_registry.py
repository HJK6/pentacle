"""Pin rejection/recovery through the persistent typed registry, without live roots."""
from unittest.mock import AsyncMock

import pytest

from error_alerts_fixture import ErrorAlertsHarness
from event_push import EventPush
from test_event_push import _push


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_first", [False, True])
async def test_pin_drift_recurrence_and_restart(tmp_path, monkeypatch, restart_first):
    monkeypatch.setenv('PENTACLE_ERROR_ALERTS_MODE', 'record-only')
    h = ErrorAlertsHarness(tmp_path / 'pin-drift')
    await h.start()
    def producer():
        ep = EventPush(h.store, AsyncMock(), h.alerts, recent_limit=20, daemon_sha='b'*40)
        ep._secret = AsyncMock(return_value='s3cret')
        return ep
    try:
        await h.store.put('event_push.target_sha', 'a'*40)
        ep = producer()
        wrong = _push(ep, [], sha='c'*40)
        assert (await ep.handle_push(wrong))['error'] == 'satellite_version_mismatch'
        (first,) = await h.notify._db.call('error_rows')
        assert first['error_context']['condition'] == 'active'
        if restart_first:
            await h.restart()
            ep = producer()
        await ep.handle_push(_push(ep, [], sha='a'*40))
        (first,) = await h.notify._db.call('error_rows')
        assert first['state'] == 'resolved'
        assert first['error_context']['condition'] == 'recovered'

        # The identical host/pin/daemon tuple is a new real episode after recovery.
        await ep.handle_push(wrong)
        rows = await h.notify._db.call('error_rows')
        assert len(rows) == 2
        active = [r for r in rows if r['error_context']['condition'] == 'active']
        assert len(active) == 1
        episode = active[0]['error_context']['episode_id']
        assert episode != first['error_context']['episode_id']

        await h.restart()
        ep = producer()
        await ep.handle_push(wrong)
        rows = await h.notify._db.call('error_rows')
        assert len(rows) == 2
        assert [r['error_context']['episode_id'] for r in rows
                if r['error_context']['condition'] == 'active'] == [episode]
        await ep.handle_push(_push(ep, [], sha='a'*40))
        rows = await h.notify._db.call('error_rows')
        assert all(r['state'] == 'resolved' and r['error_context']['condition'] == 'recovered'
                   for r in rows)
    finally:
        await h.stop()


@pytest.mark.asyncio
async def test_authenticated_recovery_is_host_scoped_and_equality_does_not_clear_rejection(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv('PENTACLE_ERROR_ALERTS_MODE', 'record-only')
    h = ErrorAlertsHarness(tmp_path / 'pin-drift-hosts')
    await h.start()
    try:
        await h.store.put('event_push.target_sha', 'b'*40)
        ep = EventPush(h.store, AsyncMock(), h.alerts, recent_limit=20, daemon_sha='b'*40)
        ep._secret = AsyncMock(return_value='s3cret')
        # Concurrent rejected pushes for one host coalesce to one real episode.
        await asyncio.gather(*(ep.handle_push(_push(ep, [], host='peer_a', sha='c'*40)) for _ in range(2)))
        await ep.handle_push(_push(ep, [], host='peer_b', sha='c'*40))
        await ep.check_pin_drift()
        rows = await h.notify._db.call('error_rows')
        assert len(rows) == 2 and all(r['error_context']['condition'] == 'active' for r in rows)
        assert (await ep.handle_push(_push(ep, [], host='peer_a', sha='b'*40, secret='wrong')))['error'] == 'unauthorized'
        assert all(r['error_context']['condition'] == 'active' for r in await h.notify._db.call('error_rows'))
        await ep.handle_push(_push(ep, [], host='peer_a', sha='b'*40))
        rows = await h.notify._db.call('error_rows')
        assert sorted(r['error_context']['condition'] for r in rows) == ['active', 'recovered']
    finally:
        await h.stop()
