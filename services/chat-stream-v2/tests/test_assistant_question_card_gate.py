"""Installed prompt.ask websocket path for the direct primary."""
import asyncio
import hashlib
import json

import websockets

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from notify import Notify
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION


def test_direct_primary_prompt_ask_is_held_over_websocket(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'sessions.db'))
        store.start()
        server = notify = composite = None
        try:
            root = await store.open_session('fixture-root', 'primary', provider='codex', visibility='hidden')
            advisor = await store.open_session('fixture-advisor', 'astra', provider='codex', pane_status='pane_alive')
            visible = await store.open_session('fixture-visible', 'seat', provider='codex', visibility='visible')
            advisor2 = await store.open_session('fixture-advisor', 'other', provider='codex', pane_status='pane_alive')
            token = 'fixture-token'
            await store.grant_stream_token('fixture-root', 'primary', hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            config = AssistantCompositeConfig.from_env({
                'PENTACLE_ASSISTANT_COMPOSITE_ENABLED': '1',
                'PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID': 'fixture-chat:assistant',
                'PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID': 'fixture-root:primary',
                'PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION': root['session_generation'],
                'PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID': 'fixture-advisor:astra',
            })
            composite = AssistantComposite(store, config=config)
            await composite.ensure_projection()
            sessions = Sessions(store, local_host='fixture-chat')
            await sessions.refresh()
            server = Server(host='127.0.0.1', port=0, store=store, sessions=sessions, local_host='fixture-chat')
            server.assistant_composite = composite
            notify = Notify(str(tmp_path / 'notifications.db'), sessions=sessions, notice_store=store,
                            assistant_binding=composite.binding, assistant_stream_id=config.stream_id,
                            lane_rulings=server.lane_rulings,
                            broadcast=server.broadcast)
            server.notify = notify
            server.handlers.update(notify.wire_handlers())
            await notify.start()
            port = await server.bind()
            async with websockets.connect(f'ws://127.0.0.1:{port}') as socket:
                assert json.loads(await socket.recv())['type'] == 'welcome'
                await socket.send(json.dumps({'type': 'hello', 'client': 'agent-orch', 'stream_token': token,
                                              'from_stream_id': 'fixture-root:primary'}))
                while json.loads(await socket.recv())['type'] != 'snapshot':
                    pass
                payload = {'type': 'prompt.ask', 'request_id': 'ask-1', 'from_stream_id': 'fixture-root:primary',
                           'envelope': {'schema_version': 1, 'question_id': 'fixture-q', 'title': 'Fixture',
                                        'body': 'Proceed?', 'dedup_key': 'fixture-q',
                                        'producer_stream_id': 'fixture-root:primary', 'response_mode': 'free_text',
                                        'options': []}, 'actions': [], 'severity': 'info'}
                await socket.send(json.dumps(payload))
                async with asyncio.timeout(5):
                    while True:
                        reply = json.loads(await socket.recv())
                        if reply.get('request_id') == 'ask-1':
                            break
                question = await notify._db.call('get_agent_question', 'fixture-q')
                count = await store.submit(lambda conn: conn.execute(
                    "SELECT count(*) FROM v2_assistant_lane_rulings WHERE action='question'").fetchone()[0])
                assert reply['type'] == 'prompt.ask.held', (reply['type'], bool(question), count)
                assert question is None
                assert count == 1
                initial_row = await server.lane_rulings._fetch(reply['ruling_request_id'])
                packet_text = json.loads(initial_row['intent_json'])['_ruling_packet']
                packet = json.loads(packet_text.split('\n', 1)[1])
                assert packet['action'] == 'question' and packet['covered'] is True
                assert packet['question']['question_id'] == 'fixture-q'
                assert packet['work_item'] is None and packet['work_item_error'] == 'no_spec'
                assert packet['recent_cards'] == {'open': 0, 'last_24h': 0}
                listing = await server.lane_rulings.latest_for_target('fixture-root:primary')
                assert listing['held_questions'][0]['question_id'] == 'fixture-q'
                await socket.send(json.dumps({'type': 'prompt.status', 'request_id': 'status-held', 'question_id': 'fixture-q'}))
                async with asyncio.timeout(5):
                    while True:
                        held_status = json.loads(await socket.recv())
                        if held_status.get('request_id') == 'status-held':
                            break
                assert held_status['state'] == 'held' and held_status['ruling_state'] == 'pending'
                decision = await server._on_assistant_ruling({
                    'request_id': 'approve-1', 'ruling_request_id': reply['ruling_request_id'],
                    'ruling': 'approve', '_auth_context': {'token_verified': True,
                    'stream_id': 'fixture-advisor:astra', 'session_generation': advisor['session_generation']}})
                assert decision['state'] == 'done', decision
                question = await notify._db.call('get_agent_question', 'fixture-q')
                assert question['envelope']['body'] == 'Proceed?'
                await socket.send(json.dumps({**payload, 'request_id': 'ask-replay'}))
                async with asyncio.timeout(5):
                    while True:
                        replay = json.loads(await socket.recv())
                        if replay.get('request_id') == 'ask-replay':
                            break
                assert replay['type'] == 'prompt.ask.ok'
                assert replay['question']['question_id'] == 'fixture-q'
                auth = {'token_verified': True, 'stream_id': 'fixture-root:primary',
                        'session_generation': root['session_generation']}
                visible_id = 'fixture-visible:seat'
                visible_ask = {**payload, 'from_stream_id': visible_id,
                    '_auth_context': {'token_verified': True, 'stream_id': visible_id, 'session_generation': visible['session_generation']},
                    'envelope': {**payload['envelope'], 'question_id': 'visible-q', 'dedup_key': 'visible-q', 'producer_stream_id': visible_id}}
                assert (await notify.prompt(visible_ask))['type'] == 'prompt.ask.ok'
                visible_cancel = await notify.prompt({'type': 'prompt.cancel', 'question_id': 'visible-q', '_auth_context': visible_ask['_auth_context'], 'from_stream_id': visible_id})
                assert visible_cancel['type'] == 'prompt.cancel.ok'
                proxy_ask = {**payload, '_auth_context': {**auth, 'assistant_composite_question_proxy': True},
                    'envelope': {**payload['envelope'], 'question_id': 'proxy-q', 'dedup_key': 'proxy-q', '_assistant_composite_question_proxy': True}}
                assert (await notify.prompt(proxy_ask))['type'] == 'prompt.ask.ok'
                ask2 = {**payload, '_auth_context': auth, 'request_id': 'ask-2',
                        'envelope': {**payload['envelope'], 'question_id': 'fixture-q2', 'dedup_key': 'fixture-q2'}}
                held = await notify.prompt(ask2)
                assert held['type'] == 'prompt.ask.held'
                status = await notify.prompt({'type': 'prompt.status', 'question_id': 'fixture-q2', '_auth_context': auth})
                assert status['state'] == 'held' and status['ruling_state'] == 'pending'
                withdrawn = await notify.prompt({'type': 'prompt.cancel', 'question_id': 'fixture-q2', '_auth_context': auth})
                assert withdrawn['state'] == 'withdrawn'
                terminal = await notify.prompt({'type': 'prompt.cancel', 'question_id': 'fixture-q2', '_auth_context': auth})
                assert terminal['error_code'] == 'question_terminal_state' and terminal['state'] == 'withdrawn'
                assert (await notify.prompt(ask2))['ruling_state'] == 'withdrawn'
                ask_denied = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-deny', 'dedup_key': 'fixture-deny'}}
                denied = await notify.prompt(ask_denied)
                decision = await server._on_assistant_ruling({'request_id': 'deny', 'ruling_request_id': denied['ruling_request_id'],
                    'ruling': 'deny', 'reason': 'Known answer', '_auth_context': {'token_verified': True,
                    'stream_id': 'fixture-advisor:astra', 'session_generation': advisor['session_generation']}})
                assert decision['state'] == 'denied'
                assert await notify._db.call('get_agent_question', 'fixture-deny') is None
                assert (await notify.prompt(ask_denied))['reason'] == 'Known answer'
                changed = {**ask_denied, 'envelope': {**ask_denied['envelope'], 'body': 'Changed'}}
                assert (await notify.prompt(changed))['error_code'] == 'assistant_ruling_intent_changed'
                ask_revised = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-revise', 'dedup_key': 'fixture-revise'}}
                revised = await notify.prompt(ask_revised)
                decision = await server._on_assistant_ruling({'request_id': 'revise', 'ruling_request_id': revised['ruling_request_id'],
                    'ruling': 'revise', 'reason': 'Add context', '_auth_context': {'token_verified': True,
                    'stream_id': 'fixture-advisor:astra', 'session_generation': advisor['session_generation']}})
                assert decision['state'] == 'revised'
                assert await notify._db.call('get_agent_question', 'fixture-revise') is None
                row = await server.lane_rulings.question_row('fixture-root:primary', root['session_generation'], 'fixture-revise')
                assert row['linked_from'] is None
                ask_rebind = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-rebind', 'dedup_key': 'fixture-rebind'}}
                rebound = await notify.prompt(ask_rebind)
                await server.lane_rulings.configure('fixture-advisor:other', actor_stream_id='fixture-root:primary', actor_generation=root['session_generation'])
                rebound_row = await server.lane_rulings._fetch(rebound['ruling_request_id'])
                assert (rebound_row['authority_stream_id'], rebound_row['authority_generation']) == ('fixture-advisor:other', advisor2['session_generation'])
                await server.lane_rulings.configure('fixture-advisor:astra', actor_stream_id='fixture-root:primary', actor_generation=root['session_generation'])
                assert (await notify.prompt({'type': 'prompt.cancel', 'question_id': 'fixture-rebind', '_auth_context': auth}))['state'] == 'withdrawn'
                await store.update_session('fixture-advisor', 'astra', pane_status='pane_dead')
                ask_unavailable = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-unavailable', 'dedup_key': 'fixture-unavailable'}}
                unavailable = await notify.prompt(ask_unavailable)
                await server.lane_rulings.tick()
                assert (await server.lane_rulings._fetch(unavailable['ruling_request_id']))['state'] == 'pending'
                assert await notify._db.call('get_agent_question', 'fixture-unavailable') is None
                await store.update_session('fixture-advisor', 'astra', pane_status='pane_alive')
                decision = await server._on_assistant_ruling({'request_id': 'approve-unavailable', 'ruling_request_id': unavailable['ruling_request_id'],
                    'ruling': 'approve', '_auth_context': {'token_verified': True,
                    'stream_id': 'fixture-advisor:astra', 'session_generation': advisor['session_generation']}})
                assert decision['state'] == 'done'
                ask_late = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-late', 'dedup_key': 'fixture-late'}}
                late = await notify.prompt(ask_late)
                await store.submit(lambda conn: (conn.execute("UPDATE v2_assistant_lane_rulings SET deadline=0 WHERE ruling_request_id=?", (late['ruling_request_id'],)), conn.commit()))
                await server.lane_rulings.tick()
                await server.lane_rulings.tick()
                assert (await server.lane_rulings._fetch(late['ruling_request_id']))['state'] == 'pending'
                assert await notify._db.call('get_agent_question', 'fixture-late') is None
                overdue = await store.submit(lambda conn: conn.execute("SELECT count(*) FROM v2_outbound_notices WHERE notice_id LIKE ? AND notice_id LIKE '%overdue%'", ('%' + late['ruling_request_id'] + '%',)).fetchone()[0])
                assert overdue == 2
                decision = await server._on_assistant_ruling({'request_id': 'approve-late', 'ruling_request_id': late['ruling_request_id'],
                    'ruling': 'approve', '_auth_context': {'token_verified': True,
                    'stream_id': 'fixture-advisor:astra', 'session_generation': advisor['session_generation']}})
                assert decision['state'] == 'done'
                ask_crash = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-crash', 'dedup_key': 'fixture-crash'}}
                crash = await notify.prompt(ask_crash)
                await store.submit(lambda conn: (conn.execute("UPDATE v2_assistant_lane_rulings SET state='approved', ruling='approve' WHERE ruling_request_id=?", (crash['ruling_request_id'],)), conn.commit()))
                crash_row = await server.lane_rulings._fetch(crash['ruling_request_id'])
                frozen = json.loads(crash_row['intent_json'])
                frozen.pop('_ruling_packet')
                broadcasts = []
                original_broadcast = notify._broadcast
                async def record_broadcast(frame):
                    broadcasts.append(frame)
                    await original_broadcast(frame)
                notify._broadcast = record_broadcast
                published = await notify._prompt_publish(frozen, request_id='before-crash')
                assert published['type'] == 'prompt.ask.ok' and len(broadcasts) == 1
                await server.lane_rulings.tick()
                assert len(broadcasts) == 1
                assert (await server.lane_rulings._fetch(crash['ruling_request_id']))['state'] == 'done'
                notify._broadcast = original_broadcast
                await store.update_session('fixture-root', 'primary', assigned_spec_id='spec_demo__first')
                await sessions.refresh()
                ask_assigned = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-assigned', 'dedup_key': 'fixture-assigned'}}
                assigned = await notify.prompt(ask_assigned)
                await store.update_session('fixture-root', 'primary', assigned_spec_id='spec_demo__second')
                await sessions.refresh()
                assert (await notify.prompt(ask_assigned))['ruling_request_id'] == assigned['ruling_request_id']
                explicit_change = {**ask_assigned, 'envelope': {**ask_assigned['envelope'], 'spec_id': 'spec_demo__second'}}
                assert (await notify.prompt(explicit_change))['error_code'] == 'assistant_ruling_intent_changed'
                assigned_row = await server.lane_rulings.question_row('fixture-root:primary', root['session_generation'], 'fixture-assigned')
                assert json.loads(assigned_row['intent_json'])['envelope']['spec_id'] == 'spec_demo__first'
                decision = await server._on_assistant_ruling({'request_id': 'approve-assigned', 'ruling_request_id': assigned['ruling_request_id'],
                    'ruling': 'approve', '_auth_context': {'token_verified': True,
                    'stream_id': 'fixture-advisor:astra', 'session_generation': advisor['session_generation']}})
                assert decision['state'] == 'done'
                assert (await notify.prompt(ask_assigned))['question']['spec_id'] == 'spec_demo__first'
                ask_block = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-block', 'dedup_key': 'fixture-block'}}
                blocked = await notify.prompt(ask_block)
                await store.submit(lambda conn: (conn.execute("UPDATE v2_assistant_lane_rulings SET state='approved', ruling='approve' WHERE ruling_request_id=?", (blocked['ruling_request_id'],)), conn.commit()))
                original_fetch = store.fetch_session
                async def changed_generation(host, name):
                    found = await original_fetch(host, name)
                    return {**found, 'session_generation': 'changed'} if (host, name) == ('fixture-root', 'primary') else found
                store.fetch_session = changed_generation
                await server.lane_rulings._release(await server.lane_rulings._fetch(blocked['ruling_request_id']))
                store.fetch_session = original_fetch
                blocked_row = await server.lane_rulings._fetch(blocked['ruling_request_id'])
                assert blocked_row['state'] == 'release_blocked'
                assert json.loads(blocked_row['outcome_json'])['error'] == 'requester_generation_changed'
                assert await notify._db.call('get_agent_question', 'fixture-block') is None
                ask_race = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-race', 'dedup_key': 'fixture-race'}}
                race = await notify.prompt(ask_race)
                await store.submit(lambda conn: (conn.execute("UPDATE v2_assistant_lane_rulings SET state='approved', ruling='approve' WHERE ruling_request_id=?", (race['ruling_request_id'],)), conn.commit()))
                race_cancel = await notify.prompt({'type': 'prompt.cancel', 'question_id': 'fixture-race', '_auth_context': auth})
                assert race_cancel['error_code'] == 'question_release_pending'
                await server.lane_rulings.tick()
                assert (await notify._db.call('get_agent_question', 'fixture-race'))['state'] == 'open'
                ask_recover = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-recover', 'dedup_key': 'fixture-recover'}}
                recover = await notify.prompt(ask_recover)
                await store.submit(lambda conn: (conn.execute("UPDATE v2_assistant_lane_rulings SET state='unruled_disabled', reason='authority_disabled' WHERE ruling_request_id=?", (recover['ruling_request_id'],)), conn.commit()))
                in_flight = await notify.prompt(ask_recover)
                assert in_flight['ruling_state'] == 'unruled_disabled' and in_flight['publishing'] is True
                await server.lane_rulings.tick()
                assert (await notify.prompt(ask_recover))['type'] == 'prompt.ask.ok'
                ask_pending = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-disable', 'dedup_key': 'fixture-disable'}}
                held_pending = await notify.prompt(ask_pending)
                assert held_pending['ruling_state'] == 'pending'
                await server.lane_rulings.configure('disabled', actor_stream_id='fixture-root:primary', actor_generation=root['session_generation'])
                disabled_row = await server.lane_rulings.question_row('fixture-root:primary', root['session_generation'], 'fixture-disable')
                assert disabled_row['state'] == 'done' and disabled_row['reason'] == 'authority_disabled'
                assert await notify._db.call('get_agent_question', 'fixture-disable') is not None
                ask3 = {**ask2, 'envelope': {**ask2['envelope'], 'question_id': 'fixture-q3', 'dedup_key': 'fixture-q3'}}
                bypass = await notify.prompt(ask3)
                assert bypass['type'] == 'prompt.ask.ok', bypass
                row = await server.lane_rulings.question_row('fixture-root:primary', root['session_generation'], 'fixture-q3')
                assert row['state'] == 'done' and row['reason'] == 'bypassed'
        finally:
            if server is not None:
                await server.close()
            if notify is not None:
                await notify.stop()
            if composite is not None:
                await composite.stop()
            store.stop()
    asyncio.run(run())


def test_question_reply_table():
    from assistant_lane_rulings import AssistantLaneRulings
    for state in ('pending', 'approved', 'unruled_disabled', 'bypassed'):
        row = {'state': state, 'ruling_request_id': 'r', 'outcome_json': None}
        reply = AssistantLaneRulings.question_reply(row)
        assert reply['type'] == 'prompt.ask.held' and reply['ruling_state'] == state
        assert reply.get('publishing', False) == (state in {'unruled_disabled', 'bypassed'})
    for state in ('denied', 'revised', 'withdrawn', 'release_blocked'):
        row = {'state': state, 'ruling_request_id': 'r', 'outcome_json': None, 'reason': 'reason'}
        assert AssistantLaneRulings.question_reply(row)['type'] == 'prompt.ask.ruled'
    row = {'state': 'done', 'ruling_request_id': 'r', 'outcome_json': '{"type":"prompt.ask.ok","question":{},"notification":{}}'}
    assert AssistantLaneRulings.question_reply(row)['type'] == 'prompt.ask.ok'


def test_question_digest_covers_frozen_notice_material():
    from assistant_lane_rulings import _question_digest
    envelope = {'schema_version': 1, 'question_id': 'q', 'producer_stream_id': 'h:p',
                'title': 'Title', 'body': 'Body', 'context': 'context', 'options': [],
                'dedup_key': 'd', 'ttl_seconds': 10, 'spec_id': 'spec_a',
                'response_mode': 'free_text', 'allow_custom': True, 'default_action': None}
    original = {'envelope': envelope, 'actions': [], 'severity': 'info'}
    digest = _question_digest(original)
    for field in ('title', 'body', 'context', 'options', 'dedup_key', 'ttl_seconds', 'spec_id'):
        changed = {**original, 'envelope': {**envelope, field: 'changed'}}
        assert _question_digest(changed) != digest, field
    assert _question_digest({**original, 'actions': [{'kind': 'x'}]}) != digest
    assert _question_digest({**original, 'severity': 'warning'}) != digest
