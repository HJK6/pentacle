"""Snapshot collectors for the four boards.

Every collector only *reads*: local files, SQLite opened read-only, and GET
endpoints of the apps it summarizes. Anything that changes state lives in
server.py behind an explicit phone action.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

HOME = Path.home()
NY = ZoneInfo("America/New_York")

TRADING_REPO = Path(os.environ.get("HUB_TRADING_REPO", HOME / "workspace/iv-rank-scanner"))
TRADING_PID_DIR = Path(os.environ.get("HUB_TRADING_PID_DIR", HOME / ".0dte/local"))
TRADING_STATE_DIR = Path(os.environ.get("HUB_TRADING_STATE_DIR", HOME / ".0dte/state/local"))
ROUTER_URL = os.environ.get("HUB_ROUTER_URL", "http://127.0.0.1:7410")
LEWS_URL = os.environ.get("HUB_LEWS_URL", "http://127.0.0.1:8765")
TRAVEL_URL = os.environ.get("HUB_TRAVEL_URL", "http://127.0.0.1:8777")
AGENT_ORCH = os.environ.get("HUB_AGENT_ORCH", str(HOME / "pentacle/.venv/bin/agent-orch"))
SESSIONS_DB = Path(os.environ.get("HUB_SESSIONS_DB", HOME / ".config/pentacle/sessions.db"))

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def get_json(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
        resp.raise_for_status()
        return await resp.json()


async def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


def pid_alive(pidfile: Path) -> bool:
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def tail_lines(path: Path, max_bytes: int = 400_000) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > max_bytes else lines


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
    except OSError:
        pass
    return out


# ── Trading ──────────────────────────────────────────────────────────────────

LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ \| (\w+)\s*\| [^-]+- (.*)$")
SKIP_LINE = re.compile(r"\[skip\] (\S+) tp(\d+) raw=([\d.]+) cutoff=([\d.]+)")
SCORED_LINE = re.compile(r"Scored (\d+)/(\d+) strategies at (.+)$")
VIX_LINE = re.compile(r"vix_heartbeat VIX=([\d.]+)")
EVENT_PATTERNS = re.compile(
    r"FILLED|TP HIT|SL HIT|Close filled|Kill switch|ircuit breaker|— skip|TELEGRAM:|Day P&L|forced exit",
)


def market_phase(now_ny: datetime) -> str:
    if now_ny.weekday() >= 5:
        return "weekend"
    hm = now_ny.hour * 60 + now_ny.minute
    if hm < 9 * 60 + 30:
        return "pre-market"
    if hm < 10 * 60:
        return "open, waiting for 10:00 entries"
    if hm < 14 * 60 + 55:
        return "entry window"
    if hm < 16 * 60:
        return "forced exit done"
    return "closed"


def _log_time_ny(stamp: str) -> str:
    """Trader log stamps are Mac local time; show them in ET like everything else."""
    try:
        local = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").astimezone()
    except ValueError:
        return stamp[11:]
    return local.astimezone(NY).strftime("%H:%M:%S")


def parse_trader_log(lines: list[str]) -> dict[str, Any]:
    vix = None
    last_scan: dict[str, Any] | None = None
    skips: list[tuple[str, int, float, float]] = []
    events: list[dict[str, str]] = []
    for line in lines:
        m = LOG_LINE.match(line)
        if not m:
            continue
        ts, level, msg = m.groups()
        if (v := VIX_LINE.search(msg)):
            vix = float(v.group(1))
        if (s := SCORED_LINE.search(msg)):
            last_scan = {"scored": int(s.group(1)), "total": int(s.group(2)), "at": s.group(3).strip()}
            skips = []
            continue
        if (k := SKIP_LINE.search(msg)):
            skips.append((k.group(1), int(k.group(2)), float(k.group(3)), float(k.group(4))))
            continue
        if level in ("ERROR", "CRITICAL") or EVENT_PATTERNS.search(msg):
            if msg.startswith("TELEGRAM: "):
                # The local Telegram stub logs its alerts at ERROR; they are notices.
                level, msg = "ALERT", msg[len("TELEGRAM: "):]
            events.append({"time": _log_time_ny(ts), "level": level, "message": msg[:240]})
    if last_scan is not None:
        last_scan["skipped"] = len(skips)
        if skips:
            strat, tp, raw, cutoff = max(skips, key=lambda item: item[2] - item[3])
            last_scan["closest"] = {
                "strategy": strat, "tp": tp, "raw": raw, "cutoff": cutoff, "gap": round(raw - cutoff, 3),
            }
    return {"vix": vix, "last_scan": last_scan, "events": events[-25:][::-1]}


async def collect_trading(session: aiohttp.ClientSession) -> dict[str, Any]:
    now_ny = datetime.now(NY)
    day = now_ny.strftime("%Y%m%d")
    log_path = TRADING_REPO / "logs/local" / f"trader_{day}.log"
    try:
        log_age = time.time() - log_path.stat().st_mtime
    except OSError:
        log_age = None

    router: dict[str, Any] = {}
    try:
        router["health"] = await get_json(session, f"{ROUTER_URL}/health")
        router["account"] = await get_json(session, f"{ROUTER_URL}/account/summary")
        router["positions"] = (await get_json(session, f"{ROUTER_URL}/positions/list")).get("positions", [])
        router["orders"] = (await get_json(session, f"{ROUTER_URL}/orders/list?status_filter=open")).get("orders", [])
    except Exception as exc:  # router down is a normal state outside trading hours
        router["error"] = type(exc).__name__

    gateway_up, theta_up = await asyncio.gather(port_open("127.0.0.1", 4002), port_open("127.0.0.1", 25503))
    health = router.get("health") or {}
    services = [
        {"name": "Trader", "up": pid_alive(TRADING_PID_DIR / "trader.pid"),
         "detail": f"log {int(log_age)}s ago" if log_age is not None else "no log today"},
        {"name": "Order router", "up": "health" in router,
         "detail": "IBKR connected" if health.get("connected") else "not connected"},
        {"name": "IB Gateway", "up": gateway_up, "detail": "port 4002"},
        {"name": "ThetaData", "up": theta_up, "detail": "port 25503"},
        {"name": "Collector", "up": pid_alive(TRADING_PID_DIR / "collector.pid"), "detail": "1-min market data"},
    ]

    positions = read_jsonl(TRADING_STATE_DIR / "positions_acct1.jsonl")
    today_iso = now_ny.strftime("%Y-%m-%d")
    todays = [p for p in positions if today_iso in str(p.get("entry_time", today_iso))]
    realized = sum(float(p.get("close_pnl") or 0) for p in todays if p.get("closed"))
    open_positions = [p for p in todays if not p.get("closed")]
    entries = read_jsonl(TRADING_REPO / "6_execution/sai_audit_logs" / day / "entries.jsonl")

    account = router.get("account") or {}
    return {
        "phase": market_phase(now_ny),
        "now_ny": now_ny.strftime("%a %H:%M ET"),
        "services": services,
        "account_id": account.get("account_id"),
        "net_liquidation": account.get("net_liquidation"),
        "realized_pnl": round(realized, 2),
        "open_count": len(open_positions),
        "trades_today": len(todays),
        "positions": [
            {
                "strategy": p.get("strategy"),
                "entry_time": str(p.get("entry_time", ""))[11:19],
                "credit": p.get("credit"),
                "qty": p.get("quantity"),
                "strikes": "/".join(str(int(float(v))) for v in (p.get("strikes") or {}).values() if v is not None),
                "tp": p.get("tp_status"),
                "sl": p.get("sl_status"),
                "status": "closed" if p.get("closed") else "open",
                "pnl": p.get("close_pnl"),
            }
            for p in todays
        ],
        "broker_positions": router.get("positions", []),
        "open_orders": router.get("orders", []),
        "entries": entries[-20:][::-1],
        **parse_trader_log(tail_lines(log_path)),
    }


# ── Projects in motion ───────────────────────────────────────────────────────

async def _agent_orch_list() -> list[dict[str, Any]]:
    env = {**os.environ, "AGENT_ORCH_HOST_ID": os.environ.get("AGENT_ORCH_HOST_ID", "local")}
    proc = await asyncio.create_subprocess_exec(
        AGENT_ORCH, "list", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), 20)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    data = json.loads(stdout.decode() or "[]")
    return data if isinstance(data, list) else []


def _closed_recently(days: float = 3.0) -> list[dict[str, Any]]:
    if not SESSIONS_DB.exists():
        return []
    con = sqlite3.connect(f"file:{SESSIONS_DB}?mode=ro", uri=True, timeout=2)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT host, session_name, title, objective, status_card, closed_at, provider "
            "FROM sessions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 30"
        ).fetchall()
    finally:
        con.close()
    cutoff = time.time() - days * 86400
    out = []
    for row in rows:
        try:
            closed_ts = datetime.fromisoformat(str(row["closed_at"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if closed_ts < cutoff:
            continue
        out.append({
            "stream_id": f"{row['host']}:{row['session_name']}",
            "title": row["title"] or row["objective"],
            "status_card": row["status_card"],
            "closed_at": row["closed_at"],
            "provider": row["provider"],
        })
    return out


def _card(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def project_row(session: dict[str, Any], *, closed: bool = False) -> dict[str, Any]:
    card = _card(session.get("status_card"))
    plan = [step for step in card.get("plan") or [] if isinstance(step, dict)]
    updates = [u for u in card.get("updates") or [] if isinstance(u, dict)]
    return {
        "stream_id": session.get("stream_id"),
        "title": session.get("title") or session.get("display_name") or session.get("objective") or "Untitled",
        "goal": card.get("goal"),
        "update": card.get("update"),
        "updated_at": card.get("updated_at"),
        "eta": card.get("eta"),
        "plan": [{"text": s.get("text"), "status": s.get("status", "pending")} for s in plan],
        "plan_done": sum(1 for s in plan if s.get("status") == "done"),
        "plan_total": len(plan),
        "recent_updates": updates[-5:][::-1],
        "working": bool(session.get("working")) and not closed,
        "working_label": session.get("working_label"),
        "online": bool(session.get("online")) and not closed,
        "last_activity": session.get("last_activity") or session.get("closed_at"),
        "provider": session.get("provider"),
        "model": session.get("effective_model") or session.get("model"),
        "context_level": session.get("context_level"),
        "closed_at": session.get("closed_at") if closed else None,
        "state": "finished" if closed else ("working" if session.get("working") else "idle"),
    }


async def collect_projects(_session: aiohttp.ClientSession) -> dict[str, Any]:
    live = await _agent_orch_list()
    active = [project_row(s) for s in live if s.get("visibility", "default") != "hidden"]
    # Working sessions first, then most recently active.
    active.sort(key=lambda r: str(r["last_activity"] or ""), reverse=True)
    active.sort(key=lambda r: not r["working"])
    finished = [project_row(s, closed=True) for s in await asyncio.to_thread(_closed_recently)]
    return {
        "active": active,
        "finished": finished,
        "working_count": sum(1 for r in active if r["working"]),
        "active_count": len(active),
    }


# ── Finance engine (Lews Budget) ─────────────────────────────────────────────

async def collect_finance(session: aiohttp.ClientSession) -> dict[str, Any]:
    dash = await get_json(session, f"{LEWS_URL}/api/dashboard")
    month = datetime.now().strftime("%Y-%m")
    accounts = [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "institution": a.get("institution"),
            "type": a.get("type"),
            "balance": a.get("balance"),
            "balance_date": a.get("balance_date"),
            "status": a.get("status"),
        }
        for a in dash.get("accounts") or []
    ]
    dates = [a["balance_date"] for a in accounts if a.get("balance_date")]
    spending = sorted(dash.get("spendingByCategory") or [], key=lambda c: -abs(float(c.get("value") or 0)))
    return {
        "month": month,
        "net_worth": dash.get("netWorth"),
        "month_income": dash.get("monthIncome"),
        "month_expenses": dash.get("monthExpenses"),
        "latest_balance_date": max(dates) if dates else None,
        "accounts": sorted(accounts, key=lambda a: -abs(float(a.get("balance") or 0))),
        "spending_by_category": spending,
        "cash_flow": dash.get("cashFlow") or [],
        "net_worth_history": (dash.get("netWorthHistory") or [])[-30:],
    }


# ── Travel engine ────────────────────────────────────────────────────────────

async def collect_travel(session: aiohttp.ClientSession) -> dict[str, Any]:
    data = await get_json(session, f"{TRAVEL_URL}/api/balances")
    balances = []
    for program, info in (data.get("balances") or {}).items():
        updated = info.get("updated_at")
        balances.append({
            "program": program,
            "points": info.get("points"),
            "kind": info.get("kind"),
            "source": info.get("source"),
            "needs_login": bool(info.get("needs_login")),
            "updated_at": datetime.fromtimestamp(updated, timezone.utc).strftime("%Y-%m-%d") if isinstance(updated, (int, float)) else updated,
        })
    balances.sort(key=lambda b: -float(b.get("points") or 0))
    providers = [
        {"name": name, "status": info.get("status"), "error": info.get("error")}
        for name, info in (data.get("providers") or {}).items()
    ]
    return {
        "balances": balances,
        "total_points": sum(float(b.get("points") or 0) for b in balances),
        "providers": providers,
        "needs_login": [b["program"] for b in balances if b["needs_login"]],
    }


def slim_search(result: dict[str, Any], limit: int = 25) -> dict[str, Any]:
    """Cut a Travel Engine search snapshot down to what fits on a phone."""

    def legs_text(legs: list[dict[str, Any]]) -> str:
        parts = []
        for leg in legs or []:
            flight = f"{leg.get('carrier') or ''}{leg.get('flight_no') or ''}".strip()
            dep_t = (leg.get("dep_t_iso") or "")[11:16]
            arr_t = (leg.get("arr_t_iso") or "")[11:16]
            parts.append(f"{flight} {leg.get('dep')}→{leg.get('arr')} {dep_t}–{arr_t}".strip())
        return " · ".join(parts)

    def group(g: dict[str, Any]) -> dict[str, Any]:
        cash = g.get("best_cash") or {}
        pts = g.get("best_points") or {}
        return {
            "route": legs_text(g.get("legs") or []),
            "stops": g.get("stops"),
            "duration_min": g.get("duration_min"),
            "date": g.get("date"),
            "cash": cash.get("cash_total"),
            "cash_source": cash.get("source"),
            "points": pts.get("points"),
            "taxes": pts.get("taxes"),
            "program": pts.get("program"),
            "points_source": pts.get("source"),
            "hidden_city": bool(cash.get("hidden_city")),
        }

    return {
        "id": result.get("id"),
        "query": result.get("query"),
        "done": bool(result.get("done")),
        "sources": result.get("sources") or {},
        "dollars": [group(g) for g in (result.get("dollars") or [])[:limit]],
        "points": [group(g) for g in (result.get("points") or [])[:limit]],
    }


COLLECTORS = {
    # dashboard_id: (collector, refresh seconds, freshness ttl seconds)
    "sai.trading": (collect_trading, 15, 90),
    "sai.projects": (collect_projects, 10, 90),
    "sai.finance": (collect_finance, 300, 1800),
    "sai.travel": (collect_travel, 300, 1800),
}
