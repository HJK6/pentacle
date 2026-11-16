import sqlite3,json,time,datetime,hashlib,subprocess,argparse
from pathlib import Path
OUT=Path.cwd()
parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=OUT/'live-readback.json');parser.add_argument('--state-root',type=Path,required=True);parser.add_argument('--satellite',action='append',default=[]);args=parser.parse_args()
def ro(name):
 c=sqlite3.connect(f"file:{args.state_root.resolve() / (name + '.db')}?mode=ro",uri=True);c.execute('pragma query_only=ON');c.execute('pragma busy_timeout=2000');return c
c=ro('sessions'); keys=['event_push.target_sha','event_push.target_sha.previous']+['event_push.runtime.'+host for host in args.satellite]; raw=dict(c.execute('select k,v from kv where k in ('+','.join('?' for _ in keys)+')',keys));c.close()
pin={k:raw.get(k) for k in keys[:2]}; now=time.time()
hosts=[]
for host in args.satellite:
 v=json.loads(raw.get('event_push.runtime.'+host,'{}')); hosts.append(dict(host=host,**{k:v.get(k) for k in ['sha','pid','observed_at','observed_at_epoch']},age_seconds=now-v.get('observed_at_epoch',0),version_admitted=v.get('sha')==pin[keys[0]]))
n=ro('notifications');rows=n.execute("select notification_id,state,error_context,firing_count,last_fired_at from notifications where json_extract(error_context,'$.code')='pin_drift' order by notification_id").fetchall(); n.close()
facts=[]
for nid,state,ctx,count,last in rows:
 ctx=json.loads(ctx); facts.append(dict(notification_id=nid,state=state,condition=ctx.get('condition'),episode_id=ctx.get('episode_id'),firing_count=count,last_fired_at=last))
active_facts=[fact for fact in facts if fact['condition']=='active']
result=dict(read_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),query_mode='mode=ro; query_only=ON; exact keys; all pin_drift facts ordered by notification_id',pins=pin,satellites=hosts,pin_drift_facts=facts,active_pin_drift_facts=active_facts,pin_drift_census=dict(complete=True,total=len(facts),active=len(active_facts)))
args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
