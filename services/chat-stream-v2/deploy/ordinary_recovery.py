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

from deploy import coordinated as c
from deploy.restoration import inherited_receipt_seal

ACTIVE = contextvars.ContextVar('ordinary_recovery_writer', default=None)
SCHEMA = 'pentacle.ordinary-recovery.v1'
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


def fence(repo, *, recovering=None):
    path = c.state_root(repo) / 'ordinary-active.json'
    if not path.exists():
        return
    pointer = c.read_json(path)
    folder = Path(pointer['bundle'])
    journal = sealed_read(folder / 'journal.json')
    if journal['operation'] != pointer['operation']:
        raise c.Invalid('ordinary recovery active operation mismatch')
    if journal['phase'] not in TERMINAL and (recovering is None or Path(recovering).resolve() != folder):
        raise c.Invalid('unfinished ordinary operation requires its authorized recovery')


class Bundle:
    def __init__(self, adapter, folder):
        self.a = adapter
        self.folder = Path(folder).absolute()
        self.bundle = self.journal = None

    def persist(self, **updates):
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
        if (current['source'] != prior or current['process'] != metadata['prior_process']
                or current['collector'] != metadata['prior_collector']
                or any(current['files'][k] != {'sha256':v['sha256'],'mode':v['mode']} for k,v in originals.items())
                or any(v['sha256'] is not None and c.file_digest(self.folder/(k+'.before')) != v['sha256'] for k,v in originals.items())):
            raise c.Invalid('ordinary capture drift before enrollment')
        adapter.validate(self.bundle)
        self.journal = sealed_write(self.folder/'journal.json', {
            'schema':SCHEMA, 'operation':operation, 'bundle_sha256':self.bundle['sha256'],
            'sequence':0, 'phase':'forward', 'allowed_source':[prior,target], 'allowed_files':{
                k:[{'sha256':v['sha256'], 'mode':v['mode']}] for k,v in originals.items()},
            'process':current['process'], 'collector':current['collector'], 'result':None})
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
        now = self.a.observe(self.bundle)
        return {**now, 'journal_sha256':self.journal['sha256']}

    def admit(self, authorization, go):
        b, j = self.bundle, self.journal
        if j['phase'] == 'qualified':
            raise c.Invalid('qualified operation is not an ordinary failure recovery')
        self.a.validate(b)
        now = self.state()
        if now['source'] not in j['allowed_source'] or now['source'] not in (b['prior_sha'], b['target_sha']):
            raise c.Invalid('unrelated ordinary source drift')
        if now['process'] != j['process'] or now['collector'] != j['collector']:
            raise c.Invalid('unrecorded ordinary process identity')
        for name, state in now['files'].items():
            if state not in j['allowed_files'][name]:
                raise c.Invalid('unrecognized ordinary file drift')
        auth = c.read_json(authorization)
        expected = {'schema':SCHEMA, 'operation':b['operation'], 'bundle_sha256':b['sha256'],
            'controller_sha':b['metadata']['controller_sha'], 'prior_sha':b['prior_sha'],
            'target_sha':b['target_sha'], 'expected_state':now, 'go_tell_id':go}
        if (set(auth) != {*expected, 'fd_gate_reference'} or any(auth.get(k) != v for k,v in expected.items())
                or not isinstance(go,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',go)
                or not isinstance(auth['fd_gate_reference'],str) or not auth['fd_gate_reference'].strip()):
            raise c.Invalid('ordinary recovery requires exact separate FD authority/current state')
        return now

    def recover(self, authorization, go):
        self.load()
        now = self.admit(authorization, go)
        if self.journal['phase'] == 'restored_unqualified':
            if self.journal.get('restored') != {k:v for k,v in now.items() if k != 'journal_sha256'}:
                raise c.Invalid('completed ordinary recovery drift')
            return self.journal['result']
        # Persist intent before stop. Unknown outcomes remain fenced, never inferred successful.
        self.persist(phase='recovering', authorization_sha256=c.file_digest(authorization), go_tell_id=go)
        boot_proven = False
        try:
            self.a.stop(self.bundle, now)
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

    def observe(self, bundle):
        return {'source':self.git('rev-parse','HEAD'),
                'files':{k:file_state(Path(v['path'])) for k,v in bundle['preimages'].items()},
                'process':self.process([bundle['prior_sha'],bundle['target_sha']]), 'collector':self.job(self.d.USAGE_COLLECTOR_LABEL)}

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

    def validate(self, bundle):
        from deploy.coordinated_runtime import code_identity_at
        m=bundle['metadata']
        if (self.controller == self.repo or self.git('rev-parse','HEAD',repo=self.controller) != m['controller_sha']
                or self.d._dirty_entries(self.controller,self.runner) or self.d._dirty_entries(self.repo,self.runner)
                or self.git('remote','get-url','origin') != m['origin']
                or self.git('remote','get-url','origin',repo=self.controller) != m['origin']):
            raise c.Invalid('ordinary recovery controller/source drift')
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

    def stop(self, bundle, now):
        # Caller has checked exact current process identity and all drift predicates.
        collector=now['collector']
        if self.observe(bundle) != {k:v for k,v in now.items() if k != 'journal_sha256'}:
            raise c.Invalid('ordinary identity/state drift immediately before stop')
        self.runtime.stop({},provenance=now['process']['provenance'], expected_identity=now['process'])
        if self.job(self.d.USAGE_COLLECTOR_LABEL) != collector:
            raise c.Invalid('collector drift before stop')
        if collector['loaded']:
            self.runtime.checked(('launchctl','bootout',self.d._launchctl_target(self.d.USAGE_COLLECTOR_LABEL)))
        state=self.d._wait_launchd_unloaded(self.d.USAGE_COLLECTOR_LABEL,self.runner,timeout_s=self.d.RELOAD_UNLOAD_WAIT_S)
        if (not state.answered or state.loaded or state.pid is not None
                or (collector['pid'] and not self.d._prior_writer_exited(collector['pid'],collector['birth'],self.runner))):
            raise c.Invalid('collector exit unproven')

    def restore_source(self, folder, bundle):
        if not self.runtime.stopped: raise c.Invalid('ordinary source restore requires proven stop')
        process=self.process([bundle['prior_sha'],bundle['target_sha']]); collector=self.job(self.d.USAGE_COLLECTOR_LABEL)
        if (process['pid'] is not None or process['loaded'] or process['provenance']['processes']
                or collector['pid'] is not None or collector['loaded']):
            raise c.Invalid('ordinary process reappeared before source restoration')
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
