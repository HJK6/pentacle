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
        "summary": "provenance fixture",
        "findings": [],
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
        "summary": "claim fixture",
        "findings": [],
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
                    "summary": "misfiled verdict",
                    "findings": [],
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
                "summary": "typed readiness",
                "findings": [],
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
                    "summary": "typed readiness",
                    "findings": [],
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
                "summary": "typed verdict",
                "findings": [],
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
        "summary": "typed QA verdict",
        "findings": [],
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
            progress.update({"status": "progress", "summary": "still reviewing"})
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
                    "summary": "review complete",
                    "findings": [],
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
                    "findings": [],
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
