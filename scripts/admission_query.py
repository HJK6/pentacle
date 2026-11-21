#!/usr/bin/env python3
"""Batch admission decisions for the JavaScript file selector."""
from __future__ import annotations

import json
from collections import Counter
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


def reconcile_junit(path, expected, empty_wrapper=None):
    if not isinstance(expected, list) or any(
            not isinstance(case, list) or not case or any(not isinstance(name, str) for name in case)
            for case in expected):
        raise ValueError('invalid discovered identities')
    root = ET.parse(path).getroot()
    if root.tag != 'testsuites':
        raise ValueError('invalid Node JUnit root')
    # Node reports the file itself when its filter removes every test.
    if not expected and empty_wrapper and len(root) == 1:
        wrapper = root[0]
        if wrapper.tag == 'testcase' and wrapper.get('name') == empty_wrapper and not len(wrapper):
            return {'complete': True, 'matches': True}
    observed = Counter()

    def walk(element, parents):
        for child in element:
            if child.tag in ('testcase', 'testsuite'):
                names = (*parents, child.attrib['name'])
                if child.find('skipped') is not None:
                    raise ValueError('unexpected skipped Node case')
                observed[names] += 1
                if child.tag == 'testsuite':
                    walk(child, names)

    walk(root, ())
    return {'complete': True, 'matches': observed == Counter(map(tuple, expected))}


def main():
    try:
        request = json.load(sys.stdin)
        if request.get('operation') == 'verify_junit':
            result = verify_junit(request['path'], request['expected'])
        elif request.get('operation') == 'reconcile_junit':
            result = reconcile_junit(request['path'], request['expected'], request.get('empty_wrapper'))
        else:
            result = query(Path(request['root']), request['files'], request['platform'])
    except (OSError, ValueError, TypeError, KeyError, ET.ParseError) as exc:
        result = {'complete': False, 'reason': type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
