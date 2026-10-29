"""Dashboard hub server (port 7781).

WebSocket `/live?token=...` speaks the protocol Pentacle Mobile's
dashboardHubClient expects: `welcome`, periodic `ping`, and `snapshot`
messages carrying a dashboard envelope. `/api/*` holds the few interactive
actions the boards offer (search, sync), each authenticated with
`Authorization: Bearer <device token>`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import web

from . import collectors
from .auth import Unauthorized, bearer, verify_token

log = logging.getLogger("dashboard_hub")

PING_SEC = 30
SCHEMA_VERSION = 1
DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d$")
IATA_RE = re.compile(r"^[A-Z]{3}$")
SEARCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

HUB_KEY = web.AppKey("hub", "Hub")


class Hub:
    def __init__(self, collector_table: dict[str, tuple[Any, int, int]] | None = None, verify=verify_token):
        self.collectors = collector_table if collector_table is not None else collectors.COLLECTORS
        self.verify = verify
        self.envelopes: dict[str, dict[str, Any]] = {}
        self.clients: set[web.WebSocketResponse] = set()
        self.wakeups = {dashboard_id: asyncio.Event() for dashboard_id in self.collectors}
        self.tasks: list[asyncio.Task] = []
        self.http: aiohttp.ClientSession | None = None

    # ── collection ────────────────────────────────────────────────────────
    async def start(self, _app: web.Application | None = None) -> None:
        self.http = aiohttp.ClientSession()
        for dashboard_id in self.collectors:
            self.tasks.append(asyncio.create_task(self._loop(dashboard_id)))
        self.tasks.append(asyncio.create_task(self._pinger()))

    async def stop(self, _app: web.Application | None = None) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for ws in list(self.clients):
            await ws.close()
        if self.http:
            await self.http.close()

    def refresh(self, dashboard_id: str) -> None:
        if dashboard_id in self.wakeups:
            self.wakeups[dashboard_id].set()

    async def collect_once(self, dashboard_id: str) -> dict[str, Any]:
        collect, _interval, ttl = self.collectors[dashboard_id]
        try:
            data = await collect(self.http)
            data = {"ok": True, **data}
        except Exception as exc:
            log.warning("collector %s failed: %s", dashboard_id, exc)
            previous = (self.envelopes.get(dashboard_id) or {}).get("data") or {}
            # Keep the last good numbers on screen, flagged with the error.
            data = {**previous, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        now = collectors.iso_now()
        envelope = {
            "dashboard_id": dashboard_id,
            "schema_version": SCHEMA_VERSION,
            "updated_at": now,
            "server_received_at": now,
            "freshness_ttl_sec": ttl,
            "data": data,
        }
        self.envelopes[dashboard_id] = envelope
        await self.broadcast({"type": "snapshot", "envelope": envelope})
        return envelope

    async def _loop(self, dashboard_id: str) -> None:
        _collect, interval, _ttl = self.collectors[dashboard_id]
        wake = self.wakeups[dashboard_id]
        while True:
            await self.collect_once(dashboard_id)
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), interval)
            except asyncio.TimeoutError:
                pass

    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(PING_SEC)
            await self.broadcast({"type": "ping", "ts": collectors.iso_now()})

    async def broadcast(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_str(payload)
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)

    # ── auth ──────────────────────────────────────────────────────────────
    def authorize(self, token: str | None) -> str:
        try:
            return self.verify(token)
        except Unauthorized as exc:
            raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": "unauthorized"}),
                                       content_type="application/json") from exc


# ── HTTP handlers ─────────────────────────────────────────────────────────────

def _hub(request: web.Request) -> Hub:
    return request.app[HUB_KEY]


def _authed(request: web.Request) -> Hub:
    hub = _hub(request)
    hub.authorize(bearer(request.headers.get("Authorization")))
    return hub


def _bad(message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=400)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "dashboard-hub"})


async def live(request: web.Request) -> web.WebSocketResponse:
    hub = _hub(request)
    credential = hub.authorize(request.query.get("token"))
    ws = web.WebSocketResponse(max_msg_size=1 << 20)
    await ws.prepare(request)
    hub.clients.add(ws)
    log.info("phone connected (%s…), %d client(s)", credential[:8], len(hub.clients))
    try:
        await ws.send_json({"type": "welcome", "ts": collectors.iso_now()})
        for envelope in list(hub.envelopes.values()):
            await ws.send_str(json.dumps({"type": "snapshot", "envelope": envelope}, default=str))
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                message = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("type") == "refresh":
                target = message.get("dashboard_id")
                for dashboard_id in hub.collectors if not target else [target]:
                    hub.refresh(str(dashboard_id))
    finally:
        hub.clients.discard(ws)
    return ws


async def refresh_board(request: web.Request) -> web.Response:
    hub = _authed(request)
    dashboard_id = request.match_info["dashboard_id"]
    if dashboard_id not in hub.collectors:
        return _bad("unknown board")
    envelope = await hub.collect_once(dashboard_id)
    return web.json_response({"ok": True, "envelope": envelope})


async def finance_filters(request: web.Request) -> web.Response:
    hub = _authed(request)
    accounts = await collectors.get_json(hub.http, f"{collectors.LEWS_URL}/api/accounts")
    categories = await collectors.get_json(hub.http, f"{collectors.LEWS_URL}/api/categories")
    return web.json_response({
        "ok": True,
        "accounts": [{"id": a.get("id"), "name": a.get("name"), "institution": a.get("institution")}
                     for a in accounts if a.get("active", 1)],
        "categories": [{"id": c.get("id"), "name": c.get("name"), "kind": c.get("kind")} for c in categories],
    })


async def finance_transactions(request: web.Request) -> web.Response:
    hub = _authed(request)
    params: dict[str, str] = {}
    q = request.query.get("q", "").strip()
    if q:
        params["q"] = q[:100]
    for key in ("account_id", "category_id"):
        value = request.query.get(key, "").strip()
        if value:
            if not value.isdigit():
                return _bad(f"{key} must be a number")
            params[key] = value
    for key in ("start", "end"):
        value = request.query.get(key, "").strip()
        if value:
            if not DATE_RE.match(value):
                return _bad(f"{key} must be YYYY-MM-DD")
            params[key] = value
    try:
        limit = max(1, min(int(request.query.get("limit", "150")), 500))
    except ValueError:
        return _bad("limit must be a number")
    async with hub.http.get(f"{collectors.LEWS_URL}/api/transactions", params=params,
                            timeout=collectors.HTTP_TIMEOUT) as resp:
        resp.raise_for_status()
        rows = await resp.json()
    keep = ("id", "date", "amount", "description", "merchant", "category", "category_kind",
            "account_id", "account_name", "event_name")
    total = sum(float(r.get("amount") or 0) for r in rows)
    return web.json_response({
        "ok": True,
        "count": len(rows),
        "capped": len(rows) >= 1000,
        "total": round(total, 2),
        "transactions": [{k: r.get(k) for k in keep if k in r} for r in rows[:limit]],
    })


async def finance_month(request: web.Request) -> web.Response:
    hub = _authed(request)
    month = request.query.get("month", datetime.now().strftime("%Y-%m"))
    if not re.match(r"^\d{4}-\d\d$", month):
        return _bad("month must be YYYY-MM")
    data = await collectors.get_json(hub.http, f"{collectors.LEWS_URL}/api/waterfall?month={month}&view=category")
    return web.json_response({"ok": True, "month": month, "waterfall": data})


async def finance_sync(request: web.Request) -> web.Response:
    hub = _authed(request)
    async with hub.http.post(f"{collectors.LEWS_URL}/api/sync",
                             timeout=aiohttp.ClientTimeout(total=300)) as resp:
        body = await resp.json(content_type=None)
    hub.refresh("sai.finance")
    errors = body.get("errors") if isinstance(body, dict) else None
    return web.json_response({
        "ok": resp.status == 200 and not errors,
        "status": resp.status,
        "elapsed_seconds": body.get("elapsed_seconds") if isinstance(body, dict) else None,
        "errors": errors or [],
    })


async def travel_start_search(request: web.Request) -> web.Response:
    hub = _authed(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _bad("expected JSON")
    origin = str(body.get("origin", "")).strip().upper()
    dest = str(body.get("dest", "")).strip().upper()
    date = str(body.get("date", "")).strip()
    if not IATA_RE.match(origin) or not IATA_RE.match(dest):
        return _bad("airports must be 3-letter codes")
    if not DATE_RE.match(date):
        return _bad("date must be YYYY-MM-DD")
    async with hub.http.post(f"{collectors.TRAVEL_URL}/api/searches",
                             json={"origin": origin, "dest": dest, "date": date},
                             timeout=collectors.HTTP_TIMEOUT) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            return web.json_response({"ok": False, "error": str(data)[:300]}, status=502)
    return web.json_response({"ok": True, "search_id": data.get("search_id")})


async def travel_get_search(request: web.Request) -> web.Response:
    hub = _authed(request)
    search_id = request.match_info["search_id"]
    if not SEARCH_ID_RE.match(search_id):
        return _bad("bad search id")
    async with hub.http.get(f"{collectors.TRAVEL_URL}/api/searches/{search_id}",
                            timeout=collectors.HTTP_TIMEOUT) as resp:
        if resp.status == 404:
            return web.json_response({"ok": False, "error": "search expired"}, status=404)
        resp.raise_for_status()
        data = await resp.json()
    return web.json_response({"ok": True, "search": collectors.slim_search(data)})


@web.middleware
async def errors_as_json(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("upstream error on %s: %s", request.path, exc)
        return web.json_response({"ok": False, "error": f"upstream unavailable ({type(exc).__name__})"}, status=502)


def build_app(hub: Hub | None = None, *, start_collectors: bool = True) -> web.Application:
    hub = hub or Hub()
    app = web.Application(middlewares=[errors_as_json])
    app[HUB_KEY] = hub
    if start_collectors:
        app.on_startup.append(hub.start)
        app.on_cleanup.append(hub.stop)
    app.router.add_get("/health", health)
    app.router.add_get("/live", live)
    app.router.add_post("/api/boards/{dashboard_id}/refresh", refresh_board)
    app.router.add_get("/api/finance/filters", finance_filters)
    app.router.add_get("/api/finance/transactions", finance_transactions)
    app.router.add_get("/api/finance/month", finance_month)
    app.router.add_post("/api/finance/sync", finance_sync)
    app.router.add_post("/api/travel/searches", travel_start_search)
    app.router.add_get("/api/travel/searches/{search_id}", travel_get_search)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Pentacle dashboard hub")
    parser.add_argument("--host", action="append",
                        help="bind address (repeatable); default 127.0.0.1 and HUB_TAILSCALE_IP")
    parser.add_argument("--port", type=int, default=int(os.environ.get("HUB_PORT", "7781")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    hosts = args.host or ["127.0.0.1", os.environ.get("HUB_TAILSCALE_IP", "127.0.0.1")]
    web.run_app(build_app(), host=hosts, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
