"""Source-derived membership, uncapped goal progress and stable detail identity."""
import json
from pathlib import Path

import pytest

from _shared.specs_service import SpecsSubsystem
from _shared.work_goals import goal_id, goal_progress, parse_goal_facts, project_goals, wire_title
from test_work_lanes import run
from work_lanes_projection import project_lane


def write_goal(root, lane_id, name='fixture', *, rank=1000, icon='cube', title='Finish the fixture', nested=False):
    path = root / 'epics' / (name + '/epic.md' if nested else name + '.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\n' + '\n'.join(f'{key}: {json.dumps(value)}' for key, value in {
        'id': 'epic_' + name, 'type': 'epic', 'title': title, 'summary': 'A finite fixture outcome.',
        'lane_id': lane_id, 'rank': rank, 'icon': icon}.items()) + '\n---\n## Goal\n\nThe fixture has the expected source-derived membership.\n')
    return path


def write_item(root, name, *, status='backlog', epic='epic_fixture'):
    path = root / 'work' / status / ('demo__' + name)
    path.mkdir(parents=True, exist_ok=True)
    metadata = f'epic: {epic}\n' if epic else ''
    (path / 'spec.md').write_text(f'---\nid: spec_demo__{name}\ntitle: Item {name}\n{metadata}---\n## Acceptance Criteria\n- [ ] Inspect.\n')
    (path / 'summary.md').write_text(f'---\n{metadata}---\n**Status** — Inspect the fixture.\n')
    return path


def scanner(root):
    return SpecsSubsystem(memory_root=str(root), session_summaries=lambda: [], changed_callback=lambda _: None, debounce_s=0)


async def reconcile(store, source):
    return await store.reconcile_work_observations(source.scan_work_observations(), settle_s=0, sweep=True)


def projected(shown):
    return project_lane(shown['_projection_row'], None, '2026-11-08T12:00:00Z', all_members=True)


def test_65_members_override_stale_explicit_lanes_and_preserve_execution(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        old = (await env.adopt(key='request:old', state='paused', owner='operator', members=['spec_demo__n00']))['lane']
        target = (await env.adopt(key='request:target', state='paused', owner='operator'))['lane']
        write_goal(root, target['lane_id'])
        for i in range(65):
            write_item(root, f'n{i:02}')
        await reconcile(env.store, scanner(root))
        assert not (await env.store.get_work_lane(old['lane_id']))['members']
        shown = await env.store.get_work_lane(target['lane_id'])
        assert len(shown['members']) == 65
        assert {member['goal_id'] for member in shown['members']} == {goal_id('epic_fixture')}
        assert projected(shown)['goals'][0] == {'goal_id': goal_id('epic_fixture'), 'title': 'Finish the fixture',
                                              'state': 'not_started', 'done': 0, 'total': 65, 'rank': 1000, 'icon': 'cube',
                                              'moving': 0, 'blocked': 0, 'working': False, 'left_h': None, 'progress_at': None}
        assert shown['lane']['version'] == 1 and shown['lane']['work_state'] == 'paused'
        assert len(await env.store.open_lanes_for_spec('spec_demo__n00')) == 1
        with pytest.raises(ValueError, match='source_epic_required'):
            await env.op('set_members', {'members': ['spec_demo__n00']}, lane=target['lane_id'], version=1)
        with pytest.raises(ValueError, match='source_epic_required'):
            await env.adopt(key='request:contradiction', state='paused', owner='operator', members=['spec_demo__n00'])
        assert not [event for event in shown['events'] if event['operation'] == 'item_change']
    run(body)


def test_goal_only_edits_noops_off_preview_replacement_and_partial_source(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        other = (await env.adopt(key='request:alternate', state='paused', owner='operator'))['lane']
        write_goal(root, other['lane_id'], 'alternate')
        replacement = write_item(root, 'replacement', epic='epic_alternate')
        goal = write_goal(root, lane['lane_id'], nested=True)
        for i in range(65):
            write_item(root, f'n{i:02}')
        source = scanner(root)
        await reconcile(env.store, source)
        first = await env.store.get_work_lane(lane['lane_id'])
        baseline = projected(first)['detail_revision']
        await reconcile(env.store, source)
        assert projected(await env.store.get_work_lane(lane['lane_id']))['detail_revision'] == baseline
        # Swap source membership outside the preview, keeping exactly 65.
        # Source identities stay immutable; both work pairs remain coherent.
        folder = root / 'work/backlog/demo__n64'
        for name in ('spec.md', 'summary.md'):
            path = folder / name
            path.write_text(path.read_text().replace('epic_fixture', 'epic_alternate'))
            path = replacement / name
            path.write_text(path.read_text().replace('epic_alternate', 'epic_fixture'))
        await reconcile(env.store, source)
        swapped = projected(await env.store.get_work_lane(lane['lane_id']))
        assert swapped['members_total'] == 65 and swapped['detail_revision'] != baseline
        assert {m['spec_id'] for m in swapped['members']} == {f'spec_demo__n{i:02}' for i in range(64)} | {'spec_demo__replacement'}
        goal.write_text(goal.read_text().replace('"cube"', '"book"').replace('rank: 1000', 'rank: 1'))
        await reconcile(env.store, source)
        changed = projected(await env.store.get_work_lane(lane['lane_id']))
        assert changed['goals'][0]['icon'] == 'book' and changed['goals'][0]['rank'] == 1
        assert changed['detail_revision'] != swapped['detail_revision']
        assert changed['freshness_at'] == swapped['freshness_at']
        assert not [event for event in (await env.store.get_work_lane(lane['lane_id']))['events'] if event['operation'] == 'item_change']
        goal.write_text(goal.read_text().replace('"book"', '"unknown-key"'))
        await reconcile(env.store, source)
        stale = projected(await env.store.get_work_lane(lane['lane_id']))
        assert stale['goals'][0]['icon'] == 'book'
        assert stale['goals'][0]['state'] == 'in_progress'
        assert (await env.store.work_index_status())['error'] == 'work_goal_source_uncertain'
    run(body)


@pytest.mark.parametrize('statuses,expected', [([], ('not_started', 0, 0)),
    (['deprecated'], ('not_started', 0, 0)), (['backlog', 'deprecated'], ('not_started', 0, 1)),
    (['completed', 'deprecated'], ('done', 1, 1)), (['completed', 'backlog'], ('in_progress', 1, 2)),
    (['ready_for_dev'], ('in_progress', 0, 1))])
def test_goal_status_truth_table(statuses, expected):
    result = goal_progress([{'status': status, 'observation': {'quality': 'fresh'}} for status in statuses])
    assert (result['state'], result['done'], result['total']) == expected


def test_active_goals_precede_done_at_selection_boundary_and_default_is_uncapped():
    goals, members = {}, []
    for i in range(11):
        epic = f'epic_fixture_{i}'
        goals[epic] = {'facts': {'lane_id': 'wl-fixture', 'title': str(i), 'rank': 1}, 'quality': 'fresh'}
        members.append({'spec_id': f'spec_demo__n{i}', 'goal_id': goal_id(epic),
                        'status': 'completed' if i < 9 else 'in_progress', 'observation': {'quality': 'fresh'}})
    members.append({'spec_id': 'spec_demo__legacy', 'status': 'backlog', 'observation': {'quality': 'fresh'}})
    row = {'_goals': goals, '_members': members, '_work_index_available': True}
    result = project_goals(row)
    assert result['goals_total'] == 11 and len(result['goals']) == 8
    assert {goal_id('epic_fixture_9'), goal_id('epic_fixture_10')} <= {goal['goal_id'] for goal in result['goals']}
    assert result['goals'] == sorted(result['goals'], key=lambda goal: (goal['rank'], goal['goal_id']))
    assert result['default_goal'] == {'state': 'not_started', 'done': 0, 'total': 1}


def test_title_encoding_and_enum_null_validation():
    title = '😀\\\"\n' * 100
    clipped = wire_title(title)
    assert len(json.dumps(clipped, ensure_ascii=False).encode()) <= 120
    assert len(json.dumps(title[:len(clipped) + 1], ensure_ascii=False).encode()) > 120
    source = '---\nid: epic_demo\ntype: epic\ntitle: Demo\nsummary: Finish fixture\nlane_id: wl-demo\nicon: null\n---\n## Goal\n\nThe fixture succeeds.\n'
    assert parse_goal_facts(source)['icon'] is None
    for bad in ['icon: nope', 'icon: 4', 'icon: [cube]']:
        with pytest.raises(ValueError):
            parse_goal_facts(source.replace('icon: null', bad))
    with pytest.raises(ValueError, match='duplicate'):
        parse_goal_facts(source.replace('icon: null', 'icon: cube\nicon: book'))


def test_pages_traverse_65_once_with_full_aggregates_and_goal_filter(tmp_path):
    from work_lanes_projection import page_lane_detail
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        write_goal(root, lane['lane_id'])
        for i in range(65):
            write_item(root, f'n{i:02}')
        write_goal(root, lane['lane_id'], 'second')
        write_item(root, 'second', epic='epic_second')
        await reconcile(env.store, scanner(root))
        shown = await env.store.get_work_lane(lane['lane_id'])
        projection = projected(shown)
        seen, cursor = [], None
        while True:
            page = page_lane_detail(shown, projection, members_after=cursor)
            from work_lanes_projection import detail_member_keys
            assert page['projection']['members'] == [{k: v for k, v in m.items() if k not in detail_member_keys()} for m in page['members']]
            assert len(page['lane']['members']) <= 32 and 'members_json' not in page['lane']
            assert page['members_total'] == 66 and page['projection']['items_total'] == 66
            assert page['detail_revision'] == projection['detail_revision']
            seen.extend(member['spec_id'] for member in page['members'])
            cursor = page['members_next']
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 66 and seen == sorted(seen)
        page = page_lane_detail(shown, projection, goal_filter=goal_id('epic_second'))
        assert page['members_total'] == 1 and page['members'][0]['spec_id'] == 'spec_demo__second'
        assert page['projection']['items_total'] == 66 and page['projection']['goals_total'] == 2
        assert page_lane_detail(shown, projection, members_after='spec_zzzz__last')['members'] == []
        for kwargs in ({'members_limit': True}, {'members_limit': 0}, {'members_limit': 33},
                       {'members_after': 'not a spec'}, {'goal_filter': 'g_' + 'f' * 24}):
            with pytest.raises(ValueError):
                page_lane_detail(shown, projection, **kwargs)
        with pytest.raises(ValueError, match='too_large'):
            page_lane_detail({**shown, 'events': [{'large': 'x' * (4 * 1024 * 1024)}]}, projection)
    run(body)


def test_cross_lane_goal_move_partial_pair_and_root_loss_keep_last_good(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        a = (await env.adopt(key='request:a', state='paused', owner='operator'))['lane']
        b = (await env.adopt(key='request:b', state='paused', owner='operator'))['lane']
        goal = write_goal(root, a['lane_id'])
        member = write_item(root, 'one')
        source = scanner(root)
        await reconcile(env.store, source)
        first = projected(await env.store.get_work_lane(a['lane_id']))
        goal.write_text(goal.read_text().replace(a['lane_id'], b['lane_id']))
        await reconcile(env.store, source)
        assert not (await env.store.get_work_lane(a['lane_id']))['members']
        second = await env.store.get_work_lane(b['lane_id'])
        assert [m['spec_id'] for m in second['members']] == ['spec_demo__one']
        assert projected(second)['freshness_at'] == first['freshness_at']
        assert second['lane']['version'] == 1 and second['lane']['work_state'] == 'paused'
        assert not [e for e in second['events'] if e['operation'] == 'item_change']
        spec = member / 'spec.md'
        spec.write_text(spec.read_text().replace('epic_fixture', 'epic_missing'))
        await reconcile(env.store, source)
        stale = await env.store.get_work_lane(b['lane_id'])
        assert len(stale['members']) == 1 and stale['members'][0]['observation']['quality'] == 'stale'
        root.rename(tmp_path / 'unavailable')
        await reconcile(env.store, source)
        lost = await env.store.get_work_lane(b['lane_id'])
        assert len(lost['members']) == 1 and projected(lost)['goals_total'] == 1
        assert projected(lost)['goals'][0]['state'] == 'in_progress'
    run(body)


def test_named_goal_and_digest_survive_restart_without_reinterpretation(tmp_path):
    root, database = tmp_path / 'memory', str(tmp_path / 'state.db')
    retained = {}
    async def first(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        write_goal(root, lane['lane_id'], icon=None)
        write_item(root, 'one', status='completed')
        await reconcile(env.store, scanner(root))
        retained.update(lane_id=lane['lane_id'], projection=projected(await env.store.get_work_lane(lane['lane_id'])))
    run(first, database)
    async def second(env):
        shown = await env.store.get_work_lane(retained['lane_id'])
        assert projected(shown)['detail_revision'] == retained['projection']['detail_revision']
        assert projected(shown)['goals'] == retained['projection']['goals']
        assert projected(shown)['goals'][0]['state'] == 'done'
    run(second, database)


def test_colliding_projected_goal_ids_fail_typed_without_dropping_goal(monkeypatch):
    from _shared import work_goals
    monkeypatch.setattr(work_goals, 'goal_id', lambda _: 'g_' + '0' * 24)
    facts = {'lane_id': 'wl-fixture', 'title': 'Fixture', 'rank': 1000}
    with pytest.raises(ValueError, match='work_goal_id_collision'):
        work_goals.project_goals({'_goals': {name: {'facts': facts, 'quality': 'fresh'}
                                             for name in ('epic_one', 'epic_two')}})


def test_shared_goals_fixture_matches_inventory_and_show():
    from work_lanes_projection import build_frame
    cell = json.loads((Path(__file__).resolve().parents[3] /
                       'pentacle-chat-core/tests/fixtures/work-lanes-inventory.json').read_text())['goals_v1']
    assert build_frame([cell['lane']], cell['sessions'], now_iso=cell['now_iso'],
                       worked_by_spec=cell['worked_by_spec']) == cell['inventory_frame']
    assert project_lane(cell['lane'], None, cell['now_iso'], all_members=True,
                        sessions_by_stream=cell['sessions'], worked_by_spec=cell['worked_by_spec']) == cell['show_projection']


def test_maximum_goal_envelope_and_correlated_full_frame_budgets():
    from server import _encode_frame
    from test_work_lane_spec_fields import _budget_frame
    maximum = {'goal_id': 'g_' + 'f' * 24, 'title': wire_title(('😀\\\"\n' * 100)),
               'state': 'in_progress', 'done': 2147483647, 'total': 2147483647,
               'rank': 2147483647, 'target_date': '9999-12-31', 'icon': 'pentacle'}
    envelope = {'goals': [maximum] * 8, 'goals_total': 2147483647,
                'default_goal': {'state': 'in_progress', 'done': 2147483647, 'total': 2147483647},
                'detail_revision': 'f' * 64}
    compact_size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())
    assert compact_size(envelope) <= 2560
    # The four progress keys sit on the lane record, outside the goal envelope.
    lane_keys = {'goal_classes': 'w' * 9, 'goal_counts': [2147483647] * 4,
                 'progress_at': '9999-12-31T23:59:59Z', 'state_at': '9999-12-31T23:59:59Z'}
    assert compact_size(lane_keys) - compact_size({}) + 1 <= 158
    for count, limit in ((16, 320 * 1024), (64, 1310720)):
        frame = _budget_frame(count)
        for lane in frame['lanes']:
            assert lane_keys.keys() <= lane.keys()
            # Old fields/seats stay the actual correlated projection at p95/caps.
            # The goal envelope is the independent worst-case legal wire bound;
            # constructing two billion source members is neither needed nor implied.
            lane.update(envelope)
            lane.update(lane_keys)
            lane['hue'] = 'chartreuse'
            for member in lane['members']:
                member['goal_id'] = maximum['goal_id']
                prior = {key: value for key, value in member.items()
                         if key not in {'worked', 'lead_seat', 'status_line', 'next_line', 'goal_id'}}
                assert compact_size(member) - compact_size(prior) <= 800
        assert len(_encode_frame(frame).encode()) <= limit


def test_source_membership_replacement_clears_completion_episode_even_if_still_terminal(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        write_goal(root, lane['lane_id'])
        write_item(root, 'first', status='completed')
        source = scanner(root)
        await reconcile(env.store, source)
        await env.store.reconcile_work_lane_episodes(None)
        async def episodes():
            return await env.store.submit(lambda conn: [dict(row) for row in conn.execute(
                "SELECT episode_id,cleared_at FROM v2_work_lane_episodes WHERE lane_id=? AND kind='completed' ORDER BY sequence", (lane['lane_id'],))])
        opened = await episodes()
        assert len(opened) == 1 and opened[0]['cleared_at'] is None
        write_item(root, 'second', status='completed')
        await reconcile(env.store, source)
        assert (await episodes())[0]['cleared_at'] is not None
        await env.store.reconcile_work_lane_episodes(None)
        renewed = await episodes()
        assert len(renewed) == 2 and renewed[-1]['cleared_at'] is None
        assert renewed[-1]['episode_id'] != opened[0]['episode_id']
    run(body)


def test_owner_beyond_preview_drives_attendance_through_derived_membership(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        write_goal(root, lane['lane_id'])
        for i in range(65):
            write_item(root, f'n{i:02}', status='in_progress')
        await reconcile(env.store, scanner(root))
        stream, _ = await env.seat('tail-owner', role='lead', assigned_spec_id='spec_demo__n64', spec_ids=['spec_demo__n64'])
        shown = await env.store.get_work_lane(lane['lane_id'])
        assert '_members_working' not in shown['_projection_row']
        assert project_lane(shown['_projection_row'], None, '2026-11-08T12:00:00Z')['state'] == 'paused'
        assert shown['lane']['work_state'] == 'paused' and shown['lane']['version'] == 1
        carried = await env.store.open_lanes_for_spec('spec_demo__n64')
        assert [row['lane_id'] for row in carried] == [lane['lane_id']]
    run(body)


@pytest.mark.parametrize('status', ['completed', 'deprecated'])
@pytest.mark.parametrize('incident', ['missing', 'ambiguous'])
def test_settled_uncertain_goal_keeps_last_good_contribution_and_restart(tmp_path, status, incident):
    root, database = tmp_path / 'memory', str(tmp_path / 'state.db')
    retained = {}
    expected = ('in_progress', 1, 1) if status == 'completed' else ('not_started', 0, 0)
    def check(shown):
        member = shown['members'][0]
        assert member['status'] == incident and member['observation']['quality'] == incident
        goal = projected(shown)['goals'][0]
        assert (goal['state'], goal['done'], goal['total']) == expected
        assert '_goal_status' not in projected(shown)['members'][0]
    async def first(env):
        lane = (await env.adopt(state='paused', owner='operator'))['lane']
        write_goal(root, lane['lane_id'])
        folder = write_item(root, 'settled', status=status)
        source = scanner(root)
        await reconcile(env.store, source)
        if incident == 'missing':
            # Move the owned fixture outside the observed work tree.
            folder.rename(tmp_path / 'retained-source')
        else:
            import shutil
            duplicate = root / 'work' / status / 'demo__duplicate'
            shutil.copytree(folder, duplicate)
        await reconcile(env.store, source)
        await reconcile(env.store, source)
        shown = await env.store.get_work_lane(lane['lane_id'])
        check(shown)
        retained.update(lane_id=lane['lane_id'], revision=projected(shown)['detail_revision'])
    run(first, database)
    async def second(env):
        shown = await env.store.get_work_lane(retained['lane_id'])
        check(shown)
        assert projected(shown)['detail_revision'] == retained['revision']
    run(second, database)


def _goal_row(goal_members, *, legacy=(), sessions=None, ranks=None):
    """goal_members: {name: [(status, extra), ...]} with fresh observations."""
    goals, members = {}, []
    for index, (name, rows) in enumerate(goal_members.items()):
        epic = 'epic_' + name
        goals[epic] = {'facts': {'lane_id': 'wl-fixture', 'title': name, 'rank': (ranks or {}).get(name, index)},
                       'quality': 'fresh'}
        for position, (status, extra) in enumerate(rows):
            members.append({'spec_id': f'spec_demo__{name}{position}', 'goal_id': goal_id(epic), 'status': status,
                            'observation': {'quality': 'fresh'}, **extra})
    for position, (status, extra) in enumerate(legacy):
        members.append({'spec_id': f'spec_demo__legacy{position}', 'status': status,
                        'observation': {'quality': 'fresh'}, **extra})
    return {'lane_id': 'wl-fixture', 'version': 1, 'work_state': 'paused', '_goals': goals, '_members': members,
            '_work_index_available': True}


def _working(spec_id):
    return {'host-a:lead': {'assigned_spec_id': spec_id, 'role': 'lead', 'status': 'open', 'online': True,
                            'working': True}}


def test_each_goal_class_including_empty_and_the_default_letter():
    row = _goal_row({'done': [('completed', {}), ('deprecated', {})], 'working': [('in_progress', {})],
                     'moving': [('needs_qa', {}), ('blocked', {})], 'blocked': [('blocked', {}), ('backlog', {})],
                     'queued': [('ready_for_dev', {})], 'empty': [], 'dropped': [('deprecated', {})]},
                    legacy=[('analysis', {})])
    shown = project_lane(row, None, '2026-11-08T12:00:00Z', all_members=True,
                         sessions_by_stream=_working('spec_demo__working0'))
    assert shown['goal_classes'] == 'dwmbqqqm'
    assert shown['goal_counts'] == [3, 1, 3, 1]
    inventory = project_lane(row, None, '2026-11-08T12:00:00Z', sessions_by_stream={})
    assert inventory['goal_classes'] == 'dmmbqqqm'
    assert project_lane(_goal_row({'a': [('in_progress', {})]}), None, '2026-11-08T12:00:00Z')['goal_classes'] == 'm-'
    only_dropped = _goal_row({'a': [('backlog', {})]}, legacy=[('deprecated', {})])
    assert project_lane(only_dropped, None, '2026-11-08T12:00:00Z')['goal_classes'] == 'q-'


def test_working_needs_a_moving_member():
    row = _goal_row({'a': [('blocked', {}), ('backlog', {})]})
    shown = project_lane(row, None, '2026-11-08T12:00:00Z', sessions_by_stream=_working('spec_demo__a0'))
    assert shown['goal_classes'] == 'b-'


def test_moving_ninth_goal_is_sent_before_eight_blocked_and_goals_keep_rank_order():
    goals = {f'b{i}': [('blocked', {})] for i in range(8)}
    goals['moving'] = [('in_progress', {})]
    goals['queued'] = [('backlog', {})]
    row = _goal_row(goals, ranks={'moving': 50, 'queued': 0})
    shown = project_lane(row, None, '2026-11-08T12:00:00Z', sessions_by_stream={})
    sent = [goal['title'] for goal in shown['goals']]
    assert 'moving' in sent and 'queued' not in sent and len(sent) == 8
    assert shown['goals'] == sorted(shown['goals'], key=lambda goal: (goal['rank'], goal['goal_id']))
    assert shown['goal_classes'] == ''.join('m' if title == 'moving' else 'b' for title in sent) + '-'
    assert shown['goal_counts'] == [1, 8, 1, 0] and shown['goals_total'] == 10


def test_lane_progress_at_is_newest_over_full_membership():
    rows = {'a': [('in_progress', {'_progress_at': f'2026-11-0{i + 1}T00:00:00Z'}) for i in range(9)]}
    row = _goal_row(rows, legacy=[('backlog', {'_progress_at': None})])
    shown = project_lane(row, None, '2026-11-08T12:00:00Z')
    assert len(shown['members']) == 8 and shown['progress_at'] == '2026-11-09T00:00:00Z'
    assert 'progress_at' not in shown['members'][0]
    assert project_lane(_goal_row({}), None, '2026-11-08T12:00:00Z')['progress_at'] is None
    assert project_lane(_goal_row({}), None, '2026-11-08T12:00:00Z')['state_at'] is None


def test_detail_goal_keys_and_time_left_population():
    estimate = lambda median: {'p25': median, 'p75': median, 'median': median, 'as_of': '2026-11-10', 'provisional': False}
    row = _goal_row({
        'mixed': [('completed', {'estimate': estimate(9)}), ('in_progress', {'estimate': estimate(1.25),
                   '_progress_at': '2026-11-02T00:00:00Z'}), ('blocked', {'estimate': estimate(2)}),
                  ('backlog', {'estimate': None, 'estimate_exempt': True})],
        'unestimated': [('in_progress', {'estimate': estimate(1)}), ('backlog', {'estimate': None})],
        'emptypop': [('completed', {'estimate': estimate(3)})],
        'stale': [('in_progress', {'estimate': estimate(1), 'observation': {'quality': 'stale'}})],
        'huge': [('in_progress', {'estimate': estimate(99999.97)}), ('backlog', {'estimate': estimate(5)})]},
        legacy=[('needs_qa', {'estimate': estimate(0.5), '_progress_at': '2026-11-03T00:00:00Z'})])
    shown = project_lane(row, None, '2026-11-08T12:00:00Z', all_members=True,
                         sessions_by_stream=_working('spec_demo__mixed1'))
    goals = {goal['title']: goal for goal in shown['goals']}
    assert {key: goals['mixed'][key] for key in ('moving', 'blocked', 'working', 'left_h', 'progress_at')} == {
        'moving': 1, 'blocked': 1, 'working': True, 'left_h': 3.2, 'progress_at': '2026-11-02T00:00:00Z'}
    assert goals['unestimated']['left_h'] is None and goals['unestimated']['working'] is False
    assert goals['emptypop']['left_h'] is None and goals['stale']['left_h'] is None
    assert goals['huge']['left_h'] == 99999.9 and goals['huge']['progress_at'] is None
    assert shown['default_goal'] == {'state': 'in_progress', 'done': 0, 'total': 1, 'moving': 1, 'blocked': 0,
                                     'working': False, 'left_h': 0.5, 'progress_at': '2026-11-03T00:00:00Z'}
    inventory = project_lane(row, None, '2026-11-08T12:00:00Z', sessions_by_stream={})
    assert set(inventory['default_goal']) == {'state', 'done', 'total'}
    assert all(set(goal) <= {'goal_id', 'title', 'state', 'done', 'total', 'rank', 'icon', 'target_date'}
               for goal in inventory['goals'])


def test_default_goal_members_are_reachable_unfiltered_across_pages(tmp_path):
    from work_lanes_projection import page_lane_detail
    root = tmp_path / 'memory'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator', members=[f'spec_demo__u{i:02}' for i in range(20)]))['lane']
        write_goal(root, lane['lane_id'])
        for i in range(20):
            write_item(root, f'n{i:02}')
            write_item(root, f'u{i:02}', epic=None)
        await reconcile(env.store, scanner(root))
        shown = await env.store.get_work_lane(lane['lane_id'])
        projection = projected(shown)
        unplaced, cursor = [], None
        while True:
            page = page_lane_detail(shown, projection, members_after=cursor)
            unplaced.extend(member['spec_id'] for member in page['members'] if not member.get('goal_id'))
            cursor = page['members_next']
            if cursor is None:
                break
        assert unplaced == [f'spec_demo__u{i:02}' for i in range(20)]
        assert page['projection']['default_goal']['total'] == 20
        assert {'moving', 'blocked', 'working', 'left_h', 'progress_at'} <= set(page['projection']['default_goal'])
        assert 'summary_line' not in page['projection']['default_goal'] and 'recent' not in page['projection']['default_goal']
        with pytest.raises(ValueError, match='goal_filter_invalid'):
            page_lane_detail(shown, projection, goal_filter='default')
    run(body)
