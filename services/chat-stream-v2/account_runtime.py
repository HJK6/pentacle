"""Default-OFF account controls on the existing authenticated daemon boundary."""
from __future__ import annotations
import asyncio
import json
import hashlib
import logging
import os
from pathlib import Path
import time
import uuid
from contextlib import asynccontextmanager

from account_store import AccountStore
from account_revalidation import interrupt_pending
from provider_accounts import AccountError, Binding, _component, policy_manages, validate_policy_scope
from sessions import VerbError
from error_adapters import ErrorFact

log=logging.getLogger("chat_streamd_v2.provider_accounts")


def load_policy(path=None):
    if not path: return {"enabled":False}
    path=Path(path); info=path.lstat()
    if path!=path.resolve() or info.st_uid!=os.getuid() or info.st_mode&0o777!=0o600:
        raise AccountError("account_policy_insecure")
    value=json.loads(path.read_text())
    validate_policy_scope(value)
    return value


class _PinInputBarrier:
    """Concurrent physical input; pin writes wait only for in-flight physical actions."""
    def __init__(self):
        self.condition=asyncio.Condition()
        self.readers=0;self.writer=False;self.waiting_writers=0

    @asynccontextmanager
    async def read(self):
        async with self.condition:
            await self.condition.wait_for(lambda:not self.writer and not self.waiting_writers)
            self.readers+=1
        try: yield
        finally:
            async with self.condition:
                self.readers-=1;self.condition.notify_all()

    @asynccontextmanager
    async def write(self):
        async with self.condition:
            self.waiting_writers+=1
            try:
                await self.condition.wait_for(lambda:not self.writer and not self.readers)
                self.writer=True
            finally:
                self.waiting_writers-=1;self.condition.notify_all()
        try: yield
        finally:
            async with self.condition:
                self.writer=False;self.condition.notify_all()


class AccountRuntime:
    def __init__(self,store,sessions,*,policy=None,clock=time.time,executor=None):
        self.store,self.sessions=store,sessions
        self.policy,self.clock,self.executor={"enabled":False} if policy is None else policy,clock,executor
        validate_policy_scope(self.policy)
        self.server=self.spawnctl=self.comms=None
        self._next_probe=0; self._collector_task=None; self._stopping=False
        self._health_epoch=uuid.uuid4().hex; self._health_started=False
        self.pin_lock=asyncio.Lock()
        self.pin_inputs=_PinInputBarrier()
        self._home_inputs={}
        from account_cap_monitor import CapMonitor
        self.cap_monitor=CapMonitor(self)

    def home_inputs(self,binding):
        from provider_cli_maintenance import scope
        return self._home_inputs.setdefault(scope(binding),_PinInputBarrier())

    async def maintenance_input(self,row):
        from provider_cli_maintenance import assert_input
        await self.run(lambda a:assert_input(a,row))

    @asynccontextmanager
    async def maintenance_guard(self,row):
        @asynccontextmanager
        async def home_read():
            if row.get("account_binding"):
                async with self.home_inputs(Binding.from_public(row["account_binding"])).read():yield
            else:yield
        async with home_read():
            await self.maintenance_input(row)
            yield

    def attach(self,server,spawnctl,comms):
        self.server,self.spawnctl,self.comms=server,spawnctl,comms
        server.account_runtime=spawnctl.account_runtime=comms.account_runtime=self
        server.handlers["account"]=self.operation

    async def run(self,function,*,settle_on_cancel=False):
        work=lambda conn:function(AccountStore(conn,self.policy,initialize=False))
        return await self.store.submit(work,settle_on_cancel=True) if settle_on_cancel else await self.store.submit(work)

    async def start(self):
        # This is the owned daemon-start boundary, not a Store connection open.
        # OFF only terminalizes old audit intents; it never restores health.
        if not self._health_started:
            from provider_cli_initial import startup as initial_startup
            await self.run(lambda ledger:initial_startup(ledger,self._health_epoch),settle_on_cancel=True)
            await self.run(lambda ledger:interrupt_pending(ledger,self._health_epoch,now=self.clock()))
            self._health_started=True
        if self.policy.get("enabled") is not True: return
        # Private verified enrollment receipts only. Never login on startup.
        for entry in self.policy.get("bindings",[]):
            binding=Binding.from_public(entry)
            if not self.manages(binding.host,binding.provider): continue
            present=await self.run(lambda accounts:accounts.bound(binding.key))
            if present is None:
                current=await self.run(lambda accounts:accounts._cell(binding.host,binding.provider,binding.alias))
                if current is None:
                    def register_if_clear(accounts):
                        from account_store import transaction
                        from account_repin_state import causes,generation_intents
                        value={"host":binding.host,"provider":binding.provider,"account":binding.alias}
                        with transaction(accounts.conn):
                            if accounts._cell(binding.host,binding.provider,binding.alias) is not None:return
                            if causes(accounts,value) or generation_intents(accounts,value):return
                            accounts._register(binding)
                    await self.run(register_if_clear,settle_on_cancel=True)

    async def stop(self):
        self._stopping=True
        if self._collector_task:
            self._collector_task.cancel()
            await asyncio.gather(self._collector_task,return_exceptions=True)

    async def authorize_choice(self,message):
        if "account" in message:
            _component(message["account"])
            auth=message.get("_auth_context") or {}
            # Choosing a launch account confers no spawn authority. The ordinary
            # lineage/spec/phase admission still runs before any provider work.
            # Administration deliberately keeps the independent FD-only guard.
            if auth.get("service_authenticated"):
                if await self._carried_choice(message,auth):
                    return
            elif auth.get("token_verified") is True:
                stream=str(auth.get("stream_id") or "")
                row=await self.store.fetch_session(*self.sessions.split(stream)) if ":" in stream else None
                if (row and row.get("status")=="open" and auth.get("session_generation")
                        and row.get("session_generation")==auth["session_generation"]):
                    return
            elif auth.get("operator_authenticated") is True:
                return
            raise AccountError("account_control_unauthorized")

    async def _carried_choice(self,message,auth):
        """Internal identities carry a recorded intent; they cannot choose aliases."""
        actor=auth.get("service_actor")
        alias=message["account"]
        key=message.get("idempotency_key")
        if actor=="daemon:scheduler":
            row=await self.store.submit(lambda c:c.execute(
                "SELECT s.account FROM v2_schedules s JOIN v2_schedule_dispatches d USING(schedule_id,generation) "
                "WHERE d.spawn_key=? AND s.state='firing' AND d.phase='dispatch_claimed'",(key,)).fetchone())
            if row is not None and row[0]==alias:
                return True
            source=str(message.get("handoff_from_stream_id") or "")
            if (message.get("handoff") and auth.get("token_verified") is True
                    and source==auth.get("stream_id") and ":" in source):
                row=await self.store.fetch_session(*self.sessions.split(source))
                return bool(row and row.get("session_generation")==auth.get("session_generation")
                    and (row.get("account_binding") or {}).get("alias")==alias)
        elif actor=="daemon:host-transfer":
            from store_host_transfer import load
            target=f"{message.get('host')}:{message.get('session_name')}"
            def retained(c):
                part=c.execute("SELECT command_id,source FROM v2_host_transfer_seats WHERE target=?",(target,)).fetchone()
                intent=load(c,part['command_id']) if part else None
                if not intent or intent['state']!='preparing' or key!=f"transfer:{part['command_id']}:{message.get('session_name')}":
                    return False
                value=intent['sources'][part['source']].get('account_binding')
                value=json.loads(value) if isinstance(value,str) else value
                return bool(value and value.get('alias')==alias)
            return await self.store.submit(retained)
        # Authority restoration freezes the complete spawn payload before dispatch.
        if actor=="daemon:scheduler" and str(key or "").startswith("authority-restore:"):
            def retained(c):
                row=c.execute("SELECT payload FROM v2_authority_restore_episode WHERE spawn_key=? AND state IN ('spawning','unresolved')",(key,)).fetchone()
                return bool(row and json.loads(row[0]).get('account')==alias)
            return await self.store.submit(retained)
        return False

    def manages(self,host,provider=None):
        return policy_manages(self.policy,host,provider)

    async def enrolled(self,host,provider=None):
        return await self.run(lambda a:a.conn.execute(
            "SELECT 1 FROM v2_account_bindings WHERE host=?" + (" AND provider=?" if provider else "") + " LIMIT 1",
            (host,provider) if provider else (host,)).fetchone() is not None)

    async def choose_launch(self,*,host,provider,model,effort,message):
        await self.authorize_choice(message)
        if await self.enrolled(host,provider) and not self.manages(host,provider):
            raise AccountError("provider_cli_pin_unavailable",host=host,provider=provider,reason="account_policy_off")
        if not self.manages(host,provider):
            if "account" in message or message.get("_account_resume_binding"):
                raise AccountError("account_feature_off")
            # OFF restores legacy admissions; retained rows remain for draining.
            if self.policy.get("enabled") is not True: return None
            managed=await self.run(lambda accounts:accounts.conn.execute(
                "SELECT 1 FROM v2_account_bindings WHERE host=? AND provider=? LIMIT 1",(host,provider)).fetchone() is not None)
            if managed: raise AccountError("account_feature_off")
            return None
        if message.get("command"): raise AccountError("managed_command_forbidden")
        if message.get("resume_session_id") and message.get("_account_resume_binding"):
            from account_repin import resume_binding
            binding=await self.run(lambda a:resume_binding(a,message["_account_resume_binding"]))
            if binding.host!=host or binding.provider!=provider or message.get("account",binding.alias)!=binding.alias:
                raise AccountError("account_resume_cross_home_forbidden")
            from provider_cli_pin import check
            await self.run(lambda a:check(a,binding))
            await self._pool_launch(binding,message)
            return binding
        if message.get("resume_session_id"):
            raise AccountError("account_resume_cross_home_forbidden")
        binding=await self.run(lambda accounts:accounts.select(host=host,provider=provider,alias=message.get("account")))
        from provider_cli_pin import check
        await self.run(lambda a:check(a,binding))
        await self._pool_launch(binding,message)
        log.info("subsystem=provider_accounts bug_ref=explicit_account_selection action=selected host=%s provider=%s account=%s generation=%s selection=%s",
                 host,provider,binding.alias,binding.generation,"explicit" if "account" in message else "default")
        return binding

    async def _control_actor(self,auth):
        from account_pool_control import operator
        if operator(auth): return {"principal":auth["operator_principal"]}
        await self._front_desk(auth)
        return {"stream_id":auth["stream_id"],"session_generation":auth["session_generation"]}

    def _control_authorizer(self,auth):
        """Re-read current assistant binding and generation inside the write transaction."""
        def check(conn):
            from account_pool_control import operator
            if operator(auth): return
            from store_assistant_binding import _binding_conn, _seat_conn
            composite=self.server.assistant_composite if self.server else None
            if not composite or not composite.enabled or not composite.env_config.direct_primary:
                raise AccountError("account_control_unauthorized")
            current=_binding_conn(conn,composite._env_binding(),name=composite.env_config.name)
            row=_seat_conn(conn,current["stream_id"])
            if (auth.get("token_verified") is not True or not row or row["status"]!="open"
                    or (auth.get("stream_id"),auth.get("session_generation"))!=(current["stream_id"],current["generation"])
                    or row["session_generation"]!=current["generation"]):
                raise AccountError("account_control_unauthorized")
        return check

    async def _pool_launch(self,binding,message):
        from account_pool_control import assert_eligible
        if message.get('_account_admitted_replay') is True: return
        override=None
        if "pool_override_reason" in message or "pool_authority_ref" in message:
            if any(not isinstance(message.get(k),str) or not message[k].strip()
                   for k in ("pool_override_reason","pool_authority_ref")):
                raise AccountError("account_request_invalid")
            actor=await self._control_actor(message.get("_auth_context") or {})
            def make(a):
                from account_pool_control import read,pool_key
                self._control_authorizer(message.get("_auth_context") or {})(a.conn)
                current=read(a,pool_key(binding.provider,binding.pool_id)) or {}
                return {"provider":binding.provider,"pool_id":binding.pool_id,"control_revision":current.get("revision",0),
                        "actor":actor,"reason":message["pool_override_reason"],"authority_ref":message["pool_authority_ref"]}
            override=await self.run(make)
            message["_pool_spawn_override"]=override
        await self.run(lambda a:assert_eligible(a,binding,now=self.clock(),override=override))

    async def bind_launch(self,row):
        if row.get("account_binding"): await self.run(lambda accounts:accounts.bind_session(row))

    async def admit_launch(self,row,*,message=None):
        """Read-only host verification, then one serialized durable launch decision."""
        if not row.get("account_binding"): return
        from provider_cli_pin import check, digest, canonical,consumer_current
        binding=Binding.from_public(row["account_binding"])
        pin=await self.run(lambda a:check(a,binding))
        proof=None
        if pin is not None:
            if self.executor is None: raise AccountError("provider_cli_verification_unavailable")
            async with self.executor._host_locks.setdefault(binding.host,asyncio.Lock()):
                proof=await self.executor.verify_artifact(binding,pin)
        async with self.pin_lock:
            def commit(a):
                from account_store import transaction
                with transaction(a.conn):
                    from account_pool_control import assert_eligible
                    current=check(a,binding)
                    hold=a.conn.execute("SELECT held_until FROM v2_spawn_admission_hold WHERE host=?",(binding.host,)).fetchone()
                    if hold and hold[0]>self.clock(): raise AccountError("provider_cli_pin_unavailable",reason="admission_hold")
                    consumer_current(a,binding.host,getattr(self.sessions,"local_host",None),self.clock())
                    if current!=pin: raise AccountError("provider_cli_pin_mismatch",reason="pin_changed_during_verification")
                    active=a._available(a._cell(binding.host,binding.provider,binding.alias))
                    if active.public()!=dict(binding.public(),default_revision=0):
                        raise AccountError("account_login_generation_stale")
                    if not self.manages(binding.host,binding.provider):
                        raise AccountError("provider_cli_pin_unavailable",reason="account_policy_off")
                    if binding.default_revision:
                        default=a.conn.execute("SELECT alias,revision FROM v2_account_defaults WHERE host=? AND provider=?",
                                               (binding.host,binding.provider)).fetchone()
                        if not default or tuple(default)!=(binding.alias,binding.default_revision):
                            raise AccountError("account_default_stale")
                    admission_key="provider_cli.admission:"+row["stream_id"]+":"+row["session_generation"]
                    admitted=a.conn.execute("SELECT v FROM kv WHERE k=?",(admission_key,)).fetchone()
                    override=(message or {}).get("_pool_spawn_override")
                    if not admitted:
                        if override: self._control_authorizer((message or {}).get("_auth_context") or {})(a.conn)
                        assert_eligible(a,binding,now=self.clock(),override=override)
                    a._bind_session(row)
                    key="provider_cli.admission:"+row["stream_id"]+":"+row["session_generation"]
                    data=({"revision":pin["revision"],"digest":digest(pin),"artifact":proof,
                           "binding_key":binding.key} if pin is not None else
                          {"mode":"pre_enforcement","mode_epoch":self._health_epoch,"binding_key":binding.key})
                    if override: data["pool_override"]=override
                    prior=a.conn.execute("SELECT v FROM kv WHERE k=?",(key,)).fetchone()
                    if prior and pin is None:
                        historical=json.loads(prior[0])
                        if historical.get("mode")=="pre_enforcement" and historical.get("binding_key")==binding.key:
                            data=historical  # Recheck now; retain its original creator epoch.
                    if prior and json.loads(prior[0])!=data: raise AccountError("provider_cli_pin_stale")
                    a.conn.execute("INSERT OR IGNORE INTO kv VALUES (?,?)",(key,canonical(data)))
                    if proof is not None:
                        a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",("provider_cli.verified:"+binding.key,
                            canonical({"proof":proof,"verified_at":self.clock()})))
            await self.run(commit,settle_on_cancel=True)

    @staticmethod
    def key(binding):
        return ":".join(str(binding.get(k) or "") for k in ("host","provider","alias","generation"))

    async def _alert(self,account,code):
        log.warning("subsystem=provider_accounts bug_ref=named_provider_accounts action=%s host=%s provider=%s account=%s generation=%s",
            code,account.host,account.provider,account.alias,account.generation)
        alerts=getattr(self.sessions,"alerts",None)
        if alerts is not None:
            await alerts.error(ErrorFact("provider_account.v1",code,hashlib.sha256(account.key.encode()).hexdigest()),
                principal="system:"+account.host+":"+account.provider+":"+account.alias)

    async def record_failure(self,account,kind):
        await self.run(lambda accounts:accounts.failure(account.key,kind,now=self.clock()))
        if kind=="auth": await self._alert(account,"login_unavailable")
        if kind=="identity": await self._alert(account,"identity_unverified")

    async def record_observation(self,account,observation):
        matched=await self.run(lambda accounts:accounts.observe(account.key,observation,now=self.clock()))
        if not matched: await self._alert(account,"identity_unverified")
        return matched

    async def before_input(self,row,*,verify=True):
        await self.maintenance_input(row)
        # Rollback can disable new launches while existing bound agents drain.
        if not row.get("account_binding") and await self.enrolled(row.get("host"),row.get("provider")):
            from provider_cli_pin import pristine
            try:
                if await self.run(pristine):
                    return  # Legacy input stays usable before the first pin.
            except AccountError:
                pass  # Corrupt history is never pre-enforcement.
            raise AccountError("provider_cli_pin_unavailable",reason="binding_unknown")
        if row.get("account_binding"):
            await self.run(lambda accounts:accounts.ensure_available(self.key(row["account_binding"])))
            from provider_cli_pin import check
            binding=Binding.from_public(row["account_binding"])
            pin=await self.run(lambda a:check(a,binding))
            if pin is None: return
            key="provider_cli.admission:"+row["stream_id"]+":"+row["session_generation"]
            receipt=await self.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?",(key,)).fetchone())
            if receipt and json.loads(receipt[0]).get("mode")=="pre_enforcement":
                receipt=None  # Keep that historical admission; observe current bytes separately.
            if not receipt:
                # Pre-enforcement seats keep their historical admission unknown.
                # A separate read-only observation proves current bytes without
                # relabeling their original launch or restarting a compliant seat.
                key="provider_cli.observed:"+row["stream_id"]+":"+row["session_generation"]
                receipt=await self.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?",(key,)).fetchone())
                if not receipt:
                    if not verify: raise AccountError("provider_cli_verification_unavailable")
                    if self.executor is None: raise AccountError("provider_cli_verification_unavailable")
                    async with self.executor._host_locks.setdefault(binding.host,asyncio.Lock()):
                        proof=await self.executor.verify_artifact(binding,pin)
                    from provider_cli_pin import canonical,consumer_current
                    def record(a):
                        from account_store import transaction
                        with transaction(a.conn):
                            if check(a,binding)!=pin: raise AccountError("provider_cli_pin_mismatch")
                            consumer_current(a,binding.host,getattr(self.sessions,"local_host",None),self.clock())
                            data=canonical({"artifact":proof,"binding_key":binding.key})
                            a.conn.execute("INSERT OR IGNORE INTO kv VALUES (?,?)",(key,data))
                            a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",("provider_cli.verified:"+binding.key,
                                canonical({"proof":proof,"verified_at":self.clock()})))
                            return (data,)
                    receipt=await self.run(record,settle_on_cancel=True)
            artifact=json.loads(receipt[0])["artifact"]
            if artifact.get("artifact")!=pin["providers"][binding.provider]["artifacts"].get(artifact.get("platform")):
                raise AccountError("provider_cli_pin_mismatch",reason="resource_hash_mismatch")

    @asynccontextmanager
    async def input_guard(self,row,*,message=None,phase="not_started",claim=False):
        # Remote preparation must never hold the fleet pin decision lock/barrier.
        await self.before_input(row)
        async with self.maintenance_guard(row):
            async with self.pin_inputs.read():
                async with self.pin_lock:
                    # Recheck after preparation and barrier admission; this path cannot invoke SSH.
                    await self.before_input(row,verify=False)
                    if phase=="not_started":
                        await self.cap_input(row,message or {},claim=claim)
                yield  # The caller scopes this to one physical action, never evidence waiting.

    async def cap_exemptions(self):
        """Current open generations of the primary and existing advisor binding."""
        return await self.run(lambda a:self._cap_exemptions_conn(a.conn))

    async def primary_pair(self):
        def current(a):
            from store_assistant_binding import _binding_conn,_seat_conn
            composite=self.server.assistant_composite if self.server else None
            if not composite or not composite.enabled or not composite.env_config.direct_primary: return None
            value=_binding_conn(a.conn,composite._env_binding(),name=composite.env_config.name)
            row=_seat_conn(a.conn,value['stream_id'])
            if row and row['status']=='open' and row['session_generation']==value['generation']:
                return value['stream_id'],value['generation']
        return await self.run(current)

    def _cap_exemptions_conn(self,conn):
        from store_assistant_binding import _binding_conn,_seat_conn
        result=set()
        composite=self.server.assistant_composite if self.server else None
        if composite and composite.enabled and composite.env_config.direct_primary:
            current=_binding_conn(conn,composite._env_binding(),name=composite.env_config.name)
            if current.get("stream_id"):
                row=_seat_conn(conn,current["stream_id"])
                if row and row.get("status")=="open" and row.get("session_generation")==current["generation"]:
                    result.add((current["stream_id"],current["generation"]))
        advisor=getattr(self.server,"lane_rulings",None)
        if advisor:
            current=advisor.binding_conn(conn)
            if current.get("state") in {"ready","same_primary"}:
                row=_seat_conn(conn,current["stream_id"])
                if row and row.get("status")=="open" and row.get("session_generation")==current["generation"]:
                    result.add((current["stream_id"],current["generation"]))
        return result

    async def cap_input(self,row,message,*,claim=False):
        from account_pool_control import operator,read,write,seat_key,eligibility,NOTICE_TOKEN
        if not row: return
        stream=row.get("stream_id") or f"{row['host']}:{row['session_name']}"
        generation=row.get("session_generation")
        auth=message.get("_auth_context") or {}
        if message.get("_pool_notice_token") is NOTICE_TOKEN:
            notice={"kind":message['_pool_notice_kind'],"metadata":message['_pool_notice_metadata'],
                    "recipient_stream_id":stream}
            decision=await self.cap_monitor.guard(notice)
            if decision:
                raise AccountError("pool_notice_stale" if decision.action=='terminal' else "pool_notice_deferred")
        if not row.get("account_binding"): return
        source=(auth.get("stream_id"),auth.get("session_generation"))
        reason=None
        if operator(auth): reason="direct_operator_order"
        elif message.get("_pool_notice_token") is NOTICE_TOKEN: reason="pool_control_notice"
        def check(a):
            from store_assistant_binding import _seat_conn
            target=_seat_conn(a.conn,stream)
            if not target or target.get("status")!="open" or target.get("session_generation")!=generation:
                raise AccountError("account_seat_generation_stale")
            exemptions=self._cap_exemptions_conn(a.conn)
            if message.get('_pool_notice_token') is NOTICE_TOKEN:
                metadata=message['_pool_notice_metadata']
                source_row=_seat_conn(a.conn,metadata['stream'])
                state=read(a,f"account.cap-seat:{metadata['stream']}:{metadata['generation']}") or {}
                directed=state.get('operator_input') or {}
                boundary=state.get('idle_boundary') or {}
                source_override=read(a,seat_key(metadata['stream'],metadata['generation'])) or {}
                if (not source_row or source_row['status']!='open' or source_row['session_generation']!=metadata['generation']
                        or (metadata['stream'],metadata['generation']) in exemptions or source_override.get('enabled')
                        or (directed and boundary.get('operator_id')!=directed['id'])
                        or eligibility(a,metadata['provider'],metadata['pool_id'],now=self.clock())['code']!='account_pool_cap_reached'):
                    raise AccountError('pool_notice_deferred')
            assistant_reason="assistant_recipient" if (stream,generation) in exemptions else (
                "assistant_sender" if auth.get("token_verified") is True and source in exemptions else None)
            override=read(a,seat_key(stream,generation))
            current_reason=reason or assistant_reason or ("seat_override" if override and override["enabled"] else None)
            if not current_reason:
                binding=row["account_binding"]
                result=eligibility(a,binding["provider"],binding["pool_id"],now=self.clock())
                if not result["eligible"]:
                    code={"account_pool_blocked":"pool_blocked","account_pool_cap_reached":"pool_at_cap",
                          "account_pool_meter_unavailable":"pool_usage_unknown"}[result.pop("code")]
                    result.pop("eligible")
                    raise AccountError(code,**result,submission_confirmed=False,delivery="not_landed",
                        retryable=False,nonretryable=True,do_not_retry=True,queued_for_redelivery=False)
            if claim and current_reason=="direct_operator_order":
                from account_store import transaction
                with transaction(a.conn):
                    key=f"account.cap-seat:{stream}:{generation}"
                    state=read(a,key) or {}
                    state["operator_input"]={"id":str(message.get("tell_id") or message.get("request_id") or ""),
                        "kind":"tell" if message.get("tell_id") else "send", "claimed_at":self.clock(),
                        "reason":"direct_operator_order","generation":generation}
                    write(a,key,state)
            return current_reason
        return await self.run(check,settle_on_cancel=claim)

    async def _front_desk(self,auth):
        composite=self.server.assistant_composite if self.server else None
        if (auth.get("token_verified") is not True or not composite or not composite.enabled
                or not composite.env_config.direct_primary): raise AccountError("account_control_unauthorized")
        binding=await self.store.get_assistant_binding(env_binding=composite._env_binding(),name=composite.env_config.name)
        if not binding or (auth.get("stream_id"),auth.get("session_generation"))!=(binding.get("stream_id"),binding.get("generation")):
            raise AccountError("account_control_unauthorized")
        row=await self.store.fetch_session(*self.sessions.split(binding["stream_id"]))
        if not row or row.get("status")!="open" or row.get("session_generation")!=binding["generation"]:
            raise AccountError("account_control_unauthorized")
        return row

    def watcher_authorized(self,auth,host):
        entry=self.policy.get("watchers",{}).get(auth.get("service_actor"))
        return auth.get("service_authenticated") is True and isinstance(entry,dict) and entry.get("host")==host

    async def operation(self,message):
        try: return await self._operation(message)
        except AccountError as exc: raise VerbError(str(exc),str(exc),**exc.context) from None

    async def _operation(self,message):
        action=message.get("action","status"); auth=message.get("_auth_context") or {}
        if action in {"pool-control-set","seat-override-set"}:
            actor=await self._control_actor(auth)
        elif action in {"pin-set","pin-status"}:
            from provider_cli_pin import privileged
            if not privileged(auth): await self._front_desk(auth)
        elif action in {"default-set","relogin","revalidate-health","upgrade","repin"}: await self._front_desk(auth)
        elif action in {"probe","observe-failure","observation"}:
            if not self.watcher_authorized(auth,message.get("host")):
                # Front desk may request a bound probe, but only the fixed host
                # watcher principal may submit observations/trusted failures.
                if action!="probe": raise AccountError("account_observation_unauthorized")
                await self._front_desk(auth)
        elif action=="status":
            if auth.get("token_verified") is not True and auth.get("operator_authenticated") is not True:
                raise AccountError("account_control_unauthorized")
        elif action=="report-failure":
            if auth.get("token_verified") is not True: raise AccountError("account_control_unauthorized")
        else: raise AccountError("account_action_invalid")
        allowed={"type","action","request_id","operation_id","provider","account","host","login_generation",
            "reason","kind","from_stream_id","stream_token","protocol"}
        if action=="upgrade":
            allowed.discard("kind")
            allowed.update({"new_generation","expected_version","expected_executable_sha256","prepare_enrollment"})
        if action=="repin": allowed.update({"new_generation","expected_pin_revision","expected_pin_digest","reconcile_of","expected_origin_digest","target_pin"})
        if action=="relogin":allowed.add("supersede_repin")
        if action in {"pin-set","pin-status"}:
            allowed.update({"local_admin_token","pin","expected_revision","recover_receipt","expected_raw_digest","expected_high_water_revision",
                            "initial_parked_cells","expected_readiness_digest"})
        if action=="observation": allowed.add("observation")
        if action=="revalidate-health": allowed.add("expected_revision")
        if action=="pool-control-set":
            allowed.update({"pool_id","expected_revision","blocked","stop_pct","window","authority_ref","supersedes_authority_ref"})
        if action=="seat-override-set":
            allowed.update({"stream","expected_generation","expected_revision","enabled","authority_ref"})
        if any(not str(k).startswith("_") and k not in allowed for k in message): raise AccountError("account_request_invalid")
        if action in {"pool-control-set","seat-override-set"}:
            from account_pool_control import mutate
            result=await self.run(lambda a:mutate(a,message,actor,now=self.clock(),authorize=self._control_authorizer(auth)),settle_on_cancel=True)
            return {"type":"account.ok",**result}
        if action in {"pin-set","pin-status"}:
            from provider_cli_pin import privileged,set_pin,status
            if action=="pin-set":
                from provider_cli_initial import set_pair
                receipt=await set_pair(self,message,recovery_principal=privileged(auth))
                public_receipt={k:v for k,v in receipt.items() if k!="previous"}
                return {"type":"account.ok","pin":receipt["pin"],"receipt":public_receipt,
                        **(await self.pin_status())}
            result={"type":"account.ok",**(await self.pin_status())}
            if "pin" in message:
                from provider_cli_initial import prepare
                _,readiness=await prepare(self,message)
                result.update(candidate_pin=message["pin"],initial_readiness=readiness)
            return result
        if action=="status":
            result=await self.run(lambda a:a.readback(now=self.clock()))
            def seats(a):
                from store_assistant_binding import _seat_conn
                values=[]
                for raw in a.conn.execute("SELECT v FROM kv WHERE k LIKE 'account.seat-override:%'"):
                    value=json.loads(raw[0]); target=_seat_conn(a.conn,value['stream'])
                    values.append(dict(value,active=bool(value['enabled'] and target and target['status']=='open'
                        and target['session_generation']==value['generation'])))
                return values
            result["seat_overrides"]=await self.run(seats)
            result["cap_seats"]=await self.run(lambda a:[json.loads(row[0]) for row in a.conn.execute(
                "SELECT v FROM kv WHERE k LIKE 'account.cap-seat:%'")])
            filters={"host":message.get("host"),"provider":message.get("provider"),"alias":message.get("account")}
            result["accounts"]=[row for row in result["accounts"] if all(v is None or row.get(k)==v for k,v in filters.items())]
            result["defaults"]=[row for row in result["defaults"] if all(v is None or row.get(k)==v for k,v in filters.items())]
            pools={(row["provider"],row["pool_id"]) for row in result["accounts"]}
            result["quota_pools"]=[row for row in result["quota_pools"] if (row["provider"],row["pool_id"]) in pools]
            if all(filters.values()):
                try:await self._front_desk(auth)
                except AccountError:pass
                else:
                    from account_repin_state import recovery_status
                    recovery=await recovery_status(self,{"host":filters["host"],"provider":filters["provider"],"account":filters["alias"]})
                    recovery.pop("_snapshot",None)
                    result["recovery"]=[recovery]
            return {"type":"account.ok",**result}
        if self.policy.get("enabled") is not True: raise AccountError("account_feature_off")
        host,provider,alias=message.get("host"),message.get("provider"),message.get("account")
        _component(host); _component(alias)
        if provider not in {"codex","claude"}: raise AccountError("account_request_invalid")
        if not self.manages(host,provider): raise AccountError("account_feature_off")
        if action=="repin":
            if not self.executor: raise AccountError("account_executor_unavailable")
            from account_repin import repin
            result=await repin(self.executor,message,auth)
            return {"type":"account.ok","accepted":True,"result":result}
        if action=="relogin" and message.get("supersede_repin") is not None:
            if not self.executor:raise AccountError("account_executor_unavailable")
            from account_repin_hatch import hatch
            return {"type":"account.ok","accepted":True,"result":await hatch(self.executor,message,auth)}
        if action=="upgrade":
            if not self.executor: raise AccountError("account_executor_unavailable")
            from account_upgrade import upgrade
            result=await upgrade(self.executor,message,auth)
            return {"type":"account.ok","accepted":True,"result":result}
        if action=="revalidate-health":
            if not self.executor: raise AccountError("account_executor_unavailable")
            result=await self.executor.revalidate_health(message,auth)
            return {"type":"account.ok","accepted":True,"result":result}
        if action=="default-set":
            default=await self.run(lambda a:a.set_default(host=host,provider=provider,alias=alias))
            return {"type":"account.ok","default":default}
        account=await self.run(lambda a:a._cell(host,provider,alias))
        if account is None: raise AccountError("account_generation_unknown")
        account=Binding(**json.loads(account["data"]))
        if message.get("login_generation") not in {"active",account.generation}:
            if action=="relogin" and self.executor:
                generation=message.get("login_generation")
                _component(generation)
                prior=await self.run(lambda a:a.bound(":".join((host,provider,alias,generation))))
                if prior is not None:
                    result=await self.executor.replay(prior,str(message.get("operation_id") or message.get("request_id") or ""))
                    if result is not None: return {"type":"account.ok","accepted":True,"result":result}
            raise AccountError("account_login_generation_stale")
        if action=="report-failure":
            row=await self.store.fetch_session(*self.sessions.split(auth.get("stream_id")))
            if (not row or row.get("session_generation")!=auth.get("session_generation")
                    or self.key(row.get("account_binding") or {})!=account.key): raise AccountError("account_report_unauthorized")
            action="probe"
        if action in {"probe","relogin"}:
            if not self.executor: raise AccountError("account_executor_unavailable")
            result=await (self.executor.recover(account,relogin=True,
                operation_id=str(message.get("operation_id") or message.get("request_id") or ""))
                if action=="relogin" else self.executor.observe(account))
            return {"type":"account.ok","accepted":True,"result":result}
        if action=="observation":
            await self.record_observation(account,message.get("observation") or {})
        else: await self.record_failure(account,message.get("kind"))
        return {"type":"account.ok","accepted":True,"host":host,"provider":provider,"account":alias}

    async def pin_status(self):
        from provider_cli_pin import status,version,consumer_current,initial_parked
        from provider_cli_artifact import code_identity
        result=await self.run(status)
        from provider_cli_maintenance import PREFIX,status as maintenance_status
        def maintenance(a):
            return {row[0][len(PREFIX):]:maintenance_status(a,row[0][len(PREFIX):]) for row in a.conn.execute(
                "SELECT k FROM kv WHERE substr(k,1,?)=?",(len(PREFIX),PREFIX))}
        result["input_maintenance"]=await self.run(maintenance)
        readback=await self.run(lambda a:a.readback(now=self.clock()))
        def outstanding(a):
            from account_repin import _resolved
            return [json.loads(r["data"]) for r in a.conn.execute(
                "SELECT binding_key,data FROM v2_account_recovery WHERE substr(binding_key,1,6)='repin:' AND state IN ('pending','parked')")
                if not _resolved(a,r["binding_key"])]
        pending=await self.run(outstanding)
        seats=await self.store.list_sessions()
        known={(r["stream_id"],r.get("session_generation")) for r in seats}
        for reservation in await self.store.reservations(include_expired=True):
            try:
                payload=reservation.get("payload") or {}
                payload=json.loads(payload) if isinstance(payload,str) else payload
                fields=payload.get("open_fields",{})
                sid=reservation["host"]+":"+reservation["session_name"]
                generation=fields.get("session_generation")
                if not generation or (sid,generation) in known: continue
                key="provider_cli.admission:"+sid+":"+generation
                admitted=await self.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?",(key,)).fetchone())
                if admitted:
                    seats.append({**fields,"stream_id":sid,"creation_pending":True})
                    known.add((sid,generation))
            except (ValueError,TypeError,KeyError): continue
        cells=[]
        pin=result["pin"]
        hosts=getattr(self.executor,"hosts",None)
        population={}
        for candidate in readback["accounts"]:
            cell_key=tuple(candidate[k] for k in ("host","provider","alias"))
            if cell_key not in population or candidate.get("active"):
                population[cell_key]=candidate
        for row in population.values():
            expected=pin["providers"][row["provider"]] if pin else None
            try: observed=version(row["provider"],row.get("version"))
            except (ValueError,KeyError): observed=row.get("version")
            online=(row["host"]==getattr(self.sessions,"local_host",None) or bool(hosts and hosts.is_online(row["host"])))
            binding_key=self.key(row)
            verified=await self.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?",("provider_cli.verified:"+binding_key,)).fetchone())
            verified=json.loads(verified[0]) if verified else {}
            proof=verified.get("proof",{})
            proof_current=bool(expected and proof.get("artifact")==expected["artifacts"].get(proof.get("platform"))
                and proof.get("binding_key")==binding_key and proof.get("code_identity")==code_identity()
                and 0<=self.clock()-verified.get("verified_at",0)<=120)
            try:
                await self.run(lambda a:consumer_current(a,row["host"],getattr(self.sessions,"local_host",None),self.clock()))
                source_current=True
            except AccountError: source_current=False
            matches=bool(expected and observed==expected["version"] and any(
                a["native_sha256"]==row.get("executable_sha256") for a in expected["artifacts"].values()))
            initially_parked=bool(pin and row.get("active") and await self.run(
                lambda a,key=binding_key:initial_parked(a,a.bound(key))))
            split=any(all(p["arguments"].get(k)==row.get(rk) for k,rk in (("host","host"),("provider","provider"),("account","alias"))) for p in pending)
            reason=("repin_reconciliation_pending" if split else "no_active_binding" if not row.get("active") else
                    "initial_activation_parked" if initially_parked else "disabled" if not row["enabled"] else "offline" if not online else
                    "account_policy_off" if not self.manages(row["host"],row["provider"]) else
                    "satellite_not_current" if not source_current else
                    row["health"] if row["health"]!="healthy" else "pre_enforcement" if result["mode"]=="pre_enforcement" else
                    "pin_absent_or_corrupt" if not pin else
                    "ready" if matches and proof_current else "artifact_verification_pending" if matches else "pin_mismatch")
            old_seats=[]
            for seat in seats:
                b=seat.get("account_binding") or {}
                if (b.get("host"),b.get("provider"),b.get("alias"))!=(row["host"],row["provider"],row["alias"]): continue
                try: old=(not expected or version(b["provider"],b["version"])!=expected["version"]
                          or not any(a["native_sha256"]==b.get("executable_sha256") for a in expected["artifacts"].values()))
                except (ValueError,KeyError): old=True
                if not old:
                    admission_key="provider_cli.admission:"+seat["stream_id"]+":"+str(seat.get("session_generation") or "")
                    admitted=await self.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?",(admission_key,)).fetchone())
                    if admitted:
                        data=json.loads(admitted[0])
                        if data.get("mode")=="pre_enforcement":
                            observation_key="provider_cli.observed:"+seat["stream_id"]+":"+str(seat.get("session_generation") or "")
                            observation=await self.run(lambda a:a.conn.execute("SELECT v FROM kv WHERE k=?",(observation_key,)).fetchone())
                            data=json.loads(observation[0]) if observation else {}
                        observed_artifact=data.get("artifact",{})
                        old=observed_artifact.get("artifact")!=expected["artifacts"].get(observed_artifact.get("platform"))
                if old: old_seats.append({"stream_id":seat["stream_id"],"generation":seat.get("session_generation"),
                                         "creation_pending":bool(seat.get("creation_pending"))})
            cells.append({"host":row["host"],"provider":row["provider"],"account":row["alias"],
                "generation":row["generation"],"expected_version":expected["version"] if expected else None,
                "observed_version":observed,"online":online,"observation":row.get("observation"),
                "state":"parked" if not row["enabled"] or initially_parked else "ready" if reason=="ready" and not old_seats else "pending","reason":reason,"old_seats":old_seats,
                "verified_at":verified.get("verified_at"),"proof_current":proof_current,
                "pending_operations":[p["arguments"]["operation_id"] for p in pending if all(
                    p["arguments"].get(k)==row.get(rk) for k,rk in (("host","host"),("provider","provider"),("account","alias")))]})
        return {**result,"cells":cells,"fleet_complete":bool(cells) and all(c["state"]=="ready" and not c["pending_operations"] for c in cells)}

    async def tick(self):
        # Collection never selects, re-logins or migrates an agent.
        if (not self._stopping and self.executor and self.policy.get("enabled") is True
                and self.clock()>=self._next_probe and (not self._collector_task or self._collector_task.done())):
            self._next_probe=self.clock()+60
            self._collector_task=asyncio.create_task(self.executor.collect_all(),name="account-observations")
        if not self._stopping:
            await self.cap_monitor.tick()
