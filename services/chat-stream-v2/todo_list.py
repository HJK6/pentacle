"""One shared personal to-do list with a priority per item.

Operator-authenticated clients and live seats (`seat` and `nexus` kinds; every
seat is a facet of Bart) may read and mutate; service actors and unauthenticated
callers are refused with an `unauthorized` RPC result. After each committed
mutation the open items are broadcast as `todo.inventory`, which the server
withholds from every client that is not operator-authenticated.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import sqlite3
import uuid
from typing import Any, Awaitable, Callable

from sessions import VerbError


PRIORITIES = ("high", "normal", "low")
TEXT_MAX = 200
log = logging.getLogger("chat_streamd_v2.todo_list")

_COLUMNS = "item_id,text,priority,state,position,created_at,updated_at"
_ORDER = (
    "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, position"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _items(conn: sqlite3.Connection, *, include_done: bool) -> list[dict[str, Any]]:
    where = "" if include_done else "WHERE state='open' "
    return [dict(row) for row in conn.execute(
        f"SELECT {_COLUMNS} FROM v2_todo_items {where}{_ORDER}"
    ).fetchall()]


def _item(conn: sqlite3.Connection, item_id: Any) -> dict[str, Any]:
    row = None
    if isinstance(item_id, str):
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM v2_todo_items WHERE item_id=?", (item_id,),
        ).fetchone()
    if row is None:
        raise VerbError("not_found", "to-do item not found")
    return dict(row)


def _priority(value: Any) -> str:
    if not isinstance(value, str) or value not in PRIORITIES:
        raise VerbError("invalid_priority", "priority must be one of high, normal, low")
    return value


class TodoList:
    def __init__(
        self,
        store: Any,
        sessions: Any,
        *,
        broadcast: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.broadcast = broadcast

    def wire_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]:
        return {
            "todo.list": self.todo_list,
            "todo.add": self.todo_add,
            "todo.set": self.todo_set,
            "todo.check": self.todo_check,
            "todo.remove": self.todo_remove,
        }

    def _require_store(self) -> None:
        if getattr(self.store, "schedule_schema_health", None) != "ok":
            raise VerbError("store_unavailable", "to-do schema is not healthy")

    async def _actor(self, msg: dict[str, Any]) -> tuple[str, str]:
        """Return `(kind, id)` for an allowed caller, else raise `unauthorized`."""
        self._require_store()
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        if auth.get("operator_authenticated"):
            return "operator", str(auth.get("operator_principal") or "operator")
        denied = VerbError("unauthorized", "to-do access requires an operator or a live seat")
        if auth.get("service_authenticated") or not auth.get("token_verified"):
            raise denied
        actor = str(auth.get("stream_id") or "").strip()
        if not actor or str(msg.get("from_stream_id") or "").strip() != actor:
            raise denied
        row = self.sessions.get(actor)
        if row is None and ":" in actor:
            host, name = actor.split(":", 1)
            row = await self.store.fetch_session(host, name)
        if row is None or str(row.get("status") or "open") != "open":
            raise denied
        return ("nexus" if row.get("role") == "nexus" else "seat"), actor

    async def _broadcast_inventory(self) -> None:
        if self.broadcast is None:
            return
        try:
            items = await self.store.submit(lambda conn: _items(conn, include_done=False))
            await self.broadcast({"type": "todo.inventory", "items": items})
        except Exception:  # a committed mutation must not become a false RPC failure
            log.exception("todo inventory broadcast failed")

    async def todo_list(self, msg: dict[str, Any]) -> dict[str, Any]:
        await self._actor(msg)
        include_done = msg.get("include_done") is True
        items = await self.store.submit(lambda conn: _items(conn, include_done=include_done))
        return {"type": "todo.list.ok", "items": items}

    async def todo_add(self, msg: dict[str, Any]) -> dict[str, Any]:
        await self._actor(msg)
        raw = msg.get("text")
        text = raw.strip() if isinstance(raw, str) else ""
        if not 1 <= len(text) <= TEXT_MAX:
            raise VerbError("invalid_text", f"text must be 1-{TEXT_MAX} characters")
        priority = "normal" if msg.get("priority") is None else _priority(msg.get("priority"))

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            folded = text.casefold()
            for (existing,) in conn.execute("SELECT text FROM v2_todo_items WHERE state='open'"):
                if existing.strip().casefold() == folded:
                    raise VerbError("duplicate", "an open item with this text already exists")
            now = _now()
            item_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO v2_todo_items (item_id,text,priority,state,position,created_at,updated_at) "
                "VALUES (?,?,?,'open',(SELECT COALESCE(MAX(position),0)+1 FROM v2_todo_items),?,?)",
                (item_id, text, priority, now, now),
            )
            conn.commit()
            return _item(conn, item_id)

        item = await self.store.submit(op)
        await self._broadcast_inventory()
        return {"type": "todo.add.ok", "item": item}

    async def todo_set(self, msg: dict[str, Any]) -> dict[str, Any]:
        await self._actor(msg)
        if msg.get("priority") is None:
            raise VerbError("invalid_update", "todo.set requires a priority")
        priority = _priority(msg.get("priority"))

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            item = _item(conn, msg.get("item_id"))
            conn.execute(
                "UPDATE v2_todo_items SET priority=?, updated_at=? WHERE item_id=?",
                (priority, _now(), item["item_id"]),
            )
            conn.commit()
            return _item(conn, item["item_id"])

        item = await self.store.submit(op)
        await self._broadcast_inventory()
        return {"type": "todo.set.ok", "item": item}

    async def todo_check(self, msg: dict[str, Any]) -> dict[str, Any]:
        await self._actor(msg)

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            item = _item(conn, msg.get("item_id"))
            if item["state"] == "done":
                return item
            conn.execute(
                "UPDATE v2_todo_items SET state='done', updated_at=? WHERE item_id=?",
                (_now(), item["item_id"]),
            )
            conn.commit()
            return _item(conn, item["item_id"])

        item = await self.store.submit(op)
        await self._broadcast_inventory()
        return {"type": "todo.check.ok", "item": item}

    async def todo_remove(self, msg: dict[str, Any]) -> dict[str, Any]:
        await self._actor(msg)

        def op(conn: sqlite3.Connection) -> str:
            item = _item(conn, msg.get("item_id"))
            conn.execute("DELETE FROM v2_todo_items WHERE item_id=?", (item["item_id"],))
            conn.commit()
            return item["item_id"]

        item_id = await self.store.submit(op)
        await self._broadcast_inventory()
        return {"type": "todo.remove.ok", "item_id": item_id}
