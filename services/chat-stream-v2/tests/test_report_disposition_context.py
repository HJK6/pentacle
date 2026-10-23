"""The first durable accepted-report notice and fallback carry optional cached context."""
import asyncio

from ledger import Ledger
from message_envelopes import match_message_envelope
from sessions import Sessions
from store import Store
from test_work_unattended import cached


async def setup(store):
    sessions = Sessions(store, local_host="host-a")
    await sessions.open("host-a", "parent", provider="shell", no_watch=True)
    await sessions.open("host-a", "reporter", provider="shell", role="worker", no_watch=True,
                        parent_stream_id="host-a:parent", spec_ids=["spec_demo__item_00"])
    await cached(store)
    return sessions


def payload(identity="context-first", status="done", **extra):
    return {"report_id": identity, "from_stream_id": "host-a:reporter", "to_stream_id": "host-a:parent",
            "msg_id": 0, "status": status, "summary": "Finished commission", "findings": [],
            "next_action": "Review result", "ingested_at": "2026-10-22T20:00:00Z", **extra}


async def report_notices(store):
    return await store.submit(lambda conn: [dict(r) for r in conn.execute(
        "SELECT * FROM v2_outbound_notices WHERE kind='report'")])


def test_first_durable_notice_has_context_before_ledger_fallback():
    async def run():
        store = Store(); store.start()
        try:
            sessions = await setup(store)
            snapshot = await Ledger(store, sessions=sessions)._report_routing_snapshot("host-a:reporter")
            saved = await store.put_report(payload(), routing_snapshot=snapshot)
            assert (await store.get_report(saved["report_id"]))["status"] == "done"
            rows = await report_notices(store)
            assert len(rows) == 1  # actual transactional producer, not fallback
            parsed = match_message_envelope(rows[0]["body"])
            assert parsed and parsed["report_id"] == saved["report_id"]  # ordinary notice control
            assert "disposition_context" in parsed, "first durable notice must include cached review context"
            context = parsed["disposition_context"]
            assert context["report_id"] == saved["report_id"]
            assert context["items"][0]["status"] == "in_progress"
        finally:
            store.stop()
    asyncio.run(run())


def test_ledger_fallback_context_and_report_dedupe():
    async def run():
        store = Store(); store.start()
        try:
            sessions = await setup(store)
            snapshot = await Ledger(store, sessions=sessions)._report_routing_snapshot("host-a:reporter")
            saved = await store.put_report(payload("context-fallback"), routing_snapshot=snapshot, announce=False)
            assert await report_notices(store) == []
            ledger = Ledger(store, sessions=sessions)
            await ledger._announce_child_report_ready(saved)
            rows = await report_notices(store)
            assert len(rows) == 1
            parsed = match_message_envelope(rows[0]["body"])
            assert parsed and parsed["report_id"] == saved["report_id"]
            assert "disposition_context" in parsed, "fallback must include context in its insertion transaction"
            await ledger._announce_child_report_ready(saved)
            assert len(await report_notices(store)) == 1
        finally:
            store.stop()
    asyncio.run(run())


import pytest


@pytest.mark.parametrize("status", ["done", "error", "aborted"])
@pytest.mark.parametrize("role,verdict", [("lead", None), ("nexus", None), ("worker", None),
                                          ("planner", None), ("qa", "accept"), ("qa", "reject")])
def test_terminal_roles_multispec_and_other_current_owners(status, role, verdict):
    async def run():
        store = Store(); store.start()
        try:
            sessions = await setup(store)
            await cached(store, count=3, now=101)
            await store.update_session("host-a", "reporter", role=role, assigned_spec_id="spec_demo__item_01",
                spec_ids=["demo__item_00", "demo__item_01", "demo__item_02"])
            await store.open_session("host-a", "other", role="lead", visibility="hidden",
                spec_ids=["demo__item_00", "demo__item_01"], assigned_spec_id="spec_demo__item_00")
            await store.open_session("host-a", "observer", role="qa", assigned_spec_id="spec_demo__item_01")
            snapshot = await Ledger(store, sessions=sessions)._report_routing_snapshot("host-a:reporter")
            extra = {"qa_verdict": verdict} if verdict else {}
            saved = await store.put_report(payload(status=status, **({"completion_kind": "tracked"} if status == "done" else {}), **extra), routing_snapshot=snapshot)
            context = match_message_envelope((await report_notices(store))[0]["body"])["disposition_context"]
            assert context["report_id"] == saved["report_id"]
            assert len(context["items"]) == 3
            assert context["items"][0]["other_owners"] == ["host-a:other"]
            assert context["items"][1]["other_owners"] == []  # reporter excluded, QA does not own
            assert all(item["status"] == "in_progress" and item["observed_at"] for item in context["items"])
            assert context["review"] == "Review the report outcome and current item before selecting a disposition."
            assert "move" not in context["review"].lower() and "complete" not in context["review"].lower()
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("fallback", [False, True])
def test_optional_enrichment_failure_preserves_report_notice_ingest_and_replay(monkeypatch, fallback):
    import work_unattended
    import threading
    event_thread = threading.get_ident()
    calls = []
    def fail(conn, report):
        assert threading.get_ident() != event_thread
        assert conn.execute("SELECT report_id FROM v2_reports WHERE report_id=?", (report["report_id"],)).fetchone()
        calls.append(report["report_id"])
        raise RuntimeError("optional cached-context failure")
    monkeypatch.setattr(work_unattended, "report_disposition_context_conn", fail)
    async def run():
        store = Store(); store.start()
        try:
            sessions = await setup(store)
            ledger = Ledger(store, sessions=sessions)
            snapshot = await ledger._report_routing_snapshot("host-a:reporter")
            saved = await store.put_report(payload(), routing_snapshot=snapshot, announce=not fallback)
            assert (await store.get_report(saved["report_id"]))["status"] == "done"
            if fallback:
                await ledger._announce_child_report_ready(saved)
            rows = await report_notices(store)
            assert len(rows) == 1 and match_message_envelope(rows[0]["body"])["report_id"] == saved["report_id"]
            assert "disposition_context=" not in rows[0]["body"]
            assert calls == [saved["report_id"]]
            await ledger._announce_child_report_ready(saved)
            assert len(await report_notices(store)) == 1 and calls == [saved["report_id"]]
        finally:
            store.stop()
    asyncio.run(run())


def test_envelope_compatibility_and_malformed_optional_context():
    from message_envelopes import build_message_envelope
    from ledger import child_report_ready_tell_id
    fields = {"report_id": "compat", "ledger_row_id": 1, "child_stream_id": "host-a:reporter",
              "msg_id": 0, "status": "done", "summary": "Ready", "effective_model": "fixture-model",
              "effective_effort": "high"}
    plain = build_message_envelope("child_report_ready", **fields)
    assert plain == (f"[pentacle-notice:{child_report_ready_tell_id('compat')}]\n[child_report_ready]\n"
        "report_id=compat\nledger_row_id=1\nchild_stream_id=host-a:reporter\nmsg_id=0\nstatus=done\n"
        "summary=Ready\neffective_model=fixture-model\neffective_effort=high")
    parsed = match_message_envelope(plain)
    assert parsed and "disposition_context" not in parsed
    for bad in ("{", "[]", '{"report_id":"other"}', "null"):
        assert match_message_envelope(plain + "\ndisposition_context=" + bad) == parsed
    assert build_message_envelope("child_report_ready", **fields, disposition_context=[]) == plain


@pytest.mark.parametrize("quality", ["stale", "missing", "ambiguous", "unavailable"])
def test_uncertain_cache_status_is_unknown_and_replay_keeps_first_notice(quality):
    import json
    async def run():
        store = Store(); store.start()
        try:
            sessions = await setup(store)
            if quality == "unavailable":
                await store.mark_work_index_pending()
            else:
                def change(conn):
                    raw = json.loads(conn.execute("SELECT record_json FROM v2_work_item_observations").fetchone()[0])
                    raw["member"]["observation"]["quality"] = quality
                    conn.execute("UPDATE v2_work_item_observations SET record_json=?", (json.dumps(raw),))
                    conn.commit()
                await store.submit(change)
            ledger = Ledger(store, sessions=sessions)
            snapshot = await ledger._report_routing_snapshot("host-a:reporter")
            saved = await store.put_report(payload(), routing_snapshot=snapshot)
            first = (await report_notices(store))[0]["body"]
            item = match_message_envelope(first)["disposition_context"]["items"][0]
            assert item["status"] == "unknown" and item["observed_at"] is None
            await cached(store, now=200)
            await ledger._announce_child_report_ready(saved)
            assert (await report_notices(store))[0]["body"] == first
        finally:
            store.stop()
    asyncio.run(run())


def test_parentless_report_remains_durable_without_notice():
    async def run():
        store = Store(); store.start()
        try:
            sessions = await setup(store)
            await store.update_session("host-a", "reporter", parent_stream_id=None)
            ledger = Ledger(store, sessions=sessions)
            snapshot = await ledger._report_routing_snapshot("host-a:reporter")
            saved = await store.put_report(payload(to_stream_id=None), routing_snapshot=snapshot)
            assert await store.get_report(saved["report_id"])
            assert await ledger._announce_child_report_ready(saved) is None
            assert await report_notices(store) == []
        finally:
            store.stop()
    asyncio.run(run())


def test_report_binding_union_missing_ids_and_stale_generation():
    from work_unattended import report_disposition_context_conn
    async def run():
        store = Store(); store.start()
        try:
            sessions = await setup(store)
            await cached(store, count=4, now=101)
            await store.update_session("host-a", "reporter", assigned_spec_id="spec_demo__item_01")
            snapshot = await Ledger(store, sessions=sessions)._report_routing_snapshot("host-a:reporter")
            report = {**payload(), "session_generation": snapshot["session_generation"],
                      "provenance_qualified_spec_ids": ["spec_demo__item_02"]}
            context = await store.submit(lambda conn: report_disposition_context_conn(conn, report))
            assert [i["spec_id"] for i in context["items"]] == [f"spec_demo__item_{i:02}" for i in range(3)]
            stale = await store.submit(lambda conn: report_disposition_context_conn(conn,
                {**report, "session_generation": "old-generation"}))
            assert stale["bindings_quality"] == "unknown"
            assert [i["spec_id"] for i in stale["items"]] == ["spec_demo__item_02"]
            absent = await store.submit(lambda conn: report_disposition_context_conn(conn, {"report_id": "unknown"}))
            assert absent["bindings_quality"] == "unknown" and absent["items"] == []
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("fallback", [False, True])
def test_optional_serialization_failure_preserves_ordinary_notice(monkeypatch, fallback):
    import work_unattended
    def malformed(conn, report):
        return {"report_id": report["report_id"], "review": "Review current item.",
                "bindings_quality": "known", "items": [], "unserializable": object()}
    monkeypatch.setattr(work_unattended, "report_disposition_context_conn", malformed)
    async def run():
        store = Store(); store.start()
        try:
            sessions = await setup(store)
            ledger = Ledger(store, sessions=sessions)
            snapshot = await ledger._report_routing_snapshot("host-a:reporter")
            saved = await store.put_report(payload(), routing_snapshot=snapshot, announce=not fallback)
            if fallback:
                await ledger._announce_child_report_ready(saved)
            row = (await report_notices(store))[0]
            assert match_message_envelope(row["body"])["report_id"] == saved["report_id"]
            assert "disposition_context=" not in row["body"]
            assert await store.get_report(saved["report_id"])
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("fallback", [False, True])
def test_context_uses_only_cache_and_never_nested_submit(monkeypatch, fallback):
    import threading
    from _shared.specs_service import SpecsSubsystem
    def forbidden(*args, **kwargs):
        raise AssertionError("report context must not read spec source")
    async def run():
        store = Store(); store.start()
        event_thread = threading.get_ident()
        try:
            sessions = await setup(store)
            ledger = Ledger(store, sessions=sessions)
            snapshot = await ledger._report_routing_snapshot("host-a:reporter")
            monkeypatch.setattr(SpecsSubsystem, "_scan_folders_by_id", forbidden)
            monkeypatch.setattr(SpecsSubsystem, "scan_work_observations", forbidden)
            submit = store.submit
            def checked(callback):
                assert threading.get_ident() == event_thread, "nested Store submission from writer"
                return submit(callback)
            monkeypatch.setattr(store, "submit", checked)
            saved = await store.put_report(payload(), routing_snapshot=snapshot, announce=not fallback)
            if fallback:
                await ledger._announce_child_report_ready(saved)
            context = match_message_envelope((await report_notices(store))[0]["body"])["disposition_context"]
            assert context["items"][0]["status"] == "in_progress"
        finally:
            store.stop()
    asyncio.run(run())
