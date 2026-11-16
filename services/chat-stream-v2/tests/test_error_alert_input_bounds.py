"""Bound notice/proof dependency inputs despite unrelated history growth."""
import json
from types import SimpleNamespace

import pytest
from error_alerts import ErrorAlerts
from store import Store


@pytest.mark.asyncio
async def test_required_notice_closure_and_pending_queries_are_history_bounded():
    store = Store(':memory:'); store.start()
    subject = object.__new__(ErrorAlerts); subject.store = store
    def insert(conn, nid, meta, *, terminal=None, delivered=None):
        conn.execute('INSERT INTO v2_outbound_notices (notice_id,kind,dedupe_key,recipient_stream_id,tell_id,body,payload_digest,created_at,metadata,terminal_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (nid,'error_alert' if meta else 'unrelated',nid,'fixture:seat',nid,'fixture body'*32,'digest','2026-01-01',json.dumps(meta),terminal,delivered))
    def seed(conn, low, high):
        for index in range(low, high): insert(conn, f'unrelated-{index}', {})
        conn.commit()
    async def measure(**selectors):
        steps = [0]
        def progress(): steps[0] += 1; return 0
        await store.submit(lambda conn: conn.set_progress_handler(progress, 1))
        try: rows = await subject.notice_rows(**selectors)
        finally: await store.submit(lambda conn: conn.set_progress_handler(None, 0))
        return {row['notice_id'] for row in rows}, steps[0]
    try:
        def relevant(conn):
            insert(conn,'member',{'notification_id':'fact','error_alert_v1':1,'folded_into_notice_id':'digest'})
            insert(conn,'digest',{'error_alert_v1':1,'member_notice_ids':['member'],'predecessor_notice_id':'prior'}, delivered='2026-01-02')
            insert(conn,'prior',{'error_alert_v1':1,'member_notice_ids':['missing']}, terminal='2026-01-02')
            insert(conn,'awareness',{'error_alert_v1':1,'predecessor_notice_id':'prior','awareness':True}, delivered='2026-01-02')
            conn.commit()
        await store.submit(relevant)
        results = []
        for low, high in [(0, 1000), (1000, 10000)]:
            await store.submit(lambda conn, low=low, high=high: seed(conn,low,high))
            results.append([
                await measure(notice_ids=[], notification_ids=['fact']),
                await measure(notice_ids=[], pending=True, dependencies=False),
                await measure(notice_ids=[], predecessor_ids=['prior']),
                await measure(notice_ids=[]),
            ])
        assert results[0][0][0] == {'member','digest','prior'}
        assert results[0][1][0] == {'member'}
        assert results[0][2][0] == {'awareness','prior'}
        for small, large in zip(*results):
            assert small[0] == large[0]
            assert large[1] <= max(1, small[1]) * 2, (small, large)
    finally: store.stop()


@pytest.mark.asyncio
async def test_prune_empty_facts_never_reads_unrelated_notice_history():
    class EmptyFacts:
        async def call(self, method, *args, **kwargs):
            assert method in ('error_rows','prune_resolved')
            return []
    store = Store(':memory:'); store.start()
    subject = object.__new__(ErrorAlerts); subject.store = store
    subject.notify = SimpleNamespace(_db=EmptyFacts())
    statements = []
    try:
        await store.submit(lambda conn: conn.set_trace_callback(statements.append))
        await subject._prune(1800000000)
        assert not any('v2_outbound_notices' in sql for sql in statements)
    finally: store.stop()
