"""Operator-only adapter from Pentacle Personal to the Cosmo household store.

Cosmo (https://github.com/HJK6/cosmo) is the one authoritative store for the
operator's lists and calendar; this module keeps no household state. Every call
uses Cosmo's optional ``pentacle`` credential, which acts for Vamshi: Cosmo's own
audience projection decides visibility (``vamshi`` + ``shared``), and because no
``scope`` is ever sent, rows created here are Vamshi-private.

Authorization is the server-injected ``_auth_context["operator_authenticated"]``
flag only (``server.py`` strips client-supplied ``_`` fields before dispatch).
Seats, Nexus seats, service producers, scoped credentials and unauthenticated
callers are refused before any Cosmo call. Each verb accepts a fixed field list.

The bearer token is read from a 0600 file at call time and appears only in the
outgoing ``Authorization`` header: never in a frame, error text or log line.
Calls are never retried; a mutation whose outcome cannot be known (timeout or a
connection lost after sending) is reported as ``unknown_outcome`` so the client
reads back instead of resubmitting.

Spec: spec_pentacle_mobile__personal_screens_2026_10 § B2.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sessions import VerbError

COSMO_URL_ENV = "PENTACLE_COSMO_URL"
COSMO_URL_DEFAULT = "https://thoths-mac-mini.tail1c7370.ts.net:8443"
TOKEN_FILE_ENV = "COSMO_PENTACLE_TOKEN_FILE"
TOKEN_FILE_DEFAULT = "~/.cosmo/pentacle.token"

LISTS = ("tasks", "grocery", "meals", "chores", "study")
WHO = ("me", "vamshi", "both")
CHICAGO = ZoneInfo("America/Chicago")
CALL_TIMEOUT = 5.0
SNAPSHOT_DEADLINE = 8.0
MAX_BODY = 1 << 20
_ENVELOPE = frozenset({"type", "request_id"})
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH = re.compile(r"(\d{4})-(\d{2})")
_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class _CosmoFailure(Exception):
    """Transport-level outcome. ``sent`` is False only when nothing reached Cosmo."""

    def __init__(self, sent: bool) -> None:
        super().__init__("cosmo call failed")
        self.sent = sent


class Household:
    def __init__(self, *, url: str | None = None, token_file: str | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.url = (url or os.environ.get(COSMO_URL_ENV) or COSMO_URL_DEFAULT).rstrip("/")
        self.token_file = token_file or os.environ.get(TOKEN_FILE_ENV) or TOKEN_FILE_DEFAULT
        self.clock = clock
        self._ssl = ssl.create_default_context()

    def wire_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]:
        return {
            "household.snapshot": self.snapshot,
            "household.item.add": self.item_add,
            "household.item.done": self.item_done,
            "household.item.remove": self.item_remove,
            "household.event.add": self.event_add,
            "household.event.remove": self.event_remove,
        }

    # ---- verbs -------------------------------------------------------------------------

    async def snapshot(self, msg: dict[str, Any]) -> dict[str, Any]:
        fields = _fields(msg, required=(), optional=("month",))
        today = datetime.fromtimestamp(self.clock(), CHICAGO).date()
        first = _month(fields["month"]) if "month" in fields else today.replace(day=1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        token = self._token()
        calls = [("GET", f"/lists/{name}/items", None) for name in LISTS] + [
            ("GET", "/events?" + urlencode({"from": first.isoformat(), "to": last.isoformat()}), None),
            ("GET", "/events?" + urlencode({"from": today.isoformat(),
                                            "to": (today + timedelta(days=7)).isoformat()}), None),
        ]
        try:
            replies = await asyncio.wait_for(
                asyncio.gather(*(asyncio.to_thread(self._call, token, *c) for c in calls)),
                SNAPSHOT_DEADLINE,
            )
        except (asyncio.TimeoutError, _CosmoFailure):
            raise VerbError("unavailable", "household store unavailable") from None
        bodies = []
        for status, body in replies:
            if status != 200 or not isinstance(body, dict) or not isinstance(body.get("items"), list):
                raise VerbError("unavailable", "household store unavailable")
            bodies.append(body)
        lists = {name: [row for row in bodies[i]["items"] if row.get("done_at") is None]
                 for i, name in enumerate(LISTS)}
        events = {row["id"]: row for body in bodies[len(LISTS):] for row in body["items"]}
        ordered = sorted(events.values(), key=lambda e: (e.get("date") or "", e.get("time") or "", e["id"]))
        return {"type": "household.snapshot.ok", "today": today.isoformat(), "month": first.strftime("%Y-%m"),
                "lists": lists, "events": ordered, "server_now": bodies[len(LISTS)].get("server_now")}

    async def item_add(self, msg: dict[str, Any]) -> dict[str, Any]:
        fields = _fields(msg, required=("list", "label"))
        if fields["list"] not in LISTS:
            raise VerbError("invalid_request", "unknown list")
        label = _text(fields["label"], 1000)
        body = await self._mutate("POST", f"/lists/{fields['list']}/items", {"label": label}, expect=201)
        return {"type": "household.item.add.ok", "item": _row(body), "server_now": body.get("server_now")}

    async def item_done(self, msg: dict[str, Any]) -> dict[str, Any]:
        ident = _ident(_fields(msg, required=("item_id",))["item_id"])
        body = await self._mutate("PATCH", f"/lists/items/{ident}/done", None, expect=200)
        return {"type": "household.item.done.ok", "item": _row(body), "server_now": body.get("server_now")}

    async def item_remove(self, msg: dict[str, Any]) -> dict[str, Any]:
        ident = _ident(_fields(msg, required=("item_id",))["item_id"])
        await self._mutate("DELETE", f"/lists/items/{ident}", None, expect=204)
        return {"type": "household.item.remove.ok", "item_id": ident}

    async def event_add(self, msg: dict[str, Any]) -> dict[str, Any]:
        fields = _fields(msg, required=("date", "time", "title", "who"))
        day = fields["date"]
        if not isinstance(day, str) or not _DAY.fullmatch(day):
            raise VerbError("invalid_request", "date must be YYYY-MM-DD")
        try:
            date.fromisoformat(day)
        except ValueError:
            raise VerbError("invalid_request", "date must be YYYY-MM-DD") from None
        clock = fields["time"]
        if clock is not None and (not isinstance(clock, str) or not _TIME.fullmatch(clock)):
            raise VerbError("invalid_request", "time must be HH:MM or null")
        if fields["who"] not in WHO:
            raise VerbError("invalid_request", "who must be me, vamshi or both")
        payload = {"date": day, "time": clock, "title": _text(fields["title"], 500), "who": fields["who"]}
        body = await self._mutate("POST", "/events", payload, expect=201)
        return {"type": "household.event.add.ok", "event": _row(body), "server_now": body.get("server_now")}

    async def event_remove(self, msg: dict[str, Any]) -> dict[str, Any]:
        ident = _ident(_fields(msg, required=("event_id",))["event_id"])
        await self._mutate("DELETE", f"/events/{ident}", None, expect=204)
        return {"type": "household.event.remove.ok", "event_id": ident}

    # ---- transport ---------------------------------------------------------------------

    def _token(self) -> str:
        try:
            token = Path(self.token_file).expanduser().read_text().strip()
        except OSError:
            token = ""
        if not token:
            raise VerbError("unavailable", "household store not configured")
        return token

    async def _mutate(self, method: str, path: str, payload: dict[str, Any] | None, *, expect: int) -> dict[str, Any]:
        token = self._token()
        try:
            status, body = await asyncio.to_thread(self._call, token, method, path, payload)
        except _CosmoFailure as failure:
            if failure.sent:
                raise VerbError("unknown_outcome", "household store did not confirm the change") from None
            raise VerbError("unavailable", "household store unavailable") from None
        if status == expect:
            if expect == 204:
                return {}
            if isinstance(body, dict):
                return body
            raise VerbError("unknown_outcome", "household store did not confirm the change")
        code = {404: "not_found", 410: "not_found", 400: "invalid_request", 422: "invalid_request",
                403: "forbidden", 401: "unavailable"}.get(status, "unknown_outcome")
        raise VerbError(code, f"household store refused the change ({code})")

    def _call(self, token: str, method: str, path: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        """One blocking HTTP call. Cosmo response text is parsed, never surfaced."""
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        })
        context = self._ssl if self.url.startswith("https:") else None
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT, context=context) as response:
                status, raw = response.status, response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as error:
            status, raw = error.code, b""
        except urllib.error.URLError:
            # urllib wraps only connect/send failures: the request never reached Cosmo whole.
            raise _CosmoFailure(sent=False) from None
        except (TimeoutError, socket.timeout, ConnectionError, OSError, ValueError):
            # Waiting for the response: the request may have been processed.
            raise _CosmoFailure(sent=True) from None
        if len(raw) > MAX_BODY:
            raise _CosmoFailure(sent=True)
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, None


# ---- validation helpers -------------------------------------------------------------------

def _fields(msg: dict[str, Any], *, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, Any]:
    auth = msg.get("_auth_context")
    if not isinstance(auth, dict) or auth.get("operator_authenticated") is not True:
        raise VerbError("unauthorized", "operator authentication required")
    given = {k: v for k, v in msg.items() if not str(k).startswith("_") and k not in _ENVELOPE}
    if set(given) - set(required) - set(optional) or set(required) - set(given):
        raise VerbError("invalid_request", "unexpected or missing fields")
    return given


def _month(value: Any) -> date:
    match = _MONTH.fullmatch(value) if isinstance(value, str) else None
    if not match or not 2000 <= int(match[1]) <= 2100 or not 1 <= int(match[2]) <= 12:
        raise VerbError("invalid_range", "month must be YYYY-MM between 2000-01 and 2100-12")
    return date(int(match[1]), int(match[2]), 1)


def _text(value: Any, limit: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not 1 <= len(text) <= limit:
        raise VerbError("invalid_request", "text must be non-empty and within the limit")
    return text


def _ident(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise VerbError("invalid_request", "id must be a positive integer")
    return value


def _row(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k != "server_now"}
