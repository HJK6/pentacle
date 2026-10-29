"""Queued control uses real durable Store, native normalization and Comms dedupe."""
import asyncio
import copy
import hashlib
import json
from unittest.mock import patch

import pytest
from queued_send_now import QueuedSendNow, PINS, native_turn_signal, route_for, claude_queue_composer_empty
from sessions import VerbError
from test_codex_capacity_retry import Harness, SID, normalized

GEN = 'test-generation'
async def qualified_source(_row):
    return {'active_turn_id': 'native-turn', 'source_session_id': 'native-capacity-fixture', 'file_id': [1, 2], 'watermark': 1}

AUTH = {'operator_authenticated': True, 'operator_principal': 'operator:fixture'}


async def setup(path=':memory:', provider='codex'):
    h = Harness(path)
    await h.store.open_session('fixture', 'v2-capacity', provider=provider,
        pane_pid='8123', pane_status='pane_alive', bootstrap_state='ready',
        observer_binding={'generation': GEN, 'pane_pid': '8123'}, session_generation=GEN,
        account_binding={'version': PINS[provider][0], 'executable_sha256': PINS[provider][1], 'executable': '/fixture-only'})
    await h.open(reopen=True)
    h.controller = QueuedSendNow(h.store, h.sessions, h.comms, h.tmux,
        source_snapshot=qualified_source, owner='owner-1', verify_executable=lambda row: asyncio.sleep(0, result=True))
    h.comms.queued_send_now = h.controller
    # Deterministically exercise reconciliation, independent of background scheduling.
    h.controller.request_tick = lambda: None
    h.tmux.screen = ('OpenAI Codex\n• Messages to be submitted after next tool call\n  selected input\n› \n  gpt-6-test high'
        if provider == 'codex' else 'Claude Code\n❯ selected input\n  ctrl+x ctrl+s to send now\n\n❯ \n')
    await h.store.append_send_receipt(to_stream_id=SID, request_id='selected', receipt_id='receipt',
        state='accepted', wire_text='selected input', display_text='selected input', attachments=[],
        delivery='landed', submission_confirmed=False, optimistic_id='optimistic', reason='provider_queued',
        actor_stream_id='operator:fixture', actor_trusted=True)
    await signal(h, 'task_started')
    await h.controller.bind_receipt_locked(SID, GEN, 'selected', 'receipt')
    return h


def message(**changes):
    return {'host': 'fixture', 'session_name': 'v2-capacity', 'request_id': 'control-1',
        'expected_session_generation': GEN,
        'queued_send_now': {'original_request_id': 'selected', 'optimistic_id': 'optimistic'},
        '_auth_context': AUTH, **changes}


async def signal(h, state, *, turn='native-turn', reason='interrupted'):
    h.index += 1
    e = normalized({'type': state, 'turn_id': turn, 'reason': reason}, h.now)
    e['raw']['ordinal'] = h.index
    await h.append(e)
    return e


async def intake(h, request='selected'):
    h.index += 1
    e = normalized({'type': 'message', 'id': 'intake-'+str(h.index), 'role': 'user',
        'content': [{'type': 'input_text', 'text': 'selected input' if request == 'selected' else 'continue'}]}, h.now, record_type='response_item')
    e['request_id'] = request
    await h.append(e)


async def settle(h):
    await h.controller.tick()
    if h.controller.jobs:
        await asyncio.gather(*list(h.controller.jobs.values()))
    await h.controller.tick()


async def finish(h):
    await h.controller.close()
    h.close()


@pytest.mark.parametrize('provider,keys', [('claude', ('C-x', 'C-s')), ('codex', ('Escape',))])
def test_once_only_keys_repeated_click_and_no_original_repaste(provider, keys):
    async def run():
        h = await setup(provider=provider)
        try:
            with patch('queued_send_now.claude_composer_empty', return_value=True):
                first = await h.controller.send(message())
                for n in range(5):
                    h.now += 61
                    result = await h.controller.send(message(request_id='retry-'+str(n)))
                    assert result['coalesced']
            assert h.tmux.keys == [('send-keys', '-t', '=v2-capacity:', *keys)]
            assert h.tmux.pastes == []
            assert first['landed'] is False
        finally:
            await finish(h)
    asyncio.run(run())


def test_already_consumed_receipt_never_interrupts_even_if_ui_proof_delayed():
    async def run():
        h = await setup()
        try:
            await intake(h)
            reply = await h.controller.send(message())
            assert reply['coalesced'] and h.tmux.keys == [] and h.tmux.pastes == []
        finally:
            await finish(h)
    asyncio.run(run())


@pytest.mark.parametrize('invalid', ['actor', 'generation', 'identity', 'unqualified', 'no-queue', 'no-turn'])
def test_denial_and_uncertain_native_ownership_never_dispatch(invalid):
    async def run():
        h = await setup()
        try:
            msg = message()
            if invalid == 'actor': msg['_auth_context'] = {**AUTH, 'operator_principal': 'operator:other'}
            if invalid == 'generation': msg['expected_session_generation'] = 'old'
            if invalid == 'identity': msg['queued_send_now'] = {'original_request_id': 'selected', 'optimistic_id': 'other'}
            if invalid == 'unqualified': h.controller.verify_executable = lambda row: asyncio.sleep(0, result=False)
            if invalid == 'no-queue': h.tmux.screen = 'OpenAI Codex\n› \n gpt-6-test high'
            if invalid == 'no-turn': await signal(h, 'task_complete')
            with pytest.raises(VerbError): await h.controller.send(msg)
            assert h.tmux.keys == [] and h.tmux.pastes == []
        finally:
            await finish(h)
    asyncio.run(run())


def test_codex_exact_abort_then_native_intake_then_continue_uses_real_send_dedupe(tmp_path):
    async def run():
        (tmp_path/'negative').mkdir()
        (tmp_path/'positive').mkdir()
        h = await setup(str(tmp_path/'negative'/'sessions.db'))
        try:
            await h.controller.send(message())
            await signal(h, 'turn_aborted', turn='wrong')
            await settle(h); assert h.tmux.pastes == []
            await signal(h, 'turn_aborted', reason='other')
            await settle(h); assert h.tmux.pastes == []
            # Distinct stable native identity for the causal abort after the controls.
            # The wrong-reason event above occupies the same native turn slot;
            # use a new valid turn/operation for independent positive acceptance.
        finally:
            await finish(h)
        h = await setup(str(tmp_path/'positive'/'sessions.db'))
        try:
            await h.controller.send(message())
            abort = await signal(h, 'turn_aborted')
            await settle(h); assert h.tmux.pastes == []
            await intake(h)
            h.tmux.screen = 'OpenAI Codex\n› \n  gpt-6-test high'
            await settle(h)
            assert h.tmux.pastes == ['continue']
            records = await h.store.submit(lambda c: [json.loads(r[0]) for r in c.execute("SELECT v FROM kv WHERE k LIKE 'queued-send-now.operation.%'")])
            op = records[0]
            assert op['state'] == 'continue_pending_proof'
            # Simulate lost continuation ACK and a daemon restart. Exact-ID
            # replay traverses real Comms and never emits a second native input.
            await h.controller.close()
            h.controller = QueuedSendNow(h.store, h.sessions, h.comms, h.tmux,
                source_snapshot=qualified_source, owner='owner-2', verify_executable=lambda row: asyncio.sleep(0, result=True))
            h.controller.request_tick = lambda: None
            h.comms.queued_send_now = h.controller
            await h.controller._record(op['key'], update={'state': 'continue_claimed'})
            h.now += 61
            await settle(h)
            assert h.tmux.pastes == ['continue']
            await intake(h, op['continue_request_id'])
            await settle(h)
            saved, _ = await h.controller._record(op['key'])
            assert saved['state'] == 'completed'
            await h.append(abort); await settle(h)
            assert h.tmux.pastes == ['continue']
            assert len(h.tmux.keys) == 1
        finally:
            await finish(h)
    asyncio.run(run())


def test_stop_or_generation_replacement_cancels_before_continuation():
    async def run():
        for replace in (False, True):
            h = await setup()
            try:
                await h.controller.send(message())
                await signal(h, 'turn_aborted'); await intake(h)
                if replace: await h.store.update_session('fixture', 'v2-capacity', status='closed')
                else:
                    async with h.sessions._lifecycle_lock('fixture', 'v2-capacity'), h.comms._pane_input_lock(SID):
                        await h.controller.cancel_locked(SID, GEN)
                await settle(h)
                assert h.tmux.pastes == []
            finally:
                await finish(h)
    asyncio.run(run())


def test_normalized_signal_is_typed_and_user_text_never_arms_resume():
    e = normalized({'type': 'turn_aborted', 'turn_id': 'turn', 'reason': 'interrupted'}, 1)
    e['daemon_seq'] = 1
    assert native_turn_signal(e)['turn_id'] == 'turn'
    assert e['text'] == ''
    for change in [{'kind': 'USER'}, {'provider': 'claude'}, {'raw': {'subtype': 'provider-turn-control'}}]:
        assert native_turn_signal({**e, **change}) is None


def test_receipt_from_previous_generation_cannot_control_reused_same_second_name():
    async def run():
        h = await setup()
        try:
            old = await h.store.fetch_session('fixture', 'v2-capacity')
            await h.store.open_session('fixture', 'v2-capacity', provider='codex',
                pane_pid='8123', pane_status='pane_alive', bootstrap_state='ready',
                session_generation='replacement', created_at=old['created_at'], account_binding=old['account_binding'])
            await h.sessions.refresh()
            # Matching text/chrome and a fresh generation supplied by a stale
            # caller cannot transfer an old receipt to the replacement pane.
            with pytest.raises(VerbError):
                await h.controller.send(message(expected_session_generation='replacement'))
            assert h.tmux.keys == []
        finally:
            await finish(h)
    asyncio.run(run())


def test_native_dim_queue_placeholder_is_not_typed_identical_text():
    hint = 'Press up to edit queued messages'
    suffix = '\n─────────\npaste again to expand\n'
    styled = '❯\xa0\x1b[2m\x1b[39m' + hint + '\x1b[0m' + suffix
    assert claude_queue_composer_empty(styled)
    assert claude_queue_composer_empty(styled.replace('paste again to expand', 'paste again to expand   Ctrl+Y to paste deleted text'))
    assert not claude_queue_composer_empty('❯ ' + hint + suffix)
    assert not claude_queue_composer_empty('❯ \x1b[38;5;2m' + hint + suffix)
    assert not claude_queue_composer_empty('❯ unrelated draft' + suffix)
    assert not claude_queue_composer_empty('❯\nrelated multiline draft' + suffix)
    assert not claude_queue_composer_empty('❯\nunknown native footer')


def test_shutdown_and_delayed_pre_dispatch_native_abort_cannot_resume():
    async def run():
        h = await setup()
        try:
            await h.controller.send(message())
            e = normalized({'type': 'turn_aborted', 'turn_id': 'native-turn', 'reason': 'interrupted'}, h.now)
            e['raw']['ordinal'] = 0  # older source record delivered late
            await h.append(e); await intake(h)
            await settle(h); assert h.tmux.pastes == []
            await h.controller.close()
            await h.controller.tick()
            assert h.tmux.pastes == []
        finally:
            await finish(h)
    asyncio.run(run())


def test_native_intake_ahead_of_ingest_never_dispatches_a_late_escape():
    async def run():
        h = await setup()
        try:
            async def ahead(row):
                source = await qualified_source(row)
                source['input_candidates'] = [{'wire_digest': hashlib.sha256(b'selected input').hexdigest(), 'timestamp': '9999-01-01T00:00:00Z'}]
                return source
            h.controller.source_snapshot = ahead
            with pytest.raises(VerbError): await h.controller.send(message())
            assert h.tmux.keys == []
        finally:
            await finish(h)
    asyncio.run(run())


def test_uncertain_key_crash_reconciles_native_abort_without_repeating_the_key():
    async def run():
        h = await setup()
        try:
            original = h.tmux.run
            async def lost_ack(*args, **kwargs):
                await original(*args, **kwargs)
                raise RuntimeError('synthetic uncertainty after possible key')
            h.tmux.run = lost_ack
            with pytest.raises(RuntimeError): await h.controller.send(message())
            assert len(h.tmux.keys) == 1
            reply = await h.controller.send(message())
            assert reply['error_code'] == 'queued_send_now_indeterminate'
            await h.controller.close()
            h.controller = QueuedSendNow(h.store, h.sessions, h.comms, h.tmux,
                source_snapshot=qualified_source, owner='after-crash', verify_executable=lambda row: asyncio.sleep(0, result=True))
            h.controller.request_tick = lambda: None
            h.comms.queued_send_now = h.controller
            await settle(h); assert h.tmux.pastes == []
            await signal(h, 'turn_aborted'); await intake(h)
            h.tmux.run = original
            h.tmux.screen = 'OpenAI Codex\n› \n  gpt-6-test high'
            await settle(h); await settle(h)
            assert h.tmux.pastes == ['continue']
            assert len(h.tmux.keys) == 1
        finally:
            await finish(h)
    asyncio.run(run())
