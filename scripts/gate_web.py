#!/usr/bin/env python3
"""Closed web-gate subject: existing real-browser journey and interrupt contract."""
import json
import subprocess
import sys
from pathlib import Path


def main():
    summary = Path(sys.argv[1])
    summary.parent.mkdir(parents=True, exist_ok=True)
    phases = []
    commands = [('browser', ['node', 'test/e2e/web_gate.js']),
                ('interrupt', ['node', '--test', 'test/e2e/web_gate_interrupt.test.js'])]
    for name, argv in commands:
        with (summary.parent / ('web-%s.log' % name)).open('w') as log:
            done = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
        phases.append({'name': name, 'returncode': done.returncode})
        summary.write_text(json.dumps({'schema': 'pentacle.web-gate-summary.v1', 'phases': phases}) + '\n')
        if done.returncode:
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
