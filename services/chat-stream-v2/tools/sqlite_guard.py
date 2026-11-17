"""SQLite confinement shared by read timing and disposable gate attempts."""
from __future__ import annotations
import os
import threading
import weakref
from pathlib import Path

def _resolve(database, uri: bool) -> str | None:
    """The real file path SQLite would open, or None for memory/temporary."""
    from urllib.parse import parse_qs, unquote
    if isinstance(database, bytes):
        database = database.decode()
    database = os.fspath(database)
    if database in ("", ":memory:"):
        return None
    path = database
    if uri and database.startswith("file:"):
        rest, _, query = database[5:].partition("?")
        params = parse_qs(query.partition("#")[0])
        if "vfs" in params:
            raise PermissionError(f"custom SQLite VFS refused: {database}")
        if params.get("mode") == ["memory"]:
            return None
        if rest.startswith("//"):
            authority, slash, rest = rest[2:].partition("/")
            if authority not in ("", "localhost"):
                raise PermissionError(f"remote SQLite URI refused: {database}")
            rest = slash + rest
        path = unquote(rest.partition("#")[0])
        if path in ("", ":memory:"):
            return None
    return os.path.realpath(path)


def _install_guard(owned: Path, *, attempt=False, emit=None):
    """Refuse, before SQLite opens it, every database outside ``owned``."""
    import re
    import sqlite3
    root = os.path.realpath(owned)
    inherited_root = getattr(sqlite3, "_attempt_guard_root", None)
    if inherited_root is not None:
        if root != inherited_root and not root.startswith(inherited_root + os.sep):
            Registry(Path(inherited_root)).refuse("nested guard outside attempt root")
            raise PermissionError("nested guard outside attempt root")
        attempt = True
    registry = Registry(Path(inherited_root or root)) if attempt else None
    if attempt:
        sqlite3._attempt_guard_root = inherited_root or root
    def _emit(**fields):
        if registry is not None and fields.get("kind") == "refused":
            registry.refuse(fields.get("reason", "SQLite operation refused"))
        if emit is not None:
            emit(**fields)

    real_connect = sqlite3.connect
    approved = threading.local()
    attach = re.compile(r"\s*ATTACH\s+(?:DATABASE\s+)?(\?\d*|[:@$][A-Za-z_]\w*)\s+AS\s", re.I)
    if attempt:
        attach = re.compile(r"\s*(?:ATTACH\s+(?:DATABASE\s+)?|VACUUM\s+(?:[A-Za-z_]\w*\s+)?INTO\s+)(\?\d*|[:@$][A-Za-z_]\w*)(?:\s+AS\s|\s*;?\s*$)", re.I)

    def inside(target, uri: bool) -> bool:
        try:
            path = _resolve(target, uri)
        except PermissionError as exc:
            _emit(kind="refused", reason=str(exc))
            return False
        if path is None:
            return True
        if path == root or path.startswith(root + os.sep):
            if registry is not None:
                try:
                    registry.check(path)
                except (OSError, ValueError) as exc:
                    _emit(kind="refused", reason=str(exc))
                    return False
            _emit(kind="open", path=os.path.relpath(path, root))
            return True
        _emit(kind="refused", reason=f"database outside the owned directory: {path}")
        return False

    def authorizer(conn):
        """Per connection. A bound target is approved only for the one guarded
        execution on this connection whose parameters were resolved inside,
        and the approval is consumed by its first use."""
        owner = weakref.ref(conn)

        def authorize(action, arg1, *_rest):
            if action != sqlite3.SQLITE_ATTACH:
                return sqlite3.SQLITE_OK
            if arg1 is not None:
                return sqlite3.SQLITE_OK if inside(arg1, True) else sqlite3.SQLITE_DENY
            if getattr(approved, "value", None) is not None and approved.value() is owner():
                approved.value = None
                return sqlite3.SQLITE_OK
            _emit(kind="refused", reason="ATTACH with an unresolved filename")
            return sqlite3.SQLITE_DENY
        return authorize

    def bound(sql, parameters):
        """None for no bound ATTACH; else the exact parameters to execute
        with, holding only a checked target; False to refuse. Only an exact
        str or bytes with no registered adapter is accepted, and it is the
        value executed, so SQLite binds precisely the checked filename."""
        match = attach.match(sql) if isinstance(sql, str) else None
        if match is None:
            return None
        token = match.group(1)
        try:
            if token in ("?", "?1"):
                if len(parameters) != 1:
                    return False
                value = parameters[0]
            elif token[0] in ":@$":
                value = parameters[token[1:]]
            else:
                return False
        except (LookupError, TypeError, ValueError):
            return False
        if type(value) not in (str, bytes) or (type(value), sqlite3.PrepareProtocol) in sqlite3.adapters:
            _emit(kind="refused", reason="bound ATTACH target is not an exact unadapted str or bytes")
            return False
        if not inside(value, True):
            return False
        return (value,) if token.startswith("?") else {token[1:]: value}

    def refuse(reason: str, error=sqlite3.OperationalError):
        _emit(kind="refused", reason=reason)
        raise error("read timing refused: " + reason)

    def run(conn, method, sql, parameters):
        # Checked on every execution, so a cached prepared ATTACH (which the
        # authorizer never sees again) is rechecked with its new parameters.
        if attempt:
            # Replacing the same callback expires SQLite's statement cache, so
            # literal ATTACH and VACUUM targets are checked on EVERY execution.
            authorizer_base.set_authorizer(conn, authorizer(conn))
        exact = bound(sql, parameters)
        if exact is False:
            refuse("ATTACH outside the owned directory or with an unresolved target", sqlite3.DatabaseError)
        approved.value = None if exact is None else weakref.ref(conn)
        try:
            return method(sql, parameters if exact is None else exact)
        finally:
            approved.value = None

    real_cursor = sqlite3.Cursor

    class Cursor(real_cursor):
        def execute(self, sql, parameters=(), /):
            return run(self.connection, super().execute, sql, parameters)

        def executemany(self, sql, parameters, /):
            if not attempt and isinstance(sql, str) and attach.match(sql):
                refuse("ATTACH through executemany", sqlite3.DatabaseError)
            if attempt and isinstance(sql, str) and re.match(r"\s*(?:ATTACH|VACUUM)\b", sql, re.I):
                for values in parameters:
                    self.execute(sql, values)
                return self
            return super().executemany(sql, parameters)

    real_connection = sqlite3.Connection
    authorizer_base = getattr(real_connection, "_guard_authorizer_base", real_connection)

    class Connection(real_connection):
        """Replaces sqlite3.Connection before candidate import, so every
        candidate factory subclasses it and both super().__init__ and an
        explicit sqlite3.Connection.__init__ resolve here: the path is checked
        and the ATTACH authorizer installed before any candidate SQL runs."""
        def __init__(self, database, *args, **kwargs):
            uri = kwargs.get("uri", args[6] if len(args) > 6 else False)
            if not inside(database, bool(uri)):
                raise sqlite3.OperationalError("read timing refused a database outside the owned directory")
            super().__init__(database, *args, **kwargs)
            self._guard_database = (_resolve(database, bool(uri)) or ":memory:", False)
            authorizer_base.set_authorizer(self, authorizer(self))

        def cursor(self, factory=Cursor):
            # An unguarded cursor could rerun a cached ATTACH unchecked.
            if not (isinstance(factory, type) and issubclass(factory, Cursor)):
                refuse(f"cursor factory outside the guarded base: {factory!r}")
            return super().cursor(factory)

        def execute(self, sql, parameters=(), /):
            return self.cursor().execute(sql, parameters)

        def executemany(self, sql, parameters, /):
            return self.cursor().executemany(sql, parameters)

        def backup(self, target, *args, **kwargs):
            if attempt:
                if not isinstance(target, Connection):
                    refuse("backup target is not a guarded connection")
                for connection in (self, target):
                    if not inside(*connection._guard_database):
                        refuse("backup database identity changed")
            return super().backup(target, *args, **kwargs)

        def set_authorizer(self, _callback):
            refuse("replacing the SQLite authorizer", sqlite3.NotSupportedError)

        def load_extension(self, *_args, **_kwargs):
            refuse("loading a SQLite extension", sqlite3.NotSupportedError)

        def enable_load_extension(self, enabled, /):
            if enabled:
                refuse("enabling SQLite extension loading", sqlite3.NotSupportedError)
            return super().enable_load_extension(enabled)

    Connection._guard_authorizer_base = authorizer_base
    Connection.__module__ = Cursor.__module__ = "sqlite3"

    def connect(database, *args, **kwargs):
        factory = args[4] if len(args) > 4 else kwargs.setdefault("factory", Connection)
        if not (isinstance(factory, type) and issubclass(factory, Connection)):
            refuse(f"connection factory outside the guarded base: {factory!r}")
        return real_connect(database, *args, **kwargs)

    import _sqlite3
    for module in (sqlite3, sqlite3.dbapi2, _sqlite3) if attempt else (sqlite3, sqlite3.dbapi2):
        module.Connection, module.Cursor, module.connect = Connection, Cursor, connect


class Registry:
    """Cross-interpreter, append-only creation ledger; never adopts existing files.

    The attempt owns this directory. This protects against accidental fixture
    opens, not hostile code able to rewrite the ledger or monkeypatch Python.
    """
    def __init__(self, root):
        import json
        self.root = root.resolve(strict=True)
        self.path = self.root / '.sqlite-created.jsonl'
        self.refusals = self.root / '.sqlite-refusals.jsonl'

    def refuse(self, reason):
        import json
        fd = os.open(self.refusals, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps({'pid': os.getpid(), 'reason': reason}) + '\n').encode())
        finally:
            os.close(fd)

    def check(self, path):
        import fcntl
        import json
        import stat
        if Path(path) in (self.path, self.refusals):
            raise ValueError('guard metadata is not a database')
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+') as ledger:
            fcntl.flock(ledger, fcntl.LOCK_EX)
            records = [json.loads(line) for line in ledger]
            try:
                current = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                # Reserve the inode before SQLite sees it, atomically refusing
                # an intervening file rather than adopting it as a fresh DB.
                created = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    current = os.fstat(created)
                    record = {'path': path, 'device': current.st_dev, 'inode': current.st_ino}
                    ledger.write(json.dumps(record, sort_keys=True) + '\n')
                    ledger.flush()
                    os.fsync(ledger.fileno())
                    return
                finally:
                    os.close(created)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                raise ValueError('not a single-link regular attempt database: ' + path)
            identity = {'path': path, 'device': current.st_dev, 'inode': current.st_ino}
            if identity not in records:
                raise ValueError('pre-existing or replaced database is not owned by this attempt: ' + path)
