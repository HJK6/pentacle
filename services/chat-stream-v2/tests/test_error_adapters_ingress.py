"""Real authenticated synthetic producer ingress; recording sink only."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alerts import Alerts
from notify import Notify
from server import (Server, FIXED_SYSTEM_PRODUCER_STREAM_ID,
                    WMI_BACKUP_PRODUCER_STREAM_ID, BOT_MESSAGING_PRODUCER_STREAM_ID)
from test_error_adapters import Recorder, expected_episode

CD, BACKUP, BOT = FIXED_SYSTEM_PRODUCER_STREAM_ID, WMI_BACKUP_PRODUCER_STREAM_ID, BOT_MESSAGING_PRODUCER_STREAM_ID
TOKENS = {CD: 'synthetic-cd-token', BACKUP: 'synthetic-backup-token', BOT: 'synthetic-bot-token'}
TOKEN_ENVS = {CD: 'PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE',
              BACKUP: 'PENTACLE_WMI_BACKUP_STREAM_TOKEN_FILE',
              BOT: 'PENTACLE_BOT_MESSAGING_STREAM_TOKEN_FILE'}
OPERATION = '00000000-0000-4000-8000-000000000001'


class Peer:
    remote_address = ('192.0.2.10', 54321)


def producer_frame(actor, **extra):
    frame = dict(type='notification.create', request_id='req-synthetic', from_stream_id=actor,
                 stream_token=TOKENS[actor], producer=actor, severity='critical', actions=[])
    if actor == CD:
        frame.update(title='Lambda CD pipeline blocked: reconciler',
                     body='failure_class=reconciler run=https://ci.example.invalid/runs/1',
                     dedup_key='pipeline|reconciler|2026-01-15')
    elif actor == BACKUP:
        frame.update(destination='pentacle-updates', title='Daily backup failed',
                     body='status=failed stage=archive', dedup_key='wmi-backup|archive_failed|2026-01-15')
    else:
        frame.update(title='Bot messaging failure', dedup_key=f'bot-messaging|registration|callback_timeout|{OPERATION}')
    return {**frame, **extra}


async def dispatch(server, peer, frame):
    return (await server._dispatch(json.dumps(frame), websocket=peer))[0]


async def authenticate(server, actor, token=None):
    peer = Peer()
    result = await dispatch(server, peer, dict(type='hello', request_id='hello-synthetic',
        from_stream_id=actor, stream_token=TOKENS[actor] if token is None else token,
        subscribe={'snapshot': False, 'mode': 'rpc'}))
    assert result['type'] == 'ready', result
    return peer


class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='error-adapters-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {}
        env = {'PENTACLE_SYSTEM_PRODUCER_STREAM_ID': CD}
        for actor, name in TOKEN_ENVS.items():
            path = self.root / (actor.replace(':', '_') + '.token')
            path.write_text(TOKENS[actor])
            path.chmod(0o600)
            self.paths[actor] = path
            env[name] = str(path)
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.alerts = Alerts()
        self.recorder = Recorder()
        self.alerts.sink = self.recorder
        self.notify = Notify(str(self.root / 'notifications.db'), alerts=self.alerts)
        await self.notify.start()
        self.addAsyncCleanup(self.notify.stop)
        self.server = Server()
        self.server.handlers.update(self.notify.wire_handlers())

    async def test_A1_A2_A3_authenticated_frames_commit_before_fact_and_reply(self):
        for actor, family, code in ((CD, 'system_deploy', 'deploy_failed'), (BACKUP, 'system_backup', 'backup_failed'), (BOT, 'bot_messaging', 'registration_failed')):
            with self.subTest(actor=actor):
                peer = await authenticate(self.server, actor)
                frame = producer_frame(actor)
                recorder = self.recorder
                original_emit = recorder.emit
                async def committed_emit(fact, principal=None):
                    rows = await self.notify._db.call('list_notifications')
                    self.assertTrue(any(row['dedup_key'] == frame['dedup_key'] for row in rows))
                    return await original_emit(fact, principal)
                recorder.emit = committed_emit
                before = len(recorder.calls)
                first = await dispatch(self.server, peer, frame)
                self.assertEqual(first['type'], 'notification.create.ok')
                self.assertEqual(len(recorder.calls), before + 1)
                fact, principal = recorder.calls[-1]
                self.assertEqual((fact.family, fact.code, fact.condition, principal), (family, code, 'active', 'system:' + actor))
                keys = ('step', 'operation_id') if actor == BOT else ('dedup_key',)
                fields = {'step': 'registration', 'operation_id': OPERATION} if actor == BOT else frame
                self.assertEqual(fact.episode_id, expected_episode(code, fields, keys))
                self.assertEqual(fact.stage, 'callback_timeout' if actor == BOT else None)
                replay = await dispatch(self.server, peer, frame)
                self.assertEqual(replay['notification']['notification_id'], first['notification']['notification_id'])
                self.assertEqual(recorder.calls[-1], (fact, principal))
                self.assertEqual(len(recorder.calls), before + 2)
                changed = {**frame, 'dedup_key': frame['dedup_key'].replace(OPERATION, OPERATION[:-1] + '2').replace('2026-01-15', '2026-01-16')}
                recorder.emit = original_emit
                self.assertEqual((await dispatch(self.server, peer, changed))['type'], 'notification.create.ok')
                self.assertNotEqual(recorder.calls[-1][0].episode_id, fact.episode_id)

    async def test_A4_quiet_system_frames_and_unauthenticated_notify(self):
        for actor in (CD, BACKUP):
            peer = await authenticate(self.server, actor)
            for severity in ('warning', 'info'):
                result = await dispatch(self.server, peer, producer_frame(actor, severity=severity))
                self.assertEqual(result['type'], 'notification.create.ok' if actor == CD or severity == 'warning' else 'notification.create.error')
            if actor == CD:
                for dedup in ('census|fn-example|invariant-example|2026-01-15', 'infra-census|fn-example|2026-01-15'):
                    self.assertEqual((await dispatch(self.server, peer, producer_frame(actor, dedup_key=dedup)))['type'], 'notification.create.ok')
        self.assertEqual(self.recorder.calls, [])
        frame = producer_frame(CD)
        self.assertEqual((await self.notify.notification(frame))['type'], 'notification.create.ok')
        self.assertEqual(self.recorder.calls, [])

    async def test_A5_sink_unavailable_or_raising_does_not_change_ok(self):
        for actor in (CD, BACKUP, BOT):
            peer = await authenticate(self.server, actor)
            for failure in ('none', 'raise', 'alerts_none'):
                if failure == 'alerts_none':
                    self.notify.alerts = None
                else:
                    self.notify.alerts = self.alerts
                    self.alerts.sink = None if failure == 'none' else self.recorder
                    if failure == 'raise':
                        async def fail(fact, principal=None):
                            raise RuntimeError('synthetic-private-exception')
                        self.recorder.emit = fail
                with self.assertLogs('chat_streamd_v2.notify', level='WARNING') as logs:
                    result = await dispatch(self.server, peer, producer_frame(actor))
                self.assertEqual(result['type'], 'notification.create.ok')
                self.assertTrue(any('sink_' in line for line in logs.output))
                self.assertNotIn('synthetic-private-exception', '\n'.join(logs.output))

    async def test_A6_fact_privacy(self):
        sentinel = 'PRIVATE-SYNTHETIC-/path-https://example.invalid'
        for actor in (CD, BACKUP, BOT):
            peer = await authenticate(self.server, actor)
            fields = {} if actor == BOT else {'title': sentinel, 'body': sentinel}
            self.assertEqual((await dispatch(self.server, peer, producer_frame(actor, **fields)))['type'], 'notification.create.ok')
            rendered = repr(self.recorder.calls[-1])
            for private in (sentinel, TOKENS[actor], producer_frame(actor)['title'], producer_frame(actor).get('body', 'never-present')):
                self.assertNotIn(private, rendered)

    async def test_A7_bot_payload_and_verb_denials(self):
        peer = await authenticate(self.server, BOT)
        patches = [
            {'title': 'wrong'}, {'severity': 'warning'}, {'severity': 'info'}, {'severity': []},
            {'body': ''}, {'body': None}, {'destination': 'pentacle-updates'}, {'extra': 'field'},
            {'_auth_context': {'service_authenticated': True}}, {'producer': CD},
            {'actions': None}, {'actions': [{'kind': 'ack'}]},
            {'dedup_key': None}, {'dedup_key': []}, {'dedup_key': 'bot-messaging|registration|callback_timeout|bad'},
            {'dedup_key': f'bot-messaging|registration|unknown|{OPERATION}|2026-01-15'},
            {'dedup_key': f'bot-messaging|claim|unknown|{OPERATION}'},
            {'dedup_key': f'bot-messaging|registration|secret_status|{OPERATION}'},
            {'dedup_key': f'bot-messaging|registration|unknown|{OPERATION.replace("4000", "3000")}'},
        ]
        for fields in patches:
            with self.subTest(fields=fields):
                result = await dispatch(self.server, peer, producer_frame(BOT, **fields))
                self.assertEqual(result['error_code'], 'system_producer_payload_invalid')
        for verb in ('tell', 'spawn', 'notification.list', 'notification.resolve', 'unknown.synthetic.verb'):
            self.assertEqual((await dispatch(self.server, peer, producer_frame(BOT, type=verb)))['error_code'], 'system_producer_forbidden')
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(await self.notify._db.call('list_notifications'), [])

    async def test_A7_bot_missing_unsafe_and_shared_file_tokens_fail_closed(self):
        path = self.paths[BOT]
        for state in ('unset', 'removed', 'empty', 'insecure', 'shared_cd', 'shared_backup'):
            with self.subTest(state=state):
                path.write_text(TOKENS[BOT]); path.chmod(0o600)
                os.environ[TOKEN_ENVS[BOT]] = str(path)
                token = TOKENS[BOT]
                if state == 'unset': del os.environ[TOKEN_ENVS[BOT]]
                elif state == 'removed': path.unlink()
                elif state == 'empty': path.write_text('')
                elif state == 'insecure': path.chmod(0o644)
                else:
                    token = TOKENS[CD if state == 'shared_cd' else BACKUP]
                    path.write_text(token)
                hello = dict(type='hello', request_id='hello', from_stream_id=BOT, stream_token=token,
                             subscribe={'snapshot': False, 'mode': 'rpc'})
                self.assertEqual((await dispatch(Server(), Peer(), hello))['error_code'], 'system_producer_auth_required')

    async def test_A7_bot_rotation_revokes_bound_peer_and_accepts_new_token(self):
        peer = await authenticate(self.server, BOT)
        self.paths[BOT].write_text('synthetic-rotated-bot-token')
        for frame in (producer_frame(BOT), {k: v for k, v in producer_frame(BOT).items() if k != 'stream_token'}):
            self.assertEqual((await dispatch(self.server, peer, frame))['error_code'], 'system_producer_auth_required')
        fresh = await authenticate(self.server, BOT, 'synthetic-rotated-bot-token')
        self.assertEqual((await dispatch(self.server, fresh, producer_frame(BOT, stream_token='synthetic-rotated-bot-token')))['type'], 'notification.create.ok')

    async def test_A7_delivery_step_and_absent_actions_are_accepted(self):
        peer = await authenticate(self.server, BOT)
        frame = producer_frame(BOT, dedup_key=f'bot-messaging|delivery|handoff_failed|{OPERATION}')
        del frame['actions']
        self.assertEqual((await dispatch(self.server, peer, frame))['type'], 'notification.create.ok')
        fact, principal = self.recorder.calls[-1]
        self.assertEqual((fact.code, fact.stage, principal), ('delivery_failed', 'handoff_failed', 'system:' + BOT))

    async def test_A7_bot_privileged_dedup_collision_is_refused(self):
        await self.notify._db.call('create_notification', producer=BOT, title='privileged preimage',
            dedup_key=producer_frame(BOT)['dedup_key'], actions=[{'kind': 'ack'}])
        peer = await authenticate(self.server, BOT)
        self.assertEqual((await dispatch(self.server, peer, producer_frame(BOT)))['error_code'], 'notification_invalid')
        self.assertEqual(self.recorder.calls, [])

    async def test_A8_reply_bytes_identical_with_and_without_adapters(self):
        from error_adapters import ADAPTERS
        for actor in (CD, BACKUP, BOT):
            replies = []
            for enabled in (False, True):
                notify = Notify(str(self.root / f'reply-{actor.replace(":", "_")}-{enabled}.db'), alerts=self.alerts)
                await notify.start()
                server = Server(); server.handlers.update(notify.wire_handlers())
                try:
                    peer = await authenticate(server, actor)
                    with patch('_shared.notifications_store._iso_now', return_value='2026-01-15T00:00:00Z'), patch('_shared.notifications_store.uuid.uuid4', return_value='00000000-0000-4000-8000-000000000099'):
                        if enabled:
                            reply = await dispatch(server, peer, producer_frame(actor))
                        else:
                            with patch.dict(ADAPTERS, {}, clear=True):
                                reply = await dispatch(server, peer, producer_frame(actor))
                    self.assertEqual(reply['type'], 'notification.create.ok')
                    replies.append(json.dumps(reply).encode())
                finally:
                    await notify.stop()
            self.assertEqual(*replies)


if __name__ == '__main__':
    unittest.main()
