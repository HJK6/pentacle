#!/usr/bin/env python3
"""Keep the required `checks` context green only when every partition succeeds."""
import json
import os
import sys

EXPECTED_JOB_IDS = ('frontend', 'web', 'daemon-unit', 'daemon', 'daemon-serial-control', 'contracts')


def main():
    try:
        needs = json.loads(os.environ['CI_REQUIRED_NEEDS'])
    except (KeyError, ValueError):
        print('checks: missing or invalid needs JSON', file=sys.stderr)
        return 1
    if not isinstance(needs, dict) or set(needs) != set(EXPECTED_JOB_IDS):
        print('checks: needs must exactly match expected partitions', file=sys.stderr)
        return 1
    event = os.environ.get('CI_EVENT_NAME')
    if event not in ('push', 'pull_request', 'workflow_dispatch'):
        print('checks: unsupported or missing event', file=sys.stderr)
        return 1
    skipped = {'daemon-unit', 'daemon'} if event == 'workflow_dispatch' else {'daemon-serial-control'}
    failed = [job for job, data in needs.items()
              if not isinstance(data, dict) or data.get('result') != ('skipped' if job in skipped else 'success')]
    if failed:
        print('checks: non-success partitions: ' + ', '.join(sorted(failed)), file=sys.stderr)
        return 1
    print('checks: every required partition succeeded')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
