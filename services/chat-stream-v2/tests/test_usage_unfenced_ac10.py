"""AC10: satellite usage for never-fenced and reopened seats.

spec_pentacle__satellite_usage_fence_first_span_race_2026_10, Plan 2 REDs
(a)-(l) and the Target State 5 / 2a fixtures (F1-F15, L1-L9) re-proved on the
product code. Each journey test fails at base f12a0b4 (b406a81 code).
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from event_push import EventPush
from sessions import Sessions
from store import Store

HOST = "worker-one"
HOST_SECRET = "worker-one-host-secret"
SAT_SHA = "a" * 40
PANE = "4242"
SID_A = "11111111-1111-4111-8111-111111111111"


class _Alerts:
    def emit(self, *_args, **_kwargs):
        return None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now(delta_s: float = 0.0) -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(seconds=delta_s))


def _claude(mid: str, ts: str | None, *, sid: str = SID_A, output: int = 20) -> dict:
    record = {
        "type": "assistant", "sessionId": sid, "uuid": f"u-{mid}",
        "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5",
                    "content": [{"type": "text", "text": f"reply {mid}"}],
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0, "output_tokens": output}},
    }
    if ts is not None:
        record["timestamp"] = ts
    return record


class _Loop:
    """Satellite <-> EventPush loopback for both verbs."""

    def __init__(self, ep: EventPush) -> None:
        self.ep = ep
        self.frames: list[dict] = []
        self._replies: list[str] = []

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.frames.append(frame)
        handler = self.ep.handle_host_stats if frame["type"] == "host.stats" else self.ep.handle_push
        self._replies.append(json.dumps(await handler(frame)))

    async def recv(self) -> str:
        return self._replies.pop(0)


class _Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.db = tmp_path / "sessions.db"
        self.store = Store(str(self.db))
        self.store.start()
        self.sessions = Sessions(self.store, local_host="thoth")

        async def stats_handler(_host, _stats):
            return None

        async def secret():
            return "secret"

        from usage_history import HistoryLog
        from usage_provenance import ProvenanceSink
        self.ep = EventPush(self.store, lambda _f: asyncio.sleep(0), _Alerts(), recent_limit=20,
                            sessions=self.sessions, host_secrets={HOST: HOST_SECRET},
                            host_stats_handler=stats_handler,
                            provenance=ProvenanceSink(self.store.record_provenance,
                                                      HistoryLog(tmp_path / "usage_history.jsonl")))
        self.ep._secret = secret
        self.ws = _Loop(self.ep)
        self.sat = self.new_satellite()

    def new_satellite(self):
        from satellite import Satellite, SatelliteConfig
        sat = Satellite(SatelliteConfig(
            host=HOST, checkout=str(self.tmp), host_secret=HOST_SECRET, push_secret="secret",
            history_bytes=-1, held_span_path=str(self.tmp / "held.json")))
        sat.sha = SAT_SHA
        return sat

    async def open(self, name: str, generation: str, provider: str = "claude", pane: str = PANE) -> dict:
        row = await self.sessions.open(
            HOST, name, provider=provider, pane_pid=pane, session_generation=generation,
            observer_binding={"executable": "/usr/bin/claude", "pane_pid": pane, "pane_started_at": "x"})
        time.sleep(0.05)   # a reply is never within the boundary uncertainty of its open
        return row

    async def close(self, name: str, kind: str | None = None) -> None:
        time.sleep(0.05)
        await self.store.mark_closed(HOST, name, closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                     pane_status="pane_alive", close_kind=kind)

    def write(self, name: str, records: list[dict]) -> str:
        path = self.tmp / f"{name}.jsonl"
        with path.open("a") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        return str(path)

    def panes(self, name: str, pane: str = PANE) -> dict:
        from satellite import _DiscoveredPane
        return {name: _DiscoveredPane(name, "claude", str(self.tmp / f"{name}.jsonl"), pane_pid=int(pane))}

    async def stats(self) -> dict:
        return await self.sat._send_host_stats(self.ws)

    async def cycle(self, panes: dict) -> dict:
        self.sat._tick_held()
        events, high_water, _capped = self.sat._collect(panes)
        ack = await self.sat._push(self.ws, events, high_water)
        self.sat._apply_ack(ack, high_water)
        return ack

    async def steady(self) -> None:
        """A satellite already talking to a v2 daemon: clock sample + enable flag."""
        await self.stats()
        await self.cycle({})

    def q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with sqlite3.connect(self.db) as conn:
            return [tuple(row) for row in conn.execute(sql, args)]

    def tokens(self, stream: str, generation: str) -> dict | None:
        rows = self.q("SELECT tokens FROM v2_usage_state WHERE stream_id=? AND generation=?", (stream, generation))
        return json.loads(rows[0][0]) if rows else None

    def stop(self) -> None:
        self.store.stop()


def _run(tmp_path: Path, body) -> None:
    async def go():
        rig = _Rig(tmp_path)
        try:
            await body(rig)
        finally:
            rig.stop()
    asyncio.run(go())


# --- (a) close before first fence: the Merlin journey --------------------------------

def test_a_one_reply_seat_closed_before_first_fence_is_accounted(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.steady()
        assert rig.sat._usage_fences == {}
        await rig.open("v2-short", "gen-a")
        rig.write("v2-short", [_claude("m1", _now())])
        await rig.close("v2-short")
        # The span is read only now: no fence was ever issued for the seat.
        await rig.cycle(rig.panes("v2-short"))
        await rig.cycle(rig.panes("v2-short"))
        assert rig.tokens(f"{HOST}:v2-short", "gen-a") == {
            "uncached_input": 10, "cache_read": 0, "cache_write": 0, "output": 20}
        row = await rig.store.fetch_session(HOST, "v2-short")
        assert row["usage"]["tokens"]["output"] == 20
        assert rig.sat._held_spans().pending_count() == 0
    _run(tmp_path, body)


def test_a_live_never_fenced_reply_is_accounted_with_provenance(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.steady()
        await rig.open("v2-quick", "gen-a")
        rig.write("v2-quick", [_claude("m1", _now())])
        ack = await rig.cycle(rig.panes("v2-quick"))
        assert ack["usage_recorded"] == 1
        # Provenance may follow one frame later (the provenance version probe of
        # a fresh satellite process); it then matches the ledger row.
        await rig.cycle({})
        await rig.close("v2-quick")
        row = await rig.store.fetch_session(HOST, "v2-quick")
        assert row["usage"]["tokens"]["output"] == 20
        coverage = await rig.store.usage_provenance_summary(f"{HOST}:v2-quick", "gen-a")
        assert coverage["provenance_coverage"] == {"records": 1, "with_provenance": 1}
    _run(tmp_path, body)


# --- (b) live pane appending after operator_offline_close ---------------------------

def test_b_append_after_offline_close_is_unplaced_outside_all_generations(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.steady()
        await rig.open("v2-off", "gen-a")
        await rig.stats()
        rig.write("v2-off", [_claude("m1", _now())])
        assert (await rig.cycle(rig.panes("v2-off")))["usage_recorded"] == 1
        await rig.close("v2-off", "operator_offline_close")
        time.sleep(0.2)
        rig.write("v2-off", [_claude("m2", _now(2.0), output=99)])
        await rig.cycle(rig.panes("v2-off"))      # stale fence -> session_not_open -> held
        await rig.stats()                         # fences: [] clears the stale fence
        assert rig.sat._usage_fences == {}
        await rig.cycle(rig.panes("v2-off"))
        await rig.cycle(rig.panes("v2-off"))
        assert rig.tokens(f"{HOST}:v2-off", "gen-a")["output"] == 20
        assert rig.q("SELECT record_key, reason FROM v2_usage_unplaced") == [("m2", "outside_all_generations")]
        assert rig.sat._held_spans().pending_count() == 0
    _run(tmp_path, body)


# --- (c) same-name reopen with the same pane pid -----------------------------------

def test_c_gen_a_record_under_gen_b_fence_is_not_credited_to_b(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.steady()
        await rig.open("v2-re", "gen-a")
        await rig.stats()
        a_time = _now()
        rig.write("v2-re", [_claude("m1", a_time)])
        await rig.cycle(rig.panes("v2-re"))
        await rig.close("v2-re")
        time.sleep(0.2)
        await rig.open("v2-re", "gen-b")
        await rig.stats()
        assert rig.sat._usage_fences[f"{HOST}:v2-re"]["session_generation"] == "gen-b"
        # A gen-A record read late, under gen-B's fence (the 3415f7d8 repro).
        rig.write("v2-re", [_claude("m0", _iso(datetime.fromisoformat(a_time.replace("Z", "+00:00"))
                                                - timedelta(milliseconds=500)), output=7)])
        ack = await rig.cycle(rig.panes("v2-re"))
        assert {"stream_id": f"{HOST}:v2-re", "reason": "outside_all_generations", "index": 0,
                "record_key": "m0", "transient": False} in ack["usage_rejected"]
        b = rig.tokens(f"{HOST}:v2-re", "gen-b")
        assert b is None or not b.get("output")
        assert "outside_all_generations" in json.loads(rig.q(
            "SELECT reasons FROM v2_usage_state WHERE generation='gen-b'")[0][0])
        hist = rig.q("SELECT generation, created_at, closed_at FROM v2_session_generation_history "
                     "WHERE session_name='v2-re' ORDER BY created_at")
        assert [row[0] for row in hist] == ["gen-a", "gen-b"]
        assert hist[0][2] is not None and hist[0][2] <= hist[1][1] and hist[1][2] is None
    _run(tmp_path, body)


def test_c_codex_cumulative_for_later_generation_is_ownership_conflict(tmp_path):
    from usage_accounting import native_usage
    import store_usage

    def codex(total):
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": total, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}}}}

    async def body(rig: _Rig) -> None:
        await rig.open("v2-cx", "gen-a", provider="codex")
        await rig.close("v2-cx")
        time.sleep(0.05)
        await rig.open("v2-cx", "gen-b", provider="codex")

        def op(conn):
            rows = {r["generation"]: r for r in conn.execute(
                "SELECT * FROM v2_session_generation_history WHERE session_name='v2-cx'")}
            out = []
            for generation, total in (("gen-a", 100), ("gen-b", 150)):
                with conn:
                    obs, _ = native_usage("codex", codex(total), "native-cx")
                    out.append(store_usage.record_historical_usage_conn(
                        conn, host=HOST, history_row=rows[generation], stream_id=f"{HOST}:v2-cx",
                        provider="codex", native_session_id="native-cx", digest="d" * 64,
                        observations=[obs])["outcomes"])
            return out
        assert await rig.store.submit(op) == [["recorded"], ["ownership_conflict"]]
        # The live-row writer rejects it the same way (the conflict is recorded in reasons).
        row = await rig.store.fetch_session(HOST, "v2-cx")
        outcome = await rig.store.record_usage_checked(
            row, [codex(200)], native_session_id="native-cx", collection_host=HOST)
        assert "ownership_conflict" in outcome["snapshot"]["incomplete_reasons"]
        assert not outcome["recorded"]
    _run(tmp_path, body)


def test_c_unfenced_codex_later_generation_is_surfaced_as_owned_by_prior(tmp_path):
    def codex(total, ts):
        return {"type": "event_msg", "transcript_ts": ts, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total, "cached_input_tokens": 0, "output_tokens": 5,
                                  "reasoning_output_tokens": 0}}}}

    async def body(rig: _Rig) -> None:
        await rig.open("v2-cy", "gen-a", provider="codex")
        a_ts = _now()
        time.sleep(0.1)
        await rig.close("v2-cy")
        time.sleep(0.2)
        await rig.open("v2-cy", "gen-b", provider="codex")
        time.sleep(0.2)
        clock = {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}
        entry = {"key": "k", "stream_id": f"{HOST}:v2-cy", "provider": "codex", "source_pane_pid": PANE,
                 "native_session_id": "native-cy", "source_file_identity_digest": "e" * 64,
                 "records": [{**codex(100, a_ts), "seq": 1, "clock": clock},
                             {**codex(160, _now()), "seq": 2, "clock": clock}]}
        out = await rig.store.record_unfenced(HOST, [entry], None, clock=clock, receipt_now=time.time())
        assert out["recorded"] == [{"key": "k", "seq": 1, "outcome": "recorded"}]
        assert out["rejected"] == [{"key": "k", "seq": 2, "reason": "cumulative_owned_by_prior_generation",
                                    "transient": False}]
        assert rig.q("SELECT record_key, reason FROM v2_usage_unplaced") == [
            ("cumulative", "cumulative_owned_by_prior_generation")]
        assert rig.q("SELECT count(*) FROM v2_usage_codex_responses") == [(0,)]
    _run(tmp_path, body)


# --- (d) idempotent replay ----------------------------------------------------------

def test_d_held_entry_pushed_twice_is_replayed(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.open("v2-rp", "gen-a")
        clock = {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}
        time.sleep(0.1)
        entry = {"key": "k", "stream_id": f"{HOST}:v2-rp", "provider": "claude", "source_pane_pid": PANE,
                 "native_session_id": SID_A, "source_file_identity_digest": "f" * 64,
                 "records": [{**_claude("m1", _now()), "transcript_ts": _now(), "seq": 1, "clock": clock}]}
        first = await rig.store.record_unfenced(HOST, [entry], None, clock=clock, receipt_now=time.time())
        snapshot = rig.q("SELECT * FROM v2_usage_records") + rig.q("SELECT * FROM v2_usage_state")
        second = await rig.store.record_unfenced(HOST, [entry], None, clock=clock, receipt_now=time.time())
        assert first["counts"] == {"recorded": 1, "replayed": 0}
        assert second["counts"] == {"recorded": 0, "replayed": 1}
        assert rig.q("SELECT * FROM v2_usage_records") + rig.q("SELECT * FROM v2_usage_state") == snapshot
    _run(tmp_path, body)


# --- (e) timestamp_missing / naive / legacy v1 -----------------------------------------

def test_e_v2_fenced_records_without_timezone_are_timestamp_missing_and_v1_is_unchanged(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.open("v2-ts", "gen-a")
        time.sleep(0.05)
        row = await rig.store.fetch_session(HOST, "v2-ts")
        clock = {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}
        records = [
            {**_claude("m1", None), "transcript_ts": None},
            {**_claude("m2", None), "transcript_ts": "2026-10-07T10:00:00"},   # naive
            {**_claude("m3", None), "transcript_ts": _now()},
        ]
        outcome = await rig.store.record_usage_checked(
            row, records, native_session_id=SID_A, collection_host=HOST,
            source_file_identity_digest="a" * 64, timing={"clock": clock, "receipt_now": time.time()})
        assert [(r["record_key"], r["reason"]) for r in outcome["refused"]] == [
            ("m1", "timestamp_missing"), ("m2", "timestamp_missing")]
        assert outcome["snapshot"]["tokens"]["output"] == 20
        assert "timestamp_missing" in outcome["snapshot"]["incomplete_reasons"]
        # Legacy (no timing): exactly today's admission, records carry no transcript_ts.
        row = await rig.store.fetch_session(HOST, "v2-ts")
        legacy = await rig.store.record_usage_checked(
            row, [_claude("m4", None)], native_session_id=SID_A, collection_host=HOST,
            source_file_identity_digest="a" * 64)
        assert legacy["recorded"] and "refused" not in legacy
    _run(tmp_path, body)


def test_e_v1_frame_without_clock_keeps_legacy_admission(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.open("v2-leg", "gen-a")
        msg = f"event.push.v1\0{HOST}\0{SAT_SHA}\0{4321}"
        frame = {"type": "event.push", "request_id": "r1", "push_secret": "secret", "satellite_sha": SAT_SHA,
                 "satellite_pid": 4321, "wire_version": 1, "host": HOST, "events": [], "high_water": {},
                 "source_host_proof": hmac.new(HOST_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest(),
                 "usage": [{"stream_id": f"{HOST}:v2-leg", "provider": "claude", "session_generation": "gen-a",
                            "source_pane_pid": PANE, "native_session_id": SID_A,
                            "source_file_identity_digest": "a" * 64,
                            "records": [_claude("m1", None)]}]}
        ack = await rig.ep.handle_push(frame)
        assert ack["usage_recorded"] == 1 and ack["usage_rejected"] == []
        assert "usage_unfenced" not in ack
    _run(tmp_path, body)


# --- (f) satellite restart with a non-empty held-span file ------------------------------

def test_f_restart_reloads_held_spans_and_delivers(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.stats()
        await rig.open("v2-rs", "gen-a")
        rig.write("v2-rs", [_claude("m1", _now())])
        rig.sat._collect(rig.panes("v2-rs"))           # held, then the process dies before any push
        assert rig.sat._held_spans().pending_count() == 1
        rig.sat = rig.new_satellite()                  # restart: a new process, same file
        assert rig.sat._held_spans().pending_count() == 1
        await rig.steady()
        await rig.cycle({})
        assert rig.tokens(f"{HOST}:v2-rs", "gen-a")["output"] == 20
        assert rig.sat._held_spans().pending_count() == 0
    _run(tmp_path, body)


# --- (h) mixed rollout: v1 ack clears the flag, entries retained -------------------------

def test_h_v1_ack_clears_flag_and_retains_entries(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.steady()
        assert rig.sat._unfenced_enabled
        await rig.open("v2-mx", "gen-a")
        rig.write("v2-mx", [_claude("m1", _now())])

        class V1:
            def __init__(self):
                self.frames = []

            async def send(self, raw):
                self.frames.append(json.loads(raw))

            async def recv(self):
                frame = self.frames[-1]
                return json.dumps({"type": "event.push.ok", "request_id": frame["request_id"], "accepted": 0,
                                   "dropped": [], "reopened": [], "inserted": 0, "high_water": frame["high_water"],
                                   "version": {"status": "ok"}, "wire_version": 1, "stale": False,
                                   "usage_recorded": 0, "usage_replayed": 0, "usage_rejected": []})
        v1 = V1()
        events, hw, _ = rig.sat._collect(rig.panes("v2-mx"))
        rig.sat._apply_ack(await rig.sat._push(v1, events, hw), hw)
        assert "usage_unfenced" in v1.frames[0]          # the one post-rollback frame
        assert not rig.sat._unfenced_enabled
        events, hw, _ = rig.sat._collect(rig.panes("v2-mx"))
        rig.sat._apply_ack(await rig.sat._push(v1, events, hw), hw)
        assert "usage_unfenced" not in v1.frames[1] and "usage_unfenced_losses" not in v1.frames[1]
        assert rig.sat._held_spans().pending_count() == 1
    _run(tmp_path, body)


def test_h_push_error_clears_flag():
    from satellite import Satellite, SatelliteConfig
    sat = Satellite(SatelliteConfig(host=HOST, checkout="/nonexistent"))
    sat._unfenced_enabled = True
    sat._apply_ack({"type": "event.push.error", "error": "ingest_failed"}, {})
    assert not sat._unfenced_enabled
    sat._unfenced_enabled = True
    sat._apply_ack({"type": "event.push.ok", "wire_version": 2}, {})   # no block
    assert not sat._unfenced_enabled


# --- (i) daemon-start backfill -----------------------------------------------------------

def test_i_backfill_open_sessions_with_second_precision(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.open("v2-old", "gen-old")
        await rig.open("v2-gone", "gen-gone")
        await rig.close("v2-gone")
        rig.stop()
        with sqlite3.connect(rig.db) as conn:     # as if opened under the previous daemon
            conn.execute("DELETE FROM v2_session_generation_history")
        rig.store = Store(str(rig.db))
        rig.store.start()
        rows = rig.q("SELECT session_name, generation, precision, created_at, closed_at "
                     "FROM v2_session_generation_history")
        created = rig.q("SELECT created_at FROM sessions WHERE session_name='v2-old'")[0][0]
        assert rows == [("v2-old", "gen-old", "s", created, None)]
    _run(tmp_path, body)


# --- (j) close -> reopen closer than the boundary uncertainty -----------------------------

@pytest.mark.parametrize("a_closed,b_created,ts,exp_unfenced,exp_fenced", [
    ("10:00:00.000", "10:00:00.020", "10:00:00.010", "generation_overlap", "boundary_uncertain"),   # F11
    ("10:00:00.000", "10:00:00.000", "10:00:00.030", "generation_overlap", "boundary_uncertain"),   # F12
])
def test_j_close_reopen_inside_uncertainty_is_never_credited(tmp_path, a_closed, b_created, ts,
                                                             exp_unfenced, exp_fenced):
    day = "2026-10-07T"

    async def body(rig: _Rig) -> None:
        def seed(conn):
            with conn:
                conn.execute("INSERT INTO v2_session_generation_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (HOST, "v2-j", "gen-a", PANE, "claude", day + "09:50:00.000Z", "ms",
                              day + a_closed + "Z", None, None))
                conn.execute("INSERT INTO v2_session_generation_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (HOST, "v2-j", "gen-b", PANE, "claude", day + b_created + "Z", "ms",
                              None, None, None))
        await rig.store.submit(seed)
        receipt = datetime.fromisoformat(day + "10:05:00+00:00").timestamp()
        clock = {"offset_s": 0.0, "rtt_s": 0.1, "server_now": day + "10:04:55.000Z"}
        entry = {"key": "k", "stream_id": f"{HOST}:v2-j", "provider": "claude", "source_pane_pid": PANE,
                 "native_session_id": SID_A, "source_file_identity_digest": "f" * 64,
                 "records": [{**_claude("m1", None), "transcript_ts": day + ts + "Z", "seq": 1, "clock": clock}]}
        out = await rig.store.record_unfenced(HOST, [entry], None, clock=clock, receipt_now=receipt)
        assert out["rejected"][0]["reason"] == exp_unfenced
        assert rig.q("SELECT reason FROM v2_usage_unplaced") == [(exp_unfenced,)]
        from usage_admission import classify, epoch, generation_from_row, parse_clock

        def fence(conn):
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM v2_session_generation_history WHERE generation='gen-b'").fetchone()
            return generation_from_row(row)
        b = await rig.store.submit(fence)
        assert classify(epoch(day + ts + "Z"), [b], parse_clock(clock), receipt)[0] == exp_fenced
    _run(tmp_path, body)


# --- Target State 5 fixtures F1-F15, one classifier for both paths ------------------------

def test_f1_f15_classifier_fixtures():
    from usage_admission import ClockSample, Generation, classify

    def t(hms):
        return datetime.fromisoformat(f"2026-10-07T{hms}+00:00").timestamp()
    now = t("10:05:00.000")
    s0, s2 = ClockSample(0.0, 0.1, now - 5), ClockSample(2.0, 0.1, now - 5)
    A = Generation("A", t("09:50:00"), "s", t("09:59:58"), "s")
    B = Generation("B", t("10:00:00.000"), "ms")
    A_eq, B_eq = Generation("A", t("09:50:00"), "s", t("10:00:00"), "s"), Generation("B", t("10:00:00"), "s")
    A_ms, B_ms = Generation("A", t("09:50:00"), "ms", t("10:00:00.000")), Generation("B", t("10:00:00.020"), "ms")
    A_sup, B_sup = Generation("A", t("09:50:00"), "ms", t("10:00:00.000")), Generation("B", t("10:00:00.000"), "ms")
    step = ClockSample(-1.5, 0.1, now - 4000)
    rows = [
        ("F1", t("09:59:59.900"), [A, B], B, s0, None, "outside_all_generations", "outside_all_generations"),
        ("F2", t("09:59:59.980"), [A, B], B, s0, None, "boundary_uncertain", "boundary_uncertain"),
        ("F3", t("10:00:00.600"), [A, B], B, s0, None, "credited:B", "credited:B"),
        ("F4", t("09:59:58.700"), [A, B], B, s2, None, "credited:B", "credited:B"),
        ("F5", t("10:00:00.400"), [A_eq, B_eq], B_eq, s0, None, "generation_overlap", "boundary_uncertain"),
        ("F6", t("10:00:00.600"), [A, B], B, None, None, "clock_unavailable", "clock_unavailable"),
        ("F7", t("10:00:00.600"), [A, B], B, ClockSample(0.0, 0.1, now - 601), None,
         "clock_unavailable", "clock_unavailable"),
        ("F8", None, [A, B], B, s0, None, "timestamp_missing", "timestamp_missing"),
        ("F9", t("10:00:00.600"), [], None, s0, None, "no_candidate_generation", None),
        ("F10", t("09:55:00.000"), [A, B], B, s0, None, "credited:A", "outside_all_generations"),
        ("F11", t("10:00:00.010"), [A_ms, B_ms], B_ms, s0, None, "generation_overlap", "boundary_uncertain"),
        ("F12", t("10:00:00.030"), [A_sup, B_sup], B_sup, s0, None, "generation_overlap", "boundary_uncertain"),
        ("F13", now - 0.020, [A, B], B, s0, None, "credited:B", "credited:B"),
        ("F14", t("10:00:01.000"), [A, B], B, s0, step, "boundary_uncertain", "boundary_uncertain"),
        ("F15", t("10:00:05.000"), [A], None, s0, None, "outside_all_generations", None),
    ]

    def fmt(result):
        return result[0] + (f":{result[1]}" if result[1] else "")
    for fid, ts, cands, fence, send, cap, exp_u, exp_f in rows:
        assert fmt(classify(ts, cands, send, now, cap)) == exp_u, fid
        if fence is not None:
            assert fmt(classify(ts, [fence], send, now, cap)) == exp_f, fid


def test_both_paths_call_the_one_classifier(tmp_path, monkeypatch):
    import store_usage
    calls = []
    real = store_usage.classify

    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append((len(args[1]), result[0]))
        return result
    monkeypatch.setattr(store_usage, "classify", spy)

    async def body(rig: _Rig) -> None:
        await rig.open("v2-sp", "gen-a")
        time.sleep(0.05)
        clock = {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}
        row = await rig.store.fetch_session(HOST, "v2-sp")
        await rig.store.record_usage_checked(row, [{**_claude("m1", None), "transcript_ts": _now()}],
                                             native_session_id=SID_A, collection_host=HOST,
                                             source_file_identity_digest="a" * 64,
                                             timing={"clock": clock, "receipt_now": time.time()})
        entry = {"key": "k", "stream_id": f"{HOST}:v2-sp", "provider": "claude", "source_pane_pid": PANE,
                 "native_session_id": SID_A, "source_file_identity_digest": "a" * 64,
                 "records": [{**_claude("m2", None), "transcript_ts": _now(), "seq": 1, "clock": clock}]}
        await rig.store.record_unfenced(HOST, [entry], None, clock=clock, receipt_now=time.time())
        assert calls == [(1, "credited"), (1, "credited")]
    _run(tmp_path, body)


# --- (k) loss bounds and the Target State 2a lifecycle (L1-L9) ------------------------------

class _LossRig:
    """Product HeldSpans (satellite) against the product loss upsert (daemon)."""

    def __init__(self, tmp: Path) -> None:
        from usage_hold import HeldSpans
        self.tmp = tmp
        self.path = tmp / "held.json"
        self.held = HeldSpans(self.path)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        import store_usage
        for ddl in store_usage.DDL:
            self.conn.execute(ddl)
        self.pending = None
        self.backup = None
        self.deleted_after_conflict = False

    def loss(self, key, reason, n):
        state = copy.deepcopy(self.held.state)
        self.held._record_loss(state, key=key, provider="claude", native="n-" + key, reason=reason,
                               records=n, nbytes=n * 100, at=time.time())
        self.held._write(state)

    def send(self):
        import store_usage
        with self.conn:
            self.pending = store_usage.upsert_losses_conn(self.conn, HOST, self.held.frame_losses())

    def ack(self):
        before = {loss["loss_id"] for loss in self.held.state["losses"]}
        self.held.apply_ack({}, self.pending["recorded"], self.pending["conflict"])
        after = {loss["loss_id"] for loss in self.held.state["losses"]}
        if any(c["loss_id"] in before and c["loss_id"] not in after for c in self.pending["conflict"]):
            self.deleted_after_conflict = True
        self.pending = None

    def restart(self):
        from usage_hold import HeldSpans
        self.held = HeldSpans(self.path)

    def do(self, step):
        op = step[0]
        if op == "loss":
            self.loss(*step[1:])
        elif op == "send":
            self.send()
        elif op == "ack":
            self.ack()
        elif op == "drop_ack":
            self.pending = None
        elif op == "restart":
            self.restart()
        elif op == "backup":
            self.backup = self.path.read_bytes() if self.path.exists() else None
        elif op == "restore":
            if self.backup is None:
                self.path.unlink()
            else:
                self.path.write_bytes(self.backup)
            self.restart()
        elif op == "drain":
            for _ in range(3):
                self.send()
                self.ack()

    def readback(self) -> dict:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-orch"))
        from agent_orch.usage_unplaced import summarize
        return summarize(self.conn)


def _loss_case(tmp_path, steps):
    rig = _LossRig(tmp_path)
    if not any(step[0] in {"backup"} for step in steps[:1]):
        pass
    for step in steps:
        rig.do(step)
    summary = rig.readback()
    counted = sum(summary["losses"].values())
    pending = sorted(c["pending"]["records_lost"] for c in summary["conflicts"])
    return counted, len(summary["conflicts"]), pending, len(rig.held.state["losses"]), rig


T, E = "held_span_expired_ttl", "held_span_capacity_entries"
_FULL = ([("loss", f"k{i}", E, 1) for i in range(70)] + [("loss", "kT", T, 2)]
         + [("loss", f"x{i}", T, 1) for i in range(5)])


@pytest.mark.parametrize("name,steps,expected", [
    ("L1", _FULL + [("drain",)], (77, 0, [], 0)),
    ("L2", [("loss", "k1", T, 3), ("send",), ("loss", "k1", T, 4), ("ack",), ("drain",)], (7, 0, [], 0)),
    ("L3", [("loss", "k1", T, 3), ("send",), ("drop_ack",), ("restart",), ("send",), ("ack",), ("drain",)],
     (3, 0, [], 0)),
    ("L4", [("loss", "k1", T, 3), ("send",), ("ack",), ("loss", "k1", T, 2), ("drain",)], (5, 0, [], 0)),
    ("L5", [("loss", "k1", T, 5), ("send",), ("ack",), ("loss", "k1", T, 1), ("drain",)], (6, 0, [], 0)),
    ("L6", [("backup",), ("loss", "k1", T, 3), ("send",), ("ack",), ("restore",), ("loss", "k1", T, 2),
            ("send",), ("ack",), ("loss", "k2", T, 1), ("drain",)], (4, 1, [2], 1)),
    ("L7", [("loss", "k1", T, 3), ("backup",), ("loss", "k1", T, 4), ("send",), ("ack",), ("restore",),
            ("loss", "k1", T, 2), ("send",), ("ack",), ("drain",)], (7, 1, [5], 1)),
    ("L8", [("loss", "k1", T, 3), ("backup",), ("loss", "k1", T, 2), ("loss", "k1", T, 2), ("send",), ("ack",),
            ("restore",), ("loss", "k1", T, 2), ("send",), ("ack",), ("drain",)], (7, 1, [5], 1)),
    ("L9", [("loss", "k1", T, 3), ("backup",), ("loss", "k1", T, 2), ("loss", "k1", T, 2), ("send",), ("ack",),
            ("restore",), ("loss", "k1", T, 2), ("send",), ("ack",), ("loss", "k1", T, 1), ("loss", "k1", T, 1),
            ("drain",)], (7, 1, [7], 1)),
])
def test_k_loss_lifecycle_fixtures(tmp_path, name, steps, expected):
    counted, conflicts, pending, buffer, rig = _loss_case(tmp_path, steps)
    assert (counted, conflicts, pending, buffer) == expected, name
    assert not rig.deleted_after_conflict, name
    if name == "L1":
        state = _LossRig(tmp_path / "peak")
        (tmp_path / "peak").mkdir(exist_ok=True)
        state = _LossRig(tmp_path / "peak")
        for step in _FULL:
            state.do(step)
        losses = state.held.state["losses"]
        overflow = {loss["reason"]: loss["records_lost"] for loss in losses if loss["key"] == "*"}
        assert len(losses) == 62 and overflow == {E: 10, T: 7}


def test_k_conflict_latch_is_durable_and_excluded_from_counts(tmp_path):
    counted, conflicts, pending, _buffer, rig = _loss_case(tmp_path, [
        ("loss", "k1", T, 3), ("backup",), ("loss", "k1", T, 2), ("loss", "k1", T, 2), ("send",), ("ack",),
        ("restore",), ("loss", "k1", T, 2), ("send",), ("ack",), ("loss", "k1", T, 5), ("drain",)])
    stored = rig.conn.execute("SELECT detail FROM v2_usage_unplaced WHERE record_key LIKE 'loss:%'").fetchall()
    assert [json.loads(row[0])["records_lost"] for row in stored] == [7]   # stored row untouched
    assert counted == 7 and conflicts == 1 and pending == [10]
    conflict_id = rig.conn.execute(
        "SELECT record_key FROM v2_usage_unplaced WHERE reason='loss_conflict'").fetchone()[0]
    assert conflict_id.removeprefix("loss_conflict:") in {loss["loss_id"] for loss in rig.held.state["losses"]}


def test_k_soak_supported_operations(tmp_path):
    import random
    rng = random.Random(20261007)
    reasons = (T, E, "held_span_capacity_bytes", "held_span_overflow_records")
    peak = worst = spurious = 0
    for trial in range(40):
        rig = _LossRig(tmp_path / f"t{trial}") if (tmp_path / f"t{trial}").mkdir() is None else None
        expected = 0
        for _ in range(200):
            op = rng.random()
            if op < 0.55:
                n = rng.randint(1, 5)
                rig.loss(f"k{rng.randint(0, 90)}", rng.choice(reasons), n)
                expected += n
            elif op < 0.75:
                rig.send()
            elif op < 0.88:
                if rig.pending is not None:
                    rig.ack()
            elif op < 0.95:
                rig.pending = None
            else:
                rig.restart()
                rig.pending = None
            peak = max(peak, len(rig.held.state["losses"]))
        rig.do(("drain",))
        summary = rig.readback()
        if sum(summary["losses"].values()) != expected or rig.held.state["losses"]:
            worst += 1
        spurious += len(summary["conflicts"])
    assert peak <= 64 and worst == 0 and spurious == 0


def test_k_each_loss_bound_records_a_loss_in_the_same_write(tmp_path):
    from usage_hold import HeldSpans, MAX_ENTRY_RECORDS
    clock = {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}
    wall = [1_000_000.0]
    held = HeldSpans(tmp_path / "h.json", wall=lambda: wall[0])

    def ident(i):
        return {"stream_id": f"{HOST}:v2-{i}", "provider": "claude", "source_pane_pid": PANE,
                "native_session_id": f"n{i}", "source_file_identity_digest": "a" * 64}

    def rec(i, pad=0):
        return {"type": "assistant", "sessionId": "s", "message": {"id": f"m{i}", "usage": {"output_tokens": 1}},
                "transcript_ts": "2026-10-07T10:00:00.000Z", **({"pad": "x" * pad} if pad else {})}
    # overflow records: 257 distinct records into one entry
    held.hold(ident(0), [rec(i) for i in range(MAX_ENTRY_RECORDS + 1)], clock)
    # bytes: one 70 KiB record
    held.hold(ident(1), [rec(0, pad=70 * 1024)], clock)
    # entries: 65 more entries -> the oldest dropped
    for i in range(2, 70):
        wall[0] += 1
        held.hold(ident(i), [rec(0)], clock)
    # TTL: enabled time beyond 24 h
    wall[0] += 1
    held.tick(enabled=True, elapsed_s=24 * 3600 + 1)
    on_disk = json.loads((tmp_path / "h.json").read_text())
    reasons = {loss["reason"]: loss["records_lost"] for loss in on_disk["losses"] if loss["key"] != "*"}
    by_reason: dict[str, int] = {}
    for loss in on_disk["losses"]:
        by_reason[loss["reason"]] = by_reason.get(loss["reason"], 0) + loss["records_lost"]
    assert by_reason["held_span_overflow_records"] == 1
    assert by_reason["held_span_capacity_bytes"] == 1
    assert by_reason["held_span_capacity_entries"] >= 1
    assert by_reason["held_span_expired_ttl"] >= 1
    assert on_disk["entries"] == {}
    assert reasons  # keyed records carry their entry key
    # Disabled retention: 7 days from write while the flag is clear.
    held2 = HeldSpans(tmp_path / "h2.json", wall=lambda: wall[0])
    held2.hold(ident(0), [rec(0)], clock)
    held2.tick(enabled=False, elapsed_s=6 * 86400)
    assert held2.pending_count() == 1
    wall[0] += 7 * 86400 + 1
    held2.tick(enabled=False, elapsed_s=1)
    assert held2.pending_count() == 0
    assert [loss["reason"] for loss in held2.state["losses"]] == ["held_span_expired_ttl"]


def test_k_loss_ack_is_ordered_per_satellite_process(tmp_path):
    async def body(rig: _Rig) -> None:
        loss = {"loss_id": "ab" * 8 + ":1", "key": "k", "provider": "claude", "native_session_id": "n",
                "reason": T, "coalesced": False, "records_lost": 3, "bytes_lost": 3, "occurrences": 1,
                "first_at": 1.0, "last_at": 1.0, "rev": 1}
        newer = {**loss, "records_lost": 5, "occurrences": 2, "rev": 2}

        def frame(rid, losses):
            msg = f"event.push.v1\0{HOST}\0{SAT_SHA}\0{777}"
            return {"type": "event.push", "request_id": rid, "push_secret": "secret", "satellite_sha": SAT_SHA,
                    "satellite_pid": 777, "wire_version": 2, "host": HOST, "events": [], "high_water": {},
                    "source_host_proof": hmac.new(HOST_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest(),
                    "usage_unfenced_losses": losses}
        # Frame 2 (rev 2) overtakes frame 1 (rev 1) after an ack timeout.
        second = await rig.ep.handle_push(frame(2, [newer]))
        first = await rig.ep.handle_push(frame(1, [loss]))
        assert second["losses_recorded"] == [{"loss_id": loss["loss_id"], "rev": 2}]
        assert first["losses_recorded"] == [] and first["losses_conflict"] == []
        assert rig.q("SELECT count(*) FROM v2_usage_unplaced WHERE reason='loss_conflict'") == [(0,)]
    _run(tmp_path, body)


# --- (l) history retention at the real hook -----------------------------------------------

def test_l_history_retention(tmp_path):
    from retention import RetentionConfig, RetentionJob
    from usage_admission import iso_ms

    async def body(rig: _Rig) -> None:
        now = time.time()
        rows = [("closed-8d", now - 8 * 86400), ("closed-29.9d", now - 29.9 * 86400),
                ("closed-30d+1s", now - 30 * 86400 - 1), ("open-60d", None)]

        def seed(conn):
            with conn:
                for name, closed in rows:
                    conn.execute("INSERT INTO v2_session_generation_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (HOST, name, "g", PANE, "claude", iso_ms(now - 60 * 86400), "ms",
                                  iso_ms(closed) if closed else None, None, None))
        await rig.store.submit(seed)
        result = await RetentionJob(rig.store, RetentionConfig(archive_path=None)).run_pass()
        assert result.history_pruned == 1
        assert sorted(r[0] for r in rig.q("SELECT session_name FROM v2_session_generation_history "
                                          "WHERE host=?", (HOST,))) == ["closed-29.9d", "closed-8d", "open-60d"]
        # A record that needs the pruned row has no candidate (terminal).
        clock = {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}
        entry = {"key": "k", "stream_id": f"{HOST}:closed-30d+1s", "provider": "claude", "source_pane_pid": PANE,
                 "native_session_id": SID_A, "source_file_identity_digest": "a" * 64,
                 "records": [{**_claude("m1", None), "transcript_ts": iso_ms(now - 31 * 86400),
                              "seq": 1, "clock": clock}]}
        out = await rig.store.record_unfenced(HOST, [entry], None, clock=clock, receipt_now=time.time())
        assert out["rejected"][0]["reason"] == "no_candidate_generation"
    _run(tmp_path, body)


# --- history writes -----------------------------------------------------------------------

def test_history_rows_identity_bind_once_and_superseded_open_row(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.open("v2-h", "gen-a")
        await rig.open("v2-h", "gen-b")          # minted over an open row, no close
        rows = rig.q("SELECT generation, precision, created_at, closed_at FROM v2_session_generation_history "
                     "WHERE session_name='v2-h' ORDER BY created_at")
        assert [r[0] for r in rows] == ["gen-a", "gen-b"] and {r[1] for r in rows} == {"ms"}
        assert rows[0][3] == rows[1][2] and rows[1][3] is None
        assert len(rows[1][2]) == len("2026-10-07T10:00:00.000Z")

        def bind(conn):
            import store_usage
            with conn:
                store_usage.history_bind_identity_conn(conn, HOST, "v2-h", "gen-b", native_session_id="n1",
                                                       digest="1" * 64)
                store_usage.history_bind_identity_conn(conn, HOST, "v2-h", "gen-b", native_session_id="n2",
                                                       digest="2" * 64)
        await rig.store.submit(bind)
        assert rig.q("SELECT native_session_id, source_file_identity_digest FROM v2_session_generation_history "
                     "WHERE generation='gen-b'") == [("n1", "1" * 64)]
        await rig.close("v2-h")
        await rig.open("v2-h", "gen-c")
        assert rig.q("SELECT native_session_id FROM v2_session_generation_history WHERE generation='gen-b'") == [
            ("n1",)]
    _run(tmp_path, body)


def test_identity_mismatch_against_history_is_refused(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.open("v2-id", "gen-a")
        time.sleep(0.05)
        clock = {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}

        def entry(native, digest, mid):
            return {"key": native, "stream_id": f"{HOST}:v2-id", "provider": "claude", "source_pane_pid": PANE,
                    "native_session_id": native, "source_file_identity_digest": digest,
                    "records": [{**_claude(mid, None, sid=native), "transcript_ts": _now(), "seq": 1,
                                 "clock": clock}]}
        first = await rig.store.record_unfenced(HOST, [entry("n1", "1" * 64, "m1")], None, clock=clock,
                                                receipt_now=time.time())
        assert first["counts"]["recorded"] == 1
        second = await rig.store.record_unfenced(HOST, [entry("n1", "2" * 64, "m2"), entry("n9", "1" * 64, "m3")],
                                                 None, clock=clock, receipt_now=time.time())
        assert [r["reason"] for r in second["rejected"]] == [
            "source_identity_mismatch", "native_session_identity_mismatch"]
    _run(tmp_path, body)


def test_handoff_successor_gets_its_own_disjoint_history_row(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.open("v2-pred", "gen-p")
        await rig.sessions.open(HOST, "v2-succ", provider="claude", pane_pid="5151", session_generation="gen-s",
                                handoff_from_stream_id=f"{HOST}:v2-pred",
                                observer_binding={"executable": "/usr/bin/claude", "pane_pid": "5151",
                                                  "pane_started_at": "y"})
        rows = rig.q("SELECT session_name, generation, closed_at FROM v2_session_generation_history "
                     "ORDER BY session_name")
        assert rows == [("v2-pred", "gen-p", None), ("v2-succ", "gen-s", None)]
    _run(tmp_path, body)


def test_fenced_clock_unavailable_moves_records_to_the_held_span(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.steady()
        await rig.open("v2-ck", "gen-a")
        await rig.stats()
        # The satellite's sample is fresh by its monotonic clock, but the
        # daemon judges server_now 700 s old by its own clock (guard path).
        rig.sat._clock = {**rig.sat._clock, "server_now": _now(-700)}
        rig.write("v2-ck", [_claude("m1", _now())])
        ack = await rig.cycle(rig.panes("v2-ck"))
        assert {"stream_id": f"{HOST}:v2-ck", "reason": "clock_unavailable", "index": 0, "record_key": "m1",
                "transient": True} in ack["usage_rejected"]
        assert rig.sat._held_spans().pending_count() == 1
        assert rig.tokens(f"{HOST}:v2-ck", "gen-a") is None or not rig.tokens(f"{HOST}:v2-ck", "gen-a")["output"]
        await rig.stats()                      # a fresh sample; the held record is delivered
        await rig.cycle(rig.panes("v2-ck"))
        assert rig.tokens(f"{HOST}:v2-ck", "gen-a")["output"] == 20
        assert rig.sat._held_spans().pending_count() == 0
    _run(tmp_path, body)


# --- final-QA af22e24e repairs ---------------------------------------------------------

def test_qa1_exec_restart_same_pid_is_a_new_loss_ordering_domain(tmp_path):
    async def body(rig: _Rig) -> None:
        def frame(rid, incarnation, loss):
            msg = f"event.push.v1\0{HOST}\0{SAT_SHA}\0{777}"
            return {"type": "event.push", "request_id": rid, "push_secret": "secret", "satellite_sha": SAT_SHA,
                    "satellite_pid": 777, "satellite_incarnation": incarnation, "wire_version": 2, "host": HOST,
                    "events": [], "high_water": {},
                    "source_host_proof": hmac.new(HOST_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest(),
                    "usage_unfenced_losses": [loss]}

        def loss(lid, n):
            return {"loss_id": lid, "key": "k", "provider": "claude", "native_session_id": "n", "reason": T,
                    "coalesced": False, "records_lost": n, "bytes_lost": n, "occurrences": 1,
                    "first_at": 1.0, "last_at": 1.0, "rev": 1}
        first = await rig.ep.handle_push(frame(500, "aaaa", loss("aa" * 8 + ":1", 3)))
        # Same PID after os.execv: request ids restart at 1, new incarnation.
        after = await rig.ep.handle_push(frame(1, "bbbb", loss("bb" * 8 + ":1", 2)))
        assert first["losses_recorded"] and after["losses_recorded"] == [{"loss_id": "bb" * 8 + ":1", "rev": 1}]
        stale = await rig.ep.handle_push(frame(400, "aaaa", loss("aa" * 8 + ":2", 9)))
        assert stale["losses_recorded"] == []          # same incarnation, older frame: not applied
    _run(tmp_path, body)


def test_qa1_satellite_frames_carry_a_per_process_incarnation(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.cycle({})
        first = rig.ws.frames[-1]["satellite_incarnation"]
        rig.sat = rig.new_satellite()
        await rig.cycle({})
        assert first and rig.ws.frames[-1]["satellite_incarnation"] not in ("", first)
    _run(tmp_path, body)


def test_qa2_start_reconciles_history_left_stale_by_a_daemon_without_history(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.open("v2-rb", "gen-a")
        await rig.open("v2-cl", "gen-c")
        rig.stop()
        with sqlite3.connect(rig.db) as conn:
            # A rolled-back daemon reopened v2-rb as gen-b and closed v2-cl, writing no history.
            conn.execute("UPDATE v2_session_generations SET generation='gen-b' WHERE session_name='v2-rb'")
            conn.execute("UPDATE sessions SET created_at='2030-01-01T00:00:00Z' WHERE session_name='v2-rb'")
            conn.execute("UPDATE sessions SET status='closed', closed_at='2030-01-01T00:00:05Z' "
                         "WHERE session_name='v2-cl'")
        rig.store = Store(str(rig.db))
        rig.store.start()
        rows = {r[0]: r[1:] for r in rig.q(
            "SELECT generation, precision, created_at, closed_at FROM v2_session_generation_history")}
        a_created = rows["gen-a"][1]
        assert rows["gen-a"][2] == a_created                          # zero width: records refused
        assert rows["gen-b"] == ("s", "2030-01-01T00:00:00Z", None)   # current generation backfilled
        assert rows["gen-c"][2] == "2030-01-01T00:00:05.000Z"          # closed at the session close
        clock = {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}
        entry = {"key": "k", "stream_id": f"{HOST}:v2-rb", "provider": "claude", "source_pane_pid": PANE,
                 "native_session_id": SID_A, "source_file_identity_digest": "a" * 64,
                 "records": [{**_claude("m1", None), "transcript_ts": _now(), "seq": 1, "clock": clock}]}
        out = await rig.store.record_unfenced(HOST, [entry], None, clock=clock, receipt_now=time.time())
        assert out["rejected"][0]["reason"] == "outside_all_generations"   # never credited to gen-a
    _run(tmp_path, body)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_qa3_non_finite_clocks_are_no_evidence(tmp_path, bad):
    from usage_admission import parse_clock
    assert parse_clock({"offset_s": bad, "rtt_s": 0.01, "server_now": _now()}) is None
    assert parse_clock({"offset_s": 0.0, "rtt_s": bad, "server_now": _now()}) is None

    async def body(rig: _Rig) -> None:
        await rig.open("v2-nan", "gen-a")
        good = {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}
        bad_clock = {"offset_s": bad, "rtt_s": 0.01, "server_now": _now()}
        entry = {"key": "k", "stream_id": f"{HOST}:v2-nan", "provider": "claude", "source_pane_pid": PANE,
                 "native_session_id": SID_A, "source_file_identity_digest": "a" * 64,
                 "records": [{**_claude("m1", None), "transcript_ts": _now(), "seq": 1, "clock": good}]}
        out = await rig.store.record_unfenced(HOST, [entry], None, clock=bad_clock, receipt_now=time.time())
        assert out["rejected"] == [{"key": "k", "seq": 1, "reason": "clock_unavailable", "transient": True}]
        assert rig.tokens(f"{HOST}:v2-nan", "gen-a") is None
    _run(tmp_path, body)


def test_qa4_short_writes_are_completed_and_failures_keep_the_old_file(tmp_path, monkeypatch):
    import usage_hold
    from usage_hold import HeldSpans
    real_write = usage_hold.os.write
    monkeypatch.setattr(usage_hold.os, "write", lambda fd, data: real_write(fd, bytes(data[:7])))
    held = HeldSpans(tmp_path / "h.json")
    ident = {"stream_id": f"{HOST}:v2-w", "provider": "claude", "source_pane_pid": PANE,
             "native_session_id": "n", "source_file_identity_digest": "a" * 64}
    rec = {"type": "assistant", "sessionId": "n", "message": {"id": "m1", "usage": {"output_tokens": 1}},
           "transcript_ts": "2026-10-07T10:00:00.000Z"}
    held.hold(ident, [rec], None)
    assert HeldSpans(tmp_path / "h.json").pending_count() == 1     # complete, parseable file
    good = (tmp_path / "h.json").read_bytes()

    def failing(fd, data):
        real_write(fd, bytes(data[:5]))
        raise OSError("disk full")
    monkeypatch.setattr(usage_hold.os, "write", failing)
    with pytest.raises(OSError):
        held.hold(ident, [{**rec, "message": {"id": "m2", "usage": {"output_tokens": 1}}}], None)
    assert (tmp_path / "h.json").read_bytes() == good
    assert not list(tmp_path.glob(".h.json.*.tmp"))


def test_qa5_rejected_v2_frame_is_retried_once_without_the_fields(tmp_path):
    async def body(rig: _Rig) -> None:
        await rig.steady()
        await rig.open("v2-v1", "gen-a")
        rig.write("v2-v1", [_claude("m1", _now())])

        class StrictV1:
            def __init__(self):
                self.frames = []

            async def send(self, raw):
                self.frames.append(json.loads(raw))

            async def recv(self):
                frame = self.frames[-1]
                if "usage_unfenced" in frame or "usage_unfenced_losses" in frame:
                    return json.dumps({"type": "event.push.error", "request_id": frame["request_id"],
                                       "error": "bad_batch"})
                return json.dumps({"type": "event.push.ok", "request_id": frame["request_id"], "accepted": 0,
                                   "dropped": [], "reopened": [], "inserted": 0, "high_water": frame["high_water"],
                                   "version": {"status": "ok"}, "wire_version": 1, "stale": False,
                                   "usage_recorded": 0, "usage_replayed": 0, "usage_rejected": []})
        peer = StrictV1()
        events, hw, _ = rig.sat._collect(rig.panes("v2-v1"))
        ack = await rig.sat._push_pass(peer, events, hw)
        assert len(peer.frames) == 2 and "usage_unfenced" in peer.frames[0]
        assert "usage_unfenced" not in peer.frames[1] and peer.frames[1]["high_water"] == peer.frames[0]["high_water"]
        assert ack["type"] == "event.push.ok" and not rig.sat._unfenced_enabled
        rig.sat._apply_ack(ack, hw)
        assert rig.sat._held_spans().pending_count() == 1
    _run(tmp_path, body)


@pytest.mark.parametrize("field", ["offset_s", "rtt_s", "transcript_ts"])
def test_qa6_oversized_json_integers_are_malformed_not_fatal(tmp_path, field):
    from usage_admission import classify, parse_clock
    huge = 10 ** 1000
    if field != "transcript_ts":
        assert parse_clock({"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now(), field: huge}) is None
    assert classify(huge, [], None, time.time())[0] == "timestamp_missing"

    async def body(rig: _Rig) -> None:
        await rig.open("v2-big", "gen-a")
        good = {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}
        frame_clock = {**good, field: huge} if field != "transcript_ts" else good
        record = {**_claude("m1", None), "transcript_ts": huge if field == "transcript_ts" else _now(),
                  "seq": 1, "clock": good}
        entry = {"key": "k", "stream_id": f"{HOST}:v2-big", "provider": "claude", "source_pane_pid": PANE,
                 "native_session_id": SID_A, "source_file_identity_digest": "a" * 64, "records": [record]}
        out = await rig.store.record_unfenced(HOST, [entry], None, clock=frame_clock, receipt_now=time.time())
        expected = "timestamp_missing" if field == "transcript_ts" else "clock_unavailable"
        assert [r["reason"] for r in out["rejected"]] == [expected]
    _run(tmp_path, body)
