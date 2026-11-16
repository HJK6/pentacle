"""Store-thread hot paths stay bounded and keep their exact results.

Regression for a daemon store-thread stall: the legacy send-receipt
stamp re-digested the event text once per receipt row, and the lane completion
fact decoded every resolved route of the stream once per lane.
"""

from __future__ import annotations

import asyncio
import json

import store as store_module
import store_work_lane_episodes as episodes
from store import Store, _send_wire_digest
from store_routing import _assistant_route_lane_conn


TARGET = "fixture:hotpath-target"
STREAM = "host-a:assistant"


def _store() -> Store:
    store = Store(":memory:")
    store.start()
    return store


async def _receipt(store: Store, request_id: str, text: str) -> None:
    await store.append_send_receipt(
        to_stream_id=TARGET, request_id=request_id, receipt_id=f"r-{request_id}",
        state="landed", wire_text=text, display_text=f"shown {request_id}",
        attachments=[], delivery="landed", submission_confirmed=True,
    )


def _reference_legacy_receipt(conn, text: str):
    """The pre-hotfix selection: newest current row per request whose digest matches."""
    rows = conn.execute(
        """SELECT r.rowid AS receipt_rowid, r.*
           FROM v2_send_receipts AS r
           JOIN (SELECT request_id, MAX(rowid) AS receipt_rowid FROM v2_send_receipts
                 WHERE to_stream_id=? GROUP BY request_id) AS current
             ON current.receipt_rowid=r.rowid
           ORDER BY r.rowid DESC""", (TARGET,)).fetchall()
    for row in rows:
        if str(row["wire_digest"] or "") and str(row["wire_digest"]) == _send_wire_digest(text):
            return row["request_id"]
    return None


def test_legacy_receipt_stamp_digests_once_and_matches_reference(monkeypatch) -> None:
    body = "large front desk message " + "x" * 4000

    async def go() -> None:
        store = _store()
        try:
            for i in range(1200):
                await _receipt(store, f"noise-{i}", f"{body} {i}")
            await _receipt(store, "target-old", body)   # older match
            await _receipt(store, "superseded", body)   # current row moves away below
            await _receipt(store, "superseded", "a different later attempt")
            await _receipt(store, "target-new", body)   # newest current match
            for i in range(50):
                await _receipt(store, f"tail-{i}", f"{body} tail {i}")
            expected = await store.submit(lambda conn: _reference_legacy_receipt(conn, body))
            assert expected == "target-new"

            calls = []
            real = store_module._send_wire_digest
            monkeypatch.setattr(store_module, "_send_wire_digest",
                                lambda value: calls.append(1) or real(value))
            event = {"stream_id": TARGET, "kind": "USER", "text": body, "request_id": ""}
            stamped = await store.stamp_event_with_send_receipt(event)
            assert stamped["text"] == "shown target-new"
            assert len(calls) == 1

            calls.clear()
            miss = await store.stamp_event_with_send_receipt(
                {"stream_id": TARGET, "kind": "USER", "text": "no such body", "request_id": ""})
            assert miss["text"] == "no such body" and len(calls) == 1
            empty = await store.stamp_event_with_send_receipt(
                {"stream_id": TARGET, "kind": "USER", "text": "\x1b[0m  ", "request_id": ""})
            assert empty["text"] == "\x1b[0m  "
        finally:
            store.stop()

    asyncio.run(go())


_SEQ = iter(range(1, 10**9))


def _insert(conn, table: str, values: dict) -> None:
    """Insert with placeholder values for any other NOT NULL column without a default."""
    row = dict(values)
    for _cid, name, ctype, notnull, default, pk in conn.execute(f"PRAGMA table_info({table})"):
        if name in row or not notnull or default is not None or pk:
            continue
        row[name] = 0 if "INT" in (ctype or "").upper() else f"x-{name}-{next(_SEQ)}"
    cols = ",".join(row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))


def _seed_routes(conn, lanes: list[str], per_lane: int) -> None:
    conn.execute("BEGIN")
    n = 0
    for lane in lanes:
        for j in range(per_lane):
            n += 1
            dispatch = f"d-{lane}-{j}"
            # Alternate the three linkage shapes the predicate accepts or rejects.
            if j % 3 == 0:
                route_json = json.dumps({"lane_id": lane})
            elif j % 3 == 1:
                route_json = "" if j % 2 else json.dumps({"lane_id": "other"})
                _insert(conn, "v2_assistant_composite_operations",
                        {"stream_id": STREAM, "dispatch_id": dispatch, "lane_id": lane,
                         "operation": "lane.admit"})
            else:
                route_json = json.dumps({"lane_id": "unrelated"})
            _insert(conn, "v2_assistant_composite_routes",
                    {"stream_id": STREAM, "dispatch_id": dispatch, "route_json": route_json,
                     "routing_state": "resolved" if n % 5 else "deferred"})
    conn.execute("COMMIT")


def _reference_dispatch_ids(conn, lane_id: str) -> set[str]:
    return {r["dispatch_id"] for r in conn.execute(
        "SELECT * FROM v2_assistant_composite_routes WHERE stream_id=? AND routing_state='resolved'",
        (STREAM,)) if _assistant_route_lane_conn(conn, STREAM, r, lane_id)}


def test_lane_route_linkage_is_one_query_and_identical(monkeypatch) -> None:
    lanes = [f"wl-{i:024x}" for i in range(12)]

    async def go() -> None:
        store = _store()
        try:
            await store.submit(lambda conn: _seed_routes(conn, lanes, 80))
            loads = []
            real_loads = json.loads
            monkeypatch.setattr(episodes.json if hasattr(episodes, "json") else json, "loads",
                                lambda *a, **k: loads.append(1) or real_loads(*a, **k))

            def check(conn):
                for lane in lanes + ["wl-none"]:
                    expected = _reference_dispatch_ids(conn, lane)
                    row = {"stream_id": STREAM, "lane_id": lane, "lead_reported_done": None,
                           "completion_report_id": None}
                    loads.clear()
                    got = episodes.lead_reported_done_conn(conn, row)
                    assert got == (False if expected else None)
                    assert not loads
            await store.submit(check)
        finally:
            store.stop()

    asyncio.run(go())
