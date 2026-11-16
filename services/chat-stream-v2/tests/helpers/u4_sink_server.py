#!/usr/bin/env python3
"""Owned portable HTTP sink; no private transport, recipient or credentials."""
import argparse,http.server,json,os,threading,time
from pathlib import Path
class Sink:
 def __init__(self,receipt):self.receipt=receipt;self.rows={};self.lock=threading.Lock()
 def submit(self,body):
  if set(body)!={'episode_id','condition','cause'} or body['condition'] not in ('active','recovered') or not all(isinstance(v,str) and 0<len(v)<=128 for v in body.values()):raise ValueError('invalid_sink_payload')
  key=(body['episode_id'],body['condition'])
  with self.lock:
   previous=self.rows.get(key)
   if previous:
    if previous['payload']!=body:raise ValueError('sink_payload_conflict')
    return previous['receipt']
   response={'sid':f'SM-fixture-{len(self.rows)+1}','status':'queued'}
   self.rows[key]={'payload':body,'receipt':response,'accepted_mono_s':time.monotonic()}
   tmp=self.receipt.with_suffix('.tmp');tmp.write_text(json.dumps(list(self.rows.values()),indent=2)+'\n');os.chmod(tmp,0o600);tmp.replace(self.receipt);return response
 def server(self):
  sink=self
  class Handler(http.server.BaseHTTPRequestHandler):
   def do_POST(self):
    try:
     size=int(self.headers.get('Content-Length','0'))
     if self.path!='/submission' or not 0<size<=4096:raise ValueError('invalid_request')
     response=sink.submit(json.loads(self.rfile.read(size)));code=200
    except (ValueError,KeyError,TypeError):response={'error':'sink_refused'};code=409
    data=json.dumps(response).encode();self.send_response(code);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
   def log_message(self,*args):pass
  return http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--receipt',type=Path,required=True);a=p.parse_args();server=Sink(a.receipt).server();print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/submission'}),flush=True)
 try:server.serve_forever()
 finally:server.server_close()
