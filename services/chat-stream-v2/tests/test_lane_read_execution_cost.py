"""Execution-cost regression controls for the two lane readers.

spec_pentacle__lane_read_execution_cost_2026_11. Both changes keep the exact
answer and only remove repeated serial Store work:

  A. The due worked-time census (``WorkLanesInventory`` drain) reads the member
     spec IDs through a light canonical pass instead of building enriched lane
     rows and decoding every member observation.
  B. The coherent single-lane reader (``Store.get_work_lane``) resolves canonical
     membership once and shares that snapshot across both lane reads, the
     filtered projection and the external-packet terminal filter.

Each path carries three kinds of test, as the admission plan requires:

  * equality -- output equals an oracle computed independently of the reuse (the
                legacy enriched union for A; the fresh ``current_packets_conn``
                path and hand-computed membership for B). GREEN on the frozen base
                AND on the candidate.
  * counters -- a counting wrapper proves the work happens once. RED on the base
                (work repeated / enriched rows built), GREEN on the candidate.
  * negative -- a test-local change that restores the offending reader is caught
                by the very assertion the sound test makes.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import store_external_work
import store_work_index
import store_work_lanes
from external_work import ExternalWorkConfig
from store_external_work import current_packets_conn
from store_work_index import effective_membership_conn, effective_membership_ids_conn
from store_work_lanes import work_lane_census_ids_conn
from work_lane_worked import WorkedCache
from work_lanes_projection import WorkLanesInventory

from test_work_lanes import run
from test_work_lane_goals import reconcile, scanner, write_goal, write_item

NO_SESSIONS = SimpleNamespace(list_open=lambda: [])


async def _noop_broadcast(frame):
    pass


# --------------------------------------------------------------------------- #
# Shared fixture: a target lane with a derived-override member and unrelated    #
# open/done lanes, grown with settled observations on independent axes.         #
# --------------------------------------------------------------------------- #

async def _seed_lanes(env, root, *, target_members=40):
    """Return (target_lane_id, legacy_lane_id, done_lane_id). The target carries a
    nonempty goal with ``target_members`` source-derived members, one of which was
    an explicit member of the legacy lane (a derived override of a stale explicit
    assignment). A done lane and an unrelated open lane grow the population."""
    legacy = (await env.adopt(key="request:legacy", state="paused", owner="operator",
                              members=["spec_demo__n00"]))["lane"]
    target = (await env.adopt(key="request:target", state="paused", owner="operator"))["lane"]
    other = (await env.adopt(key="request:other", state="paused", owner="operator"))["lane"]
    done = (await env.adopt(key="request:done", state="paused", owner="operator",
                            members=["spec_demo__d00", "spec_demo__d01"]))["lane"]
    write_goal(root, target["lane_id"], "fixture")
    write_goal(root, other["lane_id"], "other")
    for i in range(target_members):
        write_item(root, f"n{i:02}", epic="epic_fixture")
    # Unrelated settled observations on an independent axis (their own goal).
    for i in range(8):
        write_item(root, f"o{i:02}", epic="epic_other")
    await reconcile(env.store, scanner(root))
    # Close the done lane after its membership is indexed.
    def close(conn):
        conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='done',"
                     "work_state_reason='fd',done_at=updated_at,version=version+1 WHERE lane_id=?",
                     (done["lane_id"],))
        conn.commit()
    await env.store.submit(close)
    return target["lane_id"], legacy["lane_id"], done["lane_id"]


async def _legacy_census_union(store) -> set[str]:
    """Independent oracle: the union the enriched reader produces today."""
    rows = await store.work_lane_rows(include_done=True)
    return {member["spec_id"] for row in rows for member in row.get("_members", [])}


def _resolution_counter(monkeypatch):
    """Count canonical identity resolutions across both imported aliases."""
    calls = []
    real = store_work_index.effective_membership_ids_conn

    def counted(conn):
        calls.append(1)
        return real(conn)

    monkeypatch.setattr(store_work_index, "effective_membership_ids_conn", counted)
    monkeypatch.setattr(store_work_lanes, "effective_membership_ids_conn", counted)
    return calls


def _bulk_counters(monkeypatch):
    """Count both F2 bulk reads across every imported alias: the identity resolver
    (``effective_membership_ids_conn``) and the observation decode view
    (``effective_membership_conn``). Returns a live {'ids', 'decode'} dict."""
    c = {"ids": 0, "decode": 0}
    real_ids = store_work_index.effective_membership_ids_conn
    real_dec = store_work_index.effective_membership_conn

    def cid(conn):
        c["ids"] += 1
        return real_ids(conn)

    def cdec(conn, **kw):
        c["decode"] += 1
        return real_dec(conn, **kw)

    monkeypatch.setattr(store_work_index, "effective_membership_ids_conn", cid)
    monkeypatch.setattr(store_work_lanes, "effective_membership_ids_conn", cid)
    monkeypatch.setattr(store_work_index, "effective_membership_conn", cdec)
    monkeypatch.setattr(store_work_lanes, "effective_membership_conn", cdec)
    monkeypatch.setattr(store_external_work, "effective_membership_conn", cdec, raising=False)
    return c


class _LocalPatch:
    """Install the F2 bulk counters with an explicit undo, so several count
    windows can run inside one connection/transaction."""

    def __init__(self, counts):
        real_ids = store_work_index.effective_membership_ids_conn
        real_dec = store_work_index.effective_membership_conn

        def cid(conn):
            counts["ids"] += 1
            return real_ids(conn)

        def cdec(conn, **kw):
            counts["decode"] += 1
            return real_dec(conn, **kw)

        self._saved = []
        for mod, name, fn in ((store_work_index, "effective_membership_ids_conn", cid),
                              (store_work_lanes, "effective_membership_ids_conn", cid),
                              (store_work_index, "effective_membership_conn", cdec),
                              (store_work_lanes, "effective_membership_conn", cdec)):
            self._saved.append((mod, name, getattr(mod, name)))
            setattr(mod, name, fn)

    def undo(self):
        for mod, name, original in reversed(self._saved):
            setattr(mod, name, original)


def _patch_local(monkeypatch, counts):
    return _LocalPatch(counts)


# Base (67125a37) bulk-read counts, measured by stashing the source and rerunning
# these same counters. The candidate must preserve the answer at a lower cost.
BASE_COUNTS = {
    "get_work_lane_off": {"ids": 3, "decode": 1},
    "get_work_lane_on": {"ids": 4, "decode": 2},
    "current_packets_fresh": {"ids": 1, "decode": 1},
    "census": {"ids": 1, "decode": 1},  # the enriched reader the census used
}


def _decode_recorder(monkeypatch):
    """Record the spec_ids argument of every observation decode."""
    calls = []
    real = store_work_index._decode_observations

    def counted(conn, spec_ids, state):
        calls.append(None if spec_ids is None else list(spec_ids))
        return real(conn, spec_ids, state)

    monkeypatch.setattr(store_work_index, "_decode_observations", counted)
    return calls


async def _build_due_inventory(env, tmp_path):
    """An inventory whose worked census is due, with a recording ledger reader."""
    captured = {}

    def recorder(spec_ids, data_dir, work_root, now):
        captured["ids"] = set(spec_ids)
        return {spec: None for spec in spec_ids}

    inv = WorkLanesInventory(env.store, NO_SESSIONS, _noop_broadcast)
    # A due cache with non-None roots so needs_refresh() fires; the injected
    # reader stands in for the ledger pass and never re-triggers the drain.
    inv.worked = WorkedCache(tmp_path, tmp_path / "work", changed=lambda: None, reader=recorder)
    return inv, captured


async def _drain_once(inv):
    inv.refresh()
    await inv._task
    if inv.worked._task is not None:
        await asyncio.gather(inv.worked._task, return_exceptions=True)


# ------------------------------- A: census -------------------------------- #

def test_due_census_reads_spec_ids_without_enriched_rows(tmp_path, monkeypatch):
    """RED on base (census builds enriched rows via work_lane_rows), GREEN on the
    candidate (census uses the light canonical pass): zero enriched reads, and the
    spec IDs handed to the ledger equal the legacy enriched union."""
    root = tmp_path / "memory"

    async def body(env):
        target, legacy, done = await _seed_lanes(env, root)
        oracle = await _legacy_census_union(env.store)
        assert len(oracle) >= 40 and "spec_demo__n00" in oracle

        inv, captured = await _build_due_inventory(env, tmp_path)

        # The async enriched reader is called only by the census, never by the
        # lifecycle/episode/emit passes of a drain.
        calls = []
        real_rows = env.store.work_lane_rows

        async def counted_rows(**kwargs):
            calls.append(kwargs)
            return await real_rows(**kwargs)

        monkeypatch.setattr(env.store, "work_lane_rows", counted_rows)
        await _drain_once(inv)

        assert captured["ids"] == oracle              # unchanged census answer
        assert calls == []                            # candidate: no enriched build

    run(body, path=str(tmp_path / "db.sqlite"))


def test_due_census_controls_due_empty_and_not_due(tmp_path):
    """Controls: census fires once when due, not at all when not due, and an empty
    membership yields an empty ledger input with no scheduled worker."""
    root = tmp_path / "memory"

    async def body(env):
        await _seed_lanes(env, root)
        oracle = await _legacy_census_union(env.store)

        inv, captured = await _build_due_inventory(env, tmp_path)
        await _drain_once(inv)
        assert captured["ids"] == oracle
        assert inv.worked.values == {spec: None for spec in oracle}

        # Not due after the first pass: a second drain runs no census.
        captured.clear()
        await _drain_once(inv)
        assert "ids" not in captured

    run(body, path=str(tmp_path / "db.sqlite"))


def test_due_census_empty_membership_schedules_no_worker(tmp_path):
    root = tmp_path / "memory"

    async def body(env):
        # No lanes at all -> empty union.
        inv, captured = await _build_due_inventory(env, tmp_path)
        await _drain_once(inv)
        assert captured == {}                         # recorder never ran
        assert inv.worked.values == {}
        assert inv.worked._task is None

    run(body, path=str(tmp_path / "db.sqlite"))


def test_census_excludes_noncanonical_work_state(tmp_path):
    """A lane whose work_state is a representable noncanonical TEXT value is not a
    product lane: the census must exclude its members, matching the enriched
    reader's WORK_STATES predicate (not the wider work_state IS NOT NULL)."""
    root = tmp_path / "memory"

    async def body(env):
        target, legacy, done = await _seed_lanes(env, root)
        # Set a real target lane to an unrestricted noncanonical state.
        await env.store.submit(lambda c: (c.execute(
            "UPDATE v2_assistant_composite_lanes SET work_state='legacy_hold' WHERE lane_id=?", (target,)),
            c.commit()))
        # Oracle: the enriched reader filters to WORK_STATES, so target (and its
        # n00..n39 members) drop out.
        oracle = await _legacy_census_union(env.store)
        census = await env.store.work_lane_census_spec_ids()
        assert census == oracle
        assert not any(f"spec_demo__n{i:02}" in census for i in range(40))

    run(body, path=str(tmp_path / "db.sqlite"))


def test_census_coherent_under_concurrent_commit(tmp_path, monkeypatch):
    """All census reads see one committed snapshot. A competing commit that lands
    inside the resolver -- after its lane snapshot, before its epic-assignment
    read -- must not tear the result (the classic old-members + newly-derived-add
    mix that exists in neither committed state)."""
    dbfile = str(tmp_path / "db.sqlite")
    root = tmp_path / "memory"

    async def body(env):
        target, legacy, done = await _seed_lanes(env, root)
        before = await env.store.work_lane_census_spec_ids()
        assert "spec_demo__added" not in before

        # Fire a competing commit from a second connection at goal_id, which the
        # resolver calls after reading lanes and before the epic-assignment SELECT.
        # The commit derives a new member of the target's epic.
        import store_work_index
        real_goal_id = store_work_index.goal_id
        fired = {"done": False}

        def goal_id_hook(identity):
            if not fired["done"]:
                fired["done"] = True
                writer = sqlite3.connect(dbfile)
                try:
                    writer.execute(
                        "INSERT OR REPLACE INTO v2_work_item_observations(spec_id,record_json) VALUES (?,?)",
                        ("spec_demo__added", json.dumps({
                            "member": {"spec_id": "spec_demo__added", "observation": {"quality": "fresh"}},
                            "last_good": {"_epic": "epic_fixture"}})))
                    writer.commit()
                finally:
                    writer.close()
            return real_goal_id(identity)

        monkeypatch.setattr(store_work_index, "goal_id", goal_id_hook)
        census = await env.store.work_lane_census_spec_ids()
        assert fired["done"], "injection point not reached"
        # Coherent: the resolver's epic read sees the pre-commit snapshot, so the
        # mid-read derived member never enters the census. A torn read would add it.
        assert "spec_demo__added" not in census
        assert census == before

    run(body, path=str(tmp_path / "db.sqlite"))


def test_get_work_lane_missing_lane_survives_state_read_fault(tmp_path, monkeypatch):
    """An absent lane returns None before the global resolver runs, so a fault
    reading unrelated index state cannot turn a missing lane into an error."""
    root = tmp_path / "memory"

    async def body(env):
        await _seed_lanes(env, root)

        def boom(conn):
            raise sqlite3.DatabaseError("denied: v2_work_index_state")

        monkeypatch.setattr(store_work_lanes, "effective_membership_ids_conn", boom)
        # Missing lane: existence check returns first, the resolver is never called.
        assert await env.store.get_work_lane("lane_does_not_exist") is None

    run(body, path=str(tmp_path / "db.sqlite"))


# --------------------------- B: coherent reader --------------------------- #

def _enable_packets(env):
    env.store.external_work_config = ExternalWorkConfig(status="enabled", watch_id="dot", label="Dot")


async def _seed_packets(env, member_spec):
    """One packet on a live member, one on a packet-only spec."""
    def write(conn):
        for tag, spec, state in [("live", member_spec, "working"),
                                 ("only", "spec_packet_only", "working")]:
            conn.execute("INSERT INTO v2_external_work_packets VALUES (?,?,?,?,?,?,?,?)",
                         ("dot", tag, spec, state, 1.0, 1.0, 1.0, None))
        conn.commit()
    await env.store.submit(write)


@pytest.mark.parametrize("packets", [False, True])
def test_get_work_lane_bulk_counts(tmp_path, monkeypatch, packets):
    """The whole coherent read resolves identity once and decodes each named id at
    most once, packets off and on. RED on base (3/4 resolutions), GREEN on the
    candidate (1), with the membership answer unchanged."""
    root = tmp_path / "memory"

    async def body(env):
        if packets:
            _enable_packets(env)
        target, legacy, done = await _seed_lanes(env, root)
        if packets:
            shown0 = await env.store.get_work_lane(target)
            await _seed_packets(env, shown0["members"][0]["spec_id"])

        decode_calls = _decode_recorder(monkeypatch)
        counts = _bulk_counters(monkeypatch)
        shown = await env.store.get_work_lane(target)

        # One identity resolution (base: 3 off / 4 on). Decode view unchanged in
        # count (base: 1 off / 2 on) but now snapshot-fed, so it never re-resolves.
        assert counts["ids"] == 1
        assert counts["decode"] == (2 if packets else 1)
        assert BASE_COUNTS["get_work_lane_on" if packets else "get_work_lane_off"]["ids"] > 1
        # Decode bound: never decode-all, each named id decoded at most once.
        assert decode_calls and all(ids is not None for ids in decode_calls)
        flat = [spec for call in decode_calls for spec in call]
        assert len(flat) == len(set(flat))
        # Membership answer preserved: the override moved n00 to the target, the
        # legacy lane lost it, and every member is present.
        ids = {m["spec_id"] for m in shown["members"]}
        assert "spec_demo__n00" in ids and len(ids) == 40
        assert {m["spec_id"] for m in shown["_projection_row"]["_members"]} == ids
        assert not (await env.store.get_work_lane(legacy))["members"]

    run(body, path=str(tmp_path / "db.sqlite"))


def test_current_packets_standalone_bulk_counts(tmp_path, monkeypatch):
    """Standalone current_packets_conn: the fresh path resolves once and decodes
    once; the reuse path (snapshot supplied) resolves zero times and decodes only
    packet-only ids. Same packet result either way."""
    root = tmp_path / "memory"

    async def body(env):
        _enable_packets(env)
        config = env.store.external_work_config
        target, legacy, done = await _seed_lanes(env, root)
        member0 = (await env.store.get_work_lane(target))["members"][0]["spec_id"]
        await _seed_packets(env, member0)   # one named-member packet, one packet-only

        def check(conn):
            # Fresh path (every caller but the coherent reader): 1 resolve, 1 decode.
            fresh_counts = {"ids": 0, "decode": 0}
            c = _patch_local(monkeypatch, fresh_counts)
            fresh = current_packets_conn(conn, config=config)
            assert fresh_counts == BASE_COUNTS["current_packets_fresh"]
            c.undo()

            # Build what the coherent reader already has: a resolved snapshot plus
            # the decoded named members.
            snapshot = effective_membership_ids_conn(conn)
            named = snapshot["membership"][target]
            decoded = effective_membership_conn(conn, spec_ids=named, snapshot=snapshot)["members"]

            # Reuse path, packet-only id present: 0 resolve, 1 decode (only the
            # packet-only id), identical packets.
            reuse_counts = {"ids": 0, "decode": 0}
            c = _patch_local(monkeypatch, reuse_counts)
            reused = current_packets_conn(conn, config=config, snapshot=snapshot,
                                          decoded=decoded, decoded_ids=set(named))
            c.undo()
            assert reuse_counts == {"ids": 0, "decode": 1}
            assert reused == fresh

            # Reuse path, every packet id already decoded: 0 resolve, 0 decode.
            all_ids = set(named) | {member0, "spec_packet_only"}
            all_decoded = effective_membership_conn(conn, spec_ids=all_ids, snapshot=snapshot)["members"]
            none_counts = {"ids": 0, "decode": 0}
            c = _patch_local(monkeypatch, none_counts)
            reused2 = current_packets_conn(conn, config=config, snapshot=snapshot,
                                           decoded=all_decoded, decoded_ids=all_ids)
            c.undo()
            assert none_counts == {"ids": 0, "decode": 0}
            assert reused2 == fresh

        await env.store.submit(check)

    run(body, path=str(tmp_path / "db.sqlite"))


def test_census_standalone_bulk_counts(tmp_path, monkeypatch):
    """Standalone work_lane_census_ids_conn: one resolve, zero decodes, and the set
    equals the legacy enriched union."""
    root = tmp_path / "memory"

    async def body(env):
        await _seed_lanes(env, root)
        oracle = await _legacy_census_union(env.store)

        def check(conn):
            counts = {"ids": 0, "decode": 0}
            c = _patch_local(monkeypatch, counts)
            result = work_lane_census_ids_conn(conn)
            c.undo()
            assert counts == {"ids": 1, "decode": 0}   # base enriched reader: 1 + 1
            assert result == oracle

        await env.store.submit(check)

    run(body, path=str(tmp_path / "db.sqlite"))


@pytest.mark.parametrize("packets", [False, True])
def test_get_work_lane_result_matches_fresh_oracle(tmp_path, packets):
    """Equality: the shared-snapshot read returns the same private Store shape as
    the fresh paths -- identical packets, trail, unblocks and projection row."""
    root = tmp_path / "memory"

    async def body(env):
        if packets:
            _enable_packets(env)
        target, legacy, done = await _seed_lanes(env, root)
        config = getattr(env.store, "external_work_config", None)
        if packets:
            first = await env.store.get_work_lane(target)
            await _seed_packets(env, first["members"][0]["spec_id"])

        shown = await env.store.get_work_lane(target)

        # Independent oracle for the packet snapshot: the fresh default path.
        packet_oracle = await env.store.submit(lambda c: current_packets_conn(c, config=config))
        assert shown["_projection_row"]["_external_packets"] == packet_oracle
        if packets:
            tags = {p["tag"] for p in packet_oracle["packets"]}
            assert tags == {"live", "only"}            # both open packets kept
        assert isinstance(shown["_unblocks"], dict)
        assert isinstance(shown["_trail_events"], list)
        assert shown["_work_index"]["available"] is True
        assert shown["lane"]["hue"] is not None         # a hue was allocated once

    run(body, path=str(tmp_path / "db.sqlite"))


def test_get_work_lane_missing_lane_returns_none_and_allocates_no_hue(tmp_path):
    """Invariant (GREEN on base and candidate): a missing lane returns None and
    never writes a row (no lazy hue allocation)."""
    root = tmp_path / "memory"

    async def body(env):
        await _seed_lanes(env, root)
        before = await env.store.submit(lambda c: c.total_changes)
        assert await env.store.get_work_lane("lane_does_not_exist") is None
        assert await env.store.get_work_lane("lane_does_not_exist") is None
        assert await env.store.submit(lambda c: c.total_changes) == before

    run(body, path=str(tmp_path / "db.sqlite"))


def test_get_work_lane_negative_control_duplicate_resolution_detected(tmp_path, monkeypatch):
    """Negative control: a reader that ignores the shared snapshot (re-resolving on
    the second lane read) is caught by the `== 1` assertion the sound test makes."""
    root = tmp_path / "memory"

    async def body(env):
        target, legacy, done = await _seed_lanes(env, root)

        real_lane_row = store_work_lanes.lane_row_conn

        def ignoring_snapshot(conn, lane_id, snapshot=None):
            # The offending variant drops the shared snapshot and re-resolves.
            return real_lane_row(conn, lane_id)

        monkeypatch.setattr(store_work_lanes, "lane_row_conn", ignoring_snapshot)
        counter = _resolution_counter(monkeypatch)
        await env.store.get_work_lane(target)

        assert len(counter) > 1                        # the duplication the sound test forbids

    run(body, path=str(tmp_path / "db.sqlite"))
