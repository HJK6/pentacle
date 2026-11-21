"""Resolve first-attempt JUnit identities against actual gate progress records."""
from __future__ import annotations

from collections import defaultdict
import hashlib
from io import BytesIO
import json
import re
import subprocess
from zipfile import BadZipFile, ZipFile


def identities(raw, junit_cases, member_name='daemon/gate/unit.progress.jsonl'):
    """Return only uniquely observed, untruncated failed pytest node IDs."""
    matches = defaultdict(set)
    with ZipFile(BytesIO(raw)) as archive:
        members = [m for m in archive.infolist() if m.filename == member_name]
        if len(members) != 1 or members[0].file_size > 12_000_000:
            raise ValueError('first-attempt progress unavailable or ambiguous')
        records = [json.loads(line) for line in archive.read(members[0]).splitlines()]
    if any(row.get('kind') == 'journal_budget_exhausted' for row in records):
        raise ValueError('first-attempt progress truncated')
    for row in records:
        if row.get('kind') != 'phase' or row.get('outcome') != 'failed':
            continue
        node, phase = row.get('node'), row.get('phase')
        if not isinstance(node, str) or phase not in ('setup', 'call', 'teardown'):
            continue
        if re.search(r'\.\.\.#([a-f0-9]{12})', node):
            continue
        head, bracket, parameter = node.partition('[')
        file, *parts = head.split('::')
        if not file.startswith('tests/') or not file.endswith('.py') or not parts:
            continue
        if bracket:
            parts[-1] += bracket + parameter
        key = file[:-3].replace('/', '.')
        if len(parts) > 1:
            key += '.' + '.'.join(parts[:-1])
        key += '::' + parts[-1]
        for identity in (key, node):
            if identity in junit_cases:
                matches[identity].add((node, phase))
    return {key: {'test': next(iter(found))[0], 'phase': next(iter(found))[1]}
            for key, found in matches.items() if len(found) == 1}


def pytest_facts(raw):
    from xml.etree import ElementTree as ET
    root = ET.fromstring(raw)
    suites = [root] if root.tag == 'testsuite' else list(root.findall('testsuite'))
    expected = sum(int(s.attrib.get(k, '0')) for s in suites for k in ('errors', 'failures'))
    result = []
    for case in root.iter('testcase'):
        for detail in case:
            if detail.tag not in ('error', 'failure'):
                continue
            classname, name = case.get('classname'), case.get('name')
            if not classname or not name:
                raise ValueError('JUnit case identity missing')
            result.append({'test': classname + '::' + name,
                           'assertion_digest': hashlib.sha256(detail.get('message', '').encode()).hexdigest()})
    if not expected or expected != len(result):
        raise ValueError('incomplete JUnit failure census')
    return result


def bind_first_attempts(rows, repository, *, api, download):
    """Bind classifier cases to exact nodes in each retained first-attempt job."""
    from .reliability_artifacts import job_artifacts
    bound = {}
    missing = []
    for row in rows:
        if row.get('first') != 'failure':
            continue
        wanted = [c for c in row.get('causes', []) if c.get('kind') == 'non_code' and c.get('test')
                  and str(c.get('job', '')).startswith('daemon-unit')]
        if not wanted:
            continue
        try:
            response = api(repository, f"actions/runs/{int(row['id'])}/artifacts?per_page=100")
            artifacts = response['artifacts']
            if response.get('total_count') != len(artifacts):
                raise ValueError('incomplete artifact census')
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            missing.append(f"public-checks/{row.get('id')}/1: artifacts unavailable")
            continue
        jobs = {(c.get('job'), c.get('step')) for c in wanted}
        for job, step in jobs:
            cases = {c['test'] for c in wanted if c.get('job') == job and c.get('step') == step}
            resolved = {}
            for artifact in job_artifacts(job or '', artifacts, str(row['id'])):
                try:
                    if artifact.get('expired') or not 0 < artifact['size_in_bytes'] <= 8_000_000:
                        continue
                    raw = download(repository, f"actions/artifacts/{int(artifact['id'])}/zip")
                    resolved = identities(raw, cases)
                except (OSError, ValueError, KeyError, TypeError, BadZipFile, subprocess.SubprocessError):
                    continue
                if resolved:
                    break
            for case, exact in resolved.items():
                bound[(str(row['id']), job, step, case)] = exact
            for case in cases - resolved.keys():
                missing.append(f"public-checks/{row['id']}/1: exact identity unavailable for {case}")
    return bound, missing


def reviewed_bindings(rows, bound, signatures):
    """Require an occurrence review or a signature reviewed for this phase."""
    accepted = {}
    for row in rows:
        for cause in row.get('causes', []):
            if cause.get('kind') != 'non_code':
                continue
            key = (str(row.get('id')), cause.get('job'), cause.get('step'), cause.get('test'))
            identity = bound.get(key)
            if not identity or identity.get('phase') not in ('setup', 'call', 'teardown'):
                continue
            failures = [failure for failure in row.get('failures', [])
                        if failure.get('job') == cause.get('job') and failure.get('step') == cause.get('step')]
            if len(failures) != 1:
                continue
            runners = failures[0].get('runner', [])
            reviewed = bool(cause.get('review_source') and cause.get('reviewer') and cause.get('revision'))
            if not reviewed:
                reviewed = any(rule.get('kind') == 'non_code'
                               and rule.get('test') == cause.get('test')
                               and rule.get('step') == cause.get('step')
                               and rule.get('assertion_digest') == cause.get('assertion_digest')
                               and rule.get('revision') == cause.get('revision')
                               and rule.get('fixture_phase') == identity.get('phase')
                               and any(rule.get('runner') and rule['runner'] in runner for runner in runners)
                               for rule in signatures)
            if reviewed:
                accepted[key] = identity
    return accepted


def _python_target(artifacts, repository, run_id, job, suite, download):
    from .reliability_artifacts import job_artifacts

    junit_name = 'daemon/gate/unit.junit.xml' if suite == 'daemon' else f'{suite}/gate/pytest.junit.xml'
    progress_name = 'daemon/gate/unit.progress.jsonl' if suite == 'daemon' else f'{suite}/gate/pytest.progress.jsonl'
    for artifact in job_artifacts(job, artifacts, run_id):
        if artifact.get('expired') or not 0 < artifact['size_in_bytes'] <= 8_000_000:
            continue
        raw = download(repository, f"actions/artifacts/{int(artifact['id'])}/zip")
        with ZipFile(BytesIO(raw)) as archive:
            members = [member for member in archive.infolist() if member.filename == junit_name]
            if len(members) != 1 or members[0].file_size > 12_000_000:
                continue
            facts = pytest_facts(archive.read(members[0]))
        exact = identities(raw, {fact['test'] for fact in facts}, progress_name)
        if len(exact) != len(facts):
            raise ValueError('incomplete Python progress association')
        return facts, exact
    raise ValueError('Python test facts unavailable')


def enrich_other_python(rows, settings, *, api, download):
    """Resolve Python failures using retained first-attempt gate receipts."""
    import copy
    from xml.etree import ElementTree as ET
    from . import reliability_hosted

    output = copy.deepcopy(rows)
    bound, missing = {}, []
    decisions = {(str(d['run']), d['sha'], d['job'], d['step']): d for d in settings.get('decisions', [])}
    suites = {'CLI checks': 'cli', 'Shared checks': 'shared'}
    artifact_errors = (OSError, ValueError, KeyError, TypeError, BadZipFile, ET.ParseError,
                       subprocess.SubprocessError)
    for row in output:
        if row.get('first') != 'failure' or row.get('missing'):
            continue
        targets = [failure for failure in row.get('failures', [])
                   if ((failure.get('job') == 'contracts' and failure.get('step') in suites)
                       or (str(failure.get('job', '')).startswith('daemon-unit (')
                           and failure.get('step') == 'Daemon unit partition'))]
        if not targets:
            continue
        run_id = str(row['id'])
        repository = settings['repository']
        try:
            response = api(repository, f'actions/runs/{int(run_id)}/artifacts?per_page=100')
            artifacts = response['artifacts']
            if response.get('total_count') != len(artifacts):
                raise ValueError('incomplete artifact census')
        except artifact_errors:
            artifacts = []
        unclassified_steps = set()
        for failure in targets:
            job, step = failure['job'], failure['step']
            suite = suites.get(step, 'daemon')
            try:
                facts, exact = _python_target(artifacts, repository, run_id, job, suite, download)
            except artifact_errors:
                unclassified_steps.add((job, step))
                missing.append(f'public-checks/{run_id}/1: Python failure identity unavailable for {job} / {step}')
                continue
            failure['tests'] = facts
            for case, identity in exact.items():
                bound[(run_id, job, step, case)] = {**identity, 'suite': suite}
        row['classification'], row['causes'] = reliability_hosted.classifications(
            row, decisions, settings.get('signatures', []), settings.get('checkout'),
            unclassified_steps=unclassified_steps)
    return output, bound, missing
