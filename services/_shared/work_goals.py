"""Source goal facts and pure, bounded lane-goal projections."""
from __future__ import annotations

import copy
from datetime import date
import hashlib
import json
import re

import yaml

from .specs_parser import _section, _without_fences
from .lane_icons import validate_icon

EPIC_ID = re.compile(r'^epic_[a-z0-9_]+$')
LANE_ID = re.compile(r'^(?:wl-|assistant-lane-)[A-Za-z0-9_-]+$')
MAX_COUNT = 2147483647


class _GoalLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, 'anchor', None) or getattr(event, 'tag', None):
            raise ValueError('work_goal_yaml_graph_or_tag')
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError('work_goal_yaml_duplicate_key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_goal_facts(text: str) -> dict:
    match = re.match(r'\A---[^\S\n]*\n(.*?)^---[^\S\n]*(?:\n|$)', text, re.M | re.S)
    if not match:
        raise ValueError('work_goal_frontmatter_invalid')
    metadata = yaml.load(match[1], Loader=_GoalLoader)
    if not isinstance(metadata, dict) or metadata.get('type') != 'epic':
        raise ValueError('work_goal_type_invalid')
    identity = metadata.get('id')
    if not isinstance(identity, str) or not EPIC_ID.fullmatch(identity):
        raise ValueError('work_goal_id_invalid')
    title, summary = metadata.get('title'), metadata.get('summary')
    if any(not isinstance(value, str) or not value.strip() for value in (title, summary)):
        raise ValueError('work_goal_text_invalid')
    title.encode('utf-8')
    lane = metadata.get('lane_id')
    # Legacy charters remain observations, without inventing a scoped goal.
    if lane is not None:
        if not isinstance(lane, str) or not LANE_ID.fullmatch(lane):
            raise ValueError('work_goal_lane_invalid')
        if not (_section(_without_fences(text), 'Goal') or '').strip():
            raise ValueError('work_goal_done_condition_missing')
    rank = metadata.get('rank', 1000)
    if type(rank) is not int or not 0 <= rank <= MAX_COUNT:
        raise ValueError('work_goal_rank_invalid')
    from .checkpoint_policy import bounded_prose
    goal = bounded_prose(_section(_without_fences(text), 'Goal'))
    summary_prose = bounded_prose(summary)
    from .summary_prose import field_line
    result = {'epic_id': identity, 'title': title, 'lane_id': lane, 'rank': rank,
              '_detail': {'summary_text': summary_prose['text'], 'summary_text_truncated': summary_prose['truncated'],
                          'summary_line': field_line('goal_summary', summary),
                          'goal_text': goal['text'], 'goal_text_truncated': goal['truncated']}}
    if 'target_date' in metadata:
        target = metadata['target_date']
        if not isinstance(target, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', target):
            raise ValueError('work_goal_date_invalid')
        date.fromisoformat(target)
        result['target_date'] = target
    if 'icon' in metadata:
        result['icon'] = validate_icon(metadata['icon'])
    if 'status' in metadata:
        raise ValueError('work_goal_authored_status')
    return result


def advance_goals(previous: dict, scan: dict, known_lanes: set[str]) -> dict:
    """Persist last-good charter facts in the existing work-index snapshot."""
    candidates = scan.get('goal_candidates', {})
    result = {}
    for identity in sorted(previous.keys() | candidates.keys()):
        prior = copy.deepcopy(previous.get(identity) or {})
        current = candidates.get(identity, [])
        quality, error, accepted = 'missing', 'work_goal_file_missing', None
        if not scan['available'] or not scan.get('goals_available', True):
            quality, error = 'stale', scan.get('goal_error') or scan.get('error') or 'work_goal_root_unavailable'
        elif len(current) > 1:
            quality, error = 'ambiguous', 'work_goal_id_ambiguous'
        elif current:
            item = current[0]
            quality, error = item['quality'], item.get('error')
            if quality == 'fresh':
                lane = item['facts']['lane_id']
                if lane is not None and lane not in known_lanes:
                    quality, error = 'error', 'work_goal_lane_unknown'
                else:
                    accepted = item
        elif prior.get('path') in scan.get('goal_errors', {}):
            item = scan['goal_errors'][prior['path']]
            quality, error = item['quality'], item.get('error')
        if accepted is not None:
            prior.update(facts=accepted['facts'], source_hash=accepted['source_hash'], path=accepted['path'])
        prior.update(quality=quality, error=error)
        result[identity] = prior
    return result


def goal_id(epic_id: str) -> str:
    return 'g_' + hashlib.sha256(epic_id.encode('utf-8')).hexdigest()[:24]


def wire_title(title: str) -> str:
    low, high = 0, len(title)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(title[:middle], ensure_ascii=False).encode('utf-8')) <= 120:
            low = middle
        else:
            high = middle - 1
    return title[:low]


MOVING = ('analysis', 'in_progress', 'needs_qa')
# Selection group per class: working and moving first, then blocked, then the rest.
CLASS_GROUP = {'w': 0, 'm': 0, 'b': 1, 'q': 2, 'd': 2}


def _status(member: dict):
    return member.get('_goal_status', member.get('status'))


def goal_progress(members: list[dict], *, certain: bool = True) -> dict:
    status = _status
    included = [member for member in members if status(member) != 'deprecated']
    total = len(included)
    done = sum(status(member) == 'completed' for member in included)
    if total > MAX_COUNT:
        raise ValueError('work_lane_goal_count_overflow')
    fresh = certain and all(member.get('observation', {}).get('quality') == 'fresh' for member in included)
    state = ('not_started' if not total else 'done' if fresh and done == total
             else 'not_started' if fresh and all(status(member) == 'backlog' for member in included)
             else 'in_progress')
    return {'state': state, 'done': done, 'total': total}


def goal_class(members: list[dict], working_ids: set[str]) -> str:
    """One of done/working/moving/blocked/queued as d/w/m/b/q, in that order."""
    counted = [member for member in members if _status(member) != 'deprecated']
    if counted and all(_status(member) == 'completed' for member in counted):
        return 'd'
    if any(_status(member) in MOVING for member in counted):
        return 'w' if any(member['spec_id'] in working_ids for member in counted) else 'm'
    return 'b' if any(_status(member) == 'blocked' for member in counted) else 'q'


def goal_detail(members: list[dict], working: bool) -> dict:
    """Detail-only goal facts over the goal's full membership."""
    counted = [member for member in members if _status(member) != 'deprecated']
    # The same open, non-exempt population aggregate_members uses for the lane.
    population = [member for member in members if member.get('status') not in
                  ('completed', 'deprecated', 'missing', 'ambiguous') and not member.get('estimate_exempt', False)]
    known = population and all(member.get('estimate') is not None
                               and member.get('observation', {}).get('quality') == 'fresh' for member in population)
    stamps = [member['_progress_at'] for member in members if member.get('_progress_at')]
    return {'moving': sum(_status(member) in MOVING for member in counted),
            'blocked': sum(_status(member) == 'blocked' for member in counted), 'working': working,
            'left_h': min(round(sum(member['estimate']['median'] for member in population), 1), 99999.9) if known else None,
            'progress_at': max(stamps) if stamps else None}


def project_goals(lane: dict, *, all_goals: bool = False) -> dict:
    goals, seen, classes = [], set(), {}
    members = lane.get('_members') or []
    working_ids = lane.get('_working_specs') or set()
    for identity, record in sorted((lane.get('_goals') or {}).items()):
        facts = record['facts']
        wire_id = goal_id(identity)
        if wire_id in seen:
            raise ValueError('work_goal_id_collision')
        seen.add(wire_id)
        owned = [member for member in members if member.get('goal_id') == wire_id]
        classes[wire_id] = goal_class(owned, working_ids)
        goals.append({'goal_id': wire_id, 'title': wire_title(facts['title']),
                      **goal_progress(owned, certain=record['quality'] == 'fresh' and lane.get('_work_index_available', False)),
                      'rank': facts['rank'],
                      **({'icon': facts['icon']} if 'icon' in facts else {}),
                      **({'target_date': facts['target_date']} if 'target_date' in facts else {}),
                      **(goal_detail(owned, classes[wire_id] == 'w') if all_goals else {})})
    order = lambda goal: (goal['rank'], goal['goal_id'])
    goals.sort(key=order)
    if len(goals) > MAX_COUNT:
        raise ValueError('work_lane_goal_count_overflow')
    selected = goals if all_goals else sorted(
        sorted(goals, key=lambda goal: (CLASS_GROUP[classes[goal['goal_id']]], *order(goal)))[:8], key=order)
    result = {'goals': selected, 'goals_total': len(goals)}
    legacy = [member for member in members if not member.get('goal_id') and _status(member) != 'deprecated']
    default = goal_class(legacy, working_ids) if legacy else None
    if legacy:
        # Detail facts read every unplaced member; goal_detail leaves deprecated ones out of its counts.
        unplaced = [member for member in members if not member.get('goal_id')]
        result['default_goal'] = {**goal_progress(legacy, certain=lane.get('_work_index_available', False)),
                                  **(goal_detail(unplaced, default == 'w') if all_goals else {})}
    letters = [classes[goal['goal_id']] for goal in goals] + ([default] if default else [])
    # Working goals count as moving.
    result['goal_classes'] = ''.join(classes[goal['goal_id']] for goal in selected) + (default or '-')
    result['goal_counts'] = [sum(letter in 'wm' for letter in letters), letters.count('b'),
                             letters.count('q'), letters.count('d')]
    if lane.get('_detail_revision'):
        result['detail_revision'] = lane['_detail_revision']
    return result


def detail_revision(members: list[tuple], goals: list[tuple], state: dict) -> str:
    payload = {'format': 1, 'members': sorted(members), 'goals': sorted(goals),
               'available': bool(state['available']), 'error': state.get('error')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()
