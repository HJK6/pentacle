"""Parked answer waits must not hold the notification mutation lock."""
import asyncio

import pytest

from notification_answer_fixture import fixture
from test_notify_answer_resolution import _seed_live_shaped_question


async def parked(notify, nid, count=1):
    async def registered():
        while len(notify._await_waiters.get(nid, ())) != count:
            await asyncio.sleep(0)
    await asyncio.wait_for(registered(), 2)


def wait_for_answer(notify, nid, **changes):
    return asyncio.create_task(notify.notification({
        'type': 'notification.await', 'notification_id': nid, 'timeout': 10, **changes}))


async def answer(notify, question, entrypoint='notification.resolve'):
    message = {'type': entrypoint, 'notification_id': question['notification_id'],
               'question_id': question['question_id'], 'selections': ['approve'],
               '_auth_context': {'operator_authenticated': True}}
    return await (notify.prompt(message) if entrypoint == 'prompt.answer'
                  else notify.notification(message))


@pytest.mark.parametrize('entrypoint', ['notification.resolve', 'prompt.answer'])
def test_parked_wait_does_not_block_either_answer_entrypoint(tmp_path, entrypoint):
    async def run():
        async with fixture(tmp_path) as (notify, *_):
            q = await _seed_live_shaped_question(notify)
            other = await _seed_live_shaped_question(notify, question_id='q-unrelated')
            task = wait_for_answer(notify, q['notification_id'])
            try:
                await parked(notify, q['notification_id'])
                unrelated = await asyncio.wait_for(answer(notify, other, entrypoint), 2)
                assert unrelated['type'] == entrypoint + '.ok'
                assert not task.done()
                resolved = await asyncio.wait_for(answer(notify, q, entrypoint), 2)
                waited = await asyncio.wait_for(task, 2)
                saved = await notify._db.call('get_notification', q['notification_id'])
                assert resolved['type'] == entrypoint + '.ok'
                assert waited['type'] == 'notification.await.ok'
                assert waited['answer'] == notify._answer_payload(saved)
                assert waited['answer']['selections'] == ['approve']
                assert not notify._await_waiters
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_multiple_waiters_cancel_and_timeout_independently(tmp_path):
    async def run():
        async with fixture(tmp_path) as (notify, *_):
            q = await _seed_live_shaped_question(notify)
            nid = q['notification_id']
            tasks = [wait_for_answer(notify, nid) for _ in range(3)]
            try:
                await parked(notify, nid, 3)
                tasks[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
                await parked(notify, nid, 2)
                timeout = await notify.notification({'type': 'notification.await',
                    'notification_id': nid, 'timeout': 0})
                assert timeout['type'] == 'notification.await.timeout'
                assert len(notify._await_waiters[nid]) == 2
                await asyncio.wait_for(answer(notify, q), 2)
                replies = await asyncio.wait_for(asyncio.gather(*tasks[1:]), 2)
                assert replies[0]['answer'] == replies[1]['answer']
                assert not notify._await_waiters
                replay = await notify.notification({'type': 'notification.await',
                    'notification_id': nid, 'timeout': 0})
                assert replay['answer'] == replies[0]['answer']
                assert not notify._await_waiters
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('changes,code', [
    ({'notification_id': ''}, 'notification_invalid'),
    ({'notification_id': 'missing'}, 'notification_not_found'),
    ({'timeout': -1}, 'notification_invalid'),
    ({'timeout': 'invalid'}, 'notification_invalid'),
])
def test_invalid_and_missing_await_preserve_errors_without_waiters(tmp_path, changes, code):
    async def run():
        async with fixture(tmp_path) as (notify, *_):
            q = await _seed_live_shaped_question(notify)
            result = await notify.notification({'type': 'notification.await',
                'notification_id': q['notification_id'], 'timeout': 0, **changes})
            assert result['error_code'] == code
            assert not notify._await_waiters
    asyncio.run(run())


def test_transfer_lock_still_serializes_setup_and_answer(tmp_path):
    async def run():
        async with fixture(tmp_path) as (notify, *_):
            q = await _seed_live_shaped_question(notify)
            async with notify._host_transfer_lock:
                waiter = wait_for_answer(notify, q['notification_id'])
                resolver = asyncio.create_task(answer(notify, q))
                # A bounded scheduling barrier proves neither operation crosses the lock.
                await asyncio.sleep(.02)
                assert not waiter.done() and not resolver.done()
                assert not notify._await_waiters
                saved = await notify._db.call('get_notification', q['notification_id'])
                assert saved['state'] == 'open'
            try:
                replies = await asyncio.wait_for(asyncio.gather(waiter, resolver), 2)
                assert replies[0]['type'] == 'notification.await.ok'
                assert replies[1]['type'] == 'notification.resolve.ok'
                assert not notify._await_waiters
            finally:
                waiter.cancel(); resolver.cancel()
                await asyncio.gather(waiter, resolver, return_exceptions=True)
    asyncio.run(run())


def test_answer_during_durable_read_is_not_lost(tmp_path):
    async def run():
        async with fixture(tmp_path) as (notify, *_):
            q = await _seed_live_shaped_question(notify)
            nid = q['notification_id']
            original = notify._db.call
            injected = False
            async def read_boundary(method, *args, **kwargs):
                nonlocal injected
                record = await original(method, *args, **kwargs)
                if method == 'get_notification' and args == (nid,) and not injected:
                    injected = True
                    # Preserve the real stale read, but deliver a real durable resolution
                    # before the caller resumes, as an independent recovery path can do.
                    resolved = await original('resolve_notification', nid,
                                              action_kind='yes_no', choice=True,
                                              selections=['approve'], by='test')
                    await notify._after_resolution(resolved)
                return record
            notify._db.call = read_boundary
            result = await notify.notification({'type': 'notification.await',
                'notification_id': nid, 'timeout': .5})
            assert result['type'] == 'notification.await.ok'
            saved = await original('get_notification', nid)
            assert result['answer'] == notify._answer_payload(saved)
            assert not notify._await_waiters
    asyncio.run(run())


def test_terminal_question_without_answer_still_times_out(tmp_path):
    async def run():
        async with fixture(tmp_path) as (notify, *_):
            q = await _seed_live_shaped_question(notify)
            await notify._db.call('resolve_notification', q['notification_id'],
                                  action_kind='resolved', by='test')
            result = await notify.notification({'type': 'notification.await',
                'notification_id': q['notification_id'], 'timeout': 0})
            assert result['type'] == 'notification.await.timeout'
            assert not notify._await_waiters
    asyncio.run(run())


def test_parked_wait_allows_transfer_and_successor_owned_answer(tmp_path):
    from test_assistant_recovery_command import fixture as transfer_fixture, OLD
    from test_host_transfer_authority import claim_bundle, prepare_bundle
    from test_question_contract_d3 import _ask

    async def run():
        async with transfer_fixture(tmp_path, authority=True, questions=True) as (
                store, server, composite, gateway, *_):
            asked = await server.notify.prompt(_ask('await-transfer', producer=OLD))
            assert asked['type'] == 'prompt.ask.ok'
            q = await server.notify._db.call('get_agent_question', 'await-transfer')
            row, *_ = await claim_bundle(store, server, gateway, composite)
            parts = await prepare_bundle(store, server, row)
            successor = next(part for part in parts if part['source'] == OLD)
            waiter = wait_for_answer(server.notify, q['notification_id'])
            try:
                await parked(server.notify, q['notification_id'])
                committed = await asyncio.wait_for(server.host_transfer.commit(row), 2)
                assert committed['state'] == 'committed'
                reply = await asyncio.wait_for(server.notify.notification({
                    'type': 'notification.resolve', 'notification_id': q['notification_id'],
                    'selections': ['yes'], '_auth_context': {'operator_authenticated': True}}), 2)
                assert reply['type'] == 'notification.resolve.ok', reply
                result = await asyncio.wait_for(waiter, 2)
                assert result['type'] == 'notification.await.ok'
                saved = await server.notify._db.call('get_agent_question', 'await-transfer')
                assert saved['producer_stream_id'] == successor['target']
                assert saved['producer_session_generation'] == successor['target_generation']
                assert result['answer']['selections'] == ['yes']
                assert not server.notify._await_waiters
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['cancel', 'store_error'])
def test_waiter_cleanup_when_durable_read_does_not_finish(tmp_path, failure):
    from notify import NotificationStoreError

    async def run():
        async with fixture(tmp_path) as (notify, *_):
            q = await _seed_live_shaped_question(notify)
            entered = asyncio.Event()
            original = notify._db.call
            async def read_boundary(method, *args, **kwargs):
                if method == 'get_notification' and args == (q['notification_id'],):
                    entered.set()
                    if failure == 'store_error':
                        raise NotificationStoreError('injected read failure')
                    await asyncio.Event().wait()
                return await original(method, *args, **kwargs)
            notify._db.call = read_boundary
            task = wait_for_answer(notify, q['notification_id'])
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if failure == 'cancel':
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    result = await asyncio.wait_for(task, 2)
                    assert result['error_code'] == 'notification_store_error'
                assert not notify._await_waiters
                assert not notify._host_transfer_lock.locked()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
