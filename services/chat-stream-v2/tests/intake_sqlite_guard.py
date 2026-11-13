"""Copy-only SQLite guard for permanent replay evidence harnesses."""
from pathlib import Path
from urllib.parse import urlsplit, unquote
import sqlite3
import re


def install(root, *, writes=None):
    root = Path(root).resolve()
    original = sqlite3.connect
    evidence = {"connections": [], "attachments": [], "kv_read_attempts": 0,
                "outside_path_attempts": 0}

    def path_for(value):
        name = str(value)
        if name in (":memory:", ""):
            return name
        if name.startswith("file:"):
            parsed = urlsplit(name)
            if parsed.netloc:
                raise ValueError("database URI authority forbidden")
            name = unquote(parsed.path)
        path = Path(name).resolve()
        if not path.is_relative_to(root):
            evidence["outside_path_attempts"] += 1
            raise ValueError("database resolves outside evidence directory")
        return str(path)

    def guarded_connect(database, *args, **kwargs):
        name = path_for(database)
        allowed_attach = [0]
        base = kwargs.get("factory", sqlite3.Connection)
        def execute(owner, method, sql, parameters=()):
            attaching = sql.lstrip().upper().startswith("ATTACH")
            if attaching:
                if not re.match(r"^\s*ATTACH(?:\s+DATABASE)?\s+\?\s+AS\s+", sql, re.I):
                    raise ValueError("harness requires parameterized archive attachment")
                resolved = path_for(parameters[0])
                allowed_attach[0] += 1
            try:
                result = method(sql, parameters)
                if attaching:
                    evidence["attachments"].append(resolved)
                return result
            finally:
                if attaching:
                    allowed_attach[0] -= 1
        class GuardedCursor(sqlite3.Cursor):
            def execute(self, sql, parameters=()):
                return execute(self, super().execute, sql, parameters)
        class GuardedConnection(base):
            def execute(self, sql, parameters=()):
                return execute(self, super().execute, sql, parameters)
            def cursor(self, factory=GuardedCursor):
                if factory is not GuardedCursor:
                    raise ValueError("unguarded cursor factory")
                return super().cursor(factory)
        kwargs["factory"] = GuardedConnection
        conn = original(database, *args, **kwargs)
        evidence["connections"].append(name)
        def authorize(action, first, second, schema, trigger):
            if action == sqlite3.SQLITE_READ and first == "kv":
                evidence["kv_read_attempts"] += 1
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_ATTACH:
                # SQLite calls its authorizer before binding ATTACH parameters.
                # Only the execute wrapper can authorize that scoped operation.
                if not allowed_attach[0]:
                    return sqlite3.SQLITE_DENY
            if writes is not None and action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                writes.add(first)
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn

    sqlite3.connect = guarded_connect
    return evidence


def preflight(database, root):
    """Reject unrelocated pairs before Store startup can attempt migration."""
    root = Path(root).resolve()
    path = Path(database).resolve()
    if not path.is_relative_to(root):
        raise ValueError("main database resolves outside evidence directory")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT archive_path FROM v2_send_receipt_sequence WHERE singleton=1").fetchone()
        if row and row[0] and not Path(row[0]).resolve().is_relative_to(root):
            raise ValueError("configured archive resolves outside evidence directory")
