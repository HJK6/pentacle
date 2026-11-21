"""Full retained daemon inventory replay; opt in with the immutable receipt paths."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
from xml.etree import ElementTree as ET

import pytest
from admission_fingerprint import python_fingerprint

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT/'services/chat-stream-v2'
BASE = 'f3a76f479e14a390cd3d61234e1fd6e8a7d228d1'


def _text_nodes(path):
    return [line for line in path.read_text().splitlines() if line.startswith('tests/') and '.py::' in line]


def _xml_nodes(path, baseline):
    nodes = []
    for case in ET.parse(path).getroot().iter('testcase'):
        parts = case.get('classname', '').split('.')
        found = []
        for i in range(len(parts), 0, -1):
            file = '/'.join(parts[:i]) + '.py'
            if (baseline/file).is_file():
                found.append(file + '::' + '::'.join([*parts[i:], case.get('name', '')]))
        assert len(found) == 1, case.attrib
        nodes.extend(found)
    return nodes


def _depends_on_changed_file(node, changed):
    file = node.split('::', 1)[0]
    if file in changed:
        return True
    tree = ast.parse((SOURCE/file).read_text())
    for item in ast.walk(tree):
        if isinstance(item, ast.ImportFrom) and item.module:
            module = item.module.replace('.', '/')
            if 'tests/' + module + '.py' in changed or module + '.py' in changed:
                return True
    return False


@pytest.mark.skipif(not os.environ.get('PENTACLE_ROLLOUT_EVIDENCE'), reason='retained full rollout collection required')
def test_every_unchanged_daemon_case_matches_rollout_inventory():
    evidence = Path(os.environ['PENTACLE_ROLLOUT_EVIDENCE'])
    installed = Path(os.environ['PENTACLE_ROLLOUT_INSTALLED_EVIDENCE'])
    baseline = evidence/'source/services/chat-stream-v2'
    nodes = {
        'linux': _text_nodes(evidence/'amaterasu-unit-collection.txt') + _text_nodes(evidence/'amaterasu-smoke-collection.txt'),
        'darwin': _xml_nodes(installed/'v2-merge-repaired/gate/unit.junit.xml', baseline) + _xml_nodes(installed/'v2-merge-repaired/gate/smoke.junit.xml', baseline),
    }
    inventory = json.loads((ROOT/'scripts/test-admission-inventory.json').read_text())['python']['daemon']
    diff = subprocess.check_output(['git', '-C', str(ROOT), 'diff', '--name-only', BASE, 'HEAD', '--',
                                    'services/chat-stream-v2'], text=True).splitlines()
    changed = {name.removeprefix('services/chat-stream-v2/') for name in diff}
    cache = {}
    counts = {}
    for platform, entries in nodes.items():
        assert len(entries) > 10000
        mismatches = []
        for node in entries:
            key = hashlib.sha256(('daemon\0' + node).encode()).hexdigest()
            assert key in inventory[platform], (platform, node)
            if node not in cache:
                cache[node] = python_fingerprint(SOURCE, node)
            if cache[node] != inventory[platform][key]:
                mismatches.append(node)
        unexpected = [node for node in mismatches if not _depends_on_changed_file(node, changed)]
        assert not unexpected, (platform, unexpected)
        counts[platform] = (len(entries), len(mismatches))
    print('full daemon rollout replay:', counts)
