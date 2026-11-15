"""Source-derived membership, uncapped goal progress and stable detail identity."""
import json
from pathlib import Path

import pytest

from _shared.specs_service import SpecsSubsystem
from _shared.work_goals import derive_lane_id, goal_id, goal_progress, parse_goal_facts, project_goals, wire_title
from test_assistant_prose_mirror import ASSISTANT
from test_work_lanes import run, seed_pending_goal
from work_lanes_projection import project_lane


async def adopt_with_goal(env, *, key='stream:x', name='fixture', **kw):
    """Adopt a lane with one real goal via the proper flow: a charter the scan
    observed names the lane, then adopt accepts it. The caller's own write_goal
    then replaces the seeded facts with the real charter, leaving one goal."""
    await seed_pending_goal(env.store, 'epic_' + name, derive_lane_id(ASSISTANT, key))
    return (await env.adopt(key=key, goals=['epic_' + name], **kw))['lane']


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
                                              'moving': 0, 'blocked': 0, 'working': False, 'left_h': None, 'progress_at': None, 'pos': 0}
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
        lane = await adopt_with_goal(env, state='paused', owner='operator')
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
        # Both lanes are created with their own goal (a lane cannot be goal-less).
        a = await adopt_with_goal(env, key='request:a', name='fixture', state='paused', owner='operator')
        b = await adopt_with_goal(env, key='request:b', name='bside', state='paused', owner='operator')
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
        # b keeps its own goal plus the one moved in from a; the moved goal's
        # member contribution survives the root loss as last-good.
        assert len(lost['members']) == 1 and projected(lost)['goals_total'] == 2
        moved = next(g for g in projected(lost)['goals'] if g['goal_id'] == goal_id('epic_fixture'))
        assert moved['state'] == 'in_progress'
    run(body)


def test_named_goal_and_digest_survive_restart_without_reinterpretation(tmp_path):
    root, database = tmp_path / 'memory', str(tmp_path / 'state.db')
    retained = {}
    async def first(env):
        lane = await adopt_with_goal(env, state='paused', owner='operator')
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


@pytest.mark.parametrize('cell_name', ['goals_v1', 'goal_road_v1'])
def test_shared_goals_fixture_matches_inventory_and_show(cell_name):
    from work_lanes_projection import build_frame
    cell = json.loads((Path(__file__).resolve().parents[3] /
                       'pentacle-chat-core/tests/fixtures/work-lanes-inventory.json').read_text())[cell_name]
    assert build_frame([cell['lane']], cell['sessions'], now_iso=cell['now_iso'],
                       worked_by_spec=cell['worked_by_spec']) == cell['inventory_frame']
    assert project_lane(cell['lane'], None, cell['now_iso'], all_members=True,
                        sessions_by_stream=cell['sessions'], worked_by_spec=cell['worked_by_spec']) == cell['show_projection']


def test_maximum_goal_envelope_and_correlated_full_frame_budgets():
    from server import _encode_frame
    from test_work_lane_spec_fields import _budget_frame
    maximum = {'goal_id': 'g_' + 'f' * 24, 'title': wire_title(('😀\\\"\n' * 100)),
               'state': 'in_progress', 'done': 2147483647, 'total': 2147483647,
               'rank': 2147483647, 'pos': 63, 'target_date': '9999-12-31', 'icon': 'briefcase'}
    envelope = {'goals': [maximum] * 8, 'goals_total': 2147483647,
                'default_goal': {'state': 'in_progress', 'done': 2147483647, 'total': 2147483647},
                'detail_revision': 'f' * 64}
    compact_size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())
    assert compact_size(envelope) <= 2624
    road = {'goal_road': 'w' * 64}
    assert compact_size(road) - compact_size({}) + 1 <= 79
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
            lane.update(road)
            lane['hue'] = 'chartreuse'
            lane['parked'] = {'at': '9999-12-31T23:59:59Z', 'reason': '😀' * 160}
            for member in lane['members']:
                member['goal_id'] = maximum['goal_id']
                prior = {key: value for key, value in member.items()
                         if key not in {'worked', 'lead_seat', 'status_line', 'next_line', 'goal_id'}}
                assert compact_size(member) - compact_size(prior) <= 800
        assert len(_encode_frame(frame).encode()) <= limit


def test_source_membership_replacement_clears_completion_episode_even_if_still_terminal(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = await adopt_with_goal(env, state='paused', owner='operator')
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
        lane = await adopt_with_goal(env, state='paused', owner='operator')
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
        lane = await adopt_with_goal(env, state='paused', owner='operator')
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


def test_working_on_blocked_member_takes_precedence():
    row = _goal_row({'a': [('blocked', {}), ('backlog', {})]})
    shown = project_lane(row, None, '2026-11-08T12:00:00Z', sessions_by_stream=_working('spec_demo__a0'))
    assert shown['goal_classes'] == 'w-'
    assert shown['goal_counts'] == [1, 0, 0, 0]


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
    assert all(set(goal) <= {'goal_id', 'title', 'state', 'done', 'total', 'rank', 'icon', 'target_date', 'pos'}
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


@pytest.mark.parametrize('status', ['backlog', 'ready_for_dev', 'blocked'])
def test_working_unfinished_member_sets_working_goal_in_any_stage(status):
    from _shared.work_goals import goal_class
    assert goal_class([{'spec_id': 'spec_demo__working', 'status': status}], {'spec_demo__working'}) == 'w'


def test_ten_goal_road_and_positions_align_full_order():
    from _shared.work_goals import goal_class
    records, members = {}, []
    for i in range(10):
        identity = f'epic_fixture_{i:02}'
        records[identity] = {'quality': 'fresh', 'facts': {'title': f'Goal {i}', 'rank': i // 2}}
        members.append({'spec_id': f'spec_demo__item{i}', 'goal_id': goal_id(identity),
                        'status': 'in_progress' if i == 9 else 'completed', 'observation': {'quality': 'fresh'}})
    lane = {'_goals': records, '_members': members, '_work_index_available': True}
    frame = project_goals(lane)
    detail = project_goals(lane, all_goals=True)
    expected = sorted(records, key=lambda identity: (records[identity]['facts']['rank'], goal_id(identity)))
    assert frame['goals_total'] == 10 and len(frame['goals']) == 8
    assert [g['goal_id'] for g in detail['goals']] == [goal_id(identity) for identity in expected]
    assert frame['goal_road'] == detail['goal_road'] == ''.join(
        goal_class([m for m in members if m['goal_id'] == goal_id(identity)], set()) for identity in expected)
    assert [g['pos'] for g in detail['goals']] == list(range(10))
    for index, goal in enumerate(frame['goals']):
        assert goal['pos'] == expected.index(next(identity for identity in expected if goal_id(identity) == goal['goal_id']))
        assert frame['goal_road'][goal['pos']] == frame['goal_classes'][index]


@pytest.mark.parametrize('n', [0, 64, 65])
def test_goal_road_bounds_and_default_exclusion(n):
    goals = {f'epic_fixture_{i}': {'facts': {'title': f'Goal {i}', 'rank': 1000}, 'quality': 'fresh'} for i in range(n)}
    lane = {'_goals': goals, '_members': [{'spec_id': 'spec_demo__default', 'status': 'backlog'}],
            '_working_specs': {'spec_demo__default'}}
    for detail in [False, True]:
        value = project_goals(lane, all_goals=detail)
        assert value['goals_total'] == n and value['goal_classes'].endswith('w')
        assert value['goal_counts'] == [1, 0, n, 0]
        if n <= 64:
            assert value['goal_road'] == 'q' * n
            assert all(g['pos'] < n for g in value['goals'])
        else:
            assert 'goal_road' not in value
            assert all('pos' not in g for g in value['goals'])
        if detail:
            assert value['default_goal']['working'] is True


@pytest.mark.parametrize('members,working,expected', [
    ([('completed', 'done'), ('in_progress', 'idle')], {'done'}, 'm'),
    ([('completed', 'done')], {'done'}, 'd'),
    ([('deprecated', 'old'), ('backlog', 'idle')], {'old'}, 'q'),
    ([('blocked', 'idle')], set(), 'b'),
    ([('analysis', 'idle')], set(), 'm'),
])
def test_goal_working_precedence_excludes_completed_and_deprecated(members, working, expected):
    from _shared.work_goals import goal_class
    assert goal_class([{'status': status, 'spec_id': identity} for status, identity in members], working) == expected


# ---------------------------------------------------------------------------
# V1 / V2: a lane cannot be created without an observed goal that names it.
# ---------------------------------------------------------------------------

async def _lane_event_counts(store):
    def q(conn):
        return (conn.execute("SELECT COUNT(*) FROM v2_assistant_composite_lanes WHERE work_state IS NOT NULL").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM v2_work_lane_events").fetchone()[0])
    return await store.submit(q)


def _write_raw_goal(root, name, body):
    path = root / 'epics' / (name + '.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_v1_goal_less_creation_refused_writes_nothing(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        (root / 'work').mkdir(parents=True, exist_ok=True)
        key = 'stream:v1'
        lane_id = derive_lane_id(ASSISTANT, key)
        async def refused(code, goals, *, seed_name=None):
            before = await _lane_event_counts(env.store)
            payload = {'adoption_key': key, 'title': 'V1', 'summary': 'g', 'owner_kind': 'operator',
                       'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT},
                       'no_spec_reason': 'Fixture'}
            if goals is not None:
                payload['goals'] = goals
            with pytest.raises(ValueError) as exc:
                await env.op('adopt', payload, request_id='adopt:' + key, inject_goals=False)
            assert str(exc.value).split(':')[0] == code, (code, str(exc.value))
            assert await _lane_event_counts(env.store) == before  # a refusal writes nothing
        # Missing and empty both demand a goal.
        await refused('work_lane_goal_required', None)
        await refused('work_lane_goal_required', [])
        # Shape errors are payload-invalid.
        await refused('work_lane_payload_invalid', ['not-an-epic'])
        await refused('work_lane_payload_invalid', ['epic_dup', 'epic_dup'])
        await refused('work_lane_payload_invalid', ['epic_' + str(n) for n in range(17)])
        # Unhashable elements must be the specified refusal, not a TypeError from set().
        await refused('work_lane_payload_invalid', [{}])
        await refused('work_lane_payload_invalid', [[]])
        # An epic the daemon has not observed is unresolved.
        await refused('work_lane_goal_unresolved', ['epic_unobserved'])
        # A valid charter that names a different lane is unresolved for this one.
        write_goal(root, derive_lane_id(ASSISTANT, 'stream:elsewhere'), 'other')
        # An invalid charter observed under its own id (names no lane, no Goal
        # section) and a duplicated id both leave no pending facts for this lane.
        _write_raw_goal(root, 'broken', '---\nid: epic_broken\ntype: epic\ntitle: Broken\nsummary: S\n---\nNo goal section.\n')
        # A genuinely parser-invalid charter (malformed YAML frontmatter) leaves no
        # pending facts either; adopting its id is unresolved, never a silent create.
        _write_raw_goal(root, 'malformed', '---\nid: epic_malformed\ntype: epic\ntitle: [unterminated\n'
                        '  lane_id: ' + lane_id + '\n: : :\n---\n## Goal\n\nBad frontmatter.\n')
        _write_raw_goal(root, 'twinA', '---\nid: epic_twin\ntype: epic\ntitle: T\nsummary: S\nlane_id: '
                        + lane_id + '\n---\n## Goal\n\nDone.\n')
        _write_raw_goal(root, 'twinB', '---\nid: epic_twin\ntype: epic\ntitle: T\nsummary: S\nlane_id: '
                        + lane_id + '\n---\n## Goal\n\nDone.\n')
        await reconcile(env.store, scanner(root))
        await refused('work_lane_goal_unresolved', ['epic_other'])     # names stream:elsewhere
        await refused('work_lane_goal_unresolved', ['epic_broken'])    # valid parse, names no lane, no pending
        await refused('work_lane_goal_unresolved', ['epic_malformed']) # parser-invalid frontmatter, no pending
        await refused('work_lane_goal_unresolved', ['epic_twin'])      # duplicated, ambiguous
        # The work index being unavailable is also unresolved, not a silent create.
        write_goal(root, lane_id, 'ready')
        await reconcile(env.store, scanner(root))
        await env.store.mark_work_index_pending()
        await refused('work_lane_goal_unresolved', ['epic_ready'])
    run(body, str(tmp_path / 'v1.db'))


def test_v1_promotion_shape_refuses_goal_less_and_unresolved(tmp_path):
    from test_work_lanes_adoption import _admit_routing_lane
    root = tmp_path / 'memory'
    async def body(env):
        (root / 'work').mkdir(parents=True, exist_ok=True)
        lane_id = 'wl-promote0000000000000'
        await _admit_routing_lane(env, lane_id, 'Routing candidate', 'req-promote')
        base = {'adoption_key': 'request:promote-me', 'title': 'Promo', 'summary': 'g', 'owner_kind': 'operator',
                'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT}, 'lane_id': lane_id,
                'no_spec_reason': 'Fixture'}
        seq = [0]
        async def refused(code, goals):
            before = await _lane_event_counts(env.store)
            payload = dict(base)
            if goals is not None:
                payload['goals'] = goals
            seq[0] += 1
            with pytest.raises(ValueError) as exc:
                await env.op('adopt', payload, request_id=f'promote-x{seq[0]}', inject_goals=False)
            assert str(exc.value).split(':')[0] == code, (code, str(exc.value))
            assert await _lane_event_counts(env.store) == before  # a refusal writes nothing
        # The promotion shape enforces the SAME goal refusal matrix as new-lane creation.
        await refused('work_lane_goal_required', None)
        await refused('work_lane_goal_required', [])
        await refused('work_lane_payload_invalid', ['not-an-epic'])
        await refused('work_lane_payload_invalid', ['epic_dup', 'epic_dup'])
        await refused('work_lane_payload_invalid', ['epic_' + str(i) for i in range(17)])
        await refused('work_lane_payload_invalid', [{}])
        await refused('work_lane_payload_invalid', [[]])
        await refused('work_lane_goal_unresolved', ['epic_absent'])
        write_goal(root, derive_lane_id(ASSISTANT, 'stream:elsewhere'), 'other')
        _write_raw_goal(root, 'broken', '---\nid: epic_broken\ntype: epic\ntitle: Broken\nsummary: S\n---\nNo goal section.\n')
        _write_raw_goal(root, 'malformed', '---\nid: epic_malformed\ntype: epic\ntitle: [unterminated\n'
                        '  lane_id: ' + lane_id + '\n: : :\n---\n## Goal\n\nBad frontmatter.\n')
        _write_raw_goal(root, 'twinA', '---\nid: epic_twin\ntype: epic\ntitle: T\nsummary: S\nlane_id: '
                        + lane_id + '\n---\n## Goal\n\nDone.\n')
        _write_raw_goal(root, 'twinB', '---\nid: epic_twin\ntype: epic\ntitle: T\nsummary: S\nlane_id: '
                        + lane_id + '\n---\n## Goal\n\nDone.\n')
        await reconcile(env.store, scanner(root))
        await refused('work_lane_goal_unresolved', ['epic_other'])      # names a different lane
        await refused('work_lane_goal_unresolved', ['epic_broken'])     # valid parse, no lane, no pending
        await refused('work_lane_goal_unresolved', ['epic_malformed'])  # parser-invalid frontmatter
        await refused('work_lane_goal_unresolved', ['epic_twin'])       # duplicated, ambiguous
        # A charter naming the routing row's own id promotes it with that goal.
        write_goal(root, lane_id, 'promo')
        await reconcile(env.store, scanner(root))
        # The index being unavailable is unresolved, not a silent promote.
        await env.store.mark_work_index_pending()
        await refused('work_lane_goal_unresolved', ['epic_promo'])
        await reconcile(env.store, scanner(root))
        out = await env.op('adopt', dict(base, goals=['epic_promo']), request_id='promote-c', inject_goals=False)
        assert out['lane']['lane_id'] == lane_id and out['lane']['work_state'] == 'paused'
        shown = await env.store.get_work_lane(lane_id)
        assert project_lane(shown['_projection_row'], None, '2026-11-14T00:00:00Z')['goals_total'] == 1
    run(body, str(tmp_path / 'v1p.db'))


def test_v1_goals_only_adoption_without_members_or_no_spec_reason(tmp_path):
    """F1: valid observed goals justify a lane, so neither members nor a
    no_spec_reason is required; missing goals is goal_required, not
    no_spec_reason_required, for both the new-lane and promotion shapes."""
    root = tmp_path / 'memory'
    async def body(env):
        (root / 'work').mkdir(parents=True, exist_ok=True)
        key = 'stream:goalonly'
        lane_id = derive_lane_id(ASSISTANT, key)
        await seed_pending_goal(env.store, 'epic_goalonly', lane_id)
        # New-lane shape: goals, no members, no no_spec_reason -> created.
        out = await env.op('adopt', {'adoption_key': key, 'title': 'Goals only', 'summary': 'g',
            'owner_kind': 'operator', 'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT},
            'goals': ['epic_goalonly']}, request_id='adopt:' + key, inject_goals=False)
        assert out['lane']['lane_id'] == lane_id and out['lane']['members'] == [] and out['lane']['no_spec_reason'] is None
        # Missing goals (also no members, no no_spec_reason) is goal_required, writing nothing.
        before = await _lane_event_counts(env.store)
        with pytest.raises(ValueError, match='work_lane_goal_required'):
            await env.op('adopt', {'adoption_key': 'stream:nogoal', 'title': 'No goal', 'summary': 'g',
                'owner_kind': 'operator', 'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT}},
                request_id='adopt:stream:nogoal', inject_goals=False)
        assert await _lane_event_counts(env.store) == before
        # Promotion shape: a routing row promoted by its goal alone, no members/no_spec_reason.
        from test_work_lanes_adoption import _admit_routing_lane
        await _admit_routing_lane(env, 'wl-promote0000000000000', 'Routing candidate', 'req-promote')
        write_goal(root, 'wl-promote0000000000000', 'promo')
        await reconcile(env.store, scanner(root))
        promoted = await env.op('adopt', {'adoption_key': 'request:promote-me', 'title': 'Promo', 'summary': 'g',
            'owner_kind': 'operator', 'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT},
            'lane_id': 'wl-promote0000000000000', 'goals': ['epic_promo']},
            request_id='promote-goalonly', inject_goals=False)
        assert promoted['lane']['lane_id'] == 'wl-promote0000000000000' and promoted['lane']['no_spec_reason'] is None
    run(body, str(tmp_path / 'v1goalonly.db'))


def test_v2_creation_with_a_goal_is_immediate_and_replayable(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        (root / 'work').mkdir(parents=True, exist_ok=True)
        key = 'stream:v2'
        lane_id = derive_lane_id(ASSISTANT, key)
        write_goal(root, lane_id, 'ready')
        await reconcile(env.store, scanner(root))
        payload = {'adoption_key': key, 'title': 'V2', 'summary': 'g', 'owner_kind': 'operator',
                   'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT},
                   'goals': ['epic_ready'], 'no_spec_reason': 'Fixture'}
        out = await env.op('adopt', payload, request_id='adopt:' + key, inject_goals=False)
        assert out['lane']['lane_id'] == lane_id
        # The goal is present the moment the lane exists, with no index pass between.
        shown = await env.store.get_work_lane(lane_id)
        assert project_lane(shown['_projection_row'], None, '2026-11-14T00:00:00Z')['goals_total'] == 1
        # The charter record is fresh and carries no pending key once accepted.
        def goal_record(conn):
            from store_work_index import _state_conn
            return _state_conn(conn)['goal_observations']['epic_ready']
        record = await env.store.submit(goal_record)
        assert record['quality'] == 'fresh' and 'pending' not in record
        # Replaying the same request id returns the stored result, not a new lane.
        again = await env.op('adopt', payload, request_id='adopt:' + key, inject_goals=False)
        assert again['duplicate'] is True and again['lane']['lane_id'] == lane_id
        assert len(await env.store.work_lane_rows()) == 1
    run(body, str(tmp_path / 'v2.db'))


def test_v2_index_pass_after_adoption_keeps_acceptance(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        (root / 'work').mkdir(parents=True, exist_ok=True)
        key = 'stream:v2race'
        lane_id = derive_lane_id(ASSISTANT, key)
        write_goal(root, lane_id, 'race')
        source = scanner(root)
        await reconcile(env.store, source)
        out = await env.op('adopt', {'adoption_key': key, 'title': 'Race', 'summary': 'g',
            'owner_kind': 'operator', 'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT},
            'goals': ['epic_race'], 'no_spec_reason': 'Fixture'}, request_id='adopt:' + key, inject_goals=False)
        assert out['lane']['lane_id'] == lane_id
        # A full index pass after the adoption keeps the accepted charter fresh for
        # the lane; the changed-base check in reconcile never overwrites it.
        await reconcile(env.store, source)
        def goal_record(conn):
            from store_work_index import _state_conn
            return _state_conn(conn)['goal_observations']['epic_race']
        record = await env.store.submit(goal_record)
        assert record['quality'] == 'fresh' and record['facts']['lane_id'] == lane_id and 'pending' not in record
    run(body, str(tmp_path / 'v2race.db'))


def test_v2_held_scan_prepared_before_adoption_does_not_revert_acceptance(tmp_path):
    """F5: the changed-base protection. A scan computed while the goal is still
    pending (lane not yet created) must not, when applied after the adoption,
    revert the acceptance written in the adopt transaction."""
    root = tmp_path / 'memory'
    async def body(env):
        (root / 'work').mkdir(parents=True, exist_ok=True)
        key = 'stream:v2held'
        lane_id = derive_lane_id(ASSISTANT, key)
        write_goal(root, lane_id, 'held')
        source = scanner(root)
        await reconcile(env.store, source)  # epic_held observed as pending (lane absent)
        pre = await env.store.submit(lambda conn: __import__('store_work_index')._state_conn(conn)
                                     ['goal_observations']['epic_held'])
        assert 'pending' in pre  # the charter is pending before the lane exists
        # Prepare a scan snapshot BEFORE the adoption and hold it unapplied.
        held = source.scan_work_observations()
        out = await env.op('adopt', {'adoption_key': key, 'title': 'Held', 'summary': 'g',
            'owner_kind': 'operator', 'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT},
            'goals': ['epic_held'], 'no_spec_reason': 'Fixture'}, request_id='adopt:' + key, inject_goals=False)
        assert out['lane']['lane_id'] == lane_id
        # Applying the pre-adoption snapshot recomputes against the post-adopt
        # state and leaves the acceptance fresh (never reverts it to pending).
        await env.store.reconcile_work_observations(held, settle_s=0, sweep=True)
        def goal_record(conn):
            from store_work_index import _state_conn
            return _state_conn(conn)['goal_observations']['epic_held']
        record = await env.store.submit(goal_record)
        assert record['quality'] == 'fresh' and record['facts']['lane_id'] == lane_id and 'pending' not in record
    run(body, str(tmp_path / 'v2held.db'))
