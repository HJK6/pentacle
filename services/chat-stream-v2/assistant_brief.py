"""Read-only daemon facts and pure, bounded front-desk briefing composition."""
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

CAP_BYTES = 6000
ORDER = ('priorities','owed','questions','rulings','wakes','usage','windows','lanes','seats')
CAPS = dict(priorities=12,owed=15,questions=8,rulings=8,wakes=10,usage=None,windows=24,lanes=12,seats=10)
LABELS = dict(zip(ORDER,('Priorities','Owed','Operator questions','Advisor rulings','Wakes','Usage','Windows','Lanes','Seats')))
COMMANDS = ('Commands: agent-orch assistant priorities list; agent-orch assistant owed list; agent-orch prompt list --open; '
            'agent-orch wake list; agent-orch account status; '
            'agent-orch hold list; agent-orch screen status; agent-orch spawn freeze --help; '
            'agent-orch work-lane list; agent-orch list; agent-orch assistant brief; '
            'sqlite3 -readonly <daemon-db> "SELECT * FROM v2_assistant_lane_rulings; '
            'SELECT * FROM v2_watch_wake; SELECT * FROM v2_spawn_admission_hold;"')

def clip(value, limit=180):
    return ' '.join(str(value if value is not None else '-').split())[:limit]

def epoch(value):
    if isinstance(value,(int,float)): return float(value)
    return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp() if value else 0

def utc(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')

@dataclass
class Section:
    head: str
    lines: list[str]
    def render(self): return [self.head,*self.lines]

def compose(facts):
    """No clock, I/O, or mutation; drop item lines, never counts or commands."""
    sections = {n:Section(facts['sections'][n].head,list(facts['sections'][n].lines[:CAPS[n]])) for n in ORDER}
    def render():
        return '\n'.join([f"[front desk brief] {facts['at']} reason={facts['reason']}",
                          *(line for n in ORDER for line in sections[n].render()),COMMANDS])
    text=render()
    for n in reversed(ORDER):
        while len(text.encode())>CAP_BYTES and sections[n].lines:
            sections[n].lines.pop();text=render()
    return text

class Brief:
    def __init__(self,store,composite,server=None,notify=None,clock=time.time):
        self.store,self.composite,self.server=store,composite,server
        self.notify,self.clock=notify,clock
        self.builders={n:getattr(self,n) for n in ORDER}
    async def rows(self,sql,args=()):
        return await self.store.submit(lambda c:[dict(r) for r in c.execute(sql,args)])
    async def gather(self,reason,binding=None):
        now=self.clock()
        if binding is None: binding=await self.composite.binding()
        async def one(n):
            try: return await asyncio.wait_for(self.builders[n](binding,now),timeout=5)
            except Exception as exc: return Section(f'{LABELS[n]}: unavailable ({type(exc).__name__})',[])
        return dict(at=utc(now),reason=reason,sections=dict(zip(ORDER,await asyncio.gather(*(one(n) for n in ORDER)))))
    async def text(self,reason,binding=None): return compose(await self.gather(reason,binding))
    async def priorities(self,binding,now):
        from assistant_priorities import snapshot
        result = await self.store.submit(lambda c: snapshot(c,self.composite.config.stream_id))
        lines = [f"  {r['rank']}. {clip(r['label'])} owner {clip(r['owner'] or 'front desk')} source {clip(r['source'])}" for r in result['items'][:12]]
        return Section(f"Priorities: {len(result['items'])} open, revision {result['revision']}",lines)
    async def owed(self,binding,now):
        stream=self.composite.config.stream_id
        count=(await self.rows("SELECT COUNT(*) n,SUM(due_at IS NOT NULL AND julianday(due_at)<julianday(?)) overdue FROM v2_assistant_owed WHERE stream_id=? AND state='open'",(utc(now),stream)))[0]
        rows=await self.rows("SELECT owed_id,due_at,label,source_id,spec_id,lane_id FROM v2_assistant_owed WHERE stream_id=? AND state='open' ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,due_at,created_at,owed_id LIMIT 15",(stream,))
        return Section(f"Owed: {count['n']} open, {count['overdue'] or 0} overdue",[f"  {r['owed_id']} due {r['due_at'] or 'no due'} {clip(r['label'])} source {str(r['source_id'])[:8]} {clip(r['spec_id'] or r['lane_id'])}" for r in rows])
    async def questions(self,binding,now):
        from restart_notice import RestartNotice
        reader=self.restart_reader()
        rows=await reader._questions()
        return Section(f'Operator questions: {len(rows)} open',[f"  {clip(r['question_id'])} {clip(r.get('producer'))} {clip(r.get('spec_id'))} {clip(r.get('source'))}" for r in rows[:8]])
    def restart_reader(self):
        from restart_notice import RestartNotice
        reader = RestartNotice(store=self.store, composite=self.composite, deliver=None, probe=None,
                               notify=self.notify or getattr(self.server, 'notify', None), instance_id='brief')
        # Restart tolerates absent optional tables; a brief must expose failures.
        reader._rows = self.rows
        return reader

    async def rulings(self,binding,now):
        data = await self.restart_reader()._rulings(limit=8)
        return Section(f"Advisor rulings: {data['pending_count']} pending", [
            f"  {clip(r['ruling_request_id'])} {clip(r['action'])} requester={clip(r['requester_stream_id'])} target={clip(r['target_stream_id'])}"
            for r in data['pending']])

    async def previous_binding(self,binding):
        current=(binding.get('stream_id'),binding.get('generation'));candidates=[]
        def add(at,old):
            if (old.get('stream_id'),old.get('generation'))!=current and old.get('generation'): candidates.append((epoch(at),old))
        rows=await self.rows("SELECT old_binding_json,created_at FROM v2_assistant_rebind_audit WHERE outcome='ok' AND json_extract(new_binding_json,'$.stream_id')=? AND json_extract(new_binding_json,'$.generation')=? AND (json_extract(old_binding_json,'$.stream_id')<>? OR json_extract(old_binding_json,'$.generation')<>?) ORDER BY created_at DESC LIMIT 1",(*current,*current))
        for r in rows:add(r['created_at'],json.loads(r['old_binding_json']))
        rows=await self.rows("SELECT stream_id,generation,updated_at FROM v2_assistant_restore_episode WHERE name=? AND stream_id=? AND last_generation=? AND state IN ('bound','restored') AND generation<>? ORDER BY updated_at DESC LIMIT 1",(self.composite.config.name,*current,current[1]))
        for r in rows:add(r['updated_at'],r)
        rows=await self.rows("SELECT s.source,s.source_generation,COALESCE(json_extract(t.data,'$.receipt.committed_at'),(SELECT MAX(a.created_at) FROM v2_host_transfer_audit a WHERE a.command_id=t.command_id AND a.event='committed')) committed_at FROM v2_host_transfer_seats s JOIN v2_host_transfers t ON t.command_id=s.command_id WHERE s.target=? AND s.target_generation=? AND t.state IN ('committed','complete') ORDER BY CASE WHEN typeof(committed_at) IN ('integer','real') THEN committed_at ELSE (julianday(committed_at)-2440587.5)*86400 END DESC LIMIT 1",current)
        for r in rows:add(r['committed_at'],dict(stream_id=r['source'],generation=r['source_generation']))
        return max(candidates,key=lambda x:x[0])[1] if candidates else None
    async def wakes(self,binding,now):
        args=(binding.get('stream_id'),binding.get('generation'))
        n=(await self.rows("SELECT COUNT(*) n FROM v2_watch_wake WHERE kind='wake' AND state='active' AND owner=? AND owner_generation=?",args))[0]['n']
        rows=await self.rows("SELECT id,data FROM v2_watch_wake WHERE kind='wake' AND state='active' AND owner=? AND owner_generation=? ORDER BY json_extract(data,'$.due_at') LIMIT 5",args)
        def line(r,prefix=''):
            data=json.loads(r['data']);return f"  {prefix}{r['id']} due {utc(data['due_at'])} {clip(data.get('note'))}"
        lines=[line(r) for r in rows];old=await self.previous_binding(binding);cancelled=[]
        if old:
            cancelled=await self.rows("""SELECT w.id,w.data FROM v2_watch_wake w WHERE w.kind='wake' AND w.state='cancelled' AND w.owner=? AND w.owner_generation=? AND json_extract(w.data,'$.due_at')>?
                AND NOT EXISTS (SELECT 1 FROM v2_watch_cancellations c WHERE c.target=w.id)
                AND NOT EXISTS (SELECT 1 FROM v2_watch_wake a WHERE a.kind='wake' AND a.state='active' AND a.owner=? AND a.owner_generation=? AND json_extract(a.data,'$.note')=json_extract(w.data,'$.note')) ORDER BY json_extract(w.data,'$.due_at') LIMIT 5""",(old['stream_id'],old['generation'],now,*args))
        return Section(f'Wakes: {n} armed; {len(cancelled)} previous-binding future wakes shown',lines+[line(r,'ended with previous binding: ') for r in cancelled])
    async def usage(self,binding,now):
        runtime=self.server.account_runtime
        data=await runtime.run(lambda a:a.readback(now=runtime.clock()));pools=data.get('quota_pools',[]);lines=[]
        for p in pools:
            cap=p.get('cap') or {};windows=(p.get('observation') or {}).get('windows') or {}
            window=cap.get('window') if cap.get('stop_pct') is not None else next((w for w in ('seven_day','codex:10080') if w in windows),None)
            pct=(windows.get(window) or {}).get('pct')
            limit='blocked' if cap.get('blocked') else (str(cap['stop_pct'])+'%' if cap.get('stop_pct') is not None else 'none')
            lines.append(f"  {clip(p.get('provider'))}/{clip(p.get('pool_id'))} cap {limit}; {window or 'weekly'} {pct if pct is not None else 'unknown'}%; fresh={bool(p.get('fresh'))}")
        return Section(f'Usage: {len(pools)} provider pools',lines)
    async def windows(self,binding,now):
        data = await self.restart_reader()._windows(now, limit=8)
        lines = []
        for kind, rows in ((n, data[n]) for n in ('admission', 'holds', 'screen')):
            for r in rows:
                lines.append(f"  {kind} {clip(r.get('resource') or r.get('host'))} owner {clip(r.get('owner_stream') or r.get('holder_stream_id'))} until {clip(r.get('expires_at') or r.get('held_until'))} {clip(r.get('reason'))}")
        return Section(f"Windows: {sum(data['counts'].values())} active", lines)

    async def lanes(self,binding,now):
        # Brief needs only stored lane state and live lead presence, not full
        # membership/report enrichment (which scales with the whole work board).
        counts = await self.rows("SELECT work_state,COUNT(*) n,SUM(parked_at IS NOT NULL) parked FROM v2_assistant_composite_lanes WHERE work_state IN ('active','blocked','paused') GROUP BY work_state")
        totals = {r['work_state']: r['n'] for r in counts}
        parked = sum(r['parked'] or 0 for r in counts)
        rows = await self.rows("SELECT * FROM v2_assistant_composite_lanes WHERE work_state IN ('active','blocked') AND parked_at IS NULL ORDER BY CASE work_state WHEN 'blocked' THEN 0 ELSE 1 END,updated_at DESC,lane_id LIMIT 12")
        from work_lanes_projection import project_lanes, presence_index, server_order
        from work_lane_progress import lane_state
        from store_work_lanes import work_lane_rows_conn
        # Legacy paused/lead_lost is a computed owner-loss fault. Inspect a
        # bounded exceptional set with the canonical enrichment, not all lanes.
        legacy = await self.rows("SELECT lane_id FROM v2_assistant_composite_lanes WHERE work_state='paused' AND work_state_reason='lead_lost' ORDER BY lane_id LIMIT 13")
        if len(legacy) > 12:
            raise RuntimeError('legacy_lane_limit')
        promoted = []
        for item in legacy:
            enriched = await self.store.submit(lambda c, ident=item['lane_id']: work_lane_rows_conn(c, lane_id=ident, external_config=getattr(self.store, 'external_work_config', None)))
            if enriched and lane_state(enriched[0])[0] == 'active':
                promoted.append(enriched[0])
                totals['paused'] -= 1
                totals['active'] = totals.get('active', 0) + 1
        projected = server_order(project_lanes(rows + promoted, presence_index(self.server.sessions.list_open()), utc(now)))[:12]
        lines = []
        for r in projected:
            lead = r.get('lead') or {}
            presence = lead.get('presence') or {}
            lines.append(f"  {clip(r['title'])} {r['state']} lead={clip(lead.get('stream_id'))} presence={clip(presence.get('capture_liveness'))} online={bool(presence.get('online'))} blocker={clip(r['blocker'])}")
        return Section(f"Lanes: {totals.get('active',0)+totals.get('blocked',0)} active/blocked; {totals.get('paused',0)} paused; {parked} parked", lines)

    async def seats(self,binding,now):
        rows=await self.store.children_of(binding.get('stream_id')) if binding.get('stream_id') else []
        return Section(f'Seats: {len(rows)} open children',[f"  {clip(r.get('stream_id'))} {clip(r.get('role'))} {clip(r.get('spec_id'))} {clip(r.get('title') or r.get('objective'))}" for r in rows[:10]])
