"""Drive the reconciler callback against a real Store and a fake clock."""
import asyncio
import json

from queued_send_now import PREFIX, QueuedSendNow
from test_queued_send_now import GEN, Harness, SID, finish, intake, message, setup, settle, signal


def track_scans(h, monkeypatch):
    submitted = []
    submit = h.store.submit

    async def tracked(operation, **kwargs):
        submitted.append(operation.__name__)
        return await submit(operation, **kwargs)

    monkeypatch.setattr(h.store, 'submit', tracked)
    h.controller.clock = lambda: h.now
    return submitted


def test_idle_ticks_skip_store_after_first_tick(monkeypatch):
    async def run():
        h = Harness()
        h.controller = QueuedSendNow(h.store, h.sessions, h.comms, h.tmux)
        submitted = track_scans(h, monkeypatch)
        try:
            await h.controller.tick()
            assert submitted == ['pending']
            h.now += 1
            await h.controller.tick()
            assert submitted == ['pending']
        finally:
            await finish(h)

    asyncio.run(run())


def test_idle_hour_scans_at_most_120_times(monkeypatch):
    async def run():
        h = Harness()
        h.controller = QueuedSendNow(h.store, h.sessions, h.comms, h.tmux)
        submitted = track_scans(h, monkeypatch)
        try:
            for _ in range(3600):
                await h.controller.tick()
                h.now += 1
            assert submitted.count('pending') <= 120
        finally:
            await finish(h)

    asyncio.run(run())


def test_bind_queued_receipt_wakes_the_next_tick(monkeypatch):
    async def run():
        h = await setup()
        submitted = track_scans(h, monkeypatch)
        try:
            await h.controller.tick()
            submitted.clear()
            h.now += 1
            _, created = await h.controller.bind_receipt_locked(SID, GEN, 'another', 'another-receipt')
            assert created
            await h.controller.tick()
            assert submitted.count('pending') == 1
            await h.controller.tick()
            assert submitted.count('pending') == 1
        finally:
            await finish(h)

    asyncio.run(run())


def test_continue_retry_scans_at_the_durable_deadline(monkeypatch):
    async def run():
        h = await setup()
        submitted = track_scans(h, monkeypatch)
        try:
            await h.controller.send(message())
            await signal(h, 'turn_aborted')
            await intake(h)
            h.tmux.screen = 'OpenAI Codex\n› \n  gpt-6-test high'
            attempts = []
            send = h.comms.send

            async def transient(msg):
                attempts.append(msg['request_id'])
                if len(attempts) == 1:
                    return {'delivery': 'not_landed', 'reason': 'queued_continue_cancelled'}
                return await send(msg)

            monkeypatch.setattr(h.comms, 'send', transient)
            await settle(h)
            key = h.controller.operation_key(SID, GEN, 'selected')
            operation, _ = await h.controller._record(key)
            assert operation['state'] == 'continue_retry_wait'
            submitted.clear()
            h.now = operation['next_retry_at'] - 1
            await h.controller.tick()
            assert submitted == []
            assert h.tmux.pastes == []
            h.now = operation['next_retry_at']
            await settle(h)
            assert h.tmux.pastes == ['continue']
            assert attempts == [operation['continue_request_id']] * 2
        finally:
            await finish(h)

    asyncio.run(run())


def test_fallback_finds_foreign_dispatch_claim_without_a_wake(monkeypatch):
    async def run():
        h = await setup(provider='claude')
        submitted = track_scans(h, monkeypatch)
        try:
            await h.controller.tick()
            key = h.controller.operation_key(SID, GEN, 'selected')
            operation = {'key': key, 'stream_id': SID, 'generation': GEN,
                         'original_request_id': 'selected', 'state': 'dispatch_claimed',
                         'owner': 'stopped-owner', 'route': 'claude-native'}

            def external_write(conn):
                with conn:
                    conn.execute('UPDATE kv SET v=? WHERE k=?', (json.dumps(operation), PREFIX + key))

            await h.store.submit(external_write)
            submitted.clear()
            h.now += 29
            await h.controller.tick()
            assert submitted == []
            h.now += 1
            await h.controller.tick()
            saved, _ = await h.controller._record(key)
            assert saved['state'] == 'indeterminate'
            assert saved['reason'] == 'key_dispatch_crash'
        finally:
            await finish(h)

    asyncio.run(run())
