#!/usr/bin/env python3
"""Run blocking pytest cases and report quarantined cases as advisory."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def _observed(path, expected):
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    if any(case.find('skipped') is not None for case in cases):
        return False
    observed = [case.get('classname', '') + '::' + case.get('name', '') for case in cases]
    wanted = []
    for node in expected:
        head, bracket, parameter = node.partition('[')
        file, *parts = head.split('::')
        if not file.endswith('.py') or not parts:
            return False
        if bracket:
            parts[-1] += bracket + parameter
        module = file[:-3].replace('/', '.')
        if len(parts) > 1:
            module += '.' + '.'.join(parts[:-1])
        wanted.append(module + '::' + parts[-1])
    return Counter(observed) == Counter(wanted)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--suite', choices=('daemon', 'cli', 'shared'), required=True)
    parser.add_argument('--junit', required=True)
    parser.add_argument('--basetemp', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--strict-capabilities', action='store_true')
    parser.add_argument('selection', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    final = Path(args.manifest + '.final')
    final.unlink(missing_ok=True)
    Path(args.manifest).unlink(missing_ok=True)
    Path(args.junit).unlink(missing_ok=True)
    selection = args.selection[1:] if args.selection[:1] == ['--'] else args.selection
    if not selection:
        print('quarantine: explicit pytest selection required', file=sys.stderr)
        return 3
    root = Path(args.root).resolve()
    def source():
        try:
            sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
            dirty = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=all'], text=True, stderr=subprocess.DEVNULL, timeout=10).strip())
            return {'sha': sha, 'dirty': dirty}
        except (OSError, subprocess.SubprocessError):
            return None
    before = source()
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(filter(None, (str(root/'scripts'), str(root/'services/chat-stream-v2'), env.get('PYTHONPATH', ''))))
    common = ['-p', 'quarantine_plugin', f'--quarantine-root={root}', f'--quarantine-suite={args.suite}']
    strict = ['--strict-capabilities'] if args.strict_capabilities else []
    blocking_marker = 'not quarantined' + (' and not soak' if args.suite == 'daemon' else '')
    blocking = [sys.executable, '-m', 'pytest', *selection, '-q', '-rs', *strict,
                *common, f'--quarantine-manifest={args.manifest}', '-m', blocking_marker,
                '-p', 'tools.gate_progress', f'--gate-progress-journal={Path(args.junit).with_name("pytest.progress.jsonl")}',
                f'--gate-progress-run-id={env.get("PENTACLE_GATE_ATTEMPT_ID", "quarantine")}',
                f'--junitxml={args.junit}', '--basetemp', args.basetemp]
    result = subprocess.run(blocking, env=env, check=False).returncode
    try:
        manifest = json.loads(Path(args.manifest).read_text())
        if manifest.get('complete') is not True or not isinstance(manifest.get('quarantined'), list):
            raise ValueError('quarantine selection incomplete')
        expected = manifest['quarantined']
        after = source()
        final.write_text(json.dumps({'complete': True, 'generated_at': datetime.now(timezone.utc).isoformat(),
                                     'suite': args.suite, 'repo_root': str(root),
                                     'source_before': before, 'source_after': after,
                                     'blocking_returncode': result, 'junit': str(Path(args.junit).resolve()),
                                     'junit_sha256': hashlib.sha256(Path(args.junit).read_bytes()).hexdigest()}))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        final.write_text(json.dumps({'complete': False, 'reason': str(exc)}))
        print('quarantine selection incomplete: ' + str(exc), file=sys.stderr)
        return result if result not in (0, 5) else 3
    files = sorted({test.split('::', 1)[0] for test in expected})
    if files:
        for node in expected:
            print('ADVISORY ' + node, flush=True)
        advisory_junit = str(Path(args.junit).with_name('pytest-advisory.junit.xml'))
        Path(advisory_junit).unlink(missing_ok=True)
        advisory = [sys.executable, '-m', 'pytest', *expected, '-q', '-rs', *strict, *common,
                    f'--quarantine-manifest={args.manifest}.advisory', '-m', 'quarantined',
                    f'--junitxml={advisory_junit}', '--basetemp', args.basetemp + '-advisory']
        try:
            advisory_result = subprocess.run(advisory, env=env, check=False, timeout=60).returncode
            try:
                complete = _observed(advisory_junit, expected)
            except (OSError, ET.ParseError):
                complete = False
            print(f'ADVISORY returncode: {advisory_result}; complete: {complete}', flush=True)
        except subprocess.TimeoutExpired:
            print('ADVISORY timed out or incomplete', flush=True)
        except OSError as exc:
            print(f'ADVISORY unavailable: {exc}', flush=True)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
