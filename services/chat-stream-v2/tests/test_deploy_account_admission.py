"""Deploy admission uses durable recovery meaning, not historical row states."""
import json
import sqlite3
import time

import pytest

from account_store import AccountStore
from account_repin_state import EFFECT, causes, generation_intents, resolved
from deploy.coordinated import Invalid
from deploy.coordinated_runtime import Runtime
from provider_cli_fixtures import release_pair
from provider_cli_pin import digest
from provider_cli_maintenance import PREFIX, scope
from store import SPAWN_ADMISSION_HOLD_DDL
from test_account_rotation import binding


@pytest.fixture
def history(tmp_path):
    database = tmp_path / "accounts.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    ledger = AccountStore(conn, {"enabled": True})
    conn.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute(SPAWN_ADMISSION_HOLD_DDL)
    pin = release_pair()
    conn.executemany("INSERT INTO kv VALUES (?,?)", [
        ("provider_cli.release_pin", json.dumps(pin)), ("provider_cli.pin_initialized", "1")])

    def save(key, state, data):
        conn.execute("INSERT OR REPLACE INTO v2_account_recovery VALUES (?,?,?)", (key, state, json.dumps(data)))

    save("provider-cli-pin:fixture", "done", {"operation_id": "fixture", "pin": pin, "digest": digest(pin)})
    conn.commit()
    for host in ("host-a", "host-b", "host-c"):
        for provider in ("codex", "claude"):
            for alias in ("primary", "spare"):
                ledger.register(binding(alias, host, provider))
            ledger.set_default(host=host, provider=provider, alias="primary")
    # Production refusal shape: ten cleared maintenance receipts, two historical
    # ready receipts without a live slot, and four resolved parked repins.
    for i in range(12):
        save(f"provider-cli-maintenance:host-a:old-{i}", "cleared" if i < 10 else "ready",
             {"state": "cleared" if i < 10 else "ready", "arguments": {"maintenance_id": f"old-{i}"}})
    parked = []
    for i in range(4):
        arguments = {"host": "host-a", "provider": "codex", "account": "primary", "operation_id": f"old-{i}"}
        key = f"repin:host-a:codex:primary:old-{i}"
        save(key, "parked", {"arguments": arguments, "status": "parked", "park_reason": "provider_timeout"})
        save(EFFECT + key, "done", {"disposition": "sealed_non_split", "arguments_digest": digest(arguments),
            "host_seal": {"state": "sealed", "worker_quiescent": True,
                          "arguments_digest": digest(arguments), "successor_absent": True}})
        parked.append(key)
    conn.commit()
    runtime = Runtime.__new__(Runtime)
    yield runtime, ledger, database, save, parked
    conn.close()


def test_snapshot_accepts_actual_16_row_history_without_rewriting_it(history):
    runtime, ledger, database, _, parked = history
    assert ledger.conn.execute("SELECT count(*) FROM v2_account_recovery WHERE state NOT IN ('done','idle')").fetchone()[0] == 16
    assert all(resolved(ledger, key) for key in parked)
    cell = {"host": "host-a", "provider": "codex", "account": "primary"}
    assert causes(ledger, cell) == [] and generation_intents(ledger, cell) == {}
    before = list(ledger.conn.iterdump())
    snapshot = runtime.account_snapshot(ledger.policy, database)
    assert len(snapshot["bindings"]) == 12 and len(snapshot["defaults"]) == 6
    assert before == list(ledger.conn.iterdump())


@pytest.mark.parametrize("live", ["unresolved-repin", "generation-intent", "unregistered-intent",
                                  "ready-slot", "settling-slot", "corrupt-slot", "hold"])
def test_snapshot_still_refuses_one_live_operation(history, live):
    runtime, ledger, database, save, parked = history
    if live == "unresolved-repin":
        ledger.conn.execute("DELETE FROM v2_account_recovery WHERE binding_key=?", (EFFECT + parked[0],))
    elif live in {"generation-intent", "unregistered-intent"}:
        account = "primary" if live == "generation-intent" else "not-enrolled"
        arguments = {"host": "host-a", "provider": "codex", "account": account, "operation_id": "live"}
        save(f"account-mutation:host-a:codex:{account}:live", "pending", {"arguments": arguments, "status": "pending"})
    elif live.endswith("slot"):
        account = binding()
        value = {"binding": account.public(), "scope": scope(account), "arguments": {"maintenance_id": "live"},
                 "sessions": [], "hold": {}, "state": "settling" if live == "settling-slot" else "ready"}
        if live == "corrupt-slot":
            value["scope"] = "invalid"
        ledger.conn.execute("INSERT INTO kv VALUES (?,?)", (PREFIX + account.host, json.dumps(value)))
    else:
        ledger.conn.execute("INSERT INTO v2_spawn_admission_hold VALUES (?,?,?,?)", ("host-a", time.time() + 600, "live", time.time()))
    ledger.conn.commit()
    before = list(ledger.conn.iterdump())
    with pytest.raises(Invalid, match="account|maintenance"):
        runtime.account_snapshot(ledger.policy, database)
    assert before == list(ledger.conn.iterdump())


def test_snapshot_allows_expired_hold(history):
    runtime, ledger, database, _, _ = history
    ledger.conn.execute("INSERT INTO v2_spawn_admission_hold VALUES (?,?,?,?)", ("host-a", time.time() - 1, "expired", 1))
    ledger.conn.commit()
    assert runtime.account_snapshot(ledger.policy, database)["pin"] == release_pair()
