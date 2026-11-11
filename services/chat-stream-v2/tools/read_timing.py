#!/usr/bin/env python3
"""Time a release candidate's main Store reads on copies of the live database.

One mode. A child process backs up the hot database and the archive it names
through SQLite's backup API from ``mode=ro`` sources into one owned directory,
checks the pair identity, rebinds the hot copy to the archive copy, closes both
sources, and only then imports the candidate ``store`` module. From that point
every SQLite open and ATTACH is resolved to a real path and refused before
SQLite sees it when it lies outside the owned directory. The candidate's real
``Store`` then serves three frozen reads, each timed as store execution.

PASS needs all three reads to run within budget version 1. A breach or the
deadline is FAIL; anything that keeps a read from running is NOT_EVALUATED.
FAIL exits 1, NOT_EVALUATED exits 2. ``deploy.py`` admits a forward daemon
deploy only with a passing receipt for its exact SHA (``check_receipt``).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

SCHEMA = "pentacle.v2.read-timing.v1"
BUDGET_VERSION = 1
INVENTORY = "list_open_sessions_with_event_summary"
HISTORY = "fetch_session_event_page"
FENCE = "list_open_usage_fence_rows"
#: Budget version 1, seconds of store execution. Changing one is a spec edit.
BUDGETS = {INVENTORY: 3.0, HISTORY: 1.0, FENCE: 0.1}
HISTORY_LIMIT = 200
HISTORY_MIN_USER_OR_TELL = 10
DEADLINE_S = 900.0
SOURCE_LOCK_WAIT_S = 5.0
FREE_SPACE_FACTOR = 1.2
DEFAULT_DB = Path.home() / ".local/share/pentacle-stream/sessions.db"
PASS, FAIL, NOT_EVALUATED = "PASS", "FAIL", "NOT_EVALUATED"
EXIT = {PASS: 0, FAIL: 1, NOT_EVALUATED: 2}
HOT_COPY, ARCHIVE_COPY = "sessions.db", "sessions_archive.db"


# -- admission (imported by deploy.py) ----------------------------------------

def _seconds(value, where: str, budget: float) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{where} timing is not a finite non-negative number")
    if value > budget:
        raise ValueError(f"{where} timing exceeds budget version {BUDGET_VERSION}")


def check_receipt(raw: object, sha: str) -> None:
    """Raise ValueError unless ``raw`` is a passing version-1 receipt for ``sha``."""
    if not isinstance(raw, dict):
        raise ValueError("receipt is not an object")
    if raw.get("schema") != SCHEMA:
        raise ValueError("receipt schema is not " + SCHEMA)
    if raw.get("budget_version") != BUDGET_VERSION or type(raw.get("budget_version")) is not int:
        raise ValueError(f"receipt budget version is not {BUDGET_VERSION}")
    if raw.get("sha") != sha:
        raise ValueError("receipt sha does not match deploy sha")
    source = raw.get("source")
    if not isinstance(source, dict) or source.get("sha_before") != sha or source.get("sha_after") != sha:
        raise ValueError("receipt source SHA does not match deploy SHA")
    if source.get("clean_before") is not True or source.get("clean_after") is not True:
        raise ValueError("receipt source was not clean")
    if raw.get("passed") is not True or raw.get("verdict") != PASS:
        raise ValueError("receipt is not passing")
    reads = raw.get("reads")
    if not isinstance(reads, list):
        raise ValueError("receipt reads must be a list")
    rows: dict[str, dict] = {}
    for row in reads:
        if not isinstance(row, dict) or row.get("name") not in BUDGETS:
            raise ValueError("receipt read row is malformed")
        if row["name"] in rows:
            raise ValueError(f"receipt repeats read {row['name']}")
        rows[row["name"]] = row
    for name, budget in BUDGETS.items():
        row = rows.get(name)
        if row is None:
            raise ValueError(f"receipt lacks required read {name}")
        if row.get("verdict") != PASS:
            raise ValueError(f"required read {name} is {row.get('verdict')}")
        _seconds(row.get("seconds"), name, budget)
        if type(row.get("statements")) is not int or row["statements"] < 1:
            raise ValueError(f"required read {name} ran no store statements")
    page = rows[HISTORY]
    if (type(page.get("events")) is not int or page["events"] != HISTORY_LIMIT
            or type(page.get("user_or_tell")) is not int or page["user_or_tell"] < HISTORY_MIN_USER_OR_TELL):
        raise ValueError("history page did not exercise a full page")
    fence = rows[FENCE]
    expected, hosts = fence.get("expected_hosts"), fence.get("hosts")
    if (not isinstance(expected, list) or not expected or len(set(map(str, expected))) != len(expected)
            or not all(isinstance(host, str) and host for host in expected) or not isinstance(hosts, list)):
        raise ValueError("usage-fence expected hosts are malformed")
    seen = {}
    for row in hosts:
        if not isinstance(row, dict) or row.get("host") not in expected or row["host"] in seen:
            raise ValueError("usage-fence host row is malformed, unexpected or repeated")
        seen[row["host"]] = row
    for host in expected:
        row = seen.get(host)
        if row is None or row.get("verdict") != PASS:
            raise ValueError(f"usage-fence host {host} has no passing row")
        _seconds(row.get("seconds"), f"{FENCE}({host})", BUDGETS[FENCE])


# -- child: copy, contain, time -----------------------------------------------

_EMIT_LOCK = threading.Lock()


def _emit(**fields) -> None:
    """One JSON line to the parent; the store thread and the main thread both emit."""
    with _EMIT_LOCK:
        sys.stdout.write(json.dumps(fields, sort_keys=True) + "\n")
        sys.stdout.flush()


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


def _install_guard(owned: Path) -> None:
    """Refuse, before SQLite opens it, every database outside ``owned``."""
    import re
    import sqlite3
    root = os.path.realpath(owned)
    real_connect = sqlite3.connect
    approved = threading.local()
    attach = re.compile(r"\s*ATTACH\s+(?:DATABASE\s+)?(\?\d*|[:@$][A-Za-z_]\w*)\s+AS\s", re.I)

    def inside(target, uri: bool) -> bool:
        try:
            path = _resolve(target, uri)
        except PermissionError as exc:
            _emit(kind="refused", reason=str(exc))
            return False
        if path is None:
            return True
        if path == root or path.startswith(root + os.sep):
            _emit(kind="open", path=os.path.relpath(path, root))
            return True
        _emit(kind="refused", reason=f"database outside the owned directory: {path}")
        return False

    def authorize(action, arg1, *_rest):
        if action != sqlite3.SQLITE_ATTACH:
            return sqlite3.SQLITE_OK
        if arg1 is not None:
            return sqlite3.SQLITE_OK if inside(arg1, True) else sqlite3.SQLITE_DENY
        if getattr(approved, "value", False):
            return sqlite3.SQLITE_OK
        _emit(kind="refused", reason="ATTACH with an unresolved filename")
        return sqlite3.SQLITE_DENY

    def bound(sql, parameters):
        match = attach.match(sql) if isinstance(sql, str) else None
        if match is None:
            return None
        token = match.group(1)
        try:
            if token.startswith("?"):
                value = parameters[int(token[1:] or 1) - 1]
            else:
                value = parameters[token[1:]]
        except (LookupError, TypeError, ValueError):
            return False
        return isinstance(value, (str, bytes)) and inside(value, True)

    classes: dict[type, type] = {}

    def guarded(factory):
        if factory not in classes:
            class Guarded(factory):
                def execute(self, sql, parameters=(), /):
                    ok = bound(sql, parameters)
                    if ok is False:
                        raise sqlite3.DatabaseError("read timing refused an ATTACH outside the owned directory")
                    approved.value = bool(ok)
                    try:
                        return super().execute(sql, parameters)
                    finally:
                        approved.value = False

                def set_authorizer(self, callback):
                    def both(*args):
                        verdict = authorize(*args)
                        return verdict if verdict != sqlite3.SQLITE_OK or callback is None else callback(*args)
                    super().set_authorizer(both)

            Guarded.__name__ = Guarded.__qualname__ = factory.__name__
            classes[factory] = Guarded
        return classes[factory]

    def connect(database, *args, **kwargs):
        uri = kwargs.get("uri", args[6] if len(args) > 6 else False)
        if not inside(database, bool(uri)):
            raise sqlite3.OperationalError("read timing refused a database outside the owned directory")
        if len(args) > 4:  # positional factory follows timeout..check_same_thread
            args = (*args[:4], guarded(args[4]), *args[5:])
        else:
            kwargs["factory"] = guarded(kwargs.get("factory", sqlite3.Connection))
        conn = real_connect(database, *args, **kwargs)
        conn.set_authorizer(None)
        return conn

    sqlite3.connect = connect
    sqlite3.dbapi2.connect = connect


def _backup(source: Path, target: Path) -> float:
    import sqlite3
    started = time.monotonic()
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=SOURCE_LOCK_WAIT_S)
    try:
        dst = sqlite3.connect(str(target))
        try:
            # One step: a single consistent snapshot of the source.
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return round(time.monotonic() - started, 3)


def _binding(path: Path):
    import sqlite3
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=SOURCE_LOCK_WAIT_S)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='v2_send_receipt_sequence'").fetchone() is None:
            return None
        return conn.execute("SELECT pair_id, archive_path FROM v2_send_receipt_sequence WHERE singleton=1").fetchone()
    finally:
        conn.close()


def _prepare_copies(db: Path, owned: Path) -> dict:
    """Copy and rebind the pair; return source identity or raise RuntimeError."""
    import sqlite3
    hot = owned / HOT_COPY
    binding = _binding(db)
    archive = Path(binding[1]).resolve() if binding and binding[1] else None
    sizes = db.stat().st_size + (archive.stat().st_size if archive and archive.is_file() else 0)
    if shutil.disk_usage(owned).free < sizes * FREE_SPACE_FACTOR:
        raise RuntimeError("less free disk than both files plus 20%")
    if archive is not None and not os.access(archive, os.R_OK):
        raise RuntimeError(f"bound archive missing or unreadable: {archive}")
    timings = {"hot": _backup(db, hot)}
    copied = _binding(hot)
    identity = {"path": str(db.resolve()), "size_bytes": db.stat().st_size, "archive_path": None,
                "archive_size_bytes": None, "backup_seconds": timings}
    conn = sqlite3.connect(str(hot))
    try:
        identity["max_event_id"] = conn.execute(
            "SELECT MAX(event_id) FROM session_event_tail").fetchone()[0] if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='session_event_tail'").fetchone() else None
    finally:
        conn.close()
    if not (copied and copied[1]):
        return identity
    archive = Path(copied[1]).resolve()
    if not os.access(archive, os.R_OK):
        raise RuntimeError(f"bound archive missing or unreadable: {archive}")
    timings["archive"] = _backup(archive, owned / ARCHIVE_COPY)
    identity.update(archive_path=str(archive), archive_size_bytes=archive.stat().st_size)
    # The existing retention pair contract: version 1 marker naming the hot pair id.
    conn = sqlite3.connect(str(owned / ARCHIVE_COPY))
    try:
        marker = conn.execute("SELECT version, pair_id FROM v2_receipt_route_archive WHERE singleton=1").fetchone()
    except sqlite3.Error:
        marker = None
    finally:
        conn.close()
    if marker is None or tuple(marker) != (1, copied[0]):
        raise RuntimeError("hot/archive pair identity mismatch")
    conn = sqlite3.connect(str(hot))
    try:
        conn.execute("UPDATE v2_send_receipt_sequence SET archive_path=? WHERE singleton=1",
                     (str((owned / ARCHIVE_COPY).resolve()),))
        conn.commit()
    finally:
        conn.close()
    return identity


def _timed_store(base):
    """The candidate Store, timing store execution and statements of reads."""
    import functools

    class Timed(base):
        timing = None

        def submit(self, fn, *args, **kwargs):
            meter = self.timing
            if meter is None:
                return super().submit(fn, *args, **kwargs)

            @functools.wraps(fn)
            def run(conn):
                conn.set_trace_callback(lambda _sql: meter.__setitem__("statements", meter["statements"] + 1))
                started = time.perf_counter()
                try:
                    return fn(conn)
                finally:
                    meter["seconds"] += time.perf_counter() - started
                    meter["calls"] += 1
                    conn.set_trace_callback(None)
            return super().submit(run, *args, **kwargs)

    return Timed


async def _time(store, name: str, call) -> tuple[dict, object]:
    store.timing = meter = {"seconds": 0.0, "statements": 0, "calls": 0}
    try:
        result = await call()
    finally:
        store.timing = None
    row = {"name": name, "seconds": round(meter["seconds"], 6), "statements": meter["statements"],
           "budget_s": BUDGETS[name]}
    if meter["calls"] == 0:
        return {**row, "verdict": NOT_EVALUATED, "detail": "read did not run through Store.submit"}, result
    return {**row, "verdict": PASS if meter["seconds"] <= BUDGETS[name] else FAIL}, result


async def _reads(store, cls) -> None:
    import traceback

    def missing(name):
        return not callable(getattr(cls, name, None))

    async def guarded(name, body):
        if missing(name):
            _emit(kind="read", row={"name": name, "verdict": NOT_EVALUATED, "detail": "candidate lacks the method"})
            return
        try:
            row = await body()
        except Exception as exc:  # the read could not run: say why, never pass
            row = {"name": name, "verdict": NOT_EVALUATED,
                   "detail": f"{type(exc).__name__}: {exc}"[:500],
                   "trace": traceback.format_exc(limit=3)[-800:]}
        _emit(kind="read", row=row)

    async def inventory():
        row, _ = await _time(store, INVENTORY, store.list_open_sessions_with_event_summary)
        return row

    async def history():
        pick = await store.submit(lambda conn: conn.execute(
            "SELECT s.host || ':' || s.session_name, COUNT(*) AS n "
            "FROM sessions s JOIN session_event_tail t ON t.stream_id = s.host || ':' || s.session_name "
            "AND t.session_created_at = s.created_at WHERE s.status='open' "
            "GROUP BY s.host, s.session_name ORDER BY n DESC, s.host, s.session_name LIMIT 1").fetchone())
        if pick is None:
            return {"name": HISTORY, "verdict": NOT_EVALUATED, "detail": "no open session with events to select"}
        stream_id, rows = pick[0], pick[1]
        # The daemon's open-chat request: no generation, so the page is the
        # current birth's (only recovered closed history passes a generation).
        row, page = await _time(store, HISTORY, lambda: store.fetch_session_event_page(
            stream_id, before_daemon_seq=None, limit=HISTORY_LIMIT))
        page = page if isinstance(page, list) else []
        visible = sum(1 for event in page if isinstance(event, dict) and event.get("kind") in ("USER", "TELL"))
        row.update(stream_id=stream_id, tail_rows=rows, events=len(page), user_or_tell=visible)
        if row["verdict"] == PASS and (len(page) != HISTORY_LIMIT or visible < HISTORY_MIN_USER_OR_TELL):
            row.update(verdict=NOT_EVALUATED, detail="page was short or lacked user/tell events")
        return row

    async def fence():
        expected = [r[0] for r in await store.submit(lambda conn: conn.execute(
            "SELECT DISTINCT host FROM sessions WHERE status='open' ORDER BY host").fetchall())]
        if not expected:
            return {"name": FENCE, "verdict": NOT_EVALUATED, "expected_hosts": [], "hosts": [],
                    "detail": "no open session to select a host"}
        hosts = []
        for host in expected:
            row, _ = await _time(store, FENCE, lambda: store.list_open_usage_fence_rows(host))
            hosts.append({"host": host, "verdict": row["verdict"], "seconds": row["seconds"],
                          "statements": row["statements"]})
        verdicts = {row["verdict"] for row in hosts}
        verdict = FAIL if FAIL in verdicts else NOT_EVALUATED if NOT_EVALUATED in verdicts else PASS
        return {"name": FENCE, "verdict": verdict, "budget_s": BUDGETS[FENCE], "expected_hosts": expected,
                "hosts": hosts, "seconds": max(row["seconds"] for row in hosts),
                "statements": sum(row["statements"] for row in hosts)}

    await guarded(INVENTORY, inventory)
    await guarded(HISTORY, history)
    await guarded(FENCE, fence)


def child_main(config: dict) -> int:
    import asyncio
    import logging
    logging.disable(logging.CRITICAL)
    owned = Path(config["owned"])
    try:
        identity = _prepare_copies(Path(config["db"]), owned)
    except Exception as exc:
        _emit(kind="refused", reason=f"copy: {type(exc).__name__}: {exc}"[:500])
        return 0
    _emit(kind="database", database=identity)
    _install_guard(owned)
    os.chdir(owned)
    service = Path(config["candidate"]) / "services" / "chat-stream-v2"
    sys.path[:0] = [str(service), str(service.parent)]
    try:
        import store as candidate
        cls = _timed_store(candidate.Store)
        archive = owned / ARCHIVE_COPY
        store = cls(str(owned / HOT_COPY), **({"archive_path": str(archive)} if archive.exists() else {}))
        store.start(timeout=max(1.0, config["deadline_at"] - time.time()))
    except Exception as exc:
        _emit(kind="refused", reason=f"candidate Store did not start: {type(exc).__name__}: {exc}"[:500])
        return 0
    try:
        asyncio.run(_reads(store, candidate.Store))
    finally:
        store.stop(timeout=30)
    _emit(kind="done")
    return 0


# -- parent: owned directory, deadline, receipt -------------------------------

def _git(candidate: Path, *args: str) -> str:
    return subprocess.run(("git", "-C", str(candidate), *args), text=True, capture_output=True,
                          check=True).stdout.strip()


def _source_state(candidate: Path) -> tuple[str | None, bool]:
    try:
        return _git(candidate, "rev-parse", "--verify", "HEAD"), _git(candidate, "status", "--porcelain") == ""
    except (OSError, subprocess.CalledProcessError):
        return None, False


def _verdict(reads: list[dict], refusals: list[str], expired: bool) -> str:
    verdicts = {row["verdict"] for row in reads}
    if expired or FAIL in verdicts:
        return FAIL
    if refusals or verdicts != {PASS} or {row["name"] for row in reads} != set(BUDGETS):
        return NOT_EVALUATED
    return PASS


def run(candidate: Path, out: Path, *, db: Path = DEFAULT_DB, deadline_s: float = DEADLINE_S,
        work_root: Path | None = None, python: str = sys.executable) -> dict:
    """Run one timing pass and write its receipt; return the receipt."""
    import platform
    import sqlite3
    started = time.time()
    candidate = candidate.resolve()
    sha, clean = _source_state(candidate)
    reads: list[dict] = []
    refusals: list[str] = []
    opened: list[str] = []
    database: dict = {"path": str(db)}
    expired = False
    if sha is None or not clean:
        refusals.append("candidate checkout is not a clean Git tree")
        reads = [{"name": name, "verdict": NOT_EVALUATED, "detail": "candidate checkout is not clean"}
                 for name in BUDGETS]
    else:
        owned = Path(tempfile.mkdtemp(prefix="pentacle-read-timing-", dir=work_root))
        config = {"owned": str(owned), "db": str(db), "candidate": str(candidate),
                  "deadline_at": started + deadline_s}
        proc = subprocess.Popen((python, "-I", "-B", str(Path(__file__).resolve()), "--child", json.dumps(config)),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, cwd=str(owned))
        try:
            stdout, stderr = proc.communicate(timeout=max(0.0, started + deadline_s - time.time()))
        except subprocess.TimeoutExpired:
            expired = True
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        finally:
            shutil.rmtree(owned, ignore_errors=True)
        done = False
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("kind")
            if kind == "read":
                reads.append(event["row"])
            elif kind == "refused":
                refusals.append(event["reason"])
            elif kind == "open" and event["path"] not in opened:
                opened.append(event["path"])
            elif kind == "database":
                database = event["database"]
            done = done or kind == "done"
        if expired:
            refusals.append(f"deadline of {deadline_s:g}s expired; child killed")
        elif not done and not refusals:
            refusals.append(f"child exited rc={proc.returncode}: {stderr[-500:]}")
        seen = {row["name"] for row in reads}
        reads += [{"name": name, "verdict": FAIL if expired else NOT_EVALUATED,
                   "detail": "deadline expired" if expired else "read did not run"}
                  for name in BUDGETS if name not in seen]
    sha_after, clean_after = _source_state(candidate)
    verdict = _verdict(reads, refusals, expired)
    if (sha_after, clean_after) != (sha, True) and verdict == PASS:
        verdict = NOT_EVALUATED
    receipt = {
        "schema": SCHEMA, "budget_version": BUDGET_VERSION, "sha": sha,
        "source": {"sha_before": sha, "sha_after": sha_after, "clean_before": clean, "clean_after": clean_after},
        "verdict": verdict, "passed": verdict == PASS, "reads": reads, "refusals": refusals,
        "opened": opened, "database": database, "deadline_s": deadline_s,
        "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "elapsed_s": round(time.time() - started, 3),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--child"]:
        return child_main(json.loads(argv[1]))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate", type=Path, required=True, help="clean checkout of the release candidate")
    parser.add_argument("--out", type=Path, required=True, help="receipt path for deploy.py --read-timing-receipt")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="hot database to copy (read-only)")
    args = parser.parse_args(argv)
    receipt = run(args.candidate, args.out, db=args.db)
    print(json.dumps({"verdict": receipt["verdict"], "reads": [
        {key: row.get(key) for key in ("name", "verdict", "seconds", "detail") if key in row}
        for row in receipt["reads"]], "refusals": receipt["refusals"], "receipt": str(args.out)}, indent=2))
    return EXIT[receipt["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
