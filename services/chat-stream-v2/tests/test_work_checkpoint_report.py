"""Item (2): the spec owner's report cannot claim a checkpoint its source fails (B2, R7)."""
from __future__ import annotations

import pytest

from ledger import Ledger
from sessions import Sessions, VerbError
from store import Store

HOST = "fixture"
SPEC = ("---\nid: spec_demo__bridge\ntitle: Bridge\n---\n## Acceptance Criteria\n- [x] Cut.\n- [ ] Join.\n\n"
        "## Estimate\n- remaining_work_h: 2–5 (median 3) as_of 2026-10-15, provisional\n")
PROSE = "---\nid: work_demo__bridge\n---\nCut and waiting on glue; join next.\n"
FIXED_SPEC = SPEC.replace("as_of 2026-10-15, provisional", "as_of 2026-10-15 provisional")
LABELLED = "---\nid: work_demo__bridge\n---\n**Progress** — Cut the supports.\n**Status** — Cut.\n\n**Next action** — Join the deck.\n"


@pytest.fixture
def memory(tmp_path, monkeypatch):
    folder = tmp_path / "work" / "in_progress" / "demo__bridge"
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text(SPEC)
    (folder / "summary.md").write_text(PROSE)
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path))
    return folder


async def ledger_with(owner_fields):
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, local_host=HOST)
    await store.open_session(HOST, "owner", visibility="hidden", spec_ids=["spec_demo__bridge"], **owner_fields)
    await sessions.refresh()
    return store, Ledger(store, sessions=sessions)


def report(status, report_id):
    return {"report_id": report_id, "from_stream_id": f"{HOST}:owner", "msg_id": 0, "status": status,
            "summary": "checkpoint", "findings": [], "next_action": "continue"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["done", "progress"])
async def test_owner_report_with_malformed_source_is_durable_with_warning(memory, status):
    store, ledger = await ledger_with({"role": "lead", "assigned_spec_id": "spec_demo__bridge"})
    try:
        reply = await ledger.report(report(status, "r-1"))
        assert reply["work_checkpoint"]["state"] == "invalid"
        assert (await store.get_report("r-1"))["extras"]["work_checkpoint"]["state"] == "invalid"
        assert (await ledger.report(report(status, "r-1")))["report_id"] == "r-1"
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_new_readiness_requires_current_checkpoint_and_verified_qa_then_replays(memory, monkeypatch):
    from test_qa_attestation_validation import _state, _report, SPEC_ID, READY, QA
    monkeypatch.setenv("PENTACLE_QA_ATTESTATION_MODE", "enforce")
    other = memory.parent / "pentacle__daemon_v2_qa_attestation_validation_control_plane_2026_08"
    other.mkdir()
    spec = FIXED_SPEC.replace("spec_demo__bridge", SPEC_ID)
    other.joinpath("spec.md").write_text(spec)
    summary = other / "summary.md"
    summary.write_text(PROSE)
    store, sessions, _, ledger, _ = await _state()
    try:
        row = await store.update_session("alpha", "ready", role="lead", assigned_spec_id=SPEC_ID)
        sessions.apply_durable(READY, role="lead", assigned_spec_id=SPEC_ID)
        await ledger.report(_report("qa-accepted-checkpoint", QA, qa_verdict="accept"))
        ready = _report("ready-checkpoint", READY, completion_kind="implementation_ready",
                        attestation={"stream_id": QA, "report_id": "qa-accepted-checkpoint"})
        with pytest.raises(VerbError) as exc:
            await ledger.report(ready)
        assert exc.value.code == "work_checkpoint_invalid" and await store.get_report("ready-checkpoint") is None
        summary.write_text(LABELLED)
        with pytest.raises(VerbError) as exc:
            await ledger.report({**ready, "qa_attestation": {"stream_id": QA, "report_id": "forged"}})
        assert exc.value.code == "qa_attestation_unverified"
        reply = await ledger.report(ready)
        assert reply["work_checkpoint"]["state"] == "valid"
        assert reply["qa_attestation_validation"]["state"] == "verified"
        snapshot = reply["work_checkpoint"]
        assert snapshot["source_revisions"]["summary.md"] and snapshot["policy_revision"]
        summary.unlink()  # owned fixture only; accepted replay never rereads it
        replay = await ledger.report(ready)
        assert replay["work_checkpoint"] == snapshot
        with pytest.raises(VerbError):
            await ledger.report({**ready, "summary": "changed payload"})
        error = _report("progress-still-allowed", READY, status="error")
        error.update(reason="Source unavailable", findings=[], next_action="Repair the source")
        assert (await ledger.report(error))["work_checkpoint"]["state"] == "unverifiable"
    finally:
        store.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["error", "aborted"])
async def test_failure_reports_are_never_refused_and_carry_the_deficiencies(memory, status):
    store, ledger = await ledger_with({"role": "lead", "assigned_spec_id": "spec_demo__bridge"})
    try:
        payload = report(status, "r-" + status)
        payload["reason"] = "blocked upstream"
        reply = await ledger.report(payload)
        assert reply["work_checkpoint"]["state"] == "invalid"
    finally:
        store.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{"role": "worker", "assigned_spec_id": "spec_demo__bridge"},
                                    {"role": "nexus", "assigned_spec_id": "spec_demo__bridge"}])  # single-spec fallback only, nothing persisted
async def test_non_owner_reports_are_unchanged(memory, fields):
    store, ledger = await ledger_with(fields)
    try:
        reply = await ledger.report(report("done", "r-x"))
        assert "work_checkpoint" not in reply
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_unreadable_memory_root_is_reported_not_refused(memory, monkeypatch, tmp_path):
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path / "absent"))
    store, ledger = await ledger_with({"role": "lead", "assigned_spec_id": "spec_demo__bridge"})
    try:
        reply = await ledger.report(report("done", "r-u"))
        assert reply["work_checkpoint"]["state"] == "unverifiable"
    finally:
        store.stop()


@pytest.mark.asyncio
async def test_unrelated_atomic_publication_keeps_owner_report_checkpoint_valid(memory, monkeypatch):
    from pathlib import Path
    from _shared import specs_service

    target = memory / "spec.md"
    target.write_text(FIXED_SPEC)
    (memory / "summary.md").write_text(LABELLED)
    unrelated = memory.with_name("demo__aaa") / "spec.md"
    unrelated.parent.mkdir()
    unrelated.write_text("---\nid: spec_demo__other\n---\nBefore.\n")
    original_parse, original_iterdir = specs_service._parse_identity_bytes, Path.iterdir
    applied = []

    def ordered(path):
        children = original_iterdir(path)
        return iter(sorted(children)) if path == memory.parent else children

    def parsed(path, content):
        front = original_parse(path, content)
        if path == target and not applied:
            applied.append(True)
            replacement = unrelated.with_suffix(".new")
            replacement.write_text("---\nid: spec_demo__other\n---\nAfter.\n")
            replacement.replace(unrelated)
        return front

    monkeypatch.setattr(Path, "iterdir", ordered)
    monkeypatch.setattr(specs_service, "_parse_identity_bytes", parsed)
    store, ledger = await ledger_with({"role": "lead", "assigned_spec_id": "spec_demo__bridge"})
    try:
        reply = await ledger.report(report("done", "r-unrelated-publish"))
        assert applied == [True]
        assert reply["work_checkpoint"]["state"] == "valid"
        stored = await store.get_report("r-unrelated-publish")
        assert stored["extras"]["work_checkpoint"]["state"] == "valid"
        assert stored["extras"]["work_checkpoint"]["facts"]["next_action_text"] == "Join the deck."
    finally:
        store.stop()
