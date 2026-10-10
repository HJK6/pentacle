"""Asset publication admission follows the resolved stored catalog row."""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from assets import Assets
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION


CATALOG = json.loads((Path(__file__).resolve().parents[3] /
    "test/fixtures/dashboard_catalog/catalog_cases.json").read_text())["valid"][0]["catalog"]
REPORT = json.dumps({"schema_version": 1, "title": "Example", "sections": [
    {"id": "s1", "title": "S", "status": "reference", "blocks": []}]})


def run_catalog_wire(tmp_path, scenario, catalog_spec="example__catalog"):
    class Peer:
        remote_address = ("127.0.0.1", 12345)

    async def main():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        sessions = Sessions(store, local_host="node-alpha")
        for name in ("owner", "other"):
            await sessions.open("node-alpha", name, provider="codex", pane_status="pane_alive")
            await store.grant_stream_token("node-alpha", name,
                hashlib.sha256(("synthetic-" + name).encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
        assets = Assets(str(tmp_path / "assets.db"), sessions=sessions, fleet_hosts={"node-alpha", "node-beta"}, catalog_spec_id=catalog_spec)
        await assets.start()
        server = Server(store=store, sessions=sessions, local_host="node-alpha")
        server.handlers.update(assets.wire_handlers())

        async def dispatch(message, seat=None):
            message = dict(message)
            if seat:
                sid = seat if ":" in seat else "node-alpha:" + seat
                message.update(from_stream_id=sid, stream_token="synthetic-" + sid.split(":", 1)[1])
            return (await server._dispatch(json.dumps(message), websocket=Peer()))[0]

        try:
            before = await assets._call("publish_asset", host="node-alpha", session_name="owner",
                stream_id="node-alpha:owner", asset_id="dashboard-catalog", spec_id=catalog_spec,
                title="Catalog", content_type="dashboard-catalog", body=json.dumps(CATALOG))
            await scenario(assets, server, dispatch, before)
        finally:
            await assets.stop()
            store.stop()
    with patch.dict("os.environ", {"PENTACLE_HOSTED_DASHBOARD_TAILNET_SUFFIX":"example.ts.net",
                                  "PENTACLE_HOSTED_DASHBOARD_PENTACLE_ORIGIN":"https://pentacle.example.ts.net"}):
        asyncio.run(main())


def publish(**fields):
    return {"type": "asset.publish", "host": "node-alpha", "session_name": "owner",
        "stream_id": "node-alpha:" + fields.get("session_name", "owner"),
        "asset_id": "dashboard-catalog", "spec_id": "example__catalog", "title": "Catalog",
        "content_type": "dashboard-catalog", "body": json.dumps(CATALOG), **fields}


@pytest.mark.parametrize("seat", [None, "other"])
@pytest.mark.parametrize("spec,session", [("example__other", "owner"), ("example__catalog", "other")])
def test_catalog_publish_admission_uses_resolved_row(tmp_path, seat, spec, session):
    async def scenario(assets, server, dispatch, before):
        reply = await dispatch(publish(spec_id=spec, session_name=session, content_type="report", body=REPORT,
            _auth_context={"operator_authenticated": True, "token_verified": True, "stream_id": "node-alpha:owner"}), seat)
        assert reply["type"] == "asset.error", reply
        assert reply["error_code"] == "asset_unauthorized", reply
        assert await assets._call("find_assets_by_id", asset_id="dashboard-catalog") == [before]
    run_catalog_wire(tmp_path, scenario)


@pytest.mark.parametrize("field", ["host", "session_name", "spec_id", "asset_id", "asset_id_arg"])
def test_publish_rejects_identity_type_coercion_without_creation(tmp_path, field):
    async def scenario(assets, server, dispatch, before):
        message = publish(asset_id="new-report", spec_id="example__new", content_type="report", body=REPORT)
        message[field] = 7
        if field in {"host", "session_name"}:
            message.pop("stream_id")
        elif field == "asset_id_arg":
            message.pop("asset_id")
        snapshot = await assets._run(lambda: (tuple(assets._store._conn.iterdump()),
                                               assets._store._conn.total_changes))
        reply = await dispatch(message, "owner")
        assert reply["error_code"] == "asset_invalid", reply
        assert await assets._run(lambda: (tuple(assets._store._conn.iterdump()),
                                         assets._store._conn.total_changes)) == snapshot
    run_catalog_wire(tmp_path, scenario)


@pytest.mark.parametrize("spec", ["example__catalog", "example__other"])
def test_catalog_owner_cannot_change_stored_content_type(tmp_path, spec):
    async def scenario(assets, server, dispatch, before):
        reply = await dispatch(publish(spec_id=spec, content_type="report", body=REPORT), "owner")
        assert reply["error_code"] == "asset_invalid", reply
        assert await assets._call("find_assets_by_id", asset_id="dashboard-catalog") == [before]
    run_catalog_wire(tmp_path, scenario)


def test_catalog_owner_publish_and_anonymous_delete_admission(tmp_path):
    async def scenario(assets, server, dispatch, before):
        reply = await dispatch(publish(title="Updated"), "owner")
        assert reply["type"] == "asset.publish.ok", reply
        assert reply["asset"]["title"] == "Updated"
        assert reply["asset"]["stream_id"] == before["stream_id"]
        deleted = await dispatch({"type": "asset.delete", "host": "node-alpha", "session_name": "owner",
            "asset_id": "dashboard-catalog", "spec_id": "example__other"})
        assert deleted["error_code"] == "asset_unauthorized", deleted
        assert await assets._call("find_assets_by_id", asset_id="dashboard-catalog") == [reply["asset"]]
    run_catalog_wire(tmp_path, scenario)


@pytest.mark.parametrize("field", ["stream_host", "stream_session", "from_host", "from_session",
                                   "host", "session_name", "spec_id", "asset_id", "asset_id_arg"])
@pytest.mark.parametrize("whitespace", [" ", "\t", "\n", "\u2003"])
@pytest.mark.parametrize("leading", [True, False])
@pytest.mark.parametrize("seat", [None, "owner"])
def test_publish_rejects_noncanonical_identity_without_write(tmp_path, field, whitespace, leading, seat):
    async def scenario(assets, server, dispatch, before):
        updates = []

        async def broadcast(message):
            updates.append(message)

        assets._broadcast = broadcast
        message = publish()
        alias = lambda value: whitespace + value if leading else value + whitespace
        if field.startswith(("stream_", "from_")):
            host, name = "node-alpha", "owner"
            if field.endswith("host"):
                host = alias(host)
            else:
                name = alias(name)
            if field.startswith("from_"):
                message.pop("stream_id")
                message["from_stream_id"] = f"{host}:{name}"
            else:
                message["stream_id"] = f"{host}:{name}"
        elif field == "asset_id_arg":
            message["asset_id_arg"] = alias(message.pop("asset_id"))
        else:
            message[field] = alias(message[field])
            if field in {"host", "session_name"}:
                message.pop("stream_id")
        if field not in {"spec_id", "asset_id", "asset_id_arg"}:
            message.update(spec_id="example__other", content_type="report", body=REPORT)
        # Keep the requested identity independent of the verified connection identity.
        if field.startswith("from_") and seat:
            message["stream_token"] = "synthetic-" + seat
            seat_for_dispatch = None
        else:
            seat_for_dispatch = seat
        snapshot = await assets._run(lambda: (tuple(assets._store._conn.iterdump()),
                                               assets._store._conn.total_changes))
        reply = await dispatch(message, seat_for_dispatch)
        assert reply["type"] == "asset.error", reply
        assert reply["error_code"] == "asset_invalid", reply
        assert await assets._call("find_assets_by_id", asset_id="dashboard-catalog") == [before]
        assert await assets._run(lambda: (tuple(assets._store._conn.iterdump()),
                                         assets._store._conn.total_changes)) == snapshot
        assert updates == []
    run_catalog_wire(tmp_path, scenario)


@pytest.mark.parametrize("catalog_spec", ["pentacle__dashboard_catalog", "pentacle__dashboard_catalog_v2"])
def test_wire_cross_host_full_publish_refused_and_entry_mutations_allowed(tmp_path, catalog_spec):
    async def scenario(assets, server, dispatch, before):
        await assets._sessions.open("node-beta", "cross", provider="codex", pane_status="pane_alive")
        await assets._sessions.store.grant_stream_token("node-beta", "cross",
            hashlib.sha256(b"synthetic-cross").hexdigest(), STREAM_TOKEN_HASH_VERSION)
        for spec_ids in ([], [catalog_spec]):
            await assets._sessions.store.update_session("node-beta", "cross", spec_ids=spec_ids)
            await assets._sessions.refresh()
            denied = await dispatch(publish(spec_id=catalog_spec, title="Denied"), "node-beta:cross")
            assert denied["error_code"] == "asset_unauthorized", denied
            assert await assets._call("find_assets_by_id", asset_id="dashboard-catalog") == [before]
        owner = await dispatch(publish(spec_id=catalog_spec, title="Updated"), "owner")
        assert owner["type"] == "asset.publish.ok", owner
        assert owner["asset"]["stream_id"] == before["stream_id"]
        assert owner["asset"]["spec_id"] == catalog_spec
        added = await dispatch({"type":"dashboard.add", "id":"cross-host-entry", "title":"Cross host entry",
            "url":"https://viewer.example.ts.net/app/"}, "node-beta:cross")
        assert added["type"] == "dashboard.add.ok", added
        assert added["asset"]["stream_id"] == before["stream_id"]
        removed = await dispatch({"type":"dashboard.remove", "id":"cross-host-entry"}, "node-beta:cross")
        assert removed["type"] == "dashboard.remove.ok", removed
        assert removed["asset"]["body"] == owner["asset"]["body"]
    run_catalog_wire(tmp_path, scenario, catalog_spec)
