#!/usr/bin/env python3
"""Check relay quarantine additions against owned decision records.

Exit 0 accepts, 1 refuses a missing or invalid decision, and 2 indicates
unavailable Git or private decision evidence. Never performs public writes.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess

from admission_inputs import _history

PATH = 'scripts/quarantine.json'
ITEM = re.compile(r'^[A-Za-z0-9_-]+$')
OCCURRENCE = re.compile(r'^public-checks/([0-9]+)/1$')

class Refusal(Exception):
    pass

class Unavailable(Exception):
    pass


def _git(root, *args):
    try:
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unavailable('Git evidence unavailable') from exc


def _list(root, ref):
    names = _git(root, 'ls-tree', '-r', '--name-only', ref, '--', PATH).decode().splitlines()
    if PATH not in names:
        return set()
    try:
        data = json.loads(_git(root, 'show', ref + ':' + PATH))
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError('bad version')
        return set(_history(data)[0])
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise Refusal('invalid quarantine list') from exc


def verify_additions(added, register, work_root):
    if not isinstance(register, dict) or not isinstance(register.get('records'), list):
        raise Unavailable('decision register unavailable')
    root = Path(work_root)
    if not root.is_dir():
        raise Unavailable('work records unavailable')
    for test in sorted(added):
        rows = [r for r in register['records'] if isinstance(r, dict) and r.get('test') == test]
        if len(rows) != 1 or not ITEM.fullmatch(str(rows[0].get('item', ''))):
            raise Refusal('matching decision missing or ambiguous')
        row = rows[0]
        occurrences = row.get('occurrences')
        if (not isinstance(occurrences, list)
                or any(not isinstance(x, str) or not OCCURRENCE.fullmatch(x) for x in occurrences)
                or len(set(occurrences)) < 2):
            raise Refusal('decision needs two distinct first-attempt occurrence links')
        matches = list(root.glob('*/' + row['item'] + '/summary.md'))
        if len(matches) != 1:
            raise Unavailable('decision lifecycle evidence unavailable')
        summary = matches[0]
        if summary.parent.parent.name in ('completed', 'deprecated'):
            raise Refusal('decision is not open')
        text = summary.read_text()
        if (not re.search(r'(?m)^owner:\s*\S', text)
                or not re.search(r'(?m)^id:\s*work_' + re.escape(row['item']) + r'\s*$', text)
                or not re.search(r'(?m)^status:\s*' + re.escape(summary.parent.parent.name) + r'\s*$', text)
                or not (summary.parent/'spec.md').is_file()):
            raise Unavailable('decision owner or spec unavailable')
        spec = (summary.parent/'spec.md').read_text()
        links = {'https://github.com/HJK6/pentacle/actions/runs/' + OCCURRENCE.fullmatch(x).group(1)
                 for x in occurrences}
        if test not in spec or sum(bool(re.search(re.escape(link) + r'(?=/|[)"\s]|$)', spec)) for link in links) < 2:
            raise Refusal('decision spec lacks the exact test and two occurrence links')


def check(root, base, tip, register, work_root):
    if not all(re.fullmatch(r'[a-f0-9]{40}', x or '') for x in (base, tip)):
        raise Refusal('full Git base and tip required')
    try:
        p = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', base, tip],
                           capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unavailable('Git ancestry unavailable') from exc
    if p.returncode == 1:
        raise Refusal('base is not an ancestor of tip')
    if p.returncode:
        raise Unavailable('Git ancestry unavailable')
    added = _list(root, tip) - _list(root, base)
    if added:
        try:
            records = json.loads(Path(register).read_text())
        except (OSError, ValueError, TypeError) as exc:
            raise Unavailable('decision register unavailable') from exc
        verify_additions(added, records, work_root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--tip', required=True)
    parser.add_argument('--register', required=True)
    parser.add_argument('--work-root', required=True)
    args = parser.parse_args(argv)
    try:
        check(args.root, args.base, args.tip, args.register, args.work_root)
    except Refusal as exc:
        print(json.dumps({'status': 'refuse', 'reason': str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({'status': 'error', 'reason': type(exc).__name__}))
        return 2
    print(json.dumps({'status': 'accept', 'base': args.base, 'tip': args.tip}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
