"""Upsert quarantine decisions before proposing selector changes."""
from __future__ import annotations

import hashlib
import fcntl
import json
from pathlib import Path

from .quarantine_transition import _save
from .reliability_work import item


class DecisionStore:
    def __init__(self, work_root, register_path, publisher):
        self.work_root = Path(work_root)
        self.register_path = Path(register_path)
        self.publisher = publisher

    def upsert(self, test, occurrences, restoration=None):
        with self.register_path.with_name(self.register_path.name + ".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._upsert(test, occurrences, restoration)

    def _upsert(self, test, occurrences, restoration):
        if not isinstance(test, str) or not test or "::" not in test:
            raise ValueError("exact test identity required")
        ids = [row.get("id") for row in occurrences]
        if len(set(ids)) < 2 or any(not isinstance(oid, str) or not oid for oid in ids):
            raise ValueError("two distinct occurrences required")
        register = json.loads(self.register_path.read_text())
        matches = [record for record in register["records"] if record.get("test") == test]
        if len(matches) > 1:
            raise ValueError("ambiguous quarantine decision")
        if matches:
            record = matches[0]
            found = item(self.work_root, record["item"])
            if found["status"] in ("completed", "deprecated"):
                if not restoration:
                    raise ValueError("decision requires verified recurrence")
                self.publisher.reopen(record["item"], test, restoration)
                found = item(self.work_root, record["item"])
                if found["status"] in ("completed", "deprecated"):
                    raise ValueError("decision reopening incomplete")
            missing = [row for row in occurrences if row["id"] not in record.get("occurrences", [])]
        else:
            name = "pentacle__flaky_" + hashlib.sha256(test.encode()).hexdigest()[:24]
            # The publisher uses a stable create request ID, so a lost response
            # after creation is recovered without creating a second item.
            self.publisher.create(name, test)
            found = item(self.work_root, name)
            if found["status"] in ("completed", "deprecated"):
                raise ValueError("decision requires verified recurrence")
            record = {"item": name, "cause": name, "test": test,
                      "first_seen": min(row["created"] for row in occurrences), "occurrences": []}
            missing = occurrences
        if missing:
            # Attach before publishing the register; on partial failure this
            # operation is repeated under the same stable occurrence IDs.
            self.publisher.attach(record["item"], test, missing)
            record["occurrences"] = list(dict.fromkeys([*record.get("occurrences", []), *ids]))
            if not matches:
                register["records"].append(record)
            _save(self.register_path, register)
        return {"item": record["item"], "owner": found["owner"],
                "coverage": "Blocking execution of " + test}


class MemoryPublisher:
    """Publish decisions with the existing revision-checked memory client."""
    def __init__(self, memory_root, owner, epic, machine, submit=None, requesting_stream_id=None):
        import subprocess
        import sys
        self.root = Path(memory_root)
        self.owner, self.epic, self.machine = owner, epic, machine
        self.requesting_stream_id = requesting_stream_id
        if submit is None:
            def submit(request):
                proc = subprocess.run([sys.executable, str(self.root / 'scripts/memory_client.py'), 'apply'],
                                      input=json.dumps(request), text=True, capture_output=True,
                                      cwd=self.root, timeout=980, check=False)
                try:
                    return json.loads(proc.stdout)
                except ValueError as exc:
                    raise ValueError('memory publisher response unavailable') from exc
        self.submit = submit

    def reopen(self, item_id, test, restoration):
        self.reactivate(item_id, restoration['sha256'],
                        'A verified restored test has recurring failures.',
                        'Resolve the new quarantine recurrence.', legacy=True)

    def reactivate(self, item_id, occurrence, reason, next_action, *, legacy=False):
        if not self.requesting_stream_id:
            raise ValueError('recurrence publisher identity unavailable')
        self._move(item_id, 'backlog', occurrence, legacy=legacy, reason=reason, next_action=next_action,
                   requesting_stream_id=self.requesting_stream_id)

    def supersede(self, item_id, successor, closure_report):
        self._move(item_id, 'deprecated', successor, closure={'reference': closure_report},
                   reason='The same cause is tracked by ' + successor + '.')

    def _move(self, item_id, status, evidence_id, *, legacy=False, **fields):
        for _ in range(3):
            folders = list((self.root / 'work').glob('*/' + item_id))
            if len(folders) != 1:
                raise ValueError('decision unavailable or ambiguous')
            folder = folders[0]
            terminal = folder.parent.name in ('completed', 'deprecated')
            if (status == 'backlog' and not terminal) or (status == 'deprecated' and terminal):
                return
            paths = [(folder / name).relative_to(self.root).as_posix() for name in ('spec.md', 'summary.md')]
            reads = [self.submit({'operation': 'read', 'path': path}) for path in paths]
            if any(not value.get('ok') for value in reads):
                raise ValueError('decision revision unavailable')
            expected = {path: value['revision'] for path, value in zip(paths, reads)}
            identity = ('reopen', item_id) if legacy else ('move', item_id, status)
            request = {'operation': 'move', 'item_id': 'spec_' + item_id, 'status': status,
                       'request_id': self._request_id(*identity, evidence_id, *expected.values()),
                       'expected': expected, **fields}
            result = self.submit(request)
            if result.get('ok'):
                return
            if result.get('code') != 'REVISION_CONFLICT':
                raise ValueError('decision move incomplete: ' + str(result.get('code', 'unknown')))
        raise ValueError('decision move remained conflicted')

    @staticmethod
    def _request_id(*parts):
        import uuid
        return str(uuid.uuid5(uuid.NAMESPACE_URL, 'pentacle-quarantine:' + ':'.join(parts)))

    def create(self, item_id, test):
        if not item_id.startswith('pentacle__flaky_'):
            raise ValueError('invalid generated decision ID')
        self.create_item(item_id, 'Flaky test decision', 'Resolve a confirmed flaky test and its coverage.')

    def create_item(self, item_id, title, summary, *, owner=None):
        if not item_id.startswith(('pentacle__flaky_', 'pentacle__scorecard_')):
            raise ValueError('invalid generated work ID')
        topic = item_id.removeprefix('pentacle__')
        request = {'operation': 'create', 'request_id': self._request_id('create', item_id),
                   'metadata': {'repo': 'pentacle', 'topic': topic, 'title': title,
                                'summary': summary, 'kind': 'defect', 'status': 'backlog',
                                'machine': self.machine, 'owner': owner or self.owner, 'epic': self.epic}}
        result = self.submit(request)
        if not result.get('ok'):
            raise ValueError('decision creation incomplete: ' + str(result.get('code', 'unknown')))

    def attach(self, item_id, test, occurrences):
        self.attach_observations(item_id, 'Quarantine observation',
                                 [{'test': test, **row} for row in occurrences], test=test)

    def attach_observations(self, item_id, label, occurrences, *, test=None, next_action=None):
        path = self.spec_path(item_id)
        marker = f'- {label}: '
        for _ in range(3):
            current = self.submit({'operation': 'read', 'path': path})
            if not current.get('ok'):
                raise ValueError('decision read incomplete')
            content = current['content']
            parts = content.split('---', 2)
            if len(parts) != 3 or parts[0]:
                raise ValueError('invalid decision spec')
            body = parts[2].lstrip('\n')
            known = set()
            for line in body.splitlines():
                if line.startswith(marker):
                    observation = json.loads(line[len(marker):])
                    if test is not None and observation.get('test') != test:
                        raise ValueError('decision test identity mismatch')
                    known.add(observation['id'])
            additions = [row for row in occurrences if row['id'] not in known]
            if not additions:
                return
            lines = [marker + json.dumps(row, sort_keys=True, separators=(',', ':'))
                     for row in additions]
            if '## Tracking\n' not in body:
                raise ValueError('decision tracking section missing')
            body = body.replace('## Tracking\n', '## Tracking\n\n' + '\n'.join(lines) + '\n', 1)
            rev = current['revision']
            digest = hashlib.sha256(body.encode()).hexdigest()
            expected = {path: rev}
            documents = {path: {'body': body}}
            identity = ['attach', path, rev, digest]
            if next_action:
                summary = str(Path(path).with_name('summary.md'))
                current_summary = self.submit({'operation': 'read', 'path': summary})
                if not current_summary.get('ok') or not current_summary.get('content'):
                    raise ValueError('work summary read incomplete')
                summary_body = current_summary['content'].split('---', 2)[2].lstrip('\n')
                default = '**Next action** — Gather requirements with the user and fill the spec.'
                if default in summary_body.splitlines():
                    summary_body = summary_body.replace(default, '**Next action** — ' + next_action, 1)
                    expected[summary] = current_summary['revision']
                    documents[summary] = {'body': summary_body}
                    identity.extend((expected[summary], hashlib.sha256(summary_body.encode()).hexdigest()))
            request = {'operation': 'update', 'request_id': self._request_id(*identity),
                       'expected': expected, 'documents': documents}
            result = self.submit(request)
            if result.get('ok'):
                if label == 'Quarantine observation':
                    return
                readback = self.submit({'operation': 'read', 'path': path})
                if readback.get('ok') and all(line in readback['content'].splitlines() for line in lines):
                    return
                raise ValueError('decision attachment readback incomplete')
            if result.get('code') != 'REVISION_CONFLICT':
                raise ValueError('decision attachment incomplete: ' + str(result.get('code', 'unknown')))
        raise ValueError('decision attachment remained conflicted')

    def spec_path(self, item_id):
        matches = list((self.root / 'work').glob('*/' + item_id + '/spec.md'))
        if len(matches) != 1:
            raise ValueError('decision spec unavailable or ambiguous')
        return matches[0].relative_to(self.root).as_posix()

    def observations(self, item_id, label):
        current = self.submit({'operation': 'read', 'path': self.spec_path(item_id)})
        if not current.get('ok'):
            raise ValueError('decision read incomplete')
        marker = f'- {label}: '
        return [json.loads(line[len(marker):]) for line in current['content'].splitlines()
                if line.startswith(marker)]
