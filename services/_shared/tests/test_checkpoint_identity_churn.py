"""Checkpoint body publication must not weaken board identity guards."""
import json
from pathlib import Path

import pytest

from _shared import specs_service
from _shared.spec_reader import SpecReader
from _shared.work_checkpoint import work_checkpoint


TARGET_ID = "spec_fixture__target"
SPEC = """---
id: spec_fixture__target
---
## Acceptance Criteria
- [ ] Finish.
## Estimate
- remaining_work_h: unknown — fixture
"""
SUMMARY = "**Status** — Ready.\n**Next action** — Finish.\n"
OTHER = "---\nid: spec_fixture__other\n---\nOther body.\n"


@pytest.fixture
def board(tmp_path):
    bucket = tmp_path / "work/in_progress"
    other = bucket / "fixture__aaa/spec.md"
    target = bucket / "fixture__zzz/spec.md"
    for path, content in ((other, OTHER), (target, SPEC)):
        path.parent.mkdir(parents=True)
        path.write_text(content)
        path.with_name("summary.md").write_text(SUMMARY)
    return tmp_path, target, other


def during_scan(monkeypatch, target, mutate):
    """Mutate exactly once after the early file is captured, then assert it ran."""
    original_parse = specs_service._parse_identity_bytes
    original_iterdir = Path.iterdir
    applied = []

    def ordered(path):
        children = original_iterdir(path)
        return iter(sorted(children)) if path == target.parent.parent else children

    def parsed(path, content):
        front = original_parse(path, content)
        if path == target and not applied:
            applied.append(True)
            mutate()
        return front

    monkeypatch.setattr(Path, "iterdir", ordered)
    monkeypatch.setattr(specs_service, "_parse_identity_bytes", parsed)
    return applied


@pytest.mark.parametrize("mode", ["body", "atomic", "summary_control"])
def test_unrelated_body_publication_keeps_checkpoint_valid(board, monkeypatch, mode):
    root, target, other = board
    before = target.read_bytes()

    def publish():
        if mode == "atomic":
            replacement = other.with_suffix(".new")
            replacement.write_text(OTHER + "Published.\n")
            replacement.replace(other)
        elif mode == "body":
            other.write_text(OTHER + "Published.\n")
        else:
            other.with_name("summary.md").write_text(SUMMARY + "Published.\n")

    applied = during_scan(monkeypatch, target, publish)
    result = work_checkpoint(TARGET_ID, root=root)
    assert applied == [True]
    assert target.read_bytes() == before
    assert result["state"] == "valid", result
    assert result["facts"]["ac_total"] == 1


@pytest.mark.parametrize("consumer", ["reader", "wip"])
def test_default_reader_and_wip_still_seal_unrelated_bytes(board, monkeypatch, consumer):
    from _shared.wip_cap import check_lead_spawn

    root, target, other = board
    (root / "work/wip_policy.json").write_text(json.dumps({
        "schema_version": 1, "cap": 3, "snapshot_at": "2026-10-26", "grandfather": {},
    }))
    applied = during_scan(monkeypatch, target, lambda: other.write_text(OTHER + "Published.\n"))
    if consumer == "reader":
        with pytest.raises(ValueError, match="spec_identity_changed"):
            SpecReader(root)
    else:
        assert check_lead_spawn(root, TARGET_ID)["error_code"] == "wip_spec_unreadable"
    assert applied == [True]


@pytest.mark.parametrize("change", [
    "target_body", "target_replace", "target_id", "other_duplicate_id", "other_alias",
    "other_missing", "other_malformed", "other_symlink", "add_duplicate", "rename", "status_move", "statuses",
])
def test_checkpoint_rejects_identity_target_and_membership_changes(board, monkeypatch, change):
    root, target, other = board

    def mutate():
        if change == "target_body":
            target.write_text(SPEC + "Changed.\n")
        elif change == "target_replace":
            replacement = target.with_suffix(".new")
            replacement.write_text(SPEC)
            replacement.replace(target)
        elif change == "target_id":
            target.write_text(SPEC.replace(TARGET_ID, "spec_fixture__new"))
        elif change in ("other_duplicate_id", "other_alias"):
            identity = TARGET_ID if change == "other_duplicate_id" else "fixture__target"
            other.write_text(OTHER.replace("spec_fixture__other", identity))
        elif change == "other_missing":
            other.unlink()
        elif change == "other_malformed":
            other.write_text("---\nid: [\n---\n")
        elif change == "other_symlink":
            other.unlink()
            other.symlink_to(target)
        elif change == "add_duplicate":
            duplicate = other.parent.with_name("fixture__duplicate")
            duplicate.mkdir()
            (duplicate / "spec.md").write_text(SPEC)
        elif change == "rename":
            other.parent.rename(other.parent.with_name("fixture__renamed"))
        elif change == "status_move":
            destination = root / "work/completed"
            destination.mkdir()
            other.parent.rename(destination / other.parent.name)
        else:
            (root / "work/statuses.json").write_text(json.dumps({"statuses": []}))

    applied = during_scan(monkeypatch, target, mutate)
    result = work_checkpoint(TARGET_ID, root=root)
    assert applied == [True]
    assert result["state"] == "unverifiable", result
    assert result["facts"] is None


@pytest.mark.parametrize("identity", [TARGET_ID, "fixture__target"])
def test_preexisting_duplicate_or_declared_alias_is_unverifiable(board, identity):
    root, _target, other = board
    other.write_text(OTHER.replace("spec_fixture__other", identity))
    assert work_checkpoint(TARGET_ID, root=root)["state"] == "unverifiable"


@pytest.mark.parametrize("changed_document", ["spec", "summary"])
@pytest.mark.parametrize("same_metadata", [False, True])
def test_target_pair_change_during_summary_capture_is_unverifiable(board, monkeypatch, changed_document, same_metadata):
    from .test_spec_identity_snapshot import alias_file_timestamps

    root, target, _other = board
    summary = target.with_name("summary.md")
    changed = target if changed_document == "spec" else summary
    if same_metadata:
        alias_file_timestamps(changed, monkeypatch)
    original = specs_service.SpecsSubsystem._read_identity_input
    original_text = Path.read_text
    applied = []

    def publish():
        applied.append(True)
        # Equal-length content plus aliased timestamps leaves the digest as
        # the decisive guard in the same-metadata cells.
        changed.write_bytes(changed.read_bytes().replace(b"Finish.", b"Change."))

    def read(service, path, **kwargs):
        captured = original(service, path, **kwargs)
        if path == summary and kwargs.get("directory_fd") is None and not applied:
            publish()
        return captured

    def read_text(path, *args, **kwargs):
        text = original_text(path, *args, **kwargs)
        if path == summary and not applied:
            publish()
        return text

    monkeypatch.setattr(specs_service.SpecsSubsystem, "_read_identity_input", read)
    monkeypatch.setattr(Path, "read_text", read_text)
    result = work_checkpoint(TARGET_ID, root=root)
    assert applied == [True]
    assert result["state"] == "unverifiable", result


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "crlf", "cr"])
def test_summary_source_contract(board, kind):
    root, target, _other = board
    summary = target.with_name("summary.md")
    summary.unlink()
    if kind == "symlink":
        outside = root / "outside.md"
        outside.write_text(SUMMARY)
        summary.symlink_to(outside)
    elif kind == "directory":
        summary.mkdir()
    elif kind in ("crlf", "cr"):
        summary.write_bytes(SUMMARY.replace("\n", "\r\n" if kind == "crlf" else "\r").encode())
    result = work_checkpoint(TARGET_ID, root=root)
    assert result["state"] == ("valid" if kind in ("crlf", "cr") else "unverifiable")
    if kind == "missing":
        assert result["error"] == "FileNotFoundError"
