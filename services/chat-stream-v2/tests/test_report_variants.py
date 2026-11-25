"""Focused coverage for report payload variants that v2 deliberately rejects."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from ledger import Ledger  # noqa: E402
from routing_integrity import RoutingIntegrity  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from store import Store  # noqa: E402

_EXAMPLE_DEPENDENCY_AVAILABLE = False


PROVENANCE_SPEC = "example__report_provenance"
CANONICAL_PROVENANCE_SPEC = f"spec_{PROVENANCE_SPEC}"


def _spec_binding(spec_id: str) -> list[dict[str, str]]:
    return [{
        "spec_id": spec_id,
        "provenance": "spawn_explicit",
        "granting_principal": "operator",
        "granted_at": "2026-08-21T00:00:00Z",
    }]


async def _provenance_state(tmp_path):
    store = Store(str(tmp_path / "provenance.db"))
    store.start()
    sessions = Sessions(store, local_host="alpha")
    await sessions.open(
        "alpha",
        "worker",
        provider="codex",
        requested_model="gpt-5.6-luna",
        requested_effort="max",
        effective_model="gpt-5.6-luna",
        effective_effort="max",
        session_generation="provenance-generation",
        spec_id=PROVENANCE_SPEC,
        spec_ids=[PROVENANCE_SPEC],
        spec_binding_provenance=_spec_binding(PROVENANCE_SPEC),
    )
    frames: list[dict] = []

    async def broadcast(frame: dict) -> None:
        frames.append(frame)

    ledger = Ledger(
        store,
        sessions=sessions,
        routing_integrity=RoutingIntegrity(store, sessions),
        broadcast=broadcast,
    )
    return store, sessions, ledger, frames


def _provenance_report(report_id: str) -> dict:
    return {
        "report_id": report_id,
        "from_stream_id": "alpha:worker",
        "msg_id": 7,
        "status": "done",
        "summary": "provenance fixture (test report body)",
        "findings": [], "no_findings": True,
        "next_action": "continue",
    }


def _claim_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    root = tmp_path / "memory"
    root.mkdir()
    source = root / "spec.md"
    source.write_text(
        "- [ ] check-1\n- [ ] check-2\n- [ ] check-3\n- [ ] check-4\n- [ ] validation\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "spec.md"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=pytest", "-c", "user.email=user@example.com",
         "commit", "-q", "-m", "fixture"],
        cwd=root, check=True,
    )
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    monkeypatch.setenv("EXAMPLE_MEMORY_ROOT", str(root))
    return "example_spec__close_truth", sha


def _ac_claim(spec_id: str, sha: str, checked: bool) -> dict:
    return {
        "spec_id": spec_id,
        "spec_source": {"path": "spec.md", "sha": sha},
        "claims": [{"index": index, "checked": checked} for index in range(1, 6)],
    }


class _CloseImmediatelyTmux:
    async def has_session(self, _name: str) -> bool:
        return False


def _claim_report(report_id: str, claim: dict) -> dict:
    return {
        "report_id": report_id,
        "from_stream_id": "alpha:worker",
        "msg_id": 1,
        "status": "done",
        "summary": "claim fixture (test report body)",
        "findings": [], "no_findings": True,
        "next_action": "continue",
        "ac_claim": claim,
    }


@pytest.mark.skipif(
    not _EXAMPLE_DEPENDENCY_AVAILABLE,
    reason="optional specification resolver is not part of this public fixture",
)
def test_close_flags_false_ac_claim(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        spec_id, sha = _claim_source(tmp_path, monkeypatch)
        store = Store(":memory:")
        store.start()
        alerts = []
        try:
            sessions = Sessions(store, tmux=_CloseImmediatelyTmux(), local_host="alpha")
            sessions.alerts = type("Alerts", (), {"emit": lambda _self, kind, **fields: alerts.append((kind, fields))})()
            await sessions.open("alpha", "worker", visibility="hidden")
            await sessions.refresh()
            reply = await Ledger(store, sessions=sessions).report({
                **_claim_report("false-ac-claim", _ac_claim(spec_id, sha, True)),
                "terminate": True,
            })
            assert reply["ac_claim_verified"] == "mismatch"
            assert reply["ac_claim_mismatch"]["unverified_indices"] == [1, 2, 3, 4, 5]
            assert (await store.get_report("false-ac-claim"))["claim_verified"] == "mismatch"
            assert [kind for kind, _fields in alerts] == ["close_claim_mismatch"]
        finally:
            store.stop()

    asyncio.run(go())


@pytest.mark.skipif(
    not _EXAMPLE_DEPENDENCY_AVAILABLE,
    reason="optional specification resolver is not part of this public fixture",
)
def test_matching_ac_claim_verifies(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        spec_id, sha = _claim_source(tmp_path, monkeypatch)
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="alpha")
            await sessions.open("alpha", "worker", visibility="hidden")
            await sessions.refresh()
            reply = await Ledger(store, sessions=sessions).report(
                _claim_report("matching-ac-claim", _ac_claim(spec_id, sha, False))
            )
            assert reply["ac_claim_verified"] == "match"
            assert "ac_claim_mismatch" not in reply
            assert (await store.get_report("matching-ac-claim"))["claim_verified"] == "match"
            assert getattr(sessions, "alerts", None) is None
        finally:
            store.stop()

    asyncio.run(go())


@pytest.mark.skipif(
    not _EXAMPLE_DEPENDENCY_AVAILABLE,
    reason="optional specification resolver is not part of this public fixture",
)
def test_unresolvable_spec_sha_marks_unverifiable(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        spec_id, _sha = _claim_source(tmp_path, monkeypatch)
        store = Store(":memory:")
        store.start()
        alerts = []
        try:
            sessions = Sessions(store, local_host="alpha")
            sessions.alerts = type("Alerts", (), {"emit": lambda _self, kind, **fields: alerts.append((kind, fields))})()
            await sessions.open("alpha", "worker", visibility="hidden")
            await sessions.refresh()
            reply = await Ledger(store, sessions=sessions).report(
                _claim_report("unverifiable-ac-claim", _ac_claim(spec_id, "0" * 40, False))
            )
            assert reply["ac_claim_verified"] == "unverifiable"
            assert "ac_claim_mismatch" not in reply
            assert (await store.get_report("unverifiable-ac-claim"))["claim_verified"] == "unverifiable"
            assert [kind for kind, _fields in alerts] == ["close_claim_mismatch"]
        finally:
            store.stop()

    asyncio.run(go())


def test_result_blob_variant_is_rejected_with_inline_hint_before_persistence() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            with pytest.raises(VerbError) as raised:
                await Ledger(store).ingest(
                    {
                        "report_id": "file-variant",
                        "from_stream_id": "hosta:worker",
                        "msg_id": 0,
                        "status": "done",
                        "result_blob_sha": "a" * 64,
                    }
                )

            assert raised.value.code == "unsupported_in_v2"
            assert "agent-orch report --result" in raised.value.extra["hint"]
            assert await store.get_report("file-variant") is None
        finally:
            store.stop()

    asyncio.run(go())


@pytest.mark.parametrize(
    ("container", "value", "field"),
    [
        ("details", {"qa_verdict": "accept"}, "details.qa_verdict"),
        ("extras", {"nested": {"qa_verdict": "reject"}}, "extras.nested.qa_verdict"),
        ("details", {"completion_kind": "implementation_ready"}, "details.completion_kind"),
        ("extras", {"nested": {"completion_kind": "implementation_ready"}}, "extras.nested.completion_kind"),
        ("details", {"qa_attestation": {"stream_id": "alpha:qa", "report_id": "qa-1"}}, "details.qa_attestation"),
        ("extras", {"nested": {"qa_attestation": {"stream_id": "alpha:qa", "report_id": "qa-1"}}}, "extras.nested.qa_attestation"),
    ],
)
def test_nested_governance_field_is_rejected_before_any_durable_row(
    container: str, value: dict[str, object], field: str,
) -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            report_id = f"nested-verdict-{container}"
            with pytest.raises(VerbError) as raised:
                await Ledger(store).ingest({
                    "report_id": report_id,
                    "from_stream_id": "alpha:worker",
                    "msg_id": 1,
                    "status": "done",
                    "summary": "misfiled verdict (test report body)",
                    "findings": [], "no_findings": True,
                    "next_action": "refile",
                    container: value,
                })

            assert raised.value.code == "schema_error"
            assert any(
                violation["field"] == field
                and violation["code"] == "reserved_field"
                and "top-level" in violation["detail"]
                for violation in raised.value.extra["schema_violations"]
            )
            assert await store.get_report(report_id) is None
        finally:
            store.stop()

    asyncio.run(go())


def test_report_ack_echoes_every_submitted_governance_field() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="alpha")
            await sessions.open("alpha", "worker", visibility="hidden", role="worker")
            agent_attestation = {
                "release_id": "example-build",
                "manifest_sha256": "b" * 64,
                "host": "alpha",
            }
            ledger = Ledger(store, sessions=sessions)
            reply = await ledger.report({
                "report_id": "ack-governance-fields",
                "from_stream_id": "alpha:worker",
                "msg_id": 1,
                "status": "done",
                "summary": "typed readiness (test report body)",
                "findings": [], "no_findings": True,
                "next_action": "continue",
                "completion_kind": "implementation_ready",
                "qa_verdict": "accept",
                "target_sha": "0123456789abcdef0123456789abcdef01234567",
                "qa_attestation": {"stream_id": "alpha:qa", "report_id": "qa-1"},
                "agent_orch_attestation": agent_attestation,
            })
            assert reply["completion_kind"] == "implementation_ready"
            assert reply["qa_verdict"] == "accept"
            assert reply["target_sha"] == "0123456789abcdef0123456789abcdef01234567"
            assert reply["qa_attestation"] == {"stream_id": "alpha:qa", "report_id": "qa-1"}
            assert reply["agent_orch_attestation"] == agent_attestation
            assert (await store.get_report("ack-governance-fields"))["agent_orch_attestation"] == agent_attestation
            with pytest.raises(VerbError) as raised:
                await ledger.report({
                    "report_id": "ack-governance-fields",
                    "from_stream_id": "alpha:worker",
                    "msg_id": 1,
                    "status": "done",
                    "summary": "typed readiness (test report body)",
                    "findings": [], "no_findings": True,
                    "next_action": "continue",
                    "completion_kind": "implementation_ready",
                    "qa_verdict": "accept",
                    "target_sha": "0123456789abcdef0123456789abcdef01234567",
                    "qa_attestation": {"stream_id": "alpha:qa", "report_id": "qa-1"},
                    "agent_orch_attestation": {
                        **agent_attestation, "release_id": "other-build",
                    },
                })
            assert raised.value.code == "report_id_replay_conflict"
        finally:
            store.stop()

    asyncio.run(go())


def test_top_level_qa_verdict_persists_to_the_durable_report_row() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="alpha")
            await sessions.open("alpha", "worker", visibility="hidden", role="worker")
            await Ledger(store, sessions=sessions).ingest({
                "report_id": "top-level-verdict",
                "from_stream_id": "alpha:worker",
                "msg_id": 1,
                "status": "done",
                "summary": "typed verdict (test report body)",
                "findings": [], "no_findings": True,
                "next_action": "continue",
                "qa_verdict": "accept",
            })
            assert (await store.get_report("top-level-verdict"))["qa_verdict"] == "accept"
        finally:
            store.stop()

    asyncio.run(go())


FULL_SHA = "0123456789abcdef0123456789abcdef01234567"


def _qa_grade_report(report_id: str, stream_id: str, **fields: object) -> dict[str, object]:
    return {
        "report_id": report_id,
        "from_stream_id": stream_id,
        "msg_id": 1,
        "status": "done",
        "summary": "typed QA verdict (test report body)",
        "findings": [], "no_findings": True,
        "next_action": "continue",
        **fields,
    }


@pytest.mark.parametrize(
    ("session_fields", "report_fields", "expected_field", "expected_code"),
    (
        ({"role": "qa"}, {"qa_verdict": "accept"}, "target_sha", "missing_field"),
        ({"phase": "qa"}, {"target_sha": FULL_SHA}, "qa_verdict", "missing_field"),
        ({"role": "qa"}, {"qa_verdict": None, "target_sha": FULL_SHA}, "qa_verdict", "invalid_value"),
        ({"role": "qa"}, {"qa_verdict": "reject", "target_sha": "abc123"}, "target_sha", "invalid_value"),
        ({"phase": "QA"}, {"qa_verdict": "accept", "target_sha": "z" * 40}, "target_sha", "invalid_value"),
    ),
)
def test_terminal_qa_grade_report_rejects_missing_or_malformed_binding_before_side_effects(
    session_fields: dict[str, str],
    report_fields: dict[str, object],
    expected_field: str,
    expected_code: str,
) -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host="alpha")
        await sessions.open("alpha", "qa-grade", visibility="hidden", **session_fields)
        frames: list[dict] = []

        async def broadcast(frame: dict) -> None:
            frames.append(frame)

        ledger = Ledger(store, sessions=sessions, broadcast=broadcast)
        report_id = f"qa-grade-{expected_field}-{expected_code}"
        try:
            with pytest.raises(VerbError) as raised:
                await ledger.ingest(_qa_grade_report(report_id, "alpha:qa-grade", **report_fields))
            assert raised.value.code == "schema_error"
            assert any(
                violation["field"] == expected_field and violation["code"] == expected_code
                for violation in raised.value.extra["schema_violations"]
            )
            assert await store.get_report(report_id) is None
            assert frames == []
        finally:
            store.stop()

    asyncio.run(go())


@pytest.mark.parametrize("session_fields", ({"role": "qa"}, {"phase": "qa"}))
def test_conforming_qa_grade_report_round_trips_full_target_sha(
    session_fields: dict[str, str],
) -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host="alpha")
        await sessions.open("alpha", "qa-grade", visibility="hidden", **session_fields)
        try:
            reply = await Ledger(store, sessions=sessions).report(_qa_grade_report(
                "qa-grade-valid", "alpha:qa-grade", qa_verdict="accept", target_sha=FULL_SHA,
            ))
            stored = await store.get_report("qa-grade-valid")
            assert reply["qa_verdict"] == "accept"
            assert reply["target_sha"] == FULL_SHA
            assert stored is not None and stored["target_sha"] == FULL_SHA
        finally:
            store.stop()

    asyncio.run(go())


@pytest.mark.parametrize("status", ("aborted", "error"))
def test_qa_aborted_or_error_report_needs_no_verdict_or_target(status: str) -> None:
    # Fleet audit 2026-09-26: a QA seat that stops without reviewing has no verdict to give.
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host="alpha")
        await sessions.open("alpha", "qa-stop", visibility="hidden", role="qa")
        try:
            reply = await Ledger(store, sessions=sessions).report(_qa_grade_report(
                f"qa-{status}", "alpha:qa-stop", status=status, reason="candidate withdrawn",
            ))
            stored = await store.get_report(f"qa-{status}")
            assert reply["report_id"] == f"qa-{status}"
            assert stored is not None and stored["status"] == status
            assert stored["qa_verdict"] is None and stored["target_sha"] is None
        finally:
            store.stop()

    asyncio.run(go())


def test_qa_progress_and_non_qa_terminal_reports_do_not_require_target_binding() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        sessions = Sessions(store, local_host="alpha")
        await sessions.open("alpha", "qa-progress", visibility="hidden", role="qa")
        await sessions.open("alpha", "worker", visibility="hidden", role="worker")
        ledger = Ledger(store, sessions=sessions)
        try:
            progress = _qa_grade_report("qa-progress", "alpha:qa-progress")
            progress.update({"status": "progress", "summary": "still reviewing (test report body)"})
            progress.pop("findings")
            progress.pop("next_action")
            assert (await ledger.report(progress))["report_id"] == "qa-progress"
            assert (await ledger.report(_qa_grade_report(
                "ordinary-terminal", "alpha:worker",
            )))["report_id"] == "ordinary-terminal"
        finally:
            store.stop()

    asyncio.run(go())


def test_report_stamps_daemon_observed_effective_tuple(tmp_path) -> None:
    async def go() -> None:
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        sessions = Sessions(store, local_host="hosta")
        await sessions.open(
            "hosta",
            "qa-seat",
            provider="codex",
            requested_model="model-b",
            requested_effort="high",
            effective_model="model-b",
            effective_effort="high",
            pane_status="pane_alive",
        )
        try:
            observer = RoutingIntegrity(store, sessions)
            row = await Ledger(store, sessions=sessions, routing_integrity=observer).ingest(
                {
                    "report_id": "tuple-stamped",
                    "from_stream_id": "hosta:qa-seat",
                    "status": "done",
                    "summary": "review complete (test report body)",
                    "findings": [], "no_findings": True,
                    "next_action": "accept",
                }
            )
            assert (row["effective_model"], row["effective_effort"]) == (
                "model-b",
                "high",
            )
            stored = await store.get_report("tuple-stamped")
            assert stored["effective_model"] == "model-b"
            assert stored["effective_effort"] == "high"
        finally:
            store.stop()

    asyncio.run(go())


def test_report_accepts_live_mismatch_even_when_swept_field_is_stale(tmp_path) -> None:
    async def go() -> None:
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        sessions = Sessions(store, local_host="hosta")
        await sessions.open(
            "hosta",
            "drifted-seat",
            provider="codex",
            requested_model="model-b",
            requested_effort="high",
            effective_model="gpt-5.6-luna",
            effective_effort="low",
            pane_status="pane_alive",
        )
        await store.update_session(
            "hosta",
            "drifted-seat",
            routing_integrity="verified",
            routing_integrity_updated_at="2026-08-09T00:00:00Z",
        )
        try:
            observer = RoutingIntegrity(store, sessions)
            report = await Ledger(store, sessions=sessions, routing_integrity=observer).ingest(
                {
                    "report_id": "accepted-stale-verified",
                    "from_stream_id": "hosta:drifted-seat",
                    "status": "done",
                    "summary": "live drift remains reportable",
                    "findings": [], "no_findings": True,
                    "next_action": "continue",
                    "qa_verdict": "accept",
                }
            )
            assert report["effective_model"] == "gpt-5.6-luna"
            assert report["effective_effort"] == "low"
            assert await store.get_report("accepted-stale-verified") is not None
        finally:
            store.stop()

    asyncio.run(go())



def test_report_snapshot_does_not_rewrite_legacy_periodic_trust_cache(tmp_path) -> None:
    async def go() -> None:
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        sessions = Sessions(store, local_host="hosta")
        await sessions.open(
            "hosta",
            "snapshot-seat",
            provider="codex",
            requested_model="model-b",
            requested_effort="high",
            effective_model="model-b",
            effective_effort="high",
            pane_status="pane_alive",
        )
        try:
            await store.update_session(
                "hosta",
                "snapshot-seat",
                routing_integrity="verified",
                routing_integrity_reason="legacy-cache",
                routing_integrity_updated_at="2026-08-20T00:00:00Z",
            )
            before = await store.fetch_session("hosta", "snapshot-seat")
            snapshot = await RoutingIntegrity(store, sessions).report_write_snapshot(
                "hosta:snapshot-seat"
            )
            after = await store.fetch_session("hosta", "snapshot-seat")
            assert snapshot is not None
            assert snapshot["routing_integrity"] == "verified"
            assert {
                field: after[field]
                for field in (
                    "routing_integrity",
                    "routing_integrity_reason",
                    "routing_integrity_updated_at",
                )
            } == {
                field: before[field]
                for field in (
                    "routing_integrity",
                    "routing_integrity_reason",
                    "routing_integrity_updated_at",
                )
            }
        finally:
            store.stop()

    asyncio.run(go())


def test_report_persists_one_atomic_provenance_snapshot(tmp_path) -> None:
    async def go() -> None:
        store, _sessions, ledger, _frames = await _provenance_state(tmp_path)
        try:
            row = await ledger.ingest(_provenance_report("provenance-control"))
            assert row["provenance_version"] == "v1"
            assert row["provenance_generation"] == "provenance-generation"
            assert row["provenance_qualified_spec_ids"] == [CANONICAL_PROVENANCE_SPEC]
            assert row["provenance_routing_integrity"] == "verified"
            assert row["provenance_requested_model"] == "gpt-5.6-luna"
            assert row["provenance_effective_effort"] == "max"
        finally:
            store.stop()

    asyncio.run(go())


@pytest.mark.parametrize("mutation", ("generation", "tuple", "qualified_specs"))
def test_report_provenance_interleaving_is_non_consuming(tmp_path, mutation: str) -> None:
    async def go() -> None:
        store, _sessions, ledger, frames = await _provenance_state(tmp_path)
        try:
            original_snapshot = ledger._report_routing_snapshot

            async def snapshot_then_mutate(stream_id: str):
                snapshot = await original_snapshot(stream_id)
                if mutation == "generation":
                    await store.open_session(
                        "alpha", "worker", session_generation="replacement-generation",
                    )
                elif mutation == "tuple":
                    await store.update_session("alpha", "worker", effective_effort="low")
                else:
                    await store.update_session(
                        "alpha",
                        "worker",
                        spec_id="other__spec",
                        spec_ids=["other__spec"],
                        spec_binding_provenance=_spec_binding("other__spec"),
                    )
                return snapshot

            ledger._report_routing_snapshot = snapshot_then_mutate
            await store.register_awaiter("alpha:worker", 7)
            report_id = f"provenance-{mutation}"
            with pytest.raises(VerbError) as raised:
                await ledger.ingest(_provenance_report(report_id))
            assert raised.value.code == "report_provenance_changed"
            assert raised.value.extra["cycle_consuming"] is False
            assert await store.get_report(report_id) is None
            awaiter = await store.submit(
                lambda connection: connection.execute(
                    "SELECT outcome FROM v2_awaiters WHERE stream_id=? AND target_msg_id=?",
                    ("alpha:worker", 7),
                ).fetchone()
            )
            assert awaiter["outcome"] == "pending"
            assert frames == []
        finally:
            store.stop()

    asyncio.run(go())


def test_report_requires_a_complete_provenance_snapshot(tmp_path) -> None:
    class NoSnapshot:
        async def report_write_snapshot(self, _stream_id: str, **_kwargs):
            return None

    async def go() -> None:
        store, sessions, _ledger, frames = await _provenance_state(tmp_path)
        try:
            ledger = Ledger(store, sessions=sessions, routing_integrity=NoSnapshot(), broadcast=lambda frame: frames.append(frame))
            await store.register_awaiter("alpha:worker", 7)
            with pytest.raises(VerbError) as raised:
                await ledger.ingest(_provenance_report("provenance-unavailable"))
            assert raised.value.code == "report_provenance_unavailable"
            assert raised.value.extra["cycle_consuming"] is False
            assert await store.get_report("provenance-unavailable") is None
            assert (await store.get_awaiter_result("alpha:worker", 7))["outcome"] == "pending"
            assert frames == []
        finally:
            store.stop()

    asyncio.run(go())


# spec_pentacle__report_placeholder_reject_2026_11: the 2026-11-15 incident body.
INCIDENT_BODY = {"summary": "x", "findings": [], "next_action": "y"}
REAL_BODY = {"summary": "Fixed the report validator and shipped it.", "findings": [], "next_action": "none"}


async def _worker_ledger(store: Store) -> Ledger:
    sessions = Sessions(store, local_host="alpha")
    await sessions.open("alpha", "worker", visibility="hidden", role="worker")
    return Ledger(store, sessions=sessions)


def _report(report_id: str, body: dict) -> dict:
    return {"report_id": report_id, "from_stream_id": "alpha:worker", "msg_id": 0, "status": "done", **body}


async def _store_as_before_deploy(ledger: Ledger, report: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    import ledger as ledger_module

    original = ledger_module.validate_report_payload
    with monkeypatch.context() as patch:
        patch.setattr(
            ledger_module, "validate_report_payload",
            lambda *a, **kw: original(*a, **{**kw, "reject_placeholders": False}),
        )
        await ledger.ingest(report)


def test_ingest_rejects_placeholder_done_report_and_seat_stays_open() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            ledger = await _worker_ledger(store)
            with pytest.raises(VerbError) as raised:
                await ledger.ingest(_report("placeholder", INCIDENT_BODY))
            codes = {v["field"]: v["code"] for v in raised.value.extra["schema_violations"]}
            assert codes == {
                "summary": "placeholder_summary",
                "next_action": "placeholder_next_action",
                "findings": "findings_unmarked_empty",
            }
            assert await store.get_report("placeholder") is None
            assert (await store.fetch_session("alpha", "worker"))["status"] == "open"
            await ledger.ingest(_report("real", {**REAL_BODY, "no_findings": True}))
            final = await store.find_report("alpha:worker", statuses=("done",))
            assert final["report_id"] == "real"
        finally:
            store.stop()

    asyncio.run(go())


def test_ingest_accepts_no_findings_marker() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            ledger = await _worker_ledger(store)
            await ledger.ingest(_report("marked", {**REAL_BODY, "no_findings": True}))
            row = await store.get_report("marked")
            assert row["findings"] in ([], "[]")
        finally:
            store.stop()

    asyncio.run(go())


def test_ingest_replays_predeploy_placeholder_row_same_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            ledger = await _worker_ledger(store)
            await _store_as_before_deploy(ledger, _report("old", INCIDENT_BODY), monkeypatch)
            stored = await store.get_report("old")
            await ledger.ingest(_report("old", INCIDENT_BODY))
            assert await store.get_report("old") == stored
            with pytest.raises(VerbError) as raised:
                await ledger.ingest(_report("old", {**INCIDENT_BODY, "next_action": "lead_merge"}))
            assert raised.value.code == "report_id_replay_conflict"
            with pytest.raises(VerbError) as raised:
                await ledger.ingest(_report("new", INCIDENT_BODY))
            assert raised.value.code == "schema_error"
            assert await store.get_report("new") is None
        finally:
            store.stop()

    asyncio.run(go())


def test_no_findings_marker_keeps_request_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            ledger = await _worker_ledger(store)
            await _store_as_before_deploy(ledger, _report("legacy", REAL_BODY), monkeypatch)
            stored = await store.get_report("legacy")
            await ledger.ingest(_report("legacy", {**REAL_BODY, "no_findings": True}))
            assert await store.get_report("legacy") == stored
        finally:
            store.stop()

    asyncio.run(go())


def test_advisor_progress_with_unstamped_tell_cannot_attest(tmp_path):
    from test_advisor_rule_forward import rule_fixture, seat, rule_report, SEAT_A, SEAT_B, WORDS, TELL_ID

    async def run():
        async with rule_fixture(tmp_path) as (h, _):
            async with seat(h, SEAT_A) as fd, seat(h, SEAT_B) as advisor:
                ordinary = await fd("tell", to_stream_id=SEAT_B, text=WORDS, tell_id=TELL_ID)
                assert ordinary["delivery_status"] == "delivered"
                report = await advisor("report", **rule_report())
                assert report.get("error_code") == "advisor_rule_event_unverified"
                assert await h.store.get_report("fixture-rule-report") is None
    asyncio.run(run())


def test_advisor_accept_reports_validate_strict_shape_before_any_write(tmp_path):
    from test_advisor_rule_forward import rule_fixture, rule_report, seat, SEAT_B

    invalid_reviews = [
        {"version": True}, {"version": 2}, {"verdict": "reject"},
        {"operator_message_id": ""}, {"operator_message_id": "wrong\ninput"},
        {"fd_event_id": 12}, {"fd_event_id": "x" * 201}, {"path": "docs/reference/alternate.md"},
        {"base_revision": "A" * 64}, {"new_bytes_sha256": "b" * 63},
        {"scope": {"mode": "other", "removed": [], "added": []}},
        {"scope": {"mode": "single", "removed": [], "added": []}},
        {"scope": {"mode": "single", "removed": [], "added": ["c" * 64] * 2}},
        {"scope": {"mode": "batch", "removed": ["b" * 64, "a" * 64], "added": []}},
        {"scope": {"mode": "batch", "removed": ["z" * 64], "added": []}},
        {"scope": {"mode": "reorganize", "removed": [], "added": [], "other": "forged"}},
        {"fd_binding_revision": 0}, {"operator_text_sha256": "a" * 64},
    ]

    async def run():
        async with rule_fixture(tmp_path) as (h, _), seat(h, SEAT_B) as advisor:
            frames = [rule_report(**review) for review in invalid_reviews]
            for key in rule_report()["extras"]["advisor_rule"]:
                missing = rule_report()
                missing["extras"]["advisor_rule"].pop(key)
                frames.append(missing)
            frames += [{**rule_report(), **claims} for claims in (
                {"status": "done"}, {"msg_id": False}, {"msg_id": 6}, {"report_id": None},
                {"terminate": True}, {"close_on_ingest": True}, {"operator_confirm": True},
                {"discharges": ["one"]}, {"qa_verdict": "accept"}, {"completion_kind": "implementation_ready"},
                {"advisor_rule_accept": {}}, {"session_generation": "forged"},
                {"details": {"nested": [{"fd_generation": "forged"}]}},
                {"agent_orch_attestation": {"envelope": {"advisor_rule_accept": {}}}},
                {"details": {"nested": [{"completion_kind": "implementation_ready"}]}},
                {"extras": {"advisor_rule_accept": {}}}, {"extras": {"advisor_rule": None}},
            )]
            for fields in frames:
                refused = await advisor("report", **fields)
                assert refused.get("error_code") == "advisor_rule_invalid", refused.get("error_code")
            assert await h.store.get_report("fixture-rule-report") is None
            side_effects = await h.store.submit(lambda conn: tuple(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
                    "v2_report_rejections", "v2_outbound_notices", "v2_assistant_composite_terminal_reports",
                )))
            assert side_effects == (0, 0, 0)

    asyncio.run(run())


@pytest.mark.parametrize("commit_first", [False, True])
def test_live_retained_advisor_rebind_serializes_with_accept_insert(tmp_path, monkeypatch, commit_first):
    from test_advisor_rule_forward import (
        admit, forward, operator, rule_fixture, rule_report, seat, SEAT_A, SEAT_B, SENDER,
    )

    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            assert (await admit(socket))["type"] == "send.result"
            async with seat(h, SEAT_A) as fd, seat(h, SEAT_B) as advisor, seat(h, SENDER) as successor:
                assert (await forward(fd))["delivery_status"] == "delivered"
                report = rule_report()
                entered, release = asyncio.Event(), asyncio.Event()
                submit = h.store._submit_spec_commit

                async def at_insert(callback, *, cancel_owner=None):
                    if commit_first:
                        result = await submit(callback, cancel_owner=cancel_owner)
                    entered.set()
                    await release.wait()
                    return result if commit_first else await submit(callback, cancel_owner=cancel_owner)

                monkeypatch.setattr(h.store, "_submit_spec_commit", at_insert)
                task = asyncio.create_task(advisor("report", **report))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    brief = tmp_path / "advisor-brief.md"
                    brief.write_text("Synthetic advisor transition fixture.")
                    changed = await fd("assistant.authority", action="set", value=SENDER, brief_file=str(brief))
                    assert changed["type"] == "assistant.authority.ok", changed.get("error_code")
                finally:
                    release.set()
                result = await task
                if commit_first:
                    assert result["type"] == "report.ok"
                    replay = await advisor("report", **report)
                    assert replay["ledger_row_id"] == result["ledger_row_id"]
                    conflict = await advisor("report", **rule_report(new_bytes_sha256="d" * 64))
                    assert conflict.get("error_code") == "report_id_replay_conflict"
                else:
                    assert result.get("error_code") == "advisor_rule_advisor_unverified"
                    assert await h.store.get_report(report["report_id"]) is None
                wrong_recipient = await successor("report", **rule_report("fixture-wrong-recipient"))
                assert wrong_recipient.get("error_code") == "advisor_rule_advisor_unverified"
                assert await h.store.get_report("fixture-wrong-recipient") is None
                fresh_tell = "fixture-successor-dispatch"
                assert (await forward(fd, tell_id=fresh_tell, target=SENDER))["delivery_status"] == "delivered"
                fresh_report = await successor("report", **rule_report("fixture-successor", fd_event_id=fresh_tell))
                assert fresh_report["type"] == "report.ok"

    asyncio.run(run())


def test_old_fd_evidence_can_attest_and_advances_no_terminal_state(tmp_path):
    from test_advisor_rule_forward import (
        admit, forward, operator, rule_fixture, rule_report, seat, SEAT_A, SEAT_B, SENDER,
    )

    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            assert (await admit(socket))["type"] == "send.result"
            host, name = SENDER.split(":")
            await h.store.update_session(host, name, phase="qa")
            await h.sessions.refresh()
            async with seat(h, SEAT_A) as fd, seat(h, SEAT_B) as advisor, seat(h, SENDER) as unrelated:
                assert (await forward(fd))["delivery_status"] == "delivered"
                for rpc in (fd, unrelated):
                    refused = await rpc("report", **rule_report())
                    assert refused.get("error_code") == "advisor_rule_advisor_unverified"
                for wrong_pair in ({"operator_message_id": "fixture-wrong-input"}, {"fd_event_id": "fixture-dispatch"}):
                    refused = await advisor("report", **rule_report(**wrong_pair))
                    assert refused.get("error_code") == "advisor_rule_event_unverified"
                await h.rotate_fd()
                accepted = await advisor("report", **rule_report())
                assert accepted["type"] == "report.ok"
                stored = await h.store.get_report("fixture-rule-report", include_advisor_rule=True)
                assert stored["advisor_rule_accept"]["fd_stream_id"] == SEAT_A
                host, name = SEAT_B.split(":")
                assert (await h.store.fetch_session(host, name))["status"] == "open"
                terminal, notices, latest = await h.store.submit(lambda conn: tuple(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
                        "v2_assistant_composite_terminal_reports", "v2_outbound_notices", "v2_work_item_latest_report",
                    )))
                assert (terminal, notices, latest) == (0, 0, 0)

    asyncio.run(run())


def test_advisor_accept_checks_delivery_proof_and_exact_recipient_generation(tmp_path, monkeypatch):
    import comms
    from test_advisor_rule_forward import (
        admit, forward, operator, record_native, rule_fixture, rule_report, seat, SEAT_A, SEAT_B, TELL_ID,
    )

    monkeypatch.setattr(comms, "PROOF_FAST_WAIT_S", 0.04)

    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            assert (await admit(socket))["type"] == "send.result"

            async def paste_normalized(name, wire, **_options):
                h.tmux.inputs.append((name, wire))
                # Normalized provider proof alone is insufficient. Persist
                # a genuine event with different raw bytes for the first paste.
                await record_native(h, name, wire.rstrip())

            monkeypatch.setattr(h.tmux, "paste", paste_normalized)
            async with seat(h, SEAT_A) as fd, seat(h, SEAT_B) as advisor:
                assert (await forward(fd))["delivery_status"] == "committed_pending_proof"
                assert (await advisor("report", **rule_report())).get("error_code") == "advisor_rule_event_unverified"
                await record_native(h, *h.tmux.inputs[-1])
                assert (await forward(fd))["submission_confirmed"] is True
                accepted = await advisor("report", **rule_report())
                assert accepted["type"] == "report.ok"

                host, name = SEAT_B.split(":")
                await h.store.update_session(host, name, pane_status="pane_dead")
                assert (await advisor("report", **rule_report("not-ready"))).get("error_code") == "advisor_rule_advisor_unverified"
                await h.store.update_session(host, name, pane_status="pane_alive")

                def new_generation(conn):
                    conn.execute("UPDATE v2_session_generations SET generation=? WHERE host=? AND session_name=?",
                                 ("new-fixture-generation", host, name))
                    conn.commit()

                await h.store.submit(new_generation)
                replay = await advisor("report", **rule_report())
                assert replay.get("error_code") == "advisor_rule_advisor_unverified"
                wrong_generation = await advisor("report", **rule_report("wrong-generation"))
                assert wrong_generation.get("error_code") == "advisor_rule_advisor_unverified"
                assert (await h.store.get_tell_delivery(TELL_ID))["advisor_rule_intent"]["advisor_generation"] == h.gen[SEAT_B]
                assert await h.store.get_report("wrong-generation") is None

    asyncio.run(run())


def test_advisor_fenced_after_connection_auth_is_refused_at_insert(tmp_path, monkeypatch):
    from test_advisor_rule_forward import admit, forward, operator, rule_fixture, rule_report, seat, SEAT_A, SEAT_B

    async def run():
        async with rule_fixture(tmp_path) as (h, envelope), operator(h, envelope) as socket:
            assert (await admit(socket))["type"] == "send.result"
            async with seat(h, SEAT_A) as fd, seat(h, SEAT_B) as advisor:
                assert (await forward(fd))["delivery_status"] == "delivered"
                submit = h.store._submit_spec_commit

                def retire(conn):
                    conn.execute("INSERT INTO v2_host_transfer_retirements VALUES (?,?,?,?)",
                                 (SEAT_B, h.gen[SEAT_B], "fixture-retire", 1))
                    conn.commit()

                async def fence_at_commit(callback, *, cancel_owner=None):
                    await h.store.submit(retire)
                    return await submit(callback, cancel_owner=cancel_owner)

                monkeypatch.setattr(h.store, "_submit_spec_commit", fence_at_commit)
                result = await advisor("report", **rule_report())
                assert result.get("error_code") == "advisor_rule_advisor_unverified"
                assert await h.store.get_report("fixture-rule-report") is None

    asyncio.run(run())
