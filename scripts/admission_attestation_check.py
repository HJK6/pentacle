#!/usr/bin/env python3
"""Verify changed base-published admission attestations for the FD relay.

Exit 0 accepts; 1 refuses invalid or rejected attestations; 2 means required
Git or private report-reader evidence is unavailable. No public writes occur.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

from admission_fingerprint import javascript_fingerprint, python_fingerprint
from admission_policy import identity, manifest_entry, valid_record

RECORDS = 'scripts/test-admission-records.json'
SUITES = {'daemon': 'services/chat-stream-v2', 'cli': 'services/agent-orch', 'shared': 'services/_shared'}


class Refusal(Exception):
    pass


class Unavailable(Exception):
    pass


def verify_changed(previous, proposed, get_report, fingerprint_at_target, load_manifest):
    for key, row in proposed.items():
        if previous.get(key) == row:
            continue
        if not isinstance(row, dict) or key != identity(str(row.get('suite', '')), str(row.get('test_id', ''))):
            raise Refusal('invalid attestation identity')
        platforms = row.get('platforms')
        if (not isinstance(platforms, list) or not platforms or
                any(not valid_record(row, row.get('suite'), row.get('test_id'), row.get('fingerprint'), p,
                                     row.get('restoration_of', [])) for p in platforms)):
            raise Refusal('incomplete admission attestation')
        if row.get('suite') not in (*SUITES, 'javascript'):
            raise Refusal('unsupported admission suite')
        raw = load_manifest(row['evidence_manifest'])
        if hashlib.sha256(raw).hexdigest() != row['evidence_manifest_sha256']:
            raise Refusal('admission evidence manifest digest mismatch')
        try:
            manifest = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise Refusal('admission evidence manifest invalid') from exc
        if (not isinstance(manifest, dict) or manifest.get('version') != 1
                or not isinstance(manifest.get('records'), dict)
                or manifest['records'].get(key) != manifest_entry(row)):
            raise Refusal('attestation missing from reviewed evidence manifest')
        report = get_report(row['qa_report_id'])
        if report.get('type') != 'work.report.get.ok':
            if report.get('error_code') == 'not_found':
                raise Refusal('QA report not found')
            raise Unavailable('QA report unavailable')
        review = report.get('qa_review')
        if report.get('qa_verdict') is None or not isinstance(review, dict):
            raise Unavailable('qualified QA report projection unavailable')
        if (report.get('report_id') != row['qa_report_id'] or report.get('status') != 'done'
                or report.get('qa_verdict') != 'accept' or report.get('target_sha') != row['qa_target_sha']
                or review.get('candidate_identity') != row['qa_target_sha']
                or not isinstance(review.get('reviewed_scope'), str) or not review['reviewed_scope'].strip()
                or type(review.get('commission_msg_id')) is not int
                or review['commission_msg_id'] < 0
                or review.get('gate_evidence_digest') != row['evidence_manifest_sha256']):
            raise Refusal('QA acceptance or evidence binding mismatch')
        if fingerprint_at_target(row) != row['fingerprint']:
            raise Refusal('test and fixture fingerprint differs from QA target')


def _git(root, *args):
    try:
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unavailable('Git object unavailable') from exc


def _records(root, ref):
    names = _git(root, 'ls-tree', '-r', '--name-only', ref, '--', RECORDS).decode().splitlines()
    if not names:
        return {}
    try:
        data = json.loads(_git(root, 'show', ref + ':' + RECORDS))
    except (ValueError, UnicodeError) as exc:
        raise Refusal('admission attestation file invalid') from exc
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('records'), dict):
        raise Refusal('admission attestation file invalid')
    return data['records']


def _report(report_id, command):
    try:
        proc = subprocess.run([command, 'report', 'get', report_id, '--json'],
                              capture_output=True, text=True, timeout=25, check=False)
        response = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Unavailable('QA report reader unavailable') from exc
    if proc.returncode and response.get('error_code') != 'not_found':
        raise Unavailable('QA report reader unavailable')
    return response


def check(root, base, tip, command='agent-orch'):
    if not all(re.fullmatch(r'[a-f0-9]{40}', value or '') for value in (base, tip)):
        raise Refusal('full Git base and tip are required')
    root = Path(root).resolve()
    try:
        subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', base, tip],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            raise Refusal('base is not an ancestor of tip') from exc
        raise Unavailable('Git ancestry unavailable') from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unavailable('Git ancestry unavailable') from exc
    previous, proposed = _records(root, base), _records(root, tip)
    cache = {}
    with tempfile.TemporaryDirectory(prefix='admission-review-') as temporary:
        def fingerprint(row):
            target = row['qa_target_sha']
            if target not in cache:
                snapshot = Path(temporary) / target
                snapshot.mkdir()
                archive = Path(temporary) / (target + '.tar')
                try:
                    with archive.open('wb') as out:
                        subprocess.run(['git', '-C', str(root), 'archive', target], stdout=out,
                                       stderr=subprocess.PIPE, timeout=120, check=True)
                    with tarfile.open(archive) as tar:
                        tar.extractall(snapshot, filter='data')
                except (OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
                    raise Unavailable('QA target source unavailable') from exc
                cache[target] = snapshot
            try:
                if row['suite'] == 'javascript':
                    return javascript_fingerprint(cache[target], row['test_id'])
                return python_fingerprint(cache[target] / SUITES[row['suite']], row['test_id'])
            except (OSError, ValueError, SyntaxError) as exc:
                raise Refusal('test or fixture missing at QA target') from exc
        def load_manifest(path):
            size = int(_git(root, 'cat-file', '-s', tip + ':' + path))
            if size > 1_000_000:
                raise Refusal('admission evidence manifest too large')
            return _git(root, 'show', tip + ':' + path)
        verify_changed(previous, proposed, lambda rid: _report(rid, command), fingerprint, load_manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--tip', required=True)
    parser.add_argument('--agent-orch', default='agent-orch')
    args = parser.parse_args(argv)
    try:
        check(args.root, args.base, args.tip, args.agent_orch)
    except Refusal as exc:
        print(json.dumps({'status': 'refuse', 'reason': str(exc)}))
        return 1
    except (Unavailable, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'error', 'reason': type(exc).__name__}))
        return 2
    print(json.dumps({'status': 'accept', 'base': args.base, 'tip': args.tip}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
