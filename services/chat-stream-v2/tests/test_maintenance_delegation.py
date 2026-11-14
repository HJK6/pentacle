"""Delegated holds use authenticated seats and durable, synthetic grants only."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import os

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from store import STREAM_TOKEN_HASH_VERSION
from test_loopback_privileged_rpc import rpc
from test_provider_cli_maintenance import ready_daemon

ACTOR = "fixture-fd:primary"
GENERATION = "fd-generation"
ADVISOR = "fixture-advisor:advisor"
ADVISOR_GENERATION = "advisor-generation"
COMPOSITE = "fixture-chat:assistant"
DISPATCH = "fixture-operator-dispatch"
TELL = "fixture-advisor-ruling"
TOKEN = "synthetic-fd-token"


async def mutate(store, action):
    from account_store import transaction
    def commit(conn):
        with transaction(conn):
            return action(conn)
    return await store.submit(commit)


@asynccontextmanager
async def delegated_daemon(tmp_path, monkeypatch):
    async with ready_daemon(tmp_path, monkeypatch) as fixture:
        daemon, store, runtime, host, selected, row, request = fixture
        from uiverbs import UIVerbs
        daemon.handlers.update(UIVerbs(store, runtime.sessions, daemon.spawnctl).wire_handlers())
        await store.open_session("fixture-fd", "primary", session_generation=GENERATION, provider="codex")
        await store.grant_stream_token("fixture-fd", "primary", hashlib.sha256(TOKEN.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
        await store.open_session("fixture-advisor", "advisor", session_generation=ADVISOR_GENERATION,
                                 provider="codex", pane_status="pane_alive")
        config = AssistantCompositeConfig.from_env({
            "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": COMPOSITE,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": ACTOR,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": GENERATION,
            "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": ADVISOR,
        })
        daemon.assistant_composite = AssistantComposite(store, config=config)
        await mutate(store, lambda conn: conn.execute(
            "INSERT INTO v2_assistant_composite_routes "
            "(route_id,stream_id,input_identity,payload_digest,input_request_id,event_id,body,actor_stream_id,"
            "routing_state,delivery_state,dispatch_id,route_target,route_target_generation,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("fixture-route", COMPOSITE, "fixture-input", "fixture-digest", "fixture-request", 42,
             "Delegate one repair hold", "operator:fixture-credential", "resolved", "landed", DISPATCH,
             ACTOR, GENERATION, "2026-11-14T00:00:00Z", "2026-11-14T00:00:00Z")))
        line = (f"DELEGATED-HOLD v1 dispatch={DISPATCH} actor={ACTOR}/{GENERATION} "
                f"host={selected.host} maintenance={request['maintenance_id']} ttl_max_s=900")
        await store.put_tell_delivery(TELL, {"payload_digest": "fixture-ruling-digest", "reply": {"type": "tell.ok"},
            "delivery": {"from_stream_id": ADVISOR, "to_stream_id": ACTOR, "text": line,
                         "sender_verified": True, "sender_kind": "peer", "sender_generation": ADVISOR_GENERATION}})
        request = {k: v for k, v in request.items() if k != "local_admin_token"}
        request.update(from_stream_id=ACTOR, stream_token=TOKEN,
                       delegation={"source_dispatch_id": DISPATCH, "ruling_tell_id": TELL})
        yield daemon, store, runtime, selected, request


def test_fd_delegated_freeze_records_grant_and_actual_actor(tmp_path, monkeypatch):
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            result = await rpc(daemon, "spawn_freeze", **request)
            assert result.get("error_code") is None, result.get("error_code")
            assert result["type"] == "spawn_freeze.ok"
            owner = result["receipt"]["owner"]
            assert owner["stream_id"] == ACTOR
            assert owner["session_generation"] == GENERATION
            assert not owner["operator_authenticated"] and not owner["local_admin_verified"]
            grant = owner["delegation"]
            assert grant["source_dispatch_id"] == DISPATCH
            assert grant["source_event_id"] == 42
            assert grant["source_operator_principal"] == "operator:fixture-credential"
            assert grant["ruling_tell_id"] == TELL
            assert grant["ruling_sender_stream_id"] == ADVISOR
            assert grant["ruling_sender_generation"] == ADVISOR_GENERATION
            assert grant["advisor_binding"] == {"stream_id": ADVISOR, "generation": ADVISOR_GENERATION, "source": "env"}
            assert grant["scope"] == {"dispatch": DISPATCH, "actor": ACTOR + "/" + GENERATION,
                                      "host": selected.host, "maintenance": request["maintenance_id"], "ttl_max_s": 900}
            from provider_cli_pin import digest
            assert grant["scope_digest"] == digest(grant["scope"])
            assert grant["verified_at"] == runtime.clock()
            assert (await rpc(daemon, "spawn_freeze", **request))["receipt"] == result["receipt"]
            assert "PENTACLE_SEAT_OPERATOR_AUTHORITY" not in os.environ
    asyncio.run(run())


# These snapshots deliberately include every hold, receipt and reservation, so a
# refusal cannot hide a partial write behind an error response.
async def writes(store):
    return await store.submit(lambda conn: {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("v2_spawn_admission_hold", "v2_maintenance_delegations", "v2_account_recovery", "kv")})


async def change_tell(store, change):
    import json
    def update(conn):
        row = conn.execute("SELECT reply FROM v2_tell_deliveries WHERE tell_id=?", (TELL,)).fetchone()
        envelope = json.loads(row[0])
        change(envelope)
        conn.execute("UPDATE v2_tell_deliveries SET reply=? WHERE tell_id=?", (json.dumps(envelope), TELL))
    await mutate(store, update)


async def authority(store, value):
    await mutate(store, lambda conn: conn.execute(
        "INSERT OR REPLACE INTO kv(k,v) VALUES ('assistant.authority.stream_id',?)", (value,)))


async def rotate_advisor(store):
    await store.open_session("fixture-advisor", "replacement", session_generation="replacement-generation",
                             provider="codex", pane_status="pane_alive")
    await authority(store, "fixture-advisor:replacement")


def clear(request, result):
    return {key: request[key] for key in ("host", "maintenance_id", "delegation", "from_stream_id", "stream_token")} | {
        "expected_maintenance_digest": result["maintenance_digest"], "expected_hold_digest": result["hold_digest"]}


@pytest.mark.parametrize("case,reason", [
    ("missing_dispatch", "dispatch"),
    ("agent_origin", "operator_origin"), ("seat_origin", "operator_origin"), ("empty_operator", "operator_origin"),
    ("composite", "composite"), ("route_target", "actor"), ("route_generation", "actor"),
    ("undelivered", "dispatch_not_landed"),
    ("non_advisor", "advisor_rotated"), ("advisor_generation", "advisor_rotated"), ("advisor_rotated", "advisor_rotated"),
    ("same_primary", "advisor_binding"), ("unconfigured", "advisor_binding"), ("disabled", "advisor_binding"),
    ("unavailable", "advisor_binding"), ("recipient", "ruling_recipient"),
    ("missing_tell", "ruling_missing"), ("legacy_envelope", "ruling_missing"),
    ("forged_sender", "ruling_provenance"), ("wrong_kind", "ruling_provenance"),
    ("legacy_provenance", "ruling_provenance"), ("missing_sender_generation", "ruling_provenance"),
    ("unknown_kind", "ruling_provenance"), ("non_boolean_verified", "ruling_provenance"),
    ("scope_missing", "scope_line"), ("scope_duplicate", "scope_line"), ("scope_malformed", "scope_line"),
    ("scope_indented", "scope_line"),
    ("scope_dispatch", "scope_dispatch"), ("scope_actor", "scope_actor"), ("scope_generation", "scope_actor"),
    ("scope_host", "scope_host"), ("scope_maintenance", "scope_maintenance"),
    ("scope_ttl_max", "ttl"), ("ttl_901", "ttl"), ("ttl_exceeds_scope", "ttl"),
    ("pair_missing_key", "delegation"), ("pair_extra_key", "delegation"), ("pair_empty", "delegation"),
])
def test_acquire_refuses_each_invariant_without_writes(tmp_path, monkeypatch, case, reason):
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            route_changes = {
                "agent_origin": ("actor_stream_id", "agent:fixture"), "seat_origin": ("actor_stream_id", ACTOR),
                "empty_operator": ("actor_stream_id", "operator:"), "composite": ("stream_id", "other:assistant"),
                "route_target": ("route_target", "fixture-fd:other"), "route_generation": ("route_target_generation", "retired"),
                "undelivered": ("delivery_state", "committed_pending"),
            }
            tell_changes = {
                "non_advisor": {"from_stream_id": "fixture:impostor"}, "advisor_generation": {"sender_generation": "old-advisor"},
                "recipient": {"to_stream_id": "fixture-fd:other"}, "forged_sender": {"sender_verified": False},
                "wrong_kind": {"sender_kind": "operator"}, "missing_sender_generation": {"sender_generation": ""},
                "unknown_kind": {"sender_kind": "unknown"}, "non_boolean_verified": {"sender_verified": 1},
            }
            replacements = {
                "scope_dispatch": (f"dispatch={DISPATCH}", "dispatch=other-dispatch"),
                "scope_actor": (f"actor={ACTOR}/", "actor=fixture-fd:other/"),
                "scope_generation": (f"/{GENERATION} ", "/retired "),
                "scope_host": (f"host={selected.host}", "host=other-host"),
                "scope_maintenance": (f"maintenance={request['maintenance_id']}", "maintenance=other-maintenance"),
                "scope_ttl_max": ("ttl_max_s=900", "ttl_max_s=901"),
                "ttl_exceeds_scope": ("ttl_max_s=900", "ttl_max_s=30"),
            }
            if case in route_changes:
                column, value = route_changes[case]
                await mutate(store, lambda conn: conn.execute(f"UPDATE v2_assistant_composite_routes SET {column}=?", (value,)))
            elif case in tell_changes:
                await change_tell(store, lambda e: e["delivery"].update(tell_changes[case]))
            elif case in replacements:
                old, new = replacements[case]
                await change_tell(store, lambda e: e["delivery"].update(text=e["delivery"]["text"].replace(old, new)))
            elif case == "missing_dispatch":
                await mutate(store, lambda conn: conn.execute("DELETE FROM v2_assistant_composite_routes"))
            elif case == "missing_tell":
                await mutate(store, lambda conn: conn.execute("DELETE FROM v2_tell_deliveries"))
            elif case == "legacy_envelope":
                await change_tell(store, lambda e: e.pop("delivery"))
            elif case == "legacy_provenance":
                await change_tell(store, lambda e: [e["delivery"].pop(k) for k in ("sender_verified", "sender_kind", "sender_generation")])
            elif case == "advisor_rotated":
                await rotate_advisor(store)
            elif case in {"same_primary", "unconfigured", "disabled"}:
                await authority(store, {"same_primary": ACTOR, "unconfigured": "", "disabled": "disabled"}[case])
            elif case == "unavailable":
                await mutate(store, lambda conn: conn.execute("UPDATE sessions SET pane_status='pane_gone' WHERE host='fixture-advisor'"))
            elif case.startswith("scope_"):
                def change(e):
                    line = e["delivery"]["text"]
                    e["delivery"]["text"] = {"scope_missing": "Prose is not a grant", "scope_duplicate": line + "\n" + line,
                        "scope_malformed": line.replace("v1", "v2"), "scope_indented": " " + line}[case]
                await change_tell(store, change)
            elif case == "ttl_901":
                request["ttl_s"] = 901
            elif case == "pair_missing_key":
                request["delegation"].pop("ruling_tell_id")
            elif case == "pair_extra_key":
                request["delegation"]["scope"] = "claimed"
            elif case == "pair_empty":
                request["delegation"]["ruling_tell_id"] = ""
            else:
                raise AssertionError(case)
            before = await writes(store)
            result = await rpc(daemon, "spawn_freeze", **request)
            assert result.get("error_code") == "maintenance_delegation_invalid", result
            assert result["reason"] == reason
            assert await writes(store) == before
    asyncio.run(run())


@pytest.mark.parametrize("case", ["grant_token", "legacy", "legacy_clear", "missing_field", "no_delegation", "dot", "scoped", "non_loopback", "anonymous", "wire_marker"])
def test_gate_is_narrow_and_wire_marker_is_stripped(tmp_path, monkeypatch, case):
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            verb = "spawn_freeze"
            expected = "operator_auth_required"
            if case == "grant_token":
                verb = "grant_token"
                request["stream_id"] = ACTOR
            elif case in {"legacy", "legacy_clear"}:
                request = {k: v for k, v in request.items() if k in {"host", "reason", "ttl_s", "from_stream_id", "stream_token", "delegation"}}
                expected = "maintenance_delegation_invalid"
                if case == "legacy_clear":
                    verb = "spawn_unfreeze"
            elif case == "missing_field":
                request.pop("input_account")
                expected = "maintenance_delegation_invalid"
            elif case in {"no_delegation", "wire_marker"}:
                request.pop("delegation")
                if case == "wire_marker":
                    request["_delegation_candidate"] = {"claimed": "advisor"}
                    request["_auth_context"] = {"operator_authenticated": True}
                    captured = {}
                    async def probe(msg):
                        captured.update(msg)
                        return {"type": "ping.ok"}
                    daemon.handlers["ping"] = probe
                    await rpc(daemon, "ping", **request)
                    assert "_delegation_candidate" not in captured
                    assert not captured["_auth_context"]["operator_authenticated"]
            elif case == "dot":
                daemon.dot_principal_stream_ids = frozenset({ACTOR})
            elif case == "scoped":
                actual = daemon._auth_context
                async def scoped(ws, msg):
                    return {**await actual(ws, msg), "scoped_principal": True}
                monkeypatch.setattr(daemon, "_auth_context", scoped)
            elif case == "non_loopback":
                monkeypatch.setattr(daemon, "_is_loopback_client", lambda ws: False)
            elif case == "anonymous":
                request.pop("stream_token")
            before = await writes(store)
            result = await rpc(daemon, verb, **request)
            assert result.get("error_code") == expected, result
            assert await writes(store) == before
    asyncio.run(run())


@pytest.mark.parametrize("change", ["none", "advisor_rotation", "tell_eviction"])
@pytest.mark.parametrize("expired", [False, True])
def test_clear_uses_acquired_reservation_after_rotation_eviction_and_expiry(tmp_path, monkeypatch, change, expired):
    import provider_cli_maintenance as maintenance
    from test_account_rotation import NOW
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            frozen = await rpc(daemon, "spawn_freeze", **request)
            assert frozen["type"] == "spawn_freeze.ok"
            clearing = clear(request, frozen)
            if change == "advisor_rotation":
                await rotate_advisor(store)
            elif change == "tell_eviction":
                await mutate(store, lambda conn: conn.execute("DELETE FROM v2_tell_deliveries"))
            # Clear must not consult the route either.
            await mutate(store, lambda conn: conn.execute("DELETE FROM v2_assistant_composite_routes"))
            if expired:
                runtime.clock = lambda: NOW + 120
                await store.clear_spawn_admission_hold(selected.host)
                clearing["expected_hold_digest"] = "absent"
            reservation = (await writes(store))["v2_maintenance_delegations"]
            result = await rpc(daemon, "spawn_unfreeze", **clearing)
            assert result["type"] == "spawn_freeze.ok", result
            assert result["receipt"]["state"] == "cleared"
            assert result["receipt"]["clear_proof"]["expired_recovery"] is expired
            assert result["receipt"]["owner"] == frozen["receipt"]["owner"]
            assert await runtime.run(lambda a: maintenance.slot(a, selected.host)) is None
            assert await store.get_spawn_admission_hold(selected.host) is None
            assert (await writes(store))["v2_maintenance_delegations"] == reservation
            assert (await rpc(daemon, "spawn_unfreeze", **clearing))["receipt"] == result["receipt"]
    asyncio.run(run())


@pytest.mark.parametrize("case", ["stream", "generation", "dispatch", "ruling", "maintenance", "host"])
def test_clear_wrong_owner_or_pair_refuses_without_writes(tmp_path, monkeypatch, case):
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            frozen = await rpc(daemon, "spawn_freeze", **request)
            clearing = clear(request, frozen)
            if case == "stream":
                await store.open_session("fixture-fd", "other", session_generation=GENERATION, provider="codex")
                token = "synthetic-other-seat-token"
                await store.grant_stream_token("fixture-fd", "other", hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
                clearing.update(from_stream_id="fixture-fd:other", stream_token=token)
            elif case == "generation":
                await mutate(store, lambda conn: conn.execute("UPDATE v2_session_generations SET generation='rotated' WHERE host='fixture-fd'"))
            elif case in {"dispatch", "ruling"}:
                clearing["delegation"] = {**request["delegation"], {"dispatch": "source_dispatch_id", "ruling": "ruling_tell_id"}[case]: "other"}
            elif case == "maintenance":
                clearing["maintenance_id"] = "foreign-maintenance"
            elif case == "host":
                clearing["host"] = "other-host"
            before = await writes(store)
            result = await rpc(daemon, "spawn_unfreeze", **clearing)
            assert result.get("error_code") == "maintenance_delegation_invalid", result
            assert await writes(store) == before
    asyncio.run(run())


@pytest.mark.parametrize("terminal", ["ready", "cleared", "expired"])
@pytest.mark.parametrize("reuse", ["maintenance", "dispatch", "ruling"])
def test_each_grant_key_is_consumed_forever(tmp_path, monkeypatch, terminal, reuse):
    from test_account_rotation import NOW
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            frozen = await rpc(daemon, "spawn_freeze", **request)
            if terminal == "cleared":
                result = await rpc(daemon, "spawn_unfreeze", **clear(request, frozen))
                assert result["receipt"]["state"] == "cleared"
            elif terminal == "expired":
                runtime.clock = lambda: NOW + 120
                await store.clear_spawn_admission_hold(selected.host)
            before = await writes(store)
            repeat = await rpc(daemon, "spawn_freeze", **request)
            assert repeat["receipt"] == (result["receipt"] if terminal == "cleared" else frozen["receipt"])
            assert await writes(store) == before  # no renewal, including after expiry
            changed = {**request, "maintenance_id": "second-maintenance"}
            if reuse in {"dispatch", "ruling"}:
                changed["delegation"] = {**request["delegation"], {"dispatch": "source_dispatch_id", "ruling": "ruling_tell_id"}[reuse]: "new-id"}
            result = await rpc(daemon, "spawn_freeze", **changed)
            assert result.get("error_code") == "maintenance_delegation_consumed", result
            assert await writes(store) == before
    asyncio.run(run())


def test_delegated_clear_preserves_foreign_hold(tmp_path, monkeypatch):
    from test_account_rotation import NOW
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            frozen = await rpc(daemon, "spawn_freeze", **request)
            runtime.clock = lambda: NOW + 120
            await store.set_spawn_admission_hold(selected.host, held_until=NOW + 1000, reason="foreign-owner")
            held = await store.get_spawn_admission_hold(selected.host)
            clearing = clear(request, frozen)
            from provider_cli_maintenance import hold_digest
            clearing["expected_hold_digest"] = hold_digest(held)
            result = await rpc(daemon, "spawn_unfreeze", **clearing)
            assert result["receipt"]["clear_proof"]["foreign_hold_preserved"]
            assert await store.get_spawn_admission_hold(selected.host) == held
            before = await writes(store)
            clearing["maintenance_id"] = "foreign-owner"
            result = await rpc(daemon, "spawn_unfreeze", **clearing)
            assert result.get("error_code") == "maintenance_delegation_invalid"
            assert await writes(store) == before
    asyncio.run(run())


def test_reservation_and_hold_rollback_together(tmp_path, monkeypatch):
    import provider_cli_maintenance as maintenance
    from provider_accounts import AccountError
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            before = await writes(store)
            original = maintenance._write_receipt
            def fail(a, *args):
                assert a.conn.execute("SELECT COUNT(*) FROM v2_maintenance_delegations").fetchone()[0] == 1
                assert a.conn.execute("SELECT COUNT(*) FROM v2_spawn_admission_hold").fetchone()[0] == 1
                raise AccountError("injected_after_reservation")
            monkeypatch.setattr(maintenance, "_write_receipt", fail)
            result = await rpc(daemon, "spawn_freeze", **request)
            assert result.get("error_code") == "injected_after_reservation"
            assert await writes(store) == before
            monkeypatch.setattr(maintenance, "_write_receipt", original)
            assert (await rpc(daemon, "spawn_freeze", **request))["type"] == "spawn_freeze.ok"
    asyncio.run(run())


def test_reservation_has_independent_database_uniqueness(tmp_path, monkeypatch):
    import sqlite3
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            assert (await rpc(daemon, "spawn_freeze", **request))["type"] == "spawn_freeze.ok"
            before = await writes(store)
            for ruling, dispatch in ((TELL, "another-dispatch"), ("another-ruling", DISPATCH)):
                with pytest.raises(sqlite3.IntegrityError):
                    await mutate(store, lambda conn: conn.execute(
                        "INSERT INTO v2_maintenance_delegations VALUES (?,?,?,?,?,?)",
                        (ruling, dispatch, "another-host", "another-maintenance", ACTOR, GENERATION)))
                assert await writes(store) == before
    asyncio.run(run())


def test_reservation_survives_store_reopen(tmp_path, monkeypatch):
    from store import Store
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            frozen = await rpc(daemon, "spawn_freeze", **request)
            before = await writes(store)
            await daemon.close()
            store.stop()
            reopened = Store(str(tmp_path / "ready-state.db"))
            reopened.start()
            try:
                assert await writes(reopened) == before
                from maintenance_delegation import verify_clear
                auth = {"token_verified": True, "peer_loopback": True, "stream_id": ACTOR, "session_generation": GENERATION}
                reservation = await reopened.submit(lambda conn: verify_clear(conn, clear(request, frozen), auth))
                assert reservation["maintenance_id"] == request["maintenance_id"]
                from account_store import transaction
                from maintenance_delegation import verify_acquire
                from provider_accounts import AccountError
                def consumed(conn):
                    with transaction(conn):
                        return verify_acquire(conn, {**request, "maintenance_id": "new", "_delegation_candidate": daemon.lane_rulings}, auth, runtime.clock())
                with pytest.raises(AccountError, match="^maintenance_delegation_consumed$"):
                    await reopened.submit(consumed)
                assert await writes(reopened) == before
            finally:
                reopened.stop()
    asyncio.run(run())


def test_competing_connections_reserve_exactly_one_hold(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    import threading
    from account_store import AccountStore
    import provider_cli_maintenance as maintenance
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            auth = {"token_verified": True, "peer_loopback": True, "stream_id": ACTOR, "session_generation": GENERATION}
            message = {**request, "_auth_context": auth, "_delegation_candidate": daemon.lane_rulings}
            args = maintenance._arguments(message, selected.host)
            barrier = threading.Barrier(2)
            def acquire():
                conn = sqlite3.connect(str(tmp_path / "ready-state.db"), isolation_level=None, timeout=5)
                conn.row_factory = sqlite3.Row
                try:
                    a = AccountStore(conn, runtime.policy, initialize=False)
                    barrier.wait(timeout=3)
                    return maintenance._begin(a, args, selected, runtime.clock(), auth, message)
                finally:
                    conn.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = [pool.submit(acquire) for _ in range(2)]
                assert first.result(timeout=5) == second.result(timeout=5)
            snapshot = await writes(store)
            assert len(snapshot["v2_maintenance_delegations"]) == 1
            assert len(snapshot["v2_spawn_admission_hold"]) == 1
            assert len([r for r in snapshot["v2_account_recovery"] if r[0].startswith(maintenance.RECEIPT)]) == 1
            assert len([r for r in snapshot["kv"] if r[0].startswith(maintenance.PREFIX)]) == 1
    asyncio.run(run())


def test_delegation_cannot_adopt_an_existing_operator_receipt(tmp_path, monkeypatch):
    import provider_cli_maintenance as maintenance
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            operator_request = {k: v for k, v in request.items() if k not in {"delegation", "from_stream_id", "stream_token"}}
            operator_request["_auth_context"] = {"operator_authenticated": True}
            await maintenance.freeze(runtime, operator_request, selected.host)
            before = await writes(store)
            result = await rpc(daemon, "spawn_freeze", **request)
            assert result.get("error_code") == "maintenance_delegation_invalid", result
            assert result["reason"] == "receipt_owner"
            assert await writes(store) == before
    asyncio.run(run())


def test_delegated_retry_replays_receipt_after_account_generation_changes(tmp_path, monkeypatch):
    from dataclasses import asdict, replace
    import json
    async def run():
        async with delegated_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, selected, request):
            frozen = await rpc(daemon, "spawn_freeze", **request)
            changed = replace(selected, generation="new-login-generation")
            await mutate(store, lambda conn: conn.execute(
                "UPDATE v2_account_bindings SET generation=?,data=? WHERE binding_key=?",
                (changed.generation, json.dumps(asdict(changed)), selected.key)))
            before = await writes(store)
            repeated = await rpc(daemon, "spawn_freeze", **request)
            assert repeated.get("error_code") is None, repeated
            assert repeated["receipt"] == frozen["receipt"]
            assert await writes(store) == before
    asyncio.run(run())
