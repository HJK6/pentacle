"""Read-only admission decisions shared by local and hosted gates."""
from __future__ import annotations

import hashlib
import json
import re

SHA256 = re.compile(r'^[a-f0-9]{64}$')
SHA = re.compile(r'^[a-f0-9]{40}$')


def identity(suite, test):
    return hashlib.sha256((suite + '\0' + test).encode()).hexdigest()


def manifest_entry(record):
    fields = ('test_id', 'suite', 'fingerprint', 'platforms', 'journey', 'boundary',
              'failing_receipt', 'failing_sha256', 'passing_receipt', 'passing_sha256',
              'restoration_of')
    return {field: record[field] for field in fields if field in record}


def manifest_digest(record):
    payload = manifest_entry(record)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def valid_record(record, suite, test, fingerprint, platform, restorations):
    if not isinstance(record, dict) or record.get('test_id') != test or record.get('suite') != suite:
        return False
    if record.get('fingerprint') != fingerprint or not SHA256.fullmatch(str(fingerprint)):
        return False
    platforms = record.get('platforms')
    if (not isinstance(platforms, list) or not platforms
            or any(not isinstance(p, str) or p not in ('linux', 'darwin') for p in platforms)
            or len(set(platforms)) != len(platforms) or platform not in platforms):
        return False
    fields = ('journey', 'boundary', 'failing_receipt', 'passing_receipt', 'qa_report_id', 'evidence_manifest')
    if any(not isinstance(record.get(k), str) or not record[k].strip() for k in fields):
        return False
    if (not SHA256.fullmatch(str(record.get('failing_sha256', '')))
            or not SHA256.fullmatch(str(record.get('passing_sha256', '')))
            or not SHA256.fullmatch(str(record.get('evidence_manifest_sha256', '')))
            or not re.fullmatch(r'scripts/test-admission-evidence/[a-f0-9]{64}\.json', record['evidence_manifest'])
            or record['evidence_manifest'] != 'scripts/test-admission-evidence/' + record['evidence_manifest_sha256'] + '.json'
            or not SHA.fullmatch(str(record.get('qa_target_sha', '')))):
        return False
    supplied = record.get('restoration_of', [])
    return isinstance(supplied, list) and all(isinstance(x, str) and x for x in supplied) and sorted(supplied) == sorted(restorations)


def admission(suite, test, fingerprint, platform, legacy, records, quarantine):
    active = quarantine['nonblocking_tests']
    history = quarantine['history']
    if (not isinstance(active, list) or not isinstance(history, dict)
            or any(not isinstance(v, dict) or not isinstance(v.get('activation'), str)
                   or not isinstance(v.get('file'), str) for v in history.values())):
        raise ValueError('quarantine history unavailable')
    identities = {suite + '::' + test}
    if suite == 'daemon':
        identities.add(test)  # adopted legacy identities predate suite prefixes
    if suite != 'javascript' and any(value in active for value in identities):
        return 'quarantine', 'active quarantine'
    active_ids = {hashlib.sha256(name.encode()).hexdigest() for name in active}
    own = hashlib.sha256(test.encode()).hexdigest()
    if suite == 'javascript':
        file_hash = own
        restorations = [v['activation'] for k, v in history.items()
                        if v['file'] == file_hash and k not in active_ids]
    else:
        prior_keys = {hashlib.sha256(value.encode()).hexdigest() for value in identities}
        restorations = [history[key]['activation'] for key in prior_keys if key in history and key not in active_ids]
    key = identity(suite, test)
    if suite == 'javascript':
        grandfather = legacy['javascript'].get(key)
    else:
        grandfather = legacy['python'].get(suite, {}).get(platform, {}).get(key)
    if not restorations and grandfather == fingerprint:
        return 'blocking', 'grandfathered'
    if valid_record(records.get(key), suite, test, fingerprint, platform, restorations):
        return 'blocking', 'reviewed admission'
    return 'advisory', 'missing, stale, incomplete or unreviewed admission'
