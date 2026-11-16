"""Real collector ticks, disposable account ledgers and quota-only child scripts."""
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from account_store import AccountStore
from provider_accounts import Binding

SERVICE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("default_home_collector", SERVICE / "tools/collect_usage_state.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "sessions.db")
    conn.row_factory = sqlite3.Row
    store = AccountStore(conn, {})
    homes = [tmp_path / name for name in ("first", "second")]
    bindings = []
    for alias, home in zip(("primary", "secondary"), homes):
        home.mkdir()
        binding = Binding(provider="codex", alias=alias, host="fixture-host", home=str(home),
                          generation="login-1", profile_ref="fixture", executable="/fixture/codex",
                          version="1", platform="macos", provider_id="id-" + alias,
                          pool_id="pool-" + alias, enabled=True, healthy=True)
        store.register(binding)
        bindings.append(binding)
    store.set_default(host="fixture-host", provider="codex", alias="primary")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check_codex_usage.py").write_text('''import json,os
from pathlib import Path
from datetime import datetime,timezone,timedelta
home=os.environ.get('CODEX_HOME') or str(Path.home()/'.codex')
with open(os.environ['FIXTURE_LOG'],'a') as log:
    log.write(json.dumps({'home':home,'secret_present':'FIXTURE_SECRET' in os.environ})+'\\n')
print(json.dumps({'pct':{'first':10,'second':80,'replacement':35}.get(Path(home).name,99),
 'resets_at_iso':(datetime.now(timezone.utc)+timedelta(days=3)).isoformat(),
 'resets_text':'fixture reset','upstream_reported_at':datetime.now(timezone.utc).isoformat()}))
''')
    (scripts / "check_claude_usage.py").write_text('''import json,os
with open(os.environ['FIXTURE_CLAUDE_LOG'],'a') as log:log.write('tick\\n')
print(json.dumps({'week_all_pct':20,'week_all_resets':'fixture',
                 'week_fable_pct':5,'week_fable_resets':'fixture'}))
''')
    monkeypatch.setenv("PENTACLE_HOST_ID", "fixture-host")
    monkeypatch.setenv("CODEX_HOME", str(homes[0]))
    monkeypatch.setenv("FIXTURE_SECRET", "inert-private-sentinel")
    monkeypatch.setenv("FIXTURE_LOG", str(tmp_path / "codex.jsonl"))
    monkeypatch.setenv("FIXTURE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    state = tmp_path / "usage_state.json"

    def tick(*, ledger=True, extra=()):
        args = ["--state", str(state), "--shared-scripts", str(scripts),
                "--claude-config", str(tmp_path / "absent.json")]
        if ledger:
            args += ["--account-ledger", str(tmp_path / "sessions.db")]
        assert tool.main(args + list(extra)) == 0
        return json.loads(state.read_text())

    yield SimpleNamespace(conn=conn, store=store, homes=homes, bindings=bindings,
                          tick=tick, root=tmp_path, state=state)
    conn.close()


def test_default_switch_and_active_generation_take_effect_each_tick(rig):
    assert rig.tick()["codex_lkg"]["pct"] == 10
    rig.store.set_default(host="fixture-host", provider="codex", alias="secondary")
    assert rig.tick()["codex_lkg"]["pct"] == 80
    rig.store.set_default(host="fixture-host", provider="codex", alias="primary")
    assert rig.tick()["codex_lkg"]["pct"] == 10
    replacement = rig.root / "replacement"
    replacement.mkdir()
    rig.store.register(replace(rig.bindings[0], generation="login-2", home=str(replacement)))
    assert rig.tick()["codex_lkg"]["pct"] == 35
    records = [json.loads(line) for line in (rig.root / "codex.jsonl").read_text().splitlines()]
    assert [r["home"] for r in records] == [str(rig.homes[0]), str(rig.homes[1]), str(rig.homes[0]), str(replacement)]
    assert not any(r["secret_present"] for r in records)
    assert os.environ["CODEX_HOME"] == str(rig.homes[0])


def test_unflagged_keeps_ambient_behavior_and_never_opens_ledger(rig, monkeypatch):
    rig.store.set_default(host="fixture-host", provider="codex", alias="secondary")
    def forbidden(*args, **kwargs):
        pytest.fail("unflagged collector opened account DB")
    monkeypatch.setattr(tool.sqlite3, "connect", forbidden)
    assert rig.tick(ledger=False)["codex_lkg"]["pct"] == 10


@pytest.mark.parametrize("failure", ["default", "foreign-host", "disabled", "identity_conflict",
    "auth_unhealthy", "unknown", "malformed", "home-type", "relative", "missing-home",
    "not-directory", "unreadable-home", "ambiguous", "missing-db", "busy", "import",
    "enabled-type", "enabled-float"])
def test_failed_lookup_retains_receipts_no_wrong_probe_and_claude_continues(rig, monkeypatch, failure):
    before = rig.tick()
    key = rig.bindings[0].key
    blocker = None
    if failure in {"default", "foreign-host"}:
        rig.conn.execute("DELETE FROM v2_account_defaults")
        if failure == "foreign-host":
            rig.conn.execute("INSERT INTO v2_account_defaults VALUES ('other-host','codex','primary',1)")
    elif failure == "disabled":
        rig.conn.execute("UPDATE v2_account_bindings SET data=json_set(data,'$.enabled',json('false')) WHERE binding_key=?", (key,))
    elif failure in {"enabled-type", "enabled-float"}:
        rig.conn.execute("UPDATE v2_account_bindings SET data=json_set(data,'$.enabled',?) WHERE binding_key=?",
                         ("true" if failure == "enabled-type" else 1.0, key))
    elif failure in {"identity_conflict", "auth_unhealthy", "unknown"}:
        rig.conn.execute("UPDATE v2_account_bindings SET health=? WHERE binding_key=?", (failure, key))
    elif failure == "malformed":
        rig.conn.execute("UPDATE v2_account_bindings SET data='{' WHERE binding_key=?", (key,))
    elif failure in {"home-type", "relative", "missing-home", "not-directory"}:
        if failure == "not-directory":
            path = rig.root / "file"
            path.write_text("not a directory")
        else:
            path = {"home-type": 7, "relative": "relative", "missing-home": str(rig.root / "absent")}[failure]
        rig.conn.execute("UPDATE v2_account_bindings SET data=json_set(data,'$.home',?) WHERE binding_key=?", (str(path) if isinstance(path, Path) else path, key))
    elif failure == "unreadable-home":
        monkeypatch.setattr(tool.os, "access", lambda *args: False)
    elif failure == "ambiguous":
        rig.conn.execute("INSERT INTO v2_account_bindings SELECT binding_key||'-duplicate',provider,alias,host,generation,pool_id,data,health,active,health_revision FROM v2_account_bindings WHERE binding_key=?", (key,))
    elif failure == "missing-db":
        (rig.root / "sessions.db").unlink()
    elif failure == "import":
        def unavailable(*args):
            raise ImportError("inert-private-sentinel")
        monkeypatch.setattr(tool, "_codex_default_home", unavailable)
    rig.conn.commit()
    if failure == "busy":
        blocker = sqlite3.connect(rig.root / "sessions.db")
        blocker.execute("BEGIN EXCLUSIVE")
    try:
        after = rig.tick()
    finally:
        if blocker:
            blocker.rollback()
            blocker.close()
    assert after["codex_lkg"] == before["codex_lkg"]
    assert after["observations"]["codex"]["observed_at"] == before["observations"]["codex"]["observed_at"]
    assert after["codex_health"]["probed_at"] == before["codex_health"]["probed_at"]
    assert after["codex_health"]["outcome"] == "provider_error"
    assert after["codex_health"]["error"]["message"] == "codex_default_home_unavailable"
    assert after["claude_health"]["outcome"] == "ok"
    assert len((rig.root / "codex.jsonl").read_text().splitlines()) == 1
    assert len((rig.root / "claude.log").read_text().splitlines()) == 2
    assert "inert-private-sentinel" not in rig.state.read_text()
    if failure == "missing-db":
        assert not (rig.root / "sessions.db").exists()


def test_skip_codex_never_resolves_and_keeps_dummy_no_update(rig, monkeypatch):
    def forbidden(*args):
        pytest.fail("skip-codex resolved a default")
    monkeypatch.setattr(tool, "_codex_default_home", forbidden)
    state = rig.tick(extra=("--skip-codex",))
    assert state["observations"]["codex"]["collection"]["status"] == "no_update"
    assert not (rig.root / "codex.jsonl").exists()


def test_readonly_connection_named_projection_timeout_and_host_resolution(rig, monkeypatch):
    # The narrow read must not decode or return unrelated payload fields.
    rig.conn.execute("UPDATE v2_account_bindings SET data=json_set(data,'$.token','inert-private-sentinel')")
    rig.conn.commit()
    original = sqlite3.connect
    calls, queries = [], []
    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        conn = original(*args, **kwargs)
        conn.set_trace_callback(queries.append)
        return conn
    monkeypatch.setattr(tool.sqlite3, "connect", connect)
    monkeypatch.delenv("PENTACLE_HOST_ID")
    monkeypatch.setenv("AGENT_ORCH_HOST_ID", "fixture-host")
    before = (rig.root / "sessions.db").read_bytes()
    assert tool._codex_default_home(rig.root / "sessions.db") == str(rig.homes[0])
    assert calls[0][0][0].endswith("?mode=ro")
    assert calls[0][1] == {"uri": True, "timeout": 1.0}
    assert queries[0] == "PRAGMA query_only=1"
    assert len(queries) == 2
    assert queries[1].startswith("SELECT d.alias, json_extract(b.data,'$.home'), json_extract(b.data,'$.enabled'), b.health ")
    assert "SELECT *" not in queries[1] and "secret" not in queries[1] and "token" not in queries[1]
    assert (rig.root / "sessions.db").read_bytes() == before


def test_unconfigured_first_tick_stays_unknown_without_fallback(rig):
    rig.conn.execute("DELETE FROM v2_account_defaults")
    rig.conn.commit()
    state = rig.tick()
    assert state["codex_lkg"]["pct"] is None
    assert state["codex_health"]["outcome"] == "provider_error"
    assert state["observations"]["codex"]["observed_at"] is None
    assert not (rig.root / "codex.jsonl").exists()
    assert state["claude_health"]["outcome"] == "ok"


def test_flag_absent_does_not_install_run_wrapper(rig, monkeypatch):
    captured = []
    class Collector:
        def __init__(self, **kwargs):
            captured.append(kwargs)
        def run_once(self):
            pass
    monkeypatch.setattr(tool, "UsageStateCollector", Collector)
    tool.main(["--state", str(rig.state), "--shared-scripts", str(rig.root / "scripts")])
    assert set(captured[0]) == {"state_path", "claude_command", "codex_command", "claude_config_path"}
