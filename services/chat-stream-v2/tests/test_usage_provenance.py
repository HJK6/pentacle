"""Usage provenance (spec_pentacle__usage_provenance_export_2026_10): AC1-AC5, AC7 source oracles."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path

import pytest

from event_push import EventPush
from store import SCHEMA_VERSION, Store
from usage_accounting import iso_from_ms, native_provenance
from usage_collector import UsageStateCollector
from usage_history import HistoryLog, claude_cache_lines
from usage_provenance import BackfillCursor, ProvenanceSink, file_items, run_backfill, validate_item

HOST = "worker-one"
HOST_SECRET = "worker-one-host-secret"
SATELLITE_SHA = "a" * 40
SATELLITE_PID = 1234
ORG_A = "00000000-0000-4000-8000-00000000000a"
ORG_B = "00000000-0000-4000-8000-00000000000b"
CODEX_ACCOUNT = "codex-account-0001"


class _Alerts:
    def emit(self, *_args, **_kwargs):
        return None


def _proof(host: str = HOST) -> str:
    message = f"event.push.v1\0{host}\0{SATELLITE_SHA}\0{SATELLITE_PID}"
    return hmac.new(HOST_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def _frame(items: list[dict], *, host: str = HOST, dry_run: bool | None = None, request_id: str = "p-1") -> dict:
    payload: dict = {"version": 1, "items": items}
    if dry_run is not None:
        payload["dry_run"] = dry_run
    return {
        "type": "event.push", "request_id": request_id, "push_secret": "secret",
        "satellite_sha": SATELLITE_SHA, "satellite_pid": SATELLITE_PID, "wire_version": 1,
        "host": host, "source_host_proof": _proof(host), "events": [], "high_water": {},
        "usage_provenance": payload,
    }


async def _secret() -> str:
    return "secret"


def _event_push(store: Store, history: HistoryLog | None) -> EventPush:
    ep = EventPush(
        store, lambda _frame: asyncio.sleep(0), _Alerts(), recent_limit=20,
        host_secrets={HOST: HOST_SECRET},
        provenance=ProvenanceSink(store.record_provenance, history),
    )
    ep._secret = _secret
    return ep


def _ledger_rows(conn: sqlite3.Connection) -> list[tuple]:
    return (
        [tuple(row) for row in conn.execute("SELECT * FROM v2_usage_records ORDER BY 1,2,3,4")]
        + [tuple(row) for row in conn.execute("SELECT * FROM v2_usage_state ORDER BY 1,2")]
    )


def _seed_ledger(db: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """Ledger rows for streams that are closed or archived (no open session)."""
    with sqlite3.connect(db) as conn:
        for host, provider, native, record_key in rows:
            conn.execute(
                "INSERT INTO v2_usage_records VALUES (?,?,?,?,?,?,?)",
                (host, provider, native, record_key, f"{host}:v2-{native}", "gen-old", json.dumps({"output": 1})),
            )
        conn.execute(
            "INSERT INTO v2_usage_state VALUES (?,?,?,?,?,?,?,?)",
            (f"{HOST}:v2-archived", "gen-old", HOST, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z",
             3, json.dumps({"output": 3}), json.dumps(["history_not_verified"])),
        )


def _claude_assistant(mid: str, *, session: str = "native-claude", output: int = 5,
                      ts: str = "2026-10-07T06:10:37.622Z", model: str = "claude-opus-5-5",
                      sidechain: bool = False) -> dict:
    return {"type": "assistant", "sessionId": session, "timestamp": ts, "isSidechain": sidechain,
            "version": "2.1.292", "requestId": "req-" + mid,
            "message": {"id": mid, "model": model, "usage": {"input_tokens": 1, "output_tokens": output,
                                                               "cache_read_input_tokens": 0,
                                                               "cache_creation_input_tokens": 0}}}


def _credential(org: str, *, session: str = "native-claude") -> dict:
    return {"type": "attachment", "sessionId": session, "version": "2.1.292",
            "timestamp": "2026-10-07T06:10:31.844Z",
            "attachment": {"type": "credential_org", "organizationUuid": org}}


def _codex_rollout(*, account: str | None = CODEX_ACCOUNT, cli: str = "0.160.0", native: str = "native-codex",
                   responses: int = 3) -> list[dict]:
    meta = {"id": native, "cli_version": cli}
    if account is not None:
        meta["creator_account_id"] = account
    records = [{"type": "session_meta", "timestamp": "2026-10-07T05:58:47.720Z", "payload": meta}]
    for turn in range(responses):
        records.append({"type": "turn_context", "timestamp": f"2026-10-07T06:0{turn}:00.000Z",
                        "payload": {"turn_id": f"turn-{turn}", "model": f"gpt-6-luna-{turn}"}})
        records.append({"type": "token_usage_record", "timestamp": f"2026-10-07T06:0{turn}:05.000Z",
                        "payload": {"turn_id": f"turn-{turn}", "response_id": f"resp-{turn}",
                                    "usage": {"input_tokens": 100 + turn, "cached_input_tokens": 50,
                                              "cache_write_input_tokens": 0, "output_tokens": 30,
                                              "reasoning_output_tokens": 10 + turn}}})
        records.append({"type": "event_msg", "timestamp": f"2026-10-07T06:0{turn}:06.000Z",
                        "payload": {"type": "token_count", "info": {}, "rate_limits": {
                            "limit_id": "codex",
                            "primary": {"used_percent": 3.0, "window_minutes": 10080, "resets_at": 1791948531},
                            "secondary": {"used_percent": 3.0, "window_minutes": 300, "resets_at": 1791948531},
                        }}})
    return records


# --- AC1: additive, rollback-safe tables ----------------------------------

OLD_DDL = (
    """CREATE TABLE v2_usage_state (
        stream_id TEXT NOT NULL, generation TEXT NOT NULL,
        collection_host TEXT NOT NULL, collected_since TEXT NOT NULL,
        updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
        tokens TEXT NOT NULL, reasons TEXT NOT NULL,
        PRIMARY KEY(stream_id,generation))""",
    """CREATE TABLE v2_usage_records (
        host TEXT NOT NULL, provider TEXT NOT NULL, native_session_id TEXT NOT NULL,
        record_key TEXT NOT NULL, stream_id TEXT NOT NULL, generation TEXT NOT NULL,
        tokens TEXT NOT NULL,
        PRIMARY KEY(host,provider,native_session_id,record_key))""",
)


def test_store_usage_mixed_version(tmp_path: Path) -> None:
    reference = sqlite3.connect(":memory:")
    for ddl in OLD_DDL:  # the 539b8bd shape an older daemon writes against
        reference.execute(ddl)
    expected = {table: list(reference.execute(f"PRAGMA table_info({table})"))
                for table in ("v2_usage_records", "v2_usage_state")}

    db = tmp_path / "sessions.db"
    store = Store(str(db))
    store.start()
    store.stop()
    with sqlite3.connect(db) as conn:
        for table, info in expected.items():
            assert list(conn.execute(f"PRAGMA table_info({table})")) == info
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"v2_usage_provenance", "v2_usage_identity", "v2_usage_codex_responses"} <= tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
        # Exactly the older daemon's positional statements still succeed.
        conn.execute("INSERT INTO v2_usage_records VALUES (?,?,?,?,?,?,?)",
                     ("thoth", "claude", "n", "m", "thoth:v2-x", "g", "{}"))
        conn.execute("INSERT INTO v2_usage_state VALUES (?,?,?,?,?,?,?,?)",
                     ("thoth:v2-x", "g", "thoth", "t", "t", 1, "{}", "[]"))
    # Re-open (and a rolled-back daemon's re-open) is idempotent and keeps rows.
    store = Store(str(db))
    store.start()
    store.stop()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM v2_usage_records").fetchone()[0] == 1


# --- AC2: event accepted for closed/archived sessions; never touches tokens --

def test_event_push_provenance_closed_session(tmp_path: Path) -> None:
    async def run() -> None:
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        try:
            opened = await store.open_session(HOST, "v2-closed", provider="claude", session_generation="gen-c")
            await store.mark_closed(HOST, "v2-closed", closed_at="2026-10-07T00:00:00Z", pane_status="pane_dead",
                                    expected_generation=opened["session_generation"])
            _seed_ledger(db, [
                (HOST, "claude", "native-closed", "msg-1"),     # closed stream
                (HOST, "claude", "native-archived", "msg-9"),   # archived: no session row at all
                (HOST, "codex", "native-codex", "cumulative"),  # archived Codex cumulative row
            ])
            with sqlite3.connect(db) as conn:
                before = _ledger_rows(conn)
            history = HistoryLog(tmp_path / "usage_history.jsonl")
            ep = _event_push(store, history)
            items = (
                native_provenance("claude", [_credential(ORG_A, session="native-closed"),
                                             _claude_assistant("msg-1", session="native-closed")], complete=True)
                + native_provenance("claude", [_claude_assistant("msg-9", session="native-archived")], complete=True)
                + [item for item in native_provenance("codex", _codex_rollout(responses=1), complete=True)
                   if item["kind"] == "codex_response"]
            )
            assert [item["kind"] for item in items] == ["claude_record", "claude_record", "codex_response"]
            first = await ep.handle_push(_frame(items))
            assert first["type"] == "event.push.ok"
            assert first["usage_provenance"]["counts"] == {"recorded": 3}
            replay = await ep.handle_push(_frame(items, request_id="p-2"))
            assert replay["usage_provenance"]["counts"] == {"replayed": 3}

            mixed = dict(items[0], data={**items[0]["data"], "response_id": "resp-0"})
            extra = dict(items[2], extra=True)
            wrong_kind = dict(items[0], kind="codex_response")
            claude_limit = {"kind": "rate_limit", "provider": "claude", "native_session_id": None,
                            "source_file_identity_digest": None, "identity": None,
                            "data": {"account_id": None, "window_kind": "seven_day", "window_minutes": 10080,
                                     "pct": 5, "resets_at": 1791948531, "observed_at": "2026-10-07T06:00:00Z"}}
            unknown = native_provenance("claude", [_claude_assistant("msg-x", session="native-other")], complete=True)
            missing_record = native_provenance("claude", [_claude_assistant("msg-2", session="native-closed")], complete=True)
            bad = await ep.handle_push(_frame([mixed, extra, wrong_kind, claude_limit, *unknown, *missing_record],
                                              request_id="p-3"))
            assert [entry["reason"] for entry in bad["usage_provenance"]["rejected"]] == [
                "bad_provenance", "bad_provenance", "unsupported_kind_for_provider",
                "unsupported_kind_for_provider", "unknown_native_session", "unknown_record",
            ]
            with sqlite3.connect(db) as conn:
                assert _ledger_rows(conn) == before  # byte-identical totals
                assert conn.execute("SELECT COUNT(*) FROM v2_usage_provenance").fetchone()[0] == 2
                assert conn.execute("SELECT COUNT(*) FROM v2_usage_codex_responses").fetchone()[0] == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_provenance_requires_source_host_proof_and_ignores_generation(tmp_path: Path) -> None:
    async def run() -> None:
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        try:
            _seed_ledger(db, [(HOST, "claude", "native-claude", "msg-1"), ("other-host", "claude", "native-x", "msg-1")])
            ep = _event_push(store, None)
            frame = _frame(native_provenance("claude", [_claude_assistant("msg-1")], complete=True))
            frame["source_host_proof"] = "0" * 64
            assert (await ep.handle_push(frame))["error"] == "unauthorized_source_host"
            # Another host's native session is unknown to this authenticated host.
            other = await ep.handle_push(_frame(native_provenance(
                "claude", [_claude_assistant("msg-1", session="native-x")], complete=True)))
            assert other["usage_provenance"]["counts"] == {"unknown_native_session": 1}
            dry = await ep.handle_push(_frame(native_provenance(
                "claude", [_claude_assistant("msg-1")], complete=True), dry_run=True))
            assert dry["usage_provenance"]["counts"] == {"recorded": 1}
            assert dry["usage_provenance"]["known_native_sessions"] == ["claude:native-claude"]
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT COUNT(*) FROM v2_usage_provenance").fetchone()[0] == 0
                assert conn.execute("SELECT COUNT(*) FROM v2_usage_identity").fetchone()[0] == 0
            bad_version = _frame([])
            bad_version["usage_provenance"]["version"] = 3  # versions 1 and 2 are admitted
            assert (await ep.handle_push(bad_version))["usage_provenance"] == {"version": 2, "error": "unsupported_version"}
        finally:
            store.stop()

    asyncio.run(run())


# --- AC3: identity rules ---------------------------------------------------

def _identity(items: list[dict]) -> dict | None:
    identities = {json.dumps(item["identity"], sort_keys=True) for item in items if item["kind"] != "rate_limit"}
    assert len(identities) == 1
    return json.loads(identities.pop())


def test_claude_identity_fixtures() -> None:
    single = native_provenance("claude", [_credential(ORG_A), _claude_assistant("m1")], complete=True)
    recurring = native_provenance("claude", [_credential(ORG_A), _claude_assistant("m1"), _credential(ORG_A),
                                             _claude_assistant("m2")], complete=True)
    conflicting = native_provenance("claude", [_credential(ORG_A), _claude_assistant("m1"), _credential(ORG_B)],
                                    complete=True)
    absent = native_provenance("claude", [_claude_assistant("m1")], complete=True)
    live_absent = native_provenance("claude", [_claude_assistant("m1")])
    assert _identity(single) == {"account_id": ORG_A, "account_source": "transcript", "conflict": 0, "cli_version": "2.1.292"}
    assert _identity(recurring)["account_id"] == ORG_A and len(recurring) == 2
    assert _identity(conflicting) == {"account_id": None, "account_source": "transcript", "conflict": 1, "cli_version": "2.1.292"}
    assert _identity(absent) == {"account_id": None, "account_source": "unknown", "conflict": 0, "cli_version": "2.1.292"}
    assert live_absent[0]["identity"] is None  # a live span is not proof of absence
    assert all(validate_item(item) is None for item in single + conflicting + absent + live_absent)


def test_claude_record_dedupes_by_message_and_skips_sidechains() -> None:
    items = native_provenance("claude", [
        _claude_assistant("m1", output=2, ts="2026-10-07T06:00:00.000Z"),
        _claude_assistant("m1", output=9, ts="2026-10-07T06:00:01.000Z", model="claude-fable-5-1"),
        _claude_assistant("m1", output=9, ts="2026-10-07T06:00:02.000Z"),
        _claude_assistant("m2", sidechain=True),
    ], complete=True)
    assert [item["data"] for item in items] == [
        {"record_key": "m1", "observed_at": "2026-10-07T06:00:01Z", "model": "claude-fable-5-1"},
    ]


def test_codex_identity_fixtures() -> None:
    old = native_provenance("codex", _codex_rollout(account=None, cli="0.156.0"), complete=True)
    new = native_provenance("codex", _codex_rollout(), complete=True)
    assert _identity(old) == {"account_id": None, "account_source": "unknown", "conflict": 0, "cli_version": "0.156.0"}
    assert _identity(new) == {"account_id": CODEX_ACCOUNT, "account_source": "transcript", "conflict": 0,
                              "cli_version": "0.160.0"}


def test_identity_conflict_is_sticky_against_later_known_replay(tmp_path: Path) -> None:
    async def run() -> None:
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        try:
            _seed_ledger(db, [(HOST, "claude", "native-claude", "m1"), (HOST, "claude", "native-claude", "m2")])
            sink = ProvenanceSink(store.record_provenance, None)

            async def push(records: list[dict], complete: bool = True) -> dict:
                return await sink.admit(HOST, {"version": 1, "items": native_provenance("claude", records, complete=complete)})

            def identity() -> tuple:
                with sqlite3.connect(db) as conn:
                    return conn.execute("SELECT account_id, account_source, conflict, cli_version "
                                        "FROM v2_usage_identity").fetchone()

            await push([_claude_assistant("m1")])                       # unknown first
            assert identity() == (None, "unknown", 0, "2.1.292")
            await push([_credential(ORG_A), _claude_assistant("m1")])   # null filled while conflict=0
            assert identity() == (ORG_A, "transcript", 0, "2.1.292")
            await push([_credential(ORG_B), _claude_assistant("m2")], complete=False)  # a second org: conflict
            assert identity() == (None, "transcript", 1, "2.1.292")
            for records in ([_credential(ORG_A), _claude_assistant("m1")], [_credential(ORG_B), _claude_assistant("m2")]):
                await push(records)                                      # later known replays
                assert identity() == (None, "transcript", 1, "2.1.292")
        finally:
            store.stop()

    asyncio.run(run())


def test_claude_record_upsert_is_null_fill_only(tmp_path: Path) -> None:
    async def run() -> None:
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        try:
            _seed_ledger(db, [(HOST, "claude", "native-claude", "m1")])
            sink = ProvenanceSink(store.record_provenance, None)
            item = native_provenance("claude", [_claude_assistant("m1")])[0]
            blank = {**item, "data": {**item["data"], "model": None}}
            assert (await sink.admit(HOST, {"version": 1, "items": [blank]}))["counts"] == {"recorded": 1}
            assert (await sink.admit(HOST, {"version": 1, "items": [item]}))["counts"] == {"recorded": 1}  # fill
            other = {**item, "data": {**item["data"], "model": "claude-sonnet-5-5"}}
            assert (await sink.admit(HOST, {"version": 1, "items": [other]}))["counts"] == {"replayed": 1}
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT model FROM v2_usage_provenance").fetchall() == [("claude-opus-5-5",)]
        finally:
            store.stop()

    asyncio.run(run())


# --- AC4: Codex per-response detail ---------------------------------------

def test_codex_responses_live_then_backfill_and_conflicts(tmp_path: Path) -> None:
    async def run() -> None:
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        try:
            _seed_ledger(db, [(HOST, "codex", "native-codex", "cumulative")])
            sink = ProvenanceSink(store.record_provenance, HistoryLog(tmp_path / "usage_history.jsonl"))
            records = _codex_rollout()
            # Live: three spans with cross-span state, identity seeded from the first.
            state: dict = {}
            live = []
            for span in (records[:4], records[4:7], records[7:]):
                live += native_provenance("codex", span, native_session_id="native-codex", state=state)
            live_responses = [item for item in live if item["kind"] == "codex_response"]
            assert (await sink.admit(HOST, {"version": 1, "items": live_responses}))["counts"] == {"recorded": 3}
            backfill = native_provenance("codex", records, complete=True)
            replay = await sink.admit(HOST, {"version": 1, "items": [i for i in backfill if i["kind"] == "codex_response"]})
            assert replay["counts"] == {"replayed": 3}
            differing = dict(live_responses[0], data={**live_responses[0]["data"], "output": 31})
            assert (await sink.admit(HOST, {"version": 1, "items": [differing]}))["counts"] == {"response_conflict": 1}
            # A live span that starts mid-turn (no turn_context yet) defers to backfill.
            midturn = native_provenance("codex", records[5:6], native_session_id="native-codex", state={})
            assert midturn == []
            # Resumed/forked rollout repeating a response id yields one row.
            forked = native_provenance("codex", records + records[4:6], complete=True)
            assert sum(item["kind"] == "codex_response" for item in forked) == 3
            with sqlite3.connect(db) as conn:
                rows = conn.execute("SELECT response_id, model, observed_at, output, reasoning_output "
                                    "FROM v2_usage_codex_responses ORDER BY response_id").fetchall()
                assert rows == [
                    ("resp-0", "gpt-6-luna-0", "2026-10-07T06:00:05Z", 30, 10),
                    ("resp-1", "gpt-6-luna-1", "2026-10-07T06:01:05Z", 30, 11),
                    ("resp-2", "gpt-6-luna-2", "2026-10-07T06:02:05Z", 30, 12),
                ]
                assert all(reasoning <= output for *_rest, output, reasoning in rows)
                assert conn.execute("SELECT account_id, conflict FROM v2_usage_identity").fetchall() == [(CODEX_ACCOUNT, 0)]
                assert conn.execute("SELECT tokens FROM v2_usage_records").fetchall() == [(json.dumps({"output": 1}),)]
        finally:
            store.stop()

    asyncio.run(run())


# --- AC5: history lines ----------------------------------------------------

def _claude_json(*, fetched: object = 1791348948799, cache_account: str = "acct-1", oauth_account: str = "acct-1",
                 org: str = ORG_A, fable_style: str = "limits") -> dict:
    utilization: dict = {"seven_day": {"utilization": 45, "resets_at": "2026-10-11T07:00:00.463938+00:00"}}
    if fable_style == "limits":
        utilization["limits"] = [{"kind": "weekly_scoped", "percent": 24, "resets_at": "2026-10-11T07:00:00.464109+00:00",
                                  "scope": {"model": {"display_name": "Fable"}}}]
    else:
        utilization["seven_day_fable"] = {"utilization": 24, "resets_at": "2026-10-11T07:00:00Z"}
    cache = {"accountUuid": cache_account, "utilization": utilization}
    if fetched is not None:
        cache["fetchedAtMs"] = fetched
    return {"cachedUsageUtilization": cache,
            "oauthAccount": {"accountUuid": oauth_account, "organizationUuid": org, "accessToken": "never-copied"}}


def test_cache_reader_same_observation_lines() -> None:
    lines = claude_cache_lines(_claude_json(), host="thoth", probed_at="2026-10-07T05:00:00Z")
    assert iso_from_ms(1791348948799) == "2026-10-07T04:55:48.799Z"
    assert [(line["window_kind"], line["pct"], line["account_id"], line["observed_at"], line["resets_at"])
            for line in lines] == [
        ("seven_day", 45, ORG_A, "2026-10-07T04:55:48.799Z", "2026-10-11T07:00:00.463Z"),
        ("seven_day_fable", 24, ORG_A, "2026-10-07T04:55:48.799Z", "2026-10-11T07:00:00.464Z"),
    ]
    assert all(set(line) == {"observed_at", "probed_at", "host", "provider", "account_id", "window_kind",
                             "window_minutes", "pct", "resets_at", "source"} for line in lines)
    assert all(line["source"] == "cache" and line["window_minutes"] == 10080 for line in lines)
    assert "never-copied" not in json.dumps(lines)
    mismatch = claude_cache_lines(_claude_json(oauth_account="acct-2"), host="thoth", probed_at="x")
    assert [(line["account_id"], line["observed_at"]) for line in mismatch] == [
        (None, "2026-10-07T04:55:48.799Z"), (None, "2026-10-07T04:55:48.799Z")]
    for fetched in (None, 0, -5, "1791348948799", True):
        assert claude_cache_lines(_claude_json(fetched=fetched), host="thoth", probed_at="x") == []
    direct = claude_cache_lines(_claude_json(fable_style="direct"), host="thoth", probed_at="x")
    assert direct[1]["window_kind"] == "seven_day_fable" and direct[1]["resets_at"] == "2026-10-11T07:00:00Z"


def test_history_account_switch_and_dedupe(tmp_path: Path) -> None:
    log = HistoryLog(tmp_path / "usage_history.jsonl")
    first = claude_cache_lines(_claude_json(), host="amaterasu", probed_at="2026-10-07T05:00:00Z")
    assert log.append(first) == [True, True]
    assert log.append(claude_cache_lines(_claude_json(), host="amaterasu", probed_at="2026-10-07T05:05:00Z")) == [False, False]
    switched = claude_cache_lines(_claude_json(fetched=1791349000000, org=ORG_B), host="amaterasu", probed_at="p")
    assert log.append(switched) == [True, True]
    accounts = [json.loads(line)["account_id"] for line in (tmp_path / "usage_history.jsonl").read_text().splitlines()]
    assert accounts == [ORG_A, ORG_A, ORG_B, ORG_B]
    # A second writer instance (another process) sees the first's lines.
    assert HistoryLog(tmp_path / "usage_history.jsonl").append(first) == [False, False]


def test_rollout_lines_window_minutes_and_snapshot_dedupe(tmp_path: Path) -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            path = tmp_path / "usage_history.jsonl"
            sink = ProvenanceSink(store.record_provenance, HistoryLog(path), now=lambda: "2026-10-07T07:00:00Z")
            limits = [item for item in native_provenance("codex", _codex_rollout(), complete=True)
                      if item["kind"] == "rate_limit"]
            assert len(limits) == 2  # 300- and 10080-minute windows; repeated snapshots collapse
            assert (await sink.admit("amaterasu", {"version": 1, "items": limits}))["counts"] == {"recorded": 2}
            assert (await sink.admit("merlin", {"version": 1, "items": limits}))["counts"] == {"replayed": 2}
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            assert lines == [
                {"observed_at": "2026-10-07T06:00:06Z", "probed_at": "2026-10-07T07:00:00Z", "host": "amaterasu",
                 "provider": "codex", "account_id": CODEX_ACCOUNT, "window_kind": "codex",
                 "window_minutes": minutes, "pct": 3, "resets_at": "2026-10-14T03:28:51Z", "source": "rollout"}
                for minutes in (10080, 300)
            ]
        finally:
            store.stop()

    asyncio.run(run())


def test_collector_writes_cache_and_probe_lines(tmp_path: Path) -> None:
    class Result:
        def __init__(self, stdout: str) -> None:
            self.returncode, self.stdout, self.stderr = 0, stdout, ""

    claude_payload = {"week_all_pct": 45, "week_all_resets": "Oct 11, 2am", "week_fable_pct": 24, "week_fable_resets": None}
    codex_payload = {"pct": 3, "resets_text": "Oct 14", "resets_at_iso": "2026-10-14T03:28:51Z",
                     "upstream_reported_at": "2026-10-07T05:00:00Z"}
    outputs = {"claude": json.dumps(claude_payload), "codex": json.dumps(codex_payload)}
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps(_claude_json()))
    collector = UsageStateCollector(
        state_path=tmp_path / "usage_state.json", claude_command=("claude",), codex_command=("codex",),
        run=lambda command, **_kw: Result(outputs[command[0]]), now_fn=lambda: "2026-10-07T05:00:01.000Z",
        claude_config_path=config, host="thoth",
    )
    collector.run_once()
    collector.run_once()
    lines = [json.loads(line) for line in (tmp_path / "usage_history.jsonl").read_text().splitlines()]
    assert [(line["source"], line["provider"], line["window_kind"], line["account_id"]) for line in lines] == [
        ("cache", "claude", "seven_day", ORG_A), ("cache", "claude", "seven_day_fable", ORG_A),
        ("probe", "claude", "seven_day", None), ("probe", "claude", "seven_day_fable", None),
        ("probe", "codex", "codex", None),
        # second cadence: same cache snapshot is not a new observation; probes are
        ("probe", "claude", "seven_day", None), ("probe", "claude", "seven_day_fable", None),
        ("probe", "codex", "codex", None),
    ]
    assert all(line["observed_at"] == line["probed_at"] for line in lines if line["source"] == "probe")
    assert lines[4]["resets_at"] == "2026-10-14T03:28:51Z" and lines[2]["resets_at"] is None


# --- AC7: backfill walker, cursor and idempotence --------------------------

def test_backfill_dry_run_cursor_and_noop_rerun(tmp_path: Path) -> None:
    async def run() -> None:
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        try:
            claude_root = tmp_path / "claude" / "-proj"
            (claude_root / "native-claude" / "subagents").mkdir(parents=True)
            (claude_root / "native-claude.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
                _credential(ORG_A), _claude_assistant("m1"), _claude_assistant("m2")]) + "{torn")
            (claude_root / "native-claude" / "subagents" / "agent-1.jsonl").write_text(
                json.dumps(_claude_assistant("s1", sidechain=True)) + "\n")
            (claude_root / "unknown.jsonl").write_text(json.dumps(_claude_assistant("u1", session="native-unknown")) + "\n")
            codex_root = tmp_path / "codex" / "2026" / "10" / "07"
            codex_root.mkdir(parents=True)
            (codex_root / "rollout-x.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _codex_rollout()))
            (codex_root / "notes.jsonl").write_text("{}\n")
            _seed_ledger(db, [(HOST, "claude", "native-claude", "m1"), (HOST, "claude", "native-claude", "m2"),
                              (HOST, "codex", "native-codex", "cumulative")])
            sink = ProvenanceSink(store.record_provenance, HistoryLog(tmp_path / "usage_history.jsonl"))

            async def push(items, dry_run):
                return await sink.admit(HOST, {"version": 1, "items": items, "dry_run": dry_run})

            roots = {"claude": str(tmp_path / "claude"), "codex": str(tmp_path / "codex")}
            cursor = BackfillCursor(tmp_path / "cursor.json")
            dry = await run_backfill(push, roots=roots, cursor=cursor, dry_run=True, batch=2)
            assert dry["providers"]["claude"]["native_sessions_found"] == 2
            assert dry["providers"]["claude"]["native_sessions_known_in_ledger"] == 1
            assert dry["providers"]["claude"]["native_sessions_unknown_to_ledger"] == 1
            assert dry["providers"]["codex"]["files"] == 1
            assert not (tmp_path / "cursor.json").exists()
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT COUNT(*) FROM v2_usage_provenance").fetchone()[0] == 0
            assert not (tmp_path / "usage_history.jsonl").exists()

            real = await run_backfill(push, roots=roots, cursor=cursor, dry_run=False, batch=2)
            assert real["providers"]["claude"]["recorded"] == 2
            assert real["providers"]["codex"]["recorded"] == 5  # 3 responses + 2 windows
            rerun = await run_backfill(push, roots=roots, cursor=BackfillCursor(tmp_path / "cursor.json"), dry_run=False)
            assert rerun["providers"]["claude"].get("recorded", 0) == 0
            assert rerun["providers"]["claude"]["files_skipped_cursor"] == 3
            fresh = await run_backfill(push, roots=roots, cursor=BackfillCursor(None), dry_run=False)
            assert fresh["providers"]["claude"].get("recorded", 0) == 0
            assert fresh["providers"]["codex"].get("recorded", 0) == 0
            summary = await store.usage_provenance_summary(f"{HOST}:v2-native-claude", "gen-old")
            assert summary["account_id"] == ORG_A and summary["account_source"] == "transcript"
            assert summary["provenance_coverage"] == {"records": 2, "with_provenance": 2}
            codex = await store.usage_provenance_summary(f"{HOST}:v2-native-codex", "gen-old")
            assert codex["provenance_coverage"] == {"records": 1, "with_provenance": 1}
        finally:
            store.stop()

    asyncio.run(run())


def test_file_items_reads_whole_transcript(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in [_credential(ORG_A), {"type": "user", "sessionId": "native-claude"},
                                                            _claude_assistant("m1")]))
    items = file_items("claude", str(path))
    assert len(items) == 1 and items[0]["identity"]["account_id"] == ORG_A
    assert len(items[0]["source_file_identity_digest"]) == 64


@pytest.mark.parametrize("mutate", [
    lambda item: item.pop("identity"),
    lambda item: item["data"].update(extra=1),
    lambda item: item.update(kind="nope"),
    lambda item: item["identity"].update(conflict=True),
    lambda item: item["identity"].update(account_source="transcript", account_id=None),
    lambda item: item.update(source_file_identity_digest="zz"),
])
def test_validate_item_rejects_shape_drift(mutate) -> None:
    item = json.loads(json.dumps(native_provenance("claude", [_claude_assistant("m1")], complete=True)[0]))
    mutate(item)
    assert validate_item(item) in {"bad_provenance", "unsupported_kind"}


# --- live exporters: satellite tail and Thoth-local ingest ------------------

class _LoopbackWs:
    """Delivers satellite frames straight to an EventPush sink."""

    def __init__(self, ep: EventPush) -> None:
        self.ep = ep
        self.frames: list[dict] = []
        self._replies: list[str] = []

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.frames.append(frame)
        self._replies.append(json.dumps(await self.ep.handle_push(frame)))

    async def recv(self) -> str:
        return self._replies.pop(0)


def test_satellite_live_tail_exports_provenance_and_clears_on_ack(tmp_path: Path) -> None:
    from satellite import Satellite, SatelliteConfig, _DiscoveredPane

    async def run() -> None:
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        try:
            _seed_ledger(db, [(HOST, "claude", "native-claude", "m1"), (HOST, "codex", "native-codex", "cumulative")])
            claude_path = tmp_path / "native-claude.jsonl"
            claude_path.write_text("".join(json.dumps(r) + "\n" for r in [_credential(ORG_A), _claude_assistant("m1")]))
            codex_path = tmp_path / "rollout-native-codex.jsonl"
            codex_records = _codex_rollout(responses=2)
            codex_path.write_text("".join(json.dumps(r) + "\n" for r in codex_records))
            # A history horizon that starts after session_meta: identity comes from the head.
            tail_bytes = len("".join(json.dumps(r) + "\n" for r in codex_records[4:]).encode())
            sat = Satellite(SatelliteConfig(host=HOST, checkout=str(tmp_path), host_secret=HOST_SECRET,
                                            push_secret="secret", history_bytes=tail_bytes))
            sat.sha = SATELLITE_SHA
            ep = _event_push(store, HistoryLog(tmp_path / "usage_history.jsonl"))
            ws = _LoopbackWs(ep)
            events, high_water, _capped = sat._collect({
                "v2-claude": _DiscoveredPane("v2-claude", "claude", str(claude_path), pane_pid=11),
                "v2-codex": _DiscoveredPane("v2-codex", "codex", str(codex_path), pane_pid=12),
            })
            kinds = sorted(item["kind"] for item in sat._pending_provenance.values())
            assert kinds == ["claude_record", "codex_response", "rate_limit", "rate_limit"]
            probe = await sat._push(ws, [], {})  # the empty version-2 probe precedes the first data batch
            sat._apply_ack(probe, {})
            assert ws.frames[0]["usage_provenance"] == {"version": 2, "items": []}
            ack = await sat._push(ws, events, high_water)
            sat._apply_ack(ack, high_water)
            assert ws.frames[1]["usage_provenance"]["version"] == 2
            assert ack["usage_provenance"]["counts"] == {"recorded": 4}
            assert sat._pending_provenance == {}
            limits = [json.loads(line) for line in (tmp_path / "usage_history.jsonl").read_text().splitlines()]
            assert {line["account_id"] for line in limits} == {CODEX_ACCOUNT}
            with sqlite3.connect(db) as conn:
                assert sorted(conn.execute("SELECT provider, account_id FROM v2_usage_identity")) == [
                    ("claude", ORG_A), ("codex", CODEX_ACCOUNT)]
            # A whole-block error keeps the batch for the next push.
            sat._queue_provenance(native_provenance("claude", [_claude_assistant("m1")]))
            ep.provenance = None
            ack = await sat._push(ws, [], {})
            sat._apply_ack(ack, {})
            assert ack["usage_provenance"] == {"error": "provenance_unconfigured"}
            assert len(sat._pending_provenance) == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_local_ingest_records_provenance_and_inspect_reports_coverage(tmp_path: Path) -> None:
    from ingest import Ingest, _close_stream
    from server import Server
    from sessions import Sessions

    class Tmux:
        async def capture(self, _name):
            return ""

    async def run() -> None:
        path = tmp_path / "native-claude.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in [
            _credential(ORG_A), _claude_assistant("m1"), _claude_assistant("m2", model="claude-fable-5-1")]))
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        sessions = Sessions(store, local_host="thoth")
        sink = ProvenanceSink(store.record_provenance, HistoryLog(tmp_path / "usage_history.jsonl"))
        ingest = Ingest(store, sessions, Tmux(), lambda _: asyncio.sleep(0), local_host="thoth",
                        recent_limit=20, provenance=sink)
        try:
            await sessions.open("thoth", "v2-local", provider="claude", pane_status="pane_alive", jsonl_path=str(path))
            await ingest.run_pass()
            server = Server(store=store, sessions=sessions)
            server.inventory_ready.set()
            inspected = await server._on_inspect_stream({"stream_id": "thoth:v2-local", "event_tail": 0})
            provenance = inspected["usage_provenance"]
            assert provenance["account_id"] == ORG_A
            assert provenance["account_source"] == "transcript"
            assert provenance["conflict"] == 0
            assert provenance["provenance_coverage"] == {"records": 2, "with_provenance": 2}
            with sqlite3.connect(db) as conn:
                assert sorted(conn.execute("SELECT record_key, model FROM v2_usage_provenance")) == [
                    ("m1", "claude-opus-5-5"), ("m2", "claude-fable-5-1")]
        finally:
            for state in ingest._streams.values():
                _close_stream(state)
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize("fable_style", ["limits", "direct"])
@pytest.mark.parametrize("oauth_account", ["acct-1", "acct-2"])
def test_readback_history_lines_match_collector_format(tmp_path: Path, fable_style: str, oauth_account: str) -> None:
    """agent-orch keeps its own stdlib copy of the cache-line format; pin them together."""
    import importlib.util
    import subprocess
    import sys

    source = Path(__file__).resolve().parents[2] / "agent-orch" / "agent_orch" / "usage_readback.py"
    spec = importlib.util.spec_from_file_location("usage_readback_parity", source)
    readback = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(readback)
    data = _claude_json(oauth_account=oauth_account, fable_style=fable_style)
    (tmp_path / ".claude.json").write_text(json.dumps(data))
    proc = subprocess.run([sys.executable, "-"], input=readback.CLAUDE_CACHE_PLUCK, text=True,
                          capture_output=True, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, check=True)
    plucked = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "never-copied" not in proc.stdout
    ours = claude_cache_lines(data, host="amaterasu", probed_at="2026-10-07T05:00:00Z")
    theirs = readback.claude_cache_history_lines("amaterasu", plucked, probed_at="2026-10-07T05:00:00Z")
    assert len(ours) == 2 and theirs == ours
    assert readback.HISTORY_FIELDS == tuple(ours[0])


def _write_host_transcripts(tmp_path: Path) -> dict[str, str]:
    claude_root = tmp_path / "claude" / "-proj"
    claude_root.mkdir(parents=True)
    (claude_root / "native-claude.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        _credential(ORG_A), _claude_assistant("m1")]))
    codex_root = tmp_path / "codex" / "2026" / "10" / "07"
    codex_root.mkdir(parents=True)
    (codex_root / "rollout-x.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _codex_rollout(responses=1)))
    return {"claude": str(tmp_path / "claude"), "codex": str(tmp_path / "codex")}


def test_thoth_backfill_tool_uses_the_same_sink(tmp_path: Path, capsys) -> None:
    from tools import backfill_usage_provenance as tool

    db = tmp_path / "sessions.db"
    store = Store(str(db))
    store.start()
    store.stop()
    _seed_ledger(db, [("thoth", "claude", "native-claude", "m1"), ("thoth", "codex", "native-codex", "cumulative")])
    roots = _write_host_transcripts(tmp_path)
    common = ["--db", str(db), "--host", "thoth", "--claude-root", roots["claude"], "--codex-root", roots["codex"],
              "--cursor", str(tmp_path / "cursor.json")]
    assert tool.main([*common, "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["providers"]["claude"]["native_sessions_known_in_ledger"] == 1
    assert tool.main(common) == 0
    real = json.loads(capsys.readouterr().out)
    assert real["providers"]["claude"]["recorded"] == 1 and real["providers"]["codex"]["recorded"] == 3
    assert tool.main([*common, "--no-cursor"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert "recorded" not in again["providers"]["claude"] and "recorded" not in again["providers"]["codex"]
    assert len((tmp_path / "usage_history.jsonl").read_text().splitlines()) == 2
    empty = tmp_path / "fresh.db"
    sqlite3.connect(empty).close()
    assert tool.main(["--db", str(empty), "--host", "thoth"]) == 2


def test_satellite_backfill_cli_over_event_push(tmp_path: Path, monkeypatch, capsys) -> None:
    import satellite

    async def run() -> None:
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        try:
            _seed_ledger(db, [(HOST, "claude", "native-claude", "m1"), (HOST, "codex", "native-codex", "cumulative")])
            ep = _event_push(store, HistoryLog(tmp_path / "usage_history.jsonl"))
            roots = _write_host_transcripts(tmp_path)

            class Connect:
                def __init__(self, *_args, **_kwargs):
                    self.ws = _LoopbackWs(ep)

                async def __aenter__(self):
                    return self.ws

                async def __aexit__(self, *_exc):
                    return False

            monkeypatch.setattr(satellite.websockets, "connect", Connect)
            sat = satellite.Satellite(satellite.SatelliteConfig(host=HOST, checkout=str(tmp_path),
                                                                host_secret=HOST_SECRET, push_secret="secret"))
            sat.sha = SATELLITE_SHA
            base = ["--backfill", "--claude-root", roots["claude"], "--codex-root", roots["codex"],
                    "--cursor", str(tmp_path / "cursor.json")]
            dry = await satellite._backfill(sat, satellite._parse_args([*base, "--dry-run"]))
            assert dry["providers"]["claude"]["native_sessions_found"] == 1
            assert dry["providers"]["claude"]["native_sessions_known_in_ledger"] == 1
            real = await satellite._backfill(sat, satellite._parse_args(base))
            assert real["providers"]["codex"]["recorded"] == 3 and real["host"] == HOST
            rerun = await satellite._backfill(sat, satellite._parse_args(base))
            assert rerun["providers"]["claude"]["files_skipped_cursor"] == 1
            assert "recorded" not in rerun["providers"]["codex"]
        finally:
            store.stop()

    asyncio.run(run())


# --- QA 0801faa5 repairs ---------------------------------------------------

def test_codex_state_never_carries_identity_across_native_sessions() -> None:
    """F1: a rebind to another native session starts from a clean identity."""
    state: dict = {}
    first = native_provenance("codex", _codex_rollout(account="acct-A", native="s1", responses=1),
                              native_session_id="s1", state=state)
    second = native_provenance("codex", _codex_rollout(account=None, cli="0.156.0", native="s2", responses=1),
                               native_session_id="s2", state=state)
    assert _identity(first)["account_id"] == "acct-A"
    assert _identity(second) == {"account_id": None, "account_source": "unknown", "conflict": 0, "cli_version": "0.156.0"}
    third = native_provenance("codex", _codex_rollout(account="acct-B", native="s3", responses=1),
                              native_session_id="s3", state=state)
    assert _identity(third)["account_id"] == "acct-B" and _identity(third)["conflict"] == 0


def test_local_ingest_rebind_resets_provenance_state(tmp_path: Path) -> None:
    from ingest import _StreamIngest, Ingest

    seen: list[dict] = []

    class Sink:
        async def admit(self, _host, payload):
            seen.extend(payload["items"])
            return {}

    ingest = Ingest(None, None, None, lambda _: asyncio.sleep(0), local_host="thoth", recent_limit=20, provenance=Sink())
    st = _StreamIngest(path="/a.jsonl", session_id="s1")
    asyncio.run(ingest._record_provenance("thoth:v2-x", "codex", _codex_rollout(account="acct-A", native="s1", responses=1), st))
    st.path, st.session_id = "/b.jsonl", "s2"
    asyncio.run(ingest._record_provenance("thoth:v2-x", "codex", _codex_rollout(account=None, native="s2", responses=1)[1:], st))
    assert [item["identity"] for item in seen if item["kind"] == "codex_response"][-1] is None
    assert {item["data"]["account_id"] for item in seen[-2:] if item["kind"] == "rate_limit"} == {None}


def test_copied_parent_responses_stay_with_their_session() -> None:
    """F4: a token_usage_record naming another session is not this session's."""
    records = _codex_rollout(native="child", responses=3)
    for record in records:
        if record["type"] == "token_usage_record":
            # Real sub-agent rollouts: session_id is the root, thread_id is this rollout.
            record["payload"].update(session_id="root-session", thread_id="child")
    records[2]["payload"]["thread_id"] = "parent"
    responses = [i for i in native_provenance("codex", records, complete=True) if i["kind"] == "codex_response"]
    assert [i["data"]["response_id"] for i in responses] == ["resp-1", "resp-2"]


def test_satellite_provenance_respects_byte_budget_and_unaware_daemon(tmp_path: Path) -> None:
    """F2/F3: provenance never pushes a frame past the WS budget, stops for a
    daemon that ignores it, and an ack never drops a newer replacement."""
    import satellite
    from satellite import Satellite, SatelliteConfig

    sat = Satellite(SatelliteConfig(host=HOST, checkout=str(tmp_path)))
    sat._provenance_version = 2  # past the capability probe
    items = native_provenance("claude", [_claude_assistant(f"m{i}") for i in range(50)], complete=True)
    sat._queue_provenance(items)
    big = {"events": ["x" * (int(satellite.WS_MAX_SIZE * 0.75) - 2000)]}
    sat._attach_provenance(big)
    assert 0 < len(big["usage_provenance"]["items"]) < 50
    assert len(json.dumps(big)) <= int(satellite.WS_MAX_SIZE * 0.75)
    frame: dict = {}
    sat._attach_provenance(frame)
    sent_key = next(iter(sat._inflight_provenance))
    replacement = dict(sat._pending_provenance[sent_key])
    sat._queue_provenance([replacement])
    sat._apply_ack({"type": "event.push.ok", "usage_provenance": {"counts": {}}}, {})
    assert sat._pending_provenance.get(sent_key) is replacement and len(sat._pending_provenance) == 1
    sat._attach_provenance(frame)
    sat._apply_ack({"type": "event.push.ok"}, {})  # an older daemon: no usage_provenance key
    assert sat._provenance_supported is False
    unaware: dict = {}
    sat._attach_provenance(unaware)
    assert "usage_provenance" not in unaware


def test_collector_history_survives_malformed_limits(tmp_path: Path) -> None:
    """F5: a malformed limits value never fails the collector or the cache line."""
    data = _claude_json()
    data["cachedUsageUtilization"]["utilization"]["limits"] = 7
    lines = claude_cache_lines(data, host="thoth", probed_at="p")
    assert [line["window_kind"] for line in lines] == ["seven_day"]
