"""Cross-request and out-of-order recovery proofs for the composite boundary."""
import asyncio
from contextlib import asynccontextmanager
import json

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from store import Store
from test_assistant_composite import (
    COMPOSITE_STREAM as STREAM, AUTHORITY_STREAM as ASTRA,
    CONVERSATION_STREAM as LUNA, LEAD_STREAM as LEAD, _seed_dispatch,
)


@pytest.mark.parametrize('delegated', [False, True])
@pytest.mark.parametrize('terminate', [False, True])
@pytest.mark.parametrize('stale', [None, 'before_ingest', 'after_ingest'])
def test_real_ledger_report_completes_before_close_and_wakes_once(delegated, terminate, stale):
    import hashlib
    from ledger import Ledger
    from sessions import Sessions
    from server import Server
    from store import STREAM_TOKEN_HASH_VERSION
    async def run():
        async with setup() as (store, composite, rows):
            rows[LEAD] = await store.open_session(*LEAD.split(':', 1), provider='codex',
                role='lead', visibility='hidden', parent_stream_id=ASTRA,
                requested_model='gpt-5.6-luna', requested_effort='max',
                effective_model='gpt-5.6-luna', effective_effort='max',
                spec_ids=['example__report_provenance'], spec_id='example__report_provenance',
                spec_binding_provenance=[dict(spec_id='example__report_provenance',
                    provenance='spawn_explicit', granting_principal='operator',
                    granted_at='2026-09-19T00:00:00Z')])
            lane = await bound_lane(store, composite, rows)
            dispatch = 'work' if delegated else 'work-lead'
            await composite.operation(operation('lane.decision', 'start', dispatch, dict(
                decision_id='d', transition='start', from_phase='discussion',
                to_phase='execution', operator_basis_message_ids=['work']), lane, 2),
                actor_stream_id=LEAD)
            sessions = Sessions(store, local_host=LEAD.split(':', 1)[0])
            await sessions.refresh()
            ledger = Ledger(store, sessions=sessions)
            closed = []
            async def close_after_completion(row):
                current = await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=lane)
                assert current['phase'] == 'completed'
                closed.append(row['report_id'])
                return {'closed': True}
            # Leave the actual ledger validation, persistence and close ordering
            # intact; only the external session termination effect is captured.
            ledger._terminate_after_report = close_after_completion
            server = Server(store=store, sessions=sessions, ledger=ledger)
            server.assistant_composite = composite
            token = 'fixture-terminal-token'
            await store.grant_stream_token(*LEAD.split(':', 1),
                hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            class Peer:
                remote_address = ('127.0.0.1', 1234)
            auth = await server._auth_context(Peer(), dict(stream_token=token, from_stream_id=LEAD))
            async def replace_generation():
                await store.mark_closed(*LEAD.split(':', 1), closed_at='2026-09-19T00:00:00Z',
                    pane_status='closed', expected_generation=auth['session_generation'])
                await store.open_session(*LEAD.split(':', 1), provider='codex')
            if stale == 'before_ingest':
                await replace_generation()
            elif stale == 'after_ingest':
                ingest = ledger.ingest
                async def persist_then_replace(*args, **kwargs):
                    result = await ingest(*args, **kwargs)
                    await replace_generation()
                    return result
                ledger.ingest = persist_then_replace
            before = set(await store.list_outbound_notice_ids(limit=100, force=True))
            msg = dict(type='report', from_stream_id=LEAD, status='done', report_id='real-terminal',
                summary='done', findings=[], next_action='continue', terminate=terminate,
                extras={'assistant_composite': dict(stream_id=STREAM, lane_id=lane, dispatch_id=dispatch)},
                _auth_context=auth)
            if stale:
                with pytest.raises(ValueError, match='generation|scope'):
                    await server._on_report(msg)
                assert (await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=lane))['phase'] == 'execution'
                assert not closed
                new_notices = set(await store.list_outbound_notice_ids(limit=100, force=True)) - before
                # The injected session close has its own D2 lifecycle notice.
                assert not any(n.startswith(('assistant-terminal:', 'child-report-ready-')) for n in new_notices)
                return
            for _ in range(2):
                reply = await server._on_report(msg)
                assert reply['durability_ack']
            current = await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=lane)
            assert current['phase'] == 'completed'
            notices = set(await store.list_outbound_notice_ids(limit=100, force=True)) - before
            assert notices == {f'assistant-terminal:{lane}:real-terminal'}
            assert bool(closed) == terminate
    asyncio.run(run())


@asynccontextmanager
async def setup(**kwargs):
    store = Store(':memory:')
    store.start()
    rows = {}
    for sid in (ASTRA, LUNA, LEAD):
        rows[sid] = await store.open_session(*sid.split(':', 1), provider='codex')
    composite = AssistantComposite(store, config=AssistantCompositeConfig(
        enabled=True, stream_id=STREAM, router_endpoint='ssh://fixture/assistant-router-v1',
        astra_stream_id=ASTRA, luna_stream_id=LUNA,
    ), **kwargs)
    await composite.ensure_projection()
    try:
        yield store, composite, rows
    finally:
        await composite.stop()
        store.stop()


def operation(name, key, dispatch, payload, lane=None, version=None, **extra):
    return dict(operation=name, request_id=key, composite_stream_id=STREAM,
                dispatch_id=dispatch, lane_id=lane, expected_lane_version=version,
                payload=payload, evidence_refs=[], **extra)


def test_admission_cannot_substitute_another_input():
    async def run():
        async with setup() as (store, composite, rows):
            await _seed_dispatch(store, input_identity='actual', dispatch_id='d',
                                 target=ASTRA, generation=rows[ASTRA]['session_generation'])
            with pytest.raises(ValueError, match='scope|input|correlation'):
                await composite.operation(operation('lane.admit', 'admit', 'd', {
                    'mode': 'new', 'subject': 'work', 'request_message_id': 'other',
                }), actor_stream_id=ASTRA)
            assert await store.list_assistant_composite_open_lanes(stream_id=STREAM, limit=10) == []
    asyncio.run(run())


@pytest.mark.parametrize('old_outcome', ['landed', 'failed', 'exception'])
def test_old_fallback_receipt_cannot_overwrite_typed_resolution(old_outcome):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def dispatch(route):
            if route['dispatch_id'] == 'old-fallback':
                entered.set()
                await release.wait()
                if old_outcome == 'exception':
                    raise RuntimeError('late ambiguous receipt')
                return {'delivery': old_outcome}
            return {'delivery': 'landed'}
        async with setup(dispatch=dispatch) as (store, composite, rows):
            composite._wake_worker = lambda: None
            await _seed_dispatch(store, input_identity='input', dispatch_id='old-fallback',
                                 target=LUNA, generation=rows[LUNA]['session_generation'])
            route = await store.get_assistant_composite_route(stream_id=STREAM, input_identity='input')
            route = await store.update_assistant_composite_route(route['route_id'],
                routing_state='fallback_dispatched', delivery_state='intent')
            composite._start_dispatch(route)
            await asyncio.wait_for(entered.wait(), 1)
            await composite.operation(operation('route.resolve', 'resolve', 'old-fallback', {
                'schema_version': 'assistant-router/v1', 'disposition': 'conversation',
                'lane_id': None, 'depends_on_message_id': None, 'reason': 'resolved',
            }, reply_to_message_id='input'), actor_stream_id=LUNA)
            release.set()
            await asyncio.gather(*tuple(composite._dispatch_tasks))
            final = await store.get_assistant_composite_route(stream_id=STREAM, input_identity='input')
            assert final['routing_state'] == 'resolved'
            assert final['dispatch_id'].startswith('assistant-resolve-')
            assert final['delivery_state'] == 'landed'
    asyncio.run(run())


@pytest.mark.parametrize('fallback_available', [False, True])
def test_missing_target_does_not_strand_following_input(fallback_available):
    class Router:
        async def classify(self, _route):
            return dict(schema_version='assistant-router/v1', disposition='new_topic',
                        lane_id=None, depends_on_message_id=None, reason='new work')
    async def run():
        async with setup(router=Router(), dispatch=lambda route: asyncio.sleep(0, result={'delivery': 'landed'})) as (store, composite, _rows):
            # Configured but nonexistent targets prove recovery independently of close machinery.
            object.__setattr__(composite.config, 'astra_stream_id', 'fixture-missing:authority')
            if not fallback_available:
                object.__setattr__(composite.config, 'luna_stream_id', 'fixture-missing:conversation')
            await composite.accept_input({'message': 'one', 'msg_id': 'one'})
            await composite.accept_input({'message': 'two', 'msg_id': 'two'})
            await composite._worker
            if composite._dispatch_tasks:
                await asyncio.gather(*tuple(composite._dispatch_tasks))
            for key in ('one', 'two'):
                row = await store.get_assistant_composite_route(stream_id=STREAM, input_identity=key)
                assert row['routing_state'] == ('fallback_dispatched' if fallback_available else 'routing_failed')
    asyncio.run(run())


def test_known_pre_effect_question_failure_retries_same_key():
    async def run():
        calls = []
        async def adapter(_op, msg):
            calls.append(msg['request_id'])
            if len(calls) == 1:
                return {'type': 'prompt.error', 'error_code': 'fixture_pre_effect_failure'}
            return {'type': 'prompt.ask.ok', 'question': {'question_id': 'q', 'state': 'open'}}
        async with setup(question_operation=adapter) as (store, composite, rows):
            for key in ('admit',):
                await _seed_dispatch(store, input_identity=key, dispatch_id=key,
                                     target=ASTRA, generation=rows[ASTRA]['session_generation'])
            admitted = await composite.operation(operation('lane.admit', 'a', 'admit',
                {'mode': 'new', 'subject': 'work', 'request_message_id': 'admit'}), actor_stream_id=ASTRA)
            lane = admitted['lane_id']
            await _seed_dispatch(store, input_identity='bind', dispatch_id='bind', lane_id=lane,
                                 target=ASTRA, generation=rows[ASTRA]['session_generation'])
            await composite.operation(operation('lane.bind', 'b', 'bind', {
                'backend_kind': 'assistant_conversation', 'backend_stream_id': LEAD,
                'backend_generation': rows[LEAD]['session_generation'],
            }, lane, 1), actor_stream_id=ASTRA)
            await _seed_dispatch(store, input_identity='ask', dispatch_id='ask', lane_id=lane,
                                 target=LEAD, generation=rows[LEAD]['session_generation'])
            request = operation('question.open', 'q-op', 'ask', {'envelope': {}}, lane, 2)
            with pytest.raises(ValueError, match='assistant_question_operation_failed'):
                await composite.operation(request, actor_stream_id=LEAD)
            await composite.operation(request, actor_stream_id=LEAD)
            replay = await composite.operation(request, actor_stream_id=LEAD)
            assert replay['duplicate']
            assert calls == ['q-op', 'q-op']
            row = await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=lane)
            assert row['pending_question_id'] == 'q'
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['prose', 'question', 'result', 'status'])
def test_fallback_cannot_publish(kind):
    async def run():
        async with setup() as (store, composite, rows):
            await _seed_dispatch(store, input_identity='input', dispatch_id='fallback',
                                 target=LUNA, generation=rows[LUNA]['session_generation'])
            route = await store.get_assistant_composite_route(stream_id=STREAM, input_identity='input')
            await store.update_assistant_composite_route(route['route_id'], routing_state='fallback_dispatched')
            with pytest.raises(ValueError, match='fallback|state'):
                await composite.publish(dict(request_id='p', composite_stream_id=STREAM,
                    dispatch_id='fallback', reply_to_message_id='input', publish_kind=kind,
                    message='not allowed', attachment_ids=[], evidence_refs=[]), actor_stream_id=LUNA)
    asyncio.run(run())


def test_publication_refuses_unverified_attachment():
    async def run():
        async with setup() as (store, composite, rows):
            await _seed_dispatch(store, input_identity='input', dispatch_id='d',
                                 target=LUNA, generation=rows[LUNA]['session_generation'])
            with pytest.raises(ValueError, match='attachment'):
                await composite.publish(dict(request_id='p', composite_stream_id=STREAM,
                    dispatch_id='d', reply_to_message_id='input', publish_kind='prose',
                    message='missing image', attachment_ids=['0' * 64], evidence_refs=[]), actor_stream_id=LUNA)
    asyncio.run(run())


async def bound_lane(store, composite, rows, key='work'):
    await _seed_dispatch(store, input_identity=key, dispatch_id=key,
                         target=ASTRA, generation=rows[ASTRA]['session_generation'])
    admitted = await composite.operation(operation('lane.admit', key+'-admit', key,
        {'mode': 'new', 'subject': key, 'request_message_id': key}), actor_stream_id=ASTRA)
    lane = admitted['lane_id']
    # Admission associates the original authority dispatch with its lane. It
    # remains a valid authority capability when later lead reports arrive.
    await composite.operation(operation('lane.bind', key+'-bind', key, {
        'backend_kind': 'assistant_conversation', 'backend_stream_id': LEAD,
        'backend_generation': rows[LEAD]['session_generation'],
    }, lane, 1), actor_stream_id=ASTRA)
    await _seed_dispatch(store, input_identity=key+'-lead', dispatch_id=key+'-lead',
                         lane_id=lane, target=LEAD, generation=rows[LEAD]['session_generation'])
    return lane


def test_operation_cannot_cross_lanes_with_valid_actor_and_version():
    async def run():
        async with setup() as (store, composite, rows):
            first = await bound_lane(store, composite, rows, 'one')
            second = await bound_lane(store, composite, rows, 'two')
            with pytest.raises(ValueError, match='lane.*scope|scope.*lane'):
                await composite.operation(operation('lane.bind', 'cross', 'one', {
                    'backend_kind': 'assistant_conversation', 'backend_stream_id': LEAD,
                    'backend_generation': rows[LEAD]['session_generation'],
                }, second, 2), actor_stream_id=ASTRA)
            assert (await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=first))['version'] == 2
            assert (await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=second))['version'] == 2
    asyncio.run(run())


@pytest.mark.parametrize('basis', ['missing', 'other'])
def test_decision_requires_recorded_operator_basis_in_its_lane(basis):
    async def run():
        async with setup() as (store, composite, rows):
            lane = await bound_lane(store, composite, rows)
            await bound_lane(store, composite, rows, 'other')
            with pytest.raises(ValueError, match='basis'):
                await composite.operation(operation('lane.decision', 'bad-basis', 'work-lead', {
                    'decision_id': 'decision', 'transition': 'start', 'from_phase': 'discussion',
                    'to_phase': 'execution', 'operator_basis_message_ids': [basis],
                }, lane, 2), actor_stream_id=LEAD)
            assert (await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=lane))['phase'] == 'discussion'
    asyncio.run(run())


def test_close_requires_terminal_receipt_for_same_lane():
    async def run():
        async with setup() as (store, composite, rows):
            lane = await bound_lane(store, composite, rows)
            await bound_lane(store, composite, rows, 'other')
            for key in ('work', 'other'):
                current = await store.find_assistant_composite_route_by_dispatch(key+'-lead')
                target_lane = json.loads(current['route_json'])['lane_id']
                await composite.operation(operation('lane.decision', key+'-start', key+'-lead', {
                    'decision_id': key+'-decision', 'transition': 'start', 'from_phase': 'discussion',
                    'to_phase': 'execution', 'operator_basis_message_ids': [key],
                }, target_lane, 2), actor_stream_id=LEAD)
                completed = await composite.terminal_report(dict(status='done', actor_stream_id=LEAD,
                    report_id=key+'-report', lane_id=target_lane, dispatch_id=key+'-lead'),
                    actor_generation=rows[LEAD]['session_generation'])
                assert completed['phase'] == 'completed'
            row = await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=lane)
            with pytest.raises(ValueError, match='completion|terminal'):
                await composite.operation(operation('lane.close', 'bad-close', 'work', {
                    'completion_message_id': 'other-report', 'completion_disposition': 'accepted',
                }, lane, row['version']), actor_stream_id=ASTRA)
            good = await composite.operation(operation('lane.close', 'good-close', 'work', {
                'completion_message_id': 'work-report', 'completion_disposition': 'accepted',
            }, lane, row['version']), actor_stream_id=ASTRA)
            assert good['next_phase'] == 'closed'
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['question', 'result', 'status'])
def test_operation_publication_requires_matching_committed_receipt(kind):
    async def run():
        async with setup() as (store, composite, rows):
            await bound_lane(store, composite, rows)
            request = dict(request_id='p', composite_stream_id=STREAM, dispatch_id='work-lead',
                reply_to_message_id='work-lead', publish_kind=kind, message='claims completion',
                attachment_ids=[], evidence_refs=['work-admit'])
            with pytest.raises(ValueError, match='receipt'):
                await composite.publish(request, actor_stream_id=LEAD)
            assert await store.get_assistant_composite_publication(stream_id=STREAM, publication_key='p') is None
    asyncio.run(run())


def test_question_publication_and_explicit_reply_retain_real_wire_ids():
    async def run():
        events = []
        async def broadcast(event):
            events.append(event['event'])
        async def adapter(_op, _msg):
            return {'type': 'prompt.ask.ok', 'question': {'question_id': 'question-id'}}
        async with setup(broadcast=broadcast, question_operation=adapter,
                         dispatch=lambda route: asyncio.sleep(0, result={'delivery': 'landed'})) as (store, composite, rows):
            lane = await bound_lane(store, composite, rows)
            await composite.operation(operation('question.open', 'ask', 'work-lead',
                {'envelope': {}}, lane, 2), actor_stream_id=LEAD)
            await composite.publish(dict(request_id='published', composite_stream_id=STREAM,
                dispatch_id='work-lead', reply_to_message_id='work-lead', reply_to_question_id='question-id',
                publish_kind='question', message='Which approach?', attachment_ids=[], evidence_refs=['ask']),
                actor_stream_id=LEAD)
            published = events[-1]
            assert published['message_id'] == 'publication:published'
            assert published['reply_to_question_id'] == 'question-id'
            # A reply to the rendered publication uses its actual durable ID.
            await composite.accept_input(dict(message='  compare options  ', msg_id='client-id',
                reply_to_message_id=published['message_id']), operator_principal='operator:fixture')
            echoed = events[-1]
            assert echoed['message_id'] == echoed['optimistic_id'] == 'client-id'
            assert echoed['text'] == '  compare options  '
            assert echoed['reply_to_message_id'] == published['message_id']
            route = await store.get_assistant_composite_route(stream_id=STREAM, input_identity='client-id')
            assert route['route_target'] == LEAD
            assert json.loads(route['route_json'])['lane_id'] == lane
            await asyncio.gather(*tuple(composite._dispatch_tasks))
    asyncio.run(run())


def test_authenticated_generation_is_not_replaced_by_new_lookup():
    async def run():
        async with setup() as (store, composite, rows):
            await _seed_dispatch(store, input_identity='work', dispatch_id='work',
                                 target=ASTRA, generation=rows[ASTRA]['session_generation'])
            with pytest.raises(ValueError, match='generation'):
                await composite.operation(operation('lane.admit', 'admit', 'work',
                    {'mode': 'new', 'subject': 'work', 'request_message_id': 'work'},
                    _auth_context={'session_generation': 'previous-generation'}), actor_stream_id=ASTRA)
            assert await store.list_assistant_composite_open_lanes(stream_id=STREAM, limit=10) == []
    asyncio.run(run())


def test_actor_generation_rechecked_at_transaction_after_service_validation():
    async def run():
        async with setup() as (store, composite, rows):
            await _seed_dispatch(store, input_identity='work', dispatch_id='work',
                                 target=ASTRA, generation=rows[ASTRA]['session_generation'])
            original = store.apply_assistant_composite_operation
            async def race(**kwargs):
                await store.mark_closed(*ASTRA.split(':', 1), closed_at='2026-09-19T00:00:00Z',
                    pane_status='closed', expected_generation=rows[ASTRA]['session_generation'])
                await store.open_session(*ASTRA.split(':', 1), provider='codex')
                return await original(**kwargs)
            store.apply_assistant_composite_operation = race
            with pytest.raises(ValueError, match='generation'):
                await composite.operation(operation('lane.admit', 'admit', 'work',
                    {'mode': 'new', 'subject': 'work', 'request_message_id': 'work'}), actor_stream_id=ASTRA)
            assert await store.list_assistant_composite_open_lanes(stream_id=STREAM, limit=10) == []
    asyncio.run(run())


def test_real_blob_publication_retains_verified_download_envelope(tmp_path):
    import base64
    import hashlib
    from types import SimpleNamespace
    from blobs import BlobStore
    from server import Server
    async def run():
        blobs = BlobStore(str(tmp_path/'blobs'))
        await blobs.start()
        handlers = blobs.wire_handlers()
        body = b'generated result artifact'
        await handlers['upload_blob_init']({'request_id': 'upload'})
        uploaded = await handlers['upload_blob_chunk']({'request_id': 'upload',
            'data_b64': base64.b64encode(body).decode(), 'final': True})
        key = hashlib.sha256(body).hexdigest()
        assert uploaded['blob_sha'] == key
        async with setup() as (store, composite, rows):
            server = Server(store=store, comms=SimpleNamespace(blob_store=blobs))
            composite.publication_attachments = server._assistant_publication_attachments
            await _seed_dispatch(store, input_identity='input', dispatch_id='d',
                                 target=LUNA, generation=rows[LUNA]['session_generation'])
            request = dict(request_id='p', composite_stream_id=STREAM, dispatch_id='d',
                reply_to_message_id='input', publish_kind='prose', message='Artifact attached',
                attachment_ids=[key], evidence_refs=[])
            published = await composite.publish(request, actor_stream_id=LUNA)
            assert not published['duplicate']
            saved = await store.get_assistant_composite_publication(stream_id=STREAM, publication_key='p')
            event = await store.submit(lambda conn: json.loads(conn.execute(
                'SELECT event_json FROM session_event_tail WHERE event_id=?', (saved['event_id'],),
            ).fetchone()[0]))
            assert event['attachments'] == [{'key': key, 'mime': 'application/octet-stream', 'size': len(body)}]
            with pytest.raises(ValueError, match='attachment_unverified'):
                await composite.publish({**request, 'request_id': 'missing', 'attachment_ids': ['0'*64]}, actor_stream_id=LUNA)
    asyncio.run(run())


def test_real_token_auth_context_carries_generation_to_transaction():
    import hashlib
    from store import STREAM_TOKEN_HASH_VERSION
    from server import Server
    class Peer:
        remote_address = ('127.0.0.1', 1234)
    async def run():
        async with setup() as (store, composite, rows):
            token = 'fixture-token-only'
            await store.grant_stream_token(*ASTRA.split(':', 1), hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            server = Server(store=store)
            auth = await server._auth_context(Peer(), {'stream_token': token, 'from_stream_id': ASTRA})
            assert auth['token_verified']
            assert auth['session_generation'] == rows[ASTRA]['session_generation']
    asyncio.run(run())


def test_split_admission_preserves_each_lanes_authority_and_requires_explicit_reply_binding():
    async def run():
        async with setup() as (store, composite, rows):
            await _seed_dispatch(store, input_identity='split', dispatch_id='split',
                                 target=ASTRA, generation=rows[ASTRA]['session_generation'])
            lanes = []
            for key in ('first', 'second'):
                admitted = await composite.operation(operation('lane.admit', key, 'split', {
                    'mode': 'new', 'subject': key, 'request_message_id': 'split', 'split_group_id': 'split-group',
                }), actor_stream_id=ASTRA)
                lanes.append(admitted['lane_id'])
            for index, lane in enumerate(lanes):
                await composite.operation(operation('lane.bind', 'bind-'+str(index), 'split', {
                    'backend_kind': 'assistant_conversation', 'backend_stream_id': LEAD,
                    'backend_generation': rows[LEAD]['session_generation'],
                }, lane, 1), actor_stream_id=ASTRA)
                await composite.operation(operation('lane.decision', 'start-'+str(index), 'split', {
                    'decision_id': 'decision-'+str(index), 'transition': 'start', 'from_phase': 'discussion',
                    'to_phase': 'execution', 'operator_basis_message_ids': ['split'],
                }, lane, 2), actor_stream_id=ASTRA)
            with pytest.raises(ValueError, match='ambiguous'):
                await composite.accept_input(dict(message='continue', msg_id='reply', reply_to_message_id='split'))
    asyncio.run(run())


def test_question_reply_cannot_attach_another_lanes_message():
    async def run():
        async with setup() as (store, composite, rows):
            lane = await bound_lane(store, composite, rows)
            await bound_lane(store, composite, rows, 'other')
            await store.set_assistant_composite_lane_question(stream_id=STREAM, lane_id=lane, question_id='q')
            with pytest.raises(ValueError, match='mismatch'):
                await composite.accept_input(dict(message='yes', msg_id='reply',
                    reply_to_question_id='q', reply_to_message_id='other'))
            assert await store.get_assistant_composite_route(stream_id=STREAM, input_identity='reply') is None
    asyncio.run(run())


def test_question_cancel_checks_binding_before_adapter_side_effect():
    async def run():
        calls = []
        async def adapter(op, msg):
            calls.append(op)
            return {'type': 'prompt.cancel.ok', 'question': {'question_id': msg['payload']['question_id']}}
        async with setup(question_operation=adapter) as (store, composite, rows):
            lane = await bound_lane(store, composite, rows)
            await store.set_assistant_composite_lane_question(stream_id=STREAM, lane_id=lane, question_id='owned')
            current = await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=lane)
            with pytest.raises(ValueError, match='binding'):
                await composite.operation(operation('question.cancel', 'cancel', 'work-lead',
                    {'question_id': 'other-question'}, lane, current['version']), actor_stream_id=LEAD)
            assert calls == []
    asyncio.run(run())


def test_reopened_lane_accepts_new_report_and_refuses_prior_completion():
    async def run():
        async with setup() as (store, composite, rows):
            lane = await bound_lane(store, composite, rows)
            for cycle in (1, 2):
                current = await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=lane)
                await composite.operation(operation('lane.decision', f'start-{cycle}', 'work-lead', {
                    'decision_id': f'd-{cycle}', 'transition': 'start', 'from_phase': 'discussion',
                    'to_phase': 'execution', 'operator_basis_message_ids': ['work'],
                }, lane, current['version']), actor_stream_id=LEAD)
                completed = await composite.terminal_report(dict(status='done', actor_stream_id=LEAD,
                    report_id=f'report-{cycle}', lane_id=lane, dispatch_id='work-lead'),
                    actor_generation=rows[LEAD]['session_generation'])
                assert completed['phase'] == 'completed'
                if cycle == 1:
                    await composite.operation(operation('lane.decision', 'reopen', 'work', {
                        'decision_id': 'reopen-d', 'transition': 'reopen', 'from_phase': 'completed',
                        'to_phase': 'discussion', 'operator_basis_message_ids': ['work'],
                    }, lane, completed['version']), actor_stream_id=ASTRA)
            with pytest.raises(ValueError, match='completion'):
                await composite.operation(operation('lane.close', 'close-old', 'work', {
                    'completion_message_id': 'report-1', 'completion_disposition': 'accepted',
                }, lane, completed['version']), actor_stream_id=ASTRA)
            result = await composite.operation(operation('lane.close', 'close-current', 'work', {
                'completion_message_id': 'report-2', 'completion_disposition': 'accepted',
            }, lane, completed['version']), actor_stream_id=ASTRA)
            assert result['next_phase'] == 'closed'
    asyncio.run(run())


@pytest.mark.parametrize('replace_generation', [False, True])
def test_real_server_report_preserves_authenticated_successor_generation(replace_generation):
    import hashlib
    from types import SimpleNamespace
    from server import Server
    from store import STREAM_TOKEN_HASH_VERSION
    class Peer:
        remote_address = ('127.0.0.1', 1234)
    async def run():
        async with setup() as (store, composite, rows):
            lane = await bound_lane(store, composite, rows)
            await composite.operation(operation('lane.decision', 'start', 'work-lead', {
                'decision_id': 'd', 'transition': 'start', 'from_phase': 'discussion',
                'to_phase': 'execution', 'operator_basis_message_ids': ['work'],
            }, lane, 2), actor_stream_id=LEAD)
            successor = 'fixture-host-lead:successor'
            first = await store.open_session(*successor.split(':', 1), provider='codex', handoff_from_stream_id=LEAD)
            token = 'fixture-successor-token'
            await store.grant_stream_token(*successor.split(':', 1), hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            # Only the ledger persistence response is stubbed: both authentication
            # and report-to-composite dispatch are the actual Server implementation.
            async def persist(msg, *, before_close=None):
                assert msg['_auth_context']['session_generation'] == first['session_generation']
                reply = {'type': 'report.ok', 'report_id': 'terminal', 'durability_ack': True}
                if before_close:
                    await before_close(reply)
                return reply
            server = Server(store=store, ledger=SimpleNamespace(report=persist))
            server.assistant_composite = composite
            auth = await server._auth_context(Peer(), {'stream_token': token, 'from_stream_id': successor})
            assert auth['token_verified'] and auth['session_generation'] == first['session_generation']
            if replace_generation:
                await store.mark_closed(*successor.split(':', 1), closed_at='2026-09-19T00:00:00Z',
                    pane_status='closed', expected_generation=first['session_generation'])
                second = await store.open_session(*successor.split(':', 1), provider='codex', handoff_from_stream_id=LEAD)
                assert second['session_generation'] != auth['session_generation']
            msg = dict(type='report', from_stream_id=successor, status='done', report_id='terminal',
                extras={'assistant_composite': dict(stream_id=STREAM, lane_id=lane, dispatch_id='work-lead')},
                _auth_context=auth)
            if replace_generation:
                with pytest.raises(ValueError, match='generation'):
                    await server._on_report(msg)
                assert (await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=lane))['phase'] == 'execution'
            else:
                assert (await server._on_report(msg))['durability_ack']
                assert (await store.get_assistant_composite_lane(stream_id=STREAM, lane_id=lane))['phase'] == 'completed'
    asyncio.run(run())
