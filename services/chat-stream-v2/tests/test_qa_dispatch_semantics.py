"""Counter invariants independent of transport attempts and candidate SHAs."""

import asyncio
import hashlib
import json

import pytest

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
