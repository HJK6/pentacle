"""Identical-answer proofs for the round-2 store-latency decode caches.

Both fixes memoise a per-connection JSON decode that the membership/delivery
read paths repeat every call after the round-1 work. Each test pins the answer
to the uncached path and shows the parse work actually dropped (a counting
subclass of the real cache). They are written to FAIL under the negative-control
mutants named in spec_pentacle__store_latency_round2_decode_caches_2026_11:

  (Fix 1, the membership record_json decode cache, was replaced by round-3 item 4
  decode-on-demand; its proofs live in test_store_latency_round3.py.)

  Fix 2 (v2_tell_deliveries reply):
    * a cache that ignores a changed reply ->
      test_delivery_parse_cache_reparses_only_changed_rows
    * a cache that caches a strict-parse failure instead of re-raising ->
      test_delivery_parse_cache_legacy_row_is_identical
    * a cache that never prunes a deleted delivery ->
      test_delivery_parse_cache_prunes_deleted_rows
"""
from __future__ import annotations

import asyncio
import json

from store import Store
from tell_proof import TellDeliveryParseCache

from test_read_projection_deliveries import corpus
from test_store_latency_fixes import _walk, _page


# --- Fix 2: v2_tell_deliveries reply parse cache ------------------------------

class CountingParseCache(TellDeliveryParseCache):
    def __init__(self) -> None:
        super().__init__()
        self.parses = 0

    def decode(self, tell_id, reply_text):
        entry = self._entries.get(tell_id)
        if entry is None or entry[0] != reply_text:
            self.parses += 1
        return super().decode(tell_id, reply_text)


CORPUS_DELIVERIES = 9  # peer, service, overlap, twin, generation, birth, unrecorded, quoted, own-row


async def _walk_oracle(store):
    await store.submit(lambda c: setattr(c, "delivery_parse_cache", None))
    return await _walk(store, None)


async def _walk_cached(store, counting):
    await store.submit(lambda c: setattr(c, "delivery_parse_cache", counting))
    return await _walk(store, None)


def test_delivery_parse_cache_reused_across_builds():
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            oracle = await _walk_oracle(store)
            counting = CountingParseCache()
            cached = await _walk_cached(store, counting)
            assert cached == oracle
            # Every page rebuilds a fresh ProjectionDeliveries, but each distinct
            # delivery row is parsed exactly once across the whole walk.
            assert counting.parses == CORPUS_DELIVERIES
        finally:
            store.stop()
    asyncio.run(run())


def test_delivery_parse_cache_reparses_only_changed_rows():
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            counting = CountingParseCache()
            await _walk_cached(store, counting)
            assert counting.parses == CORPUS_DELIVERIES
            # Rewrite one delivery's reply in place; its text now differs.
            await store.submit(lambda c: (c.execute(
                "UPDATE v2_tell_deliveries SET reply=? WHERE tell_id='service'",
                (json.dumps({"payload_digest": "service", "delivery": {
                    "tell_id": "service", "to_stream_id": "host-a:proof",
                    "text": "[pentacle-notice:fixture]\nservice input", "proof_generation": "gen",
                    "proof_session_created_at": "2026-10-01T00:00:00Z", "proof_watermark": 0,
                    "proof_watermark_state": "reachable", "request_payload_hash": "service",
                    "sender_kind": "service", "sender_verified": True,
                    "from_stream_id": "host-b:sender", "sender_generation": "sender-gen",
                    "edited": True}}),)), c.commit()))
            cached = await _walk_cached(store, counting)
            assert counting.parses == CORPUS_DELIVERIES + 1   # only the edited row
            assert cached == await _walk_oracle(store)
        finally:
            store.stop()
    asyncio.run(run())


def test_delivery_parse_cache_prunes_deleted_rows():
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            counting = CountingParseCache()
            await _walk_cached(store, counting)
            await store.submit(lambda c: (c.execute(
                "DELETE FROM v2_tell_deliveries WHERE tell_id='twin'"), c.commit()))
            cached = await _walk_cached(store, counting)
            assert "twin" not in counting._entries            # stale entry pruned
            assert cached == await _walk_oracle(store)
        finally:
            store.stop()
    asyncio.run(run())


def test_delivery_parse_cache_legacy_row_is_identical():
    # A duplicate-key reply makes SQLite/Python disagree and flips the read to the
    # legacy path; the cached walk must still equal the uncached walk, and the bad
    # row must not be cached (the strict parse keeps raising).
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            await store.submit(lambda c: (c.execute(
                "INSERT INTO v2_tell_deliveries VALUES('dup',?,1)",
                ('{"delivery":{"to_stream_id":"host-b:other","text":"a","text":"b"}}',)), c.commit()))
            counting = CountingParseCache()
            cached = await _walk_cached(store, counting)
            assert cached == await _walk_oracle(store)
            assert "dup" not in counting._entries
        finally:
            store.stop()
    asyncio.run(run())


# --- Fix 3: durable interim-relief env on the coordinated release --------------

def test_service_config_sets_work_lanes_refresh_min_s():
    from deploy.deploy import SERVICES
    env = dict(SERVICES["chat-streamd-v2"].launchd_environment)
    assert env.get("WORK_LANES_REFRESH_MIN_S") == "10"
    assert env.get("PENTACLE_SPAWN_RESERVATION_TTL_S") == "600"   # preserved
