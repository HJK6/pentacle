"""Acceptance journeys: provider files -> durable accounting -> public readbacks."""
import asyncio
import json
import pytest

from ingest import Ingest, _close_stream
from server import Server
from sessions import Sessions
from store import Store

HOST = 'amaterasu'

class Tmux:
    async def capture(self, _name):
        return ''


def codex(total=100, cached=40, output=20, reasoning=7):
    return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'total_token_usage': {'input_tokens': total, 'cached_input_tokens': cached,
                             'output_tokens': output, 'reasoning_output_tokens': reasoning},
        'last_token_usage': {'input_tokens': 999999, 'total_tokens': 999999},
    }}}


def claude(mid='message-1', output=20):
    return {'type': 'assistant', 'sessionId': 'native-claude', 'uuid': 'record-' + mid,
            'message': {'id': mid, 'role': 'assistant', 'content': [], 'usage': {
                'input_tokens': 100, 'cache_read_input_tokens': 40,
                'cache_creation_input_tokens': 10, 'output_tokens': output,
                'cache_creation': {'ephemeral_5m_input_tokens': 10}}}}


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_bound_provider_files_persist_usage_across_restart_and_replay(tmp_path, provider):
    async def run():
        for _case in [provider]:
            path = tmp_path / (provider + '.jsonl')
            records = ([{'type': 'session_meta', 'payload': {'id': 'native-codex'}},
                        codex(), codex(), codex(150, 60, 30, 9)] if provider == 'codex'
                       else [claude(), claude(), claude(output=25), claude('message-2')])
            path.write_text(''.join(json.dumps(r) + '\n' for r in records[:1]) + '{broken-json}\n' + ''.join(json.dumps(r) + '\n' for r in records[1:]))
            database = str(tmp_path / (provider + '.db'))
            expected = ({'input_total': 150, 'cached_input': 60, 'output': 30, 'reasoning': 9}
                        if provider == 'codex' else
                        {'uncached_input': 200, 'cache_read': 80, 'cache_write': 20, 'output': 45})
            for iteration in range(2):
                store = Store(database)
                store.start()
                sessions = Sessions(store, local_host=HOST)
                ingest = Ingest(store, sessions, Tmux(), lambda _: asyncio.sleep(0), local_host=HOST, recent_limit=20)
                try:
                    if iteration == 0:
                        await sessions.open(HOST, provider, provider=provider,
                                            pane_status='pane_alive', jsonl_path=str(path),
                                            spec_ids=['spec_primary', 'spec_secondary'])
                    else:
                        await sessions.refresh()
                    await ingest.run_pass()
                    server = Server(store=store, sessions=sessions)
                    server.inventory_ready.set()
                    listed = await server._on_list_sessions({})
                    usage = listed['active'][0].get('usage')
                    assert usage is not None, 'native usage is absent from persisted stream accounting'
                    assert usage['tokens'] == expected
                    assert usage['host'] == HOST and usage['incomplete'] is True
                    assert 'malformed_record' in usage['incomplete_reasons']
                    assert usage['spec_ids'] == ['spec_primary', 'spec_secondary']
                    assert usage['attribution'] == {'spec_id': 'spec_primary', 'mode': 'exclusive_primary'}
                    inspected = await server._on_inspect_stream({'stream_id': HOST + ':' + provider, 'event_tail': 0})
                    assert inspected['session']['usage'] == usage
                finally:
                    for state in ingest._streams.values():
                        _close_stream(state)
                    store.stop()
    asyncio.run(run())


def test_partial_tail_truncation_and_compaction_never_recount(tmp_path):
    async def run():
        path = tmp_path / 'native.jsonl'
        header = json.dumps({'type': 'session_meta', 'payload': {'id': 'native-codex'}}) + '\n'
        first = json.dumps(codex()) + '\n'
        second = json.dumps(codex(150, 60, 30, 9))
        path.write_text(header + first + second[:35])
        store = Store(str(tmp_path / 'partial.db'))
        store.start()
        sessions = Sessions(store, local_host=HOST)
        ingest = Ingest(store, sessions, Tmux(), lambda _: asyncio.sleep(0), local_host=HOST, recent_limit=20)
        try:
            await sessions.open(HOST, 'partial', provider='codex', jsonl_path=str(path))
            await ingest.run_pass()
            assert (await store.fetch_session(HOST, 'partial'))['usage']['tokens']['input_total'] == 100
            with path.open('a') as handle:
                handle.write(second[35:] + '\n')
            await ingest.run_pass()
            assert (await store.fetch_session(HOST, 'partial'))['usage']['tokens']['input_total'] == 150
            # Truncate in place to an older complete prefix, then append again.
            path.write_text(header + first)
            await ingest.run_pass()
            with path.open('a') as handle:
                handle.write(second + '\n')
            await ingest.run_pass()
            assert (await store.fetch_session(HOST, 'partial'))['usage']['tokens']['input_total'] == 150
            # Replacement/compaction retains only the cumulative record, not history.
            replacement = tmp_path / 'replacement.jsonl'
            replacement.write_text(header + second + '\n')
            replacement.replace(path)
            await ingest.run_pass()
            await ingest.run_pass()
            assert (await store.fetch_session(HOST, 'partial'))['usage']['tokens']['input_total'] == 150
        finally:
            for state in ingest._streams.values():
                _close_stream(state)
            store.stop()
    asyncio.run(run())


def test_summary_hello_projection_preserves_usage():
    snapshot = {'host': HOST, 'tokens': {'input_total': 100}, 'incomplete': True}
    projected = Server._summary_snapshot_sessions([{'stream_id': HOST + ':s', 'usage': snapshot}])
    assert projected[0]['usage'] == snapshot


def test_report_carries_server_snapshot_and_freezes_replay(tmp_path):
    from test_report_variants import _provenance_state, _provenance_report

    async def run():
        store, sessions, ledger, _frames = await _provenance_state(tmp_path)
        path = tmp_path / 'report-native.jsonl'
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'report-native'}}) + '\n' + json.dumps(codex()) + '\n')
        await store.update_session('alpha', 'worker', jsonl_path=str(path))
        await sessions.refresh()
        ingest = Ingest(store, sessions, Tmux(), lambda _: asyncio.sleep(0), local_host='alpha', recent_limit=20)
        try:
            await ingest.run_pass()
            message = _provenance_report('usage-report')
            # Client-supplied accounting must never become authoritative.
            message['usage_snapshot'] = {'tokens': {'input_total': 999999}}
            with pytest.raises(Exception, match='unknown field: usage_snapshot'):
                await ledger.report(message)
            message.pop('usage_snapshot')
            response = await ledger.report(message)
            report = await store.get_report('usage-report')
            snapshot = report.get('usage_snapshot')
            assert snapshot is not None, 'report omits its server-authored usage snapshot'
            assert snapshot['tokens']['input_total'] == 100
            assert snapshot['stream_id'] == 'alpha:worker'
            assert response['usage_snapshot'] == snapshot
            with path.open('a') as handle:
                handle.write(json.dumps(codex(150, 60, 30, 9)) + '\n')
            await ingest.run_pass()
            assert (await ledger.report(message))['usage_snapshot'] == snapshot
            next_report = _provenance_report('usage-report-later')
            assert (await ledger.report(next_report))['usage_snapshot']['tokens']['input_total'] == 150
        finally:
            for state in ingest._streams.values():
                _close_stream(state)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_invalid_and_sidechain_records_do_not_invent_counts(tmp_path, provider):
    async def run():
        path = tmp_path / (provider + '-invalid.jsonl')
        if provider == 'codex':
            records = [{'type': 'session_meta', 'payload': {'id': 'native-codex'}}, codex()]
            records += [codex(total=value) for value in (-1, True, 1.5, '100')]
            records += [codex(80, 30, 10, 2)]  # same-session counter regression
        else:
            records = [claude()]
            for value in (-1, True, 1.5, '100'):
                record = claude('invalid-' + str(value))
                record['message']['usage']['input_tokens'] = value
                records.append(record)
            records.append({**claude('sidechain'), 'isSidechain': True})
            missing_id = claude('no-stable-id')
            missing_id['message'].pop('id')
            records.append(missing_id)
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        store = Store(str(tmp_path / 'invalid.db'))
        store.start()
        sessions = Sessions(store, local_host=HOST)
        ingest = Ingest(store, sessions, Tmux(), lambda _: asyncio.sleep(0), local_host=HOST, recent_limit=20)
        try:
            await sessions.open(HOST, 'invalid', provider=provider, jsonl_path=str(path))
            await ingest.run_pass()
            usage = (await store.fetch_session(HOST, 'invalid'))['usage']
            field = 'input_total' if provider == 'codex' else 'uncached_input'
            assert usage['tokens'][field] == 100
            assert usage['incomplete'] is True
            assert 'invalid_usage' in usage['incomplete_reasons']
        finally:
            for state in ingest._streams.values():
                _close_stream(state)
            store.stop()
    asyncio.run(run())


def test_handoff_keeps_individual_totals_and_predecessor_link(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'lineage.db'))
        store.start()
        sessions = Sessions(store, local_host=HOST)
        ingest = Ingest(store, sessions, Tmux(), lambda _: asyncio.sleep(0), local_host=HOST, recent_limit=20)
        try:
            for name, total, predecessor in [('before', 100, None), ('after', 150, HOST + ':before')]:
                path = tmp_path / (name + '.jsonl')
                path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': name}}) + '\n' + json.dumps(codex(total)) + '\n')
                await sessions.open(HOST, name, provider='codex', jsonl_path=str(path),
                                    handoff_from_stream_id=predecessor,
                                    spec_ids=['spec_primary', 'spec_secondary'])
            await ingest.run_pass()
            before = (await store.fetch_session(HOST, 'before'))['usage']
            after = (await store.fetch_session(HOST, 'after'))['usage']
            assert before['tokens']['input_total'] == 100
            assert after['tokens']['input_total'] == 150
            assert after['handoff_from_stream_id'] == HOST + ':before'
            assert after['attribution'] == {'mode': 'exclusive_primary', 'spec_id': 'spec_primary'}
        finally:
            for state in ingest._streams.values():
                _close_stream(state)
            store.stop()
    asyncio.run(run())


def test_usage_lifecycle_and_source_fences(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'fences.db'))
        store.start()
        try:
            row = await store.open_session(HOST, 'fenced', provider='codex', jsonl_path='/bound/native.jsonl')
            for changed in ({'session_generation': 'old'}, {'jsonl_path': '/foreign.jsonl'}, {'provider': 'claude'}):
                assert await store.record_usage({**row, **changed}, [codex()], native_session_id='native', collection_host=HOST) is None
            assert await store.record_usage(row, [codex()], native_session_id='native', collection_host='bart') is None
            counted = await store.record_usage(row, [codex()], native_session_id='native', collection_host=HOST)
            assert counted['tokens']['input_total'] == 100
            other = await store.open_session(HOST, 'other', provider='codex')
            conflict = await store.record_usage(other, [codex()], native_session_id='native', collection_host=HOST)
            assert conflict['tokens']['input_total'] is None
            assert 'ownership_conflict' in conflict['incomplete_reasons']
            added = await store.record_usage(row, [codex(20, 0, 5, 1)], native_session_id='new-native', collection_host=HOST)
            assert added['tokens'] == {'input_total': 120, 'cached_input': 40, 'output': 25, 'reasoning': 8}
            # Same-name lifecycle change rejects the previously observed row.
            await store.update_session(HOST, 'fenced', status='closed')
            reopened = await store.open_session(HOST, 'fenced', provider='codex')
            assert reopened['session_generation'] != row['session_generation']
            assert await store.record_usage(row, [codex(500)], native_session_id='native', collection_host=HOST) is None
            replay = await store.record_usage(reopened, [codex()], native_session_id='native', collection_host=HOST)
            assert replay['tokens']['input_total'] is None
        finally:
            store.stop()
    asyncio.run(run())


def test_usage_transaction_rolls_back(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'atomic.db'))
        store.start()
        try:
            row = await store.open_session(HOST, 'atomic', provider='codex')
            await store.submit(lambda conn: conn.execute("CREATE TRIGGER usage_test_abort BEFORE INSERT ON v2_usage_records BEGIN SELECT RAISE(ABORT, 'usage-test-failure'); END"))
            with pytest.raises(Exception, match='usage-test-failure'):
                await store.record_usage(row, [codex()], native_session_id='native', collection_host=HOST)
            counts = await store.submit(lambda conn: (conn.execute('SELECT count(*) FROM v2_usage_records').fetchone()[0], conn.execute('SELECT count(*) FROM v2_usage_state').fetchone()[0]))
            assert counts == (0, 0)
            await store.submit(lambda conn: conn.execute('DROP TRIGGER usage_test_abort'))
            result = await store.record_usage(row, [codex()], native_session_id='native', collection_host=HOST)
            assert result['tokens']['input_total'] == 100
        finally:
            store.stop()
    asyncio.run(run())


def test_usage_local_host_scope(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'host.db'))
        store.start()
        sessions = Sessions(store, local_host=HOST)
        ingest = Ingest(store, sessions, Tmux(), lambda _: asyncio.sleep(0), local_host=HOST, recent_limit=20)
        try:
            path = tmp_path / 'remote.jsonl'
            path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'remote'}}) + '\n' + json.dumps(codex()) + '\n')
            await sessions.open('bart', 'remote', provider='codex', jsonl_path=str(path))
            await ingest.run_pass()
            assert not ingest._streams, 'local collector attempted a foreign provider path'
            usage = (await store.fetch_session('bart', 'remote'))['usage']
            assert usage['collection_host'] is None and usage['scope'] == 'stream'
            assert usage['tokens']['input_total'] is None
            assert not {'fleet_total', 'total_tokens', 'aggregate', 'cost', 'quota'} & usage.keys()
        finally:
            for state in ingest._streams.values():
                _close_stream(state)
            store.stop()
    asyncio.run(run())


def test_usage_input_decisions(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'inputs.db'))
        store.start()
        try:
            row = await store.open_session(HOST, 'inputs', provider='codex')
            missing = codex()
            missing['payload']['info']['total_token_usage'].pop('cached_input_tokens')
            result = await store.record_usage(row, [missing], native_session_id='native', collection_host=HOST)
            assert all(value is None for value in result['tokens'].values())
            assert 'invalid_usage' in result['incomplete_reasons']
            result = await store.record_usage(row, [codex()], native_session_id='native', collection_host=HOST, malformed=True)
            assert result['tokens']['input_total'] == 100
            assert result['incomplete_reasons'] == ['history_not_verified', 'invalid_usage', 'malformed_record']
            revision = result['revision']
            replay = await store.record_usage(row, [codex()], native_session_id='native', collection_host=HOST)
            assert replay == result and replay['revision'] == revision
            # All fields use max; an out-of-order lower input cannot erase it.
            increased = await store.record_usage(row, [codex(90, 40, 30, 8)], native_session_id='native', collection_host=HOST)
            assert increased['tokens'] == {'input_total': 100, 'cached_input': 40, 'output': 30, 'reasoning': 8}
            assert 'counter_regression' in increased['incomplete_reasons']
            assert increased['revision'] == revision + 1
        finally:
            store.stop()
    asyncio.run(run())


def test_usage_attribution_changes(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'attribution.db'))
        store.start()
        try:
            row = await store.open_session(HOST, 'attrs', provider='codex', spec_ids=['spec_a', 'spec_a', 'spec_b'])
            first = await store.record_usage(row, [codex()], native_session_id='native', collection_host=HOST)
            assert first['spec_ids'] == ['spec_a', 'spec_b']
            assert first['attribution'] == {'mode': 'exclusive_primary', 'spec_id': 'spec_a'}
            await store.update_session(HOST, 'attrs', spec_ids=['spec_b', 'spec_a'])
            updated = (await store.fetch_session(HOST, 'attrs'))['usage']
            assert updated['attribution']['spec_id'] == 'spec_b'
            assert updated['tokens'] == first['tokens'] and first['attribution']['spec_id'] == 'spec_a'
            empty = await store.open_session(HOST, 'empty', provider='codex')
            assert empty['usage']['attribution'] == {'mode': 'unattributed', 'spec_id': None}
        finally:
            store.stop()
    asyncio.run(run())


def test_report_usage_interleaving(tmp_path, monkeypatch):
    from test_report_variants import _provenance_state, _provenance_report

    async def run():
        store, sessions, ledger, _frames = await _provenance_state(tmp_path)
        try:
            row = await store.fetch_session('alpha', 'worker')
            await store.record_usage(row, [codex()], native_session_id='native', collection_host='alpha')
            put_report = store.put_report
            entered, release = asyncio.Event(), asyncio.Event()

            async def delayed_put(*args, **kwargs):
                entered.set()
                await release.wait()
                return await put_report(*args, **kwargs)

            monkeypatch.setattr(store, 'put_report', delayed_put)
            task = asyncio.create_task(ledger.report(_provenance_report('interleaved')))
            await asyncio.wait_for(entered.wait(), 2)
            latest = await store.record_usage(row, [codex(150, 60, 30, 9)], native_session_id='native', collection_host='alpha')
            release.set()
            reply = await task
            assert reply['usage_snapshot']['revision'] == latest['revision']
            assert reply['usage_snapshot']['tokens']['input_total'] == 150
            await store.record_usage(row, [codex(200, 80, 40, 12)], native_session_id='native', collection_host='alpha')
            assert (await ledger.report(_provenance_report('interleaved')))['usage_snapshot'] == reply['usage_snapshot']
            assert (await ledger.report(_provenance_report('later')))['usage_snapshot']['tokens']['input_total'] == 200
        finally:
            store.stop()
    asyncio.run(run())


def test_usage_migrate_on_open(tmp_path):
    async def run():
        database = str(tmp_path / 'migration.db')
        store = Store(database)
        store.start()
        try:
            await store.open_session(HOST, 'preserved', provider='codex')
            def preledger(conn):
                conn.execute('DROP TABLE v2_usage_records')
                conn.execute('DROP TABLE v2_usage_state')
                conn.execute('ALTER TABLE v2_reports DROP COLUMN usage_snapshot')
                conn.commit()
            await store.submit(preledger)
        finally:
            store.stop()
        for _ in range(2):
            store = Store(database)
            store.start()
            try:
                row = await store.fetch_session(HOST, 'preserved')
                assert row['usage']['tokens']['input_total'] is None
                columns = await store.submit(lambda conn: [r[1] for r in conn.execute('PRAGMA table_info(v2_reports)')])
                assert columns.count('usage_snapshot') == 1
            finally:
                store.stop()
    asyncio.run(run())


def test_unclassified_legacy_provider_keeps_existing_chat_ingestion(tmp_path):
    async def run():
        path = tmp_path / 'legacy.jsonl'
        record = claude()
        record['message']['content'] = [{'type': 'text', 'text': 'legacy response'}]
        path.write_text(json.dumps(record) + '\n')
        store = Store(str(tmp_path / 'legacy.db'))
        store.start()
        sessions = Sessions(store, local_host=HOST)
        ingest = Ingest(store, sessions, Tmux(), lambda _: asyncio.sleep(0), local_host=HOST, recent_limit=20)
        try:
            await sessions.open(HOST, 'legacy', jsonl_path=str(path), pane_pid='8123')
            await ingest.run_pass()
            assert len(await store.fetch_session_event_tail(HOST + ':legacy', limit=20)) == 1
            assert (await store.fetch_session(HOST, 'legacy'))['usage']['tokens'] == {}
        finally:
            for state in ingest._streams.values():
                _close_stream(state)
            store.stop()
    asyncio.run(run())
