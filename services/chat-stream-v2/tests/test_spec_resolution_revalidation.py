"""Strict prepared inputs and checked Store commits across resolver awaits."""
import asyncio
import json
import threading
import time

import pytest
from store import Store, SpecInputsUnavailable, spec_binding_source
from store_qa import QaError
from sessions import Sessions, VerbError
from uiverbs import UIVerbs
from spawnctl import SpawnCtl
from _shared.specs_service import SpecsSubsystem
from test_qa_attestation_validation import _state, _report, QA, READY, SPEC_ID


SPEC = 'spec_fixture__work'


async def owner(store):
    return await store.open_session('fixture', 'lead', role='lead', spec_id=SPEC)


def admission(row, **overrides):
    return dict(scope={'spec_id': SPEC, 'surface': 'implementation', 'cycle': 1},
                actor='fixture:lead', actor_generation=row['session_generation'],
                reviewer='fixture:qa', generation='review-generation', msg_id=0,
                payload_hash='a' * 64, target_specs=[SPEC], **overrides)


async def count(store, table):
    return await store.submit(lambda conn: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0])


@pytest.mark.parametrize('mode', ['missing', 'exception'])
@pytest.mark.asyncio
async def test_constant_failed_batch_refuses_twice_without_single_fallback(mode):
    store = Store(':memory:'); store.start()
    seen, singles = [], []
    def single(value):
        singles.append(threading.current_thread().name); return value
    def batch(ids):
        seen.append(threading.current_thread())
        if mode == 'exception':
            raise TypeError('fixture unavailable')
        return {}
    store.set_spec_identity_resolver(single, batch)
    try:
        row = await owner(store)
        with pytest.raises(QaError) as error:
            await store.qa_admit(**admission(row))
        assert error.value.code == 'qa_spec_unavailable'
        assert len(seen) == 2 and all(thread is not store._thread and thread is not threading.main_thread() for thread in seen)
        assert singles == []
        assert await count(store, 'v2_qa_commissions') == await count(store, 'v2_qa_surfaces') == 0
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_missing_first_batch_has_exactly_one_fresh_uncached_offthread_retry(tmp_path):
    path = tmp_path / 'work/in_progress/fixture__work/spec.md'; path.parent.mkdir(parents=True)
    path.write_text(f'---\nid: {SPEC}\n---\n')
    store = Store(':memory:'); store.start()
    calls = []
    def batch(ids):
        calls.append(threading.current_thread())
        if len(calls) == 1:
            return {}
        # The retry uses a cold resolver, independent of any retained cache.
        fresh = SpecsSubsystem(memory_root=tmp_path.resolve(), session_summaries=lambda: [], changed_callback=lambda ids: None)
        return fresh.canonical_spec_identities(ids)
    store.set_spec_identity_resolver(lambda value: pytest.fail('single fallback'), batch)
    try:
        row = await owner(store)
        assert (await store.qa_admit(**admission(row)))['spec_id'] == SPEC
        assert len(calls) == 2 and all(thread is not store._thread and thread is not threading.main_thread() for thread in calls)
        assert await count(store, 'v2_qa_commissions') == 1
    finally:
        store.stop()


@pytest.mark.parametrize('field,value', [('role', 'worker'), ('status', 'closed'), ('spec_id', 'spec_fixture__other'), ('generation', 'replacement')])
@pytest.mark.asyncio
async def test_changed_actor_cannot_authorize_old_capture(field, value):
    store = Store(':memory:'); store.start()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def batch(ids):
        calls.append(1)
        if len(calls) == 1:
            entered.set(); assert release.wait(5)
        return {value: value for value in ids}
    store.set_spec_identity_resolver(lambda value: value, batch)
    try:
        row = await owner(store)
        task = asyncio.create_task(store.qa_admit(**admission(row)))
        await asyncio.to_thread(entered.wait, 2)
        if field == 'generation':
            await store.submit(lambda conn: (conn.execute("UPDATE v2_session_generations SET generation=? WHERE host='fixture' AND session_name='lead'", (value,)), conn.commit()))
        elif field == 'spec_id':
            await store.update_session('fixture', 'lead', spec_id=value, spec_ids=[value], qualified_spec_ids=[], spec_binding_provenance=[])
        else:
            await store.update_session('fixture', 'lead', **{field: value})
        release.set()
        with pytest.raises(QaError):
            await task
        assert len(calls) == 2
        assert await count(store, 'v2_qa_commissions') == 0
    finally:
        release.set(); store.stop()


@pytest.mark.asyncio
async def test_existing_reviewer_generation_is_pinned_across_retry():
    store = Store(':memory:'); store.start()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def batch(ids):
        calls.append(1)
        if len(calls) == 1:
            entered.set(); assert release.wait(5)
        return {value: value for value in ids}
    store.set_spec_identity_resolver(lambda value: value, batch)
    try:
        row = await owner(store)
        reviewer = await store.open_session('fixture', 'qa', spec_id=SPEC)
        args = admission(row, existing_reviewer=True); args['generation'] = reviewer['session_generation']
        task = asyncio.create_task(store.qa_admit(**args))
        await asyncio.to_thread(entered.wait, 2)
        await store.submit(lambda conn: (conn.execute("UPDATE v2_session_generations SET generation='replacement' WHERE session_name='qa'"), conn.commit()))
        release.set()
        with pytest.raises(QaError) as error:
            await task
        assert error.value.code == 'qa_reviewer_unavailable'
        assert await count(store, 'v2_qa_commissions') == 0
    finally:
        release.set(); store.stop()


@pytest.mark.asyncio
async def test_one_second_offthread_wait_preserves_loop_and_store_responsiveness():
    store = Store(':memory:'); store.start()
    entered = threading.Event()
    times = []
    def batch(ids):
        assert threading.current_thread() not in (store._thread, threading.main_thread())
        entered.set(); time.sleep(1.1); return {value: value for value in ids}
    store.set_spec_identity_resolver(lambda value: value, batch)
    try:
        row = await owner(store)
        task = asyncio.create_task(store.qa_admit(**admission(row)))
        await asyncio.to_thread(entered.wait, 2)
        for _ in range(10):
            queued = time.perf_counter()
            def probe(conn):
                start = time.perf_counter(); conn.execute('SELECT 1').fetchone()
                return start - queued, time.perf_counter() - start
            enqueue, callback = await store.submit(probe)
            heartbeat = time.perf_counter(); await asyncio.sleep(.01)
            times.append((enqueue, callback, time.perf_counter() - heartbeat))
        assert all(max(sample) < .2 for sample in times), times
        await task
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_cancelled_resolution_and_queued_commit_have_no_effects():
    store = Store(':memory:'); store.start()
    entered, release = threading.Event(), threading.Event()
    def batch(ids):
        entered.set(); assert release.wait(5); return {value: value for value in ids}
    store.set_spec_identity_resolver(lambda value: value, batch)
    try:
        row = await owner(store)
        task = asyncio.create_task(store.qa_admit(**admission(row)))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert store._spec_workers == 1  # abandon does not free a running worker slot
        release.set()
        while store._spec_workers: await asyncio.sleep(.001)
        assert await count(store, 'v2_qa_commissions') == 0
        # Exercise cancellation after submission but before Store starts it.
        worker_entered, worker_release = threading.Event(), threading.Event()
        blocker = asyncio.create_task(store.submit(lambda conn: (worker_entered.set(), worker_release.wait(5))))
        await asyncio.to_thread(worker_entered.wait, 2)
        queued = asyncio.create_task(store._submit_spec_commit(lambda conn: conn.execute("INSERT INTO kv VALUES ('abandoned','yes')")))
        await asyncio.sleep(.01); queued.cancel()
        with pytest.raises(asyncio.CancelledError): await queued
        worker_release.set(); await blocker
        assert await store.get('abandoned') is None
    finally:
        release.set(); store.stop()


@pytest.mark.asyncio
async def test_report_batch_error_has_no_partial_report_or_awaiter_effect():
    store, sessions, server, ledger, frames = await _state()
    try:
        await ledger.ingest(_report('accept', QA, qa_verdict='accept'))
        calls = []
        def batch(ids): calls.append(1); return {}
        store.set_spec_identity_resolver(lambda value: pytest.fail('fallback'), batch)
        with pytest.raises(VerbError) as error:
            await ledger.report(_report('ready-failed', READY, completion_kind='implementation_ready', attestation={'stream_id': QA, 'report_id': 'accept'}))
        assert error.value.code == 'report_provenance_unavailable'
        assert len(calls) == 2
        assert await store.submit(lambda conn: conn.execute("SELECT count(*) FROM v2_reports WHERE report_id='ready-failed'").fetchone()[0]) == 0
        assert not any(frame.get('report_id') == 'ready-failed' for frame in frames)
    finally: store.stop()


@pytest.mark.asyncio
async def test_report_replay_skips_resolver_and_changed_payload_conflicts():
    store, sessions, server, ledger, frames = await _state()
    try:
        await ledger.ingest(_report('accept', QA, qa_verdict='accept'))
        message = _report('ready', READY, completion_kind='implementation_ready', attestation={'stream_id': QA, 'report_id': 'accept'})
        await ledger.report(message)
        store.set_spec_identity_resolver(lambda value: pytest.fail('replay resolution'), lambda ids: pytest.fail('replay batch'))
        assert (await ledger.report(message))['qa_attestation_validation']['state'] == 'verified'
        with pytest.raises(VerbError) as error:
            await ledger.report({**message, 'summary': 'changed'})
        assert error.value.code == 'report_id_replay_conflict'
    finally: store.stop()


@pytest.mark.asyncio
async def test_prepared_qa_and_intent_commit_or_refuse_together():
    store = Store(':memory:'); store.start()
    store.set_spec_identity_resolver(lambda value: value)
    try:
        row = await owner(store)
        args = admission(row)
        prepared = await store.qa_admit(**args, prepare_only=True)
        assert await count(store, 'v2_qa_commissions') == 0
        assert await store.reserve_stream_id('fixture', 'qa', ttl_s=30, request_id='request', nonce='owned')
        expected = spec_binding_source(row)
        await store.update_session('fixture', 'lead', role='worker')
        with pytest.raises(SpecInputsUnavailable):
            await store.record_spawn_intent('fixture', 'qa', {'open_fields': {}}, request_id='request', nonce='owned', expected_spec_sources=(('fixture:lead', expected),), prepared_qa=prepared)
        assert await count(store, 'v2_qa_commissions') == await count(store, 'v2_qa_surfaces') == 0
        assert (await store.reservations())[0]['payload'] is None
    finally: store.stop()


@pytest.mark.asyncio
async def test_report_retries_changed_referenced_verdict_without_stale_success():
    store, sessions, server, ledger, frames = await _state()
    entered, release = threading.Event(), threading.Event()
    calls = []
    try:
        await ledger.ingest(_report('accept', QA, qa_verdict='accept'))
        original = store._spec_identity_resolver
        def batch(ids):
            calls.append(1)
            if len(calls) == 1:
                entered.set(); assert release.wait(5)
            return {value: original(value) for value in ids}
        store.set_spec_identity_resolver(original, batch)
        task = asyncio.create_task(ledger.report(_report('ready-race', READY, completion_kind='implementation_ready', attestation={'stream_id': QA, 'report_id': 'accept'})))
        await asyncio.to_thread(entered.wait, 2)
        await store.submit(lambda conn: (conn.execute("UPDATE v2_reports SET qa_verdict='reject' WHERE report_id='accept'"), conn.commit()))
        release.set()
        reply = await task
        assert len(calls) == 2
        assert reply['qa_attestation_validation']['state'] == 'unverified'
        assert 'qa_verdict_not_accept' in reply['qa_attestation_validation']['reasons']
    finally:
        release.set(); store.stop()


@pytest.mark.asyncio
async def test_attach_retries_provenance_mutation_and_keeps_concurrent_binding(tmp_path):
    for name in ['one', 'two', 'three']:
        path = tmp_path / f'work/in_progress/fixture__{name}/spec.md'; path.parent.mkdir(parents=True)
        path.write_text(f'---\nid: spec_fixture__{name}\n---\n')
    subject = SpecsSubsystem(memory_root=tmp_path.resolve(), session_summaries=lambda: [], changed_callback=lambda ids: None)
    store = Store(':memory:'); store.start()
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = subject.spec_resolution_snapshot
    def capture():
        calls.append(1)
        if len(calls) == 1:
            entered.set(); assert release.wait(5)
        return original()
    subject.spec_resolution_snapshot = capture
    try:
        sessions = Sessions(store, local_host='fixture')
        await sessions.open('fixture', 'lead', spec_id='spec_fixture__one')
        verbs = UIVerbs(store, sessions, SpawnCtl(store, sessions, tmux=object(), specs=subject), specs=subject)
        task = asyncio.create_task(verbs.session_spec_update({'action':'attach', 'host':'fixture', 'session_name':'lead', 'spec_id':'fixture__two'}))
        await asyncio.to_thread(entered.wait, 2)
        await store.update_session('fixture', 'lead', spec_ids=['spec_fixture__one', 'spec_fixture__three'])
        release.set(); result = await task
        assert len(calls) == 2
        assert result['session']['spec_ids'] == ['spec_fixture__one', 'spec_fixture__three', 'spec_fixture__two']
    finally:
        release.set(); store.stop()


@pytest.mark.asyncio
async def test_two_binding_mutations_refuse_without_commission():
    store = Store(':memory:'); store.start()
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = []
    def batch(ids):
        index = len(calls); calls.append(1)
        entered[index].set(); assert release[index].wait(5)
        return {value: value for value in ids}
    store.set_spec_identity_resolver(lambda value: value, batch)
    try:
        row = await owner(store)
        task = asyncio.create_task(store.qa_admit(**admission(row)))
        for index in range(2):
            await asyncio.to_thread(entered[index].wait, 2)
            # Same canonical ID, different consumed provenance grants.
            await store.update_session('fixture', 'lead', spec_id=SPEC, spec_ids=[SPEC], spec_binding_provenance=[{'spec_id':SPEC, 'provenance':'operator_v2', 'granting_principal':f'operator-{index}', 'granted_at':f'2026-01-0{index+1}T00:00:00Z'}])
            release[index].set()
        with pytest.raises(QaError) as error: await task
        assert error.value.code == 'qa_spec_unavailable'
        assert len(calls) == 2 and await count(store, 'v2_qa_commissions') == 0
    finally:
        for item in release: item.set()
        store.stop()


@pytest.mark.asyncio
async def test_cancel_after_callback_entry_before_sql_begin_rolls_back_report():
    store,sessions,server,ledger,frames=await _state()
    entered,release=threading.Event(),threading.Event()
    blocked=[]
    def trace(sql):
        if sql=='BEGIN IMMEDIATE' and not blocked:
            blocked.append(True); entered.set(); assert release.wait(5)
    try:
        await store.submit(lambda conn:conn.set_trace_callback(trace))
        task=asyncio.create_task(ledger.report(_report('cancelled-inside-callback',READY)))
        assert await asyncio.to_thread(entered.wait,3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        release.set()
        assert await store.submit(lambda conn:conn.execute("SELECT count(*) FROM v2_reports WHERE report_id='cancelled-inside-callback'").fetchone()[0])==0
        assert not any(frame.get('report_id')=='cancelled-inside-callback' for frame in frames)
    finally:
        release.set(); await store.submit(lambda conn:conn.set_trace_callback(None));store.stop()
