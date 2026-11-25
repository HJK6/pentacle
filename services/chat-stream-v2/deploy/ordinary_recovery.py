"""Operation-scoped ordinary recovery. Hashes bind consistency, not same-uid authority."""
from __future__ import annotations

import contextvars
import base64
import json
import copy
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import time
import tempfile
import uuid
from datetime import datetime, timezone

from deploy import coordinated as c

ACTIVE = contextvars.ContextVar('ordinary_recovery_writer', default=None)
SCHEMA = 'pentacle.ordinary-recovery.v1'
MANUAL_SCHEMA = 'pentacle.ordinary-manual-recovery.v1'
SUPERSEDED_SCHEMA = 'pentacle.ordinary-superseded.v1'
MANUAL_OUTCOME = 'manual_recovered_serving_target'
MANUAL_SEQUENCE = 9
MANUAL_PROOF_SECONDS = 30
MANUAL_GO_SECONDS = 600
MANUAL_LISTENER = '127.0.0.1:7791'
# Exact incident pins prevent this administrative entry from admitting another operation.
MANUAL_LEGACY = {
    'operation': 'ffbf544888944aa895e1c40065361a85',
    'bundle_sha256': '2651d5f472f7808542c9e80051f9d12e3188a0734b235c30c88edf0e49f27a54',
    'bundle_bytes_sha256': '77f8fff57aa1032543266c9270e6a8c6f2aac8d3fadfc2891f1ecc70858fc1d1',
    'pointer_bytes_sha256': 'f862bbc1fbcd14a9b17c9ba0e0f0d76bddbae064790d6d2f8bb1bc8c4ddf4e3e',
    'journal_sha256': 'abf358ca4015d4d11f18dd2f2f99b83c61a3dcfe5fea8760a7bd2f3ed6582f5e',
    'journal_bytes_sha256': '8e408853b96a47823a7769bb25586b780040a6a7b495f7f963b1db88416c7c70',
    'operator_receipt_sha256': '2cab705873e80c17a50ddd7f813788ffd58699a73d9af6e69c6ba9cba4b285d7',
}
TERMINAL = {'qualified', 'restored_unqualified', 'aborted'}
PHASES = {'forward', 'serving_unqualified', 'recovering', 'restoring', 'starting',
          'recovery_required', *TERMINAL}


def file_state(path):
    path = Path(path)
    if path.is_symlink() or path.resolve() != path.absolute():
        raise c.Invalid('recovery path symlink refused')
    if not path.exists():
        return {'sha256': None, 'mode': None}
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o7000:
        raise c.Invalid('recovery file type/owner/mode refused')
    return {'sha256': c.file_digest(path), 'mode': stat.S_IMODE(info.st_mode)}


def _logs_postdate_boot(items, birth):
    try:
        started = datetime.strptime(birth, '%a %b %d %H:%M:%S %Y').replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return False
    # The process birth probe has whole-second precision.
    for item in items:
        info = Path(item['path']).stat()
        if ((info.st_dev, info.st_ino) != (item['device'], item['inode'])
                or getattr(info, 'st_birthtime', 0) < started + 1):
            return False
    return True


def sealed_write(path, value):
    value = copy.deepcopy(value)
    value['sha256'] = c.digest(value)
    c.atomic_json(path, value)
    return value


def sealed_read(path):
    value = c.read_json(path)
    body = dict(value); seal = body.pop('sha256', None)
    if seal != c.digest(body):
        raise c.Invalid('ordinary recovery integrity mismatch')
    return value


def _disposition_path(repo, folder):
    # Key on the bundle directory, so changing the journal operation cannot hide a disposition.
    return c.state_root(repo) / ('ordinary-superseded-' + c.digest(str(folder)) + '.json')


def _failed_snapshot(repo, folder):
    pointer = c.private_file(c.state_root(repo) / 'ordinary-active.json').read_bytes()
    bundle = c.private_file(folder / 'bundle.json').read_bytes()
    journal = c.private_file(folder / 'journal.json').read_bytes()
    b, j = c.strict_json(bundle), c.strict_json(journal)
    for value in (b, j):
        if value['sha256'] != c.digest({k: v for k, v in value.items() if k != 'sha256'}):
            raise c.Invalid('supersession integrity mismatch')
    if (b['schema'] != SCHEMA or j['schema'] != SCHEMA or b['folder'] != str(folder)
            or b['repo'] != str(repo) or j['operation'] != b['operation']
            or not re.fullmatch('[0-9a-f]{32}', j['operation'])
            or c.strict_json(pointer) != {'operation': j['operation'], 'bundle': str(folder)}
            or j['bundle_sha256'] != b['sha256'] or j['phase'] != 'recovery_required'
            or type(j['sequence']) is not int or j['sequence'] < 1):
        raise c.Invalid('supersession requires the active failed operation')
    pins = {'operation': j['operation'], 'sequence': j['sequence'],
            'pointer_bytes_sha256': hashlib.sha256(pointer).hexdigest(),
            'journal_bytes_sha256': hashlib.sha256(journal).hexdigest(),
            'bundle_bytes_sha256': hashlib.sha256(bundle).hexdigest()}
    return pins, b, j


def _successor_generation(switch, bundle, journal):
    live = switch['live']
    pid, birth, sha = live['pid'], live['birth'], live['sha']
    if (switch['outcome'] != 'serving' or switch['prior'] != bundle['target_sha']
            or not re.fullmatch('[0-9a-f]{40}', sha) or switch['target'] != sha
            or sha in (bundle['prior_sha'], bundle['target_sha'])
            or type(pid) is not int or pid <= 0 or not isinstance(birth, str) or not birth.strip()
            or type(switch['old_pid']) is not int or switch['old_pid'] <= 0
            or pid in (switch['old_pid'], journal['process']['pid']) or birth == journal['process']['birth']):
        raise c.Invalid('successor receipt does not establish a distinct serving generation')
    return {'pid': pid, 'birth': birth, 'sha': sha}


def _verify_superseded_payloads(folder, bundle):
    # The enrolled manifest covers the saved file contents and exact Git bundle.
    for name, row in bundle['preimages'].items():
        if not name or Path(name).name != name or name in ('.', '..'):
            raise c.Invalid('supersession preimage name is invalid')
        saved = folder / (name + '.before')
        if row['sha256'] is None:
            if os.path.lexists(saved):
                raise c.Invalid('supersession preimage absence changed')
        elif c.file_digest(c.private_file(saved)) != row['sha256']:
            raise c.Invalid('supersession preimage changed')
    if c.file_digest(c.private_file(folder / 'source.bundle')) != bundle['source']['sha256']:
        raise c.Invalid('supersession source bundle changed')


def _recorded_supersession(repo, folder):
    path = _disposition_path(repo, folder)
    if not os.path.lexists(path):
        return None
    record = sealed_read(path)
    failed, bundle, journal = _failed_snapshot(repo, folder)
    _verify_superseded_payloads(folder, bundle)
    successor = _successor_generation(record['switch'], bundle, journal)
    serving = record['after']['serving']
    authority, proof = record['authority'], record['authenticated_readback']
    if (record['schema'] != SUPERSEDED_SCHEMA or record['outcome'] != 'superseded'
            or c.canonical(record['before']) != c.canonical(record['after'])
            or c.canonical(record['after']['failed']) != c.canonical(failed)
            or c.canonical(serving['process']) != c.canonical({'loaded': True, **successor})
            or serving['checkout'] != str(repo) or serving['files']['stamp'] not in journal['allowed_files']['stamp']
            or proof['outcome'] != 'passed' or proof['verb'] != 'ledger_get' or not proof['request_id']
            or proof['runtime_sha'] != successor['sha'] or proof['go_tell_id'] != authority['go_tell_id']
            or not re.fullmatch('[0-9a-f]{64}', authority['authorization_sha256'])
            or not re.fullmatch('[0-9a-f]{64}', authority['successor_receipt_sha256'])):
        raise c.Invalid('supersession disposition no longer matches the failed operation')
    return record


def fence(repo, *, recovering=None):
    path = c.state_root(repo) / 'ordinary-active.json'
    if not path.exists():
        return
    pointer = c.read_json(path)
    folder = Path(pointer['bundle'])
    journal = sealed_read(folder / 'journal.json')
    if journal['operation'] != pointer['operation']:
        raise c.Invalid('ordinary recovery active operation mismatch')
    if _recorded_supersession(repo, folder) is not None:
        if recovering is not None and Path(recovering).resolve() != folder:
            raise c.Invalid('ordinary recovery active bundle mismatch')
        return
    if journal['phase'] not in TERMINAL and (recovering is None or Path(recovering).resolve() != folder):
        raise c.Invalid('unfinished ordinary operation requires its authorized recovery')


def _compare_process(adapter, expected, observed):
    """Compare ordinary identities, accepting only independently proved turnover."""
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        raise c.Invalid('ordinary process observation malformed')
    if {k:v for k,v in expected.items() if k != 'provenance'} != {k:v for k,v in observed.items() if k != 'provenance'}:
        raise c.Invalid('ordinary primary identity drift')
    def identities(value):
        p = value.get('provenance')
        if (not isinstance(p, dict) or set(p) != {'launchd_answered','pid','processes'}
                or p['launchd_answered'] is not True or p['pid'] != value.get('pid')
                or not isinstance(p['processes'], list)):
            raise c.Invalid('ordinary process census malformed or unavailable')
        rows = {}
        for row in p['processes']:
            if (not isinstance(row, dict) or set(row) != {'pid','start'}
                    or type(row['pid']) is not int or row['pid'] <= 0
                    or not isinstance(row['start'], str) or not row['start'].strip()
                    or row['pid'] in rows):
                raise c.Invalid('ordinary process census malformed or duplicate')
            rows[row['pid']] = row['start']
        if value.get('pid') is not None and rows.get(value['pid']) != value.get('birth'):
            raise c.Invalid('ordinary primary absent from census')
        return rows
    before, after = identities(expected), identities(observed)
    if any(before[pid] != after[pid] for pid in before.keys() & after.keys()):
        raise c.Invalid('ordinary process PID reused')
    departed = [{'pid':pid, 'start':before[pid]} for pid in sorted(before.keys() - after.keys())]
    added = [{'pid':pid, 'start':after[pid]} for pid in sorted(after.keys() - before.keys())]
    if any(not adapter.process_absent(row['pid'], row['start']) for row in departed):
        raise c.Invalid('ordinary departed helper exit unproven')
    surviving = [{'pid':pid, 'start':after[pid]} for pid in sorted(after) if pid != observed.get('pid')]
    if surviving and not adapter.authenticate_new_processes(observed, surviving):
        raise c.Invalid('ordinary helper ownership unproven')
    return {'previous': [{'pid':pid, 'start':before[pid]} for pid in sorted(before)],
            'observed': [{'pid':pid, 'start':after[pid]} for pid in sorted(after)],
            'departed_absent': departed, 'added_owned': added}


def compare_process(adapter, expected, observed):
    try:
        return _compare_process(adapter, expected, observed)
    except c.Invalid as exc:
        def safe_rows(value):
            if not isinstance(value, dict): return []
            provenance = value.get('provenance')
            if not isinstance(provenance, dict) or not isinstance(provenance.get('processes'), list): return []
            safe = []
            for row in provenance['processes'][:256]:
                if not isinstance(row, dict) or type(row.get('pid')) is not int or row['pid'] <= 0: continue
                start = row.get('start')
                safe.append({'pid':row['pid'], 'birth_sha256':hashlib.sha256(start.encode()).hexdigest()
                             if isinstance(start,str) else None})
            return safe
        detail = {'previous':safe_rows(expected), 'observed':safe_rows(observed)}
        raise c.Invalid(str(exc) + ' ' + json.dumps(detail, separators=(',',':'))) from exc


class Bundle:
    def __init__(self, adapter, folder):
        self.a = adapter
        self.folder = Path(folder).absolute()
        self.bundle = self.journal = None

    def require_open_recovery(self):
        if os.path.lexists(_disposition_path(self.a.repo, self.folder)):
            raise c.Invalid('superseded operation is terminal')

    def persist(self, **updates):
        self.require_open_recovery()
        if sealed_read(self.folder/'journal.json') != self.journal:
            raise c.Invalid('ordinary recovery journal changed during operation')
        if getattr(self, 'controller_grant', None):
            updates.update(controller_used=self.controller_grant['replacement_controller_sha'],
                           controller_authorization_sha256=self.controller_grant['authorization_sha256'])
        body = {k:v for k,v in self.journal.items() if k != 'sha256'}
        body.update(updates)
        body['sequence'] += 1
        self.journal = sealed_write(self.folder/'journal.json', body)

    @classmethod
    def prepare(cls, adapter, folder, prior, target, ancestry, accounts):
        self = cls(adapter, folder)
        if self.folder.exists() or self.folder.resolve() != self.folder:
            raise c.Invalid('ordinary recovery requires a new canonical operation directory')
        fence(adapter.repo)
        metadata = adapter.capture(prior, target, ancestry, accounts)
        paths = adapter.paths(metadata)
        if len(set(map(str, paths.values()))) != len(paths):
            raise c.Invalid('ordinary recovery capture paths collide')
        if self.folder.is_relative_to(adapter.repo) or any(self.folder == p or self.folder.is_relative_to(p) or p.is_relative_to(self.folder) for p in paths.values()):
            raise c.Invalid('ordinary recovery bundle overlaps captured source/files')
        self.folder.mkdir(mode=0o700)
        operation = uuid.uuid4().hex
        originals = {}
        for name, path in paths.items():
            state = file_state(path)
            originals[name] = {'path':str(path), **state}
            if state['sha256'] is not None:
                c.atomic_bytes(self.folder/(name+'.before'), path.read_bytes())
        source = adapter.save_source(self.folder, prior, target)
        self.bundle = sealed_write(self.folder/'bundle.json', {
            'schema':SCHEMA, 'operation':operation, 'folder':str(self.folder),
            'repo':str(adapter.repo), 'prior_sha':prior, 'target_sha':target,
            'metadata':metadata, 'preimages':originals, 'source':source})
        current = adapter.observe(self.bundle)
        try:
            process_decision = adapter.compare_process(metadata['prior_process'], current['process'])
        except c.Invalid as exc:
            c.atomic_json(self.folder/'process-decision.json', {'outcome':'refused', 'reason':str(exc)})
            raise
        if (current['source'] != prior
                or current['collector'] != metadata['prior_collector']
                or any(current['files'][k] != {'sha256':v['sha256'],'mode':v['mode']} for k,v in originals.items())
                or any(v['sha256'] is not None and c.file_digest(self.folder/(k+'.before')) != v['sha256'] for k,v in originals.items())):
            raise c.Invalid('ordinary capture drift before enrollment')
        adapter.validate(self.bundle)
        self.journal = sealed_write(self.folder/'journal.json', {
            'schema':SCHEMA, 'operation':operation, 'bundle_sha256':self.bundle['sha256'],
            'sequence':0, 'phase':'forward', 'allowed_source':[prior,target], 'allowed_files':{
                k:[{'sha256':v['sha256'], 'mode':v['mode']}] for k,v in originals.items()},
            'process':current['process'], 'collector':current['collector'], 'result':None,
            'process_decision':process_decision})
        # Nothing may mutate until the complete bundle and journal are durable.
        c.atomic_json(c.state_root(adapter.repo)/'ordinary-active.json',
                      {'operation':operation, 'bundle':str(self.folder)})
        return self

    def load(self):
        if (not self.folder.is_dir() or self.folder.is_symlink() or self.folder.resolve() != self.folder
                or self.folder.stat().st_uid != os.getuid() or stat.S_IMODE(self.folder.stat().st_mode) != 0o700):
            raise c.Invalid('missing or insecure ordinary bundle directory')
        self.bundle = sealed_read(self.folder/'bundle.json')
        self.journal = sealed_read(self.folder/'journal.json')
        b, j = self.bundle, self.journal
        if (b.get('schema') != SCHEMA or j.get('schema') != SCHEMA
                or b.get('folder') != str(self.folder) or b.get('repo') != str(self.a.repo)
                or j.get('operation') != b.get('operation') or j.get('bundle_sha256') != b['sha256']
                or j.get('phase') not in PHASES or type(j.get('sequence')) is not int):
            raise c.Invalid('ordinary recovery schema/operation mismatch')
        paths = self.a.paths(b['metadata'])
        if set(paths) != set(b['preimages']) or set(paths) != set(j['allowed_files']):
            raise c.Invalid('ordinary recovery preimage population mismatch')
        for name, path in paths.items():
            row = b['preimages'][name]
            if str(path) != row['path']:
                raise c.Invalid('ordinary recovery destination substitution')
            file_state(path)
            if row['sha256'] is not None:
                saved = c.private_file(self.folder/(name+'.before'))
                if c.file_digest(saved) != row['sha256']:
                    raise c.Invalid('ordinary recovery preimage corrupt')
            elif (self.folder/(name+'.before')).exists() or row['mode'] is not None:
                raise c.Invalid('ordinary recovery absence mismatch')
            if not isinstance(j['allowed_files'][name], list) or not j['allowed_files'][name]:
                raise c.Invalid('ordinary recovery admitted images missing')
        self.a.validate_source(self.folder, b['source'], b['prior_sha'], b['target_sha'])
        return self

    def publish(self, path, data, mode=None):
        """Write-ahead exact image, then atomic replace. Never adopt arbitrary observed bytes."""
        path = Path(path).absolute()
        names = [k for k,v in self.bundle['preimages'].items() if v['path'] == str(path)]
        if len(names) != 1:
            raise c.Invalid('uncaptured ordinary file mutation refused')
        name = names[0]; now = file_state(path)
        if now not in self.journal['allowed_files'][name]:
            raise c.Invalid('ordinary file drift before write')
        mode = mode if mode is not None else now['mode'] if now['mode'] is not None else 0o600
        state = {'sha256':hashlib.sha256(data).hexdigest(), 'mode':mode}
        allowed = copy.deepcopy(self.journal['allowed_files'])
        allowed[name] = [now] if now == state else [now,state]
        self.persist(allowed_files=allowed)
        path.parent.mkdir(parents=True, exist_ok=True)
        c.atomic_bytes(path, data, mode=mode)
        if file_state(path) != state:
            raise c.Invalid('ordinary publication readback mismatch')
        allowed[name] = [state]
        self.persist(allowed_files=allowed)

    def observed_process(self):
        now = self.a.observe(self.bundle)
        self.persist(process=now['process'], collector=now['collector'], allowed_source=[now['source']])

    def finish(self, result):
        now = self.a.observe(self.bundle)
        if (now['source'] not in self.journal['allowed_source']
                or (result.get('serving') and now['source'] != self.bundle['target_sha'])
                or any(state not in self.journal['allowed_files'][name] for name,state in now['files'].items())
                or (result.get('serving') and any(now['process'].get(k) != self.journal['process'].get(k)
                                                for k in ('pid','birth','sha')))):
            raise c.Invalid('ordinary state drift before terminal publication')
        self.a.validate(self.bundle)
        phase = 'qualified' if result.get('disposition') == 'active and checks passed' else 'serving_unqualified'
        self.persist(phase=phase, process=now['process'], collector=now['collector'], allowed_source=[now['source']],
                     result={'disposition':result.get('disposition'), 'full_fleet_acceptance':False})

    def state(self):
        record = _recorded_supersession(self.a.repo, self.folder)
        if record is not None:
            return {'phase': 'superseded', 'disposition_sha256': record['sha256'],
                    'recorded_serving': record['after']['serving'], 'journal_sha256': self.journal['sha256']}
        now = self.a.observe(self.bundle)
        return {**now, 'journal_sha256':self.journal['sha256']}

    def record_superseded(self, authorization, successor_receipt, credential, go):
        before, b, j = _failed_snapshot(self.a.repo, self.folder)
        self.load()
        if self.bundle != b or self.journal != j:
            raise c.Invalid('supersession bundle changed during validation')
        path = _disposition_path(self.a.repo, self.folder)
        auth_bytes = c.private_file(authorization).read_bytes()
        switch_bytes = c.private_file(successor_receipt).read_bytes()
        auth, switch = c.strict_json(auth_bytes), c.strict_json(switch_bytes)
        inputs = [c.private_file(path).stat() for path in (authorization, successor_receipt, credential)]
        if len({(entry.st_dev, entry.st_ino) for entry in inputs}) != len(inputs):
            raise c.Invalid('supersession requires distinct authority, successor receipt and credential')

        expected_keys = {'schema', 'failed', 'expected_serving', 'successor_receipt_sha256',
                         'replacement_controller_sha', 'deployer_stream_id'}
        switch_hash = hashlib.sha256(switch_bytes).hexdigest()
        if (not isinstance(auth, dict) or set(auth) != expected_keys or auth['schema'] != SUPERSEDED_SCHEMA
                or c.canonical(auth['failed']) != c.canonical(before)
                or auth['successor_receipt_sha256'] != switch_hash
                or not self.a.stream or auth['deployer_stream_id'] != self.a.stream
                or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', go)):
            raise c.Invalid('supersession authority does not match the failed operation or successor receipt')
        controller = auth['replacement_controller_sha']
        if not re.fullmatch('[0-9a-f]{40}', controller) or controller == b['metadata']['controller_sha']:
            raise c.Invalid('supersession requires a separately reviewed controller')
        authority = {'authorization_sha256': hashlib.sha256(auth_bytes).hexdigest(),
                     'successor_receipt_sha256': switch_hash, 'replacement_controller_sha': controller,
                     'deployer_stream_id': self.a.stream, 'go_tell_id': go}

        generation = _successor_generation(switch, b, j)
        successor = generation['sha']

        def confirm_serving():
            self.a.validate_controller(b, controller)
            serving = self.a.observe_manual(b, successor_sha=successor)
            observed = serving['process']
            if (c.canonical(observed) != c.canonical({'loaded': True, **generation})
                    or serving['files']['stamp'] not in j['allowed_files']['stamp']
                    or c.canonical(serving) != c.canonical(auth['expected_serving'])):
                raise c.Invalid('supersession checkout, files or serving generation drift')
            return serving

        def confirm_inputs():
            _verify_superseded_payloads(self.folder, b)
            after, _, _ = _failed_snapshot(self.a.repo, self.folder)
            if (after != before or c.private_file(authorization).read_bytes() != auth_bytes
                    or c.private_file(successor_receipt).read_bytes() != switch_bytes):
                raise c.Invalid('supersession inputs changed before readback')
            return after

        def result(record):
            return {'outcome': 'superseded', 'operation': before['operation'], 'full_fleet_acceptance': False,
                    'disposition_path': str(path), 'disposition_sha256': record['sha256']}

        serving = confirm_serving()
        recorded = _recorded_supersession(self.a.repo, self.folder)
        if recorded is not None:
            if recorded['authority'] != authority or c.canonical(recorded['after']['serving']) != c.canonical(serving):
                raise c.Invalid('supersession replay differs from committed authority or generation')
            confirm_inputs()
            if sealed_read(path) != recorded:
                raise c.Invalid('supersession disposition changed during replay')
            return result(recorded)

        started = time.monotonic()
        proof = self.a.prove_manual(serving, credential, authority, confirm_serving,
                                    superseded_operation=before['operation'])
        if (proof['outcome'] != 'passed' or proof['verb'] != 'ledger_get' or not proof['request_id']
                or proof['runtime_sha'] != successor or proof['go_tell_id'] != go):
            raise c.Invalid('authenticated successor or disposition GO missing')
        after_serving = confirm_serving()
        after = confirm_inputs()
        switch_identity = {key: switch[key] for key in ('outcome', 'prior', 'target', 'old_pid')}
        switch_identity['live'] = generation
        record = {'schema': SUPERSEDED_SCHEMA, 'outcome': 'superseded',
                  'before': {'failed': before, 'serving': serving}, 'after': {'failed': after, 'serving': after_serving},
                  'switch': switch_identity, 'authority': authority, 'authenticated_readback': proof}
        # Stage and fsync before the last comparison. Link once so a retry or collision
        # cannot replace a previous decision. The transaction lock spans the readback.
        staged = path.with_name('.supersession-' + uuid.uuid4().hex)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            sealed = sealed_write(staged, record)
            confirm_serving()
            confirm_inputs()
            if time.monotonic() - started >= MANUAL_PROOF_SECONDS:
                raise c.Invalid('supersession proof expired before recording')
            os.link(staged, path)
            try:
                os.fsync(directory)
                confirm_serving()
                confirm_inputs()
                if _recorded_supersession(self.a.repo, self.folder) != sealed:
                    raise c.Invalid('supersession commit readback mismatch')
            except Exception:
                if path.exists() and path.samefile(staged):
                    path.unlink()
                    os.fsync(directory)
                raise
        finally:
            try:
                staged.unlink(missing_ok=True)
            finally:
                os.close(directory)
        return result(sealed)

    def record_manual_recovery(self, authorization, operator_receipt, credential, go):
        self.require_open_recovery()
        self.load()
        bundle, journal = self.bundle, self.journal
        if (bundle['operation'] != MANUAL_LEGACY['operation']
                or bundle['sha256'] != MANUAL_LEGACY['bundle_sha256']):
            raise c.Invalid('manual recovery is outside the enrolled legacy operation')

        pointer = c.state_root(self.a.repo)/'ordinary-active.json'
        journal_path = self.folder/'journal.json'
        pointer_bytes = c.private_file(pointer).read_bytes()
        bundle_bytes = c.private_file(self.folder/'bundle.json').read_bytes()
        journal_bytes = c.private_file(journal_path).read_bytes()
        auth_bytes = c.private_file(authorization).read_bytes()
        receipt_bytes = c.private_file(operator_receipt).read_bytes()
        auth = c.strict_json(auth_bytes)
        original = journal.get('manual_recovery_audit')
        failed_bytes = journal_bytes if original is None else base64.b64decode(original['original_journal_bytes'], validate=True)
        failed = c.strict_json(failed_bytes)
        def sha(data):
            return hashlib.sha256(data).hexdigest()

        if (c.strict_json(pointer_bytes) != {'operation': bundle['operation'], 'bundle': str(self.folder)}
                or sha(pointer_bytes) != MANUAL_LEGACY['pointer_bytes_sha256']
                or c.strict_json(bundle_bytes) != bundle or c.strict_json(journal_bytes) != journal
                or sha(bundle_bytes) != MANUAL_LEGACY['bundle_bytes_sha256']
                or sha(failed_bytes) != MANUAL_LEGACY['journal_bytes_sha256']
                or failed['sha256'] != MANUAL_LEGACY['journal_sha256']
                or failed['phase'] != 'recovery_required' or failed['sequence'] != MANUAL_SEQUENCE
                or sha(receipt_bytes) != MANUAL_LEGACY['operator_receipt_sha256']):
            raise c.Invalid('manual recovery pointer, bundle, failed journal or operator receipt mismatch')

        common = {'schema': MANUAL_SCHEMA, **MANUAL_LEGACY,
                  'failed_sequence': MANUAL_SEQUENCE,
                  'original_controller_sha': bundle['metadata']['controller_sha'],
                  'prior_sha': bundle['prior_sha'], 'target_sha': bundle['target_sha'],
                  'deployer_stream_id': self.a.stream}
        if (not isinstance(auth, dict) or set(auth) != common.keys() | {'replacement_controller_sha', 'expected_serving'}
                or c.canonical({key: auth[key] for key in common}) != c.canonical(common)):
            raise c.Invalid('manual recovery authority does not bind the exact operation and state')
        replacement = auth['replacement_controller_sha']
        if (not isinstance(replacement, str) or not re.fullmatch('[0-9a-f]{40}', replacement)
                or replacement == common['original_controller_sha']
                or not isinstance(go, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', go)):
            raise c.Invalid('manual recovery replacement controller or FD GO identity refused')
        authority = {'authorization_sha256': sha(auth_bytes), 'go_tell_id': go,
                     'original_controller_sha': common['original_controller_sha'],
                     'replacement_controller_sha': replacement,
                     'operator_receipt_sha256': common['operator_receipt_sha256'],
                     'deployer_stream_id': self.a.stream}
        self.a.validate_controller(bundle, replacement)

        def result(audit):
            return {'outcome': MANUAL_OUTCOME, 'operation': bundle['operation'], 'phase': 'aborted',
                    'full_fleet_acceptance': False, 'audit_sha256': audit['sha256']}

        def terminal(audit):
            body = {**failed, 'phase': 'aborted', 'sequence': MANUAL_SEQUENCE + 1,
                    'result': result(audit), 'manual_recovery_audit': audit}
            body.pop('sha256')
            return body

        def readback(audit):
            body = terminal(audit)
            if (c.private_file(journal_path).read_bytes() != c.canonical({**body, 'sha256': c.digest(body)}) + b"\n"
                    or c.private_file(pointer).read_bytes() != pointer_bytes
                    or c.private_file(self.folder/'bundle.json').read_bytes() != bundle_bytes):
                raise c.Invalid('manual recovery terminal readback mismatch')
            fence(self.a.repo)
            return result(audit)

        if original is not None:
            body = {k: v for k, v in original.items() if k != 'sha256'}
            if (original['sha256'] != c.digest(body) or original['authority'] != authority
                    or c.canonical(original['serving']) != c.canonical(auth['expected_serving'])
                    or original['original_failure'] != failed['result']):
                raise c.Invalid('manual recovery terminal audit or replay authority mismatch')
            return readback(original)

        if journal['phase'] != 'recovery_required':
            raise c.Invalid('manual recovery requires the original failed journal')
        if Path(credential) in (authorization, operator_receipt, pointer, journal_path):
            raise c.Invalid('manual recovery inputs must have distinct roles')
        serving = self.a.observe_manual(bundle)
        process = serving['process']
        historical = failed['process']
        if (c.canonical(serving) != c.canonical(auth['expected_serving']) or serving['source'] != bundle['target_sha']
                or process['sha'] != bundle['target_sha'] or process['loaded'] is not True
                or type(process['pid']) is not int or process['pid'] <= 0 or not process['birth']
                or process['pid'] == historical['pid'] or process['birth'] == historical['birth']
                or historical['sha'] != bundle['prior_sha']):
            raise c.Invalid('manual recovery serving identity or historical generation mismatch')
        def confirm_serving():
            if self.a.observe_manual(bundle) != serving:
                raise c.Invalid('manual recovery serving state changed before commit')

        started = time.monotonic()
        proof = self.a.prove_manual(serving, credential, authority, confirm_serving)
        if (proof.get('outcome') != 'passed' or proof.get('verb') != 'ledger_get'
                or not isinstance(proof.get('request_id'), str) or not proof['request_id']
                or proof.get('go_tell_id') != go or proof.get('runtime_sha') != serving['source']):
            raise c.Invalid('manual recovery authenticated Store readback missing')

        audit = {'schema': MANUAL_SCHEMA, 'operation': bundle['operation'],
                 'pointer_bytes_sha256': sha(pointer_bytes), 'bundle_sha256': bundle['sha256'],
                 'original_journal_bytes': base64.b64encode(failed_bytes).decode(),
                 'original_journal_bytes_sha256': sha(failed_bytes),
                 'original_failure': failed['result'],
                 'authority': authority, 'serving': serving, 'authenticated_readback': proof}
        audit['sha256'] = c.digest(audit)
        terminal_body = terminal(audit)
        self.a.validate_controller(bundle, replacement)
        confirm_serving()
        if (c.private_file(pointer).read_bytes() != pointer_bytes
                or c.private_file(self.folder/'bundle.json').read_bytes() != bundle_bytes
                or c.private_file(journal_path).read_bytes() != journal_bytes
                or c.private_file(authorization).read_bytes() != auth_bytes
                or c.private_file(operator_receipt).read_bytes() != receipt_bytes
                or time.monotonic() - started >= MANUAL_PROOF_SECONDS):
            raise c.Invalid('manual recovery state changed or authenticated readback expired before commit')

        # The original failure and its replacement audit share one atomic commit.
        sealed_write(journal_path, terminal_body)
        return readback(audit)

    def admit(self, authorization, go):
        self.require_open_recovery()
        b, j = self.bundle, self.journal
        self.a.recovery_authorization = None
        if 'manual_recovery_audit' in j:
            raise c.Invalid('manually recovered operation is terminal')
        if j['phase'] == 'qualified':
            raise c.Invalid('qualified operation is not an ordinary failure recovery')
        now = self.state()
        if now['source'] not in j['allowed_source'] or now['source'] not in (b['prior_sha'], b['target_sha']):
            raise c.Invalid('unrelated ordinary source drift')
        self.process_decision = self.a.compare_process(j['process'], now['process'])
        self.a.verify_sealed_prior(b, now['process'])
        if now['collector'] != j['collector']:
            raise c.Invalid('unrecorded ordinary collector identity')
        for name, state in now['files'].items():
            if state not in j['allowed_files'][name]:
                raise c.Invalid('unrecognized ordinary file drift')
        auth = c.read_json(c.private_file(authorization))
        replacement = auth.get('replacement_controller_sha')
        self.controller_grant = None
        if replacement is not None:
            if (b['operation'] != '08826e95f2d347f498d8caea51d4ad13'
                    or not isinstance(replacement, str) or not re.fullmatch(r'[0-9a-f]{40}', replacement)
                    or replacement == b['metadata']['controller_sha']
                    or not isinstance(auth.get('authorized_by'), str) or not auth['authorized_by'].strip()):
                raise c.Invalid('ordinary replacement controller authorization refused')
        expected = {'schema':SCHEMA, 'operation':b['operation'], 'bundle_sha256':b['sha256'],
            'controller_sha':b['metadata']['controller_sha'], 'prior_sha':b['prior_sha'],
            'target_sha':b['target_sha'], 'expected_state':now, 'go_tell_id':go}
        auth_state = auth.get('expected_state')
        if not isinstance(auth_state, dict):
            raise c.Invalid('ordinary recovery expected state malformed')
        if {k:v for k,v in auth_state.items() if k != 'process'} != {k:v for k,v in now.items() if k != 'process'}:
            raise c.Invalid('ordinary recovery authorized state drift')
        self.authorization_decision = self.a.compare_process(auth_state.get('process'), now['process'])
        expected['expected_state'] = auth_state
        extra = {'replacement_controller_sha', 'authorized_by'} if replacement is not None else set()
        if (set(auth) != {*expected, 'fd_gate_reference', *extra} or any(auth.get(k) != v for k,v in expected.items())
                or not isinstance(go,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',go)
                or not isinstance(auth['fd_gate_reference'],str) or not auth['fd_gate_reference'].strip()):
            raise c.Invalid('ordinary recovery requires exact separate FD authority/current state')
        if replacement is not None:
            self.controller_grant = {'operation': b['operation'], 'bundle_sha256': b['sha256'],
                'starting_journal_sha256': j['sha256'], 'original_controller_sha': b['metadata']['controller_sha'],
                'replacement_controller_sha': replacement, 'authorization_sha256': c.file_digest(authorization)}
            self.a.recovery_authorization = self.controller_grant
        self.a.validate(b)
        return now

    def recover(self, authorization, go):
        self.load()
        now = self.admit(authorization, go)
        if self.journal['phase'] == 'restored_unqualified':
            if self.journal.get('restored') != {k:v for k,v in now.items() if k != 'journal_sha256'}:
                raise c.Invalid('completed ordinary recovery drift')
            return self.journal['result']
        # Persist intent before stop. Unknown outcomes remain fenced, never inferred successful.
        self.persist(phase='recovering', authorization_sha256=c.file_digest(authorization), go_tell_id=go,
                     process_decision=self.process_decision, authorization_process_decision=self.authorization_decision)
        boot_proven = False
        self.a.before_stop = lambda decision: self.persist(pre_stop_process_decision=decision)
        try:
            self.a.stop(self.bundle, now, [self.process_decision, self.authorization_decision])
            stopped_identity=self.a.observe(self.bundle)
            if (stopped_identity['process'].get('pid') is not None or stopped_identity['process'].get('loaded')
                    or stopped_identity['collector'].get('pid') is not None or stopped_identity['collector'].get('loaded')):
                raise c.Invalid('ordinary stopped identity not proven')
            self.observed_process()
            self.a.validate(self.bundle)
            stopped = self.a.observe(self.bundle)
            if (stopped['files'] != now['files'] or stopped['source'] != now['source']
                    or stopped['process'].get('pid') is not None or stopped['process'].get('loaded')
                    or stopped['collector'].get('pid') is not None or stopped['collector'].get('loaded')
                    or stopped['process'].get('provenance', {}).get('processes')):
                raise c.Invalid('ordinary source/file drift while stopping')
            self.persist(phase='restoring', allowed_source=list({now['source'],self.bundle['prior_sha']}))
            self.a.restore_source(self.folder, self.bundle)
            for name, row in self.bundle['preimages'].items():
                self.a.ensure_stopped(self.bundle)
                path = Path(row['path'])
                state={'sha256':row['sha256'],'mode':row['mode']}
                allowed=copy.deepcopy(self.journal['allowed_files']); current=file_state(path)
                if current not in allowed[name]: raise c.Invalid('ordinary file drift during restore')
                allowed[name]=[current] if current == state else [current,state]
                self.persist(allowed_files=allowed)
                if row['sha256'] is None:
                    path.unlink(missing_ok=True)
                    if path.parent.exists():
                        fd=os.open(path.parent,os.O_RDONLY)
                        try: os.fsync(fd)
                        finally: os.close(fd)
                else:
                    c.atomic_bytes(path, (self.folder/(name+'.before')).read_bytes(), mode=row['mode'])
                if file_state(path) != {'sha256':row['sha256'], 'mode':row['mode']}:
                    raise c.Invalid('ordinary restored file mismatch')
                allowed[name]=[state]; self.persist(allowed_files=allowed)
            self.a.verify_restored(self.bundle)
            self.persist(phase='starting')
            identity, boot = self.a.start(self.bundle)
            observed=self.a.observe(self.bundle)
            if any(observed['process'].get(k) != identity.get(k) for k in ('pid','birth','sha')):
                raise c.Invalid('restored process identity changed after boot')
            boot_proven = True
            self.persist(process=observed['process'],collector=observed['collector'],allowed_source=[observed['source']])
            proof = self.a.prove(self.bundle, boot)
            if not isinstance(proof,dict) or proof.get('outcome') != 'passed' or proof.get('mode') != 'zero_input':
                raise c.Invalid('ordinary restoration proof is incomplete')
            self.a.verify_restored(self.bundle)
            final = self.a.observe(self.bundle)
            if any(final['process'].get(k) != identity.get(k) for k in ('pid','birth','sha')):
                raise c.Invalid('restored process identity changed during proof')
            if final['collector']['loaded'] != self.bundle['metadata']['prior_collector']['loaded']:
                raise c.Invalid('restored collector loaded state mismatch')
            result = {'outcome':'restored_unqualified', 'full_fleet_acceptance':False,
                      'runtime':identity, 'restoration':proof, 'operation':self.bundle['operation']}
            self.persist(phase='restored_unqualified', result=result, restored=final,
                         process=final['process'], collector=final['collector'])
            return result
        except Exception as exc:
            # Source/files are never restored before stop has been proven. Do not
            # signal an unknown process on exception, or stop a proven serving prior.
            result={'outcome':'recovery_required', 'prior_boot_proven':boot_proven,
                'full_fleet_acceptance':False, 'error':type(exc).__name__,
                'disposition':'active with failed checks' if boot_proven else 'target not active, restore unproven'}
            try: self.persist(phase='recovery_required', result=result)
            except OSError: pass
            return result


class OrdinaryRuntime:
    """Concrete adapter; tests replace only external process/account/boot probes."""
    def __init__(self, deployer, service, repo, runner, *, stream, boot_wait_seconds):
        from deploy.coordinated_runtime import Runtime
        self.d, self.service, self.repo, self.runner = deployer, service, Path(repo).resolve(), runner
        self.runtime = Runtime(deployer, service, self.repo, runner, stream=stream,
                               boot_wait_seconds=boot_wait_seconds)
        self.controller = self.runtime.controller
        self.stream = stream

    def git(self, *args, repo=None):
        return self.d._git(repo or self.repo, *args, runner=self.runner)

    def paths(self, metadata):
        return {k:Path(v) for k,v in {
            'stamp':str(self.d._stamp_path(self.repo,self.service)),
            'plist':str(self.d._launchd_plist_path(self.service.launchd_label)),
            'collector':str(self.d._usage_collector_plist_path()),
            'schedule':str(self.d._launchd_plist_path('com.pentacle.spawn-fleet-smoke')),
            'settings':str(self.repo/'services/_shared/spawn_defaults.local.json'),
            'profile':str(self.d._stamp_path(self.repo,self.service).with_name(self.service.name+'.test-profile.json')),
            'policy':metadata['policy_path'], 'machines':metadata['machines_path']}.items()}

    def job(self, label):
        state=self.d._launchd_state(label,self.runner)
        if not state.answered: raise c.Invalid('ordinary launchd state unavailable')
        birth=self.d._process_start(state.pid,self.runner) if state.pid else None
        if state.pid and not birth: raise c.Invalid('ordinary process birth unavailable')
        return {'loaded':state.loaded,'pid':state.pid,'birth':birth}

    def process(self, shas=None):
        value=self.job(self.service.launchd_label)
        if value['pid']:
            confirmed=False
            for sha in shas or [self.git('rev-parse','HEAD')]:
                confirmed, observed=self.d._verify_runtime_sha(sha,value['pid'])
                if confirmed: break
            if not confirmed: raise c.Invalid('ordinary process/source SHA not proven')
            value['sha']=sha
        else: value['sha']=None
        value['provenance']=self.runtime.provenance({})
        return value

    def verify_sealed_prior(self, bundle, observed):
        rows = bundle['metadata']['prior_process']['provenance']['processes']
        if (not isinstance(rows, list) or any(not isinstance(r, dict) or set(r) != {'pid','start'}
                or type(r['pid']) is not int or r['pid'] <= 0 or not isinstance(r['start'],str) or not r['start']
                for r in rows) or len({r['pid'] for r in rows}) != len(rows)):
            raise c.Invalid('ordinary sealed prior census malformed or duplicate')
        current = {r['pid']:r['start'] for r in observed['provenance']['processes']}
        for row in rows:
            pid, birth = row['pid'], row['start']
            if pid in current:
                if current[pid] != birth: raise c.Invalid('ordinary sealed prior PID reused')
            elif not self.process_absent(pid, birth):
                raise c.Invalid('ordinary sealed prior process exit unproven')
        return rows

    def compare_process(self, expected, observed):
        return compare_process(self, expected, observed)

    def process_absent(self, pid, birth):
        # A reused PID is not proof of absence for ordinary admission.
        return self.runtime._process_absent(pid)

    def authenticate_new_processes(self, primary, added):
        # Require actual same-UID ancestry to the unchanged launchd primary.
        # Helper names or command substrings alone are not ownership proof.
        state = self.job(self.service.launchd_label)
        if any(state[k] != primary[k] for k in ('loaded', 'pid', 'birth')):
            return False
        rows = self.runtime.process_rows()
        by_pid = {}
        for pid, parent, uid, _ in rows:
            if pid in by_pid: return False
            by_pid[pid] = (parent, uid)
        owned = {primary['pid']}
        for _ in rows:
            owned.update(pid for pid, (parent, uid) in by_pid.items()
                         if parent in owned and uid == os.getuid())
        if any(row['pid'] not in owned or self.d._process_start(row['pid'], self.runner) != row['start']
               for row in added):
            return False
        return self.job(self.service.launchd_label) == state

    def observe(self, bundle):
        return {'source':self.git('rev-parse','HEAD'),
                'files':{k:file_state(Path(v['path'])) for k,v in bundle['preimages'].items()},
                'process':self.process([bundle['prior_sha'],bundle['target_sha']]), 'collector':self.job(self.d.USAGE_COLLECTOR_LABEL)}

    def observe_manual(self, bundle, *, successor_sha=None):
        from deploy.packet.log_scan import freeze, generation_ranges

        label = self.service.launchd_label
        source = self.git('rev-parse', 'HEAD')
        process = self.job(label)
        pid, birth = process['pid'], process['birth']
        if (successor_sha is None and source != bundle['target_sha']) or not process['loaded'] or not pid or not birth:
            raise c.Invalid('manual recovery requires the registered target process')

        paths = self.paths(bundle['metadata'])
        files = {name: file_state(path) for name, path in paths.items()}
        for name in ('policy', 'machines', 'settings', 'schedule'):
            row = bundle['preimages'][name]
            if files[name] != {'sha256': row['sha256'], 'mode': row['mode']}:
                raise c.Invalid('manual recovery immutable managed input changed')
        stamp = paths['stamp'].read_bytes()
        stamped = c.strict_json(stamp)
        if (not isinstance(stamped, dict) or stamped.get('sha') != bundle['target_sha']
                or hashlib.sha256(stamp).hexdigest() != files['stamp']['sha256']):
            raise c.Invalid('manual recovery target stamp unavailable')

        plist = plistlib.loads(paths['plist'].read_bytes())
        argv = plist['ProgramArguments']
        workdir = self.repo/'services/chat-stream-v2'
        main = str(workdir/'main.py')
        python = self.d._venv_python(self.repo, self.service)
        configured_main = argv[2] if argv[1] == '-u' else argv[1]
        configured_cwd = plist.get('WorkingDirectory')
        if (plist['Label'] != label or configured_cwd not in (None, str(workdir))
                or argv[0] != str(python) or configured_main != main):
            raise c.Invalid('manual recovery plist does not identify the runtime checkout')
        loaded = self.runtime.checked(('launchctl', 'print', self.d._launchctl_target(label))).stdout
        program = re.search(r'^\s*program = (.+)$', loaded, re.M)
        arguments = re.search(r'\n\s*arguments = \{\n(.*?)\n\s*\}', loaded, re.S)
        if (not program or program[1].strip() != argv[0] or not arguments
                or [line.strip() for line in arguments[1].splitlines()] != argv
                or file_state(paths['plist']) != files['plist']):
            raise c.Invalid('manual recovery loaded launchd arguments do not match the managed plist')
        command = self.runtime.checked(('ps', '-ww', '-p', str(pid), '-o', 'command=')).stdout.strip()
        interpreter = python.resolve()
        framework = interpreter.parent.parent/'Resources/Python.app/Contents/MacOS/Python'
        if (not command or command.split()[0] not in {str(python), str(interpreter), str(framework)}
                or not any(command.startswith(f'{command.split()[0]} {prefix}{main} ')
                           for prefix in ('', '-u '))):
            raise c.Invalid('manual recovery live process does not run the registered checkout')

        working_directory = '/' if configured_cwd is None else configured_cwd
        cwd_rows = self.runtime.checked(('/usr/sbin/lsof', '-nP', '-a', '-p', str(pid), '-d', 'cwd',
                                         '-Fpn')).stdout.splitlines()
        if cwd_rows != [f'p{pid}', 'fcwd', f'n{working_directory}']:
            raise c.Invalid('manual recovery live process working directory unproven')

        port = MANUAL_LISTENER.split(':')[1]
        listeners = self.runtime.checked(('/usr/sbin/lsof', '-nP', '-iTCP:' + port,
                                          '-sTCP:LISTEN', '-Fpn')).stdout.splitlines()
        if (not listeners or {row for row in listeners if row.startswith('p')} != {'p' + str(pid)}
                or [row for row in listeners if row == 'n' + MANUAL_LISTENER] != ['n' + MANUAL_LISTENER]):
            raise c.Invalid('manual recovery loopback listener ownership unproven')

        managed = self.d._managed_log_config(self.service)
        log_paths = self.d._daemon_log_paths(self.service)
        groups = []
        for path in log_paths:
            candidates = list(reversed(managed.paths(path))) if managed else [path]
            groups.append([freeze(item) for item in candidates if item.is_file()])
        if sum(item['bytes_read'] for group in groups for item in group) > 128 * 1024 * 1024:
            raise c.Invalid('manual recovery boot evidence exceeds the read bound')
        rotated_stderr = (managed is not None and len(groups) == 2
                          and len(groups[1]) == len(managed.paths(log_paths[1])) > 1
                          and _logs_postdate_boot(groups[1], birth))
        _, _, markers = generation_ranges(groups, pid, birth, allow_rotated_stderr=rotated_stderr)
        checkout = {}
        serving_sha = source
        if successor_sha is not None:
            # Git HEAD and the failed stamp can both differ from the release resident in memory.
            branch = self.git('rev-parse', '--symbolic-full-name', 'HEAD')
            checkout = {'checkout': str(self.repo), 'branch': branch}
            try:
                with self.d.connect('ws://' + MANUAL_LISTENER, proxy=None, close_timeout=0,
                                    origin='https://example.invalid', open_timeout=2) as sock:
                    welcome = c.strict_json(sock.recv(timeout=2))
            except Exception:
                raise c.Invalid('successor welcome unavailable') from None
            if not isinstance(welcome, dict) or welcome.get('type') != 'welcome' or welcome.get('runtime_sha') != successor_sha:
                raise c.Invalid('successor serving release mismatch')
            serving_sha = welcome['runtime_sha']
        if self.job(label) != process:
            raise c.Invalid('manual recovery process changed during observation')
        return {**checkout, 'source': source, 'process': {**process, 'sha': serving_sha},
                'boot_uuid': markers[0]['identity']['boot_uuid'], 'listener': MANUAL_LISTENER,
                'boot_evidence': 'paired' if len(markers) == 2 else 'stdout_stderr_rotated',
                'launchd_label': label, 'launch_arguments_sha256': c.digest(argv),
                'working_directory': working_directory,
                'files': files, 'collector': self.job(self.d.USAGE_COLLECTOR_LABEL)}

    def prove_manual(self, serving, credential, authority, confirm_serving, *, superseded_operation=None):
        from tools.live_window.core import operator_hello

        go = authority['go_tell_id']
        deadline = time.monotonic() + MANUAL_PROOF_SECONDS

        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise c.Invalid('manual recovery authenticated readback deadline expired')
            return left

        def receive(socket):
            value = c.strict_json(socket.recv(timeout=remaining()))
            remaining()
            if not isinstance(value, dict):
                raise c.Invalid('manual recovery malformed daemon reply')
            return value

        def correlated(socket, payload, expected):
            rid = uuid.uuid4().hex
            socket.send(json.dumps({**payload, 'request_id': rid}))
            while True:
                value = receive(socket)
                if value.get('request_id') == rid:
                    if value['type'] != expected:
                        raise c.Invalid('manual recovery authenticated request refused')
                    confirm_serving()
                    remaining()
                    return value

        try:
            with self.d.connect('ws://' + MANUAL_LISTENER, proxy=None, close_timeout=0,
                                origin='https://example.invalid', open_timeout=remaining(),
                                max_size=128 * 1024 * 1024) as socket:
                welcome = receive(socket)
                if welcome.get('type') != 'welcome' or welcome.get('runtime_sha') != serving['process']['sha']:
                    raise c.Invalid('manual recovery listener reports a different serving release')
                hello = operator_hello(c.private_file(credential), welcome)
                correlated(socket, hello, 'hello')
                binding = correlated(socket, {'type': 'assistant.binding'}, 'assistant.binding.ok')
                ledger = correlated(socket, {'type': 'ledger_get', 'tell_id': go}, 'ledger_get.ok')
                seat = correlated(socket, {'type': 'inspect_stream', 'stream_id': binding['stream_id'],
                                           'event_tail': 0}, 'inspect_stream.ok')
                current = correlated(socket, {'type': 'assistant.binding'}, 'assistant.binding.ok')
        except c.Invalid:
            raise
        except Exception:
            raise c.Invalid('manual recovery authenticated connection failed') from None

        def role(value):
            return {key: value.get(key) for key in ('composite_stream_id', 'stream_id', 'generation', 'revision', 'source')}

        sender = role(binding)
        session = seat.get('session') or {}
        tell = ledger.get('tell') or {}
        verb, operation = (('record-ordinary-manual-recovery', MANUAL_LEGACY['operation'])
                           if superseded_operation is None else ('supersede-ordinary', superseded_operation))
        expected_text = (f'GO {verb} {operation} {authority["authorization_sha256"]} '
                         f'{authority["replacement_controller_sha"]}')
        delivered = datetime.fromisoformat(tell['delivered_at'])
        age = (datetime.now(timezone.utc) - delivered).total_seconds()
        if (sender != role(current) or not sender['stream_id'] or not sender['generation']
                or not sender['composite_stream_id'] or type(sender['revision']) is not int
                or sender['source'] not in {'durable', 'env'}
                or seat.get('stream_id') != sender['stream_id'] or session.get('status') != 'open'
                or session.get('session_generation') != sender['generation']
                or ledger.get('read_only') is not True or ledger.get('source') != 'v2_tell_deliveries'
                or ledger.get('tell_id') != go or tell.get('tell_id') != go
                or type(ledger.get('ledger_row_id')) is not int or ledger['ledger_row_id'] <= 0
                or ledger['ledger_row_id'] != tell.get('ledger_row_id')
                or tell.get('sender_verified') is not True or tell.get('sender_kind') != 'peer'
                or tell.get('from_stream_id') != sender['stream_id']
                or tell.get('sender_generation') != sender['generation']
                or tell.get('to_stream_id') != authority['deployer_stream_id']
                or tell.get('original_to_stream_id') != authority['deployer_stream_id']
                or tell.get('delivery_status') != 'delivered' or tell.get('text') != expected_text
                or not 0 <= age <= MANUAL_GO_SECONDS):
            raise c.Invalid('manual recovery FD GO does not match current binding, authority or freshness')
        return {'outcome': 'passed', 'verb': 'ledger_get', 'request_id': ledger['request_id'],
                'ledger_row_id': ledger['ledger_row_id'], 'go_tell_id': go, 'go_delivered_at': tell['delivered_at'],
                'front_desk': sender, 'runtime_sha': welcome['runtime_sha'],
                'observed_at': datetime.now(timezone.utc).isoformat()}

    def capture(self, prior, target, ancestry, entries):
        from deploy.coordinated_runtime import code_identity_at, helper_release_root
        if self.controller == self.repo or self.d._dirty_entries(self.controller,self.runner):
            raise c.Invalid('ordinary recovery requires a clean separate pinned controller')
        if self.d._dirty_entries(self.repo,self.runner) or self.git('rev-parse','HEAD') != prior:
            raise c.Invalid('ordinary recovery prior source drift')
        controller_sha=self.git('rev-parse','HEAD',repo=self.controller)
        origin=self.git('remote','get-url','origin')
        if origin != self.git('remote','get-url','origin',repo=self.controller):
            raise c.Invalid('ordinary recovery controller/source origin mismatch')
        identity=code_identity_at(self.repo,prior,self.runner)
        if identity != code_identity_at(self.repo,target,self.runner):
            raise c.Invalid('ordinary recovery code identity mismatch')
        requirements=self.git('rev-parse',prior+':'+self.service.requirements_path)
        if requirements != self.git('rev-parse',target+':'+self.service.requirements_path):
            raise c.Invalid('ordinary recovery cannot reverse dependency changes')
        stamp=self.d._read_stamp(self.repo,self.service)
        if (not self.d._venv_python(self.repo,self.service).exists()
                or stamp.get('requirements_hash') != self.d._sha256(self.repo/self.service.requirements_path)):
            raise c.Invalid('ordinary recovery requires the already provisioned unchanged interpreter')
        plist=self.d._launchd_plist(self.service.launchd_label); env=plist.get('EnvironmentVariables',{})
        policy_path=c.absolute(env.get('PENTACLE_PROVIDER_ACCOUNT_POLICY',''))
        machines_path=c.absolute(env.get('PENTACLE_MACHINES_FILE',''))
        policy=c.read_json(policy_path)
        scope=c.host_scope(machines_path,self.d._declared_release_scope(self.d.release_exclusions(self.service,self.repo)))
        accounts=self.runtime.account_snapshot(policy,self.runtime.database(plist),accounts=entries,scope=scope)
        if not accounts['selected_bindings']: raise c.Invalid('ordinary recovery binding population empty')
        helpers={}
        for host in scope['included_hosts']:
            config=policy['executors'][host]
            machine=next(row for row in scope['machines'] if row['name']==host)
            root=str(helper_release_root(config['script']))
            helper={'python':config['python'],'script':config['script'], 'target':{
                'name':host,'ssh':machine.get('ssh_target') or 'localhost','root':root,'local':machine.get('ssh_target') is None}}
            # Retain exact immutable manifest/archive bytes and modes; never migrate helpers.
            helper['preimage']=self.helper_preimage(helper)
            helper['proof']=self.runtime.helper_proof(helper,accounts,identity,
                helper['preimage']['manifest'],helper['preimage']['commit'])
            helpers[host]=helper
        daemon_helper = copy.deepcopy(helpers[scope['local_hosts'][0]])
        daemon_helper.update(python=str(self.d._venv_python(self.repo,self.service)),
                             script=str(self.repo/'services/chat-stream-v2/provider_account_host.py'))
        daemon_proof = self.daemon_proof(daemon_helper,accounts,identity,prior)
        self.d.verify_deploy_guard(self.service,self.stream,self.runner,repo=self.repo,sha=target)
        # Keep standalone deploy/check-release imports free of runtime-only tools.
        from deploy.restoration import inherited_receipt_seal
        seal=inherited_receipt_seal(ancestry) if ancestry else None
        process=self.process()
        if process['sha'] != prior: raise c.Invalid('ordinary recovery prior serving identity unavailable')
        schedule=self.job('com.pentacle.spawn-fleet-smoke')
        if schedule['loaded']: raise c.Invalid('retired smoke schedule must be unloaded before ordinary recovery enrollment')
        return {'controller_sha':controller_sha,'origin':origin,'identity':identity,
                'requirements_blob':requirements,'requirements_hash':stamp['requirements_hash'],
                'policy_path':str(policy_path),'machines_path':str(machines_path),
                'scope':scope,'accounts':accounts,'account_entries':list(entries or []),'helpers':helpers,
                'daemon_helper':daemon_helper,'daemon_proof':daemon_proof,
                'ancestry':ancestry,'receipt_sha256':seal,'prior_process':process,
                'prior_collector':self.job(self.d.USAGE_COLLECTOR_LABEL),'schedule':schedule}

    def daemon_proof(self, helper, accounts, identity, sha):
        proof=self.runtime.helper_proof(helper,accounts,identity,{},sha,verify_release=False,closure_only=True)
        return {k:proof[k] for k in ('python','real_python','python_sha256','version','dependencies','dependency_files')}

    def helper_preimage(self, helper):
        release=Path(helper['script']).parents[3]
        if release.parent.name != '.pentacle-account-helpers' or not re.fullmatch('[0-9a-f]{40}',release.name):
            raise c.Invalid('durable ordinary recovery requires immutable managed helper releases')
        program = """
import base64,hashlib,json,os,pathlib,stat,sys
root=pathlib.Path(sys.argv[1]);result={}
for name in ('manifest.json','pentacle.tar'):
    path=root/name;info=path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not path.is_symlink()
    data=path.read_bytes();result[name]={'bytes':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest(),'mode':stat.S_IMODE(info.st_mode)}
print(json.dumps(result))
"""
        files=self.runtime.remote_json(helper,[helper['python'],'-I','-c',program,str(release)])
        for row in files.values():
            if hashlib.sha256(base64.b64decode(row['bytes'],validate=True)).hexdigest()!=row['sha256']:
                raise c.Invalid('helper archive capture mismatch')
        manifest=json.loads(base64.b64decode(files['manifest.json']['bytes']))
        if manifest.get('commit') != release.name or manifest.get('archive_sha256') != files['pentacle.tar']['sha256']:
            raise c.Invalid('helper manifest/archive mismatch')
        return {'commit':release.name,'manifest':manifest,'files':files}

    def save_source(self, folder, prior, target):
        staging=folder/'source-staging.git'; archive=folder/'source.bundle'
        checked=self.runtime.checked
        checked(('git','clone','--bare','--shared',str(self.repo),str(staging)))
        for name,sha in (('prior',prior),('target',target)):
            checked(('git','--git-dir',str(staging),'update-ref','refs/heads/'+name,sha))
        checked(('git','--git-dir',str(staging),'bundle','create',str(archive),'refs/heads/prior','refs/heads/target'))
        archive.chmod(0o600)
        with archive.open('rb') as stream: os.fsync(stream.fileno())
        shutil.rmtree(staging)
        return {'sha256':c.file_digest(archive),'heads':{'prior':prior,'target':target}}

    def validate_source(self, folder, source, prior, target):
        archive=c.private_file(folder/'source.bundle')
        if source != {'sha256':c.file_digest(archive),'heads':{'prior':prior,'target':target}}:
            raise c.Invalid('ordinary source bundle corrupt')
        rows=self.runtime.checked(('git','bundle','list-heads',str(archive))).stdout.splitlines()
        if {tuple(row.split()) for row in rows} != {(prior,'refs/heads/prior'),(target,'refs/heads/target')}:
            raise c.Invalid('ordinary source bundle refs mismatch')
        self.runtime.checked(('git','bundle','verify',str(archive)))
        self._source_validation=tempfile.TemporaryDirectory(prefix='ordinary-source-validation-')
        self.source_repo=Path(self._source_validation.name)/'source.git'
        self.runtime.checked(('git','clone','--bare',str(archive),str(self.source_repo)))

    def validate_controller(self, bundle, allowed_controller):
        m = bundle['metadata']
        if (self.controller == self.repo or self.git('rev-parse','HEAD',repo=self.controller) != allowed_controller
                or self.d._dirty_entries(self.controller,self.runner) or self.d._dirty_entries(self.repo,self.runner)
                or self.git('remote','get-url','origin') != m['origin']
                or self.git('remote','get-url','origin',repo=self.controller) != m['origin']):
            raise c.Invalid('ordinary recovery controller/source drift')

    def validate(self, bundle):
        from deploy.restoration import inherited_receipt_seal
        from deploy.coordinated_runtime import code_identity_at
        m=bundle['metadata']
        grant = getattr(self, 'recovery_authorization', None)
        allowed_controller = m['controller_sha']
        if grant is not None:
            if (grant['operation'] != bundle['operation'] or grant['bundle_sha256'] != bundle['sha256']
                    or grant['original_controller_sha'] != m['controller_sha']):
                raise c.Invalid('ordinary replacement controller scope drift')
            allowed_controller = grant['replacement_controller_sha']
        self.validate_controller(bundle, allowed_controller)
        for sha in (bundle['prior_sha'],bundle['target_sha']):
            if code_identity_at(getattr(self,'source_repo',self.repo),sha,self.runner) != m['identity']:
                raise c.Invalid('ordinary recovery identity drift')
        if m['ancestry'] and inherited_receipt_seal(m['ancestry']) != m['receipt_sha256']:
            raise c.Invalid('ordinary recovery ancestry drift')
        if self.job('com.pentacle.spawn-fleet-smoke') != m['schedule']:
            raise c.Invalid('ordinary recovery schedule state drift')
        # These inputs must never change in this bounded ordinary operation.
        for name in ('policy','machines','settings','schedule'):
            row=bundle['preimages'][name]
            if file_state(row['path']) != {'sha256':row['sha256'],'mode':row['mode']}:
                raise c.Invalid('ordinary recovery immutable input drift')
        policy=c.read_json(m['policy_path'])
        accounts=self.runtime.account_snapshot(policy,m['accounts']['database'],accounts=m['account_entries'],scope=m['scope'])
        if accounts != m['accounts']: raise c.Invalid('ordinary recovery account/pin/binding drift')
        for helper in m['helpers'].values():
            preimage=self.helper_preimage(helper)
            if (preimage != helper['preimage'] or self.runtime.helper_proof(helper,accounts,m['identity'],
                    preimage['manifest'],preimage['commit']) != helper['proof']):
                raise c.Invalid('ordinary recovery helper/interpreter drift')
        if self.daemon_proof(m['daemon_helper'],accounts,m['identity'],bundle['prior_sha']) != m['daemon_proof']:
            raise c.Invalid('ordinary recovery daemon interpreter/dependency drift')
        self.d.verify_deploy_guard(self.service,self.stream,self.runner,repo=self.repo,
            sha=bundle['prior_sha'],rollback=True,rollback_from_sha=self.git('rev-parse','HEAD'))

    def stop(self, bundle, now, decisions):
        collector=now['collector']
        fresh = self.observe(bundle)
        if {k:v for k,v in fresh.items() if k != 'process'} != {k:v for k,v in now.items() if k not in ('journal_sha256', 'process')}:
            raise c.Invalid('ordinary identity/state drift immediately before stop')
        pre_stop = self.compare_process(now['process'], fresh['process'])
        tracked = {(r['pid'],r['start']) for decision in decisions for key in ('previous','observed') for r in decision[key]}
        tracked.update((r['pid'],r['start']) for key in ('previous','observed') for r in pre_stop[key])
        sealed_prior = self.verify_sealed_prior(bundle, fresh['process'])
        tracked.update((r['pid'],r['start']) for r in sealed_prior)
        if len({pid for pid, _ in tracked}) != len(tracked):
            raise c.Invalid('ordinary tracked PID reused before stop')
        self.tracked_processes = tracked
        self.before_stop(pre_stop)
        self.runtime.stop({},provenance={'processes':[{'pid':pid,'start':birth} for pid,birth in tracked]}, expected_identity=now['process'])
        if self.job(self.d.USAGE_COLLECTOR_LABEL) != collector:
            raise c.Invalid('collector drift before stop')
        if collector['loaded']:
            self.runtime.checked(('launchctl','bootout',self.d._launchctl_target(self.d.USAGE_COLLECTOR_LABEL)))
        state=self.d._wait_launchd_unloaded(self.d.USAGE_COLLECTOR_LABEL,self.runner,timeout_s=self.d.RELOAD_UNLOAD_WAIT_S)
        if (not state.answered or state.loaded or state.pid is not None
                or (collector['pid'] and not self.d._prior_writer_exited(collector['pid'],collector['birth'],self.runner))):
            raise c.Invalid('collector exit unproven')

    def ensure_stopped(self, bundle):
        if not self.runtime.stopped: raise c.Invalid('ordinary restoration requires proven stop')
        if any(not self.process_absent(pid,birth) for pid,birth in self.tracked_processes):
            raise c.Invalid('ordinary tracked process exit no longer proven')
        process=self.process([bundle['prior_sha'],bundle['target_sha']]); collector=self.job(self.d.USAGE_COLLECTOR_LABEL)
        if (process['pid'] is not None or process['loaded'] or process['provenance']['processes']
                or collector['pid'] is not None or collector['loaded']):
            raise c.Invalid('ordinary process reappeared before restoration')

    def restore_source(self, folder, bundle):
        self.ensure_stopped(bundle)
        self.runtime.checked(('git','fetch',str(folder/'source.bundle'),'refs/heads/prior'))
        self.runtime.checked(('git','checkout','--detach',bundle['prior_sha']))

    def verify_restored(self, bundle):
        if self.git('rev-parse','HEAD') != bundle['prior_sha'] or self.d._dirty_entries(self.repo,self.runner):
            raise c.Invalid('ordinary source restoration unproven')
        for row in bundle['preimages'].values():
            if file_state(row['path']) != {'sha256':row['sha256'],'mode':row['mode']}:
                raise c.Invalid('ordinary file restoration unproven')
        self.validate(bundle)

    def start(self, bundle):
        old=self.runtime.old_pid
        offsets=self.d._capture_log_offsets(self.d._daemon_log_paths(self.service))
        self.d._bootstrap_launchd(self.service.launchd_label,self.runner,cwd=self.repo)
        boot=self.d.verify_fresh_daemon_log_line(self.service,previous_size=offsets,prior_pid=old,
            wait_seconds=self.runtime.boot_wait_seconds,runner=self.runner)
        if boot.outcome != self.d.BOOT_OBSERVED or boot.pid == old:
            raise c.Invalid('ordinary restored fresh boot unproven')
        confirmed, observed=self.d._verify_runtime_sha(bundle['prior_sha'],boot.pid)
        birth=self.d._process_start(boot.pid,self.runner)
        if not confirmed or not birth: raise c.Invalid('ordinary restored runtime identity unproven')
        self.runtime.verification_started=time.time()
        # The collector is started only after the complete prior tree is verified.
        if bundle['metadata']['prior_collector']['loaded']:
            self.d._bootstrap_launchd(self.d.USAGE_COLLECTOR_LABEL,self.runner,cwd=self.repo)
        return {'sha':bundle['prior_sha'],'pid':boot.pid,'birth':birth},boot

    def prove(self, bundle, boot):
        m=bundle['metadata']
        receipt={'accounts':m['accounts'],'scope':m['scope'],'transaction_directory':bundle['folder']}
        return self.runtime.restoration_proof(receipt,{'sha':bundle['prior_sha'],'code_identity':m['identity']},boot)

def supersede_main(argv=None):
    """Record a successor through the same lock and Bundle used by ordinary deployment."""
    import argparse
    import subprocess
    import sys
    from deploy import deploy as deployer

    parser = argparse.ArgumentParser(description='Record a failed ordinary operation as superseded.')
    for name in ('release-checkout', 'bundle', 'authorization', 'successor-receipt', 'operator-credential'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--deployer-stream-id', required=True)
    parser.add_argument('--go-tell-id', required=True)
    args = parser.parse_args(argv)
    try:
        repo = c.absolute(str(args.release_checkout))
        folder = c.absolute(str(args.bundle))
        # A mistyped checkout must not create fresh deployment state. Revalidated under the lock.
        c.private_file(c.state_root(repo) / 'ordinary-active.json')
        with c.transaction_lock(repo):
            adapter = OrdinaryRuntime(deployer, deployer.SERVICES['chat-streamd-v2'], repo, deployer._run,
                                      stream=args.deployer_stream_id, boot_wait_seconds=0)
            result = Bundle(adapter, folder).record_superseded(args.authorization, args.successor_receipt,
                                                               args.operator_credential, args.go_tell_id)
    except (c.Invalid, deployer.DeployError, OSError, ValueError, TypeError, KeyError, IndexError,
            subprocess.SubprocessError) as exc:
        print(f'ordinary supersession refused: {exc}', file=sys.stderr)
        return deployer.EXIT_REFUSED
    print(json.dumps(result, sort_keys=True))
    return deployer.EXIT_OK


if __name__ == '__main__':
    raise SystemExit(supersede_main())
