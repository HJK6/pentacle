"""Counter invariants independent of transport attempts and candidate SHAs."""

import asyncio
import hashlib
import json

import pytest
from agent_orch import cli

from ledger import Ledger
from server import Server
from sessions import Sessions, VerbError
from store import Store
from test_qa_dispatch_counter import (
    SPEC,
    LEAD,
    TOKEN,
    SHA,
    META,
    state,
    send_msg,
    report_msg,
    issue,
    two_rejects,
)


def test_typed_repair_commission_binds_msg_zero_report_without_inflating_rejects(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "off")

    async def run():
        store, _, _, comms, ledger, server = await state()
        try:
            for msg_id, report_id in ((0, "initial-reject"), (7, "repair-reject")):
                assert (await comms.send(send_msg("qa1", msg_id=msg_id)))["delivery"] == "landed"
                report = report_msg("qa1", report_id=report_id)
                report["extras"]["qa_review"]["commission_msg_id"] = msg_id
                await ledger.ingest(report)
                result = await issue(server, "adjudicate", report_id=report_id,
                                     adjudicated_valid=True, reason="Substantive fixture defect")
                assert result["type"] == "coordination.spec_issue.adjudicate.ok", result

            # A duplicate of the repair commission cannot become a third reject.
            duplicate = report_msg("qa1", report_id="duplicate-repair")
            duplicate["extras"]["qa_review"]["commission_msg_id"] = 7
            await ledger.ingest(duplicate)
            result = await issue(server, "adjudicate", report_id="duplicate-repair",
                                 adjudicated_valid=True, reason="same commission")
            assert result["error_code"] == "qa_commission_report_conflict", result
            assert result["report_id"] == "repair-reject"
            shown = await issue(server, "show")
            assert shown["report_ids"] == ["initial-reject", "repair-reject"]
            assert {r["msg_id"]: r["report_id"] for r in shown["commissions"]} == {
                0: "initial-reject", 7: "repair-reject"}
            for report_id in ("initial-reject", "repair-reject", "duplicate-repair"):
                assert (await store.get_report(report_id))["msg_id"] == 0
            diagnosed = await issue(server, "diagnose", diagnosis_id="distinct-repair",
                                    diagnosis="two distinct commissions rejected",
                                    pivot="repair the demonstrated surface")
            assert diagnosed["next_cycle"] == 2, diagnosed
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize("commission_id", [99, -1, True, "7"])
def test_explicit_commission_never_falls_back_to_another_review(monkeypatch, commission_id):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "off")

    async def run():
        store, _, _, comms, ledger, server = await state()
        try:
            await comms.send(send_msg("qa1"))
            report = report_msg("qa1")
            report["extras"]["qa_review"]["commission_msg_id"] = commission_id
            await ledger.ingest(report)
            result = await issue(server, "adjudicate", report_id="reject-qa1",
                                 adjudicated_valid=True, reason="wrong explicit commission")
            assert result["error_code"] == "qa_report_binding_mismatch", result
            assert (await issue(server, "show"))["report_ids"] == []
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "case",
    [
        "void",
        "retry",
        "duplicate",
        "other_surface",
        "worker",
        "off",
        "diagnose",
        "sha",
        "body_conflict",
    ],
)
def test_counter_transitions_and_exemptions(monkeypatch, case):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, sessions, tmux, comms, ledger, server = await state()
        try:
            await two_rejects(comms, ledger, server)
            if case == "void":
                result = await issue(
                    server,
                    "adjudicate",
                    report_id="reject-qa1",
                    adjudicated_valid=False,
                    reason="withdrawn",
                )
                assert result["adjudicated_valid"] is False
                assert (await issue(server, "show"))["report_ids"] == ["reject-qa2"]
            elif case == "retry":
                result = await issue(
                    server,
                    "adjudicate",
                    report_id="reject-qa1",
                    adjudicated_valid=True,
                    reason="retry",
                )
                assert result["changed"] is False
                assert (await comms.send(send_msg("qa1")))["delivery"] == "landed"
                assert len((await issue(server, "show"))["commissions"]) == 2
                return
            elif case == "duplicate":
                await ledger.ingest(report_msg("qa1", report_id="new-report-id"))
                for valid in (True, False):
                    await issue(
                        server,
                        "adjudicate",
                        report_id="reject-qa1",
                        adjudicated_valid=valid,
                        reason="test",
                    )
                    result = await issue(
                        server,
                        "adjudicate",
                        report_id="new-report-id",
                        adjudicated_valid=True,
                        reason="retry id",
                    )
                    assert result["error_code"] == "qa_commission_report_conflict"
                    assert result["report_id"] == "reject-qa1"
                return
            elif case == "worker":
                msg = send_msg("worker")
                for key in META:
                    msg.pop(key)
                assert (await comms.send(msg))["delivery"] == "landed"
                return
            elif case == "off":
                monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "off")
                with pytest.raises(VerbError) as exc:
                    await comms.send(send_msg("qa3"))
                assert exc.value.code == "qa_dispatch_reject_limit"
                assert exc.value.extra["report_ids"] == ["reject-qa1", "reject-qa2"]
                result = await issue(
                    server,
                    "diagnose",
                    diagnosis_id="pivot-1",
                    diagnosis="wrong abstraction",
                    pivot="different acceptance scope",
                )
                assert result["next_cycle"] == 2
                assert (await comms.send(send_msg("qa3", qa_cycle=2)))[
                    "delivery"
                ] == "landed"
                return
            elif case == "diagnose":
                params = dict(
                    diagnosis_id="pivot-1",
                    diagnosis="wrong abstraction",
                    pivot="different acceptance scope",
                )
                first = await issue(server, "diagnose", **params)
                assert first["next_cycle"] == 2
                replay = await issue(server, "diagnose", **params)
                assert replay["next_cycle"] == 2
                conflict = await issue(
                    server, "diagnose", **{**params, "pivot": "different"}
                )
                assert conflict["error_code"] == "qa_diagnosis_conflict"
                assert (await issue(server, "show"))["report_ids"] == []
                with pytest.raises(VerbError) as exc:
                    await comms.send(send_msg("qa3"))
                assert exc.value.code == "qa_cycle_conflict"
                assert (await comms.send(send_msg("qa3", qa_cycle=2)))[
                    "delivery"
                ] == "landed"
                # A late old-cycle void/revalidate does not affect current cycle.
                for valid in (False, True):
                    await issue(
                        server,
                        "adjudicate",
                        report_id="reject-qa1",
                        adjudicated_valid=valid,
                        reason="late",
                    )
                assert (await issue(server, "show"))["report_ids"] == []
                return
            elif case == "sha":
                with pytest.raises(VerbError) as exc:
                    await comms.send(send_msg("qa3", text="review new SHA " + "c" * 40))
                assert exc.value.code == "qa_dispatch_reject_limit"
                return
            elif case == "body_conflict":
                with pytest.raises(VerbError) as exc:
                    await comms.send(
                        send_msg("qa1", text="a new review under reused msg id")
                    )
                assert exc.value.code == "qa_commission_conflict"
                return
            result = await comms.send(
                send_msg(
                    "qa3",
                    **({"qa_surface": "other"} if case == "other_surface" else {})
                )
            )
            assert result["delivery"] == "landed"
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({"stream_token": None}, "qa_unauthorized"),
        ({"stream_token": "wrong"}, "qa_unauthorized"),
        ({"from_stream_id": "localhost:foreign"}, "qa_unauthorized"),
        ({"cycle": True}, "qa_invalid_request"),
        ({"surface": "New surface"}, "qa_invalid_request"),
        ({"spec_id": "spec_other__scope"}, "qa_spec_mismatch"),
        ({"cycle": 2}, "qa_report_binding_mismatch"),
        ({"report_id": "missing"}, "qa_report_invalid"),
        ({"adjudicated_valid": "true"}, "qa_invalid_request"),
    ],
)
def test_invalid_adjudications_do_not_write(monkeypatch, mutation, expected):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, _, comms, ledger, server = await state()
        try:
            await comms.send(send_msg("qa1"))
            await ledger.ingest(report_msg("qa1"))
            result = await issue(
                server,
                "adjudicate",
                **{
                    "report_id": "reject-qa1",
                    "adjudicated_valid": True,
                    "reason": "test",
                    **mutation,
                }
            )
            assert result["error_code"] == expected, result
            assert (await issue(server, "show"))["report_ids"] == []
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"qa_review": {}},
        {
            "qa_review": {
                "candidate_identity": SHA,
                "reviewed_scope": "scope",
                "gate_evidence_digest": "bad",
            }
        },
        {
            "qa_review": {
                "candidate_identity": "BASE14 descriptive candidate",
                "reviewed_scope": "admission",
                "gate_evidence_digest": "b" * 64,
            }
        },
    ],
)
def test_missing_typed_evidence_cannot_count(monkeypatch, evidence):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, _, comms, ledger, server = await state()
        try:
            await comms.send(send_msg("qa1"))
            await ledger.ingest(report_msg("qa1", extras=evidence))
            result = await issue(
                server,
                "adjudicate",
                report_id="reject-qa1",
                adjudicated_valid=True,
                reason="test",
            )
            assert result["error_code"] == "qa_report_invalid"
        finally:
            store.stop()

    asyncio.run(run())


def test_qa_report_producer_bindings_reach_commissioned_store(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, _, comms, ledger, server = await state()
        try:
            for reviewer, verdict in (("qa1", "reject"), ("qa2", "accept")):
                await comms.send(send_msg(reviewer))
                args = cli.build_parser().parse_args([
                    "report", "--msg-id", "0", "--status", "done",
                    "--result", json.dumps({
                        "summary": "Reviewed admission", "findings": [],
                        "next_action": "repair" if verdict == "reject" else "lead_proceed",
                        "extras": {"review_note": "retained"},
                    }),
                    "--qa-verdict", verdict, "--target-sha", SHA,
                    "--qa-reviewed-scope", "admission",
                    "--qa-gate-evidence-digest", "b" * 64,
                ])
                payload, blob = cli._report_payload_from_args(args)
                assert blob is None
                assert payload["extras"] == {
                    "review_note": "retained",
                    "qa_review": {
                        "candidate_identity": SHA,
                        "reviewed_scope": "admission",
                        "gate_evidence_digest": "b" * 64,
                    },
                }
                report_id = f"produced-{reviewer}"
                await ledger.ingest(report_msg(reviewer, report_id=report_id, **payload))
                durable = await store.get_report(report_id)
                assert durable["qa_verdict"] == verdict
                assert durable["from_stream_id"] == f"localhost:{reviewer}"
                assert durable["session_generation"]
                if verdict == "reject":
                    result = await issue(server, "adjudicate", report_id=report_id,
                                         adjudicated_valid=True, reason="in scope")
                    assert result["type"] == "coordination.spec_issue.adjudicate.ok", result
        finally:
            store.stop()

    asyncio.run(run())


def test_concurrent_ids_and_restart_keep_one_representative(monkeypatch, tmp_path):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        path = tmp_path / "counter.db"
        store, _, _, comms, ledger, server = await state(path)
        try:
            await comms.send(send_msg("qa1"))
            for ident in ("one", "two"):
                await ledger.ingest(report_msg("qa1", report_id=ident))
            results = await asyncio.gather(
                *(
                    issue(
                        server,
                        "adjudicate",
                        report_id=i,
                        adjudicated_valid=True,
                        reason="concurrent",
                    )
                    for i in ("one", "two")
                )
            )
            assert sum(r["type"].endswith(".ok") for r in results) == 1
            ids = (await issue(server, "show"))["report_ids"]
            assert len(ids) == 1
        finally:
            store.stop()
        reopened = Store(str(path))
        reopened.start()
        try:
            result = await reopened.qa_issue(
                "show", {"spec_id": SPEC, "surface": "admission"}
            )
            assert result["report_ids"] == ids
            assert len(result["commissions"]) == 1
        finally:
            reopened.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "relationship,allowed",
    [
        ("peer", False),
        ("worker", False),
        ("parent", True),
        ("successor", True),
        ("chain", True),
    ],
)
def test_adjudicator_lineage_not_just_shared_tags(monkeypatch, relationship, allowed):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, sessions, _, comms, ledger, server = await state()
        try:
            await comms.send(send_msg("qa1"))
            await ledger.ingest(report_msg("qa1"))
            token = "other-token"
            fields = {}
            if relationship in ("successor", "chain"):
                fields["handoff_from_stream_id"] = LEAD
            if relationship == "chain":
                await sessions.open(
                    "localhost", "middle", role="lead", spec_id=SPEC, **fields
                )
                fields["handoff_from_stream_id"] = "localhost:middle"
            await sessions.open(
                "localhost",
                "other",
                role="worker" if relationship == "worker" else "lead",
                spec_id=SPEC,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                token_hash_version="sha256:v1",
                **fields
            )
            if relationship == "parent":
                await store.update_session(
                    "localhost", "lead", parent_stream_id="localhost:other"
                )
            result = await issue(
                server,
                "adjudicate",
                report_id="reject-qa1",
                adjudicated_valid=True,
                reason="test",
                stream_token=token,
                from_stream_id="localhost:other",
            )
            assert result["type"].endswith(".ok") == allowed, result
            if not allowed:
                assert result["error_code"] == "qa_unauthorized"
        finally:
            store.stop()

    asyncio.run(run())


def test_reopened_reviewer_generation_is_a_new_commission(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, sessions, _, comms, ledger, server = await state()
        try:
            await comms.send(send_msg("qa1"))
            await ledger.ingest(report_msg("qa1"))
            old = (await store.fetch_session("localhost", "qa1"))["session_generation"]
            await store.update_session("localhost", "qa1", status="closed")
            await sessions.open(
                "localhost",
                "qa1",
                provider="claude",
                role="qa",
                spec_id=SPEC,
                parent_stream_id=LEAD,
                session_generation="new-generation",
            )
            assert (await store.fetch_session("localhost", "qa1"))[
                "session_generation"
            ] != old
            await comms.send(send_msg("qa1"))
            await ledger.ingest(
                report_msg(
                    "qa1",
                    report_id="new-generation-report",
                    target_sha="c" * 40,
                    extras={
                        "qa_review": {
                            "candidate_identity": "c" * 40,
                            "reviewed_scope": "admission",
                            "gate_evidence_digest": "d" * 64,
                        }
                    },
                )
            )
            for ident in ("reject-qa1", "new-generation-report"):
                assert (
                    await issue(
                        server,
                        "adjudicate",
                        report_id=ident,
                        adjudicated_valid=True,
                        reason="in scope",
                    )
                )["type"].endswith(".ok")
            assert len((await issue(server, "show"))["report_ids"]) == 2
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "role,mode,fields,expected",
    [
        ("qa", "enforce", {}, "qa_scope_required"),
        ("QA", "enforce", {}, "qa_scope_required"),
        ("worker", "enforce", {}, None),
        ("qa", "off", {}, None),
        ("worker", "enforce", {"qa_surface": "admission"}, "qa_scope_required"),
    ],
)
def test_classification_is_typed_not_prose(monkeypatch, role, mode, fields, expected):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", mode)

    async def run():
        store, _, tmux, comms, _, _ = await state()
        try:
            await store.update_session("localhost", "qa3", role=role)
            msg = send_msg("qa3", text="QA REJECT review reject reject")
            for key in META:
                msg.pop(key)
            msg.update(fields)
            if expected:
                with pytest.raises(VerbError) as exc:
                    await comms.send(msg)
                assert exc.value.code == expected
                assert tmux.pastes == []
            else:
                assert (await comms.send(msg))["delivery"] == "landed"
        finally:
            store.stop()

    asyncio.run(run())


def test_diagnosis_concurrency_and_second_cycle_bound(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, _, comms, ledger, server = await state()
        try:
            await two_rejects(comms, ledger, server)
            results = await asyncio.gather(
                *(
                    issue(
                        server,
                        "diagnose",
                        diagnosis_id="same-pivot",
                        diagnosis="cause",
                        pivot="scope repair",
                    )
                    for _ in range(6)
                )
            )
            assert all(r.get("next_cycle") == 2 for r in results)
            assert len((await issue(server, "show"))["diagnoses"]) == 1
            for name in ("qa1", "qa2"):
                await comms.send(send_msg(name, msg_id=1, qa_cycle=2))
                await ledger.ingest(
                    report_msg(name, report_id="cycle2-" + name, msg_id=1)
                )
                assert (
                    await issue(
                        server,
                        "adjudicate",
                        report_id="cycle2-" + name,
                        cycle=2,
                        adjudicated_valid=True,
                        reason="new defect",
                    )
                )["type"].endswith(".ok")
            with pytest.raises(VerbError) as exc:
                await comms.send(send_msg("qa3", qa_cycle=2))
            assert exc.value.code == "qa_dispatch_reject_limit"
            assert exc.value.extra["report_ids"] == ["cycle2-qa1", "cycle2-qa2"]
        finally:
            store.stop()

    asyncio.run(run())


def test_wire_private_auth_cannot_authorize_and_reopened_coordinator_cannot_adjudicate(
    monkeypatch,
):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, sessions, _, comms, ledger, server = await state()
        try:
            await comms.send(send_msg("qa1"))
            await ledger.ingest(report_msg("qa1"))
            params = dict(report_id="reject-qa1", adjudicated_valid=True, reason="test")
            result = await issue(
                server,
                "adjudicate",
                **params,
                stream_token="wrong",
                _auth_context={"token_verified": True, "stream_id": LEAD}
            )
            assert result["error_code"] == "qa_unauthorized"
            await store.update_session("localhost", "lead", status="closed")
            await sessions.open(
                "localhost",
                "lead",
                role="lead",
                spec_id=SPEC,
                session_generation="new-lead",
                token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
                token_hash_version="sha256:v1",
            )
            result = await issue(server, "adjudicate", **params)
            assert result["error_code"] == "qa_unauthorized"
        finally:
            store.stop()

    asyncio.run(run())


def test_resolved_legacy_target_alias_uses_canonical_scope(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, _, comms, _, server = await state()
        try:
            alias = "spec_pentacle__qa_counter_legacy"
            store.set_spec_identity_resolver(
                lambda value: SPEC if value in (SPEC, alias) else None
            )
            await store.update_session(
                "localhost", "qa3", spec_id=alias, spec_ids=[alias]
            )
            assert (await comms.send(send_msg("qa3")))["delivery"] == "landed"
            assert (await issue(server, "show"))["commissions"][0]["spec_id"] == SPEC
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["off", "enforce"])
@pytest.mark.parametrize(
    "case",
    [
        "misfiled_single_open",
        "misfiled_retry_void_revalidate",
        "two_open_candidates",
        "exact_key_wins_over_open_candidates",
        "superseding_id_conflicts",
        "no_commission",
        "old_generation_does_not_cross",
        "new_generation_binds",
    ],
)
def test_misfiled_report_commission_binding(monkeypatch, mode, case):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", mode)

    async def run():
        store, sessions, _, comms, ledger, server = await state()
        try:
            async def adjudicate(ident="reject-qa1", valid=True):
                return await issue(
                    server, "adjudicate", report_id=ident,
                    adjudicated_valid=valid, reason="in-scope admission defect",
                )

            async def assert_unwritten(expected_msg_ids):
                shown = await issue(server, "show")
                assert shown["report_ids"] == []
                assert sorted(row["msg_id"] for row in shown["commissions"]) == expected_msg_ids
                assert all(row["report_id"] is None for row in shown["commissions"])
                assert await store.submit(lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM v2_qa_adjudications"
                ).fetchone()[0]) == 0

            if case == "no_commission":
                await ledger.ingest(report_msg("qa3"))
                result = await adjudicate("reject-qa3")
                assert result["error_code"] == "qa_report_binding_mismatch", result
                assert result["candidate_msg_ids"] == []
                await assert_unwritten([])
                return

            if case in ("old_generation_does_not_cross", "new_generation_binds"):
                # File the old report without ever commissioning its generation.
                await ledger.ingest(report_msg("qa1"))
                old = (await store.fetch_session("localhost", "qa1"))["session_generation"]
                await store.update_session("localhost", "qa1", status="closed")
                await sessions.open(
                    "localhost", "qa1", provider="claude", role="qa",
                    spec_id=SPEC, parent_stream_id=LEAD,
                    session_generation="new-generation",
                )
                new = (await store.fetch_session("localhost", "qa1"))["session_generation"]
                assert new != old
                await comms.send(send_msg("qa1"))
                result = await adjudicate()
                assert result["error_code"] == "qa_report_binding_mismatch", result
                # Final-only assertion: added after the base-green generation control.
                assert result["candidate_msg_ids"] == []
                await assert_unwritten([0])
                shown = await issue(server, "show")
                assert shown["commissions"][0]["generation"] == new
                assert (await store.get_report("reject-qa1"))["session_generation"] == old
                if case == "old_generation_does_not_cross":
                    return
                await ledger.ingest(report_msg("qa1", report_id="new-generation-report", msg_id=1))
                result = await adjudicate("new-generation-report")
                assert result["type"] == "coordination.spec_issue.adjudicate.ok", result
                shown = await issue(server, "show")
                assert shown["report_ids"] == ["new-generation-report"]
                assert len(shown["commissions"]) == 1
                assert shown["commissions"][0]["msg_id"] == 0
                assert shown["commissions"][0]["report_id"] == "new-generation-report"
                assert (await store.get_report("new-generation-report"))["msg_id"] == 1
                return

            await comms.send(send_msg("qa1"))
            if case in ("two_open_candidates", "exact_key_wins_over_open_candidates"):
                await comms.send(send_msg("qa1", msg_id=1, text="review qa1 again"))
                if case == "exact_key_wins_over_open_candidates":
                    await comms.send(send_msg("qa1", msg_id=2, text="review qa1 a third time"))
                await ledger.ingest(report_msg("qa1", msg_id=5 if case == "two_open_candidates" else 1))
                result = await adjudicate()
                if case == "two_open_candidates":
                    assert result["error_code"] == "qa_report_binding_mismatch", result
                    assert result["candidate_msg_ids"] == [0, 1]
                    await assert_unwritten([0, 1])
                else:
                    assert result["type"] == "coordination.spec_issue.adjudicate.ok", result
                    shown = await issue(server, "show")
                    assert shown["report_ids"] == ["reject-qa1"]
                    assert {row["msg_id"]: row["report_id"] for row in shown["commissions"]} == {
                        0: None, 1: "reject-qa1", 2: None,
                    }
                return

            await ledger.ingest(report_msg("qa1", msg_id=1))
            result = await adjudicate()
            assert result["type"] == "coordination.spec_issue.adjudicate.ok", result
            shown = await issue(server, "show")
            assert shown["report_ids"] == ["reject-qa1"]
            assert len(shown["commissions"]) == 1
            assert shown["commissions"][0]["msg_id"] == 0
            assert shown["commissions"][0]["report_id"] == "reject-qa1"
            assert (await store.get_report("reject-qa1"))["msg_id"] == 1

            if case == "misfiled_retry_void_revalidate":
                # A later exact-key commission must not steal the representative.
                await comms.send(send_msg("qa1", msg_id=1, text="review qa1 again"))
                assert (await adjudicate())["changed"] is False
                for valid in (False, True):
                    result = await adjudicate(valid=valid)
                    assert result["type"] == "coordination.spec_issue.adjudicate.ok", result
                    assert result["changed"] is True
                    shown = await issue(server, "show")
                    assert shown["report_ids"] == (["reject-qa1"] if valid else [])
                    assert {row["msg_id"]: row["report_id"] for row in shown["commissions"]} == {
                        0: "reject-qa1", 1: None,
                    }
                assert (await store.get_report("reject-qa1"))["msg_id"] == 1
            elif case == "superseding_id_conflicts":
                await ledger.ingest(report_msg("qa1", report_id="second", msg_id=2))
                result = await adjudicate("second")
                assert result["error_code"] == "qa_commission_report_conflict", result
                assert result["report_id"] == "reject-qa1"
                assert (await issue(server, "show"))["report_ids"] == ["reject-qa1"]
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "case,variant",
    [
        ("void_clears", "off"),
        ("one_pending_admits", "off"),
        ("misfiled_counts", "off"),
        ("malformed_never_holds", "missing"),
        ("malformed_never_holds", "nonhex"),
        ("malformed_never_holds", "mixed_missing"),
        ("malformed_never_holds", "mixed_nonhex"),
        ("check_only_and_validate_only_refuse", "check_only"),
        ("check_only_and_validate_only_refuse", "validate_only"),
        ("later_cycle_admits", "off"),
        ("enforce_mode_same", "void_clears"),
        ("enforce_mode_same", "valid_rejects"),
    ],
)
def test_adjudication_required_transitions(monkeypatch, case, variant):
    from spawnctl import SpawnCtl
    from store_qa import QaError
    from test_spec_binding_provenance import SpawnTmux
    from test_window_schedule_contract import RouteProbeSpecs

    monkeypatch.setenv(
        "PENTACLE_QA_DISPATCH_MODE", "enforce" if case == "enforce_mode_same" else "off"
    )

    async def run():
        store, sessions, tmux, comms, ledger, server = await state()
        expected_ids = ["reject-qa1", "reject-qa2"]
        expected_extra = {
            "spec_id": SPEC,
            "surface": "admission",
            "cycle": 1,
            "report_ids": expected_ids,
            "commands": [
                f"agent-orch spec-issue adjudicate {rid} --spec-id {SPEC} "
                "--surface admission --cycle 1 --valid|--void --reason '<reason>'"
                for rid in expected_ids
            ],
        }
        try:
            if case == "enforce_mode_same" and variant == "valid_rejects":
                await two_rejects(comms, ledger, server)
                with pytest.raises(VerbError) as exc:
                    await comms.send(send_msg("qa3"))
                assert exc.value.code == "qa_dispatch_reject_limit"
                assert exc.value.extra["report_ids"] == expected_ids
            else:
                for name in ("qa1", "qa2"):
                    assert (await comms.send(send_msg(name)))["delivery"] == "landed"
                if case == "later_cycle_admits":
                    # Create all commissions before either count reaches two.
                    for name in ("qa1", "qa2"):
                        assert (await comms.send(send_msg(name, msg_id=1)))["delivery"] == "landed"
                    for name in ("qa1", "qa2"):
                        await ledger.ingest(report_msg(name, msg_id=1, report_id=f"valid-{name}"))
                        result = await issue(
                            server, "adjudicate", report_id=f"valid-{name}",
                            adjudicated_valid=True, reason="in-scope defect",
                        )
                        assert result["adjudicated_valid"] is True
                for name in ("qa1", "qa2"):
                    if case == "one_pending_admits" and name == "qa2":
                        continue
                    changes = {}
                    if case == "misfiled_counts" and name == "qa1":
                        changes["msg_id"] = 1
                    if case == "malformed_never_holds" and not (
                        variant.startswith("mixed_") and name == "qa1"
                    ):
                        changes["extras"] = (
                            {"qa_review": {
                                "candidate_identity": SHA,
                                "reviewed_scope": "admission",
                                "gate_evidence_digest": "g" * 64,
                            }} if variant.endswith("nonhex") else {}
                        )
                    await ledger.ingest(report_msg(name, **changes))

            if case == "later_cycle_admits" or (
                case == "enforce_mode_same" and variant == "valid_rejects"
            ):
                result = await issue(
                    server, "diagnose", diagnosis_id="pending-pivot",
                    diagnosis="wrong abstraction", pivot="different acceptance scope",
                )
                assert result["next_cycle"] == 2
                assert (await comms.send(send_msg("qa3", qa_cycle=2)))["delivery"] == "landed"
                return
            if case in ("one_pending_admits", "malformed_never_holds"):
                assert (await comms.send(send_msg("qa3")))["delivery"] == "landed"
                return

            before = list(tmux.pastes)
            commissions = (await issue(server, "show"))["commissions"]
            if case == "check_only_and_validate_only_refuse":
                panes = SpawnTmux()
                ctl = SpawnCtl(store, sessions, tmux=panes, specs=RouteProbeSpecs())
                if variant == "check_only":
                    with pytest.raises(VerbError) as exc:
                        await ctl.admit_schedule({
                            "objective": "Review admission", "command": "provider",
                            "provider": "claude", "model": "claude-opus-4-8", "effort": "high",
                            "parent_stream_id": LEAD, "from_stream_id": LEAD,
                            "stream_token": TOKEN, "role": "qa", "spec_id": SPEC, **META,
                        }, "localhost", admission_name="qa-preflight")
                else:
                    owner = await store.fetch_session("localhost", "lead")
                    reviewer = await store.fetch_session("localhost", "qa3")
                    with pytest.raises(QaError) as exc:
                        await store.qa_admit(
                            scope={"spec_id": SPEC, "surface": "admission", "cycle": 1},
                            actor=LEAD, actor_generation=owner["session_generation"],
                            reviewer="localhost:qa3", generation=reviewer["session_generation"],
                            msg_id=0, payload_hash="dry-admission", target_specs=[SPEC],
                            enforce=False, prepare_only=True,
                        )
                assert panes.live == set()
            else:
                with pytest.raises(VerbError) as exc:
                    await comms.send(send_msg("qa3"))
            assert exc.value.code == "qa_adjudication_required"
            assert exc.value.extra == expected_extra
            assert tmux.pastes == before
            assert (await issue(server, "show"))["commissions"] == commissions

            if case == "void_clears" or case == "enforce_mode_same":
                result = await issue(
                    server, "adjudicate", report_id="reject-qa1",
                    adjudicated_valid=False, reason="finding withdrawn",
                )
                assert result["adjudicated_valid"] is False
                assert (await comms.send(send_msg("qa3")))["delivery"] == "landed"
        finally:
            store.stop()

    asyncio.run(run())
