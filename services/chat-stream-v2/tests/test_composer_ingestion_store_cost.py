"""Measured Store-cost regressions, with authority and scope controls first."""
import asyncio
from copy import deepcopy
import hashlib
import json
import re

import pytest

from assets import Assets
from sessions import Sessions
from store import Store
import store as store_module


STREAM = "fixture:lead"
BODY = "[pentacle-notice:fixture-notice]\nfixed synthetic status"
EVENT = {"stream_id": STREAM, "daemon_seq": 42, "kind": "USER", "text": BODY,
         "raw": {"source": "fixture", "daemon_notice": {"forged": True}}}


async def _notice_fixture(store):
    row = await store.open_session("fixture", "lead", provider="claude")
    binding = {"body_sha256": hashlib.sha256(BODY.encode()).hexdigest(),
               "kind": "status_card", "notice_id": "fixture-notice",
               "pre_input_watermark": 41, "proof_event_id": 42,
               "recipient_stream_id": STREAM, "session_generation": row["session_generation"]}
    await store.submit(lambda conn: (
        conn.execute("""INSERT INTO v2_outbound_notices
            (notice_id,kind,dedupe_key,recipient_stream_id,tell_id,body,payload_digest,
             created_at,proof_binding) VALUES (?,?,?,?,?,?,?,?,?)""",
            ("fixture-notice", "status_card", "fixture-notice", STREAM,
             "fixture-notice", BODY, "synthetic", "2026-10-25T00:00:00Z", json.dumps(binding))),
        conn.commit()))


async def _trace(store):
    statements = []
    await store.submit(lambda conn: conn.set_trace_callback(
        lambda sql: statements.append(sql) if sql.lstrip().upper().startswith("SELECT") else None))
    return statements


def _notice_queries(statements):
    return [sql for sql in statements if "FROM V2_OUTBOUND_NOTICES" in sql.upper()]


def _trusted(event):
    return event.get("raw", {}).get("daemon_notice", {}).get("notice_id") == "fixture-notice"


async def _check_projection(changes, expected_trust, expected_queries):
    store = Store(":memory:")
    store.start()
    try:
        await _notice_fixture(store)
        statements = await _trace(store)
        control = await store.project_session_events([deepcopy(EVENT)])
        assert _trusted(control[0]) and len(_notice_queries(statements)) == 2
        event = {**deepcopy(EVENT), **deepcopy(changes)}
        original = deepcopy(event)
        statements.clear()
        output = await store.project_session_events([event])
        # Semantic authority/stripping and immutability precede the cost assertion.
        assert event == original and len(output) == 1
        assert output[0]["text"] == event["text"]
        assert _trusted(output[0]) is expected_trust
        assert output[0].get("raw", {}).get("daemon_notice") != {"forged": True}
        assert len(_notice_queries(statements)) == expected_queries
    finally:
        store.stop()


@pytest.mark.parametrize("changes", [
    {"kind": "ASSIST_TEXT"}, {"kind": "TOOL"}, {"kind": "SYSTEM"},
    {"kind": True}, {"text": "ordinary USER"}, {"text": None},
    {"text": 0}, {"text": []}, {"text": {}}, {"text": " " + BODY},
])
def test_ineligible_live_projection_strips_without_notice_sql(changes):
    asyncio.run(_check_projection(changes, False, 0))


@pytest.mark.parametrize("kind", ["USER", "uSeR", "uſer"])
def test_exact_upper_kind_preserves_fixed_proof(kind):
    asyncio.run(_check_projection({"kind": kind}, True, 2))


@pytest.mark.parametrize("key", ["client_origin", "optimistic_id", "request_id", "receipt_id"])
@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("value,bound", [(True, True), (0, True), ([], True),
                                         ("client", True), (False, False),
                                         (None, False), ("", False), ("  ", False)])
def test_exact_client_binding_semantics(key, raw, value, bound):
    changes = {"raw": {**deepcopy(EVENT["raw"]), key: value}} if raw else {key: value}
    asyncio.run(_check_projection(changes, not bound, 0 if bound else 2))


@pytest.mark.parametrize("changes", [{"daemon_seq": value} for value in [None, True, 0, -1, "42"]]
                         + [{"stream_id": None}, {"stream_id": ""}])
def test_invalid_identity_still_strips_without_query(changes):
    asyncio.run(_check_projection(changes, False, 0))


@pytest.mark.parametrize("eligible_first", [False, True])
def test_mixed_batch_queries_only_eligible_streams_and_strips_every_input(eligible_first):
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            await _notice_fixture(store)
            statements = await _trace(store)
            events = [deepcopy(EVENT), {**deepcopy(EVENT), "stream_id": "fixture:unknown"},
                      {**deepcopy(EVENT), "stream_id": "fixture:noise", "kind": "ASSIST_TEXT"}]
            expected = [True, False, False]
            if not eligible_first:
                events.reverse()
                expected.reverse()
            original = deepcopy(events)
            output = await store.project_session_events(events)
            assert events == original and [_trusted(event) for event in output] == expected
            assert all(event.get("raw", {}).get("daemon_notice") != {"forged": True} for event in output)
            queries = _notice_queries(statements)
            assert len(queries) == 4
            assert not any("fixture:noise" in sql for sql in queries)
        finally:
            store.stop()
    asyncio.run(go())


def test_question_scope_keeps_phantom_cycles_and_depth_bound():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="fixture")
            await store.open_session("fixture", "phantom-child", provider="claude", visibility="hidden",
                                     parent_stream_id="fixture:phantom")
            assert await sessions.visible_question_scope_stream_ids("fixture:phantom") == [
                "fixture:phantom", "fixture:phantom-child"]
            for name, parent in [("cycle-a", "cycle-b"), ("cycle-b", "cycle-a")]:
                await store.open_session("fixture", name, provider="claude", visibility="hidden",
                                         parent_stream_id="fixture:" + parent)
            assert await sessions.visible_question_scope_stream_ids("fixture:cycle-a") == ["fixture:cycle-a"]
            await store.open_session("fixture", "depth-root", provider="claude")
            parent = "fixture:depth-root"
            expected = [parent]
            for depth in range(1, 66):
                name = f"depth-{depth:02}"
                await store.open_session("fixture", name, provider="claude", visibility="hidden",
                                         parent_stream_id=parent)
                parent = "fixture:" + name
                if depth <= 64:
                    expected.append(parent)
            assert await sessions.visible_question_scope_stream_ids("fixture:depth-root") == expected
        finally:
            store.stop()
    asyncio.run(go())


def test_ineligible_durable_history_strips_without_notice_sql():
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            await _notice_fixture(store)
            for index, kind in enumerate(["USER", "ASSIST_TEXT", "TOOL"]):
                event = {**deepcopy(EVENT), "kind": kind, "text": "ordinary history",
                         "provider": "claude", "timestamp": "2026-10-25T00:00:00Z"}
                assert await store.append_session_event(STREAM, event, identity=f"fixture-{index}", limit=500)
            statements = await _trace(store)
            control = await store.project_session_events([deepcopy(EVENT)])
            assert _trusted(control[0]) and len(_notice_queries(statements)) == 2
            statements.clear()
            output = await store.fetch_session_event_tail(STREAM, limit=500)
            assert [event["kind"] for event in output] == ["USER", "ASSIST_TEXT", "TOOL"]
            assert all(event["text"] == "ordinary history" for event in output)
            assert all("daemon_notice" not in event.get("raw", {}) for event in output)
            assert not _notice_queries(statements)
        finally:
            store.stop()
    asyncio.run(go())


async def _scope_fixture(store):
    for name, fields in [("lead", {}), ("z_child", {"parent_stream_id": STREAM, "visibility": "hidden"}),
                         ("a_child", {"parent_stream_id": STREAM, "visibility": "hidden"}),
                         ("grandchild", {"parent_stream_id": "fixture:z_child", "visibility": "hidden"}),
                         ("sublead", {"parent_stream_id": STREAM}),
                         ("subworker", {"parent_stream_id": "fixture:sublead", "visibility": "hidden"}),
                         ("successor", {"handoff_from_stream_id": STREAM}), ("unrelated", {})]:
        row = await store.open_session("fixture", name, provider="claude", **fields)
        if name == "z_child":
            assert await store.mark_closed("fixture", name, closed_at="2026-10-25T00:00:00Z",
                                           pane_status="pane_dead", expected_generation=row["session_generation"])


@pytest.mark.parametrize("consumer", ["asset", "question"])
def test_scope_uses_one_lineage_select_without_hydration(tmp_path, monkeypatch, consumer):
    async def go():
        store = Store(":memory:")
        store.start()
        assets = None
        try:
            await _scope_fixture(store)
            sessions = Sessions(store, local_host="fixture")
            assets = Assets(str(tmp_path / "assets.db"), sessions=sessions)
            await assets.start()
            async def call():
                return (await assets._chat_scope("fixture:successor") if consumer == "asset"
                        else await sessions.visible_question_scope_stream_ids(STREAM))
            # The oracle uses the unchanged real full-read API. The cost call below
            # restores the real narrow method; no Store result is substituted there.
            with monkeypatch.context() as oracle:
                async def full_read():
                    return await store.list_sessions(status=None)
                oracle.setattr(store, "list_session_lineage", full_read, raising=False)
                expected = await call()
            if consumer == "asset":
                assert expected == sorted([STREAM, "fixture:z_child", "fixture:a_child", "fixture:grandchild",
                                           "fixture:sublead", "fixture:subworker", "fixture:successor"])
            else:
                assert expected == [STREAM, "fixture:z_child", "fixture:a_child", "fixture:grandchild"]
            hydrated = []
            real = store_module._session_row
            with monkeypatch.context() as observe:
                observe.setattr(store_module, "_session_row",
                                lambda conn, row: hydrated.append(1) or real(conn, row))
                statements = await _trace(store)
                full = await store.list_sessions(status=None)
                assert len(hydrated) == len(full) == 8 and len(statements) > 8
                hydrated.clear()
                statements.clear()
                actual = await call()
                # Exact ordered equality, not membership-only, precedes witnesses.
                assert actual == expected
                assert len(statements) == 1, f"scope SELECT count: {len(statements)}"
                tables = re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", statements[0], re.I)
                assert {table.lower() for table in tables} == {"sessions", "v2_session_generations"}
                assert not hydrated
        finally:
            if assets is not None:
                await assets.stop()
            store.stop()
    asyncio.run(go())
