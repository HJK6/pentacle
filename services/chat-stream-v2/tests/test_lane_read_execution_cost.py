"""Cost and value controls for the worked census and coherent lane detail."""

import asyncio
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from hashlib import sha256
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import websockets

from external_work import ExternalWorkConfig
from server import Server, VerbError
from sessions import Sessions
from store import STREAM_TOKEN_HASH_VERSION
import store_work_index as index
import store_work_lanes as lanes
from test_assistant_prose_mirror import ASSISTANT, ROOT
from test_work_lane_episodes import member
from test_work_lane_goals import adopt_with_goal
from test_work_lane_progress_trail import event, state
from test_work_lanes import run
from _shared.work_goals import goal_id
import work_lanes_projection as projection
from work_lane_worked import WorkedCache

STAMP = "2026-01-02T12:00:00.000Z"
GOAL = "epic_cost"
TARGET_IDS = tuple(f"spec_fixture__item_{i:02}" for i in range(40))
EXPLICIT = ("spec_fixture__unresolved", "spec_fixture__legacy")
DONE = "spec_fixture__done_only"
ACTIVE = "spec_fixture__active_only"
BLOCKED = "spec_fixture__blocked_only"
UNKNOWN = "spec_fixture__unknown_goal"
EXCLUDED = "spec_fixture__moved_out"
BLOCKERS = tuple(f"spec_fixture__blocker_{i}" for i in range(6))
EXPECTED = {*TARGET_IDS, *EXPLICIT, DONE, ACTIVE, BLOCKED, UNKNOWN}
HOST = "fixture-root"
TOKEN = "synthetic-lane-cost-token"
UPDATED = "2026-01-02T12:01:00.000Z"


def _record(identity, *, status="completed", quality="fresh", epic=None, blocked_by=None):
    return json.dumps({
        "member": {**member(status, quality), "spec_id": identity, "title": identity},
        "last_good": {"status": status, "_epic": epic, "_blocked_by": blocked_by},
    })


async def seed(env, *, done_lanes=32, unrelated=512, packets=False, trail=502):
    target = await adopt_with_goal(env, key="stream:cost", name="cost", state="paused",
                                   owner="operator", members=list(reversed(EXPLICIT)))
    lane_id = target["lane_id"]

    def write(conn):
        others = [
            ("active-other", "active", [ACTIVE, TARGET_IDS[0], UNKNOWN, EXCLUDED, ACTIVE]),
            ("blocked-other", "blocked", [BLOCKED, EXPLICIT[0]]),
            ("routing-other", None, ["spec_fixture__routing_only"]),
            ("unknown-state", "legacy", ["spec_fixture__nonproduct"]),
            *((f"done-{i:02}", "done", [DONE, EXPLICIT[0]]) for i in range(done_lanes)),
        ]
        conn.executemany(
            "INSERT INTO v2_assistant_composite_lanes "
            "(lane_id,stream_id,phase,version,created_at,updated_at,done_at,title,work_state,owner_kind,members_json) "
            "VALUES (?,?,'completed',1,?,?,?,?,?,'fd',?)",
            [(lid, ASSISTANT if i % 2 else "fixture:other-composite", STAMP, STAMP,
              STAMP if work_state == "done" else None, lid, work_state, json.dumps(members))
             for i, (lid, work_state, members) in enumerate(others)],
        )
        catalog = index._state_conn(conn)
        catalog.update(available=True, root_configured=True, snapshot_at=STAMP, error=None)
        goals = catalog["goal_observations"]
        for epic, destination in ((GOAL, lane_id), ("epic_unknown_destination", "absent"),
                                  ("epic_excluded_destination", "unknown-state")):
            goals[epic] = {"facts": {"epic_id": epic, "lane_id": destination, "title": epic, "rank": 1},
                           "quality": "fresh", "error": None, "source_hash": "fixture:" + epic}
        conn.execute("UPDATE v2_work_index_state SET state_json=?", (json.dumps(catalog),))
        records = {
            identity: _record(identity, epic=GOAL, status="in_progress",
                              quality=("fresh", "stale", "ambiguous")[i % 3])
            for i, identity in enumerate(TARGET_IDS)
        }
        records.update({identity: _record(identity) for identity in (EXPLICIT[1], ACTIVE, BLOCKED, DONE)})
        records[UNKNOWN] = _record(UNKNOWN, epic="epic_unknown_destination")
        records[EXCLUDED] = _record(EXCLUDED, epic="epic_excluded_destination")
        records.update({spec: _record(spec, status="blocked", blocked_by=[TARGET_IDS[0], TARGET_IDS[0]])
                        for spec in BLOCKERS})
        records.update({f"spec_fixture__settled_{i:03}":
                        _record(f"spec_fixture__settled_{i:03}", epic="epic_unassigned")
                        for i in range(unrelated)})
        conn.executemany("INSERT INTO v2_work_item_observations VALUES (?,?)", records.items())
        conn.execute("INSERT INTO v2_work_item_latest_report VALUES (?,?,?,?)",
                     (TARGET_IDS[-1], "reported_done", STAMP, "fixture-report"))
        changes = [event(STAMP, TARGET_IDS[0], state(checked=0), state(checked=i), rev=i, lane=lane_id)
                   for i in range(trail)]
        conn.executemany(
            "INSERT INTO v2_work_lane_events "
            "(event_id,lane_id,stream_id,operation,source_id,payload_json,payload_digest,created_at) "
            "VALUES (?,?,?,'item_change',?,?,'fixture',?)",
            [(change["event_id"], lane_id, ASSISTANT, change["event_id"], json.dumps(change["payload"]), STAMP)
             for change in changes],
        )
        conn.execute(
            "INSERT INTO v2_work_lane_events "
            "(event_id,lane_id,stream_id,operation,source_id,payload_json,payload_digest,created_at,"
            "update_kind,update_id,publication_event_id) "
            "VALUES ('fixture-update',?,?,'update','fixture-update','{}','fixture',?,'milestone','upd-cost',1)",
            (lane_id, ASSISTANT, STAMP),
        )
        if packets:
            conn.executemany("INSERT INTO v2_external_work_packets VALUES ('fixture',?,?,?,1,1,1,NULL)", [
                ("member", TARGET_IDS[0], "working"),
                ("terminal", "spec_fixture__settled_000", "review"),
                ("packet-only", BLOCKERS[0], "working"),
                ("missing", "spec_fixture__packet_unresolved", "queued"),
                ("closed", TARGET_IDS[1], "closed"),
            ])
        conn.commit()

    await env.store.submit(write)
    if packets:
        env.store.external_work_config = ExternalWorkConfig(status="enabled", watch_id="fixture", label="Fixture")
    return lane_id


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(lanes, "_now", lambda: STAMP)
    monkeypatch.setattr(projection, "_iso_now", lambda: UPDATED)


def enriched_ids(rows):
    return {item["spec_id"] for row in rows for item in row["_members"]}


def census_inventory(store):
    inv = projection.WorkLanesInventory(store, None, AsyncMock(), refresh_min_s=0)
    inv.store = SimpleNamespace(reconcile_lane_lifecycle=AsyncMock(), reconcile_work_lane_episodes=AsyncMock(),
                                work_lane_rows=store.work_lane_rows)
    if hasattr(store, "work_lane_worked_spec_ids"):
        inv.store.work_lane_worked_spec_ids = store.work_lane_worked_spec_ids
    inv.worked = SimpleNamespace(needs_refresh=Mock(return_value=True), refresh=Mock())
    inv.emit_if_changed = AsyncMock()
    return inv


async def drain(inv):
    inv.refresh()
    await inv._task


@contextmanager
def observe_callback(store, patch):
    submit = store.submit
    callbacks = []
    with patch.context() as instrument:
        async def traced(callback, **kwargs):
            def call(conn):
                statements = []
                callbacks.append((conn, statements))
                conn.set_trace_callback(statements.append)
                try:
                    return callback(conn)
                finally:
                    conn.set_trace_callback(None)
            return await submit(call, **kwargs)

        instrument.setattr(store, "submit", traced)
        yield callbacks


def report(record_property, **values):
    for key, value in values.items():
        record_property(key, json.dumps(value, sort_keys=True))


def _no_enrichment(count):
    assert count == 0, ("worked_census_enriched_rows", count, 0)


async def census_result(store, patch, *, mutant=False):
    inv = census_inventory(store)
    counts = Counter()
    real_rows, real_decode = lanes.work_lane_rows_conn, index._decode_observations

    def count_rows(conn, **kwargs):
        counts["enriched"] += 1
        assert kwargs["include_done"] is True
        return real_rows(conn, **kwargs)

    def count_decode(*args, **kwargs):
        counts["decode"] += 1
        return real_decode(*args, **kwargs)

    with patch.context() as instrument:
        instrument.setattr(lanes, "work_lane_rows_conn", count_rows)
        instrument.setattr(index, "_decode_observations", count_decode)
        if mutant:
            candidate = inv.store.work_lane_worked_spec_ids

            async def restore_enriched():
                counts["legacy_installed_entry_called"] += 1
                return enriched_ids(await inv.store.work_lane_rows(include_done=True))

            instrument.setattr(inv.store, "work_lane_worked_spec_ids", restore_enriched)
            assert inv.store.work_lane_worked_spec_ids is restore_enriched
            assert candidate is not restore_enriched
        with observe_callback(store, instrument) as callbacks:
            await drain(inv)
            assert inv.worked.refresh.call_count == 1
            inv.emit_if_changed.assert_awaited_once()
            value, = inv.worked.refresh.call_args.args
            assert isinstance(value, set)
            assert len(callbacks) == 1
            _, sql = callbacks[0]
            inv.worked.needs_refresh.return_value = False
            await drain(inv)
            assert inv.worked.refresh.call_count == 1 and len(callbacks) == 1
    return value, counts, sql


def test_due_worked_census_sound(tmp_path, monkeypatch, record_property, fixed_clock):
    async def body(env):
        for width in (4, 32):
            await seed_for_census_width(env, width)
            oracle = enriched_ids(await env.store.work_lane_rows(include_done=True))
            value, counts, sql = await census_result(env.store, monkeypatch)
            assert value == oracle == EXPECTED
            selects = [statement for statement in sql if statement.startswith("SELECT")]
            report(record_property, reader="due worked census", done_lanes=width, required_ids=sorted(value),
                   enriched_calls=counts["enriched"], observation_decode_calls=counts["decode"],
                   selects=len(selects))
            _no_enrichment(counts["enriched"])
            assert counts["decode"] == 0
            assert len(selects) <= 4
            assert all(any(table in statement for table in (
                "v2_assistant_composite_lanes", "v2_work_index_state", "v2_work_item_observations"))
                       for statement in selects)
            assert sql[0] == "BEGIN" and sql[-1] == "COMMIT"

    run(body, str(tmp_path / "census.sqlite"))


async def seed_for_census_width(env, width):
    if width == 4:
        await seed(env, done_lanes=32, unrelated=32, trail=0)

    def set_width(conn):
        conn.execute("UPDATE v2_assistant_composite_lanes SET work_state=NULL WHERE lane_id LIKE 'done-%'")
        conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='done' WHERE lane_id LIKE 'done-%' AND lane_id < ?",
                     (f"done-{width:02}",))
        conn.commit()

    await env.store.submit(set_width)


def test_due_worked_census_fault(tmp_path, monkeypatch, record_property, fixed_clock):
    async def body(env):
        await seed(env, unrelated=32, trail=0)
        oracle = enriched_ids(await env.store.work_lane_rows(include_done=True))
        sound, sound_counts, _ = await census_result(env.store, monkeypatch)
        _no_enrichment(sound_counts["enriched"])
        fault, counts, _ = await census_result(env.store, monkeypatch, mutant=True)
        assert sound == fault == oracle == EXPECTED
        assert counts["legacy_installed_entry_called"] == counts["enriched"] == 1
        with pytest.raises(AssertionError) as raised:
            _no_enrichment(counts["enriched"])
        assert raised.value.args[0] == ("worked_census_enriched_rows", 1, 0)
        report(record_property, reader="due worked census", mutation_called=counts["legacy_installed_entry_called"],
               matching_ids=sorted(fault), assertion=raised.value.args[0],
               enriched_calls=counts["enriched"], sound_enriched_calls=sound_counts["enriched"])

    run(body, str(tmp_path / "census.sqlite"))


@asynccontextmanager
async def authorized_show(env):
    sessions = Sessions(env.store, tmux=None, local_host=HOST)
    await sessions.refresh()
    server = Server(host="127.0.0.1", port=0, store=env.store, sessions=sessions, local_host=HOST)
    inventory = projection.WorkLanesInventory(env.store, sessions, server.broadcast)
    server.work_lanes = inventory
    inventory.open_questions = AsyncMock(return_value={TARGET_IDS[-1]: {"count": 1}})
    await env.store.grant_stream_token(HOST, "visible", sha256(TOKEN.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
    port = await server.bind()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            async with asyncio.timeout(5):
                assert json.loads(await socket.recv())["type"] == "welcome"
                await socket.send(json.dumps({"type": "hello", "client": "lane-cost-fixture",
                                              "from_stream_id": ROOT, "stream_token": TOKEN}))
                while json.loads(await socket.recv())["type"] != "snapshot":
                    pass

            async def show(lane_id, **fields):
                request_id = "fixture-show"
                await socket.send(json.dumps({"type": "work_lanes.show", "lane_id": lane_id,
                                              "from_stream_id": ROOT, "stream_token": TOKEN,
                                              "request_id": request_id, **fields}))
                async with asyncio.timeout(5):
                    while True:
                        reply = json.loads(await socket.recv())
                        if reply.get("request_id") == request_id:
                            return reply

            yield show, server
    finally:
        await inventory.stop()
        await server.close()


def _one_resolution(count):
    assert count == 1, ("coherent_lane_identity_resolutions", count, 1)


async def detail_result(store, show, lane_id, patch, *, mutant=False):
    real = index.effective_membership_ids_conn
    real_decode = index._decode_observations
    real_read = store.get_work_lane
    identities, decodes, raw = [], [], []
    dropped = Counter()

    def trace_identities(conn):
        identities.append((conn, conn.in_transaction))
        return real(conn)

    def trace_decode(conn, spec_ids, snapshot_state):
        decoded = real_decode(conn, spec_ids, snapshot_state)
        decodes.append((conn, conn.in_transaction, tuple(spec_ids), tuple(decoded)))
        return decoded

    async def capture(identity):
        shown = await real_read(identity)
        raw.append(shown)
        return shown

    with patch.context() as instrument:
        instrument.setattr(index, "effective_membership_ids_conn", trace_identities)
        instrument.setattr(lanes, "effective_membership_ids_conn", trace_identities)
        instrument.setattr(index, "_decode_observations", trace_decode)
        instrument.setattr(store, "get_work_lane", capture)
        if mutant:
            def restore(helper, label):
                def without_snapshot(*args, **kwargs):
                    assert kwargs.pop("membership_snapshot") is not None
                    dropped[label] += 1
                    return helper(*args, **kwargs)
                return without_snapshot

            for name in ("lane_row_conn", "work_lane_rows_conn"):
                replacement = restore(getattr(lanes, name), name)
                instrument.setattr(lanes, name, replacement)
                assert getattr(lanes, name) is replacement
        with observe_callback(store, instrument) as callbacks:
            wire = await show(lane_id, members_after=TARGET_IDS[5], members_limit=13, goal_id=goal_id(GOAL))
    assert wire["type"] == "work_lanes.show.ok", wire
    assert wire["members_total"] == len(TARGET_IDS)
    assert [item["spec_id"] for item in wire["members"]] == list(TARGET_IDS[6:19])
    assert wire["members_next"] == TARGET_IDS[18]
    assert len(raw) == 1 and not any(key.startswith("_") for key in wire)
    lane_transactions = [(conn, sql) for conn, sql in callbacks if sql and sql[0] == "BEGIN IMMEDIATE"]
    assert len(lane_transactions) == 1
    conn, sql = lane_transactions[0]
    assert sql[-1] == "COMMIT"
    assert all(read_conn is conn and in_transaction for read_conn, in_transaction in identities)
    assert all(read_conn is conn and in_transaction for read_conn, in_transaction, _, _ in decodes)
    assert sum("SELECT lane_id,members_json FROM " in statement for statement in sql) == len(identities)
    assert sum("SELECT spec_id,json_extract(record_json,'$.last_good._epic')" in statement
               for statement in sql) == len(identities)
    return raw[0], wire, len(identities), decodes, dropped, sql


def _verify_detail_values(raw, wire, *, packets):
    assert raw["lane"]["members"] == [*reversed(EXPLICIT), *TARGET_IDS]
    assert len(raw["_projection_row"]["_members"]) == len(TARGET_IDS) + len(EXPLICIT)
    assert raw["_projection_row"]["_members"][-1]["_latest_report_id"] == "fixture-report"
    assert raw["lane"]["hue"] == raw["_projection_row"]["hue"]
    assert len(raw["events"]) == len(wire["events"]) == 504
    assert len(raw["_trail_events"]) == 500
    assert raw["updates"] == wire["updates"] and wire["updates"][0]["update_id"] == "upd-cost"
    assert [item["spec_id"] for item in raw["_unblocks"][TARGET_IDS[0]]] == list(BLOCKERS[:4])
    tags = {packet["tag"] for packet in raw["_projection_row"]["_external_packets"]["packets"]}
    assert tags == ({"member", "packet-only", "missing"} if packets else set())


def _detail_sound(tmp_path, monkeypatch, record_property, *, packets):
    async def body(env):
        lane_id = await seed(env, packets=packets)
        async with authorized_show(env) as (show, _):
            raw, wire, count, decodes, _, sql = await detail_result(env.store, show, lane_id, monkeypatch)
        _verify_detail_values(raw, wire, packets=packets)
        looked_up = {item for _, _, names, _ in decodes for item in names}
        decoded = Counter(item for _, _, _, values in decodes for item in values)
        report(record_property, reader="coherent lane detail", external_packets_enabled=packets,
               resolutions=count, worker_statements=len(sql), decoded_rows=dict(decoded),
               page_members=[item["spec_id"] for item in wire["members"]])
        _one_resolution(count)
        extra = {BLOCKERS[0], "spec_fixture__packet_unresolved", "spec_fixture__settled_000"} if packets else set()
        assert looked_up == {*TARGET_IDS, *EXPLICIT, *extra}
        assert set(decoded) == looked_up - {EXPLICIT[0], "spec_fixture__packet_unresolved"}
        assert all(times == 1 for times in decoded.values())

    run(body, str(tmp_path / "detail.sqlite"))


def _detail_fault(tmp_path, monkeypatch, record_property, *, packets):
    async def body(env):
        lane_id = await seed(env, packets=packets)
        async with authorized_show(env) as (show, _):
            await env.store.get_work_lane(lane_id)  # settle hue before paired reads
            raw, wire, count, _, _, _ = await detail_result(env.store, show, lane_id, monkeypatch)
            _one_resolution(count)
            fault, fault_wire, fault_count, _, dropped, _ = await detail_result(
                env.store, show, lane_id, monkeypatch, mutant=True)
        assert fault == raw and fault_wire == wire
        assert dropped == {"lane_row_conn": 2, "work_lane_rows_conn": 1}
        with pytest.raises(AssertionError) as raised:
            _one_resolution(fault_count)
        assert raised.value.args[0] == ("coherent_lane_identity_resolutions", 4 if packets else 3, 1)
        report(record_property, reader="coherent lane detail", external_packets_enabled=packets,
               mutation_calls=dict(dropped), assertion=raised.value.args[0], resolutions=fault_count,
               sound_resolutions=count, full_store_and_wire_equal=True)

    run(body, str(tmp_path / "detail.sqlite"))


def test_coherent_detail_sound(tmp_path, monkeypatch, record_property, fixed_clock):
    _detail_sound(tmp_path, monkeypatch, record_property, packets=False)


def test_coherent_detail_fault(tmp_path, monkeypatch, record_property, fixed_clock):
    _detail_fault(tmp_path, monkeypatch, record_property, packets=False)


def test_coherent_detail_packets_sound(tmp_path, monkeypatch, record_property, fixed_clock):
    _detail_sound(tmp_path, monkeypatch, record_property, packets=True)


def test_coherent_detail_packets_fault(tmp_path, monkeypatch, record_property, fixed_clock):
    _detail_fault(tmp_path, monkeypatch, record_property, packets=True)
