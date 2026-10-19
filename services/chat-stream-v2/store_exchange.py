"""Generation-fenced retained observations; all helpers run on the store thread."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3


def initialize(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS v2_child_pair_epochs (
      child TEXT PRIMARY KEY, epoch TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v2_child_exchange (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      parent TEXT NOT NULL, child TEXT NOT NULL,
      parent_generation TEXT NOT NULL, child_generation TEXT NOT NULL,
      epoch TEXT NOT NULL, kind TEXT NOT NULL, ref_id TEXT NOT NULL,
      direction TEXT NOT NULL, ts TEXT NOT NULL, text TEXT NOT NULL,
      truncated INTEGER NOT NULL, payload_sha256 TEXT NOT NULL,
      UNIQUE(parent_generation,child_generation,kind,ref_id));
    CREATE INDEX IF NOT EXISTS v2_child_exchange_page
      ON v2_child_exchange(parent,child,seq);
    CREATE TABLE IF NOT EXISTS v2_agent_report_state (
      stream_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
      status TEXT NOT NULL, ts TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS v2_child_exchange_session_update
    AFTER UPDATE OF status,parent_stream_id ON sessions
    WHEN NEW.status IS NOT OLD.status OR NEW.parent_stream_id IS NOT OLD.parent_stream_id
    BEGIN
      DELETE FROM v2_child_exchange WHERE child=OLD.host||':'||OLD.session_name
        OR (parent=OLD.host||':'||OLD.session_name AND NEW.status IS NOT OLD.status);
      DELETE FROM v2_child_pair_epochs WHERE child=OLD.host||':'||OLD.session_name;
      INSERT INTO v2_child_pair_epochs VALUES
        (OLD.host||':'||OLD.session_name,lower(hex(randomblob(16))));
      DELETE FROM v2_agent_report_state WHERE stream_id=OLD.host||':'||OLD.session_name
        AND NEW.status IS NOT OLD.status;
    END;
    CREATE TRIGGER IF NOT EXISTS v2_child_exchange_generation_update
    AFTER UPDATE OF generation ON v2_session_generations
    WHEN NEW.generation IS NOT OLD.generation
    BEGIN
      DELETE FROM v2_child_exchange WHERE child=OLD.host||':'||OLD.session_name
        OR parent=OLD.host||':'||OLD.session_name;
      DELETE FROM v2_child_pair_epochs WHERE child=OLD.host||':'||OLD.session_name;
      INSERT INTO v2_child_pair_epochs VALUES
        (OLD.host||':'||OLD.session_name,lower(hex(randomblob(16))));
      DELETE FROM v2_agent_report_state WHERE stream_id=OLD.host||':'||OLD.session_name;
    END;
    CREATE TRIGGER IF NOT EXISTS v2_child_exchange_session_delete
    AFTER DELETE ON sessions
    BEGIN
      DELETE FROM v2_child_exchange WHERE child=OLD.host||':'||OLD.session_name
        OR parent=OLD.host||':'||OLD.session_name;
      DELETE FROM v2_child_pair_epochs WHERE child=OLD.host||':'||OLD.session_name;
      DELETE FROM v2_agent_report_state WHERE stream_id=OLD.host||':'||OLD.session_name;
    END;
    """)


def session(conn, stream_id):
    host, _, name = str(stream_id).partition(":")
    row = conn.execute(
        "SELECT s.status,s.parent_stream_id,g.generation FROM sessions s "
        "JOIN v2_session_generations g USING(host,session_name) "
        "WHERE s.host=? AND s.session_name=?", (host, name)).fetchone()
    return dict(row) if row else None


def pair(conn, parent, child):
    p, c = session(conn, parent), session(conn, child)
    if not p or not c or p["status"] != "open" or c["status"] != "open":
        raise ValueError("pair_closed")
    if c["parent_stream_id"] != parent:
        raise ValueError("not_direct_child")
    epoch = conn.execute("SELECT epoch FROM v2_child_pair_epochs WHERE child=?", (child,)).fetchone()
    return {"parent": parent, "child": child, "parent_generation": p["generation"],
            "child_generation": c["generation"], "epoch": epoch[0] if epoch else c["generation"]}


def direct_pair(conn, sender, recipient):
    for parent, child in ((sender, recipient), (recipient, sender)):
        try:
            result = pair(conn, parent, child)
            result["direction"] = "parent_to_child" if sender == parent else "child_to_parent"
            return result
        except ValueError:
            pass
    return None


def source(binding, kind, ref_id, text, ts):
    if not binding:
        return None
    return {**binding, "kind": kind, "ref_id": ref_id, "text": text, "ts": ts}


def append(conn, observation):
    """False means lifecycle invalidation; conflicting identities raise before commit."""
    if not observation:
        return False
    try:
        current = pair(conn, observation["parent"], observation["child"])
    except ValueError:
        return False
    if any(observation.get(k) != v for k, v in current.items()):
        return False
    raw = observation["text"].encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    identity = tuple(observation[k] for k in ("parent_generation", "child_generation", "kind", "ref_id"))
    prior = conn.execute("SELECT payload_sha256,direction,ts,seq FROM v2_child_exchange WHERE "
                         "parent_generation=? AND child_generation=? AND kind=? AND ref_id=?", identity).fetchone()
    if prior:
        if tuple(prior)[:3] != (digest, observation["direction"], observation["ts"]):
            conn.rollback()
            raise ValueError("exchange_ref_conflict")
        observation["_seq"] = prior["seq"]
        return True
    inserted = conn.execute("INSERT INTO v2_child_exchange "
                 "(seq,parent,child,parent_generation,child_generation,epoch,kind,ref_id,direction,ts,text,truncated,payload_sha256) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (observation.get("_seq"), *[observation[k] for k in ("parent", "child", "parent_generation", "child_generation", "epoch", "kind", "ref_id", "direction", "ts")],
                  raw[:8192].decode("utf-8", errors="ignore"), int(len(raw) > 8192), digest))
    observation["_seq"] = inserted.lastrowid
    return True


def cursor(binding, before_seq):
    value = {k: binding[k] for k in ("parent", "child", "parent_generation", "child_generation")}
    value.update(v=1, before_seq=before_seq)
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")


def page(conn, msg, auth):
    def fail(code):
        return {"type": "thread.error", "request_id": msg.get("request_id"), "ok": False,
                "error_code": code, "message": code.replace("_", " ")}
    if not auth.get("token_verified") and not auth.get("operator_authenticated"):
        return fail("unauthorized")
    parent = msg.get("parent_stream_id")
    if auth.get("token_verified"):
        if parent and parent != auth.get("stream_id"):
            return fail("not_direct_child")
        parent = auth.get("stream_id")
    if not parent:
        return fail("parent_required")
    if auth.get("thread_token_hash"):
        host, _, name = str(parent).partition(":")
        token = conn.execute("SELECT token_hash FROM sessions WHERE host=? AND session_name=? AND status='open'", (host, name)).fetchone()
        if not token or token[0] != auth["thread_token_hash"]:
            return fail("unauthorized")
    child = msg.get("child_stream_id")
    try:
        binding = pair(conn, parent, child)
    except ValueError as exc:
        return fail(str(exc))
    if auth.get("session_generation") and auth["session_generation"] != binding["parent_generation"]:
        return fail("pair_closed")
    limit = msg.get("limit", 25)
    if type(limit) is not int or not 1 <= limit <= 50:
        return fail("limit_invalid")
    before = 9223372036854775807
    if msg.get("cursor") is not None:
        try:
            encoded = msg["cursor"]
            if not isinstance(encoded, str) or len(encoded) > 4096 or "=" in encoded:
                raise ValueError()
            decoded = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
            before = decoded["before_seq"]
            if type(before) is not int or not 0 < before < 9223372036854775807 or encoded != cursor(binding, before):
                raise ValueError()
            # The boundary itself must remain retained: detects reparent-away/back too.
            if not conn.execute("SELECT 1 FROM v2_child_exchange WHERE parent=? AND child=? AND seq=?", (parent, child, before)).fetchone():
                raise ValueError()
        except (ValueError, TypeError, KeyError, OverflowError):
            return fail("cursor_invalid")
    response = {"type": "thread.read.ok", "request_id": msg.get("request_id"), "ok": True,
                "parent_stream_id": parent, "child_stream_id": child,
                "parent_generation": binding["parent_generation"], "child_generation": binding["child_generation"],
                "rows": [], "next_cursor": None}
    rows = conn.execute("SELECT * FROM v2_child_exchange WHERE parent=? AND child=? AND seq<? ORDER BY seq DESC LIMIT ?",
                        (parent, child, before, limit + 1)).fetchall()
    chosen = []
    for i, row in enumerate(rows[:limit]):
        item = {k: row[k] for k in ("ref_id", "ts", "direction", "kind", "text")}
        item.update(row_id=f"{row['kind']}:{row['ref_id']}", truncated=bool(row["truncated"]))
        next_cursor = cursor(binding, row["seq"]) if i + 1 < len(rows) else None
        candidate = {**response, "rows": [item, *chosen], "next_cursor": next_cursor}
        if len(json.dumps(candidate).encode("utf-8")) > 65536:
            break
        chosen.insert(0, item)
        response = candidate
    return response


class ExchangeStoreMixin:
    async def exchange_binding(self, sender, recipient):
        return await self.submit(lambda conn: direct_pair(conn, sender, recipient))

    async def read_child_thread(self, msg, auth):
        return await self.submit(lambda conn: page(conn, msg, auth))

    async def append_child_exchange(self, observation):
        def op(conn):
            result = append(conn, observation)
            conn.commit()
            return result
        return await self.submit(op)

    async def child_report_state(self, stream_id):
        return await self.submit(lambda conn: dict(row) if (row := conn.execute(
            "SELECT * FROM v2_agent_report_state WHERE stream_id=?", (stream_id,)).fetchone()) else None)


# A completed cursor accounts for legacy receipts. Source triggers retain later
# changes, including updates below that cursor, in the source transaction.
_REPAIR_VERSION = 1
_REPAIR_ROWS = 100
_REPAIR_BYTES = 1024 * 1024
_SOURCES = {
    "tell": ("v2_tell_deliveries", ("tell_id",), "reply"),
    "report": ("v2_reports", ("report_id",), "exchange_json"),
    "brief": ("v2_spawn_outcomes", ("host", "session_name"), "delivery_receipt"),
}


def _initialize_repair(conn, rebuild):
    # This is called only after Store has installed all source columns. Finish
    # schema work before entering independently durable repair transactions.
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS v2_exchange_repair_progress (
      source TEXT PRIMARY KEY, version INTEGER NOT NULL,
      key1 TEXT, key2 TEXT, complete INTEGER NOT NULL DEFAULT 0,
      rebuilding INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS v2_exchange_repair_pending (
      source TEXT NOT NULL, key1 TEXT NOT NULL, key2 TEXT NOT NULL,
      PRIMARY KEY(source,key1,key2));
    CREATE INDEX IF NOT EXISTS v2_reports_exchange_latest
      ON v2_reports(from_stream_id,session_generation,ledger_row_id);
    CREATE TRIGGER IF NOT EXISTS v2_exchange_repair_deleted
    AFTER DELETE ON v2_child_exchange BEGIN
      INSERT INTO v2_exchange_repair_pending
        SELECT 'tell',tell_id,'' FROM v2_tell_deliveries
        WHERE OLD.kind='tell' AND tell_id=OLD.ref_id
        ON CONFLICT(source,key1,key2) DO NOTHING;
      INSERT INTO v2_exchange_repair_pending
        SELECT 'report',report_id,'' FROM v2_reports
        WHERE OLD.kind='report' AND report_id=OLD.ref_id
        ON CONFLICT(source,key1,key2) DO NOTHING;
      INSERT INTO v2_exchange_repair_pending
        SELECT 'brief',host,session_name FROM v2_spawn_outcomes
        WHERE OLD.kind='brief' AND request_id=OLD.ref_id
        ON CONFLICT(source,key1,key2) DO NOTHING;
    END;
    CREATE TRIGGER IF NOT EXISTS v2_exchange_repair_roster_deleted
    AFTER DELETE ON v2_agent_report_state BEGIN
      INSERT INTO v2_exchange_repair_pending
        SELECT 'report',report_id,'' FROM v2_reports
        WHERE from_stream_id=OLD.stream_id AND session_generation=OLD.generation
        ORDER BY ledger_row_id DESC LIMIT 1
        ON CONFLICT(source,key1,key2) DO NOTHING;
    END;
    """)
    for kind, (table, keys, _payload) in _SOURCES.items():
        new_keys = "NEW." + keys[0] + (",NEW." + keys[1] if len(keys) == 2 else ",''")
        # An outer source UPSERT can override a trigger's OR IGNORE policy;
        # explicit UPSERT conflict handling keeps repeated dirty marks harmless.
        for operation in ("INSERT", "UPDATE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS v2_exchange_repair_{kind}_{operation.lower()} "
                         f"AFTER {operation} ON {table} BEGIN "
                         "INSERT INTO v2_exchange_repair_pending(source,key1,key2) "
                         f"VALUES ('{kind}',{new_keys}) ON CONFLICT(source,key1,key2) DO NOTHING; END")
        conn.execute("INSERT INTO v2_exchange_repair_progress(source,version) VALUES (?,?) "
                     "ON CONFLICT(source) DO UPDATE SET version=excluded.version,"
                     "key1=NULL,key2=NULL,complete=0,rebuilding=0 "
                     "WHERE v2_exchange_repair_progress.version != excluded.version",
                     (kind, _REPAIR_VERSION))
    if rebuild:
        conn.execute("UPDATE v2_exchange_repair_progress SET key1=NULL,key2=NULL,complete=0,rebuilding=1")
    conn.commit()


def _decoded(value):
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _project_receipt(conn, kind, row, rebuilding):
    value = _decoded(row[0])
    if kind == "tell":
        delivery = value.get("delivery")
        observation = delivery.get("exchange") if isinstance(delivery, dict) else None
    elif kind == "brief":
        observation = value.get("exchange")
    else:
        observation = value
    if rebuilding and observation:
        try:
            current = pair(conn, observation["parent"], observation["child"])
        except ValueError:
            current = None
        if current and all(observation.get(k) == v for k, v in current.items()):
            identity = tuple(observation[k] for k in ("parent_generation", "child_generation", "kind", "ref_id"))
            prior = conn.execute("SELECT seq FROM v2_child_exchange WHERE parent_generation=? "
                                 "AND child_generation=? AND kind=? AND ref_id=?", identity).fetchone()
            if prior:
                observation.setdefault("_seq", prior[0])
                conn.execute("DELETE FROM v2_child_exchange WHERE seq=?", (prior[0],))
    repaired = int(append(conn, observation))
    if kind == "report":
        # Repair only missing state. The writer preserves the original time of
        # an unchanged status transition, which the latest report cannot infer.
        conn.execute("INSERT OR IGNORE INTO v2_agent_report_state "
                     "SELECT r.from_stream_id,r.session_generation,r.status,r.ingested_at "
                     "FROM v2_reports r JOIN sessions s ON r.from_stream_id=s.host||':'||s.session_name "
                     "JOIN v2_session_generations g USING(host,session_name) "
                     "WHERE s.status='open' AND r.session_generation=g.generation "
                     "AND r.from_stream_id=? AND r.session_generation=? "
                     "ORDER BY r.ledger_row_id DESC LIMIT 1", (row[1], row[2]))
    return repaired


def _repair_batch(conn, kind, *, pending):
    table, keys, payload = _SOURCES[kind]
    key_columns = ",".join(keys)
    key_tuple = f"({key_columns})" if len(keys) == 2 else key_columns
    placeholders = "(?,?)" if len(keys) == 2 else "?"
    selected_keys = key_columns if len(keys) == 2 else key_columns + ",''"
    lookup = " AND ".join(f"{key}=?" for key in keys)
    conn.execute("BEGIN IMMEDIATE")
    try:
        progress = conn.execute("SELECT * FROM v2_exchange_repair_progress WHERE source=?", (kind,)).fetchone()
        if pending:
            rows = conn.execute("SELECT key1,key2 FROM v2_exchange_repair_pending "
                                "WHERE source=? ORDER BY key1,key2 LIMIT ?", (kind, _REPAIR_ROWS)).fetchall()
        elif progress["complete"]:
            conn.commit()
            return False, 0
        else:
            after = f" WHERE {key_tuple}>{placeholders}" if progress["key1"] is not None else ""
            params = tuple(progress[k] for k in ("key1", "key2")[:len(keys)]) if after else ()
            rows = conn.execute(f"SELECT {selected_keys} FROM {table}{after} "
                                f"ORDER BY {key_columns} LIMIT ?", (*params, _REPAIR_ROWS)).fetchall()
        if not rows:
            if not pending:
                conn.execute("UPDATE v2_exchange_repair_progress SET complete=1,rebuilding=0 WHERE source=?", (kind,))
            conn.commit()
            return False, 0
        used, repaired = 0, 0
        for key in rows:
            params = tuple(key[:len(keys)])
            size_row = conn.execute(f"SELECT length(CAST({payload} AS BLOB)) FROM {table} WHERE {lookup}", params).fetchone()
            size = (size_row[0] or 0) if size_row else 0
            if size > _REPAIR_BYTES:
                raise ValueError(f"exchange repair oversized {kind} receipt {tuple(key)!r}: {size} bytes")
            if used + size > _REPAIR_BYTES:
                break
            used += size
            if size_row is not None:
                report_fields = ",from_stream_id,session_generation" if kind == "report" else ""
                row = conn.execute(f"SELECT {payload}{report_fields} FROM {table} WHERE {lookup}", params).fetchone()
                repaired += _project_receipt(conn, kind, row, bool(progress["rebuilding"]) and not pending)
            conn.execute("DELETE FROM v2_exchange_repair_pending WHERE source=? AND key1=? AND key2=?", (kind, *key))
            if not pending:
                conn.execute("UPDATE v2_exchange_repair_progress SET key1=?,key2=? WHERE source=?", (*key, kind))
        conn.commit()
        return True, repaired
    except BaseException:
        conn.rollback()
        raise


def repair(conn, *, rebuild=False):
    """Catch up retained fenced receipts in bounded, crash-resumable batches.

    Commits each batch with its progress; call only at the Store startup seam.
    No delivery occurs. Explicit rebuild replaces source-backed observations
    only, retaining healthy roster transition times and saved sequence ids.
    """
    _initialize_repair(conn, rebuild)
    repaired = 0
    for pending in (False, True):
        for kind in _SOURCES:
            while True:
                more, count = _repair_batch(conn, kind, pending=pending)
                repaired += count
                if not more:
                    break
    return repaired
