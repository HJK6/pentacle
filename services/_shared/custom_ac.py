"""Read-only custom acceptance and resolver-backed lane completion checks."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from collections import Counter
from pathlib import Path
import re
from typing import Any

from .specs_parser import load_frontmatter
from .specs_service import SOURCE_READ_ERRORS, SpecsSubsystem, _resolve_specs_memory_root, _bounded_directory

NONTERMINAL = frozenset({'backlog', 'analysis', 'ready_for_dev', 'in_progress', 'needs_qa', 'blocked'})
TRACKED_CLASSES = frozenset({'external_event', 'operator_action', 'new_scope'})
_HEADING = re.compile(r'^ {0,3}(#{1,6})(?:[ \t]+(.*))?[ \t]*$')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
_CHECKBOX = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+\[([ xX])\](?:\s+(.*))?$')
_NAME = re.compile(r'^\*\*([^*]+)\*\*')
_MARKER = re.compile(r'\(waived:|\btracked:')
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


def _heading(line: str):
    match = _HEADING.fullmatch(line)
    if match:
        title = re.sub(r'[ \t]+#+[ \t]*$', '', match[2] or '').strip(' \t')
        return len(match[1]), title
    return None


def _visible_lines(text: str):
    """Keep original line numbers, excluding Markdown fenced examples."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        if fence:
            if re.fullmatch(rf' {{0,3}}{fence[0]}{{{fence[1]},}}\s*', line):
                fence = None
            continue
        opener = _FENCE.match(line)
        if opener and not (opener[1][0] == '`' and '`' in line[opener.end():]):
            fence = opener[1][0], len(opener[1])
            continue
        yield number, line


def section_lines(text: str, heading: str):
    """Yield live text in a level-two section, ending at its peer or parent."""
    inside = False
    for number, line in _visible_lines(text):
        parsed = _heading(line)
        if parsed and parsed[0] <= 2:
            if inside:
                return
            inside = parsed == (2, heading)
            continue
        if inside:
            yield number, line


def _escaped(text: str, index: int) -> bool:
    count = 0
    while index > 0 and text[index - 1] == "\\":
        count += 1
        index -= 1
    return count % 2 == 1


def _example_mask(text: str) -> str:
    """Mask matched inline-code and quoted examples without shifting offsets."""
    masked = list(text)
    index = 0
    while index < len(text):
        if text[index] != '`' or _escaped(text, index):
            index += 1
            continue
        end = index
        while end < len(text) and text[end] == '`':
            end += 1
        run = re.compile(r'(?<!`)`{' + str(end - index) + r'}(?!`)')
        closing = next((m for m in run.finditer(text, end) if not _escaped(text, m.start())), None)
        if closing:
            masked[index:closing.end()] = ' ' * (closing.end() - index)
            index = closing.end()
        else:
            index = end
    code_masked = ''.join(masked)
    pairs = {"'": "'", '"': '"', '‘': '’', '“': '”'}
    word = lambda char: char.isalnum() or char == '_'
    index = 0
    while index < len(text):
        char = code_masked[index]
        if (char not in pairs or _escaped(text, index)
                or (index > 0 and word(code_masked[index - 1]))):
            index += 1
            continue
        closing = index + 1
        while closing < len(text):
            if (code_masked[closing] == pairs[char] and not _escaped(text, closing)
                    and (closing + 1 == len(text) or not word(code_masked[closing + 1]))):
                break
            closing += 1
        if closing < len(text):
            masked[index:closing + 1] = ' ' * (closing + 1 - index)
            index = closing + 1
        else:
            index += 1
    return ''.join(masked)


def _normalize(text: str) -> str:
    return ' '.join(text.split())


class SpecReader:
    """One fresh resolver index per check; no observer, cached catalog or writes."""
    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root is not None else _resolve_specs_memory_root()[0]
        if self.root is None or not (self.root / 'work').is_dir():
            raise ValueError('spec_root_unavailable')
        self.service = SpecsSubsystem(memory_root=self.root, session_summaries=lambda: [], changed_callback=lambda _: None)
        # Capture directory metadata around exactly one fresh identity scan.
        # Identity-only files are deliberately not consumed-input commit fences.
        status_dirs = self.service._work_dirs()
        self.directories = {path: _directory_fingerprint(path)
                            for path in (self.root, self.root / 'work', *status_dirs)}
        self.index = self.service._scan_folders_by_id(status_dirs=status_dirs, strict=True)
        self.consumed: dict[Path, ConsumedSpec] = {}

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
        scanned = self.service._identity_cache.get(path)
        if before is None or scanned is None or scanned[0] != before:
            raise ValueError('spec_changed')
        directory_before = _directory_fingerprint(folder)
        # Neither the file nor any ancestor may redirect the consumed read.
        with _bounded_directory(folder) as directory:
            fd = os.open('spec.md', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, 'rb') as source:
                value = os.fstat(source.fileno())
                opened = (str(path), value.st_dev, value.st_ino, value.st_size,
                          value.st_mtime_ns, value.st_ctime_ns)
                if not stat.S_ISREG(value.st_mode) or opened != before:
                    raise ValueError('spec_changed')
                content = source.read()
        if (self.service._identity_fingerprint(path) != before
                or _directory_fingerprint(folder) != directory_before):
            raise ValueError('spec_changed')
        text = content.decode('utf-8')
        previous = self.consumed.get(path)
        current = ConsumedSpec(path, before, content)
        if previous is not None and previous != current:
            raise ValueError('spec_changed')
        self.consumed[path] = current
        self.directories[folder] = directory_before
        return status, folder, identity, text


def _directory_fingerprint(path: Path):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError('spec_directory_changed')
    return value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns


@dataclass(frozen=True)
class ConsumedSpec:
    path: Path
    fingerprint: tuple
    content: bytes


@dataclass(frozen=True)
class PreparedCompletion:
    """Observed inputs, not an atomic snapshot or a namespace-wide commit fence."""
    members: tuple[str, ...]
    files: tuple[ConsumedSpec, ...] = ()
    directories: tuple[tuple[Path, tuple | None], ...] = ()
    dispositions: tuple[dict[str, Any], ...] = ()
    refusal: CompletionRefusal | None = None

    def revalidate(self) -> None:
        # Metadata only. Never resolve, parse, enumerate or read content here.
        if self.refusal is not None:
            raise self.refusal
        changed = []
        for path, before in self.directories:
            try:
                if _directory_fingerprint(path) != before:
                    changed.append(path)
            except SOURCE_READ_ERRORS:
                changed.append(path)
        for item in self.files:
            try:
                if SpecsSubsystem._identity_fingerprint(item.path) != item.fingerprint:
                    changed.append(item.path)
            except SOURCE_READ_ERRORS:
                changed.append(item.path)
        if changed:
            refusal = _unreadable(self.members)
            refusal.source_paths = tuple(changed)
            raise refusal


def _unreadable(members) -> CompletionRefusal:
    return CompletionRefusal('member_spec_unreadable', [
        {'spec_id': member, 'criterion': None, 'reason': 'member_spec_unreadable'} for member in members])


def prepare_lane_completion(root: str | Path | None, member_ids) -> PreparedCompletion:
    """One off-thread fresh preparation; defer refusal to the old guard position."""
    members = tuple(sorted(set(member_ids)))
    if not members:
        return PreparedCompletion(members)
    reader = None
    dispositions, refusal = (), None
    try:
        reader = SpecReader(root)
        dispositions = tuple(_check_reader_members(reader, members)['dispositions'])
    except CompletionRefusal as exc:
        refusal = exc
    except SOURCE_READ_ERRORS:
        refusal = _unreadable(members)
    prepared = PreparedCompletion(
        members, tuple(reader.consumed.values()) if reader else (),
        tuple(reader.directories.items()) if reader else (), dispositions, refusal)
    # Detect changes during consumption without a second namespace scan.
    if refusal is None:
        try:
            prepared.revalidate()
        except CompletionRefusal as exc:
            prepared = PreparedCompletion(members, prepared.files, prepared.directories, dispositions, exc)
    return prepared


def parse_custom(text: str) -> list[dict[str, Any]]:
    """Use checkbox state and explicit dispositions, never display typography."""
    custom = False
    items = []
    for number, line in section_lines(text, 'Acceptance Criteria'):
        heading = _heading(line)
        if heading and heading[0] <= 3:
            if custom:
                break
            custom = heading == (3, 'Custom')
            continue
        checkbox = _CHECKBOX.fullmatch(line) if custom else None
        if checkbox is None:
            continue
        mark, body = checkbox[1], (checkbox[2] or '').strip()
        markers = list(_MARKER.finditer(_example_mask(body)))
        label = body[:markers[0].start()].strip() if markers else body
        named = _NAME.match(label)
        criterion = _normalize(named[1] if named else label)
        item = {'line': number, 'criterion': criterion, 'reason': 'undisposed'}
        if mark.lower() == 'x':
            item['disposition'] = 'checked'
        elif markers:
            suffix = body[markers[0].start():]
            if len(markers) != 1 or not criterion:
                item['reason'] = 'disposition_invalid'
            elif (waiver := _WAIVER.fullmatch(suffix)) and waiver[1].strip():
                item.update(disposition='waived', reason=waiver[1].strip())
            elif tracked := _TRACKED.fullmatch(suffix):
                item.update(disposition='tracked', target=tracked[1], **{'class': tracked[2].strip()})
            else:
                item['reason'] = 'disposition_invalid'
        items.append(item)
    return items


def _tracked(reader: SpecReader, member: str, folder: Path, item: dict[str, Any], identities: Counter) -> dict[str, Any]:
    if identities[item['criterion']] > 1:
        return {'reason': 'criterion_ambiguous'}
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
    prefix = f'- deferred_from: {member} — '
    ambiguous = False
    for _, line in section_lines(text, 'Tracking'):
        line = line.strip()
        if not line.startswith(prefix):
            continue
        tail = _normalize(line[len(prefix):])
        matches = [identity for identity in identities if identity
                   and tail.startswith(identity + ': ') and tail[len(identity) + 2:].strip()]
        if len(matches) > 1:
            ambiguous = True
        elif matches == [item['criterion']]:
            return {'target': identity, 'owner': owner.strip(),
                    'reason': tail[len(item['criterion']) + 2:].strip()}
    return dict(reason='follow_up_unlinked', **({'detail': 'receipt_ambiguous'} if ambiguous else {}))


def check_lane_members(root: str | Path | None, member_ids) -> dict[str, Any]:
    """Standalone fresh evaluator, also used by spawn-independent parser callers."""
    prepared = prepare_lane_completion(root, member_ids)
    if prepared.refusal is not None:
        raise prepared.refusal
    return {'dispositions': list(prepared.dispositions)}


def _check_reader_members(reader: SpecReader, members) -> dict[str, Any]:
    failures, dispositions = [], []
    for member in members:
        try:
            _, folder, identity, text = reader.read(member)
        except SOURCE_READ_ERRORS:
            failures.append({'spec_id': member, 'criterion': None, 'reason': 'member_spec_unreadable'})
            continue
        parsed_items = parse_custom(text)
        identities = Counter(item['criterion'] for item in parsed_items)
        for parsed in parsed_items:
            item = {'spec_id': member, **parsed}
            disposition = item.get('disposition')
            if disposition == 'tracked':
                item.update(_tracked(reader, identity, folder, item, identities))
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
        code = 'member_spec_unreadable' if 'member_spec_unreadable' in reasons else 'lane_residuals_undisposed'
        raise CompletionRefusal(code, failures)
    return {'dispositions': dispositions}
