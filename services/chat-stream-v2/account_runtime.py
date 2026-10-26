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


class AccountRuntime:
    def __init__(self,store,sessions,*,policy=None,clock=time.time,executor=None):
        self.store,self.sessions=store,sessions
        self.policy,self.clock,self.executor={"enabled":False} if policy is None else policy,clock,executor
        validate_policy_scope(self.policy)
        self.server=self.spawnctl=self.comms=None
        self._next_probe=0; self._collector_task=None; self._stopping=False
        self._health_epoch=uuid.uuid4().hex; self._health_started=False

    def attach(self,server,spawnctl,comms):
        self.server,self.spawnctl,self.comms=server,spawnctl,comms
        server.account_runtime=spawnctl.account_runtime=comms.account_runtime=self
        server.handlers["account"]=self.operation

    async def run(self,function):
        return await self.store.submit(lambda conn:function(AccountStore(conn,self.policy,initialize=False)))

    async def start(self):
        # This is the owned daemon-start boundary, not a Store connection open.
        # OFF only terminalizes old audit intents; it never restores health.
        if not self._health_started:
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
                if current is None: await self.run(lambda accounts:accounts.register(binding))

    async def stop(self):
        self._stopping=True
        if self._collector_task:
            self._collector_task.cancel()
            await asyncio.gather(self._collector_task,return_exceptions=True)

    async def authorize_choice(self,message):
        if "account" in message:
            _component(message["account"])
            await self._front_desk(message.get("_auth_context") or {})

    def manages(self,host,provider=None):
        return policy_manages(self.policy,host,provider)

    async def choose_launch(self,*,host,provider,model,effort,message):
        await self.authorize_choice(message)
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
            prior=message["_account_resume_binding"]
            binding=await self.run(lambda accounts:accounts.ensure_available(self.key(prior)))
            if (binding.host!=host or binding.provider!=provider
                    or message.get("account",binding.alias)!=binding.alias
                    or binding.public()!=dict(prior,default_revision=0)):
                raise AccountError("account_resume_cross_home_forbidden")
            return Binding.from_public(prior)
        if message.get("resume_session_id"):
            raise AccountError("account_resume_cross_home_forbidden")
        return await self.run(lambda accounts:accounts.select(host=host,provider=provider,alias=message.get("account")))

    async def bind_launch(self,row):
        if row.get("account_binding"): await self.run(lambda accounts:accounts.bind_session(row))

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

    async def before_input(self,row):
        # Rollback can disable new launches while existing bound agents drain.
        if row.get("account_binding"):
            await self.run(lambda accounts:accounts.ensure_available(self.key(row["account_binding"])))

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
        if action in {"default-set","relogin","revalidate-health"}: await self._front_desk(auth)
        elif action in {"probe","observe-failure","observation"}:
            if not self.watcher_authorized(auth,message.get("host")):
                # Front desk may request a bound probe, but only the fixed host
                # watcher principal may submit observations/trusted failures.
                if action!="probe": raise AccountError("account_observation_unauthorized")
                await self._front_desk(auth)
        elif action in {"status","report-failure"}:
            if auth.get("token_verified") is not True: raise AccountError("account_control_unauthorized")
        else: raise AccountError("account_action_invalid")
        allowed={"type","action","request_id","operation_id","provider","account","host","login_generation",
            "reason","kind","from_stream_id","stream_token","protocol"}
        if action=="observation": allowed.add("observation")
        if action=="revalidate-health": allowed.add("expected_revision")
        if any(not str(k).startswith("_") and k not in allowed for k in message): raise AccountError("account_request_invalid")
        if action=="status":
            result=await self.run(lambda a:a.readback(now=self.clock()))
            filters={"host":message.get("host"),"provider":message.get("provider"),"alias":message.get("account")}
            result["accounts"]=[row for row in result["accounts"] if all(v is None or row.get(k)==v for k,v in filters.items())]
            result["defaults"]=[row for row in result["defaults"] if all(v is None or row.get(k)==v for k,v in filters.items())]
            pools={(row["provider"],row["pool_id"]) for row in result["accounts"]}
            result["quota_pools"]=[row for row in result["quota_pools"] if (row["provider"],row["pool_id"]) in pools]
            return {"type":"account.ok",**result}
        if self.policy.get("enabled") is not True: raise AccountError("account_feature_off")
        host,provider,alias=message.get("host"),message.get("provider"),message.get("account")
        _component(host); _component(alias)
        if provider not in {"codex","claude"}: raise AccountError("account_request_invalid")
        if not self.manages(host,provider): raise AccountError("account_feature_off")
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

    async def tick(self):
        # Observation only. Errors never select, re-login or migrate an agent.
        if (not self._stopping and self.executor and self.policy.get("enabled") is True
                and self.clock()>=self._next_probe and (not self._collector_task or self._collector_task.done())):
            self._next_probe=self.clock()+60
            self._collector_task=asyncio.create_task(self.executor.collect_all(),name="account-observations")
