"""Dependency-neutral keyed live Tracking declarations."""
from __future__ import annotations

from datetime import date
import re

_HEADING = re.compile(r'^ {0,3}(#{1,6})(?:[ \t]+(.*))?[ \t]*$')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')


def parse_tracking_line(text: str, key: str, *, last_wins: bool = False):
    """Return date/authority/reason, None if absent; malformed grants raise.

    Only unfenced declarations within Tracking count. Duplicate declarations
    fail by default; history keys may explicitly select the last declaration.
    """
    key_pattern = re.compile(r'^\s*(?:[-*+]\s*)?' + re.escape(key) + r'\b')
    grammar = re.compile(r'- ' + re.escape(key) + r': ([0-9]{4}-[0-9]{2}-[0-9]{2}) (\S+) — (\S.*)')
    tracking_level = None
    fence = None
    lines = []
    for line in text.splitlines():
        if fence:
            if re.fullmatch(rf' {{0,3}}{fence[0]}{{{fence[1]},}}\s*', line):
                fence = None
            continue
        opener = _FENCE.match(line)
        if opener and not (opener[1][0] == '`' and '`' in line[opener.end():]):
            fence = (opener[1][0], len(opener[1]))
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading[1])
            if tracking_level is not None and level <= tracking_level:
                tracking_level = None
            # Empty ATX headings also end a section. Closing hashes count
            # only after whitespace; Tracking# is a different title.
            title = re.sub(r'[ \t]+#+[ \t]*$', '', heading[2] or '').strip(' \t')
            if title == 'Tracking':
                tracking_level = level
            continue
        if tracking_level is not None and key_pattern.match(line):
            lines.append(line)
    if not lines:
        return None
    match = grammar.fullmatch(lines[-1]) if last_wins or len(lines) == 1 else None
    try:
        if match is None or not match[3].strip():
            raise ValueError()
        date.fromisoformat(match[1])
    except ValueError:
        raise ValueError(key + '_malformed') from None
    return {'date': match[1], 'authority': match[2], 'reason': match[3].strip()}

