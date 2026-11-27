"""Short, durable advisor attempts. The ruling row remains the authority."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from assistant_restore import SERVICE_ACTOR
from store_assistant_binding import _seat_conn
from store_watch_wake import coalesce_notice_conn

log = logging.getLogger(__name__)
KINDS_KEY = "assistant.authority.session_kinds"
SHAPE_KEY = "assistant.authority.session_shape"
DDL = """
CREATE TABLE IF NOT EXISTS v2_advisor_sessions (
    ruling_request_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    stream_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    guidelines_identity TEXT NOT NULL,
    started_at REAL NOT NULL,
    ready_at REAL,
    ended_at REAL,
    end_reason TEXT,
    failed INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    PRIMARY KEY(ruling_request_id,attempt),
    UNIQUE(stream_id,generation)
)
"""
RULES = """Review this one request only. Skip shared startup reading, title and status card.
Answer advice with its answer_command. For any other action, use
agent-orch assistant ruling --ruling-request-id <ruling_request_id> --request-id <key>
--ruling <approve|revise|deny> [--reason <reason>] [--conditions <conditions>].
Send no tell except a DELEGATED-HOLD
line to the acting seat when granting such a hold, before answering.
Wait for a running command in one wait of at least 60 seconds. After answering, stop.
"""


def json_record(conn, key, default):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return json.loads(row[0]) if row and row[0] else default


def owns_conn(conn, row):
    return bool(row and conn.execute(
        "SELECT 1 FROM v2_advisor_sessions WHERE ruling_request_id=? AND stream_id=? "
        "AND generation=? AND ended_at IS NULL",
        (row["ruling_request_id"], row["authority_stream_id"], row["authority_generation"])).fetchone())


def cut(text, size):
    return str(text).encode("utf-8")[:size].decode("utf-8", errors="ignore")


class AdvisorSessions:
    def __init__(self, rulings):
        self.rulings, self.store, self.server = rulings, rulings.store, rulings.server
        self.lock = asyncio.Lock()
        self.tasks = {}

    async def owns(self, row):
        return await self.store.submit(lambda c: owns_conn(c, row))

    async def status(self):
        def read(c):
            kinds = json_record(c, KINDS_KEY, [])
            running = c.execute("SELECT count(*) FROM v2_advisor_sessions WHERE ended_at IS NULL").fetchone()[0]
            waiting = sum(r["action"] in kinds for r in c.execute(
                "SELECT action FROM v2_assistant_lane_rulings WHERE state='pending' AND authority_stream_id=''"))
            return {"session_kinds": kinds, "session_shape": json_record(c, SHAPE_KEY, None),
                    "running_sessions": running, "waiting_sessions": waiting}
        return await self.store.submit(read)

    async def thread(self, request):
        from assistant_lane_rulings import _TERMINAL, _canonical
        if not request["work_item"]:
            return ""
        def read(c):
            return [dict(r) for r in c.execute(
                "SELECT * FROM v2_assistant_lane_rulings WHERE work_item=? AND ruling_request_id!=? "
                "AND state IN (" + ",".join("?" for _ in _TERMINAL) + ") ORDER BY created_at DESC,rowid DESC LIMIT 8",
                (request["work_item"], request["ruling_request_id"], *sorted(_TERMINAL)))]
        entries, size = [], 0
        for row in await self.store.submit(read):
            outcome = ({"answer": cut(row["answer"] or "", 1500)} if row["action"] == "advice" else
                       {k: row[k] for k in ("state", "ruling", "reason", "conditions")})
            entry = _canonical({"time": row["created_at"], "action": row["action"],
                                "request": cut(self.rulings.packet(row), 1500), "outcome": outcome})
            if size + len(entry.encode("utf-8")) + 1 > 12000:
                break
            entries.append(entry)
            size += len(entry.encode("utf-8")) + 1
        return "\n".join(reversed(entries))

    async def brief(self, request):
        from assistant_guidelines import read_set, DEFAULT_ADVISOR
        guidelines = getattr(getattr(self.server, "comms", None), "guidelines", None)
        loaded = guidelines.load("advisor") if guidelines else read_set(DEFAULT_ADVISOR)
        if loaded is None:
            raise ValueError("advisor_guidelines_unavailable")
        identity, text = loaded
        packet = self.rulings.packet(request)
        return identity, RULES + "\n" + text + "\n[Thread record]\n" + await self.thread(request) + "\n" + packet

    async def _spawn(self, record):
        payload = json.loads(record["payload"])
        try:
            await self.server.spawnctl.spawn(payload, self.server.local_host)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The spawn journal, not a lost RPC response, decides failure.
            log.exception("advisor spawn needs reconciliation request=%s", record["ruling_request_id"])

    async def _start(self, request, attempt):
        from assistant_lane_rulings import _canonical
        now = time.time()
        shape = await self.store.submit(lambda c: json_record(c, SHAPE_KEY, {}))
        generation = uuid.uuid4().hex
        name = "advisor-" + uuid.uuid4().hex[:16]
        stream = self.server.local_host + ":" + name
        key = f"advisor-session:{request['ruling_request_id']}:{attempt}"
        identity, brief = "", ""
        error = None
        try:
            identity, brief = await self.brief(request)
            if not shape.get("provider") or not shape.get("model") or not shape.get("effort"):
                raise ValueError("advisor_session_shape_missing")
        except ValueError as exc:
            error = str(exc)
        composite = self.rulings._composite()
        payload = {"type": "spawn", "request_id": key, "idempotency_key": key,
                   "_auth_context": {"service_authenticated": True, "service_actor": SERVICE_ACTOR},
                   "_session_generation": generation, "_advisor_session": True,
                   "host": self.server.local_host, "session_name": name,
                   "provider": shape.get("provider"), "model": shape.get("model"), "effort": shape.get("effort"),
                   "visibility": "hidden", "no_watch": True,
                   "spec_id": request["work_item"] or None,
                   "parent_stream_id": composite.config.direct_primary_stream_id,
                   "initial_prompt": brief, "self_close_on_completion": False,
                   "objective": "Answer one advisor request"}
        if shape.get("account"):
            payload["account"] = shape["account"]
        record = dict(ruling_request_id=request["ruling_request_id"], attempt=attempt, stream_id=stream,
                      generation=generation, guidelines_identity=identity, started_at=now, ready_at=None,
                      ended_at=None, end_reason=error, failed=int(bool(error)), payload=_canonical(payload))
        def reserve(c):
            with c:
                current = c.execute("SELECT * FROM v2_assistant_lane_rulings WHERE ruling_request_id=?",
                                    (request["ruling_request_id"],)).fetchone()
                if not current or current["state"] != "pending" or current["authority_stream_id"]:
                    return False
                c.execute("INSERT INTO v2_advisor_sessions (" + ",".join(record) + ") VALUES ("
                          + ",".join("?" for _ in record) + ")", tuple(record.values()))
                c.execute("UPDATE v2_assistant_lane_rulings SET authority_stream_id=?,authority_generation=? "
                          "WHERE ruling_request_id=?", (stream, generation, request["ruling_request_id"]))
                return True
        if await self.store.submit(reserve) and not error:
            self.tasks[key] = asyncio.create_task(self._spawn(record), name=key)

    async def finish(self, record, reason, *, failed=False):
        """Do not free a slot or lose custody until generation-fenced cleanup succeeds."""
        rid, attempt = record["ruling_request_id"], record["attempt"]
        key = f"advisor-session:{rid}:{attempt}"
        def mark(c):
            with c:
                c.execute("UPDATE v2_advisor_sessions SET end_reason=?,failed=? "
                          "WHERE ruling_request_id=? AND attempt=? AND ended_at IS NULL",
                          (reason, int(failed), rid, attempt))
        await self.store.submit(mark)
        task = self.tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        cancel = getattr(self.server.spawnctl, "spawn_cancel", None)
        if cancel:
            await cancel({"target": key, "host": self.server.local_host}, self.server.local_host)
            status = await self.server.spawnctl.spawn_status({"target": key, "host": self.server.local_host}, self.server.local_host)
            if status.get("reservations"):
                return False
        host, name = record["stream_id"].split(":", 1)
        seat = await self.store.fetch_session(host, name)
        if seat and seat.get("status") == "open" and seat.get("session_generation") == record["generation"]:
            result = await self.server.sessions.close(host, name, reason="Advisor request: " + reason,
                expected_generation=record["generation"], operator_confirm=True)
            if any(result.get(k) for k in ("failed", "deferred", "stale_generation")):
                return False
        def complete(c):
            with c:
                c.execute("UPDATE v2_advisor_sessions SET ended_at=? WHERE ruling_request_id=? AND attempt=?",
                          (time.time(), rid, attempt))
                c.execute("UPDATE v2_assistant_lane_rulings SET authority_stream_id='',authority_generation='' "
                          "WHERE ruling_request_id=? AND authority_stream_id=? AND authority_generation=? AND state='pending'",
                          (rid, record["stream_id"], record["generation"]))
        await self.store.submit(complete)
        self.rulings._wake.set()
        return True

    async def close_request(self, rid, reason):
        records = await self.store.submit(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM v2_advisor_sessions WHERE ruling_request_id=? AND ended_at IS NULL", (rid,))])
        for record in records:
            if not await self.finish(record, reason):
                return False
        return True

    async def _reconcile(self, record, now):
        from assistant_lane_rulings import _TERMINAL
        rid, attempt = record["ruling_request_id"], record["attempt"]
        request = await self.rulings._fetch(rid)
        if record["end_reason"]:
            await self.finish(record, record["end_reason"], failed=bool(record["failed"]))
            return
        if not request or request["state"] in _TERMINAL:
            await self.finish(record, "terminal")
            return
        if request["state"] != "pending":
            return  # An accepted ruling may still be releasing its action.
        if not request["covered"] and request["deadline"] <= now:
            await self.rulings._expire(rid, "deadline")
            await self.finish(record, "deadline")
            return
        host, name = record["stream_id"].split(":", 1)
        seat = await self.store.fetch_session(host, name)
        if seat and (seat.get("session_generation") != record["generation"] or
                     self.rulings._authority_loss_evidence(seat)):
            await self.finish(record, "seat_lost", failed=True)
            return
        if record["ready_at"] is not None:
            if not seat or now - record["ready_at"] >= 300:
                await self.finish(record, "answer_timeout" if seat else "seat_lost", failed=True)
            return
        key = f"advisor-session:{rid}:{attempt}"
        status = await self.server.spawnctl.spawn_status({"target": key, "host": host}, self.server.local_host)
        outcomes = [o for o in status.get("outcomes", []) if key in (o.get("request_id"), o.get("idempotency_key"))]
        reservations = status.get("reservations", [])
        delivered = any(o.get("state") == "delivered" and o.get("stream_id") == record["stream_id"] for o in outcomes)
        # The spawn journal's delivered outcome is the boot/delivery proof used
        # by await_spawn; the session's bootstrap projection can lag it.
        if delivered and seat and seat.get("pane_status") == "pane_alive":
            def ready(c):
                with c:
                    c.execute("UPDATE v2_advisor_sessions SET ready_at=? "
                              "WHERE ruling_request_id=? AND attempt=? AND ready_at IS NULL", (now, rid, attempt))
            await self.store.submit(ready)
        elif any(o.get("state") in {"failed", "cancelled"} for o in outcomes) and not reservations:
            await self.finish(record, "spawn_refused", failed=True)
        elif not outcomes and not reservations and not seat:
            task = self.tasks.get(key)
            if task is None:
                self.tasks[key] = asyncio.create_task(self._spawn(record), name=key)
            elif task.done():
                await self.finish(record, "spawn_refused", failed=True)

    async def failed_notice(self, request):
        from assistant_lane_rulings import _canonical
        rid = request["ruling_request_id"]
        composite = self.rulings._composite()
        attempts = await self.store.submit(lambda c: [dict(r) for r in c.execute(
            "SELECT attempt,stream_id,end_reason FROM v2_advisor_sessions WHERE ruling_request_id=? AND failed=1 ORDER BY attempt LIMIT 2", (rid,))])
        recipients = {(request["requester_stream_id"], request["requester_generation"]),
                      (composite.config.direct_primary_stream_id, composite.config.direct_primary_generation)}
        for stream, generation in recipients:
            notice_id = f"advisor-session-failed:{rid}:{stream}"
            await self.store.enqueue_outbound_notice(notice_id=notice_id, kind="assistant_lane_ruling_result",
                dedupe_key=notice_id, recipient_stream_id=stream, tell_id=notice_id, source_stream_id="",
                body="[Advisor session failed] " + _canonical({"ruling_request_id": rid, "attempts": attempts}),
                metadata={"authority_generation": generation})

    async def tick(self) -> bool:
        async with self.lock:
            now = time.time()
            records = await self.store.submit(lambda c: [dict(r) for r in c.execute(
                "SELECT * FROM v2_advisor_sessions WHERE ended_at IS NULL")])
            for record in records:
                await self._reconcile(record, now)
            async with self.server.sessions.assistant.authority_lock, self.rulings._authority_lock:
                binding = await self.rulings.binding()
                if binding["state"] == "disabled":
                    return bool(records)
                # Commit holds this authority lock through binding publication.
                # Cleanup may retry later; it must not keep new requests paused.
                if await self.store.submit(lambda c: bool(c.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_host_transfers'").fetchone()) and
                        bool(c.execute("SELECT 1 FROM v2_host_transfers WHERE state='preparing' LIMIT 1").fetchone())):
                    return bool(records)
                kinds = await self.store.submit(lambda c: json_record(c, KINDS_KEY, []))
                active = await self.store.submit(lambda c: [dict(r) for r in c.execute(
                    "SELECT r.work_item FROM v2_advisor_sessions s JOIN v2_assistant_lane_rulings r "
                    "ON s.ruling_request_id=r.ruling_request_id WHERE s.ended_at IS NULL")])
                slots = 4 - len(active)
                busy = {r["work_item"] for r in active if r["work_item"]}
                rows = await self.store.submit(lambda c: [dict(r) for r in c.execute(
                    "SELECT * FROM v2_assistant_lane_rulings WHERE state IN "
                    "('pending','approved','unruled','unruled_disabled','bypassed') ORDER BY created_at,rowid")])
                candidates, seen = [], set(busy)
                for row in rows:
                    item = row["work_item"]
                    if item and item in seen:
                        continue
                    if item:
                        seen.add(item)
                    if row["state"] == "pending" and row["action"] in kinds and not row["authority_stream_id"]:
                        candidates.append(row)
                for row in sorted(candidates, key=lambda r: bool(r["covered"])):
                    if slots <= 0:
                        break
                    rid = row["ruling_request_id"]
                    history = await self.store.submit(lambda c: [dict(r) for r in c.execute(
                        "SELECT * FROM v2_advisor_sessions WHERE ruling_request_id=? ORDER BY attempt", (rid,))])
                    failures = [r for r in history if r["failed"]]
                    if not row["covered"] and (row["deadline"] <= now or failures and (len(failures) >= 2 or row["deadline"] - now < 300)):
                        await self.rulings._expire(rid, "deadline" if row["deadline"] <= now else "authority_unavailable")
                        continue
                    if row["covered"] and len(failures) >= 2:
                        await self.failed_notice(row)
                        if now - failures[-1]["ended_at"] < 900:
                            continue
                    await self._start(row, 1 + (history[-1]["attempt"] if history else 0))
                    slots -= 1
                return bool(records)

    async def stop(self):
        tasks, self.tasks = list(self.tasks.values()), {}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
