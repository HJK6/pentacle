"""Old committed digests cannot silently suppress all later owed-debt delivery."""
import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from comms import Comms, payload_digest
from message_envelopes import match_message_envelope
from outbound_notices import OutboundNoticeConfig, OutboundNoticeQueue
from sessions import Sessions
from store import Store
from test_work_owed import observe, lead, end
from test_work_unattended import notices


def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')


class NoInput:
    async def capture(self, *args):
        raise AssertionError('evidence-only recovery must not inspect pane chrome')

    async def paste(self, *args):
        raise AssertionError('evidence-only recovery must not input')


async def seeded(tmp_path, monkeypatch):
    now = time.time()
    clock = [now - 6 * 3600]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    monkeypatch.setenv('PENTACLE_LANE_DIGEST_S', '5')
    store = Store(str(tmp_path / 'fixture.db')); store.start()
    def bind_archive(conn):
        from retention import initialize_history_pair
        with conn:
            initialize_history_pair(conn)
    await store.submit(bind_archive)
    sid = 'host-a:root'
    tmux = NoInput()
    sessions = Sessions(store, tmux=tmux, local_host='host-a')
    row = await sessions.open('host-a', 'root', provider='claude', no_watch=True,
                              created_at=stamp(clock[0] - 3600), pane_status='pane_alive')
    binding = (sid, row['session_generation'])
    anchor = await store.append_session_event(sid, {'kind': 'USER', 'text': 'before original attempt',
        'stream_id': sid, 'timestamp': stamp(clock[0])}, identity='anchor', limit=5000)
    assert anchor > 0
    clock[0] += 1
    comms = Comms(store, sessions, SimpleNamespace(tmux=tmux))
    queue = OutboundNoticeQueue(store, comms, config=OutboundNoticeConfig(max_attempts=10))
    old = await queue.enqueue(kind='lane_digest', dedupe_key='old-digest', tell_id='old-digest',
        recipient_stream_id=sid, body='Old digest before debt existed',
        metadata={'root_generation': binding[1]}, created_at=stamp(clock[0]))
    reply = {'type': 'tell.ok', 'tell_id': old['tell_id'], 'to_stream_id': sid,
             'delivery_status': 'committed_pending_proof', 'submission_confirmed': False,
             'submission_attempts': 1, 'do_not_resubmit': True, 'proof_watermark': anchor,
             'proof_watermark_state': 'reachable', 'proof_state': 'pending'}
    await store.put_tell_delivery(old['tell_id'], {
        'payload_digest': payload_digest(sid, old['body']), 'reply': reply,
        'delivery': {**reply, 'text': old['body'], 'enqueued_at': stamp(clock[0])}})
    def age_attempts(conn):
        with conn:
            conn.execute('UPDATE v2_outbound_notices SET attempts=341, last_error=? WHERE notice_id=?',
                         ('submission_proof_pending', old['notice_id']))
    await store.submit(age_attempts)
    clock[0] = now
    await store.append_session_event(sid, {'kind': 'USER', 'text': 'later unrelated input',
        'stream_id': sid, 'timestamp': stamp(now)}, identity='later', limit=5000)
    await observe(store, 'in_progress', 'start', now)
    owner = await lead(store, parent_stream_id=sid)
    await end(store, owner)
    assert (await store.work_owed())['total'] == 1
    presence = {sid: {'session_generation': binding[1], 'online': True, 'pane_status': 'pane_alive'}}
    return store, queue, old, binding, presence, clock


def test_live_row_shape_retires_absent_digest_then_allows_new_debt_digest(tmp_path, monkeypatch):
    async def run():
        store, queue, old, binding, presence, clock = await seeded(tmp_path, monkeypatch)
        try:
            await store.evaluate_watch_wake(presence, now=clock[0], root_binding=binding)
            assert await queue.deliver_now(old['notice_id']) is False
            rows = await notices(store)
            assert rows[0]['delivered_at'] is None  # no fabricated acknowledgment
            assert rows[0]['terminal_reason'] == 'delivery_absent_after_bound', rows[0]['last_error']
            retained = await store.get_tell_delivery(old['tell_id'])
            assert retained['reply']['delivery_status'] == 'committed_pending_proof'
            assert retained['reply']['submission_attempts'] == 1
            await store.evaluate_watch_wake(presence, now=clock[0] + 5, root_binding=binding)
            rows = await notices(store)
            assert len(rows) == 2
            assert match_message_envelope(rows[-1]['body'])['owed_dispositions']['total'] == 1
            assert (await store.work_owed())['total'] == 1
        finally:
            store.stop()
    asyncio.run(run())


async def stored(store, notice_id):
    return await store.submit(lambda c: dict(c.execute(
        'SELECT * FROM v2_outbound_notices WHERE notice_id=?', (notice_id,)).fetchone()))


async def edit_notice(store, notice_id, field, value):
    assert field in {'attempts', 'created_at', 'lease_owner', 'lease_until', 'body', 'kind'}
    def change(conn):
        with conn:
            conn.execute(f'UPDATE v2_outbound_notices SET {field}=? WHERE notice_id=?', (value, notice_id))
    await store.submit(change)


async def archive_old_events(store, target, keep=2):
    from retention import EventSchema, _load_hot_table, archive_events
    def run(conn):
        _load_hot_table(conn, {target})
        return archive_events(conn, EventSchema(conn), conn.history_archive_path, keep, 500)
    return await store.submit(run)


@pytest.mark.parametrize('matching', [False, True])
def test_complete_archived_range_and_late_positive_precedence(tmp_path, monkeypatch, matching):
    async def run():
        store, queue, old, binding, _, clock = await seeded(tmp_path, monkeypatch)
        try:
            proof_id = None
            if matching:
                proof_id = await store.append_session_event(binding[0], {'kind': 'USER',
                    'text': old['body'], 'stream_id': binding[0], 'timestamp': stamp(clock[0])},
                    identity='late-positive', limit=5000)
            # Walk past the ordinary 500-event lookup. Real retention moves the
            # anchor and any positive proof to the actual owned archive.
            for i in range(510):
                await store.append_session_event(binding[0], {'kind': 'USER', 'text': f'later {i}'},
                                                  identity=f'after-{i}', limit=5000)
            assert (await archive_old_events(store, binding[0]))[1] > 500
            assert await queue.deliver_now(old['notice_id']) is matching
            row = await stored(store, old['notice_id'])
            if matching:
                assert row['delivered_at'] and row['terminal_at'] is None
                proof = await store.get_tell_delivery(old['tell_id'])
                assert proof['reply']['proof_event_id'] == proof_id
                assert proof['reply']['submission_attempts'] == 1
            else:
                assert row['terminal_reason'] == 'delivery_absent_after_bound'
                assert row['delivered_at'] is None
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('mode,reason', [
    ('missing_archive', 'archive_unavailable'),
    ('bad_archive', 'DatabaseError'),
    ('missing_anchor', 'anchor_missing'),
    ('zero_watermark', 'watermark_unknown'),
    ('unreachable_watermark', 'watermark_unknown'),
    ('hot_bound', 'readback_bound'),
    ('archive_bound', 'readback_bound'),
    ('malformed_event', 'JSONDecodeError'),
    ('malformed_event_shape', 'malformed_event'),
    ('malformed_metadata', 'metadata_malformed'),
    ('malformed_tell', 'tell_malformed'),
    ('missing_tell', 'tell_missing'),
])
def test_unknown_is_not_absence_and_retains_typed_reason(tmp_path, monkeypatch, mode, reason):
    async def run():
        import digest_delivery_proof as proof
        store, queue, old, binding, _, _ = await seeded(tmp_path, monkeypatch)
        try:
            if mode in {'missing_archive', 'archive_bound'}:
                await store.append_session_event(binding[0], {'kind': 'USER', 'text': 'another'},
                                                  identity='another', limit=5000)
                await archive_old_events(store, binding[0], keep=1)
                if mode == 'missing_archive':
                    (tmp_path/'sessions_archive.db').rename(tmp_path/'retained-archive.db')
                else:
                    monkeypatch.setattr(proof, 'ARCHIVE_USER_LIMIT', 1)
            elif mode == 'bad_archive':
                (tmp_path/'sessions_archive.db').write_bytes(b'not sqlite')
            elif mode == 'hot_bound':
                monkeypatch.setattr(proof, 'HOT_ROW_LIMIT', 1)
            else:
                def change(conn):
                    with conn:
                        if mode == 'missing_anchor':
                            conn.execute('DELETE FROM session_event_tail WHERE identity=?', ('anchor',))
                            from retention import EventSchema, ensure_archive_table
                            ensure_archive_table(conn.history_archive_path, EventSchema(conn).create_sql,
                                                 'session_event_tail')
                        elif mode == 'malformed_event':
                            conn.execute('UPDATE session_event_tail SET event_json=? WHERE identity=?',
                                         ('{', 'later'))
                        elif mode == 'malformed_event_shape':
                            conn.execute('UPDATE session_event_tail SET event_json=? WHERE identity=?',
                                         (json.dumps({'kind': 'TELL', 'raw': ['bad']}), 'later'))
                        elif mode == 'malformed_metadata':
                            conn.execute('UPDATE v2_outbound_notices SET metadata=? WHERE notice_id=?',
                                         ('[]', old['notice_id']))
                        elif mode == 'malformed_tell':
                            conn.execute('UPDATE v2_tell_deliveries SET reply=? WHERE tell_id=?',
                                         ('[]', old['tell_id']))
                        elif mode == 'missing_tell':
                            conn.execute('DELETE FROM v2_tell_deliveries WHERE tell_id=?', (old['tell_id'],))
                        else:
                            r = conn.execute('SELECT reply FROM v2_tell_deliveries WHERE tell_id=?',
                                             (old['tell_id'],)).fetchone()
                            p = json.loads(r[0])
                            field, value = ('proof_watermark', 0) if mode == 'zero_watermark' else (
                                'proof_watermark_state', 'unreachable')
                            p['delivery'][field] = value; p['reply'][field] = value
                            conn.execute('UPDATE v2_tell_deliveries SET reply=? WHERE tell_id=?',
                                         (json.dumps(p), old['tell_id']))
                await store.submit(change)
            assert await queue.deliver_now(old['notice_id']) is False
            row = await stored(store, old['notice_id'])
            assert row['terminal_at'] is None and row['delivered_at'] is None
            assert row['last_error'] == 'absence_readback_unknown:' + reason
            assert 'never resend' in row['next_action']
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('attempts,age,terminal', [(8, 3600, False), (9, 19.999, False), (9, 20.001, True)])
def test_both_existing_bounds_are_required(tmp_path, monkeypatch, attempts, age, terminal):
    async def run():
        store, queue, old, _, _, clock = await seeded(tmp_path, monkeypatch)
        try:
            await edit_notice(store, old['notice_id'], 'attempts', attempts)
            await edit_notice(store, old['notice_id'], 'created_at', stamp(clock[0] - age))
            assert await queue.deliver_now(old['notice_id']) is False
            assert bool((await stored(store, old['notice_id']))['terminal_at']) is terminal
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('race', ['generation', 'lease', 'lease_expired', 'notice', 'late_positive',
                                  'archive_pair', 'archive_attachment', 'archive_file'])
def test_readback_cannot_commit_after_its_evidence_or_owner_changes(tmp_path, monkeypatch, race):
    async def run():
        import digest_delivery_proof as proof
        store, queue, old, binding, _, clock = await seeded(tmp_path, monkeypatch)
        ready, resume = threading.Event(), threading.Event()
        actual = proof._archive_rows
        def paused(*args):
            result = actual(*args)
            ready.set()
            assert resume.wait(2), 'test failed to resume its owned readback'
            return result
        monkeypatch.setattr(proof, '_archive_rows', paused)
        task = asyncio.create_task(queue.deliver_now(old['notice_id']))
        try:
            assert await asyncio.to_thread(ready.wait, 2)
            if race == 'generation':
                await store.open_session('host-a', 'root', provider='claude', pane_status='pane_alive')
            elif race == 'lease':
                await edit_notice(store, old['notice_id'], 'lease_owner', 'other-owner')
            elif race == 'lease_expired':
                await edit_notice(store, old['notice_id'], 'lease_until', clock[0] - 1)
            elif race == 'notice':
                await edit_notice(store, old['notice_id'], 'body', 'different body')
            elif race == 'archive_pair':
                def change_pair(conn):
                    with conn:
                        conn.execute("UPDATE v2_send_receipt_sequence SET pair_id='replacement-pair'")
                await store.submit(change_pair)
            elif race == 'archive_attachment':
                await store.submit(lambda conn: setattr(conn, 'history_attachment_identity', (-1, -1)))
            elif race == 'archive_file':
                import sqlite3
                path = tmp_path/'sessions_archive.db'
                replacement = tmp_path/'replacement.db'
                with sqlite3.connect(path) as source, sqlite3.connect(replacement) as dest:
                    source.backup(dest)
                replacement.replace(path)
            else:
                await store.append_session_event(binding[0], {'kind': 'USER', 'text': old['body'],
                    'stream_id': binding[0], 'timestamp': stamp(clock[0])}, identity='raced-proof', limit=5000)
            resume.set()
            assert await task is False
            row = await stored(store, old['notice_id'])
            assert row['terminal_at'] is None and row['delivered_at'] is None
            if race == 'late_positive':
                assert row['last_error'] == 'absence_readback_unknown:evidence_changed'
                assert await queue.deliver_now(old['notice_id']) is True
        finally:
            resume.set()
            await task
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('age,terminal', [(19.999, False), (20.001, True)])
def test_old_queue_age_does_not_age_a_recent_original_attempt(tmp_path, monkeypatch, age, terminal):
    async def run():
        store, queue, old, _, _, clock = await seeded(tmp_path, monkeypatch)
        try:
            def change(conn):
                with conn:
                    conn.execute('UPDATE v2_tell_deliveries SET created_at=? WHERE tell_id=?',
                                 (clock[0] - age, old['tell_id']))
            await store.submit(change)
            assert await queue.deliver_now(old['notice_id']) is False
            row = await stored(store, old['notice_id'])
            assert bool(row['terminal_at']) is terminal
            if not terminal:
                assert row['last_error'] == 'absence_readback_unknown:original_attempt_age_bound_not_reached'
        finally:
            store.stop()
    asyncio.run(run())


def test_failed_guard_cannot_turn_pending_proof_into_absence(tmp_path, monkeypatch):
    async def run():
        store, queue, old, _, _, _ = await seeded(tmp_path, monkeypatch)
        async def guard(row):
            raise RuntimeError('guard unavailable')
        queue.register_kind('lane_digest', guard=guard)
        try:
            assert await queue.deliver_now(old['notice_id']) is False
            row = await stored(store, old['notice_id'])
            assert row['terminal_at'] is None and row['delivered_at'] is None
            assert row['last_error'] == 'absence_readback_unknown:notice_guard_failed'
        finally:
            store.stop()
    asyncio.run(run())


def test_negative_is_durable_and_not_replayed_after_restart(tmp_path, monkeypatch):
    async def run():
        store, queue, old, _, _, _ = await seeded(tmp_path, monkeypatch)
        await queue.deliver_now(old['notice_id'])
        before = await stored(store, old['notice_id'])
        assert before['terminal_reason'] == 'delivery_absent_after_bound'
        store.stop()
        replacement = Store(str(tmp_path/'fixture.db')); replacement.start()
        try:
            assert await OutboundNoticeQueue(replacement, None).deliver_now(old['notice_id']) is False
            assert await stored(replacement, old['notice_id']) == before
            assert (await replacement.work_owed())['total'] == 1
        finally:
            replacement.stop()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['unpaired', 'replaced', 'attachment_changed'])
def test_untrusted_archive_is_unknown_even_with_a_valid_anchor(tmp_path, monkeypatch, mode):
    async def run():
        import sqlite3
        store, queue, old, binding, _, _ = await seeded(tmp_path, monkeypatch)
        try:
            await store.append_session_event(binding[0], {'kind': 'USER', 'text': 'another'},
                                              identity='archive-extra', limit=5000)
            await archive_old_events(store, binding[0], keep=1)
            from retention import _attached_history
            await store.submit(_attached_history)
            path = tmp_path/'sessions_archive.db'
            if mode == 'unpaired':
                with sqlite3.connect(path) as archive:
                    archive.execute("UPDATE v2_receipt_route_archive SET pair_id='unpaired'")
            elif mode == 'replaced':
                replacement = tmp_path/'replacement.db'
                with sqlite3.connect(path) as source, sqlite3.connect(replacement) as dest:
                    source.backup(dest)
                replacement.replace(path)
            else:
                await store.submit(lambda conn: setattr(conn, 'history_attachment_identity', (-1, -1)))
            assert await queue.deliver_now(old['notice_id']) is False
            row = await stored(store, old['notice_id'])
            assert row['terminal_at'] is None and row['delivered_at'] is None
            assert row['last_error'].startswith('absence_readback_unknown:archive_')
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('event', [
    {'kind': 'TELL', 'raw': []},
    {'kind': 'TELL', 'raw': None},
    {'kind': 'TELL', 'raw': False},
    {'kind': 'TELL', 'raw': {'peer_wire_text': []}},
    {'kind': 'USER', 'text': []},
    {'kind': 'USER', 'text': None},
    {'kind': 'USER', 'text': 'caption', 'provider_text_digest': []},
    {'kind': 'USER', 'text': 'caption', 'provider_text_digest': 'not-a-digest'},
    {'kind': 'SYSTEM', 'text': {}},
    {'kind': 'SYSTEM', 'text': 'caption', 'provider_text_digest': False},
])
def test_malformed_submission_fields_cannot_prove_absence(tmp_path, monkeypatch, event):
    async def run():
        store, queue, old, _, _, _ = await seeded(tmp_path, monkeypatch)
        try:
            def corrupt(conn):
                with conn:
                    conn.execute('UPDATE session_event_tail SET event_json=? WHERE identity=?',
                                 (json.dumps(event), 'later'))
            await store.submit(corrupt)
            assert await queue.deliver_now(old['notice_id']) is False
            row = await stored(store, old['notice_id'])
            assert row['terminal_at'] is None and row['delivered_at'] is None
            assert row['last_error'] == 'absence_readback_unknown:malformed_event'
        finally:
            store.stop()
    asyncio.run(run())
