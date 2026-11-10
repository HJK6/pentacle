#!/usr/bin/env python3
"""Focused aggregate CLI and workflow wiring controls (PyYAML is an existing gate dependency)."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/ci_required_aggregate.py'
JOBS = {'frontend', 'web', 'daemon-unit', 'daemon', 'daemon-serial-control', 'contracts'}


def required_needs(event='pull_request'):
    skipped = {'daemon-unit', 'daemon'} if event == 'workflow_dispatch' else {'daemon-serial-control'}
    return {job: {'result': 'skipped' if job in skipped else 'success'} for job in JOBS}


class AggregateControls(unittest.TestCase):
    def run_aggregate(self, needs, event='pull_request'):
        env = dict(os.environ, CI_REQUIRED_NEEDS=needs if isinstance(needs, str) else json.dumps(needs), CI_EVENT_NAME=event)
        return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)

    def test_success(self):
        for event in ('push', 'pull_request', 'workflow_dispatch'):
            done = self.run_aggregate(required_needs(event), event)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_each_partition_must_succeed(self):
        for event in ('push', 'pull_request', 'workflow_dispatch'):
            for job in JOBS:
                for result in ('success', 'failure', 'skipped', 'cancelled', 'error', None, '', 'SUCCESS'):
                    with self.subTest(event=event, job=job, result=result):
                        needs = required_needs(event)
                        expected = needs[job]['result']
                        needs[job] = {'result': result}
                        self.assertEqual(self.run_aggregate(needs, event).returncode, 0 if result == expected else 1)
                needs = required_needs(event); del needs[job]
                self.assertEqual(self.run_aggregate(needs, event).returncode, 1)

    def test_invalid_data(self):
        valid = required_needs()
        cases = ['', '{', 'null', '[]', 'true', '{}', dict(valid, future={'result': 'success'})]
        for value in ({}, [], None, 'success', {'outputs': {}}):
            cases.append(dict(valid, frontend=value))
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(self.run_aggregate(value).returncode, 1)
        env = dict(os.environ)
        env.pop('CI_REQUIRED_NEEDS', None)
        self.assertEqual(subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True).returncode, 1)
        for event in ('', 'schedule', 'PULL_REQUEST'):
            self.assertEqual(self.run_aggregate(valid, event).returncode, 1)


class DaemonUnionControls(unittest.TestCase):
    """Synthetic receipts; the fixed file-to-cell answers are independent of the plugin."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='union-control-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cells = self.base / 'cells'
        self.sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        self.out = self.base / 'union/v2-daemon-union-evidence.json'
        self.files = ['test_cell_0.py', 'test_cell_4.py', 'test_cell_16.py',
                      'test_cell_1.py', 'test_cell_6.py', 'test_cell_5.py']
        nodes = [f'{file}::test_ok' for file in self.files]
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        eligible = {'count': 6, 'nodeids_sha256': digest(''.join(n + '\n' for n in nodes)),
                    'files': sorted(self.files), 'node_digests': sorted(digest(n) for n in nodes)}
        for i, (file, node) in enumerate(zip(self.files, nodes)):
            gate = self.cells / f'cell-{i}/daemon/gate'
            gate.mkdir(parents=True)
            manifest = {'schema': 'pentacle.v2.gate-shard-manifest.v1', 'shard_index': i,
                        'shard_count': 6, 'assignment': 'sha256(nodeid file prefix) mod shard_count',
                        'rootpath': '/synthetic/service', 'eligible': eligible,
                        'assigned': {'count': 1, 'nodeids_sha256': digest(node + '\n'), 'files': [file],
                                     'nodes': [{'node': node, 'sha256': digest(node)}]}}
            self.write(gate / 'unit.shard-manifest.json', manifest)
            self.write(gate / 'v2-unit-evidence.json', self.receipt('unit'))
            rows = [{'kind': 'run_start', 'schema': 'pentacle.v2.gate-progress.v1', 'run_id': f'r{i}:unit'},
                    {'kind': 'collected', 'count': 1, 'nodeids_sha256': digest(node + '\n')},
                    {'kind': 'start', 'node': node},
                    *({'kind': 'phase', 'node': node, 'phase': phase, 'outcome': 'passed'}
                      for phase in ('setup', 'call', 'teardown')),
                    {'kind': 'finish', 'node': node}, {'kind': 'run_finish', 'exitstatus': 0}]
            (gate / 'unit.progress.jsonl').write_text(''.join(json.dumps(dict(row, seq=j + 1, t=j / 10)) + '\n'
                                                           for j, row in enumerate(rows)))
            suite = ET.Element('testsuite', tests='2', failures='0', errors='0', skipped='1', time='1')
            ET.SubElement(suite, 'testcase', classname=file[:-3], name='test_ok')
            skip = ET.SubElement(ET.SubElement(suite, 'testcase', classname='', name='test_collection_skip'),
                                 'skipped', message='collection skipped')
            skip.text = repr(('tests/test_collection_skip.py', 7, 'Skipped: synthetic'))
            ET.ElementTree(suite).write(gate / 'unit.junit.xml')

    def receipt(self, tier):
        return {'schema': 'pentacle.v2.gate-evidence.v1', 'gate': tier, 'sha': self.sha,
                'source': {'sha_before': self.sha, 'sha_after': self.sha, 'clean_before': True, 'clean_after': True},
                'finished': True, 'passed': True,
                'tiers': [{'tier': tier, 'passed': True, 'secondary_outcomes': [],
                           'counts': {'tests': 2, 'skipped': 1, 'failures': 0, 'errors': 0, 'duration_s': 1}}]}

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data))

    def run_union(self, *extra):
        args = list(extra) if extra else ['verify', '--cells', str(self.cells), '--shard-count', '6',
                                         '--expect-sha', self.sha, '--checkout', str(ROOT), '--out', str(self.out)]
        return subprocess.run([sys.executable, str(ROOT / 'scripts/ci_daemon_union.py'), *args],
                              capture_output=True, text=True, timeout=15)

    def test_complete_union_then_exactly_one_smoke(self):
        done = self.run_union()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        data = json.loads(self.out.read_text())
        self.assertTrue(data['unit_union_passed'])
        self.assertFalse(data['passed'])
        self.assertEqual(len(data['collection_skips']), 1)
        smoke = self.base / 'smoke'
        smoke.mkdir()
        self.write(smoke / 'v2-smoke-evidence.json', self.receipt('smoke'))
        # Smoke inputs must be real retained bytes, not just references from a receipt.
        (smoke / 'smoke.junit.xml').write_bytes(next(self.cells.rglob('unit.junit.xml')).read_bytes())
        (smoke / 'smoke.progress.jsonl').write_bytes(next(self.cells.rglob('unit.progress.jsonl')).read_bytes())
        args = ['finalize', '--union', str(self.out), '--smoke', str(smoke / 'v2-smoke-evidence.json'), '--out', str(self.out)]
        done = self.run_union(*args)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(json.loads(self.out.read_text())['passed'])
        self.assertNotEqual(self.run_union(*args).returncode, 0, 'a finalized receipt cannot attach smoke twice')

    def test_corrupt_or_incomplete_cells_refuse(self):
        manifest = next(self.cells.rglob('unit.shard-manifest.json'))
        receipt = manifest.with_name('v2-unit-evidence.json')
        journal = manifest.with_name('unit.progress.jsonl')
        junit = manifest.with_name('unit.junit.xml')
        originals = {p: p.read_bytes() for p in (manifest, receipt, journal, junit)}
        mutations = [
            (manifest, lambda d: d.update(shard_index=1)),
            (manifest, lambda d: d.update(shard_count=5)),
            (manifest, lambda d: d.update(shard_index=True)),
            (manifest, lambda d: d['eligible'].update(count=7)),
            (manifest, lambda d: d['assigned'].update(count=0, nodes=[])),
            (manifest, lambda d: d['assigned'].update(files=['test_cell_4.py'])),
            (manifest, lambda d: d['assigned']['nodes'][0].update(sha256='0' * 64)),
            (receipt, lambda d: d.update(sha='0' * 40)),
            (receipt, lambda d: d.update(finished=False)),
            (receipt, lambda d: d.update(passed=False)),
            (receipt, lambda d: d['source'].update(clean_after=False)),
            (receipt, lambda d: d['tiers'][0].update(secondary_outcomes=['journal_incomplete'])),
            (receipt, lambda d: d['tiers'][0]['counts'].update(duration_s=2)),
            (receipt, lambda d: d['tiers'][0]['counts'].update(duration_s=float('nan'))),
        ]
        for path, mutate in mutations:
            with self.subTest(mutation=mutate):
                data = json.loads(originals[path]); mutate(data); self.write(path, data)
                self.assertNotEqual(self.run_union().returncode, 0)
                if self.out.exists():
                    self.assertFalse(json.loads(self.out.read_text())['passed'])
                path.write_bytes(originals[path])
        for path, damaged in ((manifest, b'{'), (receipt, b'[]'), (journal, b'not json\n'),
                              (journal, originals[journal][:-1]), (junit, b'<testsuite>')):
            with self.subTest(path=path, damaged=damaged[:20]):
                path.write_bytes(damaged)
                self.assertNotEqual(self.run_union().returncode, 0)
                path.write_bytes(originals[path])
        # A complete journal with a false collection digest is also RED.
        rows = [json.loads(line) for line in originals[journal].splitlines()]
        rows[1]['nodeids_sha256'] = '0' * 64
        journal.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.assertNotEqual(self.run_union().returncode, 0)

    def test_missing_extra_and_overlapping_cells_refuse(self):
        cell = self.cells / 'cell-5'
        moved = self.base / 'held-cell'
        cell.rename(moved)
        self.assertNotEqual(self.run_union().returncode, 0)
        moved.rename(cell)
        (self.cells / 'extra').mkdir()
        self.assertNotEqual(self.run_union().returncode, 0)
        (self.cells / 'extra').rmdir()
        source = next((self.cells / 'cell-0').rglob('unit.shard-manifest.json'))
        target = next((self.cells / 'cell-1').rglob('unit.shard-manifest.json'))
        data = json.loads(target.read_text()); data['assigned'] = json.loads(source.read_text())['assigned']
        self.write(target, data)
        self.assertNotEqual(self.run_union().returncode, 0)

    def test_join_head_junit_and_smoke_corruption_refuse(self):
        args = ['verify', '--cells', str(self.cells), '--shard-count', '6', '--expect-sha', '0' * 40,
                '--checkout', str(ROOT), '--out', str(self.out)]
        self.assertNotEqual(self.run_union(*args).returncode, 0)
        path = next(self.cells.rglob('unit.junit.xml'))
        original = path.read_bytes()
        for change in ('duplicate', 'failure', 'collection-reason'):
            tree = ET.fromstring(original)
            if change == 'duplicate':
                tree.append(ET.fromstring(ET.tostring(tree.find('testcase'))))
            elif change == 'failure':
                ET.SubElement(tree.find('testcase'), 'failure', message='synthetic')
            else:
                tree.find('.//skipped').text = repr(('tests/test_collection_skip.py', 7, 'Changed reason'))
            ET.ElementTree(tree).write(path)
            self.assertNotEqual(self.run_union().returncode, 0)
            path.write_bytes(original)
        self.assertEqual(self.run_union().returncode, 0)
        smoke = self.base / 'smoke'
        smoke.mkdir()
        self.write(smoke / 'v2-smoke-evidence.json', self.receipt('smoke'))
        (smoke / 'smoke.junit.xml').write_bytes(original)
        (smoke / 'smoke.progress.jsonl').write_bytes(next(self.cells.rglob('unit.progress.jsonl')).read_bytes())
        finish = ['finalize', '--union', str(self.out), '--smoke', str(smoke / 'v2-smoke-evidence.json'), '--out', str(self.out)]
        failed = self.receipt('smoke'); failed['passed'] = False
        self.write(smoke / 'v2-smoke-evidence.json', failed)
        self.assertNotEqual(self.run_union(*finish).returncode, 0)
        self.assertFalse(json.loads(self.out.read_text())['passed'])
        self.assertEqual(self.run_union().returncode, 0)
        self.write(smoke / 'v2-smoke-evidence.json', self.receipt('smoke'))
        unit = next(self.cells.rglob('unit.progress.jsonl'))
        unit.write_text(unit.read_text() + '\n')
        self.assertNotEqual(self.run_union(*finish).returncode, 0, 'unit bytes cannot drift after verify')


class WorkflowControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BaseLoader preserves GitHub's `on` key instead of YAML 1.1's boolean conversion.
        cls.workflow = yaml.load((ROOT / '.github/workflows/predeploy-tests.yml').read_text(), Loader=yaml.BaseLoader)

    def test_triggers_and_permissions(self):
        self.assertEqual(self.workflow['on'], {'push': {'branches': ['main']}, 'pull_request': '',
                         'workflow_dispatch': {'inputs': {'daemon_mode': {'type': 'choice',
                           'options': ['serial-control'], 'required': 'true'}}}})
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})

    def test_expected_ids_equal_every_need(self):
        spec = importlib.util.spec_from_file_location('aggregate', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        jobs = self.workflow['jobs']
        self.assertEqual(set(jobs), JOBS | {'checks'})
        self.assertEqual(set(module.EXPECTED_JOB_IDS), JOBS)
        self.assertEqual(set(jobs['checks']['needs']), set(module.EXPECTED_JOB_IDS))
        self.assertEqual(len(jobs['checks']['needs']), len(module.EXPECTED_JOB_IDS))
        self.assertEqual(jobs['checks']['if'], '${{ always() }}')
        aggregate = next(step for step in jobs['checks']['steps'] if 'run' in step)
        self.assertEqual(aggregate['run'], 'python3 scripts/ci_required_aggregate.py')
        self.assertEqual(aggregate['env']['CI_REQUIRED_NEEDS'], '${{ toJSON(needs) }}')
        self.assertEqual(aggregate['env']['CI_EVENT_NAME'], '${{ github.event_name }}')

    def test_isolation_and_failure_policy(self):
        for job_id, job in self.workflow['jobs'].items():
            self.assertNotIn('continue-on-error', job)
            conditions = {'daemon-unit': "${{ github.event_name != 'workflow_dispatch' }}",
                          'daemon': "${{ success() && github.event_name != 'workflow_dispatch' }}",
                          'daemon-serial-control': "${{ github.event_name == 'workflow_dispatch' && inputs.daemon_mode == 'serial-control' }}"}
            if job_id in conditions:
                self.assertEqual(job['if'], conditions[job_id])
            elif job_id != 'checks':
                self.assertNotIn('if', job)
            self.assertEqual(job['runs-on'], 'ubuntu-24.04' if job_id.startswith('daemon') else 'ubuntu-latest')
            bound = 110 if job_id == 'daemon-serial-control' else 40 if job_id in ('daemon-unit', 'daemon') else 50
            self.assertLessEqual(int(job['timeout-minutes']), bound)
            self.assertTrue(any(step.get('uses') == 'actions/checkout@v4' for step in job['steps']))
            for step in job['steps']:
                self.assertNotIn('continue-on-error', step)
            if job_id == 'checks':
                continue
            if job_id == 'daemon':
                self.assertEqual(job['needs'], ['daemon-unit'])
            else:
                self.assertNotIn('needs', job)
            if job_id == 'daemon-unit':
                self.assertEqual(job['strategy'], {'fail-fast': 'false', 'matrix': {'shard': ['0','1','2','3','4','5']}})
            steps = job['steps']
            integrity = [step for step in steps if 'source_integrity.py' in step.get('run', '')]
            self.assertGreaterEqual(len(integrity), 2)
            self.assertEqual(integrity[-1]['if'], '${{ always() }}')
            uploads = [step for step in steps if step.get('uses') == 'actions/upload-artifact@v4']
            self.assertTrue(any(step.get('if') == '${{ always() }}' for step in uploads))
            for step in uploads:
                for identity in ('github.job', 'github.run_id', 'github.run_attempt'):
                    self.assertIn(identity, step['with']['name'])
                if job_id == 'daemon-unit':
                    self.assertIn('${{ matrix.shard }}', step['with']['name'])
            for step in steps:
                for line in step.get('run', '').splitlines():
                    if 'scripts/gate_env.py run' not in line:
                        continue
                    args = shlex.split(line)
                    self.assertEqual(args[args.index('--workspace') + 1], '${RUNNER_TEMP}/gate-env/${GITHUB_JOB}')
                    self.assertEqual(args[args.index('--attempt-manifest') + 1], '${RUNNER_TEMP}/gate-evidence/${GITHUB_JOB}/attempt-manifest.json')
                    self.assertNotIn('--timeout-seconds', args)

    def test_old_command_and_selection_union(self):
        commands = [(step.get('working-directory', ''), step.get('run', ''))
                    for name, job in self.workflow['jobs'].items() if name != 'checks' for step in job['steps']]
        targets = []
        for _, command in commands:
            for line in command.splitlines():
                if 'scripts/gate_env.py run' not in line:
                    continue
                args = shlex.split(line)
                targets.append((args[args.index('--target') + 1], args[args.index('--select') + 1] if '--select' in args else None))
        self.assertCountEqual(targets, [('npm-test', None), ('web-gate', None), ('v2-unit-ci-shard', None),
                                      ('v2-smoke', None), ('v2-merge-ci', None), ('cli-pytest', 'tests'), ('shared-pytest', 'tests')])
        jobs = self.workflow['jobs']
        unit = next(s['run'] for s in jobs['daemon-unit']['steps'] if '--target v2-unit-ci-shard' in s.get('run', ''))
        self.assertIn('--shard-count 6', unit)
        join = '\n'.join(s.get('run', '') for s in jobs['daemon']['steps'])
        self.assertIn('ci_daemon_union.py verify', join)
        self.assertIn('--shard-count 6', join)
        self.assertEqual(join.count('--target v2-smoke'), 1)
        self.assertEqual(join.count('ci_daemon_union.py finalize'), 1)
        self.assertLess(join.index('ci_daemon_union.py verify'), join.index('--target v2-smoke'))
        self.assertLess(join.index('--target v2-smoke'), join.index('ci_daemon_union.py finalize'))
        self.assertEqual(sum('ci_daemon_union.py' in run for _, run in commands), 2)
        pip = next(s['run'] for s in jobs['frontend']['steps'] if s.get('name') == 'Required CI controls')
        requirements = (ROOT / 'services/chat-stream-v2/requirements.txt').read_text()
        self.assertIn('pytest==9.0.3', requirements)
        self.assertIn('pytest==9.0.3', pip)
        self.assertIn('PyYAML==6.0.3', pip)
        runs = [command for _, command in commands]
        for command in ('python3 scripts/test_check_public_residue.py', 'python3 scripts/check_public_residue.py'):
            self.assertEqual(runs.count(command), 1)
        self.assertEqual(sum('PYTHONPATH=mic-server python3 -m pytest mic-server/test' in command for command in runs), 1)
        self.assertIn(('vendor/dashboard-library', 'npm run build\nnpm run check:dist\nnpm test\n'), commands)
        self.assertIn('python3 -m pip install pytest==8.4.2 numpy==2.5.3 scipy==1.18.1', '\n'.join(runs))


if __name__ == '__main__':
    unittest.main()
