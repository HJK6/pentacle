"""Read-only custom acceptance and resolver-backed lane completion checks."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .specs_parser import load_frontmatter
from .specs_service import SOURCE_READ_ERRORS, SpecsSubsystem, _resolve_specs_memory_root

NONTERMINAL = frozenset({'backlog', 'analysis', 'ready_for_dev', 'in_progress', 'needs_qa', 'blocked'})
TRACKED_CLASSES = frozenset({'external_event', 'operator_action', 'new_scope'})
_CHECKBOX = re.compile(r'^\s*(?:(?:[-*+]|\d+[.)])\s*)?\[[^\]]*\]')
_NAMED = re.compile(r'^\s*- \[([ xX])\]\s+\*\*([^*]+)\*\*\s+—\s+(\S.*)$')
_WAIVER = re.compile(r'\(waived:\s*(.*?)\)\s*$')
_TRACKED = re.compile(r'\btracked:\s+(\S+)\s+\(([^()]+)\)\s*$')


class CompletionRefusal(ValueError):
    """Stable wire class with deterministic item details, separate from text."""
    def __init__(self, code: str, items: list[dict[str, Any]]) -> None:
        self.code = code
        self.details = {'items': items, 'retriable': code == 'member_spec_unreadable'}
        super().__init__(code + ': ' + '; '.join(
            f"{item['spec_id']} / {item.get('criterion') or 'spec'}: {item['reason']}" for item in items))

    def audit_reason(self) -> str:
        """Bounded existing-table text, retaining the class and item reasons."""
        return (self.code + ': ' + json.dumps(self.details['items'], ensure_ascii=False, sort_keys=True))[:2000]


def section_lines(text: str, heading: str):
    """Yield numbered lines within an exact level-two Markdown section."""
    inside = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith('## '):
            if inside:
                return
            inside = line.strip() == '## ' + heading
            continue
        if inside:
            yield number, line


class SpecReader:
    """One fresh resolver index per check; no observer, cached catalog or writes."""
    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root is not None else _resolve_specs_memory_root()[0]
        if self.root is None or not (self.root / 'work').is_dir():
            raise ValueError('spec_root_unavailable')
        self.service = SpecsSubsystem(memory_root=self.root, session_summaries=lambda: [], changed_callback=lambda _: None)
        self.index = self.service._scan_folders_by_id(strict=True)

    def resolve(self, spec_id: str) -> tuple[str, Path, str]:
        matches = self.service._matches_from_index(self.index, spec_id)
        if len(matches) != 1:
            raise ValueError('spec_unresolved')
        status, folder = matches[0]
        identity = str(self.index.identities[folder][0])
        return status, folder, identity

    def read(self, spec_id: str) -> tuple[str, Path, str, str]:
        status, folder, identity = self.resolve(spec_id)
        path = folder / 'spec.md'
        before = self.service._identity_fingerprint(path)
        if before is None:
            raise ValueError('spec_unreadable')
        text = path.read_text(encoding='utf-8')
        if self.service._identity_fingerprint(path) != before:
            raise ValueError('spec_changed')
        return status, folder, identity, text


def parse_custom(text: str) -> list[dict[str, Any]]:
    """Parse Custom checkbox syntax without doing any filesystem work."""
    custom = False
    items = []
    for number, line in section_lines(text, 'Acceptance Criteria'):
        if line.strip() == '### Custom':
            custom = True
            continue
        if not custom or not _CHECKBOX.match(line):
            continue
        named = _NAMED.fullmatch(line)
        if named is None or not named[2].strip():
            items.append({'line': number, 'criterion': None, 'reason': 'custom_ac_unparseable'})
            continue
        mark, criterion, body = named.groups()
        item = {'line': number, 'criterion': criterion.strip(), 'reason': 'undisposed'}
        if mark.lower() == 'x':
            item['disposition'] = 'checked'
        elif waiver := _WAIVER.search(body):
            if waiver[1].strip():
                item.update(disposition='waived', reason=waiver[1].strip())
            else:
                item['reason'] = 'custom_ac_unparseable'
        elif tracked := _TRACKED.search(body):
            item.update(disposition='tracked', target=tracked[1], **{'class': tracked[2].strip()})
        elif 'tracked:' in body or '(waived:' in body:
            item['reason'] = 'custom_ac_unparseable'
        items.append(item)
    return items


def _tracked(reader: SpecReader, member: str, folder: Path, item: dict[str, Any]) -> dict[str, Any]:
    if item['class'] not in TRACKED_CLASSES:
        return {'reason': 'class_invalid'}
    try:
        status, target, identity, text = reader.read(item['target'])
    except SOURCE_READ_ERRORS:
        return {'reason': 'follow_up_not_found'}
    if target == folder:
        return {'reason': 'follow_up_is_self'}
    if status not in NONTERMINAL:
        return {'reason': 'follow_up_terminal'}
    try:
        owner = load_frontmatter(text).get('owner')
    except SOURCE_READ_ERRORS:
        owner = None
    if not isinstance(owner, str) or not owner.strip():
        return {'reason': 'follow_up_unowned'}
    receipt = re.compile(r'^- deferred_from: ' + re.escape(member) + r' — '
                         + re.escape(item['criterion']) + r':\s*(\S.*)$')
    for _, line in section_lines(text, 'Tracking'):
        match = receipt.fullmatch(line.strip())
        if match and match[1].strip():
            return {'target': identity, 'owner': owner.strip(), 'reason': match[1].strip()}
    return {'reason': 'follow_up_unlinked'}


def check_lane_members(root: str | Path | None, member_ids) -> dict[str, Any]:
    """Allow only readable members whose custom criteria all have dispositions."""
    members = sorted(set(member_ids))
    if not members:
        return {'dispositions': []}
    try:
        reader = SpecReader(root)
    except SOURCE_READ_ERRORS:
        raise CompletionRefusal('member_spec_unreadable', [
            {'spec_id': member, 'criterion': None, 'reason': 'member_spec_unreadable'} for member in members]) from None
    failures, dispositions = [], []
    for member in members:
        try:
            _, folder, identity, text = reader.read(member)
        except SOURCE_READ_ERRORS:
            failures.append({'spec_id': member, 'criterion': None, 'reason': 'member_spec_unreadable'})
            continue
        for parsed in parse_custom(text):
            item = {'spec_id': member, **parsed}
            disposition = item.get('disposition')
            if disposition == 'tracked':
                item.update(_tracked(reader, identity, folder, item))
                if item.get('owner'):
                    dispositions.append(item)
                else:
                    failures.append(item)
            elif disposition in {'checked', 'waived'}:
                dispositions.append(item)
            else:
                failures.append(item)
    failures.sort(key=lambda item: (item['spec_id'], item.get('line', 0), item.get('criterion') or ''))
    if failures:
        reasons = {item['reason'] for item in failures}
        code = ('member_spec_unreadable' if 'member_spec_unreadable' in reasons else
                'custom_ac_unparseable' if 'custom_ac_unparseable' in reasons else 'lane_residuals_undisposed')
        raise CompletionRefusal(code, failures)
    return {'dispositions': dispositions}
