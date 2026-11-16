"""dashboard-catalog asset type and the dashboard report retrieval contract.

Catalog validation cases and report-retrieval rows come from the shared
synthetic fixtures in test/fixtures/dashboard_catalog/, which the web and
mobile catalog loaders reuse.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch
from pathlib import Path

import pytest

from _shared.asset_schema import (
    AssetBodyTooLarge,
    AssetValidationError,
    report_id_grammar,
    validate_asset_payload,
)
from assets import Assets


FIXTURES = Path(__file__).resolve().parents[3] / "test" / "fixtures" / "dashboard_catalog"
CATALOG_CASES = json.loads((FIXTURES / "catalog_cases.json").read_text(encoding="utf-8"))
RETRIEVAL = json.loads((FIXTURES / "report_retrieval_cases.json").read_text(encoding="utf-8"))
SPEC_ID = RETRIEVAL["descriptor"]["spec_id"]
CATALOG_SPEC_ID = "example__dashboard_catalog"
OPERATOR = {"operator_authenticated": True, "token_verified": False, "stream_id": ""}


def _report_body(title: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "title": title,
            "sections": [{"id": "s1", "title": "S", "status": "reference", "blocks": []}],
        }
    )


def _run(tmp_path, scenario):
    async def _main():
        class Seats:
            def __init__(self):
                self.specs = {}
            def attach(self, sid, spec_id):
                self.specs.setdefault(sid, []).append("spec_" + spec_id)
            def get(self, sid):
                return {"host": "fixturehost", "status": "open", "role": "worker",
                        "spec_ids": self.specs.get(sid, [])} if sid.startswith("fixturehost:") else None
        assets = Assets(str(tmp_path / "assets.db"), sessions=Seats(), fleet_hosts={"fixturehost"})
        await assets.start()
        try:
            return await scenario(assets)
        finally:
            await assets.stop()

    with patch.dict("os.environ", {"PENTACLE_HOSTED_DASHBOARD_TAILNET_SUFFIX":"example.ts.net",
                                  "PENTACLE_HOSTED_DASHBOARD_PENTACLE_ORIGIN":"https://pentacle.example.ts.net"}):
        return asyncio.run(_main())


async def _publish(assets: Assets, stream_id: str, asset_id: str, *, content_type="report",
                   body=None, producer=None, spec_id=SPEC_ID) -> dict:
    msg = {"type": "asset.publish", "request_id": "p", "stream_id": stream_id,
           "asset_id": asset_id, "title": asset_id, "content_type": content_type,
           "body": body if body is not None else _report_body(asset_id), "spec_id": spec_id}
    if content_type == "dashboard-catalog":
        msg["_auth_context"] = {"token_verified":True, "stream_id":stream_id}
    if producer is not None:
        msg["producer"] = producer
    reply = await assets.asset(msg)
    assert reply["type"] == "asset.publish.ok", reply
    return reply["asset"]


# --- schema -----------------------------------------------------------------


@pytest.mark.parametrize("case", CATALOG_CASES["valid"], ids=lambda c: c["name"])
def test_valid_catalog_accepted_and_board_order_preserved(case):
    body = validate_asset_payload("dashboard-catalog", json.dumps(case["catalog"]))
    parsed = json.loads(body)
    assert [b["id"] for b in parsed["boards"]] == [b["id"] for b in case["catalog"]["boards"]]


@pytest.mark.parametrize("case", CATALOG_CASES["invalid"], ids=lambda c: c["name"])
def test_invalid_catalog_refused(case):
    with pytest.raises(AssetValidationError) as exc:
        validate_asset_payload("dashboard-catalog", json.dumps(case["catalog"]))
    assert case["error"] in str(exc.value)


def test_catalog_body_cap_and_malformed_json(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSET_BODY_MAX_BYTES", "64")
    with pytest.raises(AssetBodyTooLarge):
        validate_asset_payload("dashboard-catalog", json.dumps(CATALOG_CASES["valid"][0]["catalog"]))
    monkeypatch.delenv("PENTACLE_ASSET_BODY_MAX_BYTES")
    with pytest.raises(AssetValidationError, match="not valid JSON"):
        validate_asset_payload("dashboard-catalog", "{")


def test_catalog_accepts_lanes_and_navigate_action_names():
    catalog = json.loads(json.dumps(CATALOG_CASES["valid"][0]["catalog"]))
    board = next(b for b in catalog["boards"] if b["kind"] == "web-adapter")
    board["actions"] = ["lanes", "navigate"]
    assert json.loads(validate_asset_payload("dashboard-catalog", json.dumps(catalog))) == catalog


def test_report_type_unchanged_and_types_not_interchangeable():
    report = _report_body("r")
    assert json.loads(validate_asset_payload("report", report))["title"] == "r"
    with pytest.raises(AssetValidationError):
        validate_asset_payload("report", json.dumps(CATALOG_CASES["valid"][0]["catalog"]))
    with pytest.raises(AssetValidationError):
        validate_asset_payload("dashboard-catalog", report)


def test_reader_grammar_excludes_all_zero_and_variable_width_revisions():
    pattern = report_id_grammar("example-report-", "[0-9]{8}T[0-9]{4}Z", 2)
    admitted = ["example-report-20261007T1300Z", "example-report-20261007T1300Z-r01",
                "example-report-20261007T1300Z-r10"]
    refused = ["example-report-20261007T1300Z-r00", "example-report-20261007T1300Z-r9",
               "example-report-20261007T1300Z-r100", "example-digest-20261007T1300Z"]
    assert all(pattern.match(i) for i in admitted)
    assert not any(pattern.match(i) for i in refused)
    no_revisions = report_id_grammar("example-report-", "[0-9]{8}", 0)
    assert no_revisions.match("example-report-20261007")
    assert not no_revisions.match("example-report-20261007-r01")


# --- retrieval contract -----------------------------------------------------


def test_metadata_carries_owner_stream_and_producer(tmp_path):
    async def scenario(assets):
        await _publish(assets, "fixturehost:seat1", "example-report-20261007T1300Z",
                       producer="hostx:example-producer")
        return await assets.asset({"type": "asset.list", "request_id": "l", "spec_id": SPEC_ID})

    reply = _run(tmp_path, scenario)
    [meta] = reply["assets"]
    assert meta["stream_id"] == "fixturehost:seat1"
    assert meta["producer"] == "hostx:example-producer"
    assert "body" not in meta


@pytest.mark.parametrize("content_type", ["dashboard-catalog", "report"])
def test_spec_scoped_list_then_get_with_listed_stream_id(tmp_path, content_type):
    """Positive path both clients use: spec-scoped list → operator asset.get
    targeting the listed owner stream returns the body."""
    asset_id = "dashboard-catalog" if content_type == "dashboard-catalog" else "example-report-20261007T1300Z"
    body = (json.dumps(CATALOG_CASES["valid"][0]["catalog"]) if content_type == "dashboard-catalog"
            else _report_body(asset_id))

    async def scenario(assets):
        await _publish(assets, "fixturehost:publisher-seat", asset_id, content_type=content_type,
                       body=body, spec_id=CATALOG_SPEC_ID)
        listed = await assets.asset({"type": "asset.list", "request_id": "l",
                                     "spec_id": CATALOG_SPEC_ID})
        [meta] = [m for m in listed["assets"] if m["asset_id"] == asset_id]
        return meta, await assets.asset({
            "type": "asset.get", "request_id": "g", "asset_id": asset_id,
            "stream_id": meta["stream_id"], "spec_id": CATALOG_SPEC_ID,
            "_auth_context": OPERATOR})

    meta, got = _run(tmp_path, scenario)
    assert meta["content_type"] == content_type
    assert got["type"] == "asset.get.ok", got
    assert got["asset"]["content_type"] == content_type
    assert json.loads(got["asset"]["body"]) == json.loads(body)


def test_catalog_owner_republish_keeps_owner_anchor(tmp_path):
    """Release N+1 / rollback republish from the owner seat updates
    the same row; the listed stream_id still targets it."""
    catalog = CATALOG_CASES["valid"][0]["catalog"]
    next_catalog = dict(catalog, catalog_version="0.1.1+bbbbbbb")

    async def scenario(assets):
        await _publish(assets, "fixturehost:seat1", "dashboard-catalog", content_type="dashboard-catalog",
                       body=json.dumps(catalog), spec_id=CATALOG_SPEC_ID)
        await _publish(assets, "fixturehost:seat1", "dashboard-catalog", content_type="dashboard-catalog",
                       body=json.dumps(next_catalog), spec_id=CATALOG_SPEC_ID)
        listed = await assets.asset({"type": "asset.list", "request_id": "l",
                                     "spec_id": CATALOG_SPEC_ID})
        [meta] = listed["assets"]
        got = await assets.asset({"type": "asset.get", "request_id": "g",
                                  "asset_id": "dashboard-catalog", "stream_id": meta["stream_id"],
                                  "_auth_context": OPERATOR})
        return meta, got

    meta, got = _run(tmp_path, scenario)
    assert meta["stream_id"] == "fixturehost:seat1"
    assert json.loads(got["asset"]["body"])["catalog_version"] == "0.1.1+bbbbbbb"


@pytest.mark.parametrize("case", RETRIEVAL["cases"], ids=lambda c: c["name"])
def test_report_window_filters_before_sort_and_limit(tmp_path, case):
    async def scenario(assets):
        # Seed the candidate rows first so every foreign row is newer by
        # updated_at: the result must not depend on upload time.
        for index, row in enumerate(case["rows"]):
            body = (json.dumps(CATALOG_CASES["valid"][0]["catalog"])
                    if row["content_type"] == "dashboard-catalog" else None)
            await _publish(assets, f"fixturehost:seat{index}", row["asset_id"],
                           content_type=row["content_type"], body=body, producer=row["producer"])
        return await assets.asset({"type": "asset.list", "request_id": "l", **case["request"]})

    reply = _run(tmp_path, scenario)
    assert reply["type"] == "asset.list.ok", reply
    assert [m["asset_id"] for m in reply["assets"]] == case["server_ids"]
    assert all(m["producer"] == case["request"]["producer"] for m in reply["assets"])


def test_full_window_overflow_returns_exactly_w_rows(tmp_path):
    [case] = [c for c in RETRIEVAL["cases"] if c["name"].startswith("F-D")]
    window = RETRIEVAL["window"]

    async def scenario(assets):
        for index, row in enumerate(case["rows"]):
            await _publish(assets, f"fixturehost:seat{index}", row["asset_id"], producer=row["producer"])
        return await assets.asset({"type": "asset.list", "request_id": "l", **case["request"]})

    reply = _run(tmp_path, scenario)
    assert len(case["rows"]) == window + 1
    assert len(reply["assets"]) == window
    assert "example-report-20261003T1300Z" not in [m["asset_id"] for m in reply["assets"]]


def test_list_without_filters_is_unchanged(tmp_path):
    async def scenario(assets):
        for asset_id in ["example-report-a", "example-report-c", "example-report-b"]:
            await _publish(assets, "fixturehost:seat1", asset_id)
        return await assets.asset({"type": "asset.list", "request_id": "l",
                                   "spec_id": SPEC_ID, "limit": 2})

    reply = _run(tmp_path, scenario)
    # Default order stays updated_at desc (newest publish first).
    assert [m["asset_id"] for m in reply["assets"]] == ["example-report-b", "example-report-c"]


WINDOW = {"sort": "asset_id_desc", "limit": 12}


@pytest.mark.parametrize("bad", [
    {**WINDOW, "asset_id_prefix": "Bad-Prefix"},
    {**WINDOW, "asset_id_prefix": ""},
    {**WINDOW, "producer": ""},
    {**WINDOW, "producer": 7},
    {"asset_id_prefix": "example-report-", "limit": 12},           # sort required
    {"producer": "hostx:example-producer", "limit": 12},          # sort required
    {"sort": "updated_at_desc", "limit": 12},
    {"asset_id_prefix": "example-report-", "sort": "asset_id_desc"},  # limit required
    {**WINDOW, "limit": 401},
    {**WINDOW, "limit": 0},
    {**WINDOW, "stream_id": "fixturehost:seat1"},                  # spec-scoped only
])
def test_list_window_arguments_are_validated(tmp_path, bad):
    async def scenario(assets):
        return await assets.asset({"type": "asset.list", "request_id": "l",
                                   "spec_id": SPEC_ID, **bad})

    reply = _run(tmp_path, scenario)
    assert reply["type"] == "asset.error"
    assert reply["error_code"] == "asset_invalid"


def test_report_window_reads_an_index_range_not_the_namespace(tmp_path):
    """The window query is served by an index in asset_id order with LIMIT, so
    SQLite stops after `limit` matching rows (no full scan, no temp sort)."""
    from _shared.assets_store import AssetStore

    store = AssetStore(str(tmp_path / "assets.db"))
    try:
        for producer in ("hostx:example-producer", None):
            # The plan of the exact statement list_spec_window executes.
            sql, params = store._spec_window_query(SPEC_ID, limit=12, asset_id_prefix="example-report-",
                                                   producer=producer)
            plan = " | ".join(row[-1] for row in store._conn.execute(
                f"EXPLAIN QUERY PLAN {sql}", params).fetchall())
            want = "idx_assets_spec_producer_asset" if producer else "idx_assets_spec_asset"
            assert want in plan, plan
            assert "TEMP B-TREE" not in plan, plan
            assert "SCAN assets" not in plan, plan
        # Behaviour: a large namespace still returns exactly the newest W ids.
        for day in range(1, 29):
            for rev in ("", "-r01"):
                store.publish_asset(host="fixturehost", session_name="seed", stream_id="fixturehost:seed",
                                    asset_id=f"example-report-202610{day:02d}T1300Z{rev}", title="t",
                                    content_type="report", body=_report_body("t"),
                                    producer="hostx:example-producer", spec_id=SPEC_ID)
        rows = store.list_spec_window(SPEC_ID, limit=3, asset_id_prefix="example-report-",
                                      producer="hostx:example-producer")
        assert [r["asset_id"] for r in rows] == [
            "example-report-20261028T1300Z-r01", "example-report-20261028T1300Z",
            "example-report-20261027T1300Z-r01"]
    finally:
        store.close()


@pytest.mark.parametrize("prefix", ["example-report-", "a", "z", "x9", "example.report_"])
def test_prefix_range_matches_startswith(tmp_path, prefix):
    from _shared.assets_store import AssetStore

    ids = ["a", "a-", "a0", "b", "example-report-1", "example-report.", "example-report_",
           "example.report_x", "x9", "x9-r01", "x:", "y", "z", "z~", "za"]
    store = AssetStore(str(tmp_path / "assets.db"))
    try:
        for asset_id in ids:
            store.publish_asset(host="fixturehost", session_name="seed", stream_id="fixturehost:seed",
                                asset_id=asset_id, title="t", content_type="report",
                                body=_report_body("t"), spec_id=SPEC_ID)
        got = [r["asset_id"] for r in store.list_spec_window(SPEC_ID, limit=100, asset_id_prefix=prefix)]
        assert got == sorted((i for i in ids if i.startswith(prefix)), reverse=True)
    finally:
        store.close()


# Hosted dashboard registration: real store, internal seat principals, synthetic policy.
POLICY = CATALOG_CASES["hosted_policy"]
INTERNAL = {"token_verified": True, "stream_id": "node-alpha:seat", "session_generation": "g"}

class FleetSeats:
    def __init__(self):
        self.specs = {}

    def attach(self, sid, spec_id):
        self.specs.setdefault(sid, []).append("spec_" + spec_id)

    def get(self, sid):
        if sid not in {"node-alpha:seat", "node-beta:seat", "foreign:seat"}:
            return None
        return {"stream_id": sid, "host": sid.split(":")[0], "status": "open",
                "session_generation": "g", "role": "worker", "spec_ids": self.specs.get(sid, [])}


def hosted_run(tmp_path, monkeypatch, scenario, *, configured=True, catalog_spec_id=None):
    for key, value in [("TAILNET_SUFFIX", POLICY["tailnetSuffix"]), ("PENTACLE_ORIGIN", POLICY["pentacleOrigin"])]:
        env = "PENTACLE_HOSTED_DASHBOARD_" + key
        if configured: monkeypatch.setenv(env, value)
        else: monkeypatch.delenv(env, raising=False)
    async def main():
        assets = Assets(str(tmp_path / "hosted.db"), sessions=FleetSeats(), fleet_hosts={"node-alpha", "node-beta"},
                        **({"catalog_spec_id": catalog_spec_id} if catalog_spec_id is not None else {}))
        await assets.start()
        try:
            # Seed the real canonical row via the public handler.
            catalog = json.loads(json.dumps(CATALOG_CASES["valid"][-1]["catalog"]))
            catalog["boards"] = [b for b in catalog["boards"] if b["kind"] != "hosted-view"]
            reply = await assets.asset({"type":"asset.publish", "stream_id":"node-alpha:seat",
                "asset_id":"dashboard-catalog", "spec_id":catalog_spec_id or "pentacle__dashboard_catalog",
                "content_type":"dashboard-catalog", "title":"Dashboards", "body":json.dumps(catalog),
                "_auth_context": INTERNAL})
            assert reply["type"] == "asset.publish.ok", reply
            return await scenario(assets, catalog)
        finally: await assets.stop()
    return asyncio.run(main())


def mutation(verb="add", auth=None, **fields):
    return {"type":"dashboard." + verb, "request_id":"edit", "id":"hosted-example",
            "title":"Hosted example", "url":"https://viewer.example.ts.net:8444/app/",
            "_auth_context": INTERNAL if auth is None else auth, **fields}


def test_hosted_cross_host_replacement_removal_and_package_preservation(tmp_path, monkeypatch):
    async def scenario(assets, catalog):
        first = await assets.dashboard(mutation(hidden=True, order=1))
        assert first["type"] == "dashboard.add.ok", first
        body = json.loads(first["asset"]["body"])
        assert body["boards"][1]["visible"] is False
        for key in ("package", "catalog_version", "libs"):
            assert body[key] == catalog[key]
        foreign_host = dict(INTERNAL, stream_id="node-beta:seat")
        replaced = await assets.dashboard(mutation(auth=foreign_host, title="Replaced", url="https://other.example.ts.net/new/"))
        assert replaced["replaced"] is True
        body = json.loads(replaced["asset"]["body"])
        assert body["boards"][1]["name"] == "Replaced"
        assert body["boards"][1]["visible"] is False
        assert [b for b in body["boards"] if b["id"] != "hosted-example"] == catalog["boards"]
        removed = await assets.dashboard(mutation("remove", auth=foreign_host))
        assert removed["removed"] is True
        assert json.loads(removed["asset"]["body"])["boards"] == catalog["boards"]
        assert (await assets.dashboard(mutation("remove")))["removed"] is False
    hosted_run(tmp_path, monkeypatch, scenario)


def test_simultaneous_record_edits_are_serialized(tmp_path, monkeypatch):
    async def scenario(assets, catalog):
        replies = await asyncio.gather(*(assets.dashboard(mutation(id="board-"+str(i))) for i in range(12)))
        assert all(r["type"] == "dashboard.add.ok" for r in replies), replies
        final = json.loads(replies[-1]["asset"]["body"])
        assert final["boards"][:len(catalog["boards"])] == catalog["boards"]
        assert {b["id"] for b in final["boards"]} >= {"board-"+str(i) for i in range(12)}
    hosted_run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("auth", [
    {}, {"operator_authenticated":True}, {"token_verified":False,"stream_id":"node-alpha:seat"},
    dict(INTERNAL, stream_id="foreign:seat"), dict(INTERNAL, dot_principal=True),
    dict(INTERNAL, scoped_principal=True), dict(INTERNAL, service_authenticated=True),
    dict(INTERNAL, stream_id="node-alpha:missing"), dict(INTERNAL, session_generation="stale"),
])
def test_catalog_commands_and_direct_writes_deletes_share_principal_rule(tmp_path, monkeypatch, auth):
    async def scenario(assets, catalog):
        for verb in ("add", "remove"):
            reply = await assets.dashboard(mutation(verb, auth=auth, host="node-alpha", from_stream_id="node-alpha:seat"))
            assert reply["error_code"] == "dashboard_unauthorized", reply
        for content_type, body in [("dashboard-catalog",json.dumps(catalog)), ("report",_report_body("spoof"))]:
            reply = await assets.asset({"type":"asset.publish", "stream_id":"node-alpha:seat", "host":"node-alpha",
                "from_stream_id":"node-alpha:seat", "spec_id":"pentacle__dashboard_catalog", "asset_id":"dashboard-catalog",
                "content_type":content_type,"title":"spoof", "body":body,"_auth_context":auth})
            assert reply["error_code"] == "asset_unauthorized", reply
        reply = await assets.asset({"type":"asset.delete","asset_id":"dashboard-catalog",
            "stream_id":"node-alpha:seat", "_auth_context":auth})
        assert reply["error_code"] == "asset_unauthorized", reply
    hosted_run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("case", CATALOG_CASES["hosted_urls"], ids=lambda c:c["name"])
def test_url_policy_on_commands_and_complete_publication(tmp_path, monkeypatch, case):
    async def scenario(assets, catalog):
        reply = await assets.dashboard(mutation(url=case["url"]))
        assert (reply["type"] == "dashboard.add.ok") == case["allowed"], reply
        catalog["boards"].append({"id":"hosted-url", "name":"Hosted", "kind":"hosted-view", "hosted":{"url":case["url"]}})
        reply = await assets.asset({"type":"asset.publish","stream_id":"node-alpha:seat","asset_id":"dashboard-catalog",
            "spec_id":"pentacle__dashboard_catalog", "content_type":"dashboard-catalog", "title":"Catalog",
            "body":json.dumps(catalog),"_auth_context":INTERNAL})
        assert (reply["type"] == "asset.publish.ok") == case["allowed"], reply
    hosted_run(tmp_path, monkeypatch, scenario)


def test_missing_policy_refuses_hosted_mutation(tmp_path, monkeypatch):
    async def scenario(assets, catalog):
        reply = await assets.dashboard(mutation())
        assert reply["error_code"] == "dashboard_policy_unconfigured", reply
    hosted_run(tmp_path, monkeypatch, scenario, configured=False)


def test_nonhosted_collisions_and_invalid_order_leave_catalog_intact(tmp_path, monkeypatch):
    async def scenario(assets, catalog):
        for verb in ("add", "remove"):
            reply = await assets.dashboard(mutation(verb, id=catalog["boards"][0]["id"]))
            assert reply["error_code"] == "dashboard_collision", reply
        for order in (-1, True, "1", 99):
            reply = await assets.dashboard(mutation(order=order))
            assert reply["error_code"] == "dashboard_invalid", reply
    hosted_run(tmp_path, monkeypatch, scenario)


def test_authenticated_hello_snapshot_carries_host_owned_policy(tmp_path, monkeypatch):
    from server import Server
    async def main():
        server = Server(local_host="node-alpha")
        frames = await server._on_hello({"type":"hello", "hostedDashboardPolicy":{"tailnetSuffix":"evil.ts.net"}})
        for kind in ("hello", "snapshot"):
            assert next(f for f in frames if f["type"] == kind)["hostedDashboardPolicy"] == POLICY
        frames = await server._on_hello({"type":"hello","subscribe":{"mode":"rpc"}})
        assert frames[0]["hostedDashboardPolicy"] == POLICY
    monkeypatch.setenv("PENTACLE_HOSTED_DASHBOARD_TAILNET_SUFFIX", POLICY["tailnetSuffix"])
    monkeypatch.setenv("PENTACLE_HOSTED_DASHBOARD_PENTACLE_ORIGIN", POLICY["pentacleOrigin"])
    asyncio.run(main())


@pytest.mark.parametrize("suffix,origin", [(None,None), ("example.ts.net","http://pentacle.example.ts.net"), ("foreign.test","https://pentacle.foreign.test"), ("example.ts.net","https://pentacle.example.ts.net/?x"), ("example.ts.net","https://foreign.test")])
def test_missing_invalid_policy_hello_has_explicit_null(monkeypatch,suffix,origin):
    from server import Server
    for key,val in [("TAILNET_SUFFIX",suffix),("PENTACLE_ORIGIN",origin)]:
        if val is None: monkeypatch.delenv("PENTACLE_HOSTED_DASHBOARD_"+key,raising=False)
        else: monkeypatch.setenv("PENTACLE_HOSTED_DASHBOARD_"+key,val)
    frames=asyncio.run(Server(local_host="node-alpha")._on_hello({"type":"hello"}))
    assert next(f for f in frames if f["type"]=="snapshot")["hostedDashboardPolicy"] is None


@pytest.mark.parametrize("catalog_spec_id", ["pentacle__dashboard_catalog", "pentacle__dashboard_catalog_v2"])
def test_cross_host_full_publish_refused_and_owner_publish_delete(tmp_path,monkeypatch,catalog_spec_id):
    async def scenario(assets,catalog):
        auth=dict(INTERNAL,stream_id="node-beta:seat")
        before = await assets._call("find_assets_by_id", asset_id="dashboard-catalog")
        denied = await assets.asset({"type":"asset.publish", "stream_id":"node-alpha:seat",
            "content_type":"dashboard-catalog", "spec_id":catalog_spec_id,
            "asset_id":"dashboard-catalog", "title":"Denied", "body":json.dumps(catalog), "_auth_context":auth})
        assert denied["error_code"] == "asset_unauthorized", denied
        assert await assets._call("find_assets_by_id", asset_id="dashboard-catalog") == before
        assets._sessions.attach("node-beta:seat", catalog_spec_id)
        attached = await assets.asset({"type":"asset.publish", "stream_id":"node-alpha:seat",
            "content_type":"dashboard-catalog", "spec_id":catalog_spec_id,
            "asset_id":"dashboard-catalog", "title":"Denied", "body":json.dumps(catalog), "_auth_context":auth})
        assert attached["error_code"] == "asset_unauthorized", attached
        assert await assets._call("find_assets_by_id", asset_id="dashboard-catalog") == before
        auth = INTERNAL
        reply=await assets.asset({"type":"asset.publish","stream_id":"node-alpha:seat","content_type":"dashboard-catalog",
            "spec_id":catalog_spec_id,"asset_id":"dashboard-catalog", "title":"Catalog",
            "body":json.dumps(catalog),"_auth_context":auth})
        assert reply["type"]=="asset.publish.ok",reply
        assert reply["asset"]["stream_id"]=="node-alpha:seat"
        deleted=await assets.asset({"type":"asset.delete","stream_id":"node-alpha:seat","asset_id":"dashboard-catalog","_auth_context":auth})
        assert deleted["type"]=="asset.delete.ok",deleted
    hosted_run(tmp_path,monkeypatch,scenario,catalog_spec_id=catalog_spec_id)


def test_wire_forgery_refused_and_verified_token_commands_work(tmp_path,monkeypatch):
    from server import Server
    from sessions import Sessions
    from store import Store, STREAM_TOKEN_HASH_VERSION
    import hashlib
    class Peer:
        remote_address=("127.0.0.1",12345)
    async def main():
        store=Store(str(tmp_path/"sessions.db"));store.start()
        sessions=Sessions(store,local_host="node-alpha")
        await sessions.open("node-alpha","seat",provider="codex",pane_status="pane_alive")
        await store.grant_stream_token("node-alpha","seat",hashlib.sha256(b"synthetic-dashboard-seat").hexdigest(),STREAM_TOKEN_HASH_VERSION)
        assets=Assets(str(tmp_path/"assets.db"),sessions=sessions,fleet_hosts={"node-alpha","node-beta"})
        await assets.start()
        server=Server(store=store,sessions=sessions,local_host="node-alpha")
        server.handlers.update(assets.wire_handlers())
        try:
            wire=mutation(); wire["_auth_context"]=INTERNAL
            assert (await server._dispatch(json.dumps(wire),websocket=Peer()))[0]["error_code"]=="dashboard_unauthorized"
            peer=Peer()
            wire.update(from_stream_id="node-alpha:seat",stream_token="synthetic-dashboard-seat")
            # A verified principal passes admission and reaches the missing-catalog condition.
            assert (await server._dispatch(json.dumps(wire),websocket=peer))[0]["error_code"]=="dashboard_catalog_missing"
            wire["from_stream_id"]="node-beta:seat"
            assert (await server._dispatch(json.dumps(wire),websocket=Peer()))[0]["error_code"]=="dashboard_unauthorized"
        finally:
            await assets.stop();store.stop()
    monkeypatch.setenv("PENTACLE_HOSTED_DASHBOARD_TAILNET_SUFFIX",POLICY["tailnetSuffix"])
    monkeypatch.setenv("PENTACLE_HOSTED_DASHBOARD_PENTACLE_ORIGIN",POLICY["pentacleOrigin"])
    asyncio.run(main())


@pytest.mark.parametrize("catalog_spec_id", [None, "pentacle__dashboard_catalog_v2"])
def test_dashboard_commands_target_default_or_configured_catalog(tmp_path, monkeypatch, catalog_spec_id):
    selected = catalog_spec_id or "pentacle__dashboard_catalog"
    other = "pentacle__dashboard_catalog_v2" if catalog_spec_id is None else "pentacle__dashboard_catalog"

    async def scenario(assets, catalog):
        # Keep another catalog at a distinct owner anchor; commands must not edit it.
        other_auth = dict(INTERNAL, stream_id="node-beta:seat")
        published = await assets.asset({"type": "asset.publish", "stream_id": "node-beta:seat",
            "asset_id": "dashboard-catalog", "spec_id": other, "content_type": "dashboard-catalog",
            "title": "Other catalog", "body": json.dumps(catalog), "_auth_context": other_auth})
        assert published["type"] == "asset.publish.ok", published
        before = published["asset"]["body"]
        added = await assets.dashboard(mutation(spec_id=other))
        assert added["type"] == "dashboard.add.ok", added
        assert added["asset"]["spec_id"] == selected
        assert any(b["id"] == "hosted-example" for b in json.loads(added["asset"]["body"])["boards"])
        removed = await assets.dashboard(mutation("remove", spec_id=other))
        assert removed["type"] == "dashboard.remove.ok", removed
        assert removed["asset"]["spec_id"] == selected
        assert removed["removed"] is True
        assert json.loads(removed["asset"]["body"])["boards"] == catalog["boards"]
        records = await assets._call("find_assets_by_id", asset_id="dashboard-catalog")
        assert next(r for r in records if r["spec_id"] == other)["body"] == before
    hosted_run(tmp_path, monkeypatch, scenario, catalog_spec_id=catalog_spec_id)


@pytest.mark.parametrize("auth", [
    {}, {"operator_authenticated": True}, dict(INTERNAL, stream_id="foreign:seat"),
    dict(INTERNAL, dot_principal=True), dict(INTERNAL, scoped_principal=True),
    dict(INTERNAL, service_authenticated=True), dict(INTERNAL, session_generation="stale"),
])
def test_configured_catalog_direct_writes_and_deletes_refuse_non_internal(tmp_path, monkeypatch, auth):
    async def scenario(assets, catalog):
        for content_type, body in [("dashboard-catalog", json.dumps(catalog)), ("report", _report_body("spoof"))]:
            reply = await assets.asset({"type": "asset.publish", "stream_id": "node-alpha:seat",
                "asset_id": "dashboard-catalog", "spec_id": "pentacle__dashboard_catalog_v2",
                "content_type": content_type, "title": "Denied", "body": body, "_auth_context": auth})
            assert reply["error_code"] == "asset_unauthorized", reply
        reply = await assets.asset({"type": "asset.delete", "asset_id": "dashboard-catalog",
            "stream_id": "node-alpha:seat", "_auth_context": auth})
        assert reply["error_code"] == "asset_unauthorized", reply
    hosted_run(tmp_path, monkeypatch, scenario, catalog_spec_id="pentacle__dashboard_catalog_v2")


def test_daemon_catalog_target_configuration_defaults_and_override():
    from main import parse_args
    assert parse_args([]).dashboard_catalog_spec_id == "pentacle__dashboard_catalog"
    assert parse_args(["--dashboard-catalog-spec-id", "pentacle__dashboard_catalog_v2"]).dashboard_catalog_spec_id == "pentacle__dashboard_catalog_v2"


def test_configured_catalog_reserved_target_refuses_first_non_catalog_write(tmp_path):
    async def run():
        assets = Assets(str(tmp_path / "configured-empty.db"), sessions=FleetSeats(),
            fleet_hosts={"node-alpha", "node-beta"}, catalog_spec_id="pentacle__dashboard_catalog_v2")
        await assets.start()
        try:
            # No existing catalog row can supply the content-type guard here.
            for spec_id in ("pentacle__dashboard_catalog", "pentacle__dashboard_catalog_v2"):
                reply = await assets.asset({"type": "asset.publish", "stream_id": "node-alpha:seat",
                    "asset_id": "dashboard-catalog", "spec_id": spec_id, "content_type": "report",
                    "title": "Spoof", "body": _report_body("spoof"),
                    "_auth_context": {"operator_authenticated": True}})
                assert reply["error_code"] == "asset_unauthorized", reply
            assert await assets._call("find_assets_by_id", asset_id="dashboard-catalog") == []
        finally:
            await assets.stop()
    asyncio.run(run())


def catalog_wire_run(tmp_path, monkeypatch, scenario):
    """Real dispatch derives authority from a granted synthetic seat token."""
    from server import Server
    from sessions import Sessions
    from store import Store, STREAM_TOKEN_HASH_VERSION
    import hashlib

    class Peer:
        remote_address = ("127.0.0.1", 12345)

    async def main():
        store = Store(str(tmp_path / "wire-sessions.db"))
        store.start()
        sessions = Sessions(store, local_host="node-alpha")
        await sessions.open("node-alpha", "seat", provider="codex", pane_status="pane_alive")
        await store.grant_stream_token("node-alpha", "seat",
            hashlib.sha256(b"synthetic-dashboard-seat").hexdigest(), STREAM_TOKEN_HASH_VERSION)
        assets = Assets(str(tmp_path / "wire-assets.db"), sessions=sessions,
            fleet_hosts={"node-alpha"}, catalog_spec_id="pentacle__dashboard_catalog_v2")
        await assets.start()
        server = Server(store=store, sessions=sessions, local_host="node-alpha")
        server.handlers.update(assets.wire_handlers())

        async def dispatch(msg, *, verified=False):
            msg = dict(msg)
            if verified:
                msg.update(from_stream_id="node-alpha:seat", stream_token="synthetic-dashboard-seat")
            return (await server._dispatch(json.dumps(msg), websocket=Peer()))[0]

        try:
            catalog = CATALOG_CASES["valid"][0]["catalog"]
            await scenario(assets, catalog, dispatch)
        finally:
            await assets.stop()
            store.stop()

    monkeypatch.setenv("PENTACLE_HOSTED_DASHBOARD_TAILNET_SUFFIX", POLICY["tailnetSuffix"])
    monkeypatch.setenv("PENTACLE_HOSTED_DASHBOARD_PENTACLE_ORIGIN", POLICY["pentacleOrigin"])
    asyncio.run(main())


def catalog_wire_publish(catalog, spec_id, **fields):
    return {"type": "asset.publish", "host": "node-alpha", "session_name": "seat",
        "asset_id": "dashboard-catalog", "spec_id": spec_id, "title": "Catalog",
        "content_type": "dashboard-catalog", "body": json.dumps(catalog), **fields}


@pytest.mark.parametrize("seed_spec", ["arbitrary__catalog", "pentacle__dashboard_catalog", "pentacle__dashboard_catalog_v2"])
@pytest.mark.parametrize("verb,verified", [("publish", False), ("publish", True), ("delete", False)])
def test_wire_alternate_spec_cannot_replace_or_delete_catalog(tmp_path, monkeypatch, seed_spec, verb, verified):
    async def scenario(assets, catalog, dispatch):
        seeded = await dispatch(catalog_wire_publish(catalog, seed_spec), verified=True)
        assert seeded["type"] == "asset.publish.ok", seeded
        before = seeded["asset"]
        attack = catalog_wire_publish(catalog, "unrelated__report", type="asset." + verb,
            content_type="report", body=_report_body("Spoof"), title="Spoof",
            _auth_context=INTERNAL)  # Wire authority must be discarded by Server.
        reply = await dispatch(attack, verified=verified)
        assert reply["type"] == "asset.error", reply
        if not verified:
            assert reply["error_code"] == "asset_unauthorized", reply
        assert await assets._call("find_assets_by_id", asset_id="dashboard-catalog") == [before]
    catalog_wire_run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("wire_host", ["node-alpha", "node-beta"])
def test_wire_same_writer_legacy_then_v2_refuses_physical_alias_without_changing_old_row(tmp_path, monkeypatch, wire_host):
    async def scenario(assets, catalog, dispatch):
        old = await dispatch(catalog_wire_publish(catalog, "pentacle__dashboard_catalog"), verified=True)
        assert old["type"] == "asset.publish.ok", old
        revised = dict(catalog, catalog_version="v2-candidate")
        reply = await dispatch(catalog_wire_publish(revised, "pentacle__dashboard_catalog_v2", host=wire_host), verified=True)
        assert reply["type"] == "asset.error", reply
        assert reply["error_code"] == "asset_invalid", reply
        assert "spec" in reply["message"], reply
        readback = await dispatch({"type": "asset.get", "stream_id": "node-alpha:seat",
            "spec_id": "pentacle__dashboard_catalog", "asset_id": "dashboard-catalog"}, verified=True)
        assert readback["type"] == "asset.get.ok", readback
        assert readback["asset"]["body"] == old["asset"]["body"]
        assert readback["asset"]["spec_id"] == "pentacle__dashboard_catalog"
        assert await assets._call("list_by_spec_id", spec_id="pentacle__dashboard_catalog_v2") == []
    catalog_wire_run(tmp_path, monkeypatch, scenario)
