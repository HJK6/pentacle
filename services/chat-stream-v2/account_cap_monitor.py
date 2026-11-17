"""One reminder and one primary wake per measured cap episode and seat generation."""
from __future__ import annotations

import hashlib
import json
import uuid

from account_pool_control import read,write,seat_key,pool_key,actionable_reading,USAGE_CAP_WARNING_MARGIN
from account_store import number,transaction
from outbound_notices import NoticeDecision,_epoch_from_iso

REMINDER="account_cap_reminder"
FLAG="account_cap_blocker"
WARNING="account_pool_cap_warning"
PROBLEM="account_pool_meter_problem"
RECOVERED="account_pool_meter_recovered"
POOL_NOTICES=(WARNING,PROBLEM,RECOVERED)


class CapMonitor:
    def __init__(self,runtime):
        self.runtime=runtime
        self.outbound=None
        self.sample_max_age=30  # Existing presence working-active TTL, set at wiring.

    def attach(self,outbound):
        self.outbound=outbound
        for kind in (REMINDER,FLAG,*POOL_NOTICES): outbound.register_kind(kind,guard=self.guard)

    @staticmethod
    def key(stream,generation):
        return f"account.cap-seat:{stream}:{generation}"

    async def state(self,stream,generation):
        return await self.runtime.run(lambda a:read(a,self.key(stream,generation)) or {})

    async def patch(self,stream,generation,values):
        def update(a):
            with transaction(a.conn):
                key=self.key(stream,generation); state=read(a,key) or {}
                state.update(values); write(a,key,state)
                return state
        return await self.runtime.run(update,settle_on_cancel=True)

    def fresh(self,row):
        stamp=row.get("capture_observed_at")
        return bool(row.get("status")=="open" and row.get("online") is True
            and row.get("pane_status")=="pane_alive" and row.get("session_generation")
            and row.get("capture_generation")==row["session_generation"] and row.get("capture_liveness")=="idle"
            and number(stamp) and 0<=self.runtime.clock()-stamp<=self.sample_max_age)

    async def suppressed(self,row):
        """Only a proved submitted receipt followed by a fresh idle can end suppression."""
        stream,generation=row['stream_id'],row['session_generation']
        state=await self.state(stream,generation)
        receipt=state.get("operator_input")
        if not receipt: return False
        boundary=state.get("idle_boundary") or {}
        if boundary.get("operator_id")==receipt["id"]: return False
        reason="operator_input_outcome_uncertain"
        submitted=None; wire=None; confirmed=False
        if receipt["kind"]=="tell":
            envelope=await self.runtime.store.get_tell_delivery(receipt["id"])
            delivery=(envelope or {}).get('delivery') or {}
            reply=(envelope or {}).get('reply') or {}
            if (reply.get('phase')=='not_started' and reply.get('delivery_status')=='not_landed'
                    and reply.get('submission_confirmed') is False): return False
            confirmed=reply.get('submission_confirmed') is True
            submitted=_epoch_from_iso(reply.get('delivery_ack_at') or delivery.get('delivered_at'))
            wire=delivery.get('text')
        else:
            def lookup(a):
                from retention import history_query
                raw=history_query(a.conn,"SELECT * FROM v2_send_receipts WHERE to_stream_id=? AND request_id=? "
                    "ORDER BY rowid DESC LIMIT 1",(stream,receipt['id'])).fetchone()
                return dict(raw) if raw else {}
            value=await self.runtime.run(lookup)
            meta=json.loads(value.get('meta_json') or '{}')
            if meta.get('pool_input_not_started'): return False
            confirmed=bool(value.get('submission_confirmed'))
            submitted=_epoch_from_iso(value.get('created_at'))
            wire=(meta.get('pool_input') or {}).get('wire_text')
        if confirmed and submitted is not None and wire and self.fresh(row) and row.get('working') is False:
            if row['capture_observed_at']>max(submitted,receipt['claimed_at']):
                try:
                    queued=await self.runtime.comms._prompt_in_native_queue(stream,row.get('provider'),wire,strict=True)
                except Exception:
                    queued=True  # Missing/uncertain evidence keeps suppression.
                if not queued:
                    def idle(a):
                        with transaction(a.conn):
                            key=self.key(stream,generation); current=read(a,key) or {}
                            if current.get('operator_input')!=receipt: return False
                            current['idle_boundary']={"operator_id":receipt['id'],"observed_at":row['capture_observed_at']}
                            current['suppression_reason']=None;write(a,key,current)
                            return True
                    if await self.runtime.run(idle,settle_on_cancel=True): return False
            reason="operator_input_waiting_for_idle_boundary"
        await self.patch(stream,generation,{"suppression_reason":reason})
        return True

    async def eligible(self,metadata,*,flag=False):
        runtime=self.runtime
        stream,generation=metadata['stream'],metadata['generation']
        row=runtime.sessions.get(stream) or {}
        if row.get('session_generation')!=generation or not self.fresh(row) or row.get('working') is not True:
            return False
        if (stream,generation) in await runtime.cap_exemptions(): return False
        def current(a):
            from account_pool_control import eligibility
            episode=read(a,f"account.cap-episode:{metadata['provider']}:{metadata['pool_id']}") or {}
            override=read(a,seat_key(stream,generation)) or {}
            return bool(episode.get('active') and episode.get('id')==metadata['episode_id'] and not override.get('enabled')
                and eligibility(a,metadata['provider'],metadata['pool_id'],now=runtime.clock())['code']=='account_pool_cap_reached')
        if not await runtime.run(current) or await self.suppressed(row): return False
        if flag:
            state=await self.state(stream,generation)
            reminded=state.get('reminded_at')
            if not number(reminded) or runtime.clock()-reminded<120 or row['capture_observed_at']<=reminded:
                return False
        return True

    async def guard(self,notice):
        metadata=notice.get('metadata') or {}
        if isinstance(metadata,str): metadata=json.loads(metadata)
        if notice['kind'] in POOL_NOTICES:
            return await self.pool_notice_guard(notice,metadata)
        def current(a):
            from store_assistant_binding import _seat_conn
            source=_seat_conn(a.conn,metadata['stream'])
            episode=read(a,f"account.cap-episode:{metadata['provider']}:{metadata['pool_id']}") or {}
            return bool(source and source['status']=='open' and source['session_generation']==metadata['generation']
                and episode.get('active') and episode.get('id')==metadata['episode_id'])
        if not await self.runtime.run(current):
            return NoticeDecision.terminal('cap_episode_or_generation_ended','retain historical notice')
        flag=notice['kind']==FLAG
        if flag:
            target=(notice['recipient_stream_id'],metadata.get('recipient_generation'))
            if target!=await self.runtime.primary_pair(): return NoticeDecision.terminal('primary_generation_changed','retain historical notice')
        if not await self.eligible(metadata,flag=flag):
            return NoticeDecision.defer('cap_notice_not_current','wait for fresh autonomous working evidence')
        return None

    async def pool_notice_guard(self,notice,metadata):
        pair=await self.runtime.primary_pair()
        if pair!=(notice['recipient_stream_id'],metadata['recipient_generation']):
            return NoticeDecision.terminal('primary_generation_changed','retain historical notice')
        def current(a):
            state=read(a,f"account.cap-pool:{metadata['provider']}:{metadata['pool_id']}") or {}
            if state.get('pending',{}).get(notice['kind'],{}).get('tell_id')!=notice['tell_id']:
                return 'pool_notice_episode_ended'
            pool=next((p for p in a.readback(now=self.runtime.clock())['quota_pools']
                if (p['provider'],p['pool_id'])==(metadata['provider'],metadata['pool_id'])),None)
            control=(pool or {}).get('cap')
            if (not control or control['blocked'] or control['stop_pct'] is None
                    or control['revision']!=metadata['control_revision']):
                return 'pool_control_changed'
            window=actionable_reading(a,pool,control,now=self.runtime.clock())
            if notice['kind']==WARNING:
                if window is None: return 'pool_reading_unavailable'
                margin=a.policy.get('usage_cap_warning_margin',USAGE_CAP_WARNING_MARGIN)
                if not number(margin) or margin<0 or window['pct']<control['stop_pct']-margin:
                    return 'pool_warning_below_margin'
            elif notice['kind']==PROBLEM and window is not None:
                return 'pool_reading_recovered'
            elif notice['kind']==RECOVERED and window is None:
                return 'pool_reading_unavailable'
            return None
        reason=await self.runtime.run(current)
        if reason in {'pool_reading_unavailable','pool_warning_below_margin'}:
            return NoticeDecision.defer(reason,'wait for an actionable reading')
        if reason:
            return NoticeDecision.terminal(reason,'retain historical notice')
        return None

    async def pool_notices(self,pool):
        runtime=self.runtime
        primary=await runtime.primary_pair()
        if not primary: return
        delivered=set()
        prior=await runtime.run(lambda a:read(a,f"account.cap-pool:{pool['provider']}:{pool['pool_id']}") or {})
        for item in prior.get('pending',{}).values():
            receipt=await runtime.store.get_tell_delivery(item['tell_id'])
            if ((receipt or {}).get('reply') or {}).get('submission_confirmed') is True:
                delivered.add(item['tell_id'])
        def update(a):
            with transaction(a.conn):
                key=f"account.cap-pool:{pool['provider']}:{pool['pool_id']}"
                state=read(a,key) or {}
                control=pool['cap']
                revision=(control or {}).get('revision')
                if state.get('revision')!=revision:
                    state={'revision':revision}
                pending=state.setdefault('pending',{})
                def notice(kind,body):
                    tell_id='cap-pool-'+uuid.uuid4().hex
                    pending[kind]={'kind':kind,'dedupe_key':tell_id,'tell_id':tell_id,
                        'recipient_stream_id':primary[0], 'body':body,
                        'metadata':{'provider':pool['provider'],'pool_id':pool['pool_id'],
                                    'control_revision':revision,'recipient_generation':primary[1]}}
                if not control or control['blocked'] or control['stop_pct'] is None:
                    state={'revision':revision,'pending':{}}
                else:
                    window=actionable_reading(a,pool,control,now=runtime.clock())
                    label=f"{pool['provider']}/{pool['pool_id']}"
                    if window is None:
                        pending.pop(RECOVERED,None)
                        if not state.get('unavailable'):
                            notice(PROBLEM,f"Pool {label} reading unavailable; last reading age {pool.get('age_seconds')} seconds; "
                                   f"collection error {pool.get('error_codes') or 'no healthy current reading'}")
                            state['unavailable']=True
                    else:
                        if state.get('unavailable'):
                            pending.pop(PROBLEM,None)
                            notice(RECOVERED,f"Pool {label} healthy reading returned at {window['pct']} percent")
                            state['unavailable']=False
                        margin=a.policy.get('usage_cap_warning_margin',USAGE_CAP_WARNING_MARGIN)
                        if number(margin) and margin>=0:
                            if window['pct']<control['stop_pct']-margin:
                                state['warned']=False
                                pending.pop(WARNING,None)
                            elif not state.get('warned'):
                                notice(WARNING,f"Pool {label} reading {window['pct']} percent; cap {control['stop_pct']} percent; "
                                       f"reset at {window['reset_at']}")
                                state['warned']=True
                for item in state['pending'].values():
                    if (item['tell_id'] not in delivered and
                            (item['recipient_stream_id'],item['metadata']['recipient_generation'])!=primary):
                        tell_id='cap-pool-'+uuid.uuid4().hex
                        item.update(tell_id=tell_id,dedupe_key=tell_id,recipient_stream_id=primary[0])
                        item['metadata']['recipient_generation']=primary[1]
                write(a,key,state)
                return list(state['pending'].values())
        for notice in await runtime.run(update,settle_on_cancel=True):
            await self.outbound.enqueue(**notice)

    async def tick(self):
        if self.outbound is None: return
        runtime=self.runtime
        pools=(await runtime.run(lambda a:a.readback(now=runtime.clock())))['quota_pools']
        for pool in pools:
            await self.pool_notices(pool)
            control=pool['cap']
            episode_key=f"account.cap-episode:{pool['provider']}:{pool['pool_id']}"
            def episode(a):
                with transaction(a.conn):
                    value=read(a,episode_key) or {}
                    if not control or control['stop_pct'] is None:
                        value['active']=False
                    else:
                        window=actionable_reading(a,pool,control,now=runtime.clock())
                        if isinstance(window,dict) and number(window.get('pct')):
                            hit=window['pct']>=control['stop_pct']
                            if hit and not value.get('active'): value={'active':True,'id':uuid.uuid4().hex}
                            elif not hit: value['active']=False
                    write(a,episode_key,value)
                    return value
            value=await runtime.run(episode,settle_on_cancel=True)
            if not value.get('active'): continue
            for row in runtime.sessions.list_open():
                binding=row.get('account_binding') or {}
                if (binding.get('provider'),binding.get('pool_id'))!=(pool['provider'],pool['pool_id']): continue
                stream,generation=row['stream_id'],row['session_generation']
                metadata={'stream':stream,'generation':generation,'provider':pool['provider'],
                          'pool_id':pool['pool_id'],'episode_id':value['id']}
                # Observe idle even when the seat is not eligible for an alert.
                await self.suppressed(row)
                if not await self.eligible(metadata): continue
                state=await self.state(stream,generation)
                if state.get('flag_id'):
                    old=await runtime.store.outbound_notice_for_dedupe(state['flag_id'])
                    if old and old.get('terminal_reason')=='primary_generation_changed' and not await runtime.store.get_tell_delivery(state['flag_id']):
                        state=await self.patch(stream,generation,{'flag_id':None})
                digest=hashlib.sha256((value['id']+stream+generation).encode()).hexdigest()[:32]
                reminder_id='cap-reminder-'+digest
                if state.get('episode_id')!=value['id']:
                    state=await self.patch(stream,generation,dict(metadata,reminder_id=reminder_id,reminded_at=None,flag_id=None))
                receipt=await runtime.store.get_tell_delivery(reminder_id)
                reply=(receipt or {}).get('reply') or {}
                if reply.get('submission_confirmed') is True and state.get('reminded_at') is None:
                    stamp=_epoch_from_iso(reply.get('delivery_ack_at'))
                    if stamp is not None: state=await self.patch(stream,generation,{'reminded_at':stamp})
                if state.get('reminded_at') is None:
                    await self.outbound.enqueue(kind=REMINDER,dedupe_key=reminder_id,tell_id=reminder_id,
                        recipient_stream_id=stream,metadata=metadata,
                        body='Pool cap reached. Checkpoint and stop autonomous work; report any blocker to the primary assistant.')
                elif not state.get('flag_id') and await self.eligible(metadata,flag=True):
                    primary=await runtime.primary_pair()
                    if not primary: continue
                    target=hashlib.sha256((primary[0]+primary[1]).encode()).hexdigest()[:12]
                    flag_id='cap-blocker-'+digest+'-'+target
                    await self.outbound.enqueue(kind=FLAG,dedupe_key=flag_id,tell_id=flag_id,
                        recipient_stream_id=primary[0],source_stream_id=stream,
                        metadata=dict(metadata,recipient_generation=primary[1]),
                        body=f'BLOCKER {stream} is still working at its pool cap, with fresh evidence 120 seconds after the submitted reminder.')
                    await self.patch(stream,generation,{'flag_id':flag_id})
