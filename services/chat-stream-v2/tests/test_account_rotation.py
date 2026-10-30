"""Explicit choices and saved defaults; no alternate-account machinery."""
from dataclasses import replace
import sqlite3

import pytest
from account_store import AccountStore
from provider_accounts import AccountError, Binding

NOW = 2000000000

def binding(alias="primary", host="host-a", provider="codex"):
    return Binding(provider=provider, alias=alias, host=host, home=f"/private-fixture/{host}/{provider}/{alias}/login-one",
        generation="login-one", profile_ref="fixture-profile", executable="/fixture/provider", version="0.160.0" if provider=="codex" else "2.1.312", platform="macos", executable_sha256="a"*64,
        provider_id=f"native-{alias}", pool_id=f"pool-{alias}", enabled=True, healthy=True)

def policy(): return {"enabled": True}

def observation(account, stamp=NOW):
    return {"provider_id": account.provider_id, "pool_id": account.pool_id, "auth_mode": "subscription", "collection": "ok",
        "observed_at": stamp, "windows": {"weekly": {"pct": 21, "reset_at": NOW + 1000, "reset_id": "period"}}}

def inventory(path=":memory:"):
    conn=sqlite3.connect(path); conn.row_factory=sqlite3.Row
    accounts=AccountStore(conn, policy())
    for alias in ("primary", "spare"): accounts.register(binding(alias))
    return accounts

def test_explicit_choice_never_mutates_saved_default_and_restart_keeps_revision(tmp_path):
    path=tmp_path/"state.db"; accounts=inventory(path)
    with pytest.raises(AccountError, match="account_default_unconfigured"):
        accounts.select(host="host-a", provider="codex")
    assert accounts.set_default(host="host-a", provider="codex", alias="primary")["revision"] == 1
    selected=accounts.select(host="host-a", provider="codex")
    assert selected.alias == "primary" and selected.default_revision == 1
    accounts.set_default(host="host-a", provider="codex", alias="spare")
    assert accounts.select(host="host-a", provider="codex", alias="primary").alias == "primary"
    accounts.conn.close()
    conn=sqlite3.connect(path); conn.row_factory=sqlite3.Row
    restarted=AccountStore(conn, policy())
    current=restarted.select(host="host-a", provider="codex")
    assert current.alias == "spare" and current.default_revision == 2
    assert selected.alias == "primary" and selected.default_revision == 1

def test_failure_is_local_and_does_not_select_an_alternate_or_rewrite_default():
    accounts=inventory(); other=binding(host="host-b"); accounts.register(other)
    accounts.set_default(host="host-a",provider="codex",alias="primary")
    accounts.failure(binding().key,"auth",now=NOW)
    with pytest.raises(AccountError,match="account_unavailable"):
        accounts.select(host="host-a",provider="codex")
    assert accounts.select(host="host-b",provider="codex",alias="primary").key == other.key
    assert accounts.select(host="host-a",provider="codex",alias="spare").alias == "spare"
    assert accounts.defaults()[0]["alias"] == "primary"
    # Even a later successful quota probe cannot silently enable a broken login.
    accounts.observe(binding().key,observation(binding()),now=NOW)
    with pytest.raises(AccountError,match="account_unavailable"):
        accounts.select(host="host-a",provider="codex")

@pytest.mark.parametrize("alias,provider",[("unknown","codex"),("primary","claude")])
def test_unknown_and_wrong_provider_refuse_without_fallback(alias,provider):
    accounts=inventory()
    with pytest.raises(AccountError,match="account_unconfigured"):
        accounts.select(host="host-a",provider=provider,alias=alias)
    assert accounts.defaults() == []

def test_disabled_and_identity_conflict_refuse():
    accounts=inventory(); disabled=replace(binding("disabled"),enabled=False)
    accounts.register(disabled)
    with pytest.raises(AccountError,match="account_disabled"):
        accounts.select(host="host-a",provider="codex",alias="disabled")
    accounts.observe(binding().key,observation(binding("spare")),now=NOW)
    with pytest.raises(AccountError,match="account_unavailable"):
        accounts.set_default(host="host-a",provider="codex",alias="primary")

def test_quota_is_readback_and_does_not_relogin_or_change_selection():
    accounts=inventory(); accounts.set_default(host="host-a",provider="codex",alias="primary")
    accounts.failure(binding().key,"quota",now=NOW)
    assert accounts.select(host="host-a",provider="codex").alias == "primary"
    assert accounts.readback(now=NOW)["accounts"][0]["last_failure"]["kind"] == "quota"

def test_observations_share_pool_without_summing_and_preserve_unknown():
    accounts=inventory(); second=binding(host="host-b"); accounts.register(second)
    accounts.observe(binding().key,observation(binding()),now=NOW)
    accounts.observe(second.key,observation(second),now=NOW)
    pools=accounts.readback(now=NOW)["quota_pools"]
    shared=next(p for p in pools if p["pool_id"]=="pool-primary")
    assert shared["observation"]["windows"]["weekly"]["pct"] == 21
    assert len(shared["sources"]) == 2
    assert next(p for p in pools if p["pool_id"]=="pool-spare")["observation"] is None
    assert not next(p for p in pools if p["pool_id"]=="pool-spare")["fresh"]

def test_default_resolution_is_transactional_with_second_connection(tmp_path):
    accounts=inventory(tmp_path/"state.db")
    accounts.set_default(host="host-a",provider="codex",alias="primary")
    other=sqlite3.connect(tmp_path/"state.db"); other.row_factory=sqlite3.Row
    writer=AccountStore(other,policy())
    before=accounts.select(host="host-a",provider="codex")
    writer.set_default(host="host-a",provider="codex",alias="spare")
    after=accounts.select(host="host-a",provider="codex")
    assert (before.alias,before.default_revision)==("primary",1)
    assert (after.alias,after.default_revision)==("spare",2)


def test_readback_keeps_caps_shared_and_missing_windows_unknown():
    accounts=inventory(); accounts.policy["account_caps"]={"codex":{"pool-primary":{"weekly":37}}}
    accounts.policy["cells"]=[{"host":"host-a","provider":"claude","alias":"primary"}]
    partial=observation(binding()); partial["windows"]={}
    accounts.observe(binding().key,partial,now=NOW)
    result=accounts.readback(now=NOW)
    pool=next(p for p in result["quota_pools"] if p["pool_id"]=="pool-primary")
    assert pool["cap"]=={"weekly":37} and not pool["fresh"]
    missing=next(a for a in result["accounts"] if a["provider"]=="claude")
    assert missing["health"]=="unconfigured" and missing["pool_id"] is None
