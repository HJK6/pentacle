import json
import os

import pytest

from dashboard_hub import auth, collectors, server
from dashboard_hub.auth import operator_auth


@pytest.fixture
def registry(tmp_path):
    folder = tmp_path / "creds"
    folder.mkdir(mode=0o700)
    reg = operator_auth.OperatorCredentialRegistry(folder / "operator-credentials.json")
    reg.initialize()
    return reg


def test_verify_token_accepts_mobile_and_rejects_others(registry):
    cred_id, token = registry.issue("pentacle-mobile", label="phone")
    assert auth.verify_token(token, registry) == cred_id

    _desktop_id, desktop_token = registry.issue("pentacle", label="desktop")
    with pytest.raises(auth.Unauthorized):
        auth.verify_token(desktop_token, registry)

    registry.revoke(cred_id)
    with pytest.raises(auth.Unauthorized):
        auth.verify_token(token, registry)

    for bad in (None, "", "pentacle-auth-v2:garbage", "not-a-token"):
        with pytest.raises(auth.Unauthorized):
            auth.verify_token(bad, registry)


def _fake_verify(token):
    if token != "good":
        raise auth.Unauthorized("nope")
    return "cred-123456789"


async def _fake_collect(_http):
    return {"value": 42}


async def _failing_collect(_http):
    raise RuntimeError("upstream down")


@pytest.fixture
def hub():
    return server.Hub({"sai.test": (_fake_collect, 3600, 60), "sai.broken": (_failing_collect, 3600, 60)},
                      verify=_fake_verify)


async def test_live_socket_protocol(aiohttp_client, hub):
    client = await aiohttp_client(server.build_app(hub))
    resp = await client.get("/live?token=bad")
    assert resp.status == 401

    ws = await client.ws_connect("/live?token=good")
    assert (await ws.receive_json())["type"] == "welcome"
    seen = {}
    while len(seen) < 2:
        msg = await ws.receive_json(timeout=5)
        if msg["type"] == "snapshot":
            seen[msg["envelope"]["dashboard_id"]] = msg["envelope"]
    ok = seen["sai.test"]
    assert ok["data"] == {"ok": True, "value": 42}
    assert ok["updated_at"].endswith("Z") and ok["server_received_at"].endswith("Z")
    assert ok["freshness_ttl_sec"] == 60 and ok["schema_version"] == 1
    assert seen["sai.broken"]["data"]["ok"] is False
    assert "upstream down" in seen["sai.broken"]["data"]["error"]
    await ws.close()


async def test_api_requires_bearer_and_validates(aiohttp_client, hub):
    client = await aiohttp_client(server.build_app(hub))
    assert (await client.get("/api/finance/transactions")).status == 401
    headers = {"Authorization": "Bearer good"}
    resp = await client.get("/api/finance/transactions?start=yesterday", headers=headers)
    assert resp.status == 400
    resp = await client.get("/api/finance/transactions?account_id=1;drop", headers=headers)
    assert resp.status == 400
    resp = await client.post("/api/travel/searches", headers=headers,
                             json={"origin": "SF", "dest": "NYC", "date": "2026-10-01"})
    assert resp.status == 400
    resp = await client.get("/api/travel/searches/..%2Fvault", headers=headers)
    assert resp.status in (400, 404)
    resp = await client.post("/api/boards/sai.test/refresh", headers=headers)
    assert (await resp.json())["envelope"]["data"]["value"] == 42


def test_parse_trader_log_finds_scan_closest_miss_and_alerts():
    lines = [
        "2026-09-28 12:04:09.995 | INFO     | execution.trader.bot:_vix:1134 - vix_heartbeat VIX=16.00 limit=26.0 margin=10.00",
        "2026-09-28 12:10:00.000 | INFO     | execution.trader.bot:scan:1222 - Scored 9/9 strategies at 2026-09-28 13:10:00",
        "2026-09-28 12:10:00.100 | DEBUG    | execution.trader.bot:sel:674 -   [skip] IC_05d_25w tp10 raw=0.547 cutoff=0.600",
        "2026-09-28 12:11:00.000 | INFO     | execution.trader.bot:scan:1222 - Scored 2/9 strategies at 2026-09-28 13:11:00",
        "2026-09-28 12:11:00.100 | DEBUG    | execution.trader.bot:sel:674 -   [skip] IC_05d_25w tp10 raw=0.400 cutoff=0.500",
        "2026-09-28 12:11:00.100 | DEBUG    | execution.trader.bot:sel:674 -   [skip] IC_10d_25w tp20 raw=0.480 cutoff=0.500",
        "2026-09-28 12:11:05.000 | ERROR    | execution.trader.bot:alert:9 - TELEGRAM: 0DTE Trader started",
    ]
    parsed = collectors.parse_trader_log(lines)
    assert parsed["vix"] == 16.0
    scan = parsed["last_scan"]
    assert scan["scored"] == 2 and scan["skipped"] == 2
    assert scan["closest"]["strategy"] == "IC_10d_25w" and scan["closest"]["gap"] == -0.02
    assert parsed["events"][0]["level"] == "ALERT"
    assert parsed["events"][0]["message"] == "0DTE Trader started"


def test_market_phase_boundaries():
    from datetime import datetime
    def at(h, m, day=28):
        return datetime(2026, 9, day, h, m, tzinfo=collectors.NY)
    assert collectors.market_phase(at(9, 0)) == "pre-market"
    assert collectors.market_phase(at(10, 0)) == "entry window"
    assert collectors.market_phase(at(14, 55)) == "forced exit done"
    assert collectors.market_phase(at(12, 0, day=27)) == "weekend"


def test_slim_search_trims_groups():
    group = {
        "legs": [{"carrier": "UA", "flight_no": 123, "dep": "SFO", "arr": "DEN",
                  "dep_t_iso": "2026-10-01T08:00:00", "arr_t_iso": "2026-10-01T11:30:00"}],
        "stops": 0, "duration_min": 150, "date": "2026-10-01",
        "best_cash": {"cash_total": 129.0, "source": "google"},
        "best_points": {"points": 7500, "taxes": 5.6, "program": "United", "source": "pointsyeah"},
        "cash_offers": [{}] * 10, "points_offers": [{}] * 10,
    }
    slim = collectors.slim_search({"id": "abc", "done": True, "sources": {}, "dollars": [group] * 40, "points": [group]})
    assert len(slim["dollars"]) == 25
    row = slim["points"][0]
    assert row["route"] == "UA123 SFO→DEN 08:00–11:30"
    assert row["points"] == 7500 and row["cash"] == 129.0
    assert "cash_offers" not in row


def test_project_row_from_status_card():
    row = collectors.project_row({
        "stream_id": "local:x", "display_name": "Do thing", "working": True, "online": True,
        "status_card": json.dumps({"goal": "g", "update": "u", "plan": [
            {"text": "a", "status": "done"}, {"text": "b", "status": "active"}]}),
    })
    assert row["title"] == "Do thing" and row["plan_done"] == 1 and row["plan_total"] == 2
    assert row["state"] == "working"
