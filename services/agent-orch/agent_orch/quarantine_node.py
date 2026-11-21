"""Read exact failed Node cases from a retained first-attempt gate artifact."""
from __future__ import annotations

import hashlib
import copy
from io import BytesIO
import json
import subprocess
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


def failures(raw):
    with ZipFile(BytesIO(raw)) as archive:
        by_name = {member.filename: member for member in archive.infolist()}
        summary_name = 'npm/gate/npm-test-summary.json'
        summary = by_name.get(summary_name)
        if not summary or summary.file_size > 12_000_000:
            raise ValueError('Node gate summary unavailable')
        data = json.loads(archive.read(summary))
        rows = data.get('files')
        if not isinstance(rows, list) or not rows or data.get('runner_errors') or data.get('directory_cleanup_errors'):
            raise ValueError('Node failure census unavailable')
        result, seen = [], set()
        for row in rows:
            status = row.get('status')
            if status in ('pass', 'advisory'):
                continue
            if status != 'fail' or not isinstance(row.get('file'), str):
                raise ValueError('Node job contains an unresolved non-test failure')
            if (row.get('execution') or {}).get('exitCode') in (None, 0):
                raise ValueError('Node failure is not an identified test assertion')
            file = row['file']
            if not file.startswith('test/') or '..' in file.split('/'):
                raise ValueError('invalid Node test file identity')
            name = hashlib.sha256(file.encode()).hexdigest() + '.xml'
            member = by_name.get('npm/gate/node-junit/' + name)
            if not member or member.file_size > 12_000_000:
                raise ValueError('Node JUnit unavailable')
            root = ET.fromstring(archive.read(member))
            before = len(result)
            def walk(element, parents):
                if element.tag == 'testsuite' and element.get('name'):
                    parents = [*parents, element.get('name')]
                if element.tag == 'testcase':
                    nodes = [child for child in element if child.tag in ('failure', 'error')]
                    if nodes:
                        if len(nodes) != 1 or not element.get('name'):
                            raise ValueError('ambiguous Node failure')
                        name = ' '.join([*parents, element.get('name')])
                        test = file + '::' + name
                        if test in seen:
                            raise ValueError('duplicate Node test identity')
                        seen.add(test)
                        detail = nodes[0]
                        result.append({'test': test,
                            'assertion_digest': hashlib.sha256(detail.get('message', '').encode()).hexdigest(),
                            'phase': 'call' if detail.get('type') == 'testCodeFailure' else 'unknown',
                            'parents': parents})
                for child in element:
                    if child.tag in ('testsuites', 'testsuite', 'testcase'):
                        walk(child, parents)
            walk(root, [])
            if len(result) == before:
                raise ValueError('Node failure has no exact test case')
        if not result:
            raise ValueError('Node job has no identified test failures')
        return result


def enrich(rows, settings, *, api, download):
    """Feed complete Node failure facts through the existing classifier."""
    from . import reliability_hosted
    from .reliability_artifacts import job_artifacts
    output = copy.deepcopy(rows)
    bound, missing = {}, []
    decisions = {(str(d['run']), d['sha'], d['job'], d['step']): d for d in settings.get('decisions', [])}
    for row in output:
        if row.get('first') != 'failure' or row.get('missing'):
            continue
        targets = [failure for failure in row.get('failures', [])
                   if failure.get('job') == 'frontend' and failure.get('step') == 'Supported npm tests']
        if not targets:
            continue
        try:
            response = api(settings['repository'], f"actions/runs/{int(row['id'])}/artifacts?per_page=100")
            artifacts = response['artifacts']
            if response.get('total_count') != len(artifacts):
                raise ValueError('incomplete artifact census')
            facts = None
            for artifact in job_artifacts('frontend', artifacts, str(row['id'])):
                if artifact.get('expired') or not 0 < artifact['size_in_bytes'] <= 8_000_000:
                    continue
                try:
                    facts = failures(download(settings['repository'], f"actions/artifacts/{int(artifact['id'])}/zip"))
                except (OSError, ValueError, KeyError, TypeError, BadZipFile, ET.ParseError, subprocess.SubprocessError):
                    continue
                break
            if not facts:
                raise ValueError('Node test facts unavailable')
            for failure in targets:
                failure['tests'] = [{k: fact[k] for k in ('test', 'assertion_digest')} for fact in facts]
                for fact in facts:
                    bound[(str(row['id']), failure['job'], failure['step'], fact['test'])] = {
                        'test': fact['test'], 'phase': fact['phase'], 'suite': 'javascript', 'parents': fact['parents']}
            row['classification'], row['causes'] = reliability_hosted.classifications(
                row, decisions, settings.get('signatures', []), settings.get('checkout'))
        except (OSError, ValueError, KeyError, TypeError, BadZipFile, ET.ParseError, subprocess.SubprocessError):
            missing.append(f"public-checks/{row.get('id')}/1: Node failure identity unavailable")
    return output, bound, missing
