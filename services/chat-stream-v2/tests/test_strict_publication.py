"""Strict publication suppresses implicit replies, independently of the mirror flag."""
import pytest
import desk_stamp
from test_assistant_dispatch_turn_final import run
from test_assistant_prose_mirror import ASSISTANT, ROOT
import asyncio
import json
import shlex
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from assistant_lane_rulings import AssistantLaneRulings
from assistant_composite import AssistantCompositeConfig


async def mirror_count(store):
    return await store.submit(lambda conn: conn.execute(
        "SELECT count(*) FROM session_event_tail WHERE stream_id=? AND identity LIKE 'assistant-mirror:%'",
        (ASSISTANT,)).fetchone()[0])


def test_environment_defaults_are_primary_on_and_daff_off():
    assert AssistantCompositeConfig.from_env({}).strict_publication_default is True
    assert AssistantCompositeConfig.from_env({}, name='daff', env_prefix='DAFF_').strict_publication_default is False
    assert AssistantCompositeConfig.from_env({'PENTACLE_ASSISTANT_DAFF_STRICT_PUBLICATION': 'on'},
                                             name='daff', env_prefix='DAFF_').strict_publication_default is True


def test_primary_turn_final_never_publishes_implicitly():
    async def body(h):
        route = await h.route("one-door")
        await h.dispatch_user(route, "one-door-user")
        await h.ingest("ASSIST_TEXT", "Unpublished turn prose", "one-door-final", final=True)
        assert await h.answers() == []
        assert await mirror_count(h.store) == 0
        assert await h.publications(route["dispatch_id"]) == []
    run(body, strict=True)


def test_strict_publication_override_takes_effect_on_next_turn():
    async def body(h):
        assert await h.store.strict_publication_state(ASSISTANT) == {"enabled": True, "source": "env"}
        def override(conn):
            conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('assistant.strict_publication.enabled','off')")
            conn.commit()
        await h.store.submit(override)
        assert await h.store.strict_publication_state(ASSISTANT) == {"enabled": False, "source": "kv"}
        route = await h.route("rollback")
        await h.dispatch_user(route, "rollback-user")
        await h.ingest("ASSIST_TEXT", "restored mirror", "rollback-final", final=True)
        assert [e["text"] for e in await h.answers()] == ["restored mirror"]
        msg = {"request_id": "rollback-note", "composite_stream_id": ASSISTANT,
               "publish_kind": "status", "message": "old behavior", "note_type": "result",
               "ref": "message:not-present", "_auth_context": {"token_verified": True,
               "stream_id": ROOT, "session_generation": h.generation}}
        await h.composite.publish(msg, actor_stream_id=ROOT)
        assert (await h.composite.publish(dict(msg, note_type="relay", ref="tell:other"), actor_stream_id=ROOT))["duplicate"]
    run(body, strict=True)


@pytest.mark.parametrize("sender_kind,wire", [
    ("peer", "[from peer:seat] [tell:strict-trigger]\nhello"),
    ("service", "[pentacle-notice:strict-trigger] wake"),
])
def test_peer_and_notice_turn_text_is_not_mirrored(sender_kind, wire):
    async def body(h):
        from comms import payload_digest
        seat = await h.store.fetch_session(*ROOT.split(':', 1))
        digest = payload_digest(ROOT, wire)
        await h.store.put_tell_delivery('strict-trigger', {'payload_digest': digest,
            'reply': {'tell_id': 'strict-trigger', 'to_stream_id': ROOT, 'delivery_status': 'committed_pending_proof'},
            'delivery': {'tell_id': 'strict-trigger', 'to_stream_id': ROOT, 'text': wire,
                'sender_verified': True, 'sender_kind': sender_kind,
                'from_stream_id': 'peer:seat' if sender_kind == 'peer' else None,
                'sender_generation': 'peer-generation' if sender_kind == 'peer' else None,
                'proof_generation': seat['session_generation'], 'proof_session_created_at': seat['created_at'],
                'proof_watermark': 0, 'proof_watermark_state': 'reachable', 'request_payload_hash': digest,
                'delivery_status': 'committed_pending_proof'}})
        await h.ingest('USER', wire, 'trigger-user')
        receipt = await h.store.get_tell_delivery('strict-trigger')
        assert receipt['reply']['delivery_status'] == 'delivered'
        await h.ingest('ASSIST_TEXT', 'unpublished prose', 'trigger-final', final=True)
        assert await h.answers() == []
        assert await mirror_count(h.store) == 0
    run(body, strict=True)


def test_turn_end_notifies_once_and_copied_ack_is_accepted():
    async def body(h):
        route = await h.route("unanswered")
        await h.dispatch_user(route, "unanswered-user")
        await h.ingest("ASSIST_TEXT", "done", "unanswered-final", final=True)
        def notices(conn):
            return [dict(r) for r in conn.execute("SELECT * FROM v2_outbound_notices WHERE kind='dispatch_no_reply'")]
        rows = await h.store.submit(notices)
        assert len(rows) == 1
        assert route["dispatch_id"] in rows[0]["body"]
        command = rows[0]["body"].split("Acknowledgment: ", 1)[1].splitlines()[0]
        words = shlex.split(command)[3:]
        msg = {words[i][2:].replace("-", "_"): words[i + 1] for i in range(0, len(words), 2)}
        result = await h.composite.publish(msg, actor_stream_id=ROOT)
        assert result["duplicate"] is False
        await h.dispatch_user(route, "unanswered-user-again")
        await h.ingest("ASSIST_TEXT", "done", "unanswered-final-again", final=True)
        assert len(await h.store.submit(notices)) == 1
    run(body, strict=True)


def test_old_frozen_dispatch_envelope_still_matches():
    async def body(h):
        route = await h.route('old-envelope')
        payload = json.loads(route['route_json'])
        added = ("Only a publish reaches the operator; turn text is never delivered. "
                 f"Use publish:{route['dispatch_id']}:ack for an acknowledgment and "
                 f"publish:{route['dispatch_id']} for the final. End the turn with the single word done.\n")
        wire = payload['direct_envelope']['wire_body']
        assert added in wire
        payload['direct_envelope']['wire_body'] = wire.replace(added, '')
        def freeze(conn):
            conn.execute('UPDATE v2_assistant_composite_routes SET route_json=? WHERE route_id=?',
                         (json.dumps(payload), route['route_id']))
            conn.commit()
        await h.store.submit(freeze)
        route['route_json'] = json.dumps(payload)
        await h.dispatch_user(route, 'old-envelope-user')
        await h.ingest('ASSIST_TEXT', 'done', 'old-envelope-final', final=True)
        count = await h.store.submit(lambda conn: conn.execute(
            "SELECT count(*) FROM v2_outbound_notices WHERE notice_id=?",
            ('dispatch_no_reply:' + route['dispatch_id'],)).fetchone()[0])
        assert count == 1
    run(body, strict=True)


def test_legacy_assistant_boundary_does_not_attach_old_dispatch_to_notice():
    async def body(h):
        route = await h.route('legacy-boundary')
        await h.dispatch_user(route, 'legacy-user')
        await h.store.append_session_events_lifecycle_cas([{
            'stream_id': ROOT, 'identity': 'legacy-assistant',
            'event': {'stream_id': ROOT, 'kind': 'ASSIST_TEXT', 'text': 'old row',
                      'raw': {'source_session_identity': 'fixture-session', 'jsonl_record_uuid': 'legacy'}},
            'lifecycle': await h.store.fetch_open_session_lifecycle(ROOT, pane_pid='4242')}], limit=100)
        await h.ingest('ASSIST_TEXT', 'done', 'new-final', final=True)
        count = await h.store.submit(lambda conn: conn.execute(
            "SELECT count(*) FROM v2_outbound_notices WHERE kind='dispatch_no_reply'").fetchone()[0])
        assert count == 0
    run(body, strict=True)


@pytest.mark.parametrize("state,kind,suffix", [("acknowledged", "status", ":ack"), ("final", "prose", "")])
def test_published_reply_prevents_notice_and_marker(state, kind, suffix):
    async def body(h):
        route = await h.route("already-published")
        await h.dispatch_user(route, "published-user")
        await h.composite.publish({"request_id": "publish:" + route["dispatch_id"] + suffix,
            "composite_stream_id": ASSISTANT, "dispatch_id": route["dispatch_id"],
            "reply_to_message_id": "already-published", "publish_kind": kind,
            "response_state": state, "message": "Published"}, actor_stream_id=ROOT)
        await h.ingest("ASSIST_TEXT", "done", "published-final", final=True)
        notices = await h.store.submit(lambda conn: conn.execute(
            "SELECT count(*) FROM v2_outbound_notices WHERE kind='dispatch_no_reply'").fetchone()[0])
        assert notices == 0
        timings = json.loads(route["route_json"])["timings"]
        landed = datetime.fromisoformat(timings["landed_at"].replace("Z", "+00:00")).timestamp()
        service = AssistantLaneRulings(SimpleNamespace(store=h.store, assistant_composite=h.composite, broadcast=None))
        await service._mark_unanswered(landed + 301)
        assert [event["text"] for event in await h.answers()] == ["Published"]
    run(body, strict=True)


def test_unanswered_marker_at_300_seconds_once():
    async def body(h):
        route = await h.route("marker-input")
        stamp = json.loads(route["route_json"])["timings"]["landed_at"]
        landed = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        service = AssistantLaneRulings(SimpleNamespace(store=h.store, assistant_composite=h.composite,
                                                       broadcast=None))
        await service._mark_unanswered(landed + 299)
        assert await h.answers() == []
        await service._mark_unanswered(landed + 300)
        await service._mark_unanswered(landed + 301)
        answers = await h.answers()
        assert len(answers) == 1
        assert answers[0]["publish_kind"] == "unanswered"
        assert answers[0]["reply_to_message_id"] == "marker-input"
        assert answers[0]["text"] == "No reply from Bart yet to this message."
    run(body, strict=True)


def test_periodic_pass_marks_landed_dispatch_unanswered():
    async def body(h):
        route = await h.route("periodic-input")
        timings = json.loads(route["route_json"])["timings"]
        stamp = timings.get("landed_at") or timings["delivery_recorded_at"]
        landed = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        service = AssistantLaneRulings(SimpleNamespace(store=h.store, assistant_composite=h.composite,
                                                       broadcast=None))
        async def no_authority_check():
            pass
        service._check_authority_loss = no_authority_check
        with patch("assistant_lane_rulings.time.time", return_value=landed + 300):
            await service.tick()
        assert [event["publish_kind"] for event in await h.answers()] == ["unanswered"]
    run(body, strict=True)


def test_unanswered_marker_is_not_recreated_after_store_restart(tmp_path):
    from dataclasses import replace
    from assistant_composite import AssistantComposite
    from store import Store
    from test_assistant_dispatch_turn_final import Harness
    from test_assistant_prose_mirror import _config
    async def body():
        store = Store(str(tmp_path / 'sessions.db'))
        store.start()
        composite = None
        try:
            root = await store.open_session('fixture-root', 'visible', provider='claude', pane_pid='4242')
            async def dispatch(_route):
                return {'delivery': 'landed'}
            composite = AssistantComposite(store, config=replace(_config(root['session_generation']),
                                           strict_publication_default=True), dispatch=dispatch)
            await composite.ensure_projection()
            harness = Harness(store, composite, root['session_generation'])
            route = await harness.route('restart-marker')
            stamp = json.loads(route['route_json'])['timings']['landed_at']
            landed = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
            service = AssistantLaneRulings(SimpleNamespace(store=store, assistant_composite=composite, broadcast=None))
            await service._mark_unanswered(landed + 300)
            await composite.stop()
            composite = None
            store.stop()
            store = Store(str(tmp_path / 'sessions.db'))
            store.start()
            composite = AssistantComposite(store, config=replace(_config(root['session_generation']),
                                           strict_publication_default=True), dispatch=dispatch)
            await composite.ensure_projection()
            harness = Harness(store, composite, root['session_generation'])
            service = AssistantLaneRulings(SimpleNamespace(store=store, assistant_composite=composite, broadcast=None))
            await service._mark_unanswered(landed + 301)
            assert [event['publish_kind'] for event in await harness.answers()] == ['unanswered']
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()
    asyncio.run(body())


def test_proactive_message_reference_resolves_archived_route(tmp_path):
    from dataclasses import replace
    from assistant_composite import AssistantComposite
    from store import Store
    from test_assistant_prose_mirror import _config
    from test_receipt_route_retention import OLD, move
    import retention
    async def body():
        store = Store(str(tmp_path / 'sessions.db')); store.start()
        composite = None
        try:
            root = await store.open_session('fixture-root', 'visible', provider='codex', pane_pid='4242')
            composite = AssistantComposite(store, config=replace(_config(root['session_generation']),
                                           strict_publication_default=True))
            await composite.ensure_projection()
            await store.admit_assistant_composite_input(stream_id=ASSISTANT, input_identity='old-input',
                input_request_id='old-request', body='Set up reminders', attachments=[],
                reply_to_message_id=None, reply_to_question_id=None, actor_stream_id=None)
            def age(conn):
                conn.execute("UPDATE v2_assistant_composite_routes SET routing_state='resolved',delivery_state='landed',created_at=?,updated_at=? WHERE input_identity='old-input'", (OLD, OLD))
                conn.commit()
            await store.submit(age)
            assert (await move(store, retention.ROUTE_TABLE))['moved'] == 1
            assert await store.submit(lambda conn: conn.execute("SELECT count(*) FROM v2_assistant_composite_routes WHERE input_identity='old-input'").fetchone()[0]) == 0
            result = await composite.publish({'request_id': 'archived-ref', 'composite_stream_id': ASSISTANT,
                'publish_kind': 'status', 'message': 'Scheduled reminder', 'note_type': 'reminder',
                'ref': 'message:old-input', '_auth_context': {'token_verified': True, 'stream_id': ROOT,
                'session_generation': root['session_generation']}}, actor_stream_id=ROOT)
            assert not result['duplicate']
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()
    asyncio.run(body())


def test_provider_login_publish_accepted_with_caller_reference():
    from tools import provider_account_operator_login as login
    async def body(h):
        await h.route('login-request')
        loop = asyncio.get_running_loop()
        def command(argv):
            if argv[:2] == ['prompt', 'ask']:
                return {'type': 'prompt.ask.ok', 'question': {'question_id': 'q', 'notification_id': 'n'},
                        'notification': {'surfaced_to_stream_id': ASSISTANT}}
            words = argv[2:]
            words.remove('--proactive')
            payload = {words[i][2:].replace('-', '_'): words[i + 1] for i in range(0, len(words), 2)}
            payload['_auth_context'] = {'token_verified': True, 'stream_id': ROOT, 'session_generation': h.generation}
            return asyncio.run_coroutine_threadsafe(h.composite.publish(payload, actor_stream_id=ROOT), loop).result(timeout=5)
        result = await asyncio.to_thread(login.publish_url, 'https://auth.openai.com/oauth/authorize?state=synthetic',
                                         'login-op', 'message:login-request', command=command,
                                         verify=lambda: ASSISTANT)
        assert result['publication_id'] == 'login-op-url'
        event = (await h.answers())[0]
        assert event['note_type'] == 'problem' and event['ref'] == 'message:login-request'
    run(body, strict=True)


def test_proactive_note_validation_deduplication_and_replay():
    async def body(h):
        await h.route("reference")
        def message(key, text, **extra):
            return {"request_id": key, "composite_stream_id": ASSISTANT, "publish_kind": "prose",
                    "message": text, "_auth_context": {"token_verified": True, "stream_id": ROOT,
                    "session_generation": h.generation}, **extra}
        with pytest.raises(ValueError, match="assistant_publish_note_type_required"):
            await h.composite.publish(message("missing-type", "hello"), actor_stream_id=ROOT)
        with pytest.raises(ValueError, match="assistant_publish_ref_required"):
            await h.composite.publish(message("missing-ref", "hello", note_type="result"), actor_stream_id=ROOT)
        with pytest.raises(ValueError, match="assistant_publish_ref_not_found"):
            await h.composite.publish(message("bad-ref", "hello", note_type="result", ref="message:absent"), actor_stream_id=ROOT)
        with pytest.raises(ValueError, match="assistant_publish_ref_kind_not_allowed"):
            await h.composite.publish(message("bad-kind", "hello", note_type="relay", ref="lane:foo"), actor_stream_id=ROOT)
        first = message("first", "Hello **world**", note_type="result", ref="message:reference")
        result = await h.composite.publish(first, actor_stream_id=ROOT)
        assert (await h.composite.publish(first, actor_stream_id=ROOT))["duplicate"] is True
        with pytest.raises(ValueError, match="assistant_publish_conflict"):
            await h.composite.publish(dict(first, note_type="problem"), actor_stream_id=ROOT)
        with pytest.raises(ValueError, match=f"assistant_publish_duplicate_text:{result['event_id']}"):
            await h.composite.publish(message("second", "hello   world", note_type="relay", ref="message:reference"), actor_stream_id=ROOT)
        event = (await h.answers())[0]
        assert event["note_type"] == "result" and event["ref"] == "message:reference"
        stored = await h.store.submit(lambda conn: conn.execute(
            "SELECT canonical_payload_json FROM v2_assistant_composite_publications WHERE publication_key='first'").fetchone())
        assert json.loads(stored[0])["ref"] == "message:reference"
        def age(conn):
            conn.execute("UPDATE session_event_tail SET recorded_at=recorded_at-3660 WHERE event_id=?", (result["event_id"],))
            conn.commit()
        await h.store.submit(age)
        assert not (await h.composite.publish(message("later", "hello world", note_type="relay", ref="message:reference"), actor_stream_id=ROOT))["duplicate"]
    run(body, strict=True)


def test_proactive_reference_kinds_and_status_duplicates():
    async def body(h):
        route = await h.route("references")
        def seed(conn):
            conn.execute("INSERT INTO v2_assistant_composite_lanes(lane_id,stream_id,phase,summary,created_at,updated_at,work_state) VALUES ('lane1',?,'execution','x','now','now','active')", (ASSISTANT,))
            conn.execute("INSERT INTO v2_reports(report_id,from_stream_id,status,ingested_at,created_at) VALUES ('report1',?,'done','now',0)", (ROOT,))
            conn.execute("INSERT INTO v2_watch_wake(id,kind,owner,owner_generation,request_id,fingerprint,state,data) VALUES ('wake1','idle',?,'g','r','f','active','{}')", (ROOT,))
            conn.execute("INSERT INTO v2_schedules(schedule_id,request_id,owner_stream_id,owner_spec_ids_json,owner_spec_provenance_json,target_host,requested_provider,requested_model,requested_effort,resolved_provider,resolved_model,resolved_effort,fires_at_utc,state,created_at,updated_at) VALUES ('schedule1','schedule-r',?,'[]','[]','h','codex','m','high','codex','m','high','2026-01-01T00:00:00Z','pending','now','now')", (ROOT,))
            conn.execute("INSERT INTO v2_tell_deliveries(tell_id,reply,created_at) VALUES ('tell1','{}',0)")
            conn.commit()
        await h.store.submit(seed)
        refs = [("result", "message:" + route["dispatch_id"]), ("problem", "lane:lane1"),
                ("result", "report:report1"), ("reminder", "wake:wake1"), ("relay", "tell:tell1"),
                ("reminder", "wake:schedule1")]
        for index, (kind, ref) in enumerate(refs):
            msg = {"request_id": f"note-{index}", "composite_stream_id": ASSISTANT,
                   "publish_kind": "status", "message": f"Different status {index}",
                   "note_type": kind, "ref": ref, "_auth_context": {"token_verified": True,
                   "stream_id": ROOT, "session_generation": h.generation}}
            result = await h.composite.publish(msg, actor_stream_id=ROOT)
            if index == 0:
                changed = dict(msg, request_id="duplicate-status", message="different **STATUS**   0")
                with pytest.raises(ValueError, match=f"assistant_publish_duplicate_text:{result['event_id']}"):
                    await h.composite.publish(changed, actor_stream_id=ROOT)
                def age(conn):
                    conn.execute("UPDATE session_event_tail SET recorded_at=recorded_at-3660 WHERE event_id=?", (result["event_id"],))
                    conn.commit()
                await h.store.submit(age)
                assert not (await h.composite.publish(changed, actor_stream_id=ROOT))["duplicate"]
    run(body, strict=True)


def test_desk_credential_is_persisted_and_dispatch_replies_are_refused(monkeypatch):
    credential = "6a54b9a1-406f-4d5f-8ffb-818b703e6a56"
    monkeypatch.setattr(desk_stamp, "load", lambda: {credential: "test-desk"})

    async def body(h):
        await h.composite.accept_input({"message": "desk message", "msg_id": "desk-1"},
                                       operator_principal="operator:" + credential)
        for _ in range(200):
            route = await h.store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="desk-1")
            if route and route["delivery_state"] == "landed":
                break
            await asyncio.sleep(.01)
        assert json.loads(route["route_json"])["desk_dispatch"] is True
        monkeypatch.setattr(desk_stamp, "load", lambda: {})
        assert (await h.activity("desk-1"))["response_state"] == "reply_not_required"
        for kind, state, suffix in (("prose", "final", ""), ("status", "acknowledged", ":ack"),
                                    ("status", None, ":status")):
            msg = {"request_id": "publish:" + route["dispatch_id"] + suffix,
                   "composite_stream_id": ASSISTANT, "dispatch_id": route["dispatch_id"],
                   "reply_to_message_id": "desk-1", "publish_kind": kind,
                   "response_state": state, "message": "reply"}
            with pytest.raises(ValueError, match="assistant_publish_desk_dispatch"):
                await h.composite.publish(msg, actor_stream_id=ROOT)
        await h.dispatch_user(route, "desk-user")
        await h.ingest("ASSIST_TEXT", "done", "desk-final", final=True)
        count = await h.store.submit(lambda conn: conn.execute(
            "SELECT count(*) FROM v2_outbound_notices WHERE kind='dispatch_no_reply'").fetchone()[0])
        assert count == 0
        timings = json.loads(route["route_json"])["timings"]
        landed = datetime.fromisoformat(timings["landed_at"].replace("Z", "+00:00")).timestamp()
        service = AssistantLaneRulings(SimpleNamespace(store=h.store, assistant_composite=h.composite, broadcast=None))
        await service._mark_unanswered(landed + 301)
        assert await h.answers() == []
        assert await mirror_count(h.store) == 0
        reply = await h.composite.publish({"request_id": "relay-desk", "composite_stream_id": ASSISTANT,
            "publish_kind": "prose", "message": "Relayed desk message", "note_type": "relay",
            "ref": "message:desk-1", "_auth_context": {"token_verified": True, "stream_id": ROOT,
            "session_generation": h.generation}}, actor_stream_id=ROOT)
        assert not reply["duplicate"]
    run(body, strict=True)


def test_batch_cannot_answer_a_desk_dispatch(monkeypatch):
    credential = '6a54b9a1-406f-4d5f-8ffb-818b703e6a56'
    monkeypatch.setattr(desk_stamp, 'load', lambda: {credential: 'test-desk'})
    async def body(h):
        await h.composite.accept_input({'message': 'desk relay', 'msg_id': 'batch-desk'},
                                       operator_principal='operator:' + credential)
        for _ in range(200):
            desk = await h.store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity='batch-desk')
            if desk and desk['delivery_state'] == 'landed':
                break
            await asyncio.sleep(.01)
        assert json.loads(desk['route_json'])['desk_dispatch'] is True
        operator = await h.route('batch-operator')
        assert 'batch-desk' in json.loads(operator['route_json'])['direct_envelope']['wire_body']
        with pytest.raises(ValueError, match='assistant_publish_desk_dispatch'):
            await h.composite.publish({'request_id': 'publish:' + operator['dispatch_id'],
                'composite_stream_id': ASSISTANT, 'dispatch_id': operator['dispatch_id'],
                'reply_to_message_id': 'batch-operator', 'reply_to_message_ids': ['batch-operator', 'batch-desk'],
                'publish_kind': 'prose', 'response_state': 'final', 'message': 'Combined answer'}, actor_stream_id=ROOT)
        assert not await h.answers()
    run(body, strict=True)


@pytest.mark.parametrize("client", ["web", "mobile", "voice", "room-microphone"])
def test_operator_body_cannot_spoof_desk_classification(monkeypatch, client):
    monkeypatch.setattr(desk_stamp, "load", lambda: {"6a54b9a1-406f-4d5f-8ffb-818b703e6a56": "test-desk"})
    async def body(h):
        await h.composite.accept_input({"message": "[from test-desk]\noperator input", "msg_id": client},
                                       operator_principal="operator:9f1dd120-ea56-4a1b-8f8d-a33e73355268")
        route = await h.store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity=client)
        assert not json.loads(route["route_json"] or "{}").get("desk_dispatch")
    run(body, strict=True)


def test_strict_proactive_replay_survives_rollback_and_rejects_conflict():
    async def body(h):
        await h.route('rollback-reference')
        msg = {'request_id': 'rollback-replay', 'composite_stream_id': ASSISTANT,
               'publish_kind': 'status', 'message': 'Hello', 'note_type': 'result',
               'ref': 'message:rollback-reference', '_auth_context': {'token_verified': True,
               'stream_id': ROOT, 'session_generation': h.generation}}
        first = await h.composite.publish(msg, actor_stream_id=ROOT)
        def rollback(conn):
            conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('assistant.strict_publication.enabled','off')")
            conn.commit()
        await h.store.submit(rollback)
        replay = await h.composite.publish(msg, actor_stream_id=ROOT)
        assert replay['duplicate'] and replay['event_id'] == first['event_id']
        with pytest.raises(ValueError, match='assistant_publish_conflict'):
            await h.composite.publish(dict(msg, note_type='problem'), actor_stream_id=ROOT)
    run(body, strict=True)


def test_marker_route_receipt_survives_generation_change_without_new_index():
    async def body(h):
        route = await h.route('cross-generation')
        stamp = json.loads(route['route_json'])['timings']['landed_at']
        landed = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
        service = AssistantLaneRulings(SimpleNamespace(store=h.store, assistant_composite=h.composite, broadcast=None))
        await service._mark_unanswered(landed + 300)
        def move_marker(conn):
            conn.execute("UPDATE session_event_tail SET session_created_at='prior-generation' WHERE stream_id=? AND identity=?",
                         (ASSISTANT, 'dispatch-unanswered:' + route['dispatch_id']))
            conn.commit()
            return {r[1] for r in conn.execute('PRAGMA index_list(session_event_tail)')}
        indexes = await h.store.submit(move_marker)
        assert 'idx_session_event_tail_stream_identity' not in indexes
        stored = await h.store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity='cross-generation')
        assert json.loads(stored['route_json'])['unanswered_marker_recorded'] == 1
        await h.composite._update_route(route['route_id'], routing_state='resolved', route_payload={'admission_mode': 'direct_primary'})
        stored = await h.store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity='cross-generation')
        assert json.loads(stored['route_json'])['unanswered_marker_recorded'] == 1
        await service._mark_unanswered(landed + 301)
        count = await h.store.submit(lambda c: c.execute("SELECT count(*) FROM session_event_tail WHERE stream_id=? AND identity=?", (ASSISTANT, 'dispatch-unanswered:' + route['dispatch_id'])).fetchone()[0])
        assert count == 1
    run(body, strict=True)


def test_rollback_dispatch_uses_legacy_packet():
    async def body(h):
        def rollback(conn):
            conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('assistant.strict_publication.enabled','off')")
            conn.commit()
        await h.store.submit(rollback)
        route = await h.route('legacy-packet')
        wire = json.loads(route['route_json'])['direct_envelope']['wire_body']
        assert 'Only a publish reaches the operator' not in wire
        assert 'End the turn with the single word done.' not in wire
    run(body, strict=True)


def test_daff_packet_matches_pre_strict_packet():
    from assistant_composite import direct_dispatch_envelope
    config = AssistantCompositeConfig.from_env({}, name='daff', env_prefix='DAFF_')
    route = {'input_identity': 'operator-input', 'body': 'Hello', 'attachments_json': '[]'}
    args = {'dispatch_id': 'dispatch-1', 'target': 'daff:seat', 'generation': 'g'}
    wire = direct_dispatch_envelope(config, route, **args)['wire_body']
    strict = direct_dispatch_envelope(config, route, strict_publication=True, **args)['wire_body']
    added = ('Only a publish reaches the operator; turn text is never delivered. '
             'Use publish:dispatch-1:ack for an acknowledgment and publish:dispatch-1 for the final. '
             'End the turn with the single word done.\n')
    assert wire.encode() == strict.replace(added, '').encode()
    assert added in strict


def test_publication_dispatch_index_reuses_history_index():
    async def body(h):
        indexes = await h.store.submit(lambda c: {r[1] for r in c.execute('PRAGMA index_list(v2_assistant_composite_publications)')})
        assert 'idx_history_publication_dispatch' in indexes
        assert 'idx_v2_assistant_publications_dispatch' not in indexes
    run(body, strict=True)


def test_marker_sweep_failure_does_not_block_ruling_reconciliation(caplog):
    async def body(h):
        service = AssistantLaneRulings(SimpleNamespace(store=h.store, assistant_composite=h.composite, broadcast=None))
        checked = []
        async def failed_sweep():
            raise RuntimeError('synthetic marker read failure')
        async def authority_check():
            checked.append(True)
        service._mark_unanswered = failed_sweep
        service._check_authority_loss = authority_check
        await service.tick()
        assert checked == [True]
        assert 'assistant unanswered-marker check failed; will retry' in caplog.text
    run(body, strict=True)
