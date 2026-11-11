"""Derived lane progress: member progress time, progress trail, unblocks and state time."""
import json

from _shared.work_goals import goal_id
from test_work_lane_goals import projected, reconcile, scanner, write_goal, write_item
from test_work_lanes import run
from work_lane_progress import progress_trail
from work_lanes_projection import page_lane_detail


def event(at, spec, prior, nxt, *, rev=1, lane='wl-fixture'):
    return {'event_id': f'item:{lane}:{spec}:{rev}', 'created_at': at,
            'payload': {'spec_id': spec, 'obs_rev': rev, 'prior': prior, 'next': nxt}}


def state(status='in_progress', checked=0, line='Inspect the fixture.', blocked=None, *, legacy=False):
    value = {'status': status, 'ac_checked': checked, 'ac_total': 3, 'estimate': None, 'estimate_exempt': False}
    return value if legacy else {**value, 'status_line': line, 'blocked_line': blocked}


A, B, C = 'spec_demo__a', 'spec_demo__b', 'spec_demo__c'


def test_each_kind_order_and_old_events():
    events = [  # newest first, as the store reads them
        event('2026-11-10T10:00:00.500Z', A, state(line='Inspect the fixture.'), state(line='Assemble the span.'), rev=5),
        event('2026-11-10T09:00:00.000Z', A, state('in_progress', 1), state('blocked', 1, blocked='Waiting on keys.'), rev=4),
        event('2026-11-10T08:00:00.000Z', B, state('needs_qa', 3), state('completed', 3), rev=3),
        event('2026-11-10T07:00:00.000Z', A, state(checked=0), state(checked=1), rev=3),
        event('2026-11-10T06:00:00.000Z', A, state(line=None), state(line='Inspect the fixture.'), rev=2),
        event('2026-11-10T05:00:00.000Z', A, state('backlog'), state('in_progress'), rev=1),
        event('2026-11-09T05:00:00.000Z', B, state(checked=2, legacy=True), state('blocked', 0, legacy=True), rev=2),
        event('2026-11-09T04:00:00.000Z', B, state(checked=0, legacy=True), state(checked=2, legacy=True), rev=1),
    ]
    trail = progress_trail(events, {A, B})
    assert trail == [
        {'at': '2026-11-10T10:00:00Z', 'spec_id': A, 'kind': 'note', 'text': 'Assemble the span.'},
        {'at': '2026-11-10T09:00:00Z', 'spec_id': A, 'kind': 'blocked', 'text': 'Waiting on keys.'},
        {'at': '2026-11-10T08:00:00Z', 'spec_id': B, 'kind': 'done'},
        {'at': '2026-11-10T07:00:00Z', 'spec_id': A, 'kind': 'checks', 'count': 1},
        {'at': '2026-11-09T05:00:00Z', 'spec_id': B, 'kind': 'blocked', 'text': None},
        {'at': '2026-11-09T04:00:00Z', 'spec_id': B, 'kind': 'checks', 'count': 2},
    ]
    assert progress_trail(events, {B}) == [trail[2], trail[4], trail[5]]
    assert progress_trail(events, {C}) == []


def test_six_entry_cap_and_current_members_only():
    events = [event(f'2026-11-10T{hour:02}:00:00Z', A if hour % 2 else C, state(checked=0), state(checked=1), rev=hour)
              for hour in range(20, 0, -1)]
    trail = progress_trail(events, {A})
    assert len(trail) == 6 and {entry['spec_id'] for entry in trail} == {A}
    assert [entry['at'] for entry in trail] == sorted((entry['at'] for entry in trail), reverse=True)


def _seed(root, lane_id):
    write_goal(root, lane_id)
    write_goal(root, lane_id, 'second')
    items = {name: write_item(root, name, status='in_progress') for name in ('alpha', 'beta')}
    items['gamma'] = write_item(root, 'gamma', status='in_progress', epic='epic_second')
    return items


def _edit(folder, old, new, name='spec.md'):
    path = folder / name
    path.write_text(path.read_text().replace(old, new))


def test_end_to_end_detail_progress_trail_unblocks_and_summary(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        items = _seed(root, lane['lane_id'])
        _edit(items['alpha'], 'title: Item alpha\n', 'title: Item alpha\nsummary: The fixture shows its trail.\n')
        _edit(items['alpha'], '## Acceptance Criteria\n',
              '## Estimate\n\n- remaining_work_h: 1–3 (median 2) as_of 2026-11-01\n'
              '- remaining_work_h: 2–4 (median 3) as_of 2026-11-10\n\n## Acceptance Criteria\n')
        blocker = write_item(root, 'waiting', status='blocked', epic=None)
        _edit(blocker, '---\n---\n', '---\nblocked_by:\n- spec_demo__alpha\n---\n', 'summary.md')
        source = scanner(root)
        await reconcile(env.store, source)
        shown = await env.store.get_work_lane(lane['lane_id'])
        first = {m['spec_id']: m for m in page_lane_detail(shown, projected(shown))['members']}
        alpha = first['spec_demo__alpha']
        assert alpha['progress_at'] is None and alpha['recent'] == []
        assert alpha['summary_line'] == 'The fixture shows its trail.'
        assert alpha['estimate_first'] == {'p25': 1.0, 'p75': 3.0, 'median': 2.0}
        assert alpha['unblocks'] == [{'spec_id': 'spec_demo__waiting', 'title': 'Item waiting'}]
        assert first['spec_demo__beta']['unblocks'] == [] and first['spec_demo__beta']['summary_line'] is None

        _edit(items['alpha'], '- [ ] Inspect.', '- [x] Inspect.')
        _edit(items['beta'], 'Inspect the fixture.', 'Assemble the span.', 'summary.md')
        _edit(items['gamma'], 'Inspect the fixture.', 'Inspect spec_demo__x first.', 'summary.md')
        await reconcile(env.store, source)
        shown = await env.store.get_work_lane(lane['lane_id'])
        projection = projected(shown)
        page = page_lane_detail(shown, projection)
        members = {m['spec_id']: m for m in page['members']}
        assert [(e['kind'], e.get('count')) for e in members['spec_demo__alpha']['recent']] == [('checks', 1)]
        assert members['spec_demo__alpha']['progress_at'] is not None
        assert [(e['kind'], e['text']) for e in members['spec_demo__beta']['recent']] == [('note', 'Assemble the span.')]
        # A wording edit to an invalid line is not progress.
        assert members['spec_demo__gamma']['progress_at'] is None and members['spec_demo__gamma']['recent'] == []
        assert {e['spec_id'] for e in page['projection']['recent']} == {'spec_demo__alpha', 'spec_demo__beta'}
        assert projection['progress_at'] == max(members[s]['progress_at'] for s in ('spec_demo__alpha', 'spec_demo__beta'))
        selected = page_lane_detail(shown, projection, goal_filter=goal_id('epic_fixture'))['projection']['selected_goal']
        assert {e['spec_id'] for e in selected['recent']} == {'spec_demo__alpha', 'spec_demo__beta'}
        assert selected['summary_line'] == 'A finite fixture outcome.'
        assert selected['progress_at'] == projection['progress_at']
        second = page_lane_detail(shown, projection, goal_filter=goal_id('epic_second'))['projection']['selected_goal']
        assert second['recent'] == []
        assert 'blocked_by' not in json.dumps(page) and '_blocked_by' not in json.dumps(page)

        # Moving beta to the other goal removes its entries from this goal's trail.
        for name in ('spec.md', 'summary.md'):
            _edit(items['beta'], 'epic_fixture', 'epic_second', name)
        # The blocker leaves blocked, so alpha no longer unblocks anything.
        (root / 'work/backlog').mkdir(exist_ok=True)
        (root / 'work/blocked/demo__waiting').rename(root / 'work/backlog/demo__waiting')
        await reconcile(env.store, source)
        shown = await env.store.get_work_lane(lane['lane_id'])
        projection = projected(shown)
        selected = page_lane_detail(shown, projection, goal_filter=goal_id('epic_fixture'))['projection']['selected_goal']
        assert {e['spec_id'] for e in selected['recent']} == {'spec_demo__alpha'}
        members = {m['spec_id']: m for m in page_lane_detail(shown, projection)['members']}
        assert members['spec_demo__alpha']['unblocks'] == []
    run(body)


def test_trail_read_is_bounded_to_500_events(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        _seed(root, lane['lane_id'])
        await reconcile(env.store, scanner(root))
        def insert(conn):
            from store_work_lanes import _insert_event
            row = dict(conn.execute('SELECT * FROM v2_assistant_composite_lanes WHERE lane_id=?', (lane['lane_id'],)).fetchone())
            for i in range(600):
                # The 100 oldest events would make entries; the newest 500 make none.
                prior, nxt = (state(checked=0), state(checked=1)) if i < 100 else (state(), state())
                payload = {'spec_id': 'spec_demo__alpha', 'obs_rev': 1000 + i, 'prior': prior, 'next': nxt}
                _insert_event(conn, event_id=f'item:{row["lane_id"]}:spec_demo__alpha:{1000 + i}', lane=row,
                              operation='item_change', source_id=f'fixture-{i}', actor='daemon:index', generation=None,
                              prior=row['work_state'], nxt=row['work_state'], expected=None, payload=payload,
                              stamp=f'2026-11-10T{i // 60:02}:{i % 60:02}:00.000Z')
            conn.commit()
        await env.store.submit(insert)
        shown = await env.store.get_work_lane(lane['lane_id'])
        assert len(shown['_trail_events']) == 500
        assert page_lane_detail(shown, projected(shown))['projection']['recent'] == []
    run(body)


def test_progress_time_is_filled_once_from_stored_history(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        items = _seed(root, lane['lane_id'])
        source = scanner(root)
        await reconcile(env.store, source)
        _edit(items['alpha'], '- [ ] Inspect.', '- [x] Inspect.')
        await reconcile(env.store, source)
        def legacy(conn):
            # Records written before this change have no progress time.
            for spec_id, raw in conn.execute('SELECT spec_id,record_json FROM v2_work_item_observations').fetchall():
                record = json.loads(raw)
                record.pop('progress_at', None)
                conn.execute('UPDATE v2_work_item_observations SET record_json=? WHERE spec_id=?', (json.dumps(record), spec_id))
            stamp = conn.execute("SELECT created_at FROM v2_work_lane_events WHERE operation='item_change'").fetchone()[0]
            conn.commit()
            return stamp
        stamp = await env.store.submit(legacy)
        await reconcile(env.store, source)
        records = await env.store.submit(lambda conn: {row[0]: json.loads(row[1]) for row in conn.execute(
            'SELECT spec_id,record_json FROM v2_work_item_observations')})
        assert records['spec_demo__alpha']['progress_at'] == stamp[:19] + 'Z'
        assert records['spec_demo__beta']['progress_at'] is None and 'progress_at' in records['spec_demo__beta']
    run(body)


def test_state_at_moves_only_on_a_state_change(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator', members=['spec_demo__u0']))['lane']
        for name in ('u0', 'u1'):
            write_item(root, name, epic=None)
        await reconcile(env.store, scanner(root))
        async def current():
            return projected(await env.store.get_work_lane(lane['lane_id']))
        adopted = await current()
        events = (await env.store.get_work_lane(lane['lane_id']))['events']
        assert adopted['state_at'] == events[0]['created_at'][:19] + 'Z'
        await env.op('set_text', {'title': 'Renamed lane'}, lane=lane['lane_id'], version=1)
        await env.op('set_members', {'members': ['spec_demo__u0', 'spec_demo__u1']}, lane=lane['lane_id'], version=2)
        renamed = await current()
        assert renamed['state_at'] == adopted['state_at'] and renamed['title'] == 'Renamed lane'
        await env.op('set_state', {'to': 'blocked', 'blocker': 'Need keys', 'reason': 'Waiting on keys',
                                   'next_action': 'Resume after the keys arrive'}, lane=lane['lane_id'], version=3)
        blocked = await current()
        events = (await env.store.get_work_lane(lane['lane_id']))['events']
        changed = [e for e in events if e['operation'] == 'set_state'][-1]
        assert blocked['state_at'] == changed['created_at'][:19] + 'Z' and blocked['state'] == 'blocked'
    run(body)


def test_default_goal_progress_time_reads_deprecated_unplaced_members():
    from work_lanes_projection import project_lane
    members = [{'spec_id': 'spec_demo__open', 'status': 'in_progress', 'observation': {'quality': 'fresh'},
                '_progress_at': '2026-11-01T00:00:00Z'},
               {'spec_id': 'spec_demo__dropped', 'status': 'deprecated', 'observation': {'quality': 'fresh'},
                '_progress_at': '2026-11-10T00:00:00Z'}]
    row = {'lane_id': 'wl-fixture', 'version': 1, 'work_state': 'paused', '_members': members, '_work_index_available': True}
    shown = project_lane(row, None, '2026-11-11T00:00:00Z', all_members=True)
    assert shown['default_goal']['progress_at'] == shown['progress_at'] == '2026-11-10T00:00:00Z'
    assert (shown['default_goal']['total'], shown['default_goal']['moving']) == (1, 1)


def test_unblocks_dedupes_ids_and_keeps_four_by_spec_id(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        _seed(root, lane['lane_id'])
        for name in ('w1', 'w2', 'w3', 'w4', 'w5'):
            folder = write_item(root, name, status='blocked', epic=None)
            ids = '- spec_demo__alpha\n- spec_demo__alpha\n' if name == 'w1' else '- spec_demo__alpha\n'
            _edit(folder, '---\n---\n', '---\nblocked_by:\n' + ids + '---\n', 'summary.md')
        await reconcile(env.store, scanner(root))
        shown = await env.store.get_work_lane(lane['lane_id'])
        members = {m['spec_id']: m for m in page_lane_detail(shown, projected(shown))['members']}
        assert [entry['spec_id'] for entry in members['spec_demo__alpha']['unblocks']] == [
            'spec_demo__w1', 'spec_demo__w2', 'spec_demo__w3', 'spec_demo__w4']
        assert members['spec_demo__alpha']['unblocks'][0]['title'] == 'Item w1'
        def retitle(conn):
            raw = conn.execute("SELECT record_json FROM v2_work_item_observations WHERE spec_id='spec_demo__w1'").fetchone()[0]
            record = json.loads(raw)
            record['member']['title'] = '\U0001f9ed' * 240
            conn.execute("UPDATE v2_work_item_observations SET record_json=? WHERE spec_id='spec_demo__w1'", (json.dumps(record),))
            conn.commit()
        await env.store.submit(retitle)
        shown = await env.store.get_work_lane(lane['lane_id'])
        title = page_lane_detail(shown, projected(shown))['members'][0]['unblocks'][0]['title']
        assert 0 < len(json.dumps(title, ensure_ascii=False).encode()) <= 120
    run(body)


def test_equal_time_events_read_newest_event_id_first(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        _seed(root, lane['lane_id'])
        await reconcile(env.store, scanner(root))
        def insert(conn):
            from store_work_lanes import _insert_event
            row = dict(conn.execute('SELECT * FROM v2_assistant_composite_lanes WHERE lane_id=?', (lane['lane_id'],)).fetchone())
            for spec, rev in (('spec_demo__alpha', 7), ('spec_demo__beta', 7)):
                payload = {'spec_id': spec, 'obs_rev': rev, 'prior': state(checked=0), 'next': state(checked=1)}
                _insert_event(conn, event_id=f'item:{row["lane_id"]}:{spec}:{rev}', lane=row, operation='item_change',
                              source_id=f'fixture-{spec}', actor='daemon:index', generation=None, prior=row['work_state'],
                              nxt=row['work_state'], expected=None, payload=payload, stamp='2026-11-10T00:00:00.000Z')
            conn.commit()
        await env.store.submit(insert)
        shown = await env.store.get_work_lane(lane['lane_id'])
        trail = page_lane_detail(shown, projected(shown))['projection']['recent']
        assert [entry['spec_id'] for entry in trail] == ['spec_demo__beta', 'spec_demo__alpha']
    run(body)
