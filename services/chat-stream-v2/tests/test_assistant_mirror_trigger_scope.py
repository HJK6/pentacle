"""Trusted trigger filtering, proactive publication, and composite voice metadata."""
import asyncio
import json
from dataclasses import replace

import pytest

from assistant_composite import AssistantComposite
from store import Store
from test_assistant_prose_mirror import ASSISTANT, ROOT, _config, _answer_rows, _route_for


async def setup():
    store = Store(':memory:')
    store.start()
    root = await store.open_session('fixture-root', 'visible', provider='codex', pane_pid='4242')
    composite = AssistantComposite(store, config=_config(root['session_generation']))
    await composite.ensure_projection()
    return store, composite, root['session_generation']


async def append(store, kind, text, identity, **raw):
    event = {'stream_id': ROOT, 'provider': 'codex', 'kind': kind, 'text': text,
             'raw': {'source_session_identity': 'scope-session', 'transport': 'codex-rollout', **raw}}
    return (await store.append_session_events_lifecycle_cas([
        {'stream_id': ROOT, 'event': event, 'identity': identity,
         'lifecycle': await store.fetch_open_session_lifecycle(ROOT, pane_pid='4242')}], limit=100))[0]


async def sql(store, statement, values=()):
    def mutate(conn):
        conn.execute(statement, values)
        conn.commit()
    await store.submit(mutate)


async def trust(store, record, target=ROOT):
    if record == 'tell':
        await store.put_tell_delivery('trusted', {'reply': {'to_stream_id': target},
            'delivery': {'to_stream_id': target}})
    elif record == 'notice':
        await sql(store, 
            "INSERT INTO v2_outbound_notices(notice_id,kind,dedupe_key,recipient_stream_id,tell_id,body,payload_digest,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ('trusted', 'fixture', 'scope', target, 'notice-tell', 'fixture', 'digest', 'now'))


@pytest.mark.parametrize('kind,text,record,target,mirrors', [
    ('USER', '[pentacle-notice:trusted] wake', 'notice', ROOT, False),
    ('USER', '[pentacle-notice:trusted] lane digest', 'notice', ROOT, False),
    ('USER', '[pentacle-notice:trusted] child report', 'notice', ROOT, False),
    ('USER', '[from peer:seat] [tell:trusted] hi', 'tell', ROOT, False),
    ('TELL', '[from peer:seat] [tell:trusted] hi', 'tell', ROOT, False),
    ('USER', '[from Dot] [send:trusted] hi', 'tell', ROOT, False),
    ('USER', '[from peer:seat] [tell:notice-tell] hi', 'notice', ROOT, False),
    ('USER', '[from peer:seat] [tell:trusted] spoof', None, ROOT, True),
    ('USER', '[pentacle-notice:trusted] spoof', None, ROOT, True),
    ('USER', '[pentacle-notice:trusted] wrong recipient', 'notice', 'other:seat', True),
    ('USER', '[from peer:seat] [tell:trusted] wrong recipient', 'tell', 'other:seat', True),
    ('USER', 'typed directly', None, ROOT, True),
])
def test_trigger_provenance(kind, text, record, target, mirrors, caplog):
    async def go():
        store, composite, _ = await setup()
        try:
            await trust(store, record, target)
            await append(store, kind, text, 'trigger')
            for k in ('SYSTEM', 'THINKING', 'TOOL_USE', 'ASSIST_TEXT'):
                seq = await append(store, k, 'progress ' + k, k)
                if k == 'ASSIST_TEXT':
                    assert bool(await store.assistant_mirror_event_for_source(seq)) == mirrors
            seq = await append(store, 'ASSIST_TEXT', 'final', 'final', phase='final_answer')
            assert bool(await store.assistant_mirror_event_for_source(seq)) == mirrors
            if mirrors:
                assert 'assistant_mirror_unknown_trigger' in caplog.text
        finally:
            await composite.stop()
            store.stop()
    with caplog.at_level('INFO'):
        asyncio.run(go())


# Normalizers strip the peer envelope from TELL text and carry its identity in
# raw.tell_id (claude_jsonl_norm / codex_rollout_norm), so live TELLs never
# show the envelope to the trigger classifier.
@pytest.mark.parametrize('tell_id,record,target,mirrors', [
    ('trusted', 'tell', ROOT, False),
    ('notice-tell', 'notice', ROOT, False),
    ('trusted', None, ROOT, True),
    ('trusted', 'tell', 'other:seat', True),
    (None, 'tell', ROOT, True),
])
def test_normalized_tell_uses_raw_tell_id(tell_id, record, target, mirrors, caplog):
    async def go():
        store, composite, _ = await setup()
        try:
            await trust(store, record, target)
            raw = {'sender': 'peer:seat', 'peer_payload': 'hi', 'enqueued_at': ''}
            if tell_id is not None:
                raw['tell_id'] = tell_id
            await append(store, 'TELL', 'hi', 'trigger', **raw)
            seq = await append(store, 'ASSIST_TEXT', 'final', 'final', phase='final_answer')
            assert bool(await store.assistant_mirror_event_for_source(seq)) == mirrors
            if mirrors:
                assert 'assistant_mirror_unknown_trigger' in caplog.text
        finally:
            await composite.stop()
            store.stop()
    with caplog.at_level('INFO'):
        asyncio.run(go())


@pytest.mark.parametrize('queued', [False, True])
def test_operator_interleaving_and_glyph(queued):
    async def go():
        store, composite, _ = await setup()
        try:
            await trust(store, 'notice')
            await append(store, 'USER', '[pentacle-notice:trusted] wake', 'notice-first')
            assert await store.assistant_mirror_event_for_source(await append(store, 'ASSIST_TEXT', 'notice reply', 'n1')) is None
            route = await _route_for(composite, store, 'operator')
            # Release existing open-route suppression by proving no submit.
            await sql(store, "UPDATE v2_assistant_composite_routes SET delivery_state='failed' WHERE input_identity='operator'")
            wire = json.loads(route['route_json'])['direct_envelope']['wire_body']
            await append(store, 'USER', wire, 'operator-user', subtype='queued-command' if queued else '')
            # Sidechain and other transcript inputs cannot steal the trigger.
            await append(store, 'USER', '[pentacle-notice:trusted] wake', 'side', is_sidechain=True)
            await append(store, 'USER', '[pentacle-notice:trusted] wake', 'other-session', source_session_identity='other')
            seq = await append(store, 'ASSIST_TEXT', 'operator reply', 'o1', phase='final_answer')
            assert await store.assistant_mirror_event_for_source(seq) is not None
            await append(store, 'USER', '[pentacle-notice:trusted] second wake', 'notice-second')
            assert await store.assistant_mirror_event_for_source(await append(store, 'ASSIST_TEXT', 'notice reply 2', 'n2')) is None
            assert await store.assistant_mirror_event_for_source(await append(store, 'ASSIST_TEXT', '•', 'glyph')) is None
            assert [e['text'] for e in await _answer_rows(store)] == ['operator reply']
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(go())


def payload(generation, **changes):
    return {'type': 'assistant.publish', 'request_id': 'proactive-1', 'composite_stream_id': ASSISTANT,
            'publish_kind': 'prose', 'message': 'Milestone',
            '_auth_context': {'token_verified': True, 'stream_id': ROOT, 'session_generation': generation}, **changes}


@pytest.mark.parametrize('kind', ['prose', 'status'])
def test_proactive_idempotent_and_conflict(kind):
    async def go():
        store, composite, generation = await setup()
        try:
            msg = payload(generation, publish_kind=kind)
            first = await composite.publish(msg, actor_stream_id=ROOT)
            retry = await composite.publish(msg, actor_stream_id=ROOT)
            assert first['event_id'] == retry['event_id'] and retry['duplicate']
            with pytest.raises(ValueError, match='assistant_publish_conflict'):
                await composite.publish({**msg, 'message': 'changed'}, actor_stream_id=ROOT)
            assert len(await _answer_rows(store)) == 1
            # Rebinding revokes even a retry from the old seat.
            await sql(store, "INSERT OR REPLACE INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) VALUES('bart','other:seat','other-generation',1,'now')")
            with pytest.raises(ValueError, match='assistant_publish_provenance_unverified'):
                await composite.publish(msg, actor_stream_id=ROOT)
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('case', ['unbound', 'stale', 'other', 'scoped', 'unverified', 'missing-generation', 'attachments', 'question', 'result', 'reply', 'closed'])
def test_proactive_refusals(case):
    async def go():
        store, composite, generation = await setup()
        try:
            msg, actor = payload(generation), ROOT
            if case == 'unbound':
                composite.env_config = replace(composite.env_config, direct_primary_stream_id='', direct_primary_generation='')
            elif case == 'stale': msg['_auth_context']['session_generation'] = 'stale'
            elif case == 'other': actor = 'other:seat'; msg['_auth_context']['stream_id'] = actor
            elif case == 'scoped': msg['_auth_context']['scoped_principal'] = True
            elif case == 'unverified': msg['_auth_context']['token_verified'] = False
            elif case == 'missing-generation': msg['_auth_context'].pop('session_generation')
            elif case == 'attachments': msg['attachment_ids'] = ['file']
            elif case in ('question', 'result'): msg['publish_kind'] = case
            elif case == 'reply': msg['reply_to_message_id'] = 'input'
            elif case == 'closed': await sql(store, "UPDATE sessions SET status='closed' WHERE host='fixture-root'")
            with pytest.raises(ValueError):
                await composite.publish(msg, actor_stream_id=actor)
            assert await _answer_rows(store) == []
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('duration,expected', [(4.12345, 4.123), (600, 600.0), (0.0004, None), (0.0005, 0.001), (0, None), (-1, None), (601, None), (float('nan'), None), (float('inf'), None), (True, None), ('4', None)])
def test_composite_voice_receipt_and_event(duration, expected):
    async def go():
        store, composite, _ = await setup()
        try:
            await composite.accept_input({'message': 'voice transcript', 'msg_id': 'voice', 'request_id': 'voice',
                'meta': {'voice': {'duration_s': duration, 'ignored': 1}, 'ignored': 2}}, operator_principal='operator:test')
            receipt = await store.get_send_receipt(ASSISTANT, 'voice')
            event = next(e for e in await store.fetch_session_event_tail(ASSISTANT, limit=20) if e['kind'] == 'USER')
            voice = {} if expected is None else {'voice': {'duration_s': expected}}
            assert receipt['meta'] == {'assistant_composite': True, **voice}
            assert event.get('meta', {}) == voice
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(go())


def test_proactive_rechecks_binding_in_commit_transaction(monkeypatch):
    async def go():
        store, composite, generation = await setup()
        try:
            original = store.record_assistant_composite_publication
            async def rebind_before_commit(**kwargs):
                await sql(store, "INSERT OR REPLACE INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) VALUES('bart','other:seat','other-generation',1,'now')")
                return await original(**kwargs)
            monkeypatch.setattr(store, 'record_assistant_composite_publication', rebind_before_commit)
            with pytest.raises(ValueError, match='assistant_publish_provenance_unverified'):
                await composite.publish(payload(generation), actor_stream_id=ROOT)
            assert await _answer_rows(store) == []
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('verified', [True, False])
def test_server_proactive_auth_boundary(verified):
    from server import Server
    from sessions import Sessions
    async def go():
        store, composite, generation = await setup()
        server = Server(store=store, sessions=Sessions(store, tmux=None, local_host='fixture-chat'), local_host='fixture-chat')
        server.assistant_composite = composite
        try:
            msg = payload(generation)
            msg['_auth_context']['token_verified'] = verified
            if verified:
                assert (await server._on_assistant_publish(msg))['type'] == 'assistant.publish.ok'
            else:
                with pytest.raises(Exception) as caught:
                    await server._on_assistant_publish(msg)
                assert caught.value.code == 'assistant_publish_provenance_unverified'
        finally:
            await server.close()
            await composite.stop()
            store.stop()
    asyncio.run(go())
