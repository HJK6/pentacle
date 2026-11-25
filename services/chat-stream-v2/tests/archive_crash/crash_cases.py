"""Event archive crash boundaries with the real retention writer.

Run on the named host, not in the hosted workflow:

    python -m pytest tests/archive_crash/crash_cases.py -v

Each case lands real sends through a real daemon process, stops it, then runs
``RetentionJob.run_pass()`` in a child process (``writer.py``) that dies by real
process exit at one boundary. The stopped pair is inspected offline before any
retry; application-facing readback after a restart uses public frames only.
"""

from __future__ import annotations

import json
import platform
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.archive_crash.writer import STAGE_EXIT, STORAGE_EXIT
from tests.contract.harness import expect_landed, unique

SERVICE_DIR = Path(__file__).resolve().parents[2]
WRITER = Path(__file__).with_name("writer.py")
EVENTS, SESSIONS = "session_event_tail", "sessions"
ARCHIVE = "sessions_archive.db"
# The target USER is the older of the two in its open stream: with one event
# kept per stream it is the only eligible event, and the retired seat is the
# only eligible session row.
TAIL_KEEP = 1
STAGES = {"archive_precommit": (1, 0), "archive_committed": (1, 1),
          "verified": (1, 1), "hot_deleted": (0, 1)}


@dataclass
class Pair:
    root: Path
    seat: object
    send: dict
    event: dict
    session: dict
    receipt: dict
    newer_seq: int

    @property
    def db(self):
        return self.root / "sessions.db"


def _row(path, table, row):
    key = ("event_id",) if table == EVENTS else ("host", "session_name")
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", path
        if not exists:
            return []
        where = " AND ".join(f'"{name}" IS ?' for name in key)
        return [dict(found) for found in
                db.execute(f'SELECT * FROM "{table}" WHERE {where}', [row[name] for name in key])]


@pytest.fixture(autouse=True)
def _record_run():
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=SERVICE_DIR, capture_output=True, text=True)
    print("run", json.dumps({"node": platform.node(), "commit": head.stdout.strip() or "unknown",
                             "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                             "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))


def _receipt_of(client, seat, request_id):
    reply = client.call("send.receipt.get", request_id=request_id, to_stream_id=seat.stream_id)
    assert reply["type"] == "send.receipt.get.ok" and reply["found"], reply
    return reply["receipts"][0]


def _snapshot(source: Path, target: Path) -> None:
    target.mkdir()
    for name in ("sessions.db", ARCHIVE):
        with closing(sqlite3.connect(source / name)) as src, \
                closing(sqlite3.connect(target / name)) as dest:
            src.backup(dest)
    # The copy is its own pair: the hot allocator names the copy's archive.
    with closing(sqlite3.connect(target / "sessions.db")) as db:
        db.execute("UPDATE v2_send_receipt_sequence SET archive_path=? WHERE singleton=1",
                   (str(target / ARCHIVE),))
        db.commit()


def build_pair(chat, archive_mode) -> Pair:
    chat.start()
    client = chat.client()
    client.subscribe()
    seat = chat.spawn(client, unique("archived"))
    retired = chat.spawn(client, unique("retired"))
    send = dict(request_id=unique("send"), optimistic_id=unique("optimistic"),
                to_stream_id=seat.stream_id, text=unique("message"))
    seqs = []
    for payload in (send, dict(send, request_id=unique("send"), optimistic_id=unique("optimistic"),
                               text=unique("newer"))):
        start = len(client.frames)
        expect_landed(client.call("send", **payload), seat.stream_id, payload["request_id"])
        event = client.event(seat.stream_id, start=start, description="durable USER",
                             match=lambda event, p=payload: (event["kind"], event["text"])
                             == ("USER", p["text"]))
        seqs.append(event["daemon_seq"])
    receipt = _receipt_of(client, seat, send["request_id"])
    assert chat.close_seat(client, retired)["type"] == "close.ok"
    chat.daemon.stop(signal.SIGTERM)
    root = Path(chat.daemon.root)
    if archive_mode == "wal":
        with closing(sqlite3.connect(root / ARCHIVE)) as archive:
            assert archive.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    event = _row(root / "sessions.db", EVENTS, {"event_id": seqs[0]})
    session = _row(root / "sessions.db", SESSIONS, {"host": retired.stream_id.split(":")[0],
                                                     "session_name": retired.name})
    assert len(event) == 1 and len(session) == 1, (event, session)
    with closing(sqlite3.connect(root / "sessions.db")) as db:
        # Setup witness: these are the only two rows retention may move.
        assert db.execute(f"SELECT COUNT(*) FROM {EVENTS} WHERE stream_id=?",
                          (seat.stream_id,)).fetchone() == (2,)
        assert db.execute(f"SELECT COUNT(*) FROM {EVENTS} WHERE stream_id!=?",
                          (seat.stream_id,)).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM sessions WHERE status!='open'").fetchone() == (1,)
    return Pair(root, seat, send, event[0], session[0], receipt, seqs[1])


def run_writer(root: Path, *, boundary="none", table=None, stage=None, file="archive"):
    receipt = root / f"writer-{time.monotonic_ns()}.json"
    argv = [sys.executable, str(WRITER), "--db", str(root / "sessions.db"),
            "--archive", str(root / ARCHIVE), "--receipt", str(receipt),
            "--boundary", boundary, "--tail-keep", str(TAIL_KEEP), "--file", file]
    if table:
        argv += ["--table", table]
    if stage:
        argv += ["--stage", stage]
    started = time.monotonic()
    child = subprocess.run(argv, cwd=SERVICE_DIR, capture_output=True, text=True, timeout=60)
    body = json.loads(receipt.read_text()) if receipt.exists() else None
    record = {"argv": argv[2:], "returncode": child.returncode, "receipt": body,
              "elapsed_s": round(time.monotonic() - started, 3), "stderr": child.stderr[-2000:]}
    print("writer", json.dumps(record, default=str))
    return child.returncode, body, record


def placement(pair, root, table):
    row = pair.event if table == EVENTS else pair.session
    hot, archived = _row(root / "sessions.db", table, row), _row(root / ARCHIVE, table, row)
    for copy in (*hot, *archived):
        assert copy == row, f"{table} copy differs from the original: {copy!r} != {row!r}"
    return len(hot), len(archived)


def converge(pair, root):
    code, body, _ = run_writer(root)
    assert code == 0 and body["boundary"] == "none", body
    for table in (EVENTS, SESSIONS):
        assert placement(pair, root, table) == (0, 1), f"{table} did not converge"
    code, body, _ = run_writer(root)
    assert code == 0 and (body["sessions_moved"], body["events_moved"]) == (0, 0), body
    for table in (EVENTS, SESSIONS):
        assert placement(pair, root, table) == (0, 1), f"{table} changed on a no-op pass"


def public_readback(chat, pair, hot_count):
    # Defer retention for this daemon: its first pass waits a full interval.
    chat.daemon.extra_env["PENTACLE_RETENTION_FIRST_DELAY_S"] = "86400"
    chat.daemon.start()
    client = chat.client()
    client.subscribe()
    seq = pair.event["event_id"]
    replies = client.history(pair.seat.stream_id, limit=10, chunk_limit=10,
                             before_daemon_seq=pair.newer_seq + 1)
    seen = [event for frame in replies for event in frame.body.get("events") or []
            if event.get("daemon_seq") == seq]
    assert len(seen) == hot_count, f"public history returned the event {len(seen)} times"
    if seen:
        assert (seen[0]["kind"], seen[0]["text"]) == ("USER", pair.send["text"]), seen[0]
    assert _receipt_of(client, pair.seat, pair.send["request_id"]) == pair.receipt, (
        "original receipt changed")
    inputs = len(chat.inputs(pair.seat))
    replay = client.call("send", **pair.send)
    expect_landed(replay, pair.seat.stream_id, pair.send["request_id"])
    assert replay["receipt_id"] == pair.receipt["receipt_id"], replay
    assert len(chat.inputs(pair.seat)) == inputs, "exact replay added a provider input"
    chat.daemon.stop(signal.SIGTERM)


# The first write inside a move's COMMIT to the named file. The archive copy
# commits first and alone, so a death there leaves the row hot only; the hot
# file is first written by the delete's commit, after the archive copy is
# durable, so a death there leaves it in both.
STORAGE = {"archive": (1, 0), "hot": (1, 1)}


@pytest.mark.parametrize("file", list(STORAGE))
@pytest.mark.parametrize("archive_mode", ["delete", "wal"])
@pytest.mark.parametrize("table", [EVENTS, SESSIONS])
def test_storage_stop_between_file_commits(chat, tmp_path, table, archive_mode, file):
    """Real exit at a file's first write inside COMMIT keeps the row."""
    pair = build_pair(chat, archive_mode)
    _snapshot(pair.root, tmp_path / "twin")
    _snapshot(pair.root, tmp_path / "control")
    code, body, _ = run_writer(pair.root, boundary="storage", table=table, file=file)
    assert code == STORAGE_EXIT and body["boundary"] == "storage" and body["table"] == table, body
    assert Path(body["file"]).name.startswith(ARCHIVE if file == "archive" else "sessions.db"), body
    stopped = placement(pair, pair.root, table)
    print("stopped", table, archive_mode, "hot/archive", stopped)
    assert placement(pair, tmp_path / "twin", table) == (1, 0)
    code, body, _ = run_writer(tmp_path / "control", boundary="trace")
    assert code == 0, body
    assert placement(pair, tmp_path / "control", table) == (0, 1), "uninterrupted control"
    # No move commit writes both files: the archive copy commits alone, and the
    # hot delete's commit (which holds the archive reservation) writes no archive page.
    assert all(len(commit["files"]) <= 1 for commit in body["commits"]), body["commits"]
    for name in (EVENTS, SESSIONS):
        written = [next(iter(commit["files"])) for commit in body["commits"]
                   if commit["table"] == name and commit["files"]]
        assert written and written == ["archive", "hot"] * (len(written) // 2), (name, body["commits"])
    assert sum(stopped) >= 1, f"{table} row is in neither file after the stop: {stopped}"
    assert stopped == STORAGE[file]
    public_readback(chat, pair, hot_count=1)
    converge(pair, pair.root)


@pytest.mark.parametrize("stage", list(STAGES))
@pytest.mark.parametrize("table", [EVENTS, SESSIONS])
def test_stage_stop(chat, table, stage):
    """Real exit at each named stage leaves the stated counts and exact rows."""
    pair = build_pair(chat, "delete")
    code, body, _ = run_writer(pair.root, boundary="stage", table=table, stage=stage)
    assert code == STAGE_EXIT, f"stage {stage} was not observed: {body}"
    assert (body["stage"], body["table"], body["rows"]) == (stage, table, 1), body
    key = {"event_id": pair.event["event_id"]} if table == EVENTS else {
        "host": pair.session["host"], "session_name": pair.session["session_name"]}
    assert all(body["keys"][0].get(name) == value for name, value in key.items()), body
    assert placement(pair, pair.root, table) == STAGES[stage]
    event_hot = placement(pair, pair.root, EVENTS)[0]
    public_readback(chat, pair, hot_count=event_hot)
    converge(pair, pair.root)
