"""tools/read_timing.py on owned fixtures only; the live database is never touched."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess

import pytest

import read_timing
from retention import initialize_history_pair
from store import Store

SERVICE = Path(__file__).resolve().parents[1]
BIRTH = "2026-10-01T00:00:00Z"
INVENTORY, HISTORY, FENCE = read_timing.INVENTORY, read_timing.HISTORY, read_timing.FENCE


def _commit(root: Path) -> Path:
    (root / ".gitignore").write_text("__pycache__/\n")
    for args in (("init", "-q"), ("add", "."), ("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                                 "commit", "-qm", "candidate")):
        subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)
    return root


@contextmanager
def _db(path, **kwargs):
    """Commit and close: an unclosed handle would keep a lock or a WAL beside the file."""
    conn = sqlite3.connect(path, **kwargs)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(receipt):
    return {row["name"]: row for row in receipt["reads"]}


@pytest.fixture(scope="module")
def real_candidate(tmp_path_factory):
    """A clean Git checkout of this service's actual modules."""
    root = tmp_path_factory.mktemp("real-candidate")
    target = root / "services/chat-stream-v2"
    target.mkdir(parents=True)
    for source in SERVICE.glob("*.py"):
        shutil.copy2(source, target / source.name)
    shutil.copytree(SERVICE.parent / "_shared", root / "services/_shared",
                    ignore=shutil.ignore_patterns("__pycache__", "tests"))
    # Contract files that shared modules read at import.
    shutil.copytree(SERVICE.parents[1] / "pentacle-chat-core", root / "pentacle-chat-core",
                    ignore=shutil.ignore_patterns("node_modules", ".build"))
    return _commit(root)


def _seed_real(directory: Path) -> Path:
    """A real Store pair whose archive binding is absolute and outside any owned directory."""
    db = directory / "sessions.db"
    store = Store(str(db))
    store.start()

    def write(conn):
        initialize_history_pair(conn)  # what the retention job does: create and pair the archive
        for i, host in enumerate(("host-a", "host-b")):
            name, stream = f"seat-{i}", f"{host}:seat-{i}"
            conn.execute("INSERT INTO sessions(host,session_name,provider,visibility,status,created_at) "
                         "VALUES(?,?,'codex','hidden','open',?)", (host, name, BIRTH))
            conn.execute("INSERT INTO v2_session_generations VALUES(?,?,?)", (host, name, f"gen-{i}"))
            for j in range(260 if i == 0 else 20):
                kind = "USER" if j % 5 == 0 else "ASSIST_TEXT"
                event = {"kind": kind, "stream_id": stream, "text": f"e-{j}", "timestamp": BIRTH}
                conn.execute("INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,"
                             "event_ts,recorded_at) VALUES(?,?,?,?,?,1.0)",
                             (stream, BIRTH, str(j), json.dumps(event), BIRTH))
        conn.commit()

    asyncio.run(store.submit(write))
    assert store.stop()
    return db


def _freeze(*paths: Path) -> dict:
    for path in paths:
        with _db(path) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return {path: _digest(path) for path in paths}


@pytest.fixture
def kept(monkeypatch, tmp_path):
    """Snapshot the owned directory just before the command removes it."""
    keep = tmp_path / "kept"
    real = shutil.rmtree

    def snapshot(path, *args, **kwargs):
        if Path(path).name.startswith("pentacle-read-timing-") and Path(path).is_dir():
            shutil.copytree(path, keep)
        real(path, *args, **kwargs)

    monkeypatch.setattr(read_timing.shutil, "rmtree", snapshot)
    return keep


def test_real_candidate_times_copies_and_never_touches_the_originals(real_candidate, tmp_path, kept):
    source = tmp_path / "live"
    source.mkdir()
    db = _seed_real(source)
    archive = source / "sessions_archive.db"
    with _db(db) as conn:
        assert conn.execute("SELECT archive_path, archive_ready FROM v2_send_receipt_sequence").fetchone() == (
            str(archive.absolute()), 1)
    # Make startup migrate the archive: drop the known nullable batch columns.
    with _db(archive) as conn:
        for column in ("batched_into_dispatch_id", "answered_by_event_id"):
            conn.execute(f"ALTER TABLE v2_assistant_composite_routes DROP COLUMN {column}")
    before = _freeze(db, archive)
    work = tmp_path / "work"
    work.mkdir()

    receipt = read_timing.run(real_candidate, tmp_path / "receipt.json", db=db, work_root=work)

    assert receipt["refusals"] == [] and receipt["verdict"] == "PASS", receipt
    assert {_digest(path) for path in before} == set(before.values()), "originals changed"
    assert sorted(receipt["opened"]) == ["sessions.db", "sessions_archive.db"]
    assert list(work.iterdir()) == [], "owned directory removed"
    with _db(kept / "sessions_archive.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(v2_assistant_composite_routes)")}
    assert {"batched_into_dispatch_id", "answered_by_event_id"} <= columns, "migration landed in the copy"
    with _db(kept / "sessions.db") as conn:
        bound = Path(conn.execute("SELECT archive_path FROM v2_send_receipt_sequence").fetchone()[0])
    assert bound.name == "sessions_archive.db" and bound.parent.name.startswith("pentacle-read-timing-")
    rows = _rows(receipt)
    assert rows[HISTORY]["stream_id"] == "host-a:seat-0"
    assert all(sample["events"] == 200 and sample["user_or_tell"] >= 10 for sample in rows[HISTORY]["samples"])
    assert rows[FENCE]["expected_hosts"] == ["host-a", "host-b"]
    assert receipt["database"]["path"] == str(db.resolve())
    assert receipt["database"]["archive_path"] == str(archive.resolve())
    read_timing.check_receipt(receipt, receipt["sha"])
    assert json.loads((tmp_path / "receipt.json").read_text()) == receipt


def test_mismatched_or_missing_archive_is_not_evaluated(real_candidate, tmp_path):
    source = tmp_path / "live"
    source.mkdir()
    db = _seed_real(source)
    archive = source / "sessions_archive.db"
    with _db(archive) as conn:
        conn.execute("UPDATE v2_receipt_route_archive SET pair_id='other'")
    receipt = read_timing.run(real_candidate, tmp_path / "r.json", db=db, work_root=tmp_path)
    assert receipt["verdict"] == "NOT_EVALUATED" and "pair identity mismatch" in receipt["refusals"][0]
    archive.unlink()
    receipt = read_timing.run(real_candidate, tmp_path / "r.json", db=db, work_root=tmp_path)
    assert receipt["verdict"] == "NOT_EVALUATED" and "missing or unreadable" in receipt["refusals"][0]
    assert {row["verdict"] for row in receipt["reads"]} == {"NOT_EVALUATED"}


# -- fake candidates: refusals, deadline, verdicts ------------------------------

FAKE_STORE = '''
import json, os, sqlite3, time
SETTINGS = json.loads({settings!r})


class Store:
    def __init__(self, path, *, archive_path=None):
        self.path = path

    def start(self, timeout=10.0):
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        if SETTINGS.get("pidfile"):
            with open(SETTINGS["pidfile"], "w") as handle:
                handle.write(str(os.getpid()))
        outside = SETTINGS.get("outside")
        try:
            if SETTINGS.get("escape") == "connect":
                sqlite3.connect(outside).execute("CREATE TABLE IF NOT EXISTS t(a)")
            elif SETTINGS.get("escape") == "uri":
                sqlite3.connect("file:" + outside + "?mode=rw", uri=True).execute("CREATE TABLE t(a)")
            elif SETTINGS.get("escape") == "attach_param":
                self.conn.execute("ATTACH DATABASE ? AS x", (outside,))
                self.conn.execute("CREATE TABLE x.t(a)")
            elif SETTINGS.get("escape") == "attach_literal":
                self.conn.executescript("ATTACH DATABASE '" + outside + "' AS y; CREATE TABLE y.t(a);")
        except sqlite3.Error:
            if not SETTINGS.get("swallow"):
                raise

    def stop(self, timeout=5.0):
        self.conn.close()
        return True

    async def submit(self, fn, *, settle_on_cancel=False):
        return fn(self.conn)

    calls = {{"inventory": 0, "history": 0}}

    async def list_open_sessions_with_event_summary(self):
        call = self.calls["inventory"]
        self.calls["inventory"] += 1
        def op(conn):
            sleeps = SETTINGS.get("inventory_sleeps", [0])
            time.sleep(sleeps[min(call, len(sleeps) - 1)])
            return conn.execute("SELECT * FROM sessions WHERE status='open'").fetchall()
        return await self.submit(op)

    async def fetch_session_event_page(self, stream_id, *, before_daemon_seq, limit, generation=None):
        call = self.calls["history"]
        self.calls["history"] += 1
        if call == SETTINGS.get("history_raises_on"):
            raise RuntimeError("one invocation failed")
        def op(conn):
            rows = conn.execute("SELECT event_json FROM session_event_tail ORDER BY event_id DESC LIMIT ?",
                                (limit,)).fetchall()
            page = [json.loads(row[0]) for row in rows]
            return page[:12] if call == SETTINGS.get("history_short_on") else page
        return await self.submit(op)

    async def list_open_usage_fence_rows(self, host):
        return await self.submit(lambda conn: conn.execute(
            "SELECT * FROM sessions WHERE host=? AND status='open'", (host,)).fetchall())


for name in SETTINGS.get("omit", []):
    delattr(Store, name)
'''


def _fake_candidate(root: Path, **settings) -> Path:
    service = root / "services/chat-stream-v2"
    service.mkdir(parents=True)
    (service / "store.py").write_text(FAKE_STORE.format(settings=json.dumps(settings)))
    return _commit(root)


def _fake_world(directory: Path, *, status="open", events=200) -> Path:
    directory.mkdir(exist_ok=True)
    db = directory / "sessions.db"
    with _db(db) as conn:
        conn.execute("CREATE TABLE sessions(host, session_name, status, created_at)")
        conn.execute("CREATE TABLE session_event_tail(event_id INTEGER PRIMARY KEY, stream_id, "
                     "session_created_at, event_json)")
        conn.execute("CREATE TABLE v2_session_generations(host, session_name, generation)")
        conn.execute("INSERT INTO sessions VALUES('host-a','seat',?,?)", (status, BIRTH))
        conn.execute("INSERT INTO v2_session_generations VALUES('host-a','seat','gen')")
        conn.executemany("INSERT INTO session_event_tail(stream_id,session_created_at,event_json) VALUES(?,?,?)",
                         (("host-a:seat", BIRTH, json.dumps({"kind": "TELL" if j % 4 == 0 else "TOOL_USE"}))
                          for j in range(events)))
    return db


def test_fake_world_passes_and_its_receipt_is_admitted(tmp_path):
    candidate = _fake_candidate(tmp_path / "c")
    receipt = read_timing.run(candidate, tmp_path / "r.json", db=_fake_world(tmp_path / "w"), work_root=tmp_path)
    assert receipt["verdict"] == "PASS" and receipt["passed"] is True, receipt
    for field in ("schema", "budget_version", "sha", "source", "reads", "python", "sqlite", "database"):
        assert field in receipt
    assert receipt["source"] == {"sha_before": receipt["sha"], "sha_after": receipt["sha"],
                                 "clean_before": True, "clean_after": True}
    assert set(receipt["database"]) >= {"path", "size_bytes", "max_event_id", "backup_seconds"}
    assert receipt["database"]["max_event_id"] == 200
    rows = _rows(receipt)
    for row in (rows[INVENTORY], rows[HISTORY], *rows[FENCE]["hosts"]):
        assert set(row) >= {"verdict", "seconds", "warmup", "samples"} and len(row["samples"]) == 3
        for invocation in (row["warmup"], *row["samples"]):
            assert set(invocation) >= {"seconds", "statements"} and invocation["statements"] >= 1
    assert rows[HISTORY]["samples"][0]["events"] == 200 and rows[INVENTORY]["samples"][0]["rows"] == 1
    read_timing.check_receipt(receipt, receipt["sha"])


@pytest.mark.parametrize("escape", ["connect", "uri", "attach_param", "attach_literal"])
@pytest.mark.parametrize("swallow", [False, True], ids=["raised", "swallowed"])
def test_outside_database_is_refused_before_open(tmp_path, escape, swallow):
    sentinel = tmp_path / "outside" / "sentinel.db"
    sentinel.parent.mkdir()
    with _db(sentinel) as conn:
        conn.execute("CREATE TABLE marker(a)")
    before = _digest(sentinel)
    candidate = _fake_candidate(tmp_path / "c", escape=escape, outside=str(sentinel), swallow=swallow)
    receipt = read_timing.run(candidate, tmp_path / "r.json", db=_fake_world(tmp_path / "w"), work_root=tmp_path)
    assert receipt["verdict"] == "NOT_EVALUATED" and receipt["passed"] is False
    assert any(str(sentinel.resolve()) in reason for reason in receipt["refusals"]), receipt["refusals"]
    assert _digest(sentinel) == before
    assert sorted(path.name for path in sentinel.parent.iterdir()) == ["sentinel.db"]


@pytest.mark.parametrize("case", ["missing_method", "dirty_tree", "no_open_session", "short_page", "no_user_or_tell"])
def test_unrunnable_reads_are_not_evaluated_and_exit_nonzero(tmp_path, case):
    candidate = _fake_candidate(tmp_path / "c", omit=[FENCE] if case == "missing_method" else [])
    if case == "dirty_tree":
        (candidate / "services/chat-stream-v2/store.py").write_text("# edited\n", encoding="utf-8")
    world = _fake_world(tmp_path / "w", status="closed" if case == "no_open_session" else "open",
                        events=12 if case == "short_page" else 200)
    if case == "no_user_or_tell":
        with _db(world) as conn:
            conn.execute("UPDATE session_event_tail SET event_json='{\"kind\": \"SYSTEM\"}'")
    out = tmp_path / "r.json"
    code = read_timing.main(["--candidate", str(candidate), "--out", str(out), "--db", str(world)])
    receipt = json.loads(out.read_text())
    rows = _rows(receipt)
    assert code == 2 and receipt["verdict"] == "NOT_EVALUATED" and receipt["passed"] is False
    expected = {"missing_method": {FENCE}, "dirty_tree": {INVENTORY, HISTORY, FENCE},
                "no_open_session": {INVENTORY, HISTORY, FENCE}, "short_page": {HISTORY}, "no_user_or_tell": {HISTORY}}[case]
    assert {name for name, row in rows.items() if row["verdict"] == "NOT_EVALUATED"} == expected
    assert {name for name, row in rows.items() if row["verdict"] == "PASS"} == set(rows) - expected
    if case == "dirty_tree":
        assert receipt["source"]["clean_before"] is False and "database" in receipt


def test_median_breach_fails_without_hiding_other_reads(tmp_path):
    # warm-up fast, then samples slow, fast, slow: the median breaches.
    candidate = _fake_candidate(tmp_path / "c", inventory_sleeps=[0, 3.1, 0, 3.1], omit=[FENCE])
    out = tmp_path / "r.json"
    code = read_timing.main(["--candidate", str(candidate), "--out", str(out),
                             "--db", str(_fake_world(tmp_path / "w"))])
    receipt = json.loads(out.read_text())
    rows = _rows(receipt)
    assert code == 1 and receipt["verdict"] == "FAIL"
    assert rows[INVENTORY]["verdict"] == "FAIL" and rows[INVENTORY]["seconds"] > 3.0
    assert len(rows[INVENTORY]["samples"]) == 3
    assert rows[HISTORY]["verdict"] == "PASS" and rows[FENCE]["verdict"] == "NOT_EVALUATED"


def test_warmed_median_excludes_a_slow_warm_up_and_one_slow_sample(tmp_path):
    """The stated guarantee is warmed median execution, not first-read or tail latency."""
    candidate = _fake_candidate(tmp_path / "c", inventory_sleeps=[3.2, 0, 3.2, 0])
    receipt = read_timing.run(candidate, tmp_path / "r.json", db=_fake_world(tmp_path / "w"), work_root=tmp_path)
    row = _rows(receipt)[INVENTORY]
    assert receipt["verdict"] == "PASS" and row["warmup"]["seconds"] > 3.0
    assert sorted(sample["seconds"] > 3.0 for sample in row["samples"]) == [False, False, True]
    read_timing.check_receipt(receipt, receipt["sha"])


@pytest.mark.parametrize("settings", [{"history_short_on": 0}, {"history_short_on": 2},
                                      {"history_raises_on": 3}], ids=["short_warm_up", "short_sample", "failed_sample"])
def test_one_bad_invocation_cannot_hide_behind_a_passing_median(tmp_path, settings):
    candidate = _fake_candidate(tmp_path / "c", **settings)
    receipt = read_timing.run(candidate, tmp_path / "r.json", db=_fake_world(tmp_path / "w"), work_root=tmp_path)
    rows = _rows(receipt)
    assert receipt["verdict"] == "NOT_EVALUATED" and rows[HISTORY]["verdict"] == "NOT_EVALUATED"
    assert rows[INVENTORY]["verdict"] == "PASS" and rows[FENCE]["verdict"] == "PASS"


def test_deadline_kills_child_closes_source_removes_copy_and_fails(tmp_path):
    pidfile = tmp_path / "child.pid"
    candidate = _fake_candidate(tmp_path / "c", inventory_sleeps=[120], pidfile=str(pidfile))
    world = _fake_world(tmp_path / "w")
    work = tmp_path / "work"
    work.mkdir()
    receipt = read_timing.run(candidate, tmp_path / "r.json", db=world, deadline_s=4, work_root=work)
    assert receipt["verdict"] == "FAIL" and receipt["passed"] is False
    assert any("deadline" in reason for reason in receipt["refusals"])
    assert _rows(receipt)[INVENTORY]["verdict"] == "FAIL"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    assert list(work.iterdir()) == []
    with _db(world, timeout=0) as conn:
        conn.execute("BEGIN EXCLUSIVE")  # no reader still holds the source
        conn.rollback()
