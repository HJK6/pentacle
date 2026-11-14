"""Complete source-revision paging and uncapped offline manifests."""
import io
import json
from types import SimpleNamespace

import pytest

from agent_orch import work_lane_cli
from test_work_lane_cli import _run, _wire


def page(ids, *, revision='a' * 64, total=65, cursor=None):
    members = [{'spec_id': f'spec_demo__n{i:02}'} for i in ids]
    return {'type': 'work_lanes.show.ok', 'lane': {'lane_id': 'wl-test'},
            'projection': {'members': members}, 'members': members,
            'members_total': total, 'members_next': cursor, 'detail_revision': revision}


def test_show_members_traverses_65_once_and_preserves_goal_filter(monkeypatch, capsys):
    def reply(request):
        after = request.get('members_after')
        if after is None:
            return page(range(32), cursor='spec_demo__n31')
        if after == 'spec_demo__n31':
            return page(range(32, 64), cursor='spec_demo__n63')
        assert after == 'spec_demo__n63'
        return page([64])
    sent = _wire(monkeypatch, reply)
    goal = 'g_' + 'a' * 24
    assert _run(['work-lane', 'show', 'wl-test', '--members', '--json', '--goal-id', goal, '--composite-stream-id', 'home:assistant']) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result['members']) == len(set(m['spec_id'] for m in result['members'])) == 65
    assert len(result['projection']['members']) == len(result['lane']['members']) == 65
    assert all(request['goal_id'] == goal for request in sent) and len(sent) == 3


def test_one_revision_restart_then_second_change_is_bounded(monkeypatch):
    request = {'type': 'work_lanes.show', 'lane_id': 'wl-test'}
    first = page([0], total=2, cursor='spec_demo__n00')
    replies = iter([page([1], revision='b' * 64, total=2),
                    page([0], revision='b' * 64, total=2, cursor='spec_demo__n00'),
                    page([1], revision='b' * 64, total=2)])
    calls = []
    def call(_args, message):
        calls.append(message)
        return next(replies)
    monkeypatch.setattr(work_lane_cli, '_call', call)
    assert len(work_lane_cli._complete_members(SimpleNamespace(), request, first)['members']) == 2
    assert len(calls) == 3 and 'members_after' not in calls[1]
    replies = iter([page([1], revision='b' * 64), page([0], revision='b' * 64, total=2, cursor='spec_demo__n00'),
                    page([1], revision='c' * 64)])
    with pytest.raises(ValueError, match='changed twice'):
        work_lane_cli._complete_members(SimpleNamespace(), request, first)


@pytest.mark.parametrize('first,next_page', [
    (page([0, 0], total=2), None),
    (page([0], total=2), None),
    (page([0], total=2, cursor='spec_demo__n00'), page([0], total=2)),
    (page([0], total=3, cursor='spec_demo__n00'), page([1], total=3, cursor='spec_demo__n00')),
])
def test_partial_duplicate_and_nonadvancing_reads_are_not_complete(monkeypatch, first, next_page):
    monkeypatch.setattr(work_lane_cli, '_call', lambda *_: next_page)
    with pytest.raises(ValueError):
        work_lane_cli._complete_members(SimpleNamespace(), {}, first)


def test_offline_65_manifest_checks_full_count_and_uniqueness(monkeypatch, tmp_path, capsys):
    root = tmp_path / 'memory';ids = [f'spec_demo__n{i:02}' for i in range(65)]
    for identity in ids:
        folder = root / 'work/in_progress' / identity[5:];folder.mkdir(parents=True)
        (folder / 'spec.md').write_text(f'---\nid: {identity}\n---\n## Estimate\n- remaining_work_h: 1–2 (median 1.5) as_of 2026-11-08\n')
        (folder / 'summary.md').write_text('Synthetic summary\n')
    lane = {'composite_stream_id': 'example:assistant', 'lane_id': 'wl-test', 'state': 'paused',
            'members_total': 65, 'members': ids}
    def run_manifest(value):
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'schema': 'work_lane_estimate_manifest_v1', 'lanes': [value]})))
        return _run(['work-lane', 'estimate-preview', '--memory-root', str(root), '--lanes-json', '-'])
    assert run_manifest(lane) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['errors'] == [] and result['lanes'][0]['open_estimate_h'] == {'p25': 65, 'p75': 130, 'median': 97.5}
    assert run_manifest({**lane, 'members': ids[:-1]}) == 2
    assert run_manifest({**lane, 'members': ids[:-1] + [ids[0]]}) == 2


# ---------------------------------------------------------------------------
# V8: the command passes goals, drops the display-only new_lane_id, and lane-id.
# ---------------------------------------------------------------------------

def test_v8_adopt_apply_passes_goals_and_drops_new_lane_id(monkeypatch, tmp_path, capsys):
    sent = _wire(monkeypatch, lambda req: {"type": "assistant.operation.ok", "lane": {"lane_id": "wl-made"},
                                           "duplicate": False})
    apply = tmp_path / "apply.json"
    apply.write_text(json.dumps([{"adoption_key": "stream:new", "title": "New", "owner_kind": "fd",
        "work_state": "paused", "visible_chat": {"stream_id": "home:assistant"},
        "goals": ["epic_one"], "new_lane_id": "wl-display-only", "no_spec_reason": "Fixture"}]))
    assert _run(["work-lane", "adopt", "--apply", str(apply), "--composite-stream-id", "home:assistant"]) == 0
    payload = sent[-1]["payload"]
    assert payload["goals"] == ["epic_one"]
    assert "new_lane_id" not in payload


def test_v8_lane_id_prints_daemon_assigned_id(monkeypatch, capsys):
    from agent_orch_lane_id_helper import expected
    assigned = expected("home:assistant", "stream:thing")
    sent = _wire(monkeypatch, lambda req: {"type": "work_lanes.lane_id.ok",
                                           "adoption_key": req["adoption_key"], "lane_id": assigned})
    assert _run(["work-lane", "lane-id", "--adoption-key", "stream:thing",
                 "--composite-stream-id", "home:assistant"]) == 0
    assert sent[-1] == {"type": "work_lanes.lane_id", "composite_stream_id": "home:assistant",
                        "adoption_key": "stream:thing"}
    assert capsys.readouterr().out.strip() == assigned


def test_v8_preview_to_apply_round_trips_both_shapes(monkeypatch, capsys):
    # A new-lane candidate carries new_lane_id and no lane_id; a routing-row
    # candidate carries lane_id. Apply sends goals through and preserves the shape.
    seen = []
    sent = _wire(monkeypatch, lambda req: seen.append(req) or {"type": "assistant.operation.ok",
                                                              "lane": {"lane_id": req["payload"].get("lane_id", "wl-new")},
                                                              "duplicate": False})
    import tempfile
    from pathlib import Path
    apply = Path(tempfile.mkdtemp()) / "apply.json"
    apply.write_text(json.dumps([
        {"adoption_key": "stream:newlane", "title": "New lane", "owner_kind": "fd", "work_state": "paused",
         "visible_chat": {"stream_id": "home:assistant"}, "goals": ["epic_newlane"],
         "new_lane_id": "wl-newid", "no_spec_reason": "Fixture"},
        {"adoption_key": "request:rrow", "title": "Routing row", "owner_kind": "fd", "work_state": "paused",
         "visible_chat": {"stream_id": "home:assistant"}, "lane_id": "wl-rrow", "goals": ["epic_rrow"],
         "summary": "s"}]))
    assert _run(["work-lane", "adopt", "--apply", str(apply), "--composite-stream-id", "home:assistant"]) == 0
    new_payload = seen[0]["payload"]
    assert new_payload["goals"] == ["epic_newlane"] and "lane_id" not in new_payload and "new_lane_id" not in new_payload
    row_payload = seen[1]["payload"]
    assert row_payload["lane_id"] == "wl-rrow" and row_payload["goals"] == ["epic_rrow"]
