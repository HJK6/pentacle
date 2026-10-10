"""Packet predicates: exact watcher expectation and process-bound interpreter evidence."""
import hashlib,json,os,plistlib,re,subprocess,sys
from pathlib import Path

def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def validate_process(facts,argv,expected_hash,actual_hash,expected_pid,expected_start):
 assert actual_hash==expected_hash,'launch config hash changed'
 assert facts['pid']==expected_pid and facts['start_before'].strip()==facts['start_after'].strip()==expected_start.strip(),'PID/generation changed'
 intended=Path(argv[0]); base=intended.resolve(); main=Path(argv[2] if argv[1]=='-u' else argv[1])
 assert intended.parent.name=='bin' and intended.parent.parent.name=='.venv' or facts.get('private_rehearsal'),'unexpected venv prefix'
 assert facts['launch_program']==str(intended),'loaded launch program differs'
 assert facts['launch_arguments']==argv,'loaded launch arguments differ'
 assert str(base)==facts['resolved_interpreter'],'wrong resolved interpreter'
 assert str(main) in facts['command'],'wrong main'
 # macOS can print the framework launcher instead of the venv symlink.
 framework=base.parent.parent/'Resources/Python.app/Contents/MacOS/Python'
 allowed={str(intended),str(base)}
 if framework.is_file():allowed.add(str(framework.resolve()))
 assert facts['command'].split()[0] in allowed,'wrong process executable'
 root=str(intended.parent.parent.resolve())+'/lib/'
 paths=facts['open_files']; assert any(p.startswith(root) and '/site-packages/' in p for p in paths),'no process-bound intended-venv module'
 other=[p for p in paths if '/site-packages/' in p and not p.startswith(root)]
 assert not other,('contradictory other-venv modules',other)
 return {'passed':True,'pid':expected_pid,'start':expected_start.strip(),'venv':str(intended.parent.parent),'resolved_interpreter':str(base),'launch_config_sha256':actual_hash,'venv_open_files':[p for p in paths if p.startswith(root)]}

def collect(pid,argv,launch_program,launch_arguments,private_rehearsal=False):
 def ps(field):return subprocess.check_output(['ps','-p',str(pid),'-o',field+'='],text=True).strip()
 start=ps('lstart'); command=ps('command')
 raw=subprocess.check_output(['/usr/sbin/lsof','-a','-p',str(pid),'-Fn'],text=True)
 return {'pid':pid,'start_before':start,'start_after':ps('lstart'),'command':command,'open_files':[s[1:] for s in raw.splitlines() if s.startswith('n')],'resolved_interpreter':str(Path(argv[0]).resolve()),'launch_program':launch_program,'launch_arguments':launch_arguments,'private_rehearsal':private_rehearsal}

def watchdog_expectation(proof):
 if proof is None:return False
 p=Path(proof);r=json.loads(p.read_text());policy=Path(r['policy_path'])
 assert r['policy_sha256']==digest(policy)
 dep=Path(r['dependency_path']);fallback=Path(r['fallback_path'])
 assert r['dependency_sha256']==digest(dep) and r['fallback_sha256']==digest(fallback)
 d=json.loads(dep.read_text());f=json.loads(fallback.read_text())
 return (json.loads(policy.read_text())['watchdog']=='absent' and json.loads(policy.read_text()).get('overlay')=='none' and f.get('dependency_sha256')==digest(dep) and d['watchdog_importable'] is False and d['watchdog_installed'] is False and d['overlay_empty'] is True and f['passed'] is True and f['polling_observed'] is True)

def main():
 pid=int(sys.argv[1]);plist=Path(sys.argv[2]);preimage=Path(sys.argv[3]);out=Path(sys.argv[4]);expected_start=sys.argv[5]
 runtime=Path(sys.argv[6]);manifest=Path(sys.argv[7])
 receipt={'result':{'passed':False},'facts':{}}
 try:
  sys.path.insert(0,str(Path(__file__).resolve().parents[2]));from deploy import deploy
  actual_manifest=deploy.runtime_manifest(runtime,deploy.SERVICES['chat-streamd-v2'])
  receipt['runtime_manifest']=actual_manifest
  assert actual_manifest==json.loads(manifest.read_text()),'installed runtime manifest differs'
  config=plistlib.loads(plist.read_bytes());argv=config['ProgramArguments']
  raw=subprocess.check_output(['launchctl','print',os.environ['DOMAIN']+'/'+config['Label']],text=True)
  assert re.search(r'^\s*pid = '+str(pid)+r'\s*$',raw,re.M),'loaded PID differs'
  program=re.search(r'^\s*program = (.+)$',raw,re.M);assert program,'loaded program missing'
  block=re.search(r'\n\s*arguments = \{\n(.*?)\n\s*\}',raw,re.S);assert block,'loaded arguments missing'
  loaded=[line.strip() for line in block[1].splitlines() if line.strip()]
  receipt['facts']=collect(pid,argv,program[1].strip(),loaded)
  receipt['result']=validate_process(receipt['facts'],argv,digest(preimage),digest(plist),pid,expected_start)
 except (AssertionError,OSError,ValueError,KeyError,IndexError,subprocess.SubprocessError) as error:
  receipt['result']={'passed':False,'predicate':str(error)}
 finally:
  out.write_text(json.dumps(receipt,indent=2)+'\n')
 print('PROCESS_BOUND_INTERPRETER_PASS' if receipt['result']['passed'] else 'PROCESS_BOUND_INTERPRETER_FAILED')
 return 0 if receipt['result']['passed'] else 8

if __name__=='__main__':raise SystemExit(main())
