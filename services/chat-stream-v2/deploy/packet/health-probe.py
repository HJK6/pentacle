"""Read-only activation probe. Failed assertions close normally before retrying.
30 attempts, 2s open, 3s welcome, 5s pong, 1s retry: inherited bounds unchanged.
"""
import json, os, sys, time
from datetime import datetime, timezone
from websockets.sync.client import connect
ATTEMPTS=[]
def now():return datetime.now(timezone.utc).isoformat()
def probe(expected,url):
 last=None
 for attempt in range(30):
  row={'attempt':attempt,'pid':os.getpid(),'start':now(),'endpoint':url,'connected':False,'failed':True};ATTEMPTS.append(row)
  value=None;failure=None
  try:
   with connect(url,open_timeout=2) as w:
    row.update(connected=True,local_address=w.local_address,remote_address=w.remote_address)
    try:
     welcome=json.loads(w.recv(timeout=3));row['welcome_received_utc']=now();row['welcome_conn_id']=welcome.get('conn_id');row['observed_runtime_sha']=welcome.get('runtime_sha')
     if welcome.get('runtime_sha')!=expected:raise AssertionError('runtime SHA mismatch')
     w.send(json.dumps({'type':'hello','client':'agent-orch','subscribe':{'snapshot':False}}))
     w.send(json.dumps({'type':'ping','request_id':'inc2-health'}))
     until=time.monotonic()+5
     while time.monotonic()<until:
      frame=json.loads(w.recv(timeout=max(.01,until-time.monotonic())))
      if frame.get('type')=='pong' and frame.get('request_id')=='inc2-health':
       value={'runtime_sha':expected,'welcome':'ok','pong':'ok','endpoint':url};break
     if value is None:raise AssertionError('missing pong')
    except Exception as error:
     failure=error
    finally:
     # sync close waits for the closing handshake and transport termination.
     # Never pass a readiness exception to __exit__ (which otherwise sends 1011).
     w.close(code=1000)
     row.update(close_code=w.close_code,close_sent_code=getattr(w.protocol.close_sent,'code',None),close_received_code=getattr(w.protocol.close_rcvd,'code',None))
   if failure is not None:raise failure
   if row['close_code']!=1000:raise AssertionError('abnormal transport close')
   row['failed']=False
   return value
  except Exception as error:
   last=type(error).__name__+':'+str(error);row['error_type']=type(error).__name__
  finally:row['end']=now()
  time.sleep(1)
 raise SystemExit('HEALTH_PROBE_FAILED '+str(last))
if __name__=='__main__':print(json.dumps(probe(sys.argv[1],sys.argv[2] if len(sys.argv)>2 else 'ws://127.0.0.1:7791')))
