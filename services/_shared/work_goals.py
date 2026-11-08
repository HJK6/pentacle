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
    result = {'epic_id': identity, 'title': title, 'lane_id': lane, 'rank': rank}
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


def goal_progress(members: list[dict], *, certain: bool = True) -> dict:
    status = lambda member: member.get('_goal_status', member.get('status'))
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


def project_goals(lane: dict, *, all_goals: bool = False) -> dict:
    goals, seen = [], set()
    members = lane.get('_members') or []
    for identity, record in sorted((lane.get('_goals') or {}).items()):
        facts = record['facts']
        wire_id = goal_id(identity)
        if wire_id in seen:
            raise ValueError('work_goal_id_collision')
        seen.add(wire_id)
        goals.append({'goal_id': wire_id, 'title': wire_title(facts['title']),
                      **goal_progress([member for member in members if member.get('goal_id') == wire_id],
                                      certain=record['quality'] == 'fresh' and lane.get('_work_index_available', False)),
                      'rank': facts['rank'],
                      **({'icon': facts['icon']} if 'icon' in facts else {}),
                      **({'target_date': facts['target_date']} if 'target_date' in facts else {})})
    order = lambda goal: (goal['rank'], goal['goal_id'])
    goals.sort(key=order)
    if len(goals) > MAX_COUNT:
        raise ValueError('work_lane_goal_count_overflow')
    selected = goals if all_goals else sorted(
        sorted(goals, key=lambda goal: (goal['state'] != 'in_progress', *order(goal)))[:8], key=order)
    result = {'goals': selected, 'goals_total': len(goals)}
    legacy = [member for member in members if not member.get('goal_id') and member.get('_goal_status', member.get('status')) != 'deprecated']
    if legacy:
        result['default_goal'] = goal_progress(legacy, certain=lane.get('_work_index_available', False))
    if lane.get('_detail_revision'):
        result['detail_revision'] = lane['_detail_revision']
    return result


def detail_revision(members: list[tuple], goals: list[tuple], state: dict) -> str:
    payload = {'format': 1, 'members': sorted(members), 'goals': sorted(goals),
               'available': bool(state['available']), 'error': state.get('error')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()
