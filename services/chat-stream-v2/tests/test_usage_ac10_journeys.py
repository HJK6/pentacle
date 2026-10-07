"""AC10 journeys written only against the base (b406a81) public surface.

Each test asserts the fixed behaviour through APIs that exist at base, so at
base it fails on the defect itself (lost span, gen-A record credited to B,
stale fence never cleared) rather than on a missing symbol.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_push import EventPush
from sessions import Sessions
from store import Store

HOST = "worker-one"
HOST_SECRET = "worker-one-host-secret"
PANE = "4242"
SID = "22222222-2222-4222-8222-222222222222"


class _Alerts:
    def emit(self, *_args, **_kwargs):
        return None


def _now(delta_s: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=delta_s)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _reply(mid: str, ts: str, output: int = 20) -> dict:
    return {"type": "assistant", "sessionId": SID, "timestamp": ts, "uuid": f"u-{mid}",
            "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0, "output_tokens": output}}}


class _Loop:
    def __init__(self, ep: EventPush) -> None:
        self.ep, self.frames, self._replies = ep, [], []

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.frames.append(frame)
        handler = self.ep.handle_host_stats if frame["type"] == "host.stats" else self.ep.handle_push
        self._replies.append(json.dumps(await handler(frame)))

    async def recv(self) -> str:
        return self._replies.pop(0)


def _journey(tmp_path: Path, monkeypatch, body) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))   # the satellite's state dir stays in the sandbox

    async def go():
        from satellite import Satellite, SatelliteConfig, _DiscoveredPane
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        sessions = Sessions(store, local_host="thoth")

        async def stats_handler(_host, _stats):
            return None

        async def secret():
            return "secret"
        ep = EventPush(store, lambda _f: asyncio.sleep(0), _Alerts(), recent_limit=20, sessions=sessions,
                       host_secrets={HOST: HOST_SECRET}, host_stats_handler=stats_handler)
        ep._secret = secret
        ws = _Loop(ep)
        sat = Satellite(SatelliteConfig(host=HOST, checkout=str(tmp_path), host_secret=HOST_SECRET,
                                        push_secret="secret", history_bytes=-1))
        sat.sha = "a" * 40

        class J:
            pass
        j = J()
        j.store, j.sat, j.ws, j.db = store, sat, ws, db

        async def open_(name, generation):
            await sessions.open(HOST, name, provider="claude", pane_pid=PANE, session_generation=generation,
                                observer_binding={"executable": "/usr/bin/claude", "pane_pid": PANE,
                                                  "pane_started_at": "x"})
            time.sleep(0.05)

        async def close(name, kind=None):
            time.sleep(0.05)
            await store.mark_closed(HOST, name, closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    pane_status="pane_alive", close_kind=kind)

        def write(name, records):
            with (tmp_path / f"{name}.jsonl").open("a") as fh:
                for record in records:
                    fh.write(json.dumps(record) + "\n")

        def panes(name):
            return {name: _DiscoveredPane(name, "claude", str(tmp_path / f"{name}.jsonl"), pane_pid=int(PANE))}

        async def cycle(found):
            events, high_water, _ = sat._collect(found)
            ack = await sat._push(ws, events, high_water)
            sat._apply_ack(ack, high_water)
            return ack

        def state(stream, generation):
            with sqlite3.connect(db) as conn:
                row = conn.execute("SELECT tokens FROM v2_usage_state WHERE stream_id=? AND generation=?",
                                   (stream, generation)).fetchone()
            return json.loads(row[0]) if row else None

        j.open, j.close, j.write, j.panes, j.cycle, j.state = open_, close, write, panes, cycle, state
        j.stats = lambda: sat._send_host_stats(ws)
        try:
            await body(j)
        finally:
            store.stop()
    asyncio.run(go())


def test_red_a_short_seat_span_reaches_the_ledger(tmp_path, monkeypatch):
    async def body(j):
        await j.stats()
        await j.cycle({})
        await j.open("v2-merlin", "gen-a")
        j.write("v2-merlin", [_reply("m1", _now())])
        await j.close("v2-merlin")
        for _ in range(3):
            await j.cycle(j.panes("v2-merlin"))
        assert j.state(f"{HOST}:v2-merlin", "gen-a") is not None, "span lost: usage.tokens stays null"
        assert j.state(f"{HOST}:v2-merlin", "gen-a")["output"] == 20
    _journey(tmp_path, monkeypatch, body)


def test_red_b_closed_seat_fence_is_cleared_and_append_never_counted(tmp_path, monkeypatch):
    async def body(j):
        await j.stats()
        await j.open("v2-off", "gen-a")
        await j.stats()
        j.write("v2-off", [_reply("m1", _now())])
        await j.cycle(j.panes("v2-off"))
        await j.close("v2-off", "operator_offline_close")
        time.sleep(0.1)
        j.write("v2-off", [_reply("m2", _now(1.0), output=99)])
        await j.stats()
        assert j.sat._usage_fences == {}, "stale fence of the closed row is never cleared"
        for _ in range(3):
            await j.cycle(j.panes("v2-off"))
        assert j.state(f"{HOST}:v2-off", "gen-a")["output"] == 20
        with sqlite3.connect(j.db) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "v2_usage_unplaced" in tables, "the appended record is lost without a trace"
            assert conn.execute("SELECT reason FROM v2_usage_unplaced WHERE record_key='m2'").fetchall() == [
                ("outside_all_generations",)]
    _journey(tmp_path, monkeypatch, body)


def test_red_c_gen_a_record_read_under_gen_b_fence(tmp_path, monkeypatch):
    async def body(j):
        await j.stats()
        await j.open("v2-re", "gen-a")
        await j.stats()
        a_ts = _now()
        j.write("v2-re", [_reply("m1", a_ts)])
        await j.cycle(j.panes("v2-re"))
        await j.close("v2-re")
        time.sleep(0.2)
        await j.open("v2-re", "gen-b")
        await j.stats()
        late = datetime.fromisoformat(a_ts.replace("Z", "+00:00")) - timedelta(milliseconds=300)
        j.write("v2-re", [_reply("m0", late.isoformat(timespec="milliseconds").replace("+00:00", "Z"), 7)])
        await j.cycle(j.panes("v2-re"))
        b = j.state(f"{HOST}:v2-re", "gen-b") or {}
        assert not b.get("output"), f"gen-A record credited to gen B: {b}"
    _journey(tmp_path, monkeypatch, body)


def test_red_h_v1_daemon_ignores_unfenced_fields(tmp_path, monkeypatch):
    """Documents the daemon reaction to one frame carrying the v2 fields: at
    base (v1) it is accepted and the fields are ignored (no ack block); on the
    candidate the ack carries the block."""
    async def body(j):
        import hashlib
        import hmac
        msg = f"event.push.v1\0{HOST}\0{'a' * 40}\0{99}"
        frame = {"type": "event.push", "request_id": 1, "push_secret": "secret", "satellite_sha": "a" * 40,
                 "satellite_pid": 99, "wire_version": 2, "host": HOST, "events": [], "high_water": {},
                 "source_host_proof": hmac.new(HOST_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest(),
                 "usage_unfenced": [], "usage_unfenced_losses": [],
                 "clock": {"offset_s": 0.0, "rtt_s": 0.01, "server_now": _now()}}
        ack = await j.ws.ep.handle_push(frame)
        print("DAEMON_REACTION", json.dumps({k: ack.get(k) for k in ("type", "error", "wire_version")}),
              "usage_unfenced" in ack)
        assert ack["type"] == "event.push.ok"
        assert "usage_unfenced" in ack, "v1 daemon: frame accepted, unfenced fields ignored (no ack block)"
    _journey(tmp_path, monkeypatch, body)
