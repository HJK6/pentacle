"""Synthetic discovery, settled quality, and monotonic material revisions."""
import os
from pathlib import Path

from _shared.specs_service import SpecsSubsystem
from _shared.work_observations import advance_observation

ID = "spec_demo__bridge"


def write_item(root, status="in_progress", slug="demo__bridge", checked=False):
    folder = root / "work" / status / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "spec.md").write_text(
        f"---\nid: {ID}\ntitle: Bridge\n---\n## Acceptance Criteria\n- [{'x' if checked else ' '}] Assemble.\n")
    (folder / "summary.md").write_text("**Next action** — Test the span.\n")
    return folder


def subsystem(root, monkeypatch):
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(root))
    return SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None, debounce_s=0)


def test_declared_identity_after_move_and_duplicate(tmp_path, monkeypatch):
    folder = write_item(tmp_path)
    service = subsystem(tmp_path, monkeypatch)
    assert service.scan_work_observations()["candidates"][ID][0]["facts"]["status"] == "in_progress"
    target = tmp_path / "work" / "completed" / "renamed_folder"
    target.parent.mkdir()
    folder.rename(target)
    result = service.scan_work_observations()
    assert result["candidates"][ID][0]["facts"]["status"] == "completed"
    write_item(tmp_path, slug="other")
    assert len(service.scan_work_observations()["candidates"][ID]) == 2


def test_artifacts_and_symlinks_are_never_read(tmp_path, monkeypatch):
    folder = write_item(tmp_path)
    write_item(tmp_path, status="_artifacts", slug="trap")
    write_item(folder, status="completed", slug="nested")
    artifact = folder / "_artifacts"
    artifact.mkdir()
    (artifact / "spec.md").write_text("broken secret-looking placeholder")
    (tmp_path / "work" / "in_progress" / "link").symlink_to(folder, target_is_directory=True)
    original = Path.read_text
    def guarded(path, *args, **kwargs):
        assert "_artifacts" not in path.parts
        assert "nested" not in path.parts
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded)
    result = subsystem(tmp_path, monkeypatch).scan_work_observations()
    assert len(result["candidates"][ID]) == 1


def test_root_absent_restored_and_error_classification(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    service = subsystem(root, monkeypatch)
    assert service.scan_work_observations()["available"] is False
    folder = write_item(root)
    assert service.scan_work_observations()["available"] is True
    (folder / "spec.md").write_text("---\nid: [broken\n---\n")
    result = service.scan_work_observations()
    assert result["available"] is True
    assert result["errors"][str(folder)]["quality"] == "error"


def test_split_write_and_conflict_keep_quality_stale(tmp_path, monkeypatch):
    folder = write_item(tmp_path)
    service = subsystem(tmp_path, monkeypatch)
    service.debounce_s = 5
    assert service.scan_work_observations()["candidates"][ID][0]["quality"] == "stale"
    service.debounce_s = 0
    (folder / "spec.sync-conflict-fixture.md").write_text("do not read")
    assert service.scan_work_observations()["candidates"][ID][0]["quality"] == "stale"


def candidate(checked=0, status="completed"):
    return {"quality": "fresh", "path": "work/completed/demo__bridge", "source_hash": str((checked, status)),
            "facts": {"title": "Bridge", "status": status, "terminal": status if status == "completed" else None,
                      "ac_checked": checked, "ac_total": 1, "estimate": None,
                      "status_text": "Ready", "next_action_text": "Review"}}


def advance(prior, value, now, sweep=True):
    return advance_observation(ID, prior, value, now=now, settle_s=300, sweep=sweep)


def test_material_a_b_a_b_revision_and_no_stale_events():
    record, change = advance(None, candidate(), 1000)
    assert record["member"]["obs_rev"] == 1 and change is None
    for rev, count in ((2, 1), (3, 0), (4, 1)):
        record, change = advance(record, candidate(count), 1000 + rev)
        assert change["obs_rev"] == rev
        replay, event = advance(record, candidate(count), 1100 + rev)
        assert replay["member"]["obs_rev"] == rev and event is None
        assert replay["member"]["source_changed_at"] == record["member"]["source_changed_at"]
        stale, event = advance(record, {"quality": "stale"}, 1200 + rev)
        assert stale["member"]["obs_rev"] == rev and event is None


def test_missing_duplicate_settle_restart_and_restore():
    import json
    for bad in ("missing", "ambiguous"):
        record, _ = advance(None, candidate(1), 1000)
        record, _ = advance(record, {"quality": bad}, 1001)
        assert record["member"]["observation"]["quality"] == "stale"
        assert record["member"]["terminal"] == "completed"
        record = json.loads(json.dumps(record))  # persistent representation survives restart
        record, _ = advance(record, {"quality": bad}, 1302, sweep=False)
        assert record["member"]["observation"]["quality"] == "stale"  # callbacks are not sweeps
        record, change = advance(record, {"quality": bad}, 1303)
        assert record["member"]["status"] == bad and record["member"]["terminal"] is None
        assert record["member"]["observation"]["observed_at"].endswith("Z")
        record, change = advance(record, candidate(1), 1304)
        assert record["member"]["observation"]["quality"] == "fresh"
        assert record["member"]["terminal"] == "completed" and record["member"]["obs_rev"] == 1
        assert change is None


def test_error_preserves_last_good_and_does_not_settle_missing():
    record, _ = advance(None, candidate(1), 1000)
    for now in (1001, 2000):
        record, change = advance(record, {"quality": "error", "error": "work_yaml_invalid"}, now)
        assert record["member"]["terminal"] == "completed" and change is None
        assert record["member"]["observation"]["quality"] == "error"


def test_epic_preview_expands_only_specs_once(tmp_path, monkeypatch):
    import json
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "epics.json").write_text(json.dumps([{"id": "epic_demo", "members": [
        ID, "work_demo__bridge", ID, "spec_demo__archived", "spec_demo__unknown"]}]))
    (catalog / "documents.json").write_text(json.dumps([{"id": ID, "type": "spec"},
                                                        {"id": "work_demo__bridge", "type": "work"}]))
    (catalog / "archive.json").write_text(json.dumps([{"id": "spec_demo__archived", "type": "spec"}]))
    service = subsystem(tmp_path, monkeypatch)
    before = [(p.name, p.read_bytes()) for p in catalog.iterdir()]
    assert service.epic_spec_members("epic_demo") == [ID, "spec_demo__archived"]
    assert [(p.name, p.read_bytes()) for p in catalog.iterdir()] == before
