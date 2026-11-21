"""Proactive notes survive live delivery retries and summary-mode reconnects."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import pytest

from tools.disposable_smoke import disposable_composite
from tools.proactive_note_probe import connect_client, history as read_history, note, prepare, receive


@asynccontextmanager
async def fixture():
    async with disposable_composite(operator_composite='operator-excluded:assistant') as harness:
        composite = await prepare(harness)
        async with AsyncExitStack() as stack:
            clients = [await connect_client(stack, harness, kind) for kind in ('pentacle', 'pentacle-mobile')]
            harness.update(clients=clients, stack=stack, composite=composite)
            yield harness
    assert not harness['state_path'].exists()
    assert harness['server']._ws_server is None


async def live(harness, message):
    frames = await asyncio.gather(*(receive(socket, lambda frame:
        frame.get('type') == 'chat.event' and frame.get('event', {}).get('message_id') ==
        'publication:' + message['request_id']) for socket in harness['clients']))
    assert frames[0]['event']['daemon_seq'] == frames[1]['event']['daemon_seq']
    assert all(frame['event']['text'] == message['message'] for frame in frames)
    return frames[0]['event']


async def history(harness, kind):
    socket = await connect_client(harness['stack'], harness, kind)
    return await read_history(socket, harness['target'], 'history-' + kind)


def test_every_note_type_reaches_web_mobile_and_reconnect_history():
    async def run():
        async with fixture() as h:
            pushes = []
            async def push(**payload):
                pushes.append(payload)
            h['composite'].reply_push = push
            published = []
            for kind in ('result', 'problem', 'reminder', 'relay', 'started', 'change', 'other'):
                message = note(h, 'normal-' + kind, kind,
                               **({'note_reason': 'Protocol coverage'} if kind == 'other' else {}))
                await h['rpc'](message)
                event = await live(h, message)
                assert event['note_type'] == kind
                published.append(event)
            assert pushes == []  # Proactive notes without response_state=final do not push.
            for client_kind in ('pentacle', 'pentacle-mobile'):
                events = await history(h, client_kind)
                for event in published:
                    assert [row for row in events if row.get('daemon_seq') == event['daemon_seq']] == [event]
    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['activity', 'broadcast'])
def test_retry_recovers_a_committed_note_after_interruption(interruption):
    async def run():
        async with fixture() as h:
            composite = h['composite']
            message = note(h, 'interrupted-' + interruption)
            attribute = 'refresh_activity' if interruption == 'activity' else 'broadcast'
            original = getattr(composite, attribute)
            async def fail(*args, **kwargs):
                raise RuntimeError('controlled publication interruption')
            setattr(composite, attribute, fail)
            try:
                with pytest.raises(RuntimeError):
                    await h['rpc'](message)
            finally:
                setattr(composite, attribute, original)
            persisted = [row for row in await h['store'].fetch_session_event_tail(h['target'], limit=100)
                         if row.get('message_id') == 'publication:' + message['request_id']]
            assert len(persisted) == 1
            if interruption == 'activity':
                assert (await live(h, message))['daemon_seq'] == persisted[0]['daemon_seq']
            receipt = await h['rpc'](message)
            assert receipt['duplicate'] is True
            assert (await live(h, message))['daemon_seq'] == persisted[0]['daemon_seq']
            for client_kind in ('pentacle', 'pentacle-mobile'):
                assert len([row for row in await history(h, client_kind)
                            if row.get('message_id') == persisted[0]['message_id']]) == 1
    asyncio.run(run())


def test_successful_retry_replays_same_identity_without_a_second_push():
    async def run():
        async with fixture() as h:
            pushes = []
            async def push(**payload):
                pushes.append(payload)
            h['composite'].reply_push = push
            message = note(h, 'final-note', response_state='final')
            await h['rpc'](message)
            first = await live(h, message)
            assert len(pushes) == 1
            assert (await h['rpc'](message))['duplicate'] is True
            assert await live(h, message) == first
            assert len(pushes) == 1
    asyncio.run(run())
