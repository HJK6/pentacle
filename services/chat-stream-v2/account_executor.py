"""Finite daemon dispatch of bound host probes and native re-login generations."""
from __future__ import annotations
import asyncio
import json
import hashlib
import re
import time
import uuid

from account_store import transaction
from provider_accounts import AccountError, Binding
from account_revalidation import revalidate_health, _valid_observation

_HOST_SUCCESS_OUTCOMES={"ok","healthy","recovered","success"}
_HOST_FAILURE_OUTCOMES={
    "provider_auth_failed", "provider_quota_blocked", "provider_capacity", "provider_transport_failed",
    "provider_permission_denied", "provider_timeout", "provider_probe_failed", "provider_identity_changed",
    "subscription_identity_unverified", "provider_identity_unavailable", "provider_identity_or_cache_unverified",
    "provider_executable_changed", "provider_version_changed", "provider_cache_unavailable", "provider_cache_stale",
    "provider_windows_invalid", "account_login_generation_stale", "provider_cli_pin_mismatch",
    "provider_cli_pin_unavailable", "provider_cli_verification_unavailable", "account_repin_home_busy",
    "account_repin_occupancy_unavailable",
}


class AccountExecutor:
    def __init__(self, runtime, hosts, *, execute=None):
        self.runtime, self.hosts = runtime, hosts
        self.execute = execute or self._execute
        self._host_locks = {}

    async def _execute(self, account, action, operation_id, generation=None, *, pin=None, reconcile=None):
        if action=="observe" and pin is None:
            from provider_cli_pin import check
            pin=await self.runtime.run(lambda a:check(a,account))
        configuration = self.runtime.policy.get("executors", {}).get(account.host)
        if not configuration or not self.hosts: raise AccountError("account_host_executor_unavailable")
        # The daemon's authenticated Hosts transport selects the destination.
        # Only paths in private host configuration can select an executor.
        args = [configuration["python"], configuration["script"], action,
                "--provider", account.provider, "--account", account.alias,
                "--login-generation", account.generation, "--operation-id", hashlib.sha256(operation_id.encode()).hexdigest()]
        if generation: args += ["--new-generation", generation]
        if pin is not None:
            from provider_cli_pin import canonical
            args += ["--pin-json", canonical(pin)]
        if reconcile is not None:
            from provider_cli_pin import canonical
            args += ["--reconcile-json",canonical(reconcile)]
        try:
            code, output = await self.hosts.run_command(account.host, *args,
                timeout=320 if action in {"enroll","repin","repin-reconcile"} else 200 if action in {"observe","observe-lineage","observe-bound"} else 100)
            if len(output) > 65536: raise AccountError("account_host_response_invalid")
            result = json.loads(output)
        except AccountError: raise
        except TimeoutError: raise AccountError("provider_timeout") from None
        except Exception: raise AccountError("account_host_unreachable") from None
        if (not isinstance(result, dict) or not isinstance(result.get("outcome"), str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", result["outcome"]) is None):
            raise AccountError("account_host_response_invalid")
        if code != 0:
            if result["outcome"] in _HOST_SUCCESS_OUTCOMES:
                raise AccountError("account_host_response_invalid")
            if result["outcome"] not in _HOST_FAILURE_OUTCOMES:
                raise AccountError(result["outcome"])
        return result

    async def verify_artifact(self,account,pin):
        from provider_cli_pin import digest
        from provider_cli_artifact import code_identity
        try:
            proof=await self.execute(account,"verify-artifact",uuid.uuid4().hex,pin=pin)
            artifact=pin["providers"][account.provider]["artifacts"].get(proof.get("platform"))
            platform_matches=(str(proof.get("platform") or "").startswith("darwin-") if account.platform=="macos"
                              else str(proof.get("platform") or "").startswith("linux-"))
            if (proof.get("outcome")!="ok" or proof.get("binding_key")!=account.key
                    or not platform_matches or not artifact or proof.get("artifact")!=artifact or proof.get("pin_revision")!=pin["revision"]
                    or proof.get("pin_digest")!=digest(pin) or proof.get("code_identity")!=code_identity()
                    or artifact["native_sha256"]!=account.executable_sha256):
                raise AccountError("provider_cli_pin_mismatch")
            return proof
        except AccountError: raise
        except Exception: raise AccountError("provider_cli_verification_unavailable") from None

    async def launcher_descriptors(self,host,provider):
        from provider_cli_occupancy import descriptors
        return await descriptors(self.runtime,host,provider)

    async def verify_launchers(self,account):
        from provider_cli_artifact import code_identity
        from provider_cli_pin import digest,current,raw,raw_digest
        def pin_snapshot(ledger):
            token=raw_digest(raw(ledger))
            try:pin=current(ledger)
            except AccountError:pin=None  # Direct natives do not require a release pin.
            return token,pin
        before,pin=await self.runtime.run(pin_snapshot)
        launchers=await self.launcher_descriptors(account.host,account.provider)
        value=await self.execute(account,"launcher-status",uuid.uuid4().hex,
                                 pin=pin,reconcile={"managed_launchers":launchers})
        after,latest=await self.runtime.run(pin_snapshot)
        expected_pin=digest(pin) if pin is not None else 'absent'
        if (value.get("outcome")!="ok" or value.get("host")!=account.host
                or before!=after or latest!=pin or value.get('pin_digest',expected_pin)!=expected_pin
                or value.get("provider")!=account.provider or value.get("manifest_digest")!=digest(launchers)
                or value.get("code_identity")!=code_identity() or not isinstance(value.get("managed"),list)
                or not isinstance(value.get("unmanaged"),list) or value.get("missing")!=[]
                or value.get("pending_mutations")!=[]):
            raise AccountError("provider_cli_verification_unavailable",reason="launcher_unverified")
        expected={row["stream_id"]:row for row in launchers}
        seen=set()
        for row in value["managed"]:
            if not isinstance(row,dict):
                raise AccountError("provider_cli_verification_unavailable",reason="launcher_unverified")
            sid=row.get("stream_id")
            if not isinstance(sid,str):raise AccountError("provider_cli_verification_unavailable",reason="launcher_unverified")
            original=expected.get(sid)
            process=row.get("process") or {};root=row.get("pane_root") or {}
            from _shared.provider_cli_release import SHA256
            def complete(proof):
                return (isinstance(proof,dict) and proof.get("version")==2 and proof.get("host")==account.host
                        and type(proof.get("pid")) is int and proof["pid"]>0
                        and type(proof.get("uid")) is int and proof["uid"]>=0
                        and bool(proof.get("boot_id")) and bool(proof.get("start_id"))
                        and isinstance(proof.get("command_fingerprint"),str)
                        and SHA256.fullmatch(proof["command_fingerprint"]))
            if (sid in seen or not original or row.get("binding")!=original["binding"]
                    or row.get("session_generation")!=original["session_generation"]
                    or not complete(process) or not complete(root)
                    or str(root.get("pid"))!=original["pane_pid"]
                    or root.get("boot_id")!=process.get("boot_id") or root.get("uid")!=process.get("uid")
                    or root.get("start_id")!=__import__('prockill').canonical_start(original["pane_started_at"])):
                raise AccountError("provider_cli_verification_unavailable",reason="launcher_unverified")
            seen.add(sid)
        if seen!=set(expected):raise AccountError("provider_cli_verification_unavailable",reason="launcher_unverified")
        return value

    async def observe(self, account):
        async with self._host_locks.setdefault(account.host, asyncio.Lock()):
            mark_key=(account.key,account.generation)
            revision=self.runtime._self_recovery_marks.get(mark_key)
            now=self.runtime.monotonic()
            last=self.runtime._self_recovery_last_dispatch.get(mark_key)
            eligible=(revision is not None and mark_key not in self.runtime._self_recovery_inflight
                and (last is None or now-last>=self.runtime.AUTH_HEALTH_SELF_RECOVERY_COOLDOWN_SECONDS))
            if not eligible:
                return await self._observe_locked(account)

            # Claim synchronously while holding the host lock, before any await.
            # A newer refusal can publish a fresh mark while this attempt runs.
            if self.runtime._self_recovery_marks.get(mark_key)!=revision:
                return await self._observe_locked(account)
            self.runtime._self_recovery_marks.pop(mark_key,None)
            self.runtime._self_recovery_inflight.add(mark_key)
            dispatched=False
            try:
                try:
                    pin,pin_state=await self.runtime.run(lambda ledger:self._self_recovery_pin_sample(ledger,account))
                    current=await self.runtime.run(lambda ledger:self._self_recovery_mark_current(
                        ledger,account,revision))
                except asyncio.CancelledError:
                    raise
                except AccountError:
                    # Let the ordinary path return its established typed pin
                    # failure, and retain the mark for a later suitable visit.
                    self._restore_self_recovery_mark(mark_key,revision)
                    return await self._observe_locked(account)
                if not current:
                    return await self._observe_locked(account)

                operation_id="account-self-recovery:"+uuid.uuid4().hex
                # Cooldown starts at the actual host dispatch, not at marking or
                # lock acquisition.  Cancellation from this await onward spends
                # the attempt and never retries it.
                self.runtime._self_recovery_last_dispatch[mark_key]=self.runtime.monotonic()
                self.runtime._index_self_recovery(mark_key)
                dispatched=True
                if pin is None:
                    result=await self.execute(account,"observe-bound",operation_id)
                else:
                    result=await self.execute(account,"observe-bound",operation_id,pin=pin)
                return await self._finish_self_recovery_observation(
                    account,revision,operation_id,pin,pin_state,result)
            except asyncio.CancelledError:
                if not dispatched:
                    self._restore_self_recovery_mark(mark_key,revision)
                raise
            except AccountError as exc:
                if not dispatched:
                    self._restore_self_recovery_mark(mark_key,revision)
                return self._observation_failure(account,str(exc))
            except Exception:
                if not dispatched:
                    self._restore_self_recovery_mark(mark_key,revision)
                return self._observation_failure(account,"provider_probe_failed")
            finally:
                self.runtime._self_recovery_inflight.discard(mark_key)

    def _restore_self_recovery_mark(self,key,revision):
        if not self.runtime._self_recovery_live(key[0]):
            return
        if key not in self.runtime._self_recovery_marks:
            self.runtime._self_recovery_marks[key]=revision
        elif self.runtime._self_recovery_marks[key]<revision:
            self.runtime._self_recovery_marks[key]=revision
        self.runtime._index_self_recovery(key)

    @staticmethod
    def _observation_failure(account,outcome):
        return {"outcome":outcome,"host":account.host,"provider":account.provider,"account":account.alias}

    @staticmethod
    def _self_recovery_pin_sample(ledger,account):
        """The full preflight plus the keyed pin state it was decided on."""
        from provider_cli_pin import check
        return check(ledger,account),AccountExecutor._self_recovery_pin_keys(ledger)

    @staticmethod
    def _self_recovery_pin_keys(ledger):
        from provider_cli_pin import INITIALIZED,raw,raw_digest
        marker=ledger.conn.execute("SELECT v FROM kv WHERE k=?",(INITIALIZED,)).fetchone()
        return raw_digest(raw(ledger)),marker[0] if marker else None

    @staticmethod
    def _self_recovery_mark_current(ledger,account,revision):
        row=ledger.conn.execute("SELECT active,health,health_revision,data FROM v2_account_bindings WHERE binding_key=?",
            (account.key,)).fetchone()
        current=ledger.bound(account.key)
        return bool(row and row["active"] and current==account and current.enabled
            and row["health"]=="auth_unhealthy" and row["health_revision"]==revision)

    async def _finish_self_recovery_observation(self,account,expected_revision,operation_id,pin,pin_state,result):
        if not isinstance(result,dict):
            return self._observation_failure(account,"account_host_response_invalid")
        outcome=result.get("outcome")
        if outcome=="ok":
            observation=result.get("observation")
            if result.get("binding_key")!=account.key or not _valid_observation(account,observation):
                return self._observation_failure(account,"provider_probe_failed")
            proof=result.get("artifact_proof")
            if pin is None:
                if proof:
                    return self._observation_failure(account,"provider_cli_pin_mismatch")
            else:
                try:
                    self._validate_self_recovery_proof(account,pin,proof)
                except AccountError as exc:
                    return self._observation_failure(account,str(exc))
            receipt=await self.runtime.run(lambda ledger:self._finish_self_recovery(
                ledger,account,expected_revision,operation_id,pin_state,proof if pin is not None else None),
                settle_on_cancel=True)
            if receipt.get("outcome")!="healthy":
                return self._observation_failure(account,receipt.get("outcome","account_health_revision_stale"))
            return result
        if outcome=="provider_auth_failed":
            # Confirms the failure already counted at the marked revision.
            return self._observation_failure(account,outcome)
        if outcome in {"provider_identity_changed","subscription_identity_unverified",
                       "provider_identity_or_cache_unverified"}:
            if not (outcome=="provider_identity_or_cache_unverified"
                    and result.get("identity_evidence")=="contradiction"):
                return self._observation_failure(account,outcome)
            changed=await self._self_recovery_identity_failure(account,expected_revision,pin_state)
            if not changed:
                return self._observation_failure(account,"account_health_revision_stale")
            await self.runtime._alert(account,"identity_unverified")
        else:
            kinds={"provider_quota_blocked":"quota","provider_capacity":"capacity",
                "provider_transport_failed":"network","provider_permission_denied":"permission"}
            kind=kinds.get(outcome)
            if kind:
                changed=await self.runtime.run(lambda ledger:ledger.failure(account.key,kind,
                    now=self.runtime.clock(),expected_revision=expected_revision),settle_on_cancel=True)
                if not changed:
                    return self._observation_failure(account,"account_health_revision_stale")
        return self._observation_failure(account,outcome if isinstance(outcome,str) else "provider_probe_failed")

    @staticmethod
    def _validate_self_recovery_proof(account,pin,proof):
        from provider_cli_pin import digest
        from provider_cli_artifact import code_identity
        artifact=pin["providers"][account.provider]["artifacts"].get(proof.get("platform") if isinstance(proof,dict) else None)
        platform_matches=(str(proof.get("platform") or "").startswith("darwin-") if account.platform=="macos"
                          else str(proof.get("platform") or "").startswith("linux-")) if isinstance(proof,dict) else False
        if (not isinstance(proof,dict) or proof.get("outcome")!="ok" or proof.get("binding_key")!=account.key
                or not platform_matches or not artifact or proof.get("artifact")!=artifact
                or proof.get("pin_revision")!=pin["revision"] or proof.get("pin_digest")!=digest(pin)
                or proof.get("code_identity")!=code_identity() or artifact.get("native_sha256")!=account.executable_sha256):
            raise AccountError("provider_cli_verification_unavailable")

    def _finish_self_recovery(self,ledger,account,expected_revision,operation_id,pin_state,proof):
        """The one winning write: keyed rechecks, then proof, health and receipt together."""
        with transaction(ledger.conn):
            # Same-host fence and park writers hold this observation's host
            # lock; a fleet pin change rewrites these two keyed values.
            if self._self_recovery_pin_keys(ledger)!=pin_state:
                return {"outcome":"provider_cli_pin_stale"}
            row=ledger.conn.execute("SELECT active,health,health_revision,data FROM v2_account_bindings WHERE binding_key=?",
                (account.key,)).fetchone()
            current=ledger.bound(account.key)
            if (row is None or not row["active"] or current!=account or not current.enabled
                    or row["health"]!="auth_unhealthy" or row["health_revision"]!=expected_revision):
                return {"outcome":"account_health_revision_stale"}
            transition=ledger.transition_relogin_auth_health(account.key,expected_revision)
            if transition.get("outcome")!="healthy" or not transition.get("changed"):
                return transition
            if proof is not None:
                from provider_cli_pin import canonical
                ledger.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",("provider_cli.verified:"+account.key,
                    canonical({"proof":proof,"verified_at":self.runtime.clock()})))
            state=self._read_recovery(ledger,account.key)
            receipts=state.setdefault("receipts",{})
            if operation_id in receipts:
                return receipts[operation_id]
            receipt={"outcome":"healthy","operation_id":operation_id,
                "health_revision":transition["applied_revision"],
                "provenance":"admission_signalled_self_recovery"}
            receipts[operation_id]=receipt
            self._write_recovery(ledger,account.key,state)
            return receipt

    async def _self_recovery_identity_failure(self,account,expected_revision,pin_state):
        changed=await self.runtime.run(lambda ledger:ledger.failure(account.key,"identity",
            now=self.runtime.clock(),expected_revision=expected_revision,
            fence=lambda fenced:self._self_recovery_pin_fence(fenced,pin_state)),settle_on_cancel=True)
        return bool(changed)

    @staticmethod
    def _self_recovery_pin_fence(ledger,pin_state):
        # The positive winner's fence: evidence sampled under another pin is superseded.
        if AccountExecutor._self_recovery_pin_keys(ledger)!=pin_state:
            raise AccountError("provider_cli_pin_stale")

    async def _observe_locked(self, account, *, require_match=False, recovery=False):
        try:
            result = await self.execute(account, "observe-bound" if recovery else "observe", uuid.uuid4().hex)
            observation = result.get("observation")
            if result["outcome"] == "ok" and isinstance(observation, dict) and result.get("binding_key") == account.key:
                matched=await self.runtime.record_observation(account, observation)
                if result.get("artifact_proof"):
                    from provider_cli_pin import canonical,check,digest
                    proof=result["artifact_proof"]
                    def record(a):
                        pin=check(a,account)
                        if pin is None:raise AccountError("provider_cli_pin_mismatch",reason="unexpected_pristine_artifact")
                        if proof.get("pin_digest")!=digest(pin): raise AccountError("provider_cli_pin_mismatch")
                        with transaction(a.conn):
                            a.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",("provider_cli.verified:"+account.key,
                                canonical({"proof":proof,"verified_at":self.runtime.clock()})))
                    await self.runtime.run(record)
                if require_match and matched is not True:
                    return {"outcome":"provider_identity_or_cache_unverified"}
                return result
            if require_match and result["outcome"]=="ok":
                result={"outcome":"account_host_response_invalid"}
            kind = {"provider_auth_failed": "auth", "provider_quota_blocked": "quota",
                    "provider_capacity": "capacity", "provider_transport_failed": "network",
                    "provider_permission_denied": "permission", "subscription_identity_unverified": "identity",
                    "provider_identity_changed": "identity", "provider_identity_or_cache_unverified": "identity"}.get(result["outcome"])
            if kind:
                await self.runtime.record_failure(account, kind)
            await self._failed_observation(account, result["outcome"])
            return {"outcome": result["outcome"], "host": account.host, "provider": account.provider, "account": account.alias}
        except AccountError as exc:
            await self._failed_observation(account, str(exc))
            return {"outcome": str(exc), "host": account.host, "provider": account.provider, "account": account.alias}

    async def _failed_observation(self, account, code):
        await self.runtime.run(lambda ledger: ledger.observe(account.key, {
            "provider_id": account.provider_id, "pool_id": account.pool_id, "auth_mode": "subscription",
            "collection": "failed", "error_code": code,
            "observed_at": None, "windows": {}}, now=self.runtime.clock()))

    async def revalidate_health(self, message, auth):
        return await revalidate_health(self,message,auth)

    async def recover(self, account, *, relogin, operation_id):
        if not operation_id or len(operation_id) > 160: raise AccountError("account_operation_id_required")
        if not relogin: return await self.observe(account)
        # Explicit front-desk requests only. Distinct IDs cannot create parallel browser attempts.
        async with self._host_locks.setdefault(account.host, asyncio.Lock()):
            return await self._recover_locked(account, relogin=relogin, operation_id=operation_id)

    async def replay(self, account, operation_id):
        """Inspect a known operation on an inactive generation; never start a login."""
        if not operation_id or len(operation_id) > 160: return None
        async with self._host_locks.setdefault(account.host, asyncio.Lock()):
            state = await self.runtime.run(lambda ledger: self._read_recovery(ledger, account.key))
            if operation_id in state.get("receipts", {}): return state["receipts"][operation_id]
            if state.get("inflight") == operation_id:
                return await self._inspect_recovery(account,state)
            return None

    async def _recover_locked(self, account, *, relogin, operation_id):
        now = self.runtime.clock()
        from account_repin_state import require_generation_clear
        await self.runtime.run(lambda a:require_generation_clear(a,{"host":account.host,"provider":account.provider,"account":account.alias}))
        prior = await self.runtime.run(lambda ledger: self._prior_recovery(ledger, account, operation_id))
        if prior:
            owner, prior_state = prior
            if operation_id in prior_state.get("receipts", {}): return prior_state["receipts"][operation_id]
            return await self._inspect_recovery(owner,prior_state)
        state = await self.runtime.run(lambda ledger: self._read_recovery(ledger, account.key))
        receipts = state.get("receipts", {})
        if operation_id in receipts: return receipts[operation_id]
        if state.get("inflight"):
            # After an uncertain remote reply, only inspect the same generation.
            return await self._inspect_recovery(account,state)
        # Keep the pre-probe revision only as the success transition's CAS input.
        expected_revision = await self.runtime.run(lambda ledger: ledger.health_revision(account.key))
        # Confirm an actual auth failure before a requested re-login. A quota,
        # network or capacity failure never enters native enrollment.
        probe = await self._observe_locked(account,recovery=True,require_match=True)
        if probe["outcome"] == "ok":
            if not _valid_observation(account, probe.get("observation")):
                return {"outcome": "provider_probe_failed"}
            def complete_probe(ledger):
                with transaction(ledger.conn):
                    state_now=self._read_recovery(ledger,account.key)
                    existing=state_now.get("receipts",{}).get(operation_id)
                    if existing is not None:return existing
                    transition=ledger.transition_relogin_auth_health(account.key,expected_revision)
                    if transition["outcome"] != "healthy":
                        return transition
                    if not transition["changed"]:
                        return {"outcome":"healthy"}
                    receipt={"outcome":"healthy","operation_id":operation_id,
                        "health_revision":transition["applied_revision"],"relogin_probe":"ok"}
                    state_now.setdefault("receipts",{})[operation_id]=receipt
                    self._write_recovery(ledger,account.key,state_now)
                    return receipt
            return await self.runtime.run(complete_probe,settle_on_cancel=True)
        if probe["outcome"] != "provider_auth_failed": return probe
        # Browser-attempt limits belong to the stable host/provider/account cell,
        # while operation receipts remain bound to their original generation.
        limits_key = "cell:" + ":".join((account.host, account.provider, account.alias))
        limits = await self.runtime.run(lambda ledger: self._read_recovery(ledger, limits_key))
        attempts = [moment for moment in limits.get("attempts", []) if 0 <= now - moment < 3600]
        if len(attempts) >= 3: return {"outcome": "recovery_capped"}
        if limits.get("last_attempt") is not None and now - limits["last_attempt"] < 900:
            return {"outcome": "recovery_cooldown"}
        from account_repin_state import reserve_generation,mutation_status
        await mutation_status(self,account)
        intent=await self.runtime.run(lambda a:reserve_generation(a,account,operation_id,uuid.uuid4().hex,"enroll",now=self.runtime.clock()),settle_on_cancel=True)
        state.update(inflight=operation_id,new_generation=intent["arguments"]["generation"],human_stop=False,
            mutation_intent=intent,mutation_stage="prepared")
        await self.runtime.run(lambda a:self._save_recovery(a,account.key,state),settle_on_cancel=True)
        return await self._dispatch_recovery(account,state)

    async def _inspect_recovery(self,account,state):
        if state.get("mutation_stage")=="prepared":return await self._dispatch_recovery(account,state)
        result=await self.execute(account,"status",state["inflight"],state["new_generation"])
        return await self._finish_recovery(account,state,result,state["inflight"])

    async def _dispatch_recovery(self,account,state):
        from account_repin_state import host_status,permit,require_generation_clear
        operation=state["inflight"];now=self.runtime.clock()
        try:
            remote=await host_status(self,account)
            clearance=await permit(self,account,"enroll",operation,state["new_generation"],remote,state["mutation_intent"])
            def started(a):
                with transaction(a.conn):
                    require_generation_clear(a,{"host":account.host,"provider":account.provider,"account":account.alias})
                    from provider_cli_maintenance import require_mutation
                    require_mutation(a,account,self.runtime.clock())
                    row=a._cell(account.host,account.provider,account.alias)
                    if row is None or Binding(**json.loads(row["data"])).public()!=account.public():raise AccountError("account_login_generation_stale")
                    key="cell:"+":".join((account.host,account.provider,account.alias))
                    limits=self._read_recovery(a,key)
                    attempts=[t for t in limits.get("attempts",[]) if 0<=now-t<3600]
                    if len(attempts)>=3:raise AccountError("recovery_capped")
                    if limits.get("last_attempt") is not None and now-limits["last_attempt"]<900:raise AccountError("recovery_cooldown")
                    self._write_recovery(a,key,{**limits,"attempts":attempts+[now],"last_attempt":now})
                    state["mutation_stage"]="started"
                    self._write_recovery(a,account.key,state)
            await self.runtime.run(started,settle_on_cancel=True)
            result=await self.execute(account,"enroll",operation,state["new_generation"],reconcile={"_clearance":clearance})
        except AccountError:return {"outcome":"execution_unresolved"}
        return await self._finish_recovery(account,state,result,operation)

    async def _finish_recovery(self, old, state, result, operation_id):
        if result.get("state") != "done": return {"outcome": "execution_unresolved"}
        if result.get("outcome") == "ok":
            raw = result.get("binding") or {}
            binding = Binding.from_public(raw)
            if (binding.host != old.host or binding.provider != old.provider or binding.alias != old.alias
                    or binding.generation != state["new_generation"] or binding.provider_id != old.provider_id
                    or binding.pool_id != old.pool_id or binding.home == old.home or not binding.enabled or not binding.healthy):
                raise AccountError("account_host_binding_invalid")
            result = {"outcome": "recovered", "binding": binding.public()}
        else:
            result = {"outcome": result.get("outcome", "account_enrollment_failed")}
            state["human_stop"] = result["outcome"] in {"human_reseed_required", "cancelled", "locked_keychain"}
        state.pop("inflight", None)
        state.pop("mutation_stage",None)
        state.setdefault("receipts", {})[operation_id] = result
        def commit(ledger):
            from account_repin_state import require_generation_clear,complete_generation
            with transaction(ledger.conn):
                require_generation_clear(ledger,{"host":old.host,"provider":old.provider,"account":old.alias})
                row=ledger._cell(old.host,old.provider,old.alias)
                if row is None or Binding(**json.loads(row["data"])).public()!=old.public():
                    raise AccountError("account_login_generation_stale")
                if result["outcome"]=="recovered":ledger._register(binding)
                self._write_recovery(ledger,old.key,state)
                complete_generation(ledger,old,operation_id)
        await self.runtime.run(commit,settle_on_cancel=True)
        return result

    @staticmethod
    def _prior_recovery(ledger, account, operation_id):
        pending = None
        for row in ledger.conn.execute(
                "SELECT b.data AS binding, r.data AS recovery FROM v2_account_bindings b "
                "JOIN v2_account_recovery r ON r.binding_key=b.binding_key "
                "WHERE b.host=? AND b.provider=? AND b.alias=? AND b.binding_key<>?",
                (account.host, account.provider, account.alias, account.key)):
            state = json.loads(row["recovery"])
            owner = Binding(**json.loads(row["binding"]))
            receipt=state.get("receipts",{}).get(operation_id)
            # A successful probe does not rotate generations. Its receipt is
            # scoped to that binding key, so an accidental ID reuse on a later
            # generation must not suppress the new generation's own probe.
            if receipt and receipt.get("relogin_probe") == "ok": continue
            if receipt: return owner, state
            if state.get("inflight"): pending = owner, state
        return pending

    @staticmethod
    def _read_recovery(ledger, key):
        row = ledger.conn.execute("SELECT data FROM v2_account_recovery WHERE binding_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    @staticmethod
    def _save_recovery(ledger, key, state):
        with transaction(ledger.conn):
            AccountExecutor._write_recovery(ledger, key, state)

    @staticmethod
    def _write_recovery(ledger, key, state):
        ledger.conn.execute("INSERT OR REPLACE INTO v2_account_recovery VALUES (?,?,?)",
                            (key, "pending" if state.get("inflight") else "idle", json.dumps(state)))

    async def collect_all(self):
        cells = await self.runtime.run(lambda accounts: [Binding(**json.loads(row[0])) for row in accounts.conn.execute(
            "SELECT data FROM v2_account_bindings WHERE active=1")])
        await asyncio.gather(*(self.observe(account) for account in cells
            if account.enabled and self.runtime.manages(account.host,account.provider)))
