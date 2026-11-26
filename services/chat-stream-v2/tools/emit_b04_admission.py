#!/usr/bin/env python3
"""Print one proposed B04 admission block from two Git commits; never edit either tree."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

import daemon_b04_gate as gate


def emit_block(repo, base, head, spec_id, spec_review_id, reason):
    gate.require_signature_python()

    def git(*args):
        return subprocess.check_output(['git', '-C', repo, *args])

    def source(rev, path):
        if not git('ls-tree', '-z', rev, '--', path):
            return ''
        return git('show', f'{rev}:{path}').decode('utf-8')

    base, head = (git('rev-parse', '--verify', '--end-of-options', rev + '^{commit}').decode().strip()
                  for rev in (base, head))
    git('merge-base', '--is-ancestor', base, head)
    git('cat-file', '-e', gate.FROZEN + '^{commit}')
    path = gate.ADMISSIONS_PATH.relative_to(gate.ROOT).as_posix()
    blocks = json.loads(source(head, path) or '[]')
    gate._admission_counts(blocks)
    existing = gate._admission_counts([block for block in blocks if block['spec_id'] != spec_id])
    scopes = []
    files = git('diff', '--no-renames', '--name-only', '-z', base, head).decode().split('\0')
    for path in sorted(filter(gate._production_path, files)):
        old = gate._primitive_sites(source(gate.FROZEN, path), path)
        merged = gate._primitive_sites(source(base, path), path)
        eligible = set()
        current = gate._primitive_sites(source(head, path), path, admissible=eligible)
        grouped = {}
        counts = existing.get(path, Counter())
        for site, count in sorted(current.items()):
            scope, kind, signature = site
            digest = hashlib.sha256(signature.encode('utf-8')).hexdigest()
            covered = old[site] + counts[scope, kind, digest]
            if count <= max(covered, merged[site]):
                continue
            if site not in eligible or kind not in gate.ADMITTABLE_KINDS:
                raise ValueError(f'non-admittable B04 site: {path} {scope} {kind} {digest}')
            grouped.setdefault(scope, []).append({'kind': kind, 'sha256': digest, 'count': count-covered})
        scopes.extend({'path': path, 'scope': scope, 'nodes': nodes} for scope, nodes in grouped.items())
    if not scopes:
        raise ValueError('no new B04 sites require an admission')
    block = dict(spec_id=spec_id, spec_review_id=spec_review_id, reason=reason, scopes=scopes)
    block['sha256'] = gate.admission_digest(block)
    gate._admission_counts([block])
    return block


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    for flag in ('base', 'head', 'spec-id', 'spec-review-id', 'reason'):
        parser.add_argument('--' + flag, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(emit_block(**vars(args)), indent=2, ensure_ascii=False))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'{exc}\n')
