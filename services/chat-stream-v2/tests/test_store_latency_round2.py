"""Identical-answer proofs for the round-2 store-latency decode caches.

Both fixes memoise a per-connection JSON decode that the membership/delivery
read paths repeat every call after the round-1 work. Each test pins the answer
to the uncached path and shows the parse work actually dropped (a counting
subclass of the real cache). They are written to FAIL under the negative-control
mutants named in spec_pentacle__store_latency_round2_decode_caches_2026_11:

  Fix 1 (membership record_json):
    * a cache that stores the post-overlay record (freezes observed_at) ->
      test_membership_decode_cache_reapplies_snapshot_overlay
    * a cache that shares the parsed object with callers (caller mutation leaks
      forward) -> test_membership_decode_cache_survives_caller_mutation
    * a cache keyed by spec_id only, ignoring a changed record_json ->
      test_membership_decode_cache_reparses_only_changed_rows
    * a cache that never prunes a deleted row ->
      test_membership_decode_cache_prunes_deleted_rows

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
from store_work_index import effective_membership_conn, WorkObservationDecodeCache
from store_work_lanes import work_lane_rows_conn
from tell_proof import TellDeliveryParseCache

from test_work_lane_episodes import member, seed_observations, NOW, OLD
from test_read_projection_deliveries import corpus
from test_store_latency_fixes import _walk, _page


# --- Fix 1: membership record_json decode cache -------------------------------

class CountingDecodeCache(WorkObservationDecodeCache):
    """Real cache plus a miss (parse) counter; identical behaviour otherwise."""

    def __init__(self) -> None:
        super().__init__()
        self.parses = 0

    def decode(self, spec_id, record_json):
        entry = self._entries.get(spec_id)
        if entry is None or entry[0] != record_json:
            self.parses += 1
        return super().decode(spec_id, record_json)


def _obs(spec_id, status="completed", quality="fresh"):
    item = member(status=status, quality=quality)
    item["spec_id"] = spec_id
    return item


async def _membership(store, *, cache):
    """snapshot["members"] with the given cache bound to the store connection."""
    def op(conn):
        conn.work_decode_cache = cache
        return effective_membership_conn(conn)["members"]
    return await store.submit(op)


async def _set_snapshot_at(store, snapshot_at):
    await store.submit(lambda c: (c.execute(
        "UPDATE v2_work_index_state SET state_json=? WHERE singleton=1",
        (json.dumps({"available": True, "root_configured": True, "snapshot_at": snapshot_at,
                     "last_sweep_at": snapshot_at, "error": None}),)), c.commit()))


def test_membership_decode_cache_matches_uncached_and_reuses_parse():
    async def run():
        store = Store()
        store.start()
        try:
            await seed_observations(store, [_obs("spec_a__x"), _obs("spec_b__y", "in_progress"),
                                            _obs("spec_c__z", "missing", "missing")])
            oracle = await _membership(store, cache=None)
            counting = CountingDecodeCache()
            first = await _membership(store, cache=counting)
            assert counting.parses == 3            # cold: every row parsed once
            second = await _membership(store, cache=counting)
            assert counting.parses == 3            # warm: unchanged rows never reparsed
            assert first == oracle
            assert second == oracle
        finally:
            store.stop()
    asyncio.run(run())


def test_membership_decode_cache_reapplies_snapshot_overlay():
    # A fresh observation's observed_at tracks the CURRENT state snapshot_at even
    # when the record_json text is unchanged; the cache must not freeze it.
    async def run():
        store = Store()
        store.start()
        try:
            await seed_observations(store, [_obs("spec_a__x")])  # fresh, snapshot_at=OLD
            counting = CountingDecodeCache()
            first = await _membership(store, cache=counting)
            assert first["spec_a__x"]["observation"]["observed_at"] == OLD
            await _set_snapshot_at(store, NOW)                   # row text unchanged
            second = await _membership(store, cache=counting)
            assert second["spec_a__x"]["observation"]["observed_at"] == NOW
            assert counting.parses == 1                         # never reparsed
            assert second == await _membership(store, cache=None)
        finally:
            store.stop()
    asyncio.run(run())


def test_membership_decode_cache_reparses_only_changed_rows():
    async def run():
        store = Store()
        store.start()
        try:
            await seed_observations(store, [_obs("spec_a__x"), _obs("spec_b__y", "in_progress")])
            counting = CountingDecodeCache()
            await _membership(store, cache=counting)
            assert counting.parses == 2
            # Change only spec_b's record_json; spec_a's text is unchanged.
            await store.submit(lambda c: (c.execute(
                "INSERT OR REPLACE INTO v2_work_item_observations VALUES (?,?)",
                ("spec_b__y", json.dumps({"member": _obs("spec_b__y", "completed")}))), c.commit()))
            res = await _membership(store, cache=counting)
            assert counting.parses == 3                         # only spec_b reparsed
            assert res == await _membership(store, cache=None)
        finally:
            store.stop()
    asyncio.run(run())


def test_membership_decode_cache_prunes_deleted_rows():
    async def run():
        store = Store()
        store.start()
        try:
            await seed_observations(store, [_obs("spec_a__x"), _obs("spec_b__y")])
            counting = CountingDecodeCache()
            await _membership(store, cache=counting)
            await store.submit(lambda c: (c.execute(
                "DELETE FROM v2_work_item_observations WHERE spec_id='spec_b__y'"), c.commit()))
            res = await _membership(store, cache=counting)
            assert "spec_b__y" not in res
            assert set(counting._entries) == {"spec_a__x"}      # stale entry pruned
            assert res == await _membership(store, cache=None)
        finally:
            store.stop()
    asyncio.run(run())


def test_membership_decode_cache_survives_caller_mutation():
    # A consumer that mutates the returned member/observation must not corrupt the
    # pristine cached parse; the next read is independent and matches the oracle.
    async def run():
        store = Store()
        store.start()
        try:
            await seed_observations(store, [_obs("spec_a__x")])
            counting = CountingDecodeCache()
            first = await _membership(store, cache=counting)
            first["spec_a__x"]["goal_id"] = "injected"
            first["spec_a__x"]["observation"]["observed_at"] = "CORRUPT"
            first["spec_a__x"]["observation"]["quality"] = "CORRUPT"
            second = await _membership(store, cache=counting)
            assert counting.parses == 1                         # pure shell isolation
            assert second["spec_a__x"]["observation"]["quality"] == "fresh"
            assert second["spec_a__x"]["observation"]["observed_at"] == OLD
            assert second.get("spec_a__x", {}).get("goal_id") != "injected"
            assert second == await _membership(store, cache=None)
        finally:
            store.stop()
    asyncio.run(run())


def test_membership_decode_cache_threads_through_work_lane_rows():
    # The real hot caller (work_lane_rows_conn -> effective_membership_conn) picks
    # up the connection-bound cache: a second pass reparses nothing, same rows.
    from test_work_lanes import run as lanes_run

    async def go(env):
        await env.adopt(key="stream:a", state="paused", lead=False, owner="operator",
                        members=["spec_a__x"])
        await seed_observations(env.store, [_obs("spec_a__x")])

        def passes(conn):
            counting = CountingDecodeCache()
            conn.work_decode_cache = counting
            first = work_lane_rows_conn(conn, include_done=True)
            after_first = counting.parses
            second = work_lane_rows_conn(conn, include_done=True)
            return first, second, after_first, counting.parses

        first, second, after_first, after_second = await env.store.submit(passes)
        assert after_first >= 1           # cold pass parsed the member
        assert after_second == after_first  # warm pass reused every parse
        assert first == second

    lanes_run(go)


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
