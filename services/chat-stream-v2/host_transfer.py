"""One fixed FD/advisor/helper migration, owned and recovered by the daemon."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import time

import store_host_transfer as journal
from sessions import VerbError


class HostTransfer:
    def __init__(self, server):
        self.server=server
        self.store=server.store
        self.tasks={}
        self.actor_locks={}
        self.retries={}
        self.stopping=False

    def actor_lock(self, actor):
        return self.actor_locks.setdefault(tuple(actor), asyncio.Lock())

    async def read(self,cid):
        return await self.store.submit(lambda c: journal.load(c,cid))

    async def mutate(self,fn,*,credential=False):
        registry=self.server.operator_credential_registry if credential else None
        return await self.store.submit(lambda c: journal.transaction(c,fn,registry))

    async def start(self):
        rows=await self.store.submit(lambda c: [r[0] for r in c.execute(
            "SELECT command_id FROM v2_host_transfers WHERE state IN ('preparing','committed') "
            "OR (state IN ('cancelled','failed','expired') AND COALESCE(json_extract(data,'$.cleanup_complete'),0)=0)")])
        for cid in rows: self.schedule(cid)

    async def stop(self):
        self.stopping=True
        for timer in self.retries.values(): timer.cancel()
        for task in self.tasks.values(): task.cancel()
        await asyncio.gather(*self.tasks.values(),return_exceptions=True)

    def retry(self,cid):
        if self.stopping: return
        previous=self.retries.pop(cid,None)
        if previous is not None: previous.cancel()
        self.retries[cid]=asyncio.get_running_loop().call_later(2,self.schedule,cid)

    def schedule(self,cid):
        if self.stopping or cid in self.tasks and not self.tasks[cid].done(): return
        # A source RPC context must never leak into daemon recovery work.
        token=journal.actor_context.set(None)
        try: self.tasks[cid]=asyncio.create_task(self.drive(cid))
        finally: journal.actor_context.reset(token)

    async def operation(self,msg):
        auth=msg.get('_auth_context') or {}
        cid=msg.get('command_id')
        action=msg.get('action','status')
        retired=auth.get('retired_transfer_owner')
        if retired:
            row=await self.read(cid)
            if not row or (row['actor'],row['actor_generation']) != (retired['stream_id'],retired['session_generation']):
                raise journal.TransferError('transfer_status_unauthorized')
            if action=='request':
                payload={k:msg.get(k) for k in ('dispatch_id','destination')}
                payload.update(members=sorted(msg.get('members') or []),pointers=msg.get('pointers',{}),intent='full-authority-transfer/v1')
                if journal.digest(payload)!=row['digest']:
                    raise journal.TransferError('transfer_command_conflict')
            elif action!='status': raise journal.TransferError('transfer_status_unauthorized')
            return {'type':'assistant.transfer.ok','command':row,'seats':await self.store.submit(lambda c:journal.seats(c,cid))}
        composite=self.server._composite_for_message(msg)
        if composite is None or not composite.enabled or not composite.config.direct_primary:
            raise journal.TransferError('transfer_composite_unavailable')
        if action=='request':
            hosts=self.server.hosts
            if hosts is None: raise journal.TransferError('transfer_hosts_unavailable')
            advisor=await self.store.get('assistant.authority.stream_id')
            if advisor is None: advisor=composite.env_config.authority_stream_id
            helpers={k:getattr(composite.config,k) for k in ('luna_stream_id',)
                     if getattr(composite.config,k)}
            row=await self.mutate(lambda c: journal.claim(c,msg,auth,
                name=composite.env_config.name,composite=composite.config.stream_id,
                env_binding=composite._env_binding(),advisor=advisor,helpers=helpers,
                trusted_hosts={hosts.local_host,*hosts.peers},registry=self.server.operator_credential_registry),credential=True)
            self.schedule(cid)
        elif action=='ready':
            await self.mutate(lambda c: journal.ready(c,cid,auth,msg.get('pointer_digest')))
            row=await self.read(cid)
            self.schedule(cid)
        elif action=='cancel':
            async with self.server.sessions.assistant.authority_lock:
                row=await self.mutate(lambda c: journal.cancel(c,cid,auth))
            self.schedule(cid)
        elif action=='status':
            row=await self.read(cid)
            if not row: raise journal.TransferError('transfer_unknown')
            parts=await self.store.submit(lambda c: journal.seats(c,cid))
            pairs={(row['actor'],row['actor_generation']),*((p['target'],p['target_generation']) for p in parts)}
            if not (auth.get('token_verified') and (auth.get('stream_id'),auth.get('session_generation')) in pairs
                    or auth.get('operator_authenticated') and not auth.get('token_verified')
                    or auth.get('peer_loopback') and auth.get('local_admin_verified')):
                raise journal.TransferError('transfer_status_unauthorized')
        else: raise journal.TransferError('transfer_action_invalid')
        return {'type':'assistant.transfer.ok','command':row,
                'seats':await self.store.submit(lambda c: journal.seats(c,cid))}

    async def verify_pointers(self,row):
        hosts=self.server.hosts
        await hosts.ensure_reachable(row['destination'],'transfer')
        # Hash on the recorded host, never treat another host's path as local.
        for refs in row['pointers'].values():
            for ref in refs:
                await hosts.ensure_reachable(ref['host'],'transfer pointer')
                code,out=await hosts.run_command(ref['host'],'python3','-c',
                    'import hashlib,sys;print(hashlib.file_digest(open(sys.argv[1],"rb"),"sha256").hexdigest())',ref['path'])
                if code or out.strip()!=ref['sha256']:
                    raise journal.TransferError('transfer_pointer_unavailable')

    async def prepare(self,row):
        if self.server.spawnctl is None: raise journal.TransferError('transfer_spawn_unavailable')
        await self.verify_pointers(row)
        parts=await self.store.submit(lambda c: journal.seats(c,row['command_id']))
        for part in parts:
            if part['target_generation']: continue
            source=row['sources'][part['source']]
            host,name=part['target'].split(':',1)
            # No parent or handoff lineage until authority commit. A persistent
            # reservation fence already denies this name all ordinary seat RPCs.
            message={'type':'spawn','host':host,'session_name':name,
                'request_id':'transfer:'+row['command_id']+':'+name,
                'idempotency_key':'transfer:'+row['command_id']+':'+name,
                'provider':source['provider'],'model':source['effective_model'],
                'effort':source['effective_effort'],'role':'worker','visibility':'hidden',
                'objective':'Prepare the commissioned host transfer; await daemon authority commitment.',
                'no_watch':True,'self_close_on_completion':False,
                'initial_prompt':json.dumps({'migration':row['command_id'],'source':part['source'],
                    'pointers':row['pointers'].get(part['source'],[]),
                    'instructions':'Read and verify the recorded host/path/hash pointers. Use agent-orch assistant transfer ready with this command id and pointer digest. Until status says committed, only readiness and status are authorized.',
                    'pointer_digest':journal.digest(row['pointers'].get(part['source'],[]))}),
                '_auth_context':{'service_authenticated':True,'service_actor':'daemon:host-transfer'}}
            existing=await self.store.fetch_session(host,name)
            if existing is None:
                result=await self.server.spawnctl.spawn(message,self.server.local_host)
                if result.get('type')!='spawn.ok': raise journal.TransferError('transfer_preparation_failed')
            # Spawn may acknowledge admission before the provider is ready. Its
            # existing reservation/outcome remains the only spawn obligation.
            while True:
                fresh=await self.read(row['command_id'])
                if fresh['state']!='preparing': return
                journal._fresh(fresh,time.time())
                existing=await self.store.fetch_session(host,name)
                if existing and existing.get('bootstrap_state')=='ready': break
                if existing and existing.get('status')!='open': raise journal.TransferError('transfer_preparation_failed')
                outcome=await self.store.get_spawn_outcome(host,name)
                if outcome and outcome.get('state') in {'failed','cancelled'}:
                    raise journal.TransferError('transfer_preparation_failed')
                await asyncio.sleep(.1)
            await self.mutate(lambda c: journal.prepared(c,row['command_id'],part['target'],existing['session_generation']))

    async def commit(self,row):
        sessions=self.server.sessions
        composite=self.server._composite_for(row['composite'])
        if composite is None: raise journal.TransferError('transfer_composite_unavailable')
        # Drain producers before locks their effect paths acquire. Never wait
        # for a ruling release while holding authority/lifecycle/graph locks.
        async with AsyncExitStack() as stack:
            for sid in sorted(row['sources']):
                await stack.enter_async_context(self.actor_lock((sid,row['sources'][sid]['session_generation'])))
            rulings=self.server.lane_rulings
            if rulings is not None:
                for key in sorted(rulings._release_locks):
                    await stack.enter_async_context(rulings._release_locks[key])
            if self.server.notify is not None:
                await stack.enter_async_context(self.server.notify._host_transfer_lock)
            await stack.enter_async_context(composite._binding_lock)
            await stack.enter_async_context(sessions.assistant.lock)
            await stack.enter_async_context(sessions.assistant.authority_lock)
            parts=await self.store.submit(lambda c: journal.seats(c,row['command_id']))
            for sid in sorted({*row['sources'],*(p['target'] for p in parts)}):
                await stack.enter_async_context(sessions._lifecycle_lock(*sid.split(':',1)))
            await stack.enter_async_context(sessions._graph_lock)
            async def committed_and_published():
                result=await self.mutate(lambda c: journal.commit(c,row['command_id'],self.server.operator_credential_registry),credential=True)
                await composite.load_binding()
                await sessions.refresh()
                return result
            # Cancellation cannot leave a committed grant paired with a stale
            # routing cache in this running daemon. Restart reads durable state.
            task=asyncio.create_task(committed_and_published())
            try: return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def drive(self,cid):
        try:
            row=await self.read(cid)
            if row['state']=='preparing':
                await self.prepare(row)
                while True:
                    row=await self.read(cid)
                    if row['state']!='preparing': break
                    await self.store.submit(lambda c: journal.check_source(c,row,self.server.operator_credential_registry))
                    parts=await self.store.submit(lambda c: journal.seats(c,cid))
                    if all(p['ready'] for p in parts):
                        row=await self.commit(row)
                        break
                    await asyncio.sleep(.1)
            if row['state'] in journal.COMMITTED:
                await self.project_and_cleanup(row)
            elif row['state'] in journal.TERMINAL:
                await self.cleanup_preparations(row)
        except asyncio.CancelledError: raise
        except Exception as exc:
            def failed(c):
                row=journal.load(c,cid)
                row['last_error']=getattr(exc,'code',type(exc).__name__)
                if row['state']=='preparing': row['state']='expired' if row['expires_at']<=time.time() else 'failed'
                journal.save(c,row)
                journal.audit(c,cid,'reconcile_failed',detail=row['last_error'])
                return row
            row=await self.mutate(failed)
            if row['state'] not in journal.COMMITTED:
                await self.cleanup_preparations(row)
            else:
                self.retry(cid)

    async def cleanup_preparations(self,row):
        parts=await self.store.submit(lambda c: journal.seats(c,row['command_id']))
        outcomes={}
        for part in parts:
            host,name=part['target'].split(':',1)
            key='transfer:'+row['command_id']+':'+name
            try:
                if self.server.spawnctl is not None:
                    await self.server.spawnctl.spawn_cancel({'host':host,'target':key},self.server.local_host)
                pending=any(r['host']==host and r['session_name']==name
                            for r in await self.store.reservations(include_expired=True))
                if pending:
                    outcomes[part['target']]='spawn_cleanup_pending'
                    continue
                current=await self.store.fetch_session(host,name)
                if not current or current['status']!='open':
                    outcomes[part['target']]='closed_or_not_created'
                    continue
                generation=part['target_generation']
                if generation is None:
                    spawn=await self.store.get_spawn_outcome(host,name)
                    if not spawn or spawn.get('request_id')!=key:
                        outcomes[part['target']]='preparation_identity_unproved'
                        continue
                    generation=current['session_generation']
                result=await self.server.sessions.close(host,name,reason='cancelled host transfer preparation',
                    expected_generation=generation,operator_confirm=True)
                outcomes[part['target']]='closed' if not any(result.get(k) for k in ('failed','deferred','stale_generation')) else 'close_pending'
            except Exception as exc:
                outcomes[part['target']]=getattr(exc,'code',type(exc).__name__)
        complete=all(v in {'closed','closed_or_not_created'} for v in outcomes.values())
        def record(c):
            current=journal.load(c,row['command_id']);current.update(cleanup=outcomes,cleanup_complete=complete);journal.save(c,current)
        await self.mutate(record)
        if not complete: self.retry(row['command_id'])

    async def project_and_cleanup(self,row):
        # Implemented with exact-generation committed succession; no adoption
        # prompt and no inferred authority from an old process's liveness.
        parts=await self.store.submit(lambda c: journal.seats(c,row['command_id']))
        if self.server.notify is not None:
            await self.server.notify.project_host_transfer(parts)
        for part in parts:
            result=await self.server.sessions.close(*part['source'].split(':',1),
                reason='committed host transfer',expected_generation=part['source_generation'],operator_confirm=True)
            if result.get('failed') or result.get('deferred') or result.get('stale_generation'):
                raise journal.TransferError('transfer_cleanup_incomplete')
        def complete(c):
            current=journal.load(c,row['command_id']);current['state']='complete';current['cleanup_complete']=True;journal.save(c,current)
            journal.audit(c,row['command_id'],'complete')
        await self.mutate(complete)
