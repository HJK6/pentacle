#!/usr/bin/env python3
"""Batch admission decisions for the JavaScript file selector."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from admission_fingerprint import javascript_fingerprint
from admission_inputs import load_inputs
from admission_policy import admission


def query(root, files, platform):
    if platform not in ('linux', 'darwin') or not isinstance(files, list):
        raise ValueError('unsupported admission platform or selection')
    legacy, records, quarantine = load_inputs(root)
    result = {}
    for file in files:
        if not isinstance(file, str):
            raise ValueError('invalid test file identity')
        try:
            fingerprint = javascript_fingerprint(root, file)
            state, reason = admission('javascript', file, fingerprint, platform, legacy, records, quarantine)
        except (OSError, ValueError, SyntaxError):
            state, reason = 'advisory', 'test or fixture fingerprint unavailable'
        result[file] = {'state': state, 'reason': reason}
    return {'complete': True, 'files': result, 'quarantine': quarantine}


def verify_junit(path, expected):
    if not isinstance(expected, list) or any(not isinstance(name, str) or not name for name in expected):
        raise ValueError('invalid advisory identities')
    root = ET.parse(path).getroot()
    found = set()
    def walk(element, parents):
        if element.tag == 'testsuite' and element.get('name'):
            parents = [*parents, element.get('name')]
        if element.tag == 'testcase' and element.find('skipped') is None:
            found.add(' '.join([*parents, element.get('name', '')]))
        for child in element:
            if child.tag in ('testsuites', 'testsuite', 'testcase'):
                walk(child, parents)
    walk(root, [])
    return {'complete': True, 'executed': all(name in found for name in expected),
            'absent': all(name not in found for name in expected), 'observed': sorted(found)}


def main():
    try:
        request = json.load(sys.stdin)
        if request.get('operation') == 'verify_junit':
            result = verify_junit(request['path'], request['expected'])
        else:
            result = query(Path(request['root']), request['files'], request['platform'])
    except (OSError, ValueError, TypeError, KeyError, ET.ParseError) as exc:
        result = {'complete': False, 'reason': type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
