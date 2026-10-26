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
LABELLED = "---\nid: work_demo__bridge\n---\n**Status** — Cut.\n\n**Next action** — Join the deck.\n"


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
async def test_owner_done_with_malformed_source_is_refused_then_accepted_after_fix(memory):
    store, ledger = await ledger_with({"role": "lead", "assigned_spec_id": "spec_demo__bridge"})
    try:
        with pytest.raises(VerbError) as exc:
            await ledger.report(report("done", "r-1"))
        assert exc.value.code == "work_checkpoint_invalid"
        assert "**Status** —" in str(exc.value) and "as_of 2026-10-15, provisional" in str(exc.value)
        assert await store.get_report("r-1") is None
        (memory / "spec.md").write_text(FIXED_SPEC)
        (memory / "summary.md").write_text(LABELLED)
        reply = await ledger.report(report("done", "r-1"))
        assert reply["work_checkpoint"]["state"] == "valid"
        assert reply["work_checkpoint"]["facts"]["next_action_text"] == "Join the deck."
        assert (await store.get_report("r-1"))["extras"]["work_checkpoint"]["state"] == "valid"
        # A later spec regression never refuses the replay of a stored report.
        (memory / "summary.md").write_text(PROSE)
        assert (await ledger.report(report("done", "r-1")))["report_id"] == "r-1"
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
                                    {"role": "nexus", "assigned_spec_id": "spec_demo__bridge"},
                                    {"role": "lead"}])  # single-spec fallback only, nothing persisted
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
