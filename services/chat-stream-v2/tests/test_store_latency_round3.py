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
from test_store_latency_fixes import _walk

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
