import sqlite3

from work_lane_progress import member_status_behind, lane_status_behind
from store_work_lane_episodes import ensure_work_lane_episodes_schema

NOW = '2026-01-01T01:00:00Z'

def item(**overrides):
    return {'spec_id': 'spec_demo__a', 'status': 'in_progress', 'observation': {'quality': 'fresh'},
            '_progress_at': '2026-01-01T00:00:00Z',
            '_latest_event': {'kind': 'review_passed', 'at': '2026-01-01T00:29:00Z'}, **overrides}

def test_behind_grace_and_observation():
    assert member_status_behind(item(), NOW, True)
    assert not member_status_behind(item(_latest_event={'kind': 'review_passed', 'at': '2026-01-01T00:31:00Z'}), NOW, True)
    assert not member_status_behind(item(_latest_event={'kind': 'review_failed', 'at': '2026-01-01T00:00:00Z'}), NOW, True)
    assert not member_status_behind(item(_progress_at='2026-01-01T00:30:00Z'), NOW, True)
    assert member_status_behind(item(_progress_at=None), NOW, True)
    assert not member_status_behind(item(status='completed'), NOW, True)
    assert not member_status_behind(item(observation={'quality': 'stale'}), NOW, True)
    assert not member_status_behind(item(), NOW, False)

def test_lane_condition_preserves_unknown_and_prioritizes_behind():
    lane = {'work_state': 'active', '_work_index_available': True, '_members': [item()]}
    assert lane_status_behind(lane, NOW) is True
    assert lane_status_behind({**lane, '_work_index_available': False}, NOW) is None
    assert lane_status_behind({**lane, '_members': [item(observation={'quality': 'stale'})]}, NOW) is None
    assert lane_status_behind({**lane, '_members': [item(), item(observation={'quality': 'stale'})]}, NOW) is True
    assert lane_status_behind({**lane, 'work_state': 'done'}, NOW) is False
    assert lane_status_behind({**lane, 'work_state': 'paused', 'parked_at': NOW}, NOW) is False

def test_episode_schema_upgrades_four_kind_table_once():
    conn = sqlite3.connect(':memory:')
    from store_work_lane_episodes import DDL
    conn.execute(DDL.replace(",'status_behind'", ''))
    conn.execute("INSERT INTO v2_work_lane_episodes VALUES ('e','l','stale',1,'now',NULL,NULL,'{}')")
    ensure_work_lane_episodes_schema(conn)
    assert conn.execute('SELECT episode_id FROM v2_work_lane_episodes').fetchone()[0] == 'e'
    conn.execute("INSERT INTO v2_work_lane_episodes VALUES ('f','l','status_behind',1,'now',NULL,NULL,'{}')")
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='v2_work_lane_episodes'").fetchone()[0]
    ensure_work_lane_episodes_schema(conn)
    assert conn.execute("SELECT rootpage FROM sqlite_master WHERE name='v2_work_lane_episodes'").fetchone()[0] == root

def test_latest_report_insert_order_and_filter():
    from store_work_lanes import ensure_latest_report_schema, record_latest_report_conn
    conn = sqlite3.connect(':memory:')
    ensure_latest_report_schema(conn)
    def put(report_id, stamp, verdict='accept', status='done', attribution=None):
        return record_latest_report_conn(conn, {'report_id': report_id, 'created_at': stamp, 'status': status,
            'qa_verdict': verdict, 'usage_snapshot': {'attribution': attribution or {'mode': 'exclusive_primary', 'spec_id': 'spec_demo__a'}}})
    assert put('first', 1000.9)
    assert not put('older', 999.9)
    assert put('same', 1000.1, 'reject')
    assert not put('progress', 1001, status='progress')
    assert not put('multiple', 1001, attribution={'mode': 'shared'})
    assert conn.execute('SELECT kind,at,report_id FROM v2_work_item_latest_report').fetchone() == ('review_failed', '1970-01-01T00:16:40Z', 'same')
    assert put('done', 1001, None)
    assert conn.execute('SELECT kind FROM v2_work_item_latest_report').fetchone()[0] == 'reported_done'

def test_report_insert_updates_latest_in_same_transaction(tmp_path):
    import asyncio
    from ledger import Ledger
    from store import Store
    from test_report_disposition_context import setup, payload
    async def run():
        store = Store(str(tmp_path / 'reports.db'))
        store.start()
        try:
            sessions = await setup(store)
            snapshot = await Ledger(store, sessions=sessions)._report_routing_snapshot('host-a:reporter')
            await store.put_report(payload('latest', qa_verdict='accept'), routing_snapshot=snapshot)
            row = await store.submit(lambda conn: conn.execute('SELECT kind,report_id FROM v2_work_item_latest_report WHERE spec_id=?', ('spec_demo__item_00',)).fetchone())
            assert tuple(row) == ('review_passed', 'latest')
        finally:
            store.stop()
    asyncio.run(run())
