"""Generation-owned subscriptions; all mutations use the Store SQLite worker.

The existing outbox remains the only delivery state machine. Facts associate
subscriptions with immutable notice identities, including legacy report/death
notices whose payloads must never be rewritten by a later subscriber.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from datetime import datetime, timezone

from store_routing import _insert_outbound_notice_conn


WATCH_WAKE_DDL = (
    """CREATE TABLE IF NOT EXISTS v2_watch_wake (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
        owner_generation TEXT NOT NULL, child TEXT, child_generation TEXT,
        request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
        data TEXT NOT NULL, UNIQUE(owner, owner_generation, request_id))""",
    """CREATE TABLE IF NOT EXISTS v2_watch_facts (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, owner_generation TEXT NOT NULL,
        child TEXT, child_generation TEXT, kind TEXT NOT NULL, source TEXT NOT NULL,
        notice_id TEXT NOT NULL, trigger_at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS v2_watch_active ON v2_watch_wake(state, owner, child)",
    """CREATE TABLE IF NOT EXISTS v2_watch_pairs (
        child TEXT PRIMARY KEY, child_generation TEXT NOT NULL,
        parent TEXT, parent_generation TEXT, epoch INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS v2_watch_cancellations (
        owner TEXT NOT NULL, owner_generation TEXT NOT NULL, request_id TEXT NOT NULL,
        kind TEXT NOT NULL, target TEXT NOT NULL, PRIMARY KEY(owner, owner_generation, request_id))""",
)
D2_KINDS = ("watch", "wake", "wake_urgent")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _id(*values):
    return "d2:" + hashlib.sha256(_json(values).encode()).hexdigest()


def _stamp(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def _session(conn, sid):
    host, _, name = str(sid or "").partition(":")
    row = conn.execute("""SELECT s.*, g.generation AS session_generation FROM sessions s
        JOIN v2_session_generations g USING(host, session_name)
        WHERE s.host=? AND s.session_name=?""", (host, name)).fetchone()
    return dict(row) if row else None


def _live(conn, sid, generation):
    row = _session(conn, sid)
    return row if row and row["status"] == "open" and row["session_generation"] == generation else None


def _decode(row):
    data = json.loads(row["data"])
    due = []
    if row["state"] == "active":
        if row["kind"] == "wake":
            due.append(data["due_at"])
        else:
            for trigger in data["triggers"]:
                if trigger in data["consumed"]:
                    continue
                if trigger == "idle" and data["idle_since"] is not None:
                    due.append(data["idle_since"] + 900)
                elif trigger.startswith("quiet="):
                    due.append(data["quiet_since"] + int(trigger.split("=")[1]) * 60)
    return {**data, "next_due_at": _stamp(min(due)) if due else None,
            **{k: row[k] for k in ("id", "kind", "state", "owner_generation", "child_generation")}}


def _save(conn, row, data, state=None):
    conn.execute("UPDATE v2_watch_wake SET data=?, state=? WHERE id=?",
                 (_json(data), state or row["state"], row["id"]))


def _consume(conn, row, trigger, notice_id, episode):
    data = json.loads(row["data"])
    data["consumed"][trigger] = {"notice_id": notice_id, "episode": episode}
    data["notice_id"] = notice_id
    finished = all(t in data["consumed"] for t in data["triggers"])
    state = "consumed" if finished and not data["repeat"] else "active"
    if trigger == "end":
        state = "consumed"
        data["retired_reason"] = "terminal_end"
    _save(conn, row, data, state)


def _fact(conn, *, owner, owner_generation, child, child_generation, kind, source, notice_id, now):
    key = _id(owner, owner_generation, child, child_generation, kind, source)
    conn.execute("INSERT OR IGNORE INTO v2_watch_facts VALUES (?,?,?,?,?,?,?,?,?)",
        (key, owner, owner_generation, child, child_generation, kind, str(source), notice_id, now))
    return key


def _fact_notice(conn, owner, owner_generation, child, child_generation, kind, source=None):
    sql = """SELECT n.* FROM v2_watch_facts f JOIN v2_outbound_notices n USING(notice_id)
        WHERE f.owner=? AND f.owner_generation=? AND f.child IS ? AND f.child_generation IS ? AND f.kind=?"""
    args = [owner, owner_generation, child, child_generation, kind]
    if source is not None:
        sql += " AND f.source=?"
        args.append(str(source))
    row = conn.execute(sql + " ORDER BY f.trigger_at LIMIT 1", args).fetchone()
    return {**dict(row), "created": False} if row else None


def coalesce_notice_conn(conn, notice, report=None):
    """Insert/find and consume in one caller-owned transaction, even on replay."""
    owner = notice["recipient_stream_id"]
    child = notice.get("source_stream_id")
    bound = conn.execute("SELECT 1 FROM v2_watch_facts WHERE notice_id=?", (notice["notice_id"],)).fetchone()
    if bound:
        return _insert_outbound_notice_conn(conn, **notice)
    parent_row, child_row = _session(conn, owner), _session(conn, child)
    trigger = None
    if report:
        trigger = {"done": "end", "aborted": "end", "error": "blocker"}.get(report.get("status"))
    elif notice["kind"] == "reconciler":
        trigger = "end"
    if not parent_row or not child_row or not trigger:
        return _insert_outbound_notice_conn(conn, **notice)
    owner_gen, child_gen = parent_row["session_generation"], child_row["session_generation"]
    if report and report.get("session_generation") not in (None, "", child_gen):
        # Old-generation replay may find its existing identity, but cannot bind
        # new subscriptions or address a replacement parent.
        prior = conn.execute("SELECT * FROM v2_outbound_notices WHERE notice_id=?", (notice["notice_id"],)).fetchone()
        if prior:
            return {**dict(prior), "created": False}
        result = _insert_outbound_notice_conn(conn, **notice)
        conn.execute("UPDATE v2_outbound_notices SET terminal_at=?, terminal_reason=? WHERE notice_id=?",
                     (_stamp(time.time()), "stale_report_generation", result["notice_id"]))
        return result
    rows = conn.execute("""SELECT * FROM v2_watch_wake WHERE kind='watch'
        AND owner=? AND owner_generation=? AND child=? AND child_generation=?""",
        (owner, owner_gen, child, child_gen)).fetchall()
    prior = _fact_notice(conn, owner, owner_gen, child, child_gen, "end") if trigger == "end" else None
    if prior is not None:
        # One terminal fact per generation: a later report id cannot replay
        # that fact into subscriptions registered after it was announced.
        return prior
    result = _insert_outbound_notice_conn(conn, **notice)
    source = str((report or {}).get("report_id") or notice.get("episode_id") or notice["notice_id"])
    _fact(conn, owner=owner, owner_generation=owner_gen, child=child, child_generation=child_gen,
          kind=trigger, source=source, notice_id=result["notice_id"], now=(report or {}).get("created_at") or time.time())
    for row in rows:
        data = json.loads(row["data"])
        if row["state"] != "active" or trigger not in data["triggers"] or trigger in data["consumed"]:
            continue
        if report and int(report.get("ledger_row_id") or 0) <= data["watermark"]:
            continue
        _consume(conn, row, trigger, result["notice_id"], source)
    return result


def report_watch_conn(conn, report):
    """Accepted-report hook; called after D1's append, before the shared commit."""
    owner, child = report.get("to_stream_id"), report["from_stream_id"]
    if not owner:
        return False
    if report["status"] == "progress":
        rows = conn.execute("""SELECT * FROM v2_watch_wake WHERE owner=? AND child=?
            AND child_generation=? AND state='active' AND kind='watch'""",
            (owner, child, report["session_generation"])).fetchall()
        for row in rows:
            data = json.loads(row["data"])
            if data["repeat"] and report["ledger_row_id"] > data.get("progress_watermark", data["watermark"]):
                data["consumed"].pop("blocker", None)
                data["progress_watermark"] = report["ledger_row_id"]
                _save(conn, row, data)
        return False
    if report["status"] not in {"done", "aborted", "error"}:
        return False
    from ledger import child_report_ready_text, child_report_ready_tell_id
    from outbound_notices import ensure_notice_marker
    nid = child_report_ready_tell_id(report["report_id"])
    result = coalesce_notice_conn(conn, dict(notice_id=nid, tell_id=nid, kind="report",
        dedupe_key=f"report:{report['report_id']}", recipient_stream_id=owner, source_stream_id=child,
        body=ensure_notice_marker(nid, child_report_ready_text(report)),
        metadata={"report_id": report["report_id"], "ledger_row_id": report["ledger_row_id"],
                  "msg_id": report["msg_id"]}), report)
    return result.get("created", False)


def _register_conn(conn, kind, owner, generation, payload, now, *, default=False):
    if not _live(conn, owner, generation):
        raise ValueError("stale_generation")
    request = str(payload.get("request_id") or "")
    if not request:
        raise ValueError("request_id_required")
    fingerprint = _json({"kind": kind, **payload})
    if conn.execute("SELECT 1 FROM v2_watch_cancellations WHERE owner=? AND owner_generation=? AND request_id=?",
                    (owner, generation, request)).fetchone():
        raise ValueError("request_conflict")
    prior = conn.execute("SELECT * FROM v2_watch_wake WHERE owner=? AND owner_generation=? AND request_id=?",
                         (owner, generation, request)).fetchone()
    if prior:
        if prior["fingerprint"] != fingerprint:
            raise ValueError("request_conflict")
        return _decode(prior)
    child = payload.get("child_stream_id") if kind == "watch" else None
    child_row = _session(conn, child) if child else None
    if kind == "watch" and (not child_row or child_row["status"] != "open" or child_row["parent_stream_id"] != owner):
        raise ValueError("not_direct_child")
    child_generation = child_row["session_generation"] if child_row else None
    key = _id(kind, owner, generation, request)
    if "delay_seconds" in payload:
        payload = {**payload, "due_at": now + payload["delay_seconds"]}
    if kind == "wake" and payload["due_at"] <= now:
        raise ValueError("time_not_future")
    data = {**payload, "registered_at": now, "default": default, "consumed": {},
            "triggers": payload.get("triggers", ["wake"]), "repeat": payload.get("repeat", False),
            "last_activity": None, "quiet_since": now, "idle_since": None, "working": None,
            "watermark": conn.execute("SELECT COALESCE(MAX(ledger_row_id),0) FROM v2_reports").fetchone()[0]}
    conn.execute("INSERT INTO v2_watch_wake VALUES (?,?,?,?,?,?,?,?,?,?)",
        (key, kind, owner, generation, child, child_generation, request, fingerprint, "active", _json(data)))
    return _decode(conn.execute("SELECT * FROM v2_watch_wake WHERE id=?", (key,)).fetchone())


def install_default_conn(conn, child_sid, now):
    child = _session(conn, child_sid)
    if not child or child["status"] != "open":
        return
    parent = _session(conn, child.get("parent_stream_id"))
    parent_gen = parent["session_generation"] if parent and parent["status"] == "open" else None
    binding = (child["session_generation"], child.get("parent_stream_id"), parent_gen)
    prior = conn.execute("SELECT * FROM v2_watch_pairs WHERE child=?", (child_sid,)).fetchone()
    epoch = prior["epoch"] if prior else 0
    if not prior or tuple(prior[k] for k in ("child_generation", "parent", "parent_generation")) != binding:
        epoch += 1
        conn.execute("INSERT OR REPLACE INTO v2_watch_pairs VALUES (?,?,?,?,?)", (child_sid, *binding, epoch))
    if child.get("no_watch"):
        return
    if not parent or parent["status"] != "open":
        return
    _register_conn(conn, "watch", child["parent_stream_id"], parent["session_generation"],
        {"request_id": "default:" + _id(child_sid, child["session_generation"], epoch),
         "child_stream_id": child_sid, "triggers": ["end", "blocker", "idle"], "repeat": True}, now, default=True)


def lifecycle_watch_conn(conn, now):
    """Retire replaced owners/targets; close's terminal fact precedes retirement."""
    rows = conn.execute("SELECT * FROM v2_watch_wake WHERE state='active'").fetchall()
    for row in rows:
        owner = _live(conn, row["owner"], row["owner_generation"])
        child = _session(conn, row["child"]) if row["child"] else None
        target_valid = not row["child"] or bool(child and child["session_generation"] == row["child_generation"]
                                                and child["parent_stream_id"] == row["owner"])
        closed = bool(child and child["status"] == "closed")
        if owner and target_valid and closed:
            data = json.loads(row["data"])
            if "end" in data["triggers"] and "end" not in data["consumed"]:
                from outbound_notices import ensure_notice_marker
                nid = _id("end", row["owner_generation"], row["child_generation"])
                coalesce_notice_conn(conn, dict(notice_id=nid, tell_id=nid, kind="reconciler",
                    dedupe_key=nid, recipient_stream_id=row["owner"], source_stream_id=row["child"],
                    episode_id=nid, body=ensure_notice_marker(nid, f"Child session {row['child']} closed.")))
        if not owner or not target_valid or closed:
            conn.execute("UPDATE v2_watch_wake SET state='cancelled' WHERE id=? AND state='active'", (row["id"],))
    # Check consumed work too: a fired wake still belongs to its old generation.
    for fact in conn.execute("""SELECT f.* FROM v2_watch_facts f JOIN v2_outbound_notices n USING(notice_id)
                              WHERE n.delivered_at IS NULL AND n.terminal_at IS NULL""").fetchall():
        if not _notice_valid_conn(conn, fact):
            conn.execute("UPDATE v2_outbound_notices SET terminal_at=?, terminal_reason=? WHERE notice_id=?",
                         (_stamp(now), "watch_lifecycle_retired", fact["notice_id"]))


def _notice_valid_conn(conn, fact):
    if not _live(conn, fact["owner"], fact["owner_generation"]):
        return False
    # Accepted reports and confirmed death describe a completed child episode.
    # Reopening/reparenting the child cannot retract its notice to the same parent.
    if not fact["child"] or fact["kind"] in {"end", "blocker"}:
        return True
    child = _session(conn, fact["child"])
    if not child or child["session_generation"] != fact["child_generation"]:
        return False
    return child["status"] == "open" and child["parent_stream_id"] == fact["owner"]


class _WatchWakeStoreMixin:
    async def register_watch_wake(self, kind, owner, generation, payload, *, now=None):
        def op(conn):
            with conn:
                return _register_conn(conn, kind, owner, generation, payload, time.time() if now is None else now)
        return await self.submit(op)

    async def list_watch_wake(self, kind, owner, generation):
        def op(conn):
            if not _live(conn, owner, generation):
                raise ValueError("stale_generation")
            return [_decode(r) for r in conn.execute("SELECT * FROM v2_watch_wake WHERE kind=? AND owner=? AND owner_generation=? ORDER BY rowid",
                                                     (kind, owner, generation))]
        return await self.submit(op)

    async def cancel_watch_wake(self, kind, owner, generation, key, *, request_id=None):
        def op(conn):
            with conn:
                if not _live(conn, owner, generation):
                    raise ValueError("stale_generation")
                if request_id:
                    registered = conn.execute("SELECT 1 FROM v2_watch_wake WHERE owner=? AND owner_generation=? AND request_id=?",
                                              (owner, generation, request_id)).fetchone()
                    prior = conn.execute("SELECT * FROM v2_watch_cancellations WHERE owner=? AND owner_generation=? AND request_id=?",
                                         (owner, generation, request_id)).fetchone()
                    if registered or (prior and (prior["kind"] != kind or prior["target"] != key)):
                        raise ValueError("request_conflict")
                row = conn.execute("SELECT * FROM v2_watch_wake WHERE id=?", (key,)).fetchone()
                if not row or row["kind"] != kind or row["owner"] != owner or row["owner_generation"] != generation:
                    raise ValueError("not_owner")
                if row["state"] == "active":
                    conn.execute("UPDATE v2_watch_wake SET state='cancelled' WHERE id=?", (key,))
                if request_id:
                    conn.execute("INSERT OR IGNORE INTO v2_watch_cancellations VALUES (?,?,?,?,?)",
                                 (owner, generation, request_id, kind, key))
                return {"id": key}
        return await self.submit(op)

    async def watch_notice_valid(self, notice_id):
        def op(conn):
            facts = conn.execute("SELECT * FROM v2_watch_facts WHERE notice_id=?", (notice_id,)).fetchall()
            return all(_notice_valid_conn(conn, f) for f in facts)
        return await self.submit(op)

    async def evaluate_watch_wake(self, observations, *, now=None):
        stamp = time.time() if now is None else now
        def op(conn):
            with conn:
                lifecycle_watch_conn(conn, stamp)
                for row in conn.execute("SELECT host, session_name FROM sessions WHERE status='open'").fetchall():
                    install_default_conn(conn, f"{row['host']}:{row['session_name']}", stamp)
                count = 0
                for row in conn.execute("SELECT * FROM v2_watch_wake WHERE state='active'").fetchall():
                    count += _evaluate_conn(conn, row, observations.get(row["child"], {}), stamp)
                return count
        return await self.submit(op)

    async def cancel_watch_wake_for_rollback(self, *, now=None):
        def op(conn):
            with conn:
                work = conn.execute("UPDATE v2_watch_wake SET state='cancelled' WHERE state!='cancelled'").rowcount
                notices = conn.execute("""UPDATE v2_outbound_notices SET terminal_at=?, terminal_reason='d2_rollback'
                    WHERE kind IN ('watch','wake','wake_urgent') AND delivered_at IS NULL AND terminal_at IS NULL""",
                    (_stamp(time.time() if now is None else now),)).rowcount
                return {"cancelled_work": work, "terminal_notices": notices}
        return await self.submit(op)


def _evaluate_conn(conn, row, observation, now):
    from outbound_notices import ensure_notice_marker
    data = json.loads(row["data"])
    due = []
    if row["kind"] == "wake":
        if data["due_at"] <= now:
            due.append(("wake", data["due_at"], row["id"]))
    else:
        valid_observation = observation.get("session_generation") == row["child_generation"]
        activity = observation.get("genuine_activity_at")
        valid_activity = (valid_observation and observation.get("genuine_activity_generation") == row["child_generation"]
                          and isinstance(activity, (float, int)) and not isinstance(activity, bool) and math.isfinite(activity) and 0 < activity <= now)
        advanced = valid_activity and activity > (data["last_activity"] or data["registered_at"])
        working = observation.get("working") if valid_observation else None
        working_at = observation.get("watch_working_at")
        short_turn = (valid_observation and isinstance(working_at, (float, int)) and math.isfinite(working_at)
                      and (data.get("last_working_at") or data["registered_at"]) < working_at <= now)
        if short_turn:
            data["last_working_at"] = working_at
            data["idle_since"] = None
        working_transition = short_turn or (working is True and data["working"] is not True)
        if advanced:
            data["last_activity"] = activity
            data["quiet_since"] = max(data["registered_at"], activity)
        if working is True:
            data["idle_since"] = None
        elif working is False and (advanced or data["idle_since"] is None):
            data["idle_since"] = max(data["registered_at"], activity) if advanced else now
        if working is not None:
            data["working"] = working
        if data["repeat"]:
            for trigger in list(data["consumed"]):
                if trigger == "end":
                    continue
                if advanced or (trigger == "idle" and working_transition):
                    del data["consumed"][trigger]
        for trigger in data["triggers"]:
            if trigger in data["consumed"]:
                continue
            if trigger == "idle" and data["idle_since"] is not None and working is False:
                baseline, threshold = data["idle_since"], 900
                ready = now > baseline + threshold
            elif trigger.startswith("quiet="):
                baseline, threshold = data["quiet_since"], int(trigger.split("=")[1]) * 60
                ready = now >= baseline + threshold
            else:
                continue
            if ready:
                due.append((trigger, baseline + threshold, _json([float(baseline), float(threshold)])))
        _save(conn, row, data)
    count = 0
    for trigger, trigger_at, episode in due:
        fact_kind = "wake" if trigger == "wake" else "inactivity"
        nid = _id(row["owner"], row["owner_generation"], row["child"], row["child_generation"], fact_kind, episode)
        prior = _fact_notice(conn, row["owner"], row["owner_generation"], row["child"], row["child_generation"], fact_kind, episode)
        if prior is None:
            kind = ("wake_urgent" if data.get("urgent") else "wake") if trigger == "wake" else "watch"
            body = (f"Timed wake: {data.get('note') or 'resume your work'}." if trigger == "wake" else
                    f"Child session {row['child']} reached an inactivity threshold at {_stamp(trigger_at)}.")
            _insert_outbound_notice_conn(conn, notice_id=nid, tell_id=nid, kind=kind, dedupe_key=nid,
                recipient_stream_id=row["owner"], source_stream_id=row["child"], episode_id=episode,
                body=ensure_notice_marker(nid, body), created_at=_stamp(time.time()),
                metadata={"trigger_at": trigger_at, "owner_generation": row["owner_generation"],
                          "child_generation": row["child_generation"]})
            _fact(conn, owner=row["owner"], owner_generation=row["owner_generation"], child=row["child"],
                  child_generation=row["child_generation"], kind=fact_kind, source=episode, notice_id=nid, now=trigger_at)
            count += 1
        row = conn.execute("SELECT * FROM v2_watch_wake WHERE id=?", (row["id"],)).fetchone()
        _consume(conn, row, trigger, nid, episode)
    return count
