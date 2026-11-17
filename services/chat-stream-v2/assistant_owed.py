"""Composite-owned follow-up records, separate from immutable publications."""
from __future__ import annotations
import json
import os
import re
import time
import uuid
import unicodedata
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from retention import history_query

FIELDS = ('owes', 'owes_due', 'owes_spec', 'owes_lane', 'closes')
DDL = '''CREATE TABLE IF NOT EXISTS v2_assistant_owed (
 owed_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL, label TEXT NOT NULL,
 source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_ids TEXT NOT NULL,
 opened_by TEXT NOT NULL DEFAULT '', due_at TEXT, spec_id TEXT, lane_id TEXT,
 state TEXT NOT NULL DEFAULT 'open', actor_stream_id TEXT NOT NULL,
 actor_generation TEXT NOT NULL, created_at TEXT NOT NULL, source_at TEXT NOT NULL,
 closed_at TEXT, closed_kind TEXT, closed_ref TEXT, closed_reason TEXT, due_notice_at TEXT)'''


def initialize(conn):
    conn.execute(DDL)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_assistant_owed_open ON v2_assistant_owed(stream_id,state,due_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_assistant_owed_publication ON v2_assistant_owed(opened_by,closed_ref)')


def stamp(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now,timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def instant(value):
    if not isinstance(value,str): raise ValueError('assistant_owed_due_invalid')
    try:
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        if d.tzinfo is None: raise ValueError()
        return d.astimezone(timezone.utc)
    except ValueError as exc: raise ValueError('assistant_owed_due_invalid') from exc


def due(value,now,*,past=False):
    if value is None:return None
    match=re.fullmatch(r'\+(\d+)([mhd])',value) if isinstance(value,str) else None
    d=(instant(now)+timedelta(seconds=int(match[1])*{'m':60,'h':3600,'d':86400}[match[2]])) if match else instant(value)
    if not past and d<=instant(now):raise ValueError('assistant_owed_due_invalid')
    return d.isoformat(timespec='microseconds').replace('+00:00','Z')


def text(value,minimum=1,maximum=160):
    if (not isinstance(value,str) or not minimum<=len(value.strip())<=maximum
            or any(unicodedata.category(c).startswith('C') for c in value)):
        raise ValueError('assistant_owed_text_invalid')
    return value.strip()


def rowdict(row):
    if row is None:raise ValueError('assistant_owed_not_found')
    d=dict(row);d['source_ids']=json.loads(d['source_ids']);return d


def get(conn,stream,oid):
    return rowdict(conn.execute('SELECT * FROM v2_assistant_owed WHERE stream_id=? AND owed_id=?',(stream,oid)).fetchone())


def route(conn,stream,source):
    return history_query(conn,'SELECT * FROM v2_assistant_composite_routes WHERE stream_id=? AND input_identity=? LIMIT 1',(stream,source)).fetchone()


def operator_route(conn,stream,source):
    r=route(conn,stream,source)
    return bool(r and str(r['actor_stream_id'] or '').startswith('operator:') and not json.loads(r['route_json'] or '{}').get('desk_dispatch'))


def publication(conn,stream,key):
    r=conn.execute('SELECT * FROM v2_assistant_composite_publications WHERE stream_id=? AND publication_key=?',(stream,key)).fetchone()
    if r is None:raise ValueError('assistant_owed_source_invalid')
    return dict(r)


def source(conn,stream,kind,key):
    if kind=='message':
        r=route(conn,stream,key)
        if not operator_route(conn,stream,key):raise ValueError('assistant_owed_source_invalid')
        return dict(r)
    if kind=='publication':return publication(conn,stream,key)
    raise ValueError('assistant_owed_source_invalid')


def open_item(conn,stream,args,actor,generation,now,*,opened_by='',source_ids=None):
    label=text(args.get('label'))
    if bool(args.get('message_id'))==bool(args.get('publication_key')):raise ValueError('assistant_owed_source_invalid')
    kind='message' if args.get('message_id') else 'publication';key=args.get('message_id') or args.get('publication_key')
    src=source(conn,stream,kind,key)
    if args.get('spec_id') is not None and args.get('lane_id') is not None:raise ValueError('assistant_owed_target_invalid')
    spec=text(args['spec_id']) if args.get('spec_id') is not None else None
    lane=text(args['lane_id']) if args.get('lane_id') is not None else None
    if not opened_by:
        prior=conn.execute('SELECT * FROM v2_assistant_owed WHERE stream_id=? AND source_kind=? AND source_id=? AND label=? ORDER BY created_at LIMIT 1',(stream,kind,key,label)).fetchone()
        if prior:return rowdict(prior)
    deadline=due(args.get('due'),now)
    oid='owed-'+uuid.uuid4().hex[:12]
    conn.execute('INSERT INTO v2_assistant_owed(owed_id,stream_id,label,source_kind,source_id,source_ids,opened_by,due_at,spec_id,lane_id,actor_stream_id,actor_generation,created_at,source_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (oid,stream,label,kind,key,json.dumps(source_ids or ([key] if kind=='message' else [])),opened_by,deadline,spec,lane,actor or '',generation or '',now,src['created_at']))
    return get(conn,stream,oid)


def close_item(conn,stream,oid,kind,ref,reason,now):
    item=get(conn,stream,oid)
    if item['state']!='open':raise ValueError('assistant_owed_not_open')
    if kind=='void':reason=text(reason,20,300);ref=''
    elif kind=='delivered':
        evidence=publication(conn,stream,ref)
        if instant(evidence['created_at'])<=instant(item['source_at']):raise ValueError('assistant_owed_evidence_invalid')
    elif kind in {'withdrawn','superseded'}:
        evidence=source(conn,stream,'message',ref)
        if instant(evidence['created_at'])<=instant(item['source_at']):raise ValueError('assistant_owed_evidence_invalid')
    else:raise ValueError('assistant_owed_evidence_invalid')
    conn.execute("UPDATE v2_assistant_owed SET state='closed',closed_at=?,closed_kind=?,closed_ref=?,closed_reason=? WHERE owed_id=?",(now,kind,ref,reason,oid))
    return get(conn,stream,oid)


def publication_effects(conn,stream,key,payload,flags,actor,generation,*,duplicate=False):
    """Called after publication's duplicate lookup, within its write transaction."""
    opened=conn.execute('SELECT * FROM v2_assistant_owed WHERE stream_id=? AND opened_by=?',(stream,key)).fetchone()
    closed=[r[0] for r in conn.execute("SELECT owed_id FROM v2_assistant_owed WHERE stream_id=? AND closed_ref=? AND closed_kind='delivered' ORDER BY owed_id",(stream,key))]
    if duplicate:
        if flags:
            expected=None if not opened else dict(opened)
            label=flags.get('owes')
            replay_closes=flags.get('closes',[])
            if (not isinstance(replay_closes,list) or any(not isinstance(i,str) for i in replay_closes)
                    or ('owes' in flags and label is None)
                    or (expected and label in (None,'none'))
                    or (label not in (None,'none') and (not expected or text(label)!=expected['label']))
                    or sorted(set(replay_closes))!=closed):
                raise ValueError('assistant_publish_conflict')
            if expected and label is not None:
                if (due(flags.get('owes_due'),expected['created_at'],past=True)!=expected['due_at']
                    or flags.get('owes_spec')!=expected['spec_id'] or flags.get('owes_lane')!=expected['lane_id']):
                    raise ValueError('assistant_publish_conflict')
            elif any(flags.get(f) is not None for f in ('owes_due','owes_spec','owes_lane')):
                raise ValueError('assistant_publish_conflict')
        return {'owed_id':opened['owed_id'] if opened else None,'closed_owed_ids':closed}
    if 'owes' in flags and flags['owes'] != 'none':text(flags['owes'])
    final=payload.get('publish_kind')=='prose' and payload.get('response_state')=='final'
    head=payload.get('reply_to_message_id');ids=payload.get('reply_to_message_ids') or ([head] if head else [])
    required=(payload.get('dispatch_id') and final and ids and all(operator_route(conn,stream,i) for i in ids))
    if required and os.environ.get('PENTACLE_ASSISTANT_OWES_REQUIRED','').lower() in {'1','true','yes','on'} and 'owes' not in flags:
        raise ValueError('assistant_publish_owes_required: use --owes none or --owes "<label>"')
    if any(flags.get(f) is not None for f in ('owes_due','owes_spec','owes_lane')) and flags.get('owes') in (None,'none'):
        raise ValueError('assistant_owed_target_invalid')
    now=publication(conn,stream,key)['created_at']
    oid=None
    if flags.get('owes') not in (None,'none'):
        if payload.get('dispatch_id') and not final:raise ValueError('assistant_owed_publication_invalid')
        args={'label':flags['owes'],'due':flags.get('owes_due'),'spec_id':flags.get('owes_spec'),'lane_id':flags.get('owes_lane')}
        args['message_id' if head else 'publication_key']=head or key
        oid=open_item(conn,stream,args,actor,generation,now,opened_by=key,source_ids=ids)['owed_id']
    closes=flags.get('closes',[])
    if not isinstance(closes,list) or any(not isinstance(i,str) for i in closes):raise ValueError('assistant_owed_not_found')
    if closes and not (final or not payload.get('dispatch_id') and payload.get('note_type')=='result'):
        raise ValueError('assistant_owed_publication_invalid')
    for i in sorted(set(closes)):close_item(conn,stream,i,'delivered',key,None,now)
    return {'owed_id':oid,'closed_owed_ids':sorted(set(closes))}


class OwedItems:
    def __init__(self,composite):self.composite=composite;self.store=composite.store;self.server=None;self.outbound=None
    def binding(self,conn):
        from store_assistant_binding import _binding_conn
        return _binding_conn(conn,self.composite._env_binding(),name=self.composite.config.name,include_target=False)
    def authorize(self,conn,auth,write):
        if auth.get('operator_authenticated') is True and str(auth.get('operator_principal') or '').startswith('operator:'):return
        actor=auth.get('stream_id');generation=auth.get('session_generation')
        if not auth.get('token_verified') or auth.get('scoped_principal') or auth.get('dot_principal'):raise ValueError('assistant_owed_unauthorized')
        from store_routing import _assistant_actor_conn
        _assistant_actor_conn(conn,actor,generation)
        binding=self.binding(conn)
        if binding.get('stream_id')==actor and binding.get('generation')==generation:return
        rulings=getattr(self.server,'lane_rulings',None)
        if not write and rulings:
            advisor=rulings.binding_conn(conn)
            if advisor.get('state') in {'ready','same_primary'} and advisor.get('stream_id')==actor and advisor.get('generation')==generation:return
        raise ValueError('assistant_owed_unauthorized')
    async def request(self,action,args,auth):
        stream=self.composite.config.stream_id
        def op(conn):
            conn.execute('BEGIN IMMEDIATE')
            try:
                self.authorize(conn,auth,action in {'open','close'})
                now=stamp()
                if action=='open':result={'item':open_item(conn,stream,args,auth.get('stream_id'),auth.get('session_generation'),now)}
                elif action=='close':
                    options=[(k,args[k]) for k in ('delivered_by','withdrawn_by','superseded_by','void') if args.get(k)]
                    if len(options)!=1:raise ValueError('assistant_owed_evidence_invalid')
                    k,ref=options[0];kind={'delivered_by':'delivered','withdrawn_by':'withdrawn','superseded_by':'superseded','void':'void'}[k]
                    result={'item':close_item(conn,stream,args.get('owed_id'),kind,ref,args.get('reason'),now)}
                elif action=='show':
                    item=get(conn,stream,args.get('owed_id'));src=source(conn,stream,item['source_kind'],item['source_id'])
                    body=src.get('body') if item['source_kind']=='message' else json.loads(src['canonical_payload_json']).get('message','')
                    result={'item':item,'source_text':body}
                elif action=='list':
                    where="state='open'";params=[stream]
                    if args.get('void'):where="closed_kind='void'"
                    elif args.get('closed'):where="state='closed' AND closed_kind<>'void'"
                    if args.get('since'):
                        cutoff=due(args['since'],now,past=True);where+=' AND closed_at>=?';params.append(cutoff)
                    rows=conn.execute('SELECT * FROM v2_assistant_owed WHERE stream_id=? AND '+where+' ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,due_at,created_at,owed_id',params)
                    result={'items':[rowdict(r) for r in rows]}
                else:raise ValueError('assistant_owed_action_invalid')
                conn.commit();return result
            except BaseException:conn.rollback();raise
        return await self.store.submit(op)

    def attach(self,outbound):
        self.outbound=outbound
        if self not in outbound.owed:outbound.owed.append(self)
        # One dispatcher handles multiple composites through each row's source stream.
        outbound.register_kind('owed_due',guard=self.dispatch_guard,on_delivered=self.dispatch_delivered,lock_factory=self.delivery_locks)
    def owner(self,row):
        return next((x for x in self.outbound.owed if x.composite.config.stream_id==row['source_stream_id']),None)
    async def dispatch_guard(self,row):
        from outbound_notices import NoticeDecision
        owner=self.owner(row)
        return await owner.guard(row) if owner else NoticeDecision.terminal('assistant_owed_unavailable','composite unavailable')
    async def dispatch_delivered(self,row):
        owner=self.owner(row)
        if owner:await owner.delivered(row)
    @asynccontextmanager
    async def delivery_locks(self,row):
        async with self.store.routing_integrity_lifecycle_lock(row['recipient_stream_id']):yield
    async def tick(self):
        from store_routing import _insert_outbound_notice_conn
        from outbound_notices import ensure_notice_marker
        stream=self.composite.config.stream_id
        def op(conn):
            conn.execute('BEGIN IMMEDIATE')
            try:
                # Outbox completion is durable before its callback; recover that crash gap.
                conn.execute("UPDATE v2_assistant_owed SET due_notice_at=(SELECT min(delivered_at) FROM v2_outbound_notices n WHERE n.kind='owed_due' AND n.source_stream_id=v2_assistant_owed.stream_id AND n.episode_id=v2_assistant_owed.owed_id AND n.delivered_at IS NOT NULL) WHERE stream_id=? AND due_notice_at IS NULL",(stream,))
                binding=self.binding(conn);target=binding.get('stream_id');generation=binding.get('generation')
                if target and generation:
                    rows=conn.execute("SELECT * FROM v2_assistant_owed WHERE stream_id=? AND state='open' AND due_at<=? AND due_notice_at IS NULL ORDER BY due_at LIMIT 100",(stream,stamp())).fetchall()
                    for row in rows:
                        nid=f"owed_due:{row['owed_id']}:{target}:{generation}"
                        body=f"Owed item due: {row['owed_id']}\n{row['label']}\nDue: {row['due_at']}\nSource: {row['source_id']}"
                        _insert_outbound_notice_conn(conn,notice_id=nid,kind='owed_due',dedupe_key=nid,
                            recipient_stream_id=target,tell_id=nid,body=ensure_notice_marker(nid,body),source_stream_id=stream,
                            episode_id=row['owed_id'],metadata={'owed_id':row['owed_id'],'generation':generation})
                conn.commit()
            except BaseException:conn.rollback();raise
        await self.store.submit(op)
    async def guard(self,row):
        from outbound_notices import NoticeDecision
        def check(conn):
            binding=self.binding(conn)
            try:item=get(conn,self.composite.config.stream_id,row['episode_id'])
            except ValueError:return False
            meta=row['metadata'];meta=json.loads(meta) if isinstance(meta,str) else meta
            from store_routing import _assistant_actor_conn
            try:_assistant_actor_conn(conn,row['recipient_stream_id'],meta['generation'])
            except ValueError:return False
            return (item['state']=='open' and item['due_notice_at'] is None
                and binding.get('stream_id')==row['recipient_stream_id'] and binding.get('generation')==meta['generation'])
        if not await self.store.submit(check):return NoticeDecision.terminal('assistant_owed_stale','item closed or binding changed')
        return None
    async def delivered(self,row):
        def op(conn):
            conn.execute('UPDATE v2_assistant_owed SET due_notice_at=COALESCE(due_notice_at,?) WHERE owed_id=? AND stream_id=?',(stamp(),row['episode_id'],self.composite.config.stream_id));conn.commit()
        await self.store.submit(op)
