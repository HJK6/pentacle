"""Account inventory, atomic defaults and immutable launch attribution.

Only explicit choices select accounts. Quota observations are readback for the
front desk; this module does not schedule, reserve allowance or migrate agents.
"""
from __future__ import annotations
import contextlib
from dataclasses import asdict, replace
import json
import math
import re
import sqlite3

from provider_accounts import AccountError, Binding, _component

DDL = """
CREATE TABLE IF NOT EXISTS v2_account_bindings (
    binding_key TEXT PRIMARY KEY, provider TEXT NOT NULL, alias TEXT NOT NULL,
    host TEXT NOT NULL, generation TEXT NOT NULL, pool_id TEXT NOT NULL,
    data TEXT NOT NULL, health TEXT NOT NULL, active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS v2_account_defaults (
    host TEXT NOT NULL, provider TEXT NOT NULL, alias TEXT NOT NULL,
    revision INTEGER NOT NULL, PRIMARY KEY(host,provider));
CREATE TABLE IF NOT EXISTS v2_account_session_bindings (
    stream_id TEXT NOT NULL, generation TEXT NOT NULL, binding_key TEXT NOT NULL,
    data TEXT NOT NULL, PRIMARY KEY(stream_id,generation));
CREATE TABLE IF NOT EXISTS v2_account_observations (
    binding_key TEXT PRIMARY KEY, data TEXT NOT NULL, received_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS v2_account_failures (
    binding_key TEXT PRIMARY KEY, kind TEXT NOT NULL, observed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS v2_account_recovery (
    binding_key TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v2_account_health_revalidations (
    operation_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, binding_key TEXT NOT NULL,
    epoch TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
"""


def migrate(conn):
    for statement in DDL.split(";"):
        if statement.strip(): conn.execute(statement)
    if "health_revision" not in {row[1] for row in conn.execute("PRAGMA table_info(v2_account_bindings)")}:
        conn.execute("ALTER TABLE v2_account_bindings ADD COLUMN health_revision INTEGER NOT NULL DEFAULT 0")


@contextlib.contextmanager
def transaction(conn):
    if conn.in_transaction: raise AccountError("account_transaction_nested")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def number(value):
    return isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value)


class AccountStore:
    def __init__(self, conn: sqlite3.Connection, policy: dict, *, initialize=True):
        self.conn,self.policy=conn,policy
        if initialize: migrate(conn); conn.commit()

    def register(self,binding: Binding):
        with transaction(self.conn):
            self._register(binding)

    def _register(self,binding: Binding):
        """Caller owns the transaction; used for atomic verified imports."""
        for value in (binding.alias,binding.host,binding.generation): _component(value)
        if (not binding.provider_id or not binding.pool_id or binding.provider not in {"codex","claude"}
                or binding.default_revision != 0):
            raise AccountError("account_binding_unverified")
        data=asdict(binding)
        prior=self.conn.execute("SELECT data FROM v2_account_bindings WHERE binding_key=?",(binding.key,)).fetchone()
        if prior and json.loads(prior[0]) != data: raise AccountError("account_binding_immutable")
        self.conn.execute("UPDATE v2_account_bindings SET active=0 WHERE host=? AND provider=? AND alias=? AND binding_key<>?",
            (binding.host,binding.provider,binding.alias,binding.key))
        self.conn.execute("INSERT OR IGNORE INTO v2_account_bindings "
            "(binding_key,provider,alias,host,generation,pool_id,data,health,active) VALUES (?,?,?,?,?,?,?,?,?)",(
            binding.key,binding.provider,binding.alias,binding.host,binding.generation,binding.pool_id,
            json.dumps(data),"healthy" if binding.healthy else "unknown",1))

    def bound(self,key):
        row=self.conn.execute("SELECT data FROM v2_account_bindings WHERE binding_key=?",(key,)).fetchone()
        return Binding(**json.loads(row[0])) if row else None

    def _available(self,row):
        if row is None: raise AccountError("account_unconfigured")
        binding=Binding(**json.loads(row["data"]))
        if not binding.enabled: raise AccountError("account_disabled")
        if row["health"] != "healthy": raise AccountError("account_unavailable")
        return binding

    def _cell(self,host,provider,alias):
        _component(host); _component(alias)
        if provider not in {"codex","claude"}: raise AccountError("account_provider_invalid")
        return self.conn.execute("SELECT * FROM v2_account_bindings WHERE host=? AND provider=? AND alias=? AND active=1",
            (host,provider,alias)).fetchone()

    def set_default(self,*,host,provider,alias):
        with transaction(self.conn):
            self._available(self._cell(host,provider,alias))
            self.conn.execute("INSERT INTO v2_account_defaults VALUES (?,?,?,1) ON CONFLICT(host,provider) "
                "DO UPDATE SET alias=excluded.alias,revision=revision+1",(host,provider,alias))
            return dict(self.conn.execute("SELECT * FROM v2_account_defaults WHERE host=? AND provider=?",(host,provider)).fetchone())

    def defaults(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM v2_account_defaults ORDER BY host,provider")]

    def codex_default_home(self, *, host):
        """Read only the default's non-secret probe fields; never select a binding payload."""
        _component(host)
        rows = self.conn.execute(
            "SELECT d.alias, json_extract(b.data,'$.home'), "
            "json_extract(b.data,'$.enabled'), b.health "
            "FROM v2_account_defaults d JOIN v2_account_bindings b "
            "ON b.host=d.host AND b.provider=d.provider AND b.alias=d.alias "
            "WHERE d.host=? AND d.provider='codex' AND b.active=1", (host,),
        ).fetchmany(2)
        if len(rows) != 1:
            raise AccountError("account_default_unavailable")
        alias, home, enabled, health = rows[0]
        _component(alias)
        if type(enabled) is not int or enabled != 1 or health != "healthy" or not isinstance(home, str) or not home:
            raise AccountError("account_default_unavailable")
        return home

    def select(self,*,host,provider,alias=None):
        try:
            if self.policy.get("enabled") is not True: raise AccountError("account_feature_off")
            with transaction(self.conn):
                revision=0
                if alias is None:
                    default=self.conn.execute("SELECT * FROM v2_account_defaults WHERE host=? AND provider=?",(host,provider)).fetchone()
                    if default is None: raise AccountError("account_default_unconfigured")
                    alias,revision=default["alias"],default["revision"]
                return replace(self._available(self._cell(host,provider,alias)),default_revision=revision)
        except AccountError as exc:
            exc.context.update(host=host,provider=provider,account=alias or "unconfigured")
            raise

    def ensure_available(self,key):
        return self._available(self.conn.execute("SELECT * FROM v2_account_bindings WHERE binding_key=?",(key,)).fetchone())

    def bind_session(self,row):
        with transaction(self.conn):
            self._bind_session(row)

    def _bind_session(self,row):
        account=row.get("account_binding")
        if not account: return
        key=":".join(str(account.get(k) or "") for k in ("host","provider","alias","generation"))
        binding=self.ensure_available(key)
        if binding.public() != dict(account,default_revision=0): raise AccountError("account_launch_binding_mismatch")
        existing=self.conn.execute("SELECT data FROM v2_account_session_bindings WHERE stream_id=? AND generation=?",
            (row["stream_id"],row["session_generation"])).fetchone()
        if existing and json.loads(existing[0]) != account: raise AccountError("account_launch_binding_mismatch")
        self.conn.execute("INSERT OR IGNORE INTO v2_account_session_bindings VALUES (?,?,?,?)",
            (row["stream_id"],row["session_generation"],key,json.dumps(account)))

    def observe(self,key,observation,*,now):
        with transaction(self.conn):
            return self._observe(key,observation,now=now)

    def _observe(self,key,observation,*,now):
        """Caller owns the transaction; validation matches ordinary observation."""
        binding=self.bound(key)
        if binding is None: raise AccountError("account_generation_unknown")
        allowed={k:observation.get(k) for k in (
            "provider_id","pool_id","auth_mode","collection","observed_at","windows","ordinary_usage_allowed")}
        if (allowed["provider_id"]!=binding.provider_id or allowed["pool_id"]!=binding.pool_id
                or allowed["auth_mode"]!="subscription"):
            self.conn.execute("UPDATE v2_account_bindings SET health='identity_conflict', "
                "health_revision=health_revision+1 WHERE binding_key=?",(key,))
            allowed["collection"]="identity_conflict"
        if allowed["collection"]=="failed":
            code=observation.get("error_code")
            if isinstance(code,str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}",code):
                allowed["error_code"]=code
            raw=self.conn.execute("SELECT data FROM v2_account_observations WHERE binding_key=?",(key,)).fetchone()
            previous=json.loads(raw[0]) if raw else {}
            if (all(previous.get(k)==allowed[k] for k in ("provider_id","pool_id","auth_mode"))
                    and previous.get("collection") in {"ok","failed"}
                    and number(previous.get("observed_at")) and previous.get("windows")):
                # Keep the upstream timestamp: a failed fetch never refreshes quota.
                allowed.update(observed_at=previous["observed_at"],windows=previous["windows"])
        # A quota probe never clears an auth failure or identity conflict.
        self.conn.execute("INSERT OR REPLACE INTO v2_account_observations VALUES (?,?,?)",(key,json.dumps(allowed),now))
        return allowed["collection"]!="identity_conflict"

    def failure(self,key,kind,*,now):
        if kind not in {"auth","quota","capacity","network","permission","identity"}: raise AccountError("provider_signal_invalid")
        with transaction(self.conn):
            if self.bound(key) is None: raise AccountError("account_generation_unknown")
            if kind=="auth":
                self.conn.execute("UPDATE v2_account_bindings SET health=CASE WHEN health='identity_conflict' "
                    "THEN health ELSE 'auth_unhealthy' END, health_revision=health_revision+1 WHERE binding_key=?",(key,))
            if kind=="identity":
                self.conn.execute("UPDATE v2_account_bindings SET health='identity_conflict', "
                    "health_revision=health_revision+1 WHERE binding_key=?",(key,))
            self.conn.execute("INSERT OR REPLACE INTO v2_account_failures VALUES (?,?,?)",(key,kind,now))

    def readback(self,*,now):
        accounts=[]; pools=[]
        rows=list(self.conn.execute("SELECT * FROM v2_account_bindings ORDER BY provider,alias,host,generation"))
        for row in rows:
            binding=Binding(**json.loads(row["data"]))
            raw=self.conn.execute("SELECT data FROM v2_account_observations WHERE binding_key=?",(binding.key,)).fetchone()
            failure=self.conn.execute("SELECT kind,observed_at FROM v2_account_failures WHERE binding_key=?",(binding.key,)).fetchone()
            accounts.append(binding.public()|{"health":row["health"] if binding.enabled else "disabled","active":bool(row["active"]),
                "health_revision":row["health_revision"], "observation":json.loads(raw[0]) if raw else None,
                "last_failure":dict(failure) if failure else None})
        for cell in self.policy.get("cells",[]):
            if not isinstance(cell,dict): continue
            host,provider,alias=(cell.get(k) for k in ("host","provider","alias"))
            _component(host); _component(alias)
            if provider not in {"codex","claude"}: raise AccountError("account_provider_invalid")
            if not any((a["host"],a["provider"],a["alias"],a["active"])==(host,provider,alias,True) for a in accounts):
                accounts.append({"host":host,"provider":provider,"alias":alias,"active":False,"enabled":False,
                    "pool_id":None,"provider_id":None,"generation":None,"health":"unconfigured","observation":None})
        for provider,pool in sorted({(r["provider"],r["pool_id"]) for r in rows}):
            sources=[a for a in accounts if a["provider"]==provider and a["pool_id"]==pool and a["active"]]
            measured=[a for a in sources if a["health"]=="healthy" and isinstance(a["observation"],dict)
                and a["observation"].get("collection")=="ok" and number(a["observation"].get("observed_at"))]
            best=max(measured,key=lambda a:a["observation"]["observed_at"],default=None)
            if best is None:
                retained=[a for a in sources if a["health"]=="healthy" and isinstance(a["observation"],dict)
                    and a["observation"].get("collection")=="failed" and number(a["observation"].get("observed_at"))]
                best=max(retained,key=lambda a:a["observation"]["observed_at"],default=None)
            data=best["observation"] if best else None
            age=now-data["observed_at"] if data else None
            # Freshness is informational and uses the collector's existing cadence;
            # it never admits/denies a launch or asserts zero usage for missing data.
            max_age=self.policy.get("observation_max_age",600)
            windows=data.get("windows") if data else None
            complete=isinstance(windows,dict) and bool(windows) and all(
                isinstance(w,dict) and number(w.get("pct")) and 0<=w["pct"]<=100
                and number(w.get("reset_at")) and w["reset_at"]>now
                and isinstance(w.get("reset_id"),str) and bool(w["reset_id"]) for w in windows.values())
            conflict=any(a["health"]=="identity_conflict" for a in sources)
            pools.append({"provider":provider,"pool_id":pool,"sources":[a["host"]+":"+a["generation"] for a in sources],
                "source_binding":best["host"]+":"+provider+":"+best["alias"]+":"+best["generation"] if best else None,
                "observation":data,"age_seconds":age,"fresh":bool(complete and data.get("collection")=="ok" and not conflict and age is not None and number(max_age) and 0<=age<=max_age),
                "error_codes":{a["host"]+":"+provider+":"+a["alias"]+":"+a["generation"]:a["observation"]["error_code"]
                    for a in sources if isinstance(a["observation"],dict) and a["observation"].get("error_code")},
                "identity_conflict":conflict,
                "cap":self.policy.get("account_caps",{}).get(provider,{}).get(pool)})
        return {"accounts":accounts,"quota_pools":pools,"defaults":self.defaults(),"enabled":self.policy.get("enabled") is True}
