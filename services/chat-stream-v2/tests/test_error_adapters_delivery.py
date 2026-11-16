"""Five real-path synthetic delivery rows on the frozen core fixture."""
import asyncio
import json

import pytest

from error_alerts_fixture import ErrorAlertsHarness
from test_error_adapters import expected_episode
from test_error_adapters_ingress import (
    CD, BACKUP, BOT, OPERATION, TOKENS, TOKEN_ENVS,
    authenticate, dispatch, producer_frame,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('row_id,actor,family,code', [
    ('D1', None, 'session_lifecycle', 'session_dead'),
    ('D2', None, 'integrity', 'pin_drift'),
    ('D3', CD, 'system_deploy', 'deploy_failed'),
    ('D4', BACKUP, 'system_backup', 'backup_failed'),
    ('D5', BOT, 'bot_messaging', 'registration_failed'),
], ids=['D1-session', 'D2-integrity-restart', 'D3-deploy-census', 'D4-backup', 'D5-bot'])
async def test_five_producer_delivery(tmp_path, monkeypatch, row_id, actor, family, code):
    monkeypatch.setenv('PENTACLE_ERROR_ALERTS_MODE', 'on')
    for env in TOKEN_ENVS.values():
        monkeypatch.delenv(env, raising=False)
    if actor is not None:
        token_file = tmp_path / 'synthetic-producer.token'
        token_file.write_text(TOKENS[actor])
        token_file.chmod(0o600)
        monkeypatch.setenv(TOKEN_ENVS[actor], str(token_file))
        if actor == CD:
            monkeypatch.setenv('PENTACLE_SYSTEM_PRODUCER_STREAM_ID', CD)
    h = ErrorAlertsHarness(tmp_path / row_id, real_transport=False)
    await h.start()
    try:
        h.notify.alerts = h.alerts
        frame = producer_frame(actor) if actor else {}
        peer = await authenticate(h.server, actor) if actor else None
        if row_id == 'D1':
            fields = {'episode_id': 'ep-synthetic-1', 'stream_id': 'fixture:v2-test'}
            keys = ('episode_id',)
            async def induce():
                return await h.alerts.record('reconciler_session_dead', **fields)
        elif row_id == 'D2':
            fields = {'host': 'fixture', 'pinned_sha': 'a'*40, 'daemon_sha': 'b'*40}
            keys = ('host', 'pinned_sha', 'daemon_sha')
            async def induce():
                return await h.alerts.record('pin_drift', **fields)
        else:
            fields = {'step': 'registration', 'operation_id': OPERATION} if actor == BOT else frame
            keys = ('step', 'operation_id') if actor == BOT else ('dedup_key',)
            async def induce():
                reply = await dispatch(h.server, peer, frame)
                assert reply['type'] == 'notification.create.ok'
                return reply['notification']['notification_id']
        episode = expected_episode(code, fields, keys)
        principal = 'system:' + actor if actor else 'daemon:fixture'
        nid = await induce()
        (row,) = await h.notify._db.call('error_rows')
        assert row['error_key'] == f'{family}:{principal}:{episode}'
        assert row['producer'] == family
        assert (row['error_context']['family'], row['error_context']['code']) == (family, code)
        if actor is None:
            assert row['notification_id'] == nid
        await h.queue.drain_once()
        # The frozen fixture starts its own pump; it can own this notice's
        # lease while drain_once returns. Wait for that same synthetic paste.
        async with asyncio.timeout(2):
            while not h.provider.pastes:
                await asyncio.sleep(0.01)
        assert len(h.provider.pastes) == 1
        (notice,) = await h.server.error_alerts.notice_rows()
        assert json.loads(notice['metadata'])['notification_id'] == row['notification_id']
        prefix = f"BLOCKER {family} {code} alert={row['notification_id']} episode={episode} condition=active"
        assert notice['body'].startswith(prefix)
        # Amended W5 #2: the frozen transport prepends its exact notice marker.
        assert h.provider.pastes[0] == f"[pentacle-notice:{notice['tell_id']}]\n" + notice['body']
        for private in (frame.get('title'), frame.get('body'), frame.get('stream_token'), 'https://ci.example.invalid/runs/1'):
            if private:
                assert private not in h.provider.pastes[0]
        await induce()
        await h.queue.drain_once()
        (repeated,) = await h.notify._db.call('error_rows')
        assert repeated['firing_count'] == 2
        assert repeated['notification_id'] == row['notification_id']
        assert len(h.provider.pastes) == 1
        assert len(await h.server.error_alerts.notice_rows()) == 1
        if row_id == 'D3':
            warning = producer_frame(CD, severity='warning', dedup_key='census|fn-example|invariant-example|2026-01-15')
            assert (await dispatch(h.server, peer, warning))['type'] == 'notification.create.ok'
            await h.queue.drain_once()
            assert len(await h.notify._db.call('error_rows')) == 1
            assert len(h.provider.pastes) == 1
        if row_id == 'D2':
            # Amended W5 #5: restart replaces the synthetic provider, so retain
            # the observed pre-restart history and require no new paste.
            before_restart = list(h.provider.pastes)
            await h.restart()
            await h.queue.drain_once()
            (restarted,) = await h.notify._db.call('error_rows')
            (restarted_notice,) = await h.server.error_alerts.notice_rows()
            assert restarted['notification_id'] == row['notification_id']
            assert restarted['error_key'] == row['error_key']
            assert restarted_notice['notice_id'] == notice['notice_id']
            assert json.loads(restarted_notice['metadata'])['notification_id'] == row['notification_id']
            assert h.provider.pastes == []
            assert len(before_restart) + len(h.provider.pastes) == 1
    finally:
        await h.stop()
