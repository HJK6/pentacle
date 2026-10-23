"""Finite daemon dispatch of bound host probes and native re-login generations."""
from __future__ import annotations
import asyncio
import json
import hashlib
import time
import uuid

from account_store import transaction
from provider_accounts import AccountError, Binding


class AccountExecutor:
    def __init__(self, runtime, hosts, *, execute=None):
        self.runtime, self.hosts = runtime, hosts
        self.execute = execute or self._execute
        self._host_locks = {}

    async def _execute(self, account, action, operation_id, generation=None):
        configuration = self.runtime.policy.get("executors", {}).get(account.host)
        if not configuration or not self.hosts: raise AccountError("account_host_executor_unavailable")
        # The daemon's authenticated Hosts transport selects the destination.
        # Only paths in private host configuration can select an executor.
        args = [configuration["python"], configuration["script"], action,
                "--provider", account.provider, "--account", account.alias,
                "--login-generation", account.generation, "--operation-id", hashlib.sha256(operation_id.encode()).hexdigest()]
        if generation: args += ["--new-generation", generation]
        try:
            code, output = await self.hosts.run_command(account.host, *args, timeout=320 if action == "enroll" else 100)
            if len(output) > 65536: raise AccountError("account_host_response_invalid")
            result = json.loads(output)
        except AccountError: raise
        except Exception: raise AccountError("account_host_unreachable") from None
        if not isinstance(result, dict) or not isinstance(result.get("outcome"), str):
            raise AccountError("account_host_response_invalid")
        if code and result["outcome"] not in {
            "provider_auth_failed", "provider_quota_blocked", "provider_capacity", "provider_transport_failed",
            "provider_permission_denied", "provider_timeout", "provider_probe_failed", "provider_identity_changed",
            "subscription_identity_unverified", "provider_identity_unavailable", "provider_identity_or_cache_unverified",
            "provider_executable_changed", "provider_version_changed", "provider_cache_unavailable", "provider_windows_invalid",
            "account_login_generation_stale"}:
            raise AccountError("account_host_failed")
        return result

    async def observe(self, account):
        async with self._host_locks.setdefault(account.host, asyncio.Lock()):
            return await self._observe_locked(account)

    async def _observe_locked(self, account):
        try:
            result = await self.execute(account, "observe", uuid.uuid4().hex)
            observation = result.get("observation")
            if result["outcome"] == "ok" and isinstance(observation, dict) and result.get("binding_key") == account.key:
                await self.runtime.record_observation(account, observation)
                return result
            kind = {"provider_auth_failed": "auth", "provider_quota_blocked": "quota",
                    "provider_capacity": "capacity", "provider_transport_failed": "network",
                    "provider_permission_denied": "permission", "subscription_identity_unverified": "identity",
                    "provider_identity_changed": "identity", "provider_identity_or_cache_unverified": "identity"}.get(result["outcome"])
            if kind:
                await self.runtime.record_failure(account, kind)
            await self._failed_observation(account)
            return {"outcome": result["outcome"], "host": account.host, "provider": account.provider, "account": account.alias}
        except AccountError as exc:
            await self._failed_observation(account)
            return {"outcome": str(exc), "host": account.host, "provider": account.provider, "account": account.alias}

    async def _failed_observation(self, account):
        await self.runtime.run(lambda ledger: ledger.observe(account.key, {
            "provider_id": account.provider_id, "pool_id": account.pool_id, "auth_mode": "subscription",
            "collection": "failed", "observed_at": None, "windows": {}}, now=self.runtime.clock()))

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
                result = await self.execute(account, "status", operation_id, state["new_generation"])
                return await self._finish_recovery(account, state, result, operation_id)
            return None

    async def _recover_locked(self, account, *, relogin, operation_id):
        now = self.runtime.clock()
        prior = await self.runtime.run(lambda ledger: self._prior_recovery(ledger, account, operation_id))
        if prior:
            owner, prior_state = prior
            if operation_id in prior_state.get("receipts", {}): return prior_state["receipts"][operation_id]
            result = await self.execute(owner, "status", prior_state["inflight"], prior_state["new_generation"])
            return await self._finish_recovery(owner, prior_state, result, prior_state["inflight"])
        state = await self.runtime.run(lambda ledger: self._read_recovery(ledger, account.key))
        receipts = state.get("receipts", {})
        if operation_id in receipts: return receipts[operation_id]
        if state.get("inflight"):
            # After an uncertain remote reply, only inspect the same generation.
            result = await self.execute(account, "status", state["inflight"], state["new_generation"])
            return await self._finish_recovery(account, state, result, state["inflight"])
        # Confirm an actual auth failure before a requested re-login. A quota,
        # network or capacity failure never enters native enrollment.
        probe = await self._observe_locked(account)
        if probe["outcome"] == "ok": return {"outcome": "healthy"}
        if probe["outcome"] != "provider_auth_failed": return probe
        # Browser-attempt limits belong to the stable host/provider/account cell,
        # while operation receipts remain bound to their original generation.
        limits_key = "cell:" + ":".join((account.host, account.provider, account.alias))
        limits = await self.runtime.run(lambda ledger: self._read_recovery(ledger, limits_key))
        attempts = [moment for moment in limits.get("attempts", []) if 0 <= now - moment < 3600]
        if len(attempts) >= 3: return {"outcome": "recovery_capped"}
        if limits.get("last_attempt") is not None and now - limits["last_attempt"] < 900:
            return {"outcome": "recovery_cooldown"}
        state.update(inflight=operation_id, new_generation=uuid.uuid4().hex, human_stop=False)
        limits.update(attempts=attempts + [now], last_attempt=now)
        def save_attempt(ledger):
            with transaction(ledger.conn):
                self._write_recovery(ledger, account.key, state)
                self._write_recovery(ledger, limits_key, limits)
        await self.runtime.run(save_attempt)
        try: result = await self.execute(account, "enroll", operation_id, state["new_generation"])
        except AccountError:
            # The host may still be authorizing. Persist the outstanding call;
            # a daemon restart must never turn it into another login attempt.
            return {"outcome": "execution_unresolved"}
        return await self._finish_recovery(account, state, result, operation_id)

    async def _finish_recovery(self, old, state, result, operation_id):
        if result.get("state") != "done": return {"outcome": "execution_unresolved"}
        if result.get("outcome") == "ok":
            raw = result.get("binding") or {}
            binding = Binding.from_public(raw)
            if (binding.host != old.host or binding.provider != old.provider or binding.alias != old.alias
                    or binding.generation != state["new_generation"] or binding.provider_id != old.provider_id
                    or binding.pool_id != old.pool_id or binding.home == old.home or not binding.enabled or not binding.healthy):
                raise AccountError("account_host_binding_invalid")
            await self.runtime.run(lambda ledger: ledger.register(binding))
            result = {"outcome": "recovered", "binding": binding.public()}
        else:
            result = {"outcome": result.get("outcome", "account_enrollment_failed")}
            state["human_stop"] = result["outcome"] in {"human_reseed_required", "cancelled", "locked_keychain"}
        state.pop("inflight", None)
        state.setdefault("receipts", {})[operation_id] = result
        await self.runtime.run(lambda ledger: self._save_recovery(ledger, old.key, state))
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
            if operation_id in state.get("receipts", {}): return owner, state
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
        await asyncio.gather(*(self.observe(account) for account in cells if account.enabled and self.runtime.manages(account.host)))
