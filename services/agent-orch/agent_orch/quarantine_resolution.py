"""Verify reviewed, installed and hosted evidence before a new flake series."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from . import reliability_hosted
from .reliability_receipts import instant
from .reliability_selection import effective_tests

REPOSITORY = 'HJK6/pentacle'


def _read(path, digest, limit=16_000_000):
    path = Path(path)
    if not path.is_absolute() or not re.fullmatch(r'[a-f0-9]{64}', str(digest)):
        raise ValueError('invalid evidence reference')
    raw = path.read_bytes()
    if len(raw) > limit or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('evidence digest mismatch')
    return raw


def _suite(test):
    for suite in ('daemon', 'cli', 'shared'):
        prefix = suite + '::'
        if test.startswith(prefix):
            return suite, test[len(prefix):]
    if test.startswith('tests/'):
        return 'daemon', test
    if test.startswith('test/'):
        return 'javascript', test
    raise ValueError('unsupported test identity')


def _passed(raw, suite, test):
    root = ET.fromstring(raw)
    matches = []
    if suite == 'javascript':
        _, name = test.split('::', 1)
        try:
            components = json.loads(name) if name.startswith('[') else [name]
        except ValueError:
            return False
        if not isinstance(components, list) or not components or any(not isinstance(c, str) or not c for c in components):
            return False
        def walk(node, parents):
            if node.tag == 'testsuite' and node.get('name'):
                parents = [*parents, node.get('name')]
            if node.tag == 'testcase' and [*parents, node.get('name', '')] == components:
                matches.append(node)
            for child in node:
                if child.tag in ('testsuites', 'testsuite', 'testcase'):
                    walk(child, parents)
        walk(root, [])
    else:
        head, bracket, parameter = test.partition('[')
        file, *parts = head.split('::')
        if not file.endswith('.py') or not parts:
            return False
        if bracket:
            parts[-1] += bracket + parameter
        module = file[:-3].replace('/', '.') + ('.' + '.'.join(parts[:-1]) if len(parts) > 1 else '')
        matches = [case for case in root.iter('testcase')
                   if case.get('classname') == module and case.get('name') == parts[-1]]
    return len(matches) == 1 and all(matches[0].find(tag) is None for tag in ('failure', 'error', 'skipped'))


def _report(report_id, command='agent-orch'):
    proc = subprocess.run([command, 'report', 'get', report_id, '--json'], capture_output=True,
                          text=True, timeout=25, check=False)
    if proc.returncode:
        raise ValueError('QA report unavailable')
    return json.loads(proc.stdout)


def _main_contains(checkout, target, main):
    proc = subprocess.run(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', target, main],
                          capture_output=True, timeout=20, check=False)
    return proc.returncode == 0


def _hosted_junit(repository, artifact, member):
    raw = reliability_hosted.download(repository, f'actions/artifacts/{int(artifact)}/zip')
    with ZipFile(BytesIO(raw)) as archive:
        files = [item for item in archive.infolist() if item.filename == member]
        if len(files) != 1 or files[0].file_size > 12_000_000:
            raise ValueError('hosted JUnit unavailable')
        return archive.read(files[0])


def verify_restoration(test, descriptor, checkout, *, get_report=_report, api=reliability_hosted.api,
                       hosted_junit=_hosted_junit, main_contains=_main_contains, selected=None):
    try:
        manifest_raw = _read(descriptor['manifest'], descriptor['sha256'], 1_000_000)
        manifest = json.loads(manifest_raw)
        suite, node = _suite(test)
        sha = manifest['target_sha']
        if (manifest.get('version') != 1 or manifest.get('disposition') != 'repair'
                or manifest.get('test') != test or not re.fullmatch(r'[a-f0-9]{40}', sha)
                or manifest.get('restored_at') != descriptor.get('at')
                or instant(manifest['restored_at']) > datetime.now(timezone.utc)):
            return False
        report = get_report(descriptor['report_id'])
        review = report.get('qa_review') or {}
        if (report.get('type') != 'work.report.get.ok' or report.get('report_id') != descriptor['report_id']
                or report.get('status') != 'done' or report.get('qa_verdict') != 'accept'
                or report.get('target_sha') != sha or review.get('candidate_identity') != sha
                or review.get('gate_evidence_digest') != descriptor['sha256']
                or test not in str(review.get('reviewed_scope', ''))
                or 'restor' not in str(review.get('reviewed_scope', '')).lower()
                or type(review.get('commission_msg_id')) is not int or review['commission_msg_id'] < 0):
            return False
        main = api(REPOSITORY, 'commits/main')['sha']
        if not re.fullmatch(r'[a-f0-9]{40}', main) or not main_contains(checkout, sha, main):
            return False
        if selected is None:
            selected = effective_tests(checkout)
        aliases = {test}
        if suite == 'daemon':
            aliases.update({node, 'daemon::' + node})
        if aliases & set(selected):
            return False
        installed = manifest['installed']
        gate = json.loads(_read(installed['gate'], installed['gate_sha256']))
        if instant(gate['generated_at']) > instant(manifest['restored_at']):
            return False
        junit = _read(installed['junit'], installed['junit_sha256'])
        checkout = Path(checkout).resolve()
        junit_path = Path(installed['junit']).resolve()
        if not _passed(junit, suite, node):
            return False
        if suite == 'daemon':
            if (gate.get('sha') != sha or Path(gate.get('repo_root', '')).resolve() != checkout
                    or gate.get('finished') is not True or gate.get('passed') is not True
                    or gate.get('source', {}).get('clean_before') is not True
                    or gate.get('source', {}).get('clean_after') is not True
                    or gate['source'].get('sha_before') != sha or gate['source'].get('sha_after') != sha):
                return False
            tiers = [tier for tier in gate.get('tiers', []) if tier.get('passed') is True
                     and Path(tier.get('junit', '')).resolve() == junit_path]
            if len(tiers) != 1 or not any('not quarantined' in str(arg)
                                          for arg in tiers[0].get('command', [])):
                return False
        elif suite in ('cli', 'shared'):
            if (gate.get('complete') is not True or gate.get('suite') != suite or gate.get('blocking_returncode') != 0
                    or Path(gate.get('repo_root', '')).resolve() != checkout
                    or Path(gate.get('junit', '')).resolve() != junit_path
                    or gate.get('junit_sha256') != installed['junit_sha256']
                    or gate.get('source_before') != {'sha': sha, 'dirty': False}
                    or gate.get('source_after') != {'sha': sha, 'dirty': False}):
                return False
        else:
            file = node.split('::', 1)[0]
            rows = [row for row in gate.get('files', []) if row.get('file') == file]
            if (gate.get('exit_code') != 0 or Path(gate.get('repo_root', '')).resolve() != checkout
                    or gate.get('source_before') != {'sha': sha, 'dirty': False}
                    or gate.get('source_after') != {'sha': sha, 'dirty': False}
                    or len(rows) != 1 or rows[0].get('status') != 'pass'
                    or (rows[0].get('execution') or {}).get('exitCode') != 0
                    or Path(rows[0].get('junit', '')).resolve() != junit_path):
                return False
        hosted = manifest['hosted']
        run, attempt, artifact = int(hosted['run']), int(hosted['attempt']), int(hosted['artifact'])
        if run <= 0 or attempt <= 0 or artifact <= 0:
            return False
        response = api(REPOSITORY, f'actions/runs/{run}/attempts/{attempt}')
        if (response.get('head_sha') != sha or response.get('status') != 'completed'
                or response.get('conclusion') != 'success'
                or instant(response['updated_at']) > instant(manifest['restored_at'])):
            return False
        listing = api(REPOSITORY, f'actions/runs/{run}/artifacts?per_page=100')
        artifacts = listing['artifacts']
        if listing.get('total_count') != len(artifacts):
            return False
        found = [a for a in artifacts if a.get('id') == artifact and not a.get('expired')
                 and str(a.get('name', '')).endswith(f'-{run}-{attempt}') and 0 < a.get('size_in_bytes', 0) <= 8_000_000]
        if len(found) != 1:
            return False
        if suite == 'daemon':
            allowed = {'daemon/gate/unit.junit.xml', 'daemon/gate/smoke.junit.xml'}
        elif suite in ('cli', 'shared'):
            allowed = {f'{suite}/gate/pytest.junit.xml'}
        else:
            file = node.split('::', 1)[0]
            allowed = {'npm/gate/node-junit/' + hashlib.sha256(file.encode()).hexdigest() + '.xml'}
        member = hosted['junit_member']
        if member not in allowed:
            return False
        raw = hosted_junit(REPOSITORY, artifact, member)
        return hashlib.sha256(raw).hexdigest() == hosted['junit_sha256'] and _passed(raw, suite, node)
    except (OSError, ValueError, TypeError, KeyError, ET.ParseError, BadZipFile, subprocess.SubprocessError):
        return False
