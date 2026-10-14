"""Parking grants only unmarked criteria and retains source-change fences."""
import json
import os
from pathlib import Path

import pytest
from _shared.custom_ac import check_lane_members, CompletionRefusal
from test_lane_close_residual_guard import write_spec, MEMBER

PARK = '- parked: 2026-10-14 operator-pause — program paused'
FIXTURES = Path(__file__).parent / 'fixtures/parked_incident_20261014'


@pytest.mark.parametrize('form', ['original', 'transformed'])
def test_incident_population(form):
    external = os.environ.get('PARKED_EXACT_FIXTURES')
    root = Path(external) if external else FIXTURES
    manifest = json.loads((root / ('exact_manifest.json' if external else 'manifest.json')).read_text())
    members = ([s['spec_id'] for s in manifest['sources'] if s['spec_id'] != manifest['resume_spec_id']]
               if external else manifest['members'])
    source = root / 'exact' / form if external else root / form
    result = check_lane_members(source, members)['dispositions']
    expected = 'tracked' if form == 'original' else 'parked'
    assert sum(d['disposition'] == expected for d in result) == manifest['unmarked_criteria'] == 53
    assert all(d['disposition'] in {expected, 'checked', 'waived'} for d in result)


@pytest.mark.parametrize('status,tracking,reason', [
    ('in_progress', PARK, 'undisposed'),
    ('blocked', '', 'park_line_missing'),
    ('blocked', '```\n' + PARK + '\n```', 'park_line_missing'),
    ('blocked', '## Other\n' + PARK, 'park_line_missing'),
    ('blocked', PARK.replace('2026-10-14', '2026-02-30'), 'park_line_malformed'),
    ('blocked', PARK.replace('operator-pause', ''), 'park_line_malformed'),
    ('blocked', PARK.replace('program paused', ''), 'park_line_malformed'),
    ('blocked', PARK + '\n- parked: broken', 'park_line_malformed'),
])
def test_park_boundaries(tmp_path, status, tracking, reason):
    root = tmp_path.resolve()
    write_spec(root, status=status, tracking=tracking)
    with pytest.raises(CompletionRefusal) as caught:
        check_lane_members(root, [MEMBER])
    assert caught.value.code == 'lane_residuals_undisposed'
    assert caught.value.details['items'][0]['reason'] == reason
    if reason.startswith('park_line_'):
        assert '- parked: YYYY-MM-DD <authority-ref> — <reason>' in str(caught.value)


def test_last_park_and_resume(tmp_path):
    root = tmp_path.resolve()
    path = write_spec(root, status='blocked', tracking='- parked: broken\n' + PARK)
    result = check_lane_members(root, [MEMBER])['dispositions'][0]
    assert {k: result[k] for k in ('disposition', 'date', 'authority', 'reason')} == {
        'disposition': 'parked', 'date': '2026-10-14', 'authority': 'operator-pause', 'reason': 'program paused'}
    destination = root / 'work/in_progress' / path.parent.name
    destination.parent.mkdir(parents=True)
    path.parent.rename(destination)
    with pytest.raises(CompletionRefusal) as caught:
        check_lane_members(root, [MEMBER])
    assert caught.value.details['items'][0]['reason'] == 'undisposed'


@pytest.mark.parametrize('marker,expected', [
    ('- [x] **Checked** — complete', 'checked'),
    ('- [ ] **Waived** — behavior (waived: decision)', 'waived'),
    ('- [ ] **Tracked** — behavior tracked: spec_alpha__follow (new_scope)', 'tracked'),
    ('- [ ] **Tracked** — behavior tracked: spec_missing__target (new_scope)', 'follow_up_not_found'),
    ('- [ ] **Invalid** — behavior tracked: broken', 'disposition_invalid'),
    ('- [ ] **Invalid** — behavior (waived: )', 'disposition_invalid'),
])
def test_markers_under_park(tmp_path, marker, expected):
    root = tmp_path.resolve()
    write_spec(root, status='blocked', tracking=PARK, custom=marker)
    write_spec(root, 'spec_alpha__follow', status='backlog', custom='',
               tracking='- deferred_from: spec_alpha__member — Tracked: receipt reason')
    if expected in {'checked', 'waived', 'tracked'}:
        result = check_lane_members(root, [MEMBER])['dispositions']
        assert result[0]['disposition'] == expected
    else:
        with pytest.raises(CompletionRefusal) as caught:
            check_lane_members(root, [MEMBER])
        assert caught.value.details['items'][0]['reason'] == expected


def test_malformed_park_only_affects_unmarked(tmp_path):
    root = tmp_path.resolve()
    write_spec(root, status='blocked', tracking='- parked: broken',
               custom='- [x] Checked\n- [ ] Waived (waived: decision)')
    assert [d['disposition'] for d in check_lane_members(root, [MEMBER])['dispositions']] == ['checked', 'waived']


@pytest.mark.parametrize('route', ['local', 'remote', 'offline_operator', 'offline_manager'])
@pytest.mark.parametrize('stage', ['admission', 'commit'])
@pytest.mark.parametrize('change', ['line', 'folder'])
def test_park_close_seams(tmp_path, monkeypatch, route, stage, change):
    from test_lane_close_residual_guard import (
        PreparedRace, ordered_close_context, ADMISSION_CALLBACK, CLOSE_CALLBACK)
    from test_work_lanes import FD, run
    from sessions import VerbError
    root = tmp_path.resolve()
    path = write_spec(root, status='blocked', tracking=PARK)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))

    async def body(env):
        lead = await env.seat('park-race', role='lead', parent_stream_id=FD)
        await env.adopt(lead=lead, members=[MEMBER])
        def mutation(prepared):
            if change == 'line':
                path.write_text(path.read_text().replace(PARK, '- parked: broken'))
            else:
                destination = root / 'work/in_progress' / path.parent.name
                destination.parent.mkdir(parents=True)
                path.parent.rename(destination)
        seam = PreparedRace(env, monkeypatch, path, mutation, route=route,
                            callback=ADMISSION_CALLBACK if stage == 'admission' else CLOSE_CALLBACK)
        sessions, transport, kwargs = await ordered_close_context(env, lead, seam, monkeypatch, route)
        if stage == 'admission':
            with pytest.raises(VerbError) as caught:
                await sessions.close(*lead[0].split(':', 1), **kwargs)
            assert caught.value.code == 'member_spec_unreadable'
            assert transport.kills == 0
            assert (await env.store.fetch_session(*lead[0].split(':', 1)))['status'] == 'open'
        else:
            result = await sessions.close(*lead[0].split(':', 1), reason='Fixture close', **kwargs)
            assert result['session']['status'] == 'closed'
            flagged = not route.startswith('offline')
            assert bool(result['session'].get('completion_source_changed')) is flagged
            if not flagged:
                assert result['reap_status'] == 'deferred_host_offline' and transport.kills == 0
            audit = await env.store.latest_close_audit(lead[0])
            clause = f'parked: {MEMBER} criteria=1 date=2026-10-14 authority=operator-pause reason=program paused'
            assert clause in audit['reason']
            assert ('u1_source_changed_after_termination:' in audit['reason']) is flagged
            from server import Server
            server = Server(store=env.store, sessions=sessions, local_host=sessions.local_host)
            inspected = await server._on_inspect_stream({'stream_id': lead[0], 'event_tail': 0})
            assert inspected['close_audit']['reason'] == audit['reason']
        seam.verify(f'park:{route}:{stage}:{change}')
    run(body)


def test_audit_groups_members_once():
    from store_work_lanes import _LaneCloseAuditReason
    dispositions = [dict(spec_id=member, disposition='parked', date='2026-10-14',
                         authority='operator-pause', reason='program paused')
                    for member in ('spec_alpha__b', 'spec_alpha__a', 'spec_alpha__a')]
    assert str(_LaneCloseAuditReason('Close', 'generation', dispositions)) == (
        'Close; parked: spec_alpha__a criteria=2 date=2026-10-14 authority=operator-pause reason=program paused; '
        'parked: spec_alpha__b criteria=1 date=2026-10-14 authority=operator-pause reason=program paused')


@pytest.mark.parametrize('route', ['local', 'remote', 'offline_operator', 'offline_manager'])
def test_parked_normal_close(tmp_path, monkeypatch, route):
    from test_work_lanes import FD, run
    from test_lane_close_residual_guard import close_context
    root = tmp_path.resolve()
    write_spec(root, status='blocked', tracking=PARK,
               custom='- [ ] First\n- [ ] Second\n- [x] Complete\n- [ ] Waived (waived: decision)')
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lead = await env.seat('park-close', role='lead', parent_stream_id=FD)
        await env.adopt(lead=lead, members=[MEMBER])
        sessions, transport = await close_context(env, lead, remote=route != 'local',
                                                 online=not route.startswith('offline'))
        kwargs = ({'operator_confirm': True} if route == 'offline_operator' else
                  {'close_kind': 'manager_close'} if route == 'offline_manager' else {})
        result = await sessions.close(*lead[0].split(':', 1), **kwargs)
        assert result['session']['status'] == 'closed'
        audit = await env.store.latest_close_audit(lead[0])
        assert audit['disposition'] == 'closed'
        clause = f'parked: {MEMBER} criteria=2 date=2026-10-14 authority=operator-pause reason=program paused'
        assert clause in audit['reason']
        assert audit['reason'].count('parked:') == 1
    run(body)
