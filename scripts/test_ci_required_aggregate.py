#!/usr/bin/env python3
"""Focused aggregate CLI and workflow wiring controls (PyYAML is an existing gate dependency)."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/ci_required_aggregate.py'
JOBS = {'frontend', 'web', 'daemon', 'contracts'}


class AggregateControls(unittest.TestCase):
    def run_aggregate(self, needs):
        env = dict(os.environ, CI_REQUIRED_NEEDS=needs if isinstance(needs, str) else json.dumps(needs))
        return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)

    def test_success(self):
        done = self.run_aggregate({job: {'result': 'success', 'outputs': {}} for job in JOBS})
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_each_partition_must_succeed(self):
        for job in JOBS:
            for result in ('failure', 'skipped', 'cancelled', 'error', None, '', 'SUCCESS'):
                with self.subTest(job=job, result=result):
                    needs = {key: {'result': 'success'} for key in JOBS}
                    needs[job] = {'result': result}
                    self.assertEqual(self.run_aggregate(needs).returncode, 1)
            needs = {key: {'result': 'success'} for key in JOBS if key != job}
            self.assertEqual(self.run_aggregate(needs).returncode, 1)

    def test_invalid_data(self):
        valid = {job: {'result': 'success'} for job in JOBS}
        cases = ['', '{', 'null', '[]', 'true', '{}', dict(valid, future={'result': 'success'})]
        for value in ({}, [], None, 'success', {'outputs': {}}):
            cases.append(dict(valid, frontend=value))
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(self.run_aggregate(value).returncode, 1)
        env = dict(os.environ)
        env.pop('CI_REQUIRED_NEEDS', None)
        self.assertEqual(subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True).returncode, 1)


class WorkflowControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BaseLoader preserves GitHub's `on` key instead of YAML 1.1's boolean conversion.
        cls.workflow = yaml.load((ROOT / '.github/workflows/predeploy-tests.yml').read_text(), Loader=yaml.BaseLoader)

    def test_triggers_and_permissions(self):
        self.assertEqual(self.workflow['on'], {'push': {'branches': ['main']}, 'pull_request': ''})
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

    def test_isolation_and_failure_policy(self):
        for job_id, job in self.workflow['jobs'].items():
            self.assertNotIn('continue-on-error', job)
            if job_id != 'checks':
                self.assertNotIn('if', job)
            self.assertEqual(job['runs-on'], 'ubuntu-latest')
            self.assertLessEqual(int(job['timeout-minutes']), 70 if job_id == 'daemon' else 50)
            self.assertTrue(any(step.get('uses') == 'actions/checkout@v4' for step in job['steps']))
            for step in job['steps']:
                self.assertNotIn('continue-on-error', step)
            if job_id == 'checks':
                continue
            self.assertNotIn('needs', job)
            steps = job['steps']
            integrity = [step for step in steps if 'source_integrity.py' in step.get('run', '')]
            self.assertGreaterEqual(len(integrity), 2)
            self.assertEqual(integrity[-1]['if'], '${{ always() }}')
            uploads = [step for step in steps if step.get('uses') == 'actions/upload-artifact@v4']
            self.assertTrue(any(step.get('if') == '${{ always() }}' for step in uploads))
            for step in uploads:
                for identity in ('github.job', 'github.run_id', 'github.run_attempt'):
                    self.assertIn(identity, step['with']['name'])
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
        self.assertCountEqual(targets, [('npm-test', None), ('web-gate', None), ('v2-merge-ci', None), ('cli-pytest', 'tests'), ('shared-pytest', 'tests')])
        runs = [command for _, command in commands]
        for command in ('python3 scripts/test_check_public_residue.py', 'python3 scripts/check_public_residue.py'):
            self.assertEqual(runs.count(command), 1)
        self.assertEqual(sum('PYTHONPATH=mic-server python3 -m pytest mic-server/test' in command for command in runs), 1)
        self.assertIn(('vendor/dashboard-library', 'npm run build\nnpm run check:dist\nnpm test\n'), commands)
        self.assertIn('python3 -m pip install pytest==8.4.2 numpy==2.5.3 scipy==1.18.1', '\n'.join(runs))


if __name__ == '__main__':
    unittest.main()
