"""Read-only candidate-inclusive additional-work cap and public formatter."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
from typing import Any

from .custom_ac import SpecReader
from .tracking_lines import parse_tracking_line
from .specs_service import SOURCE_READ_ERRORS, _resolve_specs_memory_root

INSTRUCTION = "Add to this spec's Tracking section: - wip_override: YYYY-MM-DD <authority-ref> — <reason>"
_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def _date(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def load_policy(root: Path | None) -> dict[str, Any] | None:
    """Absent is disabled. Every present invalid/unreadable policy fails closed."""
    if root is None:
        return None
    try:
        text = (root / 'work' / 'wip_policy.json').read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    try:
        policy = json.loads(text)
        valid = (isinstance(policy, dict) and type(policy.get('schema_version')) is int
                 and policy['schema_version'] == 1 and type(policy.get('cap')) is int
                 and policy['cap'] > 0 and _date(policy.get('snapshot_at'))
                 and isinstance(policy.get('grandfather'), dict)
                 and all(isinstance(repo, str) and isinstance(stems, list)
                         and all(isinstance(stem, str) for stem in stems)
                         for repo, stems in policy['grandfather'].items()))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('wip_policy_malformed')
    return policy


def parse_override(text):
    """Return exemption; reject malformed/duplicate live Tracking declarations.

    Examples in fenced blocks and declarations outside Tracking are not grants.
    Nested Tracking subsections stay in scope until the next peer/parent heading.
    """
    return parse_tracking_line(text, 'wip_override') is not None


def format_cap_refusal(repo: str, effective: int, cap: int, grandfather: int, overrides: int) -> str:
    return f'wip_cap_exceeded: repo={repo} E_R={effective} cap={cap} G_R={grandfather} O_R={overrides}\n{INSTRUCTION}'


def check_lead_spawn(root: str | Path | None, spec_id: str) -> dict[str, Any]:
    """Evaluate one resolved primary candidate without reserving or changing it."""
    root = Path(root) if root is not None else _resolve_specs_memory_root()[0]
    result = {'E_R': None, 'error_code': None, 'message': None, 'warnings': []}
    try:
        policy = load_policy(root)
    except SOURCE_READ_ERRORS:
        return dict(result, error_code='wip_policy_malformed', message='wip_policy_malformed')
    if policy is None:
        return result
    try:
        reader = SpecReader(root)
        _, candidate_folder, identity, candidate_text = reader.read(spec_id)
    except SOURCE_READ_ERRORS:
        # The existing spawn resolver usually catches this first; a fresh read
        # can still race an edit. Never authorize an unreadable candidate.
        return dict(result, error_code='wip_override_malformed', message=f'wip_override_malformed: spec={spec_id}')
    stem = candidate_folder.name
    repo = stem.split('__', 1)[0]
    active = {folder.name: (folder, canonical) for folder, (canonical, _) in reader.index.identities.items()
              if folder.parent.name == 'in_progress' and folder.name.split('__', 1)[0] == repo}
    active[stem] = (candidate_folder, identity)
    listed = set(policy['grandfather'].get(repo, []))
    grandfather = listed & active.keys()
    warnings = ['grandfather_stale'] if listed - active.keys() else []
    overrides = set()
    for item_stem, (folder, canonical) in sorted(active.items()):
        try:
            text = candidate_text if item_stem == stem else reader.read(canonical)[3]
            if parse_override(text):
                overrides.add(item_stem)
        except SOURCE_READ_ERRORS:
            return dict(result, error_code='wip_override_malformed',
                        message=f'wip_override_malformed: spec={canonical}', warnings=warnings)
    effective = len(active.keys() - (grandfather | overrides))
    exempt = stem in grandfather | overrides
    result.update(E_R=effective, repo=repo, cap=policy['cap'], G_R=len(grandfather), O_R=len(overrides),
                  candidate_exempt=exempt, warnings=warnings)
    if not exempt and effective > policy['cap']:
        result.update(error_code='wip_cap_exceeded',
                      message=format_cap_refusal(repo, effective, policy['cap'], len(grandfather), len(overrides)))
    return result
