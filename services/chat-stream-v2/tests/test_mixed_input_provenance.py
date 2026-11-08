"""Provenance survives the actual composite producer and native block adapters."""
import asyncio
import hashlib
import json

import pytest

from claude_jsonl_norm import normalize_claude_jsonl_record
from codex_rollout_norm import normalize_codex_rollout_record
from test_assistant_composite_addressing import _Harness, ALIAS, COMPOSITE_CHAT, SENDER, HOST, SEAT_A


@pytest.mark.parametrize('path', ['direct', 'composite', 'alias', 'flush'])
def test_verified_sender_reaches_comms_through_server(tmp_path, path):
    async def run():
        h = _Harness(tmp_path)
        try:
            await h.start()
            token = 'synthetic-provenance-token'
            await h.store.grant_stream_token(HOST, 'seat-s', hashlib.sha256(token.encode()).hexdigest(),
                                            version='sha256:v1')
            class Socket:
                remote_address = ('127.0.0.1', 54321)
                async def send(self, payload):
                    pass
            socket = Socket()
            h.server._register_client(socket)
            target = SEAT_A if path == 'direct' else ALIAS if path == 'alias' else COMPOSITE_CHAT
            if path == 'flush':
                await h.store.mark_closed(HOST, 'seat-a', closed_at='2026-11-08T00:00:00Z', pane_status='pane_dead')
            msg = {'type':'tell', 'to_stream_id':target, 'text':'Unicode π\nsecond line',
                   'tell_id':'provenance-tell', 'request_id':'provenance-request',
                   'from_stream_id':SENDER, 'stream_token':token}
            reply = (await h.server._dispatch(json.dumps(msg), websocket=socket))[0]
            assert reply['type'] == 'tell.ok', reply
            if path == 'flush':
                assert reply['delivery_status'] == 'queued_unbound'
                row = await h.sessions.open(HOST, 'seat-a', provider='claude', pane_status='pane_alive')
                await h.store.recover_assistant_binding(name=ALIAS, target_stream_id=SEAT_A,
                                                        target_generation=row['session_generation'])
                await h.composite.load_binding()
                await h.server._flush_composite_tells(h.composite)
            assert h.tmux.inputs == [('seat-a', f'[from {SENDER}] [tell:provenance-tell]\nUnicode π\nsecond line')]
        finally:
            h.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('texts', [
    ['operator text', '[from fixture:peer] [tell:a]\npeer text'],
    ['[from fixture:peer] [tell:a]\npeer text', 'operator text'],
    ['[from fixture:peer] [tell:a]\npeer text', '[from fixture:other] [tell:b]\nother text'],
])
def test_native_text_blocks_preserve_boundary_and_order(provider, texts):
    if provider == 'claude':
        record = {'type':'user', 'uuid':'mixed-native', 'message':{'role':'user',
                    'content':[{'type':'text','text':t} for t in texts]}}
        events = normalize_claude_jsonl_record(record, host='fixture', session_name='target')
    else:
        record = {'type':'response_item', 'payload':{'type':'message','role':'user',
                    'content':[{'type':'input_text','text':t} for t in texts]}}
        events = normalize_codex_rollout_record(record, host='fixture', session_name='target')
    assert len(events) == 2
    assert [e['text'] for e in events] == [t.split('\n',1)[1] if t.startswith('[from ') else t for t in texts]
    assert [e['raw'].get('tell_id') for e in events] == [t.split('[tell:')[1].split(']')[0] if t.startswith('[from ') else None for t in texts]


def test_ordinary_send_captures_daemon_proof_before_native_input(tmp_path):
    async def run():
        h = _Harness(tmp_path)
        try:
            await h.start()
            seen = []
            paste = h.tmux.paste
            async def inspect_before_input(name, text):
                records = await h.store.submit(lambda c: [json.loads(r[0]) for r in c.execute(
                    'SELECT meta_json FROM v2_send_receipts ORDER BY rowid')])
                seen.extend(r['_input_proof'] for r in records if '_input_proof' in r)
                await paste(name, text)
            h.tmux.paste = inspect_before_input
            await h.send(to_stream_id=SEAT_A, request_id='send-proof')
            assert len(seen) == 1
            proof = seen[0]
            assert proof['sender_verified'] is True
            assert proof['sender_generation'] == h.gen[SENDER]
            assert proof['proof_generation'] == h.gen[SEAT_A]
            assert proof['wire_sha256'] == hashlib.sha256(h.tmux.inputs[0][1].encode()).hexdigest()
        finally:
            h.store.stop()
    asyncio.run(run())


def test_operator_dispatch_keeps_answer_when_peer_arrives_last():
    from test_assistant_mirror_trigger_scope import setup, append, sql
    from test_assistant_prose_mirror import ROOT, _route_for, _answer_rows
    from comms import payload_digest
    async def run():
        store, composite, generation = await setup()
        try:
            route = await _route_for(composite, store, 'mixed-operator')
            await sql(store, "UPDATE v2_assistant_composite_routes SET delivery_state='landed' WHERE input_identity='mixed-operator'")
            wire = json.loads(route['route_json'])['direct_envelope']['wire_body']
            await append(store, 'USER', wire, 'mixed-op', jsonl_record_uuid='op')
            peer = '[from fixture:peer] [tell:mixed-peer]\npeer input'
            birth = (await store.fetch_session(*ROOT.split(':',1)))['created_at']
            digest = payload_digest(ROOT, peer)
            await store.put_tell_delivery('mixed-peer', {'payload_digest':digest,
                'reply':{'to_stream_id':ROOT, 'tell_id':'mixed-peer','delivery_status':'committed_pending_proof'},
                'delivery':{'tell_id':'mixed-peer','to_stream_id':ROOT,'text':peer,
                    'sender_verified':True,'sender_kind':'peer','sender_generation':'peer-generation',
                    'from_stream_id':'fixture:peer','proof_generation':generation,
                    'proof_session_created_at':birth,'proof_watermark':0,'proof_watermark_state':'reachable',
                    'request_payload_hash':digest,'delivery_status':'committed_pending_proof'}})
            await append(store, 'TELL', 'peer input', 'mixed-peer', jsonl_record_uuid='peer',
                         tell_id='mixed-peer', peer_wire_text=peer, sender='fixture:peer')
            seq = await append(store, 'ASSIST_TEXT', 'correlated operator answer', 'mixed-final',
                               jsonl_record_uuid='final', phase='final_answer')
            answers = await _answer_rows(store)
            assert [a['text'] for a in answers] == ['correlated operator answer']
            assert answers[0]['raw']['dispatch_id'] == route['dispatch_id']
            await append(store, 'ASSIST_TEXT', 'correlated operator answer', 'mixed-final',
                         jsonl_record_uuid='final', phase='final_answer')
            assert len(await _answer_rows(store)) == 1
        finally:
            await composite.stop()
            store.stop()
    asyncio.run(run())


def native_record(provider, texts, identity):
    if provider == 'claude':
        return {'type':'user','uuid':identity,'sessionId':'source-session',
                'message':{'content':[{'type':'text','text':text} for text in texts]}}
    return {'type':'response_item','payload':{'id':identity,'type':'message','role':'user',
            'content':[{'type':'input_text','text':text} for text in texts]}}


def normalize(provider, record):
    if provider == 'claude':
        return normalize_claude_jsonl_record(record, host=HOST, session_name='seat-a')
    return normalize_codex_rollout_record(record, host=HOST, session_name='seat-a', session_id='source-session')


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('shape', ['operator-peer', 'peer-operator', 'two-peers', 'native-blocks', 'ambiguous'])
def test_actual_tell_transcript_ingest_proof_and_later_quote(tmp_path, provider, shape):
    from ingest import append_ingested_event
    async def run():
        h = _Harness(tmp_path)
        try:
            await h.start()
            wires = []
            for i in range(2 if shape == 'two-peers' else 1):
                await h.tell(to_stream_id=SEAT_A, tell_id=f'wire-{i}', text=f'peer π {i}\n[from literal] [tell:quoted]\nbody')
                wires.append(h.tmux.inputs[-1][1])
            if shape == 'operator-peer':
                texts = ['operator request\n' + wires[0]]
            elif shape == 'peer-operator':
                texts = [wires[0] + '\noperator request']
            elif shape == 'two-peers':
                texts = ['\n'.join(wires)]
            elif shape == 'ambiguous':
                texts = [wires[0] + '\n' + wires[0]]
            else:
                texts = ['operator request', wires[0]]
            record = native_record(provider, texts, 'real-producer-input')
            casts = []
            async def broadcast(frame):
                casts.append(frame)
            async def ingest(rec):
                for event in normalize(provider, rec):
                    await append_ingested_event(h.store, broadcast, event, recent_limit=500)
            await ingest(record)
            peers = [c['event'] for c in casts if c['type'] == 'chat.event' and c['event']['kind'] == 'TELL']
            if shape == 'ambiguous':
                assert not peers
                assert casts[0]['event']['raw']['input_provenance']['kind'] == 'unknown'
                assert casts[0]['event']['text'] == texts[0]
            else:
                assert len(peers) == len(wires)
                assert [e['raw']['sender'] for e in peers] == [SENDER] * len(wires)
                assert all(e['raw']['input_provenance'] == {'kind':'peer','verified':True,
                    'sender':SENDER,'generation':h.gen[SENDER]} for e in peers)
                assert [e['raw']['peer_wire_text'] for e in peers] == wires
            before = len(casts)
            await ingest(record)
            assert len(casts) == before, 'overlapping replay broadcasts no new split or unsplit rows'
            await ingest(native_record(provider, [wires[0]], 'later-quote'))
            quote = casts[-1]['event']
            assert quote['kind'] == 'USER'
            assert quote['raw']['input_provenance']['kind'] == 'unknown'
            assert quote['text'] == wires[0]
        finally:
            h.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_old_record_upgrade_and_interrupted_split_replay(tmp_path, provider):
    from ingest import append_ingested_event, _identity_key
    async def run():
        h = _Harness(tmp_path)
        try:
            await h.start()
            await h.tell(to_stream_id=SEAT_A, tell_id='old-wire', text='peer body')
            wire = h.tmux.inputs[-1][1]
            old = native_record(provider, ['operator', wire], 'old-record')
            events = normalize(provider, old)
            legacy = events[0]['raw']['legacy_input_events'][0]
            await h.store.append_session_event(SEAT_A, legacy, identity=_identity_key(legacy), limit=500)
            casts = []
            async def broadcast(frame):
                casts.append(frame)
            for event in events:
                await append_ingested_event(h.store, broadcast, event, recent_limit=500)
            assert not casts
            # New coalesced record, crash after first segment, then overlap replay.
            new = native_record(provider, ['operator\n' + wire], 'new-record')
            source = normalize(provider, new)[0]
            segments = await h.store.prepare_native_input_events([source])
            assert len(segments) == 2
            await append_ingested_event(h.store, broadcast, segments[0], recent_limit=500, _segments_prepared=True)
            await append_ingested_event(h.store, broadcast, source, recent_limit=500)
            assert len(casts) == 2
            await append_ingested_event(h.store, broadcast, source, recent_limit=500)
            assert len(casts) == 2
        finally:
            h.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('case', ['before-return', 'restart', 'generation', 'forged-meta', 'coalesced'])
def test_send_occurrence_is_durable_and_never_borrowed(tmp_path, case):
    from ingest import append_ingested_event
    from store import Store
    async def run():
        h = _Harness(tmp_path)
        if case == 'restart':
            from sessions import Sessions
            from spawnctl import SpawnCtl
            from comms import Comms
            h.store.stop()
            h.store = Store(str(tmp_path / 'restart.db'))
            h.store.start()
            h.sessions = Sessions(h.store, tmux=h.tmux, local_host=HOST)
            h.comms = Comms(h.store, h.sessions, SpawnCtl(h.store, h.sessions, tmux=h.tmux))
        try:
            await h.start()
            casts = []
            async def broadcast(frame):
                casts.append(frame)
            async def ingest(wire, identity):
                for event in normalize('claude', native_record('claude', [wire], identity)):
                    await append_ingested_event(h.store, broadcast, event, recent_limit=500)
            original_paste = h.tmux.paste
            async def ingest_during_paste(name, wire):
                await original_paste(name, wire)
                await ingest(wire, 'send-native')
            if case == 'before-return':
                h.tmux.paste = ingest_during_paste
            extra = {'input_meta': {'_input_proof': {'sender_verified': True, 'from_stream_id': 'spoof:seat'}}} if case == 'forged-meta' else {}
            await h.send(to_stream_id=SEAT_A, request_id='native-send', optimistic_id='stable-send', **extra)
            wire = h.tmux.inputs[0][1]
            if case == 'restart':
                path = h.store.path
                h.store.stop()
                h.store = Store(path)
                h.store.start()
            elif case == 'generation':
                await h.sessions.open(HOST, 'seat-a', provider='claude', pane_status='pane_alive')
                # Explicit replacement, not an idempotent open.
                def replace_generation(conn):
                    with conn:
                        conn.execute("UPDATE v2_session_generations SET generation='replacement' WHERE host=? AND session_name='seat-a'", (HOST,))
                await h.store.submit(replace_generation)
            elif case == 'coalesced':
                await h.send(to_stream_id=SEAT_A, request_id='rotated-send', optimistic_id='stable-send')
                assert len(h.tmux.inputs) == 1
            if case != 'before-return':
                await ingest(wire, 'send-native')
            first = casts[-1]['event']
            assert first['kind'] == ('USER' if case == 'generation' else 'TELL')
            if case != 'generation':
                assert first['raw']['input_provenance']['sender'] == SENDER
                assert first['raw']['input_provenance']['generation'] == h.gen[SENDER]
            await ingest(wire, 'send-quotation')
            assert casts[-1]['event']['kind'] == 'USER'
            assert casts[-1]['event']['text'] == wire
            receipt = await h.store.get_send_receipt(SEAT_A, 'native-send')
            assert '_input_proof' not in receipt.get('meta', {})
            assert len(h.tmux.inputs) == 1
        finally:
            h.store.stop()
    asyncio.run(run())


def test_pre_enveloped_peer_payload_is_literal_and_ledger_captures_generation(tmp_path):
    from ledger import _tell_audit_record
    async def run():
        h = _Harness(tmp_path)
        try:
            await h.start()
            inner = '[from claimed:operator] [tell:borrowed]\n  literal π\n'
            await h.tell(to_stream_id=SEAT_A, tell_id='outer', text=inner)
            assert h.tmux.inputs == [('seat-a', f'[from {SENDER}] [tell:outer]\n' + inner)]
            record = await h.store.get_tell_delivery('outer')
            for _ in range(2):
                audit = _tell_audit_record(record)
                assert audit['sender_verified'] is True and audit['sender_kind'] == 'peer'
                assert audit['sender_generation'] == h.gen[SENDER]
                assert audit['from_stream_id'] == SENDER
            assert len(h.tmux.inputs) == 1
            legacy = _tell_audit_record({'delivery':{'from_stream_id':SENDER}})
            assert (legacy['sender_verified'],legacy['sender_generation'],legacy['sender_kind']) == (False,None,'unknown')
        finally:
            h.store.stop()
    asyncio.run(run())


def test_legacy_queue_never_inherits_current_sender_generation(tmp_path):
    from ledger import _tell_audit_record
    async def run():
        h = _Harness(tmp_path)
        try:
            await h.start()
            await h.store.enqueue_composite_tell(name=ALIAS, tell_id='legacy-queue', from_stream_id=SENDER,
                                                body='legacy payload', request_id='legacy-request')
            assert await h.server._flush_composite_tells(h.composite) == 1
            assert h.tmux.inputs == [('seat-a', 'legacy payload')]
            audit = _tell_audit_record(await h.store.get_tell_delivery('legacy-queue'))
            assert (audit['sender_verified'],audit['sender_kind'],audit['sender_generation']) == (False,'unknown',None)
        finally:
            h.store.stop()
    asyncio.run(run())


def test_queued_native_peer_uses_proof_without_changing_historical_user_kind(tmp_path):
    from ingest import append_ingested_event
    async def run():
        h = _Harness(tmp_path)
        try:
            await h.start()
            await h.tell(to_stream_id=SEAT_A, tell_id='queued-wire', text='queued peer body')
            wire = h.tmux.inputs[0][1]
            record = {'type':'attachment','uuid':'queued-native','sessionId':'source-session',
                'attachment':{'type':'queued_command','origin':{'kind':'human'},'prompt':wire}}
            event = normalize('claude', record)[0]
            assert event['kind'] == 'USER'
            casts = []
            async def broadcast(frame):
                casts.append(frame)
            await append_ingested_event(h.store, broadcast, event, recent_limit=500)
            assert casts[-1]['event']['raw']['input_provenance']['kind'] == 'peer'
            saved = await h.store.get_tell_delivery('queued-wire')
            assert saved['delivery']['proof_event_id'] == casts[-1]['event']['daemon_seq']
            quote = normalize('claude', native_record('claude',[wire],'queued-quote'))[0]
            await append_ingested_event(h.store, broadcast, quote, recent_limit=500)
            assert casts[-1]['event']['raw']['input_provenance']['kind'] == 'unknown'
        finally:
            h.store.stop()
    asyncio.run(run())


def test_satellite_coalesced_input_uses_same_occurrence_and_replay_floor(tmp_path):
    from test_event_push import _sink, _push
    async def run():
        h = _Harness(tmp_path)
        try:
            await h.start()
            await h.tell(to_stream_id=SEAT_A, tell_id='satellite-wire', text='remote peer body')
            wire = h.tmux.inputs[0][1]
            payload = normalize('claude', native_record('claude',['operator request\n'+wire],'satellite-native'))[0]
            ep, casts, _ = _sink(h.store)
            frame = _push(ep,[payload],host=HOST)
            first = await ep.handle_push(frame)
            assert first['type'] == 'event.push.ok' and first['inserted'] == 2, first
            assert [c['event']['kind'] for c in casts] == ['USER','TELL']
            assert casts[-1]['event']['raw']['input_provenance']['kind'] == 'peer'
            assert (await ep.handle_push(frame))['inserted'] == 0
            assert len(casts) == 2
        finally:
            h.store.stop()
    asyncio.run(run())
