"""Front-desk authority boundary cases from final-candidate QA cycle 1."""
import asyncio
import json
import pytest

from test_assistant_composite_addressing import _run, SEAT_A, SEAT_B, SENDER, HOST, _refused
from outbound_notices import OutboundNoticeQueue, OutboundNoticeConfig
from comms import payload_digest


def test_legacy_receipt_cannot_be_claimed_by_another_sender(tmp_path):
    async def body(h):
        # Real pre-upgrade physical delivery retains its sender.
        await h.server.comms.tell({'to_stream_id': SEAT_A, 'text': 'legacy body',
            'from_stream_id': SENDER, 'tell_id': 'legacy-sender'})
        prior = await h.store.get_tell_delivery('legacy-sender')
        assert prior['payload_digest'] == payload_digest(SEAT_A, 'legacy body')
        assert prior['delivery']['from_stream_id'] == SENDER
        await h.move_to_b()
        await _refused(h.server._on_tell({'to_stream_id': SEAT_A, 'text': 'legacy body',
            'from_stream_id': SEAT_B, 'tell_id': 'legacy-sender', '_auth_context': {
                'token_verified': True, 'stream_id': SEAT_B, 'session_generation': h.gen[SEAT_B]}}),
            'tell_id_conflict')
    _run(tmp_path, body, front_desk=True)


def test_secondary_assistant_history_does_not_become_primary_alias(tmp_path):
    async def body(h):
        generations = {}
        for name in ('secondary-a', 'secondary-b'):
            row = await h.sessions.open(HOST, name, provider='claude', role='lead',
                pane_status='pane_alive', effective_model='fixture-model', effective_effort='medium')
            generations[name] = row['session_generation']
        await h.store.rebind_assistant(name='secondary',
            env_binding={'stream_id': HOST+':secondary-a', 'generation': generations['secondary-a']},
            actor_stream_id=HOST+':secondary-a', actor_generation=generations['secondary-a'],
            target_stream_id=HOST+':secondary-b', target_generation=generations['secondary-b'],
            request_id='secondary-rebind', expected_revision=0)
        aliases = await h.store.front_desk_aliases()
        await h.tell(to_stream_id=HOST+':secondary-a', tell_id='secondary-history')
        assert h.inputs_at(SEAT_A) == 0, {'aliases': sorted(aliases), 'inputs': h.tmux.inputs}
        assert HOST+':secondary-a' not in aliases
    _run(tmp_path, body, front_desk=True)


def test_never_attempted_ruling_result_follows_same_stream_new_generation(tmp_path):
    async def body(h):
        queue = OutboundNoticeQueue(h.store, h.comms,
            config=OutboundNoticeConfig(lease_s=.1, max_attempts=2))
        await queue.enqueue(kind='assistant_lane_ruling_result', dedupe_key='restore-result',
            recipient_stream_id=SEAT_A, body='[Assistant lane ruling] {"event":"authority_lost"}',
            tell_id='restore-result', source_stream_id=SENDER,
            metadata={'authority_generation': h.gen[SEAT_A]})
        row = await h.store.open_session(HOST, 'seat-a', provider='claude', pane_status='pane_alive')
        assert row['session_generation'] != h.gen[SEAT_A]
        await h.store.recover_assistant_binding(name=h.composite.env_config.name,
            target_stream_id=SEAT_A, target_generation=row['session_generation'])
        await h.composite.load_binding()
        await h.sessions.refresh()
        await queue.drain_once(force=True)
        notice = await h.store.submit(lambda c: dict(c.execute(
            "SELECT * FROM v2_outbound_notices WHERE notice_id='restore-result'").fetchone()))
        # Routed to the restored generation; this fixture cannot prove
        # submission, so delivery stays evidence-pending, never terminal.
        assert json.loads(notice['metadata'])['authority_generation'] == row['session_generation'], notice
        assert notice['terminal_at'] is None, notice
        assert h.inputs_at(SEAT_A) == 1
    _run(tmp_path, body, front_desk=True)


def test_stale_fd_cannot_configure_advisor_after_waiting_for_lock(tmp_path):
    async def body(h):
        rulings = h.server.lane_rulings
        await rulings._authority_lock.acquire()
        task = asyncio.create_task(h.server._on_assistant_authority({'action': 'set', 'value': 'disabled',
            '_auth_context': {'token_verified': True, 'stream_id': SEAT_A,
                'session_generation': h.gen[SEAT_A]}}))
        # configure() checks the old principal before waiting for this lock.
        for _ in range(100):
            await asyncio.sleep(0)
            if rulings._authority_lock._waiters:
                break
        assert rulings._authority_lock._waiters
        await h.move_to_b()
        rulings._authority_lock.release()
        await _refused(task, 'assistant_authority_config_unauthorized')
    _run(tmp_path, body, front_desk=True)


def test_fd_own_child_with_pending_disposition_uses_fd_custody():
    from test_fd_close_visible import setup, close, FD
    from test_lifecycle_authority import scenario, HOST as LHOST
    from test_work_unattended import cached
    async def body(env):
        auth = await setup(env)
        await cached(env.store)
        await env.store.update_session(LHOST, 'target', role='lead', assigned_spec_id='spec_demo__item_00',
            parent_stream_id=FD)
        await env.sessions.refresh()
        # Ordinary FD own-child closure follows the owner disposition guard.
        await _refused(env.server._on_close(close(auth)), 'lead_close_requires_disposition')
        assert env.tmux.killed == []
        assert (await env.store.fetch_session(LHOST, 'target'))['status'] == 'open'
        # Explicit recovery of this FD's proven busy child retains FD debt.
        env.tmux.working.add('target')
        result = await env.server._on_close(close(auth, force=True, request_id='fd-busy-recovery'))
        assert result['type'] == 'close.ok', result
        assert env.tmux.killed == ['target']
        owed = await env.store.work_owed()
        assert [(item['spec_id'], item['closing_stream_id']) for item in owed['items']] == [
            ('spec_demo__item_00', LHOST + ':target')]
        from store_work_index import _state_conn
        state = await env.store.submit(_state_conn)
        debt, = state['_lead_dispositions']['owed'].values()
        assert debt['front_desk_owned'] is True
    scenario(body)


def test_superseded_ruling_emits_exactly_one_notification():
    from test_approved_close_retry import journey
    from test_assistant_lane_rulings import _fd_swap
    async def body():
        async with journey() as env:
            env.sessions.assistant.front_desk = env.server.assistant_composite
            await env.request()
            await _fd_swap(env)
            await env.approve()
            notices = await env.store.submit(lambda c: [dict(r) for r in c.execute(
                "SELECT notice_id,body FROM v2_outbound_notices WHERE kind='assistant_lane_ruling_result'")])
            assert len(notices) == 1, notices
    asyncio.run(body())


def test_unavailable_fd_does_not_deliver_report_to_old_open_fd(tmp_path):
    async def body(h):
        queue = OutboundNoticeQueue(h.store, h.comms,
            config=OutboundNoticeConfig(lease_s=.1, max_attempts=2))
        await queue.enqueue(kind='report', dedupe_key='unavailable-report', recipient_stream_id=SEAT_A,
            body='[child_report_ready] fixture', tell_id='unavailable-report', source_stream_id=SENDER,
            metadata={'report_id': 'fixture'})
        await h.move_to_b()
        await h.store.update_session(HOST, 'seat-b', status='closed')
        await queue.drain_once(force=True)
        assert h.inputs_at(SEAT_A) == 0, h.tmux.inputs
    _run(tmp_path, body, front_desk=True)


def test_alias_send_swap_after_fence_before_input_is_refused(tmp_path):
    async def body(h):
        ordinary = h.comms._assert_claude_send_ready
        swapped = False
        async def swap_after_fence(route):
            nonlocal swapped
            await ordinary(route)
            if not swapped:
                swapped = True
                await h.move_to_b()
        h.comms._assert_claude_send_ready = swap_after_fence
        result = await h.send(request_id='swap-after-fence')
        assert result['delivery'] == 'not_landed', result
        assert result['reason'] == 'assistant_binding_stale', result
        assert h.inputs_at(SEAT_A) == 0
    _run(tmp_path, body, front_desk=True)


def test_ruling_cannot_execute_after_fd_swap_between_check_and_effect():
    from test_approved_close_retry import journey
    from test_assistant_lane_rulings import _fd_swap
    async def body():
        async with journey() as env:
            env.sessions.assistant.front_desk = env.server.assistant_composite
            await env.request()
            original = env.rulings._fd_requester_superseded
            swaps = []
            async def swap_after_check(rid, current):
                superseded = await original(rid, current)
                assert superseded is False
                # A binding change racing the release waits for its effect.
                swaps.append(asyncio.create_task(_fd_swap(env)))
                for _ in range(20):
                    await asyncio.sleep(0)
                assert not swaps[0].done()
                return superseded
            env.rulings._fd_requester_superseded = swap_after_check
            kill = env.tmux.kill_session
            bound_at_kill = []
            async def recording_kill(*a, **k):
                bound_at_kill.append(env.server.assistant_composite.config.direct_primary_stream_id)
                return await kill(*a, **k)
            env.tmux.kill_session = recording_kill
            env.tmux.text = 'ready\n'
            await env.approve()
            await swaps[0]
            # The close was admitted for the FD that was current at its fence.
            assert env.tmux.kills == 1 and bound_at_kill and not bound_at_kill[0].endswith(':desk-b')
            assert env.server.assistant_composite.config.direct_primary_stream_id.endswith(':desk-b')
    asyncio.run(body())


def test_fd_own_child_operator_lane_is_still_protected():
    from test_fd_close_visible import setup, close, lane, FD
    from test_lifecycle_authority import scenario, HOST as LHOST
    async def body(env):
        auth = await setup(env)
        await env.store.update_session(LHOST, 'target', parent_stream_id=FD)
        await lane(env)
        await env.sessions.refresh()
        await _refused(env.server._on_close(close(auth, operator_confirm=True, operator_override=True)),
            'close_operator_lane_protected')
        assert env.tmux.killed == []
    scenario(body)


def test_alias_send_swap_at_attempt_entry_waits_for_input(tmp_path):
    async def body(h):
        original = h.comms._attempt_send_delivery
        swaps = []
        async def swap_before_attempt(plan):
            # A concurrent rebind cannot commit between the binding check and
            # physical input; it lands after the paste.
            swaps.append(asyncio.create_task(h.move_to_b()))
            for _ in range(20):
                await asyncio.sleep(0)
            assert not swaps[0].done()
            return await original(plan)
        h.comms._attempt_send_delivery = swap_before_attempt
        result = await h.send(request_id='swap-at-attempt-entry')
        await swaps[0]
        # Committed into the seat bound at the check; proof readback may
        # finish after the rebind.
        assert result['delivery'] in {'landed', 'committed_pending_proof'}, result
        assert h.inputs_at(SEAT_A) == 1 and h.inputs_at(SEAT_B) == 0, {
            'result': result, 'inputs': h.tmux.inputs}
        assert (await h.store.lifecycle_authority_effective())['binding_stream_id'] == SEAT_B
        # The next alias send samples the new binding.
        h.comms._attempt_send_delivery = original
        after = await h.send(request_id='after-swap')
        assert h.inputs_at(SEAT_B) == 1 and h.inputs_at(SEAT_A) == 1, {'after': after, 'inputs': h.tmux.inputs}
    _run(tmp_path, body, front_desk=True)


# Scoped re-review (cycle 1, round 2) cases.
from test_fd_close_visible import setup, close, FD
from test_lifecycle_authority import scenario, HOST as LHOST, _until

@pytest.mark.parametrize('case,code', [('reason','lifecycle_reason_required'),('request','lifecycle_request_id_required')])
def test_fd_own_child_keeps_explicit_request_fences(case,code):
    async def run(env):
        auth=await setup(env)
        await env.store.update_session(LHOST,'target',parent_stream_id=FD)
        await env.sessions.refresh()
        msg=close(auth)
        msg.pop('reason' if case=='reason' else 'request_id')
        await _refused(env.server._on_close(msg),code)
        assert env.tmux.killed == []
    scenario(run)

def test_fd_own_child_cannot_force_busy_target():
    async def run(env):
        auth=await setup(env)
        await env.store.update_session(LHOST,'target',parent_stream_id=FD)
        await env.sessions.refresh()
        env.tmux.working.add('target')
        result=await env.server._on_close(close(auth,operator_override=True,operator_confirm=True))
        assert env.tmux.killed == [], result
    scenario(run)

def test_fd_own_child_audit_failure_refuses_before_effect():
    import sqlite3
    async def run(env):
        auth=await setup(env)
        await env.store.update_session(LHOST,'target',parent_stream_id=FD)
        await env.sessions.refresh()
        async def broken(*a,**k): raise sqlite3.OperationalError('synthetic audit failure')
        env.store.lifecycle_authority_audit=broken
        with pytest.raises((sqlite3.OperationalError,Exception)):
            await env.server._on_close(close(auth))
        assert env.tmux.killed == []
    scenario(run)

def test_question_release_and_alias_send_do_not_invert_gate_lifecycle(tmp_path):
    from notify import Notify
    async def run(h):
        rulings=h.server.lane_rulings
        notify=Notify(str(tmp_path/'notifications.db'),sessions=h.sessions,notice_store=h.store,lane_rulings=rulings)
        h.server.notify=notify
        await notify.start()
        tasks=[]
        paused,proceed=asyncio.Event(),asyncio.Event()
        try:
            frozen={'envelope':{'schema_version':1,'question_id':'repair-lock-order','title':'Fixture','body':'Fixture question',
                'dedup_key':'repair-lock-order','producer_stream_id':SEAT_A,'producer_session_generation':h.gen[SEAT_A],
                'response_mode':'free_text','options':[]},'actions':[],'severity':'info'}
            row=await rulings._create(action='question',key='repair-lock-order',intent=frozen,requester=SEAT_A,
                requester_generation=h.gen[SEAT_A],binding=await rulings.binding(),target=SEAT_A,target_generation='',covered=True)
            rid=row['ruling_request_id']
            await h.store.submit(lambda c:(c.execute("UPDATE v2_assistant_lane_rulings SET state='approved',ruling='approve' WHERE ruling_request_id=?",(rid,)),c.commit()))
            original=rulings._fetch
            async def paused_fetch(rid_arg):
                result=await original(rid_arg)
                if asyncio.current_task() is tasks[0]:
                    paused.set()
                    await proceed.wait()
                return result
            rulings._fetch=paused_fetch
            tasks.append(asyncio.create_task(rulings._release(row)))
            await asyncio.wait_for(paused.wait(),2)
            tasks.append(asyncio.create_task(h.send(request_id='question-send-race')))
            await _until(lambda:h.store.front_desk_effect_gate.locked())
            await asyncio.sleep(.02)
            proceed.set()
            done,pending=await asyncio.wait(tasks,timeout=.5)
            assert not pending, {'pending':len(pending),'effect_gate_locked':h.store.front_desk_effect_gate.locked(),
                'gate_waiters':len(h.store.front_desk_effect_gate._waiters or []),
                'stacks':[[str(f.f_code.co_name)+':'+str(f.f_lineno) for f in t.get_stack()] for t in pending]}
            for t in done:t.result()
        finally:
            proceed.set()
            for t in tasks:
                if not t.done():t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await rulings.stop()
            await notify.stop()
    _run(tmp_path,run,front_desk=True)

@pytest.mark.parametrize('change',['closed','generation'])
def test_unavailable_fd_cannot_configure_advisor_after_lock_wait(tmp_path,change):
    async def run(h):
        rulings=h.server.lane_rulings
        await rulings._authority_lock.acquire()
        task=asyncio.create_task(h.server._on_assistant_authority({'action':'set','value':'disabled',
            '_auth_context':{'token_verified':True,'stream_id':SEAT_A,'session_generation':h.gen[SEAT_A]}}))
        try:
            await _until(lambda:bool(rulings._authority_lock._waiters))
            if change=='closed':
                await h.store.update_session(SEAT_A.split(':')[0],'seat-a',status='closed')
            else:
                await h.store.open_session(SEAT_A.split(':')[0],'seat-a',provider='claude',pane_status='pane_alive')
        finally:rulings._authority_lock.release()
        try:
            await _refused(task,'assistant_authority_config_unauthorized')
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    _run(tmp_path,run,front_desk=True)
