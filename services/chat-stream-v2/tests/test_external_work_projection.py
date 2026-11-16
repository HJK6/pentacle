"""Synthetic packet snapshots and pure lane projections; no servers or sockets."""
import json
import sqlite3
from dataclasses import replace

import pytest

from external_work import ExternalWorkConfig
from store_external_work import EXTERNAL_WORK_DDL, current_packets_conn
from work_lane_progress import working_spec_ids
from work_lanes_projection import build_frame, page_lane_detail, project_lane
from test_lane_checkpoint_visibility import NOW, visibility_fixture


def packet(spec_id, tag, state='working', at=1):
    return dict(watch_id='dot', spec_id=spec_id, tag=tag, state=state,
                opened_at=1, state_at=1, evidence_at=at, mail_id=None)


def context(lane, rows):
    lane['_external_packets'] = {'label': 'Dot', 'packets': rows}
    return lane


def test_working_union_distinct_full_census_and_selected_goal():
    lane, seats, questions = visibility_fixture()
    identity = lane['_members'][-1]['spec_id']
    context(lane, [packet(identity, 'a'), packet(identity, 'b'),
                   packet(lane['_members'][0]['spec_id'], 'q', 'queued'),
                   packet(lane['_members'][1]['spec_id'], 'r', 'review'),
                   packet('spec_foreign', 'foreign')])
    assert working_spec_ids(lane, []) == {identity}
    assert working_spec_ids(lane, list(seats.values())) == {identity}
    detail = project_lane(lane, None, NOW, all_members=True, sessions_by_stream=seats, questions_by_spec=questions)
    assert detail['visibility_counts']['working'] == 1
    assert detail['lead']['presence']['working'] is True
    assert detail['external'] == {'label': 'Dot', 'working': 2, 'queued': 1, 'review': 1,
                                  'verified_at': '1970-01-01T00:00:01Z'}
    shown = {'lane': lane, 'events': [], 'updates': [], '_projection_row': lane, '_work_index': {'available': True}}
    selected = page_lane_detail(shown, detail, goal_filter=lane['_members'][-1]['goal_id'],
                                sessions_by_stream=seats, questions_by_spec=questions)['projection']['selected_goal']
    assert selected['visibility_counts']['working'] == 1
    assert selected['working'] is True
    inventory = build_frame([lane], seats, now_iso=NOW, questions_by_spec=questions)['lanes'][0]
    assert inventory['external'] == detail['external']
    assert all('external' not in m for m in inventory['members'])
    # External activity is presentation, not authorization or a lane-state write.
    lane['work_state'] = 'done'
    done = project_lane(lane, None, NOW, all_members=True, sessions_by_stream={})
    assert done['state'] == 'done' and done['external']['working'] == 2
    assert build_frame([lane], {}, now_iso=NOW)['lanes'] == []


def test_member_order_cap_and_latest_evidence_not_age():
    lane, _, _ = visibility_fixture(3)
    identity = lane['_members'][0]['spec_id']
    context(lane, [packet(identity, tag, state, at) for tag, state, at in
                   [('z','queued',1), ('r-b','review',2), ('r-a','review',3),
                    ('b','working',4), ('a','working',5), ('c','working',6)]])
    detail = project_lane(lane, None, NOW, all_members=True, sessions_by_stream={})
    external = detail['members'][0]['external']
    assert [(r['state'], r['ref']) for r in external] == [('working','a'),('working','b'),('working','c'),('review','r-a')]
    assert detail['external']['verified_at'] == '1970-01-01T00:00:06Z'
    assert detail['visibility_counts']['working'] == 1


@pytest.mark.parametrize('available', [True, False])
def test_current_packets_read_only_terminal_filter_and_disabled(monkeypatch, available):
    import store_work_index
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    for ddl in EXTERNAL_WORK_DDL:
        conn.execute(ddl)
    rows = [packet('spec_'+state, state, state) for state in
            ('working','queued','review','unlinked','ignored','closed')]
    rows += [packet('spec_done','done'), packet('spec_dropped','dropped')]
    for r in rows:
        conn.execute('INSERT INTO v2_external_work_packets VALUES (?,?,?,?,?,?,?,?)', tuple(r[k] for k in
                     ('watch_id','tag','spec_id','state','opened_at','state_at','evidence_at','mail_id')))
    monkeypatch.setattr(store_work_index, 'effective_membership_conn', lambda conn, **kw:
                        {'state': {'available': available}, 'members': {
                            'spec_done': {'terminal':'completed'}, 'spec_dropped': {'terminal':'deprecated'}}})
    config = ExternalWorkConfig(status='enabled', watch_id='dot', label='Dot')
    before = conn.total_changes
    result = current_packets_conn(conn, config=config)
    assert {r['tag'] for r in result['packets']} == ({'working','queued','review'} if available else
                                                  {'working','queued','review','done','dropped'})
    assert conn.total_changes == before
    assert current_packets_conn(conn, config=replace(config,status='disabled'))['packets'] == []
    conn.close()


def test_external_maximum_frame_budget(capsys):
    from server import _encode_frame
    from test_work_lane_spec_fields import _budget_frame
    from _shared.work_goals import wire_title
    maximum = {'goal_id': 'g_' + 'f'*24, 'title': wire_title(('😀\\\"\n'*100)),
               'state':'in_progress','done':2147483647,'total':2147483647,
               'rank':2147483647,'pos':63,'target_date':'9999-12-31','icon':'briefcase'}
    envelope = {'goals':[maximum]*8,'goals_total':2147483647,
                'default_goal':{'state':'in_progress','done':2147483647,'total':2147483647},
                'detail_revision':'f'*64}
    ext = {'label':'😀'*80,'working':32,'queued':32,'review':32,'verified_at':'9999-12-31T23:59:59Z'}
    # Per-state maxima intentionally over-approximate the shared 32-row cap.
    for count, limit in ((16,320*1024),(64,1310720)):
        frame = _budget_frame(count)
        for lane in frame['lanes']:
            lane.update(envelope)
            lane.update(goal_classes='w'*9,goal_counts=[2147483647]*4,
                        progress_at='9999-12-31T23:59:59Z',state_at='9999-12-31T23:59:59Z',
                        goal_road='w'*64,hue='chartreuse',parked={'at':'9999-12-31T23:59:59Z','reason':'😀'*160})
            for member in lane['members']:
                member['goal_id']=maximum['goal_id']
        baseline=len(_encode_frame(frame).encode())
        for lane in frame['lanes']:
            lane['external']=ext
        size=len(_encode_frame(frame).encode())
        print(f'EXTERNAL_BUDGET lanes={count} baseline={baseline} bytes={size} delta={size-baseline} limit={limit}')
        assert size <= limit


def test_thirty_two_packets_count_rows_not_members():
    lane, _, _ = visibility_fixture(3)
    identity = lane['_members'][0]['spec_id']
    context(lane, [packet(identity, f'packet-{i:02}', ('working','queued','review')[i % 3], i + 1)
                   for i in range(32)])
    detail = project_lane(lane, None, NOW, all_members=True, sessions_by_stream={})
    assert detail['external'] == {'label':'Dot','working':11,'queued':11,'review':10,
                                  'verified_at':'1970-01-01T00:00:32Z'}
    assert detail['visibility_counts']['working'] == 1
    assert len(detail['members'][0]['external']) == 4


def test_lane_status_read_has_no_store_instance_dependency(monkeypatch):
    import store_work_lanes
    monkeypatch.setattr(store_work_lanes, 'work_lane_rows_conn', lambda conn, **kw: [])
    assert store_work_lanes._lane_work_status_conn(None, 'missing-lane') == 'uncertain'
