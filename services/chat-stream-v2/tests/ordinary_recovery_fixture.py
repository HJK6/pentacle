"""Synthetic external runtime probes; Git, bundle, journal and file operations are real."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from deploy import ordinary_recovery as o


def command(argv, cwd):
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True)


class SyntheticRuntime(o.OrdinaryRuntime):
    def __init__(self, deployer, service, repo, runner, *, stream, boot_wait_seconds):
        self.d,self.service,self.repo,self.runner=deployer,service,Path(repo).resolve(),command
        self.root=self.repo.parent; self.stream=stream; self.controller=self.root/'controller'
        def checked(argv,cwd=None):
            result=command(tuple(map(str,argv)),cwd or self.repo)
            if result.returncode: raise o.c.Invalid('synthetic git command failed: '+result.stderr)
            return result
        self.runtime=SimpleNamespace(checked=checked,stopped=False,old_pid=None)

    def process(self, shas=None): return self.probes()['process']
    def job(self, label): return self.probes()['collector']
    def probes(self): return json.loads((self.root/'probes.json').read_text())
    def save_probes(self,p): (self.root/'probes.json').write_text(json.dumps(p))
    def event(self,event):
        with (self.root/'events').open('a') as f: f.write(event+'\n')
    def paths(self,metadata):
        return {name:self.root/'files'/name for name in ('stamp','plist','collector','schedule','settings','profile','policy','machines')}
    def capture(self,prior,target,ancestry,entries):
        p=self.probes()
        return {'controller_sha':p['controller_sha'],'accounts':p['accounts'],
                'prior_collector':p['collector'],'prior_process':p['process'],
                'identity':'synthetic-only','ancestry':ancestry,'receipt_sha256':None}
    def observe(self,bundle):
        p=self.probes()
        return {'source':self.git('rev-parse','HEAD'),
                'files':{k:o.file_state(v) for k,v in self.paths(bundle['metadata']).items()},
                'process':p['process'],'collector':p['collector']}
    def validate(self,bundle):
        p=self.probes();m=bundle['metadata']
        grant = getattr(self, 'recovery_authorization', None)
        expected_controller = grant['replacement_controller_sha'] if grant else m['controller_sha']
        if p['controller_sha'] != expected_controller or p['accounts'] != m['accounts']:
            raise o.c.Invalid('synthetic controller/account/binding drift')
        if p.get('fail_validate'): raise o.c.Invalid('synthetic validation refusal')
    def compare_process(self, expected, observed):
        # Legacy synthetic fixtures predate complete launchd census records.
        if 'launchd_answered' not in expected.get('provenance', {}):
            if expected != observed: raise o.c.Invalid('synthetic process drift')
            return {'previous': [], 'observed': [], 'departed_absent': [], 'added_owned': []}
        return super().compare_process(expected, observed)
    def stop(self,bundle,now,decision):
        self.tracked_processes = set()
        self.event('stop')
        p=self.probes()
        if p.get('fail_stop'): raise o.c.Invalid('synthetic stop unproven')
        self.runtime.old_pid=p['process']['pid']
        p['process']={'pid':None,'birth':None,'sha':None,'loaded':False,'provenance':{'processes':[]}}
        p['collector']={'pid':None,'birth':None,'loaded':False}
        self.save_probes(p);self.runtime.stopped=True
        self.event('stop-proven')
        if p.get('drift_during_stop'): (self.root/'files'/'policy').write_bytes(b'foreign drift')
    def ensure_stopped(self,bundle):
        if not self.runtime.stopped: raise o.c.Invalid('synthetic stop not proven')
    def restore_source(self,folder,bundle):
        self.event('source')
        super().restore_source(folder,bundle)
    def start(self,bundle):
        self.event('start')
        self.verify_restored(bundle)
        p=self.probes()
        if p.get('fail_start'): raise o.c.Invalid('synthetic boot unproven')
        p['process']={'pid':303,'birth':'synthetic-fresh','sha':bundle['prior_sha'],'loaded':True,'provenance':{'processes':[]}}
        p['collector']=bundle['metadata']['prior_collector']
        self.save_probes(p)
        return dict(p['process']),None
    def prove(self,bundle,boot):
        self.event('zero-input-owned-close')
        if self.probes().get('fail_proof'): raise o.c.Invalid('synthetic binding proof failed')
        return {'outcome':'passed','mode':'zero_input','adapter':'synthetic, not live runtime proof'}


def cli(root,args):
    from deploy import deploy as d
    original, runner = o.OrdinaryRuntime, d._run
    try:
        o.OrdinaryRuntime=SyntheticRuntime
        d._run=command
        return d.main(['chat-streamd-v2','--release-checkout',str(Path(root)/'source'),
                       '--deployer-stream-id','synthetic-owner',*args])
    finally:
        o.OrdinaryRuntime, d._run = original, runner
