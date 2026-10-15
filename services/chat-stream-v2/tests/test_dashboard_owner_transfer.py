"""Catalog ownership is a credential/binding-authorized, audited transaction."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from assets import Assets
from _shared.assets_store import AssetStore
from _shared.operator_auth import ConnectionTrust
from server import Server
from store import Store, STREAM_TOKEN_HASH_VERSION
from sessions import Sessions

SPEC = "example__catalog"
OWNER, TARGET, FD, WORKER = (f"node-a:{name}" for name in ("old", "new", "fd", "worker"))
CATALOG = json.loads((Path(__file__).resolve().parents[3] / "test/fixtures/dashboard_catalog/catalog_cases.json").read_text())["valid"][0]["catalog"]
CATALOG = {**CATALOG, "boards": [b for b in CATALOG["boards"] if b["kind"] != "hosted-view"]}
CREDENTIAL = "00000000-0000-4000-8000-000000000001"
OPERATOR = {"operator_authenticated": True, "operator_trusted": True, "transport": "v2",
            "credential_id": CREDENTIAL, "operator_principal": f"operator:{CREDENTIAL}"}


class Fixture:
    def __init__(self, path):
        self.rows = {sid: {"host": "node-a", "status": "open", "role": "worker", "spec_ids": [],
                          "session_generation": "gen-" + sid.split(":")[1]} for sid in (OWNER, TARGET, FD, WORKER)}
        self.rows[OWNER]["status"] = "closed"
        self.binding = {"stream_id": FD, "generation": self.rows[FD]["session_generation"]}
        self.events = []
        self.assets = Assets(str(path), sessions=self, fleet_hosts={"node-a"}, catalog_spec_id=SPEC,
                             assistant_binding=self.bound, broadcast=self.broadcast)

    def get(self, sid):
        return self.rows.get(sid)

    async def bound(self):
        return self.binding

    async def broadcast(self, event):
        self.events.append(event)

    def auth(self, sid=FD):
        return {"token_verified": True, "stream_id": sid, "session_generation": self.rows[sid]["session_generation"]}

    def request(self, auth=None, **changes):
        return {"type": "dashboard.owner.transfer", "request_id": "transfer-1", "to_stream_id": TARGET,
                "target_generation": self.rows[TARGET]["session_generation"], "expected_owner": OWNER,
                "reason": "Recover orphaned catalog", "_auth_context": self.auth() if auth is None else auth, **changes}

    async def transfer(self, **changes):
        return await self.assets.wire_handlers()["dashboard.owner.transfer"](self.request(**changes))

    async def seed(self):
        self.before = await self.assets._call("publish_asset", host="node-a", session_name="old", stream_id=OWNER,
            asset_id="dashboard-catalog", spec_id=SPEC, content_type="dashboard-catalog", body=json.dumps(CATALOG),
            title="Catalog", tags=["history"], producer="node-a:original-producer")
        await self.assets._call("set_read", host="node-a", session_name="old", asset_id="dashboard-catalog", read=True)
        await self.assets._call("set_review_status", host="node-a", session_name="old", asset_id="dashboard-catalog", review_status="approved")
        self.comment = await self.assets._call("add_comment", host="node-a", session_name="old", asset_id="dashboard-catalog",
            section_id="catalog", block_id="board", body="Keep this history", author="operator", excerpt="Catalog board")
        self.before = await self.assets._call("get_asset", host="node-a", session_name="old", asset_id="dashboard-catalog")

    def snapshot(self):
        return {table: [tuple(r) for r in self.assets._store._conn.execute(f"SELECT * FROM {table}")]
                for table in ("assets", "asset_comments", "catalog_owner_transfers")}

    async def publish(self, sid):
        return await self.assets.asset({"type": "asset.publish", "request_id": "publish-after-transfer", "stream_id": sid,
            "asset_id": "dashboard-catalog", "spec_id": SPEC, "content_type": "dashboard-catalog",
            "title": "Updated catalog", "body": json.dumps({**CATALOG, "boards": []}), "_auth_context": self.auth(sid)})


def run(tmp_path, scenario):
    async def execute():
        f = Fixture(tmp_path / "assets.db")
        await f.assets.start()
        try:
            await f.seed()
            return await scenario(f)
        finally:
            await f.assets.stop()
    return asyncio.run(execute())


@pytest.mark.parametrize("operator", [False, True])
def test_transfer_preserves_history_then_new_owner_can_publish(tmp_path, operator):
    async def scenario(f):
        reply = await f.transfer(auth=OPERATOR if operator else f.auth())
        assert reply["type"] == "dashboard.owner.transfer.ok", reply
        assert reply["audit_id"] and not reply["unchanged"] and reply["broadcast_delivered"]
        after = await f.assets._call("get_asset", host="node-a", session_name="new", asset_id="dashboard-catalog")
        for key in f.before.keys() - {"host", "session_name", "stream_id", "updated_at"}:
            assert after[key] == f.before[key], key
        assert after["stream_id"] == TARGET
        comment = dict(f.assets._store._conn.execute("SELECT * FROM asset_comments").fetchone())
        assert comment["session_name"] == "new" and comment["comment_id"] == f.comment["comment_id"]
        for key in ("body", "excerpt", "author", "created_at", "updated_at", "resolved"):
            assert comment[key] == f.comment[key]
        audit = dict(f.assets._store._conn.execute("SELECT * FROM catalog_owner_transfers").fetchone())
        details = json.loads(audit["details"])
        assert audit["audit_id"] == reply["audit_id"] and audit["request_id"] == "transfer-1"
        assert details["old_owner"] == OWNER and details["new_owner"] == TARGET
        assert details["body_sha256"] == hashlib.sha256(f.before["body"].encode()).hexdigest()
        assert details["actor"]["identity"] == (OPERATOR["operator_principal"] if operator else FD)
        assert [e["type"] for e in f.events] == ["asset.removed", "asset.update"]
        assert f.events[0]["session_key"] != f.events[1]["session_key"]
        assert (await f.publish(TARGET))["type"] == "asset.publish.ok"
        for sid in (OWNER, WORKER):
            assert (await f.publish(sid))["error_code"] == "asset_unauthorized"
        await f.assets.stop()
        reopened = AssetStore(tmp_path / "assets.db")
        try:
            assert reopened.get_asset(host="node-a", session_name="new", asset_id="dashboard-catalog")["stream_id"] == TARGET
            assert reopened._conn.execute("SELECT count(*) FROM catalog_owner_transfers").fetchone()[0] == 1
        finally:
            reopened.close()
    run(tmp_path, scenario)


@pytest.mark.parametrize("case", ["ordinary", "role_only", "stale_binding", "stale_generation", "closed", "no_binding",
                                  "binding_error", "unverified", "service", "dot", "scoped", "elevated_worker",
                                  "operator_flag_only", "wrong_operator_principal", "operator_legacy"])
def test_unauthorized_actors_do_not_move_or_audit(tmp_path, case):
    async def scenario(f):
        auth = f.auth()
        if case in {"ordinary", "role_only", "elevated_worker"}:
            auth = f.auth(WORKER)
            if case == "role_only": f.rows[WORKER]["role"] = "assistant"
            if case == "elevated_worker": auth.update(operator_authenticated=True, operator_authority_source="stream_token")
        elif case == "stale_binding": f.binding["generation"] = "retired"
        elif case == "stale_generation": auth["session_generation"] = "retired"
        elif case == "closed": f.rows[FD]["status"] = "closed"
        elif case == "no_binding": f.assets._assistant_binding = None
        elif case == "binding_error":
            async def broken(): raise RuntimeError("unreadable binding")
            f.assets._assistant_binding = broken
        elif case == "unverified": auth["token_verified"] = False
        elif case in {"service", "dot", "scoped"}:
            auth = {**OPERATOR, {"service": "service_authenticated", "dot": "dot_principal", "scoped": "scoped_principal"}[case]: True}
        elif case == "operator_flag_only": auth = {"operator_authenticated": True}
        elif case == "wrong_operator_principal": auth = {**OPERATOR, "operator_principal": "agent:worker"}
        elif case == "operator_legacy": auth = {**OPERATOR, "transport": "legacy"}
        before = f.snapshot()
        assert (await f.transfer(auth=auth))["error_code"] == "dashboard_unauthorized"
        assert f.snapshot() == before and not f.events
    run(tmp_path, scenario)


@pytest.mark.parametrize("change", [{"status": "closed"}, {"host": "node-b"}, {"role": "external"}, {"role": "dot"},
                                   {"role": "report-producer"}, {"scope": "private"}, {"scoped_principal": True},
                                   {"dot_principal": True}, {"session_generation": "replaced"}, None])
def test_target_state_and_generation_are_required(tmp_path, change):
    async def scenario(f):
        request = f.request()
        if change is None: del f.rows[TARGET]
        else: f.rows[TARGET].update(change)
        before = f.snapshot()
        reply = await f.assets.transfer_catalog_owner(request)
        assert reply["error_code"] == "dashboard_unauthorized", reply
        assert f.snapshot() == before
    run(tmp_path, scenario)


@pytest.mark.parametrize("changed_seat", [FD, TARGET])
def test_state_is_rechecked_after_binding_await(tmp_path, changed_seat):
    async def scenario(f):
        async def close_during_lookup():
            f.rows[changed_seat]["status"] = "closed"
            return f.binding
        f.assets._assistant_binding = close_during_lookup
        before = f.snapshot()
        assert (await f.transfer())["error_code"] == "dashboard_unauthorized"
        assert f.snapshot() == before
    run(tmp_path, scenario)


@pytest.mark.parametrize("case,code", [("missing", "dashboard_catalog_missing"), ("duplicate", "dashboard_catalog_ambiguous"),
    ("wrong_type", "dashboard_invalid"), ("collision", "dashboard_owner_collision"), ("expected_owner", "dashboard_owner_mismatch")])
def test_row_preconditions_refuse_without_effect(tmp_path, case, code):
    async def scenario(f):
        conn = f.assets._store._conn
        changes = {}
        if case == "missing": conn.execute("DELETE FROM assets")
        elif case == "duplicate":
            conn.execute("INSERT INTO assets SELECT host,'duplicate','node-a:duplicate',asset_id,title,content_type,body,tags,producer,read_at,created_at,updated_at,review_status,spec_id FROM assets")
        elif case == "wrong_type": conn.execute("UPDATE assets SET content_type = 'report'")
        elif case == "collision":
            conn.execute("INSERT INTO assets SELECT host,'new','node-a:new',asset_id,title,content_type,body,tags,producer,read_at,created_at,updated_at,review_status,'other__spec' FROM assets")
        else: changes["expected_owner"] = "node-a:unrelated"
        conn.commit()
        before = f.snapshot()
        assert (await f.transfer(**changes))["error_code"] == code
        assert f.snapshot() == before
    run(tmp_path, scenario)


def test_noop_and_stale_retry_do_not_append_audit(tmp_path):
    async def scenario(f):
        first = await f.transfer()
        assert first["audit_id"]
        before = f.snapshot()
        assert (await f.transfer())["error_code"] == "dashboard_owner_mismatch"
        assert f.snapshot() == before
        noop = await f.transfer(expected_owner=TARGET)
        assert noop["unchanged"] and noop["audit_id"] is None
        assert f.snapshot() == before
    run(tmp_path, scenario)


@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_audit_or_commit_failure_rolls_back_every_change(tmp_path, failure):
    async def scenario(f):
        conn = f.assets._store._conn
        if failure == "audit":
            conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON catalog_owner_transfers BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
            conn.commit()
        else:
            conn.set_authorizer(lambda action, first, *rest: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT" else sqlite3.SQLITE_OK)
        before = f.snapshot()
        reply = await f.transfer()
        conn.set_authorizer(None)
        assert reply["error_code"] == "asset_store_error", reply
        assert f.snapshot() == before and not f.events
    run(tmp_path, scenario)


def test_broadcast_failure_returns_committed_receipt_and_attempts_both_events(tmp_path):
    async def scenario(f):
        async def broken(event):
            f.events.append(event)
            raise RuntimeError("delivery unavailable")
        f.assets._broadcast = broken
        reply = await f.transfer()
        assert reply["type"] == "dashboard.owner.transfer.ok" and reply["audit_id"]
        assert reply["broadcast_delivered"] is False and len(f.events) == 2
        assert len(f.snapshot()["catalog_owner_transfers"]) == 1
        assert (await f.assets._call("get_asset", host="node-a", session_name="new", asset_id="dashboard-catalog"))["stream_id"] == TARGET
    run(tmp_path, scenario)


@pytest.mark.parametrize("changes", [{"body": "replacement"}, {"spec_id": "other"}, {"asset_id": "other"},
    {"reason": " "}, {"reason": "x" * 501}, {"target_generation": ""}, {"to_stream_id": "bad"},
    {"expected_owner": " node-a:old"}, {"request_id": ""}])
def test_invalid_requests_preserve_catalog(tmp_path, changes):
    async def scenario(f):
        before = f.snapshot()
        assert (await f.transfer(**changes))["error_code"] == "dashboard_invalid"
        assert f.snapshot() == before
    run(tmp_path, scenario)


@pytest.mark.parametrize("actor,expected", [("fd", True), ("operator", True), ("operator_seat", True),
    ("worker", False), ("nonbound", False), ("stale_binding", False), ("spoof", False), ("scoped_operator", False)])
def test_real_dispatch_distinguishes_connection_operator_from_elevated_seat(tmp_path, actor, expected):
    async def scenario(f):
        sid = FD if actor in {"fd", "stale_binding"} else WORKER
        token = "synthetic-transfer-token"
        store = Store(str(tmp_path / "wire-sessions.db"))
        store.start()
        sessions = Sessions(store, local_host="node-a")
        for stream in (FD, TARGET, WORKER):
            await sessions.open("node-a", stream.split(":")[1], provider="codex", pane_status="pane_alive")
            f.rows[stream] = sessions.get(stream)
        f.binding = {"stream_id": FD, "generation": f.rows[FD]["session_generation"]}
        await store.grant_stream_token("node-a", sid.split(":")[1],
            hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
        server = Server(store=store, sessions=sessions, local_host="node-a", seat_operator_authority=True)
        server.handlers.update(f.assets.wire_handlers())
        class Peer:
            remote_address = ("192.0.2.10", 12345)
        peer = Peer()
        if actor in {"operator", "operator_seat", "scoped_operator"}:
            kwargs = {"scope": {"kind": "stream", "stream_id": "node-a:external"}} if actor == "scoped_operator" else {}
            server._connection_trust[peer] = ConnectionTrust(transport="v2", client_kind="pentacle", operator_trusted=True,
                credential_id=CREDENTIAL, **kwargs)
        if actor == "stale_binding": f.binding["generation"] = "retired"
        request = f.request()
        request.pop("_auth_context")
        if actor not in {"operator", "spoof", "scoped_operator"}:
            request.update(from_stream_id=sid, stream_token=token)
        if actor == "spoof": request["_auth_context"] = {**OPERATOR, **f.auth()}
        before = f.snapshot()
        try:
            reply = (await server._dispatch(json.dumps(request), websocket=peer))[0]
            assert (reply["type"] == "dashboard.owner.transfer.ok") is expected, reply
            if not expected:
                assert reply["error_code"] == ("authentication_required" if actor in {"spoof", "scoped_operator"}
                                                else "dashboard_unauthorized"), reply
                assert f.snapshot() == before
            elif actor == "fd":
                assert (await f.publish(TARGET))["type"] == "asset.publish.ok"
        finally:
            store.stop()
    run(tmp_path, scenario)


def test_additive_audit_table_preserves_existing_database(tmp_path):
    async def scenario(f):
        before = f.snapshot()
        await f.assets._run(lambda: f.assets._store._conn.execute("DROP TABLE catalog_owner_transfers"))
        await f.assets.stop()
        f.assets = Assets(str(tmp_path / "assets.db"), sessions=f, fleet_hosts={"node-a"},
                          catalog_spec_id=SPEC, assistant_binding=f.bound, broadcast=f.broadcast)
        await f.assets.start()
        assert f.snapshot() == before
        assert (await f.transfer())["audit_id"]
    run(tmp_path, scenario)
