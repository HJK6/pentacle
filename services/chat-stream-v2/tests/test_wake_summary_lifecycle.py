"""Replay the compact authenticated inventory into the installed wake consumer."""
import json
import subprocess
from pathlib import Path

from server import Server


def test_summary_preserves_lifecycle_for_strict_wake_target():
    row = dict(stream_id='local:current', session_generation='g1', status='open',
               closed_at=None, pane_status='pane_alive', visibility='hidden', state='ready')
    root = Path(__file__).resolve().parents[3]
    script = r'''
const assert = require('node:assert/strict');
const {createWakeDelivery} = require('./renderer/wake_delivery');
const rows = JSON.parse(process.argv[1]);
(async () => {
  const status = {mode:'on',wake:{enabled:true,generation:'w1',pending_count:0}};
  async function check(sessions, available) {
    const helper = createWakeDelivery({
      config:{features:{mic:true},mic:{wakeTargetStreamId:'old'},chatStream:{}},
      getState:async()=>({connected:true,sessions}),
      getBinding:async()=>({source:'durable',stream_id:'local:current',generation:'g1'}),
      api:()=>{throw Error('must not claim')}, sendTurn:()=>{throw Error('must not send')}
    });
    await helper.tick(status);
    assert.equal(helper.message(status).includes('No wake target'), !available);
  }
  await check(rows.open, true);
  await check(rows.closed, false);
  await check(rows.closedAt, false);
  await check(rows.missing, false);
  await check(rows.open.concat(rows.open), false);
  await check(rows.open.map(x=>({...x,session_generation:'wrong'})), false);
  await check(rows.open.map(x=>({...x,pane_status:'pane_dead'})), false);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    cases = dict(open=[row], closed=[{**row, 'status':'closed'}],
                 closedAt=[{**row, 'closed_at':'2026-09-28T00:00:00Z'}],
                 missing=[{k:v for k,v in row.items() if k not in ('status','closed_at')}])
    projected = {k:Server._summary_snapshot_sessions(v) for k,v in cases.items()}
    result = subprocess.run(['node','-e',script,json.dumps(projected)], cwd=root,
                            capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    assert projected['open'][0]['status'] == 'open'
    assert projected['closedAt'][0]['closed_at'] == cases['closedAt'][0]['closed_at']
