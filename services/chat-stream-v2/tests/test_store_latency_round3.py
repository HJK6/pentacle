"""Identical-answer proofs for the round-3 front-desk page work.

spec_pentacle__store_latency_round3_frontdesk_page_2026_11. Every change removes
repeated per-request decode/scan work and must return output byte-identical to
the pre-change path. Each item carries three kinds of test:

  * equality   -- full output equals an oracle captured independently of the
                  refactored code (the pinned `read_projection_legacy` module,
                  the unchanged non-cache path, or an inline computation); GREEN
                  on the behaviour-preserving baseline AND on the candidate.
  * counters   -- a counting wrapper proves the work is done once; RED on the
                  baseline (work repeated), GREEN on the candidate.
  * mutants    -- a deliberately broken variant of the reuse (stale-after-write,
                  no-prune, ...) is detected: the same equality/invariant check
                  that passes on the real code FAILS on the mutant.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import read_projection_legacy as legacy
import tell_proof
from store import Store
from submission_events import provider_text_digest as real_digest
from message_envelopes import strip_peer_delivery
from tell_proof import ProjectionDeliveries, TellDeliveryParseCache, retained_proof

from test_read_projection_deliveries import corpus
from test_store_latency_fixes import _walk, _page

BIRTH = "2026-10-01T00:00:00Z"
TARGET = "host-a:px"
GEN = "gen"


def _counting_digest(monkeypatch):
    """Record the argument of every `tell_proof.provider_text_digest` call."""
    calls: list = []

    def counted(value):
        calls.append(value)
        return real_digest(value)

    monkeypatch.setattr(tell_proof, "provider_text_digest", counted)
    return calls


def _row_oracle(conn, target):
    """Delivery rows for `target`, built independently of `ProjectionDeliveries`."""
    out = []
    for row in conn.execute("SELECT tell_id,reply FROM v2_tell_deliveries"):
        payload = json.loads(row["reply"])
        delivery = payload.get("delivery") or {}
        if delivery.get("to_stream_id") == target:
            out.append({"tell_id": row["tell_id"], "payload": payload, "delivery": delivery,
                        "text_digest": real_digest(delivery.get("text"))})
    return out


# --- Item 1: provider_text_digest memoisation --------------------------------

def test_row_digest_equality_across_cache_states():
    # GREEN on baseline and candidate: the per-row text_digest is exactly the
    # digest of each delivery's text, cold, warm, after an UPDATE, after a DELETE.
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)

            def check(conn):
                table = ProjectionDeliveries()
                rows = table.rows_for(conn, "host-a:proof", "input peer")
                assert rows == _row_oracle(conn, "host-a:proof")

            await store.submit(check)           # cold
            await store.submit(check)           # warm
            await store.submit(lambda c: (c.execute(
                "UPDATE v2_tell_deliveries SET reply=? WHERE tell_id='service'",
                (json.dumps({"payload_digest": "service", "delivery": {
                    "tell_id": "service", "to_stream_id": "host-a:proof",
                    "text": "[pentacle-notice:fixture]\nEDITED", "proof_generation": "gen",
                    "proof_session_created_at": BIRTH, "proof_watermark": 0,
                    "proof_watermark_state": "reachable", "request_payload_hash": "service",
                    "sender_kind": "service", "sender_verified": True,
                    "from_stream_id": "host-b:sender", "sender_generation": "sender-gen"}}),)),
                c.commit()))
            await store.submit(check)           # updated reply -> new digest
            await store.submit(lambda c: (c.execute(
                "DELETE FROM v2_tell_deliveries WHERE tell_id='twin'"), c.commit()))
            await store.submit(check)           # deleted row gone
        finally:
            store.stop()
    asyncio.run(run())


def test_row_digest_not_recomputed_for_unchanged_reply(monkeypatch):
    # RED on baseline: every build re-hashes every row's delivery text.
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            calls = _counting_digest(monkeypatch)
            await store.submit(lambda c: ProjectionDeliveries().rows_for(c, "host-a:proof", "x"))
            cold = len(calls)
            assert cold == 9                       # one per delivery row
            await store.submit(lambda c: ProjectionDeliveries().rows_for(c, "host-a:proof", "x"))
            await store.submit(lambda c: ProjectionDeliveries().rows_for(c, "host-a:proof", "x"))
            assert len(calls) == cold              # unchanged rows: zero recomputation
            # An UPDATE recomputes exactly the changed row.
            await store.submit(lambda c: (c.execute(
                "UPDATE v2_tell_deliveries SET reply=? WHERE tell_id='peer'",
                (json.dumps({"payload_digest": "peer", "delivery": {
                    "tell_id": "peer", "to_stream_id": "host-a:proof", "text": "edited peer"}}),)),
                c.commit()))
            await store.submit(lambda c: ProjectionDeliveries().rows_for(c, "host-a:proof", "x"))
            assert len(calls) == cold + 1
        finally:
            store.stop()
    asyncio.run(run())


def test_row_digest_legacy_row_still_uncertain_and_never_cached():
    # GREEN on both: a duplicate-key reply flips the build to the legacy path.
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            await store.submit(lambda c: (c.execute(
                "INSERT INTO v2_tell_deliveries VALUES('dup',?,1)",
                ('{"delivery":{"to_stream_id":"host-b:other","text":"a","text":"b"}}',)), c.commit()))

            def check(conn):
                table = ProjectionDeliveries()
                assert table.rows_for(conn, "host-a:proof", "x") is None
                assert "dup" not in conn.delivery_parse_cache._entries

            await store.submit(check)
        finally:
            store.stop()
    asyncio.run(run())


class _StaleDigestCache(TellDeliveryParseCache):
    """Mutant: keeps the first digest for a tell_id across a changed reply."""

    def decode(self, tell_id, reply_text):
        previous = self._entries.get(tell_id)
        parsed = super().decode(tell_id, reply_text)
        if previous is not None and previous[0] != reply_text:
            self._entries[tell_id][2] = previous[2]
        return parsed


class _NoPruneCache(TellDeliveryParseCache):
    """Mutant: never evicts a deleted delivery."""

    def prune(self, live_tell_ids):
        return None


async def _update_then_delete_check(store, cache):
    """The invariant the real cache holds: digest follows the reply; delete evicts."""
    await store.submit(lambda c: setattr(c, "delivery_parse_cache", cache))

    def table(conn):
        return ProjectionDeliveries().rows_for(conn, "host-a:proof", "x")

    await store.submit(table)
    await store.submit(lambda c: (c.execute(
        "UPDATE v2_tell_deliveries SET reply=? WHERE tell_id='service'",
        (json.dumps({"payload_digest": "service", "delivery": {
            "tell_id": "service", "to_stream_id": "host-a:proof", "text": "EDITED"}}),)),
        c.commit()))
    rows = await store.submit(table)
    assert rows == await store.submit(lambda c: _row_oracle(c, "host-a:proof"))
    await store.submit(lambda c: (c.execute(
        "DELETE FROM v2_tell_deliveries WHERE tell_id='twin'"), c.commit()))
    await store.submit(table)
    assert "twin" not in cache._entries


@pytest.mark.parametrize("cache_factory,detected", [
    (TellDeliveryParseCache, False),
    (_StaleDigestCache, True),     # stale-after-write
    (_NoPruneCache, True),         # no-prune
])
def test_row_digest_mutants_are_detected(cache_factory, detected):
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            if detected:
                with pytest.raises(AssertionError):
                    await _update_then_delete_check(store, cache_factory())
            else:
                await _update_then_delete_check(store, cache_factory())
        finally:
            store.stop()
    asyncio.run(run())


def _seed_proof(conn, text, *, event_digest="__unset__", other_texts=("other one", "other two", "other three"),
                generation=GEN):
    """One session, one delivery D for `text`, one matching tail event, N other rows."""
    conn.execute("INSERT INTO sessions(host,session_name,provider,visibility,status,created_at) "
                 "VALUES('host-a','px','codex','default','open',?)", (BIRTH,))
    conn.execute("INSERT INTO v2_session_generations VALUES('host-a','px',?)", (GEN,))
    peer = strip_peer_delivery(text) != text
    event = {"kind": "TELL" if peer else "USER", "stream_id": TARGET, "text": text, "timestamp": BIRTH,
             "raw": ({"peer_wire_text": text, "sender": "host-b:s", "peer_payload": "p"} if peer else {})}
    if event_digest != "__unset__":
        event["provider_text_digest"] = event_digest
    seq = conn.execute("INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,"
                       "event_ts,recorded_at) VALUES(?,?,?,?,?,?)",
                       (TARGET, BIRTH, "e1", json.dumps(event), BIRTH, 1.0)).lastrowid

    def delivery(tell_id, body, mark):
        return {"payload_digest": mark, "delivery": {
            "tell_id": tell_id, "to_stream_id": TARGET, "text": body, "proof_generation": generation,
            "proof_session_created_at": BIRTH, "proof_watermark": 0, "proof_watermark_state": "reachable",
            "request_payload_hash": mark, "sender_kind": "peer", "sender_verified": True,
            "from_stream_id": "host-b:s", "sender_generation": "sg"}}

    main = delivery("D", text, "dmain")
    conn.execute("INSERT INTO v2_tell_deliveries VALUES('D',?,1)", (json.dumps(main),))
    for index, body in enumerate(other_texts):
        conn.execute("INSERT INTO v2_tell_deliveries VALUES(?,?,1)",
                     (f"O{index}", json.dumps(delivery(f"O{index}", body, f"o{index}"))))
    conn.commit()
    return main, seq


ENVELOPED = "[from host-b:s] [tell:D]\nhello"
PLAIN = "hello plain"


def _retained(conn, payload, *, via_table):
    if via_table:
        table = ProjectionDeliveries()
        table.rows_for(conn, TARGET, payload["delivery"]["text"])  # build outside the counted call
        return table
    return None


@pytest.mark.parametrize("text", [ENVELOPED, PLAIN])
@pytest.mark.parametrize("form", ["raw", "stripped", "absent", "wrong"])
@pytest.mark.parametrize("via_table", [True, False])
def test_retained_proof_equals_legacy_oracle(text, form, via_table):
    # GREEN on baseline and candidate; covers BOTH accepted event-digest forms (the
    # 209 membership check) plus absent/wrong, and the other-row overlap (233).
    digest = {"raw": real_digest(text), "stripped": real_digest(strip_peer_delivery(text)),
              "absent": "__unset__", "wrong": "0" * 64}[form]

    async def run():
        store = Store()
        store.start()
        try:
            payload, _ = await store.submit(lambda c: _seed_proof(c, text, event_digest=digest))

            def check(conn):
                table = _retained(conn, payload, via_table=via_table)
                got = retained_proof(conn, payload, deliveries=table) if via_table else retained_proof(conn, payload)
                assert got == legacy.retained_proof(conn, payload)
                return got

            got = await store.submit(check)
            assert (got is not None) == (form != "wrong")
        finally:
            store.stop()
    asyncio.run(run())


def _classify(calls, text):
    stripped = strip_peer_delivery(text)
    raw = sum(1 for value in calls if value == text)
    strip = sum(1 for value in calls if value == stripped and stripped != text)
    return raw, strip


@pytest.mark.parametrize("form", ["raw", "stripped"])
def test_retained_proof_digests_each_input_representation_once(monkeypatch, form):
    # RED on baseline: raw digest is computed at :209 AND again per overlap row at :233.
    async def run():
        store = Store()
        store.start()
        try:
            text = ENVELOPED
            digest = real_digest(text) if form == "raw" else real_digest(strip_peer_delivery(text))
            payload, _ = await store.submit(lambda c: _seed_proof(c, text, event_digest=digest))

            def call(conn):
                table = _retained(conn, payload, via_table=True)
                calls = _counting_digest(monkeypatch)
                result = retained_proof(conn, payload, deliveries=table)
                return result, calls

            result, calls = await store.submit(call)
            assert result is not None
            assert _classify(calls, text) == (1, 1)      # one raw, one stripped, per call
        finally:
            store.stop()
    asyncio.run(run())


def test_retained_proof_plain_text_has_no_second_digest(monkeypatch):
    # strip_peer_delivery(text) == text: the stripped digest equals the raw one and
    # is never hashed separately. RED on baseline (two hashes of the same text).
    async def run():
        store = Store()
        store.start()
        try:
            payload, _ = await store.submit(lambda c: _seed_proof(c, PLAIN, event_digest=real_digest(PLAIN)))

            def call(conn):
                table = _retained(conn, payload, via_table=True)
                calls = _counting_digest(monkeypatch)
                return retained_proof(conn, payload, deliveries=table), calls

            result, calls = await store.submit(call)
            assert result is not None
            assert sum(1 for value in calls if value == PLAIN) == 1
            assert len(calls) == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_retained_proof_early_return_computes_no_digest(monkeypatch):
    # GREEN on both: a wrong generation returns before any digest comparison.
    async def run():
        store = Store()
        store.start()
        try:
            payload, _ = await store.submit(lambda c: _seed_proof(c, ENVELOPED, event_digest=real_digest(ENVELOPED)))
            payload["delivery"]["proof_generation"] = "other-gen"

            def call(conn):
                calls = _counting_digest(monkeypatch)
                return retained_proof(conn, payload, deliveries=ProjectionDeliveries()), calls

            result, calls = await store.submit(call)
            assert result is None
            assert calls == []
            # No event digest present on the event: the 209 comparison is skipped too.
        finally:
            store.stop()
    asyncio.run(run())


def test_retained_proof_absent_event_digest_hashes_only_for_overlap(monkeypatch):
    # With no stored event digest the 209 comparison never runs: only the raw input
    # digest at the overlap check is needed (once), never the stripped one.
    async def run():
        store = Store()
        store.start()
        try:
            payload, _ = await store.submit(lambda c: _seed_proof(c, ENVELOPED))

            def call(conn):
                table = _retained(conn, payload, via_table=True)
                calls = _counting_digest(monkeypatch)
                return retained_proof(conn, payload, deliveries=table), calls

            result, calls = await store.submit(call)
            assert result is not None
            assert _classify(calls, ENVELOPED) == (1, 0)
        finally:
            store.stop()
    asyncio.run(run())


def test_unchanged_reply_changed_provider_text_keeps_row_digests_recomputes_call_digests(monkeypatch):
    # Row digests follow the REPLY; call digests follow the INPUT text. Two calls
    # on the same unchanged delivery table with different input texts must not
    # share a call digest, and must not rehash any row.
    async def run():
        store = Store()
        store.start()
        try:
            payload, _ = await store.submit(lambda c: _seed_proof(c, ENVELOPED, event_digest=real_digest(ENVELOPED)))

            def build(conn):
                ProjectionDeliveries().rows_for(conn, TARGET, ENVELOPED)

            await store.submit(build)                 # warm the per-row digests
            calls = _counting_digest(monkeypatch)

            altered_text = ENVELOPED + " changed"

            def second(conn):
                altered = json.loads(json.dumps(payload))
                altered["delivery"]["text"] = altered_text
                event = {"kind": "TELL", "stream_id": TARGET, "text": altered_text, "timestamp": BIRTH,
                         "raw": {"peer_wire_text": altered_text, "sender": "host-b:s", "peer_payload": "p"},
                         "provider_text_digest": real_digest(altered_text)}
                conn.execute("INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,"
                             "event_ts,recorded_at) VALUES(?,?,?,?,?,?)",
                             (TARGET, BIRTH, "e2", json.dumps(event), BIRTH, 1.0))
                conn.commit()
                table = ProjectionDeliveries()
                table.rows_for(conn, TARGET, ENVELOPED)
                assert calls == []                    # no row recomputed on the unchanged reply
                # A different provider input is a different call: fresh call digests.
                result = retained_proof(conn, altered, deliveries=table)
                assert result is not None
                return list(calls)

            seen = await store.submit(second)
            assert not any(value == ENVELOPED for value in seen)
            assert _classify(seen, altered_text) == (1, 1)
        finally:
            store.stop()
    asyncio.run(run())


# --- Shared fixture/oracle for items 2 and 3 ----------------------------------
#
# One stream of plain USER inputs. Each input has a retained daemon send receipt
# carrying a pre-input `_input_proof`, so classifying it runs the whole origin
# path: `send_proofs` (item 2: the stream's receipts) feeding `retained_proof`
# (item 3: the tail events after the proof watermark).

import copy
import hashlib
import re

from store import _insert_send_receipt_row, _project_session_events_conn

PAGE_STREAM = "host-a:pg"


def _proof_meta(text, watermark, stream, *, wire_text=None):
    wire = text if wire_text is None else wire_text
    return {"_input_proof": {
        "wire_text": wire, "wire_length": len(text) if isinstance(wire, str) else 1,
        "wire_sha256": hashlib.sha256(text.encode()).hexdigest(), "to_stream_id": stream,
        "proof_generation": GEN, "proof_session_created_at": BIRTH, "proof_watermark": watermark,
        "proof_watermark_state": "reachable", "sender_verified": True, "sender_kind": "operator",
        "from_stream_id": "host-b:op", "sender_generation": "sg"}}


def _add_receipt(conn, stream, i, text, watermark, *, wire_text=None):
    _insert_send_receipt_row(
        conn, to_stream_id=stream, request_id=f"send-{i}", receipt_id=f"rcpt-{i}", state="landed",
        optimistic_id=None, wire_digest=hashlib.sha256(text.encode()).hexdigest(), display_text=text,
        content_kind="text", attachments_json="[]", delivery="landed", submission_confirmed=True,
        reason=None, attempts=1, created_at=BIRTH, from_stream_id=None, actor_stream_id=None,
        actor_trusted=False, meta_json=json.dumps(_proof_meta(text, watermark, stream, wire_text=wire_text)))


def _add_event(conn, stream, i, text, *, request=True):
    event = {"kind": "USER", "stream_id": stream, "text": text, "timestamp": BIRTH}
    if request:
        event.update(request_id=f"send-{i}", receipt_id=f"rcpt-{i}")
    return conn.execute(
        "INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at)"
        " VALUES(?,?,?,?,?,?)", (stream, BIRTH, f"k{i}", json.dumps(event), BIRTH, 1.0)).lastrowid


def _seed_stream(conn, stream=PAGE_STREAM, n=8, *, ladder=False, receipts=True, wire_text=None):
    """n USER inputs; `ladder` gives each receipt the previous event's id as watermark."""
    host, name = stream.split(":")
    conn.execute("INSERT INTO sessions(host,session_name,provider,visibility,status,created_at)"
                 " VALUES(?,?,'codex','default','open',?)", (host, name, BIRTH))
    conn.execute("INSERT INTO v2_session_generations VALUES(?,?,?)", (host, name, GEN))
    ids, previous = [], 0
    for i in range(n):
        text = f"msg {i} for {stream}"
        event_id = _add_event(conn, stream, i, text, request=receipts)
        if receipts:
            _add_receipt(conn, stream, i, text, previous if ladder else 0, wire_text=wire_text)
        ids.append(event_id)
        previous = event_id
    conn.commit()
    return ids


def _legacy_projection(conn, stream):
    """Whole-stream projection through the pinned pre-round-1 origin oracle."""
    rows = conn.execute("SELECT event_id,event_json FROM session_event_tail WHERE stream_id=? "
                        "ORDER BY event_id", (stream,)).fetchall()
    events = [{**json.loads(row["event_json"]), "daemon_seq": row["event_id"]} for row in rows]
    original = tell_proof.input_origin
    tell_proof.input_origin = lambda c, e, i, **kwargs: legacy.input_origin(c, e, i)
    try:
        return _project_session_events_conn(conn, copy.deepcopy(events))
    finally:
        tell_proof.input_origin = original


def _statements(store):
    """Start tracing the store connection; returns (list, stop())."""
    seen: list[str] = []

    async def start():
        await store.submit(lambda c: c.set_trace_callback(seen.append))

    async def stop():
        await store.submit(lambda c: c.set_trace_callback(None))

    return seen, start, stop


def _receipt_scans(statements):
    return sum(1 for s in statements if "v2_send_receipts" in s and "$._input_proof')='object'" in s)


def _tail_reads(statements):
    """(lo, hi) bounds of every retained_proof tail read; hi is None when unbounded."""
    reads = []
    for sql in statements:
        if "SELECT event_id,event_json FROM session_event_tail" not in sql or "event_id>" not in sql:
            continue
        lo = int(re.search(r"event_id>(\d+)", sql).group(1))
        hi = re.search(r"event_id<=(\d+)", sql)
        reads.append((lo, int(hi.group(1)) if hi else None))
    return reads


def _origins(events):
    return [(e["daemon_seq"], e["raw"]["input_provenance"]["kind"]) for e in events]


# --- Item 2: send receipts once per history request ---------------------------

@pytest.mark.parametrize("ladder", [False, True])
@pytest.mark.parametrize("limit", [1, 3, 50])
def test_item2_page_walk_equals_legacy_oracle(ladder, limit):
    # GREEN on baseline and candidate: the paged walk equals the pinned oracle.
    async def run():
        store = Store()
        store.start()
        try:
            await store.submit(lambda c: _seed_stream(c, ladder=ladder))
            oracle = await store.submit(lambda c: _legacy_projection(c, PAGE_STREAM))
            walked = await _walk(store, {}, limit=limit, stream=PAGE_STREAM)
            assert walked == oracle
            assert {kind for _, kind in _origins(walked)} == {"operator"}   # path really exercised
        finally:
            store.stop()
    asyncio.run(run())


def test_item2_two_streams_stay_isolated():
    # GREEN on both: another stream's receipts never classify this stream's inputs.
    async def run():
        store = Store()
        store.start()
        try:
            await store.submit(lambda c: _seed_stream(c, PAGE_STREAM, 5))
            await store.submit(lambda c: _seed_stream(c, "host-a:other", 5, receipts=False))
            for stream in (PAGE_STREAM, "host-a:other"):
                oracle = await store.submit(lambda c, s=stream: _legacy_projection(c, s))
                assert await _walk(store, {}, limit=2, stream=stream) == oracle
        finally:
            store.stop()
    asyncio.run(run())


def test_item2_receipts_read_once_per_unchanged_request():
    # RED on baseline: send_proofs scans the stream's receipts once per input event.
    async def run():
        store = Store()
        store.start()
        try:
            await store.submit(lambda c: _seed_stream(c, n=8))
            statements, start, stop = _statements(store)
            await start()
            walked = await _walk(store, {}, limit=3, stream=PAGE_STREAM)
            await stop()
            assert len(walked) == 8
            assert _receipt_scans(statements) == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_item2_between_pages_write_invalidates_and_matches_fresh_projection():
    # GREEN on baseline; the candidate must rebuild after the intervening write.
    async def run():
        store = Store()
        store.start()
        try:
            # Oldest input (index 0) starts with no receipt, so its origin is unknown.
            def seed(conn):
                ids = _seed_stream(conn, n=6, receipts=False)
                for i in range(1, 6):                      # receipts for 1..5 only
                    conn.execute("UPDATE session_event_tail SET event_json=? WHERE event_id=?", (json.dumps({
                        "kind": "USER", "stream_id": PAGE_STREAM, "text": f"msg {i} for {PAGE_STREAM}",
                        "timestamp": BIRTH, "request_id": f"send-{i}", "receipt_id": f"rcpt-{i}"}), ids[i]))
                    _add_receipt(conn, PAGE_STREAM, i, f"msg {i} for {PAGE_STREAM}", 0)
                conn.commit()
                return ids

            ids = await store.submit(seed)
            cache: dict = {}
            first = await _page(store, None, cache, limit=3, stream=PAGE_STREAM)       # events 3..5
            before = await store.submit(lambda c: _legacy_projection(c, PAGE_STREAM))
            assert first == before[3:]
            assert _origins(before)[0] == (ids[0], "unknown")

            def late_receipt(conn):                          # lands between page 1 and page 2
                conn.execute("UPDATE session_event_tail SET event_json=? WHERE event_id=?", (json.dumps({
                    "kind": "USER", "stream_id": PAGE_STREAM, "text": f"msg 0 for {PAGE_STREAM}",
                    "timestamp": BIRTH, "request_id": "send-0", "receipt_id": "rcpt-0"}), ids[0]))
                _add_receipt(conn, PAGE_STREAM, 0, f"msg 0 for {PAGE_STREAM}", 0)
                conn.commit()

            await store.submit(late_receipt)
            second = await _page(store, first[0]["daemon_seq"], cache, limit=3, stream=PAGE_STREAM)
            after = await store.submit(lambda c: _legacy_projection(c, PAGE_STREAM))
            assert second == after[:3]
            assert _origins(after)[0] == (ids[0], "operator")   # the new receipt is seen
        finally:
            store.stop()
    asyncio.run(run())


def test_item2_write_between_pages_costs_exactly_one_rescan():
    # RED on baseline (one scan per event); candidate: one scan per unchanged generation.
    async def run():
        store = Store()
        store.start()
        try:
            await store.submit(lambda c: _seed_stream(c, n=6))
            statements, start, stop = _statements(store)
            cache: dict = {}
            await start()
            first = await _page(store, None, cache, limit=3, stream=PAGE_STREAM)
            await store.submit(lambda c: (c.execute("INSERT INTO v2_tell_deliveries VALUES('w',?,1)",
                                                    ('{"delivery":{"to_stream_id":"host-z:q","text":"z"}}',)), c.commit()))
            await _page(store, first[0]["daemon_seq"], cache, limit=3, stream=PAGE_STREAM)
            await stop()
            assert _receipt_scans(statements) == 2     # once before the write, once after
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("wire", [5, 1.5, True, "bad\u0000nul"])
def test_item2_uncertain_receipt_disables_reuse_and_matches_oracle(wire):
    # A receipt whose wire_text is not a clean string makes SQLite's instr coercion
    # the authority: reuse is off (one scan per event, as before) and output equal.
    async def run():
        store = Store()
        store.start()
        try:
            def seed(conn):
                _seed_stream(conn, n=4)
                _add_receipt(conn, PAGE_STREAM, 99, "odd", 0, wire_text=wire)
            await store.submit(seed)
            oracle = await store.submit(lambda c: _legacy_projection(c, PAGE_STREAM))
            statements, start, stop = _statements(store)
            await start()
            walked = await _walk(store, {}, limit=2, stream=PAGE_STREAM)
            await stop()
            assert walked == oracle
            assert _receipt_scans(statements) >= 4     # reuse disabled: per-event scans
        finally:
            store.stop()
    asyncio.run(run())


def test_item2_send_proofs_equals_legacy_oracle_per_input():
    # The reader itself: same payloads, same receipt rows (original columns), same order.
    async def run():
        store = Store()
        store.start()
        try:
            await store.submit(lambda c: _seed_stream(c, n=5))

            def check(conn):
                reuse = tell_proof.ProjectionReuse()
                for i in range(5):
                    text = f"prefix {i} msg {i} for {PAGE_STREAM} suffix"
                    mine = [(p, {k: r[k] for k in r.keys() if k != "sql_wire"})
                            for p, r in tell_proof.send_proofs(conn, PAGE_STREAM, text, reuse=reuse)]
                    theirs = [(p, {k: r[k] for k in r.keys()})
                              for p, r in legacy.send_proofs(conn, PAGE_STREAM, text)]
                    assert mine == theirs
                    assert len(mine) == 1
                other = list(tell_proof.send_proofs(conn, "host-a:other", f"msg 1 for {PAGE_STREAM}", reuse=reuse))
                assert other == []

            await store.submit(check)
        finally:
            store.stop()
    asyncio.run(run())


def _one_table_write_between_projections(conn):
    """ONE ProjectionDeliveries, a write between two projections of the same events."""
    table = tell_proof.ProjectionDeliveries()
    rows = conn.execute("SELECT event_id,event_json FROM session_event_tail ORDER BY event_id").fetchall()
    events = [{**json.loads(r["event_json"]), "daemon_seq": r["event_id"]} for r in rows]
    _project_session_events_conn(conn, copy.deepcopy(events), deliveries=table)
    conn.execute("DELETE FROM v2_send_receipts")
    conn.commit()
    got = _project_session_events_conn(conn, copy.deepcopy(events), deliveries=table)
    assert got == _legacy_projection(conn, PAGE_STREAM)


def test_item2_one_table_write_between_projections_matches_oracle():
    # GREEN on baseline and candidate; the mutant below must fail this same check.
    async def run():
        store = Store()
        store.start()
        try:
            await store.submit(lambda c: _seed_stream(c, n=6))
            await store.submit(_one_table_write_between_projections)
        finally:
            store.stop()
    asyncio.run(run())


def _mutant_reuse(kind):
    base = tell_proof.ProjectionReuse

    class NoGuard(base):                 # reuse-not-invalidated-on-write / stale-after-write
        def _fresh(self, conn):
            return None

    class Leaky(base):                   # stream-isolation leak: one slot for every stream
        def receipt_rows(self, conn, target, text):
            first = next(iter(self._receipts), None) if self._receipts else None
            return super().receipt_rows(conn, first if first is not None else target, text)

    return {"no_guard": NoGuard, "leaky": Leaky}[kind]


@pytest.mark.parametrize("mutant", ["no_guard", "leaky"])
def test_item2_mutants_are_detected(monkeypatch, mutant):
    async def run():
        store = Store()
        store.start()
        try:
            monkeypatch.setattr(tell_proof, "ProjectionReuse", _mutant_reuse(mutant))
            if mutant == "no_guard":
                await store.submit(lambda c: _seed_stream(c, n=6))

                await store.submit(_one_table_write_between_projections)
            else:
                await store.submit(lambda c: _seed_stream(c, PAGE_STREAM, 4))
                await store.submit(lambda c: _seed_stream(c, "host-a:other", 4))

                def mixed(conn):         # one table across two streams, as the inventory does
                    table = tell_proof.ProjectionDeliveries()
                    rows = conn.execute("SELECT event_id,event_json FROM session_event_tail ORDER BY event_id").fetchall()
                    events = [{**json.loads(r["event_json"]), "daemon_seq": r["event_id"]} for r in rows]
                    got = _project_session_events_conn(conn, copy.deepcopy(events), deliveries=table)
                    original = tell_proof.input_origin
                    tell_proof.input_origin = lambda c, e, i, **kw: legacy.input_origin(c, e, i)
                    try:
                        want = _project_session_events_conn(conn, copy.deepcopy(events))
                    finally:
                        tell_proof.input_origin = original
                    assert got == want

                await store.submit(mixed)
        finally:
            store.stop()

    # The real class passes the same check (see the equality tests above); the
    # mutant must fail it.
    with pytest.raises(AssertionError):
        asyncio.run(run())


def test_item2_single_writer_assumption_total_changes(tmp_path):
    # The reuse guard is conn.total_changes. It sees every write made on the Store
    # connection (the only writer in the daemon) and, by design, none made by a
    # different connection -- which is why the single-writer assumption is stated.
    import sqlite3
    path = str(tmp_path / "store.db")
    store = Store(path)
    store.start()
    try:
        asyncio.run(_single_writer(store, path, sqlite3))
    finally:
        store.stop()


async def _single_writer(store, path, sqlite3):
    await store.submit(lambda c: _seed_stream(c, n=2))
    before = await store.submit(lambda c: c.total_changes)
    await store.submit(lambda c: _add_receipt(c, PAGE_STREAM, 50, "x", 0))
    assert await store.submit(lambda c: c.total_changes) > before        # same-connection write seen
    mid = await store.submit(lambda c: c.total_changes)
    external = sqlite3.connect(path)
    try:
        external.execute("INSERT INTO v2_tell_deliveries VALUES('ext','{}',1)")
        external.commit()
    finally:
        external.close()
    assert await store.submit(lambda c: c.total_changes) == mid         # external writer invisible
