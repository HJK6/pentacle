"""dashboard-catalog asset type and the dashboard report retrieval contract.

Catalog validation cases and report-retrieval rows come from the shared
synthetic fixtures in test/fixtures/dashboard_catalog/, which the web and
mobile catalog loaders reuse.
"""

from __future__ import annotations

import asyncio
import json
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
        assets = Assets(str(tmp_path / "assets.db"))
        await assets.start()
        try:
            return await scenario(assets)
        finally:
            await assets.stop()

    return asyncio.run(_main())


async def _publish(assets: Assets, stream_id: str, asset_id: str, *, content_type="report",
                   body=None, producer=None, spec_id=SPEC_ID) -> dict:
    msg = {"type": "asset.publish", "request_id": "p", "stream_id": stream_id,
           "asset_id": asset_id, "title": asset_id, "content_type": content_type,
           "body": body if body is not None else _report_body(asset_id), "spec_id": spec_id}
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


def test_catalog_republish_from_another_seat_keeps_owner_anchor(tmp_path):
    """Release N+1 / rollback republish from a different operator seat updates
    the same row; the listed stream_id still targets it."""
    catalog = CATALOG_CASES["valid"][0]["catalog"]
    next_catalog = dict(catalog, catalog_version="0.1.1+bbbbbbb")

    async def scenario(assets):
        await _publish(assets, "fixturehost:seat1", "dashboard-catalog", content_type="dashboard-catalog",
                       body=json.dumps(catalog), spec_id=CATALOG_SPEC_ID)
        await _publish(assets, "fixturehost:seat2", "dashboard-catalog", content_type="dashboard-catalog",
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
